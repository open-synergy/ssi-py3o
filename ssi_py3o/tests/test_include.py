# Copyright 2026 OpenSynergy Indonesia
# Copyright 2026 PT. Simetri Sinergi Indonesia
# License AGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
"""Deviation from the odoo-yaml-test standard (documented, see the skill
odoo-development-unit-test): the sub-template include engine works on raw
zip bytes and XML, not on Odoo records, so assertions on the content of the
resulting package cannot be expressed in YAML (P4, L-05, L-07, L-08). The
``include_code`` constraint itself is tested in YAML, see
test_include_code.yaml.

CI runs on Python 3.6 -- no walrus operator.
"""
import base64
from io import BytesIO
from zipfile import ZIP_STORED, ZipFile

from lxml import etree
from odoo_yaml_test import YamlTransactionCase

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import LOGO_PNG_BYTES, base_odt, body_odt, report_odt

NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "xlink": "http://www.w3.org/1999/xlink",
    "manifest": "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0",
}


def _q(prefix, local):
    """Return the Clark name of ``local`` in namespace ``prefix``."""
    return "{%s}%s" % (NS[prefix], local)


MARKER = '<text:p text:style-name="P1">[[py3o_include:ident]]</text:p>'
SPLIT_MARKER = (
    "<text:p><text:span>[[py3o_</text:span>"
    "<text:span>include:ident]]</text:span></text:p>"
)
FRAGMENT_BODY = '<text:p text:style-name="P1">IDENTITY BLOCK</text:p>'
FRAGMENT_STYLE = (
    '<style:style style:name="P1" style:family="paragraph" '
    'style:parent-style-name="Standard">'
    '<style:text-properties fo:font-weight="bold"/></style:style>'
)
REPORT_STYLE = (
    '<style:style style:name="P1" style:family="paragraph" '
    'style:parent-style-name="Standard">'
    '<style:text-properties fo:font-style="italic"/></style:style>'
)


