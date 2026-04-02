#!/usr/bin/env python3
"""
Chess Clip Automator
Fetches your Twitch clips, calculates VOD timestamps, opens TikTok Live Center
at the right moment, then transcribes + burns captions and title into the video.
"""

import os
import sys
import time
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta

import re

import requests
from dotenv import load_dotenv

load_dotenv()

# Ensure Homebrew binaries (ffmpeg, yt-dlp) are on PATH
# Prefer ffmpeg-full (has libass, drawtext) over the stripped-down ffmpeg
os.environ["PATH"] = "/opt/homebrew/opt/ffmpeg-full/bin:/opt/homebrew/bin:" + os.environ.get("PATH", "")

TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
TWITCH_USERNAME = os.getenv("TWITCH_USERNAME")
PLATFORM = os.getenv("PLATFORM", "tiktok").lower()  # "tiktok" or "youtube"
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
YOUTUBE_CHANNEL_HANDLE = os.getenv("YOUTUBE_CHANNEL_HANDLE", "")
YOUTUBE_CLIENT_ID = os.getenv("YOUTUBE_CLIENT_ID", "")
YOUTUBE_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET", "")
SHOW_AUTOCLIPS = os.getenv("SHOW_AUTOCLIPS", "true").lower() == "true"
SHOW_OTHERS_CLIPS = os.getenv("SHOW_OTHERS_CLIPS", "true").lower() == "true"
CLIP_DAYS = int(os.getenv("CLIP_DAYS", "7"))

OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

BROWSER_DATA_DIR = Path(__file__).parent / ".browser_session"
YOUTUBE_TOKEN_PATH = Path(__file__).parent / ".youtube_token.json"

TIKTOK_LIVE_CENTER_URL = "https://livecenter.tiktok.com/replay?lang=en"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def fmt_time(seconds):
    """Format seconds as H:MM:SS or M:SS."""
    seconds = int(seconds)
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def seconds_to_ass(s):
    """Convert float seconds to ASS timestamp format H:MM:SS.cs"""
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    cs = round((s % 1) * 100)
    sec = int(s % 60)
    return f"{h}:{m:02d}:{sec:02d}.{cs:02d}"


# ─── Twitch API ───────────────────────────────────────────────────────────────

