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
Tag Cleaner logic for DiscWise (FLAC files, plus the song-title check for MP3 files).

It looks for two kinds of repeated text in an album's tags, WITHOUT a keyword list:
  1. text that ends nearly every song title, like "(2017 Remaster)" or "- Album Version"
  2. the same value sitting in the other tags of nearly every song (COMMENT, LABEL, ENCODED-BY,
     website fields...), like the "PMEDIA" stamped into everything by some uploaders
It also checks the artist and album tags for extra text, and the album folder names for the same kind of
leftovers (a year in front, brackets, a stamp on the end). It just reports what it finds, and the person decides. Nothing changes until they say so, and every
change is written to a log so it can be undone.
"""

import difflib
import json
import os
import re
import shutil
from collections import Counter
from datetime import datetime

try:
    from mutagen.flac import FLAC
    from mutagen.mp3 import EasyMP3
except ImportError:  # the GUI shows a friendly message if this is missing
    FLAC = EasyMP3 = None

EXTRA_FILE_EXTS = (".txt", ".m3u", ".m3u8")     # leftovers that come with a download; .cue sheets are never touched
SKIP_FOLDER_WORDS = ("spectrogram", "spectrograph")
MIN_TRACKS = 3        # the repeated-text checks need at least this many songs to see a pattern
SHARE = 0.8           # the text must end this share of the titles to be reported
MAX_WORDS = 8         # longest repeated ending considered


# Tags that legitimately hold the same value on every song of an album -- not reported.
NORMAL_TAGS = {"title", "artist", "album", "albumartist", "album artist", "date", "year", "genre",
               "discnumber", "disctotal", "totaldiscs", "tracknumber", "tracktotal", "totaltracks",
               "originaldate", "originalyear", "compilation", "discsubtitle", "encoder"}
NORMAL_PREFIXES = ("replaygain", "musicbrainz", "r128", "acoustid")


def mutagen_ready():
    return FLAC is not None


def _open_audio(path, create=False, mp3=None):
    """A FLAC or an MP3 (through its ID3 tags) opened so that audio["title"] reads and writes the same way.
    create=True gives an MP3 that has no tags yet an empty set to write into."""
    if mp3 is None:
        mp3 = path.lower().endswith(".mp3")
    if mp3:
        audio = EasyMP3(path)
        if audio.tags is None:
            if not create:
                raise ValueError("no tags")
            audio.add_tags()
        return audio
    return FLAC(path)


def _save_audio(audio):
    """Saves the tags. An MP3 keeps the ID3 version it came with (2.3 stays 2.3)."""
    if FLAC is not None and isinstance(audio, FLAC):
        audio.save()
        return
    inner = getattr(audio.tags, "_EasyID3__id3", None)
    version = getattr(inner, "version", (2, 4))
    audio.save(v2_version=3 if tuple(version[:2]) == (2, 3) else 4)


def _title(audio):
    values = audio.get("title") or []
    return values[0] if values else ""


def _strip_track_number(stem):
    return re.sub(r"^\s*\d+\s*[-_. ]+\s*", "", stem).strip()


def _normal(text):
    return re.sub(r"\s+", " ", text).strip().lower()


FIELD_KEYS = (("artist", "Artist tag"), ("album", "Album tag"))
_TRAIL_BRACKET = re.compile(r"\s*[\(\[\{][^\)\]\}]*[\)\]\}]\s*$")
_TRAIL_DASH = re.compile(r"\s+[-\u2013\u2014]\s+\S.*$")
# A bracket sitting just before a disc marker: "Album (CAT123) CD1". Only the bracket is taken off; the
# "CD1" stays, so the discs of one album keep different names and players don't merge them.
_BRACKET_BEFORE_DISC = re.compile(r"\s*[\(\[\{][^\)\]\}]*[\)\]\}](?=\s*(?:cd|disc|disk)\s*\d+\s*$)", re.IGNORECASE)
_URL = re.compile(r"(?i)(https?://\S+|www\.\S+|\b[\w-]+\.(?:com|net|org|info|ru|me|to)\b\S*)")


def clean_field_value(value):
    """For an artist or album tag: returns the value with extra baggage taken off the end (text in
    brackets or parentheses, text after a dash, a web address), or None if it looks normal."""
    new = value
    for _ in range(5):
        before = new
        new = _URL.sub("", new)
        new = _BRACKET_BEFORE_DISC.sub("", new)
        new = _TRAIL_BRACKET.sub("", new)
        new = _TRAIL_DASH.sub("", new)
        new = new.strip(" -\u2013\u2014_")
        if new == before:
            break
    new = re.sub(r"\s+", " ", new).strip()
    if not new or new == value.strip():
        return None
    return new


# A year in front: "1991 - Ten", "[1974] - Title", "2012. Artist - Title". Not a date like "1972.04.07" or
# "(1976-06-10)", which is part of the name.
_YEAR_LEAD = re.compile(r"^\s*(?:\[(?:19|20)\d\d\]|\((?:19|20)\d\d\)|(?:19|20)\d\d(?!\d|[.\-/]\d))"
                        r"\s*[-\u2013\u2014._]*\s*")
_BRACKET_START = re.compile(r"\s*[\[\(\{]")
_TRAIL_FORMAT = re.compile(r"(?i)[\s._+-]*\bflac\b(?:\+cue)?[\s._-]*$")


def propose_folder_name(name, album="", artist=""):
    """What an album folder could be called without the year in front, the artist in front, the
    bracketed notes and the stamps on the end -- or None if the name looks fine. If the album tag turns
    up inside the folder name, the tag is the answer. If not, the name is cut at its first bracket."""
    new = None
    if album:
        if re.search(r"(?<!\w)" + re.escape(album) + r"(?!\w)", name, re.IGNORECASE):
            new = album
    if new is None:
        text = _YEAR_LEAD.sub("", name)
        if artist:
            text = re.sub(r"^\s*" + re.escape(artist) + r"\s*[-\u2013\u2014]\s*", "", text, flags=re.IGNORECASE)
        match = _BRACKET_START.search(text)
        if match:
            text = text[: match.start()]
        new = _TRAIL_FORMAT.sub("", text)
    new = re.sub(r"\s+", " ", new.replace("/", "\u2044")).strip(" -\u2013\u2014_")
    if not new or new.casefold() == name.strip().casefold():
        return None
    return new


def _most_common(audios, key):
    counts = Counter()
    for audio in audios:
        values = audio.get(key) or []
        if values and values[0].strip():
            counts[values[0].strip()] += 1
    return counts.most_common(1)[0][0] if counts else ""


def find_repeated_ending(titles):
    """Returns (ending, count) -- the longest ending shared by at least SHARE of the titles,
    cut at a word break and never eating a whole title -- or None."""
    n = len(titles)
    if n < MIN_TRACKS:
        return None
    need = max(MIN_TRACKS, int(SHARE * n + 0.9999))
    word_lists = [t.split() for t in titles]
    for k in range(MAX_WORDS, 0, -1):
        counts = Counter()
        for words in word_lists:
            if len(words) > k:                       # something must be left of the title
                counts[" ".join(words[-k:])] += 1
        if counts:
            ending, count = counts.most_common(1)[0]
            if count >= need and _cuts_clean(titles, ending):
                return ending, count
    return None


def _cuts_clean(titles, ending):
    """False when taking the ending off would cut a title in the middle of a bracket, leaving
    'Song (2003' behind. That happens when most, but not all, titles end the same way."""
    for t in titles:
        if t.endswith(" " + ending):
            rest = t[: -len(ending)]
            if rest.count("(") > rest.count(")") or rest.count("[") > rest.count("]"):
                return False
    return True


_SPAM_WORDS = r"(?:remaster(?:ed)?|original recording|bonus track)"
_SPAM_ENDING = re.compile(
    r"(?:\s*[\(\[][^\)\]]*" + _SPAM_WORDS + r"[^\)\]]*[\)\]]"
    r"|\s+[-\u2013\u2014]\s+[^\-\u2013\u2014]*" + _SPAM_WORDS + r"[^\-\u2013\u2014]*)\s*$", re.IGNORECASE)


def _title_findings(folder, album, tracks, fmt="flac"):
    """The repeated-ending findings for one album's songs. Two rules: an ending shared by at least SHARE
    of the titles (any wording), and -- for the usual spam wording like 'Remaster' or 'Original
    Recording' -- an ending that sits on just two or more songs, which is shown as 'some songs'."""
    tag = " (MP3)" if fmt == "mp3" else ""
    titled = [(p, n, _title(a)) for p, n, a in tracks if _title(a)]
    out, covered = [], set()
    found = find_repeated_ending([t[2] for t in titled])
    if found:
        ending, _count = found
        matching = [t for t in titled if t[2].endswith(" " + ending)]
        covered = {t[0] for t in matching}
        only_in_tags = sum(
            1 for _p, name, _t in matching
            if _normal(ending) not in _normal(_strip_track_number(os.path.splitext(name)[0])))
        first = matching[0][2]
        out.append({
            "kind": "title", "folder": folder, "album": album, "where": "Song title ending" + tag,
            "ending": ending, "count": len(matching), "total": len(titled),
            "only_in_tags": only_in_tags, "before": first, "after": first[: -len(ending)].rstrip(),
            "files": [(p, t) for p, _n, t in matching], "format": fmt,
        })
    groups = {}
    for p, _n, t in titled:
        if p in covered:
            continue
        m = _SPAM_ENDING.search(t)
        if m and t[: m.start()].strip():
            groups.setdefault(m.group(0).strip(), []).append((p, t, m.start()))
    for ending, items in sorted(groups.items()):
        if len(items) < 2:
            continue
        first, cut = items[0][1], items[0][2]
        out.append({
            "kind": "title", "folder": folder, "album": album, "where": "Song title ending (" + ("MP3, " if fmt == "mp3" else "") + "some songs)",
            "ending": ending, "count": len(items), "total": len(titled), "only_in_tags": "-",
            "before": first, "after": first[:cut].rstrip(),
            "files": [(p, t) for p, t, _c in items], "format": fmt,
        })
    return out


