"""Validate packaged definitions and optionally compare an explicit source transcription.

Public provenance: https://public.dhe.ibm.com/systems/power/docs/hw/p9/p9eai_prg1.pdf
Only the factual notation/section/page inventory is extracted; no publication prose
is generated or distributed by this command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

SOURCE_URL = "https://public.dhe.ibm.com/systems/power/docs/hw/p9/p9eai_prg1.pdf"
NOTATION = re.compile(r"(?:[0-9A-Fa-f]{4}|[0-9A-Fa-fXxYyTtSs]{8}|888-102-[0-9X]{3})")
INVENTORY_FIELDS = ("notation", "applicability", "pdf_pages")


def extract_inventory(source: Path) -> list[dict[str, Any]]:
    """Read every explanation's code identity, including unheaded source entries."""
    inventory = []
    page = 0
    section = ""
    identity = None
    for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        marker = re.fullmatch(r"## PDF page (\d+)", line)
        if marker:
            page = int(marker[1])
        if line.startswith("### ") and line != "### Failing Item":
            section = line[4:]
        code = line.removeprefix("#### ")
        if NOTATION.fullmatch(code):
            if identity is not None:
                raise ValueError(f"source line {number}: code without explanation")
            identity = {"notation": code, "applicability": section, "pdf_pages": [page]}
        if line == "**Explanation:**":
            if identity is None:
                raise ValueError(f"source line {number}: unassociated explanation")
            inventory.append(identity)
            identity = None
    if identity is not None:
        raise ValueError("source ends with code without explanation")
    return inventory


def inventory_digest(inventory: list[dict[str, Any]]) -> str:
    encoded = json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_catalog(
    data: dict[str, Any], expected: list[dict[str, Any]] | None = None
) -> None:
    """Reject missing identities, invalid metadata, and incomplete original summaries."""
    entries = data["entries"]
    actual = [{key: entry[key] for key in INVENTORY_FIELDS} for entry in entries]
    if expected is not None and actual != expected:
        raise ValueError("catalog inventory differs from source inventory")
    seen = set()
    for entry in entries:
        notation = entry["notation"]
        if not NOTATION.fullmatch(notation) or notation.upper() in seen:
            raise ValueError("catalog has invalid or duplicate notation")
        seen.add(notation.upper())
        if not isinstance(entry["definition"], str) or not entry["definition"].strip():
            raise ValueError(f"catalog {notation}: missing definition")
        if not entry["applicability"] or not entry["pdf_pages"]:
            raise ValueError(
                f"catalog {notation}: missing source locator/applicability"
            )
        if not all(
            type(page) is int and 15 <= page <= 136 for page in entry["pdf_pages"]
        ):
            raise ValueError(f"catalog {notation}: invalid PDF page")
    source = data["source"]
    if source["url"] != SOURCE_URL or source["inventory_sha256"] != inventory_digest(
        actual
    ):
        raise ValueError("catalog source metadata/inventory checksum differs")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, help="explicit development transcription"
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "src/hmcpctl/_progress_codes.json",
    )
    args = parser.parse_args()
    try:
        data = json.loads(args.catalog.read_text(encoding="utf-8"))
        expected = extract_inventory(args.source) if args.source else None
        validate_catalog(data, expected)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"progress-code validation failed: {exc}\n")
    print(f"progress codes: {len(data['entries'])} definitions validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
