# SPDX-License-Identifier: GPL-2.0-or-later
"""Preferences: connection (shared with the top bar extension), projects, list size, new
tickets, cache."""

from gi.repository import Adw, Gtk

from ..core import secrets
from ..core.api import JiraClient
from ..core.config import normalize_site, parse_projects, read_extension_settings
from .util import human_size


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, window):
        super().__init__(title='Preferences')
        self.window = window
        app = window.app
        cfg = app.config
        page = Adw.PreferencesPage()
        self.add(page)

        conn = Adw.PreferencesGroup(
            title='Connection',
            description='Create an API token at id.atlassian.com → Security → API tokens (not the "with scopes" kind). '
                        'The token is kept in GNOME Keyring and shared with the top bar extension.')
        page.add(conn)
        self.site = Adw.EntryRow(title='Site URL', text=cfg.site, show_apply_button=True)
        self.site.connect('apply', self._apply_account)
        self.email = Adw.EntryRow(title='Account Email', text=cfg.email, show_apply_button=True)
        self.email.connect('apply', self._apply_account)
        self.token = Adw.PasswordEntryRow(title='API Token', show_apply_button=True)
        self.token.connect('apply', self._apply_token)
        self.test_row = Adw.ActionRow(title='Connection', subtitle='Not tested', use_markup=False)
        test = Gtk.Button(label='Test', valign=Gtk.Align.CENTER)
        test.connect('clicked', self._test)
        self.test_row.add_suffix(test)
        for r in (self.site, self.email, self.token, self.test_row):
            conn.add(r)

        self.ext = read_extension_settings()
        if self.ext and self.ext.get('site'):
            imp = Adw.ButtonRow(title='Use Settings from Top Bar Extension')
            imp.connect('activated', self._import)
            conn.add(imp)

        lists = Adw.PreferencesGroup(title='Ticket Lists')
        page.add(lists)
        self.projects = Adw.EntryRow(title='Projects for My Tickets (empty = all)',
                                     text=', '.join(cfg.projects), show_apply_button=True)
        self.projects.connect('apply', self._apply_projects)
        lists.add(self.projects)
        size = Adw.SpinRow.new_with_range(20, 500, 10)
        size.set_title('Tickets per List')
        size.set_value(cfg.list_size)
        size.connect('notify::value', lambda r, _p: self._set_list_size(int(r.get_value())))
        lists.add(size)
        live = Adw.SpinRow.new_with_range(10, 600, 5)
        live.set_title('Live View Refresh (seconds)')
        live.set_subtitle('Live View is switched on in the sort menu; it works when sorted by Updated, newest first')
        live.set_value(cfg.live_interval)
        live.connect('notify::value', lambda r, _p: self._set_timer('live_interval', int(r.get_value())))
        lists.add(live)

        notes = Adw.PreferencesGroup(
            title='Notifications',
            description='Ticket Desk checks Jira for activity and shows it under the bell. '
                        'Your own changes never notify you.')
        page.add(notes)
        for attr, title, subtitle in (
                ('notify_mentions', 'Mentions of Me', 'In comments and descriptions'),
                ('notify_updates', 'Updates to My Tickets',
                 'Comments, status changes and assignments on tickets you are assigned to, reported, created or watch')):
            row = Adw.SwitchRow(title=title, subtitle=subtitle, active=getattr(cfg, attr))
            row.connect('notify::active', lambda r, _p, a=attr: self._set_timer(a, r.get_active()))
            notes.add(row)
        desktop = Adw.SwitchRow(title='Desktop Notifications', active=cfg.notify_desktop,
                                subtitle='Also announce them on the desktop while Ticket Desk is in the background. '
                                         'The top bar extension can do this too; keep one of them on to avoid doubles.')
        desktop.connect('notify::active', lambda r, _p: self._set('notify_desktop', r.get_active()))
        notes.add(desktop)
        every = Adw.SpinRow.new_with_range(1, 60, 1)
        every.set_title('Check Every (minutes)')
        every.set_value(cfg.notify_interval)
        every.connect('notify::value', lambda r, _p: self._set_timer('notify_interval', int(r.get_value())))
        notes.add(every)

        new = Adw.PreferencesGroup(title='New Tickets')
        page.add(new)
        assign = Adw.SwitchRow(title='Assign New Tickets to Me', active=cfg.assign_to_me,
                               subtitle='Otherwise new tickets start unassigned')
        assign.connect('notify::active', lambda r, _p: self._set('assign_to_me', r.get_active()))
        new.add(assign)
        self.default_project = Adw.ComboRow(title='Default Project', use_markup=False, enable_search=True)
        self._project_keys = ['']
        self.default_project.set_model(Gtk.StringList.new(['Project of the current list or board']))
        self.default_project.connect('notify::selected', self._default_project_changed)
        new.add(self.default_project)
        window.store.create_projects(self._fill_default_projects)

        cache = Adw.PreferencesGroup(title='Offline Cache',
                                     description='Tickets, comments and image previews you have opened, for instant display and offline reading.')
        page.add(cache)
        self.cache_row = Adw.ActionRow(title='Cache', use_markup=False)
        clear = Gtk.Button(label='Clear', valign=Gtk.Align.CENTER, css_classes=['destructive-action'])
        clear.connect('clicked', self._clear_cache)
        self.cache_row.add_suffix(clear)
        cache.add(self.cache_row)
        self._update_cache_stats()
        self._token_state()

    # ----- new tickets -----

    def _set(self, name, value):
        cfg = self.window.app.config
        setattr(cfg, name, value)
        cfg.save()

    def _set_timer(self, name, value):
        """A setting that changes what or how often the app checks."""
        self._set(name, value)
        self.window.restart_timers()
        self.window.bell.refresh()
        self.window.filter_bar.sync()

    def _fill_default_projects(self, projects):
        cfg = self.window.app.config
        keys = [''] + [p['key'] for p in projects]
        if cfg.create_project and cfg.create_project not in keys:
            keys.append(cfg.create_project)
        names = {p['key']: p.get('name') for p in projects}
        labels = ['Project of the current list or board'] + [
            f'{names.get(k)} ({k})' if names.get(k) else k for k in keys[1:]]
        self._project_keys = []  # ignore selection changes while the model is replaced
        self.default_project.set_model(Gtk.StringList.new(labels))
        self.default_project.set_selected(keys.index(cfg.create_project) if cfg.create_project in keys else 0)
        self._project_keys = keys

    def _default_project_changed(self, row, _pspec):
        i = row.get_selected()
        if 0 <= i < len(self._project_keys):
            self._set('create_project', self._project_keys[i])

    # ----- account -----

    def _apply_account(self, *_):
        cfg = self.window.app.config
        cfg.site = normalize_site(self.site.get_text())
        cfg.email = self.email.get_text().strip()
        self.site.set_text(cfg.site)
        cfg.save()
        self.window.app.connect_account(account_changed=True)
        self._token_state()

    def _token_state(self):
        cfg = self.window.app.config
        if not cfg.configured:
            self.token.set_title('API Token')
            return

        def found(token):
            self.token.set_title('API Token (saved in keyring)' if token else 'API Token (not saved)')
        self.window.app.worker.run(secrets.lookup_token, cfg.site, cfg.email, on_done=found,
                                   on_error=lambda e: self.token.set_title(f'API Token (keyring error: {e})'))

    def _apply_token(self, *_):
        cfg = self.window.app.config
        token = self.token.get_text().strip()
        if not cfg.configured:
            self.add_toast(Adw.Toast(title='Enter the site URL and email first'))
            return
        if not token:
            return

        def stored(_ok):
            self.token.set_text('')
            self.add_toast(Adw.Toast(title='Token saved to GNOME Keyring'))
            self._token_state()
            self.window.app.connect_account()
        self.window.app.worker.run(secrets.store_token, cfg.site, cfg.email, token, on_done=stored,
                                   on_error=lambda e: self.add_toast(Adw.Toast(title=f'Keyring error: {e}')))

    def _test(self, *_):
        cfg = self.window.app.config
        self.test_row.set_subtitle('Testing…')

        def check():
            token = secrets.lookup_token(cfg.site, cfg.email)
            if not token:
                raise RuntimeError('No token saved for this site and email')
            return JiraClient(cfg.site, cfg.email, token).myself()
        self.window.app.worker.run(check, on_done=lambda me: self.test_row.set_subtitle(f'Connected as {me.get("displayName")}'),
                                   on_error=lambda e: self.test_row.set_subtitle(f'Failed: {e}'))

    def _import(self, *_):
        self.site.set_text(self.ext['site'])
        self.email.set_text(self.ext['email'])
        if self.ext.get('projects') and not self.projects.get_text().strip():
            self.projects.set_text(', '.join(self.ext['projects']))
            self._apply_projects()
        self._apply_account()

    # ----- lists -----

    def _apply_projects(self, *_):
        cfg = self.window.app.config
        cfg.projects = parse_projects(self.projects.get_text())
        self.projects.set_text(', '.join(cfg.projects))
        cfg.save()
        self.window.reload_list()

    def _set_list_size(self, n):
        cfg = self.window.app.config
        cfg.list_size = n
        cfg.save()

    # ----- cache -----

    def _update_cache_stats(self):
        def done(st):
            self.cache_row.set_subtitle(f'{st["issues"]} tickets · {human_size(st["db_bytes"])} data · '
                                        f'{human_size(st["file_bytes"])} images and files')
        self.window.app.worker.run(self.window.app.cache.stats, on_done=done)

    def _clear_cache(self, *_):
        def done(_r):
            self._update_cache_stats()
            self.add_toast(Adw.Toast(title='Cache cleared'))
            self.window.reload_list()
        self.window.app.worker.run(self.window.app.cache.clear, on_done=done)

