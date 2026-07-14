# Copyright 2025 OpenSynergy Indonesia
# Copyright 2025 PT. Simetri Sinergi Indonesia
# License AGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
import importlib.util
import logging
import mimetypes
import os
import sys
from copy import deepcopy
from inspect import isfunction
from io import BytesIO
from zipfile import ZIP_STORED, BadZipFile, ZipFile

import babel.dates
from lxml import etree

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

_ODF_NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
    "xlink": "http://www.w3.org/1999/xlink",
    "loext": "urn:org:documentfoundation:names:experimental:office:xmlns:loext:1.0",
    "manifest": "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0",
}


def _clark(prefix, local):
    return "{%s}%s" % (_ODF_NS[prefix], local)


ODT_MIMETYPE = "application/vnd.oasis.opendocument.text"

# ODF 1.2 §16.9 order of <style:master-page> header/footer children.
_MP_TAG_SLOT = {
    _clark("office", "forms"): 0,
    _clark("style", "header"): 1,
    _clark("style", "header-left"): 2,
    _clark("style", "header-first"): 3,
    _clark("loext", "header-first"): 3,
    _clark("style", "footer"): 4,
    _clark("style", "footer-left"): 5,
    _clark("style", "footer-first"): 6,
    _clark("loext", "footer-first"): 6,
}

# Attributes that reference another style by name. Scanned both to discover the
# transitive closure of styles to import (subset below) and to rewrite once the
# sbt_ rename map is known (this full list).
_STYLE_REF_ATTRS = [
    _clark("text", "style-name"),
    _clark("table", "style-name"),
    _clark("draw", "style-name"),
    _clark("style", "parent-style-name"),
    _clark("style", "data-style-name"),
    _clark("style", "list-style-name"),
    _clark("style", "next-style-name"),
]

# style:next-style-name is rewritten (above) but intentionally NOT traversed to
# discover further styles to import — it is purely editorial (which style the
# *next* paragraph should use), not a rendering dependency of this one.
_STYLE_REF_ATTRS_TRAVERSE = [
    attr for attr in _STYLE_REF_ATTRS if attr != _clark("style", "next-style-name")
]


