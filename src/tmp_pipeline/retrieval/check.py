"""Step 6, the answer check (plan 5.12), in code:

1. every number, time, date, RP and phone number in the answer appears in the context;
2. every cited page belongs to the context;
3. a value used with unverified status or a conflict is flagged as such (from the value's status, not the model);
4. nothing from a struck-through or generic block is presented as this site's fact.

Problems are returned as short sentences, the form Q4 is given them in.
"""
from __future__ import annotations

import re

from ..catalog import normalise
from .context import Context, show

_NUM = re.compile(r"(?<![\w.])\d+(?:[.,:]\d+)*(?![\w])")
_PAGES = re.compile(r"(?i)\b(?:pp?\.|pages?)\s*(\d+(?:\s*(?:,|and|&|–|-|to)\s*(?:p\.\s*)?\d+)*)")
_UNCONFIRMED = re.compile(r"(?i)unconfirm|unverified|not (?:been )?confirmed|not verified|uncertain|could not be read")
_GENERIC = re.compile(r"(?i)general standard|generic|not specific to this site|contractor's (?:general )?standard|in general")
_STRUCK = re.compile(r"(?i)struck|doesn't apply|does not apply|not applicable")
_SENTENCE = re.compile(r"[^.!?\n]+")
_LIST_MARK = re.compile(r"(?m)^(\s*)(\d{1,2})[.)]\s+")       # "1. " at the start of a line may number a list


def _forms(token: str) -> set[str]:
    """Equivalent spellings: 15:00 / 1500, 0.150 / 0.15, 1,000 / 1000."""
    out = {token, token.replace(",", "")}
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", token)
    if m:
        out |= {f"{int(m.group(1)):02d}{m.group(2)}", f"{int(m.group(1))}:{m.group(2)}", f"{int(m.group(1)):02d}:{m.group(2)}"}
    m = re.fullmatch(r"([01]\d|2[0-3])([0-5]\d)", token)
    if m:
        out |= {f"{m.group(1)}:{m.group(2)}", f"{int(m.group(1))}:{m.group(2)}"}
    try:
        f = float(token.replace(",", ""))
        out.add(f"{f:g}")
    except ValueError:
        pass
    return out


def _known_numbers(text: str) -> set[str]:
    out: set[str] = set()
    for t in _NUM.findall(text):
        out |= _forms(t)
    return out


def _cited_pages(answer: str) -> list[int]:
    out: list[int] = []
    for m in _PAGES.finditer(answer):
        nums = [int(n) for n in re.findall(r"\d+", m.group(1))]
        span = m.group(1)
        if len(nums) == 2 and re.search(r"[–-]|to", span) and nums[1] > nums[0] and nums[1] - nums[0] < 40:
            out += list(range(nums[0], nums[1] + 1))
        else:
            out += nums
    return out


def _words(text: str) -> list[str]:
    return [w for w in normalise(text).split() if len(w) > 2]


def _without_list_numbers(answer: str) -> str:
    """Drop the numbers of a numbered list (1, 2, 3 ... in order); any other number at a line start is checked."""
    marks = [int(m.group(2)) for m in _LIST_MARK.finditer(answer)]
    return _LIST_MARK.sub(r"\1", answer) if marks and marks == list(range(1, len(marks) + 1)) else answer


def _grams(text: str, n: int = 6) -> set[str]:
    ws = _words(text)
    return {" ".join(ws[i:i + n]) for i in range(len(ws) - n + 1)}


def _mentions(answer: str, value: object) -> bool:
    """The answer states this value (a number as a whole token, text by most of its words)."""
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return bool(_forms(f"{value:g}") & _known_numbers(answer))
    text = show(value)
    if len(text) < 3:
        return False
    if normalise(text) in normalise(answer):
        return True
    words = _words(text)
    have = set(_words(answer))
    return len(words) >= 2 and sum(w in have for w in words) / len(words) >= 0.6


_TMD = re.compile(r"(?i)\bTMD\s*(?:sheet\s*)?[-–]?\s*(\d{1,2})\b")


def _same_numbers(answer: str, value: object) -> bool:
    """Every number of a conflicting version is in the answer: "TMD 01 – Shoulder Closure" is not used by an answer
    about "TMD-02 Shoulder Closure", although most of its words are."""
    value = str(value or "")
    tmds = {int(n) for n in _TMD.findall(value)}
    if tmds:                                     # a diagram reference is compared as one: "TMD 01" is not any "1"
        return tmds <= {int(n) for n in _TMD.findall(answer)}
    have = {int(n) for n in re.findall(r"\d+", answer)}
    return all(int(n) in have for n in re.findall(r"\d+", value))


def check_answer(answer: str, ctx: Context) -> list[str]:
    problems: list[str] = []
    known = _known_numbers(ctx.text) | _known_numbers(ctx.question)
    for tok in dict.fromkeys(_NUM.findall(_without_list_numbers(answer))):
        if not (_forms(tok) & known):
            problems.append(f"number {tok} not in context")
    for p in dict.fromkeys(_cited_pages(answer)):
        if p not in ctx.pages:
            problems.append(f"page {p} not in context")
    for v, it in ctx.values:
        if v.status == "unverified" and _mentions(answer, v.value) and not _UNCONFIRMED.search(answer):
            problems.append(f"{it.label} {v.field} is unverified but the answer does not say it is unconfirmed")
    for c in ctx.conflicts:
        used = [ver for ver in c.versions if _mentions(answer, ver.get("value")) and _same_numbers(answer, ver.get("value"))
                and not _mentions(ctx.question, ver.get("value"))]       # naming what was asked about is no use of it
        if used and len(used) < min(2, len(c.versions)) and c.conflict_id not in answer:
            problems.append(f"conflict {c.conflict_id} not mentioned (give every version: "
                            + "; ".join(f"\"{ver.get('value')}\"" for ver in c.versions) + ")")
    for texts, label, rx in ((ctx.generic, "general standard", _GENERIC), (ctx.struck, "struck-through section", _STRUCK)):
        if not texts or rx.search(answer):
            continue
        site = ctx.text
        for t in texts:
            site = site.replace(t, "")
        src = {g for t in texts for g in _grams(t)}
        own = _grams(site)
        for sentence in _SENTENCE.findall(answer):
            gram = _grams(sentence)
            if gram & src and not gram & own:
                problems.append(f"text from a {label} is presented as this site's fact")
                break
    return problems
