# ClipPilot AI — Local-First Video Clipping

> Long video in, short vertical clips out. **Local-first** — everything runs
> on your machine by default; two optional cloud features (Groq LLM clip
> selection and direct YouTube publishing) run only when you configure them.

Inspired by [supoclip](https://github.com/FujiwaraChoki/supoclip) (open-source OpusClip alternative).
ClipPilot mirrors its feature set: local substitutes where possible, real
cloud features where a key/account is needed — and it always says which is which.

## What it does

1. **Upload a video** (MP4/WebM/MOV/MKV, up to **500 MB**) or **ingest a public URL**
   (YouTube, Twitch, Kick or a direct MP4 link) with yt-dlp. No live streams.
2. **Analyse**: ffmpeg `silencedetect` finds speech segments; then clip picks
   come from the **Groq LLM** (`openai/gpt-oss-120b`) when it is configured —
   the LLM reads the transcript and picks the top 3–7 moments with a hook
   title and a reason for each. When the LLM is not configured (or the call
   fails, or its output is invalid), the transparent local scorer ranks
   15–60s candidate windows instead and returns the top 5 with a score, a
   4-part virality breakdown (hook, engagement, value, shareability) and a
   human-readable reason for each. Either way the same local scorer computes
   the score/breakdown, so the dashboard bars always work. Every analysis
   response says which selector was used: `selector: "llm"` or `"local"`.
3. **Render real clips** with ffmpeg: cut + smart-crop to **720x1280 (9:16)**,
   H.264 + AAC. Face detected (OpenCV Haar cascade) → crop centres on the face;
   no face → centre-crop fallback.
4. **Word-synced captions** from a transcript (local faster-whisper transcription
   or an uploaded SRT/TXT) as burned-in `.ass` captions in 2 templates
   (bold-yellow / white-on-black). Needs a font: `CAPTION_FONT` env var, or
   Windows `C:\Windows\Fonts\arial.ttf` by default. No font found → the clip
   ships with an SRT sidecar and the UI says so honestly.
5. **Hook titles**: template-based titles from segment keywords, burned into the
   opening 3 seconds. Labelled template-based, not LLM-generated.
6. **Editor**: trim, split, merge clips (real ffmpeg) and export with platform
   presets — TikTok, Reels, Shorts (resolution + caption style per preset).
7. **B-roll slot**: upload your own B-roll files; the renderer overlays them for
   the first 5 seconds of a clip (bottom-right corner). Pexels stock footage
   needs a Pexels API key and is **not** included.
8. **Performance loop**: log real views/likes/comments/shares per clip (numbers
   you enter yourself). Strong logged performance gives future candidates on
   the same video a small scoring boost (max +5). No stats are ever invented.
9. **Real-time progress**: analysis and rendering run as background tasks;
   `GET /api/videos/tasks/{task_id}` reports stage + progress %, the dashboard polls it.
10. **Publishing**: connect your Google account (OAuth2) and publish a rendered
    clip straight to **YouTube** from the Clips tab — uploads default to
    **private**, you choose private/unlisted/public per upload. TikTok and
    Instagram stay **Not configured** (their developer-app approval is a
    manual step) and never fake a post. Nothing is ever posted on your behalf
    without you clicking Publish.
11. **Billing (MOCK) + API keys**: Free/Pro plans with server-side limits and a
    mock upgrade (no payment). The API keys page issues real local API keys for
    the REST API (`X-API-Key` header). Provider keys (AssemblyAI / LLM / Pexels)
    are shown as **Not configured** until their env vars are set.

## How the highlight score works (local heuristic, not a black box)

For each 15–60s candidate window:

| Signal | Meaning |
|---|---|
| Speech density | Fraction of the window that is speech (from silencedetect) |
| Length fit | 1.0 at 30–45s, falling off towards 15s and 60s |
| Position | Slight preference for the first half of the video |
| Energy | Mean volume of the window (ffmpeg volumedetect), normalised |
| Keyword hits | Transcript keywords inside the window (0 if no transcript) |

Virality sub-scores (0–100 each):

- **Hook** = 60% speech in the first 5s + 40% keyword hits in the first 5s
- **Engagement** = 60% speech density + 40% energy
- **Value** = 60% keyword density + 40% length fit
- **Shareability** = 50% energy + 30% position + 20% length fit

Overall = 0.30·hook + 0.30·engagement + 0.25·value + 0.15·shareability
(+ up to 5 points from logged performance of earlier clips on the same video).

## SupoClip feature map

| SupoClip feature | ClipPilot status |
|---|---|
| File upload + direct MP4 URLs | ✅ Working (yt-dlp; direct MP4 verified) |
| YouTube / Twitch / Kick URLs | ⚠️ Partial — yt-dlp wired, but YouTube downloads failed here with "Requested format is not available" (format listing works, download blocked — SABR/PO-token gating in this environment); may work from a home connection |
| AI clip selection (LLM, 3–7 segments) | ✅ Working with Groq (`openai/gpt-oss-120b`, free tier) when configured; validated hard (15–60s, in-range, no overlapping duplicates) and falls back to the local scorer on any failure — the analysis response always says which selector was used |
| Virality scoring (hook/engagement/value/shareability) | 🔶 Local equivalent — rule-based sub-scores, labelled local (used for both LLM picks and local picks) |
| Smart vertical crop + face detection | ✅ Working (OpenCV Haar; centre-crop fallback) |
| Word-synced subtitles (AssemblyAI) | 🔶 Local equivalent — faster-whisper (tiny) or SRT upload → .ass |
| Hook titles (AI headline) | ✅ LLM titles when the LLM picks the clip; template-based from keywords otherwise (labelled) |
| B-roll & transitions (Pexels) | 🔶 Local B-roll slot; Pexels = Not configured (needs key) |
| Built-in editor + platform presets | ✅ Working (trim/split/merge + TikTok/Reels/Shorts) |
| Direct publishing | 🔶 YouTube working (OAuth2 + resumable upload, uploads default to private) when connected; TikTok / Instagram = Not configured (developer-app approval needed, never fake-posts) |
| Performance loop | 🔶 Local equivalent — manually logged stats steer scoring |
| Real-time progress | ✅ Working (task endpoint + frontend polling) |
| Transcription (AssemblyAI / Whisper / Ollama) | 🔶 Local faster-whisper (tiny); AssemblyAI = Not configured |
| Billing + API keys | 🔶 Mock billing; real local API keys for the REST API |
| MCP server | ❌ Later step (not in this build) |
| Twitch auto-import / live streams | ❌ Later step (not in this build) |

## Run it (Windows, PowerShell + VS Code)

VS Code is only one way to run these commands — the same PowerShell commands
work in any PowerShell window. Delete any old `clippilot-ai` folder and its old
`.venv` first, then extract this ZIP fresh.

```powershell
cd clippilot-ai
python --version          # Python 3.14 expected (cp314 wheels verified)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m app.seed        # demo login + SAMPLE videos/clips
uvicorn app.main:app --reload --port 8012
```

Then open **http://127.0.0.1:8012** — the dashboard. API docs at `/docs`,
health at `/health`. Before installing, run `Get-Content requirements.txt`
inside the project folder and confirm the pins are there.

**Demo login:** `admin@clippilot.app` / `admin123`

**First transcription downloads the faster-whisper tiny model once** (~75 MB from
HuggingFace) — internet is needed for that one download only. After that,
transcription is fully offline.

**ffmpeg:** bundled via the `imageio-ffmpeg` pip package — no separate install
on Windows. If the bundled binary is unavailable, the app falls back to a
system `ffmpeg` on PATH.

**Fonts:** burned-in captions need a font file. Default is
`C:\Windows\Fonts\arial.ttf`. Set `CAPTION_FONT` in `.env` to use another
`.ttf`. If no font is found, clips are delivered with an SRT sidecar instead
and the UI/README say so — captions are never silently faked.

## LLM clip selection (Groq, optional)

Without this, analysis uses the local transparent scorer — the app works
fully. To let a real LLM pick the moments (like supoclip):

1. Go to **https://console.groq.com/keys** and sign up / log in (free tier,
   no card). Click **Create API Key**, copy it.
