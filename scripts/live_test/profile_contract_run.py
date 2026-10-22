"""Execution and outside recovery for the finite saved-profile experiment."""

from __future__ import annotations

import asyncio
import copy
import json
import math
import time
from pathlib import Path

from hmcpctl.operations.lpar.core import delete_lpar
from hmcpctl.operations.lpar.ownership import (
    authorize_lpar_ownership_description,
    parse_lpar_ownership_caller_token,
)
from hmcpctl.ssh.lpar import create_lpar_via_cli, stamp_lpar_ownership

from . import partition_profile_contract as contract
from . import system_profile_activation_source as fixture_safety


class ContractMismatch(ValueError):
    """Known settled effect with a captured but nonconforming postcondition."""


def stable(snapshot, fixture_uuid=None):
    return {
        "partitions": [e for e in snapshot["partitions"] if e["UUID"] != fixture_uuid],
        "namespace": snapshot["namespace"],
        "system_auto_off": snapshot["system_auto_off"],
    }


def observations_equal(before, after):
    left, right = copy.deepcopy(before), copy.deepcopy(after)
    if len(left["partitions"]) != len(right["partitions"]):
        return False
    for a, b in zip(left["partitions"], right["partitions"], strict=True):
        x, y = a["Resource"], b["Resource"]
        if "Uptime" not in x or "Uptime" not in y:
            continue
        for resource in (x, y):
            counter = resource["Uptime"]
            if (
                not isinstance(counter, dict)
                or set(counter) != {"@attrs", "text"}
                or counter["@attrs"] != {"group": "Advanced", "ksv": "V1_15_0"}
            ):
                return False
            text = counter["text"]
            if (
                not isinstance(text, str)
                or not text.isascii()
                or not text.isdecimal()
                or not 1 <= len(text) <= 19
                or int(text) > 2**63 - 1
            ):
                return False
        if int(y["Uptime"]["text"]) < int(x["Uptime"]["text"]):
            return False
        del x["Uptime"], y["Uptime"]
    return left == right


def capacity(snapshot):
    memory = snapshot["capacity"]["AssociatedSystemMemoryConfiguration"]
    processor = snapshot["capacity"]["AssociatedSystemProcessorConfiguration"]
    block = int(memory["CurrentLogicalMemoryBlockSize"])
    available = float(processor["CurrentAvailableSystemProcessorUnits"])
    minimum = float(processor["MinimumProcessorUnitsPerVirtualProcessor"])
    if (
        not math.isfinite(available)
        or not math.isfinite(minimum)
        or not 0 < minimum <= 0.05
        or available < 0.05
        or int(memory["CurrentAvailableSystemMemory"]) < 1024
        or block <= 0
        or any(size % block for size in (512, 768, 1024))
    ):
        raise ValueError("fixed fixture resources are unavailable; no fallback")


def check_original(journal, snapshot, fixture_uuid=None):
    if snapshot["namespace"] or snapshot["system_auto_off"] != "false":
        raise ValueError("system-profile entry or unsafe/unknown stay-on policy")
    if not observations_equal(journal.data["baseline"], stable(snapshot, fixture_uuid)):
        raise ValueError("original partition/profile/policy baseline changed")


def fixture(api, snapshot, names, *, owned=True, uuid=None):
    packet = api.packet
    matches = [
        e
        for e in snapshot["partitions"]
        if e["Resource"].get("PartitionName") == packet["fixture"]
    ]
    if len(matches) != 1:
        raise ValueError("one exact fixture is required")
    entry = matches[0]
    resource = entry["Resource"]
    if (
        entry["kind"] != "LogicalPartition"
        or resource.get("PartitionType") != "AIX/Linux"
        or resource.get("PartitionState") != "not activated"
        or uuid is not None
        and entry["UUID"] != uuid
    ):
        raise ValueError("bound inactive AIX/Linux fixture required")
    fixture_safety._runtime_limits(resource)
    children = entry["profiles"]
    actual = [contract.domain._text(p["Resource"].get("ProfileName")) for p in children]
    if (
        len(children) != len(names)
        or set(actual) != set(names)
        or len(set(actual)) != len(actual)
    ):
        raise ValueError("exact profile membership required")
    for child in children:
        name = contract.domain._text(child["Resource"]["ProfileName"])
        view = copy.deepcopy(child)
        io = view["Resource"].get("IOConfigurationInstance", {})
        if (
            "VirtualOpticonnectPool" in io
            and io.pop("VirtualOpticonnectPool") != "false"
        ):
            raise ValueError("nonempty optional IO configuration")
        fixture_safety._safe_profile(view, names[name])
        if contract.domain._text(io.get("MaximumVirtualIOSlots")) != "2":
            raise ValueError("exact two-slot empty IO envelope required")
        contract.domain._profile_parent(
            child, entry["UUID"], packet["system_uuid"], api, "LogicalPartition"
        )
    membership = resource.get("PartitionProfiles")
    if not isinstance(membership, dict) or set(membership) != {"link"}:
        raise ValueError("explicit contained membership required")
    links = membership["link"]
    links = [links] if isinstance(links, dict) else links
    if (
        not isinstance(links, list)
        or len(links) != len(children)
        or any(
            not isinstance(link, dict)
            or set(link) != {"href", "rel"}
            or link["rel"] != "related"
            for link in links
        )
        or sorted(link["href"] for link in links)
        != sorted(child["link"] for child in children)
    ):
        raise ValueError("partition links contradict child profile feed")
    if owned:
        description = contract.domain._text(resource.get("Description"))
        owner = authorize_lpar_ownership_description(
            api,
            packet["mtms"],
            packet["fixture"],
            description,
            operation="lpar-mutation",
        )
        if (
            owner != packet["agent_id"]
            or parse_lpar_ownership_caller_token(description) != packet["caller"]
        ):
            raise ValueError("exact normal owner/caller required")
    return entry


