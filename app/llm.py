"""Real LLM clip selection (Groq, free tier) — optional.

When configured, the LLM reads the video transcript and picks the top 3-7
viral-worthy moments, like supoclip does with Gemini/GPT/Claude. When it is
not configured — or on ANY failure — the caller falls back to the transparent
local scorer in app/video.py and says so in the response (selector "local").

Config (env vars; placeholders only in .env.example — never put a real key
in the repo, and never log the key anywhere):
  LLM_PROVIDER   "none" (default) or "groq"
  LLM_API_KEY    Groq API key from https://console.groq.com/keys
  LLM_MODEL      default "openai/gpt-oss-120b"
  LLM_BASE_URL   default "https://api.groq.com/openai/v1"

Only httpx is used (already a dependency) — no LLM SDK. The endpoint is
OpenAI-compatible chat completions, so another compatible provider can be
pointed at via LLM_BASE_URL / LLM_MODEL later.
"""
import json
import os

import httpx

DEFAULT_MODEL = "openai/gpt-oss-120b"
DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"

MIN_CLIP_SEC = 15.0
MAX_CLIP_SEC = 60.0
MAX_CLIPS = 7
MAX_TRANSCRIPT_CHARS = 12000


class LLMError(Exception):
    """Raised when the LLM call itself fails (never carries the API key)."""


def llm_config() -> dict:
    return {
        "provider": os.getenv("LLM_PROVIDER", "none").strip().lower(),
        "api_key": os.getenv("LLM_API_KEY", "").strip(),
        "model": os.getenv("LLM_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        "base_url": os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/") or DEFAULT_BASE_URL,
    }


def llm_configured() -> bool:
    cfg = llm_config()
    return cfg["provider"] == "groq" and bool(cfg["api_key"])


def _transcript_text(segments: list[dict]) -> str:
    """Compact transcript for the prompt: one line per segment with times."""
    lines = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"[{float(seg.get('start', 0)):.1f}-{float(seg.get('end', 0)):.1f}s] {text}")
    out = "\n".join(lines)
    if len(out) > MAX_TRANSCRIPT_CHARS:
        out = out[:MAX_TRANSCRIPT_CHARS] + "\n[... transcript truncated ...]"
    return out


def _chat_json(payload: dict, headers: dict, url: str, timeout: float = 60.0) -> dict:
    """POST a chat-completions request and return the parsed JSON body.

    Tries response_format json_object first; if the provider rejects it
    (400), retries once without it. Error messages never include the key.
    """
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    except Exception as exc:
        # Includes httpx.HTTPError and InvalidURL (e.g. a malformed proxy
        # env var in the sandbox) — any failure to even make the request.
        raise LLMError(f"LLM request failed ({type(exc).__name__})") from exc
    if resp.status_code == 400 and payload.get("response_format"):
        payload = {k: v for k, v in payload.items() if k != "response_format"}
        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
        except Exception as exc:
            raise LLMError(f"LLM request failed ({type(exc).__name__})") from exc
    if resp.status_code == 401:
        raise LLMError("LLM API key rejected (401) — check LLM_API_KEY in .env")
    if resp.status_code == 429:
        raise LLMError("LLM rate limit hit (429) — try again later")
    if resp.status_code != 200:
        raise LLMError(f"LLM API error {resp.status_code}")
    try:
        return resp.json()
    except Exception as exc:
        raise LLMError("LLM returned a non-JSON response") from exc


def validate_llm_clips(raw, duration: float) -> list[dict]:
    """Validate LLM output hard. Returns the valid clips (sorted by start).

    Rules: list (or {"clips": [...]}) of 1-7 entries; each needs numeric
    start/end inside [0, duration], length 15-60s; duplicates and clips
    overlapping >50% of the shorter one are dropped. Raises ValueError when
    nothing valid survives.
    """
    if isinstance(raw, dict):
        items = raw.get("clips")
    else:
        items = raw
    if not isinstance(items, list):
        raise ValueError("LLM output has no clips list")
    valid: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if start < 0 or end > duration or end <= start:
            continue
        length = end - start
        if length < MIN_CLIP_SEC or length > MAX_CLIP_SEC:
            continue
        title = str(item.get("title") or "").strip()[:120] or "Watch this moment"
        reason = str(item.get("reason") or "").strip()[:300]
        valid.append({"start": round(start, 2), "end": round(end, 2),
                      "title": title, "reason": reason})
    valid.sort(key=lambda c: c["start"])
    kept: list[dict] = []
    for cand in valid:
        bad_overlap = False
        for k in kept:
            inter = max(0.0, min(cand["end"], k["end"]) - max(cand["start"], k["start"]))
            shorter = min(cand["end"] - cand["start"], k["end"] - k["start"])
            if inter > 0.5 * shorter:
                bad_overlap = True
                break
        if not bad_overlap:
            kept.append(cand)
    if not kept:
        raise ValueError("LLM returned no valid clips (all out of range, wrong length or overlapping)")
    return kept[:MAX_CLIPS]


def select_clips_llm(segments: list[dict], duration: float):
    """Ask the LLM for clip picks. Returns (clips | None, detail).

    clips is a list of {start, end, title, reason} (validated), or None when
    the LLM is not configured or the call failed — the caller then falls back
    to the local scorer. detail is a short human-readable explanation.
    """
    cfg = llm_config()
    if cfg["provider"] != "groq" or not cfg["api_key"]:
        return None, "not_configured"
    if not segments:
        return None, "no_transcript"
    if duration <= 0:
        return None, "no_duration"
    system = (
        "You are a short-form video editor for TikTok, Reels and Shorts. "
        "Given a video transcript with timestamps, pick the most viral-worthy "
        "moments: strong hooks, complete thoughts, clear takeaways, high energy. "
        "Return ONLY strict JSON, no other text: "
        '{"clips": [{"start": seconds, "end": seconds, "title": "short hook title", '
        '"reason": "one sentence why this moment works"}]}. '
        "Rules: 3 to 7 clips; each clip 15 to 60 seconds long; start >= 0; "
        f"end <= {duration:.1f}; clips must not overlap each other."
    )
    user = (
        f"Video duration: {duration:.1f} seconds\n\n"
        "Transcript (start-end in seconds):\n"
        f"{_transcript_text(segments)}"
    )
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": 1500,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {cfg['api_key']}",
               "Content-Type": "application/json"}
    url = cfg["base_url"] + "/chat/completions"
    try:
        data = _chat_json(payload, headers, url)
        content = data["choices"][0]["message"]["content"]
        text = content.strip()
        # Tolerate a fenced code block around the JSON.
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.lower().startswith("json"):
                text = text[4:].strip()
        raw = json.loads(text)
    except LLMError as exc:
        return None, str(exc)
    except Exception as exc:
        return None, f"invalid_output: {type(exc).__name__}"
    try:
        clips = validate_llm_clips(raw, duration)
    except ValueError as exc:
        return None, f"invalid_output: {exc}"
    return clips, f"ok ({len(clips)} clips, {cfg['model']})"