2. In `backend/.env` — copy `.env.example` to `.env` first if you have not —
   set:
   ```
   LLM_PROVIDER=groq
   LLM_API_KEY=paste-your-key-here
   LLM_MODEL=openai/gpt-oss-120b
   LLM_BASE_URL=https://api.groq.com/openai/v1
   ```
   **Never share the key with anyone and never send it in chat** — it lives
   in your local `.env` only (`.env` is gitignored and never in the ZIP).
3. Restart the server (`uvicorn app.main:app --reload --port 8012`).
4. Transcribe a video first (the LLM reads the transcript), then click
   Analyze. The task result shows `selector: "llm"` and the dashboard shows
   "Clip selection: LLM (Groq)".

How it works: the transcript (segments with timestamps) is sent to Groq's
OpenAI-compatible chat completions endpoint; the model returns 3–7 clips as
strict JSON. ClipPilot validates the output hard — every clip must be
15–60s, inside the video duration, and not overlap another pick — and
scores the accepted windows with the same transparent local scorer, so the
hook/engagement/value/shareability bars still mean the same thing. On any
failure (no key, network error, bad key, bad JSON, invalid ranges) analysis
falls back to the local scorer and the response says so
(`selector: "local"`, with the reason). The key is never logged.

## Direct publishing to YouTube (optional)

