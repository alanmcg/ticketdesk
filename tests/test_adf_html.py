# SPDX-License-Identifier: GPL-2.0-or-later
import unittest

from ticketdesk.core.adf_html import adf_to_html, html_to_adf, is_empty


def t(text, *marks):
    n = {'type': 'text', 'text': text}
    if marks:
        n['marks'] = list(marks)
    return n


def p(*content):
    return {'type': 'paragraph', 'content': list(content)}


def doc(*content):
    return {'type': 'doc', 'version': 1, 'content': list(content)}


RICH = doc(
    {'type': 'heading', 'attrs': {'level': 2}, 'content': [t('Plan')]},
    p(t('Some '), t('bold', {'type': 'strong'}), t(' and '), t('both', {'type': 'strong'}, {'type': 'em'}),
      t(', '), t('code', {'type': 'code'}), t(' '), t('a link', {'type': 'link', 'attrs': {'href': 'https://x.org/?a=1&b=2'}}),
      {'type': 'hardBreak'}, t('next <line> & "quotes"'),
      {'type': 'mention', 'attrs': {'id': 'abc', 'text': '@Ylva'}}, t(' H'), t('2', {'type': 'subsup', 'attrs': {'type': 'sub'}}),
      t('O red', {'type': 'textColor', 'attrs': {'color': '#ff5630'}})),
    {'type': 'bulletList', 'content': [
        {'type': 'listItem', 'content': [p(t('one')), {'type': 'orderedList', 'attrs': {'order': 3}, 'content': [
            {'type': 'listItem', 'content': [p(t('nested'))]}]}]},
        {'type': 'listItem', 'content': [p(t('two')), p(t('second paragraph'))]}]},
    {'type': 'taskList', 'attrs': {'localId': 'tl1'}, 'content': [
        {'type': 'taskItem', 'attrs': {'localId': 'ti1', 'state': 'DONE'}, 'content': [t('done thing')]},
        {'type': 'taskItem', 'attrs': {'localId': 'ti2', 'state': 'TODO'}, 'content': [t('open thing')]}]},
    {'type': 'codeBlock', 'attrs': {'language': 'python'}, 'content': [t('if a < b:\n    print("x")')]},
    {'type': 'blockquote', 'content': [p(t('quoted'))]},
    {'type': 'panel', 'attrs': {'panelType': 'warning'}, 'content': [p(t('careful'))]},
    {'type': 'rule'},
    {'type': 'table', 'attrs': {'isNumberColumnEnabled': False, 'layout': 'default'}, 'content': [
        {'type': 'tableRow', 'content': [
            {'type': 'tableHeader', 'content': [p(t('H1'))]},
            {'type': 'tableHeader', 'attrs': {'colspan': 2, 'colwidth': [100, 120]}, 'content': [p(t('H2'))]}]},
        {'type': 'tableRow', 'content': [
            {'type': 'tableCell', 'attrs': {'rowspan': 2}, 'content': [p(t('tall'))]},
            {'type': 'tableCell', 'content': [p()]},
            {'type': 'tableCell', 'attrs': {'background': '#deebff'}, 'content': [p(t('c'))]}]},
        {'type': 'tableRow', 'content': [
            {'type': 'tableCell', 'content': [{'type': 'bulletList', 'content': [
                {'type': 'listItem', 'content': [p(t('in cell'))]}]}]},
            {'type': 'tableCell', 'content': [p(t('d'))]}]}]},
    {'type': 'mediaSingle', 'attrs': {'layout': 'center'}, 'content': [
        {'type': 'media', 'attrs': {'id': 'm1', 'type': 'file', 'collection': '', 'alt': 'shot.png'}}]},
    {'type': 'expand', 'attrs': {'title': 'More'}, 'content': [p(t('hidden'))]},
    p(),
)


class RoundTripTests(unittest.TestCase):
    def test_rich_document_round_trips(self):
        self.assertEqual(html_to_adf(adf_to_html(RICH)), RICH)

    def test_html_is_escaped(self):
        h = adf_to_html(doc(p(t('<script>alert(1)</script>'))))
        self.assertNotIn('<script>', h)
        self.assertIn('&lt;script&gt;', h)

    def test_empty(self):
        self.assertTrue(is_empty(html_to_adf('<p><br></p>')))
        self.assertTrue(is_empty(html_to_adf('')))
        self.assertFalse(is_empty(html_to_adf('<p>x</p>')))
        self.assertEqual(adf_to_html(None), '<p><br></p>')


