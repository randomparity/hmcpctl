"""Composition and startup refuse without an access policy.

Covers docs/workflow/specs/2026-08-19-fail-closed-startup-design.md.

Spec item -> node id:
  R1   test_composing_without_a_policy_is_refused
  R1   test_composing_with_an_explicit_none_is_refused
  R2   test_the_module_level_application_is_gone
  R3   test_both_gates_are_derived_and_neither_is_none
  R4   test_the_transports_take_a_required_policy
  R5   test_serve_without_a_policy_exits_2
  R5a  test_serve_with_an_absent_policy_file_exits_1
  R5b  test_an_unresolvable_config_home_refuses_without_a_traceback
  R5b  test_a_dangling_symlink_is_not_reported_as_an_absent_file
  R6   test_the_unselected_policy_warning_is_gone
  R12  test_the_generated_policy_authorizes_an_omitted_optional_selector
  R13  test_the_generated_policy_authorizes_a_vios_partition_id_call
  R14  test_the_generated_policy_denies_the_arbitrary_command
  --   test_a_denial_bounds_the_callers_own_token
  R16e test_error_text_survives_square_brackets
  R16e test_error_text_survives_a_closing_tag_shape
  G1   test_the_composer_has_no_default_policy
  G2   test_the_legacy_policy_composes_the_registry_the_default_used_to
  G3   test_no_module_composes_an_application_at_import
  L1   test_serve_without_a_policy_exits_2_as_a_subprocess
  L2   test_the_documented_migration_works_end_to_end

R11 and R11a live in tests/app/test_cli_commands/config.py; R7-R10, R9a and R9b in
tests/unit/test_legacy_policy.py. Each of the three modules carries its own header and
its own inventory guard, because the guard pattern reads ``__file__`` and cannot span
modules. R16, R16a-R16d and R17 are documentation requirements with no node id.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import typer
from rendered_output import collapse_whitespace
from typer.testing import CliRunner

from hmcpctl import server as server_module
from hmcpctl.authorization.access_policy import DEFAULT_CONNECTION_TOKEN
from hmcpctl.authorization.dispatch_scope import dispatch_authorizer
from hmcpctl.cli import app
from hmcpctl.cli_commands.legacy_policy import LEGACY_POLICY_NAME, compile_legacy_policy
from hmcpctl.server import TOOL_SECURITY, create_mcp

REPO_ROOT = Path(__file__).parents[2]


@pytest.fixture
def steer_config(tmp_path, monkeypatch):
    """Point `config_dir()` at a temporary directory, on every platform.

    `HOME` alone does not steer it: Linux reads `XDG_CONFIG_HOME` first and darwin reads
    `Path.home()` with no override at all. Without all four steps these tests would read
    the developer's own profiles, and the generator tests would write a real
    `access-policy.toml` into their config directory.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    from hmcpctl.config import config_dir

    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _legacy_policy(*, include_arbitrary_command: bool = False):
    return compile_legacy_policy(
        TOOL_SECURITY,
        (DEFAULT_CONNECTION_TOKEN,),
        include_arbitrary_command=include_arbitrary_command,
    )


def _names(application) -> set[str]:
    return {tool.name for tool in asyncio.run(application.list_tools())}


def _unstyle(text: str) -> str:
    """Strip ANSI styling so an assertion reads the words, not the escape codes."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _plain(text: str) -> str:
    """The rendered words alone, unstyled and rejoined across Rich's wraps (#1621)."""
    return collapse_whitespace(_unstyle(text))


def _console_script(*, proof: str) -> str:
    """This checkout's `hmcpctl` console script, or skip.

    The same-checkout assertion `test_authorization_audit_live.py` makes: a proof that
    silently ran against another build is worse than no proof. `proof` names what the
    caller was about to prove, so the failure says which guarantee would have been made
    about the wrong build.

    L1 and L2 only, rather than a `tests/app/conftest.py` the other copies of this form
    could share: `pythonpath = ["tests"]` plus pytest's prepend import mode makes a
    second `conftest.py` shadow `tests/conftest.py` for the 49 modules that reach it
    with `from conftest import ...`.
    """
    executable = shutil.which("hmcpctl")
    if executable is None:
        pytest.skip("the hmcpctl console script is not on PATH")
    assert str(REPO_ROOT) in str(Path(executable).resolve().parents[1]), (
        f"the console script resolves outside this checkout, so the {proof} proved would "
        "belong to a different build of hmcpctl"
    )
    return executable


