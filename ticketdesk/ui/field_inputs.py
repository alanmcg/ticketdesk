# SPDX-License-Identifier: GPL-2.0-or-later
"""Rows for entering Jira field values (used when creating tickets):
choice lists, multiple choice, people, dates, numbers, text and parent tickets; and the
person search popover (also used to assign tickets)."""

import datetime

from gi.repository import Adw, GLib, GObject, Gtk, Pango

from ..core import fields as F
from .util import label


class PersonPopover(Gtk.Popover):
    """Search for a person. Before typing it offers you and, if allowed, nobody.
    on_pick(account_id, name) is called with the choice (None, label for nobody)."""

    def __init__(self, store, on_pick, project=None, issue_key=None, assignable=False, allow_none=True,
                 none_label='Unassigned'):
        super().__init__()
        self.store = store
        self.on_pick = on_pick
        self.project = project
        self.issue_key = issue_key
        self.assignable = assignable
        self.allow_none = allow_none
        self.none_label = none_label
        self._serial = 0
        self.search = Gtk.SearchEntry(placeholder_text='Search people', search_delay=300)
        self.search.connect('search-changed', self._search)
        self.search.connect('activate', self._activate_first)
        self.results = Gtk.ListBox(css_classes=['navigation-sidebar'], selection_mode=Gtk.SelectionMode.NONE)
        self.results.connect('row-activated', self._picked)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, width_request=300)
        box.append(self.search)
        box.append(Gtk.ScrolledWindow(child=self.results, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      propagate_natural_height=True, max_content_height=320))
        self.set_child(box)
        self.connect('show', lambda *_: self._opened())

    def _person_row(self, account_id, name, detail=''):
        row = Gtk.ListBoxRow()
        row.pick = (account_id, name)
        box = Gtk.Box(spacing=10, margin_top=4, margin_bottom=4, margin_start=4)
        av = Adw.Avatar(size=24, text=name if account_id else '', show_initials=True)
        if not account_id:
            av.set_icon_name('avatar-default-symbolic')
        box.append(av)
        box.append(label(name, ellipsize=Pango.EllipsizeMode.END))
        if detail:
            box.append(label(detail, css=('dim-label', 'caption')))
        row.set_child(box)
        return row

    def _clear(self):
        while (r := self.results.get_row_at_index(0)) is not None:
            self.results.remove(r)

    def _opened(self):
        self.search.set_text('')
        self._fill_defaults()
        self.search.grab_focus()

    def _fill_defaults(self):
        self._clear()
        me = self.store.me
        if me.get('accountId'):
            self.results.append(self._person_row(me['accountId'], me.get('displayName') or 'Me', 'Me'))
        if self.allow_none:
            self.results.append(self._person_row(None, self.none_label))

    def _search(self, entry):
        text = entry.get_text().strip()
        self._serial += 1
        serial = self._serial
        if not text:
            self._fill_defaults()
            return

        def got(users):
            if serial != self._serial:
                return
            self._clear()
            for u in users:
                self.results.append(self._person_row(u['accountId'], u.get('displayName') or u['accountId'],
                                                     u.get('emailAddress') or ''))
            if not users:
                row = Gtk.ListBoxRow(child=label('No one found', css=('dim-label',)), activatable=False)
                self.results.append(row)

        def failed(e):
            if serial == self._serial:
                self._clear()
                self.results.append(Gtk.ListBoxRow(child=label(str(e), css=('dim-label',), wrap=True), activatable=False))
        self.store.find_users(text, self.project, self.assignable, got, failed, issue_key=self.issue_key)

    def _activate_first(self, _entry):
        row = self.results.get_row_at_index(0)
        if row is not None and hasattr(row, 'pick'):
            self._picked(self.results, row)

    def _picked(self, _list, row):
        if not hasattr(row, 'pick'):
            return
        self.popdown()
        self.on_pick(*row.pick)


