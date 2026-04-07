#!/usr/bin/env python3
"""
Chess Clip Automator
Fetches your Twitch clips, calculates VOD timestamps, opens TikTok Live Center
at the right moment, then transcribes + burns captions and title into the video.
"""

import os
import sys
import json
import time
import plistlib
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
TIKTOK_CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY", "")
TIKTOK_CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLIP_MODE = os.getenv("CLIP_MODE", "off").lower()  # "off", "auto", or "auto-post"
SHOW_AUTOCLIPS = os.getenv("SHOW_AUTOCLIPS", "true").lower() == "true"
SHOW_OTHERS_CLIPS = os.getenv("SHOW_OTHERS_CLIPS", "true").lower() == "true"
CLIP_DAYS = min(int(os.getenv("CLIP_DAYS", "3")), 60)
CLIP_LENGTH = float(os.getenv("CLIP_LENGTH", "2"))  # minutes, 0.5-5
SCHEDULE_HOUR = int(os.getenv("SCHEDULE_HOUR", "10"))  # 0-23, hour to run batch
SCHEDULE_MINUTE = int(os.getenv("SCHEDULE_MINUTE", "0"))  # 0-59, minute to run batch

# ─── Color themes & positioning ───────────────────────────────────────────────
COLOR_THEMES = {
    "white":  {"name": "Classic White",  "box": "#FFFFFF", "text": "#000000"},
    "black":  {"name": "Classic Black",  "box": "#000000", "text": "#FFFFFF"},
    "blue":   {"name": "Electric Blue",  "box": "#1D8CD7", "text": "#FFFFFF"},
    "red":    {"name": "Fire Red",       "box": "#D72638", "text": "#FFFFFF"},
    "green":  {"name": "Neon Green",     "box": "#39FF14", "text": "#000000"},
    "purple": {"name": "Royal Purple",   "box": "#7B2FBE", "text": "#FFFFFF"},
    "gold":   {"name": "Gold",           "box": "#FFB800", "text": "#000000"},
}
COLOR_THEME_ORDER = ["white", "black", "blue", "red", "green", "purple", "gold"]
COLOR_THEME = os.getenv("COLOR_THEME", "blue")  # legacy fallback
TITLE_COLOR_THEME = os.getenv("TITLE_COLOR_THEME", "") or os.getenv("COLOR_THEME", "blue")
CAPTION_COLOR_THEME = os.getenv("CAPTION_COLOR_THEME", "") or os.getenv("COLOR_THEME", "blue")
TITLE_Y_PERCENT = float(os.getenv("TITLE_Y_PERCENT", "59"))
CAPTION_Y_PERCENT = float(os.getenv("CAPTION_Y_PERCENT", "65"))

OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
RAW_DIR = OUTPUT_DIR / "raw"
RAW_DIR.mkdir(exist_ok=True)
COMPLETED_DIR = OUTPUT_DIR / "completed"
COMPLETED_DIR.mkdir(exist_ok=True)

ICLOUD_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/ZaksClips"

BROWSER_DATA_DIR = Path(__file__).parent / ".browser_session"
YOUTUBE_TOKEN_PATH = Path(__file__).parent / ".youtube_token.json"
TIKTOK_TOKEN_PATH = Path(__file__).parent / ".tiktok_token.json"
TIKTOK_REDIRECT_URI = "http://localhost:3000/callback/"

TIKTOK_LIVE_CENTER_URL = "https://livecenter.tiktok.com/replay?lang=en"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def safe_filename(title, date_str):
    """Create a filename from title + date. e.g. 'Sunday Funday' + '2026-03-29' → 'Sunday_Funday_03-29'"""
    clean = re.sub(r'[^\w\s-]', '', title).strip()
    clean = re.sub(r'\s+', '_', clean)
    month_day = date_str[5:10]  # "03-29" from "2026-03-29"
    return f"{clean}_{month_day}"


def unique_output_path(directory, base_name, ext=".mp4"):
    """Return a path that doesn't collide with existing files.
    e.g. Title_03-29.mp4 → Title_03-29_2.mp4 → Title_03-29_3.mp4"""
    path = directory / f"{base_name}{ext}"
    if not path.exists():
        return path
    counter = 2
    while True:
        path = directory / f"{base_name}_{counter}{ext}"
        if not path.exists():
            return path
        counter += 1


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
    # Keep clips whose VOD is expired (can't verify = assume manual),
    # and clips whose title matches their VOD title (confirmed manual).
    # Only drop clips we can confirm are autoclips (VOD exists + title differs).
    return [c for c in clips if c.get("video_id") not in vod_titles
            or c["title"] == vod_titles[c["video_id"]]]


