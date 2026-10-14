# Sub-report

A **sub-report** prints the result of another py3o ODT report inside the calling report.
The other report keeps its own model, template and parser and is not modified.

## When to use it

- Report A (for example on `res.partner`) must list data of another model (invoices,
  bank accounts) and a report for that data already exists.
- You want to pass the records instead of searching for them inside the template of A.

For a block that only reads the calling record, a [sub-template](sub-template.md) is
simpler and cheaper.

## Set up

1. Have the sub-report ready: a py3o report with an ODT template, for example
   [report_bank_list.odt](examples/report_bank_list.odt) on `res.partner.bank` with
   report name `example_bank_list`.
2. In the calling `.odt`, add a paragraph that contains only the field
   `py3o://function="subreport('example_bank_list', line.bank_ids)"`. The second
   argument is any expression that returns records of the model of the sub-report. See
   [report_partner_with_subreport.odt](examples/report_partner_with_subreport.odt).
3. Print the calling report. After it is rendered, and before it is converted to PDF,
   each call is replaced by the body of the sub-report, rendered with the given records
   as its `objects`.

## Behavior

- The call only records the request, so it must be alone in its paragraph.
- An empty recordset inserts nothing.
- Only the body is inserted. The letterhead, header/footer, page size and margins of the
  sub-report are not carried over.
- Styles, fonts, pictures and the names of tables, frames and bookmarks are imported
  with the prefix `inc_sub<n>_`.
- The sub-report runs as the printing user: the user must belong to one of its groups
  (when it has any) and be able to read the records.
- Each call is one extra render; avoid calling it inside very large loops.

## Limits and errors

- A sub-report cannot call another sub-report.
- Both the calling report and the sub-report must be ODT.
- The model of the sub-report must match the passed records.
- Any failure raises a `UserError` (or `AccessError` for permissions) naming the report
  and the sub-report, and nothing is printed.
