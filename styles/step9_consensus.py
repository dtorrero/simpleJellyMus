#!/usr/bin/env python3
"""Step 9 - propagation + consensus: make every source answer together.

The earlier steps each look at one kind of evidence on their own:

    tag / rules   the genre the owner wrote in the file
    audio         what the track *sounds* like (step 7)
    external      Wikidata / MusicBrainz / Wikipedia (step 6)
    llm           what DeepSeek knows about the artist (step 5)

None of them ever looks at the whole picture of a track, and none of them
notices when two sources agree - or fight. This step is the referee. It does
two things, both offline and free:

    propagation  what an artist (or an album) is, its own tracks already say.
                 If nine of ten tracks of a band are "Death Metal", the tenth
                 one is too - so it is filled in as source ``propagated``.
                 Two guards keep it honest: a style must be backed by at least
                 ``propagate_min_tracks`` tracks (a lone tag never spreads), and
                 a ``Various Artists`` compilation must be near-unanimous
                 (``propagate_compilation_min_ratio``) before it is trusted - so
                 a punk sampler is not silently relabelled "Symphonic Metal".

    consensus    for every track, the independent sources vote. When several
                 agree, that answer is written once as source ``consensus``
                 (which ranks above the lone guesses). When they disagree,
                 nothing is written: the track lands in ``out/review.csv`` for
                 you to look at.

It never overwrites stronger evidence (both ``propagated`` and ``consensus``
rank below the real tag, the rules and the audio classifier), and it never
invents a style: the vote is a pure choice among labels that already exist.

    python3 step9_consensus.py                 # propagate + consensus + report
    python3 step9_consensus.py --propagate     # only fill the gaps
    python3 step9_consensus.py --dry-run       # report only, write nothing
    python3 step9_consensus.py --report        # just show the current picture
    python3 step9_consensus.py --limit 50      # quick look on a few tracks
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import styles_db as S  # noqa: E402

# Independent voters. ``tag`` and ``rules`` are the *same* piece of evidence
# (the rules only clean the tag), so they count as one voter; a human override
# is authoritative and short-circuits the vote.
VOTER_OF = {
    "human": "metadata", "tag": "metadata", "rules": "metadata",
    "external": "external", "audio": "audio", "llm": "llm",
}
VOTER_ORDER = ["metadata", "external", "audio", "llm"]
# Row order of the review file, most urgent first. A "disputed" track is the
# most useful one to look at: the owner's own tag says one family and the
# automatic layers heard another (the tag may be the mistake).
SEVERITY = {"disputed": 0, "conflict": 1, "mismatch": 2, "vague": 3, "unlabelled": 4}

# A ``Various Artists`` compilation gives every track in it the same album
# artist, so it must never be pooled into one "artist" bucket (it is not one
# artist) and it must never be trusted on a single track. The shared keys seen
# in the wild are listed here; the normaliser (``styles_db.norm_key``) already
# lower-cased and de-punctuated them.
VA_ARTIST_KEYS = frozenset({"various", "various artist", "various artists", "v a", "va"})


def as_bool(value: Any) -> bool:
    """Read a config flag that may be a real bool or a ``"yes"``/``"on"``/``"1"`` string."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(value)


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #

def options(config: Dict[str, Any]) -> Dict[str, Any]:
    """The consensus/propagation knobs (config.json, then the CLI overrides)."""
    return {
        "min_sources": int(S.setting(config, "consensus.min_independent_sources", 2) or 2),
        "review_confidence": float(S.setting(config, "consensus.review_confidence", 0.6) or 0.6),
        "min_tracks": int(S.setting(config, "consensus.propagate_min_tracks", 2) or 2),
        "min_ratio": float(S.setting(config, "consensus.propagate_min_ratio", 0.5) or 0.5),
        # Various Artists handling: never pool the shared bucket, and demand
        # near-unanimity before a compilation relabels its own tracks.
        "various_artists": as_bool(S.setting(config, "consensus.propagate_various_artists", False)),
        "compilation_min_ratio": float(
            S.setting(config, "consensus.propagate_compilation_min_ratio", 0.9) or 0.9),
        "compilation_min_tracks": int(
            S.setting(config, "consensus.propagate_compilation_min_tracks", 3) or 3),
    }


# --------------------------------------------------------------------------- #
# reading the labels
# --------------------------------------------------------------------------- #

