// SPDX-License-Identifier: GPL-2.0-or-later
// Talks to Ticket Desk's helper (native messaging host "ticketdesk"): opens tickets and
// learns the Jira site from Ticket Desk's settings.
'use strict';

const HOST = 'ticketdesk';
const REFRESH_MS = 30 * 60 * 1000;

async function refresh() {
  try {
    const reply = await browser.runtime.sendNativeMessage(HOST, { get: 'site' });
    await browser.storage.local.set({ helper: 'found', helperSite: (reply && reply.site) || '', helperVersion: (reply && reply.version) || '' });
  } catch (e) {
    await browser.storage.local.set({ helper: String(e && e.message || e) });
  }
}

browser.runtime.onMessage.addListener((message) => {
  if (message && typeof message.open === 'string' && TicketLinks.KEY_RE.test(message.open)) {
    return browser.runtime.sendNativeMessage(HOST, { open: message.open }).then(
      (reply) => ({ ok: !!(reply && reply.ok) }),
      (e) => { browser.storage.local.set({ helper: String(e && e.message || e) }); return { ok: false }; });
  }
  if (message && message.refresh) return refresh().then(() => ({}));
  return undefined;
});

refresh();
setInterval(refresh, REFRESH_MS);
