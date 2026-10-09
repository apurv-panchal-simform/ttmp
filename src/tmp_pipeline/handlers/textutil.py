"""Small text rules shared by the text and table handlers."""
from __future__ import annotations

import re

LIST_ITEM = re.compile(r"^\s*(?:[•·▪●○\-–*]|\d{1,2}[.)])\s+")
_HYPHEN_END = re.compile(r"[A-Za-z]-$")


def join_lines(lines: list[str]) -> str:
    """Join wrapped lines with a space, except after a line-end hyphen (no space is inserted; the hyphen stays,
    because "high-" / "visibility" is a real hyphen as often as a break), and list items start a line.
    Nothing else is rewritten: text is stored as read."""
    out = ""
    for ln in lines:
        if not out:
            out = ln
        elif LIST_ITEM.match(ln):
            out += "\n" + ln
        elif _HYPHEN_END.search(out) and ln[:1].islower():
            out += ln
        else:
            out += " " + ln
    return out
