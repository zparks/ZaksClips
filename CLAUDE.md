# ZaksClips (formerly Clipper)

Automates the post-stream clip workflow for a chess streamer. Repo: github.com/zparks/ZaksClips

## What this does

1. Fetches recent clips from Twitch API (day-by-day to catch all clips)
2. Calculates the 2-minute VOD window ending at each clip
3. Downloads segment (YouTube VOD via yt-dlp, or TikTok Live Center manual download)
4. Opens raw video for preview — user can adjust segment timing (+/- start/end)
5. Prompts for title and description/caption
6. Transcribes audio with Whisper (MLX, runs on M4)
7. Burns styled captions and title overlay into the video with ffmpeg
8. Copies finished video to iCloud Drive for phone access
9. Optionally uploads to YouTube Shorts and/or TikTok (each with explicit confirmation)
10. If upload fails, shows message to post from iCloud Drive on phone

## GUI (`python3 gui.py`)

Full desktop app wrapping clipper.py with customtkinter (dark mode).

### Home screen = My Videos
- No tabs — My Videos is the main screen, "Get Clips" opens the manual flow as an overlay
- Three sections: **Active** (drafts/approved/failed — always visible), **Scheduled** (expanded by default, collapsible), **Posted** (collapsed by default, expandable)
- Each video card shows: title, date/size/auto tag, progress dots (● Processed → ● Approved → ● Posted)
- Progress dot states: green ● = done, orange ● = needs attention, ◷ = scheduled (blue, shows date/time), ✕ = failed (red)
- Buttons adapt by status: "Review" (orange) for unreviewed, "Edit" (gray) for approved/scheduled, "Unschedule" (red) + "Edit" for scheduled
- "Get Clips" button adapts: prominent blue in off mode, gray "Get Clips Manually" in auto/auto-post mode
- Empty state adapts: big "Get Clips" CTA in off mode, "your clips will be processed at Xam" + "Process Now" in auto mode

### Schedule modes (top bar selector: off | auto | auto-post)
- **off** — no schedule, manual workflow only
- **auto** — batch processes clips daily, queues for review in My Videos (soft green banner)
- **auto-post** — batch processes AND uploads automatically (red danger banner)
- Time picker (hour + minute in 5-min increments) appears inline next to selector when auto/auto-post
- Schedule saved to `.env` as `SCHEDULE_HOUR` and `SCHEDULE_MINUTE`, launchd job auto-managed

### Processing flow (Get Clips overlay)
- Fetch clips → select (already-processed shown as "(already in My Videos)", unchecked by default) → process
- Steps are display-only progress bar (not clickable)
- Always interactive: trim → title → caption → burn → preview+edit → upload
- Preview+edit screen (`_preview_and_approve`): video player, editable title/caption, color theme picker, position sliders, re-burn, "Save as Default"
- Buttons: "Looks Good" → "Save for Later" → "Re-trim" → "Discard"
- After "Looks Good": "Upload Now" / "Schedule Post" / "Not Now"
- Schedule Post: date picker (14 days) + hour/30min time picker
- Returns to My Videos when done

### Review/Edit flow (from My Videos)
- Uses the same `_preview_and_approve` screen as processing — full editing capabilities
- Buttons adapt: "Close" normally, "Unschedule" for scheduled videos
- Re-burn uses raw file (never burns on top of already-burned output)
- On approve → upload choice → iCloud copy + upload + raw cleanup

### Video metadata (`.video_meta.json`)
- Tracks per-video: status, date, mode, title, caption, raw_path, youtube/tiktok flags, scheduled_time
- Statuses: `ready_for_review`, `approved`, `needs_upload`, `scheduled`, `posted`, `upload_failed`
- Written by both GUI and batch processor

### Other GUI features
- Embedded video player with synced audio (PyAV + sounddevice)
- All dialogs are inline overlays (`_show_overlay`/`_hide_overlay`)
- Help button: renders README.md inline
- Settings hot-reloads .env into clipper globals
- Escape closes overlays or cancels processing
- .app launcher at ~/Applications/ZaksClips.app (launches GUI directly)
- Deps: customtkinter, python-tk@3.14, av (PyAV), sounddevice, numpy, Pillow

### File organization
- `output/raw/` — downloads and trims (kept until user approves in review)
- `output/completed/` — finished captioned files
- iCloud copy happens on upload (not during batch processing)
- .ass and .part files auto-deleted after processing
- Raw files deleted after approval+upload; kept for re-trimming during review

## CLI commands

