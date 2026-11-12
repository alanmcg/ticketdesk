# SPDX-License-Identifier: GPL-2.0-or-later
"""List filters in one compact row (wrapping when the pane is narrow), with the same
behaviour as the top bar extension:

- Status categories (To Do / In Progress / Done); at least one stays on.
- Created: any time, this week (Monday to today), last week (Monday to Sunday), or a
  From–To range picked in calendars. A date range lists everything in it (up to 200).
- Sort: Created, Updated, Priority, Status or Key, descending or ascending.
Status and sort are remembered; the date range lasts for the session.
"""

import datetime

from gi.repository import Adw, GLib, GObject, Gtk

from ..core.search import CATEGORIES, SORT_LABELS, SORTS, week_range
from .util import label

DATE_MODES = (('any', 'Any time'), ('this', 'This week'), ('last', 'Last week'))


def _linked(*buttons):
    box = Gtk.Box(css_classes=['linked'])
    for b in buttons:
        box.append(b)
    return box


def _wrap_box():
    """Adw.WrapBox (libadwaita 1.7) when available, otherwise a Gtk.FlowBox."""
    if hasattr(Adw, 'WrapBox'):
        return Adw.WrapBox(child_spacing=6, line_spacing=6)
    return Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False, column_spacing=6,
                       row_spacing=6, max_children_per_line=4, min_children_per_line=1)


class _DayPicker(Gtk.Box):
    """A titled calendar that emits 'picked' when a day is clicked, but not when the
    month or year arrows change the selected day."""

    __gsignals__ = {'picked': (GObject.SignalFlags.RUN_FIRST, None, (object,))}

    def __init__(self, title):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.append(label(title, css=('heading',)))
        self.calendar = Gtk.Calendar()
        self.append(self.calendar)
        self._navigated = False
        self._queued = False
        self._setting = False
        self.calendar.connect('day-selected', self._day_selected)
        for sig in ('next-month', 'prev-month', 'next-year', 'prev-year'):
            self.calendar.connect(sig, self._navigation)

    def set_date(self, d):
        d = d or datetime.date.today()
        dt = GLib.DateTime.new_local(d.year, d.month, d.day, 0, 0, 0)
        self._setting = True
        if hasattr(self.calendar, 'set_date'):  # GTK 4.20
            self.calendar.set_date(dt)
        else:
            self.calendar.select_day(dt)
        self._setting = False

    def _navigation(self, *_):
        self._navigated = True

    def _day_selected(self, _cal):
        if self._setting:
            return
        if not self._queued:  # decide after the navigation signal (if any) has run too
            self._queued = True
            GLib.idle_add(self._apply)

    def _apply(self):
        self._queued = False
        if self._navigated:
            self._navigated = False
            return GLib.SOURCE_REMOVE
        dt = self.calendar.get_date()
        self.emit('picked', datetime.date(dt.get_year(), dt.get_month(), dt.get_day_of_month()))
        return GLib.SOURCE_REMOVE


