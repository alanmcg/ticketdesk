# Ticket Desk

A fast, native GNOME client for Jira Cloud, built with GTK 4 and libadwaita. Companion
to the *Ticket Panel for Jira* top bar extension.

Not affiliated with or endorsed by Atlassian. Jira is a trademark of Atlassian.

## Status

| Version | Contents | State |
|---|---|---|
| 0.1 | My tickets, recently viewed, saved filters; search; status filter and sort; ticket view with rich text (tables, panels, code, lists), attachments with image viewer, comments; comment; change status incl. required fields and workflow-rule fields; watch; offline cache; preferences | done |
| 0.2 | Boards (kanban and active sprint) with drag and drop to change status and rank; resizable panes; hideable sidebar; images embedded in descriptions and comments | done |
| 0.3 | Lists per project and for all projects; filter bar (status, created date, sort) like the extension; child and linked tickets with drill-down; choose which ticket fields are shown; long values clipped | done |
| 0.4 | Swimlanes; ticket panel under boards; bookmarks; exact back/forward history; compact filter bar; details collapsed under the title | done |
| 0.5 | Create tickets; document editor for descriptions (tables with merged cells, lists, tasks, code, quotes, panels) | done |
| 0.6 | Images in descriptions and comments (paste, drop, insert; resizable); attachments for new tickets, comments and existing tickets; formatted comments | done |
| 0.7 | @mentions; clipboard images and file drops handled by the app (WebKitGTK passes neither to the page); toolbars wrap in narrow windows | done |
| 0.8 | Notifications (bell with history, desktop notifications, check frequency); live view | done |
| 0.9 | Firefox extension: Jira ticket links clicked in Firefox and Firefox web apps (e.g. Teams) open in Ticket Desk | done |
| 0.10 | Change the assignee; link tickets (relates, blocks, duplicates, parent/child) and remove links; per-user installer | **this release** |
| maybe later | Editing other fields in the ticket view, ticket history | — |
| later | Sprint planning and backlog, JQL editor with autocomplete, notification history, polling moved from the extension | planned |

## Using it

- **New ticket** (`+` next to back/forward, Ctrl+N): summary and description on the left;
  project, type (Task preselected), reporter (you), assignee (you, unless turned off in
  Preferences), priority (Medium) on the right, then fields Jira requires, then *More
  Fields* (collapsed) with the rest of the project's create screen. The pin next to a
  field makes it always shown and required for that project (e.g. a cost-centre field
  that a workflow rule needs). Preferences: default project, assign to me.
