# SPDX-License-Identifier: GPL-2.0-or-later
"""Link a ticket to another: pick how ("Blocks", "Is child of", …) and which ticket."""

from gi.repository import Adw, Gtk, Pango

from ..core import linking, relations
from .util import label


class LinkDialog(Adw.Dialog):
    """`on_linked(issue)` gets the ticket as Jira has it after linking."""

    def __init__(self, store, key, on_linked, toast):
        super().__init__(title=f'Link {key}', content_width=520, content_height=560)
        self.store = store
        self.key = key
        self.on_linked = on_linked
        self.toast = toast
        self.kinds = list(linking.KINDS)
        self._serial = 0

        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label='Cancel')
        cancel.connect('clicked', lambda *_: self.close())
        header.pack_start(cancel)
        self.link_button = Gtk.Button(label='Link', css_classes=['suggested-action'], sensitive=False)
        self.link_button.connect('clicked', lambda *_: self._link())
        header.pack_end(self.link_button)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=18,
                       margin_start=18, margin_end=18)
        group = Adw.PreferencesGroup()
        self.kind_row = Adw.ComboRow(title=key, use_markup=False, model=Gtk.StringList.new([lbl for _k, lbl in self.kinds]))
        group.add(self.kind_row)
        body.append(group)

        self.search = Gtk.SearchEntry(placeholder_text='Ticket key or words from it', search_delay=350)
        self.search.connect('search-changed', lambda *_: self._search())
        self.search.connect('activate', lambda *_: self._activate())
        body.append(self.search)
        self.caption = label('Recently viewed', css=('dim-label', 'caption'), xalign=0.0)
        body.append(self.caption)
        self.results = Gtk.ListBox(css_classes=['boxed-list'], selection_mode=Gtk.SelectionMode.SINGLE)
        self.results.connect('row-selected', lambda *_: self._update_button())
        self.results.connect('row-activated', lambda *_: self._link())
        empty = label('No tickets found', css=('dim-label',), xalign=0.5)
        empty.set_margin_top(12)
        empty.set_margin_bottom(12)
        self.results.set_placeholder(empty)
        body.append(Gtk.ScrolledWindow(child=self.results, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                       propagate_natural_height=True))
        self.spinner = Adw.Spinner(visible=False, halign=Gtk.Align.CENTER)
        body.append(self.spinner)

        view = Adw.ToolbarView(content=body)
        view.add_top_bar(header)
        self.set_child(view)
        self.set_focus(self.search)

        self._show([relations.brief(i) for i in store.recent() if i.get('key') != key][:10])
        store.link_kinds(self._got_kinds, lambda e: None)  # keep the full list if this fails

    def _got_kinds(self, kinds):
        chosen = self.kinds[self.kind_row.get_selected()][0] if self.kinds else None
        self.kinds = kinds or self.kinds
        self.kind_row.set_model(Gtk.StringList.new([lbl for _k, lbl in self.kinds]))
        ids = [k for k, _ in self.kinds]
        if chosen in ids:
            self.kind_row.set_selected(ids.index(chosen))

    def _row(self, b):
        row = Gtk.ListBoxRow()
        row.key = b['key']
        box = Gtk.Box(spacing=8, margin_top=8, margin_bottom=8, margin_start=10, margin_end=10)
        box.append(label(b['key'], css=('monospace', 'dim-label')))
        summary = label(b['summary'], ellipsize=Pango.EllipsizeMode.END)
        summary.set_hexpand(True)
        summary.set_tooltip_text(b['summary'])
        box.append(summary)
        if b.get('status'):
            box.append(label(b['status'], css=('tag', b.get('category') or 'todo')))
        row.set_child(box)
        row.update_property([Gtk.AccessibleProperty.LABEL], [f'{b["key"]}: {b["summary"]}'])
        return row

    def _show(self, items):
        while (r := self.results.get_row_at_index(0)) is not None:
            self.results.remove(r)
        for b in items:
            self.results.append(self._row(b))
        self._update_button()

    def _search(self):
        text = self.search.get_text().strip()
        self._serial += 1
        serial = self._serial
        if not text:
            self.caption.set_text('Recently viewed')
            self._show([relations.brief(i) for i in self.store.recent() if i.get('key') != self.key][:10])
            return
        self.caption.set_text('')
        self.spinner.set_visible(True)

        def got(items):
            if serial == self._serial:
                self.spinner.set_visible(False)
                self._show(items)
                if len(items) == 1:
                    self.results.select_row(self.results.get_row_at_index(0))

        def failed(e):
            if serial == self._serial:
                self.spinner.set_visible(False)
                self._show([])
                self.caption.set_text(str(e))
        self.store.find_tickets(text, self.key, got, failed)

    def _update_button(self):
        self.link_button.set_sensitive(self.results.get_selected_row() is not None and self.store.online)

    def _activate(self):
        row = self.results.get_selected_row() or self.results.get_row_at_index(0)
        if row is not None:
            self.results.select_row(row)
            self._link()

    def _link(self):
        row = self.results.get_selected_row()
        if row is None or not self.link_button.get_sensitive():
            return
        kind, label_text = self.kinds[self.kind_row.get_selected()]
        other = row.key
        self.link_button.set_sensitive(False)
        self.spinner.set_visible(True)

        def done(issue):
            self.toast(f'{self.key} {label_text.lower()} {other}')
            self.on_linked(issue)
            self.close()

        def failed(e):
            self.spinner.set_visible(False)
            self._update_button()
            self.caption.set_text(f'Could not link: {e}')
            self.caption.set_wrap(True)
        self.store.link(self.key, kind, other, done, failed)
