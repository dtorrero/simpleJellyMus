#!/usr/bin/env python3
"""Playback engine for SimpleJellyMus.

Layers:
    * MpvPlayer    - thread-safe client for mpv's JSON IPC interface
    * Preloader    - downloads the next track to disk while one plays
    * PlayQueue    - random audio batches with "no repeats / no same artist"
    * PlayerEngine - glues everything together and exposes a state snapshot

mpv is always started audio only (``--no-video --vid=no --audio-display=no``)
so a stray video stream can never be decoded or displayed.
"""

from __future__ import annotations

import itertools
import json
import os
import random
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

from jellyfin import (
    AUDIO_CACHE_DIR,
    DownloadCancelled,
    JellyfinClient,
    JellyfinError,
    audio_extension,
)

PRELOAD_DEADLINE_SECONDS = 25.0
PRELOAD_RETRY_SECONDS = 2.0
LIBRARY_TIMEOUT_SECONDS = 60.0
PLAYBACK_START_TIMEOUT = 10.0
HISTORY_LIMIT = 60
QUEUE_MIN_BATCH = 25
QUEUE_BATCH_SIZE = 200
MAX_CACHED_TRACKS = 4


class PlayerError(Exception):
    """Raised when mpv cannot be started or controlled."""


# Feeds the per-instance IPC socket name (see MpvPlayer.__init__).
_PLAYER_INSTANCES = itertools.count(1)


def cleanup_stale_sockets() -> None:
    """Delete sockets left behind by crashed runs (never those in use)."""
    for path in Path(tempfile.gettempdir()).glob("simplejellymus-*.sock"):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.2)
            probe.connect(str(path))
        except OSError:
            try:
                path.unlink()       # nothing answered: stale file
            except OSError:
                pass
        except Exception:
            pass
        finally:
            probe.close()