def get_recent_clips(token, user_id, count=20, days=CLIP_DAYS):
    # Fetch day-by-day to avoid Twitch's view-count sorting dropping low-view clips
    now = datetime.now(timezone.utc)
    seen_ids = set()
    clips = []
    for d in range(days):
        day_start = (now - timedelta(days=d + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        day_end = (now - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cursor = None
        for _ in range(5):  # max 5 pages per day
            params = {
                "broadcaster_id": user_id, "first": 50,
                "started_at": day_start, "ended_at": day_end,
            }
            if cursor:
                params["after"] = cursor
            resp = requests.get(
                "https://api.twitch.tv/helix/clips",
                headers={
                    "Client-ID": TWITCH_CLIENT_ID,
                    "Authorization": f"Bearer {token}",
                },
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
            for c in data["data"]:
                if c["id"] not in seen_ids:
                    seen_ids.add(c["id"])
                    clips.append(c)
            cursor = data.get("pagination", {}).get("cursor")
            if not cursor:
                break
    if not SHOW_OTHERS_CLIPS:
        clips = [c for c in clips if c.get("creator_name", "").lower() == TWITCH_USERNAME.lower()]
    if not SHOW_AUTOCLIPS:
        clips = _filter_autoclips(clips, token)
    clips.sort(key=lambda c: c["created_at"], reverse=True)
    return clips[:count]


# ─── Segment adjustment ──────────────────────────────────────────────────────

def parse_adjustment(text):
    """Parse adjustment input like '+10', '-15', 'e+10', 'e-20'.
    Returns (start_delta, end_delta) in seconds.
    + = more video (start earlier / end later), - = less video."""
    text = text.strip().lower()
    if not text:
        return 0, 0

    adjustments = [a.strip() for a in text.split(",")]
    start_delta = 0
    end_delta = 0

    for adj in adjustments:
        if not adj:
            continue
        if adj.startswith("e"):
            # End adjustment
            val = adj[1:]
            try:
                end_delta += int(val)
            except ValueError:
                print(f"  Couldn't parse '{adj}', skipping")
        else:
            # Start adjustment: +N means start earlier (subtract from start),
            # -N means start later (add to start)
            try:
                val = int(adj)
                start_delta += val
            except ValueError:
                print(f"  Couldn't parse '{adj}', skipping")

    return start_delta, end_delta


def trim_video(video_path, trim_start_secs, trim_end_secs):
    """Trim a video locally with ffmpeg. trim_start_secs trims from the front,
    trim_end_secs trims from the tail. Returns the new file path."""
    stamp = datetime.now().strftime("%m-%d_%H%M")
    trimmed_path = RAW_DIR / f"trimmed_{stamp}.mp4"

    # Put -ss before -i for fast seek, then use re-encode for frame-accurate cuts
    cmd = ["ffmpeg", "-y"]

    if trim_start_secs > 0:
        cmd += ["-ss", str(trim_start_secs)]
    cmd += ["-i", str(video_path)]

    if trim_end_secs > 0:
        # Get duration and subtract trim from end
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True,
        )
        total = float(probe.stdout.strip())
        new_duration = total - trim_start_secs - trim_end_secs
        if new_duration <= 0:
            print("  Trim would remove entire video, skipping.")
            return video_path
        cmd += ["-t", str(new_duration)]

    # Re-encode for frame-accurate trimming (stream copy cuts at keyframes only)
    cmd += ["-preset", "fast", "-crf", "18", "-avoid_negative_ts", "make_zero", str(trimmed_path)]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ffmpeg trim failed: {result.stderr[-500:]}")
        return video_path

    print(f"  ✓ Trimmed: {trimmed_path.name}")
    return trimmed_path


def preview_and_adjust(video_path, seg_start, seg_end, clip, download_fn):
    """Open raw video, let user adjust segment timing. Trims locally when possible,
    only re-downloads when extending. Returns final video_path, seg_start, seg_end."""
    while True:
        print("\n  Opening raw video for review...")
        subprocess.run(["open", str(video_path)])

        duration = seg_end - seg_start
        print(f"\n  Segment: {fmt_time(seg_start)} → {fmt_time(seg_end)} ({fmt_time(duration)})")
        print("  Adjust? Examples: +10 (more at start), -15 (trim start), e-10 (trim end), e+10 (more at end)")
        print("  Combine with commas: +10,e-15")
        print("  Enter to keep as-is")
        choice = input("  > ").strip()

        if not choice:
            return video_path, seg_start, seg_end

        start_delta, end_delta = parse_adjustment(choice)
        if start_delta == 0 and end_delta == 0:
            return video_path, seg_start, seg_end

        new_start = max(0, seg_start - start_delta)
        new_end = seg_end + end_delta

        if new_end <= new_start:
            print("  Invalid adjustment — end would be before start. Try again.")
            continue

        needs_redownload = new_start < seg_start or new_end > seg_end

        if needs_redownload:
            print(f"\n  New segment: {fmt_time(new_start)} → {fmt_time(new_end)} ({fmt_time(new_end - new_start)})")
            print("  Extending — re-downloading...")
            video_path = download_fn(new_start, new_end, clip)
        else:
            # Pure trim — crop locally
            trim_front = seg_start - new_start + (-start_delta if start_delta < 0 else 0)
            # start_delta < 0 means trim start: new_start = seg_start + abs(start_delta)
            # So trim_front = new_start - seg_start... wait let me recalc
            trim_front = new_start - seg_start  # how many secs into the video to start
            trim_tail = seg_end - new_end       # how many secs to cut from end
            print(f"\n  Trimming locally ({fmt_time(new_end - new_start)} final)...")
            video_path = trim_video(video_path, trim_front, trim_tail)

        seg_start = new_start
        seg_end = new_end


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
        seg_start = max(0, vod_end - int(CLIP_LENGTH * 60))
        print(f"  [{i+1}] {clip['title']}")
        print(f"       Created : {clip['created_at'][:10]}")
        print(f"       Segment : {fmt_time(seg_start)} → {fmt_time(vod_end)}  ({CLIP_LENGTH}min ending at clip)")
        print()

    print("Which clips do you want to process? (e.g. 1,3 or 'all')")
    choice = input("> ").strip().lower()

    if choice == "all":
        return usable

    indices = [int(x.strip()) - 1 for x in choice.split(",") if x.strip().isdigit()]
    return [usable[i] for i in indices if 0 <= i < len(usable)]


# ─── TikTok Live Center download ──────────────────────────────────────────────

def download_from_tiktok(seg_start, seg_end, clip=None):
    """
    Opens TikTok Live Center in a persistent browser session.
    Displays the exact timestamp to seek to.
    Waits for a new .mp4 to appear in ~/Downloads and returns its path.
    """
    from playwright.sync_api import sync_playwright

    tiktok_start = seg_start
    tiktok_end = seg_end

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
    """Find a livestream VOD by matching actualStartTime date to the clip's date.
    Also checks the day before, since streams that start before midnight UTC
    produce clips timestamped the next day."""
    clip_dt = datetime.fromisoformat(clip_created_at.replace("Z", "+00:00"))
    clip_date = clip_dt.strftime("%Y-%m-%d")
    prev_date = (clip_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    match_dates = {clip_date, prev_date}

    # Step 1: get recent video IDs from the channel
    search_resp = requests.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={
            "part": "id",
            "channelId": channel_id,
            "type": "video",
            "order": "date",
            "maxResults": 50,
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

    # Step 3: match on actualStartTime date (or the day before)
    matches = []
    for v in videos:
        start = v.get("liveStreamingDetails", {}).get("actualStartTime", "")
        if start[:10] in match_dates:
            matches.append({
                "id": {"videoId": v["id"]},
                "snippet": v["snippet"],
            })
    return matches


# ─── YouTube VOD download ─────────────────────────────────────────────────────

def download_from_youtube(seg_start, seg_end, clip=None):
    """
    Looks up the YouTube VOD matching the clip date, then downloads
    the segment using yt-dlp. Fully automated.
    """
    clip_created_at = clip["created_at"] if clip else ""
    yt_start = seg_start
    yt_end = seg_end

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

    stamp = datetime.now().strftime("%m-%d_%H%M%S")
    output_path = RAW_DIR / f"yt_raw_{stamp}.mp4"
    section = f"*{to_yt_ts(yt_start)}-{to_yt_ts(yt_end)}"

    cmd = [
        "yt-dlp",
        "--download-sections", section,
        "--force-keyframes-at-cuts",
        "--merge-output-format", "mp4",
        "-o", str(output_path),
        vod_url,
    ]

    result = subprocess.run(cmd, text=True)
    if result.returncode != 0:
        print("\n  Retrying without --force-keyframes-at-cuts...")
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

    # Release MLX/Metal GPU resources to prevent trace trap crashes in PyAV
    import gc
    gc.collect()
    try:
        import mlx.core as mx
        mx.eval(mx.zeros(1))  # force Metal sync/flush
    except Exception:
        pass

    return result


def save_whisper_result(video_path, result):
    """Save Whisper result to a JSON file alongside the video."""
    out = Path(video_path).with_suffix(".whisper.json")
    out.write_text(json.dumps(result, ensure_ascii=False))
    return out


def load_whisper_result(video_path):
    """Load saved Whisper result for a video. Returns None if not found."""
    out = Path(video_path).with_suffix(".whisper.json")
    if out.exists():
        try:
            return json.loads(out.read_text())
        except Exception:
            pass
    return None


def trim_whisper_result(result, trim_start, trim_end_from_tail, total_duration):
    """Trim a Whisper result to a new time range by filtering and shifting timestamps.
    trim_start: seconds cut from the beginning
    trim_end_from_tail: seconds cut from the end
    total_duration: original video duration in seconds
    Returns a new result dict with adjusted timestamps."""
    if trim_start == 0 and trim_end_from_tail == 0:
        return result
    new_end = total_duration - trim_end_from_tail
    new_segments = []
    for seg in result.get("segments", []):
        # Skip segments entirely outside the new range
        if seg["end"] <= trim_start or seg["start"] >= new_end:
            continue
        new_seg = dict(seg)
        new_seg["start"] = max(0, seg["start"] - trim_start)
        new_seg["end"] = min(new_end - trim_start, seg["end"] - trim_start)
        if "words" in seg:
            new_words = []
            for w in seg["words"]:
                ws = w.get("start", seg["start"])
                we = w.get("end", seg["end"])
                if we <= trim_start or ws >= new_end:
                    continue
                new_w = dict(w)
                new_w["start"] = max(0, ws - trim_start)
                new_w["end"] = min(new_end - trim_start, we - trim_start)
                new_words.append(new_w)
            new_seg["words"] = new_words
        new_segments.append(new_seg)
    return {"segments": new_segments, "text": result.get("text", "")}


def _read_voice_file():
    """Read voice.txt if it exists, return contents or empty string."""
    voice_path = Path(__file__).parent / "voice.txt"
    if voice_path.exists():
        try:
            return voice_path.read_text().strip()
        except Exception:
            return ""
    return ""


def log_to_voice(title, caption):
    """Append a posted title+caption to voice.txt."""
    voice_path = Path(__file__).parent / "voice.txt"
    try:
        content = voice_path.read_text() if voice_path.exists() else ""
        # Find the Posted Titles section and append
        title_marker = "## Posted Titles (burned-in overlay)"
        caption_marker = "## Posted Captions (YouTube/TikTok)"
        if title and title_marker in content:
            idx = content.index(caption_marker)
            content = content[:idx] + f"- {title.upper()}\n" + content[idx:]
        if caption and caption_marker in content:
            rejected_marker = "## Rejected"
            idx = content.index(rejected_marker)
            content = content[:idx] + f"- {caption}\n" + content[idx:]
        voice_path.write_text(content)
        print(f"  ✓ Logged to voice.txt")
    except Exception as e:
        print(f"  Could not log to voice.txt: {e}")


def log_rejection_to_voice(ai_title, user_title, ai_caption, user_caption):
    """Log rejected AI suggestions to voice.txt so the AI learns what NOT to do."""
    voice_path = Path(__file__).parent / "voice.txt"
    try:
        content = voice_path.read_text() if voice_path.exists() else ""
        lines = []
        if ai_title and user_title and ai_title.upper() != user_title.upper():
            lines.append(f"- TITLE: \"{ai_title}\" -> \"{user_title}\"")
        if ai_caption and user_caption and ai_caption.strip() != user_caption.strip():
            lines.append(f"- CAPTION: \"{ai_caption}\" -> \"{user_caption}\"")
        if lines:
            content = content.rstrip() + "\n" + "\n".join(lines) + "\n"
            voice_path.write_text(content)
            print(f"  ✓ Logged rejection(s) to voice.txt")
    except Exception as e:
        print(f"  Could not log rejection to voice.txt: {e}")


def generate_title_caption(transcript_text, clip_title=""):
    """Use Claude to generate a title and caption from the transcript.
    Returns (title, caption) or (None, None) if unavailable."""
    if not ANTHROPIC_API_KEY:
        print("  No ANTHROPIC_API_KEY — skipping AI title generation")
        return None, None

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        voice_guide = _read_voice_file()
        voice_section = f"""
Here is the creator's voice and brand guide — match this style closely:
---
{voice_guide}
---

""" if voice_guide else ""

        prompt = f"""You are writing a title and caption for a chess TikTok/YouTube Short.
{voice_section}
Here is the transcript of the clip:
{transcript_text[:3000]}

{f'The Twitch clip was titled: "{clip_title}"' if clip_title else ''}

Generate TWO things:

1. TITLE: A short, punchy, ALL CAPS title (2-6 words). Match the voice and style from the guide above. No quotes or punctuation.

2. CAPTION: A quirky, funny one-liner caption matching the creator's voice. End it with #chess.

Pay close attention to the "Rejected" section in the voice guide — avoid generating titles/captions similar to ones the creator has replaced before.

Reply in EXACTLY this format, nothing else:
TITLE: YOUR TITLE HERE
CAPTION: your caption here #chess"""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        title = None
        caption = None
        for line in text.split("\n"):
            line = line.strip()
            if line.upper().startswith("TITLE:"):
                title = line.split(":", 1)[1].strip().upper()
            elif line.upper().startswith("CAPTION:"):
                caption = line.split(":", 1)[1].strip()

        if title:
            print(f"  ✓ AI title: {title}")
        if caption:
            print(f"  ✓ AI caption: {caption}")
        return title, caption

    except Exception as e:
        print(f"  AI title generation failed: {e}")
        return None, None


def get_transcript_text(whisper_result):
    """Extract plain text from a Whisper result dict."""
    return " ".join(
        seg.get("text", "").strip()
        for seg in whisper_result.get("segments", [])
    ).strip()


# ─── ASS subtitle generation ──────────────────────────────────────────────────

def _hex_to_ass(hex_color):
    """Convert #RRGGBB to ASS &H00BBGGRR format."""
    r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
    return f"&H00{b:02X}{g:02X}{r:02X}"


def _make_ass_header():
    """Generate ASS header using current theme and caption Y position."""
    theme = COLOR_THEMES.get(CAPTION_COLOR_THEME, COLOR_THEMES["blue"])
    margin_v = int(1920 * (1 - CAPTION_Y_PERCENT / 100))
    primary = _hex_to_ass(theme["text"])    # caption text color
    outline = _hex_to_ass(theme["box"])     # caption outline = theme color
    return f"""\
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 1

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,88,{primary},&H000000FF,{outline},&H00000000,1,0,0,0,100,100,2,0,1,5,2,2,60,60,{margin_v},1

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
        f.write(_make_ass_header())
        f.write("\n".join(lines))

    print(f"  ✓ Subtitles written ({len(lines)} lines)")


# ─── ffmpeg: burn captions + title ───────────────────────────────────────────

def burn_captions(video_path, ass_path, title, output_path):
    print("  Burning captions and title with ffmpeg...")

    # Use a system font guaranteed to be on macOS
    font_path = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
    if not Path(font_path).exists():
        font_path = "/System/Library/Fonts/Helvetica.ttc"

    safe_ass_path = str(ass_path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")

    # Theme colors
    theme = COLOR_THEMES.get(TITLE_COLOR_THEME, COLOR_THEMES["blue"])
    box_color = theme["box"]
    text_color = theme["text"]
    y_pct = TITLE_Y_PERCENT / 100

    # Wrap title into multiple lines, stack drawtext filters vertically
    words = title.upper().split()
    title_lines = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > 14:
            title_lines.append(current)
            current = word
        else:
            current = f"{current} {word}" if current else word
    if current:
        title_lines.append(current)

    fontsize = 72
    box_pad = 20
    line_height = fontsize  # boxes overlap by 2*box_pad, looks connected
    total_height = len(title_lines) * line_height
    base_y = f"(h*{y_pct})-{total_height // 2}"

    # Two-pass rendering: draw all boxes first, then all text on top.
    # Single-pass would let a lower line's box paint over the upper line's text.
    title_filters = ""
    for i, line in enumerate(title_lines):
        safe_line = line.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:").replace("%", "\\%")
        y_expr = f"{base_y}+{i * line_height}"
        # Pass 1: box only (transparent text)
        title_filters += (
            f",drawtext=text='{safe_line}'"
            f":fontfile='{font_path}'"
            f":fontcolor={text_color}@0.0"
            f":fontsize={fontsize}"
            f":x=(w-tw)/2"
            f":y={y_expr}"
            f":box=1"
            f":boxcolor={box_color}"
            f":boxborderw={box_pad}"
            f":enable='between(t,0,5)'"
        )
    for i, line in enumerate(title_lines):
        safe_line = line.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:").replace("%", "\\%")
        y_expr = f"{base_y}+{i * line_height}"
        # Pass 2: text only (no box)
        title_filters += (
            f",drawtext=text='{safe_line}'"
            f":fontfile='{font_path}'"
            f":fontcolor={text_color}"
            f":fontsize={fontsize}"
            f":x=(w-tw)/2"
            f":y={y_expr}"
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

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=False)
    buf = b""
    last_logged_secs = -5  # ensure first update prints
    while True:
        chunk = proc.stdout.read(1)
        if not chunk:
            break
        if chunk in (b"\r", b"\n"):
            line = buf.decode("utf-8", errors="replace").strip()
            buf = b""
            if not line:
                continue
            # Parse ffmpeg progress: frame= 776 fps= 47 ... time=00:00:25.47 ... speed=1.53x
            m = re.search(r"time=(\d+):(\d+):([\d.]+)", line)
            if m:
                secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                if secs - last_logged_secs >= 5:
                    last_logged_secs = secs
                    sm = re.search(r"speed=\s*([\d.]+)x", line)
                    speed = f" ({sm.group(1)}x)" if sm else ""
                    t_min, t_sec = divmod(int(secs), 60)
                    print(f"  ffmpeg: {t_min}:{t_sec:02d} encoded{speed}", flush=True)
            elif "error" in line.lower():
                print(f"  ffmpeg: {line}", flush=True)
        else:
            buf += chunk
    proc.wait()
    if proc.returncode != 0:
        print("\n  ffmpeg error (see terminal for details)")
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
    """Upload a video to YouTube as a Short. Returns the video URL or None.
    For Shorts, the description becomes YouTube's title (the burned-in title is on the video itself)."""
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = get_youtube_credentials()
    if not creds:
        return None

    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": description,
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


# ─── TikTok OAuth & Upload ──────────────────────────────────────────────────

def _tiktok_load_token():
    """Load saved TikTok token from disk."""
    import json
    if not TIKTOK_TOKEN_PATH.exists():
        return None
    with open(TIKTOK_TOKEN_PATH) as f:
        return json.load(f)


def _tiktok_save_token(token_data):
    """Save TikTok token to disk."""
    import json
    with open(TIKTOK_TOKEN_PATH, "w") as f:
        json.dump(token_data, f, indent=2)


def _tiktok_refresh_token(token_data):
    """Refresh an expired TikTok access token."""
    resp = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": TIKTOK_CLIENT_KEY,
            "client_secret": TIKTOK_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": token_data["refresh_token"],
        },
    )
    resp.raise_for_status()
    new_data = resp.json()
    if "access_token" not in new_data:
        print(f"  TikTok refresh failed: {new_data}")
        return None
    new_data["obtained_at"] = time.time()
    _tiktok_save_token(new_data)
    print("  ✓ TikTok token refreshed")
    return new_data


def get_tiktok_access_token():
    """Get a valid TikTok access token. Launches browser auth on first run."""
    import json
    import webbrowser
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from urllib.parse import urlparse, parse_qs

    # Try loading saved token
    token_data = _tiktok_load_token()
    if token_data:
        age = time.time() - token_data.get("obtained_at", 0)
        expires_in = token_data.get("expires_in", 86400)
        if age < expires_in - 300:
            return token_data["access_token"]
        # Token expired, try refresh
        refreshed = _tiktok_refresh_token(token_data)
        if refreshed:
            return refreshed["access_token"]

    if not TIKTOK_CLIENT_KEY or not TIKTOK_CLIENT_SECRET:
        print("\n  TikTok upload requires credentials.")
        print("  Add TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET to your .env")
        return None

    # OAuth with PKCE: open browser, catch the callback
    # TikTok uses hex-encoded SHA256 for code_challenge (not base64url like standard PKCE)
    import secrets
    import hashlib
    import string
    csrf_state = secrets.token_urlsafe(16)
    # code_verifier: 43-128 chars from unreserved charset [A-Za-z0-9-._~]
    charset = string.ascii_letters + string.digits + "-._~"
    code_verifier = "".join(secrets.choice(charset) for _ in range(64))
    code_challenge = hashlib.sha256(code_verifier.encode("ascii")).hexdigest()

    auth_url = (
        "https://www.tiktok.com/v2/auth/authorize/"
        f"?client_key={TIKTOK_CLIENT_KEY}"
        "&scope=user.info.basic,video.publish,video.upload"
        "&response_type=code"
        f"&redirect_uri={TIKTOK_REDIRECT_URI}"
        f"&state={csrf_state}"
        f"&code_challenge={code_challenge}"
        "&code_challenge_method=S256"
    )

    auth_code = None

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal auth_code
            qs = parse_qs(urlparse(self.path).query)
            if qs.get("state", [None])[0] != csrf_state:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"State mismatch - try again.")
                return
            if "error" in qs:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(f"Error: {qs['error'][0]} — {qs.get('error_description', [''])[0]}".encode())
                return
            auth_code = qs.get("code", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>TikTok authorized! You can close this tab.</h2>")

        def log_message(self, format, *args):
            pass  # suppress server logs

    print("\n  Opening browser for TikTok authorization (one-time setup)...")
    HTTPServer.allow_reuse_address = True
    server = HTTPServer(("localhost", 3000), CallbackHandler)
    webbrowser.open(auth_url)

    # Wait for the callback
    while auth_code is None:
        server.handle_request()
    server.server_close()

    # Exchange code for token (include PKCE code_verifier)
    resp = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": TIKTOK_CLIENT_KEY,
            "client_secret": TIKTOK_CLIENT_SECRET,
            "code": auth_code,
            "grant_type": "authorization_code",
            "redirect_uri": TIKTOK_REDIRECT_URI,
            "code_verifier": code_verifier,
        },
    )
    resp.raise_for_status()
    token_data = resp.json()

    if "access_token" not in token_data:
        print(f"  TikTok token exchange failed: {token_data}")
        return None

    token_data["obtained_at"] = time.time()
    _tiktok_save_token(token_data)
    print("  ✓ TikTok credentials saved")
    return token_data["access_token"]


def upload_to_tiktok(video_path, caption):
    """Upload a video to TikTok via the Content Posting API. Returns publish_id or None.
    caption is used as TikTok's post description (the burned-in title is on the video itself)."""
    access_token = get_tiktok_access_token()
    if not access_token:
        return None

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
    }

    # Step 1: Query creator info to get allowed privacy levels
    print("\n  Querying TikTok creator info...")
    info_resp = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/creator_info/query/",
        headers=headers,
    )
    if info_resp.status_code != 200:
        print(f"  TikTok creator info failed ({info_resp.status_code}): {info_resp.text[:500]}")
        return None

    creator_info = info_resp.json().get("data", {})
    privacy_options = creator_info.get("privacy_level_options", ["SELF_ONLY"])
    max_duration = creator_info.get("max_video_post_duration_sec", 600)

    # Pick best available privacy: PUBLIC > FRIENDS > SELF_ONLY
    if "PUBLIC_TO_EVERYONE" in privacy_options:
        privacy = "PUBLIC_TO_EVERYONE"
    elif "MUTUAL_FOLLOW_FRIENDS" in privacy_options:
        privacy = "MUTUAL_FOLLOW_FRIENDS"
    else:
        privacy = "SELF_ONLY"
    print(f"  Privacy: {privacy}")

    # Step 2: Initialize upload
    file_size = video_path.stat().st_size
    # TikTok: chunk_size min 5MB, max 64MB, final chunk can be up to 128MB.
    # For files under 64MB use a single chunk (chunk_size must be >= file_size).
    # For tiny files under 5MB, still use file_size — TikTok's min only applies
    # to non-final chunks in multi-chunk uploads.
    if file_size <= 64_000_000:
        chunk_size = file_size
        total_chunks = 1
    else:
        chunk_size = 10_000_000
        total_chunks = (file_size + chunk_size - 1) // chunk_size

    init_body = {
        "post_info": {
            "title": caption[:150],
            "privacy_level": privacy,
            "disable_comment": False,
            "disable_duet": False,
            "disable_stitch": False,
            "video_cover_timestamp_ms": 1000,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": file_size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunks,
        },
    }

    print("  Initializing TikTok upload...")
    init_resp = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/video/init/",
        headers=headers,
        json=init_body,
    )
    if init_resp.status_code != 200:
        print(f"  TikTok init failed ({init_resp.status_code}): {init_resp.text[:500]}")
        return None

    init_data = init_resp.json().get("data", {})
    publish_id = init_data.get("publish_id")
    upload_url = init_data.get("upload_url")

    if not upload_url:
        print(f"  TikTok init returned no upload_url: {init_resp.json()}")
        return None

    # Step 3: Upload chunks
    print(f"  Uploading {file_size / 1_000_000:.1f} MB in {total_chunks} chunk(s)...")
    with open(video_path, "rb") as f:
        for i in range(total_chunks):
            chunk = f.read(chunk_size)
            start = i * chunk_size
            end = start + len(chunk) - 1

            put_resp = requests.put(
                upload_url,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Content-Length": str(len(chunk)),
                },
                data=chunk,
            )

            if put_resp.status_code not in (200, 201, 206):
                print(f"  Chunk {i+1} failed ({put_resp.status_code}): {put_resp.text[:300]}")
                return None

            pct = int((i + 1) / total_chunks * 100)
            print(f"  ⬆ {pct}%", end="\r")

    print(f"\n  ✓ Upload complete (publish_id: {publish_id})")

    # Step 4: Poll for publish status
    print("  Waiting for TikTok to process...")
    for _ in range(30):
        time.sleep(5)
        status_resp = requests.post(
            "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
            headers=headers,
            json={"publish_id": publish_id},
        )
        if status_resp.status_code != 200:
            continue
        status_data = status_resp.json().get("data", {})
        status = status_data.get("status")
        if status == "PUBLISH_COMPLETE":
            print("  ✓ Published to TikTok!")
            return publish_id
        elif status in ("FAILED", "PUBLISH_FAILED"):
            fail_reason = status_data.get("fail_reason", "unknown")
            print(f"  ✗ TikTok publish failed: {fail_reason}")
            return None
        # PROCESSING_UPLOAD or PROCESSING_DOWNLOAD — keep waiting

    print("  ✗ TikTok publish timed out (may still be processing)")
    return publish_id


