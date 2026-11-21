"""ClipPilot AI v1.3 tests — full-fledged features (MCP server, Pexels B-roll,
AssemblyAI transcription option, Twitch VOD import, TikTok + Instagram
publishing).

No real provider keys and no real network are used anywhere here: every
provider is exercised through mocked httpx responses or a local stub, and
the MCP server is exercised through its real stdio subprocess. The only
guarantees asserted are: the real code paths run, failures fail closed
(503) with clear steps, and nothing is ever faked as a successful post,
transcript, search result or download.
"""
import json
import os
import subprocess
import sys
import time

import httpx
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_clippilot.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import mcp as mcp_mod  # noqa: E402
from app import pexels as pexels_mod  # noqa: E402
from app import publish as publish_mod  # noqa: E402
from app import transcribe as transcribe_mod  # noqa: E402
from app import twitch as twitch_mod  # noqa: E402
from app import video as video_mod  # noqa: E402
from app.database import SessionLocal, engine as _engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Clip, Video  # noqa: E402
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
        time.sleep(0.5)
    raise AssertionError(f"task {task_id} did not finish in {timeout}s")


class _FakeResp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    @property
    def headers(self):
        return {}


def _upload_video(client, headers, test_video):
    with open(test_video, "rb") as f:
        r = client.post("/api/videos/upload", headers=headers,
                        data={"title": "Test video"},
                        files={"file": ("test.mp4", f, "video/mp4")})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _ready_clip(test_video):
    """A ready clip row pointing at the test video file (for publish tests)."""
    db = SessionLocal()
    try:
        video = Video(title="Ready video", source_type="upload",
                      filename="test.mp4", stored_path=test_video,
                      duration_sec=20.0, size_bytes=1000, width=640, height=360,
                      status="analyzed")
        db.add(video)
        db.commit()
        db.refresh(video)
        clip = Clip(video_id=video.id, title="Clip 1", hook_title="Hook",
                    start_sec=0.0, end_sec=15.0, duration_sec=15.0, score=80.0,
                    breakdown_json="{}", reason="test", status="ready",
                    file_path=test_video)
        db.add(clip)
        db.commit()
        db.refresh(clip)
        return clip.id
    finally:
        db.close()


# ============================ MCP server ============================

def test_mcp_initialize_and_tools_list():
    resp = mcp_mod.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert resp["result"]["protocolVersion"] == "2024-11-05"
    assert resp["result"]["serverInfo"]["name"] == "clippilot-ai"
    resp = mcp_mod.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in resp["result"]["tools"]]
    assert names == ["list_videos", "analyze_video", "list_clips", "render_clip",
                     "get_clip", "youtube_status", "publish_to_youtube"]
    resp = mcp_mod.handle_request({"jsonrpc": "2.0", "id": 3, "method": "ping"})
    assert resp["result"] == {}
    assert mcp_mod.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    resp = mcp_mod.handle_request({"jsonrpc": "2.0", "id": 4, "method": "resources/list"})
    assert resp["error"]["code"] == -32601


def test_mcp_list_videos_and_get_clip(client):
    resp = mcp_mod.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "list_videos", "arguments": {}}})
    assert resp["result"]["isError"] is False
    videos = json.loads(resp["result"]["content"][0]["text"])["videos"]
    assert len(videos) >= 1  # seeded SAMPLE videos
    resp = mcp_mod.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                   "params": {"name": "get_clip", "arguments": {"clip_id": 999999}}})
    assert resp["result"]["isError"] is True
    resp = mcp_mod.handle_request({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                   "params": {"name": "nope", "arguments": {}}})
    assert resp["error"]["code"] == -32602


def test_mcp_youtube_status():
    resp = mcp_mod.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "youtube_status", "arguments": {}}})
    data = json.loads(resp["result"]["content"][0]["text"])
    assert data == {"configured": False, "connected": False}


