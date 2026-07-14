# Copyright 2026 OpenSynergy Indonesia
# Copyright 2026 PT. Simetri Sinergi Indonesia
# License AGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
"""Deviation from the odoo-yaml-test standard (documented, see the skill
odoo-development-unit-test): ``_py3o_get_base_template()`` returns a
``py3o.template`` recordset that is not stored on any field. In
odoo-yaml-test 0.4.0, ``action: call`` discards the method's return value
and only runs ``asserts`` against the *target* record's own fields (see
``YamlTransactionCase._action_call`` in the library source) — there is no
way to assert an arbitrary method's return value from YAML. These 5
resolution scenarios are therefore written as a plain TransactionCase.
The ``py3o_is_odt_template`` compute field itself (a real, readable field)
is still tested in YAML — see test_base_template.yaml.
"""
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestBaseTemplateResolution(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Template = self.env["py3o.template"]
        self.Report = self.env["ir.actions.report"]

    def _create_odt_template(self, name):
        return self.Template.create({"name": name, "filetype": "odt"})

    def _create_report(self, **values):
        vals = {
            "name": "Test Report",
            "model": "res.partner",
            "report_name": "ssi_py3o.test_report_resolution",
            "report_type": "py3o",
            "py3o_filetype": "pdf",
            "py3o_template_fallback": "reports/x.odt",
        }
        vals.update(values)
        return self.Report.create(vals)

    def test_report_base_template_wins_over_company(self):
        template_a = self._create_odt_template("Letterhead A")
        template_b = self._create_odt_template("Letterhead B")
        self.env.company.py3o_base_template_id = template_b
        report = self._create_report(py3o_base_template_id=template_a.id)
        self.assertEqual(report._py3o_get_base_template(), template_a)

    def test_fallback_to_company_base_template(self):
        template_b = self._create_odt_template("Letterhead B")
        self.env.company.py3o_base_template_id = template_b
        report = self._create_report()
        self.assertEqual(report._py3o_get_base_template(), template_b)

    def test_no_base_template_anywhere(self):
        self.env.company.py3o_base_template_id = False
        report = self._create_report()
        self.assertFalse(report._py3o_get_base_template())

    def test_opt_out_ignores_company_base_template(self):
        template_b = self._create_odt_template("Letterhead B")
        self.env.company.py3o_base_template_id = template_b
        report = self._create_report(py3o_no_base_template=True)
        self.assertFalse(report._py3o_get_base_template())

    def test_non_odt_template_ignores_company_base_template(self):
        template_b = self._create_odt_template("Letterhead B")
        self.env.company.py3o_base_template_id = template_b
        report = self._create_report(py3o_template_fallback="reports/x.ods")
        self.assertFalse(report.py3o_is_odt_template)
        self.assertFalse(report._py3o_get_base_template())