def same_read(before, after):
    if before["etag"] != after["etag"]:
        return False
    roots = [contract.fromstring(read["xml"]) for read in (before, after)]
    for root in roots:
        if root.tag != contract.ATOM + "entry":
            return False
        published = root.findall(contract.ATOM + "published")
        if len(published) != 1 or list(published[0]):
            return False
        published[0].text = ""
    return contract.canonical(roots[0]) == contract.canonical(roots[1])


async def observe(api, journal, names, *, owned=True):
    snapshot = await api.snapshot()
    entry = fixture(
        api, snapshot, names, owned=owned, uuid=journal.data.get("fixture_uuid")
    )
    check_original(journal, snapshot, entry["UUID"])
    journal.data["fixture_uuid"] = entry["UUID"]
    journal.data["latest_fixture"] = entry
    journal.data["profile_memory"] = dict(names)
    journal.write()
    return entry


async def preflight(hmc_config, config):
    packet = contract.configured(config)
    contract.verify_revision(packet)
    directory = Path(packet["window_dir"])
    marker = directory / "preflight.json"
    with marker.open("x") as stream:
        stream.write("{}")
    marker.chmod(0o600)
    budget = contract.Budget(20)
    started = time.time()
    journal = contract.Journal(marker)
    journal.data.update(
        started=started,
        packet_sha256=config.partition_profile_contract_sha256,
        tested_commit=packet["tested_commit"],
        tested_tree=packet["tested_tree"],
        admitted=False,
    )
    api = contract.Native(hmc_config, packet, budget, directory)
    try:
        async with asyncio.timeout(60):
            version = await api.command("lshmc -V")
            if not all(
                text in version
                for text in (
                    "Version: 10",
                    "Release: 3",
                    "Service Pack: 1060",
                    packet["expected_build"],
                )
            ):
                raise ValueError("approved HMC release differs")
            journal.data["version"] = version
            await api.logon()
            snapshot = await api.snapshot()
            if (
                snapshot["namespace"]
                or snapshot["system_auto_off"] != "false"
                or any(
                    e["Resource"].get("PartitionName", "").startswith(contract.PREFIX)
                    for e in snapshot["partitions"]
                )
            ):
                raise ValueError(
                    "existing experiment residue/system-profile/policy refuses admission"
                )
            capacity(snapshot)
            journal.data.update(baseline=stable(snapshot), admitted=True)
    finally:
        try:
            await api.logoff()
        finally:
            await api._http.aclose()
            journal.data["transports_upper_bound"] = budget.used
            journal.write()
    return journal.data


async def create(api, journal):
    packet = api.packet
    before = await api.snapshot()
    check_original(journal, before)
    capacity(before)
    if any(
        e["Resource"].get("PartitionName", "").startswith(contract.PREFIX)
        for e in before["partitions"]
    ):
        raise ValueError("fixture namespace collision")
    journal.intent("create")
    api.budget.take()  # Native provisioning path is exactly one SSH command, no fallback/retry.
    await create_lpar_via_cli(
        api.config,
        packet["mtms"],
        packet["fixture"],
        resources=fixture_safety.resources(),
        max_virtual_slots=2,
        profile_name=packet["base"],
    )
    names = {packet["base"]: 512}
    await observe(api, journal, names, owned=False)
    journal.settle("create")
    # Stamp only the exact, just-created inactive/name+UUID fixture after a fresh comparison.
    journal.intent("stamp")
    api.budget.take()
    result = await stamp_lpar_ownership(
        api.config,
        packet["mtms"],
        packet["fixture"],
        agent_id=packet["agent_id"],
        caller_token=packet["caller"],
    )
    if result is None:
        raise ValueError("owner stamp outcome unconfirmed")
    await observe(api, journal, names)
    journal.settle("stamp")
    return names


