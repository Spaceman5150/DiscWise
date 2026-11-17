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
FLAC Verifier logic for DiscWise.

For every album folder of FLAC files it does the same checks, in the same order, as checking by hand:
  1. Integrity -- every file decodes all the way through ("flac -t"; ffmpeg stands in if flac isn't installed)
  2. Codec     -- every file really is FLAC (ffprobe), and what sample rate / bit depth it is
  3. Spectrogram -- one picture per album, from a song in the middle, saved to a folder to look at
  3b. Spectrum hint -- measures how high the music actually reaches, as a hint about whether the files
               were converted up from MP3 (it never calls anything good or bad, only what to look at)
  4. Tags      -- song titles, artist and album with extra text (the Tag Cleaner's scan), and missing basics
  5. Cover art -- is a picture embedded in the files
Nothing is changed in the music folder. The only thing written is the spectrogram pictures.
"""

import json
import os
import queue
import re
import shutil
import subprocess

import tag_cleaner

try:
    from mutagen.flac import FLAC
except ImportError:
    FLAC = None

OK, WARN, FAIL = "ok", "warn", "fail"
REQUIRED_TAGS = ("title", "artist", "album", "tracknumber")
COVER_NAMES = ("cover", "folder", "front", "album", "albumart")
SKIP_FOLDER_WORDS = ("spectrogram", "spectrograph")


def load_marks(path):
    """Albums whose spectrogram the person already looked at: {album_key: "fine" or "suspicious"}.
    The key is the album's own name (see tag_cleaner.album_key), not its location, so the marks survive a move.
    Older files saved the full path; those are converted here (if two albums collapse onto one name,
    'suspicious' wins)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    marks = {}
    for key, kind in data.items():
        key = tag_cleaner.album_key(key) if os.path.isabs(key) else key
        if marks.get(key) != "suspicious":
            marks[key] = kind
    return marks


def save_marks(path, marks):
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(marks, fh, ensure_ascii=False, indent=0)
    except OSError:
        pass


def missing_tools():
    """Plain-language list of what still needs installing; empty when everything is there."""
    missing = []
    if FLAC is None:
        missing.append(("mutagen", "pip install mutagen --break-system-packages"))
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        missing.append(("ffmpeg", "sudo apt install ffmpeg"))
    return missing


def flac_tool_present():
    return shutil.which("flac") is not None


def find_albums(root):
    """Every folder under root (root included) that holds .flac files, in order."""
    albums = []
    for folder, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not any(w in d.lower() for w in SKIP_FOLDER_WORDS)]
        flacs = sorted(n for n in names if n.lower().endswith(".flac"))
        if flacs:
            albums.append((folder, flacs))
    albums.sort()
    return albums


def _run(cmd, timeout=900):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                          text=True, errors="replace")


def check_integrity(paths, cancelled=None):
    """Returns (status, text, bad_names)."""
    bad = []
    use_flac = flac_tool_present()
    for path in paths:
        if cancelled and cancelled():
            break
        try:
            if use_flac:
                result = _run(["flac", "-t", "-s", "--", path])
            else:
                result = _run(["ffmpeg", "-v", "error", "-xerror", "-i", path, "-f", "null", "-"])
            failed = result.returncode != 0 or (not use_flac and result.stderr.strip())
        except (OSError, subprocess.TimeoutExpired):
            failed = True
        if failed:
            bad.append(os.path.basename(path))
    if bad:
        return FAIL, f"{len(bad)} of {len(paths)} bad", bad
    return OK, "Pass" if use_flac else "Pass (ffmpeg)", bad


def check_codec(paths):
    """Returns (status, text, problems). Fails if anything isn't FLAC; notes mixed qualities."""
    formats, problems = {}, []
    for path in paths:
        try:
            result = _run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                           "stream=codec_name,sample_rate,bits_per_raw_sample,bits_per_sample,channels",
                           "-of", "json", path], timeout=60)
            stream = (json.loads(result.stdout or "{}").get("streams") or [{}])[0]
        except (OSError, subprocess.TimeoutExpired, ValueError):
            stream = {}
        codec = stream.get("codec_name", "")
        if codec != "flac":
            problems.append(f"{os.path.basename(path)}: {codec or 'unreadable'}")
            continue
        rate = int(stream.get("sample_rate") or 0)
        bits = stream.get("bits_per_raw_sample") or stream.get("bits_per_sample") or "?"
        key = f"{rate / 1000:g} kHz / {bits}-bit"
        formats[key] = formats.get(key, 0) + 1
    if problems:
        return FAIL, f"{len(problems)} not FLAC", problems
    if not formats:
        return FAIL, "couldn't read", problems
    if len(formats) > 1:
        return WARN, "Mixed FLAC: " + ", ".join(sorted(formats)), problems
    return OK, next(iter(formats)) + " FLAC", problems


