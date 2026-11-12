# SPDX-License-Identifier: GPL-2.0-or-later
"""Document editor for descriptions: a native GTK toolbar over an editable page in
WebKitGTK (editor/editor.html). The page holds the document; the toolbar shows the
formatting at the cursor, and a table toolbar appears while the cursor is in a table.

Without WebKitGTK 6 a plain text editor is used instead (no formatting).
"""

import base64
import json
import logging
import os
from pathlib import Path

import gi
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from ..core import adf
from ..core.adf_html import adf_to_html, html_to_adf, is_empty
from ..core.attachments import guess_mime, image_size
from .util import label, open_uri

try:
    gi.require_version('WebKit', '6.0')
    from gi.repository import WebKit
except (ValueError, ImportError):
    WebKit = None

log = logging.getLogger(__name__)
EDITOR_HTML = Path(__file__).parent / 'editor' / 'editor.html'

BLOCK_STYLES = (('p', 'Normal Text'), ('h1', 'Heading 1'), ('h2', 'Heading 2'), ('h3', 'Heading 3'),
                ('blockquote', 'Quote'), ('pre', 'Code Block'))


def available():
    return WebKit is not None


def create(**kwargs):
    """The rich editor when WebKitGTK is installed, otherwise the plain text editor."""
    return RichEditor(**kwargs) if available() else PlainEditor(**kwargs)


def _toolbar_box(*css):
    """A toolbar row that wraps onto more lines when the editor is narrow, so it never
    makes its window wider than the screen (Adw.WrapBox, or Gtk.FlowBox before libadwaita 1.7)."""
    classes = ['toolbar', *css]
    if hasattr(Adw, 'WrapBox'):
        return Adw.WrapBox(child_spacing=2, line_spacing=2, css_classes=classes)
    return Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False, column_spacing=2, row_spacing=2,
                       max_children_per_line=40, css_classes=classes)


def _rgba(c, alpha=None):
    c = c.copy()
    if alpha is not None:
        c.alpha = alpha
    return c.to_string()


