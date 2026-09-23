#!/usr/bin/env python3
"""Offline self-test for SimpleJellyMus.

Spins up a tiny fake Jellyfin server (with decoy video items) and checks the
client, the music-only filter, cover download, the preload cache, the whole
playback engine (auto-advance, next, previous, pause, volume, seek), the UI
layout with long titles, the launcher/icon wiring and the single-instance guard.
Dropped files are covered too: expanding folders and m3u/pls/xspf playlists,
reading tags and cover art, playing a drop in order, the return to random when it
ends, the "back to random" button, the Tcl list a file manager drops, and the
XDND exchange itself (the test plays the file manager and drops two files on the
window over the real X11 protocol).

    python3 selftest.py

Playback happens at volume 0 and every cache/config artefact goes to a
throwaway directory, so your real Jellyfin login and library are never used.
"""

import json
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Must run before importing the project modules: they read XDG_* at import time.
_TMP = Path(tempfile.mkdtemp(prefix="simplejellymus-selftest-"))
os.environ["XDG_CACHE_HOME"] = str(_TMP / "cache")
os.environ["XDG_CONFIG_HOME"] = str(_TMP / "config")

import dnd  # noqa: E402  (imported once the environment is ready)
import jellyfin  # noqa: E402
import localmedia  # noqa: E402
from jellyfin import AuthError, JellyfinClient, JellyfinError  # noqa: E402
from player import PlayerEngine, PlayQueue, Preloader  # noqa: E402

TOKEN = "test-token"
# Tracks are deliberately short: the whole playback flow (including the
# auto-advance) is verified in a few seconds. 4 s leaves room for the
# next/previous/pause assertions without racing the track changes.
TRACK_SECONDS = 4.0

AUDIO_ITEMS = [
    {
        "Id": f"a{index}",
        "Name": f"Test Song {index}",
        "Type": "Audio",
        "MediaType": "Audio",
        "Container": "wav",
        "Album": "Test Album",
        "AlbumId": "album-1",
        "AlbumPrimaryImageTag": "tag-1",
        "AlbumArtist": f"Artist {index % 2}",
        "Artists": [f"Artist {index % 2}"],
        "ProductionYear": 2024,
        "RunTimeTicks": int(TRACK_SECONDS * 10_000_000),
    }
    for index in range(1, 7)
]
AUDIO_IDS = {item["Id"] for item in AUDIO_ITEMS}

# Every entry below must be rejected by the music-only filter.
VIDEO_ITEMS = [
    {"Id": "v1", "Name": "Music Video", "Type": "Video", "MediaType": "Video",
     "Container": "mp4", "RunTimeTicks": int(TRACK_SECONDS * 10_000_000)},
    {"Id": "v2", "Name": "Concert Movie", "Type": "Movie", "MediaType": "Video",
     "Container": "mkv"},
    {"Id": "v3", "Name": "Audio item flagged as video", "Type": "Audio", "MediaType": "Audio",
     "VideoType": "VideoFile", "Container": "mkv"},
    {"Id": "v4", "Name": "Container without an audio stream", "Type": "Audio",
     "MediaType": "Audio", "Container": "mp4",
     "MediaSources": [{"Id": "s1", "MediaStreams": [{"Type": "Video"}]}]},
]