# ─── Preview & Upload ───────────────────────────────────────────────────────

def copy_to_icloud(video_path):
    """Copy finished video to iCloud Drive so it's accessible on phone."""
    import shutil
    ICLOUD_DIR.mkdir(exist_ok=True)
    dest = ICLOUD_DIR / video_path.name
    shutil.copy2(video_path, dest)
    print(f"  ✓ Copied to iCloud Drive → Files app → ZaksClips/{video_path.name}")


def preview_and_upload(output_path, title, description):
    """Open video for review, then optionally upload to YouTube Shorts and/or TikTok."""
    # Gate 1: Preview the video
    print("\n  Opening video for preview...")
    subprocess.run(["open", str(output_path)])

    print("\n  Is this video good? (y/n)")
    if input("  > ").strip().lower() != "y":
        print("  ✗ Video rejected — not uploading.")
        return

    # Copy to iCloud Drive for phone access
    copy_to_icloud(output_path)

    icloud_msg = f"  Post manually from iCloud Drive → Files → ZaksClips/{output_path.name}"

    # Gate 2: YouTube upload
    print("\n  Upload to YouTube Shorts? (y/n)")
    if input("  > ").strip().lower() == "y":
        url = upload_to_youtube(output_path, title, description)
        if url:
            print(f"\n  🎬 YouTube: {url}")
        else:
            print(f"\n  ✗ YouTube upload failed.")
            print(icloud_msg)

    # Gate 3: TikTok upload
    if TIKTOK_CLIENT_KEY:
        print("\n  Upload to TikTok? (y/n)")
        if input("  > ").strip().lower() == "y":
            publish_id = upload_to_tiktok(output_path, description)
            if publish_id:
                print(f"\n  🎬 TikTok publish_id: {publish_id}")
            else:
                print(f"\n  ✗ TikTok upload failed.")
                print(icloud_msg)


