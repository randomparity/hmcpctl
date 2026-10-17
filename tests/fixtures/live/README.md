# Captured HMC fixtures

Each file here is one request and the HMC's answer, taken for issue #1161:
REST answers and the refcode listing from captures recorded with
`scripts/live_test/capture.py`, and two CLI listings (`cli-io-slots.json`,
`cli-lpar-uuid-name.json`) from raw read-only command output. The captures were
taken on an HMC at V10R3 managing POWER9 hardware; `docs/api-patterns.md`
records the patterns they show. REST and CLI captures whose `capture` begins `2026-09-30-ro/` come
from the read-only sweep for #1202, on the same HMC and system; a nonzero-exit one is
raised in tests with `live_process_error(name)`. Captures whose `capture` begins
`2026-09-30-v11r2-p9/`, `2026-09-30-v11r2-p11-9824/` or `2026-09-30-v11r2-p11-9242/` come
from the same sweep on HMCs at V11R2 SP1120 managing a POWER9 9009-42A, a POWER11 9824-42A
and a POWER11 9242-21B.

The VIOS and storage REST captures from that sweep are `rest-lpar-path-vios`,
`rest-lpar-quick-vios`, `rest-vios-*`, `rest-ms-vios-feed-media` and
`rest-volume-group`, with the same `capture` prefix. The other VIOS files ending
`-v11r2` begin `2026-09-30-v11r2-p11-9824/`: the same sweep on an HMC at V11R2
SP1120 managing a POWER11 9824-42A.

`rest-vios-create` and `rest-vios-create-lpar-path-refused` (`capture` prefix
`2026-10-01-1214/`) come from an operator-authorized mutation window for #1214 on
the V10R3 HMC and POWER9 system above: a VIOS create PUT to each collection, read
back and then deleted.

`cli-memopt-resgroup-calc` (`capture` prefix `2026-10-05-v11r2-p11-9824/`) is a read-only
capture for #627 on the V11R2 HMC and POWER11 9824-42A system above.

`rest-lpar-feed-per-system` (`capture` prefix `2026-10-12-q466/`) is a read-only capture for
#466 on the V10R3 HMC: a `ManagedSystem/<uuid>/LogicalPartition` feed of one running and one
not-activated partition, from a system other than the #1161 one. Its partition names are
`sys-R13-lp1` and `sys-R13-lp2`, renamed from the exporter's output because the exporter keeps a
name's suffix and these were not synthetic, and its `MACAddressPrefix` values and one `Uptime` are zeroed. Its token map is its own, as for the exports above.
`docs/workflow/lpar-parent-system-survey-466.md` records what the capture shows.

`cli-refcodes-unknown-lpar` (`capture` prefix `2026-10-12-q893/`) is a read-only capture for #893
on an HMC at V10R3 SP1060 managing a POWER8 8247-22L: the reader's `lsrefcode` command against a
synthetic partition name the HMC does not know.

`rest-vios-scsi-mapping-unmapped-adapter` and `cli-vios-scsi-unmapped-adapter` (`capture`
prefix `2026-10-13-q1250/`) come from an operator-authorized window for #1250 on the V10R3 HMC
and POWER9 system above. One vSCSI server adapter was DLPAR-added to the VIOS toward the test
partition with no mapping, read while it existed, and removed. The REST body is the VIOS's
`ViosSCSIMapping` group, and the listing is the #1237 `lshwres` command. Its `MACAddressPrefix`
is zeroed, which the exporter does not do. Their token map is their own, as for the exports above.