def build_wav(seconds: float = TRACK_SECONDS, frequency: float = 440.0) -> bytes:
    """Generate a short quiet sine WAV so the test needs no fixtures."""
    rate = 22050
    frames = bytearray()
    for index in range(int(rate * seconds)):
        frames += struct.pack("<h", int(6000 * math.sin(2 * math.pi * frequency * index / rate)))
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        path = Path(handle.name)
    try:
        with wave.open(str(path), "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(rate)
            target.writeframes(bytes(frames))
        return path.read_bytes()
    finally:
        path.unlink(missing_ok=True)


def build_cover() -> bytes:
    """Generate a small JPEG cover."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (600, 600), (10, 126, 164))
    ImageDraw.Draw(image).ellipse((150, 150, 450, 450), fill=(21, 23, 24))
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as handle:
        path = Path(handle.name)
    try:
        image.save(path, "JPEG", quality=80)
        return path.read_bytes()
    finally:
        path.unlink(missing_ok=True)


WAV_BYTES = build_wav()
COVER_BYTES = build_cover()

class FakeJellyfin(BaseHTTPRequestHandler):
    """Just enough of the Jellyfin API for the player to work."""

    protocol_version = "HTTP/1.1"
    server_version = "FakeJellyfin/1.0"

    def log_message(self, *args):  # keep the test output clean
        pass

    def _reply(self, status: int, payload: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: int, data) -> None:
        self._reply(status, json.dumps(data).encode("utf-8"))

    def _authorised(self, query=None) -> bool:
        """Real Jellyfin accepts either the auth header or ``?api_key=``."""
        if f'Token="{TOKEN}"' in self.headers.get("X-Emby-Authorization", ""):
            return True
        return bool(query) and query.get("api_key") == [TOKEN]

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if path == "/System/Info/Public":
            return self._json(200, {"ServerName": "Selftest Jellyfin", "Version": "10.10.3"})
        if not self._authorised(query):
            return self._json(401, {"error": "unauthorised"})
        if path == "/Users/Me":
            return self._json(200, {"Id": "user-1", "Name": "tester"})
        if path.startswith("/Users/") and path.endswith("/Items"):
            items = AUDIO_ITEMS + VIDEO_ITEMS
            return self._json(200, {"Items": items, "TotalRecordCount": len(items)})
        if path.startswith("/Audio/") and path.endswith("/stream"):
            if query.get("static") != ["true"]:
                return self._json(400, {"error": "expected static=true"})
            return self._reply(200, WAV_BYTES, "audio/wav")
        if path.startswith("/Items/") and "/Images/Primary" in path:
            return self._reply(200, COVER_BYTES, "image/jpeg")
        return self._json(404, {"error": f"no route for {path}"})

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if path != "/Users/authenticatebyname":
            return self._json(404, {"error": "no route"})
        if 'Client="SimpleJellyMus"' not in self.headers.get("X-Emby-Authorization", ""):
            return self._json(400, {"error": "missing client header"})
        if body.get("Username") != "tester" or body.get("Pw") != "secret":
            return self._json(401, {"error": "bad credentials"})
        return self._json(200, {"AccessToken": TOKEN, "ServerId": "server-1",
                                "User": {"Id": "user-1", "Name": "tester"}})


def start_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeJellyfin)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def wait_for(predicate, timeout: float, interval: float = 0.1):
    """Poll *predicate* until it returns something truthy, else return None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


class Reporter:
    """Collects the check results and prints them as they happen."""

    def __init__(self) -> None:
        self.failures = []

    def check(self, label: str, condition, detail: str = "") -> bool:
        passed = bool(condition)
        if not passed:
            self.failures.append(label)
        suffix = f" ({detail})" if detail else ""
        print(f"  [{'ok' if passed else 'FAIL'}] {label}{suffix}", flush=True)
        return passed

def track_changed(engine, previous_id: str, timeout: float = 25.0):
    """Wait until the engine is playing a track other than *previous_id*."""
    def current():
        item = engine.snapshot().get("current")
        return item if item and item.get("Id") != previous_id else None
    return wait_for(current, timeout)


def test_client(report: Reporter, port: int) -> JellyfinClient:
    client = JellyfinClient(f"http://127.0.0.1:{port}")
    info = client.test_connection()
    report.check("server info is readable", info.get("ServerName") == "Selftest Jellyfin",
                 str(info.get("ServerName")))

    try:
        JellyfinClient(f"http://127.0.0.1:{port}").authenticate("tester", "wrong")
        report.check("a wrong password is rejected", False)
    except AuthError:
        report.check("a wrong password is rejected", True)

    client.authenticate("tester", "secret")
    report.check("login returns an access token", client.access_token == TOKEN)
    report.check("login returns the user id", client.user_id == "user-1")
    report.check("the stored token validates", client.validate_token().get("Name") == "tester")

    items = client.random_batch()
    ids = {item["Id"] for item in items}
    report.check("the batch contains every audio track", ids == AUDIO_IDS,
                 f"{len(ids)} of {len(AUDIO_IDS)}")
    report.check("video items never reach the queue",
                 not (ids & {item["Id"] for item in VIDEO_ITEMS}))
    for item in VIDEO_ITEMS:
        report.check(f"is_music() rejects {item['Id']} ({item['Name']})",
                     not JellyfinClient.is_music(item))
    report.check("is_music() accepts the audio tracks",
                 all(JellyfinClient.is_music(item) for item in AUDIO_ITEMS))

    cover = client.download_image(AUDIO_ITEMS[0])
    report.check("cover art is downloaded",
                 cover is not None and cover.exists() and cover.stat().st_size > 0)
    if cover is not None:
        from PIL import Image
        with Image.open(cover) as image:
            report.check("cover art decodes as an image", image.size[0] >= 300, str(image.size))
    return client


def test_preloader(report: Reporter, client: JellyfinClient) -> None:
    preloader = Preloader(client, cache_dir=_TMP / "audio-test")
    finished = threading.Event()
    result = {}

    def on_ready(item, path):
        result["item"], result["path"] = item, path
        finished.set()

    preloader.request(AUDIO_ITEMS[0], on_ready)
    report.check("the preloader finishes its download", finished.wait(timeout=20))
    path = result.get("path")
    size = path.stat().st_size if path is not None and path.exists() else 0
    report.check("the preloaded file is complete", size == len(WAV_BYTES), f"{size} bytes")
    report.check("a cached track is found again",
                 preloader.cached_path(AUDIO_ITEMS[0]) is not None)
    report.check("no .part files are left behind",
                 not list((_TMP / "audio-test").glob("*.part")))
    preloader.stop()

def test_engine(report: Reporter, client: JellyfinClient):
    engine = PlayerEngine(client, volume=0, debug=bool(os.environ.get("SELFTEST_DEBUG")))
    observed = set()
    errors = []
    preload_ready = threading.Event()
    counters = {"transitions": 0, "last": None}

    def listener(state):
        current = state.get("current")
        if current:
            observed.add(current["Id"])
            if counters["last"] is not None and current["Id"] != counters["last"]:
                counters["transitions"] += 1
            counters["last"] = current["Id"]
        if state.get("preload_ready"):
            preload_ready.set()
        if state.get("error"):
            errors.append(state["error"])

    engine.add_listener(listener)
    try:
        engine.start()
    except Exception as exc:  # e.g. mpv is not installed
        report.check(f"the player starts ({exc})", False)
        return engine

    try:
        first = wait_for(lambda: engine.snapshot().get("current"), 30)
        report.check("the engine starts playing", first is not None)
        if first is None:
            return engine
        report.check("the first track is audio", first["Id"] in AUDIO_IDS, first["Id"])
        report.check("the position advances (sound is really decoded)",
                     wait_for(lambda: engine.snapshot()["position"] > 0.2, 15) is not None)

        # Regression guard for the "UI freezes" bug: mpv's reader thread must stay
        # free to deliver replies, so (a) round trips stay fast and (b) the state
        # the UI renders never drifts far behind what mpv is playing.
        latencies = []
        lags = []
        for _sample in range(12):
            started = time.time()
            engine.mpv.get("time-pos")
            latencies.append(time.time() - started)
            reference = engine.mpv.get("time-pos")
            if isinstance(reference, (int, float)):
                lags.append(abs(float(reference) - float(engine.snapshot()["position"] or 0.0)))
            time.sleep(0.2)
        report.check("mpv keeps answering promptly (no reader-thread stalls)",
                     latencies and max(latencies) < 1.0,
                     f"worst round trip {max(latencies) * 1000:.0f} ms")
        report.check("the player state follows mpv closely",
                     lags and max(lags) < 1.5, f"worst lag {max(lags):.2f} s")

        # A command issued while a track change is in flight must return at once.
        engine.next_track()
        started = time.time()
        engine.mpv.set_volume(30)
        command_time = time.time() - started
        report.check("a command during a track change returns quickly",
                     command_time < 0.5, f"{command_time * 1000:.0f} ms")
        engine.set_volume(0)

        first_id = first["Id"]
        second = track_changed(engine, first_id, 25)
        report.check("the preloaded track starts on its own (gapless advance)",
                     second is not None,
                     f"{first_id} -> {second['Id'] if second else 'nothing'}")
        report.check("a track was preloaded before the hand-over", preload_ready.is_set())
        if second is None:
            return engine
        second_id = second["Id"]

        engine.next_track()
        third = track_changed(engine, second_id, 15)
        report.check("next plays another track", third is not None,
                     f"{second_id} -> {third['Id'] if third else 'nothing'}")

        if third is not None:
            # What "back" means is read from the engine's own history right before
            # the call: on 4-second test tracks the player can auto-advance
            # between two assertions, and that must not look like a bug here.
            expected_back = None
            with engine._lock:
                if engine._pos > 0:
                    expected_back = engine._seq[engine._pos - 1].get("Id")
            engine.previous_track()
            back = track_changed(engine, third["Id"], 15)
            report.check("previous goes back to the track we came from",
                         back is not None and back["Id"] == expected_back,
                         f"{third['Id']} -> {back['Id'] if back else 'nothing'}"
                         f" (expected {expected_back}, started from {second_id})")
            if back is not None and engine._pos > 0:
                # Regression guard: "previous" steps to the entry before the
                # current one in the history instead of appending the track and
                # jumping forward again. ``_seq``/``_pos`` are the engine's own
                # history bookkeeping.
                expected = engine._seq[engine._pos - 1]["Id"]
                engine.previous_track()
                earlier = track_changed(engine, back["Id"], 10)
                report.check("previous keeps walking back (no forward jump)",
                             earlier is not None and earlier["Id"] == expected,
                             f"{back['Id']} -> {earlier['Id'] if earlier else 'nothing'} "
                             f"(expected {expected})")

        engine.toggle_pause()
        report.check("pause is applied",
                     wait_for(lambda: engine.snapshot()["paused"], 5) is not None)
        engine.toggle_pause()
        report.check("play resumes",
                     wait_for(lambda: not engine.snapshot()["paused"], 5) is not None)

        engine.adjust_volume(25)
        report.check("the volume can be changed",
                     wait_for(lambda: engine.snapshot()["volume"] == 25, 5) is not None,
                     f"volume={engine.snapshot()['volume']}")

        seeked = False
        for _attempt in range(3):
            duration = float(engine.snapshot().get("duration") or 0.0)
            if duration <= 0:
                time.sleep(0.5)
                continue
            target = round(duration * 0.5, 2)
            engine.seek_absolute(target)
            if wait_for(lambda: abs(engine.snapshot()["position"] - target) <= 0.8, 4) is not None:
                seeked = True
                break
        report.check("seeking within the track works", seeked,
                     f"position={engine.snapshot()['position']:.2f}s")

        report.check("only audio items were ever played", observed <= AUDIO_IDS,
                     ", ".join(sorted(observed - AUDIO_IDS)) or "no video ids")
        report.check("the player kept advancing through the library",
                     counters["transitions"] >= 3,
                     f"{counters['transitions']} track changes, {len(observed)} distinct tracks")
        report.check("no playback errors were reported", not errors, "; ".join(errors[:2]))
    finally:
        engine.stop()

    process = engine.mpv._process
    report.check("mpv is shut down with the engine",
                 process is not None and process.poll() is not None)
    return engine


def _noop(*_args: Any, **_kwargs: Any) -> None:
    """A do-nothing callback for the UI tests."""


class _StubEngine:
    """A player engine good enough to build the real PlayerScreen against."""

    volume = 40

    def __init__(self, item: Any = None) -> None:
        self.style_calls: list = []
        self.drop_calls: list = []
        self.filter = {"entries": [], "mode": "any"}
        self.local = {"active": False, "remaining": 0, "total": 0}
        item = item or {"Id": "x1", "Name": "Stub", "Artists": ["A"], "Album": "B"}
        self._state = {
            "current": item, "up_next": None, "position": 1.0, "duration": 10.0,
            "paused": False, "volume": 40, "status": "Playing " + item.get("Name", ""),
            "error": "", "preload_ready": False, "server": "http://stub", "user": "stub",
        }

    def add_listener(self, callback: Any) -> None:
        self._listener = callback

    def snapshot(self) -> dict:
        state = dict(self._state)
        state["filter"] = {"entries": list(self.filter["entries"]),
                           "mode": self.filter["mode"]}
        state["local"] = dict(self.local)
        return state

    def emit_local(self, active: bool, remaining: int = 0, total: int = 0) -> None:
        """Push a state as if the engine had a dropped playlist (or lost it)."""
        self.local = {"active": active, "remaining": remaining, "total": total}
        self._listener(self.snapshot())

    def set_style_filter(self, entries: Any = None, mode: str = "any") -> None:
        self.style_calls.append((list(entries or []), mode))
        self.filter = {"entries": list(entries or []), "mode": mode}

    def play_local(self, paths: Any) -> int:
        """Record a drop the way the real engine would take it."""
        dropped = [str(path) for path in paths]
        self.drop_calls.append(("play", dropped))
        return len(dropped)

    def clear_local_queue(self) -> None:
        self.drop_calls.append(("clear", []))

    adjust_volume = seek_relative = seek_absolute = _noop
    toggle_pause = next_track = previous_track = restart_track = _noop


def widget_texts(widget: Any) -> list:
    """Every text shown inside a widget tree (used by the overlay checks)."""
    texts = []
    for child in widget.winfo_children():
        try:
            text = child.cget("text")
        except Exception:
            text = ""
        if text:
            texts.append(str(text))
        texts.extend(widget_texts(child))
    return texts


def test_ui(report: Reporter) -> None:
    """Regression guard for the hover loop that froze the window.

    Deleting/recreating canvas items from item <Enter>/<Leave> bindings makes Tk
    re-pick the current item, which fires the bindings again - an endless idle
    loop that pinned the CPU at 100% and made the whole app unresponsive. Hover
    highlighting must therefore use the canvas' own <Motion> event and only
    recolour existing items.
    """
    try:
        import tkinter as tk

        import ui
    except ImportError as exc:      # no tkinter in this interpreter
        report.check(f"tkinter is importable ({exc})", False)
        return
    try:
        root = tk.Tk()
    except tk.TclError as exc:      # headless machine
        print(f"  [skip] no display for the UI check ({exc})", flush=True)
        return

    class StubEngine:
        volume = 40

        def __init__(self, item=None):
            item = item or {"Id": "x1", "Name": "Stub", "Artists": ["A"], "Album": "B"}
            self._state = {
                "current": item, "up_next": None, "position": 1.0, "duration": 10.0,
                "paused": False, "volume": 40, "status": "Playing " + item.get("Name", ""),
                "error": "", "preload_ready": False, "server": "http://stub", "user": "stub",
            }

        def add_listener(self, callback):
            pass

        def snapshot(self):
            return dict(self._state)

        def toggle_pause(self):
            pass

        next_track = previous_track = restart_track = toggle_pause
        adjust_volume = seek_relative = seek_absolute = toggle_pause

    screen = None
    try:
        root.geometry("1280x800")
        screen = ui.PlayerScreen(root, engine=StubEngine(), client=JellyfinClient("http://127.0.0.1:1"),
                                 family=ui.pick_font_family(),
                                 on_change_account=lambda: None, on_quit=lambda: None)
        screen.pack(fill="both", expand=True)
        root.update()
        canvas = screen._transport
        report.check("no per-item Enter/Leave bindings on the transport",
                     not canvas.tag_bind("main", "<Enter>") and not canvas.tag_bind("main", "<Leave>")
                     and not canvas.tag_bind("prev", "<Enter>")
                     and not canvas.tag_bind("next", "<Leave>"))
        report.check("the transport icons are still clickable",
                     bool(canvas.tag_bind("main", "<Button-1>"))
                     and bool(canvas.tag_bind("prev", "<Button-1>")))

        counters = {"delete": 0, "create": 0}
        real_delete, real_oval = canvas.delete, canvas.create_oval
        canvas.delete = lambda *a, **k: (counters.__setitem__("delete", counters["delete"] + 1),
                                         real_delete(*a, **k))[1]
        canvas.create_oval = lambda *a, **k: (counters.__setitem__("create", counters["create"] + 1),
                                              real_oval(*a, **k))[1]
        paints = {"n": 0}
        real_paint = screen._paint_transport
        screen._paint_transport = lambda: (paints.__setitem__("n", paints["n"] + 1), real_paint())[1]
        screen._set_hovered("main")
        screen._set_hovered("main")          # repeating the same hover must be a no-op
        screen._set_hovered("prev")
        screen._set_hovered(None)
        report.check("hover repaints without rebuilding the icons",
                     counters["delete"] == 0 and counters["create"] == 0,
                     f"delete={counters['delete']} create={counters['create']}")
        report.check("only real hover changes trigger a repaint", paints["n"] == 3,
                     f"{paints['n']} repaints for 4 calls")
        callbacks = {"n": 0}
        real_hover = screen._set_hovered

        def counting(tag):
            callbacks["n"] += 1
            return real_hover(tag)

        screen._set_hovered = counting
        screen._set_hovered("main")
        for _ in range(5):
            root.update()
            time.sleep(0.05)
        report.check("a hover change cannot feed itself", callbacks["n"] == 1,
                     f"{callbacks['n']} calls for one hover")

        # ------------------------------------------------------------- layout
        # A long song/artist/album name must never move the artwork or the
        # controls: the text shrinks (down to a readable minimum) and is then
        # ellipsized inside its fixed box.
        from tkinter import font as tkfont

        screen.destroy()
        screen = None
        root.update_idletasks()

        cases = {
            "1 line": {"Id": "a", "Name": "Short Song", "Artists": ["Artist"],
                       "Album": "Album", "ProductionYear": 2024},
            "2 lines": {"Id": "b", "Name": "A Moderately Long Song Title That Wraps Onto A Second Line",
                        "Artists": ["Artist"], "Album": "Album", "ProductionYear": 2024},
            "4 lines": {"Id": "c", "Name": "A Very Long Song Title That Goes On And On And On "
                                             "(Live At The Somewhere Stadium, Remastered) " * 2,
                        "Artists": ["An Extremely Long Featured Artist Collective Name Indeed"],
                        "Album": "The Longest Album Name Imaginable With A Subtitle", "ProductionYear": 2024},
            "huge": {"Id": "d", "Name": "Song " * 40, "Artists": ["Featured Artist " * 60],
                     "Album": "Extremely Long Album Name " * 30, "ProductionYear": 2024},
        }
        geometries = ("900x620", "1024x700", "1280x800", "1920x1080")

        def font_size(label) -> int:
            return abs(int(tkfont.Font(font=label.cget("font")).cget("size")))

        def pump(seconds: float) -> None:
            end = time.time() + seconds
            while time.time() < end:
                root.update()
                time.sleep(0.02)

        for geometry in geometries:
            window_width = int(geometry.split("x")[0])
            root.geometry(geometry)
            root.update_idletasks()
            layouts = {}
            sizes = {}
            for name, item in cases.items():
                built = ui.PlayerScreen(root, engine=StubEngine(item),
                                        client=JellyfinClient("http://127.0.0.1:1"),
                                        family=ui.pick_font_family(),
                                        on_change_account=lambda: None, on_quit=lambda: None)
                built.pack(fill="both", expand=True)
                # Let the debounced re-layout fire, exactly like a real resize.
                pump(0.6)
                layouts[name] = (built._progress.winfo_rooty(), built._transport.winfo_rooty(),
                                 built._footer.winfo_rooty(), built._cover_size,
                                 built._text_block_height)
                sizes[name] = font_size(built._title)
                report.check(f"{geometry} / {name}: controls and artwork keep their place",
                             built._transport.winfo_height() == 90
                             and built._progress.winfo_height() == 46
                             and (built._transport.winfo_rooty() + built._transport.winfo_height()
                                  <= built._footer.winfo_rooty()),
                             f"transport {built._transport.winfo_height()}/90, "
                             f"progress {built._progress.winfo_height()}/46")
                needed = (built._title.winfo_reqheight() + built._artist.winfo_reqheight()
                          + built._album.winfo_reqheight())
                report.check(f"{geometry} / {name}: the song info fits its fixed box",
                             needed <= built._text_block_height,
                             f"needs {needed}px of {built._text_block_height}px")
                if name == "1 line":
                    report.check(f"{geometry}: a short title is shown complete at full size",
                                 built._title.cget("text") == item["Name"]
                                 and sizes[name] == ui.TITLE_SIZE)
                elif name == "2 lines" and window_width >= 1280:
                    report.check(f"{geometry}: a two-line title is shown complete",
                                 built._title.cget("text") == item["Name"],
                                 built._title.cget("text")[:40])
                elif name == "4 lines":
                    report.check(f"{geometry}: a long title shrinks instead of moving things",
                                 sizes[name] < ui.TITLE_SIZE, f"{sizes[name]}pt")
                elif name == "huge":
                    report.check(f"{geometry}: an extreme name is ellipsized at the minimum size",
                                 built._title.cget("text").endswith("…")
                                 and built._artist.cget("text").endswith("…")
                                 and built._album.cget("text").endswith("…"),
                                 f"title {sizes[name]}pt")
                built.destroy()
                root.update_idletasks()
            report.check(f"{geometry}: identical layout for every title length",
                         len(set(layouts.values())) == 1,
                         f"{len(set(layouts.values()))} different layouts")
            report.check(f"{geometry}: the title font stays readable "
                         f"[{ui.TITLE_MIN_SIZE}, {ui.TITLE_SIZE}] pt",
                         all(ui.TITLE_MIN_SIZE <= size <= ui.TITLE_SIZE for size in sizes.values()),
                         ", ".join(f"{name}={size}pt" for name, size in sizes.items()))
    finally:
        if screen is not None:
            screen.destroy()
        root.destroy()


def test_overlays(report: Reporter) -> None:
    """The "?" card and the overlay mechanics it shares with the style picker.

    An overlay is *placed* over the screen, never packed - that is what lets the
    help and the picker exist without moving the artwork, the progress bar, the
    transport or the footer by a single pixel.
    """
    try:
        import tkinter as tk

        import ui
    except ImportError as exc:
        report.check(f"tkinter is importable ({exc})", False)
        return
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"  [skip] no display for the overlay check ({exc})", flush=True)
        return

    def pump(seconds: float) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            root.update()
            time.sleep(0.02)

    screen = None
    try:
        root.geometry("1280x800")
        screen = ui.PlayerScreen(root, engine=_StubEngine(),
                                 client=JellyfinClient("http://127.0.0.1:1"),
                                 family=ui.pick_font_family(),
                                 on_change_account=_noop, on_quit=_noop)
        screen.pack(fill="both", expand=True)
        pump(0.6)

        # ------------------------------------------------------------ the button
        buttons = [child for child in screen._header.winfo_children()
                   if isinstance(child, tk.Button)]
        labels = [str(button.cget("text")) for button in buttons]
        report.check("the header has the \"?\" button", "?" in labels, str(labels))
        heights = {button.winfo_reqheight() for button in buttons}
        report.check("the new button is exactly as tall as its neighbours",
                     len(heights) == 1, str(heights))
        report.check("the header height is unchanged by the extra button",
                     screen._header.winfo_reqheight() == max(heights),
                     f"header {screen._header.winfo_reqheight()}px, buttons {max(heights)}px")

        # ----------------------------------------------------------- the bindings
        # Moving the keys into one table must not lose a single binding.
        legacy = ("<space>", "<Right>", "<n>", "<N>", "<Left>", "<p>", "<P>", "<Up>", "<Down>",
                  "<Button-4>", "<Button-5>", "<s>", "<S>", "<comma>", "<period>", "<f>", "<F>",
                  "<Escape>", "<Control-q>", "<q>", "<Q>")
        missing = [sequence for sequence in legacy if not root.bind(sequence)]
        report.check("every key the player had before is still bound", not missing, str(missing))
        table = ui.PlayerScreen._SHORTCUTS
        report.check("every shortcut row points at a real method",
                     all(getattr(screen, name, None) is not None for name, _s, _t in table))
        report.check("every key of the table is really bound",
                     all(root.bind(sequence) for _n, sequences, _t in table
                         for sequence in sequences))
        report.check("the ? key is in the table and opens the help",
                     any(name == "_open_help" and "<question>" in sequences
                         for name, sequences, _t in table))

        # ------------------------------------------------------------- the card
        def metrics() -> tuple:
            return (screen._cover_size, screen._progress.winfo_rooty(),
                    screen._transport.winfo_rooty(), screen._footer.winfo_rooty(),
                    screen._text_block_height, screen._header.winfo_height())

        before = metrics()
        screen._open_help()
        pump(0.2)
        card = screen._overlay
        report.check("? opens the help card", card is not None)
        if card is None:
            return
        report.check("the card is placed, not packed", bool(card.place_info()))
        width, height = card.winfo_width(), card.winfo_height()
        report.check("the card fits inside the window",
                     0 <= card.winfo_x() and 0 <= card.winfo_y()
                     and card.winfo_x() + width <= screen.winfo_width()
                     and card.winfo_y() + height <= screen.winfo_height(),
                     f"{width}x{height} at {card.winfo_x()},{card.winfo_y()}")
        report.check("opening the card moves nothing in the layout",
                     metrics() == before, f"{before} -> {metrics()}")
        shown = widget_texts(card)
        rows = screen._help_rows()
        report.check("the help shows every shortcut of the table",
                     all(keys in shown for keys, _text in rows),
                     str([keys for keys, _t in rows if keys not in shown]))
        report.check("the help mentions ? itself and Esc", "?" in shown and "Esc" in shown)

        # -------------------------------------------------------------- Esc owns it
        # The spy *replaces* the real toggle: whether the window manager honours
        # fullscreen is not something a test should depend on - only that Esc was
        # routed to the right action.
        calls = {"fullscreen": 0}
        real_toggle = screen._toggle_fullscreen

        def counting_toggle() -> None:
            calls["fullscreen"] += 1

        screen._toggle_fullscreen = counting_toggle
        report.check("Esc is consumed by the open card", screen._on_escape() == "break")
        pump(0.2)
        report.check("Esc closed the card", screen._overlay is None)
        report.check("Esc did not also switch fullscreen", calls["fullscreen"] == 0)
        screen._on_escape()                    # nothing open: the old behaviour
        report.check("Esc still switches fullscreen when nothing is open",
                     calls["fullscreen"] == 1)
        screen._toggle_fullscreen = real_toggle
        report.check("the window never moved during the Esc checks", metrics() == before,
                     f"{before} -> {metrics()}")

        # A card owns the keyboard: the global shortcuts must not fire as well.
        class FakeEvent:
            def __init__(self, widget: Any) -> None:
                self.widget = widget

        seen = {"n": 0}
        wrapped = screen._wrap(lambda: seen.__setitem__("n", seen["n"] + 1))
        screen._open_help()
        pump(0.2)
        wrapped(FakeEvent(screen))
        report.check("an open card swallows the global shortcuts", seen["n"] == 0)
        screen._close_overlay()
        pump(0.2)
        wrapped(FakeEvent(screen))
        report.check("closing the card gives the shortcuts back", seen["n"] == 1)
        entry = tk.Entry(screen)
        seen["n"] = 0
        wrapped(FakeEvent(entry))
        report.check("a focused text field still swallows the shortcuts", seen["n"] == 0)
        entry.destroy()

        # ------------------------------------------------------- cards do not pile up
        children = len(screen.winfo_children())
        for _ in range(3):
            screen._open_help()
            pump(0.15)
            screen._close_overlay()
            pump(0.15)
        report.check("cards do not pile up",
                     len(screen.winfo_children()) == children,
                     f"{len(screen.winfo_children())} children vs {children}")
        report.check("the layout is untouched after opening and closing cards",
                     metrics() == before, f"{before} -> {metrics()}")
    finally:
        if screen is not None:
            screen.destroy()
        root.destroy()


