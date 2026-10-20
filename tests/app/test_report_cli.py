"""CLI contracts for ``hmcpctl report utilization`` (ADR 0184)."""

from __future__ import annotations

import csv
import stat
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hmcpctl import cli
from hmcpctl.cli_commands import report
from hmcpctl.config import ConfigError, HMCConfig
from hmcpctl.operations.inventory.utilization import (
    AdapterFigures,
    CpuFigures,
    DiskFigures,
    FleetSurvey,
    MemoryFigures,
    PartitionFigures,
    ProfileFailure,
    SystemReading,
)

RUNNER = CliRunner()
EXPECTED_COLUMNS = [
    "row_type", "profiles", "system", "machine_type", "model", "serial", "firmware",
    "state", "systems", "cpu_installed", "cpu_configurable",
    "cpu_vios", "cpu_client_active", "cpu_idle_reserved", "cpu_other_reserved",
    "cpu_free", "cpu_dedicated", "cpu_shared", "cpu_allocated", "cpu_util_pct",
    "shared_pools",
    "mem_installed_mib", "mem_configurable_mib", "mem_hypervisor_mib", "mem_vios_mib",
    "mem_client_active_mib", "mem_idle_reserved_mib", "mem_other_reserved_mib",
    "mem_free_mib", "mem_allocated_mib", "mem_util_pct", "partitions_running",
    "partitions_not_activated",
    "partitions_other", "profile_claims", "profile_claim_mem_mib", "profile_claim_cpu",
    "notes",
    "disk_internal_total_mib", "disk_internal_assigned_mib", "disk_internal_free_mib",
    "disk_san_total_mib", "disk_san_assigned_mib", "disk_san_free_mib", "disk_util_pct",
    "slots_assigned", "slots_unassigned", "slots_sriov", "slots_empty", "slots_util_pct",
    "sriov_adapters", "sriov_logical_ports", "sriov_logical_ports_free", "sriov_util_pct",
]  # fmt: skip
DISK = DiskFigures(286102, 286102, 0, 102400, 0, 102400)
UNKNOWN_DISK = DiskFigures(None, None, None, None, None, None)


def _reading(profile: str, serial: str, *, vios: int | None = 1024) -> SystemReading:
    return SystemReading(
        profile=profile,
        name=f"system-{serial[-1]}",
        machine_type="9080",
        model="HEX",
        serial=serial,
        firmware="FW1120.00 (1)",
        state="operating",
        cpu=CpuFigures(48.0, 48.0, 2.0, 8.0, 4.0, 0.0, 34.0, 12.0, 2.0),
        memory=MemoryFigures(65536, 65536, 2048, vios, 8192, 4096, 0, 50176),
        partitions=PartitionFigures(2, 1, 0, 0, 0, 0),
        disk=DISK if vios is not None else UNKNOWN_DISK,
        adapters=AdapterFigures(1, 1, 1, 1, 1, 48, 2),
        shared_pools=(0,),
        gaps=() if vios is not None else ("VirtualIOServer feed: boom (HTTP 500)",),
    )


SURVEY = FleetSurvey(
    profiles=("hmc-a", "hmc-b", "hmc-c"),
    readings=(
        _reading("hmc-a", "SER0001"),
        _reading("hmc-b", "SER0001", vios=None),
        _reading("hmc-b", "SER0002"),
    ),
    failures=(ProfileFailure("hmc-c", "no answer within 300 s"),),
)