def _safe_name(text):
    text = text.replace(os.sep, " - ")
    text = re.sub(r'[<>:"|?*\x00-\x1f]', "", text)
    return re.sub(r"\s+", " ", text).strip(" .") or "album"


def make_spectrogram(path, out_png):
    """One picture of the whole song. Returns an error text, or "" when it worked."""
    os.makedirs(os.path.dirname(out_png), exist_ok=True)
    for graph in ("showspectrumpic=s=1200x700:legend=1", "showspectrumpic=s=1200x700:legend=0"):
        try:
            result = _run(["ffmpeg", "-y", "-v", "error", "-t", "900", "-i", path, "-lavfi", graph,
                           "-frames:v", "1", out_png], timeout=300)
        except (OSError, subprocess.TimeoutExpired):
            return "ffmpeg couldn't make the picture"
        if result.returncode == 0 and os.path.isfile(out_png):
            return ""
    return "ffmpeg couldn't make the picture"


# ---------------------------------------------------------------- spectrum hint
SHARP_DROP_DB = 20        # how far the sound must fall within 1 kHz to count as a hard ceiling
_BAND_RE = re.compile(r"\[astats@p(\d+) @ [^\]]+\]\s+RMS level dB:\s*(-?[\d.]+|-inf|nan)")


def _probe_audio(path):
    try:
        result = _run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                       "stream=sample_rate,bits_per_raw_sample,bits_per_sample", "-of", "json", path],
                      timeout=60)
        stream = (json.loads(result.stdout or "{}").get("streams") or [{}])[0]
        bits = int(stream.get("bits_per_raw_sample") or stream.get("bits_per_sample") or 16)
        return int(stream.get("sample_rate") or 0), bits
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return 0, 16


def _band_points(rate):
    """Frequencies (Hz) to measure 'how much sound is above this', from 10 kHz up to near the top."""
    top = min(rate / 2 - 1000, 46000)
    points, f = [], 10000
    while f <= top:
        points.append(f)
        f += 1000 if f < 24000 else 2000
    return points


