"""Direct publishing endpoints.

YouTube: real OAuth2 + resumable upload (see app/publish.py). The OAuth
callback is a plain browser navigation from Google, so it cannot carry the
dashboard's localStorage JWT — it is verified by the one-time state token
instead, and the initiating user is recorded when the auth URL is created.

TikTok: real Content Posting API (OAuth2 + direct file upload, see
app/publish.py). Same fail-closed pattern: without a developer app +
approval, or without a connected account, uploads fail with 503.

Instagram: real Instagram Graph API (media container + publish, see
app/publish.py). Token-based (long-lived access token in .env). Hard
requirement: the clip must sit at a PUBLIC https URL — Instagram's servers
download it, so localhost cannot work; without INSTAGRAM_VIDEO_BASE_URL the
endpoint fails closed with the exact steps.

Without credentials configured, or without a connected account, uploads fail
closed (503) — a publish is never reported as successful without a real
provider API response containing a publish/media id.
"""
import os

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import tasks as task_store
from ..auth import get_current_principal
from ..database import SessionLocal, get_db
from ..models import Clip, ConnectedAccount, User
from ..publish import (PublishError, build_auth_url, build_tiktok_auth_url,
                       callback_page_html, clear_tiktok_tokens, clear_tokens,
                       consume_state, consume_tiktok_state, exchange_code,
                       exchange_tiktok_code, fetch_channel_title,
                       instagram_config, instagram_configured,
                       instagram_public_url_ready,
                       publish_to_instagram,
                       tiktok_callback_page_html, tiktok_configured,
                       tiktok_connected, upload_to_tiktok, upload_to_youtube,
                       youtube_configured, youtube_connected)
from ..schemas import InstagramUploadIn, TikTokUploadIn, YoutubeUploadIn

router = APIRouter(prefix="/api/publish", tags=["publish"])

