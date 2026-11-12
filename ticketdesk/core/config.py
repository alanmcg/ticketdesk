# SPDX-License-Identifier: GPL-2.0-or-later
"""User configuration (JSON in the XDG config dir) and import from the top bar extension."""

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Ticket fields shown in Details until the user picks others
DEFAULT_SHOWN_FIELDS = ('assignee', 'reporter', 'issuetype', 'created', 'updated')


def config_dir():
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'ticketdesk'


def cache_dir():
    return Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'ticketdesk'


def normalize_site(url):
    url = (url or '').strip().rstrip('/')
    if url and '://' not in url:
        url = f'https://{url}'
    return url


def parse_projects(text):
    return [p for p in (s.strip().upper() for s in (text or '').replace(',', ' ').split()) if p]


@dataclass
class Config:
    site: str = ''
    email: str = ''
    projects: list = field(default_factory=list)
    list_size: int = 100
    show_todo: bool = True
    show_progress: bool = True
    show_done: bool = False
    sort: str = 'created'
    sort_desc: bool = True
    recent: list = field(default_factory=list)
    window_width: int = 1280
    window_height: int = 820
    sidebar_visible: bool = True
    sidebar_width: int = 240
    list_width: int = 400
    board_ticket_height: int = 0  # ticket panel under a board; 0: about 45 % of the window
    filters_visible: bool = True
    shown_fields: list = field(default_factory=lambda: list(DEFAULT_SHOWN_FIELDS))
    details_expanded: bool = False
    bookmarks: list = field(default_factory=list)  # [{'kind': 'project', 'key', 'name'} | {'kind': 'board', 'id', ...}]
    swimlanes: dict = field(default_factory=dict)  # board id (str) -> 'none' | 'assignee' | 'parent' | ...
    # Notifications
    notify_mentions: bool = True
    notify_updates: bool = True        # comments, status changes and assignments on your tickets
    notify_desktop: bool = False       # also as desktop notifications (the extension may already do this)
    notify_interval: int = 5           # minutes between checks
    live_view: bool = False            # refresh the list by itself (only when sorted by updated, newest first)
    live_interval: int = 60            # seconds
    # New tickets
    create_project: str = ''          # '' = the project of the current list or board, else the first
    assign_to_me: bool = True
    pinned_fields: dict = field(default_factory=dict)  # project key -> [field ids] shown (and required) on create

    @property
    def configured(self):
        return bool(self.site and self.email)

    @classmethod
    def load(cls, path=None):
        path = Path(path or config_dir() / 'config.json')
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as e:
            log.warning('ignoring unreadable config %s: %s', path, e)
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        cfg = cls(**known)
        cfg.site = normalize_site(cfg.site)
        return cfg

    def save(self, path=None):
        path = Path(path or config_dir() / 'config.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.replace(tmp, path)  # atomic: never leaves a half-written config

    def add_recent(self, key, limit=50):
        self.recent = [key] + [k for k in self.recent if k != key][:limit - 1]

    # ----- bookmarks (projects and boards) -----

    @staticmethod
    def bookmark_id(entry):
        return (entry['kind'], str(entry.get('key') if entry['kind'] == 'project' else entry.get('id')))

    def is_bookmarked(self, entry):
        bid = self.bookmark_id(entry)
        return any(self.bookmark_id(b) == bid for b in self.bookmarks)

    def toggle_bookmark(self, entry):
        """Add or remove; returns True if the entry is now bookmarked."""
        bid = self.bookmark_id(entry)
        if self.is_bookmarked(entry):
            self.bookmarks = [b for b in self.bookmarks if self.bookmark_id(b) != bid]
            return False
        self.bookmarks = self.bookmarks + [dict(entry)]
        return True


def _extension_schema_dirs():
    roots = [Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'gnome-shell/extensions',
             Path('/usr/share/gnome-shell/extensions')]
    for root in roots:
        if not root.is_dir():
            continue
        for ext in root.iterdir():
            schemas = ext / 'schemas'
            if (schemas / 'gschemas.compiled').exists() and \
               (schemas / 'org.gnome.shell.extensions.jira-panel.gschema.xml').exists():
                yield schemas


def read_extension_settings():
    """Site, email, projects and mentioned keys from the top bar extension, or None."""
    from gi.repository import Gio

    from ..const import EXTENSION_SCHEMA
    for schemas in _extension_schema_dirs():
        try:
            source = Gio.SettingsSchemaSource.new_from_directory(
                str(schemas), Gio.SettingsSchemaSource.get_default(), False)
            schema = source.lookup(EXTENSION_SCHEMA, False)
            if not schema:
                continue
            s = Gio.Settings.new_full(schema, None, None)
            keys = schema.list_keys()
            return {
                'site': normalize_site(s.get_string('site-url')),
                'email': s.get_string('email').strip(),
                'projects': parse_projects(s.get_string('projects')),
                'mentioned': list(s.get_strv('mentioned-keys')) if 'mentioned-keys' in keys else [],
            }
        except Exception as e:  # noqa: BLE001 - optional integration
            log.info('cannot read extension settings in %s: %s', schemas, e)
    return None
