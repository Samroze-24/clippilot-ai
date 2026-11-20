"""ClipPilot AI smoke tests — run in a fresh venv:
    pytest -q
Covers auth, upload, analysis, transparent scoring, real ffmpeg rendering
(720x1280 + audio), face-crop math, transcript upload, editor (trim/split/
merge/export), API keys, billing limits and not-connected states.
"""
import json
import os
import subprocess
import time

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_clippilot.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import video as video_mod  # noqa: E402
from app.captions import extract_keywords, parse_srt  # noqa: E402
from app.database import engine as _engine  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import clips as clips_router  # noqa: E402
from app.routers import videos as videos_router  # noqa: E402
from app.seed import seed  # noqa: E402

TEST_VIDEO = os.path.join(os.path.dirname(__file__), "test_video.mp4")


def _ffmpeg():
    return video_mod.ffmpeg_exe()


def _make_test_video(path: str):
    """20s testsrc video, 640x360, sine tone with two silent gaps (5-7s, 13-15s)."""
    if os.path.exists(path):
        return
    cmd = [
        _ffmpeg(), "-y",
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=20",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
        "-filter_complex",
        "[1:a]volume=0:enable='between(t,5,7)+between(t,13,15)'[a]",
        "-map", "0:v", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-shortest",
        path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=300)


@pytest.fixture(scope="session")
def test_video(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("media") / "test_video.mp4")
    _make_test_video(path)
    return path


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Isolated media dirs so tests never touch the real uploads//clips/.
    up = tmp_path / "uploads"
    cl = tmp_path / "clips"
    br = tmp_path / "broll"
    for d in (up, cl, br):
        d.mkdir()
    monkeypatch.setattr(videos_router, "UPLOAD_DIR", str(up))
    monkeypatch.setattr(videos_router, "CLIP_DIR", str(cl))
    monkeypatch.setattr(clips_router, "CLIP_DIR", str(cl))
    monkeypatch.setattr(clips_router, "BROLL_DIR", str(br))
    # Fresh DB per test: dispose pooled connections BEFORE deleting the file,
    # otherwise SQLite keeps a handle on the deleted inode and reports the
    # database as readonly on the next write.
    _engine.dispose()
    if os.path.exists("test_clippilot.db"):
        os.remove("test_clippilot.db")
    seed()
    with TestClient(app) as c:
        yield c
    if os.path.exists("test_clippilot.db"):
        os.remove("test_clippilot.db")


