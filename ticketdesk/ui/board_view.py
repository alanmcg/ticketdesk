# SPDX-License-Identifier: GPL-2.0-or-later
"""Board: one column per board column, cards in Jira's rank order, optionally split
into swimlanes (assignee, epic/parent, priority, ticket type).

Dragging a card to another column runs the matching transition (with the same
required-field and workflow-rule handling as the ticket view); dropping it between
cards ranks it there; dropping it in another swimlane changes that field first. The card
moves at once and is marked as pending until Jira has accepted the changes; afterwards
the board is re-read, so what is shown is Jira's state.
"""

from gi.repository import Adw, Gdk, GLib, GObject, Gtk, Pango

from ..core import boards as board_logic
from ..core.api import OfflineError
from .transitions import TransitionFlow
from .util import label

COLUMN_WIDTH = 272
EDGE = 48           # px from a scroller's edge where dragging scrolls it
STEP = 14           # px per tick
TICK_MS = 16


def _value_str(value):
    return value.get_string() if isinstance(value, GObject.Value) else str(value)


class _AutoScroll:
    """Scroll a ScrolledWindow while a drag hovers near its edge (GTK does not do this itself)."""

    def __init__(self, scroller, orientation):
        self.scroller = scroller
        self.vertical = orientation == Gtk.Orientation.VERTICAL
        self.speed = 0
        self.source = 0
        motion = Gtk.DropControllerMotion()
        motion.connect('enter', self._motion)
        motion.connect('motion', self._motion)
        motion.connect('leave', lambda *_: self._stop())
        scroller.add_controller(motion)

    def _motion(self, _ctl, x, y):
        pos = y if self.vertical else x
        size = self.scroller.get_height() if self.vertical else self.scroller.get_width()
        if pos < EDGE:
            self.speed = -STEP
        elif pos > size - EDGE:
            self.speed = STEP
        else:
            self.speed = 0
        if self.speed and not self.source:
            self.source = GLib.timeout_add(TICK_MS, self._tick)
        elif not self.speed:
            self._stop()

    def _tick(self):
        adj = self.scroller.get_vadjustment() if self.vertical else self.scroller.get_hadjustment()
        new = max(adj.get_lower(), min(adj.get_value() + self.speed, adj.get_upper() - adj.get_page_size()))
        adj.set_value(new)
        if not self.speed:
            self.source = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _stop(self):
        self.speed = 0
        if self.source:
            GLib.source_remove(self.source)
            self.source = 0


class _Cell(Gtk.Box):
    """The cards of one column (within one swimlane, when lanes are on). Without lanes it
    has a header and scrolls on its own; with lanes the whole board scrolls."""

    def __init__(self, board, index, col, lane=None, lanes_on=False):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, width_request=COLUMN_WIDTH,
                         css_classes=['board-column'])
        self.board = board
        self.index = index
        self.col = col
        self.lane = lane
        self.keys = []
        self._marked = None
        self.count = None

        if not lanes_on:
            self.append(_column_header(col, self))
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=['board-list'])
        self.list.set_placeholder(label('' if lanes_on else ('No cards' if col['statuses'] else
                                                          'No statuses mapped to this column'),
                                        css=('dim-label', 'caption'), xalign=0.5))
        self.list.set_filter_func(board.filter_row)
        self.list.connect('row-activated', lambda _l, row: board.emit('activated', row.key))
        if lanes_on:
            self.list.set_size_request(-1, 56)  # room to drop into an empty cell
            self.list.set_vexpand(True)
            self.append(self.list)
            self.scroller = None
        else:
            self.scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, child=self.list)
            self.append(self.scroller)
            self._autoscroll = _AutoScroll(self.scroller, Gtk.Orientation.VERTICAL)

        if col['statuses']:
            drop = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
            drop.connect('motion', self._motion)
            drop.connect('leave', lambda *_: self._mark(None))
            drop.connect('drop', self._drop)
            self.list.add_controller(drop)

    # ----- cards -----

    def set_cards(self, rows):
        while (r := self.list.get_row_at_index(0)) is not None:
            self.list.remove(r)
        self.keys = []
        for row in rows:
            self.list.append(row)
            self.keys.append(row.key)
        self.board.update_counts()

    def take(self, key):
        i = self.keys.index(key)
        row = self.list.get_row_at_index(i)
        self.list.remove(row)
        self.keys.pop(i)
        self.board.update_counts()
        return row

    def put(self, row, index):
        index = max(0, min(index, len(self.keys)))
        self.list.insert(row, index)
        self.keys.insert(index, row.key)
        self.board.update_counts()

    # ----- drop target -----

    def _index_at(self, y):
        row = self.list.get_row_at_y(int(y))
        if row is None:
            first = self.list.get_row_at_index(0)
            if first is not None:
                ok, rect = first.compute_bounds(self.list)
                if ok and y < rect.get_y():
                    return 0
            return len(self.keys)
        ok, rect = row.compute_bounds(self.list)
        after = ok and y > rect.get_y() + rect.get_height() / 2
        return row.get_index() + (1 if after else 0)

    def _mark(self, index):
        if self._marked is not None:
            self._marked[0].remove_css_class(self._marked[1])
            self._marked = None
        if index is None:
            self.remove_css_class('drop-target')
            return
        self.add_css_class('drop-target')
        if not self.keys:
            return
        if index < len(self.keys):
            row, css = self.list.get_row_at_index(index), 'drop-before'
        else:
            row, css = self.list.get_row_at_index(len(self.keys) - 1), 'drop-after'
        row.add_css_class(css)
        self._marked = (row, css)

    def _motion(self, _target, _x, y):
        self._mark(self._index_at(y))
        return Gdk.DragAction.MOVE

    def _drop(self, _target, value, _x, y):
        index = self._index_at(y)
        self._mark(None)
        self.board.dropped(_value_str(value), self, index)
        return True


