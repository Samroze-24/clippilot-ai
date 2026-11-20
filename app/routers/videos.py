"""Video router: upload, URL ingestion (yt-dlp), analysis, transcripts,
file serving and background-task progress."""
import json
import os
import shutil
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import tasks as task_store
from ..auth import get_current_principal
from ..captions import parse_transcript_file, transcribe_whisper
from ..database import SessionLocal, get_db
from ..llm import llm_config, llm_configured, select_clips_llm
from ..models import Clip, ClipStat, Transcript, User, Video
from ..schemas import TaskOut, VideoOut
from ..video import (PLAN_LIMITS, analyze_video, download_url, extract_keywords,
                     mean_volume, probe_video, score_candidates)
from .. import video as video_mod

router = APIRouter(prefix="/api/videos", tags=["videos"])

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
CLIP_DIR = os.path.join(BASE_DIR, "clips")
BROLL_DIR = os.path.join(BASE_DIR, "broll")
MAX_UPLOAD_BYTES = 500 * 1024 * 1024
ALLOWED_EXTS = {".mp4", ".webm", ".mov", ".mkv"}


def _video_out(v: Video) -> VideoOut:
    return VideoOut(
        id=v.id, title=v.title, source_type=v.source_type, source_url=v.source_url,
        filename=v.filename, duration_sec=v.duration_sec, size_bytes=v.size_bytes,
        width=v.width, height=v.height, status=v.status,
        transcript_status=v.transcript_status,
        created_at=v.created_at.isoformat() if v.created_at else "",
    )


def _check_video_limit(db: Session, user: User):
    limit = PLAN_LIMITS.get(user.plan, PLAN_LIMITS["free"])["videos"]
    count = db.query(Video).filter(Video.source_type != "sample").count()
    if count >= limit:
        raise HTTPException(
            status_code=403,
            detail=f"Video limit reached for the {user.plan} plan ({limit} videos). "
                   "Upgrade to Pro (mock) to add more.")


