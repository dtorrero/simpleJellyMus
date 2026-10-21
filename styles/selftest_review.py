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
from catalog import Catalog  # noqa: E402
from step3_vocabulary import (build, load_vocabulary, normalize_raw,  # noqa: E402
                              validate)

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


class StubPreview:
    """In-memory stand-in for PreviewPlayer (the tests never start mpv)."""

    def __init__(self) -> None:
        self.played: List[str] = []
        self.seeks: List[float] = []
        self.volumes: List[int] = []
        self.volume = 80
        self.source = ""
        self.position = 0.0
        self.duration = 0.0
        self.paused = True
        self.ended = False
        self.closed = False

    def play(self, source: str) -> None:
        self.source = str(source)
        self.position = 0.0
        self.duration = 180.0
        self.paused = False
        self.ended = False
        self.played.append(self.source)

    def toggle_pause(self) -> bool:
        self.paused = not self.paused
        return self.paused

    def stop(self) -> None:
        self.source = ""
        self.position = 0.0
        self.duration = 0.0
        self.paused = True
        self.ended = False

    def close(self) -> None:
        self.closed = True
        self.stop()

    def seek(self, seconds: float) -> None:
        self.seeks.append(float(seconds))
        self.position = float(seconds)

    def set_volume(self, value: float) -> None:
        volume = max(0, min(100, int(round(float(value)))))
        self.volume = volume
        self.volumes.append(volume)

    def snapshot(self) -> Dict[str, Any]:
        return {"source": self.source, "position": self.position,
                "duration": self.duration, "paused": self.paused,
                "ended": self.ended}


def config_for(base: Path) -> Dict[str, Any]:
    config = json.loads(json.dumps(S.load_config()))
    config["db_path"] = str(base / "catalog.sqlite")
    config["out_dir"] = str(base / "out")
    config["music_root"] = str(base / "music")
    normalize = dict(config.get("normalize") or {})
    normalize["overrides_file"] = str(base / "overrides.json")
    normalize["styles_file"] = str(base / "styles.json")
    config["normalize"] = normalize
    return config


def write_vocabulary(config: Dict[str, Any]) -> None:
    """A small, valid vocabulary that matches the fixture catalog."""
    path = Path(config["normalize"]["styles_file"])
    data = {
        "_comment": ["fixture vocabulary for selftest_review.py"],
        "version": 1,
        "families": ["Metal", "Hardcore", "Hip Hop"],
        "styles": [
            {"name": "Deathcore", "family": "Hardcore", "aliases": ["deathcore"]},
            {"name": "Metalcore", "family": "Hardcore", "aliases": ["metalcore"]},
            {"name": "Death Metal", "family": "Metal",
             "aliases": ["death metal", "death"]},
            {"name": "Hip Hop", "family": "Hip Hop", "aliases": ["hip hop", "rap"]},
            {"name": "Trap", "family": "Hip Hop", "aliases": ["trap"]},
            {"name": "Rap Metal", "family": "Metal", "aliases": ["rap metal", "rapcore"]},
        ],
    }
    path.write_text(R.dump_vocabulary(data), encoding="utf-8")


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
    for rel_path in ("Band/Album/01 - Corruption.mp3",
                     "Band/Album/02 - Two.mp3",
                     "Band/Album/03 - Three.mp3",
                     "Other/Album/01 - Four.mp3",
                     "Band/Bad/01 - Wrong.mp3"):
        sample = Path(config["music_root"]) / rel_path
        sample.parent.mkdir(parents=True, exist_ok=True)
        sample.write_bytes(b"stub audio for the playback test")


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
                     catalog.suggestion(track) == "death-metal",
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


