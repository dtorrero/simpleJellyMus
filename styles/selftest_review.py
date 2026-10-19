#!/usr/bin/env python3
"""Standalone tests for the manual style review tool.

This does **not** import or run the player's ``selftest.py``. Everything is
built in a throwaway directory: a tiny catalog, a tiny overrides file and a
temporary music root. Run it with:

    python3 styles/selftest_review.py

The GUI smoke test is skipped automatically when no display is available.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import tkinter as tk
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

STYLES_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(STYLES_DIR))

import review_gui as R  # noqa: E402
import styles_db as S  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="simplejellymus-review-"))


class Reporter:
    def __init__(self) -> None:
        self.failures: List[str] = []

    def check(self, label: str, ok: Any, detail: str = "") -> None:
        passed = bool(ok)
        print(f"  [{'ok' if passed else 'FAIL'}] {label}"
              + (f" — {detail}" if detail and not passed else ""))
        if not passed:
            self.failures.append(label)


def config_for(base: Path) -> Dict[str, Any]:
    config = json.loads(json.dumps(S.load_config()))
    config["db_path"] = str(base / "catalog.sqlite")
    config["out_dir"] = str(base / "out")
    config["music_root"] = str(base / "music")
    normalize = dict(config.get("normalize") or {})
    normalize["overrides_file"] = str(base / "overrides.json")
    config["normalize"] = normalize
    return config


def insert_track(connection, *, rel_path: str, name: str, artist: str, album: str,
                 track_no: int, raw_genre: str, primary: Optional[str] = None,
                 family: Optional[str] = None, source: Optional[str] = None,
                 labels: Sequence[Sequence[Any]] = ()) -> None:
    connection.execute(
        "INSERT INTO tracks(rel_path, name, album, album_artist, artists, year,"
        " raw_genre, primary_style, family, label_source, confidence,"
        " artist_key, album_key, track_no, scanned_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rel_path, name, album, artist, artist, 2020, raw_genre, primary, family,
         source, 1.0 if primary else None, S.norm_key(artist),
         S.norm_key(f"{artist} :: {album}"), track_no, S.now()))
    for style_id, weight, label_source, confidence in labels:
        connection.execute(
            "INSERT INTO track_styles(rel_path, style_id, weight, source, confidence,"
            " evidence, updated_at) VALUES(?,?,?,?,?,?,?)",
            (rel_path, style_id, weight, label_source, confidence,
             f"fixture {label_source}", S.now()))


def build_catalog(config: Dict[str, Any]) -> None:
    connection = S.connect(Path(config["db_path"]))
    styles = [
        ("deathcore", "Deathcore", "Hardcore"),
        ("metalcore", "Metalcore", "Hardcore"),
        ("death-metal", "Death Metal", "Metal"),
        ("metal-unspecified", "Metal (unspecified)", "Metal"),
        ("hip-hop", "Hip Hop", "Hip Hop"),
        ("trap", "Trap", "Hip Hop"),
        ("rap-metal", "Rap Metal", "Metal"),
    ]
    connection.executemany(
        "INSERT INTO styles(id, name, family, aliases, search_key, source, tracks, artists)"
        " VALUES(?,?,?,?,?,'seed',0,0)",
        [(style_id, name, family, "[]", S.norm_key(name))
         for style_id, name, family in styles])
    insert_track(connection, rel_path="Band/Album/01 - Corruption.mp3",
                 name="Corruption", artist="Band", album="Album", track_no=1,
                 raw_genre="")
    insert_track(connection, rel_path="Band/Album/02 - Two.mp3",
                 name="Two", artist="Band", album="Album", track_no=2,
                 raw_genre="Death Metal", primary="death-metal", family="Metal",
                 source="tag", labels=[("death-metal", 1.0, "tag", 1.0)])
    insert_track(connection, rel_path="Band/Album/03 - Three.mp3",
                 name="Three", artist="Band", album="Album", track_no=3,
                 raw_genre="Metal", primary="metal-unspecified", family="Metal",
                 source="rules", labels=[("metal-unspecified", 1.0, "rules", 0.8)])
    insert_track(connection, rel_path="Other/Album/01 - Four.mp3",
                 name="Four", artist="Other", album="Album", track_no=1,
                 raw_genre="", primary="hip-hop", family="Hip Hop", source="llm",
                 labels=[("hip-hop", 1.0, "llm", 0.4)])
    insert_track(connection, rel_path="Band/Bad/01 - Wrong.mp3",
                 name="Wrong", artist="Band", album="Bad", track_no=1,
                 raw_genre="", primary="hip-hop", family="Hip Hop", source="llm",
                 labels=[("hip-hop", 1.0, "llm", 0.4), ("trap", 0.7, "llm", 0.4)])
    connection.commit()
    connection.close()
    Path(config["music_root"]).mkdir(parents=True, exist_ok=True)


def test_store(report: Reporter, config: Dict[str, Any]) -> None:
    print("\nOverrideStore")
    path = Path(config["normalize"]["overrides_file"])
    store = R.OverrideStore(path, backup_dir=Path(config["out_dir"]))
    store.set("tracks", "Band/Album/01 - Corruption.mp3", ["Deathcore"],
              exclusive=True)
    store.set("albums", "Band :: Album", ["Metalcore"], exclusive=False)
    store.set("artists", "Band", ["Deathcore"], exclusive=True)
    data = store.load()
    report.check("track override written",
                 data["tracks"]["Band/Album/01 - Corruption.mp3"]["styles"] == ["Deathcore"])
    report.check("album override written",
                 data["albums"]["Band :: Album"]["styles"] == ["Metalcore"])
    report.check("artist override written and exclusive",
                 data["artists"]["Band"].get("exclusive") is True)
    report.check("existing entries are preserved",
                 "tracks" in data and "albums" in data and "artists" in data)
    report.check("track is found before album/artist",
                 (store.find({"rel_path": "Band/Album/01 - Corruption.mp3",
                              "album_artist": "Band", "album": "Album"}) or ("",))[0] == "tracks")
    report.check("album is found for another track",
                 (store.find({"rel_path": "Band/Album/02 - Two.mp3",
                              "album_artist": "Band", "album": "Album"}) or ("",))[0] == "albums")
    report.check("artist is found for the third album",
                 (store.find({"rel_path": "Band/Bad/01 - Wrong.mp3",
                              "album_artist": "Band", "album": "Bad"}) or ("",))[0] == "artists")
    report.check("a backup was written before overwriting",
                 bool(list(Path(config["out_dir"]).glob("overrides.json.bak-*"))))
    report.check("no .tmp file is left behind",
                 not (path.parent / (path.name + ".tmp")).exists())
    store.remove("albums", "Band :: Album")
    report.check("remove deletes the album entry",
                 "Band :: Album" not in store.load()["albums"])


def test_catalog(report: Reporter, config: Dict[str, Any]) -> None:
    print("\nReviewCatalog")
    catalog = R.ReviewCatalog(config)
    try:
        rows, total = catalog.queue("no_style")
        report.check("no_style finds the unlabelled track",
                     [row["rel_path"] for row in rows]
                     == ["Band/Album/01 - Corruption.mp3"], str(total))
        rows, total = catalog.queue("no_tag")
        report.check("no_tag finds every empty raw_genre",
                     len(rows) == 3 and total == 3,
                     ", ".join(row["rel_path"] for row in rows))
        rows, _total = catalog.queue("vague")
        report.check("vague finds (unspecified) and name=family styles",
                     {row["rel_path"] for row in rows}
                     == {"Band/Album/03 - Three.mp3",
                         "Other/Album/01 - Four.mp3",
                         "Band/Bad/01 - Wrong.mp3"},
                     ", ".join(row["rel_path"] for row in rows))
        rows, total = catalog.queue("all")
        report.check("all returns the whole fixture", len(rows) == 5, str(total))
        rows, _total = catalog.queue("no_style", query="corruption")
        report.check("the queue filter is case/space friendly",
                     len(rows) == 1 and rows[0]["name"] == "Corruption")
        report.check("the style vocabulary is loaded", len(catalog.styles()) == 7)

        all_rows = {row["rel_path"]: row for row in catalog.queue("all")[0]}
        track = all_rows["Band/Album/01 - Corruption.mp3"]
        report.check("the album suggestion ignores vague labels",
                     catalog.suggestion(track) == "Death Metal",
                     str(catalog.suggestion(track)))
        report.check("album scope count", catalog.scope_count("albums", track) == 3)
        report.check("artist scope count", catalog.scope_count("artists", track) == 4)
        report.check("labels are read back",
                     len(catalog.labels("Band/Bad/01 - Wrong.mp3")) == 2)

        review_path = Path(config["out_dir"]) / "review.csv"
        review_path.write_text(
            "decision,confidence,agree,n_sources,path,artist,album,year,raw_genre,"
            "primary_style,label_source,metadata_says,external_says,audio_says,llm_says\n"
            "conflict,0.58,1/2,2,Band/Album/01 - Corruption.mp3,Band,Album,2020,,"
            "hip-hop,llm,,,,Rap Metal\n", encoding="utf-8")
        rows, total = catalog.queue("review")
        report.check("review mode reads out/review.csv",
                     len(rows) == 1 and rows[0].get("_review", {}).get("decision") == "conflict")
    finally:
        catalog.close()


def _labels(catalog: R.ReviewCatalog, rel_path: str) -> Dict[str, Dict[str, Any]]:
    return {row["name"]: row for row in catalog.labels(rel_path)}


def test_apply(report: Reporter, config: Dict[str, Any]) -> None:
    print("\nApply overrides")
    path = Path(config["normalize"]["overrides_file"])
    path.unlink(missing_ok=True)
    store = R.OverrideStore(path, backup_dir=Path(config["out_dir"]))
    store.set("tracks", "Band/Bad/01 - Wrong.mp3", ["Deathcore"], exclusive=True)
    store.set("tracks", "Band/Album/02 - Two.mp3", ["Metalcore"], exclusive=False)
    store.set("artists", "Other", ["Trap"], exclusive=True)
    catalog = R.ReviewCatalog(config)
    try:
        catalog.apply_overrides()
        wrong = _labels(catalog, "Band/Bad/01 - Wrong.mp3")
        report.check("exclusive removes the old labels",
                     set(wrong) == {"Deathcore"}, ", ".join(wrong))
        report.check("exclusive writes source=human",
                     wrong.get("Deathcore", {}).get("source") == "human")
        row = catalog.connection.execute(
            "SELECT primary_style, family, label_source FROM tracks WHERE rel_path=?",
            ("Band/Bad/01 - Wrong.mp3",)).fetchone()
        report.check("exclusive updates primary_style/family/source",
                     tuple(row) == ("deathcore", "Hardcore", "human"), str(tuple(row)))

        two = _labels(catalog, "Band/Album/02 - Two.mp3")
        report.check("non-exclusive keeps the old tag label",
                     "Death Metal" in two and two["Death Metal"]["source"] == "tag")
        report.check("non-exclusive adds the human label",
                     two.get("Metalcore", {}).get("source") == "human")
        row = catalog.connection.execute(
            "SELECT primary_style, label_source FROM tracks WHERE rel_path=?",
            ("Band/Album/02 - Two.mp3",)).fetchone()
        report.check("human outranks the tag as primary",
                     tuple(row) == ("metalcore", "human"), str(tuple(row)))

        other = _labels(catalog, "Other/Album/01 - Four.mp3")
        report.check("exclusive artist override clears the artist bucket",
                     set(other) == {"Trap"}, ", ".join(other))
        artist = catalog.connection.execute(
            "SELECT styles, family, label_source FROM artists WHERE key='other'").fetchone()
        report.check("the artist roll-up is updated for an exclusive artist override",
                     artist is not None and artist["styles"] == '["trap"]'
                     and artist["family"] == "Hip Hop"
                     and artist["label_source"] == "human",
                     str(dict(artist)) if artist else "missing")

        before = {rel: len(catalog.labels(rel)) for rel in
                  ("Band/Bad/01 - Wrong.mp3", "Band/Album/02 - Two.mp3",
                   "Other/Album/01 - Four.mp3")}
        catalog.apply_overrides()
        after = {rel: len(catalog.labels(rel)) for rel in before}
        report.check("applying the same overrides twice is idempotent",
                     before == after, f"{before} -> {after}")
    finally:
        catalog.close()


def test_gui(report: Reporter, config: Dict[str, Any]) -> None:
    print("\nGUI smoke test")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        report.check("GUI smoke test skipped (no display)", True, str(exc))
        return
    root.withdraw()
    app: Optional[R.ReviewApp] = None
    try:
        app = R.ReviewApp(root, config=config, db_path=config["db_path"],
                          overrides_path=config["normalize"]["overrides_file"])
        root.update()
        app.reload_queue()
        report.check("the GUI loads the unlabelled queue",
                     app.current is not None
                     and app.current["rel_path"] == "Band/Album/01 - Corruption.mp3",
                     str((app.current or {}).get("rel_path")))
        app._select_style_by_name("Deathcore")
        report.check("the GUI can select a style by name",
                     app.selected_style is not None
                     and app.selected_style["name"] == "Deathcore")
        app.save("tracks")
        row = app.catalog.connection.execute(
            "SELECT primary_style, label_source FROM tracks WHERE rel_path=?",
            ("Band/Album/01 - Corruption.mp3",)).fetchone()
        report.check("saving from the GUI applies source=human",
                     row is not None and tuple(row) == ("deathcore", "human"),
                     str(tuple(row)) if row else "missing")
        labels = _labels(app.catalog, "Band/Album/01 - Corruption.mp3")
        report.check("the GUI leaves only the manual label with exclusive on",
                     set(labels) == {"Deathcore"}, ", ".join(labels))
    finally:
        if app is not None:
            app.close()
        else:
            root.destroy()


def main() -> int:
    report = Reporter()
    config = config_for(TMP)
    build_catalog(config)
    try:
        for label, function in (
                ("OverrideStore", test_store),
                ("ReviewCatalog", test_catalog),
                ("Apply overrides", test_apply),
                ("GUI smoke test", test_gui)):
            try:
                function(report, config)
            except Exception as exc:  # one crashed section must not hide the rest
                report.check(f"{label} crashed", False, repr(exc))
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print()
    if report.failures:
        print(f"FAILED ({len(report.failures)}): " + "; ".join(report.failures))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
