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
Cover Art Embedder logic for DiscWise.

Finds the cover picture that sits next to (or near) an album's FLAC or MP3 files and embeds it inside the
songs, so players like Elisa show it and the FLAC Verifier's "Cover art" check goes green.

Where it looks for a picture, in this order:
  1. In the album folder itself: cover, folder, front, album... (.jpg, .jpeg, .png)
  2. In an art folder inside the album folder (Artwork, Covers, Scans, Images...)
  3. In the folder above, but only when the album folder is a disc of a set (CD 1, Disc 2...)
  4. The one and only picture in the album folder, whatever it is called

Normally only songs that have no embedded picture get one; songs that already have art are never touched.
In "replace" mode (for one or two albums at a time) the pictures already inside the songs are swapped for a
new one the person picked; the old pictures are saved first, so Undo can put them back.
Every change is logged, one line per song, so Undo can reverse it.
Each song is rewritten through a temporary copy that replaces the original only once it has been
checked, so stopping halfway (or running out of disk space) can't leave a half-written file.
"""

import hashlib
import io
import json
import os
import re
import shutil
from datetime import datetime

try:
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import ID3, APIC, ID3NoHeaderError
    from mutagen.mp3 import MP3
except ImportError:
    FLAC = Picture = ID3 = APIC = ID3NoHeaderError = MP3 = None

try:
    from PIL import Image
except ImportError:
    Image = None

SONG_EXTS = (".flac", ".mp3")
IMAGE_EXTS = (".jpg", ".jpeg", ".png")
COVER_NAMES = ("cover", "folder", "front", "album", "albumart")     # best first
ART_FOLDER_WORDS = ("art", "cover", "scan", "image", "artwork")
NOT_FRONT_WORDS = ("back", "small", "thumb", "inlay", "tray", "booklet", "disc", "cd", "inside", "rear",
                   "spine", "label")
SKIP_FOLDER_WORDS = ("spectrogram", "spectrograph")
DISC_FOLDER = re.compile(r"(?i)(^|[\s._(\[-])(cd|disc|disk)\s*\d+\s*[)\]]?(\s*-?\s*bonus)?$")
MAX_SIDE = 1500            # bigger pictures are shrunk to this before being embedded
MAX_BYTES = 3 * 1024 * 1024


def ready():
    return FLAC is not None and ID3 is not None


def _is_mp3(path):
    return path.lower().endswith(".mp3")


def _pictures_of(path):
    """The embedded pictures of a FLAC or MP3 as (sha1 of the picture data) strings. A song with no
    tags at all simply has none."""
    if _is_mp3(path):
        try:
            return [hashlib.sha1(f.data).hexdigest() for f in ID3(path).getall("APIC")]
        except ID3NoHeaderError:
            return []
    return [hashlib.sha1(p.data).hexdigest() for p in FLAC(path).pictures]


def _has_art(path):
    return bool(_pictures_of(path))


def _images_in(folder):
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    return [n for n in names
            if os.path.splitext(n)[1].lower() in IMAGE_EXTS and os.path.isfile(os.path.join(folder, n))]


def _name_score(name):
    """Lower is better; None means the name says it is not a front cover."""
    stem = os.path.splitext(name)[0].lower().strip()
    if stem in COVER_NAMES:
        return COVER_NAMES.index(stem)
    words = re.findall(r"[a-z]+", stem)
    if any(w in NOT_FRONT_WORDS for w in words):
        return None
    if "front" in words or "cover" in words or "folder" in words:
        return len(COVER_NAMES)
    return None


def _best_named(folder, names):
    scored = [(s, n) for n in names for s in [_name_score(n)] if s is not None]
    return os.path.join(folder, min(scored)[1]) if scored else None


def is_disc_folder(name):
    return bool(DISC_FOLDER.search(name.strip()))


def find_cover(folder):
    """Returns (picture path or None, where it was found)."""
    here = _images_in(folder)
    found = _best_named(folder, here)
    if found:
        return found, "In the album folder"
    try:
        subs = sorted(d for d in os.listdir(folder) if os.path.isdir(os.path.join(folder, d)))
    except OSError:
        subs = []
    for sub in subs:
        if any(w in sub.lower() for w in SKIP_FOLDER_WORDS):
            continue
        if any(w in sub.lower() for w in ART_FOLDER_WORDS):
            path = os.path.join(folder, sub)
            pics = _images_in(path)
            found = _best_named(path, pics) or (os.path.join(path, pics[0]) if len(pics) == 1 else None)
            if found:
                return found, f'In the "{sub}" folder'
    if is_disc_folder(os.path.basename(folder.rstrip(os.sep))):
        parent = os.path.dirname(folder.rstrip(os.sep))
        found = _best_named(parent, _images_in(parent))
        if found:
            return found, "In the folder above (a disc of a set)"
    if len(here) == 1:
        return os.path.join(folder, here[0]), "The only picture in the folder"
    return None, "No picture found"


def _album_folders(root):
    for folder, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not any(w in d.lower() for w in SKIP_FOLDER_WORDS))
        songs = sorted(n for n in names if n.lower().endswith(SONG_EXTS))
        if songs:
            yield folder, songs


def scan(root, progress=None, cancelled=None, replace=False):
    """One item for every album folder whose songs are missing a picture. Items with no picture to
    use have cover=None and cannot be ticked. Changes nothing.

    With replace=True every album folder is listed whether or not its songs have art, and no picture is
    chosen on its own: each item waits (cover=None) until the person picks a replacement."""
    parent = os.path.dirname(root.rstrip(os.sep)) or root
    albums = list(_album_folders(root))
    items = []
    for index, (folder, names) in enumerate(albums):
        if cancelled and cancelled():
            break
        if progress:
            progress(index + 1, len(albums), folder)
        missing, current = [], None
        for name in names:
            path = os.path.join(folder, name)
            try:
                if replace:
                    missing.append(path)
                    if current is None:
                        current = _current_size(path)
                elif not _has_art(path):
                    missing.append(path)
            except Exception:
                continue
        if not missing:
            continue
        if replace:
            cover, where = None, "Pick a replacement: double-click this row"
        else:
            cover, where = find_cover(folder)
        item = {"id": folder, "folder": folder, "album": os.path.relpath(folder, parent),
                "cover": cover, "where": where, "missing": missing, "total": len(names),
                "size": None, "note": "", "replace": replace, "current": current}
        if cover:
            try:
                prepared = prepare_picture(cover)
                item["size"] = (prepared["width"], prepared["height"])
                item["note"] = "will be shrunk" if prepared["resized"] else ""
            except Exception as exc:
                item["cover"], item["where"] = None, f"Can't read the picture ({exc})"
        items.append(item)
    return items


def _current_size(path):
    """(width, height) of the first picture embedded in the song, or None."""
    try:
        pics = _old_pictures(path)
        if not pics:
            return None
        if Image is not None:
            img = Image.open(io.BytesIO(pics[0]["data"]))
            return img.size
        if pics[0]["width"]:
            return pics[0]["width"], pics[0]["height"]
    except Exception:
        pass
    return None


def prepare_picture(path):
    """Reads the picture and returns what mutagen needs. Very large pictures are shrunk first (the
    file in the folder is never touched)."""
    with open(path, "rb") as fh:
        data = fh.read()
    ext = os.path.splitext(path)[1].lower()
    mime = "image/png" if ext == ".png" else "image/jpeg"
    width = height = depth = 0
    resized = False
    if Image is not None:
        img = Image.open(io.BytesIO(data))
        img.load()
        width, height = img.size
        depth = {"1": 1, "L": 8, "P": 8, "RGB": 24, "RGBA": 32, "CMYK": 32}.get(img.mode, 24)
        if max(width, height) > MAX_SIDE or len(data) > MAX_BYTES:
            img = img.convert("RGB")
            img.thumbnail((MAX_SIDE, MAX_SIDE))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=92)
            data, mime, resized = buf.getvalue(), "image/jpeg", True
            width, height, depth = img.size[0], img.size[1], 24
    elif not data:
        raise ValueError("the file is empty")
    return {"data": data, "mime": mime, "width": width, "height": height, "depth": depth,
            "resized": resized, "sha1": hashlib.sha1(data).hexdigest()}


def _make_picture(prepared):
    pic = Picture()
    pic.type = 3                       # front cover
    pic.mime = prepared["mime"]
    pic.desc = "Cover"
    pic.width, pic.height, pic.depth = prepared["width"], prepared["height"], prepared["depth"]
    pic.data = prepared["data"]
    return pic


def _rewrite(path, change):
    """Applies change(tags) to a temporary copy, checks it, then swaps it in for the original. The tags
    object is a FLAC, or for an MP3 its ID3 tags."""
    tmp = path + ".dw_tmp"
    try:
        shutil.copy2(path, tmp)
        if _is_mp3(path):
            try:
                tags = ID3(tmp)
            except ID3NoHeaderError:
                tags = ID3()
            change(tags)
            tags.save(tmp, v2_version=3 if tuple(tags.version[:2]) == (2, 3) else 4)
            MP3(tmp)                    # must still open cleanly
        else:
            audio = FLAC(tmp)
            change(audio)
            audio.save()
            FLAC(tmp)                   # must still open cleanly
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _add_picture(prepared):
    def change(tags):
        if isinstance(tags, ID3):
            tags.add(APIC(encoding=3, mime=prepared["mime"], type=3, desc="Cover", data=prepared["data"]))
        else:
            tags.add_picture(_make_picture(prepared))
    return change


def _old_pictures(path):
    """Everything embedded in the song right now: a list of dicts with the picture bytes and its details."""
    out = []
    if _is_mp3(path):
        try:
            frames = ID3(path).getall("APIC")
        except ID3NoHeaderError:
            frames = []
        for f in frames:
            out.append({"data": bytes(f.data), "mime": f.mime, "type": int(f.type), "desc": f.desc or "",
                        "width": 0, "height": 0, "depth": 0})
    else:
        for p in FLAC(path).pictures:
            out.append({"data": bytes(p.data), "mime": p.mime, "type": int(p.type), "desc": p.desc or "",
                        "width": p.width, "height": p.height, "depth": p.depth})
    for o in out:
        o["sha1"] = hashlib.sha1(o["data"]).hexdigest()
    return out


def backup_dir(log_path):
    return os.path.join(os.path.dirname(os.path.abspath(log_path)), "art_backups")


def _keep_backup(log_path, pictures):
    """Saves each old picture once (named after its checksum) so Undo can put it back."""
    folder = backup_dir(log_path)
    os.makedirs(folder, exist_ok=True)
    for pic in pictures:
        target = os.path.join(folder, pic["sha1"] + ".img")
        if not os.path.exists(target):
            with open(target + ".tmp", "wb") as fh:
                fh.write(pic["data"])
            os.replace(target + ".tmp", target)


def _replace_picture(prepared):
    def change(tags):
        if isinstance(tags, ID3):
            tags.delall("APIC")
        else:
            tags.clear_pictures()
        _add_picture(prepared)(tags)
    return change


def _restore_pictures(new_sha1, old, folder):
    """Takes the replacement out and puts the saved pictures back. Raises if a saved picture is missing."""
    restored = []
    for meta in old:
        with open(os.path.join(folder, meta["sha1"] + ".img"), "rb") as fh:
            restored.append((meta, fh.read()))

    def change(tags):
        if isinstance(tags, ID3):
            keep = [f for f in tags.getall("APIC") if hashlib.sha1(f.data).hexdigest() != new_sha1]
            tags.delall("APIC")
            for f in keep:
                tags.add(f)
            for meta, data in restored:
                tags.add(APIC(encoding=3, mime=meta["mime"], type=meta["type"], desc=meta["desc"], data=data))
        else:
            keep = [p for p in tags.pictures if hashlib.sha1(p.data).hexdigest() != new_sha1]
            tags.clear_pictures()
            for p in keep:
                tags.add_picture(p)
            for meta, data in restored:
                pic = Picture()
                pic.type, pic.mime, pic.desc = meta["type"], meta["mime"], meta["desc"]
                pic.width, pic.height, pic.depth, pic.data = meta["width"], meta["height"], meta["depth"], data
                tags.add_picture(pic)
    return change


def _log_replace_line(log, batch, path, sha1, old):
    meta = [{k: o[k] for k in ("sha1", "mime", "type", "desc", "width", "height", "depth")} for o in old]
    log.write(json.dumps({"batch": batch, "path": path, "key": "__art_replace__", "sha1": sha1, "old": meta},
                         ensure_ascii=False) + "\n")
    log.flush()


def _log_line(log, batch, path, sha1):
    log.write(json.dumps({"batch": batch, "path": path, "key": "__art__", "sha1": sha1},
                         ensure_ascii=False) + "\n")
    log.flush()


def apply(items, log_path, progress=None, cancelled=None, replace=False):
    """Embeds the picture of every item into its songs that have none (with replace=True: into all its
    songs, swapping whatever picture was there, after saving the old one). Returns
    (songs changed, songs that failed, albums done)."""
    batch = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    changed = failed = albums = 0
    os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log:
        for index, item in enumerate(items):
            if cancelled and cancelled():
                break
            if progress:
                progress(index + 1, len(items), item["folder"])
            if not item.get("cover"):
                continue
            try:
                prepared = prepare_picture(item["cover"])
            except Exception:
                failed += len(item["missing"])
                continue
            did = 0
            for path in item["missing"]:
                if cancelled and cancelled():
                    break
                try:
                    if replace:
                        old = _old_pictures(path)
                        if [o["sha1"] for o in old] == [prepared["sha1"]]:
                            continue               # already has exactly this picture
                        _keep_backup(log_path, old)
                        _log_replace_line(log, batch, path, prepared["sha1"], old)
                        _rewrite(path, _replace_picture(prepared))
                    else:
                        if _has_art(path):         # got art since the scan: leave it alone
                            continue
                        _log_line(log, batch, path, prepared["sha1"])
                        _rewrite(path, _add_picture(prepared))
                    changed += 1
                    did += 1
                except Exception:
                    failed += 1
            if did:
                albums += 1
    return changed, failed, albums


def undo_last(log_path):
    """Takes out the pictures the most recent embed put in, and nothing else. Returns
    (songs restored, songs that failed)."""
    if not os.path.isfile(log_path):
        return 0, 0
    rows = []
    with open(log_path, encoding="utf-8") as fh:
        for ln in fh:
            if ln.strip():
                try:
                    rows.append(json.loads(ln))
                except ValueError:
                    pass
    if not rows:
        return 0, 0
    last = rows[-1]["batch"]
    batch = [r for r in rows if r["batch"] == last]
    rest = [r for r in rows if r["batch"] != last]
    restored = failed = 0
    for r in batch:
        path, sha1 = r["path"], r["sha1"]
        try:
            if not os.path.isfile(path):
                failed += 1
                continue
            if sha1 not in _pictures_of(path):
                continue                              # nothing of ours in there (never written)
            if r.get("key") == "__art_replace__":
                _rewrite(path, _restore_pictures(sha1, r.get("old", []), backup_dir(log_path)))
                restored += 1
                continue

            def change(tags, sha1=sha1):
                if isinstance(tags, ID3):
                    keep = [f for f in tags.getall("APIC") if hashlib.sha1(f.data).hexdigest() != sha1]
                    tags.delall("APIC")
                    for f in keep:
                        tags.add(f)
                else:
                    keep = [p for p in tags.pictures if hashlib.sha1(p.data).hexdigest() != sha1]
                    tags.clear_pictures()
                    for p in keep:
                        tags.add_picture(p)

            _rewrite(path, change)
            restored += 1
        except Exception:
            failed += 1
    with open(log_path, "w", encoding="utf-8") as fh:
        for r in rest:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return restored, failed
