# SPDX-License-Identifier: GPL-2.0-or-later
"""Files waiting to be attached (new ticket, comment): a list with sizes and remove
buttons, and a button to add files. Files come from a file chooser (paths) or are
dropped into the editor (data)."""

import os

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from ..core.attachments import guess_mime
from .util import human_size, is_image


class AttachmentList(Gtk.Box):
    __gsignals__ = {'changed': (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, show_button=True):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._items = []
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=['boxed-list'])
        self.list.set_visible(False)
        self.append(self.list)
        self.add_button = Gtk.Button(halign=Gtk.Align.START, css_classes=['flat'],
                                     child=Adw.ButtonContent(icon_name='mail-attachment-symbolic', label='Attach Files…'))
        self.add_button.connect('clicked', lambda *_: self.choose())
        self.add_button.set_visible(show_button)
        self.append(self.add_button)

    def items(self):
        """[{'path'} | {'name', 'data', 'mime'}] in the order added."""
        return [dict(i) for i in self._items]

    def clear(self):
        self._items = []
        self._refresh()

    def set_items(self, items):
        self._items = [dict(i) for i in items or []]
        self._refresh()

    def add_paths(self, paths):
        known = {i.get('path') for i in self._items}
        for p in paths:
            if p and p not in known:
                self._items.append({'path': p})
        self._refresh()

    def add_items(self, items):
        """Files from the editor: {'path'} or {'name', 'data', 'mime'}."""
        for item in items:
            if item.get('path'):
                self.add_paths([item['path']])
            else:
                self.add_data(item['name'], item['data'], item.get('mime'))

    def add_data(self, name, data, mime):
        self._items.append({'name': name, 'data': data, 'mime': mime})
        self._refresh()

    def choose(self):
        dialog = Gtk.FileDialog(title='Attach Files', modal=True)

        def done(d, result):
            try:
                files = d.open_multiple_finish(result)
            except GLib.Error:
                return  # cancelled
            paths = [files.get_item(i).get_path() for i in range(files.get_n_items())]
            self.add_paths([p for p in paths if p])
        dialog.open_multiple(self.get_root(), None, done)

    def _refresh(self):
        while (row := self.list.get_row_at_index(0)) is not None:
            self.list.remove(row)
        for i, item in enumerate(self._items):
            if item.get('path'):
                name = os.path.basename(item['path'])
                try:
                    size = os.path.getsize(item['path'])
                except OSError:
                    size = None
            else:
                name, size = item['name'], len(item['data'])
            row = Adw.ActionRow(title=name, use_markup=False,
                                subtitle=human_size(size) if size is not None else 'File not found')
            row.add_prefix(Gtk.Image.new_from_icon_name('image-x-generic-symbolic' if is_image({'mimeType': guess_mime(name)})
                                                        else 'text-x-generic-symbolic'))
            remove = Gtk.Button(icon_name='user-trash-symbolic', valign=Gtk.Align.CENTER, css_classes=['flat'],
                                tooltip_text='Remove')
            remove.connect('clicked', lambda _b, n=i: self._remove(n))
            row.add_suffix(remove)
            self.list.append(row)
        self.list.set_visible(bool(self._items))
        self.emit('changed')

    def _remove(self, index):
        if 0 <= index < len(self._items):
            del self._items[index]
            GLib.idle_add(lambda: (self._refresh(), GLib.SOURCE_REMOVE)[1])  # not while its button is handling a click


def image_filter():
    f = Gtk.FileFilter(name='Images')
    for mime in ('image/png', 'image/jpeg', 'image/gif', 'image/webp'):
        f.add_mime_type(mime)
    store = Gio.ListStore.new(Gtk.FileFilter)
    store.append(f)
    return store
