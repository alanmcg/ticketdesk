# SPDX-License-Identifier: GPL-2.0-or-later
"""Ticket creation logic independent of the UI: type order, defaults, which fields go
where in the form, and the request body."""

from . import fields as F

PREFERRED_TYPES = ('task', 'story', 'epic', 'bug')
# Fields with a place of their own in the form
MAIN_FIELDS = {'project', 'issuetype', 'summary', 'description', 'reporter', 'assignee', 'priority'}
# Fields that cannot be filled in on the form (they need uploads or other tickets)
NEVER = {'attachment', 'issuelinks', 'worklog', 'timetracking', 'comment', 'watches'}


def order_types(types):
    """Task, Story, Epic, Bug first (when the project has them), then the rest by name."""
    def key(t):
        name = (t.get('name') or '').lower()
        return (PREFERRED_TYPES.index(name) if name in PREFERRED_TYPES else len(PREFERRED_TYPES), name)
    return sorted(types, key=key)


def default_type(types):
    ordered = order_types(types)
    return ordered[0] if ordered else None


def field_id(meta):
    return meta.get('fieldId') or meta.get('key')


def by_id(field_list):
    return {field_id(m): m for m in field_list or [] if field_id(m)}


def default_option(meta, preferred='medium'):
    """The option to preselect: one named like `preferred`, else Jira's default value."""
    values = [v for v in meta.get('allowedValues') or [] if not v.get('disabled')]
    for v in values:
        if (v.get('name') or v.get('value') or '').lower() == preferred:
            return str(v['id'])
    d = meta.get('defaultValue')
    if isinstance(d, dict) and d.get('id') is not None:
        return str(d['id'])
    return None


def split(field_list, pinned):
    """Sort the create screen's fields (other than those with their own place) into
    (shown, more, browser_only):
    - shown: required by Jira, or pinned for this project (always visible, must be filled)
    - more: optional fields the form can fill in (under "More Fields")
    - browser_only: required fields the form cannot fill in."""
    shown, more, browser_only = [], [], []
    pinned = list(pinned or [])
    for meta in field_list or []:
        fid = field_id(meta)
        if not fid or fid in MAIN_FIELDS:
            continue
        supported = F.is_supported(meta) and fid not in NEVER
        if (meta.get('required') and not meta.get('hasDefaultValue')) or fid in pinned:
            if supported:
                shown.append(meta)
            elif meta.get('required') and not meta.get('hasDefaultValue'):
                browser_only.append(meta)
        elif supported:
            more.append(meta)
    order = {f: i for i, f in enumerate(pinned)}
    shown.sort(key=lambda m: (field_id(m) not in order, order.get(field_id(m), 0), (m.get('name') or '').lower()))
    more.sort(key=lambda m: (m.get('name') or field_id(m)).lower())
    return shown, more, browser_only


def payload(project, type_id, summary, description=None, reporter=None, assignee=None, priority=None,
            other=None, available=None):
    """Request body fields for POST /issue. Fields not on the create screen (`available`,
    when given) are left out, so Jira's own defaults apply instead of an error."""
    out = {'project': {'key': project}, 'issuetype': {'id': str(type_id)}, 'summary': summary.strip()}

    def allowed(fid):
        return available is None or fid in available
    if description and allowed('description'):
        out['description'] = description
    if reporter and allowed('reporter'):
        out['reporter'] = {'accountId': reporter}
    if assignee and allowed('assignee'):
        out['assignee'] = {'accountId': assignee}
    if priority and allowed('priority'):
        out['priority'] = {'id': str(priority)}
    for fid, value in (other or {}).items():
        if value is not None and allowed(fid):
            out[fid] = value
    return out


def error_text(error, names):
    """Jira's create errors with field names instead of ids."""
    parts = list(getattr(error, 'messages', []) or [])
    for fid, msg in (getattr(error, 'field_errors', {}) or {}).items():
        parts.append(f'{names.get(fid, fid)}: {msg}')
    return '\n'.join(parts) or str(error)
