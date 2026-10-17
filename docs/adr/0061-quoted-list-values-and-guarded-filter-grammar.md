# 0061 — Quoted list values and a guarded filter grammar on HMC CLI records

## Status

Accepted (2026-08-21). Amends ADR 0045 in part: the "the grammar rejects; it does not encode"
clause no longer governs attributes whose HMC-side value grammar is itself comma-separated.

## Context

ADR 0045 gave `build_attribute_record` sole ownership of the HMC `-i` attribute-record grammar
and chose rejection over encoding because IBM's double-quote convention had to agree across
three parsers (`shlex.quote`, the remote shell, the HMC) and no live HMC was available to
verify that agreement. On that reasoning `chhwres -a` sites were deferred to issue #285,
because `backing_devices` — whose own value grammar is a comma-separated device list — sits in
direct conflict with a blanket comma rejection.

Live verification now exists (recorded on #285, 2026-08-21, HMC V10R3M1060 and V11R2M1120):

- A bare comma inside `backing_devices=` misparses: the HMC's `-a` parser reads the second
  device token as a new attribute pair, finds no `=`, and rejects the whole record with a
  format error. Multi-device input is therefore *currently broken*, not merely injectable.
- The IBM quoted-pair form — `-a "port_vlan_id=0,\"backing_devices=dev1,dev2\""` — is accepted;
  the error in that probe was a capacity constraint, not a format error. The quoting rule IBM
  documents for `chsyscfg` lists applies to `chhwres -a`. The probe verified the pair
  separator `=` inside the quoted region; it did not probe an additional `=` inside a value,
  a quoted *single*-device value, or a quoted pair in any position other than last.

Separately, the `--filter name=value` selections on `lssyscfg`/`lshwres`/`chhwres` commands are
built by f-string from caller-supplied names. A value carrying `,` or `=` adds or rewrites a
filter pair, so a mutation acts on a partition the caller did not name — the selection half of
the same boundary ADR 0045 closed for the mutation half.

## Decision

**The builder gains an evidence-bounded quoted-pair form.**
`build_attribute_record(pairs, *, quoted=())` accepts the names of list-valued attributes. A
marked attribute whose rendered value contains a comma is emitted as `"name=v1,v2"` — literal
double quotes around the pair, per IBM's convention. A marked attribute without a comma is
emitted bare, byte-identical to today. Inside a quoted value the comma is permitted because the
live probes verified exactly that; the pair's own `=` separator inside the quoted region is
likewise probe-verified, but an `=` supplied inside a caller's *value* is not, so caller values
are still refused — as are `"` and control characters, whose behaviour inside a quoted region
nothing has probed. Quoting is conditional, not unconditional, precisely because the quoted
single-device form was never probed: no currently-working command changes a single byte.

**Every `chhwres -a` record routes through the builder.** `add_vnic_backing` builds
`port_vlan_id`/`backing_devices` through it, marking `backing_devices` quotable. No caller on
this branch can currently deliver a multi-device value — ADR 0057 deliberately replaced the
opaque `backing_devices` string with a typed single-device selector — so the quoted branch is
forward-looking infrastructure, adopted here because the issue asked for an explicit decision
on `backing_devices` and the operator chose the encode path once the quoting mechanism was
live-verified. The value-form site `chhwres -r mempool -o r -a <pool_name>` is not an attribute
record — `-a` there carries a bare pool name, not `name=value` pairs — so it keeps its plain
value, validated against the same delimiter table, and the recurrence guard exempts it
explicitly rather than pretending it is a record.

> **Amended by #1245** (2026-10-13): the value-form site no longer exists. Since PR #1488 the
> memory-pool remove runs the documented nameless form `chhwres -r mempool -m <system> -o r`,
> with no `-a <pool_name>`. The recurrence guard's value-form exemption, described below, was
> removed in the same change, so the guard now requires every `chhwres -a` payload it selects
> to come from the builder.

**The filter half gets its own builder from the same table.** `build_filter(pairs)` takes
`(name, value)` pairs, refuses record delimiters and control characters in each value, and
joins them comma-separated — the shape `--filter lpar_names=x,profile_names=y` already uses.
Every `--filter` site in `src/hmc_mcp/` and `scripts/` routes through it. This deliberately
does not reproduce the filter grammar's other half: IBM documents comma-separated *lists*
inside one filter value (`--filter "lpar_names=lpar1,lpar2"`), and until a probed encoding
exists for that form the builder refuses it, exactly as it refuses a comma in a record value.
Every site this change migrates selects a single resolved object by name, so nothing reachable
narrows; the refusal keeps a future multi-value site from reaching an unguarded escape hatch.

**The recurrence guard widens with the grammar.** `tests/unit/test_i_record_grammar.py`
selects `chsyscfg`/`mksyscfg` `-i`, `chhwres` `-a`, and any literal carrying `--filter`, and
requires each payload to come from the owning builder. As decided here, the guard also named
the one value-form exemption rather than silently allowing it; #1245 removed that exemption
along with its site (see the amendment above). All three selections key on flag-carrying segments
rather than on a literal opening with the command name: ADR 0045's opening rule let the
appended-fragment idiom (`cmd += f" -a {…}"`) evade the scan, and this branch's own filter
sites use that shape. The flag must be a whitespace-preceded token, so bare diagnostic labels
are not selected; prose that trips the scan fails closed like any unguarded site. Element-wise
assembly (`" ".join(["chhwres", "-a", payload])`) remains outside any literal selection — the
same bound ADR 0045 records for its opening rule, inherited unchanged here.

## Consequences

**Public input contracts narrow, fail-closed.** A name or id containing `,`, `=`, or `"` that
today reaches an `--filter` or `-a` payload and corrupts it is refused before the command is
built, with `HMCCLIError` naming the field and character. Multi-value filter lists
(`lpar_names=a,b`) join that refusal until their encoding is probed. Names arriving from HMC
resolution are unaffected unless an HMC object name genuinely contains a delimiter — the same
bet ADR 0045 made, now applied to the selection half.

**Multi-device vNIC input becomes expressible at the builder, not yet reachable from a tool.**
A `backing_devices` value with a comma renders quoted instead of misparsing; the injection
demonstrated on #285 closes with the fix rather than by refusal. Reaching that capability from
an MCP tool wants a typed multi-device selector, which is ADR 0057's surface, not this record's.

**Emitted commands are byte-stable except where they were broken.** Single-device payloads and
every well-formed filter render exactly as before; only delimiter-carrying values produce
different text, and those commands never worked.

**Unverified, carried deliberately**: a quoted single-device value; `=` or `"` inside a
caller's value portion of a quoted region. The non-trailing quoted position is not merely
carried — the builder refuses it, applying the same fail-closed rule as the multi-value
filter refusal: the probes verified the quoted pair only as the record's final element, so a
quotable comma-carrying attribute followed by another pair is refused before dispatch. If a
live HMC ever shows one of the remaining forms failing, the fix is a builder rule, not a
grammar change.

**ADR 0045 otherwise stands**: one builder owns the grammar; duplicates are refused; space and
semicolon remain non-structure; the backslash residual remains open.

## Considered & rejected

**Do nothing** — leave `add_vnic_backing` on its f-string and keep `--filter` unguarded. It is
the cheapest option and closes nothing: the injection reproduced on #285 stays reachable
through every listed tool, which is the condition the issue reports.

**Route through the builder with plain ADR 0045 rejection; defer quoting** — the smaller wire
change, and sufficient today because the only producer emits a comma-free single device. Passed
over because the issue required an explicit decision on `backing_devices`, the operator chose
the encode path (decision recorded on the charter, 2026-08-21), and the quoting mechanism is
now probe-verified rather than guessed; deferring would re-open this exact question when the
first multi-device caller lands, with the evidence already on the shelf.

**Unconditional quoting of marked attributes.** Simpler to state, but it changes every
single-device command to a form no live probe exercised, and churns recorded-command fixtures
for zero security gain. Conditional quoting buys the same closure with byte stability.

**Per-attribute exemption for `backing_devices` with its own validator.** Would leave the
multi-device form broken (bare commas still misparse) and duplicate the delimiter table at a
second owner — the exact shape ADR 0045 exists to prevent.

**Extending the guard to `--filter` by hand-written per-site tests only.** Per-site tests
prove today's sites; the AST scan is what stops the next author writing the eighth f-string
site. ADR 0045's coupling test exists because per-site tests alone did not.