class EditorHtmlTests(unittest.TestCase):
    """HTML as browsers' contentEditable and pasting produce it."""

    def test_divs_b_i_and_trailing_br(self):
        self.assertEqual(html_to_adf('<div>a <b>b</b><i>c</i><br></div><div><br></div>'),
                         doc(p(t('a '), t('b', {'type': 'strong'}), t('c', {'type': 'em'})), p()))

    def test_li_without_paragraph_and_stray_text(self):
        self.assertEqual(html_to_adf('<ul><li>x</li>loose</ul>'),
                         doc({'type': 'bulletList', 'content': [{'type': 'listItem', 'content': [p(t('x'))]},
                                                                {'type': 'listItem', 'content': [p(t('loose'))]}]}))

    def test_list_nested_directly_in_list(self):
        got = html_to_adf('<ul><li>a</li><ul><li>b</li></ul></ul>')
        item = got['content'][0]['content'][0]
        self.assertEqual(item['content'][1]['type'], 'bulletList')

    def test_pretty_printed_html(self):
        src = '<p>\n  Hello\n  <strong>world</strong>\n</p>\n<p>x&nbsp; y</p>'
        self.assertEqual(html_to_adf(src), doc(p(t('Hello '), t('world', {'type': 'strong'})), p(t('x  y'))))

    def test_code_mark_drops_incompatible_marks(self):
        self.assertEqual(html_to_adf('<p><b><code>x</code></b></p>'), doc(p(t('x', {'type': 'code'}))))

    def test_inline_text_between_blocks_becomes_paragraphs(self):
        self.assertEqual(html_to_adf('top<h1>T</h1>bottom'),
                         doc(p(t('top')), {'type': 'heading', 'attrs': {'level': 1}, 'content': [t('T')]}, p(t('bottom'))))

    def test_heading_in_list_and_nested_table(self):
        got = html_to_adf('<ul><li><h2>Big</h2></li></ul><table><tr><td><table><tr><td>in</td><td>ner</td></tr>'
                          '</table></td></tr></table>')
        self.assertEqual(got['content'][0]['content'][0]['content'][0], p(t('Big')))
        cell = got['content'][1]['content'][0]['content'][0]
        self.assertEqual(cell['content'], [p(t('in | ner'))])

    def test_table_cells_never_empty_and_header_cells(self):
        got = html_to_adf('<table><thead><tr><th>h</th></tr></thead><tbody><tr><td></td></tr></tbody></table>')
        rows = got['content'][0]['content']
        self.assertEqual(rows[0]['content'][0]['type'], 'tableHeader')
        self.assertEqual(rows[1]['content'][0]['content'], [p()])

    def test_tasks_get_ids(self):
        got = html_to_adf('<ul data-type="tasks"><li data-done="true">a</li><li>b</li></ul>')
        tl = got['content'][0]
        self.assertTrue(tl['attrs']['localId'])
        self.assertEqual([i['attrs']['state'] for i in tl['content']], ['DONE', 'TODO'])
        self.assertTrue(all(i['attrs']['localId'] for i in tl['content']))

    def test_pre_with_br_and_divs(self):
        self.assertEqual(html_to_adf('<pre>a<br>b<div>c</div></pre>'),
                         doc({'type': 'codeBlock', 'content': [t('a\nb\nc')]}))

    def test_scripts_and_images_dropped(self):
        self.assertEqual(html_to_adf('<p>a<img src="x"><script>evil()</script>b</p>'), doc(p(t('ab'))))

    def test_mention_from_editor(self):
        import base64
        import json as _json
        node = {'type': 'mention', 'attrs': {'id': 'acc-1', 'text': '@Åsa Öberg', 'accessLevel': ''}}
        b64 = base64.b64encode(_json.dumps(node, ensure_ascii=False).encode('utf-8')).decode()
        got = html_to_adf(f'<p>Ping <span class="adf-inline mention" contenteditable="false" data-adf="{b64}">@Åsa Öberg</span>&nbsp;now</p>')
        self.assertEqual(got, doc(p(t('Ping '), node, t(' now'))))

    def test_unknown_marks_kept(self):
        ann = {'type': 'annotation', 'attrs': {'id': 'c1', 'annotationType': 'inlineComment'}}
        d = doc(p(t('see '), t('this', ann)))
        self.assertEqual(html_to_adf(adf_to_html(d)), d)


if __name__ == '__main__':
    unittest.main()
