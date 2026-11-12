# SPDX-License-Identifier: GPL-2.0-or-later
"""Application: single instance, opens ticketdesk://browse/KEY links, owns config/cache/store."""

import logging
from pathlib import Path

from gi.repository import GLib, Adw, Gdk, Gio, Gtk

from ..const import APP_ID, APP_NAME, VERSION
from ..core import secrets
from ..core.cache import Cache
from ..core.config import Config, cache_dir, read_extension_settings
from ..core.store import Store
from ..core.worker import Worker
from .prefs import PreferencesDialog
from .window import Window

log = logging.getLogger(__name__)


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.config = None
        self.cache = None
        self.worker = None
        self.store = None
        self.window = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        self.config = Config.load()
        self.cache = Cache(cache_dir() / 'cache.db')
        self.worker = Worker()
        self.store = Store(self.config, self.cache, self.worker)

        ext = read_extension_settings()
        if ext:
            self.store.mentioned = list(dict.fromkeys(list(ext.get('mentioned') or []) + self.store.mentioned))
            if not self.config.configured and ext.get('site') and ext.get('email'):
                # First run next to the top bar extension: reuse its connection settings.
                self._apply_extension(ext)

        provider = Gtk.CssProvider()
        provider.load_from_path(str(Path(__file__).with_name('style.css')))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        for name, cb, accels in (('preferences', self._preferences, ['<Control>comma']),
                                 ('about', self._about, []),
                                 ('quit', lambda *_: self.quit(), ['<Control>q'])):
            act = Gio.SimpleAction.new(name, None)
            act.connect('activate', cb)
            self.add_action(act)
            if accels:
                self.set_accels_for_action(f'app.{name}', accels)

        # Desktop notifications open their ticket through this action
        open_ticket = Gio.SimpleAction.new('open-ticket', GLib.VariantType('s'))
        open_ticket.connect('activate', self._open_ticket)
        self.add_action(open_ticket)

    def _open_ticket(self, _action, value):
        win = self._window()
        win.present()
        key = value.get_string()
        if key:
            self.store.mark_notifications_read(key=key)
            win.open_key(key)
            win.bell.refresh()

    def _window(self):
        if self.window is None:
            self.window = Window(self)
            self.connect_account()
        return self.window

    def do_activate(self):
        self._window().present()

    def do_open(self, files, _n, _hint):
        win = self._window()
        win.present()
        for f in files:
            key = win.key_from_uri(f.get_uri())
            if key:
                win.open_key(key)

    def do_shutdown(self):
        if self.config:
            self.config.save()
        if self.worker:
            self.worker.shutdown()
        Adw.Application.do_shutdown(self)

    # ----- account -----

    def connect_account(self, account_changed=False):
        cfg = self.config
        if account_changed:
            self.store.me = {}
            self.cache.put_meta('myself', {})
        if not cfg.configured:
            self.store.set_credentials(None)
            if self.window:
                self.window.on_connected()
            return

        def found(token):
            try:
                self.store.set_credentials(token)
            except ValueError as e:
                self.store.set_credentials(None)
                if self.window:
                    self.window.toast(str(e))
            if self.window:
                self.window.on_connected()

        def failed(e):
            log.warning('keyring: %s', e)
            self.store.set_credentials(None)
            if self.window:
                self.window.toast(f'Could not read GNOME Keyring: {e}')
                self.window.on_connected()
        self.worker.run(secrets.lookup_token, cfg.site, cfg.email, on_done=found, on_error=failed)

    def _apply_extension(self, ext):
        self.config.site = ext['site']
        self.config.email = ext['email']
        if ext.get('projects'):
            self.config.projects = ext['projects']
        self.config.save()

    def import_extension_settings(self):
        ext = read_extension_settings()
        if ext and ext.get('site') and ext.get('email'):
            self._apply_extension(ext)
            self.connect_account(account_changed=True)

    # ----- dialogs -----

    def _preferences(self, *_):
        PreferencesDialog(self._window()).present(self._window())

    def _about(self, *_):
        about = Adw.AboutDialog(application_name=APP_NAME, application_icon=APP_ID, version=VERSION,
                                comments='A fast GNOME client for Jira Cloud.\n'
                                         'Not affiliated with or endorsed by Atlassian. Jira is a trademark of Atlassian.',
                                license_type=Gtk.License.GPL_2_0, developer_name='Ticket Desk contributors')
        about.present(self._window())
