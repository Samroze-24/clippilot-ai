"""ClipPilot AI v1.1 tests — Groq LLM clip selection (mocked) + direct
YouTube publishing (mocked / fail-closed).

No real Groq key and no real Google credentials are used anywhere here:
the LLM is exercised through a mocked httpx response, and YouTube through
mocked upload calls. The only real network-free guarantees asserted are:
LLM picks are validated hard, every LLM failure falls back to the local
scorer, and publishing without credentials fails closed (503).
"""
import json
import os
import subprocess
import time

import httpx
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_clippilot.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import llm as llm_mod  # noqa: E402
from app import publish as publish_mod  # noqa: E402
from app import video as video_mod  # noqa: E402
from app.database import engine as _engine  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import clips as clips_router  # noqa: E402
from app.routers import publish as publish_router  # noqa: E402
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
    up = tmp_path / "uploads"
    cl = tmp_path / "clips"
    br = tmp_path / "broll"
    for d in (up, cl, br):
        d.mkdir()
    monkeypatch.setattr(videos_router, "UPLOAD_DIR", str(up))
    monkeypatch.setattr(videos_router, "CLIP_DIR", str(cl))
    monkeypatch.setattr(clips_router, "CLIP_DIR", str(cl))
    monkeypatch.setattr(clips_router, "BROLL_DIR", str(br))
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


def _upload_srt(client, headers, video_id):
    srt = ("1\n00:00:01,000 --> 00:00:04,000\nHello launch world\n\n"
           "2\n00:00:08,000 --> 00:00:11,000\nPricing tips here\n")
    r = client.post(f"/api/videos/{video_id}/transcript", headers=headers,
                    files={"file": ("subs.srt", srt.encode(), "text/plain")})
    assert r.status_code == 201, r.text
    return r.json()


class FakeResp:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _groq_payload(clips):
    return {"choices": [{"message": {"content": json.dumps({"clips": clips})}}]}


LLM_CLIPS = [
    {"start": 0, "end": 20, "title": "LLM hook one", "reason": "Strong opening."},
    {"start": 30, "end": 50, "title": "LLM hook two", "reason": "Clear payoff."},
    {"start": 60, "end": 85, "title": "LLM hook three", "reason": "Best takeaway."},
]


# ---- LLM validation (pure) ----

def test_llm_validate_ok():
    clips = llm_mod.validate_llm_clips({"clips": LLM_CLIPS}, 120.0)
    assert len(clips) == 3
    assert clips[0]["title"] == "LLM hook one"
    assert clips[0]["start"] == 0 and clips[0]["end"] == 20
    # Bare list is accepted too.
    assert len(llm_mod.validate_llm_clips(LLM_CLIPS, 120.0)) == 3


def test_llm_validate_rejects_bad():
    # Out of range / wrong length / non-numeric / empty -> ValueError.
    with pytest.raises(ValueError):
        llm_mod.validate_llm_clips({"clips": [{"start": -5, "end": 10, "title": "x"}]}, 120.0)
    with pytest.raises(ValueError):
        llm_mod.validate_llm_clips({"clips": [{"start": 0, "end": 10, "title": "x"}]}, 120.0)
    with pytest.raises(ValueError):
        llm_mod.validate_llm_clips({"clips": [{"start": 0, "end": 70, "title": "x"}]}, 120.0)
    with pytest.raises(ValueError):
        llm_mod.validate_llm_clips({"clips": [{"start": 110, "end": 130, "title": "x"}]}, 120.0)
    with pytest.raises(ValueError):
        llm_mod.validate_llm_clips({"clips": []}, 120.0)
    with pytest.raises(ValueError):
        llm_mod.validate_llm_clips({"nope": []}, 120.0)
    # Overlapping duplicates are deduped (keeps the first), never both.
    dup = [
        {"start": 0, "end": 20, "title": "A", "reason": ""},
        {"start": 5, "end": 25, "title": "B", "reason": ""},
        {"start": 40, "end": 60, "title": "C", "reason": ""},
    ]
    kept = llm_mod.validate_llm_clips({"clips": dup}, 120.0)
    assert [c["title"] for c in kept] == ["A", "C"]
    # At most 7 clips survive.
    many = [{"start": i * 16, "end": i * 16 + 15, "title": f"C{i}", "reason": ""}
            for i in range(10)]
    assert len(llm_mod.validate_llm_clips({"clips": many}, 200.0)) == 7


