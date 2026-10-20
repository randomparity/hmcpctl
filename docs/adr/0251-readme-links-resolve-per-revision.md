# ADR 0251: README links resolve per revision

## Status
Accepted (2026-10-19), issue #1481. Amends the README-pointer bullet of
[ADR 0097](0097-generated-tool-reference-from-the-registry.md).

## Context
Twelve `README.md` links to this repository's own documentation pointed at
`https://github.com/randomparity/hmcpctl/blob/main/...`. A reader of the `v0.1.0` tag, or
of any older commit, followed them to `main`'s documentation, which can describe behaviour
that revision does not have.

ADR 0097 made the tool-reference pointer absolute because `README.md` is the
`[project] readme` long description and the sdist does not ship `docs/`, so a relative
link dangles on a package-index page. Two facts now weigh against that reason: nothing
is published to a package index ([ADR 0024](0024-separate-artifact-build-and-validation.md),
[development guide](../development.md#releasing)), and the README already carried eight
relative links (to `LICENSE`, `CONTRIBUTING.md`, `SECURITY.md`, `docs/python-api.md`, and
`docs/configuration.md`), three of them asserted by
`tests/test_project_metadata.py`.

## Decision
Every README link to a file in this repository is a relative path. GitHub resolves a
relative link against the ref being viewed, so the tag's README opens the tag's docs and
`main`'s opens `main`'s, with no edit at release time.

`tests/test_project_metadata.py` asserts that the README carries no `blob/` or `tree/`
URL into this repository and that every relative link names a path that exists in the
checkout; the anchor after `#` is not checked. The tool-reference pointer test asserts the
relative form.

## Consequences
- The release procedure gains no step: the `@vX.Y.Z` install pins remain the only README
  text bumped at release.
- Links in the long description embedded in a built wheel or sdist do not resolve
  outside a checkout. That reader is not served today; adopting package-index publication
  reopens this decision, under whichever record decides the publication.
- A renamed or deleted doc that the README links now fails a test instead of 404ing.
- Anchors can still drift unnoticed, as they could before.
- ADR 0097 does not yet point here: this change left its file untouched, so its Status
  needs an `Amended by` banner in a later change.

## Considered & rejected
- **Keep `blob/main` and rewrite the links to `blob/vX.Y.Z` at release.** judgment: two
  hand edits per release (pin, then unpin on reopening `main`) to serve a reader who views
  the README at the tag, while every untagged commit still points at `main`.
- **Pin every link to the latest tag permanently.** judgment: `main`'s README would then
  describe new features through the previous release's docs.
- **Do nothing.** verified: `grep -o 'hmcpctl/blob/main/docs' README.md | wc -l` printed `12` at
  `8bedccee`; the issue's defect is those links.
