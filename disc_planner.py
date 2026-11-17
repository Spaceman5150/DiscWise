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
Disc packing planner for a music library.

Scans a root Music folder, finds "album" units (any folder that directly
contains audio files, including multi-disc subfolders like Disc 1/CD 1
counted as part of their parent album), excludes anything matching
exclude keywords (e.g. youtube, background music), and simulates
packing them onto DVD-R discs using a greedy first-fit-decreasing
algorithm -- separately for lossless (FLAC) content and MP3 content,
since you want the MP3s isolated onto their own disc(s).

Also writes two output files next to the scanned folder: a printable
Excel disc checklist (with a running-totals Summary sheet), and a
searchable SQLite database of every artist/album/song and which disc
it landed on -- open it with DB Browser for SQLite (or hook it into
LibreOffice Base via a SQLite driver extension) to search your whole
collection.

Usage:
    python3 disc_planner.py "$HOME/Music/FLAC Files"

    Multiple folders are packed together onto the same set of discs:
    python3 disc_planner.py "$HOME/Music/FLAC Files" "$HOME/Music/Extra Van Halen all FLAC"

    If an extra folder is already dedicated to a single artist (no
    artist-level subfolder inside it -- its immediate children are the
    album/show folders directly), tag it so those units group under
    that artist correctly:
    python3 disc_planner.py "$HOME/Music/FLAC Files" "$HOME/Music/Extra Van Halen all FLAC" \\
        --force-artist "$HOME/Music/Extra Van Halen all FLAC=Van Halen"

