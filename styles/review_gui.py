#!/usr/bin/env python3
"""Manual style review GUI for the SimpleJellyMus catalog.

This is a **separate tool**. The player only ever reads the catalog; this tool
is the one place that writes a manual verdict. It is meant for the tracks where
automatic detection failed: no genre tag, no style, a vague family-only label,
or a low-confidence/conflicting entry from ``out/review.csv``.

A saved verdict goes into ``data/overrides.json`` and is applied with the same
``step4_normalize.apply_overrides`` function the pipeline uses. The resulting
``track_styles`` row has ``source='human'`` and survives future pipeline runs.

The tool can also add a **new canonical style** to ``data/styles.json`` (name,
family, optional parent/sub-style and aliases) and load it into the catalog
immediately, so a style the automatic pipeline never saw can be used at once.

    python3 styles/review_gui.py
    python3 styles/review_gui.py --list no_style --limit 20
    python3 styles/review_gui.py --db /tmp/catalog.sqlite
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont, messagebox, ttk
from typing import Any, Dict, List, Optional, Sequence, Tuple

STYLES_DIR = Path(__file__).resolve().parent
ROOT = STYLES_DIR.parent
sys.path.insert(0, str(STYLES_DIR))
sys.path.insert(0, str(ROOT))

import styles_db as S  # noqa: E402
from step3_vocabulary import normalize_raw  # noqa: E402
from step4_normalize import Normalizer, apply_overrides  # noqa: E402

# Same palette/font preference as the player, so the tool feels native.
BACKGROUND = "#151718"
CARD = "#1d2124"
CARD_LIGHT = "#2b2f33"
TEXT = "#ECEDEE"
MUTED = "#9BA1A6"
ACCENT = "#0A7EA4"
ACCENT_LIGHT = "#3aa7c9"
DANGER = "#e0533d"
FONT_CANDIDATES = ("Noto Sans", "DejaVu Sans", "Liberation Sans", "FreeSans", "Hack", "Arial")

# Same "looks like a label, not a band" rule as step5_llm.py. An artist-level
# override on one of these would hit a whole label bucket, so the GUI warns.
LABELISH = re.compile(
    r"(records|recordings|music|media|label|spin|vision|elite|distribution|"
    r"promotions|entertainment|productions|studios?|various artists|va)\s*$",
    re.IGNORECASE,
)

QUEUE_MODES: Tuple[Tuple[str, str], ...] = (
    ("no_style", "No style"),
    ("no_tag", "No genre tag"),
    ("vague", "Vague / family only"),
    ("review", "Review queue (CSV)"),
    ("all", "All tracks"),
)
QUEUE_LABELS = dict(QUEUE_MODES)


def pick_font_family() -> str:
    """Return the first available nice-looking font family."""
    try:
        available = set(tkfont.families())
    except tk.TclError:
        return "TkDefaultFont"
    for family in FONT_CANDIDATES:
        if family in available:
            return family
    return "TkDefaultFont"


def scope_key(row: Dict[str, Any], kind: str) -> str:
    """The key written to overrides.json for a track/album/artist scope."""
    if kind == "tracks":
        return str(row.get("rel_path") or "")
    artist = str(row.get("album_artist") or row.get("artists") or "").strip()
    if kind == "albums":
        album = str(row.get("album") or "").strip()
        return f"{artist} :: {album}" if artist and album else ""
    return artist


def scope_label(kind: str) -> str:
    return {"tracks": "track", "albums": "album", "artists": "artist"}[kind]


class OverrideStore:
    """Read/write ``overrides.json`` atomically, with a backup in ``out/``."""

    def __init__(self, path: Path, backup_dir: Optional[Path] = None) -> None:
        self.path = Path(path)
        self.backup_dir = Path(backup_dir) if backup_dir else self.path.parent

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"artists": {}, "albums": {}, "tracks": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError(f"{self.path} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"{self.path} must contain a JSON object")
        for section in ("artists", "albums", "tracks"):
            if not isinstance(data.get(section), dict):
                data[section] = {}
        return data

    def _backup(self) -> Optional[Path]:
        if not self.path.exists():
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.backup_dir / f"{self.path.stem}.json.bak-{stamp}"
        counter = 1
        while target.exists():
            target = self.backup_dir / f"{self.path.stem}.json.bak-{stamp}-{counter}"
            counter += 1
        shutil.copy2(self.path, target)
        return target

    def _write(self, data: Dict[str, Any]) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.replace(tmp, self.path)

    def set(self, kind: str, key: str, styles: Sequence[str], *,
            exclusive: bool, note: str = "review GUI") -> Dict[str, Any]:
        if kind not in ("artists", "albums", "tracks"):
            raise ValueError(f"unknown override kind: {kind}")
        if not key:
            raise ValueError(f"empty key for a {scope_label(kind)} override")
        data = self.load()
        existing = data.setdefault(kind, {}).get(str(key))
        entry: Dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
        entry["styles"] = list(styles)
        if exclusive:
            entry["exclusive"] = True
        else:
            entry.pop("exclusive", None)
        if note:
            entry["note"] = note
        data[kind][str(key)] = entry
        self._backup()
        self._write(data)
        return data

    def remove(self, kind: str, key: str) -> Dict[str, Any]:
        data = self.load()
        section = data.get(kind) or {}
        if key in section:
            del section[key]
        data[kind] = section
        self._backup()
        self._write(data)
        return data

    def find(self, row: Dict[str, Any]) -> Optional[Tuple[str, str, Dict[str, Any]]]:
        """Return the most specific existing override for *row*."""
        data = self.load()
        track_key = scope_key(row, "tracks")
        entry = (data.get("tracks") or {}).get(track_key)
        if isinstance(entry, dict):
            return "tracks", track_key, entry
        album_key = scope_key(row, "albums")
        if album_key:
            wanted = S.norm_key(album_key)
            for key, entry in (data.get("albums") or {}).items():
                if S.norm_key(key) == wanted and isinstance(entry, dict):
                    return "albums", key, entry
        artist_key = scope_key(row, "artists")
        if artist_key:
            wanted = S.norm_key(artist_key)
            for key, entry in (data.get("artists") or {}).items():
                if S.norm_key(key) == wanted and isinstance(entry, dict):
                    return "artists", key, entry
        return None


FAMILY_LINE_WIDTH = 82


def _quote(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _wrap_families(families: Sequence[str], width: int = FAMILY_LINE_WIDTH) -> List[str]:
    """Wrap the family names the way the current ``styles.json`` does."""
    lines: List[str] = []
    current = "    "
    for index, family in enumerate(families):
        token = _quote(family) + ("," if index < len(families) - 1 else "")
        if current.strip() and len(current) + 1 + len(token) > width:
            lines.append(current)
            current = "    "
        current += ("" if current == "    " else " ") + token
    if current.strip():
        lines.append(current)
    return lines or ["    "]


def dump_vocabulary(data: Dict[str, Any]) -> str:
    """Serialise ``data/styles.json`` in the format the project documents.

    The vocabulary is hand-edited and versioned, so it keeps the existing
    layout: *one style per line* and the families wrapped at roughly 82
    columns. ``json.dump(indent=2)`` would turn every style into a multi-line
    block and make every future diff unreadable.
    """
    known = {"_comment", "version", "families", "styles"}
    if any(key not in known for key in data):
        # Do not lose an unknown top-level key: fall back to plain JSON.
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    lines: List[str] = ["{"]
    comment = data.get("_comment")
    if isinstance(comment, list):
        lines.append('  "_comment": [')
        for index, text in enumerate(comment):
            comma = "," if index < len(comment) - 1 else ""
            lines.append(f"    {_quote(str(text))}{comma}")
        lines.append("  ],")
    lines.append(f'  "version": {json.dumps(data.get("version", 1))},')
    lines.append("")

    families = [str(name).strip() for name in (data.get("families") or [])
                if str(name).strip()]
    lines.append('  "families": [')
    lines.extend(_wrap_families(families))
    lines.append("  ],")
    lines.append("")

    styles = [entry for entry in (data.get("styles") or []) if isinstance(entry, dict)]
    lines.append('  "styles": [')
    for index, entry in enumerate(styles):
        comma = "," if index < len(styles) - 1 else ""
        lines.append(f"    {_quote(entry)}{comma}")
    lines.append("  ]")
    lines.append("}")
    return "\n".join(lines) + "\n"


class VocabularyStore:
    """Read/write ``data/styles.json``, the canonical style vocabulary.

    Adding a style here only edits the vocabulary file. The caller must also
    upsert the same entry into ``catalog.sqlite`` (see :meth:`ReviewApp.add_style`)
    so the picker and the overrides can use it immediately.
    """

    def __init__(self, path: Path, backup_dir: Optional[Path] = None) -> None:
        self.path = Path(path)
        self.backup_dir = Path(backup_dir) if backup_dir else self.path.parent

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            raise ValueError(f"vocabulary not found: {self.path}")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError(f"{self.path} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("styles"), list):
            raise ValueError(f"{self.path} must contain a 'styles' list")
        if not isinstance(data.get("families"), list):
            data["families"] = []
        return data

    def families(self) -> List[str]:
        """Every family in the vocabulary (empty when the file is missing)."""
        try:
            return [str(name) for name in (self.load().get("families") or [])]
        except ValueError:
            return []

    def styles(self) -> List[Dict[str, Any]]:
        """Every style entry (empty when the file is missing)."""
        try:
            return [entry for entry in (self.load().get("styles") or [])
                    if isinstance(entry, dict)]
        except ValueError:
            return []

    @staticmethod
    def _alias_keys(text: Any) -> List[str]:
        """Both keys the pipeline uses, so an alias cannot become ambiguous."""
        keys = [S.norm_key(text), normalize_raw(str(text or ""))]
        return list(dict.fromkeys(key for key in keys if key))

    @classmethod
    def _keys(cls, entry: Dict[str, Any]) -> List[str]:
        keys = cls._alias_keys(entry.get("name"))
        for alias in (entry.get("aliases") or []):
            keys.extend(cls._alias_keys(alias))
        return list(dict.fromkeys(keys))

    def _backup(self) -> Optional[Path]:
        if not self.path.exists():
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.backup_dir / f"{self.path.stem}.json.bak-{stamp}"
        counter = 1
        while target.exists():
            target = self.backup_dir / f"{self.path.stem}.json.bak-{stamp}-{counter}"
            counter += 1
        shutil.copy2(self.path, target)
        return target

    def _write(self, data: Dict[str, Any]) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(dump_vocabulary(data), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, name: str, family: str, *, parent: str = "",
            aliases: Sequence[str] = (),
            reserved: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Append one style in the documented format and return its entry.

        ``reserved`` maps normalised names/aliases to a human description for
        entries that exist only in the catalog (for example the automatic
        ``Metal (unspecified)`` placeholders), so they cannot be duplicated.
        """
        data = self.load()
        clean = S.style_display(name)
        if not clean:
            raise ValueError("A style needs a name.")
        clean_family = S.style_display(family)
        if not clean_family:
            raise ValueError("Choose a family for the style.")

        family_name = clean_family
        new_family = True
        for existing in data.get("families") or []:
            if S.norm_key(existing) == S.norm_key(clean_family):
                family_name = str(existing)
                new_family = False
                break

        parent_name = ""
        wanted_parent = S.norm_key(parent)
        if wanted_parent:
            for entry in data["styles"]:
                if S.norm_key(entry.get("name")) != wanted_parent:
                    continue
                parent_name = str(entry.get("name"))
                parent_family = S.norm_key(entry.get("family"))
                if parent_family and parent_family != S.norm_key(family_name):
                    raise ValueError(
                        f"Parent style {parent_name!r} is in the "
                        f"{entry.get('family')!r} family, not {family_name!r}.")
                break
            if not parent_name:
                raise ValueError(
                    f"Parent style {parent!r} is not in {self.path.name}. "
                    "Run step3_vocabulary.py --build first.")
            if S.norm_key(parent_name) == S.norm_key(clean):
                raise ValueError("A style cannot be its own sub-style.")

        name_keys = self._alias_keys(clean)
        alias_list: List[str] = []
        seen_aliases = set()
        for alias in aliases or []:
            text = str(alias).strip()
            keys = self._alias_keys(text)
            if not keys or any(key in name_keys for key in keys):
                continue
            if any(key in seen_aliases for key in keys):
                continue
            seen_aliases.update(keys)
            alias_list.append(text)

        # Names and aliases already owned by the vocabulary.
        owned: Dict[str, str] = {}
        for entry in data["styles"]:
            entry_name = str(entry.get("name") or "").strip()
            if not entry_name:
                continue
            for key in self._keys(entry):
                owned.setdefault(key, entry_name)
        slug = S.style_slug(clean)
        for entry in data["styles"]:
            if S.style_slug(entry.get("name")) == slug:
                raise ValueError(
                    f"A style named {entry.get('name')!r} already exists "
                    f"({entry.get('family') or 'no family'}).")
        for key in name_keys:
            owner = owned.get(key)
            if owner:
                raise ValueError(f"{clean!r} is already used by the style {owner!r}.")
        for alias in alias_list:
            for key in self._alias_keys(alias):
                owner = owned.get(key)
                if owner:
                    raise ValueError(
                        f"{alias!r} is already used by the style {owner!r}.")

        # Names that only the catalog knows (placeholders created by step 4).
        reserved = reserved or {}
        for key in name_keys:
            if key in reserved:
                raise ValueError(
                    f"The catalog already contains {reserved[key]}.")
        for alias in alias_list:
            for key in self._alias_keys(alias):
                if key in reserved:
                    raise ValueError(
                        f"{alias!r} is already used by {reserved[key]}")

        entry: Dict[str, Any] = {"name": clean, "family": family_name}
        if parent_name:
            entry["parent"] = parent_name
        entry["aliases"] = alias_list
        data["styles"].append(entry)
        if new_family:
            data["families"].append(family_name)
        self._backup()
        self._write(data)
        return entry


