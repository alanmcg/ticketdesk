project('ticketdesk',
  version: '0.10.0',
  license: 'GPL-2.0-or-later',
  meson_version: '>= 0.62.0',
)

app_id = 'io.github.ticketdesk.TicketDesk'
python = import('python').find_installation('python3')
gnome = import('gnome')
pkgdatadir = get_option('prefix') / get_option('datadir') / meson.project_name()

subdir('data')

install_subdir('ticketdesk',
  install_dir: pkgdatadir,
  exclude_directories: ['__pycache__', 'core/__pycache__', 'ui/__pycache__'],
)

launcher = configuration_data()
launcher.set('PYTHON', python.full_path())
launcher.set('pkgdatadir', pkgdatadir)
configure_file(
  input: 'bin/ticketdesk.in',
  output: 'ticketdesk',
  configuration: launcher,
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x',
)

# Browser helper for browser/firefox (native messaging host). Firefox looks for host
# manifests in /usr/lib/mozilla/native-messaging-hosts on every distribution. The helper
# goes to <prefix>/lib/ticketdesk: architecture-independent, unlike libexecdir on Debian.
host_dir = get_option('prefix') / 'lib' / meson.project_name()
configure_file(
  input: 'bin/ticketdesk-native-host.in',
  output: 'ticketdesk-native-host',
  configuration: launcher,
  install: true,
  install_dir: host_dir,
  install_mode: 'rwxr-xr-x',
)
host = configuration_data()
host.set('HOST_PATH', host_dir / 'ticketdesk-native-host')
configure_file(
  input: 'data/ticketdesk-native-host.json.in',
  output: 'ticketdesk.json',
  configuration: host,
  install: true,
  install_dir: get_option('prefix') / 'lib' / 'mozilla' / 'native-messaging-hosts',
)

test('core', python,
  args: ['-m', 'unittest', 'discover', '-s', 'tests', '-t', '.'],
  workdir: meson.project_source_root(),
  timeout: 120,
)

gnome.post_install(
  gtk_update_icon_cache: true,
  update_desktop_database: true,
)
