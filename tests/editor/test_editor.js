// SPDX-License-Identifier: GPL-2.0-or-later
// Browser tests for ui/editor/editor.html (run: node tests/editor/test_editor.js).
// Needs Node and Playwright with Chromium; the app itself runs the editor in WebKitGTK.
'use strict';
const path = require('path');
const assert = require('assert');
const { chromium } = require('playwright');

const FILE = 'file://' + path.resolve(__dirname, '../../ticketdesk/ui/editor/editor.html');

// grid of cell texts, '^' for a position covered by a cell from above, '<' from the left
const GRID = `(() => {
  const t = document.querySelector('#editor table');
  if (!t) return null;
  const m = tableModel(t);
  const out = [];
  for (let r = 0; r < m.rows; r++) {
    const row = [];
    for (let c = 0; c < m.cols; c++) {
      const x = cellAt(m, r, c);
      if (!x) row.push('?');
      else if (x.r !== r) row.push('^');
      else if (x.c !== c) row.push('<');
      else row.push((x.el.tagName === 'TH' ? '#' : '') + x.el.textContent.trim());
    }
    out.push(row.join(' '));
  }
  return out;
})()`;

async function main() {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const messages = [];
  await page.exposeFunction('tdTestSink', (m) => messages.push(JSON.parse(m)));
  await page.goto(FILE);
  await page.waitForFunction(() => window.td);
  let failures = 0;
  async function test(name, fn) {
    try {
      await page.evaluate(() => window.td.setContent('<p><br></p>'));
      await fn();
      console.log('ok  ', name);
    } catch (e) {
      failures++;
      console.log('FAIL', name, '\n    ', e.message.split('\n').join('\n     '));
    }
  }
  const html = () => page.evaluate(() => window.td.getHtml());
  const grid = () => page.evaluate(GRID);
  async function caretIn(selector, index = 0) {
    await page.evaluate(([s, i]) => { caretTo(document.querySelectorAll(s)[i], false); reportState(); }, [selector, index]);
  }
  const op = (name) => page.evaluate((n) => window.td.cmd('tableOp', n), name);
  const TABLE = '<table><tbody>' +
    '<tr><th>A</th><th colspan="2">B</th></tr>' +
    '<tr><td rowspan="2">C</td><td>D</td><td>E</td></tr>' +
    '<tr><td>F</td><td>G</td></tr></tbody></table>';

  await test('typing and markdown shortcuts', async () => {
    await page.click('#editor');
    await page.keyboard.type('# Title');
    await page.keyboard.press('Enter');
    await page.keyboard.type('* one');
    await page.keyboard.press('Enter');
    await page.keyboard.type('two');
    await page.keyboard.press('Enter');
    await page.keyboard.press('Enter');
    await page.keyboard.type('[] task');
    const h = await html();
    assert.match(h, /<h1>Title<\/h1>/);
    assert.match(h, /<ul><li>one<\/li><li>two<\/li><\/ul>/);
    assert.match(h, /<\/ul><ul data-type="tasks"><li[^>]*>task<\/li><\/ul>/);
  });

  await test('list conversions keep items and do not merge lists', async () => {
    await page.click('#editor');
    await page.keyboard.type('* a');
    await page.keyboard.press('Enter');
    await page.keyboard.type('b');
    await page.evaluate(() => window.td.cmd('block', 'ol'));
    assert.match(await html(), /^<ol><li>a<\/li><li>b<\/li><\/ol>$/);
    await page.evaluate(() => window.td.cmd('block', 'tasks'));
    assert.match(await html(), /^<ul data-type="tasks"><li>a<\/li><li>b<\/li><\/ul>$/);
    await page.evaluate(() => window.td.cmd('block', 'ul'));
    assert.match(await html(), /^<ul><li>a<\/li><li>b<\/li><\/ul>$/);
    await page.keyboard.press('Enter');
    await page.keyboard.press('Enter');
    await page.keyboard.type('para');
    await page.evaluate(() => window.td.cmd('block', 'ol'));
    assert.match(await html(), /^<ul><li>a<\/li><li>b<\/li><\/ul><ol><li>para<\/li><\/ol>$/);
    await page.evaluate(() => window.td.cmd('block', 'ol'));   // toggle off again
    assert.match(await html(), /<\/ul><p>para<\/p>$/);
  });

  await test('undo after a markdown shortcut', async () => {
    await page.click('#editor');
    await page.keyboard.type('## x');
    assert.match(await html(), /<h2>x<\/h2>/);
  });

  await test('bold via shortcut and toolbar state', async () => {
    await page.click('#editor');
    await page.keyboard.type('a ');
    await page.keyboard.press('Control+b');
    await page.keyboard.type('bold');
    assert.match(await html(), /a <b>bold<\/b>|a <strong>bold<\/strong>/);
    await page.waitForTimeout(50);
    const st = messages.filter(m => m.type === 'state').pop();
    assert.strictEqual(st.bold, true);
  });

  await test('table: insert row below a merged cell extends the span', async () => {
    await page.evaluate((t) => window.td.setContent(t), TABLE);
    await caretIn('td', 0);  // C (rowspan 2)
    await op('rowBelow');
    assert.deepStrictEqual(await grid(), ['#A #B <', 'C D E', '^ F G', '  ']);
    await caretIn('td', 1);  // D
    await op('rowBelow');    // inside C's span: C grows
    assert.deepStrictEqual(await grid(), ['#A #B <', 'C D E', '^  ', '^ F G', '  ']);
  });

  await test('table: insert column inside a colspan extends it; header row stays header', async () => {
    await page.evaluate((t) => window.td.setContent(t), TABLE);
    await caretIn('td', 1);  // D at col 1
    await op('colRight');    // new col 2, inside B's span
    assert.deepStrictEqual(await grid(), ['#A #B < <', 'C D  E', '^ F  G']);
    await caretIn('th', 0);
    await op('colLeft');
    assert.deepStrictEqual(await grid(), ['# #A #B < <', ' C D  E', ' ^ F  G']);
  });

  await test('table: delete row and column with spans', async () => {
    await page.evaluate((t) => window.td.setContent(t), TABLE);
    await caretIn('td', 0);  // C starts in row 1 and spans 2
    await op('deleteRow');
    assert.deepStrictEqual(await grid(), ['#A #B <', 'C F G']);
    await page.evaluate((t) => window.td.setContent(t), TABLE);
    await caretIn('td', 2);  // E, col 2 (inside B's span)
    await op('deleteCol');
    assert.deepStrictEqual(await grid(), ['#A #B', 'C D', '^ F']);
  });

  await test('table: merge and split', async () => {
    await page.evaluate((t) => window.td.setContent(t), TABLE);
    await caretIn('td', 1);  // D
    await op('mergeDown');   // D + F
    assert.deepStrictEqual(await grid(), ['#A #B <', 'C DF E', '^ ^ G']);
    await op('mergeRight');  // DF (2 high) + E (1 high): refused
    assert.strictEqual(messages.filter(m => m.type === 'notice').pop().text.includes('different height'), true);
    await op('split');
    assert.deepStrictEqual(await grid(), ['#A #B <', 'C DF E', '^  G']);
    await caretIn('th', 0);
    await op('mergeRight');  // A + B (B spans 2)
    assert.deepStrictEqual(await grid(), ['#AB < <', 'C DF E', '^  G']);
  });

  await test('table: merging away a whole row folds it into the spans', async () => {
    await page.evaluate(() => window.td.setContent('<table><tbody><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></tbody></table>'));
    await caretIn('td', 0);
    await op('mergeDown');
    await caretIn('td', 1);
    await op('mergeDown');
    // both columns now span both rows: the second row would have no cells, so it is removed
    assert.deepStrictEqual(await grid(), ['ac bd']);
    assert.ok(!(await html()).includes('rowspan'));
  });

  await test('table: undo restores the table after an operation', async () => {
    await page.evaluate((t) => window.td.setContent(t), TABLE);
    await caretIn('td', 1);
    await op('deleteCol');
    assert.deepStrictEqual(await grid(), ['#A #B', 'C E', '^ G']);
    await page.keyboard.press('Control+z');
    assert.deepStrictEqual(await grid(), ['#A #B <', 'C D E', '^ F G']);
  });

  await test('table: header row toggle, delete last column removes table', async () => {
    await page.evaluate((t) => window.td.setContent(t), TABLE);
    await caretIn('td', 1);
    await op('headerRow');
    assert.deepStrictEqual((await grid())[0], 'A B <');
    await page.evaluate(() => window.td.setContent('<p>x</p><table><tbody><tr><td>only</td></tr></tbody></table><p>y</p>'));
    await caretIn('td', 0);
    await op('deleteCol');
    assert.strictEqual(await grid(), null);
    assert.match(await html(), /<p>x<\/p><p>y<\/p>/);
  });

  await test('table: insert, Tab moves through cells and adds a row at the end', async () => {
    await page.click('#editor');
    await page.evaluate(() => window.td.cmd('table', {rows: 2, cols: 2, header: true}));
    await page.keyboard.type('h1');
    await page.keyboard.press('Tab');
    await page.keyboard.type('h2');
    await page.keyboard.press('Tab');
    await page.keyboard.type('a');
    await page.keyboard.press('Tab');
    await page.keyboard.type('b');
    await page.keyboard.press('Tab');
    await page.keyboard.type('c');
    assert.deepStrictEqual(await grid(), ['#h1 #h2', 'a b', 'c ']);
    const st = messages.filter(m => m.type === 'state').pop();
    assert.strictEqual(st.inTable, true);
  });

  await test('table: Backspace at the start of a cell does not merge cells', async () => {
    await page.evaluate(() => window.td.setContent('<table><tbody><tr><td><p>a</p></td><td><p>b</p></td></tr></tbody></table>'));
    await caretIn('td', 1);
    await page.keyboard.press('Backspace');
    await page.keyboard.press('Backspace');
    assert.deepStrictEqual(await grid(), ['a b']);
  });

  await test('table: deleting a selection across cells keeps the table regular', async () => {
    await page.evaluate((t) => window.td.setContent(t), TABLE);
    await page.evaluate(() => {
      const cells = document.querySelectorAll('#editor td');
      const r = document.createRange();
      r.setStart(cells[1].firstChild, 0);
      r.setEnd(cells[3].firstChild, 1);
      const s = getSelection(); s.removeAllRanges(); s.addRange(r);
    });
    await page.keyboard.press('Delete');
    await page.waitForTimeout(200);  // normalizeTables runs with the change report
    const g = await grid();
    assert.strictEqual(g.length, 3);
    assert.ok(g.every(row => !row.includes('?')), JSON.stringify(g));
  });

  await test('caret in a table reports state for the table toolbar', async () => {
    await page.evaluate((t) => window.td.setContent(t), TABLE);
    await caretIn('td', 0);
    await page.waitForTimeout(50);
    const st = messages.filter(m => m.type === 'state').pop();
    assert.strictEqual(st.inTable, true);
    assert.strictEqual(st.canSplit, true);
    await caretIn('th', 0);
    await page.waitForTimeout(50);
    const st2 = messages.filter(m => m.type === 'state').pop();
    assert.strictEqual(st2.canMergeRight, true);
    assert.strictEqual(st2.canSplit, false);
  });

  await test('paste is cleaned: styles become marks, images and scripts dropped', async () => {
    const out = await page.evaluate(() => sanitize(
      '<div><span style="font-weight:700">B</span> <span style="font-style:italic">I</span>' +
      '<img src="x"><script>alert(1)</script><a href="javascript:x">j</a><a href="https://e.org">e</a></div>' +
      '<table><tr><td colspan="2" style="color:red">c</td></tr></table>'));
    assert.strictEqual(out.dropped, 1);
    assert.ok(!out.html.includes('script') && !out.html.includes('img') && !out.html.includes('javascript'));
    assert.match(out.html, /<strong>B<\/strong>/);
    assert.match(out.html, /<em>I<\/em>/);
    assert.match(out.html, /<a href="https:\/\/e.org">e<\/a>/);
    assert.match(out.html, /<td colspan="2">c<\/td>/);
  });

  await test('task checkbox toggles on click', async () => {
    await page.evaluate(() => window.td.setContent('<ul data-type="tasks"><li data-done="false">do it</li></ul>'));
    const box = await page.locator('#editor li').boundingBox();
    await page.mouse.click(box.x + 6, box.y + box.height / 2);
    assert.match(await html(), /data-done="true"/);
  });

  await test('locked content survives and setContent reports changes', async () => {
    messages.length = 0;
    await page.evaluate(() => window.td.setContent('<p>a <span class="adf-inline" contenteditable="false" data-adf="eyJ0eXBlIjoibWVudGlvbiJ9">@X</span></p><div class="adf-block" contenteditable="false" data-adf="e30=">Image</div>'));
    const h = await html();
    assert.match(h, /data-adf="eyJ0eXBlIjoibWVudGlvbiJ9"/);
    assert.match(h, /class="adf-block"/);
    assert.ok(messages.some(m => m.type === 'changed'));
  });

  await test('block toggles: heading back to paragraph, quote, code block exit', async () => {
    await page.click('#editor');
    await page.keyboard.type('x');
    await page.evaluate(() => window.td.cmd('block', 'h2'));
    assert.match(await html(), /<h2>x<\/h2>/);
    await page.evaluate(() => window.td.cmd('block', 'h2'));
    assert.match(await html(), /<p>x<\/p>/);
    await page.evaluate(() => window.td.cmd('block', 'pre'));
    await page.keyboard.press('End');
    await page.keyboard.press('Enter');
    await page.keyboard.type('y');
    await page.keyboard.press('Enter');
    await page.keyboard.press('Enter');
    await page.keyboard.type('after');
    const h = await html();
    // engines end code lines with <br> or a newline; both convert to the same ADF
    assert.match(h, /<pre>x(\n|<br>)y<\/pre><p>after<\/p>/, h);
  });

  // 1x1 red PNG
  const PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg==';

  await test('images: insert, export with data, resize by dragging, undo', async () => {
    await page.click('#editor');
    await page.keyboard.type('before');
    await page.evaluate((png) => window.td.insertImage(png, 'shot.png', 1600, 900), PNG);
    let h = await html();
    assert.match(h, /<figure class="td-image"[^>]*><img src="data:image\/png;base64,/);
    for (const a of ['data-pending="', 'data-alt="shot.png"', 'data-w="1600"', 'data-h="900"', 'data-width="100"', 'contenteditable="false"'])
      assert.ok(h.includes(a), a);
    assert.ok(!h.includes('td-handle') && !h.includes('style='), h);
    // the 1x1 test image is square: at full width its corner is far down
    await page.setViewportSize({width: 700, height: 1600});
    const fig = await page.locator('figure.td-image').boundingBox();
    await page.mouse.click(fig.x + 10, fig.y + 10);  // select
    await page.waitForFunction(() => document.querySelector('figure.td-image.selected'));
    await page.waitForTimeout(100);  // page messages reach the test asynchronously
    let st = messages.filter(m => m.type === 'state').pop();
    assert.deepStrictEqual(st.image, {width: 100});
    const handle = await page.locator('figure.td-image .td-handle').boundingBox();
    const ed = await page.locator('#editor').boundingBox();
    await page.mouse.move(handle.x + 7, handle.y + 7);
    await page.mouse.down();
    await page.mouse.move(fig.x + (ed.width - 28) * 0.42, handle.y + 7, {steps: 5});
    await page.mouse.up();
    h = await html();
    assert.match(h, /data-width="(40|45)"/, h.slice(0, 300));
    await page.keyboard.press('Control+z');
    assert.match(await html(), /data-width="100"/);
    await page.keyboard.press('Control+Shift+z');
    assert.match(await html(), /data-width="(40|45)"/);
    await page.setViewportSize({width: 1280, height: 720});
  });

  await test('images: preset width, delete selected image, undo brings it back', async () => {
    await page.evaluate((png) => { window.td.setContent('<p>a</p>'); window.td.insertImage(png, 'x.png', 10, 10); }, PNG);
    const fig = await page.locator('figure.td-image').boundingBox();
    await page.mouse.click(fig.x + 3, fig.y + 3);
    await page.evaluate(() => window.td.cmd('imageWidth', 50));
    assert.match(await html(), /data-width="50"/);
    await page.keyboard.press('Delete');
    assert.ok(!(await html()).includes('figure'));
    await page.keyboard.press('Control+z');
    const h = await html();
    assert.match(h, /<figure[^>]*data-width="50"[^>]*><img src="data:image\/png/, h);
  });

  await test('images: pasting a screenshot inserts it', async () => {
    await page.click('#editor');
    await page.evaluate((png) => {
      const bin = atob(png.split(',')[1]);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      const dt = new DataTransfer();
      dt.items.add(new File([bytes], 'image.png', {type: 'image/png'}));
      dt.setData('text/html', '<img src="file:///tmp/x.png">');
      document.getElementById('editor').dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
    }, PNG);
    await page.waitForTimeout(150);
    assert.match(await html(), /<figure class="td-image"[^>]*data-alt="image.png"[^>]*><img src="data:image\/png/);
  });

  await test('files: dropping a non-image file asks GTK to attach it', async () => {
    messages.length = 0;
    await page.evaluate(() => {
      const dt = new DataTransfer();
      dt.items.add(new File(['hello'], 'notes.txt', {type: 'text/plain'}));
      const r = document.getElementById('editor').getBoundingClientRect();
      document.getElementById('editor').dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles: true, cancelable: true,
        clientX: r.left + 20, clientY: r.top + 10}));
    });
    await page.waitForTimeout(150);
    const att = messages.find(m => m.type === 'attach');
    assert.deepStrictEqual(att.files, [{name: 'notes.txt', mime: 'text/plain', data: 'aGVsbG8='}]);
    assert.ok(!(await html()).includes('figure'));
  });

  await test('images: existing images ask for their data and export without it', async () => {
    messages.length = 0;
    await page.evaluate(() => window.td.setContent('<figure class="td-image" contenteditable="false" data-adf="e30=" ' +
      'data-media-id="m-1" data-alt="old.png" data-width="60" data-orig-width="60"><img alt="old.png"></figure><p>t</p>'));
    assert.deepStrictEqual(messages.find(m => m.type === 'need-images').items, [{id: 'm-1', alt: 'old.png'}]);
    await page.evaluate((png) => window.td.setImageSrc('m-1', png), PNG);
    assert.ok(await page.evaluate(() => document.querySelector('figure img').src.startsWith('blob:')));
    const h = await html();
    assert.ok(!h.includes('src='), h);
    assert.match(h, /data-width="60" data-orig-width="60"/);
    // an undo snapshot taken before the data arrived must not blank the image again
    await page.click('#editor p');
    await page.keyboard.type('x');
    await page.waitForTimeout(800);
    await page.keyboard.press('Control+z');
    assert.ok(await page.evaluate(() => (document.querySelector('figure img').getAttribute('src') || '').startsWith('blob:')));
  });

  await test('mentions: @ asks for people, Enter inserts a mention, Escape closes', async () => {
    messages.length = 0;
    await page.click('#editor');
    await page.keyboard.type('Ping @ås');
    await page.waitForTimeout(300);
    const q = messages.filter(m => m.type === 'mention-query').pop();
    assert.strictEqual(q.query, 'ås');
    await page.evaluate((seq) => window.td.mentionResults(seq, [{id: 'acc-1', name: 'Åsa Öberg'}, {id: 'acc-2', name: 'Åke'}]), q.seq);
    assert.strictEqual(await page.evaluate(() => document.getElementById('mentions').hidden), false);
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('ArrowUp');
    await page.keyboard.press('Enter');
    await page.keyboard.type('please look');
    const h = await html();
    assert.match(h, /^<p>Ping <span class="adf-inline mention" contenteditable="false" data-adf="[^"]+">@Åsa Öberg<\/span>(&nbsp;| )please look<\/p>$/, h);
    assert.strictEqual(await page.evaluate(() => document.getElementById('mentions').hidden), true);
    // the stored node is a Jira mention (decoded as UTF-8)
    const node = await page.evaluate(() => JSON.parse(decodeURIComponent(escape(atob(document.querySelector('.mention').getAttribute('data-adf'))))));
    assert.deepStrictEqual(node, {type: 'mention', attrs: {id: 'acc-1', text: '@Åsa Öberg', accessLevel: ''}});
    await page.keyboard.type(' @x');
    await page.waitForTimeout(250);
    await page.keyboard.press('Escape');
    assert.strictEqual(await page.evaluate(() => document.getElementById('mentions').hidden), true);
    await page.keyboard.press('Enter');   // Escape closed the list: Enter is a new paragraph again
    assert.match(await html(), /<\/p><p><br><\/p>$/);
  });

  await test('mentions: e-mail addresses and stale answers do not open the list', async () => {
    messages.length = 0;
    await page.click('#editor');
    await page.keyboard.type('mail a@b.se');
    await page.waitForTimeout(250);
    assert.ok(!messages.some(m => m.type === 'mention-query'));
    await page.keyboard.type(' @a');
    await page.waitForTimeout(250);
    await page.keyboard.type('b');
    await page.waitForTimeout(250);
    const qs = messages.filter(m => m.type === 'mention-query');
    await page.evaluate((seq) => window.td.mentionResults(seq, [{id: 'old', name: 'Old Answer'}]), qs[0].seq);
    assert.ok(!(await page.evaluate(() => document.getElementById('mentions').textContent)).includes('Old Answer'));
  });

  await test('drop position: caretAtPoint moves the caret', async () => {
    await page.evaluate(() => window.td.setContent('<p>first</p><p>second</p>'));
    const box = await page.locator('#editor p').nth(1).boundingBox();
    await page.evaluate(([x, y]) => window.td.caretAtPoint(x, y), [box.x + 2, box.y + box.height / 2]);
    await page.keyboard.type('X');
    assert.match(await html(), /<p>first<\/p><p>Xsecond<\/p>/);
  });

  await browser.close();
  console.log(failures ? `${failures} FAILED` : 'all passed');
  process.exit(failures ? 1 : 0);
}

main();
