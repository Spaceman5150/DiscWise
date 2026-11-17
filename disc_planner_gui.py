#!/usr/bin/env python3
# DiscWise Audio Archive Manager
# Copyright (C) 2026 Spaceman
#
# This program is free software: you can redistribute it and/or modify it under the terms of the
# GNU General Public License as published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version. It is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. See the GNU General Public License for more details:
# https://www.gnu.org/licenses/gpl-3.0.html
"""
Disc Planner -- desktop GUI

A point-and-click front end for disc_planner.py. Lets you add one or
more music folders, mark a folder as belonging to a single artist (for
folders like "Extra Van Halen all FLAC" that sit outside your normal
Artist/Album layout), run the same scan/pack/write logic as the
command-line tool, and then search the resulting database right here
in the same window -- no separate DB Browser needed.

Requires disc_planner.py to sit in the same folder as this file.

Run it with:
    python3 disc_planner_gui.py
"""

import io
import json
import os
import sys
import traceback
from datetime import datetime

# When launched from the desktop icon there is no terminal, so a startup
# failure is invisible. Write any uncaught error to a log file next to the
# scripts so there is always something to look at.
def _user_data_dir():
    """Where DiscWise keeps its settings and logs. When installed under /usr/share the program folder is
    read-only, so everything the program writes goes in the person's own folder instead. When run straight
    from a folder you can write to (the way it was before installing), that folder is still used, so an
    existing setup keeps its settings."""
    here = os.path.dirname(os.path.abspath(__file__))
    if os.access(here, os.W_OK) and not here.startswith(("/usr/", "/opt/")):
        return here
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    path = os.path.join(base, "discwise")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        path = here
    return path


DATA_DIR = _user_data_dir()
_CRASH_LOG = os.path.join(DATA_DIR, "disc_wise_error.log")


def _log_uncaught(exc_type, exc, tb):
    try:
        with open(_CRASH_LOG, "a") as fh:
            fh.write(f"\n--- {datetime.now():%Y-%m-%d %H:%M:%S} --- python {sys.version.split()[0]} "
                     f"({sys.executable})\n")
            fh.write("".join(traceback.format_exception(exc_type, exc, tb)))
    except OSError:
        pass
    sys.__excepthook__(exc_type, exc, tb)


sys.excepthook = _log_uncaught

# Startup speed: with the ibus keyboard input method active, Tk waits on it every time it creates a
# widget (about 0.1 s each, which made this window take 15-20 seconds to open). Turning the input method
# off just while the main window is created brings that down to a couple of seconds. It is put back right
# after, so programs DiscWise starts (the file manager, K3b) see the normal setting.
_OLD_XMODIFIERS = os.environ.get("XMODIFIERS")
if sys.platform.startswith("linux"):
    os.environ["XMODIFIERS"] = "@im=none"


def _restore_input_method():
    if _OLD_XMODIFIERS is None:
        os.environ.pop("XMODIFIERS", None)
    else:
        os.environ["XMODIFIERS"] = _OLD_XMODIFIERS


try:
    from PIL import Image, ImageColor, ImageDraw, ImageTk
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False
import math
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import contextlib
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox, scrolledtext, simpledialog

try:
    from ttkthemes import ThemedTk
    HAVE_TTKTHEMES = True
except ImportError:
    HAVE_TTKTHEMES = False

# disc_planner.py must live next to this file.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import disc_planner as core  # noqa: E402
import tag_cleaner  # noqa: E402
import art_embedder  # noqa: E402
import art_finder  # noqa: E402  (finds missing cover art on the internet)
import flac_verifier  # noqa: E402
try:
    import file_converter  # noqa: E402  (the File Converter tab; lives next to this file too)
except ImportError:
    file_converter = None

STATE_FILE = os.path.join(DATA_DIR, "disc_planner_gui_state.json")


class RoundedButton(tk.Canvas):
    """
    A push-button with actually-rounded corners, since ttk/Tk has no
    native border-radius. Drop-in enough for this app's needs: supports
    text, command, and .config(state="normal"/"disabled"), and sizes
    itself to fit its text automatically.
    """

    def __init__(self, parent, text="", command=None, radius=8, height=30,
                 padx=16, bg=None, hover_bg=None, active_bg=None, fg="#ffffff",
                 disabled_bg=None, disabled_fg=None, font=("", 11), **kwargs):
        self._bg = bg or ACCENT
        self._hover_bg = hover_bg or ACCENT_LIGHT
        self._active_bg = active_bg or ACCENT_DARK
        self._fg = fg
        self._disabled_bg = disabled_bg or BG_PANEL
        self._disabled_fg = disabled_fg or FG_MUTED
        self._text = text
        self._command = command
        self._radius = radius
        self._font = tkfont.Font(font=font)
        self._state = kwargs.pop("state", "normal")

        text_w = self._font.measure(text)
        width = text_w + padx * 2
        parent_bg = kwargs.pop("parent_bg", None)
        super().__init__(parent, width=width, height=height, highlightthickness=0,
                          background=parent_bg or _canvas_bg_for(parent), **kwargs)

        self._shape = None
        self._label = None
        self._hovering = False
        self._pressed = False
        self._image_cache = {}
        self._draw()

        self.bind("<Enter>", self._on_enter, add="+")
        self.bind("<Leave>", self._on_leave, add="+")
        self.bind("<ButtonPress-1>", self._on_press, add="+")
        self.bind("<ButtonRelease-1>", self._on_release, add="+")

    def _current_bg(self):
        if self._state == "disabled":
            return self._disabled_bg
        if self._pressed:
            return self._active_bg
        if self._hovering:
            return self._hover_bg
        return self._bg

    def _current_fg(self):
        return self._disabled_fg if self._state == "disabled" else self._fg

    def _rounded_photo(self, w, h, r, fill):
        """A smooth, anti-aliased rounded rectangle on the button's own background colour:
        drawn at 4x size and shrunk, so all four corners are evenly round."""
        key = (w, h, r, fill)
        photo = self._image_cache.get(key)
        if photo is None:
            scale = 4
            mask = Image.new("L", (w * scale, h * scale), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                [0, 0, w * scale - 1, h * scale - 1], radius=r * scale, fill=255)
            mask = mask.resize((w, h), Image.LANCZOS)
            base = Image.new("RGB", (w, h), ImageColor.getrgb(self.cget("background")))
            base.paste(Image.new("RGB", (w, h), ImageColor.getrgb(fill)), (0, 0), mask)
            photo = ImageTk.PhotoImage(base)
            self._image_cache[key] = photo
        return photo

    def _draw(self):
        self.delete("all")
        w, h, r = int(self["width"]), int(self["height"]), self._radius
        color = self._current_bg()
        if HAVE_PIL:
            self._shape = self.create_image(0, 0, image=self._rounded_photo(w, h, r, color), anchor="nw")
        else:
            # Fallback without Pillow: the standard smoothed-polygon rounded rectangle, inset by a
            # pixel so the right and bottom edges aren't clipped by the canvas border.
            x1, y1, x2, y2 = 1, 1, w - 1, h - 1
            points = [x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y1 + r,
                      x2, y2 - r, x2, y2 - r, x2, y2, x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2,
                      x1, y2, x1, y2 - r, x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1]
            self._shape = self.create_polygon(points, smooth=True, fill=color, outline=color)
        self._label = self.create_text(w / 2, h / 2, text=self._text, fill=self._current_fg(), font=self._font)
        cursor = "hand2" if self._state != "disabled" else "arrow"
        self.config(cursor=cursor)

    def _on_enter(self, event=None):
        if self._state != "disabled":
            self._hovering = True
            self._draw()

    def _on_leave(self, event=None):
        self._hovering = False
        self._pressed = False
        self._draw()

    def _on_press(self, event=None):
        if self._state != "disabled":
            self._pressed = True
            self._draw()

    def _on_release(self, event=None):
        if self._state != "disabled":
            was_pressed = self._pressed
            self._pressed = False
            self._draw()
            if was_pressed and self._command:
                self._command()

    def config(self, **kwargs):
        if "state" in kwargs:
            self._state = kwargs.pop("state")
            self._draw()
        if "text" in kwargs:
            self._text = kwargs.pop("text")
            text_w = self._font.measure(self._text)
            new_width = text_w + 32
            super().config(width=new_width)
            self._draw()
        if kwargs:
            super().config(**kwargs)

    configure = config

    def set_width(self, pixels):
        """Make the button exactly this wide (the text stays centered), so neighbours can be matched."""
        super().config(width=int(pixels))
        self._draw()

    def __setitem__(self, key, value):
        self.config(**{key: value})

    def __getitem__(self, key):
        if key == "state":
            return self._state
        return super().__getitem__(key)


def _canvas_bg_for(widget):
    """Best-effort background color to paint a Canvas's own corners with,
    matching whatever panel it's sitting in."""
    try:
        style_name = widget.cget("style")
    except tk.TclError:
        style_name = ""
    if "Panel" in str(style_name):
        return BG_PANEL
    try:
        if widget.winfo_class() == "TLabelframe":
            return BG_PANEL
    except tk.TclError:
        pass
    return BG_MAIN


class Tooltip:
    """A small hover tooltip for any widget -- shows `text` near the
    cursor shortly after hovering, hides on leave or click."""

    def __init__(self, widget, text, delay=450):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.tip_window = None
        self._after_id = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, event=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after_id:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self):
        if self.tip_window or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        try:
            tw.attributes("-topmost", True)
        except tk.TclError:
            pass
        label = tk.Label(
            tw, text=self.text, justify="left", background="#333333", foreground="white",
            relief="solid", borderwidth=1, font=("", 9), padx=6, pady=3, wraplength=320,
        )
        label.pack()

    def _hide(self, event=None):
        self._cancel()
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None


_BULB_ROWS = {}


def bulb_pixel_rows(size, background_hex):
    """
    A full-color light bulb as rows of "#rrggbb" pixels, computed by hand with no imaging library at
    all (so it works even when Pillow isn't installed). Edges are smoothed by blending with
    `background_hex`, the color of whatever the bulb sits on.
    """
    import math

    def hex_to_rgb(h):
        h = h.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

    def near_segment(px, py, ax, ay, bx, by, radius):
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        t = 0.0 if length_sq == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
        return math.hypot(px - (ax + t * dx), py - (ay + t * dy)) <= radius

    def in_polygon(px, py, pts):
        inside = False
        j = len(pts) - 1
        for i in range(len(pts)):
            xi, yi = pts[i]
            xj, yj = pts[j]
            if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
        return inside

    rays = ((64, 4, 64, 16), (22, 20, 30, 29), (106, 20, 98, 29), (6, 56, 18, 56), (122, 56, 110, 56))
    filament = ((54, 92, 54, 62), (54, 62, 64, 50), (64, 50, 74, 62), (74, 62, 74, 92))
    neck = ((44, 76), (84, 76), (78, 96), (50, 96))

    # Top layer first: the first test that matches decides the color.
    layers = (
        (lambda x, y: 54 <= x <= 74 and 116 <= y <= 124, (95, 95, 110)),                      # base tip
        (lambda x, y: 48 <= x <= 80 and 106 <= y <= 114, (120, 120, 135)),                    # lower ring
        (lambda x, y: 48 <= x <= 80 and 96 <= y <= 104, (150, 150, 165)),                     # upper ring
        (lambda x, y: any(near_segment(x, y, *s, 2.2) for s in filament), (230, 150, 20)),   # filament
        (lambda x, y: (x - 49) ** 2 / 81 + (y - 42) ** 2 / 100 <= 1, (255, 240, 160)),       # shine
        (lambda x, y: (x - 64) ** 2 + (y - 56) ** 2 <= 34 ** 2 or in_polygon(x, y, neck), (255, 217, 64)),  # glass
        (lambda x, y: any(near_segment(x, y, *r, 3.2) for r in rays), (255, 214, 64)),       # rays
    )
    bg = hex_to_rgb(background_hex)
    sub_n = 3
    rows = []
    for row in range(size):
        line = []
        for col in range(size):
            total = [0, 0, 0]
            for sy in range(sub_n):
                for sx in range(sub_n):
                    x = (col + (sx + 0.5) / sub_n) * 128 / size
                    y = (row + (sy + 0.5) / sub_n) * 128 / size
                    color = bg
                    for test, rgb in layers:
                        if test(x, y):
                            color = rgb
                            break
                    for k in range(3):
                        total[k] += color[k]
            n = sub_n * sub_n
            line.append("#%02x%02x%02x" % tuple(int(t / n) for t in total))
        rows.append(line)
    return rows