def test_style_panel(report: Reporter) -> None:
    """The "play by style" panel: search, families, modes, apply and cancel."""
    try:
        import tkinter as tk

        import ui
    except ImportError as exc:
        report.check(f"tkinter is importable ({exc})", False)
        return
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"  [skip] no display for the style panel check ({exc})", flush=True)
        return

    def pump(seconds: float) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            root.update()
            time.sleep(0.02)

    def picker_in(widget):
        for child in widget.winfo_children():
            if isinstance(child, ui.StylePicker):
                return child
            found = picker_in(child)
            if found is not None:
                return found
        return None

    def row_for(picker, name: str) -> int:
        for index in range(picker._list.size()):
            if picker._list.get(index).strip().startswith(name + " "):
                return index
        return -1

    screen = None
    try:
        catalog = build_fake_catalog(_TMP / "panel-catalog.sqlite")
        catalog_module = __import__("catalog")
        cat = catalog_module.Catalog(catalog)
        root.geometry("1280x800")
        engine = _StubEngine()
        saved = []
        screen = ui.PlayerScreen(root, engine=engine, client=JellyfinClient("http://127.0.0.1:1"),
                                 family=ui.pick_font_family(), on_change_account=_noop,
                                 on_quit=_noop, catalog=cat,
                                 on_style_change=lambda entries, mode: saved.append((entries, mode)))
        screen.pack(fill="both", expand=True)
        pump(1.0)                     # let the debounced first layout settle

        def metrics() -> tuple:
            return (screen._cover_size, screen._progress.winfo_rooty(),
                    screen._transport.winfo_rooty(), screen._footer.winfo_rooty(),
                    screen._text_block_height)

        before = metrics()
        report.check("G is in the table and opens the panel",
                     any(name == "_open_style_picker" and "<g>" in sequences
                         for name, sequences, _t in ui.PlayerScreen._SHORTCUTS))
        screen._open_style_picker()
        pump(0.3)
        picker = picker_in(screen._overlay) if screen._overlay is not None else None
        report.check("G opens the style panel", picker is not None)
        if picker is None:
            return
        report.check("the panel lists every style", picker._list.size() == 3,
                     f"{picker._list.size()} rows")
        report.check("the families are offered as chips", len(picker._chip_buttons) == 2,
                     str(sorted(picker._chip_buttons)))
        report.check("the panel moves nothing in the layout", metrics() == before,
                     f"{before} -> {metrics()}")
        report.check("with nothing selected it says there is no filter",
                     "No filter" in picker._summary.cget("text"),
                     picker._summary.cget("text"))
        report.check("the Play button is available without a filter",
                     str(picker._play.cget("state")) == "normal")

        # ------------------------------------------------------------- searching
        picker._query.set("alph")
        picker._reload()
        pump(0.1)
        report.check("typing narrows the list",
                     picker._list.size() == 1
                     and picker._list.get(0).strip().startswith("Alpha"),
                     f"{picker._list.size()} rows: "
                     f"{[picker._list.get(i) for i in range(picker._list.size())]}")
        report.check("the detail line describes the highlighted style",
                     "Alpha" in picker._details.cget("text")
                     and "3 tracks" in picker._details.cget("text"),
                     picker._details.cget("text"))

        # ----------------------------------------------------------- selecting
        picker._toggle_row(row_for(picker, "Alpha"))
        pump(0.1)
        report.check("clicking a style selects it", picker.entries() == ["Alpha"],
                     str(picker.entries()))
        report.check("the count line follows the selection",
                     "3 track" in picker._summary.cget("text"), picker._summary.cget("text"))
        report.check("the selected row is highlighted",
                     picker._list.itemcget(0, "background") == picker.ROW_SELECTED_CURSOR,
                     picker._list.itemcget(0, "background"))

        picker._toggle_family("Alpha Family")
        pump(0.1)
        report.check("a family chip adds the whole family",
                     picker.entries() == ["family:Alpha Family", "Alpha"],
                     str(picker.entries()))
        report.check("the chip shows as selected",
                     picker._chip_buttons["Alpha Family"].cget("bg") == ui.ACCENT)

        # ------------------------------------------------------------- the modes
        picker._set_mode("all")
        pump(0.1)
        report.check("'all of' needs every style of the selection",
                     "1 track" in picker._summary.cget("text"),
                     picker._summary.cget("text"))
        picker._set_mode("not")
        pump(0.1)
        report.check("'not' counts the rest of the library",
                     "3 track" in picker._summary.cget("text"), picker._summary.cget("text"))
        picker._set_mode("any")
        pump(0.1)
        report.check("the active mode is the highlighted button",
                     picker._mode_buttons["any"].cget("bg") == ui.ACCENT
                     and picker._mode_buttons["not"].cget("bg") == ui.CARD_LIGHT)

        # ---------------------------------------------------------- applying it
        # Deterministic state (not dependent on what the checks above selected):
        # every track has Alpha or Beta, so "not (both families)" matches nothing.
        picker._clear()
        picker._toggle_family("Alpha Family")
        picker._toggle_family("Beta Family")
        picker._set_mode("not")
        pump(0.1)
        report.check("a combination that matches nothing is shown as such",
                     "Nothing matches" in picker._summary.cget("text")
                     and str(picker._play.cget("state")) == "disabled",
                     f"{picker._summary.cget('text')} / {picker._play.cget('state')}")
        picker._apply()
        pump(0.1)
        report.check("such a combination is refused, with the panel still open",
                     screen._overlay is not None
                     and "Nothing matches" in picker._message.cget("text"),
                     picker._message.cget("text"))
        report.check("and the engine was never asked", not engine.style_calls)

        picker._set_mode("any")
        picker._clear()
        pump(0.1)
        report.check("Clear empties the selection", picker.entries() == [], str(picker.entries()))
        report.check("Clear goes back to any random song",
                     "No filter" in picker._summary.cget("text")
                     and "any random song" in picker._message.cget("text"),
                     picker._summary.cget("text"))
        picker._apply()
        pump(0.2)
        report.check("applying an empty filter asks for the whole library",
                     engine.style_calls[-1] == ([], "any"), str(engine.style_calls[-1]))
        report.check("the panel closes when the choice is applied",
                     screen._overlay is None)
        report.check("the app was told to remember the choice",
                     saved and saved[-1] == ([], "any"), str(saved))
        report.check("the layout is untouched after a full round trip",
                     metrics() == before, f"{before} -> {metrics()}")

        # ------------------------------------------- the Enter key (type and go)
        screen._open_style_picker()
        pump(0.3)
        picker = picker_in(screen._overlay) if screen._overlay is not None else None
        engine.style_calls.clear()
        if picker is not None:
            picker._query.set("beta")
            picker._reload()
            pump(0.1)
            picker._apply(True)             # as the Return key does
            pump(0.2)
            report.check("Enter plays the style that was typed",
                         engine.style_calls[-1] == (["Beta"], "any"), str(engine.style_calls))
            report.check("the panel closes after Enter", screen._overlay is None)

        # ------------------------------------------------------ reopening it
        engine.filter = {"entries": ["Beta"], "mode": "any"}
        screen._open_style_picker()
        pump(0.3)
        picker = picker_in(screen._overlay) if screen._overlay is not None else None
        report.check("reopening shows the filter that is playing",
                     picker is not None and picker.entries() == ["Beta"]
                     and picker._mode == "any", str(picker.entries() if picker else None))
        if picker is not None:
            row = row_for(picker, "Beta")
            report.check("its row is marked as selected",
                         row in picker._list.curselection(), str(picker._list.curselection()))
            engine.style_calls.clear()
            picker._close()
            pump(0.2)
            report.check("Cancel leaves the filter alone",
                         screen._overlay is None and not engine.style_calls)
        report.check("the layout survived the panel", metrics() == before,
                     f"{before} -> {metrics()}")

        # --------------------------------------------------- without a catalog
        screen.catalog = None
        screen._open_style_picker()
        pump(0.2)
        texts = widget_texts(screen._overlay)
        report.check("without a catalog the panel explains what to do",
                     picker_in(screen._overlay) is None
                     and any("prepare_styles.sh" in text for text in texts),
                     str([text for text in texts if "prepare" in text]))
    finally:
        if screen is not None:
            screen.destroy()
        root.destroy()


