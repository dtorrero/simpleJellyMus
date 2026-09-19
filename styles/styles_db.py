#!/usr/bin/env python3
"""Shared building blocks for the SimpleJellyMus style dataset.

This folder is completely independent from the player: it *builds* the dataset,
the player only ever *reads* it. Nothing here writes to a music file, to
Jellyfin or to the player's own configuration - every step is read-only with
respect to the outside world and writes only into the generated database and
the ``out/`` folder.

The dataset is one SQLite file with

    tracks          one row per song (the player's unit of playback)
    track_styles    many-to-many: a song can have several styles, each with a
                    weight, a source and a confidence
    styles          the canonical vocabulary (also what the picker shows)
    raw_genres      the audit trail of the messy genre strings of the library
    artists         artist-level roll-up: propagation, LLM/external caches
    api_calls       token/cost accounting for the optional LLM step
    meta            schema version, timestamps, statistics

Run ``python3 styles_db.py`` for a quick look at the current state of the
dataset. Every other step imports this module.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

STYLES_DIR = Path(__file__).resolve().parent
CONFIG_PATH = STYLES_DIR / "config.json"
LOCAL_CONFIG_PATH = STYLES_DIR / "config.local.json"

SCHEMA_VERSION = 2

# Sources are ranked: the higher number wins when two layers disagree about the
# same (track, style) pair. A human override always wins; the raw file tag beats
# everything automatic because it is what the owner of the library wrote down.
SOURCE_PRIORITY = {
    "human": 100,        # data/overrides.json
    "tag": 80,           # the genre embedded in the file itself
    "rules": 70,         # that tag, cleaned/split/mapped by data/rules.json
    "audio": 60,         # the acoustic classifier (independent evidence)
    "external": 50,      # Wikipedia / Wikidata / MusicBrainz
    "llm": 40,           # DeepSeek artist-level guess
    "propagated": 30,    # inferred from the artist/album roll-up
    "unknown": 0,
}
SOURCE_ORDER = sorted(SOURCE_PRIORITY, key=lambda name: -SOURCE_PRIORITY[name])

# Step 4 creates a placeholder style per family ("Metal (unspecified)") for the
# tags that only state a family. This SQL fragment finds the tracks whose best
# label is such a placeholder - exactly the ones the enrichment steps should go
# after.
FAMILY_STYLE_SQL = ("primary_style IN (SELECT id FROM styles"
                    " WHERE name LIKE '%(unspecified)')")

# Styles whose name is simply the family ("Rock", "Pop", "Ska", ...) say just as
# little as a placeholder: a more specific style outranks them too, whatever
# source it comes from. The vague label is never removed, so filtering by
# "Rock" still finds those tracks - only the *primary* label becomes useful.
VAGUE_STYLE_SQL = "primary_style IN (SELECT id FROM styles WHERE name = family)"

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- --------------------------------------------------------------------------
-- one row per song
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tracks (
    rel_path        TEXT PRIMARY KEY,       -- path relative to music_root
    id              TEXT UNIQUE,            -- Jellyfin item id (filled by step2)
    dir_artist      TEXT,                   -- guessed from the folder name
    dir_album       TEXT,
    dir_year        INTEGER,
    file_size       INTEGER,
    file_mtime      REAL,

    name            TEXT,                   -- title
    album           TEXT,
    album_id        TEXT,
    album_artist    TEXT,
    artists         TEXT,
    year            INTEGER,
    track_no        INTEGER,
    disc_no         INTEGER,
    duration_ms     INTEGER,
    container       TEXT,
    bitrate         INTEGER,

    image_tag       TEXT,                   -- cover tags: the player needs no API
    album_image_tag TEXT,

    raw_genre       TEXT,                   -- verbatim, never modified
    tags_json       TEXT,                   -- all other interesting file tags
    raw_genre_count INTEGER DEFAULT 0,

    artist_key      TEXT,                   -- normalised album artist (join key)
    album_key       TEXT,                   -- normalised "artist :: album"

    primary_style   TEXT,                   -- denormalised main style (fast path)
    family          TEXT,
    label_source    TEXT,                   -- which layer produced primary_style
    confidence      REAL,

    scanned_at      INTEGER,
    linked_at       INTEGER,
    normalized_at   INTEGER,
    enriched_at     INTEGER,
    classified_at   INTEGER
);

CREATE INDEX IF NOT EXISTS ix_tracks_artist_key ON tracks(artist_key);
CREATE INDEX IF NOT EXISTS ix_tracks_album_key  ON tracks(album_key);
CREATE INDEX IF NOT EXISTS ix_tracks_primary    ON tracks(primary_style);
CREATE INDEX IF NOT EXISTS ix_tracks_family     ON tracks(family);

-- --------------------------------------------------------------------------
-- the canonical vocabulary
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS styles (
    id         TEXT PRIMARY KEY,        -- slug, e.g. symphonic-power-metal
    name       TEXT NOT NULL,           -- Symphonic Power Metal
    family     TEXT,                    -- Metal
    parent     TEXT,                    -- optional parent style id
    aliases    TEXT,                    -- JSON list of extra search strings
    search_key TEXT,                    -- pre-normalised, for instant search
    source     TEXT,                    -- seed|rules|llm|external|human
    tracks     INTEGER DEFAULT 0,       -- materialised counts for the picker
    artists    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_styles_search ON styles(search_key);
CREATE INDEX IF NOT EXISTS ix_styles_family ON styles(family);

-- --------------------------------------------------------------------------
-- many styles per song
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS track_styles (
    rel_path   TEXT NOT NULL REFERENCES tracks(rel_path) ON DELETE CASCADE,
    style_id   TEXT NOT NULL,
    weight     REAL NOT NULL DEFAULT 1.0,
    source     TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    evidence   TEXT,
    updated_at INTEGER,
    PRIMARY KEY (rel_path, style_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_ts_style  ON track_styles(style_id, weight DESC);
CREATE INDEX IF NOT EXISTS ix_ts_source ON track_styles(source);

-- --------------------------------------------------------------------------
-- audit trail of the messy genre strings
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_genres (
    raw       TEXT PRIMARY KEY,
    tracks    INTEGER DEFAULT 0,
    styles    TEXT,                     -- JSON list of canonical style ids
    unknown   TEXT,                     -- JSON list of parts that were not mapped
    decision  TEXT,                     -- mapped|split|partial|dropped|junk|unresolved
    note      TEXT,
    updated_at INTEGER
);

-- --------------------------------------------------------------------------
-- artist roll-up: propagation, caches for the external/LLM layers
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS artists (
    key           TEXT PRIMARY KEY,     -- normalised name
    name          TEXT,                 -- as written in the files
    tracks        INTEGER DEFAULT 0,
    albums        INTEGER DEFAULT 0,
    styles        TEXT,                 -- JSON list of style ids
    family        TEXT,
    label_source  TEXT,
    confidence    REAL,
    external_json TEXT,                 -- cached Wikidata/MusicBrainz answer
    external_at   INTEGER,
    llm_json      TEXT,                 -- cached LLM answer
    llm_prompt    TEXT,                 -- prompt/model fingerprint of that answer
    llm_at        INTEGER,
    updated_at    INTEGER
);
CREATE INDEX IF NOT EXISTS ix_artists_family ON artists(family);

-- --------------------------------------------------------------------------
-- token/cost accounting for the optional LLM step
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS api_calls (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER,
    provider   TEXT,
    model      TEXT,
    purpose    TEXT,
    cache_key  TEXT,
    batch_size INTEGER,
    tokens_in  INTEGER,
    tokens_out INTEGER,
    cached_in  INTEGER,
    cost_usd   REAL,
    ok         INTEGER,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS ix_api_calls_ts ON api_calls(ts);
"""


