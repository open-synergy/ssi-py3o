# Copyright 2026 OpenSynergy Indonesia
# Copyright 2026 PT. Simetri Sinergi Indonesia
# License AGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
"""Every method here is pure Python -- trigger P1 (L-01: ``action: call``
in odoo-yaml-test discards the method's return value, and
``_get_html_text`` is exactly that: a method whose string/``Markup``
return value is what is being asserted, not a side effect on a
record). See the skill odoo-development-unit-test,
references/python-escape-hatch.md.
"""
import re

from genshi.core import Markup
from odoo_yaml_test import YamlTransactionCase

from odoo.tests import tagged


@tagged("post_install", "-at_install")
class TestHtmlText(YamlTransactionCase):
    """Cover ``py3o.report._get_html_text`` (Html field -> ODF markup)."""

    def setUp(self):
        """Bind an empty ``py3o.report`` recordset to call the method on.

        None of the ``_get_html_text*`` helpers read ``self`` — an
        empty recordset is enough to call them, same pattern as
        ``TestOdfMerge`` in ``test_odf_merge.py``.
        """
        super().setUp()
        self.engine = self.env["py3o.report"]

    def test_bold_from_style_attribute(self):
        """Detect Bold from ``style="font-weight: bolder"``.

        This is the bug reported in HT/26/000756: the old ad-hoc
        parser only recognised the ``<b>``/``<strong>`` tags, not the
        ``style`` attribute that the Odoo Html editor actually emits.

        Pure Python -- trigger P1 (L-01: asserting the return value
        of ``_get_html_text``, not a record side effect).
        """
        result = self.engine._get_html_text(
            '<p><span style="font-weight: bolder;">Context</span> ' "teks biasa</p>"
        )
        self.assertIsInstance(result, Markup)
        self.assertIn(
            '<text:span text:style-name="Bold">Context</text:span>',
            str(result),
        )
        self.assertIn("Context</text:span> teks biasa", str(result))
        self.assertNotIn("<text:p>", str(result))

    def test_bold_italic_underline_from_semantic_tags(self):
        """Keep detecting Bold/Italic/Underline from semantic tags.

        Guards against a regression while fixing the ``style``
        attribute detection above.

        Pure Python -- trigger P1 (L-01: asserting the return value
        of ``_get_html_text``, not a record side effect).
        """
        result = str(
            self.engine._get_html_text("<b>Bold</b> <i>Italic</i> <u>Underline</u>")
        )
        self.assertIn('<text:span text:style-name="Bold">Bold</text:span>', result)
        self.assertIn('<text:span text:style-name="Italic">Italic</text:span>', result)
        self.assertIn(
            '<text:span text:style-name="Underline">Underline</text:span>',
            result,
        )

    def test_unordered_list_bullets(self):
        """Render ``<ul><li>`` items as bullet-prefixed lines.

        Items are separated by ``<text:line-break/>`` instead of a new
        ``<text:p>``, per the structural constraint in the issue.

        Pure Python -- trigger P1 (L-01: asserting the return value
        of ``_get_html_text``, not a record side effect).
        """
        result = str(self.engine._get_html_text("<ul><li>A</li><li>B</li></ul>"))
        self.assertIn("<text:line-break/>", result)
        self.assertIn("• A", result)
        self.assertIn("• B", result)
        self.assertLess(result.index("A"), result.index("B"))
        self.assertNotIn("<text:p>", result)

    def test_ordered_list_numbers(self):
        """Render ``<ol><li>`` items with ``1.``/``2.`` prefixes.

        Pure Python -- trigger P1 (L-01: asserting the return value
        of ``_get_html_text``, not a record side effect).
        """
        result = str(self.engine._get_html_text("<ol><li>A</li><li>B</li></ol>"))
        self.assertIn("1. A", result)
        self.assertIn("2. B", result)

    def test_empty_input_returns_empty_markup(self):
        """Return ``Markup("")`` for ``False``/blank Html values.

        Pure Python -- trigger P1 (L-01: asserting the return value
        of ``_get_html_text``, not a record side effect).
        """
        self.assertEqual(self.engine._get_html_text(False), Markup(""))
        self.assertEqual(self.engine._get_html_text("   "), Markup(""))

    def test_heading_uses_its_own_font_size_style(self):
        """Give each ``<h1>``-``<h6>`` its own font-size style, not Bold.

        Regression guard: headings used to be rendered by passing
        ``bold=True`` into the run builder, giving every level the
        exact same look (Bold only, no size difference).

        Pure Python -- trigger P1 (L-01: asserting the return value
        of ``_get_html_text``, not a record side effect).
        """
        result = str(self.engine._get_html_text("<h1>Title</h1>"))
        self.assertEqual(
            result, '<text:span text:style-name="OdooH1">Title</text:span>'
        )

    def test_heading_levels_use_different_styles(self):
        """Give ``<h1>`` and ``<h6>`` distinct style names.

        Pure Python -- trigger P1 (L-01: asserting the return value
        of ``_get_html_text``, not a record side effect).
        """
        h1 = str(self.engine._get_html_text("<h1>A</h1>"))
        h6 = str(self.engine._get_html_text("<h6>A</h6>"))
        self.assertIn("OdooH1", h1)
        self.assertIn("OdooH6", h6)
        self.assertNotEqual(h1, h6)

    def test_color_keyword_resolves_to_registered_style(self):
        """Resolve ``style="color: red"`` to the pre-registered style.

        Pure Python -- trigger P1 (L-01: asserting the return value
        of ``_get_html_text``, not a record side effect).
        """
        result = str(self.engine._get_html_text('<span style="color: red;">X</span>'))
        self.assertEqual(
            result, '<text:span text:style-name="OdooColor_ff0000">X</text:span>'
        )

    def test_color_hex_matching_a_keyword_resolves_the_same_way(self):
        """Resolve a hex color equal to a known keyword's hex value.

        Pure Python -- trigger P1 (L-01: asserting the return value
        of ``_get_html_text``, not a record side effect).
        """
        result = str(
            self.engine._get_html_text('<span style="color: #FF0000;">X</span>')
        )
        self.assertEqual(
            result, '<text:span text:style-name="OdooColor_ff0000">X</text:span>'
        )

    def test_font_color_attribute_is_recognized(self):
        """Resolve the legacy ``<font color="...">`` attribute too.

        Pure Python -- trigger P1 (L-01: asserting the return value
        of ``_get_html_text``, not a record side effect).
        """
        result = str(self.engine._get_html_text('<font color="red">X</font>'))
        self.assertEqual(
            result, '<text:span text:style-name="OdooColor_ff0000">X</text:span>'
        )

    def test_unregistered_color_renders_without_color(self):
        """Fall back to plain text for a color outside the fixed set.

        ODF text runs can only reference an already-declared style
        (see ``_py3o_ensure_html_text_styles``), so an arbitrary hex
        not equal to one of the 16 registered keywords cannot be
        supported -- it must render without color, not raise or
        reference a style that does not exist.

        Pure Python -- trigger P1 (L-01: asserting the return value
        of ``_get_html_text``, not a record side effect).
        """
        result = str(
            self.engine._get_html_text('<span style="color: #123456;">X</span>')
        )
        self.assertEqual(result, "X")

    def test_heading_and_color_combine(self):
        """Nest a heading's colored text as color-inside-heading.

        Pure Python -- trigger P1 (L-01: asserting the return value
        of ``_get_html_text``, not a record side effect).
        """
        result = str(self.engine._get_html_text('<h2 style="color: blue;">Title</h2>'))
        self.assertEqual(
            result,
            '<text:span text:style-name="OdooH2">'
            '<text:span text:style-name="OdooColor_0000ff">Title</text:span>'
            "</text:span>",
        )

    def test_whitespace_only_paragraph_adds_no_line(self):
        """Drop a stray whitespace-only ``<p>`` between two paragraphs.

        Such paragraphs are a common paste artifact in the Odoo Html
        editor; the browser collapses them to ~0 height, so they must
        not add a line break here either (HT/26/000770-style regression
        guard: they were previously inflating paragraph spacing).

        Pure Python -- trigger P1 (L-01: asserting the return value
        of ``_get_html_text``, not a record side effect).
        """
        result = str(self.engine._get_html_text("<p>A</p><p>\n\t\n</p><p>B</p>"))
        self.assertEqual(result, "A<text:line-break/>B")

    def test_br_only_paragraph_adds_exactly_one_blank_line(self):
        """Render a ``<p><br></p>`` as exactly one blank line.

        Not two: the paragraph's own ``<br>`` and the normal join
        break between paragraphs must not both count, or a single
        deliberate blank line renders as two.

        Pure Python -- trigger P1 (L-01: asserting the return value
        of ``_get_html_text``, not a record side effect).
        """
        result = str(self.engine._get_html_text("<p>A</p><p><br></p><p>B</p>"))
        self.assertEqual(result, "A<text:line-break/><text:line-break/>B")

    def test_table_renders_as_real_odf_table(self):
        """Render ``<table>`` as a genuine nested ``<table:table>``.

        The surrounding ``<text:p>`` is closed before the table and
        reopened (empty) after it -- see ``_get_html_text_table`` for
        why that is the correct escape sequence here. ``<th>`` cells
        render Bold; row/cell counts match the source HTML.

        Pure Python -- trigger P1 (L-01: asserting the return value
        of ``_get_html_text``, not a record side effect).
        """
        result = str(
            self.engine._get_html_text(
                "<table><tr><th>Col 1</th><th>Col 2</th></tr>"
                "<tr><td>A</td><td><i>B</i></td></tr></table>"
            )
        )
        self.assertIn("</text:span></text:p><table:table", result)
        self.assertIn("</table:table><text:p><text:span>", result)
        self.assertEqual(result.count("<table:table-row>"), 2)
        self.assertEqual(result.count("<table:table-column/>"), 2)
        self.assertIn('<text:span text:style-name="Bold">Col 1</text:span>', result)
        self.assertIn(
            '<text:p><text:span text:style-name="Italic">B</text:span></text:p>',
            result,
        )
        self.assertLess(result.index("Col 1"), result.index(">A<"))

    def test_table_name_is_unique_across_calls(self):
        """Give each rendered table a unique ``table:name``.

        ODF requires unique table names within one document; a report
        that calls ``get_html_text`` more than once (e.g. inside a
        ``py3o://for=`` loop) must not collide.

        Pure Python -- trigger P1 (L-01: asserting the return value
        of ``_get_html_text``, not a record side effect).
        """
        first = str(self.engine._get_html_text("<table><tr><td>A</td></tr></table>"))
        second = str(self.engine._get_html_text("<table><tr><td>B</td></tr></table>"))
        name_re = re.compile(r'table:name="([^"]+)"')
        self.assertNotEqual(
            name_re.search(first).group(1), name_re.search(second).group(1)
        )

    def test_thead_rows_wrapped_for_page_repeat(self):
        """Wrap ``<thead>`` rows in ``<table:table-header-rows>``.

        So a renderer repeats the header row on every page a long
        table spans, instead of each page fragment showing bare
        bordered cells with no header -- which reads as two
        disconnected tables rather than one continuing table.

        Pure Python -- trigger P1 (L-01: asserting the return value
        of ``_get_html_text``, not a record side effect).
        """
        result = str(
            self.engine._get_html_text(
                "<table><thead><tr><th>Col 1</th></tr></thead>"
                "<tbody><tr><td>A</td></tr><tr><td>B</td></tr></tbody></table>"
            )
        )
        self.assertIn("<table:table-header-rows><table:table-row>", result)
        header_end = result.index("</table:table-header-rows>")
        self.assertLess(result.index("Col 1"), header_end)
        self.assertGreater(result.index(">A<"), header_end)
        self.assertGreater(result.index(">B<"), header_end)

    def test_table_without_thead_has_no_header_rows_wrapper(self):
        """Leave a plain ``<table>`` (no ``<thead>``) unaffected.

        Pure Python -- trigger P1 (L-01: asserting the return value
        of ``_get_html_text``, not a record side effect).
        """
        result = str(self.engine._get_html_text("<table><tr><td>A</td></tr></table>"))
        self.assertNotIn("table-header-rows", result)

    def test_unknown_tag_falls_back_to_plain_text(self):
        """Render unsupported tags as plain text instead of raising.

        Pure Python -- trigger P1 (L-01: asserting the return value
        of ``_get_html_text``, not a record side effect).
        """
        result = str(self.engine._get_html_text("<mark>teks</mark>"))
        self.assertIn("teks", result)
