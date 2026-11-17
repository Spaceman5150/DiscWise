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
file_converter.py -- the FLAC-to-MP3 engine behind the File Converter tab.

Point it at a folder of FLAC files and a destination: it recreates the same folder structure in the
destination with MP3s (320 kbps, the highest MP3 quality), keeping the tags and the embedded cover
art. Anything already converted is skipped, so a run that was interrupted can simply be started again.

Normally it never changes or deletes the FLAC files. If you ask for it (the "verify, then delete the
FLACs" option), it first checks every MP3 against its FLAC (it plays all the way through, same length,
same title/artist/album/track tags, cover art came along) and then deletes ONLY the FLAC files whose MP3
passed every check. Then it tidies the source side: leftover files (cue sheets, logs, and pictures or
booklets that are confirmed to be in the destination) and the emptied folders go too, so no empty shell
of the album is left behind. Any folder that still holds a FLAC that didn't pass is left completely alone.
When it deletes, it works one album folder at a time, smallest first (convert, check, delete, clear up, then
the next), so disk space comes back as it goes, and it stops cleanly if the MP3 drive can't hold the next album.

Where the MP3s land: normally every folder inside the source is recreated inside the destination. If the
source is ONE artist's folder (say "Neil Young", with the albums inside), pass parent_name="Neil Young" and the
albums go into a "Neil Young" folder inside the destination instead of landing loose in it. That folder is used
if it's already there (ignoring capitals and stray spaces) and created if it isn't, so a big batch of artists
ends up neatly grouped. When the FLACs are deleted, the emptied folder you picked goes too.

No GUI code lives here (that's in disc_planner_gui.py), so it can also be used from a terminal:

    python3 file_converter.py "/path/to/FLAC Files" "/path/to/MP3 Files"
    python3 file_converter.py "/path/to/FLAC Files" "/path/to/MP3 Files" --delete-flacs
    python3 file_converter.py "/path/to/Neil Young" "/path/to/MP3 Files" --artist-folder

Needs ffmpeg (with the libmp3lame encoder). On Ubuntu:  sudo apt install ffmpeg
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

MP3_BITRATE = "320k"          # the highest bitrate MP3 supports
MP3_BYTES_PER_SECOND = 40000  # 320 kbit/s
FREE_SPACE_MARGIN = 200 * 1024 ** 2   # always keep this much of the MP3 drive spare when deleting as it goes
SOURCE_EXTENSION = ".flac"
# Everything that isn't a FLAC is carried along into the new folders -- cover pictures, booklets, PDFs,
# comics (.cbr/.cbz), videos, text files, whole "Extra Art" folders -- EXCEPT these, which would be
# wrong or useless next to MP3s (a cue sheet or rip log points at the FLAC files) or are just junk.
SKIP_EXTENSIONS = (".flac", ".cue", ".log", ".m3u", ".m3u8", ".md5", ".ffp", ".sfv", ".accurip",
                   ".db", ".ini", ".tmp", ".part", ".lnk", ".bak")
SKIP_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}


def is_extra_file(name):
    """True for a file that should travel along with the MP3s (see SKIP_EXTENSIONS above)."""
    lowered = name.lower()
    return not (name.startswith("._") or lowered in SKIP_NAMES or lowered.endswith(SKIP_EXTENSIONS))
NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


# --------------------------------------------------------------------------------------------
# Checking the tools and the folders
# --------------------------------------------------------------------------------------------
def check_tools():
    """
    Returns {"ffmpeg": path or None, "ffprobe": path or None, "problem": str or None}.
    "problem" is a plain-English explanation when conversion isn't possible yet.
    """
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    result = {"ffmpeg": ffmpeg, "ffprobe": ffprobe, "problem": None}
    if not ffmpeg:
        result["problem"] = ("ffmpeg isn't installed, and the converter needs it.\n\n"
                             "Install it by pasting this into a terminal:\n\n    sudo apt install ffmpeg")
        return result
    try:
        encoders = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True,
                                  text=True, timeout=30, **NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        encoders = ""
    if "libmp3lame" not in encoders:
        result["problem"] = ("This copy of ffmpeg can't make MP3s (it was built without the libmp3lame "
                             "encoder).\n\nThe normal Ubuntu package includes it:\n\n"
                             "    sudo apt install ffmpeg")
    return result


def resolve_folder(path):
    """
    Finds the real folder a typed or browsed path means. The folder-picker dialog drops spaces at
    the end of a name, so "Jimi Hendrix " (with a trailing space, which is easy to end up with) comes
    back as "Jimi Hendrix" and doesn't exist. If the path isn't found exactly, each part is matched
    against the real names in its parent folder, ignoring spaces at the start and end.
    Returns the real path, or the original text if nothing matches.
    """
    if not path or os.path.isdir(path):
        return path
    parts = os.path.abspath(path).split(os.sep)
    current = os.sep
    for part in parts[1:]:
        if not part:
            continue
        candidate = os.path.join(current, part)
        if os.path.exists(candidate):
            current = candidate
            continue
        try:
            matches = [n for n in os.listdir(current) if n.strip() == part.strip()]
        except OSError:
            return path
        if len(matches) != 1:
            return path
        current = os.path.join(current, matches[0])
    return current if os.path.isdir(current) else path