- **Editing descriptions**: the pencil next to *Description*. The editor is a normal
  document: headings, bold/italic/underline/strike/code, links (Ctrl+K), bullet, numbered
  and task lists, code blocks, quotes, panels, dividers and tables. In a table a second
  toolbar appears: insert rows/columns, merge right/down, split, header row, delete.
  Markdown-style shortcuts work at the start of a line (`#`, `*`, `1.`, `>`, `[]`,
  ```` ``` ````). Content the editor cannot change (images, mentions, macros…) is kept
  as is. Before saving, the app checks whether someone else changed the description.
- **Images and files**: paste a screenshot, drop an image or use the image button to put
  it into a description or comment. Click an image to choose Small/Medium/Large/Full
  Width/Original, or drag its corner (5 % steps); Delete removes it. Other files dropped
  into the text, or added with *Attach Files…*, are attached to the ticket (for a
  comment, they are also listed in it). New images are uploaded as attachments when you
  create, save or post. Jira has no official way to place an uploaded file in rich text;
  the app uses the file id in the attachment's download address, as Jira's own editor
  data shows. If that id cannot be found, the image stays attached and the text names it.
- **Mentions**: type `@` and a few letters of a name in a description or comment; pick
  someone with the arrow keys and Enter (or the mouse). Jira notifies them. The list
  shows people who can browse the ticket's project.
- **Notifications**: the bell next to the search field gets a small dot when something
  new happened: you were mentioned, or a ticket you are assigned to, reported, created or
  watch got a comment, a status change, or was assigned to you. Click one to open the
  ticket; opening a ticket marks its notifications read. Preferences: which kinds, how
  often to check (default every 5 minutes), and desktop notifications (off by default;
  the top bar extension can also show them, so keep one of the two on). The history is
  kept when the offline cache is cleared. Checks happen while the app is running.
- **Live view** (in the sort menu): the list refreshes by itself every 60 seconds
  (configurable) while it is sorted by Updated, newest first, keeping your scroll
  position. It pauses for other sort orders and while the window is minimised.
- **Bookmarks**: hover a project or board in the sidebar and click the star to move it
  to *Bookmarks* at the top; click the star again to put it back.
- **Back and forward** (buttons next to the search field, Alt+Left/Right, or the mouse's
  side buttons) return exactly where you were: list or board, search text, scroll
  position and the open ticket.
- **Sidebar**: *Tickets* has Mine (tickets related to you), All projects (every ticket in
  the projects from Preferences), one entry per project, and Recently Viewed. *Boards*
  lists the boards of those projects; *All* (click to expand) lists every other board.
- **Filters** (the sort button next to the search field, Ctrl+Shift+F): one row with the
  status categories, the created date (any time, this week, last week, or From–To from
  calendars) and the sort order, as in the top bar extension. A date range lists up to
  200 tickets.
- **Related tickets**: the parent chain is shown above the summary; child tickets (an
  epic's tickets, or sub-tasks) and linked tickets are listed in the ticket. Click one to
  open it; Back returns. *Show in List* lists all children in the list pane.
- **Details**: collapsed under the title (one line with the chosen fields); click to
  expand. The cog chooses the fields (default: assignee, reporter, type, created,
  updated). Long values are shortened; click them for the full text. Click the assignee
  to change it: you, nobody, or search people who can be assigned the ticket.
- **Linking**: the link button next to *Linked Tickets* links the ticket to another one,
  found by key or words (recently viewed tickets are offered first): *relates to*,
  *blocks*, *is blocked by*, *duplicates*, *is duplicated by*, *is child of*, *is parent
  of*. The `−` button on a linked ticket removes the link (Undo in the notice brings it
  back). Parent and child set the ticket's parent; Jira decides which types may be
  parents (an epic above a task, a task above a sub-task).
- **Panes**: drag the dividers to resize; the widths are remembered (separately for lists
  and boards). Hide the sidebar with the `<` button next to the main menu or F9; show it
  again with the `>` button or F9. Narrow windows switch to an overlay sidebar and then to
  one pane at a time.
- **Boards**: the sidebar lists the boards of your configured projects. Kanban boards show
  open work plus what was finished in the last 14 days; Scrum boards show the active
  sprint. Drag a card to another column to change its status (if the workflow allows it in
  one step; required fields are asked for), or between cards to rank it. The card is shown
  dimmed until Jira has accepted the change, then the board is re-read from Jira. The
  search field filters the cards on the board.
- **Images in text**: images pasted into descriptions and comments are shown in place,
  downloaded once and kept in the cache. Click one for the full-size viewer.

## Design

- **Instant, then accurate.** Lists and tickets are shown from a local SQLite cache
  immediately and refreshed from Jira in the background. The header shows whether you
  are looking at cached or current data. A cached ticket whose list entry changed in Jira
  is marked stale until its details are reloaded.
- **Writes are confirmed by Jira.** Comments, status changes and edits are sent first and
  the ticket is re-read afterwards; nothing is shown as done before Jira accepted it. A
  POST is never retried automatically, so a flaky connection cannot create duplicate comments.
- **Never blocks the UI.** Network and keyring calls run in a thread pool with one
  keep-alive HTTPS connection per thread; the list uses GTK's recycling list view.
- **Embedded images are matched exactly.** Jira's rich text refers to images by an ID
  that the REST API does not expose on attachments; the app matches them by file name
  when that is unique, otherwise by the attachment IDs in Jira's rendered HTML (fetched
  only for those tickets).
- **Nothing silently disappears.** Rich text the app cannot draw (Jira add-on macros,
  unknown content) is shown as a labelled placeholder.
- **Shares sign-in with the extension.** The API token lives in GNOME Keyring under the
  same entry the extension uses; on first start the site, email and projects are imported
  from the extension's settings if it is installed. Tickets the extension saw you mentioned
  in are included in *My Tickets*.

## Requirements

Jira Cloud and an API token (create one at id.atlassian.com → Security → API tokens; use
the plain kind, not "with scopes").

Runtime: Python ≥ 3.11, PyGObject, GTK ≥ 4.14, libadwaita ≥ 1.6, libsecret, WebKitGTK 6
(`webkitgtk-6.0` on Arch, `webkitgtk6.0` on Fedora, `gir1.2-webkit-6.0` on Debian) for
the document editor; without it descriptions are edited as plain text.
That is Arch (current), Fedora ≥ 41, Debian ≥ 13, Ubuntu ≥ 25.04.

## Install for your user

```
./install.sh            # checks the libraries first; --deps installs them with sudo
./install.sh --uninstall
```

No root needed. It puts the `ticketdesk` command in `~/.local/bin`, the program in
`~/.local/share/ticketdesk`, a menu entry (also the handler for `ticketdesk://` links),
icons and metainfo under `~/.local/share`, and registers the Firefox extension's helper
in `~/.mozilla/native-messaging-hosts`. A signed Firefox extension in
`browser/firefox/web-ext-artifacts/` is copied to `~/.local/share/ticketdesk/` with
instructions for adding it to Firefox. Run it again to update. Uninstalling keeps your
settings, cache and stored token. `PYTHON=/usr/bin/python3.x ./install.sh` uses another
system Python.

## Build and run

```
meson setup build --prefix=/usr
meson compile -C build
meson test -C build
sudo meson install -C build
```

Editor and extension tests (need Node and Playwright with Chromium):
`node tests/editor/test_editor.js`, `node tests/browser/test_extension.js`

Run from the source tree without installing: `python3 -m ticketdesk`
(debug logging: `TICKETDESK_LOG=debug python3 -m ticketdesk`).

Packages: `packaging/arch/PKGBUILD`, `debian/` (`dpkg-buildpackage -us -uc -b`),
`packaging/rpm/ticketdesk.spec`. Replace the `USER` placeholders in the URLs and the
maintainer lines before publishing.

## Opening tickets from elsewhere

`ticketdesk ticketdesk://browse/ABC-123` (or the link `ticketdesk://browse/ABC-123`)
opens a ticket; links to `https://<your-site>/browse/ABC-123` inside tickets also open
in the app. The top bar extension opens tickets in the app when it is installed.

### Ticket links in Firefox and Firefox web apps

The *Ticket Desk Links* extension (`browser/firefox/`) opens Jira ticket links in the app
when you click them in Firefox, including web apps made with PWAsForFirefox such as Teams.
Nothing else changes: other links, and links to tickets on other Jira sites, behave as
before.

- **Plain click** on a ticket link: opens in Ticket Desk. **Ctrl, Shift, Alt or middle
  click**: the browser as usual. While you are on the Jira site itself, links stay in the
  browser (an option turns that off).
- Ticket links are `/browse/KEY`, `/jira/…/issues/KEY`, service desk queue links ending in
  a key, and board, backlog or search links with `selectedIssue=KEY`; Microsoft Safe Links
  wrappers (Outlook, Teams) are looked through. `?ticketdesk=web` on a link keeps it in
  the browser.
- With PWA Links in the same web app, Ticket Desk Links sees the click first; PWA Links
  keeps handling every other external link.

It talks to a small helper (native messaging host `ticketdesk`) that opens the ticket and
tells the extension your Jira site from Ticket Desk's settings, so the extension needs no
setup. The packages install the helper (`/usr/lib/mozilla/native-messaging-hosts/ticketdesk.json`),
`install.sh` registers it for your user. When running from a source checkout instead:

```
python3 -m ticketdesk.nativehost --install     # --uninstall removes it
```

Without the helper, the extension falls back to `ticketdesk://` links (Firefox asks once
per site whether to open them) and needs the site entered in its options.

**Installing the extension.** Release Firefox only installs signed extensions. Sign it
for yourself (free, automatic, not published) with an addons.mozilla.org account and API
credentials from https://addons.mozilla.org/developers/addon/api/key/:

```
cd browser/firefox
npx web-ext sign --channel=unlisted --api-key=… --api-secret=…
```

The signed `.xpi` lands in `web-ext-artifacts/`. Install it in each Firefox profile that
should use it; for a web app, in that web app's window: Add-ons Manager (Ctrl+Shift+A)
→ gear menu → *Install Add-on From File…*. If the extension id `@ticketdesk-links` is
already taken on addons.mozilla.org, change it in `browser/firefox/manifest.json`,
`data/ticketdesk-native-host.json.in` and `ticketdesk/nativehost.py` (a test checks
they agree).

To try it unsigned: `about:debugging` → *This Firefox* → *Load Temporary Add-on…* →
`browser/firefox/manifest.json` (gone after a restart).

Tests: `node tests/browser/test_extension.js` (Node and Playwright with Chromium).

## Files

- Settings: `~/.config/ticketdesk/config.json`
- Cache (safe to delete): `~/.cache/ticketdesk/`
- Token: GNOME Keyring, item "Jira API token (…)"

## License

GPL-2.0-or-later. This project was written largely with the help of an AI assistant
(Claude, by Anthropic), directed and tested by the maintainer.
