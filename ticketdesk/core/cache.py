# SPDX-License-Identifier: GPL-2.0-or-later
"""Offline cache in SQLite (WAL mode).

Every thread gets its own connection, so the UI thread can read while workers write.
Cached data is always shown as cached; the UI re-validates against Jira.
"""

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from . import adf

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS issues(
    key TEXT PRIMARY KEY, project TEXT, updated TEXT,
    fields TEXT NOT NULL, names TEXT, full INTEGER NOT NULL DEFAULT 0, fetched REAL NOT NULL);
CREATE TABLE IF NOT EXISTS comments(key TEXT PRIMARY KEY, data TEXT NOT NULL, fetched REAL NOT NULL);
CREATE TABLE IF NOT EXISTS lists(id TEXT PRIMARY KEY, keys TEXT NOT NULL, fetched REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meta(name TEXT PRIMARY KEY, data TEXT NOT NULL, fetched REAL NOT NULL);
CREATE TABLE IF NOT EXISTS notifications(
    id TEXT PRIMARY KEY, key TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT,
    author TEXT, time INTEGER NOT NULL, read INTEGER NOT NULL DEFAULT 0, added REAL NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(key UNINDEXED, summary, body, tokenize='unicode61');
"""


class Cache:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.blob_dir = self.path.parent / 'files'
        self.blob_dir.mkdir(exist_ok=True)
        self._local = threading.local()
        db = self._db()
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            # Unknown layout from another version: the cache is disposable, start over.
            db.close()
            self._local.db = None
            self.path.unlink(missing_ok=True)
            db = self._db()
        db.executescript(_SCHEMA)
        db.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
        db.commit()

    def _db(self):
        db = getattr(self._local, 'db', None)
        if db is None:
            db = sqlite3.connect(self.path, timeout=10)
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=NORMAL')
            self._local.db = db
        return db

    # ---------- issues ----------

    def put_issues(self, issues, names=None, full=False):
        """Store search results (partial fields) or full issues. A partial update never
        downgrades cached full details unless the issue changed in Jira meanwhile."""
        db = self._db()
        now = time.time()
        for it in issues:
            key, fields = it['key'], it.get('fields') or {}
            row = db.execute('SELECT fields, updated, full, names FROM issues WHERE key=?', (key,)).fetchone()
            old_names = row[3] if row else None
            if full:
                merged, is_full = fields, True
                names_json = json.dumps(names) if names else old_names
            elif row:
                # Partial fields from a list: keep cached details only if the issue is unchanged.
                merged = {**json.loads(row[0]), **fields}
                is_full = bool(row[2]) and row[1] == fields.get('updated')
                names_json = old_names
            else:
                merged, is_full, names_json = fields, False, None
            db.execute('INSERT OR REPLACE INTO issues(key, project, updated, fields, names, full, fetched) '
                       'VALUES (?,?,?,?,?,?,?)',
                       (key, key.split('-')[0], merged.get('updated'), json.dumps(merged),
                        names_json, int(is_full), now))
            db.execute('DELETE FROM fts WHERE key=?', (key,))
            db.execute('INSERT INTO fts(key, summary, body) VALUES (?,?,?)',
                       (key, merged.get('summary') or '', adf.to_text(merged.get('description'))))
        db.commit()

    def get_issue(self, key):
        row = self._db().execute('SELECT fields, names, full, fetched FROM issues WHERE key=?', (key,)).fetchone()
        if not row:
            return None
        return {'key': key, 'fields': json.loads(row[0]), 'names': json.loads(row[1]) if row[1] else {},
                'full': bool(row[2]), 'fetched': row[3]}

    def get_issues(self, keys):
        if not keys:
            return []
        db = self._db()
        found = {}
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            q = f'SELECT key, fields FROM issues WHERE key IN ({",".join("?" * len(chunk))})'
            for k, f in db.execute(q, chunk):
                found[k] = {'key': k, 'fields': json.loads(f)}
        return [found[k] for k in keys if k in found]

    def search_local(self, text, limit=100):
        """Offline full-text search over cached summaries and descriptions."""
        terms = [t for t in ''.join(c if c.isalnum() else ' ' for c in text).split() if t]
        if not terms:
            return []
        query = ' '.join(f'"{t}"*' for t in terms)
        rows = self._db().execute('SELECT key FROM fts WHERE fts MATCH ? ORDER BY rank LIMIT ?', (query, limit)).fetchall()
        return self.get_issues([r[0] for r in rows])

    # ---------- comments, lists, meta ----------

    def put_comments(self, key, comments):
        db = self._db()
        db.execute('INSERT OR REPLACE INTO comments(key, data, fetched) VALUES (?,?,?)',
                   (key, json.dumps(comments), time.time()))
        db.commit()

    def get_comments(self, key):
        row = self._db().execute('SELECT data, fetched FROM comments WHERE key=?', (key,)).fetchone()
        return (json.loads(row[0]), row[1]) if row else (None, None)

    def put_list(self, list_id, keys):
        db = self._db()
        db.execute('INSERT OR REPLACE INTO lists(id, keys, fetched) VALUES (?,?,?)',
                   (list_id, json.dumps(keys), time.time()))
        db.commit()

    def get_list(self, list_id):
        row = self._db().execute('SELECT keys, fetched FROM lists WHERE id=?', (list_id,)).fetchone()
        return (json.loads(row[0]), row[1]) if row else (None, None)

    def put_meta(self, name, data):
        db = self._db()
        db.execute('INSERT OR REPLACE INTO meta(name, data, fetched) VALUES (?,?,?)',
                   (name, json.dumps(data), time.time()))
        db.commit()

    def get_meta(self, name, max_age=None):
        row = self._db().execute('SELECT data, fetched FROM meta WHERE name=?', (name,)).fetchone()
        if not row or (max_age is not None and time.time() - row[1] > max_age):
            return None
        return json.loads(row[0])

    # ---------- files (thumbnails, attachments) ----------

    def blob_path(self, name):
        safe = ''.join(c if c.isalnum() or c in '-_.' else '_' for c in name)
        return self.blob_dir / safe

    def put_blob(self, name, data):
        path = self.blob_path(name)
        tmp = path.with_suffix(path.suffix + '.tmp')
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return path

    def get_blob(self, name):
        path = self.blob_path(name)
        return path if path.exists() else None

    # ---------- maintenance ----------

    # ---------- notifications (kept when the cache is cleared: they are history) ----------

    def add_notifications(self, events, keep=500):
        """Store events; returns those not seen before (newest first)."""
        db = self._db()
        new = []
        for e in sorted(events, key=lambda e: e['time']):
            cur = db.execute('INSERT OR IGNORE INTO notifications(id, key, kind, title, body, author, time, added) '
                             'VALUES (?,?,?,?,?,?,?,?)',
                             (e['id'], e['key'], e['kind'], e['title'], e.get('body') or '', e.get('author') or '',
                              int(e['time']), time.time()))
            if cur.rowcount:
                new.append(e)
        db.execute('DELETE FROM notifications WHERE id NOT IN '
                   '(SELECT id FROM notifications ORDER BY time DESC LIMIT ?)', (keep,))
        db.commit()
        return list(reversed(new))

    def notifications(self, limit=200):
        rows = self._db().execute('SELECT id, key, kind, title, body, author, time, read FROM notifications '
                                  'ORDER BY time DESC LIMIT ?', (limit,)).fetchall()
        return [{'id': r[0], 'key': r[1], 'kind': r[2], 'title': r[3], 'body': r[4], 'author': r[5],
                 'time': r[6], 'read': bool(r[7])} for r in rows]

    def mark_notifications_read(self, ids=None, key=None):
        """Mark given ids, all notifications of a ticket, or (neither given) everything as read."""
        db = self._db()
        if ids:
            db.executemany('UPDATE notifications SET read=1 WHERE id=?', [(i,) for i in ids])
        elif key:
            db.execute('UPDATE notifications SET read=1 WHERE key=?', (key,))
        else:
            db.execute('UPDATE notifications SET read=1')
        db.commit()

    def unread_count(self):
        return self._db().execute('SELECT COUNT(*) FROM notifications WHERE read=0').fetchone()[0]

    def newest_notification_time(self):
        return self._db().execute('SELECT MAX(time) FROM notifications').fetchone()[0] or 0

    def stats(self):
        db = self._db()
        issues = db.execute('SELECT COUNT(*) FROM issues').fetchone()[0]
        size = self.path.stat().st_size if self.path.exists() else 0
        wal = self.path.with_name(self.path.name + '-wal')
        size += wal.stat().st_size if wal.exists() else 0
        files = sum(p.stat().st_size for p in self.blob_dir.iterdir() if p.is_file())
        return {'issues': issues, 'db_bytes': size, 'file_bytes': files}

    def clear(self):
        db = self._db()
        for table in ('issues', 'comments', 'lists', 'meta', 'fts'):
            db.execute(f'DELETE FROM {table}')
        db.commit()
        db.execute('VACUUM')
        for p in self.blob_dir.iterdir():
            if p.is_file():
                p.unlink(missing_ok=True)