def _do_analyze(task_id: str, video_id: int):
    db = SessionLocal()
    try:
        task_store.update_task(task_id, "probing video", 10)
        video = db.query(Video).filter(Video.id == video_id).first()
        if not video or not video.stored_path or not os.path.exists(video.stored_path):
            task_store.fail_task(task_id, "Video file not found.")
            return
        video.status = "analyzing"
        db.commit()
        # Transcript keywords for the boost, if a transcript exists.
        words, keywords = [], []
        tr = db.query(Transcript).filter(Transcript.video_id == video_id).order_by(Transcript.id.desc()).first()
        if tr:
            words = json.loads(tr.words_json or "[]")
            full_text = " ".join(w["word"] for w in words)
            keywords = extract_keywords(full_text)
        # Performance boost from manually logged stats (max +5).
        stats_boost = 0.0
        for clip in db.query(Clip).filter(Clip.video_id == video_id).all():
            for st in db.query(ClipStat).filter(ClipStat.clip_id == clip.id).all():
                stats_boost = max(stats_boost, min(5.0, st.views / 500.0))
        task_store.update_task(task_id, "detecting speech segments", 35)
        result = analyze_video(video.stored_path, transcript_words=words,
                               keywords=keywords, stats_boost=stats_boost, top_n=5)
        # Real LLM clip selection (Groq) when configured and a transcript
        # exists — otherwise the local scorer above stands. The LLM only
        # *picks* windows; the same transparent scorer still computes the
        # score/breakdown, and the LLM's title/reason are kept verbatim.
        selector = "local"
        selector_detail = "Local transparent scorer"
        if tr:
            segments = json.loads(tr.segments_json or "[]")
            if segments and llm_configured():
                task_store.update_task(task_id, "asking LLM for clip picks (Groq)", 60)
                llm_picks, llm_detail = select_clips_llm(segments, result["duration"])
                if llm_picks:
                    llm_windows = [(c["start"], c["end"]) for c in llm_picks]
                    for s, e in llm_windows:
                        key = (round(s, 1), round(e, 1))
                        if key not in video_mod.mean_volume_cached:
                            video_mod.mean_volume_cached[key] = mean_volume(
                                video.stored_path, s, e)
                    speech = [tuple(s) for s in result["speech_segments"]]
                    scored = score_candidates(
                        result["duration"], speech, keywords, words,
                        stats_boost, top_n=len(llm_windows), windows=llm_windows)
                    by_window = {(round(c["start"], 2), round(c["end"], 2)): c
                                 for c in llm_picks}
                    llm_candidates = []
                    for cand in scored:
                        pick = by_window.get((round(cand["start"], 2),
                                              round(cand["end"], 2)))
                        if pick:
                            cand = dict(cand)
                            cand["llm_title"] = pick["title"]
                            cand["llm_reason"] = pick["reason"]
                            llm_candidates.append(cand)
                    if llm_candidates:
                        result["candidates"] = llm_candidates
                        selector = "llm"
                        selector_detail = (
                            f"LLM (Groq {llm_config()['model']}) — "
                            f"{len(llm_candidates)} picks")
                    else:
                        selector_detail = ("LLM picks could not be scored — "
                                           "fell back to local scorer")
                else:
                    selector_detail = (f"LLM failed ({llm_detail}) — fell back "
                                       "to local scorer")
            elif segments:
                selector_detail = ("Local scorer — LLM not configured "
                                   "(set LLM_PROVIDER=groq and LLM_API_KEY in .env)")
            else:
                selector_detail = ("Local scorer — no transcript yet "
                                   "(transcribe the video to enable LLM picks)")
        task_store.update_task(task_id, "scoring candidates", 70)
        # Replace previous candidates for this video.
        db.query(Clip).filter(Clip.video_id == video_id, Clip.status == "queued").delete()
        clip_ids = []
        for i, cand in enumerate(result["candidates"]):
            from ..video import make_hook_title
            clip = Clip(
                video_id=video_id,
                title=f"Clip {i + 1} — {cand['start']:.0f}s–{cand['end']:.0f}s",
                hook_title=cand.get("llm_title") or make_hook_title(keywords),
                start_sec=cand["start"], end_sec=cand["end"],
                duration_sec=cand["duration"], score=cand["score"],
                breakdown_json=json.dumps(cand["breakdown"]),
                reason=cand.get("llm_reason") or cand["reason"], status="queued",
                caption_status="none", caption_template="bold-yellow",
            )
            db.add(clip)
            db.commit()
            db.refresh(clip)
            clip_ids.append(clip.id)
        video.status = "analyzed"
        video.duration_sec = result["duration"]
        video.width = result["width"]
        video.height = result["height"]
        db.commit()
        task_store.finish_task(task_id, {"video_id": video_id, "clip_ids": clip_ids,
                                         "candidates": len(clip_ids),
                                         "duration": result["duration"],
                                         "selector": selector,
                                         "selector_detail": selector_detail})
    except Exception as exc:
        task_store.fail_task(task_id, str(exc))
    finally:
        db.close()


def _do_download(task_id: str, url: str, title: str, user_id: int,
                 use_cookies: bool = False, cookies_browser: str = "chrome",
                 cookies_path: str | None = None):
    db = SessionLocal()
    try:
        task_store.update_task(task_id, "downloading (yt-dlp)", 20)
        info = download_url(url, UPLOAD_DIR, use_cookies=use_cookies,
                            cookies_browser=cookies_browser,
                            cookies_file=cookies_path)
        task_store.update_task(task_id, "probing video", 70)
        probe = probe_video(info["path"])
        video = Video(
            title=title or info["title"], source_type="url", source_url=url,
            filename=os.path.basename(info["path"]), stored_path=info["path"],
            duration_sec=probe["duration"], size_bytes=os.path.getsize(info["path"]),
            width=probe["width"], height=probe["height"], status="uploaded",
        )
        db.add(video)
        db.commit()
        db.refresh(video)
        task_store.finish_task(task_id, {"video_id": video.id, "duration": probe["duration"]})
    except Exception as exc:
        task_store.fail_task(task_id, str(exc))
    finally:
        # Cookies are sensitive credentials: delete an uploaded cookies.txt
        # whatever happened (success or failure). It is never stored in the
        # database and never survives the ingest.
        if cookies_path and os.path.exists(cookies_path):
            try:
                os.remove(cookies_path)
            except OSError:
                pass
        db.close()


