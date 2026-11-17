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
Cover Art Finder logic for DiscWise.

For an album whose songs have no picture and whose folder has none either, this looks on the internet
for covers that belong to it and lets you pick one. The pick is saved as cover.jpg (or cover.png) in the
album folder, and from there the Cover Art Embedder takes over exactly as it does for pictures you put
there yourself.

Where it looks (all free, no account or key needed):
  - iTunes / Apple Music
  - Deezer
  - MusicBrainz, with the pictures from the Cover Art Archive

Nothing is ever overwritten: if a cover.jpg already exists the new one is saved as cover_2.jpg, and so on.
Every picture saved is logged, one line each, in art_finder_log.jsonl.
"""

import difflib
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

try:
    from mutagen.flac import FLAC
    from mutagen import File as MutagenFile
except ImportError:
    FLAC = MutagenFile = None

try:
    from PIL import Image
except ImportError:
    Image = None

import art_embedder

USER_AGENT = "DiscWise/1.0 (personal music archive tool)"
TIMEOUT = 12
MAX_DOWNLOAD = 25 * 1024 * 1024
MAX_PER_SOURCE = 5
MAX_CANDIDATES = 12
MIN_MATCH = 0.30            # candidates that match the album worse than this are dropped
SMALL_SIDE = 400            # a saved picture smaller than this gets a warning


# ---------------------------------------------------------------- what to search for
_BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_DISC = re.compile(r"(?i)[\s._-]*\b(cd|disc|disk)\s*\d+\b.*$")
_YEAR_PREFIX = re.compile(r"^\s*[\(\[]?\d{4}(?:[.\-/]\d{2}){0,2}[\)\]]?\s*[-_.]*\s*")
_JUNK_WORDS = re.compile(r"(?i)\b(flac|24[\s-]?bit|16[\s-]?bit|lossless|remaster(?:ed)?|"
                         r"deluxe edition|expanded edition|bonus tracks?)\b")


def clean_album(name):
    """An album name the way a record shop would list it: no year in front, no brackets, no CD 2, no
    'FLAC' or 'Remastered'. Falls back to the original if cleaning leaves nothing (an album called 1984)."""
    s = _BRACKETS.sub(" ", name.replace("_", " "))
    plain = re.sub(r"\s+", " ", s).strip(" -_.")
    s = _YEAR_PREFIX.sub("", s)
    s = _DISC.sub("", s)
    s = _JUNK_WORDS.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" -_.")
    return s or plain or name.strip()


def guess_album(folder, flac_path=None):
    """(artist, album) to search for: the tags of a FLAC or MP3 song first, then the folder names."""
    artist = album = ""
    if FLAC is not None and flac_path:
        try:
            if flac_path.lower().endswith(".mp3"):
                tags = MutagenFile(flac_path, easy=True).tags or {}
            else:
                tags = FLAC(flac_path).tags or {}

            def first(*keys):
                for key in keys:
                    value = tags.get(key)
                    if value and str(value[0]).strip():
                        return str(value[0]).strip()
                return ""
            artist = first("albumartist", "album artist", "artist")
            album = first("album")
        except Exception:
            pass
    folder = folder.rstrip(os.sep)
    album_dir = os.path.dirname(folder) if art_embedder.is_disc_folder(os.path.basename(folder)) else folder
    if not album:
        album = os.path.basename(album_dir)
    if not artist:
        artist = os.path.basename(os.path.dirname(album_dir))
    return artist.strip(), clean_album(album)


def save_folder(folder):
    """Where the found picture is saved: the album folder, or the folder above it when the album folder is
    one disc of a set (the Embedder looks there for discs, so every disc finds the one picture)."""
    folder = folder.rstrip(os.sep)
    if art_embedder.is_disc_folder(os.path.basename(folder)):
        return os.path.dirname(folder)
    return folder


# ---------------------------------------------------------------- talking to the internet
def _get(url, accept="*/*"):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = resp.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise ValueError("that picture is too big")
    return data


def _get_json(url):
    return json.loads(_get(url, "application/json").decode("utf-8", "replace"))


def _q(params):
    return urllib.parse.urlencode(params)


def _lucene(text):
    return re.sub(r'["\\]', " ", text).strip()


def _itunes(artist, album):
    data = _get_json("https://itunes.apple.com/search?" + _q(
        {"term": f"{artist} {album}".strip(), "entity": "album", "limit": 10}))
    out = []
    for r in data.get("results", []):
        art = r.get("artworkUrl100") or ""
        if not art:
            continue
        out.append({"source": "iTunes", "artist": r.get("artistName", ""), "album": r.get("collectionName", ""),
                    "year": (r.get("releaseDate") or "")[:4],
                    "thumb": art.replace("100x100bb", "250x250bb"),
                    "full_urls": [art.replace("100x100bb", "1200x1200bb"), art.replace("100x100bb", "600x600bb"),
                                  art]})
    return out


def _deezer(artist, album):
    def run(query):
        return _get_json("https://api.deezer.com/search/album?" + _q({"q": query})).get("data", [])
    rows = []
    if artist:
        rows = run(f'artist:"{_lucene(artist)}" album:"{_lucene(album)}"')
    if not rows:
        rows = run(f"{artist} {album}".strip())
    out = []
    for r in rows:
        thumb = r.get("cover_medium") or ""
        if not thumb or not re.search(r"/images/cover/[0-9a-f]{32}/", thumb):
            continue                                    # Deezer's grey "no cover" placeholder has no id
        out.append({"source": "Deezer", "artist": (r.get("artist") or {}).get("name", ""),
                    "album": r.get("title", ""), "year": "", "thumb": thumb,
                    "full_urls": [u for u in (r.get("cover_xl"), r.get("cover_big"), thumb) if u]})
    return out


def _musicbrainz(artist, album):
    query = f'releasegroup:"{_lucene(album)}"'
    if artist:
        query += f' AND artist:"{_lucene(artist)}"'
    data = _get_json("https://musicbrainz.org/ws/2/release-group/?" + _q(
        {"query": query, "fmt": "json", "limit": 6}))
    out = []
    for r in data.get("release-groups", []):
        mbid = r.get("id")
        if not mbid:
            continue
        credit = (r.get("artist-credit") or [{}])[0]
        base = f"https://coverartarchive.org/release-group/{mbid}/"
        out.append({"source": "MusicBrainz", "artist": credit.get("name") or credit.get("artist", {}).get("name", ""),
                    "album": r.get("title", ""), "year": (r.get("first-release-date") or "")[:4],
                    "thumb": base + "front-250",
                    "full_urls": [base + "front-1200", base + "front-500", base + "front"]})
    return out


SOURCES = (("iTunes", _itunes), ("Deezer", _deezer), ("MusicBrainz", _musicbrainz))


# ---------------------------------------------------------------- ranking
def _norm(text):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()


def _sim(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    if len(a) >= 3 and len(b) >= 3 and (a in b or b in a):
        ratio = max(ratio, 0.9)
    return ratio


def score(candidate, artist, album):
    """0 to 1: how well a candidate's artist and album names match what we're looking for."""
    if not artist:
        return _sim(candidate["album"], album)
    return 0.65 * _sim(candidate["album"], album) + 0.35 * _sim(candidate["artist"], artist)


def search(artist, album):
    """Asks every source at once. Returns (candidates best-first, errors) where errors is a list of
    'iTunes: ...' strings for sources that couldn't be reached (so 'offline' is easy to tell from 'nothing
    found')."""
    results, errors = [], []

    def ask(entry):
        name, func = entry
        try:
            return name, func(artist, album), None
        except Exception as exc:
            return name, [], f"{name}: {exc}"
    with ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
        answers = list(pool.map(ask, SOURCES))
    for order, (name, found, error) in enumerate(answers):
        if error:
            errors.append(error)
        for c in found:
            c["score"] = score(c, artist, album)
            c["order"] = order
        keep = sorted((c for c in found if c["score"] >= MIN_MATCH), key=lambda c: -c["score"])
        results.extend(keep[:MAX_PER_SOURCE])
    seen, unique = set(), []
    for c in sorted(results, key=lambda c: (-c["score"], c["order"])):
        key = c["thumb"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
    return unique[:MAX_CANDIDATES], errors


def fetch_thumbnail(candidate):
    """The small preview picture's bytes, or None if that source has no picture for it."""
    try:
        data = _get(candidate["thumb"])
        if Image is not None:
            Image.open(io.BytesIO(data)).verify()
        return data
    except Exception:
        return None


def candidate_from_link(url):
    """A candidate for a picture link the person pasted in."""
    url = url.strip()
    if not re.match(r"(?i)^https?://\S+$", url):
        raise ValueError("that doesn't look like a web link (it should start with http)")
    return {"source": "Your link", "artist": "", "album": "", "year": "", "thumb": url, "full_urls": [url],
            "score": 1.0, "order": 0}


# ---------------------------------------------------------------- saving
def _free_name(folder, stem, ext):
    path = os.path.join(folder, stem + ext)
    n = 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{stem}_{n}{ext}")
        n += 1
    return path


def save_cover(data, folder, source="", log_path=None):
    """Checks the bytes really are a picture, then saves them in the folder as cover.jpg / cover.png (never
    over something that is already there). Returns {"path", "width", "height", "small"}."""
    if Image is None:
        raise RuntimeError("saving pictures needs the Pillow add-on (pip install pillow --break-system-packages)")
    img = Image.open(io.BytesIO(data))
    img.load()
    width, height = img.size
    fmt = (img.format or "").upper()
    if fmt == "JPEG":
        ext, out = ".jpg", data
    elif fmt == "PNG":
        ext, out = ".png", data
    else:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=95)
        ext, out = ".jpg", buf.getvalue()
    path = _free_name(folder, "cover", ext)
    tmp = path + ".dw_tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(out)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as log:
                log.write(json.dumps({"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "path": path,
                                      "source": source, "width": width, "height": height},
                                     ensure_ascii=False) + "\n")
        except OSError:
            pass
    return {"path": path, "width": width, "height": height, "small": min(width, height) < SMALL_SIDE}


def download_cover(candidate, folder, log_path=None):
    """Downloads the biggest version of the picture that works and saves it. Raises if none of them do."""
    last = None
    for url in candidate["full_urls"]:
        try:
            data = _get(url)
            return save_cover(data, folder, candidate.get("source", ""), log_path)
        except Exception as exc:
            last = exc
    raise last or RuntimeError("no picture could be downloaded")