class Label:
    """One existing ``track_styles`` row, with its style name and family."""

    __slots__ = ("style_id", "source", "weight", "confidence", "name", "family", "vague")

    def __init__(self, style_id: str, source: str, weight: float, confidence: float,
                 name: str, family: str) -> None:
        self.style_id = style_id
        self.source = source
        self.weight = float(weight or 0.0)
        self.confidence = float(confidence or 0.0)
        self.name = name
        self.family = family
        # "Rock (unspecified)" and a style whose name *is* its family ("Rock")
        # say nothing specific - they must never win a vote against a real one.
        self.vague = name.endswith("(unspecified)") or (bool(family) and name == family)

    @property
    def specific(self) -> bool:
        return not self.vague


def load_labels(connection) -> Dict[str, List[Label]]:
    """Every label of every track, keyed by ``rel_path`` (one query)."""
    names, families = {}, {}
    for row in connection.execute("SELECT id, name, family FROM styles"):
        names[row["id"]] = row["name"]
        families[row["id"]] = row["family"] or ""
    labels: Dict[str, List[Label]] = defaultdict(list)
    for row in connection.execute(
            "SELECT rel_path, style_id, source, weight, confidence FROM track_styles"
            " ORDER BY rel_path, style_id"):
        style_id = row["style_id"]
        labels[row["rel_path"]].append(Label(
            style_id, row["source"], row["weight"], row["confidence"],
            names.get(style_id, style_id), families.get(style_id, "")))
    return labels


# --------------------------------------------------------------------------- #
# the vote
# --------------------------------------------------------------------------- #

def analyse(entries: Sequence[Label], *, min_sources: int,
            review_confidence: float) -> Dict[str, Any]:
    """Let the independent sources vote on one track (pure, no I/O).

    Returns a verdict with the winner, a confidence and the per-voter answer.
    Nothing here writes anything: :func:`cmd_consensus` decides what to keep.
    """
    # One vote per *voter*: the most specific label it produced, and among
    # equally specific ones the most trusted (human > tag > rules).
    voters: Dict[str, Label] = {}
    for label in entries:
        voter = VOTER_OF.get(label.source)
        if voter is None:                       # propagated / consensus / unknown
            continue
        key = (label.specific, S.SOURCE_PRIORITY.get(label.source, 0), label.weight)
        current = voters.get(voter)
        if current is None or key > (current.specific,
                                     S.SOURCE_PRIORITY.get(current.source, 0), current.weight):
            voters[voter] = label

    base = {"winner": None, "winner_name": "", "winner_family": "", "agree": 0,
            "n_voters": len(voters), "n_specific": 0, "voters": voters, "review": False,
            "write": False, "confidence": 0.0}

    if not voters:
        return {**base, "decision": "unlabelled"}

    # A human override is the owner's word: it settles the track by itself.
    metadata = voters.get("metadata")
    if metadata is not None and metadata.source == "human":
        return {**base, "decision": "human", "winner": metadata.style_id,
                "winner_name": metadata.name, "winner_family": metadata.family,
                "agree": 1, "n_specific": 1, "confidence": round(metadata.confidence, 3)}

    specific = [label for label in voters.values() if label.specific]
    if not specific:
        # Every source only knows a family ("Rock", "Metal (unspecified)").
        vague = max(voters.values(),
                    key=lambda label: (S.SOURCE_PRIORITY.get(label.source, 0), label.weight,
                                       label.style_id))
        return {**base, "decision": "vague", "winner": vague.style_id,
                "winner_name": vague.name, "winner_family": vague.family,
                "confidence": 0.3}

    support: Dict[str, int] = Counter(label.style_id for label in specific)
    rank: Dict[str, int] = {}
    weight: Dict[str, float] = {}
    for label in specific:
        rank[label.style_id] = max(rank.get(label.style_id, 0),
                                   S.SOURCE_PRIORITY.get(label.source, 0))
        weight[label.style_id] = weight.get(label.style_id, 0.0) + label.weight
    # Ties are broken by style id (never by set/dict iteration order), so the
    # winner is identical on every run whatever the interpreter's hash seed is.
    winner = max(support, key=lambda sid: (support[sid], rank[sid], weight[sid], sid))
    winner_label = next(label for label in specific if label.style_id == winner)
    agree = support[winner]
    n_specific = len(specific)

    # Confidence: how much of the specific evidence agrees, and how trusted it is.
    strongest = max(S.SOURCE_PRIORITY.get(label.source, 0) for label in specific) / 100.0
    confidence = round(0.6 * (agree / n_specific) + 0.4 * strongest, 3)

    families = {label.family for label in specific}
    if len(families) > 1:
        # The owner wrote one family and the automatic layers heard another:
        # the tag is the suspect, so this is the most useful row to review.
        if metadata is not None and metadata.specific and metadata.family != winner_label.family:
            decision = "disputed"
        else:
            decision = "conflict"
    elif len(support) > 1:
        decision = "mismatch"
    else:
        decision = "agree"

    return {**base, "decision": decision, "winner": winner, "winner_name": winner_label.name,
            "winner_family": winner_label.family, "agree": agree, "n_specific": n_specific,
            "confidence": confidence,
            # ``review`` is the list of *possible mistakes*: a real disagreement,
            # or an agreement nobody is sure about. The known gaps ("vague",
            # "unlabelled") are not mistakes - the enrichment steps own those.
            "review": decision in ("conflict", "disputed", "mismatch")
                      or (decision == "agree" and confidence < review_confidence),
            # Only a genuine agreement of several independent sources is kept.
            "write": decision == "agree" and n_specific >= min_sources,
            "voters": voters}


