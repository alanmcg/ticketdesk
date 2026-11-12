# SPDX-License-Identifier: GPL-2.0-or-later
"""A ticket's place among other tickets: parent, children and links."""

CATEGORY = {'new': 'todo', 'indeterminate': 'progress', 'done': 'done'}
CATEGORY_ORDER = {'progress': 0, 'todo': 1, 'done': 2}


def brief(issue):
    """Compact form of an issue or of the issue stubs Jira embeds in fields."""
    f = (issue or {}).get('fields') or {}
    status = f.get('status') or {}
    return {'key': issue.get('key', ''), 'summary': f.get('summary') or '',
            'status': status.get('name') or '',
            'category': CATEGORY.get((status.get('statusCategory') or {}).get('key'), 'todo'),
            'type': (f.get('issuetype') or {}).get('name') or ''}


def level(fields):
    """Jira's hierarchy level: -1 sub-task, 0 standard, 1 epic, 2+ above epics."""
    try:
        return int(((fields or {}).get('issuetype') or {}).get('hierarchyLevel', 0))
    except (TypeError, ValueError):
        return 0


def has_child_query(fields):
    """Epics and above find their children with a query; standard tickets list their
    sub-tasks in the `subtasks` field; sub-tasks have no children."""
    return level(fields) >= 1


def parent(fields):
    p = (fields or {}).get('parent')
    return brief(p) if p and p.get('key') else None


def subtasks(fields):
    return [brief(s) for s in (fields or {}).get('subtasks') or [] if s.get('key')]


def links(fields):
    """[(phrase, [brief, ...])] e.g. ('blocks', [...]), ('is blocked by', [...]), in Jira's order."""
    groups = {}
    for link in (fields or {}).get('issuelinks') or []:
        t = link.get('type') or {}
        if link.get('outwardIssue'):
            phrase, other = t.get('outward') or t.get('name') or 'relates to', link['outwardIssue']
        elif link.get('inwardIssue'):
            phrase, other = t.get('inward') or t.get('name') or 'relates to', link['inwardIssue']
        else:
            continue
        b = brief(other)
        b['link'] = link  # for removing it
        groups.setdefault(phrase, []).append(b)
    return list(groups.items())


def summarize(children):
    """{'todo': n, 'progress': n, 'done': n}"""
    out = {'todo': 0, 'progress': 0, 'done': 0}
    for c in children:
        out[c['category']] = out.get(c['category'], 0) + 1
    return out


def ordered(children):
    """Work in progress first, then to do, then done; Jira's order within each group."""
    return sorted(children, key=lambda c: CATEGORY_ORDER.get(c['category'], 1))