# ---------------------------------------------------------------------------
# R1 / G1 — composition requires a policy
# ---------------------------------------------------------------------------


def test_composing_without_a_policy_is_refused():
    """R1: the unbounded composition is gone, not merely discouraged."""
    with pytest.raises(TypeError):
        create_mcp()  # ty: ignore[missing-argument]


def test_composing_with_an_explicit_none_is_refused():
    """R1: an annotation does not refuse at runtime, so a guard has to.

    Without it ``create_mcp(None)`` would compose an application with no ceiling and no
    authorizer — exactly the state this change exists to remove — and every type checker
    in the world would still be happy at the call site that passed a ``None`` it got from
    somewhere else.
    """
    with pytest.raises(TypeError, match="init-access-policy"):
        create_mcp(None)  # ty: ignore[invalid-argument-type]


def test_the_composer_has_no_default_policy():
    """G1: a restored default would make R1's guard unreachable and silent."""
    parameter = inspect.signature(create_mcp).parameters["policy"]

    assert parameter.default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# R2 / G3 — no application exists at import
# ---------------------------------------------------------------------------


def test_the_module_level_application_is_gone():
    """R2: the one object that could have been an unbounded served application."""
    assert not hasattr(server_module, "mcp")

    with pytest.raises(ImportError):
        from hmcpctl.server import mcp  # noqa: F401


def test_no_module_composes_an_application_at_import():
    """G3: import-time composition is what R2 removes; keep it removed.

    Parsed rather than grepped: a comment or a docstring mentioning ``create_mcp()``
    must not fail this, and a call nested inside a function is fine — only module scope
    is the hazard.
    """
    offenders = []
    for path in [
        *sorted((REPO_ROOT / "src" / "hmcpctl").glob("*.py")),
        *sorted((REPO_ROOT / "scripts").glob("*.py")),
    ]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            # A call inside a function or class body runs when that is called, not at
            # import, so only the statements that execute on import are walked.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if name == "create_mcp":
                    offenders.append(f"{path.name}:{call.lineno}")

    assert not offenders, f"application composed at import: {offenders}"


# ---------------------------------------------------------------------------
# R3 / R4 — the gates and the transports
# ---------------------------------------------------------------------------


def test_both_gates_are_derived_and_neither_is_none():
    """R3: a site given one gate without the other registers what it cannot authorize."""
    permits, authorize = server_module._gates(_legacy_policy())

    assert permits is not None and authorize is not None
    assert permits("hmc_list_systems") is True
    assert permits("hmc_run_command") is False


@pytest.mark.parametrize("entry_point", ["main_stdio", "main_http"])
def test_the_transports_take_a_required_policy(entry_point):
    """R4: neither transport can be entered without one."""
    parameter = inspect.signature(getattr(server_module, entry_point)).parameters[
        "access_policy"
    ]

    assert parameter.default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# G2 — legacy equivalence, as a test rather than as prose
# ---------------------------------------------------------------------------


def test_the_legacy_policy_composes_the_registry_the_default_used_to():
    """G2: the exact assertion the retired `test_no_policy_applies_no_ceiling` made.

    That test asserted `create_mcp()` registered `set(TOOL_SECURITY) - {hmc_run_command}`.
    The unpolicied composition is gone, so the claim moves to the policy that replaces it:
    this is what "legacy-equivalent" means, checked rather than asserted in a comment.
    """
    assert _names(create_mcp(_legacy_policy())) == set(TOOL_SECURITY) - {
        "hmc_run_command"
    }


def test_the_generated_policy_denies_the_arbitrary_command():
    """R14: the flag stays insufficient on its own (epic #218 requirement 6)."""
    assert "hmc_run_command" not in _names(create_mcp(_legacy_policy()))
    assert _legacy_policy(include_arbitrary_command=True).permits_tool(
        "hmc_run_command"
    )


# ---------------------------------------------------------------------------
# R12 / R13 — the two properties that make the generator legacy-*equivalent*
# ---------------------------------------------------------------------------


