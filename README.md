# ClipPilot AI — Local-First Video Clipping

> Long video in, short vertical clips out. **Local-first** — everything runs
> on your machine by default; optional cloud features (Groq LLM clip
> selection, direct YouTube/TikTok/Instagram publishing, AssemblyAI
> transcription, Pexels B-roll, Twitch VOD import) run only when you
> configure them.

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
   by default, or AssemblyAI cloud transcription when `TRANSCRIPTION_PROVIDER=assemblyai`
   and `ASSEMBLYAI_API_KEY` are set — see below) or an uploaded SRT/TXT, as burned-in
   `.ass` captions in 2 templates (bold-yellow / white-on-black). Needs a font:
   `CAPTION_FONT` env var, or Windows `C:\Windows\Fonts\arial.ttf` by default.
   No font found → the clip ships with an SRT sidecar and the UI says so honestly.
5. **Hook titles**: template-based titles from segment keywords, burned into the
   opening 3 seconds. Labelled template-based, not LLM-generated.
6. **Editor**: trim, split, merge clips (real ffmpeg) and export with platform
   presets — TikTok, Reels, Shorts (resolution + caption style per preset).
7. **B-roll slot**: upload your own B-roll files, or search **Pexels** stock footage
   (needs `PEXELS_API_KEY` — see below) and download a clip straight into the B-roll
   folder; the renderer overlays it for the first 5 seconds of a clip (bottom-right corner).
8. **Performance loop**: log real views/likes/comments/shares per clip (numbers
   you enter yourself). Strong logged performance gives future candidates on
   the same video a small scoring boost (max +5). No stats are ever invented.
9. **Real-time progress**: analysis and rendering run as background tasks;
   `GET /api/videos/tasks/{task_id}` reports stage + progress %, the dashboard polls it.
10. **Publishing**: connect your Google account (OAuth2) and publish a rendered
    clip straight to **YouTube** from the Clips tab — uploads default to
    **private**, you choose private/unlisted/public per upload. **TikTok** works the
    same way (OAuth2 + Content Posting API, uploads default to SELF_ONLY) once your
    TikTok developer app is approved. **Instagram** publishes Reels via the Graph API
    once a long-lived token is set — it needs the clip at a PUBLIC https URL (see below).
    Nothing is ever posted on your behalf without you clicking Publish, and a platform
    that is not configured shows "Not configured" instead of faking a post.
11. **Billing (MOCK) + API keys**: Free/Pro plans with server-side limits and a
    mock upgrade (no payment). The API keys page issues real local API keys for
    the REST API (`X-API-Key` header). Provider keys (AssemblyAI / LLM / Pexels /
    Twitch / TikTok / Instagram) are shown as **Not configured** until their env vars are set.
12. **Twitch VOD import**: with `TWITCH_CLIENT_ID` + `TWITCH_CLIENT_SECRET` set, list a
    channel's recent VODs from the Videos tab and import one as a URL-ingest job
    (yt-dlp downloads it). Live streams are never imported — only finished VODs.
