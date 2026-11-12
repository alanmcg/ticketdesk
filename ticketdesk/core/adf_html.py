# SPDX-License-Identifier: GPL-2.0-or-later
"""Convert between ADF (Jira's rich text) and the HTML used by the document editor.

The editor supports paragraphs, headings, bullet/numbered/task lists, code blocks,
quotes, panels, rules, tables (with merged cells and header cells) and the inline marks
bold, italic, underline, strike, code, links, sub/superscript and text colour.

Images (`mediaSingle` with a file) become resizable figures. Images added in the editor
are "pending": their data travels in the figure until it is uploaded; html_to_adf
collects it and leaves a placeholder media id (`td-pending:<token>`) to be replaced with
the uploaded file's id (see core/attachments.py).

Everything else (files, mentions, emoji, dates, status lozenges, cards, expands,
macros…) is carried through unchanged: it is shown as a locked item whose original ADF
travels in a `data-adf` attribute, so editing a description never silently drops content.
"""

import base64
import binascii
import html
import json
import re
import threading
import uuid
from html.parser import HTMLParser

# ---------------------------------------------------------------- ADF -> HTML

PENDING_PREFIX = 'td-pending:'
DOC_WIDTH = 760  # Jira's default content width, for converting pixel image widths to percent
_MARK_TAGS = {'strong': 'strong', 'em': 'em', 'underline': 'u', 'strike': 's', 'code': 'code'}
_PANEL_TYPES = ('info', 'note', 'success', 'warning', 'error')


def _b64(obj):
    return base64.b64encode(json.dumps(obj, separators=(',', ':')).encode()).decode()


def _unb64(s):
    try:
        return json.loads(base64.b64decode(s.encode()).decode())
    except (ValueError, TypeError):
        return None


def _esc(s):
    return html.escape(str(s), quote=True)


def _inline_label(n):
    a = n.get('attrs') or {}
    t = n.get('type')
    if t == 'mention':
        return a.get('text') or '@' + (a.get('id') or 'someone')
    if t == 'emoji':
        return a.get('text') or a.get('shortName') or ''
    if t == 'date':
        try:
            import datetime
            return datetime.datetime.fromtimestamp(int(a.get('timestamp')) / 1000, datetime.timezone.utc).date().isoformat()
        except (TypeError, ValueError):
            return 'date'
    if t == 'status':
        return a.get('text') or 'status'
    if t == 'inlineCard':
        return a.get('url') or 'link'
    if t == 'mediaInline':
        return a.get('alt') or 'file'
    return t or '?'


def _block_label(n):
    a = n.get('attrs') or {}
    t = n.get('type')
    if t in ('mediaSingle', 'mediaGroup'):
        names = [((m.get('attrs') or {}).get('alt') or 'file') for m in n.get('content') or []]
        return ('Image: ' if t == 'mediaSingle' else 'Files: ') + ', '.join(names)
    if t in ('expand', 'nestedExpand'):
        return f'Expandable section: {a.get("title") or ""}'.strip()
    if t in ('extension', 'bodiedExtension', 'inlineExtension'):
        return f'Jira add-on content: {a.get("extensionKey") or ""}'.strip()
    if t in ('blockCard', 'embedCard'):
        return f'Link card: {a.get("url") or ""}'.strip()
    if t == 'layoutSection':
        return 'Column layout'
    if t == 'decisionList':
        return 'Decisions'
    return f'{t} (kept as is)'


