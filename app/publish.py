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
