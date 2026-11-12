# Report where adapter changes live and check the profile at power-on (#981)

## Problem

Adapter and mapping writes change only the current configuration unless `CurrentProfileSync`
is `On`; `power-on --partition-profile` then silently drops them, leaving no boot device.

## Scope

New `operations/lpar/profiles/sync.py` owns the policy (clean extension, no ownership move).

- `ChangeLocation(current_profile_sync, lives_in, profile_name)` with `summary()`.
  `read_change_location(hmc, lpar_uuid)` reads the partition entry once: `On` gives
  `current-configuration-and-profile` plus `LastActivatedProfile`; `Disabled` or `Suspended`
  gives `current-configuration`; anything else gives `unknown`, keeping the raw value.
- Operations `add_network_adapter`, `add_vscsi_adapter`, `add_vfc_adapter`, `delete_adapter`,
  `map_storage`, `mount_optical_media`, `detach_storage_mapping`, `unmount_optical_media` call
  it after authorization, before the write, and return it (`AdapterResult`/`StorageMapResult`
  gain `change_location`; mount returns `StorageMapResult`). MCP add and mount tools keep
  their resource dict plus a `change_location` key, which `scripts/live_test` readers tolerate; map already returns the whole result. Delete and unmount append `summary()` to
  their message; detach (breaking, in CHANGELOG) returns `{mapping_id, change_location}`;
  the CLI prints `summary()`.
- `power_lpar` with a partition profile compares the current configuration's
  `VirtualSCSIClientAdapter`, `VirtualFibreChannelClientAdapter` and `ClientNetworkAdapter`
  slots with the direct `VirtualSlotNumber` child of each `ProfileVirtualIOAdapterSubclass`
  entry, whatever its subclass name. Each missing adapter is one `warnings` entry on
  `LparPowerResult` and `LparPowerOnOutcome`; an `HMCError` from an adapter feed becomes one
  "check not run" warning. The job is submitted either way.
- **Warn, not refuse:** activating a profile legitimately discards current changes, refusal
  needs a new override flag, and a removed adapter is recreated after power-off.
- Recipe step 5, `docs/cli.md`, regenerated `docs/tools/` and `CHANGELOG.md` describe both.

### Failure model

1. Actors and deployments: a CLI operator; an MCP agent such as kdive.
2. Invariants: a successful write is never reported as failed (the read precedes it); the
   power-on job document is unchanged; the power-on check never blocks the job.
3. Accepted: a failed partition read fails an adapter or mapping command before its write.
   vFC and Ethernet placement in the profile is unobserved; if it differs, the cost is a false
   warning, never a missed one. A same-slot adapter of another type counts as present.
4. Covered elsewhere: adapter PUT 406 on V10R3 (#935); sync default at create (operator);
   `lpars provision` and other profile-affecting operations (operator).

## Success

- The eight named operations report `CurrentProfileSync` and location via CLI and MCP.
- Power-on with a profile warns once per current client adapter of the three named types whose
  slot the profile lacks, and warns nothing when the profile has them all.

## Validation

- `lives_in` mapping and profile diff (single, list, empty) — focused-test,
  `tests/unit/test_profile_sync.py`; red: module absent; green: `pytest` on that file.
- Operations return `change_location`, read before the write — focused-test, existing tests.
- `power_lpar` warnings, no-warning, feed-error cases — focused-test, `tests/lpar/test_power.py`.
- MCP and CLI shapes, including the kept mount keys — focused-test in existing app tests.
- Live, authorized LPAR under the lock: read `CurrentProfileSync`; run the diff against its
  profile, powering on only if it is off; an adapter-PUT 406 is recorded as deferred to #935.
- Recipe and cli prose — task-test-not-applicable: no executable consumer reads them.
