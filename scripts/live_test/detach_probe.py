"""Bounded ST41 diagnostic capture; product failure policy remains unchanged."""

from __future__ import annotations

import re
import shlex
import uuid
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import TYPE_CHECKING, Any

from fastmcp import Client

from hmcpctl.ssh.commands import HMC_NO_RESULTS

from . import lpar_power as power
from .observation import CallFailure, judge_create_result
from .results import resource
from .storage import scsi_adapter_listing

if TYPE_CHECKING:
    from live_test.runner import RunState
_KEYS = ("id", "lpar_uuid", "backing_kind", "backing_name")


class _Stop(Exception):
    """Further writes or cleanup cannot be justified by this capture."""


def mapping_rows(data: object) -> frozenset[tuple[Any, ...]]:
    if not isinstance(data, list):
        raise _Stop("mapping inventory is not a list")
    rows = []
    for entry in data:
        if is_dataclass(entry) and (not isinstance(entry, type)):
            entry = asdict(entry)
        if not isinstance(entry, Mapping) or any(key not in entry for key in _KEYS):
            raise _Stop("mapping inventory has a malformed row")
        row = tuple(entry[key] for key in _KEYS)
        if (
            not isinstance(row[0], str)
            or not row[0]
            or any(
                value is not None and (not isinstance(value, str)) for value in row[1:]
            )
        ):
            raise _Stop("mapping inventory has an invalid identity")
        rows.append(row)
    if len({row[0] for row in rows}) != len(rows):
        raise _Stop("mapping inventory has duplicate IDs")
    return frozenset(rows)


def adapter_rows(data: object) -> frozenset[str]:
    if not isinstance(data, str) or len(data) > 1048576:
        raise _Stop("adapter inventory unreadable or oversized")
    text = data.strip()
    if text == HMC_NO_RESULTS:
        return frozenset()
    lines = text.splitlines()
    if not lines or len(lines) > 4096:
        raise _Stop("adapter inventory missing or oversized")
    slots = set()
    for line in lines:
        fields = line.split(",")
        if (
            len(fields) != 3
            or not fields[0].isascii()
            or (not fields[0].isdigit())
            or (not fields[2].isascii())
            or (not fields[2].isdigit())
            or (not fields[1])
            or any(char.isspace() for char in fields[1])
        ):
            raise _Stop("adapter inventory has malformed three-field row")
        slot = int(fields[0])
        if slot in slots:
            raise _Stop("adapter inventory has duplicate local slot")
        slots.add(slot)
    return frozenset(lines)


def attached_adapters(
    original: tuple[Any, Any, Any],
    mapped: tuple[Any, Any, Any],
    scratch: str,
    vios_name: str,
) -> None:
    added = []
    for baseline, current in zip(original[1:], mapped[1:], strict=True):
        if not baseline <= current or len(current - baseline) != 1:
            raise _Stop("attach removed, changed or added unrelated adapters")
        added.append(next(iter(current - baseline)).split(","))
    (server, client) = added
    if (
        server[1] != scratch
        or client[1] != vios_name
        or int(server[0]) != int(client[2])
        or (int(server[2]) != int(client[0]))
    ):
        raise _Stop("attach did not add the exact scratch/VIOS adapter pair")


