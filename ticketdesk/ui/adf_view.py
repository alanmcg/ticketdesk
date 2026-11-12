# SPDX-License-Identifier: GPL-2.0-or-later
"""Render Jira rich text (ADF display blocks) with native GTK widgets.

Tables are real Gtk.Grid layouts, so merged cells (colspan/rowspan) are exact.
Content the app cannot draw (add-on macros, unknown nodes) gets a clear placeholder
instead of disappearing.
"""

from gi.repository import Gtk

from ..core import adf
from .util import label

PANEL_ICONS = {
    'info': 'dialog-information-symbolic', 'note': 'document-edit-symbolic',
    'success': 'object-select-symbolic', 'warning': 'dialog-warning-symbolic',
    'error': 'dialog-error-symbolic', 'tip': 'starred-symbolic', 'custom': 'dialog-information-symbolic',
}
HEADING_CSS = {1: 'title-2', 2: 'title-3', 3: 'title-4'}


class AdfView(Gtk.Box):
    def __init__(self, blocks, on_link=None, media_widget=None, compact=False):
        """on_link(uri) handles link clicks; media_widget(block) returns a widget for an image."""
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6 if compact else 10)
        self.add_css_class('adf')
        self._on_link = on_link
        self._media_widget = media_widget
        for b in blocks:
            w = self._block(b)
            if w is not None:
                self.append(w)

    def _child(self, blocks):
        return AdfView(blocks, self._on_link, self._media_widget, compact=True)

    def _text(self, inlines, css=()):
        markup = adf.inlines_to_markup(inlines)
        if not markup.strip():
            return None
        lbl = label(css=css, wrap=True, selectable=True)
        try:
            lbl.set_markup(markup)
        except Exception:  # noqa: BLE001 - never lose text over a markup problem
            lbl.set_text(''.join(i.get('text', '') for i in inlines))
        lbl.connect('activate-link', self._activate_link)
        return lbl

    def _activate_link(self, _label, uri):
        if self._on_link:
            self._on_link(uri)
            return True
        return False

    def _block(self, b):
        t = b['type']
        if t == 'paragraph':
            return self._text(b['inlines'])
        if t == 'heading':
            return self._text(b['inlines'], (HEADING_CSS.get(b['level'], 'heading'),))
        if t == 'list':
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            for n, item in enumerate(b['items'], start=b.get('start', 1)):
                row = Gtk.Box(spacing=8)
                row.append(label(f'{n}.' if b['ordered'] else '•', css=('dim-label', 'numeric'), xalign=1.0))
                body = self._child(item)
                body.set_hexpand(True)
                row.append(body)
                box.append(row)
            return box
        if t == 'tasks':
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            for it in b['items']:
                row = Gtk.Box(spacing=8)
                if b.get('decision'):
                    row.append(label('◆', css=('dim-label',)))
                else:
                    # Read-only here: ticking a task edits the description, which comes with the editor.
                    check = Gtk.CheckButton(active=it['done'], sensitive=False, valign=Gtk.Align.START)
                    row.append(check)
                text = self._text(it['inlines'], ('dim-label',) if it['done'] else ())
                if text:
                    text.set_hexpand(True)
                    row.append(text)
                box.append(row)
            return box
        if t == 'code':
            return self._code(b)
        if t == 'quote':
            box = self._child(b['blocks'])
            box.add_css_class('adf-quote')
            return box
        if t == 'panel':
            box = Gtk.Box(spacing=10, css_classes=['adf-panel', b['kind']])
            icon = Gtk.Image.new_from_icon_name(PANEL_ICONS.get(b['kind'], PANEL_ICONS['info']))
            icon.set_valign(Gtk.Align.START)
            icon.add_css_class('panel-icon')
            box.append(icon)
            body = self._child(b['blocks'])
            body.set_hexpand(True)
            box.append(body)
            return box
        if t == 'rule':
            return Gtk.Separator()
        if t == 'table':
            return self._table(b)
        if t == 'media':
            w = self._media_widget(b) if self._media_widget else None
            return w or self.placeholder('image-x-generic-symbolic', f'{b.get("alt") or "Embedded file"}')
        if t == 'expand':
            exp = Gtk.Expander(label=b['title'] or 'Details')
            exp.add_css_class('adf-expand')
            exp.set_child(self._child(b['blocks']))
            return exp
        if t == 'extension':
            return self.placeholder('application-x-addon-symbolic',
                                     f'{b["title"]} — content from a Jira add-on; open the ticket in the browser to see it')
        if t == 'unsupported':
            return self.placeholder('dialog-question-symbolic', f'Content this app cannot display yet ({b["name"]})')
        return None

    @staticmethod
    def placeholder(icon_name, text):
        box = Gtk.Box(spacing=10, css_classes=['adf-placeholder'])
        box.append(Gtk.Image.new_from_icon_name(icon_name))
        box.append(label(text, css=('dim-label',), wrap=True))
        return box

    def _code(self, b):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=['adf-code'])
        head = Gtk.Box(css_classes=['adf-code-head'])
        lang = label(b['lang'] or 'Code', css=('caption', 'dim-label'))
        lang.set_hexpand(True)
        head.append(lang)
        copy = Gtk.Button(icon_name='edit-copy-symbolic', tooltip_text='Copy', css_classes=['flat', 'circular'])
        copy.connect('clicked', lambda btn: btn.get_clipboard().set_text(b['text']))
        head.append(copy)
        box.append(head)
        scroller = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER,
                                      hscrollbar_policy=Gtk.PolicyType.AUTOMATIC, propagate_natural_height=True)
        code = label(b['text'], css=('monospace', 'adf-code-text'), selectable=True)
        scroller.set_child(code)
        box.append(scroller)
        return box

    def _table(self, b):
        grid = Gtk.Grid(css_classes=['adf-table'])
        occupied = set()
        for r, row in enumerate(b['rows']):
            col = 0
            for cell in row:
                while (r, col) in occupied:
                    col += 1
                cs, rs = cell['colspan'], cell['rowspan']
                for dr in range(rs):
                    for dc in range(cs):
                        occupied.add((r + dr, col + dc))
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=['adf-cell'])
                if cell['header']:
                    box.add_css_class('header')
                content = self._child(cell['blocks'])
                for lbl in _labels(content):
                    lbl.set_max_width_chars(40)
                box.append(content)
                grid.attach(box, col, r, cs, rs)
                col += cs
        scroller = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER,
                                      hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                                      propagate_natural_height=True, propagate_natural_width=True)
        grid.set_halign(Gtk.Align.START)
        scroller.set_child(grid)
        scroller.add_css_class('adf-table-scroller')
        return scroller


def _labels(widget):
    child = widget.get_first_child()
    while child:
        if isinstance(child, Gtk.Label):
            yield child
        else:
            yield from _labels(child)
        child = child.get_next_sibling()