def _marks_open_close(marks):
    opens, closes, extra = [], [], []
    for m in marks or []:
        t = m.get('type')
        a = m.get('attrs') or {}
        if t in _MARK_TAGS:
            tag = _MARK_TAGS[t]
            opens.append(f'<{tag}>')
            closes.insert(0, f'</{tag}>')
        elif t == 'link':
            opens.append(f'<a href="{_esc(a.get("href") or "")}">')
            closes.insert(0, '</a>')
        elif t == 'subsup':
            tag = 'sup' if a.get('type') == 'sup' else 'sub'
            opens.append(f'<{tag}>')
            closes.insert(0, f'</{tag}>')
        elif t == 'textColor' and a.get('color'):
            c = _esc(a['color'])
            opens.append(f'<span data-color="{c}" style="color:{c}">')
            closes.insert(0, '</span>')
        else:
            extra.append(m)  # e.g. inline comment anchors: kept, not editable as such
    if extra:
        opens.insert(0, f'<span data-marks="{_b64(extra)}">')
        closes.append('</span>')
    return ''.join(opens), ''.join(closes)


def _inlines_html(nodes):
    out = []
    for n in nodes or []:
        t = n.get('type')
        if t == 'text':
            o, c = _marks_open_close(n.get('marks'))
            out.append(o + _esc(n.get('text') or '') + c)
        elif t == 'hardBreak':
            out.append('<br>')
        else:
            out.append(f'<span class="adf-inline" contenteditable="false" data-adf="{_b64(n)}">'
                       f'{_esc(_inline_label(n))}</span>')
    return ''.join(out)


def _para_html(n, tag='p'):
    inner = _inlines_html(n.get('content'))
    return f'<{tag}>{inner or "<br>"}</{tag}>'


def _blocks_html(nodes):
    return ''.join(_block_html(n) for n in nodes or [])


def _list_item_html(item):
    content = item.get('content') or []
    first, rest = '', content
    if content and content[0].get('type') == 'paragraph':
        first, rest = _inlines_html(content[0].get('content')), content[1:]
    inner = first + _blocks_html(rest)
    return f'<li>{inner or "<br>"}</li>'


def _cell_html(cell):
    a = cell.get('attrs') or {}
    tag = 'th' if cell.get('type') == 'tableHeader' else 'td'
    attrs = ''
    if (a.get('colspan') or 1) > 1:
        attrs += f' colspan="{int(a["colspan"])}"'
    if (a.get('rowspan') or 1) > 1:
        attrs += f' rowspan="{int(a["rowspan"])}"'
    keep = {k: v for k, v in a.items() if k not in ('colspan', 'rowspan')}
    if keep:
        attrs += f' data-adf-attrs="{_b64(keep)}"'
    inner = _blocks_html(cell.get('content')) or '<p><br></p>'
    return f'<{tag}{attrs}>{inner}</{tag}>'


def _image_width_pct(a, media_attrs):
    """mediaSingle width as a percentage of the text width (None = natural size)."""
    w = a.get('width')
    if w is None:
        return None
    try:
        w = float(w)
    except (TypeError, ValueError):
        return None
    if a.get('widthType') == 'pixel':
        w = w / DOC_WIDTH * 100
    return max(5, min(100, round(w)))


def _figure_html(n):
    """An image (mediaSingle with one file media node) as an editable figure."""
    a = n.get('attrs') or {}
    m = ((n.get('content') or [{}])[0].get('attrs')) or {}
    pct = _image_width_pct(a, m)
    width = f' data-width="{pct}" data-orig-width="{pct}"' if pct is not None else ''
    dims = ''.join(f' data-{k[0]}="{int(m[k])}"' for k in ('width', 'height') if isinstance(m.get(k), (int, float)))
    alt = _esc(m.get('alt') or '')
    return (f'<figure class="td-image" contenteditable="false" data-adf="{_b64(n)}" '
            f'data-media-id="{_esc(m.get("id") or "")}" data-alt="{alt}"{width}{dims}>'
            f'<img alt="{alt}"></figure>')


def _is_image_node(n):
    content = n.get('content') or []
    media = [c for c in content if c.get('type') == 'media']
    if n.get('type') != 'mediaSingle' or len(media) != 1:
        return False
    return (media[0].get('attrs') or {}).get('type', 'file') == 'file' and bool((media[0].get('attrs') or {}).get('id'))


def _block_html(n):
    t = n.get('type')
    a = n.get('attrs') or {}
    if _is_image_node(n):
        return _figure_html(n)
    if t == 'paragraph':
        return _para_html(n)
    if t == 'heading':
        level = min(max(int(a.get('level') or 1), 1), 6)
        return _para_html(n, f'h{level}')
    if t == 'bulletList':
        return '<ul>' + ''.join(_list_item_html(i) for i in n.get('content') or []) + '</ul>'
    if t == 'orderedList':
        order = a.get('order')
        start = f' start="{int(order)}"' if order not in (None, 1) else ''
        return f'<ol{start}>' + ''.join(_list_item_html(i) for i in n.get('content') or []) + '</ol>'
    if t == 'taskList':
        items = []
        for i in n.get('content') or []:
            if i.get('type') == 'taskItem':
                ia = i.get('attrs') or {}
                done = 'true' if ia.get('state') == 'DONE' else 'false'
                items.append(f'<li data-done="{done}" data-local-id="{_esc(ia.get("localId") or "")}">'
                             f'{_inlines_html(i.get("content")) or "<br>"}</li>')
            elif i.get('type') == 'taskList':  # nested task list: kept as a locked block
                items.append(f'<li data-done="false">{_esc("(nested tasks)")}</li>')
        return f'<ul data-type="tasks" data-local-id="{_esc(a.get("localId") or "")}">' + ''.join(items) + '</ul>'
    if t == 'codeBlock':
        text = ''.join(c.get('text') or '' for c in n.get('content') or [])
        lang = f' data-lang="{_esc(a["language"])}"' if a.get('language') else ''
        return f'<pre{lang}>{_esc(text) or "<br>"}</pre>'
    if t == 'blockquote':
        return '<blockquote>' + (_blocks_html(n.get('content')) or '<p><br></p>') + '</blockquote>'
    if t == 'panel':
        kind = a.get('panelType') if a.get('panelType') in _PANEL_TYPES else 'info'
        extra = {k: v for k, v in a.items() if k != 'panelType'}
        keep = f' data-adf-attrs="{_b64(extra)}"' if extra else ''
        return (f'<div class="panel" data-panel="{kind}"{keep}>'
                + (_blocks_html(n.get('content')) or '<p><br></p>') + '</div>')
    if t == 'rule':
        return '<hr>'
    if t == 'table':
        keep = f' data-adf-attrs="{_b64(a)}"' if a else ''
        rows = ''.join('<tr>' + ''.join(_cell_html(c) for c in r.get('content') or []) + '</tr>'
                       for r in n.get('content') or [] if r.get('type') == 'tableRow')
        return f'<table{keep}><tbody>{rows}</tbody></table>'
    return (f'<div class="adf-block" contenteditable="false" data-adf="{_b64(n)}">'
            f'{_esc(_block_label(n))}</div>')


def adf_to_html(doc):
    if not isinstance(doc, dict):
        return '<p><br></p>'
    return _blocks_html(doc.get('content')) or '<p><br></p>'


# ---------------------------------------------------------------- HTML -> ADF

class _Node:
    __slots__ = ('tag', 'attrs', 'children', 'parent')

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = dict(attrs or {})
        self.children = []
        self.parent = parent


_VOID = {'br', 'hr', 'img', 'input', 'meta', 'link', 'col', 'wbr', 'source'}
_SKIP = {'script', 'style', 'head', 'title', 'template', 'noscript'}


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node('#root')
        self.cur = self.root
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        node = _Node(tag, attrs, self.cur)
        self.cur.children.append(node)
        if tag not in _VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        if tag in _SKIP or self.skip:
            return
        self.cur.children.append(_Node(tag, attrs, self.cur))

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag in _VOID:
            return
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        if not self.skip:
            self.cur.children.append(data)


_BLOCK_TAGS = {'p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'pre', 'blockquote', 'table',
               'hr', 'section', 'article', 'header', 'footer', 'main', 'figure', 'dl', 'dt', 'dd', 'thead',
               'tbody', 'tfoot', 'tr', 'td', 'th', 'address', 'nav', 'aside', 'details', 'summary'}
_SPACE = '\x00'  # whitespace that came from line breaks in the HTML source


def _is_block(n):
    return isinstance(n, _Node) and (n.tag in _BLOCK_TAGS or ('data-adf' in n.attrs and n.tag != 'span'))


def _norm_text(s, pre=False):
    s = s.replace('\xa0', ' ')
    if pre:
        return s
    return re.sub(r'[ \t\r]*\n[ \t\r\n]*', _SPACE, s)


def _add_mark(marks, mark):
    if any(m['type'] == mark['type'] for m in marks):
        return marks
    return marks + [mark]


def _clean_marks(marks):
    if any(m['type'] == 'code' for m in marks):  # code combines only with links in ADF
        marks = [m for m in marks if m['type'] in ('code', 'link')]
    return marks


def _inlines(nodes, marks=()):
    out = []
    for n in nodes:
        if isinstance(n, str):
            text = _norm_text(n)
            if text:
                out.append({'type': 'text', 'text': text, 'marks': list(marks)})
            continue
        tag = n.tag
        if tag == 'br':
            out.append({'type': 'hardBreak'})
            continue
        if 'data-adf' in n.attrs:
            node = _unb64(n.attrs['data-adf'])
            if isinstance(node, dict):
                out.append(node)
            continue
        if tag in ('img', 'input', 'hr'):
            continue
        m = list(marks)
        if tag in ('strong', 'b'):
            m = _add_mark(m, {'type': 'strong'})
        elif tag in ('em', 'i'):
            m = _add_mark(m, {'type': 'em'})
        elif tag == 'u':
            m = _add_mark(m, {'type': 'underline'})
        elif tag in ('s', 'strike', 'del'):
            m = _add_mark(m, {'type': 'strike'})
        elif tag in ('code', 'kbd', 'samp', 'tt'):
            m = _add_mark(m, {'type': 'code'})
        elif tag == 'a' and n.attrs.get('href'):
            m = _add_mark(m, {'type': 'link', 'attrs': {'href': n.attrs['href']}})
        elif tag in ('sub', 'sup'):
            m = _add_mark(m, {'type': 'subsup', 'attrs': {'type': tag}})
        elif tag == 'span':
            if n.attrs.get('data-color'):
                m = _add_mark(m, {'type': 'textColor', 'attrs': {'color': n.attrs['data-color']}})
            for extra in _unb64(n.attrs['data-marks']) or [] if n.attrs.get('data-marks') else []:
                if isinstance(extra, dict) and extra.get('type'):
                    m = _add_mark(m, extra)
        out.extend(_inlines(n.children, tuple(m)))
    return out


def _finish_inlines(items):
    """Merge neighbouring text with equal marks, trim source line breaks at the edges,
    drop empty text, and tidy marks."""
    merged = []
    for it in items:
        if it.get('type') == 'text':
            it = {**it, 'marks': _clean_marks(it.get('marks') or [])}
            if merged and merged[-1].get('type') == 'text' and merged[-1]['marks'] == it['marks']:
                merged[-1] = {**merged[-1], 'text': merged[-1]['text'] + it['text']}
                continue
        merged.append(it)
    if merged and merged[0].get('type') == 'text':
        merged[0] = {**merged[0], 'text': merged[0]['text'].lstrip(_SPACE)}
    if merged and merged[-1].get('type') == 'text':
        merged[-1] = {**merged[-1], 'text': merged[-1]['text'].rstrip(_SPACE)}
    while merged and merged[-1].get('type') == 'hardBreak':  # editors leave a trailing <br>
        merged.pop()
    out = []
    for it in merged:
        if it.get('type') == 'text':
            text = it['text'].replace(_SPACE, ' ')
            if not text:
                continue
            node = {'type': 'text', 'text': text}
            if it['marks']:
                node['marks'] = it['marks']
            out.append(node)
        else:
            out.append(it)
    return out