# --------------------------------------------------------------------------- #
# configuration and paths
# --------------------------------------------------------------------------- #

def now() -> int:
    """Current time as an integer number of seconds."""
    return int(time.time())


def load_config() -> Dict[str, Any]:
    """Read ``config.json`` plus the optional ``config.local.json``."""
    config: Dict[str, Any] = {}
    for path in (CONFIG_PATH, LOCAL_CONFIG_PATH):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except ValueError as exc:
            raise SystemExit(f"{path} is not valid JSON: {exc}")
        if isinstance(data, dict):
            config = _merge(config, data)
    return config


def _merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    """Recursive dict merge used for the local overrides."""
    result = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def setting(config: Dict[str, Any], path: str, default: Any = None) -> Any:
    """Read a dotted setting, e.g. ``setting(cfg, "llm.model")``."""
    node: Any = config
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def resolve(path: Any, base: Optional[Path] = None) -> Path:
    """Expand ``~`` and make *path* absolute (relative to ``styles/``)."""
    text = str(path or "").strip()
    expanded = Path(os.path.expanduser(text))
    if not expanded.is_absolute():
        expanded = (base or STYLES_DIR) / expanded
    return expanded


def db_path(config: Dict[str, Any]) -> Path:
    """Where the generated dataset lives."""
    return resolve(config.get("db_path") or "out/catalog.sqlite")