Adjust DISC_CAPACITY_MB and EXCLUDE_KEYWORDS below if needed.
"""

import argparse
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime

try:
    import mutagen
    HAVE_MUTAGEN = True
except ImportError:
    HAVE_MUTAGEN = False

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# DVD-R nominal capacity is 4.7GB (4700MB in decimal marketing terms),
# but real usable capacity after formatting/filesystem overhead is
# closer to 4300MB. Using a conservative number leaves headroom so a
# disc doesn't come up short at burn time.
DISC_CAPACITY_MB = 4300

# Disc types the packer can plan for. Each number is the usable space in MB, kept a few percent
# under the disc's rated size (same cushion as the original 4300 for a 4.7GB DVD-R) so a full disc
# still burns cleanly. "DVD-R DL" is the dual-layer version of the minus-R format.
MEDIA_TYPES = {
    "4.7 GB  (DVD-R or DVD+R)": 4300,
    "8.5 GB  (DVD-R or DVD+R dual layer)": 7800,
    "25 GB  (Blu-ray BD-R)": 22800,
    "50 GB  (Blu-ray BD-R dual layer)": 45600,
}
DEFAULT_MEDIA_TYPE = "4.7 GB  (DVD-R or DVD+R)"
# What the choices were called in earlier versions, so a saved setting still finds its match.
_OLD_MEDIA_NAMES = {
    "DVD-R  (4.7 GB)": "4.7 GB  (DVD-R or DVD+R)",
    "DVD-R DL  (8.5 GB)": "8.5 GB  (DVD-R or DVD+R dual layer)",
    "BD-R  (25 GB)": "25 GB  (Blu-ray BD-R)",
    "BD-R DL  (50 GB)": "50 GB  (Blu-ray BD-R dual layer)",
}


def media_type_name(saved):
    """Turns a saved disc-capacity choice (current or old name) into a current name, or None."""
    saved = _OLD_MEDIA_NAMES.get(saved, saved)
    return saved if saved in MEDIA_TYPES else None


def set_media_type(name):
    """Switches the planned disc size to one of MEDIA_TYPES (unknown names are ignored)."""
    global DISC_CAPACITY_MB
    if name in MEDIA_TYPES:
        DISC_CAPACITY_MB = MEDIA_TYPES[name]
    return DISC_CAPACITY_MB

# Folder names containing any of these (case-insensitive) are skipped
# entirely -- e.g. tiny YouTube background-music folders.
EXCLUDE_KEYWORDS = ["youtube", "background music", "for youtube"]

# Folder names containing any of these (case-insensitive) are treated
# as the MP3 pool and packed onto their own separate disc(s).
MP3_KEYWORDS = ["mp3"]

# Bonus/support folders that sometimes sit next to real albums (or
# turn up when an oversized "Discography" folder gets split apart).
# These hold artwork, scans, or leftover technical files, not music,
# so they should never be counted as an "album" of their own -- an
# exact (whole-name) match is used rather than a substring, so a real
# album that happens to contain one of these words isn't caught by
# mistake.
NON_ALBUM_FOLDER_NAMES = {
    "cover art", "covers", "cover", "artwork", "scans", "scan",
    "technical", "spectrograms", "spectrogram", "spectrograph",
    "extra art", "booklet", "liner notes",
}

AUDIO_EXTENSIONS = (".flac", ".mp3", ".mp4", ".mov", ".m4v", ".avi", ".mkv")

# Of the folders above, these are only generated technical leftovers, so they're NOT worth putting on
# a disc. The rest ("Extra Art", "Scans", "Booklet"...) can hold things you'd hate to lose -- a comic
# book PDF, scanned liner notes -- so when one sits beside the albums, it gets saved as its own item.
DISPOSABLE_FOLDER_NAMES = {"technical", "spectrograms", "spectrogram", "spectrograph"}


def extras_folders_beside_albums(path):
    """Non-album folders (Extra Art, Scans...) directly inside `path` that are worth saving."""
    keep = []
    for d in sorted(os.listdir(path)):
        full = os.path.join(path, d)
        if (os.path.isdir(full) and is_non_album_folder(d) and not is_excluded(d)
                and d.strip().lower() not in DISPOSABLE_FOLDER_NAMES):
            keep.append(d)
    return keep


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



def get_folder_size_mb(path):
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if os.path.isfile(fp):
                total += os.path.getsize(fp)
    return total / (1024 * 1024)


def get_direct_files_size_mb(path):
    """Sums only the files sitting directly in `path`, not in subfolders."""
    total = 0
    try:
        for f in os.listdir(path):
            fp = os.path.join(path, f)
            if os.path.isfile(fp):
                total += os.path.getsize(fp)
    except PermissionError:
        pass
    return total / (1024 * 1024)


def folder_has_audio_directly(path):
    try:
        return any(f.lower().endswith(AUDIO_EXTENSIONS) for f in os.listdir(path)
                   if os.path.isfile(os.path.join(path, f)))
    except PermissionError:
        return False


def folder_has_music(path):
    """True when there is at least one audio file anywhere inside `path` (subfolders included)."""
    for _dirpath, _dirs, filenames in os.walk(path):
        if any(f.lower().endswith(AUDIO_EXTENSIONS) for f in filenames):
            return True
    return False


def is_excluded(name):
    lname = name.lower()
    return any(kw in lname for kw in EXCLUDE_KEYWORDS)


def is_non_album_folder(name):
    return name.strip().lower() in NON_ALBUM_FOLDER_NAMES


def should_skip_subfolder(name):
    return is_excluded(name) or is_non_album_folder(name)


def is_mp3_pool(name):
    lname = name.lower()
    return any(kw in lname for kw in MP3_KEYWORDS)


import re

DISC_PART_PATTERN = re.compile(r'^(cd|disc|disk)\s*[\-_.]?\s*0*\d+[a-z]?$', re.IGNORECASE)


def looks_like_disc_part(name):
    """True for folder names like 'CD1', 'CD 2', 'Disc 1', 'Disk01' --
    the parts of a single multi-disc release, not separate albums."""
    return bool(DISC_PART_PATTERN.match(name.strip()))


def _extras_units(path, label):
    """Units for the worthwhile non-album folders inside `path` (empty ones are left out)."""
    units = []
    for name in extras_folders_beside_albums(path):
        full = os.path.join(path, name)
        size_mb = get_folder_size_mb(full)
        if size_mb > 0:
            units.append((f"{label}/{name}", size_mb, full))
    return units


def discover_units(path, label, capacity_mb):
    """
    Recursively finds real album-level units under `path`, at whatever
    depth they actually sit -- unlike a purely size-based split, this
    looks at folder structure, not just whether something is oversized.

    A folder becomes one unit when it holds audio directly with no
    further subfolders, or when its subfolders are just the parts of
    one multi-disc release (CD1/CD2, Disc 1/Disc 2, etc). Otherwise its
    subfolders are distinct albums (or a further grouping layer like
    "Studio Albums"/"Live") and each is explored the same way,
    regardless of whether the parent folder is "oversized" -- so a
    6-album "Studio Albums" folder that happens to fit under one disc
    still gets split into its 6 real albums.

    Returns a list of (label, size_mb, real_path).
    """
    subfolders = [d for d in sorted(os.listdir(path))
                  if os.path.isdir(os.path.join(path, d)) and not should_skip_subfolder(d)]

    if not subfolders:
        size_mb = get_folder_size_mb(path)
        return [(label, size_mb, path)] if size_mb > 0 else []

    if all(looks_like_disc_part(d) for d in subfolders):
        size_mb = get_folder_size_mb(path)
        if size_mb <= capacity_mb:
            return [(label, size_mb, path)] if size_mb > 0 else []
        # An oversized multi-disc release with no other way to split --
        # fall back to one unit per disc rather than leaving it whole.
        results = []
        for sub in subfolders:
            results.extend(discover_units(os.path.join(path, sub), f"{label}/{sub}", capacity_mb))
        results.extend(_extras_units(path, label))
        return results

    # Subfolders here are distinct albums (or another grouping layer) --
    # explore each one independently, at any depth.
    results = []
    for sub in subfolders:
        results.extend(discover_units(os.path.join(path, sub), f"{label}/{sub}", capacity_mb))

    # Extra Art / Scans / Booklet folders sitting beside the albums are saved too, as their own items.
    results.extend(_extras_units(path, label))

    # A folder can ALSO hold audio directly alongside real album
    # subfolders (e.g. a stray loose track sitting next to album folders).
    if folder_has_audio_directly(path):
        direct_mb = get_direct_files_size_mb(path)
        if direct_mb > 0:
            results.append((label, direct_mb, path))

    return results


def split_oversized(path, label, capacity_mb):
    """
    If a folder's total size exceeds one disc's capacity, recurse into
    its own subfolders and treat each as a separate unit instead --
    e.g. a "Discography" folder becomes N per-album units. Keeps
    recursing as long as a piece is still oversized and splittable.
    Returns a list of (label, size_mb, real_path).
    """
    size_mb = get_folder_size_mb(path)
    if size_mb <= capacity_mb:
        return [(label, size_mb, path)]

    subfolders = [d for d in sorted(os.listdir(path))
                  if os.path.isdir(os.path.join(path, d)) and not should_skip_subfolder(d)]

    if not subfolders:
        # No further way to split -- flag as still oversized.
        return [(label, size_mb, path)]

    results = []
    for sub in subfolders:
        sub_path = os.path.join(path, sub)
        sub_label = f"{label}/{sub}"
        results.extend(split_oversized(sub_path, sub_label, capacity_mb))
    return results


def find_album_units(root, capacity_mb):
    """
    Finds album-level units at any depth under root: for each top-level
    artist folder, its structure below is explored via discover_units,
    which finds real album boundaries regardless of whether a given
    folder happens to be oversized. If a top-level folder contains
    audio files directly (no subfolders), it's treated as its own unit.
    """
    units = []  # list of (label, size_mb, is_mp3, real_path)

    if not os.path.isdir(root):
        print(f"ERROR: {root} is not a directory.")
        sys.exit(1)

    for top_name in sorted(os.listdir(root)):
        top_path = os.path.join(root, top_name)
        if not os.path.isdir(top_path):
            continue
        if is_excluded(top_name):
            print(f"Skipping excluded folder: {top_name}")
            continue

        mp3_pool = is_mp3_pool(top_name)

        for label, size_mb, real_path in discover_units(top_path, top_name, capacity_mb):
            if size_mb > 0:
                units.append((label, size_mb, mp3_pool, real_path))

    return units


def find_album_units_categorized(root, capacity_mb, artist_name, is_mp3=False):
    """
    For a single-artist folder, at any depth (a flat list of albums, a
    grouping layer like Live/EPs/Studio, or a mix of both). Kept as a
    separate name for compatibility with --force-artist-nested, but now
    identical to find_album_units_flat -- discover_units finds the real
    album boundaries regardless of how many folder layers are involved.
    """
    return find_album_units_flat(root, capacity_mb, artist_name, is_mp3)


def find_album_units_flat(root, capacity_mb, artist_name, is_mp3=False):
    """
    For a folder that's already dedicated to a single artist, at
    whatever depth its albums actually sit -- a flat list of album
    folders, an extra grouping layer (Live/EPs/Studio), or a mix.
    Every resulting unit is labeled under `artist_name` so it groups
    correctly with that artist's other material in the checklist and
    database.
    """
    if not os.path.isdir(root):
        print(f"ERROR: {root} is not a directory.")
        sys.exit(1)

    return [(label, size_mb, is_mp3, real_path)
            for label, size_mb, real_path in discover_units(root, artist_name, capacity_mb)
            if size_mb > 0]


def pack_discs(units, capacity_mb):
    """
    First-fit-decreasing bin packing. units: list of tuples whose first
    two elements are (label, size_mb) -- any additional elements (e.g.
    a real folder path) are carried through untouched.
    Returns list of discs, each a list of those same tuples.
    """
    sorted_units = sorted(units, key=lambda u: u[1], reverse=True)
    discs = []  # each is [used_mb, [items]]

    for item in sorted_units:
        label, size_mb = item[0], item[1]
        if size_mb > capacity_mb:
            print(f"WARNING: '{label}' ({size_mb:.1f}MB) exceeds a single disc's capacity on its own!")
            discs.append([size_mb, [item]])
            continue

        placed = False
        for disc in discs:
            if disc[0] + size_mb <= capacity_mb:
                disc[0] += size_mb
                disc[1].append(item)
                placed = True
                break

        if not placed:
            discs.append([size_mb, [item]])

    return discs


def print_disc_plan(discs, capacity_mb, title, start_num=1):
    print(f"\n=== {title}: {len(discs)} disc(s) needed ===")
    for i, (used_mb, items) in enumerate(discs, start_num):
        pct = (used_mb / capacity_mb) * 100
        print(f"\nDisc {i}  ({used_mb:.0f}MB / {capacity_mb}MB, {pct:.0f}% full)")
        for item in items:
            label, size_mb = item[0], item[1]
            print(f"   - {label}  ({size_mb:.0f}MB)")


def artist_name_for_label(label, is_mp3):
    """
    Pulls the artist name out of a unit's path-style label. For FLAC
    units the top-level folder is the artist. For MP3 units the
    top-level folder is just the "MP3 Collection" container, so the
    artist is the folder one level below that instead.
    """
    parts = label.split("/")
    if is_mp3 and len(parts) > 1:
        return parts[1]
    return parts[0]


def write_summary_sheet(wb, checklist_sheet_name, last_data_row, unique_artists, total_albums):
    """
    Adds a "Summary" sheet with running totals pulled from the
    checklist via formulas, so it stays accurate if the checklist is
    ever hand-edited, and gives a running snapshot each time this
    script is re-run down the road (e.g. after adding new albums).
    """
    ws = wb.create_sheet("Summary")

    title_font = Font(name="Arial", bold=True, size=14)
    label_font = Font(name="Arial", bold=True, size=11)
    value_font = Font(name="Arial", size=11)
    note_font = Font(name="Arial", italic=True, size=9, color="666666")

    ws["A1"] = "Music Library Disc Archive — Summary"
    ws["A1"].font = title_font
    ws["A2"] = f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    ws["A2"].font = note_font

    src = f"'{checklist_sheet_name}'"
    rows = [
        ("FLAC discs", f'=COUNTIF({src}!B2:B{last_data_row},"FLAC")'),
        ("MP3 discs", f'=COUNTIF({src}!B2:B{last_data_row},"MP3")'),
        ("Total discs", f'=B4+B5'),
        ("", ""),
        ("FLAC size (GB)", f'=SUMIF({src}!B2:B{last_data_row},"FLAC",{src}!E2:E{last_data_row})/1024'),
        ("MP3 size (GB)", f'=SUMIF({src}!B2:B{last_data_row},"MP3",{src}!E2:E{last_data_row})/1024'),
        ("Total size (GB)", f'=B8+B9'),
        ("", ""),
        ("Unique artists", unique_artists),
        ("Total albums", total_albums),
        ("", ""),
        ("Discs burned", f'=COUNTA({src}!G2:G{last_data_row})'),
        ("Discs verified", f'=COUNTA({src}!H2:H{last_data_row})'),
    ]

    start_row = 4
    for i, (label, formula) in enumerate(rows):
        r = start_row + i
        ws.cell(row=r, column=1, value=label).font = label_font
        if formula:
            cell = ws.cell(row=r, column=2, value=formula)
            cell.font = value_font
            if "GB" in label:
                cell.number_format = "0.00"

    ws.column_dimensions["A"].width = 20
    ws.column_dimensions["B"].width = 14

    note_row = start_row + len(rows) + 1
    ws.cell(row=note_row, column=1,
            value="Re-run disc_planner.py any time your library changes — this sheet updates from the checklist automatically.").font = note_font
    ws.cell(row=note_row + 1, column=1,
            value="Unique artists / Total albums are counted directly from your folder scan (not a sheet formula), so they refresh only when you re-run the script.").font = note_font

    return ws


CHECKLIST_HEADERS = ["Disc #", "Type", "Number of Albums", "Contents", "Size (MB)", "% Full", "Burned", "Verified"]


def _checklist_cols(ws):
    """
    {"disc": 1, "type": 2, "albums": 3, "contents": 4, ...} for a checklist sheet, found by reading its
    header row, so both the original 7-column layout (no "Number of Albums") and the newer 8-column
    layout work. Falls back to the original layout if there's no readable header.
    """
    names = {"disc #": "disc", "type": "type", "number of albums": "albums", "contents": "contents",
             "size (mb)": "size", "% full": "pct", "burned": "burned", "verified": "verified"}
    cols = {}
    for c in range(1, ws.max_column + 1):
        value = ws.cell(row=1, column=c).value
        key = names.get(str(value).strip().lower()) if value is not None else None
        if key and key not in cols:
            cols[key] = c
    if "disc" not in cols or "burned" not in cols:
        cols = {"disc": 1, "type": 2, "contents": 3, "size": 4, "pct": 5, "burned": 6, "verified": 7}
    return cols


def upgrade_checklist_layout(ws):
    """
    Adds the "Number of Albums" column (between Type and Contents) to an existing checklist sheet that
    doesn't have it yet, keeping everything else -- including hand-ticked Burned/Verified marks -- exactly
    as it was. Formulas to the right of the new column are re-pointed, and each disc's count is the
    number of album lines in its Contents cell. Returns True if it changed anything.
    """
    from openpyxl.formula.translate import Translator
    from copy import copy

    if "albums" in _checklist_cols(ws):
        return False

    ws.insert_cols(3)
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row, min_col=4, max_col=ws.max_column):
        for cell in row:
            if isinstance(cell.value, str) and cell.value.startswith("="):
                origin = f"{get_column_letter(cell.column - 1)}{cell.row}"
                cell.value = Translator(cell.value, origin=origin).translate_formula(cell.coordinate)

    header = ws.cell(row=1, column=3, value="Number of Albums")
    template = ws.cell(row=1, column=2)
    header.font, header.fill = copy(template.font), copy(template.fill)
    header.alignment, header.border = copy(template.alignment), copy(template.border)

    body_font = Font(name="Arial", size=11)
    thin = Side(style="thin", color="999999")
    box_border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center")
    for r in range(2, ws.max_row + 1):
        if not isinstance(ws.cell(row=r, column=1).value, int):
            continue
        contents = ws.cell(row=r, column=4).value or ""
        count = sum(1 for line in str(contents).split("\n") if line.strip())
        cell = ws.cell(row=r, column=3, value=count)
        cell.font, cell.alignment, cell.border = body_font, center, box_border

    _set_checklist_widths(ws)
    return True


def _set_checklist_widths(ws):
    for letter, width in (("A", 8), ("B", 8), ("C", 11), ("D", 55), ("E", 11), ("F", 8), ("G", 10), ("H", 10)):
        ws.column_dimensions[letter].width = width


def write_checklist_xlsx(flac_discs, mp3_discs, capacity_mb, out_path, unique_artists, total_albums,
                          append=False, start_disc_num=1, keep_through=None):
    """
    Writes a printable disc checklist workbook: one row per disc, with
    its contents listed, its size, and blank "Burned" / "Verified"
    boxes to tick by hand with a pen after each disc is done.

    With append=True and an existing file at out_path, new disc rows
    are added below the existing ones (numbered starting at
    start_disc_num) rather than starting the sheet over -- for when
    older material has already been burned and deleted, so this run
    only sees what's left.

    With keep_through=N, rows for discs 1..N are kept exactly as they
    are (including any hand-ticked Burned/Verified marks), but every
    row after that is deleted and replaced with a fresh repack
    starting at start_disc_num (which should be N+1) -- for when
    nothing gets deleted from disk after burning, and a partially-full
    last disc should be freed up and repacked with new material.
    """
    header_font = Font(name="Arial", bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="4472C4")
    body_font = Font(name="Arial", size=11)
    thin = Side(style="thin", color="999999")
    box_border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center")
    wrap_left = Alignment(horizontal="left", vertical="top", wrap_text=True)

    if keep_through is not None and os.path.exists(out_path):
        wb = load_workbook(out_path)
        ws = wb["Disc Checklist"]
        if upgrade_checklist_layout(ws):
            print("Added a \"Number of Albums\" column to the existing checklist (your edits were kept).")

        last_keep_row = None
        for r in range(2, ws.max_row + 1):
            if ws.cell(row=r, column=1).value == keep_through:
                last_keep_row = r
                break

        if last_keep_row is None:
            print(f"WARNING: couldn't find Disc {keep_through} in the existing checklist -- "
                  f"keeping everything and appending after the last row instead.")
            last_keep_row = ws.max_row

        if ws.max_row > last_keep_row:
            ws.delete_rows(last_keep_row + 1, ws.max_row - last_keep_row)

        row = last_keep_row + 1
        disc_num = start_disc_num

        if "Summary" in wb.sheetnames:
            del wb["Summary"]

    elif append and os.path.exists(out_path):
        wb = load_workbook(out_path)
        ws = wb["Disc Checklist"]
        if upgrade_checklist_layout(ws):
            print("Added a \"Number of Albums\" column to the existing checklist (your edits were kept).")

        total_row_idx = None
        for r in range(2, ws.max_row + 1):
            if ws.cell(row=r, column=4).value == "TOTAL":
                total_row_idx = r
                break

        if total_row_idx:
            last_existing_row = total_row_idx - 1
            # There's an intentional blank spacer row directly above
            # TOTAL -- skip past it to find the true last disc row.
            while last_existing_row >= 2 and ws.cell(row=last_existing_row, column=1).value is None:
                last_existing_row -= 1
            for col in range(1, 9):
                ws.cell(row=total_row_idx, column=col).value = None
        else:
            last_existing_row = ws.max_row

        row = last_existing_row + 1
        disc_num = start_disc_num

        if "Summary" in wb.sheetnames:
            del wb["Summary"]
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = "Disc Checklist"

        headers = list(CHECKLIST_HEADERS)
        ws.append(headers)
        for col in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=col)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = center
            cell.border = box_border

        row = 2
        disc_num = start_disc_num

    for disc_type, discs in (("FLAC", flac_discs), ("MP3", mp3_discs)):
        for used_mb, items in discs:
            contents = "\n".join(f"{item[0]}  ({item[1]:.0f}MB)" for item in items)
            ws.cell(row=row, column=1, value=disc_num).alignment = center
            ws.cell(row=row, column=2, value=disc_type).alignment = center
            ws.cell(row=row, column=3, value=len(items)).alignment = center
            ws.cell(row=row, column=4, value=contents).alignment = wrap_left
            size_cell = ws.cell(row=row, column=5, value=round(used_mb))
            size_cell.alignment = center
            pct_cell = ws.cell(row=row, column=6, value=f"=E{row}/{capacity_mb}")
            pct_cell.number_format = "0%"
            pct_cell.alignment = center
            ws.cell(row=row, column=7, value=None).border = box_border
            ws.cell(row=row, column=8, value=None).border = box_border

            for col in range(1, 9):
                ws.cell(row=row, column=col).font = body_font
                ws.cell(row=row, column=col).border = box_border

            # Give the wrapped contents cell room to breathe. Column D
            # is 55 characters wide, and a long album name wraps into
            # multiple visual lines within that width -- counting only
            # the "\n"-separated entries undercounts the real line
            # count, which can cut off the last entry (it just doesn't
            # render below the row's bottom edge, even though the data
            # is there).
            col_width_chars = 55
            visual_lines = sum(max(1, -(-len(line) // col_width_chars)) for line in contents.split("\n"))
            ws.row_dimensions[row].height = max(18, 14 * visual_lines)

            disc_num += 1
            row += 1

    # Totals row, using a formula so it recalculates if the sheet is edited.
    # Existing + new disc rows are always contiguous starting at row 2.
    total_row = row + 1
    ws.cell(row=total_row, column=4, value="TOTAL").font = Font(name="Arial", bold=True)
    last_data_row = row - 1
    if last_data_row >= 2:
        total_cell = ws.cell(row=total_row, column=5, value=f"=SUM(E2:E{last_data_row})")
        total_cell.font = Font(name="Arial", bold=True)
        total_cell.alignment = center

    albums_row = total_row + 1
    ws.cell(row=albums_row, column=4, value="Total albums (for cross-checking against your file manager):").font = \
        Font(name="Arial", bold=True)
    albums_cell = ws.cell(row=albums_row, column=5, value=total_albums)
    albums_cell.font = Font(name="Arial", bold=True, size=13)
    albums_cell.alignment = center

    _set_checklist_widths(ws)

    ws.freeze_panes = "A2"
    ws.print_title_rows = "1:1"
    ws.page_setup.orientation = "portrait"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True

    if last_data_row >= 2:
        write_summary_sheet(wb, ws.title, last_data_row, unique_artists, total_albums)

    wb.save(out_path)
    print(f"\nWrote checklist spreadsheet: {out_path}")


def file_type_for(filename):
    """FLAC / MP3 / MP4 ... from a file name's extension (upper-cased, no dot)."""
    return os.path.splitext(filename)[1].lstrip(".").upper() or "UNKNOWN"