def test_the_generated_policy_authorizes_an_omitted_optional_selector():
    """R12: `all-targets` covers `target_scope.ABSENT`, and no targets table does.

    `hmc_list_lpars` declares an optional `system_name_or_uuid`; omitting it means "every
    system". Under a table-constrained grant that is a denial, so a generator that
    narrowed targets would silently retire every call that leaves an optional argument
    unset — a large fraction of ordinary use, and the reason the generated grant can only
    be `all-targets`.
    """
    authorize = dispatch_authorizer(_legacy_policy())
    security = TOOL_SECURITY["hmc_list_lpars"]

    # None, exactly as `tool_registry.authorized` binds an omitted optional argument.
    authorize(
        "hmc_list_lpars",
        security,
        {"profile": None, "system_name_or_uuid": None},
    )


def test_the_generated_policy_authorizes_a_vios_partition_id_call():
    """R13: the surface's only non-string selector, carried by three live tools.

    `target_scope._value` renders an int through `str()`. Without that arm these calls
    read as `UNREADABLE`, which denies under `all-targets` too — so the legacy-equivalent
    guarantee would quietly stop covering them.
    """
    authorize = dispatch_authorizer(_legacy_policy())

    authorize(
        "hmc_add_vscsi_adapter",
        TOOL_SECURITY["hmc_add_vscsi_adapter"],
        {
            "profile": None,
            "lpar_name_or_uuid": "db-01",
            "vios_partition_id": 2,
            # #259: the tool now declares this optional selector too.
            "system_name_or_uuid": None,
        },
    )


def test_a_denial_bounds_the_callers_own_token():
    """A denial echoes no more of a caller's input than the audit record keeps.

    Not a spec requirement — raised by the security pass on this branch. The same value
    in the same call is truncated to `audit.MAX_VALUE_LENGTH` on its way into the
    record, and was unbounded on its way into the message FastMCP renders back to the
    client. ADR 0041 is what makes that path universal rather than opt-in, which is why
    it is closed here rather than left to the layer that has always had it.
    """
    from hmcpctl.audit.records import MAX_VALUE_LENGTH
    from hmcpctl.authorization.connection_scope import ConnectionScopeError

    authorize = dispatch_authorizer(_legacy_policy())
    oversized = "z" * (MAX_VALUE_LENGTH * 40)

    with pytest.raises(ConnectionScopeError) as denial:
        authorize(
            "hmc_list_systems",
            TOOL_SECURITY["hmc_list_systems"],
            {"profile": oversized, "state": None, "limit": None},
        )

    assert oversized not in str(denial.value)
    assert "z" * MAX_VALUE_LENGTH in str(denial.value)


# ---------------------------------------------------------------------------
# R6 — the warning whose condition is now unreachable
# ---------------------------------------------------------------------------


def test_the_unselected_policy_warning_is_gone():
    """R6: no server starts without a policy, so nothing can warn that none was selected."""
    assert not hasattr(server_module, "_unselected_policy_file")

    lines = server_module._startup_warnings(129, _legacy_policy(), False)

    assert not any("no access policy was selected" in line for line in lines)


# ---------------------------------------------------------------------------
# #279 — a mixed effect-resolved grant warns at startup instead of loading silent
# ---------------------------------------------------------------------------


def test_a_mixed_effect_grant_warns_at_startup():
    """#279: an effects-only grant reaching a table-unboundable tool is diagnosed.

    Before the fix, `effects = ["mutate"]` beside a `managed_system` table loaded
    with no diagnostic at all, even though it resolves `hmc_provision_lpar` (and
    four siblings) whose declaration is `exhaustive_targets=False` -- every call
    to any of the five is denied under `target-unboundable`
    (`target_scope.py`) regardless of argument. `_startup_warnings` is where an
    operator already looks for what a compiled policy means for this run, so
    that is where the dead subset is named.
    """
    from hmcpctl.authorization.access_policy import compile_access_policy

    policy = compile_access_policy(
        {
            "policies": {
                "lab": {
                    "grants": [
                        {
                            "effects": ["mutate"],
                            "connections": ["lab"],
                            "targets": {"managed_system": ["S1"]},
                        }
                    ]
                }
            }
        },
        "lab",
        TOOL_SECURITY,
        "access-policy.toml",
    )

    lines = server_module._startup_warnings(129, policy, False)

    matches = [line for line in lines if "'hmc_provision_lpar'" in line]
    assert len(matches) == 1
    assert matches[0].startswith("warning: ")
    assert "all-targets" in matches[0]


# ---------------------------------------------------------------------------
# R16e — CLI error text is not markup
# ---------------------------------------------------------------------------


