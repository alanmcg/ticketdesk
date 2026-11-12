# SPDX-License-Identifier: GPL-2.0-or-later
"""Ticket view: details (collapsed under the title), description, child and linked tickets,
attachments, comments, and status changes. Back/forward is handled by the window."""

import logging
import os

from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from ..core import adf
from ..core import fields as F
from ..core import linking, media, relations
from ..core.api import OfflineError
from ..core.config import DEFAULT_SHOWN_FIELDS
from .adf_view import AdfView
from . import rich_editor
from .attachment_list import AttachmentList
from .dialogs import ImageViewer
from .field_inputs import PersonPopover
from .link_dialog import LinkDialog
from .transitions import TransitionFlow
from .util import clear, full_time, human_size, is_image, label, open_file, open_uri, relative, status_category

log = logging.getLogger(__name__)
CHILD_PREVIEW = 8   # children listed in the ticket; all of them are one click away in the list


def _section(title, child, suffix=None):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    head = Gtk.Box(spacing=6)
    h = label(title, css=('heading',))
    h.set_hexpand(True)
    head.append(h)
    if suffix:
        head.append(suffix)
    box.append(head)
    box.append(child)
    return box


class IssueView(Gtk.Box):
    """Content of the ticket pane. `window` gives access to the store, toasts and online state."""

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.store = window.store
        self.key = None
        self.issue = None
        self.comments = None
        self._media_map = {}
        self._serial = 0
        self._children_serial = -1
        self._restore_scroll = None

        # ----- header bar -----
        self.header = Adw.HeaderBar()
        self.title = Adw.WindowTitle()
        self.header.set_title_widget(self.title)
        self.status_button = Gtk.MenuButton(always_show_arrow=True, tooltip_text='Change Status')
        self.status_button.add_css_class('status-button')
        self.status_popover = Gtk.Popover()
        self.status_popover.add_css_class('menu')
        self.status_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=['navigation-sidebar'])
        self.status_list.connect('row-activated', self._transition_row)
        pop_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, width_request=260)
        pop_box.append(label('Move To', css=('heading', 'dim-label'), xalign=0.0))
        pop_box.append(self.status_list)
        self.status_popover.set_child(pop_box)
        self.status_popover.connect('show', lambda *_: self._load_transitions())
        self.status_button.set_popover(self.status_popover)
        self.header.pack_start(self.status_button)

        self.watch_button = Gtk.ToggleButton(icon_name='view-reveal-symbolic', tooltip_text='Watch')
        self.watch_handler = self.watch_button.connect('toggled', self._watch_toggled)
        browser = Gtk.Button(icon_name='web-browser-symbolic', tooltip_text='Open in Browser')
        browser.connect('clicked', lambda *_: self.key and self.store.client and open_uri(self, self.store.client.browse_url(self.key)))
        copy = Gtk.Button(icon_name='edit-copy-symbolic', tooltip_text='Copy Link')
        copy.connect('clicked', self._copy_link)
        refresh = Gtk.Button(icon_name='view-refresh-symbolic', tooltip_text='Refresh (F5)')
        refresh.connect('clicked', lambda *_: self.show(self.key))
        self.close_button = Gtk.Button(icon_name='window-close-symbolic', tooltip_text='Close Ticket',
                                       visible=False)
        self.close_button.connect('clicked', lambda *_: self.window.close_ticket())
        for b in (self.close_button, browser, copy, self.watch_button, refresh):
            self.header.pack_end(b)

        self.toolbar = Adw.ToolbarView()
        self.toolbar.add_top_bar(self.header)
        self.append(self.toolbar)
        self.toolbar.set_vexpand(True)

        # ----- body -----
        self.stack = Gtk.Stack()
        self.stack.add_named(Adw.StatusPage(title='No Ticket Selected', description='Select a ticket from the list',
                                            icon_name='view-list-bullet-symbolic'), 'empty')
        spinner = Adw.Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, width_request=32, height_request=32)
        self.stack.add_named(spinner, 'loading')
        self.error_page = Adw.StatusPage(icon_name='dialog-warning-symbolic')
        self.stack.add_named(self.error_page, 'error')

        self.scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=18, margin_bottom=24,
                       margin_start=18, margin_end=18)
        self.crumbs = Gtk.Box(spacing=2, css_classes=['breadcrumb'])
        body.append(self.crumbs)
        self.summary = label(css=('title-2',), wrap=True, selectable=True)
        body.append(self.summary)
        self.stale = label('', css=('dim-label', 'caption'))
        body.append(self.stale)
        body.append(self._details_area())
        self.main_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22, hexpand=True)
        body.append(self.main_col)
        self.scroller.set_child(body)
        self.stack.add_named(self.scroller, 'issue')

        self.desc_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.att_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.comment_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=['boxed-list'])
        self.edit_desc_button = Gtk.Button(icon_name='document-edit-symbolic', tooltip_text='Edit Description',
                                           css_classes=['flat'], valign=Gtk.Align.CENTER)
        self.edit_desc_button.connect('clicked', lambda *_: self._edit_description())
        self.main_col.append(_section('Description', self.desc_box, self.edit_desc_button))
        self._desc_editor = None
        self._drafts = {}
        self.children_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=['boxed-list'])
        self.children_list.connect('row-activated', self._related_activated)
        self.children_info = label('', css=('dim-label', 'caption'))
        self.children_bar = Gtk.LevelBar(min_value=0, max_value=1, width_request=80, valign=Gtk.Align.CENTER)
        show_all = Gtk.Button(label='Show in List', css_classes=['flat'], tooltip_text='List all child tickets')
        show_all.connect('clicked', lambda *_: self.key and self.window.show_children(self.key))
        suffix = Gtk.Box(spacing=8)
        suffix.append(self.children_info)
        suffix.append(self.children_bar)
        suffix.append(show_all)
        self.children_section = _section('Child Tickets', self.children_list, suffix)
        self.main_col.append(self.children_section)
        self.links_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.link_button = Gtk.Button(icon_name='insert-link-symbolic', tooltip_text='Link Ticket…',
                                      css_classes=['flat'], valign=Gtk.Align.CENTER)
        self.link_button.connect('clicked', lambda *_: self._open_link_dialog())
        self.links_section = _section('Linked Tickets', self.links_box, self.link_button)
        self.main_col.append(self.links_section)
        self.attach_button = Gtk.Button(child=Adw.ButtonContent(icon_name='mail-attachment-symbolic', label='Attach Files…'),
                                        css_classes=['flat'], valign=Gtk.Align.CENTER,
                                        tooltip_text='Attach files to this ticket (or drop them into a comment)')
        self.attach_button.connect('clicked', lambda *_: self._choose_attachments())
        self.att_section = _section('Attachments', self.att_box, self.attach_button)
        self.main_col.append(self.att_section)
        self.main_col.append(_section('Comments', self._comments_area()))


        self.toolbar.set_content(self.stack)
        self.stack.set_visible_child_name('empty')
        self._set_header_sensitive(False)

    # ----- public -----

    def show(self, key, scroll=None):
        """Show a ticket; `scroll` restores a previous scroll position (back/forward)."""
        if not key:
            return
        self._restore_scroll = scroll
        changed = key != self.key
        self.key = key
        self._serial += 1
        serial = self._serial
        if changed:
            self.issue = None
            self.comments = None
            self.title.set_title(key)
            self.title.set_subtitle('')
            self.stack.set_visible_child_name('loading')
            self.scroller.get_vadjustment().set_value(0)
        self._switch_composer(key)

        def cached(issue, comments):
            if serial != self._serial:
                return
            if issue:
                self._render_issue(issue, from_cache=True)
            if comments is not None:
                self._render_comments(comments)

        def fresh(issue):
            if serial == self._serial:
                self._render_issue(issue, from_cache=False)

        def fresh_comments(comments):
            if serial == self._serial:
                self._render_comments(comments)

        def failed(e):
            if serial != self._serial:
                return
            if self.issue:
                self.window.toast(f'Showing cached copy: {e}' if isinstance(e, OfflineError) else str(e))
                return
            self.error_page.set_title('Offline' if isinstance(e, OfflineError) else 'Could Not Load Ticket')
            self.error_page.set_description(GLib.markup_escape_text(str(e)))
            self.stack.set_visible_child_name('error')
        self.store.load_issue(key, cached, fresh, fresh_comments, failed)

    def set_closable(self, closable):
        self.close_button.set_visible(closable)

    def scroll_position(self):
        return self.scroller.get_vadjustment().get_value()

    def clear(self):
        if self._desc_editor is not None and self._desc_editor.edited:
            self._drafts[self._desc_editor.key] = self._desc_editor.draft()
        self._desc_editor = None
        self.key = None
        self.issue = None
        self.comments = None
        self._serial += 1
        self.stack.set_visible_child_name('empty')
        self.title.set_title('')
        self.title.set_subtitle('')
        self._set_header_sensitive(False)

    def set_online(self, online):
        self.post_button.set_sensitive(online)
        self.composer.set_sensitive(online)
        self.status_button.set_sensitive(online and self.issue is not None)
        self.watch_button.set_sensitive(online and self.issue is not None)
        self.link_button.set_sensitive(online and self.issue is not None)
        self._rerender_details()  # the assignee can only be changed online

    # ----- rendering -----

    def _set_header_sensitive(self, on):
        for w in (self.status_button, self.watch_button):
            w.set_sensitive(on and self.store.online)

    def _render_issue(self, issue, from_cache):
        adj = self.scroller.get_vadjustment()
        keep = adj.get_value() if self.issue and self.issue['key'] == issue['key'] else 0
        self.issue = issue
        f = issue['fields']
        names = issue.get('names') or {}
        project = (f.get('project') or {}).get('name') or issue['key'].split('-')[0]
        itype = (f.get('issuetype') or {}).get('name') or ''
        freshness = 'cached' if from_cache else 'up to date'
        self.title.set_title(issue['key'])
        self.title.set_subtitle(' · '.join(x for x in (project, itype, freshness) if x))
        self.summary.set_text(f.get('summary') or '')

        status = (f.get('status') or {}).get('name') or 'Status'
        self.status_button.set_label(status)
        for c in ('todo', 'progress', 'done'):
            self.status_button.remove_css_class(c)
        self.status_button.add_css_class(status_category(f))

        self.watch_button.handler_block(self.watch_handler)
        self.watch_button.set_active(bool((f.get('watches') or {}).get('isWatching')))
        self.watch_button.handler_unblock(self.watch_handler)
        self._set_header_sensitive(True)

        if from_cache and not issue.get('full'):
            self.stale.set_text('Showing the list version of this ticket; loading details…')
            self.stale.set_visible(True)
        else:
            self.stale.set_visible(False)

        self._media_map = self.store.media_map(issue['key'])
        if self._desc_editor is not None and self._desc_editor.key == issue['key']:
            pass  # being edited: keep the editor (a save or cancel re-renders)
        else:
            if self._desc_editor is not None and self._desc_editor.edited:
                self._drafts[self._desc_editor.key] = self._desc_editor.draft()  # left without saving
            self._desc_editor = None
            self._render_description(f)
        self.edit_desc_button.set_visible(self._desc_editor is None)
        # editing from the cached copy is safe: saving checks Jira for changes first
        self.edit_desc_button.set_sensitive(self.store.online and bool(issue.get('full')))

        self._render_crumbs(f)

        self._render_relations(issue)
        self._render_attachments(f.get('attachment') or [])
        self._render_details(f, names)
        if self.comments is not None:
            self._render_comments(self.comments)  # embedded images depend on the attachment list
        self.stack.set_visible_child_name('issue')
        if self._restore_scroll is not None:
            keep = self._restore_scroll
            if not from_cache:
                self._restore_scroll = None
        if keep:
            GLib.idle_add(lambda: (adj.set_value(keep), GLib.SOURCE_REMOVE)[1])

    # ----- description -----

    def _render_description(self, f):
        clear(self.desc_box)
        blocks = adf.to_blocks(f.get('description'))
        if blocks:
            view = AdfView(blocks, on_link=self._link, media_widget=self._media_widget)
            view.add_css_class('card')
            view.add_css_class('adf-card')
            self.desc_box.append(view)
        else:
            self.desc_box.append(label('No description', css=('dim-label',)))

    def _edit_description(self):
        if not self.issue or not self.issue.get('full'):
            return
        key = self.key
        original = self.issue['fields'].get('description')
        editor = rich_editor.create(placeholder='Description', auto_height=True, min_height=200,
                                    resolve_image=self._resolve_image, find_people=self._find_people)
        editor.key = key
        editor.original = original
        editor.set_adf(original)
        editor.edited = False
        if key in self._drafts:
            editor.restore_draft(self._drafts.pop(key))
            editor.edited = True
            self.window.toast('Your unsaved changes to this description were restored')
        editor.connect('changed', lambda e: setattr(e, 'edited', True) if getattr(e, 'ready_seen', False)
                       else setattr(e, 'ready_seen', True))
        editor.connect('notice', lambda _e, text: self.window.toast(text))
        editor.connect('files', lambda _e, files: self._attach(files))  # dropped files: attach to the ticket
        editor.connect('submit', lambda *_: self._save_description(editor))
        cancel = Gtk.Button(label='Cancel')
        cancel.connect('clicked', lambda *_: self._end_edit())
        save = Gtk.Button(label='Save', css_classes=['suggested-action'], tooltip_text='Save (Ctrl+Enter)')
        save.connect('clicked', lambda *_: self._save_description(editor))
        editor.save_button = save
        bar = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        bar.append(cancel)
        bar.append(save)
        clear(self.desc_box)
        self.desc_box.append(editor)
        self.desc_box.append(bar)
        self._desc_editor = editor
        self.edit_desc_button.set_visible(False)
        editor.grab_focus()

    def _end_edit(self, issue=None):
        self._desc_editor = None
        if issue is not None and issue.get('key') == self.key:
            self._render_issue(issue, from_cache=False)
        elif self.issue:
            self._render_description(self.issue['fields'])
            self.edit_desc_button.set_visible(True)

    def _save_description(self, editor, force=False):
        if not self.store.online:
            self.window.toast('Offline: the description cannot be saved now')
            return
        editor.save_button.set_sensitive(False)
        key = editor.key

        def got(doc, pending):
            def saved(issue, warnings):
                for w in warnings:
                    self.window.toast(w)
                self.window.toast(f'Description of {key} saved')
                if self._desc_editor is editor:
                    self._end_edit(issue)
                self.window.issue_changed(issue)

            def conflict(current):
                editor.save_button.set_sensitive(True)
                dlg = Adw.AlertDialog(heading='Description Changed in Jira',
                                      body=f'Someone changed the description of {key} while you were editing it. '
                                           'Saving replaces their version with yours.')
                dlg.add_response('cancel', 'Keep Editing')
                dlg.add_response('overwrite', 'Replace Their Version')
                dlg.set_response_appearance('overwrite', Adw.ResponseAppearance.DESTRUCTIVE)
                dlg.set_close_response('cancel')
                dlg.connect('response', lambda _d, r: r == 'overwrite' and self._save_description(editor, force=True))
                dlg.present(self.get_root())

            def failed(e):
                editor.save_button.set_sensitive(True)
                self.window.toast(f'Could not save the description: {e}')
            self.store.save_description(key, editor.original, doc, force, saved, conflict, failed, pending=pending)
        editor.fetch_content(got)

    # ----- parent, children, links -----

    def _render_crumbs(self, f):
        """Parent chain above the summary, e.g. Initiative › Epic › Story (parents known
        from the cache are included; the direct parent always is)."""
        clear(self.crumbs)
        chain, seen = [], set()
        p = relations.parent(f)
        while p and p['key'] not in seen and len(chain) < 4:
            chain.insert(0, p)
            seen.add(p['key'])
            cached = self.store.cache.get_issue(p['key'])
            p = relations.parent(cached['fields']) if cached else None
        self.crumbs.set_visible(bool(chain))
        for i, b in enumerate(chain):
            if i:
                self.crumbs.append(label('›', css=('dim-label',)))
            text = f'{b["key"]} {b["summary"]}'.strip()
            lbl = label(text, ellipsize=Pango.EllipsizeMode.END)
            lbl.set_max_width_chars(36)
            btn = Gtk.Button(child=lbl, css_classes=['flat'], tooltip_text=f'{b["type"]} {text}'.strip())
            btn.connect('clicked', lambda _b, k=b['key']: self.window.open_related(k))
            self.crumbs.append(btn)

    def _related_row(self, b, removable=False):
        row = Gtk.ListBoxRow()
        row.key = b['key']
        box = Gtk.Box(spacing=8, css_classes=['related-row'])
        box.append(label(b['key'], css=('monospace', 'issue-key', 'dim-label')))
        summary = label(b['summary'], ellipsize=Pango.EllipsizeMode.END)
        summary.set_hexpand(True)
        summary.set_tooltip_text(b['summary'])
        box.append(summary)
        if b['status']:
            box.append(label(b['status'], css=('tag', b['category'])))
        if removable and b.get('link', {}).get('id'):
            remove = Gtk.Button(icon_name='list-remove-symbolic', tooltip_text='Remove Link',
                                css_classes=['flat', 'circular'], valign=Gtk.Align.CENTER)
            remove.set_sensitive(self.store.online)
            remove.connect('clicked', lambda _b, link=b['link']: self._remove_link(link))
            box.append(remove)
        row.set_child(box)
        row.update_property([Gtk.AccessibleProperty.LABEL], [f'{b["key"]}: {b["summary"]}, {b["status"]}'])
        return row

    def _related_activated(self, _list, row):
        if getattr(row, 'show_all', False):
            self.window.show_children(self.key)
        elif getattr(row, 'key', None):
            self.window.open_related(row.key)

    def _render_relations(self, issue):
        f = issue['fields']
        key = issue['key']
        # Linked tickets, grouped by how they are linked ("blocks", "is blocked by", ...)
        clear(self.links_box)
        groups = relations.links(f)
        self.links_box.set_visible(bool(groups))
        self.link_button.set_sensitive(self.store.online)
        for phrase, items in groups:
            head = label(phrase[:1].upper() + phrase[1:], css=('dim-label', 'caption', 'subsection'))
            self.links_box.append(head)
            lst = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=['boxed-list'])
            lst.connect('row-activated', self._related_activated)
            for b in items:
                lst.append(self._related_row(b, removable=True))
            self.links_box.append(lst)

        # Children: sub-tasks are part of the ticket; an epic's tickets need a query (once per show)
        if not relations.has_child_query(f):
            self._set_children(relations.subtasks(f))
            return
        if self._children_serial == self._serial:
            return
        self._children_serial = serial = self._serial
        self._set_children(None)

        def got(issues):
            if serial == self._serial and self.key == key:
                self._set_children([relations.brief(i) for i in issues])

        def failed(e):
            if serial == self._serial and self.key == key and self.children_list.get_row_at_index(0) is not None \
                    and getattr(self.children_list.get_row_at_index(0), 'loading', False):
                self._set_children([], error=str(e))
        self.store.children(key, got, got, failed)

    def _set_children(self, children, error=None):
        clear(self.children_list)
        if children is None:
            self.children_section.set_visible(True)
            row = Gtk.ListBoxRow(child=label('Loading…', css=('dim-label',)), activatable=False)
            row.get_child().set_margin_top(8)
            row.get_child().set_margin_bottom(8)
            row.get_child().set_margin_start(12)
            row.loading = True
            self.children_list.append(row)
            self.children_info.set_text('')
            self.children_bar.set_visible(False)
            return
        if error:
            self.children_section.set_visible(True)
            self.children_list.append(Gtk.ListBoxRow(child=label(f'Could not load: {error}', css=('dim-label',), wrap=True),
                                                     activatable=False))
            return
        self.children_section.set_visible(bool(children))
        if not children:
            return
        counts = relations.summarize(children)
        n = len(children)
        self.children_info.set_text(f'{counts["done"]} of {n} done')
        self.children_bar.set_visible(True)
        self.children_bar.set_value(counts['done'] / n)
        for b in relations.ordered(children)[:CHILD_PREVIEW]:
            self.children_list.append(self._related_row(b))
        if n > CHILD_PREVIEW:
            row = Gtk.ListBoxRow()
            row.show_all = True
            more = label(f'Show all {n} in the list', css=('accent',), xalign=0.5)
            more.set_margin_top(8)
            more.set_margin_bottom(8)
            row.set_child(more)
            self.children_list.append(row)

    # ----- linking -----

    def _open_link_dialog(self):
        if not self.issue:
            return
        if not self.store.online:
            self.window.toast('Offline: tickets cannot be linked now')
            return
        key = self.key
        LinkDialog(self.store, key, lambda issue: self._changed_here(key, issue), self.window.toast).present(self.get_root())

    def _changed_here(self, key, issue):
        """A change made from this view is done: show the ticket as Jira has it now."""
        if self.key == key and issue:
            self._children_serial = -1  # parent/child changes: load the children again
            self._render_issue(issue, from_cache=False)
        self.window.issue_changed(issue)

    def _remove_link(self, link):
        key = self.key
        other = (link.get('outwardIssue') or link.get('inwardIssue') or {}).get('key', '')
        again = linking.recreate(link, key)

        def undo():
            self.store.relink(key, again, lambda issue: self._changed_here(key, issue),
                              lambda e: self.window.toast(f'Could not restore the link: {e}'))

        def done(issue):
            self._changed_here(key, issue)
            toast = Adw.Toast(title=GLib.markup_escape_text(f'Link to {other} removed'), button_label='Undo', timeout=6)
            toast.connect('button-clicked', lambda *_: undo())
            self.window.toasts.add_toast(toast)
        self.store.unlink(key, link['id'], done, lambda e: self.window.toast(f'Could not remove the link: {e}'))

    # ----- assignee -----

    def _assignee_tile(self, box, name):
        """The Assignee value in Details: a button that opens a person search."""
        pop = PersonPopover(self.store, lambda account_id, who: self._assign(account_id, who),
                            issue_key=self.key, assignable=True, allow_none=True)
        button = Gtk.MenuButton(child=box, popover=pop, css_classes=['flat', 'detail-tile'],
                                tooltip_text=f'Change {name.lower()}', sensitive=self.store.online)
        return button

    def _assign(self, account_id, who):
        if not self.issue:
            return
        current = (self.issue['fields'].get('assignee') or {}).get('accountId')
        if account_id == current:
            return
        key = self.key

        def done(issue):
            self.window.toast(f'{key} assigned to {who}' if account_id else f'{key} unassigned')
            self._changed_here(key, issue)
        self.store.assign(key, account_id, done, lambda e: self.window.toast(f'Could not assign {key}: {e}'))

    # ----- details -----

    def _details_area(self):
        """Collapsible details under the title. Collapsed it shows the chosen fields' values
        on one line; expanded, a grid of fields that wraps to the pane's width."""
        cfg = self.window.app.config
        self.details_line = label('', css=('dim-label',), ellipsize=Pango.EllipsizeMode.END)
        self.details_line.set_hexpand(True)
        head = Gtk.Box(spacing=8)
        head.append(label('Details', css=('heading',)))
        head.append(self.details_line)
        self.details_flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                        min_children_per_line=1, max_children_per_line=4,
                                        column_spacing=18, row_spacing=12, margin_top=10)
        self.details_expander = Gtk.Expander(label_widget=head, child=self.details_flow,
                                             expanded=cfg.details_expanded, hexpand=True)
        self.details_expander.connect('notify::expanded', self._details_toggled)
        self.fields_button = Gtk.MenuButton(icon_name='emblem-system-symbolic', tooltip_text='Choose Fields',
                                            css_classes=['flat'], valign=Gtk.Align.START)
        self.fields_popover = Gtk.Popover()
        self.fields_popover.connect('show', lambda *_: self._fill_fields_popover())
        self.fields_button.set_popover(self.fields_popover)
        box = Gtk.Box(spacing=6, css_classes=['details-area'])
        box.append(self.details_expander)
        box.append(self.fields_button)
        return box

    def _details_toggled(self, expander, _pspec):
        self.details_line.set_visible(not expander.get_expanded())
        self.window.app.config.details_expanded = expander.get_expanded()

    def _render_details(self, f, names):
        cfg = self.window.app.config
        shown = set(cfg.shown_fields)
        rows = [r for r in F.detail_rows(f, names) if r[0] in shown]
        self.details_line.set_text(' · '.join(F.clip(text, 60)[0] for _fid, _name, text in rows))
        self.details_line.set_visible(not self.details_expander.get_expanded())
        clear(self.details_flow)
        if not rows:
            self.details_flow.append(label('No fields chosen: use the cog button', css=('dim-label',)))
            return
        for fid, name, text in rows:
            shown_text, clipped = F.clip(text)
            editable = fid == 'assignee'
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.append(label(name, css=('dim-label', 'caption'), wrap=True))
            value = label(shown_text, wrap=True, selectable=not clipped and not editable)
            value.set_max_width_chars(30)  # bounds the natural width, so values never widen the pane
            if editable:
                line = Gtk.Box(spacing=4)
                line.append(value)
                line.append(Gtk.Image(icon_name='pan-down-symbolic', css_classes=['dim-label']))
                box.append(line)
                self.details_flow.append(self._assignee_tile(box, name))
                continue
            box.append(value)
            if clipped:
                tile = Gtk.Button(child=box, css_classes=['flat', 'detail-tile'], tooltip_text='Show the full value')
                tile.connect('clicked', lambda _b, n=name, t=text: self._show_full_value(n, t))
            else:
                tile = box
            self.details_flow.append(tile)

    def _show_full_value(self, name, text):
        dlg = Adw.Dialog(title=name, content_width=560, content_height=440)
        header = Adw.HeaderBar()
        copy = Gtk.Button(icon_name='edit-copy-symbolic', tooltip_text='Copy')
        copy.connect('clicked', lambda b: (b.get_clipboard().set_text(text), self.window.toast('Copied')))
        header.pack_start(copy)
        value = label(text, wrap=True, selectable=True)
        value.set_valign(Gtk.Align.START)
        for side in ('top', 'bottom', 'start', 'end'):
            getattr(value, f'set_margin_{side}')(18)
        view = Adw.ToolbarView(content=Gtk.ScrolledWindow(child=value, hscrollbar_policy=Gtk.PolicyType.NEVER))
        view.add_top_bar(header)
        dlg.set_child(view)
        dlg.present(self.get_root())

    def _fill_fields_popover(self):
        """Every field this ticket has, with a check box: checked fields are shown in Details
        (on all tickets)."""
        cfg = self.window.app.config
        f = (self.issue or {}).get('fields') or {}
        rows = F.detail_rows(f, (self.issue or {}).get('names') or {})
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, width_request=300)
        box.append(label('Shown Fields', css=('heading',)))
        search = Gtk.SearchEntry(placeholder_text='Find field')
        box.append(search)
        lst = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=['navigation-sidebar'])
        for fid, name, _text in sorted(rows, key=lambda r: (r[0] not in cfg.shown_fields, r[1].lower())):
            check = Gtk.CheckButton(label=name, active=fid in cfg.shown_fields)
            check.connect('toggled', self._field_toggled, fid)
            row = Gtk.ListBoxRow(child=check, activatable=False)
            row.name = name.lower()
            lst.append(row)
        lst.set_filter_func(lambda row: search.get_text().lower() in row.name)
        search.connect('search-changed', lambda *_: lst.invalidate_filter())
        scroller = Gtk.ScrolledWindow(child=lst, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      propagate_natural_height=True, max_content_height=380)
        box.append(scroller)
        reset = Gtk.Button(label='Default Fields')
        reset.connect('clicked', self._default_fields)
        box.append(reset)
        self.fields_popover.set_child(box)

    def _field_toggled(self, check, fid):
        cfg = self.window.app.config
        shown = [x for x in cfg.shown_fields if x != fid]
        if check.get_active():
            shown.append(fid)
        cfg.shown_fields = shown
        cfg.save()
        self._rerender_details()

    def _default_fields(self, _btn):
        cfg = self.window.app.config
        cfg.shown_fields = list(DEFAULT_SHOWN_FIELDS)
        cfg.save()
        self.fields_popover.popdown()
        self._rerender_details()

    def _rerender_details(self):
        if self.issue:
            self._render_details(self.issue['fields'], self.issue.get('names') or {})

    def _attachments(self):
        return (self.issue or {}).get('fields', {}).get('attachment') or []

    def _choose_attachments(self):
        dialog = Gtk.FileDialog(title='Attach Files', modal=True)

        def done(d, result):
            try:
                files = d.open_multiple_finish(result)
            except GLib.Error:
                return
            paths = [files.get_item(i).get_path() for i in range(files.get_n_items())]
            self._attach([{'path': p} for p in paths if p])
        dialog.open_multiple(self.get_root(), None, done)

    def _attach(self, files):
        if not files or not self.key:
            return
        if not self.store.online:
            self.window.toast('Offline: files cannot be attached now')
            return
        key = self.key
        self.attach_button.set_sensitive(False)
        names = ', '.join(f.get('name') or os.path.basename(f.get('path') or '') for f in files)
        self.window.toast(f'Attaching {names}…')

        def done(issue, warnings):
            self.attach_button.set_sensitive(True)
            for w in warnings:
                self.window.toast(w)
            self.window.toast(f'Attached to {key}')
            if self.key == key:
                self._render_issue(issue, from_cache=False)

        def failed(e):
            self.attach_button.set_sensitive(True)
            self.window.toast(f'Not attached: {e}')
        self.store.attach_files(key, files, done, failed)

    def _render_attachments(self, atts):
        clear(self.att_box)
        self.attach_button.set_sensitive(self.store.online)
        if not atts:
            self.att_box.append(label('No attachments', css=('dim-label',)))
        images = [a for a in atts if is_image(a)]
        files = [a for a in atts if not is_image(a)]
        if images:
            flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=12, row_spacing=12,
                               homogeneous=True, max_children_per_line=8, min_children_per_line=1)
            for i, att in enumerate(images):
                flow.append(self._thumb_button(att, images, i, 168, 108))
            self.att_box.append(flow)
        if files:
            lst = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=['boxed-list'])
            for att in files:
                row = Adw.ActionRow(title=att.get('filename', 'file'), use_markup=False,
                                    subtitle=f'{human_size(att.get("size") or 0)} · {relative(att.get("created"))}')
                row.add_prefix(Gtk.Image.new_from_icon_name('text-x-generic-symbolic'))
                btn = Gtk.Button(label='Open', valign=Gtk.Align.CENTER)
                btn.connect('clicked', self._open_file, att)
                row.add_suffix(btn)
                lst.append(row)
            self.att_box.append(lst)

    def _thumb_button(self, att, images, index, w, h):
        pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True, width_request=w, height_request=h)
        pic.add_css_class('thumb')
        pic.set_alternative_text(att.get('filename'))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(pic)
        box.append(label(att.get('filename', ''), css=('caption',), ellipsize=Pango.EllipsizeMode.MIDDLE))
        btn = Gtk.Button(child=box, css_classes=['flat', 'thumb-button'], tooltip_text=att.get('filename'))
        btn.connect('clicked', lambda *_: ImageViewer(self.store, images, index, self.get_root()).present(self.get_root()))

        def loaded(path):
            try:
                pic.set_paintable(Gdk.Texture.new_from_filename(str(path)))
            except GLib.Error as e:
                log.info('thumbnail %s: %s', att.get('filename'), e)
        self.store.thumbnail(att, loaded)
        return btn

    def _media_widget(self, block):
        """A file embedded in rich text: the image itself, or a button for other files."""
        if block.get('media_type') == 'external':
            url = block.get('url') or ''
            return self._inline_image(None, block, url) if url.startswith('https://') else \
                AdfView.placeholder('image-x-generic-symbolic', f'Image from {url or "an unknown address"} (not loaded)')
        atts = self._attachments()
        att = media.lookup(block.get('id'), block.get('alt'), self._media_map, atts)
        if att is None:
            if not (self.issue or {}).get('full'):
                return AdfView.placeholder('image-x-generic-symbolic', 'Loading attachment…')
            name = block.get('alt') or 'An embedded file'
            return AdfView.placeholder('image-x-generic-symbolic',
                                       f'{name} is not among the ticket\'s attachments (deleted, or stored elsewhere)')
        if is_image(att):
            return self._inline_image(att, block)
        btn = Gtk.Button(halign=Gtk.Align.START, css_classes=['inline-file'])
        box = Gtk.Box(spacing=8)
        box.append(Gtk.Image.new_from_icon_name('text-x-generic-symbolic'))
        box.append(label(att.get('filename', 'file')))
        box.append(label(human_size(att.get('size') or 0), css=('dim-label', 'caption')))
        btn.set_child(box)
        btn.set_tooltip_text('Open')
        btn.connect('clicked', self._open_file, att)
        return btn

    def _inline_image(self, att, block, url=None):
        """The image at its own size, scaled down to fit (at most 720 px wide), loaded from the
        cache or downloaded once."""
        pic = Gtk.Picture(can_shrink=True, content_fit=Gtk.ContentFit.SCALE_DOWN)
        pic.add_css_class('inline-image')
        pic.set_overflow(Gtk.Overflow.HIDDEN)  # clip to the rounded corners
        pic.set_alternative_text((att or {}).get('filename') or block.get('alt') or 'Image')
        spinner = Adw.Spinner(width_request=24, height_request=24, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        holder = Gtk.Box(css_classes=['inline-image-loading'], height_request=120, width_request=200)
        holder.append(spinner)
        spinner.set_hexpand(True)
        stack = Gtk.Stack(hhomogeneous=False, vhomogeneous=False, interpolate_size=False)
        stack.add_named(holder, 'loading')
        stack.add_named(pic, 'image')
        # an image resized in an editor (percent of the text width) is shown at that share of 720 px
        limit = int(720 * block['width_pct'] / 100) if block.get('width_pct') else 720
        clamp = Adw.Clamp(maximum_size=limit, tightening_threshold=limit, child=stack)
        btn = Gtk.Button(child=clamp, halign=Gtk.Align.START, css_classes=['flat', 'inline-image-button'],
                         tooltip_text=(att or {}).get('filename') or url or '')
        if att is not None:
            images = [a for a in self._attachments() if is_image(a)]
            index = next((i for i, a in enumerate(images) if a.get('id') == att.get('id')), 0)
            btn.connect('clicked', lambda *_: ImageViewer(self.store, images, index, self.get_root()).present(self.get_root()))
        else:
            btn.connect('clicked', lambda *_: open_uri(self, url))

        def decoded(texture):
            pic.set_paintable(texture)
            stack.set_visible_child_name('image')

        def failed(e):
            if spinner.get_parent() is None:
                return
            holder.remove(spinner)
            holder.append(label(f'Could not load image: {e}', css=('dim-label', 'caption'), wrap=True))

        def loaded(path):
            self.store.worker.run(Gdk.Texture.new_from_filename, str(path), on_done=decoded, on_error=failed)
        if att is not None:
            self.store.image(att, loaded, failed)
        else:
            self.store.external_image(url, loaded, failed)
        return btn

    def _open_file(self, _btn, att):
        self.window.toast(f'Downloading {att.get("filename")}…')
        self.store.attachment_file(att, lambda path: open_file(self, path),
                                   lambda e: self.window.toast(f'Download failed: {e}'))

    # ----- comments -----

    def _comments_area(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.append(self.comment_list)
        composer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=['composer'])
        self.composer = rich_editor.create(placeholder='Add a comment… (@ to mention someone)', auto_height=True,
                                           min_height=110, resolve_image=self._resolve_image,
                                           find_people=self._find_people)
        self.composer.update_property([Gtk.AccessibleProperty.LABEL], ['New comment'])
        self.composer.connect('submit', lambda *_: self._post())
        self.composer.connect('notice', lambda _e, text: self.window.toast(text))
        self.composer.connect('files', lambda _e, files: self.comment_files.add_items(files))
        self.composer.connect('changed', lambda *_: self._composer_changed())
        composer.append(self.composer)
        self.comment_files = AttachmentList(show_button=False)
        composer.append(self.comment_files)
        actions = Gtk.Box(spacing=6)
        attach = Gtk.Button(child=Adw.ButtonContent(icon_name='mail-attachment-symbolic', label='Attach Files…'),
                            css_classes=['flat'], tooltip_text='Attach files to the ticket with this comment')
        attach.connect('clicked', lambda *_: self.comment_files.choose())
        actions.append(attach)
        hint = label('Ctrl+Enter to post · paste or drop images into the text', css=('dim-label', 'caption'))
        hint.set_hexpand(True)
        hint.set_ellipsize(Pango.EllipsizeMode.END)
        actions.append(hint)
        self.post_button = Gtk.Button(label='Comment', css_classes=['suggested-action'])
        self.post_button.connect('clicked', lambda *_: self._post())
        actions.append(self.post_button)
        composer.append(actions)
        box.append(composer)
        self._comment_drafts = {}   # ticket key -> (editor draft, files)
        self._composer_key_for = None
        self._composer_dirty = False
        self._composer_loading = False
        return box

    def _composer_changed(self):
        if self._composer_loading:
            self._composer_loading = False  # the change report of the content we just set
            return
        self._composer_dirty = True

    def _switch_composer(self, key):
        """Keep an unsent comment (and its files) per ticket."""
        old = self._composer_key_for
        if old == key:
            return
        unsent = self._composer_dirty and self.composer.draft() not in ('', '<p><br></p>')
        if old is not None and (unsent or self.comment_files.items()):
            self._comment_drafts[old] = (self.composer.draft(), self.comment_files.items())
        draft, files = self._comment_drafts.pop(key, (None, []))
        self._composer_loading = True
        if draft is not None:
            self.composer.restore_draft(draft)
            self._composer_dirty = True
            self._composer_loading = False
        else:
            self.composer.set_adf(None)
            self._composer_dirty = False
        self.comment_files.set_items(files)
        self._composer_key_for = key

    def _find_people(self, query, callback):
        if self.key and self.store.online:
            self.store.mention_people(query, self.key, None, callback)
        else:
            callback([])

    def _resolve_image(self, media_id, alt, callback):
        """File of an image already in a description or comment (for showing it while editing)."""
        att = media.lookup(media_id, alt, self.store.media_map(self.key) if self.key else {}, self._attachments())
        if att is None:
            callback(None)
            return
        self.store.image(att, callback, lambda _e: callback(None))

    def _render_comments(self, comments):
        self.comments = comments
        if self.key:
            self._media_map = self.store.media_map(self.key)
        clear(self.comment_list)
        if not comments:
            self.comment_list.append(Adw.ActionRow(title='No comments yet', css_classes=['dim-label']))
            return
        for c in comments:
            author = (c.get('author') or {}).get('displayName') or 'Unknown'
            row = Gtk.ListBoxRow(activatable=False)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=10, margin_bottom=12,
                          margin_start=12, margin_end=12)
            head = Gtk.Box(spacing=8)
            head.append(Adw.Avatar(size=24, text=author, show_initials=True))
            head.append(label(author, css=('heading',)))
            when = label(relative(c.get('created')), css=('dim-label', 'caption'))
            when.set_tooltip_text(full_time(c.get('created')) + (' (edited)' if c.get('updated') != c.get('created') else ''))
            head.append(when)
            box.append(head)
            box.append(AdfView(adf.to_blocks(c.get('body')), on_link=self._link, media_widget=self._media_widget,
                               compact=True))
            row.set_child(box)
            self.comment_list.append(row)

    def _post(self):
        if not self.key or not self.store.online or not self.post_button.get_sensitive():
            return
        key = self.key
        files = self.comment_files.items()
        self.post_button.set_sensitive(False)

        def got(doc, pending):
            if doc is None and not files:
                self.post_button.set_sensitive(True)
                return

            def done(comments, warnings):
                self.post_button.set_sensitive(True)
                if self._composer_key_for == key:
                    self._composer_loading = True
                    self.composer.set_adf(None)
                    self._composer_dirty = False
                    self.comment_files.clear()
                else:
                    self._comment_drafts.pop(key, None)
                if self.key == key:
                    self._render_comments(comments)
                    self.show(key, scroll=self.scroll_position())  # new attachments
                for w in warnings:
                    self.window.toast(w)
                self.window.toast(f'Comment added to {key}')

            def failed(e):
                self.post_button.set_sensitive(True)
                self.window.toast(f'Comment not posted: {e}')  # text and files stay
            self.store.add_comment(key, doc, done, failed, pending=pending, files=files)
        self.composer.fetch_content(got)

    # ----- status / transitions -----

    def _load_transitions(self):
        clear(self.status_list)
        self.status_list.append(Gtk.ListBoxRow(child=label('Loading…', css=('dim-label',)), activatable=False))
        key = self.key

        def got(transitions):
            if key != self.key:
                return
            clear(self.status_list)
            if not transitions:
                self.status_list.append(Gtk.ListBoxRow(child=label('No transitions available', css=('dim-label',)), activatable=False))
            for t in transitions:
                target = (t.get('to') or {}).get('name') or t.get('name')
                required = [(fid, m) for fid, m in (t.get('fields') or {}).items() if m.get('required')]
                box = Gtk.Box(spacing=8, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
                name = t['name'] if t['name'] == target else f'{t["name"]} → {target}'
                box.append(label(name, xalign=0.0))
                box.get_first_child().set_hexpand(True)
                if required:
                    box.append(label('needs fields', css=('dim-label', 'caption')))
                row = Gtk.ListBoxRow(child=box)
                row.transition = t
                row.required = required
                self.status_list.append(row)

        def failed(e):
            clear(self.status_list)
            self.status_list.append(Gtk.ListBoxRow(child=label(str(e), wrap=True, css=('error',)), activatable=False))
        self.store.transitions(key, got, failed)

    def _transition_row(self, _list, row):
        t = getattr(row, 'transition', None)
        if not t or not self.issue:
            return
        self.status_popover.popdown()
        key = self.key
        target = (t.get('to') or {}).get('name') or t['name']
        self.status_button.set_sensitive(False)

        def done(issue):
            self.status_button.set_sensitive(self.store.online)
            if self.key == key:
                self._render_issue(issue, from_cache=False)
            self.window.toast(f'{key} moved to {target}')
            self.window.issue_changed(issue)

        def failed(message):
            self.status_button.set_sensitive(self.store.online and self.issue is not None)
            if message:
                self.window.toast(message)
        TransitionFlow(self, self.store, key, self.issue['fields'], t, done, failed).start()

    # ----- misc -----

    def _watch_toggled(self, btn):
        key, on = self.key, btn.get_active()
        btn.set_sensitive(False)

        def done(issue):
            btn.set_sensitive(True)
            if self.key == key:
                self._render_issue(issue, from_cache=False)
            self.window.toast(f'Watching {key}' if on else f'Stopped watching {key}')

        def failed(e):
            btn.set_sensitive(True)
            btn.handler_block(self.watch_handler)
            btn.set_active(not on)
            btn.handler_unblock(self.watch_handler)
            self.window.toast(str(e))
        self.store.set_watching(key, on, done, failed)

    def _copy_link(self, *_):
        if self.key and self.store.client:
            self.get_clipboard().set_text(self.store.client.browse_url(self.key))
            self.window.toast('Link copied')

    def _link(self, uri):
        key = self.window.key_from_uri(uri)
        if key:
            self.window.open_key(key)
        else:
            open_uri(self, uri)