def _column_header(col, owner):
    head = Gtk.Box(spacing=8, margin_start=10, margin_end=10, margin_top=8)
    name = label(col['name'], css=('heading',), ellipsize=Pango.EllipsizeMode.END)
    name.set_tooltip_text(col['name'])
    head.append(name)
    owner.count = label('', css=('dim-label', 'numeric'))
    owner.count.set_hexpand(True)
    head.append(owner.count)
    return head


def _set_count(lbl, col, n):
    text = str(n)
    if col.get('max'):
        text = f'{n} / max {col["max"]}'
        if n > col['max']:
            lbl.add_css_class('error')
        else:
            lbl.remove_css_class('error')
    lbl.set_text(text)


class _HeaderCell(Gtk.Box):
    """Column header above the swimlanes."""

    def __init__(self, col):
        super().__init__(width_request=COLUMN_WIDTH, css_classes=['board-column-head'])
        self.col = col
        self.append(_column_header(col, self))


class BoardView(Gtk.Box):
    __gsignals__ = {'activated': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
                    'info': (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        self.window = window
        self.store = window.store
        self.board = None
        self.state = None
        self.issues = {}
        self.cells = []
        self.headers = []
        self.lane_mode = 'none'
        self._collapsed = set()
        self.selected = None
        self._text = ''
        self._serial = 0
        self._pending = set()

        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(Adw.Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                                         width_request=32, height_request=32), 'loading')
        self.message = Adw.StatusPage(icon_name='view-grid-symbolic')
        self.stack.add_named(self.message, 'message')
        self.columns_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=10,
                                   margin_end=10, margin_top=4, margin_bottom=10)
        self.scroller = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, child=self.columns_box)
        self._vautoscroll = _AutoScroll(self.scroller, Gtk.Orientation.VERTICAL)
        self._autoscroll = _AutoScroll(self.scroller, Gtk.Orientation.HORIZONTAL)
        self.stack.add_named(self.scroller, 'board')
        self.append(self.stack)

    # ----- loading -----

    def show(self, board):
        changed = not self.board or board['id'] != self.board['id']
        self.board = board
        self._serial += 1
        serial = self._serial
        if changed:
            self.state = None
            self.cells = []
            self._collapsed = set()
            self.lane_mode = self.window.app.config.swimlanes.get(str(board['id']), 'none')
            self.stack.set_visible_child_name('loading')
            self.emit('info', board['name'])

        def cached(state):
            if serial == self._serial and not self._pending:
                self._render(state, cached=True)

        def fresh(state):
            if serial == self._serial:
                self._render(state, cached=False)

        def failed(e):
            if serial != self._serial:
                return
            if self.state is not None:
                self.window.toast(f'Showing cached board: {e}' if isinstance(e, OfflineError) else str(e))
                return
            self.message.set_title('Offline' if isinstance(e, OfflineError) else 'Could Not Load Board')
            self.message.set_description(GLib.markup_escape_text(str(e)))
            self.stack.set_visible_child_name('message')
        self.store.load_board(board, cached, fresh, failed)

    def reload(self):
        if self.board:
            self.show(self.board)

    def browser_url(self):
        if self.board and self.store.client:
            return f'{self.store.client.site}/secure/RapidBoard.jspa?rapidView={int(self.board["id"])}'
        return None

    def _render(self, state, cached):
        self.state = state
        self._pending.clear()
        cols = state['columns']
        grouped, unplaced = board_logic.group(state['issues'], cols)
        self.issues = {i['key']: i for i in state['issues']}

        n = sum(len(g) for g in grouped)
        parts = [self.board['name']]
        if state.get('scope'):
            parts.append(state['scope'])
        parts.append(f'{n} cards')
        if state.get('total', 0) > len(state['issues']):
            parts.append(f'first {len(state["issues"])} of {state["total"]}')
        if unplaced:
            parts.append(f'{unplaced} in statuses not on the board')
        if cached:
            parts.append('cached' if not self.store.online else 'updating…')
        self.emit('info', ' · '.join(parts))

        if self.board.get('type') == 'scrum' and not state.get('sprints'):
            self.message.set_title('No Active Sprint')
            self.message.set_description('This board shows its active sprint. Start a sprint in Jira to see it here.')
            self.stack.set_visible_child_name('message')
            return
        if not cols:
            self.message.set_title('No Columns')
            self.message.set_description('This board has no columns configured.')
            self.stack.set_visible_child_name('message')
            return

        self._build(cols, state['issues'])

    def set_swimlanes(self, mode):
        self.lane_mode = mode
        if self.board:
            self.window.app.config.swimlanes[str(self.board['id'])] = mode
            self.window.app.config.save()
        if self.state:
            self._collapsed = set()
            self._build(self.state['columns'], self.state['issues'], keep_scroll=False)

    def _build(self, cols, issues, keep_scroll=True):
        lanes_on = self.lane_mode != 'none'
        h, v = self.scroller.get_hadjustment(), self.scroller.get_vadjustment()
        same = keep_scroll and [c.col['name'] for c in self.cells[:len(cols)]] == [c['name'] for c in cols]
        hpos, vpos = (h.get_value(), v.get_value()) if same else (0, 0)
        cell_pos = {(c.lane, c.index): c.scroller.get_vadjustment().get_value()
                    for c in self.cells if c.scroller is not None} if same else {}

        while (child := self.columns_box.get_first_child()) is not None:
            self.columns_box.remove(child)
        self.cells = []
        self.scroller.set_policy(Gtk.PolicyType.AUTOMATIC,
                                 Gtk.PolicyType.AUTOMATIC if lanes_on else Gtk.PolicyType.NEVER)
        self.headers = []
        if not lanes_on:
            row = Gtk.Box(spacing=10, vexpand=True)
            grouped, _ = board_logic.group(issues, cols)
            for i, (col, cards) in enumerate(zip(cols, grouped)):
                cell = _Cell(self, i, col)
                self.cells.append(cell)
                cell.set_cards([self._card(it) for it in cards])
                row.append(cell)
            self.columns_box.append(row)
        else:
            heads = Gtk.Box(spacing=10)
            for col in cols:
                hc = _HeaderCell(col)
                self.headers.append(hc)
                heads.append(hc)
            self.columns_box.append(heads)
            for lane_key, title, lane_issues in board_logic.lanes(issues, self.lane_mode):
                self.columns_box.append(self._lane(cols, lane_key, title, lane_issues))
        self.update_counts()
        self.select(self.selected)
        self.stack.set_visible_child_name('board')

        def restore():
            h.set_value(hpos)
            v.set_value(vpos)
            for c in self.cells:
                if c.scroller is not None and (c.lane, c.index) in cell_pos:
                    c.scroller.get_vadjustment().set_value(cell_pos[(c.lane, c.index)])
            return GLib.SOURCE_REMOVE
        if same:
            GLib.idle_add(restore)

    def _lane(self, cols, lane_key, title, lane_issues):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=['board-lane'])
        collapsed = lane_key in self._collapsed
        arrow = Gtk.Image.new_from_icon_name('pan-end-symbolic' if collapsed else 'pan-down-symbolic')
        head = Gtk.Box(spacing=8)
        head.append(arrow)
        name = label(title or 'None', css=('heading',), ellipsize=Pango.EllipsizeMode.END)
        name.set_max_width_chars(60)
        head.append(name)
        head.append(label(f'{len(lane_issues)}', css=('dim-label', 'numeric')))
        toggle = Gtk.Button(child=head, css_classes=['flat', 'lane-toggle'], halign=Gtk.Align.START)
        box.append(toggle)
        row = Gtk.Box(spacing=10)
        grouped, _ = board_logic.group(lane_issues, cols)
        for i, (col, cards) in enumerate(zip(cols, grouped)):
            cell = _Cell(self, i, col, lane=lane_key, lanes_on=True)
            self.cells.append(cell)
            cell.set_cards([self._card(it) for it in cards])
            row.append(cell)
        revealer = Gtk.Revealer(child=row, reveal_child=not collapsed)
        box.append(revealer)

        def flip(_b):
            show = not revealer.get_reveal_child()
            revealer.set_reveal_child(show)
            arrow.set_from_icon_name('pan-down-symbolic' if show else 'pan-end-symbolic')
            (self._collapsed.discard if show else self._collapsed.add)(lane_key)
        toggle.connect('clicked', flip)
        return box

    def update_counts(self):
        """Card counts per column (summed over swimlanes), with WIP limits."""
        if self.lane_mode == 'none' or not getattr(self, 'headers', None):
            for c in self.cells:
                if c.count is not None:
                    _set_count(c.count, c.col, len(c.keys))
            return
        for i, hc in enumerate(self.headers):
            _set_count(hc.count, hc.col, sum(len(c.keys) for c in self.cells if c.index == i))

    # ----- cards -----

    def _card(self, issue):
        f = issue.get('fields') or {}
        row = Gtk.ListBoxRow(css_classes=['board-card-row'])
        row.key = issue['key']
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=['card', 'board-card'])
        top = Gtk.Box(spacing=6)
        key = label(issue['key'], css=('monospace', 'issue-key', 'dim-label'))
        key.set_hexpand(True)
        top.append(key)
        assignee = (f.get('assignee') or {}).get('displayName')
        if assignee:
            avatar = Adw.Avatar(size=22, text=assignee, show_initials=True)
            avatar.set_tooltip_text(f'Assignee: {assignee}')
            top.append(avatar)
        card.append(top)
        summary = label(f.get('summary') or '', wrap=True, ellipsize=Pango.EllipsizeMode.END)
        summary.set_lines(4)
        summary.set_tooltip_text(f.get('summary') or '')
        card.append(summary)
        meta = ' · '.join(x for x in ((f.get('issuetype') or {}).get('name'), (f.get('priority') or {}).get('name')) if x)
        if meta:
            card.append(label(meta, css=('dim-label', 'caption')))
        row.set_child(card)
        row.search_text = ' '.join((issue['key'], f.get('summary') or '', assignee or '')).lower()
        row.update_property([Gtk.AccessibleProperty.LABEL], [f'{issue["key"]}: {f.get("summary") or ""}'])

        drag = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        drag.connect('prepare', self._drag_prepare, row)
        drag.connect('drag-begin', self._drag_begin, row)
        drag.connect('drag-end', lambda *_: row.remove_css_class('dragging'))
        row.add_controller(drag)
        return row

    def _drag_prepare(self, _source, x, y, row):
        if not self.store.online or row.key in self._pending:
            return None
        row.hot = (int(x), int(y))
        return Gdk.ContentProvider.new_for_value(GObject.Value(GObject.TYPE_STRING, row.key))

    def _drag_begin(self, source, _drag, row):
        x, y = getattr(row, 'hot', (0, 0))
        source.set_icon(Gtk.WidgetPaintable.new(row.get_child()), x, y)
        row.add_css_class('dragging')

    def filter_row(self, row):
        return not self._text or self._text in getattr(row, 'search_text', '')

    def set_filter(self, text):
        self._text = (text or '').strip().lower()
        for c in self.cells:
            c.list.invalidate_filter()

    def select(self, key):
        self.selected = key
        for c in self.cells:
            for i, k in enumerate(c.keys):
                row = c.list.get_row_at_index(i)
                if k == key:
                    row.add_css_class('selected-card')
                else:
                    row.remove_css_class('selected-card')

    # ----- moves -----

    def _cell_of_key(self, key):
        return next((c for c in self.cells if key in c.keys), None)

    def dropped(self, key, dst, index):
        src = self._cell_of_key(key)
        if src is None or key in self._pending:
            return
        if not self.store.online:
            self.window.toast('Offline: cards cannot be moved')
            return
        lane_fields = None
        if src.lane != dst.lane:
            lane_fields = board_logic.lane_change(self.lane_mode, dst.lane)
            if lane_fields is None:
                what = dict(board_logic.LANE_MODES).get(self.lane_mode, '').lower()
                self.window.toast(f'Cards cannot be moved to that {what} swimlane by dragging; '
                                  'change the field in the ticket instead')
                return
        needs_transition, rank = board_logic.drop_plan(dst.keys, key, src.index, dst.index, index)
        if src is not dst and not needs_transition and rank is None and lane_fields is None:
            return
        if src is dst and not needs_transition and rank is None:
            return
        # Show the card where it was dropped, marked as pending until Jira confirms.
        old = src.keys.index(key)
        row = src.take(key)
        if src is dst and index > old:
            index -= 1
        dst.put(row, index)
        row.add_css_class('pending')
        self._pending.add(key)

        steps = []
        if lane_fields is not None:
            steps.append(('edit', lane_fields))
        if needs_transition:
            steps.append(('transition', dst.col))
        if rank:
            steps.append(('rank', rank))
        self._next_step(key, steps, done_any=False)

    def _next_step(self, key, steps, done_any):
        """Run the changes of one drop in order: field change (swimlane), transition (column),
        rank (position). Stops at the first failure; the board is then re-read from Jira."""
        if not steps:
            self._pending.discard(key)
            self.reload()
            return
        kind, arg = steps[0]
        rest = steps[1:]
        if kind == 'edit':
            def edited(issue):
                self.window.issue_updated(issue)
                self._next_step(key, rest, True)
            self.store.edit_fields(key, arg, edited, lambda e: self._failed(key, f'Could not change {key}: {e}'))
        elif kind == 'transition':
            self._transition_into(key, arg, lambda: self._next_step(key, rest, True))
        else:
            field = ((self.state or {}).get('config') or {}).get('ranking', {}).get('rankCustomFieldId')
            prefix = f'{key} was changed, but could not be ranked there' if done_any else f'Could not move {key}'
            self.store.rank(key, arg, field, lambda _r: self._next_step(key, rest, True),
                            lambda e: self._failed(key, f'{prefix}: {e}'))

    def _transition_into(self, key, column, then):
        issue = self.issues.get(key) or {'key': key, 'fields': {}}
        status = ((issue.get('fields') or {}).get('status') or {}).get('name') or 'its status'

        def got(transitions):
            options = board_logic.transitions_into(transitions, column)
            if not options:
                self._failed(key, f'{key} cannot move from {status} to “{column["name"]}” '
                                  'in one step (no such transition in the workflow)')
            elif len(options) == 1:
                self._transition(key, options[0], then)
            else:
                self._choose(key, column['name'], options, then)
        self.store.transitions(key, got, lambda e: self._failed(key, f'Could not move {key}: {e}'))

    def _choose(self, key, column, options, then):
        dlg = Adw.AlertDialog(heading=f'Move {key} to {column}',
                              body='Several workflow transitions lead to this column. Choose one.')
        dlg.add_response('cancel', 'Cancel')
        for t in options:
            target = (t.get('to') or {}).get('name') or ''
            dlg.add_response(f't{t["id"]}', t['name'] if t['name'] == target else f'{t["name"]} → {target}')
        dlg.set_close_response('cancel')

        def response(_d, rid):
            chosen = next((t for t in options if f't{t["id"]}' == rid), None)
            if chosen:
                self._transition(key, chosen, then)
            else:
                self._failed(key, None)
        dlg.connect('response', response)
        dlg.present(self.get_root())

    def _transition(self, key, t, then):
        issue = self.issues.get(key) or {'fields': {}}
        target = (t.get('to') or {}).get('name') or t['name']

        def done(updated):
            self.window.toast(f'{key} moved to {target}')
            self.window.issue_updated(updated)
            then()

        TransitionFlow(self, self.store, key, issue.get('fields'), t, done,
                       lambda message: self._failed(key, message)).start()

    def _failed(self, key, message):
        if message:
            self.window.toast(message)
        self._pending.discard(key)
        self.reload()  # put the card back where Jira has it