def test_error_text_survives_square_brackets(capsys):
    """R16e: rich deletes a bracketed token, and that token is R9b's whole payload.

    Verified against this checkout before the fix: `[profiles." prod"]` rendered as an
    empty string, so the generator's most important diagnostic named a key the operator
    could not see.
    """
    from hmcpctl.cli_commands.output import fail

    with pytest.raises(typer.Exit):
        fail(ValueError('came from a [profiles." prod"] key in config.toml'))

    assert '[profiles." prod"]' in _plain(capsys.readouterr().err)


def test_error_text_survives_a_closing_tag_shape(capsys):
    """R16e: and a closing-tag shape raises MarkupError, replacing exit 1 with a crash.

    `_check_entries` renders the offending value under `repr()`, so a profile key like
    `[/prod]` reaches this helper as literal text and must not be parsed as markup.
    """
    from hmcpctl.cli_commands.output import usage_error

    with pytest.raises(typer.Exit):
        usage_error("connections entry '[/prod]' is empty or padded")

    assert "'[/prod]'" in _plain(capsys.readouterr().err)


# ---------------------------------------------------------------------------
# R5 / R5a / R5b — the two refusals
# ---------------------------------------------------------------------------


def test_serve_without_a_policy_exits_2(steer_config):
    """R5: a usage error, naming the generator rather than only the problem."""
    result = CliRunner().invoke(app, ["serve"])

    assert result.exit_code == 2
    output = _plain(result.output)
    assert "config init-access-policy" in output
    assert "--access-policy" in output


def test_serve_with_an_absent_policy_file_exits_1(steer_config):
    """R5a: the likeliest upgrade order is edit the launcher, then find the file missing.

    Left alone that reaches `load_access_policy`'s unreadable-file arm and prints
    `cannot be read: [Errno 2] No such file or directory`, which names neither the
    generator nor the remedy.
    """
    result = CliRunner().invoke(app, ["serve", "--access-policy", LEGACY_POLICY_NAME])

    assert result.exit_code == 1
    assert "config init-access-policy" in _plain(result.output)


def test_an_unresolvable_config_home_refuses_without_a_traceback(monkeypatch):
    """R5b: `config_dir()` reaches `Path.home()`, which raises under a uid with no HOME.

    Unguarded, the refusal would raise while rendering itself — a traceback instead of a
    refusal, in the deployment least able to read one. This is what
    `_unselected_policy_file` guarded and what its removal must not take with it.
    """
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)

    def _no_home(cls):
        raise RuntimeError("Could not determine home directory")

    monkeypatch.setattr(Path, "home", classmethod(_no_home))

    result = CliRunner().invoke(app, ["serve"])

    assert result.exit_code == 2
    assert "config init-access-policy" in _plain(result.output)


# Narrower than "cannot be read", so Rich must break that phrase wherever the path ends;
# 40 columns wraps it only for some temporary-path lengths (#1621). It must also stay at
# least len("access-policy"): any narrower and Rich folds that word mid-word, which turns
# the "no access-policy file at" negative back into one that cannot fail.
_PHRASE_SPLITTING_COLUMNS = len("cannot be read") - 1


@pytest.mark.skipif(os.name != "posix", reason="symlink semantics are POSIX here")
@pytest.mark.parametrize("columns", [None, _PHRASE_SPLITTING_COLUMNS])
def test_a_dangling_symlink_is_not_reported_as_an_absent_file(
    steer_config, monkeypatch, columns
):
    """R5b: `exists()` follows the link; `O_EXCL` does not.

    Symlinking the platform-native path at a mounted or packaged policy is the natural
    workaround for a container or unit, because `--access-policy` takes a NAME and no
    option takes a path. If the target is absent or not yet mounted, treating the link
    as an absent file sends the operator to a generator that then refuses with "already
    exists" — two refusals, neither mentioning a symlink and neither remedy applying.
    Reporting it as present hands the case to the load path, whose error names the
    resolved target and the errno.
    """
    if columns is not None:
        monkeypatch.setenv("COLUMNS", str(columns))
    link = steer_config / "access-policy.toml"
    link.symlink_to(steer_config / "nowhere" / "access-policy.toml")

    result = CliRunner().invoke(app, ["serve", "--access-policy", LEGACY_POLICY_NAME])

    assert result.exit_code == 1
    output = _plain(result.output)
    assert "no access-policy file at" not in output
    assert "cannot be read" in output


# ---------------------------------------------------------------------------
# L1 — the refusal as an operator actually meets it
# ---------------------------------------------------------------------------


