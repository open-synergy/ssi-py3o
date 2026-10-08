# Copyright 2025 OpenSynergy Indonesia
# Copyright 2025 PT. Simetri Sinergi Indonesia
# License AGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
import importlib.util
import logging
import mimetypes
import os
import re
import subprocess
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
    "svg": "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0",
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

# The close-current-paragraph/reopen escape _get_html_text_table() and
# _get_html_text_list() both return (see either's docstring) always starts
# and ends with these two exact strings -- shared so _join_html_text_blocks()
# can recognize such a block by nothing more than a startswith/endswith
# check, without either of those methods needing to expose anything else.
_ESCAPE_BLOCK_PREFIX = "</text:span></text:p>"
_ESCAPE_BLOCK_SUFFIX = "<text:p><text:span>"

# A CSS property name only counts when nothing that could extend it sits right
# before it: a plain `font-weight` declaration must not match inside Word's
# `mso-bidi-font-weight`, nor `color` inside `background-color`/`border-color`,
# nor `font-size` inside `mso-ansi-font-size`. Used by every style lookup below.
_CSS_PROP_START = r"(?<![\w-])"
# Matches an inline `style="...font-size: <n><px|pt>..."` declaration -- see
# Py3oReport._get_html_text_size_style(). Only px/pt are recognized (what the
# Odoo Html editor emits); other units (em, %, ...) are left unsupported
# rather than guessed at, same "documented limitation" posture as an
# unlisted color in _get_html_text_color_style().
_FONT_SIZE_RE = re.compile(_CSS_PROP_START + r"font-size\s*:\s*([\d.]+)\s*(px|pt)")

# A run of non-whitespace this long or longer (e.g. a pasted string with no
# spaces) has no natural word-wrap point, forcing LibreOffice's PDF export
# into a character-level "forced wrap" fallback for that one line -- which
# some PDF renderers (confirmed against Atril/MATE's poppler-based viewer)
# then mis-compute the line height for, visually overlapping the next line.
# _break_long_runs() avoids that fallback entirely by giving such a run
# normal word-wrap points every _LONG_RUN_CHUNK characters, via an inserted
# zero-width space -- invisible, so ordinary text (virtually every real word
# is well under this threshold) is never touched.
_LONG_RUN_CHUNK = 20
_LONG_RUN_RE = re.compile(r"\S{%d,}" % (_LONG_RUN_CHUNK + 1))
_ZERO_WIDTH_SPACE = "​"

# A 1pt text style and the span that uses it. A paragraph that the escape
# blocks leave behind at the very start or end of the content holds only this
# span (one zero-width space), so its line is as tall as 1pt text instead of a
# full line of the wrapper paragraph's font. Registered by
# _py3o_ensure_html_align_styles() on every template that calls
# get_html_text. (``text:hidden-paragraph`` was tried and LibreOffice
# ignores it.)
_HTML_TINY_STYLE_NAME = "OdooHtmlTiny"
_HTML_TINY_SPAN = '<text:span text:style-name="%s">%s</text:span>' % (
    _HTML_TINY_STYLE_NAME,
    _ZERO_WIDTH_SPACE,
)

# table-cell automatic style injected by _py3o_ensure_html_table_style() so
# _get_html_text_table()'s cells render with a visible border -- see that
# method's docstring for why it cannot simply be a style already present in
# the source template.
_HTML_TABLE_CELL_STYLE_NAME = "OdooHtmlTableCell"
# A thin/"normal" table-grid weight (LibreOffice's own default table border
# is in this range) -- the source HTML's own <table style="border: 1px..."">
# only styles the table's outer edge, not a per-cell grid, so there is no
# HTML value to read this from; a fixed, deliberately thin weight instead of
# the previous 1pt (visibly heavier than a normal document table).
_HTML_TABLE_CELL_BORDER = "0.5pt solid #000000"

# paragraph-family automatic style injected by _py3o_ensure_html_table_style()
# for _get_html_text_table()'s <text:p> cell content -- an unstyled <text:p>
# falls back to the ODF *global default* paragraph style (this template's is
# 12pt Liberation Serif), which reads as visibly oversized next to the rest
# of the report's body text (rendered through automatic per-placeholder
# styles around 8pt, not through any named/reusable paragraph style this
# method could instead just reference). Matches this report's own body size.
_HTML_TABLE_TEXT_STYLE_NAME = "OdooHtmlTableText"
_HTML_TABLE_TEXT_FONT_SIZE = "8pt"

# _get_html_text_list() renders <ul>/<ol> as genuine ODF list markup --
# <text:list>/<text:list-item> driven by a <text:list-style> -- rather than
# manually prefixing each item with "1. "/"• " text inside its own
# hand-indented paragraph. That first approach (a fixed fo:margin-left/
# fo:text-indent guess) could only approximate where a wrapped continuation
# line should align, since it never actually measured the marker's own
# rendered width; a numbered marker's width isn't even constant (single vs.
# double-digit numbers). A real text:list-style's
# style:list-level-label-alignment (see _py3o_append_list_style) instead has
# the renderer itself compute and align the hang indent to the marker it
# generates, the same way every ODF-aware word processor does. This needs
# each <li> to be its own <text:list-item>/<text:p> (still no inline/span
# equivalent for a paragraph-level indent), which is also why
# _get_html_text_list uses the same close/reopen escape as
# _get_html_text_table instead of staying inline in the shared placeholder
# paragraph.
_HTML_LIST_ITEM_STYLE_NAME = "OdooListItem"
# Per-wrapper variant of the list item style: "OdooListItem_<base_style>".
_HTML_LIST_ITEM_VARIANT_FORMAT = "%s_%s"
_HTML_LIST_OL_STYLE_NAME = "OdooOL"
_HTML_LIST_UL_STYLE_NAME = "OdooUL"
_HTML_LIST_STYLE_NAMES = {
    "ol": _HTML_LIST_OL_STYLE_NAME,
    "ul": _HTML_LIST_UL_STYLE_NAME,
}
# Where the item's own text (and any wrapped continuation line) sits,
# measured from the paragraph's normal left margin -- this is also the
# fo:margin-left/list-tab-stop-position _py3o_append_list_style sets. Matches
# a browser's UL/OL default indent (~40px/2.5em) instead of the block sitting
# flush with surrounding paragraphs the way a first attempt at this (fixed
# tab-stop only, no block-level margin) rendered.
_HTML_LIST_ITEM_INDENT = "0.5in"
# How far left of that the marker itself hangs (fo:text-indent, negative) --
# together these two are why the marker is NOT flush with the page margin
# either: matches list-style-position: outside's default look, marker sitting
# a bit left of the indented text block rather than at the very edge.
_HTML_LIST_MARKER_OVERHANG = "0.2in"

# Paragraph alignment (HTML text-align / align) -> ODF fo:text-align. An
# aligned <p>/<h1>-<h6> cannot stay inside the placeholder's shared
# <text:p>: alignment is a paragraph property, and a manual
# <text:line-break/> inside a justified paragraph makes LibreOffice stretch
# the line it ends. _get_html_text_aligned_block() therefore emits each
# such block as its own <text:p> (the same close/reopen escape tables and
# lists use), styled with one of the automatic styles named by
# _html_align_style_name() and injected by _py3o_ensure_html_align_styles().
_HTML_ALIGN_VALUES = {
    "left": "start",
    "start": "start",
    "right": "end",
    "end": "end",
    "center": "center",
    "justify": "justify",
}
_HTML_ALIGN_STYLE_PREFIX = "OdooHtmlAlign"
_HTML_ALIGN_ODF_KEYS = ("start", "center", "end", "justify")
# The block tags whose own text-align is honoured (<li> is excluded: its
# alignment is part of the list rendering, not of a paragraph of its own).
_HTML_ALIGN_BLOCK_TAGS = ("p", "h1", "h2", "h3", "h4", "h5", "h6")
_HTML_ALIGN_STYLE_RE = re.compile(
    r"text-align\s*:\s*(left|right|center|justify|start|end)\b", re.IGNORECASE
)
# CSS paragraph spacing read from a <p> (see _get_html_text_paragraph_margins).
# The tuple order is the one used for ``margins`` everywhere: top, bottom,
# left, right, text-indent.
_HTML_MARGIN_PROPS = (
    "margin-top",
    "margin-bottom",
    "margin-left",
    "margin-right",
    "text-indent",
)
_HTML_MARGIN_NAME_KEYS = ("mt", "mb", "ml", "mr", "ti")
# Index into the CSS ``margin`` shorthand (top, right, bottom, left) for 1, 2,
# 3 and 4 values, keyed by the number of values given.
_HTML_MARGIN_SHORTHAND = {
    1: (0, 0, 0, 0),
    2: (0, 1, 0, 1),
    3: (0, 1, 2, 1),
    4: (0, 1, 2, 3),
}
_HTML_MARGIN_SHORTHAND_SIDES = (
    "margin-top",
    "margin-right",
    "margin-bottom",
    "margin-left",
)
# CSS length -> points. 1px = 0.75pt (96px = 1in = 72pt).
_HTML_LENGTH_TO_PT = {
    "pt": 1.0,
    "px": 0.75,
    "cm": 72.0 / 2.54,
    "mm": 72.0 / 25.4,
    "in": 72.0,
}
_HTML_LENGTH_RE = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+))\s*(pt|px|cm|mm|in)?$")
# Only ASCII whitespace is collapsed in text; U+00A0 is kept as is.
_HTML_ASCII_SPACE_RE = re.compile(r"[ \t\r\n\f]+")
# A text run in a Symbol font: Word's bullet glyph is U+00B7 (or the private
# use U+F0B7) there, which the editor shows as a bullet.
_HTML_FONT_FAMILY_RE = re.compile(_CSS_PROP_START + r"font-family\s*:\s*([^;]*)")
_HTML_SYMBOL_BULLETS = {0x00B7: "•", 0xF0B7: "•"}
# A template placeholder as written in <text:text-input text:description>:
# py3o://function="get_html_text(<expr>)". Group 2 is the call's arguments.
_HTML_TEXT_DESCRIPTION_RE = re.compile(
    r'^(py3o://function="get_html_text\()(.*)(\)")$', re.DOTALL
)
# What an aligned block starts and ends with, see _get_html_text_aligned_block().
# _ESCAPE_BLOCK_PREFIX/_SUFFIX are defined above, next to the table/list blocks.
_ALIGN_BLOCK_START = '%s<text:p text:style-name="%s' % (
    _ESCAPE_BLOCK_PREFIX,
    _HTML_ALIGN_STYLE_PREFIX,
)

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

