"""Direct publishing — YouTube Data API v3 (OAuth2 + resumable upload).

httpx only — no google-api-python-client / google-auth-oauthlib needed.

OAuth2 authorization-code flow:
  GET  /api/publish/youtube/auth-url   -> Google consent URL (user clicks Connect)
  GET  /api/publish/youtube/callback   -> Google redirects here with a code;
                                          exchanged for tokens (state-verified)

Tokens live in data/youtube_token.json — a LOCAL file, gitignored, never in
the ZIP and never in the repo. The access token is refreshed automatically
when it is close to expiring.

Without credentials configured, or without a connected account, every
publish call fails closed with "Not configured" / "Not connected" — a
publish is NEVER faked. A successful publish is only ever reported after a
real YouTube API response containing a video id.

Env vars (placeholders only in .env.example — never real secrets):
  YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET,
  YOUTUBE_REDIRECT_URI (default http://127.0.0.1:8012/api/publish/youtube/callback)
"""
import html
import json
import os
import secrets
import time
from urllib.parse import urlencode

import httpx

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
YOUTUBE_API_URL = "https://www.googleapis.com/youtube/v3"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8012/api/publish/youtube/callback"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
TOKEN_PATH = os.path.join(DATA_DIR, "youtube_token.json")

# state -> {"user_id": int, "expires": float}; CSRF protection for the OAuth
# callback (the callback is a plain browser navigation, so it cannot carry
# the dashboard's localStorage JWT — the state is the verification).
_pending_states: dict[str, dict] = {}
STATE_TTL_SEC = 600


class PublishError(Exception):
    """Raised for any publishing failure (never carries secrets)."""


def youtube_config() -> dict:
    return {
        "client_id": os.getenv("YOUTUBE_CLIENT_ID", "").strip(),
        "client_secret": os.getenv("YOUTUBE_CLIENT_SECRET", "").strip(),
        "redirect_uri": os.getenv("YOUTUBE_REDIRECT_URI", DEFAULT_REDIRECT_URI).strip()
        or DEFAULT_REDIRECT_URI,
    }


def youtube_configured() -> bool:
    cfg = youtube_config()
    return bool(cfg["client_id"] and cfg["client_secret"])


# ---- token storage (local file only) ----

def load_tokens() -> dict | None:
    try:
        with open(TOKEN_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def save_tokens(tokens: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = TOKEN_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tokens, f)
    os.replace(tmp, TOKEN_PATH)


def clear_tokens() -> None:
    try:
        os.remove(TOKEN_PATH)
    except OSError:
        pass


def youtube_connected() -> bool:
    tokens = load_tokens()
    return bool(tokens and tokens.get("refresh_token"))


# ---- OAuth flow ----

def build_auth_url(user_id: int) -> tuple[str, str]:
    """Return (auth_url, state). Raises PublishError when not configured."""
    cfg = youtube_config()
    if not cfg["client_id"] or not cfg["client_secret"]:
        raise PublishError(
            "YouTube publishing is not configured. Set YOUTUBE_CLIENT_ID and "
            "YOUTUBE_CLIENT_SECRET in .env (see README), restart, then connect."
        )
    state = secrets.token_hex(16)
    _pending_states[state] = {"user_id": user_id, "expires": time.time() + STATE_TTL_SEC}
    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": cfg["redirect_uri"],
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    query = urlencode(params)
    return f"{GOOGLE_AUTH_URL}?{query}", state


def consume_state(state: str) -> int | None:
    """Validate a callback state once; returns the initiating user_id or None."""
    now = time.time()
    for s in [s for s, v in _pending_states.items() if v["expires"] < now]:
        del _pending_states[s]
    entry = _pending_states.pop(state, None)
    return entry["user_id"] if entry else None


def _google_error_text(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        err = data.get("error", {})
        if isinstance(err, dict):
            return err.get("message", str(err))
        return str(err)
    except Exception:
        return (resp.text or "")[:300]


def exchange_code(code: str) -> dict:
    """Exchange an authorization code for tokens and save them locally."""
    cfg = youtube_config()
    try:
        resp = httpx.post(GOOGLE_TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "redirect_uri": cfg["redirect_uri"],
        }, timeout=30)
    except Exception as exc:
        raise PublishError(f"YouTube authorization failed ({type(exc).__name__})") from exc
    if resp.status_code != 200:
        raise PublishError(f"YouTube authorization failed: {_google_error_text(resp)}")
    data = resp.json()
    tokens = {
        "access_token": data.get("access_token", ""),
        "refresh_token": data.get("refresh_token", ""),
        "token_type": data.get("token_type", "Bearer"),
        "scope": data.get("scope", ""),
        "expires_at": time.time() + int(data.get("expires_in", 3600)),
    }
    save_tokens(tokens)
    return tokens


def _refresh_tokens(tokens: dict) -> dict:
    cfg = youtube_config()
    try:
        resp = httpx.post(GOOGLE_TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
        }, timeout=30)
    except Exception as exc:
        raise PublishError(f"YouTube token refresh failed ({type(exc).__name__})") from exc
    if resp.status_code != 200:
        raise PublishError(
            "YouTube session expired — please reconnect your account. "
            f"({_google_error_text(resp)})"
        )
    data = resp.json()
    new_tokens = dict(tokens)
    new_tokens["access_token"] = data.get("access_token", "")
    new_tokens["expires_at"] = time.time() + int(data.get("expires_in", 3600))
    if data.get("refresh_token"):
        new_tokens["refresh_token"] = data["refresh_token"]
    save_tokens(new_tokens)
    return new_tokens


