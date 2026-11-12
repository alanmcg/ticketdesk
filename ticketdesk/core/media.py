# SPDX-License-Identifier: GPL-2.0-or-later
"""Match images embedded in rich text (ADF media nodes) to the ticket's attachments.

ADF refers to files by an Atlassian media-service UUID that the REST API does not
expose on attachments. Two links are reliable:
- the node's alt text is normally the file name (used when that name is unique), and
- Jira's rendered HTML names the attachment id of every embedded file, often together
  with the media UUID (`data-media-services-id`).
The HTML is only fetched when a node cannot be matched by name.
"""

import re

_ATT_ID = re.compile(r'/(?:secure/attachment|secure/thumbnail|rest/api/[23]/attachment/(?:content|thumbnail))/(\d+)')
_TAG = re.compile(r'<(?:img|a)\b[^>]*>', re.I)
_MEDIA_ID = re.compile(r'data-media-services-id\s*=\s*"([^"]+)"', re.I)


def nodes(doc):
    """File media nodes in document order: [{'id', 'alt'}]."""
    out = []

    def walk(n):
        if not isinstance(n, dict):
            return
        if n.get('type') in ('media', 'mediaInline'):
            a = n.get('attrs') or {}
            if (a.get('type') or 'file') == 'file' and a.get('id'):
                out.append({'id': a['id'], 'alt': a.get('alt') or ''})
        for c in n.get('content') or []:
            walk(c)
    walk(doc)
    return out


def by_name(node, attachments):
    """The attachment whose file name equals the node's alt text, if exactly one does."""
    if not node.get('alt'):
        return None
    hits = [a for a in attachments or [] if a.get('filename') == node['alt']]
    return hits[0] if len(hits) == 1 else None


def needs_html(node_list, attachments, mapping):
    """True if some node can be matched neither by name nor by an earlier mapping."""
    if attachments is None:
        return bool(node_list)
    ids = {str(a.get('id')) for a in attachments}
    return any(not by_name(n, attachments) and mapping.get(n['id']) not in ids for n in node_list)


def html_refs(html):
    """Attachment references in rendered HTML, in order: [(attachment_id, media_id or None)]."""
    refs = []
    for tag in _TAG.finditer(html or ''):
        t = tag.group(0)
        m = _ATT_ID.search(t)
        if not m:
            continue
        mid = _MEDIA_ID.search(t)
        ref = (m.group(1), mid.group(1) if mid else None)
        # An image is often wrapped in a link to the same file: count it once.
        if refs and refs[-1][0] == ref[0] and (refs[-1][1] is None or ref[1] is None or refs[-1][1] == ref[1]):
            refs[-1] = (ref[0], refs[-1][1] or ref[1])
            continue
        refs.append(ref)
    return refs


def html_mapping(node_list, html):
    """{media_id: attachment_id} from rendered HTML. Exact media ids are used when present;
    otherwise nodes and references are paired by position, but only if their counts agree."""
    refs = html_refs(html)
    out = {mid: aid for aid, mid in refs if mid}
    if len(refs) == len(node_list):
        for n, (aid, mid) in zip(node_list, refs):
            if n['id'] not in out and mid is None:
                out[n['id']] = aid
    return out


def lookup(node_id, alt, mapping, attachments):
    """The attachment for a media node, or None."""
    atts = attachments or []
    aid = (mapping or {}).get(node_id)
    if aid is not None:
        for a in atts:
            if str(a.get('id')) == str(aid):
                return a
    return by_name({'alt': alt}, atts)