def test_vocabulary(report: Reporter, config: Dict[str, Any]) -> None:
    print("\nVocabularyStore")
    fixture = Path(config["normalize"]["styles_file"])
    path = fixture.with_name("vocabulary-test.json")
    shutil.copy2(fixture, path)
    store = R.VocabularyStore(path, backup_dir=Path(config["out_dir"]))

    def rejected(label: str, action: Any) -> None:
        try:
            action()
        except ValueError:
            report.check(label, True)
        else:
            report.check(label, False, "no ValueError")

    entry, moved = store.add("Melodic Death Metal", "metal", parent="death metal",
                             aliases=["melodeath", "MDM", "melodeath",
                                      "Melodic Death Metal"])
    report.check("a style is stored in the existing format",
                 entry == {"name": "Melodic Death Metal", "family": "Metal",
                           "parent": "Death Metal",
                           "aliases": ["melodeath", "MDM"]},
                 repr(entry))
    report.check("an unrelated new name moves no aliases", moved == [], repr(moved))
    data = store.load()
    report.check("the new style is the last vocabulary entry",
                 data["styles"][-1] == entry)
    report.check("existing vocabulary entries are preserved",
                 [style["name"] for style in data["styles"][:6]]
                 == ["Deathcore", "Metalcore", "Death Metal", "Hip Hop",
                     "Trap", "Rap Metal"])

    added, _moved = store.add("Breakcore", "electronic", aliases=["breakcore"])
    report.check("a new family is appended to the families list",
                 added["family"] == "Electronic" and "Electronic" in store.families(),
                 repr(added))

    report.check("a style's own name listed as an alias is not moved",
                 store.alias_moves("Death Metal") == [],
                 repr(store.alias_moves("Death Metal")))
    duplicate, duplicate_moved = store.add(
        "Death Metal", "Hardcore", aliases=["hardcore death metal"])
    report.check("the same name can be added under a different family",
                 duplicate["name"] == "Death Metal"
                 and duplicate["family"] == "Hardcore", repr(duplicate))
    report.check("adding a duplicate name moves no self-name alias",
                 duplicate_moved == [], repr(duplicate_moved))
    report.check("both same-named styles are in the vocabulary",
                 sum(1 for style in store.load()["styles"]
                     if style["name"] == "Death Metal") == 2)
    metal_death = next(style for style in store.load()["styles"]
                       if style["name"] == "Death Metal"
                       and style["family"] == "Metal")
    report.check("the original style keeps its aliases",
                 "death metal" in metal_death["aliases"],
                 repr(metal_death["aliases"]))

    text = path.read_text(encoding="utf-8")
    style_lines = [line for line in text.splitlines()
                   if line.strip().startswith('{"name"')]
    report.check("styles stay one per line",
                 len(style_lines) == len(store.load()["styles"]),
                 f"{len(style_lines)} lines, {len(store.load()['styles'])} styles")
    report.check("a backup was written before overwriting",
                 bool(list(Path(config["out_dir"]).glob("vocabulary-test.json.bak-*"))))
    report.check("no .tmp file is left behind",
                 not path.with_name(path.name + ".tmp").exists())

    rejected("a duplicate style name is rejected",
             lambda: store.add("melodic death metal", "Metal"))
    rejected("an alias owned by another style is rejected",
             lambda: store.add("War Metal", "Metal", aliases=["death"]))
    rejected("a parent outside the vocabulary is rejected",
             lambda: store.add("War Metal", "Metal", parent="Black Metal"))
    rejected("a parent in another family is rejected",
             lambda: store.add("War Metal", "Metal", parent="Trap"))
    rejected("a style cannot be its own sub-style",
             lambda: store.add("Death Metal", "Metal", parent="Death Metal"))
    rejected("a catalog-only placeholder is rejected",
             lambda: store.add(
                 "Metal (unspecified)", "Metal",
                 reserved_names={S.style_pair_key("Metal (unspecified)", "Metal"):
                                 "'Metal (unspecified)'"}))
    store.add("Rhythm and Blues", "Hip Hop", aliases=["R&B"])
    rejected("an alias that only collides after tag normalisation is rejected",
             lambda: store.add("RnB", "Hip Hop", aliases=["R B"]))

    moves = store.alias_moves("Death")
    report.check("a style name that is an alias is reported as a move",
                 moves == [("Death Metal", "Metal", "death")], repr(moves))
    entry, moved = store.add("Death", "Punk", aliases=[])
    report.check("the alias is moved to the new style",
                 moved == [("Death Metal", "Metal", "death")], repr(moved))
    deaths = [style for style in store.load()["styles"] if style["name"] == "Death Metal"]
    report.check("the old style no longer owns the alias",
                 all("death" not in (style.get("aliases") or []) for style in deaths),
                 repr([style.get("aliases") for style in deaths]))
    report.check("the new style is appended", entry["name"] == "Death"
                 and entry["family"] == "Punk", repr(entry))


