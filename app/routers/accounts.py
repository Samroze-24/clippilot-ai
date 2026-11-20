"""Publishing accounts (not-connected states), mock billing and local API keys."""
import os

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import get_current_principal, hash_api_key, new_api_key
from ..database import get_db
from ..models import ApiKey, ConnectedAccount, User
from ..schemas import ApiKeyCreate, ApiKeyOut
from ..video import PLAN_LIMITS

router = APIRouter(prefix="/api", tags=["accounts"])

PLATFORMS = {
    "youtube": {
        "label": "YouTube",
        "steps": [
            "Create a project in Google Cloud Console and enable the YouTube Data API v3.",
            "Create OAuth 2.0 credentials (Desktop app) and set the redirect URI to http://127.0.0.1:8012/api/accounts/youtube/callback.",
            "Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in .env, restart, then connect here.",
        ],
    },
    "tiktok": {
        "label": "TikTok",
        "steps": [
            "Register your app at developers.tiktok.com and request the video.upload scope.",
            "Set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET in .env, restart, then connect here.",
        ],
    },
    "instagram": {
        "label": "Instagram",
        "steps": [
            "Create a Meta developer app, add the Instagram Graph API product and connect a Business/Creator account.",
            "Set INSTAGRAM_APP_ID, INSTAGRAM_APP_SECRET and a long-lived access token in .env, restart, then connect here.",
        ],
    },
}


@router.get("/accounts")
def list_accounts(user: User = Depends(get_current_principal),
                  db: Session = Depends(get_db)):
    rows = db.query(ConnectedAccount).filter(ConnectedAccount.user_id == user.id).all()
    out = []
    for r in rows:
        info = PLATFORMS.get(r.platform, {})
        out.append({
            "platform": r.platform, "label": info.get("label", r.platform),
            "status": r.status, "display": r.display,
            "connect_steps": info.get("steps", []),
        })
    return {"accounts": out,
            "note": "Publishing is not connected in this build. No post is ever made on your behalf."}


@router.post("/accounts/{platform}/connect")
def connect_account(platform: str, user: User = Depends(get_current_principal),
                    db: Session = Depends(get_db)):
    if platform not in PLATFORMS:
        raise HTTPException(status_code=404, detail="Unknown platform")
    return {
        "platform": platform, "status": "not_connected",
        "message": "Not connected — OAuth credentials are not configured. Follow the steps to connect.",
        "connect_steps": PLATFORMS[platform]["steps"],
    }


@router.get("/billing")
def billing(user: User = Depends(get_current_principal), db: Session = Depends(get_db)):
    from ..models import Clip, Video
    limits = PLAN_LIMITS.get(user.plan, PLAN_LIMITS["free"])
    videos = db.query(Video).filter(Video.source_type != "sample").count()
    clips = db.query(Clip).count()
    return {
        "plan": user.plan, "mock": True,
        "limits": limits,
        "usage": {"videos": videos, "clips": clips},
        "plans": {
            "free": {"price": "$0", "videos": 5, "clips": 20},
            "pro": {"price": "$19/mo (mock)", "videos": 100, "clips": 500},
        },
        "note": "Billing is MOCK — no payment provider is connected and no money is charged.",
    }


@router.post("/billing/upgrade")
def upgrade(user: User = Depends(get_current_principal), db: Session = Depends(get_db)):
    user.plan = "pro"
    db.commit()
    return {"plan": "pro", "mock": True,
            "message": "Mock upgrade complete — no payment was processed. Plan is now Pro."}


@router.post("/billing/downgrade")
def downgrade(user: User = Depends(get_current_principal), db: Session = Depends(get_db)):
    user.plan = "free"
    db.commit()
    return {"plan": "free", "mock": True, "message": "Plan is now Free (mock)."}


@router.get("/api-keys", response_model=list[ApiKeyOut])
def list_api_keys(user: User = Depends(get_current_principal), db: Session = Depends(get_db)):
    rows = db.query(ApiKey).filter(ApiKey.user_id == user.id).order_by(ApiKey.id.desc()).all()
    return [ApiKeyOut(id=r.id, name=r.name, key_prefix=r.key_prefix, is_active=r.is_active,
                      created_at=r.created_at.isoformat() if r.created_at else "",
                      last_used_at=r.last_used_at.isoformat() if r.last_used_at else None)
            for r in rows]


@router.post("/api-keys", status_code=201)
def create_api_key(payload: ApiKeyCreate, user: User = Depends(get_current_principal),
                   db: Session = Depends(get_db)):
    key = new_api_key()
    record = ApiKey(user_id=user.id, name=payload.name, key_hash=hash_api_key(key),
                    key_prefix=key[:10])
    db.add(record)
    db.commit()
    db.refresh(record)
    return {"id": record.id, "name": record.name, "key_prefix": record.key_prefix,
            "api_key": key,
            "warning": "This is the only time the full key is shown. Store it safely. "
                       "Send it as the X-API-Key header."}


@router.delete("/api-keys/{key_id}")
def revoke_api_key(key_id: int, user: User = Depends(get_current_principal),
                   db: Session = Depends(get_db)):
    record = db.query(ApiKey).filter(ApiKey.id == key_id, ApiKey.user_id == user.id).first()
    if not record:
        raise HTTPException(status_code=404, detail="API key not found")
    record.is_active = False
    db.commit()
    return {"revoked": key_id}


@router.get("/api-keys/providers")
def provider_keys(user: User = Depends(get_current_principal)):
    def status(env_name: str):
        return "configured" if os.getenv(env_name) else "not_configured"
    return {
        "providers": [
            {"name": "AssemblyAI (cloud transcription)", "status": status("ASSEMBLYAI_API_KEY"),
             "env": "ASSEMBLYAI_API_KEY", "note": "Optional. Local faster-whisper is used instead."},
            {"name": "LLM provider (clip selection)", "status": status("LLM_API_KEY"),
             "env": "LLM_API_KEY", "note": "Optional. Local transparent scorer is used instead."},
            {"name": "Pexels (stock B-roll)", "status": status("PEXELS_API_KEY"),
             "env": "PEXELS_API_KEY", "note": "Optional. Upload your own B-roll files instead."},
        ]
    }
