# Copyright 2025 OpenSynergy Indonesia
# Copyright 2025 PT. Simetri Sinergi Indonesia
# License AGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
import importlib.util
import logging
import mimetypes
import os
import re
import sys
import uuid
from copy import deepcopy
from inspect import isfunction
from io import BytesIO
from zipfile import ZIP_STORED, BadZipFile, ZipFile

import babel.dates
from genshi.core import Markup
from lxml import etree, html

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

# -- Html field -> ODF markup (see Py3oReport._get_html_text) -----------------
_HTML_ODF_BLOCK_TAGS = ("p", "h1", "h2", "h3", "h4", "h5", "h6", "li")
_HTML_ODF_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_HTML_ODF_BOLD_TAGS = ("b", "strong")
_HTML_ODF_ITALIC_TAGS = ("i", "em")
_HTML_ODF_UNDERLINE_TAGS = ("u",)
# Tags that, met while walking a non-block container, close whatever inline
# text was accumulated so far and start a fresh block of their own.
_HTML_ODF_FLUSH_TAGS = ("ul", "ol", "hr", "table") + _HTML_ODF_BLOCK_TAGS

# Strips ODF markup so a rendered block's *visible* text can be checked for
# blankness (see _get_html_text_blocks' skip-blank-paragraph handling).
_ODF_TAG_RE = re.compile(r"<[^>]+>")

# table-cell automatic style injected by _py3o_ensure_html_table_style() so
# _get_html_text_table()'s cells render with a visible border -- see that
# method's docstring for why it cannot simply be a style already present in
# the source template.
_HTML_TABLE_CELL_STYLE_NAME = "OdooHtmlTableCell"

# text (character) automatic styles injected by _py3o_ensure_html_text_styles()
# for <h1>-<h6> font sizes -- same constraint as the table-cell style above,
# see that method's docstring. All headings are bold, decreasing in size.
_HTML_HEADING_STYLE_NAMES = {
    "h1": "OdooH1",
    "h2": "OdooH2",
    "h3": "OdooH3",
    "h4": "OdooH4",
    "h5": "OdooH5",
    "h6": "OdooH6",
}
_HTML_HEADING_FONT_SIZES = {
    "h1": "24pt",
    "h2": "20pt",
    "h3": "17pt",
    "h4": "14pt",
    "h5": "12pt",
    "h6": "10pt",
}

