# SPDX-License-Identifier: GPL-2.0-or-later
"""Native messaging host for the "Ticket Desk Links" browser extension (browser/firefox).

The browser starts this program for each request and talks to it over stdin/stdout: each
message is a 32-bit length in native byte order followed by that many bytes of UTF-8 JSON.

Requests:
  {"get": "site"}      -> {"ok": true, "site": "https://…", "version": "…"}
  {"open": "ABC-123"}  -> {"ok": true|false}

Installed packages register it system-wide (/usr/lib/mozilla/native-messaging-hosts);
install.sh and `python3 -m ticketdesk.nativehost --install` (from a source checkout)
register it for the current user, `--uninstall` removes that again."""

import json
import os
import shlex
import struct
import sys
from pathlib import Path

NAME = 'ticketdesk'                   # what the extension passes to sendNativeMessage
EXTENSION_ID = '@ticketdesk-links'    # browser/firefox/manifest.json
MAX_MESSAGE = 1024 * 1024


def read_message(stream):
    raw = stream.read(4)
    if len(raw) < 4:
        return None
    (size,) = struct.unpack('=I', raw)
    if size > MAX_MESSAGE:
        raise ValueError(f'message too large: {size} bytes')
    data = stream.read(size)
    if len(data) < size:
        return None
    return json.loads(data.decode('utf-8'))


def write_message(stream, message):
    data = json.dumps(message).encode('utf-8')
    stream.write(struct.pack('=I', len(data)) + data)
    stream.flush()


def handle(message):
    from .const import VERSION
    from .core.config import Config
    from .core.links import KEY_RE
    if not isinstance(message, dict):
        return {'ok': False, 'error': 'not a request'}
    if message.get('get') == 'site':
        return {'ok': True, 'site': Config.load().site, 'version': VERSION}
    key = message.get('open')
    if isinstance(key, str) and KEY_RE.match(key):
        from .launch import open_in_app
        return {'ok': open_in_app(key)}
    return {'ok': False, 'error': 'unknown request'}


def serve(inp, out):
    while True:
        try:
            message = read_message(inp)
        except ValueError as e:  # bad JSON or size: answer once, then stop reading this stream
            write_message(out, {'ok': False, 'error': str(e)})
            return
        if message is None:
            return
        write_message(out, handle(message))


def _private_stdio():
    """Keep the browser's pipes for this program only. Ticket Desk, when started from here,
    gets /dev/null instead: output of its own on the pipe would corrupt the messages, and the
    pipe closes when the browser is done with this host."""
    inp = os.fdopen(os.dup(0), 'rb', buffering=0)   # os.dup: not inherited by children
    out = os.fdopen(os.dup(1), 'wb')
    err = os.fdopen(os.dup(2), 'w', buffering=1)
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    os.close(null)
    sys.stdin, sys.stdout, sys.stderr = open(os.devnull), open(os.devnull, 'w'), err
    return inp, out


# ----- per-user registration for a source checkout -----

def _user_manifest():
    return Path.home() / '.mozilla' / 'native-messaging-hosts' / f'{NAME}.json'


def _user_launcher():
    return Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'ticketdesk' / 'native-host'


def manifest(path):
    return {'name': NAME,
            'description': 'Opens Jira tickets in Ticket Desk',
            'path': str(path),
            'type': 'stdio',
            'allowed_extensions': [EXTENSION_ID]}


def install_user():
    root = Path(__file__).resolve().parent.parent
    launcher = _user_launcher()
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text('#!/bin/sh\n'
                        f'# Ticket Desk browser helper (Ticket Desk in {root})\n'
                        f'PYTHONPATH={shlex.quote(str(root))} exec {shlex.quote(sys.executable)} '
                        '-P -m ticketdesk.nativehost "$@"\n')
    launcher.chmod(0o755)
    path = _user_manifest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest(launcher), indent=2) + '\n')
    return path, launcher


def uninstall_user():
    removed = []
    for path in (_user_manifest(), _user_launcher()):
        try:
            path.unlink()
            removed.append(path)
        except FileNotFoundError:
            pass
    return removed


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if '--install' in argv:
        path, launcher = install_user()
        print(f'Registered the browser helper for this user:\n  {path}\n  {launcher}')
        return 0
    if '--uninstall' in argv:
        removed = uninstall_user()
        print('Removed:\n' + '\n'.join(f'  {p}' for p in removed) if removed else 'Nothing to remove')
        return 0
    # Started by the browser (arguments: manifest path, extension id)
    inp, out = _private_stdio()
    try:
        serve(inp, out)
    except BrokenPipeError:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