def test_serve_without_a_policy_exits_2_as_a_subprocess():
    """L1: an in-process assertion cannot see the exit code an operator sees.

    The console script, not `python -m hmcpctl`: since ADR 0128 both reach the same
    `main`, but the console script is what an operator actually runs, and the exit
    code an operator meets is the whole of what this test proves.
    """
    executable = _console_script(proof="refusal")

    completed = subprocess.run(
        [executable, "serve"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 2
    assert "config init-access-policy" in _plain(completed.stderr)


@pytest.mark.skipif(
    os.name != "posix",
    reason="the fixture steers config_dir() through HOME; on win32 config_dir() reads "
    "APPDATA while Path.home() reads USERPROFILE, so the steering does not hold",
)
def test_the_documented_migration_works_end_to_end(tmp_path):
    """L2: generate, refuse to regenerate, then actually serve on what was written.

    The only check here that *writes* through a subprocess, so its isolation is
    explicit rather than by reference: `HOME` is redirected, and `XDG_CONFIG_HOME` and
    `APPDATA` are removed from the child environment — setting `HOME` alone does not
    steer `config_dir()` on Linux, and the failure mode is creating a real
    `access-policy.toml` in the developer's own config directory, silently changing what
    `--access-policy` resolves to for them afterwards.

    `HMC_HOST` and `HMC_PROFILE` go too: they change which connection a call selects,
    and a developer with either exported would otherwise be testing their machine.
    """
    executable = _console_script(proof="migration")

    env = dict(os.environ)
    for name in ("HMC_HOST", "HMC_PROFILE", "XDG_CONFIG_HOME", "APPDATA"):
        env.pop(name, None)
    env["HOME"] = str(tmp_path)

    generated = subprocess.run(
        [executable, "config", "init-access-policy"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert generated.returncode == 0, generated.stderr

    # Taken from the command's own stdout rather than joined by hand: `config_dir()`
    # is `~/Library/Application Support/hmcpctl` on darwin and `~/.config/hmcpctl`
    # elsewhere, and a hand-built path would pin one platform's answer. Asserting it
    # lands under the steered HOME is what proves the isolation actually held — the
    # thing that matters, since this is the one check that writes.
    target = Path(_unstyle(generated.stdout).strip())
    assert str(target).startswith(str(tmp_path)), (
        f"wrote outside the steered HOME: {target}"
    )
    assert target.name == "access-policy.toml"
    assert target.exists(), f"nothing written at {target}"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    written = target.read_bytes()

    # Second run: refuses, changes nothing, and names the way forward.
    again = subprocess.run(
        [executable, "config", "init-access-policy"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert again.returncode == 1
    assert target.read_bytes() == written
    assert "--output" in _plain(again.stdout + again.stderr)

    # And the file it wrote is one a server actually starts on. `initialize` plus
    # `tools/list` over raw newline-delimited JSON-RPC, deliberately not a client
    # library: anything the server prints outside the protocol shows up as an
    # unparseable line on stdout and is observable.
    server = subprocess.Popen(
        [executable, "serve", "--access-policy", LEGACY_POLICY_NAME],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "l2", "version": "0"},
            },
        }
        assert server.stdin is not None and server.stdout is not None
        server.stdin.write(json.dumps(request) + "\n")
        server.stdin.flush()
        line = server.stdout.readline()
    finally:
        try:
            if server.stdin is not None and not server.stdin.closed:
                server.stdin.close()
            if server.poll() is None:
                server.kill()
            server.wait(timeout=30)
        finally:
            for stream in (server.stdout, server.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

    assert line, "the server produced no initialize reply"
    reply = json.loads(line)
    assert reply["id"] == 1
    assert "result" in reply, reply


# ---------------------------------------------------------------------------
# Inventory guard — see the module docstring
# ---------------------------------------------------------------------------


def test_every_spec_numbered_test_named_in_the_header_still_exists():
    """A header naming a deleted test is worse than no header.

    On #224 a slice-replacement refactor removed the only test that could observe a
    regression and left 2011 tests green.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    header = source.split('"""', 2)[1]

    named = set(re.findall(r"^  \w+\s+(test_\w+)$", header, flags=re.MULTILINE))
    defined = set(re.findall(r"^def (test_\w+)", source, flags=re.MULTILINE))

    assert named, "the header maps no test; the guard would pass vacuously"
    assert named <= defined, (
        f"named in the header but not defined: {sorted(named - defined)}"
    )