def get_valid_access_token() -> str:
    """Access token, refreshed automatically when close to expiry."""
    tokens = load_tokens()
    if not tokens or not tokens.get("access_token"):
        raise PublishError(
            "YouTube is not connected. Connect it on the Publishing tab first."
        )
    if tokens.get("expires_at", 0) - time.time() < 60:
        if not tokens.get("refresh_token"):
            raise PublishError("YouTube session expired — please reconnect your account.")
        tokens = _refresh_tokens(tokens)
    return tokens["access_token"]


def fetch_channel_title(access_token: str) -> str | None:
    """Best-effort channel title for the connected account (may be None)."""
    try:
        resp = httpx.get(
            f"{YOUTUBE_API_URL}/channels",
            params={"part": "snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        if resp.status_code != 200:
            return None
        items = resp.json().get("items") or []
        if items:
            return (items[0].get("snippet") or {}).get("title")
    except Exception:
        pass
    return None


# ---- resumable upload ----

def _init_upload_session(access_token: str, title: str, description: str,
                         privacy: str, file_size: int, content_type: str = "video/mp4") -> str:
    """Start a resumable upload session; returns the session upload URL."""
    url = f"{YOUTUBE_UPLOAD_URL}?uploadType=resumable&part=snippet,status"
    body = {
        "snippet": {"title": title[:100], "description": description[:5000],
                    "categoryId": "22"},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    try:
        resp = httpx.post(url, json=body, headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": content_type,
            "X-Upload-Content-Length": str(file_size),
        }, timeout=60)
    except Exception as exc:
        raise PublishError(f"YouTube upload failed ({type(exc).__name__})") from exc
    if resp.status_code == 401:
        raise _Unauthorized()
    if resp.status_code != 200:
        raise PublishError(f"YouTube upload failed: {_google_error_text(resp)}")
    location = resp.headers.get("location") or resp.headers.get("Location")
    if not location:
        raise PublishError("YouTube did not return an upload URL.")
    return location


class _Unauthorized(Exception):
    pass


def upload_to_youtube(file_path: str, title: str, description: str,
                      privacy: str = "private") -> dict:
    """Upload a rendered clip file to YouTube (resumable upload).

    Returns {"video_id", "watch_url", "title", "privacy"} — only after a real
    YouTube API response containing a video id. Retries once after a token
    refresh on 401. Raises PublishError on any failure (never fakes success).
    """
    if privacy not in ("private", "unlisted", "public"):
        raise PublishError("privacy must be private, unlisted or public")
    if not os.path.exists(file_path):
        raise PublishError("Clip file not found — generate the clip first.")
    size = os.path.getsize(file_path)
    if size <= 0:
        raise PublishError("Clip file is empty — generate the clip first.")
    with open(file_path, "rb") as f:
        data = f.read()
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            access_token = get_valid_access_token()
            session_url = _init_upload_session(
                access_token, title, description, privacy, size)
            try:
                resp = httpx.put(session_url, content=data, headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(size),
                }, timeout=600)
            except Exception as exc:
                raise PublishError(f"YouTube upload failed ({type(exc).__name__})") from exc
            if resp.status_code == 401:
                raise _Unauthorized()
            if resp.status_code not in (200, 201):
                raise PublishError(f"YouTube upload failed: {_google_error_text(resp)}")
            video_id = (resp.json() or {}).get("id")
            if not video_id:
                raise PublishError("YouTube upload finished but returned no video id.")
            return {
                "video_id": video_id,
                "watch_url": f"https://www.youtube.com/watch?v={video_id}",
                "title": title,
                "privacy": privacy,
            }
        except _Unauthorized:
            # Token went stale mid-upload: refresh and retry once.
            tokens = load_tokens()
            if tokens and tokens.get("refresh_token") and attempt == 0:
                try:
                    _refresh_tokens(tokens)
                except PublishError as exc:
                    last_exc = exc
                    break
                last_exc = None
                continue
            last_exc = PublishError("YouTube authorization expired — please reconnect your account.")
            break
        except PublishError as exc:
            last_exc = exc
            break
    raise last_exc or PublishError("YouTube upload failed.")


def callback_page_html(success: bool, message: str) -> str:
    """Simple result page shown after the Google OAuth redirect."""
    title = "YouTube connected" if success else "YouTube connection failed"
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>ClipPilot AI — YouTube</title></head>"
        "<body style='font-family:sans-serif;max-width:560px;margin:60px auto;padding:0 16px'>"
        f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>"
        "<p>You can close this tab and go back to ClipPilot.</p>"
        "</body></html>"
    )


