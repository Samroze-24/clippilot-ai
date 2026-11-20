"""Local video engine for ClipPilot AI.

Everything runs on local ffmpeg (bundled via imageio-ffmpeg, no separate
install) plus OpenCV for face-centered cropping. No cloud service, no API
key. The highlight scorer is a transparent local heuristic — every signal
and weight is documented in the README and returned in the API response.
"""
import json
import math
import os
import re
import shutil
import subprocess

from .captions import extract_keywords

PLAN_LIMITS = {
    "free": {"videos": 5, "clips": 20},
    "pro": {"videos": 100, "clips": 500},
}

HOOK_TEMPLATES = [
    "The {kw} trick nobody shows you",
    "{kw}: what actually works",
    "Stop doing {kw} wrong",
    "Why {kw} matters more than you think",
]

_FFMPEG = None


def ffmpeg_exe() -> str:
    """Bundled ffmpeg via imageio-ffmpeg; falls back to a system ffmpeg."""
    global _FFMPEG
    if _FFMPEG:
        return _FFMPEG
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            _FFMPEG = exe
            return exe
    except Exception:
        pass
    exe = shutil.which("ffmpeg")
    if exe:
        _FFMPEG = exe
        return exe
    raise RuntimeError(
        "No ffmpeg found: imageio-ffmpeg is not installed and no system "
        "ffmpeg is on PATH. Run: pip install -r requirements.txt"
    )


def ffmpeg_version() -> str:
    try:
        out = subprocess.run([ffmpeg_exe(), "-version"], capture_output=True,
                             text=True, timeout=30)
        return (out.stdout.splitlines() or [""])[0].strip()
    except Exception:
        return "unknown"


def _run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def probe_video(path: str) -> dict:
    """Duration + resolution from `ffmpeg -i` (works with the bundled binary,
    no ffprobe needed)."""
    out = _run([ffmpeg_exe(), "-i", path], timeout=60)
    text = out.stderr or ""
    dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", text)
    duration = 0.0
    if dur:
        duration = int(dur.group(1)) * 3600 + int(dur.group(2)) * 60 + float(dur.group(3))
    res = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", text)
    width, height = (int(res.group(1)), int(res.group(2))) if res else (0, 0)
    return {"duration": round(duration, 2), "width": width, "height": height}


def detect_speech(path: str, noise: str = "-30dB", min_d: float = 0.5) -> list[tuple[float, float]]:
    """Speech segments from ffmpeg silencedetect (complement of silences)."""
    info = probe_video(path)
    duration = info["duration"]
    out = _run([ffmpeg_exe(), "-i", path, "-af",
                f"silencedetect=noise={noise}:d={min_d}", "-f", "null", "-"], timeout=600)
    text = out.stderr or ""
    silences: list[tuple[float, float]] = []
    for m in re.finditer(r"silence_start: ([\d.]+)", text):
        silences.append((float(m.group(1)), -1.0))
    for i, m in enumerate(re.finditer(r"silence_end: ([\d.]+) \| silence_duration: ([\d.]+)", text)):
        if i < len(silences):
            start, _ = silences[i]
            silences[i] = (start, float(m.group(1)))
    silences = [(s, e) for s, e in silences if e > s]
    speech: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in silences:
        if s > cursor:
            speech.append((round(cursor, 2), round(s, 2)))
        cursor = max(cursor, e)
    if cursor < duration:
        speech.append((round(cursor, 2), round(duration, 2)))
    # Drop fragments shorter than 0.4s (noise).
    return [(s, e) for s, e in speech if e - s >= 0.4]


def mean_volume(path: str, start: float | None = None, end: float | None = None) -> float | None:
    """Mean volume in dB for a (sub)range, or None if undetectable."""
    cmd = [ffmpeg_exe()]
    if start is not None:
        cmd += ["-ss", str(start)]
    cmd += ["-i", path]
    if end is not None and start is not None:
        cmd += ["-t", str(max(end - start, 0.1))]
    cmd += ["-af", "volumedetect", "-f", "null", "-"]
    try:
        out = _run(cmd, timeout=300)
        m = re.search(r"mean_volume: ([-\d.]+) dB", out.stderr or "")
        return float(m.group(1)) if m else None
    except Exception:
        return None


def _energy_norm(mean_db: float | None) -> float:
    if mean_db is None:
        return 0.5
    return max(0.0, min(1.0, (mean_db + 50.0) / 40.0))