def _do_transcribe(task_id: str, video_id: int):
    db = SessionLocal()
    try:
        video = db.query(Video).filter(Video.id == video_id).first()
        if not video or not os.path.exists(video.stored_path or ""):
            task_store.fail_task(task_id, "Video file not found.")
            return
        video.transcript_status = "transcribing"
        db.commit()
        task_store.update_task(task_id, "transcribing (faster-whisper, tiny model)", 30)
        result = transcribe_whisper(video.stored_path)
        task_store.update_task(task_id, "saving transcript", 85)
        db.query(Transcript).filter(Transcript.video_id == video_id).delete()
        db.add(Transcript(video_id=video_id, source="whisper",
                          language=result["language"],
                          segments_json=json.dumps(result["segments"]),
                          words_json=json.dumps(result["words"])))
        video.transcript_status = "transcribed"
        db.commit()
        task_store.finish_task(task_id, {"video_id": video_id,
                                         "words": len(result["words"]),
                                         "language": result["language"]})
    except Exception as exc:
        db.rollback()
        video = db.query(Video).filter(Video.id == video_id).first()
        if video:
            video.transcript_status = "failed"
            db.commit()
        task_store.fail_task(task_id, str(exc))
    finally:
        db.close()


@router.get("", response_model=list[VideoOut])
def list_videos(user: User = Depends(get_current_principal), db: Session = Depends(get_db)):
    return [_video_out(v) for v in db.query(Video).order_by(Video.id.desc()).all()]


@router.post("/upload", response_model=VideoOut, status_code=201)
async def upload_video(title: str = "", file: UploadFile = File(...),
                       user: User = Depends(get_current_principal),
                       db: Session = Depends(get_db)):
    _check_video_limit(db, user)
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(status_code=400,
                            detail=f"Unsupported file type '{ext}'. Use MP4 or WebM (MOV/MKV also accepted).")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex[:12]}_{os.path.basename(file.filename)}"
    dest = os.path.join(UPLOAD_DIR, safe_name)
    size = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413,
                                        detail="Upload limit is 500 MB per video.")
                out.write(chunk)
    except HTTPException:
        if os.path.exists(dest):
            os.remove(dest)
        raise
    probe = probe_video(dest)
    video = Video(title=title or os.path.splitext(file.filename or "Untitled")[0],
                  source_type="upload", filename=file.filename or safe_name,
                  stored_path=dest, duration_sec=probe["duration"], size_bytes=size,
                  width=probe["width"], height=probe["height"], status="uploaded")
    db.add(video)
    db.commit()
    db.refresh(video)
    return _video_out(video)


@router.post("/from-url")
async def ingest_url(background: BackgroundTasks,
                     url: str = Form(...), title: str = Form(""),
                     use_cookies: bool = Form(False),
                     cookies_browser: str = Form("chrome"),
                     cookies_file: UploadFile | None = File(None),
                     user: User = Depends(get_current_principal),
                     db: Session = Depends(get_db)):
    """Ingest a public URL (multipart form, so a cookies.txt can ride along).

    use_cookies + cookies_browser ask yt-dlp to read the logged-in browser
    session (--cookies-from-browser); cookies_file is an uploaded Netscape
    cookies.txt, saved only under uploads/ and deleted right after the
    ingest, success or failure. Both are opt-in — default is a plain fetch.
    """
    _check_video_limit(db, user)
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")
    if cookies_browser not in {"chrome", "edge", "firefox"}:
        raise HTTPException(status_code=400, detail="Browser must be chrome, edge or firefox.")
    cookies_path = None
    if cookies_file is not None:
        ext = os.path.splitext(cookies_file.filename or "")[1].lower()
        if ext != ".txt":
            raise HTTPException(status_code=400,
                                detail="Cookies file must be a .txt file in Netscape format.")
        content = await cookies_file.read()
        if len(content) > 1024 * 1024:
            raise HTTPException(status_code=400,
                                detail="Cookies file is too large (max 1 MB).")
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        cookies_path = os.path.join(UPLOAD_DIR, f"cookies_{uuid.uuid4().hex[:12]}.txt")
        with open(cookies_path, "wb") as f:
            f.write(content)
    task_id = task_store.create_task("url-download")
    background.add_task(_do_download, task_id, url, title, user.id,
                        use_cookies, cookies_browser, cookies_path)
    return {"task_id": task_id, "status": "running"}


