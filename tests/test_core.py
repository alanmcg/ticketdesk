# SPDX-License-Identifier: GPL-2.0-or-later
"""Core tests against a fake Jira server. Run: python3 -m unittest discover tests"""

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ticketdesk.core import adf, boards, fields, media, search
from ticketdesk.core.api import JiraClient, JiraError, OfflineError
from ticketdesk.core.cache import Cache
from ticketdesk.core.config import Config, normalize_site, parse_projects
from ticketdesk.core.store import Store
from ticketdesk.core.worker import Worker, direct_dispatch


def issue(key, summary='S', updated='2026-11-10T10:00:00.000+0100', **extra):
    return {'key': key, 'fields': {'summary': summary, 'updated': updated, **extra}}


class FakeJira(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    state = {}

    def log_message(self, *a):
        pass

    def _send(self, status, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else b''
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return json.loads(self.rfile.read(n)) if n else None

    def do_GET(self):
        s = self.state
        s.setdefault('calls', []).append(('GET', self.path))
        if self.headers.get('Authorization') != 'Basic YUBiLmM6dG9rZW4=':
            return self._send(401, {'errorMessages': ['no']})
        if self.path.startswith('/rest/api/3/myself'):
            return self._send(200, {'accountId': 'me-1', 'displayName': 'Alex'})
        if self.path.startswith('/rest/api/3/attachment/content/'):
            att = self.path.split('/')[-1].split('?')[0]
            if s.get('no_media_redirect'):
                return self._send(200, {})
            self.send_response(303)
            self.send_header('Location', f'https://api.media.atlassian.com/file/0b8a1c2d-3e4f-4a5b-8c6d-00000000{int(att):04d}'
                                         '/binary?token=secret&client=c&collection=&dl=true')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return None
        if self.path.startswith('/rest/api/3/flaky'):
            s['flaky'] = s.get('flaky', 0) + 1
            if s['flaky'] == 1:
                return self._send(429, {'errorMessages': ['slow down']}, {'Retry-After': '0'})
            return self._send(200, {'ok': True})
        if self.path.startswith('/rest/agile/1.0/board?'):
            q = self.path
            if 'projectKeyOrId=ABC' in q:
                return self._send(200, {'values': [{'id': 7, 'name': 'ABC board', 'type': 'kanban',
                                                    'location': {'projectKey': 'ABC'}}], 'isLast': True})
            if 'projectKeyOrId' not in q:
                return self._send(200, {'values': [
                    {'id': 7, 'name': 'ABC board', 'type': 'kanban', 'location': {'projectKey': 'ABC'}},
                    {'id': 9, 'name': 'Other board', 'type': 'scrum', 'location': {'projectKey': 'XYZ'}}],
                    'isLast': True})
            return self._send(200, {'values': [], 'isLast': True})
        if self.path.startswith('/rest/api/3/issue/createmeta/OPS/issuetypes/10001'):
            start = int(self.path.split('startAt=')[1].split('&')[0])
            fl = [{'fieldId': f'customfield_{i}', 'name': f'F{i}', 'required': False, 'schema': {'type': 'string'}}
                  for i in range(60)]
            return self._send(200, {'fields': fl[start:start + 50], 'startAt': start, 'maxResults': 50, 'total': 60})
        if self.path.startswith('/rest/api/3/issue/createmeta/OPS/issuetypes'):
            return self._send(200, {'issueTypes': [{'id': '10001', 'name': 'Task'}, {'id': '10002', 'name': 'Sub-task',
                                                                                     'subtask': True}],
                                    'startAt': 0, 'maxResults': 50, 'total': 2})
        if self.path.startswith('/rest/api/3/user/assignable/search'):
            return self._send(200, [{'accountId': 'u1', 'displayName': 'Ylva', 'accountType': 'atlassian'},
                                    {'accountId': 'app', 'displayName': 'Bot', 'accountType': 'app'}])
        if self.path.startswith('/rest/api/3/project/search'):
            return self._send(200, {'values': [{'key': 'ABC', 'name': 'Alpha Bravo'}]})
        if self.path.startswith('/rest/agile/1.0/board/7/configuration'):
            return self._send(200, {'columnConfig': {'columns': [
                {'name': 'To Do', 'statuses': [{'id': '1'}]}, {'name': 'Doing', 'statuses': [{'id': '3'}]},
                {'name': 'Done', 'statuses': [{'id': '10001'}]}]},
                'subQuery': {'query': 'fixVersion is EMPTY'}, 'ranking': {'rankCustomFieldId': 10019}})
        if self.path.startswith('/rest/agile/1.0/board/7/issue'):
            s['board_query'] = self.path
            items = s.get('board', [])
            start = int(self.path.split('startAt=')[1].split('&')[0])
            page = items[start:start + 2]  # small pages to exercise paging
            return self._send(200, {'issues': page, 'total': len(items), 'startAt': start, 'maxResults': 2})
        if '/changelog' in self.path:
            vals = s.get('changelog', [])
            if 'startAt=' in self.path:
                return self._send(200, {'values': vals[-100:], 'total': len(vals), 'isLast': True})
            return self._send(200, {'values': vals[:100], 'total': len(vals), 'isLast': len(vals) <= 100})
        if self.path.startswith('/rest/api/3/issue/ABC-1/comment') and 'notify_comments' in s:
            return self._send(200, {'comments': s['notify_comments'], 'total': len(s['notify_comments'])})
        if self.path.startswith('/rest/api/3/issue/ABC-1/comment'):
            if 'expand=renderedBody' in self.path:
                return self._send(200, {'comments': [{'id': '1', 'body': s.get('comment_body'),
                                                      'renderedBody': s.get('comment_html', '')}], 'total': 1})
            return self._send(200, {'comments': [{'id': '1', 'body': s.get('comment_body')}], 'total': 1})
        if self.path.startswith('/rest/api/3/issue/ABC-1?fields=description&expand=renderedFields'):
            return self._send(200, {'key': 'ABC-1', 'renderedFields': {'description': s.get('desc_html', '')}})
        if self.path.startswith('/rest/api/3/issue/ABC-1'):
            return self._send(200, {'key': 'ABC-1', 'names': {'customfield_1': 'Cost center'},
                                    'fields': {'summary': 'Full', 'updated': '2026-11-10T10:00:00.000+0100',
                                               'description': s.get('description') or adf.text_to_adf('hello world'),
                                               'attachment': s.get('attachments', []),
                                               'customfield_1': {'value': 'CC-1'}}})
        return self._send(404, {'errorMessages': ['Issue does not exist']})

    def do_POST(self):
        s = self.state
        if '/attachments' in self.path:
            n = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(n)
            s.setdefault('calls', []).append(('POST', self.path, None))
            if self.headers.get('X-Atlassian-Token') != 'no-check':
                return self._send(403, {'errorMessages': ['XSRF check failed']})
            name = raw.split(b'filename="')[1].split(b'"')[0].decode()
            if name.startswith('toolarge'):
                return self._send(413, {'errorMessages': ['The file is too large']})
            s['next_att'] = s.get('next_att', 100) + 1
            s.setdefault('uploads', []).append((self.path, name, raw.split(b'\r\n\r\n', 1)[1][:20]))
            return self._send(200, [{'id': str(s['next_att']), 'filename': name}])
        body = self._body()
        s.setdefault('calls', []).append(('POST', self.path, body))
        if self.path == '/rest/api/3/issue':
            return self._create(body)
        if self.path.endswith('/comment'):
            s['comment'] = body['body']
            return self._send(201, {'id': '9'})
        if self.path == '/rest/api/3/search/jql':
            if "'GONE-1'" in s.get('reject', '') and 'GONE-1' in body['jql']:
                return self._send(400, {'errorMessages': ["An issue with key 'GONE-1' does not exist for field 'key'."]})
            if 'key ~' in body['jql'] and s.get('no_key_wildcard'):
                return self._send(400, {'errorMessages': ['The operator ~ is not supported by the key field.']})
            all_issues = s.get('issues', [])
            start = int(body.get('nextPageToken') or 0)
            page = all_issues[start:start + body['maxResults']]
            res = {'issues': page}
            if start + len(page) < len(all_issues):
                res['nextPageToken'] = str(start + len(page))
            return self._send(200, res)
        if self.path.endswith('/transitions'):
            return self._send(400, {'errorMessages': ['Please fill in Cost center before closing'], 'errors': {}})
        return self._send(404, {})

    def _create(self, body):
        s = self.state
        if not (body.get('fields') or {}).get('summary'):
            return self._send(400, {'errorMessages': [], 'errors': {'summary': 'You must specify a summary of the issue.'}})
        s['created'] = body['fields']
        return self._send(201, {'id': '20001', 'key': 'OPS-77'})

    def do_PUT(self):
        s = self.state
        body = self._body()
        s.setdefault('calls', []).append(('PUT', self.path, body))
        if self.path.startswith('/rest/api/3/issue/OPS-77'):
            s['edited'] = body['fields']
            self.send_response(204)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return None
        if self.path == '/rest/agile/1.0/issue/rank':
            if body.get('rankBeforeIssue') == 'BAD-1':
                return self._send(207, {'entries': [{'issueKey': body['issues'][0], 'status': 400,
                                                     'errors': ['Ranking is disabled']}]})
            self.send_response(204)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return None
        return self._send(404, {})


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeJira)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.site = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeJira.state = {}
        self.client = JiraClient(self.site, 'a@b.c', 'token')


class ApiTests(ServerCase):
    def test_requires_https_for_remote_sites(self):
        with self.assertRaises(ValueError):
            JiraClient('http://example.atlassian.net', 'a', 'b')

    def test_auth_and_myself(self):
        self.assertEqual(self.client.myself()['accountId'], 'me-1')

    def test_auth_failure_message(self):
        bad = JiraClient(self.site, 'x@y.z', 'nope')
        with self.assertRaises(JiraError) as cm:
            bad.myself()
        self.assertEqual(cm.exception.status, 401)
        self.assertIn('Authentication failed', str(cm.exception))

    def test_search_pages_with_next_token(self):
        FakeJira.state['issues'] = [issue(f'ABC-{i}') for i in range(250)]
        got = self.client.search('project = ABC', ['summary'], limit=230)
        self.assertEqual(len(got), 230)
        self.assertEqual(got[-1]['key'], 'ABC-229')

    def test_retries_get_after_429(self):
        self.assertEqual(self.client.request('GET', '/rest/api/3/flaky'), {'ok': True})

    def test_rule_error_carries_messages(self):
        with self.assertRaises(JiraError) as cm:
            self.client.do_transition('ABC-1', '31')
        self.assertEqual(cm.exception.status, 400)
        self.assertIn('Cost center', cm.exception.messages[0])

    def test_unreachable_is_offline_error(self):
        dead = JiraClient('http://127.0.0.1:9', 'a@b.c', 'token', timeout=2)
        with self.assertRaises(OfflineError):
            dead.myself()

    def test_connection_is_reused(self):
        self.client.myself()
        first = self.client._local.conn
        self.client.myself()
        self.assertIs(first, self.client._local.conn)


class AdfTests(unittest.TestCase):
    TABLE = {'type': 'doc', 'version': 1, 'content': [
        {'type': 'heading', 'attrs': {'level': 2}, 'content': [{'type': 'text', 'text': 'Timings'}]},
        {'type': 'table', 'content': [
            {'type': 'tableRow', 'content': [
                {'type': 'tableHeader', 'content': [{'type': 'paragraph', 'content': [{'type': 'text', 'text': 'Job'}]}]},
                {'type': 'tableHeader', 'content': [{'type': 'paragraph', 'content': [{'type': 'text', 'text': 'Time'}]}]}]},
            {'type': 'tableRow', 'content': [
                {'type': 'tableCell', 'attrs': {'colspan': 2}, 'content': [{'type': 'paragraph', 'content': [
                    {'type': 'text', 'text': 'Total', 'marks': [{'type': 'strong'}]}]}]}]}]},
        {'type': 'bodiedExtension', 'attrs': {'extensionKey': 'drawio'}},
        {'type': 'mysteryNode'},
    ]}

    def test_table_with_merged_cell(self):
        blocks = adf.to_blocks(self.TABLE)
        self.assertEqual([b['type'] for b in blocks], ['heading', 'table', 'extension', 'unsupported'])
        rows = blocks[1]['rows']
        self.assertTrue(rows[0][0]['header'])
        self.assertEqual(rows[1][0]['colspan'], 2)
        self.assertEqual(adf.inlines_to_markup(rows[1][0]['blocks'][0]['inlines']), '<b>Total</b>')

    def test_markup_escapes(self):
        inl = [{'kind': 'text', 'text': 'a<b & "c"', 'marks': {'link': 'https://x/?a=1&b=2'}}]
        self.assertEqual(adf.inlines_to_markup(inl), '<a href="https://x/?a=1&amp;b=2">a&lt;b &amp; &quot;c&quot;</a>')

    def test_text_roundtrip_and_mentions(self):
        doc = adf.text_to_adf('one\n\ntwo')
        self.assertEqual(len(doc['content']), 3)
        self.assertEqual(adf.to_text(doc), 'one\n\ntwo\n')
        m = {'type': 'doc', 'content': [{'type': 'paragraph', 'content': [{'type': 'mention', 'attrs': {'id': 'me-1'}}]}]}
        self.assertTrue(adf.mentions(m, 'me-1'))
        self.assertFalse(adf.mentions(m, 'other'))


class SearchTests(unittest.TestCase):
    SHOW = {'todo': True, 'progress': True, 'done': False}

    def test_default_list_is_mine_in_projects(self):
        jql, _, _ = search.build('mine', '', ['OPS', 'WEB'], self.SHOW, 'created', True, ['OPS-1'])
        self.assertIn('project in ("OPS", "WEB")', jql)
        self.assertIn('watcher = currentUser()', jql)
        self.assertIn('key in (OPS-1)', jql)
        self.assertIn('statusCategory in ("To Do", "In Progress")', jql)
        self.assertTrue(jql.endswith('ORDER BY created DESC'))

    def test_project_and_key_prefix(self):
        jql, title, _ = search.build('mine', 'ops- web-', [], self.SHOW, 'priority', False)
        self.assertIn('project in ("OPS", "WEB")', jql)
        self.assertNotIn('currentUser', jql)
        self.assertIn('ORDER BY priority ASC, created DESC', jql)
        jql, _, fb = search.build('mine', 'ops-12', [], self.SHOW, 'created', True)
        self.assertIn('key ~ "OPS-12*"', jql)
        self.assertEqual(fb[1], 'OPS-12')

    def test_text_is_sanitised(self):
        jql, _, _ = search.build('mine', 'nginx "upgrade" (api)', [], self.SHOW, 'created', True)
        self.assertIn('text ~ "nginx  upgrade   api*"', jql)

    def test_saved_filter_keeps_its_query_but_our_order(self):
        jql, _, _ = search.build('filter', '', [], {'todo': 1, 'progress': 1, 'done': 1}, 'updated', True,
                                 filter_jql='project = X ORDER BY rank')
        self.assertEqual(jql, '(project = X) ORDER BY updated DESC, created DESC')

    def test_views_and_created_range(self):
        import datetime
        show = {'todo': 1, 'progress': 1, 'done': 1}
        jql, _, _ = search.build('all', '', ['OPS', 'WEB'], show, 'status', False)
        self.assertEqual(jql, '(project in ("OPS", "WEB")) ORDER BY status ASC, created DESC')
        rng = (datetime.date(2026, 11, 2), datetime.date(2026, 11, 8))
        jql, _, _ = search.build('project', 'nginx', ['OPS'], self.SHOW, 'created', True, project='WEB', created=rng)
        self.assertEqual(jql, '(project = "WEB" AND text ~ "nginx*") AND statusCategory in ("To Do", "In Progress") '
                              'AND created >= "2026-11-02" AND created < "2026-11-09" ORDER BY created DESC')

    def test_week_range(self):
        import datetime
        wed = datetime.date(2026, 11, 11)
        self.assertEqual(search.week_range(0, wed), (datetime.date(2026, 11, 9), wed))
        self.assertEqual(search.week_range(-1, wed), (datetime.date(2026, 11, 2), datetime.date(2026, 11, 8)))

    def test_unbounded_is_bounded(self):
        jql, _, _ = search.build('filter', '', [], {'todo': 1, 'progress': 1, 'done': 1}, 'created', True, filter_jql='ORDER BY rank')
        self.assertTrue(jql.startswith('created >= -90d'))


class FieldTests(unittest.TestCase):
    def test_display(self):
        self.assertEqual(fields.display({'displayName': 'Alex'}), 'Alex')
        self.assertEqual(fields.display([{'name': 'a'}, {'name': 'b'}]), 'a, b')
        self.assertEqual(fields.display('2026-11-10T10:00:00.000+0100'), '2026-11-10 10:00')
        self.assertEqual(fields.display({'value': 'EU', 'child': {'value': 'SE'}}), 'EU › SE')

    def test_rule_error_matches_field_by_name(self):
        meta = {'customfield_1': {'name': 'Cost center (EUR)', 'schema': {'type': 'option'}, 'allowedValues': [{'id': '1', 'value': 'A'}]},
                'customfield_2': {'name': 'Team', 'schema': {'type': 'option'}, 'allowedValues': [{'id': '2', 'value': 'B'}]},
                'description': {'name': 'Description', 'schema': {'type': 'string'}}}
        err = JiraError(400, 'Please fill in Cost Center before closing the description', ['Please fill in Cost Center'])
        self.assertEqual([f for f, _ in fields.fields_for_rule_error(meta, err)], ['customfield_1'])

    def test_detail_rows_ids_and_clip(self):
        rows = fields.detail_rows({'priority': {'name': 'High'}, 'customfield_9': 'x' * 1000, 'parent': {'key': 'A-1'}},
                                  {'customfield_9': 'Notes'})
        self.assertEqual([r[0] for r in rows], ['priority', 'customfield_9'])
        shown, clipped = fields.clip(rows[1][2])
        self.assertTrue(clipped)
        self.assertLessEqual(len(shown), fields.MAX_DETAIL_CHARS + 1)
        self.assertEqual(fields.clip('short\n text'), ('short text', False))
        shown, _ = fields.clip('word ' * 100, 50)
        self.assertTrue(shown.endswith('word…'))

    def test_form_values(self):
        opt = {'schema': {'type': 'option'}, 'allowedValues': [{'id': '7'}]}
        self.assertEqual(fields.form_value(opt, ['7']), {'id': '7'})
        multi = {'schema': {'type': 'array', 'items': 'option'}, 'allowedValues': [{'id': '7'}]}
        self.assertEqual(fields.form_value(multi, ['7', '8']), [{'id': '7'}, {'id': '8'}])
        self.assertEqual(fields.form_value({'schema': {'type': 'number'}}, text='1,5'), 1.5)
        self.assertIsNone(fields.form_value(opt, []))


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Cache(Path(self.tmp.name) / 'c.db')

    def tearDown(self):
        self.tmp.cleanup()

    def test_partial_update_keeps_full_details_only_if_unchanged(self):
        full = issue('ABC-1', description=adf.text_to_adf('details'))
        self.cache.put_issues([full], names={'x': 'y'}, full=True)
        self.cache.put_issues([issue('ABC-1')])  # same updated -> still full
        self.assertTrue(self.cache.get_issue('ABC-1')['full'])
        self.assertEqual(self.cache.get_issue('ABC-1')['names'], {'x': 'y'})
        self.cache.put_issues([issue('ABC-1', updated='2026-11-11T09:00:00.000+0100')])
        got = self.cache.get_issue('ABC-1')
        self.assertFalse(got['full'])
        self.assertIn('description', got['fields'])  # still shown, but marked stale

    def test_lists_preserve_order(self):
        self.cache.put_issues([issue('A-1'), issue('A-2'), issue('A-3')])
        self.cache.put_list('q', ['A-3', 'A-1'])
        keys, _ = self.cache.get_list('q')
        self.assertEqual([i['key'] for i in self.cache.get_issues(keys)], ['A-3', 'A-1'])

    def test_full_text_search(self):
        self.cache.put_issues([issue('A-1', summary='Rotate TLS certificates'),
                               issue('A-2', summary='Backup', description=adf.text_to_adf('nightly certificate job'))])
        self.assertEqual({i['key'] for i in self.cache.search_local('certif')}, {'A-1', 'A-2'})
        self.assertEqual(self.cache.search_local('"; DROP TABLE issues; --'), [])

    def test_clear(self):
        self.cache.put_issues([issue('A-1')])
        self.cache.put_blob('thumb-1', b'x')
        self.cache.clear()
        self.assertEqual(self.cache.stats()['issues'], 0)
        self.assertIsNone(self.cache.get_blob('thumb-1'))


class StoreTests(ServerCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Cache(Path(self.tmp.name) / 'c.db')
        cfg = Config(site=self.site, email='a@b.c', projects=['ABC'])
        self.store = Store(cfg, self.cache, Worker(dispatch=direct_dispatch, max_workers=2))
        self.store.set_credentials('token')

    def tearDown(self):
        self.store.worker.shutdown(wait=True)
        self.tmp.cleanup()

    def wait(self, event):
        self.assertTrue(event.wait(5), 'timed out')

    def test_list_shows_cache_then_fresh(self):
        FakeJira.state['issues'] = [issue('ABC-1', 'fresh')]
        done = threading.Event()
        seen = []
        self.store.load_list('mine', '', None, lambda *a: seen.append('cached'),
                             lambda issues, jql, title: (seen.append(issues[0]['fields']['summary']), done.set()),
                             lambda e: done.set())
        self.wait(done)
        self.assertEqual(seen, ['fresh'])  # nothing cached the first time
        done.clear()
        self.store.load_list('mine', '', None, lambda issues, *a: seen.append('cached:' + issues[0]['key']),
                             lambda *a: done.set(), lambda e: done.set())
        self.wait(done)
        self.assertEqual(seen, ['fresh', 'cached:ABC-1'])

    def test_deleted_mention_is_dropped_and_query_retried(self):
        FakeJira.state.update(issues=[issue('ABC-1')], reject="'GONE-1'")
        self.store.mentioned = ['GONE-1']
        done = threading.Event()
        out = {}
        self.store.load_list('mine', '', None, lambda *a: None,
                             lambda issues, jql, title: (out.update(jql=jql), done.set()),
                             lambda e: (out.update(err=e), done.set()))
        self.wait(done)
        self.assertNotIn('err', out)
        self.assertNotIn('GONE-1', out['jql'])
        self.assertEqual(self.store.mentioned, [])

    def test_key_prefix_fallback(self):
        FakeJira.state.update(issues=[issue('ABC-12'), issue('ABC-120'), issue('ABC-2')], no_key_wildcard=True)
        done = threading.Event()
        out = {}
        self.store.load_list('mine', 'abc-12', None, lambda *a: None,
                             lambda issues, jql, title: (out.update(keys=[i['key'] for i in issues]), done.set()),
                             lambda e: (out.update(err=e), done.set()))
        self.wait(done)
        self.assertEqual(out.get('keys'), ['ABC-12', 'ABC-120'])

    def test_issue_loads_and_caches_names(self):
        done = threading.Event()
        out = {}
        self.store.load_issue('ABC-1', lambda cached, comments: out.update(cached=cached),
                              lambda it: (out.update(issue=it), done.set()),
                              lambda cs: out.update(comments=cs), lambda e: done.set())
        self.wait(done)
        self.assertIsNone(out['cached'])
        self.assertEqual(out['issue']['names']['customfield_1'], 'Cost center')
        self.assertTrue(self.cache.get_issue('ABC-1')['full'])

    def test_offline_marks_store_offline(self):
        self.store.client = JiraClient('http://127.0.0.1:9', 'a@b.c', 'token', timeout=2)
        changes, done = [], threading.Event()
        self.store.on_online_changed(changes.append)
        self.store.load_list('mine', '', None, lambda *a: None, lambda *a: done.set(), lambda e: done.set())
        self.wait(done)
        self.assertEqual(changes, [False])


def media_doc(*nodes):
    return {'type': 'doc', 'version': 1, 'content': [
        {'type': 'mediaSingle', 'content': [{'type': 'media', 'attrs': {'type': 'file', 'id': mid, 'alt': alt}}]}
        for mid, alt in nodes]}


class MediaTests(unittest.TestCase):
    ATTS = [{'id': '100', 'filename': 'shot.png'}, {'id': '101', 'filename': 'image.png'},
            {'id': '102', 'filename': 'image.png'}]

    def test_unique_name_matches_without_html(self):
        found = media.nodes(media_doc(('u1', 'shot.png')))
        self.assertFalse(media.needs_html(found, self.ATTS, {}))
        self.assertEqual(media.lookup('u1', 'shot.png', {}, self.ATTS)['id'], '100')

    def test_duplicate_or_missing_names_need_html(self):
        self.assertTrue(media.needs_html(media.nodes(media_doc(('u2', 'image.png'))), self.ATTS, {}))
        self.assertTrue(media.needs_html(media.nodes(media_doc(('u3', ''))), self.ATTS, {}))
        self.assertFalse(media.needs_html(media.nodes(media_doc(('u3', ''))), self.ATTS, {'u3': '102'}))

    def test_html_exact_ids_win(self):
        found = media.nodes(media_doc(('u1', ''), ('u2', '')))
        html = ('<p><span class="image-wrap"><a href="/rest/api/3/attachment/content/102">'
                '<img src="/rest/api/3/attachment/content/102" data-media-services-id="u2"></a></span>'
                '<img src="/rest/api/3/attachment/content/101" data-media-services-id="u1"></p>')
        self.assertEqual(media.html_mapping(found, html), {'u2': '102', 'u1': '101'})

    def test_html_by_position_only_when_counts_agree(self):
        found = media.nodes(media_doc(('u1', ''), ('u2', '')))
        two = '<img src="/rest/api/3/attachment/content/101"><a href="/secure/attachment/102/x.png">x</a>'
        self.assertEqual(media.html_mapping(found, two), {'u1': '101', 'u2': '102'})
        one = '<img src="/rest/api/3/attachment/content/101">'
        self.assertEqual(media.html_mapping(found, one), {})
        # links to the same file around an image count once
        wrapped = ('<a href="/secure/attachment/101/a.png"><img src="/secure/thumbnail/101/a.png"></a>'
                   '<img src="/rest/api/3/attachment/content/102">')
        self.assertEqual(media.html_mapping(found, wrapped), {'u1': '101', 'u2': '102'})

    def test_lookup_ignores_ids_not_on_ticket(self):
        self.assertIsNone(media.lookup('u9', '', {'u9': '999'}, self.ATTS))

    def test_external_and_inline_nodes(self):
        doc = {'type': 'doc', 'content': [
            {'type': 'mediaSingle', 'content': [{'type': 'media', 'attrs': {'type': 'external', 'url': 'https://x/y.png'}}]},
            {'type': 'paragraph', 'content': [{'type': 'mediaInline', 'attrs': {'id': 'u5', 'type': 'file'}}]}]}
        self.assertEqual(media.nodes(doc), [{'id': 'u5', 'alt': ''}])


class BoardTests(unittest.TestCase):
    CONFIG = {'columnConfig': {'columns': [
        {'name': 'To Do', 'statuses': [{'id': '1'}, {'id': 2}]},
        {'name': 'Doing', 'statuses': [{'id': '3'}], 'max': 3},
        {'name': 'Empty', 'statuses': []}]},
        'subQuery': {'query': 'fixVersion in unreleasedVersions()'}}

    def test_columns_and_grouping(self):
        cols = boards.columns(self.CONFIG)
        self.assertEqual([c['name'] for c in cols], ['To Do', 'Doing', 'Empty'])
        self.assertEqual(cols[0]['statuses'], {'1', '2'})
        issues = [issue('A-1', status={'id': '2'}), issue('A-2', status={'id': '3'}),
                  issue('A-3', status={'id': '99'}), issue('A-4', status={'id': '1'})]
        grouped, unplaced = boards.group(issues, cols)
        self.assertEqual([[i['key'] for i in g] for g in grouped], [['A-1', 'A-4'], ['A-2'], []])
        self.assertEqual(unplaced, 1)

    def test_kanban_jql(self):
        self.assertEqual(boards.kanban_jql(self.CONFIG, 14),
                         '(fixVersion in unreleasedVersions()) AND (statusCategory != Done OR updated >= -14d)')
        self.assertEqual(boards.kanban_jql({}, 7), '(statusCategory != Done OR updated >= -7d)')

    def test_transitions_into_column(self):
        cols = boards.columns(self.CONFIG)
        ts = [{'id': '11', 'to': {'id': '3'}}, {'id': '21', 'to': {'id': '1'}}, {'id': '31', 'to': {'id': 2}}]
        self.assertEqual([t['id'] for t in boards.transitions_into(ts, cols[0])], ['21', '31'])
        self.assertEqual(boards.transitions_into(ts, cols[2]), [])

    def test_drop_plan(self):
        keys = ['A', 'B', 'C']
        self.assertEqual(boards.drop_plan(keys, 'A', 0, 0, 1), (False, None))     # same place
        self.assertEqual(boards.drop_plan(keys, 'C', 0, 0, 3), (False, None))     # same place, end
        self.assertEqual(boards.drop_plan(keys, 'A', 0, 0, 2), (False, ('before', 'C')))
        self.assertEqual(boards.drop_plan(keys, 'C', 0, 0, 0), (False, ('before', 'A')))
        self.assertEqual(boards.drop_plan(keys, 'A', 0, 0, 3), (False, ('after', 'C')))
        self.assertEqual(boards.drop_plan(keys, 'X', 1, 0, 3), (True, ('after', 'C')))
        self.assertEqual(boards.drop_plan(keys, 'X', 1, 0, 1), (True, ('before', 'B')))
        self.assertEqual(boards.drop_plan([], 'X', 1, 0, 0), (True, None))


class RelationTests(unittest.TestCase):
    def stub(self, key, cat='new', summary='S'):
        return {'key': key, 'fields': {'summary': summary, 'status': {'name': cat, 'statusCategory': {'key': cat}},
                                       'issuetype': {'name': 'Task'}}}

    def test_parent_links_subtasks(self):
        from ticketdesk.core import relations
        f = {'issuetype': {'name': 'Story', 'hierarchyLevel': 0}, 'parent': self.stub('EP-1'),
             'subtasks': [self.stub('A-2', 'done')],
             'issuelinks': [{'type': {'name': 'Blocks', 'inward': 'is blocked by', 'outward': 'blocks'},
                             'outwardIssue': self.stub('A-3')},
                            {'type': {'name': 'Blocks', 'inward': 'is blocked by', 'outward': 'blocks'},
                             'inwardIssue': self.stub('A-4')},
                            {'type': {'name': 'Blocks', 'inward': 'is blocked by', 'outward': 'blocks'},
                             'outwardIssue': self.stub('A-5')}]}
        self.assertEqual(relations.parent(f)['key'], 'EP-1')
        self.assertEqual([s['category'] for s in relations.subtasks(f)], ['done'])
        self.assertEqual([(p, [b['key'] for b in g]) for p, g in relations.links(f)],
                         [('blocks', ['A-3', 'A-5']), ('is blocked by', ['A-4'])])
        self.assertFalse(relations.has_child_query(f))
        self.assertTrue(relations.has_child_query({'issuetype': {'hierarchyLevel': 1}}))

    def test_summary_and_order(self):
        from ticketdesk.core import relations
        kids = [relations.brief(self.stub(k, c)) for k, c in
                (('A-1', 'done'), ('A-2', 'new'), ('A-3', 'indeterminate'), ('A-4', 'new'))]
        self.assertEqual(relations.summarize(kids), {'todo': 2, 'progress': 1, 'done': 1})
        self.assertEqual([c['key'] for c in relations.ordered(kids)], ['A-3', 'A-2', 'A-4', 'A-1'])


class SwimlaneTests(unittest.TestCase):
    def test_lanes_and_changes(self):
        def it(key, who=None, prio=None, parent=None):
            f = {}
            if who:
                f['assignee'] = {'accountId': who.lower(), 'displayName': who}
            if prio:
                f['priority'] = {'id': prio[0], 'name': prio[1]}
            if parent:
                f['parent'] = {'key': parent, 'fields': {'summary': 'Epic'}}
            return {'key': key, 'fields': f}
        issues = [it('A-1', 'Ylva', ('3', 'Medium'), 'EP-2'), it('A-2', None, ('1', 'Highest')),
                  it('A-3', 'Bo', ('3', 'Medium'), 'EP-1'), it('A-4', 'Ylva', None, 'EP-2')]
        self.assertEqual([(k, t, [i['key'] for i in g]) for k, t, g in boards.lanes(issues, 'assignee')],
                         [('bo', 'Bo', ['A-3']), ('ylva', 'Ylva', ['A-1', 'A-4']), ('-', 'Unassigned', ['A-2'])])
        self.assertEqual([k for k, _, _ in boards.lanes(issues, 'priority')], ['1', '3', '-'])
        self.assertEqual([t for _, t, _ in boards.lanes(issues, 'parent')], ['EP-2 Epic', 'EP-1 Epic', 'No parent'])
        self.assertEqual(boards.lane_change('assignee', '-'), {'assignee': None})
        self.assertEqual(boards.lane_change('assignee', 'bo'), {'assignee': {'accountId': 'bo'}})
        self.assertEqual(boards.lane_change('parent', 'EP-1'), {'parent': {'key': 'EP-1'}})
        self.assertIsNone(boards.lane_change('issuetype', '10001'))
        self.assertIsNone(boards.lane_change('priority', '-'))
        self.assertIsNone(boards.lane_change('parent', '-'))


class BookmarkConfigTests(unittest.TestCase):
    def test_toggle(self):
        c = Config()
        self.assertTrue(c.toggle_bookmark({'kind': 'board', 'id': 7, 'name': 'B'}))
        self.assertTrue(c.is_bookmarked({'kind': 'board', 'id': '7'}))
        self.assertTrue(c.toggle_bookmark({'kind': 'project', 'key': 'ABC'}))
        self.assertFalse(c.toggle_bookmark({'kind': 'board', 'id': 7}))
        self.assertEqual(c.bookmarks, [{'kind': 'project', 'key': 'ABC'}])
        self.assertEqual(c.shown_fields, ['assignee', 'reporter', 'issuetype', 'created', 'updated'])


class CreateLogicTests(unittest.TestCase):
    def test_types_and_defaults(self):
        from ticketdesk.core import create
        types = [{'id': '1', 'name': 'Bug'}, {'id': '2', 'name': 'Improvement'}, {'id': '3', 'name': 'Task'},
                 {'id': '4', 'name': 'Epic'}, {'id': '5', 'name': 'Story'}]
        self.assertEqual([t['name'] for t in create.order_types(types)], ['Task', 'Story', 'Epic', 'Bug', 'Improvement'])
        self.assertEqual(create.default_type(types)['id'], '3')
        prio = {'allowedValues': [{'id': '1', 'name': 'Highest'}, {'id': '3', 'name': 'Medium'}],
                'defaultValue': {'id': '1'}}
        self.assertEqual(create.default_option(prio), '3')
        self.assertEqual(create.default_option({'allowedValues': [{'id': '9', 'name': 'P2'}], 'defaultValue': {'id': '9'}}), '9')

    def test_split_fields(self):
        from ticketdesk.core import create
        fl = [{'fieldId': 'summary', 'required': True, 'schema': {'type': 'string'}},
              {'fieldId': 'customfield_1', 'name': 'Cost center', 'required': False,
               'schema': {'type': 'option'}, 'allowedValues': [{'id': '1', 'value': 'A'}]},
              {'fieldId': 'customfield_2', 'name': 'Team', 'required': True, 'schema': {'type': 'string'}},
              {'fieldId': 'customfield_3', 'name': 'Sprint', 'required': True, 'schema': {'type': 'array', 'items': 'json'}},
              {'fieldId': 'labels', 'name': 'Labels', 'required': False, 'schema': {'type': 'array', 'items': 'string'}},
              {'fieldId': 'attachment', 'name': 'Attachment', 'required': False, 'schema': {'type': 'array', 'items': 'attachment'}},
              {'fieldId': 'customfield_4', 'name': 'Defaulted', 'required': True, 'hasDefaultValue': True,
               'schema': {'type': 'string'}}]
        shown, more, browser_only = create.split(fl, ['customfield_1'])
        self.assertEqual([create.field_id(m) for m in shown], ['customfield_1', 'customfield_2'])
        self.assertEqual([create.field_id(m) for m in more], ['customfield_4', 'labels'])
        self.assertEqual([create.field_id(m) for m in browser_only], ['customfield_3'])

    def test_payload(self):
        from ticketdesk.core import create
        body = create.payload('OPS', 10001, ' Fix it ', description={'type': 'doc'}, reporter='me', assignee=None,
                              priority='3', other={'customfield_1': {'id': '1'}, 'labels': None},
                              available={'summary', 'description', 'priority', 'customfield_1'})
        self.assertEqual(body, {'project': {'key': 'OPS'}, 'issuetype': {'id': '10001'}, 'summary': 'Fix it',
                                'description': {'type': 'doc'}, 'priority': {'id': '3'},
                                'customfield_1': {'id': '1'}})

    def test_rich_text_and_parent_form_values(self):
        area = {'schema': {'type': 'string', 'custom': 'com.atlassian.jira.plugin.system.customfieldtypes:textarea'}}
        self.assertEqual(fields.form_value(area, text='a\n\nb')['type'], 'doc')
        self.assertEqual(fields.form_value({'fieldId': 'parent', 'schema': {'type': 'issuelink', 'system': 'parent'}},
                                           text='ops-12'), {'key': 'OPS-12'})


class AgileApiTests(ServerCase):
    def test_board_issues_page_and_total(self):
        FakeJira.state['board'] = [issue(f'ABC-{i}') for i in range(5)]
        got, total = self.client.board_issues(7, ['summary', 'status'], limit=500, jql='x = 1')
        self.assertEqual([i['key'] for i in got], [f'ABC-{i}' for i in range(5)])
        self.assertEqual(total, 5)
        self.assertIn('fields=summary%2Cstatus', FakeJira.state['board_query'])
        got, total = self.client.board_issues(7, ['summary'], limit=3)
        self.assertEqual((len(got), total), (3, 5))

    def test_rank_body_and_partial_failure(self):
        self.client.rank(['ABC-1'], before='ABC-2', field_id='10019')
        self.assertEqual(FakeJira.state['calls'][-1][2],
                         {'issues': ['ABC-1'], 'rankBeforeIssue': 'ABC-2', 'rankCustomFieldId': 10019})
        with self.assertRaises(JiraError) as cm:
            self.client.rank(['ABC-1'], before='BAD-1')
        self.assertIn('Ranking is disabled', str(cm.exception))

    def test_external_image_requires_https(self):
        with self.assertRaises(JiraError):
            self.client.external_image('http://example.com/a.png')


class StoreMediaBoardTests(ServerCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Cache(Path(self.tmp.name) / 'c.db')
        self.store = Store(Config(site=self.site, email='a@b.c', projects=['ABC']), self.cache,
                           Worker(dispatch=direct_dispatch, max_workers=2))
        self.store.set_credentials('token')

    def tearDown(self):
        self.store.worker.shutdown(wait=True)
        self.tmp.cleanup()

    def load(self):
        done, out = threading.Event(), {}
        pending = {'issue', 'comments'}

        def finish(part, value):
            out[part] = value
            pending.discard(part)
            if not pending:
                done.set()
        self.store.load_issue('ABC-1', lambda *a: None, lambda it: finish('issue', it),
                              lambda cs: finish('comments', cs), lambda e: finish('issue', e))
        self.assertTrue(done.wait(5))
        return out

    def test_description_media_by_name_needs_no_html(self):
        FakeJira.state.update(description=media_doc(('u1', 'shot.png')),
                              attachments=[{'id': '100', 'filename': 'shot.png'}])
        self.load()
        self.assertFalse(any('renderedFields' in c[1] for c in FakeJira.state['calls']))

    def test_description_and_comment_media_resolved_from_html(self):
        FakeJira.state.update(
            description=media_doc(('u1', 'image.png')),
            attachments=[{'id': '101', 'filename': 'image.png'}, {'id': '102', 'filename': 'image.png'},
                         {'id': '103', 'filename': 'c.png'}],
            desc_html='<img src="/rest/api/3/attachment/content/102">',
            comment_body=media_doc(('c1', '')),
            comment_html='<img src="/rest/api/3/attachment/content/103" data-media-services-id="c1">')
        out = self.load()
        self.assertNotIn('renderedBody', json.dumps(out['comments']))
        self.assertEqual(self.store.media_map('ABC-1'), {'u1': '102', 'c1': '103'})

    def test_boards_and_board_load(self):
        got = threading.Event()
        found = []
        self.store.boards(lambda b: (found.extend(b), got.set()))
        self.assertTrue(got.wait(5))
        self.assertEqual(found, [{'id': 7, 'name': 'ABC board', 'type': 'kanban', 'project': 'ABC'}])
        FakeJira.state['board'] = [issue('ABC-1', status={'id': '3'}), issue('ABC-2', status={'id': '1'}),
                                   issue('ABC-3', status={'id': '3'})]
        done, out = threading.Event(), {}
        self.store.load_board(found[0], lambda st: out.update(cached=st),
                              lambda st: (out.update(fresh=st), done.set()), lambda e: (out.update(err=e), done.set()))
        self.assertTrue(done.wait(5))
        self.assertNotIn('err', out)
        self.assertIn('fixVersion+is+EMPTY', FakeJira.state['board_query'])
        grouped, _ = boards.group(out['fresh']['issues'], out['fresh']['columns'])
        self.assertEqual([[i['key'] for i in g] for g in grouped], [['ABC-2'], ['ABC-1', 'ABC-3'], []])
        # second load shows the cached board first
        done.clear()
        self.store.load_board(found[0], lambda st: out.update(cached=st), lambda st: done.set(), lambda e: done.set())
        self.assertTrue(done.wait(5))
        self.assertEqual([i['key'] for i in out['cached']['issues']], ['ABC-1', 'ABC-2', 'ABC-3'])


PNG_1x1 = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x02\x80\x00\x00\x01\xe0\x08\x02\x00\x00\x00'
           b'\x00\x00\x00\x00IEND\xaeB`\x82')


def adf_mention(acc):
    return {'type': 'doc', 'version': 1, 'content': [{'type': 'paragraph', 'content': [
        {'type': 'text', 'text': 'hi '}, {'type': 'mention', 'attrs': {'id': acc, 'text': '@Me'}}]}]}


class NotifyTests(unittest.TestCase):
    ME = 'me-1'
    SINCE = 1_800_000_000_000  # 2027-01-15

    def ts(self, offset_s):
        import datetime
        return datetime.datetime.fromtimestamp((self.SINCE + offset_s * 1000) / 1000,
                                               datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000+0000')

    def test_rules(self):
        from ticketdesk.core import notify
        bob = {'accountId': 'bob', 'displayName': 'Bob'}
        me = {'accountId': self.ME, 'displayName': 'Me'}
        issue = {'key': 'OPS-1', 'fields': {'summary': 'Disk full', 'assignee': me, 'creator': bob,
                                            'created': self.ts(-86400)}}
        comments = [
            {'id': '1', 'author': bob, 'created': self.ts(10), 'updated': self.ts(10), 'body': adf_mention(self.ME)},
            {'id': '2', 'author': bob, 'created': self.ts(20), 'updated': self.ts(20), 'body': adf.text_to_adf('plain')},
            {'id': '3', 'author': me, 'created': self.ts(30), 'updated': self.ts(30), 'body': adf_mention(self.ME)},
            {'id': '4', 'author': bob, 'created': self.ts(-100), 'updated': self.ts(-100), 'body': adf.text_to_adf('old')}]
        changelog = [
            {'id': '10', 'author': bob, 'created': self.ts(40), 'items': [{'field': 'status', 'fromString': 'To Do', 'toString': 'Done'}]},
            {'id': '11', 'author': bob, 'created': self.ts(50), 'items': [{'field': 'assignee', 'to': self.ME}]},
            {'id': '12', 'author': bob, 'created': self.ts(60),
             'items': [{'field': 'description', 'fromString': 'x', 'toString': f'see [~accountid:{self.ME}]'}]},
            {'id': '13', 'author': me, 'created': self.ts(70), 'items': [{'field': 'status', 'toString': 'Open'}]}]
        ev = notify.events_for(issue, comments, changelog, self.ME, self.SINCE)
        self.assertEqual([(e['id'], e['kind']) for e in ev],
                         [('cm:1', 'mention'), ('c:2', 'comment'), ('hs:10', 'status'), ('ha:11', 'assigned'), ('hd:12', 'mention')])
        self.assertEqual(ev[2]['title'], 'OPS-1: To Do → Done')
        # not involved: only mentions
        other = {'key': 'OPS-2', 'fields': {'summary': 's', 'assignee': bob, 'creator': bob, 'created': self.ts(-86400)}}
        ev = notify.events_for(other, comments, changelog, self.ME, self.SINCE)
        self.assertEqual([e['kind'] for e in ev], ['mention', 'assigned', 'mention'])
        # switched off
        self.assertEqual(notify.events_for(issue, comments, changelog, self.ME, self.SINCE, mentions=False, updates=False), [])

    def test_new_ticket_mention_and_assignment(self):
        from ticketdesk.core import notify
        bob = {'accountId': 'bob', 'displayName': 'Bob'}
        new = {'key': 'OPS-3', 'fields': {'summary': 'n', 'creator': bob, 'created': self.ts(5), 'description': adf_mention(self.ME)}}
        self.assertEqual([e['id'] for e in notify.events_for(new, [], [], self.ME, self.SINCE)], ['new:OPS-3'])
        new['fields'].update(description=None, assignee={'accountId': self.ME})
        self.assertEqual([e['kind'] for e in notify.events_for(new, [], [], self.ME, self.SINCE)], ['assigned'])

    def test_queries_and_times(self):
        from ticketdesk.core import notify
        q = notify.queries(True, True, ['OPS'], 123)
        self.assertIn('watcher = currentUser()', q[0])
        self.assertTrue(q[1].startswith('project in ("OPS") AND updated >= 123'))
        self.assertEqual(notify.queries(False, False, [], 1), [])
        self.assertEqual(notify.jira_ms('2026-11-12T10:00:00.000+0100'), 1794474000000)
        self.assertEqual(notify.jira_ms(None), 0)

    def test_cache_history(self):
        with tempfile.TemporaryDirectory() as d:
            c = Cache(Path(d) / 'c.db')
            e1 = {'id': 'a', 'key': 'K-1', 'kind': 'mention', 'title': 't1', 'time': 2}
            e2 = {'id': 'b', 'key': 'K-2', 'kind': 'comment', 'title': 't2', 'time': 1}
            self.assertEqual([e['id'] for e in c.add_notifications([e1, e2])], ['a', 'b'])
            self.assertEqual(c.add_notifications([e1]), [])
            self.assertEqual(c.unread_count(), 2)
            c.mark_notifications_read(key='K-1')
            self.assertEqual([(n['id'], n['read']) for n in c.notifications()], [('a', True), ('b', False)])
            c.clear()  # the offline cache can be cleared; notification history stays
            self.assertEqual(c.unread_count(), 1)
            c.mark_notifications_read()
            self.assertEqual(c.unread_count(), 0)


class AttachmentLogicTests(unittest.TestCase):
    def test_editor_image_round_trip_to_upload_placeholders(self):
        import base64
        from ticketdesk.core import attachments
        from ticketdesk.core.adf_html import html_to_adf
        src = ('<p>see</p><figure class="td-image" contenteditable="false" data-pending="t1" data-alt="image.png" '
               'data-width="50" data-w="640" data-h="480"><img src="data:image/png;base64,'
               + base64.b64encode(PNG_1x1).decode() + '"></figure><p>end</p>')
        pending = {}
        doc = html_to_adf(src, pending)
        self.assertEqual(pending['t1']['data'], PNG_1x1)
        self.assertEqual(pending['t1']['mime'], 'image/png')
        node = doc['content'][1]
        self.assertEqual(node['attrs'], {'layout': 'center', 'width': 50, 'widthType': 'percentage'})
        self.assertEqual(node['content'][0]['attrs'],
                         {'type': 'file', 'id': 'td-pending:t1', 'collection': '', 'alt': 'image.png', 'width': 640, 'height': 480})
        self.assertEqual(attachments.pending_tokens(doc), ['t1'])
        self.assertEqual([b['type'] for b in attachments.without_pending(doc)['content']], ['paragraph', 'paragraph'])
        done, missing = attachments.resolve(doc, {'t1': {'media_id': 'uuid-1', 'filename': 'image-1.png'}})
        self.assertEqual(done['content'][1]['content'][0]['attrs']['id'], 'uuid-1')
        self.assertEqual(missing, [])
        fallback, missing = attachments.resolve(doc, {'t1': {'media_id': None, 'filename': 'image-1.png'}})
        self.assertEqual(fallback['content'][1]['type'], 'paragraph')
        self.assertIn('image-1.png', fallback['content'][1]['content'][0]['text'])
        self.assertEqual(missing, ['image-1.png'])

    def test_resized_existing_image_keeps_media_and_changes_width(self):
        from ticketdesk.core.adf_html import adf_to_html, html_to_adf
        node = {'type': 'mediaSingle', 'attrs': {'layout': 'center', 'width': 760, 'widthType': 'pixel'},
                'content': [{'type': 'media', 'attrs': {'type': 'file', 'id': 'm1', 'collection': '', 'alt': 'a.png'}}]}
        doc = {'type': 'doc', 'version': 1, 'content': [node]}
        h = adf_to_html(doc)
        self.assertIn('data-width="100"', h)
        self.assertEqual(html_to_adf(h), doc)  # untouched: exactly as before
        resized = html_to_adf(h.replace('data-width="100"', 'data-width="40"'))
        self.assertEqual(resized['content'][0]['attrs'], {'layout': 'center', 'width': 40, 'widthType': 'percentage'})
        self.assertEqual(resized['content'][0]['content'], node['content'])

    def test_names_and_sizes(self):
        import datetime
        from ticketdesk.core import attachments
        taken = {'log.txt'}
        now = datetime.datetime(2026, 11, 12, 10, 15, 30)
        self.assertEqual(attachments.unique_name('image.png', taken, now), 'image-20261112-101530.png')
        self.assertEqual(attachments.unique_name('image.png', taken, now), 'image-20261112-101530-2.png')
        self.assertEqual(attachments.unique_name('/tmp/log.txt', taken), 'log-2.txt')
        self.assertEqual(attachments.image_size(PNG_1x1), (640, 480))
        gif = b'GIF89a\x20\x03\x58\x02' + b'\x00' * 10
        self.assertEqual(attachments.image_size(gif), (800, 600))
        jpeg = (b'\xff\xd8\xff\xe0\x00\x10' + b'J' * 14 + b'\xff\xc0\x00\x11\x08\x02\x58\x03\x20' + b'\x00' * 12)
        self.assertEqual(attachments.image_size(jpeg), (800, 600))
        self.assertIsNone(attachments.image_size(b'nope'))

    def test_files_block(self):
        from ticketdesk.core import attachments
        blocks = attachments.files_block([{'filename': 'a.pdf', 'media_id': 'u1'}, {'filename': 'b.zip', 'media_id': None}])
        self.assertEqual(blocks[0]['type'], 'mediaGroup')
        self.assertEqual(blocks[0]['content'][0]['attrs']['id'], 'u1')
        self.assertIn('b.zip', blocks[1]['content'][0]['text'])


class UploadApiTests(ServerCase):
    def test_upload_multipart_and_media_id(self):
        recs = self.client.upload_attachments('ABC-1', [('a b".png', PNG_1x1, 'image/png')])
        self.assertEqual(recs[0]['filename'], "a b'.png")
        self.assertEqual(FakeJira.state['uploads'][0][2][:8], PNG_1x1[:8])
        self.assertEqual(self.client.media_file_id(recs[0]['id']), '0b8a1c2d-3e4f-4a5b-8c6d-000000000101')
        FakeJira.state['no_media_redirect'] = True
        self.assertIsNone(self.client.media_file_id(recs[0]['id']))

    def test_upload_is_not_retried_and_errors_surface(self):
        with self.assertRaises(JiraError) as cm:
            self.client.upload_attachments('ABC-1', [('toolarge.bin', b'x', None)])
        self.assertIn('too large', str(cm.exception))
        self.assertEqual(sum(1 for c in FakeJira.state['calls'] if c[0] == 'POST'), 1)


class CreateApiTests(ServerCase):
    def test_createmeta_paging_and_types(self):
        fl = self.client.create_fields('OPS', '10001')
        self.assertEqual(len(fl), 60)
        self.assertEqual([t['name'] for t in self.client.create_types('OPS')], ['Task', 'Sub-task'])

    def test_create_and_errors(self):
        self.assertEqual(self.client.create_issue({'summary': 'x', 'project': {'key': 'OPS'}})['key'], 'OPS-77')
        with self.assertRaises(JiraError) as cm:
            self.client.create_issue({'project': {'key': 'OPS'}})
        self.assertIn('summary', cm.exception.field_errors)

    def test_create_is_not_retried(self):
        FakeJira.state['calls'] = []
        with self.assertRaises(JiraError):
            self.client.create_issue({'project': {'key': 'OPS'}})
        self.assertEqual(sum(1 for c in FakeJira.state['calls'] if c[1] == '/rest/api/3/issue'), 1)

    def test_users_exclude_apps(self):
        self.assertEqual([u['accountId'] for u in self.client.find_users('y', 'OPS', assignable=True)], ['u1'])


class StoreViewsTests(ServerCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Cache(Path(self.tmp.name) / 'c.db')
        self.store = Store(Config(site=self.site, email='a@b.c', projects=['ABC', 'NOPE']), self.cache,
                           Worker(dispatch=direct_dispatch, max_workers=2))
        self.store.set_credentials('token')

    def tearDown(self):
        self.store.worker.shutdown(wait=True)
        self.tmp.cleanup()

    def collect(self, start, n=1):
        done, seen = threading.Event(), []

        def add(*a):
            seen.append(a[0] if len(a) == 1 else a)
            if len(seen) >= n:
                done.set()
        start(add)
        self.assertTrue(done.wait(5))
        return seen

    def test_projects_named_in_configured_order(self):
        seen = self.collect(lambda cb: self.store.projects(cb), 2)
        self.assertEqual(seen[0], [{'key': 'ABC', 'name': ''}, {'key': 'NOPE', 'name': ''}])  # immediately
        self.assertEqual(seen[1], [{'key': 'ABC', 'name': 'Alpha Bravo'}, {'key': 'NOPE', 'name': ''}])

    def test_other_boards_exclude_project_boards(self):
        seen = self.collect(lambda cb: self.store.other_boards([7], cb, cb))
        self.assertEqual([b['id'] for b in seen[0]], [9])

    def test_create_with_images_and_files(self):
        import base64
        from ticketdesk.core.adf_html import html_to_adf
        pending = {}
        desc = html_to_adf('<p>x</p><figure class="td-image" data-pending="t1" data-alt="image.png">'
                           '<img src="data:image/png;base64,' + base64.b64encode(PNG_1x1).decode() + '"></figure>', pending)
        tmp = Path(self.tmp.name) / 'notes.txt'
        tmp.write_text('hello')
        got = self.collect(lambda cb: self.store.create_issue(
            {'summary': 'New', 'project': {'key': 'OPS'}, 'description': desc}, lambda i, w: cb((i, w)), cb,
            pending=pending, files=[{'path': str(tmp)}, {'path': '/nonexistent/x.bin'}]))
        issue, warnings = got[0]
        self.assertEqual(issue['key'], 'OPS-77')
        # created without the image, then the description is completed with the uploaded one
        self.assertEqual([b['type'] for b in FakeJira.state['created']['description']['content']], ['paragraph'])
        final = FakeJira.state['edited']['description']['content'][1]['content'][0]['attrs']
        self.assertEqual(final['id'], '0b8a1c2d-3e4f-4a5b-8c6d-000000000101')
        self.assertTrue(final['alt'].startswith('image-'))
        self.assertEqual([u[1] for u in FakeJira.state['uploads']][1], 'notes.txt')
        self.assertTrue(any('x.bin could not be read' in w for w in warnings))

    def test_comment_with_files(self):
        tmp = Path(self.tmp.name) / 'log.txt'
        tmp.write_text('x')
        got = self.collect(lambda cb: self.store.add_comment(
            'ABC-1', {'type': 'doc', 'version': 1, 'content': [{'type': 'paragraph', 'content': [{'type': 'text', 'text': 'see'}]}]},
            lambda cs, w: cb((cs, w)), cb, files=[{'path': str(tmp)}]))
        self.assertEqual(got[0][1], [])
        body = FakeJira.state['comment']
        self.assertEqual([b['type'] for b in body['content']], ['paragraph', 'mediaGroup'])

    def test_create_types_drop_subtasks_and_create_reads_back(self):
        seen = self.collect(lambda cb: self.store.create_types('OPS', cb, cb))
        self.assertEqual([t['name'] for t in seen[0]], ['Task'])
        got = self.collect(lambda cb: self.store.create_issue({'summary': 'New', 'project': {'key': 'OPS'}},
                                                              lambda i, w: cb(i), cb))
        self.assertEqual(got[0]['key'], 'OPS-77')

    def test_notification_check(self):
        import time as _time
        self.store.config.projects = []
        first = self.collect(lambda cb: self.store.check_notifications(cb, cb))
        self.assertEqual(first[0], [])  # the first check only sets the starting point
        self.cache.put_meta('notify-last', int(_time.time() * 1000) - 600_000)
        now = _time.strftime('%Y-%m-%dT%H:%M:%S.000+0000', _time.gmtime())
        FakeJira.state['issues'] = [issue('ABC-1')]
        FakeJira.state['notify_comments'] = [{'id': '77', 'author': {'accountId': 'bob', 'displayName': 'Bob'},
                                              'created': now, 'updated': now, 'body': adf_mention('me-1')}]
        FakeJira.state['changelog'] = [{'id': str(i), 'created': '2020-01-01T00:00:00.000+0000', 'items': []} for i in range(150)]
        got = self.collect(lambda cb: self.store.check_notifications(cb, cb))
        self.assertEqual([e['id'] for e in got[0]], ['cm:77'])
        self.assertIn('ABC-1', self.store.mentioned)
        self.assertTrue(any('startAt=50' in c[1] for c in FakeJira.state['calls'] if c[0] == 'GET'))
        again = self.collect(lambda cb: self.store.check_notifications(cb, cb))
        self.assertEqual(again[0], [])  # already known

    def test_children_query(self):
        FakeJira.state['issues'] = [issue('ABC-2'), issue('ABC-3')]
        seen = self.collect(lambda cb: self.store.children('EP-1', cb, cb, cb))
        self.assertEqual([i['key'] for i in seen[0]], ['ABC-2', 'ABC-3'])
        self.assertEqual(FakeJira.state['calls'][-1][2]['jql'], 'parent = "EP-1" ORDER BY created ASC')

    def test_created_range_lists_more(self):
        import datetime
        FakeJira.state['issues'] = [issue(f'ABC-{i}') for i in range(250)]
        self.store.config.list_size = 50
        rng = (datetime.date(2026, 11, 2), datetime.date(2026, 11, 8))
        seen = self.collect(lambda cb: self.store.load_list('all', '', None, lambda *a: None, lambda i, j, t: cb(i), cb,
                                                            created=rng))
        self.assertEqual(len(seen[0]), search.RANGE_MAX)


class ConfigTests(unittest.TestCase):
    def test_roundtrip_and_helpers(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'cfg.json'
            c = Config(site='example.atlassian.net/', email='a@b.c', projects=['OPS'])
            c.site = normalize_site(c.site)
            c.add_recent('OPS-1')
            c.add_recent('OPS-2')
            c.add_recent('OPS-1')
            c.save(p)
            back = Config.load(p)
            self.assertEqual(back.site, 'https://example.atlassian.net')
            self.assertEqual(back.recent, ['OPS-1', 'OPS-2'])
        self.assertEqual(parse_projects('ops, web  pay'), ['OPS', 'WEB', 'PAY'])


if __name__ == '__main__':
    unittest.main()
