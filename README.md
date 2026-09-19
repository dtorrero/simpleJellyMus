# SimpleJellyMus

A tiny desktop **random Jellyfin music player** written in Python, made for my
machine (Manjaro / X11 / KDE, 1920x1080).

It logs in **once**, then plays endless random **audio** tracks from your
Jellyfin library, preloading the next song so there is no waiting between
tracks. The screen shows the album cover, the song information, a progress bar
and play/pause/next/previous controls.

> Music only: the library query asks Jellyfin for `IncludeItemTypes=Audio`, every
> item is validated (no videos, no music videos), and mpv runs with
> `--no-video --vid=no --audio-display=no`. Video files are ignored entirely.
<img width="1920" height="1080" alt="SJM_example" src="https://github.com/user-attachments/assets/c4e7cd1f-80eb-440f-a4dc-dc013e9b3ac8" />
---


## Requirements

| Component | Status on this machine |
|---|---|
| Python 3.9+ (with `tkinter`) | ✅ 3.14.7 |
| `mpv` (playback engine, used as an audio-only subprocess) | ✅ 0.41.0 |
| Pillow (`PIL`) for cover art | ✅ 12.3.0 |

Everything else is Python's standard library (`urllib`, `json`, `socket`,
`threading`, `tkinter`). No `pip install` is required for the current setup
(`requirements.txt` just documents the optional dependency).

## Install / run

```bash
cd simpleJellyMus
python3 main.py
```

Useful flags:

```bash
python3 main.py --fullscreen     # start in fullscreen (Esc switches back and forth)
python3 main.py --windowed       # start windowed (this is the default)
python3 main.py --login          # force the login screen
python3 main.py --reset-login    # forget the saved login and exit
python3 main.py --debug          # verbose Jellyfin + mpv logging
```

The app starts **windowed** (remembering the size and fullscreen/windowed mode
you used last time) and `Esc` switches between windowed and fullscreen.

### Desktop entry (application menu)

```bash
./install.sh                 # add the menu entry + icon (no root needed)
./install.sh --uninstall     # remove them again
```

The installer renders `simplejellymus.desktop` with this directory's real path,
writes it to `~/.local/share/applications/simplejellymus.desktop`, installs the
application icon into the `hicolor` icon theme and refreshes the menu/icon
caches. **SimpleJellyMus** then shows up under *Audio / Music* and can be pinned
to the panel or the task manager.

The icon is `assets/fire_icon_variant_1.png`: installed as-is (1024×1024) plus
downscaled copies for the menu, the panel and the window list (256, 128, 64, 48,
32, 24 and 16 px, generated with Pillow). Both the launcher and the window itself
use it, so it also shows when the program is started from a terminal; the window
sends the window manager its own set of sizes (128 px down to 16 px) as well, so
a panel or a title bar never has to scale a single large bitmap down itself. To
use a different picture, point `ICON_SOURCE=` in `install.sh` and `ICON_FILE` in
`main.py` at the new file and run `./install.sh` again.

**Only one window, ever.** The launcher is marked `SingleMainWindow`, and the app
itself enforces it: the first copy binds a socket in `$XDG_RUNTIME_DIR`, and
starting it again (menu, panel, `python3 main.py`, a second terminal) just prints
`SimpleJellyMus is already running - bringing it to the front.`, un-minimises the
running window and quits, so you never end up with two players fighting over the
speakers.

## First run

1. Enter the server URL, e.g. `http://192.168.1.10:8096` (the scheme is optional).
2. Enter your Jellyfin username and password.
3. Press **Test connection** to verify the address (optional), then **Log in**.

The access token, user id and a stable device id are stored in
`~/.config/simplejellymus/config.json` (permissions `600`, the password is never
saved). On the next start the player goes straight to the music.

## Controls

| Key | Action |
|---|---|
| `Space` | play / pause |
| `N` or `→` | next random song |
| `P` or `←` | previous song (restarts the current track if more than 5 s played) |
| `S` | restart the current track |
| `↑` / `↓` | volume up / down (mouse wheel works too) |
| `,` / `.` | seek -10 s / +10 s |
| `Esc` or `F` | switch between windowed and fullscreen |
| `Q` (or `Ctrl+Q`) | quit |

The on-screen buttons do the same: click the cover art area's transport
buttons, or click the progress bar to seek.

## How it works

```
Tkinter UI (ui.py) ──state snapshots── PlayerEngine (player.py)
                                          ├── MpvPlayer   mpv JSON IPC (audio only)
                                          ├── Preloader   next track -> disk cache
                                          ├── PlayQueue   random batches, no repeats
                                          └── JellyfinClient (jellyfin.py) -> server
```

* **Random songs** come from Jellyfin in random batches
  (`/Users/{id}/Items?Recursive=true&IncludeItemTypes=Audio&SortBy=Random&Limit=200`).
  Recently played tracks are skipped, and the same artist is avoided twice in a row.
  A new batch is fetched in the background before the current one runs out.
