# SPDX-License-Identifier: GPL-2.0-or-later
"""Field values: display text, form values for transitions/edits, and matching
workflow-rule errors ("please fill in Cost center") to editable fields."""

import re

from . import adf

SKIP_IN_DETAILS = {
    'summary', 'description', 'comment', 'attachment', 'worklog', 'issuelinks', 'subtasks', 'watches',
    'status', 'statuscategorychangedate', 'lastViewed', 'votes', 'thumbnail', 'project', 'timetracking',
    'aggregateprogress', 'progress', 'aggregatetimespent', 'aggregatetimeestimate', 'aggregatetimeoriginalestimate',
    'workratio', 'issuerestriction', 'creator', 'environment', 'security', 'parent',
}
MAX_DETAIL_CHARS = 280
FIRST = ['assignee', 'reporter', 'priority', 'issuetype', 'labels', 'components', 'fixVersions',
         'versions', 'duedate', 'resolution', 'created', 'updated']
NOT_SETTABLE_HERE = {'comment', 'description', 'environment', 'summary', 'issuelinks', 'attachment'}


def display(value):
    """Human-readable text for any Jira field value, or '' if empty."""
    if value is None or value == '' or value == []:
        return ''
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    if isinstance(value, (int, float)):
        return f'{value:g}'
    if isinstance(value, str):
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}T[\d:.]+[+-]\d{4}', value):
            return value[:16].replace('T', ' ')
        return value
    if isinstance(value, list):
        return ', '.join(t for t in (display(v) for v in value) if t)
    if isinstance(value, dict):
        if value.get('type') == 'doc':
            from .adf import to_text
            return to_text(value).strip()
        for k in ('displayName', 'name', 'value', 'key', 'title'):
            if value.get(k):
                text = str(value[k])
                if k == 'key' and (value.get('fields') or {}).get('summary'):
                    text += f' {value["fields"]["summary"]}'
                if value.get('child'):
                    text += f' › {display(value["child"])}'
                return text
    return ''


def detail_rows(fields, names):
    """(field_id, label, text) for the Details panel: well-known fields first, then other
    non-empty fields whose values have a readable form."""
    rows, seen = [], set()
    for fid in FIRST + sorted(fields, key=lambda f: (names.get(f) or f).lower()):
        if fid in seen or fid in SKIP_IN_DETAILS:
            continue
        seen.add(fid)
        text = display(fields.get(fid))
        if not text and fid == 'assignee' and 'assignee' in fields:
            text = 'Unassigned'  # shown, so it can be assigned from Details
        if text:
            rows.append((fid, names.get(fid) or fid, text))
    return rows


def clip(text, limit=MAX_DETAIL_CHARS):
    """(shown, clipped): long values are cut at a word boundary where possible."""
    text = re.sub(r'\s+', ' ', text or '').strip()
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    space = cut.rfind(' ')
    if space > limit * 0.7:
        cut = cut[:space]
    return cut.rstrip(' ,;') + '…', True


RICH_TEXT_FIELDS = ('com.atlassian.jira.plugin.system.customfieldtypes:textarea',)


def is_rich_text(meta):
    """Multi-line text fields take ADF (not plain strings) in REST API v3."""
    schema = meta.get('schema') or {}
    return schema.get('custom') in RICH_TEXT_FIELDS or schema.get('system') in ('description', 'environment')


def is_user(meta):
    schema = meta.get('schema') or {}
    return schema.get('type') == 'user' or (schema.get('type') == 'array' and schema.get('items') == 'user')


def is_parent(meta):
    return (meta.get('schema') or {}).get('system') == 'parent' or meta.get('fieldId') == 'parent'


def is_supported(meta):
    if meta.get('allowedValues'):
        return True
    schema = meta.get('schema') or {}
    t = schema.get('type')
    return (t in ('string', 'number', 'date', 'datetime') or (t == 'array' and schema.get('items') == 'string')
            or is_user(meta) or is_parent(meta))


def option_label(v):
    return str(v.get('value') or v.get('name') or v.get('id'))


def form_value(meta, selected_ids=None, text=None):
    """Convert a form choice to the JSON Jira expects; None if nothing was chosen."""
    schema = meta.get('schema') or {}
    t = schema.get('type')
    if meta.get('allowedValues'):
        ids = [str(i) for i in (selected_ids or [])]
        if not ids:
            return None
        return [{'id': i} for i in ids] if t == 'array' else {'id': ids[0]}
    text = (text or '').strip()
    if not text:
        return None
    if is_parent(meta):
        return {'key': text.upper()}
    if is_rich_text(meta):
        return adf.text_to_adf(text)
    if t == 'number':
        try:
            return float(text.replace(',', '.'))
        except ValueError:
            return None
    if t == 'array':
        return [s.strip() for s in text.split(',') if s.strip()]
    return text


def current_ids(value):
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    return [str(v['id']) for v in values if isinstance(v, dict) and v.get('id') is not None]


def current_text(value):
    if value is None or isinstance(value, (dict, list)):
        return ', '.join(value) if isinstance(value, list) and all(isinstance(v, str) for v in value) else ''
    return str(value)


def _norm(s):
    return re.sub(r'[\W_]+', '', str(s or '').lower())


def fields_for_rule_error(edit_meta, error):
    """Editable fields a workflow-rule error is about: by field id in the error, or by
    the field's name appearing in the message. Returns [(field_id, meta)]."""
    text = ' '.join(list(error.messages) + list(error.field_errors.values()) + [str(error)])
    haystack = _norm(text)
    picked = {}
    for fid in error.field_errors:
        if fid in edit_meta and fid not in NOT_SETTABLE_HERE:
            picked[fid] = edit_meta[fid]
    for fid, meta in edit_meta.items():
        if fid in NOT_SETTABLE_HERE:
            continue
        name = _norm(re.sub(r'\([^)]*\)|\[[^\]]*\]', '', meta.get('name') or ''))
        if len(name) >= 4 and name in haystack:
            picked[fid] = meta
    return [(fid, meta) for fid, meta in picked.items() if is_supported(meta)]
