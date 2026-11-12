# SPDX-License-Identifier: GPL-2.0-or-later
"""Assigning and linking tickets, against a small fake Jira that keeps links and parents."""

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ticketdesk.core import linking, relations
from ticketdesk.core.cache import Cache
from ticketdesk.core.config import Config
from ticketdesk.core.store import Store
from ticketdesk.core.worker import Worker, direct_dispatch

TYPES = [{'id': '10000', 'name': 'Blocks', 'inward': 'is blocked by', 'outward': 'blocks'},
         {'id': '10001', 'name': 'Cloners', 'inward': 'is cloned by', 'outward': 'clones'},
         {'id': '10002', 'name': 'Duplicate', 'inward': 'is duplicated by', 'outward': 'duplicates'},
         {'id': '10003', 'name': 'Relates', 'inward': 'relates to', 'outward': 'relates to'}]


class Jira(BaseHTTPRequestHandler):
    """Links are stored as Jira documents them: POST inwardIssue A, outwardIssue B makes
    "A <outward> B"; A then shows B under outwardIssue. With `reverse`, it does the opposite
    (for testing the repair)."""
    protocol_version = 'HTTP/1.1'
    state = {}

    def log_message(self, *a):
        pass

    def _send(self, status, body=None):
        data = json.dumps(body).encode() if body is not None else b''
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return json.loads(self.rfile.read(n)) if n else None

    def _issue(self, key):
        s = self.state
        links = []
        for lid, (tid, a, b) in s['links'].items():  # a <outward> b
            t = next(t for t in TYPES if t['id'] == tid)
            if key == a:
                links.append({'id': lid, 'type': t, 'outwardIssue': {'key': b, 'fields': {'summary': b}}})
            elif key == b:
                links.append({'id': lid, 'type': t, 'inwardIssue': {'key': a, 'fields': {'summary': a}}})
        parent = s['parents'].get(key)
        assignee = s['assignees'].get(key)
        return {'key': key, 'names': {}, 'fields': {
            'summary': f'Summary of {key}', 'updated': '2026-11-12T10:00:00.000+0100', 'issuelinks': links,
            'parent': {'key': parent, 'fields': {'summary': parent}} if parent else None,
            'assignee': {'accountId': assignee, 'displayName': assignee} if assignee else None}}

    def do_GET(self):
        self.state['calls'].append(('GET', self.path))
        if self.path == '/rest/api/3/issueLinkType':
            return self._send(200, {'issueLinkTypes': TYPES})
        if self.path.startswith('/rest/api/3/issue/'):
            return self._send(200, self._issue(self.path.split('/')[5].split('?')[0]))
        return self._send(404, {'errorMessages': ['no']})

    def do_POST(self):
        body = self._body()
        self.state['calls'].append(('POST', self.path, body))
        s = self.state
        if self.path == '/rest/api/3/issueLink':
            s['next'] = s.get('next', 20000) + 1
            a, b = body['inwardIssue']['key'], body['outwardIssue']['key']
            s['links'][str(s['next'])] = (body['type']['id'], *((b, a) if s.get('reverse') else (a, b)))
            return self._send(201)
        if self.path == '/rest/api/3/search/jql':
            jql = body['jql']
            if jql.startswith('key = "NOPE-'):
                return self._send(400, {'errorMessages': ["An issue with key 'NOPE-1' does not exist for field 'key'."]})
            s['jql'] = jql
            return self._send(200, {'issues': [
                {'key': k, 'fields': {'summary': f'About {k}', 'status': {'name': 'Open', 'statusCategory': {'key': 'new'}}}}
                for k in ('ABC-1', 'ABC-2', 'ABC-3')]})
        return self._send(404, {})

    def do_PUT(self):
        body = self._body()
        self.state['calls'].append(('PUT', self.path, body))
        key = self.path.split('/')[5]
        if self.path.endswith('/assignee'):
            if 'accountId' not in body:
                return self._send(400, {'errorMessages': ['accountId missing']})
            self.state['assignees'][key] = body['accountId']
            return self._send(204)
        if 'parent' in body.get('fields', {}):
            if body['fields']['parent']['key'] == key:
                return self._send(400, {'errors': {'parent': 'A ticket cannot be its own parent'}})
            self.state['parents'][key] = body['fields']['parent']['key']
            return self._send(204)
        return self._send(404, {})

    def do_DELETE(self):
        self.state['calls'].append(('DELETE', self.path))
        lid = self.path.rsplit('/', 1)[1]
        if self.state['links'].pop(lid, None) is None:
            return self._send(404, {'errorMessages': ['No link']})
        return self._send(204)