def get_twitch_token():
    resp = requests.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id": TWITCH_CLIENT_ID,
            "client_secret": TWITCH_CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_user_id(token):
    resp = requests.get(
        "https://api.twitch.tv/helix/users",
        headers={
            "Client-ID": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {token}",
        },
        params={"login": TWITCH_USERNAME},
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    if not data:
        print(f"Error: Twitch user '{TWITCH_USERNAME}' not found.")
        sys.exit(1)
    return data[0]["id"]


def _filter_autoclips(clips, token):
    """Remove autoclips by comparing clip titles to their VOD titles.
    Manual clips keep the stream title; autoclips get transcript-based titles."""
    video_ids = list({c["video_id"] for c in clips if c.get("video_id")})
    if not video_ids:
        return clips
    vod_titles = {}
    for i in range(0, len(video_ids), 100):
        batch = video_ids[i:i + 100]
        resp = requests.get(
            "https://api.twitch.tv/helix/videos",
            headers={
                "Client-ID": TWITCH_CLIENT_ID,
                "Authorization": f"Bearer {token}",
            },
            params={"id": batch},
        )
        resp.raise_for_status()
        for v in resp.json()["data"]:
            vod_titles[v["id"]] = v["title"]
    return [c for c in clips if c.get("video_id") in vod_titles
            and c["title"] == vod_titles[c["video_id"]]]


def get_recent_clips(token, user_id, count=10, days=CLIP_DAYS):
    started_at = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    resp = requests.get(
        "https://api.twitch.tv/helix/clips",
        headers={
            "Client-ID": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {token}",
        },
        params={"broadcaster_id": user_id, "first": 50, "started_at": started_at},
    )
    resp.raise_for_status()
    clips = resp.json()["data"]
    if not SHOW_OTHERS_CLIPS:
        clips = [c for c in clips if c.get("creator_name", "").lower() == TWITCH_USERNAME.lower()]
    if not SHOW_AUTOCLIPS:
        clips = _filter_autoclips(clips, token)
    clips.sort(key=lambda c: c["created_at"], reverse=True)
    return clips[:count]


# ─── Clip selection ───────────────────────────────────────────────────────────

def pick_clips(clips):
    print("\nYour recent Twitch clips:\n")
    usable = []
    for clip in clips:
        if clip.get("vod_offset") is None:
            continue  # skip clips with no VOD offset
        usable.append(clip)

    if not usable:
        print("No clips with VOD data found. Clips need a VOD offset to calculate the timestamp.")
        sys.exit(0)

    for i, clip in enumerate(usable):
        vod_end = clip["vod_offset"] + clip["duration"]
        seg_start = max(0, vod_end - 120)
        print(f"  [{i+1}] {clip['title']}")
        print(f"       Created : {clip['created_at'][:10]}")
        print(f"       Segment : {fmt_time(seg_start)} → {fmt_time(vod_end)}  (2 min ending at clip)")
        print()

    print("Which clips do you want to process? (e.g. 1,3 or 'all')")
    choice = input("> ").strip().lower()

    if choice == "all":
        return usable

    indices = [int(x.strip()) - 1 for x in choice.split(",") if x.strip().isdigit()]
    return [usable[i] for i in indices if 0 <= i < len(usable)]


# ─── TikTok Live Center download ──────────────────────────────────────────────

def download_from_tiktok(vod_end_seconds):
    """
    Opens TikTok Live Center in a persistent browser session.
    Displays the exact timestamp to seek to.
    Waits for a new .mp4 to appear in ~/Downloads and returns its path.
    """
    from playwright.sync_api import sync_playwright

    tiktok_start = max(0, vod_end_seconds - 120)
    tiktok_end = vod_end_seconds

    print(f"\n  Target segment: {fmt_time(tiktok_start)} → {fmt_time(tiktok_end)}")
    print("  Opening TikTok Live Center in browser...")

    downloads_dir = Path.home() / "Downloads"
    files_before = set(downloads_dir.glob("*.mp4"))

    BROWSER_DATA_DIR.mkdir(exist_ok=True)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_DATA_DIR),
            headless=False,
            viewport={"width": 1280, "height": 860},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(TIKTOK_LIVE_CENTER_URL, wait_until="domcontentloaded")

        print()
        print("  ┌─────────────────────────────────────────────┐")
        print(f"  │  Seek to:  {fmt_time(tiktok_start)}  →  {fmt_time(tiktok_end)}          │")
        print("  │  Download that 2-minute segment             │")
        print("  │  Save it to your Downloads folder           │")
        print("  └─────────────────────────────────────────────┘")
        print()
        print("  Watching Downloads folder for new .mp4... (Ctrl+C to cancel)")

        downloaded_file = None
        while not downloaded_file:
            time.sleep(2)
            files_after = set(downloads_dir.glob("*.mp4"))
            new_files = files_after - files_before
            if new_files:
                downloaded_file = max(new_files, key=lambda f: f.stat().st_mtime)
                print(f"\n  ✓ Picked up: {downloaded_file.name}")

        ctx.close()

    return downloaded_file


# ─── YouTube API ──────────────────────────────────────────────────────────────

def get_youtube_channel_id():
    resp = requests.get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={
            "part": "id",
            "forHandle": YOUTUBE_CHANNEL_HANDLE.lstrip("@"),
            "key": YOUTUBE_API_KEY,
        },
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    if not items:
        print(f"Error: YouTube channel '{YOUTUBE_CHANNEL_HANDLE}' not found.")
        sys.exit(1)
    return items[0]["id"]


def find_youtube_vod(channel_id, clip_created_at):
    """Find a livestream VOD by matching actualStartTime date to the clip's date."""
    clip_date = clip_created_at[:10]  # "2026-03-29"

    # Step 1: get recent video IDs from the channel
    search_resp = requests.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={
            "part": "id",
            "channelId": channel_id,
            "type": "video",
            "order": "date",
            "maxResults": 10,
            "key": YOUTUBE_API_KEY,
        },
    )
    search_resp.raise_for_status()
    video_ids = [item["id"]["videoId"] for item in search_resp.json().get("items", [])]

    if not video_ids:
        return []

    # Step 2: fetch liveStreamingDetails for those videos
    details_resp = requests.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={
            "part": "snippet,liveStreamingDetails",
            "id": ",".join(video_ids),
            "key": YOUTUBE_API_KEY,
        },
    )
    details_resp.raise_for_status()
    videos = details_resp.json().get("items", [])

    # Step 3: match on actualStartTime date
    matches = []
    for v in videos:
        start = v.get("liveStreamingDetails", {}).get("actualStartTime", "")
        if start[:10] == clip_date:
            matches.append({
                "id": {"videoId": v["id"]},
                "snippet": v["snippet"],
            })
    return matches


