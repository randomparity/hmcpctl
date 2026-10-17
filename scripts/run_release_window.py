"""Run one explicitly authorized release window and retain private phase evidence."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import check_capability_inventory as inventory
import live_test_baseline as baseline
import live_test_preflight as preflight
import live_test_runner as runner
from live_test import cli

from hmcpctl.config import HMCConfig

ARMS = {
    name: f"live_{name.replace('-', '_')}.py"
    for name in (
        "round2",
        "vmedia",
        "sriov",
        "dedicated",
        "bare-cec",
        "profiles",
        "users",
        "vios-backup",
        "pcm",
        "network",
        "lpar-config",
        "storage",
        "lpar-power",
    )
}
VIOS_ARMS = {
    "round2",
    "vmedia",
    "sriov",
    "profiles",
    "vios-backup",
    "network",
    "storage",
    "lpar-power",
}
MAX_LOG = 8 * 1024 * 1024
GRACE = 5.0


class WindowError(ValueError):
    """A safe operator-facing refusal, with no raw child or configuration output."""


def git(root, *arguments):
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise WindowError("repository check failed; inspect the candidate checkout")
    return result.stdout.strip()


def read_json(path, limit):
    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise WindowError("duplicate JSON keys are not allowed")
        return result

    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise WindowError("input exceeds the supported size")
    return json.loads(content, object_pairs_hook=unique)


def prepare(args):
    root = Path.cwd().resolve()
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise WindowError("run from the candidate checkout root")
    branch = git(root, "symbolic-ref", "--short", "HEAD")
    default = subprocess.run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if default.returncode:
        raise WindowError(
            "origin default branch is unknown; configure origin/HEAD first"
        )
    protected = {"main", "master", default.stdout.strip().removeprefix("origin/")}
    if branch in protected or git(
        root, "status", "--porcelain", "--untracked-files=all"
    ):
        raise WindowError("use a clean non-default candidate branch")
    if not args.branch.startswith("promotion/"):
        raise WindowError("choose a new branch below promotion/")
    git(root, "check-ref-format", "--branch", args.branch)
    if git(root, "branch", "--list", args.branch):
        raise WindowError("promotion branch already exists")
    claims = args.claims.resolve(strict=True)
    if claims.parent != root / "docs/capabilities/releases" or args.claims.is_symlink():
        raise WindowError("claims must be a committed release file in this checkout")
    git(root, "ls-files", "--error-unmatch", str(claims.relative_to(root)))
    rows = inventory.load_release_claims(
        claims, {tool.operation for tool in inventory.discover_registry()}
    )
    selected = {row["arm"] for row in rows}
    if not selected or not selected <= ARMS.keys():
        raise WindowError("claims must select supported named arms")
    arms = [arm for arm in ARMS if arm in selected]
    stale = {"round2", "vmedia", *arms}
    if any(
        (root / f"test-results-{arm}{suffix}.json").exists()
        or (root / f"test-results-{arm}{suffix}.json").is_symlink()
        for arm in stale
        for suffix in ("", "-observations")
    ):
        raise WindowError(
            "prior runner outputs exist; preserve them outside this attempt"
        )
    with (
        contextlib.redirect_stdout(io.StringIO()),
        contextlib.redirect_stderr(io.StringIO()),
    ):
        if not runner._bootstrap_config():
            raise WindowError("configure the normal runner HMC environment first")
        runner._load_dotenv()
        config = runner.LiveTestConfig.from_env_file()
        hmc = HMCConfig()
        verdicts = [preflight.arm_verdicts(config, arm)[0] for arm in arms]
    if any(not item.runnable for item in verdicts):
        raise WindowError("a claimed arm is not configured; run its normal preflight")
    systems = list(
        dict.fromkeys(
            [
                config.system_name,
                *(item.system_name for item in verdicts if item.system_name),
            ]
        )
    )
    boundary = {
        item.group: [f"HMC {hmc.host}:{hmc.port}, account {hmc.user}", *item.mutates]
        for item in verdicts
    }
    if "round2" in arms:
        boundary["round2"].extend(
            [
                f"scratch partition {config.scratch_name}: create, modify, power and delete",
                f"partition {config.lp3_name}: description/profile property round trips",
                f"temporary hmcpctl-live-round2-<8 hex> partition and hp14<8 hex> disk ({config.provision_disk_mib} MiB) in {config.vdisk_volume_group_name}: provision with VLAN {config.provision_vlan_id}, power on, decommission and delete; configured originals retained",
            ]
        )
    if VIOS_ARMS & selected:
        for arm in arms:
            boundary[arm].append(
                "read-only baseline of VIOS partitions on the selected systems, including mappings and volumes"
            )
    authorization = {
        "candidate": git(root, "rev-parse", "HEAD"),
        "arms": arms,
        "mutation_boundary": boundary,
        "authorized": False,
        "vios_access": False,
    }
    return {
        "root": root,
        "claims": claims,
        "cli_coverage": cli_coverage([row["operation"] for row in rows], [], ""),
        "branch": args.branch,
        "systems": systems,
        "authorization": authorization,
    }


def cli_coverage(operations, documents, candidate):
    coverage = dict.fromkeys(operations, "gap: CLI not verified in this window")
    for document in documents:
        run = document.get("run", {})
        if (
            run.get("tested_commit") != candidate
            or run.get("tree_clean") is not True
            or run.get("partial") is not False
            or run.get("group") != "bare-cec"
            or run.get("subtasks") != runner.SUBTASK_GROUPS["bare-cec"]
        ):
            continue
        for row in document.get("results", []):
            recorded = row.get("cli_observation", {})
            operation = recorded.get("operation")
            observation = recorded.get("observation", {})
            if (
                operation in coverage
                and operation in cli.LABELS
                and row.get("tool") == cli.LABELS[operation]
                and row.get("subtask") == 35
                and row.get("status") == "PASS"
                and row.get("result") == "passed"
                and observation.get("result") == "passed"
                and observation.get("scenario") == cli.SCENARIO
                and observation.get("cleanup") == "not-required"
                and observation.get("assertions")
            ):
                coverage[operation] = "exercised: bare-cec CLI at candidate"
    return coverage


def authorize(plan, path):
    if path is None:
        raise WindowError(
            "explicit authorization file required; inspect --dry-run first"
        )
    expected = dict(
        plan["authorization"],
        authorized=True,
        vios_access=bool(VIOS_ARMS & set(plan["authorization"]["arms"])),
    )
    supplied = read_json(path, 64 * 1024)
    if (
        supplied != expected
        or supplied.get("authorized") is not True
        or type(supplied.get("vios_access")) is not bool
    ):
        raise WindowError(
            "authorization does not exactly match this candidate and mutation boundary"
        )


def private_directory(path, root):
    if (
        path.is_symlink()
        or not path.is_dir()
        or path.resolve() == root
        or root in path.resolve().parents
        or stat.S_IMODE(path.stat().st_mode) != 0o700
    ):
        raise WindowError(
            "output must be an existing physical mode-0700 directory outside the checkout"
        )
    return Path(tempfile.mkdtemp(prefix="window-", dir=path.resolve()))


class Execution:
    """Sequential subprocess ownership, bounded private output, and signal handling."""

    def __init__(self, root, output):
        self.root, self.output = root, output
        self.cancelled = False
        self.safe = True
        self.phases = []

    def interrupt(self, *_):
        self.cancelled = True

    @staticmethod
    def stop(process, sig):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass

    def run(self, label, arguments, *, recovery=False, markdown=False):
        if self.cancelled and not recovery:
            return False
        path = self.output / (label + (".md" if markdown else ".log"))
        process = None
        code = 125
        truncated = False
        orphaned = False
        try:
            with path.open("xb") as log:
                process = subprocess.Popen(
                    [sys.executable, *map(str, arguments)],
                    cwd=self.root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                self.safe = False
                with selectors.DefaultSelector() as selector:
                    assert process.stdout is not None
                    selector.register(process.stdout, selectors.EVENT_READ)
                    written, stage, deadline = 0, 0, 0.0
                    exited = None
                    while selector.get_map() or process.poll() is None:
                        now = time.monotonic()
                        if self.cancelled and not recovery and stage == 0:
                            self.stop(process, signal.SIGINT)
                            stage, deadline = 1, now + GRACE
                        if stage and now >= deadline and process.poll() is None:
                            if stage == 3:
                                raise WindowError(
                                    "child could not be reaped; manual inspection required"
                                )
                            self.stop(
                                process,
                                signal.SIGTERM if stage == 1 else signal.SIGKILL,
                            )
                            stage, deadline = stage + 1, now + GRACE
                        if process.poll() is not None:
                            exited = exited or now
                            if now - exited > GRACE:
                                self.stop(process, signal.SIGKILL)
                                orphaned = True
                                break
                        for key, _ in selector.select(0.1):
                            chunk = os.read(key.fd, 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            room = MAX_LOG - written
                            log.write(chunk[:room])
                            written += min(room, len(chunk))
                            truncated |= len(chunk) > room
                code = process.wait(timeout=GRACE)
                self.safe = True
        except (OSError, subprocess.TimeoutExpired):
            code = 125
        finally:
            if process is not None:
                if not self.safe:
                    self.stop(process, signal.SIGKILL)
                    try:
                        process.wait(timeout=GRACE)
                        self.safe = True
                    except subprocess.TimeoutExpired:
                        pass
                if process.stdout is not None:
                    process.stdout.close()
            passed = (
                code == 0
                and not truncated
                and not orphaned
                and (recovery or not self.cancelled)
            )
            self.phases.append(
                {
                    "phase": label,
                    "exit_code": code,
                    "passed": passed,
                    "output_truncated": truncated,
                    "orphaned_output": orphaned,
                }
            )
        return passed


def capture(execution, index, system, when, ids=()):
    path = execution.output / f"baseline-{index}-{when}.json"
    argv = [
        "scripts/live_test_baseline.py",
        "capture",
        "--system",
        system,
        "--output",
        path,
    ]
    for vios in ids:
        argv.extend(["--vios-id", str(vios)])
    if not execution.run(f"baseline-{index}-{when}", argv):
        raise WindowError("baseline capture failed; inspect private phase output")
    return path


def run_window(plan, execution):
    arms = plan["authorization"]["arms"]
    captures = []
    for index, system in enumerate(plan["systems"]):
        before = capture(execution, index, system, "discovery")
        document = baseline.validate(read_json(before, 16 * 1024 * 1024))
        ids = (
            [
                int(row["lpar_id"])
                for row in document["tables"]["partitions"]
                if row["lpar_env"] == "vioserver"
            ]
            if VIOS_ARMS & set(arms)
            else []
        )
        if index == 0 and VIOS_ARMS & set(arms) and not ids:
            raise WindowError(
                "VIOS inventory is absent; baseline coverage cannot be established"
            )
        if ids:
            before = capture(execution, index, system, "before", ids)
        captures.append((index, system, before, ids))
    results = []
    for arm in arms:
        if not execution.run(
            f"{arm}-preflight", ["scripts/live_test_preflight.py", "--group", arm]
        ):
            return False
        result = plan["root"] / f"test-results-{arm}.json"
        arm_ok = False
        recovery_ok = False
        try:
            arm_ok = execution.run(f"{arm}-arm", ["scripts/" + ARMS[arm]])
        finally:
            if execution.safe:
                recovery_ok = execution.run(
                    f"{arm}-recovery",
                    ["scripts/live_test_recovery.py", "--results", result],
                    recovery=True,
                )
        if not arm_ok or not recovery_ok or execution.cancelled:
            return False
        results.append(result)
        if arm == "bare-cec":
            plan["cli_coverage"] = cli_coverage(
                plan["cli_coverage"],
                [read_json(result, 16 * 1024 * 1024)],
                plan["authorization"]["candidate"],
            )
    for index, system, before, ids in captures:
        after = capture(execution, index, system, "after", ids)
        if not execution.run(
            f"baseline-{index}-compare",
            ["scripts/live_test_baseline.py", "compare", before, after],
        ):
            return False
    for arm, result in zip(arms, results, strict=True):
        if not execution.run(
            f"{arm}-evidence", ["scripts/live_test_evidence.py", result], markdown=True
        ):
            return False
    if git(plan["root"], "rev-parse", "HEAD") != plan["authorization"]["candidate"]:
        raise WindowError("candidate changed during the window; promotion refused")
    if not execution.run(
        "promotion",
        [
            "scripts/promote_live_observations.py",
            "--claims",
            plan["claims"],
            "--branch",
            plan["branch"],
            "--window-complete",
            "--recovery-confirmed",
            *results,
        ],
    ):
        return False
    return execution.run(
        "claimed-set-gate",
        ["scripts/check_capability_inventory.py", "--require-current", plan["claims"]],
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claims", type=Path, required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--authorization", type=Path)
    choice.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    execution = None
    passed = False
    previous = {}
    exit_code = 2
    old_umask = os.umask(0o077)
    try:
        plan = prepare(args)
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "authorization": plan["authorization"],
                        "systems": plan["systems"],
                        "cli_coverage": plan["cli_coverage"],
                        "phases": [
                            "baseline",
                            "preflight/arm/recovery",
                            "compare",
                            "evidence",
                            "promotion",
                            "claimed-set-gate",
                        ],
                    },
                    indent=2,
                )
            )
            return 0
        authorize(plan, args.authorization)
        execution = Execution(
            plan["root"], private_directory(args.output_dir, plan["root"])
        )
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, execution.interrupt)
        passed = run_window(plan, execution)
        exit_code = 0 if passed else 1
    except (WindowError, OSError, ValueError, inventory.InventoryError):
        print(
            "FAIL: release-window authorization, configuration or phase check failed; inspect --dry-run and private phase output",
            file=sys.stderr,
        )
        exit_code = 2
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if execution is not None:
            try:
                with (execution.output / "summary.json").open("x") as stream:
                    json.dump(
                        {
                            "passed": passed,
                            "interrupted": execution.cancelled,
                            "candidate": plan["authorization"]["candidate"],
                            "arms": plan["authorization"]["arms"],
                            "phases": execution.phases,
                            "cli_coverage": plan["cli_coverage"],
                        },
                        stream,
                        indent=2,
                    )
            except OSError:
                exit_code = 2
                print("FAIL: private summary could not be written", file=sys.stderr)
        os.umask(old_umask)
    if execution is not None and exit_code != 2:
        print(
            f"{'PASS' if passed else 'FAIL'}: release window; private summary: {execution.output / 'summary.json'}"
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