def _length_fit(seconds: float) -> float:
    if seconds < 15:
        return max(0.0, seconds / 15.0)
    if seconds <= 45:
        return 1.0
    if seconds <= 60:
        return 1.0 - (seconds - 45.0) / 15.0 * 0.6
    return 0.4


def _position_score(center_frac: float) -> float:
    return 1.0 - 0.5 * abs(center_frac - 0.3) / 0.7


def _speech_in_window(speech, start: float, end: float) -> float:
    total = 0.0
    for s, e in speech:
        total += max(0.0, min(e, end) - max(s, start))
    return total / max(end - start, 0.01)


def _keyword_hits(keywords, words, start: float, end: float) -> int:
    kw = {k for k, _ in keywords}
    hits = 0
    for w in words:
        if start <= w["start"] < end and w["word"].strip(".,!?\"'").lower() in kw:
            hits += 1
    return hits


def make_hook_title(keywords) -> str:
    if not keywords:
        return "Watch this moment"
    kw = keywords[0][0]
    idx = sum(ord(c) for c in kw) % len(HOOK_TEMPLATES)
    return HOOK_TEMPLATES[idx].format(kw=kw.title())


def _merge_blocks(speech, max_gap: float = 3.0) -> list[list[float]]:
    """Merge speech segments separated by at most max_gap seconds of silence."""
    blocks: list[list[float]] = []
    for s, e in speech:
        if blocks and s - blocks[-1][1] <= max_gap:
            blocks[-1][1] = max(blocks[-1][1], e)
        else:
            blocks.append([s, e])
    return blocks


def build_windows(duration: float, speech) -> list[tuple[float, float]]:
    """Candidate windows (15-60s) for a video.

    Block-anchored windows first: long blocks become sliding 40s windows,
    15-60s blocks are used as-is, short blocks are padded to 15s with
    surrounding context (density accounts for the silence inside).
    Sliding 20s/30s windows add coverage so a video with scattered speech
    still yields candidates.
    """
    blocks = _merge_blocks(speech)
    windows: list[tuple[float, float]] = []
    for s, e in blocks:
        length = e - s
        if length > 60:
            t = s
            while t + 15 <= e:
                windows.append((t, min(t + 40.0, e)))
                t += 20.0
        elif length >= 15:
            windows.append((s, e))
        else:
            need = 15.0 - length
            pad_before = min(need / 2.0, s)
            pad_after = need - pad_before
            ws, we = s - pad_before, e + pad_after
            if we > duration:
                we = duration
                ws = max(0.0, we - 15.0)
            if we - ws >= 15.0 or duration < 15.0:
                windows.append((round(ws, 2), round(we, 2)))
    if duration >= 20:
        for size in (30.0, 20.0):
            t = 0.0
            while t + size <= duration:
                windows.append((round(t, 2), round(t + size, 2)))
                t += 10.0
    return windows


