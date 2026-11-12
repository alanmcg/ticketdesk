// SPDX-License-Identifier: GPL-2.0-or-later
'use strict';

const $ = (id) => document.getElementById(id);

async function show() {
  const s = await browser.storage.local.get(['helper', 'helperSite', 'helperVersion', 'site', 'onSite']);
  if (s.helper === 'found') {
    $('status').textContent = `Ticket Desk ${s.helperVersion || ''} found: ticket links open in the app.`;
    $('detail').textContent = s.helperSite ? `Site from Ticket Desk: ${s.helperSite}` : 'Ticket Desk has no Jira site set up yet.';
  } else {
    $('status').textContent = 'Ticket Desk\'s browser helper was not found.';
    $('detail').textContent = 'Ticket links then open through ticketdesk:// links, and Firefox asks once per site. ' +
      'The helper comes with the Ticket Desk package; from a source checkout run ' +
      '"python3 -m ticketdesk.nativehost --install". ' + (s.helper ? `(${s.helper})` : '');
  }
  $('site').placeholder = s.helperSite || 'https://your-site.atlassian.net';
  if (document.activeElement !== $('site')) $('site').value = s.site || '';
  $('onSite').checked = !!s.onSite;
}

$('check').addEventListener('click', async () => {
  $('status').textContent = 'Checking for Ticket Desk…';
  await browser.runtime.sendMessage({ refresh: true });
  show();
});
$('site').addEventListener('change', () => {
  const value = $('site').value.trim();
  browser.storage.local.set({ site: value && TicketLinks.hostOf(value) ? value : '' });
});
$('onSite').addEventListener('change', () => browser.storage.local.set({ onSite: $('onSite').checked }));
browser.storage.onChanged.addListener(show);
show();
