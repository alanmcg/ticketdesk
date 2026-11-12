# SPDX-License-Identifier: GPL-2.0-or-later
"""Data access for the UI: show cached data at once, then refresh from Jira in the
background ("stale while revalidate"). Writes go to Jira first and are re-read after."""

import hashlib
import logging
import os
import time

from . import boards as board_logic
from . import attachments, linking, media, notify, relations, search
from .api import JiraClient, JiraError, OfflineError
from .search import LIST_FIELDS

NOTIFY_FIELDS = ['summary', 'status', 'assignee', 'reporter', 'creator', 'created', 'description', 'watches']

log = logging.getLogger(__name__)


def _board(b):
    loc = b.get('location') or {}
    return {'id': b['id'], 'name': b.get('name') or f'Board {b["id"]}',
            'type': b.get('type') or 'kanban', 'project': loc.get('projectKey') or ''}


class Store:
    def __init__(self, config, cache, worker):
        self.config = config
        self.cache = cache
        self.worker = worker
        self.client = None
        self.me = cache.get_meta('myself') or {}
        self.mentioned = list(cache.get_meta('mentioned-keys') or []) if cache else []
        self._checking = False
        self.online = True
        self._online_listeners = []

    # ---------- connection ----------

    def set_credentials(self, token):
        self.client = JiraClient(self.config.site, self.config.email, token) if token else None

    def on_online_changed(self, callback):
        self._online_listeners.append(callback)

    def _mark(self, online):
        if online != self.online:
            self.online = online
            for cb in self._online_listeners:
                cb(online)

    def run(self, fn, *args, on_done=None, on_error=None):
        """Run a Jira call in the background; track whether Jira is reachable."""
        if self.client is None:
            if on_error:
                on_error(JiraError(0, 'Not connected: open Preferences to set up your Jira site'))
            return None

        def done(result):
            self._mark(True)
            if on_done:
                on_done(result)

        def failed(e):
            if isinstance(e, OfflineError):
                self._mark(False)
            elif isinstance(e, JiraError):
                self._mark(True)
            if on_error:
                on_error(e)
            else:
                log.warning('%s', e)
        return self.worker.run(fn, *args, on_done=done, on_error=failed)

    def ensure_me(self, on_done=None):
        if self.me.get('accountId'):
            if on_done:
                on_done(self.me)
            return

        def fetch():
            me = self.client.myself()
            self.cache.put_meta('myself', me)
            return me

        def got(me):
            self.me = me
            if on_done:
                on_done(me)
        self.run(fetch, on_done=got)

    # ---------- lists ----------

    def list_query(self, view, text, filter_jql=None, project=None, created=None):
        c = self.config
        show = {'todo': c.show_todo, 'progress': c.show_progress, 'done': c.show_done}
        return search.build(view, text, c.projects, show, c.sort, c.sort_desc, self.mentioned, filter_jql,
                            project=project, created=created)

    def load_list(self, view, text, filter_jql, on_cached, on_fresh, on_error, project=None, created=None):
        jql, title, fallback = self.list_query(view, text, filter_jql, project, created)
        keys, fetched = self.cache.get_list(jql)
        if keys is not None:
            on_cached(self.cache.get_issues(keys), fetched, jql, title)
        # With a created-date range (e.g. a weekly review) list everything in it, like the extension.
        limit = max(self.config.list_size, search.RANGE_MAX) if created else self.config.list_size

        def fetch():
            nonlocal jql
            for _ in range(5):
                try:
                    issues = self.client.search(jql, LIST_FIELDS, limit)
                except JiraError as e:
                    gone = search.missing_key(str(e)) if e.status == 400 else None
                    if gone and gone in self.mentioned:
                        # A remembered mention points at a deleted or moved ticket.
                        self.mentioned.remove(gone)
                        jql = self.list_query(view, text, filter_jql, project, created)[0]
                        continue
                    if e.status == 400 and fallback:
                        issues = [i for i in self.client.search(fallback[0], LIST_FIELDS, 100)
                                  if i['key'].startswith(fallback[1])][:limit]
                    else:
                        raise
                self.cache.put_issues(issues)
                self.cache.put_list(jql, [i['key'] for i in issues])
                return issues, jql
            raise JiraError(400, 'Could not build a valid query')

        def failed(e):
            if isinstance(e, OfflineError) and keys is None and text:
                local = self.cache.search_local(text)
                on_cached(local, None, jql, f'Offline results for “{text}”')
            on_error(e)
        self.run(fetch, on_done=lambda r: on_fresh(r[0], r[1], title), on_error=failed)
        return jql

    def recent(self):
        return self.cache.get_issues(self.config.recent)

    def projects(self, on_done):
        """[{'key', 'name'}] for the configured projects, in the configured order."""
        keys = list(self.config.projects)
        cached = {p['key']: p for p in self.cache.get_meta('projects') or []}
        on_done([cached.get(k) or {'key': k, 'name': ''} for k in keys])
        if not keys:
            return

        def fetch():
            found = {p['key']: {'key': p['key'], 'name': p.get('name') or ''} for p in self.client.projects(keys)}
            self.cache.put_meta('projects', list({**cached, **found}.values()))
            return [found.get(k) or {'key': k, 'name': ''} for k in keys]
        self.run(fetch, on_done=on_done, on_error=lambda e: log.info('projects: %s', e))

    def children(self, key, on_cached, on_fresh, on_error, limit=500):
        """Tickets whose parent is `key` (epic children, or sub-tasks), in creation order."""
        list_id = f'children:{key}'
        keys, _ = self.cache.get_list(list_id)
        if keys is not None:
            on_cached(self.cache.get_issues(keys))

        def fetch():
            issues = self.client.search(f'parent = {search.jql_string(key)} ORDER BY created ASC', LIST_FIELDS, limit)
            self.cache.put_issues(issues)
            self.cache.put_list(list_id, [i['key'] for i in issues])
            return issues
        self.run(fetch, on_done=on_fresh, on_error=on_error)

    # ---------- linking tickets ----------

    def _link_types(self):
        types = self.cache.get_meta('link-types', max_age=24 * 3600)
        if types is None:
            types = self.client.link_types()
            self.cache.put_meta('link-types', types)
        return types

    def link_kinds(self, on_done, on_error):
        """[(kind, label)] for linking on this site."""
        self.run(lambda: linking.kinds(self._link_types()), on_done=on_done, on_error=on_error)

    def find_tickets(self, text, exclude, on_done, on_error, limit=20):
        """Tickets for a key or words, most recently viewed first, as relations.brief dicts."""
        jql = linking.search_jql(text)

        def go():
            if not jql:
                return []
            try:
                issues = self.client.search(jql, ['summary', 'status', 'issuetype'], limit)
            except JiraError as e:
                if e.status == 400 and jql.startswith('key ='):
                    return []  # no such ticket (or not visible)
                raise
            return [relations.brief(i) for i in issues if i.get('key') != exclude]
        self.run(go, on_done=on_done, on_error=on_error)

    def link(self, this, kind, other, on_done, on_error):
        """Relate `this` to `other` (linking.KINDS); on_done(issue) with `this` as Jira has it now."""
        def go():
            types = self._link_types()
            action = linking.plan(kind, this, other, types)
            if action[0] == 'parent':
                _, child, parent = action
                self.client.edit_issue(child, {'parent': {'key': parent}})
                if child != this:
                    self.refetch(child)
                return self.refetch(this)
            body = action[1]
            self.client.create_link(body)
            issue = self.refetch(this)
            t = next(t for t in types if str(t['id']) == body['type']['id'])
            if not linking.symmetric(t):
                link_id, side = linking.find_link(issue['fields'], t['id'], other)
                if link_id and side != linking.expected_side(kind):
                    log.warning('Jira created the %s link %s/%s the other way round; correcting it',
                                t.get('name'), this, other)
                    self.client.delete_link(link_id)
                    self.client.create_link(linking.swapped(body))
                    issue = self.refetch(this)
            return issue
        self.run(go, on_done=on_done, on_error=on_error)

    def unlink(self, this, link_id, on_done, on_error):
        def go():
            self.client.delete_link(link_id)
            return self.refetch(this)
        self.run(go, on_done=on_done, on_error=on_error)

    def relink(self, this, body, on_done, on_error):
        """Make a removed link again (undo)."""
        def go():
            self.client.create_link(body)
            return self.refetch(this)
        self.run(go, on_done=on_done, on_error=on_error)

    # ---------- creating tickets ----------

    def create_projects(self, on_done):
        """Projects to create tickets in: the configured ones, or else the most active ones."""
        if self.config.projects:
            self.projects(on_done)
            return
        cached = self.cache.get_meta('create-projects')
        if cached is not None:
            on_done(cached)

        def fetch():
            out = [{'key': p['key'], 'name': p.get('name') or ''} for p in self.client.all_projects()]
            self.cache.put_meta('create-projects', out)
            return out
        self.run(fetch, on_done=on_done, on_error=lambda e: log.info('projects: %s', e))

    def create_types(self, project, on_done, on_error):
        """Creatable ticket types (sub-task types left out: they need a parent)."""
        name = f'create-types:{project}'
        cached = self.cache.get_meta(name)
        if cached is not None:
            on_done(cached)

        def fetch():
            types = [{'id': str(t['id']), 'name': t.get('name') or '', 'subtask': bool(t.get('subtask'))}
                     for t in self.client.create_types(project)]
            types = [t for t in types if not t['subtask']]
            self.cache.put_meta(name, types)
            return types
        self.run(fetch, on_done=lambda t: t != cached and on_done(t), on_error=on_error)

    def create_fields(self, project, type_id, on_done, on_error):
        name = f'create-fields:{project}:{type_id}'
        cached = self.cache.get_meta(name)
        if cached is not None:
            on_done(cached)

        def fetch():
            fields = self.client.create_fields(project, type_id)
            self.cache.put_meta(name, fields)
            return fields
        self.run(fetch, on_done=lambda f: f != cached and on_done(f), on_error=on_error)

    def create_issue(self, fields, on_done, on_error, pending=None, files=None):
        """Create a ticket, upload its images and files, put the images into the description,
        then read it back from Jira. on_done(issue, warnings). Once the ticket exists, later
        problems are warnings, not errors: the ticket is not created twice."""
        def go():
            warnings = []
            description = fields.get('description')
            first = dict(fields)
            if pending and description:
                stripped = attachments.without_pending(description)
                if stripped.get('content'):
                    first['description'] = stripped
                else:
                    first.pop('description', None)
            created = self.client.create_issue(first)
            key = created.get('key')
            if not key:
                raise JiraError(0, 'Jira did not return the new ticket')
            if pending or files:
                try:
                    images, _uploaded = self._upload(key, pending, files, warnings)
                    if pending and description:
                        final, _missing = attachments.resolve(description, images)
                        self.client.edit_issue(key, {'description': final})
                except JiraError as e:
                    warnings.append(f'{key} was created, but adding its images failed: {e}')
            try:
                issue = self.refetch(key)
            except JiraError:
                issue = {'key': key, 'fields': {}}
            return issue, warnings
        self.run(go, on_done=lambda r: on_done(*r), on_error=on_error)

    # ---------- notifications ----------

    def check_notifications(self, on_done, on_error=None):
        """Look for new activity (see core/notify.py). on_done(new_events), newest first.
        The first check only sets the starting point, so old activity is not reported."""
        if self._checking or not self.client:
            return
        self._checking = True
        cfg = self.config

        def go():
            if not (cfg.notify_mentions or cfg.notify_updates):
                return [], set()
            if not self.me.get('accountId'):
                self.me = self.client.myself()
                self.cache.put_meta('myself', self.me)
            me = self.me['accountId']
            started = int(time.time() * 1000)
            last = self.cache.get_meta('notify-last')
            if last is None:
                self.cache.put_meta('notify-last', started)
                return [], set()
            since = max(int(last) - notify.OVERLAP_MS, started - notify.CATCH_UP_MS)
            issues = {}
            for jql in notify.queries(cfg.notify_mentions, cfg.notify_updates, cfg.projects, since):
                for it in self.client.search(jql, NOTIFY_FIELDS, notify.ISSUE_CAP):
                    issues[it['key']] = it
            events = []
            for it in issues.values():
                comments = self.client.comments(it['key'])
                changelog = self.client.recent_changelog(it['key'])
                events.extend(notify.events_for(it, comments, changelog, me, since,
                                                cfg.notify_mentions, cfg.notify_updates))
            new = self.cache.add_notifications(events)
            self.cache.put_meta('notify-last', started)
            return new, {e['key'] for e in events if e['kind'] == 'mention'}

        def done(result):
            self._checking = False
            new, mentioned = result
            if mentioned:
                self.remember_mentions(mentioned)
            on_done(new)

        def failed(e):
            self._checking = False
            log.info('notification check: %s', e)
            if on_error:
                on_error(e)
        self.run(go, on_done=done, on_error=failed)

    def remember_mentions(self, keys):
        """Tickets you were mentioned in are part of Mine (shared with what the extension saw)."""
        known = list(self.mentioned)
        for k in keys:
            if k not in known:
                known.insert(0, k)
        self.mentioned = known[:200]
        self.cache.put_meta('mentioned-keys', self.mentioned)

    def notifications(self):
        return self.cache.notifications()

    def unread_notifications(self):
        return self.cache.unread_count()

    def mark_notifications_read(self, ids=None, key=None):
        self.cache.mark_notifications_read(ids, key)

    def mention_people(self, query, issue_key, project, on_done):
        """[{'id', 'name'}] for @mentions; an empty list if the search fails."""
        def go():
            return [{'id': u['accountId'], 'name': u.get('displayName') or u['accountId']}
                    for u in self.client.users_for_mention(query, issue_key, project)]
        self.run(go, on_done=on_done, on_error=lambda e: (log.info('mention search: %s', e), on_done([])))

    def find_users(self, query, project, assignable, on_done, on_error, issue_key=None):
        self.run(lambda: self.client.find_users(query, project, assignable, issue_key=issue_key),
                 on_done=on_done, on_error=on_error)

    def assign(self, key, account_id, on_done, on_error):
        """Assign (None: unassign); on_done(issue) with the ticket as Jira has it now."""
        def go():
            self.client.assign(key, account_id)
            return self.refetch(key)
        self.run(go, on_done=on_done, on_error=on_error)

    def favourite_filters(self, on_done):
        cached = self.cache.get_meta('filters')
        if cached is not None:
            on_done(cached)

        def fetch():
            filters = [{'id': f['id'], 'name': f['name'], 'jql': f.get('jql', '')}
                       for f in self.client.favourite_filters()]
            self.cache.put_meta('filters', filters)
            return filters
        self.run(fetch, on_done=on_done, on_error=lambda e: log.info('filters: %s', e))

    # ---------- one ticket ----------

    def load_issue(self, key, on_cached, on_issue, on_comments, on_error):
        cached = self.cache.get_issue(key)
        comments, _ = self.cache.get_comments(key)
        on_cached(cached, comments)

        def fetch_issue():
            it = self.client.issue(key)
            self.cache.put_issues([it], names=it.get('names') or {}, full=True)
            self._map_description_media(key, it.get('fields') or {})
            return self.cache.get_issue(key)

        def fetch_comments():
            cs = self.client.comments(key)
            cs = self._map_comment_media(key, cs)
            self.cache.put_comments(key, cs)
            return cs
        self.run(fetch_issue, on_done=on_issue, on_error=on_error)
        self.run(fetch_comments, on_done=on_comments, on_error=lambda e: log.info('comments %s: %s', key, e))

    # ---------- uploading images and files ----------

    def _upload(self, key, pending, files, warnings):
        """Upload images added in the editor (pending) and other files to a ticket.
        Returns ({token: {'media_id', 'filename'}}, [{'filename', 'media_id', 'id'}]).
        A file that fails is reported in `warnings`; the others still go up."""
        cached = self.cache.get_issue(key)
        taken = {a.get('filename') for a in ((cached or {}).get('fields') or {}).get('attachment') or []}
        images, uploaded_files = {}, []

        def send(name, data, mime):
            name = attachments.unique_name(name, taken)
            try:
                rec = (self.client.upload_attachments(key, [(name, data, mime)]) or [{}])[0]
            except JiraError as e:
                warnings.append(f'{name} was not uploaded: {e}')
                return None
            media_id = self.client.media_file_id(rec['id']) if rec.get('id') else None
            return {'id': rec.get('id'), 'filename': rec.get('filename') or name, 'media_id': media_id}

        for token, p in (pending or {}).items():
            info = send(p['name'], p['data'], p.get('mime') or attachments.guess_mime(p['name']))
            images[token] = info or {}
        for f in files or []:
            if f.get('path'):
                try:
                    with open(f['path'], 'rb') as fh:
                        data = fh.read()
                except OSError as e:
                    warnings.append(f'{os.path.basename(f["path"])} could not be read: {e.strerror}')
                    continue
                name = os.path.basename(f['path'])
            else:
                data, name = f['data'], f['name']
            info = send(name, data, f.get('mime') or attachments.guess_mime(name))
            if info:
                uploaded_files.append(info)
        missing = [i['filename'] for i in images.values() if i and not i.get('media_id')]
        if missing:
            warnings.append(f'Jira did not reveal how to show {", ".join(missing)} in the text; '
                            'the image is attached to the ticket and named in the text instead')
        return images, uploaded_files

    def attach_files(self, key, files, on_done, on_error):
        """Attach files to a ticket. on_done(issue, warnings) with the ticket re-read."""
        def go():
            warnings = []
            _images, uploaded = self._upload(key, None, files, warnings)
            if not uploaded:
                raise JiraError(0, '; '.join(warnings) or 'Nothing was attached')
            return self.refetch(key), warnings
        self.run(go, on_done=lambda r: on_done(*r), on_error=on_error)

    def add_comment(self, key, adf_doc, on_done, on_error, pending=None, files=None):
        """Post a comment. Images and files are uploaded to the ticket first and shown in the
        comment. on_done(comments, warnings)."""
        def post():
            warnings = []
            doc = adf_doc
            if pending or files:
                images, uploaded = self._upload(key, pending, files, warnings)
                doc, _missing = attachments.resolve(doc, images)
                doc = attachments.append_blocks(doc, attachments.files_block(uploaded))
                if not doc or not doc.get('content'):
                    raise JiraError(0, 'Nothing could be uploaded, so the comment was not posted: '
                                       + '; '.join(warnings))
            try:
                self.client.add_comment(key, doc)
            except JiraError as e:
                if pending or files:
                    raise JiraError(e.status, f'{e} (the files were already attached to {key})',
                                    e.messages, e.field_errors) from e
                raise
            cs = self.client.comments(key)  # re-read: show exactly what Jira stored
            cs = self._map_comment_media(key, cs)
            self.cache.put_comments(key, cs)
            try:
                self.refetch(key)  # the attachment list changed
            except JiraError:
                pass
            return cs, warnings
        self.run(post, on_done=lambda r: on_done(*r), on_error=on_error)

    def transitions(self, key, on_done, on_error):
        self.run(self.client.transitions, key, on_done=on_done, on_error=on_error)

    def do_transition(self, key, transition_id, fields, on_done, on_error):
        def go():
            self.client.do_transition(key, transition_id, fields)
            return self.refetch(key)
        self.run(go, on_done=on_done, on_error=on_error)

    def edit_meta(self, key, on_done, on_error):
        self.run(self.client.edit_meta, key, on_done=on_done, on_error=on_error)

    def edit_fields(self, key, fields, on_done, on_error):
        def go():
            self.client.edit_issue(key, fields)
            return self.refetch(key)
        self.run(go, on_done=on_done, on_error=on_error)

    def save_description(self, key, original, doc, force, on_saved, on_conflict, on_error, pending=None):
        """Save a description edited from `original`. Unless `force`, the description in Jira
        is checked first: if someone changed it meanwhile, on_conflict(current) is called
        instead of overwriting their change. New images are uploaded before saving.
        on_saved(issue, warnings)."""
        def go():
            if not force:
                current = self.client.issue_fields(key, ['description']).get('description')
                if current != original:
                    return ('conflict', current, None)
            warnings = []
            final = doc
            if pending:
                images, _ = self._upload(key, pending, None, warnings)
                final, _missing = attachments.resolve(doc, images)
            self.client.edit_issue(key, {'description': final})
            return ('saved', self.refetch(key), warnings)

        def done(result):
            kind, value, warnings = result
            if kind == 'saved':
                on_saved(value, warnings)
            else:
                on_conflict(value)
        self.run(go, on_done=done, on_error=on_error)

    def set_watching(self, key, on, on_done, on_error):
        def go():
            if not self.me.get('accountId'):
                self.me = self.client.myself()
                self.cache.put_meta('myself', self.me)
            self.client.watch(key, self.me['accountId'], on)
            return self.refetch(key)
        self.run(go, on_done=on_done, on_error=on_error)

    def refetch(self, key):
        it = self.client.issue(key)
        self.cache.put_issues([it], names=it.get('names') or {}, full=True)
        return self.cache.get_issue(key)

    # ---------- images embedded in rich text ----------

    def media_map(self, key):
        """{media_id: attachment_id} for the ticket's description and comments."""
        return {**(self.cache.get_meta(f'media-comments:{key}') or {}),
                **(self.cache.get_meta(f'media-desc:{key}') or {})}

    def _map_description_media(self, key, fields):
        found = media.nodes(fields.get('description'))
        known = self.cache.get_meta(f'media-desc:{key}') or {}
        if not media.needs_html(found, fields.get('attachment') or [], known):
            return
        try:
            mapping = media.html_mapping(found, self.client.rendered_description(key))
        except JiraError as e:
            log.info('rendered description %s: %s', key, e)
            return
        self.cache.put_meta(f'media-desc:{key}', mapping)

    def _map_comment_media(self, key, comments):
        """Comments with embedded files that cannot be matched by name are re-read with
        Jira's HTML rendering, which names the attachment ids. Returns the comments."""
        cached = self.cache.get_issue(key)
        atts = (cached or {}).get('fields', {}).get('attachment') if cached else None
        known = self.cache.get_meta(f'media-comments:{key}') or {}
        per_comment = [media.nodes(c.get('body')) for c in comments]
        if not any(media.needs_html(n, atts, known) for n in per_comment if n):
            return comments
        try:
            rendered = self.client.comments(key, rendered=True)
        except JiraError as e:
            log.info('rendered comments %s: %s', key, e)
            return comments
        html = {c.get('id'): c.get('renderedBody') or '' for c in rendered}
        mapping = dict(known)
        for c, found in zip(comments, per_comment):
            if found and c.get('id') in html:
                mapping.update(media.html_mapping(found, html[c['id']]))
        self.cache.put_meta(f'media-comments:{key}', mapping)
        return comments

    def image(self, att, on_done, on_error=None):
        """Path of an embedded image: the full file unless it is very large (then the thumbnail)."""
        if (att.get('size') or 0) > 20 * 1024 * 1024:
            self.thumbnail(att, on_done, on_error)
        else:
            self.attachment_file(att, on_done, on_error or (lambda e: log.info('image: %s', e)))

    def external_image(self, url, on_done, on_error=None):
        name = 'ext-' + hashlib.sha256(url.encode()).hexdigest()[:32]
        path = self.cache.get_blob(name)
        if path:
            on_done(path)
            return

        def fetch():
            data, ctype = self.client.external_image(url)
            if not ctype.startswith('image/'):
                raise JiraError(0, f'Not an image ({ctype})')
            return self.cache.put_blob(name, data)
        self.run(fetch, on_done=on_done, on_error=on_error or (lambda e: log.info('external image: %s', e)))

    # ---------- boards ----------

    def boards(self, on_done):
        """Boards of the configured projects (other boards: other_boards())."""
        if not self.config.projects:
            on_done([])
            return
        cached = self.cache.get_meta('boards')
        if cached is not None:
            on_done(cached)

        def fetch():
            seen, out = set(), []
            for project in self.config.projects:
                for b in self.client.boards(project):
                    if b.get('id') in seen:
                        continue
                    seen.add(b.get('id'))
                    out.append(_board(b))
            out.sort(key=lambda b: (b['project'], b['name'].lower()))
            self.cache.put_meta('boards', out)
            return out
        self.run(fetch, on_done=on_done, on_error=lambda e: log.info('boards: %s', e))

    def other_boards(self, exclude_ids, on_done, on_error):
        """All boards visible to you except `exclude_ids` (fetched when first needed)."""
        exclude = set(exclude_ids)
        cached = self.cache.get_meta('boards-all')
        if cached is not None:
            on_done([b for b in cached if b['id'] not in exclude])

        def fetch():
            out = [_board(b) for b in self.client.boards(None, limit=1000)]
            out.sort(key=lambda b: (b['project'], b['name'].lower()))
            self.cache.put_meta('boards-all', out)
            return [b for b in out if b['id'] not in exclude]
        self.run(fetch, on_done=on_done, on_error=on_error)

    def load_board(self, board, on_cached, on_fresh, on_error, limit=500):
        """Board state: {'config', 'columns', 'issues', 'total', 'sprints', 'scope'}."""
        bid = board['id']
        state = self.cache.get_meta(f'board:{bid}')
        keys, _ = self.cache.get_list(f'board:{bid}')
        if state is not None and keys is not None:
            on_cached({**state, 'columns': board_logic.columns(state['config']), 'issues': self.cache.get_issues(keys)})

        def fetch():
            config = self.client.board_config(bid)
            sprints, issues, total, scope = [], [], 0, ''
            if board.get('type') in ('scrum', 'simple'):
                try:
                    sprints = self.client.active_sprints(bid)
                except JiraError as e:
                    if board.get('type') == 'scrum' or e.status not in (400, 404):
                        raise
                    sprints = []  # team-managed board without sprints
            if sprints:
                for sp in sprints:
                    if len(issues) >= limit:
                        break
                    got, n = self.client.board_issues(bid, LIST_FIELDS, limit - len(issues), sprint_id=sp['id'])
                    issues.extend(got)
                    total += n
                scope = ', '.join(sp.get('name') or '' for sp in sprints)
            elif board.get('type') == 'scrum':
                scope = 'no active sprint'
            else:
                issues, total = self.client.board_issues(bid, LIST_FIELDS, limit, jql=board_logic.kanban_jql(config))
                scope = f'done in the last {board_logic.KANBAN_DONE_DAYS} days'
            self.cache.put_issues(issues)
            self.cache.put_list(f'board:{bid}', [i['key'] for i in issues])
            st = {'config': config, 'total': total, 'scope': scope,
                  'sprints': [{'id': s['id'], 'name': s.get('name')} for s in sprints]}
            self.cache.put_meta(f'board:{bid}', st)
            return {**st, 'columns': board_logic.columns(config), 'issues': issues}
        self.run(fetch, on_done=on_fresh, on_error=on_error)

    def rank(self, key, rank, field_id, on_done, on_error):
        where, other = rank

        def go():
            self.client.rank([key], before=other if where == 'before' else None,
                             after=other if where == 'after' else None, field_id=field_id)
        self.run(go, on_done=on_done, on_error=on_error)

    # ---------- attachments ----------

    def thumbnail(self, att, on_done, on_error=None):
        name = f'thumb-{att["id"]}'
        path = self.cache.get_blob(name)
        if path:
            on_done(path)
            return

        def fetch():
            try:
                data, _ = self.client.attachment_thumbnail(att['id'])
            except JiraError as e:
                if e.status != 404:
                    raise
                data, _ = self.client.attachment_content(att['id'])  # no thumbnail: use the image
            return self.cache.put_blob(name, data)
        self.run(fetch, on_done=on_done, on_error=on_error or (lambda e: log.info('thumbnail: %s', e)))

    def attachment_file(self, att, on_done, on_error):
        name = f'att-{att["id"]}-{att.get("filename", "file")}'
        path = self.cache.get_blob(name)
        if path:
            on_done(path)
            return

        def fetch():
            data, _ = self.client.attachment_content(att['id'])
            return self.cache.put_blob(name, data)
        self.run(fetch, on_done=on_done, on_error=on_error)
