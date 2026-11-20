"""Clip router: generation (real ffmpeg render), editor (trim/split/merge),
platform export presets, stats logging, B-roll uploads."""
import json
import os
import shutil
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import tasks as task_store
from ..auth import get_current_principal
from ..captions import build_ass, find_caption_font
from ..database import SessionLocal, get_db
from ..models import Clip, ClipStat, Transcript, User, Video
from ..schemas import ClipOut, ExportIn, GenerateIn, MergeIn, SplitIn, StatIn, TrimIn
from ..video import PLAN_LIMITS, merge_clips, probe_video, render_clip

router = APIRouter(prefix="/api/clips", tags=["clips"])

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLIP_DIR = os.path.join(BASE_DIR, "clips")
BROLL_DIR = os.path.join(BASE_DIR, "broll")

PRESETS = {
    "tiktok": {"caption_template": "bold-yellow", "hook_title": True},
    "reels": {"caption_template": "white-black", "hook_title": True},
    "shorts": {"caption_template": "bold-yellow", "hook_title": False},
}


def _clip_out(c: Clip) -> ClipOut:
    try:
        breakdown = json.loads(c.breakdown_json or "{}")
    except Exception:
        breakdown = {}
    return ClipOut(
        id=c.id, video_id=c.video_id, title=c.title, hook_title=c.hook_title,
        start_sec=c.start_sec, end_sec=c.end_sec, duration_sec=c.duration_sec,
        score=c.score, breakdown=breakdown, reason=c.reason, status=c.status,
        caption_status=c.caption_status, caption_template=c.caption_template,
        preset=c.preset, parent_clip_id=c.parent_clip_id, has_broll=c.has_broll,
        created_at=c.created_at.isoformat() if c.created_at else "",
    )


def _check_clip_limit(db: Session, user: User):
    limit = PLAN_LIMITS.get(user.plan, PLAN_LIMITS["free"])["clips"]
    count = db.query(Clip).count()
    if count >= limit:
        raise HTTPException(
            status_code=403,
            detail=f"Clip limit reached for the {user.plan} plan ({limit} clips). "
                   "Upgrade to Pro (mock) to create more.")