class PreviewPlayer:
    """A tiny mpv preview process; no IPC and no state to break."""

    def __init__(self, seconds: int = 60) -> None:
        self.seconds = max(10, int(seconds))
        self._process: Optional[subprocess.Popen] = None

    @staticmethod
    def available() -> bool:
        return shutil.which("mpv") is not None

    def play(self, source: str, seconds: Optional[int] = None) -> None:
        self.stop()
        if not self.available():
            raise RuntimeError("mpv was not found in PATH")
        length = max(0, int(seconds if seconds is not None else self.seconds))
        command = [
            "mpv", "--no-video", "--force-window=no", "--no-terminal",
            "--really-quiet", "--no-config", "--audio-client-name=SimpleJellyMus-review",
        ]
        if length > 0:
            command.append(f"--length={length}")
        command.append(str(source))
        self._process = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL,
                                         stdin=subprocess.DEVNULL)

    def stop(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()


class ReviewCatalog:
    """Catalog access for the review tool (the only writer is apply_overrides)."""

    QUEUES = tuple(mode for mode, _ in QUEUE_MODES)

    def __init__(self, config: Dict[str, Any], db_path: Optional[Any] = None) -> None:
        self.config = config
        self.db_path = S.resolve(db_path) if db_path else S.db_path(config)
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"catalog not found: {self.db_path}\n"
                "Build it first with: python3 styles/step1_scan.py")
        self.connection = S.connect(self.db_path, create=False)
        self.out_dir = S.out_dir(config)
        root = str(S.setting(config, "music_root", "") or "").strip()
        self.music_root = S.resolve(root) if root else None
        self._styles: Optional[List[Dict[str, Any]]] = None

    def close(self) -> None:
        self.connection.close()

    def styles(self) -> List[Dict[str, Any]]:
        if self._styles is None:
            self._styles = [dict(row) for row in self.connection.execute(
                "SELECT id, name, family, parent, aliases, search_key, tracks"
                " FROM styles ORDER BY family COLLATE NOCASE, name COLLATE NOCASE")]
        return self._styles

    def reload_styles(self) -> List[Dict[str, Any]]:
        """Drop the cached vocabulary (after a style was added)."""
        self._styles = None
        return self.styles()

    def style_names(self) -> Dict[str, str]:
        return {row["id"]: row["name"] for row in self.styles()}

    def queue(self, mode: str = "no_style", query: str = "",
              limit: int = 3000) -> Tuple[List[Dict[str, Any]], int]:
        if mode not in self.QUEUES:
            raise ValueError(f"unknown queue mode: {mode}")
        base = ("SELECT t.rel_path, t.name, t.album, t.album_artist, t.artists,"
                " t.year, t.raw_genre, t.primary_style, t.family, t.label_source,"
                " t.duration_ms, t.track_no, t.id, t.album_key, t.artist_key"
                " FROM tracks t")
        where = ""
        params: Tuple[Any, ...] = ()
        if mode == "no_style":
            where = " WHERE t.primary_style IS NULL"
        elif mode == "no_tag":
            where = " WHERE TRIM(COALESCE(t.raw_genre, '')) = ''"
        elif mode == "vague":
            where = (" WHERE t.primary_style IN (SELECT id FROM styles"
                     " WHERE name LIKE '%(unspecified)' OR name = family)")
        order = (" ORDER BY t.album_artist COLLATE NOCASE, t.album COLLATE NOCASE,"
                 " t.track_no, t.name COLLATE NOCASE")
        if mode == "review":
            review = self.review_rows()
            if not review:
                return [], 0
            rows = [dict(row) for row in self.connection.execute(base + order)]
            rows = [row for row in rows if row["rel_path"] in review]
            order_map = {path: index for index, path in enumerate(review)}
            rows.sort(key=lambda row: order_map.get(row["rel_path"], 0))
            for row in rows:
                row["_review"] = review[row["rel_path"]]
        else:
            rows = [dict(row) for row in self.connection.execute(base + where + order,
                                                                 params)]
        if query.strip():
            rows = [row for row in rows if self._matches(query, row)]
        total = len(rows)
        return rows[:max(1, int(limit))], total

    def review_rows(self) -> Dict[str, Dict[str, str]]:
        path = self.out_dir / "review.csv"
        if not path.exists():
            return {}
        rows: Dict[str, Dict[str, str]] = {}
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                rel_path = row.get("path")
                if rel_path:
                    rows[rel_path] = row
        return rows

    @staticmethod
    def _matches(query: str, row: Dict[str, Any]) -> bool:
        wanted = S.norm_key(query)
        text = " ".join(str(row.get(key) or "") for key in
                        ("album_artist", "artists", "album", "name", "rel_path"))
        haystack = S.norm_key(text)
        if not wanted:
            return True
        if wanted in haystack:
            return True
        words = haystack.split()
        return all(any(word in cand or cand.startswith(word) for cand in words)
                   for word in wanted.split())


    def labels(self, rel_path: str) -> List[Dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT s.name, s.family, ts.source, ts.weight, ts.confidence, ts.evidence"
            " FROM track_styles ts JOIN styles s ON s.id = ts.style_id"
            " WHERE ts.rel_path = ? ORDER BY ts.weight DESC, ts.source DESC",
            (rel_path,))]

    def suggestion(self, row: Dict[str, Any]) -> Optional[str]:
        names = self.style_names()
        for column in ("album_key", "artist_key"):
            key = str(row.get(column) or "")
            if not key:
                continue
            found = self.connection.execute(
                "SELECT primary_style, COUNT(*) AS n FROM tracks"
                f" WHERE {column} = ? AND rel_path <> ? AND primary_style IS NOT NULL"
                " AND primary_style NOT IN (SELECT id FROM styles"
                "  WHERE name LIKE '%(unspecified)' OR name = family)"
                " GROUP BY primary_style ORDER BY n DESC LIMIT 1",
                (key, row.get("rel_path"))).fetchone()
            if found and found["primary_style"] in names:
                return names[found["primary_style"]]
        return None

    def scope_count(self, kind: str, row: Dict[str, Any]) -> int:
        if kind == "tracks":
            return 1
        column = "album_key" if kind == "albums" else "artist_key"
        key = str(row.get(column) or "")
        if not key:
            return 0
        return int(self.connection.execute(
            f"SELECT COUNT(*) FROM tracks WHERE {column} = ?", (key,)).fetchone()[0])

    def apply_overrides(self) -> Tuple[int, List[str]]:
        """Write every override through the pipeline helper and refresh counts."""
        try:
            normalizer = Normalizer(self.connection, self.config)
            overridden, errors = apply_overrides(
                normalizer, self.connection, self.config, dry_run=False)
            S.refresh_style_counts(self.connection)
            S.refresh_track_primary(self.connection)
            S.rebuild_artist_rollup(self.connection)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        self._styles = None
        return overridden, errors

    def local_path(self, row: Dict[str, Any]) -> Optional[Path]:
        if self.music_root is None:
            return None
        path = self.music_root / str(row.get("rel_path") or "")
        return path if path.is_file() else None

    def stream_url(self, row: Dict[str, Any]) -> Optional[str]:
        """Fallback when the local mount is not available."""
        if not row.get("id"):
            return None
        try:
            import jellyfin
            config = jellyfin.load_config()
            if not config:
                return None
            client = jellyfin.JellyfinClient.from_config(config)
            return client.stream_url({"Id": row["id"]})
        except Exception:
            return None