def get_tracks_for_unit(folder_path):
    """
    Walks a unit's folder and returns one entry per audio file:
    (track_number_or_None, title, filename, file_type) where file_type is
    the upper-cased extension (FLAC, MP3, MP4 ...). Reads the title/track
    number from tags via mutagen when available (works for both FLAC
    and MP3 through its "easy" interface); falls back to the filename
    itself when a tag is missing or mutagen isn't installed.
    """
    tracks = []
    for dirpath, _, filenames in os.walk(folder_path):
        for fn in sorted(filenames):
            if not fn.lower().endswith(AUDIO_EXTENSIONS):
                continue
            full_path = os.path.join(dirpath, fn)
            title = None
            track_no = None

            if HAVE_MUTAGEN:
                try:
                    audio = mutagen.File(full_path, easy=True)
                    if audio:
                        t = audio.get("title")
                        if t:
                            title = t[0]
                        tn = audio.get("tracknumber")
                        if tn:
                            try:
                                track_no = int(str(tn[0]).split("/")[0])
                            except ValueError:
                                track_no = None
                except Exception:
                    pass

            if not title:
                title = os.path.splitext(fn)[0]

            tracks.append((track_no, title, fn, file_type_for(fn)))

    tracks.sort(key=lambda t: (t[0] is None, t[0], t[2]))
    return tracks


def get_extra_files_for_unit(folder_path):
    """
    For an item with no music in it at all (an "Extra Art" folder holding a comic PDF, say): one
    entry per file, shaped like get_tracks_for_unit's, so the item still shows up in searches --
    the file name stands in for the song title and its extension (PDF, CBR, JPG...) for the type.
    """
    items = []
    for dirpath, _, filenames in os.walk(folder_path):
        for fn in sorted(filenames):
            if fn.startswith(".") or fn.lower() in ("thumbs.db", "desktop.ini"):
                continue
            items.append((None, os.path.splitext(fn)[0], fn, file_type_for(fn)))
    return items


def ensure_burn_columns(conn):
    """Adds the burn-tracking columns to a packing-list database made by an older version."""
    disc_cols = {row[1] for row in conn.execute("PRAGMA table_info(discs)")}
    if "burned" not in disc_cols:
        conn.execute("ALTER TABLE discs ADD COLUMN burned INTEGER NOT NULL DEFAULT 0")
    if "burned_date" not in disc_cols:
        conn.execute("ALTER TABLE discs ADD COLUMN burned_date TEXT")
    if "verified" not in disc_cols:
        conn.execute("ALTER TABLE discs ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")
    if "verified_date" not in disc_cols:
        conn.execute("ALTER TABLE discs ADD COLUMN verified_date TEXT")
    conn.commit()


def count_burned_discs(db_path):
    """How many discs a packing-list database has marked as burned (0 if it can't be read)."""
    try:
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM discs WHERE disc_number > 0 AND burned = 1").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def list_discs(db_path):
    """
    Every committed disc in a packing-list database, for the Burn Tracker: a list of dicts with
    disc_number, type, albums, size_mb, burned, burned_date, verified, verified_date.
    """
    conn = sqlite3.connect(db_path)
    try:
        ensure_burn_columns(conn)
        rows = conn.execute(
            "SELECT discs.disc_number, discs.type, COUNT(albums.id), COALESCE(SUM(albums.size_mb), 0), "
            "discs.burned, discs.burned_date, discs.verified, discs.verified_date "
            "FROM discs LEFT JOIN albums ON albums.disc_id = discs.id "
            "WHERE discs.disc_number > 0 GROUP BY discs.id ORDER BY discs.disc_number").fetchall()
    finally:
        conn.close()
    return [{"disc_number": n, "type": t, "albums": a, "size_mb": mb,
             "burned": bool(b), "burned_date": d or "", "verified": bool(v), "verified_date": vd or ""}
            for n, t, a, mb, b, d, v, vd in rows]


def set_discs_burned(db_path, disc_numbers, burned=True, date_text=None):
    """
    Marks the given disc numbers as burned (stamping today's date, unless it already has one) or not
    burned. A disc that isn't burned can't be verified, so un-burning also clears its Verified mark.
    Returns how many changed.
    """
    import datetime
    date_text = date_text or datetime.date.today().isoformat()
    conn = sqlite3.connect(db_path)
    try:
        ensure_burn_columns(conn)
        changed = 0
        for number in disc_numbers:
            if burned:
                cur = conn.execute(
                    "UPDATE discs SET burned = 1, "
                    "burned_date = CASE WHEN burned = 1 AND burned_date IS NOT NULL THEN burned_date ELSE ? END "
                    "WHERE disc_number = ? AND disc_number > 0", (date_text, number))
            else:
                cur = conn.execute(
                    "UPDATE discs SET burned = 0, burned_date = NULL, verified = 0, verified_date = NULL "
                    "WHERE disc_number = ? AND disc_number > 0", (number,))
            changed += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return changed


def set_discs_verified(db_path, disc_numbers, verified=True, date_text=None):
    """
    Marks the given disc numbers as verified (stamping today's date) or not verified. A verified
    disc has obviously been burned, so verifying also marks it burned if it wasn't already.
    Returns how many changed.
    """
    import datetime
    date_text = date_text or datetime.date.today().isoformat()
    if verified:
        set_discs_burned(db_path, disc_numbers, True, date_text)
    conn = sqlite3.connect(db_path)
    try:
        ensure_burn_columns(conn)
        changed = 0
        for number in disc_numbers:
            cur = conn.execute(
                "UPDATE discs SET verified = ?, verified_date = ? WHERE disc_number = ? AND disc_number > 0",
                (1 if verified else 0, date_text if verified else None, number))
            changed += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return changed


def _read_checklist_rows(path):
    """
    Returns the checklist's rows as lists of cell values (column 1 first), from either an Excel
    (.xlsx) file or a LibreOffice (.ods) file. LibreOffice saves an edited copy as .ods unless you
    tell it to keep the Excel format, so both need to work. The .ods is read straight from its
    zipped XML, so no extra software has to be installed.
    """
    if not path.lower().endswith(".ods"):
        wb = load_workbook(path, data_only=True)
        ws = wb["Disc Checklist"] if "Disc Checklist" in wb.sheetnames else wb.worksheets[0]
        return [[ws.cell(row=r, column=c).value for c in range(1, 9)] for r in range(1, ws.max_row + 1)]

    import zipfile
    import xml.etree.ElementTree as ET
    ns = {"table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
          "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
          "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0"}
    table_ns = "{%s}" % ns["table"]
    office_ns = "{%s}" % ns["office"]
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("content.xml"))
    tables = root.findall(".//table:table", ns)
    sheet = next((t for t in tables if t.get(table_ns + "name") == "Disc Checklist"), tables[0] if tables else None)
    if sheet is None:
        return []

    rows = []
    # iter() rather than direct children: LibreOffice can wrap rows (e.g. a repeating header row) in groups.
    for row in sheet.iter(table_ns + "table-row"):
        values = []
        for cell in row:
            if cell.tag not in (table_ns + "table-cell", table_ns + "covered-table-cell"):
                continue
            if cell.get(office_ns + "value") is not None:
                number = float(cell.get(office_ns + "value"))
                value = int(number) if number == int(number) else number
            else:
                value = "".join("".join(p.itertext()) for p in cell.findall("text:p", ns)).strip() or None
            repeat = min(int(cell.get(table_ns + "number-columns-repeated", "1")), 20)
            values.extend([value] * repeat)
        repeat_rows = min(int(row.get(table_ns + "number-rows-repeated", "1")), 5)
        rows.extend([values] * repeat_rows)
    return rows


def _checklist_ticks(checklist_path):
    """
    (burned, verified, listed): the disc numbers ticked in the checklist's Burned column, the ones
    ticked in its Verified column, and every disc number that has a row. Any non-empty cell counts
    as a tick, except obvious "no" values (no / n / false / 0 / -). A disc ticked Verified is
    counted as burned too, since it can't be verified without being burned.
    """
    no_values = {"", "no", "n", "false", "0", "-", "none"}

    def ticked(cell):
        return cell is not None and str(cell).strip().lower() not in no_values

    burned, verified, listed = [], [], set()
    burned_idx, verified_idx = 5, 6      # the original layout; the header row (below) overrides these
    for values in _read_checklist_rows(checklist_path):
        if values and isinstance(values[0], str) and values[0].strip().lower().startswith("disc"):
            names = [str(v).strip().lower() if v is not None else "" for v in values]
            if "burned" in names:
                burned_idx = names.index("burned")
            if "verified" in names:
                verified_idx = names.index("verified")
            continue
        if len(values) <= burned_idx or not isinstance(values[0], int):
            continue
        number = values[0]
        listed.add(number)
        is_verified = len(values) > verified_idx and ticked(values[verified_idx])
        if is_verified:
            verified.append(number)
        if is_verified or ticked(values[burned_idx]):
            burned.append(number)
    return burned, verified, listed


def burned_through(db_path):
    """
    The highest disc number N such that discs 1..N are ALL marked burned in the packing-list
    database (0 if disc 1 isn't burned or the database can't be read). This is the "keep burned"
    line: those discs are left exactly as they are, and everything after them can be repacked.
    """
    try:
        discs = list_discs(db_path)
    except sqlite3.Error:
        return 0
    through = 0
    for disc in discs:
        if disc["disc_number"] == through + 1 and disc["burned"]:
            through += 1
        else:
            break
    return through


def sync_checklist_ticks(db_path, checklist_path):
    """
    Writes an X into the checklist's Burned (and Verified) boxes for every disc the database says
    is burned (verified), without touching anything else. Used after a keep-burned run so ticks that
    only existed in the LibreOffice (.ods) copy still show in the rewritten Excel checklist.
    Returns how many boxes were filled in.
    """
    if not os.path.exists(checklist_path):
        return 0
    discs = {d["disc_number"]: d for d in list_discs(db_path)}
    wb = load_workbook(checklist_path)
    ws = wb["Disc Checklist"] if "Disc Checklist" in wb.sheetnames else wb.worksheets[0]
    filled = 0
    cols = _checklist_cols(ws)
    tick_cols = [(cols["burned"], "burned")]
    if "verified" in cols:
        tick_cols.append((cols["verified"], "verified"))
    for r in range(2, ws.max_row + 1):
        number = ws.cell(row=r, column=1).value
        disc = discs.get(number) if isinstance(number, int) else None
        if not disc:
            continue
        for col, flag in tick_cols:
            cell = ws.cell(row=r, column=col)
            if disc[flag] and cell.value in (None, ""):
                cell.value = "X"
                cell.alignment = Alignment(horizontal="center", vertical="center")
                filled += 1
    if filled:
        wb.save(checklist_path)
    return filled