def score_candidates(duration: float, speech, keywords, words,
                     stats_boost: float = 0.0, top_n: int = 5,
                     windows: list[tuple[float, float]] | None = None) -> list[dict]:
    """Rank candidate 15-60s windows. Transparent local heuristic:
    hook (first-5s speech + keywords), engagement (speech density + energy),
    value (keyword density + length fit), shareability (energy + position +
    length fit). Overall = 0.30*hook + 0.30*engagement + 0.25*value +
    0.15*shareability. stats_boost (0-5) is added when the user logged strong
    performance for an overlapping earlier clip."""
    if duration <= 0 or not speech:
        return []
    if windows is None:
        windows = build_windows(duration, speech)
    # Deduplicate heavily overlapping windows (keep the longer one first).
    windows = sorted(windows, key=lambda w: w[1] - w[0], reverse=True)
    kept: list[tuple[float, float]] = []
    for w in windows:
        overlap = False
        for k in kept:
            inter = max(0.0, min(w[1], k[1]) - max(w[0], k[0]))
            if inter > 0.5 * min(w[1] - w[0], k[1] - k[0]):
                overlap = True
                break
        if not overlap:
            kept.append(w)

    scored = []
    for start, end in kept:
        length = end - start
        density = _speech_in_window(speech, start, end)
        first5 = _speech_in_window(speech, start, min(start + 5.0, end))
        hits = _keyword_hits(keywords, words, start, end)
        hits_first5 = _keyword_hits(keywords, words, start, min(start + 5.0, end))
        energy = _energy_norm(mean_volume_cached.get((round(start, 1), round(end, 1))))
        length_fit = _length_fit(length)
        position = _position_score(((start + end) / 2.0) / duration)
        kw_density = min(1.0, hits / max(length / 10.0, 1.0) / 2.0)
        hook = round(100 * (0.6 * first5 + 0.4 * min(1.0, hits_first5 / 3.0)), 1)
        engagement = round(100 * (0.6 * density + 0.4 * energy), 1)
        value = round(100 * (0.6 * kw_density + 0.4 * length_fit), 1)
        shareability = round(100 * (0.5 * energy + 0.3 * position + 0.2 * length_fit), 1)
        overall = round(0.30 * hook + 0.30 * engagement + 0.25 * value + 0.15 * shareability, 1)
        if stats_boost:
            overall = min(100.0, round(overall + stats_boost, 1))
        signals = {
            "speech_density": round(density, 2),
            "length_sec": round(length, 1),
            "length_fit": round(length_fit, 2),
            "position": round(position, 2),
            "energy": round(energy, 2),
            "keyword_hits": hits,
            "stats_boost": round(stats_boost, 1),
        }
        reason = (
            f"Dense speech ({int(density * 100)}%) over {int(length)}s"
            + (f" with {hits} keyword hits" if hits else " with no keyword hits")
            + f", energy {int(energy * 100)}%, near the "
            + ("start" if (start / duration) < 0.33 else "middle" if (start / duration) < 0.66 else "end")
            + " of the video."
        )
        scored.append({
            "start": round(start, 2), "end": round(end, 2),
            "duration": round(length, 1), "score": overall,
            "breakdown": {"hook": hook, "engagement": engagement,
                          "value": value, "shareability": shareability,
                          "signals": signals},
            "reason": reason,
        })
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored[: max(3, min(7, top_n))]


# Cache for per-window mean volume within one analysis run (avoids re-running
# ffmpeg for the same window). Cleared by analyze_video().
mean_volume_cached: dict = {}


def analyze_video(path: str, transcript_words=None, keywords=None,
                  stats_boost: float = 0.0, top_n: int = 5) -> dict:
    """Full local analysis: probe + silencedetect + transparent scoring."""
    global mean_volume_cached
    mean_volume_cached = {}
    info = probe_video(path)
    duration = info["duration"]
    speech = detect_speech(path)
    # Pre-compute energy per candidate window (one volumedetect call each).
    windows = build_windows(duration, speech)
    for s, e in windows:
        mean_volume_cached[(round(s, 1), round(e, 1))] = mean_volume(path, s, e)
    words = transcript_words or []
    kw = keywords or []
    candidates = score_candidates(duration, speech, kw, words, stats_boost, top_n,
                                  windows=windows)
    return {
        "duration": duration, "width": info["width"], "height": info["height"],
        "speech_segments": [[s, e] for s, e in speech],
        "candidates": candidates,
    }


def compute_crop(source_w: int, source_h: int, face_box=None) -> tuple[int, int, int, int]:
    """9:16 crop box (crop_w, crop_h, x, y) in source pixels. If a face box
    (x, y, w, h) is given, the crop is centred on the face; otherwise centred.
    Pure math — unit-testable without any video."""
    if source_w <= 0 or source_h <= 0:
        return (0, 0, 0, 0)
    if source_w >= source_h:
        crop_h = source_h
        crop_w = int(round(source_h * 9 / 16))
        y = 0
        if face_box:
            cx = face_box[0] + face_box[2] / 2.0
            x = int(max(0, min(source_w - crop_w, cx - crop_w / 2.0)))
        else:
            x = int((source_w - crop_w) / 2)
    else:
        crop_w = source_w
        crop_h = int(round(source_w * 16 / 9))
        if crop_h > source_h:
            crop_h = source_h
            crop_w = int(round(source_h * 9 / 16))
            x = int((source_w - crop_w) / 2)
            y = 0
        else:
            x = 0
            if face_box:
                cy = face_box[1] + face_box[3] / 2.0
                y = int(max(0, min(source_h - crop_h, cy - crop_h / 2.0)))
            else:
                y = int((source_h - crop_h) / 2)
    return (crop_w, crop_h, x, y)