class MpvPlayer:
    """Minimal JSON-IPC client for a dedicated, audio-only mpv process."""

    def __init__(self, *, volume: int = 80, debug: bool = False) -> None:
        self.debug = debug
        self.volume = max(0, min(100, int(volume)))
        self._instance = next(_PLAYER_INSTANCES)
        # One socket per instance: a re-login (or a quick restart) must never be
        # able to unlink the socket of a player that is still running.
        self._socket_path = Path(tempfile.gettempdir()) / (
            f"simplejellymus-{os.getpid()}-{self._instance}.sock"
        )
        self._process: Optional[subprocess.Popen] = None
        self._socket: Optional[socket.socket] = None
        self._socket_file = None
        self._reader: Optional[threading.Thread] = None
        self._send_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._pending: Dict[int, List[Any]] = {}
        self._property_listeners: Dict[str, List[Callable[[Any], None]]] = {}
        self._event_listeners: List[Callable[[str, Dict[str, Any]], None]] = []
        self._request_ids = itertools.count(1)
        self._observe_ids = itertools.count(1)
        self._closed = threading.Event()

    # ------------------------------------------------------------------ setup
    def start(self) -> None:
        """Spawn mpv and connect to its IPC socket."""
        binary = shutil.which("mpv")
        if not binary:
            raise PlayerError("mpv was not found in PATH (install it with: sudo pacman -S mpv)")
        cleanup_stale_sockets()
        try:
            self._socket_path.unlink()
        except OSError:
            pass
        command = [
            binary,
            "--idle=yes",
            "--no-config",
            "--no-terminal",
            "--no-video",
            "--vid=no",
            "--audio-display=no",
            "--no-sub",
            "--force-window=no",
            "--ytdl=no",
            "--osc=no",
            "--input-default-bindings=no",
            "--input-vo-keyboard=no",
            "--audio-client-name=SimpleJellyMus",
            "--ao=pulse,alsa",
            "--cache=yes",
            "--demuxer-max-bytes=64MiB",
            "--demuxer-readahead-secs=30",
            "--prefetch-playlist=yes",
            "--gapless-audio=yes",
            "--volume-max=100",
            f"--volume={self.volume}",
            f"--input-ipc-server={self._socket_path}",
        ]
        if not self.debug:
            command.append("--really-quiet")
        self._process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL
        )
        deadline = time.time() + PLAYBACK_START_TIMEOUT
        connected = False
        while time.time() < deadline:
            if self._socket_path.exists():
                candidate = None
                try:
                    candidate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    candidate.connect(str(self._socket_path))
                except OSError:
                    if candidate is not None:
                        candidate.close()
                    time.sleep(0.1)
                    continue
                self._socket = candidate
                connected = True
                break
            if self._process.poll() is not None:
                raise PlayerError("mpv exited immediately (run with --debug for details)")
            time.sleep(0.1)
        if not connected:
            raise PlayerError("mpv did not open its control socket")
        self._socket_file = self._socket.makefile("rwb")
        self._reader = threading.Thread(target=self._reader_loop, name="mpv-reader", daemon=True)
        self._reader.start()

    # ---------------------------------------------------------------- IPC core
    def _reader_loop(self) -> None:
        """Read mpv replies and events until the socket closes."""
        while not self._closed.is_set():
            try:
                line = self._socket_file.readline()
            except (OSError, ValueError):
                break
            if not line:
                break
            try:
                message = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if self.debug:
                print("[mpv]", line.decode("utf-8", "replace").strip(), flush=True)
            request_id = message.get("request_id")
            if request_id is not None:
                with self._state_lock:
                    waiter = self._pending.pop(request_id, None)
                if waiter is not None:
                    waiter[1] = message
                    waiter[0].set()
                continue
            event = message.get("event")
            if event == "property-change":
                name = message.get("name")
                if name:
                    for callback in list(self._property_listeners.get(name, ())):
                        self._safe(callback, message.get("data"))
            elif event:
                for callback in list(self._event_listeners):
                    self._safe(callback, event, message)

    @staticmethod
    def _safe(callback: Callable[..., None], *args: Any) -> None:
        try:
            callback(*args)
        except Exception as exc:  # never kill the reader thread
            print(f"[player] callback error: {exc}", flush=True)

    def _send(self, command: List[Any], *, timeout: float = 3.0) -> Dict[str, Any]:
        """Send one IPC command and wait for its reply.

        Only mpv's reader thread can deliver a reply, so a command issued from
        that thread would wait for itself. Fail fast in that case instead of
        stalling the player (and with it the whole UI) for seconds.
        """
        if self._socket_file is None:
            raise PlayerError("mpv is not running")
        if threading.current_thread() is self._reader:
            raise PlayerError(
                f"refusing to send {command[0]!r} from mpv's reader thread: "
                "the reply could never be delivered"
            )
        request_id = next(self._request_ids)
        waiter: List[Any] = [threading.Event(), None]
        with self._state_lock:
            self._pending[request_id] = waiter
        payload = json.dumps({"command": command, "request_id": request_id}) + "\n"
        try:
            with self._send_lock:
                self._socket_file.write(payload.encode("utf-8"))
                self._socket_file.flush()
        except (OSError, ValueError) as exc:
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise PlayerError(f"lost the connection to mpv ({exc})") from exc
        if not waiter[0].wait(timeout):
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise PlayerError(f"mpv did not answer {command[0]!r} in time")
        with self._state_lock:
            self._pending.pop(request_id, None)
        reply = waiter[1] or {}
        if self.debug and reply.get("error") not in (None, "success"):
            print(f"[mpv] {command[0]} -> {reply.get('error')}", flush=True)
        return reply

    def get(self, name: str, default: Any = None) -> Any:
        """Return one mpv property, or *default* when it is unavailable."""
        try:
            reply = self._send(["get_property", name], timeout=3.0)
        except PlayerError:
            return default
        if reply.get("error") == "success" and "data" in reply:
            return reply["data"]
        return default

    def observe(self, name: str, callback: Callable[[Any], None]) -> None:
        """Register a property observer (callback runs on the reader thread)."""
        with self._state_lock:
            self._property_listeners.setdefault(name, []).append(callback)
        try:
            self._send(["observe_property", next(self._observe_ids), name])
        except PlayerError:
            pass

    def on_event(self, callback: Callable[[str, Dict[str, Any]], None]) -> None:
        with self._state_lock:
            self._event_listeners.append(callback)

    # --------------------------------------------------------------- transport
    def load(self, source: Any) -> Optional[int]:
        """Play *source* (local path or URL) now; returns the new mpv entry id."""
        return self._entry_id(self._send(["loadfile", str(source), "replace"]))

    def append(self, source: Any) -> Optional[int]:
        """Queue *source* right after the current entry (preload hook).

        mpv has no ``playlist-append`` command; appending is done by loading a
        file with the ``append`` flag. The reply carries the new entry id, which
        the engine uses to recognise the entry once mpv starts playing it.
        """
        return self._entry_id(self._send(["loadfile", str(source), "append"]))

    @staticmethod
    def _entry_id(reply: Dict[str, Any]) -> Optional[int]:
        data = reply.get("data")
        if isinstance(data, dict) and isinstance(data.get("playlist_entry_id"), int):
            return data["playlist_entry_id"]
        return None

    def clear_playlist(self) -> None:
        """Drop queued entries but keep the file that is playing."""
        try:
            self._send(["playlist-clear"], timeout=2.0)
        except PlayerError:
            pass

    def playlist_next(self) -> None:
        self._send(["playlist-next"], timeout=2.0)

    def set_pause(self, paused: bool) -> None:
        self._send(["set_property", "pause", bool(paused)], timeout=2.0)

    def toggle_pause(self) -> bool:
        """Flip the pause state and return the new value."""
        paused = not bool(self.get("pause", False))
        self.set_pause(paused)
        return paused

    def seek(self, seconds: float, *, absolute: bool = False) -> None:
        try:
            if absolute:
                self._send(["seek", float(seconds), "absolute+exact"], timeout=2.0)
            else:
                self._send(["seek", float(seconds), "relative"], timeout=2.0)
        except PlayerError:
            pass

    def set_volume(self, value: float) -> None:
        self._send(["set_property", "volume", max(0.0, min(100.0, float(value)))], timeout=2.0)

    def is_idle(self) -> bool:
        return bool(self.get("idle-active", True))

    def shutdown(self) -> None:
        """Ask mpv to quit, then make sure the process is really gone."""
        self._closed.set()
        try:
            self._send(["quit"], timeout=2.0)
        except PlayerError:
            pass
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
        for handle in (self._socket_file, self._socket):
            try:
                if handle is not None:
                    handle.close()
            except Exception:
                pass
        try:
            self._socket_path.unlink()
        except OSError:
            pass

