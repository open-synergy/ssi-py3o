# Copyright 2025 OpenSynergy Indonesia
# Copyright 2025 PT. Simetri Sinergi Indonesia
# License AGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
from odoo import models

# CONTOH MENGGUNAKAN DECORATOR
# @py3o_report_extender()
# def get_config_paramater(report_xml, context):
#     raise UserError(_("%s")%(context))
#     obj_config_param = self.env["ir.config_parameter"]
#     context["_get_config_param"] = obj_config_param.get_param(key, default=False)


class Py3oReport(models.TransientModel):
    _inherit = "py3o.report"

    def _get_parser_context(self, model_instance, data):
        _super = super(Py3oReport, self)
        res = _super._get_parser_context(model_instance, data)
        res["parameter_value"] = self._get_config_param
        return res

    def _get_config_param(self, key):
        obj_config_param = self.env["ir.config_parameter"].sudo()
        return obj_config_param.get_param(key, "")