class _Probe:
    def __init__(self, client: Client, state: RunState) -> None:
        (self.client, self.state) = (client, state)
        self.run = power.Run(state.config.system_name, uuid.uuid4().hex[:8])
        self.baseline: Any = None
        self.original: Any = None
        self.created = False
        self.volume_attempted = False

    async def inventory(self, label: str) -> tuple[Any, Any, Any]:
        vios = self.run.vios
        assert vios is not None
        (st, data) = await self.state.call(
            self.client,
            "hmc_list_storage_mappings",
            vios_name_or_uuid=vios.uuid,
            system_name_or_uuid=self.run.system,
        )
        self.state.record(
            power.SUBTASK, f"hmc_list_storage_mappings ({label})", st, data
        )
        if st != "PASS":
            raise _Stop("mapping inventory unreadable")
        mappings = mapping_rows(data)
        adapters = []
        selectors = [("lpar_ids", vios.partition_id)]
        if self.run.p_uuid is not None:
            selectors.append(("lpar_names", self.run.p_name))
        for attribute, selector in selectors:
            (st, data) = await self.state.call(
                self.client,
                "hmc_run_command",
                cmd=scsi_adapter_listing(self.run.system, attribute, selector),
            )
            self.state.record(
                power.SUBTASK,
                f"hmc_run_command ({f'{label} adapters {attribute}'})",
                st,
                data,
            )
            if st != "PASS":
                raise _Stop("adapter inventory unreadable")
            parsed = adapter_rows(data)
            adapters.append(parsed)
        if self.run.p_uuid is None:
            adapters.append(frozenset())
        return (mappings, adapters[0], adapters[1])

    async def context(self, label: str) -> dict[str, Any]:
        run = self.run
        assert run.vios is not None and run.p_uuid is not None
        if await power._adopt(self.client, self.state, run, run.p_name) != run.p_uuid:
            raise _Stop("scratch UUID or caller-token ownership changed")
        details = await power._get_lpar(self.client, self.state, run, run.p_uuid)
        client_id = str(resource(details).get("PartitionID") or "")
        if not client_id.isdigit():
            raise _Stop("scratch partition ID unreadable")
        (st, listing) = await self.state.call(
            self.client,
            "hmc_run_command",
            cmd=f"lssyscfg -r lpar -m {shlex.quote(run.system)} -F lpar_id,name,lpar_env,state,rmc_state",
        )
        self.state.record(power.SUBTASK, f"hmc_run_command ({label})", st, listing)
        if st != "PASS" or not isinstance(listing, str):
            raise _Stop("RMC/state inventory unreadable")
        rows = [line.split(",") for line in listing.splitlines() if line.strip()]
        if any(len(row) != 5 or not row[0].isdigit() for row in rows):
            raise _Stop("RMC/state inventory malformed")
        selected = {}
        for role, identifier in (
            ("client", client_id),
            ("vios", str(run.vios.partition_id)),
        ):
            matches = [row for row in rows if row[0] == identifier]
            if len(matches) != 1:
                raise _Stop("RMC/state identity missing or duplicated")
            row = matches[0]
            if role == "client" and row[1] != run.p_name:
                raise _Stop("scratch name/ID association changed")
            if role == "vios" and row[2] != "vioserver":
                raise _Stop("selected VIOS ID no longer identifies a VIOS")
            selected[role] = dict(
                zip(("id", "name", "environment", "state", "rmc"), row, strict=True)
            )
        return selected

    async def settled(self, label: str) -> None:
        if await self.inventory(label) != self.original:
            raise _Stop(
                "mapping or adapter inventory differs from the protected baseline"
            )

    async def cycle(self) -> None:
        number = 3
        expected = "open firmware"
        run = self.run
        assert run.vios is not None and run.p_uuid is not None
        await self.settled(f"cycle {number} before attach")
        before = await self.context(f"cycle {number} before attach RMC/state")
        attach_context = before
        if (
            before["client"]["state"].lower() != "not activated"
            or before["vios"]["rmc"].lower() != "active"
        ):
            raise _Stop("expected client state or active VIOS RMC not established")
        (st, data) = await self.state.call(
            self.client,
            "hmc_map_storage_to_lpar",
            vios_name_or_uuid=run.vios.uuid,
            storage_name=run.volume,
            lpar_name_or_uuid=run.p_uuid,
            storage_kind="VirtualDisk",
            system_name_or_uuid=run.system,
        )
        self.state.record(
            power.SUBTASK,
            f"hmc_map_storage_to_lpar ({f'probe cycle {number}'})",
            st,
            data,
        )
        mapped = await self.inventory(f"cycle {number} after attach")
        candidates = [row for row in mapped[0] if row[3] == run.volume]
        if st != "PASS" or len(candidates) != 1:
            raise _Stop("attach refused or mapping identity not unique")
        row = candidates[0]
        if (
            row[1] is None
            or row[1].lower() != run.p_uuid.lower()
            or row[2] != "VirtualDisk"
        ):
            raise _Stop("mapping does not name the owned scratch client and disk")
        if mapped[0] - {row} != self.original[0]:
            raise _Stop("attach changed unrelated mappings")
        attached = await self.context("cycle 3 before activation RMC/state")
        attached_adapters(self.original, mapped, run.p_name, attached["vios"]["name"])
        if (
            attached["client"]["state"].lower() != "not activated"
            or attached["vios"]["rmc"].lower() != "active"
        ):
            raise _Stop("endpoint context changed before activation")
        before = await self.activate(mapped)
        (st, response) = await self.state.call(
            self.client,
            "hmc_detach_storage_mapping",
            vios_name_or_uuid=run.vios.uuid,
            mapping_id=row[0],
            system_name_or_uuid=run.system,
        )
        self.state.record(
            power.SUBTASK,
            f"hmc_detach_storage_mapping ({f'probe cycle {number}'})",
            st,
            response,
        )
        after = await self.inventory(f"cycle {number} after detach")
        context = await self.context(f"cycle {number} after detach RMC/state")
        failure = response if isinstance(response, CallFailure) else None
        self.state.record(
            power.SUBTASK,
            f"detach probe comparison {number}",
            st,
            {
                "before_attach_context": attach_context,
                "before_activation_context": attached,
                "before_context": before,
                "after_context": context,
                "before_mappings": sorted(mapped[0], key=repr),
                "after_mappings": sorted(after[0], key=repr),
                "before_adapters": [sorted(rows) for rows in mapped[1:]],
                "after_adapters": [sorted(rows) for rows in after[1:]],
                "http_status": failure.http_status if failure else None,
                "response": failure.message if failure else response,
                "codes": re.findall("\\b(?:REST|HSCL)[0-9]+\\b", failure.message)
                if failure
                else [],
                "mapping_absent": row[0] not in {item[0] for item in after[0]},
            },
        )
        if after != self.original:
            raise _Stop("detach did not restore exact mapping/adapter inventory")
        if context["client"]["state"].lower() != expected:
            raise _Stop("client state changed during detach")

    async def activate(self, mapped: tuple[Any, Any, Any]) -> dict[str, Any]:
        run = self.run
        assert run.p_uuid is not None
        st, data = await power._power_on(self.client, self.state, run, boot_mode="of")
        self.state.record(power.SUBTASK, "hmc_power_on_lpar (detach probe)", st, data)
        reached = await power._wait_for_state(
            self.client, self.state, run, run.p_uuid, frozenset({"open firmware"})
        )
        after = await self.inventory("cycle 3 after activation")
        context = await self.context("cycle 3 after activation RMC/state")
        if st != "PASS" or reached != "open firmware":
            raise _Stop("scratch Open Firmware activation not established")
        if after != mapped:
            raise _Stop(
                "activation changed the exact attached mapping/adapter snapshot"
            )
        attached_adapters(self.original, after, run.p_name, context["vios"]["name"])
        if (
            context["client"]["state"].lower() != "open firmware"
            or context["vios"]["rmc"].lower() != "active"
        ):
            raise _Stop("endpoint context changed before detach")
        return context

    async def execute(self) -> None:
        run = self.run
        self.created = True
        (st, response) = await power._create(
            self.client, self.state, run, run.p_name, power.RESOURCES
        )
        (status, note) = judge_create_result(st, response)
        self.state.record(
            power.SUBTASK, "hmc_create_lpar (detach probe)", status, response, note
        )
        run.p_uuid = await power._adopt(self.client, self.state, run, run.p_name)
        run.a_uuid = run.p_uuid
        if status != "PASS" or run.p_uuid is None:
            raise _Stop("scratch create not confirmed with owned identity")
        await self.context("scratch identity before volume create")
        self.volume_attempted = run.volume_attempted = True
        assert run.vios is not None
        (st, data) = await self.state.call(
            self.client,
            "hmc_create_virtual_disk",
            vios_name_or_uuid=run.vios.uuid,
            vg_uuid=run.vios.group_uuid,
            disk_name=run.volume,
            capacity_mib=power.VOLUME_MIB,
            system_name_or_uuid=run.system,
        )
        self.state.record(
            power.SUBTASK, f"hmc_create_virtual_disk ({'detach probe'})", st, data
        )
        volumes = await power._volumes(self.client, self.state, run)
        if st != "PASS" or volumes != self.baseline.volumes | {run.volume}:
            raise _Stop(
                "run volume creation or preserved volume baseline not confirmed"
            )
        await self.cycle()

    async def cleanup(self) -> None:
        run = self.run
        assert run.vios is not None and run.p_uuid is not None
        await self.settled("before probe cleanup")
        before = await self.context("before probe cleanup ownership")
        if before["client"]["state"].lower() != "open firmware":
            raise _Stop("scratch state changed before cleanup")
        if await power._volumes(
            self.client, self.state, run
        ) != self.baseline.volumes | {run.volume}:
            raise _Stop("volume inventory changed before cleanup")
        st, data = await self.state.call(
            self.client,
            "hmc_delete_virtual_disk",
            vios_name_or_uuid=run.vios.uuid,
            vg_uuid=run.vios.group_uuid,
            disk_name=run.volume,
            system_name_or_uuid=run.system,
        )
        self.state.record(
            power.SUBTASK, "hmc_delete_virtual_disk (detach probe cleanup)", st, data
        )
        remaining = await power._volumes(self.client, self.state, run)
        if st != "PASS" or remaining != self.baseline.volumes:
            raise _Stop(
                "volume deletion not accepted and confirmed; keeping scratch partition"
            )
        await self.settled("before cleanup power off")
        before = await self.context("before cleanup power off ownership")
        if before["client"]["state"].lower() != "open firmware":
            raise _Stop("scratch state changed before cleanup power off")
        st, data = await power._power_off(
            self.client, self.state, run, run.p_uuid, immediate=True
        )
        self.state.record(
            power.SUBTASK, "hmc_power_off_lpar (detach probe cleanup)", st, data
        )
        reached = await power._wait_for_state(
            self.client, self.state, run, run.p_uuid, frozenset({"not activated"})
        )
        if st != "PASS" or reached != "not activated":
            raise _Stop("scratch shutdown not accepted and confirmed")
        await self.settled("before cleanup partition delete")
        before = await self.context("before cleanup partition delete ownership")
        if before["client"]["state"].lower() != "not activated":
            raise _Stop("scratch state changed before cleanup partition delete")
        st, data = await self.state.call(
            self.client,
            "hmc_delete_lpar",
            system_name_or_uuid=run.system,
            lpar_name_or_uuid=run.p_uuid,
        )
        self.state.record(
            power.SUBTASK, "hmc_delete_lpar (detach probe cleanup)", st, data
        )
        gone = await power._gone(self.client, self.state, run, run.p_name)
        if st != "PASS" or not gone:
            raise _Stop("scratch delete not accepted and confirmed")

    def retained(self, reason: str) -> None:
        self.state.record(
            power.SUBTASK,
            "detach probe stop",
            "FAIL",
            {
                "reason": reason,
                "scratch": self.run.p_name,
                "volume": self.run.volume,
                "created": self.created,
                "volume_attempted": self.volume_attempted,
            },
            "MANUAL RECOVERY REQUIRED when assets remain; no ambiguous write was retried",
        )


