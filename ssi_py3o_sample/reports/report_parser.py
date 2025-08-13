# Copyright 2025 OpenSynergy Indonesia
# Copyright 2025 PT. Simetri Sinergi Indonesia
# License AGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).


class Parser:
    def __init__(self, env, document):
        self.env = env
        self.document = document

    def hello_world(self):
        return "Hello, %s!" % (self.document.name)
