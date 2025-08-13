# Copyright 2025 OpenSynergy Indonesia
# Copyright 2025 PT. Simetri Sinergi Indonesia
# License AGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
import importlib.util
import logging
import os
import sys

from odoo import _, api, models
from odoo.exceptions import UserError
from odoo.tools.config import config

logger = logging.getLogger(__name__)

# CONTOH
# @py3o_report_extender()
# def get_config_paramater(report_xml, context):
#     raise UserError(_("%s")%(context))
#     obj_config_param = self.env["ir.config_parameter"]
#     context["_get_config_param"] = obj_config_param.get_param(key, default=False)


class Py3oReport(models.TransientModel):
    _inherit = "py3o.report"

    @api.model
    def load_from_file(self, path, key):
        """Load Parser class from a Python file in addons path"""
        if not path:
            return None

        try:
            # Get addons paths
            addons_paths = self._get_addons_paths()

            # Find the file in addons paths
            filepath = self._find_parser_file(path, addons_paths)
            if not filepath:
                logger.warning("Parser file not found: %s", path)
                return None

            # Load the module and get Parser class
            return self._load_parser_class(filepath, key)

        except SyntaxError as e:
            raise UserError(_("Syntax Error in parser file: %s") % str(e))
        except Exception as e:
            logger.error("Error loading parser from %s: %s", path, str(e))
            return None

    @api.model
    def _get_addons_paths(self):
        """Get list of addons paths"""
        paths = []

        # Add configured addons paths
        if config.get("addons_path"):
            paths.extend(
                [os.path.abspath(p.strip()) for p in config["addons_path"].split(",")]
            )

        # Add default addons path
        root_path = config.get("root_path", "")
        if root_path:
            default_addons = os.path.join(root_path, "addons")
            paths.append(os.path.abspath(default_addons))

        # Remove duplicates while preserving order
        return list(dict.fromkeys(paths))

    @api.model
    def _find_parser_file(self, path, addons_paths):
        """Find parser file in addons paths"""
        for addons_path in addons_paths:
            # Check if module directory exists
            module_name = path.split(os.path.sep)[0]
            module_path = os.path.join(addons_path, module_name)

            if os.path.isdir(module_path):
                filepath = os.path.join(addons_path, path)
                if os.path.isfile(filepath) and filepath.endswith(".py"):
                    return filepath
        return None

    @api.model
    def _load_parser_class(self, filepath, key):
        """Load Parser class from Python file"""
        try:
            # Create unique module name
            mod_name = f"{self.env.cr.dbname}_{os.path.basename(filepath)[:-3]}_{key}"

            # Add the module directory to Python path for relative imports
            module_dir = os.path.dirname(filepath)
            if module_dir not in sys.path:
                sys.path.insert(0, module_dir)

            # Load module using importlib
            spec = importlib.util.spec_from_file_location(mod_name, filepath)
            if not spec or not spec.loader:
                return None

            module = importlib.util.module_from_spec(spec)

            # Set module in sys.modules to enable proper import handling
            sys.modules[mod_name] = module

            try:
                spec.loader.exec_module(module)
            finally:
                # Clean up sys.modules to avoid conflicts
                if mod_name in sys.modules:
                    del sys.modules[mod_name]
                # Remove from path if we added it
                if module_dir in sys.path:
                    sys.path.remove(module_dir)

            # Get Parser class
            parser_class = getattr(module, "Parser", None)
            return parser_class

        except Exception as e:
            logger.error("Failed to load parser class from %s: %s", filepath, str(e))
            return None

    @api.model
    def _get_config_param(self, key):
        obj_config_param = self.env["ir.config_parameter"].sudo()
        return obj_config_param.get_param(key, "")

    @api.model
    def _exec_parser_code(self, code_str, env, data):
        """Execute parser code with proper import support"""

        global_namespace = {
            "__builtins__": __builtins__,
            "datetime": __import__("datetime"),
            "json": __import__("json"),
            "base64": __import__("base64"),
            "math": __import__("math"),
            "time": __import__("time"),
            "re": __import__("re"),
            "logging": logging,
        }

        local_namespace = {}
        try:
            exec(code_str, global_namespace, local_namespace)
            ParserClass = local_namespace.get("Parser")
            if not ParserClass:
                raise UserError(_("Parser class 'Parser' not found in parser code."))
            return ParserClass(env, data)
        except Exception as e:
            raise UserError(_("Parser execution error:\n%s") % str(e))

    @api.model
    def _get_parser_context(self, model_instance, data):
        _super = super(Py3oReport, self)
        res = _super._get_parser_context(model_instance, data)
        res["parameter_value"] = self._get_config_param
        report = self.ir_actions_report_id
        if report.parser_state == "code":
            parser = None
            if report.parser_code:
                parser = self._exec_parser_code(
                    report.parser_code, self.env, model_instance
                )
                res["parser"] = parser

        if report.parser_state == "loc" and report.parser_loc:
            parser = None
            ParserClass = self.load_from_file(report.parser_loc, report.id)
            if ParserClass:
                parser = ParserClass(self.env, model_instance)
            res["parser"] = parser
        return res