# ======================================================================
# TikTok — Content Posting API (OAuth2 + direct video upload)
#
# Flow:
#   GET  /api/publish/tiktok/auth-url -> TikTok authorize page (video.upload)
#   GET  /api/publish/tiktok/callback -> code exchanged for tokens
#   POST /api/publish/tiktok/upload   -> init -> PUT upload_url -> status
# Tokens live in data/tiktok_token.json (local file, gitignored, never in
# the ZIP or repo). Uploads default to SELF_ONLY (only you can see them).
#
# Without a TikTok developer app + approval, every call fails closed with
# "Not configured" — a TikTok post is NEVER faked. A successful publish is
# only reported after a real TikTok API response containing a publish id.
# NOTE: TikTok requires a developer app with the video.upload scope, which
# needs TikTok's approval (see README). A sandbox/regular account cannot
# skip that step.
#
# Env vars (placeholders only in .env.example — never real secrets):
#   TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET,
#   TIKTOK_REDIRECT_URI (default http://127.0.0.1:8012/api/publish/tiktok/callback)
# ======================================================================

TIKTOK_AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
TIKTOK_STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
TIKTOK_SCOPES = ["video.upload"]
TIKTOK_DEFAULT_REDIRECT_URI = "http://127.0.0.1:8012/api/publish/tiktok/callback"
TIKTOK_PRIVACY_LEVELS = {"PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS",
                         "FOLLOWER_OF_CREATOR", "SELF_ONLY"}
TIKTOK_TOKEN_PATH = os.path.join(DATA_DIR, "tiktok_token.json")

_pending_tiktok_states: dict[str, dict] = {}


def tiktok_config() -> dict:
    return {
        "client_key": os.getenv("TIKTOK_CLIENT_KEY", "").strip(),
        "client_secret": os.getenv("TIKTOK_CLIENT_SECRET", "").strip(),
        "redirect_uri": os.getenv("TIKTOK_REDIRECT_URI", TIKTOK_DEFAULT_REDIRECT_URI).strip()
        or TIKTOK_DEFAULT_REDIRECT_URI,
    }


def tiktok_configured() -> bool:
    cfg = tiktok_config()
    return bool(cfg["client_key"] and cfg["client_secret"])