def update_checklist_marks(db_path, checklist_path, numbers):
    """
    Makes the Burned and Verified boxes of just the given discs match the database: an X where the
    database says burned (verified), an empty box where it says not. Every other row is left exactly as
    it is, so anything ticked by hand elsewhere survives. Returns how many boxes changed.
    """
    wanted = {n for n in numbers if isinstance(n, int)}
    if not wanted or not os.path.exists(checklist_path):
        return 0
    discs = {d["disc_number"]: d for d in list_discs(db_path)}
    wb = load_workbook(checklist_path)
    ws = wb["Disc Checklist"] if "Disc Checklist" in wb.sheetnames else wb.worksheets[0]
    cols = _checklist_cols(ws)
    changed = 0
    for r in range(2, ws.max_row + 1):
        number = ws.cell(row=r, column=1).value
        if not isinstance(number, int) or number not in wanted or number not in discs:
            continue
        for key in ("burned", "verified"):
            if key not in cols:
                continue
            cell = ws.cell(row=r, column=cols[key])
            has = cell.value not in (None, "")
            if discs[number][key] and not has:
                cell.value = "X"
                cell.alignment = Alignment(horizontal="center", vertical="center")
                changed += 1
            elif not discs[number][key] and has:
                cell.value = None
                changed += 1
    if changed:
        wb.save(checklist_path)
    return changed


def checklist_differences(db_path, checklist_path):
    """
    Compares the database's burned/verified marks with the checklist's ticks, changing nothing.
    Returns a dict of disc-number lists: burned_ticked_not_marked, burned_marked_not_ticked,
    verified_ticked_not_marked, verified_marked_not_ticked.
    """
    burned_ticks, verified_ticks, listed = _checklist_ticks(checklist_path)
    discs = {d["disc_number"]: d for d in list_discs(db_path)}
    return {
        "burned_ticked_not_marked": [n for n in burned_ticks if n in discs and not discs[n]["burned"]],
        "burned_marked_not_ticked": sorted(n for n, d in discs.items()
                                           if d["burned"] and n in listed and n not in burned_ticks),
        "verified_ticked_not_marked": [n for n in verified_ticks if n in discs and not discs[n]["verified"]],
        "verified_marked_not_ticked": sorted(n for n, d in discs.items()
                                             if d["verified"] and n in listed and n not in verified_ticks),
    }


def import_burned_from_checklist(db_path, checklist_path):
    """
    Reads the hand-ticked "Burned" and "Verified" columns of the checklist (an .xlsx or .ods file)
    and marks those discs in the database. It adds marks but never removes any itself -- discs
    marked here but no longer ticked are reported ("unticked") for the caller to ask about.

    Returns {"marked": [newly burned], "already": [...], "not_in_database": [...],
             "verified_marked": [newly verified], "unticked": [burned here, not ticked],
             "verified_unticked": [verified here, not ticked]}.
    """
    burned_ticks, verified_ticks, listed = _checklist_ticks(checklist_path)
    discs = {d["disc_number"]: d for d in list_discs(db_path)}
    to_mark = [n for n in burned_ticks if n in discs and not discs[n]["burned"]]
    already = [n for n in burned_ticks if n in discs and discs[n]["burned"]]
    missing = [n for n in burned_ticks if n not in discs]
    verified_to_mark = [n for n in verified_ticks if n in discs and not discs[n]["verified"]]
    unticked = sorted(n for n, d in discs.items() if d["burned"] and n in listed and n not in burned_ticks)
    verified_unticked = sorted(n for n, d in discs.items()
                               if d["verified"] and n in listed and n not in verified_ticks)
    if to_mark:
        set_discs_burned(db_path, to_mark, True)
    if verified_to_mark:
        set_discs_verified(db_path, verified_to_mark, True)
    return {"marked": to_mark, "already": already, "not_in_database": missing,
            "verified_marked": verified_to_mark, "unticked": unticked,
            "verified_unticked": verified_unticked}


def backup_files(paths, backup_dir, keep=10):
    """
    Copies each existing file in `paths` into `backup_dir` with a date-and-time stamp in its name
    (archived_catalog_2026-10-29_203015.db), then trims each file's backups to the newest `keep`.
    Files that don't exist yet are skipped. Returns the list of backup files created.
    """
    import datetime
    import re
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    created = []
    for path in paths:
        if not path or not os.path.isfile(path):
            continue
        os.makedirs(backup_dir, exist_ok=True)
        stem, ext = os.path.splitext(os.path.basename(path))
        dest = os.path.join(backup_dir, f"{stem}_{stamp}{ext}")
        shutil.copy2(path, dest)
        created.append(dest)
        pattern = re.compile(rf"^{re.escape(stem)}_\d{{4}}-\d{{2}}-\d{{2}}_\d{{6}}{re.escape(ext)}$")
        olds = sorted(name for name in os.listdir(backup_dir) if pattern.match(name))
        for name in olds[:-keep] if keep > 0 else []:
            try:
                os.remove(os.path.join(backup_dir, name))
            except OSError:
                pass
    return created


# --------------------------------------------------------------------------------------------
# The DiscWise folder: one tidy home for the database(s), checklist(s) and backups
# --------------------------------------------------------------------------------------------
DISCWISE_FOLDER_NAME = "DiscWise"
LOOSE_FILE_NAMES = ("archived_catalog.db", "listening_library_archive.db",
                    "disc_checklist.xlsx", "disc_checklist.ods")
_BACKUP_NAME = re.compile(r"^(archived_catalog|listening_library_archive|disc_checklist)"
                          r"_\d{4}-\d{2}-\d{2}_\d{6}\.(db|xlsx|ods)$")


def discwise_home(any_music_folder):
    """
    The folder DiscWise keeps its own files in (databases, checklists, backups): a folder named
    "DiscWise" right beside the given music folder, e.g. ~/Music/FLAC Files -> ~/Music/DiscWise.
    """
    parent = os.path.dirname(os.path.abspath(os.path.expanduser(any_music_folder)))
    return os.path.join(parent, DISCWISE_FOLDER_NAME)


def find_loose_files(parent_dir):
    """
    DiscWise files sitting loose in `parent_dir` (where older versions put them) that belong in the
    DiscWise folder. Returns a list of (current_path, new_path). Files that would land on top of
    something already in the DiscWise folder are left out. Does not move anything.
    """
    parent_dir = os.path.abspath(parent_dir)
    if os.path.basename(parent_dir) == DISCWISE_FOLDER_NAME:
        return []
    home = os.path.join(parent_dir, DISCWISE_FOLDER_NAME)
    found = []
    for name in LOOSE_FILE_NAMES:
        old = os.path.join(parent_dir, name)
        new = os.path.join(home, name)
        if os.path.isfile(old) and not os.path.exists(new):
            found.append((old, new))
    old_backups = os.path.join(parent_dir, "backups")
    if os.path.isdir(old_backups):
        for name in sorted(os.listdir(old_backups)):
            new = os.path.join(home, "backups", name)
            if _BACKUP_NAME.match(name) and not os.path.exists(new):
                found.append((os.path.join(old_backups, name), new))
    return found


def move_loose_files(moves):
    """
    Carries out the (current_path, new_path) pairs from find_loose_files. Returns
    (moved, failed) where moved is the list of pairs that worked and failed is [(path, reason)].
    Empties out the old "backups" folder afterwards if nothing else is in it.
    """
    moved, failed = [], []
    for old, new in moves:
        try:
            os.makedirs(os.path.dirname(new), exist_ok=True)
            shutil.move(old, new)
            moved.append((old, new))
        except OSError as exc:
            failed.append((old, str(exc)))
    for old, _new in moved:
        folder = os.path.dirname(old)
        if os.path.basename(folder) == "backups":
            try:
                os.rmdir(folder)  # only succeeds if it's now empty
            except OSError:
                pass
    return moved, failed


