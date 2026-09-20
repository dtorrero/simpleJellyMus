#!/usr/bin/env python3
"""Read-only access to the style catalog built by the ``styles/`` tools.

The catalog is one SQLite file (see ``styles/README.md``) with one row per song
and a many-to-many table of styles. The player only ever *reads* it, and only
when a style filter is active: with no filter the queue keeps asking Jellyfin
for a random batch exactly as before.

Everything here is standard library only (``sqlite3``), opens the database in
read-only mode and never issues a write. If the file is missing, every method
reports that politely instead of failing - the player then behaves as if style
filtering did not exist.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sqlite3
import threading
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from jellyfin import JellyfinError

DEFAULT_PATH = (Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
                / "simplejellymus" / "catalog.sqlite")

MODES = ("any", "all", "not")

# Only the columns the player and the UI actually need.
TRACK_COLUMNS = (
    "t.id", "t.rel_path", "t.name", "t.album", "t.album_id", "t.album_artist",
    "t.artists", "t.year", "t.duration_ms", "t.container", "t.image_tag",
    "t.album_image_tag", "t.raw_genre", "t.primary_style", "t.family",
)

_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACES = re.compile(r"\s+", re.UNICODE)


def norm_key(text: Any) -> str:
    """Normalise a string for comparisons (same rule the builder uses)."""
    folded = unicodedata.normalize("NFKD", str(text or ""))
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    folded = folded.lower().replace("&", " and ")
    folded = folded.replace("\u00e6", "ae").replace("\u00f8", "o").replace("\u00df", "ss")
    return _SPACES.sub(" ", _PUNCT.sub(" ", folded)).strip()


def _similar(left: str, right: str) -> bool:
    """Close enough to be the same word with a typo ("progresive"/"progressive")."""
    return difflib.SequenceMatcher(None, left, right).ratio() >= 0.8


def shared_words(query: str, key: str) -> int:
    """How many words two phrases have in common, small typos allowed."""
    key_words = key.split()
    if not key_words:
        return 0
    matched = 0
    for word in query.split():
        if word in key_words:
            matched += 1
        elif len(word) >= 4 and any(len(other) >= 4 and _similar(word, other)
                                    for other in key_words):
            matched += 1
    return matched


def _artists(value: Any) -> List[str]:
    """The stored ``Artists`` string back into the list Jellyfin gives us."""
    text = str(value or "").strip()
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def item_from_row(row: sqlite3.Row, styles: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """A row of the catalog as an item dict in the shape the player expects.

    The shape matters: the queue, the preloader and the UI are written against
    the dicts Jellyfin returns, so a catalog-sourced track has to look the same
    (``Id``, ``Name``, ``ImageTags``, ``RunTimeTicks``, ...).
    """
    ticks = int(row["duration_ms"] or 0) * 10_000
    item: Dict[str, Any] = {
        "Id": row["id"],
        "Name": row["name"],
        "Type": "Audio",
        "MediaType": "Audio",
        "Album": row["album"],
        "AlbumId": row["album_id"],
        "AlbumArtist": row["album_artist"],
        "Artists": _artists(row["artists"]) or ([row["album_artist"]] if row["album_artist"] else []),
        "ProductionYear": row["year"],
        "RunTimeTicks": ticks,
        "Container": row["container"],
        "Path": row["rel_path"],
    }
    if row["image_tag"]:
        item["ImageTags"] = {"Primary": row["image_tag"]}
    if row["album_image_tag"]:
        item["AlbumPrimaryImageTag"] = row["album_image_tag"]
    if styles:
        item["_styles"] = list(styles)
    return item


class Catalog:
    """The style dataset, opened read-only (one connection per thread)."""

    def __init__(self, path: Optional[Any] = None, *, debug: bool = False) -> None:
        self.path = Path(path) if path else DEFAULT_PATH
        self.debug = debug
        self._local = threading.local()
        self._lock = threading.RLock()
        self._styles: Optional[List[Dict[str, Any]]] = None
        self._families: Optional[List[Dict[str, Any]]] = None

    # ------------------------------------------------------------- connection
    @property
    def connection(self) -> sqlite3.Connection:
        """A read-only connection for the calling thread."""
        connection = getattr(self._local, "connection", None)
        if connection is None:
            if not self.available():
                raise JellyfinError(
                    "No style catalog yet - run styles/prepare_styles.sh to build it")
            try:
                connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=15)
            except sqlite3.Error as exc:
                raise JellyfinError(f"The style catalog could not be opened: {exc}") from exc
            connection.row_factory = sqlite3.Row
            self._local.connection = connection
        return connection

    def available(self) -> bool:
        """True when the dataset file is there and readable."""
        try:
            return self.path.is_file() and self.path.stat().st_size > 0
        except OSError:
            return False

    def close(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
            self._local.connection = None

    def _rows(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        try:
            return self.connection.execute(sql, tuple(params)).fetchall()
        except sqlite3.Error as exc:
            if self.debug:
                print(f"[catalog] query failed: {exc}", flush=True)
            raise JellyfinError(f"The style catalog query failed: {exc}") from exc

    # ------------------------------------------------------------------ styles
    def styles(self, *, in_use_only: bool = True) -> List[Dict[str, Any]]:
        """Every style with its counts (cached: the vocabulary rarely changes)."""
        with self._lock:
            if self._styles is None:
                rows = self._rows(
                    "SELECT id, name, family, parent, aliases, tracks, artists FROM styles"
                    " ORDER BY family, name")
                self._styles = [dict(row) for row in rows]
            styles = self._styles
        if in_use_only:
            return [style for style in styles if (style.get("tracks") or 0) > 0]
        return list(styles)

    def families(self) -> List[Dict[str, Any]]:
        """The coarse buckets, with the number of *tracks* behind each one.

        Counted as distinct tracks (a song with two metal styles is one track),
        so the number shown on a chip is exactly what selecting it will play.
        Cached like :meth:`styles` - searching asks for them on every keystroke.
        """
        with self._lock:
            if self._families is None:
                self._families = [dict(row) for row in self._rows(
                    "SELECT s.family AS name, COUNT(DISTINCT s.id) AS styles,"
                    "       COUNT(DISTINCT t.id) AS tracks, MAX(s.tracks) AS biggest"
                    " FROM styles s LEFT JOIN track_styles ts ON ts.style_id = s.id"
                    " LEFT JOIN tracks t ON t.rel_path = ts.rel_path AND t.id IS NOT NULL"
                    " WHERE s.family IS NOT NULL GROUP BY s.family"
                    " ORDER BY tracks DESC, s.family")]
            return list(self._families)

    def stats(self) -> Dict[str, Any]:
        """A few numbers for the UI and the status line."""
        one = lambda sql: self._rows(sql)[0][0]  # noqa: E731
        return {
            "path": str(self.path),
            "tracks": one("SELECT COUNT(*) FROM tracks"),
            "playable": one("SELECT COUNT(*) FROM tracks WHERE id IS NOT NULL"),
            "labelled": one("SELECT COUNT(DISTINCT rel_path) FROM track_styles"),
            "styles": one("SELECT COUNT(*) FROM styles WHERE tracks > 0"),
            "labels": one("SELECT COUNT(*) FROM track_styles"),
        }

    # ----------------------------------------------------------------- search
    def search(self, text: str, limit: int = 15) -> List[Dict[str, Any]]:
        """Fuzzy search over style names and their aliases (typing finds it).

        Same scoring as the builder's picker preview: an exact hit beats a short
        phrase match, which beats a long one, which beats sharing a word; a hit
        on the family name lifts every style of that family.
        """
        query = norm_key(text)
        styles = self.styles()
        if not query:
            return sorted(styles, key=lambda style: -(style.get("tracks") or 0))[:limit]
        scored: List[Tuple[float, int, int, Dict[str, Any]]] = []
        for style in styles:
            keys = [norm_key(style["name"])]
            keys += [norm_key(alias) for alias in json.loads(style.get("aliases") or "[]")]
            best = 0.0
            specific = 0
            for key in keys:
                if not key:
                    continue
                words = len(key.split())
                score = 0.0
                if key == query:
                    score = 3.0
                elif words <= 3 and (query in key or key in query):
                    score = 2.4
                elif query in key or key in query:
                    score = 1.5
                elif words <= 4:
                    shared = shared_words(query, key)
                    if shared:
                        score = 1.0 + 0.1 * shared
                if not score:
                    continue
                # For a phrase match the longer key is the better answer ("Ska
                # Punk" over "Punk" for "ska punkz"); for a match that only
                # shares words the shorter one is ("Progressive Metal" over
                # "Experimental Progressive Metal").
                weight = len(key) if score >= 1.5 else -len(key)
                if score > best or (score == best and weight > specific):
                    best, specific = score, weight
            if style.get("family") and norm_key(style["family"]) == query:
                best += 1.2
            if best:
                scored.append((best, specific, style.get("tracks") or 0, style))
        # Ties go to the more specific match, and only then to more tracks.
        scored.sort(key=lambda item: (-item[0], -item[1], -item[2]))
        results = [style for _score, _specific, _count, style in scored[:limit]]
        for family in self.families():          # typing a family offers the family first
            if norm_key(family["name"]) == query:
                results.insert(0, {"id": f"family:{family['name']}", "name": family["name"],
                                   "family": family["name"], "tracks": int(family["tracks"] or 0),
                                   "artists": 0, "kind": "family", "aliases": None})
                break
        return results

    def resolve(self, entries: Iterable[str]) -> Tuple[List[str], List[str]]:
        """Turn names / aliases / ``family:Name`` into ids and family names.

        Rule when a name is both a style and a family ("Ska", "Rock", "Pop"):
        the **style wins**, because that is the specific intent, and a family is
        asked for explicitly with ``family:Ska``. The picker's family chips use
        that form, so there is no ambiguity in the UI.
        """
        ids: List[str] = []
        families: List[str] = []
        by_key = {norm_key(style["name"]): style["id"]
                  for style in self.styles(in_use_only=False)}
        for style in self.styles(in_use_only=False):
            for alias in json.loads(style.get("aliases") or "[]"):
                by_key.setdefault(norm_key(alias), style["id"])
        known = {norm_key(family["name"]): family["name"] for family in self.families()}
        for entry in entries or []:
            text = str(entry).strip()
            if not text:
                continue
            family_form = re.match(r"family[:\s]+(.+)$", text, re.IGNORECASE)
            if family_form:
                name = known.get(norm_key(family_form.group(1)))
                if name and name not in families:
                    families.append(name)
                elif not name:
                    raise JellyfinError(f"Unknown family {family_form.group(1)!r}")
                continue
            style_id = by_key.get(norm_key(text))
            if style_id is not None:
                if style_id not in ids:
                    ids.append(style_id)
                continue
            if norm_key(text) in known:
                name = known[norm_key(text)]
                if name not in families:
                    families.append(name)
                continue
            hint = self.search(text, 1)
            suggestion = f" - did you mean {hint[0]['name']!r}?" if hint else ""
            raise JellyfinError(f"Unknown style {text!r}{suggestion}")
        return ids, families

    def aliases_for(self, name: str) -> List[str]:
        """The aliases of one style (the picker shows them in its detail line)."""
        key = norm_key(name)
        for style in self.styles(in_use_only=False):
            if norm_key(style["name"]) == key:
                return [str(alias) for alias in json.loads(style.get("aliases") or "[]")]
        return []

    def expand(self, style_ids: Sequence[str], families: Sequence[str]) -> List[str]:
        """Add every style of the selected families to the selected styles."""
        ids = [str(style_id) for style_id in style_ids]
        for family in families or []:
            for row in self._rows("SELECT id FROM styles WHERE family = ?", (family,)):
                if row["id"] not in ids:
                    ids.append(row["id"])
        return ids

    # -------------------------------------------------------------- selection
    def _where(self, ids: Sequence[str], mode: str,
               exclude_ids: Sequence[str]) -> Tuple[str, List[Any]]:
        """The FROM/WHERE/GROUP BY parts shared by count() and random_batch()."""
        marks = ",".join("?" * len(ids))
        params: List[Any] = list(ids)
        if mode == "not":
            sql = (" FROM tracks t WHERE t.id IS NOT NULL AND NOT EXISTS"
                   f" (SELECT 1 FROM track_styles ts WHERE ts.rel_path = t.rel_path"
                   f" AND ts.style_id IN ({marks}))")
        else:
            sql = (" FROM tracks t JOIN track_styles ts ON ts.rel_path = t.rel_path"
                   f" WHERE t.id IS NOT NULL AND ts.style_id IN ({marks})")
        if exclude_ids:
            sql += f" AND t.id NOT IN ({','.join('?' * len(exclude_ids))})"
            params += list(exclude_ids)
        if mode == "all":
            sql += " GROUP BY t.rel_path HAVING COUNT(DISTINCT ts.style_id) = ?"
            params.append(len(ids))
        elif mode != "not":
            sql += " GROUP BY t.rel_path"
        return sql, params

    def count(self, style_ids: Sequence[str], families: Sequence[str], mode: str = "any") -> int:
        """How many tracks the selection would pick from (shown in the picker)."""
        ids = self.expand(style_ids, families)
        mode = mode if mode in MODES else "any"
        if not ids:
            return 0
        if mode == "any" and len(ids) == 1 and not families:
            # One style: the exact number is already materialised in the table.
            for row in self._rows("SELECT tracks FROM styles WHERE id = ?", (ids[0],)):
                return int(row["tracks"] or 0)
        where, params = self._where(ids, mode, ())
        rows = self._rows("SELECT COUNT(*) FROM (SELECT t.rel_path" + where + ")", params)
        return int(rows[0][0] if rows else 0)

    def random_batch(self, style_ids: Sequence[str], families: Sequence[str], mode: str = "any",
                     limit: int = 200, exclude_ids: Sequence[str] = ()) -> List[Dict[str, Any]]:
        """A random batch of tracks matching the selection.

        Returns item dicts shaped exactly like the ones Jellyfin sends, plus
        ``_styles`` (the track's style names) so the UI needs no second lookup.
        """
        ids = self.expand(style_ids, families)
        mode = mode if mode in MODES else "any"
        if not ids:
            raise JellyfinError("The style filter is empty")
        where, params = self._where(ids, mode, [str(item) for item in exclude_ids])
        columns = ", ".join(TRACK_COLUMNS)
        rows = self._rows(f"SELECT {columns}{where} ORDER BY RANDOM() LIMIT ?",
                          params + [int(limit)])
        if not rows:
            return []
        names = self._style_names([row["rel_path"] for row in rows])
        return [item_from_row(row, names.get(row["rel_path"])) for row in rows]

    def _style_names(self, rel_paths: Sequence[str]) -> Dict[str, List[str]]:
        """Style names per track, heaviest first (this is what the UI shows)."""
        names: Dict[str, List[str]] = {}
        for start in range(0, len(rel_paths), 400):
            chunk = list(rel_paths[start:start + 400])
            marks = ",".join("?" * len(chunk))
            for row in self._rows(
                    "SELECT ts.rel_path AS path, s.name AS name FROM track_styles ts"
                    " JOIN styles s ON s.id = ts.style_id"
                    f" WHERE ts.rel_path IN ({marks}) ORDER BY ts.weight DESC, s.name", chunk):
                names.setdefault(row["path"], []).append(row["name"])
        return names

    def matches(self, item: Dict[str, Any], style_ids: Sequence[str], families: Sequence[str],
                mode: str = "any") -> bool:
        """Does *item* belong to the selection? (used to keep the preloaded next)"""
        item_id = (item or {}).get("Id")
        if not item_id:
            return False
        ids = self.expand(style_ids, families)
        mode = mode if mode in MODES else "any"
        if not ids:
            return True
        marks = ",".join("?" * len(ids))
        rows = self._rows(
            "SELECT COUNT(DISTINCT ts.style_id) AS n FROM tracks t"
            " JOIN track_styles ts ON ts.rel_path = t.rel_path"
            f" WHERE t.id = ? AND ts.style_id IN ({marks})", [str(item_id)] + list(ids))
        found = int(rows[0]["n"] if rows else 0)
        if mode == "any":
            return found > 0
        if mode == "all":
            return found == len(ids)
        return found == 0

    def track_styles(self, item_id: Any) -> Dict[str, Any]:
        """Everything the dataset knows about one song (the info view)."""
        if not item_id:
            return {}
        rows = self._rows(
            "SELECT t.raw_genre, t.primary_style, t.family, t.label_source, t.confidence,"
            "       s.name AS name, s.family AS style_family, ts.weight, ts.source,"
            "       ts.confidence AS style_confidence"
            " FROM tracks t LEFT JOIN track_styles ts ON ts.rel_path = t.rel_path"
            " LEFT JOIN styles s ON s.id = ts.style_id"
            " WHERE t.id = ? ORDER BY ts.weight DESC, ts.source", (str(item_id),))
        if not rows:
            return {}
        first = rows[0]
        return {
            "raw_genre": first["raw_genre"] or "",
            "primary": first["primary_style"] or "",
            "family": first["family"] or "",
            "label_source": first["label_source"] or "",
            "confidence": first["confidence"],
            "styles": [{"name": row["name"], "family": row["style_family"] or "",
                        "weight": row["weight"], "source": row["source"] or "",
                        "confidence": row["style_confidence"]}
                       for row in rows if row["name"]],
        }


def open_catalog(path: Optional[Any] = None, *, debug: bool = False) -> Optional[Catalog]:
    """The catalog, or ``None`` when the dataset has not been built yet."""
    catalog = Catalog(path, debug=debug)
    return catalog if catalog.available() else None