def test_style_config(report: Reporter) -> None:
    """The style filter is remembered across restarts (config round trip)."""
    import main

    report.check("a saved filter is read back",
                 main.saved_style_filter({"style_filter": {"entries": ["Beta"], "mode": "not"}})
                 == (["Beta"], "not"))
    report.check("a missing or damaged filter falls back to no filter",
                 main.saved_style_filter({}) == ([], "any")
                 and main.saved_style_filter({"style_filter": "nonsense"}) == ([], "any")
                 and main.saved_style_filter({"style_filter": {"entries": [], "mode": "junk"}})
                 == ([], "any"))
    app = type("FakeApp", (), {})()
    app.config = dict(jellyfin.load_config() or {})
    app.style_filter, app.style_mode = [], "any"
    main.Application._save_style_filter(app, ["family:Metal", "Ska Punk"], "all")
    stored = jellyfin.load_config() or {}
    report.check("the player's choice is written to the config file",
                 stored.get("style_filter") == {"entries": ["family:Metal", "Ska Punk"],
                                                "mode": "all"},
                 str(stored.get("style_filter")))
    report.check("and comes back as the same filter",
                 main.saved_style_filter(stored) == (["family:Metal", "Ska Punk"], "all"))
    report.check("the window settings are not lost by saving the filter",
                 stored.get("window_mode") in (None, "windowed", "fullscreen"))


def test_launcher(report: Reporter) -> None:
    """The icon, the installer and the launcher must agree on one file and name.

    Renaming the asset or the app id is easy to do in one place and forget in the
    others; the desktop would then just show a generic icon without complaining.
    """
    import main

    project = Path(__file__).resolve().parent
    icon = main.ICON_FILE
    try:
        from PIL import Image

        with Image.open(icon) as image:
            width, height = image.size
            is_png = image.format == "PNG"
    except OSError as exc:
        report.check(f"the application icon file is readable ({exc})", False)
        return
    report.check("the application icon is a PNG", is_png, f"assets/{icon.name}")
    report.check("it is square and big enough for a menu icon",
                 width == height and width >= 256, f"{width}x{height}")

    installer = (project / "install.sh").read_text(encoding="utf-8")
    report.check("install.sh installs the icon main.py shows in the window",
                 f"assets/{icon.name}" in installer, f"assets/{icon.name}")
    app_id = re.search(r'^APP_ID="([^"]+)"', installer, re.M)
    launcher = (project / "simplejellymus.desktop").read_text(encoding="utf-8")
    icon_name = re.search(r"^Icon=(.+)$", launcher, re.M)
    report.check("the launcher asks for the icon the installer creates",
                 bool(app_id and icon_name) and icon_name.group(1).strip() == app_id.group(1),
                 f"Icon={icon_name.group(1) if icon_name else '?'} vs "
                 f"APP_ID={app_id.group(1) if app_id else '?'}")
    wm_class = re.search(r"^StartupWMClass=(.+)$", launcher, re.M)
    report.check("the launcher matches the window class the app sets",
                 bool(wm_class) and wm_class.group(1).strip() == main.WINDOW_CLASS,
                 f"StartupWMClass={wm_class.group(1) if wm_class else '?'} vs {main.WINDOW_CLASS}")


