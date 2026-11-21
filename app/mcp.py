"""MCP server for ClipPilot AI — stdio JSON-RPC, hand-rolled, zero new deps.

Run as:  python -m app.mcp

Speaks the Model Context Protocol over stdio (newline-delimited JSON-RPC
2.0, the MCP stdio transport). Lets an MCP client (e.g. Claude Desktop)
list videos, analyse them, render clips and publish to YouTube — the same
local database and the same code paths as the web dashboard.

Tools:
  list_videos, analyze_video, list_clips, render_clip, get_clip,
  youtube_status, publish_to_youtube

The server runs locally with full access to the local ClipPilot database —
do NOT expose its stdio to anyone else. It never calls a cloud provider
except through the same fail-closed paths as the dashboard (Groq LLM when
configured, YouTube OAuth when connected).

Protocol handled: initialize, notifications/initialized, ping,
tools/list, tools/call. Anything else returns a JSON-RPC -32601 error.
"""
import json
import sys

from . import tasks as task_store
from .database import SessionLocal
from .models import Clip, Video
from .publish import (PublishError, upload_to_youtube, youtube_configured,
                      youtube_connected)

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "clippilot-ai", "version": "1.3.0"}


class MCPError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


TOOLS = [
    {
        "name": "list_videos",
        "description": "List all videos in ClipPilot (uploads, URL ingests, samples).",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "analyze_video",
        "description": "Analyse a video: detect speech and rank 15-60s clip candidates (local scorer, or Groq LLM picks when configured). Returns clip_ids.",
        "inputSchema": {"type": "object",
                        "properties": {"video_id": {"type": "integer"}},
                        "required": ["video_id"]},
    },
    {
        "name": "list_clips",
        "description": "List clips, optionally filtered by video_id.",
        "inputSchema": {"type": "object",
                        "properties": {"video_id": {"type": "integer"}},
                        "required": []},
    },
    {
        "name": "render_clip",
        "description": "Render a clip candidate to a real 720x1280 MP4 with ffmpeg (face-centred crop, captions when a transcript exists).",
        "inputSchema": {"type": "object",
                        "properties": {
                            "clip_id": {"type": "integer"},
                            "caption_template": {"type": "string"},
                            "preset": {"type": "string"},
                            "broll_filename": {"type": "string"},
                        },
                        "required": ["clip_id"]},
    },
    {
        "name": "get_clip",
        "description": "Get one clip with its score breakdown and status.",
        "inputSchema": {"type": "object",
                        "properties": {"clip_id": {"type": "integer"}},
                        "required": ["clip_id"]},
    },
    {
        "name": "youtube_status",
        "description": "Whether YouTube publishing is configured and connected.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "publish_to_youtube",
        "description": "Upload a rendered clip to YouTube (OAuth, default private). Only works when YouTube is connected; never fakes a publish.",
        "inputSchema": {"type": "object",
                        "properties": {
                            "clip_id": {"type": "integer"},
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "privacy": {"type": "string"},
                        },
                        "required": ["clip_id"]},
    },
]


def _video_dict(v: Video) -> dict:
    return {"id": v.id, "title": v.title, "source_type": v.source_type,
            "source_url": v.source_url or "", "duration_sec": v.duration_sec,
            "status": v.status, "transcript_status": v.transcript_status}


def _clip_dict(c: Clip) -> dict:
    try:
        breakdown = json.loads(c.breakdown_json or "{}")
    except Exception:
        breakdown = {}
    return {"id": c.id, "video_id": c.video_id, "title": c.title,
            "hook_title": c.hook_title, "start_sec": c.start_sec,
            "end_sec": c.end_sec, "duration_sec": c.duration_sec,
            "score": c.score, "breakdown": breakdown, "reason": c.reason,
            "status": c.status, "caption_status": c.caption_status,
            "preset": c.preset, "has_broll": c.has_broll,
            "file_path": c.file_path or ""}


def _run_task(kind: str, fn, *args) -> dict:
    """Run a dashboard background-task function synchronously and return
    its task result (or raise with the task's error)."""
    task_id = task_store.create_task(kind)
    fn(task_id, *args)
    task = task_store.get_task(task_id) or {}
    if task.get("status") == "failed":
        raise MCPError(-32603, task.get("error") or f"{kind} failed")
    return task.get("result") or {}


def tool_list_videos(args: dict) -> dict:
    db = SessionLocal()
    try:
        videos = db.query(Video).order_by(Video.id.desc()).all()
        return {"videos": [_video_dict(v) for v in videos]}
    finally:
        db.close()


def tool_analyze_video(args: dict) -> dict:
    video_id = args.get("video_id")
    if not isinstance(video_id, int):
        raise MCPError(-32602, "video_id (integer) is required")
    db = SessionLocal()
    try:
        video = db.query(Video).filter(Video.id == video_id).first()
        if not video:
            raise MCPError(-32602, f"Video {video_id} not found")
        if not video.stored_path:
            raise MCPError(-32602, "SAMPLE entries have no file to analyze. Upload a video first.")
    finally:
        db.close()
    from .routers.videos import _do_analyze
    return _run_task("analyze", _do_analyze, video_id)


