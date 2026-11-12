# SPDX-License-Identifier: GPL-2.0-or-later
"""Which web links are Jira tickets that Ticket Desk can open.
browser/firefox/links.js is the same logic for the browser extension; both are tested
against tests/link_cases.json."""

import re
import urllib.parse

from .config import normalize_site

KEY = r'[A-Z][A-Z0-9_]*-\d+'
KEY_RE = re.compile(rf'^{KEY}$')
# Paths that show one ticket: /browse/KEY, …/issues/KEY, service desk queues …/queues/…/KEY
_PATH_RES = [re.compile(rf'^/browse/({KEY})(?:/|$)'),
             re.compile(rf'^/jira/(?:[^/]+/)*issues/({KEY})/?$'),
             re.compile(rf'^/jira/servicedesk/projects/[^/]+/queues/(?:[^/]+/)*({KEY})/?$')]
BYPASS = 'ticketdesk'  # ?ticketdesk=web: leave the link to the browser


def host_of(site):
    try:
        return (urllib.parse.urlsplit(normalize_site(site)).hostname or '').lower()
    except ValueError:
        return ''


def unwrap(url):
    """The real target of a Microsoft Safe Links URL (Outlook, Teams), else `url`."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or '').lower()
    except ValueError:
        return url
    wrapped = host.endswith('.safelinks.protection.outlook.com') or \
        (host == 'statics.teams.cdn.office.net' and '/safelinks/' in parts.path)
    if wrapped:
        target = (urllib.parse.parse_qs(parts.query).get('url') or [''])[0]
        if target.startswith(('http://', 'https://')):
            return target
    return url


def ticket_key(url, site_host):
    """The ticket key if `url` shows a single ticket on the configured Jira site, else None.
    Pages that only highlight a ticket among others (board, backlog, issue search with
    `selectedIssue`) count as that ticket: it is what the link was shared for."""
    try:
        parts = urllib.parse.urlsplit(unwrap(url or ''))
        host = (parts.hostname or '').lower()
    except ValueError:
        return None
    if parts.scheme not in ('http', 'https') or not site_host or host != site_host:
        return None
    query = urllib.parse.parse_qs(parts.query)
    if BYPASS in query:
        return None
    for rx in _PATH_RES:
        m = rx.match(parts.path)
        if m:
            return m.group(1)
    selected = (query.get('selectedIssue') or [''])[0].upper()
    if KEY_RE.match(selected):
        return selected
    return None
