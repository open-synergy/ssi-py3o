# Copyright 2026 OpenSynergy Indonesia
# Copyright 2026 PT. Simetri Sinergi Indonesia
# License AGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).

from odoo.addons.report_py3o.models.py3o_report import py3o_report_extender


@py3o_report_extender()
def add_company_context(report_xml, context):
    """Guarantee 'company'/'company_partner' in every py3o report's parser
    context, so the shared header/footer (letterhead, see the 'Py3o Base
    Template' fields on ir.actions.report/res.company) can rely on them
    without depending on a report-specific field such as
    'objects[0].company_id'.
    """
    objects = context.get("objects")
    company = False
    if objects and "company_id" in objects._fields:
        company = objects[:1].company_id
    company = company or report_xml.env.company
    context["company"] = company
    context["company_partner"] = company.partner_id
