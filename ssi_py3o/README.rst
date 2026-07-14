.. image:: https://img.shields.io/badge/licence-AGPL--3-blue.svg
   :target: http://www.gnu.org/licenses/lgpl-3.0-standalone.html
   :alt: License: AGPL-3

==========
Py3o Extra
==========


Installation
============

To install this module, you need to:

1.  Clone the branch 14.0 of the repository https://github.com/open-synergy/ssi-py3o
2.  Add the path to this repository in your configuration (addons-path)
3.  Update the module list (Must be on developer mode)
4.  Go to menu *Apps -> Apps -> Main Apps*
5.  Search For *Py3o Extra*
6.  Install the module


Configuration — Py3o Base Template (Letterhead)
===============================================

This module can inject a shared header/footer (letterhead) into every py3o report
that uses an ODT template, without editing the ``.odt`` file of each report.

The base template is picked from ``ir.actions.report.py3o_base_template_id``. If
empty, it falls back to ``res.company.py3o_base_template_id`` (tab *Py3o Report* on
the company form). Only the header and footer are merged; paper size, orientation,
and left/right margins always stay the ones defined by the report itself. The
feature only activates when the report template is an ODT file.

A report can opt out entirely with the *No Base Template* checkbox on its *Py3o*
tab.

Writing a letterhead template
-----------------------------

The letterhead ``.odt`` header/footer is rendered by py3o **for every report that
uses it**. A variable that is not available in some report will break that
report's printing entirely, so the rules below are deliberately strict:

* Only use these variables inside the header/footer: ``company``,
  ``company_partner``, ``user``, ``o_format_date`` / ``o_format_datetime``,
  ``time``, and the native ODF fields ``<text:page-number>`` / ``<text:page-count>``
  (rendered by LibreOffice, not py3o).
* **Never** reference ``objects[0].<field>`` in the letterhead — that field only
  exists on some report models, not all of them.
* **Never** use a bare ``$`` (Genshi tries to interpolate it) — write ``$$``
  instead.
* For a logo, prefer
  ``py3o.image(company.partner_id.image_256, 'png', height='1.3cm', isb64=True)``
  over a static ``Pictures/*.png`` image: it follows the active company and needs
  no zip/manifest bookkeeping.

If the base template is corrupted, or the merge fails for any other reason, the
report is still printed **without** the letterhead (fail-open) and the error is
logged — a broken letterhead never blocks invoicing/printing.


Bug Tracker
===========

Bugs are tracked on `GitHub Issues
<https://github.com/open-synergy/ssi-py3o/issues>`_. In case of trouble, please
check there if your issue has already been reported. If you spotted it first,
help us smash it by providing detailed and welcomed feedback.


Credits
=======

Contributors
------------

* Michael Viriyananda <viriyananda.michael@gmail.com>
* Andhitia Rama <andhitia.r@gmail.com>

Maintainer
----------

.. image:: https://simetri-sinergi.id/logo.png
   :alt: PT. Simetri Sinergi Indonesia
   :target: https://simetri-sinergi.id

This module is maintained by the PT. Simetri Sinergi Indonesia.
