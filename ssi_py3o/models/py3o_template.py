# Copyright 2026 OpenSynergy Indonesia
# Copyright 2026 PT. Simetri Sinergi Indonesia
# License AGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).

import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

INCLUDE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class Py3oTemplate(models.Model):
    _inherit = "py3o.template"

    include_code = fields.Char(
        string="Include Code",
        copy=False,
        help="Identifier that makes this ODT template insertable into the body "
        "of other py3o ODT reports. A report pulls it in with a paragraph "
        "that contains only the py3o function field subtemplate('<code>'). "
        "Must be unique and "
        "match [a-z][a-z0-9_]*. Leave empty if the template is not meant to "
        "be inserted.",
    )

    def _find_include_code_duplicate(self):
        """Find another template that already uses this include code.

        :return: the other ``py3o.template`` record, or an empty recordset
            when the code is empty or not used by any other template
        """
        self.ensure_one()
        if not self.include_code:
            return self.browse()
        domain = [("include_code", "=", self.include_code)]
        origin_id = self._origin.id or self.id
        if origin_id:
            domain.append(("id", "!=", origin_id))
        return self.search(domain, limit=1)

    @api.onchange("include_code")
    def onchange_include_code(self):
        """Warn while typing when the include code is already taken.

        The save is still refused by ``_check_include_code``; this only
        gives the user the same answer before saving.

        :return: a ``warning`` dict naming the template that owns the code,
            or ``None`` when the code is free
        """
        duplicate = self._find_include_code_duplicate()
        if not duplicate:
            return None
        message = (
            _(
                """
Problem: Include code '%s' is already used by template '%s'
Solution: Choose an include code that is not used yet
"""
            )
            % (self.include_code, duplicate.display_name)
        )
        return {
            "warning": {
                "title": _("Include code already used"),
                "message": message,
            }
        }

    @api.constrains("include_code")
    def _check_include_code(self):
        """Validate format and uniqueness of ``include_code``.

        :raises ValidationError: if the code does not match
            ``[a-z][a-z0-9_]*`` or is already used by another template.
        """
        for template in self:
            code = template.include_code
            if not code:
                continue
            if not INCLUDE_CODE_RE.match(code):
                error_message = (
                    _(
                        """
Context: Set include code of py3o template
Database ID: %s
Problem: Include code '%s' has an invalid format
Solution: Use only lowercase letters, digits and underscore, starting with \
a letter
"""
                    )
                    % (template.id, code)
                )
                raise ValidationError(error_message)
            if template._find_include_code_duplicate():
                error_message = (
                    _(
                        """
Context: Set include code of py3o template
Database ID: %s
Problem: Include code '%s' is already used by another template
Solution: Choose an include code that is not used yet
"""
                    )
                    % (template.id, code)
                )
                raise ValidationError(error_message)
