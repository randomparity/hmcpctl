"""Contract tests for the live-run preflight.

Every case runs in `tmp_path` with `monkeypatch.chdir`, because both the
runner's `.env` reader and the credential bootstrap resolve relative to the
working directory.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SCRIPTS_ROOT = Path(__file__).parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_ROOT))
import live_test_preflight as preflight  # noqa: E402
from live_test import config as live_config  # noqa: E402
from live_test import runner  # noqa: E402


def _env_text(**overrides: str) -> str:
    """A complete, valid `.env`, derived from `LiveTestConfig`'s own defaults.

    `from_env_file` requires every mandatory key, so a hand-written fragment
    fails for the wrong reason. Building from `_CONFIG_FIELDS` also means a new
    setting cannot silently leave these tests testing a rejected file.
    """
    defaults = live_config.LiveTestConfig()
    values = {}
    for key, field in live_config.LiveTestConfig._CONFIG_FIELDS.items():
        value = getattr(defaults, field)
        values[key] = ",".join(value) if isinstance(value, tuple) else str(value)
    # Replacing, not appending: a second line for the same key is a duplicate,
    # which `from_env_file` rejects for a reason unrelated to the case at hand.
    values.update(overrides)
    return "".join(f"{key}={value}\n" for key, value in values.items())


_DEDICATED = {
    "LIVE_TEST_DEDICATED_PCIE_SYSTEM_NAME": "sys-R1",
    "LIVE_TEST_DEDICATED_PCIE_LPAR_PREFIX": "live-pcie-",
    "LIVE_TEST_DEDICATED_PCIE_DRC_INDEX": "21010020",
}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """An isolated working directory with no ambient HMC credentials.

    The environ is swapped for a copy rather than only cleared: the credential
    check reaches `runner._load_dotenv`, which assigns into `os.environ`
    directly, and `monkeypatch` cannot take back a key it never recorded.
    """
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.chdir(tmp_path)
    for key in ("HMC_HOST", "HMC_USER", "HMC_PASSWORD", "HMC_SCHEMA_VERSION"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def test_the_generated_fixture_env_is_one_the_runner_accepts(workspace):
    """Guards every case below: a fixture the validator rejects proves nothing."""
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")

    config = live_config.LiveTestConfig.from_env_file()

    assert config.dedicated_pcie_system_name == "sys-R1"


def _credentials(monkeypatch, resolved: bool = True) -> None:
    """Stand in for the runner's bootstrap without reading a real profile."""
    monkeypatch.setattr(runner, "_bootstrap_config", lambda: resolved)
    if resolved:
        for key in ("HMC_HOST", "HMC_USER", "HMC_PASSWORD"):
            monkeypatch.setenv(key, f"value-for-{key}")


# ---------------------------------------------------------------------------
# Preflight agrees with the runner's own verdict (Validation 3)
# ---------------------------------------------------------------------------


def test_a_valid_configuration_reports_the_runner_would_start(
    workspace, monkeypatch, capsys
):
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")
    _credentials(monkeypatch)

    assert preflight.main(["--skip-hardware"]) == 0
    assert "the runner would start" in capsys.readouterr().out


@pytest.mark.parametrize(
    "env_text",
    [
        _env_text(LIVE_TEST_SYSTEM_NAME=""),
        _env_text(LIVE_TEST_NOT_A_REAL_KEY="x"),
        _env_text() + "LIVE_TEST_SYSTEM_NAME=second\n",
        _env_text(LIVE_TEST_ISO_HTTP_PORT="not-a-number"),
        _env_text(LIVE_TEST_PROVISION_DESIRED_VCPUS="0"),
        _env_text(LIVE_TEST_PROVISION_DESIRED_MEMORY_MIB="99999999"),
    ],
    ids=[
        "empty",
        "unknown-key",
        "duplicate",
        "not-an-int",
        "not-positive",
        "inconsistent",
    ],
)
def test_each_rejection_the_runner_makes_is_a_non_zero_exit(
    workspace, monkeypatch, capsys, env_text
):
    """Preflight's verdict is the delegated validator's, not a second opinion.

    The validator's own message is relayed verbatim rather than reworded, so
    the two cannot disagree about why a run will not start.
    """
    (workspace / ".env").write_text(env_text, encoding="utf-8")
    _credentials(monkeypatch)

    with pytest.raises(ValueError) as runner_error:
        live_config.LiveTestConfig.from_env_file()

    assert preflight.main(["--skip-hardware"]) == 1
    output = capsys.readouterr().out
    assert "configuration  FAIL" in output
    assert str(runner_error.value) in output


