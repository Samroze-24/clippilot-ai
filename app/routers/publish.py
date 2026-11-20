"""Direct publishing endpoints.

YouTube: real OAuth2 + resumable upload (see app/publish.py). The OAuth
callback is a plain browser navigation from Google, so it cannot carry the
dashboard's localStorage JWT — it is verified by the one-time state token
instead, and the initiating user is recorded when the auth URL is created.

TikTok / Instagram: honest "Not configured" only — no fake posting. Their
developer-app approval is a manual step for the user (see README).

Without credentials configured, or without a connected account, uploads fail
closed (503) — a publish is never reported as successful without a real
YouTube API response containing a video id.
"""
import os

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import tasks as task_store
from ..auth import get_current_principal
from ..database import SessionLocal, get_db
from ..models import Clip, ConnectedAccount, User
from ..publish import (PublishError, build_auth_url, callback_page_html,
                       clear_tokens, consume_state, exchange_code,
                       fetch_channel_title, load_tokens, upload_to_youtube,
                       youtube_configured, youtube_connected)
from ..schemas import YoutubeUploadIn

router = APIRouter(prefix="/api/publish", tags=["publish"])

TIKTOK_STEPS = [
    "Register your app at developers.tiktok.com and request the video.upload scope (developer approval required).",
    "Set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET in .env, restart, then connect here.",
]
INSTAGRAM_STEPS = [
    "Create a Meta developer app, add the Instagram Graph API product and connect a Business/Creator account (app review may be required).",
    "Set INSTAGRAM_APP_ID, INSTAGRAM_APP_SECRET and a long-lived access token in .env, restart, then connect here.",
]


def _not_configured(platform: str, steps: list[str]):
    return {
        "platform": platform, "status": "not_configured", "configured": False,
        "connected": False,
        "message": f"{platform} publishing is not configured — a developer app and approval are required first.",
        "steps": steps,
    }


@router.get("/status")
def publish_status(user: User = Depends(get_current_principal),
                   db: Session = Depends(get_db)):
    row = db.query(ConnectedAccount).filter(
        ConnectedAccount.user_id == user.id,
        ConnectedAccount.platform == "youtube").first()
    return {
        "youtube": {
            "configured": youtube_configured(),
            "connected": youtube_connected(),
            "channel": row.display if row and row.status == "connected" else None,
            "status": "connected" if youtube_connected() else "not_connected",
        },
        "tiktok": _not_configured("tiktok", TIKTOK_STEPS),
        "instagram": _not_configured("instagram", INSTAGRAM_STEPS),
    }


@router.get("/youtube/auth-url")
def youtube_auth_url(user: User = Depends(get_current_principal)):
    try:
        url, _state = build_auth_url(user.id)
    except PublishError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"auth_url": url,
            "note": "Sign in with Google and allow the YouTube upload permission. "
                    "You will be redirected back here."}


@router.get("/youtube/callback", response_class=HTMLResponse)
def youtube_callback(code: str | None = None, state: str | None = None,
                     error: str | None = None, db: Session = Depends(get_db)):
    if error:
        return callback_page_html(False, f"Google returned an error: {error}")
    if not code or not state:
        return callback_page_html(False, "Missing code or state from Google.")
    user_id = consume_state(state)
    if user_id is None:
        return callback_page_html(
            False, "Invalid or expired state — please start the connect flow "
                   "again from the ClipPilot Publishing tab.")
    try:
        tokens = exchange_code(code)
        title = fetch_channel_title(tokens["access_token"])
    except PublishError as exc:
        return callback_page_html(False, str(exc))
    row = db.query(ConnectedAccount).filter(
        ConnectedAccount.user_id == user_id,
        ConnectedAccount.platform == "youtube").first()
    if row is None:
        row = ConnectedAccount(user_id=user_id, platform="youtube",
                               status="connected", display=title or "YouTube")
        db.add(row)
    else:
        row.status = "connected"
        row.display = title or "YouTube"
    db.commit()
    return callback_page_html(
        True, f"Connected as {title or 'YouTube'} — ClipPilot can now upload "
              "videos to this channel (only when you click Publish).")


@router.post("/youtube/disconnect")
def youtube_disconnect(user: User = Depends(get_current_principal),
                       db: Session = Depends(get_db)):
    clear_tokens()
    row = db.query(ConnectedAccount).filter(
        ConnectedAccount.user_id == user.id,
        ConnectedAccount.platform == "youtube").first()
    if row:
        row.status = "not_connected"
        row.display = ""
        db.commit()
    return {"disconnected": True,
            "message": "YouTube disconnected. The stored tokens were deleted. "
                       "You can also revoke access at myaccount.google.com/permissions."}


def _do_youtube_upload(task_id: str, clip_id: int, title: str,
                       description: str, privacy: str):
    db = SessionLocal()
    try:
        clip = db.query(Clip).filter(Clip.id == clip_id).first()
        if not clip or not clip.file_path or not os.path.exists(clip.file_path):
            task_store.fail_task(task_id, "Clip file not found — generate the clip first.")
            return
        task_store.update_task(task_id, "uploading to YouTube", 40)
        result = upload_to_youtube(clip.file_path, title, description, privacy)
        task_store.finish_task(task_id, {"clip_id": clip_id, "platform": "youtube",
                                         **result})
    except PublishError as exc:
        task_store.fail_task(task_id, str(exc))
    except Exception as exc:
        task_store.fail_task(task_id, str(exc))
    finally:
        db.close()


@router.post("/youtube/upload")
def youtube_upload(payload: YoutubeUploadIn, background: BackgroundTasks,
                   user: User = Depends(get_current_principal),
                   db: Session = Depends(get_db)):
    if not youtube_configured():
        raise HTTPException(
            status_code=503,
            detail="YouTube publishing is not configured. Set YOUTUBE_CLIENT_ID "
                   "and YOUTUBE_CLIENT_SECRET in .env (see README), restart, then "
                   "connect your account.")
    if not youtube_connected():
        raise HTTPException(
            status_code=503,
            detail="YouTube is not connected. Connect it on the Publishing tab first.")
    if payload.privacy not in ("private", "unlisted", "public"):
        raise HTTPException(status_code=400,
                            detail="privacy must be private, unlisted or public")
    clip = db.query(Clip).filter(Clip.id == payload.clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    if clip.status != "ready" or not clip.file_path or not os.path.exists(clip.file_path):
        raise HTTPException(status_code=400,
                            detail="Clip is not rendered yet — generate it first.")
    title = payload.title.strip() or clip.title or f"ClipPilot clip #{clip.id}"
    description = payload.description.strip() or (
        f"{clip.hook_title or ''}\n{clip.reason or ''}\n\nMade with ClipPilot AI."
    ).strip()
    task_id = task_store.create_task("youtube-upload")
    background.add_task(_do_youtube_upload, task_id, clip.id, title,
                        description, payload.privacy)
    return {"task_id": task_id, "status": "running",
            "note": "Upload runs in the background — poll the task for the video URL."}


@router.post("/tiktok/upload")
def tiktok_upload(user: User = Depends(get_current_principal)):
    raise HTTPException(status_code=503, detail=_not_configured("tiktok", TIKTOK_STEPS)["message"])


@router.post("/instagram/upload")
def instagram_upload(user: User = Depends(get_current_principal)):
    raise HTTPException(status_code=503, detail=_not_configured("instagram", INSTAGRAM_STEPS)["message"])
