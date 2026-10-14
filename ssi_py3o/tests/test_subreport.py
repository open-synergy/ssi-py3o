# Copyright 2026 OpenSynergy Indonesia
# Copyright 2026 PT. Simetri Sinergi Indonesia
# License AGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
"""Deviation from the odoo-yaml-test standard (documented, see the skill
odoo-development-unit-test): the sub-report engine renders real py3o ODT
files and asserts on the XML of the resulting package, which YAML cannot
express (P4, L-05, L-07, L-08, L-19). Acting as another user (``with_user``)
is also Python only (P9).

CI runs on Python 3.6 -- no walrus operator.
"""
import base64
import os
import tempfile
from io import BytesIO
from zipfile import ZIP_STORED, ZipFile

from lxml import etree
from odoo_yaml_test import YamlTransactionCase

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import base_odt, body_odt

NS_TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
NS_TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
NS_STYLE = "urn:oasis:names:tc:opendocument:xmlns:style:1.0"
NS_FO = "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"
SUB_NAME = "ssi_py3o.test_subreport_b"


def _field(expression, label="X"):
    """Return a py3o field that evaluates ``expression``."""
    return (
        '<text:text-input text:description="py3o://function=&quot;%s&quot;">'
        "%s</text:text-input>" % (expression.replace('"', "&quot;"), label)
    )


def _directive(directive, label="X"):
    """Return a py3o loop directive field."""
    return '<text:text-input text:description="%s">%s</text:text-input>' % (
        directive.replace('"', "&quot;"),
        label,
    )


def _para(*parts):
    """Return a paragraph holding ``parts``."""
    return '<text:p text:style-name="P1">%s</text:p>' % "".join(parts)


def _table(text):
    """Return a one-cell table named ``Table1``."""
    return (
        '<table:table table:name="Table1"><table:table-column/><table:table-row>'
        '<table:table-cell office:value-type="string"><text:p>%s</text:p>'
        "</table:table-cell></table:table-row></table:table>" % text
    )


def _style(name, prop):
    """Return a paragraph style ``name`` with the text property ``prop``."""
    return (
        '<style:style style:name="%s" style:family="paragraph" '
        'style:parent-style-name="Standard"><style:text-properties %s/>'
        "</style:style>" % (name, prop)
    )


SUB_BODY = (
    _para("SUB HEADER")
    + _table("SUB TABLE")
    + _para(_directive('py3o://for="b in objects"', "FOR"))
    + _para("ACC ", _field("b.acc_number"))
    + _para(_directive("py3o:///for", "END"))
)
SUB_STYLES = _style("P1", 'fo:font-weight="bold"')
CALLER_STYLES = _style("P1", 'fo:font-style="italic"')


