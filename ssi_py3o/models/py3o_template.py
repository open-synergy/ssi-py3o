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
        "that contains only '[[py3o_include:<code>]]'. Must be unique and "
        "match [a-z][a-z0-9_]*. Leave empty if the template is not meant to "
        "be inserted.",
    )

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
            duplicate = self.search_count(
                [("include_code", "=", code), ("id", "!=", template.id)]
            )
            if duplicate:
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
