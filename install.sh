#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Installs Ticket Desk for the current user, without root:
#   ~/.local/bin/ticketdesk                         the command
#   ~/.local/share/ticketdesk/                      the program
#   ~/.local/share/applications/<app id>.desktop    menu entry, handler for ticketdesk:// links
#   ~/.local/share/icons/hicolor/…                  icons
#   ~/.local/share/metainfo/<app id>.metainfo.xml   description for software centres
#   ~/.mozilla/native-messaging-hosts/ticketdesk.json  helper for the Firefox extension
# and copies a signed Firefox extension (browser/firefox/web-ext-artifacts/*.xpi), if
# there is one, to ~/.local/share/ticketdesk/ for installing in Firefox.
#
# Usage: ./install.sh [--deps] [--no-deps-check] [--uninstall] [--help]
#        PYTHON=/usr/bin/python3.12 ./install.sh   (another system Python)

set -euo pipefail

APP_ID=io.github.ticketdesk.TicketDesk
SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}
BIN_DIR=$HOME/.local/bin
PKG_DIR=$DATA_HOME/ticketdesk
APPS_DIR=$DATA_HOME/applications
ICON_DIR=$DATA_HOME/icons/hicolor
META_DIR=$DATA_HOME/metainfo

say() { printf '%s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
Install Ticket Desk for $USER (no root needed).

  ./install.sh                  install or update
  ./install.sh --deps           first install the system libraries it needs (uses sudo)
  ./install.sh --no-deps-check  install even if libraries seem to be missing
  ./install.sh --uninstall      remove it again (keeps settings, cache and the stored token)
EOF
}

DEPS=0 CHECK=1 UNINSTALL=0
for arg in "$@"; do
    case $arg in
        --deps) DEPS=1 ;;
        --no-deps-check) CHECK=0 ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

[ "$(id -u)" -ne 0 ] || die "run this as your own user, not root: it installs into your home directory"

# The system Python: it is the one with the GTK bindings (a pyenv or conda python is not).
# PYTHON=/path/to/python3 ./install.sh picks another.
if [ -z "${PYTHON:-}" ]; then
    if [ -x /usr/bin/python3 ]; then PYTHON=/usr/bin/python3; else PYTHON=$(command -v python3) || die "python3 not found"; fi
fi

refresh_caches() {
    if command -v update-desktop-database >/dev/null; then update-desktop-database -q "$APPS_DIR" || true; fi
    if command -v gtk4-update-icon-cache >/dev/null && [ -f "$ICON_DIR/index.theme" ]; then
        gtk4-update-icon-cache -q -t -f "$ICON_DIR" || true
    fi
    touch "$ICON_DIR" 2>/dev/null || true
}

# ----- uninstall -----

if [ "$UNINSTALL" -eq 1 ]; then
    if [ -d "$PKG_DIR/ticketdesk" ]; then
        PYTHONPATH=$PKG_DIR "$PYTHON" -P -m ticketdesk.nativehost --uninstall >/dev/null || true
    fi
    if [ -f "$BIN_DIR/ticketdesk" ] && grep -q 'ticketdesk.main' "$BIN_DIR/ticketdesk"; then
        rm -f "$BIN_DIR/ticketdesk"
    fi
    rm -rf "$PKG_DIR/ticketdesk"
    rm -f "$PKG_DIR/ticketdesk-links.xpi" \
          "$APPS_DIR/$APP_ID.desktop" \
          "$ICON_DIR/scalable/apps/$APP_ID.svg" \
          "$ICON_DIR/symbolic/apps/$APP_ID-symbolic.svg" \
          "$META_DIR/$APP_ID.metainfo.xml"
    rmdir "$PKG_DIR" 2>/dev/null || true
    refresh_caches
    say "Ticket Desk removed."
    say "Kept: settings (${XDG_CONFIG_HOME:-$HOME/.config}/ticketdesk), cache (${XDG_CACHE_HOME:-$HOME/.cache}/ticketdesk)"
    say "and the API token in your keyring (Passwords and Keys, search for Ticket Desk)."
    say "The Firefox extension, if installed, is removed in Firefox: Add-ons Manager (Ctrl+Shift+A)."
    exit 0
fi

# ----- dependencies -----

