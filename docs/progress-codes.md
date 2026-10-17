# Offline progress-code definitions

Reference-code reads include independently written factual summaries based on IBM's
[Power Systems: Progress codes](https://public.dhe.ibm.com/systems/power/docs/hw/p9/p9eai_prg1.pdf)
(IBM copyright 2018, 2021). The source applies to POWER9 servers and associated models;
individual entries retain their section's applicability. These summaries describe the
meaning of a reported checkpoint. They do not establish current boot success, readiness,
or failure, and they are not repair instructions. In particular, CA00E105 reports the
normal firmware handoff to the operating system; the OS may still fail afterward.

## Coverage and matching

The packaged catalog contains 2,202 defined notations from the source's physical PDF
pages 15–136: AIX configuration and diagnostics, dump indicators, crash sequences,
service-processor and virtual-service-processor codes, IPL and partition/boot firmware,
Linux boot, dump status, partition status and general status codes. It includes the
unheaded crash and dump entries, not only section headings. Documented wildcard ranges
are represented by their original variable notations; there are no separate inclusive
interval labels in this source's definition inventory. Coverage does not imply applicability
to a different processor generation or an inventory of system reference codes generally.

Matching ignores case and surrounding whitespace. A whitespace-separated diagnostic
suffix is allowed; the application preserves the entire original `refcode` string.
Exact documented notations win, followed by documented short AIX indicator aliases,
then variable patterns (`X`, `Y`, `T`, `S` each denote a hexadecimal position).
Documented bounds limit individual positions: C104550X ends in 0 or 1,
C116C2XX ends in 00–1F, and C16453XX ends in 2 or 3. Patterns admitting fewer
concrete values win; document order breaks equal-specificity ties.
For example D600430A wins over D6xx430A, C200ABCD matches C200XXXX, and
D1A23F01 matches D1XX3Y01. The variables retain their contextual meanings, such as
node IDs, dump progress or event type/subtype; lookup does not diagnose those values.

The AIX configuration and diagnostic sections allow a leading zero to be omitted;
the dump section allows its two leading zeros or blanks to be omitted. Examples:
`2e6` matches `02E6`, and `c9` or `0c9` matches `00C9`.
Hyphenated crash sequences such as `888-102-32F` remain a single code. Arbitrary
prefixes, nonhexadecimal variable values and unknown codes have no definition.

## Python and application output

```python
from hmcpctl.progress_codes import lookup_progress_code

record = lookup_progress_code(" ca00e105 LP=00002 ")
if record is not None:
    print(record.definition)
    print(record.source())
```

The lookup returns an immutable `ProgressCodeDefinition` or `None`. Its fields are
`notation`, `definition`, `applicability`, and `pdf_pages` (physical PDF page numbers).
`source()` returns a fresh JSON-compatible provenance object containing `notation`,
`applicability`, `url`, and `pdf_pages`. The module is a pre-release domain API;
`hmcpctl.api`'s six-name stable facade is unchanged.

`hmcpctl lpars refcodes`, `hmc_read_lpar_refcodes`, and the reference-code section
of `hmc_inspect_lpar` use the same enriched reader. Rows preserve `lpar_name`,
`time_stamp` and `refcode` and add `definition` and `definition_source`. Both added
fields are `null` when lookup is unknown, including malformed or unsupported values.
The CLI table says `Unknown definition` and escapes nonprintable characters in cells;
`--json` and MCP preserve the original strings and explicit nulls.
Transport errors, selector validation, ordering and count bounds retain their behavior.

Lookup uses the installed package's JSON resource. It requires no network, credentials
or local reference corpus and works from the wheel and source distribution.

## Catalog maintenance

The catalog ships original factual summaries, public source identity, attribution,
POWER9 applicability, physical page/code locators, PDF checksum and an inventory
checksum. It does not ship IBM explanation or repair prose. A frozen test fixture holds
the independently extracted notation/section/page inventory's count and SHA-256.

Validate structure and inventory integrity with:

```sh
uv run --no-sync python scripts/check_progress_codes.py
```

For source comparison, obtain a development Markdown transcription of the public PDF
with physical page markers and explanation boundaries, then explicitly supply it:

```sh
uv run --no-sync python scripts/check_progress_codes.py --source TRANSCRIPTION.md
```

The checker extracts factual identities only, including unheaded definitions. It rejects
missing/extra/reordered identities, duplicate notations, missing definitions or locators,
and inventory checksum differences. Updating definitions requires factual review;
a checksum alone cannot prove their semantic accuracy. Source transcription is a
maintenance convenience and is not a package dependency or a provenance link.
