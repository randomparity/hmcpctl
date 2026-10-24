"""Admit and atomically publish live-run evidence."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import check_capability_inventory

from live_test import config as live_config
from live_test.capture import destination_is_ignored

if TYPE_CHECKING:
    from live_test.runner import RunState


#: The environment a live observation was made in. Both or neither: a lone key
#: is a configuration error, caught at startup rather than after a hardware run.
ENVIRONMENT_KEYS = ("LIVE_TEST_ENV_HMC_RELEASE", "LIVE_TEST_ENV_HARDWARE_FAMILY")

#: A run records its release with the maintenance level, so one HMC is never
#: catalogued under two tokens (#1335). Stricter than the catalog's own
#: `HMC_RELEASE`, which still admits the bare `V<n>R<n>` rows recorded earlier.
RUN_HMC_RELEASE = re.compile(r"V\d+R\d+M\d+")


def write_results(path: Path, document: str) -> None:
    """Atomically replace the persisted live-test report."""
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(document)
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def read_environment(path: Path | None = None) -> tuple[str, str] | None:
    """Read the observation environment from `.env`: both keys, or neither."""
    path = path or live_config.ENV_FILE
    values: dict[str, str] = {}
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key in ENVIRONMENT_KEYS and value:
                values[key] = value
    if not values:
        return None
    if len(values) != len(ENVIRONMENT_KEYS):
        raise ValueError(
            "invalid live-test configuration: "
            + " and ".join(ENVIRONMENT_KEYS)
            + " must be set together"
        )
    # Grammars applied here rather than at copy-in: these two strings are the only
    # free text an observation carries, and a hostname or a serial typed into
    # either would otherwise be written to disk and discovered only when a human
    # pastes it into `maturity.json`. The message never echoes the value.
    release, family = values[ENVIRONMENT_KEYS[0]], values[ENVIRONMENT_KEYS[1]]
    invalid = [
        f"{key} does not match its grammar (expected {form})"
        for key, value, pattern, form in (
            (
                ENVIRONMENT_KEYS[0],
                release,
                RUN_HMC_RELEASE,
                "V<n>R<n>M<n>, e.g. V10R3M1060",
            ),
            (
                ENVIRONMENT_KEYS[1],
                family,
                check_capability_inventory.HARDWARE_FAMILY,
                "POWER<n>, e.g. POWER10",
            ),
        )
        if not pattern.fullmatch(value)
    ]
    if invalid:
        raise ValueError("invalid live-test configuration: " + ", ".join(invalid))
    return release, family


def _git(repo_root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def repository_root() -> Path | None:
    """The repository the runner is checked out in, whatever directory it ran from.

    The caller's directory can be outside Git or inside another checkout. Only
    the canonical tracked source can identify HEAD, cleanliness and closures.
    """
    module = Path(__file__).resolve()
    result = _git(module.parent, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        return None
    root = Path(result.stdout.strip()).resolve()
    canonical = root / "scripts" / "live_test" / "evidence.py"
    if (
        module != canonical
        or canonical.is_symlink()
        or not canonical.is_file()
        or not (root / "src" / "hmcpctl").is_dir()
    ):
        return None
    tracked = _git(
        root, "ls-files", "--error-unmatch", "--", "scripts/live_test/evidence.py"
    )
    return root if tracked.returncode == 0 else None


def run_provenance(
    tasks: Sequence[int],
    group: str | None,
    repo_root: Path | None,
    schema_version: str,
    partial: bool,
    *,
    detach_probe: bool = False,
) -> dict[str, Any]:
    """What this run was, so a matrix taken from it can be dated.

    Written unconditionally, unlike the sibling observations document, which is
    skipped when nothing resolves or the source has no checkout — the run that
    fails early is the one whose provenance matters most. Without a source
    checkout the commit is `None`, which is a reportable state; a missing block
    is not.

    `tree_clean` qualifies `commit`: with `src` or `scripts` dirty the sha names
    a tree that was not the one exercised, so evidence cannot cite it.

    `schema_version` is the resolved `HMC_SCHEMA_VERSION` (`(not set)` when
    unset), the same string the run header prints, so a matrix can say which
    request environment produced it.

    `partial` is true when an exception or interrupt ended the subtask loop:
    `subtasks` then names what was selected, not what ran, and only the rows
    recorded before the stop are present.
    """
    commit: str | None = None
    tree_clean: bool | None = None
    if repo_root is not None:
        head = _git(repo_root, "rev-parse", "HEAD")
        if head.returncode == 0:
            commit = head.stdout.strip()
        tree_clean = _tree_is_clean(repo_root)
    provenance = {
        "tested_commit": commit,
        "tree_clean": tree_clean,
        "group": group,
        "subtasks": list(tasks),
        "schema_version": schema_version,
        "finished": datetime.now(UTC).isoformat(),
        "partial": partial,
    }

    if detach_probe:
        provenance["detach_probe"] = True
    return provenance


def observations_path(results_path: str) -> Path:
    """The observations file written beside a run's results document."""
    path = Path(results_path)
    return path.with_name(f"{path.stem}-observations.json")