def _paragraph(nodes):
    return {'type': 'paragraph', 'content': _finish_inlines(_inlines(nodes))}


def _text_of(n, pre=False):
    """Text of a node; in code blocks, <br> and block elements are line breaks."""
    if isinstance(n, str):
        return _norm_text(n, pre)
    if n.tag == 'br':
        return '\n'
    out = ''
    for c in n.children:
        part = _text_of(c, pre)
        if pre and isinstance(c, _Node) and c.tag in ('p', 'div', 'li', 'tr'):
            if out and not out.endswith('\n'):
                out += '\n'
            if not part.endswith('\n'):
                part += '\n'
        out += part
    return out


def _blocks(nodes, ctx='doc'):
    """Convert a run of HTML nodes to ADF blocks. Inline content between blocks becomes
    paragraphs. `ctx` limits what may appear (list items, cells, quotes, panels)."""
    out, pending = [], []

    def flush():
        if pending and all(isinstance(x, str) and not x.strip(' \t\r\n\xa0') for x in pending):
            pending.clear()  # whitespace between blocks
        if pending:
            para = _paragraph(pending)
            if para['content']:
                out.append(para)
            pending.clear()

    for n in nodes:
        if not _is_block(n):
            if isinstance(n, str) and not pending and not n.strip(' \t\r\n\xa0') and '\n' in n:
                continue  # formatting whitespace between blocks
            pending.append(n)
            continue
        flush()
        out.extend(_block(n, ctx))
    flush()
    return out


def _empty_para():
    return {'type': 'paragraph', 'content': []}


def _list_items(n, ctx):
    items = []
    stray = []
    for c in n.children:
        if isinstance(c, _Node) and c.tag == 'li':
            if stray:
                items.append({'type': 'listItem', 'content': [_paragraph(stray)]})
                stray = []
            content = _blocks(c.children, 'listItem')
            if not content or content[0]['type'] != 'paragraph':
                content.insert(0, _empty_para())
            items.append({'type': 'listItem', 'content': content})
        elif isinstance(c, _Node) and c.tag in ('ul', 'ol') and items:
            items[-1]['content'].extend(_block(c, 'listItem'))  # list nested without an <li>
        elif isinstance(c, str) and not c.strip():
            continue
        else:
            stray.append(c)
    if stray:
        items.append({'type': 'listItem', 'content': [_paragraph(stray)]})
    return items


_ALLOWED = {
    # what each container may hold; anything else is reduced to paragraphs of its text
    'listItem': {'paragraph', 'bulletList', 'orderedList', 'taskList', 'codeBlock', 'mediaSingle'},
    'blockquote': {'paragraph', 'bulletList', 'orderedList', 'codeBlock', 'mediaGroup', 'mediaSingle'},
    'panel': {'paragraph', 'heading', 'bulletList', 'orderedList', 'taskList', 'rule', 'codeBlock',
              'mediaGroup', 'mediaSingle', 'decisionList', 'blockCard'},
    'cell': {'paragraph', 'panel', 'blockquote', 'orderedList', 'bulletList', 'rule', 'heading', 'codeBlock',
             'mediaGroup', 'mediaSingle', 'decisionList', 'taskList', 'extension', 'nestedExpand', 'blockCard',
             'embedCard'},
}


def _fit(block, ctx):
    allowed = _ALLOWED.get(ctx)
    if allowed is None or block['type'] in allowed:
        return [block]
    if block['type'] == 'heading':
        return [{'type': 'paragraph', 'content': block.get('content') or []}]
    text = _plain(block).strip()
    return [{'type': 'paragraph', 'content': [{'type': 'text', 'text': line}] if line else []}
            for line in text.split('\n')] if text else []


