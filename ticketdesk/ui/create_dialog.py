# SPDX-License-Identifier: GPL-2.0-or-later
"""New ticket.

Wide: summary and description on the left, the ticket's settings in a column on the
right. Narrow: one column. Settings: project, type, reporter, assignee, priority, then
fields Jira requires or that are pinned for the project, and the remaining optional
fields under "More Fields" (collapsed). Pinning a field there makes it always shown,
and required, for that project.
"""

from gi.repository import Adw, GLib, GObject, Gtk

from ..core import create as C
from ..core import fields as F
from ..core.api import JiraError
from . import rich_editor
from .attachment_list import AttachmentList
from .field_inputs import FieldInput, UserRow
from .util import label


class CreateDialog(Adw.Dialog):
    def __init__(self, window, project=None):
        super().__init__(title='New Ticket', content_width=1000, content_height=760)
        self.window = window
        self.store = window.store
        self.config = window.app.config
        self.projects = []
        self.types = []
        self.fields = []
        self.inputs = []
        self._wanted_project = project or self.config.create_project or None
        self._wanted_type = None
        self._fields_serial = 0
        self._busy = False

        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label='Cancel')
        cancel.connect('clicked', lambda *_: self.close())
        header.pack_start(cancel)
        self.create_button = Gtk.Button(label='Create', css_classes=['suggested-action'], sensitive=False,
                                        tooltip_text='Create the ticket (Ctrl+Enter)')
        self.create_button.connect('clicked', lambda *_: self._create())
        header.pack_end(self.create_button)
        self.spinner = Adw.Spinner(visible=False)
        header.pack_end(self.spinner)

        self.banner = Adw.Banner(revealed=False)
        self.banner.connect('button-clicked', lambda *_: self.banner.set_revealed(False))

        # Main column: summary and description
        self.summary = Gtk.Entry(placeholder_text='Summary', css_classes=['summary-entry'], hexpand=True)
        self.summary.connect('changed', lambda *_: self._update_sensitivity())
        self.summary.connect('activate', lambda *_: self.editor.grab_focus())
        self.editor = rich_editor.create(placeholder='Description (@ to mention someone)', min_height=260,
                                         find_people=lambda q, cb: self.store.mention_people(q, None, self._project(), cb))
        self.editor.connect('submit', lambda *_: self._create())
        self.editor.connect('notice', lambda _e, text: self._toast(text))
        self.attachments = AttachmentList()
        self.editor.connect('files', lambda _e, files: self.attachments.add_items(files))
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, hexpand=True)
        main.append(self.summary)
        main.append(self.editor)
        main.append(self.attachments)
        self.editor.set_vexpand(True)

        # Side column: settings
        self.side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, width_request=320)
        basics = Adw.PreferencesGroup()
        self.project_row = Adw.ComboRow(title='Project', model=Gtk.StringList(), use_markup=False, enable_search=True)
        self.project_row.connect('notify::selected', lambda *_: self._project_changed())
        self.type_row = Adw.ComboRow(title='Type', model=Gtk.StringList(), use_markup=False)
        self.type_row.connect('notify::selected', lambda *_: self._type_changed())
        basics.add(self.project_row)
        basics.add(self.type_row)
        self.side.append(basics)

        people = Adw.PreferencesGroup(title='People')
        self.reporter = UserRow(self.store, 'Reporter', allow_none=False)
        self.assignee = UserRow(self.store, 'Assignee', assignable=True, allow_none=True)
        people.add(self.reporter)
        people.add(self.assignee)
        self.side.append(people)

        self.details = Adw.PreferencesGroup(title='Details')
        self.priority_row = Adw.ComboRow(title='Priority', model=Gtk.StringList(), use_markup=False)
        self.priority_ids = []
        self.details.add(self.priority_row)
        self.side.append(self.details)
        self.extra_rows = []  # rows added to Details for required/pinned fields

        more_group = Adw.PreferencesGroup()
        self.more = Adw.ExpanderRow(title='More Fields', expanded=False, use_markup=False)
        more_group.add(self.more)
        self.more_rows = []
        self.side.append(more_group)
        self.browser_note = label('', css=('dim-label', 'caption'), wrap=True)
        self.browser_note.set_visible(False)
        self.side.append(self.browser_note)
        self.loading_note = label('Loading the project’s fields…', css=('dim-label', 'caption'))
        self.side.append(self.loading_note)

        self.columns = Gtk.Box(spacing=24, margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
        self.columns.append(main)
        self.columns.append(self.side)
        scroller = Gtk.ScrolledWindow(child=self.columns, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.toasts = Adw.ToastOverlay(child=scroller)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.add_top_bar(self.banner)
        view.set_content(self.toasts)
        self.set_child(view)

        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse('max-width: 880sp'))
        vertical = GObject.Value()
        vertical.init(Gtk.Orientation.__gtype__)
        vertical.set_enum(Gtk.Orientation.VERTICAL)
        narrow.add_setter(self.columns, 'orientation', vertical)
        narrow.add_setter(self.side, 'width-request', -1)
        narrow.add_setter(self.editor, 'height-request', 300)
        self.add_breakpoint(narrow)

        keys = Gtk.ShortcutController()
        keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string('<Control>Return'),
                                       action=Gtk.CallbackAction.new(lambda *_: (self._create(), True)[1])))
        self.add_controller(keys)

        # Closing with something written asks first
        self._editor_changes = 0
        self.editor.connect('changed', lambda *_: self._count_change())
        self.summary.connect('changed', lambda *_: self._update_can_close())
        self.attachments.connect('changed', lambda *_: self._update_can_close())
        self.connect('close-attempt', lambda *_: self._confirm_discard())

        self.store.ensure_me(lambda me: self._me_known(me))
        self.store.create_projects(self._set_projects)
        GLib.idle_add(lambda: (self.summary.grab_focus(), GLib.SOURCE_REMOVE)[1])

    # ----- closing -----

    def _count_change(self):
        self._editor_changes += 1   # the first report is the empty document being loaded
        self._update_can_close()

    def _has_content(self):
        loaded = 1 if rich_editor.available() else 0  # the rich editor reports its initial empty document
        return bool(self.summary.get_text().strip() or self.attachments.items() or self._editor_changes > loaded)

    def _update_can_close(self):
        self.set_can_close(not self._has_content())

    def _confirm_discard(self):
        if self._busy:
            return  # creating: wait for the result
        dlg = Adw.AlertDialog(heading='Discard New Ticket?', body='The summary, description and files will be lost.')
        dlg.add_response('keep', 'Keep Editing')
        dlg.add_response('discard', 'Discard')
        dlg.set_response_appearance('discard', Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response('keep')
        dlg.set_close_response('keep')
        dlg.connect('response', lambda _d, r: r == 'discard' and self.force_close())
        dlg.present(self)

    # ----- loading -----

    def _me_known(self, me):
        if not me.get('accountId'):
            return
        if self.reporter.value() is None:
            self.reporter.set(me['accountId'], me.get('displayName') or 'Me')
        if self.config.assign_to_me and self.assignee.value() is None:
            self.assignee.set(me['accountId'], me.get('displayName') or 'Me')

    def _set_projects(self, projects):
        current = self._project()
        self.projects = projects
        model = Gtk.StringList.new([f'{p["name"]} ({p["key"]})' if p.get('name') else p['key'] for p in projects])
        self.project_row.set_model(model)
        keys = [p['key'] for p in projects]
        want = current or self._wanted_project
        self.project_row.set_selected(keys.index(want) if want in keys else 0)
        self._project_changed()

    def _project(self):
        i = self.project_row.get_selected()
        return self.projects[i]['key'] if 0 <= i < len(self.projects) else None

    def _type(self):
        i = self.type_row.get_selected()
        return self.types[i] if 0 <= i < len(self.types) else None

    def _project_changed(self):
        project = self._project()
        if not project or project == getattr(self, '_loaded_project', None):
            return
        self._loaded_project = project
        self.assignee.project = project
        self.reporter.project = project
        self.types = []
        self.type_row.set_model(Gtk.StringList())
        self._set_loading(True)

        def got(types):
            if self._project() != project:
                return
            current = (self._type() or {}).get('name') or self._wanted_type or ''
            self.types = C.order_types(types)
            self.type_row.set_model(Gtk.StringList.new([t['name'] for t in self.types]))
            names = [t['name'].lower() for t in self.types]
            want = current.lower() if current.lower() in names else (C.default_type(self.types) or {}).get('name', '').lower()
            self._loaded_type = None
            self.type_row.set_selected(names.index(want) if want in names else 0)
            self._type_changed()

        def failed(e):
            if self._project() == project:
                self._set_loading(False)
                self._show_error(f'Could not load ticket types for {project}: {e}')
        self.store.create_types(project, got, failed)

    def _type_changed(self):
        t = self._type()
        project = self._project()
        if not t or not project:
            return
        if (project, t['id']) == getattr(self, '_loaded_type', None):
            return
        self._loaded_type = (project, t['id'])
        self._wanted_type = t['name']
        self._fields_serial += 1
        serial = self._fields_serial
        self._set_loading(True)

        def got(field_list):
            if serial == self._fields_serial:
                self._set_loading(False)
                self._build_fields(project, field_list)

        def failed(e):
            if serial == self._fields_serial:
                self._set_loading(False)
                self._show_error(f'Could not load the fields of {project} {t["name"]}: {e}')
        self.store.create_fields(project, t['id'], got, failed)

    def _set_loading(self, on):
        self.loading_note.set_visible(on)
        self._update_sensitivity()

    # ----- fields -----

    def _build_fields(self, project, field_list):
        self.fields = field_list
        meta = C.by_id(field_list)
        # keep what was entered in fields that are still on the form
        previous = {i.fid: i for i in self.inputs}
        for row in self.extra_rows:
            self.details.remove(row)
        for row in self.more_rows:
            self.more.remove(row)
        self.extra_rows, self.more_rows, self.inputs = [], [], []

        self.reporter.set_visible('reporter' in meta)
        self.assignee.set_visible('assignee' in meta)
        prio = meta.get('priority')
        self.priority_row.set_visible(prio is not None)
        if prio is not None:
            current = self.priority_ids[self.priority_row.get_selected()] \
                if 0 <= self.priority_row.get_selected() < len(self.priority_ids) else None
            values = [v for v in prio.get('allowedValues') or [] if not v.get('disabled')]
            self.priority_ids = [str(v['id']) for v in values]
            self.priority_row.set_model(Gtk.StringList.new([F.option_label(v) for v in values]))
            want = current if current in self.priority_ids else C.default_option(prio)
            if want in self.priority_ids:
                self.priority_row.set_selected(self.priority_ids.index(want))

        pinned = self.config.pinned_fields.get(project, [])
        shown, more, browser_only = C.split(field_list, pinned)
        for m in shown:
            inp = FieldInput(self.store, m, project, pinned=C.field_id(m) in pinned, on_pin=self._pin)
            self._carry(previous.get(inp.fid), inp)
            inp.required = True
            self.details.add(inp.row)
            self.extra_rows.append(inp.row)
            self.inputs.append(inp)
        for m in more:
            inp = FieldInput(self.store, m, project, pinned=False, on_pin=self._pin)
            self._carry(previous.get(inp.fid), inp)
            inp.required = False
            self.more.add_row(inp.row)
            self.more_rows.append(inp.row)
            self.inputs.append(inp)
        self.more.set_visible(bool(more))
        self.more.set_subtitle(f'{len(more)} optional field{"s" if len(more) != 1 else ""}')
        if browser_only:
            names = ', '.join(m.get('name') or C.field_id(m) for m in browser_only)
            self.browser_note.set_text(f'Jira requires {names}, which can only be filled in on the Jira website. '
                                       'Creating will fail until the project changes this.')
        self.browser_note.set_visible(bool(browser_only))
        self._update_sensitivity()

    @staticmethod
    def _carry(old, new):
        if old is None or old.kind != new.kind:
            return
        if new.kind == 'text':
            new.row.set_text(old.row.get_text())
        elif new.kind == 'choice':
            sel = old.ids[old.row.get_selected()] if 0 <= old.row.get_selected() < len(old.ids) else None
            if sel in new.ids:
                new.row.set_selected(new.ids.index(sel))
        elif new.kind == 'multi':
            chosen = {i for i, c in old.checks if c.get_active()}
            for i, c in new.checks:
                c.set_active(i in chosen)
        elif new.kind == 'user' and old.row.value():
            new.row.set(old.row.value(), old.row.name)

    def _pin(self, fid, on):
        project = self._project()
        pins = dict(self.config.pinned_fields)
        current = [f for f in pins.get(project, []) if f != fid]
        if on:
            current.append(fid)
        if current:
            pins[project] = current
        else:
            pins.pop(project, None)
        self.config.pinned_fields = pins
        self.config.save()
        name = (C.by_id(self.fields).get(fid) or {}).get('name') or fid
        self._toast(f'{name} is now always shown for {project}' if on else f'{name} is optional again')
        GLib.idle_add(lambda: (self._build_fields(project, self.fields), GLib.SOURCE_REMOVE)[1])

    # ----- creating -----

    def _update_sensitivity(self):
        ready = bool(self._project() and self._type() and self.fields and self.summary.get_text().strip()
                     and not self._busy)
        self.create_button.set_sensitive(ready)

    def _show_error(self, text):
        self.banner.set_title(GLib.markup_escape_text(text))
        self.banner.set_button_label('Dismiss')
        self.banner.set_revealed(True)

    def _toast(self, text):
        if text:
            self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=4))

    def _create(self):
        if self._busy or not self.create_button.get_sensitive():
            return
        missing, problems, other = [], [], {}
        for inp in self.inputs:
            inp.set_error(False)
            bad = inp.invalid()
            if bad:
                problems.append(bad)
                inp.set_error(True)
                continue
            v = inp.value()
            if v is None and inp.required:
                missing.append(inp.name)
                inp.set_error(True)
            other[inp.fid] = v
        if self.reporter.get_visible() and not self.reporter.value():
            missing.append('Reporter')
        if missing or problems:
            self._show_error('; '.join(problems + ([f'Required: {", ".join(missing)}'] if missing else [])))
            return
        self.banner.set_revealed(False)
        self._busy = True
        self.spinner.set_visible(True)
        self._update_sensitivity()
        self.editor.fetch_content(lambda doc, pending: self._send(doc, other, pending))

    def _send(self, description, other, pending):
        meta = C.by_id(self.fields)
        prio_i = self.priority_row.get_selected()
        body = C.payload(
            self._project(), self._type()['id'], self.summary.get_text(), description=description,
            reporter=self.reporter.value() if self.reporter.get_visible() else None,
            assignee=self.assignee.value() if self.assignee.get_visible() else None,
            priority=self.priority_ids[prio_i] if self.priority_row.get_visible() and 0 <= prio_i < len(self.priority_ids) else None,
            other=other, available=set(meta))

        files = self.attachments.items()
        if pending or files:
            n = len(pending) + len(files)
            self._toast(f'Creating the ticket and uploading {n} file{"s" if n != 1 else ""}…')

        def done(issue, warnings):
            self._busy = False
            self.force_close()
            self.window.ticket_created(issue, warnings)

        def failed(e):
            self._busy = False
            self.spinner.set_visible(False)
            self._update_sensitivity()
            names = {fid: m.get('name') or fid for fid, m in meta.items()}
            names.update({'summary': 'Summary', 'description': 'Description', 'reporter': 'Reporter',
                          'assignee': 'Assignee', 'priority': 'Priority'})
            text = C.error_text(e, names) if isinstance(e, JiraError) else str(e)
            for inp in self.inputs:
                inp.set_error(inp.fid in getattr(e, 'field_errors', {}))
            self._show_error(f'Jira did not create the ticket: {text}')
        self.store.create_issue(body, done, failed, pending=pending, files=files)