def test_a_configuration_the_runner_accepts_is_never_rejected_here(
    workspace, monkeypatch
):
    """The other direction of Validation 3: no verdict preflight invents alone."""
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")
    _credentials(monkeypatch)

    live_config.LiveTestConfig.from_env_file()  # the runner accepts it

    assert preflight.main(["--skip-hardware"]) == 0


def test_an_absent_schema_version_is_reported_without_stopping_the_run(
    workspace, monkeypatch, capsys
):
    """#875. The variable is opt-in, so its absence is a report, not a FAIL row."""
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")
    monkeypatch.setattr(runner, "_bootstrap_config", lambda: True)
    for key in ("HMC_HOST", "HMC_USER", "HMC_PASSWORD"):
        monkeypatch.setenv(key, f"value-for-{key}")

    assert preflight.main(["--skip-hardware"]) == 0

    output = capsys.readouterr().out
    assert "HMC_SCHEMA_VERSION=not set" in output
    assert "MISSING" not in output
    assert "the runner would start" in output


def test_a_dotenv_only_schema_version_is_reported_as_set(
    workspace, monkeypatch, capsys
):
    """#875. The mirror of the runner's own `.env` load, and for the same reason.

    `_bootstrap_config` reads `.env` only when the TOML profile fails, so a
    resolved profile that does not pin the variable would leave preflight
    naming a request environment the run will not use.
    """
    (workspace / ".env").write_text(
        _env_text(**_DEDICATED) + "HMC_SCHEMA_VERSION=V1_0\n", encoding="utf-8"
    )
    monkeypatch.setattr(runner, "_bootstrap_config", lambda: True)
    monkeypatch.setattr(live_config, "ENV_FILE", workspace / ".env")
    for key in ("HMC_HOST", "HMC_USER", "HMC_PASSWORD"):
        monkeypatch.setenv(key, f"value-for-{key}")

    assert preflight.main(["--skip-hardware"]) == 0

    assert "HMC_SCHEMA_VERSION=set" in capsys.readouterr().out


def test_unresolvable_credentials_are_a_non_zero_exit(workspace, monkeypatch, capsys):
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")
    _credentials(monkeypatch, resolved=False)

    assert preflight.main(["--skip-hardware"]) == 1
    output = capsys.readouterr().out
    assert "credentials    FAIL" in output
    assert "HMC_PASSWORD=MISSING" in output


# ---------------------------------------------------------------------------
# No HMC_* value is printed (Validation 4)
# ---------------------------------------------------------------------------


def test_no_credential_value_reaches_either_output_stream(
    workspace, monkeypatch, capsys
):
    """The output is meant to be pasted into an issue when a run will not start."""
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")
    monkeypatch.setattr(runner, "_bootstrap_config", lambda: True)
    monkeypatch.setenv("HMC_HOST", "SENTINEL-host-d4f2.internal")
    monkeypatch.setenv("HMC_USER", "SENTINEL-user-d4f2")
    monkeypatch.setenv("HMC_PASSWORD", "SENTINEL-pw-d4f2")
    # A sentinel here too, not `V1_0`: the schema-version row is the newest
    # thing on this output path (#875), and a plausible value gives the
    # assertion below nothing to catch it printing.
    monkeypatch.setenv("HMC_SCHEMA_VERSION", "SENTINEL-schema-d4f2")

    assert preflight.main(["--skip-hardware"]) == 0

    captured = capsys.readouterr()
    assert "SENTINEL" not in captured.out
    assert "SENTINEL" not in captured.err
    assert "HMC_PASSWORD=set" in captured.out


def test_the_bootstrap_s_own_chatter_is_not_relayed(workspace, monkeypatch, capsys):
    """`_bootstrap_config` announces the profile it loaded; that must not escape."""
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")

    def chatty_bootstrap() -> bool:
        print("  Credentials loaded from profile SENTINEL-profile-d4f2")
        return True

    monkeypatch.setattr(runner, "_bootstrap_config", chatty_bootstrap)

    preflight.main(["--skip-hardware"])

    assert "SENTINEL" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The verdict names what a mutating arm will touch (Validation 5)
# ---------------------------------------------------------------------------