def tool_list_clips(args: dict) -> dict:
    db = SessionLocal()
    try:
        q = db.query(Clip)
        video_id = args.get("video_id")
        if video_id is not None:
            q = q.filter(Clip.video_id == int(video_id))
        clips = q.order_by(Clip.score.desc()).all()
        return {"clips": [_clip_dict(c) for c in clips]}
    finally:
        db.close()


def tool_render_clip(args: dict) -> dict:
    clip_id = args.get("clip_id")
    if not isinstance(clip_id, int):
        raise MCPError(-32602, "clip_id (integer) is required")
    template = args.get("caption_template") or "bold-yellow"
    broll = args.get("broll_filename") or ""
    db = SessionLocal()
    try:
        clip = db.query(Clip).filter(Clip.id == clip_id).first()
        if not clip:
            raise MCPError(-32602, f"Clip {clip_id} not found")
        if clip.status == "processing":
            raise MCPError(-32603, "Clip is already rendering.")
    finally:
        db.close()
    from .routers.clips import _render_clip_task
    return _run_task("render", _render_clip_task, clip_id, template, True, broll)


def tool_get_clip(args: dict) -> dict:
    clip_id = args.get("clip_id")
    if not isinstance(clip_id, int):
        raise MCPError(-32602, "clip_id (integer) is required")
    db = SessionLocal()
    try:
        clip = db.query(Clip).filter(Clip.id == clip_id).first()
        if not clip:
            raise MCPError(-32602, f"Clip {clip_id} not found")
        return {"clip": _clip_dict(clip)}
    finally:
        db.close()


def tool_youtube_status(args: dict) -> dict:
    return {"configured": youtube_configured(), "connected": youtube_connected()}


def tool_publish_to_youtube(args: dict) -> dict:
    clip_id = args.get("clip_id")
    if not isinstance(clip_id, int):
        raise MCPError(-32602, "clip_id (integer) is required")
    privacy = args.get("privacy") or "private"
    if privacy not in ("private", "unlisted", "public"):
        raise MCPError(-32602, "privacy must be private, unlisted or public")
    db = SessionLocal()
    try:
        clip = db.query(Clip).filter(Clip.id == clip_id).first()
        if not clip:
            raise MCPError(-32602, f"Clip {clip_id} not found")
        if clip.status != "ready" or not clip.file_path:
            raise MCPError(-32603, "Clip is not rendered yet — generate it first.")
        title = (args.get("title") or "").strip() or clip.title or f"ClipPilot clip #{clip.id}"
        description = (args.get("description") or "").strip() or (
            f"{clip.hook_title or ''}\n{clip.reason or ''}\n\nMade with ClipPilot AI."
        ).strip()
        file_path = clip.file_path
    finally:
        db.close()
    try:
        return upload_to_youtube(file_path, title, description, privacy)
    except PublishError as exc:
        raise MCPError(-32603, str(exc)) from exc


TOOL_HANDLERS = {
    "list_videos": tool_list_videos,
    "analyze_video": tool_analyze_video,
    "list_clips": tool_list_clips,
    "render_clip": tool_render_clip,
    "get_clip": tool_get_clip,
    "youtube_status": tool_youtube_status,
    "publish_to_youtube": tool_publish_to_youtube,
}


def handle_request(msg: dict):
    """Return a JSON-RPC response dict, or None for notifications."""
    method = msg.get("method", "")
    req_id = msg.get("id")
    is_notification = "id" not in msg
    params = msg.get("params") or {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code: int, message: str):
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": code, "message": message}}

    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        return ok({
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return err(-32602, f"Unknown tool: {name}")
        try:
            result = handler(args if isinstance(args, dict) else {})
        except MCPError as exc:
            return ok({"content": [{"type": "text", "text": exc.message}],
                       "isError": True})
        except Exception as exc:
            return ok({"content": [{"type": "text", "text": str(exc)}],
                       "isError": True})
        return ok({"content": [{"type": "text", "text": json.dumps(result)}],
                   "isError": False})
    if is_notification:
        return None
    return err(-32601, f"Method not found: {method}")


def main() -> None:
    """Read NDJSON JSON-RPC from stdin, write responses to stdout."""
    from .database import Base, engine
    Base.metadata.create_all(bind=engine)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "Parse error"}}) + "\n")
            sys.stdout.flush()
            continue
        if not isinstance(msg, dict):
            continue
        try:
            resp = handle_request(msg)
        except Exception as exc:
            resp = {"jsonrpc": "2.0", "id": msg.get("id"),
                    "error": {"code": -32603, "message": str(exc)}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
