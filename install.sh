#!/usr/bin/env bash
# Install SimpleJellyMus as a desktop app: a menu entry plus its icon.
#
#   ./install.sh              install (or update) the launcher and the icon
#   ./install.sh --uninstall  remove the launcher and the icon again
#
# Everything goes into ~/.local/share for the current user, so no root is needed.
# The launcher entry is generated from simplejellymus.desktop by replacing the
# "@APPDIR@" placeholder with the real path of this directory. The icon goes into
# the hicolor icon theme, downscaled to the sizes the menus and panels ask for.
set -euo pipefail

APP_ID="simplejellymus"
APP_NAME="SimpleJellyMus"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}"
APPS_DIR="$DATA_DIR/applications"
ICON_THEME_DIR="$DATA_DIR/icons/hicolor"
ENTRY="$APPS_DIR/$APP_ID.desktop"
ICON_NAME="$APP_ID.png"
TEMPLATE="$PROJECT_DIR/$APP_ID.desktop"

# The application icon: one PNG master, installed under the app id so that the
# launcher only has to say "Icon=simplejellymus". Point ICON_SOURCE at another
# file to use a different icon - main.py shows the same file in the window.
ICON_SOURCE="$PROJECT_DIR/assets/fire_icon_variant_1.png"
# Extra copies generated from the master for the menu, the panel and the window
# list. Generating them needs Pillow, which this app depends on anyway; without
# Pillow only the master itself is installed.
ICON_SIZES=(256 128 64 48 32 24 16)
# The master's real pixel size, filled in by install_app().
MASTER_PIXELS=""

say() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }

master_pixel_size() {
    # Read the width straight out of the PNG header - no image library needed.
    python3 - "$ICON_SOURCE" <<'PY'
import sys

PNG_MAGIC = bytes.fromhex("89504e470d0a1a0a")   # 0x89 P N G CR LF 0x1a LF
with open(sys.argv[1], "rb") as handle:
    header = handle.read(24)
if header[:8] != PNG_MAGIC:
    raise SystemExit(f"{sys.argv[1]} is not a PNG file")
print(int.from_bytes(header[16:20], "big"))
PY
}

install_icon() {
    # An icon theme entry is looked up by size, so every copy must live in a
    # directory named after its real pixel size.
    local target="$ICON_THEME_DIR/${MASTER_PIXELS}x${MASTER_PIXELS}/apps/$ICON_NAME"
    mkdir -p "$(dirname "$target")"
    # Replace instead of writing over the old file in place: a recreated file
    # makes the icon caches (GTK's theme cache, KDE's icon cache) notice the
    # change, while an overwrite often leaves the previous picture in the menu.
    rm -f "$target"
    cp -f "$ICON_SOURCE" "$target"
    chmod 644 "$target"
    say "  icon:     $target (${MASTER_PIXELS}x${MASTER_PIXELS} master)"

    if ! python3 -c 'import PIL' >/dev/null 2>&1; then
        warn "Pillow is not installed - only the ${MASTER_PIXELS}x${MASTER_PIXELS} icon was installed"
        return 0
    fi
    local size scaled
    for size in "${ICON_SIZES[@]}"; do
        (( size < MASTER_PIXELS )) || continue
        scaled="$ICON_THEME_DIR/${size}x${size}/apps/$ICON_NAME"
        mkdir -p "$(dirname "$scaled")"
        rm -f "$scaled"
        python3 - "$ICON_SOURCE" "$scaled" "$size" <<'PY'
import sys

from PIL import Image

with Image.open(sys.argv[1]) as image:
    image.resize((int(sys.argv[3]), int(sys.argv[3])), Image.LANCZOS).save(sys.argv[2], optimize=True)
PY
        chmod 644 "$scaled"
    done
    say "  icons:    ${ICON_SIZES[*]} px copies of the same icon"
}

drop_stale_icon_cache() {
    # KDE answers icon lookups from ~/.cache/icon-cache.kcache. Replacing an
    # icon can leave the old (or a missing) picture in the menu while the panel
    # already shows the new one, so drop the cache whenever it is older than the
    # icons just installed. It is only a cache: KDE rebuilds it on demand.
    local cache="${XDG_CACHE_HOME:-$HOME/.cache}/icon-cache.kcache"
    [[ -f "$cache" ]] || return 0
    [[ -n "$(find "$ICON_THEME_DIR" -maxdepth 3 -type f -name "$ICON_NAME" -path "*/apps/*" \
        -newer "$cache" -print -quit 2>/dev/null || true)" ]] || return 0
    rm -f "$cache"
    say "  cache:    dropped the stale icon cache ($cache)"
}

refresh_caches() {
    # Menu and icon caches are optional; a missing tool is not an error.
    drop_stale_icon_cache
    command -v update-desktop-database >/dev/null 2>&1 \
        && update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
    command -v gtk-update-icon-cache >/dev/null 2>&1 \
        && gtk-update-icon-cache -f -t "$DATA_DIR/icons/hicolor" >/dev/null 2>&1 || true
    for tool in kbuildsycoca6 kbuildsycoca5; do
        if command -v "$tool" >/dev/null 2>&1; then
            "$tool" --noincremental >/dev/null 2>&1 || true
            break
        fi
    done
}

uninstall() {
    local installed
    rm -f "$ENTRY"
    say "Removed $ENTRY"
    # Every size this installer has ever written, so older installs go too.
    while IFS= read -r installed; do
        rm -f "$installed"
        say "Removed $installed"
    done < <(find "$ICON_THEME_DIR" -maxdepth 3 -type f -name "$ICON_NAME" -path "*/apps/*" 2>/dev/null || true)
    refresh_caches
    say "The program itself was left untouched in $PROJECT_DIR"
}

install_app() {
    [[ -f "$PROJECT_DIR/main.py" ]] || { warn "main.py not found next to install.sh"; exit 1; }
    [[ -f "$TEMPLATE" ]] || { warn "$TEMPLATE is missing"; exit 1; }
    [[ -f "$ICON_SOURCE" ]] || { warn "$ICON_SOURCE is missing"; exit 1; }
    MASTER_PIXELS="$(master_pixel_size 2>/dev/null || true)"
    [[ "$MASTER_PIXELS" =~ ^[0-9]+$ ]] || { warn "$ICON_SOURCE is not a readable PNG"; exit 1; }

    mkdir -p "$APPS_DIR"

    # Render the template with this project's absolute path.
    local rendered
    rendered="$(sed "s|@APPDIR@|$PROJECT_DIR|g" "$TEMPLATE")"
    printf '%s\n' "$rendered" > "$ENTRY"
    chmod 644 "$ENTRY"

    say "Installed $APP_NAME"
    say "  launcher: $ENTRY"
    say "  command:  python3 $PROJECT_DIR/main.py"
    install_icon

    if command -v desktop-file-validate >/dev/null 2>&1; then
        desktop-file-validate "$ENTRY" || warn "desktop-file-validate reported the above"
    fi
    refresh_caches

    say ""
    say "Find it in the application menu (Audio / Music) or pin it to the panel."
    say "Starting it while it already runs just brings the running window to the front."
    say ""
    say "If the menu still shows no icon, restart the desktop shell once:"
    say "  kquitapp6 plasmashell && plasmashell --no-respawn"
}

case "${1:-}" in
    ""|--install) install_app ;;
    --uninstall|-u) uninstall ;;
    -h|--help)
        sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
        ;;
    *) warn "unknown option: $1 (try --help)"; exit 2 ;;
esac