def test_the_dedicated_verdict_names_system_prefix_and_slot(
    workspace, monkeypatch, capsys
):
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")
    _credentials(monkeypatch)

    assert preflight.main(["--group", "dedicated", "--skip-hardware"]) == 0

    output = capsys.readouterr().out
    assert "sys-R1" in output
    assert "live-pcie-" in output
    assert "21010020" in output
    assert "RUNNABLE" in output


def test_the_profiles_verdict_names_each_change_it_makes(
    workspace, monkeypatch, capsys
):
    """#627. The arm restores each change, but the operator approves the list first."""
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")
    _credentials(monkeypatch)

    assert preflight.main(["--group", "profiles", "--skip-hardware"]) == 0

    output = capsys.readouterr().out
    assert "RUNNABLE" in output
    assert "msp (toggled, then restored)" in output
    assert "type-3 merge-restored" in output
    assert "profile re-applied" in output
    assert "skipped when ST0 reads sync_curr_profile as 1" in output


def test_the_profiles_verdict_states_when_sync_curr_profile_changes(
    workspace, monkeypatch, capsys
):
    """#1376. The sync round trip skips an activated partition or an invalid ST0 value."""
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")
    _credentials(monkeypatch)

    assert preflight.main(["--group", "profiles", "--skip-hardware"]) == 0

    output = capsys.readouterr().out
    assert "description (restored)" in output
    assert (
        "sync_curr_profile (restored; only when ST0 reads it as 0, 1 or 2 "
        "and the partition as Not Activated)" in output
    )
    assert "(each restored)" not in output


def test_the_users_verdict_discloses_the_scratch_user(workspace, monkeypatch, capsys):
    """#632. The arm creates an HMC-global user; the operator sees which first."""
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")
    _credentials(monkeypatch)

    assert preflight.main(["--group", "users", "--skip-hardware"]) == 0

    output = capsys.readouterr().out
    assert "RUNNABLE" in output
    assert "one user hmcpctl-live-<8 hex> with the hmcviewer task role" in output
    assert "remote access disabled" in output
    assert "deleted by UUID" in output


def test_the_network_verdict_names_each_change_it_makes(workspace, monkeypatch, capsys):
    """#629. Every mutation the network arm makes, and that it touches no existing network."""
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")
    _credentials(monkeypatch)

    assert preflight.main(["--group", "network", "--skip-hardware"]) == 0

    output = capsys.readouterr().out
    assert "RUNNABLE" in output
    assert "no existing network is changed" in output
    assert "only while Not Activated" in output
    assert "vSCSI and a vFC client adapter" in output
    assert "FC port's label (set," in output
    assert "a duplicate refused, renamed" in output
    assert "slot the partition's own vSCSI" in output
    assert "fcs9999" in output


def test_the_vmedia_verdict_names_each_change_it_makes(workspace, monkeypatch, capsys):
    """#1347. The arm works inside an existing repository and removes only its own media."""
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")
    _credentials(monkeypatch)

    assert preflight.main(["--group", "vmedia", "--skip-hardware"]) == 0

    output = capsys.readouterr().out
    assert "RUNNABLE" in output
    assert "only when the VIOS has none" in output
    assert "hmcpctl_live_<8 hex>" in output
    assert "only while Not Activated" in output
    assert "only when that file exists" in output
    assert "no other medium or mapping is unmounted or deleted" in output


def test_the_storage_verdict_names_each_change_it_makes(workspace, monkeypatch, capsys):
    """#1348. One run-named volume at a time, mapped only while Not Activated."""
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")
    _credentials(monkeypatch)

    assert preflight.main(["--group", "storage", "--skip-hardware"]) == 0

    output = capsys.readouterr().out
    assert "RUNNABLE" in output
    assert "one 1 GiB logical volume hpctl<8 hex> at a time" in output
    assert "only while Not Activated" in output
    assert "a delete while mapped expected refused" in output
    assert "attach-disk" in output
    assert "new virtual target device the HMC names (vtscsi<n>" in output
    assert "ST42 SKIPs" in output


def test_the_pcm_verdict_discloses_the_system_wide_round_trip(
    workspace, monkeypatch, capsys
):
    """#634. PCM preferences are shared by every PCM consumer of the system."""
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")
    _credentials(monkeypatch)

    assert preflight.main(["--group", "pcm", "--skip-hardware"]) == 0

    output = capsys.readouterr().out
    assert "RUNNABLE" in output
    assert "PCM collection preferences (system-wide)" in output
    assert "all five restored to the pre-run read" in output