@pytest.fixture
def configured(monkeypatch):
    for name in (
        "HMC_HOST",
        "HMC_USER",
        "HMC_PASSWORD",
        "HMC_PORT",
        "HMC_VERIFY_SSL",
        "HMC_CERT_FINGERPRINT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    names = ["hmc-a", "hmc-b", "hmc-c"]
    monkeypatch.setattr(
        report,
        "config_inventory",
        lambda: {"profiles": [{"name": n} for n in names], "config_file": "c.toml"},
    )
    calls: dict = {}

    async def fake_survey(profiles, open_client, *, concurrency, hmc_timeout):
        calls["surveys"] = calls.get("surveys", 0) + 1
        calls.update(
            profiles=list(profiles),
            open_client=open_client,
            concurrency=concurrency,
            hmc_timeout=hmc_timeout,
        )
        return SURVEY

    monkeypatch.setattr(report, "survey_fleet", fake_survey)
    return calls


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_csv_header_is_the_stable_column_list(configured, tmp_path) -> None:
    out = tmp_path / "report.csv"
    result = RUNNER.invoke(cli.app, ["report", "utilization", "--csv", str(out)])

    assert result.exit_code == 0, result.output
    with out.open(newline="", encoding="utf-8") as handle:
        assert next(csv.reader(handle)) == EXPECTED_COLUMNS
    assert list(report.COLUMNS) == EXPECTED_COLUMNS


def test_report_writes_system_hmc_fleet_and_failure_rows(configured, tmp_path) -> None:
    out = tmp_path / "report.csv"
    result = RUNNER.invoke(cli.app, ["report", "utilization", "--csv", str(out)])

    assert result.exit_code == 0, result.output
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    rows = _rows(out)
    assert [row["row_type"] for row in rows] == [
        "system", "system", "hmc", "hmc", "fleet", "failure",
    ]  # fmt: skip
    dual = rows[0]
    assert (dual["serial"], dual["profiles"]) == ("SER0001", "hmc-a;hmc-b")
    assert dual["mem_vios_mib"] == "1024"
    assert (dual["cpu_allocated"], dual["cpu_util_pct"]) == ("14", "29.2")
    assert dual["mem_allocated_mib"] == "15360"
    assert rows[1]["mem_vios_mib"] == "1024"
    hmc_b = rows[3]
    assert (hmc_b["profiles"], hmc_b["systems"], hmc_b["mem_vios_mib"]) == (
        "hmc-b",
        "2",
        "1024",
    )
    assert hmc_b["mem_configurable_mib"] == "131072"
    assert hmc_b["notes"] == "; ".join(
        f"{column}: 1 of 2 systems unknown"
        for column in (
            "mem_vios_mib",
            "disk_internal_total_mib",
            "disk_internal_assigned_mib",
            "disk_internal_free_mib",
            "disk_san_total_mib",
            "disk_san_assigned_mib",
            "disk_san_free_mib",
        )
    )
    fleet = rows[4]
    assert fleet["systems"] == "2"
    assert fleet["notes"] == (
        "1 of 3 profiles failed (hmc-c); systems only they manage are absent"
    )
    assert (fleet["mem_configurable_mib"], fleet["mem_util_pct"]) == ("131072", "23.4")
    assert fleet["mem_allocated_mib"] == "30720"
    assert fleet["system"] == ""
    assert rows[5] == {
        **{column: "" for column in EXPECTED_COLUMNS},
        "row_type": "failure",
        "profiles": "hmc-c",
        "notes": "no answer within 300 s",
    }
    assert "hmc-c" in result.stderr
    assert "Wrote 2 systems from 2 of 3 profiles" in result.stderr


def test_report_writes_disk_and_adapter_columns(configured, tmp_path) -> None:
    out = tmp_path / "report.csv"
    result = RUNNER.invoke(cli.app, ["report", "utilization", "--csv", str(out)])

    assert result.exit_code == 0, result.output
    system, _, hmc_b, fleet = _rows(out)[1:5]
    assert (system["disk_internal_total_mib"], system["disk_san_free_mib"]) == (
        "286102",
        "102400",
    )
    assert (system["disk_util_pct"], system["slots_util_pct"]) == ("73.6", "66.7")
    assert (system["slots_sriov"], system["sriov_util_pct"]) == ("1", "95.8")
    assert hmc_b["disk_internal_total_mib"] == "286102"
    assert hmc_b["slots_assigned"] == "2"
    # SER0001, read by two profiles, counts once: the fleet is SER0001 plus SER0002.
    assert fleet["disk_internal_total_mib"] == "572204"
    assert (fleet["sriov_logical_ports"], fleet["sriov_logical_ports_free"]) == (
        "96",
        "4",
    )
    assert fleet["disk_util_pct"] == "73.6"


def test_unknown_vios_renders_unknown_not_zero(
    configured, tmp_path, monkeypatch
) -> None:
    out = tmp_path / "report.csv"
    configured_survey = FleetSurvey(
        ("hmc-b",), (_reading("hmc-b", "SER0003", vios=None),), ()
    )

    async def fake(profiles, open_client, *, concurrency, hmc_timeout):
        return configured_survey

    monkeypatch.setattr(report, "survey_fleet", fake)
    result = RUNNER.invoke(
        cli.app, ["report", "utilization", "--csv", str(out), "--profile", "hmc-b"]
    )

    assert result.exit_code == 0, result.output
    system = _rows(out)[0]
    assert system["mem_vios_mib"] == "unknown"
    assert system["disk_san_total_mib"] == "unknown"
    assert system["disk_util_pct"] == "unknown"
    assert system["notes"] == "VirtualIOServer feed: boom (HTTP 500)"


def test_default_selects_every_profile_and_opens_each_with_load_profile(
    configured, tmp_path, monkeypatch
) -> None:
    loaded: list[str] = []
    monkeypatch.setattr(report, "load_profile", lambda name: loaded.append(name))
    monkeypatch.setattr(report, "HMCClient", lambda config: ("client", config))
    out = tmp_path / "report.csv"

    result = RUNNER.invoke(
        cli.app,
        ["report", "utilization", "--csv", str(out), "--concurrency", "2",
         "--hmc-timeout", "30"],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    assert configured["profiles"] == ["hmc-a", "hmc-b", "hmc-c"]
    assert (configured["concurrency"], configured["hmc_timeout"]) == (2, 30.0)
    configured["open_client"]("hmc-b")
    assert loaded == ["hmc-b"]


def test_invalid_profile_error_names_fields_without_their_values(
    configured, tmp_path, monkeypatch
) -> None:
    def invalid(name: str) -> HMCConfig:
        return HMCConfig.from_mapping({"host": "h", "user": "u", "password": 99887766})

    monkeypatch.setattr(report, "load_profile", invalid)
    RUNNER.invoke(cli.app, ["report", "utilization", "--csv", str(tmp_path / "r.csv")])

    with pytest.raises(ConfigError) as caught:
        configured["open_client"]("hmc-b")
    assert "99887766" not in str(caught.value)
    assert "hmc-b" in str(caught.value)
    assert "password" in str(caught.value)


def test_unknown_profile_is_a_usage_error(configured, tmp_path) -> None:
    result = RUNNER.invoke(
        cli.app,
        ["report", "utilization", "--csv", str(tmp_path / "r.csv"), "--profile", "x"],
    )

    assert result.exit_code == 2
    assert "unknown profile(s) x" in result.stderr
    assert "hmc-a, hmc-b, hmc-c" in result.stderr


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("HMC_HOST", "elsewhere.test"),
        ("HMC_HOST", ""),
        ("hmc_host", "elsewhere.test"),
        ("HMC_USER", "someone"),
        ("HMC_PASSWORD", "secret"),
        ("HMC_PORT", "12443"),
        ("HMC_VERIFY_SSL", "false"),
        ("HMC_CERT_FINGERPRINT", ":".join(["AB"] * 32)),
    ],
)
def test_exported_connection_override_is_refused(
    configured, tmp_path, monkeypatch, name, value
) -> None:
    monkeypatch.setenv(name, value)
    result = RUNNER.invoke(
        cli.app, ["report", "utilization", "--csv", str(tmp_path / "r.csv")]
    )

    assert result.exit_code == 2
    assert f"unset {name.upper()}" in result.stderr
    assert "profiles" not in configured


