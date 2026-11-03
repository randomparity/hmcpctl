# Client Ethernet add refusals and the VSI profile (#1864)

Status: design for #1864. Lane: full-spec. Complexity: M. Fixed design denominator: 250.
Authority: https://github.com/randomparity/hmcpctl/issues/1864#issuecomment-6592148124.
Decision record: [ADR 0315](../../adr/0315-client-ethernet-add-refusals.md).

## Problem

`hmc_add_network_adapter` and `hmcpctl adapter add-network` surface two HMC refusals as bare
HTTP 500s. One is HSCLB021 (RMC return code 83), which names the serving VIOS. The other is
REST0269, the partition-state refusal. Neither tells the caller what to do. The ST9 network arm
stops at the add, so its storage round trips have not run since 2026-11-02.

## Evidence (live capture, 2026-11-03, test HMC V10R3 M1060, `sys-R1`)

The test partition was Not Activated, and `lssyscfg` reported `curr_profile=default_profile`. The capture did not read `resource_config`.
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

1. **VSI documentation, no new validation.**
   - The tool docstring and the CLI help for the three `vsi_*` fields will state the following:
     - the HMC requires `vsi_manager_id` and `vsi_type_id` together, and refuses a lone field with REST0126;
     - the HMC was observed to call the serving VIOS over RMC for a complete VSI profile.
   - Validation stays with the HMC (ADR 0307). Its REST0126 already names the missing field.
2. **Classified add refusals.**
   - `operations.virtualization.adapters.add_network_adapter` is the one owner that both the MCP
     and CLI paths reach.
   - When the native add raises an `HMCError` with status 500, it re-raises it as a new
     `HMCError` carrying an actionable message, chosen by the code the body names:
     - `HSCLB021`: the HMC's RMC call to the serving VIOS failed. The HMC makes that call to
       register a VSI profile. Check the VIOS's RMC state and VSI support, or omit the `vsi_*`
       fields.
     - `REST0269`: the partition has no applied current configuration. Check its
       `resource_config` (`lssyscfg -r lpar -F name,state,resource_config`; `1` means applied),
       then apply its profile once (`chsyscfg -r lpar -o apply`) or activate the partition, and
       retry. A `curr_profile` that names a profile does not prove the profile is applied. The
       `resource_config` link rests on #939's prior evidence; this capture proved the remedy.
   - Both messages say the HMC refused the add, and tell the caller to list the partition's
     adapters to confirm none was left. HSCLB021 is checked first, because the HMC wraps it in
     REST0126.
   - The new error keeps `status_code` and `body`, and chains the original as `__cause__`.
   - Any other error, including the REST0126 refusals of a lone `vsi_*` field, propagates
     unchanged as the same object.
3. **ST9 documentation.** In `docs/live-testing.md`:
   - The ST9 preconditions gain the `resource_config` requirement, and name ST10's restore as a
     known way to reset it.
   - Applying the profile is the operator's per-window decision. The arm never applies it.
   - The sentence about #1861's field bisect is replaced by its outcome.
   - ST9 keeps its four-field request (ADR 0315).
4. **Fixtures.** Three tokenized captures in `tests/fixtures/live/`, with a README paragraph.
   The capture recorded no response headers, so `content_type` is `null`.
5. **Live proof.**
   - Before the run, read the test partition's `resource_config`. If it reads `0`, ask the
     operator whether to apply the profile (exclusion 4 governs activation only).
   - Then run one ST9 network arm on the PR head, under an orchestrator lease. It must pass the
     client adapter add and the vSCSI round trip, which discharges #1826's owed proof.

The following stay unchanged:
- Omission of unset fields, already pinned by `test_omitted_new_fields_keep_legacy_reads_and_result` and `test_network_adapter_minimal`.
- The update path, other adapter types, and VSI/VEPA support.

## Failure model

1. Actors and deployments:
   - an operator or MCP agent calling the add against a V10R3 HMC;
   - CI running fake-HMC tests.
2. Invariants and assets:
   - A classified message never claims that a change happened, nor that none did.
   - Unclassified errors keep their identity, which existing callers and tests rely on.
3. Accepted:
   - Another HMC release may word REST0269 or HSCLB021 differently. The match is on the code
     token, and an unmatched body passes through unchanged.
   - The cause of the 2026-11-02 unapplied state is unknown. The capture did not read
     `resource_config`; the remedy was proven, not the cause.
4. Covered elsewhere:
   - Session values the HMC echoes in error bodies (follow-up candidate).
   - `addl_vlan_ids=[]` verified as `unavailable` (follow-up candidate).
   - ST9 recording a no-residue refusal as MANUAL (follow-up candidate).
   - vSCSI/vFC REST0269 classification (ADR 0315, consequence; exclusion 2).

## Success

1. The captured HSCLB021 and REST0269 bodies, raised from the native add, yield `HMCError`s
   that carry their actionable messages, status 500, the original body, and the original as
   cause. Each test is red before the change.
2. A generic `HMCError`, and the captured REST0126 VSI-incomplete body, propagate as the same
   object. This is a regression guard, green before and after.
3. `docs/tools/` is regenerated, and `just verify` is green.
4. The ST9 live run on the PR head passes the add and the vSCSI round trip.