def test_the_lpar_config_verdict_names_its_one_partition(
    workspace, monkeypatch, capsys
):
    """#1345. The arm's only mutation target is the partition it creates."""
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")
    _credentials(monkeypatch)

    assert preflight.main(["--group", "lpar-config", "--skip-hardware"]) == 0

    output = capsys.readouterr().out
    assert "RUNNABLE" in output
    assert "one partition hmcpctl-live-lpar-<8 hex>" in output
    assert "activated to SMS" in output


def test_the_lpar_power_verdict_names_its_partitions_and_volume(
    workspace, monkeypatch, capsys
):
    """#1346. Its targets are the run's own partitions and one VIOS volume."""
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")
    _credentials(monkeypatch)

    assert preflight.main(["--group", "lpar-power", "--skip-hardware"]) == 0

    output = capsys.readouterr().out
    assert "RUNNABLE" in output
    assert "partitions hmcpctl-live-pwr-<8 hex>" in output
    assert "volume lppwr<8 hex>" in output


def test_the_vios_backup_verdict_names_the_restore_and_its_cleanup(
    workspace, monkeypatch, capsys
):
    """#1349. The restart and the rmviosbk are what the operator approves."""
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")
    _credentials(monkeypatch)
    monkeypatch.setenv("HMC_SSH_TIMEOUT", "2400")

    assert preflight.main(["--group", "vios-backup", "--skip-hardware"]) == 0

    output = capsys.readouterr().out
    assert "RUNNABLE" in output
    assert "disk VTD removed" in output
    assert "restore with -r" in output
    assert "rmviosbk" in output


def test_the_vios_backup_verdict_refuses_the_default_ssh_timeout(
    workspace, monkeypatch, capsys
):
    """A 300 s timeout would cut the restore off while the VIOS restarts."""
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")
    _credentials(monkeypatch)
    monkeypatch.delenv("HMC_SSH_TIMEOUT", raising=False)

    preflight.main(["--group", "vios-backup", "--skip-hardware"])

    output = capsys.readouterr().out
    assert "vios-backup SKIP     HMC_SSH_TIMEOUT must be at least 2400" in output


def test_a_pinned_slot_predicts_the_io_slots_scenario_will_skip(
    workspace, monkeypatch, capsys
):
    """#1000. `_DEDICATED` pins a DRC index, leaving no room for the scenario's
    two further slots — the same gate `pcie._io_slots_scenario` uses."""
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")
    _credentials(monkeypatch)

    assert preflight.main(["--group", "dedicated", "--skip-hardware"]) == 0

    output = capsys.readouterr().out
    assert "io_slots scenario: SKIP" in output
    assert "LIVE_TEST_DEDICATED_PCIE_DRC_INDEX pins one slot" in output


def test_an_unpinned_slot_predicts_the_io_slots_scenario_will_mutate_two_spares(
    workspace, monkeypatch, capsys
):
    """#1000. With no DRC index pinned, the scenario takes two further spare
    slots (#985/PR #992), beyond the fixture's own auto-selected slot."""
    (workspace / ".env").write_text(
        _env_text(
            LIVE_TEST_DEDICATED_PCIE_SYSTEM_NAME="sys-R1",
            LIVE_TEST_DEDICATED_PCIE_LPAR_PREFIX="live-pcie-",
        ),
        encoding="utf-8",
    )
    _credentials(monkeypatch)

    assert preflight.main(["--group", "dedicated", "--skip-hardware"]) == 0

    output = capsys.readouterr().out
    assert "RUNNABLE" in output
    assert (
        f"io_slots scenario: two further spare slots {preflight.dedicated_pcie.AUTO_SELECTED_SLOT}"
        in (output)
    )


def test_an_unconfigured_dedicated_arm_is_predicted_skip_not_a_failure(
    workspace, monkeypatch, capsys
):
    """A missing arm setting stops that arm, not the run."""
    (workspace / ".env").write_text(_env_text(), encoding="utf-8")
    _credentials(monkeypatch)

    assert preflight.main(["--group", "dedicated", "--skip-hardware"]) == 0
    assert "SKIP" in capsys.readouterr().out


def test_a_delimiter_in_an_arm_setting_is_predicted_skip(
    workspace, monkeypatch, capsys
):
    """The arm's own `_dedicated_config` rejects it, so the prediction must too."""
    (workspace / ".env").write_text(
        _env_text(
            LIVE_TEST_DEDICATED_PCIE_SYSTEM_NAME="sys-R1",
            LIVE_TEST_DEDICATED_PCIE_LPAR_PREFIX="live=bad",
        ),
        encoding="utf-8",
    )
    _credentials(monkeypatch)

    assert preflight.main(["--group", "dedicated", "--skip-hardware"]) == 0
    assert "SKIP" in capsys.readouterr().out


