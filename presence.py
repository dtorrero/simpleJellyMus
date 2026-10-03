#!/usr/bin/env python3
"""Discord Rich Presence: what is playing, shown in the Discord profile.

The player already knows everything worth showing, so this module only has to
tell Discord about it - over the local IPC socket the Discord client creates
(``discord-ipc-N``), which is the same channel the official rich-presence
libraries use. No extra process, no network connection, no new dependency.

How it behaves
--------------

* **Auto-detecting.** :func:`find_discord_socket` looks for the socket wherever
  the native, the Flatpak and the Snap builds are known to put it. Discord not
  running (or started later) is never an error: the presence stays quiet and
  keeps looking.
* **Never in the way.** Everything happens on one daemon worker thread, and the
  engine listener only stores the newest snapshot - Discord being slow, hung or
  absent can never delay a track change or a key press.
* **Text first, artwork second.** The track is published the moment it starts
  (title, artist, album, elapsed and remaining time). Real cover art needs an
  upload first (see :mod:`discord_cover`), so until that has landed the
  application's own static image is used and is replaced by the artwork when it
  is ready. A failed upload costs the picture, never the track.
* **A music player, not an app name.** Discord's status text - the line others
  read in their member list - shows the application name unless the activity
  says otherwise, so :func:`activity_payload` sends ``status_display_type`` (2 =
  the ``details`` field, the track title) next to ``type`` (2 = "Listening
  to ..."). Clients that predate those fields simply ignore them.

Client id
---------

Discord identifies an application by its client id, which decides the name and
the images the profile shows. SimpleJellyMus therefore borrows the id that
`jellyfin-rpc <https://github.com/Radiicall/jellyfin-rpc>`_ ships with
(:data:`DEFAULT_CLIENT_ID`), so the presence works without any Discord setup.
``--discord-client-id`` (or ``discord.client_id`` in the config file) publishes
under your own application with your own artwork instead - see the README.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import struct
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

# The application the presence publishes under: the client id jellyfin-rpc ships
# in its binary, so the profile reads "Jellyfin" and shows its artwork until you
# register your own Discord application and pass its id.
DEFAULT_CLIENT_ID = "1053747938519679018"

# Asset keys, i.e. the names of the images uploaded for that application in the
# Discord Developer Portal. They are only names: Discord resolves them on its
# side, and a key that was never uploaded renders nothing at all - it is not an
# error, so a missing image degrades to no image.
DEFAULT_ASSETS = {"logo": "logo", "playing": "playing", "paused": "paused"}

# Discord is rate limited about SET_ACTIVITY; one frame every two seconds is far
# below anything it complains about, and only real changes are ever sent.
MIN_UPDATE_INTERVAL = 2.0
# A jump larger than this many seconds is a seek and worth an update - plain
# progress is not, Discord advances the clock from the timestamps itself.
POSITION_TOLERANCE = 5.0
# How long the presence waits before it looks for a socket again.
REDISCOVER_INTERVAL = 15.0
# Discord cuts both text lines at 128 characters; trimming here at least ends on
# a whole word and an ellipsis instead of mid-word.
MAX_TEXT = 128

# IPC opcodes (a little-endian uint32 ahead of every frame).
OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2
OP_PING = 3
OP_PONG = 4


# --------------------------------------------------------------------------- #
# finding Discord
# --------------------------------------------------------------------------- #

def _socket_directories() -> List[Path]:
    """Every directory the known Discord builds leave their IPC socket in."""
    candidates: List[Path] = []
    seen = set()

    def add(path: Optional[Path]) -> None:
        if path is not None and path not in seen:
            seen.add(path)
            candidates.append(path)

    for value in (os.environ.get("XDG_RUNTIME_DIR"), os.environ.get("TMPDIR"), "/tmp"):
        if value:
            add(Path(value))
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        # The Snap build gets a directory of its own inside the runtime dir and
        # the Flatpak build a private one below it; the native builds (Arch,
        # Debian, Ubuntu packages) put the socket straight into the runtime dir,
        # which the loop above already covers.
        add(Path(runtime) / "snap.discord")
        add(Path(runtime) / "app" / "com.discordapp.Discord")
        flatpak = Path(runtime) / ".flatpak"
        if flatpak.is_dir():
            try:
                for child in sorted(flatpak.iterdir()):
                    if child.is_dir():
                        add(child)
            except OSError:
                pass
    return candidates


def find_discord_socket(directories: Optional[Iterable[Any]] = None) -> Optional[str]:
    """Path of a Discord IPC socket, or ``None`` while Discord is not running.

    *directories* replaces the search path - the self-test uses that to point the
    lookup at a directory it controls instead of the real Discord.
    """
    places = ([Path(entry) for entry in directories] if directories is not None
              else _socket_directories())
    for directory in places:
        for index in range(10):
            path = directory / f"discord-ipc-{index}"
            try:
                mode = path.stat().st_mode
            except OSError:
                continue
            if stat.S_ISSOCK(mode):
                return str(path)
    return None


# --------------------------------------------------------------------------- #
# the activity itself
# --------------------------------------------------------------------------- #

def _frame(opcode: int, payload: Any) -> bytes:
    """One IPC frame: opcode and length as little-endian uint32, then JSON."""
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return struct.pack("<II", opcode, len(data)) + data


def _text(value: Any) -> str:
    """*value* as a single line, shortened to what Discord will show anyway."""
    text = " ".join(str(value or "").split())
    if len(text) <= MAX_TEXT:
        return text
    return text[:MAX_TEXT - 2].rstrip() + "…"


def _seconds(value: Any) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def display_title(item: Dict[str, Any]) -> str:
    """Title of an item - Jellyfin and dropped files both use ``Name``."""
    return _text(item.get("Name") or "Unknown title")


def display_artist(item: Dict[str, Any]) -> str:
    """The artist line, exactly like the window shows it."""
    artists = item.get("Artists") or []
    if artists:
        return _text(", ".join(str(artist) for artist in artists))
    return _text(item.get("AlbumArtist") or "")


def activity_payload(state: Dict[str, Any], *, assets: Optional[Dict[str, str]] = None,
                     cover_url: Optional[str] = None,
                     activity_type: int = 0,
                     status_display_type: int = 0) -> Optional[Dict[str, Any]]:
    """The ``activity`` object for one engine snapshot (``None`` = nothing to show).

    ``details`` is the track, ``state`` is artist and album, and the timestamps
    let Discord count the elapsed and the remaining time itself instead of us
    sending an update every second. Nothing in here can ever contain the access
    token: the only image URL that can appear is the one :mod:`discord_cover`
    uploaded to an image host.

    ``assets`` are the application's image keys; without it the module defaults
    (:data:`DEFAULT_ASSETS`) are used, and an empty dict means "no images".

    ``activity_type`` and ``status_display_type`` are the two fields that decide
    how Discord *words* all of this: the first picks the verb (2 = "Listening
    to ...", which also draws the timestamps as a time bar instead of a
    countdown), the second picks which field the status text shows (0 = the
    application name, 1 = ``state``, 2 = ``details``, the track title).
    """
    item = (state or {}).get("current")
    if not isinstance(item, dict) or not item:
        return None
    paused = bool(state.get("paused"))
    duration = _seconds(state.get("duration"))
    position = _seconds(state.get("position"))
    artist = display_artist(item)
    album = _text(item.get("Album") or "")
    if artist and album:
        second_line = f"{artist} — {album}"
    else:
        second_line = artist or album

    activity: Dict[str, Any] = {"details": display_title(item)}
    if second_line:
        activity["state"] = second_line
    if not paused and duration > 0:
        # While paused no timestamps are sent at all: with a start timestamp
        # alone Discord would keep counting up as if the song were running.
        start = int(time.time()) - int(min(position, duration))
        activity["timestamps"] = {"start": start, "end": start + int(round(duration))}

    art: Dict[str, str] = {}
    keys = DEFAULT_ASSETS if assets is None else assets
    if keys:
        image = cover_url or keys.get("logo")
        if image:
            art["large_image"] = image
            art["large_text"] = album or second_line or activity["details"]
        badge = keys.get("paused" if paused else "playing")
        if badge:
            art["small_image"] = badge
            art["small_text"] = "Paused" if paused else "Playing"
    if art:
        activity["assets"] = art
    if activity_type:
        # "Playing" (0) is the default and is left out completely, because that
        # is what every rich-presence client sends. Anything else is passed on
        # as a wish, not a promise.
        activity["type"] = int(activity_type)
    if status_display_type:
        # 0 (the application name) is Discord's own default, so it is left out
        # too; 2 puts the track title into the status text.
        activity["status_display_type"] = int(status_display_type)
    return activity


# --------------------------------------------------------------------------- #
# the connection
# --------------------------------------------------------------------------- #

class DiscordIPC:
    """One Discord IPC connection: handshake, activities, pings.

    The protocol is small enough to speak directly - a handshake, an
    ``SET_ACTIVITY`` frame per change and a pong for every ping Discord sends.
    Everything here blocks, so it is only ever used from the worker thread of
    :class:`DiscordPresence`.
    """

    def __init__(self, client_id: str, socket_path: Any, *, timeout: float = 5.0,
                 debug: bool = False) -> None:
        self.client_id = str(client_id)
        self.socket_path = str(socket_path)
        self.timeout = float(timeout)
        self.debug = bool(debug)
        self._socket: Optional[socket.socket] = None

    def _log(self, message: str) -> None:
        if self.debug:
            print(f"[discord] {message}", flush=True)

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def connect(self) -> bool:
        """Open the socket and handshake. ``False`` means "no Discord for us"."""
        self.close()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self.socket_path)
            sock.sendall(_frame(OP_HANDSHAKE, {"v": 1, "client_id": self.client_id}))
        except OSError as exc:
            self._log(f"cannot connect to {self.socket_path}: {exc}")
            try:
                sock.close()
            except OSError:
                pass
            return False
        self._socket = sock
        status = self._pump(time.monotonic() + self.timeout)
        if status in ("error", "closed"):
            self._log("Discord refused the handshake (unknown client id?)")
            self.close()
            return False
        self._log(f"connected to {self.socket_path} as {self.client_id}")
        return True

    def close(self) -> None:
        """Drop the connection (idempotent)."""
        sock, self._socket = self._socket, None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def set_activity(self, activity: Optional[Dict[str, Any]]) -> bool:
        """Publish *activity* - or clear the presence when it is ``None``.

        ``False`` means the frame could not be sent at all (the connection is
        gone). Discord refusing a payload while the connection is fine is only
        worth a line in the debug log.
        """
        if self._socket is None:
            return False
        nonce = str(uuid.uuid4())
        payload = {"cmd": "SET_ACTIVITY",
                   "args": {"pid": os.getpid(), "activity": activity},
                   "nonce": nonce}
        try:
            self._socket.sendall(_frame(OP_FRAME, payload))
        except OSError as exc:
            self._log(f"cannot send the activity: {exc}")
            self.close()
            return False
        # Wait for the answer (Discord replies to every frame), but never long:
        # this runs on the way out as well, when the window wants to disappear.
        self._pump(time.monotonic() + min(self.timeout, 1.5), expect_nonce=nonce)
        return self._socket is not None

    # ------------------------------------------------------------------ plumbing
    def _pump(self, deadline: float, expect_nonce: Optional[str] = None) -> str:
        """Read frames until *deadline*: 'reply', 'error', 'timeout' or 'closed'."""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self._socket is None:
                return "timeout"
            try:
                self._socket.settimeout(remaining)
                opcode, payload = self._receive()
            except socket.timeout:
                return "timeout"
            except OSError as exc:
                self._log(f"connection lost: {exc}")
                self.close()
                return "closed"
            if opcode == OP_PING:
                try:
                    self._socket.sendall(_frame(OP_PONG, payload))
                except OSError:
                    self.close()
                    return "closed"
                continue
            if opcode == OP_CLOSE:
                self._log("Discord closed the connection")
                self.close()
                return "closed"
            if opcode != OP_FRAME:
                continue                # our own pong, or something unknown
            event = str(payload.get("evt") or "").upper()
            if event == "ERROR":
                detail: Any = payload.get("data")
                if isinstance(detail, dict):
                    detail = detail.get("message") or detail.get("code")
                self._log(f"Discord refused the frame: {detail or payload}")
                return "error"
            if expect_nonce is None or payload.get("nonce") == expect_nonce:
                return "reply"          # the READY frame, or the answer to ours
            # Anything else (join requests, voice events) is simply not ours.

    def _receive(self):
        """Read one frame: ``(opcode, payload)`` with the payload as a dict."""
        opcode, length = struct.unpack("<II", self._read(8))
        payload = self._read(length) if length else b""
        try:
            data = json.loads(payload.decode("utf-8")) if payload else None
        except ValueError:
            data = None
        return opcode, (data if isinstance(data, dict) else {})

    def _read(self, count: int) -> bytes:
        sock = self._socket
        if sock is None:
            raise ConnectionResetError("Discord closed the connection")
        chunks = bytearray()
        while len(chunks) < count:
            block = sock.recv(count - len(chunks))
            if not block:
                raise ConnectionResetError("Discord closed the connection")
            chunks += block
        return bytes(chunks)


# --------------------------------------------------------------------------- #
# the publisher
# --------------------------------------------------------------------------- #

class DiscordPresence:
    """Publishes what the engine plays to Discord, from one worker thread.

    The engine calls :meth:`on_state` (from mpv's and its own threads); that
    method does nothing but store the snapshot and wake the worker, so a slow,
    hung or missing Discord can never delay playback or a key press. Call
    :meth:`stop` to clear the presence again.

    ``cover`` is a :class:`discord_cover.CoverUploader` and ``cover_provider`` a
    callable that returns the local cover file of an item; both are optional and
    only used when cover art was asked for.

    ``activity_type`` and ``status_display_type`` are passed straight through to
    :func:`activity_payload` on every update (2/2 = "Listening to <track title>",
    the defaults the config file hands over).
    """

    def __init__(self, *, client_id: str = DEFAULT_CLIENT_ID,
                 assets: Optional[Dict[str, str]] = None,
                 activity_type: int = 0,
                 status_display_type: int = 0,
                 socket_path: Optional[Any] = None,
                 cover: Optional[Any] = None,
                 cover_provider: Optional[Callable[[Dict[str, Any]], Optional[Any]]] = None,
                 interval: float = MIN_UPDATE_INTERVAL,
                 tolerance: float = POSITION_TOLERANCE,
                 rediscover: float = REDISCOVER_INTERVAL,
                 debug: bool = False) -> None:
        self.client_id = str(client_id or "").strip()
        self.assets = dict(DEFAULT_ASSETS if assets is None else assets)
        self.activity_type = int(activity_type or 0)
        self.status_display_type = int(status_display_type or 0)
        self.debug = bool(debug)
        # Frames actually sent, and activities built (the self-test reads them).
        self.frames = 0
        self.updates = 0
        self._fixed_socket = str(socket_path) if socket_path else None
        self._cover = cover
        self._cover_provider = cover_provider
        self._interval = max(0.05, float(interval))
        self._tolerance = max(0.1, float(tolerance))
        self._rediscover = max(0.2, float(rediscover))
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pending: Optional[Dict[str, Any]] = None
        self._ipc: Optional[DiscordIPC] = None
        self._next_lookup = 0.0
        self._next_send = 0.0
        self._sent: Optional[tuple] = None
        self._sent_position = 0.0
        self._last_state: Optional[Dict[str, Any]] = None
        self._cover_url: Optional[str] = None
        self._cover_track: Optional[str] = None    # the track *cover_url* belongs to
        self._cover_tried: Optional[str] = None    # the track whose artwork failed

    def _log(self, message: str) -> None:
        if self.debug:
            print(f"[discord] {message}", flush=True)

    @property
    def connected(self) -> bool:
        """True while the worker holds a usable Discord connection."""
        with self._lock:
            ipc = self._ipc
        return ipc is not None and ipc.connected

    # ------------------------------------------------------------------ lifetime
    def start(self) -> None:
        """Start the worker (idempotent, and a no-op without a client id)."""
        if not self.client_id:
            self._log("no client id - rich presence stays off")
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="discord-presence",
                                            daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 1.5) -> None:
        """Clear the presence and stop the worker (safe to call more than once).

        Returns quickly even when Discord stopped answering: quitting the player
        must not wait for a picture to disappear.
        """
        with self._lock:
            thread, self._thread = self._thread, None
        self._stop.set()
        self._wake.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        with self._lock:
            stuck, self._ipc = self._ipc, None
        if stuck is not None:
            # The worker did not finish in time (Discord is not answering).
            # Closing the socket is enough: Discord forgets the presence by
            # itself as soon as the process is gone.
            stuck.close()

    # ------------------------------------------------------------------ updating
    def on_state(self, state: Dict[str, Any]) -> None:
        """Engine listener: keep the newest snapshot and wake the worker.

        Deliberately tiny - this runs on the engine's threads, and a listener
        must never slow down or break playback.
        """
        try:
            with self._lock:
                self._pending = dict(state) if isinstance(state, dict) else None
            self._wake.set()
        except Exception:
            pass

    def _run(self) -> None:
        """The worker: connect, publish changes, reconnect, clear on exit."""
        while not self._stop.is_set():
            if self._ipc is None:
                if not self._connect():
                    self._pause(1.0)
                    continue
                self._sent = None      # a fresh connection knows nothing yet
                with self._lock:
                    if self._pending is None:
                        # Discord restarted: put the current track back at once.
                        self._pending = self._last_state
            state = self._take()
            if state is None:
                self._pause(0.25)
                continue
            try:
                self._publish(state)
            except Exception as exc:   # the worker must never die of an update
                self._log(f"update failed: {exc}")
                self._pause(0.5)
        self._finish()

    def _pause(self, seconds: float) -> None:
        """Wait for new work or for the timeout, whichever comes first."""
        self._wake.wait(seconds)
        self._wake.clear()

    def _take(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            state, self._pending = self._pending, None
        return state

    def _connect(self) -> bool:
        """Look for Discord (at most every *rediscover* seconds) and connect."""
        now = time.monotonic()
        if now < self._next_lookup:
            return False
        self._next_lookup = now + self._rediscover
        path = self._fixed_socket or find_discord_socket()
        if not path:
            self._log("no Discord IPC socket found (is the Discord client running?)")
            return False
        ipc = DiscordIPC(self.client_id, path, debug=self.debug)
        if not ipc.connect():
            return False
        with self._lock:
            self._ipc = ipc
        return True

    def _finish(self) -> None:
        """Clear the presence on the way out - Discord keeps it otherwise."""
        with self._lock:
            ipc, self._ipc = self._ipc, None
        if ipc is None:
            return
        ipc.set_activity(None)
        ipc.close()

    # ----------------------------------------------------------------- publishing
    @staticmethod
    def _track_key(state: Dict[str, Any]) -> str:
        item = (state or {}).get("current") or {}
        return str(item.get("Id") or item.get("_local_path") or "")

    def _signature(self, state: Dict[str, Any]) -> tuple:
        """Everything Discord would draw differently about a *track*.

        Plain progress is not in here on purpose: Discord advances the clock from
        the timestamps itself, so an update is only needed for another track, a
        pause or another duration - a seek is caught by comparing the position
        with the one that was last sent (see :meth:`_publish`).
        """
        track = self._track_key(state)
        if not track:
            return ("idle",)
        return (track, bool(state.get("paused")), round(_seconds(state.get("duration"))))

    def _publish(self, state: Dict[str, Any]) -> None:
        signature = self._signature(state)
        position = _seconds(state.get("position"))
        if (signature == self._sent
                and abs(position - self._sent_position) <= self._tolerance):
            return
        remaining = self._next_send - time.monotonic()
        if remaining > 0:
            # Rate limited: wait, and publish whatever arrived in the meantime -
            # a skipped track must not flash up as the one before it.
            if self._stop.wait(remaining):
                return
            newest = self._take()
            if newest is not None:
                state, signature = newest, self._signature(newest)
                if (signature == self._sent
                        and abs(_seconds(newest.get("position")) - self._sent_position)
                        <= self._tolerance):
                    return
        self._send(state, signature)
        self._resolve_cover(state)

    def _send(self, state: Dict[str, Any], signature: tuple) -> None:
        with self._lock:
            ipc = self._ipc
        if ipc is None:
            return
        activity = activity_payload(state, assets=self.assets,
                                    cover_url=self._cover_url_for(state),
                                    activity_type=self.activity_type,
                                    status_display_type=self.status_display_type)
        self.updates += 1
        self._next_send = time.monotonic() + self._interval
        if not ipc.set_activity(activity):
            self._log("the connection is gone - reconnecting")
            with self._lock:
                self._ipc = None
            self._next_lookup = 0.0        # try again at once, then throttle
            self._sent = None
            return
        self._sent = signature
        self._sent_position = _seconds(state.get("position"))
        self._last_state = state
        self.frames += 1

    def _cover_url_for(self, state: Dict[str, Any]) -> Optional[str]:
        """The uploaded artwork URL - but only while it belongs to this track."""
        if self._cover is None or self._cover_url is None:
            return None
        return self._cover_url if self._cover_track == self._track_key(state) else None

    def _resolve_cover(self, state: Dict[str, Any]) -> None:
        """Upload the artwork of the current track, then publish it.

        Runs on the worker thread, after the track itself is already on Discord,
        so a slow or broken image host only costs the picture. Every track is
        tried exactly once; a failure simply keeps the static image.
        """
        if self._cover is None or self._cover_provider is None:
            return
        track = self._track_key(state)
        if not track or track in (self._cover_track, self._cover_tried):
            return
        item = state.get("current") or {}
        path: Optional[Any] = None
        try:
            path = self._cover_provider(item)
        except Exception as exc:
            self._log(f"cover lookup failed: {exc}")
        url: Optional[str] = None
        if path is not None:
            try:
                url = self._cover.url_for(path, is_cancelled=self._stop.is_set)
            except Exception as exc:
                self._log(f"cover upload failed: {exc}")
        if not url:
            self._cover_tried = track
            self._log("no artwork published for this track - the static image stays")
            return
        self._cover_url = url
        self._cover_track = track
        self._sent = None                  # publish the same track again, with art
        with self._lock:
            if self._pending is None:
                self._pending = state
        self._wake.set()