def _plain(node):
    if node.get('type') == 'text':
        return node.get('text') or ''
    if node.get('type') == 'hardBreak':
        return '\n'
    sep = '\n' if node.get('type') in ('table', 'tableRow', 'bulletList', 'orderedList', 'taskList', 'doc') else ''
    parts = [_plain(c) for c in node.get('content') or []]
    if node.get('type') in ('tableCell', 'tableHeader'):
        return ' '.join(p.strip() for p in parts)
    if node.get('type') == 'tableRow':
        return ' | '.join(parts)
    return sep.join(parts)


def _figure(n, ctx):
    """A figure from the editor: an existing image (data-adf, maybe resized) or one added
    in the editor (data-pending, data in the img src)."""
    a = n.attrs
    try:
        pct = max(5, min(100, int(float(a.get('data-width'))))) if a.get('data-width') else None
    except ValueError:
        pct = None
    if a.get('data-adf'):
        node = _unb64(a['data-adf'])
        if not isinstance(node, dict):
            return []
        if pct is not None and str(pct) != a.get('data-orig-width'):
            attrs = {k: v for k, v in (node.get('attrs') or {}).items() if k not in ('width', 'widthType')}
            attrs.update({'width': pct, 'widthType': 'percentage'})
            attrs.setdefault('layout', 'center')
            node = {**node, 'attrs': attrs}
        return _fit(node, ctx)
    token = a.get('data-pending')
    img = next((c for c in n.children if isinstance(c, _Node) and c.tag == 'img'), None)
    src = (img.attrs.get('src') if img else '') or ''
    m = re.match(r'data:([\w/+.-]+);base64,(.*)$', src, re.S)
    if not token or not m:
        return []
    try:
        data = base64.b64decode(m.group(2), validate=False)
    except (binascii.Error, ValueError):
        return []
    store = getattr(_STATE, 'pending', None)
    name = a.get('data-alt') or 'image.png'
    media = {'type': 'file', 'id': PENDING_PREFIX + token, 'collection': '', 'alt': name}
    for k, attr in (('width', 'data-w'), ('height', 'data-h')):
        try:
            if a.get(attr):
                media[k] = int(float(a[attr]))
        except ValueError:
            pass
    if store is not None:
        store[token] = {'name': name, 'mime': m.group(1), 'data': data,
                        'width': media.get('width'), 'height': media.get('height')}
    attrs = {'layout': 'center'}
    if pct is not None:
        attrs.update({'width': pct, 'widthType': 'percentage'})
    return _fit({'type': 'mediaSingle', 'attrs': attrs, 'content': [{'type': 'media', 'attrs': media}]}, ctx)


