import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
RECIPE = ROOT / "docs/recipes/system-inventory.md"
READ_COMMANDS = {
    'systems show "$SYSTEM_NAME" --json',
    'systems summary "$SYSTEM" --json',
    'raw get "/rest/api/uom/ManagedSystem/$SYSTEM"',
    'network list-switches "$SYSTEM" --json',
    'network list-networks "$SYSTEM" --json',
    'network list-bridges "$SYSTEM" --json',
    'network list-sea-adapters "$SYSTEM" --json',
    'network list-dedicated-pcie-slots "$SYSTEM" --json',
    'network list-sriov-adapters "$SYSTEM" --json',
    'network list-sriov-physical-ports "$SYSTEM" --adapter-id "$adapter" --json',
    'network list-sriov-logical-ports "$SYSTEM" --adapter-id "$adapter" --json',
    'network list-fc-ports "$SYSTEM" --json',
    'lpars list --system "$SYSTEM" --json',
    'lpars show "$lpar" --json',
    'lpars summary "$lpar" --json',
    'adapters list "$lpar" --type ClientNetworkAdapter --json',
    'adapters list "$lpar" --type VirtualSCSIClientAdapter --json',
    'adapters list "$lpar" --type ClientFibreChannelAdapter --json',
    'vios list --system "$SYSTEM" --json',
    'raw get "/rest/api/uom/VirtualIOServer/$vios"',
    'storage list-vgs "$vios" --system "$SYSTEM" --json',
    'storage list-mappings "$vios" --system "$SYSTEM" --json',
    'storage get-media-repo "$vios" --vg "$vg" --system "$SYSTEM" --json',
    'storage list-optical-media "$vios" --vg "$vg" --system "$SYSTEM" --json',
    'raw get "/rest/api/uom/VirtualIOServer/$vios/VolumeGroup/$vg"',
    "cluster list --json",
    "cluster list-ssps --json",
    'raw get "/rest/api/uom/SharedStoragePool/$ssp"',
}


def test_system_inventory_recipe_has_required_read_only_contract() -> None:
    recipe = RECIPE.read_text()
    for marker in (
        "Sensitive data",
        "umask 077",
        "raw HMC XML",
        "error.txt",
        "dedicated-pcie-slots.json",
        "sriov-logical-ports-$adapter.json",
        "sea-adapters.json",
        "vfc-ports.json",
        "vios-$vios.raw.xml",
        "vg-$vg.raw.xml",
        "shared-storage-pools.json",
        "ssp-$ssp.raw.xml",
        "VIOS partition ID / server slot",
        "Candidate disk name",
        "Free-space evidence",
    ):
        assert marker in recipe
    commands = set(re.findall(r"hmcpctl ([^\n\x60]+)", recipe))
    assert commands == READ_COMMANDS


def test_system_inventory_recipe_is_linked_from_documentation_index() -> None:
    assert (
        "[Read-only system inventory recipe](recipes/system-inventory.md)"
        in (ROOT / "docs/index.md").read_text()
    )


def _uuid_write_line() -> str:
    """The recipe line that records the resolved UUID, however it is written."""
    redirect = '>"$CAPTURE_DIR/system.uuid.txt"'
    lines = [line for line in RECIPE.read_text().splitlines() if redirect in line]
    assert len(lines) == 1, f"expected one system.uuid.txt write, found {len(lines)}"
    return lines[0]


def test_unresolved_system_stops_the_capture_rather_than_writing_empty_files(
    tmp_path,
) -> None:
    """An unresolved system must fail loudly; the HMC firmware behind #783 makes this reachable."""
    guard = _uuid_write_line()
    script = f'CAPTURE_DIR={tmp_path}\nSYSTEM=""\n{guard}\necho reached-next-step\n'

    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )

    assert result.returncode != 0
    assert "reached-next-step" not in result.stdout
    assert not (tmp_path / "system.uuid.txt").exists()


def test_sriov_port_captures_always_supply_an_adapter_id() -> None:
    """Both port commands are refused by the CLI without --adapter-id."""
    recipe = RECIPE.read_text()
    for command in ("list-sriov-physical-ports", "list-sriov-logical-ports"):
        for line in recipe.splitlines():
            if command in line and "hmcpctl" in line:
                assert "--adapter-id" in line, line


def test_inventory_recipe_hands_off_to_the_iso_install_recipe() -> None:
    assert "(lpar-iso-install.md)" in RECIPE.read_text()
