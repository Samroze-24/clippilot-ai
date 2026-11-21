"""Models for ClipPilot AI.

Local only: videos, transcripts, clips, clip stats, API keys, connected
accounts (not-connected states) and billing plan all live in SQLite.
No external service is ever called at runtime.
"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text

from .database import Base


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False)
    email = Column(String(200), unique=True, index=True, nullable=False)
    hashed_password = Column(String(300), nullable=False)
    role = Column(String(20), default="admin")
    plan = Column(String(20), default="free")  # free | pro (mock billing)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Video(Base):
    __tablename__ = "videos"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(300), nullable=False)
    # source_type: upload | url | sample (seeded SAMPLE entry, no file)
    source_type = Column(String(20), default="upload")
    source_url = Column(String(1000), default="")
    filename = Column(String(300), default="")
    stored_path = Column(String(1000), default="")
    duration_sec = Column(Float, default=0.0)
    size_bytes = Column(Integer, default=0)
    width = Column(Integer, default=0)
    height = Column(Integer, default=0)
    # status: uploaded | analyzing | analyzed | failed
    status = Column(String(20), default="uploaded")
    # transcript_status: none | uploaded | transcribing | transcribed | failed | unavailable
    transcript_status = Column(String(20), default="none")
    transcript_path = Column(String(1000), default="")
    error = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class Transcript(Base):
    __tablename__ = "transcripts"
    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(Integer, ForeignKey("videos.id"), nullable=False)
    # source: upload | whisper | assemblyai
    source = Column(String(20), default="upload")
    language = Column(String(10), default="en")
    # JSON: [{"text": str, "start": float, "end": float}]
    segments_json = Column(Text, default="[]")
    # JSON: [{"word": str, "start": float, "end": float}]
    words_json = Column(Text, default="[]")
    created_at = Column(DateTime, default=datetime.utcnow)


class Clip(Base):
    __tablename__ = "clips"
    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(Integer, ForeignKey("videos.id"), nullable=False)
    title = Column(String(300), default="")
    hook_title = Column(String(300), default="")
    start_sec = Column(Float, nullable=False)
    end_sec = Column(Float, nullable=False)
    duration_sec = Column(Float, default=0.0)
    score = Column(Float, default=0.0)
    # JSON: {"hook":..,"engagement":..,"value":..,"shareability":..,"signals":{...}}
    breakdown_json = Column(Text, default="{}")
    reason = Column(Text, default="")
    # status: queued (candidate, not rendered yet) | processing | ready | failed
    status = Column(String(20), default="queued")
    file_path = Column(String(1000), default="")
    # caption_status: none | burned_in | sidecar_only
    caption_status = Column(String(20), default="none")
    caption_template = Column(String(30), default="bold-yellow")
    # preset: default | tiktok | reels | shorts
    preset = Column(String(20), default="default")
    parent_clip_id = Column(Integer, nullable=True)
    has_broll = Column(Boolean, default=False)
    error = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class ClipStat(Base):
    """Performance numbers the user logs manually. Never invented."""
    __tablename__ = "clip_stats"
    id = Column(Integer, primary_key=True, index=True)
    clip_id = Column(Integer, ForeignKey("clips.id"), nullable=False)
    views = Column(Integer, default=0)
    likes = Column(Integer, default=0)
    comments = Column(Integer, default=0)
    shares = Column(Integer, default=0)
    note = Column(String(300), default="")
    logged_at = Column(DateTime, default=datetime.utcnow)


class ApiKey(Base):
    """Local API keys for the ClipPilot REST API (sha256 hash stored)."""
    __tablename__ = "api_keys"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(120), nullable=False)
    key_hash = Column(String(100), nullable=False)
    key_prefix = Column(String(20), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_used_at = Column(DateTime, nullable=True)


class ConnectedAccount(Base):
    """Publishing accounts. Always 'not_connected' in this build — OAuth is
    documented, never faked."""
    __tablename__ = "connected_accounts"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    platform = Column(String(30), nullable=False)  # youtube | tiktok | instagram
    status = Column(String(30), default="not_connected")
    display = Column(String(200), default="")
    created_at = Column(DateTime, default=datetime.utcnow)