@tagged("post_install", "-at_install")
class TestInclude(YamlTransactionCase):
    """Cover the ``[[py3o_include:<code>]]`` sub-template engine."""

    def setUp(self):
        """Prepare helpers to create templates and a py3o report engine."""
        super().setUp()
        self.Template = self.env["py3o.template"]

    def _create_template(self, name, data, filetype="odt", code=False):
        """Create a ``py3o.template`` from raw ODT bytes."""
        return self.Template.create(
            {
                "name": name,
                "filetype": filetype,
                "py3o_template_data": base64.b64encode(data),
                "include_code": code,
            }
        )

    def _create_fragment(self, code="ident", **kwargs):
        """Create the sub-template registered under ``code``."""
        kwargs.setdefault("body_inner", FRAGMENT_BODY)
        kwargs.setdefault("auto_styles_inner", FRAGMENT_STYLE)
        return self._create_template("Fragment", body_odt(**kwargs), code=code)

    def _engine(self, report_data, **report_values):
        """Return a ``py3o.report`` bound to a report using ``report_data``."""
        template = self._create_template("Report Template", report_data)
        values = {
            "name": "Worksheet Report",
            "model": "res.partner",
            "report_name": "ssi_py3o.test_report_include",
            "report_type": "py3o",
            "py3o_filetype": "pdf",
            "py3o_template_id": template.id,
        }
        values.update(report_values)
        report = self.env["ir.actions.report"].create(values)
        return self.env["py3o.report"].create({"ir_actions_report_id": report.id})

    def _content(self, odt_bytes):
        """Parse ``content.xml`` of ``odt_bytes`` into an lxml element."""
        with ZipFile(BytesIO(odt_bytes)) as zf:
            return etree.fromstring(zf.read("content.xml"))

    def _styles(self, odt_bytes):
        """Parse ``styles.xml`` of ``odt_bytes`` into an lxml element."""
        with ZipFile(BytesIO(odt_bytes)) as zf:
            return etree.fromstring(zf.read("styles.xml"))

    def _texts(self, root):
        """Return the full text of every paragraph below ``root``."""
        return ["".join(p.itertext()) for p in root.iter(_q("text", "p"))]

    def _style(self, root, name):
        """Return the style named ``name`` below ``root`` or None."""
        for style in root.iter(_q("style", "style")):
            if style.get(_q("style", "name")) == name:
                return style
        return None

    def _apply(self, report_data, **report_values):
        """Run the include engine over ``report_data``."""
        engine = self._engine(report_data, **report_values)
        return engine._py3o_apply_includes(report_data)

    def test_markers_replaced(self):
        """Replace two markers, one in a table cell.

        Pure Python, trigger P4 and P8 (L-05, L-07, L-08, L-19).
        """
        self._create_fragment()
        cell = (
            "<table:table><table:table-row><table:table-cell>%s"
            "</table:table-cell></table:table-row></table:table>" % MARKER
        )
        result = self._apply(
            body_odt(body_inner=MARKER + "<text:p>Tail</text:p>" + cell)
        )
        texts = self._texts(self._content(result))
        self.assertEqual(texts.count("IDENTITY BLOCK"), 2)
        self.assertIn("Tail", texts)
        self.assertFalse([t for t in texts if "py3o_include" in t])

    def test_split_spans_marker(self):
        """Detect a marker split over several spans.

        Pure Python, trigger P4 and P8 (L-05, L-07, L-08, L-19).
        """
        self._create_fragment()
        result = self._apply(body_odt(body_inner=SPLIT_MARKER))
        texts = self._texts(self._content(result))
        self.assertEqual(texts, ["IDENTITY BLOCK"])

    def test_no_marker_unchanged(self):
        """Return a template without marker byte for byte.

        Pure Python, trigger P4 and P8 (L-05, L-07, L-08, L-19).
        """
        self._create_fragment()
        data = body_odt(body_inner="<text:p>Plain</text:p>")
        self.assertEqual(self._apply(data), data)

    def test_edit_fragment_changes_all_reports(self):
        """Editing the sub-template changes the result.

        Pure Python, trigger P4 and P8 (L-05, L-07, L-08, L-19).
        """
        fragment = self._create_fragment()
        data = body_odt(body_inner=MARKER)
        first = self._apply(data)
        fragment.py3o_template_data = base64.b64encode(
            body_odt(body_inner="<text:p>NEW BLOCK</text:p>")
        )
        second = self._apply(data)
        self.assertIn("IDENTITY BLOCK", self._texts(self._content(first)))
        self.assertEqual(self._texts(self._content(second)), ["NEW BLOCK"])

    def test_style_collision(self):
        """Keep the report style when the sub-template reuses its name.

        Pure Python, trigger P4 and P8 (L-05, L-07, L-08, L-19).
        """
        self._create_fragment()
        data = body_odt(body_inner=MARKER, auto_styles_inner=REPORT_STYLE)
        content = self._content(self._apply(data))
        report_p1 = self._style(content, "P1")
        self.assertEqual(
            report_p1.find(_q("style", "text-properties")).get(
                "{%s}font-style"
                % "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"
            ),
            "italic",
        )
        inserted = [p for p in content.iter(_q("text", "p"))][0]
        self.assertEqual(inserted.get(_q("text", "style-name")), "inc_ident_P1")
        self.assertIsNotNone(self._style(content, "inc_ident_P1"))

    def test_directives_intact(self):
        """Copy py3o links and image frames untouched.

        Pure Python, trigger P4 and P8 (L-05, L-07, L-08, L-19).
        """
        link = (
            '<text:p><text:a xlink:type="simple" '
            'xlink:href="py3o://for each=&quot;i in o.line_ids&quot;">'
            "x</text:a></text:p>"
        )
        frame = (
            "<text:p><draw:frame draw:name=\"py3o.image(o.image, 'png')\">"
            '<draw:image xlink:href="Pictures/logo.png"/></draw:frame></text:p>'
        )
        self._create_template(
            "Fragment",
            body_odt(
                body_inner=link + frame,
                extra_files={"Pictures/logo.png": LOGO_PNG_BYTES},
            ),
            code="ident",
        )
        result = self._apply(body_odt(body_inner=MARKER))
        content = self._content(result)
        hrefs = [a.get(_q("xlink", "href")) for a in content.iter(_q("text", "a"))]
        self.assertEqual(hrefs, ['py3o://for each="i in o.line_ids"'])
        names = [f.get(_q("draw", "name")) for f in content.iter(_q("draw", "frame"))]
        self.assertEqual(names, ["py3o.image(o.image, 'png')"])

    def test_fonts_and_images_prefixed(self):
        """Import fonts and pictures under the ``inc_<code>_`` prefix.

        Pure Python, trigger P4 and P8 (L-05, L-07, L-08, L-19).
        """
        font = '<style:font-face style:name="Report Font" svg:font-family="Other"/>'
        new_font = '<style:font-face style:name="Frag Font" svg:font-family="Frag"/>'
        style = (
            '<style:style style:name="P2" style:family="paragraph">'
            '<style:text-properties style:font-name="Frag Font"/></style:style>'
        )
        frame = (
            '<text:p text:style-name="P2"><draw:frame draw:name="f">'
            '<draw:image xlink:href="Pictures/logo.png"/></draw:frame></text:p>'
        )
        self._create_template(
            "Fragment",
            body_odt(
                body_inner=frame,
                auto_styles_inner=style,
                font_decls_inner=font + new_font,
                extra_files={"Pictures/logo.png": LOGO_PNG_BYTES},
            ),
            code="ident",
        )
        result = self._apply(body_odt(body_inner=MARKER))
        styles = self._styles(result)
        fonts = {
            f.get(_q("style", "name")): f for f in styles.iter(_q("style", "font-face"))
        }
        self.assertIn("Report Font", fonts)
        self.assertIn("inc_ident_Frag Font", fonts)
        self.assertIn("inc_ident_Report Font", fonts)
        content = self._content(result)
        imported = self._style(content, "inc_ident_P2")
        self.assertEqual(
            imported.find(_q("style", "text-properties")).get(_q("style", "font-name")),
            "inc_ident_Frag Font",
        )
        image = next(content.iter(_q("draw", "image")))
        self.assertEqual(image.get(_q("xlink", "href")), "Pictures/inc_ident_logo.png")
        with ZipFile(BytesIO(result)) as zf:
            self.assertEqual(zf.read("Pictures/inc_ident_logo.png"), LOGO_PNG_BYTES)
            manifest = etree.fromstring(zf.read("META-INF/manifest.xml"))
        paths = [e.get(_q("manifest", "full-path")) for e in manifest]
        self.assertIn("Pictures/inc_ident_logo.png", paths)

    def test_with_base_template(self):
        """Run the include and the letterhead merge together.

        Pure Python, trigger P4 and P8 (L-05, L-07, L-08, L-19).
        """
        self._create_fragment()
        base = self._create_template("Base", base_odt())
        engine = self._engine(
            body_odt(body_inner=MARKER), py3o_base_template_id=base.id
        )
        result = engine.get_template(self.env["res.partner"])
        self.assertIn("IDENTITY BLOCK", self._texts(self._content(result)))
        header_node = next(self._styles(result).iter(_q("style", "header")))
        header = "".join(header_node.itertext())
        self.assertIn("ACME LETTERHEAD", header)

    def test_unknown_code(self):
        """Fail closed on an unknown code naming report and code.

        Pure Python, trigger P4 and P8 (L-05, L-07, L-08, L-19).
        """
        with self.assertRaises(UserError) as caught:
            self._apply(body_odt(body_inner=MARKER))
        message = str(caught.exception)
        self.assertIn("Worksheet Report", message)
        self.assertIn("ident", message)

    def test_fragment_not_odt(self):
        """Fail closed when the sub-template is an ODS file.

        Pure Python, trigger P4 and P8 (L-05, L-07, L-08, L-19).
        """
        self._create_template("Sheet", report_odt(), filetype="ods", code="ident")
        with self.assertRaises(UserError) as caught:
            self._apply(body_odt(body_inner=MARKER))
        self.assertIn("Worksheet Report", str(caught.exception))
        self.assertIn("ident", str(caught.exception))

    def test_nested_marker(self):
        """Fail closed on a marker inside a sub-template.

        Pure Python, trigger P4 and P8 (L-05, L-07, L-08, L-19).
        """
        self._create_fragment(body_inner=MARKER)
        with self.assertRaises(UserError) as caught:
            self._apply(body_odt(body_inner=MARKER))
        self.assertIn("Worksheet Report", str(caught.exception))
        self.assertIn("ident", str(caught.exception))

    def test_ods_report_untouched(self):
        """Leave an ODS report that contains marker text alone.

        Pure Python, trigger P4 and P8 (L-05, L-07, L-08, L-19).
        """
        self._create_fragment()
        data = body_odt(body_inner=MARKER)
        out = BytesIO()
        with ZipFile(data and BytesIO(data)) as zin, ZipFile(out, "w") as zout:
            for info in zin.infolist():
                if info.filename == "mimetype":
                    zout.writestr(
                        "mimetype",
                        "application/vnd.oasis.opendocument.spreadsheet",
                        compress_type=ZIP_STORED,
                    )
                else:
                    zout.writestr(info, zin.read(info.filename))
        ods = out.getvalue()
        engine = self._engine(body_odt())
        self.assertEqual(engine._py3o_apply_includes(ods), ods)
