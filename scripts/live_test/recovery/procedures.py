"""Delegate existing standalone recovery protocols."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
from importlib import import_module
from pathlib import Path
from typing import Any

from hmcpctl.config import HMCConfig
from live_test import runner


def _paging_recovery(path: Path) -> int:
    from live_test.arms import paging_switchover

    def unique_members(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Paging recovery JSON has duplicate members")
            result[key] = value
        return result

    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Paging recovery requires a regular results file")
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Paging recovery results exceed 1 MiB")
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_members)
        if not runner._bootstrap_config():
            raise ValueError("Paging recovery credentials unavailable")
        result = asyncio.run(paging_switchover.recover_document(HMCConfig(), document))
    except Exception as error:  # noqa: BLE001 - uncertain evidence never confirms recovery
        print(f"paging recovery unconfirmed: {type(error).__name__}", file=sys.stderr)
        return 2
    print(
        f"PAGING {result['status']}: read-only original-baseline comparison; historical forward result unchanged"
    )
    return {"clean": 0, "drift": 1, "unverified": 2}[result["status"]]


def _statistics_recovery(path: Path) -> int:
    from live_test.arms import memory_statistics

    def unique_members(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Statistics recovery JSON has duplicate members")
            result[key] = value
        return result

    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Statistics recovery requires a regular results file")
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Statistics recovery raw results exceed 1 MiB")
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_members)
        if (
            not isinstance(document, dict)
            or document.get("run", {}).get("group") != memory_statistics.GROUP
        ):
            raise ValueError("Statistics recovery requires its named run")
        if not runner._bootstrap_config():
            raise ValueError("Statistics recovery credentials unavailable")
        asyncio.run(memory_statistics.recover_document(HMCConfig(), document))
    except Exception as error:  # noqa: BLE001 — malformed/uncertain evidence cannot confirm recovery
        print(
            f"statistics recovery unconfirmed: {type(error).__name__}", file=sys.stderr
        )
        return 2
    print(
        "OBSERVED: finite reset-attempt record and current environment confirmed; reset verification unavailable"
    )
    return 0


def delegate_procedure_recovery(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> int | None:
    if args.ethernet_update_procedure:
        from live_test.arms import ethernet_update

        if not args.output or args.packet_sha256:
            parser.error(
                "ST69 recovery requires original results and a fresh private output"
            )
        return ethernet_update.main(
            ["recover", "--results", str(args.results), "--output", str(args.output)]
        )
    if args.paging_switchover:
        if args.output or args.packet_sha256:
            parser.error("ST66 recovery uses retained results only")
        return _paging_recovery(args.results)
    if args.memory_statistics:
        if args.output or args.packet_sha256:
            parser.error("ST67 recovery uses retained results only")
        return _statistics_recovery(args.results)
    for selected, module_name, refusal in (
        (
            args.boot_start_procedure,
            "lpar_boot_start",
            "ST68 recovery derives its binding from original private results only",
        ),
        (
            args.serial_procedure,
            "lpar_serial_adapter",
            "ST63 recovery uses retained results only",
        ),
    ):
        if selected:
            module = import_module(f"live_test.arms.{module_name}")
            if args.output or args.packet_sha256:
                parser.error(refusal)
            return module.main(["recover", "--results", str(args.results)])
    return None


def delegate_packet_recovery(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> int | None:
    for packet, module_name, refusal in (
        (
            args.vnic_creation_policy_packet,
            "vnic_creation_policy",
            "vNIC policy recovery requires packet digest and private output",
        ),
        (
            args.vios_ame_source_packet,
            "vios_ame_source",
            "VIOS AME source recovery requires packet digest and private output",
        ),
        (
            args.vios_boot_source_packet,
            "vios_boot_source",
            "VIOS boot source recovery requires packet digest and private output",
        ),
        (
            args.activation_source_packet,
            "system_profile_activation_source",
            "activation source recovery requires packet digest and private output",
        ),
    ):
        if packet:
            module = import_module(f"live_test.arms.{module_name}")
            if not args.packet_sha256 or not args.output:
                parser.error(refusal)
            return module.main(
                [
                    "recover",
                    "--packet",
                    str(packet),
                    "--packet-sha256",
                    args.packet_sha256,
                    "--results",
                    str(args.results),
                    "--output",
                    str(args.output),
                ]
            )
    if args.output:
        parser.error("--output requires --activation-source-packet")
    if args.source_packet:
        from live_test.arms import system_profile_source

        if not args.packet_sha256:
            parser.error("source recovery requires --packet-sha256")
        return system_profile_source.main(
            [
                "recover",
                "--packet",
                str(args.source_packet),
                "--packet-sha256",
                args.packet_sha256,
                "--results",
                str(args.results),
            ]
        )
    if args.packet_sha256:
        parser.error("--packet-sha256 requires --source-packet")
    return None


def recover_document_group(document: dict[str, Any]) -> int | None:
    run = document.get("run")
    group = run.get("group") if isinstance(run, dict) else None
    if group == "lpar-memory-statistics":
        print(
            "statistics recovery requires --memory-statistics for bounded raw admission",
            file=sys.stderr,
        )
        return 2
    protocols = {
        "partition-profiles": (
            "partition_profiles",
            "partition-profiles recovery unreadable",
            "partition-profiles recovery",
            "CLEAN: partition-profile original baseline and terminal sequence confirmed",
        ),
        "partition-profile-contract": (
            "partition_profile_contract",
            "profile contract recovery",
            "profile contract recovery",
            "CLEAN: independent original baseline and fixture absence confirmed",
        ),
        "system-profiles": (
            "system_profiles",
            "system-profiles recovery unreadable",
            "system-profiles recovery",
            "CLEAN: system-profile original baseline and finite terminal sequence confirmed",
        ),
        "lpar-memory-move": (
            "memory_move",
            "memory-move recovery",
            "memory-move recovery",
            "CLEAN: finite memory-move success and both original baselines confirmed; no writes",
        ),
        "lpar-accelerator-qos": (
            "accelerator_qos",
            "accelerator-qos recovery",
            "accelerator-qos recovery",
            "CLEAN: finite accelerator-qos proof and original baselines confirmed; no writes",
        ),
        "lpar-processor-move": (
            "processor_move",
            "processor-move recovery",
            "processor-move recovery",
            "CLEAN: finite processor-move success and both original baselines confirmed; no writes",
        ),
    }
    # Unknown or malformed group values must continue to generic witness admission.
    protocol = protocols.get(group) if isinstance(group, str) else None
    if protocol is None:
        return None
    module_name, credentials_label, error_label, success = protocol
    module = import_module(f"live_test.arms.{module_name}")
    if not runner._bootstrap_config():
        print(f"{credentials_label}: credentials unavailable", file=sys.stderr)
        return 2
    try:
        asyncio.run(module.recover_document(HMCConfig(), document))
    except Exception as error:  # noqa: BLE001 — incomplete evidence never attests CLEAN
        print(f"{error_label} unconfirmed: {type(error).__name__}", file=sys.stderr)
        return 2
    print(success)
    return 0
