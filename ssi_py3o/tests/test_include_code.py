# Copyright 2026 OpenSynergy Indonesia
# Copyright 2026 PT. Simetri Sinergi Indonesia
# License AGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).

from odoo_yaml_test import YamlTransactionCase

from odoo.tests import tagged


@tagged("post_install", "-at_install")
class TestIncludeCode(YamlTransactionCase):
    """Cover the ``include_code`` field of ``py3o.template``."""

    def test_include_code(self):
        """Run the include code validation scenarios."""
        self.run_yaml_scenario("test_include_code.yaml")
