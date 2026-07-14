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

from .common import LOGO_PNG_BYTES, base_odt, report_odt

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
        props = self._page_layout(styles_root).find(_q("style", "page-layout-properties"))
        self.assertEqual(props.get(_q("fo", "page-width")), "11.69in")
        self.assertEqual(props.get(_q("style", "print-orientation")), "landscape")
        self.assertEqual(props.get(_q("fo", "margin-left")), "2cm")

    def test_margins_copied(self):
        merged = self._merge()
        styles_root = self._styles_of(merged)
        props = self._page_layout(styles_root).find(_q("style", "page-layout-properties"))
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
        report = self._create_report(
            py3o_filetype="pdf", template_data=template_data
        )
        report.py3o_template_id.filetype = "ods"
        report.py3o_base_template_id = self._create_template(
            "Base", "odt", base_template_data
        )
        py3o_report = self.env["py3o.report"].create({"ir_actions_report_id": report.id})
        result = py3o_report.get_template(self.env["res.partner"])
        self.assertEqual(result, base64.b64decode(template_data))

    def test_fail_open(self):
        template_data = base64.b64encode(report_odt())
        report = self._create_report(py3o_filetype="pdf", template_data=template_data)
        report.py3o_base_template_id = self._create_template(
            "Corrupt Base", "odt", base64.b64encode(b"not a zip")
        )
        py3o_report = self.env["py3o.report"].create({"ir_actions_report_id": report.id})
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
        py3o_report = self.env["py3o.report"].create({"ir_actions_report_id": report.id})
        # A fresh partner with no company_id of its own, so the extender must
        # fall back to env.company rather than "objects[0].company_id".
        partner = self.env["res.partner"].create({"name": "No Company Partner"})
        context = py3o_report._get_parser_context(partner, {})
        self.assertIn("company", context)
        self.assertEqual(context["company"], self.env.company)
        self.assertEqual(context["company_partner"], self.env.company.partner_id)

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
