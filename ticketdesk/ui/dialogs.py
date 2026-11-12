# SPDX-License-Identifier: GPL-2.0-or-later
"""Dialogs: field form (transition screens, workflow-rule fixes) and the image viewer."""

import shutil

from gi.repository import Adw, Gdk, GLib, Gtk

from ..core import fields as F
from .util import human_size, label


class FieldFormDialog(Adw.Dialog):
    """Ask for field values. `field_list` is [(field_id, meta)] from Jira's transition
    or edit metadata; `current` holds the ticket's current values for preselection."""

    def __init__(self, title, subtitle, field_list, current, submit_label, on_submit, note=None):
        super().__init__(title=title, content_width=480)
        self._on_submit = on_submit
        self._states = []

        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        header.set_title_widget(Adw.WindowTitle(title=title, subtitle=subtitle))
        cancel = Gtk.Button(label='Cancel')
        cancel.connect('clicked', lambda *_: self.close())
        header.pack_start(cancel)
        self._submit = Gtk.Button(label=submit_label, css_classes=['suggested-action'])
        self._submit.connect('clicked', self._submit_clicked)
        header.pack_end(self._submit)

        page = Adw.PreferencesPage()
        if note:
            g = Adw.PreferencesGroup(description=GLib.markup_escape_text(note))
            page.add(g)
        unsupported = False
        for fid, meta in field_list:
            name = GLib.markup_escape_text(meta.get('name') or fid)
            group = Adw.PreferencesGroup(title=f'{name} *' if meta.get('required', True) else name)
            state = {'id': fid, 'meta': meta}
            if not F.is_supported(meta):
                unsupported = True
                group.set_description('This field type cannot be set here. Open the ticket in the browser.')
            elif meta.get('allowedValues'):
                values = [v for v in meta['allowedValues'] if not v.get('disabled')]
                chosen = set(F.current_ids(current.get(fid)))
                if (meta.get('schema') or {}).get('type') == 'array':
                    state['checks'] = []
                    for v in values:
                        row = Adw.ActionRow(title=F.option_label(v), use_markup=False)
                        check = Gtk.CheckButton(active=str(v['id']) in chosen, valign=Gtk.Align.CENTER)
                        row.add_prefix(check)
                        row.set_activatable_widget(check)
                        group.add(row)
                        state['checks'].append((str(v['id']), check))
                else:
                    labels = ['Choose…'] + [F.option_label(v) for v in values]
                    combo = Adw.ComboRow(title='Value', model=Gtk.StringList.new(labels))
                    ids = [None] + [str(v['id']) for v in values]
                    pre = next((i for i, x in enumerate(ids) if x in chosen), 0)
                    combo.set_selected(pre)
                    group.add(combo)
                    state['combo'] = (combo, ids)
            else:
                hint = {'number': 'Number', 'date': 'Date (YYYY-MM-DD)',
                        'datetime': 'Date and time (YYYY-MM-DDThh:mm:ss.000+0000)'}.get((meta.get('schema') or {}).get('type'), 'Value')
                entry = Adw.EntryRow(title=hint, text=F.current_text(current.get(fid)))
                group.add(entry)
                state['entry'] = entry
            self._states.append(state)
            page.add(group)

        self._error = label('', css=('error',), wrap=True)
        self._error.set_visible(False)
        err_group = Adw.PreferencesGroup()
        err_group.add(self._error)
        page.add(err_group)
        if unsupported:
            note_group = Adw.PreferencesGroup(description='Fields that cannot be set here are left unchanged.')
            page.add(note_group)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(page)
        self.set_child(view)

    def _submit_clicked(self, *_):
        values = {}
        for s in self._states:
            meta = s['meta']
            if not F.is_supported(meta):
                continue
            if 'checks' in s:
                v = F.form_value(meta, [i for i, c in s['checks'] if c.get_active()])
            elif 'combo' in s:
                combo, ids = s['combo']
                sel = ids[combo.get_selected()] if combo.get_selected() < len(ids) else None
                v = F.form_value(meta, [sel] if sel else [])
            else:
                v = F.form_value(meta, text=s['entry'].get_text())
            if v is None:
                if meta.get('required', True):
                    self.show_error(f'{meta.get("name") or s["id"]} is required')
                    return
                continue
            values[s['id']] = v
        self._submit.set_sensitive(False)
        self._on_submit(values, self)

    def show_error(self, text):
        self._submit.set_sensitive(True)
        self._error.set_text(text)
        self._error.set_visible(True)


