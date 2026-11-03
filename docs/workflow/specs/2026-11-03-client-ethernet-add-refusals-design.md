# Client Ethernet add refusals and the VSI profile (#1864)

Status: design for #1864. Lane: full-spec. Complexity: M. Fixed design denominator: 250.
Authority: https://github.com/randomparity/hmcpctl/issues/1864#issuecomment-6592148124.
Decision record: [ADR 0315](../../adr/0315-client-ethernet-vsi-and-add-refusals.md).

## Problem

`hmc_add_network_adapter` and `hmcpctl adapter add-network` surface two HMC refusals as bare
HTTP 500s. One is HSCLB021 (RMC return code 83), which names the serving VIOS. The other is
REST0269, the partition-state refusal. Neither tells the caller what to do. The ST9 network arm
stops at the add, so its storage round trips have not run since 2026-11-02.

## Evidence (live capture, 2026-11-03, test HMC V10R3 M1060, `sys-R1`)

The test partition was Not Activated, and `lssyscfg` reported `curr_profile=default_profile`.
Each add used PVID 2 on trunked switch 0. The captured results:

| request (beside PVID and switch) | as found | after one `chsyscfg -o apply` |
|---|---|---|
| none | REST0269 | accepted |
| `addl_vlan_ids=[]` | REST0269 | accepted |
| `allowed_os_mac_addrs=all` | REST0269 | accepted |
| `qos_priority=none` | REST0269 | accepted |
| one `vsi_*` field alone (each of three) | REST0269 | REST0126: missing `vsi_type_id` (manager alone) or `vsi_manager_id` (type or version alone) |
| all six fields | REST0269 | REST0126 wrapping HSCLB021, naming the VIOS |

- Every accepted adapter was deleted by its own UUID.
- After every refusal, the adapter listing equalled the baseline.
- Conclusions:
  - The REST0269 refusal came from a partition whose current configuration had not been applied. One apply cleared it, even though `curr_profile` already named the profile.
  - The policy fields do not involve the VIOS.
  - A complete VSI profile is what reaches the VIOS.
- Not captured: the VSI triple without the policy fields, and the manager-and-type pair without a version. Unexplained: #1861's run 1 got HSCLB021, not REST0269, for all six fields on the same unapplied partition. That run used a newly created VLAN rather than an existing one. Both refusals are classified, so the order does not matter here.

## Scope

1. **VSI co-requirement.** `validate_network_adapter_creation_fields` refuses any selected
   `vsi_*` value unless both `vsi_manager_id` and `vsi_type_id` are selected. `vsi_type_version`
   stays optional, because no capture shows the HMC requires it. The MCP tool and the CLI already
   call this validator before any read. The tool and CLI help say that a VSI profile is registered
   through the serving VIOS over RMC.
2. **Classified add refusals.** `operations.virtualization.adapters.add_network_adapter` is the
   one owner that both the MCP and CLI paths reach. It re-raises an `HMCError` from the native add
   as a new `HMCError` with an actionable message when the status is 500 and the body names:
   - `HSCLB021`: the serving VIOS's RMC dependency. The message says that nothing was created,
     to check the VIOS RMC state and VSI support, or to retry without the `vsi_*` fields.
   - `REST0269`: the partition has no applied current configuration. The message says that
     nothing was created, to apply its profile once (`chsyscfg -r lpar -o apply`) or activate
     it, and that `curr_profile` alone does not prove the configuration is applied.

   The new error keeps `status_code` and `body`, and chains the original as `__cause__`. Any
   other error propagates unchanged, as the same object.
3. **ST9 documentation.** In `docs/live-testing.md`:
   - The ST9 preconditions gain the applied-configuration requirement.
   - The sentence about #1861's field bisect is replaced by its outcome.
   - ST9 keeps its four-field request. It sends no creation fields, because VSI needs a VSI
     manager (excluded) and the policy fields' proof is this capture.
4. **Fixtures.** Three tokenized captures in `tests/fixtures/live/`, with a README paragraph.
5. **Live proof.** One ST9 network-arm run on the PR head, under an orchestrator lease. It must
   pass the client adapter add and the vSCSI round trip, which discharges #1826's owed proof.

The following stay unchanged:
- Omission of unset fields. This is already pinned by `test_omitted_new_fields_keep_legacy_reads_and_result` and `test_network_adapter_minimal`.
- The update path, other adapter types, and VSI/VEPA support.

## Failure model

1. Actors and deployments:
   - an operator or MCP agent calling the add against a V10R3 HMC;
   - CI running fake-HMC tests.
2. Invariants and assets:
   - No refused add leaves an adapter behind.
   - A classified message never claims that a change happened.
   - Unclassified errors keep their identity, which existing callers and tests rely on.
3. Accepted:
   - Another HMC release may word REST0269 or HSCLB021 differently. The match is on the code
     token, and an unmatched body passes through unchanged.
   - The capture does not prove that a VSI profile without a version is accepted. The HMC still
     validates it finally.
   - The classified message keeps the HMC's own text through `body`.
4. Covered elsewhere:
   - Session values the HMC echoes in error bodies (follow-up candidate).
   - `addl_vlan_ids=[]` verified as `unavailable` (follow-up candidate).
   - ST9 recording a no-residue refusal as MANUAL (follow-up candidate).

## Success

1. A `vsi_*` field selected without both the manager and the type raises `ValueError` before any
   HMC read, for each of the three single-field selections.
2. The captured HSCLB021 and REST0269 bodies, raised from the native add, yield `HMCError`s that
   carry their actionable messages, status 500, the original body, and the original as cause.
3. A generic `HMCError` passes through as the same object.
4. `docs/tools/` is regenerated, and `just verify` is green.
5. The ST9 live run on the PR head passes the add and the vSCSI round trip.
