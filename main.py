#!/usr/bin/env python3
"""SimpleJellyMus - a tiny random Jellyfin music player (audio only).

Usage:
    python3 main.py                 normal start, windowed (asks for the login once)
    python3 main.py --fullscreen    start fullscreen (Esc switches back and forth)
    python3 main.py --windowed      start windowed (the default)
    python3 main.py --login         force the login screen
    python3 main.py --reset-login   forget the saved login and exit
    python3 main.py --debug         verbose Jellyfin/mpv logging

Keys: Space play/pause · N/→ next · P/← previous · ↑/↓ volume · ,/. seek ·
      Esc or F fullscreen · Q (or Ctrl+Q) quit.

Only one copy runs at a time: starting the app again brings the running window
back to the front instead of opening a second one.
"""

from __future__ import annotations

import argparse
import atexit
import queue
import re
import signal
import sys
import threading
import tkinter as tk
from tkinter import messagebox
from typing import Any, Callable, Dict, Optional

import jellyfin
from instance import SingleInstance
from jellyfin import AuthError, JellyfinClient, JellyfinError
from player import PlayerEngine, PlayerError
from ui import (BACKGROUND, CARD, CARD_LIGHT, DANGER, MUTED, TEXT, WINDOW_SIZE, LoginFrame,
                PlayerScreen, pick_font_family)

GEOMETRY_RE = re.compile(r"^(\d{3,5})x(\d{3,5})$")
MIN_WINDOW_SIZE = (900, 620)
WINDOW_CLASS = "simplejellymus"       # WM_CLASS, matches StartupWMClass in the launcher