def _find_other_tags(tracks):
    """tracks: list of (path, name, audio). Returns [(value, [keys], count)] for values that sit in
    the same tag of at least SHARE of the songs, grouped so one stamp in many tags is one finding."""
    n = len(tracks)
    if n < MIN_TRACKS:
        return []
    need = max(MIN_TRACKS, int(SHARE * n + 0.9999))
    per_key = {}
    for _p, _n, audio in tracks:
        for key in audio.keys():
            low = key.lower()
            if low in NORMAL_TAGS or low.startswith(NORMAL_PREFIXES):
                continue
            for value in audio.get(key) or []:
                if value.strip():
                    per_key.setdefault((low, value), 0)
                    per_key[(low, value)] += 1
    grouped = {}
    for (low, value), count in per_key.items():
        if count >= need:
            grouped.setdefault(value, []).append((low, count))
    out = []
    for value, keys in grouped.items():
        out.append((value, sorted(k for k, _c in keys), max(c for _k, c in keys)))
    return sorted(out)


_DISC_FOLDER = re.compile(r"^(disc|disk|cd)\s*[-_ .]?\s*\d+\b", re.IGNORECASE)


def album_key(folder):
    """A name for an album folder that does not depend on where it sits on the drive, so a choice made about it
    (Ignore, 'Looks fine') still applies after the folder is moved. It is the folder's own name; a plain
    'Disc 1' style folder also gets the name of the album folder it sits in."""
    folder = (folder or "").rstrip("/\\")
    name = os.path.basename(folder)
    if _DISC_FOLDER.match(name):
        parent = os.path.basename(os.path.dirname(folder))
        if parent:
            return f"{parent} :: {name}"
    return name