def test_mcp_analyze_and_render_real_video(client, auth_headers, test_video):
    video_id = _upload_video(client, auth_headers, test_video)
    resp = mcp_mod.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "analyze_video",
                                              "arguments": {"video_id": video_id}}})
    assert resp["result"]["isError"] is False, resp
    result = json.loads(resp["result"]["content"][0]["text"])
    assert result["video_id"] == video_id
    assert result["clip_ids"]
    clip_id = result["clip_ids"][0]
    resp = mcp_mod.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                   "params": {"name": "render_clip",
                                              "arguments": {"clip_id": clip_id}}})
    assert resp["result"]["isError"] is False, resp
    rendered = json.loads(resp["result"]["content"][0]["text"])
    assert rendered["clip_id"] == clip_id
    assert rendered["file"].endswith(".mp4")
    resp = mcp_mod.handle_request({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                   "params": {"name": "get_clip", "arguments": {"clip_id": clip_id}}})
    clip = json.loads(resp["result"]["content"][0]["text"])["clip"]
    assert clip["status"] == "ready"


def test_mcp_subprocess_smoke(tmp_path):
    """Real stdio subprocess: initialize + tools/list + list_videos."""
    db_path = tmp_path / "mcp_test.db"
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path}"
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.mcp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, cwd=os.getcwd(), env=env)
    try:
        import select as _select

        def send(obj):
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()
            ready, _, _ = _select.select([proc.stdout], [], [], 30)
            assert ready, f"MCP server did not respond to {obj.get('method')} within 30s"
            line = proc.stdout.readline()
            assert line, "MCP server produced no response"
            return json.loads(line)
        r1 = send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert r1["result"]["serverInfo"]["name"] == "clippilot-ai"
        # Notifications get NO response — write only, never read.
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()
        r2 = send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert len(r2["result"]["tools"]) == 7
        r3 = send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                   "params": {"name": "list_videos", "arguments": {}}})
        assert r3["result"]["isError"] is False
        assert json.loads(r3["result"]["content"][0]["text"])["videos"] == []
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


# ============================ Pexels ============================

def test_pexels_search_not_configured(monkeypatch):
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    with pytest.raises(pexels_mod.PexelsError, match="not configured"):
        pexels_mod.search_pexels("city night")
    with pytest.raises(pexels_mod.PexelsError, match="not configured"):
        pexels_mod.download_pexels_file(
            "https://videos.pexels.com/video-files/1/1-hd.mp4", "/tmp/x.mp4")


def test_pexels_search_mocked(monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "test-key")
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update(url=url, params=params, headers=headers)
        return _FakeResp(200, {"videos": [
            {"id": 111, "width": 720, "height": 1280, "duration": 12,
             "url": "https://www.pexels.com/video/111/",
             "image": "https://images.pexels.com/111.jpg",
             "video_files": [
                 {"link": "https://videos.pexels.com/video-files/111/111-hd.mp4",
                  "width": 720, "height": 1280, "quality": "hd", "file_type": "video/mp4"},
                 {"link": "https://videos.pexels.com/video-files/111/111-sd.mp4",
                  "width": 360, "height": 640, "quality": "sd", "file_type": "video/mp4"}]},
            {"id": 222, "width": 720, "height": 1280, "duration": 8,
             "url": "https://www.pexels.com/video/222/",
             "image": "", "video_files": []},  # no files -> skipped
        ]})
    monkeypatch.setattr(pexels_mod.httpx, "get", fake_get)
    results = pexels_mod.search_pexels("city night", per_page=6)
    assert seen["params"]["orientation"] == "portrait"
    assert seen["params"]["query"] == "city night"
    assert seen["headers"]["Authorization"] == "test-key"
    assert len(results) == 1
    assert results[0]["id"] == 111
    assert len(results[0]["files"]) == 2


def test_pexels_search_401(monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "bad-key")
    monkeypatch.setattr(pexels_mod.httpx, "get",
                        lambda *a, **k: _FakeResp(401, {"error": "Unauthorized"}))
    with pytest.raises(pexels_mod.PexelsError, match="key rejected"):
        pexels_mod.search_pexels("city")


def test_pexels_download_mocked_and_host_check(client, auth_headers, tmp_path, monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "test-key")

    class FakeStream:
        def __init__(self, *a, **k):
            self.status_code = 200
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def iter_bytes(self, n):
            yield b"fake-video-bytes"

    monkeypatch.setattr(pexels_mod.httpx, "stream", FakeStream)
    dest = str(tmp_path / "pexels_111_abc.mp4")
    out = pexels_mod.download_pexels_file(
        "https://videos.pexels.com/video-files/111/111-hd.mp4", dest)
    assert out == dest
    assert open(dest, "rb").read() == b"fake-video-bytes"
    # Any other host is refused (no arbitrary URL fetching).
    with pytest.raises(pexels_mod.PexelsError, match="not a Pexels"):
        pexels_mod.download_pexels_file("https://evil.example.com/x.mp4", dest)


