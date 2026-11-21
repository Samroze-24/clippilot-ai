"""ClipPilot AI — local-first OpusClip-style video clipping tool.

Upload a long video (or a public URL), analyse it locally, get ranked
9:16 clips with transparent scores, and render them with real ffmpeg.
Local by default; optional cloud features (Groq LLM clip selection, direct
YouTube/TikTok/Instagram publishing, AssemblyAI transcription, Pexels
B-roll, Twitch VOD import) run only when you configure their credentials.
No paid service is ever required. An MCP server (python -m app.mcp) exposes
the same features to MCP clients.
"""
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .database import Base, engine
from .routers import accounts, auth, clips, publish, videos
from .video import ffmpeg_version

app = FastAPI(title="ClipPilot AI", version="1.3.0",
              description="Local-first video clipping: long video in, short vertical "
                          "clips out. Uploads, URL ingestion (yt-dlp), transparent "
                          "local highlight scoring with optional Groq LLM clip "
                          "selection, face-centred 9:16 rendering, "
                          "word-synced captions, editor (trim/split/merge), platform "
                          "export presets, direct YouTube/TikTok/Instagram publishing "
                          "(OAuth / Graph API), Pexels stock B-roll, Twitch VOD import, "
                          "AssemblyAI transcription option, MCP server, mock "
                          "billing and local API keys. Cloud features only run when "
                          "you configure them — otherwise everything stays local.")

Base.metadata.create_all(bind=engine)

app.include_router(auth.router)
app.include_router(videos.router)
app.include_router(clips.router)
app.include_router(accounts.router)
app.include_router(publish.router)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/health")
def health():
    return {"status": "ok", "service": "clippilot-ai", "mode": "local-demo",
            "ffmpeg": ffmpeg_version()}


@app.get("/")
def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
