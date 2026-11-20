"""In-memory background task store.

Analysis and rendering run in FastAPI BackgroundTasks; the frontend polls
GET /api/tasks/{task_id} for stage + progress %. Tasks are ephemeral —
restarting the server clears them, which is fine for a local demo.
"""
import itertools
import threading
import time
import uuid

_lock = threading.Lock()
_tasks: dict[str, dict] = {}


def create_task(kind: str) -> str:
    task_id = uuid.uuid4().hex[:12]
    with _lock:
        _tasks[task_id] = {
            "id": task_id,
            "kind": kind,
            "stage": "queued",
            "progress": 0,
            "status": "running",
            "result": {},
            "error": "",
            "created_at": time.time(),
        }
    return task_id


def update_task(task_id: str, stage: str, progress: int) -> None:
    with _lock:
        task = _tasks.get(task_id)
        if task:
            task["stage"] = stage
            task["progress"] = max(0, min(100, progress))


def finish_task(task_id: str, result: dict | None = None) -> None:
    with _lock:
        task = _tasks.get(task_id)
        if task:
            task["stage"] = "done"
            task["progress"] = 100
            task["status"] = "done"
            task["result"] = result or {}


def fail_task(task_id: str, error: str) -> None:
    with _lock:
        task = _tasks.get(task_id)
        if task:
            task["stage"] = "failed"
            task["status"] = "failed"
            task["error"] = error


def get_task(task_id: str) -> dict | None:
    with _lock:
        task = _tasks.get(task_id)
        return dict(task) if task else None
