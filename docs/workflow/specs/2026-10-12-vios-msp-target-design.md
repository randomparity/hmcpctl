# Let `hmc_set_lpar_msp` resolve and authorize a VIOS target (#1318)

## Authority and scope

Frozen scope: issue #1318, token `q1318-f68d6cc8` (WORK:SCOPE on the issue). Campaign
dispatch; the operator approved the exclusions on 2026-10-12 and delegated two decisions
to this design: the VIOS authorization rule, and non-VIOS refusal behaviour.
One PR; non-trivial/full-spec; M/250 changed-line denominator; iterating review.
Excluded: VIOS ownership stamping; widening proc_compat, sync or modify to VIOS; a live
MSP round trip on a VIOS (recorded evidence gap); renaming the MSP tools; reworking
`resolve_and_authorize_lpar_names` for its other callers.

## Problem

`msp` is valid only on a Virtual I/O Server. `configure_lpar_msp` authorizes through
`resolve_and_authorize_lpar_names`, which resolves only through the `LogicalPartition`
feed, so a VIOS raises `No LPAR named ...` before any command runs. The CLI `hmc lpars
set-msp` calls the SSH helper directly and crosses no ownership check at all, against
ADR 0092 §1's rule that the guard lives in the operations layer.

## Decisions

1. **VIOS authorization rule.** The MSP target resolves through the `VirtualIOServer`
   feed of the selected managed system and crosses the same description-stamp rule ADR
   0092 applies to LPARs (`authorize_lpar_mutation`): an unstamped description proceeds,
   a foreign or malformed stamp is refused before the write, and `ownership_override=true`
   skips the read and emits the existing `ownership-override` audit event. ADR 0011 never
   stamps a VIOS, so the common outcome is "unowned, proceed"; the check still refuses a
   stamp an operator wrote by hand and costs one SSH read on a low-frequency call.
2. **Containment.** A name is looked up in the selected system's `VirtualIOServer` feed
   (one `list_vios(system_uuid)` read): a UUID matches an entry's `UUID` case-insensitively,
   mirroring ADR 0094's `_hosts_partition`; a name matches `PartitionName` exactly (names
   are unique within one system). The CLI name is the matched entry's `PartitionName`.
3. **Non-VIOS selector.** A selector that misses the system's `VirtualIOServer` feed is
   refused before any SSH command with a `ResourceNotFoundError` naming the missing
   Virtual I/O Server (`lpar_env=vioserver`) on that system and pointing at
   `hmc_list_vios`; the tool docstring already states `msp` is VIOS-only. The live non-VIOS refusal check (`scripts/live_test/arms/lpar.py`) matches
   `vioserver` in the error and keeps passing unchanged. The SSH `lpar_env` probe in
   `ssh/profiles.py::set_lpar_msp` stays as a second line.
4. **CLI parity.** `hmc lpars set-msp` delegates to `configure_lpar_msp` through
   `with_client` and gains `--ownership-override`, as `set-description` already does.
5. **ADR 0092 amended in place**: §1 names `configure_lpar_msp` as the one VIOS-targeting
   operation it governs and states the rule above; the §3.2 row cites the new helper.
   No new ADR: the decision extends 0092's own rule to one more resource type.

## Architecture and ownership

Owner: `operations/lpar/configuration.py` gains a private
`_resolve_and_authorize_vios_names(hmc, system_name_or_uuid, vios_name_or_uuid, *,
ownership_override) -> tuple[str, str]` beside its sole caller `configure_lpar_msp`; it
reuses `resolve_system_uuid`, `_hosts_partition`, `_resolve_system_name` and
`authorize_lpar_mutation` unchanged, and the client's `list_vios`. It lives there, not in
`ownership.py`, because the capability inventory fingerprints each operation's module
closure: an addition to `ownership.py` marked the unrelated `boot_order.*` live evidence
stale, while `configuration.py`'s operations were already stale. Callers: MCP
`hmc_set_lpar_msp` (unchanged) and CLI `lpars_set_msp` (migrated).
No obsolete path remains; the LPAR helper keeps its other callers.

## Failure model

1. Actors and deployments: a local operator via CLI; an MCP client under the server access
   policy; a `hmcpctl.api`/operations caller. All against one HMC with credentials in config.
2. Invariants and assets: a VIOS owned by another agent's stamp is never mutated without an
   audited override; an MSP write never lands on a partition not hosted by the selected
   system; the VIOS's I/O service availability (MSP only flags LPM eligibility).
3. Accepted failure classes:
   - TOCTOU between the description read and `chsyscfg` — same window as every ADR 0092
     guard; accepted by ADR 0092.
   - A VIOS renamed between REST resolution and SSH — bounded: the SSH write fails `HSCL8012`.
   - A stale-but-unstamped VIOS proceeds: ADR 0011 does not stamp VIOS; ownership is advisory.
4. Covered elsewhere: who may call the tool (#218 server access policy, ADR 0038/0039);
   live VIOS evidence (recorded gap, operator exclusion).

### Threat model

- Boundaries: caller-supplied `system_name_or_uuid` and `vios_name_or_uuid` (existing,
  now resolved through a second feed); the VIOS description read (existing SSH path).
  Nothing new is added; the CLI path is narrowed by gaining the guard. The tool keeps
  `target_kind="lpar"` (unchanged; not in this charter's surface), so a #218 policy grant
  names a VIOS under `lpar` targets; the kind mismatch is a reported follow-up candidate.
- Actor: an authorized caller, or an agent sharing the HMC, naming a VIOS it does not own,
  or pairing a UUID with the wrong system.
- Controls: feed containment (decision 2); stamp rule (decision 1); `shlex`-quoted SSH
  commands and `build_attribute_record` (unchanged); override audit.
- Out of scope: VIOS stamping (excluded); policy grants (covered by #218).

## Success

1. VIOS by name on the selected system reaches `chsyscfg ... msp=<0|1>`.
2. VIOS by UUID reaches the write; a UUID absent from that system's VIOS feed is refused.
3. A foreign or malformed stamp is refused before the write; unstamped proceeds.
4. Override skips the description read and audits.
5. A non-VIOS or unknown selector is refused before any SSH command, message naming
   `vioserver`.
6. CLI `set-msp` crosses criteria 3–4 and forwards `--ownership-override`.
7. ADR 0092 §1 and §3.2 amended; `just verify` and `prek run --all-files` green.

## Validation

Focused tests in `tests/lpar/test_lpar_msp.py` (criteria 1, 2, 5),
`tests/lpar/test_reconfiguration_ownership.py` (3, 4), `tests/app/test_cli_commands.py`
and `tests/app/test_cli_verbatim_output.py` (6); `tests/unit/test_adr_0092_citations.py`
(7, citation resolves). ADR prose is review-only.
