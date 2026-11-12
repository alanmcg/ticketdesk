# SPDX-License-Identifier: GPL-2.0-or-later
"""Board logic independent of the UI: columns, card placement, moves and ranking."""

KANBAN_DONE_DAYS = 14


def columns(config):
    """Board columns from the board configuration: [{'name', 'statuses': set, 'min', 'max'}].
    Columns without statuses are kept (Jira shows them too) but cannot receive cards."""
    out = []
    for c in ((config or {}).get('columnConfig') or {}).get('columns') or []:
        out.append({'name': c.get('name') or '',
                    'statuses': {str(s.get('id')) for s in c.get('statuses') or [] if s.get('id') is not None},
                    'min': c.get('min'), 'max': c.get('max')})
    return out


def column_of(issue, cols):
    sid = str(((issue.get('fields') or {}).get('status') or {}).get('id'))
    for i, c in enumerate(cols):
        if sid in c['statuses']:
            return i
    return None


def group(issues, cols):
    """Cards per column, keeping Jira's (rank) order. Issues whose status is not on the
    board are counted, not placed."""
    out = [[] for _ in cols]
    unplaced = 0
    for it in issues:
        i = column_of(it, cols)
        if i is None:
            unplaced += 1
        else:
            out[i].append(it)
    return out, unplaced


def kanban_jql(config, done_days=KANBAN_DONE_DAYS):
    """Kanban boards keep finished work forever; show only recently finished cards, like Jira does."""
    parts = []
    sub = ((config or {}).get('subQuery') or {}).get('query')
    if sub:
        parts.append(f'({sub})')
    parts.append(f'(statusCategory != Done OR updated >= -{int(done_days)}d)')
    return ' AND '.join(parts)


def transitions_into(transitions, column):
    """Transitions whose target status belongs to the column."""
    return [t for t in transitions or [] if str((t.get('to') or {}).get('id')) in column['statuses']]


def drop_plan(col_keys, key, from_col, to_col, index):
    """What a drop means. `col_keys` are the target column's card keys as shown (may include
    `key` when moving within a column); `index` is the drop position among them.
    Returns (needs_transition, rank) where rank is None, ('before', k) or ('after', k)."""
    others = [k for k in col_keys if k != key]
    if from_col == to_col and key in col_keys:
        old = col_keys.index(key)
        if index > old:
            index -= 1
        if index == old:
            return False, None
    index = max(0, min(index, len(others)))
    if index < len(others):
        rank = ('before', others[index])
    elif others:
        rank = ('after', others[-1])
    else:
        rank = None
    return from_col != to_col, rank


# ---------- swimlanes ----------

LANE_MODES = (('none', 'No Swimlanes'), ('assignee', 'Assignee'), ('parent', 'Epic / Parent'),
              ('priority', 'Priority'), ('issuetype', 'Ticket Type'))
NO_LANE = '-'  # lane key for "Unassigned", "No parent", ...


def lane_of(issue, mode):
    """(lane_key, title) of an issue."""
    f = issue.get('fields') or {}
    if mode == 'assignee':
        a = f.get('assignee') or {}
        return (a.get('accountId') or NO_LANE), (a.get('displayName') or 'Unassigned')
    if mode == 'parent':
        p = f.get('parent') or {}
        if not p.get('key'):
            return NO_LANE, 'No parent'
        return p['key'], f'{p["key"]} {((p.get("fields") or {}).get("summary") or "")}'.strip()
    if mode in ('priority', 'issuetype'):
        v = f.get(mode) or {}
        return (str(v['id']) if v.get('id') is not None else NO_LANE), (v.get('name') or 'None')
    return NO_LANE, ''


def lanes(issues, mode):
    """[(lane_key, title, [issues])] in display order; issues keep their rank order.
    Assignee lanes are alphabetical, priority lanes follow Jira's priority ids (highest
    first), parent and type lanes appear in rank order; the empty lane comes last."""
    groups, titles, first = {}, {}, {}
    for i, it in enumerate(issues):
        key, title = lane_of(it, mode)
        groups.setdefault(key, []).append(it)
        titles.setdefault(key, title)
        first.setdefault(key, i)

    def order(key):
        if key == NO_LANE:
            return (1, 0, '')
        if mode == 'assignee':
            return (0, 0, titles[key].lower())
        if mode == 'priority':
            return (0, int(key) if key.isdigit() else 1 << 30, titles[key].lower())
        return (0, first[key], '')
    return [(k, titles[k], groups[k]) for k in sorted(groups, key=order)]


def lane_change(mode, lane_key):
    """Field update that puts a ticket into another lane, or None if that cannot be done
    by dragging (ticket type changes are a "move" in Jira)."""
    if mode == 'assignee':
        return {'assignee': None if lane_key == NO_LANE else {'accountId': lane_key}}
    if mode == 'priority':
        return None if lane_key == NO_LANE else {'priority': {'id': lane_key}}
    if mode == 'parent':
        # Setting a parent is documented; removing one differs between project types, so
        # that is left to the ticket itself.
        return None if lane_key == NO_LANE else {'parent': {'key': lane_key}}
    return None