def test_pexels_endpoints_503_without_key(client, auth_headers):
    r = client.get("/api/clips/pexels/search?q=city", headers=auth_headers)
    assert r.status_code == 503
    assert "Not configured" in r.text or "not configured" in r.text
    r = client.post("/api/clips/pexels/download", headers=auth_headers,
                    json={"video_id": 111,
                          "file_link": "https://videos.pexels.com/video-files/111/111-hd.mp4"})
    assert r.status_code == 503


def test_pexels_download_bad_link_400(client, auth_headers, monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "test-key")
    r = client.post("/api/clips/pexels/download", headers=auth_headers,
                    json={"video_id": 111, "file_link": "https://evil.example.com/x.mp4"})
    assert r.status_code == 400


# ============================ AssemblyAI ============================

def test_assemblyai_words_to_segments():
    words = [
        {"word": "hello", "start": 0.0, "end": 0.4},
        {"word": "world", "start": 0.5, "end": 0.9},
        {"word": "again", "start": 2.5, "end": 2.9},  # 1.6s gap -> new segment
        {"word": "here", "start": 3.0, "end": 3.4},
    ]
    segs = transcribe_mod._words_to_segments(words)
    assert len(segs) == 2
    assert segs[0]["text"] == "hello world"
    assert segs[1]["text"] == "again here"


def test_assemblyai_not_configured(monkeypatch):
    monkeypatch.delenv("ASSEMBLYAI_API_KEY", raising=False)
    with pytest.raises(transcribe_mod.AssemblyAIError, match="not configured"):
        transcribe_mod.transcribe_assemblyai("/tmp/x.mp4")


def test_assemblyai_mocked_success(monkeypatch, tmp_path):
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "test-key")
    audio = tmp_path / "a.mp4"
    audio.write_bytes(b"fake-audio")
    calls = {"polls": 0}

    def fake_post(url, content=None, json=None, headers=None, timeout=None):
        if url.endswith("/upload"):
            assert headers["authorization"] == "test-key"
            return _FakeResp(200, {"upload_url": "https://cdn.assemblyai.com/audio.mp4"})
        if url.endswith("/transcript"):
            return _FakeResp(200, {"id": "tr-1"})
        raise AssertionError(f"unexpected POST {url}")

    def fake_get(url, headers=None, timeout=None):
        calls["polls"] += 1
        if calls["polls"] == 1:
            return _FakeResp(200, {"status": "processing"})
        return _FakeResp(200, {
            "status": "completed", "language_code": "en",
            "words": [
                {"text": "hello", "start": 0, "end": 400},
                {"text": "world", "start": 500, "end": 900},
                {"text": "again", "start": 2500, "end": 2900},
            ]})
    monkeypatch.setattr(transcribe_mod.httpx, "post", fake_post)
    monkeypatch.setattr(transcribe_mod.httpx, "get", fake_get)
    result = transcribe_mod.transcribe_assemblyai(str(audio), poll_interval=0)
    assert result["language"] == "en"
    assert [w["word"] for w in result["words"]] == ["hello", "world", "again"]
    assert result["words"][0]["start"] == 0.0  # ms -> seconds
    assert result["words"][0]["end"] == 0.4
    assert len(result["segments"]) == 2


def test_assemblyai_401_and_error_status(monkeypatch, tmp_path):
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "bad-key")
    audio = tmp_path / "a.mp4"
    audio.write_bytes(b"x")
    monkeypatch.setattr(transcribe_mod.httpx, "post",
                        lambda *a, **k: _FakeResp(401, {"error": "bad key"}))
    with pytest.raises(transcribe_mod.AssemblyAIError, match="key rejected"):
        transcribe_mod.transcribe_assemblyai(str(audio), poll_interval=0)
    # Error status after a successful create.
    monkeypatch.setattr(transcribe_mod.httpx, "post",
                        lambda url, **k: _FakeResp(
                            200, {"upload_url": "https://x/y"} if url.endswith("/upload")
                            else {"id": "tr-2"}))
    monkeypatch.setattr(transcribe_mod.httpx, "get",
                        lambda *a, **k: _FakeResp(
                            200, {"status": "error", "error": "audio too short"}))
    with pytest.raises(transcribe_mod.AssemblyAIError, match="audio too short"):
        transcribe_mod.transcribe_assemblyai(str(audio), poll_interval=0)


