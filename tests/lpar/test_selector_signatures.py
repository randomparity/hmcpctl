"""LPAR operations share target-first, keyword-only system selection."""

import inspect
from unittest.mock import AsyncMock

import pytest

from hmcpctl.operations.lpar.core import (
    delete_lpar,
    power_off_lpar,
    power_on_lpar,
    rename_lpar,
    submit_power_on_lpar,
)
from hmcpctl.operations.lpar.migration import (
    abort_lpar_migration,
    migrate_lpar,
    migrate_lpar_with_affinity_preflight,
    recover_lpar_migration,
    validate_lpar_migration,
)


@pytest.mark.parametrize(
    "operation",
    [
        power_on_lpar,
        power_off_lpar,
        submit_power_on_lpar,
        validate_lpar_migration,
        migrate_lpar,
        migrate_lpar_with_affinity_preflight,
        abort_lpar_migration,
        recover_lpar_migration,
    ],
)
def test_target_precedes_keyword_only_optional_system(operation):
    signature = inspect.signature(operation)
    parameters = list(signature.parameters.values())
    assert [p.name for p in parameters[:2]] == ["hmc", "lpar_name_or_uuid"]
    system = signature.parameters["system_name_or_uuid"]
    assert system.kind is inspect.Parameter.KEYWORD_ONLY
    assert system.default is None


@pytest.mark.parametrize("operation", [delete_lpar, rename_lpar])
def test_target_precedes_keyword_only_required_system(operation):
    signature = inspect.signature(operation)
    parameters = list(signature.parameters.values())
    assert [p.name for p in parameters[:2]] == ["hmc", "lpar_name_or_uuid"]
    if operation is rename_lpar:
        assert parameters[2].name == "new_name"
        assert parameters[2].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    system = signature.parameters["system_name_or_uuid"]
    assert system.kind is inspect.Parameter.KEYWORD_ONLY
    assert system.default is inspect.Parameter.empty


@pytest.mark.parametrize("operation", [delete_lpar, rename_lpar])
@pytest.mark.asyncio
async def test_missing_required_system_fails_before_client_access(operation):
    hmc = AsyncMock()
    args = ("partition", "renamed") if operation is rename_lpar else ("partition",)
    with pytest.raises(TypeError, match="system_name_or_uuid"):
        await operation(hmc, *args)
    assert hmc.mock_calls == []
