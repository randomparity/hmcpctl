# Operation details

[Documentation index](index.md) · [MCP server guide](mcp-server.md)

For operation signatures, see the [MCP tool reference](tools/index.md).
For shell examples, see the [CLI guide](cli.md).

### Collection limits

Feed-backed collection tools accept an optional client-side `limit`. The complete HMC feed is
still transferred and parsed before the result is truncated: the limit bounds only the number of
entries returned to the agent, not HMC work, network bytes, parsing cost, or the size of each
entry. The affected collection tools are unbounded when `limit` is omitted.

### Public parameter units and selectors

Storage quantities use binary-unit suffixes: virtual-disk create and attach inputs use
`capacity_mib`, while VolumeGroup inventory returns `capacity_gib` and `free_space_gib`.
An impossible VolumeGroup free-space reading is returned as null with
`free_space_diagnostic="free_space_exceeds_capacity"`. `size_mib` names media-repository
and optical-media quantities, and `lu_size_gib` names Shared Storage Pool logical units.
Numeric virtual-switch selectors are named
`virtual_switch_id`. vNIC mutations instead select the backing VIOS,
SR-IOV adapter, and physical port; they do not accept a virtual-switch selector.

The NIM install tools distinguish `hmc_timeout_minutes` from the client-side
`wait_timeout_seconds`. With `wait=True` and no explicit client budget, the
client waits for the HMC timeout converted to seconds plus one polling interval,
so it can observe the terminal state at the HMC deadline. LPM's separate
`wait_time` value is an HMC migration/validation field measured in seconds.

Every partition tool accepts an optional `system_name_or_uuid`
(SystemName or UUID) that disambiguates its `lpar_name_or_uuid`; omitted, the
name is searched fleet-wide. The LPM tools take it as the *source*-system
selector beside their required `target_system_name_or_uuid`, so a policy table
must match both endpoints against its `managed_system` allowlist.

> **SSH/CLI tools** — some tools reach the HMC by running CLI commands over SSH
> rather than through the REST API.
> Their system/LPAR arguments (`system_name_or_uuid` / `lpar_name_or_uuid`)
> accept either a CLI name or a UUID. Names are used as-is; UUIDs are resolved
> to their CLI names via the REST API first, falling back to an `lssyscfg` name
> lookup over SSH when the REST API is unreachable. Resolution happens before
> the command runs, so a UUID that cannot be resolved surfaces as an error
> rather than being passed through to the CLI. VIOS backup and restore are an
> exception: a direct system name plus VIOS UUID is SSH-ready, but a system UUID
> requires REST to resolve its unique MTMS identity and has no `lssyscfg`
> fallback. Separately, the opt-in `hmc_run_command` tool runs whatever command
> you give it verbatim without selector resolution.
> See [docs/hmc-cli-cheatsheet.md](hmc-cli-cheatsheet.md) for a concise
> reference to all HMC CLI commands used by this project.

`hmc_backup_vios` and `hmc_restore_vios` retain required managed-system and VIOS
selector metadata for authorization diagnostics and audit. Because their `ssp`
mode can affect the wider cluster, both tools are non-exhaustive and require
`targets = "all-targets"`. Prefer a grant that explicitly names only the needed
tool. An effect-class grant with `all-targets` can reach either tool; a targets
table cannot authorize either one even when it contains both selector kinds.

