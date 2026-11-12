# SPDX-License-Identifier: GPL-2.0-or-later
"""Notifications from Jira activity, found by polling (Jira Cloud has no public API for
its own notification feed). The same rules as the top bar extension:

- mentions of you: in comments, in the description of a new ticket, or added to a
  description later;
- on tickets you are involved in (assignee, reporter, creator or watcher): new comments
  and status changes;
- a ticket assigned to you.
Your own actions never notify you.
"""

import datetime

from . import adf

OVERLAP_MS = 60 * 1000          # re-check a minute back; duplicates are filtered by event id
CATCH_UP_MS = 7 * 86400 * 1000  # after a long break, look back at most a week
ISSUE_CAP = 100                 # updated tickets inspected per check


def jira_ms(value):
    """Jira timestamp (2026-11-12T10:15:30.000+0100) to epoch milliseconds; 0 if unknown."""
    if not value:
        return 0
    try:
        return int(datetime.datetime.strptime(value, '%Y-%m-%dT%H:%M:%S.%f%z').timestamp() * 1000)
    except ValueError:
        try:
            return int(datetime.datetime.fromisoformat(value).timestamp() * 1000)
        except ValueError:
            return 0


def _snippet(text, n):
    text = ' '.join((text or '').split())
    return text if len(text) <= n else text[:n - 1].rstrip() + '…'


def involved(fields, me):
    f = fields or {}
    people = [f.get('assignee'), f.get('reporter'), f.get('creator')]
    return any((p or {}).get('accountId') == me for p in people) or bool((f.get('watches') or {}).get('isWatching'))


def events_for(issue, comments, changelog, me, since, mentions=True, updates=True):
    """Events on one ticket since `since` (epoch ms): [{'id', 'key', 'kind', 'title', 'body',
    'author', 'time'}]. kind: mention, comment, status, assigned."""
    f = issue.get('fields') or {}
    key = issue['key']
    summary = _snippet(f.get('summary'), 90)
    mine = involved(f, me)
    out = []

    def add(eid, kind, title, body, author, when):
        out.append({'id': eid, 'key': key, 'kind': kind, 'title': title, 'body': body,
                    'author': author, 'time': when})

    creator = (f.get('creator') or {})
    created = jira_ms(f.get('created'))
    if created >= since and creator.get('accountId') != me:
        who = creator.get('displayName') or 'Someone'
        if mentions and adf.mentions(f.get('description'), me):
            add(f'new:{key}', 'mention', f'{who} mentioned you in new {key}', summary, who, created)
        elif updates and (f.get('assignee') or {}).get('accountId') == me:
            add(f'na:{key}', 'assigned', f'{who} assigned new {key} to you', summary, who, created)

    for c in comments or []:
        author = c.get('author') or {}
        if author.get('accountId') == me:
            continue
        who = author.get('displayName') or 'Someone'
        when = max(jira_ms(c.get('created')), jira_ms(c.get('updated')))
        if when < since:
            continue
        text = _snippet(adf.to_text(c.get('body')), 200)
        if mentions and adf.mentions(c.get('body'), me):
            add(f'cm:{c.get("id")}', 'mention', f'{who} mentioned you on {key}', f'{summary}\n{text}', who, when)
        elif updates and mine and jira_ms(c.get('created')) >= since:
            add(f'c:{c.get("id")}', 'comment', f'{who} commented on {key}', f'{summary}\n{text}', who,
                jira_ms(c.get('created')))

    for h in changelog or []:
        author = h.get('author') or {}
        when = jira_ms(h.get('created'))
        if when < since or author.get('accountId') == me:
            continue
        who = author.get('displayName') or 'Someone'
        for item in h.get('items') or []:
            field = item.get('field') or item.get('fieldId')
            if field == 'status' and updates and mine:
                add(f'hs:{h.get("id")}', 'status', f'{key}: {item.get("fromString") or "?"} → {item.get("toString") or "?"}',
                    f'{who} · {summary}', who, when)
            elif field == 'assignee' and updates and item.get('to') == me:
                add(f'ha:{h.get("id")}', 'assigned', f'{who} assigned {key} to you', summary, who, when)
            elif field == 'description' and mentions and me in (item.get('toString') or '') \
                    and me not in (item.get('fromString') or ''):
                add(f'hd:{h.get("id")}', 'mention', f'{who} mentioned you in the description of {key}', summary,
                    who, when)
    return out


def queries(mentions, updates, projects, since):
    """JQL for tickets to inspect. Unquoted numbers in date clauses are epoch milliseconds."""
    out = []
    if updates:
        out.append('(assignee = currentUser() OR reporter = currentUser() OR creator = currentUser() '
                   f'OR watcher = currentUser()) AND updated >= {int(since)} ORDER BY updated DESC')
    if mentions:
        scope = f'project in ({", ".join(chr(34) + p + chr(34) for p in projects)}) AND ' if projects else ''
        out.append(f'{scope}updated >= {int(since)} ORDER BY updated DESC')
    return out