class Preloader:
    """Downloads upcoming tracks in the background and prunes old cache files."""

    def __init__(self, client: JellyfinClient, cache_dir: Path = AUDIO_CACHE_DIR, *,
                 max_files: int = MAX_CACHED_TRACKS, debug: bool = False) -> None:
        self._client = client
        self._cache_dir = Path(cache_dir)
        self._max_files = max(2, int(max_files))
        self._debug = debug
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._request: Optional[tuple] = None
        self._token = 0
        self._thread: Optional[threading.Thread] = None
        self._downloaded: Dict[str, Path] = {}

    # ------------------------------------------------------------------ public
    def cached_path(self, item: Dict[str, Any]) -> Optional[Path]:
        """Return the local file for *item* when it is already cached."""
        if not item or not item.get("Id"):
            return None
        item_id = str(item["Id"])
        with self._lock:
            known = self._downloaded.get(item_id)
        if known is not None and known.exists():
            return known
        for candidate in self._cache_dir.glob(f"{item_id}.*"):
            if candidate.suffix != ".part" and candidate.is_file():
                with self._lock:
                    self._downloaded[item_id] = candidate
                return candidate
        return None

    def request(self, item: Dict[str, Any], callback: Callable[[Dict[str, Any], Path], None]) -> int:
        """Queue *item* for download; *callback* runs from the worker thread."""
        if self._stop.is_set() or not item or not item.get("Id"):
            return 0
        existing = self.cached_path(item)
        with self._lock:
            self._token += 1
            token = self._token
            self._request = None if existing is not None else (token, dict(item), callback)
        self._prune(keep=[item])
        if existing is not None:
            try:
                callback(dict(item), existing)
            except Exception as exc:
                print(f"[preload] callback error: {exc}", flush=True)
            return token
        self._wake.set()
        self._ensure_thread()
        return token

    def cancel(self) -> None:
        """Forget the pending download (an ongoing one stops at the next chunk)."""
        with self._lock:
            self._token += 1
            self._request = None

    def cleanup_partials(self) -> None:
        """Remove half-written files left over by a previous run."""
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        for partial in self._cache_dir.glob("*.part"):
            try:
                partial.unlink()
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()
        self.cancel()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)

    # --------------------------------------------------------------- internals
    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._worker, name="preloader", daemon=True)
            self._thread.start()

    def _worker(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            if self._stop.is_set():
                return
            with self._lock:
                request = self._request
                self._request = None
            if request is None:
                continue
            token, item, callback = request
            destination = self._cache_dir / f"{item['Id']}.{audio_extension(item)}"
            try:
                path = self._client.download_audio(
                    item, destination, is_cancelled=lambda: self._is_stale(token)
                )
            except DownloadCancelled:
                continue
            except (JellyfinError, OSError) as exc:
                if self._debug:
                    print(f"[preload] {item.get('Name')!r} failed: {exc}", flush=True)
                continue
            if self._is_stale(token):
                continue
            with self._lock:
                self._downloaded[str(item["Id"])] = path
            self._prune(keep=[item])
            try:
                callback(item, path)
            except Exception as exc:
                print(f"[preload] callback error: {exc}", flush=True)

    def _is_stale(self, token: int) -> bool:
        with self._lock:
            return token != self._token or self._stop.is_set()

    def _prune(self, keep: List[Dict[str, Any]]) -> None:
        """Keep at most MAX_CACHED_TRACKS files, never deleting *keep*."""
        keep_ids = {str(item.get("Id")) for item in keep if item}
        try:
            files = sorted(
                (f for f in self._cache_dir.glob("*") if f.is_file() and f.suffix != ".part"),
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        for index, path in enumerate(files):
            if path.stem in keep_ids or index < self._max_files:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            with self._lock:
                self._downloaded.pop(path.stem, None)

class PlayQueue:
    """Supplies random audio items while avoiding recent repeats."""

    def __init__(self, client: JellyfinClient, *, history_limit: int = HISTORY_LIMIT,
                 batch_size: int = QUEUE_BATCH_SIZE, min_batch: int = QUEUE_MIN_BATCH,
                 debug: bool = False) -> None:
        self._client = client
        self._debug = debug
        self._batch: List[Dict[str, Any]] = []
        self._batch_ids: set = set()
        self._recent: Deque[str] = deque(maxlen=history_limit)
        self._lock = threading.RLock()
        self._refilling = False
        self._available = threading.Event()
        self._batch_size = batch_size
        self._min_batch = min_batch
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------------ public
    def ensure_batch(self, *, timeout: float = LIBRARY_TIMEOUT_SECONDS) -> bool:
        """Block (up to *timeout*) until at least one track is available."""
        deadline = time.time() + max(0.0, timeout)
        while True:
            with self._lock:
                if self._batch:
                    return True
            self.request_refill(force=True)
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            self._available.wait(timeout=min(1.0, remaining))

    def request_refill(self, force: bool = False) -> None:
        """Fetch another random batch in the background when it runs low."""
        with self._lock:
            if self._refilling:
                return
            if not force and len(self._batch) >= self._min_batch:
                return
            self._refilling = True
        threading.Thread(target=self._refill, name="queue-refill", daemon=True).start()

    def pick(self, *, avoid_artist: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Return the next random track, or ``None`` when the batch is empty."""
        with self._lock:
            candidates = list(self._batch)
            recent = set(self._recent)
        if not candidates:
            self.request_refill(force=True)
            return None
        fresh = [item for item in candidates if item.get("Id") not in recent]
        if not fresh:
            fresh = candidates
        if avoid_artist:
            others = [i for i in fresh if JellyfinClient.artist_key(i) != avoid_artist]
            if others:
                fresh = others
        item = fresh[random.randrange(len(fresh))]
        with self._lock:
            try:
                self._batch.remove(item)
                self._batch_ids.discard(item.get("Id"))
            except ValueError:
                pass
            self._recent.append(str(item.get("Id")))
            low = len(self._batch) < self._min_batch
        if low:
            self.request_refill()
        return dict(item)

    # --------------------------------------------------------------- internals
    def _refill(self) -> None:
        try:
            items = self._client.random_batch(self._batch_size)
            self.last_error = None
        except JellyfinError as exc:
            items = []
            self.last_error = str(exc)
            if self._debug:
                print("[queue] refill failed:", exc, flush=True)
        with self._lock:
            recent = set(self._recent)
            queued = set(self._batch_ids)
            fresh = [i for i in items if i.get("Id") not in recent and i.get("Id") not in queued]
            if not fresh and items:
                # Tiny library (or a server that keeps returning the same list):
                # forget the "no repeats" window instead of stalling forever.
                self._recent.clear()
                already_queued = {item.get("Id") for item in self._batch}
                fresh = [dict(item) for item in items if item.get("Id") not in already_queued]
            self._batch.extend(fresh)
            self._batch_ids.update(item["Id"] for item in fresh)
            self._refilling = False
        if fresh:
            self._available.set()

class PlayerEngine:
    """Coordinates the mpv process, the preload cache and the random queue."""

    def __init__(self, client: JellyfinClient, *, volume: int = 80, debug: bool = False) -> None:
        self.client = client
        self.debug = debug
        self.queue = PlayQueue(client, debug=debug)
        self.preloader = Preloader(client, debug=debug)
        self.mpv = MpvPlayer(volume=volume, debug=debug)
        self._volume = max(0, min(100, int(volume)))
        self._lock = threading.RLock()
        # Serialises complete track transitions: the "end of file" and the
        # "playlist advanced" callbacks must never interleave.
        self._transition = threading.RLock()
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._seq: List[Dict[str, Any]] = []
        self._pos = -1
        self._current: Optional[Dict[str, Any]] = None
        self._pending_next: Optional[Dict[str, Any]] = None
        self._pending_sequential = False
        self._appended = False
        self._current_entry: Optional[int] = None
        self._pending_entry: Optional[int] = None
        self._position = 0.0
        self._duration = 0.0
        self._paused = False
        self._status = "Starting…"
        self._error = ""
        self._deadline: Optional[threading.Timer] = None
        self._preload_token = 0
        self._stopped = threading.Event()

    # --------------------------------------------------------------- public API
    @property
    def volume(self) -> int:
        return self._volume

    def add_listener(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Register a state listener (called from worker threads)."""
        with self._lock:
            self._listeners.append(callback)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "current": dict(self._current) if self._current else None,
                "up_next": dict(self._pending_next) if self._pending_next else None,
                "position": self._position,
                "duration": self._duration,
                "paused": self._paused,
                "volume": self._volume,
                "status": self._status,
                "error": self._error,
                "preload_ready": self._appended,
                "server": self.client.server_url,
                "user": self.client.username,
            }

    def _emit(self) -> None:
        state = self.snapshot()
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(state)
            except Exception as exc:
                print(f"[engine] listener error: {exc}", flush=True)

    def _set_status(self, text: str) -> None:
        with self._lock:
            self._status = text
            self._error = ""

    def _set_error(self, text: str) -> None:
        with self._lock:
            self._error = text

    # --------------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Start mpv, wire the observers and load the first random track."""
        self.preloader.cleanup_partials()
        self.mpv.start()
        self._wire_mpv()
        self._set_status("Loading your music library…")
        self._emit()
        threading.Thread(target=self._bootstrap, name="bootstrap", daemon=True).start()

    def stop(self) -> None:
        """Stop playback and release every resource (safe to call twice)."""
        self._stopped.set()
        self._cancel_deadline()
        self.preloader.stop()
        try:
            self.mpv.shutdown()
        except Exception:
            pass

    def _wire_mpv(self) -> None:
        self.mpv.observe("time-pos", self._on_position)
        self.mpv.observe("duration", self._on_duration)
        self.mpv.observe("pause", self._on_pause)
        self.mpv.observe("volume", self._on_volume)
        self.mpv.observe("playlist-pos", self._on_playlist_pos)
        self.mpv.on_event(self._on_mpv_event)

    def _bootstrap(self) -> None:
        """Load the library (blocking) and start the first random song."""
        if self._stopped.is_set():
            return
        if not self.queue.ensure_batch():
            message = self.queue.last_error or "No audio tracks were found in your Jellyfin library"
            self._set_error(message)
            self._set_status("Retrying in a moment…")
            self._emit()
            self._retry(15.0)
            return
        item = self._pick_item()
        if item is None:
            self._retry(3.0)
            return
        self._begin_item(item, direction="new", reload=True)

    def _retry(self, delay: float) -> None:
        if self._stopped.is_set():
            return
        timer = threading.Timer(delay, self._bootstrap)
        timer.daemon = True
        timer.start()

    # ------------------------------------------------------------ user actions
    def toggle_pause(self) -> None:
        """Pause or resume playback (the engine owns the state; no read first)."""
        with self._lock:
            paused = not self._paused
            self._paused = paused
        try:
            self.mpv.set_pause(paused)
        except PlayerError as exc:
            self._set_error(str(exc))
        self._emit()

    def next_track(self) -> None:
        """Skip to the next random song."""
        with self._lock:
            can_reuse_preload = self._appended and self._pending_next is not None
        if can_reuse_preload:
            try:
                self.mpv.playlist_next()   # mpv moves on to the preloaded entry
                return
            except PlayerError:
                pass
        item, sequential = self._advance_target()
        if item is None:
            return
        self._begin_item(item, direction="next" if sequential else "new", reload=True)

    def previous_track(self) -> None:
        """Restart the current song, or step back to the previous one."""
        if self._position > 5.0:
            self.restart_track()
            return
        with self._lock:
            target = dict(self._seq[self._pos - 1]) if self._pos > 0 else None
        if target is None:
            self.restart_track()
            return
        self._begin_item(target, direction="previous", reload=True)

    def restart_track(self) -> None:
        """Start the current song from the beginning."""
        try:
            self.mpv.seek(0.0, absolute=True)
        except PlayerError as exc:
            self._set_error(str(exc))
        with self._lock:
            self._position = 0.0
        self._emit()

    def seek_relative(self, delta: float) -> None:
        """Jump *delta* seconds forward (or backwards) in the current song."""
        try:
            self.mpv.seek(delta)
        except PlayerError as exc:
            self._set_error(str(exc))

    def seek_absolute(self, position: float) -> None:
        """Jump to an absolute position in the current song."""
        try:
            self.mpv.seek(position, absolute=True)
        except PlayerError as exc:
            self._set_error(str(exc))

    def adjust_volume(self, delta: float) -> None:
        self.set_volume(self._volume + delta)

    def set_volume(self, value: float) -> None:
        """Set the volume (0-100) and remember it for the next session."""
        volume = max(0, min(100, int(round(value))))
        with self._lock:
            self._volume = volume
        try:
            self.mpv.set_volume(volume)
        except PlayerError as exc:
            self._set_error(str(exc))
        self._emit()


    # ------------------------------------------------------------ internal core
    def _begin_item(self, item: Dict[str, Any], *, direction: str, reload: bool) -> None:
        """Make *item* the current track and queue the preload of the next one.

        ``direction`` keeps the history pointer in sync: ``"next"`` steps
        forward, ``"previous"`` steps back and ``"new"`` branches off with a
        freshly picked random track.

        ``reload=False`` is used when mpv already advanced to the preloaded
        entry by itself, so the file must not be re-opened.
        """
        with self._transition:
            self.preloader.cancel()
            self._cancel_deadline()
            with self._lock:
                if (direction == "next" and 0 <= self._pos + 1 < len(self._seq)
                        and self._seq[self._pos + 1].get("Id") == item.get("Id")):
                    self._pos += 1
                elif (direction == "previous" and self._pos > 0
                        and self._seq[self._pos - 1].get("Id") == item.get("Id")):
                    self._pos -= 1
                else:
                    self._seq = self._seq[: self._pos + 1]
                    self._seq.append(item)
                    self._pos = len(self._seq) - 1
                if len(self._seq) > HISTORY_LIMIT * 2:
                    dropped = len(self._seq) - HISTORY_LIMIT
                    self._seq = self._seq[dropped:]
                    self._pos = max(0, self._pos - dropped)
                self._pending_next = None
                self._pending_sequential = False
                self._appended = False
                self._current = item
                self._position = 0.0
                self._duration = JellyfinClient.duration_seconds(item)
                self._error = ""
                self._status = f"Playing {JellyfinClient.display_title(item)}"
            if reload:
                cached = self.preloader.cached_path(item)
                source = str(cached) if cached is not None else self.client.stream_url(item)
                try:
                    self._current_entry = self.mpv.load(source)
                except PlayerError as exc:
                    self._set_error(str(exc))
                    self._current_entry = None
            else:
                # mpv advanced to the preloaded entry by itself.
                with self._lock:
                    self._current_entry = self._pending_entry
                self.mpv.clear_playlist()
            with self._lock:
                self._pending_entry = None
            self._emit()
            self._schedule_preload()

    def _schedule_preload(self, attempt: int = 0) -> None:
        """Download the follow-up track and append it to mpv's playlist."""
        if self._stopped.is_set() or attempt > 15:
            return
        with self._lock:
            if self._pending_next is None:
                if 0 <= self._pos + 1 < len(self._seq):
                    self._pending_next = self._seq[self._pos + 1]
                    self._pending_sequential = True
                else:
                    candidate = self._pick_item()
                    if candidate is None:
                        self._pending_next = None
                    else:
                        self._pending_next = candidate
                        self._pending_sequential = False
                        self._emit()
            item = dict(self._pending_next) if self._pending_next else None
        if item is None:
            self._delayed(PRELOAD_RETRY_SECONDS, lambda: self._schedule_preload(attempt + 1))
            return
        self._preload_token = self.preloader.request(item, self._on_preload_ready)
        self._arm_deadline()

    def _on_preload_ready(self, item: Dict[str, Any], path: Path) -> None:
        """Runs on the preloader thread once the next track is on disk."""
        with self._transition:
            with self._lock:
                if self._stopped.is_set() or self._pending_next is None:
                    return
                if item.get("Id") != self._pending_next.get("Id"):
                    return
            self._cancel_deadline()
            try:
                entry = self.mpv.append(str(path))
            except PlayerError as exc:
                if self.debug:
                    print(f"[engine] could not append the preloaded track: {exc}", flush=True)
                return
            with self._lock:
                self._pending_entry = entry
                self._appended = entry is not None
            self._emit()

    def _arm_deadline(self) -> None:
        """Fall back to streaming if the download is not finished in time."""
        duration = self._duration or 0.0
        delay = PRELOAD_DEADLINE_SECONDS
        if 0 < duration < PRELOAD_DEADLINE_SECONDS * 2:
            delay = max(5.0, duration * 0.5)
        self._cancel_deadline()
        timer = threading.Timer(delay, self._on_preload_deadline)
        timer.daemon = True
        self._deadline = timer
        timer.start()

    def _on_preload_deadline(self) -> None:
        with self._transition:
            with self._lock:
                if self._stopped.is_set() or self._appended or self._pending_next is None:
                    return
                item = dict(self._pending_next)
            try:
                entry = self.mpv.append(self.client.stream_url(item))
            except PlayerError as exc:
                self._set_error(str(exc))
                return
            with self._lock:
                self._pending_entry = entry
                self._appended = entry is not None
            if self.debug:
                print("[engine] preload too slow - streaming the next track instead", flush=True)
            self._emit()

    def _cancel_deadline(self) -> None:
        timer, self._deadline = self._deadline, None
        if timer is not None:
            timer.cancel()

    def _delayed(self, delay: float, callback: Callable[[], None]) -> None:
        if self._stopped.is_set():
            return
        timer = threading.Timer(delay, callback)
        timer.daemon = True
        timer.start()

    def _run_transition(self, callback: Callable[[], None]) -> None:
        """Run a track transition off mpv's reader thread.

        A transition issues ``loadfile``/``playlist-clear`` commands, and only the
        reader thread can deliver their replies - so the work always goes to a
        short-lived thread. Running it inline would stall that thread (and with it
        every UI action) for the whole IPC timeout.
        """
        self._delayed(0.0, callback)

    # --------------------------------------------------------------- selection
    def _advance_target(self):
        """Return ``(item, is_sequential)`` for the song that should play next."""
        with self._lock:
            if 0 <= self._pos + 1 < len(self._seq):
                return dict(self._seq[self._pos + 1]), True
        return self._pick_item(), False

    def _pick_item(self) -> Optional[Dict[str, Any]]:
        item = self.queue.pick(avoid_artist=self._current_artist())
        if item is None:
            self._set_status("Fetching more songs from the server…")
            self._emit()
        return item

    def _current_artist(self) -> str:
        with self._lock:
            return JellyfinClient.artist_key(self._current) if self._current else ""

    def _advance_after_end(self) -> None:
        """A track ended and mpv has nothing queued behind it."""
        if self._stopped.is_set():
            return
        with self._lock:
            pending = dict(self._pending_next) if self._pending_next else None
            sequential = self._pending_sequential
        if pending is not None:
            # The preload was still running: keep the track that was already
            # chosen (served from the cache when it finished in time).
            self._begin_item(pending, direction="next" if sequential else "new", reload=True)
            return
        item, sequential = self._advance_target()
        if item is None:
            self._set_status("Waiting for the server…")
            self._emit()
            self._delayed(PRELOAD_RETRY_SECONDS, self._advance_after_end)
            return
        self._begin_item(item, direction="next" if sequential else "new", reload=True)

    def _advance_if_idle(self) -> None:
        """Skip a track that failed to decode (only when playback really stopped)."""
        if self._stopped.is_set():
            return
        try:
            if not self.mpv.is_idle():
                return
        except PlayerError:
            return
        self._advance_after_end()

    # ------------------------------------------------------------ mpv callbacks
    def _on_position(self, value: Any) -> None:
        try:
            position = float(value)
        except (TypeError, ValueError):
            return
        with self._lock:
            self._position = max(0.0, position)
        self._emit()

    def _on_duration(self, value: Any) -> None:
        try:
            duration = float(value)
        except (TypeError, ValueError):
            return
        with self._lock:
            if duration > 0:
                self._duration = duration
        self._emit()

    def _on_pause(self, value: Any) -> None:
        with self._lock:
            self._paused = bool(value)
        self._emit()

    def _on_volume(self, value: Any) -> None:
        try:
            volume = int(round(float(value)))
        except (TypeError, ValueError):
            return
        with self._lock:
            self._volume = max(0, min(100, volume))
        self._emit()

    def _on_playlist_pos(self, value: Any) -> None:
        """mpv moved on to the preloaded entry: adopt it as the current track."""
        try:
            position = int(value)
        except (TypeError, ValueError):
            return
        if position <= 0:
            return
        with self._lock:
            if not self._appended or self._pending_next is None:
                return
            item = dict(self._pending_next)
            sequential = self._pending_sequential
        if self.debug:
            print(f"[engine] mpv advanced to {JellyfinClient.display_title(item)!r}", flush=True)
        self._run_transition(
            lambda: self._begin_item(item, direction="next" if sequential else "new", reload=False)
        )

    def _on_mpv_event(self, event: str, message: Dict[str, Any]) -> None:
        if event != "end-file":
            return
        reason = message.get("reason")
        if reason == "quit":
            return
        entry = message.get("playlist_entry_id")
        with self._lock:
            appended = self._appended
            expected = self._current_entry
        if entry is not None and expected is not None and entry != expected:
            # Stale notification for an entry we already left behind (mpv can
            # report the new playlist position before this event): ignoring it
            # prevents the same track from being replaced twice.
            if self.debug:
                print(f"[engine] ignoring stale end-file for entry {entry}", flush=True)
            return
        if reason == "error":
            title = JellyfinClient.display_title(self._current or {})
            self._set_error(f"Could not play {title} - skipping")
            self._emit()
            self._delayed(0.5, self._advance_if_idle)
            return
        if not appended:
            # Never block mpv's reader thread: the transition needs replies that
            # only that thread can deliver.
            self._run_transition(self._advance_after_end)