def test_transcribe_endpoint_provider_checks(client, auth_headers, test_video):
    video_id = _upload_video(client, auth_headers, test_video)
    # assemblyai provider without a key -> 503, fail-closed.
    import os as _os
    _os.environ["TRANSCRIPTION_PROVIDER"] = "assemblyai"
    _os.environ.pop("ASSEMBLYAI_API_KEY", None)
    try:
        r = client.post(f"/api/videos/{video_id}/transcribe", headers=auth_headers)
        assert r.status_code == 503
        assert "AssemblyAI" in r.text
        # Unknown provider -> 400.
        _os.environ["TRANSCRIPTION_PROVIDER"] = "watson"
        r = client.post(f"/api/videos/{video_id}/transcribe", headers=auth_headers)
        assert r.status_code == 400
    finally:
        _os.environ.pop("TRANSCRIPTION_PROVIDER", None)


def test_transcribe_endpoint_assemblyai_mocked(client, auth_headers, test_video, monkeypatch):
    video_id = _upload_video(client, auth_headers, test_video)
    monkeypatch.setenv("TRANSCRIPTION_PROVIDER", "assemblyai")
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "test-key")

    def fake_transcribe(path, **k):
        return {"segments": [{"text": "hello world", "start": 0.0, "end": 0.9}],
                "words": [{"word": "hello", "start": 0.0, "end": 0.4},
                          {"word": "world", "start": 0.5, "end": 0.9}],
                "language": "en"}
    monkeypatch.setattr(videos_router, "transcribe_assemblyai", fake_transcribe)
    r = client.post(f"/api/videos/{video_id}/transcribe", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["provider"] == "assemblyai"
    task = _wait_task(client, auth_headers, r.json()["task_id"])
    assert task["status"] == "done", task
    assert task["result"]["provider"] == "assemblyai"
    tr = client.get(f"/api/videos/{video_id}/transcript", headers=auth_headers)
    assert tr.status_code == 200
    assert tr.json()["source"] == "assemblyai"
    assert tr.json()["words"][0]["word"] == "hello"


# ============================ Twitch ============================

def test_twitch_not_configured(monkeypatch):
    monkeypatch.delenv("TWITCH_CLIENT_ID", raising=False)
    monkeypatch.delenv("TWITCH_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(twitch_mod, "_token_cache", {"token": "", "expires_at": 0.0})
    with pytest.raises(twitch_mod.TwitchError, match="not configured"):
        twitch_mod.list_vods("shroud")


def _twitch_fakes(monkeypatch, user_data=None, vods=None):
    monkeypatch.setenv("TWITCH_CLIENT_ID", "cid")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "csecret")
    monkeypatch.setattr(twitch_mod, "_token_cache", {"token": "", "expires_at": 0.0})

    def fake_post(url, params=None, timeout=None):
        assert params["grant_type"] == "client_credentials"
        return _FakeResp(200, {"access_token": "app-token", "expires_in": 3600})
    def fake_get(url, params=None, headers=None, timeout=None):
        assert headers["Client-Id"] == "cid"
        if url.endswith("/users"):
            return _FakeResp(200, {"data": user_data if user_data is not None else
                                   [{"id": "123", "display_name": "Shroud"}]})
        if url.endswith("/videos"):
            return _FakeResp(200, {"data": vods if vods is not None else [
                {"id": "v1", "title": "Stream 1", "url": "https://www.twitch.tv/videos/111",
                 "duration": "2h3m", "created_at": "2026-11-20T10:00:00Z",
                 "view_count": 5000, "language": "en"},
                {"id": "v2", "title": "Stream 2", "url": "https://www.twitch.tv/videos/222",
                 "duration": "1h10m", "created_at": "2026-11-19T10:00:00Z",
                 "view_count": 3000, "language": "en"}]})
        raise AssertionError(f"unexpected GET {url}")
    monkeypatch.setattr(twitch_mod.httpx, "post", fake_post)
    monkeypatch.setattr(twitch_mod.httpx, "get", fake_get)


def test_twitch_list_vods_mocked(monkeypatch):
    _twitch_fakes(monkeypatch)
    vods = twitch_mod.list_vods("shroud", first=2)
    assert len(vods) == 2
    assert vods[0]["url"] == "https://www.twitch.tv/videos/111"
    assert vods[0]["channel"] == "Shroud"
    assert vods[0]["view_count"] == 5000


def test_twitch_channel_not_found(monkeypatch):
    _twitch_fakes(monkeypatch, user_data=[])
    with pytest.raises(twitch_mod.TwitchError, match="not found"):
        twitch_mod.list_vods("no-such-channel")


def test_twitch_endpoints_503_without_creds(client, auth_headers, monkeypatch):
    monkeypatch.delenv("TWITCH_CLIENT_ID", raising=False)
    monkeypatch.delenv("TWITCH_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(twitch_mod, "_token_cache", {"token": "", "expires_at": 0.0})
    r = client.get("/api/videos/twitch/vods?channel=shroud", headers=auth_headers)
    assert r.status_code == 503
    assert "Twitch" in r.text


def test_twitch_vods_endpoint_mocked(client, auth_headers, monkeypatch):
    _twitch_fakes(monkeypatch)
    r = client.get("/api/videos/twitch/vods?channel=shroud", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["vods"]) == 2
    assert "Live streams are not imported" in body["note"]


def test_twitch_import_endpoint(client, auth_headers, monkeypatch):
    r = client.post("/api/videos/twitch/import", headers=auth_headers,
                    json={"vod_url": "https://www.youtube.com/watch?v=x"})
    assert r.status_code == 400  # must be a twitch.tv/videos URL
    monkeypatch.setattr(videos_router, "download_url",
                        lambda url, out_dir, **k: {"path": "/tmp/nope.mp4",
                                                   "title": "VOD", "duration": 10,
                                                   "ext": "mp4"})
    # _do_download probes the file afterwards; make probe return a dummy.
    monkeypatch.setattr(videos_router, "probe_video",
                        lambda path: {"duration": 10.0, "width": 640, "height": 360})
    import os as _os
    orig_getsize = _os.path.getsize
    monkeypatch.setattr(_os.path, "getsize", lambda p: 1000)
    monkeypatch.setattr(_os.path, "exists", lambda p: True)
    r = client.post("/api/videos/twitch/import", headers=auth_headers,
                    json={"vod_url": "https://www.twitch.tv/videos/111", "title": "VOD 1"})
    assert r.status_code == 200, r.text
    task = _wait_task(client, auth_headers, r.json()["task_id"])
    assert task["status"] == "done", task
    assert task["result"]["video_id"]


# ============================ TikTok ============================

def test_tiktok_auth_url_not_configured(client, auth_headers, monkeypatch):
    monkeypatch.delenv("TIKTOK_CLIENT_KEY", raising=False)
    monkeypatch.delenv("TIKTOK_CLIENT_SECRET", raising=False)
    r = client.get("/api/publish/tiktok/auth-url", headers=auth_headers)
    assert r.status_code == 503
    assert "TikTok" in r.text


def test_tiktok_auth_url_mocked(monkeypatch):
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "ckey")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "csecret")
    url, state = publish_mod.build_tiktok_auth_url(user_id=1)
    assert "tiktok.com" in url
    assert "client_key=ckey" in url
    assert "video.upload" in url
    assert publish_mod.consume_tiktok_state(state) == 1
    assert publish_mod.consume_tiktok_state(state) is None  # one-time use


