// SPDX-License-Identifier: GPL-2.0-or-later
// A plain click on a Jira ticket link opens the ticket in Ticket Desk instead of the page.
// Ctrl, Shift, Alt or middle click keep the browser's behaviour.
//
// Runs at document_start and listens on window in the capture phase, so it sees the
// click before the page and before extensions that listen on document (PWA Links).
'use strict';

(() => {
  const KEYS = ['site', 'helperSite', 'onSite'];
  let siteHost = '';
  let onSite = false;

  function load(s) {
    siteHost = TicketLinks.hostOf(s.site || s.helperSite || '');
    onSite = !!s.onSite;
  }
  browser.storage.local.get(KEYS).then(load);
  browser.storage.onChanged.addListener((_changes, area) => {
    if (area === 'local') browser.storage.local.get(KEYS).then(load);
  });

  function linkOf(event) {
    for (const el of event.composedPath()) {
      if ((el.localName === 'a' || el.localName === 'area') && typeof el.href === 'string' && el.href) return el;
    }
    return null;
  }

  function fallback(key) {
    // No helper: the ticketdesk:// link; Firefox asks once whether this site may open it
    location.href = `ticketdesk://browse/${key}`;
  }

  window.addEventListener('click', (event) => {
    if (event.button !== 0 || event.ctrlKey || event.shiftKey || event.altKey || event.metaKey || !siteHost) return;
    const link = linkOf(event);
    const key = link && TicketLinks.ticketKey(link.href, siteHost);
    if (!key) return;
    if (!onSite && location.hostname.toLowerCase() === siteHost) return; // browsing Jira itself
    event.preventDefault();
    event.stopImmediatePropagation();
    browser.runtime.sendMessage({ open: key }).then(
      (reply) => { if (!reply || !reply.ok) fallback(key); },
      () => fallback(key));
  }, true);
})();
