# SPDX-License-Identifier: GPL-2.0-or-later
"""Ticket list: a recycling Gtk.ListView, so long lists stay fast."""

from gi.repository import Gio, GObject, Gtk, Pango

from .util import is_image, label, relative, status_category

CATEGORY_CSS = ('todo', 'progress', 'done')


class IssueItem(GObject.Object):
    __gtype_name__ = 'TicketDeskIssueItem'

    def __init__(self, data, me_id):
        super().__init__()
        f = data.get('fields') or {}
        self.key = data['key']
        self.summary = f.get('summary') or ''
        self.status = (f.get('status') or {}).get('name') or ''
        self.category = status_category(f)
        self.priority = (f.get('priority') or {}).get('name') or ''
        self.updated = f.get('updated') or ''
        self.assigned = bool(me_id) and (f.get('assignee') or {}).get('accountId') == me_id
        self.created = bool(me_id) and me_id in ((f.get('reporter') or {}).get('accountId'),
                                                 (f.get('creator') or {}).get('accountId'))
        self.watching = bool((f.get('watches') or {}).get('isWatching'))
        self.has_images = any(is_image(a) for a in f.get('attachment') or [])
        self.mentioned = False


class _Row(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=3, margin_top=6, margin_bottom=6,
                         margin_start=4, margin_end=4)
        top = Gtk.Box(spacing=8)
        self.key = label(css=('monospace', 'issue-key'))
        self.status = label(css=('tag',))
        top.append(self.key)
        top.append(self.status)
        spacer = Gtk.Box(hexpand=True)
        top.append(spacer)
        self.icons = {}
        for name, icon, tip in (('images', 'image-x-generic-symbolic', 'Has images'),
                                ('assigned', 'avatar-default-symbolic', 'Assigned to you'),
                                ('created', 'document-edit-symbolic', 'Created or reported by you'),
                                ('watching', 'view-reveal-symbolic', 'Watching'),
                                ('mentioned', 'mail-unread-symbolic', 'Mentioned you')):
            img = Gtk.Image.new_from_icon_name(icon)
            img.set_tooltip_text(tip)
            img.add_css_class('dim-label')
            img.update_property([Gtk.AccessibleProperty.LABEL], [tip])
            self.icons[name] = img
            top.append(img)
        self.updated = label(css=('dim-label', 'caption', 'numeric'), xalign=1.0)
        self.updated.set_width_chars(8)
        top.append(self.updated)
        self.summary = label(ellipsize=Pango.EllipsizeMode.END)
        self.append(top)
        self.append(self.summary)

    def bind(self, item):
        self.key.set_text(item.key)
        self.status.set_text(item.status)
        for c in CATEGORY_CSS:
            self.status.remove_css_class(c)
        self.status.add_css_class(item.category)
        self.summary.set_text(item.summary)
        self.summary.set_tooltip_text(item.summary)
        self.updated.set_text(relative(item.updated))
        self.icons['images'].set_visible(item.has_images)
        self.icons['assigned'].set_visible(item.assigned)
        self.icons['created'].set_visible(item.created)
        self.icons['watching'].set_visible(item.watching)
        self.icons['mentioned'].set_visible(item.mentioned)


class IssueList(Gtk.ScrolledWindow):
    __gsignals__ = {'activated': (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.model = Gio.ListStore(item_type=IssueItem)
        self.selection = Gtk.SingleSelection(model=self.model, autoselect=False, can_unselect=True)
        factory = Gtk.SignalListItemFactory()
        factory.connect('setup', lambda _f, li: li.set_child(_Row()))
        factory.connect('bind', lambda _f, li: li.get_child().bind(li.get_item()))
        # single-click-activate is off on purpose: with it, GTK selects whatever row the
        # pointer hovers, so the highlight would not stay on the open ticket. A click (or
        # the arrow keys) selects; selecting opens the ticket.
        self.view = Gtk.ListView(model=self.selection, factory=factory, single_click_activate=False)
        self.view.add_css_class('navigation-sidebar')
        self.view.add_css_class('issue-list')
        self.view.connect('activate', self._activate)
        self.selection.connect('selection-changed', self._selection_changed)
        self._quiet = False
        self.set_child(self.view)

    def _selection_changed(self, selection, _position, _n):
        if self._quiet:
            return
        item = selection.get_selected_item()
        if item is not None:
            self.emit('activated', item.key)

    def _activate(self, _view, position):
        # Enter or double-click: open again (e.g. after following links away from it)
        item = self.model.get_item(position)
        if item:
            self.emit('activated', item.key)

    def set_issues(self, issues, me_id, mentioned=(), selected_key=None):
        items = []
        mentioned = set(mentioned)
        for data in issues:
            it = IssueItem(data, me_id)
            it.mentioned = it.key in mentioned
            items.append(it)
        self._quiet = True
        try:
            # splice replaces everything in one model change: one redraw, no flicker
            self.model.splice(0, self.model.get_n_items(), items)
        finally:
            self._quiet = False
        self.select_key(selected_key)

    def select_key(self, key):
        """Highlight the open ticket without opening it again."""
        self._quiet = True
        try:
            for i in range(self.model.get_n_items()):
                if self.model.get_item(i).key == key:
                    self.selection.set_selected(i)
                    return
            self.selection.set_selected(Gtk.INVALID_LIST_POSITION)
        finally:
            self._quiet = False

    def count(self):
        return self.model.get_n_items()
