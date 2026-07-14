# Copyright 2025 OpenSynergy Indonesia
# Copyright 2025 PT. Simetri Sinergi Indonesia
# License AGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).

import os
from base64 import b64decode

from odoo import _, api, fields, models


class IrActionsReport(models.Model):
    _inherit = "ir.actions.report"

    DEFAULT_PYTHON_CODE = """class Parser:
    def __init__(self, env, document):
        self.env = env
        self.document = document"""

    py3o_is_odt_template = fields.Boolean(
        string="Py3o Template Is ODT",
        compute="_compute_py3o_is_odt_template",
        store=False,
        help="Technical flag telling whether the py3o TEMPLATE (not the output "
        "format) used by this report is an ODT (zipped ODF text) package. Base "
        "template merge only applies when this is True: 'fodt' (flat XML) is not "
        "a zip package, so it is never considered ODT here.",
    )
    py3o_base_template_id = fields.Many2one(
        string="Py3o Base Template",
        comodel_name="py3o.template",
        help="ODT template whose header/footer is merged into this report when "
        "rendered. Leave empty to fall back to the company's 'Py3o Base Template "
        "(Letterhead)'.",
    )
    py3o_no_base_template = fields.Boolean(
        string="No Base Template",
        default=False,
        help="If checked, this report is never merged with a base template "
        "(letterhead), even if one is set on the report or on the company.",
    )

    parser_state = fields.Selection(
        string="State of Parser",
        selection=[
            ("default", _("Default")),
            ("code", _("Parser Code (Python)")),
            ("loc", _("Location")),
        ],
        default="default",
        help="Select the parser configuration method:\n"
        "- Default: Use the standard parser\n"
        "- Parser Code (Python): Define custom parser using Python code\n"
        "- Location: Specify the path to an external parser file",
    )

    parser_loc = fields.Char(
        string="Parser location",
        help="Path to the parser location. Beginning of the path must be start \
              with the module name!\n Like this: {module name}/{path to the \
              parser.py file}",
    )

    parser_code = fields.Text(
        string="Parser Code (Python)",
        default=DEFAULT_PYTHON_CODE,
        help="Python code defined as parser. "
        "Must define a 'Parser' class with __init__(self, env, docs)",
    )

    @api.depends(
        "report_type",
        "py3o_template_id.filetype",
        "py3o_template_fallback",
    )
    def _compute_py3o_is_odt_template(self):
        for report in self:
            report.py3o_is_odt_template = report._py3o_check_is_odt_template()

    def _py3o_check_is_odt_template(self):
        self.ensure_one()
        if self.py3o_template_id:
            return self.py3o_template_id.filetype == "odt"
        fallback = self.py3o_template_fallback
        if not fallback:
            return False
        _root, ext = os.path.splitext(fallback)
        return ext.lower() == ".odt"

    def _py3o_get_base_template(self):
        """Resolve the ODT base template (letterhead) to merge into this report.

        Returns an empty ``py3o.template`` recordset when merging must not
        happen: wrong report type, non-ODT template, or explicit opt-out.
        """
        self.ensure_one()
        template_obj = self.env["py3o.template"]
        if self.report_type != "py3o":
            return template_obj
        if not self.py3o_is_odt_template:
            return template_obj
        if self.py3o_no_base_template:
            return template_obj
        return self.py3o_base_template_id or self.env.company.py3o_base_template_id

    def _py3o_get_base_template_data(self):
        """Return the raw ODT bytes of the resolved base template, or None."""
        self.ensure_one()
        base_template = self._py3o_get_base_template()
        if not base_template or base_template.filetype != "odt":
            return None
        if not base_template.py3o_template_data:
            return None
        return b64decode(base_template.py3o_template_data)