# ─── Main ─────────────────────────────────────────────────────────────────────

def reprocess():
    """Skip download — pick an existing raw file and redo transcription + burn."""
    print()
    print("  Chess Clip Automator — Reprocess Mode")
    print("  ══════════════════════════════════════")

    raw_files = sorted(RAW_DIR.glob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)

    if not raw_files:
        print("\n  No raw .mp4 files found in output/raw/")
        sys.exit(1)

    print("\n  Raw files (newest first):\n")
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
    ass_path = RAW_DIR / f"{stamp}.ass"
    make_ass(result, ass_path)

    output_name = safe_filename(title, datetime.now().strftime("%Y-%m-%d"))
    output_path = unique_output_path(COMPLETED_DIR, output_name)
    burn_captions(video_path, ass_path, title, output_path)

    print()
    print(f"  🎬 Ready to post → {output_path}")

    preview_and_upload(output_path, title, description)
    print()


def clean():
    """List output files and let user delete some or all."""
    print()
    print("  Chess Clip Automator — Clean Output")
    print("  ════════════════════════════════════")

    all_files = list(COMPLETED_DIR.glob("*.mp4")) + list(RAW_DIR.glob("*.mp4"))
    files = sorted(all_files, key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        print("\n  Output folder is empty.")
        return

    total_size = sum(f.stat().st_size for f in files)
    print(f"\n  {len(files)} files ({total_size / 1_000_000:.1f} MB total):\n")
    for i, f in enumerate(files):
        size = f.stat().st_size / 1_000_000
        print(f"  [{i+1}] {f.name}  ({size:.1f} MB)")

    print("\n  Delete which? (e.g. 1,3 or 'all' or Enter to cancel)")
    choice = input("  > ").strip().lower()

    if not choice:
        print("  Cancelled.")
        return

    if choice == "all":
        to_delete = files
    else:
        indices = [int(x.strip()) - 1 for x in choice.split(",") if x.strip().isdigit()]
        to_delete = [files[i] for i in indices if 0 <= i < len(files)]

    if not to_delete:
        print("  Nothing selected.")
        return

    for f in to_delete:
        f.unlink()
        print(f"  ✗ Deleted: {f.name}")

    print(f"\n  Removed {len(to_delete)} file(s).")


LAUNCHD_LABEL = "com.zaksclips.reminder"
LAUNCHD_PLIST = Path.home() / "Library/LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
CLIPPER_PATH = Path(__file__).resolve()


def send_notification():
    """Show a macOS dialog with Start/Dismiss. Start launches the clipper in Terminal."""
    script = f'''
    set response to display dialog "Clips are ready! Time to process yesterday's stream." ¬
        with title "ZaksClips" ¬
        buttons {{"Dismiss", "Start"}} default button "Start" ¬
        giving up after 3600
    if button returned of response is "Start" then
        tell application "Terminal"
            activate
            do script "cd \\"{CLIPPER_PATH.parent}\\" && python3 clipper.py"
        end tell
    end if
    '''
    subprocess.run(["osascript", "-e", script])


def schedule(test=False):
    """Schedule batch processing for 10am daily via launchd.
    Runs clipper.py --batch headlessly — videos land in the GUI review queue."""
    if test:
        print("\n  Running batch process now...")
        batch_process()
        return

    python_path = sys.executable  # e.g. /opt/homebrew/bin/python3
    batch_cmd = f'cd "{CLIPPER_PATH.parent}" && "{python_path}" "{CLIPPER_PATH}" --batch'

    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": ["/bin/bash", "-c", batch_cmd],
        "StartCalendarInterval": {
            "Hour": SCHEDULE_HOUR,
            "Minute": SCHEDULE_MINUTE,
        },
        "StandardOutPath": str(OUTPUT_DIR / "worker.log"),
        "StandardErrorPath": str(OUTPUT_DIR / "worker.log"),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/opt/ffmpeg-full/bin:/opt/homebrew/bin:/usr/bin:/bin",
        },
        "RunAtLoad": False,
    }

    # Unload existing if present
    if LAUNCHD_PLIST.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(LAUNCHD_PLIST)],
                        capture_output=True)

    LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)
    with open(LAUNCHD_PLIST, "wb") as f:
        plistlib.dump(plist, f)

    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(LAUNCHD_PLIST)],
                    check=True)

    hour_label = f"{SCHEDULE_HOUR % 12 or 12}:{SCHEDULE_MINUTE:02d}{'am' if SCHEDULE_HOUR < 12 else 'pm'}"
    print(f"\n  ✓ Auto-process scheduled for {hour_label} daily")
    print(f"    Clips will be processed and queued for review in the app.")
    print(f"    To cancel: python3 clipper.py --unschedule")
    print(f"    To test now: python3 clipper.py -s --test")


