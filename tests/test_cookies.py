"""ClipPilot AI cookies tests — browser-cookies + cookies.txt for YouTube
ingest (v1.2). yt-dlp calls are MOCKED throughout: no real browser, no
real YouTube login and no network in these tests. What is asserted:
  - the right yt-dlp options reach YoutubeDL (cookiesfrombrowser tuple /
    cookiefile / neither by default),
  - invalid browsers and bad cookies files fail early with clear messages,
  - a cookie-load failure tells the user to close the browser and check
    their YouTube login,
  - "Requested format is not available" suggests cookies when none were
    used, and the direct-upload fallback when they were,
  - ANSI colour codes are stripped from download errors,
  - the /from-url endpoint accepts multipart form data, saves an uploaded
    cookies.txt only under uploads/ and deletes it right after the ingest,
    success or failure.
"""
import os
import subprocess
import time

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_clippilot.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import video as video_mod  # noqa: E402
from app.database import engine as _engine  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import clips as clips_router  # noqa: E402
from app.routers import videos as videos_router  # noqa: E402
from app.seed import seed  # noqa: E402
from yt_dlp.cookies import CookieLoadError  # noqa: E402
from yt_dlp.utils import DownloadError  # noqa: E402

TEST_VIDEO = os.path.join(os.path.dirname(__file__), "test_video.mp4")


def _ffmpeg():
    return video_mod.ffmpeg_exe()


def _make_test_video(path: str):
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


def _wait_task(client, headers, task_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/videos/tasks/{task_id}", headers=headers)
        assert r.status_code == 200, r.text
        t = r.json()
        if t["status"] in ("done", "failed"):
            return t
        time.sleep(0.5)
    raise AssertionError(f"task {task_id} did not finish in {timeout}s")


# ---- Fake YoutubeDL (records opts, never touches the network) ----

def _make_fake_ydl(out_path, exc=None):
    class FakeYDL:
        instances = []

        def __init__(self, opts):
            self.opts = opts
            FakeYDL.instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=True):
            if exc is not None:
                raise exc
            return {"title": "Test video", "duration": 30,
                    "ext": "mp4", "id": "test123"}

        def prepare_filename(self, info):
            return out_path

    return FakeYDL


def _valid_cookies_file(tmp_path, name="cookies.txt"):
    p = tmp_path / name
    p.write_text("# Netscape HTTP Cookie File\n"
                 ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tfake-session-value\n",
                 encoding="utf-8")
    return str(p)


# ---- download_url: options passed to yt-dlp ----

def test_download_url_plain_passes_no_cookie_opts(tmp_path, monkeypatch):
    out = tmp_path / "vid.mp4"
    out.write_bytes(b"fake")
    FakeYDL = _make_fake_ydl(str(out))
    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    result = video_mod.download_url("https://www.youtube.com/watch?v=abc", str(tmp_path))
    assert result["title"] == "Test video"
    opts = FakeYDL.instances[-1].opts
    assert "cookiesfrombrowser" not in opts
    assert "cookiefile" not in opts


@pytest.mark.parametrize("browser", ["chrome", "edge", "firefox"])
def test_download_url_use_cookies_passes_browser_tuple(tmp_path, monkeypatch, browser):
    out = tmp_path / "vid.mp4"
    out.write_bytes(b"fake")
    FakeYDL = _make_fake_ydl(str(out))
    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    video_mod.download_url("https://www.youtube.com/watch?v=abc", str(tmp_path),
                           use_cookies=True, cookies_browser=browser)
    opts = FakeYDL.instances[-1].opts
    assert opts["cookiesfrombrowser"] == (browser,)


def test_download_url_cookiefile_passed(tmp_path, monkeypatch):
    out = tmp_path / "vid.mp4"
    out.write_bytes(b"fake")
    FakeYDL = _make_fake_ydl(str(out))
    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    cpath = _valid_cookies_file(tmp_path)
    video_mod.download_url("https://www.youtube.com/watch?v=abc", str(tmp_path),
                           cookies_file=cpath)
    opts = FakeYDL.instances[-1].opts
    assert opts["cookiefile"] == cpath
    assert "cookiesfrombrowser" not in opts


