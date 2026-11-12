# SPDX-License-Identifier: GPL-2.0-or-later
"""Small UI helpers."""

import datetime
import re

from gi.repository import Gio, Gtk, Pango

CATEGORY = {'new': 'todo', 'indeterminate': 'progress', 'done': 'done'}


def parse_time(value):
    if not value:
        return None
    value = re.sub(r'([+-]\d{2})(\d{2})$', r'\1:\2', value)
    try:
        return datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


def relative(value):
    """'5 min', '3 h', 'Yesterday', 'Nov 9', '2025-11-09' — compact like GNOME apps."""
    t = parse_time(value)
    if not t:
        return ''
    now = datetime.datetime.now(t.tzinfo)
    delta = now - t
    if delta.total_seconds() < 60:
        return 'Now'
    if delta.total_seconds() < 3600:
        return f'{int(delta.total_seconds() // 60)} min'
    local = t.astimezone()
    today = datetime.datetime.now().astimezone().date()
    if local.date() == today:
        return local.strftime('%H:%M')
    if local.date() == today - datetime.timedelta(days=1):
        return 'Yesterday'
    if local.year == today.year:
        return local.strftime('%b %-d')
    return local.strftime('%Y-%m-%d')


def full_time(value):
    t = parse_time(value)
    return t.astimezone().strftime('%Y-%m-%d %H:%M') if t else ''


def status_category(fields):
    key = ((fields.get('status') or {}).get('statusCategory') or {}).get('key')
    return CATEGORY.get(key, 'todo')


def human_size(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return ''


def is_image(att):
    return (att.get('mimeType') or '').startswith('image/')


def label(text='', css=(), wrap=False, xalign=0.0, selectable=False, ellipsize=None, markup=False):
    lbl = Gtk.Label(xalign=xalign, selectable=selectable)
    if markup:
        lbl.set_markup(text)
    else:
        lbl.set_text(text)
    if wrap:
        lbl.set_wrap(True)
        lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if ellipsize is not None:
        lbl.set_ellipsize(ellipsize)
    for c in css:
        lbl.add_css_class(c)
    return lbl


def open_uri(widget, uri):
    Gtk.UriLauncher.new(uri).launch(widget.get_root(), None, None)


def open_file(widget, path):
    Gtk.FileLauncher.new(Gio.File.new_for_path(str(path))).launch(widget.get_root(), None, None)


def clear(box):
    child = box.get_first_child()
    while child:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt
