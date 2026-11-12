# SPDX-License-Identifier: GPL-2.0-or-later
"""Entry point."""

import logging
import os
import sys


def main():
    logging.basicConfig(level=os.environ.get('TICKETDESK_LOG', 'WARNING').upper(),
                        format='%(levelname)s %(name)s: %(message)s')
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    gi.require_version('Secret', '1')
    from .ui.application import Application
    return Application().run(sys.argv)


if __name__ == '__main__':
    sys.exit(main())
