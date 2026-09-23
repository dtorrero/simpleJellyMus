#!/usr/bin/env python3
"""Drag & drop from the file manager into the player window (optional).

Tk itself cannot receive drops from a file manager. The ``tkdnd`` Tcl extension
can (it speaks the XDND protocol Dolphin, Nautilus and Thunar use), and it comes
either with the ``tkinterdnd2`` Python package or as a system package. Both are
used when they are available; when neither is installed every function here
quietly does nothing, so the player keeps working exactly as before - it simply
has no drop target.

The window only ever listens, it never starts a drag itself, and a drop is always
answered with ``copy``: nothing the file manager hands over is moved or deleted.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence
from urllib.parse import unquote, urlparse

# The drop types offered by the file manager that we accept: a list of files.
DROP_TYPES = ("DND_Files",)

# Where a system-wide tkdnd (e.g. the AUR package) usually ends up. Only needed
# when the tkinterdnd2 package is not installed.
SYSTEM_TKDND_DIRS = (
    "/usr/lib/tkdnd2.9.5", "/usr/lib/tkdnd2.9.4", "/usr/lib/tkdnd2.8",
    "/usr/lib/tkdnd", "/usr/local/lib/tkdnd2.9.5", "/usr/local/lib/tkdnd",
    "/usr/lib/tcltk/tkdnd", "/usr/share/tcltk/tkdnd",
)

COPY = "copy"
INSTALL_HINT = ("no tkdnd found - install drag & drop with:  "
                "pip install --user --break-system-packages tkinterdnd2  "
                "(or the AUR package: yay -S tkdnd)")

_splitlist: Optional[Callable[[str], Sequence[str]]] = None


# --------------------------------------------------------------------------- #
# is it there?
# --------------------------------------------------------------------------- #

def available() -> bool:
    """True when drops can be enabled (tkinterdnd2 or a system tkdnd)."""
    return _tkinterdnd2() is not None or _system_directory() is not None


def _tkinterdnd2() -> Any:
    """The tkinterdnd2 package, or ``None`` when it is not installed."""
    try:
        import tkinterdnd2
    except Exception:
        return None
    return tkinterdnd2


def _system_directory() -> Optional[str]:
    for candidate in SYSTEM_TKDND_DIRS:
        if os.path.isdir(candidate):
            return candidate
    return None


def _require(root: Any) -> Optional[str]:
    """Load the tkdnd Tcl package into *root*; its version, or ``None``."""
    package = _tkinterdnd2()
    if package is not None:
        loader = (getattr(package.TkinterDnD, "require", None)
                  or getattr(package.TkinterDnD, "_require", None))
        if loader is not None:
            try:
                return str(loader(root))
            except Exception as exc:
                print(f"[dnd] tkinterdnd2 could not be loaded ({exc})", flush=True)
    directory = _system_directory()
    if directory is not None:
        try:
            root.tk.call("lappend", "auto_path", directory)
            return str(root.tk.call("package", "require", "tkdnd"))
        except Exception as exc:
            print(f"[dnd] tkdnd could not be loaded from {directory} ({exc})", flush=True)


# --------------------------------------------------------------------------- #
# enabling a window
# --------------------------------------------------------------------------- #

def enable(window: Any, *, on_drop: Callable[[str], Any],
           on_enter: Optional[Callable[[], None]] = None,
           on_leave: Optional[Callable[[], None]] = None) -> Optional[str]:
    """Let files from the file manager be dropped on *window*.

    Registers the window and its toplevel, so a drop lands whether it happens on
    the artwork, on a control or on a card floating above the screen. Returns the
    tkdnd version when drops are active and ``None`` when the extension is not
    installed - the caller then simply has no drop support.
    """
    root = window.winfo_toplevel()
    version = _require(root)
    if version is None:
        return None
    widgets = [root]
    if window is not root:
        widgets.append(window)
    for widget in widgets:
        register(widget, on_drop=on_drop, on_enter=on_enter, on_leave=on_leave)
    return version


def register(widget: Any, *, on_drop: Callable[[str], Any],
             on_enter: Optional[Callable[[], None]] = None,
             on_leave: Optional[Callable[[], None]] = None) -> bool:
    """Make one more widget (a card placed over the screen) a drop target.

    *on_drop* receives what was dropped as a string (pass it to
    :func:`parse_paths`), *on_enter*/*on_leave* are called with no argument. Both
    flavours of the extension - with and without the ``tkinterdnd2`` wrapper -
    are handled here, so callers never see the difference.
    """
    handlers = (("<<Drop>>", _drop_handler(on_drop)),
                ("<<DropEnter>>", _no_argument_handler(on_enter)),
                ("<<DropLeave>>", _no_argument_handler(on_leave)))
    try:
        if hasattr(widget, "drop_target_register"):
            widget.drop_target_register(*DROP_TYPES)
            for sequence, handler in handlers:
                if handler is not None:
                    widget.dnd_bind(sequence, handler)
        else:
            _register_without_wrapper(widget, handlers)
    except Exception as exc:
        print(f"[dnd] {widget} is not a drop target: {exc}", flush=True)
        return False
    return True


def _drop_handler(callback: Callable[[str], Any]) -> Callable[[Any], Any]:
    """Wrap the drop callback so it always sees the data, and answers ``copy``.

    ``tkinterdnd2`` hands over a small event object (with the data on ``.data``)
    while a bare system tkdnd hands over the data itself; the wrapper hides that,
    and returning ``copy`` keeps the file manager from ever moving or deleting a
    file that was dragged onto the window.
    """
    def handler(event_or_data: Any = None) -> Any:
        data = getattr(event_or_data, "data", event_or_data)
        try:
            action = callback(data)
        except Exception as exc:
            print(f"[dnd] a drop could not be handled: {exc}", flush=True)
            action = None
        return action or COPY
    return handler


def _no_argument_handler(callback: Optional[Callable[[], None]]) -> Optional[Callable[..., Any]]:
    """Wrap an enter/leave callback (both are called without arguments)."""
    if callback is None:
        return None

    def handler(*_args: Any) -> Any:
        try:
            callback()
        except Exception as exc:
            print(f"[dnd] drop hint failed: {exc}", flush=True)
        return COPY
    return handler


def _register_without_wrapper(widget: Any, handlers: Sequence[tuple]) -> None:
    """Register a drop target when only a system tkdnd is installed.

    ``tkinterdnd2`` is not there to bind the ``%D`` substitution for us, so the
    two Tcl calls it would make are made here instead (nothing else is needed:
    the data of a file drop is a plain Tcl list of paths).
    """
    widget.tk.call("tkdnd::drop_target", "register", widget._w, DROP_TYPES)
    for sequence, handler in handlers:
        if handler is None:
            continue
        name = widget.register(handler)
        script = f"{name} %D" if sequence == "<<Drop>>" else name
        widget.tk.call("bind", widget._w, sequence, script)


# --------------------------------------------------------------------------- #
# reading what was dropped
# --------------------------------------------------------------------------- #

def parse_entries(data: Any,
                  splitlist: Optional[Callable[[str], Sequence[str]]] = None) -> List[str]:
    """Every entry of a drop as text, local paths and network locations alike.

    A drop arrives as a Tcl list of paths (``%D``), so it is parsed by Tcl itself
    - a file name with a space, a bracket or a backslash in it survives that. An
    already split sequence (a listener that hands the files over one by one) is
    accepted just as well. ``file://`` URIs are decoded and empty entries are
    dropped; entries that are not local paths (``sftp://``, ``smb://``, a stream
    URL) are kept, so the caller can say *why* nothing came of a drop.
    """
    if data is None:
        return []
    if isinstance(data, (list, tuple, set, frozenset)):
        raw = [str(entry) for entry in data]
    else:
        text = str(data)
        if not text.strip():
            return []
        split = splitlist or _tcl_splitlist()
        try:
            raw = [str(entry) for entry in split(text)]
        except Exception:
            raw = [text]
    entries: List[str] = []
    for entry in raw:
        decoded = _decoded_reference(entry)
        if decoded:
            entries.append(decoded)
    return entries


def parse_paths(data: Any,
                splitlist: Optional[Callable[[str], Sequence[str]]] = None) -> List[Path]:
    """The local files of a drop, in the order the file manager listed them.

    Everything that is not a path on this machine (a network location, a stream
    URL, an empty entry) is left out; nothing here touches the disk.
    """
    paths: List[Path] = []
    for entry in parse_entries(data, splitlist):
        if not _is_remote(entry):
            paths.append(Path(entry))
    return paths


def remote_entries(entries: Sequence[str]) -> List[str]:
    """The network locations of a drop (``sftp://...`` and friends)."""
    return [str(entry) for entry in entries or () if _is_remote(str(entry))]


def _decoded_reference(entry: str) -> str:
    """One dropped entry as a plain path or URI (``file://`` decoded)."""
    text = entry.strip().strip('"').strip()
    if not text:
        return ""
    if text.lower().startswith("file://"):
        parsed = urlparse(text)
        if parsed.netloc and parsed.netloc.lower() not in ("", "localhost"):
            return text                  # file://host/... is not on this machine
        return unquote(parsed.path).strip()
    return text


def _is_remote(entry: str) -> bool:
    """True for something that is not a path on this machine."""
    return "://" in entry and not entry.startswith("/")


def _tcl_splitlist() -> Callable[[str], Sequence[str]]:
    """Tcl's own list parser (a drop is a Tcl list), created once."""
    global _splitlist
    if _splitlist is None:
        try:
            import tkinter
            _splitlist = tkinter.Tcl().splitlist
        except Exception:            # no Tcl at all: fall back to plain text
            _splitlist = lambda text: text.split("\n")      # noqa: E731
    return _splitlist

