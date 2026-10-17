# Read-only managed-system inventory

Capture one managed system before considering a later change. Every HMC command below is read-only; this recipe does not select or alter a resource.

> **Sensitive data:** captures can contain names, UUIDs, network topology, and storage detail. Choose a protected local directory. Do not commit or publish it.

## 1. Prepare a protected local capture directory

Inventory captures explicitly select `--json`: redirected inventory now defaults to
TSV. Keep these flags when copying the recipe; see [output formats](../cli.md#inventory-output-and-migration).

```bash
export HMC_PROFILE=operator-profile
export SYSTEM_NAME=example-system
export CAPTURE_DIR="$HOME/hmc-inventory-$(date +%Y%m%d-%H%M%S)"
umask 077
mkdir -p "$CAPTURE_DIR"
```

Use capture for optional categories. A denied, unavailable, or failed command leaves an error.txt file as evidence.

```bash
capture() {
  local name=$1
  shift
  if "$@" >"$CAPTURE_DIR/$name" 2>"$CAPTURE_DIR/$name.error.txt"; then
    rm -f -- "$CAPTURE_DIR/$name.error.txt"
  fi
}
```

## 2. Resolve and capture the managed system

```bash
capture system.json hmcpctl systems show "$SYSTEM_NAME" --json
SYSTEM=$(jq -er '.UUID' "$CAPTURE_DIR/system.json")
printf '%s\n' "${SYSTEM:?unresolved managed system — stop and read system.json.error.txt}" >"$CAPTURE_DIR/system.uuid.txt"
capture system-summary.json hmcpctl systems summary "$SYSTEM" --json
capture managed-system.raw.xml hmcpctl raw get "/rest/api/uom/ManagedSystem/$SYSTEM"
```

`systems show` accepts the managed-system name, so one command both resolves the UUID and captures the system. The recipe does not list the console inventory to find it: that listing is not needed to capture a system you can already name, and some HMC firmware levels cannot serialize it at all.

**Stop here if the UUID does not resolve.** Every later command interpolates `$SYSTEM`; an empty value asks the HMC for nothing and fills the directory with empty files. The `${SYSTEM:?...}` guard fails the command that writes system.uuid.txt, so an absent or empty system.uuid.txt means the capture never started.

system.json and system-summary.json are stable CLI JSON for identity, firmware, RAM, CPU capacity, and partition counts. managed-system.raw.xml is **raw HMC XML** for fields the stable projections in step 3 do not expose.

## 3. Capture network, PCIe, and SR-IOV inventory

```bash
capture virtual-switches.json hmcpctl network list-switches "$SYSTEM" --json
capture virtual-networks.json hmcpctl network list-networks "$SYSTEM" --json
capture network-bridges.json hmcpctl network list-bridges "$SYSTEM" --json
capture sea-adapters.json hmcpctl network list-sea-adapters "$SYSTEM" --json
capture dedicated-pcie-slots.json hmcpctl network list-dedicated-pcie-slots "$SYSTEM" --json
capture sriov-adapters.json hmcpctl network list-sriov-adapters "$SYSTEM" --json
jq -r '.items[]? | select(.adapter_id != null and .adapter_id != "null") | .adapter_id' "$CAPTURE_DIR/sriov-adapters.json" | while IFS= read -r adapter; do
  capture "sriov-physical-ports-$adapter.json" hmcpctl network list-sriov-physical-ports "$SYSTEM" --adapter-id "$adapter" --json
  capture "sriov-logical-ports-$adapter.json" hmcpctl network list-sriov-logical-ports "$SYSTEM" --adapter-id "$adapter" --json
done
capture vfc-ports.json hmcpctl network list-fc-ports "$SYSTEM" --json
```

sea-adapters.json records the Shared Ethernet Adapters that back bridged client networks.

The SR-IOV port commands require an adapter, so they run once per adapter found
in sriov-adapters.json and their files carry that adapter id. The filter skips
the literal string `"null"`, which the HMC reports for an adapter in dedicated
mode.

These PCIe, SR-IOV, and vFC files are stable CLI output. Do not replace their
normalized capability state with raw XML. The PCIe and SR-IOV commands return a
capability envelope whose `items` array holds the rows. Empty item lists,
unavailable capability, and error.txt files are distinct evidence.

## 4. Capture every LPAR and its adapters

```bash
capture lpars.json hmcpctl lpars list --system "$SYSTEM" --json
jq -r '.entries[].UUID' "$CAPTURE_DIR/lpars.json" | while IFS= read -r lpar; do
  capture "lpar-$lpar.json" hmcpctl lpars show "$lpar" --json
  capture "lpar-$lpar-summary.json" hmcpctl lpars summary "$lpar" --json
  capture "lpar-$lpar-network.json" hmcpctl adapters list "$lpar" --type ClientNetworkAdapter --json
  capture "lpar-$lpar-vscsi.json" hmcpctl adapters list "$lpar" --type VirtualSCSIClientAdapter --json
  capture "lpar-$lpar-vfc.json" hmcpctl adapters list "$lpar" --type ClientFibreChannelAdapter --json
done
```

Full documents and summaries retain LPAR state, memory, CPU allocation, and description. The adapter files cover network, vSCSI, and vFC types.

## 5. Capture every VIOS and storage state

```bash
capture vios.json hmcpctl vios list --system "$SYSTEM" --json
jq -r '.entries[].UUID' "$CAPTURE_DIR/vios.json" | while IFS= read -r vios; do
  capture "vios-$vios.raw.xml" hmcpctl raw get "/rest/api/uom/VirtualIOServer/$vios"
  capture "vios-$vios-vgs.json" hmcpctl storage list-vgs "$vios" --system "$SYSTEM" --json
  capture "vios-$vios-mappings.json" hmcpctl storage list-mappings "$vios" --system "$SYSTEM" --json
  jq -r '.[].uuid' "$CAPTURE_DIR/vios-$vios-vgs.json" | while IFS= read -r vg; do
    capture "vios-$vios-vg-$vg.raw.xml" hmcpctl raw get "/rest/api/uom/VirtualIOServer/$vios/VolumeGroup/$vg"
    capture "vios-$vios-vg-$vg-media-repository.json" hmcpctl storage get-media-repo "$vios" --vg "$vg" --system "$SYSTEM" --json
    capture "vios-$vios-vg-$vg-optical-media.json" hmcpctl storage list-optical-media "$vios" --vg "$vg" --system "$SYSTEM" --json
  done
done
```

The raw VIOS document records physical volumes and end-to-end vFC mappings; the
raw VG document records physical volumes, virtual disks, and media state. The
other VIOS files provide identity, partition ID, state, version, free-space
diagnostics, and stable mapping projections. Do not infer a disk name from a
mapping.

## 6. Capture Shared Storage Pool state

```bash
capture clusters.json hmcpctl cluster list --json
capture shared-storage-pools.json hmcpctl cluster list-ssps --json
jq -r '.[].UUID' "$CAPTURE_DIR/shared-storage-pools.json" | while IFS= read -r ssp; do
  capture "ssp-$ssp.raw.xml" hmcpctl raw get "/rest/api/uom/SharedStoragePool/$ssp"
done
```

The list commands are stable CLI output. Each raw SSP document records its
physical volumes and logical units. SSP inventory is console-wide because the
existing CLI list has no managed-system selector; review only the pools that
contain the captured system's VIOSs.

## 7. Complete the selector worksheet

This is a human decision record for a later workflow. Leave a field blank when evidence is missing, denied, stale, or unsuitable.

| Future input | Saved evidence | Operator choice |
|---|---|---|
| SYSTEM | system.uuid.txt, system.json | managed-system UUID |
| VIOS | vios.json | VIOS UUID and name |
| VG | vios-<UUID>-vgs.json | volume-group UUID |
| VLAN | network JSON files | VLAN and switch ID |
| VIOS partition ID / server slot | vios.json, vSCSI JSON | paired VIOS ID and slot |
| Candidate disk name | raw VIOS/VG/SSP XML and mappings | approved unused name |
| Free-space evidence | volume-group JSON and diagnostic | timestamped value |

Review every error.txt file and verify the capture is current before any later change. Inventory informs an operator; it never authorizes or executes a mutation.

To provision a partition from an installation ISO using this evidence, continue with the
[LPAR ISO installation recipe](lpar-iso-install.md).
