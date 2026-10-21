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
    """Return (errors, warnings) for the vocabulary file.

    A name is unique *within its family*: ``Hardcore`` may exist in Metal, Punk
    and Electronic, but not twice in one family. Aliases stay globally unique,
    so a bare tag can never become ambiguous.
    """
    errors: List[str] = []
    warnings: List[str] = []
    families = {str(name).strip() for name in (vocabulary.get("families") or [])}
    family_keys = {S.norm_key(name): str(name) for name in families}
    seen_pairs: Dict[Tuple[str, str], str] = {}
    name_owners: Dict[str, List[str]] = defaultdict(list)
    alias_owners: Dict[str, str] = {}
    entries: List[Tuple[str, str, Dict[str, Any]]] = []

    for entry in vocabulary["styles"]:
        name = str(entry.get("name") or "").strip()
        if not name:
            errors.append("a style entry has no name")
            continue
        family = str(entry.get("family") or "").strip()
        if S.STYLE_REF_SEPARATOR in name or S.STYLE_REF_SEPARATOR in family:
            errors.append(
                f"{name!r}: names and families cannot contain {S.STYLE_REF_SEPARATOR!r}")
        label = f"{name!r} ({family or 'no family'})"
        pair = S.style_pair_key(name, family)
        if pair in seen_pairs:
            errors.append(
                f"duplicate style: {label} and {seen_pairs[pair]} are the same name "
                f"in the same family")
        seen_pairs[pair] = label
        entries.append((name, family, entry))
        name_owners[S.norm_key(name)].append(label)
        if family and families and S.norm_key(family) not in family_keys:
            warnings.append(f"{name!r}: family {family!r} is not in the families list")
        if not family:
            warnings.append(f"{name!r}: no family")

    for name, _family, entry in entries:
        own_key = S.norm_key(name)
        for alias in (entry.get("aliases") or []):
            key = S.norm_key(alias)
            if not key or key == own_key:
                continue
            if key in name_owners:
                warnings.append(
                    f"alias {alias!r} of {name!r} is already a style name "
                    f"({', '.join(name_owners[key])})")
            owner = alias_owners.get(key)
            if owner and owner != name:
                warnings.append(
                    f"alias {alias!r} is used by both {owner!r} and {name!r}")
            alias_owners.setdefault(key, name)

    for name, family, entry in entries:
        parent = str(entry.get("parent") or "").strip()
        if not parent:
            continue
        if S.STYLE_REF_SEPARATOR in parent:
            errors.append(f"{name!r}: a parent is a plain style name, not a reference")
            continue
        if S.style_pair_key(parent, family) not in seen_pairs:
            warnings.append(
                f"{name!r}: parent {parent!r} does not exist in the "
                f"{family or 'same'} family")
        if S.norm_key(parent) == S.norm_key(name):
            warnings.append(f"{name!r}: a style cannot be its own parent")
    return errors, warnings


def build(connection, vocabulary: Dict[str, Any]) -> int:
    """Load the vocabulary into the dataset (idempotent).

    Two passes: all styles first, then their parents. A parent is resolved by
    ``(family, name)`` because the same name may exist in another family.
    """
    S.ensure_style_index(connection)
    loaded: List[Tuple[str, Optional[str], Optional[str]]] = []
    for entry in vocabulary["styles"]:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        family = str(entry.get("family") or "").strip() or None
        S.upsert_style(
            connection, name, family=family,
            aliases=entry.get("aliases") or [], source="seed")
        loaded.append((name, family, str(entry.get("parent") or "").strip() or None))
    for name, family, parent in loaded:
        row = S.find_style(connection, name, family)
        if row is None:
            continue
        parent_row = S.find_style(connection, parent, family) if parent else None
        parent_id = parent_row["id"] if parent_row is not None else None
        if parent_id == row["id"]:
            parent_id = None
        connection.execute("UPDATE styles SET parent=? WHERE id=?", (parent_id, row["id"]))
    return len(loaded)


def normalize_raw(value: str) -> str:
    """Normalise a raw tag piece for comparison (shared with step 4)."""
    text = S.strip_accents(value or "").lower()
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def vocabulary_keys(vocabulary: Dict[str, Any]) -> Dict[str, Tuple[str, str]]:
    """Map every normalised name/alias -> ``(name, family)``.

    All style **names** are registered first, then the aliases. An exact name
    therefore always beats an alias of another style ("Progressive Deathcore"
    wins over Deathcore's old ``progressive deathcore`` alias). For a duplicated
    name the first entry in the file stays the default.
    """
    entries: List[Tuple[str, str, Dict[str, Any]]] = []
    for entry in vocabulary["styles"]:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        family = str(entry.get("family") or "").strip()
        entries.append((name, family, entry))
    keys: Dict[str, Tuple[str, str]] = {}
    for name, family, _entry in entries:
        keys.setdefault(normalize_raw(name), (name, family))
    for name, family, entry in entries:
        for alias in (entry.get("aliases") or []):
            key = normalize_raw(str(alias))
            if key:
                keys.setdefault(key, (name, family))
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