class FilterBar(Gtk.Box):
    __gsignals__ = {'changed': (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, config):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, css_classes=['filter-bar'],
                         margin_start=10, margin_end=10, margin_top=2, margin_bottom=6)
        self.config = config
        self.mode = 'any'
        self.range = None  # (from, to) dates, inclusive
        self._syncing = False
        wrap = _wrap_box()
        self.append(wrap)

        # Status categories
        self.cat_buttons = {}
        for cat, title in CATEGORIES:
            b = Gtk.ToggleButton(label=title)
            b.connect('toggled', self._category_toggled, cat)
            self.cat_buttons[cat] = b
        status = _linked(*self.cat_buttons.values())
        status.set_tooltip_text('Status categories to show')
        wrap.append(status)

        # Created date: a button that opens choices and two calendars
        self.date_buttons = {}
        for mode, title in DATE_MODES:
            b = Gtk.ToggleButton(label=title)
            b.connect('toggled', self._date_toggled, mode)
            self.date_buttons[mode] = b
        self.from_picker = _DayPicker('From')
        self.from_picker.connect('picked', self._picked, 'from')
        self.to_picker = _DayPicker('To')
        self.to_picker.connect('picked', self._picked, 'to')
        pickers = Gtk.Box(spacing=12)
        pickers.append(self.from_picker)
        pickers.append(self.to_picker)
        date_pop = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=6, margin_bottom=6,
                           margin_start=6, margin_end=6)
        quick = _linked(*self.date_buttons.values())
        quick.set_halign(Gtk.Align.CENTER)
        date_pop.append(quick)
        date_pop.append(pickers)
        self.date_label = Adw.ButtonContent(icon_name='x-office-calendar-symbolic')
        self.date_button = Gtk.MenuButton(child=self.date_label, popover=Gtk.Popover(child=date_pop),
                                          tooltip_text='Created date')
        wrap.append(self.date_button)

        # Sort: field and direction in one menu
        self.sort_checks = {}
        self.dir_checks = {}
        sort_pop = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                           margin_start=6, margin_end=6)
        sort_pop.append(label('Sort By', css=('heading',)))
        group = None
        for s in SORTS:
            c = Gtk.CheckButton(label=SORT_LABELS[s][0], group=group)
            group = group or c
            c.connect('toggled', self._sort_toggled, s)
            self.sort_checks[s] = c
            sort_pop.append(c)
        sort_pop.append(Gtk.Separator(margin_top=4, margin_bottom=4))
        dgroup = None
        for desc in (True, False):
            c = Gtk.CheckButton(group=dgroup)
            dgroup = dgroup or c
            c.connect('toggled', self._dir_toggled, desc)
            self.dir_checks[desc] = c
            sort_pop.append(c)
        sort_pop.append(Gtk.Separator(margin_top=4, margin_bottom=4))
        live_row = Gtk.Box(spacing=12)
        live_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        live_text.append(label('Live View'))
        self.live_hint = label('', css=('dim-label', 'caption'), wrap=True)
        self.live_hint.set_max_width_chars(28)
        live_text.append(self.live_hint)
        live_row.append(live_text)
        self.live_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.live_switch.connect('notify::active', self._live_toggled)
        live_row.append(self.live_switch)
        sort_pop.append(live_row)
        self.sort_label = Adw.ButtonContent()
        self.sort_button = Gtk.MenuButton(child=self.sort_label, popover=Gtk.Popover(child=sort_pop))
        wrap.append(self.sort_button)
        self._sync()

    # ----- state -----

    def created(self):
        """(from, to) dates, or None for any time."""
        return self.range if self.mode != 'any' and self.range else None

    def describe(self):
        """Short text for the list's info line (used when the bar is hidden)."""
        parts = []
        shown = [t for c, t in CATEGORIES if getattr(self.config, f'show_{c}')]
        if len(shown) < len(CATEGORIES):
            parts.append(', '.join(shown))
        rng = self.created()
        if rng:
            parts.append(f'created {rng[0].isoformat()} – {rng[1].isoformat()}')
        return parts

    def sort_description(self):
        name, desc, asc = SORT_LABELS.get(self.config.sort, SORT_LABELS['created'])
        return f'by {name.lower()}, {desc if self.config.sort_desc else asc}'

    def _date_text(self):
        if self.mode == 'any' or not self.range:
            return 'Any time'
        if self.mode in ('this', 'last'):
            return dict(DATE_MODES)[self.mode]
        a, b = self.range
        return a.isoformat() if a == b else f'{a.isoformat()} – {b.isoformat()}'

    def _sync(self):
        self._syncing = True
        cfg = self.config
        for cat, b in self.cat_buttons.items():
            b.set_active(getattr(cfg, f'show_{cat}'))
        for mode, b in self.date_buttons.items():
            b.set_active(mode == self.mode)
        rng = self.created()
        self.from_picker.set_date(rng[0] if rng else None)
        self.to_picker.set_date(rng[1] if rng else None)
        self.date_label.set_label(self._date_text())
        sort = cfg.sort if cfg.sort in SORTS else 'created'
        self.sort_checks[sort].set_active(True)
        name, desc, asc = SORT_LABELS[sort]
        self.dir_checks[True].set_label(desc[:1].upper() + desc[1:])
        self.dir_checks[False].set_label(asc[:1].upper() + asc[1:])
        self.dir_checks[cfg.sort_desc].set_active(True)
        self.sort_label.set_icon_name('view-sort-descending-symbolic' if cfg.sort_desc else 'view-sort-ascending-symbolic')
        self.sort_label.set_label(name)
        live_possible = sort == 'updated' and cfg.sort_desc
        self.live_switch.set_active(cfg.live_view)
        self.live_switch.set_sensitive(live_possible)
        self.live_hint.set_text(f'Refreshes the list every {cfg.live_interval} s' if live_possible
                                else 'Sort by Updated, newest first, to use it')
        if cfg.live_view and live_possible:
            self.sort_label.set_label(f'{name} · Live')
        self.sort_button.set_tooltip_text(f'Sorted {self.sort_description()}'
                                          + (' · live view' if cfg.live_view and live_possible else ''))
        self._syncing = False

    def sync(self):
        """Show settings changed elsewhere (Preferences)."""
        self._sync()

    def _changed(self):
        self._sync()
        self.emit('changed')

    # ----- handlers -----

    def _category_toggled(self, button, cat):
        if self._syncing:
            return
        cfg = self.config
        if not button.get_active() and not any(b.get_active() for c, b in self.cat_buttons.items() if c != cat):
            self._sync()  # never filter everything away
            return
        setattr(cfg, f'show_{cat}', button.get_active())
        cfg.save()
        self._changed()

    def _date_toggled(self, button, mode):
        if self._syncing:
            return
        if not button.get_active():
            self._sync()  # clicking the active choice keeps it
            return
        self.mode = mode
        self.range = week_range(0) if mode == 'this' else week_range(-1) if mode == 'last' else None
        self.date_button.popdown()
        self._changed()

    def _picked(self, _picker, date, which):
        today = datetime.date.today()
        start, end = self.range if self.mode != 'any' and self.range else (None, None)
        if which == 'from':
            start, end = date, end or today
        else:
            start, end = start or date, date
        if end < start:
            if which == 'from':
                end = start
            else:
                start = end
        self.mode, self.range = 'custom', (start, end)
        self._changed()  # the popover stays open so the other end can be picked too

    def _sort_toggled(self, check, sort):
        if self._syncing or not check.get_active():
            return
        self.config.sort = sort
        self.config.save()
        self._changed()

    def _live_toggled(self, switch, _pspec):
        if self._syncing:
            return
        self.config.live_view = switch.get_active()
        self.config.save()
        self._changed()

    def _dir_toggled(self, check, desc):
        if self._syncing or not check.get_active():
            return
        self.config.sort_desc = desc
        self.config.save()
        self._changed()