@router.get("/{video_id}", response_model=VideoOut)
def get_video(video_id: int, user: User = Depends(get_current_principal),
              db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    return _video_out(video)


@router.delete("/{video_id}")
def delete_video(video_id: int, user: User = Depends(get_current_principal),
                 db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.stored_path and os.path.exists(video.stored_path):
        os.remove(video.stored_path)
    for clip in db.query(Clip).filter(Clip.video_id == video_id).all():
        if clip.file_path and os.path.exists(clip.file_path):
            os.remove(clip.file_path)
        db.delete(clip)
    db.query(Transcript).filter(Transcript.video_id == video_id).delete()
    db.delete(video)
    db.commit()
    return {"deleted": video_id}


@router.get("/{video_id}/file")
def video_file(video_id: int, user: User = Depends(get_current_principal),
               db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video or not video.stored_path or not os.path.exists(video.stored_path):
        raise HTTPException(status_code=404, detail="No video file for this entry (SAMPLE entries have no file).")
    return FileResponse(video.stored_path, media_type="video/mp4",
                        filename=video.filename or "video.mp4")


@router.post("/{video_id}/analyze")
def analyze(video_id: int, background: BackgroundTasks,
            user: User = Depends(get_current_principal),
            db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if not video.stored_path or not os.path.exists(video.stored_path):
        raise HTTPException(status_code=400, detail="SAMPLE entries have no file to analyze. Upload a video first.")
    task_id = task_store.create_task("analyze")
    background.add_task(_do_analyze, task_id, video_id)
    return {"task_id": task_id, "status": "running"}


@router.post("/{video_id}/transcript", status_code=201)
async def upload_transcript(video_id: int, file: UploadFile = File(...),
                            user: User = Depends(get_current_principal),
                            db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in {".srt", ".txt"}:
        raise HTTPException(status_code=400, detail="Transcript must be .srt or .txt")
    raw = await file.read()
    text = raw.decode("utf-8", errors="ignore")
    if ext == ".srt":
        from ..captions import parse_srt
        segments = parse_srt(text)
        words = None
    else:
        from ..captions import parse_txt
        segments = parse_txt(text, video.duration_sec or 0)
        words = None
    if not segments:
        raise HTTPException(status_code=400, detail="Could not parse any transcript segments from the file.")
    from ..captions import words_from_segments
    words = words_from_segments(segments)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    tpath = os.path.join(UPLOAD_DIR, f"transcript_{video_id}_{uuid.uuid4().hex[:8]}{ext}")
    with open(tpath, "w", encoding="utf-8") as f:
        f.write(text)
    db.query(Transcript).filter(Transcript.video_id == video_id).delete()
    db.add(Transcript(video_id=video_id, source="upload", language="en",
                      segments_json=json.dumps(segments), words_json=json.dumps(words)))
    video.transcript_status = "uploaded"
    video.transcript_path = tpath
    db.commit()
    return {"video_id": video_id, "segments": len(segments), "words": len(words),
            "source": "upload",
            "note": "Word timing is estimated (evenly distributed per segment) for SRT/TXT uploads."}


@router.post("/{video_id}/transcribe")
def transcribe(video_id: int, background: BackgroundTasks,
               user: User = Depends(get_current_principal),
               db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if not video.stored_path or not os.path.exists(video.stored_path):
        raise HTTPException(status_code=400, detail="SAMPLE entries have no file to transcribe.")
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        raise HTTPException(status_code=503, detail=(
            "Local transcription engine (faster-whisper) is not installed. "
            "Run: pip install -r requirements.txt — or upload an SRT/TXT transcript instead."))
    task_id = task_store.create_task("transcribe")
    background.add_task(_do_transcribe, task_id, video_id)
    return {"task_id": task_id, "status": "running"}


@router.get("/{video_id}/transcript")
def get_transcript(video_id: int, user: User = Depends(get_current_principal),
                   db: Session = Depends(get_db)):
    tr = db.query(Transcript).filter(Transcript.video_id == video_id).order_by(Transcript.id.desc()).first()
    if not tr:
        raise HTTPException(status_code=404, detail="No transcript for this video")
    return {"video_id": video_id, "source": tr.source, "language": tr.language,
            "segments": json.loads(tr.segments_json or "[]"),
            "words": json.loads(tr.words_json or "[]")}


@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: str, user: User = Depends(get_current_principal)):
    task = task_store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task
