# Copyright 2025 OpenSynergy Indonesia
# Copyright 2025 PT. Simetri Sinergi Indonesia
# License AGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).

from odoo import _, fields, models


class IrActionsReport(models.Model):
    _inherit = "ir.actions.report"

    DEFAULT_PYTHON_CODE = """class Parser:
    def __init__(self, env, document):
        self.env = env
        self.document = document"""

    parser_state = fields.Selection(
        string="State of Parser",
        selection=[
            ("default", _("Default")),
            ("code", _("Parser Code (Python)")),
            ("loc", _("Location")),
        ],
        default="default",
    )

    parser_loc = fields.Char(
        string="Parser location",
        size=128,
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
