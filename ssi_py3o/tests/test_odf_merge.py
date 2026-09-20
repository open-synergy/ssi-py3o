# Copyright 2026 OpenSynergy Indonesia
# Copyright 2026 PT. Simetri Sinergi Indonesia
# License AGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
"""Deviation from the odoo-yaml-test standard (documented, see the skill
odoo-development-unit-test): the ODF merge engine operates on raw zip bytes
and XML, not on Odoo records — there is no `model`/`values`/`assert` in
odoo-yaml-test that can express "element <style:header> in the merged
styles.xml contains text X". This file is therefore a plain TransactionCase,
mirroring the existing pattern in ssi_account_move_py3o_report/tests/.

CI runs on Python 3.6 — no walrus operator (`:=`).
"""
import base64
from io import BytesIO
from zipfile import ZipFile

from lxml import etree

from odoo.tests import TransactionCase, tagged

from .common import LOGO_PNG_BYTES, base_odt, content_odt, report_odt

NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "xlink": "http://www.w3.org/1999/xlink",
    "manifest": "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0",
}


def _q(prefix, local):
    return "{%s}%s" % (NS[prefix], local)


@tagged("post_install", "-at_install")
class TestOdfMerge(TransactionCase):
    def setUp(self):
        super().setUp()
        # None of the merge helpers read `self` — an empty recordset is
        # enough to call them.
        self.engine = self.env["py3o.report"]

    def _merge(self, report_bytes=None, base_bytes=None):
        return self.engine._py3o_merge_base_template(
            report_bytes if report_bytes is not None else report_odt(),
            base_bytes if base_bytes is not None else base_odt(),
        )

    def _styles_of(self, merged_bytes):
        with ZipFile(BytesIO(merged_bytes)) as zf:
            return etree.fromstring(zf.read("styles.xml"))

    def _master_page(self, styles_root, name="Standard"):
        for mp in styles_root.iter(_q("style", "master-page")):
            if mp.get(_q("style", "name")) == name:
                return mp
        return None

    def _page_layout(self, styles_root, name="Mpm1"):
        for pl in styles_root.iter(_q("style", "page-layout")):
            if pl.get(_q("style", "name")) == name:
                return pl
        return None

    def test_header_injected(self):
        merged = self._merge()
        styles_root = self._styles_of(merged)
        mp = self._master_page(styles_root)
        header = mp.find(_q("style", "header"))
        self.assertIsNotNone(header)
        self.assertIn("ACME LETTERHEAD", "".join(header.itertext()))

    def test_report_geometry_survives(self):
        merged = self._merge()
        styles_root = self._styles_of(merged)
        props = self._page_layout(styles_root).find(
            _q("style", "page-layout-properties")
        )
        self.assertEqual(props.get(_q("fo", "page-width")), "11.69in")
        self.assertEqual(props.get(_q("style", "print-orientation")), "landscape")
        self.assertEqual(props.get(_q("fo", "margin-left")), "2cm")

    def test_margins_copied(self):
        merged = self._merge()
        styles_root = self._styles_of(merged)
        props = self._page_layout(styles_root).find(
            _q("style", "page-layout-properties")
        )
        self.assertEqual(props.get(_q("fo", "margin-top")), "0.7874in")
        self.assertEqual(props.get(_q("fo", "margin-bottom")), "0.7874in")

    def test_header_style_reserves_space(self):
        merged = self._merge()
        styles_root = self._styles_of(merged)
        page_layout = self._page_layout(styles_root)
        header_style = page_layout.find(_q("style", "header-style"))
        props = header_style.find(_q("style", "header-footer-properties"))
        self.assertEqual(props.get(_q("fo", "min-height")), "1cm")
        self.assertEqual(props.get(_q("fo", "margin-bottom")), "0.5cm")
        # ODF §16.5 order: page-layout-properties, header-style, footer-style.
        tags = [child.tag for child in page_layout]
        self.assertEqual(
            tags,
            [
                _q("style", "page-layout-properties"),
                _q("style", "header-style"),
                _q("style", "footer-style"),
            ],
        )

    def test_style_collision(self):
        merged = self._merge()
        styles_root = self._styles_of(merged)

        # The report's own P1 (italic) survives untouched...
        report_p1 = None
        for style in styles_root.iter(_q("style", "style")):
            if style.get(_q("style", "name")) == "P1":
                report_p1 = style
        self.assertIsNotNone(report_p1)
        text_props = report_p1.find(_q("style", "text-properties"))
        self.assertEqual(text_props.get(_q("fo", "font-style")), "italic")

        # ...while the header now references sbt_P1, not P1.
        mp = self._master_page(styles_root)
        header_p = mp.find(_q("style", "header")).find(_q("text", "p"))
        self.assertEqual(header_p.get(_q("text", "style-name")), "sbt_P1")

        sbt_p1 = None
        sbt_header = None
        for style in styles_root.iter(_q("style", "style")):
            name = style.get(_q("style", "name"))
            if name == "sbt_P1":
                sbt_p1 = style
            elif name == "sbt_Header":
                sbt_header = style
        self.assertIsNotNone(sbt_p1)
        self.assertIsNotNone(sbt_header)
        self.assertEqual(sbt_p1.get(_q("style", "parent-style-name")), "sbt_Header")

    def test_font_faces(self):
        merged = self._merge()
        styles_root = self._styles_of(merged)
        font_decls = styles_root.find(_q("office", "font-face-decls"))
        names = {f.get(_q("style", "name")) for f in font_decls}
        self.assertIn("Report Font", names)
        self.assertIn("Base Font", names)
        self.assertFalse(any(n.startswith("sbt_") for n in names if n))

    def test_image_copied(self):
        merged = self._merge()
        with ZipFile(BytesIO(merged)) as zf:
            self.assertIn("Pictures/logo.png", zf.namelist())
            self.assertEqual(zf.read("Pictures/logo.png"), LOGO_PNG_BYTES)
            manifest_root = etree.fromstring(zf.read("META-INF/manifest.xml"))
        entries = {
            e.get(_q("manifest", "full-path")): e.get(_q("manifest", "media-type"))
            for e in manifest_root
        }
        self.assertEqual(entries.get("Pictures/logo.png"), "image/png")

    def test_image_collision(self):
        own_logo = b"REPORT-OWN-LOGO-DIFFERENT-BYTES"
        report_bytes = report_odt(extra_files={"Pictures/logo.png": own_logo})
        merged = self._merge(report_bytes=report_bytes)
        with ZipFile(BytesIO(merged)) as zf:
            names = zf.namelist()
            self.assertIn("Pictures/logo.png", names)
            self.assertEqual(zf.read("Pictures/logo.png"), own_logo)
            new_images = [n for n in names if n.startswith("Pictures/sbt_")]
            self.assertEqual(len(new_images), 1)
            self.assertEqual(zf.read(new_images[0]), LOGO_PNG_BYTES)
        styles_root = self._styles_of(merged)
        mp = self._master_page(styles_root)
        image = mp.find(_q("style", "header")).find(".//" + _q("draw", "image"))
        self.assertEqual(image.get(_q("xlink", "href")), new_images[0])

    def test_mimetype_first_and_stored(self):
        merged = self._merge()
        with ZipFile(BytesIO(merged)) as zf:
            first = zf.infolist()[0]
            self.assertEqual(first.filename, "mimetype")
            self.assertEqual(first.compress_type, 0)  # ZIP_STORED
            # still a valid, openable ODT afterwards
            self.assertEqual(
                zf.read("mimetype").decode(),
                "application/vnd.oasis.opendocument.text",
            )

    def test_master_page_child_order(self):
        merged = self._merge()
        styles_root = self._styles_of(merged)
        mp = self._master_page(styles_root)
        tags = [child.tag for child in mp]
        self.assertEqual(tags, [_q("style", "header"), _q("style", "footer")])

    def test_idempotent(self):
        merged_once = self._merge()
        merged_twice = self._merge(report_bytes=merged_once)
        styles_once = etree.tostring(self._styles_of(merged_once))
        styles_twice = etree.tostring(self._styles_of(merged_twice))
        self.assertNotIn(b"sbt_sbt_", styles_twice)
        self.assertEqual(styles_once, styles_twice)

    def test_skip_non_odt_template(self):
        template_data = base64.b64encode(report_odt())
        base_template_data = base64.b64encode(base_odt())
        report = self._create_report(py3o_filetype="pdf", template_data=template_data)
        report.py3o_template_id.filetype = "ods"
        report.py3o_base_template_id = self._create_template(
            "Base", "odt", base_template_data
        )
        py3o_report = self.env["py3o.report"].create(
            {"ir_actions_report_id": report.id}
        )
        result = py3o_report.get_template(self.env["res.partner"])
        self.assertEqual(result, base64.b64decode(template_data))

    def test_fail_open(self):
        template_data = base64.b64encode(report_odt())
        report = self._create_report(py3o_filetype="pdf", template_data=template_data)
        report.py3o_base_template_id = self._create_template(
            "Corrupt Base", "odt", base64.b64encode(b"not a zip")
        )
        py3o_report = self.env["py3o.report"].create(
            {"ir_actions_report_id": report.id}
        )
        with self.assertLogs("odoo.addons.ssi_py3o", level="ERROR"):
            result = py3o_report.get_template(self.env["res.partner"])
        self.assertEqual(result, base64.b64decode(template_data))
        with self.assertRaises(ValueError):
            py3o_report.with_context(py3o_base_template_strict=True).get_template(
                self.env["res.partner"]
            )

    def test_extender_registers_company(self):
        template_data = base64.b64encode(report_odt())
        report = self._create_report(py3o_filetype="pdf", template_data=template_data)
        py3o_report = self.env["py3o.report"].create(
            {"ir_actions_report_id": report.id}
        )
        # A fresh partner with no company_id of its own, so the extender must
        # fall back to env.company rather than "objects[0].company_id".
        partner = self.env["res.partner"].create({"name": "No Company Partner"})
        context = py3o_report._get_parser_context(partner, {})
        self.assertIn("company", context)
        self.assertEqual(context["company"], self.env.company)
        self.assertEqual(context["company_partner"], self.env.company.partner_id)

    # -- _py3o_ensure_html_table_style() ---------------------------------------
    # Exercises the border style injected for _get_html_text_table()'s cells
    # (see py3o_report.py). Uses content_odt(), not report_odt(): the latter's
    # content.xml has no <office:automatic-styles> at all, so it cannot prove
    # anything about a style added to that element.

    def _content_of(self, odt_bytes):
        """Parse ``odt_bytes``' ``content.xml`` into an lxml element."""
        with ZipFile(BytesIO(odt_bytes)) as zf:
            return etree.fromstring(zf.read("content.xml"))

    def test_html_table_style_injected(self):
        """Add the bordered ``OdooHtmlTableCell`` table-cell style.

        Pure Python -- trigger P8 (L-01, L-19: this asserts the ODT
        template's own ``content.xml`` bytes, a report-rendering
        artifact no ``odoo-yaml-test`` action can produce or inspect).
        """
        result = self.engine._py3o_ensure_html_table_style(content_odt())
        content_root = self._content_of(result)
        auto_styles = content_root.find(_q("office", "automatic-styles"))
        style = None
        for child in auto_styles:
            if child.get(_q("style", "name")) == "OdooHtmlTableCell":
                style = child
                break
        self.assertIsNotNone(style)
        self.assertEqual(style.get(_q("style", "family")), "table-cell")
        props = style.find(_q("style", "table-cell-properties"))
        self.assertIsNotNone(props)
        for side in ("top", "bottom", "left", "right"):
            self.assertTrue(props.get(_q("fo", "border-%s" % side)))

    def test_html_list_item_style_injected(self):
        """Add the ``OdooListItem`` paragraph style with its own font-size.

        Regression guard: this style must carry its own ``fo:font-
        size`` (matching ``OdooHtmlTableText``'s reasoning) -- each
        ``<li>`` is its own new paragraph (see ``_get_html_text_list``),
        not the placeholder's own, so without an explicit size it falls
        back to the same oversized document default a missing
        ``OdooHtmlTableText`` would. Unlike ``OdooHtmlTableText``, no
        margin-left/text-indent belongs on this style: the hang indent
        instead comes from the ``OdooOL``/``OdooUL`` list styles (see
        ``test_html_list_styles_injected``) that this same method
        injects.

        Pure Python -- trigger P8 (L-01, L-19: same as
        ``test_html_table_style_injected`` above).
        """
        result = self.engine._py3o_ensure_html_table_style(content_odt())
        content_root = self._content_of(result)
        auto_styles = content_root.find(_q("office", "automatic-styles"))
        style = None
        for child in auto_styles:
            if child.get(_q("style", "name")) == "OdooListItem":
                style = child
                break
        self.assertIsNotNone(style)
        self.assertEqual(style.get(_q("style", "family")), "paragraph")
        self.assertIsNone(style.find(_q("style", "paragraph-properties")))
        text_props = style.find(_q("style", "text-properties"))
        self.assertIsNotNone(text_props)
        self.assertTrue(text_props.get(_q("fo", "font-size")))

    def test_html_list_styles_injected(self):
        """Add the ``OdooOL``/``OdooUL`` ``text:list-style`` elements.

        Each carries one level (number for ``OdooOL``, bullet for
        ``OdooUL``) with a ``style:list-level-label-alignment`` --
        this is what lets the renderer compute a wrapped continuation
        line's hang indent against the marker it generates, instead of
        this module guessing a fixed indent (see
        ``_HTML_LIST_ITEM_INDENT``'s comment).

        Pure Python -- trigger P8 (L-01, L-19: same as
        ``test_html_table_style_injected`` above).
        """
        result = self.engine._py3o_ensure_html_table_style(content_odt())
        content_root = self._content_of(result)
        auto_styles = content_root.find(_q("office", "automatic-styles"))

        ol_style = None
        ul_style = None
        for child in auto_styles:
            name = child.get(_q("style", "name"))
            if name == "OdooOL":
                ol_style = child
            elif name == "OdooUL":
                ul_style = child
        self.assertIsNotNone(ol_style)
        self.assertIsNotNone(ul_style)

        number_level = ol_style.find(_q("text", "list-level-style-number"))
        self.assertIsNotNone(number_level)
        self.assertEqual(number_level.get(_q("style", "num-format")), "1")

        bullet_level = ul_style.find(_q("text", "list-level-style-bullet"))
        self.assertIsNotNone(bullet_level)
        self.assertTrue(bullet_level.get(_q("text", "bullet-char")))

        for level in (number_level, bullet_level):
            level_props = level.find(_q("style", "list-level-properties"))
            self.assertIsNotNone(level_props)
            alignment = level_props.find(_q("style", "list-level-label-alignment"))
            self.assertIsNotNone(alignment)
            self.assertTrue(alignment.get(_q("fo", "margin-left")))
            self.assertTrue(alignment.get(_q("fo", "text-indent")).startswith("-"))

    def test_html_table_style_idempotent(self):
        """Leave an already-present same-named style untouched.

        Pre-seeds a style of the same name, distinguishable from what
        the method itself would generate (no ``fo:border``), to prove
        a second call leaves an already-present style untouched
        rather than duplicating or overwriting it.

        Pure Python -- trigger P8 (L-01, L-19: same as
        ``test_html_table_style_injected`` above).
        """
        preseeded = (
            '<style:style xmlns:style="urn:oasis:names:tc:opendocument:'
            'xmlns:style:1.0" style:name="OdooHtmlTableCell" '
            'style:family="table-cell"/>'
        )
        source = content_odt(auto_styles_inner=preseeded)
        result = self.engine._py3o_ensure_html_table_style(source)
        content_root = self._content_of(result)
        auto_styles = content_root.find(_q("office", "automatic-styles"))
        matches = [
            c for c in auto_styles if c.get(_q("style", "name")) == "OdooHtmlTableCell"
        ]
        self.assertEqual(len(matches), 1)
        self.assertIsNone(matches[0].find(_q("style", "table-cell-properties")))

    def test_html_table_style_fails_open_on_corrupt_input(self):
        """Return the input bytes unchanged when it is not a valid ODT.

        Pure Python -- trigger P8 (L-01, L-19: same as
        ``test_html_table_style_injected`` above).
        """
        result = self.engine._py3o_ensure_html_table_style(b"not a zip")
        self.assertEqual(result, b"not a zip")

    # -- _py3o_ensure_html_text_styles() ---------------------------------------
    # Exercises the heading-size and color styles used by _get_html_text_run()
    # (see py3o_report.py). Same content_odt() fixture as the table-cell style
    # tests above, for the same reason (report_odt()'s content.xml has no
    # <office:automatic-styles> to prove anything against).

    def test_text_styles_injected(self):
        """Add all 6 heading styles and all 16 color styles.

        Pure Python -- trigger P8 (L-01, L-19: same as
        ``test_html_table_style_injected`` above).
        """
        result = self.engine._py3o_ensure_html_text_styles(content_odt())
        content_root = self._content_of(result)
        auto_styles = content_root.find(_q("office", "automatic-styles"))
        names = {c.get(_q("style", "name")) for c in auto_styles}
        for expected in ("OdooH1", "OdooH2", "OdooH3", "OdooH4", "OdooH5", "OdooH6"):
            self.assertIn(expected, names)
        self.assertIn("OdooColor_ff0000", names)  # red
        self.assertIn("OdooColor_0000ff", names)  # blue
        h1 = next(c for c in auto_styles if c.get(_q("style", "name")) == "OdooH1")
        h1_props = h1.find(_q("style", "text-properties"))
        self.assertEqual(h1_props.get(_q("fo", "font-size")), "24pt")
        red = next(
            c for c in auto_styles if c.get(_q("style", "name")) == "OdooColor_ff0000"
        )
        red_props = red.find(_q("style", "text-properties"))
        self.assertEqual(red_props.get(_q("fo", "color")), "#ff0000")

    def test_text_styles_idempotent(self):
        """Leave an already-present same-named style untouched.

        Mirrors ``test_html_table_style_idempotent``: pre-seeds
        ``OdooH1`` distinguishably (no ``fo:font-size``) to prove a
        second call does not duplicate or overwrite it, while still
        adding the other 21 styles that were genuinely missing.

        Pure Python -- trigger P8 (L-01, L-19: same as
        ``test_html_table_style_injected`` above).
        """
        preseeded = (
            '<style:style xmlns:style="urn:oasis:names:tc:opendocument:'
            'xmlns:style:1.0" style:name="OdooH1" style:family="text"/>'
        )
        source = content_odt(auto_styles_inner=preseeded)
        result = self.engine._py3o_ensure_html_text_styles(source)
        content_root = self._content_of(result)
        auto_styles = content_root.find(_q("office", "automatic-styles"))
        matches = [c for c in auto_styles if c.get(_q("style", "name")) == "OdooH1"]
        self.assertEqual(len(matches), 1)
        self.assertIsNone(matches[0].find(_q("style", "text-properties")))
        # the other 21 styles were still added around the pre-seeded one
        names = {c.get(_q("style", "name")) for c in auto_styles}
        self.assertIn("OdooH2", names)
        self.assertIn("OdooColor_ff0000", names)

    def test_text_styles_fail_open_on_corrupt_input(self):
        """Return the input bytes unchanged when it is not a valid ODT.

        Pure Python -- trigger P8 (L-01, L-19: same as
        ``test_html_table_style_injected`` above).
        """
        result = self.engine._py3o_ensure_html_text_styles(b"not a zip")
        self.assertEqual(result, b"not a zip")

    # -- helpers --------------------------------------------------------------

    def _create_template(self, name, filetype, template_data):
        return self.env["py3o.template"].create(
            {"name": name, "filetype": filetype, "py3o_template_data": template_data}
        )

    def _create_report(self, py3o_filetype, template_data):
        template = self._create_template("Report Template", "odt", template_data)
        return self.env["ir.actions.report"].create(
            {
                "name": "Test Report",
                "model": "res.partner",
                "report_name": "ssi_py3o.test_report_merge",
                "report_type": "py3o",
                "py3o_filetype": py3o_filetype,
                "py3o_template_id": template.id,
            }
        )