def test_the_bare_cec_verdict_names_what_it_creates_and_powers(
    workspace, monkeypatch, capsys
):
    (workspace / ".env").write_text(
        _env_text(**_DEDICATED, LIVE_TEST_ACCEPT_PLATFORM_DUMP="true"), encoding="utf-8"
    )
    _credentials(monkeypatch)
    monkeypatch.setenv("HMC_AUTHORIZE_POWER_OPERATIONS", "true")

    assert preflight.main(["--group", "bare-cec", "--skip-hardware"]) == 0

    output = capsys.readouterr().out
    assert "bare-cec   RUNNABLE" in output
    assert "live-pcie-* (created, activated to SMS" in output
    assert "21010020" in output
    assert "platform dump: taken" in output


@pytest.mark.parametrize(
    ("overrides", "authorized", "reason"),
    [
        ({}, "true", "_LPAR_PREFIX must both be set"),
        (_DEDICATED, "false", "HMC_AUTHORIZE_POWER_OPERATIONS must be true"),
        (
            {**_DEDICATED, "LIVE_TEST_ACCEPT_PLATFORM_DUMP": "maybe"},
            "true",
            "LIVE_TEST_ACCEPT_PLATFORM_DUMP must be true, false or unset",
        ),
    ],
)
def test_a_bare_cec_arm_the_arm_would_refuse_is_predicted_skip(
    workspace, monkeypatch, capsys, overrides, authorized, reason
):
    """Each refusal is the arm's own admission, so the prediction must match it."""
    (workspace / ".env").write_text(_env_text(**overrides), encoding="utf-8")
    _credentials(monkeypatch)
    monkeypatch.setenv("HMC_AUTHORIZE_POWER_OPERATIONS", authorized)

    assert preflight.main(["--group", "bare-cec", "--skip-hardware"]) == 0

    output = capsys.readouterr().out
    assert "bare-cec   SKIP" in output
    assert reason in output


def test_every_arm_is_predicted_when_no_group_is_given(workspace, monkeypatch, capsys):
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")
    _credentials(monkeypatch)

    assert preflight.main(["--skip-hardware"]) == 0

    output = capsys.readouterr().out
    for group in runner.SUBTASK_GROUPS:
        if group != "all":
            assert group in output
    # "all" is every other group; predicting it too would double every row.
    assert "  all " not in output


def test_an_explicit_group_all_predicts_every_arm(workspace, monkeypatch, capsys):
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")
    _credentials(monkeypatch)

    assert preflight.main(["--group", "all", "--skip-hardware"]) == 0

    output = capsys.readouterr().out
    for group in runner.SUBTASK_GROUPS:
        if group != "all":
            assert group in output
    assert "  all " not in output


# ---------------------------------------------------------------------------
# Hardware findings are advisory (failure model)
# ---------------------------------------------------------------------------


def test_an_unreachable_hmc_is_reported_without_changing_the_exit_status(
    workspace, monkeypatch, capsys
):
    async def unreachable(_config, _system):
        raise OSError("no route to host")

    monkeypatch.setattr(preflight, "require_admitted_environment", unreachable)
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")
    _credentials(monkeypatch)

    assert preflight.main(["--group", "dedicated"]) == 0

    output = capsys.readouterr().out
    assert "not admitted or unreachable (OSError)" in output
    assert "the runner would start" in output


def test_an_admitted_system_is_reported_as_in_envelope(workspace, monkeypatch, capsys):
    async def admitted(_config, _system):
        return None

    monkeypatch.setattr(preflight, "require_admitted_environment", admitted)
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")
    _credentials(monkeypatch)

    assert preflight.main(["--group", "dedicated"]) == 0
    assert "admitted (ADR 0053 envelope)" in capsys.readouterr().out


