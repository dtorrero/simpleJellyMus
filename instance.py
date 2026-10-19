#!/usr/bin/env python3
"""Single-instance guard *and* remote control for SimpleJellyMus.

Only one copy of the player may run at a time. The first copy binds a Unix
socket in the per-user runtime directory; later copies find that socket, ask the
running player to come to the front and exit immediately. That is exactly what
the desktop launcher relies on: clicking the menu entry twice never opens a
second window, it just focuses the one that is already there.

The same socket is the player's little control channel, so anything on the
machine can drive it without stealing the keyboard focus: a script, a KDE
shortcut, the Stream Deck. A client sends one command line and reads back one
JSON line:

    play_pause   pause or resume the current song
    next         skip to the next random song
    previous     go back (restarts the song when it has played more than 5 s)
    restart      restart the current song
    status       what is playing right now (title, artist, position, volume...)
    quit         close the player (same as the Quit button)
    focus        bring the window to the front (what a second launch sends)

The socket lives in ``$XDG_RUNTIME_DIR`` when that directory exists (it is
cleaned up on logout), otherwise in the system temporary directory. It is only
accessible by the current user, so no other account can signal the player.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Union

SOCKET_NAME = "simplejellymus.sock"
COMMAND_FOCUS = "focus"
COMMAND_PLAY_PAUSE = "play_pause"
COMMAND_NEXT = "next"
COMMAND_PREVIOUS = "previous"
COMMAND_RESTART = "restart"
COMMAND_STATUS = "status"
COMMAND_QUIT = "quit"
#: ``focus`` is answered by the guard itself; the rest go to the application.
COMMANDS = (COMMAND_FOCUS, COMMAND_PLAY_PAUSE, COMMAND_NEXT, COMMAND_PREVIOUS,
            COMMAND_RESTART, COMMAND_STATUS, COMMAND_QUIT)
CONNECT_TIMEOUT = 1.0
MAX_COMMAND = 64
MAX_REPLY = 4096

CommandHandler = Callable[[str], Optional[Mapping[str, Any]]]


def socket_path() -> Path:
    """Where the single-instance socket lives on this machine."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or ""
    base = Path(runtime) if runtime and Path(runtime).is_dir() else Path(tempfile.gettempdir())
    return base / SOCKET_NAME


class SingleInstance:
    """Detect (and talk to) an already running copy of the app.

    ``acquire()`` returns ``True`` when this process is the one and only
    instance. When another instance is running it asks it to come to the front
    and returns ``False``, so the caller can simply quit.
    """

    def __init__(self, path: Optional[Union[str, os.PathLike]] = None) -> None:
        self.path = Path(path) if path is not None else socket_path()
        self._server: Optional[socket.socket] = None
        self._closed = False

    # -------------------------------------------------------------- public API
    def acquire(self) -> bool:
        """Become the single instance (``True``) or focus the existing one (``False``)."""
        if self._is_running():
            self._signal(COMMAND_FOCUS)
            return False
        # Nobody answers: either nothing runs or a crashed copy left the file.
        self._remove_stale()
        try:
            if self._bind():
                return True
        except OSError as exc:
            # Without a socket the app still works, it just loses the guard.
            print(f"[instance] single-instance guard disabled: {exc}", flush=True)
            return True
        # Another copy won the race between the check and the bind.
        self._signal(COMMAND_FOCUS)
        return False

    def listen(self, on_focus: Callable[[], None],
               on_command: Optional[CommandHandler] = None) -> None:
        """Answer later launches *and* remote commands.

        *on_focus* is called for ``focus`` (the second-launch case); every other
        command is handed to *on_command*, whose returned mapping (if any) is
        sent back to the caller as one JSON line. Both callbacks are called from
        a helper thread, never from the Tk thread.
        """
        if self._server is None:
            return
        threading.Thread(target=self._serve, args=(on_focus, on_command),
                         name="single-instance", daemon=True).start()

    def close(self) -> None:
        """Release the socket and remove the file (idempotent)."""
        if self._closed:
            return
        self._closed = True
        server, self._server = self._server, None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        self._remove_stale()

    def send(self, command: str,
             timeout: float = CONNECT_TIMEOUT) -> Optional[Dict[str, Any]]:
        """Send one command to the running player.

        Returns the reply as a dict, or ``None`` when nothing is listening (the
        player is not running). Handy for scripts and for the Stream Deck:

            reply = SingleInstance().send("status")
            print(reply and reply.get("title"))
        """
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(timeout)
                client.connect(str(self.path))
                client.sendall(command.encode("utf-8") + b"\n")
                chunks: list[bytes] = []
                total = 0
                while total < MAX_REPLY:
                    chunk = client.recv(MAX_REPLY - total)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
        except OSError:
            return None
        payload = b"".join(chunks).decode("utf-8", "replace").strip()
        if not payload:
            return {"ok": True}
        try:
            reply = json.loads(payload.splitlines()[0])
        except ValueError:
            return {"ok": True}
        return reply if isinstance(reply, dict) else {"ok": True}

    def is_running(self) -> bool:
        """True when another copy of the player is listening."""
        return self._is_running()

    # --------------------------------------------------------------- internals
    def _is_running(self) -> bool:
        """True when another process is listening on the socket."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(CONNECT_TIMEOUT)
                probe.connect(str(self.path))
        except OSError:
            return False
        return True

    def _signal(self, command: str) -> None:
        """Send one command to the running instance (best effort)."""
        self.send(command)

    def _remove_stale(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass

    def _bind(self) -> bool:
        """Try to become the listener; ``False`` when somebody else is."""
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.path))
        except OSError:
            server.close()
            return False
        try:
            os.chmod(self.path, 0o600)      # only this user may signal the player
        except OSError:
            pass
        server.listen(8)
        self._server = server
        return True

    def _serve(self, on_focus: Callable[[], None],
               on_command: Optional[CommandHandler] = None) -> None:
        """Accept connections until the app shuts down."""
        server = self._server
        while server is not None and not self._closed:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            try:
                with connection:
                    connection.settimeout(CONNECT_TIMEOUT)
                    command = connection.recv(MAX_COMMAND).decode("utf-8", "replace").strip()
                    reply = self._dispatch(command, on_focus, on_command)
                    if reply is not None:
                        try:
                            connection.sendall(json.dumps(reply).encode("utf-8") + b"\n")
                        except OSError:
                            pass        # the caller hung up: its problem, not ours
            except OSError:
                continue

    def _dispatch(self, command: str, on_focus: Callable[[], None],
                  on_command: Optional[CommandHandler]) -> Optional[Dict[str, Any]]:
        """Run one command and return the reply to send back (``None`` = silent)."""
        if not command:
            return None
        if command == COMMAND_FOCUS:
            try:
                on_focus()
            except Exception as exc:    # never let the listener die
                print(f"[instance] focus callback failed: {exc}", flush=True)
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "command": command}
        if on_command is None:
            return {"ok": False, "error": f"unknown command {command!r}"}
        try:
            reply = on_command(command)
        except Exception as exc:        # a failing command must not kill the listener
            print(f"[instance] command {command!r} failed: {exc}", flush=True)
            return {"ok": False, "error": str(exc)}
        if reply is None:
            return {"ok": True, "command": command}
        return {"command": command, "ok": True, **dict(reply)}