def default_destination(source):
    """Suggested destination: a sibling folder named after the source, e.g. 'FLAC Files (MP3)'."""
    source = os.path.abspath(source.rstrip("/\\"))
    return os.path.join(os.path.dirname(source), os.path.basename(source).strip() + " (MP3)")


def validate_paths(source, dest):
    """Returns a plain-English problem with the chosen folders, or None if they're fine."""
    source = resolve_folder(source)
    if not source or not os.path.isdir(source):
        return "Pick a source folder that exists."
    if not dest:
        return "Pick a destination folder for the MP3s."
    src, dst = os.path.realpath(source), os.path.realpath(dest)
    if src == dst:
        return "The destination can't be the same folder as the source."
    if dst.startswith(src + os.sep):
        return ("The destination can't be inside the source folder -- the new MP3s would end up mixed "
                "in with your FLAC files. Pick a different place, such as next to the source folder.")
    return None


def scan_flacs(source):
    """Every FLAC under `source`: a sorted list of (full_path, path_relative_to_source), plus total bytes."""
    found, total = [], 0
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames.sort()
        for name in sorted(filenames):
            if name.lower().endswith(SOURCE_EXTENSION) and not name.startswith("._"):
                full = os.path.join(dirpath, name)
                try:
                    total += os.path.getsize(full)
                except OSError:
                    pass
                found.append((full, os.path.relpath(full, source)))
    return found, total


def resolve_child(parent, name):
    """
    The name to use for the folder `name` inside `parent`: the one that's already there if there is one
    (same name, or the same apart from capitals and spaces at the ends -- "neil young" finds "Neil Young "),
    otherwise `name` itself, which is then created when needed. Nothing is created here.
    """
    if not name or os.path.exists(os.path.join(parent, name)):
        return name
    try:
        matches = [n for n in os.listdir(parent)
                   if os.path.isdir(os.path.join(parent, n)) and n.strip().casefold() == name.strip().casefold()]
    except OSError:
        return name
    return matches[0] if len(matches) == 1 else name


def mp3_path_for(dest, relative_flac_path):
    """Where the MP3 for a given FLAC (relative path) goes inside the destination."""
    return os.path.join(dest, os.path.splitext(relative_flac_path)[0] + ".mp3")


# --------------------------------------------------------------------------------------------
# One file
# --------------------------------------------------------------------------------------------
def friendly_error(text):
    """Turns ffmpeg's technical complaint into something a person can act on."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    last = lines[-1] if lines else ""
    last = last.split("] ", 1)[-1] if last.startswith("[") else last
    lowered = (text or "").lower()
    if any(hint in lowered for hint in ("invalid data", "nothing was written", "no packets", "does not contain any stream",
                                         "could not find codec", "end of file")):
        return "couldn't be read as audio -- the file may be damaged or isn't really a FLAC"
    if "permission denied" in lowered:
        return "permission denied"
    if "no space left" in lowered:
        return "the destination drive is full"
    return last or "ffmpeg couldn't convert this file"



def _probe(ffprobe, path):
    """Sample rate and channel count of the first audio stream ({} if it can't be read)."""
    if not ffprobe:
        return {}
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries",
             "stream=sample_rate,channels", "-of", "json", path],
            capture_output=True, text=True, timeout=60, **NO_WINDOW).stdout
        stream = json.loads(out).get("streams", [{}])[0]
        return {"sample_rate": int(stream.get("sample_rate", 0)), "channels": int(stream.get("channels", 0))}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError, json.JSONDecodeError):
        return {}


# --------------------------------------------------------------------------------------------
# Checking an MP3 against its FLAC (used before any FLAC is deleted)
# --------------------------------------------------------------------------------------------
DURATION_TOLERANCE = 0.5      # seconds the MP3 may differ from the FLAC (encoders add a few ms of padding)
CHECKED_TAGS = (("title", "title"), ("artist", "artist"), ("album", "album"), ("track", "track number"))


def human_size(n):
    """12345678 -> '11.8 MB' (for messages)."""
    for unit, size in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n} bytes"


def _norm_tag(key, value):
    value = " ".join(str(value).split()).casefold()
    if key == "track":                       # "3/12" and "3" and "03" are all track 3
        head = value.split("/")[0].strip()
        return head.lstrip("0") or head
    return value


def _probe_full(ffprobe, path):
    """
    What matters about a file for the before-delete comparison, or None if it can't be read:
    {"duration": seconds or None, "audio": number of audio streams, "art": has a cover picture,
     "tags": {title, artist, album, track}}.
    """
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", path],
                             capture_output=True, text=True, timeout=120, **NO_WINDOW).stdout
        data = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    streams = data.get("streams", [])
    audio = [st for st in streams if st.get("codec_type") == "audio"]
    art = any(st.get("codec_type") == "video" for st in streams)
    raw = {}
    for source in [data.get("format", {})] + audio:
        for key, value in (source.get("tags") or {}).items():
            key = key.lower()
            key = "track" if key in ("track", "tracknumber") else key
            if key in dict(CHECKED_TAGS) and str(value).strip() and key not in raw:
                raw[key] = _norm_tag(key, value)
    try:
        duration = float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        duration = None
    return {"duration": duration, "audio": len(audio), "art": art, "tags": raw}