def test_instance(report: Reporter) -> None:
    """The single-instance guard: a second launch only focuses the first copy."""
    import socket as socket_module
    import stat
    import instance

    runtime = _TMP / "runtime"
    runtime.mkdir(exist_ok=True)
    saved_runtime = os.environ.get("XDG_RUNTIME_DIR")

    def restore_runtime() -> None:
        if saved_runtime is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = saved_runtime

    try:
        os.environ["XDG_RUNTIME_DIR"] = str(runtime)
        report.check("the lock socket lives in XDG_RUNTIME_DIR",
                     instance.socket_path() == runtime / instance.SOCKET_NAME,
                     str(instance.socket_path()))
        os.environ["XDG_RUNTIME_DIR"] = str(_TMP / "not-created")
        report.check("it falls back to the temp dir when XDG_RUNTIME_DIR is missing",
                     instance.socket_path().parent == Path(tempfile.gettempdir()),
                     str(instance.socket_path()))
    finally:
        restore_runtime()

    path = runtime / "guard.sock"
    first = instance.SingleInstance(path=path)
    report.check("the first copy becomes the single instance", first.acquire() is True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0
    report.check("the socket is private to this user", mode == 0o600, oct(mode))

    focused = []
    first.listen(lambda: focused.append(time.time()))

    report.check("a second copy does not start another instance",
                 instance.SingleInstance(path=path).acquire() is False)
    report.check("... instead it asks the running window to come to the front",
                 wait_for(lambda: len(focused) == 1, 3.0) is not None,
                 f"{len(focused)} focus calls")

    for _ in range(2):
        instance.SingleInstance(path=path).acquire()
    report.check("every later copy focuses the same window again",
                 wait_for(lambda: len(focused) >= 3, 3.0) is not None,
                 f"{len(focused)} focus calls")

    # A crashed copy leaves the socket file behind: it must not block a restart.
    stale_path = runtime / "stale.sock"
    stale = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    stale.bind(str(stale_path))
    stale.close()
    takeover = instance.SingleInstance(path=stale_path)
    report.check("a socket left over from a crash is taken over",
                 stale_path.exists() and takeover.acquire() is True)

    first.close()
    report.check("closing removes the socket file", not path.exists())
    reopened = instance.SingleInstance(path=path)
    report.check("after a clean exit the next copy starts normally",
                 reopened.acquire() is True)
    reopened.close()
    takeover.close()


def build_fake_catalog(path: Path) -> Path:
    """A tiny catalog in the shape the player reads.

    a1/a3/a5 -> Alpha (+ a5 also Gamma), a2/a4/a6 -> Beta, so three cases are
    covered at once: a plain style, a family (Alpha + Gamma) and "all of".
    """
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(
            "CREATE TABLE tracks (id TEXT PRIMARY KEY, rel_path TEXT UNIQUE, name TEXT,"
            " album TEXT, album_id TEXT, album_artist TEXT, artists TEXT, year INTEGER,"
            " duration_ms INTEGER, container TEXT, image_tag TEXT, album_image_tag TEXT,"
            " raw_genre TEXT, primary_style TEXT, family TEXT, label_source TEXT,"
            " confidence REAL);"
            "CREATE TABLE styles (id TEXT PRIMARY KEY, name TEXT, family TEXT, parent TEXT,"
            " aliases TEXT, tracks INTEGER, artists INTEGER);"
            "CREATE TABLE track_styles (rel_path TEXT, style_id TEXT, weight REAL,"
            " source TEXT, confidence REAL, evidence TEXT);")
        connection.executemany(
            "INSERT INTO styles VALUES (?,?,?,?,?,?,?)",
            [("alpha", "Alpha", "Alpha Family", None, json.dumps(["alpha alias"]), 3, 3),
             ("beta", "Beta", "Beta Family", None, None, 3, 3),
             ("gamma", "Gamma", "Alpha Family", None, None, 1, 1)])
        for index, item in enumerate(AUDIO_ITEMS, start=1):
            style = "alpha" if index % 2 else "beta"
            connection.execute(
                "INSERT INTO tracks (id, rel_path, name, album, album_id, album_artist,"
                " artists, year, duration_ms, container, image_tag, album_image_tag,"
                " raw_genre, primary_style, family, label_source, confidence)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item["Id"], f"test/{item['Id']}.wav", item["Name"], item["Album"],
                 item["AlbumId"], item["AlbumArtist"], ", ".join(item["Artists"]), 2024,
                 int(item["RunTimeTicks"] / 10_000), item["Container"], "tag-1", "tag-1",
                 "Alpha" if style == "alpha" else "Beta", style,
                 "Alpha Family" if style == "alpha" else "Beta Family", "tag", 0.95))
            connection.execute("INSERT INTO track_styles VALUES (?,?,?,?,?,?)",
                               (f"test/{item['Id']}.wav", style, 1.0, "tag", 0.95, "test"))
            if item["Id"] == "a5":
                connection.execute("INSERT INTO track_styles VALUES (?,?,?,?,?,?)",
                                   (f"test/{item['Id']}.wav", "gamma", 0.7, "rules", 0.9, "test"))
                connection.execute("INSERT INTO track_styles VALUES (?,?,?,?,?,?)",
                                   (f"test/{item['Id']}.wav", "beta", 0.5, "llm", 0.8, "test"))
        connection.commit()
    finally:
        connection.close()
    return path


def test_catalog(report: Reporter, path: Path):
    """The read-only catalog layer the player talks to."""
    import catalog as catalog_module

    cat = catalog_module.Catalog(path)
    report.check("the catalog opens read-only", cat.available(), str(path))
    stats = cat.stats()
    report.check("stats count the tracks", stats["tracks"] == len(AUDIO_ITEMS), str(stats))
    report.check("styles come with their counts",
                 any(style["name"] == "Alpha" and style["tracks"] == 3 for style in cat.styles()))
    report.check("search finds a style by name", cat.search("Alpha")[0]["name"] == "Alpha")
    report.check("search finds a style by alias", cat.search("alpha alias")[0]["name"] == "Alpha")
    ids, families = cat.resolve(["Alpha"])
    report.check("a style name resolves to its id", ids == ["alpha"], str(ids))
    ids_f, fam = cat.resolve(["family:Alpha Family"])
    report.check("a family stays a family", fam == ["Alpha Family"] and not ids_f, str(fam))
    report.check("count() agrees with the materialised count", cat.count(["alpha"], [], "any") == 3)
    batch = cat.random_batch(["alpha"], [], "any", 10)
    report.check("only matching tracks come back",
                 {item["Id"] for item in batch} <= {"a1", "a3", "a5"},
                 str(sorted(item["Id"] for item in batch)))
    report.check("catalog items look like Jellyfin items",
                 all(item.get("Id") and item.get("Name") and item.get("RunTimeTicks")
                     for item in batch))
    report.check("an item carries its own styles", bool(batch and batch[0].get("_styles")))
    family_batch = cat.random_batch([], ["Alpha Family"], "any", 10)
    report.check("a family selection includes all its styles",
                 {item["Id"] for item in family_batch} <= {"a1", "a3", "a5"}
                 and len(family_batch) == 3,
                 str(sorted(item["Id"] for item in family_batch)))
    both = cat.resolve(["Alpha", "Gamma"])[0]
    report.check("'all of' needs every style", cat.count(both, [], "all") == 1,
                 str(cat.count(both, [], "all")))
    report.check("'not' excludes the selection", cat.count(["alpha"], [], "not") == 3)
    report.check("matches() agrees for a member", cat.matches({"Id": "a1"}, ["alpha"], [], "any"))
    report.check("matches() agrees for a non-member",
                 not cat.matches({"Id": "a2"}, ["alpha"], [], "any"))
    info = cat.track_styles("a5")
    report.check("track_styles returns the tag and every style",
                 info.get("raw_genre") == "Alpha" and len(info.get("styles") or []) == 3,
                 str(info.get("styles")))
    report.check("an unknown style is refused", _refuses(cat, "not a style at all"))
    return cat


def _refuses(cat, name) -> bool:
    try:
        cat.resolve([name])
    except JellyfinError:
        return True
    return False


def _refuses_filter(engine) -> bool:
    """Nothing matches Alpha+Beta+'not' - the filter must refuse to change."""
    try:
        engine.set_style_filter(["Alpha", "Beta"], "not")
    except JellyfinError:
        return True
    return False


def test_style_filter(report: Reporter, client: JellyfinClient, cat) -> None:
    """The seam between the queue/engine and the catalog."""
    queue = PlayQueue(client, source=lambda limit: cat.random_batch(["alpha"], [], "any", limit))
    report.check("the queue can be fed by the catalog", queue.ensure_batch(timeout=5))
    picked = {queue.pick()["Id"] for _ in range(3)}
    report.check("the queue keeps to the selection", picked <= {"a1", "a3", "a5"}, str(picked))

    engine = PlayerEngine(client, volume=0, catalog=cat, style_filter=["Alpha"])
    try:
        report.check("the engine reports the filter",
                     engine.snapshot()["filter"] == {"entries": ["Alpha"], "mode": "any"},
                     str(engine.snapshot()["filter"]))
        report.check("the engine uses the catalog as its source",
                     engine.queue._source != client.random_batch)
        engine.set_style_filter([])
        report.check("clearing the filter restores the classic source",
                     engine.queue._source == client.random_batch)
        report.check("the snapshot shows no filter",
                     engine.snapshot()["filter"]["entries"] == [])
        engine.set_style_filter(["Beta"])
        report.check("a track outside the filter is detected",
                     not engine._matches_filter({"Id": "a1"}))
        report.check("a track inside the filter is detected",
                     engine._matches_filter({"Id": "a2"}))
        report.check("a selection with no tracks is refused", _refuses_filter(engine))
        report.check("the refused selection did not change the filter",
                     engine.snapshot()["filter"]["entries"] == ["Beta"])
        # a1 is Alpha, a2 is Beta: with the Alpha filter the queued a2 is outside.
        engine.set_style_filter(["Alpha"])
        engine._seq = [{"Id": "a1"}, {"Id": "a2"}]
        engine._pos = 0
        item, _sequential = engine._advance_target()
        report.check("Next skips a queued track outside the filter",
                     item is None or item.get("Id") != "a2", str(item))
        engine.set_style_filter([])
        engine._seq = [{"Id": "a1"}, {"Id": "a2"}]
        engine._pos = 0
        item, sequential = engine._advance_target()
        report.check("without a filter the walking order is untouched",
                     bool(item) and item["Id"] == "a2" and sequential, str(item))
    finally:
        engine.stop()


# --------------------------------------------------------------------------- #
# dropped files, folders and playlists
# --------------------------------------------------------------------------- #

LOCAL_ROOT = _TMP / "local"
LOCAL_ALBUM = LOCAL_ROOT / "Test Artist - Test Album"
LOCAL_LOOSE = LOCAL_ROOT / "Loose"
LOCAL_TRACKS = {
    "first": LOCAL_ALBUM / "01 - First Song.wav",
    "second": LOCAL_ALBUM / "02 - Second Song.wav",
    "third": LOCAL_ALBUM / "03 - Third Song.wav",
    "loose": LOCAL_LOOSE / "Loose Track.wav",
}


def build_stereo_wav(path: Path, seconds: float = 3.0, rate: int = 44100) -> Path:
    """A CD-format stereo WAV (a *different* format than the library tracks).

    Used to check that the sound card follows the file that is playing instead of
    being stuck on the format of whatever played before it.
    """
    frames = bytearray()
    for index in range(int(rate * seconds)):
        sample = struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * index / rate)))
        frames.extend(sample)
        frames.extend(sample)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))
    return path


def build_local_fixtures() -> None:
    """A tiny music folder: an album, a loose track, a video decoy, a cover."""
    LOCAL_ALBUM.mkdir(parents=True, exist_ok=True)
    LOCAL_LOOSE.mkdir(parents=True, exist_ok=True)
    for path in LOCAL_TRACKS.values():
        if not path.exists():
            path.write_bytes(build_wav(1.0))
    (LOCAL_ALBUM / "cover.jpg").write_bytes(COVER_BYTES)
    (LOCAL_ALBUM / "notes.txt").write_text("not music\n", encoding="utf-8")
    # A video file must never reach the player, whatever it is called.
    decoy = LOCAL_LOOSE / "concert.mp4"
    if not decoy.exists():
        decoy.write_bytes(build_wav(0.5))


def tcl_drop(paths: Any) -> str:
    """A drop as tkdnd hands it over: one Tcl list with braced words.

    ``tkinterdnd2`` passes ``%D`` through Tcl's ``list``, so the callback gets a
    single string in which every path that needs it is braced - this builds the
    same thing, so the tests exercise the real code path.
    """
    return " ".join("{" + str(path) + "}" for path in paths)


def _subsequence(needle: list, haystack: list) -> bool:
    """True when *needle* appears inside *haystack*, in order."""
    index = 0
    for entry in haystack:
        if index < len(needle) and entry == needle[index]:
            index += 1
    return index == len(needle)


