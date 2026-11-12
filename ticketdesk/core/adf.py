# SPDX-License-Identifier: GPL-2.0-or-later
"""Atlassian Document Format (ADF): Jira's rich text for descriptions and comments.

`to_blocks()` turns an ADF document into a small, display-oriented tree that the UI
renders with native widgets. Unknown node types are kept as "unsupported" blocks so
nothing is silently dropped from view.
"""

import datetime
import html

PANEL_KINDS = {'info', 'note', 'success', 'warning', 'error', 'tip', 'custom'}


def to_text(node):
    """Plain text, for search indexing and notification bodies."""
    if not isinstance(node, dict):
        return node if isinstance(node, str) else ''
    t = node.get('type')
    attrs = node.get('attrs') or {}
    if t == 'text':
        return node.get('text', '')
    if t == 'mention':
        return attrs.get('text') or '@user'
    if t in ('emoji', 'status'):
        return attrs.get('text') or attrs.get('shortName') or ''
    if t == 'date':
        return _date(attrs.get('timestamp'))
    if t in ('inlineCard', 'blockCard', 'embedCard'):
        return attrs.get('url') or ''
    if t == 'hardBreak':
        return '\n'
    inner = ''.join(to_text(c) for c in node.get('content') or [])
    if t in ('paragraph', 'heading', 'codeBlock', 'listItem', 'taskItem', 'tableRow', 'blockquote'):
        return inner.rstrip('\n') + '\n'
    if t in ('tableCell', 'tableHeader'):
        return inner.strip() + '\t'
    return inner


def mentions(node, account_id):
    if not isinstance(node, dict):
        return False
    if node.get('type') == 'mention' and (node.get('attrs') or {}).get('id') == account_id:
        return True
    return any(mentions(c, account_id) for c in node.get('content') or [])


def text_to_adf(text):
    """Plain text -> ADF, one paragraph per line (empty lines become empty paragraphs)."""
    lines = text.replace('\r\n', '\n').split('\n')
    return {'type': 'doc', 'version': 1, 'content': [
        {'type': 'paragraph', 'content': [{'type': 'text', 'text': line}] if line else []}
        for line in lines]}


def _date(ts):
    try:
        return datetime.datetime.fromtimestamp(int(ts) / 1000, datetime.timezone.utc).strftime('%Y-%m-%d')
    except (TypeError, ValueError, OverflowError, OSError):
        return ''


# ---------- display tree ----------

def _inlines(nodes):
    out = []
    for n in nodes or []:
        t = n.get('type')
        attrs = n.get('attrs') or {}
        if t == 'text':
            marks = {}
            for m in n.get('marks') or []:
                mt = m.get('type')
                if mt == 'link':
                    marks['link'] = (m.get('attrs') or {}).get('href', '')
                elif mt == 'subsup':
                    marks[(m.get('attrs') or {}).get('type', 'sub')] = True
                elif mt in ('strong', 'em', 'strike', 'code', 'underline'):
                    marks[mt] = True
            out.append({'kind': 'text', 'text': n.get('text', ''), 'marks': marks})
        elif t == 'hardBreak':
            out.append({'kind': 'break'})
        elif t == 'mention':
            out.append({'kind': 'mention', 'text': attrs.get('text') or '@user', 'id': attrs.get('id')})
        elif t == 'emoji':
            out.append({'kind': 'text', 'text': attrs.get('text') or attrs.get('shortName') or '', 'marks': {}})
        elif t == 'status':
            out.append({'kind': 'status', 'text': attrs.get('text', ''), 'color': attrs.get('color', 'neutral')})
        elif t == 'date':
            out.append({'kind': 'text', 'text': _date(attrs.get('timestamp')), 'marks': {}})
        elif t == 'inlineCard':
            url = attrs.get('url') or ''
            out.append({'kind': 'text', 'text': url, 'marks': {'link': url}})
        elif t == 'mediaInline':
            out.append({'kind': 'text', 'text': f'[{attrs.get("alt") or "attachment"}]', 'marks': {}})
        elif n.get('content'):
            out.extend(_inlines(n['content']))
        elif n.get('text'):
            out.append({'kind': 'text', 'text': n['text'], 'marks': {}})
    return out


def _media(node):
    attrs = node.get('attrs') or {}
    return {'type': 'media', 'id': attrs.get('id'), 'alt': attrs.get('alt') or '',
            'media_type': attrs.get('type', 'file'), 'url': attrs.get('url')}


