# SPDX-License-Identifier: GPL-2.0-or-later
"""Images and files added while writing: placeholders in the document until they are
uploaded, then real media nodes (or, if Jira's media id cannot be found, a note)."""

import datetime
import mimetypes
import os
import struct

from .adf_html import PENDING_PREFIX


def _walk_replace(nodes, fn):
    out = []
    for n in nodes or []:
        r = fn(n)
        if r is None:
            continue
        if isinstance(r, list):
            out.extend(r)
            continue
        if isinstance(r, dict) and r.get('content') is not None:
            r = {**r, 'content': _walk_replace(r['content'], fn)}
        out.append(r)
    return out


def _pending_token(node):
    if node.get('type') != 'mediaSingle':
        return None
    for m in node.get('content') or []:
        mid = (m.get('attrs') or {}).get('id') or ''
        if m.get('type') == 'media' and mid.startswith(PENDING_PREFIX):
            return mid[len(PENDING_PREFIX):]
    return None


def pending_tokens(doc):
    found = []

    def visit(n):
        t = _pending_token(n)
        if t:
            found.append(t)
        return n
    _walk_replace((doc or {}).get('content'), visit)
    return found


def without_pending(doc):
    """The document without images that are not uploaded yet (for creating the ticket
    before its attachments exist)."""
    if not doc:
        return doc
    return {**doc, 'content': _walk_replace(doc.get('content'), lambda n: None if _pending_token(n) else n)}


def resolve(doc, uploaded):
    """Replace placeholders with uploaded files. uploaded: {token: {'media_id', 'filename'}}.
    Images whose media id is unknown become a line naming the attached file.
    Returns (doc, names_not_embedded)."""
    missing = []

    def fix(n):
        token = _pending_token(n)
        if not token:
            return n
        info = uploaded.get(token) or {}
        if not info.get('media_id'):
            name = info.get('filename') or 'image'
            missing.append(name)
            text = f'[Image attached to the ticket: {name}]' if info.get('filename') else '[Image could not be uploaded]'
            return {'type': 'paragraph', 'content': [{'type': 'text', 'text': text, 'marks': [{'type': 'em'}]}]}
        content = []
        for m in n.get('content') or []:
            if m.get('type') == 'media':
                attrs = {**(m.get('attrs') or {}), 'id': info['media_id'], 'collection': ''}
                attrs['alt'] = info.get('filename') or attrs.get('alt')
                m = {**m, 'attrs': attrs}
            content.append(m)
        return {**n, 'content': content}
    if not doc:
        return doc, missing
    return {**doc, 'content': _walk_replace(doc.get('content'), fix)}, missing


def files_block(files):
    """Attached files listed in a comment: media nodes where Jira's media id is known,
    otherwise their names. files: [{'filename', 'media_id'}]."""
    blocks = []
    known = [f for f in files if f.get('media_id')]
    unknown = [f for f in files if not f.get('media_id')]
    if known:
        blocks.append({'type': 'mediaGroup', 'content': [
            {'type': 'media', 'attrs': {'type': 'file', 'id': f['media_id'], 'collection': '', 'alt': f['filename']}}
            for f in known]})
    if unknown:
        blocks.append({'type': 'paragraph', 'content': [
            {'type': 'text', 'text': 'Attached: ' + ', '.join(f['filename'] for f in unknown), 'marks': [{'type': 'em'}]}]})
    return blocks


def append_blocks(doc, blocks):
    if not blocks:
        return doc
    doc = doc or {'type': 'doc', 'version': 1, 'content': []}
    return {**doc, 'content': list(doc.get('content') or []) + blocks}


def unique_name(name, taken, now=None):
    """A file name not used by the ticket's attachments yet. Pasted images (image.png) get a
    time stamp like Jira gives them, so images can be told apart later."""
    name = os.path.basename(name or '') or 'file'
    base, ext = os.path.splitext(name)
    if base.lower() in ('image', 'clipboard', 'pasted image', 'screenshot'):
        stamp = (now or datetime.datetime.now()).strftime('%Y%m%d-%H%M%S')
        base = f'{base.replace(" ", "-")}-{stamp}'
    candidate, n = f'{base}{ext}', 2
    while candidate in taken:
        candidate = f'{base}-{n}{ext}'
        n += 1
    taken.add(candidate)
    return candidate


def guess_mime(name):
    return mimetypes.guess_type(name)[0] or 'application/octet-stream'


def image_size(data):
    """(width, height) of PNG, GIF, JPEG or WebP data, or None."""
    try:
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            return struct.unpack('>II', data[16:24])
        if data[:6] in (b'GIF87a', b'GIF89a'):
            return struct.unpack('<HH', data[6:10])
        if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            chunk = data[12:16]
            if chunk == b'VP8 ':
                w, h = struct.unpack('<HH', data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if chunk == b'VP8L':
                b = data[21:25]
                w = 1 + (((b[1] & 0x3F) << 8) | b[0])
                h = 1 + (((b[3] & 0xF) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
                return w, h
            if chunk == b'VP8X':
                w = 1 + int.from_bytes(data[24:27], 'little')
                h = 1 + int.from_bytes(data[27:30], 'little')
                return w, h
        if data[:2] == b'\xff\xd8':
            i = 2
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack('>HH', data[i + 5:i + 9])
                    return w, h
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                i += 2 + struct.unpack('>H', data[i + 2:i + 4])[0]
    except (struct.error, IndexError):
        return None
    return None
