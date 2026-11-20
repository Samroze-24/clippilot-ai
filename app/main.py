"""ClipPilot AI — local OpusClip-style video clipping tool.

Upload a long video (or a public URL), analyse it locally, get ranked
9:16 clips with transparent scores, and render them with real ffmpeg.
No cloud AI, no API key, no paid service.
"""
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .database import Base, engine
from .routers import accounts, auth, clips, videos
from .video import ffmpeg_version

app = FastAPI(title="ClipPilot AI", version="1.0.0",
              description="Local video clipping: long video in, short vertical "
                          "clips out. Uploads, URL ingestion (yt-dlp), transparent "
                          "local highlight scoring, face-centred 9:16 rendering, "
                          "word-synced captions, editor (trim/split/merge), platform "
                          "export presets, mock billing and local API keys. "
                          "No cloud AI, no API key.")

Base.metadata.create_all(bind=engine)

app.include_router(auth.router)
app.include_router(videos.router)
app.include_router(clips.router)
app.include_router(accounts.router)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/health")
def health():
    return {"status": "ok", "service": "clippilot-ai", "mode": "local-demo",
            "ffmpeg": ffmpeg_version()}


@app.get("/")
def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
