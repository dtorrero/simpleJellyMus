# SimpleJellyMus

A tiny desktop **random Jellyfin music player** written in Python, made for my
machine (Manjaro / X11 / KDE, 1920x1080).

It logs in **once**, then plays endless random **audio** tracks from your
Jellyfin library, preloading the next song so there is no waiting between
tracks. The screen shows the album cover, the song information, a progress bar
and play/pause/next/previous controls. If you want to steer it, press `G` and
tell it to play only the styles you feel like (or everything *except* one).

> Music only: the library query asks Jellyfin for `IncludeItemTypes=Audio`, every
> item is validated (no videos, no music videos), and mpv runs with
> `--no-video --vid=no --audio-display=no`. Video files are ignored entirely.
<img width="1920" height="1080" alt="SJM_example" src="https://github.com/user-attachments/assets/c4e7cd1f-80eb-440f-a4dc-dc013e9b3ac8" />
---

## What it does

SimpleJellyMus turns your Jellyfin library into a radio station that never
stops: you tell it once which server and account to use, and from then on it
just plays - one song after another, picked at random from your collection,
with the cover art on screen and no silence between tracks.

**The music**

* Plays **music only**. Films, concerts and music videos in your library are
  ignored on purpose, and mpv is told never to open a video window.
* Never runs out: it keeps fetching new random songs for as long as you leave it on.
* Doesn't loop the same few songs: recently played tracks are skipped and the
  same artist is not played twice in a row.
* No waiting between songs: the next track is downloaded in the background while
  the current one plays, and handed to mpv so the two run into each other.
* A broken file or a hiccup on the server never stops playback: it tells you
  what happened and moves on to another song.

**The window**

* Album cover, song title, artist and album, plus a progress bar you can click
  to jump anywhere in the song.
* Play / pause, next, previous, restart, volume and seeking - as buttons and as
  keys. Press `?` (or the **?** button, top right) to see every shortcut.
* Windowed or fullscreen, and it remembers the size and mode you left it in.
* Nothing on the screen ever jumps: a very long title shrinks and is shortened
  with `…` instead of pushing the artwork, the progress bar or the buttons around.

**Choosing what to hear** (optional - random is the default)

* Press `G` to open the style panel: play only *Ska Punk*, only *Black Metal*,
  a whole family such as *Metal*, or everything *except* *Schlager* once you have
  had enough of it.
* Tick as many styles as you like, then choose whether a song must match **any
  of them**, **all of them** or **none of them**. Family buttons add a whole
  branch in one click.
* Searching tolerates typos (`trash metl` finds *Thrash Metal*), and the panel
  always shows how many songs the current choice would play before you commit.
* It never interrupts the song that is playing: a new choice takes effect from
  the next track on.
* The choice is remembered, so starting the app again plays the same way - and
  `Clear` + `Play these` takes you straight back to plain random.

**Things you don't have to think about**

* You log in once. Your password is never stored; only an access token, in your
  own config file (permissions `600`).
* Starting the app a second time never gives you two players fighting over the
  speakers: it brings the running window to the front instead.
* It can live in your application menu with its own icon (`./install.sh`).
* Nothing in your library is ever modified. The player only reads from Jellyfin,
  and the style feature only reads a database file that it built on your machine.