The vNIC mutation contract is admitted only for POWER9 8375-42A managed by HMC V10R3
M1060. The admission is a code gate, not a live-verification claim: no release claims these
operations ([what 0.2.0 claims](compatibility.md#what-020-claims)). `hmc_add_vnic` selects one backing with `vios_name`, `vios_lpar_id`, `adapter_id`,
`physical_port_id`, and `capacity_percent`, plus the vNIC `port_vlan_id`. The HMC assigns the
logical-port ID; callers do not supply one. A successful or unchanged result reports the assigned
ID in `backing_after[].logical_port_id` after correlated HMC readback. `hmc_remove_vnic` accepts
the `slot_num` reported by `hmc_list_vnics`, verifies its single active Operational backing, and
removes that slot.

Add and remove return the stable fields `operation`, `mutation_dispatched`, `changed`, `selector`,
`slot_num`, `vnic_before`, `backing_before`, `vnic_after`, `backing_after`,
`vnic_after_read_succeeded`, `backing_after_read_succeeded`, `output`, and `errors`. Once a
mutation has been dispatched, a command or reconciliation failure raises a structured partial
error carrying the same result evidence; there is no rollback promise. An empty inventory with
its matching read-succeeded flag set means verified absence.

`hmc_update_vnic_policy` (`hmcpctl network update-vnic-policy`) changes one policy field of an
existing vNIC selected by `slot_num`, offering only the modes qualified natively (ADR 0332): set
`pvid_priority` or `auto_priority_failover`; set `port_vlan_id` from 0 while allowed VLAN IDs are
`none`; or add members to a finite allowed VLAN or OS MAC list. Replacing or removing list
members, `all`/`none`, and combined changes are refused before any effect, and a change already in
place returns without dispatch. Every update also submits the current `auto_priority_failover`,
because the HMC resets an omitted value to 1. It submits once and returns `submission`
(`not_submitted`, `completed`, `rejected`, `unknown`), a selected-policy `readback`, and a
`preservation` check over every other cell of the partition's vNIC rows; anything short of
completed, verified and unchanged raises the same structured partial error.

| vNIC capability | Admitted | Notes |
|-----------------|----------|-------|
| One SR-IOV backing on POWER9 8375-42A / HMC V10R3 M1060 | Yes | Exact family and HMC level only |
| Caller-selected logical-port ID | No | The HMC allocates and readback reports it |
| Multiple backings or failover topology | No | Ambiguous or degraded topology fails before mutation |
| Priority or maximum-capacity inputs | No | HMC defaults remain in effect |
| Other server families or HMC levels | No | Capability checks fail before mutation |
| Rollback after a dispatched mutation | No | Partial errors retain observed before/after evidence |
| Policy update: replace, remove, `all`/`none`, combined changes | No | Refused natively or not qualified (ADR 0332) |

> **PCM notes**: metrics are stored as *JSON*, reached via an Atom feed of
> links. The `*_metric_links` tools return the link list, while the `*_metrics`
> tools download the most recent document (or `{}` when none are in range). The CLI
> `metrics show` accepts `--fetch` to do both in one step. Logical-partition
> processed and aggregated metrics require the owning managed system through
> `system_name_or_uuid` (MCP) or `--system` (CLI); their endpoint is nested
> below that system. Long-term
> monitoring + aggregation must be enabled via `hmc_set_pcm_preferences`
> before processed/aggregated metrics accumulate. Preferences and raw Long
> Term Monitor feeds are documented only for `ManagedSystem`, not
> `LogicalPartition`.

`hmc_update_firmware` accepts IBM's nested `PlatformUpdateParameter` JSON.
It rejects older HMC releases before submitting the destructive operation.
For example, a system-firmware update is:

```json
{
  "system_name_or_uuid": "system1",
  "platform_update": {
    "SystemFirmwareUpdate": {
      "UpdateType": "Update",
      "UpdateOrder": 1
    }
  }
}
```

`hmc_vios_update` uses IBM's `UpdateVIOS` parameter names. Every source requires
`ResourceType`; updates accept `HMC`, `NFS`, `SFTP`, `USB`, or `IBMWebsite` and may
include `RestartVIOS`. `hmc_vios_upgrade` accepts the `HMC`, `NFS`, `SFTP`, and `USB`
upgrade sources, each of which requires `Disks`.

HMC sources require `Name`, NFS/SFTP sources require `ServerHostOrIP` and
`RemoteDirectory`, USB sources require `USBDevice`, and every upgrade requires
`Disks`. Setting `SaveFile` to `true` also requires `Name`.

```json
{
  "vios_name_or_uuid": "vios1",
  "repository": {
    "ResourceType": "NFS",
    "ServerHostOrIP": "repo.example.com",
    "RemoteDirectory": "/images/vios",
    "Name": "update.iso",
    "RestartVIOS": "false"
  }
}
```

```json
{
  "vios_name_or_uuid": "vios1",
  "kind": "upgrade",
  "repository": {
    "ResourceType": "HMC",
    "Name": "install.iso",
    "Disks": "hdisk1"
  }
}
```

With `wait=true`, a terminal job's documented `stdOut` result is also exposed
at the top level when present. Invalid or cross-operation parameters are
rejected before connecting to the HMC.

Normalized PCIe inventories share this envelope:

- `resource_kind`: `dedicated_slot`, `sriov_adapter`, `sriov_physical_port`, or
  `sriov_logical_port`;
- `capability`: `available` or `capability-unavailable`;
- `system`: the resolved managed-system name;
- `selector`: nullable `adapter_id`, `physical_port_id`, and `logical_port_id` values copied from
  the request;
- `items`: stable records for the selected resource kind; and
- `unavailable_reason`: `null` when available, otherwise a stable evidence-bound explanation.

Dedicated-slot identity is `(system, drc_index)`. Its `description` and `owner_lpar` come from the
exact ADR 0053 projection; `availability` remains `null` because an empty owner does not prove that
a slot is assignable. SR-IOV identities are `(system, adapter_id)`, adapter identity plus
`physical_port_id`, and adapter identity plus `logical_port_id`. Their mode, availability,
ownership/use, location, capacity, and compatibility category fields are explicit nullable values.
Percentage fields use decimal percentages, never bytes, bandwidth, or integer weights.

The SR-IOV collections answer only on the HMC level and managed-system model pairs whose captures
admit them ([ADR 0183](adr/0183-sriov-read-envelope-from-captures.md)):

| HMC level | Model | Admitted reads |
|-----------|-------|----------------|
| V10R3 M1060 | 8375-42A | adapters, physical ports, logical ports |
| V11R2 SP1120 | 9009-42A | adapters, physical ports, logical ports |
| V11R2 SP1120 | 9824-42A, 9242-21B | adapters only |

Elsewhere a collection returns `capability-unavailable` with no items, and `unavailable_reason`
names the admitted pairs or, on an admitted model without that read, says why. It reads the HMC level and the system's model to decide, and runs no
inventory command.

The CLI equivalents are `hmcpctl network list-dedicated-pcie-slots`, `list-sriov-adapters`,
`list-sriov-physical-ports`, and `list-sriov-logical-ports`. All accept `--json`; the SR-IOV
commands accept the applicable `--adapter-id`, `--physical-port-id`, and `--logical-port-id`
selectors. The older `hmc_list_io_slots` / `network list-io-slots` surface remains raw and is not
the normalized contract.
