# SPDX-License-Identifier: GPL-2.0-or-later
"""Jira Cloud REST client.

Thread-safe: each thread keeps its own keep-alive HTTPS connection, so parallel
requests from the worker pool reuse connections without locking.
"""

import base64
import gzip
import http.client
import json
import re
import socket
import ssl
import threading
import time
import urllib.parse
import uuid

USER_AGENT = 'TicketDesk/0.8 (GNOME)'
_STALE = (http.client.RemoteDisconnected, http.client.CannotSendRequest,
          http.client.BadStatusLine, ConnectionResetError, BrokenPipeError)
IDLE_RECONNECT_S = 50  # servers drop idle keep-alive connections around 60 s


MEDIA_FILE_RE = re.compile(r'/file/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:/|\?|$)')


class Redirect:
    """A redirect that was not followed (request(..., follow=False))."""

    def __init__(self, location):
        self.location = location


class JiraError(Exception):
    def __init__(self, status, message, messages=None, field_errors=None):
        super().__init__(message)
        self.status = status
        self.messages = messages or []
        self.field_errors = field_errors or {}


class OfflineError(JiraError):
    """The site could not be reached at all (no network, DNS, TLS, timeout)."""

    def __init__(self, message):
        super().__init__(0, message)


class JiraClient:
    def __init__(self, site, email, token, timeout=30):
        parts = urllib.parse.urlsplit(site)
        local = parts.hostname in ('localhost', '127.0.0.1')
        if parts.scheme != 'https' and not local:
            raise ValueError('The site URL must start with https://')
        self.site = f'{parts.scheme}://{parts.netloc}'
        self._https = parts.scheme == 'https'
        self._host = parts.hostname
        self._port = parts.port
        self._timeout = timeout
        creds = f'{email.strip()}:{token}'.encode()
        self._auth = 'Basic ' + base64.b64encode(creds).decode()
        self._local = threading.local()
        self._ssl = ssl.create_default_context()

    # ---------- transport ----------

    def _connection(self):
        conn = getattr(self._local, 'conn', None)
        if conn is not None and time.monotonic() - getattr(self._local, 'used', 0) > IDLE_RECONNECT_S:
            self._drop()
            conn = None
        if conn is None:
            if self._https:
                conn = http.client.HTTPSConnection(self._host, self._port, timeout=self._timeout, context=self._ssl)
            else:
                conn = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
            self._local.conn = conn
        return conn

    def _drop(self):
        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            conn.close()
        self._local.conn = None

    def request(self, method, path, query=None, body=None, data=None, content_type=None,
                headers=None, raw=False, follow=True):
        url = path
        if query:
            url += '?' + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None}, doseq=True)
        hdrs = {'Authorization': self._auth, 'Accept': 'application/json',
                'Accept-Encoding': 'gzip', 'User-Agent': USER_AGENT}
        if body is not None:
            data = json.dumps(body).encode()
            hdrs['Content-Type'] = 'application/json'
        elif content_type:
            hdrs['Content-Type'] = content_type
        hdrs.update(headers or {})
        # Only requests that are safe to repeat are retried. A POST (comment, transition)
        # is never sent twice: a duplicate comment is worse than an error message.
        idempotent = method in ('GET', 'HEAD', 'PUT', 'DELETE')

        for attempt in range(3):
            conn = self._connection()
            try:
                conn.request(method, url, body=data, headers=hdrs)
                resp = conn.getresponse()
                payload = resp.read()
                self._local.used = time.monotonic()
            except _STALE as e:
                self._drop()
                if idempotent and attempt == 0:
                    continue
                raise OfflineError(f'Connection to {self._host} was interrupted ({e.__class__.__name__})') from e
            except (OSError, ssl.SSLError, socket.timeout) as e:
                self._drop()
                raise OfflineError(f'Cannot reach {self._host}: {e}') from e

            if (resp.getheader('Connection') or '').lower() == 'close':
                self._drop()
            if resp.getheader('Content-Encoding') == 'gzip' and payload:
                payload = gzip.decompress(payload)
            if resp.status == 429 and idempotent and attempt < 2:
                time.sleep(min(float(resp.getheader('Retry-After') or 2), 10))
                continue
            if 300 <= resp.status < 400 and raw and resp.getheader('Location'):
                if not follow:
                    return Redirect(resp.getheader('Location'))
                return self._follow(resp.getheader('Location'))
            if not 200 <= resp.status < 300:
                raise self._error(resp.status, payload)
            if raw:
                return payload, resp.getheader('Content-Type') or 'application/octet-stream'
            return json.loads(payload) if payload else None
        raise JiraError(429, 'Jira is rate limiting requests; try again shortly')

    def _follow(self, location, hops=5):
        """Attachment downloads redirect to Atlassian's media service with a signed URL.
        Follow it without our credentials, which must never go to another host."""
        for _ in range(hops):
            parts = urllib.parse.urlsplit(urllib.parse.urljoin(self.site + '/', location))
            if parts.hostname == self._host:
                path = parts.path + (f'?{parts.query}' if parts.query else '')
                return self.request('GET', path, raw=True)
            cls = http.client.HTTPSConnection if parts.scheme == 'https' else http.client.HTTPConnection
            kwargs = {'context': self._ssl} if parts.scheme == 'https' else {}
            conn = cls(parts.hostname, parts.port, timeout=self._timeout, **kwargs)
            try:
                conn.request('GET', parts.path + (f'?{parts.query}' if parts.query else ''),
                             headers={'User-Agent': USER_AGENT})
                resp = conn.getresponse()
                payload = resp.read()
            except (OSError, ssl.SSLError, http.client.HTTPException) as e:
                raise OfflineError(f'Cannot download attachment: {e}') from e
            finally:
                conn.close()
            if 300 <= resp.status < 400 and resp.getheader('Location'):
                location = resp.getheader('Location')
                continue
            if not 200 <= resp.status < 300:
                raise JiraError(resp.status, f'Attachment download failed ({resp.status})')
            return payload, resp.getheader('Content-Type') or 'application/octet-stream'
        raise JiraError(310, 'Too many redirects while downloading')

    @staticmethod
    def _error(status, payload):
        messages, field_errors, text = [], {}, payload.decode('utf-8', 'replace')[:500]
        try:
            j = json.loads(payload)
            messages = list(j.get('errorMessages') or [])
            field_errors = dict(j.get('errors') or {})
        except (ValueError, AttributeError):
            pass
        detail = '; '.join(messages + list(field_errors.values())) or text or f'HTTP {status}'
        if status == 401:
            detail = 'Authentication failed: check the account email and API token'
        elif status == 403 and not messages:
            detail = 'You do not have permission to do this in Jira'
        elif status == 404 and not messages:
            detail = 'Not found in Jira (deleted, moved, or no access)'
        return JiraError(status, detail, messages, field_errors)

    # ---------- API ----------

    def myself(self):
        return self.request('GET', '/rest/api/3/myself')

    def search(self, jql, fields, limit=100):
        """Enhanced JQL search with nextPageToken paging."""
        out, token = [], None
        while len(out) < limit:
            body = {'jql': jql, 'fields': fields, 'maxResults': min(100, limit - len(out))}
            if token:
                body['nextPageToken'] = token
            res = self.request('POST', '/rest/api/3/search/jql', body=body) or {}
            out.extend(res.get('issues') or [])
            token = res.get('nextPageToken')
            if not token:
                break
        return out

    def issue(self, key, fields='*all', expand='names'):
        return self.request('GET', f'/rest/api/3/issue/{urllib.parse.quote(key)}',
                            query={'fields': fields, 'expand': expand})

    def rendered_description(self, key):
        """Jira's HTML rendering of the description (used to locate embedded attachments)."""
        res = self.request('GET', f'/rest/api/3/issue/{urllib.parse.quote(key)}',
                           query={'fields': 'description', 'expand': 'renderedFields'}) or {}
        return (res.get('renderedFields') or {}).get('description') or ''

    def comments(self, key, limit=500, rendered=False):
        out, start = [], 0
        while len(out) < limit:
            res = self.request('GET', f'/rest/api/3/issue/{urllib.parse.quote(key)}/comment',
                               query={'startAt': start, 'maxResults': 100, 'orderBy': 'created',
                                      'expand': 'renderedBody' if rendered else None}) or {}
            page = res.get('comments') or []
            out.extend(page)
            start += len(page)
            if not page or start >= res.get('total', 0):
                break
        return out

    def add_comment(self, key, adf_doc):
        return self.request('POST', f'/rest/api/3/issue/{urllib.parse.quote(key)}/comment',
                            body={'body': adf_doc})

    def transitions(self, key):
        res = self.request('GET', f'/rest/api/3/issue/{urllib.parse.quote(key)}/transitions',
                           query={'expand': 'transitions.fields'}) or {}
        return res.get('transitions') or []

    def do_transition(self, key, transition_id, fields=None):
        body = {'transition': {'id': str(transition_id)}}
        if fields:
            body['fields'] = fields
        return self.request('POST', f'/rest/api/3/issue/{urllib.parse.quote(key)}/transitions', body=body)

    def issue_fields(self, key, fields):
        return (self.request('GET', f'/rest/api/3/issue/{urllib.parse.quote(key)}',
                             query={'fields': ','.join(fields)}) or {}).get('fields') or {}

    def edit_meta(self, key):
        res = self.request('GET', f'/rest/api/3/issue/{urllib.parse.quote(key)}/editmeta') or {}
        return res.get('fields') or {}

    def edit_issue(self, key, fields):
        return self.request('PUT', f'/rest/api/3/issue/{urllib.parse.quote(key)}', body={'fields': fields})

    def assign(self, key, account_id):
        """Assign a ticket; None unassigns. Needs the Assign Issues permission (not Edit Issues)."""
        return self.request('PUT', f'/rest/api/3/issue/{urllib.parse.quote(key)}/assignee',
                            body={'accountId': account_id})

    def link_types(self):
        return (self.request('GET', '/rest/api/3/issueLinkType') or {}).get('issueLinkTypes') or []

    def create_link(self, body):
        """Link two tickets (needs Link Issues permission). Never retried (POST)."""
        return self.request('POST', '/rest/api/3/issueLink', body=body)

    def delete_link(self, link_id):
        return self.request('DELETE', f'/rest/api/3/issueLink/{urllib.parse.quote(str(link_id))}')

    def watch(self, key, account_id, on):
        path = f'/rest/api/3/issue/{urllib.parse.quote(key)}/watchers'
        if on:
            return self.request('POST', path, body=account_id)
        return self.request('DELETE', path, query={'accountId': account_id})

    def projects(self, keys):
        """Name and key of the given projects (unknown keys are left out by Jira)."""
        res = self.request('GET', '/rest/api/3/project/search', query={'keys': list(keys), 'maxResults': 100}) or {}
        return res.get('values') or []

    def all_projects(self, limit=100):
        """Projects you can see, most recently active first."""
        res = self.request('GET', '/rest/api/3/project/search',
                           query={'orderBy': '-lastIssueUpdatedTime', 'maxResults': limit}) or {}
        return res.get('values') or []

    # ---------- creating tickets ----------

    def _paged_values(self, path, page=50, limit=1000):
        out = []
        while len(out) < limit:
            res = self.request('GET', path, query={'startAt': len(out), 'maxResults': page}) or {}
            values = res.get('values') or res.get('issueTypes') or res.get('fields') or res.get('results') or []
            out.extend(values)
            total = res.get('total')
            if not values or (total is not None and len(out) >= total) or res.get('isLast'):
                break
        return out

    def create_types(self, project):
        """Ticket types that can be created in a project."""
        return self._paged_values(f'/rest/api/3/issue/createmeta/{urllib.parse.quote(project)}/issuetypes')

    def create_fields(self, project, type_id):
        """Fields on the project's create screen for a ticket type: [{fieldId, name, required,
        schema, allowedValues, hasDefaultValue, defaultValue, ...}]."""
        return self._paged_values(f'/rest/api/3/issue/createmeta/{urllib.parse.quote(project)}/issuetypes/'
                                  f'{urllib.parse.quote(str(type_id))}')

    def create_issue(self, fields):
        """Create a ticket; returns {'id', 'key'}. Never retried (POST)."""
        return self.request('POST', '/rest/api/3/issue', body={'fields': fields}) or {}

    def find_users(self, query, project=None, assignable=False, limit=20, issue_key=None):
        """Active people matching `query`; with `assignable`, only those who can be assigned
        tickets of the project, or the ticket `issue_key` (that also counts its workflow state)."""
        if assignable and (project or issue_key):
            res = self.request('GET', '/rest/api/3/user/assignable/search',
                               query={'issueKey': issue_key, 'project': None if issue_key else project,
                                      'query': query, 'maxResults': limit})
        else:
            res = self.request('GET', '/rest/api/3/user/search', query={'query': query, 'maxResults': limit})
        return [u for u in res or [] if u.get('accountType', 'atlassian') == 'atlassian' and u.get('active', True)]

    def recent_changelog(self, key, page=100):
        """The latest changes of a ticket (the changelog API pages oldest first)."""
        path = f'/rest/api/3/issue/{urllib.parse.quote(key)}/changelog'
        res = self.request('GET', path, query={'maxResults': page}) or {}
        total = res.get('total') or 0
        if not res.get('isLast', True) and total > page:
            res = self.request('GET', path, query={'startAt': total - page, 'maxResults': page}) or {}
        return res.get('values') or []

    def users_for_mention(self, query, issue_key=None, project=None, limit=10):
        """People who can see the ticket (or the project, for a new ticket) matching `query`."""
        if issue_key or project:
            res = self.request('GET', '/rest/api/3/user/viewissue/search',
                               query={'query': query, 'issueKey': issue_key, 'projectKey': None if issue_key else project,
                                      'maxResults': limit})
        else:
            res = self.request('GET', '/rest/api/3/user/search', query={'query': query, 'maxResults': limit})
        return [u for u in res or [] if u.get('accountType', 'atlassian') == 'atlassian' and u.get('active', True)]

    def favourite_filters(self):
        return self.request('GET', '/rest/api/3/filter/favourite') or []

    def attachment_content(self, attachment_id):
        return self.request('GET', f'/rest/api/3/attachment/content/{attachment_id}', raw=True)

    def attachment_thumbnail(self, attachment_id):
        return self.request('GET', f'/rest/api/3/attachment/thumbnail/{attachment_id}', raw=True)

    def upload_attachments(self, key, files):
        """Attach files to a ticket. files: [(name, bytes, mime)]. Returns Jira's attachment
        records in the same order. A POST: never retried."""
        boundary = '----TicketDesk' + uuid.uuid4().hex
        parts = []
        for name, data, mime in files:
            safe = name.replace('"', "'").replace('\r', ' ').replace('\n', ' ')
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{safe}"\r\n'
                         f'Content-Type: {mime or "application/octet-stream"}\r\n\r\n'.encode() + data + b'\r\n')
        body = b''.join(parts) + f'--{boundary}--\r\n'.encode()
        return self.request('POST', f'/rest/api/3/issue/{urllib.parse.quote(key)}/attachments', data=body,
                            content_type=f'multipart/form-data; boundary={boundary}',
                            headers={'X-Atlassian-Token': 'no-check'}) or []

    def media_file_id(self, attachment_id):
        """The media-service id of an attachment, which rich text needs to show it inline.
        Jira has no API for it; it is the file id in the address the download redirects to.
        None if that cannot be found (then the file stays an ordinary attachment)."""
        try:
            res = self.request('GET', f'/rest/api/3/attachment/content/{int(attachment_id)}', raw=True, follow=False)
        except JiraError:
            return None
        if isinstance(res, Redirect):
            m = MEDIA_FILE_RE.search(res.location)
            return m.group(1) if m else None
        return None

    def external_image(self, url):
        """An image embedded by URL. Downloaded without credentials unless it is on the Jira site."""
        if urllib.parse.urlsplit(url).scheme != 'https':
            raise JiraError(0, 'Only https images are loaded')
        return self._follow(url)

    # ---------- boards (Jira Software agile API) ----------

    def boards(self, project=None, limit=200):
        out, start = [], 0
        while len(out) < limit:
            res = self.request('GET', '/rest/agile/1.0/board',
                               query={'projectKeyOrId': project, 'startAt': start, 'maxResults': 50}) or {}
            page = res.get('values') or []
            out.extend(page)
            start += len(page)
            if not page or res.get('isLast', True):
                break
        return out

    def board_config(self, board_id):
        return self.request('GET', f'/rest/agile/1.0/board/{int(board_id)}/configuration') or {}

    def active_sprints(self, board_id):
        res = self.request('GET', f'/rest/agile/1.0/board/{int(board_id)}/sprint',
                           query={'state': 'active', 'maxResults': 50}) or {}
        return res.get('values') or []

    def board_issues(self, board_id, fields, limit=500, jql=None, sprint_id=None):
        """Issues on a board (or one of its sprints) in board rank order. Returns (issues, total)."""
        path = (f'/rest/agile/1.0/board/{int(board_id)}/sprint/{int(sprint_id)}/issue' if sprint_id
                else f'/rest/agile/1.0/board/{int(board_id)}/issue')
        out, total = [], None
        while len(out) < limit:
            res = self.request('GET', path, query={'jql': jql, 'fields': ','.join(fields), 'startAt': len(out),
                                                   'maxResults': min(100, limit - len(out))}) or {}
            page = res.get('issues') or []
            out.extend(page)
            total = res.get('total', total)
            if not page or (total is not None and len(out) >= total):
                break
        return out[:limit], (total if total is not None else len(out))

    def rank(self, keys, before=None, after=None, field_id=None):
        body = {'issues': list(keys)}
        if before:
            body['rankBeforeIssue'] = before
        elif after:
            body['rankAfterIssue'] = after
        if field_id:
            body['rankCustomFieldId'] = int(field_id)
        res = self.request('PUT', '/rest/agile/1.0/issue/rank', body=body)
        errors = [e for entry in (res or {}).get('entries') or [] for e in entry.get('errors') or []]
        if errors:  # 207 Multi-Status: the request went through but ranking failed
            raise JiraError(207, '; '.join(errors))

    def browse_url(self, key):
        return f'{self.site}/browse/{urllib.parse.quote(key)}'

    def search_url(self, jql):
        return f'{self.site}/issues/?jql={urllib.parse.quote(jql)}'