def unschedule():
    """Remove the scheduled auto-processing job."""
    if LAUNCHD_PLIST.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(LAUNCHD_PLIST)],
                        capture_output=True)
        LAUNCHD_PLIST.unlink()
        print("\n  ✓ Auto-process schedule removed.")
    else:
        print("\n  No schedule active.")


def is_scheduled():
    """Check if the auto-processing launchd job is installed."""
    return LAUNCHD_PLIST.exists()


# ─── Scheduled posting ──────────────────────────────────────────────────────

POSTER_LAUNCHD_LABEL = "com.zaksclips.poster"
POSTER_LAUNCHD_PLIST = Path.home() / "Library/LaunchAgents" / f"{POSTER_LAUNCHD_LABEL}.plist"


def schedule_poster():
    """Install a launchd job that runs clipper.py --post hourly."""
    python_path = sys.executable
    post_cmd = f'cd "{CLIPPER_PATH.parent}" && "{python_path}" "{CLIPPER_PATH}" --post'

    plist = {
        "Label": POSTER_LAUNCHD_LABEL,
        "ProgramArguments": ["/bin/bash", "-c", post_cmd],
        "StartInterval": 900,  # every 15 minutes
        "StandardOutPath": str(OUTPUT_DIR / "poster.log"),
        "StandardErrorPath": str(OUTPUT_DIR / "poster.log"),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/opt/ffmpeg-full/bin:/opt/homebrew/bin:/usr/bin:/bin",
        },
        "RunAtLoad": True,
    }

    if POSTER_LAUNCHD_PLIST.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(POSTER_LAUNCHD_PLIST)],
                        capture_output=True)

    POSTER_LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)
    with open(POSTER_LAUNCHD_PLIST, "wb") as f:
        plistlib.dump(plist, f)

    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(POSTER_LAUNCHD_PLIST)],
                    check=True)