def _blocks(nodes):
    out = []
    for n in nodes or []:
        t = n.get('type')
        attrs = n.get('attrs') or {}
        content = n.get('content') or []
        if t == 'paragraph':
            out.append({'type': 'paragraph', 'inlines': _inlines(content)})
        elif t == 'heading':
            out.append({'type': 'heading', 'level': int(attrs.get('level') or 1), 'inlines': _inlines(content)})
        elif t in ('bulletList', 'orderedList'):
            out.append({'type': 'list', 'ordered': t == 'orderedList', 'start': int(attrs.get('order') or 1),
                        'items': [_blocks(li.get('content')) for li in content if li.get('type') == 'listItem']})
        elif t in ('taskList', 'decisionList'):
            items = []
            for it in content:
                if it.get('type') in ('taskItem', 'decisionItem'):
                    items.append({'done': (it.get('attrs') or {}).get('state') in ('DONE', 'DECIDED'),
                                  'inlines': _inlines(it.get('content'))})
                elif it.get('type') in ('taskList', 'decisionList'):
                    items.extend(_blocks([it])[0]['items'])
            out.append({'type': 'tasks', 'decision': t == 'decisionList', 'items': items})
        elif t == 'codeBlock':
            out.append({'type': 'code', 'lang': attrs.get('language') or '',
                        'text': ''.join(c.get('text', '') for c in content)})
        elif t == 'blockquote':
            out.append({'type': 'quote', 'blocks': _blocks(content)})
        elif t == 'panel':
            kind = attrs.get('panelType', 'info')
            out.append({'type': 'panel', 'kind': kind if kind in PANEL_KINDS else 'info', 'blocks': _blocks(content)})
        elif t == 'rule':
            out.append({'type': 'rule'})
        elif t in ('mediaSingle', 'mediaGroup'):
            width = None
            if t == 'mediaSingle':  # the width set in the editor (percent of the text width)
                from .adf_html import _image_width_pct
                width = _image_width_pct(n.get('attrs') or {}, {})
            for m in content:
                if m.get('type') == 'media':
                    out.append({**_media(m), 'width_pct': width})
        elif t == 'media':
            out.append(_media(n))
        elif t == 'table':
            rows = []
            for row in content:
                if row.get('type') != 'tableRow':
                    continue
                cells = []
                for c in row.get('content') or []:
                    ca = c.get('attrs') or {}
                    cells.append({'header': c.get('type') == 'tableHeader',
                                  'colspan': max(1, int(ca.get('colspan') or 1)),
                                  'rowspan': max(1, int(ca.get('rowspan') or 1)),
                                  'blocks': _blocks(c.get('content'))})
                rows.append(cells)
            out.append({'type': 'table', 'rows': rows})
        elif t in ('expand', 'nestedExpand'):
            out.append({'type': 'expand', 'title': attrs.get('title') or '', 'blocks': _blocks(content)})
        elif t in ('extension', 'bodiedExtension', 'inlineExtension'):
            title = (attrs.get('parameters') or {}).get('macroMetadata', {}).get('title') or attrs.get('extensionKey') or 'Add-on content'
            out.append({'type': 'extension', 'title': title})
        elif t in ('blockCard', 'embedCard'):
            url = attrs.get('url') or ''
            out.append({'type': 'paragraph', 'inlines': [{'kind': 'text', 'text': url, 'marks': {'link': url}}]})
        elif t == 'layoutSection':
            for col in content:
                out.extend(_blocks(col.get('content')))
        else:
            out.append({'type': 'unsupported', 'name': t or 'unknown'})
    return out


def to_blocks(doc):
    if not isinstance(doc, dict):
        return [{'type': 'paragraph', 'inlines': [{'kind': 'text', 'text': doc, 'marks': {}}]}] if doc else []
    return _blocks(doc.get('content'))


# ---------- Pango markup for inline content ----------

def esc(text):
    return html.escape(text or '', quote=True)


def inlines_to_markup(inlines):
    parts = []
    for i in inlines:
        k = i.get('kind')
        if k == 'break':
            parts.append('\n')
        elif k == 'mention':
            parts.append(f'<b>{esc(i["text"])}</b>')
        elif k == 'status':
            parts.append(f'<span size="small" weight="bold"> {esc(i["text"].upper())} </span>')
        else:
            s = esc(i.get('text', ''))
            m = i.get('marks') or {}
            if m.get('code'):
                s = f'<tt>{s}</tt>'
            if m.get('strong'):
                s = f'<b>{s}</b>'
            if m.get('em'):
                s = f'<i>{s}</i>'
            if m.get('strike'):
                s = f'<s>{s}</s>'
            if m.get('underline'):
                s = f'<u>{s}</u>'
            if m.get('sub'):
                s = f'<sub>{s}</sub>'
            if m.get('sup'):
                s = f'<sup>{s}</sup>'
            if m.get('link'):
                s = f'<a href="{esc(m["link"])}">{s}</a>'
            parts.append(s)
    return ''.join(parts)