* **Preloading**: as soon as a track starts, the next one is downloaded in the
  background to `~/.cache/simplejellymus/audio/` and appended to mpv's playlist
  (`--prefetch-playlist=yes --gapless-audio=yes`), so the transition is instant.
  If the download is not finished in time, the stream URL is appended instead, so
  playback never stalls. At most 4 files are kept.
* **Cover art** is fetched from `/Items/{id}/Images/Primary` (falling back to the
  album image), cached in `~/.cache/simplejellymus/covers/` and drawn as a
  rounded square, with a generated placeholder when a release has no artwork.
* **Long names can't disturb the layout**: the artwork is sized from the
  *measured* height of the header, the controls, the footer and a fixed-height
  song information box, so the artwork, progress bar and transport buttons
  always keep their place. A title that doesn't fit shrinks (25 pt down to a
  readable 15 pt) and is then ellipsized with `…`; the complete name is still in
  the window title.
* **One instance only**: `instance.py` binds a socket in `$XDG_RUNTIME_DIR`; a
  second start sends `focus` over it and exits, so the running window is brought
  to the front (un-minimised) instead of a second player being started that would
  fight over the audio output.
* **The interface never blocks**: player actions (play/pause, next, previous,
  seek, volume) run on their own thread and mpv's control thread only ever
  *reads* events — it never issues commands itself — so a slow command can never
  freeze the window. Track changes go through one serialised transition step, and
  mpv's own playlist advance keeps the hand-over gapless.

## File layout

| File | Purpose |
|---|---|
| `main.py` | entry point, CLI flags, login/player screen switching, window focusing |
| `instance.py` | single-instance guard (Unix socket in `$XDG_RUNTIME_DIR`) |
| `jellyfin.py` | Jellyfin client (login, random batches, stream/image URLs, downloads) + config storage |
| `player.py` | mpv IPC client, preloader cache, random play queue, engine |
| `ui.py` | Tkinter login screen and fullscreen player UI |
| `selftest.py` | offline self-test: fake Jellyfin server + generated audio (see below) |
| `install.sh` | installs/removes the menu entry and the icon (see below) |
| `simplejellymus.desktop` | launcher template (`@APPDIR@` is filled in by `install.sh`) |
| `assets/fire_icon_variant_1.png` | application icon (1024×1024 master, installed in several sizes) |

## Self-test (no Jellyfin server needed)

`selftest.py` starts a small fake Jellyfin server (with decoy video items),
generates short WAV tracks, and verifies the whole pipeline: login, the
music-only filter, cover download, the preload cache, gapless auto-advance,
next/previous, pause, volume, seeking, the UI layout (long titles), and the
single-instance guard.

```bash
python3 selftest.py            # ~1 minute, silent (volume 0)
python3 -u selftest.py | tail -5
```

It writes everything to a throwaway directory, so your real login and library
are never touched. Set `SELFTEST_DEBUG=1` for verbose mpv logging.


## Configuration and cache

| Path | Content |
|---|---|
| `~/.config/simplejellymus/config.json` | server URL, username, access token, user id, device id, volume, window mode and size |
| `~/.cache/simplejellymus/audio/` | preloaded (next) tracks, pruned automatically |
| `~/.cache/simplejellymus/covers/` | album art cache |
| `$XDG_RUNTIME_DIR/simplejellymus.sock` | single-instance guard (removed when the app quits) |
| `~/.local/share/applications/simplejellymus.desktop` | menu entry created by `install.sh` |
| `~/.local/share/icons/hicolor/<size>x<size>/apps/simplejellymus.png` | icon installed by `install.sh` (one copy per size) |

Delete the config file (or run `--reset-login`) to change accounts. Nothing in the
repository itself ever holds your credentials — the token only lives in the config
file above (mode `600`), and `.gitignore` additionally refuses to stage a stray
`config.json`, `*.token` or `*.log`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `mpv was not found in PATH` | `sudo pacman -S mpv` |
| Starting it again does nothing but print "already running" | that is the single-instance guard: it focuses the running window instead |
| `--reset-login` says "already running" | quit the player first; the guard will not delete the login of a running instance |
| "Cannot reach ..." on start | the Jellyfin server is down or the URL is wrong; fix it with **Change account** |
| "Login expired" | the saved token was revoked: log in again |
| No sound | check the sink with `pactl list short sinks`; mpv uses `--ao=pulse,alsa` |
| A track fails to decode | the player skips it and continues with the next random song |
| Covers missing | the library has no artwork for that release; a placeholder is shown |

## Desktop app

```bash
./install.sh                 # menu entry + icon (~/.local/share, no root needed)
./install.sh --uninstall     # remove them again
```

Details are in [Install / run](#desktop-entry-application-menu) above. The launcher
template is `simplejellymus.desktop`; `install.sh` replaces the `@APPDIR@`
placeholder with the real project path before installing it.