async def exercise_detach_probe(client: Client, state: RunState) -> None:
    probe = _Probe(client, state)
    ready = await power._preconditions(client, state, probe.run)
    if isinstance(ready, str):
        state.skip(power.SUBTASK, "detach probe", ready)
        return
    (probe.baseline, refusal) = ready
    if refusal or probe.run.vios is None:
        state.skip(power.SUBTASK, "detach probe", refusal or "no VIOS")
        return
    try:
        probe.original = await probe.inventory("probe baseline")
        canonical = frozenset(
            tuple(str(value or "") for value in row) for row in probe.original[0]
        )
        if (
            canonical != probe.baseline.mappings
            or probe.original[1] != probe.baseline.adapters
        ):
            raise _Stop("strict inventory disagrees with the captured system baseline")
        if probe.original[2] or probe.run.p_name in power._names(
            probe.baseline.partitions
        ):
            raise _Stop("scratch name or adapters already exist")
        if probe.run.volume in probe.baseline.volumes:
            raise _Stop("run volume name already exists")
        await probe.execute()
        await probe.cleanup()
    except _Stop as exc:
        probe.retained(str(exc))
    except BaseException:
        probe.retained("interrupted or unexpected failure; assets retained")
        raise
    finally:
        final = await power._read_baseline(client, state, probe.run)
        state.record(
            power.SUBTASK,
            "detach probe system baseline compare",
            "PASS" if final == probe.baseline else "FAIL",
            {
                "before": power._baseline_data(probe.baseline),
                "after": power._baseline_data(final),
            },
        )