# --------------------------------------------------------------------------- #
# propagation (free: the album and the artist already know)
# --------------------------------------------------------------------------- #

def propagation_plan(connection, labels: Dict[str, List[Label]], *,
                     min_tracks: int, min_ratio: float,
                     various_artists: bool = False,
                     compilation_min_ratio: float = 0.9,
                     compilation_min_tracks: int = 3) -> Dict[str, List[S.StyleEntry]]:
    """Return ``{rel_path: entries}`` filling the gaps from album and artist.

    Two passes, most precise first: the album is a strong context, the artist
    is the fallback for the tracks the album could not decide. A style is only
    propagated when it is backed by at least ``min_tracks`` tracks of that
    album/artist *and* by ``min_ratio`` of its already-labelled tracks, so a
    one-off tag on a sampler never spreads to the rest of the discography.

    Two extra guards keep compilations honest:

    * a ``Various Artists`` track needs near-unanimity (``compilation_min_ratio``
      over ``compilation_min_tracks`` tracks) before we believe its album - one
      strong track must not relabel a whole 100-track sampler;
    * unless ``various_artists`` is true, the shared ``various artists`` bucket
      is never used as an artist fallback, because it is not one artist at all.

    The winner is picked deterministically (most votes, ties broken by style
    id), so a run never depends on set/dict iteration order.
    """
    names = S.style_names(connection)
    tracks = list(connection.execute(
        "SELECT rel_path, album_key, artist_key FROM tracks"))

    album_counts: Dict[str, Counter] = defaultdict(Counter)
    artist_counts: Dict[str, Counter] = defaultdict(Counter)
    album_labelled: Counter = Counter()
    artist_labelled: Counter = Counter()
    needs: List[Any] = []
    for row in tracks:
        # A track "needs" propagation when it has no *specific* label from a real
        # source. This is judged on the labels themselves, never on
        # ``primary_style`` - otherwise a previous propagation run would hide the
        # very tracks it just filled and the step would stop being idempotent.
        specific = {label.style_id for label in (labels.get(row["rel_path"]) or [])
                    if label.specific and label.source != "propagated"}
        if specific:
            if row["album_key"]:
                album_labelled[row["album_key"]] += 1
                album_counts[row["album_key"]].update(specific)
            if row["artist_key"]:
                artist_labelled[row["artist_key"]] += 1
                artist_counts[row["artist_key"]].update(specific)
        else:
            needs.append(row)

    def pick(counts: Counter, labelled: int, *,
             min_tracks: int, min_ratio: float) -> Tuple[Optional[str], int]:
        if not counts or not labelled:
            return None, 0
        # Most votes wins; ties go to the lower style id so the choice is stable
        # (the counters are fed from a ``set``, whose order is hash-randomised).
        style_id, count = min(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        if count >= min_tracks and count / labelled >= min_ratio:
            return style_id, count
        return None, 0

    plan: Dict[str, List[S.StyleEntry]] = {}
    for row in needs:
        is_va = row["artist_key"] in VA_ARTIST_KEYS
        # A compilation has to be near-unanimous before we trust it - both when
        # its own album speaks and (if ever enabled) when its shared bucket does.
        floor = compilation_min_tracks if is_va else min_tracks
        ratio = compilation_min_ratio if is_va else min_ratio
        style_id, count = (None, 0)
        where = ""
        if row["album_key"]:
            style_id, count = pick(album_counts.get(row["album_key"], Counter()),
                                   album_labelled.get(row["album_key"], 0),
                                   min_tracks=floor, min_ratio=ratio)
            where = "album"
        if not style_id and row["artist_key"] and not (is_va and not various_artists):
            style_id, count = pick(artist_counts.get(row["artist_key"], Counter()),
                                   artist_labelled.get(row["artist_key"], 0),
                                   min_tracks=floor, min_ratio=ratio)
            where = "artist"
        if not style_id:
            continue
        # A propagated label must outrank the *vague* family labels (weight 0.6
        # in step 4) so the track finally gets a specific primary, yet stay below
        # a real specific label (weight 1.0) so it can never overwrite evidence.
        weight = 0.75 if where == "album" else 0.65
        confidence = round(min(0.6, 0.3 + 0.1 * count), 2)
        evidence = f"{where} {names.get(style_id, style_id)} x{count}"
        plan[row["rel_path"]] = [(style_id, weight, "propagated", confidence, evidence)]
    return plan


def cmd_propagate(connection, config: Dict[str, Any], args, opts: Dict[str, Any]) -> int:
    labels = load_labels(connection)
    plan = propagation_plan(connection, labels, min_tracks=opts["min_tracks"],
                            min_ratio=opts["min_ratio"],
                            various_artists=opts["various_artists"],
                            compilation_min_ratio=opts["compilation_min_ratio"],
                            compilation_min_tracks=opts["compilation_min_tracks"])
    if getattr(args, "limit", 0):
        plan = {path: plan[path] for path in list(plan)[:args.limit]}
    sources = Counter(entry[4].split(" ")[0] for entries in plan.values()
                      for entry in entries)
    S.log(f"propagation would label {S.human(len(plan))} tracks"
          f"  (from albums: {S.human(sources.get('album', 0))},"
          f" from artists: {S.human(sources.get('artist', 0))})")
    if args.dry_run:
        S.log("dry run: nothing was written")
        return 0
    S.clear_track_styles(connection, ["propagated"])
    written = 0
    for start in range(0, len(plan), 800):
        chunk = {path: plan[path] for path in list(plan)[start:start + 800]}
        written += S.add_track_styles_bulk(connection, chunk)
    connection.commit()
    S.refresh_style_counts(connection)
    S.refresh_track_primary(connection)
    connection.commit()
    S.log(f"propagated {S.human(len(plan))} tracks ({S.human(written)} labels)")
    return 0


# --------------------------------------------------------------------------- #
# consensus
# --------------------------------------------------------------------------- #

def voter_says(verdict: Dict[str, Any], voter: str) -> str:
    """The label one voter proposed (for the review file)."""
    label = verdict["voters"].get(voter)
    return label.name if label else ""


def cmd_consensus(connection, config: Dict[str, Any], args, opts: Dict[str, Any]) -> int:
    labels = load_labels(connection)
    rows = list(connection.execute(
        "SELECT rel_path, COALESCE(artists, album_artist) AS artist, album, year,"
        " raw_genre, primary_style, label_source FROM tracks"))
    if args.limit:
        rows = rows[:args.limit]
    if not rows:
        S.log("nothing to vote on - run step1_scan.py and step4_normalize.py first")
        return 1

    pending: Dict[str, List[S.StyleEntry]] = {}
    review: List[Tuple[Any, Dict[str, Any]]] = []
    decisions: Counter = Counter()
    progress = S.Progress(len(rows), label="voting   ")
    for row in rows:
        verdict = analyse(labels.get(row["rel_path"]) or [],
                          min_sources=opts["min_sources"],
                          review_confidence=opts["review_confidence"])
        decisions[verdict["decision"]] += 1
        if verdict["write"]:
            pending[row["rel_path"]] = [(verdict["winner"], 1.0, "consensus",
                                         verdict["confidence"],
                                         f"vote {verdict['agree']}/{verdict['n_specific']}")]
        if verdict["review"]:
            review.append((row, verdict))
        progress.step()
    progress.finish()

    written = 0
    if not args.dry_run:
        # Recompute from scratch: a track that was promoted last time but is no
        # longer unanimous must lose its consensus label, and vice versa.
        S.clear_track_styles(connection, ["consensus"])
        for start in range(0, len(pending), 800):
            chunk = {path: pending[path] for path in list(pending)[start:start + 800]}
            written += S.add_track_styles_bulk(connection, chunk)
        connection.commit()

    review.sort(key=lambda item: (SEVERITY.get(item[1]["decision"], 9), item[1]["confidence"]))
    out = S.out_dir(config)
    review_path = out / "review.csv"
    write_review(review_path, review)
    report_path = out / "consensus_report.txt"
    write_report(report_path, decisions, review, written, opts, dry_run=args.dry_run)

    if not args.dry_run:
        S.meta_set(connection, "consensus_review", len(review))
        S.meta_set(connection, "consensus_at", S.now())
        S.refresh_style_counts(connection)
        S.refresh_track_primary(connection)
        connection.commit()

    print()
    S.log("votes: " + ", ".join(f"{name}={S.human(count)}"
                                for name, count in decisions.most_common()))
    S.log(f"consensus written to {S.human(len(pending))} tracks"
          f" ({S.human(written)} labels), {S.human(len(review))} need a look")
    S.log("review list:", str(review_path))
    S.log("summary:    ", str(report_path))
    for row, verdict in review[:10]:
        S.log(f"  {verdict['decision']:10s} {verdict['confidence']:.2f}"
              f"  {str(row['raw_genre'])[:34]!r} -> {verdict['winner_name']!r}")
    return 0


def write_review(path: Path, review: Sequence[Tuple[Any, Dict[str, Any]]]) -> None:
    """The list of tracks a human should look at, most urgent first."""
    columns = ["decision", "confidence", "agree", "n_sources", "path", "artist", "album",
               "year", "raw_genre", "primary_style", "label_source"]
    columns += [f"{voter}_says" for voter in VOTER_ORDER]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row, verdict in review:
            writer.writerow([
                verdict["decision"], f"{verdict['confidence']:.2f}",
                f"{verdict['agree']}/{verdict['n_specific']}", verdict["n_voters"],
                row["rel_path"], row["artist"] or "", row["album"] or "", row["year"] or "",
                row["raw_genre"] or "", row["primary_style"] or "", row["label_source"] or "",
                *[voter_says(verdict, voter) for voter in VOTER_ORDER],
            ])


def write_report(path: Path, decisions: Counter, review: Sequence[Tuple[Any, Dict[str, Any]]],
                 written: int, opts: Dict[str, Any], *, dry_run: bool) -> None:
    """A short, readable summary of what the vote found."""
    lines: List[str] = ["# step9_consensus - what the sources said", ""]
    if dry_run:
        lines += ["(dry run - the dataset was not modified)", ""]
    lines += ["## verdict per track", ""]
    for name in ("agree", "mismatch", "conflict", "disputed", "vague", "human",
                 "unlabelled"):
        lines.append(f"- {name:11s} {S.human(decisions.get(name, 0))}")
    lines += ["", "## settings", "",
              f"- a style is written only with >= {opts['min_sources']} agreeing sources",
              f"- a track below confidence {opts['review_confidence']:.2f} is flagged",
              f"- consensus labels written: {S.human(written)}",
              f"- tracks to review        : {S.human(len(review))}", ""]
    top = Counter(row["artist"] or "?" for row, verdict in review
                  if verdict["decision"] in ("conflict", "disputed"))
    if top:
        lines += ["## artists with the most disputed tracks", ""]
        for artist, count in top.most_common(20):
            lines.append(f"- {S.human(count):>5}  {artist}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Step 9 - propagation + consensus over every source",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="", help="dataset path (default: config.json)")
    parser.add_argument("--propagate", action="store_true",
                        help="only fill the gaps from the album/artist")
    parser.add_argument("--consensus", action="store_true",
                        help="only run the vote (skip propagation)")
    parser.add_argument("--report", action="store_true",
                        help="write the report/review only, change nothing")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute everything, write no style")
    parser.add_argument("--limit", type=int, default=0, help="only the first N tracks")
    parser.add_argument("--min-sources", type=int, default=0,
                        help="override consensus.min_independent_sources")
    parser.add_argument("--review-confidence", type=float, default=0.0,
                        help="override consensus.review_confidence")
    parser.add_argument("--status", action="store_true", help="print the dataset state")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    config = S.load_config()
    opts = options(config)
    if args.min_sources:
        opts["min_sources"] = args.min_sources
    if args.review_confidence:
        opts["review_confidence"] = args.review_confidence
    if args.report:
        args.dry_run = True

    path = S.resolve(args.db) if args.db else S.db_path(config)
    connection = S.connect(path, create=False)
    try:
        do_all = not (args.propagate or args.consensus)
        if args.propagate or do_all:
            cmd_propagate(connection, config, args, opts)
        if args.consensus or do_all:
            cmd_consensus(connection, config, args, opts)
        if args.status and not args.dry_run:
            S.print_status(connection)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

