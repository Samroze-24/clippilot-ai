"""Pexels stock B-roll — real Pexels API client (httpx only).

Searches Pexels stock videos and downloads a chosen file into the local
B-roll folder, where the existing renderer can overlay it on a clip.

Without a key, everything fails closed with "Not configured" — search
results are NEVER faked. The API key is read from the environment only and
never logged.

Env var (placeholder only in .env.example — never a real key):
  PEXELS_API_KEY   from https://www.pexels.com/api/ (free tier, no card)

Endpoints used:
  GET  https://api.pexels.com/videos/search?query=...&per_page=...&orientation=portrait
  (file download) the video_files[].link returned by the search
"""
import os

import httpx

PEXELS_SEARCH_URL = "https://api.pexels.com/videos/search"
PEXELS_FILE_HOST = "videos.pexels.com"


class PexelsError(Exception):
    """Raised for any Pexels failure (never carries the API key)."""


def pexels_config() -> dict:
    return {"api_key": os.getenv("PEXELS_API_KEY", "").strip()}


def pexels_configured() -> bool:
    return bool(pexels_config()["api_key"])


def _headers() -> dict:
    return {"Authorization": pexels_config()["api_key"]}


def search_pexels(query: str, per_page: int = 6) -> list[dict]:
    """Search Pexels stock videos. Returns a list of
    {id, width, height, duration, url, image, files: [{link, width, height,
    quality, file_type}]}. Raises PexelsError when not configured or the
    API call fails. Portrait orientation is requested — B-roll is overlaid
    on 9:16 clips."""
    if not pexels_configured():
        raise PexelsError(
            "Pexels B-roll is not configured. Get a free API key at "
            "https://www.pexels.com/api/ and set PEXELS_API_KEY in .env, "
            "then restart."
        )
    query = (query or "").strip()
    if not query:
        raise PexelsError("Search needs a query (e.g. 'city night', 'coffee').")
    per_page = max(1, min(15, int(per_page or 6)))
    try:
        resp = httpx.get(
            PEXELS_SEARCH_URL,
            params={"query": query, "per_page": per_page, "orientation": "portrait"},
            headers=_headers(), timeout=30)
    except Exception as exc:
        raise PexelsError(f"Pexels search failed ({type(exc).__name__})") from exc
    if resp.status_code == 401:
        raise PexelsError("Pexels API key rejected (401) — check PEXELS_API_KEY in .env")
    if resp.status_code == 429:
        raise PexelsError("Pexels rate limit hit (429) — try again later")
    if resp.status_code != 200:
        raise PexelsError(f"Pexels API error {resp.status_code}")
    try:
        data = resp.json()
    except Exception as exc:
        raise PexelsError("Pexels returned a non-JSON response") from exc
    out = []
    for v in data.get("videos") or []:
        files = []
        for f in v.get("video_files") or []:
            link = f.get("link") or ""
            if link:
                files.append({
                    "link": link,
                    "width": f.get("width") or 0,
                    "height": f.get("height") or 0,
                    "quality": f.get("quality") or "",
                    "file_type": f.get("file_type") or "",
                })
        if not files:
            continue
        out.append({
            "id": v.get("id"),
            "width": v.get("width") or 0,
            "height": v.get("height") or 0,
            "duration": v.get("duration") or 0,
            "url": v.get("url") or "",
            "image": (v.get("image") or ""),
            "files": files,
        })
    return out


def download_pexels_file(file_link: str, dest_path: str) -> str:
    """Download one Pexels video file to dest_path. The link must be a
    Pexels CDN link (videos.pexels.com) — any other host is refused, so a
    crafted request cannot make the server fetch arbitrary URLs. Returns
    the destination path. Raises PexelsError on failure."""
    if not pexels_configured():
        raise PexelsError(
            "Pexels B-roll is not configured. Get a free API key at "
            "https://www.pexels.com/api/ and set PEXELS_API_KEY in .env, "
            "then restart."
        )
    link = (file_link or "").strip()
    if not link.startswith("https://") or PEXELS_FILE_HOST not in link.split("/")[2]:
        raise PexelsError(
            "That file link is not a Pexels video file link — pick a file "
            "from a Pexels search result instead."
        )
    try:
        with httpx.stream("GET", link, timeout=300) as resp:
            if resp.status_code != 200:
                raise PexelsError(f"Pexels download failed (HTTP {resp.status_code})")
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_bytes(1024 * 1024):
                    f.write(chunk)
    except PexelsError:
        raise
    except Exception as exc:
        raise PexelsError(f"Pexels download failed ({type(exc).__name__})") from exc
    if not os.path.exists(dest_path) or os.path.getsize(dest_path) == 0:
        raise PexelsError("Pexels download finished but produced no file.")
    return dest_path
