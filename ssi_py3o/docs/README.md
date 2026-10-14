# ssi_py3o — sub-template and sub-report

Two ways to reuse report content inside a py3o **ODT** report.

|                           | Sub-template                                                  | Sub-report                                                    |
| ------------------------- | ------------------------------------------------------------- | ------------------------------------------------------------- |
| Call in the report        | `subtemplate('<code>')`                                       | `subreport('<report_name>', <records>)`                       |
| What is inserted          | A shared ODT block (`py3o.template` with an _Include Code_)   | The result of another complete py3o report                    |
| Data it sees              | The variables of the **calling** report (`o`, loop variables) | Only the records you pass; its own model, template and parser |
| Needs a registered report | No                                                            | Yes (`ir.actions.report`)                                     |
| Access check              | None (same report, same user)                                 | Report groups and read access on the records                  |
| Cost                      | No extra render                                               | One extra render per call                                     |
| Guide                     | [sub-template.md](sub-template.md)                            | [sub-report.md](sub-report.md)                                |

## Which one should I use?

- The same block (identity header, signature table, disclaimer) is copied into many
  reports and reads the calling record: **sub-template**.
- Report A must show data of **another model** and a report for that data already
  exists: **sub-report**.
- You only need a different page header or footer: use the base template (letterhead) of
  the report, not these.
- The reports are ODS spreadsheets: neither feature applies, see
  [Spreadsheets](#spreadsheets-ods).

## Spreadsheets (ODS)

Only ODT report templates can use `subtemplate()` and `subreport()`, and only ODT
reports can be inserted. An ODS template is never scanned: a `subreport()` call in an
ODS report fails with an error, and a `subtemplate()` field is left alone.
[examples/report_partner_sheet.ods](examples/report_partner_sheet.ods) is a plain ODS
py3o report shown for comparison: spreadsheets use `${expression}` in cells and loop
links (`py3o://for=...` / `py3o:///for`) on their own rows.

## Example templates

All files are in [examples/](examples/). Upload them as `py3o.template` records and
create the reports as described in each guide.

| File                                  | Role                                                              |
| ------------------------------------- | ----------------------------------------------------------------- |
| `sub_template_contact_block.odt`      | Sub-template, include code `example_contact_block`                |
| `report_partner_with_subtemplate.odt` | `res.partner` report that inserts the block                       |
| `report_bank_list.odt`                | Sub-report on `res.partner.bank`, report name `example_bank_list` |
| `report_partner_with_subreport.odt`   | `res.partner` report that calls the sub-report per contact        |
| `report_partner_sheet.ods`            | Plain ODS report (comparison only)                                |

## Rules shared by both features

- py3o allows **one instruction per paragraph**: the `subtemplate(...)` or
  `subreport(...)` field must be the only content of its paragraph. It may sit in a
  table cell or a section.
- The field is a py3o function field. In LibreOffice Writer: _Insert > Field > More
  Fields > Functions > Input field_, put any label in _Name_ and the text
  `py3o://function="subtemplate('example_contact_block')"` (or the `subreport(...)`
  call) in _Reference_.
- Styles, fonts and pictures of the inserted content are imported with the prefix
  `inc_<code>_` (sub-template) or `inc_sub<n>_` (sub-report), so they never overwrite a
  style of the calling report with the same name.
- Both fail closed: a problem raises an error naming the report and the code or
  sub-report, and nothing is printed.
