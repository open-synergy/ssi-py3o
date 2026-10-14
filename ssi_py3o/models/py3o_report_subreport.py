# Copyright 2026 OpenSynergy Indonesia
# Copyright 2026 PT. Simetri Sinergi Indonesia
# License AGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).

import os
import re
from copy import deepcopy
from io import BytesIO
from zipfile import BadZipFile, ZipFile

from lxml import etree

from odoo import _, models
from odoo.exceptions import AccessError, UserError

from .py3o_report import ODT_MIMETYPE, _clark

_SUBREPORT_TOKEN_RE = re.compile(r"\[\[py3o_subreport:(\d+)\]\]")

# Sub-report requests recorded while a report renders, keyed by
# ``(database, id of the py3o.report record)``. They cannot live on the
# recordset itself (recordsets reject new attributes).
_SUBREPORT_REQUESTS = {}

_SKIP_BODY_TAGS = (
    _clark("text", "sequence-decls"),
    _clark("text", "variable-decls"),
    _clark("text", "user-field-decls"),
    _clark("text", "dde-connection-decls"),
    _clark("text", "alphabetical-index-auto-mark-file"),
    _clark("text", "tracked-changes"),
    _clark("office", "forms"),
)


class Py3oReport(models.TransientModel):
    _inherit = "py3o.report"

    # -- Sub-report -----------------------------------------------------------
    # A report template calls subreport('<report_name>', <recordset>). The call
    # only records the request; once the calling ODT is rendered and before it
    # is converted, every token paragraph is replaced by the body of the
    # sub-report, rendered on its own with its own model, template and parser.

    def _py3o_subreport_key(self):
        """Return the key of this record in the request registry.

        :return: tuple ``(database name, record id)``
        """
        return (self.env.cr.dbname, self.id)

    def _py3o_subreport_error(self, name, problem, solution, error=UserError):
        """Raise a structured error naming the calling report and sub-report.

        :param name: report name of the sub-report involved
        :param problem: what went wrong
        :param solution: how the user can fix it
        :param error: exception class to raise
        :raises UserError: by default, always raises ``error``
        """
        report = self.ir_actions_report_id
        error_message = (
            _(
                """
Context: Insert py3o sub-report into report '%s'
Database ID: %s
Problem: %s (sub-report '%s')
Solution: %s
"""
            )
            % (report.display_name, report.id, problem, name, solution)
        )
        raise error(error_message)

    def _get_parser_context(self, model_instance, data):
        """Add the ``subreport`` function to the render context.

        :param model_instance: records being printed
        :param data: report data
        :return: the render context
        """
        context = super()._get_parser_context(model_instance, data)
        if self.env.context.get("py3o_in_subreport"):

            def subreport(report_name, records=None):
                self._py3o_subreport_error(
                    report_name,
                    _("A sub-report cannot call another sub-report"),
                    _("Remove the subreport() call from the sub-report template"),
                )

            context["subreport"] = subreport
            return context
        requests = _SUBREPORT_REQUESTS.setdefault(self._py3o_subreport_key(), [])

        def subreport(report_name, records):
            requests.append((report_name, records))
            return "[[py3o_subreport:%d]]" % (len(requests) - 1)

        context["subreport"] = subreport
        return context

    def _create_single_report(self, model_instance, data):
        """Render the report, then resolve its sub-reports before conversion.

        :param model_instance: records being printed
        :param data: report data
        :return: path of the produced file
        """
        skip = self.env.context.get("report_py3o_skip_conversion")
        key = self._py3o_subreport_key()
        try:
            result_path = super(
                Py3oReport, self.with_context(report_py3o_skip_conversion=True)
            )._create_single_report(model_instance, data)
            if _SUBREPORT_REQUESTS.get(key):
                self._py3o_resolve_subreports(result_path, _SUBREPORT_REQUESTS[key])
        finally:
            _SUBREPORT_REQUESTS.pop(key, None)
        if skip:
            return result_path
        result_path = self._convert_single_report(result_path, model_instance, data)
        return self._postprocess_report(model_instance, result_path)

    def _py3o_render_subreport(self, name, records):
        """Render the sub-report ``name`` for ``records`` as a raw ODT file.

        :param name: ``report_name`` of the sub-report
        :param records: recordset the sub-report prints
        :return: bytes of the rendered ODT
        :raises UserError: if the sub-report is unusable
        :raises AccessError: if the user may not print it or read the records
        """
        report = self.env["ir.actions.report"]._get_report_from_name(name)
        if not report or report.report_type != "py3o":
            self._py3o_subreport_error(
                name,
                _("No py3o report has this name"),
                _("Fix the report name in the subreport() call"),
            )
        if (
            not report.py3o_template_id.py3o_template_data
            or report.py3o_template_id.filetype != "odt"
        ):
            self._py3o_subreport_error(
                name,
                _("The sub-report template is not an ODT file"),
                _("Only ODT reports can be inserted as sub-report"),
            )
        if report.model != records._name:
            self._py3o_subreport_error(
                name,
                _("The sub-report prints '%s' but got '%s'")
                % (report.model, records._name),
                _("Pass a recordset of the model of the sub-report"),
            )
        if report.groups_id and not report.groups_id & self.env.user.groups_id:
            self._py3o_subreport_error(
                name,
                _("The user is not allowed to print this sub-report"),
                _("Ask an administrator for access to the sub-report"),
                error=AccessError,
            )
        records.check_access_rights("read")
        records.check_access_rule("read")
        sub = (
            self.env["py3o.report"]
            .with_context(py3o_in_subreport=True, report_py3o_skip_conversion=True)
            .create({"ir_actions_report_id": report.id})
        )
        path = sub._create_single_report(records, {})
        try:
            with open(path, "rb") as odt_file:
                return odt_file.read()
        finally:
            os.unlink(path)

    def _py3o_resolve_subreports(self, result_path, requests):
        """Replace the sub-report tokens of a rendered ODT by their content.

        :param result_path: path of the rendered ODT, rewritten in place
        :param requests: list of ``(report_name, records)`` in token order
        :raises UserError: if the file is not an ODT or a token cannot be
            resolved
        """
        with open(result_path, "rb") as odt_file:
            report_bytes = odt_file.read()
        if self._py3o_sniff_mimetype(report_bytes) != ODT_MIMETYPE:
            self._py3o_subreport_error(
                requests[0][0],
                _("The calling report is not an ODT file"),
                _("subreport() works only in ODT report templates"),
            )
        try:
            report_zip = ZipFile(BytesIO(report_bytes))
            content_root = etree.fromstring(report_zip.read("content.xml"))
            styles_root = etree.fromstring(report_zip.read("styles.xml"))
            manifest_root = etree.fromstring(report_zip.read("META-INF/manifest.xml"))
        except (BadZipFile, KeyError, etree.XMLSyntaxError):
            self._py3o_subreport_error(
                requests[0][0],
                _("The rendered report is a damaged ODT package"),
                _("Check the template of the calling report"),
            )
        paragraphs = {}
        for para in content_root.iter(_clark("text", "p")):
            match = _SUBREPORT_TOKEN_RE.fullmatch("".join(para.itertext()).strip())
            if match:
                paragraphs[int(match.group(1))] = para
        new_zip_entries = {}
        roots = (content_root, styles_root, manifest_root)
        for index, (name, records) in enumerate(requests):
            para = paragraphs.get(index)
            if para is None:
                self._py3o_subreport_error(
                    name,
                    _("The subreport() call is not alone in its paragraph"),
                    _("Put the subreport() field in a paragraph of its own"),
                )
            if not records:
                para.getparent().remove(para)
                continue
            sub_bytes = self._py3o_render_subreport(name, records)
            frag_zip = ZipFile(BytesIO(sub_bytes))
            frag_content = etree.fromstring(frag_zip.read("content.xml"))
            frag_styles = etree.fromstring(frag_zip.read("styles.xml"))
            body = frag_content.find(
                "%s/%s" % (_clark("office", "body"), _clark("office", "text"))
            )
            blocks = [child for child in body if child.tag not in _SKIP_BODY_TAGS]
            if not blocks:
                para.getparent().remove(para)
                continue
            self._py3o_merge_fragment(
                "sub%d" % index,
                [para],
                [deepcopy(block) for block in blocks],
                (frag_zip, frag_content, frag_styles),
                roots,
                new_zip_entries,
                rename_names=True,
            )
        for name, root in (
            ("content.xml", content_root),
            ("styles.xml", styles_root),
            ("META-INF/manifest.xml", manifest_root),
        ):
            new_zip_entries[name] = etree.tostring(
                root, xml_declaration=True, encoding="UTF-8"
            )
        merged = self._py3o_write_merged_zip(report_zip, new_zip_entries)
        with open(result_path, "wb") as odt_file:
            odt_file.write(merged)