def detect_face(frame_path: str):
    """Largest face in a frame via OpenCV Haar cascade, or None.
    Returns (x, y, w, h) in frame pixels. Lazy cv2 import — returns None
    when OpenCV is unavailable."""
    try:
        import cv2
    except ImportError:
        return None
    try:
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        cascade = cv2.CascadeClassifier(cascade_path)
        img = cv2.imread(frame_path)
        if img is None or cascade.empty():
            return None
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                         minSize=(40, 40))
        if len(faces) == 0:
            return None
        faces = sorted(faces, key=lambda f: f[2] * f[3], reverse=True)
        x, y, w, h = (int(v) for v in faces[0])
        return (x, y, w, h)
    except Exception:
        return None


def extract_frame(video_path: str, at_sec: float, out_path: str) -> bool:
    out = _run([ffmpeg_exe(), "-y", "-ss", str(at_sec), "-i", video_path,
                "-vframes", "1", "-q:v", "3", out_path], timeout=120)
    return os.path.exists(out_path) and os.path.getsize(out_path) > 0


def _escape_sub_path(path: str) -> str:
    # ffmpeg subtitles filter: use forward slashes, escape ':' and '\''.
    p = path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    return p


def render_clip(src_path: str, start: float, end: float, out_path: str,
                source_w: int, source_h: int, ass_path: str | None = None,
                fonts_dir: str | None = None, font_name: str | None = None,
                broll_path: str | None = None) -> dict:
    """Cut + smart-crop to 720x1280 (9:16), H.264 + AAC. Returns render info
    including whether face centring was used."""
    duration = max(end - start, 0.1)
    mid = start + duration / 2.0
    face = None
    frame_tmp = out_path + ".frame.jpg"
    face_used = False
    try:
        if extract_frame(src_path, mid, frame_tmp):
            face = detect_face(frame_tmp)
            face_used = face is not None
    finally:
        if os.path.exists(frame_tmp):
            os.remove(frame_tmp)
    crop_w, crop_h, x, y = compute_crop(source_w, source_h, face)

    cmd = [ffmpeg_exe(), "-y", "-ss", str(start), "-i", src_path]
    inputs = 1
    if broll_path and os.path.exists(broll_path):
        cmd += ["-i", broll_path]
        inputs = 2
    cmd += ["-t", str(duration)]

    vf = f"crop={crop_w}:{crop_h}:{x}:{y},scale=720:1280,setsar=1,fps=30"
    if ass_path and fonts_dir and font_name:
        vf += f",subtitles='{_escape_sub_path(ass_path)}':fontsdir='{_escape_sub_path(fonts_dir)}'"
    if inputs == 2:
        vf = f"[0:v]{vf}[v];[1:v]scale=240:426[br];[v][br]overlay=W-w-16:H-h-16:enable='between(t,0,5)'[vout]"
        cmd += ["-filter_complex", vf, "-map", "[vout]", "-map", "0:a?"]
    else:
        cmd += ["-vf", vf, "-map", "0:v:0", "-map", "0:a?"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out_path]
    out = _run(cmd, timeout=900)
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError("ffmpeg render failed: " + (out.stderr or "")[-500:])
    return {"face_used": face_used, "crop": [crop_w, crop_h, x, y]}


def merge_clips(clip_paths: list[str], out_path: str) -> str:
    """Concatenate rendered clips (same codec/resolution) with the concat demuxer."""
    list_path = out_path + ".list.txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for p in clip_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")
    try:
        out = _run([ffmpeg_exe(), "-y", "-f", "concat", "-safe", "0",
                    "-i", list_path, "-c", "copy", out_path], timeout=600)
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise RuntimeError("ffmpeg merge failed: " + (out.stderr or "")[-500:])
        return out_path
    finally:
        if os.path.exists(list_path):
            os.remove(list_path)


def download_url(url: str, out_dir: str) -> dict:
    """Download a public video URL with yt-dlp (public videos only).
    Uses the bundled ffmpeg for any post-processing."""
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError("yt-dlp is not installed. Run: pip install -r requirements.txt") from exc
    os.makedirs(out_dir, exist_ok=True)
    opts = {
        "format": "best[height<=720]/best",
        "outtmpl": os.path.join(out_dir, "%(title).80s-%(id)s.%(ext)s"),
        "ffmpeg_location": ffmpeg_exe(),
        "quiet": True,
        "noplaylist": True,
        "max_filesize": 500 * 1024 * 1024,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
    if not path or not os.path.exists(path):
        raise RuntimeError("Download finished but no file was produced.")
    return {"path": path, "title": info.get("title", "Untitled"),
            "duration": info.get("duration") or 0,
            "ext": info.get("ext", "mp4")}