@pytest.fixture()
def auth_headers(client):
    r = client.post("/auth/login", json={"email": "admin@clippilot.app", "password": "admin123"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def _wait_task(client, headers, task_id, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/videos/tasks/{task_id}", headers=headers)
        assert r.status_code == 200, r.text
        t = r.json()
        if t["status"] in ("done", "failed"):
            return t
        time.sleep(1)
    raise AssertionError(f"task {task_id} did not finish in {timeout}s")


def _upload_video(client, headers, test_video, title="Test video"):
    with open(test_video, "rb") as f:
        r = client.post("/api/videos/upload", headers=headers,
                        data={"title": title},
                        files={"file": ("test_video.mp4", f, "video/mp4")})
    assert r.status_code == 201, r.text
    return r.json()


# ---- basics + auth ----

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert "ffmpeg" in r.json()


def test_root_serves_dashboard(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "ClipPilot" in r.text


def test_docs(client):
    assert client.get("/docs").status_code == 200


def test_login_json_ok(auth_headers):
    assert auth_headers["Authorization"].startswith("Bearer ")


def test_login_form_ok(client):
    r = client.post("/auth/login-form", data={"username": "admin@clippilot.app", "password": "admin123"})
    assert r.status_code == 200, r.text
    assert r.json()["access_token"]


def test_login_wrong_401(client):
    r = client.post("/auth/login", json={"email": "admin@clippilot.app", "password": "wrong"})
    assert r.status_code == 401


def test_unauth_401(client):
    assert client.get("/api/videos").status_code == 401


def test_openapi_token_url(client):
    spec = client.get("/openapi.json").json()
    assert spec["components"]["securitySchemes"]["OAuth2PasswordBearer"]["flows"]["password"]["tokenUrl"] == "/auth/login-form"


# ---- upload ----

def test_upload_video(client, auth_headers, test_video):
    v = _upload_video(client, auth_headers, test_video)
    assert v["status"] == "uploaded"
    assert 19 <= v["duration_sec"] <= 21
    assert v["width"] == 640 and v["height"] == 360


def test_upload_bad_extension_400(client, auth_headers):
    r = client.post("/api/videos/upload", headers=auth_headers,
                    data={"title": "x"},
                    files={"file": ("notes.txt", b"hello", "text/plain")})
    assert r.status_code == 400


# ---- analysis + scoring ----

def test_analyze_returns_candidates(client, auth_headers, test_video):
    v = _upload_video(client, auth_headers, test_video)
    r = client.post(f"/api/videos/{v['id']}/analyze", headers=auth_headers)
    assert r.status_code == 200, r.text
    t = _wait_task(client, auth_headers, r.json()["task_id"])
    assert t["status"] == "done", t
    assert t["result"]["candidates"] >= 1
    clips = client.get(f"/api/clips?video_id={v['id']}", headers=auth_headers).json()
    assert len(clips) >= 1
    c = clips[0]
    assert 0 <= c["score"] <= 100
    for key in ("hook", "engagement", "value", "shareability"):
        assert key in c["breakdown"], c["breakdown"]
        assert 0 <= c["breakdown"][key] <= 100
    assert c["reason"]
    assert c["status"] == "queued"


def test_scoring_unit_deterministic():
    speech = [(0.0, 5.0), (7.0, 13.0), (15.0, 20.0)]
    keywords = [("launch", 4), ("price", 3)]
    words = [{"word": "launch", "start": 1.0, "end": 1.5},
             {"word": "price", "start": 8.0, "end": 8.5}]
    cands = video_mod.score_candidates(20.0, speech, keywords, words, top_n=5)
    assert len(cands) >= 1
    c = cands[0]
    assert 15 <= c["duration"] <= 60
    assert 0 <= c["score"] <= 100
    assert c["breakdown"]["hook"] >= 0
    # Empty speech -> no candidates (never invent clips).
    assert video_mod.score_candidates(20.0, [], keywords, words) == []


def test_keywords_and_srt_parse():
    srt = ("1\n00:00:01,000 --> 00:00:04,000\nHello world launch\n\n"
           "2\n00:00:05,500 --> 00:00:08,000\nPricing tips here\n")
    segs = parse_srt(srt)
    assert len(segs) == 2
    assert segs[0]["start"] == 1.0 and segs[0]["end"] == 4.0
    kw = extract_keywords("launch launch price launch tips")
    assert kw[0][0] == "launch" and kw[0][1] == 3


# ---- rendering (real ffmpeg) ----

def test_generate_clip_renders_720x1280_with_audio(client, auth_headers, test_video):
    v = _upload_video(client, auth_headers, test_video)
    r = client.post(f"/api/videos/{v['id']}/analyze", headers=auth_headers)
    t = _wait_task(client, auth_headers, r.json()["task_id"])
    assert t["status"] == "done"
    clip = client.get(f"/api/clips?video_id={v['id']}", headers=auth_headers).json()[0]
    r = client.post(f"/api/clips/{clip['id']}/generate", headers=auth_headers,
                    json={"caption_template": "bold-yellow", "preset": "default",
                          "include_hook_title": False})
    assert r.status_code == 200, r.text
    t = _wait_task(client, auth_headers, r.json()["task_id"], timeout=240)
    assert t["status"] == "done", t
    assert t["result"]["width"] == 720 and t["result"]["height"] == 1280
    got = client.get(f"/api/clips/{clip['id']}", headers=auth_headers).json()
    assert got["status"] == "ready"
    # Probe the rendered file independently: 720x1280 + audio stream.
    from app.database import SessionLocal
    from app.models import Clip
    db = SessionLocal()
    path = db.query(Clip).filter(Clip.id == clip["id"]).first().file_path
    db.close()
    assert os.path.exists(path) and os.path.getsize(path) > 0
    info = video_mod.probe_video(path)
    assert info["width"] == 720 and info["height"] == 1280
    assert 14 <= info["duration"] <= 21
    probe = subprocess.run([_ffmpeg(), "-i", path], capture_output=True, text=True, timeout=60)
    assert "Audio:" in (probe.stderr or ""), "rendered clip has no audio stream"


def test_face_crop_math():
    # Landscape source, no face -> centred 405x720 crop.
    assert video_mod.compute_crop(1280, 720) == (405, 720, 437, 0)
    # Landscape source, face on the left -> crop follows the face.
    w, h, x, y = video_mod.compute_crop(1280, 720, face_box=(100, 200, 120, 120))
    assert (w, h) == (405, 720) and 0 <= x <= 1280 - 405 and x < 437
    # Portrait source, no face -> centred crop.
    w, h, x, y = video_mod.compute_crop(720, 1280)
    assert (w, h) == (720, 1280) and (x, y) == (0, 0)


def test_face_detector_fallback_on_test_frame(client, test_video):
    frame = os.path.join(os.path.dirname(test_video), "frame.jpg")
    assert video_mod.extract_frame(test_video, 2.0, frame)
    # testsrc has no human face -> detector returns None (centre-crop fallback).
    assert video_mod.detect_face(frame) is None


# ---- transcript ----

def test_transcript_upload_srt(client, auth_headers, test_video):
    v = _upload_video(client, auth_headers, test_video)
    srt = ("1\n00:00:01,000 --> 00:00:04,000\nHello launch world\n\n"
           "2\n00:00:08,000 --> 00:00:11,000\nPricing tips here\n")
    r = client.post(f"/api/videos/{v['id']}/transcript", headers=auth_headers,
                    files={"file": ("subs.srt", srt.encode(), "text/plain")})
    assert r.status_code == 201, r.text
    assert r.json()["segments"] == 2 and r.json()["words"] > 0
    tr = client.get(f"/api/videos/{v['id']}/transcript", headers=auth_headers).json()
    assert tr["source"] == "upload" and len(tr["words"]) == 6


def test_transcribe_endpoint_task(client, auth_headers, test_video, monkeypatch):
    v = _upload_video(client, auth_headers, test_video)
    # Stub the engine so the test covers the endpoint + task plumbing without
    # downloading the Whisper model (real transcription is verified end-to-end
    # separately with a TTS-spoken test video).
    monkeypatch.setattr(videos_router, "transcribe_whisper",
                        lambda path: {"segments": [{"text": "hello world", "start": 1.0, "end": 2.0}],
                                      "words": [{"word": "hello", "start": 1.0, "end": 1.5},
                                                {"word": "world", "start": 1.5, "end": 2.0}],
                                      "language": "en"})
    r = client.post(f"/api/videos/{v['id']}/transcribe", headers=auth_headers)
    assert r.status_code == 200, r.text
    t = _wait_task(client, auth_headers, r.json()["task_id"])
    assert t["status"] == "done", t
    assert t["result"]["words"] == 2


# ---- editor: trim / split / merge / export ----

def test_editor_trim_split_merge_export(client, auth_headers, test_video):
    v = _upload_video(client, auth_headers, test_video)
    r = client.post(f"/api/videos/{v['id']}/analyze", headers=auth_headers)
    _wait_task(client, auth_headers, r.json()["task_id"])
    clip = client.get(f"/api/clips?video_id={v['id']}", headers=auth_headers).json()[0]
    # Generate the base clip first.
    r = client.post(f"/api/clips/{clip['id']}/generate", headers=auth_headers,
                    json={"caption_template": "bold-yellow", "preset": "default",
                          "include_hook_title": False})
    _wait_task(client, auth_headers, r.json()["task_id"], timeout=240)
    base = client.get(f"/api/clips/{clip['id']}", headers=auth_headers).json()
    assert base["status"] == "ready"
    # Trim: first 5 seconds of the rendered clip.
    r = client.post(f"/api/clips/{clip['id']}/trim", headers=auth_headers,
                    json={"start_sec": base["start_sec"], "end_sec": base["start_sec"] + 5.0})
    assert r.status_code == 200, r.text
    trimmed = r.json()
    assert abs(trimmed["duration_sec"] - 5.0) < 0.2
    # Split the base clip in the middle.
    mid = (base["start_sec"] + base["end_sec"]) / 2.0
    r = client.post(f"/api/clips/{clip['id']}/split", headers=auth_headers,
                    json={"at_sec": mid})
    assert r.status_code == 200, r.text
    part_ids = r.json()["clip_ids"]
    assert len(part_ids) == 2
    # Export the base clip with the TikTok preset.
    r = client.post(f"/api/clips/{clip['id']}/export", headers=auth_headers,
                    json={"preset": "tiktok"})
    assert r.status_code == 200, r.text
    assert r.json()["preset"] == "tiktok"
    # Merge the two split parts (wait for their renders first).
    for pid in part_ids:
        for _ in range(120):
            got = client.get(f"/api/clips/{pid}", headers=auth_headers).json()
            if got["status"] in ("ready", "failed"):
                break
            time.sleep(1)
        assert got["status"] == "ready", got
    r = client.post("/api/clips/merge", headers=auth_headers,
                    json={"clip_ids": part_ids, "title": "Merged test"})
    assert r.status_code == 200, r.text
    merged_id = r.json()["id"]
    for _ in range(120):
        got = client.get(f"/api/clips/{merged_id}", headers=auth_headers).json()
        if got["status"] in ("ready", "failed"):
            break
        time.sleep(1)
    assert got["status"] == "ready", got
    assert abs(got["duration_sec"] - base["duration_sec"]) < 1.0


# ---- stats, API keys, billing, accounts ----

def test_stats_log(client, auth_headers, test_video):
    v = _upload_video(client, auth_headers, test_video)
    r = client.post(f"/api/videos/{v['id']}/analyze", headers=auth_headers)
    _wait_task(client, auth_headers, r.json()["task_id"])
    clip = client.get(f"/api/clips?video_id={v['id']}", headers=auth_headers).json()[0]
    r = client.post(f"/api/clips/{clip['id']}/stats", headers=auth_headers,
                    json={"views": 1200, "likes": 80, "comments": 5, "shares": 2, "note": "manual"})
    assert r.status_code == 201, r.text
    totals = client.get(f"/api/clips/{clip['id']}/stats", headers=auth_headers).json()
    assert totals["totals"]["views"] == 1200 and totals["totals"]["likes"] == 80


def test_api_key_create_and_use(client, auth_headers):
    r = client.post("/api/api-keys", headers=auth_headers, json={"name": "test-script"})
    assert r.status_code == 201, r.text
    key = r.json()["api_key"]
    assert key.startswith("cp_")
    # Use the key instead of a JWT.
    r2 = client.get("/api/videos", headers={"X-API-Key": key})
    assert r2.status_code == 200, r2.text
    # Revoked keys stop working.
    client.delete(f"/api/api-keys/{r.json()['id']}", headers=auth_headers)
    assert client.get("/api/videos", headers={"X-API-Key": key}).status_code == 401


def test_billing_limits(client, auth_headers, test_video, monkeypatch):
    # Routers bind PLAN_LIMITS at import, so patch both router attributes.
    limits = {"free": {"videos": 1, "clips": 1}, "pro": {"videos": 100, "clips": 500}}
    monkeypatch.setattr(videos_router, "PLAN_LIMITS", limits)
    monkeypatch.setattr(clips_router, "PLAN_LIMITS", limits)
    _upload_video(client, auth_headers, test_video, title="First")
    with open(test_video, "rb") as f:
        r = client.post("/api/videos/upload", headers=auth_headers, data={"title": "Second"},
                        files={"file": ("test_video.mp4", f, "video/mp4")})
    assert r.status_code == 403, r.text


def test_connected_accounts_and_providers(client, auth_headers):
    acc = client.get("/api/accounts", headers=auth_headers).json()
    assert all(a["status"] == "not_connected" for a in acc["accounts"])
    assert {a["platform"] for a in acc["accounts"]} == {"youtube", "tiktok", "instagram"}
    prov = client.get("/api/api-keys/providers", headers=auth_headers).json()
    assert all(p["status"] == "not_configured" for p in prov["providers"])
    bill = client.get("/api/billing", headers=auth_headers).json()
    assert bill["mock"] is True and bill["plan"] == "free"
    up = client.post("/api/billing/upgrade", headers=auth_headers).json()
    assert up["plan"] == "pro" and up["mock"] is True