class LinkingLogic(unittest.TestCase):
    def test_kinds_follow_the_site(self):
        self.assertEqual([k for k, _ in linking.kinds(TYPES)],
                         ['relates', 'blocks', 'blocked', 'duplicates', 'duplicated', 'child', 'parent'])
        only_blocks = [TYPES[0]]
        self.assertEqual([k for k, _ in linking.kinds(only_blocks)], ['blocks', 'blocked', 'child', 'parent'])
        renamed = [{'id': '5', 'name': 'Blocking', 'inward': 'is blocked by', 'outward': 'blocks'}]
        self.assertEqual(linking.link_type(renamed, 'blocks')['id'], '5')

    def test_plan(self):
        body = linking.plan('blocks', 'ABC-1', 'ABC-2', TYPES)[1]
        self.assertEqual((body['inwardIssue']['key'], body['outwardIssue']['key'], body['type']['id']),
                         ('ABC-1', 'ABC-2', '10000'))
        body = linking.plan('blocked', 'ABC-1', 'ABC-2', TYPES)[1]
        self.assertEqual((body['inwardIssue']['key'], body['outwardIssue']['key']), ('ABC-2', 'ABC-1'))
        self.assertEqual(linking.plan('child', 'ABC-1', 'ABC-9', TYPES), ('parent', 'ABC-1', 'ABC-9'))
        self.assertEqual(linking.plan('parent', 'ABC-1', 'ABC-5', TYPES), ('parent', 'ABC-5', 'ABC-1'))
        with self.assertRaises(ValueError):
            linking.plan('duplicates', 'ABC-1', 'ABC-2', [TYPES[0]])

    def test_recreate_and_search(self):
        link = {'id': '7', 'type': TYPES[0], 'inwardIssue': {'key': 'ABC-4'}}  # shown on ABC-1: is blocked by ABC-4
        self.assertEqual(linking.recreate(link, 'ABC-1'),
                         {'type': {'id': '10000'}, 'inwardIssue': {'key': 'ABC-4'}, 'outwardIssue': {'key': 'ABC-1'}})
        self.assertEqual(linking.search_jql(' abc-12 '), 'key = "ABC-12"')
        self.assertEqual(linking.search_jql('login "fails" (prod)'),
                         'text ~ "login fails prod*" ORDER BY lastViewed DESC, updated DESC')
        self.assertIsNone(linking.search_jql('  "*" '))


class StoreLinking(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Jira)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.site = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Jira.state = {'calls': [], 'links': {}, 'parents': {}, 'assignees': {'ABC-1': 'u1'}}
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Config(site=self.site, email='a@b.c'), Cache(Path(self.tmp.name) / 'c.db'),
                           Worker(dispatch=direct_dispatch, max_workers=2))
        self.store.set_credentials('token')

    def tearDown(self):
        self.store.worker.shutdown(wait=True)
        self.tmp.cleanup()

    def call(self, method, *args):
        done, out = threading.Event(), {}
        getattr(self.store, method)(*args, lambda r: (out.update(ok=r), done.set()),
                                    lambda e: (out.update(err=e), done.set()))
        self.assertTrue(done.wait(5), 'timed out')
        return out

    def phrases(self, issue):
        return [(p, [b['key'] for b in items]) for p, items in relations.links(issue['fields'])]

    def test_assign_and_unassign(self):
        out = self.call('assign', 'ABC-1', 'u2')
        self.assertEqual(out['ok']['fields']['assignee']['accountId'], 'u2')
        self.assertIn(('PUT', '/rest/api/3/issue/ABC-1/assignee', {'accountId': 'u2'}), Jira.state['calls'])
        out = self.call('assign', 'ABC-1', None)
        self.assertIsNone(out['ok']['fields']['assignee'])
        self.assertIn(('PUT', '/rest/api/3/issue/ABC-1/assignee', {'accountId': None}), Jira.state['calls'])

    def test_links_read_as_chosen(self):
        for kind, expect in (('blocks', ('blocks', 'ABC-2')), ('blocked', ('is blocked by', 'ABC-3')),
                             ('duplicates', ('duplicates', 'ABC-4')), ('relates', ('relates to', 'ABC-5'))):
            issue = self.call('link', 'ABC-1', kind, expect[1])['ok']
            self.assertIn((expect[0], [expect[1]]), self.phrases(issue), kind)
        self.assertFalse(any(c[0] == 'DELETE' for c in Jira.state['calls']))  # nothing needed repair

    def test_reversed_link_is_repaired(self):
        Jira.state['reverse'] = True
        issue = self.call('link', 'ABC-1', 'blocks', 'ABC-2')['ok']
        self.assertEqual(self.phrases(issue), [('blocks', ['ABC-2'])])
        self.assertEqual(len(Jira.state['links']), 1)
        issue = self.call('link', 'ABC-1', 'relates', 'ABC-3')['ok']  # same both ways: left alone
        self.assertEqual(sum(1 for c in Jira.state['calls'] if c[0] == 'DELETE'), 1)

    def test_parent_and_child(self):
        issue = self.call('link', 'ABC-1', 'child', 'ABC-9')['ok']
        self.assertEqual(relations.parent(issue['fields'])['key'], 'ABC-9')
        self.call('link', 'ABC-1', 'parent', 'ABC-5')
        self.assertEqual(Jira.state['parents']['ABC-5'], 'ABC-1')
        err = self.call('link', 'ABC-1', 'child', 'ABC-1')['err']
        self.assertIn('own parent', str(err))

    def test_unlink_and_undo(self):
        issue = self.call('link', 'ABC-1', 'blocked', 'ABC-4')['ok']
        link = relations.links(issue['fields'])[0][1][0]['link']
        issue = self.call('unlink', 'ABC-1', link['id'])['ok']
        self.assertEqual(self.phrases(issue), [])
        issue = self.call('relink', 'ABC-1', linking.recreate(link, 'ABC-1'))['ok']
        self.assertEqual(self.phrases(issue), [('is blocked by', ['ABC-4'])])

    def test_find_tickets(self):
        out = self.call('find_tickets', 'login', 'ABC-2')
        self.assertEqual([b['key'] for b in out['ok']], ['ABC-1', 'ABC-3'])
        self.assertTrue(Jira.state['jql'].startswith('text ~ "login*"'))
        self.assertEqual(self.call('find_tickets', 'nope-1', 'ABC-2')['ok'], [])
        self.assertEqual(self.call('find_tickets', '"', 'ABC-2')['ok'], [])


if __name__ == '__main__':
    unittest.main()