def test_local_media(report: Reporter) -> None:
    """What a dropped file, folder or playlist turns into (no display needed)."""
    build_local_fixtures()

    # -------------------------------------------------------------- expanding
    folder = localmedia.expand_paths([LOCAL_ALBUM])
    report.check("a dropped folder plays its audio files, sorted",
                 [path.name for path in folder] == ["01 - First Song.wav",
                                                    "02 - Second Song.wav",
                                                    "03 - Third Song.wav"],
                 str([path.name for path in folder]))
    loose = localmedia.expand_paths([LOCAL_LOOSE])
    report.check("a folder drop leaves videos and other files alone",
                 [path.name for path in loose] == ["Loose Track.wav"],
                 str([path.name for path in loose]))
    report.check("a single dropped file plays as it is",
                 localmedia.expand_paths([LOCAL_TRACKS["loose"]]) == [LOCAL_TRACKS["loose"]])
    report.check("a dropped folder is walked into its sub-folders",
                 set(localmedia.expand_paths([LOCAL_ROOT])) == set(LOCAL_TRACKS.values()),
                 str(sorted(path.name for path in localmedia.expand_paths([LOCAL_ROOT]))))
    report.check("dropping the same file twice plays it once",
                 len(localmedia.expand_paths([LOCAL_ALBUM, LOCAL_TRACKS["first"],
                                              LOCAL_ALBUM])) == 3)
    report.check("a path that is not there is skipped",
                 localmedia.expand_paths([LOCAL_ROOT / "gone.mp3",
                                          LOCAL_ROOT / "gone"]) == [])
    report.check("dropping nothing expands to nothing",
                 localmedia.expand_paths([]) == [] and localmedia.expand_paths(None) == [])

    # -------------------------------------------------------------- playlists
    m3u = LOCAL_ROOT / "list.m3u"
    m3u.write_text("#EXTM3U\n"
                   "\n"
                   "Test Artist - Test Album/02 - Second Song.wav\n"
                   f"file://{LOCAL_TRACKS['third']}\n"
                   "http://radio.example/stream.mp3\n"
                   "Test Artist - Test Album/gone.wav\n", encoding="utf-8")
    report.check("an m3u plays the files it lists, in its order",
                 localmedia.expand_paths([m3u]) == [LOCAL_TRACKS["second"],
                                                    LOCAL_TRACKS["third"]],
                 str([path.name for path in localmedia.expand_paths([m3u])]))

    pls = LOCAL_ROOT / "list.pls"
    pls.write_text(f"[playlist]\nFile2={LOCAL_TRACKS['third']}\n"
                   f"File1={LOCAL_TRACKS['first']}\nTitle1=First\n"
                   "NumberOfEntries=2\n", encoding="utf-8")
    report.check("a pls plays its FileN entries in N order",
                 localmedia.expand_paths([pls]) == [LOCAL_TRACKS["first"],
                                                    LOCAL_TRACKS["third"]],
                 str([path.name for path in localmedia.expand_paths([pls])]))

    xspf = LOCAL_ROOT / "list.xspf"
    xspf.write_text('<?xml version="1.0"?>\n<playlist version="1" '
                    'xmlns="http://xspf.org/ns/0/"><trackList><track><location>'
                    + LOCAL_TRACKS["loose"].as_uri()
                    + "</location></track></trackList></playlist>", encoding="utf-8")
    report.check("an xspf plays the location of its tracks",
                 localmedia.expand_paths([xspf]) == [LOCAL_TRACKS["loose"]],
                 str([path.name for path in localmedia.expand_paths([xspf])]))
    report.check("a playlist next to the folder does not repeat its files",
                 len(localmedia.expand_paths([m3u, pls, LOCAL_TRACKS["first"]])) == 3,
                 str([path.name for path in localmedia.expand_paths([m3u, pls,
                                                                    LOCAL_TRACKS["first"]])]))

    # ------------------------------------------------------------------ items
    items = localmedia.build_items(localmedia.expand_paths([m3u]))
    report.check("a dropped file becomes an item the window can show",
                 all(item.get("Id") and item.get("Name") and item.get("_local_path")
                     and item.get("Type") == "Audio" for item in items), str(items[:1]))
    report.check("the item id is stable and marked as local",
                 items[0]["Id"] == localmedia.local_id(LOCAL_TRACKS["second"])
                 and items[0]["Id"].startswith("local:"), items[0]["Id"])
    report.check("the file name is the title, without its track number",
                 items[0]["Name"] == "Second Song", items[0]["Name"])
    report.check("an \"Artist - Album\" folder names the artist and the album",
                 JellyfinClient.display_artist(items[0]) == "Test Artist"
                 and items[0]["Album"] == "Test Album",
                 f"{JellyfinClient.display_artist(items[0])} / {items[0]['Album']}")
    report.check("the display helpers accept a local item",
                 JellyfinClient.display_title(items[0]) == "Second Song"
                 and JellyfinClient.is_music(items[0]))
    report.check("cover art next to the song is used",
                 localmedia.cover_path(items[0]) == LOCAL_ALBUM / "cover.jpg",
                 str(localmedia.cover_path(items[0])))
    coverless = localmedia.build_items([LOCAL_TRACKS["loose"]])[0]
    report.check("a file without any artwork has no cover",
                 localmedia.cover_path(coverless) is None,
                 str(localmedia.cover_path(coverless)))
    report.check("a drop from a network location explains itself",
                 "network location" in localmedia.nothing_to_play_reason(
                     ["sftp://pi/music/A Song.flac"]),
                 localmedia.nothing_to_play_reason(["sftp://pi/music/A Song.flac"]))
    report.check("a drop without audio keeps the plain explanation",
                 localmedia.nothing_to_play_reason([LOCAL_ALBUM / "notes.txt"])
                 == "Nothing to play in that drop - no audio files were found",
                 localmedia.nothing_to_play_reason([LOCAL_ALBUM / "notes.txt"]))
    if localmedia.HAS_MUTAGEN:
        report.check("the duration comes from the file itself",
                     JellyfinClient.duration_seconds(items[0]) > 0.5,
                     f"{JellyfinClient.duration_seconds(items[0]):.2f}s")
    else:
        print("  [skip] mutagen is not installed - names come from the file names")

    # ------------------------------------------------- tags inside a real file
    ffmpeg = shutil.which("ffmpeg")
    if not (localmedia.HAS_MUTAGEN and ffmpeg):
        print("  [skip] mutagen and ffmpeg are both needed to build a tagged file")
        return
    tagged = LOCAL_ROOT / "tagged.mp3"
    tagged_cover = LOCAL_ROOT / "embedded.jpg"
    plain = LOCAL_ROOT / "plain.mp3"
    tagged_cover.write_bytes(COVER_BYTES)
    # Two steps, the way ffmpeg documents it: a tagged mp3, then the cover in.
    steps = ([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
              "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", "2",
              "-c:a", "libmp3lame",
              "-metadata", "title=Tagged Title", "-metadata", "artist=Tagged Artist",
              "-metadata", "album=Tagged Album", "-metadata", "track=7", str(plain)],
             [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
              "-i", str(plain), "-i", str(tagged_cover),
              "-map", "0:a", "-map", "1:v", "-c", "copy", "-id3v2_version", "3",
              "-metadata:s:v", "title=Album cover",
              "-metadata:s:v", "comment=Cover (front)", str(tagged)])
    try:
        for command in steps:
            subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=90, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  [skip] could not build a tagged file with ffmpeg ({exc})")
        return
    item = localmedia.build_items([tagged])[0]
    report.check("the tags name a dropped file (title/artist/album/track)",
                 (item["Name"], JellyfinClient.display_artist(item), item["Album"],
                  item["IndexNumber"]) == ("Tagged Title", "Tagged Artist",
                                           "Tagged Album", 7),
                 f"{item['Name']} / {JellyfinClient.display_artist(item)} / "
                 f"{item['Album']} / {item['IndexNumber']}")
    report.check("the duration of a tagged file is known",
                 JellyfinClient.duration_seconds(item) > 1.0,
                 f"{JellyfinClient.duration_seconds(item):.2f}s")
    cover = localmedia.cover_path(item)
    report.check("artwork stored inside the file is extracted for the window",
                 cover is not None and cover.is_file() and cover.stat().st_size > 0,
                 str(cover))
    if cover is not None:
        from PIL import Image

        with Image.open(cover) as image:
            report.check("the extracted cover is a real image", image.size[0] >= 100,
                         str(image.size))


def test_drop_parsing(report: Reporter) -> None:
    """What the file manager hands over: a Tcl list of paths (the XDND drop)."""
    report.check("a list of dropped paths is read",
                 dnd.parse_paths("/music/a.mp3 /music/b.flac")
                 == [Path("/music/a.mp3"), Path("/music/b.flac")])
    report.check("a file name with spaces survives the Tcl list",
                 dnd.parse_paths("{/music/my song.mp3}") == [Path("/music/my song.mp3")],
                 str(dnd.parse_paths("{/music/my song.mp3}")))
    report.check("a file name with brackets and a backslash survives too",
                 dnd.parse_paths("{/music/rock [live]/c\\d.mp3}")
                 == [Path("/music/rock [live]/c\\d.mp3")],
                 str(dnd.parse_paths("{/music/rock [live]/c\\d.mp3}")))
    report.check("a file:// URI is decoded",
                 dnd.parse_paths("file:///music/a%20b.mp3") == [Path("/music/a b.mp3")],
                 str(dnd.parse_paths("file:///music/a%20b.mp3")))
    report.check("a stream URL is not a local file",
                 dnd.parse_paths("https://radio.example/live.mp3") == [])
    report.check("an empty drop reads as nothing",
                 dnd.parse_paths("") == [] and dnd.parse_paths(None) == []
                 and dnd.parse_paths("{}") == [])
    report.check("parsing works without a Tcl interpreter as well",
                 dnd.parse_paths("/music/a.mp3", splitlist=lambda text: text.split("\n"))
                 == [Path("/music/a.mp3")])
    report.check("paths that are already split are accepted too",
                 dnd.parse_paths(["/music/a b.mp3", "/music/c.flac"])
                 == [Path("/music/a b.mp3"), Path("/music/c.flac")],
                 str(dnd.parse_paths(["/music/a b.mp3", "/music/c.flac"])))
    report.check("a network location is recognised (and not played as a file)",
                 dnd.parse_entries("sftp://pi/music/a.flac") == ["sftp://pi/music/a.flac"]
                 and dnd.parse_paths("sftp://pi/music/a.flac") == []
                 and dnd.remote_entries(["sftp://pi/a.flac", "/local/b.flac"])
                 == ["sftp://pi/a.flac"],
                 str(dnd.parse_entries("sftp://pi/music/a.flac")))
    if dnd.available():
        report.check("drag & drop support is installed", True,
                     "tkinterdnd2" if dnd._tkinterdnd2() else str(dnd._system_directory()))
    else:
        print(f"  [skip] {dnd.INSTALL_HINT}")