def test_skip_hardware_contacts_no_hmc(workspace, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("--skip-hardware must contact no HMC")

    monkeypatch.setattr(preflight, "require_admitted_environment", forbidden)
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")
    _credentials(monkeypatch)

    assert preflight.main(["--skip-hardware"]) == 0


def test_no_hmc_is_contacted_when_credentials_are_unresolved(workspace, monkeypatch):
    """There is nothing to connect with, so probing would only produce noise."""

    def forbidden(*_args, **_kwargs):
        raise AssertionError("probed without credentials")

    monkeypatch.setattr(preflight, "require_admitted_environment", forbidden)
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")
    _credentials(monkeypatch, resolved=False)

    assert preflight.main(["--group", "dedicated"]) == 1


# ---------------------------------------------------------------------------
# The provision VLAN is a hardware fact: reported, never gated (#970)
# ---------------------------------------------------------------------------


async def _none() -> None:
    return None


def _stub_hardware(monkeypatch, vlan_probe) -> list[tuple[str, int]]:
    """Admit every system and route the VLAN probe through `vlan_probe`."""
    probed: list[tuple[str, int]] = []

    async def admitted(_config, _system):
        return None

    async def probe(system_name, vlan_id):
        probed.append((system_name, vlan_id))
        return await vlan_probe(system_name, vlan_id)

    monkeypatch.setattr(preflight, "require_admitted_environment", admitted)
    monkeypatch.setattr(preflight, "_probe_provision_vlan", probe)
    return probed


@pytest.mark.parametrize(
    "group", [["--group", "round2"], []], ids=["round2", "every-arm"]
)
def test_the_provision_vlan_is_reported_under_round2(
    workspace, monkeypatch, capsys, group
):
    async def present(_system, vlan_id):
        return f"{vlan_id} has a virtual network"

    probed = _stub_hardware(monkeypatch, present)
    (workspace / ".env").write_text(
        _env_text(**_DEDICATED, LIVE_TEST_PROVISION_VLAN_ID="42"), encoding="utf-8"
    )
    _credentials(monkeypatch)

    assert preflight.main(group) == 0

    output = capsys.readouterr().out
    assert probed == [(live_config.LiveTestConfig().system_name, 42)]
    round2 = output.index("  round2 ")
    assert output.index("    provision VLAN: 42 has a virtual network") > round2


def test_a_vlan_with_no_virtual_network_does_not_change_the_exit_status(
    workspace, monkeypatch, capsys
):
    """ST13/ST14 will fail, but preflight predicts; hardware findings stay advisory."""

    async def check(_hmc, _system_uuid, vlan_id):
        raise ValueError(f"No VirtualNetwork with VLAN ID {vlan_id} found")

    class FakeClient:
        def __init__(self, _config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    async def resolve(_hmc, _system):
        return "system-uuid"

    monkeypatch.setattr(preflight, "require_admitted_environment", lambda *_: _none())
    monkeypatch.setattr(preflight, "HMCClient", FakeClient)
    monkeypatch.setattr(preflight, "HMCConfig", lambda: object())
    monkeypatch.setattr(preflight, "resolve_system_uuid", resolve)
    monkeypatch.setattr(preflight, "check_vlan_exists", check)
    (workspace / ".env").write_text(_env_text(), encoding="utf-8")
    _credentials(monkeypatch)

    assert preflight.main(["--group", "round2"]) == 0

    output = capsys.readouterr().out
    assert "provision VLAN: no virtual network on VLAN 1" in output
    assert "LIVE_TEST_PROVISION_VLAN_ID" in output
    assert "the runner would start" in output


def test_an_unreachable_hmc_reports_the_provision_vlan_as_unknown(
    workspace, monkeypatch, capsys
):
    class Unreachable:
        def __init__(self, _config):
            pass

        async def __aenter__(self):
            raise OSError("no route to host")

        async def __aexit__(self, *_exc):
            return None

    monkeypatch.setattr(preflight, "require_admitted_environment", lambda *_: _none())
    monkeypatch.setattr(preflight, "HMCClient", Unreachable)
    monkeypatch.setattr(preflight, "HMCConfig", lambda: object())
    (workspace / ".env").write_text(_env_text(), encoding="utf-8")
    _credentials(monkeypatch)

    assert preflight.main(["--group", "round2"]) == 0
    assert "provision VLAN: unknown (OSError)" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [["--skip-hardware"], ["--group", "dedicated"]],
    ids=["skip-hardware", "other-arm"],
)
def test_the_provision_vlan_is_not_probed_without_round2_hardware(
    workspace, monkeypatch, argv
):
    async def forbidden(*_args):
        raise AssertionError("probed the provision VLAN")

    probed = _stub_hardware(monkeypatch, forbidden)
    (workspace / ".env").write_text(_env_text(**_DEDICATED), encoding="utf-8")
    _credentials(monkeypatch)

    assert preflight.main(argv) == 0
    assert probed == []


def test_detach_probe_preflight_discloses_only_bounded_scope(
    workspace, monkeypatch, capsys
):
    (workspace / ".env").write_text(_env_text(), encoding="utf-8")
    _credentials(monkeypatch)
    assert (
        preflight.main(["--group", "lpar-power", "--detach-probe", "--skip-hardware"])
        == 0
    )
    output = capsys.readouterr().out
    assert "one ST41-prefix scratch partition" in output
    assert "one 1024 MiB" in output
    assert "one remaining fresh attach/detach cycle" in output
    assert "two previous attach attempts consumed, three total ceiling" in output
    assert "attach while Not Activated" in output and "then detach" in output
    assert "manual recovery" in output
    assert "provision" not in output and "PCIe" not in output


@pytest.mark.parametrize(
    "argv", [["--detach-probe"], ["--group", "storage", "--detach-probe"]]
)
def test_detach_probe_preflight_refuses_wrong_group_before_configuration(
    monkeypatch, argv
):
    def forbidden():
        raise AssertionError("configuration accessed for invalid selection")

    monkeypatch.setattr(preflight, "_check_configuration", forbidden)
    with pytest.raises(SystemExit):
        preflight.main(argv)


@pytest.mark.parametrize(
    "pv,group", [("hdisk9", "hpvg1234abcd"), ("", "hpvg1234abcd"), ("hdisk9", "rootvg")]
)
def test_st42_preflight_discloses_exact_optin_or_skip(pv, group):
    from dataclasses import replace

    config = replace(
        live_config.LiveTestConfig(), scratch_pv_name=pv, scratch_vg_name=group
    )
    verdict = preflight._storage_verdict(config)
    text = str(verdict)
    if pv and group == "hpvg1234abcd":
        assert "ST42 creates scratch group hpvg1234abcd" in text
        assert "explicit physical volume hdisk9" in text
        assert "reducevg" in text
    else:
        assert "ST42 SKIPs" in text


def test_lpar_config_discloses_configured_dedicated_slot():
    config = live_config.LiveTestConfig(
        system_name="sys-A",
        dedicated_pcie_system_name="sys-A",
        dedicated_pcie_lpar_prefix="scratch-",
        dedicated_pcie_drc_index="21010020",
    )
    verdict = preflight._lpar_config_verdict(config)
    assert any("21010020" in target for target in verdict.mutates)


def test_system_profiles_preflight(monkeypatch):
    import json
    from unittest.mock import AsyncMock

    from live_test import system_profiles

    config = live_config.LiveTestConfig(
        system_profile_members_json=json.dumps(
            [
                {"partition": "a", "profile": "base"},
                {"partition": "b", "profile": "base"},
            ]
        ),
        system_profile_writer_window=True,
    )
    monkeypatch.setattr(preflight, "_check_configuration", lambda: (config, ""))
    monkeypatch.setattr(preflight, "_check_credentials", lambda: (True, {}))
    probe = AsyncMock(return_value={})
    monkeypatch.setattr(system_profiles, "preflight", probe)
    assert preflight.main(["--group", "system-profiles"]) == 0
    probe.assert_awaited_once()
    probe.side_effect = ValueError("drift")
    assert preflight.main(["--group", "system-profiles"]) == 2


def test_settings_group_preflight_routes_only_registered_arm(workspace, monkeypatch):
    (workspace / ".env").write_text(_env_text(), encoding="utf-8")
    _credentials(monkeypatch)
    assert preflight.main(["--group", "lpar-settings", "--skip-hardware"]) == 0
    assert runner.SUBTASK_GROUPS["lpar-settings"] == [47]
    assert 47 not in runner.SUBTASK_GROUPS["all"]


def test_memory_group_preflight_declares_bounded_owned_setup(workspace, monkeypatch):
    (workspace / ".env").write_text(_env_text(), encoding="utf-8")
    _credentials(monkeypatch)
    assert preflight.main(["--group", "lpar-memory-quantities", "--skip-hardware"]) == 0


def test_linux_roles_preflight_discloses_both_finite_variants(
    workspace, monkeypatch, capsys
):
    (workspace / ".env").write_text(_env_text(), encoding="utf-8")
    _credentials(monkeypatch)
    assert preflight.main(["--group", "lpar-linux-roles", "--skip-hardware"]) == 0
    output = " ".join(capsys.readouterr().out.split())
    for disclosure in (
        "native is_controller=1 required for management selection",
        "management-only creation omits KVM and POWER10",
        "two management updates",
        "three updates",
        "same-record POWER10",
        "no catalog promotion",
        "mandatory recovery",
    ):
        assert disclosure in output


def test_processor_quantities_preflight(workspace, monkeypatch, capsys):
    (workspace / ".env").write_text(_env_text(), encoding="utf-8")
    _credentials(monkeypatch)
    assert (
        preflight.main(["--group", "lpar-processor-quantities", "--skip-hardware"]) == 0
    )
    assert runner.SUBTASK_GROUPS["lpar-processor-quantities"] == [56]
    assert 56 not in runner.SUBTASK_GROUPS["all"]
    output = capsys.readouterr().out
    assert "remove0.10 entitlement, zero delta, over-maximum refusal" in output
    assert "positive add/set unrun" in output
    assert "explicit set0.50" not in output
    assert "matching-token deletion and recovery" in output


@pytest.mark.parametrize("fault", ["hpt", "ppt", "token", "grant"])
def test_creation_memory_layout_preflight_is_unarmed_by_default(fault):
    from dataclasses import replace

    config = live_config.LiveTestConfig(
        creation_memory_layout_hpt_ratio="1:128",
        creation_memory_layout_ppt_ratio="1:256",
        creation_memory_layout_fresh_vios_delete_authorized=True,
    )
    change = {
        "hpt": {"creation_memory_layout_hpt_ratio": ""},
        "ppt": {"creation_memory_layout_ppt_ratio": ""},
        "token": {"creation_memory_layout_hpt_ratio": "bad token"},
        "grant": {"creation_memory_layout_fresh_vios_delete_authorized": False},
    }[fault]
    verdict = preflight.arm_verdicts(
        replace(config, **change), "creation-memory-layout"
    )[0]
    assert not verdict.runnable and not verdict.mutates


def test_creation_memory_layout_exact_effects_without_hardware_role_prerequisite():
    config = live_config.LiveTestConfig(
        creation_memory_layout_hpt_ratio="1:128",
        creation_memory_layout_ppt_ratio="1:256",
        creation_memory_layout_fresh_vios_delete_authorized=True,
    )
    verdict = preflight.arm_verdicts(config, "creation-memory-layout")[0]
    assert verdict.runnable
    effects = " ".join(verdict.mutates)
    assert all(
        term in effects
        for term in (
            "two",
            "0/0/0",
            "Decimal 0",
            "1:128",
            "1:256",
            "UUID",
            "2 GiB",
            "read-only recovery",
        )
    )
    assert "KvmOnPowerVM=true" not in effects


def test_sharing_preflight_names_scope():
    verdicts = preflight.arm_verdicts(live_config.LiveTestConfig(), GROUP_SHARING)
    assert len(verdicts) == 1
    text = str(verdicts[0])
    for literal in (
        "two sequential",
        "hmcpctl-live-lpar-",
        "2048",
        "1024",
        "4096",
        "0.5",
        "1 CPU",
        "12",
        "8 positive",
        "4 refusal",
        "no profile apply",
        "no activation",
    ):
        assert literal in text
    assert "processor-sharing-weight" in text


GROUP_SHARING = "processor-sharing-weight"


def test_serial_procedure_exact_preflight_dispatch(monkeypatch):
    from unittest.mock import Mock

    from live_test import lpar_serial_adapter

    dispatch = Mock(return_value=2)
    monkeypatch.setattr(lpar_serial_adapter, "main", dispatch)
    assert (
        preflight.main(
            [
                "--group",
                "lpar-serial-adapter",
                "--packet",
                "p",
                "--packet-sha256",
                "s",
                "--output",
                "f",
            ]
        )
        == 2
    )
    dispatch.assert_called_once_with(
        [
            "preflight",
            "--packet",
            "p",
            "--packet-sha256",
            "s",
            "--output",
            "f",
        ]
    )


@pytest.mark.parametrize("missing", ["--packet", "--packet-sha256", "--output"])
def test_serial_preflight_requires_complete_exact_inputs(monkeypatch, missing):
    from unittest.mock import Mock

    from live_test import lpar_serial_adapter

    dispatch = Mock()
    monkeypatch.setattr(lpar_serial_adapter, "main", dispatch)
    args = [
        "--group",
        "lpar-serial-adapter",
        "--packet",
        "p",
        "--packet-sha256",
        "s",
        "--output",
        "f",
    ]
    position = args.index(missing)
    del args[position : position + 2]
    with pytest.raises(SystemExit) as result:
        preflight.main(args)
    assert result.value.code == 2
    dispatch.assert_not_called()
