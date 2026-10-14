# Sub-template

A **sub-template** is an ODT block kept in one `py3o.template` and inserted into any
py3o ODT report. Editing it changes every report that uses it; the report files are
never touched.

## When to use it

- A block repeated in many reports: identity header, signature table, legal notice.
- The block reads the record being printed (`o.name`, `o.partner_id`, ...).
- You want no extra report to maintain and no extra render.

Do not use it for data of another model (use a [sub-report](sub-report.md)) or for page
headers and footers (use the letterhead).

## Set up

1. Create the block as an ODT file and upload it as a `py3o.template` (menu _Reporting >
   Py3o_ under _Settings > Technical_). Fill **Include Code**: unique, lowercase
   letters, digits and `_`, starting with a letter, for example `example_contact_block`.
   The form warns while you type a code that is already used.
2. In the report `.odt`, add a paragraph that contains only the field
   `py3o://function="subtemplate('example_contact_block')"`. The code must be a literal
   string (single or double quotes); `subtemplate(o.code)` is rejected.
3. Print the report. The paragraph is replaced by the body of the sub-template before
   rendering.

Example files: [sub_template_contact_block.odt](examples/sub_template_contact_block.odt)
and [report_partner_with_subtemplate.odt](examples/report_partner_with_subtemplate.odt).

## Writing the sub-template

- It runs in the context of the **calling** report. Put the call inside the loop that
  defines the variable it uses (`for o in objects`).
- It must close every loop or condition it opens.
- py3o directives and `py3o.image(...)` frames are copied untouched.
- Declarative elements (`text:sequence-decls` and similar) are not copied.
- It cannot contain another `subtemplate()` field (no nesting).
- To vary the content, use a different sub-template per case, or variables of the
  calling report. The call takes no parameters.

## Errors

An unknown code, a sub-template that is not an ODT, a damaged file, a nested field, or a
`subtemplate()` field that is not a literal code alone in its paragraph raises a
`UserError` naming the report and the code.
