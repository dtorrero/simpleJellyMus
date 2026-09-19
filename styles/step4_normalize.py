#!/usr/bin/env python3
"""Step 4 - turn the raw genre tags into canonical styles.

Reads every distinct genre string found by step 1, applies the vocabulary
(``data/styles.json``), the rules (``data/rules.json``) and your overrides
(``data/overrides.json``), and writes one or more styles per track into the
dataset. Nothing is ever written to a music file, and every raw string is kept
verbatim in ``tracks.raw_genre`` so the mapping can always be redone.

    python3 step4_normalize.py              # apply the mapping
    python3 step4_normalize.py --dry-run    # report only, write nothing
    python3 step4_normalize.py --report     # how the tags were understood

Sources, strongest first (styles_db.SOURCE_PRIORITY):

    human  - data/overrides.json
    tag    - the raw tag itself, when it is already a canonical style name
    rules  - this step: split, clean, translate, fall back to a family
    propagated / audio / external / llm - the later steps
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import styles_db as S  # noqa: E402
from step3_vocabulary import (load_rules, load_vocabulary, normalize_raw,  # noqa: E402
                              split_parts, vocabulary_keys, styles_file)

MAX_REPLACE_PASSES = 4
FUZZY_CUTOFF = 0.88


class Normalizer:
    """Maps raw genre strings onto canonical styles (pure, testable)."""

    def __init__(self, connection, config: Dict[str, Any]) -> None:
        self.config = config
        self.connection = connection
        self.vocabulary = load_vocabulary(config)
        self.rules = load_rules(config)

        self.style_names: Dict[str, str] = S.style_names(connection)
        self.style_families: Dict[str, str] = {
            row["id"]: (row["family"] or "")
            for row in connection.execute("SELECT id, family FROM styles")
        }
        self.keys: Dict[str, str] = {}
        for row in connection.execute("SELECT id, name, search_key, aliases FROM styles"):
            self.keys[row["search_key"] or normalize_raw(row["name"])] = row["id"]
            for alias in json.loads(row["aliases"] or "[]"):
                key = normalize_raw(alias)
                if key:
                    self.keys.setdefault(key, row["id"])
        # The vocabulary file is the source of truth; the table can lag behind it.
        for key, name in vocabulary_keys(self.vocabulary).items():
            self.keys.setdefault(key, S.style_slug(name))

        self.family_names: Dict[str, str] = {
            normalize_raw(name): str(name) for name in (self.vocabulary.get("families") or [])
        }

        self.drop = {normalize_raw(value) for value in (self.rules.get("drop") or [])}
        self.drop_parts = {normalize_raw(value) for value in (self.rules.get("drop_parts") or [])}
        self.strip_words = sorted(
            (normalize_raw(value) for value in (self.rules.get("strip_words") or [])),
            key=len, reverse=True)
        self.strip_regex = [re.compile(pattern, re.IGNORECASE)
                            for pattern in (self.rules.get("strip_regex") or [])]
        self.replace = {normalize_raw(key): normalize_raw(value)
                        for key, value in (self.rules.get("replace") or {}).items()}
        self.expand = {normalize_raw(key): [str(name) for name in (value or [])]
                       for key, value in (self.rules.get("expand") or {}).items()}
        self.force = {normalize_raw(key): value
                      for key, value in (self.rules.get("force") or {}).items()}
        self.family_only = {normalize_raw(value) for value in (self.rules.get("family_only") or [])}
        self.family_keywords = {normalize_raw(key): str(value)
                                for key, value in (self.rules.get("family_keywords") or {}).items()}
        # Words that may be dropped from a piece to find the style underneath
        # ("melodic", "technical", "old school", ...). Only used as a fallback,
        # so a real style name that contains them is never mangled.
        self.modifiers = {normalize_raw(value) for value in (self.rules.get("modifiers") or [])}
        self._keys_by_length = sorted(self.keys, key=len, reverse=True)
        self.max_styles = int(S.setting(config, "normalize.max_styles_per_track", 6) or 6)
        self._family_styles: Dict[str, str] = {}

    # ------------------------------------------------------------------ helpers
    def family_style_id(self, family: str) -> str:
        """Create (once) the placeholder style that means 'family only'."""
        if family in self._family_styles:
            return self._family_styles[family]
        style_id = S.upsert_style(
            self.connection, f"{family} (unspecified)", family=family,
            aliases=[], source="rules")
        self.style_names[style_id] = f"{family} (unspecified)"
        self.style_families[style_id] = family
        self._family_styles[family] = style_id
        return style_id

    def clean_piece(self, piece: str) -> str:
        """Normalise one piece: strip regexes, translate, drop filler words."""
        text = piece
        for pattern in self.strip_regex:
            text = pattern.sub(" ", text)
        key = normalize_raw(text)
        if not key:
            return ""
        for _ in range(MAX_REPLACE_PASSES):
            replacement = self.replace.get(key)
            if replacement is None or replacement == key:
                break
            key = replacement
        for word in self.strip_words:
            if not word or word not in key:
                continue
            pattern = re.compile(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])")
            stripped = pattern.sub(" ", key)
            if stripped != key:
                key = " ".join(stripped.split())
        return key

    # ------------------------------------------------------------------- lookup
    def lookup(self, key: str) -> Tuple[Optional[str], str, float]:
        """Resolve one cleaned piece -> (style id, how, confidence)."""
        if not key:
            return None, "", 0.0
        if key in self.drop_parts:
            return None, "modifier", 0.0
        if key in self.family_only and key in self.family_names:
            return self.family_style_id(self.family_names[key]), "family", 0.8
        if key in self.keys:
            return self.keys[key], "exact", 0.9
        if key in self.family_names:
            return self.family_style_id(self.family_names[key]), "family", 0.85
        # 1. the same name without one of its descriptive words
        words = key.split()
        if len(words) > 1 and self.modifiers:
            for word in words:
                if word not in self.modifiers:
                    continue
                reduced = " ".join(part for part in words if part != word)
                if reduced in self.keys:
                    return self.keys[reduced], f"without:{word}", 0.8
        # 2. the longest known style name contained in the piece
        if len(words) > 1:
            for candidate in self._keys_by_length:
                if len(candidate.split()) < 2:
                    continue
                if f" {candidate} " in f" {key} ":
                    return self.keys[candidate], f"part-of:{candidate}", 0.7
        # 3. close enough spelling
        candidates = difflib.get_close_matches(key, list(self.keys), n=1, cutoff=FUZZY_CUTOFF)
        if candidates:
            return self.keys[candidates[0]], f"fuzzy:{candidates[0]}", 0.6
        # 4. last resort: a word that tells us at least the family
        for keyword, family in self.family_keywords.items():
            if keyword and keyword in key:
                return self.family_style_id(family), f"keyword:{keyword}", 0.4
        return None, "", 0.0

    def map_raw(self, raw: str) -> Tuple[List[S.StyleEntry], str, List[str]]:
        """Map one raw genre tag.

        Returns ``(entries, decision, unresolved_pieces)`` where every entry is
        ``(style_id, weight, source, confidence, evidence)``.
        """
        raw = (raw or "").strip()
        if not raw:
            return [], "empty", []
        text = raw
        for pattern in self.strip_regex:
            text = pattern.sub(" ", text)
        whole = normalize_raw(text)
        if not whole:
            return [], "dropped", []
        if whole in self.drop:
            return [], "dropped", []
        if whole in self.force:
            entries = []
            for name in self.force[whole]:
                style_id = self.keys.get(normalize_raw(name)) or S.style_slug(name)
                entries.append((style_id, 1.0, "rules", 0.8, f"forced:{whole}"))
            return entries[:self.max_styles], "forced", []

        parts = split_parts(raw)
        # A tag that consists of one single piece and already *is* a canonical
        # style is the strongest evidence there is: keep it as source "tag".
        if len(parts) == 1:
            key = self.clean_piece(parts[0])
            if key in self.keys:
                return ([(self.keys[key], 1.0, "tag", 0.95, f"{parts[0]!r} is already a style")],
                        "exact", [])
            if key in self.family_names and key in self.family_only:
                return ([(self.family_style_id(self.family_names[key]), 0.6, "tag", 0.9,
                          f"{parts[0]!r} states a family")], "exact_family", [])
        entries: List[S.StyleEntry] = []
        unresolved: List[str] = []
        seen: set = set()
        for index, part in enumerate(parts):
            if normalize_raw(part) in self.drop_parts:
                continue
            key = self.clean_piece(part)
            if not key or key in self.drop_parts:
                continue
            expanded = self.expand.get(key)
            if expanded:
                for name in expanded:
                    style_id = self.keys.get(normalize_raw(name)) or S.style_slug(name)
                    if style_id in seen:
                        continue
                    seen.add(style_id)
                    weight = 1.0 if not entries else 0.7
                    entries.append((style_id, weight, "rules", 0.7,
                                    f"{part} -> expand {name}"))
                continue
            style_id, how, confidence = self.lookup(key)
            if style_id is None:
                unresolved.append(part)
                continue
            if style_id in seen:
                continue
            seen.add(style_id)
            family_level = how.startswith("family") or how.startswith("keyword")
            if family_level:
                # A family-level label is deliberately weak: any real style - from
                # a tag, from the audio classifier or from a human - becomes the
                # primary style of the track, while a track that only ever states
                # its family still keeps that as its label.
                weight = 0.6
            else:
                weight = 1.0 if not entries else 0.7
            if index >= self.max_styles:
                continue
            entries.append((style_id, weight, "rules", confidence, f"{part} -> {how}"))

        if not entries:
            decision = "unresolved" if unresolved else "dropped"
        elif unresolved:
            decision = "partial"
        elif len(entries) > 1:
            decision = "split"
        else:
            decision = "mapped"
        return entries[:self.max_styles], decision, unresolved


# --------------------------------------------------------------------------- #
# overrides (always win)
# --------------------------------------------------------------------------- #

def load_overrides(config: Dict[str, Any]) -> Dict[str, Any]:
    path = S.resolve(S.setting(config, "normalize.overrides_file", "data/overrides.json"))
    if not path.exists():
        return {"artists": {}, "albums": {}, "tracks": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit(f"{path} is not valid JSON: {exc}")
    return data if isinstance(data, dict) else {}


def _resolve_names(normalizer: Normalizer, names: Iterable[str],
                   errors: List[str], where: str) -> List[str]:
    resolved = []
    for name in names or []:
        key = normalize_raw(name)
        style_id = normalizer.keys.get(key)
        if style_id is None:
            errors.append(f"{where}: unknown style {name!r}")
            continue
        resolved.append(style_id)
    return resolved


def apply_overrides(normalizer: Normalizer, connection, config: Dict[str, Any],
                    *, dry_run: bool) -> Tuple[int, List[str]]:
    """Write the human verdicts into the dataset (source ``human``)."""
    overrides = load_overrides(config)
    errors: List[str] = []
    by_artist: Dict[str, List[str]] = {}
    by_album: Dict[str, List[str]] = {}
    by_track: Dict[str, List[str]] = {}
    for name, entry in (overrides.get("artists") or {}).items():
        styles = _resolve_names(normalizer, (entry or {}).get("styles") or [],
                                errors, f"artists[{name!r}]")
        by_artist[S.norm_key(name)] = styles
    for name, entry in (overrides.get("albums") or {}).items():
        styles = _resolve_names(normalizer, (entry or {}).get("styles") or [],
                                errors, f"albums[{name!r}]")
        by_album[S.norm_key(name)] = styles
    for path, entry in (overrides.get("tracks") or {}).items():
        styles = _resolve_names(normalizer, (entry or {}).get("styles") or [],
                                errors, f"tracks[{path!r}]")
        by_track[str(path)] = styles
    if not (by_artist or by_album or by_track):
        return 0, errors

    touched: List[str] = []
    plan: List[Tuple[str, List[str]]] = []
    for row in connection.execute("SELECT rel_path, artist_key, album_key FROM tracks"):
        styles = by_track.get(row["rel_path"])
        if styles is None:
            styles = by_album.get(row["album_key"] or "")
        if styles is None:
            styles = by_artist.get(row["artist_key"] or "")
        if styles is None:
            continue
        touched.append(row["rel_path"])
        plan.append((row["rel_path"], styles))
    if dry_run:
        return len(touched), errors

    # An empty style list in overrides.json means "no style at all": drop every
    # label of that track, whatever layer produced it.
    cleared = [track_id for track_id, styles in plan if not styles]
    if cleared:
        S.clear_track_styles(connection, list(S.SOURCE_PRIORITY), cleared)
    written = [pair for pair in plan if pair[1]]
    if written:
        S.clear_track_styles(connection, ["human"], [track_id for track_id, _ in written])
    for track_id, style_ids in written:
        entries = [(style_id, 1.0 if index == 0 else 0.7, "human", 1.0, "overrides.json")
                   for index, style_id in enumerate(style_ids)]
        S.add_track_styles(connection, track_id, entries)
    return len(plan), errors


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #

def normalize_library(normalizer: Normalizer, connection, config: Dict[str, Any], *,
                      dry_run: bool = False, only_unresolved: bool = False) -> Dict[str, Any]:
    """Map every distinct raw genre string and write the labels onto the tracks."""
    tracks_by_raw: Dict[str, List[str]] = defaultdict(list)
    for row in connection.execute(
            "SELECT rel_path, raw_genre FROM tracks WHERE TRIM(COALESCE(raw_genre, '')) <> ''"):
        tracks_by_raw[row["raw_genre"]].append(row["rel_path"])

    known: Dict[str, Any] = {}
    for row in connection.execute("SELECT raw, decision FROM raw_genres"):
        known[row["raw"]] = row["decision"]
    for raw in tracks_by_raw:
        known.setdefault(raw, None)

    settled = ("mapped", "split", "exact", "exact_family", "forced", "dropped")
    if only_unresolved:
        work = [raw for raw in tracks_by_raw if known.get(raw) not in settled]
    else:
        work = list(tracks_by_raw)

    decisions: Counter = Counter()
    unresolved_pieces: Counter = Counter()
    examples: Dict[str, set] = defaultdict(set)
    style_hits: Counter = Counter()
    pending: Dict[str, Sequence[S.StyleEntry]] = {}
    tagged = labelled = 0

    progress = S.Progress(len(work), label="mapping tags")
    if not dry_run and not only_unresolved:
        S.clear_track_styles(connection, ["rules", "tag"])
    for raw in work:
        ids = tracks_by_raw.get(raw, [])
        entries, decision, unresolved = normalizer.map_raw(raw)
        progress.step()
        decisions[decision] += len(ids)
        for entry in entries:
            style_hits[entry[0]] += len(ids)
        for piece in unresolved:
            unresolved_pieces[piece] += len(ids)
            examples[piece].add(raw)
        if ids:
            tagged += len(ids)
            if entries:
                labelled += len(ids)
        if dry_run:
            continue
        for rel_path in ids:
            pending[rel_path] = entries
        if len(pending) >= 2000:
            if only_unresolved:
                S.clear_track_styles(connection, ["rules", "tag"], list(pending))
            S.add_track_styles_bulk(connection, pending)
            connection.commit()
            pending = {}
        connection.execute(
            "UPDATE raw_genres SET styles=?, unknown=?, decision=?, updated_at=? WHERE raw=?",
            (json.dumps([entry[0] for entry in entries]) if entries else None,
             json.dumps(unresolved) if unresolved else None,
             decision, S.now(), raw),
        )
    progress.finish()
    if pending:
        if only_unresolved:
            S.clear_track_styles(connection, ["rules", "tag"], list(pending))
        S.add_track_styles_bulk(connection, pending)
        connection.commit()
    return {
        "tagged_tracks": tagged,
        "labelled_tracks": labelled,
        "decisions": decisions,
        "unresolved": unresolved_pieces,
        "examples": examples,
        "styles": style_hits,
    }


def write_report(connection, config: Dict[str, Any], result: Dict[str, Any]) -> Path:
    """Human-readable report of how the tags were understood."""
    listing = S.out_dir(config) / "normalize_report.txt"
    names = S.style_names(connection)
    with listing.open("w", encoding="utf-8") as handle:
        handle.write("# how the raw genre tags were mapped\n\n## decisions (tracks)\n")
        for decision, count in result["decisions"].most_common():
            handle.write(f"{count:8d}  {decision}\n")
        handle.write("\n## distinct tags per decision\n")
        for row in connection.execute(
                "SELECT decision, COUNT(*) AS n FROM raw_genres GROUP BY decision ORDER BY n DESC"):
            handle.write(f"{row['n']:8d}  {row['decision']}\n")
        handle.write("\n## styles found (tracks)\n")
        for style_id, count in result["styles"].most_common():
            handle.write(f"{count:8d}  {names.get(style_id, style_id)}  [{style_id}]\n")
        handle.write("\n## unresolved pieces (tracks / piece / example tags)\n")
        for piece, count in result["unresolved"].most_common():
            sample = "; ".join(sorted(result["examples"][piece])[:3])
            handle.write(f"{count:8d}  {piece}\t{sample}\n")
    return listing


def print_summary(connection, result: Dict[str, Any], listing: Path) -> None:
    stats = S.dataset_stats(connection)
    print()
    S.log(f"tracks with a raw tag    {S.human(result['tagged_tracks'])}")
    S.log(f"…of those, with a style  {S.human(result['labelled_tracks'])}"
          f" ({100.0 * result['labelled_tracks'] / max(1, result['tagged_tracks']):.1f} %)")
    S.log(f"tracks with a style now  {S.human(stats['tracks_with_style'])}"
          f"  ({S.human(stats['tracks_multi_style'])} have more than one)")
    S.log(f"still without any style  {S.human(stats['tracks_unlabelled'])}"
          f" ({stats['tracks_without_style_pct']} %)")
    S.log("decisions: " + ", ".join(f"{name}={S.human(count)}"
                                    for name, count in result["decisions"].most_common()))
    S.log(f"unresolved pieces        {S.human(len(result['unresolved']))}  -> {listing}")
    for piece, count in result["unresolved"].most_common(15):
        print(f"      {S.human(count):>6}  {piece!r}")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Step 4 - map the raw genre tags onto canonical styles",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default="", help="dataset path (default: config.json)")
    parser.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    parser.add_argument("--only-unresolved", action="store_true",
                        help="re-map just the tags that failed last time")
    parser.add_argument("--status", action="store_true", help="print the dataset state")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    config = S.load_config()
    path = S.resolve(args.db) if args.db else S.db_path(config)
    connection = S.connect(path)
    try:
        # The vocabulary file is the source of truth: make sure it is loaded.
        from step3_vocabulary import build as build_vocabulary
        build_vocabulary(connection, load_vocabulary(config))
        connection.commit()

        normalizer = Normalizer(connection, config)
        overridden, errors = apply_overrides(normalizer, connection, config, dry_run=args.dry_run)
        for message in errors:
            print(f"      ERROR   {message}")
        if overridden:
            S.log(f"human overrides applied to {S.human(overridden)} tracks")

        result = normalize_library(normalizer, connection, config, dry_run=args.dry_run,
                                   only_unresolved=args.only_unresolved)
        if not args.dry_run:
            S.refresh_style_counts(connection)
            S.refresh_track_primary(connection)
            S.rebuild_artist_rollup(connection)
            S.meta_set(connection, "last_normalize", S.now())
            connection.commit()
        listing = write_report(connection, config, result)
        print_summary(connection, result, listing)
        if args.status and not args.dry_run:
            S.print_status(connection)
        if args.dry_run:
            S.log("dry run: nothing was written")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