- `python3 clipper.py` — normal interactive flow
- `python3 clipper.py --batch` or `-b` — headless batch processing. Requires PLATFORM=youtube.
- `python3 clipper.py --post` — check for scheduled posts that are due and upload them
- `python3 clipper.py -r` — reprocess existing raw video (skip download)
- `python3 clipper.py -c` — clean/manage output folder
- `python3 clipper.py -s` — schedule auto-processing via launchd (runs `--batch` at configured time)
- `python3 clipper.py --unschedule` — cancel scheduled auto-processing
- `python3 clipper.py -s --test` — run batch processing immediately
- **Dock icon** — `~/Applications/ZaksClips.app` opens the GUI directly

## Batch processing (`--batch`)

- Runs headlessly — no terminal interaction needed
- Fetches clips, skips already-processed ones (`.processed_clips.json`)
- Downloads VOD via YouTube, transcribes with Whisper, generates AI title/caption
- Burns captions, writes to `.video_meta.json` with `status: "ready_for_review"`
- Does NOT copy to iCloud or delete raw files (user approves in GUI review)
- In `auto-post` mode: also uploads and copies to iCloud automatically
- Sends macOS notification when done
- Logs to `output/worker.log`
- Scheduled via GUI mode selector or `python3 clipper.py -s` (launchd: `com.zaksclips.reminder`)

## Scheduled posting (`--post`)

- Hourly launchd job (`com.zaksclips.poster`) checks `.video_meta.json` for `status: "scheduled"` videos
- Uploads videos whose `scheduled_time` has passed, copies to iCloud, updates status to `posted`
- Auto-installs when a video is scheduled, auto-removes when no scheduled videos remain
- Sends macOS notification when posts go out
- Logs to `output/poster.log`

## Stack

- Python 3 — packages installed to system Python via `pip3 install --user --break-system-packages` (NOT using the venv)
- `mlx-whisper` — Apple Silicon optimized Whisper large-v3, ~10-15s per clip
- `playwright` — persistent Chromium session for TikTok Live Center
- `ffmpeg` — burns captions (ASS format), title overlay, and local trimming
- `google-api-python-client` + `google-auth-oauthlib` — YouTube Shorts upload via OAuth2
- Twitch Helix API — fetches clips and VOD offsets
- YouTube Data API v3 — VOD lookup and Shorts upload
- TikTok Content Posting API — direct upload via OAuth2 + PKCE
- `customtkinter` — GUI framework (dark mode)
- `av` (PyAV) — video frame decoding for embedded preview
- `Pillow` — PIL image conversion for tkinter display
- `sounddevice` + `numpy` — audio playback in embedded preview (synced with PyAV demux)
- No paid services, runs entirely local

## Key decisions

- **No cropping needed** — user streams to TikTok Live simultaneously, TikTok saves the vertical version automatically
- **No CapCut automation** — CapCut is canvas-based, Playwright can't reliably automate it. Whisper + styled ffmpeg captions replace it entirely
- **2 minutes before clip end** — user's standard TikTok segment length, calculated from `vod_offset + duration - 120`
- **Persistent browser session** — saved to `.browser_session/`, user logs into TikTok once, stays logged in
- **Day-by-day clip fetching** — Twitch Helix clips endpoint sorts by views on wide date ranges and drops low-view clips. Fetching day-by-day ensures all clips are found.
- **YouTube VOD matching checks day before** — streams that start before midnight UTC produce clips timestamped the next day. VOD search matches both the clip date and the previous day.
- **Local trimming** — when shortening a segment (-N or e-N), ffmpeg crops locally instead of re-downloading. Only extends (+N or e+N) trigger a re-download.
- **iCloud Drive sync** — finished videos auto-copy to `~/Library/Mobile Documents/com~apple~CloudDocs/ZaksClips/` for phone access via Files app.

## Segment adjustment

After downloading, user can adjust timing before captions are burned:
- `+10` — grab 10 more seconds before start (extends, re-downloads)
- `-15` — trim 15 seconds from start (local crop)
- `e-10` — trim 10 seconds from end (local crop)
- `e+10` — extend 10 seconds past clip end (re-downloads)
- Combine with commas: `+10,e-15`
- Enter to keep as-is

## Caption & title styling