def child_uuid(entry, name):
    selected = [
        p["UUID"]
        for p in entry["profiles"]
        if contract.domain._text(p["Resource"]["ProfileName"]) == name
    ]
    if len(selected) != 1:
        raise ValueError("exact profile selector required")
    return selected[0]


def reserve_effect(api, journal):
    snapshot_cost = len(journal.data["baseline"]["partitions"]) + 6
    if api.budget.limit - api.budget.used < 3 * snapshot_cost + 12:
        raise ContractMismatch("remaining arm budget reserved for known-state cleanup")


async def patch(api, journal, names, name, value, stage):
    entry = journal.data["latest_fixture"]
    uuid = child_uuid(entry, name)
    before = await api.read_profile(entry["UUID"], uuid)
    body = contract.memory_payload(before["xml"], names[name], value)
    await observe(api, journal, names)
    latest = await api.read_profile(entry["UUID"], uuid)
    if not same_read(before, latest):
        raise ValueError("ETag/full-tree stale guard refused before patch")
    reserve_effect(api, journal)
    journal.intent(stage)
    response = await api._request(
        "POST",
        api.profile_path(entry["UUID"], uuid),
        content=body,
        headers={
            "Accept": "*/*",
            "Content-Type": contract.MEDIA,
            "If-Match": before["etag"],
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise ValueError("conditional patch synchronous200 required")
    after = await api.read_profile(entry["UUID"], uuid)
    root = contract.profile(after["xml"])
    if (
        contract.scalar(root.find(contract.UOM + "ProfileMemory"), "DesiredMemory").text
        != str(value)
        or contract.scalar(root, "SettingID").text
        != contract.scalar(contract.profile(before["xml"]), "SettingID").text
    ):
        raise ValueError("requested memory or unchanged SettingID unverified")
    names[name] = value
    await observe(api, journal, names)
    result = contract.compare_profiles(body, after["xml"])
    journal.data.setdefault("patches", {})[stage] = result
    journal.settle(stage)
    if stage == "normalize":
        allowed = contract.normalization_permitted(body, after["xml"])
    else:
        allowed = result["configuration_equal"]
    if not allowed:
        raise ContractMismatch(
            "known applied patch has unexpected represented differences"
        )
    return after


async def clone(api, journal, names, source, name, stage):
    entry = journal.data["latest_fixture"]
    source_uuid = child_uuid(entry, source)
    before = await api.read_profile(entry["UUID"], source_uuid)
    body = contract.clone_payload(before["xml"], name)
    await observe(api, journal, names)
    if not same_read(before, await api.read_profile(entry["UUID"], source_uuid)):
        raise ValueError("source ETag/full tree changed before clone")
    reserve_effect(api, journal)
    journal.intent(stage)
    response = await api._request(
        "PUT",
        api.profile_path(entry["UUID"]),
        content=body,
        headers={"Accept": "*/*", "Content-Type": contract.MEDIA},
        timeout=30,
    )
    if response.status_code not in (200, 201):
        raise ValueError("clone synchronous200/201 required")
    names[name] = names[source]
    entry = await observe(api, journal, names)
    after = await api.read_profile(entry["UUID"], child_uuid(entry, name))
    unchanged = await api.read_profile(entry["UUID"], source_uuid)
    if not same_read(before, unchanged):
        raise ValueError("source profile changed during copy")
    result = contract.compare_profiles(before["xml"], after["xml"])
    journal.data.setdefault("copies", {})[stage] = result
    journal.settle(stage)
    if not result["configuration_equal"]:
        raise ContractMismatch("copy has represented configuration differences")


async def cleanup(api, journal, names):
    if not journal.cleanup_permitted:
        raise ValueError("uncertain effects forbid cleanup")
    if (
        api.budget.limit - api.budget.used
        < 2 * (len(journal.data["baseline"]["partitions"]) + 6) + 8
    ):
        raise ValueError("insufficient reserved arm budget for guarded cleanup")
    previous = journal.data["latest_fixture"]
    entry = await observe(api, journal, names)
    if entry != previous:
        raise ValueError("fixture full state changed before cleanup")
    journal.intent("delete")
    api.budget.take()  # Normal delete authorization performs one SSH ownership-description read.
    await delete_lpar(
        api,
        entry["UUID"],
        system_name_or_uuid=api.packet["system_uuid"],
        ownership_override=False,
    )
    journal.settle("delete")
    snapshot = await api.snapshot()
    check_original(journal, snapshot)
    if any(
        e["UUID"] == entry["UUID"]
        or e["Resource"].get("PartitionName") == api.packet["fixture"]
        for e in snapshot["partitions"]
    ):
        raise ValueError("fixture absence unverified after deletion")
    journal.data["deleted"] = True
    journal.write()


async def execute(hmc_config, config):
    packet = contract.configured(config)
    contract.verify_revision(packet)
    directory = Path(packet["window_dir"])
    admission = json.loads(
        contract.private_file(directory / "preflight.json").read_text()
    )
    if (
        not admission.get("admitted")
        or admission.get("packet_sha256") != config.partition_profile_contract_sha256
        or not 0 <= time.time() - admission["started"] < 60
    ):
        raise ValueError("fresh matching native preflight required")
    path = directory / "journal.json"
    with path.open("x") as stream:
        stream.write("{}")
    path.chmod(0o600)
    journal = contract.Journal(path)
    journal.data.update(
        baseline=admission["baseline"],
        started=admission["started"],
        tested_commit=packet["tested_commit"],
        tested_tree=packet["tested_tree"],
        packet_sha256=config.partition_profile_contract_sha256,
        deleted=False,
        status="STARTED",
    )
    journal.write()
    budget = contract.Budget(160 - admission["transports_upper_bound"])
    api = contract.Native(hmc_config, packet, budget, directory / "arm")
    api.directory.mkdir(mode=0o700)
    try:
        async with asyncio.timeout(420 - (time.time() - admission["started"])):
            await api.logon()
            names = await create(api, journal)
            try:
                await patch(api, journal, names, packet["base"], 768, "normalize")
                await clone(
                    api, journal, names, packet["base"], packet["copies"][0], "copy-a"
                )
                await clone(
                    api,
                    journal,
                    names,
                    packet["copies"][0],
                    packet["copies"][1],
                    "copy-b",
                )
                await patch(api, journal, names, packet["copies"][1], 1024, "patch-b")
                journal.data["status"] = "CONTRACT_OBSERVED"
            except ContractMismatch:
                journal.data["status"] = "CONTRACT_DIFFERENCE"
            await cleanup(api, journal, names)
    except BaseException as error:
        journal.data.update(status="STOP_RETAIN", error_type=type(error).__name__)
        raise
    finally:
        try:
            await api.logoff()
        finally:
            await api._http.aclose()
            journal.data["arm_transports_upper_bound"] = budget.used
            journal.data["preflight_transports_upper_bound"] = admission[
                "transports_upper_bound"
            ]
            journal.write()
    return journal.data


async def recover(hmc_config, config):
    packet = contract.configured(config)
    contract.verify_revision(packet)
    directory = Path(packet["window_dir"])
    path = contract.private_file(directory / "journal.json")
    journal = json.loads(path.read_text())
    if (
        journal.get("packet_sha256") != config.partition_profile_contract_sha256
        or journal.get("tested_commit") != packet["tested_commit"]
        or journal.get("tested_tree") != packet["tested_tree"]
    ):
        raise ValueError("recovery packet/revision mismatch")
    receipt_path = directory / "recovery.json"
    with receipt_path.open("x") as stream:
        stream.write("{}")
    receipt_path.chmod(0o600)
    budget = contract.Budget(32)
    api = contract.Native(hmc_config, packet, budget, directory / "recovery")
    api.directory.mkdir(mode=0o700)
    result = {"status": "UNKNOWN", "tested_commit": packet["tested_commit"]}
    try:
        remaining = 600 - (time.time() - journal["started"])
        if remaining <= 0:
            raise ValueError(
                "approved window expired before recovery; new authority required"
            )
        async with asyncio.timeout(min(120, remaining)):
            await api.logon()
            snapshot = await api.snapshot()
            if not observations_equal(journal["baseline"], stable(snapshot)):
                raise ValueError("fresh independent original baseline differs")
            if any(
                e["UUID"] == journal.get("fixture_uuid")
                or e["Resource"].get("PartitionName", "").startswith(contract.PREFIX)
                for e in snapshot["partitions"]
            ):
                raise ValueError("fixture or reserved-name residue remains")
            if (
                journal.get("pending")
                or journal.get("uncertain")
                or "create" in journal["attempted"]
                and not journal.get("deleted")
            ):
                raise ValueError("absence cannot settle an uncertain mutation")
            result["status"] = "CLEAN"
    finally:
        try:
            await api.logoff()
        finally:
            await api._http.aclose()
            result["transports_upper_bound"] = budget.used
            contract.save(receipt_path, result)
    return result
