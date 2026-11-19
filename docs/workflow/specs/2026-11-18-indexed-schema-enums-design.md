# Offline enum evidence from an indexed HMC schema capture

## Scope

Issue #1813 adds an extracted schema capture as an input to the existing
`enums` command. Operators retain the private archive and can reproduce the
published vocabulary from its indexed response bodies. A JSON capture file
continues through the existing entry point. The command retains its current
options. The capture directory is the smallest import unit because its index,
release summary and response bodies are already co-located.

## Global constraints

Python >=3.11, existing dependencies, local offline operation, synthetic CI inputs.
Hosted CI runs on amd64 with Python 3.11–3.15.

## Import and derivation

Read the release and build from summary.json. Require the supplied firmware to
match the captured release. Read index.json as a sequence of unique HTTP 200
schema paths. Only the web schema route, its inc and links subdirectories, and
ordinary XSD filenames belong to this capture. Derive each member name from
its route and require the indexed relative filename to agree. Read only these
files and the two named JSON files, with finite input limits. Check the stored
size and SHA-256 against the original bytes, including empty bodies.

Feed the verified bytes into the exporter's existing schema derivation. A zero-byte
200 contributes neither declarations nor a schema source. Require an XSD schema
root for indexed nonempty bodies. Use the hardened XML parser without resolving
external references. Read named top-level simple types and their direct restriction
enumerations. Use an element's explicit type or its own anonymous simple-content
extension. A nested child's type belongs to the child. Ignore example markup in
annotations. Bind only a unique observed enum target. A different declaration
of the same element, including a non-enum declaration, suppresses the binding.
Conflicting definitions of a flat type name suppress that type and its bindings.
Compare the in-scope default namespace of an unprefixed type reference with
the target namespace of its captured declaration. A catalog path supplies no
evidence of namespace ownership. Leave prefixed references unresolved in
this flat projection. Repeated equivalent declarations give reproducible
output independent of record order. Preserve the literal order of a single declaration.

Publish firmware, types and elements in the existing vocabulary shape. Add the
verified build and a sorted mapping from nonempty schema routes to SHA-256 for
the indexed input. Review derived names and literals and run the existing
private-pattern and raw-identifier checks on the entire serialized result.
Also apply operator patterns to decoded keys and strings so JSON escaping
preserves the meaning of a private marker.
The operator supplies patterns for private names specific to their environment.

## Failure model

- Actors and deployments include a local operator processing an extracted private
  archive, and CI exercising the same command with synthetic inputs.
- Assets include private capture data, the prior output at the requested path,
  source identity and the distinction between absent and conflicting evidence.
- A rejected input fails the command before publication. Existing output survives
  validation and privacy failures. Incomplete include coverage limits conclusions
  to declarations present in captured response bytes.
- Capture authenticity rests with the operator's independently retained archive
  receipt. Digests in the index establish consistency with the supplied bytes.
- #790 owns additional fetching. #1783 owns operator and fleet diagnostics.
  #793 and #785 own runtime capability and maturity assessment. The maintainer
  owns closure validation. Private custody and CI independence remain with the
  capture maintainer and repository CI.

### Threat model

- Treat index fields, summary fields and XML as untrusted. Open regular files
  beneath the selected directory with link traversal disabled for member paths.
  Bound metadata, individual responses, row count and cumulative response size.
- Read only the selected metadata fields. Emit row numbers and public error
  categories for invalid records. Keep private paths, XML, headers, identities
  and unrecognized metadata out of the derived document and error messages.
- Keep network access outside the importer. The caller selects a trusted output
  location. Publication receives data only after derivation and scanning succeed.

## Validation

Exercise the CLI with synthetic capture directories, reversed and conflicting
declarations, zero-byte and unsafe or malformed responses, integrity mismatches,
invalid metadata, private markers and previous output. Reproduce the fixture
twice from the retained directory, check source hashes and inspect the projection.
Verify a resolvable binding and a no-namespace catalog imported alongside
an absent UOM declaration. The retained release contributes 138 enum types,
733 literals and 171 source digests. Its element references lack a verified
namespace owner, so its binding map is empty. Run the existing vocabulary
gate and check the fixture-specific inventory of 171 reviewed digest entries.
