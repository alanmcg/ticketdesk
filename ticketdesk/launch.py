# SPDX-License-Identifier: GPL-2.0-or-later
"""Start Ticket Desk on a ticket from outside the app (the browser helper)."""

import os
import subprocess
import sys
from pathlib import Path

from .const import APP_ID, URI_SCHEME


def ticket_uri(key):
    return f'{URI_SCHEME}://browse/{key}'


def open_in_app(key):
    """Show the ticket in Ticket Desk (starting it if needed). True on success."""
    uri = ticket_uri(key)
    try:
        import gi
        gi.require_version('Gio', '2.0')
        from gi.repository import Gio
        app = Gio.DesktopAppInfo.new(f'{APP_ID}.desktop')
        if app and app.launch_uris([uri], Gio.AppLaunchContext()):
            return True
    except Exception as e:  # noqa: BLE001 - ImportError, GLib.Error: try running it directly
        print(f'ticketdesk: {e}', file=sys.stderr)
    # Not installed (running from the source tree): start it from here. A running
    # instance gets the ticket through D-Bus and this process ends at once.
    root = Path(__file__).resolve().parent.parent
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [str(root), os.environ.get('PYTHONPATH')])))
    try:
        subprocess.Popen([sys.executable, '-m', 'ticketdesk', uri], env=env, start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError as e:
        print(f'ticketdesk: {e}', file=sys.stderr)
        return False
