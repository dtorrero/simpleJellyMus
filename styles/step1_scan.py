#!/usr/bin/env python3
"""Step 1 - build the track list straight from the music files.

Reads every audio file under ``music_root`` with ``ffprobe`` (metadata only,
never the whole file) and writes one row per song into the dataset, together
with the *verbatim* genre tag and every other interesting tag.

This step never writes to a music file and never talks to Jellyfin; it is
read-only with respect to everything except its own database.

    python3 step1_scan.py                  # full scan (resumable)
    python3 step1_scan.py --limit 500      # first 500 files, for a quick test
    python3 step1_scan.py --rescan         # re-read files even if unchanged
    python3 step1_scan.py --prune          # drop rows whose file disappeared
    python3 step1_scan.py --dry-run        # only report what would be done
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import styles_db as S  # noqa: E402

# Tag keys that carry no information for the dataset but do carry megabytes.
SKIP_TAG_PARTS = ("lyrics", "unsynced", "cover", "picture", "image", "apic")

# Tags that are so long they are never useful in full.
TRUNCATE_TAGS = ("comment", "description", "summary")

YEAR_IN_NAME = re.compile(r"(?:^|[^\d])((?:19|20)\d{2})(?:[^\d]|$)")
YEAR_PARENS = re.compile(r"[([]\s*((?:19|20)\d{2})\s*[)\]]")
TRACK_NO = re.compile(r"^\s*(\d+)")


# --------------------------------------------------------------------------- #
# folder names are a second, independent source of metadata
# --------------------------------------------------------------------------- #

def folder_hints(folder: str) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """Guess (artist, album, year) from an album folder name.

    The library uses names such as ``Kamelot - The Awakening (2023)``,
    ``Accept`` or ``Aenigmatum (USA) - Discography``. These hints are only used
    when the file tags are missing, and they are stored separately so nothing
    is ever overwritten by a guess.
    """
    name = (folder or "").strip()
    if not name:
        return None, None, None
    year: Optional[int] = None
    match = YEAR_PARENS.search(name)
    if match:
        year = int(match.group(1))
    else:
        match = YEAR_IN_NAME.search(name)
        if match:
            year = int(match.group(1))
    parts = [part.strip() for part in name.split(" - ", 1)]
    if len(parts) == 2 and parts[0]:
        artist, album = parts[0], parts[1]
    else:
        artist, album = None, name
    if album:
        album = re.sub(r"\s*[([]\s*(?:19|20)\d{2}\s*[)\]]\s*$", "", album).strip(" -_")
        album = re.sub(r"^\(?(?:19|20)\d{2}\)?\s*[-.]\s*", "", album).strip(" -_")
        album = re.sub(r"\s*[([](?:lossless|flac|mp3|320|ep|single)[)\]]\s*$", "", album,
                       flags=re.IGNORECASE).strip(" -_") or album
    return (artist or None), (album or None), year


# --------------------------------------------------------------------------- #
# ffprobe
# --------------------------------------------------------------------------- #

def _clean(value: Any) -> str:
    """Trim, collapse whitespace and drop surrounding decoration."""
    text = " ".join(str(value or "").split())
    text = text.strip("\ufeff \t\r\n")
    text = text.strip(" -_\u2013\u2014") if len(text) > 2 else text
    return text.strip()


def parse_year(value: Any) -> Optional[int]:
    text = _clean(value)
    match = re.search(r"((?:19|20)\d{2})", text)
    if not match:
        return None
    year = int(match.group(1))
    return year if 1900 <= year <= 2100 else None


def parse_number(value: Any) -> Optional[int]:
    match = TRACK_NO.match(_clean(value))
    return int(match.group(1)) if match else None


def parse_duration_ms(value: Any) -> Optional[int]:
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return int(round(seconds * 1000.0))


def probe(path: Path, ffprobe: str, timeout: float) -> Optional[Dict[str, Any]]:
    """Run ffprobe on one file and return its parsed JSON (or ``None``)."""
    try:
        completed = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
            capture_output=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not completed.stdout:
        return None
    try:
        return json.loads(completed.stdout.decode("utf-8", "replace"))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# turning one ffprobe answer into one track row
# --------------------------------------------------------------------------- #

DEDICATED_TAGS = {
    "title", "album", "artist", "artists", "album_artist", "albumartist",
    "genre", "date", "year", "originaldate", "track", "disc",
    "tracktotal", "disctotal", "totaltracks", "totaldiscs",
}


def extract_tags(data: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, str]:
    """Return a lower-cased view of the file tags (original keys in tags_json)."""
    raw = (data.get("format") or {}).get("tags") or {}
    tags: Dict[str, str] = {}
    for key, value in raw.items():
        if value is None:
            continue
        tags[str(key).lower()] = str(value)
    return tags


def extra_tags(tags: Dict[str, str], config: Dict[str, Any]) -> str:
    """All tags that are not already a column, as compact JSON (or ``''``)."""
    skip_parts = [part.lower() for part in S.setting(config, "scan.skip_keys", [])] or list(SKIP_TAG_PARTS)
    limit = int(S.setting(config, "scan.tag_value_limit", 400) or 400)
    extra: Dict[str, str] = {}
    for key, value in tags.items():
        if key in DEDICATED_TAGS or any(part in key for part in skip_parts):
            continue
        text = " ".join(value.split())
        if not text:
            continue
        cap = min(limit, 120) if any(part in key for part in TRUNCATE_TAGS) else limit
        extra[key] = text[:cap]
    return json.dumps(extra, ensure_ascii=False, sort_keys=True) if extra else ""


def count_genre_parts(raw_genre: str) -> int:
    """How many styles a raw genre string seems to contain."""
    parts = re.split(r"[/|,;&+]|\s+and\s+", raw_genre or "")
    return len([part for part in parts if part.strip()]) or (1 if (raw_genre or "").strip() else 0)


def build_row(rel_path: str, size: int, mtime: float, data: Dict[str, Any],
              config: Dict[str, Any]) -> Dict[str, Any]:
    """One complete ``tracks`` row from one file."""
    tags = extract_tags(data, config)
    info = data.get("format") or {}

    title = _clean(tags.get("title"))
    album = _clean(tags.get("album"))
    album_artist = _clean(tags.get("album_artist") or tags.get("albumartist"))
    artists = _clean(tags.get("artists") or tags.get("artist"))
    raw_genre = _clean(tags.get("genre"))
    year = parse_year(tags.get("date") or tags.get("year") or tags.get("originaldate"))

    folder = rel_path.split("/", 1)[0] if "/" in rel_path else ""
    dir_artist, dir_album, dir_year = folder_hints(folder)

    # Fallbacks, in order of trust: file tag -> folder name -> nothing.
    if not album:
        album = dir_album
    if not album_artist:
        album_artist = artists or dir_artist or dir_album or ""
    if not year:
        year = dir_year
    if not title:
        title = Path(rel_path).stem

    container = str(info.get("format_name") or "").split(",")[0].strip().lower()
    bitrate = info.get("bit_rate")
    try:
        bitrate = int(str(bitrate)) if bitrate else None
    except (TypeError, ValueError):
        bitrate = None

    artist_key = S.norm_key(album_artist)
    album_key = S.norm_key(f"{album_artist} :: {album}") if (album_artist and album) else ""

    return {
        "rel_path": rel_path,
        "dir_artist": dir_artist,
        "dir_album": dir_album,
        "dir_year": dir_year,
        "file_size": size,
        "file_mtime": mtime,
        "name": title,
        "album": album,
        "album_artist": album_artist or None,
        "artists": artists or None,
        "year": year,
        "track_no": parse_number(tags.get("track")),
        "disc_no": parse_number(tags.get("disc")),
        "duration_ms": parse_duration_ms(info.get("duration")),
        "container": container or None,
        "bitrate": bitrate,
        "raw_genre": raw_genre,
        "tags_json": extra_tags(tags, config),
        "raw_genre_count": count_genre_parts(raw_genre),
        "artist_key": artist_key,
        "album_key": album_key,
    }


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #

SCAN_COLUMNS = (
    "rel_path", "dir_artist", "dir_album", "dir_year", "file_size", "file_mtime",
    "name", "album", "album_artist", "artists", "year", "track_no", "disc_no",
    "duration_ms", "container", "bitrate", "raw_genre", "tags_json",
    "raw_genre_count", "artist_key", "album_key",
)


def upsert_tracks(connection, rows: Sequence[Dict[str, Any]]) -> int:
    """Insert or refresh the scanned columns of every row (identity kept)."""
    if not rows:
        return 0
    columns = list(SCAN_COLUMNS) + ["scanned_at"]
    marks = ",".join("?" * len(columns))
    updates = ",".join(f"{name}=excluded.{name}" for name in SCAN_COLUMNS if name != "rel_path")
    sql = (
        f"INSERT INTO tracks({','.join(columns)}) VALUES({marks})"
        f" ON CONFLICT(rel_path) DO UPDATE SET {updates}"
    )
    stamp = S.now()
    payload = [tuple(row.get(name) for name in SCAN_COLUMNS) + (stamp,) for row in rows]
    connection.executemany(sql, payload)
    return len(payload)


def refresh_raw_genres(connection) -> int:
    """Keep ``raw_genres`` in step with the library (step 4 does the mapping)."""
    connection.execute(
        "INSERT INTO raw_genres(raw, tracks, decision, updated_at)"
        " SELECT raw_genre, COUNT(*), 'unresolved', ? FROM tracks"
        " WHERE TRIM(COALESCE(raw_genre, '')) <> '' GROUP BY raw_genre"
        " ON CONFLICT(raw) DO UPDATE SET tracks=excluded.tracks, updated_at=excluded.updated_at",
        (S.now(),),
    )
    return int(connection.execute("SELECT COUNT(*) AS n FROM raw_genres").fetchone()["n"])


def prune(connection, prefix: str, seen: Sequence[str]) -> int:
    """Delete rows of tracks whose file is gone (only under *prefix*)."""
    connection.execute("DROP TABLE IF EXISTS temp._seen")
    connection.execute("CREATE TEMP TABLE _seen(rel_path TEXT PRIMARY KEY)")
    connection.executemany("INSERT OR IGNORE INTO _seen(rel_path) VALUES(?)",
                           [(path,) for path in seen])
    like = f"{prefix}%" if prefix else "%"
    cursor = connection.execute(
        "DELETE FROM tracks WHERE rel_path LIKE ? AND rel_path NOT IN (SELECT rel_path FROM _seen)",
        (like,),
    )
    connection.execute("DROP TABLE temp._seen")
    return cursor.rowcount or 0


def collect_files(root: Path, extensions: Sequence[str]) -> List[Tuple[str, int, float]]:
    """Every audio file under *root* as ``(relative path, size, mtime)``."""
    wanted = {ext.lower().lstrip(".") for ext in extensions}
    found: List[Tuple[str, int, float]] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if not name.startswith("."))
        for name in sorted(files):
            if name.startswith("."):
                continue
            extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if extension not in wanted:
                continue
            full = Path(current) / name
            try:
                info = full.stat()
            except OSError:
                continue
            rel = os.path.relpath(str(full), str(root)).replace(os.sep, "/")
            found.append((rel, int(info.st_size), float(info.st_mtime)))
    return found


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Step 1 - read the music files and build the track list",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--root", default="", help="music root (default: config.json)")
    parser.add_argument("--db", default="", help="dataset path (default: config.json)")
    parser.add_argument("--limit", type=int, default=0, help="only the first N files")
    parser.add_argument("--workers", type=int, default=0, help="parallel ffprobe processes")
    parser.add_argument("--batch", type=int, default=250, help="rows per commit")
    parser.add_argument("--rescan", action="store_true",
                        help="read files again even when size/mtime are unchanged")
    parser.add_argument("--prune", action="store_true",
                        help="delete rows whose file is no longer there")
    parser.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    config = S.load_config()

    root = S.resolve(args.root) if args.root else S.resolve(config.get("music_root"))
    if not root.exists():
        S.log("music root does not exist:", str(root))
        return 1
    extensions = S.setting(config, "scan.extensions", ["mp3", "flac"])
    workers = args.workers or int(S.setting(config, "scan.workers", 16) or 16)
    ffprobe = str(S.setting(config, "scan.ffprobe", "ffprobe"))
    timeout = float(S.setting(config, "scan.ffprobe_timeout", 90) or 90)
    path = S.resolve(args.db) if args.db else S.db_path(config)

    S.log("music root:", str(root))
    S.log("dataset:   ", str(path))
    S.log("collecting files…")
    entries = collect_files(root, extensions)
    S.log(f"found {S.human(len(entries))} audio files")

    connection = S.connect(path)
    try:
        known: Dict[str, Tuple[int, float]] = {}
        if not args.rescan:
            for row in connection.execute("SELECT rel_path, file_size, file_mtime FROM tracks"):
                known[row["rel_path"]] = (row["file_size"] or 0, row["file_mtime"] or 0.0)
        todo = [entry for entry in entries
                if args.rescan or known.get(entry[0]) != (entry[1], entry[2])]
        skipped = len(entries) - len(todo)
        if args.limit:
            todo = todo[:args.limit]
        S.log(f"to read: {S.human(len(todo))}  (unchanged, skipped: {S.human(skipped)})")
        if not todo:
            S.log("nothing to do - use --rescan to read everything again")
        if args.dry_run:
            S.log("dry run: no file was read and nothing was written")
            return 0

        progress = S.Progress(len(todo), label="scanning")
        batch: List[Dict[str, Any]] = []
        failed: List[str] = []
        written = 0

        def work(entry: Tuple[str, int, float]):
            rel, size, mtime = entry
            data = probe(root / rel, ffprobe, timeout)
            if data is None:
                return rel, None
            return rel, build_row(rel, size, mtime, data, config)

        try:
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                for rel, row in pool.map(work, todo):
                    progress.step()
                    if row is None:
                        failed.append(rel)
                        continue
                    batch.append(row)
                    if len(batch) >= max(1, args.batch):
                        written += upsert_tracks(connection, batch)
                        connection.commit()
                        batch = []
        except KeyboardInterrupt:
            S.log("interrupted - saving what was read so far")
        progress.finish()
        if batch:
            written += upsert_tracks(connection, batch)
            connection.commit()

        S.log(f"rows written: {S.human(written)}  failed: {S.human(len(failed))}")
        if failed:
            listing = S.out_dir(config) / "scan_failed.txt"
            listing.write_text("\n".join(failed), encoding="utf-8")
            S.log("unreadable files listed in", str(listing))
        if args.prune:
            removed = prune(connection, "", [entry[0] for entry in entries])
            connection.commit()
            S.log(f"pruned rows (file gone): {S.human(removed)}")

        S.log("updating the raw genre list…")
        distinct = refresh_raw_genres(connection)
        connection.commit()
        S.log(f"distinct raw genre strings: {S.human(distinct)}")
        artists = S.rebuild_artist_rollup(connection)
        connection.commit()
        S.log(f"artists seen: {S.human(artists)}")
        rows = connection.execute(
            "SELECT raw_genre, COUNT(*) AS n FROM tracks"
            " WHERE TRIM(COALESCE(raw_genre, '')) <> '' GROUP BY raw_genre"
            " ORDER BY n DESC LIMIT 15")
        S.log("most common raw genres:")
        for row in rows:
            print(f"      {S.human(row['n']):>7}  {row['raw_genre']!r}")
        missing = connection.execute(
            "SELECT COUNT(*) AS n FROM tracks WHERE TRIM(COALESCE(raw_genre, '')) = ''"
        ).fetchone()["n"]
        S.log(f"tracks without any genre tag: {S.human(missing)}")
        S.meta_set(connection, "last_scan", S.now())
        connection.commit()
        S.print_status(connection)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