def measure_spectrum(path, rate):
    """Returns the sound level (dB) above each of the band points, or None if it couldn't be measured."""
    points = _band_points(rate)
    if len(points) < 3:
        return None, None
    n = len(points)
    graph = "[0:a]aformat=channel_layouts=mono,asplit=%d%s;" % (n, "".join(f"[b{i}]" for i in range(n)))
    for i, freq in enumerate(points):
        graph += (f"[b{i}]firequalizer=gain='if(gte(f,{int(freq)}),0,-140)':accuracy=20:zero_phase=on,"
                  f"astats@p{i}=measure_perchannel=none:measure_overall=RMS_level[o{i}];")
    cmd = ["ffmpeg", "-v", "info", "-t", "120", "-i", path, "-filter_complex", graph.rstrip(";")]
    for i in range(n):
        cmd += ["-map", f"[o{i}]", "-f", "null", "-"]
    try:
        result = _run(cmd, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    found = {}
    for match in _BAND_RE.finditer(result.stderr):
        value = match.group(2)
        found[int(match.group(1))] = -150.0 if value in ("-inf", "nan") else float(value)
    if len(found) != n:
        return None, None
    return points, [found[i] for i in range(n)]


def classify_spectrum(rate, bits, points, levels):
    """Turns the measurements into (status, short text, detail). A hint only: quiet acoustic albums,
    old tape and some CD masters can stop early and still be perfectly real."""
    nyquist = rate / 2
    khz = lambda f: f"{f / 1000:g} kHz"
    if levels[0] < -90:
        return WARN, "Check · very quiet", "Too quiet to measure reliably"
    # a hard ceiling: the sound falls off a cliff between two neighbouring points
    cliff = None
    for i in range(len(levels) - 1):
        if levels[i] - levels[i + 1] >= SHARP_DROP_DB and points[i + 1] <= 24000:
            cliff = i
            break
    ceiling = points[cliff + 1] if cliff is not None else None
    if rate <= 48000:
        if cliff is not None and ceiling < nyquist - 2000:
            if ceiling >= 19500:
                return (OK, "Full range",
                        f"Stops sharply near {khz(ceiling)}, normal for a CD master (a high-bitrate MP3 can "
                        f"look the same)")
            if ceiling >= 18500:
                return (WARN, f"Check · {khz(ceiling)}",
                        f"Stops sharply by about {khz(ceiling)}, which happens with some CD masters and some MP3s")
            return (WARN, f"Possible MP3 · {khz(ceiling)}",
                    f"Stops sharply by about {khz(ceiling)}, a typical MP3 ceiling")
        floor = -95 if bits <= 16 else -125
        if levels[-1] > floor or cliff is not None:
            return OK, "Full range", f"Sound reaches up near {khz(points[-1] + 1000)}"
        reach = max([f for f, lv in zip(points, levels) if lv > floor] or [points[0]])
        return WARN, f"Check · {khz(reach)}", f"Fades out by about {khz(reach + 1000)}; normal for a soft or older recording"
    # hi-res files: a 44.1 kHz source stretched up stops at about 22 kHz
    if cliff is not None and ceiling < 22000:
        return (WARN, f"Possible MP3 · {khz(ceiling)}",
                f"Stops sharply by about {khz(ceiling)}, a typical MP3 ceiling, inside a hi-res file")
    try:
        a = levels[points.index(22000)]
        b = levels[points.index(26000)]
    except ValueError:
        return OK, "Full range", "Reaches the top of the range"
    if a - b >= 20 and b < levels[-1] + 15:
        return (WARN, "Possible upsample",
                "Nothing above about 22-24 kHz, like a CD-quality source stretched up to hi-res")
    return OK, "Full range", f"Sound reaches up past {khz(points[-1])}"


def check_spectrum(path):
    """Returns (status, short text, detail)."""
    rate, bits = _probe_audio(path)
    if rate < 32000:
        return OK, "Not checked", "Sample rate too low to judge"
    points, levels = measure_spectrum(path, rate)
    if not points:
        return WARN, "Not checked", "The measurement didn't finish"
    return classify_spectrum(rate, bits, points, levels)


def check_basic_tags(audios):
    """Which of title/artist/album/tracknumber are missing from how many songs."""
    missing = {}
    for audio in audios:
        for key in REQUIRED_TAGS:
            values = audio.get(key) or []
            if not any(str(v).strip() for v in values):
                missing[key] = missing.get(key, 0) + 1
    return missing


def check_art(audios, folder):
    """Returns (status, text). Notes a cover picture lying in the folder when the files have none."""
    have = sum(1 for a in audios if a.pictures)
    total = len(audios)
    if have == total and total:
        return OK, "All songs"
    cover = ""
    try:
        for name in os.listdir(folder):
            stem, ext = os.path.splitext(name)
            if stem.lower() in COVER_NAMES and ext.lower() in (".jpg", ".jpeg", ".png"):
                cover = name
                break
    except OSError:
        pass
    text = "None" if have == 0 else f"Missing in {total - have} of {total}"
    if cover:
        text += f" ({cover} in folder)"
    return WARN, text


class Verifier:
    """Runs the checks on a background thread. The window reads .events (lines for the log) and
    .snapshot() (progress and results) on its own schedule."""

    def __init__(self, ignore_path=None, marks_path=None):
        self.ignore_path = ignore_path
        self.marks_path = marks_path
        self.events = queue.Queue()
        self.results = []
        self.done = 0
        self.total = 0
        self.cancelled = False
        self.error = None
        self.finished = False

    def cancel(self):
        self.cancelled = True

    def say(self, text):
        self.events.put(text)

    def drain_events(self):
        lines = []
        while True:
            try:
                lines.append(self.events.get_nowait())
            except queue.Empty:
                return lines

    def snapshot(self):
        return {"done": self.done, "total": self.total, "cancelled": self.cancelled}

    def run(self, root, out_dir):
        try:
            self._run(root, out_dir)
        except Exception as exc:     # shown in the window, never a crash
            self.error = str(exc)
        self.finished = True

    def _run(self, root, out_dir):
        albums = find_albums(root)
        self.total = len(albums)
        if not albums:
            return
        parent = os.path.dirname(root.rstrip(os.sep)) or root
        self.say("Looking at the tags in every album...")
        tag_counts, folder_flags, file_flags, ignored_counts, details = {}, set(), set(), {}, {}
        ignored = tag_cleaner.load_ignored(self.ignore_path) if self.ignore_path else set()
        try:
            for item in tag_cleaner.scan(root, cancelled=lambda: self.cancelled, ignored=ignored):
                if item["ignored"]:           # the person already looked at this one and said leave it
                    ignored_counts[item["folder"]] = ignored_counts.get(item["folder"], 0) + 1
                    continue
                if item["kind"] == "files":
                    file_flags.add(item["folder"])
                    details.setdefault(item["folder"], []).append(f'Extra files: remove {item["before"]}')
                elif item["kind"] == "folder":
                    folder_flags.add(item["folder"])
                    details.setdefault(item["folder"], []).append(
                        f'Folder name: "{item["before"]}" \u2192 "{item["after"]}"')
                else:
                    tag_counts[item["folder"]] = tag_counts.get(item["folder"], 0) + 1
                    if item["kind"] == "title":
                        details.setdefault(item["folder"], []).append(
                            f'Song titles: remove "{item["ending"]}" ({item["count"]} songs)')
                    else:
                        details.setdefault(item["folder"], []).append(
                            f'{item["where"]}: "{item["before"]}" \u2192 "{item["after"]}"')
        except Exception as exc:
            self.say(f"  (the tag scan couldn't finish: {exc})")
        for index, (folder, names) in enumerate(albums):
            if self.cancelled:
                self.say("Stopped.")
                return
            paths = [os.path.join(folder, n) for n in names]
            album = os.path.relpath(folder, parent)
            self.say(f"[{index + 1} of {len(albums)}] {album}  ({len(paths)} files)")
            row = {"folder": folder, "album": album, "files": len(paths), "spectrogram": ""}

            row["integrity"] = check_integrity(paths, lambda: self.cancelled)[:2]
            self.say(f"    Integrity:  {row['integrity'][1]}")
            if self.cancelled:
                self.say("Stopped.")
                return
            row["codec"] = check_codec(paths)[:2]
            self.say(f"    Codec:      {row['codec'][1]}")

            audios = []
            for path in paths:
                try:
                    audios.append(FLAC(path))
                except Exception:
                    pass
            missing = check_basic_tags(audios)
            notes = []
            fixes = tag_counts.get(folder, 0)
            if fixes:
                notes.append(f"{fixes} tag fix" if fixes == 1 else f"{fixes} tag fixes")
            if missing:
                notes.append("missing " + ", ".join(f"{k} ({n})" for k, n in sorted(missing.items())))
            row["tag_issue"] = bool(fixes or missing)
            row["folder_issue"] = folder in folder_flags
            if row["folder_issue"]:
                notes.append("folder name")
            if folder in file_flags:
                notes.append("extra files")
                row["tag_issue"] = True
            if missing:
                details.setdefault(folder, []).append("Missing tags: " + ", ".join(
                    f"{k} on {n} song(s)" for k, n in sorted(missing.items())))
            row["tag_details"] = details.get(folder, [])
            if notes:
                row["tags"] = (WARN, "; ".join(notes))
            elif ignored_counts.get(folder):
                row["tags"] = (OK, f"Clean \u00b7 {ignored_counts[folder]} ignored")
            else:
                row["tags"] = (OK, "Clean")
            if not audios:
                row["tags"] = (FAIL, "couldn't read the tags")
                row["tag_issue"] = True
            self.say(f"    Tags:       {row['tags'][1]}")
            row["art"] = check_art(audios, folder) if audios else (FAIL, "couldn't read")
            self.say(f"    Cover art:  {row['art'][1]}")

            pick = paths[len(paths) // 2]
            png = os.path.join(out_dir, _safe_name(album) + ".png")
            problem = make_spectrogram(pick, png)
            if problem:
                self.say(f"    Spectrogram: {problem}")
            else:
                row["spectrogram"] = png
                self.say(f"    Spectrogram: saved ({os.path.basename(pick)})")

            hint = check_spectrum(pick) if not self.cancelled else (OK, "Not checked", "")
            row["spectrum"] = row["spectrum_orig"] = hint[:2]
            mark = (load_marks(self.marks_path).get(tag_cleaner.album_key(folder)) if self.marks_path else None)
            if mark:                       # the person already looked at this album's spectrogram
                row["mark"] = mark
                if mark == "fine":
                    row["spectrum"] = (OK, hint[1])
            self.say(f"    Spectrum:   {hint[1]}" + (f"  ({hint[2]})" if hint[2] else ""))

            statuses = [row[k][0] for k in ("integrity", "codec", "tags", "art", "spectrum")]
            row["overall"] = FAIL if FAIL in statuses else WARN if WARN in statuses else OK
            self.results.append(row)
            self.done = index + 1
