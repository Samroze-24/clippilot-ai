"""Pydantic schemas for ClipPilot AI."""
from pydantic import BaseModel, EmailStr


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: int
    name: str
    role: str


class UserOut(BaseModel):
    id: int
    name: str
    email: str
    role: str
    plan: str

    class Config:
        from_attributes = True


class VideoOut(BaseModel):
    id: int
    title: str
    source_type: str
    source_url: str
    filename: str
    duration_sec: float
    size_bytes: int
    width: int
    height: int
    status: str
    transcript_status: str
    created_at: str

    class Config:
        from_attributes = True


class UrlIngestIn(BaseModel):
    url: str
    title: str = ""


class ClipOut(BaseModel):
    id: int
    video_id: int
    title: str
    hook_title: str
    start_sec: float
    end_sec: float
    duration_sec: float
    score: float
    breakdown: dict = {}
    reason: str
    status: str
    caption_status: str
    caption_template: str
    preset: str
    parent_clip_id: int | None = None
    has_broll: bool
    created_at: str

    class Config:
        from_attributes = True


class GenerateIn(BaseModel):
    caption_template: str = "bold-yellow"
    preset: str = "default"
    broll_filename: str = ""
    include_hook_title: bool = True


class TrimIn(BaseModel):
    start_sec: float
    end_sec: float


class SplitIn(BaseModel):
    at_sec: float


class MergeIn(BaseModel):
    clip_ids: list[int]
    title: str = "Merged clip"


class ExportIn(BaseModel):
    preset: str  # tiktok | reels | shorts


class StatIn(BaseModel):
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    note: str = ""


class ApiKeyCreate(BaseModel):
    name: str


class YoutubeUploadIn(BaseModel):
    clip_id: int
    title: str = ""
    description: str = ""
    privacy: str = "private"  # private | unlisted | public (default private)


class ApiKeyOut(BaseModel):
    id: int
    name: str
    key_prefix: str
    is_active: bool
    created_at: str
    last_used_at: str | None = None

    class Config:
        from_attributes = True


class TaskOut(BaseModel):
    id: str
    kind: str
    stage: str
    progress: int
    status: str  # running | done | failed
    result: dict = {}
    error: str = ""