def unschedule_poster():
    """Remove the poster launchd job."""
    if POSTER_LAUNCHD_PLIST.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(POSTER_LAUNCHD_PLIST)],
                        capture_output=True)
        POSTER_LAUNCHD_PLIST.unlink()


def is_poster_scheduled():
    """Check if the poster launchd job is installed."""
    return POSTER_LAUNCHD_PLIST.exists()


def post_scheduled():
    """Check for scheduled videos that are due and upload them."""
    import json
    import logging

    log_path = OUTPUT_DIR / "poster.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )
    log = logging.getLogger("zaksclips.poster")

    meta_path = Path(__file__).parent / ".video_meta.json"
    if not meta_path.exists():
        return

    try:
        meta = json.loads(meta_path.read_text())
    except Exception:
        return

    now = datetime.now()
    posted_count = 0

    for filename, vmeta in list(meta.items()):
        if vmeta.get("status") != "scheduled":
            continue

        scheduled_time_str = vmeta.get("scheduled_time", "")
        if not scheduled_time_str:
            continue

        try:
            scheduled_time = datetime.fromisoformat(scheduled_time_str)
        except ValueError:
            continue

        if scheduled_time > now:
            continue  # not due yet

        video_path = COMPLETED_DIR / filename
        if not video_path.exists():
            log.warning(f"Scheduled video not found: {filename}")
            vmeta["status"] = "upload_failed"
            continue

        title = vmeta.get("title", video_path.stem)
        caption = vmeta.get("caption", title)

        log.info(f"Posting scheduled video: {title}")

        # Copy to iCloud
        copy_to_icloud(video_path)

        yt_ok = False
        tt_ok = False

        if YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET:
            try:
                url = upload_to_youtube(video_path, title, caption)
                if url:
                    vmeta["youtube"] = True
                    yt_ok = True
                    log.info(f"  YouTube: {url}")
            except Exception as e:
                vmeta["youtube_failed"] = True
                log.error(f"  YouTube upload failed: {e}")

        if TIKTOK_CLIENT_KEY:
            try:
                publish_id = upload_to_tiktok(video_path, caption)
                if publish_id:
                    vmeta["tiktok"] = True
                    tt_ok = True
                    log.info(f"  TikTok: {publish_id}")
            except Exception as e:
                vmeta["tiktok_failed"] = True
                log.error(f"  TikTok upload failed: {e}")

        if yt_ok or tt_ok:
            vmeta["status"] = "posted"
            posted_count += 1
            log_to_voice(vmeta.get("title"), vmeta.get("caption"))
        else:
            vmeta["status"] = "upload_failed"

        # Clean up raw file
        raw_path_str = vmeta.get("raw_path", "")
        if raw_path_str:
            Path(raw_path_str).unlink(missing_ok=True)

    meta_path.write_text(json.dumps(meta, indent=2))

    if posted_count > 0:
        msg = f"{posted_count} scheduled video(s) posted"
        subprocess.run(["osascript", "-e",
            f'display notification "{msg}" with title "ZaksClips"'])
        log.info(msg)

    # If no more scheduled videos, remove the poster job
    has_scheduled = any(v.get("status") == "scheduled" for v in meta.values())
    if not has_scheduled and is_poster_scheduled():
        unschedule_poster()
        log.info("No more scheduled videos — poster job removed")