def load_tiktok_tokens() -> dict | None:
    try:
        with open(TIKTOK_TOKEN_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def save_tiktok_tokens(tokens: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = TIKTOK_TOKEN_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tokens, f)
    os.replace(tmp, TIKTOK_TOKEN_PATH)


def clear_tiktok_tokens() -> None:
    try:
        os.remove(TIKTOK_TOKEN_PATH)
    except OSError:
        pass


def tiktok_connected() -> bool:
    tokens = load_tiktok_tokens()
    return bool(tokens and tokens.get("access_token"))


def build_tiktok_auth_url(user_id: int) -> tuple[str, str]:
    """Return (auth_url, state). Raises PublishError when not configured."""
    cfg = tiktok_config()
    if not cfg["client_key"] or not cfg["client_secret"]:
        raise PublishError(
            "TikTok publishing is not configured. Register an app at "
            "developers.tiktok.com, request the video.upload scope, and set "
            "TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET in .env (see README), "
            "then connect."
        )
    state = secrets.token_hex(16)
    _pending_tiktok_states[state] = {"user_id": user_id, "expires": time.time() + STATE_TTL_SEC}
    params = {
        "client_key": cfg["client_key"],
        "scope": ",".join(TIKTOK_SCOPES),
        "response_type": "code",
        "redirect_uri": cfg["redirect_uri"],
        "state": state,
    }
    return f"{TIKTOK_AUTH_URL}?{urlencode(params)}", state


def consume_tiktok_state(state: str) -> int | None:
    now = time.time()
    for s in [s for s, v in _pending_tiktok_states.items() if v["expires"] < now]:
        del _pending_tiktok_states[s]
    entry = _pending_tiktok_states.pop(state, None)
    return entry["user_id"] if entry else None


def _tiktok_error_text(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        err = data.get("error", {})
        if isinstance(err, dict):
            return str(err.get("message", err))[:300]
        return str(err)[:300]
    except Exception:
        return (resp.text or "")[:300]


def exchange_tiktok_code(code: str) -> dict:
    """Exchange an authorization code for tokens and save them locally."""
    cfg = tiktok_config()
    try:
        resp = httpx.post(TIKTOK_TOKEN_URL, data={
            "client_key": cfg["client_key"],
            "client_secret": cfg["client_secret"],
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": cfg["redirect_uri"],
        }, timeout=30)
    except Exception as exc:
        raise PublishError(f"TikTok authorization failed ({type(exc).__name__})") from exc
    if resp.status_code != 200:
        raise PublishError(f"TikTok authorization failed: {_tiktok_error_text(resp)}")
    data = resp.json()
    tokens = {
        "access_token": data.get("access_token", ""),
        "refresh_token": data.get("refresh_token", ""),
        "open_id": data.get("open_id", ""),
        "scope": data.get("scope", ""),
        "expires_at": time.time() + int(data.get("expires_in", 86400)),
    }
    save_tiktok_tokens(tokens)
    return tokens


def _refresh_tiktok_tokens(tokens: dict) -> dict:
    cfg = tiktok_config()
    try:
        resp = httpx.post(TIKTOK_TOKEN_URL, data={
            "client_key": cfg["client_key"],
            "client_secret": cfg["client_secret"],
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        }, timeout=30)
    except Exception as exc:
        raise PublishError(f"TikTok token refresh failed ({type(exc).__name__})") from exc
    if resp.status_code != 200:
        raise PublishError(
            "TikTok session expired — please reconnect your account. "
            f"({_tiktok_error_text(resp)})"
        )
    data = resp.json()
    new_tokens = dict(tokens)
    new_tokens["access_token"] = data.get("access_token", "")
    new_tokens["expires_at"] = time.time() + int(data.get("expires_in", 86400))
    if data.get("refresh_token"):
        new_tokens["refresh_token"] = data["refresh_token"]
    save_tiktok_tokens(new_tokens)
    return new_tokens


def get_valid_tiktok_token() -> str:
    tokens = load_tiktok_tokens()
    if not tokens or not tokens.get("access_token"):
        raise PublishError("TikTok is not connected. Connect it on the Publishing tab first.")
    if tokens.get("expires_at", 0) - time.time() < 60:
        if not tokens.get("refresh_token"):
            raise PublishError("TikTok session expired — please reconnect your account.")
        tokens = _refresh_tiktok_tokens(tokens)
    return tokens["access_token"]


def upload_to_tiktok(file_path: str, title: str,
                     privacy: str = "SELF_ONLY") -> dict:
    """Upload a rendered clip to TikTok (Content Posting API, direct post).

    Returns {"publish_id", "status", "title", "privacy"} — only after a real
    TikTok API response containing a publish id. The status is usually
    "PROCESSING" right after upload (TikTok processes the video on its
    side); the publish id is the real reference to check in the TikTok app.
    Raises PublishError on any failure (never fakes success)."""
    if privacy not in TIKTOK_PRIVACY_LEVELS:
        raise PublishError("privacy must be SELF_ONLY, PUBLIC_TO_EVERYONE, "
                           "MUTUAL_FOLLOW_FRIENDS or FOLLOWER_OF_CREATOR")
    if not os.path.exists(file_path):
        raise PublishError("Clip file not found — generate the clip first.")
    size = os.path.getsize(file_path)
    if size <= 0:
        raise PublishError("Clip file is empty — generate the clip first.")
    if not tiktok_configured():
        raise PublishError(
            "TikTok publishing is not configured. Register an app at "
            "developers.tiktok.com and set TIKTOK_CLIENT_KEY and "
            "TIKTOK_CLIENT_SECRET in .env (see README).")
    access_token = get_valid_tiktok_token()
    headers = {"Authorization": f"Bearer {access_token}",
               "Content-Type": "application/json; charset=UTF-8"}
    try:
        resp = httpx.post(TIKTOK_INIT_URL, json={
            "post_info": {
                "title": title[:220],
                "privacy_level": privacy,
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
                "video_cover_timestamp_ms": 1000,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": size,
                "total_chunk_count": 1,
            },
        }, headers=headers, timeout=60)
    except Exception as exc:
        raise PublishError(f"TikTok upload failed ({type(exc).__name__})") from exc
    if resp.status_code == 401:
        raise PublishError("TikTok authorization expired — please reconnect your account.")
    if resp.status_code != 200:
        raise PublishError(f"TikTok upload failed: {_tiktok_error_text(resp)}")
    try:
        data = (resp.json() or {}).get("data") or {}
        publish_id = data["publish_id"]
        upload_url = data["upload_url"]
    except Exception as exc:
        raise PublishError("TikTok did not return an upload URL.") from exc
    with open(file_path, "rb") as f:
        video_bytes = f.read()
    try:
        put = httpx.put(upload_url, content=video_bytes, headers={
            "Content-Type": "video/mp4",
            "Content-Range": f"bytes 0-{size - 1}/{size}",
        }, timeout=600)
    except Exception as exc:
        raise PublishError(f"TikTok upload failed ({type(exc).__name__})") from exc
    if put.status_code not in (200, 201, 204):
        raise PublishError(f"TikTok upload failed (HTTP {put.status_code})")
    try:
        st = httpx.post(TIKTOK_STATUS_URL, json={"publish_id": publish_id},
                        headers=headers, timeout=60)
        status = ((st.json() or {}).get("data") or {}).get("status", "PROCESSING")
    except Exception:
        status = "PROCESSING"
    return {"publish_id": publish_id, "status": status,
            "title": title, "privacy": privacy,
            "note": "TikTok is processing the video — check the publish id in the TikTok app."}


def tiktok_callback_page_html(success: bool, message: str) -> str:
    title = "TikTok connected" if success else "TikTok connection failed"
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>ClipPilot AI — TikTok</title></head>"
        "<body style='font-family:sans-serif;max-width:560px;margin:60px auto;padding:0 16px'>"
        f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>"
        "<p>You can close this tab and go back to ClipPilot.</p>"
        "</body></html>"
    )


# ======================================================================
# Instagram — Instagram Graph API (Reels)
#
# Flow (no in-app OAuth: the user supplies a long-lived access token):
#   POST https://graph.facebook.com/v20.0/{ig_user_id}/media
#        {media_type: "REELS", video_url, caption} -> container id
#   POST https://graph.facebook.com/v20.0/{ig_user_id}/media_publish
#        {creation_id} -> media id
#   GET  https://graph.facebook.com/v20.0/{media_id}?fields=permalink
#
# HARD REQUIREMENT (Instagram's, not ClipPilot's): the video must sit at a
# PUBLICLY reachable URL — Instagram's servers download it. A localhost
# file cannot be published. Set INSTAGRAM_VIDEO_BASE_URL to a public host
# that serves your clip files (e.g. an S3 bucket or any public folder);
# without it the endpoint fails closed with the exact steps. A successful
# publish is only reported after a real Graph API response with a media id.
#
# Env vars (placeholders only in .env.example — never real secrets):
#   INSTAGRAM_APP_ID, INSTAGRAM_APP_SECRET (developer app),
#   INSTAGRAM_ACCESS_TOKEN (long-lived user token),
#   INSTAGRAM_USER_ID (Instagram Business/Creator account id),
#   INSTAGRAM_VIDEO_BASE_URL (public host for clip files, e.g. https://files.example.com)
# ======================================================================

INSTAGRAM_API_URL = "https://graph.facebook.com/v20.0"


def instagram_config() -> dict:
    return {
        "app_id": os.getenv("INSTAGRAM_APP_ID", "").strip(),
        "app_secret": os.getenv("INSTAGRAM_APP_SECRET", "").strip(),
        "access_token": os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip(),
        "user_id": os.getenv("INSTAGRAM_USER_ID", "").strip(),
        "video_base_url": os.getenv("INSTAGRAM_VIDEO_BASE_URL", "").strip().rstrip("/"),
    }


def instagram_configured() -> bool:
    cfg = instagram_config()
    return bool(cfg["access_token"] and cfg["user_id"])


def instagram_public_url_ready() -> bool:
    cfg = instagram_config()
    return instagram_configured() and bool(cfg["video_base_url"])


def _instagram_error_text(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        err = data.get("error", {})
        if isinstance(err, dict):
            return str(err.get("message", err))[:300]
        return str(err)[:300]
    except Exception:
        return (resp.text or "")[:300]


def publish_to_instagram(video_public_url: str, caption: str) -> dict:
    """Publish a Reels video to Instagram via the Graph API.

    video_public_url must be a publicly reachable URL of the clip file.
    Returns {"media_id", "permalink", "caption"} — only after a real Graph
    API response containing a media id. Raises PublishError on any failure
    (never fakes a post)."""
    cfg = instagram_config()
    if not cfg["access_token"] or not cfg["user_id"]:
        raise PublishError(
            "Instagram publishing is not configured. Create a Meta developer "
            "app, add the Instagram Graph API product, connect a "
            "Business/Creator account and set INSTAGRAM_ACCESS_TOKEN and "
            "INSTAGRAM_USER_ID in .env (see README).")
    if not video_public_url.startswith("https://"):
        raise PublishError(
            "Instagram needs the clip at a PUBLIC https URL — its servers "
            "download the video. Set INSTAGRAM_VIDEO_BASE_URL in .env to a "
            "public host that serves your clip files (localhost will not work).")
    base_params = {"access_token": cfg["access_token"]}
    try:
        create = httpx.post(f"{INSTAGRAM_API_URL}/{cfg['user_id']}/media",
                            data={**base_params, "media_type": "REELS",
                                  "video_url": video_public_url,
                                  "caption": caption[:2200]},
                            timeout=120)
    except Exception as exc:
        raise PublishError(f"Instagram publish failed ({type(exc).__name__})") from exc
    if create.status_code != 200:
        raise PublishError(f"Instagram publish failed: {_instagram_error_text(create)}")
    try:
        container_id = create.json()["id"]
    except Exception as exc:
        raise PublishError("Instagram did not return a media container id.") from exc
    try:
        pub = httpx.post(f"{INSTAGRAM_API_URL}/{cfg['user_id']}/media_publish",
                         data={**base_params, "creation_id": container_id},
                         timeout=120)
    except Exception as exc:
        raise PublishError(f"Instagram publish failed ({type(exc).__name__})") from exc
    if pub.status_code != 200:
        raise PublishError(f"Instagram publish failed: {_instagram_error_text(pub)}")
    try:
        media_id = pub.json()["id"]
    except Exception as exc:
        raise PublishError("Instagram did not return a media id.") from exc
    permalink = ""
    try:
        got = httpx.get(f"{INSTAGRAM_API_URL}/{media_id}",
                        params={**base_params, "fields": "permalink"},
                        timeout=30)
        if got.status_code == 200:
            permalink = (got.json() or {}).get("permalink", "")
    except Exception:
        pass
    return {"media_id": media_id, "permalink": permalink, "caption": caption}
