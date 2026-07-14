# Copyright 2026 OpenSynergy Indonesia
# Copyright 2026 PT. Simetri Sinergi Indonesia
# License AGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
"""In-memory ODT fixtures for tests/test_odf_merge.py.

Built directly as XML strings (not binary .odt files) so the XML under test
is readable in a diff and each test can craft the exact style collision it
needs to prove. See tests/test_odf_merge.py for why this is plain Python
(TransactionCase) instead of an odoo-yaml-test YAML scenario.
"""
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

ODT_MIMETYPE = "application/vnd.oasis.opendocument.text"

_CONTENT_XML = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b"<office:document-content "
    b'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0">'
    b"<office:body><office:text/></office:body>"
    b"</office:document-content>"
)


def _build_odt(styles_xml, extra_files=None):
    """Zip up a minimal but structurally valid .odt package.

    `extra_files` is a mapping of {full_path: bytes} added to the zip and
    declared in META-INF/manifest.xml (used for Pictures/*.png fixtures).
    """
    manifest_entries = [
        '<manifest:file-entry manifest:full-path="/" manifest:version="1.2" '
        'manifest:media-type="%s"/>' % ODT_MIMETYPE,
        '<manifest:file-entry manifest:full-path="content.xml" '
        'manifest:media-type="text/xml"/>',
        '<manifest:file-entry manifest:full-path="styles.xml" '
        'manifest:media-type="text/xml"/>',
    ]
    files = {"content.xml": _CONTENT_XML, "styles.xml": styles_xml.encode("utf-8")}
    for path, data in (extra_files or {}).items():
        manifest_entries.append(
            '<manifest:file-entry manifest:full-path="%s" '
            'manifest:media-type="image/png"/>' % path
        )
        files[path] = data

    manifest_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<manifest:manifest xmlns:manifest="
        '"urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
        'manifest:version="1.2">' + "".join(manifest_entries) + "</manifest:manifest>"
    )

    out = BytesIO()
    with ZipFile(out, "w") as zf:
        zf.writestr("mimetype", ODT_MIMETYPE, compress_type=ZIP_STORED)
        zf.writestr("META-INF/manifest.xml", manifest_xml, compress_type=ZIP_DEFLATED)
        for name, data in files.items():
            zf.writestr(name, data, compress_type=ZIP_DEFLATED)
    return out.getvalue()


_STYLES_XML_NSMAP = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:xlink="http://www.w3.org/1999/xlink" '
    'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"'
)

# A4 portrait letterhead: header "ACME LETTERHEAD" (bold, style P1, parent
# Header), footer with page number, a logo image, and a base font. P1 and
# Header are deliberately named the same as styles report_odt() defines with
# a DIFFERENT meaning, to exercise the style-collision rename (Jebakan #2).
BASE_STYLES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-styles %(nsmap)s>
  <office:font-face-decls>
    <style:font-face style:name="Base Font" svg:font-family="Base Font"/>
  </office:font-face-decls>
  <office:styles>
    <style:style style:name="Standard" style:family="paragraph" style:class="text"/>
    <style:style style:name="Header" style:family="paragraph"
        style:parent-style-name="Standard" style:class="extra"/>
    <style:style style:name="Footer" style:family="paragraph"
        style:parent-style-name="Standard" style:class="extra" style:master-page-name=""/>
  </office:styles>
  <office:automatic-styles>
    <style:page-layout style:name="Mpm1">
      <style:page-layout-properties fo:page-width="8.2681in" fo:page-height="11.6929in"
          style:print-orientation="portrait"
          fo:margin-top="0.7874in" fo:margin-bottom="0.7874in"
          fo:margin-left="0.7874in" fo:margin-right="0.7874in"/>
      <style:header-style>
        <style:header-footer-properties fo:min-height="1cm" fo:margin-bottom="0.5cm"
            style:dynamic-spacing="false"/>
      </style:header-style>
      <style:footer-style>
        <style:header-footer-properties fo:min-height="0.5cm" fo:margin-top="0.3cm"
            style:dynamic-spacing="false"/>
      </style:footer-style>
    </style:page-layout>
    <style:style style:name="P1" style:family="paragraph" style:parent-style-name="Header"
        style:class="extra">
      <style:text-properties fo:font-weight="bold" style:font-name="Base Font"/>
    </style:style>
  </office:automatic-styles>
  <office:master-styles>
    <style:master-page style:name="Standard" style:page-layout-name="Mpm1">
      <style:header>
        <text:p text:style-name="P1">ACME LETTERHEAD</text:p>
        <draw:frame draw:style-name="Mfr1"
            draw:name="py3o.image(company.partner_id.image_256, 'png', isb64=True)"
            svg:width="1in">
          <draw:image xlink:href="Pictures/logo.png"/>
        </draw:frame>
      </style:header>
      <style:footer>
        <text:p text:style-name="Footer">Page
          <text:page-number text:select-page="current">1</text:page-number>
          / <text:page-count>1</text:page-count>
        </text:p>
      </style:footer>
    </style:master-page>
  </office:master-styles>
</office:document-styles>
""" % {
    "nsmap": _STYLES_XML_NSMAP
}

# Landscape report with no header/footer of its own, an unrelated (but
# same-named) P1 style, and its own font. Mirrors the real-world shape of
# ssi_py3o_sample/reports/report_sample.odt (self-closing master-page).
REPORT_STYLES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-styles %(nsmap)s>
  <office:font-face-decls>
    <style:font-face style:name="Report Font" svg:font-family="Report Font"/>
  </office:font-face-decls>
  <office:styles>
    <style:style style:name="Standard" style:family="paragraph" style:class="text"/>
  </office:styles>
  <office:automatic-styles>
    <style:page-layout style:name="Mpm1">
      <style:page-layout-properties fo:page-width="11.69in" fo:page-height="8.2681in"
          style:print-orientation="landscape"
          fo:margin-top="0.3543in" fo:margin-bottom="0.3543in"
          fo:margin-left="2cm" fo:margin-right="0.2756in"/>
      <style:header-style/>
      <style:footer-style/>
    </style:page-layout>
    <style:style style:name="P1" style:family="paragraph" style:parent-style-name="Standard"
        style:class="extra">
      <style:text-properties fo:font-style="italic" style:font-name="Report Font"/>
    </style:style>
  </office:automatic-styles>
  <office:master-styles>
    <style:master-page style:name="Standard" style:page-layout-name="Mpm1"/>
  </office:master-styles>
</office:document-styles>
""" % {
    "nsmap": _STYLES_XML_NSMAP
}

LOGO_PNG_BYTES = b"\x89PNG\r\n\x1a\nFAKE-BASE-LOGO-BYTES-FOR-TESTS"


def base_odt():
    return _build_odt(
        BASE_STYLES_XML, extra_files={"Pictures/logo.png": LOGO_PNG_BYTES}
    )


def report_odt(extra_files=None):
    return _build_odt(REPORT_STYLES_XML, extra_files=extra_files)