def test_hmc_text_cells_cannot_start_a_spreadsheet_formula(
    configured, tmp_path, monkeypatch
) -> None:
    reading = replace(_reading("hmc-a", "SER0001"), name='=HYPERLINK("x")', state="-x")

    async def fake(profiles, open_client, *, concurrency, hmc_timeout):
        return FleetSurvey(("hmc-a",), (reading,), (ProfileFailure("hmc-b", "=cmd|x"),))

    monkeypatch.setattr(report, "survey_fleet", fake)
    out = tmp_path / "r.csv"
    result = RUNNER.invoke(cli.app, ["report", "utilization", "--csv", str(out)])

    assert result.exit_code == 0, result.output
    rows = _rows(out)
    system = rows[0]
    assert (system["system"], system["state"]) == ('\'=HYPERLINK("x")', "'-x")
    assert system["cpu_installed"] == "48"
    failure = next(row for row in rows if row["row_type"] == "failure")
    assert failure["notes"] == "'=cmd|x"


def test_root_connection_option_is_refused(configured, tmp_path) -> None:
    result = RUNNER.invoke(
        cli.app,
        ["--profile", "hmc-a", "report", "utilization", "--csv",
         str(tmp_path / "r.csv")],
    )  # fmt: skip

    assert result.exit_code == 2
    assert "drop the global" in result.stderr


def test_write_failure_keeps_failure_lines_and_leaves_no_file(
    configured, tmp_path, monkeypatch
) -> None:
    def broken(stream, rows):
        stream.write("partial")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(report, "write_csv", broken)
    result = RUNNER.invoke(
        cli.app, ["report", "utilization", "--csv", str(tmp_path / "r.csv")]
    )

    assert result.exit_code == 1
    assert "hmc-c" in result.stderr
    assert "No space left on device" in result.stderr
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("value", ["nan", "inf"])
def test_non_finite_hmc_timeout_is_a_usage_error(configured, tmp_path, value) -> None:
    result = RUNNER.invoke(
        cli.app,
        ["report", "utilization", "--csv", str(tmp_path / "r.csv"),
         "--hmc-timeout", value],
    )  # fmt: skip

    assert result.exit_code == 2
    assert "profiles" not in configured


