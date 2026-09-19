#!/usr/bin/env python3
"""Step 3 - the canonical vocabulary of styles.

The vocabulary is a versioned text file (``data/styles.json``) that you own: it
lists the families and the styles of the dataset, and for every style the
spelling variants (aliases) that may appear in the wild. The file tags are never
changed - the vocabulary is only the *target* they are mapped onto.

    python3 step3_vocabulary.py --check       # validate + coverage report
    python3 step3_vocabulary.py --build       # load it into the dataset
    python3 step3_vocabulary.py --uncovered   # parts of the raw tags not covered

``--uncovered`` writes ``out/uncovered_parts.txt``; adding those spellings as
aliases (or as rules in ``data/rules.json``) is how the coverage grows. Re-run
steps 3 and 4 after every edit - nothing is ever lost by re-running.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import styles_db as S  # noqa: E402

SPLIT_RE = re.compile(r"[/|,;+\\]|\s+[-\u2013]\s+", re.IGNORECASE)


def styles_file(config: Dict[str, Any]) -> Path:
    return S.resolve(S.setting(config, "normalize.styles_file", "data/styles.json"))


def load_vocabulary(config: Dict[str, Any]) -> Dict[str, Any]:
    path = styles_file(config)
    if not path.exists():
        raise SystemExit(f"vocabulary not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit(f"{path} is not valid JSON: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("styles"), list):
        raise SystemExit(f"{path} must contain a 'styles' list")
    return data


def split_parts(raw: str) -> List[str]:
    """Split a raw genre tag into its style-sized pieces."""
    parts = []
    for piece in SPLIT_RE.split(raw or ""):
        text = " ".join((piece or "").split()).strip(" -,.;:/|+&")
        if text:
            parts.append(text)
    return parts


def validate(vocabulary: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Return (errors, warnings) for the vocabulary file."""
    errors: List[str] = []
    warnings: List[str] = []
    families = {str(name).strip() for name in (vocabulary.get("families") or [])}
    seen_ids: Dict[str, str] = {}
    seen_keys: Dict[str, str] = {}
    for entry in vocabulary["styles"]:
        name = str(entry.get("name") or "").strip()
        if not name:
            errors.append("a style entry has no name")
            continue
        slug = S.style_slug(name)
        if slug in seen_ids:
            errors.append(f"duplicate style: {name!r} and {seen_ids[slug]!r} share the id {slug}")
        seen_ids[slug] = name
        family = entry.get("family")
        if family and families and family not in families:
            warnings.append(f"{name!r}: family {family!r} is not in the families list")
        if not family:
            warnings.append(f"{name!r}: no family")
        keys = [S.norm_key(name)] + [S.norm_key(alias) for alias in (entry.get("aliases") or [])]
        for key in keys:
            if not key:
                continue
            if key in seen_keys and seen_keys[key] != name:
                warnings.append(
                    f"alias {key!r} is used by both {seen_keys[key]!r} and {name!r}")
            seen_keys.setdefault(key, name)
    for entry in vocabulary["styles"]:
        parent = entry.get("parent")
        if parent and S.style_slug(parent) not in seen_ids:
            warnings.append(f"{entry.get('name')!r}: parent {parent!r} does not exist")
    return errors, warnings


def build(connection, vocabulary: Dict[str, Any]) -> int:
    """Load the vocabulary into the dataset (idempotent)."""
    total = 0
    for entry in vocabulary["styles"]:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        S.upsert_style(
            connection, name,
            family=(str(entry.get("family")) if entry.get("family") else None),
            parent=(S.style_slug(entry["parent"]) if entry.get("parent") else None),
            aliases=entry.get("aliases") or [],
            source="seed",
        )
        total += 1
    return total


def normalize_raw(value: str) -> str:
    """Normalise a raw tag piece for comparison (shared with step 4)."""
    text = S.strip_accents(value or "").lower()
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def vocabulary_keys(vocabulary: Dict[str, Any]) -> Dict[str, str]:
    """Map every normalised name/alias -> style name."""
    keys: Dict[str, str] = {}
    for entry in vocabulary["styles"]:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        keys.setdefault(normalize_raw(name), name)
        for alias in (entry.get("aliases") or []):
            key = normalize_raw(str(alias))
            if key:
                keys.setdefault(key, name)
    return keys


def load_rules(config: Dict[str, Any]) -> Dict[str, Any]:
    path = S.resolve(S.setting(config, "normalize.rules_file", "data/rules.json"))
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit(f"{path} is not valid JSON: {exc}")
    return data if isinstance(data, dict) else {}


