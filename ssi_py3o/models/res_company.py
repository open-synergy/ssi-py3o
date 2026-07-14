# Copyright 2026 OpenSynergy Indonesia
# Copyright 2026 PT. Simetri Sinergi Indonesia
# License AGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).

from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    py3o_base_template_id = fields.Many2one(
        string="Py3o Base Template (Letterhead)",
        comodel_name="py3o.template",
        help="Default ODT template whose header/footer is merged into every py3o "
        "report using an ODT template, unless the report defines its own "
        "'Py3o Base Template' or opts out with 'No Base Template'.",
    )
