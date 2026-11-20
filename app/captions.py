"""Transcript parsing, keyword extraction, ASS caption building and local
Whisper transcription.

No cloud service is used here. Word timestamps come from faster-whisper when
available; SRT uploads get evenly-distributed estimated word timing (labelled
as estimated in the README).
"""
import os
import re
from collections import Counter

STOPWORDS = set(
    """a an the and or but if then else when at by for with about against between
    into through during before after above below to from up down in out on off
    over under again further once here there all any both each few more most
    other some such no nor not only own same so than too very can will just
    should now i you he she it we they me him her us them my your his its our
    their this that these those am is are was were be been being have has had
    having do does did doing would could ought i'm you're he's she's it's we're
    they're i've we've they've i'll you'll he'll she'll we'll they'll isn't
    aren't wasn't weren't hasn't haven't hadn't doesn't don't didn't won't
    wouldn't can't cannot couldn't shouldn't what's that's there's here's
    of as""".split()
)

TEMPLATES = {
    # name -> (font size, primary colour, border style, outline)
    "bold-yellow": {"size": 30, "colour": "&H00FFFF00", "border": 1, "outline": 2},
    "white-black": {"size": 28, "colour": "&H00FFFFFF", "border": 3, "outline": 1},
}


def srt_time_to_sec(t: str) -> float:
    m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)", t.strip())
    if not m:
        return 0.0
    h, mi, s, ms = (float(x) for x in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000.0


def parse_srt(text: str) -> list[dict]:
    """Parse SRT text into [{text, start, end}]."""
    blocks = re.split(r"\n\s*\n", text.strip())
    segments = []
    for block in blocks:
        lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        # First line may be an index number; find the timing line.
        time_idx = None
        for i, ln in enumerate(lines):
            if "-->" in ln:
                time_idx = i
                break
        if time_idx is None:
            continue
        start_s, end_s = lines[time_idx].split("-->")
        body = " ".join(lines[time_idx + 1:])
        body = re.sub(r"<[^>]+>", "", body).strip()
        if body:
            segments.append({
                "text": body,
                "start": srt_time_to_sec(start_s),
                "end": srt_time_to_sec(end_s),
            })
    segments.sort(key=lambda s: s["start"])
    return segments


def parse_txt(text: str, duration: float) -> list[dict]:
    """Plain-text transcript: split into sentences, distribute evenly across
    the video duration (estimated timing — never claimed as real)."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]
    if not sentences or duration <= 0:
        return []
    per = duration / len(sentences)
    return [
        {"text": s, "start": round(i * per, 2), "end": round((i + 1) * per, 2)}
        for i, s in enumerate(sentences)
    ]


def parse_transcript_file(path: str, duration: float = 0.0) -> list[dict]:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()
    if path.lower().endswith(".srt"):
        return parse_srt(text)
    return parse_txt(text, duration)


def words_from_segments(segments: list[dict]) -> list[dict]:
    """Evenly distribute each segment's duration across its words.
    Timing is estimated — Whisper gives real word timestamps instead."""
    words = []
    for seg in segments:
        tokens = seg["text"].split()
        if not tokens:
            continue
        span = max(seg["end"] - seg["start"], 0.1)
        per = span / len(tokens)
        for i, tok in enumerate(tokens):
            words.append({
                "word": tok,
                "start": round(seg["start"] + i * per, 2),
                "end": round(seg["start"] + (i + 1) * per, 2),
            })
    return words


def extract_keywords(text: str, top_n: int = 12) -> list[tuple[str, int]]:
    words = re.findall(r"[a-z][a-z\-]{2,}", text.lower())
    counts = Counter(w for w in words if w not in STOPWORDS)
    return counts.most_common(top_n)


def find_caption_font() -> tuple[str | None, str | None, str | None]:
    """Return (font_path, font_name, fonts_dir) or (None, None, None).
    Resolution order: CAPTION_FONT env -> Windows arial.ttf -> Linux DejaVu/Liberation."""
    env_font = os.getenv("CAPTION_FONT", "").strip()
    candidates = []
    if env_font:
        candidates.append((env_font, os.path.splitext(os.path.basename(env_font))[0]))
    candidates += [
        (r"C:\Windows\Fonts\arial.ttf", "Arial"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVu Sans"),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", "Liberation Sans"),
    ]
    for path, name in candidates:
        if path and os.path.exists(path):
            return path, name, os.path.dirname(path)
    return None, None, None


def _ass_time(sec: float) -> str:
    sec = max(sec, 0.0)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    cs = int(round((sec - int(sec)) * 100))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def build_ass(words: list[dict], template: str, hook_title: str,
              font_name: str, clip_duration: float) -> str:
    """Build an .ASS subtitle file with word-level karaoke timing.
    One Dialogue line per ~4s window; each word gets a \\k karaoke tag."""
    tpl = TEMPLATES.get(template, TEMPLATES["bold-yellow"])
    lines = []
    lines.append("[Script Info]")
    lines.append("ScriptType: v4.00+")
    lines.append("PlayResX: 720")
    lines.append("PlayResY: 1280")
    lines.append("")
    lines.append("[V4+ Styles]")
    lines.append("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
                 "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
                 "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
                 "Alignment, MarginL, MarginR, MarginV, Encoding")
    lines.append(
        f"Style: Caption,{font_name},{tpl['size']},{tpl['colour']},&H000000FF,"
        f"&H00000000,&H80000000,-1,0,0,0,100,100,0,0,{tpl['border']},{tpl['outline']},"
        f"0,2,40,40,90,1"
    )
    lines.append(
        f"Style: HookTitle,{font_name},44,&H00FFFFFF,&H000000FF,&H00000000,"
        f"&H80000000,-1,0,0,0,100,100,0,0,1,3,0,8,40,40,120,1"
    )
    lines.append("")
    lines.append("[Events]")
    lines.append("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text")
    if hook_title:
        lines.append(
            f"Dialogue: 0,{_ass_time(0)},{_ass_time(min(3.0, clip_duration))},"
            f"HookTitle,,0,0,0,,{hook_title}"
        )
    # Group words into lines of up to ~6 words / ~4 seconds.
    if words:
        group: list[dict] = []
        group_start = words[0]["start"]

        def flush(group, start):
            if not group:
                return
            end = group[-1]["end"]
            parts = []
            for w in group:
                dur_cs = max(1, int(round((w["end"] - w["start"]) * 100)))
                parts.append("{\\k%d}%s" % (dur_cs, w["word"]))
            text = " ".join(parts)
            lines.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Caption,,0,0,0,,{text}"
            )

        for w in words:
            if group and (w["start"] - group_start > 4.0 or len(group) >= 6):
                flush(group, group_start)
                group = []
                group_start = w["start"]
            group.append(w)
        flush(group, group_start)
    return "\n".join(lines) + "\n"


def transcribe_whisper(path: str, model_name: str | None = None) -> dict:
    """Local transcription with faster-whisper (tiny model, CPU, int8).
    Downloads the model from HuggingFace once (~75 MB). Raises RuntimeError
    with a clear message if the engine is not installed."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "Transcription engine not installed (faster-whisper missing). "
            "Install requirements.txt or upload an SRT/TXT transcript instead."
        ) from exc
    model_name = model_name or os.getenv("WHISPER_MODEL", "tiny")
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments_iter, info = model.transcribe(path, word_timestamps=True)
    segments: list[dict] = []
    words: list[dict] = []
    for seg in segments_iter:
        segments.append({"text": seg.text.strip(), "start": round(seg.start, 2), "end": round(seg.end, 2)})
        for w in (seg.words or []):
            words.append({"word": w.word.strip(), "start": round(w.start, 2), "end": round(w.end, 2)})
    return {"segments": segments, "words": words, "language": info.language or "en"}
