// SPDX-License-Identifier: GPL-2.0-or-later
// Tests for the Firefox extension in browser/firefox (run: node tests/browser/test_extension.js).
// Link rules run in Node; the click handling runs in Playwright's Chromium with the
// extension APIs stubbed, which is enough for the DOM side of content.js.
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const EXT = path.resolve(__dirname, '../../browser/firefox');
const TicketLinks = require(path.join(EXT, 'links.js'));
const CASES = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../link_cases.json'), 'utf8'));

let failures = 0;
async function test(name, fn) {
  try {
    await fn();
    console.log(`ok   ${name}`);
  } catch (e) {
    failures++;
    console.log(`FAIL ${name}\n     ${e.stack.split('\n').slice(0, 3).join('\n     ')}`);
  }
}

const STUB = `
  window.__sent = [];
  window.__reply = { ok: true };
  window.browser = {
    storage: {
      local: { get: async () => ({ helperSite: 'https://acme.example.net', onSite: false }) },
      onChanged: { addListener() {} },
    },
    runtime: { sendMessage: async (m) => { window.__sent.push(m); return window.__reply; } },
  };`;

const PAGE = `<!DOCTYPE html><body>
  <a id="ticket" href="https://acme.example.net/browse/OPS-12" target="_blank"><span id="inner">OPS-12</span></a>
  <a id="safe" href="https://eur02.safelinks.protection.outlook.com/?url=https%3A%2F%2Facme.example.net%2Fbrowse%2FOPS-7&amp;reserved=0">OPS-7</a>
  <a id="other" href="https://example.org/x">other</a>
  <a id="board" href="https://acme.example.net/jira/software/projects/OPS/boards/3">board</a>
  <div id="host"></div>
  <script>
    window.__page = [];
    // like PWA Links: capture on document
    document.addEventListener('click', (e) => { window.__page.push('document'); e.preventDefault(); }, true);
    // like a chat app: its own handler that opens links itself
    document.body.addEventListener('click', (e) => { window.__page.push('app'); e.preventDefault(); });
    const root = document.getElementById('host').attachShadow({ mode: 'open' });
    root.innerHTML = '<a id="shadow" href="https://acme.example.net/browse/OPS-99">OPS-99</a>';
  </script>
</body>`;

async function main() {
  await test('ticket links (shared cases)', () => {
    const host = TicketLinks.hostOf(CASES.site);
    for (const [url, key] of CASES.tickets) assert.strictEqual(TicketLinks.ticketKey(url, host), key, url);
  });
  await test('not ticket links (shared cases)', () => {
    const host = TicketLinks.hostOf(CASES.site);
    for (const url of CASES.not_tickets) assert.strictEqual(TicketLinks.ticketKey(url, host), null, url);
  });
  await test('hostOf', () => {
    for (const site of ['acme.example.net', 'https://acme.example.net/', ' https://ACME.example.net ']) {
      assert.strictEqual(TicketLinks.hostOf(site), 'acme.example.net', site);
    }
    assert.strictEqual(TicketLinks.hostOf(''), '');
  });

  const { chromium } = require('playwright');
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.route('https://**/*', (route) => route.fulfill({ contentType: 'text/html', body: PAGE }));
  // One script, as Firefox runs the manifest's content scripts in one shared scope
  await page.addInitScript([STUB, ...['links.js', 'content.js'].map((f) => fs.readFileSync(path.join(EXT, f), 'utf8'))]
    .join('\n;\n'));

  async function click(url, selector, modifiers) {
    await page.goto(url);
    await page.evaluate(() => new Promise((r) => setTimeout(r, 20))); // storage.get settles
    if (selector.startsWith('shadow:')) {
      await page.locator(`#host >> #${selector.slice(7)}`).click({ modifiers });
    } else {
      await page.click(selector, { modifiers });
    }
    await page.evaluate(() => new Promise((r) => setTimeout(r, 20)));
    return page.evaluate(() => ({ sent: window.__sent, page: window.__page, url: location.href }));
  }

  await test('plain click on a ticket link goes to Ticket Desk, before the page and PWA Links', async () => {
    const r = await click('https://chat.example.org/', '#inner');
    assert.deepStrictEqual(r.sent, [{ open: 'OPS-12' }]);
    assert.deepStrictEqual(r.page, []);
    assert.strictEqual(r.url, 'https://chat.example.org/');
  });
  await test('Safe Links are unwrapped', async () => {
    const r = await click('https://chat.example.org/', '#safe');
    assert.deepStrictEqual(r.sent, [{ open: 'OPS-7' }]);
  });
  await test('links inside shadow DOM', async () => {
    const r = await click('https://chat.example.org/', 'shadow:shadow');
    assert.deepStrictEqual(r.sent, [{ open: 'OPS-99' }]);
  });
  await test('other links are left alone', async () => {
    for (const sel of ['#other', '#board']) {
      const r = await click('https://chat.example.org/', sel);
      assert.deepStrictEqual(r.sent, [], sel);
      assert.deepStrictEqual(r.page, ['document', 'app'], sel);
    }
  });
  await test('Ctrl, Shift and Alt clicks stay in the browser', async () => {
    for (const mod of ['Control', 'Shift', 'Alt']) {
      const r = await click('https://chat.example.org/', '#ticket', [mod]);
      assert.deepStrictEqual(r.sent, [], mod);
    }
  });
  await test('not while browsing the Jira site itself', async () => {
    const r = await click('https://acme.example.net/browse/OPS-1', '#ticket');
    assert.deepStrictEqual(r.sent, []);
    assert.deepStrictEqual(r.page, ['document', 'app']);
  });
  await test('without the helper: falls back to the ticketdesk:// link', async () => {
    const requested = [];
    const onRequest = (req) => requested.push(req.url());
    page.on('request', onRequest);
    await page.goto('https://chat.example.org/');
    await page.evaluate(() => { window.__reply = { ok: false }; });
    await page.click('#ticket');
    await page.waitForTimeout(200);
    page.off('request', onRequest);
    assert.deepStrictEqual(requested.filter((u) => !u.startsWith('https:')), ['ticketdesk://browse/OPS-12']);
  });

  await browser.close();
  console.log(failures ? `${failures} failed` : 'all passed');
  process.exit(failures ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