def emoji_picture(character, size):
    """
    The real color emoji `character` as a picture (PIL image), drawn with the system's color-emoji
    font, because Tk itself can't paint color emoji on Linux (it comes out as a one-color blob).
    Raises OSError if no color-emoji font can be found.
    """
    from PIL import Image, ImageDraw, ImageFont
    candidates = [
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/google-noto-emoji/NotoColorEmoji.ttf",
        "/usr/share/fonts/google-noto-color-emoji/NotoColorEmoji.ttf",
        os.path.expanduser("~/.local/share/fonts/NotoColorEmoji.ttf"),
    ]
    try:
        found = subprocess.run(["fc-match", "-f", "%{file}", "Noto Color Emoji"], capture_output=True,
                               text=True, timeout=5).stdout.strip()
        if found:
            candidates.append(found)
    except (OSError, subprocess.SubprocessError):
        pass
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            font = ImageFont.truetype(path, 109)  # Noto's color glyphs come in this one fixed size
            canvas = Image.new("RGBA", (160, 160), (0, 0, 0, 0))
            ImageDraw.Draw(canvas).text((10, 10), character, font=font, embedded_color=True)
            box = canvas.getbbox()
            if not box:
                continue
            glyph = canvas.crop(box)
            side = max(glyph.size)
            square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            square.paste(glyph, ((side - glyph.width) // 2, (side - glyph.height) // 2))
            return square.resize((size, size), Image.LANCZOS)
        except (OSError, ValueError):
            continue
    raise OSError("no color emoji font found")


def draw_light_bulb(size):
    """
    A small full-color light bulb (PIL image, transparent background). Drawn here rather than typed
    as an emoji because Tk on Linux often can't draw color emoji -- it comes out as a one-color blob.
    """
    from PIL import Image, ImageDraw
    s = 8  # draw 8x bigger, then shrink, so the edges are smooth
    img = Image.new("RGBA", (128 * s, 128 * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def sc(*values):
        return [int(v * s) for v in values]

    ray = (255, 214, 64, 255)
    for x1, y1, x2, y2 in ((64, 4, 64, 16), (22, 20, 30, 29), (106, 20, 98, 29),
                           (6, 56, 18, 56), (122, 56, 110, 56)):
        d.line(sc(x1, y1, x2, y2), fill=ray, width=6 * s)
        for x, y in ((x1, y1), (x2, y2)):
            d.ellipse(sc(x - 3, y - 3, x + 3, y + 3), fill=ray)
    glass = (255, 217, 64, 255)
    d.ellipse(sc(30, 22, 98, 90), fill=glass)
    d.polygon(sc(44, 76, 84, 76, 78, 96, 50, 96), fill=glass)
    d.ellipse(sc(40, 32, 58, 52), fill=(255, 240, 160, 255))
    d.line(sc(54, 92, 54, 62, 64, 50, 74, 62, 74, 92), fill=(230, 150, 20, 255), width=4 * s, joint="curve")
    d.rounded_rectangle(sc(48, 96, 80, 104), radius=3 * s, fill=(150, 150, 165, 255))
    d.rounded_rectangle(sc(48, 106, 80, 114), radius=3 * s, fill=(120, 120, 135, 255))
    d.rounded_rectangle(sc(54, 116, 74, 124), radius=4 * s, fill=(95, 95, 110, 255))
    return img.resize((size, size), Image.LANCZOS)


# How big the light bulb in a tip line is, as a multiple of one line of the tip's text.
TIP_BULB_SCALE = 2.0


def add_tip_row(window, text, wraplength=900):
    """
    Adds a "tip" line to the bottom of `window`: a light bulb picture followed by `text`, in slightly
    larger type so it doesn't get skipped over. The bulb is a picture because Tk can't reliably draw
    a color emoji: the real emoji if possible, else one we draw, else one painted pixel by pixel.
    """
    tip_font = tkfont.nametofont("TkDefaultFont").copy()
    tip_font.configure(size=round(abs(int(tip_font.cget("size"))) * 1.5))
    window._tip_font = tip_font  # keep references so Tk doesn't discard them
    tip_row = ttk.Frame(window)
    tip_row.pack(anchor="w", fill="x", padx=10, pady=(0, 8))
    bulb_label = tk.Label(tip_row, background=BG_MAIN, borderwidth=0)
    bulb_label.pack(side="left", anchor="n", padx=(0, 8))
    bulb_px = int(tip_font.metrics("linespace") * TIP_BULB_SCALE)
    try:
        if not HAVE_PIL:
            raise ImportError("Pillow isn't fully installed")
        try:
            bulb_picture = emoji_picture("\U0001F4A1", bulb_px)   # the real emoji, if the font exists
        except OSError:
            bulb_picture = draw_light_bulb(bulb_px)                # otherwise our own drawing
        window._tip_bulb_image = ImageTk.PhotoImage(bulb_picture)
    except (ImportError, tk.TclError, OSError, ValueError):
        if bulb_px not in _BULB_ROWS:  # takes a moment to work out, so only do it once per size
            _BULB_ROWS[bulb_px] = bulb_pixel_rows(bulb_px, BG_MAIN)
        window._tip_bulb_image = tk.PhotoImage(width=bulb_px, height=bulb_px)
        window._tip_bulb_image.put(" ".join("{" + " ".join(r) + "}" for r in _BULB_ROWS[bulb_px]))
    bulb_label.config(image=window._tip_bulb_image)
    tip_label = ttk.Label(tip_row, text=text, foreground=FG_MUTED, font=tip_font,
                          wraplength=wraplength, justify="left")
    tip_label.pack(side="left", anchor="n", fill="x", expand=True)

    def _rewrap(event):
        # Let the text use the full width of the window: whatever is left after the bulb.
        room = event.width - bulb_label.winfo_reqwidth() - 24
        if room > 200 and abs(room - int(tip_label.cget("wraplength"))) > 4:
            tip_label.configure(wraplength=room)
    tip_row.bind("<Configure>", _rewrap)
    return tip_row


class WatermarkLog(tk.Frame):
    """
    A scrolling log with your logo faintly behind the text. (A normal Tk text box can't have a
    see-through background, so this draws each line on a canvas, with the logo underneath.)
    Add lines with add_line(); right-click for Copy / Clear. Follows new lines as they arrive, unless
    you've scrolled up to read something -- then it stays where you put it.
    """
    WATERMARK_OPACITY = 0.12   # 0 = invisible, 1 = solid; around 0.12 is a faint ghost behind the text
    MAX_LINES = 4000           # the oldest lines are dropped past this so a huge run stays quick

    def __init__(self, master, background, foreground, border, font, logo_path=None):
        super().__init__(master, background=border, highlightthickness=0, borderwidth=0)
        self._bg, self._fg = background, foreground
        self._font = font
        self._line_h = font.metrics("linespace") + 2
        self._lines = []      # (canvas item id, text), oldest first
        self._logo_path = logo_path if (logo_path and os.path.exists(logo_path) and HAVE_PIL) else None
        self._wm_image = None
        self._wm_item = None
        self._wm_size = 0

        self.canvas = tk.Canvas(self, background=background, highlightthickness=0, borderwidth=0)
        self.scrollbar = tk.Scrollbar(self, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side="right", fill="y", padx=(0, 1), pady=1)
        self.canvas.pack(side="left", fill="both", expand=True, padx=(1, 0), pady=1)

        self.canvas.bind("<Configure>", self._on_resize)
        for sequence, step in (("<MouseWheel>", None), ("<Button-4>", -3), ("<Button-5>", 3)):
            self.canvas.bind(sequence, lambda e, s=step: self._wheel(e, s))
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Copy log", command=self.copy_all)
        menu.add_command(label="Clear log", command=self.clear)
        self.canvas.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

    # ---- text
    def add_line(self, text):
        follow = self.canvas.yview()[1] >= 0.999 or not self._lines
        y = 4 + len(self._lines) * self._line_h
        item = self.canvas.create_text(8, y, text=text, anchor="nw", fill=self._fg, font=self._font)
        self._lines.append((item, text))
        if len(self._lines) > self.MAX_LINES:
            drop = len(self._lines) - self.MAX_LINES
            for old_item, _ in self._lines[:drop]:
                self.canvas.delete(old_item)
            self._lines = self._lines[drop:]
            for index, (line_item, _) in enumerate(self._lines):
                self.canvas.coords(line_item, 8, 4 + index * self._line_h)
        self._update_region()
        if follow:
            self.canvas.yview_moveto(1.0)
        self._place_watermark()

    def add_lines(self, lines):
        for line in lines:
            self.add_line(line)

    def clear(self):
        for item, _ in self._lines:
            self.canvas.delete(item)
        self._lines = []
        self._update_region()
        self.canvas.yview_moveto(0)
        self._place_watermark()

    def all_text(self):
        return "\n".join(text for _, text in self._lines)

    def copy_all(self):
        self.clipboard_clear()
        self.clipboard_append(self.all_text())

    # ---- scrolling and layout
    def _update_region(self):
        height = max(self.canvas.winfo_height(), 8 + len(self._lines) * self._line_h)
        self.canvas.configure(scrollregion=(0, 0, 1, height))

    def _wheel(self, event, step):
        if step is None:  # Windows/macOS style wheel
            step = -1 if event.delta > 0 else 1
            step *= 3
        self.canvas.yview_scroll(step, "units")
        self._place_watermark()

    def _on_resize(self, _event=None):
        self._update_region()
        self._draw_watermark()
        self._place_watermark()

    # ---- watermark
    def _draw_watermark(self):
        if not self._logo_path:
            return
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        size = int(min(width, height) * 0.8)
        if size < 40 or abs(size - self._wm_size) < 6:
            return
        try:
            logo = Image.open(self._logo_path).convert("RGBA").resize((size, size), Image.LANCZOS)
            faded = logo.copy()
            faded.putalpha(logo.getchannel("A").point(lambda a: int(a * self.WATERMARK_OPACITY)))
            backdrop = Image.new("RGBA", (size, size), ImageColor.getrgb(self._bg) + (255,))
            self._wm_image = ImageTk.PhotoImage(Image.alpha_composite(backdrop, faded).convert("RGB"))
        except (OSError, ValueError, tk.TclError):
            self._logo_path = None
            return
        self._wm_size = size
        if self._wm_item is None:
            self._wm_item = self.canvas.create_image(0, 0, image=self._wm_image, anchor="center")
        else:
            self.canvas.itemconfigure(self._wm_item, image=self._wm_image)
        self.canvas.tag_lower(self._wm_item)

    def _place_watermark(self):
        """Keeps the logo fixed in the middle of the visible area while the text scrolls over it."""
        if self._wm_item is not None:
            self.canvas.coords(self._wm_item, self.canvas.canvasx(self.canvas.winfo_width() / 2),
                               self.canvas.canvasy(self.canvas.winfo_height() / 2))
            self.canvas.tag_lower(self._wm_item)


class CellTooltip:
    """
    A hover tooltip for individual cells of a Treeview table: after hovering over a cell in
    `column` for a moment, shows whatever `text_for(cell_value)` returns (nothing if it returns
    None/empty). Moving to another cell, leaving the table or clicking hides it.
    """

    def __init__(self, tree, column, text_for, delay=450):
        self.tree = tree
        self.column = column
        self.text_for = text_for
        self.delay = delay
        self.tip_window = None
        self._after_id = None
        self._cell = None
        tree.bind("<Motion>", self._moved, add="+")
        tree.bind("<Leave>", self._hide, add="+")
        tree.bind("<ButtonPress>", self._hide, add="+")
        tree.bind("<MouseWheel>", self._hide, add="+")

    def _moved(self, event):
        row = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        cell = (row, col) if row and col == self._column_id() else None
        if cell == self._cell:
            return
        self._hide()
        self._cell = cell
        if cell is None:
            return
        text = self.text_for(self.tree.set(row, self.column))
        if text:
            self._after_id = self.tree.after(
                self.delay, lambda: self._show(text, event.x_root, event.y_root))

    def _column_id(self):
        return "#%d" % (list(self.tree["columns"]).index(self.column) + 1)

    def _show(self, text, x_root, y_root):
        if self.tip_window:
            return
        self.tip_window = tw = tk.Toplevel(self.tree)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x_root + 14}+{y_root + 18}")
        try:
            tw.attributes("-topmost", True)
        except tk.TclError:
            pass
        tk.Label(tw, text=text, justify="left", background="#333333", foreground="white",
                 relief="solid", borderwidth=1, font=("", 9), padx=6, pady=3, wraplength=320).pack()

    def _hide(self, event=None):
        if self._after_id:
            self.tree.after_cancel(self._after_id)
            self._after_id = None
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None
        if event is not None:
            self._cell = None


def enable_column_grab(tree, zone=8, fixed=()):
    """Makes the column dividers in a table's heading row easy to grab: anywhere within `zone` pixels
    of a divider starts a drag (the built-in grab area is only a pixel or two wide and invisible in
    this theme), and the mouse pointer turns into a sideways arrow to show where to grab. Columns
    named in `fixed` keep their width."""
    drag = {}

    def edges():
        cols = list(tree["columns"])
        widths = [int(tree.column(c, "width")) for c in cols]
        total = sum(widths)
        offset = tree.xview()[0] * total
        out, run = [], 0
        for col, width in zip(cols, widths):
            run += width
            out.append((col, run - offset))
        return out

    def near(event):
        if tree.identify_region(event.x, 5) not in ("heading", "separator") or event.y > 40:
            return None
        for col, edge in edges():
            if abs(event.x - edge) <= zone and col not in fixed:
                return col
        return None

    def pressed(event):
        col = near(event)
        if col is None:
            return None
        drag.update(col=col, x=event.x, width=int(tree.column(col, "width")))
        return "break"

    def moved(event):
        if drag:
            width = max(int(tree.column(drag["col"], "minwidth")), drag["width"] + event.x - drag["x"])
            tree.column(drag["col"], width=width)
            return "break"
        tree.configure(cursor="sb_h_double_arrow" if near(event) else "")
        return None

    def released(event):
        if drag:
            drag.clear()
            return "break"
        return None

    tree.bind("<ButtonPress-1>", pressed, add="+")
    tree.bind("<B1-Motion>", moved, add="+")
    tree.bind("<Motion>", moved, add="+")
    tree.bind("<ButtonRelease-1>", released, add="+")


class QueueWriter:
    """A file-like object that pushes writes into a thread-safe queue,
    so print() output from the background scan thread can be shown
    live in the GUI's log box on the main thread."""

    def __init__(self, q):
        self.q = q

    def write(self, text):
        if text:
            self.q.put(("log", text))

    def flush(self):
        pass


def show_text_dialog(parent, title, text, width=760, height=420):
    """
    Like messagebox.showwarning, but the text can be selected and copied (Ctrl+C, right-click, or the
    Copy All button), so a long list of problems can be pasted into a chat or a note.
    Waits until it's closed.
    """
    top = parent.winfo_toplevel()
    win = tk.Toplevel(top)
    win.title(title)
    win.configure(background=BG_MAIN)
    win.transient(top)
    win.geometry(f"{width}x{height}")
    win.minsize(420, 220)

    body = ttk.Frame(win)
    body.pack(fill="both", expand=True, padx=12, pady=(12, 6))
    box = tk.Text(body, wrap="word", background=BG_INPUT, foreground=FG_TEXT, relief="flat", borderwidth=1,
                  highlightthickness=1, highlightbackground=BORDER, font=("", 11), padx=8, pady=6,
                  insertbackground=FG_TEXT)
    scroll = tk.Scrollbar(body, command=box.yview)
    box.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right", fill="y")
    box.pack(side="left", fill="both", expand=True)
    box.insert("1.0", text)
    box.configure(state="disabled")      # read-only, but still selectable and copyable
    box.focus_set()

    def copy_all():
        win.clipboard_clear()
        win.clipboard_append(text)
        copy_btn.config(text="Copied")

    def copy_selection():
        try:
            chosen = box.get("sel.first", "sel.last")
        except tk.TclError:
            chosen = text
        win.clipboard_clear()
        win.clipboard_append(chosen)

    menu = tk.Menu(win, tearoff=0)
    menu.add_command(label="Copy", command=copy_selection)
    menu.add_command(label="Select All", command=lambda: box.tag_add("sel", "1.0", "end"))
    box.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))
    box.bind("<Control-a>", lambda e: (box.tag_add("sel", "1.0", "end"), "break")[1])

    row = ttk.Frame(win)
    row.pack(fill="x", padx=12, pady=(0, 12))
    copy_btn = RoundedButton(row, text="Copy All", command=copy_all)
    copy_btn.pack(side="left")
    RoundedButton(row, text="OK", command=win.destroy).pack(side="right")
    win.protocol("WM_DELETE_WINDOW", win.destroy)
    win.update_idletasks()
    try:
        win.grab_set()
    except tk.TclError:
        pass
    win.wait_window()


class FolderOverrideDialog(tk.Toplevel):
    """Small popup for telling DiscWise how the music inside one folder is organized."""

    def __init__(self, parent, current=None):
        super().__init__(parent)
        self.title("Folder layout")
        self.resizable(False, False)
        self.result = None
        self.configure(background=BG_MAIN)
        self.grab_set()

        current = current or {"mode": "normal", "artist": ""}
        # Older saved settings may say "nested"; that has been identical to "flat" for a long time.
        start_mode = "flat" if current["mode"] in ("flat", "nested") else "normal"

        ttk.Label(self, text="What's inside this folder?", font=("", 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(10, 6)
        )

        self.mode_var = tk.StringVar(value=start_mode)
        rb_normal = ttk.Radiobutton(
            self, text="A folder for each artist (the usual)", variable=self.mode_var,
            value="normal", command=self._sync_entry_state
        )
        rb_normal.grid(row=1, column=0, columnspan=2, sticky="w", padx=20)
        ttk.Label(self, text="Looks like:   Music / Pink Floyd / The Wall", foreground=FG_MUTED).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=(44, 20), pady=(0, 6))

        rb_flat = ttk.Radiobutton(
            self, text="Just one artist's music", variable=self.mode_var,
            value="flat", command=self._sync_entry_state
        )
        rb_flat.grid(row=3, column=0, columnspan=2, sticky="w", padx=20)
        ttk.Label(self, text="Looks like:   Extra Van Halen / Live in Tokyo\n"
                             "The whole folder is one artist's, however it's sorted inside.",
                  foreground=FG_MUTED, justify="left").grid(
            row=4, column=0, columnspan=2, sticky="w", padx=(44, 20), pady=(0, 4))

        ttk.Label(self, text="Whose music is it?").grid(row=5, column=0, sticky="e", padx=(20, 4), pady=(8, 2))
        self.artist_entry = ttk.Entry(self, width=30)
        self.artist_entry.insert(0, current.get("artist", ""))
        self.artist_entry.grid(row=5, column=1, sticky="w", padx=(0, 20), pady=(8, 2))
        ttk.Label(self, text="This is the artist name DiscWise puts on your checklist and in searches.",
                  foreground=FG_MUTED).grid(row=6, column=0, columnspan=2, sticky="w", padx=20, pady=(0, 6))

        ttk.Label(self, text="Not sure? Leave it on the first choice. If artist names look wrong\n"
                             "after a scan, come back here and set this folder.",
                  foreground=FG_MUTED, justify="left").grid(
            row=7, column=0, columnspan=2, sticky="w", padx=20, pady=(0, 6))

        btns = ttk.Frame(self)
        btns.grid(row=8, column=0, columnspan=2, pady=(0, 10))
        RoundedButton(btns, text="OK", command=self._ok).pack(side="left", padx=5)
        RoundedButton(btns, text="Cancel", command=self.destroy).pack(side="left", padx=5)

        self._sync_entry_state()
        self.transient(parent)
        self.wait_window(self)

    def _sync_entry_state(self):
        state = "normal" if self.mode_var.get() == "flat" else "disabled"
        self.artist_entry.config(state=state)

    def _ok(self):
        mode = self.mode_var.get()
        artist = self.artist_entry.get().strip()
        if mode == "flat" and not artist:
            messagebox.showerror("Missing artist name", "Type the artist's name for this folder.", parent=self)
            return
        self.result = {"mode": mode, "artist": artist if mode == "flat" else ""}
        self.destroy()


# Palette pulled from the app icon (DiscWise_Audio_Archive_Manager.png).
BG_MAIN = "#0d0a1a"       # outer window background -- near-black purple
BG_PANEL = "#1c1630"      # Options / Output files panel background -- lighter
BG_INPUT = "#241c3d"      # entries, listbox, text areas, treeview rows
FG_TEXT = "#e8e0ff"       # primary text -- light lavender-white
FG_MUTED = "#a89cc8"      # helper/secondary text
DONE_GREEN = "#5fe08a"    # the "done" green: file converter status and verified rows in the Burn Tracker
ACCENT = "#7639f0"        # buttons, selected tab, primary accent
ACCENT_LIGHT = "#bf8bfb"  # hover / highlight
ACCENT_DARK = "#4115be"   # pressed / border accent
BORDER = "#3a3060"        # subtle borders
INFO_ICON = "#ffd54a"     # the little (i) circles -- bright so they catch the eye


class DiscSearchWindow(tk.Toplevel):
    """
    "Search Discs" popup: looks things up in the packing-list database and shows
    which disc each match is on -- e.g. search a song title to see every artist's
    version and its disc number.
    """

    COLUMNS = ("artist", "album", "disc", "status", "type", "track", "song")
    HEADINGS = {"artist": "Artist", "album": "Album", "disc": "Disc #", "status": "Status",
                "type": "File Type", "track": "Track #", "song": "Song"}
    WIDTHS = {"artist": 170, "album": 210, "disc": 70, "status": 125, "type": 80, "track": 70, "song": 230}

    def __init__(self, parent, db_path, extra_roots=None):
        super().__init__(parent)
        self.db_path = db_path
        self.extra_roots = extra_roots      # callable -> folders to also check when searching "pending"
        self.title(f"Search Burned Discs  -  {os.path.basename(db_path)}")
        self.configure(background=BG_MAIN)
        self.geometry("1000x560")
        self.minsize(700, 320)
        self._sort_reverse = {}

        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(top, text="Search:").pack(side="left")
        self.term_entry = ttk.Entry(top, width=38)
        self.term_entry.pack(side="left", padx=(6, 6))
        self.term_entry.bind("<Return>", lambda e: self._search())
        self.scope_var = tk.StringVar(value="All")
        scope_menu = ttk.Combobox(top, textvariable=self.scope_var, state="readonly", width=10,
                                  values=["All", "Artist", "Album", "Song", "File Type", "Disc #"])
        scope_menu.pack(side="left", padx=(0, 6))
        Tooltip(scope_menu, "Limit the search to just this field, or search everything at once (artist, album, "
                            "song, file type and, for a number, the disc). \"Disc #\" lists only what's on "
                            "that one disc: type 10 to see disc 10, or leave it empty to list every disc.")
        RoundedButton(top, text="Search", command=self._search).pack(side="left", padx=4)
        self.status = ttk.Label(top, text="")
        self.status.pack(side="left", padx=12)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=10, pady=(4, 4))
        self.tree = ttk.Treeview(body, columns=self.COLUMNS, show="headings")
        for col in self.COLUMNS:
            self.tree.heading(col, text=self.HEADINGS[col], command=lambda c=col: self._sort_by(c))
            self.tree.column(col, width=self.WIDTHS[col], anchor="center")
        scroll = tk.Scrollbar(body, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        CellTooltip(self.tree, "status", lambda value: (
            "This status changes to Burned (or Burned + Verified) once you edit the "
            "checklist and update the Burn Tracker." if value == "Pending" else None))

        add_tip_row(self, "Click a column heading to sort by it, or try typing \"pending\" to list "
                         "everything not yet burned, including new music in your folders that isn't on a disc plan yet (searching an artist, album or song also checks those folders). Search by disc number in the dropdown to find out what was "
                         "on any given disc, if it gets lost or goes missing, or leave the box empty to list every disc.")

        self.term_entry.focus_set()

    def _search(self):
        term = self.term_entry.get().strip()
        for item in self.tree.get_children():
            self.tree.delete(item)
        try:
            found = core.search_library(self.db_path, term, self.scope_var.get(), always_label_discs=True)
        except sqlite3.Error as exc:
            messagebox.showerror("Search error", str(exc), parent=self)
            return
        rows = list(found["rows"])
        # Music sitting in real folders that the packing list doesn't know about yet (new downloads,
        # albums in the Waiting to Burn folder) is pending too, so look at those folders as well:
        # everything for "pending", or just the matches for any other search by name.
        scope = self.scope_var.get()
        if self.extra_roots and scope in ("All", "Artist", "Album", "Song") and term:
            try:
                is_pending = scope == "All" and term.lower() in ("pending", "unburned", "not burned")
                have = {((r[0] or "").lower(), (r[1] or "").lower()) for r in rows}
                rows += core.find_unlisted_pending(self.db_path, self.extra_roots(), skip_keys=have,
                                                   term=None if is_pending else term, scope=scope)
                rows = core.sort_search_rows(rows)    # folders on disk come in no particular order
            except OSError:
                pass
        for artist, album, disc_label, file_type, track_number, song, _path, status in rows:
            self.tree.insert("", "end", values=(artist, album, disc_label, status, file_type,
                                                 track_number or "", song))
        found = {"rows": rows}
        # Say which database this is and what's in it, so an empty result can be told apart from
        # "looking at the wrong file" or "the packing list found no music".
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                albums = conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
                tracks = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
            finally:
                conn.close()
            contents = f"{albums} albums, {tracks} songs"
        except sqlite3.Error:
            contents = "couldn't read it"
        self.status.config(text=f"{len(found['rows'])} result(s)   --   searching {self.db_path}  ({contents})")
        self._sort_reverse = {}

    def _sort_by(self, col):
        def key(value):
            # Same ordering rules as the search itself: no case, leading punctuation or spaces ignored,
            # numbers in numeric order; empty cells (like a missing track number) last.
            return (str(value).strip() == "", core.natural_sort_key(value))
        rows = [(self.tree.set(k, col), k) for k in self.tree.get_children("")]
        reverse = self._sort_reverse.get(col, False)
        rows.sort(key=lambda r: key(r[0]), reverse=reverse)
        for index, (_value, k) in enumerate(rows):
            self.tree.move(k, "", index)
        self._sort_reverse[col] = not reverse


class TagEditorWindow(tk.Toplevel):
    """
    "Edit Tags by Hand" popup for ONE album folder (FLAC or MP3 songs). The top boxes change a tag for the
    whole album at once; double-clicking the track, title or artist of a song in the list changes just that
    song. Nothing is written until Save, and the old values are logged so the cleaner's Undo brings them back.
    """
    ALBUM_FIELDS = (("album", "Album"), ("artist", "Artist"), ("albumartist", "Album artist"),
                    ("date", "Year"), ("genre", "Genre"))
    CELLS = (("tracknumber", "track"), ("title", "title"), ("artist", "artist"))
    GOLD = "#ffd54a"

    def __init__(self, parent, folder, songs, log_path, on_saved):
        super().__init__(parent)
        self.folder, self.songs, self.log_path, self.on_saved = folder, songs, log_path, on_saved
        self._busy, self._editor, self._state = False, None, None
        self.title("Edit Tags by Hand")
        self.configure(background=BG_MAIN)
        self.geometry(f"900x{max(520, min(700, self.winfo_screenheight() - 120))}")
        self.minsize(700, 460)
        self.transient(parent)

        ttk.Label(self, text=os.path.basename(folder.rstrip(os.sep)) or folder, font=("", 13, "bold"),
                  wraplength=860, justify="left").pack(fill="x", padx=12, pady=(12, 0))
        self.hint = ttk.Label(
            self, foreground=FG_MUTED, wraplength=860, justify="left",
            text="The boxes below change a tag for every song in the album. To change one song, double-click its "
                 "track number, title, artist or file name in the list. Nothing is written until you press Save Changes.")
        self.hint.pack(fill="x", padx=12, pady=(2, 8))

        box = ttk.LabelFrame(self, text="Whole album")
        box.pack(fill="x", padx=12, pady=(0, 8))
        grid = ttk.Frame(box, style="Panel.TFrame")
        grid.pack(fill="x", padx=8, pady=8)
        self.album_entries, self.album_initial = {}, {}
        for col, (key, label) in enumerate(self.ALBUM_FIELDS):
            values = {song["tags"][key] for song in songs}
            common = values.pop() if len(values) == 1 else ""
            self.album_initial[key] = common
            ttk.Label(grid, text=label + ":", style="Panel.TLabel").grid(row=0, column=col, sticky="w", padx=(0, 8))
            entry = ttk.Entry(grid, width=24 if key in ("album", "artist", "albumartist") else 12)
            entry.grid(row=1, column=col, sticky="ew", padx=(0, 8))
            entry.insert(0, common)
            self.album_entries[key] = entry
            if not common and len(values) + 1 > 1:
                ttk.Label(grid, text="(songs differ)", style="Panel.TLabel", foreground=FG_MUTED).grid(
                    row=2, column=col, sticky="w")
            grid.columnconfigure(col, weight=1 if key in ("album", "artist", "albumartist") else 0)
        Tooltip(self.album_entries["album"], "Typed here, it becomes the album tag of every song. Boxes you "
                                             "don't touch are left alone. A box that shows nothing means the "
                                             "songs differ, and it stays that way unless you type something.")

        self.status = ttk.Label(self, text="", foreground=self.GOLD, font=("", 12, "bold"), wraplength=860,
                                justify="left")
        self.status.pack(fill="x", padx=12, pady=(0, 4))

        btns = ttk.Frame(self)
        btns.pack(side="bottom", fill="x", padx=12, pady=(0, 12))
        self.save_btn = RoundedButton(btns, text="Save Changes", command=self._save)
        self.save_btn.pack(side="left")
        Tooltip(self.save_btn, "Writes the changes into the songs. Each song is rewritten through a temporary "
                               "copy, so a problem partway can't damage a file, and the old values are saved "
                               "so Undo Last Cleanup on the Tag & Folder Cleaner tab can put them back.")
        self.close_btn = RoundedButton(btns, text="Close", command=self._close)
        self.close_btn.pack(side="left", padx=8)
        self.notes_btn = RoundedButton(btns, text="Find Take Notes...", command=self._find_notes)
        self.notes_btn.pack(side="left", padx=(0, 8))
        Tooltip(self.notes_btn, "Looks at the end of every file name and title for a note in brackets that most songs "
                                "share, even when its numbers change from song to song, like \"(RS1 take 3)\" and "
                                "\"(RS1 take 5)\". If it finds one, it fills in the cleaned names for you to look over. "
                                "Nothing is written until you press Save Changes.")
        ttk.Label(btns, text="Songs with unsaved changes are shown in gold.", foreground=FG_MUTED).pack(
            side="left", padx=12)

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        columns = ("track", "title", "artist", "file")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        for col, text, width in (("track", "Track", 70), ("title", "Title", 340), ("artist", "Artist", 200),
                                 ("file", "File", 240)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, minwidth=50, anchor="center" if col == "track" else "w",
                             stretch=(col == "title"))
        scroll = tk.Scrollbar(frame, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("edited", foreground=self.GOLD)
        self.tree.bind("<Double-1>", self._edit_cell)
        Tooltip(self.tree, "Double-click a track number, title, artist or file name to change it. Press Enter to keep "
                           "what you typed, or Escape to cancel. A file name keeps its .flac or .mp3 ending.")
        self._fill()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda e: self._close() if self._editor is None else None)
        self.status.config(text=f"{len(songs)} song(s) in this album.")

    def _fill(self):
        self.tree.delete(*self.tree.get_children())
        for i, song in enumerate(self.songs):
            tags = song["tags"]
            self.tree.insert("", "end", iid=str(i),
                             values=(tags["tracknumber"], tags["title"], tags["artist"], song["name"]))

    # ------------------------------------------------------------------ editing a cell
    def _edit_cell(self, event):
        if self._busy:
            return
        row = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if not row or col not in ("#1", "#2", "#3", "#4"):
            return
        key, cell = (self.CELLS + (("__file", "file"),))[int(col[1:]) - 1]
        box = self.tree.bbox(row, col)
        if not box:
            return
        x, y, w, h = box
        self._close_editor()
        entry = ttk.Entry(self.tree)
        entry.place(x=x, y=y, width=w, height=h)
        entry.insert(0, self.tree.set(row, cell))
        entry.select_range(0, "end")
        entry.focus_set()
        self._editor = entry
        entry.bind("<Return>", lambda e: self._commit(row, cell, True))
        entry.bind("<FocusOut>", lambda e: self._commit(row, cell, True))
        entry.bind("<Escape>", lambda e: (self._commit(row, cell, False), "break")[1])

    def _close_editor(self):
        if self._editor is not None:
            try:
                self._editor.destroy()
            except tk.TclError:
                pass
            self._editor = None

    def _commit(self, row, cell, keep):
        entry = self._editor
        if entry is None:
            return
        value = entry.get().strip()
        self._close_editor()
        if not keep or (cell in ("title", "file") and not value):
            return
        if cell == "file":
            ext = os.path.splitext(self.songs[int(row)]["name"])[1]
            if not value.lower().endswith(ext.lower()):
                value += ext
        self.tree.set(row, cell, value)
        self._mark(row)

    def _song_changes(self, index):
        """What the list now says differently from the song's own tags."""
        tags = self.songs[index]["tags"]
        values = dict(zip(("track", "title", "artist"), self.tree.item(str(index), "values")[:3]))
        out = {}
        for key, cell in self.CELLS:
            now = str(values[cell]).strip()
            if now and now != tags[key]:
                out[key] = now
        return out

    def _name_change(self, index):
        """The new file name for a song when the File column says something different, else None."""
        now = str(self.tree.set(str(index), "file")).strip()
        return now if now and now != self.songs[index]["name"] else None

    def _mark(self, row):
        edited = self._song_changes(int(row)) or self._name_change(int(row))
        self.tree.item(row, tags=("edited",) if edited else ())

    # ------------------------------------------------------------------ saving
    def _album_changes(self):
        out = {}
        for key, _label in self.ALBUM_FIELDS:
            now = self.album_entries[key].get().strip()
            if now and now != self.album_initial[key]:
                out[key] = now
        return out

    def _file_renames(self):
        """[{"path", "new_name"}] for every song whose File column was changed."""
        out = []
        for i, song in enumerate(self.songs):
            new_name = self._name_change(i)
            if new_name:
                out.append({"path": song["path"], "new_name": new_name})
        return out

    def _find_notes(self):
        """Fills in cleaned titles and file names when most songs end in the same bracket note."""
        if self._busy:
            return
        self._close_editor()
        current = [{"name": self.tree.set(str(i), "file"), "tags": {"title": self.tree.set(str(i), "title")}}
                   for i in range(len(self.songs))]
        found = tag_cleaner.find_repeating_note(current)
        if not found:
            messagebox.showinfo("Find Take Notes", "I didn't find a bracket note at the end of most of the file "
                                "names or titles in this album.\n\nYou can still double-click any file name or "
                                "title and change it yourself.", parent=self)
            return
        shape, example, count, total = found
        if not messagebox.askyesno(
                "Find Take Notes",
                f"{count} of {total} songs end with a note in brackets that looks like this:\n\n    {example}\n\n"
                "(the numbers can differ from song to song).\n\nTake it off the file names and titles? "
                "Nothing is saved yet. The cleaned names show in gold so you can look them over first.",
                parent=self):
            return
        names = titles = 0
        for i, song in enumerate(self.songs):
            stem, ext = os.path.splitext(self.tree.set(str(i), "file"))
            new_stem = tag_cleaner.strip_note(stem, shape)
            if new_stem != stem:
                self.tree.set(str(i), "file", new_stem + ext)
                names += 1
            title = self.tree.set(str(i), "title")
            new_title = tag_cleaner.strip_note(title, shape)
            if new_title != title:
                self.tree.set(str(i), "title", new_title)
                titles += 1
            self._mark(str(i))
        self.status.config(text=f"Took the note off {names} file name(s) and {titles} title(s). Look them over, "
                                "then press Save Changes.", foreground=self.GOLD)

    def _save(self):
        if self._busy:
            return
        self._close_editor()
        album = self._album_changes()
        edits = []
        for i, song in enumerate(self.songs):
            changes = {k: v for k, v in album.items() if song["tags"][k] != v}
            changes.update(self._song_changes(i))                 # a change to one song wins over the album box
            if changes:
                edits.append({"path": song["path"], "changes": changes})
        renames = self._file_renames()
        if not edits and not renames:
            messagebox.showinfo("Edit Tags by Hand", "There's nothing to save yet. Change a box at the top or "
                                "double-click a song's title, track, artist or file name first.", parent=self)
            return
        final = [(self._name_change(i) or song["name"]).casefold() for i, song in enumerate(self.songs)]
        if len(set(final)) != len(final):
            messagebox.showwarning("Two songs, one name",
                                   "Two songs would end up with exactly the same file name, so nothing was saved.\n\n"
                                   "Change one of the file names (for example, keep a take number on it) and "
                                   "press Save Changes again.", parent=self)
            return
        parts = []
        if edits:
            parts.append(f"the tags of {len(edits)} song(s)")
        if renames:
            parts.append(f"the file names of {len(renames)} song(s)")
        if not messagebox.askyesno(
                "Save the changes",
                "Change " + " and ".join(parts) + " in this album?\n\n"
                "Your music itself doesn't change, only the tags stored inside the files and the names they go by. "
                "The old values are saved, so Undo Last Cleanup (on the Tag & Folder Cleaner tab) puts them back."
                + ("\n\nIf this album is already in a committed packing list, make the packing list again after "
                   "renaming files." if renames else ""), parent=self):
            return
        self._busy = True
        self.save_btn.config(state="disabled")
        state = self._state = {"text": "Saving...", "result": None, "error": None, "done": 0}

        def work():
            try:
                def progress(i, total, _path):
                    state["text"], state["done"] = f"Saving song {i} of {total}...", i
                batch = tag_cleaner.new_batch()
                changed, failed = (tag_cleaner.apply_edits(edits, self.log_path, progress=progress, batch=batch)
                                   if edits else (0, 0))
                state["text"] = "Renaming files..."
                renamed, rfailed = (tag_cleaner.apply_renames(renames, self.log_path, batch=batch)
                                    if renames else (0, 0))
                state["result"] = (changed, failed, renamed, rfailed)
            except Exception as exc:
                state["error"] = str(exc)

        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()
        self.after(150, self._poll)

    def _poll(self):
        if not self.winfo_exists():
            return
        state = self._state
        if self._thread.is_alive():
            self.status.config(text=state["text"], foreground=self.GOLD)
            self.after(150, self._poll)
            return
        self._busy = False
        self.save_btn.config(state="normal")
        if state["error"]:
            self.status.config(text="Something went wrong: " + state["error"], foreground="#ff8a8a")
            return
        changed, failed, renamed, rfailed = state["result"]
        self.songs = tag_cleaner.read_album_tags(self.folder)          # show what is really in the files now
        self._fill()
        for key, _label in self.ALBUM_FIELDS:
            values = {song["tags"][key] for song in self.songs}
            common = values.pop() if len(values) == 1 else ""
            self.album_initial[key] = common
            self.album_entries[key].delete(0, "end")
            self.album_entries[key].insert(0, common)
        note = f"Saved. {changed} song(s) changed."
        if renamed:
            note += f" {renamed} file name(s) changed."
        if failed:
            note += f" {failed} couldn't be changed."
        if rfailed:
            note += (f" {rfailed} file name(s) couldn't be changed (another file already has that name, "
                     "or a .cue sheet in the folder uses the old name).")
        self.status.config(text=note, foreground=DONE_GREEN if not (failed or rfailed) else self.GOLD)
        self.on_saved(self.folder, changed, failed, renamed, rfailed)

    def _close(self):
        self._close_editor()
        if self._busy:
            return
        self.destroy()


class CoverFinderWindow(tk.Toplevel):
    """
    "Find Cover Art Online" popup: goes through the albums that have no picture, searches the internet for
    each one, shows the covers it found as thumbnails and saves the one you pick as cover.jpg in the album's
    folder. The Cover Art Embedder then treats it like any picture you put there yourself.
    """
    THUMB = 150
    COLUMNS = 4
    GOLD = "#ffd54a"

    def __init__(self, parent, items, log_path, on_saved, on_done):
        super().__init__(parent)
        self.items, self.log_path = items, log_path
        self.on_saved, self.on_done = on_saved, on_done
        self.index, self.saved = -1, 0
        self._gen = 0
        self._closed = self._saving = False
        self._cells, self._selected = [], None
        self._queue = queue.Queue()
        self.title("Find Cover Art Online")
        self.configure(background=BG_MAIN)
        self.geometry(f"860x{max(560, min(820, self.winfo_screenheight() - 110))}")
        self.minsize(660, 480)
        self.transient(parent)
        self._blank = ImageTk.PhotoImage(Image.new("RGB", (self.THUMB, self.THUMB), BG_INPUT))

        self.album_label = ttk.Label(self, text="", font=("", 13, "bold"), wraplength=820, justify="left")
        self.album_label.pack(fill="x", padx=12, pady=(12, 0))
        self.where_label = ttk.Label(self, text="", foreground=FG_MUTED, wraplength=820, justify="left")
        self.where_label.pack(fill="x", padx=12, pady=(2, 6))

        row = ttk.Frame(self)
        row.pack(fill="x", padx=12, pady=(0, 6))
        ttk.Label(row, text="Artist:").pack(side="left")
        self.artist_entry = ttk.Entry(row, width=24)
        self.artist_entry.pack(side="left", padx=(6, 12))
        ttk.Label(row, text="Album:").pack(side="left")
        self.album_entry = ttk.Entry(row, width=34)
        self.album_entry.pack(side="left", padx=(6, 12))
        self.search_btn = RoundedButton(row, text="Search", command=self._search)
        self.search_btn.pack(side="left")
        Tooltip(self.search_btn, "Searches again with the words in the two boxes. If a search finds nothing, "
                                 "try a shorter album name or leave the artist blank.")
        for entry in (self.artist_entry, self.album_entry):
            entry.bind("<Return>", lambda e: self._search())

        self.status = ttk.Label(self, text="", foreground=self.GOLD, font=("", 12, "bold"),
                                wraplength=820, justify="left")
        self.status.pack(fill="x", padx=12, pady=(0, 4))

        holder = ttk.Frame(self)
        holder.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        self.canvas = tk.Canvas(holder, background=BG_INPUT, highlightthickness=1, highlightbackground=BORDER)
        scroll = tk.Scrollbar(holder, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.grid_frame = tk.Frame(self.canvas, background=BG_INPUT)
        self.canvas.create_window((0, 0), window=self.grid_frame, anchor="nw")
        self.grid_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-2, "units"))
        self.bind("<Button-5>", lambda e: self.canvas.yview_scroll(2, "units"))
        self.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))

        link = ttk.Frame(self)
        link.pack(fill="x", padx=12, pady=(0, 6))
        ttk.Label(link, text="Or paste a link to a picture:").pack(side="left")
        self.link_entry = ttk.Entry(link)
        self.link_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.link_entry.bind("<Return>", lambda e: self._use_link())
        link_btn = RoundedButton(link, text="Use This Link", command=self._use_link)
        link_btn.pack(side="left")
        Tooltip(link_btn, "For when the search finds nothing: right-click a picture in your web browser, copy "
                          "the image address, paste it here and press this.")

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=12, pady=(0, 12))
        self.use_btn = RoundedButton(btns, text="Use This Picture", command=self._use, state="disabled")
        self.use_btn.pack(side="left")
        Tooltip(self.use_btn, "Saves the picture you clicked as cover.jpg in the album's folder. Double-clicking "
                              "a picture does the same. Nothing is embedded yet; that's the next step.")
        self.skip_btn = RoundedButton(btns, text="Skip This Album", command=self._next_album)
        self.skip_btn.pack(side="left", padx=8)
        Tooltip(self.skip_btn, "Leaves this album as it is and moves on to the next one that needs a picture.")
        self.close_btn = RoundedButton(btns, text="Close", command=self._close)
        self.close_btn.pack(side="left")
        ttk.Label(btns, text="Click a picture to choose it.", foreground=FG_MUTED).pack(side="left", padx=12)

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda e: self._close())
        self.after(120, self._poll)
        self._next_album()

    # ------------------------------------------------------------------ moving through the albums
    def _next_album(self):
        if self._saving:
            return
        while True:
            self.index += 1
            if self.index >= len(self.items):
                self._close()
                return
            if not self.items[self.index]["cover"]:
                break
        item = self.items[self.index]
        parts = [p for p in item["album"].split(os.sep) if p]
        self.album_label.config(text=f'Album {self.index + 1} of {len(self.items)}:  {" / ".join(parts[-2:])}')
        artist, album = art_finder.guess_album(item["folder"], item["missing"][0] if item["missing"] else None)
        self.artist_entry.delete(0, "end")
        self.artist_entry.insert(0, artist)
        self.album_entry.delete(0, "end")
        self.album_entry.insert(0, album)
        self.where_label.config(text="The search words came from the song tags and folder names; fix them below "
                                     "if they look wrong. The picture will be saved in: "
                                     + art_finder.save_folder(item["folder"]))
        self.link_entry.delete(0, "end")
        self._search()

    def _close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.on_done(self.saved)
        finally:
            self.destroy()

    # ------------------------------------------------------------------ searching
    def _clear_cells(self):
        for cell in self._cells:
            cell["frame"].destroy()
        self._cells, self._selected = [], None
        self.use_btn.config(state="disabled")
        self.canvas.yview_moveto(0)

    def _search(self):
        if self._saving:
            return
        artist, album = self.artist_entry.get().strip(), self.album_entry.get().strip()
        if not album:
            self.status.config(text="Type an album name first.", foreground=self.GOLD)
            return
        self._gen += 1
        gen = self._gen
        self._clear_cells()
        self.status.config(text="Searching iTunes, Deezer and MusicBrainz...", foreground=self.GOLD)

        def work():
            from concurrent.futures import ThreadPoolExecutor
            found, errors = art_finder.search(artist, album)
            self._queue.put(("cands", gen, found, errors))
            if found:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    futures = [pool.submit(art_finder.fetch_thumbnail, c) for c in found]
                    for i, future in enumerate(futures):
                        self._queue.put(("thumb", gen, i, future.result()))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        if self._closed:
            return
        try:
            while True:
                self._handle(self._queue.get_nowait())
        except queue.Empty:
            pass
        self.after(120, self._poll)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "saved":
            self._saved(*msg[1:])
            return
        if msg[1] != self._gen:
            return                                   # an answer to an older search
        if kind == "cands":
            found, errors = msg[2], msg[3]
            for i, cand in enumerate(found):
                self._make_cell(i, cand)
            if found:
                self.status.config(text=f"Found {len(found)} picture(s). Click the one you want.",
                                   foreground=DONE_GREEN)
            elif len(errors) == len(art_finder.SOURCES):
                self.status.config(text="Couldn't reach the internet. Check your connection and press Search.",
                                   foreground="#ff8a8a")
            else:
                self.status.config(text="Nothing found. Try a shorter album name, or leave the artist blank.",
                                   foreground=self.GOLD)
        elif kind == "thumb" and msg[2] < len(self._cells):
            self._set_thumb(msg[2], msg[3])

    # ------------------------------------------------------------------ the thumbnails
    def _make_cell(self, i, cand):
        row, col = divmod(i, self.COLUMNS)
        frame = tk.Frame(self.grid_frame, background=BG_PANEL, highlightthickness=3, highlightbackground=BG_PANEL,
                         cursor="hand2")
        frame.grid(row=row, column=col, padx=6, pady=6, sticky="n")
        pic = tk.Label(frame, image=self._blank, text="Loading...", compound="center", background=BG_INPUT,
                       foreground=FG_MUTED, borderwidth=0)
        pic.pack(padx=4, pady=(4, 2))
        name = " - ".join(x for x in (cand["artist"], cand["album"]) if x) or "your link"
        year = f' ({cand["year"]})' if cand.get("year") else ""
        caption = tk.Label(frame, text=f'{cand["source"]}\n{name}{year}', background=BG_PANEL, foreground=FG_TEXT,
                           wraplength=self.THUMB, justify="center", font=("", 9))
        caption.pack(padx=4, pady=(0, 4))
        cell = {"frame": frame, "pic": pic, "caption": caption, "cand": cand, "ok": False}
        self._cells.append(cell)
        for widget in (frame, pic, caption):
            widget.bind("<Button-1>", lambda e, i=i: self._select(i))
            widget.bind("<Double-1>", lambda e, i=i: (self._select(i), self._use()))

    def _set_thumb(self, i, data):
        cell = self._cells[i]
        try:
            if data is None:
                raise ValueError("no picture")
            img = Image.open(io.BytesIO(data))
            img.load()
            size = img.size
            shown = img.convert("RGB")
            shown.thumbnail((self.THUMB, self.THUMB))
            photo = ImageTk.PhotoImage(shown)
            cell["pic"].config(image=photo, text="")
            cell["pic"].image = photo
            cell["ok"] = True
        except Exception:
            cell["pic"].config(image=self._blank, text="(no picture)")
            cell["caption"].config(foreground="#7a6f99")

    def _select(self, i):
        if self._saving or i >= len(self._cells) or not self._cells[i]["ok"]:
            return
        if self._selected is not None and self._selected < len(self._cells):
            self._cells[self._selected]["frame"].config(highlightbackground=BG_PANEL)
        self._selected = i
        self._cells[i]["frame"].config(highlightbackground=ACCENT_LIGHT)
        self.use_btn.config(state="normal")

    # ------------------------------------------------------------------ saving the pick
    def _use(self):
        if self._selected is None or self._saving:
            return
        self._start_save(self._cells[self._selected]["cand"])

    def _use_link(self):
        if self._saving:
            return
        try:
            cand = art_finder.candidate_from_link(self.link_entry.get())
        except ValueError as exc:
            self.status.config(text=str(exc).capitalize() + ".", foreground="#ff8a8a")
            return
        self._start_save(cand)

    def _start_save(self, cand):
        item = self.items[self.index]
        folder = art_finder.save_folder(item["folder"])
        self._saving = True
        self.use_btn.config(state="disabled")
        self.status.config(text="Downloading the picture...", foreground=self.GOLD)

        def work():
            try:
                info = art_finder.download_cover(cand, folder, self.log_path)
                self._queue.put(("saved", item, cand, info, None))
            except Exception as exc:
                self._queue.put(("saved", item, cand, None, str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def _saved(self, item, cand, info, error):
        self._saving = False
        if error:
            self.status.config(text=f"That one wouldn't download ({error}). Pick another, or paste a link.",
                               foreground="#ff8a8a")
            if self._selected is not None:
                self.use_btn.config(state="normal")
            return
        self.saved += 1
        self.on_saved(item, info, cand)
        self._next_album()


class BurnTrackerPanel(ttk.Frame):
    """
    "Burn Tracker" tab: every disc in the packing-list database with Burned and Verified marks, so
    the database (and Search Discs) knows what's actually been burned and checked. Marks can be set
    by hand, imported from the Excel/LibreOffice checklist, or earned by "Verify Disc", which checks
    a burned disc against the plan.
    """

    def __init__(self, parent, app, db_path, checklist_path=None):
        super().__init__(parent)
        self.app = app          # the main window: holds the running verify job and opens files
        self.db_path = db_path
        self.checklist_path = checklist_path
        self._verifying = False
        self._xlsx_reminder_shown = False
        self._export_pending = set()    # discs whose checklist boxes still need writing (file was busy)

        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=(10, 4))
        self.summary = ttk.Label(top, text="", font=("", 10, "bold"))
        self.summary.pack(side="left")
        self.diff_label = ttk.Label(self, text="", foreground=FG_MUTED, wraplength=860, justify="left")
        self.diff_label.pack(fill="x", padx=10)
        self.sync_label = ttk.Label(self, text="", foreground=FG_MUTED, wraplength=860, justify="left")
        self.sync_label.pack(fill="x", padx=10)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=10, pady=4)
        columns = ("disc", "type", "albums", "size", "burned", "verified")
        self.tree = ttk.Treeview(body, columns=columns, show="headings", selectmode="extended")
        for col, text, width in (("disc", "Disc #", 70), ("type", "Type", 80), ("albums", "Number of Albums", 130),
                                  ("size", "Size", 100), ("burned", "Burned", 150), ("verified", "Verified", 150)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="center")
        scroll = tk.Scrollbar(body, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._toggle_clicked)
        # A fully verified disc turns its whole row green (burned-only discs keep the normal colour).
        self.tree.tag_configure("verified", foreground=DONE_GREEN)

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", padx=10, pady=(4, 6))
        for text, command, tip in (
            ("Mark Burned", lambda: self._mark(True),
             "Marks the selected discs as burned (hold Shift or Ctrl to select several)."),
            ("Mark Not Burned", lambda: self._mark(False),
             "Clears the burned mark on the selected discs. A disc that isn't burned can't be verified, "
             "so this clears Verified too."),
            ("Mark Verified", self._mark_verified,
             "Marks the selected discs as verified (and burned) without checking anything -- use "
             "this if you checked a disc some other way."),
            ("Verify Disc...", self._verify_disc_clicked,
             "Checks a burned disc for read errors, like other disc-burning software does (choose the "
             "thorough option to read every file), and checks it against the packing list to make sure no "
             "albums were left out. Insert the disc, select it in the list above, and click Verify Disc. "
             "Once it passes, the disc is marked both burned and verified, and then ejected."),
            ("Open Checklist", self._open_checklist,
             "Opens the checklist. Marks you make here are written into it automatically, and edits you make "
             "in it are picked up the next time you open the Burn Tracker. Save it as .xlsx and close it."),
        ):
            button = RoundedButton(buttons, text=text, command=command)
            button.pack(side="left", padx=(0, 8))
            Tooltip(button, tip)

        add_tip_row(self, "After you burn a disc, put it back in the drive, select it in the list, and click Verify Disc. "
                          "Your burner's own check only proves the disc reads; Verify Disc checks it against your "
                          "packing list, so you know no albums were left out. The buttons above also mark discs by "
                          "hand, and you can edit the checklist in any spreadsheet program if you save it as .xlsx, "
                          "not .ods.")
        self._reload()
        # (The main window calls _on_focus when you come back from editing the checklist.)
        self.after(300, self._reconcile_on_open)
        self.after(500, self._resume_verify_job)

    def _reload(self, keep_selection=()):
        for item in self.tree.get_children():
            self.tree.delete(item)
        discs = core.list_discs(self.db_path)
        for d in discs:
            size = f"{d['size_mb'] / 1024:.2f} GB"
            burned = f"\u2713 {d['burned_date']}".strip() if d["burned"] else ""
            verified = f"\u2713 {d['verified_date']}".strip() if d["verified"] else ""
            self.tree.insert("", "end", iid=str(d["disc_number"]),
                             values=(d["disc_number"], d["type"], d["albums"], size, burned, verified),
                             tags=("verified",) if d["verified"] else ())
        burned_count = sum(1 for d in discs if d["burned"])
        verified_count = sum(1 for d in discs if d["verified"])
        self.summary.config(text=f"{burned_count} of {len(discs)} disc(s) burned, {verified_count} verified")
        for iid in keep_selection:
            if self.tree.exists(iid):
                self.tree.selection_add(iid)
        self._update_comparison()
        try:
            self.app._suggest_next_disc()      # keep the assembler's "next disc" box in step with the marks
        except Exception:
            pass

    def _checklist_file(self):
        """The checklist to read: the .xlsx, or the .ods copy LibreOffice saves beside it --
        whichever was saved most recently (that's the one with your latest X marks)."""
        if not self.checklist_path:
            return None
        stem = os.path.splitext(self.checklist_path)[0]
        found = [p for p in (self.checklist_path, stem + ".ods") if os.path.exists(p)]
        return max(found, key=os.path.getmtime) if found else None

    def _open_checklist(self):
        path = self._checklist_file()
        if not path:
            messagebox.showinfo("No checklist found",
                                "Couldn't find a checklist yet. Generate a packing list first.", parent=self)
            return
        if not self._xlsx_reminder_shown:
            if not messagebox.askokcancel(
                    "Before you edit the checklist",
                    "When you save the checklist, keep it in the .xlsx spreadsheet format, not .ods.\n\n"
                    "If your spreadsheet program asks which format to keep, choose the .xlsx option "
                    "(in LibreOffice that's \"Use Excel 2007-365!\").\n\n"
                    "The Burn Tracker writes your Burned and Verified marks into the .xlsx file. "
                    "A separate .ods copy can fall out of step with it, and then the tracker will "
                    "report that they don't match.\n\n"
                    "Close the checklist when you're done, then come back here.",
                    parent=self):
                return
            self._xlsx_reminder_shown = True
        self.app._xdg_open(path)

    # ---- Export to the checklist ---------------------------------------------------------------
    @staticmethod
    def _checklist_open_elsewhere(path):
        """True if LibreOffice or Excel has the file open right now (each leaves a small lock file)."""
        folder, name = os.path.split(path)
        return (os.path.exists(os.path.join(folder, ".~lock." + name + "#"))
                or os.path.exists(os.path.join(folder, "~$" + name)))

    def _write_marks(self, numbers=None):
        """
        Writes the tracker's marks into the Excel checklist. numbers=None adds an X for every marked disc;
        a list sets or clears just those discs' boxes. Returns (ok, detail): detail is how many boxes
        changed, None when there's no checklist yet (nothing to do), or why it couldn't.
        """
        path = self.checklist_path
        if not path or not os.path.exists(path):
            return True, None
        if not path.lower().endswith(".xlsx"):
            return False, "The checklist isn't an .xlsx file, so the app can't write to it."
        ods = os.path.splitext(path)[0] + ".ods"
        if os.path.exists(ods) and os.path.getmtime(ods) > os.path.getmtime(path):
            return False, (f"The checklist was last saved as {os.path.basename(ods)}. Open it, choose Save As, pick "
                           f"the .xlsx format, replace {os.path.basename(path)}, and close it.")
        if self._checklist_open_elsewhere(path):
            return False, f"{os.path.basename(path)} is open in your spreadsheet program. Save and close it."
        try:
            if numbers is None:
                changed = core.sync_checklist_ticks(self.db_path, path)
            else:
                changed = core.update_checklist_marks(self.db_path, path, numbers)
        except Exception as exc:
            return False, f"Couldn't write to {os.path.basename(path)}: {exc}"
        return True, changed

    def _say_sync(self, text, warn=False):
        self.sync_label.config(text=text, foreground="#f5b942" if warn else FG_MUTED)

    def _sync_checklist(self, numbers):
        """Mirrors the given discs' marks into the checklist (plus any still waiting from an earlier try).
        Returns True if the checklist is now up to date, False if it couldn't be written (it retries when
        you come back to this window), None if there's no checklist."""
        todo = set(numbers) | self._export_pending
        ok, detail = self._write_marks(sorted(todo))
        if ok and detail is None:
            return None
        if ok:
            self._export_pending.clear()
            self._say_sync("Checklist updated." if detail else "Checklist already up to date.")
            return True
        self._export_pending |= todo
        self._say_sync("The checklist wasn't updated: " + detail +
                       "  Your marks are saved here and it will try again when you come back to this window.", warn=True)
        return False

    def _on_focus(self):
        if self._export_pending:
            self._sync_checklist([])
        self._update_comparison()

    def _reconcile_on_open(self):
        """When the tracker opens: pick up any ticks made in the checklist; if the tracker has marks the
        checklist lacks, ask whether to add them (the tracker is right) or clear them here."""
        if not self.winfo_exists():
            return
        path = self._checklist_file()
        if not path:
            return
        try:
            result = core.import_burned_from_checklist(self.db_path, path)
        except Exception:
            return          # unreadable checklist: the comparison line already says so
        picked = len(result["marked"]) + len(result["verified_marked"])
        self._reload()
        if picked:
            self._say_sync(f"Picked up {picked} mark(s) from {os.path.basename(path)}.")
        stale = sorted(set(result["unticked"]) | set(result["verified_unticked"]))
        if not stale:
            return
        plural = len(stale) != 1
        if messagebox.askyesno(
                "Checklist is missing marks",
                f"{self._disc_list(stale)} {'are' if plural else 'is'} marked here but the checklist hasn't been edited to match.\n\n"
                f"Yes: add the missing edits to the checklist (the tracker is right).\n"
                f"No: clear the marks here to match the checklist (choose this if you undid "
                f"{'those edits' if plural else 'that edit'} on purpose).",
                default="yes", parent=self):
            self._sync_checklist(stale)
        else:
            if result["unticked"]:
                core.set_discs_burned(self.db_path, result["unticked"], False)
            remaining = [n for n in result["verified_unticked"] if n not in set(result["unticked"])]
            if remaining:
                core.set_discs_verified(self.db_path, remaining, False)
            self._reload()
        self._update_comparison()

    @staticmethod
    def _disc_list(numbers):
        return "Disc " + ", ".join(str(n) for n in numbers)

    def _update_comparison(self):
        """Says whether the marks here agree with the checklist, so a leftover mark can't sit
        here unnoticed."""
        path = self._checklist_file()
        if not path:
            self.diff_label.config(text="No checklist found to compare with.", foreground=FG_MUTED)
            return
        try:
            diff = core.checklist_differences(self.db_path, path)
        except Exception:
            self.diff_label.config(text="Couldn't read the checklist to compare.", foreground=FG_MUTED)
            return
        notes = []
        for key, wording in (
                ("burned_marked_not_ticked", "marked burned here but not edited in the checklist"),
                ("burned_ticked_not_marked", "edited as burned in the checklist but not marked here"),
                ("verified_marked_not_ticked", "marked verified here but not edited in the checklist"),
                ("verified_ticked_not_marked", "edited as verified in the checklist but not marked here")):
            if diff[key]:
                notes.append(f"{self._disc_list(diff[key])} {wording}")
        if notes:
            self.diff_label.config(
                text="Doesn't match " + os.path.basename(path) + ": " + "; ".join(notes)
                     + ".",
                foreground="#f5b942")
        else:
            self.diff_label.config(text="Matches " + os.path.basename(path) + ".", foreground=FG_MUTED)

    def _selected_numbers(self):
        return [int(iid) for iid in self.tree.selection()]

    def _need_selection(self):
        numbers = self._selected_numbers()
        if not numbers:
            messagebox.showinfo("Pick a disc", "Click a disc (or several) in the list first.", parent=self)
        return numbers

    def _mark(self, burned):
        numbers = self._need_selection()
        if not numbers:
            return
        core.set_discs_burned(self.db_path, numbers, burned)
        self._sync_checklist(numbers)
        self._reload(keep_selection=[str(n) for n in numbers])

    def _mark_verified(self):
        numbers = self._need_selection()
        if not numbers:
            return
        core.set_discs_verified(self.db_path, numbers, True)
        self._sync_checklist(numbers)
        self._reload(keep_selection=[str(n) for n in numbers])

    def _toggle_clicked(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        number = int(iid)
        currently = next((d["burned"] for d in core.list_discs(self.db_path) if d["disc_number"] == number), False)
        core.set_discs_burned(self.db_path, [number], not currently)
        self._sync_checklist([number])
        self._reload(keep_selection=[iid])

    # ---- Verify Disc -------------------------------------------------------------------------
    @staticmethod
    def _optical_mounts():
        """[(device, mount point)] for every mounted data disc (iso9660 / udf), e.g. /dev/sr0 at /run/media/user/Disc 28."""
        try:
            out = subprocess.run(["findmnt", "-rn", "-t", "iso9660,udf", "-o", "SOURCE,TARGET"],
                                 capture_output=True, text=True, timeout=10).stdout
        except Exception:
            return []
        found = []
        for line in out.splitlines():
            source, _, target = line.partition(" ")
            if source and target:
                found.append((source, target.replace("\\x20", " ")))
        return found

    @classmethod
    def _guess_disc_mount(cls):
        discs = cls._optical_mounts()
        if len(discs) == 1:
            return discs[0][1]          # the inserted disc itself, so the file dialog opens right on it
        user = os.environ.get("USER") or os.path.basename(os.path.expanduser("~"))
        for base in (f"/media/{user}", f"/run/media/{user}", "/media", os.path.expanduser("~")):
            if os.path.isdir(base):
                return base
        return "/"

    def _verify_disc_clicked(self):
        job = getattr(self.app, "_verify_job", None)
        if job and not job["handled"]:
            messagebox.showinfo("Verification in progress",
                                f"Disc {job['number']} is still being checked. Wait for it to finish first.",
                                parent=self)
            self._attach_verify_job(job)
            return
        if self._verifying:
            return
        numbers = self._selected_numbers()
        if len(numbers) != 1:
            messagebox.showinfo("Pick one disc",
                                "Click the disc you just burned in the list (just one), then click Verify Disc.",
                                parent=self)
            return
        number = numbers[0]
        mount = filedialog.askdirectory(
            parent=self, title=f"Choose the inserted Disc {number} (usually under /media or /run/media)",
            initialdir=self._guess_disc_mount(), mustexist=True)
        if not mount:
            return
        thorough = messagebox.askyesno(
            "How thorough?",
            "Also read every file all the way through?\n\n"
            "Yes: thorough. Takes a few minutes per disc, but catches unreadable spots.\n"
            "No: quick. Only checks that the albums, files and sizes are all there.",
            default=messagebox.YES, parent=self)
        self._run_verify(number, mount, thorough)

    def _run_verify(self, number, mount, thorough):
        """
        Starts the disc check in the background. The job is kept on the main window, not on this one, so
        it keeps running if you close the Burn Tracker; reopening the tracker brings the progress box
        back (or finishes the job if it ended while the tracker was closed).
        """
        state = {"done": 0, "total": 0, "file": "", "finished": False, "result": None, "error": None, "cancel": False}
        db_path = self.db_path
        job = {"number": number, "mount": mount, "db_path": db_path, "state": state, "handled": False}
        self.app._verify_job = job

        def progress(done, total, filename):
            state["done"], state["total"], state["file"] = done, total, filename

        def work():
            try:
                state["result"] = core.verify_disc(db_path, number, mount, full_read=thorough,
                                                    progress=progress, cancelled=lambda: state["cancel"])
            except Exception as exc:  # report it instead of dying silently in the background
                state["error"] = str(exc)
            state["finished"] = True

        threading.Thread(target=work, daemon=True).start()
        self._attach_verify_job(job)

    def _attach_verify_job(self, job):
        """Shows the progress box for a running check (a new one if the tracker was closed and reopened),
        and finishes the job -- marks, checklist, eject, result message -- when the check is done."""
        if job["handled"] or getattr(self, "_job_attached", False):
            return
        self._job_attached = True
        self._verifying = True
        state, number = job["state"], job["number"]

        win = tk.Toplevel(self)
        win.title(f"Verifying Disc {number}")
        win.configure(background=BG_MAIN)
        win.transient(self.winfo_toplevel())
        win.geometry("520x150")
        label = ttk.Label(win, text="Looking at the disc...", wraplength=480, justify="left")
        label.pack(fill="x", padx=14, pady=(14, 6))
        bar = ttk.Progressbar(win, mode="determinate", length=480)
        bar.pack(padx=14, pady=4)

        def cancel():
            state["cancel"] = True
            label.config(text="Cancelling...")
        RoundedButton(win, text="Cancel", command=cancel).pack(pady=10)

        def confirm_close():
            """The X on this box: ask first, because closing it cancels the check."""
            if state["cancel"] or state["finished"]:
                return
            if messagebox.askyesno("Cancel the verification?",
                                   "Closing this window will cancel the verification.\n\n"
                                   "Do you still want to do that?",
                                   default="no", icon="warning", parent=win):
                cancel()
        win.protocol("WM_DELETE_WINDOW", confirm_close)

        def poll():
            if not self.winfo_exists():
                return
            if state["finished"]:
                if win.winfo_exists():
                    win.destroy()
                job["handled"] = True
                self._job_attached = False
                self._verify_finished(number, state, job["mount"])
                return
            if win.winfo_exists() and state["total"] and not state["cancel"]:
                bar["maximum"] = state["total"]
                bar["value"] = state["done"]
                label.config(text=f"Reading {state['file']}   ({state['done'] / 1048576:.0f} of "
                                   f"{state['total'] / 1048576:.0f} MB)")
            self._verify_after = self.after(150, poll)

        poll()

    def _resume_verify_job(self):
        """Called when the tracker opens: picks up a check that was running when it was closed."""
        job = getattr(self.app, "_verify_job", None)
        if job and not job["handled"] and job["db_path"] == self.db_path:
            self._attach_verify_job(job)

    def stop_polling(self):
        """Called before this panel is replaced: a running check carries on, and the new panel picks it up."""
        after_id = getattr(self, "_verify_after", None)
        if after_id:
            try:
                self.after_cancel(after_id)
            except Exception:
                pass
        self._job_attached = False

    def _verify_finished(self, number, state, mount=None):
        self._verifying = False
        if not self.winfo_exists():
            return
        if state["error"]:
            messagebox.showerror("Couldn't verify", state["error"], parent=self)
            return
        result = state["result"]
        if result["cancelled"]:
            messagebox.showinfo("Cancelled", "Verification was cancelled. Nothing was changed.", parent=self)
            return
        if mount:
            self._eject_disc(mount)      # pass or fail, a checked disc pops out so you know it's done
        if result["ok"]:
            core.set_discs_verified(self.db_path, [number], True)
            self._reload(keep_selection=[str(number)])
            count = len(result["albums"])
            if count == 1:
                where = "The album is on the disc"
            elif count == 2:
                where = "Both albums are on the disc"
            elif count > 2:
                where = f"All {count} albums are on the disc"
            else:
                where = "Everything planned is on the disc"
            text = (f"Disc {number} checks out. {where}, "
                    f"every planned file is there, and the disc was marked Verified.")
            synced = self._sync_checklist([number])
            if synced:
                text += f"\n\nThe checklist ({os.path.basename(self.checklist_path)}) was updated too."
            elif synced is False:
                text += "\n\nThe checklist wasn't updated (see the note in the Burn Tracker window); it will try again."
            self._update_comparison()
            if result["other_on_disc"]:
                text += "\n\nAlso on the disc (not part of the plan, ignored): " + ", ".join(result["other_on_disc"][:6])
            if mount:
                text += "\n\nThe disc is being ejected."
            try:
                move_note = self.app._verified_move_note(number)
            except Exception:
                move_note = ""
            if move_note:
                text += "\n\n" + move_note
            messagebox.showinfo("Verified", text, parent=self)
            self.app._after_disc_verified(number)
        else:
            shown = result["problems"][:60]
            more = len(result["problems"]) - len(shown)
            text = (f"Disc {number} was NOT marked verified. Problems found:\n\n  \u2022 " + "\n  \u2022 ".join(shown)
                    + (f"\n  ...and {more} more" if more > 0 else ""))
            if result.get("read_skipped"):
                text += ("\n\nThe full read of every file was skipped because the quick check already "
                         "found problems. Fix those, then run the thorough check again.")
            if mount:
                text += "\n\nThe disc is being ejected."
            show_text_dialog(self, "Problems found", text)

    def _eject_disc(self, mount):
        """Ejects the checked disc in the background. If every method fails, a note appears in the
        Burn Tracker's status line so you know to press the drive's own eject button."""
        outcome = {"done": False, "ok": False}

        def work():
            source = ""
            target = mount
            discs = self._optical_mounts()
            # The folder picked for Verify Disc may be the disc itself, a folder on it, or the folder that
            # holds the disc's mount (/run/media/user), so match any of those to a real disc mount.
            chosen = [d for d in discs if mount == d[1] or mount.startswith(d[1].rstrip("/") + "/")
                      or d[1].startswith(mount.rstrip("/") + "/")]
            if len(chosen) == 1:
                source, target = chosen[0]
            elif not chosen and len(discs) == 1:
                source, target = discs[0]
            attempts = ([["udisksctl", "unmount", "-b", source], ["eject", source]]
                        if source.startswith("/dev/") else []) + \
                       [["eject", target], ["gio", "mount", "-e", target]]
            errors = []
            for attempt_round in range(3):
                # Right after a verify the disc can be busy for a few seconds (file indexer, file manager,
                # the drive winding down), so try again a couple of times before giving up.
                for cmd in attempts:
                    try:
                        run = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                    except Exception as exc:
                        errors.append(f"{cmd[0]}: {exc}")
                        continue
                    if run.returncode == 0:
                        if cmd[0] == "udisksctl":
                            continue     # unmounted; the eject command after it pops the tray
                        outcome["ok"] = True
                        break
                    errors.append(f"{' '.join(cmd)}: {(run.stderr or run.stdout).strip()[:160]}")
                if outcome["ok"]:
                    break
                time.sleep(4)
            outcome["errors"] = errors
            for line in errors:
                print("Eject attempt failed -", line, flush=True)
            outcome["done"] = True

        threading.Thread(target=work, daemon=True).start()

        def poll():
            if not self.winfo_exists():
                return
            if not outcome["done"]:
                self.after(300, poll)
            elif not outcome["ok"]:
                useful = [e for e in (outcome.get("errors") or []) if not e.startswith("gio ")]
                last = (useful or outcome.get("errors") or [""])[0]
                self._say_sync("The disc couldn't be ejected automatically -- press the eject button on the drive."
                               + (f" ({last})" if last else ""), warn=True)
        poll()


class DiscPlannerGUI(tk.Tk):
    def __init__(self):
        # The window's class name is "DiscWise". The launcher file (StartupWMClass=DiscWise) uses it so the
        # desktop knows this window belongs to the pinned DiscWise button instead of giving it a second icon.
        super().__init__(className="DiscWise")
        _restore_input_method()
        self.title("DiscWise Audio Archive Manager")
        # Open taller than before so more of the log shows, but never taller than the screen.
        start_height = max(650, min(1000, self.winfo_screenheight() - 90))
        # Wider than the old 920px (about 25 characters, measured in the app's own font) so the status text
        # beside the Generate button never crowds the buttons on the right.
        char_px = tkfont.Font(font=("", 11)).measure("0")
        # Another 2 inches on top of that, so the preview message still fits when Undo Last Split is showing.
        two_inches = int(round(2 * self.winfo_fpixels("1i")))
        start_width = int(min(920 + 25 * char_px + two_inches, self.winfo_screenwidth() - 40))
        self.geometry(f"{start_width}x{start_height}")
        self.minsize(820, 650)
        self.configure(background=BG_MAIN)
        self._apply_theme()
        self._apply_icon()

        self.folders = []  # list of {"path": str, "mode": str, "artist": str}
        self.log_queue = queue.Queue()
        self.worker_thread = None
        self.last_db_path = None
        self._confirm_event = threading.Event()
        self._confirm_result = False

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)
        self.notebook = notebook

        self.plan_tab = ttk.Frame(notebook)
        self.burn_tab = ttk.Frame(notebook)
        self.search_tab = ttk.Frame(notebook)
        self.convert_tab = ttk.Frame(notebook)
        self.verify_tab = ttk.Frame(notebook)
        # The cleaner and the art embedder live inside the FLAC Verifier page: three views, one shown at a time.
        self.verify_page = ttk.Frame(self.verify_tab)
        self.tags_tab = ttk.Frame(self.verify_tab)
        self.art_tab = ttk.Frame(self.verify_tab)
        self.verify_page.pack(fill="both", expand=True)
        notebook.add(self.verify_tab, text="FLAC Verifier")
        notebook.add(self.plan_tab, text="Create Packing List")
        notebook.add(self.burn_tab, text="Packager and Burn Tracker")
        notebook.add(self.convert_tab, text="File Converter")
        notebook.add(self.search_tab, text="Search Library")

        self._build_plan_tab()
        self._build_burn_tab()
        self._build_search_tab()
        self._build_convert_tab()
        self._build_verify_tab()
        self._build_tags_tab()
        self._build_art_tab()
        notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        # Coming back from editing the checklist: the Burn Tracker retries any blocked write.
        self.bind("<FocusIn>", self._app_focus_in, add="+")

        self._load_state()
        self._refresh_folder_summary()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_queue)
        self.after(400, self._offer_tidy_up)

    def _apply_theme(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=BG_MAIN, foreground=FG_TEXT, fieldbackground=BG_INPUT,
                         bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                         font=("", 11))
        style.configure("TFrame", background=BG_MAIN)
        style.configure("TLabel", background=BG_MAIN, foreground=FG_TEXT)
        # Bigger tick boxes (clam's default is a tiny 10 px square).
        style.configure("TCheckbutton", background=BG_MAIN, foreground=FG_TEXT, indicatorsize=15)
        style.map("TCheckbutton", background=[("active", BG_MAIN)], foreground=[("disabled", FG_MUTED)])
        style.configure("TRadiobutton", background=BG_MAIN, foreground=FG_TEXT)
        style.map("TRadiobutton", background=[("active", BG_MAIN)])

        # Panels (Options / Output files) are a lighter shade than the window.
        style.configure("TLabelframe", background=BG_PANEL, bordercolor=BORDER)
        style.configure("TLabelframe.Label", background=BG_PANEL, foreground=FG_TEXT,
                         font=("", 11, "bold"))
        style.configure("Panel.TFrame", background=BG_PANEL)
        style.configure("Panel.TLabel", background=BG_PANEL, foreground=FG_TEXT)
        style.configure("PanelMuted.TLabel", background=BG_PANEL, foreground=FG_MUTED)
        style.configure("Panel.TCheckbutton", background=BG_PANEL, foreground=FG_TEXT, indicatorsize=15)
        style.map("Panel.TCheckbutton", background=[("active", BG_PANEL)])

        style.configure("TEntry", fieldbackground=BG_INPUT, foreground=FG_TEXT,
                         bordercolor=BORDER, insertcolor=FG_TEXT)
        style.map("TEntry", bordercolor=[("focus", ACCENT)])

        style.configure("TButton", background=ACCENT, foreground="#ffffff",
                         bordercolor=ACCENT_DARK, focusthickness=0, padding=6)
        style.map("TButton",
                  background=[("active", ACCENT_LIGHT), ("disabled", BG_PANEL)],
                  foreground=[("disabled", FG_MUTED)])

        style.configure("TCombobox", fieldbackground=BG_INPUT, foreground=FG_TEXT,
                         background=BG_PANEL, arrowcolor=FG_TEXT, bordercolor=BORDER)
        style.map("TCombobox", fieldbackground=[("readonly", BG_INPUT)])
        self.option_add("*TCombobox*Listbox.background", BG_INPUT)
        self.option_add("*TCombobox*Listbox.foreground", FG_TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)

        style.configure("TNotebook", background=BG_MAIN, bordercolor=BORDER)
        style.configure("TNotebook.Tab", background=BG_PANEL, foreground=FG_MUTED, padding=(14, 7))
        style.map("TNotebook.Tab",
                  background=[("selected", BG_MAIN)],
                  foreground=[("selected", FG_TEXT)])

        style.configure("Treeview", background=BG_INPUT, fieldbackground=BG_INPUT,
                         foreground=FG_TEXT, bordercolor=BORDER, rowheight=22)
        style.configure("Treeview.Heading", background=BG_PANEL, foreground=FG_TEXT,
                         bordercolor=BORDER)
        style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#ffffff")])
        # Column headers keep their colour when the mouse is over them (the theme turns them white) and get
        # a lavender ring instead. Every table shares this style, so it covers all of them.
        style.map("Treeview.Heading",
                  background=[("active", BG_PANEL), ("pressed", BG_PANEL)],
                  foreground=[("active", FG_TEXT), ("pressed", FG_TEXT)],
                  bordercolor=[("active", ACCENT_LIGHT), ("pressed", ACCENT_LIGHT)],
                  lightcolor=[("active", ACCENT_LIGHT), ("pressed", ACCENT_LIGHT)],
                  darkcolor=[("active", ACCENT_LIGHT), ("pressed", ACCENT_LIGHT)])

        style.configure("TScrollbar", background=BG_PANEL, troughcolor=BG_MAIN,
                         bordercolor=BORDER, arrowcolor=FG_TEXT)

        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=BG_PANEL,
                         bordercolor=BORDER)

    def _apply_icon(self):
        icon_path = os.path.join(SCRIPT_DIR, "app_icon.png")
        if os.path.exists(icon_path):
            try:
                self._icon_image = tk.PhotoImage(file=icon_path)
                self.iconphoto(True, self._icon_image)
            except tk.TclError:
                pass

    # The logo on the packing-list page lives in its own box, so its size has nothing to do with the
    # folder list. Make the box taller (AREA_HEIGHT) and/or raise MAX to make the logo bigger. The logo
    # is drawn to fit its box, so MAX is just a ceiling. All numbers are in pixels (about 96 per inch).
    SIDEBAR_ICON_AREA_HEIGHT = 150
    SIDEBAR_ICON_MAX = 240
    # Nudges the logo up from dead center (it's drawn smaller by twice this to make the room).
    SIDEBAR_ICON_NUDGE_UP = 12
    # The Packager and Burn Tracker logo: 183 px is 50% bigger than the packing tab's 122. BURN_LOGO_DROP moves
    # it down by that many pixels (use a negative number to move it up); edit it to taste.
    BURN_LOGO_SIZE = 183
    BURN_LOGO_DROP = 10

    def _place_sidebar_icon(self, parent, fixed_size=None, drop=0):
        icon_path = os.path.join(SCRIPT_DIR, "app_icon.png")
        if not os.path.exists(icon_path):
            return
        parent.pack_propagate(False)   # the logo never decides how tall this area is
        lbl = tk.Label(parent, background=BG_MAIN, borderwidth=0)
        lbl.place(relx=0.5, rely=0.5, anchor="center", y=-self.SIDEBAR_ICON_NUDGE_UP)
        state = {"size": 0}

        def draw(size):
            size = max(16, int(size))
            if size == state["size"]:
                return
            state["size"] = size
            try:
                if HAVE_PIL:
                    photo = ImageTk.PhotoImage(self._crisp_icon(icon_path, size))
                else:
                    photo = tk.PhotoImage(file=icon_path)
                    factor = max(1, photo.width() // size)
                    photo = photo.subsample(factor, factor)
                lbl.image = photo  # keep a reference with the label, or Tk garbage-collects it
                lbl.config(image=photo)
            except (tk.TclError, OSError):
                pass

        def fit(event):
            if fixed_size:
                draw(max(32, min(fixed_size, event.width - 4)))
                lbl.place_configure(y=-self.SIDEBAR_ICON_NUDGE_UP + drop)
                return
            # Moving the logo up needs empty space to move into, so the logo is drawn a little
            # smaller by twice the nudge (the space above AND below it), then slid up by the nudge.
            nudge = max(0, min(self.SIDEBAR_ICON_NUDGE_UP, (event.height - 48) // 2))
            size = max(32, int(min(self.SIDEBAR_ICON_MAX, event.width - 4, event.height - 4 - 2 * nudge)))
            draw(size)
            lbl.place_configure(y=-nudge)

        parent.bind("<Configure>", fit)
        draw(96)

    @staticmethod
    def _crisp_icon(icon_path, size):
        """Shrinks the icon without the soft/feathery edge. The source PNG has a faint
        translucent glow around the rim; scaled down with its alpha channel, that glow
        reads as a blurry halo. So: cut the glow off at full resolution, trim the empty
        margin, then shrink the colour and the edge mask separately (LANCZOS) and
        paste onto the window background -- a clean, anti-aliased edge."""
        bg = tuple(int(BG_MAIN[i:i + 2], 16) for i in (1, 3, 5))
        src = Image.open(icon_path).convert("RGBA")
        alpha = src.getchannel("A").point(lambda v: 255 if v >= 110 else 0)
        box = alpha.getbbox() or (0, 0, src.width, src.height)
        src = src.crop(box)
        alpha = alpha.crop(box)
        side = max(src.size)
        square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        square.paste(src, ((side - src.width) // 2, (side - src.height) // 2))
        mask_full = Image.new("L", (side, side), 0)
        mask_full.paste(alpha, ((side - src.width) // 2, (side - src.height) // 2))
        flat = Image.alpha_composite(Image.new("RGBA", (side, side), bg + (255,)), square).convert("RGB")
        colour = flat.resize((size, size), Image.LANCZOS)
        mask = mask_full.resize((size, size), Image.LANCZOS)
        out = Image.new("RGB", (size, size), bg)
        out.paste(colour, (0, 0), mask)
        return out

    def _on_close(self):
        converter = getattr(self, "_converter", None)
        if converter is not None and not converter.snapshot()["finished"]:
            converter.cancel()  # don't leave ffmpeg running behind a closed window
        self._save_state()
        self.destroy()

    def _offer_tidy_up(self):
        """
        Older versions left the databases, checklists and backups loose beside the music folders.
        If any are found, offer to gather them into the DiscWise folder (once per launch), and
        repoint the Checklist / Database / Search fields at the new locations.
        """
        # Only look where older versions really left the files: beside the FIRST music folder (and the
        # first library folder), or wherever the Checklist/Database/Search fields point. Other folders in
        # the lists (a test folder on the Desktop, say) are ignored, so a backup copy someone keeps
        # elsewhere never triggers a request to create a folder.
        parents = set()
        for entries in (self.folders, self.library_folders):
            if entries and entries[0].get("path"):
                parents.add(os.path.dirname(os.path.abspath(entries[0]["path"])))
        for field in (self.xlsx_entry, self.db_entry, self.search_db_entry):
            value = field.get().strip()
            if value and os.path.basename(value) in core.LOOSE_FILE_NAMES:
                parents.add(os.path.dirname(os.path.abspath(value)))
        moves = []
        for parent in sorted(parents):
            moves.extend(core.find_loose_files(parent))
        if not moves:
            return
        names = sorted({os.path.basename(old) for old, _new in moves
                        if os.path.basename(os.path.dirname(old)) != "backups"})
        backups = sum(1 for old, _new in moves if os.path.basename(os.path.dirname(old)) == "backups")
        lines = [f"  {n}" for n in names]
        if backups:
            lines.append(f"  {backups} backup copy(ies) from the backups folder")
        target = os.path.dirname(moves[0][1])
        if os.path.basename(target) == "backups":
            target = os.path.dirname(target)
        if not messagebox.askyesno(
                "Tidy up?",
                "DiscWise files are sitting loose in your music location:\n\n" + "\n".join(lines) +
                f"\n\nMove them into one folder?\n\n    {target}\n\n"
                "Nothing is deleted, and the program will find them there from now on."):
            return
        moved, failed = core.move_loose_files(moves)
        mapping = dict(moved)
        for field in (self.xlsx_entry, self.db_entry, self.search_db_entry):
            value = field.get().strip()
            if value in mapping:
                field.delete(0, "end")
                field.insert(0, mapping[value])
        for attr in ("last_db_path", "last_xlsx_path"):
            if getattr(self, attr, None) in mapping:
                setattr(self, attr, mapping[getattr(self, attr)])
        self._save_state()
        if failed:
            messagebox.showwarning(
                "Tidy up",
                f"Moved {len(moved)} item(s), but {len(failed)} couldn't be moved:\n\n" +
                "\n".join(f"{os.path.basename(p)}: {why}" for p, why in failed[:5]))
        else:
            messagebox.showinfo("Tidy up", f"Done -- {len(moved)} item(s) moved into the DiscWise folder.")

    def _save_state(self):
        state = {
            "folders": self.folders,
            "append": self.append_var.get(),
            "keep_burned": self.keep_burned_var.get(),
            "disc_type": self.disc_type_var.get(),
            "xlsx": self.xlsx_entry.get(),
            "db": self.db_entry.get(),
            "min_fill": self.min_fill_entry.get(),
            "move_leftovers_to": self.move_leftovers_entry.get(),
            "search_db": self.search_db_entry.get(),
            "library_folders": self.library_folders,
            "library_include_art": self.library_art_var.get(),
            "convert_src": self.convert_src_entry.get(),
            "convert_dest": self.convert_dest_entry.get(),
            "convert_extras": self.convert_extras_var.get(),
            "waiting_folder": self.waiting_entry.get(),
            "pending_folder": self.pending_entry.get(),
            "downloads_folder": self.downloads_entry.get(),
            "auto_move_after_verify": self.auto_move_var.get(),
            "verify_src": self.verify_src_entry.get(),
            "verify_out": self.verify_out_entry.get(),
        }
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except OSError:
            pass  # not critical -- just means it won't be remembered next time

    def _load_state(self):
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError):
            return

        for entry in state.get("folders", []):
            path = entry.get("path", "")
            if not path:
                continue
            self.folders.append(entry)
            label = path
            if entry.get("mode", "normal") != "normal" and entry.get("artist"):
                label += f"   (one artist: {entry['artist']})"
            self.folder_list.insert("end", label)

        self.append_var.set(state.get("append", False))
        self.keep_burned_var.set(state.get("keep_burned", False) and not self.append_var.get())
        saved_capacity = core.media_type_name(state.get("disc_type"))
        if saved_capacity:
            self.disc_type_var.set(saved_capacity)
        if state.get("xlsx"):
            self.xlsx_entry.insert(0, state["xlsx"])
        if state.get("db"):
            self.db_entry.insert(0, state["db"])
        if state.get("min_fill"):
            self.min_fill_entry.delete(0, "end")
            self.min_fill_entry.insert(0, state["min_fill"])
        if state.get("move_leftovers_to"):
            self.move_leftovers_entry.insert(0, state["move_leftovers_to"])
        # Older versions auto-pointed the Search tab at the packing-list database. The two are
        # separate now, so don't carry that pairing over -- leave the field blank to start fresh.
        saved_search_db = state.get("search_db", "")
        coupled = (os.path.basename(saved_search_db) == "archived_catalog.db"
                   or (saved_search_db and saved_search_db == state.get("db")))
        if saved_search_db and not coupled:
            self.search_db_entry.insert(0, saved_search_db)

        if state.get("convert_src"):
            self.convert_src_entry.insert(0, state["convert_src"])
        if state.get("convert_dest"):
            self.convert_dest_entry.insert(0, state["convert_dest"])
        self.convert_extras_var.set(state.get("convert_extras", True))
        if state.get("waiting_folder"):
            self.waiting_entry.insert(0, state["waiting_folder"])
        if state.get("pending_folder"):
            self.pending_entry.insert(0, state["pending_folder"])
        if state.get("downloads_folder"):
            self.downloads_entry.insert(0, state["downloads_folder"])
        self.auto_move_var.set(bool(state.get("auto_move_after_verify", False)))
        if state.get("verify_src"):
            self.verify_src_entry.insert(0, state["verify_src"])
        if state.get("verify_out"):
            self.verify_out_entry.delete(0, "end")
            self.verify_out_entry.insert(0, state["verify_out"])

        self.library_art_var.set(bool(state.get("library_include_art", False)))
        for entry in state.get("library_folders", []):
            path = entry.get("path", "")
            if not path:
                continue
            self.library_folders.append(entry)
            label = path
            if entry.get("mode", "normal") != "normal" and entry.get("artist"):
                label += f"   (one artist: {entry['artist']})"
            self.library_folder_list.insert("end", label)

    # ---------------------------------------------------------- Plan tab
    def _build_plan_tab(self):
        f = self.plan_tab

        top = ttk.Frame(f)
        top.pack(fill="x", padx=10, pady=(10, 4))
        sources_label = ttk.Label(top, text="Music you want to burn to disc:", font=("", 10, "bold"))
        sources_label.pack(anchor="w")
        Tooltip(sources_label, "These are the folders of music you want to put on physical discs. "
                               "DiscWise looks through them and works out the best way to fit everything "
                               "onto as few discs as possible.")

        list_frame = ttk.Frame(f)
        list_frame.pack(fill="x", padx=10, pady=(0, 10))
        list_frame.columnconfigure(0, weight=3)
        list_frame.columnconfigure(1, weight=1)

        # Left side: the folder list with its buttons under it, kept together as one block that is
        # centered vertically beside the (taller) logo, so the logo never changes the list's size.
        left_col = ttk.Frame(list_frame)
        left_col.grid(row=0, column=0, sticky="ew")
        list_left = ttk.Frame(left_col)
        list_left.pack(fill="x")
        # tk.Listbox has no ttk equivalent -- color it to blend with the theme.
        self.folder_list = tk.Listbox(list_left, height=5, relief="flat", borderwidth=1,
                                       background=BG_INPUT, foreground=FG_TEXT,
                                       highlightthickness=1, highlightbackground=BORDER,
                                       selectbackground=ACCENT, selectforeground="#ffffff",
                                       activestyle="none")
        self.folder_list.pack(side="left", fill="both", expand=True)
        scroll = tk.Scrollbar(list_left, command=self.folder_list.yview)  # same classic look as the log box's scrollbar
        scroll.pack(side="right", fill="y")
        self.folder_list.config(yscrollcommand=scroll.set)

        # The logo gets its own box with its own height (SIDEBAR_ICON_AREA_HEIGHT), so it can be
        # as big as you like without making the folder list any taller.
        icon_holder = ttk.Frame(list_frame, height=self.SIDEBAR_ICON_AREA_HEIGHT)
        icon_holder.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        self._place_sidebar_icon(icon_holder)

        btn_row = ttk.Frame(left_col)
        btn_row.pack(fill="x", pady=(4, 0))
        add_btn = RoundedButton(btn_row, text="Add Folder...", command=self._add_folder)
        add_btn.pack(side="left")
        Tooltip(add_btn, "Choose a music folder to include in the scan.")
        settings_btn = RoundedButton(btn_row, text="Folder Layout...", command=self._edit_folder)
        settings_btn.pack(side="left", padx=6)
        Tooltip(settings_btn, "Most folders need nothing here. Select a folder above, then click this only if it "
                               "holds one artist's music (like \"Extra Van Halen\") instead of a folder for each "
                               "artist. You tell DiscWise whose music it is, so your checklist and searches show the "
                               "right artist name.")
        remove_btn = RoundedButton(btn_row, text="Remove", command=self._remove_folder)
        remove_btn.pack(side="left")
        Tooltip(remove_btn, "Remove the selected folder from the list (doesn't touch any files).")

        opts = ttk.LabelFrame(f, text="Options")
        opts.pack(fill="x", padx=10, pady=(0, 10))

        disc_type_label = ttk.Label(opts, text="Disc capacity:", style="Panel.TLabel")
        disc_type_label.grid(row=0, column=0, sticky="e", padx=(8, 4), pady=(6, 2))
        self.disc_type_var = tk.StringVar(value=core.DEFAULT_MEDIA_TYPE)
        self.disc_type_box = ttk.Combobox(opts, textvariable=self.disc_type_var, state="readonly",
                                          values=list(core.MEDIA_TYPES), width=36)
        self.disc_type_box.grid(row=0, column=1, sticky="w", pady=(6, 2))
        disc_type_tip = ("How much one blank disc holds. The packager fills each disc up to this size, "
                         "leaving a small safety margin. Pick the size that matches the discs you "
                         "have -- the brand doesn't matter. Choose it before you make a packing list.")
        Tooltip(disc_type_label, disc_type_tip)
        Tooltip(self.disc_type_box, disc_type_tip)

        # The two workflow checkboxes live in their own little frame so one note can cover both.
        workflow = ttk.Frame(opts, style="Panel.TFrame")
        workflow.grid(row=1, column=0, columnspan=4, sticky="w", padx=8, pady=(6, 2))
        ttk.Label(workflow, text="Pick the one that matches what you do after burning.",
                  style="Panel.TLabel", foreground=self.BUSY_COLOR,
                  font=("", 12, "bold")).pack(anchor="w", pady=(0, 3))
        ttk.Label(workflow, text="Making your very first packing list? Leave both boxes unticked.",
                  style="PanelMuted.TLabel").pack(anchor="w", pady=(0, 3))
        self.append_var = tk.BooleanVar(value=False)
        append_cb = ttk.Checkbutton(
            workflow, text="Add to existing checklist/database (for when you burn, convert to MP3 and delete the originals)",
            variable=self.append_var, style="Panel.TCheckbutton",
            command=lambda: self.append_var.get() and self.keep_burned_var.set(False)
        )
        append_cb.pack(anchor="w", pady=(0, 2))
        Tooltip(append_cb, "Add to existing is for when you burn, convert to MP3 and delete the originals. "
                            "The scan only sees what's left, and numbering carries on from where "
                            "the last run stopped instead of starting over at Disc 1.")
        self.keep_burned_var = tk.BooleanVar(value=False)
        keep_cb = ttk.Checkbutton(
            workflow, text="Keep burned discs as they are (for when you burn and leave everything on the drive)",
            variable=self.keep_burned_var, style="Panel.TCheckbutton",
            command=lambda: self.keep_burned_var.get() and self.append_var.set(False)
        )
        keep_cb.pack(anchor="w", pady=(0, 2))
        Tooltip(keep_cb, "Keep burned is for when you burn and leave everything on the drive. Discs marked burned in the "
                         "Burn Tracker (counting from Disc 1) stay exactly as they are; everything "
                         "else is repacked together with any new music, so you don't end up with a "
                         "half-empty disc each time. Use this OR Add to existing, not both.")


        start_disc_label = ttk.Label(opts, text="Force start disc #:", style="Panel.TLabel")
        start_disc_label.grid(row=3, column=0, sticky="e", padx=(8, 4))
        self.start_disc_entry = ttk.Entry(opts, width=8)
        self.start_disc_entry.grid(row=3, column=1, sticky="w")
        ttk.Label(opts, text="(leave blank to auto-detect)", style="PanelMuted.TLabel").grid(
            row=4, column=1, sticky="w", pady=(0, 6)
        )
        start_disc_tip = ("Only needed to override the automatic numbering -- for example, if the "
                           "database ever gets out of sync with reality. Leave blank almost always.")
        Tooltip(start_disc_label, start_disc_tip)
        Tooltip(self.start_disc_entry, start_disc_tip)

        min_fill_label = ttk.Label(opts, text="Only commit a disc if at least this full (%):",
                                    style="Panel.TLabel")
        min_fill_label.grid(row=5, column=0, sticky="e", padx=(8, 4), pady=(0, 2))
        self.min_fill_entry = ttk.Entry(opts, width=8)
        self.min_fill_entry.insert(0, "95")
        self.min_fill_entry.grid(row=5, column=1, sticky="w", pady=(0, 2))
        min_fill_tip = ("A disc below this fill level is held back instead of being added to the "
                         "checklist/database -- so you never end up with a wastefully near-empty disc. "
                         "Held-back albums are picked up automatically again next run, from the leftovers "
                         "folder if you set one.")
        Tooltip(min_fill_label, min_fill_tip)
        Tooltip(self.min_fill_entry, min_fill_tip)

        move_label = ttk.Label(opts, text="Move leftover (held-back) folders to:", style="Panel.TLabel")
        move_label.grid(row=6, column=0, sticky="e", padx=(8, 4), pady=(6, 2))
        self.move_leftovers_entry = ttk.Entry(opts, width=36)
        self.move_leftovers_entry.grid(row=6, column=1, sticky="we", pady=(6, 2))
        move_btn_row = ttk.Frame(opts, style="Panel.TFrame")
        move_btn_row.grid(row=6, column=2, padx=(4, 8), pady=(6, 2), sticky="w")
        move_browse_btn = RoundedButton(move_btn_row, text="Browse...", command=self._browse_move_leftovers)
        move_browse_btn.pack(side="left")
        move_newfolder_btn = RoundedButton(move_btn_row, text="New Folder...", command=self._new_move_leftovers_folder)
        move_newfolder_btn.pack(side="left", padx=(4, 0))
        Tooltip(move_newfolder_btn, "Pick a parent folder and create a brand-new folder inside it to "
                                     "hold leftovers -- no need to leave the app to make one first.")
        ttk.Label(opts, text="(leave blank to just leave them in place)", style="PanelMuted.TLabel").grid(
            row=7, column=1, sticky="w", pady=(0, 6)
        )
        move_tip = ("Optional. If set, held-back album folders get physically moved here, so "
                     "everything left in your original folder is exactly what got committed to a "
                     "disc -- safe to convert to MP3 and delete. If you set this folder, it's picked up "
                     "automatically the next time you create a packing list, so you don't need to add "
                     "it to the folders above. Leave blank to just leave held-back folders where "
                     "they are.")
        Tooltip(move_label, move_tip)
        Tooltip(self.move_leftovers_entry, move_tip)
        Tooltip(move_browse_btn, "Browse to an existing folder to hold leftovers.")
        opts.columnconfigure(1, weight=1)

        paths_title = ttk.Label(f, text="Output files (leave blank for the default location)",
                                style="Panel.TLabel", font=("", 11, "bold"))
        paths = ttk.LabelFrame(f, labelwidget=paths_title)
        paths.pack(fill="x", padx=10, pady=(0, 10))
        Tooltip(paths_title, "When you make a packing list, DiscWise saves two files for you: a checklist "
                             "you can edit as you burn, and a database that remembers which disc "
                             "everything is on. Leave these blank and they're saved in your DiscWise folder.")

        xlsx_label = ttk.Label(paths, text="Excel checklist:", style="Panel.TLabel")
        xlsx_label.grid(row=0, column=0, sticky="e", padx=(8, 4), pady=4)
        self.xlsx_entry = ttk.Entry(paths, width=40)
        self.xlsx_entry.grid(row=0, column=1, sticky="we", padx=(0, 4))
        xlsx_browse_btn = RoundedButton(paths, text="Browse...", command=self._browse_xlsx)
        xlsx_browse_btn.grid(row=0, column=2, padx=(0, 4))
        self.open_xlsx_btn = RoundedButton(paths, text="Open Checklist", command=self._open_xlsx)
        self.open_xlsx_btn.grid(row=0, column=3, padx=(0, 8))
        Tooltip(self.open_xlsx_btn, "Opens the Excel checklist: every disc and the albums on it, named just like "
                                     "your folders. Handy for labeling your discs and writing out the covers for "
                                     "your CD cases.")
        xlsx_tip = ("A handy way to label your discs and make the covers for your CD cases. One row per disc, "
                     "listing every album on it named just like your folders, so what you write on the case matches "
                     "what's on your drive. You can also mark each disc Burned and Verified as you go, and the Burn "
                     "Tracker can keep track of those marks.")
        Tooltip(xlsx_label, xlsx_tip)
        Tooltip(self.xlsx_entry, xlsx_tip)
        Tooltip(xlsx_browse_btn, "Choose a different place to save the checklist.")

        db_label = ttk.Label(paths, text="Database:", style="Panel.TLabel")
        db_label.grid(row=1, column=0, sticky="e", padx=(8, 4), pady=4)
        self.db_entry = ttk.Entry(paths, width=40)
        self.db_entry.grid(row=1, column=1, sticky="we", padx=(0, 4))
        db_browse_btn = RoundedButton(paths, text="Browse...", command=self._browse_db)
        db_browse_btn.grid(row=1, column=2, padx=(0, 4))
        self.open_folder_btn = RoundedButton(paths, text="Open Folder", command=self._open_folder)
        self.open_folder_btn.grid(row=1, column=3, padx=(0, 8))
        same = max(int(self.open_xlsx_btn["width"]), int(self.open_folder_btn["width"]))
        self.open_xlsx_btn.set_width(same)                  # Open Checklist and Open Folder match exactly
        self.open_folder_btn.set_width(same)
        Tooltip(self.open_folder_btn, "Opens the folder containing the checklist and database files.")
        db_tip = ("DiscWise's record of which album and song landed on which disc, and which discs "
                   "you've burned. Search Discs and the Burn Tracker both use it, so you never need to "
                   "open it yourself.")
        Tooltip(db_label, db_tip)
        Tooltip(self.db_entry, db_tip)
        Tooltip(db_browse_btn, "Choose a different place to save this record.")
        paths.columnconfigure(1, weight=1)

        run_row = ttk.Frame(f)
        run_row.pack(fill="x", padx=10, pady=(0, 6))
        self.preview_btn = RoundedButton(run_row, text="Preview Only", command=lambda: self._run_clicked(preview=True))
        self.preview_btn.pack(side="left")
        Tooltip(self.preview_btn, "Try it risk-free. Shows how many discs you'd get and which albums would be "
                                  "held back. Nothing is saved or moved. This is the time to fix any folder or "
                                  "file names, because they're locked in once you commit.")
        self.run_btn = RoundedButton(run_row, text="▶  Commit Current Packing List", command=self._run_clicked)
        self.run_btn.pack(side="left", padx=(8, 0), ipadx=6, ipady=2)
        Tooltip(self.run_btn, "Generates the next packing list, verifies it, and saves it to your checklist and database. "
                               "Once committed, don't rename or move these albums or files: Verify Disc and the "
                               "Burn Tracker match your discs against these exact names. "
                               "Not sure yet? Use Preview Only first.")
        # Verify Coverage no longer has a button: it runs by itself after every commit (and Preview Only does
        # the same check on the plan). The hidden object is kept so the enable/disable calls below still work.
        self.verify_btn = RoundedButton(run_row, text="Verify Coverage", command=self._verify_clicked)
        self.plan_verify_btn = self.verify_btn     # the FLAC Verifier tab reuses the name verify_btn later
        self.status_label = ttk.Label(run_row, text="", font=("", 16, "bold"))
        self.status_label.pack(side="left", padx=12)

        # Pack the status text last, so a long message gets trimmed instead of squeezing the buttons.
        self.status_label.pack_forget()
        self.status_label.pack(side="left", padx=12)

        # Shown only when it's useful: a scan found folders too big for one disc, or a split can be undone.
        self._oversized_items = []
        self.split_row = ttk.Frame(f)
        self.split_label = ttk.Label(self.split_row, text="", foreground=self.BUSY_COLOR, font=("", 12, "bold"))
        self.split_label.pack(side="left", padx=(0, 10))
        self.split_btn = RoundedButton(self.split_row, text="Split Them For Me...", command=self._split_clicked)
        self.split_btn.pack(side="left")
        Tooltip(self.split_btn, "Moves the songs of each oversized folder into Disc 1, Disc 2 (and so on) subfolders "
                                "inside that same folder, so the pieces can go on different discs. Nothing is copied "
                                "or deleted, and you'll see exactly what will happen before it does.")
        # Lives in the button row (shown only when there is something to undo), right after the Commit button.
        self.undo_split_btn = RoundedButton(run_row, text="Undo Last Split", command=self._undo_split_clicked)
        Tooltip(self.undo_split_btn, "Puts the songs from the most recent split back where they were and removes "
                                     "the Disc subfolders it made.")
        self.progress = ttk.Progressbar(f, mode="indeterminate")
        self.progress.pack(fill="x", padx=10, pady=(0, 6))
        self._refresh_split_row()

        # Packed to the bottom BEFORE the log, so the log (which stretches to fill) can't squeeze it out.
        plan_tip = add_tip_row(f, "Next step: add your music folders, pick a disc size, and use Preview Only to "
                                  "check your names. When everything looks right, click Commit Current Packing List. "
                                  "After that, leave file and folder names alone. Then head to the Packager and "
                                  "Burn Tracker tab to gather each disc's albums and keep track of your burning.")
        plan_tip.pack_configure(side="bottom")

        log_head = ttk.Frame(f)
        log_head.pack(fill="x", padx=10, pady=(0, 2))
        ttk.Label(log_head, text="Log:", font=("", 11, "bold")).pack(side="left")
        self.full_log_btn = RoundedButton(log_head, text="⤢  Full Log View", command=self._open_full_log, height=24)
        self.full_log_btn.pack(side="left", padx=(12, 0))
        Tooltip(self.full_log_btn, "Opens the whole log in its own big window so you can read it all at once. "
                                    "It keeps filling in live while a scan is running.")
        self._full_log_win = None
        self._full_log_box = None
        self.log_box = scrolledtext.ScrolledText(f, height=16, state="disabled", font=("Monospace", 9),
                                                   background=BG_INPUT, foreground=FG_TEXT,
                                                   insertbackground=FG_TEXT, relief="flat", borderwidth=1,
                                                   highlightthickness=1, highlightbackground=BORDER)
        self.log_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._make_log_copyable(self.log_box, on_clear=self._clear_log)

    @staticmethod
    def _make_log_copyable(box, on_clear=None):
        """The log is read-only, so give it the same right-click choices as the verifier log (Copy selected,
        Copy all, Clear), and let Ctrl+C and Ctrl+A work after clicking in it."""
        def copy_text(text):
            if text:
                box.clipboard_clear()
                box.clipboard_append(text)

        def copy_selected():
            try:
                copy_text(box.get("sel.first", "sel.last"))
            except tk.TclError:
                pass                                    # nothing selected

        def copy_all():
            copy_text(box.get("1.0", "end-1c"))

        def clear():
            if on_clear:
                on_clear()
                return
            box.config(state="normal")
            box.delete("1.0", "end")
            box.config(state="disabled")

        def select_all(_event=None):
            box.tag_add("sel", "1.0", "end-1c")
            return "break"

        menu = tk.Menu(box, tearoff=0)
        menu.add_command(label="Copy selected", command=copy_selected)
        menu.add_command(label="Copy whole log", command=copy_all)
        menu.add_separator()
        menu.add_command(label="Clear log", command=clear)

        def popup(event):
            box.focus_set()
            menu.tk_popup(event.x_root, event.y_root)

        box.bind("<Button-1>", lambda e: box.focus_set(), add="+")
        box.bind("<Button-3>", popup)
        box.bind("<Control-a>", select_all)
        box.bind("<Control-A>", select_all)
        box.bind("<Control-c>", lambda e: (copy_selected(), "break")[1])

    def _fix_folder_names(self):
        """
        The folder-picker drops spaces at the end of a folder name ("Jimi Hendrix " comes back as
        "Jimi Hendrix"), so the saved path doesn't exist. Swap each one for the real folder name.
        """
        for entry in list(self.folders) + list(self.library_folders):
            path = entry.get("path", "")
            fixed = core.resolve_folder(path)
            if fixed != path:
                entry["path"] = fixed

    def _add_folder(self):
        path = filedialog.askdirectory(title="Choose a music folder",
                                      initialdir=self._nearest_existing_folder(self.folders[-1]["path"] if self.folders else ""))
        if path:
            path = core.resolve_folder(path)
            self.folders.append({"path": path, "mode": "normal", "artist": ""})
            self.folder_list.insert("end", path)
            self._save_state()

    def _selected_index(self):
        sel = self.folder_list.curselection()
        return sel[0] if sel else None

    def _edit_folder(self):
        idx = self._selected_index()
        if idx is None:
            messagebox.showinfo("Select a folder", "Pick a folder in the list first.")
            return
        current = self.folders[idx]
        dlg = FolderOverrideDialog(self, current)
        if dlg.result:
            self.folders[idx].update(dlg.result)
            label = current["path"]
            if dlg.result["mode"] != "normal":
                label += f"   (one artist: {dlg.result['artist']})"
            self.folder_list.delete(idx)
            self.folder_list.insert(idx, label)
            self._save_state()

    def _remove_folder(self):
        idx = self._selected_index()
        if idx is None:
            return
        self.folder_list.delete(idx)
        del self.folders[idx]
        self._save_state()

    def _new_move_leftovers_folder(self):
        parent = filedialog.askdirectory(title="Choose where to create the new folder",
                                         initialdir=self._nearest_existing_folder(self.move_leftovers_entry.get()))
        if not parent:
            return
        name = simpledialog.askstring("New folder", "Name for the new folder:", parent=self)
        if not name:
            return
        new_path = os.path.join(parent, name)
        try:
            os.makedirs(new_path, exist_ok=False)
        except FileExistsError:
            pass  # fine -- just use it
        except OSError as exc:
            messagebox.showerror("Couldn't create folder", str(exc))
            return
        self.move_leftovers_entry.delete(0, "end")
        self.move_leftovers_entry.insert(0, new_path)

    def _browse_move_leftovers(self):
        path = filedialog.askdirectory(title="Choose a folder to hold leftover (not-yet-full) albums",
                                      initialdir=self._nearest_existing_folder(self.move_leftovers_entry.get()))
        if path:
            self.move_leftovers_entry.delete(0, "end")
            self.move_leftovers_entry.insert(0, path)

    def _browse_xlsx(self):
        path = filedialog.asksaveasfilename(defaultextension=".xlsx", filetypes=[("Excel files", "*.xlsx")])
        if path:
            self.xlsx_entry.delete(0, "end")
            self.xlsx_entry.insert(0, path)

    def _browse_db(self):
        path = filedialog.asksaveasfilename(defaultextension=".db", filetypes=[("SQLite database", "*.db")])
        if path:
            self.db_entry.delete(0, "end")
            self.db_entry.insert(0, path)

    def _log(self, text):
        self.log_box.config(state="normal")
        self.log_box.insert("end", text)
        self.log_box.see("end")
        self.log_box.config(state="disabled")
        big = self._full_log_box
        if big is not None:
            try:
                follow = big.yview()[1] >= 0.999     # only follow the end if they haven't scrolled up
                big.config(state="normal")
                big.insert("end", text)
                if follow:
                    big.see("end")
                big.config(state="disabled")
            except tk.TclError:
                self._full_log_box = None

    def _clear_log(self):
        for box in (self.log_box, self._full_log_box):
            if box is None:
                continue
            try:
                box.config(state="normal")
                box.delete("1.0", "end")
                box.config(state="disabled")
            except tk.TclError:
                pass

    def _open_full_log(self):
        """Pops the whole log up in a big window (like the spectrogram viewer). Stays live while scanning."""
        win = self._full_log_win
        if win is not None:
            try:
                if win.winfo_exists():
                    win.deiconify()
                    win.lift()
                    win.focus_force()
                    return
            except tk.TclError:
                pass
        win = tk.Toplevel(self)
        self._full_log_win = win
        win.title("DiscWise -- Full Log")
        win.configure(background=BG_MAIN)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = int(sw * 0.8), int(sh * 0.82)
        win.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 3)}")
        win.minsize(500, 300)

        body = ttk.Frame(win)
        body.pack(fill="both", expand=True, padx=12, pady=(12, 6))
        box = tk.Text(body, wrap="word", state="disabled", font=("Monospace", 11), background=BG_INPUT,
                      foreground=FG_TEXT, insertbackground=FG_TEXT, relief="flat", borderwidth=1,
                      highlightthickness=1, highlightbackground=BORDER, padx=8, pady=6)
        scroll = tk.Scrollbar(body, command=box.yview)
        box.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        box.pack(side="left", fill="both", expand=True)
        box.config(state="normal")
        box.insert("1.0", self.log_box.get("1.0", "end-1c"))
        box.see("end")
        box.config(state="disabled")
        self._full_log_box = box
        self._make_log_copyable(box, on_clear=self._clear_log)

        row = ttk.Frame(win)
        row.pack(fill="x", padx=12, pady=(0, 12))
        copy_btn = RoundedButton(row, text="Copy Whole Log", command=lambda: (
            win.clipboard_clear(), win.clipboard_append(box.get("1.0", "end-1c")), copy_btn.config(text="Copied")))
        copy_btn.pack(side="left")
        RoundedButton(row, text="Close", command=win.destroy).pack(side="right")

        def closed(event=None):
            if event is not None and event.widget is not win:
                return
            self._full_log_box = None
            self._full_log_win = None
        win.bind("<Destroy>", closed, add="+")
        win.bind("<Escape>", lambda e: win.destroy())
        box.focus_set()

    def _run_clicked(self, preview=False):
        if not self.folders:
            messagebox.showinfo("Add a folder", "Add at least one folder to scan first.")
            return
        if self.worker_thread and self.worker_thread.is_alive():
            return
        if not preview and not self.append_var.get() and not self.keep_burned_var.get() and self.folders:
            # Neither box ticked means "start over from Disc 1". On a first run there is nothing to lose,
            # so it just goes ahead. If a packing list already exists, make sure that is what they want.
            db_check = self.db_entry.get().strip() or getattr(self, "last_db_path", None)
            if not db_check:
                db_check = os.path.join(core.discwise_home(self.folders[0]["path"]), "archived_catalog.db")
            if os.path.exists(db_check) and not messagebox.askyesno(
                    "Start a brand-new packing list?",
                    "You already have a packing list, and neither option above is ticked.\n\n"
                    "Going ahead will start over at Disc 1 and clear your burned and verified marks.\n\n"
                    "If you meant to carry on from your last list instead, press No, tick one of the two "
                    "options (Add to existing if you delete your music after burning, or Keep burned discs if you "
                    "leave it on the drive), and run it again.\n\n"
                    "Start over anyway?", icon="warning", default="no"):
                return

        start_disc_raw = self.start_disc_entry.get().strip()
        start_disc_override = None
        if start_disc_raw:
            try:
                start_disc_override = int(start_disc_raw)
            except ValueError:
                messagebox.showerror("Invalid value", "Force start disc # must be a whole number.")
                return

        min_fill_raw = self.min_fill_entry.get().strip() or "95"
        try:
            min_fill = float(min_fill_raw) / 100.0
            if not (0 < min_fill <= 1):
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid value", "Min fill % must be a number between 0 and 100.")
            return

        move_leftovers_to = self.move_leftovers_entry.get().strip() or None

        keep_through = None
        if self.keep_burned_var.get():
            db_check = self.db_entry.get().strip() or getattr(self, "last_db_path", None)
            if not db_check and self.folders:
                db_check = os.path.join(core.discwise_home(self.folders[0]["path"]), "archived_catalog.db")
            keep_through = core.burned_through(db_check) if db_check and os.path.exists(db_check) else 0
            if keep_through == 0:
                messagebox.showinfo(
                    "Nothing to keep yet",
                    "\"Keep burned discs\" needs Disc 1 (and onward) marked as burned in the Burn Tracker, "
                    "in the packing-list database. None are marked yet.\n\nMark the discs you've burned "
                    "there first, or untick this option for a fresh packing list.")
                return

        core.set_media_type(self.disc_type_var.get())
        self._save_state()

        self.run_btn.config(state="disabled")
        self.preview_btn.config(state="disabled")
        self.plan_verify_btn.config(state="disabled")
        self.status_label.config(text="Previewing..." if preview else "Running...")
        self.progress.start(12)
        self._confirm_event.clear()
        self._confirm_result = False
        self._clear_log()

        self._fix_folder_names()
        folders_copy = [dict(f) for f in self.folders]
        append = self.append_var.get()
        xlsx_out = self.xlsx_entry.get().strip() or None
        db_out = self.db_entry.get().strip() or None

        self.worker_thread = threading.Thread(
            target=self._run_scan,
            args=(folders_copy, append, start_disc_override, xlsx_out, db_out, min_fill, move_leftovers_to,
                  keep_through, preview),
            daemon=True,
        )
        self.worker_thread.start()

    def _run_scan(self, folders, append, start_disc_override, xlsx_out, db_out, min_fill, move_leftovers_to,
                  keep_through=None, preview=False):
        writer = QueueWriter(self.log_queue)
        try:
            with contextlib.redirect_stdout(writer):
                result = self._do_scan(folders, append, start_disc_override, xlsx_out, db_out,
                                        min_fill, move_leftovers_to, keep_through, preview)
            self.log_queue.put(("done", result))
        except (Exception, SystemExit) as exc:  # surface any error in the GUI rather than dying silently
            # (SystemExit is what the scanner raises when a folder can't be found.)
            self.log_queue.put(("error", str(exc) if str(exc) not in ("", "1")
                                else "A folder couldn't be found -- see the log above for which one."))

    def _do_scan(self, folders, append, start_disc_override, xlsx_out, db_out, min_fill, move_leftovers_to,
                 keep_through=None, preview=False):
        capacity = core.DISC_CAPACITY_MB
        if preview:
            print("PREVIEW ONLY -- nothing will be saved, changed or moved.\n")
        first_root_abs = os.path.abspath(folders[0]["path"])
        if not xlsx_out:
            xlsx_out = os.path.join(core.discwise_home(first_root_abs), "disc_checklist.xlsx")
        if not db_out:
            db_out = os.path.join(core.discwise_home(first_root_abs), "archived_catalog.db")
        if not preview:
            for out_path in (xlsx_out, db_out):
                os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

        print(f"Disc capacity: {self.disc_type_var.get().strip()} -- {capacity}MB usable per disc\n")

        # Held-back albums that were moved to the leftovers folder are picked up again automatically:
        # that folder is scanned along with the listed ones (unless it's already covered by one of them).
        if move_leftovers_to:
            left_abs = os.path.realpath(os.path.abspath(os.path.expanduser(move_leftovers_to)))
            if os.path.isdir(left_abs):
                covered = False
                for entry in folders:
                    root_abs = os.path.realpath(os.path.abspath(entry["path"]))
                    if (left_abs == root_abs or left_abs.startswith(root_abs + os.sep)
                            or root_abs.startswith(left_abs + os.sep)):
                        covered = True
                        break
                if not covered:
                    folders = list(folders) + [{"path": left_abs, "mode": "auto", "artist": ""}]
                    print(f"Also scanning the leftovers folder automatically: {left_abs}\n")

        units = []
        for entry in folders:
            root = os.path.abspath(entry["path"])
            print(f"Scanning: {root}")
            if entry["mode"] == "flat":
                print(f"  (treating as a single-artist folder: {entry['artist']})")
                units.extend(core.find_album_units_flat(root, capacity, entry["artist"]))
            elif entry["mode"] == "nested":
                print(f"  (treating as a single-artist folder with a grouping layer: {entry['artist']})")
                units.extend(core.find_album_units_categorized(root, capacity, entry["artist"]))
            else:
                units.extend(core.find_album_units(root, capacity))

        if not units:
            print("No album folders with audio found.")
            return None

        locked_paths = set()
        if keep_through is not None:
            conn = sqlite3.connect(db_out)
            try:
                locked_paths = {
                    os.path.abspath(row[0]) for row in conn.execute(
                        "SELECT albums.folder_path FROM albums JOIN discs ON albums.disc_id = discs.id "
                        "WHERE discs.disc_number BETWEEN 1 AND ?", (keep_through,))
                }
            finally:
                conn.close()
            before = len(units)
            units = [u for u in units if os.path.abspath(u[3]) not in locked_paths]
            print(f"\nKeeping discs 1-{keep_through} (burned) exactly as they are: "
                  f"{before - len(units)} album(s) already placed; {len(units)} album(s) left to pack")
            if not units:
                print("Nothing new to pack -- every album is already on a kept disc.")
                return None

        flac_units = [u for u in units if not u[2]]
        mp3_units = [u for u in units if u[2]]

        total_flac_mb = sum(u[1] for u in flac_units)
        total_mp3_mb = sum(u[1] for u in mp3_units)
        print(f"\nFound {len(flac_units)} lossless album/disc units, totaling {total_flac_mb/1024:.2f}GB")
        print(f"Found {len(mp3_units)} MP3 album units, totaling {total_mp3_mb/1024:.2f}GB")

        # Pre-run sanity check -- pause and let the user confirm the scale
        # of what's about to be packed before any files get written.
        total_folders = len(units)
        total_gb = (total_flac_mb + total_mp3_mb) / 1024
        too_big_items = [(u[0], u[1], u[3]) for u in units if u[1] > capacity]
        too_big = len(too_big_items)
        wipes_burned = 0
        if not append and keep_through is None and os.path.exists(db_out):
            wipes_burned = core.count_burned_discs(db_out)
        if not preview:
            self.log_queue.put(("confirm", {"folders": total_folders, "gb": total_gb,
                                            "wipes_burned": wipes_burned, "too_big": too_big,
                                            "too_big_items": too_big_items}))
            self._confirm_event.wait()
            if not self._confirm_result:
                print("\nCancelled -- nothing was written.")
                return None

        # Safety net: keep dated copies of the existing database and checklist(s) before anything
        # gets rewritten, so a wrong click (like a fresh run with Append off) can be undone.
        backup_dir = os.path.join(os.path.dirname(db_out), "backups")
        backed_up = [] if preview else core.backup_files(
            [db_out, xlsx_out, os.path.splitext(xlsx_out)[0] + ".ods"], backup_dir)
        if backed_up:
            print(f"\nBacked up {len(backed_up)} existing file(s) to {backup_dir}")

        flac_discs = core.pack_discs(flac_units, capacity)
        mp3_discs = core.pack_discs(mp3_units, capacity) if mp3_units else []

        flac_discs, flac_held = core.split_committed_discs(flac_discs, capacity, min_fill)
        mp3_discs, mp3_held = core.split_committed_discs(mp3_discs, capacity, min_fill)

        start_disc_num = 1
        if keep_through is not None:
            start_disc_num = keep_through + 1
        elif start_disc_override is not None:
            start_disc_num = start_disc_override
        elif append and os.path.exists(db_out):
            try:
                conn = sqlite3.connect(db_out)
                row = conn.execute("SELECT MAX(disc_number) FROM discs WHERE disc_number > 0").fetchone()
                conn.close()
                if row and row[0]:
                    start_disc_num = row[0] + 1
            except sqlite3.Error:
                pass
        if append:
            print(f"\nAdding to your existing list -- new discs will start at Disc {start_disc_num}")

        core.print_disc_plan(flac_discs, capacity, "LOSSLESS (FLAC) DISCS", start_disc_num)
        if mp3_discs:
            core.print_disc_plan(mp3_discs, capacity, "MP3 DISCS", start_disc_num + len(flac_discs))

        total_discs = len(flac_discs) + len(mp3_discs)
        print(f"\n=== TOTAL: {total_discs} new disc(s) needed "
              f"({len(flac_discs)} FLAC + {len(mp3_discs)} MP3) ===")
        print(f"Suggest buying at least {total_discs + 2} to have a couple spares.")

        core.report_and_move_leftovers(flac_discs, mp3_discs, flac_held, mp3_held, min_fill,
                                       None if preview else move_leftovers_to)

        oversized = sorted((u for u in units if u[1] > capacity), key=lambda u: -u[1])
        if oversized:
            print(f"\n=== NEEDS ATTENTION: {len(oversized)} folder(s) are too big for one disc ===")
            print("These can't be burned as they are. DiscWise only moves whole folders, so each one lands "
                  "alone on a disc that is over 100% full.")
            for label, size_mb, _is_mp3, _path in oversized:
                parts = int(-(-size_mb // capacity))
                print(f"   - {label}  ({size_mb:.0f}MB, needs at least {parts} discs' worth)")
            print(f"HOW TO FIX: inside each of those folders, make subfolders named Disc 1, Disc 2 (and so on), "
                  f"move the songs into them in order, and keep each subfolder under {capacity}MB. Then run "
                  f"Preview Only again. The pieces will be packed onto different discs, just like your other "
                  f"multi-disc sets.")

        if preview:
            # The same cross-check Verify Coverage does, run on the plan itself before anything is saved.
            planned = [item[3] for _, items in (flac_discs + mp3_discs) for item in items] + list(locked_paths)
            held_planned = [item[3] for _, items in (flac_held + mp3_held) for item in items]
            if hasattr(core, "check_coverage"):
                check = core.check_coverage([e["path"] for e in folders], planned, held_planned)
            else:
                # disc_planner.py and disc_planner_gui.py are out of step; the preview itself still works.
                check = {"total": 0, "missing": []}
                print("\n(Coverage check skipped: disc_planner.py is an older version than disc_planner_gui.py. "
                      "Replace it with the newest disc_planner.py to turn the check on.)")
            if not hasattr(core, "check_coverage"):
                pass
            elif check["missing"]:
                print(f"\n=== NEEDS ATTENTION: {len(check['missing'])} folder(s) are not in this plan ===")
                for path in check["missing"]:
                    print(f"   - {path}")
            else:
                print(f"\nCoverage check: all {check['total']} album folder(s) are on a disc or held back. "
                      f"Nothing is missing.")
            print("\nThat was a preview: nothing was saved, changed or moved.")
            return {"preview": True, "total_discs": total_discs, "flac_discs": len(flac_discs),
                    "mp3_discs": len(mp3_discs), "held_back": len(flac_held) + len(mp3_held),
                    "oversized": len(oversized), "coverage_missing": len(check["missing"]),
                    "oversized_items": [(u[0], u[1], u[3]) for u in oversized]}

        # Held-back folders that are still sitting where they were scanned (i.e. not
        # moved away by "Move leftover folders to") get recorded as PENDING, so Verify
        # Coverage knows they were deliberately held back rather than missing.
        pending_items = [item for _, items in (flac_held + mp3_held) for item in items
                          if os.path.isdir(item[3])]

        unique_artists, total_albums = core.write_sqlite_db(
            flac_discs, mp3_discs, db_out, append=append, start_disc_num=start_disc_num,
            keep_through=keep_through, pending_items=pending_items
        )
        core.write_checklist_xlsx(
            flac_discs, mp3_discs, capacity, xlsx_out, unique_artists, total_albums,
            append=append, start_disc_num=start_disc_num, keep_through=keep_through,
        )
        if keep_through is not None:
            core.sync_checklist_ticks(db_out, xlsx_out)

        return {
            "xlsx": xlsx_out, "db": db_out, "total_discs": total_discs,
            "flac_discs": len(flac_discs), "mp3_discs": len(mp3_discs),
            "unique_artists": unique_artists, "total_albums": total_albums,
            "held_back": len(flac_held) + len(mp3_held), "oversized": len(oversized),
            "oversized_items": [(u[0], u[1], u[3]) for u in oversized],
        }

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.log_queue.get_nowait()
                try:
                    if kind == "log":
                        self._log(payload)
                    elif kind == "confirm":
                        self._on_confirm_prompt(payload)
                    elif kind == "done":
                        self._on_run_done(payload)
                    elif kind == "error":
                        self._on_run_error(payload)
                    elif kind == "verify_done":
                        self._on_verify_done(payload)
                    elif kind == "verify_error":
                        self._on_verify_error(payload)
                    elif kind == "library_done":
                        self._on_library_done(payload)
                    elif kind == "library_error":
                        self._on_library_error(payload)
                except Exception:
                    # One bad handler must not stop the log from ever updating again.
                    import traceback
                    traceback.print_exc()
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _on_confirm_prompt(self, info):
        warning = ""
        if info.get("wipes_burned"):
            warning = (f"\n\nHEADS UP: Add to existing is off, so this starts a brand-new database and clears the "
                       f"Burn Tracker's record of {info['wipes_burned']} burned disc(s). Cancel and tick "
                       f"Add to existing if you meant to add to what's there.")
        if info.get("too_big"):
            self._oversized_items = info.get("too_big_items", [])
            self._refresh_split_row()
            warning += (f"\n\nNOTE: {info['too_big']} folder(s) are too big to fit on one disc. They can't be "
                        f"burned as they are. You may want to press No and use the 'Split Them For Me' "
                        f"button that appears, then run this again.")
        self._confirm_result = messagebox.askyesno(
            "Sanity check",
            f"About to pack {info['folders']} folder(s), roughly {info['gb']:.1f} GB.{warning}\n\nContinue?",
        )
        self._confirm_event.set()

    def _on_run_done(self, result):
        self.run_btn.config(state="normal")
        self.preview_btn.config(state="normal")
        self.plan_verify_btn.config(state="normal")
        self.progress.stop()
        if not result:
            self.status_label.config(text="Stopped -- see log")
            return
        too_big = result.get("oversized", 0)
        self._oversized_items = result.get("oversized_items", [])
        self._refresh_split_row()
        if too_big:
            self.after(200, lambda: self._offer_split(too_big, bool(result.get("preview"))))
        if result.get("preview"):
            held = f", {result['held_back']} held back" if result.get("held_back") else ""
            note = f" {too_big} too big." if too_big else " Nothing changed."
            if result.get("coverage_missing"):
                note += f" {result['coverage_missing']} folder(s) not in the plan -- see log."
            self.status_label.config(text=f"Preview: {result['total_discs']} disc(s){held}.{note}")
            return
        held_note = f", {result['held_back']} held back" if result.get("held_back") else ""
        self._created_summary = (f"{result['total_discs']} disc(s), "
                                 f"{result['unique_artists']} artists, {result['total_albums']} albums{held_note}")
        self.status_label.config(text=f"Packing list created -- {self._created_summary}")
        self.last_xlsx_path = result["xlsx"]
        self.last_db_path = result["db"]
        self.open_xlsx_btn.config(state="normal")
        self.open_folder_btn.config(state="normal")
        self._save_state()
        self.after(300, self._auto_verify_after_commit)      # double-check the saved list, no extra click

    def _auto_verify_after_commit(self, attempt=0):
        """Runs Verify Coverage by itself once a packing list has been saved. It is quiet when all is well
        (one line in the log) and only pops up if something is missing."""
        if self.worker_thread and self.worker_thread.is_alive() and attempt < 25:
            self.after(200, lambda: self._auto_verify_after_commit(attempt + 1))
            return
        self._verify_clicked(quiet=True)

    # ---- splitting folders that are too big for one disc ----------------------------------------
    SPLIT_LOG_PATH = os.path.join(DATA_DIR, "disc_split_log.jsonl")

    @staticmethod
    def _natural_key(name):
        return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]

    def _split_log_rows(self):
        rows = []
        try:
            with open(self.SPLIT_LOG_PATH, encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        try:
                            rows.append(json.loads(line))
                        except ValueError:
                            pass
        except OSError:
            pass
        return rows

    def _prune_split_log(self):
        """Forgets splits that can't be undone any more: a split only counts while at least one of the
        files it moved is still where the split put it. Once the songs have been burned, converted and
        deleted (or moved on), Undo Last Split has nothing left to do, so the record is dropped."""
        rows = self._split_log_rows()
        if not rows:
            return
        batches = {}
        for r in rows:
            batches.setdefault(r.get("batch"), []).append(r)
        keep = {b for b, items in batches.items()
                if any("to" in r and os.path.exists(r["to"]) for r in items)}
        if len(keep) == len(batches):
            return
        try:
            with open(self.SPLIT_LOG_PATH, "w", encoding="utf-8") as fh:
                for r in rows:
                    if r.get("batch") in keep:
                        fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _refresh_split_row(self):
        items = [i for i in getattr(self, "_oversized_items", []) if os.path.isdir(i[2])]
        self._prune_split_log()
        can_undo = bool(self._split_log_rows())
        if items:
            self.split_label.config(text=f"{len(items)} folder(s) are too big for one disc.")
            self.split_btn.pack(side="left")
        else:
            self.split_label.config(text="")
            self.split_btn.pack_forget()
        if can_undo:
            self.undo_split_btn.pack_forget()
            self.undo_split_btn.pack(side="left", padx=(8, 0), after=self.run_btn)
        else:
            self.undo_split_btn.pack_forget()
        if items:
            self.split_row.pack_forget()
            self.split_row.pack(fill="x", padx=10, pady=(0, 6), before=self.progress)
        else:
            self.split_row.pack_forget()

    def _offer_split(self, count, was_preview):
        if self.worker_thread and self.worker_thread.is_alive():
            return
        if not was_preview:
            messagebox.showwarning(
                "Some folders are too big for one disc",
                f"{count} folder(s) are too big to fit on a single disc, so they ended up alone on discs that "
                f"can't be burned.\n\nThe NEEDS ATTENTION section of the log lists them. Splitting them now "
                f"would not update this packing list, so split first and generate again.")
            return
        if messagebox.askyesno(
                "Some folders are too big for one disc",
                f"{count} folder(s) are too big to fit on a single disc, so each would end up alone on a disc "
                f"that can't be burned.\n\nDiscWise can split each one into Disc 1 / Disc 2 subfolders so the "
                f"pieces fit on different discs. Nothing is copied or deleted, and you'll see exactly what will "
                f"happen first.\n\nWould you like to see the split plan?"):
            self._split_clicked()

    def _plan_split(self, folder, capacity):
        """Returns (groups, reason): groups is a list of lists of song file names (in order), or None with a
        plain-words reason why this folder can't be split automatically."""
        try:
            entries = os.listdir(folder)
        except OSError as exc:
            return None, f"can't be read ({exc.strerror or exc})"
        songs = sorted((n for n in entries if os.path.isfile(os.path.join(folder, n))
                        and n.lower().endswith(core.AUDIO_EXTENSIONS)), key=self._natural_key)
        if not songs:
            return None, "has no songs directly inside it (it's probably made of albums already)"
        if any(os.path.isdir(os.path.join(folder, n)) and core.looks_like_disc_part(n) for n in entries):
            return None, "already has Disc or CD subfolders"
        sizes = [os.path.getsize(os.path.join(folder, n)) / (1024 * 1024) for n in songs]
        if max(sizes) > capacity:
            return None, "has a single song file bigger than a whole disc"
        total = sum(sizes)
        parts = max(2, math.ceil(total / (capacity * 0.9)))
        while parts <= len(songs):
            target = total / parts
            groups, group_sizes, before = [[]], [0.0], 0.0
            for name, size in zip(songs, sizes):
                if groups[-1] and len(groups) < parts and before + size / 2 >= target * len(groups):
                    groups.append([])
                    group_sizes.append(0.0)
                groups[-1].append(name)
                group_sizes[-1] += size
                before += size
            if max(group_sizes) <= capacity:
                return [(g, sz) for g, sz in zip(groups, group_sizes)], ""
            parts += 1
        return None, "can't be divided into pieces that each fit on a disc"

    def _split_clicked(self):
        if self.worker_thread and self.worker_thread.is_alive():
            return
        capacity = core.DISC_CAPACITY_MB
        plans, skipped = [], []
        for label, size_mb, path in getattr(self, "_oversized_items", []):
            if not os.path.isdir(path):
                continue
            groups, reason = self._plan_split(path, capacity)
            if groups:
                plans.append((label, path, groups))
            else:
                skipped.append(f"{label}: {reason}")
        if not plans:
            messagebox.showinfo("Nothing to split", "None of the oversized folders can be split automatically.\n\n"
                                + "\n".join(skipped) if skipped else "There is nothing to split right now.")
            return
        lines = []
        for label, _path, groups in plans:
            lines.append(label)
            for number, (names, size) in enumerate(groups, 1):
                lines.append(f"    Disc {number}: {len(names)} song(s), {size:.0f} MB")
        text = "\n".join(lines)
        if skipped:
            text += "\n\nCan't be split automatically (do these by hand):\n" + "\n".join(skipped)
        if not messagebox.askyesno(
                "Split these folders?",
                f"Inside each folder below, the songs will be moved (not copied) into Disc subfolders, in track "
                f"order:\n\n{text}\n\nNothing is deleted and no song is changed. Any other files in the folder "
                f"(covers, notes) go into Disc 1. 'Undo Last Split' puts everything back.\n\nGo ahead?"):
            return
        batch = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        moved = made = 0
        problems = []
        os.makedirs(os.path.dirname(self.SPLIT_LOG_PATH), exist_ok=True)
        with open(self.SPLIT_LOG_PATH, "a", encoding="utf-8") as log:
            def note(row):
                row["batch"] = batch
                log.write(json.dumps(row, ensure_ascii=False) + "\n")
                log.flush()

            for label, path, groups in plans:
                names_in_groups = {n for names, _ in groups for n in names}
                targets = [os.path.join(path, f"Disc {n}") for n in range(1, len(groups) + 1)]
                if any(os.path.exists(t) for t in targets):
                    problems.append(f"{label}: a Disc folder already exists, skipped")
                    continue
                try:
                    for t in targets:
                        note({"mkdir": t})
                        os.mkdir(t)
                        made += 1
                    for target, (names, _size) in zip(targets, groups):
                        for name in names:
                            note({"from": os.path.join(path, name), "to": os.path.join(target, name)})
                            os.rename(os.path.join(path, name), os.path.join(target, name))
                            moved += 1
                    for name in sorted(os.listdir(path)):          # covers, cue sheets, logs travel with Disc 1
                        full = os.path.join(path, name)
                        if os.path.isfile(full) and name not in names_in_groups:
                            note({"from": full, "to": os.path.join(targets[0], name)})
                            os.rename(full, os.path.join(targets[0], name))
                            moved += 1
                except OSError as exc:
                    problems.append(f"{label}: stopped part-way ({exc.strerror or exc}). "
                                    f"Use Undo Last Split to put things back.")
        self._log(f"\nSplit {len(plans) - len(problems)} folder(s): {moved} file(s) moved into {made} Disc folder(s).\n")
        for line in skipped + problems:
            self._log(f"  Not split -- {line}\n")
        self._oversized_items = []
        self._refresh_split_row()
        if problems:
            messagebox.showwarning("Split finished with problems", "\n".join(problems))
        self._log("Checking the plan again...\n")
        self.after(200, lambda: self._run_clicked(preview=True))

    def _undo_split_clicked(self):
        if self.worker_thread and self.worker_thread.is_alive():
            return
        rows = self._split_log_rows()
        if not rows:
            self._refresh_split_row()
            return
        last = rows[-1]["batch"]
        batch = [r for r in rows if r["batch"] == last]
        rest = [r for r in rows if r["batch"] != last]
        count = sum(1 for r in batch if "from" in r)
        if not messagebox.askyesno("Undo the last split?",
                                   f"This puts {count} file(s) back in their original folders and removes the "
                                   f"Disc folders that the split made (only if they're empty).\n\nGo ahead?"):
            return
        restored = failed = 0
        for r in reversed(batch):
            if "from" in r:
                try:
                    if os.path.exists(r["from"]):
                        raise OSError("a file is already in the original place")
                    os.rename(r["to"], r["from"])
                    restored += 1
                except OSError:
                    failed += 1
        for r in batch:
            if "mkdir" in r:
                try:
                    os.rmdir(r["mkdir"])
                except OSError:
                    pass                                          # not empty or already gone: leave it alone
        with open(self.SPLIT_LOG_PATH, "w", encoding="utf-8") as fh:
            for r in rest:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        self._log(f"\nUndo: {restored} file(s) put back" + (f", {failed} couldn't be moved." if failed else ".") + "\n")
        self._refresh_split_row()
        if failed:
            messagebox.showwarning("Undo finished with problems",
                                   f"{failed} file(s) couldn't be put back (they may have been moved or renamed "
                                   f"since). The rest were restored.")

    def _verify_clicked(self, quiet=False):
        if not self.folders:
            messagebox.showinfo("Add a folder", "Add at least one folder to verify first.")
            return
        db_path = self.db_entry.get().strip() or getattr(self, "last_db_path", None)
        if not db_path or not os.path.exists(db_path):
            messagebox.showinfo("No database yet", "Generate a packing list first (or point the "
                                                     "Database field at an existing one) before "
                                                     "verifying coverage.")
            return
        if self.worker_thread and self.worker_thread.is_alive():
            return

        self._fix_folder_names()
        roots = [entry["path"] for entry in self.folders]
        move_to = self.move_leftovers_entry.get().strip() or None

        self.run_btn.config(state="disabled")
        self.preview_btn.config(state="disabled")
        self.plan_verify_btn.config(state="disabled")
        self.progress.start(12)
        if quiet:
            self.status_label.config(text="Packing list created -- double-checking it...")
        self._verify_quiet = quiet

        self.worker_thread = threading.Thread(
            target=self._run_verify, args=(db_path, roots, move_to), daemon=True
        )
        self.worker_thread.start()

    def _run_verify(self, db_path, roots, move_to):
        writer = QueueWriter(self.log_queue)
        try:
            with contextlib.redirect_stdout(writer):
                print(f"\n=== Verifying coverage against {db_path} ===")
                print("Walking folder(s):")
                for r in roots:
                    print(f"  - {r}")
                held_paths = []
                if move_to and os.path.isdir(move_to):
                    print(f"Also checking leftovers folder: {move_to}")
                    for name in os.listdir(move_to):
                        full = os.path.join(move_to, name)
                        if os.path.isdir(full):
                            held_paths.extend(core.find_media_leaf_folders(full))
                result = core.verify_coverage(db_path, roots, held_paths)
                print(f"Checked {result['total']} folder(s) -- this only reads folder names and sizes "
                      f"(not file contents), so it's normal for it to finish in well under a second "
                      f"even for a large library.")
            self.log_queue.put(("verify_done", result))
        except Exception as exc:
            self.log_queue.put(("verify_error", str(exc)))

    def _on_verify_done(self, result):
        quiet = getattr(self, "_verify_quiet", False)
        self._verify_quiet = False
        self.run_btn.config(state="normal")
        self.preview_btn.config(state="normal")
        self.plan_verify_btn.config(state="normal")
        self.progress.stop()
        missing = result["missing"]
        summary = getattr(self, "_created_summary", "")
        if missing:
            self.status_label.config(text=f"Packing list created, but {len(missing)} folder(s) are not accounted "
                                          f"for -- see log")
        elif quiet and summary:
            self.status_label.config(text=f"Packing list created and verified -- {summary}")
        else:
            self.status_label.config(text=f"All {result['total']} folder(s) accounted for")
        if missing:
            self._log("\n=== VERIFY: folders NOT accounted for ===\n")
            for m in missing:
                self._log(f"  - {m}\n")
            messagebox.showwarning(
                "Verification found gaps",
                f"{len(missing)} folder(s) aren't accounted for in the database. "
                f"See the log for the exact paths.",
            )
        elif quiet:
            self._log(f"\n=== AUTOMATIC CHECK after saving: all {result['total']} folder(s) accounted for -- "
                      f"nothing missing ===\n")
        else:
            self._log(f"\n=== VERIFY: all {result['total']} folder(s) accounted for -- nothing missing ===\n")
            messagebox.showinfo("Verified", "Every folder is accounted for. Nothing missing.")

    def _on_verify_error(self, message):
        self._verify_quiet = False
        self.run_btn.config(state="normal")
        self.preview_btn.config(state="normal")
        self.plan_verify_btn.config(state="normal")
        self.progress.stop()
        self.status_label.config(text="Error -- see log")
        self._log(f"\n--- VERIFY ERROR ---\n{message}\n")
        messagebox.showerror("Something went wrong", message)

    def _on_run_error(self, message):
        self.run_btn.config(state="normal")
        self.preview_btn.config(state="normal")
        self.plan_verify_btn.config(state="normal")
        self.progress.stop()
        self.status_label.config(text="Error -- see log")
        self._log(f"\n--- ERROR ---\n{message}\n")
        messagebox.showerror("Something went wrong", message)

    def _packing_db_path(self, quiet=False):
        """The packing-list database to use: the Database field, else the last run's, else the
        default spot next to the first music folder. Returns None (after telling the user) if
        there isn't one yet."""
        db_path = self.db_entry.get().strip() or getattr(self, "last_db_path", None)
        if not db_path and self.folders:
            # Same default the packing run uses when the Database field is blank.
            db_path = os.path.join(core.discwise_home(self.folders[0]["path"]),
                                   "archived_catalog.db")
        if not db_path or not os.path.exists(db_path):
            if quiet:
                return None
            messagebox.showinfo(
                "No packing-list database yet",
                "Couldn't find the packing-list database. Generate a packing list first, or put the "
                "path to an existing archived_catalog.db in the Database field.")
            return None
        return db_path

    # ---------------------------------------------------------- Burn Tracker tab
    def _build_burn_tab(self):
        f = self.burn_tab
        self.burn_panel = None
        self._burn_placeholder = None
        self._burn_busy = False

        # The panel takes the left three quarters; the logo sits in the empty space on the right, the same
        # way it does on the Create Packing List tab.
        top_row = ttk.Frame(f)
        top_row.pack(fill="x", padx=10, pady=(10, 6))
        top_row.columnconfigure(0, weight=3, uniform="logo_split")   # uniform: always 3 : 1, whatever the
        top_row.columnconfigure(1, weight=1, uniform="logo_split")   # panel's contents ask for
        box = ttk.LabelFrame(top_row, text="Get the next disc ready")
        box.grid(row=0, column=0, sticky="new")
        burn_icon_holder = ttk.Frame(top_row, height=self.BURN_LOGO_SIZE + 4 + 2 * self.SIDEBAR_ICON_NUDGE_UP
                                                    + 2 * abs(self.BURN_LOGO_DROP))
        burn_icon_holder.grid(row=0, column=1, sticky="ew", padx=(10, 0))
        self._place_sidebar_icon(burn_icon_holder, fixed_size=self.BURN_LOGO_SIZE, drop=self.BURN_LOGO_DROP)
        # Wide enough for the longest label in whatever font this computer uses (measured, not guessed).
        label_font = tkfont.nametofont("TkDefaultFont")
        label_chars = -(-label_font.measure("Need to Be Converted folder:") // max(1, label_font.measure("0"))) + 6

        row = ttk.Frame(box, style="Panel.TFrame")
        row.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Label(row, text="Disc number:", style="Panel.TLabel", width=label_chars).pack(side="left")
        self.assemble_disc_entry = ttk.Entry(row, width=6)
        self.assemble_disc_entry.pack(side="left")
        ttk.Label(row, text="  (the next disc that hasn't been burned is filled in for you)",
                  style="Panel.TLabel", foreground=FG_MUTED).pack(side="left")

        # The three folders are set-and-forget, so they live in a section that stays folded shut
        # (one summary line) until you click it.
        self._folders_open = False
        folders_head = ttk.Frame(box, style="Panel.TFrame")
        folders_head.pack(fill="x", padx=8, pady=(8, 0))
        self._folders_toggle = ttk.Label(folders_head, text="\u25b8 Folders", style="Panel.TLabel", cursor="hand2")
        self._folders_toggle.pack(side="left")
        self._folders_summary = ttk.Label(folders_head, text="", style="Panel.TLabel", foreground=FG_MUTED,
                                          cursor="hand2")
        self._folders_summary.pack(side="left", padx=(10, 0))
        for w in (self._folders_toggle, self._folders_summary):
            w.bind("<Button-1>", lambda e: self._toggle_folders())
        Tooltip(self._folders_toggle, "Click to show or hide the folders DiscWise uses: where new downloads land, "
                                      "where each disc is assembled for burning, and where burned albums wait to "
                                      "be converted to MP3s.")
        self._folders_body = ttk.Frame(box, style="Panel.TFrame")
        self._folders_head = folders_head

        def path_row(label, attr, command, tip, label_tip=None, info_tip=None):
            r = ttk.Frame(self._folders_body, style="Panel.TFrame")
            r.pack(fill="x", padx=0, pady=(8, 0))
            row_label = ttk.Label(r, text=label, style="Panel.TLabel", width=label_chars - 3)
            row_label.pack(side="left")
            if label_tip:
                Tooltip(row_label, label_tip)
            # A small slot after the label (on every row, so the boxes line up) that holds an
            # info icon when the row has an explanation worth reading.
            info_slot = ttk.Label(r, text="", style="Panel.TLabel", width=3)
            info_slot.pack(side="left")
            if info_tip:
                info_slot.config(text="\u24d8", foreground=INFO_ICON, cursor="question_arrow", font=("", 11, "bold"))
                Tooltip(info_slot, info_tip)
            entry = ttk.Entry(r)
            entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
            setattr(self, attr, entry)
            for ev in ("<KeyRelease>", "<FocusOut>"):
                entry.bind(ev, lambda e: self._refresh_folder_summary(), add="+")
            button = RoundedButton(r, text="Browse...", command=command)
            button.pack(side="left")
            Tooltip(button, tip)

        path_row("Latest Downloads folder:", "downloads_entry", self._browse_downloads,
                 "Where newly downloaded music lands. Anything in here counts as Pending, so it shows up when "
                 "you search \"pending\" in Search Discs, even before it's on a packing list.",
                 label_tip="Newly downloaded music is placed here and is treated as pending until it's burned to a "
                           "disc. This helps keep Search Discs results accurate.",
                 info_tip="This is a folder that you create yourself. It's completely optional, and not everyone "
                          "uses it.\n\n"
                          "It works as a quarantine: new downloads land here first, where you check them "
                          "(for example, confirm they're real FLACs and look at their spectrograms). Once they "
                          "pass, you move them into your main music library. When enough have piled up, you "
                          "burn them to discs and later convert them to MP3s.\n\n"
                          "If you add new music straight to your main library, leave this blank. Anything in "
                          "this folder counts as pending, so it shows up in Search Discs.")
        path_row("Waiting to Burn folder:", "waiting_entry", self._browse_waiting,
                 "Where each disc gets assembled. Every disc gets its own folder in here, like \"Disc 23\", "
                 "with the albums inside it as Artist / Album.",
                 label_tip="Once the assembler puts a disc together according to the plan, all of its files "
                           "are moved to this folder so they can be burned.",
                 info_tip="This is a folder that you create yourself. Unlike the other two folders, this one is "
                          "NOT optional.\n\n"
                          "It's used by the automatic disc assembler as the destination folder for the albums "
                          "once they've been put together for a disc, so they're ready to burn. Assemble Disc "
                          "won't run without it.")
        path_row("Need to Be Converted folder:", "pending_entry", self._browse_pending,
                 "Where a disc's albums go once the disc has been burned and verified, ready for the File "
                 "Converter. Artist folders that are already there are added to, not duplicated.",
                 label_tip="This is the folder that's used when you want to batch convert a group of discs "
                           "to MP3s later.",
                 info_tip="This is a folder that you create yourself. It's optional, and not everyone "
                          "uses it.\n\n"
                          "After a disc passes Verify Disc, DiscWise can move that disc's albums here from the "
                          "Waiting to Burn folder. They stay put until you're ready to batch convert them to "
                          "MP3s.\n\n"
                          "If the \"move albums automatically\" box below is checked, the move happens on its "
                          "own. If it's unchecked, DiscWise asks you to confirm first. If you don't plan to "
                          "convert discs this way, leave this blank.")

        self.auto_move_var = tk.BooleanVar(value=False)
        auto_row = ttk.Frame(box, style="Panel.TFrame")
        auto_row.pack(fill="x", padx=8, pady=(10, 0))
        auto_check = ttk.Checkbutton(
            auto_row, variable=self.auto_move_var,
            text="After the disc passes Verify Disc, move its albums to Need to Be Converted automatically")
        auto_check.pack(side="left")
        Tooltip(auto_check, "Checked: once a disc passes Verify Disc, its albums are moved straight from Waiting "
                            "to Burn into the Need to Be Converted folder, with no questions asked. Unchecked: "
                            "you're asked to confirm first (a last double-check before anything moves).")

        buttons = ttk.Frame(box, style="Panel.TFrame")
        buttons.pack(fill="x", padx=8, pady=(10, 4))
        self.assemble_btn = RoundedButton(buttons, text="Assemble Disc", command=self._assemble_clicked)
        self.assemble_btn.pack(side="left")
        Tooltip(self.assemble_btn, "Gathers every album planned for this disc, grouped by artist, into one folder "
                                    "in Waiting to Burn, so you can drag that single folder into your burning "
                                    "program. If an album can't be found, nothing is moved and you're told which "
                                    "one is missing.")
        self.finish_btn = RoundedButton(buttons, text="Move Disc to Need to Be Converted...",
                                        command=self._finish_clicked)
        self.finish_btn.pack(side="left", padx=8)
        Tooltip(self.finish_btn, "The manual way to do what the checkbox above does automatically: moves this "
                                  "disc's albums into Need to Be Converted, using the folders entered above. "
                                  "Rarely needed. Only use it once the disc is burned and verified.")
        self.assemble_status = ttk.Label(box, text="", style="Panel.TLabel", foreground=FG_MUTED,
                                         wraplength=620, justify="left")
        self.assemble_status.pack(fill="x", padx=8, pady=(0, 8))

        add_tip_row(f, "Automatic disc assembly: gathers all the albums planned for a disc in the previous step and groups them by "
                       "artist into one tidy folder in Waiting to Burn, so there's a single folder to drag into "
                       "your burning program and no album gets lost along the way. Turn on the checkbox above "
                       "and the albums are moved on to Need to Be Converted for you once the disc verifies.")

        self.burn_host = ttk.Frame(f)
        self.burn_host.pack(fill="both", expand=True)

    def _browse_waiting(self):
        path = filedialog.askdirectory(title="Choose the Waiting to Burn folder",
                                      initialdir=self._nearest_existing_folder(self.waiting_entry.get()))
        if path:
            self.waiting_entry.delete(0, "end")
            self.waiting_entry.insert(0, path)
            self._refresh_folder_summary()

    def _toggle_folders(self):
        """Shows or hides the three folder rows; the one-line summary shows only while they're hidden."""
        self._folders_open = not self._folders_open
        if self._folders_open:
            self._folders_body.pack(fill="x", padx=8, after=self._folders_head)
        else:
            self._folders_body.pack_forget()
        self._refresh_folder_summary()

    def _refresh_folder_summary(self):
        if not hasattr(self, "_folders_summary"):
            return
        self._folders_toggle.config(text=("\u25be Folders" if self._folders_open else "\u25b8 Folders"))
        if self._folders_open:
            self._folders_summary.config(text="")
            return

        def short(entry):
            path = entry.get().strip().rstrip(os.sep)
            return os.path.basename(path) or path or "not set"
        self._folders_summary.config(
            text=f"Downloads: {short(self.downloads_entry)}   \u00b7   Waiting: {short(self.waiting_entry)}"
                 f"   \u00b7   Convert: {short(self.pending_entry)}")

    def _browse_downloads(self):
        path = filedialog.askdirectory(title="Choose the Latest Downloads folder",
                                      initialdir=self._nearest_existing_folder(self.downloads_entry.get()))
        if path:
            self.downloads_entry.delete(0, "end")
            self.downloads_entry.insert(0, path)
            self._refresh_folder_summary()

    def _browse_pending(self):
        path = filedialog.askdirectory(title="Choose the Need to Be Converted folder",
                                      initialdir=self._nearest_existing_folder(self.pending_entry.get()))
        if path:
            self.pending_entry.delete(0, "end")
            self.pending_entry.insert(0, path)
            self._refresh_folder_summary()

    def _on_tab_changed(self, _event=None):
        if self.notebook.select() == str(self.burn_tab):
            self._show_burn_tab()

    def _show_burn_tab(self):
        """Builds (or refreshes) the disc list under the assembler controls. It's rebuilt if the database
        or checklist it points at has changed since last time."""
        db_path = self._packing_db_path(quiet=True)
        checklist = self._checklist_path()
        panel = self.burn_panel
        if panel is not None and not panel.winfo_exists():
            panel = self.burn_panel = None
        if not db_path:
            if panel is not None:
                panel.stop_polling()
                panel.destroy()
                self.burn_panel = None
            if self._burn_placeholder is None or not self._burn_placeholder.winfo_exists():
                self._burn_placeholder = ttk.Label(
                    self.burn_host, foreground=FG_MUTED, wraplength=760, justify="left",
                    text="There's no packing-list database yet. Generate a packing list on the first tab "
                         "(or put the path to an existing archived_catalog.db in its Database field), "
                         "then come back here.")
                self._burn_placeholder.pack(anchor="w", padx=14, pady=14)
            return
        if self._burn_placeholder is not None and self._burn_placeholder.winfo_exists():
            self._burn_placeholder.destroy()
            self._burn_placeholder = None
        if panel is not None and (panel.db_path != db_path or panel.checklist_path != checklist):
            panel.stop_polling()
            panel.destroy()
            panel = self.burn_panel = None
        if panel is None:
            self.burn_panel = BurnTrackerPanel(self.burn_host, self, db_path, checklist)
            self.burn_panel.pack(fill="both", expand=True)
        else:
            panel._reload()
            panel._on_focus()
        self._suggest_next_disc()

    def _app_focus_in(self, event):
        panel = self.burn_panel
        if event.widget is self and panel is not None and panel.winfo_exists() \
                and self.notebook.select() == str(self.burn_tab):
            panel._on_focus()

    def _suggest_next_disc(self, force=False):
        """Fills in the lowest-numbered disc that hasn't been burned yet. A number you typed yourself is left
        alone; an empty box, or a number this method filled in earlier, is kept up to date (so marking a disc
        burned in the Burn Tracker moves the box on to the next one)."""
        current = self.assemble_disc_entry.get().strip()
        if current and not force and current != getattr(self, "_auto_disc_filled", None):
            return
        db_path = self._packing_db_path(quiet=True)
        if not db_path:
            return
        try:
            todo = [d["disc_number"] for d in core.list_discs(db_path) if not d["burned"]]
        except Exception:
            return
        if todo:
            self.assemble_disc_entry.delete(0, "end")
            self.assemble_disc_entry.insert(0, str(min(todo)))
            self._auto_disc_filled = str(min(todo))

    def _disc_number_entered(self):
        text = self.assemble_disc_entry.get().strip()
        if not text.isdigit() or int(text) < 1:
            messagebox.showinfo("Which disc?", "Type the number of the disc you're working on (like 23).")
            return None
        return int(text)

    def _run_in_background(self, func, done):
        """Runs func() off the main thread (moving big folders between drives can take a while) and calls
        done(result, error) back on the main thread."""
        self._burn_busy = True
        box = {}

        def work():
            try:
                box["result"] = func()
            except Exception as exc:
                box["error"] = str(exc)
            box["finished"] = True
        threading.Thread(target=work, daemon=True).start()

        def poll():
            if box.get("finished"):
                self._burn_busy = False
                done(box.get("result"), box.get("error"))
            else:
                self.after(150, poll)
        poll()

    @staticmethod
    def _bullets(items, limit=12):
        shown = items[:limit]
        text = "\n".join(f"  \u2022 {item}" for item in shown)
        if len(items) > limit:
            text += f"\n  ...and {len(items) - limit} more"
        return text

    def _assemble_clicked(self):
        if self._burn_busy:
            return
        number = self._disc_number_entered()
        db_path = self._packing_db_path()
        waiting = self.waiting_entry.get().strip()
        if number is None or not db_path:
            return
        if not waiting:
            messagebox.showinfo("Where should it go?", "Choose your Waiting to Burn folder first (the Browse button).")
            return
        if not messagebox.askyesno(
                "Assemble Disc " + str(number),
                f"This moves every album planned for Disc {number} out of your music folders and into\n\n"
                f"{os.path.join(waiting, f'Disc {number}')}\n\n"
                f"laid out as Artist / Album. If any album can't be found, nothing is moved.\n\nGo ahead?",
                parent=self):
            return
        self.assemble_status.config(text=f"Gathering Disc {number}...")

        def done(result, error):
            if error:
                self.assemble_status.config(text="")
                messagebox.showerror("Couldn't assemble the disc", error, parent=self)
                return
            if result["ok"]:
                extra = f" ({result['already']} were already there)" if result["already"] else ""
                self.assemble_status.config(
                    text=f"Disc {number} is ready: {result['total']} album(s) gathered{extra}. "
                         f"Drag the \"Disc {number}\" folder into your burning program.")
                heads_up = ""
                long_names = result.get("long_names") or []
                if long_names:
                    heads_up = (f"\n\nHeads-up: {len(long_names)} file or folder name(s) are longer than "
                                f"{core.JOLIET_NAME_LIMIT} characters (the longest has {long_names[0][1]}). "
                                f"Your burning program may warn about \"Joliet\" names. Linux doesn't need Joliet, "
                                f"so you can leave it off (Windows might not show the disc properly), or choose "
                                f"its long-name option. Please don't rename anything in the burn project: "
                                f"Verify Disc looks for the original names.")
                messagebox.showinfo(
                    "Disc " + str(number) + " is ready",
                    f"All {result['total']} album(s) for Disc {number} are in:\n\n{result['disc_dir']}\n\n"
                    f"Drag that one folder into your burning program." + heads_up, parent=self)
            else:
                nothing = result["moved"] == 0
                self.assemble_status.config(text=f"Disc {number} was not assembled.")
                show_text_dialog(
                    self, "Disc " + str(number) + " was not assembled",
                    ("Nothing was moved. " if nothing else "") + "Problems found:\n\n"
                    + self._bullets(result["problems"], limit=60))
        self._run_in_background(lambda: core.assemble_disc(db_path, number, waiting), done)

    def _finish_clicked(self):
        if self._burn_busy:
            return
        number = self._disc_number_entered()
        if number is None:
            return
        self._finish_disc(number, ask=True)

    def _verified_move_note(self, number):
        """One line for the 'Disc N checks out' pop-up saying what happens to the albums next: they're being
        moved to Need to Be Converted (the automatic-move box is checked), or they're staying in Waiting to Burn.
        Empty when there's no assembled Disc folder in Waiting to Burn to talk about."""
        waiting, pending = self.waiting_entry.get().strip(), self.pending_entry.get().strip()
        if not waiting or not os.path.isdir(os.path.join(waiting, f"Disc {number}")):
            return ""
        if pending and self.auto_move_var.get():
            return "Its albums are being moved from Waiting to Burn into the Need to Be Converted folder."
        return "Its albums are still in Waiting to Burn; they were not moved."

    def _after_disc_verified(self, number):
        """Called by the Burn Tracker right after a disc passes Verify Disc: offer to move it on."""
        waiting, pending = self.waiting_entry.get().strip(), self.pending_entry.get().strip()
        if waiting and pending and os.path.isdir(os.path.join(waiting, f"Disc {number}")):
            if self.auto_move_var.get():
                self._finish_disc(number, ask=False, quiet=True)
                return
            if messagebox.askyesno(
                    "Move Disc " + str(number) + " on?",
                    f"Disc {number} is burned and verified. Move its albums from Waiting to Burn into the "
                    f"Need to Be Converted folder now?", parent=self):
                self._finish_disc(number, ask=False)
                return
        self._suggest_next_disc(force=True)

    def _finish_disc(self, number, ask, quiet=False):
        waiting, pending = self.waiting_entry.get().strip(), self.pending_entry.get().strip()
        if not waiting or not pending:
            messagebox.showinfo("Two folders needed",
                                "Choose both the Waiting to Burn folder and the Need to Be Converted folder first.")
            return
        disc_dir = os.path.join(waiting, f"Disc {number}")
        if not os.path.isdir(disc_dir):
            messagebox.showinfo("Nothing to move", f"There's no \"Disc {number}\" folder in the Waiting to Burn folder.")
            return
        if ask and not messagebox.askyesno(
                "Move Disc " + str(number) + " on?",
                f"This moves the albums in\n\n{disc_dir}\n\ninto\n\n{pending}\n\n"
                f"adding to artist folders that are already there. Only do this once the disc is burned "
                f"and verified.\n\nGo ahead?", parent=self):
            return
        self.assemble_status.config(text=f"Moving Disc {number} on...")

        def done(result, error):
            if error:
                self.assemble_status.config(text="")
                messagebox.showerror("Couldn't move the disc on", error, parent=self)
                return
            if result["ok"]:
                self.assemble_status.config(text=f"Disc {number}: {result['moved']} album(s) moved to Need to Be Converted.")
                if not quiet:
                    messagebox.showinfo("Disc " + str(number) + " moved",
                                        f"{result['moved']} album(s) were moved into the Need to Be Converted folder.",
                                        parent=self)
            else:
                self.assemble_status.config(text=f"Disc {number}: some albums were left behind.")
                lines = [f"{c}: already exists in Need to Be Converted, left where it was" for c in result["conflicts"]]
                lines += result["problems"]
                show_text_dialog(
                    self, "Some albums were left behind",
                    f"{result['moved']} album(s) were moved. These were not:\n\n" + self._bullets(lines, limit=60))
            self._suggest_next_disc(force=True)
        self._run_in_background(lambda: core.finish_disc_folder(disc_dir, pending), done)

    def _pending_search_roots(self):
        """Folders worth checking when someone searches "pending": the music folders being scanned
        (new downloads land there), the Latest Downloads folder, the Waiting to Burn folder and the leftovers folder."""
        roots = [entry["path"] for entry in self.folders]
        for field in (self.downloads_entry, self.waiting_entry, self.move_leftovers_entry):
            path = field.get().strip()
            if path:
                roots.append(path)
        return roots

    def _open_disc_search(self):
        db_path = self._packing_db_path()
        if not db_path:
            return
        existing = getattr(self, "_disc_search_window", None)
        if existing is not None and existing.winfo_exists() and existing.db_path == db_path:
            existing.deiconify()
            existing.lift()
            existing.term_entry.focus_set()
            return
        if existing is not None and existing.winfo_exists():
            existing.destroy()
        self._disc_search_window = DiscSearchWindow(self, db_path, extra_roots=self._pending_search_roots)

    def _checklist_path(self):
        """Where the checklist is (or will be): the Checklist field if filled in, else the one from
        the last run, else the default spot the packing run uses -- next to the first music folder."""
        path = self.xlsx_entry.get().strip() or getattr(self, "last_xlsx_path", None)
        if not path and self.folders:
            path = os.path.join(core.discwise_home(self.folders[0]["path"]),
                                "disc_checklist.xlsx")
        return path

    def _open_xlsx(self):
        path = self._checklist_path()
        if path and os.path.exists(path):
            self._xdg_open(path)
        else:
            messagebox.showinfo(
                "No checklist found",
                "Couldn't find a checklist yet. Generate a packing list first, or put the path to an "
                "existing checklist in the Checklist field.")

    def _open_folder(self):
        path = self._checklist_path()
        folder = os.path.dirname(path) if path else None
        if folder and os.path.isdir(folder):
            self._xdg_open(folder)
        else:
            messagebox.showinfo(
                "No folder found",
                "Couldn't work out which folder to open yet. Add a music folder (or generate a packing "
                "list) first.")

    @staticmethod
    def _xdg_open(path):
        try:
            subprocess.Popen(["xdg-open", path])
        except FileNotFoundError:
            messagebox.showinfo("Can't auto-open", f"Open this manually:\n{path}")

    # ---------------------------------------------------------- File Converter tab
    CONVERT_GAIN_NUDGE_UP = 55   # pixels the "Drive space recovered" readout is lifted; raise it to lift more, 0 for none

    def _build_convert_tab(self):
        f = self.convert_tab
        self._converter = None
        self._convert_thread = None
        self._convert_error = None
        self._convert_summary = None
        self._retry_files = []
        self._pending_redo = None
        self._convert_gain_before = 0          # drive space recovered by earlier runs; only Clear resets it

        box = ttk.LabelFrame(f, text="Convert FLAC files to MP3")
        box.pack(fill="x", padx=10, pady=(10, 6))

        def path_row(label, attr, command, tip):
            row = ttk.Frame(box, style="Panel.TFrame")
            row.pack(fill="x", padx=8, pady=(8, 0))
            ttk.Label(row, text=label, style="Panel.TLabel", width=28).pack(side="left")
            entry = ttk.Entry(row)
            entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
            setattr(self, attr, entry)
            button = RoundedButton(row, text="Browse...", command=command)
            button.pack(side="left")
            Tooltip(button, tip)

        path_row("Need to Be Converted folder:", "convert_src_entry", self._browse_convert_src,
                 "The folder of FLAC files that need to be converted. Everything inside it, in any number of "
                 "sub-folders, is included.")
        path_row("Save MP3s to:", "convert_dest_entry", self._browse_convert_dest,
                 "Where the MP3s go. The folders inside the Need to Be Converted folder are recreated here. It can't be "
                 "the Need to Be Converted folder itself or a folder inside it.")

        note = ttk.Label(box, style="Panel.TLabel", foreground=FG_MUTED, wraplength=820, justify="left",
                  text="MP3s are created at 320 kbps, the highest quality MP3s can be. "
                       "Tags and cover art come along, and your FLAC files are never changed or deleted unless you tick the box below. "
                       "Any files that are already converted are skipped, so you can cancel at any time "
                       "and press Restart to pick up where you left off."
                   )
        note.pack(fill="x", padx=8, pady=(8, 4))
        box.bind("<Configure>", lambda e, lbl=note: lbl.config(wraplength=max(200, e.width - 30)))

        opts = ttk.Frame(box, style="Panel.TFrame")
        opts.pack(fill="x")
        gain = ttk.Frame(opts, style="Panel.TFrame")          # the "free space gained" readout, on the right
        gain.pack(side="right", padx=(8, 16), pady=(0, 6 + self.CONVERT_GAIN_NUDGE_UP))
        gain_title = ttk.Label(gain, text="Drive space recovered", style="Panel.TLabel", foreground=INFO_ICON,
                               font=("", 17, "bold"), anchor="center", justify="center")
        gain_title.pack(fill="x")
        self.convert_gain_value = ttk.Label(gain, text="\u2014", style="Panel.TLabel", foreground=DONE_GREEN,
                                            font=("", 24, "bold"), anchor="center", justify="center")
        self.convert_gain_value.pack(fill="x")
        gain_tip = ("How much drive space this run has given back. Every time an album's FLAC files are deleted, DiscWise "
                    "adds the size of those FLACs and takes off the size of the MP3s that replaced them, so it only counts "
                    "what DiscWise itself does. Downloads or anything else using the drive don't change it. It climbs album "
                    "by album, keeps its total from one folder to the next, and starts over when you press Clear. It only counts "
                    "when the delete box below is ticked.")
        Tooltip(gain_title, gain_tip)
        Tooltip(self.convert_gain_value, gain_tip)
        left = ttk.Frame(opts, style="Panel.TFrame")
        left.pack(side="left", fill="x", expand=True)

        self.convert_extras_var = tk.BooleanVar(value=True)
        extras = ttk.Checkbutton(left, text="Also copy everything else (cover art, booklets, PDFs, comics, extra folders) into the new folders",
                                 variable=self.convert_extras_var, style="Panel.TCheckbutton")
        extras.pack(anchor="w", padx=8, pady=(0, 4))

        self.convert_delete_var = tk.BooleanVar(value=False)   # never remembered: starts unticked every time
        delete_box = ttk.Checkbutton(left, text="When it's done, check every MP3 against its FLAC, then DELETE the FLAC files that passed and clear out the leftovers",
                                     variable=self.convert_delete_var, style="Panel.TCheckbutton")
        delete_box.pack(anchor="w", padx=8, pady=(0, 8))
        Tooltip(delete_box, "After converting, every MP3 is checked against its FLAC: it has to play all the way "
                            "through, be the same length, carry the same title, artist, album and track tags, and have "
                            "the cover picture if the FLAC had one. Only the FLAC files whose MP3 passes every check "
                            "are deleted. Then everything else left on the FLAC side goes too (cue sheets, logs, "
                            "pictures and booklets that have an identical copy in the MP3 folder) along with the "
                            "emptied folders, so no empty album folder is left behind. A folder that still holds a "
                            "FLAC that didn't pass is left completely alone, and the reason is listed. "
                            "Deleted files do not go to the Trash. A list of what was deleted is saved in "
                            "converter_deleted.log next to the program.\n\n"
                            "It works one album at a time, smallest first, deleting each album's FLAC files before "
                            "starting the next, so the drive never needs much spare room. It checks the free space "
                            "before every album and stops cleanly if the next one won't fit.")

        row = ttk.Frame(f)
        row.pack(fill="x", padx=10, pady=(0, 6))
        self.convert_btn = RoundedButton(row, text="Convert to MP3", command=self._start_convert)
        self.convert_btn.pack(side="left")
        Tooltip(self.convert_btn, "Counts the FLAC files, asks you to confirm, then converts them.")
        self.convert_cancel_btn = RoundedButton(row, text="Cancel", state="disabled", command=self._cancel_convert)
        self.convert_cancel_btn.pack(side="left", padx=8)
        Tooltip(self.convert_cancel_btn, "Stops the conversion. Files already finished are kept, and "
                                          "the Restart button that appears carries on from there.")
        self.convert_clear_btn = RoundedButton(row, text="Clear", command=self._clear_convert)
        self.convert_clear_btn.pack(side="left", padx=(0, 8))
        Tooltip(self.convert_clear_btn, "Clears the log and resets the progress bar and status, ready for the "
                                        "next folder. Your converted files aren't touched.")
        # Status sits at the far right end of the row: a small "Status:" caption and a larger,
        # coloured value (amber while working, green when done). Packed value first so it's rightmost.
        self.convert_status = ttk.Label(row, text="", font=("", 18, "bold"), width=20, anchor="w")
        self.convert_status.pack(side="right", padx=(4, 6))
        self.convert_status_caption = ttk.Label(row, text="Status:", foreground=FG_MUTED, font=("", 11))
        self.convert_status_caption.pack(side="right")
        self._set_convert_status("Ready", "idle")
        # Restart only appears after a cancel (see _poll_convert); it picks up where the conversion stopped.
        self.convert_restart_btn = RoundedButton(row, text="Restart", command=self._restart_convert)
        Tooltip(self.convert_restart_btn, "Starts the conversion again with the same folders. Files already "
                                          "converted are skipped, so it picks up where it left off.")

        # Retry only appears when some files failed or were kept (see _poll_convert).
        self.convert_retry_btn = RoundedButton(row, text="Retry kept files", command=self._retry_convert)
        Tooltip(self.convert_retry_btn, "Throws away the MP3s that failed their check (or never got made), converts those "
                                        "songs again, checks them again, and deletes the FLAC files that now pass. "
                                        "Files that already passed are not touched. Each problem in the log says "
                                        "what to do first if Retry alone won't fix it.")

        self.convert_progress = ttk.Progressbar(f, mode="determinate")
        self.convert_progress.pack(fill="x", padx=10, pady=(0, 6))

        ttk.Label(f, text="Log:", font=("", 11, "bold")).pack(anchor="w", padx=10)
        self.convert_log = WatermarkLog(f, background=BG_INPUT, foreground=FG_TEXT, border=BORDER,
                                        font=tkfont.Font(family="Monospace", size=12),
                                        logo_path=os.path.join(SCRIPT_DIR, "app_icon.png"))
        self.convert_log.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        Tooltip(self.convert_log.canvas, "Every file shows up here as it's converted. Right-click to "
                                         "copy the whole log or clear it.")

    def _convert_say(self, text):
        self.convert_log.add_line(text)

    CONVERT_DEBUG_LOG = os.path.join(DATA_DIR, "converter_debug.log")

    def _diagnose_path(self, label, text):
        """
        Plain-text facts about a folder path, for tracking down "folder doesn't exist" mysteries.
        ascii() makes invisible things (a trailing space, an odd lookalike character) show up.
        """
        lines = [f"{label}: {ascii(text)}  (length {len(text)})"]
        if not text:
            lines.append("    (empty)")
            return lines
        lines.append(f"    exists={os.path.exists(text)}  isdir={os.path.isdir(text)}  "
                     f"readable={os.access(text, os.R_OK)}  enterable={os.access(text, os.X_OK)}")
        stripped = text.strip()
        if stripped != text:
            lines.append(f"    has spaces/newlines at the ends; trimmed version {ascii(stripped)} "
                         f"isdir={os.path.isdir(stripped)}")
        parent, name = os.path.split(text.rstrip("/"))
        lines.append(f"    parent {ascii(parent)}: isdir={os.path.isdir(parent)}")
        try:
            siblings = sorted(os.listdir(parent))
            close = [s for s in siblings if s.strip().casefold() == name.strip().casefold()]
            lines.append(f"    same-name entries actually in the parent: {ascii(close) if close else 'NONE'}")
            if not close:
                first = name.strip().casefold()[:4]
                lines.append(f"    entries starting similarly: {ascii([s for s in siblings if s.casefold().startswith(first)][:10])}")
        except OSError as exc:
            lines.append(f"    couldn't list the parent folder: {exc}")
        return lines

    def _convert_debug(self, title, lines, show=False):
        """Appends to converter_debug.log (next to the program); optionally shows it in the log box."""
        import time
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(self.CONVERT_DEBUG_LOG, "a", encoding="utf-8") as f:
                f.write(f"[{stamp}] {title}\n" + "\n".join(lines) + "\n\n")
        except OSError:
            pass
        if show:
            self._convert_say(f"--- {title} ---")
            for line in lines:
                self._convert_say(line)
            self._convert_say(f"(also saved in {self.CONVERT_DEBUG_LOG})")

    @staticmethod
    def _nearest_existing_folder(text, fallback=None):
        """The folder in `text` if it's there, else the closest parent that is (a folder that was emptied and
        removed by the converter no longer exists, and the file dialog errors out if told to start in it)."""
        path = (text or "").strip("\r\n\t")
        while path and not os.path.isdir(path):
            parent = os.path.dirname(path.rstrip(os.sep))
            if parent == path or not parent:
                break
            path = parent
        if path and os.path.isdir(path):
            name = os.path.basename(path.rstrip(os.sep))
            if name != name.strip():                 # a folder named " Rubber Soul " -- the picker can't open those
                path = os.path.dirname(path.rstrip(os.sep))
        return path if path and os.path.isdir(path) else (fallback or os.path.expanduser("~"))

    def _browse_convert_src(self):
        start = self._nearest_existing_folder(self.convert_src_entry.get())
        path = filedialog.askdirectory(title="Choose the folder of FLAC files", initialdir=start)
        if path and file_converter is not None:
            fixed = file_converter.resolve_folder(path)
            if fixed != path:
                self._convert_debug("Browse dropped a space from the folder name; using the real folder",
                                    [f"Browse returned {ascii(path)}", f"real folder {ascii(fixed)}"])
                path = fixed
        if path:
            info = self._diagnose_path("Browse returned", path)
            self._convert_debug("Source folder chosen with Browse", info, show=not os.path.isdir(path))
            self.convert_src_entry.delete(0, "end")
            self.convert_src_entry.insert(0, path)
            shown = self.convert_src_entry.get()
            if shown != path:
                self._convert_debug("The box does not match what Browse returned",
                                    [f"returned {ascii(path)}", f"box holds {ascii(shown)}"], show=True)
            if file_converter is not None and not self.convert_dest_entry.get().strip():
                self.convert_dest_entry.insert(0, file_converter.default_destination(path))

    def _browse_convert_dest(self):
        start = self._nearest_existing_folder(self.convert_dest_entry.get())
        path = filedialog.askdirectory(title="Choose where the MP3s should go", initialdir=start)
        if path:
            self.convert_dest_entry.delete(0, "end")
            self.convert_dest_entry.insert(0, path)

    CONVERT_STATUS_COLORS = {
        "idle": "#a89cc8",    # muted lavender (same as FG_MUTED)
        "busy": "#ffc857",    # amber: in process / cancelling
        "good": DONE_GREEN,    # green: done
        "warn": "#ffa94d",    # orange: cancelled, or done with some failures
        "error": "#ff6b6b",   # red: stopped by an error
    }

    def _set_convert_status(self, text, kind="idle"):
        self.convert_status.config(text=text, foreground=self.CONVERT_STATUS_COLORS.get(kind, "#a89cc8"))

    def _clear_convert(self):
        """Wipes the log, progress bar and status so the next folder starts on a clean screen."""
        if self._convert_thread is not None and self._convert_thread.is_alive():
            return
        self.convert_log.clear()
        self.convert_progress.config(value=0)
        self.convert_restart_btn.pack_forget()
        self.convert_retry_btn.pack_forget()
        self._retry_files = []
        self._convert_gain_before = 0
        self.convert_gain_value.config(text="\u2014")
        self._set_convert_status("Ready", "idle")

    def _restart_convert(self):
        self._start_convert(confirm=False)

    def _retry_convert(self):
        """Runs again with the same folders, remaking the MP3s that failed (see Converter's redo)."""
        self._pending_redo = set(self._retry_files)
        self._start_convert(confirm=False)

    def _start_convert(self, confirm=True):
        if self._converter is not None and not self._converter.snapshot()["finished"] \
                and self._convert_thread is not None and self._convert_thread.is_alive():
            return
        if file_converter is None:
            messagebox.showerror("File converter missing",
                                 "file_converter.py needs to sit in the same folder as this program.")
            return
        tools = file_converter.check_tools()
        if tools["problem"]:
            messagebox.showerror("ffmpeg needed", tools["problem"])
            return
        # Folder names can legitimately end (or start) with a space, so don't just strip the text:
        # trust the exact text when it names a real folder, and only fall back to trimming it.
        raw_source = self.convert_src_entry.get().strip("\r\n\t")
        source = raw_source if os.path.isdir(raw_source) else file_converter.resolve_folder(raw_source.strip())
        raw_dest = self.convert_dest_entry.get().strip("\r\n\t")
        if os.path.isdir(raw_dest) or raw_dest == file_converter.default_destination(source):
            dest = raw_dest
        else:
            dest = raw_dest.strip()
        problem = file_converter.validate_paths(source, dest)
        if problem:
            details = [f"message shown: {problem}"]
            details += self._diagnose_path("box text (source)", self.convert_src_entry.get())
            details += self._diagnose_path("path used (source)", source)
            details += self._diagnose_path("box text (destination)", self.convert_dest_entry.get())
            self._convert_debug("Convert refused the folders", details, show=True)
            messagebox.showerror("Check the folders", problem + "\n\nDetails were written to the log "
                                 "box on this tab (and to converter_debug.log next to the program).")
            return
        files, total_bytes = file_converter.scan_flacs(source)
        if not files:
            messagebox.showinfo("Nothing to convert", "There are no FLAC files in that folder.")
            return
        parent_name = self._convert_parent_for(source, dest)
        if parent_name is False:                      # cancelled at the "one artist?" question
            return
        landing = os.path.join(dest, file_converter.resolve_child(dest, parent_name)) if parent_name else dest
        delete_after = self.convert_delete_var.get()
        if delete_after:
            if not tools["ffprobe"]:
                messagebox.showerror("ffprobe needed", "Checking the MP3s before deleting needs ffprobe, which comes "
                                     "with ffmpeg.\n\nInstall it by pasting this into a terminal:\n\n    sudo apt install ffmpeg")
                return
            if not messagebox.askyesno(
                    "Convert, check, and DELETE the FLAC files?",
                    f"Convert {len(files)} FLAC file(s) ({total_bytes / 1024 ** 3:.1f} GB) to MP3, check each one, and "
                    f"then DELETE the FLACs that pass?\n\n"
                    f"From:  {source}\nTo:      {landing}\n\n"
                    "A FLAC goes only after its MP3 passes every check; anything that doesn't pass stays and is listed. "
                    "Deleted files do NOT go to the Trash.", icon="warning", default="no"):
                return
        elif confirm and not messagebox.askyesno(
                "Convert to MP3?",
                f"Convert {len(files)} FLAC file(s) ({total_bytes / 1024 ** 3:.1f} GB) to MP3?\n\n"
                f"From:  {source}\nTo:      {landing}\n\n"
                "Your FLAC files won't be changed. Anything already converted there is skipped."):
            return

        self._save_state()
        self.convert_restart_btn.pack_forget()
        self.convert_retry_btn.pack_forget()
        redo, self._pending_redo = self._pending_redo, None
        self._converter = file_converter.Converter(
            source, dest, copy_extras=self.convert_extras_var.get(), delete_after=delete_after,
            delete_log=os.path.join(DATA_DIR, "converter_deleted.log"), redo=redo, parent_name=parent_name)
        self._convert_error = None
        self._convert_summary = None
        if parent_name:
            self._convert_say(f"--- {os.path.basename(source.rstrip(os.sep)).strip()} is one artist's folder: its MP3s go into "
                              f"{landing} (used if it's already there, created if not) ---")
        if delete_after:
            self._convert_say(f"--- {len(files)} file(s) to do, {self._converter.workers} at a time, one album at a time "
                              "(smallest first): convert, check, delete, then the next ---")
        else:
            self._convert_say(f"--- Converting {len(files)} file(s), {self._converter.workers} at a time ---")
        self.convert_btn.config(state="disabled")
        self.convert_clear_btn.config(state="disabled")
        self.convert_cancel_btn.config(state="normal")
        self.convert_progress.config(maximum=max(1, len(files)), value=0)
        if not self._convert_gain_before:
            self.convert_gain_value.config(text="0 MB" if delete_after else "\u2014")
        self._set_convert_status("In process: starting", "busy")

        converter = self._converter

        def work():
            try:
                self._convert_summary = converter.run()
            except Exception as exc:  # report it instead of dying silently in the background
                self._convert_error = str(exc)

        self._convert_thread = threading.Thread(target=work, daemon=True)
        self._convert_thread.start()
        self.after(200, self._poll_convert)

    def _convert_parent_for(self, source, dest):
        """
        Is the folder picked as the source ONE artist's folder (albums inside) or a folder FULL of artist folders?
        Returns the folder name to create inside the destination for one artist, None for a folder of artists,
        or False if the person cancelled. The two look the same on disk, so it's worked out where it can be
        (the Need to Be Converted folder from the Packager tab, or the usual "<name> (MP3)" sibling destination
        are always whole collections) and otherwise asked once per source/destination pair.
        """
        key = (os.path.realpath(source), os.path.realpath(dest))
        remembered = getattr(self, "_convert_parent_memory", None)
        if remembered is None:
            remembered = self._convert_parent_memory = {}
        if key in remembered:
            return remembered[key]
        name = os.path.basename(os.path.abspath(source).rstrip(os.sep)).strip()
        answer = None
        pending = getattr(self, "pending_entry", None)
        pending_path = file_converter.resolve_folder(pending.get().strip()) if pending is not None else ""
        if dest == file_converter.default_destination(source):
            answer = None                                          # a fresh mirror folder: just copy the structure
        elif pending_path and os.path.isdir(pending_path) and os.path.realpath(pending_path) == key[0]:
            answer = None                                          # the Need to Be Converted folder holds artists
        else:
            reply = messagebox.askyesnocancel(
                "One artist, or a folder of artists?",
                f"Is \"{name}\" the folder of ONE artist?\n\n"
                f"Yes: the MP3s go into a \"{name}\" folder in the destination.\n"
                "No: it's a folder of artists, so its contents go straight into the destination.")
            if reply is None:
                return False
            answer = name if reply else None
        remembered[key] = answer
        return answer

    def _cancel_convert(self):
        if self._converter is not None:
            self._converter.cancel()
            self._set_convert_status("Cancelling...", "busy")
            self.convert_cancel_btn.config(state="disabled")

    def _poll_convert(self):
        converter = self._converter
        if converter is None:
            return
        still_running = self._convert_thread.is_alive()   # checked BEFORE reading the lines, so none are missed
        snap = converter.snapshot()
        for line in converter.drain_events():
            self._convert_say(line)
        self.convert_progress.config(maximum=max(1, snap["total"]), value=snap["done"])
        if converter.delete_after:
            total_gain = self._convert_gain_before + snap["free_gained"]
            self.convert_gain_value.config(text=file_converter.human_size(total_gain) if total_gain else "0 MB")
        if still_running:
            if not snap["cancelled"]:
                if snap["albums_total"] and snap["album_index"]:
                    self._set_convert_status(f"Album {snap['album_index']} of {snap['albums_total']}", "busy")
                elif snap["phase"] == "extras":
                    self._set_convert_status("In process: other files", "busy")
                elif snap["phase"] == "cleaning":
                    self._set_convert_status("Clearing leftovers", "busy")
                elif snap["albums_total"]:
                    self._set_convert_status(f"Album {snap['album_index']} of {snap['albums_total']}", "busy")
                else:
                    self._set_convert_status(f"In process: {snap['done']} of {snap['total']}", "busy")
            self.after(200, self._poll_convert)
            return

        # finished
        if converter.delete_after:
            self._convert_gain_before += converter.free_gained()      # keep it on screen for the next folder
        self.convert_btn.config(state="normal")
        self.convert_clear_btn.config(state="normal")
        self.convert_cancel_btn.config(state="disabled")
        if self._convert_error:
            self._set_convert_status("Stopped by an error", "error")
            self._convert_say(f"ERROR: {self._convert_error}")
            messagebox.showerror("Conversion stopped", self._convert_error)
            return
        summary = self._convert_summary
        failed = summary["failed"]
        failed_names = {rel for rel, _why in failed}
        all_kept = summary["held_back"]                                   # every FLAC that was left in place
        kept = [(rel, why) for rel, why in all_kept if rel not in failed_names]   # (failed ones are listed above)
        if failed:
            self._convert_say("--- Files that couldn't be converted ---")
            for relative, reason in failed:
                self._convert_say(f"  {relative}: {reason}")
                self._convert_say(f"      what to do: {file_converter.advice_for(reason)[0]}")
        line = (f"{summary['converted']} converted, {summary['skipped']} already there, "
                f"{len(failed)} failed")
        if summary["extras_copied"]:
            line += f", {summary['extras_copied']} extra file(s) copied"

        # What happened to the FLAC files (only when "check, then delete" was ticked)
        flac_note = "Your FLAC files were not touched."
        if summary["delete_requested"]:
            freed_bytes = summary["deleted_bytes"]
            freed = f"{freed_bytes / 1024 ** 3:.2f} GB" if freed_bytes >= 1024 ** 3 else f"{freed_bytes / 1024 ** 2:.0f} MB"
            if summary["cancelled"]:
                flac_note = (f"{summary['deleted']} FLAC file(s) were deleted before the cancel; "
                             "all the others are still there.") if summary["deleted"] else \
                            "Cancelled before anything was deleted, so all your FLAC files are still there."
            else:
                flac_note = (f"{summary['verified']} MP3(s) passed every check. "
                             f"{summary['deleted']} FLAC file(s) deleted ({freed} freed). "
                             f"Drive space recovered this run: {file_converter.human_size(summary['free_gained'])}.")
                if summary["leftovers_deleted"] or summary["folders_removed"]:
                    flac_note += (f" Cleared {summary['leftovers_deleted']} leftover file(s) and "
                                  f"{summary['folders_removed']} empty folder(s).")
                if all_kept:
                    flac_note += f" {len(all_kept)} FLAC file(s) were kept."
            if kept:
                self._convert_say("--- FLAC files that were KEPT (still in the Need to Be Converted folder) ---")
                for relative, reason in kept:
                    self._convert_say(f"  {relative}: {reason}")
                    self._convert_say(f"      what to do: {file_converter.advice_for(reason)[0]}")
            self._convert_say(flac_note)
            if summary["deleted"]:
                self._convert_say(f"How the recovered space adds up: {file_converter.human_size(summary['deleted_bytes'])} of FLACs deleted"
                                  f" - {file_converter.human_size(summary['mp3_bytes'])} for the MP3s that replaced them"
                                  f" + {file_converter.human_size(summary['junk_bytes'])} of cue sheets/logs deleted"
                                  f" = {file_converter.human_size(summary['free_gained'])}"
                                  + ("" if summary["same_drive"] else "  (the MP3s are on a different drive, so they weren't subtracted)"))
            if summary["deleted"]:
                self._convert_say(f"(the list of deleted files is in {os.path.join(DATA_DIR, 'converter_deleted.log')})")
            if summary["extras_differ"] and not summary["cancelled"]:
                flac_note += (f"\n\n{len(summary['extras_differ'])} other file(s) (pictures, booklets...) aren't in the "
                              "destination exactly as they are in the source. They were not deleted; see the log.")

        if summary["cancelled"]:
            self._set_convert_status("Cancelled", "warn")
            self.convert_restart_btn.pack(side="left", padx=(0, 8), before=self.convert_clear_btn)
            self._convert_say(f"Cancelled -- {line}. Press Restart to carry on.")
            messagebox.showinfo("Cancelled", f"{line}.\n\n{flac_note}\n\nPress Restart whenever you like and it will carry on.")
        elif summary["low_space"]:
            ls = summary["low_space"]
            self._set_convert_status("Stopped: drive full", "warn")
            self.convert_restart_btn.pack(side="left", padx=(0, 8), before=self.convert_clear_btn)
            msg = (f"The next album ({ls['album']}) needs about {file_converter.human_size(ls['need'])} for its MP3s, but "
                   f"only {file_converter.human_size(ls['free'])} is free on the MP3 drive, and "
                   f"{file_converter.human_size(ls['margin'])} is always kept spare.")
            self._convert_say(f"Stopped -- {msg}")
            self._convert_say("Every album before this one is finished. Free up some room (empty the Trash, or remove "
                              "something you don't need) and press Restart to carry on from here. The albums left are all "
                              "bigger than this one, so freeing space is the only way forward.")
            messagebox.showwarning("Stopped: not enough room",
                                   f"{line}.\n\n{flac_note}\n\n{msg}\n\nEvery album before it is finished. Free up some "
                                   "room and press Restart to carry on from here.")
        elif failed or kept:
            self._retry_files = list(summary["retry_files"])
            if self._retry_files:
                self.convert_retry_btn.pack(side="left", padx=(0, 8), before=self.convert_clear_btn)
            self._set_convert_status(f"Done, {len(failed) + len(kept)} kept" if kept and not failed
                                     else f"Done, {len(failed)} failed", "warn")
            self._convert_say(f"Done -- {line}")
            problems = [(rel, why) for rel, why in failed] + [(rel, why) for rel, why in kept]
            shown = "\n".join(f"  \u2022 {rel}: {why}" for rel, why in problems[:8])
            more = f"\n  ...and {len(problems) - 8} more (see the log)" if len(problems) > 8 else ""
            heading = "These files had problems (their FLAC files are still there):" if summary["delete_requested"] \
                else "These files couldn't be converted:"
            tips = []
            for _rel, why in problems:
                tip = file_converter.advice_for(why)[0]
                if tip not in tips:
                    tips.append(tip)
            todo = "\n".join(f"  \u2022 {t}" for t in tips[:4])
            retry_hint = ("\n\nThe Retry kept files button will try those again." if summary["retry_files"] else "")
            messagebox.showwarning("Done, with problems",
                                   f"{line}.\n\n{flac_note}\n\n{heading}\n{shown}{more}\n\nWhat to do:\n{todo}{retry_hint}")
        else:
            self._set_convert_status("Done", "good")
            self._convert_say(f"Done -- {line}")
            messagebox.showinfo("Done", f"{line}.\n\n{flac_note}")

    # -------------------------------------------------------- Search tab
    # ------------------------------------------------------------------ FLAC Verifier tab
    VERIFY_COLORS = {"ok": DONE_GREEN, "warn": "#ffd54a", "fail": "#ff8a8a"}

    def _build_verify_tab(self):
        f = self.verify_page
        self._verifier = None
        self._verify_thread = None
        self._verify_rows = {}

        header = ttk.Frame(f, style="Panel.TFrame")
        ttk.Label(header, text="Check new music before it joins your library",
                  style="Panel.TLabel", font=("", 11, "bold")).pack(side="left")
        info = ttk.Label(header, text="\u24d8", style="Panel.TLabel", foreground=INFO_ICON,
                         cursor="question_arrow", font=("", 11, "bold"))
        info.pack(side="left", padx=(6, 0))
        Tooltip(info, "Goes through every album in a folder and checks, in this order: that every song plays "
                      "all the way through, that the files really are FLAC (and what quality), that the tags "
                      "look right, and whether cover art is embedded in the files. It also saves one "
                      "spectrogram picture per album, taken from a song in the middle, for you to look at. "
                      "Spectrograms are super simple to read once you take a minute with one, and they're the "
                      "best way to spot a fake FLAC. Hover over Review Spectrograms to learn how.\n\n"
                      "Nothing in your music folders is changed. The only thing written is the pictures.")
        box = ttk.LabelFrame(f, labelwidget=header)
        box.pack(fill="x", padx=10, pady=(10, 6))

        def path_row(label, attr, browse_cmd, tip):
            row = ttk.Frame(box, style="Panel.TFrame")
            row.pack(fill="x", padx=8, pady=(8, 0))
            ttk.Label(row, text=label, style="Panel.TLabel", width=22).pack(side="left")
            entry = ttk.Entry(row)
            entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
            setattr(self, attr, entry)
            btn = RoundedButton(row, text="Browse...", command=browse_cmd)
            btn.pack(side="left")
            Tooltip(btn, tip)

        path_row("Folder to check:", "verify_src_entry", self._browse_verify_src,
                 "Pick the folder with the new music, such as your Latest Downloads folder. Every album "
                 "inside it, in any number of sub-folders, is checked.")
        path_row("Save spectrograms to:", "verify_out_entry", self._browse_verify_out,
                 "Where the spectrogram pictures go. One picture per album, named after the album.")
        self.verify_out_entry.insert(0, os.path.join(os.path.expanduser("~"), "Desktop", "Spectrograms"))
        note = ttk.Label(box, style="Panel.TLabel", foreground=FG_MUTED, wraplength=820, justify="left",
                  text="FLAC files only. Nothing in your music folders is changed. Larger folders can take a while."
                   )
        note.pack(fill="x", padx=8, pady=(6, 8))
        box.bind("<Configure>", lambda e, lbl=note: lbl.config(wraplength=max(200, e.width - 30)))

        btns = ttk.Frame(f)
        btns.pack(fill="x", padx=10, pady=(0, 6))
        self.verify_btn = RoundedButton(btns, text="Verify", command=self._start_verify)
        self.verify_btn.pack(side="left")
        Tooltip(self.verify_btn, "Runs all the checks on every album in the folder. This can take a while "
                                 "for a big batch. Changes nothing in your music folders.")
        self.verify_cancel_btn = RoundedButton(btns, text="Cancel", command=self._cancel_verify)
        self.verify_cancel_btn.pack(side="left", padx=8)
        self.verify_spec_btn = RoundedButton(btns, text="Review Spectrograms", command=self._review_spectrograms)
        self.verify_spec_btn.pack(side="left")
        Tooltip(self.verify_spec_btn,
                "Steps through the spectrogram pictures one album at a time (or double-click a row). "
                "Use the arrow keys to move, and mark each one Looks fine or Suspicious.\n\n"
                "Spectrograms are super simple to interpret. Take a minute and you'll understand what "
                "these pictures show: time runs left to right, pitch runs bottom to top (deep bass at the "
                "bottom, the highest sounds at the top), and the brighter the color, the louder that "
                "pitch is at that moment.\n\n"
                "How to spot a fake FLAC: a real lossless file has detail reaching up toward the top. A "
                "file that was converted up from an MP3 has a hard, flat ceiling, one straight line "
                "across the whole picture with empty black space above it, often around 16 kHz or "
                "20 kHz.\n\n"
                "Don't panic over a soft album, though. Quiet acoustic music can simply fade out before "
                "the top, and that's normal. A fake stops at the same sharp line the entire way "
                "through, loud parts included.")
        self.verify_folder_btn = RoundedButton(btns, text="Open Folder", command=self._open_verify_folder)
        self.verify_folder_btn.pack(side="left", padx=8)
        Tooltip(self.verify_folder_btn, "Click an album in the list below, then press Open Folder to go straight "
                                        "to that album's folder in your file manager.")
        self.verify_tags_btn = RoundedButton(btns, text="Clean Folders and Tags", command=self._verify_to_tag_cleaner)
        self.verify_tags_btn.pack(side="left")
        Tooltip(self.verify_tags_btn, "Opens the tag and folder cleanup right here, with the selected album (or the "
                                      "whole folder) filled in. Nothing is changed yet: press Scan to see what "
                                      "it finds.")
        self.verify_art_btn = RoundedButton(btns, text="Find or Embed Missing Cover Art", command=self._verify_to_art)
        self.verify_art_btn.pack(side="left", padx=8)
        Tooltip(self.verify_art_btn, "Opens the cover art screen right here, with the selected album (or the "
                                     "whole folder) filled in. If a picture is already in the album's folder, it "
                                     "gets embedded into the songs. If there isn't one, DiscWise can go online "
                                     "and find the cover for you, without leaving the app. Nothing is changed "
                                     "yet: press Scan to see what it would do.")
        status_row = ttk.Frame(f)
        status_row.pack(fill="x", padx=10, pady=(0, 4))
        legend = ttk.Frame(status_row)
        legend.pack(side="right", anchor="n", padx=(12, 4))
        for text, color, tip in (
                ("\u25cf Green: nothing to do", DONE_GREEN, "The music is fine and nothing needs cleaning up."),
                ("\u25cf Yellow: needs some cleanup", "#ffd54a",
                 "The music is fine, but its tags, folder name, cover art or spectrum need a little attention."),
                ("\u25cf Red: a real problem", "#ff8a8a", "Damaged files, or not really FLAC.")):
            label = ttk.Label(legend, text=text, foreground=color, font=("", 10))
            label.pack(anchor="w")
            Tooltip(label, tip)
        self.verify_status = ttk.Label(status_row, text="Ready", foreground=FG_MUTED, font=self.BIG_STATUS,
                                       wraplength=650, justify="left")
        self.verify_status.pack(side="left", padx=2)
        status_row.bind("<Configure>", lambda e: self.verify_status.config(
            wraplength=max(300, e.width - legend.winfo_reqwidth() - 40)))

        self.verify_progress = ttk.Progressbar(f, mode="determinate")
        self.verify_progress.pack(fill="x", padx=10, pady=(0, 6))

        verify_tip = add_tip_row(f, "The FLAC Verifier is the first step: it checks that your files are real, "
                                    "lossless FLAC and play all the way through, helps you clean up tag and folder "
                                    "name spam, and lets you embed cover art in any songs that are missing it.")
        verify_tip.pack_forget()
        verify_tip.pack(side="bottom", anchor="w", fill="x", padx=10, pady=(0, 8))   # lowest thing on the page
        log_box = ttk.Frame(f)
        log_box.pack(side="bottom", fill="x", padx=10, pady=(0, 10))
        ttk.Label(log_box, text="Log:", font=("", 11, "bold")).pack(anchor="w")
        self.verify_log = WatermarkLog(log_box, background=BG_INPUT, foreground=FG_TEXT, border=BORDER,
                                       font=tkfont.Font(family="Monospace", size=12),
                                       logo_path=os.path.join(SCRIPT_DIR, "app_icon.png"))
        self.verify_log.canvas.configure(height=180)
        self.verify_log.pack(fill="x")
        Tooltip(self.verify_log.canvas, "Every check as it happens. Right-click to copy the whole log or clear it.")

        columns = ("album", "files", "integrity", "codec", "tags", "art", "spectrum")
        frame = ttk.Frame(f)
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self.verify_tree = ttk.Treeview(frame, columns=columns, show="headings", height=6)
        heads = {"album": "Album folder", "files": "Songs", "integrity": "Plays through", "codec": "Format",
                 "tags": "Tags & Folder", "art": "Cover art", "spectrum": "Audio Spectrum"}
        widths = {"album": 260, "files": 52, "integrity": 118, "codec": 195, "tags": 150, "art": 288,
                  "spectrum": 175}
        for col in columns:
            # Everything but the album name is centered, heading and cells alike.
            anchor = "w" if col == "album" else "center"
            self.verify_tree.heading(col, text=heads[col], anchor=anchor)
            self.verify_tree.column(col, width=widths[col], minwidth=40 if col != "album" else 200,
                                    anchor=anchor, stretch=(col == "spectrum"))   # last column soaks up spare room, so Album can be widened
        scroll = tk.Scrollbar(frame, command=self.verify_tree.yview)
        hscroll = tk.Scrollbar(frame, orient="horizontal", command=self.verify_tree.xview)
        self.verify_tree.configure(yscrollcommand=scroll.set, xscrollcommand=hscroll.set)
        hscroll.pack(side="bottom", fill="x")
        scroll.pack(side="right", fill="y")
        self.verify_tree.pack(side="left", fill="both", expand=True)
        for kind, color in self.VERIFY_COLORS.items():
            self.verify_tree.tag_configure(kind, foreground=color)
        enable_column_grab(self.verify_tree)
        self.verify_tree.bind("<Double-1>", lambda e: self._review_spectrograms(
            self.verify_tree.identify_row(e.y) or None))
        for col in ("codec", "art"):               # the full text, when a column is too narrow for it
            CellTooltip(self.verify_tree, col, lambda value: value or None)

        def fixes_for(tip):
            row = self._verify_rows.get(tip._cell[0]) if tip._cell else None
            lines = (row or {}).get("tag_details") or []
            if not lines:
                return ""
            more = f"\n...and {len(lines) - 8} more" if len(lines) > 8 else ""
            return "To fix with Clean Folders and Tags:\n" + "\n".join(lines[:8]) + more

        tags_tip = CellTooltip(self.verify_tree, "tags", lambda value: (fixes_for(tags_tip) or value) or None)
        album_tip = CellTooltip(self.verify_tree, "album", lambda value: (
            ((self._verify_rows.get(album_tip._cell[0], {}).get("album") or "") +
             (("\n\n" + fixes_for(album_tip)) if fixes_for(album_tip) else "")) or None
            if album_tip._cell else None))
        CellTooltip(self.verify_tree, "spectrum", self._spectrum_tip)
        Tooltip(self.verify_tree, "Double-click a row to review its spectrogram. Hover over an album or its "
                                  "Tags & Folder cell to see what needs fixing.")

    @staticmethod
    def _folder_from_entry(entry):
        """The folder typed (or restored from last time) in an entry box. A path that is already a real folder
        is used exactly as it is; only a path that isn't one gets passed through the folder-fixer."""
        raw = entry.get().strip().strip("\"'")
        if not raw:
            return ""
        expanded = os.path.expanduser(raw)
        if os.path.isdir(expanded):
            return expanded
        return core.resolve_folder(raw) or ""

    def _browse_verify_src(self):
        path = filedialog.askdirectory(title="Choose the folder of new music to check",
                                      initialdir=self._nearest_existing_folder(self.verify_src_entry.get()))
        if path:
            self.verify_src_entry.delete(0, "end")
            self.verify_src_entry.insert(0, core.resolve_folder(path))

    def _browse_verify_out(self):
        path = filedialog.askdirectory(title="Choose where to save the spectrogram pictures",
                                      initialdir=self._nearest_existing_folder(self.verify_out_entry.get()))
        if path:
            self.verify_out_entry.delete(0, "end")
            self.verify_out_entry.insert(0, path)

    def _verify_busy(self):
        return self._verify_thread is not None and self._verify_thread.is_alive()

    def _start_verify(self):
        if self._verify_busy():
            return
        missing = flac_verifier.missing_tools()
        if missing:
            messagebox.showinfo("FLAC Verifier", "The verifier needs a couple of things installed first:\n\n" +
                                "\n".join(f"  {name}:  {how}" for name, how in missing))
            return
        folder = self._folder_from_entry(self.verify_src_entry)
        if not folder and self.downloads_entry.get().strip():
            folder = core.resolve_folder(self.downloads_entry.get().strip())
            self.verify_src_entry.insert(0, folder)
        if not folder or not os.path.isdir(folder):
            typed = self.verify_src_entry.get().strip()
            if typed:
                messagebox.showinfo("FLAC Verifier", f"Can't find this folder:\n\n{typed}\n\n"
                                                      "Common reasons:\n"
                                                      "  - the folder was moved or renamed\n"
                                                      "  - an external drive isn't plugged in\n"
                                                      "  - the path has a typo\n\n"
                                                      "Press Browse to pick it again.")
            else:
                messagebox.showinfo("FLAC Verifier", "Pick the folder of new music to check first.")
            return
        out_dir = self.verify_out_entry.get().strip()
        if not out_dir:
            messagebox.showinfo("FLAC Verifier", "Pick a folder to save the spectrogram pictures in.")
            return
        self.verify_tree.delete(*self.verify_tree.get_children())
        self._verify_rows = {}
        self.verify_log.clear()
        self.verify_progress.config(value=0, maximum=1)
        if not flac_verifier.flac_tool_present():
            self.verify_log.add_line("Note: the \"flac\" program isn't installed, so songs are checked by decoding "
                                     "them with ffmpeg instead. For the full check: sudo apt install flac")
        self.verify_log.add_line(f"Checking: {folder}")
        self._verifier = verifier = flac_verifier.Verifier(ignore_path=self.TAG_IGNORE_PATH,
                                                                 marks_path=self.VERIFY_MARKS_PATH)
        self._verify_thread = threading.Thread(target=verifier.run, args=(folder, out_dir), daemon=True)
        self._verify_thread.start()
        self.verify_btn.config(state="disabled")
        self._verify_shown = 0
        self._verify_big_noted = False
        self._poll_verify()

    def _cancel_verify(self):
        if self._verifier is not None and self._verify_busy():
            self._verifier.cancel()
            self.verify_status.config(text="Stopping...", foreground=self.BUSY_COLOR)

    def _poll_verify(self):
        verifier = self._verifier
        if verifier is None:
            return
        running = self._verify_busy()      # checked BEFORE reading, so nothing is missed
        for line in verifier.drain_events():
            self.verify_log.add_line(line)
        results = list(verifier.results)
        while self._verify_shown < len(results):
            row = results[self._verify_shown]
            iid = str(self._verify_shown)
            self._verify_rows[iid] = row
            icon = {"ok": "\u2713 ", "warn": "\u26a0 ", "fail": "\u2717 "}
            self.verify_tree.insert("", "end", iid=iid, tags=(row["overall"],), values=(
                self._short_album(row["album"]), row["files"],
                icon[row["integrity"][0]] + row["integrity"][1], icon[row["codec"][0]] + row["codec"][1],
                icon[row["tags"][0]] + row["tags"][1], icon[row["art"][0]] + row["art"][1],
                self._spectrum_cell(row)))
            self._verify_shown += 1
        snap = verifier.snapshot()
        self.verify_progress.config(maximum=max(1, snap["total"]), value=snap["done"])
        if running:
            if not snap["cancelled"]:
                big = snap["total"] > 20
                if big and not self._verify_big_noted:
                    self._verify_big_noted = True
                    self.verify_log.add_line(f"That's {snap['total']} albums. Larger folders can take a while, especially "
                                             "with hi-res files. You can leave it running.")
                self.verify_status.config(text=f"Checking album {min(snap['done'] + 1, snap['total'])} "
                                               f"of {snap['total']}...", foreground=self.BUSY_COLOR)
            self.after(300, self._poll_verify)
            return
        self.verify_btn.config(state="normal")
        if verifier.error:
            self.verify_status.config(text="Something went wrong: " + verifier.error, foreground="#ff8a8a")
            self.verify_log.add_line("Something went wrong: " + verifier.error)
            return
        if snap["cancelled"]:
            text, color = f"Stopped after {len(results)} of {snap['total']} album(s).", "#ffd54a"
        elif not snap["total"]:
            text, color = ("No FLAC albums were found in this folder. It may be empty, the music may have "
                           "been moved somewhere else, or it holds other kinds of files (like MP3s)."), "#ffd54a"
        else:
            text, color = self._verify_summary(results)
        self.verify_status.config(text=text, foreground=color)
        self.verify_log.add_line(text)
        if results and any(r["spectrogram"] for r in results):
            self.verify_log.add_line(f"Spectrogram pictures are in: {self.verify_out_entry.get().strip()}")
            self.verify_log.add_line("Look them over to make sure the music is truly lossless, not "
                                     "lossy files converted up to FLAC.")

    @staticmethod
    def _short_album(album):
        """The last two parts of the album's path: enough to tell albums apart without the long front."""
        parts = [p for p in album.split(os.sep) if p]
        return " / ".join(parts[-2:]) if len(parts) > 1 else album

    @staticmethod
    def _spectrum_cell(row):
        status, text = row["spectrum"]
        mark = {"fine": "  \u2713", "suspicious": "  \u2717"}.get(row.get("mark"), "")
        return ("\u26a0 " if status == "warn" else "\u2713 ") + text + mark

    @staticmethod
    def _spectrum_tip(value):
        if "Possible MP3" in value:
            return ("The sound stops sharply at a frequency typical of MP3 files. Look at the spectrogram: a "
                    "flat ceiling with empty space above it means it was probably converted from MP3.")
        if "Possible upsample" in value:
            return ("Nothing above about 22 kHz in a hi-res file, like a CD-quality source stretched up. "
                    "Look at the spectrogram to see if it's real.")
        if "Check" in value:
            return ("The sound stops or fades out early. That's normal for soft, acoustic or older "
                    "recordings, so look at the spectrogram before judging it.")
        if "Full range" in value:
            return "The sound reaches up near the top of the range, which is what a real lossless file does."
        return None

    def _review_spectrograms(self, start_iid=None):
        """A viewer that steps through the spectrogram pictures, so nothing needs opening one by one."""
        items = [(iid, row) for iid, row in sorted(self._verify_rows.items(), key=lambda kv: int(kv[0]))
                 if row["spectrogram"] and os.path.isfile(row["spectrogram"])]
        if not items:
            messagebox.showinfo("FLAC Verifier", "There are no spectrogram pictures yet. Run Verify first.")
            return
        picked = start_iid or (self.verify_tree.selection() or [None])[0]
        if not HAVE_PIL:
            row = self._verify_rows.get(picked) or items[0][1]
            self._open_path(row["spectrogram"])
            return

        win = tk.Toplevel(self)
        win.title("Review Spectrograms")
        win.geometry("1120x760")
        win.transient(self)
        back = ttk.Style().lookup("TFrame", "background") or "#1a1530"
        body = ttk.Frame(win)
        body.pack(fill="both", expand=True, padx=10, pady=10)
        title = ttk.Label(body, font=("", 12, "bold"))
        title.pack(anchor="w")
        verdict = ttk.Label(body, foreground=FG_MUTED, wraplength=1060, justify="left")
        verdict.pack(anchor="w", pady=(2, 6))
        picture = tk.Label(body, background=back, borderwidth=0)
        picture.pack(fill="both", expand=True)
        howto = ttk.Label(body, foreground=FG_MUTED, wraplength=1060, justify="left",
                          text="Real music reaches up toward the top. A flat, hard ceiling with empty black space "
                               "above it, the same height all the way through, means it was converted up from "
                               "MP3. A soft album fading out early is normal.")
        howto.pack(anchor="w", pady=(6, 6))
        bar = ttk.Frame(body)
        bar.pack(fill="x")
        only = tk.BooleanVar(value=False)
        state = {"list": items, "i": 0, "photo": None}

        def rebuild(keep=None):
            pool = [it for it in items if not only.get() or it[1]["spectrum"][0] == "warn"] or items
            state["list"] = pool
            state["i"] = next((n for n, it in enumerate(pool) if it[0] == keep), 0)
            show()

        def show():
            iid, row = state["list"][state["i"]]
            title.config(text=f"{self._short_album(row['album'])}     ({state['i'] + 1} of {len(state['list'])})")
            status, text, = row["spectrum"]
            verdict.config(text=f"Spectrum hint: {text}" + (f"      You marked it: {row['mark']}" if row.get("mark") else ""),
                           foreground="#ffd54a" if status == "warn" else DONE_GREEN)
            try:
                image = Image.open(row["spectrogram"])
                room_w = max(400, picture.winfo_width() - 4)
                room_h = max(300, picture.winfo_height() - 4)
                scale = min(room_w / image.width, room_h / image.height, 1.5)
                image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))))
                state["photo"] = ImageTk.PhotoImage(image)
                picture.config(image=state["photo"], text="")
            except Exception:
                picture.config(image="", text="Couldn't open that picture.", foreground=FG_TEXT)
            self.verify_tree.selection_set(iid)
            self.verify_tree.see(iid)

        def step(delta):
            state["i"] = (state["i"] + delta) % len(state["list"])
            show()

        def mark(kind):
            iid, row = state["list"][state["i"]]
            row["mark"] = kind
            # "Looks fine" counts as checked: remembered for next time, and no longer flagged
            row["spectrum"] = ("ok", row["spectrum"][1]) if kind == "fine" else row.get("spectrum_orig", row["spectrum"])
            marks = flac_verifier.load_marks(self.VERIFY_MARKS_PATH)
            marks[tag_cleaner.album_key(row["folder"])] = kind
            flac_verifier.save_marks(self.VERIFY_MARKS_PATH, marks)
            statuses = [row[k][0] for k in ("integrity", "codec", "tags", "art", "spectrum")]
            row["overall"] = "fail" if "fail" in statuses else "warn" if "warn" in statuses else "ok"
            self.verify_tree.item(iid, tags=(row["overall"],))
            self.verify_tree.set(iid, "spectrum", self._spectrum_cell(row))
            if not self._verify_busy():
                text, color = self._verify_summary(list(self._verify_rows.values()))
                self.verify_status.config(text=text, foreground=color)
            step(1)

        RoundedButton(bar, text="\u25c0 Previous", command=lambda: step(-1)).pack(side="left")
        RoundedButton(bar, text="Next \u25b6", command=lambda: step(1)).pack(side="left", padx=8)
        RoundedButton(bar, text="Looks fine", command=lambda: mark("fine")).pack(side="left", padx=(16, 8))
        RoundedButton(bar, text="Suspicious", command=lambda: mark("suspicious")).pack(side="left")
        skip_box = ttk.Checkbutton(bar, text="Skip the Full range ones", variable=only,
                                   command=lambda: rebuild(state["list"][state["i"]][0]))
        skip_box.pack(side="left", padx=16)
        Tooltip(skip_box, "Only steps through the albums the spectrum hint flagged (Check, Possible MP3 or "
                          "Possible upsample), and skips the ones that reached the top of the range.")
        RoundedButton(bar, text="Close", command=win.destroy).pack(side="right")
        win.bind("<Left>", lambda e: step(-1))
        win.bind("<Right>", lambda e: step(1))
        win.bind("<Escape>", lambda e: win.destroy())
        win.update_idletasks()
        win.after(80, lambda: rebuild(picked))
        win.focus_set()

    @staticmethod
    def _verify_summary(results):
        """The result in plain words: how many albums are real FLAC that plays through, then what
        still needs doing, each with its own count."""
        def albums(n):
            return f"{n} album" if n == 1 else f"{n} albums"

        def need(n):
            return "needs" if n == 1 else "need"

        total = len(results)
        damaged = sum(1 for r in results if r["integrity"][0] == "fail")
        not_flac = sum(1 for r in results if r["codec"][0] == "fail")
        good = sum(1 for r in results if r["integrity"][0] != "fail" and r["codec"][0] != "fail")
        tags = sum(1 for r in results if r.get("tag_issue"))
        folders = sum(1 for r in results if r.get("folder_issue"))
        art = sum(1 for r in results if r["art"][0] != "ok")
        suspect = sum(1 for r in results
                      if r["spectrum"][0] == "warn" and r["spectrum"][1].startswith("Possible"))
        look = sum(1 for r in results if r["spectrum"][0] == "warn" and r["spectrum"][1].startswith("Check"))

        sentences = [f"Checked {albums(total)}."]
        if good == total:
            sentences.append("All of them are real FLAC files that play all the way through." if total > 1
                             else "It's a real FLAC file that plays all the way through.")
        else:
            sentences.append(f"{good} of {total} " + ("is a real FLAC album that plays" if good == 1
                                                     else "are real FLAC albums that play") +
                             " all the way through.")
        if damaged:
            sentences.append(f"{albums(damaged)} {'has' if damaged == 1 else 'have'} damaged files.")
        if not_flac:
            sentences.append(f"{albums(not_flac)} {'is' if not_flac == 1 else 'are'} not really FLAC.")
        if suspect:
            sentences.append(f"{albums(suspect)} may be converted from MP3 "
                             f"or stretched up from a lower quality, so look at {'its' if suspect == 1 else 'their'} "
                             f"spectrogram{'' if suspect == 1 else 's'}.")
        todo = []
        if look:
            todo.append(f"{albums(look)} {need(look)} a closer look at the spectrogram")
        if tags:
            todo.append(f"{albums(tags)} {need(tags)} a tag review")
        if folders:
            todo.append(f"{folders if todo else albums(folders)} {need(folders)} a folder name cleanup")
        if art:
            todo.append(f"{art if todo else albums(art)} {need(art)} cover art")
        if todo:
            joined = todo[0] if len(todo) == 1 else ", ".join(todo[:-1]) + ", and " + todo[-1]
            sentences.append(joined + ".")
        elif not damaged and not not_flac and not suspect:
            sentences.append("Folders and tags are clean, and every album has embedded cover art. Your music is ready for the next step: head to Create Packing List.")
        color = "#ff8a8a" if damaged or not_flac else "#ffd54a" if (todo or suspect) else DONE_GREEN
        return " ".join(sentences), color

    def _selected_verify_row(self):
        picked = self.verify_tree.selection()
        return self._verify_rows.get(picked[0]) if picked else None

    @staticmethod
    def _open_path(path):
        try:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            messagebox.showinfo("Open", "Couldn't open that. It's here:\n" + path)

    def _open_verify_spec(self):
        row = self._selected_verify_row()
        if not row:
            messagebox.showinfo("FLAC Verifier", "Click an album in the list first.")
            return
        if not row["spectrogram"] or not os.path.isfile(row["spectrogram"]):
            messagebox.showinfo("FLAC Verifier", "There's no spectrogram picture for that album.")
            return
        self._open_path(row["spectrogram"])

    def _open_verify_folder(self):
        row = self._selected_verify_row()
        if not row:
            messagebox.showinfo("FLAC Verifier", "Click an album in the list first.")
            return
        self._open_path(row["folder"])

    def _verify_to_tag_cleaner(self):
        row = self._selected_verify_row()
        path = row["folder"] if row else self._folder_from_entry(self.verify_src_entry)
        if not path:
            messagebox.showinfo("FLAC Verifier", "Pick a folder first, or click an album in the list.")
            return
        self.tag_folder_entry.delete(0, "end")
        self.tag_folder_entry.insert(0, path)
        self._show_verify_view("tags")

    def _show_verify_view(self, name):
        """The FLAC Verifier page has three views (the results, the tag and folder cleanup, the cover art
        embedding). Only one is shown; the others keep their lists and settings while hidden."""
        views = {"verify": self.verify_page, "tags": self.tags_tab, "art": self.art_tab}
        for key, frame in views.items():
            if key != name:
                frame.pack_forget()
        views[name].pack(fill="both", expand=True)

    def _rerun_verifier(self, source_entry):
        folder = self._folder_from_entry(source_entry)
        if not folder or not os.path.isdir(folder):
            messagebox.showinfo("FLAC Verifier", "Pick a folder on this tab first.")
            return
        self.notebook.select(self.verify_tab)
        self._show_verify_view("verify")
        if self._verify_busy():
            return
        self.verify_src_entry.delete(0, "end")
        self.verify_src_entry.insert(0, folder)
        self._start_verify()

    def _verify_to_art(self):
        row = self._selected_verify_row()
        path = row["folder"] if row else self._folder_from_entry(self.verify_src_entry)
        if not path:
            messagebox.showinfo("FLAC Verifier", "Pick a folder first, or click an album in the list.")
            return
        self.art_folder_entry.delete(0, "end")
        self.art_folder_entry.insert(0, path)
        self._show_verify_view("art")

    # ------------------------------------------------------------------ Tag Cleaner tab
    BIG_STATUS = ("", 18, "bold")      # status and results: big enough to read from across the room
    BUSY_COLOR = "#ffd54a"             # status text while something is running (same gold as "Stopped")
    TAG_LOG_PATH = os.path.join(DATA_DIR, "tag_cleaner_log.jsonl")
    TAG_IGNORE_PATH = os.path.join(DATA_DIR, "tag_cleaner_ignore.json")
    VERIFY_MARKS_PATH = os.path.join(DATA_DIR, "verifier_spectrogram_marks.json")
    TAG_COLORS = {"disc": "#7cc4ff", "folder": "#9fe3c0", "files": "#ffb870", "ignored": "#8a86a3"}

    def _build_tags_tab(self):
        f = self.tags_tab
        self._tag_findings = []
        self._tag_checked = set()
        self._tag_ignored = set()
        self._tag_thread = None
        self._tag_state = {"busy": False, "text": "", "findings": None, "error": None}

        header = ttk.Frame(f, style="Panel.TFrame")
        ttk.Label(header, text="Clean up tags and folder names",
                  style="Panel.TLabel", font=("", 11, "bold")).pack(side="left")
        info = ttk.Label(header, text="\u24d8", style="Panel.TLabel", foreground=INFO_ICON,
                         cursor="question_arrow", font=("", 11, "bold"))
        info.pack(side="left", padx=(6, 0))
        Tooltip(info, "Downloads sometimes stick extra text into the tags inside the files: on the end of "
                      "every song title, like \"(2017 Remaster)\", or onto the artist or album name, like "
                      "\"Harvest [FLAC]\" or a web address. The file names look normal, so it's easy to "
                      "miss, but music players show the tags.\n\n"
                      "This tab scans a folder and lists song titles, artist names and album names that "
                      "carry extra text at the end. It also looks at the album folders themselves, like "
                      "\"[1974] - It's Only Rock 'N Roll [1986 Remaster]\" or \"1991 - Ten\", and "
                      "suggests a plain name (green rows). It doesn't decide what's spam. You read the "
                      "list and tick the ones you want fixed. If several disc folders of one album would end up with "
                      "the same album name once cleaned, they're numbered Disc 1, Disc 2... so players "
                      "keep them apart. Those rows are blue and say Disc numbering, since they're a tag fix and "
                      "not spam. Only tags and folder names change, never the audio, and the old values are "
                      "saved so you can undo.\n\n"
                      "After you make changes, go back to the FLAC Verifier and run it again to confirm.")
        box = ttk.LabelFrame(f, labelwidget=header)
        box.pack(fill="x", padx=10, pady=(10, 6))

        row = ttk.Frame(box, style="Panel.TFrame")
        row.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Label(row, text="Folder to check:", style="Panel.TLabel", width=18).pack(side="left")
        self.tag_folder_entry = ttk.Entry(row)
        self.tag_folder_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        browse = RoundedButton(row, text="Browse...", command=self._browse_tag_folder)
        browse.pack(side="left")
        Tooltip(browse, "Pick a folder of FLAC or MP3 files. Everything inside it, in any number of sub-folders, "
                        "is checked. Works on one album or your whole collection.")
        note = ttk.Label(box, style="Panel.TLabel", foreground=FG_MUTED, wraplength=820, justify="left",
                  text="Works with both FLAC and MP3 files (MP3s are checked for repeated song-title endings). "
                       "Nothing changes until you tick rows and press Perform Cleanup. "
                       "Clean the tags first, then scan again and do the green folder rows, since folder "
                       "names come from the album tag. Orange rows are leftover .txt and .m3u files from the "
                       "download; they're moved aside, not deleted, and .cue sheets are never touched. Want "
                       "something left as it is, like a live show's venue and date in a song title? Mark "
                       "its Ignore box; it's remembered. After you make changes, go back to the FLAC Verifier and "
                       "run it again to confirm."
                   )
        note.pack(fill="x", padx=8, pady=(6, 8))
        box.bind("<Configure>", lambda e, lbl=note: lbl.config(wraplength=max(200, e.width - 30)))

        btns = ttk.Frame(f)
        btns.pack(fill="x", padx=10, pady=(0, 6))
        self.tag_back_btn = RoundedButton(btns, text="\u2190 Back to FLAC Verifier",
                                          command=lambda: self._show_verify_view("verify"))
        self.tag_back_btn.pack(side="left", padx=(0, 8))
        Tooltip(self.tag_back_btn, "Returns to the FLAC Verifier results without running anything. After you make "
                                   "changes here, go back and run the FLAC Verifier again to confirm.")
        self.tag_scan_btn = RoundedButton(btns, text="Scan", command=self._start_tag_scan)
        self.tag_scan_btn.pack(side="left")
        Tooltip(self.tag_scan_btn, "Looks at the tags in every album and lists any text that repeats on nearly "
                                   "every song. Changes nothing.")
        self.tag_apply_btn = RoundedButton(btns, text="Perform Cleanup", command=self._apply_tag_cleaning)
        self.tag_apply_btn.pack(side="left", padx=8)
        Tooltip(self.tag_apply_btn, "Removes the repeated text from every ticked row. The old values are saved "
                                    "first so Undo can put them back. Afterward, run the FLAC Verifier again to confirm.")
        self.tag_undo_btn = RoundedButton(btns, text="Undo Last Cleanup", command=self._undo_tag_cleaning)
        self.tag_undo_btn.pack(side="left", padx=8)
        Tooltip(self.tag_undo_btn, "Puts back the tags from the most recent cleanup. Run the FLAC Verifier again "
                                   "afterward to confirm.")
        self.tag_edit_btn = RoundedButton(btns, text="Edit Tags by Hand...", command=self._open_tag_editor)
        self.tag_edit_btn.pack(side="left", padx=8)
        Tooltip(self.tag_edit_btn, "Opens a window to fix the tags of ONE album yourself: the album, artist, year "
                                   "and genre for the whole album at once, or one song's track number, title or "
                                   "artist. Works on FLAC and MP3. Nothing is written until you press Save, and "
                                   "Undo Last Cleanup takes it back. Afterward, run the FLAC Verifier again to confirm.")
        status_row = ttk.Frame(f)
        status_row.pack(fill="x", padx=10, pady=(0, 4))
        self.tag_status = ttk.Label(status_row, text="Ready", foreground=FG_MUTED, font=self.BIG_STATUS, justify="left")
        self.tag_status.pack(side="left", padx=2)
        status_row.bind("<Configure>", lambda e: self.tag_status.config(wraplength=max(300, e.width - 20)))

        legend = ttk.Frame(f)
        legend.pack(fill="x", padx=10, pady=(0, 4))
        ttk.Label(legend, text="What the colors mean:", foreground=FG_MUTED).pack(side="left")
        for text, color, tip in (
                ("\u25cf Tag fix", FG_TEXT, "White: extra text on a song title, artist or album tag."),
                ("\u25cf Disc numbering", self.TAG_COLORS["disc"],
                 "Blue: separate disc folders of one album would end up with the same album name, so "
                 "they're numbered Disc 1, Disc 2... to keep them apart. Not spam, just a tag fix."),
                ("\u25cf Folder rename", self.TAG_COLORS["folder"],
                 "Green: the album folder gets a plain name, without the year, brackets or stamps. "
                 "Nothing inside the folder changes."),
                ("\u25cf Extra files", self.TAG_COLORS["files"],
                 "Orange: leftover .txt and .m3u files that come with downloads, like rip logs, quality "
                 "reports and playlists. Most people never open them and they only add clutter. They're moved "
                 "to a holding folder, never deleted, so Undo can bring them back. .cue sheets are never "
                 "touched. Don't want them cleared out? Mark the row's Ignore box."),
                ("\u25cf Ignored", self.TAG_COLORS["ignored"],
                 "Grey: you marked it Ignore. It's left exactly as it is, and the FLAC Verifier "
                 "counts it as clean. Untick Ignore to bring it back.")):
            label = ttk.Label(legend, text=text, foreground=color)
            label.pack(side="left", padx=(14, 0))
            Tooltip(label, tip)

        # The log sits along the bottom (packed first so it always keeps its room); the table gets the rest.
        log_box = ttk.Frame(f)
        log_box.pack(side="bottom", fill="x", padx=10, pady=(0, 10))
        ttk.Label(log_box, text="Log:", font=("", 11, "bold")).pack(anchor="w")
        self.tag_log = WatermarkLog(log_box, background=BG_INPUT, foreground=FG_TEXT, border=BORDER,
                                    font=tkfont.Font(family="Monospace", size=12),
                                    logo_path=os.path.join(SCRIPT_DIR, "app_icon.png"))
        self.tag_log.canvas.configure(height=260)
        self.tag_log.pack(fill="x")
        Tooltip(self.tag_log.canvas, "What the scan found and what each cleanup changed. Right-click to "
                                     "copy the whole log or clear it.")

        # The tip sits just above the log, under the table, so it doesn't interrupt the colors-then-table reading order.
        tag_tip_row = add_tip_row(f, "After you clean up or edit tags, press Back to FLAC Verifier and run the verifier "
                                     "again to confirm everything is fixed.")
        tag_tip_row.pack_configure(side="bottom")

        columns = ("fix", "ignore", "album", "where", "ending", "tracks", "example")
        frame = ttk.Frame(f)
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self.tag_tree = ttk.Treeview(frame, columns=columns, show="headings", height=6)
        heads = {"fix": "\u2610", "ignore": "\u2610 Ignore", "album": "Album folder", "where": "Where",
                 "ending": "Repeated text", "tracks": "Songs", "example": "Before \u2192 After"}
        widths = {"fix": 34, "ignore": 78, "album": 190, "where": 140, "ending": 120, "tracks": 60,
                  "example": 720}
        for col in columns:
            self.tag_tree.heading(col, text=heads[col],
                                  command={"fix": self._tag_check_all, "ignore": self._tag_ignore_all}.get(col, ""))
            if col in ("fix", "ignore"):   # just a box: fixed, minimal width, same for every row
                self.tag_tree.column(col, width=widths[col], minwidth=widths[col], stretch=False, anchor="center")
            elif col == "example":  # long on purpose: the bar along the bottom scrolls to the end of it
                self.tag_tree.column(col, width=widths[col], minwidth=widths[col], stretch=True, anchor="w")
            else:
                self.tag_tree.column(col, width=widths[col], minwidth=40, stretch=False,
                                     anchor="center")
        scroll = tk.Scrollbar(frame, command=self.tag_tree.yview)
        hscroll = tk.Scrollbar(frame, orient="horizontal", command=self.tag_tree.xview)
        self.tag_tree.configure(yscrollcommand=scroll.set, xscrollcommand=hscroll.set)
        hscroll.pack(side="bottom", fill="x")
        scroll.pack(side="right", fill="y")
        self.tag_tree.pack(side="left", fill="both", expand=True)
        self.tag_tree.tag_configure("disc", foreground="#7cc4ff")   # disc numbering: a different kind of fix
        self.tag_tree.tag_configure("folder", foreground=self.TAG_COLORS["folder"])   # folder names: renames
        self.tag_tree.tag_configure("files", foreground=self.TAG_COLORS["files"])     # extra .txt / .m3u files
        self.tag_tree.tag_configure("ignored", foreground=self.TAG_COLORS["ignored"])
        self.tag_tree.bind("<Button-1>", self._tag_row_clicked)
        enable_column_grab(self.tag_tree, fixed=("fix", "ignore"))
        Tooltip(self.tag_tree, "Click a row to tick it for cleanup. Click its Ignore box to leave it exactly as "
                               "it is, and not be asked again. The boxes in the column headings tick or "
                               "ignore every row at once. Drag the bar along the bottom to see the whole "
                               "Before \u2192 After.")
        tag_album_tip = CellTooltip(self.tag_tree, "album", lambda value: (
            self._tag_findings[int(tag_album_tip._cell[0])]["album"]
            if tag_album_tip._cell and tag_album_tip._cell[0].isdigit()
            and int(tag_album_tip._cell[0]) < len(self._tag_findings) else None))
        CellTooltip(self.tag_tree, "example",
                    lambda value: value.replace("  \u2192  ", "\n\u2192  ", 1) if value else None)

    def _browse_tag_folder(self):
        path = filedialog.askdirectory(title="Choose a folder of FLAC or MP3 files to check",
                                       initialdir=self._nearest_existing_folder(self.tag_folder_entry.get()))
        if path:
            self.tag_folder_entry.delete(0, "end")
            self.tag_folder_entry.insert(0, core.resolve_folder(path))

    def _tag_busy(self):
        return self._tag_thread is not None and self._tag_thread.is_alive()

    def _tag_row_clicked(self, event):
        if self.tag_tree.identify_region(event.x, event.y) != "cell":
            return                                  # headings, dividers and blank space: leave them to the table itself
        row = self.tag_tree.identify_row(event.y)
        if not row or self._tag_busy():
            return
        item = self._tag_findings[int(row)]
        if self.tag_tree.identify_column(event.x) == "#2":      # the Ignore box
            item["ignored"] = not item["ignored"]
            if item["ignored"]:
                self._tag_ignored.add(item["id"])
                self._tag_checked.discard(row)
            else:
                self._tag_ignored.discard(item["id"])
            tag_cleaner.save_ignored(self.TAG_IGNORE_PATH, self._tag_ignored)
        else:
            if item["ignored"]:                       # ticking an ignored row brings it back
                item["ignored"] = False
                self._tag_ignored.discard(item["id"])
                tag_cleaner.save_ignored(self.TAG_IGNORE_PATH, self._tag_ignored)
            if row in self._tag_checked:
                self._tag_checked.discard(row)
            else:
                self._tag_checked.add(row)
        self._paint_tag_row(row)
        return "break"

    def _paint_tag_row(self, row, headers=True):
        item = self._tag_findings[int(row)]
        self.tag_tree.set(row, "fix", "\u2611" if row in self._tag_checked else "\u2610")
        self.tag_tree.set(row, "ignore", "\u2612" if item["ignored"] else "\u2610")
        kind = ("ignored" if item["ignored"] else "disc" if item.get("type") == "disc"
                else "folder" if item["kind"] == "folder" else "files" if item["kind"] == "files" else None)
        self.tag_tree.item(row, tags=(kind,) if kind else ())
        if headers:
            self._refresh_tag_headers()

    def _refresh_tag_headers(self):
        rows = self.tag_tree.get_children()
        usable = {r for r in rows if not self._tag_findings[int(r)]["ignored"]}
        ticked = bool(usable) and self._tag_checked >= usable
        ignored = bool(rows) and all(self._tag_findings[int(r)]["ignored"] for r in rows)
        self.tag_tree.heading("fix", text="\u2611" if ticked else "\u2610")
        self.tag_tree.heading("ignore", text=("\u2612" if ignored else "\u2610") + " Ignore")

    def _tag_ignore_all(self):
        """The Ignore box in the heading: ignore every row, or (if all are already ignored) bring them all back.
        Works on every row in the table, whatever part of it is showing."""
        if self._tag_busy():
            return
        rows = list(self.tag_tree.get_children())
        if not rows:
            return
        turn_on = not all(self._tag_findings[int(r)]["ignored"] for r in rows)
        for r in rows:
            item = self._tag_findings[int(r)]
            item["ignored"] = turn_on
            if turn_on:
                self._tag_ignored.add(item["id"])
                self._tag_checked.discard(r)
            else:
                self._tag_ignored.discard(item["id"])
        tag_cleaner.save_ignored(self.TAG_IGNORE_PATH, self._tag_ignored)
        for r in rows:
            self._paint_tag_row(r, headers=False)
        self._refresh_tag_headers()

    def _tag_check_all(self):
        """The tick box in the heading: tick every row that isn't ignored, or (if all are ticked) clear them."""
        if self._tag_busy():
            return
        rows = list(self.tag_tree.get_children())
        if not rows:
            return
        usable = {r for r in rows if not self._tag_findings[int(r)]["ignored"]}     # ignored rows stay as they are
        skipped = len(rows) - len(usable)
        if not usable:
            self.tag_status.config(text="Every row is set to Ignore, so there is nothing to tick. Press the Ignore "
                                        "box in the heading to bring them back.", foreground="#ffd54a")
            return
        all_on = self._tag_checked >= usable
        self._tag_checked = set() if all_on else set(usable)
        for row in rows:
            self._paint_tag_row(row, headers=False)
        self._refresh_tag_headers()
        if skipped and not all_on:
            self.tag_status.config(text=f"Ticked {len(usable)} row(s); {skipped} ignored row(s) were left alone.",
                                   foreground=FG_MUTED)

    def _start_tag_scan(self):
        if self._tag_busy():
            return
        if not tag_cleaner.mutagen_ready():
            messagebox.showinfo("Tag Cleaner", "The Tag Cleaner needs the \"mutagen\" add-on.\n\n"
                                "Install it with:\n  pip install mutagen --break-system-packages")
            return
        folder = self._folder_from_entry(self.tag_folder_entry)
        if not folder or not os.path.isdir(folder):
            messagebox.showinfo("Tag Cleaner", "Pick a folder to check first.")
            return
        self.tag_tree.delete(*self.tag_tree.get_children())
        self._tag_findings, self._tag_checked = [], set()
        self.tag_log.add_line(f"Scanning: {folder}")
        self.tag_status.config(font=self.BIG_STATUS)
        self._tag_ignored = tag_cleaner.load_ignored(self.TAG_IGNORE_PATH)
        ignored_now = set(self._tag_ignored)
        state = self._tag_state = {"text": "Starting...", "findings": None, "error": None, "stats": {}}

        def work():
            try:
                def progress(i, total, _folder):
                    state["text"] = f"Checking album {i} of {total}..."
                state["findings"] = tag_cleaner.scan(folder, progress=progress, stats=state["stats"],
                                                     ignored=ignored_now, include_mp3=True)
            except Exception as exc:  # shown in the status, never a crash
                state["error"] = str(exc)

        self._tag_thread = threading.Thread(target=work, daemon=True)
        self._tag_thread.start()
        self._poll_tag_scan()

    def _poll_tag_scan(self):
        state = self._tag_state
        if self._tag_busy():
            self.tag_status.config(text=state["text"], foreground=self.BUSY_COLOR, font=self.BIG_STATUS)
            self.after(200, self._poll_tag_scan)
            return
        if state["error"]:
            self.tag_status.config(text="Something went wrong: " + state["error"], foreground="#ff8a8a")
            self.tag_log.add_line("Something went wrong: " + state["error"])
            return
        # the ones you chose to ignore go to the bottom, still visible so you can bring them back
        self._tag_findings = sorted(state["findings"] or [], key=lambda item: item["ignored"])
        for i, item in enumerate(self._tag_findings):
            example = f'{item["before"]}  \u2192  {item["after"]}'
            self.tag_tree.insert("", "end", iid=str(i), values=(
                "\u2610", "\u2610", self._short_album(item["album"]), item["where"], item["ending"],
                f'{item["count"]} file(s)' if item["kind"] == "files" else f'{item["count"]} of {item["total"]}',
                example))
            self._paint_tag_row(str(i))
        stats = state.get("stats") or {}
        checked, flagged = stats.get("albums", 0), stats.get("flagged", 0)
        left_alone = stats.get("ignored", 0)
        discs = sum(1 for item in self._tag_findings if item.get("type") == "disc" and not item["ignored"])
        for item in self._tag_findings:
            if item["ignored"]:
                self.tag_log.add_line(f'  Ignored (left as is): {item["album"]}  |  {item["where"]}')
                continue
            if item["kind"] == "folder":
                self.tag_log.add_line(f'  Found: {item["album"]}  |  {item["where"]}  |  rename to '
                                      f'"{item["after"]}"')
                continue
            if item["kind"] == "files":
                self.tag_log.add_line(f'  Found: {item["album"]}  |  {item["where"]}  |  move aside: '
                                      f'{item["before"]}')
                continue
            self.tag_log.add_line(f'  Found: {item["album"]}  |  {item["where"]}  |  "{item["ending"]}"  '
                                  f'({item["count"]} of {item["total"]} songs)')
        self.tag_log.add_line(f"Scan finished: checked {checked} album(s), {flagged} with something to fix, "
                              f"{checked - flagged} clean.")
        if left_alone:
            self.tag_log.add_line(f"  {left_alone} item(s) you chose to ignore (grey) are counted as clean.")
        folder_rows = sum(1 for item in self._tag_findings if item["kind"] == "folder" and not item["ignored"])
        if folder_rows:
            self.tag_log.add_line(f"  {folder_rows} of the rows are folder names (green): the folder gets renamed, "
                                  f"nothing inside it changes.")
        extra_rows = sum(1 for item in self._tag_findings if item["kind"] == "files" and not item["ignored"])
        if extra_rows:
            self.tag_log.add_line(f"  {extra_rows} of the rows are extra files (orange): leftover .txt and .m3u "
                                  f"files that get moved aside, never deleted. .cue sheets are left alone.")
        if discs:
            self.tag_log.add_line(f"  {discs} of the rows are disc numbering (blue): not spam, but a tag fix "
                                  f"so separate discs don't merge into one album in a player.")
        big = self.BIG_STATUS       # the final result of a scan gets real emphasis
        if flagged:
            self.tag_status.config(
                text=f"Checked {checked} album(s): {flagged} to fix, {checked - flagged} clean.",
                foreground="#ffd54a", font=big)
        elif self._tag_findings:
            self.tag_status.config(text=f"Checked {checked} album(s): all clean (some ignored).",
                                   foreground=DONE_GREEN, font=big)
        elif checked:
            self.tag_status.config(text=f"Checked {checked} album(s): all clean. Nothing to fix.",
                                   foreground=DONE_GREEN, font=big)
        else:
            self.tag_status.config(text="No albums with 3 or more FLAC or MP3 files were found in that folder.",
                                   foreground=FG_MUTED, font=("", 11))

    def _apply_tag_cleaning(self):
        if self._tag_busy():
            return
        chosen = [self._tag_findings[int(r)] for r in sorted(self._tag_checked, key=int)]
        if not chosen:
            messagebox.showinfo("Tag Cleaner", "Tick at least one album in the list first.")
            return
        songs = sum(item["count"] for item in chosen if item["kind"] not in ("folder", "files"))
        folders = sum(1 for item in chosen if item["kind"] == "folder")
        extras = sum(item["count"] for item in chosen if item["kind"] == "files")
        extra = "Here's what will happen:\n" + self._describe_tag_items(chosen) + "\n\n"
        parts = []
        if songs:
            parts.append("tags")
        if folders:
            parts.append("folder names")
        what = " and ".join(parts) if parts else "extra files"
        covering = f", covering {songs} song entries," if songs else ""
        tail = (f"Only {what} change, never the audio. " if parts else "") + (
            f"{extras} leftover file(s) that come with downloads (rip logs, quality reports, playlists) will be "
            "cleared out of your album folders. Most people never open them and they only add clutter. They're "
            "set aside in a holding folder, not deleted. Your .cue sheets stay where they are. "
            if extras else "") + "The old values are saved, so you can press Undo Last Cleanup to put them back."
        if not messagebox.askyesno(
                "Clean up" if (extras or folders) else "Edit tags",
                f"Clean up the {len(chosen)} selected item(s){covering}?\n\n" + extra + tail):
            return
        state = self._tag_state = {"text": "Cleaning tags...", "findings": None, "error": None, "result": None,
                                   "chosen": chosen}

        def work():
            try:
                state["result"] = tag_cleaner.apply(chosen, self.TAG_LOG_PATH)
            except Exception as exc:
                state["error"] = str(exc)

        self._tag_thread = threading.Thread(target=work, daemon=True)
        self._tag_thread.start()
        self._poll_tag_apply(set(self._tag_checked))

    @staticmethod
    def _describe_tag_items(chosen):
        """One plain line for each kind of item in the selection, with how many of each."""
        counts = {"title": 0, "note": 0, "artist": 0, "album": 0, "disc": 0, "folder": 0, "files": 0, "tags": 0}
        for item in chosen:
            if item["kind"] == "files":
                counts["files"] += item["count"]
            elif item["kind"] == "folder":
                counts["folder"] += 1
            elif item.get("type") == "disc":
                counts["disc"] += 1
            elif item["kind"] == "title":
                counts["title"] += 1
            elif item["kind"] == "note":
                counts["note"] += 1
            elif item["kind"] == "field":
                counts[item["key"]] += 1
            else:
                counts["tags"] += 1
        lines = {
            "title": "Song titles ({n}): remove the repeated text from the end of the title.",
            "note": "Take notes ({n}): remove the note in brackets, like (RS1 take 3), from the end of the song "
                    "file names and titles. The music itself is not touched.",
            "artist": "Artist names ({n}): remove the extra text from the end of the name.",
            "album": "Album names ({n}): remove the extra text from the end of the name, "
                     "like a catalog number or a note in brackets.",
            "disc": "Disc numbering ({n}): take the catalog number off the album name and add "
                    "Disc 1, Disc 2... so the discs stay separate.",
            "folder": "Folder names ({n}): rename the folder to the plain name, without the year, "
                      "brackets or tags. Nothing inside the folder changes.",
            "files": "Extra files ({n}): leftovers that come with downloads, like rip logs, quality reports and "
                     "playlists. Most people never open them and they just clutter the folder. They're moved "
                     "out to a holding folder, not deleted, so Undo can bring them back. Your .cue sheets are "
                     "never touched.",
            "tags": "Other tags ({n}): remove the repeated value.",
        }
        return "\n".join("\u2022 " + lines[k].format(n=counts[k]) for k in lines if counts[k])

    def _poll_tag_apply(self, done_rows):
        state = self._tag_state
        if self._tag_busy():
            self.tag_status.config(text=state["text"], foreground=self.BUSY_COLOR, font=self.BIG_STATUS)
            self.after(200, lambda: self._poll_tag_apply(done_rows))
            return
        if state["error"]:
            self.tag_status.config(text="Something went wrong: " + state["error"], foreground="#ff8a8a")
            self.tag_log.add_line("Something went wrong: " + state["error"])
            return
        changed, failed, folders, extras = state["result"]
        for item in state.get("chosen", []):
            if item["kind"] == "files":
                self.tag_log.add_line(f'  Moved aside from {item["album"]}: {item["before"]}')
            elif item["kind"] == "folder":
                if item.get("clash"):
                    self.tag_log.add_line(f'  Skipped folder "{item["before"]}": the name "{item["after"]}" is taken')
                else:
                    self.tag_log.add_line(f'  Renamed folder "{item["before"]}" to "{item["after"]}"  |  '
                                          f'{item["album"]}')
            elif item.get("type") == "disc":
                self.tag_log.add_line(f'  Changed album "{item["before"]}" to "{item["after"]}"  |  '
                                      f'{item["album"]}  ({item["count"]} songs)')
            else:
                self.tag_log.add_line(f'  Removed "{item["ending"]}" from {item["album"]}  |  {item["where"]}  '
                                      f'({item["count"]} songs)')
        self.tag_log.add_line(f"Cleanup finished: {changed} songs changed"
                              + (f", {folders} folder(s) renamed" if folders else "")
                              + (f", {extras} extra file(s) moved aside" if extras else "")
                              + (f", {failed} couldn't be changed" if failed else "")
                              + ". Old values saved so Undo can put them back. Now run the FLAC Verifier again to confirm.")
        for row in done_rows:
            if self.tag_tree.exists(row):
                self.tag_tree.delete(row)
        self._tag_checked -= done_rows
        note = f"Done. {changed} songs cleaned."
        if extras:
            note += f" {extras} extra file(s) moved aside."
        if folders:
            note = f"Done. {changed} songs cleaned, {folders} folder(s) renamed" + (
                f", {extras} extra file(s) moved aside." if extras else ".")
            # The rows that are left still point at the old folder names, so start fresh.
            self.tag_tree.delete(*self.tag_tree.get_children())
            self._tag_findings, self._tag_checked = [], set()
            note += " Scan again to see what's left."
        if failed:
            note += f" {failed} couldn't be changed."
        self.tag_status.config(text=note + " Rescan Library in Elisa to see it, and run the FLAC Verifier again.",
                               foreground=DONE_GREEN,
                               font=self.BIG_STATUS)

    def _open_tag_editor(self):
        if self._tag_busy():
            return
        if getattr(self, "_tag_editor_win", None) is not None and self._tag_editor_win.winfo_exists():
            self._tag_editor_win.lift()
            return
        if not tag_cleaner.mutagen_ready():
            messagebox.showinfo("Edit Tags by Hand", "This needs the mutagen add-on to read and write tags.")
            return
        folder = self._folder_from_entry(self.tag_folder_entry)
        if not folder or not os.path.isdir(folder):
            messagebox.showinfo("Edit Tags by Hand", "Pick the album's folder in the \"Folder to check\" box first "
                                "(the Browse button helps). This works on one album at a time.")
            return
        songs = tag_cleaner.read_album_tags(folder)
        if not songs:
            messagebox.showinfo(
                "Edit Tags by Hand",
                "There are no FLAC or MP3 songs directly inside that folder.\n\n"
                "Pick the album's own folder (for a multi-disc album, one disc folder at a time).")
            return
        self._tag_editor_win = TagEditorWindow(self, folder, songs, self.TAG_LOG_PATH, self._tag_edit_saved)

    def _tag_edit_saved(self, folder, changed, failed, renamed=0, rfailed=0):
        name = os.path.basename(folder.rstrip(os.sep)) or folder
        note = f"Tags edited by hand in {name}: {changed} song(s) changed" + (f", {failed} failed." if failed else ".")
        if renamed or rfailed:
            note += f" File names changed: {renamed}" + (f" ({rfailed} couldn't be changed)." if rfailed else ".")
        self.tag_log.add_line(note)
        self.tag_status.config(text=note + " Rescan Library in Elisa to see it. Scan again to refresh the list, "
                                           "and run the FLAC Verifier again.",
                               foreground=DONE_GREEN, font=self.BIG_STATUS)

    def _undo_tag_cleaning(self):
        if self._tag_busy():
            return
        if not os.path.isfile(self.TAG_LOG_PATH) or not os.path.getsize(self.TAG_LOG_PATH):
            messagebox.showinfo("Tag Cleaner", "There's no cleanup to undo yet.")
            return
        if not messagebox.askyesno("Undo last cleanup",
                                   "Put back the tags, folder names and extra files from the most recent "
                                   "cleanup or by-hand tag edit?"):
            return
        restored, failed, folders, extras = tag_cleaner.undo_last(self.TAG_LOG_PATH)
        note = f"Undone. {restored} songs put back."
        if extras:
            note += f" {extras} extra file(s) brought back."
        if folders:
            note = f"Undone. {restored} songs and {folders} folder name(s) put back" + (
                f", {extras} extra file(s) brought back." if extras else ".")
            self.tag_tree.delete(*self.tag_tree.get_children())
            self._tag_findings, self._tag_checked = [], set()
        if failed:
            note += f" {failed} couldn't be restored."
        note += " Run the FLAC Verifier again to confirm."
        self.tag_status.config(text=note, foreground=DONE_GREEN, font=self.BIG_STATUS)
        self.tag_log.add_line(note)

    # ------------------------------------------------------------------ Cover Art Embedder tab
    ART_LOG_PATH = os.path.join(DATA_DIR, "art_embedder_log.jsonl")
    ART_FINDER_LOG_PATH = os.path.join(DATA_DIR, "art_finder_log.jsonl")

    def _build_art_tab(self):
        f = self.art_tab
        self._art_items = []
        self._art_checked = set()
        self._art_replace_mode = False
        self._art_thread = None
        self._art_cancel = False
        self._art_state = {"text": "", "items": None, "error": None, "result": None}

        header = ttk.Frame(f, style="Panel.TFrame")
        ttk.Label(header, text="Put cover pictures inside the songs",
                  style="Panel.TLabel", font=("", 11, "bold")).pack(side="left")
        info = ttk.Label(header, text="\u24d8", style="Panel.TLabel", foreground=INFO_ICON,
                         cursor="question_arrow", font=("", 11, "bold"))
        info.pack(side="left", padx=(6, 0))
        Tooltip(info, "Players like Elisa show the picture that is stored inside each FLAC or MP3 file. A cover.jpg "
                      "sitting in the folder isn't always enough, and the FLAC Verifier counts it as missing "
                      "art.\n\n"
                      "This tab scans a folder and, for every album whose songs have no embedded picture, finds "
                      "the cover that goes with it: first a picture in the album folder (cover, folder, front), "
                      "then an Artwork or Covers folder inside it, then the folder above when the album is one "
                      "disc of a set, and last the only picture in the folder. You see which picture it chose "
                      "for each album before anything is written.\n\n"
                      "Only songs with no picture get one. Songs that already have art are never touched, "
                      "pictures bigger than 1500 pixels are shrunk before they're embedded, and the original "
                      "picture file is never changed. Every change is saved so you can undo it.\n\n"
                      "Don't like a cover that's already inside the songs? Point the folder at ONE album (or two) "
                      "and press Replace Cover Art, then double-click the album to pick a different picture. "
                      "The old pictures are saved first, and Undo puts them back.")
        box = ttk.LabelFrame(f, labelwidget=header)
        box.pack(fill="x", padx=10, pady=(10, 6))
        row = ttk.Frame(box, style="Panel.TFrame")
        row.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Label(row, text="Folder to check:", style="Panel.TLabel", width=18).pack(side="left")
        self.art_folder_entry = ttk.Entry(row)
        self.art_folder_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        browse = RoundedButton(row, text="Browse...", command=self._browse_art_folder)
        browse.pack(side="left")
        Tooltip(browse, "Pick a folder of FLAC or MP3 files. Everything inside it, in any number of sub-folders, "
                        "is checked. Works on one album or your whole collection.")
        note = ttk.Label(box, style="Panel.TLabel", foreground=FG_MUTED, wraplength=820, justify="left",
                  text="Works with both FLAC and MP3 files. Nothing changes until you tick rows and press Embed Cover Art. "
                       "TIP: double-click any row in the list to see the picture it chose for that album. "
                       "After you make changes, go back to the FLAC Verifier and run it again to confirm."
                   )
        note.pack(fill="x", padx=8, pady=(6, 8))
        box.bind("<Configure>", lambda e, lbl=note: lbl.config(wraplength=max(200, e.width - 30)))
        btns = ttk.Frame(f)
        btns.pack(fill="x", padx=10, pady=(0, 6))
        self.art_back_btn = RoundedButton(btns, text="\u2190 Back to FLAC Verifier",
                                          command=lambda: self._show_verify_view("verify"))
        self.art_back_btn.pack(side="left", padx=(0, 8))
        Tooltip(self.art_back_btn, "Returns to the FLAC Verifier results without running anything.")
        self.art_scan_btn = RoundedButton(btns, text="Scan", command=lambda: self._start_art_scan(False))
        self.art_scan_btn.pack(side="left")
        Tooltip(self.art_scan_btn, "Finds the albums whose songs have no embedded picture and the picture that "
                                   "goes with each. Changes nothing.")
        self.art_replace_btn = RoundedButton(btns, text="Replace Cover Art", command=lambda: self._start_art_scan(True))
        self.art_replace_btn.pack(side="left", padx=(8, 0))
        Tooltip(self.art_replace_btn, "For when a cover that's already inside the songs is the wrong one (or has "
                                      "something odd on it). Point the folder at ONE album, press this, then double-click "
                                      "the album to look for a different cover online. The pictures now in the songs are "
                                      "saved first, and Undo puts them back. Meant for a couple of albums, not a whole library.")
        self.art_find_btn = RoundedButton(btns, text="Find Cover Art Online...", command=self._open_cover_finder)
        self.art_find_btn.pack(side="left", padx=8)
        Tooltip(self.art_find_btn, "For the albums marked \"No picture found\": searches the internet (iTunes, Deezer "
                                   "and MusicBrainz), shows you the covers it finds and saves the one you pick as "
                                   "cover.jpg in the album's folder. Then it's ready to tick and embed like any other. "
                                   "Run Scan first. You can also double-click a single orange row.")
        self.art_apply_btn = RoundedButton(btns, text="Embed Cover Art", command=self._apply_art)
        self.art_apply_btn.pack(side="left", padx=(0, 8))
        Tooltip(self.art_apply_btn, "Puts the chosen picture inside every song of the ticked albums that has "
                                    "none (or, in replace mode, swaps it for the pictures already there). Each song "
                                    "is rewritten through a temporary copy, so stopping partway can't damage a file.")
        self.art_cancel_btn = RoundedButton(btns, text="Cancel", command=self._cancel_art)
        self.art_cancel_btn.pack(side="left")
        Tooltip(self.art_cancel_btn, "Stops after the song it's working on. What was already done stays done, "
                                     "and Undo can still take it back.")
        self.art_undo_btn = RoundedButton(btns, text="Undo Last Embed", command=self._undo_art)
        self.art_undo_btn.pack(side="left", padx=8)
        Tooltip(self.art_undo_btn, "Reverses the most recent embed: takes out the pictures it added, or puts back the "
                                   "pictures it replaced. Nothing else changes.")
        status_row = ttk.Frame(f)
        status_row.pack(fill="x", padx=10, pady=(0, 4))
        self.art_status = ttk.Label(status_row, text="Ready", foreground=FG_MUTED, font=self.BIG_STATUS, justify="left")
        self.art_status.pack(side="left", padx=2)
        status_row.bind("<Configure>", lambda e: self.art_status.config(wraplength=max(300, e.width - 20)))

        legend = ttk.Frame(f)
        legend.pack(fill="x", padx=10, pady=(0, 4))
        ttk.Label(legend, text="What the colors mean:", foreground=FG_MUTED).pack(side="left")
        for text, color, tip in (
                ("\u25cf Ready to embed", DONE_GREEN, "Green: a picture was found. Tick the row to embed it."),
                ("\u25cf No picture found", "#ffb870",
                 "Orange: no picture was found for this album, so there's nothing to embed. Press Find Cover "
                 "Art Online (or double-click the row) to look for one on the internet, or put a cover.jpg in "
                 "its folder and scan again.")):
            label = ttk.Label(legend, text=text, foreground=color)
            label.pack(side="left", padx=(14, 0))
            Tooltip(label, tip)

        self.art_progress = ttk.Progressbar(f, mode="determinate")
        self.art_progress.pack(fill="x", padx=10, pady=(0, 6))

        log_box = ttk.Frame(f)
        log_box.pack(side="bottom", fill="x", padx=10, pady=(0, 10))
        ttk.Label(log_box, text="Log:", font=("", 11, "bold")).pack(anchor="w")
        self.art_log = WatermarkLog(log_box, background=BG_INPUT, foreground=FG_TEXT, border=BORDER,
                                    font=tkfont.Font(family="Monospace", size=12),
                                    logo_path=os.path.join(SCRIPT_DIR, "app_icon.png"))
        self.art_log.canvas.configure(height=200)
        self.art_log.pack(fill="x")
        Tooltip(self.art_log.canvas, "What the scan found and what each embed changed. Right-click to copy the "
                                     "whole log or clear it.")

        self.ART_TIP_NORMAL = ("Double-click a green row to see the picture it will embed, or an orange row to look for its "
                               "cover on the internet. To swap a cover that's already inside the songs, press Replace Cover Art.")
        self.ART_TIP_REPLACE = ("Replace mode: double-click an album to look for a different cover on the internet. The pictures "
                                "now in its songs are saved first, and Undo Last Embed puts them back.")
        art_tip_row = add_tip_row(f, self.ART_TIP_NORMAL)
        art_tip_row.pack_configure(side="bottom")  # just above the log, under the table
        self.art_tip_label = art_tip_row.winfo_children()[-1]

        columns = ("fix", "artist", "album", "picture", "where", "songs", "size")
        frame = ttk.Frame(f)
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self.art_tree = ttk.Treeview(frame, columns=columns, show="headings", height=6)
        heads = {"fix": "\u2610", "artist": "Artist", "album": "Album folder", "picture": "Picture", "where": "Found",
                 "songs": "Songs without art", "size": "Picture size"}
        widths = {"fix": 34, "artist": 170, "album": 300, "picture": 150, "where": 295, "songs": 130, "size": 140}
        for col in columns:
            self.art_tree.heading(col, text=heads[col], command=self._art_check_all if col == "fix" else "")
            if col == "fix":
                self.art_tree.column(col, width=widths[col], minwidth=widths[col], stretch=False, anchor="center")
            else:
                self.art_tree.column(col, width=widths[col], minwidth=60, stretch=(col == "size"),
                                     anchor="center")
        scroll = tk.Scrollbar(frame, command=self.art_tree.yview)
        hscroll = tk.Scrollbar(frame, orient="horizontal", command=self.art_tree.xview)
        self.art_tree.configure(yscrollcommand=scroll.set, xscrollcommand=hscroll.set)
        hscroll.pack(side="bottom", fill="x")
        scroll.pack(side="right", fill="y")
        self.art_tree.pack(side="left", fill="both", expand=True)
        self.art_tree.tag_configure("ready", foreground=DONE_GREEN)
        self.art_tree.tag_configure("nopic", foreground="#ffb870")
        self.art_tree.bind("<Button-1>", self._art_row_clicked)
        enable_column_grab(self.art_tree, fixed=("fix",))
        self.art_tree.bind("<Double-1>", self._art_row_preview)
        Tooltip(self.art_tree, "Click a row to tick it. The box in the first column heading ticks every row "
                               "that has a picture. Double-click any row to see the picture it chose.")
        CellTooltip(self.art_tree, "picture", lambda value: ("Double-click the row to see this picture. "
                                                              + value) if value else None)
        art_album_tip = CellTooltip(self.art_tree, "album", lambda value: (
            self._art_items[int(art_album_tip._cell[0])]["album"]
            if art_album_tip._cell and art_album_tip._cell[0].isdigit()
            and int(art_album_tip._cell[0]) < len(self._art_items) else None))
        CellTooltip(self.art_tree, "where", lambda value: value or None)

    @staticmethod
    def _art_artist(folder):
        """The artist shown in the cover art table: the album artist tag, else the artist tag, from the first
        songs of the album. If no song has either, the name of the folder above the album folder (above a
        CD 1 / Disc 2 folder, the folder above the album)."""
        names, where = [], folder
        try:
            for where, _dirs, files in os.walk(folder):
                names = sorted(n for n in files if n.lower().endswith((".flac", ".mp3")))
                if names:
                    break
        except OSError:
            pass
        for name in names[:5]:
            try:
                audio = tag_cleaner._open_audio(os.path.join(where, name))
            except Exception:
                continue
            for key in ("albumartist", "artist"):
                values = audio.get(key) or []
                if values and str(values[0]).strip():
                    return str(values[0]).strip()
        parent = os.path.dirname(folder.rstrip("/\\"))
        if tag_cleaner._DISC_FOLDER.match(os.path.basename(folder.rstrip("/\\"))):
            parent = os.path.dirname(parent)
        return os.path.basename(parent)

    def _browse_art_folder(self):
        path = filedialog.askdirectory(title="Choose a folder of FLAC or MP3 files to check",
                                       initialdir=self._nearest_existing_folder(self.art_folder_entry.get()))
        if path:
            self.art_folder_entry.delete(0, "end")
            self.art_folder_entry.insert(0, core.resolve_folder(path))

    def _art_busy(self):
        return self._art_thread is not None and self._art_thread.is_alive()

    def _cancel_art(self):
        if self._art_busy():
            self._art_cancel = True
            self.art_status.config(text="Stopping after this song...", foreground="#ffd54a")

    def _art_row_clicked(self, event):
        row = self.art_tree.identify_row(event.y)
        if not row or self._art_busy():
            return
        if not self._art_items[int(row)]["cover"]:
            return "break"
        if row in self._art_checked:
            self._art_checked.discard(row)
        else:
            self._art_checked.add(row)
        self._paint_art_row(row)
        return "break"

    def _paint_art_row(self, row):
        self.art_tree.set(row, "fix", "\u2611" if row in self._art_checked else "\u2610")
        usable = {r for r in self.art_tree.get_children() if self._art_items[int(r)]["cover"]}
        self.art_tree.heading("fix", text="\u2611" if usable and self._art_checked >= usable else "\u2610")

    def _art_check_all(self):
        rows = self.art_tree.get_children()
        if not rows or self._art_busy():
            return
        usable = {r for r in rows if self._art_items[int(r)]["cover"]}
        all_on = bool(usable) and self._art_checked >= usable
        self._art_checked = set() if all_on else set(usable)
        for r in rows:
            self._paint_art_row(r)

    def _art_row_preview(self, event):
        row = self.art_tree.identify_row(event.y)
        if not row or self.art_tree.identify_column(event.x) == "#1":
            return
        item = self._art_items[int(row)]
        if not item["cover"]:
            self._open_cover_finder([item])
            return
        if not HAVE_PIL:
            messagebox.showinfo("Cover Art Embedder", "Seeing the picture needs the Pillow add-on.\n\n"
                                "Install it with:\n  pip install pillow --break-system-packages\n\n"
                                "The picture file is:\n" + item["cover"])
            return
        try:
            img = Image.open(item["cover"])
            img.load()
            shown = img.convert("RGB")
            shown.thumbnail((520, 520))
            win = tk.Toplevel(self)
            win.title(os.path.basename(item["cover"]))
            win.configure(bg=BG_MAIN)
            photo = ImageTk.PhotoImage(shown)
            label = tk.Label(win, image=photo, bg=BG_MAIN)
            label.image = photo
            label.pack(padx=12, pady=(12, 6))
            note = f'{img.size[0]} x {img.size[1]}   |   {item["album"]}'
            tk.Label(win, text=note, bg=BG_MAIN, fg=FG_MUTED, wraplength=520).pack(padx=12, pady=(0, 12))
            win.bind("<Escape>", lambda e: win.destroy())
        except Exception as exc:
            messagebox.showinfo("Cover Art Embedder", f"Couldn't open that picture: {exc}")

    def _open_cover_finder(self, only=None):
        """Opens the internet cover search for the orange rows (or just the one double-clicked)."""
        if self._art_busy():
            return
        existing = getattr(self, "_cover_finder", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return
        if not HAVE_PIL:
            messagebox.showinfo("Cover Art Embedder", "Finding cover art needs the Pillow add-on.\n\n"
                                "Install it with:\n  pip install pillow --break-system-packages")
            return
        pool = only if only else [item for item in self._art_items if not item["cover"]]
        pool = [item for item in pool if not item["cover"]]
        if not pool:
            messagebox.showinfo("Cover Art Embedder", "There are no albums waiting for a picture.\n\n"
                                "Press Scan first. Every album marked \"No picture found\" in orange can be "
                                "searched for on the internet.")
            return
        self.art_log.add_line(f"Looking for cover art on the internet for {len(pool)} album(s)...")
        self._cover_found_count = 0
        self._cover_finder = CoverFinderWindow(self, pool, self.ART_FINDER_LOG_PATH,
                                               on_saved=self._cover_downloaded, on_done=self._cover_finder_done)

    def _show_found_cover(self, item, cover, where):
        """Turns an orange row green now that it has a picture to use, ticks it and moves it to the top."""
        item["cover"], item["where"] = cover, where
        try:
            prepared = art_embedder.prepare_picture(cover)
            item["size"] = (prepared["width"], prepared["height"])
            item["note"] = "will be shrunk" if prepared["resized"] else ""
        except Exception as exc:
            item["cover"], item["where"] = None, f"Can't read the picture ({exc})"
            return
        iid = str(next(i for i, other in enumerate(self._art_items) if other is item))
        if not self.art_tree.exists(iid):
            return
        size = f'{item["size"][0]} x {item["size"][1]}' + (" (shrunk)" if item["note"] else "")
        self.art_tree.item(iid, values=("\u2610", item.get("artist", ""), self._short_album(item["album"]),
                                        os.path.basename(cover), where,
                                        f'{len(item["missing"])} of {item["total"]}', size), tags=("ready",))
        self._art_checked.add(iid)
        self._paint_art_row(iid)
        self.art_tree.move(iid, "", 0)

    def _cover_downloaded(self, item, info, cand):
        name = os.path.basename(info["path"])
        self.art_log.add_line(f'  Saved {name} from {cand["source"]} ({info["width"]} x {info["height"]}) '
                              f'for {item["album"]}')
        if info["small"]:
            self.art_log.add_line(f'    Note: that picture is fairly small ({info["width"]} x {info["height"]}). '
                                  "It works, but a bigger one would look sharper.")
        self._cover_found_count += 1
        self._show_found_cover(item, info["path"], f'Downloaded from {cand["source"]}')
        # other discs of the same set now find that same picture in the folder above
        for other in ([] if self._art_replace_mode else self._art_items):
            if other is not item and not other["cover"]:
                cover, where = art_embedder.find_cover(other["folder"])
                if cover:
                    self._show_found_cover(other, cover, where)

    def _cover_finder_done(self, saved):
        remaining = sum(1 for item in self._art_items if not item["cover"])
        if saved:
            self.art_log.add_line(f"Cover search finished: {saved} picture(s) saved. They're ticked, "
                                  "so press Embed Cover Art to put them inside the songs.")
            self.art_status.config(
                text=f"Found art for {saved} album(s) and ticked them. Press Embed Cover Art to put it in the songs."
                     + (f" {remaining} still have no picture." if remaining else ""),
                foreground="#ffd54a", font=self.BIG_STATUS)
        else:
            self.art_log.add_line("Cover search finished: no pictures were saved.")

    def _start_art_scan(self, replace=False):
        if self._art_busy():
            return
        if not art_embedder.ready():
            messagebox.showinfo("Cover Art Embedder", "The Cover Art Embedder needs the \"mutagen\" add-on.\n\n"
                                "Install it with:\n  pip install mutagen --break-system-packages")
            return
        folder = self._folder_from_entry(self.art_folder_entry)
        if not folder or not os.path.isdir(folder):
            messagebox.showinfo("Cover Art Embedder", "Pick a folder to check first.")
            return
        self.art_tree.delete(*self.art_tree.get_children())
        self._art_items, self._art_checked = [], set()
        self._art_cancel = False
        self._art_replace_mode = replace
        self.art_tip_label.config(text=self.ART_TIP_REPLACE if replace else self.ART_TIP_NORMAL)
        self.art_tree.heading("songs", text="Songs to change" if replace else "Songs without art")
        self.art_log.add_line(f"Scanning{' (replace mode)' if replace else ''}: {folder}")
        self.art_status.config(font=self.BIG_STATUS)
        state = self._art_state = {"text": "Starting...", "items": None, "error": None, "done": 0, "total": 0}

        def work():
            try:
                def progress(i, total, _folder):
                    state["text"] = f"Checking album {i} of {total}..."
                    state["done"], state["total"] = i, total
                items = art_embedder.scan(folder, progress=progress, cancelled=lambda: self._art_cancel,
                                          replace=replace)
                for entry in items or []:
                    entry["artist"] = self._art_artist(entry.get("folder", ""))
                state["items"] = items
            except Exception as exc:  # shown in the status, never a crash
                state["error"] = str(exc)

        self._art_thread = threading.Thread(target=work, daemon=True)
        self._art_thread.start()
        self._poll_art_scan()

    def _poll_art_scan(self):
        state = self._art_state
        if self._art_busy():
            self.art_status.config(text=state["text"], foreground=self.BUSY_COLOR, font=self.BIG_STATUS)
            if state.get("total"):
                self.art_progress.configure(maximum=state["total"], value=state["done"])
            self.after(200, self._poll_art_scan)
            return
        self.art_progress.configure(value=0)
        if state["error"]:
            self.art_status.config(text="Something went wrong: " + state["error"], foreground="#ff8a8a")
            self.art_log.add_line("Something went wrong: " + state["error"])
            return
        # the albums that have a picture first, the ones with nothing to embed at the bottom
        self._art_items = sorted(state["items"] or [], key=lambda item: not item["cover"])
        for i, item in enumerate(self._art_items):
            have = item["cover"]
            size = (f'{item["size"][0]} x {item["size"][1]}' + (" (shrunk)" if item["note"] else "")) if item["size"] else ""
            if item.get("replace") and not have and item.get("current"):
                size = f'now {item["current"][0]} x {item["current"][1]}'
            self.art_tree.insert("", "end", iid=str(i), values=(
                "\u2610" if have else "", item.get("artist", ""), self._short_album(item["album"]),
                os.path.basename(have) if have else "", item["where"],
                f'{len(item["missing"])} of {item["total"]}', size), tags=("ready",) if have else ("nopic",))
            if have:
                self.art_log.add_line(f'  Found: {item["album"]}  |  {os.path.basename(have)}  |  {item["where"]}  '
                                      f'({len(item["missing"])} of {item["total"]} songs)')
            else:
                self.art_log.add_line(f'  No picture: {item["album"]}  |  {item["where"]}')
        if self._art_replace_mode:
            n = len(self._art_items)
            self.art_log.add_line(f"Scan finished: {n} album(s) listed. Double-click an album to look for a "
                                  "replacement cover; the pictures now in its songs are saved before anything changes.")
            if n > 20:
                self.art_log.add_line("Note: that's a lot of albums. Replace mode is meant for one or two at a time.")
            self.art_status.config(
                text=(f"{n} album(s) listed. " if n <= 20 else f"{n} albums listed, which is a lot for this mode. ")
                     + "Double-click an album to find a replacement cover." if n else "No songs were found in that folder.",
                foreground="#ffd54a", font=self.BIG_STATUS)
            return
        ready = sum(1 for item in self._art_items if item["cover"])
        nopic = len(self._art_items) - ready
        self.art_log.add_line(f"Scan finished: {len(self._art_items)} album(s) have songs without cover art; "
                              f"{ready} have a picture to embed, {nopic} have none.")
        if ready:
            self.art_log.add_line("Tip: double-click any row to see the picture it will embed.")
            self.art_status.config(text=f"{ready} album(s) ready to embed" +
                                   (f", {nopic} with no picture." if nopic else ".") +
                                   "  Double-click a row to preview its picture.", foreground="#ffd54a",
                                   font=self.BIG_STATUS)
        elif nopic:
            self.art_status.config(text=f"{nopic} album(s) need art but have no picture to use.",
                                   foreground="#ffd54a", font=self.BIG_STATUS)
        else:
            self.art_status.config(text="Every song already has cover art.", foreground=DONE_GREEN,
                                   font=self.BIG_STATUS)

    def _apply_art(self):
        if self._art_busy():
            return
        chosen = [self._art_items[int(r)] for r in sorted(self._art_checked, key=int)]
        if not chosen:
            messagebox.showinfo("Cover Art Embedder", "Tick at least one album in the list first.")
            return
        songs = sum(len(item["missing"]) for item in chosen)
        replace = self._art_replace_mode
        if replace:
            if not messagebox.askyesno(
                    "Replace the cover art",
                    f"Replace the cover pictures in up to {songs} songs in {len(chosen)} album(s)?\n\n"
                    "Every picture now inside these songs (front and back covers included) is swapped for the "
                    "new cover you picked. The old pictures are saved first, so Undo Last Embed puts them "
                    "back exactly as they were.\n\n"
                    "Your music itself doesn't change, and your picture files stay right where they are."):
                return
        elif not messagebox.askyesno(
                "Add cover art to your songs",
                f"Add cover pictures to {songs} songs in {len(chosen)} album(s)?\n\n"
                "Right now these songs have no picture stored inside them, so music players like Elisa "
                "show a blank or generic cover. This puts each album's cover picture inside its song "
                "files, so the cover shows up wherever the songs go.\n\n"
                "Your music itself doesn't change, and your picture files stay right where they are. "
                "Songs that already have a picture are skipped. A big batch can take a few minutes.\n\n"
                "Changed your mind afterward? Press Undo Last Embed and the pictures come back out."):
            return
        self._art_cancel = False
        state = self._art_state = {"text": "Embedding...", "items": None, "error": None, "result": None,
                                   "done": 0, "total": len(chosen), "chosen": chosen}

        def work():
            try:
                def progress(i, total, folder):
                    state["text"] = f"Embedding album {i} of {total}..."
                    state["done"] = i
                state["result"] = art_embedder.apply(chosen, self.ART_LOG_PATH, progress=progress,
                                                     cancelled=lambda: self._art_cancel, replace=replace)
            except Exception as exc:
                state["error"] = str(exc)

        self._art_thread = threading.Thread(target=work, daemon=True)
        self._art_thread.start()
        self._poll_art_apply(set(self._art_checked))

    def _poll_art_apply(self, done_rows):
        state = self._art_state
        if self._art_busy():
            self.art_status.config(text=state["text"], foreground=self.BUSY_COLOR, font=self.BIG_STATUS)
            self.art_progress.configure(maximum=max(1, state["total"]), value=state["done"])
            self.after(200, lambda: self._poll_art_apply(done_rows))
            return
        self.art_progress.configure(value=0)
        if state["error"]:
            self.art_status.config(text="Something went wrong: " + state["error"], foreground="#ff8a8a")
            self.art_log.add_line("Something went wrong: " + state["error"])
            return
        changed, failed, albums = state["result"]
        for item in state.get("chosen", []):
            self.art_log.add_line(f'  {"Replaced the picture with" if self._art_replace_mode else "Embedded"} '
                                  f'{os.path.basename(item["cover"])} in {item["album"]}')
        stopped = " (stopped early)" if self._art_cancel else ""
        self.art_log.add_line(f"{'Replace' if self._art_replace_mode else 'Embed'} finished{stopped}: {changed} songs in "
                              f"{albums} album(s) {'have a new cover' if self._art_replace_mode else 'now have cover art'}"
                              + (f", {failed} couldn't be changed" if failed else "")
                              + ". Saved so Undo can take them back out.")
        for row in done_rows:
            if self.art_tree.exists(row):
                self.art_tree.delete(row)
        self._art_checked -= done_rows
        note = f"Done{stopped}. {changed} songs " + ("have a new cover picture." if self._art_replace_mode
                                                     else "now have cover art.")
        if failed:
            note += f" {failed} couldn't be changed."
        self.art_status.config(text=note + " Rescan Library in Elisa, and run the FLAC Verifier again.",
                               foreground=DONE_GREEN if not failed else "#ffd54a", font=self.BIG_STATUS)

    def _undo_art(self):
        if self._art_busy():
            return
        if not os.path.isfile(self.ART_LOG_PATH) or not os.path.getsize(self.ART_LOG_PATH):
            messagebox.showinfo("Cover Art Embedder", "There's no embed to undo yet.")
            return
        if not messagebox.askyesno("Undo last embed",
                                   "Undo the most recent cover art change?\n\n"
                                   "Pictures it added are taken back out, and pictures it replaced are put back "
                                   "as they were. Pictures that were already there, and the picture files in your "
                                   "folders, are left alone."):
            return
        restored, failed = art_embedder.undo_last(self.ART_LOG_PATH)
        note = f"Undone. {restored} songs went back to how they were before that change."
        if failed:
            note += f" {failed} couldn't be restored."
        self.art_status.config(text=note, foreground=DONE_GREEN, font=self.BIG_STATUS)
        self.art_log.add_line(note)

    def _build_search_tab(self):
        f = self.search_tab
        self.library_folders = []  # list of {"path": str, "mode": str, "artist": str}

        lib_row = ttk.Frame(f)
        lib_row.pack(fill="x", padx=10, pady=(10, 6))
        lib_row.columnconfigure(0, weight=3, uniform="logo_split")
        lib_row.columnconfigure(1, weight=1, uniform="logo_split")
        lib_header = ttk.Frame(lib_row, style="Panel.TFrame")
        ttk.Label(lib_header, text="Search your collection by artist, album, or song!",
                  style="Panel.TLabel", font=("", 11, "bold")).pack(side="left")
        lib_info = ttk.Label(lib_header, text="\u24d8", style="Panel.TLabel", foreground=INFO_ICON,
                             cursor="question_arrow", font=("", 11, "bold"))
        lib_info.pack(side="left", padx=(6, 0))
        Tooltip(lib_info, "This is a standalone feature that you can use on your music collection "
                           "as it is. It doesn't depend on packing lists or burning any discs.")
        lib_frame = ttk.LabelFrame(lib_row, labelwidget=lib_header)
        lib_frame.grid(row=0, column=0, sticky="ew")      # centered, so the bigger logo stays centered beside it
        # Twice the size of the packing tab's logo (the largest the logo is ever drawn): room for the
        # picture plus the little margin and upward nudge the placement code leaves around it.
        lib_icon_holder = ttk.Frame(lib_row, height=self.SIDEBAR_ICON_MAX + 4 + 2 * self.SIDEBAR_ICON_NUDGE_UP)
        lib_icon_holder.grid(row=0, column=1, sticky="ew", padx=(10, 0))
        self._place_sidebar_icon(lib_icon_holder)
        lib_intro = ttk.Label(lib_frame, text="Add the folders where the music you listen to every day lives, then "
                                              "hit Scan Library and you can search all of it below.",
                              style="Panel.TLabel", font=("", 11, "bold"), wraplength=640, justify="left")
        lib_intro.pack(anchor="w", padx=8, pady=(6, 0))
        # Wrap to the panel's real width (only when the window is too narrow), not a fixed number.
        lib_frame.bind("<Configure>", lambda e: lib_intro.config(wraplength=max(200, e.width - 30)))

        list_row = ttk.Frame(lib_frame, style="Panel.TFrame")
        list_row.pack(fill="x", padx=8, pady=(8, 4))
        self.library_folder_list = tk.Listbox(list_row, height=4, relief="flat", borderwidth=1,
                                               background=BG_INPUT, foreground=FG_TEXT,
                                               highlightthickness=1, highlightbackground=BORDER,
                                               selectbackground=ACCENT, selectforeground="#ffffff",
                                               activestyle="none")
        self.library_folder_list.pack(side="left", fill="x", expand=True)
        lib_scroll = tk.Scrollbar(list_row, command=self.library_folder_list.yview)  # matches the log box's scrollbar
        lib_scroll.pack(side="right", fill="y")
        self.library_folder_list.config(yscrollcommand=lib_scroll.set)

        lib_btn_row = ttk.Frame(lib_frame, style="Panel.TFrame")
        lib_btn_row.pack(fill="x", padx=8, pady=(0, 8))
        add_lib_btn = RoundedButton(lib_btn_row, text="Add Folder...", command=self._add_library_folder)
        add_lib_btn.pack(side="left")
        Tooltip(add_lib_btn, "Add a folder of music you listen to -- your whole collection, or just one "
                              "folder, whatever you'd like to be able to search.")
        lib_settings_btn = RoundedButton(lib_btn_row, text="Folder Layout...", command=self._edit_library_folder)
        lib_settings_btn.pack(side="left", padx=6)
        Tooltip(lib_settings_btn, "Most folders need nothing here. Select a folder above, then click this only if it "
                                   "holds one artist's music (like \"Extra Van Halen\") instead of a folder for each "
                                   "artist, so searches show the right artist name.")
        lib_remove_btn = RoundedButton(lib_btn_row, text="Remove", command=self._remove_library_folder)
        lib_remove_btn.pack(side="left")
        Tooltip(lib_remove_btn, "Remove the selected folder from this list (doesn't touch any files).")

        scan_row = ttk.Frame(lib_frame, style="Panel.TFrame")
        scan_row.pack(fill="x", padx=8, pady=(0, 4))
        catalog_label = ttk.Label(scan_row, text="Music catalog database:", style="Panel.TLabel")
        catalog_label.pack(side="left")
        self.search_db_entry = ttk.Entry(scan_row, width=40)
        self.search_db_entry.pack(side="left", padx=4, fill="x", expand=True)
        catalog_browse_btn = RoundedButton(scan_row, text="Browse...", command=self._browse_search_db)
        catalog_browse_btn.pack(side="left")
        catalog_tip = ("This is where your music library gets turned into something searchable, and "
                       "where that file is stored. Scan Library fills it in for you (in your DiscWise "
                       "folder), so you can usually leave it alone.")
        Tooltip(catalog_label, catalog_tip)
        Tooltip(self.search_db_entry, catalog_tip)
        Tooltip(catalog_browse_btn, "Pick a different catalog file, for example one you scanned earlier.")

        scan_btn_row = ttk.Frame(lib_frame, style="Panel.TFrame")
        scan_btn_row.pack(fill="x", padx=8, pady=(4, 8))
        self.scan_library_btn = RoundedButton(scan_btn_row, text="⟳  Scan Library", command=self._scan_library_clicked)
        self.scan_library_btn.pack(side="left")
        Tooltip(self.scan_library_btn, "Scans the folders above and (re)builds the catalog database "
                                        "from scratch -- a full, independent search index of exactly "
                                        "what's in these folders, whether or not any of it has ever "
                                        "been burned to a disc.")
        self.library_art_var = tk.BooleanVar(value=False)
        library_art_cb = ttk.Checkbutton(
            scan_btn_row, text="Also list folders that only hold pictures", variable=self.library_art_var,
            style="Panel.TCheckbutton", command=self._save_state)
        library_art_cb.pack(side="left", padx=(12, 0))
        Tooltip(library_art_cb, "Leave this unticked and Scan Library skips folders with no music in them, such as a "
                                "concert's Scans or Artwork folder full of JPEGs, so your music search stays clean. "
                                "Tick it if you want those folders in the catalog too. Ticking it only changes the next "
                                "scan.")
        status_row = ttk.Frame(lib_frame, style="Panel.TFrame")
        status_row.pack(fill="x", padx=8, pady=(0, 6))
        self.library_status = ttk.Label(status_row, text="", style="Panel.TLabel", font=("", 16, "bold"),
                                        justify="left")
        self.library_status.pack(side="left")
        # One line whenever it fits; it only wraps if the window is too narrow to hold the whole message.
        status_row.bind("<Configure>", lambda e: self.library_status.config(wraplength=max(200, e.width - 8)))

        self.library_progress = ttk.Progressbar(lib_frame, mode="indeterminate")
        self.library_progress.pack(fill="x", padx=8, pady=(0, 8))

        top = ttk.Frame(f)
        top.pack(fill="x", padx=10, pady=(4, 10))

        ttk.Label(top, text="Search:").grid(row=0, column=0, sticky="e")
        self.search_entry = ttk.Entry(top)
        self.search_entry.grid(row=0, column=1, sticky="we", padx=4)
        self.search_entry.bind("<Return>", lambda e: self._do_search())
        top.columnconfigure(1, weight=1)

        self.search_scope = tk.StringVar(value="All")
        scope_menu = ttk.Combobox(
            top, textvariable=self.search_scope, state="readonly",
            values=["All", "Artist", "Album", "Song", "File Type", "Disc #"], width=10
        )
        scope_menu.grid(row=0, column=2, padx=(4, 0))
        Tooltip(scope_menu, "Limit the search to just this field, or search artist/album/song at once. "
                           "\"File Type\" finds by FLAC, MP3, MP4 and so on. \"All\" also finds a disc when you "
                           "type its number. \"Disc #\" lists only what's on that one disc: type 10 to see disc 10, "
                           "or leave it empty to list every disc.")

        RoundedButton(top, text="Search", command=self._do_search).grid(row=0, column=3, padx=(6, 0))

        self.search_status = ttk.Label(f, text="Scan your folders above, or browse to an existing catalog, then search.")
        self.search_status.pack(anchor="w", padx=10)

        search_tip = add_tip_row(f, "Double-click any row in the results to open that album's folder. "
                                    "Search by artist, album or song, or leave the box empty to list everything.")
        search_tip.pack_configure(side="bottom")     # packed before the results so they can't squeeze it out

        disc_frame = ttk.LabelFrame(f, text="Search the discs you've burned")
        disc_frame.pack(fill="x", side="bottom", padx=10, pady=(0, 6))   # sits just above the tip row
        disc_row = ttk.Frame(disc_frame, style="Panel.TFrame")
        disc_row.pack(fill="x", padx=8, pady=8)
        self.search_discs_btn = RoundedButton(disc_row, text="Search Burned Discs", command=self._open_disc_search)
        self.search_discs_btn.pack(side="left")
        Tooltip(self.search_discs_btn, "Opens a search window on the packing-list database, so you can look up "
                                        "a song, album or artist and see which disc it's on.")
        disc_note = ttk.Label(disc_row, text="Look up a song, album or artist and see which disc it's on, "
                                             "even after the files are gone from your drive.",
                              style="PanelMuted.TLabel", wraplength=700, justify="left")
        disc_note.pack(side="left", padx=12)
        disc_row.bind("<Configure>", lambda e: disc_note.config(
            wraplength=max(200, e.width - self.search_discs_btn.winfo_reqwidth() - 40)))

        columns = ("artist", "album", "disc", "status", "type", "track", "song")
        results_frame = ttk.Frame(f)
        results_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.results = ttk.Treeview(results_frame, columns=columns, show="headings", height=16)
        self._columns_no_disc = ("artist", "album", "track", "song", "type")
        self._columns_with_disc = ("artist", "album", "disc", "status", "track", "song", "type")
        self.results["displaycolumns"] = self._columns_no_disc  # Disc # only appears for packing-list databases
        headings = {"artist": "Artist", "album": "Album", "disc": "Disc #", "status": "Status",
                    "type": "File Type", "track": "Track #", "song": "Song"}
        widths = {"artist": 160, "album": 190, "disc": 60, "status": 120, "type": 70, "track": 60, "song": 190}
        for col in columns:
            self.results.heading(col, text=headings[col])
            self.results.column(col, width=widths[col], anchor="center")
        results_scroll = tk.Scrollbar(results_frame, command=self.results.yview)
        self.results.configure(yscrollcommand=results_scroll.set)
        self.results.pack(side="left", fill="both", expand=True)
        results_scroll.pack(side="right", fill="y")
        self.results.bind("<Double-1>", self._show_folder_for_selection)
        Tooltip(self.results, "Double-click a row to open that album's folder.")

        self._search_row_paths = {}

    def _add_library_folder(self):
        path = filedialog.askdirectory(title="Choose a folder of music you listen to",
                                      initialdir=self._nearest_existing_folder(self.library_folders[-1]["path"] if self.library_folders else ""))
        if path:
            path = core.resolve_folder(path)
            self.library_folders.append({"path": path, "mode": "normal", "artist": ""})
            self.library_folder_list.insert("end", path)
            self._save_state()

    def _edit_library_folder(self):
        sel = self.library_folder_list.curselection()
        if not sel:
            messagebox.showinfo("Select a folder", "Pick a folder in the list first.")
            return
        idx = sel[0]
        current = self.library_folders[idx]
        dlg = FolderOverrideDialog(self, current)
        if dlg.result:
            self.library_folders[idx].update(dlg.result)
            label = current["path"]
            if dlg.result["mode"] != "normal":
                label += f"   (one artist: {dlg.result['artist']})"
            self.library_folder_list.delete(idx)
            self.library_folder_list.insert(idx, label)
            self._save_state()

    def _remove_library_folder(self):
        sel = self.library_folder_list.curselection()
        if not sel:
            return
        idx = sel[0]
        self.library_folder_list.delete(idx)
        del self.library_folders[idx]
        self._save_state()

    def _scan_library_clicked(self):
        if not self.library_folders:
            messagebox.showinfo("Add a folder", "Add at least one music folder to your listening library first.")
            return
        if self.worker_thread and self.worker_thread.is_alive():
            return

        db_path = self.search_db_entry.get().strip()
        if not db_path:
            first_root = os.path.abspath(self.library_folders[0]["path"])
            db_path = os.path.join(core.discwise_home(first_root), "listening_library_archive.db")
            self.search_db_entry.insert(0, db_path)

        self.scan_library_btn.config(state="disabled")
        self.library_status.config(text="Scanning...")
        self.library_progress.start(12)

        self._fix_folder_names()
        folders_copy = [dict(entry) for entry in self.library_folders]
        include_art = self.library_art_var.get()
        self.worker_thread = threading.Thread(
            target=self._run_library_scan, args=(folders_copy, db_path, include_art), daemon=True
        )
        self.worker_thread.start()

    def _run_library_scan(self, folders, db_path, include_art=False):
        try:
            overrides = {
                os.path.abspath(entry["path"]): entry["artist"]
                for entry in folders if entry["mode"] in ("flat", "nested") and entry["artist"]
            }
            roots = [entry["path"] for entry in folders]
            unique_artists, total_albums, total_tracks, skipped = core.write_library_index(
                roots, db_path, overrides, include_art_folders=include_art)
            self.log_queue.put(("library_done", {
                "db": db_path, "unique_artists": unique_artists,
                "total_albums": total_albums, "total_tracks": total_tracks, "skipped": skipped,
            }))
        except (Exception, SystemExit) as exc:
            self.log_queue.put(("library_error", str(exc) if str(exc) not in ("", "1")
                                else "A folder couldn't be found -- check the folder list."))

    def _on_library_done(self, result):
        self.scan_library_btn.config(state="normal")
        self.library_progress.stop()
        self.library_status.config(
            text=f"Indexed {result['unique_artists']} artists, {result['total_albums']} albums, "
                 f"{result['total_tracks']} tracks"
                 + (f" ({result['skipped']} picture-only folders skipped)" if result.get("skipped") else "")
        )
        self.search_db_entry.delete(0, "end")
        self.search_db_entry.insert(0, result["db"])
        self._save_state()

    def _on_library_error(self, message):
        self.scan_library_btn.config(state="normal")
        self.library_progress.stop()
        self.library_status.config(text="Error -- see message")
        messagebox.showerror("Something went wrong", message)

    def _browse_search_db(self):
        path = filedialog.askopenfilename(filetypes=[("SQLite database", "*.db"), ("All files", "*.*")])
        if path:
            self.search_db_entry.delete(0, "end")
            self.search_db_entry.insert(0, path)
            self._save_state()

    def _do_search(self):
        db_path = self.search_db_entry.get().strip()
        if not db_path or not os.path.exists(db_path):
            messagebox.showinfo("No database", "Scan your library first, or browse to an existing listening_library_archive.db.")
            return

        term = self.search_entry.get().strip()
        scope = self.search_scope.get()

        for row in self.results.get_children():
            self.results.delete(row)
        self._search_row_paths = {}

        try:
            found = core.search_library(db_path, term, scope)
        except sqlite3.Error as exc:
            messagebox.showerror("Search error", str(exc))
            return

        rows = found["rows"]
        # Disc # only means something for a packing-list database; hide it for a plain library catalog.
        self.results["displaycolumns"] = (
            self._columns_with_disc if found["has_discs"] else self._columns_no_disc
        )

        for artist, album, disc_label, file_type, track_number, song, folder_path, status in rows:
            item_id = self.results.insert(
                "", "end", values=(artist, album, disc_label, status, file_type, track_number or "", song)
            )
            self._search_row_paths[item_id] = folder_path

        self.search_status.config(text=f"{len(rows)} result(s)")

    def _show_folder_for_selection(self, event):
        sel = self.results.selection()
        if not sel:
            return
        path = self._search_row_paths.get(sel[0])
        if path:
            if messagebox.askyesno("Open folder?", f"{path}\n\nOpen this folder?"):
                self._xdg_open(path)


if __name__ == "__main__":
    app = DiscPlannerGUI()
    app.mainloop()