@tagged("post_install", "-at_install")
class TestSubreport(YamlTransactionCase):
    """Cover the ``subreport()`` function of py3o ODT reports."""

    def setUp(self):
        """Create a partner with two bank accounts and the sub-report."""
        super().setUp()
        self.partner = self.env["res.partner"].create({"name": "Sub Partner"})
        Bank = self.env["res.partner.bank"]
        self.acc1 = Bank.create({"acc_number": "ACC-1", "partner_id": self.partner.id})
        self.acc2 = Bank.create({"acc_number": "ACC-2", "partner_id": self.partner.id})
        self.sub_report = self._create_report(
            "Sub Report",
            SUB_NAME,
            "res.partner.bank",
            body_odt(body_inner=SUB_BODY, auto_styles_inner=SUB_STYLES),
        )

    def _create_report(self, name, report_name, model, data, **values):
        """Create a py3o ``ir.actions.report`` printing ``model`` with ``data``."""
        template = self.env["py3o.template"].create(
            {
                "name": name,
                "filetype": "odt",
                "py3o_template_data": base64.b64encode(data),
            }
        )
        vals = {
            "name": name,
            "model": model,
            "report_name": report_name,
            "report_type": "py3o",
            "py3o_filetype": "odt",
            "py3o_template_id": template.id,
        }
        vals.update(values)
        return self.env["ir.actions.report"].create(vals)

    def _render(self, body, styles="", user=None, **values):
        """Render a caller report on ``self.partner`` into an ODT package.

        :param body: body XML of the caller template
        :param styles: automatic styles of the caller template
        :param user: user that prints the report
        :return: ``(content root, styles root)`` of the rendered ODT
        """
        caller = self._create_report(
            "Caller",
            "ssi_py3o.test_subreport_a",
            "res.partner",
            body_odt(body_inner=body, auto_styles_inner=styles),
            **values
        )
        env = self.env if user is None else self.env(user=user.id)
        engine = (
            env["py3o.report"]
            .with_context(report_py3o_skip_conversion=True)
            .create({"ir_actions_report_id": caller.id})
        )
        path = engine._create_single_report(self.partner.with_env(env), {})
        try:
            with open(path, "rb") as odt_file:
                data = odt_file.read()
        finally:
            os.unlink(path)
        with ZipFile(BytesIO(data)) as zf:
            return (
                etree.fromstring(zf.read("content.xml")),
                etree.fromstring(zf.read("styles.xml")),
            )

    def _texts(self, root):
        """Return the full text of every paragraph below ``root``."""
        return ["".join(p.itertext()) for p in root.iter("{%s}p" % NS_TEXT)]

    def _call(self, expression="objects.bank_ids", name=SUB_NAME):
        """Return a caller paragraph that calls ``subreport()``."""
        return _para(_field("subreport('%s', %s)" % (name, expression)))

    def test_two_calls_two_blocks(self):
        """Insert two sub-reports with their own records in one report.

        Pure Python, trigger P4 and P9 (L-05, L-07, L-08, L-19).
        """
        body = _para("CALLER") + self._call() + self._call("objects.bank_ids[:1]")
        content, _styles = self._render(body)
        texts = self._texts(content)
        self.assertEqual(texts.count("SUB HEADER"), 2)
        self.assertEqual(texts.count("ACC ACC-1"), 2)
        self.assertEqual(texts.count("ACC ACC-2"), 1)
        self.assertEqual(texts[0], "CALLER")
        self.assertFalse([t for t in texts if "py3o_subreport" in t])

    def test_style_and_names_isolated(self):
        """Keep caller styles and make table names unique.

        Pure Python, trigger P4 (L-05, L-07, L-08, L-19).
        """
        body = _para("CALLER") + _table("CALLER TABLE") + self._call()
        content, _styles = self._render(body, styles=CALLER_STYLES)
        names = [
            t.get("{%s}name" % NS_TABLE) for t in content.iter("{%s}table" % NS_TABLE)
        ]
        self.assertEqual(names, ["Table1", "inc_sub0_Table1"])
        styles = {
            s.get("{%s}name" % NS_STYLE): s
            for s in content.iter("{%s}style" % NS_STYLE)
        }
        caller_prop = styles["P1"].find("{%s}text-properties" % NS_STYLE)
        self.assertEqual(caller_prop.get("{%s}font-style" % NS_FO), "italic")
        sub_prop = styles["inc_sub0_P1"].find("{%s}text-properties" % NS_STYLE)
        self.assertEqual(sub_prop.get("{%s}font-weight" % NS_FO), "bold")

    def test_empty_recordset(self):
        """Insert nothing when the recordset is empty.

        Pure Python, trigger P4 (L-05, L-07, L-08, L-19).
        """
        body = _para("CALLER") + self._call("objects.bank_ids[:0]")
        content, _styles = self._render(body)
        self.assertEqual(self._texts(content), ["CALLER"])

    def test_letterhead_not_carried(self):
        """Skip the letterhead of a report rendered as sub-report.

        Pure Python, trigger P4 (L-05, L-07, L-08, L-19).
        """
        base = self.env["py3o.template"].create(
            {
                "name": "Base",
                "filetype": "odt",
                "py3o_template_data": base64.b64encode(base_odt()),
            }
        )
        self.sub_report.py3o_base_template_id = base.id
        engine = self.env["py3o.report"].create(
            {"ir_actions_report_id": self.sub_report.id}
        )
        records = self.env["res.partner.bank"]
        plain = engine.with_context(py3o_in_subreport=True).get_template(records)
        merged = engine.get_template(records)
        with ZipFile(BytesIO(plain)) as zf:
            self.assertNotIn(b"ACME LETTERHEAD", zf.read("styles.xml"))
        with ZipFile(BytesIO(merged)) as zf:
            self.assertIn(b"ACME LETTERHEAD", zf.read("styles.xml"))

    def test_invalid_sub_report(self):
        """Fail closed on a sub-report that cannot be used.

        Pure Python, trigger P4 (L-05, L-07, L-08, L-19).
        """
        self._create_report(
            "Wrong Model",
            "ssi_py3o.test_subreport_model",
            "res.partner",
            body_odt(body_inner=_para("X")),
        )
        self._create_report(
            "Nested",
            "ssi_py3o.test_subreport_nested",
            "res.partner.bank",
            body_odt(body_inner=self._call("objects")),
        )
        self.env["ir.actions.report"].create(
            {
                "name": "Not Py3o",
                "model": "res.partner.bank",
                "report_name": "ssi_py3o.test_subreport_qweb",
                "report_type": "qweb-pdf",
            }
        )
        cases = (
            self._call(name="ssi_py3o.test_subreport_unknown"),
            self._call(name="ssi_py3o.test_subreport_qweb"),
            self._call(name="ssi_py3o.test_subreport_model"),
            self._call(name="ssi_py3o.test_subreport_nested"),
            _para("Before ", _field("subreport('%s', objects.bank_ids)" % SUB_NAME)),
        )
        for body in cases:
            with self.assertRaises(UserError) as caught:
                self._render(body)
            self.assertIn("Insert py3o sub-report", str(caught.exception))

    def test_sub_report_template_not_odt(self):
        """Fail closed when the template of the sub-report is not an ODT.

        Pure Python, trigger P4 (L-05, L-07, L-08, L-19).
        """
        self.sub_report.py3o_template_id.filetype = "ods"
        with self.assertRaises(UserError) as caught:
            self._render(self._call())
        self.assertIn(SUB_NAME, str(caught.exception))

    def test_caller_not_odt(self):
        """Fail closed when the rendered caller is an ODS file.

        Pure Python, trigger P4 (L-05, L-07, L-08, L-19).
        """
        out = BytesIO()
        with ZipFile(BytesIO(body_odt())) as zin, ZipFile(out, "w") as zout:
            for info in zin.infolist():
                if info.filename == "mimetype":
                    zout.writestr(
                        "mimetype",
                        "application/vnd.oasis.opendocument.spreadsheet",
                        compress_type=ZIP_STORED,
                    )
                else:
                    zout.writestr(info, zin.read(info.filename))
        caller = self._create_report(
            "Caller Sheet",
            "ssi_py3o.test_subreport_sheet",
            "res.partner",
            body_odt(),
        )
        engine = self.env["py3o.report"].create({"ir_actions_report_id": caller.id})
        path = os.path.join(tempfile.mkdtemp(), "caller.ods")
        with open(path, "wb") as ods_file:
            ods_file.write(out.getvalue())
        try:
            with self.assertRaises(UserError) as caught:
                engine._py3o_resolve_subreports(path, [(SUB_NAME, self.acc1)])
        finally:
            os.unlink(path)
        message = str(caught.exception)
        self.assertIn("Caller Sheet", message)
        self.assertIn(SUB_NAME, message)

    def test_access_denied(self):
        """Refuse a user outside the groups of the sub-report.

        Pure Python, trigger P9 (L-19).
        """
        self.sub_report.groups_id = [(6, 0, [self.env.ref("base.group_system").id])]
        user = self.env["res.users"].create(
            {
                "name": "Plain User",
                "login": "plain_subreport_user",
                "groups_id": [(6, 0, [self.env.ref("base.group_user").id])],
            }
        )
        with self.assertRaises(AccessError):
            self._render(self._call(), user=user)
        content, _styles = self._render(self._call())
        self.assertIn("SUB HEADER", self._texts(content))