def test_fleet_row_discloses_systems_it_cannot_deduplicate(
    configured, tmp_path, monkeypatch
) -> None:
    unidentified = replace(_reading("hmc-b", "SER0002"), serial=None)

    async def fake(profiles, open_client, *, concurrency, hmc_timeout):
        return FleetSurvey(
            ("hmc-a", "hmc-b"), (_reading("hmc-a", "SER0001"), unidentified), ()
        )

    monkeypatch.setattr(report, "survey_fleet", fake)
    out = tmp_path / "r.csv"
    result = RUNNER.invoke(cli.app, ["report", "utilization", "--csv", str(out)])

    assert result.exit_code == 0, result.output
    fleet = next(row for row in _rows(out) if row["row_type"] == "fleet")
    assert fleet["notes"] == (
        "1 systems have no machine type-model-serial, so one managed by two HMCs "
        "is counted twice"
    )


@pytest.mark.parametrize(
    "outputs",
    [
        ["--csv", "absent/r.csv"],
        ["--csv", "r.csv", "--html", "absent/r.html"],
    ],
)
def test_missing_directory_is_a_usage_error_before_surveying(
    configured, tmp_path, outputs
) -> None:
    args = [str(tmp_path / value) if "." in value else value for value in outputs]
    result = RUNNER.invoke(cli.app, ["report", "utilization", *args])

    assert result.exit_code == 2
    assert "cannot write the report beside" in result.stderr
    assert "profiles" not in configured
    assert list(tmp_path.iterdir()) == []


def test_html_alone_writes_an_owner_only_page(configured, tmp_path) -> None:
    out = tmp_path / "report.html"
    result = RUNNER.invoke(cli.app, ["report", "utilization", "--html", str(out)])

    assert result.exit_code == 0, result.output
    assert out.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [out]


def test_csv_and_html_share_one_survey(configured, tmp_path) -> None:
    csv_out, html_out = tmp_path / "r.csv", tmp_path / "r.html"
    result = RUNNER.invoke(
        cli.app,
        ["report", "utilization", "--csv", str(csv_out), "--html", str(html_out)],
    )

    assert result.exit_code == 0, result.output
    assert configured["surveys"] == 1
    assert _rows(csv_out)[0]["system"] == "system-1"
    assert "system-1" in html_out.read_text(encoding="utf-8")
    assert sorted(tmp_path.iterdir()) == [csv_out, html_out]
    summary = "".join(result.stderr.split())  # Rich wraps long paths anywhere
    assert f"to{csv_out}and{html_out}" in summary


def test_no_output_is_a_usage_error(configured) -> None:
    result = RUNNER.invoke(cli.app, ["report", "utilization"])

    assert result.exit_code == 2
    assert "--csv" in result.stderr
    assert "--html" in result.stderr
    assert "profiles" not in configured


@pytest.mark.parametrize("names", [("r.out", "r.out"), ("R.out", "r.out")])
def test_same_path_for_both_is_a_usage_error(configured, tmp_path, names) -> None:
    csv_name, html_name = names
    result = RUNNER.invoke(
        cli.app,
        ["report", "utilization", "--csv", str(tmp_path / csv_name),
         "--html", str(tmp_path / html_name)],
    )  # fmt: skip

    assert result.exit_code == 2
    assert "name the same file" in result.stderr
    assert "profiles" not in configured


def test_every_profile_failed_still_writes_both_files(
    configured, tmp_path, monkeypatch
) -> None:
    async def fake(profiles, open_client, *, concurrency, hmc_timeout):
        return FleetSurvey(
            ("hmc-a", "hmc-b"),
            (),
            (ProfileFailure("hmc-a", "timed out"), ProfileFailure("hmc-b", "refused")),
        )

    monkeypatch.setattr(report, "survey_fleet", fake)
    csv_out, html_out = tmp_path / "r.csv", tmp_path / "r.html"
    result = RUNNER.invoke(
        cli.app,
        ["report", "utilization", "--csv", str(csv_out), "--html", str(html_out)],
    )

    assert result.exit_code == 0, result.output
    assert [row["row_type"] for row in _rows(csv_out)] == [
        "fleet",
        "failure",
        "failure",
    ]
    page = html_out.read_text(encoding="utf-8")
    assert "No systems were surveyed." in page
    assert "<td>hmc-b</td><td>refused</td>" in page


def test_html_write_failure_leaves_no_file(configured, tmp_path, monkeypatch) -> None:
    def broken(rows, profiles, generated):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(report, "render_html", broken)
    result = RUNNER.invoke(
        cli.app,
        ["report", "utilization", "--csv", str(tmp_path / "r.csv"),
         "--html", str(tmp_path / "r.html")],
    )  # fmt: skip

    assert result.exit_code == 1
    assert "No space left on device" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_no_configured_profiles_fails(configured, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        report, "config_inventory", lambda: {"profiles": [], "config_file": None}
    )
    result = RUNNER.invoke(
        cli.app, ["report", "utilization", "--csv", str(tmp_path / "r.csv")]
    )

    assert result.exit_code == 1
    assert "no connection profiles are configured" in result.stderr
