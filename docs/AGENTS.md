# Documentation instructions

- Never hand-edit generated documents. Regenerate `tools/` with `just tool-docs`;
  for other generated pages run the recipe in their generation banner.
- Run `just doc-links` for relative links and anchors and `just doc-freshness`
  when changing generated-document inputs. Both operate on tracked Markdown;
  stage newly added files before validating their links.
- `workflow/plans/` is gitignored and transient: never commit plans; remove them
  when complete and preserve durable decisions in ADRs or specs.
- The optional `refs/` corpus is local-only. Cite `docs/refs/<path>:<line>` and
  report search results; never reproduce its prose in tracked files.
- Live matrices must come from `scripts/live_test_evidence.py` and identify the
  tested commit. Check subsequent changes before citing evidence. Never publish
  raw `test-results-*.json`; see [live testing](live-testing.md).
- Documentation extensions are not proof of squash eligibility: consult the
  [merge classification](workflow/agent-operations.md#non-interactive-shell-discipline).

## Architecture decision records

- Use a unique number and filename `NNNN-lowercase-kebab-slug.md` with an opening
  H1 announcing the same number. Renaming requires updating both filename and H1.
- Number gaps are legal; never renumber to close them. There is no ADR index.
  Give each record a descriptive slug and run `just adr-numbering`.
- Preserve durable decisions here; implementation sequencing belongs in local,
  uncommitted plans. Follow the documentation rules above for evidence, links, generated content,
  and merge classification.