def _tree_is_clean(repo_root: Path) -> bool:
    """Report whether the implementation the observation names is what is committed."""
    result = _git(repo_root, "status", "--porcelain", "--", "src", "scripts")
    return result.returncode == 0 and not result.stdout.strip()


def emit_observations(
    state: RunState,
    path: Path,
    environment: tuple[str, str] | None,
    schema_version: str,
    repo_root: Path,
) -> bool:
    """Write the run's catalog-shaped observations, or say why it wrote none."""
    if environment is None:
        print("no LIVE_TEST_ENV_* settings — observations not written")
        return False
    if not check_capability_inventory.SCHEMA_VERSION.fullmatch(schema_version):
        print(
            "HMC_SCHEMA_VERSION is not V<n>_<n>[_<n>...] or unset — observations not written"
        )
        return False
    if not state.observations and not state.gaps:
        print("no verified observations or confirmed gaps — nothing to write")
        return False
    if not _tree_is_clean(repo_root):
        print("src/ or scripts/ is modified — observations not written")
        return False
    # `--results-file` lets an operator name any stem, so no fixed `.gitignore`
    # pattern can establish that the destination is ignored; ask Git instead.
    # `_run_from_arguments` checks the same thing before the run; this covers a
    # direct call to `main`.
    if not destination_is_ignored(path):
        print(f"{path} is not ignored by git — observations not written")
        return False
    head = _git(repo_root, "rev-parse", "HEAD")
    if head.returncode != 0:
        print("cannot resolve HEAD — observations not written")
        return False
    handlers = {
        tool.operation: tool.handler
        for tool in check_capability_inventory.discover_registry()
    }
    document: list[dict[str, Any]] = []
    seen: set[str] = set()
    for recorded in state.observations:
        handler = handlers.get(recorded["operation"])
        if handler is None:
            # Skip the one unresolvable row rather than discarding the document:
            # every other observation came from the same expensive hardware run,
            # and `test_verified_scenarios_name_registered_operations` is what
            # catches a mistyped operation before it ever reaches a live run.
            print(
                f"  ⚠️  unknown operation {recorded['operation']} — observation skipped"
            )
            continue
        observation = dict(recorded["observation"])
        if observation["id"] in seen:
            print(
                f"duplicate observation id {observation['id']} — observations not written"
            )
            return False
        seen.add(observation["id"])
        observation["tested_commit"] = head.stdout.strip()
        observation["hmc_release"], observation["hardware_family"] = environment
        observation["schema_version"] = schema_version
        observation["closure_fingerprint"] = (
            check_capability_inventory.closure_fingerprint(
                repo_root, handler.rsplit(".", 1)[0]
            )
        )
        document.append(
            {"operation": recorded["operation"], "observation": observation}
        )
    for recorded in state.gaps:
        handler = handlers.get(recorded["operation"])
        if handler is None:
            print(f"unknown operation {recorded['operation']} — gap skipped")
            continue
        scope = dict(recorded["missing_scope"])
        scope["confirmation"] = {
            **scope["confirmation"],
            "tested_commit": head.stdout.strip(),
            "hmc_release": environment[0],
            "hardware_family": environment[1],
            "closure_fingerprint": check_capability_inventory.closure_fingerprint(
                repo_root, handler.rsplit(".", 1)[0]
            ),
        }
        document.append({"operation": recorded["operation"], "missing_scope": scope})
    if not document:
        print("no resolvable observations — nothing written")
        return False
    write_results(path, json.dumps(document, indent=2))
    print(f"Observations written to {path}")
    return True


def load_known_gaps(
    environment: tuple[str, str] | None,
    repo_root: Path,
    catalog_path: Path | None = None,
) -> set[tuple[str, str]]:
    document = check_capability_inventory.load_json(
        catalog_path or repo_root / "docs/capabilities/maturity.json"
    )
    errors: list[str] = []
    check_capability_inventory._exact_keys(
        document,
        {"format_version", "admission_policy", "operations"},
        "maturity.json",
        errors,
    )
    if (
        type(document.get("format_version")) is not int
        or document.get("format_version")
        != check_capability_inventory.MATURITY_FORMAT_VERSION
        or document.get("admission_policy") != "existing-runtime-guards"
    ):
        errors.append("maturity.json: invalid format version or admission policy")
    records = check_capability_inventory._objects(
        check_capability_inventory._array(document, "operations", errors),
        "maturity operations",
        errors,
    )
    handlers = {
        tool.operation: tool.handler
        for tool in check_capability_inventory.discover_registry()
    }
    check_capability_inventory._validate_maturity(records, set(handlers), errors)
    if errors:
        raise ValueError("invalid gap catalog: " + "; ".join(errors))
    known: set[tuple[str, str]] = set()
    if environment is not None:
        for record in records:
            implementation = cast(dict[str, Any], record["implementation"])
            operation = cast(str, record["operation"])
            for scope in implementation["missing_scope"]:
                if "confirmation" not in scope:
                    continue
                fingerprint = check_capability_inventory.closure_fingerprint(
                    repo_root, handlers[operation].rsplit(".", 1)[0]
                )
                if check_capability_inventory.gap_is_current(
                    scope["confirmation"], environment, fingerprint
                ):
                    known.add((operation, scope["variant"]))
    return known
