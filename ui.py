#!/usr/bin/env python3
"""Tkinter user interface for SimpleJellyMus: login screen and player screen.

The UI only ever renders audio metadata (cover art, title, artist, album),
progress and transport controls; it never deals with video.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import font as tkfont
from typing import Any, Callable, Dict, Optional

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageTk

from jellyfin import JellyfinClient, JellyfinError

BACKGROUND = "#151718"
CARD = "#1d2124"
CARD_LIGHT = "#2b2f33"
TEXT = "#ECEDEE"
MUTED = "#9BA1A6"
ACCENT = "#0A7EA4"
ACCENT_LIGHT = "#3aa7c9"
DANGER = "#e0533d"

# Default windowed size; also used when leaving fullscreen.
WINDOW_SIZE = "1280x800"

FONT_CANDIDATES = ("Noto Sans", "DejaVu Sans", "Liberation Sans", "FreeSans", "Hack", "Arial")

# Typography and layout budget. The song information lives in a fixed-size box:
# the fonts shrink (never below the *_MIN sizes) and the text is finally
# ellipsized, so a long name can never push the artwork or the controls around.
TITLE_SIZE, TITLE_MIN_SIZE, TITLE_LINES = 25, 15, 2
ARTIST_SIZE, ARTIST_MIN_SIZE = 15, 11
ALBUM_SIZE, ALBUM_MIN_SIZE = 11, 9
STATUS_SIZE = 10
MIN_COVER, MAX_COVER = 200, 560
CHROME_MARGIN = 26            # breathing room kept around the fixed blocks
FOOTER_PAD = (28, 18)         # horizontal / bottom padding of the footer


def pick_font_family() -> str:
    """Return the first available nice-looking font family."""
    try:
        available = set(tkfont.families())
    except tk.TclError:
        return "TkDefaultFont"
    for family in FONT_CANDIDATES:
        if family in available:
            return family
    return "TkDefaultFont"


def _pil_font(size: int) -> Any:
    for name in ("DejaVuSans-Bold.ttf", "NotoSans-Bold.ttf", "LiberationSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def rounded_cover(image: Image.Image, size: int, radius: int = 26) -> Image.Image:
    """Cover-fit *image* into a rounded square canvas."""
    fitted = ImageOps.fit(image.convert("RGB"), (size, size), method=Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    rounded = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rounded.paste(fitted, (0, 0), mask)
    return rounded


def placeholder_cover(title: str, subtitle: str, size: int) -> Image.Image:
    """Draw a simple "no artwork" tile."""
    image = Image.new("RGB", (size, size), CARD_LIGHT)
    draw = ImageDraw.Draw(image)
    for step in range(size):
        shade = 29 + int(18 * step / max(1, size))
        draw.line((0, step, size, step), fill=(shade, shade + 2, shade + 4))
    letter = (title or "?").strip()[:1].upper() or "?"
    draw.text((size * 0.5, size * 0.44), letter, font=_pil_font(int(size * 0.36)),
              fill=ACCENT_LIGHT, anchor="mm")
    draw.text((size * 0.5, size * 0.72), subtitle[:34], font=_pil_font(int(size * 0.055)),
              fill=MUTED, anchor="mm")
    return image

class ThreadSafeFrame(tk.Frame):
    """Base frame that can safely receive calls from worker threads.

    Tk objects may only be touched by the thread that runs the event loop, and
    calling ``after()`` from another thread is unreliable (it can raise
    "main thread is not in main loop"). Worker threads therefore push
    ``(callback, args)`` pairs into a queue which the Tk thread drains.
    """

    def __init__(self, master: tk.Misc, **kwargs: Any) -> None:
        super().__init__(master, **kwargs)
        self._messages: queue.Queue = queue.Queue()
        self._drain_job: Optional[str] = self.after(60, self._drain)

    def _post(self, callback: Callable[..., None], *args: Any) -> None:
        """Queue *callback* so that it runs on the Tk thread."""
        self._messages.put((callback, args))

    def _drain(self) -> None:
        while True:
            try:
                callback, args = self._messages.get_nowait()
            except queue.Empty:
                break
            try:
                callback(*args)
            except Exception as exc:  # one bad update must not kill the UI
                print(f"[ui] callback error: {exc}", flush=True)
        try:
            if self.winfo_exists():
                self._drain_job = self.after(60, self._drain)
        except tk.TclError:
            pass

    def destroy(self) -> None:
        """Cancel the pending drain before the widget disappears (idempotent)."""
        if getattr(self, "_gone", False):
            return
        self._gone = True
        if self._drain_job is not None:
            try:
                self.after_cancel(self._drain_job)
            except (tk.TclError, ValueError):
                pass
            self._drain_job = None
        try:
            super().destroy()
        except tk.TclError:
            pass


class LoginFrame(ThreadSafeFrame):
    """First-run (and re-login) screen: server URL, username, password."""

    def __init__(self, master: tk.Misc, *, on_success: Callable[[JellyfinClient], None],
                 on_quit: Callable[[], None], defaults: Optional[Dict[str, Any]] = None,
                 debug: bool = False, message: Optional[str] = None) -> None:
        super().__init__(master, bg=BACKGROUND)
        self._on_success = on_success
        self._on_quit = on_quit
        self._debug = debug
        self._defaults = defaults or {}
        self._family = pick_font_family()
        self._busy = False
        self._client: Optional[JellyfinClient] = None

        card = tk.Frame(self, bg=CARD, padx=52, pady=44, highlightthickness=1,
                        highlightbackground=CARD_LIGHT)
        card.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(card, text="SimpleJellyMus", bg=CARD, fg=TEXT,
                 font=(self._family, 30, "bold")).pack(anchor="w")
        tk.Label(card, text="Random Jellyfin music player · audio only", bg=CARD, fg=MUTED,
                 font=(self._family, 12)).pack(anchor="w", pady=(2, 24))

        default_url = self._defaults.get("server_url") or ""
        self._url_entry = self._add_field(card, "Server URL", default_url or "http://",
                                          "e.g. http://192.168.1.10:8096")
        self._user_entry = self._add_field(card, "Username", self._defaults.get("username", ""),
                                           "your Jellyfin user")
        self._password_entry = self._add_field(card, "Password", "", "never stored", show="•")

        buttons = tk.Frame(card, bg=CARD)
        buttons.pack(fill="x", pady=(22, 0))
        self._test_button = self._make_button(buttons, "Test connection", self._test_connection, False)
        self._test_button.pack(side="left")
        self._login_button = self._make_button(buttons, "Log in", self._submit, True)
        self._login_button.pack(side="right")
        self._quit_button = self._make_button(card, "Quit", self._on_quit, False)
        self._quit_button.pack(anchor="w", pady=(18, 0))

        self._status = tk.Label(card, text=message or "", bg=CARD, fg=ACCENT_LIGHT if message else MUTED,
                                font=(self._family, 11), wraplength=520, justify="left")
        self._status.pack(anchor="w", pady=(16, 0))

        self._password_entry.bind("<Return>", lambda _event: self._submit())
        self._user_entry.bind("<Return>", lambda _event: self._submit())
        self._url_entry.bind("<Return>", lambda _event: self._submit())
        self.after(120, self._url_entry.focus_set)

    # ------------------------------------------------------------------ widgets
    def _add_field(self, parent: tk.Misc, label: str, value: str, hint: str = "",
                   show: Optional[str] = None) -> tk.Entry:
        tk.Label(parent, text=label, bg=CARD, fg=MUTED,
                 font=(self._family, 10, "bold")).pack(anchor="w", pady=(12, 4))
        entry = tk.Entry(parent, bg=CARD_LIGHT, fg=TEXT, insertbackground=TEXT, relief="flat",
                         font=(self._family, 13), width=38, show=show or "")
        entry.insert(0, value)
        entry.pack(anchor="w", ipady=7, ipadx=8)
        if hint:
            tk.Label(parent, text=hint, bg=CARD, fg=MUTED,
                     font=(self._family, 9)).pack(anchor="w", pady=(3, 0))
        return entry

    def _make_button(self, parent: tk.Misc, text: str, command: Callable[[], None],
                     accent: bool) -> tk.Button:
        background = ACCENT if accent else CARD_LIGHT
        active = ACCENT_LIGHT if accent else CARD
        return tk.Button(parent, text=text, command=command, relief="flat", borderwidth=0,
                         bg=background, fg=TEXT, activebackground=active, activeforeground=TEXT,
                         font=(self._family, 11, "bold"), padx=20, pady=9, cursor="hand2",
                         disabledforeground=MUTED)

    # ------------------------------------------------------------------ workers
    def _set_status(self, text: str, error: bool = False) -> None:
        self._status.configure(text=text, fg=DANGER if error else ACCENT_LIGHT)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for button in (self._test_button, self._login_button):
            button.configure(state=state)
        self.configure(cursor="watch" if busy else "")

    def _fields(self):
        return (self._url_entry.get().strip(), self._user_entry.get().strip(),
                self._password_entry.get())

    def _make_client(self, url: str) -> JellyfinClient:
        return JellyfinClient(url, device_id=self._defaults.get("device_id", ""), debug=self._debug)

    def _test_connection(self) -> None:
        if self._busy:
            return
        url, _user, _password = self._fields()
        self._set_busy(True)
        self._set_status("Testing connection…")
        threading.Thread(target=self._test_worker, args=(url,), daemon=True).start()

    def _test_worker(self, url: str) -> None:
        try:
            info = self._make_client(url).test_connection()
            name = info.get("ServerName") or "Jellyfin"
            version = info.get("Version") or "?"
            self._post(self._set_status, f"Connected to {name} (Jellyfin {version})")
        except JellyfinError as exc:
            self._post(self._set_status, str(exc), True)
        except Exception as exc:  # the screen must stay usable whatever happens
            self._post(self._set_status, f"Unexpected error: {exc}", True)
        finally:
            self._post(self._set_busy, False)

    def _submit(self) -> None:
        if self._busy:
            return
        url, user, password = self._fields()
        if not url or not user:
            self._set_status("The server URL and the username are required", True)
            return
        self._set_busy(True)
        self._set_status("Signing in…")
        threading.Thread(target=self._login_worker, args=(url, user, password), daemon=True).start()

    def _login_worker(self, url: str, user: str, password: str) -> None:
        try:
            client = self._make_client(url)
            client.authenticate(user, password)
        except JellyfinError as exc:
            self._post(self._set_status, str(exc), True)
            self._post(self._set_busy, False)
            return
        except Exception as exc:
            self._post(self._set_status, f"Unexpected error: {exc}", True)
            self._post(self._set_busy, False)
            return
        self._post(self._set_status, f"Signed in as {client.username}")
        self._post(self._on_success, client)

class PlayerScreen(ThreadSafeFrame):
    """Now-playing screen: cover art, song information, progress and transport."""

    def __init__(self, master: tk.Misc, *, engine, client: JellyfinClient, family: str,
                 on_change_account: Callable[[], None], on_quit: Callable[[], None],
                 windowed_geometry: str = WINDOW_SIZE,
                 on_window_state_change: Optional[Callable[[], None]] = None,
                 debug: bool = False) -> None:
        super().__init__(master, bg=BACKGROUND)
        self.engine = engine
        self.client = client
        self.family = family or pick_font_family()
        self._on_change_account = on_change_account
        self._on_quit = on_quit
        self._on_window_state_change = on_window_state_change
        self._windowed_geometry = windowed_geometry or WINDOW_SIZE
        self._debug = debug
        self._alive = True
        self._state: Dict[str, Any] = {}
        self._cover_key: Optional[str] = None
        self._cover_image: Optional[ImageTk.PhotoImage] = None
        self._seeking = False
        self._seek_preview = 0.0
        self._bindings: List[Any] = []
        self._cover_size = self._cover_geometry()
        self._resize_job: Optional[str] = None
        self._focus_job: Optional[str] = None
        self._text_width = 600
        self._status_width = 400
        self._up_next_width = 600
        self._account_width = 240
        self._current_text: tuple = ("", "", "")
        self._up_next_raw = ""
        self._status_raw = ""
        self._account_raw = ""
        # Height of the immovable chrome, measured once the widgets exist.
        self._chrome_height = 0
        self._text_block_height = 120
        # Player actions run on their own thread: no click may ever block the Tk
        # event loop (mpv commands wait for a reply from mpv).
        self._actions: queue.Queue = queue.Queue()
        threading.Thread(target=self._action_worker, name="ui-actions", daemon=True).start()

        self._build()
        self._measure_chrome()
        self._bind_shortcuts()
        self.bind("<Configure>", self._on_resize)
        engine.add_listener(self._on_state)
        self._apply_state(engine.snapshot())
        self._relayout(force=True)

    def _cover_size_for(self, height: int, width: int) -> int:
        """Biggest artwork that still leaves room for every fixed block.

        The chrome (header, controls, footer, text box) is measured once the
        widgets exist, so the artwork is the only thing that grows or shrinks.
        """
        available = height - self._chrome_height - CHROME_MARGIN
        return int(max(MIN_COVER, min(MAX_COVER, available, int(width * 0.34))))

    def _cover_geometry(self) -> int:
        """Provisional cover size (screen based while the window is unmapped)."""
        window = self.winfo_toplevel()
        height, width = window.winfo_height(), window.winfo_width()
        if height < 300 or width < 400:
            height = window.winfo_screenheight() or 1080
            width = window.winfo_screenwidth() or 1920
        return int(max(MIN_COVER, min(MAX_COVER, height - 420, int(width * 0.34))))

    def _measure_chrome(self) -> None:
        """Measure header + controls + footer + text box (they never change)."""
        self.update_idletasks()
        self._chrome_height = (self._header.winfo_reqheight()
                               + self._controls.winfo_reqheight()
                               + self._footer.winfo_reqheight()
                               + self._text_block_height)

    def _wrap_width(self, window_width: int) -> int:
        """How wide the song information may be (never wider than the window)."""
        limit = window_width - 2 * FOOTER_PAD[0] - 40
        return int(max(320, min(limit, self._cover_size * 1.9)))

    # ------------------------------------------------------------- text fitting
    @staticmethod
    def _split_lines(text: str, font: tkfont.Font, width: int) -> List[str]:
        """Simulate Tk's word wrapping so the needed number of lines is known."""
        lines: List[str] = []
        current = ""
        for word in text.split():
            candidate = f"{current} {word}".strip()
            if current and font.measure(candidate) > width:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current)
        return lines

    @staticmethod
    def _ellipsize(text: str, font: tkfont.Font, width: int) -> str:
        """Shorten *text* with an ellipsis until it measures at most *width*."""
        if font.measure(text) <= width:
            return text
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if font.measure(text[:middle].rstrip() + "…") <= width:
                low = middle
            else:
                high = middle - 1
        return (text[:low].rstrip() + "…") if low else "…"

    def _fit_text(self, text: str, *, max_size: int, min_size: int, width: int,
                  max_lines: int, bold: bool = False):
        """Largest readable font that fits, then an ellipsis.

        Returns ``(font_spec, text_to_show)``; the text never needs more room than
        the fixed box it goes into, which is what keeps the layout still.
        """
        text = (text or "").strip()
        weight = "bold" if bold else "normal"
        if not text or width <= 0:
            return (self.family, max_size, weight), text
        for size in range(max_size, min_size - 1, -1):
            font = tkfont.Font(family=self.family, size=size, weight=weight)
            if len(self._split_lines(text, font, width)) <= max_lines:
                return (self.family, size, weight), text
        font = tkfont.Font(family=self.family, size=min_size, weight=weight)
        if max_lines == 1:
            return (self.family, min_size, weight), self._ellipsize(text, font, width)
        lines = self._split_lines(text, font, width)
        head = " ".join(lines[: max_lines - 1])
        tail = " ".join(lines[max_lines - 1:])
        tail = self._ellipsize(tail, font, width)
        shown = f"{head} {tail}".strip() if head else tail
        return (self.family, min_size, weight), shown

    def _apply_text(self, title: str, artist: str, album: str) -> None:
        """Fit the song information into its fixed box (remembering the strings)."""
        self._current_text = (title or "", artist or "", album or "")
        width = self._text_width
        title_font, shown_title = self._fit_text(title, max_size=TITLE_SIZE,
                                                 min_size=TITLE_MIN_SIZE, width=width,
                                                 max_lines=TITLE_LINES, bold=True)
        artist_font, shown_artist = self._fit_text(artist, max_size=ARTIST_SIZE,
                                                   min_size=ARTIST_MIN_SIZE, width=width, max_lines=1)
        album_font, shown_album = self._fit_text(album, max_size=ALBUM_SIZE,
                                                 min_size=ALBUM_MIN_SIZE, width=width, max_lines=1)
        self._title.configure(text=shown_title or "Waiting for the library…",
                              font=title_font, wraplength=width)
        self._artist.configure(text=shown_artist, font=artist_font, wraplength=width)
        self._album.configure(text=shown_album, font=album_font, wraplength=width)

    def _refresh_footer(self) -> None:
        """Re-ellipsize the footer texts for the current window width."""
        font = tkfont.Font(family=self.family, size=STATUS_SIZE)
        if self._up_next_raw:
            self._up_next.configure(text=self._ellipsize(self._up_next_raw, font,
                                                         self._up_next_width))
        if self._status_raw:
            self._status.configure(text=self._ellipsize(self._status_raw, font,
                                                        self._status_width))
        if self._account_raw:
            self._account.configure(text=self._ellipsize(self._account_raw, font,
                                                         self._account_width))

    # -------------------------------------------------------------- window size
    def _is_fullscreen(self) -> bool:
        try:
            return bool(self.winfo_toplevel().attributes("-fullscreen"))
        except tk.TclError:
            return False

    def _remember_window_size(self) -> None:
        """Keep the windowed size, so Esc returns to exactly this size."""
        if self._is_fullscreen():
            return
        window = self.winfo_toplevel()
        width, height = window.winfo_width(), window.winfo_height()
        if width > 400 and height > 300:
            self._windowed_geometry = f"{width}x{height}"

    def _notify_window_state(self) -> None:
        """Tell the application to persist the window mode/size."""
        if self._on_window_state_change is None:
            return
        try:
            self._on_window_state_change()
        except Exception as exc:
            print(f"[ui] window state callback error: {exc}", flush=True)

    # ---------------------------------------------------------------- resizing
    def _on_resize(self, event: Any) -> None:
        """Re-fit the artwork a moment after the window stops changing size."""
        if event.widget is not self:
            return
        self._remember_window_size()
        if self._resize_job is not None:
            try:
                self.after_cancel(self._resize_job)
            except (tk.TclError, ValueError):
                pass
        self._resize_job = self.after(400, self._on_settled)

    def _on_settled(self) -> None:
        self._resize_job = None
        self._relayout()
        self._notify_window_state()

    def _relayout(self, force: bool = False) -> None:
        """Re-fit the artwork, the progress bar and the text to the window size."""
        height, width = self.winfo_height(), self.winfo_width()
        if height < 300 or width < 400:
            return
        size = self._cover_size_for(height, width)
        resized = force or abs(size - self._cover_size) >= 24
        if resized:
            self._cover_size = size
            self._cover_label.configure(width=size, height=size)
            self._progress_width = int(size * 1.55)
            self._progress.configure(width=self._progress_width)
            self._draw_progress(float(self._state.get("position") or 0.0),
                                float(self._state.get("duration") or 0.0))
            self._cover_key = None      # forces a re-render at the new size
        self._text_width = self._wrap_width(width)
        self._up_next_width = max(240, width - 2 * FOOTER_PAD[0])
        self._status_width = max(200, width - 2 * FOOTER_PAD[0] - 360)
        self._account_width = max(120, width - 2 * FOOTER_PAD[0] - 520)
        self._apply_text(*self._current_text)
        self._refresh_footer()
        if resized:
            self._update_cover(self._state.get("current"))

    # ------------------------------------------------------------------- layout
    def _build(self) -> None:
        self._build_header()        # packed at the top: fixed height
        self._build_footer()        # packed at the bottom: fixed height
        self._build_controls()      # packed above the footer: immovable
        # Everything above is packed first, so nothing can ever push it around.
        # The flexible area holds only the artwork and the fixed-height text box.
        self._stage = tk.Frame(self, bg=BACKGROUND)
        self._stage.pack(expand=True, fill="both")
        tk.Frame(self._stage, bg=BACKGROUND).pack(expand=True, fill="both")

        self._cover_label = tk.Label(self._stage, bg=BACKGROUND, bd=0,
                                     width=self._cover_size, height=self._cover_size)
        self._cover_label.pack()
        self._set_cover_image(placeholder_cover("♪", "loading library…", self._cover_size))

        self._build_text_block(self._stage)
        tk.Frame(self._stage, bg=BACKGROUND).pack(expand=True, fill="both")

    def _build_controls(self) -> None:
        """Progress bar + transport buttons, pinned above the footer."""
        self._controls = tk.Frame(self, bg=BACKGROUND)
        self._controls.pack(side="bottom", fill="x", pady=(0, 10))
        self._build_progress(self._controls)
        self._build_transport(self._controls)

    def _build_text_block(self, parent: tk.Misc) -> None:
        """Fixed-height box for the title/artist/album (long names shrink inside)."""
        self._text_block = tk.Frame(parent, bg=BACKGROUND)
        self._text_block.pack(fill="x", pady=(22, 0))
        self._title = tk.Label(self._text_block, text="Waiting for the library…",
                               bg=BACKGROUND, fg=TEXT, font=(self.family, TITLE_SIZE, "bold"),
                               justify="center")
        self._artist = tk.Label(self._text_block, text="", bg=BACKGROUND, fg=ACCENT_LIGHT,
                                font=(self.family, ARTIST_SIZE))
        self._album = tk.Label(self._text_block, text="", bg=BACKGROUND, fg=MUTED,
                               font=(self.family, ALBUM_SIZE))
        # Anchored to the bottom of the box, so the artist/album lines always sit
        # at the same height and a wrapping title grows upwards into the spare
        # room instead of moving anything else.
        self._album.pack(side="bottom")
        self._artist.pack(side="bottom", pady=(0, 2))
        self._title.pack(side="bottom", pady=(0, 6))
        self.update_idletasks()
        # Budget: TITLE_LINES title lines + one artist + one album line. Freezing
        # the height is what keeps the rest of the screen from ever moving.
        extra = tkfont.Font(family=self.family, size=TITLE_SIZE,
                            weight="bold").metrics("linespace") * (TITLE_LINES - 1)
        self._text_block_height = self._text_block.winfo_reqheight() + extra
        self._text_block.configure(height=self._text_block_height)
        self._text_block.pack_propagate(False)

    def _build_footer(self) -> None:
        """Bottom bar: what is next, the status, the user and the volume.

        Every label here stays on a single line (long texts are ellipsized), so
        the footer has a fixed height and can never steal room from the controls.
        """
        self._footer = tk.Frame(self, bg=BACKGROUND)
        self._footer.pack(side="bottom", fill="x", padx=FOOTER_PAD[0], pady=(0, FOOTER_PAD[1]))
        self._up_next = tk.Label(self._footer, text="", bg=BACKGROUND, fg=MUTED,
                                 font=(self.family, STATUS_SIZE), anchor="center")
        self._up_next.pack(fill="x")
        row = tk.Frame(self._footer, bg=BACKGROUND)
        row.pack(fill="x", pady=(6, 0))
        self._status = tk.Label(row, text="Loading your music library…", bg=BACKGROUND, fg=MUTED,
                                font=(self.family, STATUS_SIZE), anchor="w")
        self._status.pack(side="left")
        self._account = tk.Label(row, text="", bg=BACKGROUND, fg=MUTED,
                                 font=(self.family, STATUS_SIZE), anchor="w")
        self._account.pack(side="left", padx=(18, 0))
        self._volume_label = tk.Label(row, text="Volume 80%", bg=BACKGROUND, fg=MUTED,
                                      font=(self.family, STATUS_SIZE), anchor="e")
        self._volume_label.pack(side="right")


    def _build_header(self) -> None:
        self._header = tk.Frame(self, bg=BACKGROUND)
        self._header.pack(side="top", fill="x", padx=28, pady=(18, 0))
        tk.Label(self._header, text="SimpleJellyMus", bg=BACKGROUND, fg=TEXT,
                 font=(self.family, 15, "bold")).pack(side="left")
        tk.Button(self._header, text="Quit", command=self._on_quit, relief="flat", borderwidth=0,
                  bg=CARD_LIGHT, fg=TEXT, activebackground=CARD, activeforeground=TEXT,
                  font=(self.family, 10, "bold"), padx=14, pady=6,
                  cursor="hand2").pack(side="right", padx=(8, 0))
        tk.Button(self._header, text="Change account", command=self._on_change_account, relief="flat",
                  borderwidth=0, bg=CARD_LIGHT, fg=TEXT, activebackground=CARD, activeforeground=TEXT,
                  font=(self.family, 10, "bold"), padx=14, pady=6,
                  cursor="hand2").pack(side="right")

    # ---------------------------------------------------------------- progress
    def _build_progress(self, parent: tk.Misc) -> None:
        self._progress_width = int(self._cover_size * 1.55)
        self._progress_height = 26
        # The bar and both time labels live in one canvas, so the labels always
        # line up with the bar instead of the window edges.
        self._progress = tk.Canvas(parent, width=self._progress_width,
                                   height=self._progress_height + 20, bg=BACKGROUND,
                                   highlightthickness=0, cursor="hand2")
        self._progress.pack()
        self._progress.bind("<Button-1>", self._seek_start)
        self._progress.bind("<B1-Motion>", self._seek_move)
        self._progress.bind("<ButtonRelease-1>", self._seek_end)
        self._draw_progress(0.0, 0.0)

    def _draw_progress(self, position: float, duration: float) -> None:
        canvas = self._progress
        canvas.delete("all")
        width = self._progress_width
        bar_y = 13
        left, right = 6, width - 6
        canvas.create_line(left, bar_y, right, bar_y, fill=CARD_LIGHT, width=6, capstyle="round")
        ratio = 0.0 if duration <= 0 else max(0.0, min(1.0, position / duration))
        x = left + (right - left) * ratio
        if ratio > 0:
            canvas.create_line(left, bar_y, x, bar_y, fill=ACCENT, width=6, capstyle="round")
        canvas.create_oval(x - 6, bar_y - 6, x + 6, bar_y + 6,
                           fill=TEXT if self._seeking else ACCENT_LIGHT, outline=BACKGROUND, width=2)
        elapsed = self._seek_preview if self._seeking else position
        canvas.create_text(left, 33, text=format_time(elapsed), fill=MUTED,
                           font=(self.family, 10), anchor="w")
        canvas.create_text(right, 33, text=format_time(duration), fill=MUTED,
                           font=(self.family, 10), anchor="e")

    def _seek_start(self, event: Any) -> None:
        self._seeking = True
        self._seek_move(event)

    def _seek_move(self, event: Any) -> None:
        duration = float(self._state.get("duration") or 0.0)
        if duration <= 0:
            return
        ratio = max(0.0, min(1.0, (event.x - 6) / max(1, self._progress_width - 12)))
        self._seek_preview = ratio * duration
        self._draw_progress(self._seek_preview, duration)

    def _seek_end(self, event: Any) -> None:
        duration = float(self._state.get("duration") or 0.0)
        self._seeking = False
        if duration > 0:
            self._run(self.engine.seek_absolute, self._seek_preview)
        self._draw_progress(float(self._state.get("position") or 0.0), duration)

    # --------------------------------------------------------------- transport
    def _build_transport(self, parent: tk.Misc) -> None:
        self._transport = tk.Canvas(parent, width=360, height=90, bg=BACKGROUND,
                                    highlightthickness=0, cursor="hand2")
        self._transport.pack(pady=(4, 0))
        self._paused = False
        self._hovered: Optional[str] = None
        self._transport_items: Dict[str, List[int]] = {}
        self._draw_transport(False)
        for tag, handler in (("prev", self._previous), ("main", self._toggle), ("next", self._next)):
            self._transport.tag_bind(tag, "<Button-1>", lambda _event, h=handler: h())
        # Hover highlighting is handled by the canvas itself instead of per item
        # <Enter>/<Leave> bindings: deleting and recreating the item under the
        # pointer fires Leave/Enter again, and that feedback loop spins the Tk
        # thread at 100% CPU until the window stops responding.
        self._transport.bind("<Motion>", self._on_transport_motion)
        self._transport.bind("<Leave>", lambda _event: self._set_hovered(None))

    def _on_transport_motion(self, event: Any) -> None:
        """Find the icon under the pointer and highlight it."""
        tag: Optional[str] = None
        current = self._transport.find_withtag("current")
        if current:
            tags = self._transport.gettags(current[0])
            for candidate in ("prev", "main", "next"):
                if candidate in tags:
                    tag = candidate
                    break
        self._set_hovered(tag)

    def _set_hovered(self, tag: Optional[str]) -> None:
        """Change the highlight without touching the canvas item list."""
        if tag == self._hovered:
            return
        self._hovered = tag
        self._paint_transport()

    def _draw_transport(self, paused: bool) -> None:
        """(Re)build the transport icons for the current pause state."""
        canvas = self._transport
        self._paused = paused
        canvas.delete("all")
        items: Dict[str, List[int]] = {"prev": [], "next": [], "main": [], "glyph": []}
        items["prev"].append(canvas.create_rectangle(
            84, 28, 91, 62, fill=TEXT, outline=TEXT, tags=("prev",)))
        items["prev"].append(canvas.create_polygon(
            110, 28, 110, 62, 88, 45, fill=TEXT, outline=TEXT, tags=("prev",)))
        items["next"].append(canvas.create_polygon(
            250, 28, 250, 62, 272, 45, fill=TEXT, outline=TEXT, tags=("next",)))
        items["next"].append(canvas.create_rectangle(
            269, 28, 276, 62, fill=TEXT, outline=TEXT, tags=("next",)))
        items["main"].append(canvas.create_oval(
            144, 9, 216, 81, fill=ACCENT, outline=ACCENT, tags=("main",)))
        if paused:
            items["glyph"].append(canvas.create_rectangle(
                171, 27, 179, 63, fill=TEXT, outline=TEXT, tags=("main",)))
            items["glyph"].append(canvas.create_rectangle(
                189, 27, 197, 63, fill=TEXT, outline=TEXT, tags=("main",)))
        else:
            items["glyph"].append(canvas.create_polygon(
                171, 27, 171, 63, 199, 45, fill=TEXT, outline=TEXT, tags=("main",)))
        self._transport_items = items
        self._paint_transport()

    def _paint_transport(self) -> None:
        """Recolour the existing items for the current hover state."""
        canvas = self._transport
        for tag in ("prev", "next"):
            colour = ACCENT_LIGHT if self._hovered == tag else TEXT
            for item in self._transport_items.get(tag, ()):
                canvas.itemconfigure(item, fill=colour, outline=colour)
        colour = ACCENT_LIGHT if self._hovered == "main" else ACCENT
        for item in self._transport_items.get("main", ()):
            canvas.itemconfigure(item, fill=colour, outline=colour)

    # ----------------------------------------------------------------- controls
    def _run(self, action: Callable[..., None], *args: Any) -> None:
        """Queue a player action so the Tk thread never waits for mpv."""
        self._actions.put((action, args))

    def _action_worker(self) -> None:
        """Runs the queued player actions, one at a time."""
        while True:
            action, args = self._actions.get()
            if action is None:      # shutdown sentinel
                return
            try:
                action(*args)
            except Exception as exc:  # a failing action must not kill the worker
                print(f"[ui] action error: {exc}", flush=True)

    def _toggle(self) -> None:
        self._run(self.engine.toggle_pause)

    def _next(self) -> None:
        self._run(self.engine.next_track)

    def _previous(self) -> None:
        self._run(self.engine.previous_track)

    def _restart(self) -> None:
        self._run(self.engine.restart_track)

    def _volume(self, delta: float) -> None:
        self._run(self.engine.adjust_volume, delta)

    def _seek_relative(self, delta: float) -> None:
        self._run(self.engine.seek_relative, delta)

    def _toggle_fullscreen(self) -> None:
        """Switch between fullscreen and the windowed size (Esc or F)."""
        root = self.winfo_toplevel()
        try:
            if self._is_fullscreen():
                root.attributes("-fullscreen", False)
                root.geometry(self._windowed_geometry or WINDOW_SIZE)
            else:
                self._remember_window_size()
                root.attributes("-fullscreen", True)
        except tk.TclError:
            return
        self._notify_window_state()

    # ------------------------------------------------------------------- input
    def _bind_shortcuts(self) -> None:
        root = self.winfo_toplevel()
        shortcuts = (
            ("<space>", self._toggle),
            ("<Right>", self._next), ("<n>", self._next), ("<N>", self._next),
            ("<Left>", self._previous), ("<p>", self._previous), ("<P>", self._previous),
            ("<Up>", lambda: self._volume(5)), ("<Down>", lambda: self._volume(-5)),
            ("<Button-4>", lambda: self._volume(5)), ("<Button-5>", lambda: self._volume(-5)),
            ("<s>", self._restart), ("<S>", self._restart),
            ("<comma>", lambda: self._seek_relative(-10.0)),
            ("<period>", lambda: self._seek_relative(10.0)),
            ("<f>", self._toggle_fullscreen), ("<F>", self._toggle_fullscreen),
            ("<Escape>", self._toggle_fullscreen),
            ("<Control-q>", self._on_quit), ("<q>", self._on_quit), ("<Q>", self._on_quit),
        )
        for sequence, handler in shortcuts:
            try:
                self._bindings.append((sequence, root.bind(sequence, self._wrap(handler), add="+")))
            except tk.TclError:
                pass
        self._focus_job = self.after(150, self._grab_focus)

    def _wrap(self, handler: Callable[[], None]) -> Callable[[Any], None]:
        """Ignore shortcuts while a text/button widget has the focus."""
        def dispatch(event: Any):
            if isinstance(event.widget, (tk.Entry, tk.Text, tk.Button)):
                return None
            handler()
            return None
        return dispatch

    def _grab_focus(self) -> None:
        try:
            self.winfo_toplevel().focus_force()
        except (tk.TclError, RuntimeError):
            pass

    def destroy(self) -> None:
        """Unbind the global shortcuts, cancel timers, stop state updates."""
        if getattr(self, "_torn_down", False):
            return
        self._torn_down = True
        self._alive = False
        try:
            root = self.winfo_toplevel()
            for sequence, func_id in self._bindings:
                try:
                    root.unbind(sequence, func_id)
                except tk.TclError:
                    pass
        except tk.TclError:
            pass
        self._bindings = []
        for name in ("_resize_job", "_focus_job"):
            job = getattr(self, name, None)
            if job is not None:
                try:
                    self.after_cancel(job)
                except (tk.TclError, ValueError):
                    pass
                setattr(self, name, None)
        try:
            self._actions.put((None, ()))     # stop the action worker
        except Exception:
            pass
        super().destroy()


    # ------------------------------------------------------------ state updates
    def _on_state(self, state: Dict[str, Any]) -> None:
        """Called from player threads: hand the snapshot to the Tk thread."""
        if not self._alive:
            return
        self._post(self._apply_state, state)

    def _apply_state(self, state: Dict[str, Any]) -> None:
        if not self._alive:
            return
        current = state.get("current") or {}
        previous = self._state.get("current") or {}
        if current.get("Id") != previous.get("Id"):
            self._show_item(current)

        if bool(state.get("paused")) != bool(self._state.get("paused")):
            self._draw_transport(bool(state.get("paused")))

        duration = float(state.get("duration") or 0.0)
        if not self._seeking:
            self._draw_progress(float(state.get("position") or 0.0), duration)

        up_next = state.get("up_next") or {}
        if up_next:
            hint = (f"Up next · {JellyfinClient.display_title(up_next)} — "
                    f"{JellyfinClient.display_artist(up_next)}")
            if state.get("preload_ready"):
                hint += "  ✓ ready"
        else:
            hint = ""

        volume_text = f"Volume {int(state.get('volume') or 0)}%"
        if self._volume_label.cget("text") != volume_text:
            self._volume_label.configure(text=volume_text)

        error = state.get("error") or ""
        message = error or state.get("status") or ""
        account = f"{state.get('user') or ''} @ {state.get('server') or ''}"
        if (hint, message, account) != (self._up_next_raw, self._status_raw, self._account_raw):
            # One line each: long footer texts are ellipsized, never wrapped, so
            # the footer height (and with it the controls) can never move.
            self._up_next_raw, self._status_raw, self._account_raw = hint, message, account
            self._status.configure(fg=DANGER if error else MUTED)
            self._refresh_footer()
        self._state = state

    def _show_item(self, item: Dict[str, Any]) -> None:
        """Update the song information labels (and the window title)."""
        if not item:
            self._apply_text("", "", "")
            self._update_cover(None)
            return
        title = JellyfinClient.display_title(item)
        artist = JellyfinClient.display_artist(item)
        album = str(item.get("Album") or "")
        year = item.get("ProductionYear")
        album_line = f"{album} · {year}" if album and year else (album or (str(year) if year else ""))
        self._apply_text(title, artist, album_line)     # fitted into the fixed box
        try:
            # The window title always carries the complete, untruncated names.
            self.winfo_toplevel().title(f"{title} — {artist} — SimpleJellyMus")
        except tk.TclError:
            pass
        self._update_cover(item)

    # ---------------------------------------------------------------- cover art
    def _cover_key_for(self, item: Optional[Dict[str, Any]]) -> Optional[str]:
        if not item:
            return None
        tags = item.get("ImageTags") or {}
        tag = tags.get("Primary") or item.get("AlbumPrimaryImageTag") or "none"
        return f"{item.get('AlbumId') or item.get('Id')}:{tag}"

    def _update_cover(self, item: Optional[Dict[str, Any]]) -> None:
        """Show a placeholder, then load the real artwork in the background."""
        key = self._cover_key_for(item)
        if key == self._cover_key:
            return
        self._cover_key = key
        if not item:
            self._set_cover_image(placeholder_cover("♪", "waiting for music", self._cover_size))
            return
        self._set_cover_image(placeholder_cover(JellyfinClient.display_title(item),
                                                JellyfinClient.display_artist(item),
                                                self._cover_size))
        threading.Thread(target=self._cover_worker, args=(dict(item), key), daemon=True).start()

    def _cover_worker(self, item: Dict[str, Any], key: Optional[str]) -> None:
        image: Optional[Image.Image] = None
        try:
            path = self.client.download_image(item)
        except JellyfinError as exc:
            if self._debug:
                print(f"[ui] cover download failed: {exc}", flush=True)
            path = None
        if path is not None:
            try:
                with Image.open(path) as raw:
                    image = rounded_cover(raw, self._cover_size)
            except (OSError, ValueError) as exc:
                if self._debug:
                    print(f"[ui] cover could not be decoded: {exc}", flush=True)
        if image is None:
            return
        self._post(self._apply_cover, key, image)

    def _apply_cover(self, key: Optional[str], image: Image.Image) -> None:
        if not self._alive or key != self._cover_key:
            return
        self._set_cover_image(image)

    def _set_cover_image(self, image: Image.Image) -> None:
        try:
            photo = ImageTk.PhotoImage(image)
        except (tk.TclError, RuntimeError):
            return
        self._cover_image = photo
        self._cover_label.configure(image=photo)


def format_time(seconds: Any) -> str:
    """Format a duration in seconds as ``m:ss`` (or ``h:mm:ss``)."""
    try:
        total = int(max(0.0, float(seconds)))
    except (TypeError, ValueError):
        total = 0
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"