Without this, the Publishing tab shows YouTube as Not configured and the
Publish button stays disabled — nothing is ever fake-posted. To publish
real uploads:

1. Go to **Google Cloud Console** (console.cloud.google.com) and create a
   project.
2. **APIs & Services → Library** → search **YouTube Data API v3** → Enable.
3. **APIs & Services → OAuth consent screen** → User type **External** →
   fill the app name → add your own Google account under **Test users** →
   Save. (While the app is in Testing, only test users can connect — that is
   enough for your own channel.)
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   → Application type **Desktop app** → Create. Copy the **Client ID** and
   **Client Secret**.
5. On the same credentials page, add this **Authorized redirect URI** — it
   must match exactly:
   `http://127.0.0.1:8012/api/publish/youtube/callback`
6. In `.env` set:
   ```
   YOUTUBE_CLIENT_ID=paste-your-client-id
   YOUTUBE_CLIENT_SECRET=paste-your-client-secret
   YOUTUBE_REDIRECT_URI=http://127.0.0.1:8012/api/publish/youtube/callback
   ```
7. Restart the server, open the **Publishing & Billing** tab and click
   **Connect YouTube**. Sign in with Google and allow the YouTube upload
   permission. You are redirected back with "YouTube connected".
8. Generate a clip, then click **Publish to YouTube** on it. Pick a privacy
   (private / unlisted / public — default **private**). The task result
   contains the real video URL (`https://www.youtube.com/watch?v=...`).

Notes: tokens are stored locally in `data/youtube_token.json` (gitignored,
never in the ZIP or the repo) and refreshed automatically; **Disconnect**
deletes them. YouTube Data API quota: the default is 10,000 units/day and
one video upload costs 1,600 units — about 6 uploads a day on the free
quota. TikTok and Instagram need their own developer-app approvals and stay
Not configured (see the Publishing tab for the steps).

## Run the tests

```powershell
pytest -q
```

Tests cover auth, upload, analysis, scoring, real ffmpeg rendering
(720x1280 + audio stream), face-crop math, transcript upload, editor
(trim/split/merge/export), API keys, billing limits and not-connected states,
plus the v1.1 additions: LLM validation + mocked Groq success/failure/
fallback paths, the honest `selector` in analysis results, YouTube OAuth URL
building, token refresh (mocked), mocked publish success, and fail-closed
publishing without credentials (503).

## API overview