class ReviewApp:
    """The review window."""

    def __init__(self, root: tk.Tk, *, config: Dict[str, Any],
                 db_path: Optional[Any] = None,
                 overrides_path: Optional[Any] = None) -> None:
        self.root = root
        self.root.title("SimpleJellyMus · Style review")
        self.root.geometry("1180x760")
        self.root.minsize(980, 640)
        self.root.configure(bg=BACKGROUND)
        self.family = pick_font_family()
        self.config = config
        self.catalog = ReviewCatalog(config, db_path)
        override_file = (S.resolve(overrides_path) if overrides_path
                         else S.resolve(S.setting(
                             config, "normalize.overrides_file", "data/overrides.json")))
        self.overrides = OverrideStore(override_file, backup_dir=self.catalog.out_dir)
        vocabulary_file = S.resolve(S.setting(
            config, "normalize.styles_file", "data/styles.json"))
        self.vocabulary = VocabularyStore(vocabulary_file,
                                          backup_dir=self.catalog.out_dir)
        # The pipeline helper reads this setting, so make it point at the same
        # file the GUI edits (the default is data/overrides.json anyway).
        normalize = dict(config.get("normalize") or {})
        normalize["overrides_file"] = str(override_file)
        config["normalize"] = normalize
        self.preview = PreviewPlayer()
        self.rows: List[Dict[str, Any]] = []
        self._hidden: set = set()
        self.styles: List[Dict[str, Any]] = []
        self.style_index: Dict[str, Dict[str, Any]] = {}
        self.current: Optional[Dict[str, Any]] = None
        self.selected_style: Optional[Dict[str, Any]] = None
        self._filter_job: Optional[str] = None
        self._mode = tk.StringVar(value=QUEUE_LABELS["no_style"])
        self._query = tk.StringVar()
        self._style_query = tk.StringVar()
        self._exclusive = tk.BooleanVar(value=True)
        self._build()
        self._load_styles()
        self.reload_queue()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Control-s>", lambda _event: self.save("tracks"))
        self.root.bind("<Control-n>", lambda _event: self.open_new_style_dialog())
        self.root.bind("<F5>", lambda _event: self.preview_current())
        self.root.bind("<Escape>", lambda _event: self.preview.stop())

    # ------------------------------------------------------------- widgets
    def _label(self, parent: tk.Misc, text: str = "", *, size: int = 10,
               bold: bool = False, color: str = TEXT, bg: str = CARD,
               **kwargs: Any) -> tk.Label:
        return tk.Label(parent, text=text, bg=bg, fg=color,
                        font=(self.family, size, "bold" if bold else "normal"),
                        **kwargs)

    def _button(self, parent: tk.Misc, text: str, command: Any, *,
                accent: bool = False, width: Optional[int] = None) -> tk.Button:
        background = ACCENT if accent else CARD_LIGHT
        active = ACCENT_LIGHT if accent else CARD
        return tk.Button(parent, text=text, command=command, relief="flat",
                         borderwidth=0, bg=background, fg=TEXT,
                         activebackground=active, activeforeground=TEXT,
                         font=(self.family, 10, "bold"), padx=12, pady=7,
                         cursor="hand2", width=width)

    def _build(self) -> None:
        self._build_top()
        self._build_body()

    def _build_top(self) -> None:
        top = tk.Frame(self.root, bg=BACKGROUND, padx=18, pady=14)
        top.pack(fill="x")
        self._label(top, "Style review", size=18, bold=True, bg=BACKGROUND).pack(side="left")
        self._label(top, "  manual verdicts → source human", size=9,
                    color=MUTED, bg=BACKGROUND).pack(side="left", padx=(8, 0))

        self._label(top, "Queue", size=9, color=MUTED, bg=BACKGROUND).pack(
            side="left", padx=(24, 4))
        combo = ttk.Combobox(top, textvariable=self._mode, state="readonly", width=21,
                             values=[label for _mode, label in QUEUE_MODES])
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", lambda _event: self.reload_queue())

        self._label(top, "Filter", size=9, color=MUTED, bg=BACKGROUND).pack(
            side="left", padx=(16, 4))
        entry = tk.Entry(top, textvariable=self._query, bg=CARD, fg=TEXT,
                         insertbackground=TEXT, relief="flat",
                         font=(self.family, 10), width=24)
        entry.pack(side="left", ipady=4)
        entry.bind("<KeyRelease>", self._queue_filter_changed)

        self._button(top, "Refresh", self.reload_queue).pack(side="left", padx=(10, 0))
        self.count_label = self._label(top, "", size=9, color=MUTED, bg=BACKGROUND)
        self.count_label.pack(side="right")

    def _build_body(self) -> None:
        body = tk.Frame(self.root, bg=BACKGROUND, padx=18, pady=0)
        body.pack(fill="both", expand=True, pady=(0, 14))

        left = tk.Frame(body, bg=CARD, width=440)
        left.pack(side="left", fill="y", padx=(0, 12))
        left.pack_propagate(False)
        self._label(left, "Tracks to review", size=11, bold=True).pack(
            anchor="w", padx=12, pady=(10, 6))
        queue_frame = tk.Frame(left, bg=CARD)
        queue_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        scroll = tk.Scrollbar(queue_frame)
        scroll.pack(side="right", fill="y")
        self.queue_list = tk.Listbox(
            queue_frame, exportselection=False, activestyle="none", bd=0,
            highlightthickness=0, bg=CARD, fg=TEXT, selectbackground=ACCENT,
            selectforeground=TEXT, font=(self.family, 10), yscrollcommand=scroll.set)
        self.queue_list.pack(side="left", fill="both", expand=True)
        scroll.configure(command=self.queue_list.yview)
        self.queue_list.bind("<<ListboxSelect>>", self._on_queue_select)

        self.right = tk.Frame(body, bg=CARD)
        self.right.pack(side="left", fill="both", expand=True)
        self._build_details(self.right)

    def _build_details(self, parent: tk.Frame) -> None:
        pad = tk.Frame(parent, bg=CARD, padx=18, pady=14)
        pad.pack(fill="both", expand=True)

        self.track_title = self._label(pad, "Select a track", size=16, bold=True,
                                       wraplength=690, justify="left")
        self.track_title.pack(anchor="w")
        self.track_meta = self._label(pad, "", size=10, color=MUTED, wraplength=690,
                                      justify="left")
        self.track_meta.pack(anchor="w", pady=(4, 0))
        self.track_path = self._label(pad, "", size=8, color=MUTED, wraplength=690,
                                     justify="left")
        self.track_path.pack(anchor="w", pady=(2, 0))
        self.track_genre = self._label(pad, "", size=9, color=MUTED)
        self.track_genre.pack(anchor="w", pady=(10, 0))
        self.track_labels = self._label(pad, "", size=9, wraplength=690, justify="left")
        self.track_labels.pack(anchor="w", pady=(4, 0))
        self.override_label = self._label(pad, "", size=9, color=ACCENT_LIGHT,
                                          wraplength=690, justify="left")
        self.override_label.pack(anchor="w", pady=(4, 0))

        playback = tk.Frame(pad, bg=CARD)
        playback.pack(fill="x", pady=(12, 0))
        self._button(playback, "▶ Preview 60 s", self.preview_current,
                     accent=True).pack(side="left")
        self._button(playback, "Full track", lambda: self.preview_current(full=True)).pack(
            side="left", padx=(8, 0))
        self._button(playback, "■ Stop", self.preview.stop).pack(side="left", padx=(8, 0))
        self.preview_status = self._label(playback, "", size=9, color=MUTED)
        self.preview_status.pack(side="left", padx=(12, 0))

        style_box = tk.Frame(pad, bg=CARD)
        style_box.pack(fill="both", expand=True, pady=(16, 0))
        style_header = tk.Frame(style_box, bg=CARD)
        style_header.pack(fill="x")
        self._label(style_header, "Style — type to find", size=10,
                    bold=True).pack(side="left")
        self._button(style_header, "New style…", self.open_new_style_dialog).pack(
            side="right")
        search = tk.Entry(style_box, textvariable=self._style_query, bg=CARD_LIGHT,
                          fg=TEXT, insertbackground=TEXT, relief="flat",
                          font=(self.family, 11))
        search.pack(fill="x", ipady=5, pady=(5, 6))
        search.bind("<KeyRelease>", self._style_filter_changed)
        search.bind("<Return>", lambda _event: self._select_first_style())
        search.bind("<Down>", lambda _event: self._focus_style_list())
        self.style_search = search

        list_frame = tk.Frame(style_box, bg=CARD)
        list_frame.pack(fill="both", expand=True)
        scroll = tk.Scrollbar(list_frame)
        scroll.pack(side="right", fill="y")
        self.style_list = tk.Listbox(
            list_frame, exportselection=False, activestyle="none", bd=0,
            highlightthickness=0, bg=CARD_LIGHT, fg=TEXT, selectbackground=ACCENT,
            selectforeground=TEXT, font=(self.family, 10), height=7,
            yscrollcommand=scroll.set)
        self.style_list.pack(side="left", fill="both", expand=True)
        scroll.configure(command=self.style_list.yview)
        self.style_list.bind("<<ListboxSelect>>", self._on_style_select)
        self.style_info = self._label(style_box, "", size=9, color=MUTED)
        self.style_info.pack(anchor="w", pady=(4, 0))

        options = tk.Frame(pad, bg=CARD)
        options.pack(fill="x", pady=(10, 0))
        check = tk.Checkbutton(
            options, text="Exclusive: remove the automatic labels for the saved tracks",
            variable=self._exclusive, bg=CARD, fg=TEXT, activebackground=CARD,
            activeforeground=TEXT, selectcolor=CARD_LIGHT, anchor="w",
            font=(self.family, 9), highlightthickness=0, bd=0)
        check.pack(side="left")

        actions = tk.Frame(pad, bg=CARD)
        actions.pack(fill="x", pady=(12, 0))
        self.track_button = self._button(actions, "Save to track", lambda: self.save("tracks"),
                                         accent=True)
        self.album_button = self._button(actions, "Save to album", lambda: self.save("albums"))
        self.artist_button = self._button(actions, "Save to artist", lambda: self.save("artists"))
        self.unknown_button = self._button(actions, "Mark unknown", self.mark_unknown)
        self.clear_button = self._button(actions, "Clear override", self.clear_override)
        self.next_button = self._button(actions, "Next →", self.next_track)
        for index, button in enumerate((self.track_button, self.album_button,
                                        self.artist_button, self.unknown_button,
                                        self.clear_button, self.next_button)):
            button.grid(row=index // 3, column=index % 3, sticky="ew", padx=(0, 8), pady=4)
        for column in range(3):
            actions.grid_columnconfigure(column, weight=1)

        self.status = self._label(pad, "Pick a track and a style.", size=9, color=MUTED)
        self.status.pack(anchor="w", pady=(12, 0))

        # ------------------------------------------------------------- queue
    def _mode_id(self) -> str:
        label = self._mode.get()
        for mode, text in QUEUE_MODES:
            if text == label:
                return mode
        return "no_style"

    def _queue_filter_changed(self, _event: Any = None) -> None:
        if self._filter_job:
            self.root.after_cancel(self._filter_job)
        self._filter_job = self.root.after(120, self.reload_queue)

    def reload_queue(self, select_index: Optional[int] = None) -> None:
        self._filter_job = None
        try:
            rows, total = self.catalog.queue(self._mode_id(), self._query.get())
        except Exception as exc:
            messagebox.showerror("Queue failed", str(exc))
            return
        returned = len(rows)
        rows = [row for row in rows
                if str(row.get("rel_path") or "") not in self._hidden]
        if len(rows) < returned:
            total = max(0, total - (returned - len(rows)))
        self.rows = rows
        self.queue_list.delete(0, "end")
        for row in rows:
            self.queue_list.insert("end", self._format_track(row))
        if len(rows) < total:
            self.count_label.configure(text=f"{len(rows):,} of {total:,} · use the filter")
        else:
            self.count_label.configure(text=f"{total:,} track(s)")
        if not rows:
            self.current = None
            self._show_empty()
            return
        index = 0 if select_index is None else max(0, min(int(select_index), len(rows) - 1))
        self.queue_list.selection_clear(0, "end")
        self.queue_list.selection_set(index)
        self.queue_list.activate(index)
        self.queue_list.see(index)
        self._on_queue_select()

    @staticmethod
    def _format_track(row: Dict[str, Any]) -> str:
        artist = row.get("album_artist") or row.get("artists") or "?"
        album = row.get("album") or "?"
        name = row.get("name") or "?"
        return f"{artist}  ·  {album}  ·  {name}"

    def _on_queue_select(self, _event: Any = None) -> None:
        selection = self.queue_list.curselection()
        if not selection:
            return
        index = int(selection[0])
        if index >= len(self.rows):
            return
        self.current = self.rows[index]
        self.preview.stop()
        self.preview_status.configure(text="")
        self._update_details()

    def _show_empty(self) -> None:
        self.track_title.configure(text="Nothing to review")
        self.track_meta.configure(text=f"Queue: {self._mode.get()}")
        self.track_path.configure(text="")
        self.track_genre.configure(text="")
        self.track_labels.configure(text="")
        self.override_label.configure(text="")
        self.selected_style = None
        self._clear_style_selection()
        for button in (self.track_button, self.album_button, self.artist_button,
                       self.unknown_button, self.clear_button):
            button.configure(state="disabled")
        self.status.configure(text="Nothing in this queue.")


    def _update_details(self) -> None:
        row = self.current
        if row is None:
            return
        self.track_title.configure(text=str(row.get("name") or "Untitled"))
        meta = " · ".join(str(value) for value in (
            row.get("album_artist") or row.get("artists"),
            row.get("album"),
            row.get("year"),
        ) if value)
        self.track_meta.configure(text=meta)
        self.track_path.configure(text=str(row.get("rel_path") or ""))
        genre = str(row.get("raw_genre") or "").strip()
        self.track_genre.configure(text=f"Genre tag: {genre}" if genre else "Genre tag: (none)")
        labels = self.catalog.labels(str(row.get("rel_path") or ""))
        if labels:
            text = "Current labels: " + " · ".join(
                f"{label['name']} ({label['source']}, {float(label['weight']):.2f})"
                for label in labels)
        else:
            text = "Current labels: none"
        review = row.get("_review") or {}
        if review:
            text += (f"\nReview: {review.get('decision', '?')} "
                     f"({review.get('confidence', '?')})")
        self.track_labels.configure(text=text)

        existing = self.overrides.find(row)
        if existing:
            kind, _key, entry = existing
            styles = ", ".join(entry.get("styles") or []) or "no style"
            flags = " · exclusive" if entry.get("exclusive") else ""
            note = f" · {entry.get('note')}" if entry.get("note") else ""
            self.override_label.configure(
                text=f"Manual override ({scope_label(kind)}): {styles}{flags}{note}")
        else:
            self.override_label.configure(text="No manual override yet.")

        album_count = self.catalog.scope_count("albums", row)
        artist_count = self.catalog.scope_count("artists", row)
        self.album_button.configure(text=f"Save to album ({album_count})",
                                    state="normal" if album_count else "disabled")
        self.artist_button.configure(text=f"Save to artist ({artist_count})",
                                     state="normal" if artist_count else "disabled")
        for button in (self.track_button, self.unknown_button):
            button.configure(state="normal")
        self.clear_button.configure(state="normal" if existing else "disabled")
        artist = str(row.get("album_artist") or row.get("artists") or "")
        if LABELISH.search(artist):
            self.override_label.configure(
                text=self.override_label.cget("text")
                + "\n⚠ album artist looks like a label; prefer album/track saves.")

        if existing is not None and not existing[2].get("styles"):
            self._clear_style_selection()
            self.style_info.configure(text="Marked unknown (empty override).")
        elif existing and existing[2].get("styles"):
            self._select_style_by_name(str(existing[2]["styles"][0]))
        elif row.get("primary_style"):
            self._select_style_by_name(self.catalog.style_names().get(
                str(row["primary_style"]), ""))
        else:
            suggestion = self.catalog.suggestion(row)
            if suggestion:
                self._select_style_by_name(suggestion)
                self.style_info.configure(text=f"Suggested from this album/artist: {suggestion}")
            else:
                self._clear_style_selection()
                self.style_info.configure(text="No style selected — type to find one.")
        self.status.configure(text="Pick a style, then save to track, album or artist.")


    # ------------------------------------------------------------- styles
    def _load_styles(self) -> None:
        self.styles = self.catalog.styles()
        self.filtered_styles: List[Dict[str, Any]] = []
        self._filter_styles()

    def _family_values(self) -> List[str]:
        """Families from both the vocabulary file and the catalog."""
        families = set(self.vocabulary.families())
        for style in self.catalog.styles():
            family = str(style.get("family") or "").strip()
            if family:
                families.add(family)
        return sorted(families, key=str.casefold)

    def _catalog_reserved(self) -> Dict[str, str]:
        """Names/aliases the catalog already knows but the vocabulary may not."""
        reserved: Dict[str, str] = {}
        for style in self.catalog.styles():
            name = str(style.get("name") or "").strip()
            if not name:
                continue
            description = f"{name!r}"
            for key in VocabularyStore._alias_keys(name):
                reserved.setdefault(key, description)
            try:
                aliases = json.loads(style.get("aliases") or "[]")
            except ValueError:
                aliases = []
            for alias in aliases:
                for key in VocabularyStore._alias_keys(alias):
                    reserved.setdefault(key, description)
        return reserved

    def add_style(self, name: str, family: str, *, parent: str = "",
                  aliases: Sequence[str] = ()) -> Dict[str, Any]:
        """Add a canonical style to styles.json and the catalog.

        Raises ``ValueError`` for invalid input and ``RuntimeError`` when the
        vocabulary was written but the catalog could not be updated.
        """
        entry = self.vocabulary.add(name, family, parent=parent, aliases=aliases,
                                    reserved=self._catalog_reserved())
        try:
            S.upsert_style(
                self.catalog.connection, entry["name"],
                family=entry.get("family"),
                parent=S.style_slug(entry["parent"]) if entry.get("parent") else None,
                aliases=entry.get("aliases") or [],
                source="human")
            self.catalog.connection.commit()
        except Exception as exc:
            self.catalog.connection.rollback()
            raise RuntimeError(
                f"{entry['name']!r} was added to {self.vocabulary.path.name}, "
                f"but the catalog update failed: {exc}\n\n"
                "Run: python3 styles/step3_vocabulary.py --build") from exc
        self.catalog.reload_styles()
        self._load_styles()
        self._select_style_by_name(entry["name"])
        details = entry.get("family") or "no family"
        if entry.get("parent"):
            details += f" · sub-style of {entry['parent']}"
        self._status(f"Added {entry['name']!r} ({details}) to "
                     f"{self.vocabulary.path.name} and the catalog · source human")
        return entry

    def open_new_style_dialog(self) -> None:
        NewStyleDialog(self)

    @staticmethod
    def _style_display(style: Dict[str, Any]) -> str:
        family = style.get("family") or "—"
        return f"{style.get('name') or style.get('id')}  —  {family}"

    def _style_filter_changed(self, _event: Any = None) -> None:
        self._filter_styles()
        if self.filtered_styles:
            self.style_list.selection_clear(0, "end")
            self.style_list.selection_set(0)
            self.style_list.activate(0)
            self._on_style_select()

    def _filter_styles(self) -> None:
        query = S.norm_key(self._style_query.get())
        words = query.split()
        chosen: List[Dict[str, Any]] = []
        for style in self.styles:
            text = S.norm_key(" ".join(str(style.get(key) or "") for key in
                                       ("name", "family", "aliases", "search_key")))
            if not words:
                chosen.append(style)
            elif query in text or all(any(word in cand or cand.startswith(word)
                                          for cand in text.split()) for word in words):
                chosen.append(style)
        self.filtered_styles = chosen
        self.style_list.delete(0, "end")
        self.style_index = {}
        for style in chosen:
            display = self._style_display(style)
            self.style_index[display] = style
            self.style_list.insert("end", display)
        if not chosen:
            self.selected_style = None

    def _on_style_select(self, _event: Any = None) -> None:
        selection = self.style_list.curselection()
        if not selection:
            return
        index = int(selection[0])
        if index >= len(self.filtered_styles):
            return
        self.selected_style = self.filtered_styles[index]
        tracks = self.selected_style.get("tracks")
        suffix = f" · {int(tracks):,} tracks" if isinstance(tracks, int) else ""
        parent = str(self.selected_style.get("parent") or "")
        parent_name = self.catalog.style_names().get(parent, parent)
        hierarchy = f" · sub-style of {parent_name}" if parent_name else ""
        self.style_info.configure(
            text=f"{self.selected_style['name']} — "
                 f"{self.selected_style.get('family') or '—'}{hierarchy}{suffix}")

    def _select_first_style(self) -> None:
        if not self.filtered_styles:
            return
        self.style_list.selection_clear(0, "end")
        self.style_list.selection_set(0)
        self.style_list.activate(0)
        self._on_style_select()
        self.style_list.focus_set()

    def _focus_style_list(self) -> None:
        self.style_list.focus_set()
        if not self.style_list.curselection() and self.filtered_styles:
            self._select_first_style()

    def _select_style_by_name(self, name: str) -> None:
        """Select a style by its canonical name, clearing any search filter."""
        wanted = S.norm_key(name)
        if not wanted:
            self._clear_style_selection()
            return
        self._style_query.set("")
        self._filter_styles()
        for index, style in enumerate(self.filtered_styles):
            if S.norm_key(str(style.get("name") or "")) == wanted:
                self.style_list.selection_clear(0, "end")
                self.style_list.selection_set(index)
                self.style_list.activate(index)
                self.style_list.see(index)
                self._on_style_select()
                return
        self._clear_style_selection()

    def _clear_style_selection(self) -> None:
        self.selected_style = None
        if hasattr(self, "style_list"):
            self.style_list.selection_clear(0, "end")


    # ------------------------------------------------------------- actions
    def _status(self, text: str, *, error: bool = False) -> None:
        self.status.configure(text=text, fg=DANGER if error else MUTED)

    def _selected_index(self) -> int:
        selection = self.queue_list.curselection()
        return int(selection[0]) if selection else 0

    def _current_scope(self, kind: str) -> Tuple[str, int]:
        row = self.current
        if row is None:
            return "", 0
        return scope_key(row, kind), self.catalog.scope_count(kind, row)

    def save(self, kind: str) -> None:
        if self.current is None:
            self._status("Select a track first.", error=True)
            return
        if self.selected_style is None:
            self._status("Select a style first (type in the style box).", error=True)
            self.style_search.focus_set()
            return
        key, count = self._current_scope(kind)
        if not key or count <= 0:
            self._status(f"This track has no {scope_label(kind)} scope.", error=True)
            return
        artist = str(self.current.get("album_artist") or self.current.get("artists") or "")
        if kind == "artists" and LABELISH.search(artist):
            if not messagebox.askyesno(
                    "Artist looks like a label",
                    f"{artist!r} looks like a label, not a band.\n"
                    f"An artist-level override would affect {count} tracks.\n\n"
                    "Save to the artist anyway?\n"
                    "(No = cancel and use the album or track button)"):
                return
        if count > 1:
            if not messagebox.askyesno(
                    "Save to multiple tracks?",
                    f"Apply {self.selected_style['name']!r} to {count} "
                    f"track(s) of this {scope_label(kind)}?"):
                return
        try:
            self.overrides.set(kind, key, [self.selected_style["name"]],
                               exclusive=bool(self._exclusive.get()), note="review GUI")
        except Exception as exc:
            self._status(f"Could not write overrides.json: {exc}", error=True)
            return
        self._status(f"Applying {self.selected_style['name']!r} to {count} track(s)…")
        self.root.update_idletasks()
        try:
            _overridden, errors = self.catalog.apply_overrides()
        except Exception as exc:
            self._status(f"Saved to overrides.json, but applying failed: {exc}", error=True)
            return
        if errors:
            messagebox.showwarning("Some overrides failed", "\n".join(errors))
        self._status(f"Saved {self.selected_style['name']!r} to {count} track(s) "
                     f"· source human · catalog updated")
        self.reload_queue(select_index=self._selected_index())

    def mark_unknown(self) -> None:
        if self.current is None:
            return
        rel_path = str(self.current.get("rel_path") or "")
        if not rel_path:
            return
        if not messagebox.askyesno(
                "Mark as unknown",
                "Remove all style labels from this track?\n"
                "This is the same as an empty override with exclusive=true."):
            return
        try:
            self.overrides.set("tracks", rel_path, [], exclusive=True,
                               note="marked unknown in review GUI")
            self.catalog.apply_overrides()
        except Exception as exc:
            self._status(f"Could not mark the track unknown: {exc}", error=True)
            return
        self._hidden.add(rel_path)
        self._status("Marked unknown (no style); hidden for this session.")
        self.reload_queue(select_index=self._selected_index())


    def clear_override(self) -> None:
        if self.current is None:
            return
        existing = self.overrides.find(self.current)
        if not existing:
            self._status("This track has no manual override.", error=True)
            return
        kind, key, _entry = existing
        column = {"tracks": "rel_path", "albums": "album_key",
                  "artists": "artist_key"}[kind]
        if kind == "tracks":
            paths = [key]
        else:
            paths = [row[0] for row in self.catalog.connection.execute(
                f"SELECT rel_path FROM tracks WHERE {column} = ?",
                (self.current.get(column),))]
        if not messagebox.askyesno(
                "Clear manual override?",
                f"Remove the {scope_label(kind)} override and its human labels "
                f"from {len(paths)} track(s)?"):
            return
        try:
            self.overrides.remove(kind, key)
            S.clear_track_styles(self.catalog.connection, ["human"], paths)
            S.refresh_style_counts(self.catalog.connection)
            S.refresh_track_primary(self.catalog.connection)
            S.rebuild_artist_rollup(self.catalog.connection)
            self.catalog.connection.commit()
        except Exception as exc:
            self.catalog.connection.rollback()
            self._status(f"Could not clear the override: {exc}", error=True)
            return
        self._status("Manual override removed. Run step4/step9 if you want the "
                     "automatic labels rebuilt.")
        self.reload_queue(select_index=self._selected_index())

    def next_track(self) -> None:
        if not self.rows:
            return
        index = min(self._selected_index() + 1, len(self.rows) - 1)
        self.queue_list.selection_clear(0, "end")
        self.queue_list.selection_set(index)
        self.queue_list.activate(index)
        self.queue_list.see(index)
        self._on_queue_select()

    def preview_current(self, full: bool = False) -> None:
        row = self.current
        if row is None:
            self._status("Select a track first.", error=True)
            return
        path = self.catalog.local_path(row)
        source = str(path) if path else self.catalog.stream_url(row)
        if not source:
            self._status("File not found locally and Jellyfin has no stream URL.", error=True)
            return
        try:
            self.preview.play(source, 0 if full else 60)
        except Exception as exc:
            self._status(f"Could not start mpv: {exc}", error=True)
            return
        self.preview_status.configure(text="playing full track" if full else "preview 60 s")

    def close(self) -> None:
        try:
            self.preview.stop()
        finally:
            try:
                self.catalog.close()
            finally:
                self.root.destroy()


class NewStyleDialog(tk.Toplevel):
    """Modal form that adds one style to the vocabulary and the catalog."""

    NONE = "(none)"

    def __init__(self, app: ReviewApp) -> None:
        super().__init__(app.root)
        self.app = app
        self.title("New style")
        self.configure(bg=CARD)
        self.resizable(False, False)
        self.transient(app.root)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _event: self.destroy())
        self._name_var = tk.StringVar()
        self._family_var = tk.StringVar()
        self._parent_var = tk.StringVar(value=self.NONE)
        self._aliases_var = tk.StringVar()
        self._build()
        self._refresh_parents()
        self._center()
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.name_entry.focus_set()

    def _build(self) -> None:
        body = tk.Frame(self, bg=CARD, padx=18, pady=16)
        body.pack(fill="both", expand=True)
        self.app._label(body, "Add a canonical style", size=14,
                        bold=True).pack(anchor="w")
        self.app._label(
            body,
            "Appended to data/styles.json and loaded into the catalog "
            "immediately (source human).",
            size=9, color=MUTED).pack(anchor="w", pady=(2, 14))

        self.app._label(body, "Family *", size=9, color=MUTED).pack(anchor="w")
        self.family_box = ttk.Combobox(
            body, textvariable=self._family_var, width=48,
            values=self.app._family_values())
        self.family_box.pack(fill="x", pady=(3, 2))
        self.family_box.bind("<<ComboboxSelected>>",
                             lambda _event: self._refresh_parents())
        self.family_box.bind("<FocusOut>", lambda _event: self._refresh_parents())
        self.app._label(body, "Type a new family to create it.",
                        size=8, color=MUTED).pack(anchor="w", pady=(0, 10))

        self.app._label(body, "Style name *", size=9, color=MUTED).pack(anchor="w")
        self.name_entry = tk.Entry(
            body, textvariable=self._name_var, bg=CARD_LIGHT, fg=TEXT,
            insertbackground=TEXT, relief="flat", font=(self.app.family, 11))
        self.name_entry.pack(fill="x", ipady=5, pady=(3, 10))
        self.name_entry.bind("<Return>", lambda _event: self.save())

        self.app._label(body, "Sub-style of (optional)", size=9,
                        color=MUTED).pack(anchor="w")
        self.parent_box = ttk.Combobox(
            body, textvariable=self._parent_var, state="readonly", width=48)
        self.parent_box.pack(fill="x", pady=(3, 2))
        self.app._label(body,
                        "A parent in the same family; shown as the finer hierarchy.",
                        size=8, color=MUTED).pack(anchor="w", pady=(0, 10))

        self.app._label(body, "Aliases (optional, comma-separated)",
                        size=9, color=MUTED).pack(anchor="w")
        self.alias_entry = tk.Entry(
            body, textvariable=self._aliases_var, bg=CARD_LIGHT, fg=TEXT,
            insertbackground=TEXT, relief="flat", font=(self.app.family, 11))
        self.alias_entry.pack(fill="x", ipady=5, pady=(3, 2))
        self.alias_entry.bind("<Return>", lambda _event: self.save())
        self.app._label(body,
                        "Spellings from file tags that should map to this style.",
                        size=8, color=MUTED).pack(anchor="w", pady=(0, 12))

        actions = tk.Frame(body, bg=CARD)
        actions.pack(fill="x")
        self.app._button(actions, "Cancel", self.destroy).pack(side="right")
        self.app._button(actions, "Add style", self.save, accent=True).pack(
            side="right", padx=(0, 8))

    def _center(self) -> None:
        self.update_idletasks()
        parent = self.app.root
        width = self.winfo_reqwidth()
        height = self.winfo_reqheight()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - width) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - height) // 2)
        self.geometry(f"+{x}+{y}")

    def _parent_values(self) -> List[str]:
        """Styles of the chosen family, as possible sub-style parents."""
        family = S.norm_key(self._family_var.get())
        names: List[str] = []
        for style in self.app.catalog.styles():
            name = str(style.get("name") or "").strip()
            if not name or name.endswith("(unspecified)"):
                continue
            if family and S.norm_key(style.get("family")) != family:
                continue
            names.append(name)
        return [self.NONE] + sorted(names, key=str.casefold)

    def _refresh_parents(self) -> None:
        values = self._parent_values()
        self.parent_box.configure(values=values)
        if self._parent_var.get() not in values:
            self._parent_var.set(self.NONE)

    def save(self) -> None:
        parent = self._parent_var.get().strip()
        if parent == self.NONE:
            parent = ""
        aliases = [part.strip() for part in re.split(r"[,;]", self._aliases_var.get())
                   if part.strip()]
        try:
            self.app.add_style(self._name_var.get().strip(),
                               self._family_var.get().strip(),
                               parent=parent, aliases=aliases)
        except ValueError as exc:
            messagebox.showerror("Cannot add the style", str(exc), parent=self)
            return
        except Exception as exc:
            messagebox.showerror("Could not add the style", str(exc), parent=self)
            return
        self.destroy()


