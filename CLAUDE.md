# Chess Clip Automator

Automates the post-stream TikTok clip workflow for a chess streamer.

## What this does

1. Fetches recent clips from Twitch API
2. Calculates the 2-minute VOD window ending at each clip
3. Downloads segment (YouTube VOD via yt-dlp, or TikTok Live Center manual download)
4. Prompts for title and description/caption
5. Transcribes audio with Whisper (MLX, runs on M4)
6. Burns styled captions + title into the video with ffmpeg
7. Opens finished video for preview — user must confirm it's good
8. Optionally uploads to YouTube Shorts (requires explicit confirmation)

## Stack

- Python 3 — packages installed to system Python via `pip3 install --user --break-system-packages` (NOT using the venv)
- `mlx-whisper` — Apple Silicon optimized Whisper large-v3, ~10-15s per clip
- `playwright` — persistent Chromium session for TikTok Live Center
- `ffmpeg` — burns captions (ASS format) and title overlay
- `google-api-python-client` + `google-auth-oauthlib` — YouTube Shorts upload via OAuth2
- Twitch Helix API — fetches clips and VOD offsets
- YouTube Data API v3 — VOD lookup and Shorts upload
- No paid services, runs entirely local

## Key decisions

- **No cropping needed** — user streams to TikTok Live simultaneously, TikTok saves the vertical version automatically
- **No CapCut automation** — CapCut is canvas-based, Playwright can't reliably automate it. Whisper + styled ffmpeg captions replace it entirely
- **2 minutes before clip end** — user's standard TikTok segment length, calculated from `vod_offset + duration - 120`
- **Persistent browser session** — saved to `.browser_session/`, user logs into TikTok once, stays logged in

## Caption & title styling

- **Captions position** — 35% up from bottom (`MarginV=672` on 1920px canvas)
- **Captions are one line max** — Whisper word-level timestamps chunked at ~30 chars, no long wrapping segments
- **No punctuation in captions** — commas, periods, etc. stripped via regex
- **All caps** — both captions and title are uppercased
- **Title shows first 5 seconds only** — `enable='between(t,0,5)'` on drawtext, acts as thumbnail text
- **Title wraps** — long titles auto-wrap at ~14 chars per line, stacked vertically with overlapping blue boxes so they appear as one connected block
- **Title position** — centered vertically around `y=h*0.59`, sits over the captions for the first 5 seconds
- **Title background** — solid blue `#1D8CD7` (between sky and electric blue), `boxborderw=20`, lines spaced at fontsize only so boxes overlap and merge
- **Font** — Arial Bold from macOS system fonts, fontsize 72 for title, 88 for captions

## Files

```
clipper/
├── clipper.py            — main script
├── setup.sh              — one-time install (ffmpeg, mlx-whisper, playwright)
├── .env.example          — credential template
├── .env                  — your actual credentials (never commit this)
├── .browser_session/     — saved TikTok login session
├── .youtube_token.json   — saved YouTube OAuth2 token (auto-refreshes)
└── output/               — finished .mp4s land here
```

## Setup (already done if you ran setup.sh)

```bash
bash setup.sh
```

Requires:
- `TWITCH_CLIENT_ID` — from dev.twitch.tv/console
- `TWITCH_CLIENT_SECRET` — from dev.twitch.tv/console
- `TWITCH_USERNAME` — your Twitch channel name

## Usage

```bash
python3 clipper.py
```

## TikTok Live Center URL

`https://livecenter.tiktok.com/replay?lang=en`

## Environment notes

- Requires `ffmpeg-full` (not default `ffmpeg`) — Homebrew's default formula lacks libass and libfreetype (no `ass`, `subtitles`, or `drawtext` filters). Script prepends `/opt/homebrew/opt/ffmpeg-full/bin` to PATH.
- ffmpeg 8.x `drawtext` filter does NOT support `bold=` option — use a bold font file instead.
- Python 3.14 (Homebrew) — deps installed via `pip3 install --user --break-system-packages`. If brew upgrades Python again, all pip packages will need reinstalling.
- Dependencies: `requests`, `python-dotenv`, `mlx_whisper`, `playwright`, `google-auth-oauthlib`, `google-api-python-client`
- Installing new pip packages can sometimes break existing ones (dependency conflicts) — if `mlx_whisper` goes missing, reinstall all deps: `pip3 install --user --break-system-packages -r requirements.txt`

## YouTube Shorts upload

- Uses OAuth2 (not just API key) — requires `YOUTUBE_CLIENT_ID` and `YOUTUBE_CLIENT_SECRET` in `.env`
- Get these from Google Cloud Console → APIs & Services → Credentials → Create OAuth 2.0 Client ID (Desktop app type)
- Must enable "YouTube Data API v3" on the Google Cloud project
- First run opens browser for Google auth, saves token to `.youtube_token.json` (auto-refreshes after that)
- Two confirmation gates: 1) preview video and confirm it's good, 2) confirm upload
- Never uploads without explicit approval
- Description/caption is prompted alongside title — defaults to just the title if skipped
- Uploads as public, category "Gaming", tagged with Shorts and chess
- Google Cloud project name: "ZaksClips" — OAuth app is in Testing mode, `zak4htr@gmail.com` is added as test user
- Google Cloud Console credentials page: console.cloud.google.com/apis/credentials

## Clip filtering (env vars)

- **`SHOW_AUTOCLIPS`** (`true`/`false`, default `true`) — When `false`, filters out Twitch autoclips by comparing each clip's title to its VOD/stream title. Manual clips keep the stream title as-is; autoclips get transcript-generated titles that won't match. This relies on the user never editing clip titles.
- **`SHOW_OTHERS_CLIPS`** (`true`/`false`, default `true`) — When `false`, only shows clips where `creator_name` matches `TWITCH_USERNAME`. Filters out clips made by viewers.
- **`CLIP_DAYS`** (integer, default `7`) — How many days back to search for clips.
- Twitch Helix API has no dedicated field for autoclips — `creator_name`, `creator_id`, and `is_featured` are identical between manual and autoclips by the same user. Title comparison against the VOD title is the only reliable heuristic.
- VOD titles are fetched via `GET https://api.twitch.tv/helix/videos?id=` using the clip's `video_id` field, batched in groups of 100.

## TikTok upload

- Uses TikTok Content Posting API via OAuth2
- Requires `TIKTOK_CLIENT_KEY` and `TIKTOK_CLIENT_SECRET` in `.env` (from developers.tiktok.com)
- First run opens browser for TikTok auth, saves token to `.tiktok_token.json`
- Access tokens last 24h, refresh tokens last 365 days — auto-refreshes
- Redirect URI: `http://localhost:3000/callback/` (must match TikTok developer portal exactly)
- App is in draft/unaudited mode — uploads are private (SELF_ONLY) until audit passes
- User can manually flip videos to public in the TikTok app after upload
- Three confirmation gates: 1) preview video, 2) YouTube upload, 3) TikTok upload
- TikTok developer app name: "ZaksClips"
- Domain verification file at `docs/tiktok9rb6Eu7UOqB9QfIDGjPqVP1nBX2U6r6G.txt`
- GitHub Pages site for ToS/privacy: `https://zparks.github.io/ZaksClips/`

## Potential improvements not yet built

- Auto-generate title from Whisper transcript using an LLM
- Watch for new clips automatically after a stream ends