TIKTOK_STEPS = [
    "Register your app at developers.tiktok.com and request the video.upload scope (developer approval required — TikTok reviews the app).",
    "Set TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET and TIKTOK_REDIRECT_URI in .env, restart, then connect here.",
]
INSTAGRAM_STEPS = [
    "Create a Meta developer app, add the Instagram Graph API product and connect a Business/Creator account (app review may be required).",
    "Create a long-lived user access token and set INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_USER_ID in .env.",
    "Instagram downloads the video from a PUBLIC https URL — set INSTAGRAM_VIDEO_BASE_URL to a public host that serves your clip files (localhost will not work).",
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
    tiktok_row = db.query(ConnectedAccount).filter(
        ConnectedAccount.user_id == user.id,
        ConnectedAccount.platform == "tiktok").first()
    ig_cfg = instagram_config()
    return {
        "youtube": {
            "configured": youtube_configured(),
            "connected": youtube_connected(),
            "channel": row.display if row and row.status == "connected" else None,
            "status": "connected" if youtube_connected() else "not_connected",
        },
        "tiktok": {
            "configured": tiktok_configured(),
            "connected": tiktok_connected(),
            "channel": tiktok_row.display if tiktok_row and tiktok_row.status == "connected" else None,
            "status": ("connected" if tiktok_connected()
                       else "not_connected" if tiktok_configured() else "not_configured"),
            "message": "" if tiktok_configured() else _not_configured("tiktok", TIKTOK_STEPS)["message"],
            "steps": TIKTOK_STEPS,
        },
        "instagram": {
            "configured": instagram_configured(),
            "connected": instagram_configured(),
            "public_url_ready": instagram_public_url_ready(),
            "status": ("connected" if instagram_public_url_ready()
                       else "not_configured" if not instagram_configured()
                       else "needs_public_url"),
            "message": ("" if instagram_public_url_ready()
                        else "Instagram needs a public video URL — set INSTAGRAM_VIDEO_BASE_URL in .env."
                        if instagram_configured()
                        else _not_configured("instagram", INSTAGRAM_STEPS)["message"]),
            "steps": INSTAGRAM_STEPS,
        },
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


# ---- TikTok (real Content Posting API) ----

@router.get("/tiktok/auth-url")
def tiktok_auth_url(user: User = Depends(get_current_principal)):
    try:
        url, _state = build_tiktok_auth_url(user.id)
    except PublishError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"auth_url": url,
            "note": "Sign in with TikTok and allow the video.upload permission. "
                    "You will be redirected back here."}


@router.get("/tiktok/callback", response_class=HTMLResponse)
def tiktok_callback(code: str | None = None, state: str | None = None,
                    error: str | None = None, db: Session = Depends(get_db)):
    if error:
        return tiktok_callback_page_html(False, f"TikTok returned an error: {error}")
    if not code or not state:
        return tiktok_callback_page_html(False, "Missing code or state from TikTok.")
    user_id = consume_tiktok_state(state)
    if user_id is None:
        return tiktok_callback_page_html(
            False, "Invalid or expired state — please start the connect flow "
                   "again from the ClipPilot Publishing tab.")
    try:
        tokens = exchange_tiktok_code(code)
    except PublishError as exc:
        return tiktok_callback_page_html(False, str(exc))
    display = f"TikTok (open_id {tokens.get('open_id', '')[:12]}…)" if tokens.get("open_id") else "TikTok"
    row = db.query(ConnectedAccount).filter(
        ConnectedAccount.user_id == user_id,
        ConnectedAccount.platform == "tiktok").first()
    if row is None:
        row = ConnectedAccount(user_id=user_id, platform="tiktok",
                               status="connected", display=display)
        db.add(row)
    else:
        row.status = "connected"
        row.display = display
    db.commit()
    return tiktok_callback_page_html(
        True, "TikTok connected — ClipPilot can now upload videos to this "
              "account (only when you click Publish).")


@router.post("/tiktok/disconnect")
def tiktok_disconnect(user: User = Depends(get_current_principal),
                      db: Session = Depends(get_db)):
    clear_tiktok_tokens()
    row = db.query(ConnectedAccount).filter(
        ConnectedAccount.user_id == user.id,
        ConnectedAccount.platform == "tiktok").first()
    if row:
        row.status = "not_connected"
        row.display = ""
        db.commit()
    return {"disconnected": True,
            "message": "TikTok disconnected. The stored tokens were deleted."}


def _do_tiktok_upload(task_id: str, clip_id: int, title: str,
                      description: str, privacy: str):
    db = SessionLocal()
    try:
        clip = db.query(Clip).filter(Clip.id == clip_id).first()
        if not clip or not clip.file_path or not os.path.exists(clip.file_path):
            task_store.fail_task(task_id, "Clip file not found — generate the clip first.")
            return
        task_store.update_task(task_id, "uploading to TikTok", 40)
        full_title = (title + (" " + description if description else "")).strip()
        result = upload_to_tiktok(clip.file_path, full_title or clip.title, privacy)
        task_store.finish_task(task_id, {"clip_id": clip_id, "platform": "tiktok",
                                         **result})
    except PublishError as exc:
        task_store.fail_task(task_id, str(exc))
    except Exception as exc:
        task_store.fail_task(task_id, str(exc))
    finally:
        db.close()


@router.post("/tiktok/upload")
def tiktok_upload(payload: TikTokUploadIn, background: BackgroundTasks,
                  user: User = Depends(get_current_principal),
                  db: Session = Depends(get_db)):
    if not tiktok_configured():
        raise HTTPException(
            status_code=503,
            detail="TikTok publishing is not configured. Register an app at "
                   "developers.tiktok.com, request the video.upload scope, and set "
                   "TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET in .env (see README), "
                   "then connect your account.")
    if not tiktok_connected():
        raise HTTPException(
            status_code=503,
            detail="TikTok is not connected. Connect it on the Publishing tab first.")
    if payload.privacy not in ("SELF_ONLY", "PUBLIC_TO_EVERYONE",
                               "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR"):
        raise HTTPException(
            status_code=400,
            detail="privacy must be SELF_ONLY, PUBLIC_TO_EVERYONE, "
                   "MUTUAL_FOLLOW_FRIENDS or FOLLOWER_OF_CREATOR")
    clip = db.query(Clip).filter(Clip.id == payload.clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    if clip.status != "ready" or not clip.file_path or not os.path.exists(clip.file_path):
        raise HTTPException(status_code=400,
                            detail="Clip is not rendered yet — generate it first.")
    title = payload.title.strip() or clip.title or f"ClipPilot clip #{clip.id}"
    description = payload.description.strip() or (clip.hook_title or "")
    task_id = task_store.create_task("tiktok-upload")
    background.add_task(_do_tiktok_upload, task_id, clip.id, title,
                        description, payload.privacy)
    return {"task_id": task_id, "status": "running",
            "note": "Upload runs in the background — poll the task for the publish id. "
                    "TikTok processes the video after upload (status PROCESSING is normal)."}


# ---- Instagram (real Graph API) ----

def _do_instagram_upload(task_id: str, clip_id: int, caption: str):
    db = SessionLocal()
    try:
        clip = db.query(Clip).filter(Clip.id == clip_id).first()
        if not clip or not clip.file_path or not os.path.exists(clip.file_path):
            task_store.fail_task(task_id, "Clip file not found — generate the clip first.")
            return
        cfg = instagram_config()
        video_url = f"{cfg['video_base_url']}/{os.path.basename(clip.file_path)}"
        task_store.update_task(task_id, "publishing to Instagram", 40)
        result = publish_to_instagram(video_url, caption)
        task_store.finish_task(task_id, {"clip_id": clip_id, "platform": "instagram",
                                         "video_url": video_url, **result})
    except PublishError as exc:
        task_store.fail_task(task_id, str(exc))
    except Exception as exc:
        task_store.fail_task(task_id, str(exc))
    finally:
        db.close()


@router.post("/instagram/upload")
def instagram_upload(payload: InstagramUploadIn, background: BackgroundTasks,
                     user: User = Depends(get_current_principal),
                     db: Session = Depends(get_db)):
    if not instagram_configured():
        raise HTTPException(
            status_code=503,
            detail="Instagram publishing is not configured. Create a Meta developer "
                   "app, add the Instagram Graph API product, connect a "
                   "Business/Creator account and set INSTAGRAM_ACCESS_TOKEN and "
                   "INSTAGRAM_USER_ID in .env (see README).")
    if not instagram_public_url_ready():
        raise HTTPException(
            status_code=503,
            detail="Instagram needs the clip at a PUBLIC https URL — its servers "
                   "download the video. Set INSTAGRAM_VIDEO_BASE_URL in .env to a "
                   "public host that serves your clip files (localhost will not work).")
    clip = db.query(Clip).filter(Clip.id == payload.clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    if clip.status != "ready" or not clip.file_path or not os.path.exists(clip.file_path):
        raise HTTPException(status_code=400,
                            detail="Clip is not rendered yet — generate it first.")
    caption = payload.caption.strip() or (
        f"{clip.hook_title or ''}\n{clip.reason or ''}\n\nMade with ClipPilot AI."
    ).strip()
    task_id = task_store.create_task("instagram-upload")
    background.add_task(_do_instagram_upload, task_id, clip.id, caption)
    return {"task_id": task_id, "status": "running",
            "note": "Publish runs in the background — poll the task for the media id / permalink. "
                    "Instagram downloads the clip from your public INSTAGRAM_VIDEO_BASE_URL."}
