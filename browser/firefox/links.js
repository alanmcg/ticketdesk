// SPDX-License-Identifier: GPL-2.0-or-later
// Which links are Jira tickets. Same rules as ticketdesk/core/links.py; both are tested
// against tests/link_cases.json.
'use strict';

var TicketLinks = (() => {
  const KEY = '[A-Z][A-Z0-9_]*-\\d+';
  const KEY_RE = new RegExp(`^${KEY}$`);
  const PATHS = [
    new RegExp(`^/browse/(${KEY})(?:/|$)`),
    new RegExp(`^/jira/(?:[^/]+/)*issues/(${KEY})/?$`),
    new RegExp(`^/jira/servicedesk/projects/[^/]+/queues/(?:[^/]+/)*(${KEY})/?$`),
  ];
  const BYPASS = 'ticketdesk'; // ?ticketdesk=web: leave the link to the browser

  function parse(url) {
    try {
      return new URL(url);
    } catch (e) {
      return null;
    }
  }

  function hostOf(site) {
    site = (site || '').trim().replace(/\/+$/, '');
    if (!site) return '';
    const u = parse(site.includes('://') ? site : `https://${site}`);
    return u ? u.hostname.toLowerCase() : '';
  }

  // The real target of a Microsoft Safe Links URL (Outlook, Teams), else url
  function unwrap(url) {
    const u = parse(url);
    if (!u) return url;
    const host = u.hostname.toLowerCase();
    const wrapped = host.endsWith('.safelinks.protection.outlook.com') ||
      (host === 'statics.teams.cdn.office.net' && u.pathname.includes('/safelinks/'));
    if (wrapped) {
      const target = u.searchParams.get('url') || '';
      if (/^https?:\/\//.test(target)) return target;
    }
    return url;
  }

  // The ticket key if url shows one ticket on the Jira site siteHost, else null
  function ticketKey(url, siteHost) {
    const u = parse(unwrap(url || ''));
    if (!u || !siteHost || (u.protocol !== 'http:' && u.protocol !== 'https:')) return null;
    if (u.hostname.toLowerCase() !== siteHost) return null;
    if (u.searchParams.has(BYPASS)) return null;
    for (const rx of PATHS) {
      const m = rx.exec(u.pathname);
      if (m) return m[1];
    }
    const selected = (u.searchParams.get('selectedIssue') || '').toUpperCase();
    return KEY_RE.test(selected) ? selected : null;
  }

  return { KEY_RE, hostOf, unwrap, ticketKey };
})();

if (typeof module !== 'undefined') module.exports = TicketLinks;