distro_packages() {
    local ids=""
    if [ -r /etc/os-release ]; then ids=$(. /etc/os-release; echo "${ID:-} ${ID_LIKE:-}"); fi
    case " $ids " in
        *" arch "*) echo "pacman -S --needed python python-gobject gtk4 libadwaita libsecret webkitgtk-6.0" ;;
        *" fedora "*|*" rhel "*) echo "dnf install python3 python3-gobject gtk4 libadwaita libsecret webkitgtk6.0" ;;
        *" debian "*|*" ubuntu "*) echo "apt install python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-secret-1 gir1.2-webkit-6.0" ;;
        *) echo "" ;;
    esac
}

missing_deps() {
    "$PYTHON" - <<'EOF'
import sys
missing = []
if sys.version_info < (3, 11):
    missing.append(f'Python 3.11 or newer (found {sys.version.split()[0]})')
try:
    import gi
except ImportError:
    print('PyGObject (Python GTK bindings)')
    sys.exit()
for name, version, label in (('Gtk', '4.0', 'GTK 4'), ('Adw', '1', 'libadwaita'),
                             ('Secret', '1', 'libsecret'), ('WebKit', '6.0', 'WebKitGTK 6.0')):
    try:
        gi.require_version(name, version)
        __import__('gi.repository', fromlist=[name])
    except (ValueError, ImportError):
        missing.append(f'{label} (GObject introspection data)')
try:
    from gi.repository import Adw, Gtk
    if (Gtk.get_major_version(), Gtk.get_minor_version()) < (4, 14):
        missing.append(f'GTK 4.14 or newer (found 4.{Gtk.get_minor_version()})')
    if (Adw.get_major_version(), Adw.get_minor_version()) < (1, 6):
        missing.append(f'libadwaita 1.6 or newer (found 1.{Adw.get_minor_version()})')
except Exception:
    pass
print('\n'.join(missing))
EOF
}

if [ "$DEPS" -eq 1 ]; then
    cmd=$(distro_packages)
    [ -n "$cmd" ] || die "don't know the package names for this distribution; install GTK 4, libadwaita, libsecret, WebKitGTK 6.0 and PyGObject"
    say "Installing system libraries: sudo $cmd"
    # shellcheck disable=SC2086  # the package list is meant to be split
    sudo $cmd
fi

if [ "$CHECK" -eq 1 ]; then
    missing=$(missing_deps)
    if [ -n "$missing" ]; then
        say "Missing for $PYTHON:" >&2
        printf '%s\n' "$missing" | sed 's/^/  - /' >&2
        cmd=$(distro_packages)
        [ -z "$cmd" ] || say "Install them with:  sudo $cmd   (or run ./install.sh --deps)" >&2
        exit 1
    fi
fi

# ----- files -----

[ -f "$SRC/ticketdesk/main.py" ] || die "run this from the Ticket Desk source directory (ticketdesk/main.py not found)"

mkdir -p "$BIN_DIR" "$PKG_DIR" "$APPS_DIR" "$ICON_DIR/scalable/apps" "$ICON_DIR/symbolic/apps" "$META_DIR"

