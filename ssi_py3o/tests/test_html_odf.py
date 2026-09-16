# Copyright 2026 OpenSynergy Indonesia
# Copyright 2026 PT. Simetri Sinergi Indonesia
# License AGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
"""Every method here is pure Python -- trigger P1 (L-01: ``action: call``
in odoo-yaml-test discards the method's return value, and
``_get_html_odf`` is exactly that: a method whose string/``Markup``
return value is what is being asserted, not a side effect on a
record). See the skill odoo-development-unit-test,
references/python-escape-hatch.md.
"""
from genshi.core import Markup
from odoo_yaml_test import YamlTransactionCase

from odoo.tests import tagged


@tagged("post_install", "-at_install")
class TestHtmlOdf(YamlTransactionCase):
    """Cover ``py3o.report._get_html_odf`` (Html field -> ODF markup)."""

    def setUp(self):
        """Bind an empty ``py3o.report`` recordset to call the method on.

        None of the ``_get_html_odf*`` helpers read ``self`` — an
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
        of ``_get_html_odf``, not a record side effect).
        """
        result = self.engine._get_html_odf(
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
        of ``_get_html_odf``, not a record side effect).
        """
        result = str(
            self.engine._get_html_odf("<b>Bold</b> <i>Italic</i> <u>Underline</u>")
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
        of ``_get_html_odf``, not a record side effect).
        """
        result = str(self.engine._get_html_odf("<ul><li>A</li><li>B</li></ul>"))
        self.assertIn("<text:line-break/>", result)
        self.assertIn("• A", result)
        self.assertIn("• B", result)
        self.assertLess(result.index("A"), result.index("B"))
        self.assertNotIn("<text:p>", result)

    def test_ordered_list_numbers(self):
        """Render ``<ol><li>`` items with ``1.``/``2.`` prefixes.

        Pure Python -- trigger P1 (L-01: asserting the return value
        of ``_get_html_odf``, not a record side effect).
        """
        result = str(self.engine._get_html_odf("<ol><li>A</li><li>B</li></ol>"))
        self.assertIn("1. A", result)
        self.assertIn("2. B", result)

    def test_empty_input_returns_empty_markup(self):
        """Return ``Markup("")`` for ``False``/blank Html values.

        Pure Python -- trigger P1 (L-01: asserting the return value
        of ``_get_html_odf``, not a record side effect).
        """
        self.assertEqual(self.engine._get_html_odf(False), Markup(""))
        self.assertEqual(self.engine._get_html_odf("   "), Markup(""))

    def test_unknown_tag_falls_back_to_plain_text(self):
        """Render unsupported tags as plain text instead of raising.

        Pure Python -- trigger P1 (L-01: asserting the return value
        of ``_get_html_odf``, not a record side effect).
        """
        result = str(self.engine._get_html_odf("<mark>teks</mark>"))
        self.assertIn("teks", result)