class UserRow(Adw.ActionRow):
    """A person: the row shows who is chosen; clicking it opens a search."""

    __gsignals__ = {'changed': (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, store, title, project=None, assignable=False, allow_none=True, none_label='Unassigned'):
        super().__init__(title=title, use_markup=False)
        self.none_label = none_label
        self.account_id = None
        self.avatar = Adw.Avatar(size=28, show_initials=True)
        self.add_prefix(self.avatar)
        self.popover = PersonPopover(store, self.set, project=project, assignable=assignable,
                                     allow_none=allow_none, none_label=none_label)
        self.button = Gtk.MenuButton(icon_name='document-edit-symbolic', popover=self.popover, valign=Gtk.Align.CENTER,
                                     css_classes=['flat'], tooltip_text=f'Choose {title.lower()}')
        self.add_suffix(self.button)
        self.set_activatable_widget(self.button)
        self.set(None, None)

    @property
    def project(self):
        return self.popover.project

    @project.setter
    def project(self, value):
        self.popover.project = value

    def set(self, account_id, name):
        self.account_id = account_id
        self.name = name if account_id else None
        shown = name if account_id else self.none_label
        self.set_subtitle(GLib.markup_escape_text(shown or ''))
        self.avatar.set_text(name if account_id else '')
        self.avatar.set_icon_name(None if account_id else 'avatar-default-symbolic')
        self.emit('changed')

    def value(self):
        return self.account_id


class FieldInput:
    """One Jira field as a libadwaita row. `value()` gives the JSON Jira expects, or None."""

    def __init__(self, store, meta, project, pinned=False, on_pin=None):
        self.meta = meta
        self.fid = meta.get('fieldId') or meta.get('key')
        self.name = meta.get('name') or self.fid
        schema = meta.get('schema') or {}
        self.kind = None
        values = [v for v in meta.get('allowedValues') or [] if not v.get('disabled')]
        if values and schema.get('type') == 'array':
            self.kind = 'multi'
            self.row = Adw.ActionRow(title=self.name, use_markup=False)
            self.checks = []
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            for v in values:
                c = Gtk.CheckButton(label=F.option_label(v))
                c.connect('toggled', lambda *_: self._multi_changed())
                self.checks.append((str(v['id']), c))
                box.append(c)
            pop = Gtk.Popover(child=Gtk.ScrolledWindow(child=box, propagate_natural_height=True,
                                                       max_content_height=320, hscrollbar_policy=Gtk.PolicyType.NEVER))
            btn = Gtk.MenuButton(icon_name='pan-down-symbolic', popover=pop, valign=Gtk.Align.CENTER, css_classes=['flat'])
            self.row.add_suffix(btn)
            self.row.set_activatable_widget(btn)
            self._multi_changed()
        elif values:
            self.kind = 'choice'
            self.ids = [None] + [str(v['id']) for v in values]
            labels = ['None'] + [F.option_label(v) for v in values]
            self.row = Adw.ComboRow(title=self.name, model=Gtk.StringList.new(labels), use_markup=False,
                                    enable_search=len(values) > 12)
            d = meta.get('defaultValue')
            if meta.get('hasDefaultValue') and isinstance(d, dict) and str(d.get('id')) in self.ids:
                self.row.set_selected(self.ids.index(str(d['id'])))
        elif F.is_user(meta):
            self.kind = 'user'
            self.row = UserRow(store, self.name, project, allow_none=True, none_label='Nobody')
        else:
            t = schema.get('type')
            hint = {'number': ' (number)', 'date': ' (YYYY-MM-DD)', 'datetime': ' (YYYY-MM-DD hh:mm)'}.get(t, '')
            if F.is_parent(meta):
                hint = ' (ticket key)'
            elif t == 'array':
                hint = ' (comma separated)'
            self.kind = 'text'
            self.row = Adw.EntryRow(title=self.name + hint, use_markup=False)
            if t in ('date', 'datetime'):
                self._add_calendar(t)
        if on_pin is not None:
            pin = Gtk.ToggleButton(icon_name='view-pin-symbolic', active=pinned, valign=Gtk.Align.CENTER,
                                   css_classes=['flat'],
                                   tooltip_text=(f'Always show and require for {project}' if not pinned
                                                 else f'Stop always showing for {project}'))
            pin.connect('toggled', lambda b: on_pin(self.fid, b.get_active()))
            self.row.add_suffix(pin)

    def _add_calendar(self, kind):
        cal = Gtk.Calendar()
        pop = Gtk.Popover(child=cal)

        def picked(c):
            d = c.get_date()
            day = datetime.date(d.get_year(), d.get_month(), d.get_day_of_month()).isoformat()
            self.row.set_text(day if kind == 'date' else f'{day} 09:00')
            pop.popdown()
        cal.connect('day-selected', picked)
        btn = Gtk.MenuButton(icon_name='x-office-calendar-symbolic', popover=pop, valign=Gtk.Align.CENTER,
                             css_classes=['flat'], tooltip_text='Pick a date')
        self.row.add_suffix(btn)

    def _multi_changed(self):
        chosen = [c.get_label() for _i, c in self.checks if c.get_active()]
        self.row.set_subtitle(GLib.markup_escape_text(', '.join(chosen) or 'None'))

    def value(self):
        meta = self.meta
        if self.kind == 'multi':
            return F.form_value(meta, [i for i, c in self.checks if c.get_active()])
        if self.kind == 'choice':
            i = self.row.get_selected()
            sel = self.ids[i] if 0 <= i < len(self.ids) else None
            return F.form_value(meta, [sel] if sel else [])
        if self.kind == 'user':
            aid = self.row.value()
            if not aid:
                return None
            return [{'accountId': aid}] if (meta.get('schema') or {}).get('type') == 'array' else {'accountId': aid}
        text = self.row.get_text().strip()
        t = (meta.get('schema') or {}).get('type')
        if t == 'datetime' and text and 'T' not in text:
            try:
                dt = datetime.datetime.strptime(text, '%Y-%m-%d %H:%M').astimezone()
                text = dt.strftime('%Y-%m-%dT%H:%M:00.000%z')
            except ValueError:
                pass
        return F.form_value(meta, text=text)

    def invalid(self):
        """Text that cannot be converted (e.g. a malformed number), else None."""
        if self.kind != 'text':
            return None
        text = self.row.get_text().strip()
        if not text:
            return None
        t = (self.meta.get('schema') or {}).get('type')
        if t == 'number' and self.value() is None:
            return f'{self.name}: not a number'
        if t == 'date':
            try:
                datetime.date.fromisoformat(text)
            except ValueError:
                return f'{self.name}: use YYYY-MM-DD'
        return None

    def set_error(self, on):
        if on:
            self.row.add_css_class('error')
        else:
            self.row.remove_css_class('error')
