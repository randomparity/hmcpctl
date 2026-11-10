"""Validation shared by LPAR description composition and SSH writes."""

from __future__ import annotations

from ..documents.native_records import RECORD_DELIMITERS


def validate_lpar_description(description: str) -> None:
    """Raise ``ValueError`` if *description* cannot be written to the HMC."""
    if not description.isascii() or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in description
    ):
        raise ValueError(
            "description contains non-ASCII or non-printable characters; "
            "the HMC only accepts printable ASCII partition descriptions (HSCLC63B)"
        )
    for character, (name, reason) in RECORD_DELIMITERS.items():
        if character in description:
            raise ValueError(
                f"description {description!r} contains {name} ({character!r}); "
                f"{reason} in the HMC CLI -i attribute record, so the text "
                f"would be read as further attributes rather than as the "
                f"description. Remove {name} from the description."
            )
