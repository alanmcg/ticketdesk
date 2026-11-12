# SPDX-License-Identifier: GPL-2.0-or-later
"""Main window.

Lists: sidebar | ticket list | ticket, side by side with draggable dividers.
Boards: sidebar | board, with the ticket in a panel under the board (opened by clicking a
card), so the columns keep the full width.
Narrow windows use an overlay sidebar, then one pane at a time.

Back/forward (Alt+Left/Right, mouse buttons) restore exactly where you were: the list or
board, its search text and scroll position, and the open ticket and its scroll position.
"""

import urllib.parse

from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango

from ..const import APP_NAME, URI_SCHEME
from ..core.api import OfflineError
from ..core.boards import LANE_MODES
from ..core import links
from ..core.config import read_extension_settings
from ..core.search import jql_string
from .board_view import BoardView
from .filter_bar import FilterBar
from .issue_list import IssueList
from .issue_view import IssueView
from .notifications import Bell
from .util import label, open_uri

HISTORY_MAX = 100


class _SizeClass(GObject.Object):
    """Set by the window's breakpoints; together with list/board mode it picks the layout."""
    size = GObject.Property(type=str, default='wide')


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        cfg = app.config
        super().__init__(application=app, title=APP_NAME, default_width=cfg.window_width,
                         default_height=cfg.window_height)
        self.app = app
        self.store = app.store
        self.view = 'mine'
        self.filter = None
        self.project = None
        self.custom = None
        self.board = None
        self.projects = []
        self.project_boards = []
        self.current_jql = None
        self._list_serial = 0
        self._last_issues = []
        self._applying = False
        self._last_size = {}
        self._back = []
        self._forward = []
        self._restoring = False
        self._pending_list_scroll = None
        self._suppress_search = None

        self.issue_view = IssueView(self)
        self.issue_view.set_size_request(320, 200)
        self.sidebar_pane = self._build_sidebar()
        self.list_pane = self._build_list_pane()
        self._build_layouts()
        self.toasts = Adw.ToastOverlay(child=self.layouts)
        self.set_content(self.toasts)
        self.set_size_request(360, 360)

        # Window size class; only one breakpoint applies at a time (the last matching).
        self.size_class = _SizeClass()
        for width, name in ((900, 'medium'), (660, 'narrow')):
            bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(f'max-width: {width}sp'))
            bp.add_setter(self.size_class, 'size', name)
            self.add_breakpoint(bp)
        self.size_class.connect('notify::size', lambda *_: self._apply_layout())
        self._apply_layout()

        self._install_actions()
        mouse = Gtk.GestureClick(button=0, propagation_phase=Gtk.PropagationPhase.CAPTURE)  # side buttons
        mouse.connect('pressed', self._mouse_button)
        self.add_controller(mouse)
        self.store.on_online_changed(self._online_changed)
        self.connect('close-request', self._on_close)
        self._update_nav()

    # ---------- sidebar ----------

    def _build_sidebar(self):
        header = self.sidebar_header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=APP_NAME))
        menu = Gio.Menu()
        menu.append('Preferences', 'app.preferences')
        menu.append(f'About {APP_NAME}', 'app.about')
        hide = Gtk.Button(icon_name='pan-start-symbolic', tooltip_text='Hide Sidebar (F9)', action_name='win.toggle-sidebar')
        header.pack_end(hide)
        header.pack_end(Gtk.MenuButton(icon_name='open-menu-symbolic', menu_model=menu, primary=True,
                                       tooltip_text='Main Menu'))

        self.bookmark_nav = Gtk.ListBox(css_classes=['navigation-sidebar'])
        self.bookmark_nav.connect('row-activated', self._nav_activated)
        self.bookmark_heading = self._heading('Bookmarks', top=6)
        self.nav = Gtk.ListBox(css_classes=['navigation-sidebar'])
        self.nav.connect('row-activated', self._nav_activated)
        self.board_nav = Gtk.ListBox(css_classes=['navigation-sidebar'])
        self.board_nav.connect('row-activated', self._nav_activated)
        # "All": the boards outside the configured projects, collapsed until opened.
        self.all_boards_toggle = Gtk.ListBox(css_classes=['navigation-sidebar'], selection_mode=Gtk.SelectionMode.NONE)
        toggle_row = Gtk.ListBoxRow()
        tbox = Gtk.Box(spacing=12, margin_start=6, margin_end=6)
        tbox.append(Gtk.Image.new_from_icon_name('view-grid-symbolic'))
        all_label = label('All', ellipsize=Pango.EllipsizeMode.END)
        all_label.set_hexpand(True)
        tbox.append(all_label)
        self.all_boards_arrow = Gtk.Image.new_from_icon_name('pan-end-symbolic')
        self.all_boards_arrow.add_css_class('dim-label')
        tbox.append(self.all_boards_arrow)
        toggle_row.set_child(tbox)
        toggle_row.set_tooltip_text('Boards outside your projects')
        toggle_row.update_property([Gtk.AccessibleProperty.LABEL], ['All other boards'])
        self.all_boards_toggle.append(toggle_row)
        self.all_boards_toggle.connect('row-activated', lambda *_: self._toggle_other_boards())
        self.other_nav = Gtk.ListBox(css_classes=['navigation-sidebar', 'nested'])
        self.other_nav.connect('row-activated', self._nav_activated)
        self.other_revealer = Gtk.Revealer(child=self.other_nav, reveal_child=False,
                                           transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._other_loaded = False
        self.other_boards = []
        self.filter_nav = Gtk.ListBox(css_classes=['navigation-sidebar'])
        self.filter_nav.connect('row-activated', self._nav_activated)
        self.filter_heading = self._heading('Saved Filters')
        self.filter_heading.set_visible(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.bookmark_heading)
        box.append(self.bookmark_nav)
        box.append(self._heading('Tickets', top=6))
        box.append(self.nav)
        box.append(self._heading('Boards'))
        box.append(self.board_nav)
        box.append(self.all_boards_toggle)
        box.append(self.other_revealer)
        box.append(self.filter_heading)
        box.append(self.filter_nav)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, child=box)
        self.site_label = label('', css=('dim-label', 'caption'), wrap=True)
        self.site_label.set_margin_start(18)
        self.site_label.set_margin_end(18)
        self.site_label.set_margin_bottom(12)

        view = Adw.ToolbarView(width_request=180, css_classes=['td-sidebar'])
        view.add_top_bar(header)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(scroller)
        content.append(self.site_label)
        view.set_content(content)
        self._fill_tickets_nav()
        self._fill_bookmarks()
        return view

    @staticmethod
    def _heading(text, top=12):
        h = label(text, css=('heading', 'dim-label'))
        h.set_margin_start(18)
        h.set_margin_top(top)
        h.set_margin_bottom(2)
        return h

    def _nav_row(self, title, icon, vid=None, filt=None, board=None, subtitle=None, project=None, bookmark=None):
        """A sidebar row. `bookmark` (a project or board entry) adds a star that appears on
        hover; starring moves the row to Bookmarks, where the star stays visible."""
        row = Gtk.ListBoxRow()
        box = Gtk.Box(spacing=12, margin_start=6, margin_end=2)
        box.append(Gtk.Image.new_from_icon_name(icon))
        name = label(title, ellipsize=Pango.EllipsizeMode.END)
        name.set_hexpand(True)
        name.set_tooltip_text(title)
        box.append(name)
        if subtitle:
            box.append(label(subtitle, css=('dim-label', 'caption')))
        if bookmark is not None:
            on = self.app.config.is_bookmarked(bookmark)
            star = Gtk.Button(icon_name='starred-symbolic' if on else 'non-starred-symbolic',
                              css_classes=['flat', 'circular', 'bookmark-star'] + (['active'] if on else []),
                              valign=Gtk.Align.CENTER,
                              tooltip_text='Remove from Bookmarks' if on else 'Add to Bookmarks')
            # rebuilt rows replace this button, so do it after the click has been handled
            star.connect('clicked', lambda *_: GLib.idle_add(lambda: self._toggle_bookmark(bookmark) and False))
            box.append(star)
        row.set_child(box)
        row.vid = vid
        row.filt = filt
        row.board = board
        row.project = project
        return row

    # ----- tickets section -----

    def set_projects(self, projects):
        self.projects = projects
        self._fill_tickets_nav()
        self._fill_bookmarks()

    def _fill_tickets_nav(self):
        """Tickets: Mine, All projects, one row per configured project (unless bookmarked),
        Recently Viewed."""
        while (row := self.nav.get_row_at_index(0)) is not None:
            self.nav.remove(row)
        cfg = self.app.config
        rows = [self._nav_row('Mine', 'avatar-default-symbolic', vid='mine'),
                self._nav_row('All projects', 'view-list-symbolic', vid='all')]
        for p in self.projects:
            entry = {'kind': 'project', 'key': p['key'], 'name': p.get('name') or ''}
            if cfg.is_bookmarked(entry):
                continue
            rows.append(self._project_row(entry))
        rows.append(self._nav_row('Recently Viewed', 'document-open-recent-symbolic', vid='recent'))
        for row in rows:
            self.nav.append(row)
        self._select_nav_rows()

    def _project_row(self, entry):
        return self._nav_row(entry.get('name') or entry['key'], 'folder-symbolic', project=entry['key'],
                             subtitle=entry['key'] if entry.get('name') else None, bookmark=entry)

    # ----- boards section -----

    def set_boards(self, boards):
        self.project_boards = boards
        self._fill_boards(self.board_nav, boards)
        if self._other_loaded:
            self._load_other_boards()
        self._fill_bookmarks()

    def _fill_boards(self, nav, boards):
        while (row := nav.get_row_at_index(0)) is not None:
            nav.remove(row)
        cfg = self.app.config
        multi = len({b['project'] for b in boards}) > 1 or nav is self.other_nav
        for b in boards:
            entry = {'kind': 'board', **b}
            if cfg.is_bookmarked(entry):
                continue
            nav.append(self._board_row(entry, subtitle=b['project'] if multi else None))
        if nav is self.other_nav and nav.get_row_at_index(0) is None:
            self._placeholder_row(nav, 'No other boards')
        self._select_nav_rows()

    def _board_row(self, entry, subtitle=None):
        board = {k: v for k, v in entry.items() if k != 'kind'}
        return self._nav_row(entry.get('name') or f'Board {entry["id"]}', 'view-grid-symbolic', board=board,
                             subtitle=subtitle, bookmark=entry)

    def _toggle_other_boards(self):
        reveal = not self.other_revealer.get_reveal_child()
        self.other_revealer.set_reveal_child(reveal)
        self.all_boards_arrow.set_from_icon_name('pan-down-symbolic' if reveal else 'pan-end-symbolic')
        if reveal and not self._other_loaded:
            self._other_loaded = True
            self._placeholder_row(self.other_nav, 'Loading boards…')
            self._load_other_boards()

    def _load_other_boards(self):
        def got(boards):
            self.other_boards = boards
            self._fill_boards(self.other_nav, boards)

        def failed(e):
            self._placeholder_row(self.other_nav, f'Could not load boards: {e}')
            self._other_loaded = False  # try again when opened next time
        self.store.other_boards([b['id'] for b in self.project_boards], got, failed)

    def _placeholder_row(self, nav, text):
        while (row := nav.get_row_at_index(0)) is not None:
            nav.remove(row)
        lbl = label(text, css=('dim-label', 'caption'), wrap=True)
        lbl.set_margin_start(12)
        nav.append(Gtk.ListBoxRow(child=lbl, activatable=False, selectable=False))

    # ----- bookmarks -----

    def _fill_bookmarks(self):
        while (row := self.bookmark_nav.get_row_at_index(0)) is not None:
            self.bookmark_nav.remove(row)
        names = {p['key']: p.get('name') for p in self.projects}
        for entry in self.app.config.bookmarks:
            if entry['kind'] == 'project':
                entry = {**entry, 'name': names.get(entry['key']) or entry.get('name') or ''}
                self.bookmark_nav.append(self._project_row(entry))
            elif entry['kind'] == 'board':
                self.bookmark_nav.append(self._board_row(entry, subtitle=entry.get('project') or None))
        has = bool(self.app.config.bookmarks)
        self.bookmark_heading.set_visible(has)
        self.bookmark_nav.set_visible(has)
        self._select_nav_rows()

    def _toggle_bookmark(self, entry):
        cfg = self.app.config
        cfg.toggle_bookmark(entry)
        cfg.save()
        self._fill_tickets_nav()
        self._fill_boards(self.board_nav, self.project_boards)
        if self._other_loaded:
            self._fill_boards(self.other_nav, self.other_boards)
        self._fill_bookmarks()

    # ----- saved filters -----

    def set_filters(self, filters):
        while (row := self.filter_nav.get_row_at_index(0)) is not None:
            self.filter_nav.remove(row)
        for f in filters:
            self.filter_nav.append(self._nav_row(f['name'], 'starred-symbolic', filt=f))
        self.filter_heading.set_visible(bool(filters))
        self._select_nav_rows()

    # ----- selection follows the current place -----

    def _navs(self):
        return (self.bookmark_nav, self.nav, self.board_nav, self.other_nav, self.filter_nav)

    def _row_is_current(self, row):
        if getattr(row, 'board', None):
            return self.board is not None and row.board['id'] == self.board['id']
        if self.board is not None:
            return False
        if getattr(row, 'filt', None):
            return self.view == 'filter' and self.filter is not None and row.filt['id'] == self.filter['id']
        if getattr(row, 'project', None):
            return self.view == 'project' and row.project == self.project
        return getattr(row, 'vid', None) is not None and row.vid == self.view

    def _select_nav_rows(self):
        """Highlight the sidebar row of the current list or board (one row at most)."""
        found = False
        for nav in self._navs():
            match = None
            i = 0
            while (row := nav.get_row_at_index(i)) is not None:
                if not found and row.get_selectable() and self._row_is_current(row):
                    match = row
                i += 1
            if match is not None:
                nav.select_row(match)
                found = True
            else:
                nav.unselect_all()

    # ---------- list pane ----------

    def _build_list_pane(self):
        header = self.list_header = Adw.HeaderBar()
        self.show_sidebar_button = Gtk.Button(icon_name='pan-end-symbolic', tooltip_text='Show Sidebar (F9)',
                                              action_name='win.toggle-sidebar')
        header.pack_start(self.show_sidebar_button)
        self.back_button = Gtk.Button(icon_name='go-previous-symbolic', action_name='win.back')
        self.forward_button = Gtk.Button(icon_name='go-next-symbolic', action_name='win.forward')
        nav = Gtk.Box(css_classes=['linked'])
        nav.append(self.back_button)
        nav.append(self.forward_button)
        header.pack_start(nav)
        header.pack_start(Gtk.Button(icon_name='list-add-symbolic', tooltip_text='New Ticket (Ctrl+N)',
                                     action_name='win.new-ticket'))
        self.search = Gtk.SearchEntry(placeholder_text='Search (ABC-, ABC-12 or text)', hexpand=True, search_delay=400)
        self.search.connect('search-changed', self._search_changed)
        header.set_title_widget(self.search)

        self.list_menu = Gio.Menu()
        self.list_menu.append('Refresh', 'win.refresh')
        self.list_menu.append('Open List in Browser', 'win.open-list-in-browser')
        self.board_menu = Gio.Menu()
        lanes = Gio.Menu()
        for mode, title in LANE_MODES:
            lanes.append(title, f'win.swimlanes::{mode}')
        self.board_menu.append_section('Swimlanes', lanes)
        more = Gio.Menu()
        more.append('Refresh', 'win.refresh')
        more.append('Open Board in Browser', 'win.open-board-in-browser')
        self.board_menu.append_section(None, more)
        self.more_button = Gtk.MenuButton(icon_name='view-more-symbolic', menu_model=self.list_menu,
                                          tooltip_text='More')
        header.pack_end(self.more_button)
        self.bell = Bell(self)
        self.filter_toggle = Gtk.ToggleButton(icon_name='view-sort-descending-symbolic',
                                              tooltip_text='Filters and Sort (Ctrl+Shift+F)',
                                              active=self.app.config.filters_visible)
        header.pack_end(self.filter_toggle)
        header.pack_end(self.bell)

        self.filter_bar = FilterBar(self.app.config)
        self.filter_bar.connect('changed', lambda *_: (self.reload_list(), self._refresh_info()))
        self.filter_revealer = Gtk.Revealer(child=self.filter_bar, reveal_child=self.app.config.filters_visible)
        self.filter_toggle.connect('toggled', self._filters_toggled)

        self.banner = Adw.Banner(title='Offline — showing cached data', button_label='Retry')
        self.banner.connect('button-clicked', lambda *_: self.refresh())

        self.list_info = label('', css=('dim-label', 'caption'), ellipsize=Pango.EllipsizeMode.END)
        self.list_info.set_margin_start(14)
        self.list_info.set_margin_end(14)
        self.list_info.set_margin_top(4)
        self.list_info.set_margin_bottom(4)

        self.issue_list = IssueList()
        self.issue_list.connect('activated', lambda _l, key: self.show_issue(key))

        self.list_stack = Gtk.Stack(vexpand=True)
        self.list_stack.add_named(self.issue_list, 'list')
        self.list_stack.add_named(Adw.StatusPage(title='No Results', icon_name='edit-find-symbolic',
                                                 description='Try a different search or show more statuses'), 'empty')
        spinner = Adw.Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, width_request=32, height_request=32)
        self.list_stack.add_named(spinner, 'loading')
        self.list_error = Adw.StatusPage(icon_name='dialog-warning-symbolic')
        self.list_stack.add_named(self.list_error, 'error')
        self.list_stack.add_named(self._setup_page(), 'setup')
        self.board_view = BoardView(self)
        self.board_view.connect('activated', lambda _b, key: self.show_issue(key))
        self.board_view.connect('info', lambda _b, text: self.board is not None and self.list_info.set_text(text))
        self.list_stack.add_named(self.board_view, 'board')
        self.list_stack.connect('notify::visible-child-name', lambda *_: self.list_info.set_visible(
            self.list_stack.get_visible_child_name() in ('list', 'board')))

        view = Adw.ToolbarView(width_request=300, height_request=160, css_classes=['td-middle'])
        view.add_top_bar(header)
        view.add_top_bar(self.banner)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self.filter_revealer)
        content.append(self.list_info)
        content.append(self.list_stack)
        view.set_content(content)
        return view

    def _setup_page(self):
        page = Adw.StatusPage(title='Connect to Jira', icon_name='network-server-symbolic',
                              description='Enter your Jira Cloud site, email and API token in Preferences.')
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        prefs = Gtk.Button(label='Open Preferences', css_classes=['pill', 'suggested-action'])
        prefs.set_action_name('app.preferences')
        box.append(prefs)
        self.import_button = Gtk.Button(label='Use Top Bar Extension Settings', css_classes=['pill'])
        self.import_button.connect('clicked', lambda *_: self.app.import_extension_settings())
        self.import_button.set_visible(bool(read_extension_settings()))
        box.append(self.import_button)
        page.set_child(box)
        return page

    # ---------- pane layouts ----------

    def _paned(self, start, end, position, which, vertical=False):
        """Two panes with a draggable divider. Horizontal: the start pane keeps its width
        when the window is resized. Vertical (ticket under a board): the end pane keeps its height."""
        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL,
                          start_child=start, end_child=end,
                          resize_start_child=vertical, shrink_start_child=False,
                          resize_end_child=not vertical, shrink_end_child=False)
        if position:
            paned.set_position(position)
        paned.connect('notify::position', self._paned_moved, which)
        return paned

    def _build_layouts(self):
        """wide: sidebar | list | ticket. medium: overlay sidebar, list | ticket. narrow: one
        pane at a time. board-wide / board-medium: the board above, the ticket below it."""
        cfg = self.app.config
        self.layouts = Adw.MultiLayoutView()
        self.layouts.set_child('sidebar', self.sidebar_pane)
        self.layouts.set_child('list', self.list_pane)
        self.layouts.set_child('ticket', self.issue_view)
        self.sidebar_slots = {}
        self.ticket_slots = {}

        def layout(name, content):
            lay = Adw.Layout.new(content)
            lay.set_name(name)
            self.layouts.add_layout(lay)

        def sidebar_slot(name):
            self.sidebar_slots[name] = Adw.LayoutSlot.new('sidebar')
            return self.sidebar_slots[name]

        def ticket_slot(name):
            self.ticket_slots[name] = Adw.LayoutSlot.new('ticket')
            return self.ticket_slots[name]

        self.wide_inner = self._paned(Adw.LayoutSlot.new('list'), ticket_slot('wide'), cfg.list_width, 'list')
        self.wide_outer = self._paned(sidebar_slot('wide'), self.wide_inner, cfg.sidebar_width, 'sidebar')
        layout('wide', self.wide_outer)

        self.medium_inner = self._paned(Adw.LayoutSlot.new('list'), ticket_slot('medium'), cfg.list_width, 'list')
        self.medium_overlay = Adw.OverlaySplitView(collapsed=True, sidebar=sidebar_slot('medium'),
                                                   content=self.medium_inner)
        layout('medium', self.medium_overlay)

        self.nav_split = Adw.NavigationSplitView(
            collapsed=True,
            sidebar=Adw.NavigationPage(title='Tickets', child=Adw.LayoutSlot.new('list')),
            content=Adw.NavigationPage(title='Ticket', child=ticket_slot('narrow')))
        self.narrow_overlay = Adw.OverlaySplitView(collapsed=True, sidebar=sidebar_slot('narrow'),
                                                   content=self.nav_split)
        layout('narrow', self.narrow_overlay)

        self.board_wide_inner = self._paned(Adw.LayoutSlot.new('list'), ticket_slot('board-wide'), 0,
                                            'ticket-height', vertical=True)
        self.board_wide_outer = self._paned(sidebar_slot('board-wide'), self.board_wide_inner, cfg.sidebar_width,
                                            'sidebar')
        layout('board-wide', self.board_wide_outer)

        self.board_medium_inner = self._paned(Adw.LayoutSlot.new('list'), ticket_slot('board-medium'), 0,
                                              'ticket-height', vertical=True)
        self.board_overlay = Adw.OverlaySplitView(collapsed=True, sidebar=sidebar_slot('board-medium'),
                                                  content=self.board_medium_inner)
        layout('board-medium', self.board_overlay)

        for ov in (self.medium_overlay, self.narrow_overlay, self.board_overlay):
            ov.connect('notify::show-sidebar', lambda *_: self._update_sidebar_controls())
        self.layouts.set_layout_name('wide')

    def _layout(self):
        return self.layouts.get_layout_name() or 'wide'

    def _wanted_layout(self):
        size = self.size_class.size
        if self.board is None or size == 'narrow':
            return size
        return f'board-{size}'

    def _overlay(self):
        return {'medium': self.medium_overlay, 'narrow': self.narrow_overlay,
                'board-medium': self.board_overlay}.get(self._layout())

    def _is_board_layout(self):
        return self._layout().startswith('board-')

    def _apply_layout(self):
        name = self._wanted_layout()
        if name != self._layout():
            self.layouts.set_layout_name(name)
        cfg = self.app.config
        paned_sidebar = name in ('wide', 'board-wide')
        for slot_name, slot in self.sidebar_slots.items():
            slot.set_visible(cfg.sidebar_visible if slot_name in ('wide', 'board-wide') else True)
        self.sidebar_pane.set_visible(cfg.sidebar_visible if paned_sidebar else True)
        for ov in (self.medium_overlay, self.narrow_overlay, self.board_overlay):
            ov.set_show_sidebar(False)
        self._update_ticket_visibility()
        self._apply_pane_sizes()
        self._update_sidebar_controls()

    def _update_ticket_visibility(self):
        """Under a board the ticket panel exists only while a ticket is open."""
        board = self._is_board_layout()
        visible = not board or self.issue_view.key is not None
        self.issue_view.set_visible(visible)
        for name, slot in self.ticket_slots.items():
            slot.set_visible(visible if name.startswith('board-') else True)
        self.issue_view.set_closable(board)

    def _toggle_sidebar(self, *_):
        ov = self._overlay()
        if ov is not None:
            ov.set_show_sidebar(not ov.get_show_sidebar())
            return
        cfg = self.app.config
        cfg.sidebar_visible = not cfg.sidebar_visible
        self.sidebar_pane.set_visible(cfg.sidebar_visible)
        for name in ('wide', 'board-wide'):
            self.sidebar_slots[name].set_visible(cfg.sidebar_visible)
        self._update_sidebar_controls()

    def _update_sidebar_controls(self):
        """Show the 'show sidebar' button only when the sidebar is hidden, and put the window
        buttons on the outermost header bars (Gtk.Paned does not do that like split views do)."""
        layout = self._layout()
        ov = self._overlay()
        shown = self.app.config.sidebar_visible if ov is None else ov.get_show_sidebar()
        self.show_sidebar_button.set_visible(not shown)
        ticket = self.issue_view.header
        sides = {
            'wide': {self.sidebar_header: (True, False), self.list_header: (not shown, False), ticket: (False, True)},
            'medium': {self.sidebar_header: (True, True), self.list_header: (True, False), ticket: (False, True)},
            'board-wide': {self.sidebar_header: (True, False), self.list_header: (not shown, True), ticket: (False, False)},
            'board-medium': {self.sidebar_header: (True, True), self.list_header: (True, True), ticket: (False, False)},
        }.get(layout, {self.sidebar_header: (True, True), self.list_header: (True, True), ticket: (True, True)})
        for header, (start, end) in sides.items():
            header.set_show_start_title_buttons(start)
            header.set_show_end_title_buttons(end)

    def _apply_pane_sizes(self):
        cfg = self.app.config
        self._applying = True
        for paned in (self.wide_inner, self.medium_inner):
            paned.set_position(cfg.list_width)
        self._applying = False
        self._place_ticket_panel()

    def _place_ticket_panel(self):
        """Divider between a board and the ticket under it: the ticket keeps the height the
        user gave it (default: a bit under half the window)."""
        cfg = self.app.config
        height = self.get_height() or cfg.window_height
        ticket_h = cfg.board_ticket_height or int(height * 0.45)
        self._applying = True
        for paned in (self.board_wide_inner, self.board_medium_inner):
            total = paned.get_height() or height - 46
            paned.set_position(max(160, total - ticket_h))
        self._applying = False

    def _paned_moved(self, paned, _pspec, which):
        """Remember divider positions the user chose. Changes caused by resizing the window
        (Gtk.Paned keeps both panes at their minimum size) are not remembered."""
        vertical = which == 'ticket-height'
        size = self.get_height() if vertical else self.get_width()
        last = self._last_size.get(id(paned))
        self._last_size[id(paned)] = size
        if self._applying or not paned.get_mapped() or size != last:
            return
        cfg = self.app.config
        pos = paned.get_position()
        if which == 'sidebar':
            if cfg.sidebar_visible and pos > 0:
                cfg.sidebar_width = pos
            return
        if which == 'ticket-height':
            if self.issue_view.get_visible() and paned.get_height() > 0:
                cfg.board_ticket_height = max(160, paned.get_height() - pos)
            return
        cfg.list_width = pos
        other = self.medium_inner if paned is self.wide_inner else self.wide_inner
        self._applying = True
        other.set_position(pos)
        self._applying = False

    # ---------- actions ----------

    def _install_actions(self):
        for name, cb, accels in (('refresh', lambda *_: self.refresh(), ['F5', '<Control>r']),
                                 ('search', lambda *_: self.search.grab_focus(), ['<Control>f']),
                                 ('toggle-sidebar', self._toggle_sidebar, ['F9']),
                                 ('back', lambda *_: self.go_back(), ['<Alt>Left']),
                                 ('new-ticket', lambda *_: self.new_ticket(), ['<Control>n']),
                                 ('forward', lambda *_: self.go_forward(), ['<Alt>Right']),
                                 ('toggle-filters', lambda *_: self.filter_toggle.set_active(
                                     not self.filter_toggle.get_active()), ['<Control><Shift>f']),
                                 ('open-board-in-browser', lambda *_: self._open_board_in_browser(), []),
                                 ('open-list-in-browser', lambda *_: self._open_list_in_browser(), [])):
            act = Gio.SimpleAction.new(name, None)
            act.connect('activate', cb)
            self.add_action(act)
            if accels:
                self.app.set_accels_for_action(f'win.{name}', accels)
        self.lanes_action = Gio.SimpleAction.new_stateful('swimlanes', GLib.VariantType('s'), GLib.Variant('s', 'none'))
        self.lanes_action.connect('change-state', self._swimlanes_changed)
        self.add_action(self.lanes_action)

    def _mouse_button(self, gesture, _n, _x, _y):
        button = gesture.get_current_button()
        if button == 8:
            self.go_back()
        elif button == 9:
            self.go_forward()

    def _swimlanes_changed(self, action, value):
        action.set_state(value)
        self.board_view.set_swimlanes(value.get_string())

    def _filters_toggled(self, button):
        on = button.get_active()
        self.filter_revealer.set_reveal_child(on)
        self.app.config.filters_visible = on
        self._refresh_info()

    def _open_list_in_browser(self):
        if self.current_jql and self.store.client:
            open_uri(self, self.store.client.search_url(self.current_jql))

    def _open_board_in_browser(self):
        url = self.board_view.browser_url()
        if url:
            open_uri(self, url)

    # ---------- navigation and history ----------

    def _snapshot(self):
        key = self.issue_view.key
        return {'view': self.view, 'filter': self.filter, 'project': self.project, 'custom': self.custom,
                'board': self.board, 'search': self.search.get_text(), 'key': key,
                'scroll': self.issue_view.scroll_position() if key else 0,
                'list_scroll': self.issue_list.get_vadjustment().get_value()}

    @staticmethod
    def _place_id(p):
        return (p['view'], (p['filter'] or {}).get('id'), p['project'], (p['custom'] or {}).get('jql'),
                (p['board'] or {}).get('id'), p['search'], p['key'])

    def _navigate(self, change):
        """Run `change` (which moves somewhere else); remember where we were for Back."""
        if self._restoring:
            change()
            return
        before = self._snapshot()
        change()
        if self._place_id(before) != self._place_id(self._snapshot()):
            self._back = (self._back + [before])[-HISTORY_MAX:]
            self._forward = []
        self._update_nav()

    def go_back(self):
        if self._back:
            self._forward.append(self._snapshot())
            self._restore(self._back.pop())

    def go_forward(self):
        if self._forward:
            self._back.append(self._snapshot())
            self._restore(self._forward.pop())

    def _describe(self, p):
        if p['key']:
            return p['key']
        if p['board']:
            return p['board'].get('name') or 'board'
        if p['custom']:
            return p['custom'].get('title')
        if p['filter']:
            return p['filter'].get('name')
        if p['view'] == 'project':
            return p['project']
        return {'mine': 'Mine', 'all': 'All projects', 'recent': 'Recently Viewed'}.get(p['view'], 'list')

    def _update_nav(self):
        back = self._describe(self._back[-1]) if self._back else None
        fwd = self._describe(self._forward[-1]) if self._forward else None
        self.lookup_action('back').set_enabled(back is not None)
        self.lookup_action('forward').set_enabled(fwd is not None)
        self.back_button.set_tooltip_text(f'Back to {back} (Alt+Left)' if back else 'Back (Alt+Left)')
        self.forward_button.set_tooltip_text(f'Forward to {fwd} (Alt+Right)' if fwd else 'Forward (Alt+Right)')

    def _restore(self, p):
        self._restoring = True
        try:
            same_list = (self.view, (self.filter or {}).get('id'), self.project, (self.custom or {}).get('jql'),
                         (self.board or {}).get('id'), self.search.get_text()) == \
                        (p['view'], (p['filter'] or {}).get('id'), p['project'], (p['custom'] or {}).get('jql'),
                         (p['board'] or {}).get('id'), p['search'])
            self.view, self.filter, self.project, self.custom = p['view'], p['filter'], p['project'], p['custom']
            if self.search.get_text() != p['search']:
                self._suppress_search = p['search']
                self.search.set_text(p['search'])
            board_changed = (self.board or {}).get('id') != (p['board'] or {}).get('id')
            if board_changed:
                self._set_board(p['board'], keep_ticket=True)  # loads the board
            self._select_nav_rows()
            if not same_list and p['board'] is None:
                self._pending_list_scroll = p['list_scroll']
                self.reload_list()
            if p['key']:
                self._open_ticket(p['key'], scroll=p['scroll'])
            else:
                self._close_ticket()
        finally:
            self._restoring = False
        self._update_nav()

    def _nav_activated(self, box, row):
        if self._overlay() is not None:
            self._overlay().set_show_sidebar(False)

        def change():
            if row.board:
                self._set_board(row.board)
            else:
                self.filter = self.project = self.custom = None
                if row.filt:
                    self.view, self.filter = 'filter', row.filt
                elif row.project:
                    self.view, self.project = 'project', row.project
                else:
                    self.view = row.vid
                self._set_board(None)
                self.reload_list()
            self._select_nav_rows()
        self._navigate(change)

    def show_children(self, key):
        """Drill down: list the children of a ticket (an epic's tickets, a ticket's sub-tasks)."""
        def change():
            self.view, self.filter, self.project = 'custom', None, None
            self.custom = {'jql': f'parent = {jql_string(key)}', 'title': f'Children of {key}'}
            self._set_board(None, keep_ticket=True)
            if self.search.get_text():
                self._suppress_search = ''
                self.search.set_text('')
            self._select_nav_rows()
            self.reload_list()
        self._navigate(change)
        if self._layout() == 'narrow':
            self.nav_split.set_show_content(False)

    def _set_board(self, board, keep_ticket=False):
        """Switch between lists and boards (or to another board). The open ticket belongs
        to the previous view, so it is closed unless `keep_ticket`."""
        was_board = self.board is not None
        changed = (self.board or {}).get('id') != (board or {}).get('id')
        self.board = board
        if changed and not keep_ticket:
            self._close_ticket()
        self.filter_toggle.set_visible(board is None)
        self.filter_revealer.set_visible(board is None)
        self.more_button.set_menu_model(self.board_menu if board else self.list_menu)
        self.search.set_placeholder_text('Filter cards' if board else 'Search (ABC-, ABC-12 or text)')
        if was_board != (board is not None) and self.search.get_text() and not self._restoring:
            self._suppress_search = ''
            self.search.set_text('')  # a list search makes no sense as a card filter, and back
        self.board_view.set_filter(self.search.get_text() if board else '')
        if board:
            self._list_serial += 1  # results of a list request still under way are not shown
            self.lanes_action.set_state(GLib.Variant('s', self.app.config.swimlanes.get(str(board['id']), 'none')))
            self.list_stack.set_visible_child_name('board')
            self.board_view.select(self.issue_view.key)
            self.board_view.show(board)
        elif was_board:
            self.list_stack.set_visible_child_name('list' if self.issue_list.count() else 'loading')
        self._apply_layout()

    def _search_changed(self, *_):
        text = self.search.get_text()
        if getattr(self, '_suppress_search', None) == text:
            self._suppress_search = None  # set by us; the list is already being loaded
            if self.board is not None:
                self.board_view.set_filter(text)
            return
        self._suppress_search = None
        if self.board is not None:
            self.board_view.set_filter(text)
        else:
            self.reload_list()

    # ----- tickets -----

    def show_issue(self, key):
        """Open a ticket chosen in the list or on the board."""
        if key and key == self.issue_view.key:
            return
        self._navigate(lambda: self._open_ticket(key))

    def open_related(self, key):
        """Open a ticket reached from the open one (parent, child, link)."""
        self._navigate(lambda: self._open_ticket(key))

    def open_key(self, key):
        self.open_related(key)

    def close_ticket(self):
        self._navigate(self._close_ticket)

    def _open_ticket(self, key, scroll=None):
        self.app.config.add_recent(key)
        self.store.mark_notifications_read(key=key)  # opening a ticket reads its notifications
        self.bell.refresh()
        self.issue_list.select_key(key)
        self.board_view.select(key)
        first = not self.issue_view.get_visible()
        self.issue_view.show(key, scroll=scroll)
        self._update_ticket_visibility()
        if first and self._is_board_layout():
            self._place_ticket_panel()
        if self._layout() == 'narrow':
            self.nav_split.set_show_content(True)

    def _close_ticket(self):
        self.issue_view.clear()
        self.issue_list.select_key(None)
        self.board_view.select(None)
        self._update_ticket_visibility()
        if self._layout() == 'narrow':
            self.nav_split.set_show_content(False)

    def new_ticket(self):
        if not self.store.client:
            self.toast('Connect to Jira first (Preferences)')
            return
        from .create_dialog import CreateDialog
        # default: the preference, else the project being looked at
        context = self.project if self.view == 'project' else (self.board or {}).get('project') or None
        CreateDialog(self, project=self.app.config.create_project or context).present(self)

    def ticket_created(self, issue, warnings=()):
        key = issue.get('key')
        for w in warnings:
            self.toast(w)
        self.toast(f'Created {key}')
        self.reload_list()
        if key:
            self.open_related(key)

    def key_from_uri(self, uri):
        parts = urllib.parse.urlsplit(uri)
        if parts.scheme == URI_SCHEME:
            key = (parts.path or '').strip('/') if parts.netloc == 'browse' else parts.netloc or parts.path.strip('/')
            key = key.split('/')[-1].upper()
            return key if links.KEY_RE.match(key) else None
        return links.ticket_key(uri, links.host_of(self.app.config.site))

    # ---------- data ----------

    def on_connected(self):
        cfg = self.app.config
        self.site_label.set_text(urllib.parse.urlsplit(cfg.site).hostname or '')
        if not self.store.client:
            self.list_stack.set_visible_child_name('setup')
            self.import_button.set_visible(bool(read_extension_settings()))
            return
        self.store.ensure_me(lambda _me: self._rebind_rows())
        self.store.projects(self.set_projects)
        self.store.favourite_filters(self.set_filters)
        self.store.boards(self.set_boards)
        self.reload_list()
        self.bell.refresh()
        self.restart_timers()
        GLib.timeout_add_seconds(5, lambda: (self.check_notifications(), GLib.SOURCE_REMOVE)[1])

    # ---------- notifications and live view ----------

    def restart_timers(self):
        """(Re)start the notification check and live view timers with the current settings."""
        cfg = self.app.config
        for attr in ('_notify_timer', '_live_timer'):
            if getattr(self, attr, 0):
                GLib.source_remove(getattr(self, attr))
                setattr(self, attr, 0)
        if not self.store.client:
            return
        self._notify_timer = GLib.timeout_add_seconds(max(1, int(cfg.notify_interval)) * 60,
                                                      lambda: (self.check_notifications(), GLib.SOURCE_CONTINUE)[1])
        self._live_timer = GLib.timeout_add_seconds(max(10, int(cfg.live_interval)),
                                                    lambda: (self._live_tick(), GLib.SOURCE_CONTINUE)[1])
        self._refresh_info()

    def check_notifications(self, now=False):
        if not self.store.client or not self.store.online:
            return

        def got(new):
            self.bell.refresh()
            if any(e['kind'] == 'mention' for e in new):
                self._rebind_rows()  # the "mentioned you" marks in the list
            if new and self.app.config.notify_desktop and (now is False) and not self.is_active():
                self._desktop_notify(new)
        self.store.check_notifications(got)

    def _desktop_notify(self, events):
        if len(events) <= 5:
            for e in events:
                n = Gio.Notification.new(e['title'])
                if e.get('body'):
                    n.set_body(e['body'])
                n.set_default_action_and_target_value('app.open-ticket', GLib.Variant('s', e['key']))
                self.app.send_notification(f'td-{e["id"]}', n)
            return
        n = Gio.Notification.new(f'{len(events)} Jira updates')
        n.set_body('\n'.join(e['title'] for e in events[:6]) + ('\n…' if len(events) > 6 else ''))
        n.set_default_action_and_target_value('app.open-ticket', GLib.Variant('s', events[0]['key']))
        self.app.send_notification('td-summary', n)

    def live_active(self):
        """Live view: the list refreshes by itself, only while sorted by updated, newest first."""
        cfg = self.app.config
        return bool(cfg.live_view and cfg.sort == 'updated' and cfg.sort_desc and self.board is None
                    and self.store.client)

    def _live_tick(self):
        if not self.live_active() or not self.store.online or not self.get_mapped() or self.is_suspended():
            return
        if self.list_stack.get_visible_child_name() not in ('list', 'empty'):
            return
        self._pending_list_scroll = self.issue_list.get_vadjustment().get_value()  # stay where the reader is
        self.reload_list()

    def refresh(self):
        self.reload_list()
        if self.issue_view.key:
            self.issue_view.show(self.issue_view.key, scroll=self.issue_view.scroll_position())
        if self.store.client:
            self.store.projects(self.set_projects)
            self.store.favourite_filters(self.set_filters)
            self.store.boards(self.set_boards)

    def reload_list(self):
        if not self.store.client:
            self.list_stack.set_visible_child_name('setup')
            return
        if self.board is not None:
            self.board_view.reload()
            return
        self._list_serial += 1
        serial = self._list_serial
        text = self.search.get_text()
        recent_mode = self.view == 'recent'
        if recent_mode:
            keys = [k for k in self.app.config.recent if links.KEY_RE.match(k)][:100]
            self._show_issues(self.store.recent(), 'Recently viewed', cached=True, keep_order=True)
            if not keys:
                return
            view, filter_jql = 'filter', f'key in ({", ".join(keys)})'
        elif self.view == 'filter' and self.filter:
            view, filter_jql = 'filter', self.filter['jql']
        elif self.view == 'custom' and self.custom:
            view, filter_jql = 'filter', self.custom['jql']
        elif self.view in ('all', 'project'):
            view, filter_jql = self.view, None
        else:
            view, filter_jql = 'mine', None
        default_title = {'mine': 'My tickets', 'recent': 'Recently viewed', 'all': 'All projects',
                         'project': f'All tickets in {self.project}'}.get(self.view) \
            or (self.filter or {}).get('name') or (self.custom or {}).get('title', '')
        if self.issue_list.count() == 0 and not recent_mode:
            self.list_stack.set_visible_child_name('loading')

        def cached(issues, _fetched, _jql, title):
            if serial == self._list_serial and not recent_mode:
                self._show_issues(issues, title or default_title, cached=True)

        def fresh(issues, jql, title):
            if serial != self._list_serial:
                return
            self.current_jql = jql
            if recent_mode and not text:
                order = {k: i for i, k in enumerate(self.app.config.recent)}
                issues = sorted(issues, key=lambda it: order.get(it['key'], 1 << 30))
            self._show_issues(issues, title or default_title, cached=False, keep_order=recent_mode)

        def failed(e):
            if serial != self._list_serial:
                return
            if isinstance(e, OfflineError):
                if self.list_stack.get_visible_child_name() == 'loading':
                    self.list_error.set_title('Offline')
                    self.list_error.set_description('Nothing cached for this list yet.')
                    self.list_stack.set_visible_child_name('error')
                return
            if self.issue_list.count():
                self.toast(str(e))
            else:
                self.list_error.set_title('Could Not Load Tickets')
                self.list_error.set_description(GLib.markup_escape_text(str(e)))
                self.list_stack.set_visible_child_name('error')
        self.current_jql = self.store.load_list(view, '' if recent_mode and not text else text, filter_jql,
                                                cached, fresh, failed, project=self.project,
                                                created=self.filter_bar.created())

    def _show_issues(self, issues, title, cached, keep_order=False):
        self._last_issues = issues
        self._info = (title, len(issues), cached, keep_order)
        self.issue_list.set_issues(issues, self.store.me.get('accountId'), self.store.mentioned,
                                   selected_key=self.issue_view.key)
        self._refresh_info()
        self.list_stack.set_visible_child_name('list' if issues else 'empty')
        if self._pending_list_scroll is not None:
            pos = self._pending_list_scroll
            if not cached:
                self._pending_list_scroll = None
            adj =self.issue_list.get_vadjustment()
            GLib.idle_add(lambda: (adj.set_value(pos), GLib.SOURCE_REMOVE)[1])

    def _refresh_info(self):
        """Info line above the list: what is listed; the filters too when the bar is hidden."""
        if self.board is not None or not hasattr(self, '_info'):
            return
        title, n, cached, keep_order = self._info
        parts = [title, f'{n} tickets']
        if not self.filter_toggle.get_active():
            parts += self.filter_bar.describe()
            if not keep_order:
                parts.append(self.filter_bar.sort_description())
        if cached:
            parts.append('cached' if not self.store.online else 'updating…')
        if self.live_active():
            parts.append(f'live, every {self.app.config.live_interval} s')
        self.list_info.set_text(' · '.join(p for p in parts if p))

    def _rebind_rows(self):
        if self._last_issues:
            self.issue_list.set_issues(self._last_issues, self.store.me.get('accountId'), self.store.mentioned,
                                       selected_key=self.issue_view.key)

    def issue_changed(self, _issue):
        """A ticket was changed from the ticket view: refresh the list or board around it."""
        self.reload_list()

    def issue_updated(self, issue):
        """A ticket was changed from a board: refresh the ticket view if it shows that ticket."""
        if issue and self.issue_view.key == issue.get('key'):
            self.issue_view.show(issue['key'], scroll=self.issue_view.scroll_position())

    def _online_changed(self, online):
        self.banner.set_revealed(not online)
        self.issue_view.set_online(online)

    # ---------- misc ----------

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(str(text)), timeout=4))

    def _on_close(self, *_):
        cfg = self.app.config
        if not self.is_maximized():
            cfg.window_width, cfg.window_height = self.get_width(), self.get_height()
        cfg.save()
        return False