def list_queue(config: Dict[str, Any], mode: str, *, db_path: Optional[str],
               query: str, limit: int) -> int:
    catalog = ReviewCatalog(config, db_path or None)
    try:
        rows, total = catalog.queue(mode, query, limit)
        print(f"{QUEUE_LABELS.get(mode, mode)}: {len(rows):,} of {total:,} track(s)")
        for row in rows:
            review = row.get("_review") or {}
            suffix = (f"  [{review.get('decision')} {review.get('confidence')}]"
                      if review else "")
            print(f"  {row.get('album_artist') or row.get('artists') or '?'}"
                  f"  ·  {row.get('album') or '?'}"
                  f"  ·  {row.get('name') or '?'}{suffix}")
            print(f"      {row.get('rel_path')}")
    finally:
        catalog.close()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Review and fix the styles the automatic detection missed.")
    parser.add_argument("--db", default="", help="catalog.sqlite (default: styles/config.json)")
    parser.add_argument("--overrides", default="",
                        help="overrides.json (default: styles/data/overrides.json)")
    parser.add_argument("--list", choices=ReviewCatalog.QUEUES, default="",
                        help="print a queue and exit (no window)")
    parser.add_argument("--query", default="", help="filter the printed queue")
    parser.add_argument("--limit", type=int, default=3000,
                        help="maximum rows to load (default: 3000)")
    args = parser.parse_args(list(argv) if argv is not None else None)

    config = S.load_config()
    if args.list:
        try:
            return list_queue(config, args.list, db_path=args.db or None,
                              query=args.query, limit=args.limit)
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    try:
        root = tk.Tk()
        ReviewApp(root, config=config, db_path=args.db or None,
                  overrides_path=args.overrides or None)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