def test_download_url_bad_browser(tmp_path, monkeypatch):
    out = tmp_path / "vid.mp4"
    out.write_bytes(b"fake")
    FakeYDL = _make_fake_ydl(str(out))
    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    with pytest.raises(ValueError, match="chrome, edge or firefox"):
        video_mod.download_url("https://www.youtube.com/watch?v=abc", str(tmp_path),
                               use_cookies=True, cookies_browser="safari")


def test_download_url_missing_cookiefile(tmp_path, monkeypatch):
    FakeYDL = _make_fake_ydl(str(tmp_path / "vid.mp4"))
    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    with pytest.raises(RuntimeError, match="could not be read"):
        video_mod.download_url("https://www.youtube.com/watch?v=abc", str(tmp_path),
                               cookies_file=str(tmp_path / "nope.txt"))


def test_download_url_bad_cookiefile_content(tmp_path, monkeypatch):
    bad = tmp_path / "cookies.txt"
    bad.write_text("this is not a cookies file\nno tabs here\n", encoding="utf-8")
    FakeYDL = _make_fake_ydl(str(tmp_path / "vid.mp4"))
    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    with pytest.raises(RuntimeError, match="could not be read"):
        video_mod.download_url("https://www.youtube.com/watch?v=abc", str(tmp_path),
                               cookies_file=str(bad))


# ---- download_url: error mapping ----

def test_download_url_cookie_load_error_is_actionable(tmp_path, monkeypatch):
    FakeYDL = _make_fake_ydl(str(tmp_path / "vid.mp4"),
                             exc=CookieLoadError("failed to load cookies"))
    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    with pytest.raises(RuntimeError) as ei:
        video_mod.download_url("https://www.youtube.com/watch?v=abc", str(tmp_path),
                               use_cookies=True, cookies_browser="chrome")
    msg = str(ei.value)
    assert "Close Chrome" in msg
    assert "logged into YouTube" in msg


def test_download_url_cookie_error_as_download_error_is_actionable(tmp_path, monkeypatch):
    # yt-dlp sometimes surfaces a cookie failure as a plain DownloadError
    # ("could not find chrome cookies database ...") instead of
    # CookieLoadError — it must still map to the actionable message.
    FakeYDL = _make_fake_ydl(
        str(tmp_path / "vid.mp4"),
        exc=DownloadError('ERROR: could not find chrome cookies database in "/x"'))
    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    with pytest.raises(RuntimeError) as ei:
        video_mod.download_url("https://www.youtube.com/watch?v=abc", str(tmp_path),
                               use_cookies=True, cookies_browser="chrome")
    msg = str(ei.value)
    assert "Could not read cookies from Chrome" in msg
    assert "Close Chrome" in msg


def test_download_url_format_error_suggests_cookies(tmp_path, monkeypatch):
    FakeYDL = _make_fake_ydl(
        str(tmp_path / "vid.mp4"),
        exc=DownloadError("ERROR: [youtube] abc: Requested format is not available"))
    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    with pytest.raises(RuntimeError) as ei:
        video_mod.download_url("https://www.youtube.com/watch?v=abc", str(tmp_path))
    assert "Use my browser's YouTube login" in str(ei.value)


def test_download_url_format_error_with_cookies_suggests_upload(tmp_path, monkeypatch):
    FakeYDL = _make_fake_ydl(
        str(tmp_path / "vid.mp4"),
        exc=DownloadError("ERROR: [youtube] abc: Requested format is not available"))
    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    with pytest.raises(RuntimeError) as ei:
        video_mod.download_url("https://www.youtube.com/watch?v=abc", str(tmp_path),
                               use_cookies=True, cookies_browser="firefox")
    assert "even with cookies" in str(ei.value)
    assert "upload the file directly" in str(ei.value)


def test_download_url_strips_ansi_codes(tmp_path, monkeypatch):
    FakeYDL = _make_fake_ydl(
        str(tmp_path / "vid.mp4"),
        exc=DownloadError("\x1b[0;31mERROR:\x1b[0m something went wrong"))
    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    with pytest.raises(RuntimeError) as ei:
        video_mod.download_url("https://www.youtube.com/watch?v=abc", str(tmp_path))
    assert "\x1b" not in str(ei.value)
    assert "something went wrong" in str(ei.value)