class Py3oReport(models.TransientModel):
    _inherit = "py3o.report"

    # -- Py3o base template (letterhead) merge --------------------------------
    # Merges the master-page header/footer of a "base template" ODT
    # (letterhead) into the ODT template of a py3o report, at render time,
    # without ever touching the report's own .odt file on disk. See
    # README.rst for the rules a letterhead author must follow. Only the
    # header/footer are merged: paper size, orientation, and left/right
    # margins always stay the report's own (see _py3o_merge_page_layout).

    def get_template(self, model_instance):
        report_bytes = super().get_template(model_instance)
        report = self.ir_actions_report_id
        base_bytes = report._py3o_get_base_template_data()
        if not base_bytes:
            return report_bytes
        strict = self.env.context.get("py3o_base_template_strict")
        try:
            base_mimetype = self._py3o_sniff_mimetype(base_bytes)
            if base_mimetype is None:
                # Not a legitimate skip (e.g. ODS base): the base template is
                # unreadable/corrupt. Worth logging, unlike the plain
                # format-mismatch case below.
                raise ValueError("Py3o base template is not a valid ODF package.")
            if base_mimetype != ODT_MIMETYPE:
                return report_bytes
            if self._py3o_sniff_mimetype(report_bytes) != ODT_MIMETYPE:
                return report_bytes
            return self._py3o_merge_base_template(report_bytes, base_bytes)
        except Exception:
            if strict:
                raise
            logger.exception(
                "Failed to merge py3o base template (letterhead) into report "
                "'%s'. Printing without the base template.",
                report.display_name,
            )
            return report_bytes

    def _py3o_sniff_mimetype(self, zip_bytes):
        try:
            with ZipFile(BytesIO(zip_bytes)) as zf:
                return zf.read("mimetype").decode("utf-8", errors="ignore").strip()
        except (BadZipFile, KeyError):
            return None

    def _py3o_merge_base_template(self, report_bytes, base_bytes, prefix="sbt_"):
        report_zip_in = ZipFile(BytesIO(report_bytes))
        base_zip_in = ZipFile(BytesIO(base_bytes))

        report_styles_root = etree.fromstring(report_zip_in.read("styles.xml"))
        base_styles_root = etree.fromstring(base_zip_in.read("styles.xml"))

        report_font_decls = report_styles_root.find(_clark("office", "font-face-decls"))
        base_font_decls = base_styles_root.find(_clark("office", "font-face-decls"))
        report_common_styles = report_styles_root.find(_clark("office", "styles"))
        base_common_styles = base_styles_root.find(_clark("office", "styles"))
        report_auto_styles = report_styles_root.find(
            _clark("office", "automatic-styles")
        )
        base_auto_styles = base_styles_root.find(_clark("office", "automatic-styles"))
        report_master_styles = report_styles_root.find(
            _clark("office", "master-styles")
        )
        base_master_styles = base_styles_root.find(_clark("office", "master-styles"))

        if report_master_styles is None or base_master_styles is None:
            return report_bytes

        base_master_pages = base_master_styles.findall(_clark("style", "master-page"))
        if not base_master_pages:
            return report_bytes
        base_mp_by_name = {
            mp.get(_clark("style", "name")): mp for mp in base_master_pages
        }
        base_standard_mp = base_mp_by_name.get("Standard")

        seed_elements = []
        for report_mp in report_master_styles.findall(_clark("style", "master-page")):
            mp_name = report_mp.get(_clark("style", "name"))
            base_mp = base_mp_by_name.get(mp_name)
            if base_mp is None:
                base_mp = base_standard_mp
            if base_mp is None:
                base_mp = base_master_pages[0]

            seed_elements.extend(self._py3o_merge_master_page(report_mp, base_mp))

            pl_name = report_mp.get(_clark("style", "page-layout-name"))
            base_pl_name = base_mp.get(_clark("style", "page-layout-name"))
            report_pl = self._find_style_by_name(report_auto_styles, pl_name)
            base_pl = self._find_style_by_name(base_auto_styles, base_pl_name)
            if report_pl is not None and base_pl is not None:
                seed_elements.extend(self._py3o_merge_page_layout(report_pl, base_pl))

        if not seed_elements:
            return report_bytes

        import_map = self._py3o_build_style_import_map(
            seed_elements, base_common_styles, base_auto_styles
        )
        self._py3o_apply_style_imports(
            import_map, report_common_styles, report_auto_styles, seed_elements, prefix
        )
        self._py3o_merge_font_faces(report_font_decls, base_font_decls)

        manifest_root = etree.fromstring(report_zip_in.read("META-INF/manifest.xml"))
        new_zip_entries = {}
        self._py3o_merge_images(
            seed_elements, report_zip_in, base_zip_in, new_zip_entries, manifest_root
        )

        new_zip_entries["styles.xml"] = etree.tostring(
            report_styles_root, xml_declaration=True, encoding="UTF-8"
        )
        new_zip_entries["META-INF/manifest.xml"] = etree.tostring(
            manifest_root, xml_declaration=True, encoding="UTF-8"
        )

        return self._py3o_write_merged_zip(report_zip_in, new_zip_entries)

    def _py3o_merge_master_page(self, report_mp, base_mp):
        """Replace report_mp's header/footer children with deep copies of
        base_mp's, in ODF 1.2 §16.9 order. Returns the newly-inserted nodes.
        """
        for child in list(report_mp):
            if child.tag in _MP_TAG_SLOT:
                report_mp.remove(child)
        inserted = [deepcopy(c) for c in base_mp if c.tag in _MP_TAG_SLOT]
        if not inserted:
            return []
        remaining = list(report_mp)
        ordered = sorted(
            remaining + inserted, key=lambda c: _MP_TAG_SLOT.get(c.tag, 99)
        )
        for c in list(report_mp):
            report_mp.remove(c)
        for c in ordered:
            report_mp.append(c)
        return inserted

    def _py3o_merge_page_layout(self, report_pl, base_pl):
        """Replace report_pl's header-style/footer-style with base_pl's, copy
        only the top/bottom margins into report_pl's page-layout-properties,
        and reorder children per ODF §16.5 (properties, header-style,
        footer-style). Page geometry (width/height/orientation/left/right
        margin) is intentionally left untouched. Returns the new header-style/
        footer-style nodes.
        """
        header_style_tag = _clark("style", "header-style")
        footer_style_tag = _clark("style", "footer-style")
        props_tag = _clark("style", "page-layout-properties")

        inserted = []
        for tag in (header_style_tag, footer_style_tag):
            old = report_pl.find(tag)
            if old is not None:
                report_pl.remove(old)
            base_node = base_pl.find(tag)
            if base_node is not None:
                new_node = deepcopy(base_node)
                report_pl.append(new_node)
                inserted.append(new_node)

        report_props = report_pl.find(props_tag)
        base_props = base_pl.find(props_tag)
        if report_props is not None and base_props is not None:
            for attr in (_clark("fo", "margin-top"), _clark("fo", "margin-bottom")):
                value = base_props.get(attr)
                if value is not None:
                    report_props.set(attr, value)

        order = {props_tag: 0, header_style_tag: 1, footer_style_tag: 2}
        children = sorted(list(report_pl), key=lambda c: order.get(c.tag, 99))
        for c in list(report_pl):
            report_pl.remove(c)
        for c in children:
            report_pl.append(c)
        return inserted

    def _find_style_by_name(self, container, name):
        """Find a direct child of `container` (an office:styles or
        office:automatic-styles element) whose style:name attribute is `name`.
        Works for style:style, style:page-layout, text:list-style, and the
        number:*-style data-style elements alike — they all key on style:name.
        """
        if container is None or not name:
            return None
        name_attr = _clark("style", "name")
        for child in container:
            if child.get(name_attr) == name:
                return child
        return None

    def _py3o_collect_style_refs(self, elements, attrs):
        names = set()
        for el in elements:
            for node in el.iter():
                for attr in attrs:
                    value = node.get(attr)
                    if value:
                        names.add(value)
        return names

    def _py3o_build_style_import_map(
        self, seed_elements, base_common_styles, base_auto_styles
    ):
        """Transitive closure of style names referenced (directly or through a
        referenced style's own references) by `seed_elements`, resolved
        against the BASE document. Names with no definition in base (dangling
        refs, or names only meaningful in the report itself) are skipped —
        left as-is by the caller.
        """
        to_visit = list(
            self._py3o_collect_style_refs(seed_elements, _STYLE_REF_ATTRS_TRAVERSE)
        )
        imported = {}
        while to_visit:
            name = to_visit.pop()
            if name in imported:
                continue
            definition = self._find_style_by_name(base_common_styles, name)
            container_kind = "styles"
            if definition is None:
                definition = self._find_style_by_name(base_auto_styles, name)
                container_kind = "automatic-styles"
            if definition is None:
                continue
            imported[name] = {"definition": definition, "container": container_kind}
            for nested_name in self._py3o_collect_style_refs(
                [definition], _STYLE_REF_ATTRS_TRAVERSE
            ):
                if nested_name not in imported:
                    to_visit.append(nested_name)
        return imported

    def _py3o_apply_style_imports(
        self,
        import_map,
        report_common_styles,
        report_auto_styles,
        seed_elements,
        prefix,
    ):
        if not import_map:
            return
        rename_map = {name: prefix + name for name in import_map}
        style_name_attr = _clark("style", "name")
        display_name_attr = _clark("style", "display-name")
        master_page_name_attr = _clark("style", "master-page-name")

        new_defs = {}
        for name, info in import_map.items():
            new_def = deepcopy(info["definition"])
            new_def.set(style_name_attr, rename_map[name])
            # A copied definition must not carry style:display-name (cosmetic,
            # would keep the base's label) nor style:master-page-name (would
            # inject an unwanted page break into the destination document).
            if display_name_attr in new_def.attrib:
                del new_def.attrib[display_name_attr]
            if master_page_name_attr in new_def.attrib:
                del new_def.attrib[master_page_name_attr]
            new_defs[name] = (new_def, info["container"])

        # Rewrite refs both in the copied header/footer subtree AND inside the
        # imported definitions themselves (they may reference each other, e.g.
        # style:parent-style-name). Unknown names are left untouched.
        nodes_to_rewrite = list(seed_elements) + [d for d, _c in new_defs.values()]
        for el in nodes_to_rewrite:
            for node in el.iter():
                for attr in _STYLE_REF_ATTRS:
                    value = node.get(attr)
                    if value in rename_map:
                        node.set(attr, rename_map[value])

        # Insert into the matching report container. Re-running the merge
        # (idempotency) replaces any previously-imported sbt_ definition
        # rather than accumulating duplicates.
        for name, (new_def, container_kind) in new_defs.items():
            target = (
                report_common_styles
                if container_kind == "styles"
                else report_auto_styles
            )
            if target is None:
                continue
            existing = self._find_style_by_name(target, rename_map[name])
            if existing is not None:
                target.remove(existing)
            target.append(new_def)

    def _py3o_merge_font_faces(self, report_font_decls, base_font_decls):
        if report_font_decls is None or base_font_decls is None:
            return
        name_attr = _clark("style", "name")
        existing_names = {child.get(name_attr) for child in report_font_decls}
        for base_font in base_font_decls:
            name = base_font.get(name_attr)
            if name and name not in existing_names:
                report_font_decls.append(deepcopy(base_font))
                existing_names.add(name)

    def _py3o_merge_images(
        self, seed_elements, report_zip_in, base_zip_in, new_zip_entries, manifest_root
    ):
        href_attr = _clark("xlink", "href")
        image_tag = _clark("draw", "image")
        report_names = set(report_zip_in.namelist())
        counter = 0
        for el in seed_elements:
            for img in el.iter(image_tag):
                href = img.get(href_attr)
                if not href or not href.startswith("Pictures/"):
                    continue
                if href in new_zip_entries:
                    continue
                try:
                    base_image_bytes = base_zip_in.read(href)
                except KeyError:
                    continue

                target_name = href
                if href in report_names:
                    try:
                        report_image_bytes = report_zip_in.read(href)
                    except KeyError:
                        report_image_bytes = None
                    if report_image_bytes != base_image_bytes:
                        counter += 1
                        ext = href.rsplit(".", 1)[-1] if "." in href else "png"
                        target_name = "Pictures/%s%d.%s" % ("sbt_", counter, ext)
                        img.set(href_attr, target_name)

                new_zip_entries[target_name] = base_image_bytes
                self._py3o_add_manifest_entry(manifest_root, target_name)

    def _py3o_add_manifest_entry(self, manifest_root, path):
        full_path_attr = _clark("manifest", "full-path")
        for entry in manifest_root:
            if entry.get(full_path_attr) == path:
                return
        media_type, _encoding = mimetypes.guess_type(path)
        entry = etree.SubElement(manifest_root, _clark("manifest", "file-entry"))
        entry.set(full_path_attr, path)
        entry.set(_clark("manifest", "media-type"), media_type or "")

    def _py3o_write_merged_zip(self, report_zip_in, overrides):
        out = BytesIO()
        with ZipFile(out, "w") as zf:
            zf.writestr(
                "mimetype", report_zip_in.read("mimetype"), compress_type=ZIP_STORED
            )
            for info in report_zip_in.infolist():
                if info.filename == "mimetype" or info.filename in overrides:
                    continue
                zf.writestr(info, report_zip_in.read(info.filename))
            for name, data in overrides.items():
                zf.writestr(name, data)
        return out.getvalue()

    # EXTRA FUNCTIONS
    @api.model
    def _get_config_param(self, key):
        obj_config_param = self.env["ir.config_parameter"].sudo()
        return obj_config_param.get_param(key, "")

    @api.model
    def _get_selection_label(self, rec, field_name):
        result = "-"
        field = rec._fields[field_name]
        if field.related_field:
            field = field.related_field

        selection = field.selection

        if isfunction(selection):
            selection = selection(rec)

        for value, label in selection:
            if value == getattr(rec, field_name, False):
                result = label
        return result

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
            "babel": __import__("babel"),
            "babel_dates": babel.dates,
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
        # EXTRA FUNCTIONS
        res["parameter_value"] = self._get_config_param
        res["selection_label"] = self._get_selection_label

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
