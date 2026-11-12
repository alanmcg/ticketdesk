# SPDX-License-Identifier: GPL-2.0-or-later
"""JQL building for the ticket lists. Same search syntax as the top bar extension."""

import datetime
import re

LIST_FIELDS = ['summary', 'status', 'priority', 'issuetype', 'assignee', 'reporter', 'creator',
               'created', 'updated', 'watches', 'attachment', 'parent']
SORTS = ('created', 'updated', 'priority', 'status', 'key')
SORT_LABELS = {'created': ('Created', 'newest first', 'oldest first'),
               'updated': ('Updated', 'newest first', 'oldest first'),
               'priority': ('Priority', 'highest first', 'lowest first'),
               'status': ('Status', 'descending', 'ascending'),
               'key': ('Key', 'descending', 'ascending')}
RANGE_MAX = 200  # with a created-date range everything in it is listed, up to this many
CATEGORIES = (('todo', 'To Do'), ('progress', 'In Progress'), ('done', 'Done'))

_KEY_PREFIX = re.compile(r'^([A-Za-z][A-Za-z0-9_]*)-(\d+)$')
_PROJECT = re.compile(r'^[A-Za-z][A-Za-z0-9_]*-$')
_TEXT_OPS = re.compile(r'[+\-&|!(){}\[\]^~*?\\:"]')


def jql_string(s):
    return '"' + str(s).replace('\\', '\\\\').replace('"', '\\"') + '"'


def project_scope(projects):
    return f'project in ({", ".join(jql_string(p) for p in projects)})' if projects else ''


def relation_clause(mentioned_keys=()):
    parts = ['assignee = currentUser()', 'reporter = currentUser()',
             'creator = currentUser()', 'watcher = currentUser()']
    keys = [k for k in mentioned_keys if re.fullmatch(r'[A-Z][A-Z0-9_]*-\d+', k)]
    if keys:
        parts.append(f'key in ({", ".join(keys)})')
    return '(' + ' OR '.join(parts) + ')'


def status_clause(show):
    names = [label for cat, label in CATEGORIES if show.get(cat)]
    if not names or len(names) == len(CATEGORIES):
        return ''
    return f'statusCategory in ({", ".join(jql_string(n) for n in names)})'


def order_by(sort, desc):
    sort = sort if sort in SORTS else 'created'
    direction = 'DESC' if desc else 'ASC'
    return f'ORDER BY {sort} {direction}' + ('' if sort == 'created' else ', created DESC')


def week_range(offset, today=None):
    """(monday, end) of this week (offset 0, ending today) or an earlier week (offset -1, …)."""
    today = today or datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday()) + datetime.timedelta(weeks=offset)
    return monday, today if offset == 0 else monday + datetime.timedelta(days=6)


def created_clause(created):
    """created = (from_date, to_date), both inclusive, or None."""
    if not created:
        return ''
    start, end = created
    end_excl = end + datetime.timedelta(days=1)
    return f'created >= "{start.isoformat()}" AND created < "{end_excl.isoformat()}"'


def build(view, text, projects, show, sort, desc, mentioned_keys=(), filter_jql=None, project=None, created=None):
    """Return (jql, title, fallback) for a list view.

    view: 'mine' (my tickets in the configured projects), 'all' (every ticket in the
    configured projects), 'project' (every ticket in `project`), 'filter' (filter_jql: a
    saved filter or another query). The search text narrows the view:
      ABC- / ABC- XYZ-  -> all tickets in those projects
      ABC-12            -> keys starting with ABC-12
      other text        -> text search within the view
    fallback: (jql, key_prefix) used if Jira rejects the key wildcard.
    """
    text = (text or '').strip()
    scope = project_scope(projects)
    title = None
    if view == 'filter' and filter_jql:
        # Saved filters may carry their own ORDER BY; drop it so ours applies.
        base = re.sub(r'(^|\s+)ORDER\s+BY\s+.*$', '', filter_jql, flags=re.I | re.S).strip()
    elif view == 'project' and project:
        base = f'project = {jql_string(project)}'
    elif view == 'all':
        base = scope
    else:
        base = ' AND '.join(x for x in (scope, relation_clause(mentioned_keys)) if x)
        title = None
    fallback = None

    tokens = [t for t in re.split(r'[\s,]+', text) if t]
    if tokens and all(_PROJECT.match(t) for t in tokens):
        projs = list(dict.fromkeys(t[:-1].upper() for t in tokens))
        base = project_scope(projs)
        title = f'All tickets in {", ".join(projs)}'
    elif _KEY_PREFIX.match(text):
        m = _KEY_PREFIX.match(text)
        project, prefix = m.group(1).upper(), f'{m.group(1).upper()}-{m.group(2)}'
        base = f'project = {jql_string(project)} AND key ~ {jql_string(prefix + "*")}'
        fallback = (f'project = {jql_string(project)}', prefix)
        title = f'Keys {prefix}*'
    elif text:
        words = _TEXT_OPS.sub(' ', text).strip()
        if words:
            base = ' AND '.join(x for x in (base, f'text ~ {jql_string(words + "*")}') if x)
            title = f'Matching “{words}”'

    clauses = [c for c in (f'({base})' if base else '', status_clause(show), created_clause(created)) if c]
    if not clauses:
        clauses = ['created >= -90d']  # enhanced search rejects unbounded queries
    jql = f'{" AND ".join(clauses)} {order_by(sort, desc)}'
    if fallback:
        fallback = (f'{fallback[0]} {order_by("created", True)}', fallback[1])
    return jql, title, fallback


def missing_key(message):
    """Key named in Jira's 'An issue with key X does not exist' error, if any."""
    m = re.search(r"'([A-Z][A-Z0-9_]*-\d+)'", message or '')
    return m.group(1) if m else None