# ---- /from-url endpoint (multipart form) ----

def test_from_url_with_cookies_creates_task(client, auth_headers, monkeypatch):
    seen = {}

    def fake_download(url, out_dir, use_cookies=False, cookies_browser="chrome",
                      cookies_file=None):
        seen.update(url=url, use_cookies=use_cookies,
                    cookies_browser=cookies_browser, cookies_file=cookies_file)
        raise RuntimeError("boom")

    monkeypatch.setattr(videos_router, "download_url", fake_download)
    r = client.post("/api/videos/from-url", headers=auth_headers,
                    data={"url": "https://www.youtube.com/watch?v=abc",
                          "use_cookies": "true", "cookies_browser": "edge"})
    assert r.status_code == 200, r.text
    task = _wait_task(client, auth_headers, r.json()["task_id"])
    assert task["status"] == "failed"
    assert task["error"] == "boom"
    assert seen["use_cookies"] is True
    assert seen["cookies_browser"] == "edge"
    assert seen["cookies_file"] is None


def test_from_url_cookies_file_deleted_after_ingest(client, auth_headers, monkeypatch, tmp_path):
    seen = {}
    content = ("# Netscape HTTP Cookie File\n"
               ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tfake-session-value\n")

    def fake_download(url, out_dir, use_cookies=False, cookies_browser="chrome",
                      cookies_file=None):
        seen["cookies_file"] = cookies_file
        # The saved file must hold exactly what was uploaded.
        assert cookies_file is not None
        assert os.path.basename(cookies_file).startswith("cookies_")
        assert cookies_file.endswith(".txt")
        with open(cookies_file, encoding="utf-8") as f:
            assert "fake-session-value" in f.read()
        raise RuntimeError("boom")

    monkeypatch.setattr(videos_router, "download_url", fake_download)
    r = client.post("/api/videos/from-url", headers=auth_headers,
                    data={"url": "https://www.youtube.com/watch?v=abc",
                          "use_cookies": "false", "cookies_browser": "chrome"},
                    files={"cookies_file": ("cookies.txt", content.encode(),
                                            "text/plain")})
    assert r.status_code == 200, r.text
    task = _wait_task(client, auth_headers, r.json()["task_id"])
    assert task["status"] == "failed"
    assert task["error"] == "boom"
    # Deleted right after the ingest, whatever the outcome.
    assert seen["cookies_file"] is not None
    assert not os.path.exists(seen["cookies_file"])


def test_from_url_bad_browser_400(client, auth_headers):
    r = client.post("/api/videos/from-url", headers=auth_headers,
                    data={"url": "https://www.youtube.com/watch?v=abc",
                          "cookies_browser": "safari"})
    assert r.status_code == 400
    assert "chrome, edge or firefox" in r.text


def test_from_url_non_txt_cookies_400(client, auth_headers):
    r = client.post("/api/videos/from-url", headers=auth_headers,
                    data={"url": "https://www.youtube.com/watch?v=abc"},
                    files={"cookies_file": ("cookies.json", b"{}", "application/json")})
    assert r.status_code == 400
    assert ".txt" in r.text


def test_from_url_plain_still_works(client, auth_headers, test_video, monkeypatch):
    def fake_download(url, out_dir, use_cookies=False, cookies_browser="chrome",
                      cookies_file=None):
        assert use_cookies is False
        assert cookies_file is None
        return {"path": test_video, "title": "Plain fetch", "duration": 20, "ext": "mp4"}

    monkeypatch.setattr(videos_router, "download_url", fake_download)
    r = client.post("/api/videos/from-url", headers=auth_headers,
                    data={"url": "https://www.youtube.com/watch?v=abc"})
    assert r.status_code == 200, r.text
    task = _wait_task(client, auth_headers, r.json()["task_id"])
    assert task["status"] == "done", task
    assert task["result"]["video_id"]
