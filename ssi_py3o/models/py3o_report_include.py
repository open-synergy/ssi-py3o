# Copyright 2026 OpenSynergy Indonesia
# Copyright 2026 PT. Simetri Sinergi Indonesia
# License AGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).

import re
from base64 import b64decode
from copy import deepcopy
from io import BytesIO
from zipfile import BadZipFile, ZipFile

from lxml import etree

from odoo import _, models
from odoo.exceptions import UserError

from .py3o_report import ODT_MIMETYPE, _clark

_INCLUDE_MARKER_RE = re.compile(r"\[\[py3o_include:([^\]]*)\]\]")

# Declarative children of <office:text> that belong to the document as a whole;
# they are never copied from a sub-template into the report.
_INCLUDE_SKIP_TAGS = (
    _clark("text", "sequence-decls"),
    _clark("text", "variable-decls"),
    _clark("text", "user-field-decls"),
    _clark("text", "dde-connection-decls"),
    _clark("text", "alphabetical-index-auto-mark-file"),
    _clark("text", "tracked-changes"),
    _clark("office", "forms"),
)

_FONT_NAME_ATTRS = (
    _clark("style", "font-name"),
    _clark("style", "font-name-asian"),
    _clark("style", "font-name-complex"),
)


class Py3oReport(models.TransientModel):
    _inherit = "py3o.report"

    # -- Sub-template include ------------------------------------------------
    # Replaces a paragraph holding only "[[py3o_include:<code>]]" in the body
    # of an ODT report template by the body of the py3o.template whose
    # include_code is <code>. Unlike the letterhead merge it fails closed.
    # See README.rst for the contract a sub-template author must follow.

    def _py3o_include_error(self, code, problem, solution):
        """Raise a structured ``UserError`` naming the report and the code.

        :param code: include code involved in the failure
        :param problem: what went wrong
        :param solution: how the user can fix it
        :raises UserError: always
        """
        report = self.ir_actions_report_id
        error_message = (
            _(
                """
Context: Insert py3o sub-template into report '%s'
Database ID: %s
Problem: %s (include code '%s')
Solution: %s
"""
            )
            % (report.display_name, report.id, problem, code, solution)
        )
        raise UserError(error_message)

    def _py3o_find_include_markers(self, root):
        """Find the include marker paragraphs below ``root``.

        A marker is a ``text:p`` whose whole text, with every ``text:span``
        joined and stripped, is exactly ``[[py3o_include:<code>]]``.

        :param root: lxml element to scan
        :return: list of ``(paragraph, code)`` tuples in document order
        """
        found = []
        for para in root.iter(_clark("text", "p")):
            text = "".join(para.itertext()).strip()
            match = _INCLUDE_MARKER_RE.fullmatch(text)
            if match:
                found.append((para, match.group(1)))
        return found

    def _py3o_ensure_container(self, root, tag, successor_tags):
        """Return the direct child ``tag`` of ``root``, creating it if absent.

        :param root: lxml element
        :param tag: Clark name of the wanted child
        :param successor_tags: tags that must come after the child; the new
            child is inserted before the first of them (else appended)
        :return: the existing or newly created element
        """
        node = root.find(tag)
        if node is not None:
            return node
        node = etree.Element(tag)
        for index, child in enumerate(root):
            if child.tag in successor_tags:
                root.insert(index, node)
                return node
        root.append(node)
        return node

    def _py3o_load_include_fragment(self, code):
        """Load and validate the sub-template registered under ``code``.

        :param code: include code
        :return: tuple ``(zip, content_root, styles_root, blocks)``
        :raises UserError: if the code is unknown, the sub-template is not a
            readable ODT, it is empty or it contains another marker
        """
        template = self.env["py3o.template"].search(
            [("include_code", "=", code)], limit=1
        )
        if not template:
            self._py3o_include_error(
                code,
                _("No py3o template has this include code"),
                _("Set the include code on the sub-template or fix the marker"),
            )
        if template.filetype != "odt" or not template.py3o_template_data:
            self._py3o_include_error(
                code,
                _("The sub-template is not an ODT file with content"),
                _("Upload an ODT file to the sub-template"),
            )
        try:
            zip_in = ZipFile(BytesIO(b64decode(template.py3o_template_data)))
            if zip_in.read("mimetype").decode("utf-8").strip() != ODT_MIMETYPE:
                raise ValueError("not an ODT package")
            content_root = etree.fromstring(zip_in.read("content.xml"))
            styles_root = etree.fromstring(zip_in.read("styles.xml"))
        except (BadZipFile, KeyError, ValueError, etree.XMLSyntaxError):
            self._py3o_include_error(
                code,
                _("The sub-template is a damaged ODT package"),
                _("Re-upload a valid ODT file to the sub-template"),
            )
        body = content_root.find(
            "%s/%s" % (_clark("office", "body"), _clark("office", "text"))
        )
        if body is None:
            self._py3o_include_error(
                code,
                _("The sub-template has no text body"),
                _("Re-upload a valid ODT file to the sub-template"),
            )
        if self._py3o_find_include_markers(body):
            self._py3o_include_error(
                code,
                _("The sub-template contains another include marker"),
                _("Nested includes are not supported; remove the marker"),
            )
        blocks = [child for child in body if child.tag not in _INCLUDE_SKIP_TAGS]
        if not blocks:
            self._py3o_include_error(
                code,
                _("The sub-template body is empty"),
                _("Put the content to insert in the sub-template body"),
            )
        return zip_in, content_root, styles_root, blocks

    def _py3o_replace_markers(self, markers, blocks):
        """Replace marker paragraphs by copies of the fragment blocks.

        :param markers: marker paragraphs to replace
        :param blocks: elements copied from the sub-template body
        :return: list of all inserted elements
        """
        inserted = []
        for para in markers:
            parent = para.getparent()
            index = parent.index(para)
            tail = para.tail
            parent.remove(para)
            copies = [deepcopy(block) for block in blocks]
            for offset, copy in enumerate(copies):
                parent.insert(index + offset, copy)
            copies[-1].tail = tail
            inserted.extend(copies)
        return inserted

    def _py3o_import_include_fonts(self, prefix, frag_roots, report_roots):
        """Copy the fragment font faces into the report under ``prefix``.

        :param prefix: name prefix, ``inc_<code>_``
        :param frag_roots: fragment ``content.xml``/``styles.xml`` roots
        :param report_roots: report ``content.xml``/``styles.xml`` roots
        :return: set of the original font names that were imported
        """
        decls_tag = _clark("office", "font-face-decls")
        name_attr = _clark("style", "name")
        fonts = {}
        for root in frag_roots:
            decls = root.find(decls_tag)
            if decls is None:
                continue
            for font in decls:
                name = font.get(name_attr)
                if name and name not in fonts:
                    fonts[name] = font
        if not fonts:
            return set()
        successors = (
            _clark("office", "styles"),
            _clark("office", "automatic-styles"),
            _clark("office", "master-styles"),
            _clark("office", "body"),
        )
        for root in report_roots:
            decls = self._py3o_ensure_container(root, decls_tag, successors)
            existing = {child.get(name_attr) for child in decls}
            for name, font in fonts.items():
                if prefix + name in existing:
                    continue
                new_font = deepcopy(font)
                new_font.set(name_attr, prefix + name)
                decls.append(new_font)
        return set(fonts)

    def _py3o_rename_fonts(self, nodes, prefix, font_names):
        """Point ``style:font-name*`` attributes at the prefixed font faces.

        :param nodes: elements whose subtrees are rewritten
        :param prefix: name prefix, ``inc_<code>_``
        :param font_names: original names of the imported fonts
        """
        for node in nodes:
            for sub in node.iter():
                for attr in _FONT_NAME_ATTRS:
                    value = sub.get(attr)
                    if value in font_names:
                        sub.set(attr, prefix + value)

    def _py3o_import_include_images(
        self, code, seed, frag_zip, new_zip_entries, manifest_root
    ):
        """Copy ``Pictures/*`` used by inserted blocks, renamed per code.

        :param code: include code
        :param seed: inserted elements to scan for ``draw:image``
        :param frag_zip: sub-template zip
        :param new_zip_entries: dict of zip entries to add to the report
        :param manifest_root: report ``manifest.xml`` root, updated in place
        :raises UserError: if a referenced picture is missing in the zip
        """
        href_attr = _clark("xlink", "href")
        for element in seed:
            for img in element.iter(_clark("draw", "image")):
                href = img.get(href_attr)
                if not href or not href.startswith("Pictures/"):
                    continue
                target = "Pictures/inc_%s_%s" % (code, href[len("Pictures/") :])
                if target not in new_zip_entries:
                    try:
                        new_zip_entries[target] = frag_zip.read(href)
                    except KeyError:
                        self._py3o_include_error(
                            code,
                            _("The sub-template references missing image %s") % href,
                            _("Re-upload a valid ODT file to the sub-template"),
                        )
                    self._py3o_add_manifest_entry(manifest_root, target)
                img.set(href_attr, target)

    def _py3o_apply_includes(self, report_bytes):
        """Insert sub-templates at the markers of an ODT report template.

        Only ODT report templates are scanned. Style, font and image
        names of each sub-template are imported with prefix ``inc_<code>_``.
        A template without any marker is returned byte for byte.

        :param report_bytes: raw report template
        :return: raw report template with sub-templates inserted
        :raises UserError: if a marker cannot be resolved
        """
        if self._py3o_sniff_mimetype(report_bytes) != ODT_MIMETYPE:
            return report_bytes
        try:
            report_zip = ZipFile(BytesIO(report_bytes))
            content_root = etree.fromstring(report_zip.read("content.xml"))
            styles_root = etree.fromstring(report_zip.read("styles.xml"))
            manifest_root = etree.fromstring(report_zip.read("META-INF/manifest.xml"))
        except (BadZipFile, KeyError, etree.XMLSyntaxError):
            return report_bytes
        markers = self._py3o_find_include_markers(content_root)
        if not markers:
            return report_bytes

        by_code = []
        for para, code in markers:
            for entry in by_code:
                if entry[0] == code:
                    entry[1].append(para)
                    break
            else:
                by_code.append((code, [para]))

        common_tag = _clark("office", "styles")
        auto_tag = _clark("office", "automatic-styles")
        new_zip_entries = {}
        for code, paras in by_code:
            (
                frag_zip,
                frag_content,
                frag_styles,
                blocks,
            ) = self._py3o_load_include_fragment(code)
            prefix = "inc_%s_" % code
            seed = self._py3o_replace_markers(paras, blocks)

            frag_auto = etree.Element(auto_tag)
            for root in (frag_content, frag_styles):
                container = root.find(auto_tag)
                if container is not None:
                    frag_auto.extend(deepcopy(list(container)))
            report_common = self._py3o_ensure_container(
                styles_root,
                common_tag,
                (auto_tag, _clark("office", "master-styles")),
            )
            report_auto = self._py3o_ensure_container(
                content_root, auto_tag, (_clark("office", "body"),)
            )
            import_map = self._py3o_build_style_import_map(
                seed, frag_styles.find(common_tag), frag_auto
            )
            self._py3o_apply_style_imports(
                import_map, report_common, report_auto, seed, prefix
            )
            font_names = self._py3o_import_include_fonts(
                prefix, (frag_content, frag_styles), (content_root, styles_root)
            )
            imported = []
            for name in import_map:
                for container in (report_common, report_auto):
                    style = self._find_style_by_name(container, prefix + name)
                    if style is not None:
                        imported.append(style)
            self._py3o_rename_fonts(imported + seed, prefix, font_names)
            self._py3o_import_include_images(
                code, seed, frag_zip, new_zip_entries, manifest_root
            )

        new_zip_entries["content.xml"] = etree.tostring(
            content_root, xml_declaration=True, encoding="UTF-8"
        )
        new_zip_entries["styles.xml"] = etree.tostring(
            styles_root, xml_declaration=True, encoding="UTF-8"
        )
        new_zip_entries["META-INF/manifest.xml"] = etree.tostring(
            manifest_root, xml_declaration=True, encoding="UTF-8"
        )
        return self._py3o_write_merged_zip(report_zip, new_zip_entries)
