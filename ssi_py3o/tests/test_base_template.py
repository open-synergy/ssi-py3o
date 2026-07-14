# Copyright 2026 OpenSynergy Indonesia
# Copyright 2026 PT. Simetri Sinergi Indonesia
# License AGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).

from odoo.tests import tagged

from odoo_yaml_test import YamlTransactionCase


@tagged("post_install", "-at_install")
class TestBaseTemplate(YamlTransactionCase):
    def test_base_template(self):
        self.run_yaml_scenario("test_base_template.yaml")