def test_tiktok_upload_mocked(monkeypatch, tmp_path):
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "ckey")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "csecret")
    monkeypatch.setattr(publish_mod, "TIKTOK_TOKEN_PATH", str(tmp_path / "tiktok_token.json"))
    publish_mod.save_tiktok_tokens({"access_token": "tok", "refresh_token": "ref",
                                    "expires_at": time.time() + 3600})
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake-clip-bytes")
    seen = {}

    def fake_post(url, json=None, data=None, headers=None, timeout=None):
        if url.endswith("/video/init/"):
            seen["init"] = json
            assert headers["Authorization"] == "Bearer tok"
            return _FakeResp(200, {"data": {"publish_id": "pub-1",
                                            "upload_url": "https://upload.tiktok.com/put"}})
        if url.endswith("/status/fetch/"):
            assert json["publish_id"] == "pub-1"
            return _FakeResp(200, {"data": {"status": "PROCESSING"}})
        raise AssertionError(f"unexpected POST {url}")

    def fake_put(url, content=None, headers=None, timeout=None):
        seen["put"] = {"url": url, "bytes": content,
                       "range": headers.get("Content-Range")}
        return _FakeResp(200, {})
    monkeypatch.setattr(publish_mod.httpx, "post", fake_post)
    monkeypatch.setattr(publish_mod.httpx, "put", fake_put)
    result = publish_mod.upload_to_tiktok(str(clip), "My clip", "SELF_ONLY")
    assert result["publish_id"] == "pub-1"
    assert result["status"] == "PROCESSING"
    assert result["privacy"] == "SELF_ONLY"
    assert seen["init"]["source_info"]["video_size"] == len(b"fake-clip-bytes")
    assert seen["init"]["post_info"]["privacy_level"] == "SELF_ONLY"
    assert seen["put"]["range"] == "bytes 0-14/15"