class Application:
    """Owns the Tk root window and swaps between the login and player screens."""

    def __init__(self, root: tk.Tk, options: argparse.Namespace) -> None:
        self.root = root
        self.options = options
        self.debug = bool(options.debug)
        self.family = pick_font_family()
        self.config: Dict[str, Any] = jellyfin.load_config() or {}
        self.client: Optional[JellyfinClient] = None
        self.engine: Optional[PlayerEngine] = None
        self.frame: Optional[tk.Widget] = None
        self._closing = False
        self._save_job: Optional[str] = None
        self._drain_job: Optional[str] = None
        self._messages: queue.Queue = queue.Queue()

        self.root.title("SimpleJellyMus")
        self.root.configure(bg=BACKGROUND)
        self.root.protocol("WM_DELETE_WINDOW", self.shutdown)
        self._geometry()
        self._drain_job = self.root.after(60, self._drain)
        atexit.register(self.shutdown)

        if options.reset_login or options.login or not self._has_login():
            self.show_login()
        else:
            self.connect()

    # ------------------------------------------------------------------ helpers
    def _geometry(self) -> None:
        """Apply the remembered window mode and size (windowed by default)."""
        self.root.minsize(*MIN_WINDOW_SIZE)
        fullscreen = self.options.fullscreen or (
            self.config.get("window_mode") == "fullscreen" and not self.options.windowed
        )
        if fullscreen:
            self.root.attributes("-fullscreen", True)
        else:
            self.root.geometry(self._windowed_geometry())
            self.root.resizable(True, True)
        # Realise the size now, so the player can size its artwork accordingly.
        self.root.update_idletasks()

    def _windowed_geometry(self) -> str:
        """The remembered window size, validated against the current screen."""
        screen_width = self.root.winfo_screenwidth() or 1920
        screen_height = self.root.winfo_screenheight() or 1080
        match = GEOMETRY_RE.match(str(self.config.get("window_geometry") or ""))
        if not match:
            return WINDOW_SIZE
        width = max(MIN_WINDOW_SIZE[0], min(int(match.group(1)), screen_width))
        height = max(MIN_WINDOW_SIZE[1], min(int(match.group(2)), screen_height))
        return f"{width}x{height}"

    def _persist_window_state(self) -> None:
        """Write the window mode/size (and volume) to the config file."""
        try:
            fullscreen = bool(self.root.attributes("-fullscreen"))
            width, height = self.root.winfo_width(), self.root.winfo_height()
        except tk.TclError:
            return
        config = dict(self.config)
        config["window_mode"] = "fullscreen" if fullscreen else "windowed"
        if not fullscreen and width > 400 and height > 300:
            config["window_geometry"] = f"{width}x{height}"
        if self.engine is not None:
            config["volume"] = int(self.engine.volume)
        self.config = config
        jellyfin.save_config(config)

    def _write_window_state(self) -> None:
        """Debounced after-callback: skip when the app is shutting down."""
        self._save_job = None
        if self._closing:
            return
        self._persist_window_state()

    def _remember_window(self) -> None:
        """Remember the window state shortly after a toggle or a resize."""
        if self._closing:
            return
        if self._save_job is not None:
            try:
                self.root.after_cancel(self._save_job)
            except (tk.TclError, ValueError):
                pass
        self._save_job = self.root.after(600, self._write_window_state)

    def _has_login(self) -> bool:
        return bool(self.config.get("server_url") and self.config.get("access_token"))

    def _swap(self, frame: tk.Widget) -> None:
        previous, self.frame = self.frame, frame
        if previous is not None:
            try:
                previous.destroy()
            except tk.TclError:
                pass
        frame.pack(fill="both", expand=True)

    # -------------------------------------------------------------------- login
    def show_login(self, message: Optional[str] = None) -> None:
        self._swap(
            LoginFrame(
                self.root,
                on_success=self._login_success,
                on_quit=self.shutdown,
                defaults=self.config,
                debug=self.debug,
                message=message,
            )
        )

    def _login_success(self, client: JellyfinClient) -> None:
        self.config = client.to_config({"volume": int(self.config.get("volume", 80))})
        jellyfin.save_config(self.config)
        self.start_player(client)

    # ------------------------------------------------------------------ connect
    def connect(self) -> None:
        """Validate the saved token in the background (auto-login)."""
        frame = tk.Frame(self.root, bg=BACKGROUND)
        inner = tk.Frame(frame, bg=BACKGROUND)
        inner.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(inner, text="SimpleJellyMus", bg=BACKGROUND, fg=TEXT,
                 font=(self.family, 28, "bold")).pack(pady=(0, 8))
        self._connect_status = tk.Label(inner, text="Connecting to the server…", bg=BACKGROUND,
                                        fg=MUTED, font=(self.family, 12), wraplength=620)
        self._connect_status.pack()
        buttons = tk.Frame(inner, bg=BACKGROUND)
        buttons.pack(pady=20)
        self._connect_retry = tk.Button(buttons, text="Retry", command=self.connect, relief="flat",
                                        borderwidth=0, bg=CARD_LIGHT, fg=TEXT, padx=18, pady=8,
                                        font=(self.family, 11, "bold"), cursor="hand2",
                                        state="disabled", disabledforeground=MUTED)
        self._connect_retry.pack(side="left", padx=6)
        tk.Button(buttons, text="Change account", command=self.show_login, relief="flat", borderwidth=0,
                  bg=CARD_LIGHT, fg=TEXT, padx=18, pady=8, font=(self.family, 11, "bold"),
                  cursor="hand2").pack(side="left", padx=6)
        self._swap(frame)
        threading.Thread(target=self._connect_worker, args=(dict(self.config),), daemon=True).start()

    def _connect_worker(self, config: Dict[str, Any]) -> None:
        try:
            client = JellyfinClient.from_config(config, debug=self.debug)
            client.validate_token()
        except AuthError:
            self._post(self.show_login,
                       "The saved login is no longer valid on the server - please sign in again.")
        except JellyfinError as exc:
            self._post(self._connect_failed, str(exc))
        else:
            self._post(self.start_player, client)

    def _connect_failed(self, message: str) -> None:
        if self._closing or self.frame is None:
            return
        try:
            self._connect_status.configure(text=message, fg=DANGER)
            self._connect_retry.configure(state="normal")
        except tk.TclError:
            pass

    # -------------------------------------------------------------------- focus
    def focus_window(self) -> None:
        """Bring the window to the front - safe to call from any thread.

        Used by the single-instance guard: starting the app a second time asks
        the running copy to show itself again.
        """
        self._post(self._raise_window)

    def _raise_window(self) -> None:
        """Raise, un-minimise and focus the window (runs on the Tk thread)."""
        if self._closing:
            return
        root = self.root
        try:
            if root.state() == "iconic":
                root.deiconify()
            root.lift()
            root.attributes("-topmost", True)
            root.after(300, self._drop_topmost)
            root.focus_force()
        except tk.TclError:
            pass

    def _drop_topmost(self) -> None:
        """Undo the short ``-topmost`` flash used to raise the window."""
        if self._closing:
            return
        try:
            self.root.attributes("-topmost", False)
        except tk.TclError:
            pass

    def _post(self, callback: Callable[..., None], *args: Any) -> None:
        """Queue *callback* for the Tk thread (safe from worker threads)."""
        self._messages.put((callback, args))

    def _drain(self) -> None:
        """Run the queued callbacks on the Tk thread."""
        while True:
            try:
                callback, args = self._messages.get_nowait()
            except queue.Empty:
                break
            try:
                callback(*args)
            except Exception as exc:
                print(f"[app] callback error: {exc}", flush=True)
        try:
            if not self._closing and self.root.winfo_exists():
                self._drain_job = self.root.after(60, self._drain)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------- player
    def start_player(self, client: JellyfinClient) -> None:
        """Build the engine, spawn mpv and show the player screen."""
        if self._closing:
            return
        self.client = client
        try:
            self.engine = PlayerEngine(client, volume=int(self.config.get("volume", 80)),
                                       debug=self.debug)
            self.engine.start()
        except PlayerError as exc:
            messagebox.showerror("SimpleJellyMus", str(exc))
            self.shutdown()
            return
        self._swap(
            PlayerScreen(
                self.root,
                engine=self.engine,
                client=client,
                family=self.family,
                on_change_account=self.change_account,
                on_quit=self.shutdown,
                windowed_geometry=self._windowed_geometry(),
                on_window_state_change=self._remember_window,
                debug=self.debug,
            )
        )

    def change_account(self) -> None:
        """Stop playback and show the login screen again."""
        self._persist_window_state()
        engine, self.engine = self.engine, None
        if engine is not None:
            # Free the mpv process and its socket without freezing the UI.
            threading.Thread(target=engine.stop, name="engine-stop", daemon=True).start()
        self.show_login("Signed out - enter your credentials to continue.")

    # ------------------------------------------------------------------- quit
    def request_shutdown(self) -> None:
        """Quit from outside the Tk thread (signal handler or a second copy)."""
        try:
            self._messages.put((self.shutdown, ()))
        except Exception:
            pass

    def shutdown(self) -> None:
        if self._closing:
            return
        engine, self.engine = self.engine, None
        for job in ("_save_job", "_drain_job"):
            handle = getattr(self, job, None)
            if handle is not None:
                try:
                    self.root.after_cancel(handle)
                except (tk.TclError, ValueError):
                    pass
                setattr(self, job, None)
        self._persist_window_state()
        self._closing = True
        if engine is not None:
            engine.stop()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="SimpleJellyMus - random Jellyfin music player (audio only)",
    )
    parser.add_argument("--login", action="store_true", help="always show the login screen")
    parser.add_argument("--reset-login", action="store_true",
                        help="forget the saved login and exit")
    parser.add_argument("--windowed", action="store_true",
                        help="start windowed (the default; overrides a saved fullscreen state)")
    parser.add_argument("--fullscreen", action="store_true",
                        help="start in fullscreen (Esc switches between both)")
    parser.add_argument("--debug", action="store_true", help="verbose Jellyfin/mpv logging")
    return parser.parse_args(argv)