def finding_id(item):
    """A stable name for one finding, so 'leave this one alone' can be remembered between scans.
    It uses the album's own name, not its location, so it survives moving the folder."""
    kind = item["kind"]
    if kind == "title":
        ident = item["ending"] + (" (mp3)" if item.get("format") == "mp3" else "")
    elif kind == "note":
        ident = item["shape"] + (" (mp3)" if item.get("format") == "mp3" else "")
    elif kind == "field" and item.get("format") == "mp3":
        ident = f'{item["key"]}={item["value"]} (mp3)'
    elif kind == "folder":
        ident = item["before"]
    elif kind == "files":
        ident = "extra files"
    elif kind == "field":
        ident = f'{item["key"]}={item["value"]}'
    else:
        ident = f'{",".join(item["keys"])}={item["value"]}'
    return f'{kind}|{album_key(item["folder"])}|{ident}'


def _portable_id(saved):
    """Older saved choices were 'kind|/full/path/to/album|text'. Turn those into the location-free form;
    anything already in the new form is left alone."""
    parts = saved.split("|", 2) if isinstance(saved, str) else []
    if len(parts) == 3 and os.path.isabs(parts[1]):
        return f"{parts[0]}|{album_key(parts[1])}|{parts[2]}"
    return saved


def load_ignored(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return {_portable_id(entry) for entry in json.load(fh)}
    except (OSError, ValueError, TypeError):
        return set()


def save_ignored(path, ids):
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(sorted(ids), fh, ensure_ascii=False, indent=0)
    except OSError:
        pass


def _note_findings(folder, album, tracks, fmt="flac", skip_titles=()):
    """A note in brackets at the end of the file names (and titles) of most songs whose numbers change
    from song to song, like '(RS1 take 3)', '(RS1 take 4)'. The ordinary repeated-ending check can't see
    these because no two songs end exactly the same. File names count too, since Elisa and the disc
    lists show them. skip_titles: songs whose title ending another finding already covers."""
    n = len(tracks)
    if n < MIN_TRACKS:
        return []
    per_song = []                                   # (path, stem, ext, title, {shape: example})
    seen = Counter()
    example = {}
    for p, name, audio in tracks:
        stem, ext = os.path.splitext(name)
        title = _title(audio) if p not in skip_titles else ""
        shapes = {}
        for text in (stem, title):
            shape = _note_shape(text) if text else None
            if shape:
                shapes[shape] = _TRAIL_NOTE.search(text).group(0).strip()
        per_song.append((p, stem, ext, title))
        seen.update(shapes.keys())
        for shape, shown in shapes.items():
            example.setdefault(shape, shown)
    if not seen:
        return []
    shape, count = seen.most_common(1)[0]
    if count < max(MIN_TRACKS, int(SHARE * n + 0.9999)):
        return []
    # songs that would end up with the same file name (two takes of one song) are left alone
    new_names = Counter()
    for p, stem, ext, title in per_song:
        new_names[(os.path.dirname(p), (strip_note(stem, shape) + ext).casefold())] += 1
    entries, first = [], None
    for p, stem, ext, title in per_song:
        new_stem = strip_note(stem, shape)
        new_title = strip_note(title, shape) if title else title
        if new_stem == stem and new_title == title:
            continue
        if new_names[(os.path.dirname(p), (new_stem + ext).casefold())] > 1:
            continue
        entries.append((p, title if new_title != title else ""))
        if first is None:
            first = (stem, new_stem) if new_stem != stem else (title, new_title)
    if not entries:
        return []
    return [{
        "kind": "note", "folder": folder, "album": album,
        "where": "Take note on file names and titles" + (" (MP3)" if fmt == "mp3" else ""),
        "ending": example[shape], "shape": shape, "count": len(entries), "total": n,
        "only_in_tags": "-", "before": first[0], "after": first[1], "files": entries, "format": fmt,
    }]


def _field_findings(folder, album, tracks, fmt="flac"):
    """Extra baggage on the artist and album tags: text in brackets at the end, text after a dash, a web
    address. One finding per value per tag."""
    out = []
    for key, label in FIELD_KEYS:
        groups = {}
        for p, _n, audio in tracks:
            for value in audio.get(key) or []:
                groups.setdefault(value, []).append(p)
        have = sum(len(v) for v in groups.values())
        for value, paths in sorted(groups.items()):
            new = clean_field_value(value)
            if not new:
                continue
            if value.startswith(new):
                removed = value[len(new):].strip()
            else:                      # the cut came from the middle, e.g. "(CAT123)" before "CD1"
                sm = difflib.SequenceMatcher(None, value, new, autojunk=False)
                removed = " ".join(value[a:b].strip() for tag, a, b, _c, _d in sm.get_opcodes()
                                   if tag in ("delete", "replace")).strip() or value
            out.append({
                "kind": "field", "folder": folder, "album": album,
                "where": label + (" (MP3)" if fmt == "mp3" else ""),
                "ending": removed, "key": key, "value": value, "new": new,
                "count": len(paths), "total": have, "only_in_tags": "-",
                "before": value, "after": new, "files": [(p,) for p in paths], "format": fmt,
            })
    return out


def scan(root, progress=None, cancelled=None, stats=None, other_tags=False, ignored=None, include_mp3=False):
    """Walks the folder; returns a list of findings, one per repeated thing per album. If a dict is
    passed as stats, it gets "albums" (how many were checked) and "flagged" (how many had findings).
    By default only song titles are checked. other_tags=True also looks for values stamped into the
    other tags (COMMENT, LABEL...), which turned up mostly harmless album info, so it is off."""
    findings = []
    folders = []
    for folder, _dirs, names in os.walk(root):
        flacs = sorted(n for n in names if n.lower().endswith(".flac"))
        mp3s = sorted(n for n in names if n.lower().endswith(".mp3")) if include_mp3 else []
        if flacs or mp3s:
            folders.append((folder, flacs, mp3s))
    folders.sort()
    parent = os.path.dirname(root.rstrip(os.sep)) or root
    for i, (folder, flacs, mp3s) in enumerate(folders):
        if cancelled and cancelled():
            break
        if progress:
            progress(i + 1, len(folders), folder)
        tracks = []
        for name in flacs:
            path = os.path.join(folder, name)
            try:
                tracks.append((path, name, FLAC(path)))
            except Exception:
                continue
        album = os.path.relpath(folder, parent)

        # 1) repeated ending on the song titles
        title_found = _title_findings(folder, album, tracks)
        findings.extend(title_found)
        covered = {entry[0] for item in title_found for entry in item["files"]}
        # 1b) a note in brackets with changing numbers on the file names and titles ("(RS1 take 3)")
        findings.extend(_note_findings(folder, album, tracks, skip_titles=covered))

        # 2) extra baggage on the artist and album tags
        findings.extend(_field_findings(folder, album, tracks))

        # 3) the folder's own name
        if os.path.normpath(folder) != os.path.normpath(root) and tracks:
            tag_album = _most_common([a for _p, _n, a in tracks], "album")
            tag_album = clean_field_value(tag_album) or tag_album
            tag_artist = _most_common([a for _p, _n, a in tracks], "artist")
            new_name = propose_folder_name(os.path.basename(folder), tag_album, tag_artist)
            if new_name:
                findings.append({
                    "kind": "folder", "folder": folder, "album": album, "where": "Folder name",
                    "ending": "-", "count": len(flacs), "total": len(flacs), "only_in_tags": "-",
                    "before": os.path.basename(folder), "after": new_name, "new": new_name,
                    "files": [(folder,)],
                })

        # 4) the same value stamped into the other tags
        for value, keys, count in (_find_other_tags(tracks) if other_tags else []):
            files = []
            for p, _n, audio in tracks:
                present = [k for k in keys if value in (audio.get(k) or [])]
                if present:
                    files.append((p, present))
            shown = ", ".join(k.upper() for k in keys)
            findings.append({
                "kind": "tags", "folder": folder, "album": album, "where": "Other tags: " + shown,
                "ending": value, "value": value, "keys": keys, "count": len(files), "total": len(tracks),
                "only_in_tags": "-", "before": f"{keys[0].upper()} = {value}",
                "after": "(tag removed)", "files": files,
            })
        # 5) MP3 songs in the folder: the repeated endings on the song titles, and the artist and album tags
        if mp3s:
            mtracks = []
            for name in mp3s:
                path = os.path.join(folder, name)
                try:
                    mtracks.append((path, name, _open_audio(path)))
                except Exception:
                    continue
            mtitle_found = _title_findings(folder, album, mtracks, "mp3")
            findings.extend(mtitle_found)
            findings.extend(_note_findings(folder, album, mtracks, "mp3",
                                           {e[0] for it in mtitle_found for e in it["files"]}))
            findings.extend(_field_findings(folder, album, mtracks, "mp3"))
    findings.extend(_find_extra_files(root, parent, cancelled))
    _number_merging_discs(findings)
    _flag_folder_clashes(findings)
    ignored = ignored or set()
    for item in findings:           # things the person chose to leave alone stay in the list, marked
        item["id"] = finding_id(item)
        item["ignored"] = item["id"] in ignored
    if stats is not None:
        stats["albums"] = len(folders)
        stats["flagged"] = len({item["folder"] for item in findings if not item["ignored"]})
        stats["ignored"] = sum(1 for item in findings if item["ignored"])
    return findings


def _find_extra_files(root, parent, cancelled=None):
    """The .txt and .m3u files that come along with a download (rip logs, DR reports, playlists), one
    finding per folder. The folder you pick is only looked at when it holds songs itself, so a notes
    file next to your albums is left alone."""
    out = []
    root_norm = os.path.normpath(root)
    for folder, dirs, names in os.walk(root):
        if cancelled and cancelled():
            break
        dirs[:] = sorted(d for d in dirs if not any(w in d.lower() for w in SKIP_FOLDER_WORDS))
        has_songs = any(n.lower().endswith(".flac") for n in names)
        if os.path.normpath(folder) == root_norm and not has_songs:
            continue
        extras = sorted(n for n in names if os.path.splitext(n)[1].lower() in EXTRA_FILE_EXTS)
        if not extras:
            continue
        out.append({
            "kind": "files", "folder": folder, "album": os.path.relpath(folder, parent),
            "where": "Extra files", "ending": "-", "count": len(extras), "total": len(extras),
            "only_in_tags": "-", "before": ", ".join(extras), "after": "(moved aside)",
            "files": [(os.path.join(folder, n),) for n in extras],
        })
    return out


def _natural(text):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def _number_merging_discs(findings):
    """Disc folders side by side (CD1, CD2, CD3...) often differ only by a catalog number stuck on
    the album name. Cleaning it off would give every disc the same album name, and players would
    merge them into one jumbled album. When that is about to happen, the cleaned name gets
    "Disc 1", "Disc 2"... added instead, in folder order."""
    groups = {}
    for item in findings:
        if item["kind"] == "field" and item["key"] == "album":
            groups.setdefault((os.path.dirname(item["folder"]), item["new"]), []).append(item)
    for (_parent, base), items in groups.items():
        folders = sorted({item["folder"] for item in items}, key=_natural)
        if len(folders) < 2:
            continue
        for item in items:
            number = folders.index(item["folder"]) + 1
            item["new"] = f"{base} Disc {number}"
            item["after"] = item["new"]
            item["where"] = "Disc numbering"
            item["type"] = "disc"      # not spam: a tag fix so discs don't merge in a player


_ENDS_WITH_DISC = re.compile(r"(?i)\b(cd|disc|disk)\s*\d+\s*$")


def _flag_folder_clashes(findings):
    """A new folder name that's already taken (or wanted by two folders) can't be used as it is. Where
    the clash can be sorted out by numbering, the rename is changed to 'Album Disc 1', 'Album Disc 2'...
    (shown in the list like any other rename, so it can be looked over and unticked). Only when no free
    numbered name can be found is the row marked 'Folder name (taken)' and skipped."""
    folders = [item for item in findings if item["kind"] == "folder"]
    wanted = Counter()
    for item in folders:
        wanted[(os.path.dirname(item["folder"]), item["new"].casefold())] += 1
    groups = {}                                   # (parent, wanted name) -> the folders that want it
    for item in folders:
        parent = os.path.dirname(item["folder"])
        taken = os.path.exists(os.path.join(parent, item["new"]))
        if taken or wanted[(parent, item["new"].casefold())] > 1:
            groups.setdefault((parent, item["new"]), []).append(item)
    reserved = {(os.path.dirname(i["folder"]), i["new"].casefold()) for i in folders}
    for (parent, base), items in groups.items():
        items.sort(key=lambda i: _natural(os.path.basename(i["folder"])))
        sharing = len(items) > 1
        number = 1 if sharing else 2              # a lone folder meets an existing one: it becomes Disc 2
        for item in items:
            candidate = None
            if not _ENDS_WITH_DISC.search(base):
                while number < 100:
                    name = f"{base} Disc {number}"
                    number += 1
                    if (parent, name.casefold()) not in reserved and not os.path.exists(os.path.join(parent, name)):
                        candidate = name
                        break
            if candidate:
                reserved.add((parent, candidate.casefold()))
                item["new"] = item["after"] = candidate
                item["where"] = "Folder name (Disc number added)"
            else:
                item["clash"] = True
                item["where"] = "Folder name (taken)"


def _log_line(log, batch, path, key, old, new):
    log.write(json.dumps({"batch": batch, "path": path, "key": key, "old": old, "new": new},
                         ensure_ascii=False) + "\n")
    log.flush()


def apply(findings, log_path):
    """Makes the ticked cleanups. Every old value is logged first, one line each, so Undo can put it
    back. Extra files are moved to a holding folder (never deleted) so Undo can bring them back. Folder
    renames come last, deepest first. Returns (changed, failed, folders, extras) where changed counts
    songs touched, folders counts folders renamed and extras counts extra files moved aside."""
    batch = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    changed_files, failed, extras_done = set(), 0, 0
    note_renames, cue_cache = [], {}
    hold = os.path.join(os.path.dirname(os.path.abspath(log_path)), "tag_cleaner_removed",
                        batch.replace(":", "-").replace(" ", "_"))
    os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log:
        for item in findings:
            if item["kind"] == "folder":
                continue
            if item["kind"] == "files":
                for entry in item["files"]:
                    path = entry[0]
                    try:
                        if not os.path.isfile(path):
                            continue
                        os.makedirs(hold, exist_ok=True)
                        held = os.path.join(hold, f"{extras_done + 1:05d}_{os.path.basename(path)}")
                        _log_line(log, batch, held, "__file__", [path], [held])
                        shutil.move(path, held)
                        extras_done += 1
                    except OSError:
                        failed += 1
                continue
            if item["kind"] == "note":
                for entry in item["files"]:
                    path, old_title = entry[0], entry[1]
                    try:
                        if old_title:                      # the title tag has the note too
                            audio = _open_audio(path)
                            new_title = strip_note(old_title, item["shape"])
                            if new_title != old_title and _title(audio) == old_title:
                                _log_line(log, batch, path, "title", [old_title], [new_title])
                                audio["title"] = [new_title]
                                _save_audio(audio)
                                changed_files.add(path)
                        note_renames.append((path, item["shape"]))
                    except Exception:
                        failed += 1
                continue
            for entry in item["files"]:
                path = entry[0]
                try:
                    audio = _open_audio(path)
                    if item["kind"] == "title":
                        old_title = entry[1]
                        ending = item["ending"]
                        new_title = old_title[: -len(ending)].rstrip() if old_title.endswith(ending) else None
                        if not new_title or _title(audio) != old_title:   # changed since the scan
                            continue
                        _log_line(log, batch, path, "title", [old_title], [new_title])
                        audio["title"] = [new_title]
                    elif item["kind"] == "field":
                        key, value = item["key"], item["value"]
                        old = list(audio.get(key) or [])
                        if value not in old:           # changed since the scan
                            continue
                        new = [item["new"] if v == value else v for v in old]
                        _log_line(log, batch, path, key, old, new)
                        audio[key] = new
                    else:
                        value = item["value"]
                        touched = False
                        for key in entry[1]:
                            old = list(audio.get(key) or [])
                            if value not in old:
                                continue
                            kept = [v for v in old if v != value]
                            _log_line(log, batch, path, key, old, kept)
                            if kept:
                                audio[key] = kept
                            else:
                                del audio[key]
                            touched = True
                        if not touched:
                            continue
                    _save_audio(audio)
                    changed_files.add(path)
                except Exception:
                    failed += 1
        for path, shape in note_renames:               # song file names, before any folder is renamed
            stem, ext = os.path.splitext(os.path.basename(path))
            new = os.path.join(os.path.dirname(path), strip_note(stem, shape) + ext)
            try:
                if new == path or not os.path.isfile(path):
                    continue
                if os.path.exists(new) or _cue_mentions(os.path.dirname(path), os.path.basename(path), cue_cache):
                    failed += 1
                    continue
                os.rename(path, new)
                _log_line(log, batch, new, "__rename__", [path], [new])
                changed_files.add(path)
            except OSError:
                failed += 1
        folders_done = 0
        renames = [i for i in findings if i["kind"] == "folder"]
        for item in sorted(renames, key=lambda i: i["folder"].count(os.sep), reverse=True):
            old = item["folder"]
            new = os.path.join(os.path.dirname(old), item["new"])
            if item.get("clash") or not os.path.isdir(old) or os.path.exists(new):
                failed += 1
                continue
            try:
                os.rename(old, new)
                _log_line(log, batch, new, "__folder__", [old], [new])
                folders_done += 1
            except OSError:
                failed += 1
    return len(changed_files), failed, folders_done, extras_done


def undo_last(log_path):
    """Puts back the old values from the most recent batch in the log. Returns (restored, failed)
    -- counted in songs -- plus how many folder names were put back, and removes that batch from the log.
    Returns (restored, failed, folders, extras), extras being the extra files brought back."""
    if not os.path.isfile(log_path):
        return 0, 0, 0, 0
    with open(log_path, encoding="utf-8") as fh:
        rows = []
        for ln in fh:
            if ln.strip():
                try:
                    rows.append(json.loads(ln))
                except ValueError:
                    pass
    if not rows:
        return 0, 0, 0, 0
    last = rows[-1]["batch"]
    batch = [r for r in rows if r["batch"] == last]
    rest = [r for r in rows if r["batch"] != last]
    by_file = {}
    folders = restored = failed = extras = renamed_back = 0
    # folders first (shallowest first, the reverse of how they were renamed), so the song paths are valid again
    for r in reversed([r for r in batch if r["key"] == "__folder__"]):
        try:
            os.rename(r["path"], r["old"][0])
            folders += 1
        except OSError:
            failed += 1
    # song files renamed by hand: put the old names back first, so the tag rows below find their files
    for r in reversed([r for r in batch if r["key"] == "__rename__"]):
        try:
            if os.path.isfile(r["path"]) and not os.path.exists(r["old"][0]):
                os.rename(r["path"], r["old"][0])
                renamed_back += 1
                if r["old"][0] in {x["path"] for x in batch if x["key"] not in ("__folder__", "__rename__", "__file__")}:
                    renamed_back -= 1            # this song's tags are counted below; don't count it twice
            else:
                failed += 1
        except OSError:
            failed += 1
    for r in batch:
        if r["key"] == "__file__":
            back, held = r["old"][0], r["path"]
            try:
                if os.path.isfile(held) and not os.path.exists(back) and os.path.isdir(os.path.dirname(back)):
                    shutil.move(held, back)
                    extras += 1
                else:
                    failed += 1
            except OSError:
                failed += 1
        elif r["key"] not in ("__folder__", "__rename__"):
            by_file.setdefault(r["path"], []).append(r)
    for path, items in by_file.items():
        try:
            audio = _open_audio(path)
            for r in items:
                if r["old"]:
                    audio[r["key"]] = r["old"]
                elif r["key"] in audio:
                    del audio[r["key"]]
            _save_audio(audio)
            restored += 1
        except Exception:
            failed += 1
    with open(log_path, "w", encoding="utf-8") as fh:
        for r in rest:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    restored += renamed_back
    return restored, failed, folders, extras


# ---------------------------------------------------------------- editing one album by hand
EDIT_KEYS = ("tracknumber", "title", "artist", "album", "albumartist", "date", "genre")


def read_album_tags(folder):
    """The FLAC and MP3 songs directly inside one album folder, with the tags the by-hand editor shows:
    [{"path", "name", "tags": {key: first value or ""}}]. A song with no tags yet shows blanks."""
    songs = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return songs
    for name in names:
        path = os.path.join(folder, name)
        if not name.lower().endswith((".flac", ".mp3")) or not os.path.isfile(path):
            continue
        tags = {key: "" for key in EDIT_KEYS}
        try:
            audio = _open_audio(path)
            for key in EDIT_KEYS:
                values = audio.get(key) or []
                tags[key] = str(values[0]) if values else ""
        except ValueError:
            pass                                    # an MP3 with no tags at all: shown blank
        except Exception:
            continue
        songs.append({"path": path, "name": name, "tags": tags})
    return songs


def apply_edits(edits, log_path, progress=None, batch=None):
    """edits: [{"path": song, "changes": {tag key: new text}}]. Each song is rewritten through a temporary
    copy that replaces the original only after it opens cleanly, and every old value is logged first so the
    cleaner's Undo can put it back. Returns (songs changed, songs that failed)."""
    batch = batch or new_batch()
    changed = failed = 0
    os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log:
        for index, edit in enumerate(edits):
            if progress:
                progress(index + 1, len(edits), edit["path"])
            path = edit["path"]
            tmp = path + ".dw_tmp"
            try:
                is_mp3 = path.lower().endswith(".mp3")
                shutil.copy2(path, tmp)
                audio = _open_audio(tmp, create=True, mp3=is_mp3)
                pending = []
                for key, new in edit["changes"].items():
                    new = new.strip()
                    old = list(audio.get(key) or [])
                    if not new or old == [new]:
                        continue
                    audio[key] = [new]
                    pending.append((key, old, [new]))
                if not pending:
                    continue
                for key, old, new in pending:
                    _log_line(log, batch, path, key, old, new)
                _save_audio(audio)
                _open_audio(tmp, mp3=is_mp3)            # must still open cleanly
                os.replace(tmp, path)
                changed += 1
            except Exception:
                failed += 1
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
    return changed, failed


# ---------------------------------------------------------------- file names (edited by hand)
def new_batch():
    """One name for everything a single Save does, so Undo takes it all back together."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


_TRAIL_NOTE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")


def _note_shape(text):
    """The note in brackets at the very end of a name, with its numbers turned into '#', so that
    '(RS1 take 3)' and '(RS1 take 5)' come out the same. None if there is no such note."""
    match = _TRAIL_NOTE.search(text)
    if not match or not text[: match.start()].strip():
        return None
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", match.group(0).strip().lower()))


def strip_note(text, shape):
    """The text without its closing bracket note, when that note has the given shape; otherwise unchanged."""
    match = _TRAIL_NOTE.search(text)
    if match and text[: match.start()].strip() and _note_shape(text) == shape:
        return text[: match.start()].rstrip()
    return text


def find_repeating_note(songs):
    """songs: the list read_album_tags gives. Looks at the end of every file name and every title for a
    bracket note that has the same shape on at least SHARE of the songs, even when its numbers change
    from song to song ('(RS1 take 3)', '(RS1 take 5)'...). Returns (shape, example, count, total) or None."""
    total = len(songs)
    if total < MIN_TRACKS:
        return None
    seen = Counter()
    example = {}
    for song in songs:
        stem = os.path.splitext(song["name"])[0]
        shapes = set()
        for text in (stem, song["tags"].get("title", "")):
            shape = _note_shape(text)
            if shape:
                shapes.add(shape)
                example.setdefault(shape, _TRAIL_NOTE.search(text).group(0).strip())
        seen.update(shapes)
    if not seen:
        return None
    shape, count = seen.most_common(1)[0]
    need = max(MIN_TRACKS, int(SHARE * total + 0.9999))
    if count < need:
        return None
    return shape, example[shape], count, total


def _cue_mentions(folder, filename, cache):
    """True when a .cue sheet in the folder mentions the file name (renaming it would break the sheet)."""
    if folder not in cache:
        parts = []
        try:
            for other in os.listdir(folder):
                if other.lower().endswith(".cue"):
                    try:
                        with open(os.path.join(folder, other), encoding="utf-8", errors="replace") as fh:
                            parts.append(fh.read().lower())
                    except OSError:
                        pass
        except OSError:
            pass
        cache[folder] = "\n".join(parts)
    return filename.lower() in cache[folder]


def apply_renames(renames, log_path, batch=None):
    """renames: [{"path": song file, "new_name": new file name}]. Only the name changes, the music and
    the folder stay as they are. A rename is skipped (and counted as failed) when the new name is
    already used by another file, when two songs want the same name, or when a .cue sheet in the
    folder mentions the old name (renaming would break the sheet). Every rename is logged so Undo can
    put the old name back. Returns (renamed, failed)."""
    batch = batch or new_batch()
    renamed = failed = 0
    cue_text = {}                               # folder -> all its .cue sheets, lowercase (see _cue_mentions)
    wanted = set()                              # destinations already given out in this run
    os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log:
        for item in renames:
            old = item["path"]
            name = item["new_name"].strip()
            folder = os.path.dirname(old)
            new = os.path.join(folder, name)
            try:
                if (not name or "/" in name or "\x00" in name or name in (".", "..")
                        or not os.path.isfile(old) or new == old):
                    failed += 1
                    continue
                if os.path.exists(new) or new.casefold() in wanted:
                    failed += 1
                    continue
                if _cue_mentions(folder, os.path.basename(old), cue_text):
                    failed += 1
                    continue
                os.rename(old, new)
                wanted.add(new.casefold())
                _log_line(log, batch, new, "__rename__", [old], [new])
                renamed += 1
            except OSError:
                failed += 1
    return renamed, failed