def batch_process():
    """Headless batch processing — fetch clips, process all, write metadata for GUI review.
    No prompts, no uploads. Videos land in the GUI's 'Ready for Review' queue."""
    import json
    import logging

    log_path = OUTPUT_DIR / "worker.log"
    # Truncate log each run — GUI reads the whole file to show latest batch
    log_path.write_text("")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path),
        ],
        force=True,
    )
    log = logging.getLogger("zaksclips.batch")

    processed_path = Path(__file__).parent / ".processed_clips.json"
    meta_path = Path(__file__).parent / ".video_meta.json"

    def load_processed():
        if processed_path.exists():
            try:
                return set(json.loads(processed_path.read_text()))
            except Exception:
                return set()
        return set()

    def save_processed(ids):
        processed_path.write_text(json.dumps(list(ids)))

    def load_meta():
        if meta_path.exists():
            try:
                return json.loads(meta_path.read_text())
            except Exception:
                return {}
        return {}

    def save_meta(meta):
        meta_path.write_text(json.dumps(meta, indent=2))

    # Platform guard
    if PLATFORM != "youtube":
        log.error(f"Batch mode requires PLATFORM=youtube (current: {PLATFORM})")
        return

    if not all([TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TWITCH_USERNAME]):
        log.error("Missing Twitch credentials in .env")
        return

    if not YOUTUBE_API_KEY or not YOUTUBE_CHANNEL_HANDLE:
        log.error("Missing YOUTUBE_API_KEY or YOUTUBE_CHANNEL_HANDLE in .env")
        return

    # Fetch clips
    log.info(f"Fetching clips for @{TWITCH_USERNAME}...")
    try:
        token = get_twitch_token()
        user_id = get_user_id(token)
    except SystemExit:
        log.error("Failed to authenticate with Twitch. Check credentials.")
        return

    clips = get_recent_clips(token, user_id)
    if not clips:
        log.info("No clips found.")
        return

    processed_ids = load_processed()
    new_clips = [c for c in clips if c["id"] not in processed_ids and c.get("vod_offset") is not None]

    if not new_clips:
        log.info("No new clips to process.")
        return

    log.info(f"Found {len(new_clips)} new clip(s) to process")

    # Get YouTube channel ID once
    try:
        channel_id = get_youtube_channel_id()
    except SystemExit:
        log.error("YouTube channel not found. Check YOUTUBE_CHANNEL_HANDLE.")
        return

    success_count = 0
    fail_count = 0

    for clip in new_clips:
        try:
            log.info(f"Processing: {clip['title']}")

            vod_end = clip["vod_offset"] + clip["duration"]
            seg_start = max(0, vod_end - int(CLIP_LENGTH * 60))
            seg_end = vod_end

            # 1. Find and download VOD segment
            vods = find_youtube_vod(channel_id, clip["created_at"])
            if not vods:
                log.warning(f"No YouTube VOD found for clip date {clip['created_at'][:10]} — skipping")
                continue

            clip_dt = datetime.fromisoformat(clip["created_at"].replace("Z", "+00:00"))
            best = min(vods, key=lambda v: abs(
                (datetime.fromisoformat(v["snippet"]["publishedAt"].replace("Z", "+00:00")) - clip_dt).total_seconds()))
            vod_url = f"https://www.youtube.com/watch?v={best['id']['videoId']}"
            vod_title = best["snippet"]["title"]
            log.info(f"  VOD: {vod_title}")

            def to_yt_ts(s):
                s = int(s)
                h, r = divmod(s, 3600)
                m, sec = divmod(r, 60)
                return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"

            stamp = datetime.now().strftime("%m-%d_%H%M%S")
            video_path = RAW_DIR / f"yt_raw_{stamp}.mp4"
            section = f"*{to_yt_ts(seg_start)}-{to_yt_ts(seg_end)}"

            cmd = ["yt-dlp", "--download-sections", section,
                   "--force-keyframes-at-cuts",
                   "--no-overwrites",
                   "--merge-output-format", "mp4", "-o", str(video_path), vod_url]
            result = subprocess.run(cmd, text=True, capture_output=True)
            if result.returncode != 0:
                log.warning(f"  yt-dlp failed with --force-keyframes-at-cuts, retrying without...")
                cmd = ["yt-dlp", "--download-sections", section,
                       "--no-overwrites",
                       "--merge-output-format", "mp4", "-o", str(video_path), vod_url]
                result = subprocess.run(cmd, text=True, capture_output=True)
                if result.returncode != 0:
                    log.error(f"  yt-dlp failed: {result.stderr[:200]}")
                    continue

            log.info(f"  Downloaded: {video_path.name}")

            # 2. Transcribe
            whisper_result = transcribe(video_path)
            save_whisper_result(video_path, whisper_result)

            # 3. AI title & caption
            transcript_text = get_transcript_text(whisper_result)
            ai_title, ai_caption = None, None
            if ANTHROPIC_API_KEY:
                ai_title, ai_caption = generate_title_caption(transcript_text, clip["title"])

            title = ai_title or clip["title"]
            caption = ai_caption or title

            # 4. Generate subtitles & burn captions
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            ass_path = RAW_DIR / f"{ts}.ass"
            make_ass(whisper_result, ass_path)

            output_name = safe_filename(title, clip["created_at"])
            output_path = unique_output_path(COMPLETED_DIR, output_name)
            burn_captions(video_path, ass_path, title, output_path)

            ass_path.unlink(missing_ok=True)
            for p in RAW_DIR.glob("*.part"):
                p.unlink(missing_ok=True)

            log.info(f"  Ready: {output_path.name}")

            # 5. Write metadata
            meta = load_meta()
            vod_window = f"{fmt_time(seg_start)} – {fmt_time(seg_end)}"
            video_meta = {
                "date": clip["created_at"][:10],
                "mode": CLIP_MODE,
                "raw_path": str(video_path),
                "title": title,
                "caption": caption,
                "clip_id": clip["id"],
                "stream_title": vod_title,
                "vod_window": vod_window,
            }
            if ai_title:
                video_meta["ai_title"] = ai_title
            if ai_caption:
                video_meta["ai_caption"] = ai_caption

            if CLIP_MODE == "auto-post":
                # Auto-upload and copy to iCloud
                copy_to_icloud(output_path)
                log.info(f"  Copied to iCloud")

                yt_ok = False
                if YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET:
                    try:
                        url = upload_to_youtube(output_path, title, caption)
                        if url:
                            video_meta["youtube"] = True
                            log.info(f"  YouTube: {url}")
                            yt_ok = True
                    except Exception as e:
                        log.error(f"  YouTube upload failed: {e}")

                tt_ok = False
                if TIKTOK_CLIENT_KEY:
                    try:
                        publish_id = upload_to_tiktok(output_path, caption)
                        if publish_id:
                            video_meta["tiktok"] = True
                            log.info(f"  TikTok: {publish_id}")
                            tt_ok = True
                    except Exception as e:
                        log.error(f"  TikTok upload failed: {e}")

                video_meta["status"] = "posted" if (yt_ok or tt_ok) else "upload_failed"

                if video_meta["status"] == "posted":
                    log_to_voice(title, caption)

                # Clean up raw file after auto-post
                video_path.unlink(missing_ok=True)
            else:
                # Auto mode — queue for review, keep raw file
                video_meta["status"] = "ready_for_review"

            meta[output_path.name] = video_meta
            save_meta(meta)

            processed_ids.add(clip["id"])
            save_processed(processed_ids)

            success_count += 1

        except SystemExit:
            log.error(f"  System exit during processing of '{clip['title']}' — skipping")
            fail_count += 1
        except Exception as e:
            log.exception(f"  Failed to process '{clip['title']}': {e}")
            fail_count += 1

    # Notify user
    if success_count > 0:
        msg = f"{success_count} video(s) ready for review"
        if fail_count:
            msg += f", {fail_count} failed"
        subprocess.run(["osascript", "-e",
            f'display notification "{msg}" with title "ZaksClips"'])

    log.info(f"Batch complete: {success_count} processed, {fail_count} failed")