> The style feature is the only part that needs a one-time preparation step:
> `python3 styles/prepare_styles.sh` (details in
> [Play by style](#play-by-style) and `styles/README.md`). It is optional -
> without it the player simply plays random songs from the whole library, and
> everything above still works.

**In this document:**
[Requirements](#requirements) ·
[Install / run](#install--run) ·
[First run](#first-run) ·
[Controls](#controls) ·
[How it works](#how-it-works) ·
[File layout](#file-layout) ·
[Self-test](#self-test-no-jellyfin-server-needed) ·
[Configuration and cache](#configuration-and-cache) ·
[Troubleshooting](#troubleshooting) ·
[Desktop app](#desktop-app)

## Requirements

You need a **Jellyfin server you can reach** and an account on it; the app asks
for the address and login the first time you start it (see
[First run](#first-run)) and never asks again.

| Component | Status on this machine |
|---|---|
| Python 3.9+ (with `tkinter`) | ✅ 3.14.7 |
| `mpv` (playback engine, used as an audio-only subprocess) | ✅ 0.41.0 |
| Pillow (`PIL`) for cover art | ✅ 12.3.0 |

Needing to install one of them? On this machine `sudo pacman -S mpv` and, on
other distributions, `sudo apt install mpv python3-tk` or
`sudo dnf install mpv python3-tkinter` are usually enough.

Everything else is Python's standard library (`urllib`, `json`, `socket`,
`threading`, `tkinter`). No `pip install` is required for the current setup
(`requirements.txt` just documents the optional dependency).

The optional **style catalog** needs no extra Python package either: it is built
by the scripts in `styles/`, which do want `ffprobe` on your PATH and - only for
the optional language-model step - a DeepSeek API key (see `styles/README.md`).
The result is a single SQLite file of roughly 75 MB for a 41,000-track library.
Playing needs none of that: the player only reads the finished file.

## Install / run

```bash
git clone https://github.com/dtorrero/simpleJellyMus.git
cd simpleJellyMus
python3 main.py
```

The first start asks for your Jellyfin address and login (see
[First run](#first-run)); after that, starting the app means just running it and
music begins. Closing the window (or `Q`, or the **Quit** button) stops it.

Useful flags:

```bash
python3 main.py --fullscreen     # start in fullscreen (Esc switches back and forth)
python3 main.py --windowed       # start windowed (this is the default)
python3 main.py --login          # force the login screen
python3 main.py --reset-login    # forget the saved login and exit
python3 main.py --debug          # verbose Jellyfin + mpv logging
python3 main.py --style "Ska Punk"                     # only that style
python3 main.py --style "Black Metal" --style "Ska Punk"  # either of the two
python3 main.py --style "family:Metal"                 # a whole family
python3 main.py --style "Ska Punk" --style-mode all    # both at once
python3 main.py --style "Schlager" --style-mode not    # everything but that
```

### Play by style

Random by style instead of random everything, picked from a **local catalog**
(`~/.local/share/simplejellymus/catalog.sqlite`, built once with
`styles/prepare_styles.sh` — see `styles/README.md`). Style names are matched
fuzzily, aliases and small typos included, and the catalog is opened
**read-only**: the player never writes to the dataset or to your music.

| `--style` accepts | Example |
|---|---|
| a style name | `--style "Ska Punk"` |
| an alias | `--style "outrun"` → Synthwave |
| a family | `--style "family:Metal"` (a style with the same name wins: `--style Metal` is the style, `family:Metal` the family) |

`--style-mode` decides what several styles mean together: `any` (default, plays
whatever matches at least one), `all` (must carry every style) or `not` (plays
everything *except* the selection). Lookups are single-digit milliseconds
against the local file, so a filtered queue refills instantly; without
`--style` (and without a catalog) the player behaves exactly as before. An
unknown name is refused with a suggestion instead of playing the wrong thing:

```bash
$ python3 main.py --style "trash metl"
Cannot filter by style: Unknown style 'trash metl' - did you mean 'Thrash Metal'?
```

**Building the catalog** (once, optional):

```bash
cd simpleJellyMus
python3 styles/prepare_styles.sh      # reads your music files -> catalog.sqlite
```

It takes a while for a big library and needs to see your music (the `styles/`
folder explains how to point it at your files and at Jellyfin). It writes only
`catalog.sqlite` plus a report in `styles/out/`: your music files, your Jellyfin
server and the player's own settings are never modified, and the player opens
the database **read-only**. Every step, and which of them are optional, is
documented in `styles/README.md`.

A selection made in the player with `G` is saved to the config file and restored
on the next start, so you can keep a "normal" filter and only change it when you
feel like it. `--style` overrides it for that single run, so a one-off
`python3 main.py --style "Black Metal"` leaves your usual choice alone.

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
| `G` | play by style (open the style panel) |
| `?` | keyboard help (also the **?** button in the top right) |
| `Esc` | close an open panel, otherwise switch windowed ⇄ fullscreen |
| `F` | switch windowed ⇄ fullscreen |
| `Q` (or `Ctrl+Q`) | quit |

The on-screen buttons do the same: click the cover art area's transport
buttons, or click the progress bar to seek; **Change account** and **Quit** sit
in the top right next to **?**.

### Play by style in the player

`G` opens a panel that lists the styles of the local catalog: search them by
typing (typos are tolerated), tick as many as you like, add a whole family with
one of the family chips, and choose whether the selection means *any of them*,
*all of them* or *everything but them*. The line at the bottom always shows how
many tracks the current combination would play, and `Play these` applies it.

| In the panel | Action |
|---|---|
| type | filter the list (search asks the local catalog, never the server) |
| `↓` / `↑` (in the search box) | move into the list |
| click / `Space` | tick or untick the highlighted style (clicking again untickes) |
| `Enter` | play what is ticked — or, with nothing ticked, the style you typed |
| `Clear` | empty the selection: `Play these` then means *any random song* again |
| `Cancel` / `Esc` | close without changing anything |

The song that is playing is never interrupted: a new selection applies from the
next track on. If the track already preloaded next does not match, it is
replaced while the current one keeps playing (it usually has minutes left).
The selection is remembered in the config file, so it is still there after a
restart.

## How it works

```
Tkinter UI (ui.py) ──state snapshots── PlayerEngine (player.py)
                                          ├── MpvPlayer   mpv JSON IPC (audio only)
                                          ├── Preloader   next track -> disk cache
                                          ├── PlayQueue   random batches, no repeats
                                          │     batches come from JellyfinClient (random)
                                          │     or from catalog.py (when a style is picked)
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
* **Play by style** changes only one thing: where the queue gets its random songs
  from. Instead of asking Jellyfin for a batch, `catalog.py` reads the local
  SQLite file (read-only) and returns a random batch of the matching tracks -
  0.2-2 ms for a 41k-track library, where a random request to the server takes
  about 2.5 s. The rest of the engine is untouched: the preloaded next track is
  replaced only when a new selection rules it out, **Next** skips a song that is
  queued but outside the filter, and **Previous** still walks your real history.
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
| `catalog.py` | read-only access to the local style catalog (styles, families, search, filtered random batches) |
| `player.py` | mpv IPC client, preloader cache, random play queue, engine |
| `ui.py` | Tkinter login screen and fullscreen player UI (plus the `?` help and the style panel) |
| `selftest.py` | offline self-test: fake Jellyfin server + generated audio (see below) |
| `styles/` | the standalone builder that produces `catalog.sqlite` (scan → link → vocabulary → normalize → optional LLM → report); it is never used while playing |
| `install.sh` | installs/removes the menu entry and the icon (see below) |
| `simplejellymus.desktop` | launcher template (`@APPDIR@` is filled in by `install.sh`) |
| `assets/fire_icon_variant_1.png` | application icon (1024×1024 master, installed in several sizes) |

## Self-test (no Jellyfin server needed)

`selftest.py` starts a small fake Jellyfin server (with decoy video items),
generates short WAV tracks, and verifies the whole pipeline: login, the
music-only filter, cover download, the preload cache, gapless auto-advance,
next/previous, pause, volume, seeking, the UI layout (long titles never move
anything), the `?` help card, the style panel (search, family chips,
`any`/`all`/`not`, applying and cancelling a choice, the "nothing matches" case,
recalling the last choice), the catalog queries themselves, and the
single-instance guard - **over 200 checks**, all offline.

```bash
python3 selftest.py            # ~1 minute, silent (volume 0)
python3 -u selftest.py | tail -5
```

It writes everything to a throwaway directory, so your real login, library and
style database are never touched. Set `SELFTEST_DEBUG=1` for verbose mpv logging.
A freshly built `catalog.sqlite` is not required: the checks build their own
tiny one.


## Configuration and cache

| Path | Content |
|---|---|
| `~/.config/simplejellymus/config.json` | server URL, username, access token, user id, device id, volume, window mode and size, and the style filter picked with `G` |
| `~/.cache/simplejellymus/audio/` | preloaded (next) tracks, pruned automatically |
| `~/.cache/simplejellymus/covers/` | album art cache |
| `~/.local/share/simplejellymus/catalog.sqlite` | the style dataset (only if you built it with `styles/`); opened read-only, the player never writes to it |
| `$XDG_RUNTIME_DIR/simplejellymus.sock` | single-instance guard (removed when the app quits) |
| `~/.local/share/applications/simplejellymus.desktop` | menu entry created by `install.sh` |
| `~/.local/share/icons/hicolor/<size>x<size>/apps/simplejellymus.png` | icon installed by `install.sh` (one copy per size) |

Delete the config file (or run `--reset-login`) to change accounts. Nothing in the
repository itself ever holds your credentials — the token only lives in the config
file above (mode `600`), and `.gitignore` additionally refuses to stage a stray
`config.json`, `*.token` or `*.log`.

To get rid of a style filter, press `G` in the player and use `Clear` →
`Play these`; that clears the saved entry too. If a saved style has disappeared
from the catalog, the player says `ignoring the style filter` when it starts and
simply plays random songs - a stale config never keeps the music from starting.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `mpv was not found in PATH` | `sudo pacman -S mpv` (or `sudo apt install mpv` / `sudo dnf install mpv`) |
| Starting it again does nothing but print "already running" | that is the single-instance guard: it focuses the running window instead |
| `--reset-login` says "already running" | quit the player first; the guard will not delete the login of a running instance |
| "Cannot reach ..." on start | the Jellyfin server is down or the URL is wrong; fix it with **Change account** |
| "Login expired" | the saved token was revoked: log in again |
| No sound | check the sink with `pactl list short sinks`; mpv uses `--ao=pulse,alsa` |
| A track fails to decode | the player skips it and continues with the next random song |
| Covers missing | the library has no artwork for that release; a placeholder is shown |
| `G` shows "No style catalog yet" | the dataset was never built: run `python3 styles/prepare_styles.sh` once (see `styles/README.md`). Until then the player plays random songs from the whole library |
| It only plays one style and you never asked for it | a choice made with `G` is remembered across restarts: press `G`, pick something else, or `Clear` → `Play these` for plain random |
| A saved style no longer exists in the catalog | the player prints `[engine] ignoring the style filter: …` at start and keeps playing; choose a new one with `G` |
| `--style` prints "Unknown style … did you mean …?" | the name is not in the catalog: use the suggested name, or `family:Name` for a whole family |
| `--style` prints "matches no tracks" | that combination is empty (for example `all` of two unrelated styles): remove one of them, or switch to `any` |
| Filtered playback repeats the same songs | the selection is small by nature; add another style or a family (the panel shows the count before you commit) |
| Can't find a style in the panel | type part of the name - the search also looks at aliases (`outrun` finds *Synthwave*) and forgives typos |

## Desktop app

```bash
./install.sh                 # menu entry + icon (~/.local/share, no root needed)
./install.sh --uninstall     # remove them again
```

Details are in [Install / run](#desktop-entry-application-menu) above. The launcher
template is `simplejellymus.desktop`; `install.sh` replaces the `@APPDIR@`
placeholder with the real project path before installing it.
