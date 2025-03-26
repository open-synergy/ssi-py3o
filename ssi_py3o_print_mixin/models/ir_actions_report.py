# Copyright 2025 OpenSynergy Indonesia
# Copyright 2025 PT. Simetri Sinergi Indonesia
# License AGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).

from odoo import api, models


class IrActionsReport(models.Model):
    _inherit = "ir.actions.report"

    @api.onchange(
        "print_multi",
    )
    def onchange_py3o_multi_in_one(self):
        self.py3o_multi_in_one = self.print_multi

    @api.onchange(
        "py3o_multi_in_one",
    )
    def onchange_print_multi(self):
        self.print_multi = self.py3o_multi_in_one