def main():
    print()
    print("  Chess Clip Automator")
    print("  ════════════════════")

    if "--batch" in sys.argv or "-b" in sys.argv:
        batch_process()
        return

    if "--post" in sys.argv:
        post_scheduled()
        return

    if "--schedule" in sys.argv or "-s" in sys.argv:
        schedule(test="--test" in sys.argv)
        return

    if "--unschedule" in sys.argv:
        unschedule()
        return

    if "--clean" in sys.argv or "-c" in sys.argv:
        clean()
        return

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
        seg_start = max(0, vod_end - int(CLIP_LENGTH * 60))
        seg_end = vod_end

        # 1. Download segment
        download_fn = download_from_youtube if PLATFORM == "youtube" else download_from_tiktok
        video_path = download_fn(seg_start, seg_end, clip)

        # 2. Preview raw video and adjust segment if needed
        video_path, seg_start, seg_end = preview_and_adjust(
            video_path, seg_start, seg_end, clip, download_fn)

        # 3. Title & description
        print(f"\n  Title (Enter to use Twitch title: \"{clip['title']}\"):")
        custom_title = input("  > ").strip()
        title = custom_title if custom_title else clip["title"]

        print("\n  Description / caption (Enter to skip):")
        description = input("  > ").strip()
        if not description:
            description = title

        # 4. Transcribe
        result = transcribe(video_path)

        # 5. Generate subtitles
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        ass_path = RAW_DIR / f"{stamp}.ass"
        make_ass(result, ass_path)

        # 6. Burn in
        output_name = safe_filename(title, clip["created_at"])
        output_path = unique_output_path(COMPLETED_DIR, output_name)
        burn_captions(video_path, ass_path, title, output_path)

        print()
        print(f"  🎬 Ready to post → {output_path}")

        # 7. Preview final & optional upload
        preview_and_upload(output_path, title, description)

    print()
    print("  All done!")
    print()


if __name__ == "__main__":
    main()