def _block(n, ctx):
    tag = n.tag
    a = n.attrs
    if tag == 'figure' and 'td-image' in (a.get('class') or '').split():
        return _figure(n, ctx)
    if 'data-adf' in a:
        node = _unb64(a['data-adf'])
        return _fit(node, ctx) if isinstance(node, dict) else []
    if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
        b = {'type': 'heading', 'attrs': {'level': int(tag[1])}, 'content': _finish_inlines(_inlines(n.children))}
        return _fit(b, ctx)
    if tag == 'ul' and a.get('data-type') == 'tasks':
        list_id = a.get('data-local-id') or str(uuid.uuid4())
        items = []
        for c in n.children:
            if isinstance(c, _Node) and c.tag == 'li':
                items.append({'type': 'taskItem',
                              'attrs': {'localId': c.attrs.get('data-local-id') or str(uuid.uuid4()),
                                        'state': 'DONE' if c.attrs.get('data-done') == 'true' else 'TODO'},
                              'content': _finish_inlines(_inlines([x for x in c.children if not _is_block(x)]))})
        return _fit({'type': 'taskList', 'attrs': {'localId': list_id}, 'content': items}, ctx) if items else []
    if tag in ('ul', 'ol'):
        items = _list_items(n, ctx)
        if not items:
            return []
        b = {'type': 'bulletList' if tag == 'ul' else 'orderedList', 'content': items}
        if tag == 'ol':
            try:
                start = int(a.get('start') or 1)
            except ValueError:
                start = 1
            if start != 1:
                b['attrs'] = {'order': start}
        return _fit(b, ctx)
    if tag == 'li':  # a list item outside a list
        return _blocks(n.children, ctx)
    if tag == 'pre':
        text = _text_of(n, pre=True).rstrip('\n')
        b = {'type': 'codeBlock', 'content': [{'type': 'text', 'text': text}] if text else []}
        if a.get('data-lang'):
            b['attrs'] = {'language': a['data-lang']}
        return _fit(b, ctx)
    if tag == 'blockquote':
        content = _blocks(n.children, 'blockquote') or [_empty_para()]
        return _fit({'type': 'blockquote', 'content': content}, ctx)
    if tag == 'div' and 'panel' in (a.get('class') or '').split():
        attrs = {'panelType': a.get('data-panel') if a.get('data-panel') in _PANEL_TYPES else 'info'}
        if a.get('data-adf-attrs'):
            attrs.update(_unb64(a['data-adf-attrs']) or {})
        content = _blocks(n.children, 'panel') or [_empty_para()]
        return _fit({'type': 'panel', 'attrs': attrs, 'content': content}, ctx)
    if tag == 'hr':
        return _fit({'type': 'rule'}, ctx)
    if tag == 'table':
        return _table(n, ctx)
    if tag in ('tr', 'td', 'th', 'thead', 'tbody', 'tfoot'):  # table parts outside a table
        return _blocks(n.children, ctx)
    if tag == 'p' and not any(_is_block(c) for c in n.children):
        return [_paragraph(n.children)]
    # div and other containers: paragraph if inline-only, otherwise their blocks
    if not any(_is_block(c) for c in n.children):
        return [_paragraph(n.children)]
    return _blocks(n.children, ctx)


def _table(n, ctx):
    rows = []

    def collect(node):
        for c in node.children:
            if isinstance(c, _Node):
                if c.tag == 'tr':
                    rows.append(c)
                elif c.tag in ('thead', 'tbody', 'tfoot'):
                    collect(c)
    collect(n)
    out_rows = []
    for r in rows:
        cells = []
        for c in r.children:
            if not (isinstance(c, _Node) and c.tag in ('td', 'th')):
                continue
            attrs = {}
            if c.attrs.get('data-adf-attrs'):
                attrs.update(_unb64(c.attrs['data-adf-attrs']) or {})
            for k in ('colspan', 'rowspan'):
                try:
                    v = int(c.attrs.get(k) or 1)
                except ValueError:
                    v = 1
                if v > 1:
                    attrs[k] = v
            content = []
            for b in _blocks(c.children, 'cell'):
                content.extend(_fit(b, 'cell'))
            cell = {'type': 'tableHeader' if c.tag == 'th' else 'tableCell', 'content': content or [_empty_para()]}
            if attrs:
                cell['attrs'] = attrs
            cells.append(cell)
        if cells:
            out_rows.append({'type': 'tableRow', 'content': cells})
    if not out_rows:
        return []
    if ctx != 'doc':  # tables cannot be nested; keep their text
        return _fit({'type': 'table', 'content': out_rows}, 'cell' if ctx == 'cell' else ctx)
    table = {'type': 'table', 'content': out_rows}
    if n.attrs.get('data-adf-attrs'):
        table['attrs'] = _unb64(n.attrs['data-adf-attrs']) or {}
    return [table]


_STATE = threading.local()


def html_to_adf(source, pending=None):
    """Editor HTML to an ADF document. Images added in the editor are put into `pending`
    ({token: {'name', 'mime', 'data', 'width', 'height'}}) when a dict is given."""
    p = _Parser()
    p.feed(source or '')
    p.close()
    _STATE.pending = pending
    try:
        content = _blocks(p.root.children, 'doc')
    finally:
        _STATE.pending = None
    return {'type': 'doc', 'version': 1, 'content': content}


def is_empty(doc):
    """True if the document has no text and no other content."""
    for b in (doc or {}).get('content') or []:
        if b.get('type') != 'paragraph' or b.get('content'):
            return False
    return True