def test_tiktok_upload_not_connected(monkeypatch, tmp_path):
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "ckey")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "csecret")
    monkeypatch.setattr(publish_mod, "TIKTOK_TOKEN_PATH", str(tmp_path / "tiktok_token.json"))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    with pytest.raises(publish_mod.PublishError, match="not connected"):
        publish_mod.upload_to_tiktok(str(clip), "t", "SELF_ONLY")


def test_tiktok_router_upload_503s_and_mocked_success(client, auth_headers, test_video, monkeypatch):
    clip_id = _ready_clip(test_video)
    # Not configured at all.
    monkeypatch.delenv("TIKTOK_CLIENT_KEY", raising=False)
    r = client.post("/api/publish/tiktok/upload", headers=auth_headers,
                    json={"clip_id": clip_id, "privacy": "SELF_ONLY"})
    assert r.status_code == 503
    # Configured but not connected.
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "ckey")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "csecret")
    monkeypatch.setattr(publish_router, "tiktok_connected", lambda: False)
    r = client.post("/api/publish/tiktok/upload", headers=auth_headers,
                    json={"clip_id": clip_id, "privacy": "SELF_ONLY"})
    assert r.status_code == 503
    assert "not connected" in r.text
    # Connected + mocked upload -> task done with a publish id.
    monkeypatch.setattr(publish_router, "tiktok_connected", lambda: True)
    monkeypatch.setattr(publish_router, "upload_to_tiktok",
                        lambda path, title, privacy: {"publish_id": "pub-9",
                                                      "status": "PROCESSING",
                                                      "title": title, "privacy": privacy,
                                                      "note": "processing"})
    r = client.post("/api/publish/tiktok/upload", headers=auth_headers,
                    json={"clip_id": clip_id, "title": "Hi", "privacy": "SELF_ONLY"})
    assert r.status_code == 200, r.text
    task = _wait_task(client, auth_headers, r.json()["task_id"])
    assert task["status"] == "done", task
    assert task["result"]["publish_id"] == "pub-9"
    assert task["result"]["platform"] == "tiktok"
    # Bad privacy -> 400.
    r = client.post("/api/publish/tiktok/upload", headers=auth_headers,
                    json={"clip_id": clip_id, "privacy": "EVERYONE"})
    assert r.status_code == 400


def test_tiktok_disconnect_clears_tokens(client, auth_headers, tmp_path, monkeypatch):
    monkeypatch.setattr(publish_mod, "TIKTOK_TOKEN_PATH", str(tmp_path / "tiktok_token.json"))
    publish_mod.save_tiktok_tokens({"access_token": "tok", "refresh_token": "ref",
                                    "expires_at": time.time() + 3600})
    r = client.post("/api/publish/tiktok/disconnect", headers=auth_headers)
    assert r.status_code == 200
    assert publish_mod.load_tiktok_tokens() is None


# ============================ Instagram ============================