def test_duplicate_styles(report: Reporter, _config: Dict[str, Any]) -> None:
    print("\nDuplicate style names")
    base = TMP / "duplicates"
    base.mkdir(parents=True, exist_ok=True)
    config = config_for(base)
    vocabulary = {
        "_comment": ["duplicate-name fixture"],
        "version": 1,
        "families": ["Metal", "Hardcore", "Punk"],
        "styles": [
            {"name": "Death Metal", "family": "Metal",
             "aliases": ["death metal", "progressive deathcore"]},
            {"name": "Progressive Deathcore", "family": "Hardcore",
             "aliases": ["prog deathcore"]},
            {"name": "Hardcore", "family": "Hardcore", "aliases": ["hardcore"]},
            {"name": "Hardcore", "family": "Punk", "aliases": ["punk hardcore"]},
        ],
    }
    Path(config["normalize"]["styles_file"]).write_text(
        R.dump_vocabulary(vocabulary), encoding="utf-8")
    errors, _warnings = validate(vocabulary)
    report.check("the same name in different families is valid",
                 not errors, repr(errors))

    connection = S.connect(Path(config["db_path"]))
    try:
        insert_track(connection, rel_path="Band/Track.mp3", name="Track",
                     artist="Band", album="Album", track_no=1,
                     raw_genre="Progressive Deathcore")
        build(connection, vocabulary)
        connection.commit()
        death = S.find_style(connection, "Death Metal", "Metal")
        prog = S.find_style(connection, "Progressive Deathcore", "Hardcore")
        hard = S.find_style(connection, "Hardcore", "Hardcore")
        punk = S.find_style(connection, "Hardcore", "Punk")
        report.check("step3 built every duplicate row",
                     all(row is not None for row in (death, prog, hard, punk)))
        death_id = death["id"] if death else ""
        prog_id = prog["id"] if prog else ""
        hard_id = hard["id"] if hard else ""
        punk_id = punk["id"] if punk else ""
        ids = {death_id, prog_id, hard_id, punk_id}
        report.check("same-named styles get distinct ids", len(ids) == 4, repr(ids))

        normalizer = R.Normalizer(connection, config)
        report.check("an exact new name beats the old alias",
                     normalizer.keys.get(normalize_raw("progressive deathcore"))
                     == prog_id)
        report.check("a bare duplicate name keeps the first entry as default",
                     normalizer.keys.get(normalize_raw("hardcore")) == hard_id)
        report.check("a qualified reference resolves to the exact style",
                     normalizer.resolve_name("Punk::Hardcore") == punk_id)
        report.check("an old bare override still resolves",
                     normalizer.resolve_name("Hardcore") == hard_id)

        Path(config["normalize"]["overrides_file"]).write_text(json.dumps({
            "tracks": {"Band/Track.mp3": {
                "styles": ["Punk::Hardcore"], "exclusive": True}}}, indent=2),
            encoding="utf-8")
        R.apply_overrides(normalizer, connection, config, dry_run=False)
        labels = {row["style_id"] for row in connection.execute(
            "SELECT style_id FROM track_styles WHERE rel_path=?",
            ("Band/Track.mp3",))}
        report.check("a qualified override applies the right duplicate",
                     labels == {punk_id}, repr(labels))
    finally:
        connection.close()

    catalog = R.ReviewCatalog(config)
    try:
        styles = catalog.styles()
        by_id = {row["id"]: row for row in styles}
        report.check("the review catalog keeps both duplicates",
                     {"Hardcore", "Death Metal"} <= {row["name"] for row in styles})
        report.check("the review catalog qualifies a duplicated name",
                     catalog.style_reference(by_id[punk_id]) == "Punk::Hardcore",
                     catalog.style_reference(by_id[punk_id]))
        report.check("the review catalog keeps a unique name short",
                     catalog.style_reference(by_id[prog_id]) == "Progressive Deathcore")
    finally:
        catalog.close()

    player_catalog = Catalog(config["db_path"])
    try:
        ids_all, _families = player_catalog.resolve(["Hardcore"])
        report.check("the player's bare name selects every variant",
                     set(ids_all) == {hard_id, punk_id}, repr(ids_all))
        ids_one, _families = player_catalog.resolve(["Punk::Hardcore"])
        report.check("the player resolves a qualified reference",
                     ids_one == [punk_id], repr(ids_one))
        _ids, families = player_catalog.resolve(["family:Punk"])
        report.check("the family selector still works",
                     families == ["Punk"], repr(families))
    finally:
        player_catalog.close()


