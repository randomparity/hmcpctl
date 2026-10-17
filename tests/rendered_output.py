"""Helpers for asserting against rendered CLI output."""

from __future__ import annotations


def collapse_whitespace(text: str) -> str:
    """Rejoin rendered output so a phrase matches wherever the console wrapped it.

    Rich breaks a line wherever the console width falls, and a message carrying a
    temporary path, whose length varies by host, ``TMPDIR`` and xdist worker, can put
    that break inside any phrase after it (#1536, #1621). Only breaks at whitespace
    are rejoined: a word wider than the console is folded mid-word, and the joined
    pieces no longer match.
    """
    return " ".join(text.split())