- **7 color themes** — Classic White, Classic Black, Electric Blue (default), Fire Red, Neon Green, Royal Purple, Gold. Stored as `COLOR_THEME` in `.env`. Theme color applies to both title box and caption outline. Text color auto-picks black or white for contrast.
- **Adjustable positions** — `TITLE_Y_PERCENT` (default 59) and `CAPTION_Y_PERCENT` (default 65) control vertical placement as % from top. Configurable via sliders in both the preview/edit screen and Settings.
- **Per-clip style controls** — Color theme, title height, and caption height are editable on the preview/approve screen. Changing any triggers the "Re-burn" button. "Save as Default" persists style to `.env` for future clips. Settings page still shows/edits defaults.
- **Captions are one line max** — Whisper word-level timestamps chunked at ~30 chars, no long wrapping segments
- **No punctuation in captions** — commas, periods, etc. stripped via regex
- **All caps** — both captions and title are uppercased
- **Title shows first 5 seconds only** — `enable='between(t,0,5)'` on drawtext, acts as thumbnail text
- **Title wraps** — long titles auto-wrap at ~14 chars per line, stacked vertically with overlapping boxes so they appear as one connected block. Two-pass ffmpeg rendering (boxes first, then text) prevents lower boxes from covering upper text.
- **Font** — Arial Bold from macOS system fonts, fontsize 72 for title, 88 for captions

## Output file naming

Files are named `Title_MM-DD.mp4` (e.g. `Sunday_Funday_03-29.mp4`). No year in filename.
Raw downloads use `yt_raw_` prefix, local trims use `trimmed_` prefix.

## Files

```
ZaksClips/
├── clipper.py            — main script
├── setup.sh              — one-time install (ffmpeg, mlx-whisper, playwright)
├── .env.example          — credential template
├── .env                  — your actual credentials (never commit this)
├── .browser_session/     — saved TikTok login session
├── .youtube_token.json   — saved YouTube OAuth2 token (auto-refreshes)
├── .tiktok_token.json    — saved TikTok OAuth2 token (auto-refreshes)
├── .processed_clips.json — tracks which clip IDs have been processed (green "done" label)
├── output/               — finished .mp4s land here
└── docs/                 — GitHub Pages site (ToS, privacy, icon, TikTok verification)
```

## Setup (already done if you ran setup.sh)

```bash
bash setup.sh
```

Requires:
- `TWITCH_CLIENT_ID` — from dev.twitch.tv/console
- `TWITCH_CLIENT_SECRET` — from dev.twitch.tv/console
- `TWITCH_USERNAME` — your Twitch channel name

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
- Three confirmation gates: 1) preview video and confirm it's good, 2) YouTube upload, 3) TikTok upload
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

- Uses TikTok Content Posting API via OAuth2 with PKCE
- **PKCE note**: TikTok uses hex-encoded SHA256 for code_challenge (NOT base64url like standard PKCE)
- Requires `TIKTOK_CLIENT_KEY` and `TIKTOK_CLIENT_SECRET` in `.env` (from developers.tiktok.com)
- First run opens browser for TikTok auth, saves token to `.tiktok_token.json`
- Access tokens last 24h, refresh tokens last 365 days — auto-refreshes
- Redirect URI: `http://localhost:3000/callback/` (must match TikTok developer portal exactly)
- **Currently using sandbox credentials** — production app submitted for review (5-10 business days)
- Unaudited production apps get `unauthorized_client` error on OAuth — must use sandbox with test users, or pass review
- Once review passes: swap `.env` back to production keys (`awjzj5ky1gunw3sh` / `Xga3Iz1bHh9HZ6IRNBOWon8e8dtCUhYK`)
- Sandbox credentials: `sbawcyrvxsaacdhwl8` / `igEozUD5LId0TqjkIt9WDNKtWrjWatIT`
- TikTok developer app name: "ZaksClips"
- Domain verification file at `docs/tiktok9rb6Eu7UOqB9QfIDGjPqVP1nBX2U6r6G.txt`
- GitHub Pages site for ToS/privacy: `https://zparks.github.io/ZaksClips/`
- Chunked upload: files under 64MB upload as single chunk; larger files use 10MB chunks
- 5MB minimum chunk size only applies to non-final chunks in multi-chunk uploads

## AI title & caption generation

- Uses Claude Haiku (`claude-haiku-4-5-20251001`) via `ANTHROPIC_API_KEY` in `.env`
- After Whisper transcribes, sends transcript to Claude to generate a punchy ALL CAPS title and a quirky caption ending with #chess
- `clipper.generate_title_caption(transcript_text, clip_title)` returns `(title, caption)` or `(None, None)` if no API key
- `clipper.get_transcript_text(whisper_result)` extracts plain text from Whisper result
- In normal mode: AI title/caption become the default suggestion in the title/caption dialogs (user can override)
- In danger mode: AI title/caption are used automatically with no prompts
- Falls back to Twitch clip title if no API key or if generation fails
- `anthropic` package installed via pip (not in venv)
