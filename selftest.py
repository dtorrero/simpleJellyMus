#!/usr/bin/env python3
"""Offline self-test for SimpleJellyMus.

Spins up a tiny fake Jellyfin server (with decoy video items) and checks the
client, the music-only filter, cover download, the preload cache, the whole
playback engine (auto-advance, next, previous, pause, volume, seek), the UI
layout with long titles and the single-instance guard.

    python3 selftest.py

Playback happens at volume 0 and every cache/config artefact goes to a
throwaway directory, so your real Jellyfin login and library are never used.
"""

import json
import math
import os
import shutil
import struct
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

import jellyfin  # noqa: E402  (imported once the environment is ready)
from jellyfin import AuthError, JellyfinClient  # noqa: E402
from player import PlayerEngine, Preloader  # noqa: E402

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
            engine.previous_track()
            back = track_changed(engine, third["Id"], 15)
            report.check("previous goes back to the track we came from",
                         back is not None and back["Id"] == second_id,
                         f"{third['Id']} -> {back['Id'] if back else 'nothing'}")
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
        print("\nUser interface")
        test_ui(report)
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



