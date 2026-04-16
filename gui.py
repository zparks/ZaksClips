#!/usr/bin/env python3
"""
ZaksClips GUI — Mac desktop app wrapping clipper.py
"""

import os
import re
import sys
import json
import builtins
from collections import OrderedDict
import threading
import subprocess
import time
from pathlib import Path
from datetime import datetime, timedelta

import av
import numpy as np
import sounddevice as sd
from PIL import Image, ImageTk
import customtkinter as ctk
from tkinter import TclError
from tkinter import messagebox

# Import backend from clipper
import clipper

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class ClipperError(Exception):
    """Raised instead of sys.exit() when running in GUI mode."""
    pass


class SkipClip(Exception):
    """Raised when user clicks Skip Clip to abandon the current clip."""
    pass



class LogRedirector:
    """Redirect ALL stdout (including subprocesses) to a GUI text widget."""
    def __init__(self, text_widget, app):
        self.text_widget = text_widget
        self.app = app
        self._real_stdout = sys.stdout

        # Capture OS-level stdout so subprocess output goes to GUI too
        self._orig_fd = os.dup(1)  # save original stdout fd
        self._read_fd, self._write_fd = os.pipe()
        os.dup2(self._write_fd, 1)  # redirect fd 1 to our pipe
        os.close(self._write_fd)

        # Reader thread: reads from pipe, writes to real stdout + GUI
        def _reader():
            with os.fdopen(self._read_fd, 'r', errors='replace') as f:
                for line in f:
                    # Write to real terminal
                    os.write(self._orig_fd, line.encode())
                    # Write to GUI
                    stripped = line.rstrip()
                    if stripped:
                        self.app.after(0, self._append, stripped)
        self._reader_thread = threading.Thread(target=_reader, daemon=True)
        self._reader_thread.start()

    def write(self, msg):
        if msg.strip():
            self.app.after(0, self._append, msg)
        # Write to real terminal fd directly (bypass the pipe to avoid duplicates)
        try:
            os.write(self._orig_fd, msg.encode())
        except OSError:
            pass

    def _append(self, msg):
        try:
            self.text_widget.configure(state="normal")
            self.text_widget.insert("end", msg.rstrip() + "\n")
            self.text_widget.see("end")
            self.text_widget.configure(state="disabled")
        except TclError:
            pass

    def flush(self):
        pass