# Plain-English "what to do next" for each reason a file can be kept or fail. (substring of the reason, advice, retry helps?)
ADVICE = (
    ("another FLAC in this folder would produce the very same MP3 name",
     "Two FLAC files here would become the same MP3 name. Rename one of them (for example add the disc number), then Retry.", False),
    ("no MP3 for it", "Press Retry. If it fails again, play the FLAC to see if it's damaged, and check the drive isn't full.", True),
    ("only half written", "Press Retry. It will throw the half-finished MP3 away and make a fresh one.", True),
    ("ffprobe is missing", "Install it by pasting  sudo apt install ffmpeg  into a terminal, then run this again.", False),
    ("the FLAC itself couldn't be read", "The FLAC file looks damaged. Try playing it. If it won't play, get the album again.", False),
    ("can't be read as audio", "Press Retry. It will delete that MP3 and make a new one.", True),
    ("couldn't compare the length", "Press Retry. If it fails again, play the FLAC to see if it's damaged.", True),
    ("cut short", "Press Retry. It will delete the short MP3 and convert the song again.", True),
    ("tag didn't make it", "Open the FLAC and the MP3 in a tag viewer and compare. If they only differ in how they're written "
     "(two artists, say), the MP3 is fine and you can delete the FLAC by hand. If something is really missing, fix it in "
     "Clean Folders and Tags, then Retry.", True),
    ("cover picture didn't make it", "The picture can't go into an MP3 as it is. Retry won't change that. If a cover.jpg sits "
     "beside the album and you're happy with that, delete the FLAC by hand; otherwise use Embed Missing Cover Art with a plain JPEG first.", False),
    ("doesn't play all the way through", "Press Retry. If it fails twice, the FLAC may be damaged: play it to check.", True),
    ("couldn't delete it", "The system wouldn't let this file go (permissions, or another program has it open). Close anything using it, "
     "or delete it by hand. The MP3 is fine.", False),
    ("couldn't be read as audio", "The FLAC looks damaged or isn't really a FLAC. Try playing it. If it won't play, get the album again.", False),
    ("permission denied", "Check you're allowed to write to the MP3 folder, then Retry.", True),
    ("drive is full", "Free up space on the MP3 drive, then Retry.", True),
)


def advice_for(reason):
    """(what to do, whether pressing Retry can help) for a reason a file was kept or failed."""
    lowered = (reason or "").lower()
    for key, text, helps in ADVICE:
        if key.lower() in lowered:
            return text, helps
    return "Press Retry. If it fails again, play the FLAC to see if it's damaged.", True


def build_command(ffmpeg, source, target_part, info, with_art=True):
    """The ffmpeg command line for one FLAC -> MP3 conversion."""
    cmd = [ffmpeg, "-nostdin", "-y", "-v", "error", "-i", source, "-map", "0:a:0"]
    if with_art:
        cmd += ["-map", "0:v:0?"]                 # the embedded cover picture, if the file has one
    cmd += ["-c:a", "libmp3lame", "-b:a", MP3_BITRATE]
    if with_art:
        cmd += ["-c:v", "copy", "-disposition:v:0", "attached_pic"]
    rate = info.get("sample_rate", 0)
    if rate > 48000:                              # MP3 tops out at 48 kHz; bring hi-res files down cleanly
        cmd += ["-ar", "44100" if rate % 44100 == 0 else "48000"]
    if info.get("channels", 0) > 2:               # MP3 is stereo at most here
        cmd += ["-ac", "2"]
    cmd += ["-map_metadata", "0", "-id3v2_version", "3", "-write_id3v1", "1", "-f", "mp3", target_part]
    return cmd


