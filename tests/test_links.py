# SPDX-License-Identifier: GPL-2.0-or-later
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ticketdesk import nativehost
from ticketdesk.core import links

ROOT = Path(__file__).resolve().parent.parent
CASES = json.loads((Path(__file__).parent / 'link_cases.json').read_text())
HOST = links.host_of(CASES['site'])


class TicketKey(unittest.TestCase):
    def test_tickets(self):
        for url, key in CASES['tickets']:
            self.assertEqual(links.ticket_key(url, HOST), key, url)

    def test_not_tickets(self):
        for url in CASES['not_tickets']:
            self.assertIsNone(links.ticket_key(url, HOST), url)

    def test_host_of(self):
        for site in ('acme.example.net', 'https://acme.example.net/', ' https://ACME.example.net '):
            self.assertEqual(links.host_of(site), 'acme.example.net', site)
        self.assertEqual(links.host_of(''), '')
        self.assertIsNone(links.ticket_key('https://acme.example.net/browse/OPS-12', links.host_of('')))


def frame(message):
    data = json.dumps(message).encode()
    return struct.pack('=I', len(data)) + data


def unframe(raw):
    out = []
    while raw:
        (n,) = struct.unpack('=I', raw[:4])
        out.append(json.loads(raw[4:4 + n]))
        raw = raw[4 + n:]
    return out


class NativeHost(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {'XDG_CONFIG_HOME': self.tmp.name + '/config', 'XDG_DATA_HOME': self.tmp.name + '/data',
                    'HOME': self.tmp.name}
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        from ticketdesk.core.config import Config
        Config(site='https://acme.example.net').save()

    def run_serve(self, *messages, raw=b''):
        inp, out = io.BytesIO(b''.join(frame(m) for m in messages) + raw), io.BytesIO()
        nativehost.serve(inp, out)
        return unframe(out.getvalue())

    def test_site(self):
        (reply,) = self.run_serve({'get': 'site'})
        self.assertEqual(reply['site'], 'https://acme.example.net')
        self.assertTrue(reply['ok'])

    def test_open(self):
        with mock.patch('ticketdesk.launch.open_in_app', return_value=True) as opener:
            replies = self.run_serve({'open': 'OPS-12'}, {'open': 'not a key'}, {'open': 'OPS-12; rm -rf'}, ['x'])
        opener.assert_called_once_with('OPS-12')
        self.assertEqual([r['ok'] for r in replies], [True, False, False, False])

    def test_bad_input(self):
        self.assertEqual(self.run_serve(raw=b'\x05\x00'), [])  # truncated length: just stop
        (reply,) = self.run_serve(raw=struct.pack('=I', 3) + b'{x}')
        self.assertFalse(reply['ok'])
        (reply,) = self.run_serve(raw=struct.pack('=I', nativehost.MAX_MESSAGE + 1))
        self.assertFalse(reply['ok'])

    def test_as_the_browser_runs_it(self):
        """A separate process with the arguments Firefox passes; nothing but messages on stdout."""
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        proc = subprocess.run([sys.executable, '-m', 'ticketdesk.nativehost', '/x/ticketdesk.json', '@ticketdesk-links'],
                              input=frame({'get': 'site'}) + frame({'get': 'nothing'}),
                              capture_output=True, env=env, timeout=30, check=True)
        replies = unframe(proc.stdout)
        self.assertEqual(replies[0]['site'], 'https://acme.example.net')
        self.assertFalse(replies[1]['ok'])

    def test_started_app_does_not_hold_the_pipes(self):
        """Ticket Desk keeps running after the host exits; the browser's pipes must close anyway."""
        code = ('import subprocess, sys, ticketdesk.launch as l, ticketdesk.nativehost as h\n'
                'l.open_in_app = lambda key: bool(subprocess.Popen(["sleep", "20"]))\n'
                'sys.exit(h.main(["/x/ticketdesk.json", "@ticketdesk-links"]))\n')
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        proc = subprocess.run([sys.executable, '-c', code], input=frame({'open': 'OPS-12'}),
                              capture_output=True, env=env, timeout=10, check=True)  # times out if sleep holds them
        self.assertEqual(unframe(proc.stdout), [{'ok': True}])

    def test_install_user(self):
        path, launcher = nativehost.install_user()
        self.assertEqual(path, Path(self.tmp.name) / '.mozilla/native-messaging-hosts/ticketdesk.json')
        data = json.loads(path.read_text())
        self.assertEqual(data['name'], 'ticketdesk')
        self.assertEqual(data['path'], str(launcher))
        self.assertEqual(data['allowed_extensions'], ['@ticketdesk-links'])
        self.assertTrue(os.access(launcher, os.X_OK))
        # the launcher really runs the host from this checkout
        proc = subprocess.run([str(launcher), str(path), '@ticketdesk-links'], input=frame({'get': 'site'}),
                              capture_output=True, timeout=30, check=True)
        self.assertEqual(unframe(proc.stdout)[0]['site'], 'https://acme.example.net')
        self.assertEqual(sorted(nativehost.uninstall_user()), sorted([path, launcher]))
        self.assertFalse(path.exists())

    def test_extension_and_host_agree(self):
        manifest = json.loads((ROOT / 'browser/firefox/manifest.json').read_text())
        self.assertEqual(manifest['browser_specific_settings']['gecko']['id'], nativehost.EXTENSION_ID)
        packaged = json.loads((ROOT / 'data/ticketdesk-native-host.json.in').read_text())
        self.assertEqual(packaged['name'], nativehost.NAME)
        self.assertEqual(packaged['allowed_extensions'], [nativehost.EXTENSION_ID])
        background = (ROOT / 'browser/firefox/background.js').read_text()
        self.assertIn(f"const HOST = '{nativehost.NAME}';", background)


if __name__ == '__main__':
    unittest.main()