class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("ZaksClips")
        screen_h = self.winfo_screenheight()
        # Leave room for the macOS menu bar + dock (~80px)
        win_h = screen_h - 80
        self.geometry(f"900x{win_h}+0+0")
        self.minsize(750, 600)

        self.clips = []
        self.clip_vars = []  # BooleanVar per clip
        self._processing = False
        self._in_review = False  # True when in review/edit flow (vs clip processing)
        self._cancelled = threading.Event()
        self._schedule_mode = clipper.CLIP_MODE  # "off", "auto", or "auto-post"
        self._active_player = None  # track video player for cleanup on step jump
        self._step_labels = []     # CTkLabel references for step tracker
        self._step_names = []      # step name strings
        self._processed_file = Path(__file__).parent / ".processed_clips.json"
        self._processed_ids = self._load_processed_ids()
        self._known_videos = set(f.name for f in clipper.COMPLETED_DIR.glob("*.mp4"))

        self._build_ui()

        # Redirect stdout to the log
        self._log_redirector = LogRedirector(self.log_box, self)
        sys.stdout = self._log_redirector

        # Keyboard shortcuts
        self.bind("<Escape>", lambda e: self._on_escape())

        # Enable trackpad/mousewheel scrolling on all CTkScrollableFrames
        # Tk 9 on macOS uses <TouchpadScroll> instead of <MouseWheel>
        self._scroll_target = None
        self._scroll_last_widget = None
        self._scroll_can_scroll = False
        self.bind_all("<TouchpadScroll>", self._on_scroll, add=True)
        self.bind_all("<MouseWheel>", self._on_scroll, add=True)

        # Check credentials on startup
        self.after(200, self._check_credentials)
        self.after(200, self._update_schedule_banner)

        # Worker log path for batch output display
        self._worker_log_path = clipper.OUTPUT_DIR / "worker.log"

    def _on_scroll(self, event):
        """Route scroll events to the CTkScrollableFrame under the cursor."""
        # Only re-lookup target when the source widget changes
        w = event.widget
        if w is not self._scroll_last_widget:
            self._scroll_last_widget = w
            self._scroll_target = None
            self._scroll_can_scroll = False
            while w:
                if isinstance(w, ctk.CTkScrollableFrame):
                    self._scroll_target = w
                    # Cache whether content is scrollable
                    try:
                        top, bottom = w._parent_canvas.yview()
                        self._scroll_can_scroll = not (top <= 0.0 and bottom >= 1.0)
                    except (AttributeError, TclError):
                        pass
                    break
                try:
                    w = w.master
                except AttributeError:
                    break
        if self._scroll_target is None or not self._scroll_can_scroll:
            return
        # Tk 9 packs Y scroll into the low 16 bits of delta (signed)
        raw = event.delta
        y = (raw & 0xFFFF)
        if y > 32767:
            y -= 65536
        if y == 0:
            return
        try:
            step = max(-3, min(3, -y))
            self._scroll_target._parent_canvas.yview_scroll(step, "units")
        except (AttributeError, TclError):
            self._scroll_target = None


    # ── Layout ──────────────────────────────────────────────────────────

    def _build_ui(self):
        # Top bar
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(12, 0))

        ctk.CTkLabel(top, text="ZaksClips", font=ctk.CTkFont(size=22, weight="bold")).pack(side="left")

        # Schedule selector on main page
        self._main_mode_seg = ctk.CTkSegmentedButton(
            top, values=["off", "auto", "auto-post"],
            font=ctk.CTkFont(size=12), width=240, height=30,
            command=self._on_main_mode_change)
        self._main_mode_seg.set(self._schedule_mode)
        self._main_mode_seg.pack(side="left", padx=(16, 0))

        # Inline time picker (shown when schedule is on)
        self._time_frame = ctk.CTkFrame(top, fg_color="transparent")
        ctk.CTkLabel(self._time_frame, text="at", text_color="#888",
                     font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 4))
        _hour_opts = [f"{h % 12 or 12}{'am' if h < 12 else 'pm'}" for h in range(24)]
        self._main_hour_var = ctk.StringVar(value=_hour_opts[clipper.SCHEDULE_HOUR])
        self._main_hour_menu = ctk.CTkOptionMenu(
            self._time_frame, values=_hour_opts, variable=self._main_hour_var,
            width=75, height=28, font=ctk.CTkFont(size=11),
            command=self._on_main_time_change)
        self._main_hour_menu.pack(side="left")
        ctk.CTkLabel(self._time_frame, text=":", text_color="#888",
                     font=ctk.CTkFont(size=12)).pack(side="left", padx=1)
        _min_opts = [f"{m:02d}" for m in range(0, 60, 5)]
        _cur_min = f"{(clipper.SCHEDULE_MINUTE // 5) * 5:02d}"
        if _cur_min not in _min_opts:
            _cur_min = "00"
        self._main_min_var = ctk.StringVar(value=_cur_min)
        self._main_min_menu = ctk.CTkOptionMenu(
            self._time_frame, values=_min_opts, variable=self._main_min_var,
            width=55, height=28, font=ctk.CTkFont(size=11),
            command=self._on_main_time_change)
        self._main_min_menu.pack(side="left")
        if self._schedule_mode in ("auto", "auto-post"):
            self._time_frame.pack(side="left", padx=(6, 0))

        self.settings_btn = ctk.CTkButton(top, text="Settings", width=80, command=self._open_settings)
        self.settings_btn.pack(side="right", padx=(8, 0))

        self.help_btn = ctk.CTkButton(top, text="Help", width=60, fg_color="#555",
                                       hover_color="#666", command=self._open_help)
        self.help_btn.pack(side="right", padx=(8, 0))

        self.cancel_btn = ctk.CTkButton(top, text="Cancel", width=80, fg_color="#c0392b",
                                         hover_color="#e74c3c", command=self._cancel_processing)
        self.cancel_btn.pack(side="right", padx=(8, 0))
        self.cancel_btn.pack_forget()  # hidden until processing starts

        # Mode banner (auto = soft green, danger = red)
        self.mode_banner = ctk.CTkFrame(self, corner_radius=8, height=32)
        self.mode_banner_label = ctk.CTkLabel(
            self.mode_banner, text="",
            font=ctk.CTkFont(size=12), text_color="white")
        self.mode_banner_label.pack(pady=6)

        # Connection status strip
        self.cred_strip = ctk.CTkFrame(self, fg_color="#1a1a1a", corner_radius=8, height=32)
        self.cred_strip.pack(fill="x", padx=16, pady=(8, 0))

        self.cred_twitch = ctk.CTkLabel(self.cred_strip, text="", font=ctk.CTkFont(size=11))
        self.cred_twitch.pack(side="left", padx=(12, 16), pady=4)
        self.cred_youtube = ctk.CTkLabel(self.cred_strip, text="", font=ctk.CTkFont(size=11))
        self.cred_youtube.pack(side="left", padx=(0, 16), pady=4)
        self.cred_tiktok = ctk.CTkLabel(self.cred_strip, text="", font=ctk.CTkFont(size=11))
        self.cred_tiktok.pack(side="left", padx=(0, 12), pady=4)

        # Status label (used during fetch/processing)
        self.status_label = ctk.CTkLabel(self, text="", text_color="#888")

        # Days variable (for fetch dialog)
        self.days_var = ctk.IntVar(value=clipper.CLIP_DAYS)

        # ── Step tracker bar (hidden by default, shown during processing) ──
        self.step_bar = ctk.CTkFrame(self, fg_color="#1a1a1a", corner_radius=8, height=44)

        self.step_clip_label = ctk.CTkLabel(self.step_bar, text="", text_color="#888",
                                             font=ctk.CTkFont(size=11))
        self.step_clip_label.pack(side="left", padx=(12, 8), pady=6)

        self.step_inner = ctk.CTkFrame(self.step_bar, fg_color="transparent")
        self.step_inner.pack(side="left", fill="x", expand=True, pady=6)

        # ── Main content area ──
        self.content_area = ctk.CTkFrame(self, fg_color="transparent")
        self.content_area.pack(fill="both", expand=True, padx=16, pady=(8, 0))

        # Main panel = My Videos (home screen)
        self.main_panel = ctk.CTkFrame(self.content_area, fg_color="transparent")
        self.main_panel.pack(fill="both", expand=True)

        self.output_tab = self.main_panel
        self._build_output_tab()

        # Overlay frame — hidden by default, replaces main_panel when shown
        self.overlay = ctk.CTkFrame(self.content_area, fg_color="transparent")

        # Progress bar (hidden by default — shown only during downloads/processing)
        self.progress = ctk.CTkProgressBar(self)
        self.progress.set(0)
        self._progress_visible = False

        # Thinking indicator — animated dots next to status label
        self._thinking = False
        self._thinking_dots = 0
        self._thinking_after_id = None

        # Toast banner (hidden by default, shows errors/warnings)
        self.toast_frame = ctk.CTkFrame(self, corner_radius=8, height=36)
        # Not packed — shown via _show_toast()
        self.toast_label = ctk.CTkLabel(self.toast_frame, text="", font=ctk.CTkFont(size=12),
                                         text_color="white")
        self.toast_label.pack(side="left", padx=12, pady=6)
        self.toast_dismiss = ctk.CTkButton(self.toast_frame, text="✕", width=28, height=28,
                                            fg_color="transparent", hover_color="#ffffff20",
                                            command=self._dismiss_toast)
        self.toast_dismiss.pack(side="right", padx=(0, 8), pady=4)
        self._toast_timer = None

        # Log area (collapsible, hidden by default)
        self._log_visible = False
        self._last_batch_errors_hash = None  # track which errors we've already toasted
        self.log_toggle = ctk.CTkButton(self, text="▶ Logs", width=70, height=24,
                                         fg_color="transparent", text_color="#888",
                                         hover_color="#333",
                                         font=ctk.CTkFont(size=12),
                                         anchor="w", command=self._toggle_logs)
        self.log_toggle.pack(anchor="w", padx=16, pady=(6, 0))

        self.log_box = ctk.CTkTextbox(self, height=160, state="disabled",
                                       font=ctk.CTkFont(family="Menlo", size=12))

    def _toggle_logs(self):
        if self._log_visible:
            self.log_box.pack_forget()
            self._log_visible = False
        else:
            self._load_worker_log()
            self.log_box.pack(fill="both", expand=True, padx=16, pady=(4, 12))
            self._log_visible = True
        # _load_worker_log sets the badge/color; for collapse just update the arrow
        current = self.log_toggle.cget("text")
        if self._log_visible:
            self.log_toggle.configure(text=current.replace("▶", "▼"))
        else:
            self.log_toggle.configure(text=current.replace("▼", "▶"))

    def _load_worker_log(self):
        """Load worker.log contents into the log panel (replaces previous batch output).
        Also checks for errors and surfaces them via toast + Logs badge."""
        try:
            if not self._worker_log_path.exists():
                self._clear_log_error_badge()
                return
            text = self._worker_log_path.read_text().strip()
            if not text:
                self._clear_log_error_badge()
                return
            # Clear log panel and show batch output
            self.log_box.configure(state="normal")
            self.log_box.delete("1.0", "end")
            error_messages = []
            for line in text.splitlines():
                if not line.strip():
                    continue
                # Strip logging timestamp: "2025-04-05 18:05:00,123 [INFO] msg" → msg
                parts = line.split("] ", 1)
                msg = parts[1] if len(parts) > 1 else line
                self.log_box.insert("end", f"[batch] {msg}\n")
                # Collect ERROR lines for toast
                if "[ERROR]" in line:
                    error_messages.append(msg)
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

            # Surface batch errors on the GUI
            if error_messages:
                errors_hash = hash(tuple(error_messages))
                if errors_hash != self._last_batch_errors_hash:
                    self._last_batch_errors_hash = errors_hash
                    n = len(error_messages)
                    self._show_toast(
                        f"Last batch run had {n} error{'s' if n != 1 else ''} — check Logs",
                        "#c0392b", duration=12000)
                # Red badge on Logs toggle
                prefix = "▼" if self._log_visible else "▶"
                self.log_toggle.configure(
                    text=f"{prefix} Logs  ● {len(error_messages)}",
                    text_color="#e74c3c")
            else:
                self._clear_log_error_badge()
        except Exception:
            pass

    def _clear_log_error_badge(self):
        """Reset the Logs toggle to its normal appearance."""
        prefix = "▼" if self._log_visible else "▶"
        self.log_toggle.configure(text=f"{prefix} Logs", text_color="#888")

    # ── Overlay helpers ────────────────────────────────────────────────

    def _show_overlay(self):
        """Hide main panel and show the overlay frame."""
        self.main_panel.pack_forget()
        # Clear any old overlay content
        for w in self.overlay.winfo_children():
            w.destroy()
        self.overlay.pack(fill="both", expand=True)

    def _hide_overlay(self):
        """Hide overlay and restore the main panel."""
        self.overlay.pack_forget()
        for w in self.overlay.winfo_children():
            w.destroy()
        self.main_panel.pack(fill="both", expand=True)

    def _on_escape(self):
        """Handle Escape key — close settings overlay, or cancel if processing."""
        if self.overlay.winfo_ismapped():
            # If in settings (not processing), just close
            if not self._processing:
                self._hide_overlay()
            # If in review/edit flow, confirm before discarding edits
            elif self._in_review:
                if messagebox.askyesno("Discard Changes?",
                                       "You have unsaved edits. Discard changes and close?"):
                    self._cancel_processing()
            # If processing clips, treat as cancel
            else:
                self._cancel_processing()

    # ── Processed clip tracking ──────────────────────────────────────

    def _load_processed_ids(self):
        """Load set of processed clip IDs from disk."""
        if self._processed_file.exists():
            try:
                return set(json.loads(self._processed_file.read_text()))
            except Exception:
                return set()
        return set()

    def _mark_processed(self, clip_id):
        """Mark a clip ID as processed and save to disk."""
        self._processed_ids.add(clip_id)
        self._processed_file.write_text(json.dumps(list(self._processed_ids)))

    # ── Video metadata tracking ─────────────────────────────────────

    def _load_video_meta(self):
        """Load video metadata dict from disk. Keys are filenames."""
        meta_path = Path(__file__).parent / ".video_meta.json"
        if meta_path.exists():
            try:
                return json.loads(meta_path.read_text())
            except Exception:
                return {}
        return {}

    def _save_video_meta(self, meta):
        """Save video metadata dict to disk."""
        meta_path = Path(__file__).parent / ".video_meta.json"
        meta_path.write_text(json.dumps(meta, indent=2))

    def _set_video_status(self, filename, **kwargs):
        """Update metadata for a video file. Pass youtube=True, tiktok=True, etc."""
        meta = self._load_video_meta()
        if filename not in meta:
            meta[filename] = {"status": "approved", "date": datetime.now().strftime("%Y-%m-%d")}
        meta[filename].update(kwargs)
        self._save_video_meta(meta)

    def _get_video_status(self, filename):
        """Get status dict for a video. Returns defaults if not tracked."""
        meta = self._load_video_meta()
        return meta.get(filename, {})

    # ── Step tracker bar ─────────────────────────────────────────────

    def _show_step_bar(self, step_names, clip_idx=0, total_clips=1):
        """Show the step tracker bar with named steps (display-only)."""
        self._step_names = step_names
        self._step_labels = []
        self._current_step_idx = 0

        # Clear old step labels
        for w in self.step_inner.winfo_children():
            w.destroy()

        # Clip counter
        if total_clips > 1:
            self.step_clip_label.configure(text=f"Clip {clip_idx + 1}/{total_clips}")
        else:
            self.step_clip_label.configure(text="")

        for i, name in enumerate(step_names):
            lbl = ctk.CTkLabel(self.step_inner, text=name,
                                font=ctk.CTkFont(size=12, weight="bold" if i == 0 else "normal"),
                                text_color="#1D8CD7" if i == 0 else "#555",
                                padx=6)
            lbl.pack(side="left")
            self._step_labels.append(lbl)

            # Arrow separator between steps
            if i < len(step_names) - 1:
                sep = ctk.CTkLabel(self.step_inner, text="\u203A", text_color="#444",
                                    font=ctk.CTkFont(size=14))
                sep.pack(side="left", padx=2)

        self.step_bar.pack(fill="x", padx=16, pady=(8, 0), before=self.content_area)

    def _set_current_step(self, step_idx):
        """Set current step index immediately."""
        self._current_step_idx = step_idx

    def _update_step(self, step_idx, clip_idx=None, total_clips=None):
        """Highlight the current step in the step bar (must run on main thread)."""
        self._current_step_idx = step_idx

        if clip_idx is not None and total_clips is not None and total_clips > 1:
            self.step_clip_label.configure(text=f"Clip {clip_idx + 1}/{total_clips}")

        for i, lbl in enumerate(self._step_labels):
            if i < step_idx:
                # Completed
                lbl.configure(text_color="#4CAF50",
                              font=ctk.CTkFont(size=12, weight="normal"))
            elif i == step_idx:
                # Active
                lbl.configure(text_color="#1D8CD7",
                              font=ctk.CTkFont(size=12, weight="bold"))
            else:
                # Upcoming
                lbl.configure(text_color="#555",
                              font=ctk.CTkFont(size=12, weight="normal"))

    def _hide_step_bar(self):
        """Hide the step tracker bar."""
        self.step_bar.pack_forget()

    # ── Working overlay (big, obvious progress display) ───────────────

    def _show_working(self, title, subtitle=""):
        """Show a big centered working overlay that takes over the content area."""
        self.main_panel.pack_forget()
        # Clear any old overlay content
        for w in self.overlay.winfo_children():
            w.destroy()
        self.overlay.pack(fill="both", expand=True)

        # Center container
        center = ctk.CTkFrame(self.overlay, fg_color="transparent")
        center.place(relx=0.5, rely=0.45, anchor="center")

        # Animated spinner character
        self._working_spinner_idx = 0
        self._working_spinner_label = ctk.CTkLabel(
            center, text="◐", font=ctk.CTkFont(size=48),
            text_color="#1D8CD7")
        self._working_spinner_label.pack(pady=(0, 12))

        # Main title (e.g. "Transcribing Audio")
        self._working_title = ctk.CTkLabel(
            center, text=title,
            font=ctk.CTkFont(size=20, weight="bold"))
        self._working_title.pack(pady=(0, 4))

        # Subtitle (e.g. clip name or extra detail)
        self._working_subtitle = ctk.CTkLabel(
            center, text=subtitle, text_color="#888",
            font=ctk.CTkFont(size=13))
        self._working_subtitle.pack(pady=(0, 16))

        # Big progress bar
        self._working_progress = ctk.CTkProgressBar(center, width=360, height=12,
                                                      corner_radius=6)
        self._working_progress.pack(pady=(0, 8))
        self._working_progress.configure(mode="indeterminate")
        self._working_progress.start()

        # Percentage / detail label
        self._working_detail = ctk.CTkLabel(
            center, text="", text_color="#666",
            font=ctk.CTkFont(size=12))
        self._working_detail.pack()

        # Start spinner animation
        self._working_spinning = True
        self._animate_spinner()

    def _update_working(self, title=None, subtitle=None, detail=None, progress=None):
        """Update the working overlay text and optionally set determinate progress."""
        if not hasattr(self, '_working_title'):
            return
        if title is not None:
            self._working_title.configure(text=title)
        if subtitle is not None:
            self._working_subtitle.configure(text=subtitle)
        if detail is not None:
            self._working_detail.configure(text=detail)
        if progress is not None:
            # Switch to determinate mode with a real percentage
            self._working_progress.stop()
            self._working_progress.configure(mode="determinate")
            self._working_progress.set(progress)
        elif progress is None and title is not None:
            # New step — reset to indeterminate
            try:
                self._working_progress.configure(mode="indeterminate")
                self._working_progress.start()
            except Exception:
                pass

    def _hide_working(self):
        """Hide the working overlay and restore the main panel."""
        self._working_spinning = False
        try:
            self._working_progress.stop()
        except Exception:
            pass
        self.overlay.pack_forget()
        for w in self.overlay.winfo_children():
            w.destroy()
        self.main_panel.pack(fill="both", expand=True)

    def _animate_spinner(self):
        """Rotate the spinner character."""
        if not self._working_spinning:
            return
        chars = "◐◓◑◒"
        self._working_spinner_idx = (self._working_spinner_idx + 1) % len(chars)
        try:
            self._working_spinner_label.configure(text=chars[self._working_spinner_idx])
        except Exception:
            return
        self.after(200, self._animate_spinner)

    # ── Synced A/V playback ─────────────────────────────────────────

    def _get_thumbnail(self, video_path, width=45, height=80):
        """Extract a thumbnail from a video at ~3s. Caches to .thumb.jpg."""
        thumb_path = Path(video_path).with_suffix(".thumb.jpg")
        if thumb_path.exists():
            try:
                img = Image.open(thumb_path)
                return ctk.CTkImage(light_image=img, dark_image=img, size=(width, height))
            except Exception:
                pass
        # Generate thumbnail
        try:
            container = av.open(str(video_path))
            vst = container.streams.video[0]
            # Seek to ~3 seconds
            target = int(3 * av.time_base)
            container.seek(target)
            for frame in container.decode(video=0):
                img = frame.to_image()
                img = img.resize((width, height), Image.LANCZOS)
                img.save(str(thumb_path), "JPEG", quality=70)
                container.close()
                return ctk.CTkImage(light_image=img, dark_image=img, size=(width, height))
            container.close()
        except Exception:
            pass
        return None

    def _draw_title_overlay(self, img, title_text, theme_key, y_pct):
        """Draw a title overlay on a PIL image, matching the burned style."""
        from PIL import ImageDraw, ImageFont
        if not title_text.strip():
            return img
        theme = clipper.COLOR_THEMES.get(theme_key, clipper.COLOR_THEMES["blue"])
        box_color = theme["box"]
        text_color = theme["text"]

        img = img.copy()
        draw = ImageDraw.Draw(img)
        w, h = img.size

        # Scale font size relative to display size (72pt at 1080w)
        fontsize = max(12, int(w * 72 / 1080))
        pad = max(4, int(fontsize * 0.28))
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", fontsize)
        except Exception:
            font = ImageFont.load_default()

        # Wrap title like ffmpeg does (~14 chars per line)
        words = title_text.upper().split()
        lines = []
        cur = ""
        for word in words:
            if cur and len(cur) + 1 + len(word) > 14:
                lines.append(cur)
                cur = word
            else:
                cur = f"{cur} {word}" if cur else word
        if cur:
            lines.append(cur)

        line_h = fontsize + 2 * pad
        total_h = len(lines) * line_h
        base_y = int(h * y_pct / 100) - total_h // 2

        for i, line in enumerate(lines):
            bbox = draw.textbbox((0, 0), line, font=font)
            tw = bbox[2] - bbox[0]
            tx = (w - tw) // 2
            ty = base_y + i * line_h
            # Draw box
            draw.rectangle([tx - pad, ty - pad, tx + tw + pad, ty + fontsize + pad],
                           fill=box_color)
            # Draw text
            draw.text((tx, ty), line, fill=text_color, font=font)

        return img

    def _create_player(self, ov, video_path, max_w=640, max_h=500, title_overlay_fn=None):
        """Create a video player with synced audio. Returns state dict with 'stop' and 'ctrl_frame'.
        title_overlay_fn: optional callable() → (title_text, theme_key, y_pct) for live title preview."""
        # Release MLX/Metal GPU resources before opening video to avoid trace trap crashes
        import gc
        gc.collect()
        try:
            import mlx.core as mx
            mx.eval(mx.zeros(1))  # force Metal sync/flush
        except Exception:
            pass

        probe = av.open(str(video_path))
        vs = probe.streams.video[0]
        vid_w, vid_h = vs.width, vs.height
        duration_s = float(probe.duration) / av.time_base if probe.duration else 0
        has_audio = len(probe.streams.audio) > 0
        print(f"  Player: {Path(video_path).name} — audio={'yes' if has_audio else 'NO'}")
        if has_audio:
            a_rate = probe.streams.audio[0].rate or 44100
            a_ch = probe.streams.audio[0].channels or 2
        else:
            a_rate, a_ch = 44100, 2
        probe.close()

        scale = min(max_w / vid_w, max_h / vid_h, 1.0)
        dw, dh = int(vid_w * scale), int(vid_h * scale)

        video_label = ctk.CTkLabel(ov, text="Loading...", width=dw, height=dh)
        video_label.pack(pady=(2, 2))

        state = {"playing": True, "stopped": False, "seek_to": None,
                 "current_time": 0.0, "photo": None}

        def _fmt(s):
            m, s = divmod(int(s), 60)
            return f"{m}:{s:02d}"

        time_var = ctk.StringVar(value=f"0:00 / {_fmt(duration_s)}")
        ctk.CTkLabel(ov, textvariable=time_var, font=ctk.CTkFont(family="Menlo", size=11),
                      text_color="#aaa").pack()

        seek_var = ctk.DoubleVar(value=0.0)
        sdrag = [False]
        def _sp(e): sdrag[0] = True
        def _sr(e):
            sdrag[0] = False
            state["seek_to"] = seek_var.get()
        slider = ctk.CTkSlider(ov, from_=0, to=max(duration_s, 1), variable=seek_var, width=dw)
        slider.pack(pady=(0, 2))
        slider.bind("<ButtonPress-1>", _sp)
        slider.bind("<ButtonRelease-1>", _sr)

        ctrl = ctk.CTkFrame(ov, fg_color="transparent")
        ctrl.pack(pady=(0, 4))

        def _toggle():
            state["playing"] = not state["playing"]
            pbtn.configure(text="\u23f8" if state["playing"] else "\u25b6")
        pbtn = ctk.CTkButton(ctrl, text="\u23f8", width=40, command=_toggle)
        pbtn.pack(side="left", padx=4)

        def _open_qt():
            subprocess.Popen(["open", str(video_path)])
        ctk.CTkButton(ctrl, text="Open in QuickTime", width=140, fg_color="#555",
                       font=ctk.CTkFont(size=11), command=_open_qt).pack(side="left", padx=4)

        state["ctrl_frame"] = ctrl
        sd_stream = [None]
        playback_done = threading.Event()

        def _stop():
            state["stopped"] = True
            state["playing"] = False
            # Stop audio immediately — don't wait for playback thread
            s = sd_stream[0]
            if s:
                try: s.abort(); s.close()
                except Exception: pass
                sd_stream[0] = None
            # Wait for the playback thread to fully exit before destroying widgets
            playback_done.wait(timeout=2.0)
            # Ensure audio device is fully released to prevent trace trap on next player
            import gc; gc.collect()
            time.sleep(0.2)
        state["stop"] = _stop

        def _playback(start_offset=0.0):
            try:
                container = av.open(str(video_path))
            except Exception as e:
                print(f"  Video open failed: {e}")
                return
            vst = container.streams.video[0]
            vst.thread_type = "AUTO"

            astream = None
            resampler = None
            audio_q = None
            audio_thread = None
            if has_audio:
                try:
                    astream = sd.OutputStream(samplerate=a_rate, channels=a_ch,
                                               dtype='float32', blocksize=4096)
                    astream.start()
                    sd_stream[0] = astream
                    layout = 'stereo' if a_ch >= 2 else 'mono'
                    resampler = av.AudioResampler(format='fltp', layout=layout, rate=a_rate)
                    import queue
                    audio_q = queue.Queue(maxsize=64)
                    def _audio_writer():
                        while True:
                            chunk = audio_q.get()
                            if chunk is None:
                                break
                            try:
                                astream.write(chunk)
                            except Exception:
                                pass
                    audio_thread = threading.Thread(target=_audio_writer, daemon=True)
                    audio_thread.start()
                except Exception as e:
                    print(f"  Audio init failed: {e}")
                    astream = None
                    audio_q = None

            if start_offset > 0.5:
                container.seek(int(start_offset * av.time_base))

            wall_start = time.monotonic()
            audio_start_time = [None]  # sounddevice hardware time when audio started
            pts_start = None

            for packet in container.demux():
                if state["stopped"]: break
                if state["seek_to"] is not None:
                    target = state["seek_to"]
                    state["seek_to"] = None
                    container.seek(int(target * av.time_base))
                    pts_start = None
                    wall_start = time.monotonic()
                    audio_start_time[0] = None
                    if audio_q:
                        while not audio_q.empty():
                            try: audio_q.get_nowait()
                            except Exception: break
                    if astream:
                        try: astream.stop(); astream.start()
                        except Exception: pass
                    continue

                pt = None
                while not state["playing"] and not state["stopped"]:
                    if pt is None: pt = time.monotonic()
                    if state["seek_to"] is not None: break
                    time.sleep(0.05)
                if pt is not None:
                    wall_start += time.monotonic() - pt
                    # Reset audio clock reference after pause
                    if astream and audio_start_time[0] is not None:
                        audio_start_time[0] = astream.time - (time.monotonic() - wall_start)
                if state["stopped"]: break
                if state["seek_to"] is not None: continue

                for frame in packet.decode():
                    if state["stopped"]: break
                    if isinstance(frame, av.VideoFrame):
                        ft = float(frame.pts * vst.time_base) if frame.pts else 0.0
                        state["current_time"] = ft
                        if pts_start is None: pts_start = ft

                        # Sync to audio hardware clock when available
                        if astream and audio_start_time[0] is not None:
                            elapsed = astream.time - audio_start_time[0]
                            delay = (ft - pts_start) - elapsed
                        else:
                            delay = (ft - pts_start) - (time.monotonic() - wall_start)
                        if delay > 0.005: time.sleep(delay)
                        elif delay < -0.1: continue
                        img = frame.to_image()
                        if scale != 1.0:
                            img = img.resize((dw, dh), Image.LANCZOS)
                        # Live title overlay (first 5 seconds)
                        if title_overlay_fn and ft <= 5.0:
                            try:
                                overlay_info = title_overlay_fn()
                                if overlay_info:
                                    img = self._draw_title_overlay(img, *overlay_info)
                            except Exception:
                                pass
                        def _disp(img=img, ft=ft):
                            if state["stopped"]: return
                            try:
                                photo = ctk.CTkImage(light_image=img, dark_image=img, size=(dw, dh))
                                state["photo"] = photo
                                video_label.configure(image=photo, text="")
                                time_var.set(f"{_fmt(ft)} / {_fmt(duration_s)}")
                                if not sdrag[0]: seek_var.set(ft)
                            except Exception:
                                pass  # widget destroyed
                        self.after(0, _disp)
                    elif isinstance(frame, av.AudioFrame):
                        if audio_q and state["playing"]:
                            # Record audio start time on first chunk
                            if audio_start_time[0] is None and astream:
                                audio_start_time[0] = astream.time
                            try:
                                for rf in resampler.resample(frame):
                                    arr = rf.to_ndarray()
                                    if arr.ndim == 2: arr = arr.T
                                    else: arr = arr.reshape(-1, 1)
                                    chunk = np.ascontiguousarray(arr, dtype=np.float32)
                                    try:
                                        audio_q.put_nowait(chunk)
                                    except Exception:
                                        pass  # drop audio if queue full
                            except Exception as e:
                                if not state.get("_audio_err_logged"):
                                    print(f"  Audio write error: {e}")
                                    state["_audio_err_logged"] = True

            container.close()
            if audio_q:
                audio_q.put(None)  # signal audio thread to stop
            if audio_thread:
                audio_thread.join(timeout=1)
            if astream:
                try: astream.stop(); astream.close()
                except Exception: pass
            if not state["stopped"]:
                # Pause at end instead of looping — user can click play to restart
                state["playing"] = False
                try:
                    self.after(0, lambda: seek_var.set(0))
                    self.after(0, lambda: pbtn.configure(text="\u25b6"))
                    self.after(0, lambda: time_var.set(f"0:00 / {_fmt(duration_s)}  (ended)"))
                except Exception:
                    pass

                # Wait for user to click play again
                while not state["playing"] and not state["stopped"]:
                    if state["seek_to"] is not None:
                        break
                    time.sleep(0.1)
                if not state["stopped"]:
                    try:
                        self.after(0, lambda: pbtn.configure(text="\u23f8"))
                    except Exception:
                        pass
                    start = state["seek_to"] if state["seek_to"] is not None else 0.0
                    state["seek_to"] = None
                    _playback(start)

        def _playback_wrapper():
            try:
                _playback(0.0)
            finally:
                playback_done.set()
        threading.Thread(target=_playback_wrapper, daemon=True).start()
        return state

    # ── Embedded video preview ────────────────────────────────────────

    def _preview_video(self, video_path, title="Preview", subtitle=None, allow_skip=True):
        """Show video with synced audio. Blocks until Continue/Skip."""
        event = threading.Event()
        skipped = [False]

        def _show():
            self._show_overlay()
            ov = self.overlay
            scroll = ctk.CTkScrollableFrame(ov, fg_color="transparent")
            scroll.pack(fill="both", expand=True, padx=4, pady=4)
            ctk.CTkLabel(scroll, text=title, font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(8, 0))
            if subtitle:
                for line in subtitle.split("\n"):
                    ctk.CTkLabel(scroll, text=line, text_color="#aaa",
                                  font=ctk.CTkFont(size=12)).pack(pady=(0, 0))
                ctk.CTkFrame(scroll, height=4, fg_color="transparent").pack()
            else:
                ctk.CTkFrame(scroll, height=4, fg_color="transparent").pack()
            player = self._create_player(scroll, video_path)
            self._active_player = player  # track for cleanup on step jump
            ctrl = player["ctrl_frame"]

            def _continue():
                player["stop"]()
                self._active_player = None
                self._hide_overlay()
                event.set()
            def _skip_fn():
                player["stop"]()
                self._active_player = None
                skipped[0] = True
                self._hide_overlay()
                event.set()

            ctk.CTkButton(ctrl, text="Continue", width=120, fg_color="#1D8CD7",
                           command=_continue).pack(side="left", padx=4)
            if allow_skip:
                ctk.CTkButton(ctrl, text="Skip Clip", width=100, fg_color="#555",
                               text_color="#ccc", command=_skip_fn).pack(side="left", padx=4)

        self.after(0, _show)
        self._wait_or_cancel(event)
        if skipped[0]:
            raise SkipClip()

    # ── Thread-safe patching ─────────────────────────────────────────

    def _run_in_thread(self, fn, on_done=None):
        """Run fn in a daemon thread with sys.exit and input patched for GUI use."""
        def _wrapper():
            _real_exit = sys.exit
            _real_input = builtins.input

            def _gui_exit(code=0):
                raise ClipperError(f"clipper exited with code {code}")

            def _gui_input(prompt=""):
                # Route input() calls through a GUI dialog
                return self._ask_dialog("Input Needed", prompt.strip())

            sys.exit = _gui_exit
            builtins.input = _gui_input
            try:
                fn()
            except (Exception, SystemExit) as e:
                msg = str(e) if str(e) else type(e).__name__
                print(f"\n  Error: {msg}")
                self.after(0, self._set_status, f"Error: {msg}")
                self.after(0, self._show_toast, f"Error: {msg}")
            finally:
                sys.exit = _real_exit
                builtins.input = _real_input
                if on_done:
                    self.after(0, on_done)

        threading.Thread(target=_wrapper, daemon=True).start()

    # ── Fetch Clips ────────────────────────────────────────────────────

    def _fetch_clips(self):
        if self._processing:
            return
        self.fetch_btn.configure(state="disabled")
        self._start_thinking("Fetching clips")

        days = min(self.days_var.get(), 60)

        def _run():
            token = clipper.get_twitch_token()
            self.after(0, self._start_thinking, "Looking up channel")
            user_id = clipper.get_user_id(token)
            self.after(0, self._start_thinking, f"Fetching clips from last {days} days")
            clips = clipper.get_recent_clips(token, user_id, count=50, days=days)
            self.after(0, self._populate_clips, clips)

        def _done():
            self._stop_thinking()
            self.fetch_btn.configure(state="normal")

        self._run_in_thread(_run, on_done=_done)

    def _populate_clips(self, clips):
        # Clear old
        for w in self.clip_frame.winfo_children():
            w.destroy()

        self.clips = [c for c in clips if c.get("vod_offset") is not None]
        self.clip_vars = []

        skipped = len(clips) - len(self.clips)

        if not self.clips:
            ctk.CTkLabel(self.clip_frame, text="No clips with VOD data found.", text_color="#888").pack(pady=20)
            if skipped:
                ctk.CTkLabel(self.clip_frame,
                             text=f"({skipped} clip(s) skipped — VODs must be enabled on Twitch for clips to be processable)",
                             text_color="#666", font=ctk.CTkFont(size=11), wraplength=400).pack(pady=(0, 10))
            else:
                ctk.CTkLabel(self.clip_frame,
                             text="No clips found in this date range. Try increasing the number of days.",
                             text_color="#666", font=ctk.CTkFont(size=11)).pack(pady=(0, 10))
            self._set_status("0 clips")
            return

        # Header
        hdr = ctk.CTkFrame(self.clip_frame, fg_color="transparent")
        hdr.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(hdr, text="", width=30).pack(side="left")
        ctk.CTkLabel(hdr, text="Title", width=280, anchor="w",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(side="left", padx=4)
        ctk.CTkLabel(hdr, text="Date", width=90,
                     font=ctk.CTkFont(size=12, weight="bold")).pack(side="left", padx=4)
        ctk.CTkLabel(hdr, text="Creator", width=100,
                     font=ctk.CTkFont(size=12, weight="bold")).pack(side="left", padx=4)
        ctk.CTkLabel(hdr, text="VOD Window", width=140,
                     font=ctk.CTkFont(size=12, weight="bold")).pack(side="left", padx=4)

        for i, clip in enumerate(self.clips):
            is_done = clip["id"] in self._processed_ids
            var = ctk.BooleanVar(value=not is_done)
            self.clip_vars.append(var)

            row = ctk.CTkFrame(self.clip_frame, fg_color="transparent")
            row.pack(fill="x", pady=1)

            ctk.CTkCheckBox(row, text="", variable=var, width=30).pack(side="left")

            title_text = clip["title"][:40] + ("..." if len(clip["title"]) > 40 else "")
            if is_done:
                title_color = "#888"
                marker = "  (already in My Videos)"
            else:
                title_color = "white"
                marker = ""
            ctk.CTkLabel(row, text=title_text + marker, width=300, anchor="w",
                         text_color=title_color).pack(side="left", padx=4)
            ctk.CTkLabel(row, text=clip["created_at"][:10], width=90).pack(side="left", padx=4)
            ctk.CTkLabel(row, text=clip.get("creator_name", ""), width=100).pack(side="left", padx=4)

            vod_end = clip["vod_offset"] + clip["duration"]
            seg_start = max(0, vod_end - int(clipper.CLIP_LENGTH * 60))
            seg_text = f"{clipper.fmt_time(seg_start)} - {clipper.fmt_time(vod_end)}"
            ctk.CTkLabel(row, text=seg_text, width=140).pack(side="left", padx=4)

        self.select_all_clips_btn.configure(text="Deselect All")
        skipped_note = f" ({skipped} skipped — no VOD)" if skipped else ""
        self._set_status(f"{len(self.clips)} clips{skipped_note} — select and press Process")

        # Show action buttons now that clips are loaded
        self.auto_process_btn.pack(side="left")
        self.process_btn.pack(side="left", padx=(8, 0))
        self.select_all_clips_btn.pack(side="left", padx=(8, 0))

    def _select_all_clips(self):
        all_selected = all(v.get() for v in self.clip_vars) if self.clip_vars else False
        if all_selected:
            for v in self.clip_vars:
                v.set(False)
            self.select_all_clips_btn.configure(text="Select All")
            return
        for v in self.clip_vars:
            v.set(True)
        self.select_all_clips_btn.configure(text="Deselect All")

    def _set_status(self, text):
        try:
            self.status_label.configure(text=text)
        except TclError:
            pass

    def _show_progress(self):
        """Show the progress bar."""
        if not self._progress_visible:
            self.progress.pack(fill="x", padx=16, pady=(8, 0), before=self.log_toggle)
            self._progress_visible = True

    def _hide_progress(self):
        """Hide the progress bar."""
        if self._progress_visible:
            self.progress.pack_forget()
            self._progress_visible = False

    def _start_thinking(self, text=""):
        """Show animated thinking dots next to the status text."""
        self._thinking = True
        self._thinking_base_text = text
        self._thinking_dots = 0
        self._animate_thinking()

    def _stop_thinking(self):
        """Stop the thinking animation."""
        self._thinking = False
        if self._thinking_after_id:
            self.after_cancel(self._thinking_after_id)
            self._thinking_after_id = None

    def _animate_thinking(self):
        """Cycle dots: . .. ... on the status label."""
        if not self._thinking:
            return
        self._thinking_dots = (self._thinking_dots % 3) + 1
        dots = "·" * self._thinking_dots + " " * (3 - self._thinking_dots)
        self.status_label.configure(text=f"{self._thinking_base_text} {dots}")
        self._thinking_after_id = self.after(400, self._animate_thinking)

    def _start_indeterminate(self):
        """Switch to indeterminate mode — a segment slides across the bar."""
        self._show_progress()
        self.progress.configure(mode="indeterminate")
        self.progress.start()

    def _stop_indeterminate(self):
        """Stop indeterminate mode and switch back to determinate."""
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress.set(0)
        self._hide_progress()

    def _download_with_progress(self, download_fn, seg_start, seg_end, clip,
                                clip_idx, total_clips, total_steps):
        """Wrap a clipper download function, intercepting yt-dlp to show real progress."""
        _real_run = subprocess.run
        app = self

        def _patched_run(cmd, **kwargs):
            # Only intercept yt-dlp calls
            if not (isinstance(cmd, list) and cmd and "yt-dlp" in cmd[0]):
                return _real_run(cmd, **kwargs)

            # yt-dlp downloads video + audio separately then merges.
            # Track which stream we're on to give overall progress.
            stream_count = [0]  # [current_stream_index]
            total_streams = 2   # video + audio

            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=False
            )
            # yt-dlp uses \r to overwrite progress on the same line,
            # so we read raw bytes and split on both \r and \n.
            buf = b""
            while True:
                if app._cancelled.is_set():
                    proc.kill()
                    proc.wait()
                    raise ClipperError("Cancelled by user")
                chunk = proc.stdout.read(1)
                if not chunk:
                    break
                if chunk in (b"\r", b"\n"):
                    line = buf.decode("utf-8", errors="replace").strip()
                    buf = b""
                    if not line:
                        continue
                    # Detect new stream starting
                    if "[download] Destination:" in line:
                        stream_count[0] += 1

                    # Parse yt-dlp progress: [download]  45.2% of ~10.00MiB ...
                    m = re.search(r"\[download\]\s+([\d.]+)%", line)
                    if m:
                        stream_pct = float(m.group(1)) / 100.0
                        stream_idx = min(max(stream_count[0] - 1, 0), total_streams - 1)
                        overall_pct = (stream_idx + stream_pct) / total_streams
                        pct_int = int(overall_pct * 100)

                        app.after(0, app._update_working,
                                  None, None, f"{pct_int}%", overall_pct)
                    elif "[Merger]" in line or "[Merging]" in line or "Merging" in line:
                        app.after(0, app._update_working,
                                  "Merging Streams", None, "Almost done...", 0.95)
                    # Log all yt-dlp output to the log panel
                    print(line, flush=True)
                else:
                    buf += chunk
            proc.wait()
            return subprocess.CompletedProcess(cmd, proc.returncode)

        clip_title = clip.get("title", "")[:40]
        prefix = f"[{clip_idx+1}/{total_clips}] " if total_clips > 1 else ""
        app.after(0, app._stop_thinking)
        app.after(0, app._show_working, "Downloading Video",
                  f"{prefix}{clip_title}")
        subprocess.run = _patched_run
        try:
            result = download_fn(seg_start, seg_end, clip)
        finally:
            subprocess.run = _real_run
            app.after(0, app._hide_working)
        return result

    # ── Cancel ─────────────────────────────────────────────────────────

    def _cancel_processing(self):
        """Signal cancellation, stop any active player/overlay, and clean up."""
        self._cancelled.set()
        self._set_status("Cancelling...")
        self._hide_overlay()

    def _wait_or_cancel(self, event):
        """Wait for a threading.Event, but bail out if cancelled."""
        while not event.wait(timeout=0.2):
            if self._cancelled.is_set():
                self._stop_active_player()
                self.after(0, self._hide_overlay)
                raise ClipperError("Cancelled by user")
        if self._cancelled.is_set():
            raise ClipperError("Cancelled by user")

    def _stop_active_player(self):
        """Stop any currently playing video player."""
        if self._active_player:
            try:
                self._active_player["stop"]()
            except Exception:
                pass
            self._active_player = None


    # ── Process Selected Clips ─────────────────────────────────────────

    def _process_selected(self):
        if self._processing:
            return

        selected = [self.clips[i] for i, v in enumerate(self.clip_vars) if v.get()]
        if not selected:
            self._set_status("No clips selected")
            return

        self._processing = True
        self._cancelled.clear()
        try:
            self.process_btn.configure(state="disabled")
            self.fetch_btn.configure(state="disabled")
        except (AttributeError, Exception):
            pass
        self.cancel_btn.pack(side="right", padx=(8, 0))

        pass  # title/caption prompted per-clip during processing

        def _clip_subtitle(clip, clip_idx, total):
            """Build a subtitle string for working overlay."""
            prefix = f"Clip {clip_idx+1}/{total} — " if total > 1 else ""
            return f"{prefix}{clip.get('title', '')[:40]}"

        STEP_NAMES = ["Download", "Trim", "Transcribe", "Title", "Captions", "Preview", "Upload"]

        def _run():
            total = len(selected)
            steps = len(STEP_NAMES)

            self.after(0, self._show_step_bar, STEP_NAMES, 0, total)

            for idx, clip in enumerate(selected):
              if self._cancelled.is_set():
                print("\n  Cancelled by user")
                break
              try:
                self._set_current_step(0)
                self.after(0, self._update_step, 0, idx, total)
                sub = _clip_subtitle(clip, idx, total)

                print(f"\n  == {clip['title']} ==")

                vod_end = clip["vod_offset"] + clip["duration"]
                seg_start = max(0, vod_end - int(clipper.CLIP_LENGTH * 60))
                seg_end = vod_end

                # Download
                download_fn = clipper.download_from_youtube if clipper.PLATFORM == "youtube" else clipper.download_from_tiktok
                video_path = self._download_with_progress(
                    download_fn, seg_start, seg_end, clip, idx, total, steps)

                # Steps: 0=Download 1=Trim 2=Transcribe 3=Title 4=Captions 5=Preview 6=Upload
                approved = False

                # ── Step 1: Trim ──
                while True:
                        self._set_current_step(1)
                        self.after(0, self._update_step, 1)
                        trim_s, trim_e, ext_s, ext_e = self._adjust_segment_dialog(
                            seg_start, seg_end, video_path=video_path)

                        if trim_s == 0 and trim_e == 0 and ext_s == 0 and ext_e == 0:
                            break

                        new_start = max(0, seg_start - ext_s + trim_s)
                        new_end = seg_end + ext_e - trim_e
                        if new_end <= new_start:
                            break

                        needs_redownload = ext_s > 0 or ext_e > 0
                        if needs_redownload:
                            video_path = self._download_with_progress(
                                download_fn, new_start, new_end, clip, idx, total, steps)
                        else:
                            self.after(0, self._show_working, "Trimming Video", sub)
                            video_path = clipper.trim_video(video_path, trim_s, trim_e)
                            self.after(0, self._hide_working)
                        seg_start, seg_end = new_start, new_end

                # ── Step 2: Transcribe ──
                self._set_current_step(2)
                self.after(0, self._update_step, 2)
                self.after(0, self._show_working, "Transcribing Audio", sub)
                result = clipper.transcribe(video_path)
                clipper.save_whisper_result(video_path, result)

                ai_title, ai_caption = None, None
                if clipper.ANTHROPIC_API_KEY:
                    self.after(0, self._update_working, "Generating Title & Caption", sub)
                    transcript_text = clipper.get_transcript_text(result)
                    ai_title, ai_caption = clipper.generate_title_caption(
                        transcript_text, clip["title"])
                self.after(0, self._hide_working)

                # ── Step 3: Title & caption ──
                self._set_current_step(3)
                self.after(0, self._update_step, 3)
                default_title = ai_title or clip["title"]
                default_caption = ai_caption or ""

                title = self._ask_dialog("Title",
                    f"Title for this clip:\n(leave blank to use: {default_title})")
                if not title:
                    title = default_title

                caption_hint = f"leave blank to use: {default_caption}" if default_caption else "leave blank to use title"
                description = self._ask_dialog("Caption",
                    f"Description / caption:\n({caption_hint})")
                if not description:
                    description = default_caption or title

                while not approved:
                    # ── Step 4: Generate subtitles & burn captions ──
                    self._set_current_step(4)
                    self.after(0, self._update_step, 4)
                    self.after(0, self._show_working, "Generating Subtitles", sub)
                    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    ass_path = clipper.RAW_DIR / f"{stamp}.ass"
                    clipper.make_ass(result, ass_path)

                    self.after(0, self._update_working, "Burning Captions", sub,
                               "This may take a minute...")
                    output_name = clipper.safe_filename(title, clip["created_at"])
                    output_path = clipper.unique_output_path(clipper.COMPLETED_DIR, output_name)
                    clipper.burn_captions(video_path, ass_path, title, output_path)

                    self.after(0, self._hide_working)

                    ass_path.unlink(missing_ok=True)
                    for p in clipper.RAW_DIR.glob("*.part"):
                        p.unlink(missing_ok=True)

                    print(f"\n  Ready: {output_path.name}")

                    # ── Step 5: Preview + inline edit ──
                    self._set_current_step(5)
                    self.after(0, self._update_step, 5)

                    choice, title, description, output_path = \
                        self._preview_and_approve(
                            output_path, title, description,
                            video_path, result, clip["created_at"],
                            seg_start=seg_start, seg_end=seg_end,
                            download_fn=download_fn, clip=clip,
                            clip_idx=idx, total=total, steps=steps)

                    if choice == "approved":
                        approved = True
                    elif choice == "draft":
                        # Save as draft — keeps video in My Videos at "Processed" stage
                        self._mark_processed(clip["id"])
                        vod_window = f"{clipper.fmt_time(seg_start)} – {clipper.fmt_time(seg_end)}"
                        draft_kwargs = dict(
                            status="ready_for_review", reviewed=True,
                            date=clip["created_at"][:10],
                            title=title, caption=description,
                            raw_path=str(video_path),
                            clip_id=clip["id"],
                            vod_window=vod_window,
                            stream_title=clip["title"],
                            seg_start=seg_start, seg_end=seg_end,
                            created_at=clip["created_at"])
                        if ai_title:
                            draft_kwargs["ai_title"] = ai_title
                        if ai_caption:
                            draft_kwargs["ai_caption"] = ai_caption
                        self._set_video_status(output_path.name, **draft_kwargs)
                        # Log rejection if user changed AI suggestion
                        if ai_title or ai_caption:
                            clipper.log_rejection_to_voice(ai_title, title, ai_caption, description)
                        print(f"  Saved as draft: {output_path.name}")
                        break  # exit while loop without uploading
                    elif choice == "skip":
                        raise SkipClip()
                    elif choice == "retrim":
                        # Go back to trim, then re-transcribe and re-burn
                        output_path.unlink(missing_ok=True)
                        while True:
                            self._set_current_step(1)
                            self.after(0, self._update_step, 1)
                            trim_s, trim_e, ext_s, ext_e = self._adjust_segment_dialog(
                                seg_start, seg_end, video_path=video_path)

                            if trim_s == 0 and trim_e == 0 and ext_s == 0 and ext_e == 0:
                                break

                            new_start = max(0, seg_start - ext_s + trim_s)
                            new_end = seg_end + ext_e - trim_e
                            if new_end <= new_start:
                                break

                            needs_redownload = ext_s > 0 or ext_e > 0
                            if needs_redownload:
                                video_path = self._download_with_progress(
                                    download_fn, new_start, new_end, clip, idx, total, steps)
                            else:
                                self.after(0, self._show_working, "Trimming Video", sub)
                                video_path = clipper.trim_video(video_path, trim_s, trim_e)
                                self.after(0, self._hide_working)
                            seg_start, seg_end = new_start, new_end

                        # Re-transcribe or trim existing result
                        self._set_current_step(2)
                        self.after(0, self._update_step, 2)
                        if needs_redownload:
                            # Extended — must re-transcribe
                            self.after(0, self._show_working, "Transcribing Audio", sub)
                            result = clipper.transcribe(video_path)
                            clipper.save_whisper_result(video_path, result)
                        else:
                            # Pure trim — adjust timestamps from existing result
                            orig_duration = seg_end - seg_start + trim_s + trim_e
                            result = clipper.trim_whisper_result(result, trim_s, trim_e, orig_duration)
                            clipper.save_whisper_result(video_path, result)

                        if clipper.ANTHROPIC_API_KEY:
                            self.after(0, self._show_working, "Generating Title & Caption", sub)
                            transcript_text = clipper.get_transcript_text(result)
                            ai_title, ai_caption = clipper.generate_title_caption(
                                transcript_text, clip["title"])
                            title = ai_title or title
                            description = ai_caption or description
                        self.after(0, self._hide_working)
                        print("  Re-trimmed — re-burning captions")
                        # Loop continues → re-burns captions and shows preview again

                if choice == "draft":
                    continue  # skip upload, move to next clip

                # Approved — upload flow
                self._mark_processed(clip["id"])
                vod_window = f"{clipper.fmt_time(seg_start)} – {clipper.fmt_time(seg_end)}"
                approved_kwargs = dict(
                    status="approved",
                    date=clip["created_at"][:10],
                    clip_id=clip["id"],
                    vod_window=vod_window,
                    stream_title=clip["title"],
                    seg_start=seg_start, seg_end=seg_end,
                    created_at=clip["created_at"])
                if ai_title:
                    approved_kwargs["ai_title"] = ai_title
                if ai_caption:
                    approved_kwargs["ai_caption"] = ai_caption
                self._set_video_status(output_path.name, **approved_kwargs)
                # Log rejection if user changed AI suggestion
                if ai_title or ai_caption:
                    clipper.log_rejection_to_voice(ai_title, title, ai_caption, description)
                self._set_current_step(6)
                self.after(0, self._update_step, 6)
                self.after(0, self._show_working, "Copying to iCloud", sub)
                clipper.copy_to_icloud(output_path)

                if video_path.exists() and video_path.parent == clipper.RAW_DIR:
                    video_path.unlink(missing_ok=True)

                yt_failed = False
                tt_failed = False

                do_yt = self._confirm_dialog("YouTube", "Upload to YouTube Shorts?")
                if do_yt:
                    self.after(0, self._show_working, "Uploading to YouTube", sub)
                    url = clipper.upload_to_youtube(output_path, title, description)
                    self.after(0, self._hide_working)
                    if url:
                        self._set_video_status(output_path.name, youtube=True)
                        print(f"  YouTube: {url}")
                    else:
                        yt_failed = True
                        self._set_video_status(output_path.name, youtube_failed=True)
                        print(f"  YouTube upload failed. Use iCloud: {output_path.name}")

                if clipper.TIKTOK_CLIENT_KEY:
                    do_tt = self._confirm_dialog("TikTok", "Upload to TikTok?")
                    if do_tt:
                        self.after(0, self._show_working, "Uploading to TikTok", sub)
                        publish_id = clipper.upload_to_tiktok(output_path, description)
                        self.after(0, self._hide_working)
                        if publish_id:
                            self._set_video_status(output_path.name, tiktok=True)
                            print(f"  TikTok publish_id: {publish_id}")
                        else:
                            tt_failed = True
                            self._set_video_status(output_path.name, tiktok_failed=True)
                            print(f"  TikTok upload failed. Use iCloud: {output_path.name}")
                else:
                    do_tt = False

                # Update final status
                vmeta = self._get_video_status(output_path.name)
                if vmeta.get("youtube") or vmeta.get("tiktok"):
                    self._set_video_status(output_path.name, status="posted")
                    clipper.log_to_voice(title, description)
                elif yt_failed or tt_failed:
                    self._set_video_status(output_path.name, status="upload_failed")
                elif not do_yt and not do_tt:
                    self._set_video_status(output_path.name, status="needs_upload")

                self.after(0, self._hide_working)

              except SkipClip:
                # Clean up any burned output file left behind
                try:
                    if output_path and output_path.exists():
                        output_path.unlink(missing_ok=True)
                except (NameError, UnboundLocalError):
                    pass
                print(f"  Skipped: {clip['title']}")
                continue

            self.after(0, self._hide_working)
            self.after(0, self._set_status, "Done!")
            self.after(0, self._hide_step_bar)
            print("\n  All done!")

        def _cleanup():
            self._processing = False
            self._hide_working()
            self._hide_step_bar()
            self.cancel_btn.pack_forget()
            if self._cancelled.is_set():
                self._show_toast("Processing cancelled", "#c0392b", duration=3000)
                self._cancelled.clear()
            # Return to My Videos
            self._hide_overlay()
            self._refresh_output_tab()

        self._run_in_thread(_run, on_done=_cleanup)

    def _auto_process_selected(self):
        """Auto-process selected clips — download, transcribe, AI title, burn.
        No interactive steps. Results queued for review in My Videos."""
        if self._processing:
            return

        selected = [self.clips[i] for i, v in enumerate(self.clip_vars) if v.get()]
        if not selected:
            self._set_status("No clips selected")
            return

        self._processing = True
        self._cancelled.clear()
        try:
            self.process_btn.configure(state="disabled")
            self.auto_process_btn.configure(state="disabled")
            self.fetch_btn.configure(state="disabled")
        except (AttributeError, Exception):
            pass
        self.cancel_btn.pack(side="right", padx=(8, 0))

        def _run():
            total = len(selected)
            if clipper.PLATFORM != "youtube":
                print("  Auto-process requires VOD Platform = youtube")
                return

            try:
                channel_id = clipper.get_youtube_channel_id()
            except SystemExit:
                print("  YouTube channel not found — check YOUTUBE_CHANNEL_HANDLE")
                return

            for idx, clip in enumerate(selected):
                if self._cancelled.is_set():
                    print("\n  Cancelled by user")
                    break

                self.after(0, self._show_working,
                           f"Auto Processing ({idx+1}/{total})",
                           clip.get("title", "")[:40])
                print(f"\n  [{idx+1}/{total}] {clip['title']}")

                try:
                    vod_end = clip["vod_offset"] + clip["duration"]
                    seg_start = max(0, vod_end - int(clipper.CLIP_LENGTH * 60))
                    seg_end = vod_end

                    # 1. Find VOD
                    vods = clipper.find_youtube_vod(channel_id, clip["created_at"])
                    if not vods:
                        print(f"  No YouTube VOD found — skipping")
                        continue

                    clip_dt = datetime.fromisoformat(clip["created_at"].replace("Z", "+00:00"))
                    best = min(vods, key=lambda v: abs(
                        (datetime.fromisoformat(v["snippet"]["publishedAt"].replace("Z", "+00:00")) - clip_dt).total_seconds()))
                    vod_url = f"https://www.youtube.com/watch?v={best['id']['videoId']}"
                    vod_title = best["snippet"]["title"]
                    print(f"  VOD: {vod_title}")

                    # 2. Download
                    self.after(0, self._update_working, f"Downloading ({idx+1}/{total})",
                               clip.get("title", "")[:40])
                    video_path = clipper.download_from_youtube(seg_start, seg_end, clip)

                    # 3. Transcribe
                    self.after(0, self._update_working, f"Transcribing ({idx+1}/{total})",
                               clip.get("title", "")[:40])
                    whisper_result = clipper.transcribe(video_path)
                    clipper.save_whisper_result(video_path, whisper_result)

                    # 4. AI title & caption
                    transcript_text = clipper.get_transcript_text(whisper_result)
                    ai_title, ai_caption = None, None
                    if clipper.ANTHROPIC_API_KEY:
                        ai_title, ai_caption = clipper.generate_title_caption(
                            transcript_text, clip["title"])

                    title = ai_title or clip["title"]
                    caption = ai_caption or title

                    # 5. Burn captions
                    self.after(0, self._update_working, f"Burning Captions ({idx+1}/{total})",
                               title[:40])
                    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    ass_path = clipper.RAW_DIR / f"{stamp}.ass"
                    clipper.make_ass(whisper_result, ass_path)

                    output_name = clipper.safe_filename(title, clip["created_at"])
                    output_path = clipper.unique_output_path(clipper.COMPLETED_DIR, output_name)
                    clipper.burn_captions(video_path, ass_path, title, output_path)
                    ass_path.unlink(missing_ok=True)

                    # 6. Save metadata
                    self._mark_processed(clip["id"])
                    vod_window = f"{clipper.fmt_time(seg_start)} – {clipper.fmt_time(seg_end)}"
                    meta_kwargs = dict(
                        status="ready_for_review",
                        date=clip["created_at"][:10],
                        mode="auto",
                        raw_path=str(video_path),
                        title=title, caption=caption,
                        clip_id=clip["id"],
                        stream_title=vod_title,
                        vod_window=vod_window,
                        seg_start=seg_start, seg_end=seg_end,
                        created_at=clip["created_at"])
                    if ai_title:
                        meta_kwargs["ai_title"] = ai_title
                    if ai_caption:
                        meta_kwargs["ai_caption"] = ai_caption
                    self._set_video_status(output_path.name, **meta_kwargs)

                    print(f"  ✓ Ready for review: {output_path.name}")

                except Exception as e:
                    print(f"  Error: {e}")
                    continue

            self.after(0, self._hide_working)
            print(f"\n  Auto-processed {total} clip(s)")

        def _cleanup():
            self._processing = False
            self._hide_working()
            self.cancel_btn.pack_forget()
            if self._cancelled.is_set():
                self._show_toast("Processing cancelled", "#c0392b", duration=3000)
                self._cancelled.clear()
            self._hide_overlay()
            self._refresh_output_tab()

        self._run_in_thread(_run, on_done=_cleanup)

    # ── Fetch & Process flow (overlay) ─────────────────────────────

    def _open_fetch_flow(self):
        """Open the manual fetch + process flow as an overlay."""
        if self._processing:
            return
        self._show_overlay()
        ov = self.overlay

        # Header
        ctk.CTkLabel(ov, text="Get Clips",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(12, 4))

        # Fetch bar
        fetch_frame = ctk.CTkFrame(ov, fg_color="transparent")
        fetch_frame.pack(fill="x", padx=16, pady=(4, 0))

        def _update_fetch_btn(*_):
            d = self.days_var.get()
            self.fetch_btn.configure(text=f"Fetch Last {d} Days")

        self.fetch_btn = ctk.CTkButton(fetch_frame, text=f"Fetch Last {self.days_var.get()} Days",
                                        width=180, command=self._fetch_clips)
        self.fetch_btn.pack(side="left")
        self.days_var.trace_add("write", _update_fetch_btn)

        ctk.CTkButton(fetch_frame, text="\u25B2", width=28, height=28, fg_color="#444",
                       hover_color="#555", command=lambda: self.days_var.set(min(60, self.days_var.get() + 1)),
                       font=ctk.CTkFont(size=11)).pack(side="left", padx=(8, 2))
        ctk.CTkButton(fetch_frame, text="\u25BC", width=28, height=28, fg_color="#444",
                       hover_color="#555", command=lambda: self.days_var.set(max(1, self.days_var.get() - 1)),
                       font=ctk.CTkFont(size=11)).pack(side="left", padx=0)

        self.status_label = ctk.CTkLabel(fetch_frame, text="", text_color="#888")
        self.status_label.pack(side="left", padx=16)

        # Clip list
        self.clip_frame = ctk.CTkScrollableFrame(ov, height=200)
        self.clip_frame.pack(fill="both", expand=True, padx=16, pady=(8, 0))

        self._no_clips_label = ctk.CTkLabel(
            self.clip_frame, text="Click 'Fetch' to load your recent Twitch clips",
            text_color="#666")
        self._no_clips_label.pack(pady=20)

        # Action buttons
        btn_frame = ctk.CTkFrame(ov, fg_color="transparent")
        btn_frame.pack(fill="x", padx=16, pady=(8, 0))

        self.auto_process_btn = ctk.CTkButton(btn_frame, text="Auto Process", width=130,
                                               fg_color="#2d8a4e", hover_color="#25713f",
                                               command=self._auto_process_selected)

        self.process_btn = ctk.CTkButton(btn_frame, text="Manually Process", width=150,
                                          fg_color="#1D8CD7",
                                          command=self._process_selected)

        self.select_all_clips_btn = ctk.CTkButton(btn_frame, text="Select All", width=90,
                                                    fg_color="#555", command=self._select_all_clips)

        ctk.CTkButton(btn_frame, text="Back", width=80, fg_color="#555",
                       command=self._close_fetch_flow).pack(side="right")

    def _close_fetch_flow(self):
        """Close the fetch overlay and return to My Videos."""
        self._hide_overlay()
        self._refresh_output_tab()

    # ── Output / My Videos ───────────────────────────────────────────

    def _build_output_tab(self):
        """Build the My Videos home screen."""
        tab = self.output_tab

        # Header row
        self._output_hdr = ctk.CTkFrame(tab, fg_color="transparent")
        self._output_hdr.pack(fill="x", pady=(0, 4))

        self.output_summary = ctk.CTkLabel(self._output_hdr, text="",
                                            font=ctk.CTkFont(size=13, weight="bold"))
        self.output_summary.pack(side="left")

        # File list
        self.output_scroll = ctk.CTkScrollableFrame(tab)
        self.output_scroll.pack(fill="both", expand=True)

        self.output_items = []  # list of (BooleanVar, Path) tuples

        self._refresh_output_tab()

    def _refresh_output_tab(self):
        """Reload the output file list, grouped by date with status badges."""
        self._load_worker_log()

        # Detect new videos since last refresh
        current_videos = set(f.name for f in clipper.COMPLETED_DIR.glob("*.mp4"))
        new_videos = current_videos - self._known_videos
        if new_videos:
            count = len(new_videos)
            self._show_toast(
                f"{count} new clip{'s' if count > 1 else ''} ready for review",
                "#2d8a4e", duration=5000)
        self._known_videos = current_videos

        for w in self.output_scroll.winfo_children():
            w.destroy()

        # Rebuild header buttons (adapt to schedule mode)
        for w in self._output_hdr.winfo_children():
            if w != self.output_summary:
                w.destroy()

        is_scheduled = self._schedule_mode in ("auto", "auto-post")

        ctk.CTkButton(self._output_hdr, text="Get Clips", width=100,
                       fg_color="#1D8CD7", height=28,
                       font=ctk.CTkFont(size=12, weight="bold"),
                       command=self._open_fetch_flow).pack(side="right")

        ctk.CTkButton(self._output_hdr, text="Refresh", width=70, fg_color="#555", height=28,
                       command=self._refresh_output_tab).pack(side="right", padx=(0, 6))

        files = sorted(
            [f for f in clipper.COMPLETED_DIR.glob("*.mp4")],
            key=lambda f: f.stat().st_mtime, reverse=True,
        )

        self.output_items = []

        if not files:
            self.output_summary.configure(text="")
            empty = ctk.CTkFrame(self.output_scroll, fg_color="transparent")
            empty.pack(expand=True, pady=40)

            ctk.CTkLabel(empty, text="No videos yet",
                         font=ctk.CTkFont(size=16, weight="bold"),
                         text_color="#666").pack()
            ctk.CTkButton(empty, text="Get Clips", width=140, height=36,
                           fg_color="#1D8CD7",
                           font=ctk.CTkFont(size=14, weight="bold"),
                           command=self._open_fetch_flow).pack(pady=(12, 0))
            if is_scheduled:
                hour_label = f"{clipper.SCHEDULE_HOUR % 12 or 12}:{clipper.SCHEDULE_MINUTE:02d}{'am' if clipper.SCHEDULE_HOUR < 12 else 'pm'}"
                ctk.CTkLabel(empty, text=f"or wait for your next scheduled run at {hour_label}",
                             text_color="#555", font=ctk.CTkFont(size=11)).pack(pady=(6, 0))
            else:
                ctk.CTkLabel(empty, text="Fetch your recent Twitch clips and process them into videos",
                             text_color="#555", font=ctk.CTkFont(size=11),
                             justify="center").pack(pady=(6, 0))
            return

        total_mb = sum(f.stat().st_size for f in files) / 1_000_000
        self.output_summary.configure(text=f"{len(files)} video{'s' if len(files) != 1 else ''}  ·  {total_mb:.1f} MB")

        # Add Select All / Delete Selected only when there are files
        self.select_all_btn = ctk.CTkButton(self._output_hdr, text="Select All", width=80,
                                              fg_color="#555", height=28,
                                              command=self._select_all_outputs)
        self.select_all_btn.pack(side="right", padx=(0, 6))
        self.delete_btn = ctk.CTkButton(self._output_hdr, text="Delete Selected", width=120,
                                         fg_color="#c0392b", height=28,
                                         command=self._delete_selected_outputs)
        self.delete_btn.pack(side="right", padx=(0, 6))

        # Split files into four groups
        active_files = []   # ready_for_review, approved, failed — need attention
        draft_files = []    # "maybe later" — out of the way but not deleted
        scheduled_files = []
        posted_files = []

        incomplete_files = []

        for f in files:
            vmeta = self._get_video_status(f.name)
            status = vmeta.get("status", "")
            if not vmeta or not status:
                incomplete_files.append(f)
            elif status == "posted":
                posted_files.append(f)
            elif status == "scheduled":
                scheduled_files.append(f)
            elif status == "draft":
                draft_files.append(f)
            else:
                active_files.append(f)

        # Sort each group by mtime (newest first)
        active_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        draft_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        scheduled_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        posted_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

        def _render_video_card(parent, f):
            """Render a single video card into the parent frame."""
            var = ctk.BooleanVar(value=False)
            self.output_items.append((var, f))

            vmeta = self._get_video_status(f.name)
            status = vmeta.get("status", "")
            is_posted = status == "posted"
            is_approved = status in ("approved", "needs_upload")
            is_scheduled_v = status == "scheduled"
            is_failed = status == "upload_failed"
            has_yt = vmeta.get("youtube", False)
            has_tt = vmeta.get("tiktok", False)
            yt_failed = vmeta.get("youtube_failed", False)
            tt_failed = vmeta.get("tiktok_failed", False)

            row = ctk.CTkFrame(parent, fg_color="#1a1a1a", corner_radius=6)
            row.pack(fill="x", pady=2)

            ctk.CTkCheckBox(row, text="", variable=var, width=24).pack(
                side="left", padx=(8, 0))

            # Thumbnail
            thumb = self._get_thumbnail(f)
            if thumb:
                thumb_label = ctk.CTkLabel(row, text="", image=thumb, width=45, height=80)
                thumb_label._thumb_ref = thumb  # prevent GC
                thumb_label.pack(side="left", padx=(6, 0))

            info = ctk.CTkFrame(row, fg_color="transparent")
            info.pack(side="left", fill="x", expand=True, padx=(6, 0), pady=4)

            vid_title = vmeta.get("title", f.stem)
            ctk.CTkLabel(info, text=vid_title, anchor="w",
                         font=ctk.CTkFont(size=13)).pack(anchor="w")

            size_mb = f.stat().st_size / 1_000_000
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            meta_parts = [mtime.strftime('%I:%M %p'), f"{size_mb:.1f} MB"]
            if vmeta.get("mode") in ("auto", "auto-post"):
                meta_parts.append("auto")
            stream = vmeta.get("stream_title")
            vod_win = vmeta.get("vod_window")
            if stream:
                meta_parts.append(stream)
            if vod_win:
                meta_parts.append(f"VOD {vod_win}")
            meta_text = "  ·  ".join(meta_parts)
            ctk.CTkLabel(info, text=meta_text, anchor="w",
                         text_color="#666", font=ctk.CTkFont(size=11)).pack(anchor="w")

            # ── Progress dots ──
            prog = ctk.CTkFrame(info, fg_color="transparent")
            prog.pack(anchor="w", pady=(2, 0))

            def _dot(p, filled, color="#4CAF50", fail=False):
                c = color if filled else "#444"
                if fail:
                    c = "#c0392b"
                sym = "●" if filled else "○"
                if fail:
                    sym = "✕"
                return ctk.CTkLabel(p, text=sym, text_color=c,
                                    font=ctk.CTkFont(size=11), width=12)

            def _line(p):
                return ctk.CTkLabel(p, text="—", text_color="#444",
                                    font=ctk.CTkFont(size=9), width=14)

            _dot(prog, True).pack(side="left")
            ctk.CTkLabel(prog, text="Processed", text_color="#4CAF50",
                         font=ctk.CTkFont(size=10)).pack(side="left", padx=(1, 0))

            is_seen = vmeta.get("reviewed", False)
            approved_done = is_approved or is_scheduled_v or is_posted or is_failed

            # ● Seen
            _line(prog).pack(side="left")
            seen_done = is_seen or approved_done
            # In auto-post mode, posted without being seen = red warning
            unseen_posted = is_posted and not is_seen
            if unseen_posted:
                seen_color = "#c0392b"
                seen_text = "Unseen"
                _dot(prog, True, fail=True).pack(side="left")
            else:
                seen_color = "#4CAF50" if seen_done else "#555"
                seen_text = "Seen"
                _dot(prog, seen_done, color=seen_color).pack(side="left")
            ctk.CTkLabel(prog, text=seen_text, text_color=seen_color,
                         font=ctk.CTkFont(size=10)).pack(side="left", padx=(1, 0))

            # ● Approved
            _line(prog).pack(side="left")
            approved_color = "#4CAF50" if approved_done else "#555"
            _dot(prog, approved_done, color=approved_color).pack(side="left")
            ctk.CTkLabel(prog, text="Approved", text_color=approved_color,
                         font=ctk.CTkFont(size=10)).pack(side="left", padx=(1, 0))

            # ● Posted
            _line(prog).pack(side="left")
            if is_posted and has_yt and has_tt:
                _dot(prog, True).pack(side="left")
                ctk.CTkLabel(prog, text="Posted (YT + TT)", text_color="#4CAF50",
                             font=ctk.CTkFont(size=10)).pack(side="left", padx=(1, 0))
            elif is_posted:
                parts = []
                if has_yt: parts.append("YT")
                if has_tt: parts.append("TT")
                _dot(prog, True).pack(side="left")
                ctk.CTkLabel(prog, text=f"Posted ({' + '.join(parts)})", text_color="#4CAF50",
                             font=ctk.CTkFont(size=10)).pack(side="left", padx=(1, 0))
            elif is_scheduled_v:
                sched_time_str = vmeta.get("scheduled_time", "")
                try:
                    sched_dt = datetime.fromisoformat(sched_time_str)
                    sched_label = sched_dt.strftime("%b %d, %I:%M %p")
                except (ValueError, TypeError):
                    sched_label = "pending"
                ctk.CTkLabel(prog, text="◷", text_color="#1D8CD7",
                             font=ctk.CTkFont(size=11), width=12).pack(side="left")
                ctk.CTkLabel(prog, text=f"Scheduled ({sched_label})", text_color="#1D8CD7",
                             font=ctk.CTkFont(size=10)).pack(side="left", padx=(1, 0))
            elif yt_failed or tt_failed:
                _dot(prog, False, fail=True).pack(side="left")
                fail_parts = []
                if yt_failed: fail_parts.append("YT")
                if tt_failed: fail_parts.append("TT")
                ok_parts = []
                if has_yt: ok_parts.append("YT ✓")
                if has_tt: ok_parts.append("TT ✓")
                fail_text = "  ".join(ok_parts + [p + " ✕" for p in fail_parts])
                ctk.CTkLabel(prog, text=f"Post failed ({fail_text})", text_color="#c0392b",
                             font=ctk.CTkFont(size=10)).pack(side="left", padx=(1, 0))
            else:
                _dot(prog, False).pack(side="left")
                ctk.CTkLabel(prog, text="Posted", text_color="#555",
                             font=ctk.CTkFont(size=10)).pack(side="left", padx=(1, 0))

            # Action buttons
            if is_scheduled_v:
                def _unschedule(fp=f):
                    self._set_video_status(fp.name, status="approved")
                    self._refresh_output_tab()
                ctk.CTkButton(row, text="Unschedule", width=85, height=30,
                              fg_color="#c0392b", hover_color="#e74c3c",
                              font=ctk.CTkFont(size=11),
                              command=_unschedule).pack(side="right", padx=(0, 8), pady=4)
                ctk.CTkButton(row, text="Edit", width=55, height=30,
                              fg_color="#555", hover_color="#666",
                              font=ctk.CTkFont(size=12),
                              command=lambda path=f: self._review_video(path)).pack(
                    side="right", padx=(0, 4), pady=4)
            elif not is_posted:
                if approved_done:
                    btn_color = "#555"
                    btn_text = "Edit"
                elif is_seen:
                    btn_color = "#1D8CD7"
                    btn_text = "Continue"
                else:
                    btn_color = "#e67e22"
                    btn_text = "Review"
                ctk.CTkButton(row, text=btn_text, width=65, height=30,
                              fg_color=btn_color,
                              hover_color="#d35400" if not approved_done else "#666",
                              font=ctk.CTkFont(size=12),
                              command=lambda path=f: self._review_video(path)).pack(
                    side="right", padx=(0, 8), pady=4)

            # "Retry" button for failed uploads
            if is_failed:
                ctk.CTkButton(row, text="Retry", width=65, height=30,
                              fg_color="#e67e22", hover_color="#d35400",
                              font=ctk.CTkFont(size=12),
                              command=lambda path=f: self._review_video(path)).pack(
                    side="right", padx=(0, 4), pady=4)

            # "Mark Posted" for videos missing a platform or with failures
            missing_yt = not has_yt
            missing_tt = not has_tt
            show_mark = (is_posted and (missing_yt or missing_tt)) or is_failed or (is_approved and not is_scheduled_v)
            if show_mark:
                def _mark_posted(fp=f, m_yt=missing_yt, m_tt=missing_tt):
                    self._mark_posted_dialog(fp, m_yt, m_tt)
                ctk.CTkButton(row, text="Mark Posted", width=90, height=28,
                              fg_color="#333", hover_color="#444",
                              text_color="#aaa", font=ctk.CTkFont(size=11),
                              command=_mark_posted).pack(
                    side="right", padx=(0, 4), pady=4)

            ctk.CTkButton(row, text="\u25B6", width=32, height=32,
                          fg_color="#333", hover_color="#444",
                          font=ctk.CTkFont(size=14),
                          command=lambda path=f: subprocess.run(
                              ["open", str(path)])).pack(
                side="right", padx=(8, 8), pady=4)

            # "Draft" / "Undraft" toggle — shelve videos for later
            is_draft = status == "draft"
            if not is_posted and not is_scheduled_v:
                if is_draft:
                    def _undraft(fp=f):
                        self._set_video_status(fp.name, status="ready_for_review")
                        self._refresh_output_tab()
                    ctk.CTkButton(row, text="Undraft", width=70, height=28,
                                  fg_color="#555", hover_color="#666",
                                  text_color="#ccc", font=ctk.CTkFont(size=11),
                                  command=_undraft).pack(
                        side="right", padx=(0, 4), pady=4)
                else:
                    def _draft(fp=f):
                        self._set_video_status(fp.name, status="draft")
                        self._refresh_output_tab()
                    ctk.CTkButton(row, text="Draft", width=55, height=28,
                                  fg_color="#333", hover_color="#444",
                                  text_color="#888", font=ctk.CTkFont(size=11),
                                  command=_draft).pack(
                        side="right", padx=(0, 4), pady=4)

        # ── Incomplete videos (no meta — processing was interrupted) ──
        if incomplete_files:
            inc_container = ctk.CTkFrame(self.output_scroll, fg_color="transparent")
            inc_container.pack(fill="x", pady=(0, 4))

            ctk.CTkLabel(inc_container,
                         text=f"Incomplete ({len(incomplete_files)})",
                         text_color="#e67e22", anchor="w",
                         font=ctk.CTkFont(size=13, weight="bold")).pack(fill="x", padx=4)

            for f in incomplete_files:
                row = ctk.CTkFrame(inc_container, fg_color="#1a1a1a", corner_radius=6)
                row.pack(fill="x", pady=2)

                var = ctk.BooleanVar(value=False)
                self.output_items.append((var, f))

                ctk.CTkCheckBox(row, text="", variable=var, width=24).pack(
                    side="left", padx=(8, 0))

                info = ctk.CTkFrame(row, fg_color="transparent")
                info.pack(side="left", fill="x", expand=True, padx=(6, 0), pady=4)

                ctk.CTkLabel(info, text=f.stem, anchor="w",
                             font=ctk.CTkFont(size=13)).pack(anchor="w")

                size_mb = f.stat().st_size / 1_000_000
                ctk.CTkLabel(info, text=f"{size_mb:.1f} MB  ·  Processing was interrupted",
                             anchor="w", text_color="#e67e22",
                             font=ctk.CTkFont(size=11)).pack(anchor="w")

        # ── Active videos (ready_for_review, approved, failed) ──
        if active_files:
            for f in active_files:
                _render_video_card(self.output_scroll, f)

        # ── Drafts section (collapsed by default) ──
        if draft_files:
            draft_container = ctk.CTkFrame(self.output_scroll, fg_color="transparent")
            draft_container.pack(fill="x", pady=(8, 0))

            draft_inner = ctk.CTkFrame(self.output_scroll, fg_color="transparent")

            def _toggle_drafts():
                if draft_inner.winfo_ismapped():
                    draft_inner.pack_forget()
                    draft_toggle.configure(text=f"▶  Drafts ({len(draft_files)})")
                else:
                    draft_inner.pack(fill="x", after=draft_container)
                    draft_toggle.configure(text=f"▼  Drafts ({len(draft_files)})")

            draft_toggle = ctk.CTkButton(
                draft_container,
                text=f"▶  Drafts ({len(draft_files)})",
                fg_color="transparent", hover_color="#1a1a1a",
                text_color="#888", anchor="w",
                font=ctk.CTkFont(size=13, weight="bold"),
                command=_toggle_drafts)
            draft_toggle.pack(fill="x")
            # Collapsed by default — cards rendered but not packed
            for f in draft_files:
                _render_video_card(draft_inner, f)

        # ── Scheduled section (expanded by default) ──
        if scheduled_files:
            sched_container = ctk.CTkFrame(self.output_scroll, fg_color="transparent")
            sched_container.pack(fill="x", pady=(8, 0))

            sched_inner = ctk.CTkFrame(self.output_scroll, fg_color="transparent")

            def _toggle_scheduled():
                if sched_inner.winfo_ismapped():
                    sched_inner.pack_forget()
                    sched_toggle.configure(text=f"▶  Scheduled ({len(scheduled_files)})")
                else:
                    sched_inner.pack(fill="x", after=sched_container)
                    sched_toggle.configure(text=f"▼  Scheduled ({len(scheduled_files)})")

            sched_toggle = ctk.CTkButton(
                sched_container,
                text=f"▼  Scheduled ({len(scheduled_files)})",
                fg_color="transparent", hover_color="#1a1a1a",
                text_color="#1D8CD7", anchor="w",
                font=ctk.CTkFont(size=13, weight="bold"),
                command=_toggle_scheduled)
            sched_toggle.pack(fill="x")

            # Expanded by default
            sched_inner.pack(fill="x", after=sched_container)
            for f in scheduled_files:
                _render_video_card(sched_inner, f)

        # ── Posted section (collapsed by default) ──
        if posted_files:
            posted_container = ctk.CTkFrame(self.output_scroll, fg_color="transparent")
            posted_container.pack(fill="x", pady=(8, 0))

            posted_inner = ctk.CTkFrame(self.output_scroll, fg_color="transparent")

            def _toggle_posted():
                if posted_inner.winfo_ismapped():
                    posted_inner.pack_forget()
                    posted_toggle.configure(text=f"▶  Posted ({len(posted_files)})")
                else:
                    posted_inner.pack(fill="x", after=posted_container)
                    posted_toggle.configure(text=f"▼  Posted ({len(posted_files)})")

            posted_toggle = ctk.CTkButton(
                posted_container,
                text=f"▶  Posted ({len(posted_files)})",
                fg_color="transparent", hover_color="#1a1a1a",
                text_color="#4CAF50", anchor="w",
                font=ctk.CTkFont(size=13, weight="bold"),
                command=_toggle_posted)
            posted_toggle.pack(fill="x")
            # Render cards into posted_inner (collapsed by default)
            for f in posted_files:
                _render_video_card(posted_inner, f)

    def _select_all_outputs(self):
        for var, f in self.output_items:
            vmeta = self._get_video_status(f.name)
            if vmeta.get("status") != "posted":
                var.set(True)

    def _review_video(self, video_path):
        """Review a video using the same preview+edit screen as processing,
        then offer upload options on approval."""
        if self._processing:
            return
        self._processing = True
        self._in_review = True
        self._cancelled.clear()

        vmeta = self._get_video_status(video_path.name)
        self._set_video_status(video_path.name, reviewed=True)
        title = vmeta.get("title", video_path.stem)
        description = vmeta.get("caption", title)
        raw_path_str = vmeta.get("raw_path", "")
        raw_path = Path(raw_path_str) if raw_path_str else video_path
        clip_date = vmeta.get("date", datetime.now().strftime("%Y-%m-%d"))
        was_scheduled = vmeta.get("status") == "scheduled"

        def _run():
            current_output = video_path
            cur_title = title
            cur_desc = description
            cur_raw = raw_path

            # Determine the source file for re-burns (must be raw, not already-burned)
            source = cur_raw if cur_raw.exists() else current_output

            # Load saved Whisper result, or re-transcribe if not found
            whisper_result = clipper.load_whisper_result(source)
            if whisper_result is None:
                self.after(0, self._show_working, "Transcribing", cur_title[:40])
                whisper_result = clipper.transcribe(source)
                clipper.save_whisper_result(source, whisper_result)
                self.after(0, self._hide_working)

            close_label = "Unschedule" if was_scheduled else "Close"

            while True:
                choice, new_title, new_desc, new_output = \
                    self._preview_and_approve(
                        current_output, cur_title, cur_desc,
                        source, whisper_result, clip_date,
                        skip_label=close_label)

                current_output = new_output
                title_final = new_title
                desc_final = new_desc

                if choice == "draft":
                    # Save for Later — save edits, keep at processed stage
                    self._set_video_status(current_output.name,
                                           status="ready_for_review", reviewed=True,
                                           title=title_final, caption=desc_final)
                    break

                if choice == "skip":
                    if was_scheduled:
                        # Unschedule — move back to approved
                        self._set_video_status(current_output.name,
                                               status="approved",
                                               title=title_final, caption=desc_final)
                        print(f"  Unscheduled: {current_output.name}")
                    # Close — go back to My Videos
                    break

                if choice == "retrim":
                    if not cur_raw.exists():
                        print("  Raw file not available for re-trimming")
                        continue

                    # Let audio device fully release before opening new player
                    import gc; gc.collect()
                    import time; time.sleep(0.3)

                    # Get duration via ffprobe
                    probe = subprocess.run(
                        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                         "-of", "default=noprint_wrappers=1:nokey=1", str(cur_raw)],
                        capture_output=True, text=True)
                    try:
                        duration = int(float(probe.stdout.strip()))
                    except (ValueError, AttributeError):
                        duration = 120

                    trim_s, trim_e, ext_s, ext_e = self._adjust_segment_dialog(
                        0, duration, video_path=cur_raw, allow_skip=False)
                    print(f"  Trim values: trim_s={trim_s}, trim_e={trim_e}, ext_s={ext_s}, ext_e={ext_e}")

                    needs_redownload = ext_s > 0 or ext_e > 0
                    if needs_redownload:
                        # Re-download with extended range from YouTube VOD
                        saved_seg_start = vmeta.get("seg_start")
                        saved_seg_end = vmeta.get("seg_end")
                        saved_created_at = vmeta.get("created_at")
                        if not saved_created_at and vmeta.get("date"):
                            saved_created_at = vmeta["date"] + "T12:00:00Z"

                        # Fallback: parse vod_window "25:18 – 28:18" if seg fields missing
                        if saved_seg_start is None and vmeta.get("vod_window"):
                            try:
                                parts = vmeta["vod_window"].split(" – ")
                                def _parse_ts(ts):
                                    segs = ts.strip().split(":")
                                    segs = [int(x) for x in segs]
                                    if len(segs) == 3: return segs[0]*3600 + segs[1]*60 + segs[2]
                                    return segs[0]*60 + segs[1]
                                saved_seg_start = _parse_ts(parts[0])
                                saved_seg_end = _parse_ts(parts[1])
                            except Exception:
                                pass

                        if saved_seg_start is not None and saved_created_at:
                            new_start = max(0, int(saved_seg_start) - ext_s + trim_s)
                            new_end = int(saved_seg_end) + ext_e - trim_e
                            print(f"  VOD range: {clipper.fmt_time(new_start)} → {clipper.fmt_time(new_end)} ({new_end - new_start}s)")
                            fake_clip = {"created_at": saved_created_at}

                            self.after(0, self._show_working, "Re-downloading Extended Segment", title_final[:40])
                            try:
                                new_raw = clipper.download_from_youtube(new_start, new_end, fake_clip)
                                cur_raw = new_raw
                                # Update seg range in metadata
                                self._set_video_status(current_output.name,
                                                       seg_start=new_start, seg_end=new_end)
                            except Exception as e:
                                print(f"  Re-download failed: {e}")
                                self.after(0, self._hide_working)
                                continue
                            self.after(0, self._hide_working)

                            # Must re-transcribe after extending
                            self.after(0, self._show_working, "Transcribing", title_final[:40])
                            whisper_result = clipper.transcribe(cur_raw)
                            clipper.save_whisper_result(cur_raw, whisper_result)
                            self.after(0, self._hide_working)
                        else:
                            print("  Cannot extend — no VOD segment info saved for this video")
                            continue
                    elif trim_s > 0 or trim_e > 0:
                        self.after(0, self._show_working, "Trimming Video", title_final[:40])
                        trimmed = clipper.trim_video(cur_raw, trim_s, trim_e)
                        self.after(0, self._hide_working)
                        cur_raw = trimmed

                        # Adjust whisper timestamps instead of re-transcribing
                        whisper_result = clipper.trim_whisper_result(
                            whisper_result, trim_s, trim_e, duration)
                        clipper.save_whisper_result(cur_raw, whisper_result)

                    # Re-burn
                    self.after(0, self._show_working, "Burning Captions", title_final[:40])
                    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    ass_path = clipper.RAW_DIR / f"{stamp}.ass"
                    clipper.make_ass(whisper_result, ass_path)

                    new_name = clipper.safe_filename(title_final, clip_date)
                    new_path = clipper.unique_output_path(clipper.COMPLETED_DIR, new_name)
                    clipper.burn_captions(cur_raw, ass_path, title_final, new_path)
                    ass_path.unlink(missing_ok=True)

                    if current_output != new_path and current_output.exists():
                        old_name = current_output.name
                        current_output.unlink(missing_ok=True)
                        meta = self._load_video_meta()
                        old_meta = meta.pop(old_name, {})
                        old_meta.update(title=title_final, caption=desc_final,
                                        raw_path=str(cur_raw))
                        meta[new_path.name] = old_meta
                        self._save_video_meta(meta)

                    current_output = new_path
                    cur_title = title_final
                    cur_desc = desc_final
                    source = cur_raw  # update source for re-burns
                    self.after(0, self._hide_working)
                    continue  # show preview again

                if choice == "approved":
                    # Mark approved, save edits
                    self._set_video_status(current_output.name,
                                           status="approved",
                                           title=title_final, caption=desc_final)

                    # Ask about upload — three options
                    upload_choice = self._upload_choice_dialog()

                    if upload_choice == "now":
                        # Upload immediately — ask per platform
                        do_yt = False
                        do_tt = False
                        yt_failed = False
                        tt_failed = False

                        if clipper.YOUTUBE_CLIENT_ID:
                            do_yt = self._confirm_dialog("YouTube", "Upload to YouTube Shorts?")
                        if clipper.TIKTOK_CLIENT_KEY:
                            do_tt = self._confirm_dialog("TikTok", "Upload to TikTok?")

                        if not do_yt and not do_tt:
                            # User declined both — save as needs_upload
                            self._set_video_status(current_output.name, status="needs_upload")
                            break

                        self.after(0, self._show_working, "Copying to iCloud", title_final[:40])
                        clipper.copy_to_icloud(current_output)
                        self.after(0, self._hide_working)

                        if do_yt:
                            self.after(0, self._show_working, "Uploading to YouTube", title_final[:40])
                            url = clipper.upload_to_youtube(current_output, title_final, desc_final)
                            self.after(0, self._hide_working)
                            if url:
                                self._set_video_status(current_output.name, youtube=True)
                                print(f"  YouTube: {url}")
                            else:
                                yt_failed = True
                                self._set_video_status(current_output.name, youtube_failed=True)
                                print(f"  YouTube upload failed")

                        if do_tt:
                            self.after(0, self._show_working, "Uploading to TikTok", title_final[:40])
                            publish_id = clipper.upload_to_tiktok(current_output, desc_final)
                            self.after(0, self._hide_working)
                            if publish_id:
                                self._set_video_status(current_output.name, tiktok=True)
                                print(f"  TikTok: {publish_id}")
                            else:
                                tt_failed = True
                                self._set_video_status(current_output.name, tiktok_failed=True)
                                print(f"  TikTok upload failed")

                        self.after(0, self._hide_working)

                        # Clean up raw file and whisper cache
                        if raw_path_str and Path(raw_path_str).exists():
                            Path(raw_path_str).unlink(missing_ok=True)
                            Path(raw_path_str).with_suffix(".whisper.json").unlink(missing_ok=True)

                        # Final status
                        vm = self._get_video_status(current_output.name)
                        if vm.get("youtube") or vm.get("tiktok"):
                            self._set_video_status(current_output.name, status="posted")
                            clipper.log_to_voice(title_final, desc_final)
                            # Log rejection if user changed AI suggestion during review
                            if vm.get("ai_title") or vm.get("ai_caption"):
                                clipper.log_rejection_to_voice(
                                    vm.get("ai_title"), title_final,
                                    vm.get("ai_caption"), desc_final)
                        elif yt_failed or tt_failed:
                            self._set_video_status(current_output.name, status="upload_failed")

                    elif upload_choice == "schedule":
                        # Show schedule picker
                        sched_time = self._schedule_post_dialog()
                        if sched_time:
                            self._set_video_status(current_output.name,
                                                   status="scheduled",
                                                   scheduled_time=sched_time.isoformat())
                            # Ensure the poster job is running
                            if not clipper.is_poster_scheduled():
                                clipper.schedule_poster()
                            print(f"  Scheduled for {sched_time.strftime('%b %d at %I:%M %p')}")

                    # "not_now" — do nothing, stays approved
                    break

        def _cleanup():
            self._processing = False
            self._in_review = False
            self._hide_working()
            self._refresh_output_tab()

        self._run_in_thread(_run, on_done=_cleanup)

    def _delete_selected_outputs(self):
        to_del = [f for var, f in self.output_items if var.get()]
        if not to_del:
            return

        # Run in thread to avoid blocking the main loop during confirmation
        def _do_delete():
            names = ", ".join(f.name for f in to_del[:3])
            if len(to_del) > 3:
                names += f" + {len(to_del) - 3} more"

            result = [False]
            event = threading.Event()

            def _show():
                self._show_overlay()
                ov = self.overlay
                ctk.CTkLabel(ov, text=f"Delete {len(to_del)} file(s)?",
                             font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(60, 4))
                ctk.CTkLabel(ov, text=names, text_color="#aaa", wraplength=400).pack(pady=(0, 20))
                btn_row = ctk.CTkFrame(ov, fg_color="transparent")
                btn_row.pack()

                def _yes():
                    result[0] = True
                    self._hide_overlay()
                    event.set()
                def _no():
                    self._hide_overlay()
                    event.set()

                ctk.CTkButton(btn_row, text="Delete", width=100, fg_color="#c0392b", command=_yes).pack(side="left", padx=8)
                ctk.CTkButton(btn_row, text="Cancel", width=100, fg_color="#555", command=_no).pack(side="left", padx=8)

            self.after(0, _show)
            event.wait()

            if not result[0]:
                return

            meta = self._load_video_meta()
            for f in to_del:
                vmeta = meta.get(f.name, {})
                # Clean up raw file
                raw_path_str = vmeta.get("raw_path", "")
                if raw_path_str:
                    raw = Path(raw_path_str)
                    if raw.exists():
                        raw.unlink(missing_ok=True)
                    raw.with_suffix(".whisper.json").unlink(missing_ok=True)
                # Remove from processed IDs if not posted (so it can be reprocessed)
                clip_id = vmeta.get("clip_id", "")
                if clip_id and vmeta.get("status") != "posted":
                    self._processed_ids.discard(clip_id)
                f.unlink()
                f.with_suffix(".thumb.jpg").unlink(missing_ok=True)
                meta.pop(f.name, None)
                print(f"  Deleted: {f.name}")
            self._save_video_meta(meta)
            # Save updated processed IDs
            processed_path = Path(__file__).parent / ".processed_clips.json"
            processed_path.write_text(json.dumps(list(self._processed_ids)))
            print(f"  Removed {len(to_del)} file(s)")
            self.after(0, self._refresh_output_tab)

        threading.Thread(target=_do_delete, daemon=True).start()

    # ── Toast / banner system ─────────────────────────────────────────

    def _show_toast(self, message, color="#c0392b", duration=8000):
        """Show a toast banner below the progress bar. Auto-dismisses after duration ms."""
        self.toast_label.configure(text=message)
        self.toast_frame.configure(fg_color=color)
        self.toast_frame.pack(fill="x", padx=16, pady=(6, 0), before=self.log_toggle)

        # Cancel any existing timer
        if self._toast_timer:
            self.after_cancel(self._toast_timer)
        self._toast_timer = self.after(duration, self._dismiss_toast)

    def _dismiss_toast(self):
        """Hide the toast banner."""
        self.toast_frame.pack_forget()
        if self._toast_timer:
            self.after_cancel(self._toast_timer)
            self._toast_timer = None

    # ── Credential validation ─────────────────────────────────────────

    def _on_main_mode_change(self, new_mode):
        """Handle schedule mode change from the main page selector."""
        # Block auto/auto-post if platform is not YouTube
        if new_mode in ("auto", "auto-post") and clipper.PLATFORM != "youtube":
            self._main_mode_seg.set(self._schedule_mode)  # revert selector
            self._show_toast(
                "Auto mode requires VOD Platform = youtube. Change it in Settings first.",
                color="#c0392b", duration=6000)
            return

        self._schedule_mode = new_mode
        clipper.CLIP_MODE = new_mode

        # Update .env
        env_path = Path(__file__).parent / ".env"
        if env_path.exists():
            lines = env_path.read_text().splitlines()
            new_lines = [l for l in lines if not l.startswith("CLIP_MODE=")]
            new_lines.append(f"CLIP_MODE={new_mode}")
            env_path.write_text("\n".join(new_lines) + "\n")

        # Show/hide time picker
        if new_mode in ("auto", "auto-post"):
            self._time_frame.pack(side="left", padx=(6, 0), before=self.settings_btn)
        else:
            self._time_frame.pack_forget()

        # Schedule/unschedule based on mode
        if new_mode in ("auto", "auto-post"):
            clipper.schedule()
        elif new_mode == "off" and clipper.is_scheduled():
            clipper.unschedule()

        self._update_schedule_banner()
        self._refresh_output_tab()

    def _on_main_time_change(self, _=None):
        """Handle time change from the main page time picker."""
        hour_opts = [f"{h % 12 or 12}{'am' if h < 12 else 'pm'}" for h in range(24)]
        hour_label = self._main_hour_var.get()
        hour_idx = hour_opts.index(hour_label) if hour_label in hour_opts else 10
        min_val = int(self._main_min_var.get())

        clipper.SCHEDULE_HOUR = hour_idx
        clipper.SCHEDULE_MINUTE = min_val

        # Update .env
        env_path = Path(__file__).parent / ".env"
        if env_path.exists():
            lines = env_path.read_text().splitlines()
            new_lines = [l for l in lines
                         if not l.startswith("SCHEDULE_HOUR=")
                         and not l.startswith("SCHEDULE_MINUTE=")]
            new_lines.append(f"SCHEDULE_HOUR={hour_idx}")
            new_lines.append(f"SCHEDULE_MINUTE={min_val}")
            env_path.write_text("\n".join(new_lines) + "\n")

        # Re-schedule with new time
        if self._schedule_mode in ("auto", "auto-post") and clipper.PLATFORM == "youtube":
            clipper.schedule()

        # Refresh to update the displayed time
        self._refresh_output_tab()

    def _update_schedule_banner(self):
        """Show schedule banner. Sync main page selector."""
        self.mode_banner.pack_forget()
        if self._schedule_mode == "auto-post":
            self.mode_banner.configure(fg_color="#c0392b")
            self.mode_banner_label.configure(
                text="AUTO-POST — clips processed and uploaded automatically, no review",
                font=ctk.CTkFont(size=12, weight="bold"), text_color="white")
            self.mode_banner.pack(fill="x", padx=16, pady=(8, 0), before=self.cred_strip)
        elif self._schedule_mode == "auto":
            self.mode_banner.configure(fg_color="#1a2e1a")
            self.mode_banner_label.configure(
                text="Clips will be processed on schedule — review and upload from My Videos",
                font=ctk.CTkFont(size=12), text_color="#7dba6d")
            self.mode_banner.pack(fill="x", padx=16, pady=(8, 0), before=self.cred_strip)
        if hasattr(self, '_main_mode_seg'):
            self._main_mode_seg.set(self._schedule_mode)

    def _check_credentials(self):
        """Check which services have credentials configured and update the status strip."""
        # Twitch (required)
        has_twitch = bool(clipper.TWITCH_CLIENT_ID and clipper.TWITCH_CLIENT_SECRET
                          and clipper.TWITCH_USERNAME)
        self.cred_twitch.configure(
            text=f"{'●' if has_twitch else '○'}  Twitch",
            text_color="#4CAF50" if has_twitch else "#c0392b")

        # YouTube
        has_yt_vod = bool(clipper.YOUTUBE_API_KEY and clipper.YOUTUBE_CHANNEL_HANDLE)
        has_yt_upload = bool(clipper.YOUTUBE_CLIENT_ID and clipper.YOUTUBE_CLIENT_SECRET)
        if has_yt_vod and has_yt_upload:
            yt_text, yt_color = "●  YouTube", "#4CAF50"
        elif has_yt_vod:
            yt_text, yt_color = "◐  YouTube (no upload)", "#f39c12"
        else:
            yt_text, yt_color = "○  YouTube", "#555"
        self.cred_youtube.configure(text=yt_text, text_color=yt_color)

        # TikTok
        has_tiktok = bool(clipper.TIKTOK_CLIENT_KEY and clipper.TIKTOK_CLIENT_SECRET)
        if has_tiktok:
            tt_text, tt_color = "●  TikTok", "#4CAF50"
        else:
            tt_text, tt_color = "○  TikTok", "#555"
        self.cred_tiktok.configure(text=tt_text, text_color=tt_color)

        # Show warning toast if Twitch is missing
        if not has_twitch:
            self._show_toast("Twitch credentials missing — open Settings to configure", "#c0392b")

    # ── Help / README viewer ─────────────────────────────────────────

    def _open_help(self):
        self._show_overlay()
        ov = self.overlay

        scroll = ctk.CTkScrollableFrame(ov)
        scroll.pack(fill="both", expand=True, padx=12, pady=12)

        # Read README.md
        readme_path = Path(__file__).parent / "README.md"
        if not readme_path.exists():
            ctk.CTkLabel(scroll, text="README.md not found",
                         text_color="#888").pack(pady=40)
            ctk.CTkButton(ov, text="Close", width=100, fg_color="#555",
                          command=self._hide_overlay).pack(pady=6)
            return

        raw = readme_path.read_text()

        # Simple markdown renderer — handles headers, bold, code blocks,
        # bullet lists, tables, and plain text.
        in_code_block = False
        code_buf = []
        in_table = False
        table_rows = []

        def _flush_table():
            """Render accumulated table rows as a formatted block."""
            nonlocal table_rows, in_table
            if not table_rows:
                return
            # Parse columns from header row
            header = [c.strip() for c in table_rows[0].strip("|").split("|")]
            data = []
            for row_line in table_rows[2:]:  # skip header + separator
                cols = [c.strip() for c in row_line.strip("|").split("|")]
                data.append(cols)

            # Render as a dark block with aligned text
            tbl_frame = ctk.CTkFrame(scroll, fg_color="#111", corner_radius=6)
            tbl_frame.pack(fill="x", padx=4, pady=(2, 4))

            # Header
            hdr_frame = ctk.CTkFrame(tbl_frame, fg_color="transparent")
            hdr_frame.pack(fill="x", padx=8, pady=(6, 2))
            for i, h in enumerate(header):
                ctk.CTkLabel(hdr_frame, text=h.replace("**", ""),
                             font=ctk.CTkFont(size=12, weight="bold"),
                             text_color="#ccc", anchor="w").pack(side="left", padx=(0, 24))

            # Rows
            for cols in data:
                row_f = ctk.CTkFrame(tbl_frame, fg_color="transparent")
                row_f.pack(fill="x", padx=8, pady=1)
                for i, cell in enumerate(cols):
                    # Strip markdown bold
                    cell_text = cell.replace("**", "")
                    ctk.CTkLabel(row_f, text=cell_text,
                                 font=ctk.CTkFont(size=11),
                                 text_color="#aaa", anchor="w",
                                 wraplength=350).pack(side="left", padx=(0, 24))

            ctk.CTkFrame(tbl_frame, height=4, fg_color="transparent").pack()
            table_rows = []
            in_table = False

        for line in raw.split("\n"):
            # Code blocks
            if line.strip().startswith("```"):
                if in_code_block:
                    # End code block
                    code_text = "\n".join(code_buf)
                    code_frame = ctk.CTkFrame(scroll, fg_color="#111", corner_radius=6)
                    code_frame.pack(fill="x", padx=4, pady=(2, 4))
                    ctk.CTkLabel(code_frame, text=code_text,
                                 font=ctk.CTkFont(family="Menlo", size=11),
                                 text_color="#8cc265", anchor="w", justify="left",
                                 wraplength=700).pack(padx=10, pady=6, anchor="w")
                    code_buf = []
                    in_code_block = False
                else:
                    if in_table:
                        _flush_table()
                    in_code_block = True
                continue

            if in_code_block:
                code_buf.append(line)
                continue

            # Tables
            if "|" in line and line.strip().startswith("|"):
                if not in_table:
                    in_table = True
                    table_rows = []
                table_rows.append(line)
                continue
            elif in_table:
                _flush_table()

            stripped = line.strip()

            # Skip empty lines
            if not stripped:
                continue

            # Headers
            if stripped.startswith("# "):
                ctk.CTkLabel(scroll, text=stripped[2:],
                             font=ctk.CTkFont(size=20, weight="bold")).pack(
                    anchor="w", padx=4, pady=(12, 2))
            elif stripped.startswith("## "):
                ctk.CTkLabel(scroll, text=stripped[3:],
                             font=ctk.CTkFont(size=16, weight="bold")).pack(
                    anchor="w", padx=4, pady=(10, 2))
            elif stripped.startswith("### "):
                ctk.CTkLabel(scroll, text=stripped[4:],
                             font=ctk.CTkFont(size=14, weight="bold")).pack(
                    anchor="w", padx=4, pady=(8, 2))
            elif stripped.startswith("#### "):
                ctk.CTkLabel(scroll, text=stripped[5:],
                             font=ctk.CTkFont(size=13, weight="bold")).pack(
                    anchor="w", padx=4, pady=(6, 1))

            # Bullet points
            elif stripped.startswith("- ") or stripped.startswith("* "):
                text = stripped[2:]
                # Strip markdown bold markers for display
                display = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
                ctk.CTkLabel(scroll, text=f"  •  {display}",
                             text_color="#ccc", font=ctk.CTkFont(size=12),
                             anchor="w", justify="left", wraplength=650).pack(
                    anchor="w", padx=4, pady=1)

            # Numbered list items
            elif re.match(r'^\d+\.', stripped):
                display = re.sub(r'\*\*(.+?)\*\*', r'\1', stripped)
                ctk.CTkLabel(scroll, text=f"  {display}",
                             text_color="#ccc", font=ctk.CTkFont(size=12),
                             anchor="w", justify="left", wraplength=650).pack(
                    anchor="w", padx=4, pady=1)

            # Regular text
            else:
                display = re.sub(r'\*\*(.+?)\*\*', r'\1', stripped)
                ctk.CTkLabel(scroll, text=display,
                             text_color="#aaa", font=ctk.CTkFont(size=12),
                             anchor="w", justify="left", wraplength=680).pack(
                    anchor="w", padx=4, pady=1)

        # Flush any remaining table
        if in_table:
            _flush_table()

        # Close button
        ctk.CTkButton(ov, text="Close", width=100, fg_color="#555",
                      command=self._hide_overlay).pack(pady=6)

    # ── Settings ───────────────────────────────────────────────────────

    def _open_settings(self):
        self._show_overlay()
        ov = self.overlay

        scroll = ctk.CTkScrollableFrame(ov)
        scroll.pack(fill="both", expand=True, padx=12, pady=12)

        ctk.CTkLabel(scroll, text="Settings",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w", pady=(0, 2))
        ctk.CTkLabel(scroll, text="Configure your streaming platform credentials and preferences.",
                     text_color="#888", font=ctk.CTkFont(size=12)).pack(anchor="w", pady=(0, 6))

        # Read current .env
        env_path = Path(__file__).parent / ".env"
        env_lines = []
        if env_path.exists():
            env_lines = env_path.read_text().splitlines()

        current = {}
        for line in env_lines:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                key, _, val = line.partition("=")
                current[key.strip()] = val.strip().strip('"').strip("'")

        entries = {}

        # Field definitions grouped by section:
        # (key, label, help_text)
        sections = [
            ("Twitch  (required)", "Get these from dev.twitch.tv/console → Applications", [
                ("TWITCH_CLIENT_ID", "Client ID", "Your app's Client ID from the Twitch Developer Console"),
                ("TWITCH_CLIENT_SECRET", "Client Secret", "Your app's Client Secret (click 'New Secret' to regenerate)"),
                ("TWITCH_USERNAME", "Username", "Your Twitch channel name (e.g. pakzarks)"),
            ]),
            ("Clip Settings", "", [
                ("PLATFORM", "VOD Platform", "Where your VODs are saved: 'youtube' or 'tiktok'"),
                ("CLIP_DAYS", "Days to Search", "How many days back to look for clips (max 60)"),
                ("CLIP_LENGTH", "Clip Length", "Minutes of VOD to grab per clip (0.5 to 5, default 2)"),
                ("SHOW_AUTOCLIPS", "Show Autoclips", "'true' or 'false' — autoclips are Twitch's auto-generated clips"),
                ("SHOW_OTHERS_CLIPS", "Show Viewers' Clips", "'true' or 'false' — clips made by your viewers"),
            ]),
            ("Style", "Color theme and text positioning for burned captions/title", []),
            ("YouTube", "Get API key + OAuth from console.cloud.google.com/apis/credentials", [
                ("YOUTUBE_API_KEY", "API Key", "For VOD lookup — APIs & Services → Credentials → Create API Key"),
                ("YOUTUBE_CHANNEL_HANDLE", "Channel Handle", "Your YouTube handle (e.g. pakzarks)"),
                ("YOUTUBE_CLIENT_ID", "OAuth Client ID", "For uploads — Create OAuth 2.0 Client ID → Desktop app type"),
                ("YOUTUBE_CLIENT_SECRET", "OAuth Client Secret", "Paired with the Client ID above"),
            ]),
            ("TikTok", "Get these from developers.tiktok.com → Manage Apps", [
                ("TIKTOK_CLIENT_KEY", "Client Key", "Your TikTok app's Client Key"),
                ("TIKTOK_CLIENT_SECRET", "Client Secret", "Your TikTok app's Client Secret"),
            ]),
            ("AI Title Generation", "Auto-generates titles and captions from clip transcripts", [
                ("ANTHROPIC_API_KEY", "Anthropic API Key", "From console.anthropic.com — used for AI title/caption generation"),
            ]),
        ]

        all_keys = []  # track order for saving
        for section_title, section_help, fields in sections:
            # Section header
            sec_frame = ctk.CTkFrame(scroll, fg_color="#1a1a1a", corner_radius=8)
            sec_frame.pack(fill="x", pady=(4, 0))

            ctk.CTkLabel(sec_frame, text=section_title,
                         font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=10, pady=(6, 0))
            if section_help:
                ctk.CTkLabel(sec_frame, text=section_help,
                             text_color="#666", font=ctk.CTkFont(size=11),
                             wraplength=500).pack(anchor="w", padx=10, pady=(0, 2))

            if section_title == "Style":
                # ── Title color theme buttons ──
                title_theme_var = ctk.StringVar(value=current.get("TITLE_COLOR_THEME", "") or current.get("COLOR_THEME", "blue"))
                ctk.CTkLabel(sec_frame, text="Title Color", anchor="w",
                             font=ctk.CTkFont(size=12)).pack(anchor="w", padx=10, pady=(4, 0))
                title_theme_row = ctk.CTkFrame(sec_frame, fg_color="transparent")
                title_theme_row.pack(fill="x", padx=10, pady=(2, 0))
                title_theme_btns = {}
                for idx, key in enumerate(clipper.COLOR_THEME_ORDER):
                    t = clipper.COLOR_THEMES[key]
                    btn = ctk.CTkButton(
                        title_theme_row, text="", width=40, height=40,
                        fg_color=t["box"], hover_color=t["box"],
                        border_width=3,
                        border_color="#4CAF50" if key == title_theme_var.get() else "#333",
                        corner_radius=6,
                        command=lambda k=key: _select_title_theme(k))
                    btn.pack(side="left", padx=3)
                    title_theme_btns[key] = btn

                title_theme_name_label = ctk.CTkLabel(
                    sec_frame,
                    text=clipper.COLOR_THEMES.get(title_theme_var.get(), {}).get("name", ""),
                    text_color="#888", font=ctk.CTkFont(size=11))
                title_theme_name_label.pack(anchor="w", padx=10, pady=(2, 0))

                def _select_title_theme(k):
                    title_theme_var.set(k)
                    for bk, bt in title_theme_btns.items():
                        bt.configure(border_color="#4CAF50" if bk == k else "#333")
                    title_theme_name_label.configure(
                        text=clipper.COLOR_THEMES.get(k, {}).get("name", ""))

                # ── Caption color theme buttons ──
                cap_theme_var = ctk.StringVar(value=current.get("CAPTION_COLOR_THEME", "") or current.get("COLOR_THEME", "blue"))
                ctk.CTkLabel(sec_frame, text="Caption Color", anchor="w",
                             font=ctk.CTkFont(size=12)).pack(anchor="w", padx=10, pady=(6, 0))
                cap_theme_row = ctk.CTkFrame(sec_frame, fg_color="transparent")
                cap_theme_row.pack(fill="x", padx=10, pady=(2, 0))
                cap_theme_btns = {}
                for idx, key in enumerate(clipper.COLOR_THEME_ORDER):
                    t = clipper.COLOR_THEMES[key]
                    btn = ctk.CTkButton(
                        cap_theme_row, text="", width=40, height=40,
                        fg_color=t["box"], hover_color=t["box"],
                        border_width=3,
                        border_color="#4CAF50" if key == cap_theme_var.get() else "#333",
                        corner_radius=6,
                        command=lambda k=key: _select_cap_theme(k))
                    btn.pack(side="left", padx=3)
                    cap_theme_btns[key] = btn

                cap_theme_name_label = ctk.CTkLabel(
                    sec_frame,
                    text=clipper.COLOR_THEMES.get(cap_theme_var.get(), {}).get("name", ""),
                    text_color="#888", font=ctk.CTkFont(size=11))
                cap_theme_name_label.pack(anchor="w", padx=10, pady=(2, 0))

                def _select_cap_theme(k):
                    cap_theme_var.set(k)
                    for bk, bt in cap_theme_btns.items():
                        bt.configure(border_color="#4CAF50" if bk == k else "#333")
                    cap_theme_name_label.configure(
                        text=clipper.COLOR_THEMES.get(k, {}).get("name", ""))

                # ── Title Y slider ──
                title_y_var = ctk.DoubleVar(value=float(current.get("TITLE_Y_PERCENT", "59")))
                ty_row = ctk.CTkFrame(sec_frame, fg_color="transparent")
                ty_row.pack(fill="x", padx=10, pady=(6, 0))
                ctk.CTkLabel(ty_row, text="Title Height", anchor="w",
                             font=ctk.CTkFont(size=12), width=110).pack(side="left")
                ty_val_label = ctk.CTkLabel(ty_row, text=f"{int(title_y_var.get())}%",
                                            font=ctk.CTkFont(size=12), width=40)
                ty_val_label.pack(side="right")
                ctk.CTkSlider(ty_row, from_=20, to=80, variable=title_y_var,
                              command=lambda v: ty_val_label.configure(
                                  text=f"{int(v)}%")).pack(side="left", fill="x", expand=True, padx=4)

                # ── Caption Y slider ──
                cap_y_var = ctk.DoubleVar(value=float(current.get("CAPTION_Y_PERCENT", "65")))
                cy_row = ctk.CTkFrame(sec_frame, fg_color="transparent")
                cy_row.pack(fill="x", padx=10, pady=(4, 0))
                ctk.CTkLabel(cy_row, text="Caption Height", anchor="w",
                             font=ctk.CTkFont(size=12), width=110).pack(side="left")
                cy_val_label = ctk.CTkLabel(cy_row, text=f"{int(cap_y_var.get())}%",
                                            font=ctk.CTkFont(size=12), width=40)
                cy_val_label.pack(side="right")
                ctk.CTkSlider(cy_row, from_=20, to=80, variable=cap_y_var,
                              command=lambda v: cy_val_label.configure(
                                  text=f"{int(v)}%")).pack(side="left", fill="x", expand=True, padx=4)

                ctk.CTkLabel(sec_frame, text="20% = near top, 80% = near bottom",
                             text_color="#555", font=ctk.CTkFont(size=10)).pack(anchor="w", padx=10, pady=(2, 0))
            else:
                for key, label, help_text in fields:
                    all_keys.append(key)
                    row = ctk.CTkFrame(sec_frame, fg_color="transparent")
                    row.pack(fill="x", padx=10, pady=(2, 0))
                    row.grid_columnconfigure(1, weight=1)

                    ctk.CTkLabel(row, text=label, anchor="w",
                                 font=ctk.CTkFont(size=12), width=130).grid(
                        row=0, column=0, sticky="w")

                    entry = ctk.CTkEntry(row, placeholder_text=help_text, height=28)
                    entry.grid(row=0, column=1, sticky="ew", padx=(4, 0))
                    val = current.get(key, "")
                    if val:
                        entry.insert(0, val)
                    entries[key] = entry

            # Bottom padding inside section
            ctk.CTkFrame(sec_frame, height=4, fg_color="transparent").pack()

        # ── Processing Mode selector ──
        sched_frame = ctk.CTkFrame(scroll, fg_color="#1a1a1a", corner_radius=8)
        sched_frame.pack(fill="x", pady=(8, 0))

        ctk.CTkLabel(sched_frame, text="Schedule",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=10, pady=(8, 0))

        mode_var = ctk.StringVar(value=self._schedule_mode)
        mode_seg = ctk.CTkSegmentedButton(
            sched_frame, values=["off", "auto", "auto-post"],
            variable=mode_var, font=ctk.CTkFont(size=12))
        mode_seg.pack(fill="x", padx=10, pady=(6, 4))

        mode_descriptions = {
            "off": "No schedule — use Fetch and Process to handle clips manually",
            "auto": "Clips fetched and processed on schedule, queued for your review before posting",
            "auto-post": "Clips fetched, processed, and uploaded automatically — no review needed",
        }
        mode_desc_label = ctk.CTkLabel(sched_frame, text=mode_descriptions.get(self._schedule_mode, ""),
                     text_color="#888", font=ctk.CTkFont(size=11),
                     wraplength=480)
        mode_desc_label.pack(anchor="w", padx=10, pady=(0, 4))

        # Schedule time picker (shown for auto/auto-post only)
        sched_row = ctk.CTkFrame(sched_frame, fg_color="transparent")

        ctk.CTkLabel(sched_row, text="Process daily at", text_color="#aaa",
                     font=ctk.CTkFont(size=12)).pack(side="left")
        hour_options = [f"{h % 12 or 12}{'am' if h < 12 else 'pm'}" for h in range(24)]
        sched_hour_var = ctk.StringVar(value=hour_options[clipper.SCHEDULE_HOUR])
        sched_hour_menu = ctk.CTkOptionMenu(sched_row, values=hour_options,
                                             variable=sched_hour_var, width=80, height=28,
                                             font=ctk.CTkFont(size=12))
        sched_hour_menu.pack(side="left", padx=(6, 0))

        ctk.CTkLabel(sched_row, text=":", text_color="#888",
                     font=ctk.CTkFont(size=12)).pack(side="left", padx=1)
        minute_options = [f"{m:02d}" for m in range(0, 60, 5)]
        cur_min_label = f"{(clipper.SCHEDULE_MINUTE // 5) * 5:02d}"
        if cur_min_label not in minute_options:
            cur_min_label = "00"
        sched_min_var = ctk.StringVar(value=cur_min_label)
        sched_min_menu = ctk.CTkOptionMenu(sched_row, values=minute_options,
                                            variable=sched_min_var, width=60, height=28,
                                            font=ctk.CTkFont(size=12))
        sched_min_menu.pack(side="left")

        if clipper.PLATFORM != "youtube":
            sched_warn = ctk.CTkLabel(sched_row, text="  Requires VOD Platform = youtube",
                                       text_color="#c0392b", font=ctk.CTkFont(size=11))
            sched_warn.pack(side="left", padx=(8, 0))
            sched_hour_menu.configure(state="disabled")
            sched_min_menu.configure(state="disabled")

        def _on_mode_change(val):
            mode_desc_label.configure(text=mode_descriptions.get(val, ""))
            if val in ("auto", "auto-post"):
                sched_row.pack(fill="x", padx=10, pady=(0, 8))
            else:
                sched_row.pack_forget()

        mode_var.trace_add("write", lambda *_: _on_mode_change(mode_var.get()))
        if self._schedule_mode in ("auto", "auto-post"):
            sched_row.pack(fill="x", padx=10, pady=(0, 8))

        ctk.CTkFrame(sched_frame, height=4, fg_color="transparent").pack()

        def _save():
            # Parse schedule values first (needed for .env write)
            sched_label = sched_hour_var.get()
            sched_hour_idx = hour_options.index(sched_label) if sched_label in hour_options else 10
            sched_min_val = int(sched_min_var.get())

            lines = []
            for key in all_keys:
                val = entries[key].get().strip()
                lines.append(f"{key}={val}")
            lines.append(f"CLIP_MODE={mode_var.get()}")
            lines.append(f"TITLE_COLOR_THEME={title_theme_var.get()}")
            lines.append(f"CAPTION_COLOR_THEME={cap_theme_var.get()}")
            lines.append(f"TITLE_Y_PERCENT={int(title_y_var.get())}")
            lines.append(f"CAPTION_Y_PERCENT={int(cap_y_var.get())}")
            lines.append(f"SCHEDULE_HOUR={sched_hour_idx}")
            lines.append(f"SCHEDULE_MINUTE={sched_min_val}")
            env_path.write_text("\n".join(lines) + "\n")

            # Reload env vars into clipper module without restarting
            from dotenv import load_dotenv
            load_dotenv(override=True)
            clipper.TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
            clipper.TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
            clipper.TWITCH_USERNAME = os.getenv("TWITCH_USERNAME")
            clipper.PLATFORM = os.getenv("PLATFORM", "tiktok").lower()
            clipper.CLIP_DAYS = min(int(os.getenv("CLIP_DAYS", "3")), 60)
            clipper.SHOW_AUTOCLIPS = os.getenv("SHOW_AUTOCLIPS", "true").lower() == "true"
            clipper.SHOW_OTHERS_CLIPS = os.getenv("SHOW_OTHERS_CLIPS", "true").lower() == "true"
            clipper.YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
            clipper.YOUTUBE_CHANNEL_HANDLE = os.getenv("YOUTUBE_CHANNEL_HANDLE", "")
            clipper.YOUTUBE_CLIENT_ID = os.getenv("YOUTUBE_CLIENT_ID", "")
            clipper.YOUTUBE_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET", "")
            clipper.TIKTOK_CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY", "")
            clipper.TIKTOK_CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET", "")
            clipper.ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
            clipper.CLIP_LENGTH = float(os.getenv("CLIP_LENGTH", "2"))
            clipper.CLIP_MODE = mode_var.get()
            clipper.TITLE_COLOR_THEME = title_theme_var.get()
            clipper.CAPTION_COLOR_THEME = cap_theme_var.get()
            clipper.TITLE_Y_PERCENT = float(int(title_y_var.get()))
            clipper.CAPTION_Y_PERCENT = float(int(cap_y_var.get()))
            clipper.SCHEDULE_HOUR = sched_hour_idx
            clipper.SCHEDULE_MINUTE = sched_min_val
            self.days_var.set(clipper.CLIP_DAYS)
            self._schedule_mode = mode_var.get()
            self._update_schedule_banner()

            # Schedule based on mode
            if mode_var.get() in ("auto", "auto-post") and clipper.PLATFORM == "youtube":
                clipper.schedule()
                time_label = f"{sched_label}:{sched_min_var.get()}"
                print(f"  Auto-process scheduled for {time_label} daily")
            elif clipper.is_scheduled():
                clipper.unschedule()
                print("  Auto-process schedule removed")

            print("  Settings saved and applied")
            self._hide_overlay()
            self._check_credentials()
            self._show_toast("Settings saved", "#4CAF50", duration=3000)

        btn_row = ctk.CTkFrame(ov, fg_color="transparent")
        btn_row.pack(pady=6)
        ctk.CTkButton(btn_row, text="Save", width=100, command=_save).pack(side="left", padx=4)
        ctk.CTkButton(btn_row, text="Cancel", width=100, fg_color="#555", command=self._hide_overlay).pack(side="left", padx=4)

    # ── Dialog helpers (thread-safe) ───────────────────────────────────

    def _ask_dialog(self, title, prompt, allow_skip=True):
        """Show an input dialog inline. Returns the entered text or ''.
        Raises SkipClip if user clicks Skip Clip."""
        result = [None]
        skipped = [False]
        event = threading.Event()

        def _show():
            self._show_overlay()
            ov = self.overlay

            ctk.CTkLabel(ov, text=title, font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(40, 8))
            ctk.CTkLabel(ov, text=prompt, text_color="#aaa", wraplength=400).pack(pady=(0, 12))

            entry = ctk.CTkEntry(ov, width=350, height=36, font=ctk.CTkFont(size=14))
            entry.pack(pady=(0, 16))
            entry.focus_set()

            def _submit(val=None):
                result[0] = entry.get().strip()
                self._hide_overlay()
                event.set()

            def _skip():
                skipped[0] = True
                self._hide_overlay()
                event.set()

            entry.bind("<Return>", _submit)

            btn_row = ctk.CTkFrame(ov, fg_color="transparent")
            btn_row.pack()
            ctk.CTkButton(btn_row, text="Continue", width=120, fg_color="#1D8CD7",
                           command=_submit).pack(side="left", padx=4)
            if allow_skip:
                ctk.CTkButton(btn_row, text="Skip Clip", width=100, fg_color="#555",
                               text_color="#ccc", command=_skip).pack(side="left", padx=4)
            ctk.CTkLabel(btn_row, text="or press Enter", text_color="#666",
                          font=ctk.CTkFont(size=11)).pack(side="left", padx=(8, 0))

        self.after(0, _show)
        self._wait_or_cancel(event)
        if skipped[0]:
            raise SkipClip()
        return result[0] or ""

    def _adjust_segment_dialog(self, seg_start, seg_end, video_path=None, allow_skip=True):
        """Show video player + trim controls in one screen.
        Returns (trim_start, trim_end, extend_start, extend_end) in seconds.
        Raises SkipClip if user clicks Skip Clip."""
        result = [0, 0, 0, 0]
        skipped = [False]
        event = threading.Event()

        def _show():
            duration = seg_end - seg_start
            ext_start = [0]
            ext_end = [0]
            pos = [0.0, 1.0]

            def _total_range():
                return duration + ext_start[0] + ext_end[0]

            self._show_overlay()
            ov = self.overlay
            scroll = ctk.CTkScrollableFrame(ov, fg_color="transparent")
            scroll.pack(fill="both", expand=True, padx=4, pady=4)

            # Header
            ctk.CTkLabel(scroll, text=f"Review & Trim ({clipper.fmt_time(duration)})",
                         font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(6, 2))

            # ── Video player (synced audio via _create_player) ──
            if video_path:
                player = self._create_player(scroll, video_path, max_w=480, max_h=420)
                vid_state = player
                self._active_player = player
            else:
                vid_state = {"stopped": False, "stop": lambda: None}

            # ── Trim controls ──
            ctk.CTkFrame(scroll, height=1, fg_color="#444").pack(fill="x", padx=24, pady=(4, 4))
            ctk.CTkLabel(scroll, text="Trim handles — drag to adjust start/end",
                          text_color="#888", font=ctk.CTkFont(size=11)).pack(pady=(0, 2))

            TRACK_PAD, TRACK_H, HANDLE_R = 40, 8, 10
            canvas_w, canvas_h = 440, 40
            canvas = ctk.CTkCanvas(scroll, width=canvas_w, height=canvas_h,
                                    bg="#2b2b2b", highlightthickness=0)
            canvas.pack(padx=24, pady=(0, 2))

            track_y = canvas_h // 2
            track_x0, track_x1 = TRACK_PAD, canvas_w - TRACK_PAD
            track_len = track_x1 - track_x0

            canvas.create_rectangle(track_x0, track_y - TRACK_H // 2,
                                     track_x1, track_y + TRACK_H // 2, fill="#444", outline="")
            region = canvas.create_rectangle(track_x0, track_y - TRACK_H // 2,
                                              track_x1, track_y + TRACK_H // 2, fill="#1D8CD7", outline="")
            left_handle = canvas.create_oval(track_x0 - HANDLE_R, track_y - HANDLE_R,
                                              track_x0 + HANDLE_R, track_y + HANDLE_R,
                                              fill="white", outline="#1D8CD7", width=2)
            right_handle = canvas.create_oval(track_x1 - HANDLE_R, track_y - HANDLE_R,
                                               track_x1 + HANDLE_R, track_y + HANDLE_R,
                                               fill="white", outline="#1D8CD7", width=2)

            lbl_frame = ctk.CTkFrame(scroll, fg_color="transparent")
            lbl_frame.pack(fill="x", padx=24)
            start_label = ctk.CTkLabel(lbl_frame, text=clipper.fmt_time(seg_start),
                                        font=ctk.CTkFont(size=11), text_color="#aaa")
            start_label.pack(side="left")
            dur_var = ctk.StringVar(value=f"Selected: {clipper.fmt_time(duration)}")
            ctk.CTkLabel(lbl_frame, textvariable=dur_var,
                          font=ctk.CTkFont(size=12, weight="bold")).pack(side="left", expand=True)
            end_label = ctk.CTkLabel(lbl_frame, text=clipper.fmt_time(seg_end),
                                      font=ctk.CTkFont(size=11), text_color="#aaa")
            end_label.pack(side="right")

            detail_var = ctk.StringVar(value="No changes — just Continue")
            ctk.CTkLabel(scroll, textvariable=detail_var, text_color="#666",
                          font=ctk.CTkFont(size=11)).pack(pady=(0, 4))

            def _update_display():
                total = _total_range()
                abs_start = (seg_start - ext_start[0]) + pos[0] * total
                abs_end = (seg_start - ext_start[0]) + pos[1] * total
                sel = abs_end - abs_start
                dur_var.set(f"Selected: {clipper.fmt_time(max(0, int(sel)))}")
                start_label.configure(text=clipper.fmt_time(int(abs_start)))
                end_label.configure(text=clipper.fmt_time(int(abs_end)))
                trim_s, trim_e = int(pos[0] * total), int((1.0 - pos[1]) * total)
                parts = []
                if ext_start[0] > 0: parts.append(f"+{ext_start[0]}s before")
                if ext_end[0] > 0: parts.append(f"+{ext_end[0]}s after")
                if trim_s > 0: parts.append(f"trim {trim_s}s from start")
                if trim_e > 0: parts.append(f"trim {trim_e}s from end")
                detail_var.set(", ".join(parts) if parts else "No changes — just Continue")
                lx = track_x0 + pos[0] * track_len
                rx = track_x0 + pos[1] * track_len
                canvas.coords(region, lx, track_y - TRACK_H // 2, rx, track_y + TRACK_H // 2)
                canvas.coords(left_handle, lx - HANDLE_R, track_y - HANDLE_R, lx + HANDLE_R, track_y + HANDLE_R)
                canvas.coords(right_handle, rx - HANDLE_R, track_y - HANDLE_R, rx + HANDLE_R, track_y + HANDLE_R)

            dragging = [None]
            def _press(e):
                lx, rx = track_x0 + pos[0] * track_len, track_x0 + pos[1] * track_len
                dl, dr = abs(e.x - lx), abs(e.x - rx)
                if dl < dr and dl < HANDLE_R * 2: dragging[0] = "left"
                elif dr < HANDLE_R * 2: dragging[0] = "right"
            def _drag(e):
                if not dragging[0]: return
                frac = max(0.0, min(1.0, (e.x - track_x0) / track_len))
                total = _total_range()
                frac = round(frac * total) / total if total > 0 else frac
                if dragging[0] == "left": pos[0] = min(frac, pos[1] - 5 / total)
                else: pos[1] = max(frac, pos[0] + 5 / total)
                pos[0], pos[1] = max(0.0, pos[0]), min(1.0, pos[1])
                _update_display()
            def _release(e):
                dragging[0] = None

            canvas.bind("<ButtonPress-1>", _press)
            canvas.bind("<B1-Motion>", _drag)
            canvas.bind("<ButtonRelease-1>", _release)

            # Buttons row
            btn_row = ctk.CTkFrame(scroll, fg_color="transparent")
            btn_row.pack(pady=(4, 8))

            def _extend(which):
                if which == "start": ext_start[0] += 30
                else: ext_end[0] += 30
                old_total = _total_range() - 30
                new_total = _total_range()
                if which == "start":
                    # Shift handles to preserve absolute positions, include new range (left → 0)
                    pos[0] = 0.0
                    pos[1] = (pos[1] * old_total + 30) / new_total
                else:
                    # Shift handles to preserve absolute positions, include new range (right → 1)
                    pos[0] = (pos[0] * old_total) / new_total
                    pos[1] = 1.0
                _update_display()

            ctk.CTkButton(btn_row, text="+30s Start", width=80, height=28, fg_color="#555",
                           font=ctk.CTkFont(size=11),
                           command=lambda: _extend("start")).pack(side="left", padx=3)
            ctk.CTkButton(btn_row, text="+30s End", width=80, height=28, fg_color="#555",
                           font=ctk.CTkFont(size=11),
                           command=lambda: _extend("end")).pack(side="left", padx=3)

            def _continue():
                vid_state["stop"]()
                self._active_player = None
                total = _total_range()
                result[0] = int(round(pos[0] * total))
                result[1] = int(round((1.0 - pos[1]) * total))
                result[2] = ext_start[0]
                result[3] = ext_end[0]
                self._hide_overlay()
                event.set()

            ctk.CTkButton(btn_row, text="Continue", width=120, height=32, fg_color="#1D8CD7",
                           font=ctk.CTkFont(size=13, weight="bold"),
                           command=_continue).pack(side="left", padx=(10, 3))

            if allow_skip:
                def _skip():
                    vid_state["stop"]()
                    skipped[0] = True
                    self._hide_overlay()
                    event.set()
                ctk.CTkButton(btn_row, text="Skip", width=60, height=28,
                               fg_color="#555", text_color="#ccc",
                               font=ctk.CTkFont(size=11), command=_skip).pack(side="left", padx=3)

        self.after(0, _show)
        self._wait_or_cancel(event)
        if skipped[0]:
            raise SkipClip()
        return tuple(result)

    def _confirm_dialog(self, title, message, yes_text="Yes", no_text="No", subtitle=None):
        """Show a yes/no dialog inline. Returns True/False.
        Also returns False if a step bar click happens (jump to that step)."""
        result = [False]
        event = threading.Event()

        def _show():
            self._show_overlay()
            ov = self.overlay

            ctk.CTkLabel(ov, text=title, font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(50, 4))
            ctk.CTkLabel(ov, text=message, font=ctk.CTkFont(size=14), text_color="#aaa").pack(pady=(0, 4))
            if subtitle:
                ctk.CTkLabel(ov, text=subtitle, font=ctk.CTkFont(size=12),
                              text_color="#666").pack(pady=(0, 16))
            else:
                ctk.CTkFrame(ov, height=12, fg_color="transparent").pack()

            btn_row = ctk.CTkFrame(ov, fg_color="transparent")
            btn_row.pack()

            def _yes():
                result[0] = True
                self._hide_overlay()
                event.set()

            def _no():
                result[0] = False
                self._hide_overlay()
                event.set()

            ctk.CTkButton(btn_row, text=yes_text, width=120, fg_color="#1D8CD7", command=_yes).pack(side="left", padx=8)
            ctk.CTkButton(btn_row, text=no_text, width=120, fg_color="#555", command=_no).pack(side="left", padx=8)

        self.after(0, _show)
        self._wait_or_cancel(event)
        return result[0]

    def _mark_posted_dialog(self, video_path, missing_yt, missing_tt):
        """Show checkboxes for which platforms the user manually posted to."""
        self._show_overlay()
        ov = self.overlay

        ctk.CTkLabel(ov, text="Mark as Posted",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(60, 4))
        ctk.CTkLabel(ov, text="Which platforms did you post this to?",
                     font=ctk.CTkFont(size=13), text_color="#aaa").pack(pady=(0, 12))

        yt_var = ctk.BooleanVar(value=False)
        tt_var = ctk.BooleanVar(value=False)

        checks = ctk.CTkFrame(ov, fg_color="transparent")
        checks.pack(pady=(0, 12))

        if missing_yt:
            ctk.CTkCheckBox(checks, text="YouTube", variable=yt_var,
                            font=ctk.CTkFont(size=13)).pack(anchor="w", pady=2)
        if missing_tt:
            ctk.CTkCheckBox(checks, text="TikTok", variable=tt_var,
                            font=ctk.CTkFont(size=13)).pack(anchor="w", pady=2)

        btn_row = ctk.CTkFrame(ov, fg_color="transparent")
        btn_row.pack()

        def _confirm():
            updates = {}
            if yt_var.get():
                updates["youtube"] = True
                updates["youtube_failed"] = False
            if tt_var.get():
                updates["tiktok"] = True
                updates["tiktok_failed"] = False
            if updates:
                updates["status"] = "posted"
                self._set_video_status(video_path.name, **updates)
            self._hide_overlay()
            self._refresh_output_tab()

        ctk.CTkButton(btn_row, text="Save", width=100, fg_color="#1D8CD7",
                       font=ctk.CTkFont(size=13, weight="bold"),
                       command=_confirm).pack(side="left", padx=6)
        ctk.CTkButton(btn_row, text="Cancel", width=80, fg_color="#555",
                       command=lambda: (self._hide_overlay())).pack(side="left", padx=6)

    def _upload_choice_dialog(self):
        """Show upload options: Upload Now / Schedule Post / Not Now.
        Returns 'now', 'schedule', or 'not_now'."""
        result = ["not_now"]
        event = threading.Event()

        def _show():
            self._show_overlay()
            ov = self.overlay

            ctk.CTkLabel(ov, text="Upload", font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(50, 4))
            ctk.CTkLabel(ov, text="Upload to YouTube and TikTok?",
                         font=ctk.CTkFont(size=14), text_color="#aaa").pack(pady=(0, 16))

            btn_row = ctk.CTkFrame(ov, fg_color="transparent")
            btn_row.pack()

            def _set(val):
                result[0] = val
                self._hide_overlay()
                event.set()

            ctk.CTkButton(btn_row, text="Upload Now", width=120, fg_color="#1D8CD7",
                           font=ctk.CTkFont(size=13, weight="bold"),
                           command=lambda: _set("now")).pack(side="left", padx=6)
            ctk.CTkButton(btn_row, text="Schedule Post", width=120, fg_color="#555",
                           command=lambda: _set("schedule")).pack(side="left", padx=6)
            ctk.CTkButton(btn_row, text="Not Now", width=100, fg_color="#444",
                           text_color="#aaa",
                           command=lambda: _set("not_now")).pack(side="left", padx=6)

        self.after(0, _show)
        self._wait_or_cancel(event)
        return result[0]

    def _schedule_post_dialog(self):
        """Show a date/time picker for scheduling a post.
        Returns a datetime or None if cancelled."""
        result = [None]
        event = threading.Event()

        def _show():
            self._show_overlay()
            ov = self.overlay

            ctk.CTkLabel(ov, text="Schedule Post",
                         font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(40, 4))
            ctk.CTkLabel(ov, text="Pick a date and time to upload",
                         text_color="#aaa").pack(pady=(0, 12))

            picker = ctk.CTkFrame(ov, fg_color="#1a1a1a", corner_radius=8)
            picker.pack(padx=40, pady=(0, 12))

            # Date row
            date_row = ctk.CTkFrame(picker, fg_color="transparent")
            date_row.pack(fill="x", padx=16, pady=(12, 4))

            ctk.CTkLabel(date_row, text="Date", width=50, anchor="w",
                         font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")

            # Build date options: today + next 14 days
            today = datetime.now()
            date_options = []
            date_values = []
            for d in range(15):
                dt = today + timedelta(days=d)
                if d == 0:
                    label = f"Today ({dt.strftime('%b %d')})"
                elif d == 1:
                    label = f"Tomorrow ({dt.strftime('%b %d')})"
                else:
                    label = dt.strftime("%A, %b %d")
                date_options.append(label)
                date_values.append(dt.strftime("%Y-%m-%d"))

            date_var = ctk.StringVar(value=date_options[1])  # default to tomorrow
            ctk.CTkOptionMenu(date_row, values=date_options, variable=date_var,
                               width=200, height=28,
                               font=ctk.CTkFont(size=12)).pack(side="left", padx=(8, 0))

            # Time row
            time_row = ctk.CTkFrame(picker, fg_color="transparent")
            time_row.pack(fill="x", padx=16, pady=(4, 12))

            ctk.CTkLabel(time_row, text="Time", width=50, anchor="w",
                         font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")

            hour_opts = [f"{h % 12 or 12}{'am' if h < 12 else 'pm'}" for h in range(24)]
            hour_var = ctk.StringVar(value=hour_opts[12])  # default noon
            ctk.CTkOptionMenu(time_row, values=hour_opts, variable=hour_var,
                               width=80, height=28,
                               font=ctk.CTkFont(size=12)).pack(side="left", padx=(8, 0))

            ctk.CTkLabel(time_row, text=":", text_color="#888",
                         font=ctk.CTkFont(size=12)).pack(side="left", padx=2)

            min_opts = [f"{m:02d}" for m in range(0, 60, 5)]
            min_var = ctk.StringVar(value="00")
            ctk.CTkOptionMenu(time_row, values=min_opts, variable=min_var,
                               width=60, height=28,
                               font=ctk.CTkFont(size=12)).pack(side="left")

            # Buttons
            btn_row = ctk.CTkFrame(ov, fg_color="transparent")
            btn_row.pack(pady=(0, 8))

            def _confirm():
                date_label = date_var.get()
                date_idx = date_options.index(date_label) if date_label in date_options else 1
                date_str = date_values[date_idx]

                hour_label = hour_var.get()
                hour_idx = hour_opts.index(hour_label) if hour_label in hour_opts else 12
                minute = int(min_var.get())

                sched_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(
                    hour=hour_idx, minute=minute)
                result[0] = sched_dt
                self._hide_overlay()
                event.set()

            def _cancel():
                self._hide_overlay()
                event.set()

            ctk.CTkButton(btn_row, text="Schedule", width=120, fg_color="#1D8CD7",
                           font=ctk.CTkFont(size=13, weight="bold"),
                           command=_confirm).pack(side="left", padx=6)
            ctk.CTkButton(btn_row, text="Cancel", width=100, fg_color="#555",
                           command=_cancel).pack(side="left", padx=6)

        self.after(0, _show)
        self._wait_or_cancel(event)
        return result[0]

    def _preview_and_approve(self, output_path, title, description, video_path,
                              whisper_result, clip_date, skip_label="Discard", **kwargs):
        """Combined preview + inline title/caption editing.
        Returns (choice, final_title, final_description, final_output_path).
        choice is 'approved', 'retrim', or 'skip'.
        If the user edits the title and re-burns, final_output_path may differ."""
        result = {"choice": "retrim", "title": title, "description": description,
                  "output_path": output_path}
        event = threading.Event()

        def _show():
            self._show_overlay()
            ov = self.overlay
            scroll = ctk.CTkScrollableFrame(ov, fg_color="transparent")
            scroll.pack(fill="both", expand=True, padx=4, pady=4)

            ctk.CTkLabel(scroll, text="Final Preview",
                         font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(8, 4))

            # Video player
            player = self._create_player(scroll, output_path)
            self._active_player = player
            ctrl = player["ctrl_frame"]

            # Re-trim button next to player controls
            def _retrim():
                player["stop"]()
                self._active_player = None
                result["choice"] = "retrim"
                self._hide_overlay()
                event.set()
            ctk.CTkButton(ctrl, text="✂ Re-trim", width=100, fg_color="#555",
                           font=ctk.CTkFont(size=11), command=_retrim).pack(side="left", padx=4)

            # ── Editable title & caption ──
            ctk.CTkFrame(scroll, height=1, fg_color="#444").pack(fill="x", padx=24, pady=(8, 4))

            edit_frame = ctk.CTkFrame(scroll, fg_color="#1a1a1a", corner_radius=8)
            edit_frame.pack(fill="x", padx=24, pady=(0, 4))

            # Title row
            title_row = ctk.CTkFrame(edit_frame, fg_color="transparent")
            title_row.pack(fill="x", padx=10, pady=(8, 2))
            ctk.CTkLabel(title_row, text="Title", width=60, anchor="w",
                         font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
            title_entry = ctk.CTkEntry(title_row, height=30, font=ctk.CTkFont(size=13))
            title_entry.pack(side="left", fill="x", expand=True, padx=(4, 0))
            title_entry.insert(0, title)

            # Caption row
            cap_row = ctk.CTkFrame(edit_frame, fg_color="transparent")
            cap_row.pack(fill="x", padx=10, pady=(2, 4))
            ctk.CTkLabel(cap_row, text="Caption", width=60, anchor="w",
                         font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
            cap_entry = ctk.CTkEntry(cap_row, height=30, font=ctk.CTkFont(size=13))
            cap_entry.pack(side="left", fill="x", expand=True, padx=(4, 0))
            cap_entry.insert(0, description)

            # ── Style controls (per-clip, initialized from current defaults) ──
            ctk.CTkFrame(edit_frame, height=1, fg_color="#333").pack(fill="x", padx=10, pady=(4, 4))

            # Title color theme
            style_title_theme_var = ctk.StringVar(value=clipper.TITLE_COLOR_THEME)
            title_theme_label_row = ctk.CTkFrame(edit_frame, fg_color="transparent")
            title_theme_label_row.pack(fill="x", padx=10, pady=(2, 0))
            ctk.CTkLabel(title_theme_label_row, text="Title Color", width=80, anchor="w",
                         font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
            style_title_theme_name = ctk.CTkLabel(
                title_theme_label_row,
                text=clipper.COLOR_THEMES.get(clipper.TITLE_COLOR_THEME, {}).get("name", ""),
                text_color="#888", font=ctk.CTkFont(size=11))
            style_title_theme_name.pack(side="left", padx=(4, 0))

            title_theme_row = ctk.CTkFrame(edit_frame, fg_color="transparent")
            title_theme_row.pack(fill="x", padx=10, pady=(2, 0))
            style_title_theme_btns = {}
            for key in clipper.COLOR_THEME_ORDER:
                t = clipper.COLOR_THEMES[key]
                btn = ctk.CTkButton(
                    title_theme_row, text="", width=36, height=36,
                    fg_color=t["box"], hover_color=t["box"],
                    border_width=3,
                    border_color="#4CAF50" if key == style_title_theme_var.get() else "#333",
                    corner_radius=6,
                    command=lambda k=key: _select_title_theme(k))
                btn.pack(side="left", padx=2)
                style_title_theme_btns[key] = btn

            def _select_title_theme(k):
                style_title_theme_var.set(k)
                for bk, bt in style_title_theme_btns.items():
                    bt.configure(border_color="#4CAF50" if bk == k else "#333")
                style_title_theme_name.configure(
                    text=clipper.COLOR_THEMES.get(k, {}).get("name", ""))
                _check_needs_reburn()

            # Caption color theme
            style_cap_theme_var = ctk.StringVar(value=clipper.CAPTION_COLOR_THEME)
            cap_theme_label_row = ctk.CTkFrame(edit_frame, fg_color="transparent")
            cap_theme_label_row.pack(fill="x", padx=10, pady=(6, 0))
            ctk.CTkLabel(cap_theme_label_row, text="Caption Color", width=80, anchor="w",
                         font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
            style_cap_theme_name = ctk.CTkLabel(
                cap_theme_label_row,
                text=clipper.COLOR_THEMES.get(clipper.CAPTION_COLOR_THEME, {}).get("name", ""),
                text_color="#888", font=ctk.CTkFont(size=11))
            style_cap_theme_name.pack(side="left", padx=(4, 0))

            cap_theme_row = ctk.CTkFrame(edit_frame, fg_color="transparent")
            cap_theme_row.pack(fill="x", padx=10, pady=(2, 0))
            style_cap_theme_btns = {}
            for key in clipper.COLOR_THEME_ORDER:
                t = clipper.COLOR_THEMES[key]
                btn = ctk.CTkButton(
                    cap_theme_row, text="", width=36, height=36,
                    fg_color=t["box"], hover_color=t["box"],
                    border_width=3,
                    border_color="#4CAF50" if key == style_cap_theme_var.get() else "#333",
                    corner_radius=6,
                    command=lambda k=key: _select_cap_theme(k))
                btn.pack(side="left", padx=2)
                style_cap_theme_btns[key] = btn

            def _select_cap_theme(k):
                style_cap_theme_var.set(k)
                for bk, bt in style_cap_theme_btns.items():
                    bt.configure(border_color="#4CAF50" if bk == k else "#333")
                style_cap_theme_name.configure(
                    text=clipper.COLOR_THEMES.get(k, {}).get("name", ""))
                _check_needs_reburn()

            # Title Y slider
            style_title_y = ctk.DoubleVar(value=clipper.TITLE_Y_PERCENT)
            ty_row = ctk.CTkFrame(edit_frame, fg_color="transparent")
            ty_row.pack(fill="x", padx=10, pady=(6, 0))
            ctk.CTkLabel(ty_row, text="Title Height", anchor="w",
                         font=ctk.CTkFont(size=12), width=90).pack(side="left")
            ty_val = ctk.CTkLabel(ty_row, text=f"{int(style_title_y.get())}%",
                                  font=ctk.CTkFont(size=12), width=36)
            ty_val.pack(side="right")
            ctk.CTkSlider(ty_row, from_=20, to=80, variable=style_title_y,
                          command=lambda v: (ty_val.configure(text=f"{int(v)}%"),
                                             _check_needs_reburn())).pack(
                side="left", fill="x", expand=True, padx=4)

            # Caption Y slider
            style_cap_y = ctk.DoubleVar(value=clipper.CAPTION_Y_PERCENT)
            cy_row = ctk.CTkFrame(edit_frame, fg_color="transparent")
            cy_row.pack(fill="x", padx=10, pady=(2, 0))
            ctk.CTkLabel(cy_row, text="Caption Height", anchor="w",
                         font=ctk.CTkFont(size=12), width=90).pack(side="left")
            cy_val = ctk.CTkLabel(cy_row, text=f"{int(style_cap_y.get())}%",
                                  font=ctk.CTkFont(size=12), width=36)
            cy_val.pack(side="right")
            ctk.CTkSlider(cy_row, from_=20, to=80, variable=style_cap_y,
                          command=lambda v: (cy_val.configure(text=f"{int(v)}%"),
                                             _check_needs_reburn())).pack(
                side="left", fill="x", expand=True, padx=4)

            ctk.CTkLabel(edit_frame, text="20% = near top, 80% = near bottom",
                         text_color="#555", font=ctk.CTkFont(size=10)).pack(anchor="w", padx=10, pady=(1, 0))

            # Re-burn hint + button (shown when title or style changes)
            reburn_frame = ctk.CTkFrame(edit_frame, fg_color="transparent")
            reburn_frame.pack(fill="x", padx=10, pady=(4, 4))
            reburn_hint = ctk.CTkLabel(reburn_frame, text="", text_color="#e67e22",
                                        font=ctk.CTkFont(size=11))
            reburn_hint.pack(side="left")
            reburn_btn = ctk.CTkButton(reburn_frame, text="Re-burn", width=90,
                                        fg_color="#e67e22", hover_color="#d35400",
                                        font=ctk.CTkFont(size=12))
            # Hidden by default
            reburn_btn.pack_forget()

            # Save as Default button (next to re-burn)
            save_default_btn = ctk.CTkButton(reburn_frame, text="Save as Default", width=110,
                                              fg_color="#555", hover_color="#666",
                                              font=ctk.CTkFont(size=11))
            save_default_btn.pack_forget()

            original_title = [title]
            original_style = [clipper.TITLE_COLOR_THEME, clipper.CAPTION_COLOR_THEME,
                              int(clipper.TITLE_Y_PERCENT), int(clipper.CAPTION_Y_PERCENT)]

            def _style_changed():
                return (style_title_theme_var.get() != original_style[0]
                        or style_cap_theme_var.get() != original_style[1]
                        or int(style_title_y.get()) != original_style[2]
                        or int(style_cap_y.get()) != original_style[3])

            def _check_needs_reburn(*_):
                new_title = title_entry.get().strip()
                title_changed = new_title and new_title != original_title[0]
                style_diff = _style_changed()
                if title_changed or style_diff:
                    reasons = []
                    if title_changed:
                        reasons.append("title")
                    if style_diff:
                        reasons.append("style")
                    reburn_hint.configure(text=f"{' & '.join(reasons).capitalize()} changed — re-burn to update")
                    reburn_btn.pack(side="right", padx=(8, 0))
                    # Disable "Looks Good" until reburn is done
                    looks_good_btn.configure(state="disabled")
                else:
                    reburn_hint.configure(text="")
                    reburn_btn.pack_forget()
                    looks_good_btn.configure(state="normal")
                # Show save-as-default if style differs from saved defaults
                cur_defaults = [clipper.TITLE_COLOR_THEME, clipper.CAPTION_COLOR_THEME,
                                int(clipper.TITLE_Y_PERCENT), int(clipper.CAPTION_Y_PERCENT)]
                cur_style = [style_title_theme_var.get(), style_cap_theme_var.get(),
                             int(style_title_y.get()), int(style_cap_y.get())]
                if cur_style != cur_defaults:
                    save_default_btn.pack(side="right", padx=(4, 0))
                else:
                    save_default_btn.pack_forget()

            title_entry.bind("<KeyRelease>", _check_needs_reburn)

            def _save_as_default():
                """Save current style values as defaults in .env and clipper globals."""
                env_path = Path(__file__).parent / ".env"
                new_title_theme = style_title_theme_var.get()
                new_cap_theme = style_cap_theme_var.get()
                new_title_y = int(style_title_y.get())
                new_cap_y = int(style_cap_y.get())

                # Read and update .env
                if env_path.exists():
                    lines = env_path.read_text().splitlines()
                else:
                    lines = []
                updated_keys = set()
                new_lines = []
                for line in lines:
                    stripped = line.strip()
                    if stripped.startswith("COLOR_THEME="):
                        # Remove legacy single COLOR_THEME
                        continue
                    elif stripped.startswith("TITLE_COLOR_THEME="):
                        new_lines.append(f"TITLE_COLOR_THEME={new_title_theme}")
                        updated_keys.add("TITLE_COLOR_THEME")
                    elif stripped.startswith("CAPTION_COLOR_THEME="):
                        new_lines.append(f"CAPTION_COLOR_THEME={new_cap_theme}")
                        updated_keys.add("CAPTION_COLOR_THEME")
                    elif stripped.startswith("TITLE_Y_PERCENT="):
                        new_lines.append(f"TITLE_Y_PERCENT={new_title_y}")
                        updated_keys.add("TITLE_Y_PERCENT")
                    elif stripped.startswith("CAPTION_Y_PERCENT="):
                        new_lines.append(f"CAPTION_Y_PERCENT={new_cap_y}")
                        updated_keys.add("CAPTION_Y_PERCENT")
                    else:
                        new_lines.append(line)
                # Append any missing keys
                if "TITLE_COLOR_THEME" not in updated_keys:
                    new_lines.append(f"TITLE_COLOR_THEME={new_title_theme}")
                if "CAPTION_COLOR_THEME" not in updated_keys:
                    new_lines.append(f"CAPTION_COLOR_THEME={new_cap_theme}")
                if "TITLE_Y_PERCENT" not in updated_keys:
                    new_lines.append(f"TITLE_Y_PERCENT={new_title_y}")
                if "CAPTION_Y_PERCENT" not in updated_keys:
                    new_lines.append(f"CAPTION_Y_PERCENT={new_cap_y}")
                env_path.write_text("\n".join(new_lines) + "\n")

                # Update clipper globals
                clipper.TITLE_COLOR_THEME = new_title_theme
                clipper.CAPTION_COLOR_THEME = new_cap_theme
                clipper.TITLE_Y_PERCENT = float(new_title_y)
                clipper.CAPTION_Y_PERCENT = float(new_cap_y)

                # Update tracking so button hides
                original_style[0] = new_title_theme
                original_style[1] = new_cap_theme
                original_style[2] = new_title_y
                original_style[3] = new_cap_y
                save_default_btn.pack_forget()
                self._show_toast("Defaults saved", "#4CAF50", duration=2000)

            save_default_btn.configure(command=_save_as_default)

            def _reburn():
                new_title = title_entry.get().strip()
                if not new_title:
                    return

                # Stop player while re-burning
                player["stop"]()
                self._active_player = None

                # Apply per-clip style to clipper globals before re-burn
                result["style"] = {
                    "title_theme": style_title_theme_var.get(),
                    "cap_theme": style_cap_theme_var.get(),
                    "title_y": int(style_title_y.get()),
                    "cap_y": int(style_cap_y.get()),
                }

                # Signal re-burn to the worker thread
                result["choice"] = "reburn"
                result["title"] = new_title
                result["description"] = cap_entry.get().strip() or new_title
                self._hide_overlay()
                event.set()

            reburn_btn.configure(command=_reburn)

            # ── Action buttons ──
            ctk.CTkFrame(scroll, height=4, fg_color="transparent").pack()
            btn_row = ctk.CTkFrame(scroll, fg_color="transparent")
            btn_row.pack(pady=(0, 8))

            def _approve():
                player["stop"]()
                self._active_player = None
                result["choice"] = "approved"
                result["title"] = title_entry.get().strip() or title
                result["description"] = cap_entry.get().strip() or description
                self._hide_overlay()
                event.set()

            def _draft():
                player["stop"]()
                self._active_player = None
                result["choice"] = "draft"
                result["title"] = title_entry.get().strip() or title
                result["description"] = cap_entry.get().strip() or description
                self._hide_overlay()
                event.set()

            def _skip():
                player["stop"]()
                self._active_player = None
                result["choice"] = "skip"
                self._hide_overlay()
                event.set()

            looks_good_btn = ctk.CTkButton(btn_row, text="Looks Good", width=120, fg_color="#1D8CD7",
                           font=ctk.CTkFont(size=13, weight="bold"),
                           command=_approve)
            looks_good_btn.pack(side="left", padx=6)
            ctk.CTkButton(btn_row, text="Save for Later", width=100, fg_color="#555",
                           command=_draft).pack(side="left", padx=6)
            ctk.CTkButton(btn_row, text=skip_label, width=90, fg_color="#555",
                           text_color="#c0392b", command=_skip).pack(side="left", padx=6)

            ctk.CTkLabel(scroll, text="Edit title, caption, or style — Re-burn to update overlay  ·  Save as Default to keep style for future clips",
                          text_color="#666", font=ctk.CTkFont(size=11)).pack(pady=(0, 8))

        self.after(0, _show)
        self._wait_or_cancel(event)

        if result["choice"] == "reburn":
            # Re-burn with new title and/or style, then show preview again
            new_title = result["title"]
            new_desc = result["description"]

            # Apply per-clip style overrides to clipper globals for re-burn
            style = result.get("style")
            if style:
                clipper.TITLE_COLOR_THEME = style["title_theme"]
                clipper.CAPTION_COLOR_THEME = style["cap_theme"]
                clipper.TITLE_Y_PERCENT = float(style["title_y"])
                clipper.CAPTION_Y_PERCENT = float(style["cap_y"])

            self.after(0, self._show_working, "Re-burning", new_title[:40])
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            ass_path = clipper.RAW_DIR / f"{stamp}.ass"
            clipper.make_ass(whisper_result, ass_path)

            new_output_name = clipper.safe_filename(new_title, clip_date)
            new_output_path = clipper.unique_output_path(clipper.COMPLETED_DIR, new_output_name)
            clipper.burn_captions(video_path, ass_path, new_title, new_output_path)
            ass_path.unlink(missing_ok=True)

            # Migrate metadata and delete old output if filename changed
            if output_path != new_output_path:
                meta = self._load_video_meta()
                old_meta = meta.pop(output_path.name, {})
                old_meta.update(title=new_title, caption=new_desc,
                                raw_path=str(video_path))
                meta[new_output_path.name] = old_meta
                self._save_video_meta(meta)
                if output_path.exists():
                    output_path.unlink(missing_ok=True)

            self.after(0, self._hide_working)
            print(f"  Re-burned with title: {new_title}")

            # Recurse — show preview again with new file
            return self._preview_and_approve(new_output_path, new_title, new_desc,
                                              video_path, whisper_result, clip_date)

        return (result["choice"], result["title"], result["description"], result["output_path"])


if __name__ == "__main__":
    app = App()
    app.mainloop()
