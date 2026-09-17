#!/usr/bin/env python3
"""Single-instance guard for SimpleJellyMus.

Only one copy of the player may run at a time. The first copy binds a Unix
socket in the per-user runtime directory; later copies find that socket, ask the
running player to come to the front and exit immediately. That is exactly what
the desktop launcher relies on: clicking the menu entry twice never opens a
second window, it just focuses the one that is already there.

The socket lives in ``$XDG_RUNTIME_DIR`` when that directory exists (it is
cleaned up on logout), otherwise in the system temporary directory. It is only
accessible by the current user, so no other account can signal the player.
"""

from __future__ import annotations

import os
import socket
import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional, Union

SOCKET_NAME = "simplejellymus.sock"
COMMAND_FOCUS = "focus"
CONNECT_TIMEOUT = 1.0


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

    def listen(self, on_focus: Callable[[], None]) -> None:
        """Answer later launches; *on_focus* is called from a helper thread."""
        if self._server is None:
            return
        threading.Thread(target=self._serve, args=(on_focus,), name="single-instance",
                         daemon=True).start()

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
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(CONNECT_TIMEOUT)
                client.connect(str(self.path))
                client.sendall(command.encode("utf-8") + b"\n")
        except OSError:
            pass

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

    def _serve(self, on_focus: Callable[[], None]) -> None:
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
                    command = connection.recv(64).decode("utf-8", "replace").strip()
            except OSError:
                continue
            if command == COMMAND_FOCUS:
                try:
                    on_focus()
                except Exception as exc:    # never let the listener die
                    print(f"[instance] focus callback failed: {exc}", flush=True)
