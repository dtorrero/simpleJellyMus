#!/usr/bin/env python3
"""Step 6 - external styles from metal-api.dev (Metal Archives data).

The API is metal-only but it is very useful for artists that have no usable
genre tag: an exact band-name match can still be confirmed by matching one of
the band's albums against the albums in this library. We therefore search:

  * artists already known to be metal / hardcore (35 here), and
  * artists with no style and no tag at all (134 here),

by default. Known non-metal artists are skipped unless ``--all-candidates`` is
given. The API has no key, returns JSON, and rate-limits at 30 requests / 10 s;
the client is deliberately slower (0.5 s by default) and caches every answer.

    python3 step6_metal_api.py --estimate
    python3 step6_metal_api.py --fill
    python3 step6_metal_api.py --fill --all-candidates
    python3 step6_metal_api.py --artist "13 Dead"
    python3 step6_metal_api.py --artist "Kadjo"
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

STYLES_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(STYLES_DIR))

import styles_db as S  # noqa: E402
from step4_normalize import Normalizer  # noqa: E402
from step5_llm import build_artist_index, fill_candidates  # noqa: E402

PROVIDER = "metal_api"
DEFAULT_BASE = "https://metal-api.dev"
METAL_FAMILIES = {"Metal", "Hardcore"}
METAL_TAG = re.compile(r"(metal|core|grind|doom|black|death|thrash|sludge|heavy)", re.I)
EDITION = re.compile(
    r"\b(deluxe|remaster(?:ed)?|edition|bonus|expanded|reissue|version)\b.*$", re.I)


def cfg(config: Dict[str, Any], key: str, default: Any) -> Any:
    return S.setting(config, f"external.metal_api.{key}", default)


def album_key(text: Any) -> str:
    """A forgiving album key (ignores edition suffixes)."""
    return EDITION.sub("", S.norm_key(text)).strip()


def artist_group(record: Dict[str, Any],
                 families: Dict[str, Optional[str]]) -> str:
    """known_metal, unknown (no style/tag evidence) or known_other."""
    if any(families.get(style) in METAL_FAMILIES for style in record.get("styles") or {}):
        return "known_metal"
    if any(METAL_TAG.search(str(tag)) for tag in record.get("tags") or {}):
        return "known_metal"
    if not record.get("styles") and not record.get("tags"):
        return "unknown"
    return "known_other"


def _parse_reset(value: Any) -> Optional[float]:
    text = str(value or "").strip().replace("Z", "+00:00")
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError):
        return None


class MetalApiError(Exception):
    """The API could not be reached or returned an unusable answer."""


class MetalApiClient:
    """Tiny stdlib client with rate limiting, retries and a request counter."""

    def __init__(self, config: Dict[str, Any], *, debug: bool = False) -> None:
        self.base_url = str(cfg(config, "base_url", DEFAULT_BASE)).rstrip("/")
        self.timeout = float(cfg(config, "request_timeout", 30) or 30)
        self.delay = max(0.0, float(cfg(config, "rate_limit_seconds", 0.5) or 0.5))
        self.max_retries = max(0, int(cfg(config, "max_retries", 2) or 2))
        self.user_agent = str(S.setting(
            config, "external.user_agent",
            "SimpleJellyMus-style-builder/0.1 ( personal, non-commercial )"))
        self.debug = debug
        self.requests = 0
        self.errors = 0
        self._last_request = 0.0
        self._remaining: Optional[int] = None
        self._reset: Optional[float] = None

    def _log(self, *parts: Any) -> None:
        if self.debug:
            print("[metal-api]", *parts, flush=True)

    def _wait(self) -> None:
        wait = 0.0
        if self.delay:
            wait = self.delay - (time.monotonic() - self._last_request)
        if self._remaining is not None and self._remaining <= 2 and self._reset:
            wait = max(wait, max(0.0, self._reset - time.time()) + 0.5)
        if wait > 0:
            time.sleep(wait)


    def _get(self, path: str) -> Any:
        url = self.base_url + path
        last_error: Optional[BaseException] = None
        for attempt in range(self.max_retries + 1):
            self._wait()
            self._last_request = time.monotonic()
            request = urllib.request.Request(
                url, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
            self._log("GET", url)
            self.requests += 1
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    remaining = response.headers.get("X-Rate-Limit-Remaining")
                    self._remaining = (int(remaining)
                                       if remaining and remaining.isdigit() else None)
                    self._reset = _parse_reset(response.headers.get("X-Rate-Limit-Reset"))
                    raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else None
            except urllib.error.HTTPError as exc:
                self.errors += 1
                last_error = exc
                if exc.code == 429:
                    retry = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        wait = float(retry) if retry else 2.0 * (attempt + 1)
                    except (TypeError, ValueError):
                        wait = 2.0 * (attempt + 1)
                    self._log("HTTP 429 - waiting", wait)
                    time.sleep(wait)
                    continue
                if 500 <= exc.code < 600 and attempt < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise MetalApiError(f"HTTP {exc.code} for {url}") from exc
            except (urllib.error.URLError, OSError, ValueError) as exc:
                self.errors += 1
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise MetalApiError(f"request failed for {url}: {exc}") from exc
        raise MetalApiError(f"retries exhausted for {url}: {last_error}")

    def search_band(self, name: str) -> List[Dict[str, Any]]:
        path = "/search/bands/name/" + urllib.parse.quote(str(name), safe="")
        data = self._get(path)
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def band(self, band_id: Any) -> Optional[Dict[str, Any]]:
        path = "/bands/" + urllib.parse.quote(str(band_id), safe="")
        data = self._get(path)
        return data if isinstance(data, dict) else None


def _ensure_artist(connection, record: Dict[str, Any]) -> None:
    connection.execute(
        "INSERT INTO artists(key, name, tracks, albums, updated_at) VALUES(?,?,0,0,?)"
        " ON CONFLICT(key) DO UPDATE SET name=COALESCE(artists.name, excluded.name),"
        " updated_at=excluded.updated_at",
        (record["key"], record["name"], S.now()))


def cache_get(connection, key: str) -> Optional[Dict[str, Any]]:
    row = connection.execute(
        "SELECT external_json FROM artists WHERE key = ?", (key,)).fetchone()
    if not row or not row["external_json"]:
        return None
    try:
        payload = json.loads(row["external_json"])
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    answer = payload.get(PROVIDER)
    return answer if isinstance(answer, dict) else None


def cache_set(connection, record: Dict[str, Any], answer: Dict[str, Any]) -> None:
    _ensure_artist(connection, record)
    row = connection.execute(
        "SELECT external_json FROM artists WHERE key = ?", (record["key"],)).fetchone()
    payload: Dict[str, Any] = {}
    if row and row["external_json"]:
        try:
            parsed = json.loads(row["external_json"])
            if isinstance(parsed, dict):
                payload = parsed
        except (TypeError, ValueError):
            pass
    payload[PROVIDER] = answer
    connection.execute(
        "UPDATE artists SET external_json=?, external_at=?, updated_at=? WHERE key=?",
        (json.dumps(payload, ensure_ascii=False), S.now(), S.now(), record["key"]))


def catalog_albums(connection, artist_key: str) -> List[str]:
    return [row["album"] for row in connection.execute(
        "SELECT DISTINCT album FROM tracks WHERE artist_key = ?"
        " AND TRIM(COALESCE(album, '')) <> ''", (artist_key,))]


def match_albums(connection, record: Dict[str, Any],
                 band: Dict[str, Any]) -> List[str]:
    """API album names that also appear in this library (edition-insensitive)."""
    wanted = {album_key(name) for name in catalog_albums(connection, record["key"])}
    wanted.discard("")
    if not wanted:
        return []
    matches: List[str] = []
    for album in band.get("albums") or []:
        name = str((album or {}).get("name") or "")
        key = album_key(name)
        if key and key in wanted and name not in matches:
            matches.append(name)
    return matches


def apply_style(connection, normalizer: Normalizer, record: Dict[str, Any],
                genre: str, confidence: float, link: str, *,
                all_tracks: bool, dry_run: bool) -> Dict[str, Any]:
    entries, decision, unresolved = normalizer.map_raw(genre)
    if not entries:
        return {"status": "unmapped", "tracks": 0, "labels": 0,
                "unresolved": list(unresolved), "decision": decision}
    external = [
        (style_id, float(weight), "external",
         min(float(confidence), float(entry_confidence)),
         f"metal-api: {genre} ({link})")
        for style_id, weight, _source, entry_confidence, _evidence in entries
    ]
    targets = list(record["tracks"] if all_tracks else record["needs"])
    if dry_run or not targets:
        return {"status": "found", "tracks": len(targets),
                "labels": len(targets) * len(external), "entries": external}
    S.add_track_styles_bulk(connection, {rel_path: external for rel_path in targets})
    return {"status": "found", "tracks": len(targets),
            "labels": len(targets) * len(external), "entries": external}


def refresh_catalog(connection) -> None:
    S.refresh_style_counts(connection)
    S.refresh_track_primary(connection)
    S.rebuild_artist_rollup(connection)
    S.meta_set(connection, "last_metal_api", S.now())
    connection.commit()


def resolve_record(client: MetalApiClient, connection, normalizer: Normalizer,
                   record: Dict[str, Any], group: str,
                   args: argparse.Namespace) -> Dict[str, Any]:
    """Search, confirm and apply one artist; returns a result dict."""
    query = record["name"]
    cached = None if args.refresh else cache_get(connection, record["key"])
    if cached:
        status = str(cached.get("status") or "not_found")
        if status == "found" and cached.get("genre"):
            applied = apply_style(
                connection, normalizer, record, str(cached["genre"]),
                float(cached.get("confidence") or 0.8), str(cached.get("link") or ""),
                all_tracks=args.all_tracks, dry_run=args.dry_run)
            return {"status": applied["status"], "cached": True, "band": cached,
                    "tracks": applied["tracks"], "labels": applied["labels"],
                    "entries": applied.get("entries", [])}
        return {"status": status, "cached": True, "band": cached,
                "tracks": 0, "labels": 0}

    try:
        candidates = client.search_band(query)
    except MetalApiError as exc:
        return {"status": "error", "error": str(exc), "tracks": 0, "labels": 0}

    wanted = S.norm_key(query)
    exact = [item for item in candidates
             if S.norm_key(str(item.get("name") or "")) == wanted]
    if not exact:
        answer = {"status": "not_found", "query": query,
                  "candidates": len(candidates), "checked_at": S.now()}
        if not args.dry_run:
            cache_set(connection, record, answer)
        return {"status": "not_found", "band": answer, "tracks": 0, "labels": 0}

    candidate: Dict[str, Any]
    band: Optional[Dict[str, Any]] = None
    matches: List[str] = []
    if len(exact) > 1:
        choices: List[Tuple[Dict[str, Any], Dict[str, Any], List[str]]] = []
        for item in exact:
            info = None
            try:
                info = client.band(item.get("id"))
            except MetalApiError:
                info = None
            if not info:
                continue
            found = match_albums(connection, record, info)
            choices.append((item, info, found))
        with_albums = [choice for choice in choices if choice[2]]
        if len(with_albums) != 1:
            answer = {
                "status": "ambiguous", "query": query,
                "candidates": [
                    {"id": item.get("id"), "name": item.get("name"),
                     "genre": item.get("genre"), "country": item.get("country"),
                     "link": item.get("link"), "album_matches": found}
                    for item, _info, found in choices
                ] or [{"id": item.get("id"), "name": item.get("name"),
                       "genre": item.get("genre"), "country": item.get("country"),
                       "link": item.get("link"), "album_matches": []}
                      for item in exact],
                "checked_at": S.now(),
            }
            if not args.dry_run:
                cache_set(connection, record, answer)
            return {"status": "ambiguous", "band": answer, "tracks": 0, "labels": 0}
        candidate, band, matches = with_albums[0]
    else:
        candidate = exact[0]
        try:
            band = client.band(candidate.get("id"))
        except MetalApiError:
            band = None
        matches = match_albums(connection, record, band) if band else []

    info = dict(candidate)
    if band:
        info.update({key: value for key, value in band.items()
                     if value not in (None, "", [])})
    genre = str(info.get("genre") or "").strip()
    link = str(info.get("link") or "").strip()
    if not genre:
        answer = {"status": "unconfirmed", "query": query,
                  "name": info.get("name"), "link": link,
                  "album_matches": matches, "checked_at": S.now()}
        if not args.dry_run:
            cache_set(connection, record, answer)
        return {"status": "unconfirmed", "band": answer, "tracks": 0, "labels": 0}

    if (args.strict or group == "known_other"
            or (group == "unknown" and getattr(args, "require_album_match_unknown", True))) and not matches:
        if not (group in ("unknown", "known_other") and args.loose):
            answer = {"status": "unconfirmed", "query": query,
                      "name": info.get("name"), "genre": genre, "country": info.get("country"),
                      "link": link, "album_matches": [], "checked_at": S.now()}
            if not args.dry_run:
                cache_set(connection, record, answer)
            return {"status": "unconfirmed", "band": answer, "tracks": 0, "labels": 0}

    confidence = 0.95 if matches else (0.9 if group == "known_metal" else 0.6)
    applied = apply_style(connection, normalizer, record, genre, confidence, link,
                          all_tracks=args.all_tracks, dry_run=args.dry_run)
    answer = {
        "status": applied["status"], "query": query, "matched": "exact+album" if matches else "exact",
        "band_id": info.get("id"), "name": info.get("name"), "genre": genre,
        "country": info.get("country"), "link": link, "album_matches": matches,
        "confidence": confidence, "checked_at": S.now(),
        "tracks": applied["tracks"], "labels": applied["labels"],
    }
    if not args.dry_run:
        cache_set(connection, record, answer)
    return {"status": applied["status"], "band": answer,
            "tracks": applied["tracks"], "labels": applied["labels"],
            "entries": applied.get("entries", []),
            "unresolved": applied.get("unresolved", [])}


def select_records(connection, args: argparse.Namespace,
                   config: Optional[Dict[str, Any]] = None):
    """Return (selected records, group map, families, full index)."""
    families = {row["id"]: row["family"] for row in connection.execute(
        "SELECT id, family FROM styles")}
    index = build_artist_index(connection)
    records = fill_candidates(index, min_tracks=max(1, args.min_tracks))
    if args.artist:
        wanted = S.norm_key(args.artist)
        record = index.get(wanted)
        if record is None:
            record = next((item for item in index.values()
                           if S.norm_key(item["name"]) == wanted), None)
        if record is None:
            raise SystemExit(f"unknown artist: {args.artist!r}")
        group = artist_group(record, families)
        return [record], {record["key"]: group}, families, index

    include_unknown = bool(cfg(config, "include_unknown", True)) if config else True
    include_known_other = (bool(cfg(config, "include_known_other", False))
                           if config else False)
    groups = {record["key"]: artist_group(record, families) for record in records}
    selected = []
    for record in records:
        group = groups[record["key"]]
        if args.known_metal_only:
            if group != "known_metal":
                continue
        elif args.all_candidates or include_known_other:
            pass
        elif group == "known_other":
            continue
        elif group == "unknown" and not include_unknown:
            continue
        selected.append(record)
    if args.limit:
        selected = selected[:max(0, args.limit)]
    elif config:
        cap = int(cfg(config, "max_artists_per_run", 500) or 0)
        if cap > 0:
            selected = selected[:cap]
    return selected, groups, families, index


def estimate(config: Dict[str, Any], connection, args: argparse.Namespace) -> None:
    selected, groups, _families, index = select_records(connection, args, config)
    all_candidates = fill_candidates(index, min_tracks=max(1, args.min_tracks))
    cached = 0
    for record in selected:
        if not args.refresh and cache_get(connection, record["key"]):
            cached += 1
    counts = Counter(groups[record["key"]] for record in selected)
    known_other_total = sum(1 for record in all_candidates
                            if groups.get(record["key"]) == "known_other")
    known_other_selected = counts.get("known_other", 0)
    delay = max(0.0, float(args.delay if args.delay >= 0 else
                           cfg(config, "rate_limit_seconds", 0.5) or 0.5))
    uncached = len(selected) - cached
    # Search + band detail per uncached artist (an upper bound; not-found
    # artists stop after the search).
    requests = uncached * 2
    seconds = requests * max(delay, 0.05) + len(selected) * 0.05
    print(f"known metal/hardcore : {counts.get('known_metal', 0):,}")
    print(f"unknown (no style/tag): {counts.get('unknown', 0):,}")
    print(f"known other selected : {known_other_selected:,}")
    print(f"known other skipped  : {max(0, known_other_total - known_other_selected):,}")
    print(f"selected for this run : {len(selected):,} artists")
    print(f"already cached        : {cached:,} (no request)")
    print(f"requests (upper bound): {requests:,} at {delay:.2f}s -> about {seconds / 60:.1f} min")
    print("no request was made; run with --fill to execute")


def write_reports(config: Dict[str, Any], results: Sequence[Dict[str, Any]],
                  stats: Counter, args: argparse.Namespace) -> Path:
    out = S.out_dir(config)
    matches_path = out / "metal_api_matches.csv"
    with matches_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["artist", "group", "status", "cached", "band", "genre",
                         "country", "link", "album_matches", "confidence",
                         "tracks", "labels", "error"])
        for result in results:
            band = result.get("band") or {}
            writer.writerow([
                result.get("artist", ""), result.get("group", ""),
                result.get("status", ""), result.get("cached", False),
                band.get("name", ""), band.get("genre", ""), band.get("country", ""),
                band.get("link", ""), "; ".join(band.get("album_matches") or []),
                band.get("confidence", ""), result.get("tracks", 0),
                result.get("labels", 0), result.get("error", ""),
            ])

    unconfirmed_path = out / "metal_api_unconfirmed.csv"
    with unconfirmed_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["artist", "band", "genre", "country", "link", "album_matches"])
        for result in results:
            if result.get("status") != "unconfirmed":
                continue
            band = result.get("band") or {}
            writer.writerow([result.get("artist", ""), band.get("name", ""),
                             band.get("genre", ""), band.get("country", ""),
                             band.get("link", ""),
                             "; ".join(band.get("album_matches") or [])])

    ambiguous_path = out / "metal_api_ambiguous.csv"
    with ambiguous_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["artist", "candidate", "country", "genre", "link", "album_matches"])
        for result in results:
            if result.get("status") != "ambiguous":
                continue
            for item in (result.get("band") or {}).get("candidates") or []:
                writer.writerow([result.get("artist", ""), item.get("name", ""),
                                 item.get("country", ""), item.get("genre", ""),
                                 item.get("link", ""),
                                 "; ".join(item.get("album_matches") or [])])

    unmapped_path = out / "metal_api_unmapped.csv"
    with unmapped_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["artist", "genre", "link", "unresolved"])
        for result in results:
            if result.get("status") != "unmapped":
                continue
            band = result.get("band") or {}
            writer.writerow([result.get("artist", ""), band.get("genre", ""),
                             band.get("link", ""),
                             "; ".join(result.get("unresolved") or [])])

    report_path = out / "metal_api_report.txt"
    lines = [
        "# step6_metal_api - external styles from metal-api.dev",
        "",
        f"- selected artists       : {stats.get('selected', 0):,}",
        f"- known metal/hardcore  : {stats.get('known_metal', 0):,}",
        f"- unknown/no tag        : {stats.get('unknown', 0):,}",
        f"- known other (skipped) : {stats.get('skipped_known_other', 0):,}",
        f"- found                 : {stats.get('found', 0):,}",
        f"- unconfirmed           : {stats.get('unconfirmed', 0):,}",
        f"- ambiguous             : {stats.get('ambiguous', 0):,}",
        f"- not found             : {stats.get('not_found', 0):,}",
        f"- unmapped genre        : {stats.get('unmapped', 0):,}",
        f"- errors                : {stats.get('error', 0):,}",
        f"- cached answers used   : {stats.get('cached', 0):,}",
        f"- tracks filled         : {stats.get('tracks', 0):,}",
        f"- labels written        : {stats.get('labels', 0):,}",
        f"- API requests          : {stats.get('requests', 0):,}",
        f"- API errors            : {stats.get('api_errors', 0):,}",
        "",
        f"- base URL              : {cfg(config, 'base_url', DEFAULT_BASE)}",
        f"- rate limit (seconds)  : {cfg(config, 'rate_limit_seconds', 0.5)}",
        f"- strict / loose        : {args.strict} / {args.loose}",
        f"- all candidates        : {args.all_candidates}",
        f"- all tracks            : {args.all_tracks}",
        f"- dry run               : {args.dry_run}",
        "",
        "See metal_api_matches.csv, metal_api_unconfirmed.csv,",
        "metal_api_ambiguous.csv and metal_api_unmapped.csv next to this file.",
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def run(config: Dict[str, Any], args: argparse.Namespace,
        client: Optional[MetalApiClient] = None) -> Counter:
    db_path = S.resolve(args.db) if args.db else S.db_path(config)
    if not db_path.exists():
        raise SystemExit(f"dataset not found: {db_path} - run step1_scan.py first")
    connection = S.connect(db_path, create=False)
    try:
        normalizer = Normalizer(connection, config)
        selected, groups, _families, index = select_records(connection, args, config)
        all_candidates = fill_candidates(index, min_tracks=max(1, args.min_tracks))
        stats: Counter = Counter()
        stats["selected"] = len(selected)
        for record in selected:
            stats[groups[record["key"]]] += 1
        known_other_total = sum(1 for record in all_candidates
                                if groups.get(record["key"]) == "known_other")
        stats["skipped_known_other"] = (0 if args.all_candidates
                                        else known_other_total)
        if args.all_candidates:
            stats["skipped_known_other"] = max(
                0, known_other_total - stats.get("known_other", 0))

        client = client or MetalApiClient(config, debug=args.debug)
        results: List[Dict[str, Any]] = []
        consecutive_errors = 0
        total = len(selected)
        for index_no, record in enumerate(selected, 1):
            group = groups[record["key"]]
            try:
                result = resolve_record(client, connection, normalizer, record, group, args)
            except Exception as exc:  # a broken answer must not kill the run
                result = {"status": "error", "error": repr(exc), "tracks": 0, "labels": 0}
            result["artist"] = record["name"]
            result["group"] = group
            results.append(result)
            stats[str(result.get("status") or "error")] += 1
            if result.get("cached"):
                stats["cached"] += 1
            stats["tracks"] += int(result.get("tracks") or 0)
            stats["labels"] += int(result.get("labels") or 0)
            if not args.dry_run:
                connection.commit()
            band = result.get("band") or {}
            print(f"[{index_no}/{total}] {record['name']} -> {result.get('status')}"
                  f" {(band.get('genre') or '')[:60]}"
                  f" ({int(result.get('tracks') or 0)} track(s))", flush=True)
            if result.get("status") == "error":
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    print("too many API errors - stopping", flush=True)
                    break
            else:
                consecutive_errors = 0

        if not args.dry_run:
            refresh_catalog(connection)
        stats["requests"] = client.requests
        stats["api_errors"] = client.errors
        report = write_reports(config, results, stats, args)
        print()
        print(f"selected {stats['selected']:,} artist(s); found {stats['found']:,},"
              f" unconfirmed {stats['unconfirmed']:,}, ambiguous {stats['ambiguous']:,},"
              f" not found {stats['not_found']:,}, unmapped {stats['unmapped']:,},"
              f" errors {stats['error']:,}")
        print(f"tracks touched {stats['tracks']:,}; labels written {stats['labels']:,};"
              f" cached answers {stats['cached']:,}; API requests {stats['requests']:,}")
        print("report:", report)
        return stats
    finally:
        connection.close()


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Step 6 - external styles from metal-api.dev (Metal Archives data)")
    parser.add_argument("--db", default="", help="dataset path (default: config.json)")
    parser.add_argument("--fill", action="store_true",
                        help="fill missing/vague styles (default action)")
    parser.add_argument("--artist", default="", help="only this artist")
    parser.add_argument("--estimate", action="store_true",
                        help="show the candidate counts and request estimate; no network")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--known-metal-only", action="store_true",
                       help="only artists already known to be metal/hardcore")
    group.add_argument("--all-candidates", action="store_true",
                       help="also query known non-metal artists")
    group2 = parser.add_mutually_exclusive_group()
    group2.add_argument("--strict", action="store_true",
                        help="require an album match for every artist")
    group2.add_argument("--loose", action="store_true",
                        help="allow a unique exact match without an album match (unknown only)")
    parser.add_argument("--all-tracks", action="store_true",
                        help="write to every track of the artist, not only the gaps")
    parser.add_argument("--refresh", action="store_true", help="ignore the cache")
    parser.add_argument("--dry-run", action="store_true",
                        help="search and report, but write neither styles nor cache")
    parser.add_argument("--limit", type=int, default=0, help="only the first N artists")
    parser.add_argument("--min-tracks", type=int, default=1,
                        help="only artists with at least N tracks")
    parser.add_argument("--delay", type=float, default=-1.0,
                        help="override the request delay in seconds")
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    config = S.load_config()
    if args.delay >= 0:
        external = dict(config.get("external") or {})
        metal = dict(external.get("metal_api") or {})
        metal["rate_limit_seconds"] = args.delay
        external["metal_api"] = metal
        config["external"] = external
    args.require_album_match_unknown = bool(
        cfg(config, "require_album_match_for_unknown", True))
    if args.estimate:
        db_path = S.resolve(args.db) if args.db else S.db_path(config)
        if not db_path.exists():
            raise SystemExit(f"dataset not found: {db_path} - run step1_scan.py first")
        connection = S.connect(db_path, create=False)
        try:
            estimate(config, connection, args)
        finally:
            connection.close()
        return 0
    if not args.fill and not args.artist:
        print("choose --fill, --artist NAME or --estimate", file=sys.stderr)
        return 2
    run(config, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
