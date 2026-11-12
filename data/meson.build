conf = configuration_data()
conf.set('APP_ID', app_id)
conf.set('VERSION', meson.project_version())

desktop = configure_file(
  input: 'ticketdesk.desktop.in',
  output: app_id + '.desktop',
  configuration: conf,
  install: true,
  install_dir: get_option('datadir') / 'applications',
)

metainfo = configure_file(
  input: 'ticketdesk.metainfo.xml.in',
  output: app_id + '.metainfo.xml',
  configuration: conf,
  install: true,
  install_dir: get_option('datadir') / 'metainfo',
)

install_data('icons/hicolor/scalable/apps/ticketdesk.svg',
  install_dir: get_option('datadir') / 'icons/hicolor/scalable/apps',
  rename: app_id + '.svg',
)
install_data('icons/hicolor/symbolic/apps/ticketdesk-symbolic.svg',
  install_dir: get_option('datadir') / 'icons/hicolor/symbolic/apps',
  rename: app_id + '-symbolic.svg',
)

desktop_validate = find_program('desktop-file-validate', required: false)
if desktop_validate.found()
  test('validate-desktop', desktop_validate, args: [desktop])
endif

appstreamcli = find_program('appstreamcli', required: false)
if appstreamcli.found()
  test('validate-metainfo', appstreamcli, args: ['validate', '--no-net', '--explain', metainfo])
endif