# text (character) automatic styles injected by _py3o_ensure_html_text_styles()
# for _get_html_text_run()'s Bold/Italic/Underline spans -- unlike every
# other style that method injects (headings, colors, font-sizes), these
# three used to be the ONLY ones a source template had to already define
# itself: get_html_text_run() has always referenced them by these exact
# names, but nothing in this file ever guaranteed they existed. A template
# that never happened to define a character style literally named
# "Bold"/"Italic"/"Underline" made LibreOffice silently ignore the
# unresolved text:style-name -- no error, just plain text where bold/
# italic/underline should have rendered. Fixed values (not scanned from
# the record, unlike font sizes), so -- like headings/colors -- they can
# be injected unconditionally on every render.
_HTML_BASIC_INLINE_STYLES = {
    "Bold": {
        ("fo", "font-weight"): "bold",
        ("style", "font-weight-asian"): "bold",
        ("style", "font-weight-complex"): "bold",
    },
    "Italic": {
        ("fo", "font-style"): "italic",
        ("style", "font-style-asian"): "italic",
        ("style", "font-style-complex"): "italic",
    },
    "Underline": {
        ("style", "text-underline-style"): "solid",
        ("style", "text-underline-width"): "auto",
        ("style", "text-underline-color"): "font-color",
    },
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
        plain_sizes, heading_sizes = self._py3o_collect_html_font_sizes(model_instance)
        report_bytes = self._py3o_ensure_html_text_styles(
            report_bytes, plain_sizes, heading_sizes
        )
        report_bytes = self._py3o_ensure_html_align_styles(
            report_bytes, self._py3o_collect_html_paragraph_styles(model_instance)
        )
        self._py3o_check_html_fonts(report_bytes)
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

    def _py3o_collect_html_font_sizes(self, model_instance, max_depth=1):
        """Scan ``model_instance``'s Html fields for inline ``font-size``.

        Font-size styles can't be pre-registered as a fixed/bounded set
        the way the 16 keyword colors are (see
        ``_get_html_text_color_style``): an author can type any pixel
        value in the Odoo Html editor. So instead this scans the actual
        record(s) being printed for every ``font-size`` that occurs in
        any of their Html fields, and one level of ``one2many``/
        ``many2many`` below that (covering a line's own rich-text
        fields, e.g. a worksheet's steps, without hardcoding field
        names -- different worksheet report classes expose different
        Html fields). The result feeds
        ``_py3o_ensure_html_text_styles``, which injects exactly the
        styles this pass found, before py3o ever sees the template.

        Sizes found *inside* a heading (``<h1>``-``<h6>``) are kept
        apart from sizes found outside one: verified empirically
        against this pipeline's actual LibreOffice conversion, a plain
        nested ``text:span`` override for ``fo:font-size`` alone is
        unreliable there depending on the enclosing paragraph's own
        style (it silently loses to the heading's preset size in some
        cases). So a heading override is instead rendered as one
        *combined* style carrying both the override size and the
        heading's ``Bold`` (see ``_get_html_text_run``), which needs
        the pairing, not just the bare point value, to pre-register.

        :param model_instance: record(s) being printed
        :param max_depth: how many ``one2many``/``many2many`` hops to
            follow looking for further Html fields; 0 scans only
            ``model_instance`` itself
        :return: two-tuple ``(plain_sizes, heading_sizes)`` --
            ``plain_sizes`` a set of point sizes found outside any
            heading (see ``_px_or_pt_to_pt``), ``heading_sizes`` a set
            of ``(heading_style_name, pt_value)`` pairs found inside
            one
        :rtype: tuple
        """
        plain_sizes = set()
        heading_sizes = set()
        if not model_instance:
            return plain_sizes, heading_sizes
        for fname, field in model_instance._fields.items():
            if field.type == "html":
                for rec in model_instance:
                    rec_plain, rec_heading = self._get_html_text_font_sizes(
                        getattr(rec, fname)
                    )
                    plain_sizes |= rec_plain
                    heading_sizes |= rec_heading
            elif field.type in ("one2many", "many2many") and max_depth > 0:
                for rec in model_instance:
                    related = getattr(rec, fname)
                    if related:
                        rel_plain, rel_heading = self._py3o_collect_html_font_sizes(
                            related, max_depth - 1
                        )
                        plain_sizes |= rel_plain
                        heading_sizes |= rel_heading
        return plain_sizes, heading_sizes

    def _get_html_text_font_sizes(self, html_value):
        """Split an Html value's ``font-size`` occurrences by heading context.

        :param html_value: raw HTML string from an Odoo ``Html`` field
        :return: two-tuple ``(plain_sizes, heading_sizes)``, same shape
            as ``_py3o_collect_html_font_sizes``'s return value
        :rtype: tuple
        """
        plain_sizes = set()
        heading_sizes = set()
        if not html_value:
            return plain_sizes, heading_sizes
        try:
            root = html.fragment_fromstring(html_value, create_parent="div")
        except etree.ParserError:
            return plain_sizes, heading_sizes

        heading_style_by_el = {}
        for tag, style_name in _HTML_HEADING_STYLE_NAMES.items():
            for heading_el in root.iter(tag):
                for descendant in heading_el.iter():
                    heading_style_by_el[id(descendant)] = style_name

        for el in root.iter():
            match = _FONT_SIZE_RE.search((el.get("style") or "").lower())
            if not match:
                continue
            pt_value = self._px_or_pt_to_pt(float(match.group(1)), match.group(2))
            heading_style = heading_style_by_el.get(id(el))
            if heading_style:
                heading_sizes.add((heading_style, pt_value))
            else:
                plain_sizes.add(pt_value)
        return plain_sizes, heading_sizes

    def _px_or_pt_to_pt(self, value, unit):
        """Convert a CSS ``font-size`` value to ODF points.

        ``1px = 0.75pt`` (96px = 1in = 72pt), matching what a browser
        (and the Odoo Html editor) means by a pixel font size. Rounded
        to the nearest 0.5pt so the style set
        ``_py3o_ensure_html_text_styles`` injects stays small and
        stable rather than growing one entry per near-identical value
        (e.g. 11.8px vs 12px should share a style).

        :param value: numeric part of the CSS ``font-size``
        :param unit: ``"px"`` or ``"pt"``
        :return: point size, rounded to the nearest 0.5
        :rtype: float
        """
        pt = value if unit == "pt" else value * 0.75
        return round(pt * 2) / 2

    def _html_font_size_style_name(self, pt_value):
        """Build the ODF style name for a standalone (non-heading) point size.

        Shared by ``_get_html_text_size_style`` (reading, while
        walking an element) and ``_py3o_ensure_html_text_styles``
        (writing, while injecting styles) so both agree on the name
        for the same point value.

        :param pt_value: point size, as returned by ``_px_or_pt_to_pt``
        :return: an ``OdooFontSize_<n>`` style name
        :rtype: str
        """
        return "OdooFontSize_%s" % ("%g" % pt_value).replace(".", "_")

    def _html_heading_size_style_name(self, heading_style, size_style):
        """Build the combined style name for a heading + size-override pair.

        See ``_get_html_text_run`` for why a run inside a heading with
        its own explicit ``font-size`` gets ONE style carrying both
        properties, rather than two nested spans.

        :param heading_style: the enclosing heading's style name
            (``OdooH1``-``OdooH6``)
        :param size_style: the run's own ``OdooFontSize_<n>`` style
            name (see ``_html_font_size_style_name``)
        :return: a combined ``<heading_style>_<size_style>`` name
        :rtype: str
        """
        return "%s_%s" % (heading_style, size_style)

    def _get_html_text_escape(self, text, symbol_font=False):
        """Escape XML special characters and collapse whitespace.

        Only ASCII whitespace (space, tab, CR, LF, FF) is collapsed:
        U+00A0 runs, such as the gap Word leaves after a list marker,
        are kept. In a Symbol font run, U+00B7 and U+F0B7 become the
        bullet U+2022.

        Also breaks up any unbroken run of ``_LONG_RUN_CHUNK`` or more
        non-whitespace characters (see ``_break_long_runs``) before
        escaping -- done here, ahead of the ``&``/``<``/``>``
        replacements below, so the inserted zero-width spaces never
        land inside a multi-character XML entity like ``&amp;`` and
        split it.

        :param text: raw text extracted from an HTML text node
        :param symbol_font: whether the text sits in a Symbol font
            element (see ``_html_in_symbol_font``)
        :return: text safe to place inside ODF ``text:p`` content
        :rtype: str
        """
        if symbol_font:
            text = text.translate(_HTML_SYMBOL_BULLETS)
        collapsed = _HTML_ASCII_SPACE_RE.sub(" ", text)
        collapsed = self._break_long_runs(collapsed)
        collapsed = collapsed.replace("&", "&amp;")
        collapsed = collapsed.replace("<", "&lt;")
        collapsed = collapsed.replace(">", "&gt;")
        return collapsed

    def _break_long_runs(self, text):
        """Insert zero-width-space wrap points into very long unbroken runs.

        See ``_LONG_RUN_RE``'s comment for why this exists. Every
        ``_LONG_RUN_CHUNK``-character slice of a matched run is joined
        back with ``_ZERO_WIDTH_SPACE`` -- invisible in both the ODT
        and the printed PDF, but a valid word-wrap point, so
        LibreOffice's normal line-breaking handles the run instead of
        falling back to character-level forced wrapping.

        :param text: already whitespace-collapsed, not yet
            XML-escaped (see ``_get_html_text_escape``)
        :return: ``text`` with any long run broken up
        :rtype: str
        """

        def _insert_breaks(match):
            """Re-chunk ``match``'s run, joined back by ``_ZERO_WIDTH_SPACE``."""
            run = match.group(0)
            chunks = [
                run[i : i + _LONG_RUN_CHUNK]
                for i in range(0, len(run), _LONG_RUN_CHUNK)
            ]
            return _ZERO_WIDTH_SPACE.join(chunks)

        return _LONG_RUN_RE.sub(_insert_breaks, text)

    def _get_html_text_style_flags(self, el):
        """Detect Bold/Italic/Underline/color/size flags for one HTML element.

        Combines the semantic tag (``b``/``strong``, ``i``/``em``,
        ``u``) with the ``style`` attribute (``font-weight``,
        ``font-style``, ``text-decoration``, ``color``, ``font-size``)
        so both sources are honoured, matching the fix requested for
        this method. Color also recognizes the legacy
        ``<font color="...">`` attribute.

        :param el: ``lxml.html`` element being inspected
        :return: five-tuple ``(bold, italic, underline, color_style,
            size_style)``, ``color_style``/``size_style`` a
            pre-registered style name (see
            ``_get_html_text_color_style``/``_get_html_text_size_style``)
            or ``None``
        :rtype: tuple
        """
        tag = el.tag if isinstance(el.tag, str) else ""
        bold = tag in _HTML_ODF_BOLD_TAGS
        italic = tag in _HTML_ODF_ITALIC_TAGS
        underline = tag in _HTML_ODF_UNDERLINE_TAGS
        style = (el.get("style") or "").lower()
        if not bold:
            match = re.search(_CSS_PROP_START + r"font-weight\s*:\s*([a-z0-9]+)", style)
            if match:
                value = match.group(1)
                if value in ("bold", "bolder"):
                    bold = True
                elif value.isdigit() and int(value) >= 600:
                    bold = True
        if not italic and re.search(
            _CSS_PROP_START + r"font-style\s*:\s*italic", style
        ):
            italic = True
        if not underline and re.search(
            _CSS_PROP_START + r"text-decoration\s*:\s*underline", style
        ):
            underline = True
        color_style = self._get_html_text_color_style(el, style)
        size_style = self._get_html_text_size_style(style)
        return bold, italic, underline, color_style, size_style

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
        match = re.search(_CSS_PROP_START + r"color\s*:\s*([^;]+)", lowercase_style)
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

    def _get_html_text_size_style(self, lowercase_style):
        """Resolve an inline ``font-size`` in ``lowercase_style`` to a style name.

        Unlike color, the returned name isn't drawn from a fixed set:
        ``_py3o_collect_html_font_sizes`` already scanned the record
        being printed for every ``font-size`` its Html fields use and
        ``_py3o_ensure_html_text_styles`` pre-registered exactly those
        -- so any size this method resolves here is guaranteed to
        already exist in the template's ``automatic-styles`` by the
        time ``_get_html_text`` runs.

        :param lowercase_style: an element's ``style`` attribute,
            already lowercased by the caller
            (``_get_html_text_style_flags``)
        :return: an ``OdooFontSize_<n>`` style name, or ``None``
        :rtype: str or None
        """
        match = _FONT_SIZE_RE.search(lowercase_style)
        if not match:
            return None
        pt_value = self._px_or_pt_to_pt(float(match.group(1)), match.group(2))
        return self._html_font_size_style_name(pt_value)

    def _get_html_text_run(
        self,
        text,
        bold,
        italic,
        underline,
        color_style=None,
        heading_style=None,
        size_style=None,
    ):
        """Wrap escaped text in nested ``text:span`` per active style.

        Nesting order (innermost first): Underline, Italic, Bold,
        color, heading/size -- heading is normally outermost since it
        is a block-level property of the whole paragraph, not a
        per-run one like the others.

        A run both inside a heading *and* carrying its own explicit
        ``font-size`` is the one case this doesn't nest two spans for:
        verified empirically against this pipeline's actual
        LibreOffice conversion, a plain inner ``text:span`` overriding
        an outer one's ``fo:font-size`` alone is unreliable there
        (behaves differently depending on the enclosing paragraph's
        own style, in one observed case losing to the heading's preset
        size regardless of nesting order). So instead a single
        *combined* style (see ``_html_heading_size_style_name``,
        pre-registered by ``_py3o_ensure_html_text_styles``) carries
        both the override size and the heading's ``Bold`` in one
        ``text:span``, sidestepping the cascade question entirely.
        Color still nests normally, since its cascade (inner overrides
        outer) was verified to work as expected.

        :param text: already XML-escaped text
        :param bold: whether the ``Bold`` style applies
        :param italic: whether the ``Italic`` style applies
        :param underline: whether the ``Underline`` style applies
        :param color_style: pre-registered color style name to apply,
            or ``None`` (see ``_get_html_text_color_style``)
        :param heading_style: pre-registered heading style name
            (``OdooH1``-``OdooH6``) inherited from the enclosing
            block, or ``None`` outside a heading
        :param size_style: pre-registered ``OdooFontSize_<n>`` style
            name to apply, or ``None`` (see
            ``_get_html_text_size_style``)
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
        if heading_style and size_style:
            combined_style = self._html_heading_size_style_name(
                heading_style, size_style
            )
            result = '<text:span text:style-name="%s">%s</text:span>' % (
                combined_style,
                result,
            )
        elif heading_style:
            result = '<text:span text:style-name="%s">%s</text:span>' % (
                heading_style,
                result,
            )
        elif size_style:
            result = '<text:span text:style-name="%s">%s</text:span>' % (
                size_style,
                result,
            )
        return result

    def _html_has_symbol_font(self, el):
        """Tell whether ``el``'s own ``font-family`` names a Symbol font.

        :param el: ``lxml.html`` element
        :return: ``True`` if its ``style`` has a ``font-family`` that
            contains ``Symbol``
        :rtype: bool
        """
        match = _HTML_FONT_FAMILY_RE.search((el.get("style") or "").lower())
        return bool(match and "symbol" in match.group(1))

    def _html_in_symbol_font(self, el):
        """Tell whether ``el`` or any of its ancestors is in a Symbol font.

        :param el: ``lxml.html`` element
        :return: ``True`` if ``el`` or an ancestor passes
            ``_html_has_symbol_font``
        :rtype: bool
        """
        return self._html_has_symbol_font(el) or any(
            self._html_has_symbol_font(parent) for parent in el.iterancestors()
        )

    def _get_html_text_inline(
        self,
        el,
        bold,
        italic,
        underline,
        color_style=None,
        heading_style=None,
        size_style=None,
        symbol_font=None,
    ):
        """Serialize one element's content as inline ODF markup.

        Recurses into children, combining each element's own
        Bold/Italic/Underline/color/size with the flags inherited from
        its ancestors -- a nested element's own color/size overrides
        an ancestor's, matching CSS cascade. A ``<br>`` becomes a
        single ``<text:line-break/>``; a tag outside the supported set
        falls back to its plain escaped text instead of raising.

        :param el: ``lxml.html`` element whose content is serialized
        :param bold: Bold flag inherited from ancestors
        :param italic: Italic flag inherited from ancestors
        :param underline: Underline flag inherited from ancestors
        :param color_style: color style name inherited from ancestors,
            or ``None``
        :param heading_style: heading style name from the enclosing
            block (constant through the whole recursion, never
            re-derived per element), or ``None``
        :param size_style: font-size style name inherited from
            ancestors, or ``None`` (see ``_get_html_text_size_style``)
        :param symbol_font: whether ``el`` sits in a Symbol font
            element; ``None`` works it out from ``el`` and its
            ancestors (see ``_html_in_symbol_font``)
        :return: inline ODF markup for ``el``'s text, children and
            their tails
        :rtype: str
        """
        (
            own_bold,
            own_italic,
            own_underline,
            own_color,
            own_size,
        ) = self._get_html_text_style_flags(el)
        bold = bold or own_bold
        italic = italic or own_italic
        underline = underline or own_underline
        color_style = own_color or color_style
        size_style = own_size or size_style
        if symbol_font is None:
            symbol_font = self._html_in_symbol_font(el)

        parts = []
        if el.text:
            parts.append(
                self._get_html_text_run(
                    self._get_html_text_escape(el.text, symbol_font),
                    bold,
                    italic,
                    underline,
                    color_style,
                    heading_style,
                    size_style,
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
                        child,
                        bold,
                        italic,
                        underline,
                        color_style,
                        heading_style,
                        size_style,
                        symbol_font or self._html_has_symbol_font(child),
                    )
                )
            if child.tail:
                parts.append(
                    self._get_html_text_run(
                        self._get_html_text_escape(child.tail, symbol_font),
                        bold,
                        italic,
                        underline,
                        color_style,
                        heading_style,
                        size_style,
                    )
                )
        return "".join(parts)

    def _py3o_ensure_html_table_style(self, report_bytes):
        """Inject the automatic styles ``_get_html_text_table``/``_list`` use.

        ``_get_html_text_table`` references
        ``table:style-name="OdooHtmlTableCell"`` on every cell it
        emits and ``text:style-name="OdooHtmlTableText"`` on every
        cell's ``<text:p>``; ``_get_html_text_list`` references
        ``text:style-name="OdooListItem"`` on every ``<li>``'s own
        ``<text:p>`` and ``text:style-name="OdooOL"``/``"OdooUL"`` on
        the ``<text:list>`` wrapping a run of them. None of the four
        can already exist in the source ODT template -- tables and
        lists are only built at render time, so no template author
        could have created a matching style for any of them. This adds
        all four to the report's ``content.xml``
        ``<office:automatic-styles>`` before py3o ever sees the
        template, using the same "rewrite the ODT zip in memory"
        technique already used by ``_py3o_merge_base_template`` for
        the letterhead. Idempotent per style (a style already present
        by name is left untouched).

        The table paragraph style exists because an unstyled
        ``<text:p>`` falls back to the document's *global default*
        paragraph style -- 12pt in a typical Writer document -- which
        reads as visibly oversized next to the report's own body text
        (usually rendered a good deal smaller through automatic
        per-placeholder character styles, not through any named
        paragraph style this method could instead just point at); the
        ``OdooListItem`` paragraph style exists for the same reason.
        ``OdooOL``/``OdooUL`` (built by ``_py3o_append_list_style``)
        instead give a wrapped continuation line its hanging indent,
        computed by the renderer against the marker it generates --
        see ``_HTML_LIST_ITEM_INDENT``'s comment for why that beats
        this module guessing a fixed indent itself.

        Failure is non-fatal: any malformed/unreadable template is
        returned unchanged rather than raising here, since a missing
        border, oversized cell text, or missing hanging indent is far
        less disruptive than an unprintable report -- the affected
        elements simply render without that refinement in that case
        (same as before this method, or the relevant part of it,
        existed).

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
        if self._find_style_by_name(auto_styles, _HTML_TABLE_CELL_STYLE_NAME) is None:
            style = etree.SubElement(auto_styles, _clark("style", "style"))
            style.set(_clark("style", "name"), _HTML_TABLE_CELL_STYLE_NAME)
            style.set(_clark("style", "family"), "table-cell")
            props = etree.SubElement(style, _clark("style", "table-cell-properties"))
            # Explicit per-side longhand, not the fo:border shorthand: some
            # ODF consumers render the shorthand inconsistently on table
            # cells, and the source HTML this method exists to approximate
            # already uses the same per-side form (border-top/-bottom/-left/
            # -right).
            for side in ("top", "bottom", "left", "right"):
                props.set(_clark("fo", "border-%s" % side), _HTML_TABLE_CELL_BORDER)
            props.set(_clark("fo", "padding"), "0.05in")
            changed = True

        if self._find_style_by_name(auto_styles, _HTML_TABLE_TEXT_STYLE_NAME) is None:
            style = etree.SubElement(auto_styles, _clark("style", "style"))
            style.set(_clark("style", "name"), _HTML_TABLE_TEXT_STYLE_NAME)
            style.set(_clark("style", "family"), "paragraph")
            props = etree.SubElement(style, _clark("style", "text-properties"))
            props.set(_clark("fo", "font-size"), _HTML_TABLE_TEXT_FONT_SIZE)
            changed = True

        if self._find_style_by_name(auto_styles, _HTML_LIST_ITEM_STYLE_NAME) is None:
            style = etree.SubElement(auto_styles, _clark("style", "style"))
            style.set(_clark("style", "name"), _HTML_LIST_ITEM_STYLE_NAME)
            style.set(_clark("style", "family"), "paragraph")
            # Same reasoning as OdooHtmlTableText above: this is a brand new
            # paragraph, not the placeholder's own, so it needs its own
            # explicit font-size or it falls back to the oversized document
            # default. No margin-left/text-indent here (unlike
            # OdooHtmlTableText) -- the hang indent comes from the
            # OdooOL/OdooUL list styles below instead, computed against the
            # marker they generate rather than guessed.
            text_props = etree.SubElement(style, _clark("style", "text-properties"))
            text_props.set(_clark("fo", "font-size"), _HTML_TABLE_TEXT_FONT_SIZE)
            changed = True

        if self._find_style_by_name(auto_styles, _HTML_LIST_OL_STYLE_NAME) is None:
            self._py3o_append_list_style(
                auto_styles,
                _HTML_LIST_OL_STYLE_NAME,
                "number",
                {
                    _clark("style", "num-format"): "1",
                    _clark("style", "num-suffix"): ". ",
                },
            )
            changed = True

        if self._find_style_by_name(auto_styles, _HTML_LIST_UL_STYLE_NAME) is None:
            self._py3o_append_list_style(
                auto_styles,
                _HTML_LIST_UL_STYLE_NAME,
                "bullet",
                {_clark("text", "bullet-char"): "•"},
            )
            changed = True

        if not changed:
            return report_bytes

        new_zip_entries = {
            "content.xml": etree.tostring(
                content_root, xml_declaration=True, encoding="UTF-8"
            )
        }
        return self._py3o_write_merged_zip(report_zip_in, new_zip_entries)

    def _py3o_append_list_style(self, auto_styles, style_name, level_kind, level_attrs):
        """Append one single-level ``<text:list-style>`` to ``auto_styles``.

        Builds the ``OdooOL``/``OdooUL`` styles ``_py3o_ensure_html_table_style``
        injects and ``_get_html_text_list`` references: one
        ``<text:list-level-style-number>`` or ``<text:list-level-style-bullet>``
        (picked by ``level_kind``, ``"number"``/``"bullet"``), carrying
        ``level_attrs`` (the number format/suffix, or the bullet character)
        plus a shared ``style:list-level-label-alignment`` -- this is what
        lets the renderer itself compute a wrapped continuation line's hang
        indent against the marker it generates, rather than this module
        guessing a fixed one (see ``_HTML_LIST_ITEM_INDENT``'s comment).
        HTML only nests to a second ``<ul>``/``<ol>`` level via a fresh,
        independent ``_get_html_text_list`` call (see its docstring -- a
        nested list under a non-``<li>`` child flushes and is walked as its
        own block), so a single level here already covers every list this
        module renders.

        :param auto_styles: the template's ``<office:automatic-styles>``
            element, mutated in place
        :param style_name: ``OdooOL`` or ``OdooUL``
        :param level_kind: ``"number"`` or ``"bullet"`` -- which
            ``text:list-level-style-*`` element to create
        :param level_attrs: attributes (already Clark-notation keys, see
            ``_clark``) to set on that level element
        :return: None
        """
        list_style = etree.SubElement(auto_styles, _clark("text", "list-style"))
        list_style.set(_clark("style", "name"), style_name)
        level = etree.SubElement(
            list_style, _clark("text", "list-level-style-%s" % level_kind)
        )
        level.set(_clark("text", "level"), "1")
        for attr, value in level_attrs.items():
            level.set(attr, value)
        level_props = etree.SubElement(level, _clark("style", "list-level-properties"))
        level_props.set(
            _clark("text", "list-level-position-and-space-mode"), "label-alignment"
        )
        alignment = etree.SubElement(
            level_props, _clark("style", "list-level-label-alignment")
        )
        alignment.set(_clark("text", "label-followed-by"), "listtab")
        alignment.set(_clark("text", "list-tab-stop-position"), _HTML_LIST_ITEM_INDENT)
        alignment.set(_clark("fo", "text-indent"), "-%s" % _HTML_LIST_MARKER_OVERHANG)
        alignment.set(_clark("fo", "margin-left"), _HTML_LIST_ITEM_INDENT)

    def _py3o_ensure_html_text_styles(
        self, report_bytes, plain_sizes=(), heading_sizes=()
    ):
        """Inject the Bold/Italic/Underline/heading/color/font-size styles.

        Same constraint and technique as
        ``_py3o_ensure_html_table_style`` (see its docstring): none of
        the ``Bold``/``Italic``/``Underline`` character styles, an
        ``<h1>``-``<h6>`` font-size style, a ``style="color: ..."``
        color style, a standalone inline ``font-size`` style, or a
        combined heading+size style can be relied on to already exist
        in the source template, so all five are added to
        ``content.xml``'s ``<office:automatic-styles>`` here, before
        py3o ever sees the template. Idempotent per style (a style
        already present by name is left untouched); unlike the
        table-cell style this injects up to
        ``25 + len(plain_sizes) + len(heading_sizes)`` styles (3 basic
        inline styles + 6 headings + 16 colors + one per size
        ``_py3o_collect_html_font_sizes`` found, standalone or
        heading-paired) in one pass, skipping only individual names
        that already exist.

        Colors are deliberately bounded to the 16 standard CSS2
        keywords in ``_HTML_COLOR_KEYWORDS`` -- see
        ``_get_html_text_color_style`` for why an arbitrary/unlisted
        color cannot be supported this way. Font sizes can't be
        bounded the same way (an author can type any pixel value), so
        ``plain_sizes``/``heading_sizes`` instead carry exactly what
        ``_py3o_collect_html_font_sizes`` found by scanning the record
        actually being printed. The heading-paired styles set BOTH
        ``fo:font-size`` and ``fo:font-weight="bold"`` in one style --
        see ``_get_html_text_run`` for why a heading override can't
        just nest two separate styles the way color does.

        Failure is non-fatal, same reasoning as the table-cell style:
        a malformed/unreadable template is returned unchanged rather
        than raising, since missing heading sizes/colors/font-sizes
        are far less disruptive than an unprintable report.

        :param report_bytes: raw ODT template bytes, before py3o's
            own base-template (letterhead) merge and rendering
        :param plain_sizes: standalone (non-heading) point sizes to
            pre-register (see
            ``_py3o_collect_html_font_sizes``/``_px_or_pt_to_pt``)
        :param heading_sizes: ``(heading_style_name, pt_value)`` pairs
            to pre-register as combined styles (see
            ``_py3o_collect_html_font_sizes``)
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
        for style_name, props_map in _HTML_BASIC_INLINE_STYLES.items():
            if self._find_style_by_name(auto_styles, style_name) is not None:
                continue
            style = etree.SubElement(auto_styles, _clark("style", "style"))
            style.set(_clark("style", "name"), style_name)
            style.set(_clark("style", "family"), "text")
            props = etree.SubElement(style, _clark("style", "text-properties"))
            for (prefix, attr), value in props_map.items():
                props.set(_clark(prefix, attr), value)
            changed = True

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

        for pt_value in plain_sizes:
            style_name = self._html_font_size_style_name(pt_value)
            if self._find_style_by_name(auto_styles, style_name) is not None:
                continue
            style = etree.SubElement(auto_styles, _clark("style", "style"))
            style.set(_clark("style", "name"), style_name)
            style.set(_clark("style", "family"), "text")
            props = etree.SubElement(style, _clark("style", "text-properties"))
            props.set(_clark("fo", "font-size"), "%spt" % ("%g" % pt_value))
            changed = True

        for heading_style, pt_value in heading_sizes:
            size_style = self._html_font_size_style_name(pt_value)
            style_name = self._html_heading_size_style_name(heading_style, size_style)
            if self._find_style_by_name(auto_styles, style_name) is not None:
                continue
            style = etree.SubElement(auto_styles, _clark("style", "style"))
            style.set(_clark("style", "name"), style_name)
            style.set(_clark("style", "family"), "text")
            props = etree.SubElement(style, _clark("style", "text-properties"))
            props.set(_clark("fo", "font-size"), "%spt" % ("%g" % pt_value))
            props.set(_clark("fo", "font-weight"), "bold")
            changed = True

        if not changed:
            return report_bytes

        new_zip_entries = {
            "content.xml": etree.tostring(
                content_root, xml_declaration=True, encoding="UTF-8"
            )
        }
        return self._py3o_write_merged_zip(report_zip_in, new_zip_entries)

    def _html_escape_suffix(self):
        """Return the markup that reopens a paragraph after an escaped block.

        With a wrapper style (``py3o_html_base_style`` context key) the
        reopened paragraph carries it as ``text:style-name``, so text
        following a list, table or aligned paragraph keeps the
        placeholder paragraph's font and size. Without one, the
        unstyled ``_ESCAPE_BLOCK_SUFFIX`` is returned.

        :return: ``<text:p ...><text:span>`` markup
        :rtype: str
        """
        base_style = self.env.context.get("py3o_html_base_style")
        if not base_style:
            return _ESCAPE_BLOCK_SUFFIX
        return '<text:p text:style-name="%s"><text:span>' % base_style

    def _html_list_item_style_name(self, base_style=None):
        """Return the paragraph style name used by a list item.

        :param base_style: name of the paragraph style that wraps the
            template placeholder, or ``None``/empty for the fixed
            fallback style
        :return: style name, e.g. ``OdooListItem_P7``
        :rtype: str
        """
        if not base_style:
            return _HTML_LIST_ITEM_STYLE_NAME
        return _HTML_LIST_ITEM_VARIANT_FORMAT % (
            _HTML_LIST_ITEM_STYLE_NAME,
            base_style,
        )

    def _py3o_html_list_item_style_element(self, auto_styles, base_style):
        """Build the list item paragraph style for one wrapper style.

        Only the text properties of the wrapper paragraph (font, size
        and their asian/complex counterparts) are carried over.
        Margins, alignment and page breaks are left out, so item
        spacing and indentation stay with the list style. An automatic
        ``base_style`` has its text properties copied (and keeps its
        own parent); a common one becomes the parent instead.

        :param auto_styles: the ``office:automatic-styles`` element
        :param base_style: wrapper paragraph style name
        :return: the new, not yet attached ``style:style`` element
        :rtype: lxml element
        """
        style = etree.Element(_clark("style", "style"))
        style.set(_clark("style", "name"), self._html_list_item_style_name(base_style))
        style.set(_clark("style", "family"), "paragraph")
        source = self._find_style_by_name(auto_styles, base_style)
        if source is None:
            style.set(_clark("style", "parent-style-name"), base_style)
            return style
        parent = source.get(_clark("style", "parent-style-name"))
        if parent:
            style.set(_clark("style", "parent-style-name"), parent)
        text_props = source.find(_clark("style", "text-properties"))
        if text_props is not None:
            style.append(deepcopy(text_props))
        return style

    def _html_margin_name_suffix(self, margins):
        """Return the style name part that encodes a margin tuple.

        :param margins: ``(top, bottom, left, right, text_indent)`` in
            points, each ``None`` when not given
        :return: e.g. ``mt6_ml16_5_tin14_5`` (``.`` becomes ``_`` and a
            minus sign ``n``)
        :rtype: str
        """
        parts = []
        for key, value in zip(_HTML_MARGIN_NAME_KEYS, margins):
            if value is not None:
                text = ("%s%g" % (key, value)).replace(".", "_")
                parts.append(text.replace("-", "n"))
        return "_".join(parts)

    def _html_align_style_name(self, odf_align, base_style=None, margins=None):
        """Return the automatic paragraph style name for a paragraph.

        :param odf_align: one of ``_HTML_ALIGN_ODF_KEYS``, or ``None``
            for a paragraph that only carries margins
        :param base_style: name of the paragraph style that wraps the
            template placeholder, or ``None``/empty for the generic
            style
        :param margins: margin tuple from
            ``_get_html_text_paragraph_margins``, or ``None``
        :return: style name, e.g. ``OdooHtmlAlignJustify_P7``
        :rtype: str
        """
        name = "%s%s" % (
            _HTML_ALIGN_STYLE_PREFIX,
            odf_align.capitalize() if odf_align else "Inherit",
        )
        if margins:
            name = "%s_%s" % (name, self._html_margin_name_suffix(margins))
        if base_style:
            name = "%s_%s" % (name, base_style)
        return name

    def _py3o_html_align_style_element(
        self, auto_styles, odf_align, base_style, margins=None
    ):
        """Build the automatic paragraph style for one paragraph kind.

        With ``base_style`` the new style takes over that wrapper
        paragraph's own look (font, size, margins) and only overrides
        ``fo:text-align`` and the given margins: an automatic style
        cannot be the parent of another automatic style, so the
        properties of an automatic ``base_style`` are copied; a common
        ``base_style`` (not found among the automatic styles) becomes
        the parent instead. Page break attributes are dropped from a
        copy, since each emitted paragraph is only a slice of the
        placeholder's content.

        :param auto_styles: the ``office:automatic-styles`` element
        :param odf_align: one of ``_HTML_ALIGN_ODF_KEYS``, or ``None``
        :param base_style: wrapper paragraph style name, or ``None``
        :param margins: ``(top, bottom, left, right, text_indent)`` in
            points, each ``None`` to keep the base value, or ``None``
        :return: the new, not yet attached ``style:style`` element
        :rtype: lxml element
        """
        source = self._find_style_by_name(auto_styles, base_style)
        if source is not None:
            style = deepcopy(source)
            style.attrib.pop(_clark("style", "master-page-name"), None)
        else:
            style = etree.Element(_clark("style", "style"))
            style.set(_clark("style", "family"), "paragraph")
            if base_style:
                style.set(_clark("style", "parent-style-name"), base_style)
        style.set(
            _clark("style", "name"),
            self._html_align_style_name(odf_align, base_style, margins),
        )
        props = style.find(_clark("style", "paragraph-properties"))
        if props is None:
            props = etree.SubElement(style, _clark("style", "paragraph-properties"))
        for attr in ("break-before", "break-after"):
            props.attrib.pop(_clark("fo", attr), None)
        if odf_align:
            props.set(_clark("fo", "text-align"), odf_align)
        for attr, value in zip(_HTML_MARGIN_PROPS, margins or ()):
            if value is not None:
                props.set(_clark("fo", attr), "%gpt" % value)
        return style

    def _py3o_add_html_tiny_style(self, auto_styles):
        """Add the 1pt text style used by ``_HTML_TINY_SPAN`` if missing.

        :param auto_styles: the ``office:automatic-styles`` element
        :return: None
        """
        if self._find_style_by_name(auto_styles, _HTML_TINY_STYLE_NAME) is not None:
            return
        tiny = etree.SubElement(auto_styles, _clark("style", "style"))
        tiny.set(_clark("style", "name"), _HTML_TINY_STYLE_NAME)
        tiny.set(_clark("style", "family"), "text")
        tiny_props = etree.SubElement(tiny, _clark("style", "text-properties"))
        for prefix, attr in (
            ("fo", "font-size"),
            ("style", "font-size-asian"),
            ("style", "font-size-complex"),
        ):
            tiny_props.set(_clark(prefix, attr), "1pt")

    def _py3o_ensure_html_align_styles(self, report_bytes, para_styles=()):
        """Inject the paragraph-alignment styles ``get_html_text`` needs.

        Only a template that has at least one ``get_html_text(...)``
        placeholder is touched; any other template is returned
        byte-for-byte. For each such placeholder this finds the
        paragraph it sits in, adds ``base_style='<that paragraph's
        style>'`` to the call so ``_get_html_text`` can pick styles
        that keep the paragraph's font and margins (see
        ``_py3o_html_align_style_element``), and registers one style
        per alignment for every distinct wrapper style, plus the four
        generic alignment-only styles used when no wrapper style is
        known. Idempotent per style name, and a placeholder that
        already passes ``base_style`` is left alone.

        Failure is non-fatal, like the other ``_py3o_ensure_*``
        methods: an unreadable template is returned unchanged.

        The 1pt text style ``_HTML_TINY_STYLE_NAME`` is registered too,
        on every template that has such a placeholder.

        Each ``(odf_align, margins)`` pair of ``para_styles`` (see
        ``_py3o_collect_html_paragraph_styles``) is registered the same
        way, once per wrapper style and for the generic case.

        :param report_bytes: raw ODT template bytes
        :param para_styles: ``(odf_align, margins)`` pairs of the
            paragraphs with margins that the record being printed uses
        :return: ``report_bytes``, with the alignment styles added
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

        description_attr = _clark("text", "description")
        style_name_attr = _clark("text", "style-name")
        paragraph_tags = (_clark("text", "p"), _clark("text", "h"))
        base_styles = set()
        found = False
        for text_input in content_root.iter(_clark("text", "text-input")):
            match = _HTML_TEXT_DESCRIPTION_RE.match(
                (text_input.get(description_attr) or "").strip()
            )
            if not match:
                continue
            found = True
            if "base_style" in match.group(2):
                continue
            wrapper = text_input.getparent()
            while wrapper is not None and wrapper.tag not in paragraph_tags:
                wrapper = wrapper.getparent()
            base_style = wrapper.get(style_name_attr) if wrapper is not None else None
            if not base_style:
                continue
            base_styles.add(base_style)
            text_input.set(
                description_attr,
                "%s%s, base_style='%s'%s"
                % (match.group(1), match.group(2), base_style, match.group(3)),
            )
        if not found:
            return report_bytes

        for base_style in sorted(base_styles):
            name = self._html_list_item_style_name(base_style)
            if self._find_style_by_name(auto_styles, name) is None:
                auto_styles.append(
                    self._py3o_html_list_item_style_element(auto_styles, base_style)
                )
        self._py3o_add_html_tiny_style(auto_styles)
        combos = [(odf_align, None) for odf_align in _HTML_ALIGN_ODF_KEYS]
        combos.extend(sorted(para_styles, key=repr))
        for base_style in [None] + sorted(base_styles):
            for odf_align, margins in combos:
                name = self._html_align_style_name(odf_align, base_style, margins)
                if self._find_style_by_name(auto_styles, name) is not None:
                    continue
                auto_styles.append(
                    self._py3o_html_align_style_element(
                        auto_styles, odf_align, base_style, margins
                    )
                )

        new_zip_entries = {
            "content.xml": etree.tostring(
                content_root, xml_declaration=True, encoding="UTF-8"
            )
        }
        return self._py3o_write_merged_zip(report_zip_in, new_zip_entries)

    def _py3o_check_html_fonts(self, report_bytes):
        """Warn about any font-face this template declares but the server lacks.

        ``get_html_text``'s automatic styles (headings,
        ``_HTML_BASIC_INLINE_STYLES``, colors, font-sizes) never set
        ``fo:font-name`` -- the actual typeface printed always comes
        from whatever the template's OWN paragraph/character styles
        declare (see ``_get_html_text_run``'s docstring). A font a
        template's ``office:font-face-decls`` names but this machine
        doesn't have installed renders as whatever LibreOffice's
        fontconfig substitution picks instead, with no error and no
        visual cue in the generated PDF that anything was substituted.
        This is exactly how ``Report RR GX5 Font 8.odt`` (AURA-SWR)'s
        "Metropolis" -- applied by nearly every automatic paragraph
        style in that template, with no ``style:font-family-generic``
        fallback declared -- went unnoticed locally (silently falling
        back to Liberation Sans) until compared against the aura-swr
        server, where the font actually was installed (20 Sep 2026).

        Read-only and non-fatal by design, the same posture as every
        other ``_py3o_ensure_*``/``_get_html_text_*`` method in this
        file: a substituted font degrades the printed look, it doesn't
        make the report unprintable, so this only logs one warning per
        missing font -- it never raises and never blocks rendering.
        Requires ``fc-match`` (the ``fontconfig`` package); silently
        does nothing if that binary isn't on ``PATH`` or a call to it
        errors, rather than failing a report render over a diagnostic
        feature being unavailable.

        :param report_bytes: raw ODT template bytes, before py3o's
            own base-template (letterhead) merge and rendering
        :return: nothing; only logs a warning per missing font found
        :rtype: None
        """
        try:
            report_zip = ZipFile(BytesIO(report_bytes))
        except BadZipFile:
            return

        font_names = set()
        for member in ("content.xml", "styles.xml"):
            try:
                root = etree.fromstring(report_zip.read(member))
            except (KeyError, etree.XMLSyntaxError):
                continue
            for face in root.iter(_clark("style", "font-face")):
                family = face.get(_clark("svg", "font-family"))
                if family:
                    font_names.add(family.strip("'"))
        if not font_names:
            return

        for font_name in sorted(font_names):
            try:
                result = subprocess.run(
                    ["fc-match", "--format=%{family}", font_name],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True,
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired):
                # fc-match missing/misbehaving on this machine -- a
                # diagnostic warning is not worth failing report
                # generation over, and every other font name would
                # fail the exact same way, so stop checking entirely.
                return
            resolved = result.stdout.strip()
            if resolved and resolved != font_name:
                logger.warning(
                    "Py3o report template references font %r, which is "
                    "not installed on this server -- LibreOffice will "
                    "silently substitute %r instead.",
                    font_name,
                    resolved,
                )

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

        Cells reference the ``OdooHtmlTableCell`` style (thin border +
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
                    '<text:p text:style-name="%s">%s</text:p></table:table-cell>'
                    % (_HTML_TABLE_CELL_STYLE_NAME, _HTML_TABLE_TEXT_STYLE_NAME, text)
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
        return "%s%s%s" % (
            _ESCAPE_BLOCK_PREFIX,
            "".join(parts),
            self._html_escape_suffix(),
        )

    def _get_html_text_list(self, tag, el, blocks):
        """Render one ``<ul>``/``<ol>``'s ``<li>`` children as one block.

        Split out of ``_get_html_text_blocks`` (see its docstring)
        purely to keep that method's branching under the repo's
        flake8 ``max-complexity``.

        Every ``<li>`` becomes a genuine ``<text:list-item>`` (its
        marker -- a bullet for ``ul``, an auto-incrementing number for
        ``ol`` -- generated by the renderer from the ``OdooUL``/
        ``OdooOL`` list style, not written into the text here) holding
        its own ``<text:p>`` styled ``OdooListItem``; a run of
        consecutive ``<li>``\\ s becomes one shared ``<text:list>``.
        That list is batched into the same close-current-paragraph/
        emit/reopen escape ``_get_html_text_table`` already uses for
        ``<table>`` (batched per run of ``<li>``\\ s, not one escape
        per item, so joining blocks with ``<text:line-break/>`` -- see
        ``_get_html_text`` -- does not inject a stray blank paragraph
        between every pair of items). A non-``<li>`` child (e.g. a
        nested ``<ul>`` directly under this one, not wrapped in an
        ``<li>`` -- malformed HTML, but seen from copy-pasted content)
        flushes the batch so far and is walked back through
        ``_get_html_text_blocks`` instead, to keep document order
        correct.

        :param tag: ``"ul"`` or ``"ol"``, the list's own tag
        :param el: the ``<ul>``/``<ol>`` element being walked
        :param blocks: list mutated in place, one entry per batch of
            consecutive ``<li>``\\ s or per non-``<li>`` child
        :return: None
        """
        items = []
        item_style = self._html_list_item_style_name(
            self.env.context.get("py3o_html_base_style")
        )

        def _flush():
            """Emit the accumulated ``items`` as one ``<text:list>`` block."""
            if items:
                blocks.append(
                    '%s<text:list text:style-name="%s">%s</text:list>%s'
                    % (
                        _ESCAPE_BLOCK_PREFIX,
                        _HTML_LIST_STYLE_NAMES[tag],
                        "".join(items),
                        self._html_escape_suffix(),
                    )
                )
                items.clear()

        for child in el:
            if not isinstance(child.tag, str):
                continue
            if child.tag == "li":
                content = self._get_html_text_inline(child, False, False, False)
                items.append(
                    "<text:list-item>"
                    '<text:p text:style-name="%s">%s</text:p>'
                    "</text:list-item>" % (item_style, content)
                )
            else:
                _flush()
                self._get_html_text_blocks(child, blocks)
        _flush()

    def _get_html_text_blocks(self, el, blocks):
        """Collect one ODF block string per block-level HTML element.

        ``p``/``h1``-``h6`` each become one entry of ``blocks``; ``hr``
        contributes an empty entry (rendered as a blank separator line
        by the caller); ``ul``/``ol`` are handled by
        ``_get_html_text_list``, which batches each run of consecutive
        ``<li>``\\ s (prefixed with a bullet for ``ul``, a number for
        ``ol``) into one entry of its own, each ``<li>`` its own
        hanging-indented ``<text:p>``; ``table`` becomes one entry
        rendered by ``_get_html_text_table``. Bare text/inline content
        with no wrapping block tag is kept in a single shared entry.

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
        if self._get_html_text_aligned_block(el, tag, blocks):
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

    def _html_css_length_to_pt(self, token):
        """Convert one CSS length to points, rounded to 0.5pt.

        :param token: e.g. ``16.55pt``, ``-0.2in``, ``0``
        :return: points, or ``None`` for a non-numeric value
            (``auto``, ``normal``), an unknown unit, or a number
            without a unit other than zero
        :rtype: float or None
        """
        match = _HTML_LENGTH_RE.match(token.strip())
        if not match:
            return None
        number = float(match.group(1))
        unit = match.group(2)
        if unit is None:
            return 0.0 if number == 0 else None
        return round(number * _HTML_LENGTH_TO_PT[unit] * 2) / 2 + 0.0

    def _html_margin_shorthand(self, value):
        """Expand a CSS ``margin`` shorthand into per-side points.

        :param value: the declaration value, 1 to 4 lengths
        :return: ``{"margin-top": pt, ...}`` for the sides that
            resolve to a length
        :rtype: dict
        """
        tokens = value.replace("!important", "").split()
        indexes = _HTML_MARGIN_SHORTHAND.get(len(tokens))
        result = {}
        for side, index in zip(_HTML_MARGIN_SHORTHAND_SIDES, indexes or ()):
            points = self._html_css_length_to_pt(tokens[index])
            if points is not None:
                result[side] = points
        return result

    def _get_html_text_paragraph_margins(self, el):
        """Read the margins and text indent of a ``<p>``.

        Reads ``margin-top``, ``margin-bottom``, ``margin-left``,
        ``margin-right``, ``text-indent`` and the ``margin`` shorthand
        from ``el``'s own ``style``, later declarations winning.

        :param el: ``lxml.html`` element
        :return: ``(top, bottom, left, right, text_indent)`` in points,
            each ``None`` when not given, or ``None`` when ``el`` is
            not a ``p``, gives none of them, or gives only zeros (a
            paragraph with no spacing is a plain paragraph, joined by a
            line break like any other). A zero is kept when another
            value is not zero.
        :rtype: tuple or None
        """
        if el.tag != "p":
            return None
        values = {}
        for declaration in (el.get("style") or "").lower().split(";"):
            name, separator, value = declaration.partition(":")
            name = name.strip()
            if not separator:
                continue
            if name == "margin":
                values.update(self._html_margin_shorthand(value))
            elif name in _HTML_MARGIN_PROPS:
                points = self._html_css_length_to_pt(value.replace("!important", ""))
                if points is not None:
                    values[name] = points
        if not any(values.values()):
            return None
        return tuple(values.get(name) for name in _HTML_MARGIN_PROPS)

    def _get_html_text_paragraph_styles(self, html_value):
        """List the paragraph styles an Html value needs for its margins.

        :param html_value: raw HTML string from an Odoo ``Html`` field
        :return: set of ``(odf_align, margins)`` pairs, one per
            ``p`` that carries margins or a text indent
        :rtype: set
        """
        combos = set()
        if not html_value:
            return combos
        try:
            root = html.fragment_fromstring(html_value, create_parent="div")
        except etree.ParserError:
            return combos
        for el in root.iter("p"):
            margins = self._get_html_text_paragraph_margins(el)
            if margins is not None:
                combos.add((self._get_html_text_align(el), margins))
        return combos

    def _py3o_collect_html_paragraph_styles(self, model_instance, max_depth=1):
        """Scan Html fields for the margin paragraph styles to register.

        Same traversal as ``_py3o_collect_html_font_sizes``: the Html
        fields of ``model_instance`` and one level of ``one2many``/
        ``many2many`` below them.

        :param model_instance: record(s) being printed
        :param max_depth: how many relation hops to follow
        :return: set of ``(odf_align, margins)`` pairs
        :rtype: set
        """
        combos = set()
        if not model_instance:
            return combos
        for fname, field in model_instance._fields.items():
            if field.type == "html":
                for rec in model_instance:
                    combos |= self._get_html_text_paragraph_styles(getattr(rec, fname))
            elif field.type in ("one2many", "many2many") and max_depth > 0:
                for rec in model_instance:
                    related = getattr(rec, fname)
                    if related:
                        combos |= self._py3o_collect_html_paragraph_styles(
                            related, max_depth - 1
                        )
        return combos

    def _get_html_text_align(self, el):
        """Return the ODF alignment an HTML block asks for, if any.

        Read from the element's own ``style="text-align: ..."``, else
        from its legacy ``align`` attribute (what a paste from Word
        leaves behind); ``style`` wins when both are present. A value
        outside ``_HTML_ALIGN_VALUES`` counts as no alignment.

        :param el: ``lxml.html`` element, a ``p``/``h1``-``h6``
        :return: one of ``_HTML_ALIGN_ODF_KEYS``, or ``None``
        :rtype: str or None
        """
        match = _HTML_ALIGN_STYLE_RE.search(el.get("style") or "")
        value = match.group(1) if match else (el.get("align") or "")
        return _HTML_ALIGN_VALUES.get(value.strip().lower())

    def _get_html_text_aligned_block(self, el, tag, blocks):
        """Append an aligned or spaced ``p``/heading as paragraphs of its own.

        Does nothing, and returns ``False``, unless ``el`` is a
        ``p``/``h1``-``h6`` that asks for an alignment (see
        ``_get_html_text_align``) or a ``p`` that carries margins or
        a text indent (see ``_get_html_text_paragraph_margins``), so
        every other element keeps its existing rendering.

        The block is cut at each of its own top-level ``<br>`` so a
        title joined to its body by ``<br>`` becomes two paragraphs
        instead of one justified line ending in a manual line break
        (which LibreOffice stretches across the full width). Each
        non-empty piece is emitted through the same close/reopen
        escape ``_get_html_text_table`` uses, as a ``<text:p>`` whose
        style comes from ``_html_align_style_name`` -- built on the
        wrapper paragraph's style when the template placeholder passed
        one (``base_style`` context key), so font and margins match
        the surrounding text.

        :param el: ``lxml.html`` element being walked
        :param tag: the element's tag name
        :param blocks: list mutated in place, one entry per paragraph
        :return: ``True`` if ``el`` was handled here
        :rtype: bool
        """
        if tag not in _HTML_ALIGN_BLOCK_TAGS:
            return False
        odf_align = self._get_html_text_align(el)
        margins = self._get_html_text_paragraph_margins(el)
        if not odf_align and margins is None:
            return False
        heading_style = _HTML_HEADING_STYLE_NAMES.get(tag)
        style_name = self._html_align_style_name(
            odf_align, self.env.context.get("py3o_html_base_style"), margins
        )
        symbol_font = self._html_in_symbol_font(el)
        segment = el.makeelement(el.tag, dict(el.attrib))
        segment.text = el.text
        segments = []
        for child in el:
            if isinstance(child.tag, str) and child.tag == "br":
                segments.append(segment)
                segment = el.makeelement(el.tag, dict(el.attrib))
                segment.text = child.tail
            else:
                segment.append(deepcopy(child))
        segments.append(segment)
        for piece in segments:
            content = self._get_html_text_inline(
                piece,
                False,
                False,
                False,
                heading_style=heading_style,
                symbol_font=symbol_font,
            )
            if _ODF_TAG_RE.sub("", content).strip():
                blocks.append(
                    '%s<text:p text:style-name="%s">%s</text:p>%s'
                    % (
                        _ESCAPE_BLOCK_PREFIX,
                        style_name,
                        content,
                        self._html_escape_suffix(),
                    )
                )
        return True

    @api.model
    def _get_html_text(self, html_value, base_style=None):
        """Convert an ``Html`` field value into ODF markup.

        Meant to replace a ``text:input`` already nested inside an
        existing ``<text:p>`` in a py3o ODT template (see the
        ``get_html_text`` entry registered by ``_get_parser_context``).
        Supports ``p``, ``br``, ``h1``-``h6``, ``ul``/``ol``/``li``,
        ``span``/``font``, ``hr`` and ``table``; bold/italic/underline
        are detected from both the semantic tag (``b``/``strong``,
        ``i``/``em``, ``u``) and the ``style`` attribute. Tags outside
        that set fall back to plain text instead of raising.

        Most constructs stay entirely inside that one ``<text:p>``
        (never open a new one). ``table`` and a batch of ``<li>``\\ s
        (see ``_get_html_text_list``) are the exceptions: each closes
        that paragraph, emits real ``<table:table>``/hang-indented
        ``<text:p>`` elements, and reopens an empty paragraph for
        whatever follows -- see ``_get_html_text_table`` for why that
        is safe here. Such a block is recognizable by starting with
        ``</text:span></text:p>`` and/or ending with
        ``<text:p><text:span>``.

        Ordinary blocks are joined by a single ``<text:line-break/>``
        -- one new line per source paragraph, matching normal
        paragraph flow. A deliberate blank line between two paragraphs
        is therefore carried entirely by the source's own
        ``<p><br></p>`` (rendered as its own block, see
        ``_get_html_text_blocks``), not added again here. A join
        touching a table/list block skips that
        ``<text:line-break/>`` instead: that block already opens and
        closes its own paragraph(s) cleanly, and joining it the
        ordinary way would strand a line-break either right before a
        ``</text:p>`` or right after a ``<text:p>`` -- both render as
        an unwanted extra blank line.

        A ``p``/``h1``-``h6`` carrying ``text-align`` (or ``align``) of
        left, center, right or justify is emitted as paragraph(s) of
        its own with that alignment -- see
        ``_get_html_text_aligned_block``. HTML without it renders as
        before.

        :param html_value: raw HTML string from an Odoo ``Html`` field
        :param base_style: name of the paragraph style wrapping the
            template placeholder; added to the call by
            ``_py3o_ensure_html_align_styles`` so aligned paragraphs
            keep that paragraph's font and margins. ``None`` selects
            the generic alignment-only styles.
        :return: markup safe to insert as-is in the ODT template
        :rtype: genshi.core.Markup
        """
        if not html_value or not html_value.strip():
            return Markup("")
        engine = self
        if base_style:
            engine = self.with_context(py3o_html_base_style=base_style)
        root = html.fragment_fromstring(html_value, create_parent="div")
        blocks = []
        engine._get_html_text_blocks(root, blocks)
        return Markup(engine._join_html_text_blocks(blocks))

    def _join_html_text_blocks(self, blocks):
        """Join ``blocks`` with ``<text:line-break/>``, skipping escaped joins.

        See ``_get_html_text``'s docstring for why a join next to a
        table/list block (one starting with ``</text:span></text:p>``
        or ending with ``<text:p><text:span>``) must not add a
        ``<text:line-break/>``. Two escaped blocks in a row (list,
        table, aligned or margined paragraph) are merged into adjacent
        blocks: the first block's reopen marker and the second block's
        close marker are both dropped, since joining them through the
        close/reopen escape would leave an empty paragraph -- a visible
        blank line -- between them. An aligned block that ends the
        content keeps its paragraph open so the template's own closing
        tags end it, rather than leaving an empty one behind.

        Two tables in a row are the exception: a Word document merges
        tables that touch, so the reopened paragraph between them is
        kept and filled with the 1pt span.

        When the content starts with an escaped block, the wrapper
        paragraph is left holding nothing; a 1pt span with a zero-width
        space (``_HTML_TINY_SPAN``) is put in it so it takes the height
        of 1pt text rather than a full line. Content ending in a list
        or table gets the same span in its last reopened paragraph.

        :param blocks: block strings from ``_get_html_text_blocks``
        :return: the blocks joined into one string
        :rtype: str
        """
        suffix = self._html_escape_suffix()
        parts = []
        for index, block in enumerate(blocks):
            if index > 0:
                prev_is_escaped = blocks[index - 1].endswith(suffix)
                this_is_escaped = block.startswith(_ESCAPE_BLOCK_PREFIX)
                if (
                    prev_is_escaped
                    and this_is_escaped
                    and self._is_table_html_block(blocks[index - 1])
                    and self._is_table_html_block(block)
                ):
                    # Two tables with no paragraph between them are merged
                    # by Word: keep the reopened paragraph, 1pt tall.
                    parts.append(_HTML_TINY_SPAN)
                elif prev_is_escaped and this_is_escaped:
                    parts[-1] = parts[-1][: -len(suffix)]
                    block = block[len(_ESCAPE_BLOCK_PREFIX) :]
                elif not prev_is_escaped and not this_is_escaped:
                    parts.append("<text:line-break/>")
            parts.append(block)
        if blocks and blocks[0].startswith(_ESCAPE_BLOCK_PREFIX):
            parts[0] = _HTML_TINY_SPAN + parts[0]
        if blocks and self._is_aligned_html_block(blocks[-1]):
            align_end = "</text:p>%s" % suffix
            parts[-1] = parts[-1][: -len(align_end)] + "<text:span>"
        elif blocks and blocks[-1].endswith(suffix):
            parts[-1] += _HTML_TINY_SPAN
        return "".join(parts)

    def _is_table_html_block(self, block):
        """Tell whether ``block`` was emitted by ``_get_html_text_table``.

        :param block: one entry of the blocks list
        :return: ``True`` for a table block
        :rtype: bool
        """
        return block.startswith(
            _ESCAPE_BLOCK_PREFIX + "<table:table "
        ) and block.endswith("</table:table>%s" % self._html_escape_suffix())

    def _is_aligned_html_block(self, block):
        """Tell whether ``block`` was emitted by ``_get_html_text_aligned_block``.

        :param block: one entry of the blocks list
        :return: ``True`` for an aligned-paragraph block
        :rtype: bool
        """
        align_end = "</text:p>%s" % self._html_escape_suffix()
        return block.startswith(_ALIGN_BLOCK_START) and block.endswith(align_end)

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