| Endpoint | What it does |
|---|---|
| `POST /api/videos/upload` | Upload a video (500 MB cap) |
| `POST /api/videos/from-url` | Ingest a public URL with yt-dlp (background task) |
| `POST /api/videos/{id}/analyze` | Analysis → ranked clip candidates (`selector: "llm"` or `"local"`) |
| `POST /api/videos/{id}/transcribe` | Local faster-whisper transcription (background task) |
| `POST /api/videos/{id}/transcript` | Upload an SRT/TXT transcript |
| `GET /api/videos/tasks/{task_id}` | Task stage + progress % |
| `POST /api/clips/{id}/generate` | Render a 720x1280 clip |
| `POST /api/clips/{id}/trim` · `/split` · `/merge` · `/export` | Editor actions |
| `POST /api/clips/{id}/stats` | Log real performance numbers |
| `GET /api/publish/status` | YouTube/TikTok/Instagram publishing status |
| `GET /api/publish/youtube/auth-url` | Google OAuth consent URL (YouTube) |
| `GET /api/publish/youtube/callback` | OAuth redirect — exchanges the code for tokens |
| `POST /api/publish/youtube/upload` | Upload a rendered clip to YouTube (default private) |
| `POST /api/publish/youtube/disconnect` | Delete stored YouTube tokens |
| `POST /api/publish/tiktok/upload` · `/instagram/upload` | Always 503 Not configured (honest stub) |
| `GET /api/accounts` · `/api/billing` · `/api/api-keys` | Publishing, mock billing, local API keys |

Auth: JWT login (`/auth/login` JSON, `/auth/login-form` for Swagger Authorize)
or a local API key via the `X-API-Key` header.

## Honest limits

- Highlight scoring is a **local heuristic**, not an LLM and not a trained model.
  It finds loud, dense-speech moments — it cannot judge whether a moment is
  actually interesting. When the Groq LLM picks the clips, it can still pick
  odd moments: its output is validated for timing only, not for quality, and
  the score bars still come from the local heuristic.
- The LLM path is only as good as the transcript: no transcript, no LLM picks
  (the local scorer is used instead). A Groq key is required; without it the
  app works exactly as before.
- Transcription uses faster-whisper `tiny`: fast and offline, but less accurate
  than large/cloud models, especially on noisy audio or heavy accents.
- Face detection is an OpenCV Haar cascade: it finds clear frontal faces; it
  misses profile faces, small faces and faces in poor light (then centre-crop
  is used). **Not verified on a real human face in this build** — the
  face-centring path is covered by a unit test with synthetic face boxes.
- YouTube publishing is **not verified end-to-end with a real Google account
  here** (no OAuth client was available in the build environment): the OAuth
  URL building, token refresh, upload plumbing and all fail-closed paths are
  covered by tests with mocks, but the first real connect + upload must be
  done by the user with their own Google Cloud OAuth client. YouTube Data API
  quota (default 10,000 units/day, 1,600 per upload) also applies.
- YouTube may block yt-dlp downloads without cookies (bot checks). Verified
  from the build machine: a direct MP4 URL ingests fine, but YouTube downloads
  failed with "Requested format is not available" on two different test videos
  (format listing works; the actual stream download is blocked — SABR/PO-token
  gating in that environment). It may work from a normal home connection; if
  YouTube blocks, the task fails with a clear error and the video is not
  created — use a direct MP4 link or file upload instead.
- Verified on Linux (Python 3.12) with cp314 wheel existence proven by
  download check. The Windows run is still unconfirmed; the Swagger
  Authorize button click is a manual check.
- No demo video is bundled (too big). Upload your own video to try it.

## Project layout

```
app/
  main.py            FastAPI app + /health + static dashboard
  video.py           ffmpeg engine: probe, silencedetect, scoring, render, merge, yt-dlp
  llm.py             Groq LLM clip selection (httpx, validated, local fallback)
  publish.py         YouTube OAuth2 + resumable upload (httpx, token file in data/)
  captions.py        SRT/TXT parsing, keywords, .ass builder, Whisper transcription
  auth.py            JWT + bcrypt + API-key auth
  tasks.py           background task store (stage + progress)
  seed.py            demo admin + SAMPLE videos/clips
  routers/           auth, videos, clips, accounts, publish
  static/            dashboard (index.html, style.css, app.js)
tests/               pytest smoke tests + LLM/publish tests (mocked, no real keys)
```