def uncovered_report(connection, config: Dict[str, Any]) -> int:
    """Which pieces of the raw genre tags the vocabulary does not cover yet."""
    vocabulary = load_vocabulary(config)
    rules = load_rules(config)
    keys = vocabulary_keys(vocabulary)
    replace = {normalize_raw(k): v for k, v in (rules.get("replace") or {}).items()}
    drop_parts = {normalize_raw(part) for part in (rules.get("drop_parts") or [])}
    drop_whole = {normalize_raw(raw) for raw in (rules.get("drop") or [])}
    force = {normalize_raw(k) for k in (rules.get("force") or {})}
    families = {normalize_raw(name) for name in (vocabulary.get("families") or [])}
    family_only = {normalize_raw(part) for part in (rules.get("family_only") or [])}

    missing: Counter = Counter()
    missing_raw: defaultdict = defaultdict(set)
    covered_parts = covered_tracks = total_tracks = total_parts = raw_strings = 0
    rows = connection.execute(
        "SELECT raw_genre, COUNT(*) AS n FROM tracks"
        " WHERE TRIM(COALESCE(raw_genre, '')) <> '' GROUP BY raw_genre")
    for row in rows:
        raw = row["raw_genre"]
        count = int(row["n"] or 0)
        raw_strings += 1
        total_tracks += count
        if normalize_raw(raw) in drop_whole or normalize_raw(raw) in force:
            covered_tracks += count
            continue
        parts = split_parts(raw)
        total_parts += len(parts)
        ok = 0
        for part in parts:
            key = replace.get(normalize_raw(part), normalize_raw(part))
            if not key:
                continue
            if (key in drop_parts or key in family_only or key in families or key in keys
                    or normalize_raw(key) in keys):
                covered_parts += 1
                ok += 1
            else:
                missing[part] += count
                missing_raw[part].add(raw)
        if ok:
            covered_tracks += count

    listing = S.out_dir(config) / "uncovered_parts.txt"
    with listing.open("w", encoding="utf-8") as handle:
        handle.write("# raw genre pieces the vocabulary does not know yet\n")
        handle.write("# add them as aliases in data/styles.json (or to data/rules.json)\n")
        handle.write("# tracks\tpiece\texample raw tags\n")
        for piece, count in missing.most_common():
            examples = "; ".join(sorted(missing_raw[piece])[:3])
            handle.write(f"{count}\t{piece}\t{examples}\n")

    print()
    S.log(f"raw genre strings         {S.human(raw_strings)}")
    S.log(f"tracks with a raw genre   {S.human(total_tracks)}")
    S.log(f"pieces found              {S.human(total_parts)}")
    S.log(f"pieces covered            {S.human(covered_parts)}"
          f" ({100.0 * covered_parts / max(1, total_parts):.1f} %)")
    S.log(f"tracks fully covered      {S.human(covered_tracks)}"
          f" ({100.0 * covered_tracks / max(1, total_tracks):.1f} %)")
    S.log(f"distinct uncovered pieces {S.human(len(missing))}  ->  {listing}")
    if missing:
        S.log("worst uncovered pieces (tracks / piece):")
        for piece, count in missing.most_common(40):
            print(f"      {S.human(count):>6}  {piece!r}")
    return len(missing)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Step 3 - the canonical vocabulary of styles",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default="", help="dataset path (default: config.json)")
    parser.add_argument("--check", action="store_true", help="validate the vocabulary file")
    parser.add_argument("--build", action="store_true", help="load it into the dataset")
    parser.add_argument("--uncovered", action="store_true",
                        help="report raw genre pieces the vocabulary misses")
    parser.add_argument("--all", action="store_true", help="check + build + uncovered")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    config = S.load_config()
    vocabulary = load_vocabulary(config)
    do_check = args.check or args.all or not (args.build or args.uncovered)
    do_build = args.build or args.all
    do_uncovered = args.uncovered or args.all

    if do_check:
        errors, warnings = validate(vocabulary)
        S.log(f"vocabulary: {S.human(len(vocabulary['styles']))} styles,"
              f" {S.human(len(vocabulary.get('families') or []))} families")
        for message in errors:
            print(f"      ERROR   {message}")
        for message in warnings[:40]:
            print(f"      warn    {message}")
        if len(warnings) > 40:
            print(f"      … {len(warnings) - 40} more warnings")
        if errors:
            return 1

    path = S.resolve(args.db) if args.db else S.db_path(config)
    if do_build:
        connection = S.connect(path)
        try:
            total = build(connection, vocabulary)
            connection.commit()
            stored = connection.execute("SELECT COUNT(*) AS n FROM styles").fetchone()["n"]
            S.log(f"loaded {S.human(total)} styles into the dataset ({S.human(stored)} stored)")
        finally:
            connection.close()
    if do_uncovered:
        connection = S.connect(path, create=False)
        try:
            uncovered_report(connection, config)
        finally:
            connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
