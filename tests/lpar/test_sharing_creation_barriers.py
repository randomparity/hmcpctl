"""Update-only sharing weight cannot enter any existing creation route."""

from typing import cast

import pytest

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.lpar import (
    LparResources,
    LparSettings,
    build_lpar_document,
    build_vios_document,
)
from hmcpctl.documents.lpar_memory_layout import LparMemoryLayout
from hmcpctl.documents.lpar_security_policy import LparSecurityPolicy
from hmcpctl.operations.lpar.core import LparCreation, create_and_stamp_lpar
from hmcpctl.operations.lpar.provision import (
    ProvisionAdapters,
    ProvisionRequest,
    ProvisionStorage,
    _preflight_provision_request,
    provision_lpar,
)
from hmcpctl.operations.vios.core import create_vios
from hmcpctl.ssh.lpar import _lpar_resource_pairs, complete_create_resources


@pytest.mark.parametrize("weight", [0, 64])
@pytest.mark.parametrize("quantity", [None, 0.5])
@pytest.mark.parametrize("route", ["defaults", "native", "rest", "vios-rest"])
def test_sharing_create_pure(weight, quantity, route):
    resources = LparResources(uncapped_weight=weight, desired_procs=quantity)
    with pytest.raises(ValueError, match="uncapped_weight.*only.*update"):
        if route == "defaults":
            complete_create_resources(resources)
        elif route == "native":
            _lpar_resource_pairs(resources, None)
        elif route == "rest":
            build_lpar_document(name="new", resources=resources)
        else:
            build_vios_document(name="new", resources=resources)


def test_sharing_create_pure_omitted_weight_retains_defaults():
    assert complete_create_resources(LparResources()) is None
    assert _lpar_resource_pairs(LparResources(), None) == [("all_resources", 1)]
    completed = complete_create_resources(LparResources(desired_procs=0.5))
    assert completed is not None
    assert completed.uncapped_weight is None
    assert completed.desired_memory == 4096
    assert completed.desired_procs == 0.5


class _NoCreationReads:
    def __getattr__(self, name):
        raise AssertionError(f"Creation reached client boundary {name}")


@pytest.mark.asyncio
@pytest.mark.parametrize("weight", [0, 64])
@pytest.mark.parametrize(
    "route",
    [
        "rest-fallback",
        "all-resources",
        "native",
        "lpar-layout",
        "vios-layout",
        "vios",
        "vios-native",
        "preflight",
        "provision",
        "dry-run",
    ],
)
async def test_sharing_create_barrier(weight, route):
    hmc = cast(HMCClient, _NoCreationReads())
    resources = LparResources(
        uncapped_weight=weight,
        desired_procs=None if route == "all-resources" else 0.5,
    )
    with pytest.raises(ValueError, match="uncapped_weight.*only.*update"):
        if route.startswith("vios"):
            await create_vios(
                hmc,
                "system",
                "new",
                resources,
                security_policy=LparSecurityPolicy(secure_boot=1)
                if route == "vios-native"
                else None,
                memory_layout=LparMemoryLayout(hpt_ratio="1:128")
                if route == "vios-layout"
                else None,
            )
        elif route in {"preflight", "provision", "dry-run"}:
            request = ProvisionRequest(
                "new",
                ProvisionAdapters(100),
                ProvisionStorage("vios", "disk"),
                resources,
                dry_run=route == "dry-run",
            )
            if route == "preflight":
                await _preflight_provision_request(hmc, "system", request)
            else:
                await provision_lpar(hmc, "system", request)
        else:
            creation = LparCreation(
                "new",
                "AIX/Linux",
                resources,
                settings=LparSettings(allow_perf_collection=True)
                if route == "native"
                else LparSettings(),
                memory_layout=LparMemoryLayout(hpt_ratio="1:128")
                if route == "lpar-layout"
                else LparMemoryLayout(),
            )
            await create_and_stamp_lpar(hmc, "system", creation)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["lpar", "vios", "preflight"])
async def test_sharing_create_barrier_precedes_other_validation(owner):
    resources = LparResources(uncapped_weight=0)
    hmc = cast(HMCClient, _NoCreationReads())
    with pytest.raises(ValueError, match="uncapped_weight.*only.*update"):
        if owner == "lpar":
            await create_and_stamp_lpar(
                hmc, "system", LparCreation("new", "bad-type", resources)
            )
        elif owner == "vios":
            await create_vios(
                hmc,
                "system",
                "new",
                resources,
                security_policy=cast(LparSecurityPolicy, object()),
            )
        else:
            request = ProvisionRequest(
                "new",
                ProvisionAdapters(0),
                ProvisionStorage("vios", "disk"),
                resources,
                partition_type="bad-type",
            )
            await _preflight_provision_request(hmc, "system", request)
