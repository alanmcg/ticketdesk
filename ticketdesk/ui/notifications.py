# SPDX-License-Identifier: GPL-2.0-or-later
"""The bell: a button with a small dot while there are unread notifications, opening a
list of recent ones. Clicking one opens its ticket and marks it read."""

import time

from gi.repository import Gtk, Pango

from .util import label

ICONS = {'mention': 'mail-unread-symbolic', 'comment': 'document-edit-symbolic',
         'status': 'object-select-symbolic', 'assigned': 'avatar-default-symbolic'}


def ago(ms):
    s = max(0, time.time() - ms / 1000)
    if s < 60:
        return 'now'
    if s < 3600:
        return f'{int(s // 60)} min'
    if s < 86400:
        return f'{int(s // 3600)} h'
    if s < 7 * 86400:
        return f'{int(s // 86400)} d'
    return time.strftime('%Y-%m-%d', time.localtime(ms / 1000))


class Bell(Gtk.MenuButton):
    def __init__(self, window):
        super().__init__(tooltip_text='Notifications')
        self.window = window
        self.store = window.store
        icon = Gtk.Image.new_from_icon_name('preferences-system-notifications-symbolic')
        self.dot = Gtk.Box(css_classes=['notify-dot'], halign=Gtk.Align.END, valign=Gtk.Align.START,
                           width_request=8, height_request=8, visible=False, can_target=False)
        overlay = Gtk.Overlay(child=icon)
        overlay.add_overlay(self.dot)
        self.set_child(overlay)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=['notification-list'])
        self.list.connect('row-activated', self._activated)
        self.mark_all = Gtk.Button(label='Mark All Read', css_classes=['flat'])
        self.mark_all.connect('clicked', lambda *_: self._mark_all())
        check = Gtk.Button(icon_name='view-refresh-symbolic', css_classes=['flat'], tooltip_text='Check Now')
        check.connect('clicked', lambda *_: self.window.check_notifications(now=True))
        head = Gtk.Box(spacing=6, margin_start=8, margin_end=4, margin_top=4, margin_bottom=4)
        title = label('Notifications', css=('heading',))
        title.set_hexpand(True)
        head.append(title)
        head.append(check)
        head.append(self.mark_all)
        self.footer = label('', css=('dim-label', 'caption'), wrap=True)
        self.footer.set_margin_start(10)
        self.footer.set_margin_end(10)
        self.footer.set_margin_bottom(6)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, width_request=380)
        box.append(head)
        box.append(Gtk.ScrolledWindow(child=self.list, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      propagate_natural_height=True, max_content_height=480))
        box.append(self.footer)
        pop = Gtk.Popover(child=box)
        pop.connect('show', lambda *_: self._fill())
        self.set_popover(pop)

    def refresh(self):
        unread = self.store.unread_notifications()
        self.dot.set_visible(unread > 0)
        self.set_tooltip_text(f'Notifications ({unread} unread)' if unread else 'Notifications')
        self.update_property([Gtk.AccessibleProperty.LABEL],
                             [f'Notifications, {unread} unread' if unread else 'Notifications'])
        if self.get_popover().get_visible():
            self._fill()

    def _fill(self):
        while (row := self.list.get_row_at_index(0)) is not None:
            self.list.remove(row)
        cfg = self.window.app.config
        items = self.store.notifications()
        for n in items:
            self.list.append(self._row(n))
        if not items:
            self.list.append(Gtk.ListBoxRow(child=label('No notifications yet', css=('dim-label',), xalign=0.5),
                                            activatable=False))
        self.mark_all.set_sensitive(any(not n['read'] for n in items))
        if not (cfg.notify_mentions or cfg.notify_updates):
            self.footer.set_text('Notifications are turned off in Preferences.')
        else:
            what = ' and '.join(x for x in (('mentions of you' if cfg.notify_mentions else ''),
                                            ('updates to your tickets' if cfg.notify_updates else '')) if x)
            self.footer.set_text(f'Checking for {what} every {cfg.notify_interval} min.')

    def _row(self, n):
        row = Gtk.ListBoxRow()
        row.notification = n
        box = Gtk.Box(spacing=10, margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
        icon = Gtk.Image.new_from_icon_name(ICONS.get(n['kind'], 'dialog-information-symbolic'))
        icon.set_valign(Gtk.Align.START)
        icon.add_css_class('dim-label' if n['read'] else 'accent')
        box.append(icon)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        title = label(n['title'], css=() if n['read'] else ('heading',), wrap=True)
        text.append(title)
        if n.get('body'):
            body = label(n['body'].replace('\n', ' · '), css=('dim-label', 'caption'), wrap=True,
                         ellipsize=Pango.EllipsizeMode.END)
            body.set_lines(2)
            text.append(body)
        box.append(text)
        when = label(ago(n['time']), css=('dim-label', 'caption'))
        when.set_valign(Gtk.Align.START)
        box.append(when)
        row.set_child(box)
        row.update_property([Gtk.AccessibleProperty.LABEL],
                            [('Unread: ' if not n['read'] else '') + n['title']])
        return row

    def _activated(self, _list, row):
        n = getattr(row, 'notification', None)
        if not n:
            return
        self.get_popover().popdown()
        self.store.mark_notifications_read(key=n['key'])
        self.refresh()
        self.window.open_key(n['key'])

    def _mark_all(self):
        self.store.mark_notifications_read()
        self.refresh()
        self._fill()
