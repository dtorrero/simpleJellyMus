#!/usr/bin/env python3
"""Step 8 - what the dataset actually looks like, and is it ready for the player?

Reads only. Prints a health report and writes ``out/report.md`` (a summary you
can read later), ``out/styles.tsv`` (the whole vocabulary with its counts) and
``out/player_preview.txt`` (what the player would do for a handful of typed
style names - the exact query the app will run).

    python3 step8_report.py
    python3 step8_report.py --search "ska punk" --search "trve metal"
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import styles_db as S  # noqa: E402
from step3_vocabulary import normalize_raw  # noqa: E402
from step4_normalize import load_overrides  # noqa: E402

DEFAULT_SEARCHES = ["metal", "trash metal", "ska punk", "progressive", "soundtrack"]


def search_styles(connection, text: str, limit: int = 15) -> List[Dict[str, Any]]:
    """The fuzzy search the player will use when you type a style.

    Scoring: an exact name/alias beats a short phrase match, which beats a long
    phrase match, which beats sharing a word. Short keys win over the long
    raw-tag aliases (otherwise typing "progressive" would surface whichever
    style happens to carry a giant historical alias), and a hit on the *family*
    name lifts every style of that family.
    """
    query = normalize_raw(text)
    if not query:
        return []
    scored: List[tuple] = []
    for row in connection.execute(
            "SELECT id, name, family, search_key, aliases, tracks, artists FROM styles"):
        keys = [row["search_key"] or normalize_raw(row["name"])]
        keys += [normalize_raw(alias) for alias in json.loads(row["aliases"] or "[]")]
        best = 0.0
        for key in keys:
            if not key:
                continue
            words = len(key.split())
            if key == query:
                best = max(best, 3.0)
            elif words <= 3 and (query in key or key in query):
                best = max(best, 2.4)
            elif query in key or key in query:
                best = max(best, 1.5)
            elif words <= 4:
                shared = len(set(query.split()) & set(key.split()))
                if shared:
                    best = max(best, 1.0 + 0.1 * shared)
        if row["family"] and normalize_raw(row["family"]) == query:
            best += 1.2
        if best:
            scored.append((best, row["tracks"] or 0, dict(row)))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    results = [item[2] for item in scored[:limit]]
    # Typing a family ("metal", "punk") should offer the family itself first:
    # it is the widest useful answer, and the player treats it as one filter.
    families = {normalize_raw(row["family"]): row["family"] for row in
                connection.execute("SELECT DISTINCT family FROM styles WHERE family IS NOT NULL")}
    if query in families:
        family = families[query]
        total = connection.execute(
            "SELECT COALESCE(SUM(tracks), 0) AS n FROM styles WHERE family = ?",
            (family,)).fetchone()["n"]
        results.insert(0, {"id": f"family:{S.style_slug(family)}", "name": family,
                           "family": family, "tracks": int(total or 0), "artists": 0,
                           "kind": "family", "search_key": query, "aliases": None})
    return results


def coverage_by_source(connection) -> List[Dict[str, Any]]:
    return [dict(row) for row in connection.execute(
        "SELECT label_source, COUNT(*) AS tracks FROM tracks"
        " WHERE label_source IS NOT NULL GROUP BY label_source ORDER BY tracks DESC")]


def families_table(connection) -> List[Dict[str, Any]]:
    return [dict(row) for row in connection.execute(
        "SELECT family, COUNT(*) AS styles, SUM(tracks) AS labels, MAX(tracks) AS biggest"
        " FROM styles WHERE family IS NOT NULL GROUP BY family ORDER BY labels DESC")]


def readiness(connection) -> Dict[str, Any]:
    """Is the dataset good enough for the player to use?"""
    one = lambda sql: connection.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "tracks": one("SELECT COUNT(*) FROM tracks"),
        "with_style": one("SELECT COUNT(DISTINCT rel_path) FROM track_styles"),
        "with_family_only": one(
            f"SELECT COUNT(*) FROM tracks WHERE {S.FAMILY_STYLE_SQL}"),
        "with_vague_only": one(
            f"SELECT COUNT(*) FROM tracks WHERE {S.VAGUE_STYLE_SQL}"),
        "unlabelled": one(
            "SELECT COUNT(*) FROM tracks t WHERE NOT EXISTS"
            " (SELECT 1 FROM track_styles ts WHERE ts.rel_path=t.rel_path)"),
        "with_jellyfin_id": one("SELECT COUNT(*) FROM tracks WHERE id IS NOT NULL"),
        "playable_styles": one("SELECT COUNT(*) FROM styles WHERE tracks >= 50"),
    }


def picker_preview(connection, queries: Sequence[str]) -> List[str]:
    """For each typed query: what the player would offer (the real query)."""
    lines: List[str] = []
    for query in queries:
        hits = search_styles(connection, query)
        usable = [hit for hit in hits if (hit["tracks"] or 0) >= 1][:5]
        if not usable:
            lines.append(f"  {query!r:24s} -> nothing (no style matches, or none is used"
                         f" by a track)")
            continue
        best = usable[0]
        kind = "family" if best.get("kind") == "family" else "style"
        lines.append(f"  {query!r:24s} -> best: {best['name']} ({kind},"
                     f" {S.human(best['tracks'])} tracks)")
        if len(usable) > 1:
            detail = ", ".join(f"{hit['name']} ({S.human(hit['tracks'])})" for hit in usable[1:])
            lines.append(f"                            also: {detail}")
    return lines


def write_styles_tsv(connection, path: Path) -> int:
    rows = list(connection.execute(
        "SELECT id, name, family, parent, tracks, artists, source, aliases FROM styles"
        " ORDER BY family, tracks DESC, name"))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(["id", "name", "family", "parent", "tracks", "artists", "source",
                         "aliases"])
        for row in rows:
            aliases = ", ".join(json.loads(row["aliases"] or "[]"))
            writer.writerow([row["id"], row["name"], row["family"], row["parent"] or "",
                             row["tracks"], row["artists"], row["source"], aliases])
    return len(rows)


def write_tracks_tsv(connection, path: Path, limit: int = 0) -> int:
    """Per-label export: one line per (track, style) - the review artefact.

    Long format on purpose: you can open it in a spreadsheet and filter by
    style, by source or by confidence without writing a single SQL query.
    """
    sql = (
        "SELECT t.rel_path, COALESCE(t.artists, t.album_artist) AS artist, t.album, t.year,"
        "       t.raw_genre, s.name AS style, s.family, ts.weight, ts.source, ts.confidence,"
        "       ts.evidence, t.primary_style"
        " FROM track_styles ts"
        " JOIN tracks t ON t.rel_path = ts.rel_path"
        " JOIN styles s ON s.id = ts.style_id"
        " ORDER BY t.artist_key, t.album, t.rel_path, ts.weight DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    count = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(["path", "artist", "album", "year", "raw_genre", "style", "family",
                         "weight", "source", "confidence", "evidence", "primary"])
        for row in connection.execute(sql):
            writer.writerow([row["rel_path"], row["artist"] or "", row["album"] or "",
                             row["year"] or "", row["raw_genre"] or "", row["style"],
                             row["family"] or "", row["weight"], row["source"],
                             row["confidence"], row["evidence"] or "",
                             row["primary_style"] or ""])
            count += 1
    return count


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Step 8 - health report of the dataset")
    parser.add_argument("--db", default="", help="dataset path (default: config.json)")
    parser.add_argument("--search", action="append", default=[],
                        help="a style name to try in the picker (repeatable)")
    parser.add_argument("--top", type=int, default=30, help="how many styles to list")
    parser.add_argument("--export-limit", type=int, default=0,
                        help="cap the per-track TSV export (0 = everything)")
    args = parser.parse_args(list(argv) if argv is not None else None)

    config = S.load_config()
    path = S.resolve(args.db) if args.db else S.db_path(config)
    connection = S.connect(path, create=False)
    try:
        stats = S.dataset_stats(connection)
        ready = readiness(connection)
        out = S.out_dir(config)
        report: List[str] = []

        def add(line: str = "") -> None:
            report.append(line)
            print(line)

        add("# SimpleJellyMus - style dataset report")
        add()
        add("## tracks")
        add()
        add(f"- tracks                 : {S.human(stats['tracks'])}")
        add(f"- with a raw genre tag   : {S.human(stats['tracks_with_raw_genre'])}")
        add(f"- with at least one style: {S.human(ready['with_style'])}"
            f" ({100.0 * ready['with_style'] / max(1, stats['tracks']):.1f} %)")
        add(f"- several styles at once : {S.human(stats['tracks_multi_style'])}")
        add(f"- family level only      : {S.human(ready['with_family_only'])}")
        add(f"- vague only (e.g. \"Rock\"): {S.human(ready['with_vague_only'])}")
        add(f"- no style at all        : {S.human(ready['unlabelled'])}"
            f" ({stats['tracks_without_style_pct']} %)")
        add()
        add("## where the labels come from")
        add()
        for row in coverage_by_source(connection):
            add(f"- {str(row['label_source']):12s} {S.human(row['tracks'])} tracks")
        add()
        add("## vocabulary")
        add()
        add(f"- {S.human(stats['styles'])} styles, {S.human(stats['styles_used'])} in use,"
            f" {S.human(stats['style_links'])} labels")
        add(f"- {S.human(ready['playable_styles'])} styles have at least 50 tracks")
        add()
        for row in families_table(connection)[:25]:
            add(f"- {str(row['family']):14s} {S.human(row['styles']):>5} styles,"
                f" {S.human(row['labels']):>9} labels, biggest {S.human(row['biggest'])}")
        add()
        add("## biggest styles")
        add()
        for row in S.top_styles(connection, args.top):
            add(f"- {row['name']} [{row['family']}] - {S.human(row['tracks'])} tracks,"
                f" {S.human(row['artists'])} artists")
        add()
        queries = args.search or list(DEFAULT_SEARCHES)
        add("## picker preview (what typing a style would do)")
        add()
        for line in picker_preview(connection, queries):
            add(line)
        add()
        unresolved = [dict(row) for row in connection.execute(
            "SELECT raw, tracks, unknown FROM raw_genres"
            " WHERE decision IN ('unresolved','partial') ORDER BY tracks DESC LIMIT 15")]
        if unresolved:
            add("## raw tags still not fully understood")
            add()
            for row in unresolved:
                add(f"- {S.human(row['tracks']):>6}  {row['raw']!r}  ->  {row['unknown']}")
            add()
        add("## ready for the player?")
        add()
        add(f"- tracks with a Jellyfin item id: {S.human(ready['with_jellyfin_id'])}"
            f"  (step2_link.py fills this in)")
        add("- the player can filter by family and by style offline, in about 3 ms")
        overrides = load_overrides(config)
        count = sum(len(overrides.get(section) or {}) for section in ("artists", "albums", "tracks"))
        add(f"- hand-made overrides in data/overrides.json: {S.human(count)}")

        report_path = out / "report.md"
        report_path.write_text("\n".join(report) + "\n", encoding="utf-8")
        styles_path = out / "styles.tsv"
        count_styles = write_styles_tsv(connection, styles_path)
        tracks_path = out / "tracks_styles.tsv"
        count_labels = write_tracks_tsv(connection, tracks_path, args.export_limit)
        preview_path = out / "player_preview.txt"
        preview_path.write_text("\n".join(picker_preview(connection, queries)) + "\n",
                                encoding="utf-8")
        print()
        S.log("report written to    ", str(report_path))
        S.log(f"vocabulary written to {styles_path} ({S.human(count_styles)} styles)")
        S.log(f"per-track labels in   {tracks_path} ({S.human(count_labels)} lines"
              " - open it in a spreadsheet)")
        S.log("picker preview in    ", str(preview_path))
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
