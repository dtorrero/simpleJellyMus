#!/usr/bin/env python3
"""Local files dropped on the player: expansion, metadata and cover art.

SimpleJellyMus normally plays random audio from Jellyfin. This module is the
*other* way in: files, folders or playlists dragged from the file manager are
expanded into a plain list of audio files and turned into items that carry the
same keys as the ones the Jellyfin client produces - so the queue, the cover
art, the song information, the progress bar and the transport all keep working
without knowing where a track came from.

Like the rest of the player it is standard library only. Two extras are used
when they happen to be installed and are silently skipped otherwise:

    * mutagen (``python-mutagen``) - the real title/artist/album tags, the track
      number, the duration and the artwork embedded in the file; without it the
      file and folder names are used instead.
    * Pillow (already a dependency) - to store an embedded cover for the window.

Nothing in here touches the network, the configuration or the running player: it
only reads the file system and answers with item dictionaries.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import unquote, urlparse

from jellyfin import COVER_CACHE_DIR, ensure_dirs

# Audio containers mpv can play. Anything else inside a dropped folder (films,
# concerts, music videos, cue sheets, pictures) is left alone - exactly like the
# Jellyfin side of the player, which only ever accepts audio items.
AUDIO_SUFFIXES = frozenset({
    ".aac", ".aif", ".aiff", ".alac", ".ape", ".dff", ".dsf", ".flac", ".m4a",
    ".m4b", ".mka", ".mp2", ".mp3", ".mpc", ".mpga", ".oga", ".ogg", ".opus",
    ".spx", ".tta", ".wav", ".wma", ".wv",
})

# Playlists a file manager can hand over. They are read, never written.
PLAYLIST_SUFFIXES = frozenset({".m3u", ".m3u8", ".pls", ".xspf"})

# Cover art shipped next to the music (the usual names, in order of preference).
COVER_STEMS = ("cover", "folder", "front", "album", "albumart", "albumartsmall")
COVER_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".bmp")

MAX_SCAN_FILES = 20000              # a dropped folder tree is never unbounded
MAX_PLAYLIST_DEPTH = 4              # playlists inside playlists inside ...
MAX_COVER_BYTES = 8 * 1024 * 1024   # ignore absurd embedded pictures

REMOTE_PREFIXES = ("http://", "https://", "ftp://", "ftps://", "mms://",
                   "rtsp://", "smb://", "nfs://")

_LEADING_TRACK_NUMBER = re.compile(r"^\s*\d{1,3}\s*[-._)]\s+")
_ARTIST_ALBUM_FOLDER = re.compile(r"\s+-\s+")

try:                               # optional: real tags and embedded artwork
    from mutagen import File as _open_audio
except Exception:                  # pragma: no cover - mutagen is optional
    _open_audio = None

try:                               # optional: to store an embedded cover
    from PIL import Image
except Exception:                  # pragma: no cover - Pillow is a dependency
    Image = None

HAS_MUTAGEN = _open_audio is not None


# --------------------------------------------------------------------------- #
# what is playable
# --------------------------------------------------------------------------- #

def is_audio_file(path: Any) -> bool:
    """True for an existing file mpv is allowed to open (audio only)."""
    try:
        candidate = Path(path)
        return candidate.suffix.lower() in AUDIO_SUFFIXES and candidate.is_file()
    except OSError:
        return False


def is_playlist_file(path: Any) -> bool:
    """True for an existing playlist the file manager may have dropped."""
    try:
        candidate = Path(path)
        return candidate.suffix.lower() in PLAYLIST_SUFFIXES and candidate.is_file()
    except OSError:
        return False


def local_id(path: Any) -> str:
    """A stable id for a local file (the engine and the UI key on it)."""
    text = os.fspath(path)
    digest = hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()
    return f"local:{digest[:16]}"



# --------------------------------------------------------------------------- #
# dropped paths -> playable files
# --------------------------------------------------------------------------- #

def expand_paths(paths: Iterable[Any]) -> List[Path]:
    """Every audio file behind the dropped paths, in the order they should play.

    A file plays as it is, a folder is walked top down with its entries sorted,
    and a playlist keeps its own order. The same file is played once even when it
    was dropped twice (or sits in a folder next to the playlist pointing at it),
    and a missing, remote or non-audio entry is skipped instead of stopping the
    rest of the drop.
    """
    found: List[Path] = []
    seen: Set[str] = set()
    visited: Set[str] = set()

    def remember(path: Path) -> None:
        key = _identity(path)
        if key in seen:
            return
        seen.add(key)
        found.append(path)

    def visit(path: Path, depth: int) -> None:
        if len(found) >= MAX_SCAN_FILES:
            return
        try:
            if path.is_dir():
                for audio in _scan_directory(path):
                    remember(audio)
                    if len(found) >= MAX_SCAN_FILES:
                        return
                return
            if depth < MAX_PLAYLIST_DEPTH and is_playlist_file(path):
                key = _identity(path)
                if key in visited:              # a playlist pointing at itself
                    return
                visited.add(key)
                for entry in playlist_paths(path):
                    visit(entry, depth + 1)
                    if len(found) >= MAX_SCAN_FILES:
                        return
                return
            if is_audio_file(path):
                remember(path)
        except OSError:
            return

    for raw in paths or ():
        try:
            candidate = Path(os.fspath(raw)).expanduser()
        except (TypeError, ValueError):
            continue
        visit(candidate, 0)
    return found


def _identity(path: Path) -> str:
    """A key that makes two paths the same file (symlinks included)."""
    try:
        return os.path.realpath(os.fspath(path))
    except OSError:
        return os.fspath(path)


def _scan_directory(directory: Path) -> List[Path]:
    """Audio files under *directory*: the top level first, names sorted.

    ``os.walk`` (instead of ``Path.rglob``) so a symlinked folder can never send
    the scan round in circles.
    """
    audio: List[Path] = []
    try:
        for root, dirnames, filenames in os.walk(directory, followlinks=False):
            dirnames.sort(key=str.lower)
            for name in sorted(filenames, key=str.lower):
                if len(audio) >= MAX_SCAN_FILES:
                    return audio
                if Path(name).suffix.lower() in AUDIO_SUFFIXES:
                    audio.append(Path(root) / name)
    except OSError:
        return audio
    return audio


def nothing_to_play_reason(paths: Iterable[Any]) -> str:
    """One sentence for the footer when a drop turned out to be unplayable.

    A drop that only held network locations (``sftp://``, ``smb://``, a stream
    URL) says so: "no audio files were found" would be wrong there - the files
    are real, they are just not on this machine, and the player can only open
    files it can read itself.
    """
    entries = [str(entry) for entry in (paths or ())]
    if entries and all("://" in entry and not entry.startswith("/") for entry in entries):
        return ("That drop is a network location - only files on this machine "
                "can be played")
    return "Nothing to play in that drop - no audio files were found"


# --------------------------------------------------------------------------- #
# playlists
# --------------------------------------------------------------------------- #

def playlist_paths(path: Any) -> List[Path]:
    """The local files a playlist points at, in the order it lists them."""
    playlist = Path(path)
    suffix = playlist.suffix.lower()
    if suffix == ".pls":
        return _entries_pls(playlist)
    if suffix == ".xspf":
        return _entries_xspf(playlist)
    return _entries_m3u(playlist)


def local_reference(reference: str, base: Path) -> Optional[Path]:
    """One playlist entry as a local file, or ``None`` when it is not one.

    Relative entries are resolved against *base* (the playlist's own folder),
    ``file://`` URIs are decoded, and anything that is not an existing local file
    - a stream URL, a dead link, a text note - is skipped.
    """
    text = (reference or "").strip().strip('"').strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered.startswith("file://"):
        parsed = urlparse(text)
        if parsed.netloc and parsed.netloc.lower() not in ("", "localhost"):
            return None
        candidates = [Path(unquote(parsed.path))]
    elif "://" in text or lowered.startswith(REMOTE_PREFIXES):
        return None
    else:
        candidates = [Path(text)]
        if "%" in text:          # playlists written on Windows percent-quote
            candidates.append(Path(unquote(text)))
    for candidate in candidates:
        if not candidate.is_absolute():
            candidate = base / candidate
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _entries_m3u(path: Path) -> List[Path]:
    """m3u / m3u8: one entry per line, ``#`` starts a comment (or a tag)."""
    entries: List[Path] = []
    for line in _read_text(path).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        target = local_reference(line, path.parent)
        if target is not None:
            entries.append(target)
    return entries


def _entries_pls(path: Path) -> List[Path]:
    """pls: an INI file whose ``FileN`` keys are the tracks, in N order."""
    numbered: List[Tuple[int, str]] = []
    for line in _read_text(path).splitlines():
        line = line.strip()
        if not line or line.startswith("[") or "=" not in line:
            continue
        key, _separator, value = line.partition("=")
        match = re.fullmatch(r"file(\d+)", key.strip(), re.IGNORECASE)
        if match:
            numbered.append((int(match.group(1)), value.strip()))
    entries: List[Path] = []
    for _number, value in sorted(numbered):
        target = local_reference(value, path.parent)
        if target is not None:
            entries.append(target)
    return entries


def _entries_xspf(path: Path) -> List[Path]:
    """xspf: the ``<location>`` of every ``<track>``, in document order."""
    try:
        tree = ElementTree.parse(path)
    except (OSError, ElementTree.ParseError):
        return []
    entries: List[Path] = []
    for element in tree.iter():
        if _local_name(element.tag) != "location":
            continue
        target = local_reference(element.text or "", path.parent)
        if target is not None:
            entries.append(target)
    return entries


def _local_name(tag: Any) -> str:
    """The tag name without its XML namespace."""
    return str(tag).rsplit("}", 1)[-1].lower()



# --------------------------------------------------------------------------- #
# local files -> items (the same shape the Jellyfin client produces)
# --------------------------------------------------------------------------- #

def build_items(paths: Sequence[Path]) -> List[Dict[str, Any]]:
    """Jellyfin-shaped items for the given files (one per path, in order)."""
    covers: Dict[str, Optional[Path]] = {}
    return [item_for(path, index=index, covers=covers)
            for index, path in enumerate(paths)]


def item_for(path: Any, *, index: int = 0,
             covers: Optional[Dict[str, Optional[Path]]] = None) -> Dict[str, Any]:
    """One item for a local file: the same keys the Jellyfin client produces.

    ``Name``/``Artists``/``Album``/``RunTimeTicks`` mean exactly what they mean
    for a streamed track, so ``JellyfinClient.display_title`` and friends - and
    with them the whole window - show a dropped file like any other song. The two
    private keys tell the engine and the UI that this one lives on this machine:
    ``_local_path`` (the file mpv should open) and ``_local_cover`` (cover art
    found next to it, empty when there is none).
    """
    file = Path(path)
    tags = read_tags(file)
    artist_hint, album_hint = _folder_hint(file.parent)
    title = _first(tags.get("title")) or _title_from_name(file.stem)
    artist = (_first(tags.get("artist")) or _first(tags.get("albumartist"))
              or artist_hint)
    album = _first(tags.get("album")) or album_hint
    folder_cover = _sidecar_cover(file, covers)
    item: Dict[str, Any] = {
        "Id": local_id(file),
        "Name": title,
        "Type": "Audio",
        "MediaType": "Audio",
        "Container": file.suffix.lstrip(".").lower(),
        "Artists": [artist] if artist else [],
        "AlbumArtist": artist,
        "Album": album,
        "RunTimeTicks": int(max(0.0, float(tags.get("duration") or 0.0)) * 10_000_000),
        "_local_path": os.fspath(file),
        "_local_cover": os.fspath(folder_cover) if folder_cover else "",
    }
    track = tags.get("track")
    item["IndexNumber"] = int(track) if track else index + 1
    if tags.get("year"):
        item["ProductionYear"] = int(tags["year"])
    return item


def read_tags(path: Any) -> Dict[str, Any]:
    """Title/artist/album/duration read from the file itself (mutagen, optional).

    An unreadable or untagged file simply answers nothing: the caller then falls
    back to the file and folder names, and mpv still reports the real duration
    once the song plays.
    """
    if _open_audio is None:
        return {}
    try:
        audio = _open_audio(os.fspath(path), easy=True)
    except Exception:                 # unsupported or damaged container
        return {}
    if audio is None:
        return {}
    tags: Dict[str, Any] = {}
    try:
        stored = audio.tags or {}
        for name in ("title", "artist", "albumartist", "album", "date"):
            value = _first(stored.get(name))
            if value:
                tags[name] = value
        track = _leading_number(_first(stored.get("tracknumber")))
        if track:
            tags["track"] = track
        year = _leading_number(tags.get("date") or "")
        if year:
            tags["year"] = year
        tags.pop("date", None)
        length = getattr(getattr(audio, "info", None), "length", 0.0)
        if length:
            tags["duration"] = float(length)
    except Exception:                 # a broken tag must not stop the drop
        return tags
    return tags


def _first(value: Any) -> str:
    """First entry of a (possibly list-valued) tag, as a stripped string."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value).strip()


def _leading_number(text: str) -> Optional[int]:
    """The number a tag starts with ("3/12" -> 3, "2024-05-01" -> 2024)."""
    match = re.match(r"^\s*(\d{1,4})", text or "")
    return int(match.group(1)) if match else None


def _title_from_name(stem: str) -> str:
    """The file name without its track number ("01 - Song" -> "Song")."""
    name = _LEADING_TRACK_NUMBER.sub("", stem or "").strip()
    return name or (stem or "").strip() or "Unknown title"


def _folder_hint(folder: Path) -> Tuple[str, str]:
    """Artist/album guessed from the folder a file lives in.

    Compilations usually sit in ``Artist - Album`` folders, which is worth more
    than an empty line in the window; every other folder name is used for both.
    """
    name = (folder.name or "").strip()
    if not name:
        return "", ""
    parts = _ARTIST_ALBUM_FOLDER.split(name, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return name, name


# --------------------------------------------------------------------------- #
# cover art
# --------------------------------------------------------------------------- #

def cover_path(item: Optional[Dict[str, Any]]) -> Optional[Path]:
    """The artwork of a local item: next to the file, or out of the file itself.

    Returns the image on disk the window should show, or ``None`` when the file
    carries no artwork at all (the UI then draws its usual placeholder).
    """
    if not item:
        return None
    stored = _first(item.get("_local_cover"))
    if stored:
        candidate = Path(stored)
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        except OSError:
            pass
    return extract_cover(item)


def extract_cover(item: Optional[Dict[str, Any]]) -> Optional[Path]:
    """Store the artwork embedded in a local file in the cover cache (once)."""
    if item is None or Image is None:
        return None
    path = _first(item.get("_local_path"))
    if not path:
        return None
    destination = _cover_cache_path(path)
    try:
        if destination.is_file() and destination.stat().st_size > 0:
            return destination
    except OSError:
        pass
    data = embedded_cover_bytes(path)
    if not data:
        return None
    try:
        ensure_dirs()
        with Image.open(io.BytesIO(data)) as image:
            image.convert("RGB").save(destination, "JPEG", quality=90)
    except (OSError, ValueError):
        return None
    return destination


def embedded_cover_bytes(path: Any) -> bytes:
    """The artwork stored inside the file (ID3, FLAC, MP4 or Ogg), if any."""
    if _open_audio is None:
        return b""
    try:
        audio = _open_audio(os.fspath(path))
    except Exception:
        return b""
    if audio is None:
        return b""
    data = b""
    pictures = getattr(audio, "pictures", None)
    if pictures:
        data = bytes(getattr(pictures[0], "data", b"") or b"")
    if not data:
        data = _tag_picture(getattr(audio, "tags", None))
    if not data or len(data) > MAX_COVER_BYTES:
        return b""
    return data


def _tag_picture(tags: Any) -> bytes:
    """Artwork out of an ID3, MP4 or Vorbis comment tag (empty when none)."""
    if tags is None:
        return b""
    try:
        if hasattr(tags, "getall"):                     # ID3: mp3, aiff, wav
            pictures = tags.getall("APIC")
            if pictures:
                return bytes(getattr(pictures[0], "data", b"") or b"")
        if "covr" in tags:                              # MP4 / M4A
            return bytes(tags["covr"][0])
        if "metadata_block_picture" in tags:            # Ogg Vorbis / Opus
            value = tags["metadata_block_picture"]
            return _vorbis_picture(value[0] if isinstance(value, (list, tuple)) else value)
    except Exception:
        return b""
    return b""


def _vorbis_picture(value: Any) -> bytes:
    """The image bytes of a base64 FLAC picture block (Vorbis comments)."""
    try:
        data = base64.b64decode(value)
    except Exception:
        return b""
    offset = 4                                          # picture type
    for _field in range(2):                             # mime type, description
        if len(data) < offset + 4:
            return b""
        length = int.from_bytes(data[offset:offset + 4], "big")
        if len(data) < offset + 4 + length:
            return b""
        offset += 4 + length
    offset += 16                                        # width/height/depth/colors
    if len(data) < offset + 4:
        return b""
    length = int.from_bytes(data[offset:offset + 4], "big")
    offset += 4
    return data[offset:offset + length] if length > 0 else b""


def _cover_cache_path(path_text: str) -> Path:
    """Where the artwork of one file is kept between runs."""
    digest = hashlib.sha1(path_text.encode("utf-8", "surrogatepass")).hexdigest()
    return COVER_CACHE_DIR / f"local_{digest[:20]}.jpg"


def _sidecar_cover(file: Path,
                   covers: Optional[Dict[str, Optional[Path]]] = None) -> Optional[Path]:
    """Cover art stored next to the file (cover.jpg, folder.png, ...)."""
    folder = file.parent
    key = os.fspath(folder)
    if covers is not None and key in covers:
        return covers[key]
    found: Optional[Path] = None
    try:
        entries: Dict[str, Path] = {}
        for entry in folder.iterdir():
            if entry.is_file():
                entries[entry.name.lower()] = entry
        for stem in COVER_STEMS:
            for suffix in COVER_SUFFIXES:
                if stem + suffix in entries:
                    found = entries[stem + suffix]
                    break
            if found is not None:
                break
    except OSError:
        found = None
    if covers is not None:
        covers[key] = found
    return found