def verify_disc(db_path, disc_number, mount_path, full_read=True, progress=None, cancelled=None):
    """
    Checks a burned disc (mounted at mount_path) against the plan for `disc_number`:
      - every album planned for that disc has its folder on the disc,
      - every track file the plan lists is there (no missing files),
      - each album isn't noticeably smaller than planned (a sign of a cut-off file),
      - and, with full_read=True, every file on those albums reads all the way through without error,
        which is what catches a bad burn or an unreadable spot. That slow read only happens if the
        quick checks above passed; otherwise it stops early and sets "read_skipped".
    Folders are matched by name wherever they sit on the disc, so it doesn't matter whether you
    burned them as Artist/Album or just Album. Extra files (cover art, a README) are fine.

    progress(bytes_done, bytes_total, filename) is called while reading; cancelled() can return True to stop.
    Returns {"ok": bool, "cancelled": bool, "albums": [{"label", "ok", "notes": [...]}],
             "other_on_disc": [top-level items that match no planned album], "problems": [str, ...]}.
    """
    from collections import Counter

    conn = sqlite3.connect(db_path)
    try:
        planned = conn.execute(
            "SELECT albums.id, albums.name, albums.folder_path, albums.size_mb, artists.name "
            "FROM albums JOIN discs ON albums.disc_id = discs.id JOIN artists ON albums.artist_id = artists.id "
            "WHERE discs.disc_number = ? ORDER BY artists.name, albums.name", (disc_number,)).fetchall()
        track_names = {album_id: [row[0] for row in
                                  conn.execute("SELECT filename FROM tracks WHERE album_id = ?", (album_id,))]
                       for album_id, *_ in planned}
    finally:
        conn.close()
    expected_files = {album_id: Counter(n.casefold() for n in names) for album_id, names in track_names.items()}
    original_case = {album_id: {n.casefold(): n for n in names} for album_id, names in track_names.items()}

    if not planned:
        return {"ok": False, "cancelled": False, "albums": [], "other_on_disc": [],
                "problems": [f"The database has no albums planned for Disc {disc_number}."]}
    if not os.path.isdir(mount_path):
        return {"ok": False, "cancelled": False, "albums": [], "other_on_disc": [],
                "problems": [f"Can't find the disc at {mount_path}."]}

    import unicodedata

    def norm(text):
        """Folder names compared without caring about case, accent encoding, stray spaces or a trailing dot."""
        text = unicodedata.normalize("NFC", str(text)).casefold()
        return " ".join(text.split()).rstrip(". ")

    mount_root = os.path.normpath(mount_path)
    dirs_by_name = {}
    agg = {}          # folder -> every file name in it and below it (for matching by contents)
    agg_bytes = {}    # folder -> total size of everything in it and below it
    for dirpath, dirnames, filenames in os.walk(mount_root):
        for d in dirnames:
            dirs_by_name.setdefault(norm(d), []).append(os.path.join(dirpath, d))
        here = Counter(unicodedata.normalize("NFC", fn).casefold() for fn in filenames)
        here_bytes = 0
        for fn in filenames:
            try:
                here_bytes += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
        walker = dirpath
        while True:
            agg.setdefault(walker, Counter()).update(here)
            agg_bytes[walker] = agg_bytes.get(walker, 0) + here_bytes
            parent = os.path.dirname(walker)
            if walker == mount_root or parent == walker:
                break
            walker = parent

    def files_under(folder):
        found = []
        for dirpath, _dirnames, filenames in os.walk(folder):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                found.append((full, fn, size))
        return found

    name_claimed = set()
    for _id, _name, _folder_path, _size, _artist in planned:
        for key in (norm(os.path.basename(_folder_path.rstrip("/\\"))), norm(_name)):
            name_claimed.update(dirs_by_name.get(key, []))

    def overlaps(path, others):
        """True if `path` is one of `others`, sits inside one, or contains one."""
        return any(path == c or path.startswith(c + os.sep) or c.startswith(path + os.sep) for c in others)

    results, problems, matched_dirs, to_read = [], [], [], []
    for album_id, name, folder_path, size_mb, artist in planned:
        label = f"{artist} / {name}"
        notes = []
        folder_name = os.path.basename(folder_path.rstrip("/\\"))
        wanted = [norm(folder_name), norm(name)]
        candidates = []
        for key in wanted:
            for path in dirs_by_name.get(key, []):
                if path not in candidates:
                    candidates.append(path)
        if not candidates:
            # No folder has the right name -- but it may be on the disc under another name. Look for the
            # folder whose files best match this album's track list.
            expected_total = sum(expected_files[album_id].values())
            best, best_key = None, None
            for path, have in agg.items():
                if path == mount_root or overlaps(path, matched_dirs) or overlaps(path, name_claimed):
                    continue
                # Besides the file names, the folder's size has to be about what the plan says, so albums
                # with generic track names ("Track 01") can't be mistaken for one another.
                found_mb_here = agg_bytes.get(path, 0) / (1024 * 1024)
                if abs(found_mb_here - size_mb) > max(5.0, 0.05 * size_mb):
                    continue
                hits = sum(min(count, have[unicodedata.normalize("NFC", fn)])
                           for fn, count in expected_files[album_id].items())
                if hits < max(1, (expected_total + 1) // 2):
                    continue
                key = (hits - 0.25 * (sum(have.values()) - hits), path.count(os.sep))   # best fit, then deepest
                if best_key is None or key > best_key:
                    best, best_key = path, key
            if best:
                candidates = [best]
                notes.append(f'found by its files, under the folder name "{os.path.basename(best)}"')
        if not candidates:
            looked = f'"{folder_name}"' if norm(folder_name) == norm(name) else f'"{folder_name}" or "{name}"'
            notes.append(f"album folder not found on the disc (looked for {looked})")
            results.append({"label": label, "ok": False, "notes": notes})
            problems.append(f"{label}: album folder not found on the disc (looked for {looked})")
            continue

        def score(path):
            have = Counter(fn.casefold() for _full, fn, _size in files_under(path))
            hits = sum(min(count, have[fn]) for fn, count in expected_files[album_id].items())
            parent_bonus = 0.5 if os.path.basename(os.path.dirname(path)).casefold() == artist.casefold() else 0
            return hits + parent_bonus
        unclaimed = [c for c in candidates if c not in matched_dirs]
        if unclaimed:
            candidates = unclaimed      # two albums can share a folder name (e.g. "Covers"); don't match one folder twice
        source_parent = norm(os.path.basename(os.path.dirname(folder_path.rstrip("/\\"))))

        def score_with_parent(path):
            bonus = 0.25 if norm(os.path.basename(os.path.dirname(path))) == source_parent else 0
            return score(path) + bonus
        folder = max(candidates, key=score_with_parent)
        matched_dirs.append(folder)

        on_disc = files_under(folder)
        have = Counter(fn.casefold() for _full, fn, _size in on_disc)
        missing = expected_files[album_id] - have
        ok = True
        if missing:
            ok = False
            names = sorted(original_case[album_id][k] for k in missing.elements())
            shown = ", ".join(names[:5]) + (f" ... and {len(names) - 5} more" if len(names) > 5 else "")
            notes.append(f"{len(names)} file(s) missing: {shown}")
            problems.append(f"{label}: {len(names)} file(s) missing ({shown})")
        found_mb = sum(size for _f, _n, size in on_disc) / (1024 * 1024)
        tolerance = max(2.0, 0.01 * size_mb)
        if found_mb < size_mb - tolerance:
            ok = False
            notes.append(f"only {found_mb:.0f} MB on the disc, plan says {size_mb:.0f} MB")
            problems.append(f"{label}: only {found_mb:.0f} MB on the disc, the plan says {size_mb:.0f} MB")
        to_read.extend((full, fn, size, label) for full, fn, size in on_disc)
        results.append({"label": label, "ok": ok, "notes": notes, "folder": folder})

    # The quick check above (folders, file names, sizes) is cheap. If it already found a problem there is
    # no point reading every file all the way through, so a thorough check stops here and reports it.
    read_skipped = bool(full_read and to_read and problems)
    was_cancelled = False
    if full_read and to_read and not problems:
        total = sum(size for _full, _fn, size, _label in to_read)
        done = 0
        unreadable = {}
        for full, fn, size, label in to_read:
            if cancelled and cancelled():
                was_cancelled = True
                break
            try:
                with open(full, "rb") as fh:
                    while True:
                        chunk = fh.read(4 * 1024 * 1024)
                        if not chunk:
                            break
                        done += len(chunk)
                        if progress:
                            progress(done, total, fn)
                        if cancelled and cancelled():
                            was_cancelled = True
                            break
                if was_cancelled:
                    break
            except OSError as exc:
                unreadable.setdefault(label, []).append(f"{fn} ({exc.strerror or exc})")
                done += size
        for entry in results:
            bad = unreadable.get(entry["label"])
            if bad:
                entry["ok"] = False
                entry["notes"].append(f"{len(bad)} file(s) couldn't be read: " + ", ".join(bad[:3])
                                      + (" ..." if len(bad) > 3 else ""))
                problems.append(f"{entry['label']}: {len(bad)} file(s) couldn't be read ({', '.join(bad[:3])})")

    other = []
    for item in sorted(os.listdir(mount_path)):
        full = os.path.join(mount_path, item)
        if not any(m == full or m.startswith(full + os.sep) for m in matched_dirs):
            other.append(item)

    ok = (not was_cancelled) and all(entry["ok"] for entry in results)
    return {"ok": ok, "cancelled": was_cancelled, "albums": results, "other_on_disc": other, "problems": problems,
            "read_skipped": read_skipped}


def natural_sort_key(text):
    """
    A sort key for names shown in lists: ignores upper/lower case and any leading spaces, quotes or other
    punctuation (so " Points on the Curve" files under P), and puts numbers in numeric order inside the
    text (so "Disc 2" comes before "Disc 10", and 4, 1984, 5150 read in that order).
    """
    folded = (text if text is not None else "").__str__().casefold()
    cleaned = re.sub(r"^[\W_]+", "", folded) or folded
    return [(0, int(part), "") if part.isdigit() else (1, 0, part)
            for part in re.split(r"(\d+)", cleaned) if part != ""]


JOLIET_NAME_LIMIT = 64


def _safe_name(name):
    """A folder name made safe for a path: slashes can't appear inside a single folder name."""
    cleaned = str(name).replace("/", "_").replace("\\", "_").strip()
    return cleaned or "_"


def assemble_disc(db_path, disc_number, waiting_root):
    """
    Gathers every album planned for `disc_number` into ONE folder, waiting_root/"Disc N", laid out as
    Artist/Album, so the whole disc can be dragged into the burning program in a single move.
    The albums are MOVED from where they sit now.

    It checks everything first and moves nothing unless every album can be placed: an album whose
    folder can't be found, or one that would land on top of a folder already there, is reported and
    the run stops. Running it again after a partial run is safe: an album already sitting in the
    right place is simply counted.

    Returns {"ok": bool, "disc_dir": str, "moved": int, "already": int, "total": int, "problems": [str]}.
    """
    import shutil

    conn = sqlite3.connect(db_path)
    try:
        has_disc = conn.execute("SELECT 1 FROM discs WHERE disc_number = ?", (disc_number,)).fetchone()
        planned = conn.execute(
            "SELECT albums.name, albums.folder_path, artists.name "
            "FROM albums JOIN discs ON albums.disc_id = discs.id JOIN artists ON albums.artist_id = artists.id "
            "WHERE discs.disc_number = ? ORDER BY artists.name, albums.name", (disc_number,)).fetchall()
    finally:
        conn.close()
    disc_dir = os.path.join(waiting_root, f"Disc {disc_number}")
    result = {"ok": False, "disc_dir": disc_dir, "moved": 0, "already": 0, "total": len(planned), "problems": []}
    if not has_disc or not planned:
        result["problems"].append(f"The database has no albums planned for Disc {disc_number}.")
        return result

    jobs, problems, seen_src, seen_dst = [], [], set(), set()
    for name, folder_path, artist in planned:
        src = os.path.normpath(folder_path)
        # The album folder keeps the exact name it has now (what the disc check looks for first).
        base = os.path.basename(src.rstrip("/\\")) or _safe_name(name)
        parent = os.path.basename(os.path.dirname(src.rstrip("/\\")))
        if (is_non_album_folder(base) and parent.strip() and parent.casefold() != str(artist).casefold()):
            # An art folder (Covers, Extra Art, Scans...) saved as its own item keeps the name of the folder
            # it sat in, so two "Covers" folders from different shows can't land in the same spot.
            dst = os.path.join(disc_dir, _safe_name(artist), parent, base)
        else:
            dst = os.path.join(disc_dir, _safe_name(artist), base)
        label = f"{artist} / {name}"
        if dst.casefold() in seen_dst:
            problems.append(f"{label}: another album on this disc would be placed in the same spot")
            continue
        seen_dst.add(dst.casefold())
        if os.path.isdir(dst) and not os.path.isdir(src):
            jobs.append((label, None, dst))      # already assembled on an earlier run
            continue
        if not os.path.isdir(src):
            problems.append(f"{label}: folder not found ({src})")
            continue
        if os.path.exists(dst):
            problems.append(f"{label}: there's already a folder in {os.path.basename(disc_dir)} with that name")
            continue
        if src in seen_src:
            problems.append(f"{label}: its folder is also used by another album on this disc ({src})")
            continue
        seen_src.add(src)
        jobs.append((label, src, dst))
    if problems:
        result["problems"] = problems
        return result

    moved_dsts = []
    for label, src, dst in jobs:
        if src is None:
            result["already"] += 1
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            moved_dsts.append(dst)
            result["moved"] += 1
        except OSError as exc:
            result["problems"].append(f"{label}: couldn't be moved ({exc.strerror or exc}). "
                                      f"Albums already moved are in {disc_dir}.")
            return result
    result["ok"] = True
    # Joliet (the extra file-name table that lets Windows read a disc) only allows names up to 64 characters
    # (103 with the burning program's long-name option). Linux doesn't need it, but say so up front.
    long_names = []
    for dirpath, dirnames, filenames in os.walk(disc_dir):
        for entry in dirnames + filenames:
            if len(entry) > JOLIET_NAME_LIMIT:
                long_names.append((os.path.relpath(os.path.join(dirpath, entry), disc_dir), len(entry)))
    result["long_names"] = sorted(long_names, key=lambda item: -item[1])
    return result


def finish_disc_folder(disc_dir, pending_root):
    """
    After a disc is burned and verified: moves each Artist folder's albums from `disc_dir` into
    pending_root (the "Need to Be Converted" folder), MERGING into an Artist folder that is already there
    (matched ignoring upper/lower case) instead of making a second one. An album that already exists on
    the other side is never overwritten: it stays in `disc_dir` and is reported. When everything has moved,
    the empty "Disc N" folder is removed.

    Returns {"ok": bool, "moved": int, "conflicts": [str], "removed_disc_dir": bool, "problems": [str]}.
    """
    import shutil

    result = {"ok": False, "moved": 0, "conflicts": [], "removed_disc_dir": False, "problems": []}
    if not os.path.isdir(disc_dir):
        result["problems"].append(f"Can't find {disc_dir}.")
        return result
    os.makedirs(pending_root, exist_ok=True)
    existing = {d.casefold(): d for d in os.listdir(pending_root) if os.path.isdir(os.path.join(pending_root, d))}
    for artist in sorted(os.listdir(disc_dir)):
        artist_src = os.path.join(disc_dir, artist)
        if not os.path.isdir(artist_src):
            continue
        target_name = existing.get(artist.casefold(), artist)
        artist_dst = os.path.join(pending_root, target_name)
        for album in sorted(os.listdir(artist_src)):
            album_src = os.path.join(artist_src, album)
            album_dst = os.path.join(artist_dst, album)
            if os.path.exists(album_dst):
                if os.path.isdir(album_src) and os.path.isdir(album_dst):
                    # e.g. a "Spring 1990" folder holding only art: fill in what's missing, never overwrite
                    for child in sorted(os.listdir(album_src)):
                        if os.path.exists(os.path.join(album_dst, child)):
                            result["conflicts"].append(f"{artist} / {album} / {child}")
                        else:
                            try:
                                shutil.move(os.path.join(album_src, child), os.path.join(album_dst, child))
                                result["moved"] += 1
                            except OSError as exc:
                                result["problems"].append(f"{artist} / {album} / {child}: couldn't be moved ({exc.strerror or exc})")
                    try:
                        os.rmdir(album_src)
                    except OSError:
                        pass
                else:
                    result["conflicts"].append(f"{artist} / {album}")
                continue
            try:
                os.makedirs(artist_dst, exist_ok=True)
                shutil.move(album_src, album_dst)
                existing.setdefault(artist.casefold(), target_name)
                result["moved"] += 1
            except OSError as exc:
                result["problems"].append(f"{artist} / {album}: couldn't be moved ({exc.strerror or exc})")
        try:
            os.rmdir(artist_src)        # only succeeds if everything in it moved
        except OSError:
            pass
    try:
        os.rmdir(disc_dir)
        result["removed_disc_dir"] = True
    except OSError:
        pass
    result["ok"] = not result["conflicts"] and not result["problems"] and result["removed_disc_dir"]
    return result


def write_sqlite_db(flac_discs, mp3_discs, db_out_path, append=False, start_disc_num=1, keep_through=None,
                     pending_items=None):
    """
    Builds a searchable SQLite database of the library: artists,
    albums (each tied to the disc it landed on), and individual
    tracks. Also creates a flat "library_view" joining everything
    together, so a search for an artist, album, or song title in a
    tool like DB Browser for SQLite pulls up every related row --
    including a song title that shows up under more than one artist
    (e.g. a cover version).

    With append=True and an existing database, new rows are added to
    it (disc numbers continuing from start_disc_num) instead of
    starting over -- for when older material has already been burned
    and deleted, so this run only sees what's left.

    With keep_through=N, discs 1..N are left untouched, but any
    existing disc beyond N is deleted first (tracks, albums, and the
    disc row itself) so it can be freshly rewritten -- for when
    nothing gets deleted from disk after burning, and a partially-full
    last disc should be freed up and repacked with new material rather
    than staying stuck half-empty.

    pending_items (optional): held-back units (label, size_mb, is_mp3,
    folder_path) that didn't make it onto a disc this run. These get
    indexed too, under a synthetic disc_number=0 / type="PENDING"
    bucket, so the whole scanned library is searchable -- not just
    what's been committed to a disc. The pending bucket is always
    fully cleared and rebuilt from this run's results (regardless of
    append/keep_through), since "not yet burned" status is never
    locked in the way a real disc is.

    Returns (unique_artists, total_albums) counted across the whole
    database (old + new combined), for the checklist's Summary sheet.
    """
    if not append and keep_through is None and os.path.exists(db_out_path):
        os.remove(db_out_path)

    conn = sqlite3.connect(db_out_path)
    cur = conn.cursor()

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS discs (
            id INTEGER PRIMARY KEY,
            disc_number INTEGER NOT NULL,
            type TEXT NOT NULL,
            burned INTEGER NOT NULL DEFAULT 0,
            burned_date TEXT,
            verified INTEGER NOT NULL DEFAULT 0,
            verified_date TEXT
        );
        CREATE TABLE IF NOT EXISTS artists (
            id INTEGER PRIMARY KEY,
            name TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS albums (
            id INTEGER PRIMARY KEY,
            artist_id INTEGER NOT NULL REFERENCES artists(id),
            disc_id INTEGER NOT NULL REFERENCES discs(id),
            name TEXT NOT NULL,
            folder_path TEXT NOT NULL,
            size_mb REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tracks (
            id INTEGER PRIMARY KEY,
            album_id INTEGER NOT NULL REFERENCES albums(id),
            track_number INTEGER,
            title TEXT NOT NULL,
            filename TEXT NOT NULL,
            file_type TEXT
        );
        DROP VIEW IF EXISTS library_view;
        DROP VIEW IF EXISTS album_view;
    """)

    # Databases written by older versions have no per-track file type. Add
    # the column and fill it in from each track's file name, so appending to
    # an existing packing-list database keeps working.
    ensure_burn_columns(conn)
    track_cols = {row[1] for row in cur.execute("PRAGMA table_info(tracks)")}
    if "file_type" not in track_cols:
        cur.execute("ALTER TABLE tracks ADD COLUMN file_type TEXT")
    for track_id, filename in cur.execute(
            "SELECT id, filename FROM tracks WHERE file_type IS NULL").fetchall():
        cur.execute("UPDATE tracks SET file_type = ? WHERE id = ?", (file_type_for(filename), track_id))

    if keep_through is not None:
        cur.execute(
            "DELETE FROM tracks WHERE album_id IN ("
            "  SELECT albums.id FROM albums JOIN discs ON albums.disc_id = discs.id "
            "  WHERE discs.disc_number > ?)", (keep_through,)
        )
        cur.execute(
            "DELETE FROM albums WHERE disc_id IN ("
            "  SELECT id FROM discs WHERE disc_number > ?)", (keep_through,)
        )
        cur.execute("DELETE FROM discs WHERE disc_number > ?", (keep_through,))

    # The pending bucket (not-yet-burned material) is never "locked in" --
    # always clear it and rebuild from this run's results.
    cur.execute(
        "DELETE FROM tracks WHERE album_id IN ("
        "  SELECT albums.id FROM albums JOIN discs ON albums.disc_id = discs.id "
        "  WHERE discs.disc_number = 0)"
    )
    cur.execute("DELETE FROM albums WHERE disc_id IN (SELECT id FROM discs WHERE disc_number = 0)")
    cur.execute("DELETE FROM discs WHERE disc_number = 0")

    artist_ids = {}

    def get_artist_id(name):
        if name not in artist_ids:
            cur.execute(
                "INSERT OR IGNORE INTO artists (name) VALUES (?)", (name,)
            )
            cur.execute("SELECT id FROM artists WHERE name = ?", (name,))
            artist_ids[name] = cur.fetchone()[0]
        return artist_ids[name]

    def insert_unit(disc_id, label, size_mb, is_mp3, folder_path):
        artist = artist_name_for_label(label, is_mp3)
        album_name = label.split("/")[-1]
        artist_id = get_artist_id(artist)

        cur.execute(
            "INSERT INTO albums (artist_id, disc_id, name, folder_path, size_mb) "
            "VALUES (?, ?, ?, ?, ?)",
            (artist_id, disc_id, album_name, folder_path, size_mb),
        )
        album_id = cur.lastrowid

        unit_tracks = get_tracks_for_unit(folder_path) or get_extra_files_for_unit(folder_path)
        for track_no, title, filename, file_type in unit_tracks:
            cur.execute(
                "INSERT INTO tracks (album_id, track_number, title, filename, file_type) "
                "VALUES (?, ?, ?, ?, ?)",
                (album_id, track_no, title, filename, file_type),
            )

    disc_number = start_disc_num
    for disc_type, discs in (("FLAC", flac_discs), ("MP3", mp3_discs)):
        for used_mb, items in discs:
            cur.execute(
                "INSERT INTO discs (disc_number, type) VALUES (?, ?)",
                (disc_number, disc_type),
            )
            disc_id = cur.lastrowid

            for label, size_mb, is_mp3, folder_path in items:
                insert_unit(disc_id, label, size_mb, is_mp3, folder_path)

            disc_number += 1

    if pending_items:
        cur.execute("INSERT INTO discs (disc_number, type) VALUES (0, 'PENDING')")
        pending_disc_id = cur.lastrowid
        for label, size_mb, is_mp3, folder_path in pending_items:
            insert_unit(pending_disc_id, label, size_mb, is_mp3, folder_path)

    cur.executescript("""
        CREATE VIEW library_view AS
        SELECT
            artists.name AS artist,
            albums.name AS album,
            discs.disc_number AS disc_number,
            discs.type AS disc_type,
            tracks.track_number,
            tracks.title AS song,
            tracks.file_type AS file_type,
            albums.folder_path
        FROM tracks
        JOIN albums ON tracks.album_id = albums.id
        JOIN artists ON albums.artist_id = artists.id
        JOIN discs ON albums.disc_id = discs.id;

        CREATE VIEW album_view AS
        SELECT
            artists.name AS artist,
            albums.name AS album,
            discs.disc_number AS disc_number,
            discs.type AS disc_type,
            albums.size_mb,
            albums.folder_path
        FROM albums
        JOIN artists ON albums.artist_id = artists.id
        JOIN discs ON albums.disc_id = discs.id;

        DROP VIEW IF EXISTS summary_counts;
        CREATE VIEW summary_counts AS
        SELECT
            (SELECT COUNT(*) FROM albums JOIN discs ON albums.disc_id = discs.id
                WHERE discs.disc_number > 0) AS total_albums,
            (SELECT COUNT(DISTINCT artist_id) FROM albums JOIN discs ON albums.disc_id = discs.id
                WHERE discs.disc_number > 0) AS unique_artists,
            (SELECT COUNT(*) FROM discs WHERE disc_number > 0) AS total_discs,
            (SELECT COUNT(*) FROM albums JOIN discs ON albums.disc_id = discs.id
                WHERE discs.disc_number = 0) AS pending_albums;
    """)

    # These two feed the checklist's Summary sheet, so they should reflect
    # only what's actually committed to a disc in this plan -- not the
    # pending/not-yet-burned material that's also now searchable.
    unique_artists = cur.execute(
        "SELECT COUNT(DISTINCT artist_id) FROM albums JOIN discs ON albums.disc_id = discs.id "
        "WHERE discs.disc_number > 0"
    ).fetchone()[0]
    total_albums = cur.execute(
        "SELECT COUNT(*) FROM albums JOIN discs ON albums.disc_id = discs.id WHERE discs.disc_number > 0"
    ).fetchone()[0]

    conn.commit()
    conn.close()
    print(f"Wrote searchable database: {db_out_path}")
    if not HAVE_MUTAGEN:
        print("  (song titles came from filenames -- run 'pip install mutagen --break-system-packages' "
              "and re-run this script to pull real tags instead)")

    return unique_artists, total_albums


def find_media_leaf_folders(root):
    """Every folder under `root` that directly contains at least one
    audio/video file, skipping folders inside excluded areas (YouTube
    background music etc.)."""
    root = os.path.abspath(root)
    leaves = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not is_excluded(d)]
        if any(f.lower().endswith(AUDIO_EXTENSIONS) for f in filenames):
            leaves.append(os.path.abspath(dirpath))
    return leaves


def _path_is_within(leaf, parents):
    """True if `leaf` is one of `parents` or sits inside one of them."""
    for parent in parents:
        parent = parent.rstrip(os.sep)
        if leaf == parent or leaf.startswith(parent + os.sep):
            return True
    return False


def verify_coverage(db_path, roots, held_back_paths=()):
    """
    Cross-checks the real folders on disk against the database.

    Every folder under `roots` that directly holds audio/video files must
    be accounted for either (a) inside an album committed to a disc in the
    database, or (b) deliberately held back for next time. Held-back means
    either the folder sits in `held_back_paths` (e.g. a "not yet burned"
    folder the leftovers were moved to), OR the database lists it as
    PENDING (disc 0) -- which is how held-back folders that were left in
    place get recorded. Anything else is genuinely unaccounted for.

    Returns a dict: total, covered, held_back, missing (list of paths).
    """
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT albums.folder_path, discs.disc_number FROM albums "
        "JOIN discs ON albums.disc_id = discs.id"
    ).fetchall()
    conn.close()

    album_paths = [os.path.abspath(p) for p, n in rows if n != 0]
    pending_paths = [os.path.abspath(p) for p, n in rows if n == 0]
    held = [os.path.abspath(p) for p in held_back_paths] + pending_paths
    return check_coverage(roots, album_paths, held)


def check_coverage(roots, album_paths, held_paths):
    """The comparison behind verify_coverage, working from lists of paths instead of a database, so a
    packing run can check its plan before anything is saved. Every folder under `roots` that directly
    holds audio must sit inside one of `album_paths` (planned on a disc) or `held_paths` (held back).
    Returns a dict: total, covered, held_back, missing (list of paths)."""
    album_paths = [os.path.abspath(p) for p in album_paths]
    held = [os.path.abspath(p) for p in held_paths]
    total = covered = held_count = 0
    missing = []
    for root in roots:
        for leaf in find_media_leaf_folders(root):
            total += 1
            if _path_is_within(leaf, album_paths):
                covered += 1
            elif _path_is_within(leaf, held):
                held_count += 1
            else:
                missing.append(leaf)

    return {"total": total, "covered": covered, "held_back": held_count, "missing": missing}


def write_library_index(roots, db_out_path, overrides=None, include_art_folders=False):
    """
    Independent, packing-free scan purely for search: walks every
    folder in `roots`, finds real album boundaries the same way the
    packing scan does, and indexes everything into db_out_path under a
    single disc_number=0 / type="LIBRARY" bucket -- there's no disc
    concept here, just a full catalog of what's in these folders.

    `overrides` is an optional {folder_path: forced_artist_name} map
    for folders already dedicated to a single artist (see
    find_album_units_flat) -- same idea as --force-artist on the
    packing side.

    Folders with no music in them at all (a concert's "Scans" folder of JPEGs, say) are left out
    unless include_art_folders is True -- they are worth keeping on a disc, but they only
    clutter a search for music.

    Always rebuilds from scratch (deletes any existing db_out_path
    first), so re-running after adding music keeps the catalog in
    sync with what's actually on disk.

    Returns (unique_artists, total_albums, total_tracks, skipped_picture_folders).
    """
    os.makedirs(os.path.dirname(os.path.abspath(db_out_path)), exist_ok=True)
    overrides = overrides or {}
    units = []
    for root in roots:
        root = os.path.abspath(os.path.expanduser(root))
        forced_artist = overrides.get(root)
        if forced_artist:
            units.extend(find_album_units_flat(root, DISC_CAPACITY_MB, forced_artist))
        else:
            units.extend(find_album_units(root, DISC_CAPACITY_MB))

    skipped = 0
    if not include_art_folders:
        music_units = [u for u in units if folder_has_music(u[3])]
        skipped = len(units) - len(music_units)
        units = music_units

    if os.path.exists(db_out_path):
        # Safety: the library catalog is rebuilt from scratch, so refuse to
        # touch a file that is actually a packing-list database (one with
        # real committed discs in it) -- that would destroy the burn history.
        try:
            check = sqlite3.connect(db_out_path)
            committed = check.execute(
                "SELECT COUNT(*) FROM discs WHERE disc_number > 0").fetchone()[0]
            check.close()
        except sqlite3.Error:
            committed = 0
        if committed:
            raise ValueError(
                f"{db_out_path} looks like a packing-list database ({committed} committed disc(s)). "
                "The library scan rebuilds its file from scratch, so it won't overwrite that. "
                "Pick a different file name for the library database."
            )
        os.remove(db_out_path)

    write_sqlite_db([], [], db_out_path, pending_items=units)

    # The library catalog has no discs at all; its one bucket is just "everything
    # scanned", so don't leave it labelled PENDING.
    relabel = sqlite3.connect(db_out_path)
    relabel.execute("UPDATE discs SET type = 'LIBRARY' WHERE disc_number = 0")
    relabel.commit()
    relabel.close()

    conn = sqlite3.connect(db_out_path)
    unique_artists = conn.execute(
        "SELECT COUNT(DISTINCT artist_id) FROM albums JOIN discs ON albums.disc_id = discs.id "
        "WHERE discs.disc_number = 0"
    ).fetchone()[0]
    total_albums = conn.execute(
        "SELECT COUNT(*) FROM albums JOIN discs ON albums.disc_id = discs.id WHERE discs.disc_number = 0"
    ).fetchone()[0]
    total_tracks = conn.execute(
        "SELECT COUNT(*) FROM tracks JOIN albums ON tracks.album_id = albums.id "
        "JOIN discs ON albums.disc_id = discs.id WHERE discs.disc_number = 0"
    ).fetchone()[0]
    conn.close()

    return unique_artists, total_albums, total_tracks, skipped


def _leading_the_alt(term):
    """When what was typed starts with "The " ("the beatles"), the same words without it ("beatles"),
    squashed the way names are compared -- so a band filed as "Beatles" is found by "The Beatles" too.
    Empty when there is no leading "The "."""
    match = re.match(r"^\W*the\s+(.+)$", term or "", re.IGNORECASE)
    return re.sub(r"[\W_]+", "", match.group(1).casefold()) if match else ""


def search_library(db_path, term, scope="All", always_label_discs=False):
    """
    Searches a catalog database (library catalog or packing-list database).

    scope: "All" (artist / album / song, plus an exact file type such as MP3 or FLAC, plus the disc
    number when the term is a number), "Artist",
    "Album", "Song", "File Type" (partial match) or "Disc #" (every track on that exact disc number,
    e.g. term "10" finds disc 10 only -- not discs 1, 100 or songs with a 10 in the name; an empty
    term lists every disc in disc order).

    Returns {"rows": [...], "has_discs": bool}. Each row is
    (artist, album, disc_label, file_type, track_number, song, folder_path, status).
    has_discs is True only when the database has real committed discs -- i.e. it
    is a packing-list database -- so the caller can hide the Disc # column for a
    plain library catalog. In a packing-list database:
      - disc_label is the disc number, or "-" for an album that isn't on any disc (held back);
      - status is "Burned + Verified", "Burned", or "Pending" -- Pending meaning not burned yet,
        whether it's held back or planned for a disc that hasn't been burned;
      - searching exactly "pending" (or "unburned" / "not burned") in the All scope finds
        everything that is still Pending.
    Pass always_label_discs=True to get disc labels even from a database that has
    no committed discs yet (used by the Search Discs window).
    """
    conn = sqlite3.connect(db_path)
    try:
        track_cols = {row[1] for row in conn.execute("PRAGMA table_info(tracks)")}
        # Read straight from the tables rather than the saved view, so a catalog built by an
        # older version still shows file types: use the stored value when there is one,
        # otherwise work it out from the file name's extension.
        ext_sql = "upper(replace(tracks.filename, rtrim(tracks.filename, replace(tracks.filename, '.', '')), ''))"
        file_type_expr = f"COALESCE(tracks.file_type, {ext_sql})" if "file_type" in track_cols else ext_sql
        has_discs = conn.execute(
            "SELECT COUNT(*) FROM discs WHERE disc_number > 0").fetchone()[0] > 0
        disc_cols = {row[1] for row in conn.execute("PRAGMA table_info(discs)")}
        burned_expr = "discs.burned" if "burned" in disc_cols else "NULL"
        verified_expr = "discs.verified" if "verified" in disc_cols else "0"

        file_type_sql = "file_type"
        select = (
            "SELECT artist, album, disc_number, file_type, track_number, song, folder_path, burned, verified FROM ("
            "SELECT artists.name AS artist, albums.name AS album, discs.disc_number AS disc_number, "
            f"{file_type_expr} AS file_type, tracks.track_number AS track_number, "
            f"{burned_expr} AS burned, {verified_expr} AS verified, "
            "tracks.title AS song, albums.folder_path AS folder_path "
            "FROM tracks JOIN albums ON tracks.album_id = albums.id "
            "JOIN artists ON albums.artist_id = artists.id "
            "JOIN discs ON albums.disc_id = discs.id) AS lv"
        )
        # Names match ignoring capitals and punctuation, so "ACDC", "AC-DC" and "AC/DC" all find the
        # same band (a typed term with no letters or digits at all is matched exactly as typed).
        conn.create_function("SQUASH", 1, lambda t: re.sub(r"[\W_]+", "", (t or "").casefold()))
        squashed = re.sub(r"[\W_]+", "", term.casefold())
        like = f"%{squashed or term}%"
        col = (lambda c: f"SQUASH({c})") if squashed else (lambda c: c)
        # "The Beatles" also finds a band filed as plain "Beatles" (names that start with the typed words
        # without the "The"). Typing "Beatles" already finds both, since it is part of "The Beatles".
        the_alt = _leading_the_alt(term) if squashed else ""

        def name_hit(column, relaxed=True):
            """SQL test for an artist or album name matching the typed words; adds one extra
            question mark for the parameter list when the leading-"The" form applies."""
            if the_alt and relaxed:
                return f"({col(column)} LIKE ? OR {col(column)} LIKE ?)", [like, f"{the_alt}%"]
            return f"{col(column)} LIKE ?", [like]
        if scope == "Artist":
            sql, args = name_hit("artist")
            query = select + f" WHERE {sql} ORDER BY artist, album, track_number"
            params = tuple(args)
        elif scope == "Album":
            sql, args = name_hit("album")
            query = select + f" WHERE {sql} ORDER BY artist, album, track_number"
            params = tuple(args)
        elif scope == "Song":
            query = select + f" WHERE {col('song')} LIKE ? ORDER BY song, artist"
            params = (like,)
        elif scope == "File Type":
            query = select + f" WHERE {file_type_sql} LIKE ? ORDER BY artist, album, track_number"
            params = (f"%{term}%",)
        elif scope == "Disc #":
            digits = re.sub(r"\D", "", term)      # lets "10", "Disc 10" or "#10" all work
            if not term.strip():
                # Nothing typed: list every disc, in disc order, however many there are.
                query = select + " WHERE disc_number > 0 ORDER BY disc_number, artist, album, track_number"
                params = ()
            else:
                query = select + " WHERE disc_number = ? ORDER BY artist, album, track_number"
                params = (int(digits) if digits else -1,)
        else:
            # An exact file type typed into "All" (mp3, flac, mp4...) finds those tracks too; it's
            # an exact match so short letters inside other words don't flood the results.
            artist_sql, artist_args = name_hit("artist")
            album_sql, album_args = name_hit("album", relaxed=False)   # only artists get the leading-The leniency here
            where = (f"{artist_sql} OR {album_sql} OR {col('song')} LIKE ? "
                     "OR file_type = ?")
            keyword = term.strip().lower()
            if has_discs and keyword in ("pending", "unburned", "not burned"):
                where += " OR disc_number = 0 OR burned = 0"
            params = artist_args + album_args + [like, term.strip().upper()]
            # A number (or "Disc 10" / "#10") also finds that disc, alongside anything with it in the name.
            disc_match = re.fullmatch(r"(?:disc\s*)?#?\s*(\d+)", term.strip(), re.IGNORECASE)
            if has_discs and disc_match and int(disc_match.group(1)) > 0:
                where += " OR disc_number = ?"
                params.append(int(disc_match.group(1)))
            query = select + f" WHERE {where} ORDER BY artist, album, track_number"
            params = tuple(params)

        raw_rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    def sort_text(text):
        """Alphabetical sort key: ignores upper/lower case and any leading spaces or punctuation,
        so " Tortoise" files under T and "abba" files under A instead of falling to the ends."""
        t = re.sub(r"^[\W_]+", "", (text or "").casefold())
        return t or (text or "").casefold()

    if scope == "Song":
        raw_rows = sorted(raw_rows, key=lambda r: (sort_text(r[5]), sort_text(r[0])))
    elif scope == "Disc #":
        raw_rows = sorted(raw_rows, key=lambda r: (r[2], sort_text(r[0]), sort_text(r[1]),
                                                   r[4] is None, r[4] or 0, sort_text(r[5])))
    else:
        raw_rows = sorted(raw_rows, key=lambda r: (sort_text(r[0]), sort_text(r[1]),
                                                   r[4] is None, r[4] or 0, sort_text(r[5])))

    rows = []
    for artist, album, disc_number, file_type, track_number, song, folder_path, burned, verified in raw_rows:
        labelled = has_discs or always_label_discs
        disc_label = ("-" if disc_number == 0 else disc_number) if labelled else ""
        if not labelled or burned is None:
            status = ""
        elif disc_number == 0:
            status = "Pending"
        elif verified:
            status = "Burned + Verified"
        else:
            status = "Burned" if burned else "Pending"
        rows.append((artist, album, disc_label, file_type, track_number, song, folder_path, status))
    return {"rows": rows, "has_discs": has_discs}


def sort_search_rows(rows):
    """Puts search rows (artist, album, disc, type, track, song, ...) in the usual alphabetical order:
    by artist, then album, then track -- ignoring upper/lower case and any leading spaces or
    punctuation, with tracks that have no number last."""
    def sort_text(text):
        t = re.sub(r"^[\W_]+", "", (text or "").casefold())
        return t or (text or "").casefold()
    return sorted(rows, key=lambda r: (sort_text(r[0]), sort_text(r[1]),
                                       r[4] is None, r[4] or 0, sort_text(r[5])))


def find_unlisted_pending(db_path, roots, skip_keys=(), term=None, scope="All"):
    """
    Finds music sitting in real folders on disk that counts as "pending" but may not be in the
    packing-list database at all (e.g. albums downloaded since the last Create Packing List run, or
    albums assembled into a "Disc N" folder in the Waiting to Burn folder).

    roots: folders to look through. Any album folder already known to the database is skipped,
    and so is any (artist, album) pair in skip_keys (lower-cased), so nothing is listed twice.

    term / scope: with no term, everything found is returned (the "pending" search). With a term,
    only music matching it is returned -- by artist, album or song depending on scope ("All",
    "Artist", "Album" or "Song") -- ignoring capitals and punctuation, so "ACDC" finds "AC-DC".

    Returns rows in the same shape search_library gives:
    (artist, album, disc_label, file_type, track_number, song, folder_path, status).
    The artist is the top folder under the root (so Tom Petty / Greatest Hits / Disc 1 is Tom Petty,
    album Greatest Hits, and Yes / Albums / Fragile is Yes, album Fragile); a folder named "Disc 23"
    right under the root (an assembled burn disc) is shown as disc 23.
    """
    known = []
    try:
        conn = sqlite3.connect(db_path)
        try:
            known = [os.path.abspath(p) for (p,) in conn.execute("SELECT folder_path FROM albums") if p]
        finally:
            conn.close()
    except sqlite3.Error:
        pass

    def squash(text):
        return re.sub(r"[\W_]+", "", (text or "").casefold())
    want = squash(term) if term else ""
    want_alt = _leading_the_alt(term) if term else ""       # "the beatles" also finds a folder called "Beatles"

    rows, seen = [], set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for leaf in find_media_leaf_folders(root):
            if leaf in seen or _path_is_within(leaf, known):
                continue
            seen.add(leaf)
            parts = [x for x in os.path.relpath(leaf, root).split(os.sep) if x != "."]
            # A folder called "Disc 23" right under the root is an assembled burn disc (Waiting to Burn):
            # it tells us the disc number, and the artist folder is the next one down.
            disc_label = "-"
            if len(parts) > 1:
                m = re.fullmatch(r"Disc\s+(\d+)", parts[0], re.IGNORECASE)
                if m:
                    disc_label = int(m.group(1))
                    parts = parts[1:]
            # The artist is the top folder (Tom Petty, Yes), whatever category folders (Albums, Live,
            # Side Projects) or disc parts sit below it. The album is the folder holding the music,
            # unless that's just "Disc 1" / "CD 2" -- then it's the folder above (Greatest Hits).
            artist = parts[0] if len(parts) > 1 else ""
            album = parts[-1]
            if looks_like_disc_part(album) and len(parts) > 1:
                album = parts[-2]
            if (artist.lower(), album.lower()) in skip_keys:
                continue
            tracks = get_tracks_for_unit(leaf)
            if want:
                folder_hit = ((scope in ("All", "Artist")
                               and (want in squash(artist) or (want_alt and squash(artist).startswith(want_alt))))
                              or (scope in ("All", "Album")
                                  and (want in squash(album)
                                       or (want_alt and scope == "Album" and squash(album).startswith(want_alt)))))
                if not folder_hit:
                    if scope not in ("All", "Song"):
                        continue
                    tracks = [t for t in tracks if want in squash(t[1])]
                    if not tracks:
                        continue
            for track_no, title, _fn, file_type in tracks:
                rows.append((artist, album, disc_label, file_type, track_no, title, leaf, "Pending"))
    return rows


def split_committed_discs(discs, capacity_mb, min_fill):
    """Splits packed discs into (committed, held_back) based on whether
    each disc is at least `min_fill` full (0.0-1.0) of capacity_mb."""
    threshold_mb = capacity_mb * min_fill
    committed = [d for d in discs if d[0] >= threshold_mb]
    held_back = [d for d in discs if d[0] < threshold_mb]
    return committed, held_back


def report_and_move_leftovers(flac_discs, mp3_discs, flac_held, mp3_held, min_fill, move_leftovers_to=None):
    """
    Prints held-back leftover material (discs that didn't meet
    min_fill). If move_leftovers_to is given, also moves those album
    folders there -- skipping (with a warning) any folder that also
    contains a separate committed album as a subfolder, so a committed
    album is never dragged along by accident.
    """
    held_discs = flac_held + mp3_held
    if not held_discs:
        print("\nNo leftover material below the fill threshold -- every disc here is ready to commit.")
        return

    held_items = [item for _, items in held_discs for item in items]
    held_total_mb = sum(used_mb for used_mb, _ in held_discs)
    print(f"\n=== HELD BACK -- below {min_fill*100:.0f}% full, not added to the checklist/database "
          f"({held_total_mb:.0f}MB) ===")
    for item in held_items:
        print(f"   - {item[0]}  ({item[1]:.0f}MB)")

    if move_leftovers_to:
        dest_root = os.path.abspath(os.path.expanduser(move_leftovers_to))
        os.makedirs(dest_root, exist_ok=True)

        committed_paths = [
            os.path.abspath(item[3])
            for _, items in (flac_discs + mp3_discs) for item in items
        ]

        print(f"\nMoving held-back folders to {dest_root} ...")
        for label, size_mb, is_mp3, real_path in held_items:
            real_path = os.path.abspath(real_path)
            blocked = any(
                cp != real_path and cp.startswith(real_path + os.sep)
                for cp in committed_paths
            )
            if blocked:
                print(f"  SKIPPED (also contains a separate committed album -- move manually): {label}")
                continue

            dest_path = os.path.join(dest_root, label)
            if os.path.exists(dest_path):
                print(f"  SKIPPED (already exists at destination): {label}")
                continue

            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            shutil.move(real_path, dest_path)
            print(f"  Moved: {label}")

        print("\nDone. Anything left in the original folder(s) is exactly what got committed to "
              "a disc -- safe to convert to MP3 and delete. The moved folders will be picked up "
              "again next time you point this script at that leftover folder.")
    else:
        print("Do NOT delete these files yet -- leave them where they are. They'll be picked up "
              "again (combined with anything new you add) the next time you run this.")


def main():
    parser = argparse.ArgumentParser(description="Disc packing planner for a music library.")
    parser.add_argument("roots", nargs="+",
                         help="One or more folders to scan and pack together onto the same set of discs")
    parser.add_argument("--xlsx", help="Output path for the checklist spreadsheet")
    parser.add_argument("--db", help="Output path for the SQLite database")
    parser.add_argument("--force-artist", action="append", default=[], metavar="PATH=Artist",
                         help="For a folder already dedicated to one artist, with its immediate "
                              "children being the album/show folders directly (no artist-level "
                              "subfolder inside it). Repeatable.")
    parser.add_argument("--force-artist-nested", action="append", default=[], metavar="PATH=Artist",
                         help="Like --force-artist, but for a folder with one extra layer of "
                              "grouping folders in between (e.g. root -> Live/EPs/Studio -> the "
                              "actual release folders). Repeatable.")
    parser.add_argument("--append", action="store_true",
                         help="Add these results to the existing checklist/database instead of "
                              "starting over, continuing the disc numbering from where it left off. "
                              "Use this once older material has already been burned and deleted.")
    parser.add_argument("--start-disc", type=int, default=None,
                         help="Force the first new disc number explicitly, overriding what --append "
                              "would otherwise detect from the existing database.")
    parser.add_argument("--keep-burned", type=int, default=None, metavar="N",
                         help="For when you DON'T delete files after burning. Treat discs 1..N in the "
                              "existing checklist/database as already burned and untouchable; whatever "
                              "was on any disc after N (e.g. a partially-full last disc) is freed up and "
                              "freshly repacked together with any newly added material, starting at "
                              "Disc N+1. This avoids ending up with a mostly-empty 'last disc' every time.")
    parser.add_argument("--disc-type", choices=["dvd", "dvd-dl", "bd", "bd-dl"], default="dvd",
                         help="How much your blank discs hold: dvd = 4.7GB (default), dvd-dl = 8.5GB dual layer, "
                              "bd = 25GB Blu-ray, bd-dl = 50GB dual layer Blu-ray.")
    parser.add_argument("--min-fill", type=float, default=0.95, metavar="FRACTION",
                         help="Only commit a disc to the checklist/database if it's at least this full "
                              "(0.0-1.0, default 0.95). Anything less is held back and printed "
                              "separately instead -- don't delete those files, they'll be picked up "
                              "again (along with whatever you add in the meantime) next run.")
    parser.add_argument("--move-leftovers-to", metavar="PATH", default=None,
                         help="Move held-back album folders into this folder (preserving their "
                              "artist/album structure) instead of just printing them. Afterwards, "
                              "everything left in the original folder is exactly what got committed "
                              "-- safe to convert to MP3 and delete. Folders that mix loose audio "
                              "files with separate committed album subfolders are skipped with a "
                              "warning rather than risking moving a committed album by mistake.")
    args = parser.parse_args()
    set_media_type({"dvd": "4.7 GB  (DVD-R or DVD+R)", "dvd-dl": "8.5 GB  (DVD-R or DVD+R dual layer)",
                    "bd": "25 GB  (Blu-ray BD-R)", "bd-dl": "50 GB  (Blu-ray BD-R dual layer)"}[args.disc_type])

    force_artist_map = {}
    force_artist_nested_map = {}
    for spec in args.force_artist:
        if "=" not in spec:
            print(f"Ignoring malformed --force-artist value (expected PATH=Artist): {spec}")
            continue
        path_part, artist_part = spec.split("=", 1)
        force_artist_map[os.path.abspath(os.path.expanduser(path_part))] = artist_part
    for spec in args.force_artist_nested:
        if "=" not in spec:
            print(f"Ignoring malformed --force-artist-nested value (expected PATH=Artist): {spec}")
            continue
        path_part, artist_part = spec.split("=", 1)
        force_artist_nested_map[os.path.abspath(os.path.expanduser(path_part))] = artist_part

    first_root_abs = os.path.abspath(os.path.expanduser(args.roots[0]))
    home = discwise_home(first_root_abs)
    xlsx_out = args.xlsx or os.path.join(home, "disc_checklist.xlsx")
    db_out = args.db or os.path.join(home, "archived_catalog.db")

    print(f"Disc capacity assumed: {DISC_CAPACITY_MB}MB usable per disc\n")

    units = []
    for root_arg in args.roots:
        root = os.path.abspath(os.path.expanduser(root_arg))
        print(f"Scanning: {root}")
        if root in force_artist_map:
            artist = force_artist_map[root]
            print(f"  (treating as a single-artist folder: {artist})")
            units.extend(find_album_units_flat(root, DISC_CAPACITY_MB, artist))
        elif root in force_artist_nested_map:
            artist = force_artist_nested_map[root]
            print(f"  (treating as a single-artist folder with a grouping layer: {artist})")
            units.extend(find_album_units_categorized(root, DISC_CAPACITY_MB, artist))
        else:
            units.extend(find_album_units(root, DISC_CAPACITY_MB))

    if not units:
        print("No album folders with audio found. Check the path(s) and folder structure.")
        return

    locked_paths = set()
    if args.keep_burned is not None:
        if not os.path.exists(db_out):
            print(f"ERROR: --keep-burned needs an existing database at {db_out}")
            sys.exit(1)
        conn = sqlite3.connect(db_out)
        locked_paths = {
            os.path.abspath(row[0]) for row in conn.execute(
                "SELECT albums.folder_path FROM albums "
                "JOIN discs ON albums.disc_id = discs.id "
                "WHERE discs.disc_number <= ?", (args.keep_burned,)
            )
        }
        conn.close()
        before = len(units)
        units = [u for u in units if os.path.abspath(u[3]) not in locked_paths]
        print(f"\n--keep-burned {args.keep_burned}: locking in {before - len(units)} already-placed album(s); "
              f"{len(units)} album(s) remain to (re)pack")

    flac_units = [u for u in units if not u[2]]
    mp3_units = [u for u in units if u[2]]

    total_flac_mb = sum(u[1] for u in flac_units)
    total_mp3_mb = sum(u[1] for u in mp3_units)

    print(f"\nFound {len(flac_units)} lossless album/disc units, totaling {total_flac_mb/1024:.2f}GB")
    print(f"Found {len(mp3_units)} MP3 album units, totaling {total_mp3_mb/1024:.2f}GB")

    flac_discs = pack_discs(flac_units, DISC_CAPACITY_MB)
    mp3_discs = pack_discs(mp3_units, DISC_CAPACITY_MB) if mp3_units else []

    flac_discs, flac_held = split_committed_discs(flac_discs, DISC_CAPACITY_MB, args.min_fill)
    mp3_discs, mp3_held = split_committed_discs(mp3_discs, DISC_CAPACITY_MB, args.min_fill)

    start_disc_num = 1
    if args.keep_burned is not None:
        start_disc_num = args.keep_burned + 1
    elif args.start_disc is not None:
        start_disc_num = args.start_disc
    elif args.append and os.path.exists(db_out):
        try:
            conn = sqlite3.connect(db_out)
            row = conn.execute("SELECT MAX(disc_number) FROM discs WHERE disc_number > 0").fetchone()
            conn.close()
            if row and row[0]:
                start_disc_num = row[0] + 1
        except sqlite3.Error:
            pass
    if args.append:
        print(f"\nAppending -- new discs will start at Disc {start_disc_num}")

    print_disc_plan(flac_discs, DISC_CAPACITY_MB, "LOSSLESS (FLAC) DISCS", start_disc_num)
    if mp3_discs:
        print_disc_plan(mp3_discs, DISC_CAPACITY_MB, "MP3 DISCS", start_disc_num + len(flac_discs))

    total_discs = len(flac_discs) + len(mp3_discs)
    print(f"\n=== TOTAL: {total_discs} new disc(s) needed ({len(flac_discs)} FLAC + {len(mp3_discs)} MP3) ===")
    print(f"Suggest buying at least {total_discs + 2} to have a couple spares for burn errors/new additions.")

    report_and_move_leftovers(flac_discs, mp3_discs, flac_held, mp3_held, args.min_fill, args.move_leftovers_to)

    # Anything still sitting at its scanned path (i.e. not physically moved
    # away by --move-leftovers-to) gets indexed too, so the whole scanned
    # library is searchable, not just what's committed to a disc this run.
    pending_items = [item for _, items in (flac_held + mp3_held) for item in items
                      if os.path.isdir(item[3])]

    for out_path in (db_out, xlsx_out):
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    unique_artists, total_albums = write_sqlite_db(
        flac_discs, mp3_discs, db_out, append=args.append, start_disc_num=start_disc_num,
        keep_through=args.keep_burned, pending_items=pending_items
    )
    write_checklist_xlsx(flac_discs, mp3_discs, DISC_CAPACITY_MB, xlsx_out, unique_artists, total_albums,
                          append=args.append, start_disc_num=start_disc_num, keep_through=args.keep_burned)
    if args.keep_burned is not None:
        sync_checklist_ticks(db_out, xlsx_out)


if __name__ == "__main__":
    main()
