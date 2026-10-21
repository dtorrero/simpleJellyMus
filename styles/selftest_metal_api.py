#!/usr/bin/env python3
"""Standalone tests for step6_metal_api (no real API, no player selftest).

    python3 styles/selftest_metal_api.py
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

STYLES_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(STYLES_DIR))

import step6_metal_api as M  # noqa: E402
import styles_db as S  # noqa: E402
from step3_vocabulary import build, load_vocabulary  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="simplejellymus-metal-api-"))


class Reporter:
    def __init__(self) -> None:
        self.failures: List[str] = []

    def check(self, label: str, ok: Any, detail: str = "") -> None:
        passed = bool(ok)
        print(f"  [{'ok' if passed else 'FAIL'}] {label}"
              + (f" — {detail}" if detail and not passed else ""))
        if not passed:
            self.failures.append(label)


BANDS = {
    "1": {"id": "1", "name": "Known Metal", "genre": "Melodic Death Metal",
          "country": "Testland", "link": "https://example.test/bands/1",
          "albums": [{"name": "Album One"}]},
    "2": {"id": "2", "name": "13 Dead", "genre": "Technical/Melodic Death Metal",
          "country": "Canada", "link": "https://example.test/bands/2",
          "albums": [{"name": "13 Dead"}]},
    "3": {"id": "3", "name": "No Album Match", "genre": "Death Metal",
          "country": "Testland", "link": "https://example.test/bands/3",
          "albums": [{"name": "Elsewhere"}]},
    "4": {"id": "4", "name": "Ambiguous", "genre": "Death Metal",
          "country": "Rightland", "link": "https://example.test/bands/4",
          "albums": [{"name": "The Right Album"}]},
    "5": {"id": "5", "name": "Ambiguous", "genre": "Thrash Metal",
          "country": "Wrongland", "link": "https://example.test/bands/5",
          "albums": [{"name": "Another Album"}]},
    "6": {"id": "6", "name": "Unmapped", "genre": "Zorblaxian",
          "country": "Testland", "link": "https://example.test/bands/6",
          "albums": [{"name": "Unmapped Album"}]},
    "7": {"id": "7", "name": "Known Other", "genre": "Hard Rock",
          "country": "Testland", "link": "https://example.test/bands/7",
          "albums": [{"name": "Other Album"}]},
}
SEARCHES = {
    "Known Metal": [BANDS["1"]],
    "13 Dead": [BANDS["2"]],
    "No Album Match": [BANDS["3"]],
    "Ambiguous": [BANDS["4"], BANDS["5"]],
    "Not Found": [],
    "Unmapped": [BANDS["6"]],
    "Known Other": [BANDS["7"]],
}


class FakeMetalApi(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeMetalApi/1.0"
    requests = 0
    paths: List[str] = []

    def log_message(self, *args: Any) -> None:
        pass

    def _json(self, data: Any) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Rate-Limit-Limit", "10s")
        self.send_header("X-Rate-Limit-Remaining", "29")
        self.send_header("X-Rate-Limit-Reset", "2099-01-01T00:00:10.0000000Z")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        FakeMetalApi.requests += 1
        FakeMetalApi.paths.append(self.path)
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        prefix = "/search/bands/name/"
        if path.startswith(prefix):
            name = path[len(prefix):]
            return self._json(SEARCHES.get(name, []))
        if path.startswith("/bands/"):
            band = BANDS.get(path[len("/bands/"):])
            return self._json(band if band else {})
        self.send_error(404)


def start_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMetalApi)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def config_for(base: Path, port: int) -> Dict[str, Any]:
    config = json.loads(json.dumps(S.load_config()))
    config["db_path"] = str(base / "catalog.sqlite")
    config["out_dir"] = str(base / "out")
    config["music_root"] = str(base / "music")
    normalize = dict(config.get("normalize") or {})
    normalize["overrides_file"] = str(base / "overrides.json")
    config["normalize"] = normalize
    external = dict(config.get("external") or {})
    external["metal_api"] = {
        "base_url": f"http://127.0.0.1:{port}",
        "rate_limit_seconds": 0.0,
        "request_timeout": 5,
        "max_retries": 0,
        "max_artists_per_run": 500,
        "include_unknown": True,
        "include_known_other": False,
        "require_album_match_for_unknown": True,
    }
    config["external"] = external
    return config


def add_track(connection, rel_path: str, name: str, artist: str, album: str,
              track_no: int, raw_genre: str = "", primary: Optional[str] = None,
              family: Optional[str] = None, labels: Sequence[Sequence[Any]] = ()) -> None:
    connection.execute(
        "INSERT INTO tracks(rel_path, name, album, album_artist, artists, year,"
        " raw_genre, primary_style, family, label_source, confidence, artist_key,"
        " album_key, track_no, scanned_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rel_path, name, album, artist, artist, 2020, raw_genre, primary, family,
         "tag" if labels else None, 1.0 if primary else None, S.norm_key(artist),
         S.norm_key(f"{artist} :: {album}"), track_no, S.now()))
    for style_id, weight, source, confidence in labels:
        connection.execute(
            "INSERT INTO track_styles(rel_path, style_id, weight, source, confidence,"
            " evidence, updated_at) VALUES(?,?,?,?,?,?,?)",
            (rel_path, style_id, weight, source, confidence, f"fixture {source}", S.now()))


def build_catalog(config: Dict[str, Any]) -> None:
    connection = S.connect(Path(config["db_path"]))
    styles = [
        ("melodic-death-metal", "Melodic Death Metal", "Metal"),
        ("technical-death-metal", "Technical Death Metal", "Metal"),
        ("death-metal", "Death Metal", "Metal"),
        ("metal-unspecified", "Metal (unspecified)", "Metal"),
        ("rock", "Rock", "Rock"),
        ("hard-rock", "Hard Rock", "Rock"),
    ]
    connection.executemany(
        "INSERT INTO styles(id, name, family, aliases, search_key, source, tracks, artists)"
        " VALUES(?,?,?,?,?,'seed',0,0)",
        [(style_id, name, family, "[]", S.norm_key(name))
         for style_id, name, family in styles])

    for index_no in (1, 2):
        add_track(connection, f"Known Metal/Album One/0{index_no}.mp3",
                  f"Song {index_no}", "Known Metal", "Album One", index_no,
                  raw_genre="Metal", primary="metal-unspecified", family="Metal",
                  labels=[("death-metal", 1.0, "tag", 1.0)])
    for index_no in range(1, 5):
        add_track(connection, f"13 Dead/13 Dead/0{index_no}.mp3",
                  f"Dead {index_no}", "13 Dead", "13 Dead", index_no)
    for index_no in (1, 2):
        add_track(connection, f"No Album Match/Catalog Album/0{index_no}.mp3",
                  f"No Match {index_no}", "No Album Match", "Catalog Album", index_no)
        add_track(connection, f"Ambiguous/The Right Album/0{index_no}.mp3",
                  f"Right {index_no}", "Ambiguous", "The Right Album", index_no)
    add_track(connection, "Not Found/Whatever/01.mp3", "Missing", "Not Found", "Whatever", 1)
    add_track(connection, "Unmapped/Unmapped Album/01.mp3", "Strange", "Unmapped",
              "Unmapped Album", 1)
    add_track(connection, "Known Other/Other Album/01.mp3", "Rock Song", "Known Other",
              "Other Album", 1, raw_genre="Rock", primary="rock", family="Rock")
    # Normalizer requires every vocabulary style to exist in the dataset.
    build(connection, load_vocabulary(config))
    connection.commit()
    connection.close()
    Path(config["music_root"]).mkdir(parents=True, exist_ok=True)


def args_for(db: Path, **overrides: Any) -> argparse.Namespace:
    values: Dict[str, Any] = {
        "db": str(db), "fill": True, "artist": "", "estimate": False,
        "known_metal_only": False, "all_candidates": False, "strict": False,
        "loose": False, "all_tracks": False, "refresh": False, "dry_run": False,
        "limit": 0, "min_tracks": 1, "delay": 0.0, "debug": False,
        "require_album_match_unknown": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def labels(connection, rel_path: str) -> List[Dict[str, Any]]:
    return [dict(row) for row in connection.execute(
        "SELECT s.name, ts.source, ts.weight FROM track_styles ts"
        " JOIN styles s ON s.id=ts.style_id WHERE ts.rel_path=?"
        " ORDER BY ts.weight DESC, ts.source DESC", (rel_path,))]


def test_dry_run(report: Reporter, config: Dict[str, Any], db: Path) -> None:
    print("\nDry run")
    FakeMetalApi.requests = 0
    client = M.MetalApiClient(config)
    stats = M.run(config, args_for(db, dry_run=True), client)
    report.check("selected known metal + unknown artists", stats["selected"] == 6,
                 str(stats["selected"]))
    report.check("group counts", stats["known_metal"] == 1 and stats["unknown"] == 5,
                 f"{stats['known_metal']}/{stats['unknown']}")
    report.check("known-other artist skipped", stats["skipped_known_other"] == 1,
                 str(stats["skipped_known_other"]))
    report.check("dispositions", (stats["found"] == 3 and stats["unconfirmed"] == 1
                                  and stats["not_found"] == 1 and stats["unmapped"] == 1),
                 str({k: stats[k] for k in ("found", "unconfirmed", "not_found", "unmapped")}))
    report.check("dry run counts tracks/labels", stats["tracks"] == 8 and stats["labels"] == 8,
                 f"{stats['tracks']}/{stats['labels']}")
    report.check("dry run made API requests", stats["requests"] > 0, str(stats["requests"]))
    connection = S.connect(db, create=False)
    try:
        external = connection.execute(
            "SELECT COUNT(*) FROM track_styles WHERE source='external'").fetchone()[0]
        cached = connection.execute(
            "SELECT COUNT(*) FROM artists WHERE external_json IS NOT NULL").fetchone()[0]
        report.check("dry run wrote no styles", external == 0, str(external))
        report.check("dry run wrote no cache", cached == 0, str(cached))
    finally:
        connection.close()


def test_fill(report: Reporter, config: Dict[str, Any], db: Path) -> None:
    print("\nFill")
    FakeMetalApi.requests = 0
    client = M.MetalApiClient(config)
    stats = M.run(config, args_for(db), client)
    report.check("fill dispositions", (stats["found"] == 3 and stats["unconfirmed"] == 1
                                       and stats["not_found"] == 1 and stats["unmapped"] == 1),
                 str({k: stats[k] for k in ("found", "unconfirmed", "not_found", "unmapped")}))
    connection = S.connect(db, create=False)
    try:
        dead = labels(connection, "13 Dead/13 Dead/01.mp3")
        dead_names = {row["name"] for row in dead}
        report.check("13 Dead gets the mapped style",
                     dead_names == {"Melodic Death Metal"},
                     str(dead_names))
        report.check("13 Dead labels are external",
                     all(row["source"] == "external" for row in dead))
        primary = connection.execute(
            "SELECT primary_style, label_source FROM tracks WHERE rel_path=?",
            ("13 Dead/13 Dead/01.mp3",)).fetchone()
        report.check("13 Dead primary is the mapped style",
                     tuple(primary) == ("melodic-death-metal", "external"), str(tuple(primary)))

        known = labels(connection, "Known Metal/Album One/01.mp3")
        known_names = {row["name"] for row in known}
        report.check("external label joins the existing tag",
                     "Melodic Death Metal" in known_names and "Death Metal" in known_names,
                     str(known_names))
        primary = connection.execute(
            "SELECT primary_style, label_source FROM tracks WHERE rel_path=?",
            ("Known Metal/Album One/01.mp3",)).fetchone()
        report.check("the tag keeps the primary style",
                     tuple(primary) == ("death-metal", "tag"), str(tuple(primary)))

        report.check("no album match stayed unconfirmed",
                     not labels(connection, "No Album Match/Catalog Album/01.mp3"))
        amb = labels(connection, "Ambiguous/The Right Album/01.mp3")
        report.check("album match picked the right same-name band",
                     len(amb) == 1 and amb[0]["name"] == "Death Metal",
                     str(amb))
        report.check("unmapped genre wrote no label",
                     not labels(connection, "Unmapped/Unmapped Album/01.mp3"))
        report.check("known-other was skipped by default",
                     not labels(connection, "Known Other/Other Album/01.mp3"))

        cached = connection.execute(
            "SELECT external_json FROM artists WHERE key='13 dead'").fetchone()
        report.check("the answer is cached",
                     bool(cached and '"metal_api"' in cached["external_json"]))
    finally:
        connection.close()


def test_cache(report: Reporter, config: Dict[str, Any], db: Path) -> None:
    print("\nCache")
    FakeMetalApi.requests = 0
    client = M.MetalApiClient(config)
    stats = M.run(config, args_for(db), client)
    report.check("a second run makes no HTTP requests", client.requests == 0,
                 str(client.requests))
    report.check("the unresolved artists are cached too",
                 stats["cached"] == 3 and stats["selected"] == 3,
                 f"selected {stats['selected']}, cached {stats['cached']}")
    report.check("cached dispositions are preserved",
                 stats["unconfirmed"] == 1 and stats["not_found"] == 1
                 and stats["unmapped"] == 1,
                 str({k: stats[k] for k in ("unconfirmed", "not_found", "unmapped")}))


def test_all_candidates(report: Reporter, config: Dict[str, Any], db: Path) -> None:
    print("\nAll candidates")
    FakeMetalApi.requests = 0
    client = M.MetalApiClient(config)
    stats = M.run(config, args_for(db, all_candidates=True), client)
    report.check("known-other is included with --all-candidates",
                 stats["selected"] == 4, str(stats["selected"]))
    report.check("known-other is confirmed by its album",
                 stats["found"] == 1 and client.requests == 2,
                 f"found {stats['found']}, requests {client.requests}")
    connection = S.connect(db, create=False)
    try:
        other = labels(connection, "Known Other/Other Album/01.mp3")
        report.check("known-other got the API style",
                     len(other) == 1 and other[0]["name"] == "Hard Rock"
                     and other[0]["source"] == "external", str(other))
        primary = connection.execute(
            "SELECT primary_style, label_source FROM tracks WHERE rel_path=?",
            ("Known Other/Other Album/01.mp3",)).fetchone()
        report.check("known-other primary is the external style",
                     tuple(primary) == ("hard-rock", "external"), str(tuple(primary)))
    finally:
        connection.close()


def test_artist(report: Reporter, config: Dict[str, Any], db: Path) -> None:
    print("\nArtist mode")
    FakeMetalApi.requests = 0
    client = M.MetalApiClient(config)
    stats = M.run(config, args_for(db, artist="13 Dead", refresh=True, dry_run=True), client)
    report.check("--artist searches only that band",
                 stats["selected"] == 1 and client.requests == 2
                 and stats["found"] == 1,
                 f"selected {stats['selected']}, requests {client.requests}, "
                 f"found {stats['found']}")


def main() -> int:
    report = Reporter()
    server, port = start_server()
    config = config_for(TMP, port)
    build_catalog(config)
    db = Path(config["db_path"])
    try:
        for label, function in (
                ("Dry run", test_dry_run),
                ("Fill", test_fill),
                ("Cache", test_cache),
                ("All candidates", test_all_candidates),
                ("Artist mode", test_artist)):
            try:
                function(report, config, db)
            except Exception as exc:
                report.check(f"{label} crashed", False, repr(exc))
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(TMP, ignore_errors=True)
    print()
    if report.failures:
        print(f"FAILED ({len(report.failures)}): " + "; ".join(report.failures))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
