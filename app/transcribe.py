"""Cloud transcription via AssemblyAI (optional, httpx only).

When TRANSCRIPTION_PROVIDER=assemblyai and ASSEMBLYAI_API_KEY is set, the
video's audio is uploaded to AssemblyAI and transcribed with word-level
timestamps — the same {segments, words, language} shape as the local
faster-whisper path in app/captions.py, so the .ass caption pipeline is
shared. Default provider is "local" (faster-whisper, offline).

Fail-closed: with provider=assemblyai and no key, transcription refuses
with a clear error — it NEVER silently falls back to a fake transcript,
and it never claims a transcript is AssemblyAI's when it is not.

Env vars (placeholders only in .env.example — never a real key):
  TRANSCRIPTION_PROVIDER   "local" (default) or "assemblyai"
  ASSEMBLYAI_API_KEY       from https://www.assemblyai.com/ (free tier)

Flow (AssemblyAI REST API):
  POST https://api.assemblyai.com/v2/upload          (binary audio -> upload_url)
  POST https://api.assemblyai.com/v2/transcript      {"audio_url": ...} -> id
  GET  https://api.assemblyai.com/v2/transcript/{id} (poll until completed/error)
The transcript response carries "words": [{text, start, end, ...}] with
start/end in MILLISECONDS; segments are grouped from words by pauses.
"""
import os
import time

import httpx

ASSEMBLYAI_BASE = "https://api.assemblyai.com/v2"
POLL_INTERVAL_SEC = 3.0
POLL_TIMEOUT_SEC = 600.0


class AssemblyAIError(Exception):
    """Raised for any AssemblyAI failure (never carries the API key)."""


def transcription_provider() -> str:
    return os.getenv("TRANSCRIPTION_PROVIDER", "local").strip().lower() or "local"


def assemblyai_config() -> dict:
    return {"api_key": os.getenv("ASSEMBLYAI_API_KEY", "").strip()}


def assemblyai_configured() -> bool:
    return bool(assemblyai_config()["api_key"])


def _headers() -> dict:
    return {"authorization": assemblyai_config()["api_key"]}


def _error_text(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            return str(data.get("error") or data.get("message") or "")[:300]
    except Exception:
        pass
    return (resp.text or "")[:300]


def _words_to_segments(words: list[dict]) -> list[dict]:
    """Group word timestamps into readable segments: a new segment starts
    after a pause of 1s or more, or after ~8s / ~20 words."""
    segments: list[dict] = []
    current: list[dict] = []
    current_start = 0.0
    for w in words:
        if current:
            gap = w["start"] - current[-1]["end"]
            if gap >= 1.0 or (w["end"] - current_start) > 8.0 or len(current) >= 20:
                segments.append({
                    "text": " ".join(x["word"] for x in current).strip(),
                    "start": round(current_start, 2),
                    "end": round(current[-1]["end"], 2),
                })
                current = []
        if not current:
            current_start = w["start"]
        current.append(w)
    if current:
        segments.append({
            "text": " ".join(x["word"] for x in current).strip(),
            "start": round(current_start, 2),
            "end": round(current[-1]["end"], 2),
        })
    return [s for s in segments if s["text"]]


def transcribe_assemblyai(path: str, poll_interval: float = POLL_INTERVAL_SEC,
                          poll_timeout: float = POLL_TIMEOUT_SEC) -> dict:
    """Transcribe an audio/video file with AssemblyAI. Returns
    {"segments": [{text, start, end}], "words": [{word, start, end}],
    "language": "en"} — the same shape as captions.transcribe_whisper.
    Raises AssemblyAIError on any failure (never returns a fake transcript)."""
    if not assemblyai_configured():
        raise AssemblyAIError(
            "AssemblyAI transcription is not configured. Get a free API key "
            "at https://www.assemblyai.com/ and set ASSEMBLYAI_API_KEY in "
            ".env (with TRANSCRIPTION_PROVIDER=assemblyai), then restart. "
            "Local faster-whisper is used instead when the provider is 'local'."
        )
    if not os.path.exists(path):
        raise AssemblyAIError("Audio file not found — upload or ingest the video first.")
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as exc:
        raise AssemblyAIError(f"Could not read the audio file ({exc})") from exc
    # 1. Upload the audio.
    try:
        resp = httpx.post(f"{ASSEMBLYAI_BASE}/upload", content=data,
                          headers={**_headers(), "Content-Type": "application/octet-stream"},
                          timeout=600)
    except Exception as exc:
        raise AssemblyAIError(f"AssemblyAI upload failed ({type(exc).__name__})") from exc
    if resp.status_code == 401:
        raise AssemblyAIError("AssemblyAI API key rejected (401) — check ASSEMBLYAI_API_KEY in .env")
    if resp.status_code != 200:
        raise AssemblyAIError(f"AssemblyAI upload failed: {_error_text(resp) or resp.status_code}")
    try:
        upload_url = resp.json()["upload_url"]
    except Exception as exc:
        raise AssemblyAIError("AssemblyAI upload returned no upload URL") from exc
    # 2. Request the transcript.
    try:
        resp = httpx.post(f"{ASSEMBLYAI_BASE}/transcript",
                          json={"audio_url": upload_url},
                          headers=_headers(), timeout=60)
    except Exception as exc:
        raise AssemblyAIError(f"AssemblyAI transcript request failed ({type(exc).__name__})") from exc
    if resp.status_code == 401:
        raise AssemblyAIError("AssemblyAI API key rejected (401) — check ASSEMBLYAI_API_KEY in .env")
    if resp.status_code != 200:
        raise AssemblyAIError(f"AssemblyAI transcript request failed: {_error_text(resp) or resp.status_code}")
    try:
        transcript_id = resp.json()["id"]
    except Exception as exc:
        raise AssemblyAIError("AssemblyAI returned no transcript id") from exc
    # 3. Poll until completed or error.
    deadline = time.time() + poll_timeout
    while True:
        try:
            resp = httpx.get(f"{ASSEMBLYAI_BASE}/transcript/{transcript_id}",
                             headers=_headers(), timeout=60)
        except Exception as exc:
            raise AssemblyAIError(f"AssemblyAI status check failed ({type(exc).__name__})") from exc
        if resp.status_code == 401:
            raise AssemblyAIError("AssemblyAI API key rejected (401) — check ASSEMBLYAI_API_KEY in .env")
        if resp.status_code != 200:
            raise AssemblyAIError(f"AssemblyAI status check failed: {_error_text(resp) or resp.status_code}")
        try:
            data = resp.json()
        except Exception as exc:
            raise AssemblyAIError("AssemblyAI returned a non-JSON response") from exc
        status = data.get("status")
        if status == "completed":
            raw_words = data.get("words") or []
            words = []
            for w in raw_words:
                text = (w.get("text") or "").strip()
                if not text:
                    continue
                try:
                    start = float(w.get("start", 0)) / 1000.0
                    end = float(w.get("end", 0)) / 1000.0
                except (TypeError, ValueError):
                    continue
                words.append({"word": text, "start": round(start, 2), "end": round(end, 2)})
            language = (data.get("language_code") or "en")[:5]
            return {"segments": _words_to_segments(words), "words": words,
                    "language": language}
        if status == "error":
            raise AssemblyAIError(
                "AssemblyAI transcription failed: "
                + str(data.get("error") or "unknown error")[:300])
        if time.time() > deadline:
            raise AssemblyAIError(
                "AssemblyAI transcription timed out — the transcript was still "
                "processing after 10 minutes. Try again later.")
        time.sleep(poll_interval)