def test_preview_player(report: Reporter, _config: Dict[str, Any]) -> None:
    print("\nPreviewPlayer")
    player = R.PreviewPlayer()
    try:
        report.check("the default volume is 80 before mpv starts",
                     player.volume == 80, str(player.volume))
        player.set_volume(35.6)
        report.check("a volume set before playback is remembered",
                     player.volume == 36, str(player.volume))
        player.set_volume(-5)
        report.check("the volume is clamped at 0", player.volume == 0,
                     str(player.volume))
        player.set_volume(150)
        report.check("the volume is clamped at 100", player.volume == 100,
                     str(player.volume))
    finally:
        player.close()


def test_gui(report: Reporter, config: Dict[str, Any]) -> None:
    print("\nGUI smoke test")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        report.check("GUI smoke test skipped (no display)", True, str(exc))
        return
    root.withdraw()
    app: Optional[R.ReviewApp] = None
    preview = StubPreview()
    try:
        app = R.ReviewApp(root, config=config, db_path=config["db_path"],
                          overrides_path=config["normalize"]["overrides_file"],
                          preview=preview)
        root.update()
        app.reload_queue()
        report.check("the GUI loads the unlabelled queue",
                     app.current is not None
                     and app.current["rel_path"] == "Band/Album/01 - Corruption.mp3",
                     str((app.current or {}).get("rel_path")))

        report.check("the volume slider starts at the player volume",
                     abs(app.volume_scale.get() - 80.0) < 0.01
                     and app.volume_label.cget("text") == "80%",
                     f"{app.volume_scale.get()} / {app.volume_label.cget('text')}")
        app._volume_changed(35)
        report.check("the volume slider controls the player",
                     preview.volume == 35 and preview.volumes[-1] == 35
                     and app.volume_label.cget("text") == "35%",
                     f"{preview.volume} / {app.volume_label.cget('text')}")

        audio = str(Path(config["music_root"]) / "Band/Album/01 - Corruption.mp3")
        app.toggle_playback()
        report.check("the play button starts the full track",
                     preview.played == [audio]
                     and app.play_button.cget("text") == "Pause",
                     f"{preview.played} / {app.play_button.cget('text')}")
        preview.position, preview.duration = 60.0, 180.0
        app._update_playback()
        report.check("the progress bar follows the position",
                     abs(app.progress_var.get() - 1 / 3) < 0.01,
                     str(app.progress_var.get()))
        report.check("the time labels show position and duration",
                     app.position_label.cget("text") == "1:00"
                     and app.duration_label.cget("text") == "3:00",
                     f"{app.position_label.cget('text')} / "
                     f"{app.duration_label.cget('text')}")
        preview.duration = 0.0
        app.current["duration_ms"] = 240_000
        app._update_playback()
        report.check("the catalog duration is the fallback for a stream",
                     app.duration_label.cget("text") == "4:00",
                     app.duration_label.cget("text"))
        preview.duration = 180.0
        app.toggle_playback()
        report.check("the same button pauses the track",
                     preview.paused and app.play_button.cget("text") == "▶ Play",
                     f"{preview.paused} / {app.play_button.cget('text')}")
        app._seek_fraction(0.5)
        report.check("clicking the bar seeks",
                     preview.seeks[-1] == 90.0
                     and abs(app.progress_var.get() - 0.5) < 0.01,
                     f"{preview.seeks} / {app.progress_var.get()}")
        preview.ended = True
        preview.paused = False
        app._update_playback()
        report.check("a finished track shows the play button again",
                     app.play_button.cget("text") == "▶ Play"
                     and app.preview_status.cget("text") == "finished",
                     f"{app.play_button.cget('text')} / "
                     f"{app.preview_status.cget('text')}")
        app.toggle_playback()
        report.check("play after the end reloads the track",
                     len(preview.played) == 2, str(preview.played))
        app.stop_playback()
        report.check("stopping resets the progress bar",
                     preview.source == ""
                     and abs(app.progress_var.get()) < 1e-9
                     and app.play_button.cget("text") == "▶ Play",
                     f"{preview.source} / {app.progress_var.get()}")

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

        added, added_id, moved = app.add_style(
            "Melodic Death Metal", "Metal",
            parent="Death Metal", aliases=["melodeath", "mdm"])
        row = app.catalog.connection.execute(
            "SELECT name, family, parent, aliases, source FROM styles WHERE id=?",
            (added_id,)).fetchone()
        report.check("the GUI adds a style to the catalog",
                     row is not None and row["name"] == "Melodic Death Metal"
                     and row["family"] == "Metal" and row["parent"] == "death-metal"
                     and row["source"] == "human" and moved == [],
                     str(tuple(row)) if row else "missing")
        report.check("the new style is in the vocabulary file",
                     any(style["name"] == "Melodic Death Metal"
                         for style in app.vocabulary.styles()))
        report.check("the GUI selects the style it just added",
                     app.selected_style is not None
                     and app.selected_style["name"] == added["name"]
                     and app.selected_style["id"] == added_id,
                     str((app.selected_style or {}).get("name")))
        report.check("the style info shows the sub-style parent",
                     "sub-style of Death Metal" in app.style_info.cget("text"),
                     app.style_info.cget("text"))
        saved = json.loads(Path(config["normalize"]["styles_file"]).read_text())
        report.check("the new style is appended to styles.json",
                     saved["styles"][-1] == {"name": "Melodic Death Metal",
                                             "family": "Metal",
                                             "parent": "Death Metal",
                                             "aliases": ["melodeath", "mdm"]},
                     repr(saved["styles"][-1]))
        # The first save removed the track from the "No style" queue, so pick
        # it again from the full list before saving the new style.
        app._mode.set(R.QUEUE_LABELS["all"])
        app.reload_queue()
        index = next(position for position, candidate in enumerate(app.rows)
                     if candidate["rel_path"] == "Band/Album/01 - Corruption.mp3")
        app.queue_list.selection_clear(0, "end")
        app.queue_list.selection_set(index)
        app.queue_list.activate(index)
        app._on_queue_select()
        app._select_style_by_name("Melodic Death Metal")
        app.save("tracks")
        row = app.catalog.connection.execute(
            "SELECT primary_style, label_source FROM tracks WHERE rel_path=?",
            ("Band/Album/01 - Corruption.mp3",)).fetchone()
        report.check("the newly added style can be saved to a track",
                     row is not None and tuple(row) == (added_id, "human"),
                     str(tuple(row)) if row else "missing")

        _punk_entry, punk_id, _moved = app.add_style(
            "Deathcore", "Punk", aliases=["punk deathcore"])
        app._select_style_by_id(punk_id)
        app.save("tracks")
        row = app.catalog.connection.execute(
            "SELECT primary_style, label_source FROM tracks WHERE rel_path=?",
            ("Band/Album/01 - Corruption.mp3",)).fetchone()
        report.check("a duplicate name can be saved to a track",
                     row is not None and tuple(row) == (punk_id, "human"),
                     str(tuple(row)) if row else "missing")
        saved_overrides = json.loads(
            Path(config["normalize"]["overrides_file"]).read_text())
        refs = saved_overrides["tracks"]["Band/Album/01 - Corruption.mp3"]["styles"]
        report.check("the GUI writes Family::Name for a duplicate",
                     refs == ["Punk::Deathcore"], repr(refs))

        dialog = R.NewStyleDialog(app)
        root.update()
        report.check("the new-style dialog lists the family choices",
                     len(dialog.family_box["values"]) >= 3,
                     str(dialog.family_box["values"]))
        dialog._family_var.set("Metal")
        dialog._refresh_parents()
        offered = set(dialog.parent_box["values"])
        expected = {"(none)"} | {
            str(style["name"]) for style in app.catalog.styles()
            if style.get("family") == "Metal"
            and not str(style["name"]).endswith("(unspecified)")}
        report.check("the dialog offers only the selected family as parents",
                     offered == expected, str(sorted(offered ^ expected)))
        dialog.destroy()

        # Safety guard: an empty/unmounted root must never empty the catalog.
        empty_root = Path(config["music_root"]) / "not-mounted"
        empty_root.mkdir(parents=True, exist_ok=True)
        original_root = app.catalog.music_root
        original_ask = R.messagebox.askyesno
        original_error = R.messagebox.showerror
        errors: List[str] = []
        R.messagebox.showerror = lambda title, _message, **_kwargs: errors.append(title)
        try:
            app.catalog.music_root = empty_root
            app.prune_missing_tracks()
            remaining = app.catalog.connection.execute(
                "SELECT COUNT(*) FROM tracks").fetchone()[0]
            report.check("pruning refuses when no file is visible",
                         remaining == 5 and bool(errors),
                         f"{remaining} tracks, errors={errors}")
            app.catalog.music_root = original_root
            missing_path = Path(config["music_root"]) / "Other/Album/01 - Four.mp3"
            missing_path.unlink()
            R.messagebox.askyesno = lambda *_args, **_kwargs: True
            app.prune_missing_tracks()
        finally:
            app.catalog.music_root = original_root
            R.messagebox.askyesno = original_ask
            R.messagebox.showerror = original_error

        remaining = app.catalog.connection.execute(
            "SELECT COUNT(*) FROM tracks").fetchone()[0]
        labels = app.catalog.connection.execute(
            "SELECT COUNT(*) FROM track_styles WHERE rel_path=?",
            ("Other/Album/01 - Four.mp3",)).fetchone()[0]
        artist = app.catalog.connection.execute(
            "SELECT key FROM artists WHERE key='other'").fetchone()
        report.check("a missing file is removed from the catalog",
                     remaining == 4, str(remaining))
        report.check("its labels are deleted with it", labels == 0, str(labels))
        report.check("an artist with no tracks is removed from the roll-up",
                     artist is None, str(artist))
        report.check("the prune result is shown in the status",
                     "Removed 1 missing track" in app.status.cget("text"),
                     app.status.cget("text"))
    finally:
        if app is not None:
            app.close()
        else:
            root.destroy()


def main() -> int:
    report = Reporter()
    config = config_for(TMP)
    write_vocabulary(config)
    build_catalog(config)
    try:
        for label, function in (
                ("OverrideStore", test_store),
                ("ReviewCatalog", test_catalog),
                ("Apply overrides", test_apply),
                ("VocabularyStore", test_vocabulary),
                ("Duplicate style names", test_duplicate_styles),
                ("PreviewPlayer", test_preview_player),
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