13. **MCP server**: run `python -m app.mcp` to expose ClipPilot to MCP clients
    (e.g. Claude Desktop) over stdio — list/analyse videos, render clips, check
    YouTube status and publish, using the same local code paths as the dashboard.

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
| YouTube / Twitch / Kick URLs | ⚠️ Partial — yt-dlp wired, but YouTube downloads are blocked by SABR/PO-token gating ("Requested format is not available") — confirmed on the build machine AND on a home connection. Workaround: tick **Use my browser's YouTube login (cookies)** on the Fetch URL form, or upload a cookies.txt, or download the video and upload the file directly |
| AI clip selection (LLM, 3–7 segments) | ✅ Working with Groq (`openai/gpt-oss-120b`, free tier) when configured; validated hard (15–60s, in-range, no overlapping duplicates) and falls back to the local scorer on any failure — the analysis response always says which selector was used |
| Virality scoring (hook/engagement/value/shareability) | 🔶 Local equivalent — rule-based sub-scores, labelled local (used for both LLM picks and local picks) |
| Smart vertical crop + face detection | ✅ Working (OpenCV Haar; centre-crop fallback) |
| Word-synced subtitles (AssemblyAI) | ✅ Local faster-whisper (tiny) by default; AssemblyAI cloud transcription when `TRANSCRIPTION_PROVIDER=assemblyai` + key are set — same .ass pipeline, fail-closed without a key |
| Hook titles (AI headline) | ✅ LLM titles when the LLM picks the clip; template-based from keywords otherwise (labelled) |
| B-roll & transitions (Pexels) | ✅ Local B-roll slot + real Pexels search/download when `PEXELS_API_KEY` is set (503 "Not configured" without it) |
| Built-in editor + platform presets | ✅ Working (trim/split/merge + TikTok/Reels/Shorts) |
| Direct publishing | ✅ YouTube working (OAuth2 + resumable upload, uploads default to private) when connected; TikTok working (OAuth2 + Content Posting API, default SELF_ONLY) when its developer app is approved + connected; Instagram Reels working (Graph API) when token + public video URL are set — never fake-posts |
| Performance loop | 🔶 Local equivalent — manually logged stats steer scoring |
| Real-time progress | ✅ Working (task endpoint + frontend polling) |
| Transcription (AssemblyAI / Whisper / Ollama) | ✅ Local faster-whisper (tiny); AssemblyAI option when configured |
| Billing + API keys | 🔶 Mock billing; real local API keys for the REST API |
| MCP server | ✅ Working — `python -m app.mcp` (stdio JSON-RPC, hand-rolled, no SDK): list/analyse videos, render clips, YouTube status + publish |
| Twitch auto-import / live streams | ✅ VOD import working (Helix API + yt-dlp) when `TWITCH_CLIENT_ID`/`SECRET` are set; live streams are NOT ingested (no finished file) |

## YouTube downloads blocked? Use browser cookies

YouTube increasingly blocks yt-dlp downloads with
`Requested format is not available` (SABR/PO-token gating). This is a
YouTube-side block, not a ClipPilot bug — it was confirmed on the build
machine **and** on a home connection. Two opt-in fixes, both on the
**Fetch URL** form:

1. **Browser cookies (easiest)** — tick *Use my browser's YouTube login
   (cookies)* and pick your browser (Chrome / Edge / Firefox). yt-dlp then
   reads your logged-in session (`--cookies-from-browser`). If it fails
   with "Could not read cookies", **close the browser completely** and try
   again (the browser locks its cookie store while running), and make sure
   you are logged into YouTube in that browser.
2. **cookies.txt** — export your cookies in Netscape format (e.g. with a
   "Get cookies.txt" browser extension) and attach the file instead.
   The file lives only under `uploads/`, is never stored in the database,
   and is **deleted right after the download** (success or failure).
   Browser cookies are read live by yt-dlp and never copied anywhere.

Cookies are sensitive credentials: they are never logged and never leave
your machine. If neither option works, download the video and use **file
upload** instead — that path always works.

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
quota.

## Direct publishing to TikTok (optional)

TikTok uses the same connect-then-publish pattern as YouTube, via the
Content Posting API:

1. Register an app at **developers.tiktok.com** and request the
   `video.upload` scope. **TikTok reviews the app** — approval is required
   before the API works, and a regular account cannot skip that step.
2. In `.env` set:
   ```
   TIKTOK_CLIENT_KEY=paste-your-client-key
   TIKTOK_CLIENT_SECRET=paste-your-client-secret
   TIKTOK_REDIRECT_URI=http://127.0.0.1:8012/api/publish/tiktok/callback
   ```
   Add the same redirect URI in the TikTok app settings.
3. Restart the server, open the **Publishing & Billing** tab and click
   **Connect TikTok**. Sign in with TikTok and allow the video.upload
   permission.
