"""Seed ClipPilot AI with the demo admin, SAMPLE videos/clips and
not-connected publishing accounts.

Run:  python -m app.seed
Re-running resets the database so the demo is always the same clean
starting point. No demo video file is bundled — upload your own.
"""
import json

from .auth import get_password_hash
from .database import Base, SessionLocal, engine
from .models import Clip, ConnectedAccount, User, Video


def _breakdown(hook, engagement, value, shareability, density, length, hits):
    return json.dumps({
        "hook": hook, "engagement": engagement, "value": value,
        "shareability": shareability,
        "signals": {"speech_density": density, "length_sec": length,
                    "length_fit": 0.9, "position": 0.8, "energy": 0.7,
                    "keyword_hits": hits, "stats_boost": 0.0},
    })


def seed():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        admin = User(name="ClipPilot Admin", email="admin@clippilot.app",
                     hashed_password=get_password_hash("admin123"),
                     role="admin", plan="free")
        db.add(admin)
        db.commit()
        db.refresh(admin)

        for platform in ("youtube", "tiktok", "instagram"):
            db.add(ConnectedAccount(user_id=admin.id, platform=platform,
                                    status="not_connected", display=""))
        db.commit()

        v1 = Video(title="SAMPLE — Product launch walkthrough (12 min)",
                   source_type="sample", filename="", stored_path="",
                   duration_sec=720.0, size_bytes=0, width=1280, height=720,
                   status="analyzed", transcript_status="none")
        v2 = Video(title="SAMPLE — Founder Q&A livestream (38 min)",
                   source_type="sample", filename="", stored_path="",
                   duration_sec=2280.0, size_bytes=0, width=1920, height=1080,
                   status="analyzed", transcript_status="none")
        db.add_all([v1, v2])
        db.commit()
        db.refresh(v1)
        db.refresh(v2)

        samples = [
            (v1.id, "The pricing trick nobody shows you", "Pricing: what actually works",
             42.0, 74.0, 87.5, 91.0, 84.0, 82.0, 88.0, 0.92, 32.0, 5,
             "Dense speech (92%) over 32s with 5 keyword hits, energy 88%, near the start of the video."),
            (v1.id, "Why onboarding matters more than you think", "Onboarding: what actually works",
             180.0, 215.0, 81.2, 78.0, 85.0, 79.0, 83.0, 0.88, 35.0, 3,
             "Dense speech (88%) over 35s with 3 keyword hits, energy 83%, near the middle of the video."),
            (v2.id, "Stop doing launches wrong", "Launches: what actually works",
             640.0, 668.0, 76.4, 72.0, 80.0, 74.0, 79.0, 0.81, 28.0, 2,
             "Dense speech (81%) over 28s with 2 keyword hits, energy 79%, near the middle of the video."),
        ]
        for vid, title, hook, start, end, score, h, e, v, s, dens, length, hits, reason in samples:
            db.add(Clip(
                video_id=vid, title=title, hook_title=hook,
                start_sec=start, end_sec=end, duration_sec=round(end - start, 1),
                score=score, breakdown_json=_breakdown(h, e, v, s, dens, length, hits),
                reason=reason, status="queued", file_path="",
                caption_status="none", caption_template="bold-yellow",
                preset="default", parent_clip_id=None, has_broll=False,
            ))
        db.commit()
        print("Seeded ClipPilot AI: admin@clippilot.app / admin123, 2 SAMPLE videos, 3 SAMPLE clips.")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