class ImageViewer(Adw.Dialog):
    """Full-size image viewer with previous/next, fit/100 %, copy and save."""

    def __init__(self, store, attachments, index, parent):
        super().__init__(title='Image', follows_content_size=False)
        w, h = parent.get_width(), parent.get_height()
        self.set_content_width(max(480, int(w * 0.92)))
        self.set_content_height(max(360, int(h * 0.92)))
        self._store = store
        self._atts = attachments
        self._index = index
        self._texture = None
        self._path = None
        self._serial = 0

        header = Adw.HeaderBar()
        self._title = Adw.WindowTitle()
        header.set_title_widget(self._title)
        self._prev = Gtk.Button(icon_name='go-previous-symbolic', tooltip_text='Previous Image')
        self._prev.connect('clicked', lambda *_: self._step(-1))
        self._next = Gtk.Button(icon_name='go-next-symbolic', tooltip_text='Next Image')
        self._next.connect('clicked', lambda *_: self._step(1))
        header.pack_start(self._prev)
        header.pack_start(self._next)
        save = Gtk.Button(icon_name='document-save-symbolic', tooltip_text='Save Image…')
        save.connect('clicked', self._save)
        copy = Gtk.Button(icon_name='edit-copy-symbolic', tooltip_text='Copy Image')
        copy.connect('clicked', self._copy)
        self._zoom = Gtk.ToggleButton(icon_name='zoom-original-symbolic', tooltip_text='Original Size')
        self._zoom.connect('toggled', self._zoom_toggled)
        for b in (save, copy, self._zoom):
            header.pack_end(b)

        self._picture = Gtk.Picture(can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN, hexpand=True, vexpand=True)
        self._scroller = Gtk.ScrolledWindow(child=self._picture)
        self._spinner = Adw.Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, width_request=32, height_request=32)
        overlay = Gtk.Overlay(child=self._scroller)
        overlay.add_overlay(self._spinner)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(overlay)
        self.set_child(view)

        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', self._key)
        self.add_controller(keys)
        self._load()

    def _key(self, _ctl, keyval, _code, _state):
        if keyval == Gdk.KEY_Left:
            self._step(-1)
            return True
        if keyval == Gdk.KEY_Right:
            self._step(1)
            return True
        return False

    def _step(self, delta):
        i = self._index + delta
        if 0 <= i < len(self._atts):
            self._index = i
            self._load()

    def _load(self):
        att = self._atts[self._index]
        self._serial += 1
        serial = self._serial
        self._title.set_title(att.get('filename', 'Image'))
        self._title.set_subtitle(f'{human_size(att.get("size") or 0)} · {self._index + 1} of {len(self._atts)}')
        self._prev.set_sensitive(self._index > 0)
        self._next.set_sensitive(self._index < len(self._atts) - 1)
        self._spinner.set_visible(True)
        self._picture.set_paintable(None)

        def got_file(path):
            if serial != self._serial:
                return
            self._path = path
            self._store.worker.run(Gdk.Texture.new_from_filename, str(path), on_done=got_texture, on_error=failed)

        def got_texture(texture):
            if serial != self._serial:
                return
            self._texture = texture
            self._spinner.set_visible(False)
            self._picture.set_paintable(texture)
            self._title.set_subtitle(f'{human_size(att.get("size") or 0)} · {texture.get_width()} × {texture.get_height()} · '
                                     f'{self._index + 1} of {len(self._atts)}')

        def failed(e):
            if serial == self._serial:
                self._spinner.set_visible(False)
                self._title.set_subtitle(f'Could not load image: {e}')
        self._store.attachment_file(att, got_file, failed)

    def _zoom_toggled(self, btn):
        actual = btn.get_active()
        self._picture.set_can_shrink(not actual)
        self._picture.set_halign(Gtk.Align.START if actual else Gtk.Align.FILL)
        self._picture.set_valign(Gtk.Align.START if actual else Gtk.Align.FILL)
        btn.set_tooltip_text('Fit to Window' if actual else 'Original Size')

    def _copy(self, *_):
        if self._texture:
            self.get_clipboard().set_texture(self._texture)

    def _save(self, *_):
        if not self._path:
            return
        att = self._atts[self._index]
        dialog = Gtk.FileDialog(initial_name=att.get('filename', 'image'))

        def done(d, result):
            try:
                target = d.save_finish(result)
            except Exception:  # noqa: BLE001 - cancelled
                return
            if target:
                shutil.copyfile(self._path, target.get_path())
        dialog.save(self.get_root(), None, done)


def confirm(parent, heading, body, action_label, on_yes, destructive=False):
    dlg = Adw.AlertDialog(heading=heading, body=body)
    dlg.add_response('cancel', 'Cancel')
    dlg.add_response('ok', action_label)
    dlg.set_response_appearance('ok', Adw.ResponseAppearance.DESTRUCTIVE if destructive else Adw.ResponseAppearance.SUGGESTED)
    dlg.set_default_response('cancel')
    dlg.connect('response', lambda _d, r: on_yes() if r == 'ok' else None)
    dlg.present(parent)