def test_dropped_playlist(report: Reporter, client: JellyfinClient) -> None:
    """A drop plays in order, then the player quietly returns to random.

    The dropped files last a second each, so the whole detour - drop it, play it
    through, land back in the library - takes only a few seconds.
    """
    build_local_fixtures()
    drop = [LOCAL_TRACKS["loose"], LOCAL_TRACKS["first"], LOCAL_TRACKS["second"]]
    engine = PlayerEngine(client, volume=0, debug=bool(os.environ.get("SELFTEST_DEBUG")))
    played: list = []

    def listener(state) -> None:
        name = (state.get("current") or {}).get("Name")
        if name and (not played or played[-1] != name):
            played.append(name)

    def library_track(state):
        """The current track when it came from Jellyfin (dropped ones are local)."""
        item = state.get("current") or {}
        return item if str(item.get("Id", "")).startswith("a") else None

    def local_track(state):
        """The current track when it is a file that was dropped on the window."""
        item = state.get("current") or {}
        return item if item.get("_local_path") else None

    engine.add_listener(listener)
    try:
        engine.start()
        report.check("the engine plays a random track before the drop",
                     wait_for(lambda: library_track(engine.snapshot()), 30) is not None)
        report.check("dropping three files queues three tracks",
                     engine.play_local(drop) == 3)
        started = wait_for(lambda: local_track(engine.snapshot()), 15)
        report.check("the first dropped file starts playing at once", started is not None,
                     str(engine.snapshot().get("current")))
        report.check("a dropped track points at its own file",
                     bool(started and started.get("_local_path")), str(started))
        report.check("the snapshot says a dropped playlist is playing",
                     wait_for(lambda: engine.snapshot()["local"]["active"], 5) is not None)
        report.check("and counts the tracks still to come",
                     engine.snapshot()["local"]["remaining"] >= 1,
                     str(engine.snapshot()["local"]))
        report.check("the drop runs to its end and the player returns to random",
                     wait_for(lambda: library_track(engine.snapshot()), 45) is not None)
        report.check("the dropped tracks played in the order they were dropped",
                     _subsequence(["Loose Track", "First Song", "Second Song"], played),
                     " -> ".join(played))
        report.check("once it is over no dropped playlist is reported any more",
                     not engine.snapshot()["local"]["active"]
                     and engine.snapshot()["local"]["remaining"] == 0,
                     str(engine.snapshot()["local"]))

        # ------------------------------------------------- clearing it on purpose
        report.check("a second drop is queued again", engine.play_local(drop) == 3)
        report.check("the second drop starts playing too",
                     wait_for(lambda: local_track(engine.snapshot()), 15) is not None)
        engine.clear_local_queue()
        report.check("clearing a dropped playlist goes back to random at once",
                     wait_for(lambda: library_track(engine.snapshot()), 45) is not None)
        report.check("a cleared playlist reports no drop any more",
                     not engine.snapshot()["local"]["active"]
                     and engine.snapshot()["local"]["remaining"] == 0,
                     str(engine.snapshot()["local"]))

        # ------------------------------------------------------------ the seam
        item = localmedia.build_items([LOCAL_TRACKS["loose"]])[0]
        with engine._lock:
            engine._local_total = 1
        report.check("a dropped track is never filtered away by a style filter",
                     engine._matches_filter(item))
        with engine._lock:
            engine._local_total = 0
        report.check("and is not handed out again once the list was cleared",
                     not engine._matches_filter(item))

        # --------------------------------------------------- folders and non-music
        report.check("dropping a folder plays only the audio inside it",
                     engine.play_local([LOCAL_LOOSE]) == 1)
        report.check("a drop without a single audio file changes nothing",
                     engine.play_local([LOCAL_ALBUM / "notes.txt"]) == 0
                     and "no audio" in engine.snapshot()["error"],
                     engine.snapshot()["error"])
        report.check("a drop from a network location says so instead",
                     engine.play_local(["sftp://pi/music/A Song.flac"]) == 0
                     and "network location" in engine.snapshot()["error"],
                     engine.snapshot()["error"])

        # ------------------------------------------------------- the sound card
        # Playing silently (the position runs, nothing comes out) must not be a
        # mystery: the engine checks a moment after each track starts and says so
        # in the footer. A healthy player must never trigger it.
        engine.play_local([LOCAL_TRACKS["first"]])
        time.sleep(4)                       # longer than the check's delay
        report.check("playing a dropped file does not warn about the sound card",
                     engine.snapshot()["error"] == "", engine.snapshot()["error"])

        # ------------------------------------------------------- the sound card
        # A dropped file can have a different format than the track before it (the
        # player's library tracks are 22 kHz mono here, this one is 44.1 kHz
        # stereo). mpv must follow the *file*, not stay on the old format - with
        # "gapless-audio=yes" it stayed, which made a dropped CD-format song come
        # out resampled (or silent, for formats the kept output cannot carry).
        cd_format = build_stereo_wav(LOCAL_ROOT / "cd format.wav")
        engine.play_local([cd_format])
        reported = None
        deadline = time.time() + 10
        while time.time() < deadline:
            out = engine.mpv.get("audio-out-params") or {}
            if out.get("samplerate") == 44100 and out.get("channel-count") == 2:
                reported = out
                break
            time.sleep(0.2)
        report.check("the sound card follows the dropped file's own format",
                     reported is not None,
                     f"audio-out-params={engine.mpv.get('audio-out-params')} "
                     f"audio-params={engine.mpv.get('audio-params')}")

        # ------------------------------------------------- the format of each file
        # mpv keeps the card open across files, so before this the *next* track was
        # pushed through the previous one's format: an mp3 (decoded as float) after
        # a 16-bit file went into the card's 16-bit pipe - the state in which a song
        # plays with no sound at all. The engine now looks at every track that
        # starts and opens the output again when the two disagree - and only then,
        # so that a playlist of same-format tracks still runs gapless.
        mp3 = LOCAL_ROOT / "mp3 format.mp3"
        ffmpeg = shutil.which("ffmpeg")
        if not mp3.exists() and ffmpeg:
            subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "4",
                            "-c:a", "libmp3lame", str(mp3)], check=False)
        if mp3.exists():
            reloads = {"count": 0}
            real_reload = engine.mpv.reload_audio_output

            def counting_reload() -> None:
                reloads["count"] += 1
                real_reload()

            engine.mpv.reload_audio_output = counting_reload
            engine.play_local([cd_format])          # a 16-bit stereo track first
            wait_for(lambda: (engine.mpv.get("audio-out-params") or {}).get("format") == "s16", 10)
            engine.play_local([mp3])                # decodes to float
            wait_for(lambda: reloads["count"] > 0, 6)
            report.check("the sound card is opened again for a file of another format",
                         reloads["count"] > 0,
                         f"reopens={reloads['count']} "
                         f"out={engine.mpv.get('audio-out-params')} "
                         f"params={engine.mpv.get('audio-params')}")
            before = reloads["count"]
            engine.play_local([mp3])                # same format as the card: no reopen
            time.sleep(2.0)
            report.check("a file whose format already matches does not re-open it",
                         reloads["count"] == before,
                         f"reopens={reloads['count'] - before}")
            engine.mpv.reload_audio_output = real_reload
        else:
            print("  [skip] ffmpeg is needed to build an mp3 for the format check")

        # ------------------------------------------------------------ while paused
        engine.toggle_pause()
        paused_before = engine.snapshot()["paused"]
        engine.play_local([LOCAL_TRACKS["third"]])
        report.check("a dropped track also plays when the player was paused",
                     paused_before
                     and wait_for(lambda: not engine.snapshot()["paused"], 5) is not None,
                     f"paused {paused_before} -> {engine.snapshot()['paused']}")
    finally:
        engine.stop()


def test_drop_ui(report: Reporter) -> None:
    """The drop button, the drop hint and the C key (needs a display)."""
    try:
        import tkinter as tk

        import ui
    except ImportError as exc:
        report.check(f"tkinter is importable ({exc})", False)
        return
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"  [skip] no display for the drop checks ({exc})", flush=True)
        return

    def pump(seconds: float) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            root.update()
            time.sleep(0.02)

    def layout() -> tuple:
        """Everything on screen that a drop must never move."""
        return (screen._cover_size, screen._progress.winfo_rooty(),
                screen._transport.winfo_rooty(), screen._footer.winfo_rooty(),
                screen._header.winfo_height(), screen._header.winfo_reqheight())

    screen = None
    engine = _StubEngine()
    try:
        root.geometry("1280x800")
        screen = ui.PlayerScreen(root, engine=engine,
                                 client=JellyfinClient("http://127.0.0.1:1"),
                                 family=ui.pick_font_family(),
                                 on_change_account=_noop, on_quit=_noop)
        screen.pack(fill="both", expand=True)
        pump(0.6)
        before = layout()

        # ------------------------------------------------------------ the button
        buttons = [child for child in screen._header.winfo_children()
                   if isinstance(child, tk.Button)]
        report.check("the \"back to random\" button starts out hidden",
                     screen._clear_button in buttons
                     and not screen._clear_button.winfo_manager())
        report.check("it is exactly as tall as the buttons next to it",
                     len({button.winfo_reqheight() for button in buttons}) == 1,
                     str([button.winfo_reqheight() for button in buttons]))
        report.check("the drop hint is not on screen at start",
                     not screen._drop_hint.place_info())

        engine.emit_local(True, remaining=2, total=4)
        pump(0.3)
        report.check("the button appears while a dropped playlist plays",
                     bool(screen._clear_button.winfo_manager()))
        report.check("the button counts the tracks still to come",
                     str(screen._clear_button.cget("text")) == "Back to random (2)",
                     str(screen._clear_button.cget("text")))
        report.check("showing the button moves nothing", layout() == before,
                     f"{before} -> {layout()}")

        engine.emit_local(False)
        pump(0.3)
        report.check("the button disappears when the playlist is over",
                     not screen._clear_button.winfo_manager())
        report.check("hiding it again moves nothing either", layout() == before,
                     f"{before} -> {layout()}")

        # -------------------------------------------------------------- a drop
        screen._show_drop_hint()
        pump(0.2)
        report.check("a hovering drag shows the drop hint",
                     bool(screen._drop_hint.place_info()))
        screen._on_drop("{/music/One Song.mp3} /music/Two.flac")
        pump(0.3)
        report.check("a drop hands the files to the engine",
                     engine.drop_calls[-1:] == [("play", ["/music/One Song.mp3",
                                                          "/music/Two.flac"])],
                     str(engine.drop_calls))
        report.check("the hint is taken away again after the drop",
                     not screen._drop_hint.place_info())
        screen._clear_playlist()
        pump(0.3)
        report.check("the button asks the engine to forget the playlist",
                     engine.drop_calls[-1:] == [("clear", [])], str(engine.drop_calls))
        report.check("C is bound and in the shortcut table",
                     bool(root.bind("<c>"))
                     and any(name == "_clear_playlist" and "<c>" in sequences
                             for name, sequences, _t in ui.PlayerScreen._SHORTCUTS))

        # -------------------------------------------------------------- the help
        screen._open_help()
        pump(0.2)
        texts = widget_texts(screen._overlay) if screen._overlay is not None else []
        report.check("the help card mentions dropping files on the window",
                     any("Drag" in text for text in texts), str(texts)[:160])
        screen._close_overlay()
        pump(0.2)
        report.check("none of the drop interface moved the layout", layout() == before,
                     f"{before} -> {layout()}")

        if screen._dnd_version is not None:
            report.check("the window really is a drop target", True,
                         f"tkdnd {screen._dnd_version}")
        else:
            print(f"  [skip] {dnd.INSTALL_HINT}")
    finally:
        if screen is not None:
            screen.destroy()
        root.destroy()