def test_instagram_not_configured(monkeypatch):
    monkeypatch.delenv("INSTAGRAM_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("INSTAGRAM_USER_ID", raising=False)
    with pytest.raises(publish_mod.PublishError, match="not configured"):
        publish_mod.publish_to_instagram("https://files.example.com/c.mp4", "hi")


def test_instagram_mocked_publish(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "ig-token")
    monkeypatch.setenv("INSTAGRAM_USER_ID", "17841400000000000")
    monkeypatch.setenv("INSTAGRAM_VIDEO_BASE_URL", "https://files.example.com")
    seen = {}

    def fake_post(url, data=None, timeout=None, **k):
        if url.endswith("/media"):
            seen["create"] = data
            assert data["media_type"] == "REELS"
            assert data["video_url"] == "https://files.example.com/clip.mp4"
            return _FakeResp(200, {"id": "container-1"})
        if url.endswith("/media_publish"):
            assert data["creation_id"] == "container-1"
            return _FakeResp(200, {"id": "media-1"})
        raise AssertionError(f"unexpected POST {url}")

    def fake_get(url, params=None, timeout=None, **k):
        assert "media-1" in url
        return _FakeResp(200, {"permalink": "https://www.instagram.com/reel/abc/"})
    monkeypatch.setattr(publish_mod.httpx, "post", fake_post)
    monkeypatch.setattr(publish_mod.httpx, "get", fake_get)
    result = publish_mod.publish_to_instagram("https://files.example.com/clip.mp4", "My reel")
    assert result["media_id"] == "media-1"
    assert result["permalink"] == "https://www.instagram.com/reel/abc/"
    assert result["caption"] == "My reel"


def test_instagram_api_error_surfaced(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "bad-token")
    monkeypatch.setenv("INSTAGRAM_USER_ID", "17841400000000000")
    monkeypatch.setattr(publish_mod.httpx, "post",
                        lambda *a, **k: _FakeResp(400, {"error": {"message": "Invalid OAuth access token", "code": 190}}))
    with pytest.raises(publish_mod.PublishError, match="Invalid OAuth access token"):
        publish_mod.publish_to_instagram("https://files.example.com/clip.mp4", "hi")


def test_instagram_router_503s_and_mocked_success(client, auth_headers, test_video, monkeypatch):
    clip_id = _ready_clip(test_video)
    # Nothing configured.
    monkeypatch.delenv("INSTAGRAM_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("INSTAGRAM_USER_ID", raising=False)
    r = client.post("/api/publish/instagram/upload", headers=auth_headers,
                    json={"clip_id": clip_id, "caption": "hi"})
    assert r.status_code == 503
    assert "not configured" in r.text
    # Token set but no public base URL -> 503 with the public-URL message.
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "ig-token")
    monkeypatch.setenv("INSTAGRAM_USER_ID", "17841400000000000")
    monkeypatch.delenv("INSTAGRAM_VIDEO_BASE_URL", raising=False)
    r = client.post("/api/publish/instagram/upload", headers=auth_headers,
                    json={"clip_id": clip_id, "caption": "hi"})
    assert r.status_code == 503
    assert "PUBLIC" in r.text
    # All set + mocked publish -> task done with a media id.
    monkeypatch.setenv("INSTAGRAM_VIDEO_BASE_URL", "https://files.example.com")
    monkeypatch.setattr(publish_router, "publish_to_instagram",
                        lambda url, caption: {"media_id": "media-7",
                                              "permalink": "https://www.instagram.com/reel/x/",
                                              "caption": caption})
    r = client.post("/api/publish/instagram/upload", headers=auth_headers,
                    json={"clip_id": clip_id, "caption": "Hello Reels"})
    assert r.status_code == 200, r.text
    task = _wait_task(client, auth_headers, r.json()["task_id"])
    assert task["status"] == "done", task
    assert task["result"]["media_id"] == "media-7"
    assert task["result"]["platform"] == "instagram"
    assert task["result"]["video_url"] == "https://files.example.com/test_video.mp4"


# ============================ status + provider keys ============================

def test_publish_status_real_platforms(client, auth_headers):
    r = client.get("/api/publish/status", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["youtube"]["status"] == "not_connected"
    assert body["tiktok"]["status"] == "not_configured"
    assert body["tiktok"]["configured"] is False
    assert body["instagram"]["status"] == "not_configured"
    assert body["instagram"]["public_url_ready"] is False
    assert "steps" in body["tiktok"] and "steps" in body["instagram"]


def test_provider_keys_include_new_providers(client, auth_headers):
    r = client.get("/api/api-keys/providers", headers=auth_headers)
    assert r.status_code == 200
    names = [p["name"] for p in r.json()["providers"]]
    assert any("Twitch" in n for n in names)
    assert any("TikTok" in n for n in names)
    assert any("Instagram" in n for n in names)
    assert any("Pexels" in n for n in names)
    assert any("AssemblyAI" in n for n in names)