def _srt_text(segments, start: float) -> str:
    def fmt(sec: float) -> str:
        sec = max(sec, 0.0)
        h, m = int(sec // 3600), int((sec % 3600) // 60)
        s = sec % 60
        return f"{h:02d}:{m:02d}:{int(s):02d},{int((s - int(s)) * 1000):03d}"
    out = []
    for i, seg in enumerate(segments, 1):
        out.append(str(i))
        out.append(f"{fmt(seg['start'] - start)} --> {fmt(seg['end'] - start)}")
        out.append(seg["text"])
        out.append("")
    return "\n".join(out)


def _prepare_captions(db: Session, clip: Clip, template: str, include_hook_title: bool):
    """Returns (ass_path or None, srt_sidecar_path or None, caption_status).
    Words come from the video transcript, clipped to the clip window and
    shifted so the clip starts at 0s."""
    tr = db.query(Transcript).filter(Transcript.video_id == clip.video_id).order_by(Transcript.id.desc()).first()
    if not tr:
        return None, None, "none"
    words = json.loads(tr.words_json or "[]")
    segments = json.loads(tr.segments_json or "[]")
    win_words = [{"word": w["word"], "start": round(w["start"] - clip.start_sec, 2),
                  "end": round(w["end"] - clip.start_sec, 2)}
                 for w in words if clip.start_sec <= w["start"] < clip.end_sec]
    win_segments = [{"text": s["text"],
                     "start": max(0.0, s["start"] - clip.start_sec),
                     "end": max(0.0, s["end"] - clip.start_sec)}
                    for s in segments
                    if s["end"] > clip.start_sec and s["start"] < clip.end_sec]
    if not win_words:
        return None, None, "none"
    font_path, font_name, fonts_dir = find_caption_font()
    ass_path = None
    if font_path:
        ass_path = os.path.join(CLIP_DIR, f"clip_{clip.id}_{uuid.uuid4().hex[:8]}.ass")
        hook = clip.hook_title if include_hook_title else ""
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(build_ass(win_words, template, hook, font_name, clip.duration_sec))
        return ass_path, None, "burned_in"
    # No font: honest sidecar — clip + SRT, never a fake caption claim.
    srt_path = os.path.join(CLIP_DIR, f"clip_{clip.id}.srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(_srt_text(win_segments, clip.start_sec))
    return None, srt_path, "sidecar_only"


def _render_clip_task(task_id: str, clip_id: int, template: str,
                      include_hook_title: bool, broll_filename: str):
    db = SessionLocal()
    try:
        clip = db.query(Clip).filter(Clip.id == clip_id).first()
        if not clip:
            task_store.fail_task(task_id, "Clip not found.")
            return
        video = db.query(Video).filter(Video.id == clip.video_id).first()
        if not video or not video.stored_path or not os.path.exists(video.stored_path):
            clip.status = "failed"
            clip.error = "Source video file not found."
            db.commit()
            task_store.fail_task(task_id, "Source video file not found.")
            return
        clip.status = "processing"
        db.commit()
        task_store.update_task(task_id, "preparing captions", 15)
        os.makedirs(CLIP_DIR, exist_ok=True)
        ass_path, srt_path, caption_status = _prepare_captions(db, clip, template, include_hook_title)
        broll_path = None
        if broll_filename:
            candidate = os.path.join(BROLL_DIR, os.path.basename(broll_filename))
            if os.path.exists(candidate):
                broll_path = candidate
        out_path = os.path.join(CLIP_DIR, f"clip_{clip.id}_{uuid.uuid4().hex[:8]}.mp4")
        task_store.update_task(task_id, "rendering with ffmpeg", 45)
        info = render_clip(video.stored_path, clip.start_sec, clip.end_sec, out_path,
                           video.width or 1280, video.height or 720,
                           ass_path=ass_path,
                           fonts_dir=os.path.dirname(find_caption_font()[0]) if find_caption_font()[0] else None,
                           font_name=find_caption_font()[1],
                           broll_path=broll_path)
        task_store.update_task(task_id, "verifying output", 90)
        probe = probe_video(out_path)
        clip.status = "ready"
        clip.file_path = out_path
        clip.caption_status = caption_status
        clip.caption_template = template
        clip.has_broll = broll_path is not None
        clip.error = ""
        db.commit()
        if ass_path and os.path.exists(ass_path):
            os.remove(ass_path)
        task_store.finish_task(task_id, {
            "clip_id": clip.id, "file": os.path.basename(out_path),
            "duration": probe["duration"], "width": probe["width"],
            "height": probe["height"], "caption_status": caption_status,
            "face_centred": info.get("face_used", False)})
    except Exception as exc:
        db.rollback()
        clip = db.query(Clip).filter(Clip.id == clip_id).first()
        if clip:
            clip.status = "failed"
            clip.error = str(exc)[:500]
            db.commit()
        task_store.fail_task(task_id, str(exc))
    finally:
        db.close()


@router.get("", response_model=list[ClipOut])
def list_clips(video_id: int | None = None, user: User = Depends(get_current_principal),
               db: Session = Depends(get_db)):
    q = db.query(Clip)
    if video_id is not None:
        q = q.filter(Clip.video_id == video_id)
    return [_clip_out(c) for c in q.order_by(Clip.score.desc()).all()]


@router.get("/{clip_id}", response_model=ClipOut)
def get_clip(clip_id: int, user: User = Depends(get_current_principal),
             db: Session = Depends(get_db)):
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    return _clip_out(clip)


@router.post("/{clip_id}/generate")
def generate_clip(clip_id: int, payload: GenerateIn, background: BackgroundTasks,
                  user: User = Depends(get_current_principal),
                  db: Session = Depends(get_db)):
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    if clip.status == "processing":
        raise HTTPException(status_code=409, detail="Clip is already rendering.")
    task_id = task_store.create_task("render")
    background.add_task(_render_clip_task, task_id, clip_id, payload.caption_template,
                        payload.include_hook_title, payload.broll_filename)
    return {"task_id": task_id, "status": "running"}


@router.post("/{clip_id}/regenerate")
def regenerate_clip(clip_id: int, payload: GenerateIn, background: BackgroundTasks,
                    user: User = Depends(get_current_principal),
                    db: Session = Depends(get_db)):
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    if clip.file_path and os.path.exists(clip.file_path):
        os.remove(clip.file_path)
    clip.file_path = ""
    clip.status = "queued"
    db.commit()
    task_id = task_store.create_task("render")
    background.add_task(_render_clip_task, task_id, clip_id, payload.caption_template,
                        payload.include_hook_title, payload.broll_filename)
    return {"task_id": task_id, "status": "running"}


@router.post("/{clip_id}/trim", response_model=ClipOut)
def trim_clip(clip_id: int, payload: TrimIn, background: BackgroundTasks,
              user: User = Depends(get_current_principal),
              db: Session = Depends(get_db)):
    _check_clip_limit(db, user)
    src = db.query(Clip).filter(Clip.id == clip_id).first()
    if not src:
        raise HTTPException(status_code=404, detail="Clip not found")
    if payload.end_sec <= payload.start_sec:
        raise HTTPException(status_code=400, detail="end_sec must be after start_sec")
    if payload.start_sec < src.start_sec or payload.end_sec > src.end_sec:
        raise HTTPException(status_code=400, detail="Trim range must stay inside the source clip")
    new = Clip(video_id=src.video_id, title=f"{src.title} (trimmed)",
               hook_title=src.hook_title, start_sec=payload.start_sec,
               end_sec=payload.end_sec, duration_sec=round(payload.end_sec - payload.start_sec, 1),
               score=src.score, breakdown_json=src.breakdown_json, reason="Trimmed by user.",
               status="queued", parent_clip_id=src.id)
    db.add(new)
    db.commit()
    db.refresh(new)
    task_id = task_store.create_task("render")
    background.add_task(_render_clip_task, task_id, new.id, src.caption_template, True, "")
    return _clip_out(new)


@router.post("/{clip_id}/split")
def split_clip(clip_id: int, payload: SplitIn, background: BackgroundTasks,
               user: User = Depends(get_current_principal),
               db: Session = Depends(get_db)):
    _check_clip_limit(db, user)
    src = db.query(Clip).filter(Clip.id == clip_id).first()
    if not src:
        raise HTTPException(status_code=404, detail="Clip not found")
    if not (src.start_sec < payload.at_sec < src.end_sec):
        raise HTTPException(status_code=400, detail="Split point must be inside the clip")
    made = []
    for i, (s, e) in enumerate([(src.start_sec, payload.at_sec), (payload.at_sec, src.end_sec)]):
        new = Clip(video_id=src.video_id, title=f"{src.title} (part {i + 1})",
                   hook_title=src.hook_title if i == 0 else "", start_sec=s, end_sec=e,
                   duration_sec=round(e - s, 1), score=src.score,
                   breakdown_json=src.breakdown_json, reason="Split by user.",
                   status="queued", parent_clip_id=src.id)
        db.add(new)
        db.commit()
        db.refresh(new)
        made.append(new)
    for new in made:
        task_id = task_store.create_task("render")
        background.add_task(_render_clip_task, task_id, new.id, src.caption_template, False, "")
    return {"clip_ids": [c.id for c in made], "status": "rendering"}


@router.post("/merge", response_model=ClipOut)
def merge_clips_endpoint(payload: MergeIn, background: BackgroundTasks,
                         user: User = Depends(get_current_principal),
                         db: Session = Depends(get_db)):
    _check_clip_limit(db, user)
    if len(payload.clip_ids) < 2:
        raise HTTPException(status_code=400, detail="Merging needs at least 2 clips")
    sources = db.query(Clip).filter(Clip.id.in_(payload.clip_ids)).all()
    ready = [c for c in sources if c.status == "ready" and c.file_path and os.path.exists(c.file_path)]
    if len(ready) < 2:
        raise HTTPException(status_code=400,
                            detail="Merging needs at least 2 rendered clips (status ready). Generate them first.")
    ready.sort(key=lambda c: payload.clip_ids.index(c.id))
    total = round(sum(c.duration_sec for c in ready), 1)
    video_id = ready[0].video_id
    merged = Clip(video_id=video_id, title=payload.title, hook_title=ready[0].hook_title,
                  start_sec=0.0, end_sec=total, duration_sec=total,
                  score=round(sum(c.score for c in ready) / len(ready), 1),
                  breakdown_json=ready[0].breakdown_json,
                  reason="Merged from %d clips by user." % len(ready),
                  status="processing", parent_clip_id=None)
    db.add(merged)
    db.commit()
    db.refresh(merged)
    task_id = task_store.create_task("merge")
    background.add_task(_do_merge, task_id, merged.id, [c.file_path for c in ready])
    return _clip_out(merged)


def _do_merge(task_id: str, merged_id: int, paths: list[str]):
    db = SessionLocal()
    try:
        os.makedirs(CLIP_DIR, exist_ok=True)
        out_path = os.path.join(CLIP_DIR, f"clip_{merged_id}_{uuid.uuid4().hex[:8]}.mp4")
        task_store.update_task(task_id, "concatenating clips", 50)
        merge_clips(paths, out_path)
        clip = db.query(Clip).filter(Clip.id == merged_id).first()
        if clip:
            clip.status = "ready"
            clip.file_path = out_path
            db.commit()
        task_store.finish_task(task_id, {"clip_id": merged_id, "file": os.path.basename(out_path)})
    except Exception as exc:
        db.rollback()
        clip = db.query(Clip).filter(Clip.id == merged_id).first()
        if clip:
            clip.status = "failed"
            clip.error = str(exc)[:500]
            db.commit()
        task_store.fail_task(task_id, str(exc))
    finally:
        db.close()


@router.post("/{clip_id}/export")
def export_clip(clip_id: int, payload: ExportIn, background: BackgroundTasks,
                user: User = Depends(get_current_principal),
                db: Session = Depends(get_db)):
    _check_clip_limit(db, user)
    if payload.preset not in PRESETS:
        raise HTTPException(status_code=400, detail="Preset must be tiktok, reels or shorts")
    src = db.query(Clip).filter(Clip.id == clip_id).first()
    if not src:
        raise HTTPException(status_code=404, detail="Clip not found")
    preset = PRESETS[payload.preset]
    new = Clip(video_id=src.video_id, title=f"{src.title} ({payload.preset})",
               hook_title=src.hook_title if preset["hook_title"] else "",
               start_sec=src.start_sec, end_sec=src.end_sec,
               duration_sec=src.duration_sec, score=src.score,
               breakdown_json=src.breakdown_json,
               reason=f"Exported for {payload.preset} ({preset['caption_template']} captions).",
               status="queued", preset=payload.preset,
               caption_template=preset["caption_template"], parent_clip_id=src.id)
    db.add(new)
    db.commit()
    db.refresh(new)
    task_id = task_store.create_task("render")
    background.add_task(_render_clip_task, task_id, new.id, preset["caption_template"],
                        preset["hook_title"], "")
    return {"clip_id": new.id, "task_id": task_id, "status": "rendering", "preset": payload.preset}


@router.get("/{clip_id}/file")
def clip_file(clip_id: int, user: User = Depends(get_current_principal),
              db: Session = Depends(get_db)):
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip or not clip.file_path or not os.path.exists(clip.file_path):
        raise HTTPException(status_code=404, detail="No rendered file for this clip yet. Generate it first.")
    return FileResponse(clip.file_path, media_type="video/mp4",
                        filename=f"clippilot_clip_{clip.id}.mp4")


@router.get("/{clip_id}/srt")
def clip_srt(clip_id: int, user: User = Depends(get_current_principal),
             db: Session = Depends(get_db)):
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    srt_path = os.path.join(CLIP_DIR, f"clip_{clip.id}.srt")
    if not clip or not os.path.exists(srt_path):
        raise HTTPException(status_code=404, detail="No SRT sidecar for this clip.")
    return FileResponse(srt_path, media_type="text/plain", filename=f"clip_{clip.id}.srt")


@router.delete("/{clip_id}")
def delete_clip(clip_id: int, user: User = Depends(get_current_principal),
                db: Session = Depends(get_db)):
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    if clip.file_path and os.path.exists(clip.file_path):
        os.remove(clip.file_path)
    db.delete(clip)
    db.commit()
    return {"deleted": clip_id}


@router.post("/{clip_id}/stats", status_code=201)
def log_stat(clip_id: int, payload: StatIn, user: User = Depends(get_current_principal),
             db: Session = Depends(get_db)):
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    stat = ClipStat(clip_id=clip_id, views=payload.views, likes=payload.likes,
                    comments=payload.comments, shares=payload.shares, note=payload.note)
    db.add(stat)
    db.commit()
    db.refresh(stat)
    return {"id": stat.id, "clip_id": clip_id, "views": stat.views, "likes": stat.likes,
            "comments": stat.comments, "shares": stat.shares,
            "logged_at": stat.logged_at.isoformat() if stat.logged_at else ""}


@router.get("/{clip_id}/stats")
def list_stats(clip_id: int, user: User = Depends(get_current_principal),
               db: Session = Depends(get_db)):
    rows = db.query(ClipStat).filter(ClipStat.clip_id == clip_id).order_by(ClipStat.id.desc()).all()
    totals = {"views": sum(r.views for r in rows), "likes": sum(r.likes for r in rows),
              "comments": sum(r.comments for r in rows), "shares": sum(r.shares for r in rows)}
    return {"clip_id": clip_id, "totals": totals,
            "entries": [{"id": r.id, "views": r.views, "likes": r.likes,
                         "comments": r.comments, "shares": r.shares, "note": r.note,
                         "logged_at": r.logged_at.isoformat() if r.logged_at else ""}
                        for r in rows]}


@router.post("/broll", status_code=201)
async def upload_broll(file: UploadFile = File(...),
                       user: User = Depends(get_current_principal)):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in {".mp4", ".webm", ".mov"}:
        raise HTTPException(status_code=400, detail="B-roll must be an MP4/WebM/MOV video file.")
    os.makedirs(BROLL_DIR, exist_ok=True)
    dest = os.path.join(BROLL_DIR, os.path.basename(file.filename))
    with open(dest, "wb") as out:
        shutil.copyfileobj(file.file, out)
    return {"filename": os.path.basename(file.filename),
            "note": "B-roll is overlaid for the first 5 seconds of a clip (bottom-right corner) when selected at generation time."}


@router.get("/broll")
def list_broll(user: User = Depends(get_current_principal)):
    if not os.path.exists(BROLL_DIR):
        return {"files": []}
    return {"files": sorted(os.listdir(BROLL_DIR))}
