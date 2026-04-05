# ZaksClips

Automatically turn your Twitch stream clips into captioned vertical videos for YouTube Shorts and TikTok. No subscriptions, no cloud — everything runs on your Mac.

## What it does

1. Fetches your recent Twitch clips
2. Downloads the matching VOD segment (from YouTube or TikTok Live Center)
3. Lets you trim/extend the segment with a visual editor
4. Transcribes audio using Whisper (runs locally on Apple Silicon)
5. Burns styled captions and a title overlay into the video
6. Copies the finished video to iCloud Drive for phone access
7. Optionally uploads directly to YouTube Shorts and/or TikTok

## Requirements

- macOS with Apple Silicon (M1/M2/M3/M4) — needed for MLX Whisper
- Python 3.12+
- Homebrew

## Quick Start

### 1. Install dependencies

```bash
cd ZaksClips
bash setup.sh
```

This installs ffmpeg, MLX Whisper, Playwright, and other dependencies.

### 2. Set up your API credentials

Open the app and click **Settings**, or copy `.env.example` to `.env` and fill in the values.

You need at minimum:

#### Twitch (required)

1. Go to [dev.twitch.tv/console](https://dev.twitch.tv/console) and create a new application
2. Set the OAuth Redirect URL to `http://localhost:3000/callback/`
3. Copy your **Client ID** and generate a **Client Secret**
4. Enter your Twitch username

#### YouTube (for VOD downloads + Shorts uploads)

1. Go to [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
2. Create a project (or use an existing one)
3. Enable **YouTube Data API v3**
4. Create an **API Key** (for VOD lookup)
5. Create an **OAuth 2.0 Client ID** — choose "Desktop app" type (for uploads)
6. Copy the Client ID and Client Secret
7. Enter your YouTube channel handle (e.g. `pakzarks`)

First time you upload, a browser window will open for Google sign-in. After that, the token auto-refreshes.

#### TikTok (optional, for direct uploads)

1. Go to [developers.tiktok.com](https://developers.tiktok.com) and create an app
2. Enable **Content Posting API**
3. Copy your **Client Key** and **Client Secret**
4. Set the redirect URI to `http://localhost:3000/callback/`
5. Add yourself as a sandbox test user (until the app passes review)

#### Anthropic (optional, for AI titles)

1. Go to [console.anthropic.com](https://console.anthropic.com) and get an API key
2. Enter it in Settings under "AI Title Generation"
3. When enabled, AI auto-generates punchy titles and captions from your clip's transcript

### 3. Run the app

```bash
python3 gui.py
```

Or double-click `ZaksClips.app` if you've set up the dock icon.

## How to use

1. Click **Fetch Clips** — loads your recent clips from Twitch
2. Check the clips you want to process
3. Click **Process Selected**
4. For each clip:
   - **Preview** the raw video (plays inside the app with audio)
   - **Trim** the segment using the slider, or extend it with the +30s buttons
   - Enter a **title** and **caption** (or accept the AI-generated defaults)
   - Wait for captions to burn (usually 15-30 seconds)
   - **Preview** the final video with captions
   - **Approve** and choose where to upload (YouTube, TikTok, or just iCloud)
   - Not happy? Click **Redo** to go back and change the title, captions, or trim
5. Find your finished videos in the **My Videos** tab or in iCloud Drive on your phone

## Settings Reference

All settings are configured in the **Settings** panel (click the button in the top-right corner). Changes apply immediately — no restart needed.

### Clip Settings

| Setting | Default | Description |
|---------|---------|-------------|
| **VOD Platform** | `tiktok` | Where your VODs are saved — `youtube` or `tiktok`. Determines how the app downloads the raw VOD segment. |
| **Days to Search** | `3` | How many days back to look for clips (1–60). Also adjustable with the ▲/▼ buttons next to "Fetch Last N Days" on the main screen. |
| **Clip Length** | `2` | Minutes of VOD to grab per clip (0.5–5). This is how much video before the clip's end point gets downloaded. |
| **Show Autoclips** | `true` | Include Twitch's auto-generated clips in results. Set to `false` to only see manually-created clips. |
| **Show Viewers' Clips** | `true` | Include clips made by your viewers. Set to `false` to only see clips you created yourself. |

### Processing Mode

Controls how much automation you want during clip processing. Choose one of three modes:

| Mode | What it does |
|------|-------------|
| **Safe** (default) | Full manual control — you review and confirm every step: trim, title, captions, preview, and upload. |
| **Auto** | Hands-off processing — AI auto-generates titles and captions, skips the trim step. Still shows a preview and asks before uploading. |
| **Danger** | Full automation — AI titles, auto-captions, auto-upload. No confirmations at all. Use with caution. |

A colored banner appears at the top of the app when you're in Auto (orange) or Danger (red) mode so you always know which mode is active.

### AI Title Generation

| Setting | Description |
|---------|-------------|
| **Anthropic API Key** | Your API key from [console.anthropic.com](https://console.anthropic.com). When set, the app sends each clip's transcript to Claude to generate a punchy ALL CAPS title and a quirky caption ending with `#chess`. In Safe mode, the AI suggestion is pre-filled but you can edit it. In Auto/Danger mode, it's used automatically. If no key is set, falls back to the Twitch clip title. |

### Credential Status

The colored indicators below the title bar show connection status at a glance:
- **Green** = credentials are configured
- **Red** = credentials are missing

Twitch credentials are required. YouTube and TikTok are optional — you can always export to iCloud Drive instead.

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| **Enter** | Continue / confirm on any dialog |
| **Escape** | Close the current overlay (settings, help) or cancel processing |

## Output

Finished videos are saved to `output/completed/` and automatically copied to iCloud Drive at:

```
~/Library/Mobile Documents/com~apple~CloudDocs/ZaksClips/
```

Open the **Files** app on your iPhone to find them and post from your phone.

Files are named `Title_MM-DD.mp4` (e.g. `Sunday_Funday_03-29.mp4`).

## Troubleshooting

**"No clips with VOD data found"**
- Your VODs might not be saved, or the clips are from a deleted VOD. Check that VOD saving is enabled on Twitch.

**YouTube upload fails**
- Make sure your Google Cloud project has YouTube Data API v3 enabled
- Delete `.youtube_token.json` and re-authenticate if the token is stale

**TikTok says "unauthorized_client"**
- Your TikTok app needs to pass review for production use. Until then, use sandbox credentials and add yourself as a test user.

**Captions look wrong**
- Make sure you have `ffmpeg-full` installed (not the default `ffmpeg`). Run `brew install ffmpeg-full` if needed — the default formula is missing libass and text rendering filters.

**"No module named X"**
- Dependencies are installed to your system Python. Run: `pip3 install --user --break-system-packages -r requirements.txt`