# --------------------------------------------------------------------------------------------
# A whole run
# --------------------------------------------------------------------------------------------
class Converter:
    """
    One conversion run. Call run() (it blocks, so start it in a thread); the GUI polls snapshot()
    for progress and can call cancel() at any time.
    """

    def __init__(self, source, dest, copy_extras=True, workers=None, delete_after=False, delete_log=None, redo=None,
                 parent_name=None):
        self.source = os.path.abspath(source)
        self.dest = os.path.abspath(dest)
        self.copy_extras = copy_extras
        self.delete_after = delete_after      # verify every MP3, then delete the FLACs that passed
        self.delete_log = delete_log          # optional text file that gets one line per deleted FLAC
        self.redo = set(redo or ())           # relative FLAC paths whose existing MP3 is thrown away and made again
        self.parent_name = parent_name or None  # source is one artist's folder: put it in a folder of this name in dest
        self._names = {}                      # remembered folder-name matches in the destination
        self._name_lock = threading.Lock()
        cpus = os.cpu_count() or 2
        self.workers = workers or max(1, min(6, cpus - 1))
        tools = check_tools()
        self.ffmpeg, self.ffprobe = tools["ffmpeg"], tools["ffprobe"]
        self._lock = threading.Lock()
        self._procs = set()
        self._cancel = threading.Event()
        self.total = 0
        self.done = 0
        self.converted = 0
        self.skipped = 0
        self.failed = []              # [(relative path, reason)]
        self.extras_copied = 0
        self.verified = 0
        self.held_back = []           # [(relative path, reason)] FLACs that did NOT pass and were kept
        self.deleted = 0
        self.deleted_bytes = 0
        self.extras_differ = []       # other files that aren't in the destination (or differ)
        self.leftovers_deleted = 0
        self.folders_removed = 0
        self.gain_bytes = 0           # drive space recovered by THIS run: FLAC size minus MP3 size, per FLAC deleted
        self.mp3_bytes = 0            # size of the MP3s that replaced the FLACs that were deleted
        self.junk_bytes = 0           # cue sheets, logs and the like deleted (never copied, so pure gain)
        self.same_drive = True        # MP3s are written to the same drive the FLACs are deleted from
        self.albums_total = 0
        self.album_index = 0
        self.album_name = ""
        self.low_space = None         # set when it stops because the MP3 drive is too full for the next album
        self.current = ""
        self.finished = False
        self.phase = "converting"
        self._events = []             # lines for the on-screen log, handed out by drain_events()

    def _note(self, line):
        with self._lock:
            self._events.append(line)

    def drain_events(self):
        """The log lines written since the last call (oldest first), so a screen can show them live."""
        with self._lock:
            lines, self._events = self._events, []
        return lines

    # ---- control / progress
    def cancel(self):
        """Stop as soon as possible: no new files start, and any ffmpeg already running is ended."""
        self._cancel.set()
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            try:
                proc.terminate()
            except OSError:
                pass

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def free_gained(self):
        """
        Bytes of drive space this run has recovered (never below 0). It is worked out from the files DiscWise
        itself deleted -- each FLAC's size minus the size of the MP3 that replaced it -- so downloads or anything
        else happening on the drive never change it. (If the MP3s go to a different drive, the MP3s cost the
        source drive nothing, so the whole FLAC size counts.)
        """
        with self._lock:
            return max(0, self.gain_bytes)

    def _add_gain(self, freed, replacement=0):
        """Adds one deleted file to the running tally. `replacement` is what its copy in the destination takes."""
        with self._lock:
            self.gain_bytes += freed - (replacement if self.same_drive else 0)

    # ---- where things go in the destination
    def _child(self, parent, name):
        key = (parent, name)
        with self._name_lock:
            if key not in self._names:
                self._names[key] = resolve_child(parent, name)
            return self._names[key]

    def _dest_path(self, relative):
        """Where a file or folder (given relative to the source folder) belongs in the destination."""
        parts = relative.split(os.sep)
        if self.parent_name:
            return os.path.join(self.dest, self._child(self.dest, self.parent_name), *parts)
        if len(parts) > 1:                                   # reuse an artist folder that's already there
            return os.path.join(self.dest, self._child(self.dest, parts[0]), *parts[1:])
        return os.path.join(self.dest, relative)

    def _mp3_for(self, relative):
        return os.path.splitext(self._dest_path(relative))[0] + ".mp3"

    def landing_folder(self):
        """The folder the MP3s end up in (the destination itself, or the artist's folder inside it)."""
        if self.parent_name:
            return os.path.join(self.dest, resolve_child(self.dest, self.parent_name))
        return self.dest

    def snapshot(self):
        gained = self.free_gained()
        with self._lock:
            return {"total": self.total, "done": self.done, "converted": self.converted,
                    "skipped": self.skipped, "failed": len(self.failed), "current": self.current,
                    "finished": self.finished, "cancelled": self.cancelled, "phase": self.phase,
                    "verified": self.verified, "held_back": len(self.held_back), "deleted": self.deleted,
                    "leftovers_deleted": self.leftovers_deleted, "folders_removed": self.folders_removed,
                    "albums_total": self.albums_total, "album_index": self.album_index,
                    "album_name": self.album_name, "low_space": bool(self.low_space), "free_gained": gained}

    # ---- the work
    def _run_ffmpeg(self, cmd):
        """Runs one ffmpeg process; returns (return_code, error_text). Stops early if cancelled."""
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, **NO_WINDOW)
        with self._lock:
            self._procs.add(proc)
        try:
            _out, err = proc.communicate()
        finally:
            with self._lock:
                self._procs.discard(proc)
        return proc.returncode, (err or "").strip()

    def _convert_one(self, full, relative):
        """Converts one file. Returns "converted", "skipped", "cancelled" or ("failed", reason)."""
        target = self._mp3_for(relative)
        if relative in self.redo:
            for stale in (target, target + ".part"):
                try:
                    os.remove(stale)
                except OSError:
                    pass
        if os.path.exists(target) and os.path.getsize(target) > 0:
            return "skipped"
        if self.cancelled:
            return "cancelled"
        os.makedirs(os.path.dirname(target), exist_ok=True)
        part = target + ".part"
        info = _probe(self.ffprobe, full)
        try:
            code, err = self._run_ffmpeg(build_command(self.ffmpeg, full, part, info, with_art=True))
            if code != 0 and not self.cancelled:
                # Most often an odd cover picture the MP3 can't hold -- try again without it.
                code, err2 = self._run_ffmpeg(build_command(self.ffmpeg, full, part, info, with_art=False))
                err = err2 or err
            if self.cancelled:
                return "cancelled"
            if code != 0 or not os.path.exists(part) or os.path.getsize(part) == 0:
                return ("failed", friendly_error(err) if err else f"ffmpeg stopped with code {code}")
            os.replace(part, target)
            return "converted"
        except OSError as exc:
            return ("failed", str(exc))
        finally:
            if os.path.exists(part):
                try:
                    os.remove(part)
                except OSError:
                    pass

    def _copy_extras(self, only_dir=None):
        """
        Copies the non-FLAC files (pictures, booklets, extra folders...) across, skipping any that are already
        there. With only_dir, just the files sitting directly in that one folder (used album by album).
        """
        copied = 0
        if only_dir is None:
            with self._lock:
                self.phase = "extras"
            self._note("--- Copying the other files (pictures, booklets, comics, extra folders...) ---")
            walker = os.walk(self.source)
        else:
            try:
                names = sorted(n for n in os.listdir(only_dir) if os.path.isfile(os.path.join(only_dir, n)))
            except OSError:
                return 0
            walker = [(only_dir, [], names)]
        for dirpath, dirnames, filenames in walker:
            dirnames.sort()
            for name in sorted(filenames):
                if self.cancelled:
                    return copied
                if is_extra_file(name):
                    src = os.path.join(dirpath, name)
                    relative = os.path.relpath(src, self.source)
                    dst = self._dest_path(relative)
                    if not os.path.exists(dst):
                        try:
                            os.makedirs(os.path.dirname(dst), exist_ok=True)
                            shutil.copy2(src, dst)
                            copied += 1
                            self._note(f"  copied     {relative}")
                        except OSError as exc:
                            self._note(f"  COULDN'T COPY  {relative}: {exc}")
        return copied

    # ---- verify, then delete
    def _run_capture(self, cmd):
        """Runs a tool and returns (return_code, stderr text). Registered so cancel() can stop it."""
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, **NO_WINDOW)
        with self._lock:
            self._procs.add(proc)
        try:
            _out, err = proc.communicate()
        finally:
            with self._lock:
                self._procs.discard(proc)
        return proc.returncode, (err or "").strip()

    def _verify_one(self, full, relative, shared_targets):
        """Is the MP3 for this FLAC good enough that the FLAC can go? Returns (True, "") or (False, reason)."""
        target = self._mp3_for(relative)
        if shared_targets.get(target, 0) > 1:
            return False, "another FLAC in this folder would produce the very same MP3 name"
        if not os.path.isfile(target) or os.path.getsize(target) == 0:
            return False, "no MP3 for it in the destination"
        if os.path.exists(target + ".part"):
            return False, "its MP3 is only half written"
        if not self.ffprobe:
            return False, "ffprobe is missing, so the MP3 can't be checked"
        src = _probe_full(self.ffprobe, full)
        if src is None:
            return False, "the FLAC itself couldn't be read"
        out = _probe_full(self.ffprobe, target)
        if out is None or out["audio"] < 1:
            return False, "the MP3 can't be read as audio"
        if src["duration"] is None or out["duration"] is None:
            return False, "couldn't compare the length of the song with its MP3"
        if abs(src["duration"] - out["duration"]) > DURATION_TOLERANCE:
            return False, (f"the MP3 is {out['duration']:.1f}s long but the FLAC is {src['duration']:.1f}s "
                           "(cut short?)")
        for key, label in CHECKED_TAGS:
            if key in src["tags"] and out["tags"].get(key) != src["tags"][key]:
                return False, f"the {label} tag didn't make it into the MP3 the same"
        if src["art"] and not out["art"]:
            return False, "the FLAC's cover picture didn't make it into the MP3"
        if self.cancelled:
            return False, "cancelled"
        code, err = self._run_capture([self.ffmpeg, "-nostdin", "-v", "error", "-i", target,
                                       "-map", "0:a:0", "-f", "null", "-"])
        if self.cancelled:
            return False, "cancelled"
        if code != 0 or err:
            return False, "the MP3 doesn't play all the way through"
        return True, ""

    def _check_extras(self):
        """Lists the other files (pictures, booklets...) that aren't in the destination the way they are here."""
        missing = []
        for dirpath, dirnames, filenames in os.walk(self.source):
            dirnames.sort()
            for name in sorted(filenames):
                if not is_extra_file(name):
                    continue
                src = os.path.join(dirpath, name)
                relative = os.path.relpath(src, self.source)
                dst = self._dest_path(relative)
                try:
                    if not os.path.isfile(dst) or os.path.getsize(dst) != os.path.getsize(src):
                        missing.append(relative)
                except OSError:
                    missing.append(relative)
        return missing

    def _clean_dir(self, dirpath, log):
        """Deletes the leftover files in one folder (never FLACs), then the folder if that left it empty.
        Returns True if the folder was removed."""
        differ = set(self.extras_differ)
        kept_something = False
        try:
            filenames = sorted(n for n in os.listdir(dirpath) if os.path.isfile(os.path.join(dirpath, n))
                               or os.path.islink(os.path.join(dirpath, n)))
        except OSError:
            return False
        for name in filenames:
            full = os.path.join(dirpath, name)
            relative = os.path.relpath(full, self.source)
            if is_extra_file(name):
                twin = self._dest_path(relative)
                try:
                    safe = (self.copy_extras and relative not in differ and os.path.isfile(twin)
                            and os.path.realpath(twin) != os.path.realpath(full)
                            and os.path.getsize(twin) == os.path.getsize(full))
                except OSError:
                    safe = False
                if not safe:
                    self._note(f"  KEPT       {relative}  (no identical copy in the destination -- drag it over to "
                               "the MP3 folder by hand if you want it, or delete it if you don't)")
                    kept_something = True
                    continue
            try:
                leftover_size = os.path.getsize(full)
            except OSError:
                leftover_size = 0
            try:
                os.remove(full)
            except OSError as exc:
                self._note(f"  COULDN'T DELETE  {relative}: {exc}")
                kept_something = True
                continue
            # a picture or booklet was copied across, so the drive gains nothing; cue sheets, logs and the like were not
            self._add_gain(leftover_size, leftover_size if is_extra_file(name) else 0)
            if not is_extra_file(name):
                with self._lock:
                    self.junk_bytes += leftover_size
            with self._lock:
                self.leftovers_deleted += 1
                self._events.append(f"  deleted    {relative}")
            if log:
                log.write(f"{full}\n")
        if os.path.abspath(dirpath) == self.source or kept_something:
            return False                          # never remove the folder you pointed it at
        try:
            os.rmdir(dirpath)                     # only works if it's really empty
        except OSError:
            return False
        with self._lock:
            self.folders_removed += 1
            self._events.append(f"  removed folder  {os.path.relpath(dirpath, self.source)}")
        if log:
            log.write(f"{dirpath}/  (folder)\n")
        return True

    def _clean_leftovers(self, log, only_dir=None):
        """
        After the FLACs are gone: delete what's left beside them and the emptied folders. A folder is only
        touched when no FLAC remains anywhere inside it. A picture/booklet/etc. is deleted only when an
        identical copy (same size) is in the destination; cue sheets, logs and other junk that are never
        copied go too. The source folder itself is kept. With only_dir, just that album's folder (and any
        parent folders that become empty because of it).
        """
        source_root = os.path.realpath(self.source)
        if only_dir is not None:
            if scan_flacs(only_dir)[0] or not os.path.realpath(only_dir).startswith(source_root):
                return                            # a FLAC is still in there: leave the whole folder alone
            self._clean_dir(only_dir, log)
            parent = os.path.dirname(os.path.abspath(only_dir))
            while len(parent) > len(self.source) and parent != self.source:
                try:
                    os.rmdir(parent)
                except OSError:
                    break
                with self._lock:
                    self.folders_removed += 1
                    self._events.append(f"  removed folder  {os.path.relpath(parent, self.source)}")
                parent = os.path.dirname(parent)
            return
        with self._lock:
            self.phase = "cleaning"
        self._note("--- Clearing out what's left on the FLAC side (leftover files and empty folders) ---")
        remaining, _bytes = scan_flacs(self.source)
        protected = set()                         # folders with a FLAC still somewhere inside them
        for full, _rel in remaining:
            folder = os.path.dirname(os.path.abspath(full))
            while len(folder) >= len(self.source) and folder not in protected:
                protected.add(folder)
                if folder == self.source:
                    break
                folder = os.path.dirname(folder)
        for dirpath, dirnames, filenames in os.walk(self.source, topdown=False):
            if self.cancelled:
                self._note("Cancelled -- the rest was left alone.")
                return
            if os.path.abspath(dirpath) in protected or not os.path.realpath(dirpath).startswith(source_root):
                continue
            self._clean_dir(dirpath, log)

    def _convert_batch(self, files):
        """Converts a list of FLACs (one album, or everything when nothing is being deleted)."""
        count, local = len(files), 0
        with self._lock:
            self.phase = "converting"
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self._convert_one, full, rel): rel for full, rel in files}
            for future in as_completed(futures):
                relative = futures[future]
                try:
                    outcome = future.result()
                except Exception as exc:  # never let one odd file stop the whole run
                    outcome = ("failed", str(exc))
                with self._lock:
                    self.current = relative
                    self.done += 1
                    local += 1
                    counter = f"[{local:>{len(str(count))}}/{count}]"
                    if outcome == "converted":
                        self.converted += 1
                        self._events.append(f"{counter}  converted  {relative}")
                    elif outcome == "skipped":
                        self.skipped += 1
                        self._events.append(f"{counter}  skipped    {relative}  (already there)")
                    elif outcome != "cancelled":
                        self.failed.append((relative, outcome[1]))
                        self._events.append(f"{counter}  FAILED     {relative}: {outcome[1]}")
                        self._events.append(f"             what to do: {advice_for(outcome[1])[0]}")

    def _verify_files(self, files):
        """Checks each MP3 against its FLAC. Returns the [(full, relative)] that passed every check."""
        with self._lock:
            self.phase = "verifying"
        self._note("  checking the MP3s against their FLAC files...")
        counts = {}
        for _full, relative in files:
            target = self._mp3_for(relative)
            counts[target] = counts.get(target, 0) + 1
        passed, local, count = [], 0, len(files)
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self._verify_one, full, rel, counts): (full, rel) for full, rel in files}
            for future in as_completed(futures):
                full, relative = futures[future]
                try:
                    ok, reason = future.result()
                except Exception as exc:  # an odd file is held back, never allowed to stop the check
                    ok, reason = False, str(exc)
                with self._lock:
                    local += 1
                    self.current = relative
                    counter = f"[{local:>{len(str(count))}}/{count}]"
                    if ok:
                        self.verified += 1
                        passed.append((full, relative))
                        self._events.append(f"{counter}  checked    {relative}")
                    elif reason != "cancelled":
                        self.held_back.append((relative, reason))
                        self._events.append(f"{counter}  HELD BACK  {relative}: {reason}")
                        self._events.append(f"             what to do: {advice_for(reason)[0]}")
        return passed

    def _delete_files(self, passed, log):
        """Deletes the FLACs that passed. Anything that can't be deleted is kept and listed."""
        with self._lock:
            self.phase = "deleting"
        source_root = os.path.realpath(self.source)
        album_flac = album_mp3 = album_count = 0
        for full, relative in sorted(passed, key=lambda item: item[1]):
            if self.cancelled:
                self._note("Cancelled -- the rest of the FLAC files were left alone.")
                return
            with self._lock:
                self.current = relative
            try:
                if not os.path.realpath(os.path.dirname(full)).startswith(source_root):
                    raise OSError("not inside the source folder")
                size = os.path.getsize(full)
                try:
                    mp3_size = os.path.getsize(self._mp3_for(relative))
                except OSError:
                    mp3_size = 0
                os.remove(full)
            except OSError as exc:
                reason = f"couldn't delete it: {exc}"
                with self._lock:
                    self.held_back.append((relative, reason))
                    self._events.append(f"  COULDN'T DELETE  {relative}: {exc}")
                    self._events.append(f"             what to do: {advice_for(reason)[0]}")
                continue
            self._add_gain(size, mp3_size)
            album_flac += size
            album_mp3 += mp3_size
            album_count += 1
            with self._lock:
                self.mp3_bytes += mp3_size
                self.deleted += 1
                self.deleted_bytes += size
                self._events.append(f"  deleted    {relative}")
            if log:
                log.write(f"{full}\n")
        if album_count:
            with self._lock:
                running = max(0, self.gain_bytes)
            self._note(f"  this folder: {album_count} FLAC(s) of {human_size(album_flac)} replaced by MP3s of "
                       f"{human_size(album_mp3)} -> {human_size(max(0, album_flac - album_mp3))} recovered "
                       f"(running total {human_size(running)})")

    def _duration(self, path):
        try:
            out = subprocess.run([self.ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                                 capture_output=True, text=True, timeout=60, **NO_WINDOW).stdout.strip()
            return float(out)
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            return None

    def _space_needed(self, album_files, album_dir):
        """Bytes the MP3s (and the loose files beside them) of one album will take, worked out from song lengths."""
        need = 0
        for full, relative in album_files:
            target = self._mp3_for(relative)
            if relative not in self.redo and os.path.exists(target) and os.path.getsize(target) > 0:
                continue
            seconds = self._duration(full) if self.ffprobe else None
            try:
                need += int(seconds * MP3_BYTES_PER_SECOND * 1.02) + 262144 if seconds else os.path.getsize(full)
            except OSError:
                pass
        if self.copy_extras:
            try:
                for name in os.listdir(album_dir):
                    path = os.path.join(album_dir, name)
                    if is_extra_file(name) and os.path.isfile(path) and not os.path.exists(
                            self._dest_path(os.path.relpath(path, self.source))):
                        need += os.path.getsize(path)
            except OSError:
                pass
        return need

    def _album_groups(self, files):
        """The FLACs grouped by the folder they sit in (one album, or one disc of an album), smallest first."""
        groups = {}
        for full, relative in files:
            groups.setdefault(os.path.dirname(relative), []).append((full, relative))
        sized = []
        for folder, members in groups.items():
            total = 0
            for full, _rel in members:
                try:
                    total += os.path.getsize(full)
                except OSError:
                    pass
            sized.append((total, folder, members))
        sized.sort(key=lambda item: (item[0], item[1].lower()))
        return sized

    def _run_albums(self, files, log):
        """Convert -> check -> delete -> clear up, one album at a time, smallest first."""
        groups = self._album_groups(files)
        with self._lock:
            self.albums_total = len(groups)
        self._note(f"--- {len(groups)} album folder(s), smallest first. Each one is converted, checked, and its FLAC "
                   "files deleted before the next one starts, so space comes back as it goes. ---")
        for index, (size, folder, members) in enumerate(groups, 1):
            if self.cancelled:
                return
            album_dir = os.path.join(self.source, folder) if folder else self.source
            name = folder or "(the top folder)"
            with self._lock:
                self.album_index, self.album_name = index, name
            self._note(f"=== Album {index} of {len(groups)}: {name}  ({human_size(size)} of FLAC) ===")
            need = self._space_needed(members, album_dir)
            try:
                free = shutil.disk_usage(self.dest).free
            except OSError:
                free = None
            if free is not None and free < need + FREE_SPACE_MARGIN:
                with self._lock:
                    self.low_space = {"album": name, "need": need, "free": free, "margin": FREE_SPACE_MARGIN}
                self._note(f"STOPPED: the next album needs about {human_size(need)} for its MP3s, but only "
                           f"{human_size(free)} is free on the MP3 drive (and {human_size(FREE_SPACE_MARGIN)} "
                           "is always kept spare).")
                return
            self._convert_batch(members)
            if self.cancelled:
                return
            if self.copy_extras:
                with self._lock:
                    self.phase = "extras"
                self.extras_copied += self._copy_extras(only_dir=album_dir)
            passed = self._verify_files(members)
            if self.cancelled:
                self._note("Cancelled during the check, so this album's FLAC files were not deleted.")
                return
            self._delete_files(passed, log)
            if self.cancelled:
                return
            with self._lock:
                self.phase = "cleaning"
            self._clean_leftovers(log, only_dir=album_dir)

    def _final_sweep(self, log):
        """Once every album is done: the loose folders that held no FLACs (Extra Art...), then clear the rest."""
        if self.copy_extras:
            self.extras_copied += self._copy_extras()
            self.extras_differ = self._check_extras()
            if self.extras_differ:
                self._note(f"--- {len(self.extras_differ)} other file(s) (pictures, booklets...) aren't in the "
                           "destination exactly as they are here. These are NOT deleted. ---")
                for relative in self.extras_differ[:30]:
                    self._note(f"  {relative}")
                if len(self.extras_differ) > 30:
                    self._note(f"  ...and {len(self.extras_differ) - 30} more")
        if not self.cancelled:
            self._clean_leftovers(log)
            if self.parent_name and self.delete_after:
                try:
                    os.rmdir(self.source)             # the artist folder you picked, if it's now completely empty
                except OSError:
                    pass
                else:
                    with self._lock:
                        self.folders_removed += 1
                        self._events.append(f"  removed folder  {os.path.basename(self.source)}  (the folder you picked)")
                    if log:
                        log.write(f"{self.source}/  (folder)\n")

    def run(self):
        """Converts everything. Returns a summary dict (also available piece by piece via snapshot())."""
        files, _total_bytes = scan_flacs(self.source)
        with self._lock:
            self.total = len(files)
        os.makedirs(self.dest, exist_ok=True)
        try:
            self.same_drive = os.stat(self.source).st_dev == os.stat(self.dest).st_dev
        except OSError:
            self.same_drive = True

        log = None
        if self.delete_after and self.delete_log:
            try:
                log = open(self.delete_log, "a", encoding="utf-8")
                log.write(f"# {time.strftime('%Y-%m-%d %H:%M:%S')}  source {self.source}  ->  MP3s in {self.dest}\n")
            except OSError:
                log = None
        try:
            if not self.delete_after:
                self._convert_batch(files)
                if self.copy_extras and not self.cancelled:
                    self.extras_copied = self._copy_extras()
            else:
                self._run_albums(files, log)
                if not self.cancelled and not self.low_space:
                    self._final_sweep(log)
        finally:
            if log:
                log.close()
        retry_files = sorted({rel for rel, why in list(self.failed) + list(self.held_back)
                              if advice_for(why)[1]})
        with self._lock:
            self.finished = True
        return {"retry_files": retry_files, "total": len(files), "converted": self.converted, "skipped": self.skipped,
                "failed": list(self.failed), "extras_copied": self.extras_copied,
                "cancelled": self.cancelled, "delete_requested": self.delete_after,
                "verified": self.verified, "held_back": list(self.held_back),
                "deleted": self.deleted, "deleted_bytes": self.deleted_bytes,
                "leftovers_deleted": self.leftovers_deleted, "folders_removed": self.folders_removed,
                "extras_differ": list(self.extras_differ), "low_space": self.low_space,
                "albums_total": self.albums_total, "free_gained": self.free_gained(),
                "mp3_bytes": self.mp3_bytes, "junk_bytes": self.junk_bytes, "same_drive": self.same_drive}


# --------------------------------------------------------------------------------------------
# Terminal use
# --------------------------------------------------------------------------------------------
def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    delete_after = "--delete-flacs" in argv
    keep_parent = "--artist-folder" in argv       # the source is one artist's folder: keep it as a folder in the destination
    argv = [a for a in argv if a not in ("--delete-flacs", "--artist-folder")]
    if len(argv) != 2:
        print(__doc__)
        return 2
    source, dest = argv
    tools = check_tools()
    if tools["problem"]:
        print(tools["problem"])
        return 1
    problem = validate_paths(source, dest)
    if problem:
        print(problem)
        return 1
    converter = Converter(source, dest, delete_after=delete_after,
                          parent_name=os.path.basename(resolve_folder(source).rstrip("/\\")).strip() if keep_parent else None,
                          delete_log=os.path.join(os.environ.get("DISCWISE_DATA_DIR") or os.path.dirname(os.path.abspath(__file__)), "converter_deleted.log"))
    worker = threading.Thread(target=converter.run, daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            worker.join(1.0)
            s = converter.snapshot()
            print(f"\r{s['done']} of {s['total']} files   ", end="", flush=True)
    except KeyboardInterrupt:
        converter.cancel()
        worker.join()
        print("\nCancelled.")
        return 130
    s = converter.snapshot()
    print(f"\nDone: {s['converted']} converted, {s['skipped']} already there, {s['failed']} failed.")
    for relative, reason in converter.failed:
        print(f"  FAILED {relative}: {reason}")
    if delete_after:
        print(f"{converter.deleted} FLAC file(s) deleted, {converter.leftovers_deleted} leftover file(s) and "
              f"{converter.folders_removed} folder(s) cleared, {len(converter.held_back)} FLAC(s) kept.")
        for relative, reason in converter.held_back:
            print(f"  KEPT {relative}: {reason}")
    return 1 if converter.failed else 0


if __name__ == "__main__":
    sys.exit(main())