# The 16 standard CSS2/HTML4 keyword colors -- the FIXED, pre-registered set
# _get_html_text_color_style() can resolve `style="color: ..."` (or
# `<font color="...">`) against. ODF text runs can only reference an
# already-declared style (see _py3o_ensure_html_text_styles()), so this list
# is deliberately bounded rather than attempting to support arbitrary colors;
# an unlisted color (a custom hex not equal to one of these) renders without
# color instead of raising -- same "documented limitation, not a crash"
# posture as colspan/rowspan in _get_html_text_table().
_HTML_COLOR_KEYWORDS = {
    "black": "000000",
    "silver": "c0c0c0",
    "gray": "808080",
    "grey": "808080",
    "white": "ffffff",
    "maroon": "800000",
    "red": "ff0000",
    "purple": "800080",
    "fuchsia": "ff00ff",
    "green": "008000",
    "lime": "00ff00",
    "olive": "808000",
    "yellow": "ffff00",
    "navy": "000080",
    "blue": "0000ff",
    "teal": "008080",
    "aqua": "00ffff",
}
_HTML_KNOWN_COLOR_HEXES = sorted(set(_HTML_COLOR_KEYWORDS.values()))


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
        report_bytes = self._py3o_ensure_html_table_style(report_bytes)
        report_bytes = self._py3o_ensure_html_text_styles(report_bytes)
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

    def _get_html_text_escape(self, text):
        """Escape XML special characters and collapse whitespace.

        :param text: raw text extracted from an HTML text node
        :return: text safe to place inside ODF ``text:p`` content
        :rtype: str
        """
        collapsed = re.sub(r"\s+", " ", text)
        collapsed = collapsed.replace("&", "&amp;")
        collapsed = collapsed.replace("<", "&lt;")
        collapsed = collapsed.replace(">", "&gt;")
        return collapsed

    def _get_html_text_style_flags(self, el):
        """Detect Bold/Italic/Underline/color flags for one HTML element.

        Combines the semantic tag (``b``/``strong``, ``i``/``em``,
        ``u``) with the ``style`` attribute (``font-weight``,
        ``font-style``, ``text-decoration``, ``color``) so both
        sources are honoured, matching the fix requested for this
        method. Color also recognizes the legacy ``<font color="...">``
        attribute.

        :param el: ``lxml.html`` element being inspected
        :return: four-tuple ``(bold, italic, underline, color_style)``,
            ``color_style`` a pre-registered style name (see
            ``_get_html_text_color_style``) or ``None``
        :rtype: tuple
        """
        tag = el.tag if isinstance(el.tag, str) else ""
        bold = tag in _HTML_ODF_BOLD_TAGS
        italic = tag in _HTML_ODF_ITALIC_TAGS
        underline = tag in _HTML_ODF_UNDERLINE_TAGS
        style = (el.get("style") or "").lower()
        if not bold:
            match = re.search(r"font-weight\s*:\s*([a-z0-9]+)", style)
            if match:
                value = match.group(1)
                if value in ("bold", "bolder"):
                    bold = True
                elif value.isdigit() and int(value) >= 600:
                    bold = True
        if not italic and re.search(r"font-style\s*:\s*italic", style):
            italic = True
        if not underline and re.search(r"text-decoration\s*:\s*underline", style):
            underline = True
        color_style = self._get_html_text_color_style(el, style)
        return bold, italic, underline, color_style

    def _get_html_text_color_style(self, el, lowercase_style):
        """Resolve ``el``'s own text color to a pre-registered style name.

        Reads ``style="color: ..."`` first, falling back to the
        legacy ``<font color="...">`` attribute. Only a color equal to
        one of the 16 standard keywords in ``_HTML_COLOR_KEYWORDS``
        (by name or exact hex equivalent) resolves to a style name --
        see ``_py3o_ensure_html_text_styles`` for why the set is
        bounded. Anything else (an unrecognized name, an arbitrary
        hex) returns ``None`` rather than raising.

        :param el: ``lxml.html`` element being inspected
        :param lowercase_style: ``el``'s ``style`` attribute, already
            lowercased by the caller (``_get_html_text_style_flags``)
        :return: ``"OdooColor_<hex>"`` or ``None``
        :rtype: str or None
        """
        raw = None
        match = re.search(r"color\s*:\s*([^;]+)", lowercase_style)
        if match:
            raw = match.group(1).strip()
        elif el.tag == "font" and el.get("color"):
            raw = el.get("color").strip().lower()
        if not raw:
            return None
        hex_value = _HTML_COLOR_KEYWORDS.get(raw)
        if hex_value is None:
            hex_match = re.match(r"^#?([0-9a-f]{3}|[0-9a-f]{6})$", raw)
            if hex_match:
                digits = hex_match.group(1)
                if len(digits) == 3:
                    digits = "".join(c * 2 for c in digits)
                if digits in _HTML_KNOWN_COLOR_HEXES:
                    hex_value = digits
        if hex_value is None:
            return None
        return "OdooColor_%s" % hex_value

    def _get_html_text_run(
        self, text, bold, italic, underline, color_style=None, heading_style=None
    ):
        """Wrap escaped text in nested ``text:span`` per active style.

        Nesting order (innermost first): Underline, Italic, Bold,
        color, heading -- heading outermost since it is a block-level
        property of the whole paragraph, not a per-run one like the
        others.

        :param text: already XML-escaped text
        :param bold: whether the ``Bold`` style applies
        :param italic: whether the ``Italic`` style applies
        :param underline: whether the ``Underline`` style applies
        :param color_style: pre-registered color style name to apply,
            or ``None`` (see ``_get_html_text_color_style``)
        :param heading_style: pre-registered heading style name
            (``OdooH1``-``OdooH6``) inherited from the enclosing
            block, or ``None`` outside a heading
        :return: ``text`` wrapped in zero or more nested
            ``text:span`` elements, never a new ``text:p``
        :rtype: str
        """
        if not text:
            return ""
        result = text
        if underline:
            result = '<text:span text:style-name="Underline">%s</text:span>' % result
        if italic:
            result = '<text:span text:style-name="Italic">%s</text:span>' % result
        if bold:
            result = '<text:span text:style-name="Bold">%s</text:span>' % result
        if color_style:
            result = '<text:span text:style-name="%s">%s</text:span>' % (
                color_style,
                result,
            )
        if heading_style:
            result = '<text:span text:style-name="%s">%s</text:span>' % (
                heading_style,
                result,
            )
        return result

    def _get_html_text_inline(
        self, el, bold, italic, underline, color_style=None, heading_style=None
    ):
        """Serialize one element's content as inline ODF markup.

        Recurses into children, combining each element's own
        Bold/Italic/Underline/color with the flags inherited from its
        ancestors -- a nested element's own color overrides an
        ancestor's, matching CSS cascade. A ``<br>`` becomes a single
        ``<text:line-break/>``; a tag outside the supported set falls
        back to its plain escaped text instead of raising.

        :param el: ``lxml.html`` element whose content is serialized
        :param bold: Bold flag inherited from ancestors
        :param italic: Italic flag inherited from ancestors
        :param underline: Underline flag inherited from ancestors
        :param color_style: color style name inherited from ancestors,
            or ``None``
        :param heading_style: heading style name from the enclosing
            block (constant through the whole recursion, never
            re-derived per element), or ``None``
        :return: inline ODF markup for ``el``'s text, children and
            their tails
        :rtype: str
        """
        (
            own_bold,
            own_italic,
            own_underline,
            own_color,
        ) = self._get_html_text_style_flags(el)
        bold = bold or own_bold
        italic = italic or own_italic
        underline = underline or own_underline
        color_style = own_color or color_style

        parts = []
        if el.text:
            parts.append(
                self._get_html_text_run(
                    self._get_html_text_escape(el.text),
                    bold,
                    italic,
                    underline,
                    color_style,
                    heading_style,
                )
            )
        for child in el:
            if not isinstance(child.tag, str):
                continue  # comment/processing instruction node
            if child.tag == "br":
                parts.append("<text:line-break/>")
            else:
                parts.append(
                    self._get_html_text_inline(
                        child, bold, italic, underline, color_style, heading_style
                    )
                )
            if child.tail:
                parts.append(
                    self._get_html_text_run(
                        self._get_html_text_escape(child.tail),
                        bold,
                        italic,
                        underline,
                        color_style,
                        heading_style,
                    )
                )
        return "".join(parts)

    def _py3o_ensure_html_table_style(self, report_bytes):
        """Inject the bordered ``table-cell`` style used by HTML tables.

        ``_get_html_text_table`` references
        ``table:style-name="OdooHtmlTableCell"`` on every cell it
        emits, but that style cannot already exist in the source ODT
        template -- the table itself is only built at render time, so
        no template author could have created a matching
        ``style:style`` for it. This adds that one style to the
        report's ``content.xml`` ``<office:automatic-styles>`` before
        py3o ever sees the template, using the same "rewrite the ODT
        zip in memory" technique already used by
        ``_py3o_merge_base_template`` for the letterhead. Idempotent:
        a report whose ``content.xml`` already defines a style of that
        name (e.g. a second call for the same template) is left
        untouched.

        Failure is non-fatal: any malformed/unreadable template is
        returned unchanged rather than raising here, since a missing
        border is far less disruptive than an unprintable report --
        ``_get_html_text_table``'s cells simply render without a
        visible border in that case (same as before this method
        existed).

        :param report_bytes: raw ODT template bytes, before py3o's
            own base-template (letterhead) merge and rendering
        :return: ``report_bytes``, with the style added to
            ``content.xml``
        :rtype: bytes
        """
        try:
            report_zip_in = ZipFile(BytesIO(report_bytes))
            content_root = etree.fromstring(report_zip_in.read("content.xml"))
        except (BadZipFile, KeyError, etree.XMLSyntaxError):
            return report_bytes

        auto_styles = content_root.find(_clark("office", "automatic-styles"))
        if auto_styles is None:
            return report_bytes
        if (
            self._find_style_by_name(auto_styles, _HTML_TABLE_CELL_STYLE_NAME)
            is not None
        ):
            return report_bytes

        style = etree.SubElement(auto_styles, _clark("style", "style"))
        style.set(_clark("style", "name"), _HTML_TABLE_CELL_STYLE_NAME)
        style.set(_clark("style", "family"), "table-cell")
        props = etree.SubElement(style, _clark("style", "table-cell-properties"))
        # Explicit per-side longhand, not the fo:border shorthand: some ODF
        # consumers render the shorthand inconsistently on table cells, and
        # the source HTML this method exists to approximate already uses the
        # same per-side form (border-top/-bottom/-left/-right).
        for side in ("top", "bottom", "left", "right"):
            props.set(_clark("fo", "border-%s" % side), "1pt solid #000000")
        props.set(_clark("fo", "padding"), "0.05in")

        new_zip_entries = {
            "content.xml": etree.tostring(
                content_root, xml_declaration=True, encoding="UTF-8"
            )
        }
        return self._py3o_write_merged_zip(report_zip_in, new_zip_entries)

    def _py3o_ensure_html_text_styles(self, report_bytes):
        """Inject the heading-size and text-color styles ``get_html_text`` uses.

        Same constraint and technique as
        ``_py3o_ensure_html_table_style`` (see its docstring): neither
        an ``<h1>``-``<h6>`` font-size style nor a ``style="color:
        ..."`` color style can already exist in the source template,
        so both are added to ``content.xml``'s
        ``<office:automatic-styles>`` here, before py3o ever sees the
        template. Idempotent per style (a style already present by
        name is left untouched); unlike the table-cell style this
        injects up to 22 styles (6 headings + 16 colors) in one pass,
        skipping only individual names that already exist.

        Colors are deliberately bounded to the 16 standard CSS2
        keywords in ``_HTML_COLOR_KEYWORDS`` -- see
        ``_get_html_text_color_style`` for why an arbitrary/unlisted
        color cannot be supported this way.

        Failure is non-fatal, same reasoning as the table-cell style:
        a malformed/unreadable template is returned unchanged rather
        than raising, since missing heading sizes/colors are far less
        disruptive than an unprintable report.

        :param report_bytes: raw ODT template bytes, before py3o's
            own base-template (letterhead) merge and rendering
        :return: ``report_bytes``, with the styles added to
            ``content.xml``
        :rtype: bytes
        """
        try:
            report_zip_in = ZipFile(BytesIO(report_bytes))
            content_root = etree.fromstring(report_zip_in.read("content.xml"))
        except (BadZipFile, KeyError, etree.XMLSyntaxError):
            return report_bytes

        auto_styles = content_root.find(_clark("office", "automatic-styles"))
        if auto_styles is None:
            return report_bytes

        changed = False
        for tag, style_name in _HTML_HEADING_STYLE_NAMES.items():
            if self._find_style_by_name(auto_styles, style_name) is not None:
                continue
            style = etree.SubElement(auto_styles, _clark("style", "style"))
            style.set(_clark("style", "name"), style_name)
            style.set(_clark("style", "family"), "text")
            props = etree.SubElement(style, _clark("style", "text-properties"))
            props.set(_clark("fo", "font-size"), _HTML_HEADING_FONT_SIZES[tag])
            props.set(_clark("fo", "font-weight"), "bold")
            changed = True

        for hex_value in _HTML_KNOWN_COLOR_HEXES:
            style_name = "OdooColor_%s" % hex_value
            if self._find_style_by_name(auto_styles, style_name) is not None:
                continue
            style = etree.SubElement(auto_styles, _clark("style", "style"))
            style.set(_clark("style", "name"), style_name)
            style.set(_clark("style", "family"), "text")
            props = etree.SubElement(style, _clark("style", "text-properties"))
            props.set(_clark("fo", "color"), "#%s" % hex_value)
            changed = True

        if not changed:
            return report_bytes

        new_zip_entries = {
            "content.xml": etree.tostring(
                content_root, xml_declaration=True, encoding="UTF-8"
            )
        }
        return self._py3o_write_merged_zip(report_zip_in, new_zip_entries)

    def _get_html_text_table(self, el):
        """Render an HTML ``<table>`` as a real, nested ODF table.

        Every ``get_html_text`` call substitutes the content of one
        already-existing ``<text:p>`` in the ODT template (see
        ``_get_html_text``), and ODF forbids a ``table:table`` inside
        a ``text:p`` -- so a table cannot simply be inserted at that
        point. Worse, py3o.template does not insert our return value
        directly inside that ``<text:p>``: it wraps it in an extra
        ``<text:span>`` of its own (``py3o/template/main.py``,
        ``_TemplateHelper.__handle_link``, the ``instruction ==
        "content"`` branch) to preserve the placeholder's character
        style -- confirmed by rendering this exact template through
        the real pipeline and diffing the produced ``content.xml``
        against the source. So this closes that ``<text:span>`` *and*
        its ``<text:p>``, emits a genuine ``<table:table>``, then
        reopens an (unstyled) ``<text:span>`` inside an (unstyled)
        ``<text:p>`` for whatever follows -- relying on py3o's own
        ``</text:span>`` and the template's own ``</text:p>``, both
        already emitted right after the substitution point, to close
        that reopened pair. This is safe wherever the substitution's
        ``<text:p>`` sits inside a container that also allows
        ``table:table`` as a child, which every container these
        templates actually use it in does (``table:table-cell``, list
        items, sections, headers/footers, body text -- ODF 1.2
        §5.1.2); verified empirically end-to-end through the real
        py3o + LibreOffice 6.1 pipeline, not just a hand-built ODT.

        Cells reference the ``OdooHtmlTableCell`` style (1pt border +
        padding) for a visible grid -- see
        ``_py3o_ensure_html_table_style``, which injects that style
        into the template at render time since no source template
        could define it up front. Column widths are not set, so
        columns render evenly spaced. Each ``<tr>`` (found at any depth,
        so ``<thead>``/``<tbody>`` wrapping is transparent) becomes
        one ``table:table-row``; its ``<td>``/``<th>`` cells each
        become one ``table:table-cell`` holding a single ``text:p``
        (``<th>`` rendered Bold). A direct ``<thead>`` child's rows are
        wrapped in ``<table:table-header-rows>``, so a LibreOffice/ODF
        renderer repeats them on every page a long table spans --
        without this, each page fragment of a table that spans a page
        break would show only its own top/bottom cell borders with no
        header, reading as two disconnected tables rather than one
        continuing table. ``colspan``/``rowspan`` are not supported:
        each cell occupies exactly one column, extra ``<p>`` beyond
        the first in one cell are flattened onto that cell's single
        line, and a nested ``<table>`` inside a cell has its rows
        flattened into this table's own row list.

        :param el: ``<table>`` element being serialized
        :return: ``</text:span></text:p>``, the table, and a reopened
            ``<text:p><text:span>`` ready for py3o's own
            ``</text:span>`` and the template's own ``</text:p>``, or
            ``""`` if the table has no rows
        :rtype: str
        """
        thead = el.find("thead")
        header_trs = set(thead.iter("tr")) if thead is not None else set()

        rows = []
        max_cols = 0
        for row in el.iter("tr"):
            cells = [
                child
                for child in row
                if isinstance(child.tag, str) and child.tag in ("td", "th")
            ]
            if not cells:
                continue
            rows.append((row in header_trs, cells))
            max_cols = max(max_cols, len(cells))
        if not rows:
            return ""

        def _row_xml(cells):
            """Render one ``<tr>``'s ``cells`` as a ``table:table-row``."""
            row_parts = ["<table:table-row>"]
            for cell in cells:
                text = self._get_html_text_inline(cell, cell.tag == "th", False, False)
                row_parts.append(
                    '<table:table-cell table:style-name="%s" '
                    'office:value-type="string">'
                    "<text:p>%s</text:p></table:table-cell>"
                    % (_HTML_TABLE_CELL_STYLE_NAME, text)
                )
            row_parts.append("</table:table-row>")
            return "".join(row_parts)

        parts = ['<table:table table:name="HtmlTable_%s">' % uuid.uuid4().hex[:12]]
        parts.append("<table:table-column/>" * max_cols)
        # <thead> rows are always contiguous at the start in standard HTML,
        # so a single header block (not interleaved per-row) is enough.
        if header_trs:
            parts.append("<table:table-header-rows>")
            parts.extend(_row_xml(cells) for is_header, cells in rows if is_header)
            parts.append("</table:table-header-rows>")
        parts.extend(_row_xml(cells) for is_header, cells in rows if not is_header)
        parts.append("</table:table>")
        return "</text:span></text:p>%s<text:p><text:span>" % "".join(parts)

    def _get_html_text_list(self, tag, el, blocks):
        """Render one ``<ul>``/``<ol>``'s ``<li>`` children as blocks.

        Split out of ``_get_html_text_blocks`` (see its docstring)
        purely to keep that method's branching under the repo's
        flake8 ``max-complexity``; behaviour is unchanged. Each
        ``<li>`` becomes its own entry of ``blocks``, prefixed with a
        bullet (``ul``) or a number (``ol``); a non-``<li>`` child
        (e.g. a nested ``<ul>``) is walked back through
        ``_get_html_text_blocks`` instead.

        :param tag: ``"ul"`` or ``"ol"``, the list's own tag
        :param el: the ``<ul>``/``<ol>`` element being walked
        :param blocks: list mutated in place, one string per block
        :return: None
        """
        index = 0
        for child in el:
            if not isinstance(child.tag, str):
                continue
            if child.tag == "li":
                index += 1
                prefix = "%d. " % index if tag == "ol" else "• "
                indent = '<text:s text:c="3"/>'
                blocks.append(
                    indent
                    + prefix
                    + self._get_html_text_inline(child, False, False, False)
                )
            else:
                self._get_html_text_blocks(child, blocks)

    def _get_html_text_blocks(self, el, blocks):
        """Collect one ODF block string per block-level HTML element.

        ``p``/``h1``-``h6``/``li`` each become one entry of
        ``blocks``; ``hr`` contributes an empty entry (rendered as a
        blank separator line by the caller); ``ul``/``ol`` are
        unwrapped so each ``li`` still becomes its own entry, prefixed
        with a bullet (``ul``) or a number (``ol``); ``table`` becomes
        one entry rendered by ``_get_html_text_table``. Bare
        text/inline content with no wrapping block tag is kept in a
        single shared entry.

        A ``p``/heading with no visible text and no ``<br>`` (e.g. a
        stray whitespace-only paragraph left over from pasting into
        the Odoo Html editor) contributes **no** entry at all, rather
        than an empty one: since every entry is later joined with a
        line break (see ``_get_html_text``), an empty entry would
        still add a blank line the source never visually showed (the
        browser collapses it to ~0 height). A paragraph containing
        *only* ``<br>`` -- how the Html editor encodes a deliberate
        blank line -- keeps its slot as an empty entry (so joining
        still adds the one extra break that blank line calls for) but
        drops the ``<text:line-break/>`` marker itself, which would
        otherwise double that break.

        :param el: ``lxml.html`` element being walked
        :param blocks: list mutated in place, one string per block
        :return: None
        """
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag in ("ul", "ol"):
            self._get_html_text_list(tag, el, blocks)
            return
        if tag == "hr":
            blocks.append("")
            return
        if tag == "table":
            blocks.append(self._get_html_text_table(el))
            return
        if tag in _HTML_ODF_BLOCK_TAGS:
            heading_style = _HTML_HEADING_STYLE_NAMES.get(tag)
            content = self._get_html_text_inline(
                el, False, False, False, heading_style=heading_style
            )
            if content == "<text:line-break/>":
                # A paragraph that is *only* a <br> -- the Html editor's
                # way of encoding a deliberate blank line. Keep the slot
                # (so joining below still adds one extra line break) but
                # drop the marker itself, which would otherwise double
                # that break.
                blocks.append("")
            elif _ODF_TAG_RE.sub("", content).strip():
                blocks.append(content)
            return

        inline_parts = []
        if el.text:
            inline_parts.append(self._get_html_text_escape(el.text))
        for child in el:
            if not isinstance(child.tag, str):
                continue
            if child.tag in _HTML_ODF_FLUSH_TAGS:
                if inline_parts:
                    blocks.append("".join(inline_parts))
                    inline_parts = []
                self._get_html_text_blocks(child, blocks)
            elif child.tag == "br":
                inline_parts.append("<text:line-break/>")
            else:
                inline_parts.append(
                    self._get_html_text_inline(child, False, False, False)
                )
            if child.tail:
                inline_parts.append(self._get_html_text_escape(child.tail))
        if inline_parts:
            blocks.append("".join(inline_parts))

    @api.model
    def _get_html_text(self, html_value):
        """Convert an ``Html`` field value into ODF markup.

        Meant to replace a ``text:input`` already nested inside an
        existing ``<text:p>`` in a py3o ODT template (see the
        ``get_html_text`` entry registered by ``_get_parser_context``).
        Supports ``p``, ``br``, ``h1``-``h6``, ``ul``/``ol``/``li``,
        ``span``/``font``, ``hr`` and ``table``; bold/italic/underline
        are detected from both the semantic tag (``b``/``strong``,
        ``i``/``em``, ``u``) and the ``style`` attribute. Tags outside
        that set fall back to plain text instead of raising.

        Every construct except ``table`` stays entirely inside that
        one ``<text:p>`` (never opens a new one). ``table`` is the
        one exception: it closes that paragraph, emits a real
        ``<table:table>``, and reopens an empty paragraph for
        whatever follows -- see ``_get_html_text_table`` for why that
        is safe here.

        Blocks are joined by a single ``<text:line-break/>`` -- one
        new line per source paragraph, matching normal paragraph
        flow. A deliberate blank line between two paragraphs is
        therefore carried entirely by the source's own ``<p><br></p>``
        (rendered as its own block, see ``_get_html_text_blocks``),
        not added again here.

        :param html_value: raw HTML string from an Odoo ``Html`` field
        :return: markup safe to insert as-is in the ODT template
        :rtype: genshi.core.Markup
        """
        if not html_value or not html_value.strip():
            return Markup("")
        root = html.fragment_fromstring(html_value, create_parent="div")
        blocks = []
        self._get_html_text_blocks(root, blocks)
        return Markup("<text:line-break/>".join(blocks))

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
        res["get_html_text"] = self._get_html_text

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
