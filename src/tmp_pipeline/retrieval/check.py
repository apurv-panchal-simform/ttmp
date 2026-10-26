"""Step 6, the answer check (plan 5.12), in code:

1. every number, time, date, RP and phone number in the answer appears in the context;
2. every cited page belongs to the context;
3. nothing from a struck-through or generic block is presented as this site's fact.

Unconfirmed values and conflicting versions are written in the chunk text itself; the answer prompt tells the model to
say so, and the check does not enforce it.

Problems are returned as short sentences, the form Q4 is given them in. The check only reads the answer's text with
plain scans (``words``); it never decides what is fetched.
"""
from __future__ import annotations

from .context import Context
from .words import (
    cited_pages,
    drop_list_numbers,
    has_any,
    normalise,
    number_forms,
    number_tokens,
    sentences,
)

_UNCONFIRMED_PHRASES = ("unconfirm", "unverified", "not confirmed", "not been confirmed", "not verified", "uncertain",
                        "could not be read")
_GENERIC_PHRASES = ("general standard", "generic", "not specific to this site", "contractor's standard",
                    "contractor's general standard", "in general")
_STRUCK_PHRASES = ("struck", "doesn't apply", "does not apply", "not applicable")


def says_unconfirmed(text: str) -> bool:
    """The answer says a value is unconfirmed or unverified."""
    return has_any(text, _UNCONFIRMED_PHRASES)


def _forms(token: str) -> set[str]:
    """Equivalent spellings: 15:00 / 1500, 0.150 / 0.15, 1,000 / 1000."""
    return number_forms(token)


def _known_numbers(text: str) -> set[str]:
    out: set[str] = set()
    for t in number_tokens(text):
        out |= _forms(t)
    return out


def _cited_pages(answer: str) -> list[int]:
    return cited_pages(answer)


def _words(text: str) -> list[str]:
    return [w for w in normalise(text).split() if len(w) > 2]


def _without_list_numbers(answer: str) -> str:
    """Drop the numbers of a numbered list (1, 2, 3 ... in order); any other number at a line start is checked."""
    return drop_list_numbers(answer)


def _grams(text: str, n: int = 6) -> set[str]:
    ws = _words(text)
    return {" ".join(ws[i:i + n]) for i in range(len(ws) - n + 1)}


def check_answer(answer: str, ctx: Context) -> list[str]:
    problems: list[str] = []
    known = _known_numbers(ctx.text) | _known_numbers(ctx.question)
    for tok in dict.fromkeys(number_tokens(_without_list_numbers(answer))):
        if not (_forms(tok) & known):
            problems.append(f"number {tok} not in context")
    for p in dict.fromkeys(_cited_pages(answer)):
        if p not in ctx.pages:
            problems.append(f"page {p} not in context")
    for texts, label, phrases in ((ctx.generic, "general standard", _GENERIC_PHRASES),
                                  (ctx.struck, "struck-through section", _STRUCK_PHRASES)):
        if not texts or has_any(answer, phrases):
            continue
        site = ctx.text
        for t in texts:
            site = site.replace(t, "")
        src = {g for t in texts for g in _grams(t)}
        own = _grams(site)
        for sentence in sentences(answer):
            gram = _grams(sentence)
            if gram & src and not gram & own:
                problems.append(f"text from a {label} is presented as this site's fact")
                break
    return problems
