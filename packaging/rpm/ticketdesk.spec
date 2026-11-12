# SPDX-License-Identifier: GPL-2.0-or-later
%global app_id io.github.ticketdesk.TicketDesk

Name:           ticketdesk
Version:        0.10.0
Release:        1%{?dist}
Summary:        Fast GNOME client for Jira Cloud
License:        GPL-2.0-or-later
URL:            https://github.com/USER/ticketdesk
Source0:        %{url}/archive/refs/tags/v%{version}/%{name}-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  meson >= 0.62
BuildRequires:  python3-devel
BuildRequires:  desktop-file-utils
BuildRequires:  libappstream-glib
BuildRequires:  appstream

Requires:       python3 >= 3.11
Requires:       python3-gobject
Requires:       gtk4 >= 4.14
Requires:       libadwaita >= 1.6
Requires:       libsecret
Requires:       webkitgtk6.0
Recommends:     gnome-keyring

%description
Ticket Desk shows your Jira Cloud tickets in a native GNOME window, with an
offline cache, rich descriptions and comments, image attachments, and status
changes. Not affiliated with or endorsed by Atlassian.

%prep
%autosetup

%build
%meson
%meson_build

%install
%meson_install

%check
%meson_test

%files
%license LICENSE
%doc README.md
%{_bindir}/%{name}
%{_prefix}/lib/%{name}/
%{_prefix}/lib/mozilla/native-messaging-hosts/%{name}.json
%{_datadir}/%{name}/
%{_datadir}/applications/%{app_id}.desktop
%{_datadir}/metainfo/%{app_id}.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/%{app_id}.svg
%{_datadir}/icons/hicolor/symbolic/apps/%{app_id}-symbolic.svg

%changelog
* Thu Nov 12 2026 Your Name <you@example.com> - 0.10.0-1
- Change the assignee, link tickets, per-user installer

* Thu Nov 12 2026 Your Name <you@example.com> - 0.9.0-1
- Firefox extension helper: ticket links from Firefox and its web apps open in Ticket Desk

* Thu Nov 12 2026 Your Name <you@example.com> - 0.8.0-1
- Notifications (bell, desktop), live view

* Thu Nov 12 2026 Your Name <you@example.com> - 0.7.0-1
- @mentions; image paste and file drop; new ticket dialog fits small windows

* Thu Nov 12 2026 Your Name <you@example.com> - 0.6.0-1
- Inline images (resizable) and attachments for new tickets, comments and descriptions

* Thu Nov 12 2026 Your Name <you@example.com> - 0.5.0-1
- Create tickets; document editor for descriptions

* Wed Nov 11 2026 Your Name <you@example.com> - 0.4.0-1
- Swimlanes, ticket panel under boards, bookmarks, back/forward history, compact filters

* Wed Nov 11 2026 Your Name <you@example.com> - 0.3.0-1
- Project lists, filter bar, child and linked tickets, field chooser

* Wed Nov 11 2026 Your Name <you@example.com> - 0.2.0-1
- Boards with drag and drop, resizable panes, embedded images

* Wed Nov 11 2026 Your Name <you@example.com> - 0.1.0-1
- First release
