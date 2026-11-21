"""Twitch channel VOD import — real Twitch Helix API (httpx only).

Lists a channel's recent VODs (past broadcasts) so the user can import one
as a URL-ingest job — yt-dlp already downloads Twitch VOD URLs. Live
streams are NOT ingested (stated plainly in the README): a live stream has
no finished file to download.

Without credentials, everything fails closed with "Not configured" and the
exact setup steps. Credentials are read from the environment only and never
logged.

Env vars (placeholders only in .env.example — never real secrets):
  TWITCH_CLIENT_ID       from https://dev.twitch.tv/console/apps
  TWITCH_CLIENT_SECRET   same app (new secret)

Flow:
  POST https://id.twitch.tv/oauth2/token (client_credentials) -> app token
  GET  https://api.twitch.tv/helix/users?login=<name>        -> user id
  GET  https://api.twitch.tv/helix/videos?user_id=...&type=archive
"""
import os
import time

import httpx

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_API_URL = "https://api.twitch.tv/helix"

_token_cache: dict = {"token": "", "expires_at": 0.0}


class TwitchError(Exception):
    """Raised for any Twitch failure (never carries client secrets)."""


def twitch_config() -> dict:
    return {
        "client_id": os.getenv("TWITCH_CLIENT_ID", "").strip(),
        "client_secret": os.getenv("TWITCH_CLIENT_SECRET", "").strip(),
    }


def twitch_configured() -> bool:
    cfg = twitch_config()
    return bool(cfg["client_id"] and cfg["client_secret"])


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Client-Id": twitch_config()["client_id"],
    }


def _app_token() -> str:
    """Client-credentials app token, cached in memory until it expires."""
    if _token_cache["token"] and _token_cache["expires_at"] - time.time() > 60:
        return _token_cache["token"]
    cfg = twitch_config()
    if not cfg["client_id"] or not cfg["client_secret"]:
        raise TwitchError(
            "Twitch import is not configured. Create an app at "
            "https://dev.twitch.tv/console/apps and set TWITCH_CLIENT_ID and "
            "TWITCH_CLIENT_SECRET in .env, then restart."
        )
    try:
        resp = httpx.post(TWITCH_TOKEN_URL, params={
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "grant_type": "client_credentials",
        }, timeout=30)
    except Exception as exc:
        raise TwitchError(f"Twitch authorization failed ({type(exc).__name__})") from exc
    if resp.status_code in (400, 401, 403):
        raise TwitchError(
            "Twitch credentials rejected — check TWITCH_CLIENT_ID and "
            "TWITCH_CLIENT_SECRET in .env")
    if resp.status_code != 200:
        raise TwitchError(f"Twitch authorization failed (HTTP {resp.status_code})")
    try:
        data = resp.json()
        token = data["access_token"]
        expires_in = int(data.get("expires_in", 3600))
    except Exception as exc:
        raise TwitchError("Twitch returned an unexpected token response") from exc
    _token_cache["token"] = token
    _token_cache["expires_at"] = time.time() + expires_in
    return token


def _helix_get(path: str, params: dict, token: str) -> dict:
    try:
        resp = httpx.get(f"{TWITCH_API_URL}{path}", params=params,
                         headers=_headers(token), timeout=30)
    except Exception as exc:
        raise TwitchError(f"Twitch API request failed ({type(exc).__name__})") from exc
    if resp.status_code in (401, 403):
        _token_cache["token"] = ""
        raise TwitchError(
            "Twitch rejected the request (401/403) — check TWITCH_CLIENT_ID "
            "and TWITCH_CLIENT_SECRET in .env")
    if resp.status_code != 200:
        raise TwitchError(f"Twitch API error (HTTP {resp.status_code})")
    try:
        return resp.json()
    except Exception as exc:
        raise TwitchError("Twitch returned a non-JSON response") from exc


def list_vods(channel: str, first: int = 10) -> list[dict]:
    """Recent VODs (past broadcasts) for a Twitch channel login name.
    Returns [{id, title, url, duration, created_at, view_count, language}].
    Raises TwitchError when not configured, the channel does not exist, or
    the API call fails. Live streams are not returned (no finished file)."""
    if not twitch_configured():
        raise TwitchError(
            "Twitch import is not configured. Create an app at "
            "https://dev.twitch.tv/console/apps and set TWITCH_CLIENT_ID and "
            "TWITCH_CLIENT_SECRET in .env, then restart."
        )
    channel = (channel or "").strip().lower()
    if not channel:
        raise TwitchError("Give a Twitch channel name (e.g. 'shroud').")
    first = max(1, min(20, int(first or 10)))
    token = _app_token()
    users = _helix_get("/users", {"login": channel}, token)
    user_list = users.get("data") or []
    if not user_list:
        raise TwitchError(f"Twitch channel '{channel}' not found.")
    user_id = user_list[0]["id"]
    display = user_list[0].get("display_name") or channel
    vods = _helix_get("/videos", {"user_id": user_id, "type": "archive",
                                  "first": first}, token)
    out = []
    for v in vods.get("data") or []:
        out.append({
            "id": v.get("id"),
            "title": v.get("title") or "Untitled VOD",
            "url": v.get("url") or "",
            "duration": v.get("duration") or "",
            "created_at": v.get("created_at") or "",
            "view_count": v.get("view_count") or 0,
            "language": v.get("language") or "",
            "channel": display,
        })
    return out
