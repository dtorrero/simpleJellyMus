#!/usr/bin/env bash
# Install SimpleJellyMus as a desktop app: a menu entry plus its icon.
#
#   ./install.sh              install (or update) the launcher and the icon
#   ./install.sh --uninstall  remove the launcher and the icon again
#
# Everything goes into ~/.local/share for the current user, so no root is needed.
# The launcher entry is generated from simplejellymus.desktop by replacing the
# "@APPDIR@" placeholder with the real path of this directory.
set -euo pipefail

APP_ID="simplejellymus"
APP_NAME="SimpleJellyMus"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}"
APPS_DIR="$DATA_DIR/applications"
ICON_DIR="$DATA_DIR/icons/hicolor/256x256/apps"
ENTRY="$APPS_DIR/$APP_ID.desktop"
ICON_TARGET="$ICON_DIR/$APP_ID.png"
ICON_SOURCE="$PROJECT_DIR/assets/$APP_ID.png"
TEMPLATE="$PROJECT_DIR/$APP_ID.desktop"

say() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }

refresh_caches() {
    # Menu and icon caches are optional; a missing tool is not an error.
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
    rm -f "$ENTRY" "$ICON_TARGET"
    refresh_caches
    say "Removed $ENTRY"
    say "Removed $ICON_TARGET"
    say "The program itself was left untouched in $PROJECT_DIR"
}

install_app() {
    [[ -f "$PROJECT_DIR/main.py" ]] || { warn "main.py not found next to install.sh"; exit 1; }
    [[ -f "$TEMPLATE" ]] || { warn "$TEMPLATE is missing"; exit 1; }
    [[ -f "$ICON_SOURCE" ]] || { warn "$ICON_SOURCE is missing"; exit 1; }

    mkdir -p "$APPS_DIR" "$ICON_DIR"

    # Render the template with this project's absolute path.
    local rendered
    rendered="$(sed "s|@APPDIR@|$PROJECT_DIR|g" "$TEMPLATE")"
    printf '%s\n' "$rendered" > "$ENTRY"
    chmod 644 "$ENTRY"
    cp -f "$ICON_SOURCE" "$ICON_TARGET"
    chmod 644 "$ICON_TARGET"

    if command -v desktop-file-validate >/dev/null 2>&1; then
        desktop-file-validate "$ENTRY" || warn "desktop-file-validate reported the above"
    fi
    refresh_caches

    say "Installed $APP_NAME"
    say "  launcher: $ENTRY"
    say "  icon:     $ICON_TARGET"
    say "  command:  python3 $PROJECT_DIR/main.py"
    say ""
    say "Find it in the application menu (Audio / Music) or pin it to the panel."
    say "Starting it while it already runs just brings the running window to the front."
}

case "${1:-}" in
    ""|--install) install_app ;;
    --uninstall|-u) uninstall ;;
    -h|--help)
        sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
        ;;
    *) warn "unknown option: $1 (try --help)"; exit 2 ;;
esac