4. Generate a clip, then click **Publish to TikTok** on it. Pick a privacy
   (SELF_ONLY / PUBLIC_TO_EVERYONE / MUTUAL_FOLLOW_FRIENDS /
   FOLLOWER_OF_CREATOR — default **SELF_ONLY**, only you can see it). The
   task result contains the real `publish_id`. TikTok processes the video
   after upload, so the status is usually `PROCESSING` at first — check the
   publish id in the TikTok app.

Tokens live in `data/tiktok_token.json` (gitignored, never in the ZIP or the
repo) and refresh automatically; **Disconnect** deletes them. Without a
TikTok developer app, the TikTok endpoints return 503 "Not configured" and
nothing is ever fake-posted.

## Direct publishing to Instagram (optional)

Instagram publishes Reels via the Instagram Graph API. It is token-based
(no in-app OAuth): you supply a long-lived user access token.

1. Create a **Meta developer app**, add the **Instagram Graph API** product
   and connect a **Business/Creator** Instagram account (app review may be
   required).
2. Create a **long-lived user access token** (Graph API Explorer or your
   app's token flow) and set in `.env`:
   ```
   INSTAGRAM_ACCESS_TOKEN=paste-your-long-lived-token
   INSTAGRAM_USER_ID=paste-your-instagram-user-id
   INSTAGRAM_VIDEO_BASE_URL=https://your-public-host.example.com
   ```
3. **`INSTAGRAM_VIDEO_BASE_URL` is required** — Instagram's servers download
   the video from a PUBLIC https URL, so point it at a public host that
   serves your clip files (e.g. an S3 bucket or any public folder). A
   localhost URL will not work: the endpoint fails closed with the exact
   steps until a public base URL is set. The published URL is
   `{INSTAGRAM_VIDEO_BASE_URL}/{clip-filename}` — make sure the file is
   actually reachable there.
4. Restart the server, generate a clip, then click **Publish to Instagram**
   on it and enter a caption. The task result contains the real `media_id`
   and `permalink`.

Without the token/user ID, or without a public base URL, the Instagram
endpoint returns 503 "Not configured" and nothing is ever fake-posted.

## Pexels stock B-roll (optional)

1. Get a free API key at **https://www.pexels.com/api/** (no card needed).
2. In `.env` set `PEXELS_API_KEY=paste-your-key` and restart.
3. In the **Editor** tab, search Pexels (e.g. "city night") and click
   **Download as B-roll** on a result — the file lands in the local B-roll
   folder and can be selected when generating a clip (overlaid for the first
   5 seconds, bottom-right corner).

Without a key the Pexels endpoints return 503 "Not configured" and the
local B-roll upload slot is used instead. Only Pexels CDN links are ever
downloaded — any other host is refused.

## AssemblyAI transcription (optional)

By default transcription is local faster-whisper (`tiny`, offline, no key).
To use AssemblyAI cloud transcription instead:

1. Get a free API key at **https://www.assemblyai.com/**.
2. In `.env` set:
   ```
   TRANSCRIPTION_PROVIDER=assemblyai
   ASSEMBLYAI_API_KEY=paste-your-key
   ```
3. Restart the server. Transcribe a video as usual — the task shows
   "transcribing (AssemblyAI cloud)" and the transcript (word timestamps
   included) feeds the same .ass caption pipeline as the local path.

With `TRANSCRIPTION_PROVIDER=assemblyai` and no key, transcription fails
closed with a clear error — it never silently falls back to a fake or local
transcript and calls it AssemblyAI's. Set the provider back to `local` to
use faster-whisper again.

## Twitch channel VOD import (optional)

1. Create an app at **https://dev.twitch.tv/console/apps** and set in `.env`:
   ```
   TWITCH_CLIENT_ID=paste-your-client-id
   TWITCH_CLIENT_SECRET=paste-your-client-secret
   ```
2. Restart the server. In the **Videos** tab, enter a Twitch channel name
   and click **List VODs**, then **Import this VOD** on one — the VOD is
   downloaded with yt-dlp exactly like any other public URL.
3. **Live streams are not imported** — a live stream has no finished file
   to download; only finished VODs (past broadcasts) are listed.

Without credentials the Twitch endpoints return 503 "Not configured".

## MCP server (optional)

ClipPilot ships a hand-rolled MCP server (stdio JSON-RPC, no SDK) that
exposes the same features to MCP clients:

```powershell
python -m app.mcp
```

Tools: `list_videos`, `analyze_video`, `list_clips`, `render_clip`,
`get_clip`, `youtube_status`, `publish_to_youtube`. Analysis and rendering
run the same code paths as the dashboard (including Groq LLM picks when
configured); publishing uses the same fail-closed YouTube OAuth flow.

Claude Desktop config snippet (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "clippilot": {
      "command": "python",
      "args": ["-m", "app.mcp"],
      "cwd": "C:\\path\\to\\clippilot-ai",
      "env": { "DATABASE_URL": "sqlite:///./clippilot.db" }
    }
  }
}
```

The MCP server runs locally with full access to the local ClipPilot
database — do not expose it to anyone else. It never posts or publishes
anything on its own; `publish_to_youtube` only runs when the client calls
it, and only when YouTube is connected.

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
publishing without credentials (503), plus the v1.2 additions: browser-cookies
and cookies.txt ingest paths (mocked yt-dlp), actionable cookie errors, ANSI
stripping, and cookies-file deletion after ingest, plus the v1.3 additions:
MCP protocol + tools (mocked DB, plus a real subprocess smoke test),
Pexels search/download (mocked API, host validation), AssemblyAI
transcription (mocked upload/create/poll, fail-closed without a key),
Twitch VOD listing/import (mocked Helix), TikTok OAuth + upload
(mocked init/PUT/status), Instagram container + publish (mocked Graph API),
and fail-closed 503s for every new provider endpoint without credentials.

## API overview

| Endpoint | What it does |
|---|---|
| `POST /api/videos/upload` | Upload a video (500 MB cap) |
| `POST /api/videos/from-url` | Ingest a public URL with yt-dlp (background task; multipart form — optional browser cookies via `use_cookies` + `cookies_browser`, or a cookies.txt upload) |
| `POST /api/videos/{id}/analyze` | Analysis → ranked clip candidates (`selector: "llm"` or `"local"`) |
| `POST /api/videos/{id}/transcribe` | Local faster-whisper transcription, or AssemblyAI when `TRANSCRIPTION_PROVIDER=assemblyai` (background task) |
| `POST /api/videos/{id}/transcript` | Upload an SRT/TXT transcript |
| `GET /api/videos/twitch/vods?channel=` | List a Twitch channel's recent VODs (503 without Twitch credentials) |
| `POST /api/videos/twitch/import` | Import a Twitch VOD URL as a URL-ingest job |
| `GET /api/videos/tasks/{task_id}` | Task stage + progress % |
| `POST /api/clips/{id}/generate` | Render a 720x1280 clip |
| `POST /api/clips/{id}/trim` · `/split` · `/merge` · `/export` | Editor actions |
| `POST /api/clips/{id}/stats` | Log real performance numbers |
| `GET /api/clips/pexels/search?q=` | Search Pexels stock videos (503 without a key) |
| `POST /api/clips/pexels/download` | Download a Pexels file into the B-roll folder |
| `GET /api/publish/status` | YouTube/TikTok/Instagram publishing status |
| `GET /api/publish/youtube/auth-url` | Google OAuth consent URL (YouTube) |
| `GET /api/publish/youtube/callback` | OAuth redirect — exchanges the code for tokens |
| `POST /api/publish/youtube/upload` | Upload a rendered clip to YouTube (default private) |
| `POST /api/publish/youtube/disconnect` | Delete stored YouTube tokens |
| `GET /api/publish/tiktok/auth-url` | TikTok OAuth consent URL |
| `GET /api/publish/tiktok/callback` | OAuth redirect — exchanges the code for tokens |
| `POST /api/publish/tiktok/upload` | Upload a rendered clip to TikTok (default SELF_ONLY) |
| `POST /api/publish/tiktok/disconnect` | Delete stored TikTok tokens |
| `POST /api/publish/instagram/upload` | Publish a Reel to Instagram (needs token + public video URL) |
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
- TikTok publishing is **not verified end-to-end with a real TikTok app here**
  (no developer app was available): the OAuth URL building, token exchange,
  upload init/PUT/status plumbing and all fail-closed paths are covered by
  tests with mocks, but the first real connect + upload needs the user's own
  approved TikTok developer app (TikTok reviews apps for the video.upload
  scope). TikTok usually reports `PROCESSING` right after upload — the video
  appears in the TikTok app once processing finishes.
- Instagram publishing is **not verified end-to-end with a real Meta app
  here** (no access token was available): the container + publish + permalink
  plumbing and all fail-closed paths are covered by tests with mocks, but the
  first real publish needs the user's own long-lived token, Business/Creator
  account, and a PUBLIC video URL — Instagram's servers must be able to
  download the clip, so a localhost file can never be published.
- Pexels, AssemblyAI and Twitch are **not verified with real keys here** (no
  keys were available): search/download, upload/create/poll and
  token/users/videos plumbing are covered by tests with mocks, but the first
  real call needs the user's own keys (Pexels free key, AssemblyAI free key,
  Twitch app client ID/secret).
- The MCP server is verified with a real subprocess smoke test (initialize +
  tools/list + a tool call against a throwaway database), but no real MCP
  client (Claude Desktop) was connected here — the user must paste the config
  snippet and try it themselves.
- YouTube may block yt-dlp downloads without cookies (bot checks). A direct
  MP4 URL ingests fine, but YouTube downloads failed with "Requested format
  is not available" on the build machine **and on a home connection** (format
  listing works; the actual stream download is blocked — SABR/PO-token
  gating). Use the **browser-cookies** checkbox or a cookies.txt on the Fetch
  URL form (see above); if that fails, the task fails with a clear error and
  the video is not created — use a direct MP4 link or file upload instead.
  A real cookie-based YouTube fetch is **not verified here** (no YouTube
  login exists in the build environment); the user must try it with their
  own logged-in browser.
- Verified on Linux (Python 3.12) with cp314 wheel existence proven by
  download check. **Windows run confirmed 2026-11-20** (dashboard live at
  127.0.0.1:8012, demo login works). The Swagger Authorize button click is
  a manual check.
- No demo video is bundled (too big). Upload your own video to try it.

## Project layout

```
app/
  main.py            FastAPI app + /health + static dashboard
  video.py           ffmpeg engine: probe, silencedetect, scoring, render, merge, yt-dlp
  llm.py             Groq LLM clip selection (httpx, validated, local fallback)
  publish.py         YouTube/TikTok OAuth2 + resumable/direct upload, Instagram Graph API (httpx, token files in data/)
  pexels.py          Pexels stock B-roll search + download (httpx)
  transcribe.py      AssemblyAI cloud transcription option (httpx)
  twitch.py          Twitch Helix VOD listing (httpx, app token)
  mcp.py             MCP server over stdio JSON-RPC (no SDK)
  captions.py        SRT/TXT parsing, keywords, .ass builder, Whisper transcription
  auth.py            JWT + bcrypt + API-key auth
  tasks.py           background task store (stage + progress)
  seed.py            demo admin + SAMPLE videos/clips
  routers/           auth, videos, clips, accounts, publish
  static/            dashboard (index.html, style.css, app.js)
tests/               pytest smoke tests + LLM/publish tests + cookies tests + full-fledged tests (mocked, no real keys)
```