# ─── YouTube VOD download ─────────────────────────────────────────────────────

def download_from_youtube(vod_end_seconds, clip_created_at):
    """
    Looks up the YouTube VOD matching the clip date, then downloads
    the 2-minute segment using yt-dlp. Fully automated.
    """
    yt_start = max(0, vod_end_seconds - 120)
    yt_end = vod_end_seconds

    print("\n  Looking up YouTube VOD for this clip's date...")
    channel_id = get_youtube_channel_id()
    vods = find_youtube_vod(channel_id, clip_created_at)

    if not vods:
        print("  No YouTube VOD found for this date. Paste the URL manually:")
        vod_url = input("  > ").strip()
    else:
        # Pick the VOD whose publishedAt is closest to the clip time
        clip_dt = datetime.fromisoformat(clip_created_at.replace("Z", "+00:00"))
        def vod_distance(v):
            pub = datetime.fromisoformat(v["snippet"]["publishedAt"].replace("Z", "+00:00"))
            return abs((pub - clip_dt).total_seconds())
        best = min(vods, key=vod_distance)
        vod_id = best["id"]["videoId"]
        vod_title = best["snippet"]["title"]
        vod_url = f"https://www.youtube.com/watch?v={vod_id}"
        print(f"  ✓ Found: {vod_title}")

    print(f"\n  Downloading segment {fmt_time(yt_start)} → {fmt_time(yt_end)} from YouTube...")

    def to_yt_ts(s):
        s = int(s)
        h, r = divmod(s, 3600)
        m, sec = divmod(r, 60)
        if h:
            return f"{h}:{m:02d}:{sec:02d}"
        return f"{m}:{sec:02d}"

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = OUTPUT_DIR / f"yt_raw_{stamp}.mp4"
    section = f"*{to_yt_ts(yt_start)}-{to_yt_ts(yt_end)}"

    cmd = [
        "yt-dlp",
        "--download-sections", section,
        "--merge-output-format", "mp4",
        "-o", str(output_path),
        vod_url,
    ]

    result = subprocess.run(cmd, text=True)
    if result.returncode != 0:
        print("\n  yt-dlp failed — check the URL and try again.")
        sys.exit(1)

    print(f"\n  ✓ Downloaded: {output_path.name}")
    return output_path


# ─── Whisper transcription ────────────────────────────────────────────────────

def transcribe(video_path):
    print("\n  Transcribing with Whisper large-v3 (MLX)...")
    import mlx_whisper

    result = mlx_whisper.transcribe(
        str(video_path),
        path_or_hf_repo="mlx-community/whisper-large-v3-mlx",
        word_timestamps=True,
        language="en",
    )
    print(f"  ✓ Transcribed {len(result.get('segments', []))} segments")
    return result


# ─── ASS subtitle generation ──────────────────────────────────────────────────

ASS_HEADER = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 1

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,88,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,1,0,0,0,100,100,2,0,1,5,2,2,60,60,672,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def make_ass(result, output_path, max_chars=30):
    """Build ASS subtitles from word-level timestamps, one short line at a time."""
    lines = []
    for seg in result.get("segments", []):
        words = seg.get("words", [])
        if not words:
            # Fallback if no word timestamps
            start = seconds_to_ass(seg["start"])
            end = seconds_to_ass(seg["end"])
            text = seg["text"].strip().upper()
            lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")
            continue

        chunk_words = []
        chunk_start = None
        chunk_len = 0

        for w in words:
            word = re.sub(r'[^\w\s]', '', w["word"].strip())
            if not word:
                continue
            new_len = chunk_len + len(word) + (1 if chunk_words else 0)

            if chunk_words and new_len > max_chars:
                # Flush current chunk
                start = seconds_to_ass(chunk_start)
                end = seconds_to_ass(w["start"])
                text = " ".join(chunk_words).upper()
                lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")
                chunk_words = []
                chunk_len = 0

            if not chunk_words:
                chunk_start = w["start"]
            chunk_words.append(word)
            chunk_len = chunk_len + len(word) + (1 if chunk_len > 0 else 0)

        # Flush remaining words
        if chunk_words:
            start = seconds_to_ass(chunk_start)
            end = seconds_to_ass(words[-1].get("end", seg["end"]))
            text = " ".join(chunk_words).upper()
            lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(ASS_HEADER)
        f.write("\n".join(lines))

    print(f"  ✓ Subtitles written ({len(lines)} lines)")