def install_signal_handlers(app: "Application") -> None:
    """Make ``kill``/session logout close down like the Quit button does.

    Without this a ``SIGTERM`` (logout, shutdown, ``kill``) would kill the window
    but leave mpv playing as an orphan and keep the single-instance socket behind.
    """

    def handler(_signum, _frame):
        app.request_shutdown()

    for name in ("SIGTERM", "SIGHUP", "SIGINT"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            signal.signal(number, handler)
        except (ValueError, OSError):
            pass


def main(argv=None) -> int:
    options = parse_args(sys.argv[1:] if argv is None else argv)

    # Only one copy may run: a second start asks the running window to come to
    # the front and exits (this is also what a double-click on the launcher does).
    guard = SingleInstance()
    if not guard.acquire():
        print("SimpleJellyMus is already running - bringing it to the front.")
        return 0

    try:
        if options.reset_login:
            jellyfin.clear_config()
            print(f"Saved login removed from {jellyfin.CONFIG_FILE}")
            return 0

        try:
            root = tk.Tk(className=WINDOW_CLASS)
        except tk.TclError as exc:
            print(f"Cannot open a window: {exc}", file=sys.stderr)
            print("Is a desktop session running and DISPLAY set?", file=sys.stderr)
            return 1

        app = Application(root, options)
        install_signal_handlers(app)
        guard.listen(app.focus_window)
        try:
            root.mainloop()
        except KeyboardInterrupt:
            pass
        return 0
    finally:
        guard.close()


if __name__ == "__main__":
    raise SystemExit(main())


