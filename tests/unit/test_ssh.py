"""Tests for SSH passthrough (run_hmc_command) — asyncssh mocked."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest
from conftest import make_config

from hmcpctl.config import HMCConfig, load_profile
from hmcpctl.errors import HMCError
from hmcpctl.ssh.transport import HMCCLIError, open_hmc_connection, run_hmc_command


def test_ssh_verification_configuration(tmp_path, monkeypatch):
    assert "ssh_verify_host_key" in HMCConfig.model_fields
    assert HMCConfig.from_mapping({}).ssh_verify_host_key is True
    config_path = tmp_path / "config.toml"
    config_path.write_text("[profiles.test]\nssh_verify_host_key = false\n")
    assert load_profile("test", config_path).ssh_verify_host_key is False
    monkeypatch.setenv("HMC_SSH_VERIFY_HOST_KEY", "true")
    assert load_profile("test", config_path).ssh_verify_host_key is True
    monkeypatch.setenv("HMC_SSH_VERIFY_HOST_KEY", "false")
    assert HMCConfig().ssh_verify_host_key is False
    assert HMCConfig.from_mapping({}).ssh_verify_host_key is True
    monkeypatch.setenv("HMC_SSH_VERIFY_HOST_KEY", "invalid")
    with pytest.raises(ValueError, match="ssh_verify_host_key"):
        HMCConfig()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [run_hmc_command, open_hmc_connection])
@pytest.mark.parametrize("verify", [True, False])
@pytest.mark.parametrize("key_file", [None, "/test-key"])
async def test_ssh_host_key_policy_reaches_each_connection(
    operation, verify, key_file, caplog
):
    config = make_config(ssh_verify_host_key=verify, ssh_key_file=key_file)
    connection = _make_ssh_mock()
    connect = (
        MagicMock(return_value=connection)
        if operation is run_hmc_command
        else AsyncMock()
    )
    with patch("hmcpctl.ssh.transport.asyncssh.connect", connect):
        if operation is run_hmc_command:
            await operation(config, "lshmc -V")
        else:
            await operation(config)
    expected = str(Path.home() / ".ssh" / "known_hosts") if verify else None
    assert connect.call_args.kwargs["known_hosts"] == expected
    assert ("SSH host-key verification disabled" in caplog.text) is (not verify)
    if not verify:
        assert config.host in caplog.text
        assert config.password not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [run_hmc_command, open_hmc_connection])
@pytest.mark.parametrize(
    "trust", ["trusted", "changed", "unknown", "missing", "malformed", "insecure"]
)
async def test_ssh_host_key_handshake_precedes_password(
    operation, trust, tmp_path, monkeypatch
):
    """Use a real local SSH peer; rejected keys must never receive credentials."""
    passwords = []

    class Server(asyncssh.SSHServer):
        def begin_auth(self, username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            passwords.append(password)
            return True

    def process_handler(process):
        process.stdout.write("verified\n")
        process.exit(0)

    host_key = asyncssh.generate_private_key("ssh-ed25519")
    async with asyncssh.create_server(
        Server,
        "127.0.0.1",
        0,
        server_host_keys=[host_key],
        process_factory=process_handler,
    ) as listener:
        port = listener.get_port()
        trust_file = tmp_path / ".ssh" / "known_hosts"
        trust_file.parent.mkdir()
        if trust != "missing":
            recorded_key = (
                asyncssh.generate_private_key("ssh-ed25519")
                if trust == "changed"
                else host_key
            )
            entry = f"[127.0.0.1]:{port} " + recorded_key.export_public_key().decode()
            trust_file.write_text(
                {"unknown": "", "malformed": "invalid-entry\n"}.get(trust, entry)
            )
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        # Pin an ephemeral port and isolate ambient SSH config, keeping the actual handshake.
        connect = asyncssh.connect
        monkeypatch.setattr(
            asyncssh,
            "connect",
            lambda **kwargs: connect(port=port, config=None, **kwargs),
        )
        config = HMCConfig.from_mapping(
            {
                "host": "127.0.0.1",
                "user": "test",
                "password": "test-password",  # pragma: allowlist secret - loopback fixture
                "ssh_verify_host_key": trust != "insecure",
                "ssh_timeout": 5,
            }
        )

        async def invoke():
            if operation is run_hmc_command:
                assert await operation(config, "lshmc -V") == "verified\n"
            else:
                connection = await operation(config)
                connection.close()
                await connection.wait_closed()

        if trust in {"trusted", "insecure"}:
            await invoke()
            assert passwords == ["test-password"]
        else:
            with pytest.raises(HMCCLIError) as exc:
                await invoke()
            expected_error = {"missing": OSError, "malformed": ValueError}.get(
                trust, asyncssh.HostKeyNotVerifiable
            )
            assert isinstance(exc.value.__cause__, expected_error)
            if trust == "malformed":
                assert "Invalid known hosts entry" in str(exc.value)
            assert passwords == []


# ---------------------------------------------------------------------------
# Helpers to build a minimal asyncssh mock
# ---------------------------------------------------------------------------


def _make_ssh_mock(stdout: str = "output\n") -> MagicMock:
    """Return a mock asyncssh connection whose run() returns stdout."""
    result = MagicMock()
    result.stdout = stdout
    result.stderr = ""

    conn = AsyncMock()
    conn.run = AsyncMock(return_value=result)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    return conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [run_hmc_command, open_hmc_connection])
async def test_invalid_credentials_preserve_value_error(operation):
    config = HMCConfig.from_mapping({})
    with pytest.raises(ValueError, match="Missing HMC configuration"):
        if operation is run_hmc_command:
            await operation(config, "lshmc -V")
        else:
            await operation(config)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [run_hmc_command, open_hmc_connection])
async def test_os_connection_failures_use_hmc_cli_error(operation):
    with (
        patch(
            "hmcpctl.ssh.transport.asyncssh.connect",
            side_effect=ConnectionRefusedError("connection refused"),
        ),
        pytest.raises(HMCCLIError, match="SSH .*failed") as exc_info,
    ):
        if operation is run_hmc_command:
            await operation(make_config(), "lshmc -v")
        else:
            await operation(make_config())

    assert isinstance(exc_info.value.__cause__, ConnectionRefusedError)


@pytest.mark.asyncio
async def test_run_hmc_command_password_auth():
    """Password auth path: asyncssh.connect called with password and key suppression.

    client_keys=[] and preferred_auth='password' are required to skip key
    negotiation on HMC appliances, which enforce a low MaxAuthTries limit
    and lock out accounts when every agent key is tried before the password.
    """
    conn_mock = _make_ssh_mock("lssyscfg output\n")

    with patch(
        "hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock
    ) as mock_connect:
        result = await run_hmc_command(make_config(), "lssyscfg -r sys")

    mock_connect.assert_called_once()
    call_kwargs = mock_connect.call_args.kwargs
    assert call_kwargs["host"] == "hmc.test"
    assert call_kwargs["username"] == "hscroot"
    assert call_kwargs["password"] == "abc123"
    assert call_kwargs["client_keys"] == []
    assert call_kwargs["preferred_auth"] == "password"
    assert result == "lssyscfg output\n"


@pytest.mark.asyncio
async def test_run_hmc_command_key_auth():
    """Key auth path: asyncssh.connect called with client_keys, password=None."""
    conn_mock = _make_ssh_mock("lssyscfg key output\n")

    with patch(
        "hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock
    ) as mock_connect:
        result = await run_hmc_command(
            make_config(ssh_key_file="/home/user/.ssh/hmc_key"),
            "lssyscfg -r sys",
        )

    call_kwargs = mock_connect.call_args.kwargs
    assert call_kwargs["client_keys"] == ["/home/user/.ssh/hmc_key"]
    assert call_kwargs["password"] is None
    assert result == "lssyscfg key output\n"


@pytest.mark.asyncio
async def test_run_hmc_command_passes_cmd():
    """The exact command string is forwarded to conn.run() with the SSH timeout."""
    conn_mock = _make_ssh_mock("")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        await run_hmc_command(make_config(), "lshmc -v")

    conn_mock.run.assert_called_once_with("lshmc -v", check=True, timeout=300.0)


@pytest.mark.asyncio
async def test_run_hmc_command_command_timeout_raises_hmcclierror():
    """A command that exceeds ssh_timeout surfaces as HMCCLIError, not a hang."""
    conn_mock = _make_ssh_mock()
    conn_mock.run = AsyncMock(side_effect=TimeoutError("timed out"))

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock),
        pytest.raises(HMCCLIError, match="timed out after 300s") as exc_info,
    ):
        await run_hmc_command(make_config(), "lssyscfg -r sys")

    assert isinstance(exc_info.value, HMCError)


@pytest.mark.asyncio
async def test_run_hmc_command_connect_timeout_raises_hmcclierror():
    """A connect that never completes surfaces as HMCCLIError, not a hang."""
    with (
        patch(
            "hmcpctl.ssh.transport.asyncssh.connect",
            side_effect=TimeoutError("timed out"),
        ),
        pytest.raises(HMCCLIError, match="timed out after 300s"),
    ):
        await run_hmc_command(make_config(), "lssyscfg -r sys")


@pytest.mark.asyncio
async def test_run_hmc_command_honours_custom_ssh_timeout():
    """conn.run receives the configured ssh_timeout, not the hardcoded default."""
    conn_mock = _make_ssh_mock("")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        await run_hmc_command(make_config(ssh_timeout=45.0), "lssyscfg -r sys")

    conn_mock.run.assert_called_once_with("lssyscfg -r sys", check=True, timeout=45.0)


@pytest.mark.asyncio
async def test_run_hmc_command_nonzero_exit_raises_hmcclierror():
    """A non-zero command exit surfaces as HMCCLIError with stderr included.

    run_hmc_command translates asyncssh.ProcessError so callers never need to
    import the SSH library just to catch the exception type.
    """
    conn_mock = _make_ssh_mock()
    conn_mock.run = AsyncMock(
        side_effect=asyncssh.ProcessError(
            env={},
            command="lssyscfg -r sys",
            subsystem=None,
            exit_status=1,
            exit_signal=None,
            returncode=1,
            stdout="",
            stderr="HSCL0001 bad config",
        )
    )

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock),
        pytest.raises(HMCCLIError, match="HSCL0001 bad config") as exc_info,
    ):
        await run_hmc_command(make_config(), "lssyscfg -r sys")

    # HMCCLIError subclasses HMCError so REST and CLI failures share one type.
    assert isinstance(exc_info.value, HMCError)
    assert "lssyscfg -r sys" in str(exc_info.value)
    assert "exit status 1" in str(exc_info.value)


@pytest.mark.asyncio
async def test_run_hmc_command_signal_failure_names_signal_and_command():
    conn_mock = _make_ssh_mock()
    conn_mock.run = AsyncMock(
        side_effect=asyncssh.ProcessError(
            env={},
            command="lssyscfg -r sys",
            subsystem=None,
            exit_status=None,
            exit_signal="TERM",
            returncode=-15,
            stdout="",
            stderr="terminated",
        )
    )

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock),
        pytest.raises(HMCCLIError) as exc_info,
    ):
        await run_hmc_command(make_config(), "lssyscfg -r sys")

    message = str(exc_info.value)
    assert "lssyscfg -r sys" in message
    assert "signal TERM" in message
    assert "exit status None" not in message


@pytest.mark.asyncio
async def test_run_hmc_command_connect_error_raises_hmcclierror():
    """An SSH connection/auth failure surfaces as HMCCLIError, not a raw
    asyncssh error."""
    with (
        patch(
            "hmcpctl.ssh.transport.asyncssh.connect",
            side_effect=asyncssh.Error("connect", "connection refused"),
        ),
        pytest.raises(HMCCLIError, match="connection refused"),
    ):
        await run_hmc_command(make_config(), "lssyscfg -r sys")


def test_hmc_config_ssh_key_field_default():
    """ssh_key_file defaults to None when env var is not set."""
    cfg = HMCConfig(host="h", user="u", password="p")
    assert cfg.ssh_key_file is None


def test_hmc_config_ssh_key_field_set():
    """ssh_key_file is accepted via constructor (mirrors env var mapping)."""
    cfg = HMCConfig(host="h", user="u", password="p", ssh_key_file="/tmp/key")
    assert cfg.ssh_key_file == "/tmp/key"


def test_hmc_config_ssh_timeout_default():
    """ssh_timeout defaults to 300s and honours an explicit override."""
    assert HMCConfig(host="h", user="u", password="p").ssh_timeout == 300.0
    assert (
        HMCConfig(host="h", user="u", password="p", ssh_timeout=45.0).ssh_timeout
        == 45.0
    )


# ---------------------------------------------------------------------------
# Credential validation parity with the REST path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_hmc_command_missing_config_fails_actionably():
    """Password auth without host/user raises the shared actionable error
    instead of an obscure asyncssh error.

    The suite fixture clears ambient HMC credentials before this test.
    """
    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect") as mock_connect,
        pytest.raises(ValueError, match="Missing HMC configuration"),
    ):
        await run_hmc_command(HMCConfig(), "lssyscfg -r sys")
    mock_connect.assert_not_called()


@pytest.mark.asyncio
async def test_run_hmc_command_key_auth_skips_password_requirement():
    """Key auth does not require a password, so missing password passes the
    credential check and the command reaches asyncssh."""
    conn_mock = _make_ssh_mock("lssyscfg key output\n")

    with patch(
        "hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock
    ) as mock_connect:
        result = await run_hmc_command(
            HMCConfig(
                host="hmc.test", user="hscroot", ssh_key_file="/home/user/.ssh/hmc_key"
            ),
            "lssyscfg -r sys",
        )

    call_kwargs = mock_connect.call_args.kwargs
    assert call_kwargs["client_keys"] == ["/home/user/.ssh/hmc_key"]
    assert call_kwargs["password"] is None
    assert result == "lssyscfg key output\n"


def test_validate_credentials_key_auth_skips_password():
    """require_password=False omits the password from the missing list."""
    HMCConfig(host="h", user="u").validate_credentials(require_password=False)


def test_validate_credentials_password_still_required_by_default(monkeypatch):
    """validate_credentials() raises when password is absent.

    The suite fixture clears ambient HMC credentials before this test.
    """
    monkeypatch.delenv("HMC_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="password"):
        HMCConfig(host="h", user="u").validate_credentials()


@pytest.mark.asyncio
async def test_console_connection_sends_keepalives():
    connect = AsyncMock()
    with patch("hmcpctl.ssh.transport.asyncssh.connect", connect):
        await open_hmc_connection(make_config())
    assert connect.call_args.kwargs["keepalive_interval"] == 15.0
    assert connect.call_args.kwargs["keepalive_count_max"] == 3


@pytest.mark.asyncio
async def test_successful_stderr_is_retained_only_when_requested():
    connection = _make_ssh_mock("restored\n")
    connection.run.return_value.stderr = "keystore warning\n"
    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=connection):
        assert await run_hmc_command(make_config(), "rstprofdata") == "restored\n"
        output = await run_hmc_command(
            make_config(), "rstprofdata", include_stderr=True
        )
    assert output == "restored\n[stderr]\nkeystore warning\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,signal,completed",
    [
        (1, None, True),
        (7, None, True),
        (0, None, False),
        (None, None, False),
        (True, None, False),
        (-1, None, False),
        (1, "TERM", False),
        (None, "TERM", False),
    ],
)
@pytest.mark.parametrize("secret", [None, "private-value"])
async def test_completed_nonzero_provenance(status, signal, completed, secret):
    connection = _make_ssh_mock()
    connection.run.side_effect = asyncssh.ProcessError(
        env={},
        command="cmd",
        subsystem=None,
        exit_status=status,
        exit_signal=signal,
        returncode=status,
        stdout="",
        stderr="uncoded refusal " + (secret or "ordinary detail"),
    )
    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=connection),
        pytest.raises(HMCCLIError) as caught,
    ):
        await run_hmc_command(make_config(), "cmd", secrets=(secret,) if secret else ())
    assert caught.value.remote_nonzero_completed is completed
    if secret:
        assert secret not in str(caught.value)
        assert caught.value.__context__ is None and caught.value.__cause__ is None


def test_other_cli_errors_do_not_establish_remote_completion():
    assert HMCCLIError("exit status 1 HSCL0001").remote_nonzero_completed is False
    assert (
        HMCCLIError("opaque", remote_nonzero_completed=1).remote_nonzero_completed
        is False
    )