`rest-user-create-remote-user-id-refused` and `rest-user-profile-kerberos` (`capture` prefix
`2026-10-13-q1414/`) come from an operator-authorized window for #1414 on the V10R3 HMC, each
run making at most one disposable Kerberos-type user with a synthetic principal, then deleting
it. The first is hmcpctl's create refused with REST0001 because it sent `RemoteUserID` after
`AssociatedTaskRole` (`probe.capture.jsonl`). The second is the profile hmcpctl created once
that was fixed, with no description given (`probe3.capture.jsonl`); its `desc-1` was `HMC User`
before tokenizing. The exporter's e-mail rule rewrote the principal to
`hmcpctl-test/user@example.test`. The HMC serves `RemoteUserID` with a trailing newline that the
create did not send; the fixture keeps it. hmcpctl's shared parser strips leaf text on read, so
callers receive the principal without it (ADR 0076, #1538). Their token map is their own, as for
the exports above.

`rest-ms-quick-all-no-connection-v11r2` is record `#5` of #1290's read-only 2026-10-13 No
Connection sweep of its second HMC (V11R2 SP1120): `ManagedSystem/quick/All` while two of its
eleven systems read `State` `no connection`; its `sys-R` tokens are local to this file.

`rest-ms-quick-all-mtms-pair` and `rest-ms-entry-mtms-pair` (`capture` prefix `2026-10-14-q1534/`)
come from an operator-authorized read-only window for #1534 on the #1161 V10R3 HMC: its
`ManagedSystem/quick/All` and the full `ManagedSystem/{uuid}` entry of one of the systems it lists,
`sys-R1`, an 8375-42A, read in one session. Their token map is their own, as for the exports
above; never join them to other fixtures by token. Tokenizing replaces both serials with
`<REDACTED-SERIAL>`, so the comparison that matters was scripted over the raw capture before
export: the type, model and serial `_summary_mtms` parses from quick/All (`#2`) equal what
`mtms_parts` reads from the entry (`#3`), and from each of the sixteen entries of the full
`ManagedSystem` feed read in the same session (`#4`, not committed). The entry's `WWPNPrefix` was
zeroed by hand before #1639; the exporter now writes `<REDACTED-DEVID>` there.

## Format

Every file is a JSON object with these keys:

- `capture`: the source record, as `<capture file>#<line>` or a named raw
  output. The raw captures stay private to the operator, so this field is the
  citation, not a link.
- A REST capture has `method`, `path`, `status`, `content_type` and `body`.
- A CLI capture has `command`, `exit_status`, `stdout` and `stderr`.

Tests read a file with `live_fixture(name)` from `tests/conftest.py`, or with
`live_response(name)`, which also returns the captured path and an
`httpx.Response` for a respx route.

## Tokenization

Identifiers are replaced consistently across files, so a value that appears in
two captures has the same token in both:

- UUIDs become `NNNNNNNN-abcd-4ef0-8abc-NNNNNNNNNNNN`, keeping the case the HMC
  printed. LPAR UUIDs are upper case, as V10R3 prints them.
- The managed system is `sys-R1`, and partition names keep their suffix
  (`sys-R1-lp3`, `sys-R1-vios1`).
- HMC user names and user descriptions become `user-N` and `desc-N`.
- The HMC host is `hmc.test:443` (the port in an echoed `Host` header is the
  one the capture used), and the client IP address is `192.0.2.1`.
- A location code's unit prefix is `<REDACTED-LOC>`; its slot suffix is kept,
  except a disk WWN or array identifier inside it (`-L<id>-L0`), which is
  `<REDACTED-DEVID>`. Device identifiers that carry a disk or volume group serial
  (`UniqueDeviceID`, `VolumeUniqueID`, `DescriptorPage83`, `GroupSerialID`) are
  `<REDACTED-DEVID>` whole.
- Serial numbers, session tokens and cookies are `<REDACTED-SERIAL>`,
  `<REDACTED-SESSION>` and `<REDACTED-COOKIE>`. Inside an XML body the token is escaped
  (`&lt;REDACTED-SESSION&gt;`), so the body stays well-formed XML and parses
  as the HMC's original did.

Nothing else in a body is changed. A new or changed HMC-shaped fixture cites the
capture it came from.

## The 2026-09-30 export (#1202)

Fixtures whose `capture` begins `2026-09-30-ro/` come from a read-only sweep of
that day: every read-only MCP tool and about 70 raw GETs and `ls*` commands,
against the same V10R3 HMC and POWER9 system as the #1161 captures. The raw
records were tokenized as one corpus by the prototype of
`scripts/live_capture_export.py`, so tokens are consistent across fixtures from
that export, and the system and test partition keep the names above (`sys-R1`,
`sys-R1-lp3`).

**Its UUID token map is separate from the #1161 one.** Both use the
`NNNNNNNN-abcd-4ef0-8abc-NNNNNNNNNNNN` form, numbered from 1 in each export, so
the same token in a #1161 fixture and in a `2026-09-30-ro/` fixture can name
different objects, and the same object can carry different tokens. Never join
fixtures across the two exports by UUID. Other names in that export (`lpar-N`,
`prof-N`, `vg-N`, `dev-N`, ...) are tokens of its own map too.

`vocabulary/` holds what was derived from the read-only corpora, not captured
records: one vocabulary and one schema enum list per HMC release and system.
From the 2026-09-30 prototype export: the `v10r3-p9` enum list and the V11R2
pairs `v11r2-p9-9009-42a`, `v11r2-p10-9028-21b`, `v11r2-p11-9824-42a` and
`v11r2-p11-9242-21b`. From the first run of the tracked sweep and exporter on
2026-10-01: `v10r3-p8-8247-22l`, `v11r2-p10-9080-hex` and `v11r2-p10-9043-mrx`
(from one HMC; see below). From a 2026-10-13 sweep run
while the 9043-MRU was `No Connection` for the whole capture:
`v11r2-p11-9043-mru`, whose HMC-wide reads also hold the HMC's other managed
systems. From the same session's sweep of a second
V11R2 HMC, with a 9105-42A `No Connection` throughout: `v11r2-p10-9105-42a`, derived
for #1498 once the exporter stopped binding an element to an enum its name does not
relate to. It still infers `Status` → `VirtualNICBackingDeviceStatus.Enum` from
`LINK_DOWN` and `OPERATIONAL`, because the exporter binds by local name; the gate
ignores that inferred binding, as the documented job statuses state `Status`. Its raw
captures (`~/hmc-live-evidence/2026-10-13-q1290/`), corpus and the `export.sh` that
re-runs the derivation (`~/hmc-live-evidence/2026-10-13-q1498/`) are private, on the
live-test host. Beside them sit the
documented job statuses (`enums-documented-jobs.json`, names cited to
`docs/refs/`, each marked captured or documented-only), the stated bindings
(`enums-stated-bindings.json`, the eighteen element-to-enum bindings #1516 dropped,
each citing that PR's table) and the allowlist of
`just live-vocabulary`. Each vocabulary names its sources.

`v10r3-p9.json` was re-derived on 2026-10-13 (#1310) from one corpus, tokenized
in one pass from four raw captures of the same HMC: a tracked read-only sweep
that day, the 2026-09-29/30 #1161 and #879 mutation windows (where its job
statuses come from), the 2026-10-12 #1242 PCM metric-feed reads, and the
2026-10-13 #1250 window's reads. The #1161/#879 captures predate the tracked
harness, so their REST rows have a null `accept` and `content_type`, and their
CLI records contribute nothing. The derivation replaced the 2026-09-30 sweep and
the lost window fold, so rows only those held are gone; the #1310 pull request
lists them. The raw captures, the corpus and the `export.sh` that re-runs the
derivation are private, on the live-test host under
`~/hmc-live-evidence/2026-10-13-q1310/`.

`v11r2-p10-9043-mrx.json` holds one HMC's capture: the second of the two HMCs that
manage that system. It was derived on 2026-10-01 from that HMC's corpus with
`--fold` naming the first HMC's vocabulary, which the tracked exporter derived in
the same run, and `sources` gained an entry for the fold. The fold read only the
prototype's top-level `rest_values` and `rest_endpoints` keys, which the exporter's
own `rest.values` and `rest.endpoints` output never carries, and it added every
endpoint it read with a null `accept` and `content_type`; none of the file's 51
endpoints has a null `accept`. The fold added nothing, so on 2026-10-14 (#1561) its
`sources` entry was removed by hand. No row changed. Re-deriving the pair from both
HMCs' raw captures, with one `--source` each, would add any rows only the first
HMC's capture holds.

Regenerate them with `scripts/live_capture_export.py`, as
`docs/live-testing.md` ("Capturing an HMC's vocabulary") describes; do not edit
them by hand, apart from an allowlist entry's `reason` (the #1561 `sources`
correction above is the one exception). To add a new capture to
a pair that already has a vocabulary, follow that page's "Adding a capture to an
existing pair": add the raw file to the pair's export inputs and re-derive.