# ---- LLM selection with mocked httpx ----

def test_llm_not_configured(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    clips, detail = llm_mod.select_clips_llm(
        [{"text": "hello", "start": 0.0, "end": 2.0}], 120.0)
    assert clips is None and detail == "not_configured"


def test_llm_mocked_success(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LLM_API_KEY", "test-key-not-real")
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(url)
        assert "Authorization" in headers
        assert headers["Authorization"].startswith("Bearer ")
        # The key must never appear in the URL or the request body.
        # (The parameter is named `json`, so use str() here, not json.dumps.)
        assert "test-key-not-real" not in url
        assert "test-key-not-real" not in str(json)
        return FakeResp(200, _groq_payload(LLM_CLIPS))

    monkeypatch.setattr(llm_mod.httpx, "post", fake_post)
    segs = [{"text": "hello world launch", "start": 1.0, "end": 4.0},
            {"text": "pricing tips here", "start": 8.0, "end": 11.0}]
    clips, detail = llm_mod.select_clips_llm(segs, 120.0)
    assert clips is not None and len(clips) == 3
    assert clips[1]["title"] == "LLM hook two"
    assert detail.startswith("ok")
    assert calls and calls[0].endswith("/chat/completions")


def test_llm_retries_without_json_mode_on_400(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LLM_API_KEY", "test-key-not-real")
    seen = {"n": 0}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen["n"] += 1
        if seen["n"] == 1:
            assert "response_format" in json
            return FakeResp(400, {"error": {"message": "response_format unsupported"}})
        assert "response_format" not in json
        return FakeResp(200, _groq_payload(LLM_CLIPS[:1]))

    monkeypatch.setattr(llm_mod.httpx, "post", fake_post)
    clips, detail = llm_mod.select_clips_llm(
        [{"text": "hello", "start": 0.0, "end": 2.0}], 120.0)
    assert clips is not None and len(clips) == 1
    assert seen["n"] == 2


def test_llm_mocked_failure_falls_back(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LLM_API_KEY", "test-key-not-real")

    def fake_post(url, json=None, headers=None, timeout=None):
        raise httpx.ConnectError("boom", request=httpx.Request("POST", url))

    monkeypatch.setattr(llm_mod.httpx, "post", fake_post)
    clips, detail = llm_mod.select_clips_llm(
        [{"text": "hello", "start": 0.0, "end": 2.0}], 120.0)
    assert clips is None
    assert "failed" in detail


def test_llm_mocked_bad_output_falls_back(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LLM_API_KEY", "test-key-not-real")
    monkeypatch.setattr(llm_mod.httpx, "post",
                        lambda url, json=None, headers=None, timeout=None:
                        FakeResp(200, {"choices": [{"message": {"content": "not json at all"}}]}))
    clips, detail = llm_mod.select_clips_llm(
        [{"text": "hello", "start": 0.0, "end": 2.0}], 120.0)
    assert clips is None
    assert detail.startswith("invalid_output")


def test_llm_mocked_401_falls_back(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LLM_API_KEY", "bad-key-not-real")
    monkeypatch.setattr(llm_mod.httpx, "post",
                        lambda url, json=None, headers=None, timeout=None:
                        FakeResp(401, {"error": {"message": "Incorrect API key"}}))
    clips, detail = llm_mod.select_clips_llm(
        [{"text": "hello", "start": 0.0, "end": 2.0}], 120.0)
    assert clips is None
    assert "401" in detail


# ---- analyze flow: selector is honest ----

def test_analyze_selector_local_when_llm_unconfigured(client, auth_headers, test_video):
    v = _upload_video(client, auth_headers, test_video)
    r = client.post(f"/api/videos/{v['id']}/analyze", headers=auth_headers)
    t = _wait_task(client, auth_headers, r.json()["task_id"])
    assert t["status"] == "done", t
    assert t["result"]["selector"] == "local"
    assert "Local" in t["result"]["selector_detail"]


def test_analyze_selector_llm_with_mock(client, auth_headers, test_video, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LLM_API_KEY", "test-key-not-real")
    # 20s video: the two mock picks overlap, validation keeps the first.
    mock_clips = [
        {"start": 0, "end": 15, "title": "LLM hook one", "reason": "Strong opening."},
        {"start": 5, "end": 20, "title": "LLM hook two", "reason": "Great payoff."},
    ]
    monkeypatch.setattr(llm_mod.httpx, "post",
                        lambda url, json=None, headers=None, timeout=None:
                        FakeResp(200, _groq_payload(mock_clips)))
    v = _upload_video(client, auth_headers, test_video)
    _upload_srt(client, auth_headers, v["id"])
    r = client.post(f"/api/videos/{v['id']}/analyze", headers=auth_headers)
    t = _wait_task(client, auth_headers, r.json()["task_id"])
    assert t["status"] == "done", t
    assert t["result"]["selector"] == "llm", t["result"]
    assert "Groq" in t["result"]["selector_detail"]
    clips = client.get(f"/api/clips?video_id={v['id']}", headers=auth_headers).json()
    assert len(clips) >= 1
    assert clips[0]["hook_title"] == "LLM hook one"
    assert "Strong opening" in clips[0]["reason"]


def test_analyze_llm_failure_falls_back_to_local(client, auth_headers, test_video, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LLM_API_KEY", "test-key-not-real")

    def fake_post(url, json=None, headers=None, timeout=None):
        raise httpx.ConnectError("boom", request=httpx.Request("POST", url))

    monkeypatch.setattr(llm_mod.httpx, "post", fake_post)
    v = _upload_video(client, auth_headers, test_video)
    _upload_srt(client, auth_headers, v["id"])
    r = client.post(f"/api/videos/{v['id']}/analyze", headers=auth_headers)
    t = _wait_task(client, auth_headers, r.json()["task_id"])
    assert t["status"] == "done", t
    assert t["result"]["selector"] == "local", t["result"]
    assert "fell back" in t["result"]["selector_detail"]
    clips = client.get(f"/api/clips?video_id={v['id']}", headers=auth_headers).json()
    assert len(clips) >= 1


# ---- publishing: fail-closed without credentials ----

def test_publish_status_not_configured(client, auth_headers, monkeypatch):
    monkeypatch.delenv("YOUTUBE_CLIENT_ID", raising=False)
    monkeypatch.delenv("YOUTUBE_CLIENT_SECRET", raising=False)
    s = client.get("/api/publish/status", headers=auth_headers).json()
    assert s["youtube"]["configured"] is False
    assert s["youtube"]["connected"] is False
    assert s["tiktok"]["status"] == "not_configured"
    assert s["instagram"]["status"] == "not_configured"
    assert s["tiktok"]["steps"] and s["instagram"]["steps"]


def test_youtube_auth_url_not_configured_503(client, auth_headers, monkeypatch):
    monkeypatch.delenv("YOUTUBE_CLIENT_ID", raising=False)
    monkeypatch.delenv("YOUTUBE_CLIENT_SECRET", raising=False)
    r = client.get("/api/publish/youtube/auth-url", headers=auth_headers)
    assert r.status_code == 503, r.text
    assert "not configured" in r.text.lower()


def test_youtube_upload_fails_closed_without_credentials(client, auth_headers, test_video):
    v = _upload_video(client, auth_headers, test_video)
    r = client.post(f"/api/videos/{v['id']}/analyze", headers=auth_headers)
    _wait_task(client, auth_headers, r.json()["task_id"])
    clip = client.get(f"/api/clips?video_id={v['id']}", headers=auth_headers).json()[0]
    r = client.post("/api/publish/youtube/upload", headers=auth_headers,
                    json={"clip_id": clip["id"], "title": "Test", "privacy": "private"})
    assert r.status_code == 503, r.text
    assert "not configured" in r.text.lower()


def test_tiktok_instagram_upload_503(client, auth_headers):
    for platform in ("tiktok", "instagram"):
        r = client.post(f"/api/publish/{platform}/upload", headers=auth_headers, json={})
        assert r.status_code == 503, (platform, r.text)
        assert "not configured" in r.text.lower()


# ---- publishing: OAuth URL building + token refresh (mocked) ----

def test_youtube_auth_url_build_and_state(monkeypatch):
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "test-client-secret")
    url, state = publish_mod.build_auth_url(user_id=7)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth")
    assert "client_id=test-client-id" in url
    assert "youtube.upload" in url
    assert "state=" + state in url
    assert publish_mod.consume_state(state) == 7
    assert publish_mod.consume_state(state) is None  # one-time use


def test_youtube_token_refresh_mocked(tmp_path, monkeypatch):
    token_path = tmp_path / "youtube_token.json"
    monkeypatch.setattr(publish_mod, "TOKEN_PATH", str(token_path))
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "test-client-secret")
    publish_mod.save_tokens({
        "access_token": "old-access", "refresh_token": "rt-123",
        "token_type": "Bearer", "scope": "",
        "expires_at": time.time() - 100,  # expired
    })

    def fake_post(url, data=None, headers=None, timeout=None):
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "rt-123"
        assert "test-client-secret" in str(data.values())
        return FakeResp(200, {"access_token": "new-access", "expires_in": 3600})

    monkeypatch.setattr(publish_mod.httpx, "post", fake_post)
    token = publish_mod.get_valid_access_token()
    assert token == "new-access"
    saved = json.loads(token_path.read_text())
    assert saved["access_token"] == "new-access"
    assert saved["refresh_token"] == "rt-123"


def test_youtube_upload_mocked_success(client, auth_headers, test_video, monkeypatch):
    # Configured + connected (mocked), real rendered clip, mocked upload call.
    monkeypatch.setattr(publish_router, "youtube_configured", lambda: True)
    monkeypatch.setattr(publish_router, "youtube_connected", lambda: True)
    monkeypatch.setattr(
        publish_router, "upload_to_youtube",
        lambda path, title, description, privacy="private": {
            "video_id": "dQw4w9WgXcQ",
            "watch_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "title": title, "privacy": privacy,
        })
    v = _upload_video(client, auth_headers, test_video)
    r = client.post(f"/api/videos/{v['id']}/analyze", headers=auth_headers)
    _wait_task(client, auth_headers, r.json()["task_id"])
    clip = client.get(f"/api/clips?video_id={v['id']}", headers=auth_headers).json()[0]
    r = client.post(f"/api/clips/{clip['id']}/generate", headers=auth_headers,
                    json={"caption_template": "bold-yellow", "preset": "default",
                          "include_hook_title": False})
    _wait_task(client, auth_headers, r.json()["task_id"], timeout=240)
    got = client.get(f"/api/clips/{clip['id']}", headers=auth_headers).json()
    assert got["status"] == "ready"
    r = client.post("/api/publish/youtube/upload", headers=auth_headers,
                    json={"clip_id": clip["id"], "title": "Mock publish",
                          "description": "test", "privacy": "private"})
    assert r.status_code == 200, r.text
    t = _wait_task(client, auth_headers, r.json()["task_id"])
    assert t["status"] == "done", t
    assert t["result"]["video_id"] == "dQw4w9WgXcQ"
    assert t["result"]["watch_url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_youtube_upload_bad_privacy_400(client, auth_headers, monkeypatch):
    monkeypatch.setattr(publish_router, "youtube_configured", lambda: True)
    monkeypatch.setattr(publish_router, "youtube_connected", lambda: True)
    r = client.post("/api/publish/youtube/upload", headers=auth_headers,
                    json={"clip_id": 999, "privacy": "everyone"})
    assert r.status_code == 400, r.text


def test_youtube_disconnect(client, auth_headers, tmp_path, monkeypatch):
    monkeypatch.setattr(publish_mod, "TOKEN_PATH", str(tmp_path / "youtube_token.json"))
    publish_mod.save_tokens({"access_token": "x", "refresh_token": "y",
                             "expires_at": time.time() + 3600})
    r = client.post("/api/publish/youtube/disconnect", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["disconnected"] is True
    assert not os.path.exists(str(tmp_path / "youtube_token.json"))