# Program: replace the previous copy as a whole, so removed modules do not linger
tmp=$(mktemp -d "$PKG_DIR/.install.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
cp -r "$SRC/ticketdesk" "$tmp/ticketdesk"
find "$tmp/ticketdesk" -name __pycache__ -type d -prune -exec rm -rf {} +
rm -rf "$PKG_DIR/ticketdesk"
mv "$tmp/ticketdesk" "$PKG_DIR/ticketdesk"

# (Python commands run with -P and PYTHONPATH: the installed copy, not this source directory)
VERSION=$(PYTHONPATH=$PKG_DIR "$PYTHON" -P -c 'from ticketdesk.const import VERSION; print(VERSION)')

# Command, desktop entry and metainfo from the templates also used by the packages
SRC=$SRC PKG_DIR=$PKG_DIR BIN_DIR=$BIN_DIR APPS_DIR=$APPS_DIR META_DIR=$META_DIR \
APP_ID=$APP_ID VERSION=$VERSION PYTHON=$PYTHON "$PYTHON" - <<'EOF'
import os
import re
from pathlib import Path

e = os.environ
src = Path(e['SRC'])


def write(path, text, mode=0o644):
    path = Path(path)
    tmp = path.with_name(path.name + '.new')
    tmp.write_text(text)
    tmp.chmod(mode)
    os.replace(tmp, path)


def exec_arg(arg):
    """One Exec= argument, quoted as the Desktop Entry spec asks."""
    if re.fullmatch(r'[A-Za-z0-9_@+=:,./-]+', arg):
        quoted = arg
    else:
        quoted = '"' + re.sub(r'(["`$\\])', r'\\\1', arg) + '"'
    return quoted.replace('\\', '\\\\').replace('%', '%%')


launcher = (src / 'bin/ticketdesk.in').read_text() \
    .replace('@PYTHON@', e['PYTHON']).replace('@pkgdatadir@', e['PKG_DIR'])
command = Path(e['BIN_DIR']) / 'ticketdesk'
write(command, launcher, 0o755)

desktop = (src / 'data/ticketdesk.desktop.in').read_text() \
    .replace('@APP_ID@', e['APP_ID']).replace('@VERSION@', e['VERSION'])
# Absolute path: ~/.local/bin is not on PATH in every session
exec_line = f"TryExec={str(command).replace(chr(92), chr(92) * 2)}\nExec={exec_arg(str(command))} "
desktop, found = re.subn(r'^Exec=ticketdesk ', lambda _m: exec_line, desktop, count=1, flags=re.M)
assert found, 'Exec line not found in data/ticketdesk.desktop.in'
write(Path(e['APPS_DIR']) / f"{e['APP_ID']}.desktop", desktop)

meta = (src / 'data/ticketdesk.metainfo.xml.in').read_text() \
    .replace('@APP_ID@', e['APP_ID']).replace('@VERSION@', e['VERSION'])
write(Path(e['META_DIR']) / f"{e['APP_ID']}.metainfo.xml", meta)
EOF

install -m 644 "$SRC/data/icons/hicolor/scalable/apps/ticketdesk.svg" "$ICON_DIR/scalable/apps/$APP_ID.svg"
install -m 644 "$SRC/data/icons/hicolor/symbolic/apps/ticketdesk-symbolic.svg" "$ICON_DIR/symbolic/apps/$APP_ID-symbolic.svg"

# ticketdesk:// links (the top bar extension and the Firefox extension use them)
if command -v gio >/dev/null; then
    gio mime x-scheme-handler/ticketdesk "$APP_ID.desktop" >/dev/null 2>&1 || true
elif command -v xdg-mime >/dev/null; then
    xdg-mime default "$APP_ID.desktop" x-scheme-handler/ticketdesk || true
fi
refresh_caches

# Helper for the Firefox extension (also used by Firefox web apps from PWAsForFirefox)
PYTHONPATH=$PKG_DIR "$PYTHON" -P -m ticketdesk.nativehost --install >/dev/null

# Signed Firefox extension, if one was built
xpi=""
if [ -d "$SRC/browser/firefox/web-ext-artifacts" ]; then
    xpi=$(ls -t "$SRC"/browser/firefox/web-ext-artifacts/*.xpi 2>/dev/null | head -n 1 || true)
fi
if [ -n "$xpi" ]; then
    install -m 644 "$xpi" "$PKG_DIR/ticketdesk-links.xpi"
fi

# ----- report -----

say "Ticket Desk $VERSION installed."
say "  Command:       $BIN_DIR/ticketdesk"
say "  Menu entry:    $APPS_DIR/$APP_ID.desktop"
say "  Browser helper: $HOME/.mozilla/native-messaging-hosts/ticketdesk.json"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) say ""
       say "Note: $BIN_DIR is not on your PATH, so 'ticketdesk' does not work in a terminal yet"
       say "(the menu entry does). Most distributions add it at login once the folder exists:"
       say "log out and in. Otherwise add this line to ~/.profile:"
       say "  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac
if command -v ticketdesk >/dev/null && [ "$(command -v ticketdesk)" != "$BIN_DIR/ticketdesk" ]; then
    say ""
    say "Note: 'ticketdesk' on your PATH is $(command -v ticketdesk), not this installation."
fi
say ""
if [ -n "$xpi" ]; then
    say "Firefox extension: $PKG_DIR/ticketdesk-links.xpi"
    say "  Install it in each Firefox window or web app that should use it:"
    say "  Add-ons Manager (Ctrl+Shift+A) → gear menu → Install Add-on From File…"
else
    say "Firefox extension: no signed build found in browser/firefox/web-ext-artifacts."
    say "  Sign it once (see README, 'Ticket links in Firefox'), then run ./install.sh again."
fi
say "If Ticket Desk is running, quit it and start it again to use the new version."
