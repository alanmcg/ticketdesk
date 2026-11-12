# SPDX-License-Identifier: GPL-2.0-or-later
"""Linking tickets: the few relations people use most, and how each is made in Jira.

Jira link types have an inward and an outward phrase ("Blocks": "is blocked by" /
"blocks"). A ticket shows a link with `outwardIssue: X` as "<outward> X", with
`inwardIssue: X` as "<inward> X". When creating a link (POST /issueLink) the ticket in
`inwardIssue` is the one the outward phrase belongs to: inwardIssue A, outwardIssue B
gives "A blocks B". Store.link checks the result against that and repairs a reversed
link, so a site that behaves otherwise still gets what was asked for.

Parent and child are not links but the `parent` field."""

import re

# (id, label): "<this ticket> <label> <other ticket>"
KINDS = [
    ('relates', 'Relates to'),
    ('blocks', 'Blocks'),
    ('blocked', 'Is blocked by'),
    ('duplicates', 'Duplicates'),
    ('duplicated', 'Is duplicated by'),
    ('child', 'Is child of'),
    ('parent', 'Is parent of'),
]
LABELS = dict(KINDS)
# kind -> (link type name, outward phrase used when the name was changed, this ticket is the outward side)
_LINKS = {
    'relates': ('Relates', 'relates to', True),
    'blocks': ('Blocks', 'blocks', True),
    'blocked': ('Blocks', 'blocks', False),
    'duplicates': ('Duplicate', 'duplicates', True),
    'duplicated': ('Duplicate', 'duplicates', False),
}
KEY_RE = re.compile(r'^[A-Z][A-Z0-9_]*-\d+$')


def link_type(types, kind):
    """The site's link type for `kind` (by name, else by outward phrase), or None."""
    name, outward, _ = _LINKS[kind]
    for t in types:
        if (t.get('name') or '').lower() == name.lower():
            return t
    for t in types:
        if (t.get('outward') or '').lower() == outward:
            return t
    return None


def kinds(types):
    """[(id, label)] this site supports: parent and child always, links if the type exists."""
    return [(k, lbl) for k, lbl in KINDS if k not in _LINKS or link_type(types, k)]


def plan(kind, this, other, types):
    """What to send to Jira:
    ('link', {'type': {'id'}, 'inwardIssue': {'key'}, 'outwardIssue': {'key'}}) or
    ('parent', child_key, parent_key)."""
    if kind == 'child':
        return ('parent', this, other)
    if kind == 'parent':
        return ('parent', other, this)
    t = link_type(types, kind)
    if not t:
        raise ValueError(f'This Jira site has no "{LABELS[kind]}" link type')
    this_outward_side = _LINKS[kind][2]
    first, second = (this, other) if this_outward_side else (other, this)
    return ('link', {'type': {'id': str(t['id'])}, 'inwardIssue': {'key': first}, 'outwardIssue': {'key': second}})


def find_link(fields, type_id, other):
    """(link id, 'outward' | 'inward') of this ticket's link of type `type_id` to `other`, or (None, None).
    'outward' means the ticket shows it with the outward phrase ("blocks OTHER")."""
    for link in (fields or {}).get('issuelinks') or []:
        if str((link.get('type') or {}).get('id')) != str(type_id):
            continue
        if (link.get('outwardIssue') or {}).get('key') == other:
            return link.get('id'), 'outward'
        if (link.get('inwardIssue') or {}).get('key') == other:
            return link.get('id'), 'inward'
    return None, None


def expected_side(kind):
    """How this ticket should show the new link: 'outward' ("blocks X") or 'inward' ("is blocked by X")."""
    return 'outward' if _LINKS[kind][2] else 'inward'


def symmetric(t):
    """Types whose two phrases are the same ("relates to"): direction does not matter."""
    return (t.get('inward') or '').lower() == (t.get('outward') or '').lower()


def swapped(body):
    return {**body, 'inwardIssue': body['outwardIssue'], 'outwardIssue': body['inwardIssue']}


def recreate(link, this):
    """The request that makes `link` (as shown on ticket `this`) again: for undoing a removal."""
    t = link.get('type') or {}
    if link.get('outwardIssue'):
        first, second = this, link['outwardIssue']['key']
    else:
        first, second = link['inwardIssue']['key'], this
    return {'type': {'id': str(t.get('id'))}, 'inwardIssue': {'key': first}, 'outwardIssue': {'key': second}}


def search_jql(text):
    """JQL for the ticket search when linking: a key finds that ticket, words search text.
    None if nothing searchable is left."""
    text = (text or '').strip()
    if KEY_RE.match(text.upper()):
        return f'key = "{text.upper()}"'
    words = re.sub(r'[+\-&|!(){}\[\]^"~*?:\\/]', ' ', text).split()
    if not words:
        return None
    return f'text ~ "{" ".join(words)}*" ORDER BY lastViewed DESC, updated DESC'