# ─── ffmpeg: burn captions + title ───────────────────────────────────────────

def wrap_title(title, max_chars=14):
    """Wrap title into lines of max_chars, breaking at word boundaries."""
    words = title.upper().split()
    lines = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > max_chars:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}" if current else word
    if current:
        lines.append(current)
    return lines


def burn_captions(video_path, ass_path, title, output_path):
    print("  Burning captions and title with ffmpeg...")

    # Use a system font guaranteed to be on macOS
    font_path = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
    if not Path(font_path).exists():
        font_path = "/System/Library/Fonts/Helvetica.ttc"

    safe_ass_path = str(ass_path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")

    # Wrap title into multiple lines, stack drawtext filters vertically
    # Line spacing = fontsize only (no gap), so the boxborderw (20px) overlaps
    # between lines, creating one connected blue block
    title_lines = wrap_title(title)
    fontsize = 72
    box_pad = 20
    line_height = fontsize  # boxes overlap by 2*box_pad, looks connected
    total_height = len(title_lines) * line_height
    base_y = f"(h*0.59)-{total_height // 2}"

    title_filters = ""
    for i, line in enumerate(title_lines):
        safe_line = line.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:").replace("%", "\\%")
        y_expr = f"{base_y}+{i * line_height}"
        title_filters += (
            f",drawtext=text='{safe_line}'"
            f":fontfile='{font_path}'"
            f":fontcolor=white"
            f":fontsize={fontsize}"
            f":x=(w-tw)/2"
            f":y={y_expr}"
            f":box=1"
            f":boxcolor=#1D8CD7"
            f":boxborderw={box_pad}"
            f":enable='between(t,0,5)'"
        )

    vf = f"ass='{safe_ass_path}'{title_filters}"

    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-vf", vf,
        "-c:a", "copy",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "18",
        str(output_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("\n  ffmpeg error:")
        print(result.stderr[-2000:])
        sys.exit(1)

    print(f"  ✓ Saved: {output_path.name}")


# ─── YouTube OAuth & Upload ──────────────────────────────────────────────────

def get_youtube_credentials():
    """Get OAuth2 credentials for YouTube upload. Launches browser auth on first run."""
    import json
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.oauth2.credentials import Credentials

    scopes = ["https://www.googleapis.com/auth/youtube.upload"]

    # Try loading saved token
    if YOUTUBE_TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(YOUTUBE_TOKEN_PATH), scopes)
        if creds.valid:
            return creds
        if creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            with open(YOUTUBE_TOKEN_PATH, "w") as f:
                f.write(creds.to_json())
            return creds

    if not YOUTUBE_CLIENT_ID or not YOUTUBE_CLIENT_SECRET:
        print("\n  YouTube upload requires OAuth2 credentials.")
        print("  Add YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET to your .env")
        print("  (from Google Cloud Console → APIs & Services → Credentials → OAuth 2.0 Client ID)")
        return None

    # Build client config from env vars
    client_config = {
        "installed": {
            "client_id": YOUTUBE_CLIENT_ID,
            "client_secret": YOUTUBE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, scopes)
    print("\n  Opening browser for YouTube authorization (one-time setup)...")
    creds = flow.run_local_server(port=0)

    with open(YOUTUBE_TOKEN_PATH, "w") as f:
        f.write(creds.to_json())
    print("  ✓ YouTube credentials saved")

    return creds


def upload_to_youtube(video_path, title, description):
    """Upload a video to YouTube as a Short. Returns the video URL or None."""
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = get_youtube_credentials()
    if not creds:
        return None

    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": ["Shorts", "chess"],
            "categoryId": "20",  # Gaming
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(str(video_path), mimetype="video/mp4", resumable=True)

    print("\n  Uploading to YouTube Shorts...")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            pct = int(status.progress() * 100)
            print(f"  ⬆ {pct}%", end="\r")

    video_id = response["id"]
    video_url = f"https://youtube.com/shorts/{video_id}"
    print(f"\n  ✓ Uploaded: {video_url}")
    return video_url


# ─── Preview & Upload ───────────────────────────────────────────────────────

def preview_and_upload(output_path, title, description):
    """Open video for review, then optionally upload to YouTube Shorts."""
    # Gate 1: Preview the video
    print("\n  Opening video for preview...")
    subprocess.run(["open", str(output_path)])

    print("\n  Is this video good? (y/n)")
    if input("  > ").strip().lower() != "y":
        print("  ✗ Video rejected — not uploading.")
        return

    # Gate 2: Confirm upload
    print("\n  Upload to YouTube Shorts? (y/n)")
    if input("  > ").strip().lower() != "y":
        print("  ✗ Skipping upload.")
        return

    url = upload_to_youtube(output_path, title, description)
    if url:
        print(f"\n  🎬 Live at: {url}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def reprocess():
    """Skip download — pick an existing raw file and redo transcription + burn."""
    print()
    print("  Chess Clip Automator — Reprocess Mode")
    print("  ══════════════════════════════════════")

    raw_files = sorted(OUTPUT_DIR.glob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)
    # Exclude already-finished tiktok_ outputs
    raw_files = [f for f in raw_files if not f.name.startswith("tiktok_")]

    if not raw_files:
        print("\n  No raw .mp4 files found in output/")
        sys.exit(1)

    print("\n  Raw files in output/ (newest first):\n")
    for i, f in enumerate(raw_files):
        print(f"  [{i+1}] {f.name}")

    print("\n  Which file? (Enter for most recent)")
    choice = input("  > ").strip()
    if choice == "":
        video_path = raw_files[0]
    elif choice.isdigit() and 1 <= int(choice) <= len(raw_files):
        video_path = raw_files[int(choice) - 1]
    else:
        print("  Invalid choice.")
        sys.exit(1)

    print(f"\n  Using: {video_path.name}")

    print("\n  Title:")
    title = input("  > ").strip()
    if not title:
        title = video_path.stem

    print("\n  Description / caption (Enter to skip):")
    description = input("  > ").strip()
    if not description:
        description = title

    result = transcribe(video_path)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    ass_path = OUTPUT_DIR / f"{stamp}.ass"
    make_ass(result, ass_path)

    output_path = OUTPUT_DIR / f"tiktok_{stamp}.mp4"
    burn_captions(video_path, ass_path, title, output_path)

    print()
    print(f"  🎬 Ready to post → {output_path}")

    preview_and_upload(output_path, title, description)
    print()


def main():
    print()
    print("  Chess Clip Automator")
    print("  ════════════════════")

    if "--reprocess" in sys.argv or "-r" in sys.argv:
        reprocess()
        return

    if not all([TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TWITCH_USERNAME]):
        print("\nError: .env is missing values.")
        print("Fill in TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, and TWITCH_USERNAME in clipper/.env")
        sys.exit(1)

    print(f"\n  Fetching clips for @{TWITCH_USERNAME}...")
    token = get_twitch_token()
    user_id = get_user_id(token)
    clips = get_recent_clips(token, user_id)

    if not clips:
        print("  No clips found on your channel.")
        sys.exit(0)

    selected = pick_clips(clips)
    if not selected:
        print("  No clips selected.")
        sys.exit(0)

    for clip in selected:
        print()
        print(f"  ══ {clip['title']} ══")

        vod_end = clip["vod_offset"] + clip["duration"]

        # 1. Download segment
        if PLATFORM == "youtube":
            video_path = download_from_youtube(vod_end, clip["created_at"])
        else:
            video_path = download_from_tiktok(vod_end)

        # 2. Title & description
        print(f"\n  Title (Enter to use Twitch title: \"{clip['title']}\"):")
        custom_title = input("  > ").strip()
        title = custom_title if custom_title else clip["title"]

        print("\n  Description / caption (Enter to skip):")
        description = input("  > ").strip()
        if not description:
            description = title

        # 3. Transcribe
        result = transcribe(video_path)

        # 4. Generate subtitles
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        ass_path = OUTPUT_DIR / f"{stamp}.ass"
        make_ass(result, ass_path)

        # 5. Burn in
        output_path = OUTPUT_DIR / f"tiktok_{stamp}.mp4"
        burn_captions(video_path, ass_path, title, output_path)

        print()
        print(f"  🎬 Ready to post → {output_path}")

        # 6. Preview & optional upload
        preview_and_upload(output_path, title, description)

    print()
    print("  All done!")
    print()


if __name__ == "__main__":
    main()
