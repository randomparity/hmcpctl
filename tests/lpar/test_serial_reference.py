"""The two allocated serial field bindings retain the complete nested grammar."""

import hashlib
import json
from pathlib import Path

import pytest

from hmcpctl.documents.lpar_serial import VirtualSerialAdapter


@pytest.mark.parametrize("generation,line", [("p10", 41), ("p11", 49)])
def test_two_corpus_contract(generation, line):
    root = Path(__file__).resolve().parents[2]
    source = root / "docs/refs" / f"hmc-commands-{generation}" / "commands/mksyscfg.md"
    if not source.is_file():
        pytest.skip(
            f"Local {generation} corpus absent; serial source binding unevidenced"
        )
    inventory = json.loads((root / "docs/capabilities/corpora.json").read_text())
    topic_id = f"commands-{generation}:commands/mksyscfg"
    topic = next(item for item in inventory["topics"] if item["id"] == topic_id)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == topic["sha256"].replace(
        "-", ""
    )
    unit = next(
        item
        for item in inventory["source_units"]
        if item["id"] == f"{topic_id}:L{line:05}:command-option"
    )
    assert unit["line"] == line and unit["text"] == "-f"
    assert unit["accounting"] == {"id": "cli:commands/mksyscfg", "kind": "row"}
    section = (
        source.read_text()
        .splitlines()[line - 1]
        .split("[**virtual_serial_adapters**]", 1)[1]
        .split("[**vnic_adapters**]", 1)[0]
    )
    # Bind the complete grammar without copying local-corpus prose into this file.
    assert (
        hashlib.sha256(section.encode()).hexdigest()
        == "867ccea5-ce9a6c20-c14dcc47-6e10ec5b-26f285e2-0e321c22-686480b0-f20a4e7d".replace(
            "-", ""
        )
    )
    for component in (
        "virtual-slot-number",
        "client-or-server",
        "supports-HMC",
        "remote-lpar-ID",
        "remote-lpar-name",
        "remote-slot-number",
        "is-required",
    ):
        assert f"_{component}_" in section
    assert "**14/server/0////0**" in section
    assert VirtualSerialAdapter(14, "server", False, 0).native() == "14/server/0////0"