def test_drop_end_to_end(report: Reporter, client: JellyfinClient) -> None:
    """A drop through the real window: UI -> action thread -> engine -> mpv.

    The other checks cover the two halves (the window with a stub engine, the
    engine on its own); this one puts them together and drops a file on a real
    PlayerScreen, exactly as the file manager would.
    """
    try:
        import tkinter as tk

        import ui
    except ImportError as exc:
        report.check(f"tkinter is importable ({exc})", False)
        return
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"  [skip] no display for the drop end-to-end check ({exc})", flush=True)
        return

    build_local_fixtures()
    engine = PlayerEngine(client, volume=0, debug=bool(os.environ.get("SELFTEST_DEBUG")))
    screen = None

    def pump(seconds: float) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            root.update()
            time.sleep(0.02)

    def pump_until(predicate: Any, timeout: float) -> bool:
        """Wait for something to appear on screen (the Tk loop must run for it)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            root.update()
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def dropped_track(state):
        item = state.get("current") or {}
        return item if item.get("_local_path") else None

    def library_track(state):
        item = state.get("current") or {}
        return item if str(item.get("Id", "")).startswith("a") else None

    try:
        engine.start()
        if wait_for(lambda: engine.snapshot().get("current"), 30) is None:
            report.check("the engine started for the drop check", False)
            return
        root.geometry("1280x800")
        screen = ui.PlayerScreen(root, engine=engine, client=client,
                                 family=ui.pick_font_family(),
                                 on_change_account=_noop, on_quit=_noop)
        screen.pack(fill="both", expand=True)
        pump(0.8)
        report.check("a real window is a drop target (tkdnd is loaded)",
                     screen._dnd_version is not None, str(screen._dnd_version))
        report.check("and starts without a \"back to random\" button",
                     not screen._clear_button.winfo_manager())

        # Exactly what the file manager hands over: a Tcl list of paths.
        data = tcl_drop([LOCAL_TRACKS["loose"], LOCAL_TRACKS["first"]])
        screen._on_drop(data)
        report.check("a drop on the window starts playing in the player",
                     wait_for(lambda: dropped_track(engine.snapshot()), 20) is not None,
                     str(engine.snapshot().get("current")))
        report.check("the window shows the dropped song's name",
                     pump_until(lambda: "Loose Track" in str(screen._title.cget("text")), 10),
                     str(screen._title.cget("text")))
        report.check("the window offers \"back to random\" without anything clicked",
                     pump_until(lambda: bool(screen._clear_button.winfo_manager()), 5))
        screen._clear_playlist()
        report.check("and that button puts the player back in the library",
                     wait_for(lambda: library_track(engine.snapshot()), 45) is not None,
                     str(engine.snapshot().get("current")))
        report.check("the button is gone once the drop playlist is cleared",
                     pump_until(lambda: not screen._clear_button.winfo_manager(), 5))
    finally:
        if screen is not None:
            screen.destroy()
        engine.stop()


def _xdnd_drop_on_window(report: Reporter, root: Any, engine: Any, x11: Any, ctypes: Any,
                         event_class: Any, tracks: dict) -> None:
    """Drag *tracks* onto *root* with the XDND protocol and check what arrives.

    Split out of :func:`test_drop_protocol` because everything here needs the
    ctypes structures and the X connection that the caller set up.
    """
    display = x11.XOpenDisplay(None)
    atoms = {name: x11.XInternAtom(ctypes.c_void_p(display), name.encode(), False)
             for name in ("XdndSelection", "XdndEnter", "XdndPosition", "XdndDrop",
                          "XdndActionCopy", "text/uri-list")}
    root_window = x11.XDefaultRootWindow(ctypes.c_void_p(display))
    source_window = x11.XCreateSimpleWindow(ctypes.c_void_p(display),
                                            ctypes.c_ulong(root_window), 0, 0, 1, 1, 0, 0, 0)
    x11.XSetSelectionOwner(ctypes.c_void_p(display), ctypes.c_ulong(atoms["XdndSelection"]),
                           ctypes.c_ulong(source_window), 0)
    x11.XFlush(ctypes.c_void_p(display))
    uri = tracks["loose"].as_uri() + "\r\n" + tracks["first"].as_uri() + "\r\n"
    stopped = threading.Event()
    answers = {"asked": 0}

    def serve_requests() -> None:
        """Answer the data request, the way the file manager does."""
        event = event_class()
        while not stopped.is_set():
            if not x11.XPending(ctypes.c_void_p(display)):
                time.sleep(0.02)
                continue
            x11.XNextEvent(ctypes.c_void_p(display), ctypes.byref(event))
            if event.type != 30:                        # SelectionRequest
                continue
            request = event.xselectionrequest
            payload = uri.encode()
            buffer = ctypes.create_string_buffer(payload)
            property_atom = request.property or request.target
            x11.XChangeProperty(ctypes.c_void_p(display), ctypes.c_ulong(request.requestor),
                                ctypes.c_ulong(property_atom), ctypes.c_ulong(request.target),
                                8, 0, buffer, len(payload))
            notify = event_class()
            notify.xselectionrequest.type = 31          # SelectionNotify
            notify.xselectionrequest.send_event = True
            notify.xselectionrequest.display = ctypes.c_void_p(display)
            notify.xselectionrequest.requestor = request.requestor
            notify.xselectionrequest.selection = request.selection
            notify.xselectionrequest.target = request.target
            notify.xselectionrequest.property = property_atom
            notify.xselectionrequest.time = request.time
            x11.XSendEvent(ctypes.c_void_p(display), ctypes.c_ulong(request.requestor),
                           False, 0, ctypes.byref(notify))
            x11.XFlush(ctypes.c_void_p(display))
            answers["asked"] += 1

    threading.Thread(target=serve_requests, daemon=True).start()

    def send(message: str, window: int, values: list) -> None:
        event = event_class()
        event.xclient.type = 33                         # ClientMessage
        event.xclient.send_event = True
        event.xclient.display = ctypes.c_void_p(display)
        event.xclient.window = window
        event.xclient.message_type = atoms[message]
        event.xclient.format = 32
        for index, value in enumerate(values):
            event.xclient.data[index] = ctypes.c_long(value)
        x11.XSendEvent(ctypes.c_void_p(display), ctypes.c_ulong(window), False, 0,
                       ctypes.byref(event))
        x11.XFlush(ctypes.c_void_p(display))

    def parent_of(win: int) -> int:
        root_w = ctypes.c_ulong()
        parent_w = ctypes.c_ulong()
        kids = ctypes.POINTER(ctypes.c_ulong)()
        count = ctypes.c_uint()
        x11.XQueryTree(ctypes.c_void_p(display), ctypes.c_ulong(win), ctypes.byref(root_w),
                       ctypes.byref(parent_w), ctypes.byref(kids), ctypes.byref(count))
        if kids:
            x11.XFree(kids)
        return parent_w.value

    # A drag source looks for the window the window manager created around the
    # player (tkdnd marks that one as XDND-aware); without a WM it is the window
    # itself.
    def pump(seconds: float) -> None:
        """The messages arrive on Tk's own connection: its loop must run."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            root.update()
            time.sleep(0.02)

    def pump_until(predicate: Any, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            root.update()
            if predicate():
                return True
            time.sleep(0.02)
        return False

    client_window = root.winfo_id()
    target_window = parent_of(client_window) or client_window
    position = ((root.winfo_rootx() + 200) << 16) | (root.winfo_rooty() + 200)
    pump(0.4)                           # let the selection ownership settle
    send("XdndEnter", target_window, [source_window, 5 << 24, atoms["text/uri-list"], 0, 0])
    pump(0.4)
    send("XdndPosition", target_window,
         [source_window, 0, position, 0, atoms["XdndActionCopy"]])
    pump(0.4)
    send("XdndDrop", target_window, [source_window, 0, 0, 0, 0])
    try:
        report.check("the window accepts a drop from the file manager (XDND)",
                     pump_until(lambda: bool(engine.drop_calls), 15),
                     f"{answers['asked']} data request(s) answered")
        report.check("the dropped files arrive as the paths that were dragged",
                     engine.drop_calls[-1:] == [("play", [str(tracks["loose"]),
                                                          str(tracks["first"])])],
                     str(engine.drop_calls))
    finally:
        stopped.set()


def test_drop_protocol(report: Reporter) -> None:
    """A real XDND drop: the protocol exchange a file manager performs.

    Is the window registered correctly *and* is the data read the way tkdnd
    hands it over? That can only be shown by playing the other side: this test
    opens its own X connection, owns the drag selection like Dolphin does, sends
    enter/position/drop to the player window and answers the data request with a
    ``text/uri-list``. The window must then hand exactly those two files to the
    engine.
    """
    try:
        import ctypes
        import tkinter as tk

        import ui
    except ImportError as exc:
        report.check(f"tkinter is importable ({exc})", False)
        return
    try:
        x11 = ctypes.CDLL("libX11.so.6")
        root = tk.Tk()
    except (OSError, tk.TclError) as exc:
        print(f"  [skip] no X11 session for the protocol check ({exc})", flush=True)
        return

    class ClientMessage(ctypes.Structure):
        _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong),
                    ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
                    ("window", ctypes.c_ulong), ("message_type", ctypes.c_ulong),
                    ("format", ctypes.c_int), ("data", ctypes.c_long * 5)]

    class SelectionRequest(ctypes.Structure):
        _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong),
                    ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
                    ("owner", ctypes.c_ulong), ("requestor", ctypes.c_ulong),
                    ("selection", ctypes.c_ulong), ("target", ctypes.c_ulong),
                    ("property", ctypes.c_ulong), ("time", ctypes.c_ulong)]

    class Event(ctypes.Union):
        _fields_ = [("type", ctypes.c_int), ("xclient", ClientMessage),
                    ("xselectionrequest", SelectionRequest), ("pad", ctypes.c_long * 24)]

    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XInternAtom.restype = ctypes.c_ulong
    x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x11.XDefaultRootWindow.restype = ctypes.c_ulong
    x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]

    engine = _StubEngine()
    screen = None
    try:
        build_local_fixtures()
        root.geometry("1280x800")
        screen = ui.PlayerScreen(root, engine=engine,
                                 client=JellyfinClient("http://127.0.0.1:1"),
                                 family=ui.pick_font_family(),
                                 on_change_account=_noop, on_quit=_noop)
        screen.pack(fill="both", expand=True)
        deadline = time.time() + 0.8
        while time.time() < deadline:
            root.update()
            time.sleep(0.02)
        if screen._dnd_version is None:
            print(f"  [skip] {dnd.INSTALL_HINT}", flush=True)
            return
        _xdnd_drop_on_window(report, root, engine, x11, ctypes, Event, LOCAL_TRACKS)
    finally:
        if screen is not None:
            screen.destroy()
        root.destroy()


def main() -> int:
    report = Reporter()
    server, port = start_server()
    engine = None
    try:
        print(f"fake Jellyfin on http://127.0.0.1:{port}   (scratch dir: {_TMP})")
        print("\nJellyfin client")
        client = test_client(report, port)
        print("\nPreloader")
        test_preloader(report, client)
        print("\nPlayback engine (silent, volume 0)")
        engine = test_engine(report, client)
        print("\nDropped files, folders and playlists")
        test_local_media(report)
        test_drop_parsing(report)
        print("\nA dropped playlist in the player")
        test_dropped_playlist(report, client)
        print("\nStyle catalog and style filter")
        test_style_filter(report, client, test_catalog(report, build_fake_catalog(_TMP / "fake-catalog.sqlite")))
        print("\nUser interface")
        test_ui(report)
        print("\nOverlays (help card, style picker)")
        test_overlays(report)
        test_style_panel(report)
        print("\nDropping files on the window")
        test_drop_ui(report)
        test_drop_protocol(report)
        print("\nA drop through the real window")
        test_drop_end_to_end(report, client)
        print("\nRemembering the style filter")
        test_style_config(report)
        print("\nLauncher and application icon")
        test_launcher(report)
        print("\nSingle instance")
        test_instance(report)
    finally:
        if engine is not None:
            engine.stop()
        server.shutdown()
        server.server_close()
        shutil.rmtree(_TMP, ignore_errors=True)
    print()
    if report.failures:
        print(f"FAILED ({len(report.failures)}): " + "; ".join(report.failures))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