def out_dir(config: Dict[str, Any]) -> Path:
    """Folder for reports and caches (created when needed)."""
    path = resolve(config.get("out_dir") or "out")
    path.mkdir(parents=True, exist_ok=True)
    return path


def connect(path: Path, *, create: bool = True) -> sqlite3.Connection:
    """Open the dataset, creating the schema when *create* is set.

    ``check_same_thread=False`` lets a worker thread *hand over* work that the
    main thread then writes (see step5_llm): SQLite still requires that two
    threads never write at the same time, so callers keep a lock around writes.
    """
    path = Path(path)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.exists():
        raise SystemExit(f"dataset not found: {path} - run step1_scan.py first")
    connection = sqlite3.connect(str(path), timeout=120.0, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    if create:
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        connection.commit()
    return connection


def meta_get(connection: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(connection: sqlite3.Connection, key: str, value: Any) -> None:
    connection.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )



# --------------------------------------------------------------------------- #
# text helpers
# --------------------------------------------------------------------------- #

_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACES = re.compile(r"\s+", re.UNICODE)


def strip_accents(text: str) -> str:
    """Fold accents so ``Ænigmatum`` and ``Aenigmatum`` can be joined."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def norm_key(text: Any) -> str:
    """Normalise a name for joining and searching.

    Lower case, no accents, no punctuation, single spaces: ``"The  Beatles!"``
    and ``"the beatles"`` become the same key, which is what the artist-level
    layers (propagation, Wikidata, the LLM) need to line up their answers.
    """
    folded = strip_accents(str(text or "")).replace("&", " and ")
    folded = folded.lower()
    folded = folded.replace("\u00e6", "ae").replace("\u00f8", "o").replace("\u00df", "ss")
    folded = _PUNCT.sub(" ", folded)
    return _SPACES.sub(" ", folded).strip()


def style_slug(name: Any) -> str:
    """Stable id for a style name (``Symphonic Power Metal`` -> ``symphonic-power-metal``)."""
    slug = _SPACES.sub("-", _PUNCT.sub(" ", str(name or "").lower())).strip("-")
    return slug or "unknown"


def style_display(name: Any) -> str:
    """Tidy a style name for display (``psy trance`` -> ``Psy Trance``).

    Only used for names that come from the vocabulary file or from an external
    layer - the raw file tags are never re-spelled, they are kept verbatim.
    """
    text = _SPACES.sub(" ", str(name or "").replace("_", " ")).strip(" -,.;/|")
    if not text:
        return ""
    if text.isupper() or text.islower():
        text = text.title()
    small_words = {"and", "of", "the", "de", "y"}
    words = []
    for index, word in enumerate(text.split(" ")):
        low = word.lower()
        if index and low in small_words:
            words.append(low)
        elif low == "n":                      # the "n" in "Rock n Roll"
            words.append(low)
        else:
            words.append(word[0].upper() + word[1:] if word.islower() else word)
    return " ".join(words)


# --------------------------------------------------------------------------- #
# vocabulary
# --------------------------------------------------------------------------- #

def upsert_style(connection: sqlite3.Connection, name: str, *, family: Optional[str] = None,
                 parent: Optional[str] = None, aliases: Optional[Iterable[str]] = None,
                 source: str = "seed") -> str:
    """Create or update one canonical style and return its id."""
    clean = style_display(name)
    if not clean:
        raise ValueError("a style needs a name")
    slug = style_slug(clean)
    alias_list: List[str] = []
    if aliases:
        for alias in aliases:
            text = str(alias).strip()
            if text and text.lower() != clean.lower() and text not in alias_list:
                alias_list.append(text)
    row = connection.execute(
        "SELECT aliases, family, parent FROM styles WHERE id=?", (slug,)).fetchone()
    if row is None:
        connection.execute(
            "INSERT INTO styles(id, name, family, parent, aliases, search_key, source)"
            " VALUES(?,?,?,?,?,?,?)",
            (slug, clean, family, parent, json.dumps(alias_list) if alias_list else None,
             norm_key(clean), source),
        )
        return slug
    # Merge: never lose an alias or a family that an earlier run discovered.
    merged = json.loads(row["aliases"] or "[]")
    for alias in alias_list:
        if alias not in merged:
            merged.append(alias)
    connection.execute(
        "UPDATE styles SET name=?, family=COALESCE(?, family), parent=COALESCE(?, parent),"
        " aliases=?, search_key=? WHERE id=?",
        (clean, family, parent, json.dumps(merged) if merged else None, norm_key(clean), slug),
    )
    return slug


def style_names(connection: sqlite3.Connection) -> Dict[str, str]:
    """Map style id -> display name for the whole vocabulary."""
    return {row["id"]: row["name"] for row in connection.execute("SELECT id, name FROM styles")}


def style_lookup(connection: sqlite3.Connection) -> Dict[str, str]:
    """Map every normalised name and alias -> style id (for typed searches)."""
    lookup: Dict[str, str] = {}
    for row in connection.execute("SELECT id, name, search_key, aliases FROM styles"):
        lookup[row["search_key"] or norm_key(row["name"])] = row["id"]
        for alias in json.loads(row["aliases"] or "[]"):
            key = norm_key(alias)
            if key:
                lookup.setdefault(key, row["id"])
    return lookup


# --------------------------------------------------------------------------- #
# writing styles onto tracks
# --------------------------------------------------------------------------- #

def _priority_sql(column: str) -> str:
    """SQL CASE that turns a source name into its rank (higher wins)."""
    whens = " ".join(f"WHEN '{name}' THEN {value}" for name, value in SOURCE_PRIORITY.items())
    return f"CASE {column} {whens} ELSE 0 END"


StyleEntry = Tuple[str, float, str, float, Optional[str]]


def add_track_styles(connection: sqlite3.Connection, rel_path: str,
                     entries: Sequence[StyleEntry]) -> int:
    """Attach styles to one track, honouring the source ranking.

    *entries* is a sequence of ``(style_id, weight, source, confidence, evidence)``.
    A lower-ranked source never overwrites a higher-ranked one for the same
    style (so re-running the rules cannot undo a human override), but it does
    overwrite it when it is at least as trusted - which is what makes the whole
    pipeline idempotent and safe to re-run.
    """
    written = 0
    for style_id, weight, source, confidence, evidence in entries:
        if not style_id:
            continue
        existing = connection.execute(
            "SELECT source FROM track_styles WHERE rel_path=? AND style_id=?",
            (rel_path, style_id),
        ).fetchone()
        if existing is not None and SOURCE_PRIORITY.get(existing["source"], 0) > \
                SOURCE_PRIORITY.get(source, 0):
            continue
        connection.execute(
            "INSERT INTO track_styles(rel_path, style_id, weight, source, confidence,"
            " evidence, updated_at) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(rel_path, style_id) DO UPDATE SET"
            "   weight=excluded.weight, source=excluded.source,"
            "   confidence=excluded.confidence, evidence=excluded.evidence,"
            "   updated_at=excluded.updated_at",
            (rel_path, style_id, float(weight), source, float(confidence), evidence, now()),
        )
        written += 1
    return written


def clear_track_styles(connection: sqlite3.Connection, sources: Sequence[str],
                       rel_paths: Optional[Iterable[str]] = None) -> int:
    """Drop the labels of the given sources (used before recomputing a layer)."""
    sources = list(sources)
    if not sources:
        return 0
    marks = ",".join("?" * len(sources))
    if rel_paths is None:
        cursor = connection.execute(
            f"DELETE FROM track_styles WHERE source IN ({marks})", sources)
        return cursor.rowcount or 0
    paths = list(rel_paths)
    if not paths:
        return 0
    total = 0
    # Chunked so a 41k-row library never hits SQLite's variable limit.
    for start in range(0, len(paths), 900):
        chunk = paths[start:start + 900]
        marks_paths = ",".join("?" * len(chunk))
        cursor = connection.execute(
            f"DELETE FROM track_styles WHERE source IN ({marks}) AND rel_path IN ({marks_paths})",
            sources + chunk,
        )
        total += cursor.rowcount or 0
    return total


def add_track_styles_bulk(connection: sqlite3.Connection,
                          mapping: Dict[str, Sequence[StyleEntry]]) -> int:
    """Write many tracks at once - the fast path used by the rules layer.

    Same ranking rules as :func:`add_track_styles`, but the existing rows are
    read in one query and the new ones are written with a single ``executemany``,
    which turns a minute of per-row statements into about a second.
    """
    if not mapping:
        return 0
    written = 0
    paths = list(mapping)
    for start in range(0, len(paths), 500):
        chunk = paths[start:start + 500]
        marks = ",".join("?" * len(chunk))
        existing: Dict[Tuple[str, str], str] = {}
        for row in connection.execute(
                f"SELECT rel_path, style_id, source FROM track_styles"
                f" WHERE rel_path IN ({marks})", chunk):
            existing[(row["rel_path"], row["style_id"])] = row["source"]
        stamp = now()
        payload: List[Tuple[Any, ...]] = []
        for rel_path in chunk:
            for style_id, weight, source, confidence, evidence in mapping[rel_path]:
                if not style_id:
                    continue
                known = existing.get((rel_path, style_id))
                if known is not None and SOURCE_PRIORITY.get(known, 0) > \
                        SOURCE_PRIORITY.get(source, 0):
                    continue
                payload.append((rel_path, style_id, float(weight), source,
                                float(confidence), evidence, stamp))
        if payload:
            connection.executemany(
                "INSERT INTO track_styles(rel_path, style_id, weight, source, confidence,"
                " evidence, updated_at) VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(rel_path, style_id) DO UPDATE SET"
                "   weight=excluded.weight, source=excluded.source,"
                "   confidence=excluded.confidence, evidence=excluded.evidence,"
                "   updated_at=excluded.updated_at",
                payload)
            written += len(payload)
    return written


# --------------------------------------------------------------------------- #
# derived columns and statistics
# --------------------------------------------------------------------------- #

def refresh_style_counts(connection: sqlite3.Connection) -> None:
    """Recompute ``styles.tracks`` / ``styles.artists`` (shown in the picker).

    Done with two aggregations into temporary indexed tables instead of one
    correlated subquery per style: with 45k labels the difference is seconds
    versus minutes.
    """
    connection.execute("UPDATE styles SET tracks=0, artists=0")
    connection.execute(
        "UPDATE styles SET tracks=(SELECT COUNT(*) FROM track_styles ts"
        " WHERE ts.style_id=styles.id)")
    connection.execute("DROP TABLE IF EXISTS temp._style_artists")
    connection.execute(
        "CREATE TEMP TABLE _style_artists AS"
        " SELECT ts.style_id AS sid, COUNT(DISTINCT t.artist_key) AS n"
        " FROM track_styles ts JOIN tracks t ON t.rel_path=ts.rel_path"
        " GROUP BY ts.style_id")
    connection.execute("CREATE INDEX temp.ix_style_artists ON _style_artists(sid)")
    connection.execute(
        "UPDATE styles SET artists=COALESCE((SELECT n FROM _style_artists a"
        " WHERE a.sid=styles.id), 0)")
    connection.execute("DROP TABLE temp._style_artists")


def refresh_track_primary(connection: sqlite3.Connection) -> None:
    """Recompute the denormalised ``primary_style``/``family`` of every track.

    The best label of a track is the heaviest one (each layer ranks its own
    labels), and specificity only breaks ties: among equally weighted labels a
    specific style beats one that is merely the family name ("Rock"), which
    beats the "(unspecified)" placeholders. That way a vague tag ("Rock") loses
    to a specific answer from the model, while a model that deliberately lists
    "Hip Hop" before "Trap" keeps its own order. The vaguer label is never
    removed - it stays a secondary label, so filtering by it still works.
    """
    connection.execute("DROP TABLE IF EXISTS temp._best_style")
    connection.execute(
        "CREATE TEMP TABLE _best_style AS SELECT rel_path, style_id, source, confidence"
        " FROM (SELECT ts.rel_path, ts.style_id, ts.source, ts.confidence,"
        "   ROW_NUMBER() OVER (PARTITION BY ts.rel_path ORDER BY"
        "     ts.weight DESC,"
        "     CASE WHEN s.name LIKE '%(unspecified)' THEN 2"
        "          WHEN s.name = s.family THEN 1 ELSE 0 END ASC,"
        f"   {_priority_sql('ts.source')} DESC, ts.confidence DESC, ts.style_id ASC) AS rn"
        "   FROM track_styles ts LEFT JOIN styles s ON s.id = ts.style_id) WHERE rn = 1")
    connection.execute("CREATE INDEX temp.ix_best_style ON _best_style(rel_path)")
    connection.execute(
        "DROP TABLE IF EXISTS temp._best_family")
    connection.execute(
        "CREATE TEMP TABLE _best_family AS"
        " SELECT b.rel_path AS rel_path, s.family AS family, b.style_id AS style_id,"
        "        b.source AS source, b.confidence AS confidence"
        " FROM _best_style b LEFT JOIN styles s ON s.id = b.style_id")
    connection.execute("CREATE INDEX temp.ix_best_family ON _best_family(rel_path)")
    connection.execute(
        "UPDATE tracks SET"
        "  primary_style=(SELECT f.style_id FROM _best_family f"
        "                 WHERE f.rel_path=tracks.rel_path),"
        "  label_source =(SELECT f.source FROM _best_family f"
        "                 WHERE f.rel_path=tracks.rel_path),"
        "  confidence   =(SELECT f.confidence FROM _best_family f"
        "                 WHERE f.rel_path=tracks.rel_path),"
        "  family       =(SELECT f.family FROM _best_family f"
        "                 WHERE f.rel_path=tracks.rel_path)"
    )
    connection.execute("DROP TABLE temp._best_family")
    connection.execute("DROP TABLE temp._best_style")


def rebuild_artist_rollup(connection: sqlite3.Connection) -> int:
    """Refresh the artist table from the tracks (styles, families, counts)."""
    connection.execute(
        "INSERT INTO artists(key, name, tracks, albums, updated_at)"
        " SELECT artist_key, MIN(album_artist), COUNT(*), COUNT(DISTINCT album_key), ?"
        " FROM tracks WHERE artist_key IS NOT NULL AND artist_key <> ''"
        " GROUP BY artist_key"
        " ON CONFLICT(key) DO UPDATE SET name=COALESCE(artists.name, excluded.name),"
        "   tracks=excluded.tracks, albums=excluded.albums, updated_at=excluded.updated_at",
        (now(),),
    )
    count = connection.execute("SELECT COUNT(*) AS n FROM artists").fetchone()["n"]
    return int(count)


def dataset_stats(connection: sqlite3.Connection) -> Dict[str, Any]:
    """Everything the reports and the status output need, in one query batch."""
    one = lambda sql, *args: connection.execute(sql, args).fetchone()[0]  # noqa: E731

    styles_total = one("SELECT COUNT(*) FROM styles")
    tracks_total = one("SELECT COUNT(*) FROM tracks")
    stats: Dict[str, Any] = {
        "tracks": tracks_total,
        "tracks_linked": one("SELECT COUNT(*) FROM tracks WHERE id IS NOT NULL AND linked_at IS NOT NULL"),
        "tracks_with_raw_genre": one("SELECT COUNT(*) FROM tracks WHERE raw_genre IS NOT NULL AND TRIM(raw_genre) <> ''"),
        "tracks_with_style": one("SELECT COUNT(DISTINCT rel_path) FROM track_styles"),
        "tracks_multi_style": one(
            "SELECT COUNT(*) FROM (SELECT rel_path FROM track_styles GROUP BY rel_path HAVING COUNT(*) > 1)"),
        "tracks_unlabelled": one(
            "SELECT COUNT(*) FROM tracks t WHERE NOT EXISTS"
            " (SELECT 1 FROM track_styles ts WHERE ts.rel_path=t.rel_path)"),
        "styles": styles_total,
        "styles_used": one("SELECT COUNT(*) FROM styles WHERE tracks > 0"),
        "style_links": one("SELECT COUNT(*) FROM track_styles"),
        "raw_genres": one("SELECT COUNT(*) FROM raw_genres"),
        "raw_unresolved": one("SELECT COUNT(*) FROM raw_genres WHERE decision IN ('unresolved','partial')"),
        "artists": one("SELECT COUNT(*) FROM artists"),
        "albums": one("SELECT COUNT(DISTINCT album_key) FROM tracks WHERE album_key IS NOT NULL"),
        "families": one("SELECT COUNT(DISTINCT family) FROM styles WHERE family IS NOT NULL"),
        "llm_cost": one("SELECT COALESCE(SUM(cost_usd), 0) FROM api_calls"),
        "llm_calls": one("SELECT COUNT(*) FROM api_calls"),
    }
    stats["tracks_without_style_pct"] = round(
        100.0 * stats["tracks_unlabelled"] / max(1, tracks_total), 1)
    return stats


def styles_by_source(connection: sqlite3.Connection) -> List[sqlite3.Row]:
    """How many labels each layer contributed (for the report)."""
    return list(connection.execute(
        "SELECT source, COUNT(*) AS links, COUNT(DISTINCT rel_path) AS tracks"
        " FROM track_styles GROUP BY source ORDER BY links DESC"))


def top_styles(connection: sqlite3.Connection, limit: int = 25) -> List[sqlite3.Row]:
    return list(connection.execute(
        "SELECT id, name, family, tracks, artists FROM styles WHERE tracks > 0"
        " ORDER BY tracks DESC LIMIT ?", (int(limit),)))


def log(message: str, *parts: Any) -> None:
    """One-line progress/log output (the scripts are meant to be watched)."""
    stamp = time.strftime("%H:%M:%S")
    extra = " ".join(str(part) for part in parts)
    print(f"[{stamp}] {message}{(' ' + extra) if extra else ''}", flush=True)


def human(number: Any) -> str:
    """Thousands separator for the reports."""
    try:
        return f"{int(number):,}".replace(",", ".")
    except (TypeError, ValueError):
        return str(number)


class Progress:
    """Throttled progress line for the long scans.

    The steps are meant to be run in a terminal and watched, so they print one
    line that overwrites itself instead of thousands of log lines.
    """

    def __init__(self, total: int, label: str = "", every: float = 1.0) -> None:
        self.total = max(0, int(total))
        self.label = label
        self.every = every
        self.done = 0
        self.started = time.time()
        self._last = 0.0

    def step(self, count: int = 1, *, force: bool = False, note: str = "") -> None:
        self.done += count
        moment = time.time()
        if not force and moment - self._last < self.every and self.done < self.total:
            return
        self._last = moment
        elapsed = max(1e-6, moment - self.started)
        rate = self.done / elapsed
        percent = 100.0 * self.done / self.total if self.total else 100.0
        remaining = (self.total - self.done) / rate if rate > 0 else 0.0
        tail = f" {note}" if note else ""
        sys.stdout.write(
            f"\r  {self.label} {self.done}/{self.total} ({percent:5.1f}%) "
            f"{rate:6.1f}/s  eta {remaining/60:5.1f} min{tail}    "
        )
        sys.stdout.flush()

    def finish(self) -> None:
        self.step(0, force=True)
        sys.stdout.write("\n")
        sys.stdout.flush()


def print_status(connection: sqlite3.Connection) -> None:
    """Human-readable state of the dataset (also ``python3 styles_db.py``)."""
    stats = dataset_stats(connection)
    log("dataset:", str(connection.execute("PRAGMA database_list").fetchone()["file"]))
    if not stats["tracks"]:
        log("empty - run:  python3 step1_scan.py")
        return
    log(f"tracks            {human(stats['tracks'])}"
        f"  (linked to Jellyfin: {human(stats['tracks_linked'])})")
    log(f"with a raw genre  {human(stats['tracks_with_raw_genre'])}")
    log(f"with a style      {human(stats['tracks_with_style'])}"
        f"  ({human(stats['tracks_multi_style'])} have more than one)")
    log(f"no style at all   {human(stats['tracks_unlabelled'])}"
        f"  ({stats['tracks_without_style_pct']} %)")
    log(f"styles in use     {human(stats['styles_used'])} of {human(stats['styles'])}"
        f"  ({human(stats['style_links'])} labels)")
    log(f"raw genre strings {human(stats['raw_genres'])}"
        f"  (unresolved: {human(stats['raw_unresolved'])})")
    log(f"artists / albums  {human(stats['artists'])} / {human(stats['albums'])}")
    if stats["llm_calls"]:
        log(f"LLM calls         {human(stats['llm_calls'])}"
            f"  (cost {stats['llm_cost']:.4f} USD)")
    rows = styles_by_source(connection)
    if rows:
        log("labels by source: " + ", ".join(
            f"{row['source']}={human(row['tracks'])}" for row in rows))
    rows = top_styles(connection, 15)
    if rows:
        log("biggest styles:")
        for row in rows:
            family = f" [{row['family']}]" if row["family"] else ""
            print(f"      {row['name']}{family} - {human(row['tracks'])} tracks")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python3 styles_db.py`` prints the state of the dataset."""
    import argparse

    parser = argparse.ArgumentParser(description="Show the state of the style dataset")
    parser.add_argument("--db", default="", help="dataset path (default: from config.json)")
    args = parser.parse_args(list(argv) if argv is not None else None)

    config = load_config()
    path = resolve(args.db) if args.db else db_path(config)
    if not path.exists():
        log("no dataset yet at", str(path))
        log("run:  python3 step1_scan.py")
        return 1
    connection = connect(path, create=False)
    try:
        print_status(connection)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