class _EditorBase(Gtk.Box):
    __gsignals__ = {
        'changed': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'submit': (GObject.SignalFlags.RUN_FIRST, None, ()),       # Ctrl+Enter
        'notice': (GObject.SignalFlags.RUN_FIRST, None, (str,)),   # something to tell the user
        # non-image files dropped into the text: [{'name', 'data' (bytes), 'mime'}], to be attached
        'files': (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }


class PlainEditor(_EditorBase):
    """Fallback without WebKitGTK: plain text, converted to paragraphs."""

    def __init__(self, placeholder='', auto_height=False, min_height=160, **_):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        note = label('Formatting needs WebKitGTK 6 (package webkitgtk-6.0 / gir1.2-webkit-6.0); '
                     'text is saved as plain paragraphs.', css=('dim-label', 'caption'), wrap=True)
        self.append(note)
        self.text = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=10, bottom_margin=10,
                                 left_margin=12, right_margin=12, accepts_tab=False)
        self.text.get_buffer().connect('changed', lambda *_: self.emit('changed'))
        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', self._key)
        self.text.add_controller(keys)
        scroller = Gtk.ScrolledWindow(child=self.text, vexpand=not auto_height, min_content_height=min_height,
                                      propagate_natural_height=auto_height, css_classes=['card'])
        self.append(scroller)

    def _key(self, _ctl, keyval, _code, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and state & Gdk.ModifierType.CONTROL_MASK:
            self.emit('submit')
            return True
        return False

    def set_adf(self, doc):
        self.text.get_buffer().set_text(adf.to_text(doc) if doc else '')

    def draft(self):
        buf = self.text.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

    def restore_draft(self, text):
        self.text.get_buffer().set_text(text or '')

    def fetch_content(self, callback):
        self.fetch_adf(lambda doc: callback(doc, {}))

    def fetch_adf(self, callback):
        buf = self.text.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
        callback(adf.text_to_adf(text) if text else None)

    def grab_focus(self):
        return self.text.grab_focus()


class RichEditor(_EditorBase):
    def __init__(self, placeholder='', auto_height=False, min_height=160, resolve_image=None, find_people=None, **_):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0, css_classes=['rich-editor', 'card'])
        # resolve_image(media_id, alt, callback(path or None)): finds the file of an image
        # already in the text (from the ticket's attachments), to show it while editing
        self.resolve_image = resolve_image
        # find_people(query, callback([{'id', 'name'}])): people for @mentions
        self.find_people = find_people
        self.auto_height = auto_height
        self.min_height = min_height
        self.placeholder = placeholder
        self._ready = False
        self._queue = []
        self._syncing = False
        self._state = {}
        self._html = '<p><br></p>'

        self.append(self._build_toolbar())
        self.table_bar = Gtk.Revealer(child=self._build_table_bar(),
                                      transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.append(self.table_bar)
        self.image_bar = Gtk.Revealer(child=self._build_image_bar(),
                                      transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.append(self.image_bar)
        self.append(Gtk.Separator())

        manager = WebKit.UserContentManager()
        # world None = the page's own world (needs WebKitGTK >= 2.40.1)
        manager.register_script_message_handler('td', None)
        manager.connect('script-message-received::td', self._message)
        self.view = WebKit.WebView(user_content_manager=manager, vexpand=not auto_height, hexpand=True)
        self.view.set_background_color(Gdk.RGBA(red=0, green=0, blue=0, alpha=0))
        settings = self.view.get_settings()
        settings.set_enable_developer_extras(bool(GLib.getenv('TICKETDESK_DEVTOOLS')))
        settings.set_javascript_can_access_clipboard(True)
        settings.set_enable_back_forward_navigation_gestures(False)
        self.view.connect('decide-policy', self._decide_policy)
        self.view.set_size_request(-1, min_height)
        self.append(self.view)

        # WebKitGTK does not hand clipboard images or files dropped from the file manager to
        # the page (it inserts file:// links instead), so both are handled here, before it.
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect('key-pressed', self._key_pressed)
        self.view.add_controller(keys)
        drop = Gtk.DropTarget.new(Gdk.FileList.__gtype__, Gdk.DragAction.COPY)
        drop.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        drop.connect('drop', self._files_dropped)
        self.add_controller(drop)
        self.view.load_html(EDITOR_HTML.read_text(encoding='utf-8'), None)

        style = Adw.StyleManager.get_default()
        self._theme_ids = [style.connect('notify::dark', lambda *_: self._apply_theme()),
                           style.connect('notify::accent-color-rgba', lambda *_: self._apply_theme())]
        self.connect('destroy', self._destroyed)

    # ----- toolbar -----

    def _toggle(self, icon, tip, cmd, arg=None, text=None):
        b = Gtk.ToggleButton(tooltip_text=tip, css_classes=['flat'])
        if text:
            b.set_child(label(text, css=('monospace',)))
        else:
            b.set_icon_name(icon)
        b.connect('toggled', self._toggled, cmd, arg)
        return b

    def _button(self, icon, tip, cmd, arg=None):
        b = Gtk.Button(icon_name=icon, tooltip_text=tip, css_classes=['flat'])
        b.connect('clicked', lambda *_: self.cmd(cmd, arg))
        return b

    def _build_toolbar(self):
        bar = _toolbar_box('editor-toolbar')
        self.style_drop = Gtk.DropDown.new_from_strings([t for _, t in BLOCK_STYLES])
        self.style_drop.set_tooltip_text('Paragraph style')
        self.style_drop.add_css_class('flat')
        self.style_drop.connect('notify::selected', self._style_selected)
        bar.append(self.style_drop)
        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.marks = {
            'bold': self._toggle('format-text-bold-symbolic', 'Bold (Ctrl+B)', 'bold'),
            'italic': self._toggle('format-text-italic-symbolic', 'Italic (Ctrl+I)', 'italic'),
            'underline': self._toggle('format-text-underline-symbolic', 'Underline (Ctrl+U)', 'underline'),
            'strike': self._toggle('format-text-strikethrough-symbolic', 'Strikethrough', 'strikeThrough'),
            'code': self._toggle(None, 'Code (Ctrl+E)', 'code', text='</>'),
        }
        for b in self.marks.values():
            bar.append(b)
        self.link_button = Gtk.ToggleButton(icon_name='insert-link-symbolic', tooltip_text='Link (Ctrl+K)',
                                            css_classes=['flat'])
        self.link_button.connect('toggled', self._link_toggled)
        bar.append(self.link_button)
        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.lists = {
            'ul': self._toggle('view-list-bullet-symbolic', 'Bullet List (Ctrl+Shift+8)', 'block', 'ul'),
            'ol': self._toggle('view-list-ordered-symbolic', 'Numbered List (Ctrl+Shift+7)', 'block', 'ol'),
            'tasks': self._toggle('checkbox-checked-symbolic', 'Task List', 'block', 'tasks'),
        }
        for b in self.lists.values():
            bar.append(b)
        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        group = Gio.SimpleActionGroup()
        for name, cb in (('table', lambda *_: self.cmd('table', {'rows': 3, 'cols': 3, 'header': True})),
                         ('image', lambda *_: self.choose_image()),
                         ('rule', lambda *_: self.cmd('rule'))):
            a = Gio.SimpleAction.new(name, None)
            a.connect('activate', cb)
            group.add_action(a)
        panel = Gio.SimpleAction.new('panel', GLib.VariantType('s'))
        panel.connect('activate', lambda _a, v: self.cmd('panel', v.get_string() or None))
        group.add_action(panel)
        table_op = Gio.SimpleAction.new('table-op', GLib.VariantType('s'))
        table_op.connect('activate', lambda _a, v: self.cmd('tableOp', v.get_string()))
        group.add_action(table_op)
        self.insert_action_group('editor', group)

        insert = Gio.Menu()
        insert.append('Table', 'editor.table')
        insert.append('Image…', 'editor.image')
        insert.append('Divider', 'editor.rule')
        panels = Gio.Menu()
        for kind, title in (('info', 'Info'), ('note', 'Note'), ('success', 'Success'),
                            ('warning', 'Warning'), ('error', 'Error')):
            panels.append(f'{title} Panel', f'editor.panel::{kind}')
        insert.append_section(None, panels)
        image = Gtk.Button(icon_name='insert-image-symbolic', tooltip_text='Insert Image (or paste / drop one)',
                           css_classes=['flat'])
        image.connect('clicked', lambda *_: self.choose_image())
        bar.append(image)
        bar.append(Gtk.MenuButton(icon_name='list-add-symbolic', menu_model=insert, tooltip_text='Insert',
                                  css_classes=['flat']))
        bar.append(self._button('edit-undo-symbolic', 'Undo (Ctrl+Z)', 'undo'))
        bar.append(self._button('edit-redo-symbolic', 'Redo (Ctrl+Shift+Z)', 'redo'))
        return bar

    def _build_table_bar(self):
        bar = _toolbar_box('editor-toolbar', 'table-toolbar')
        bar.append(label('Table', css=('dim-label', 'caption-heading')))
        ins = Gio.Menu()
        for op, title in (('rowAbove', 'Row Above'), ('rowBelow', 'Row Below'),
                          ('colLeft', 'Column Left'), ('colRight', 'Column Right')):
            ins.append(title, f'editor.table-op::{op}')
        bar.append(Gtk.MenuButton(label='Insert', menu_model=ins, css_classes=['flat'], always_show_arrow=True))
        self.merge_right = Gtk.Button(label='Merge Right', css_classes=['flat'], tooltip_text='Merge with the cell to the right')
        self.merge_right.connect('clicked', lambda *_: self.cmd('tableOp', 'mergeRight'))
        self.merge_down = Gtk.Button(label='Merge Down', css_classes=['flat'], tooltip_text='Merge with the cell below')
        self.merge_down.connect('clicked', lambda *_: self.cmd('tableOp', 'mergeDown'))
        self.split = Gtk.Button(label='Split', css_classes=['flat'], tooltip_text='Split a merged cell')
        self.split.connect('clicked', lambda *_: self.cmd('tableOp', 'split'))
        self.header_row = Gtk.ToggleButton(label='Header Row', css_classes=['flat'])
        self.header_row.connect('toggled', self._header_toggled)
        for b in (self.merge_right, self.merge_down, self.split, self.header_row):
            bar.append(b)
        dele = Gio.Menu()
        for op, title in (('deleteRow', 'Delete Row'), ('deleteCol', 'Delete Column'), ('deleteTable', 'Delete Table')):
            dele.append(title, f'editor.table-op::{op}')
        bar.append(Gtk.MenuButton(label='Delete', menu_model=dele, css_classes=['flat'], always_show_arrow=True))
        return bar

    def _build_image_bar(self):
        bar = _toolbar_box('editor-toolbar', 'table-toolbar')
        bar.append(label('Image', css=('dim-label', 'caption-heading')))
        self.size_buttons = {}
        sizes = Gtk.Box(css_classes=['linked'])
        for pct, title in ((25, 'Small'), (50, 'Medium'), (75, 'Large'), (100, 'Full Width'), (None, 'Original')):
            b = Gtk.ToggleButton(label=title, css_classes=['flat'],
                                 tooltip_text=f'{pct} % of the text width' if pct else 'Natural size (at most the text width)')
            b.connect('toggled', self._size_toggled, pct)
            self.size_buttons[pct] = b
            sizes.append(b)
        bar.append(sizes)
        bar.append(label('or drag the corner', css=('dim-label', 'caption')))
        remove = Gtk.Button(icon_name='user-trash-symbolic', tooltip_text='Remove Image (Delete)', css_classes=['flat'])
        remove.connect('clicked', lambda *_: self.cmd('removeImage'))
        bar.append(remove)
        return bar

    def _size_toggled(self, button, pct):
        if self._syncing:
            return
        if not button.get_active():   # the current size: keep it
            self._sync_state(self._state)
            return
        self.cmd('imageWidth', pct)

    def choose_image(self):
        from .attachment_list import image_filter
        dialog = Gtk.FileDialog(title='Insert Image', modal=True, filters=image_filter())

        def done(d, result):
            try:
                f = d.open_finish(result)
            except GLib.Error:
                return
            if f and f.get_path():
                self.insert_image_file(f.get_path())
        dialog.open(self.get_root(), None, done)

    def insert_image_file(self, path):
        try:
            with open(path, 'rb') as fh:
                data = fh.read()
        except OSError as e:
            self.emit('notice', f'Could not read {os.path.basename(path)}: {e.strerror}')
            return
        if len(data) > 50 * 1024 * 1024:
            self.emit('notice', f'{os.path.basename(path)} is larger than 50 MB; attach it as a file instead')
            return
        size = image_size(data) or (None, None)
        url = f'data:{guess_mime(path)};base64,' + base64.b64encode(data).decode()
        self.js(f'td.insertImage({json.dumps(url)}, {json.dumps(os.path.basename(path))}, '
                f'{json.dumps(size[0])}, {json.dumps(size[1])})')
        self.view.grab_focus()

    # ----- paste and drop -----

    def _key_pressed(self, _ctl, keyval, _code, state):
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        paste = (ctrl and not shift and keyval in (Gdk.KEY_v, Gdk.KEY_V)) or \
                (shift and not ctrl and keyval == Gdk.KEY_Insert)
        if not paste:
            return False
        clipboard = self.view.get_clipboard()
        formats = clipboard.get_formats()
        has_text = any(formats.contain_mime_type(m) for m in ('text/plain', 'text/plain;charset=utf-8', 'text/html'))
        if formats.contain_gtype(Gdk.FileList.__gtype__) and not has_text:
            clipboard.read_value_async(Gdk.FileList.__gtype__, GLib.PRIORITY_DEFAULT, None, self._pasted_files)
            return True
        # Office programs put a picture of the selection next to its text: text wins then
        if formats.contain_gtype(Gdk.Texture.__gtype__) and not has_text:
            clipboard.read_texture_async(None, self._pasted_texture)
            return True
        return False  # text and HTML: the page's own paste (cleaned up there)

    def _pasted_texture(self, clipboard, result):
        try:
            texture = clipboard.read_texture_finish(result)
        except GLib.Error as e:
            self.emit('notice', f'Could not paste the image: {e.message}')
            return
        if texture is None:
            return
        data = texture.save_to_png_bytes().get_data()
        url = 'data:image/png;base64,' + base64.b64encode(data).decode()
        self.js(f'td.insertImage({json.dumps(url)}, "image.png", {texture.get_width()}, {texture.get_height()})')

    def _pasted_files(self, clipboard, result):
        try:
            files = clipboard.read_value_finish(result)
        except GLib.Error:
            return
        self._take_files([f.get_path() for f in files.get_files() if f.get_path()])

    def _files_dropped(self, _target, value, x, y):
        files = value.get_files() if hasattr(value, 'get_files') else []
        ok, rect = self.view.compute_bounds(self)
        if ok:  # put the caret where the files were dropped
            self.js(f'td.caretAtPoint({x - rect.get_x():.0f}, {y - rect.get_y():.0f})')
        self._take_files([f.get_path() for f in files if f.get_path()])
        return True

    def _take_files(self, paths):
        """Images go into the text; other files are attached (the 'files' signal)."""
        others = []
        for path in paths:
            if guess_mime(path) in ('image/png', 'image/jpeg', 'image/gif', 'image/webp'):
                self.insert_image_file(path)
            else:
                others.append({'path': path})
        if others:
            self.emit('files', others)

    def _load_images(self, items):
        """Show images already in the text: their files come from the ticket's attachments."""
        if not self.resolve_image:
            return
        for item in items:
            mid, alt = item.get('id'), item.get('alt') or ''

            def got(path, mid=mid):
                if not path:
                    return
                try:
                    with open(path, 'rb') as fh:
                        data = fh.read()
                except OSError:
                    return
                url = f'data:{guess_mime(str(path))};base64,' + base64.b64encode(data).decode()
                self.js(f'td.setImageSrc({json.dumps(mid)}, {json.dumps(url)})')
            self.resolve_image(mid, alt, got)

    # ----- toolbar handlers -----

    def _toggled(self, button, cmd, arg):
        if not self._syncing:
            self.cmd(cmd, arg)

    def _style_selected(self, drop, _pspec):
        if self._syncing:
            return
        i = drop.get_selected()
        if 0 <= i < len(BLOCK_STYLES):
            tag = BLOCK_STYLES[i][0]
            current = self._state.get('block')
            if tag == 'p' and current in BLOCK_STYLES_TAGS:
                self.cmd('block', current)  # toggling the current style off gives a paragraph
            elif tag != current:
                self.cmd('block', tag)

    def _header_toggled(self, _b):
        if not self._syncing:
            self.cmd('tableOp', 'headerRow')

    def _link_toggled(self, _b):
        if self._syncing:
            return
        self._sync_state(self._state)  # the button reflects the cursor, not the click
        self._ask_link(self._state.get('href') or '')

    def _ask_link(self, current):
        dlg = Adw.AlertDialog(heading='Link', body='Address of the link (Ctrl+click opens links in the text)')
        entry = Gtk.Entry(text=current, placeholder_text='https://', activates_default=True)
        dlg.set_extra_child(entry)
        dlg.add_response('cancel', 'Cancel')
        if self._state.get('link'):
            dlg.add_response('remove', 'Remove Link')
            dlg.set_response_appearance('remove', Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.add_response('apply', 'Apply')
        dlg.set_response_appearance('apply', Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response('apply')
        dlg.set_close_response('cancel')

        def response(_d, rid):
            url = entry.get_text().strip()
            if rid == 'remove':
                self.cmd('link', None)
            elif rid == 'apply' and url:
                if '://' not in url and not url.startswith('mailto:'):
                    url = 'https://' + url
                self.cmd('link', url)
            self.grab_focus()
        dlg.connect('response', response)
        dlg.present(self.get_root())

    def _sync_state(self, st):
        self._syncing = True
        try:
            for k, b in self.marks.items():
                b.set_active(bool(st.get(k)))
            self.link_button.set_active(bool(st.get('link')))
            block = st.get('block', 'p')
            for k, b in self.lists.items():
                b.set_active(block == k)
            tags = [t for t, _ in BLOCK_STYLES]
            self.style_drop.set_selected(tags.index(block) if block in tags else 0)
            self.style_drop.set_sensitive(block not in ('ul', 'ol', 'tasks'))
            image = st.get('image')
            self.image_bar.set_reveal_child(image is not None)
            if image is not None:
                width = image.get('width')
                for pct, b in self.size_buttons.items():
                    b.set_active(pct == width)
            in_table = bool(st.get('inTable')) and image is None
            self.table_bar.set_reveal_child(in_table)
            self.merge_right.set_sensitive(bool(st.get('canMergeRight')))
            self.merge_down.set_sensitive(bool(st.get('canMergeDown')))
            self.split.set_sensitive(bool(st.get('canSplit')))
            self.header_row.set_active(bool(st.get('headerRow')))
        finally:
            self._syncing = False

    # ----- page communication -----

    def js(self, code):
        if not self._ready:
            self._queue.append(code)
            return
        self.view.evaluate_javascript(code, -1, None, None, None, None)

    def cmd(self, name, arg=None):
        self.js(f'td.cmd({json.dumps(name)}, {json.dumps(arg)})')
        self.view.grab_focus()

    def _message(self, _manager, value):
        try:
            msg = json.loads(value.to_string())
        except (ValueError, TypeError):
            return
        kind = msg.get('type')
        if kind == 'ready':
            self._ready = True
            self._apply_theme()
            if self.auto_height:
                self.js('td.setAutoHeight(true)')
            self.js(f'td.setContent({json.dumps(self._html)}, {json.dumps(self.placeholder)})')
            for code in self._queue:
                self.view.evaluate_javascript(code, -1, None, None, None, None)
            self._queue.clear()
        elif kind == 'changed':
            self._html = msg.get('html') or ''
            self.emit('changed')
        elif kind == 'state':
            self._state = msg
            self._sync_state(msg)
        elif kind == 'height' and self.auto_height:
            self.view.set_size_request(-1, max(self.min_height, int(msg.get('height') or 0) + 2))
        elif kind == 'link-request':
            self._ask_link(msg.get('href') or '')
        elif kind == 'open-link' and msg.get('href'):
            open_uri(self, msg['href'])
        elif kind == 'notice':
            self.emit('notice', msg.get('text') or '')
        elif kind == 'mention-query':
            seq = int(msg.get('seq') or 0)

            def found(people, seq=seq):
                self.js(f'td.mentionResults({seq}, {json.dumps(people)})')
            if self.find_people and msg.get('query'):
                self.find_people(msg['query'], found)
            else:
                found([])
        elif kind == 'need-images':
            self._load_images(msg.get('items') or [])
        elif kind == 'attach':
            files = []
            for f in msg.get('files') or []:
                try:
                    files.append({'name': f['name'], 'mime': f.get('mime'), 'data': base64.b64decode(f.get('data') or '')})
                except (ValueError, KeyError):
                    continue
            if files:
                self.emit('files', files)
        elif kind == 'submit':
            self.emit('submit')

    def _decide_policy(self, _view, decision, decision_type):
        # The page never navigates: links are opened in the browser (Ctrl+click).
        if decision_type in (WebKit.PolicyDecisionType.NAVIGATION_ACTION, WebKit.PolicyDecisionType.NEW_WINDOW_ACTION):
            action = decision.get_navigation_action()
            uri = action.get_request().get_uri() if action else ''
            if uri and uri not in ('about:blank',) and not uri.startswith('data:'):
                decision.ignore()
                return True
        return False

    def _apply_theme(self):
        if not self._ready:
            return
        style = Adw.StyleManager.get_default()
        fg = self.get_color()
        font = Pango.FontDescription.from_string(Gtk.Settings.get_default().get_property('gtk-font-name') or 'Sans 11')
        size = font.get_size() / Pango.SCALE if font.get_size() else 11
        accent = style.get_accent_color_rgba() if hasattr(style, 'get_accent_color_rgba') else None
        theme = {
            'fg': _rgba(fg), 'dim': _rgba(fg, 0.55), 'border': _rgba(fg, 0.18), 'code-bg': _rgba(fg, 0.07),
            'header-bg': _rgba(fg, 0.06), 'bg': 'transparent',
            'popover-bg': '#36363a' if style.get_dark() else '#ffffff',   # libadwaita's popover colours
            'accent': accent.to_string() if accent else None,
            'select': _rgba(accent, 0.3) if accent else None,
            'font': f'"{font.get_family()}", system-ui, sans-serif' if font.get_family() else None,
            'size': f'{size:g}pt',
        }
        self.js(f'td.setTheme({json.dumps(theme)})')

    def _destroyed(self, *_):
        style = Adw.StyleManager.get_default()
        for hid in self._theme_ids:
            style.disconnect(hid)
        self._theme_ids = []

    # ----- public API -----

    def draft(self):
        """The document as last reported by the page (for keeping unsaved work)."""
        return self._html

    def restore_draft(self, html):
        self._html = html or '<p><br></p>'
        if self._ready:
            self.js(f'td.setContent({json.dumps(self._html)}, {json.dumps(self.placeholder)})')

    def set_adf(self, doc):
        self._html = adf_to_html(doc)
        if self._ready:
            self.js(f'td.setContent({json.dumps(self._html)}, {json.dumps(self.placeholder)})')

    def fetch_content(self, callback):
        """Like fetch_adf, with the images added in the editor: callback(doc, pending) where
        pending is {token: {'name', 'mime', 'data', 'width', 'height'}} (see core.adf_html)."""
        def done(view, result):
            try:
                html = view.evaluate_javascript_finish(result).to_string()
            except GLib.Error as e:
                log.warning('editor: %s', e.message)
                html = self._html
            pending = {}
            doc = html_to_adf(html, pending)
            callback(None if is_empty(doc) else doc, pending)
        if not self._ready:
            pending = {}
            doc = html_to_adf(self._html, pending)
            callback(None if is_empty(doc) else doc, pending)
            return
        self.view.evaluate_javascript('td.getHtml()', -1, None, None, None, done)

    def fetch_adf(self, callback):
        """Read the current document from the page (not from the last change report, which
        may be a moment behind) and pass its ADF, or None if it is empty, to callback."""
        if not self._ready:
            callback(None if self._html in ('', '<p><br></p>') else html_to_adf(self._html))
            return

        def done(view, result):
            try:
                value = view.evaluate_javascript_finish(result)
                html = value.to_string()
            except GLib.Error as e:
                log.warning('editor: %s', e.message)
                html = self._html
            doc = html_to_adf(html)
            from ..core.adf_html import is_empty
            callback(None if is_empty(doc) else doc)
        self.view.evaluate_javascript('td.getHtml()', -1, None, None, None, done)

    def grab_focus(self):
        self.view.grab_focus()
        self.js('td.focus(true)')
        return True


BLOCK_STYLES_TAGS = {t for t, _ in BLOCK_STYLES if t != 'p'}
