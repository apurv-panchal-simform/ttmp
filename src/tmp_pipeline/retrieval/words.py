"""Text helpers for retrieval, with no regular expressions anywhere: plain character scans.

Nothing in the retrieval package decides what to fetch by matching patterns against the question. What the question
needs (a calculation, a date check, another TMP) is decided by the model (Q3), which calls the code tools; these helpers
only normalise text so stored names, codes and numbers compare equal (the keyword index, the answer check, name
matching).
"""
from __future__ import annotations

import contextlib

_LOWER = "abcdefghijklmnopqrstuvwxyz"
_DIGITS = "0123456789"


def is_digit(ch: str) -> bool:
    return ch in _DIGITS and ch != ""


def is_alnum(ch: str) -> bool:
    return ch != "" and ch.isascii() and ch.isalnum()


def is_word(ch: str) -> bool:
    """A word character: ascii letter, digit or underscore (a boundary lies between a word and a non-word character)."""
    return ch == "_" or is_alnum(ch)


def digit_run(text: str, i: int) -> int:
    """The index just past the digits that start at ``i`` (``i`` itself when there are none)."""
    while i < len(text) and is_digit(text[i]):
        i += 1
    return i


def digit_runs(text: str) -> list[str]:
    """Every run of digits in the text, in order."""
    out, i = [], 0
    while i < len(text):
        if is_digit(text[i]):
            j = digit_run(text, i)
            out.append(text[i:j])
            i = j
        else:
            i += 1
    return out


def normalise(text: str) -> str:
    """Lower case; anything but ascii letters, digits, % and & becomes a space; spaces collapsed."""
    out = []
    for ch in text.lower():
        out.append(ch if (is_alnum(ch) or ch in "%&") else " ")
    return " ".join("".join(out).split())


def alnum_only(text: str) -> str:
    """Ascii letters and digits only, lower case ("TMD-04" -> "tmd04")."""
    return "".join(ch for ch in text.lower() if is_alnum(ch))


def norm_ref(text: str) -> str:
    """A TMP reference compared without spaces or punctuation: "KTS 26-1581" -> "KTS261581"."""
    return "".join(ch for ch in text.upper() if is_alnum(ch))


def norm_name(text: str, abbreviations: dict[str, str]) -> str:
    """Names compared for matching: normalised, abbreviations expanded ("ave" -> "avenue")."""
    return " ".join(abbreviations.get(w, w) for w in normalise(text).split())


def has_word(text: str, word: str) -> bool:
    """``word`` appears in ``text`` as a whole word (case sensitive)."""
    if not word:
        return False
    start = text.find(word)
    while start != -1:
        end = start + len(word)
        before = text[start - 1] if start else ""
        after = text[end] if end < len(text) else ""
        if not is_word(before) and not is_word(after):
            return True
        start = text.find(word, start + 1)
    return False


def split_letters_digits(token: str) -> list[str]:
    """"7am" -> ["7", "am"]; a token of digits, dots and slashes stays whole."""
    if all(is_digit(c) or c in "./" for c in token):
        return [token]
    parts, cur = [], token[:1]
    for ch in token[1:]:
        if is_digit(ch) != is_digit(cur[-1]) and is_alnum(ch) and is_alnum(cur[-1]):
            parts.append(cur)
            cur = ch
        else:
            cur += ch
    parts.append(cur)
    return parts


def raw_tokens(text: str) -> list[str]:
    """Lower-case words: runs of ascii letters and digits, where a "." or "/" followed by a digit continues the run
    ("0.465", "1/2"); everything else separates words."""
    t = text.lower()
    out, i, n = [], 0, len(t)
    while i < n:
        if not is_alnum(t[i]):
            i += 1
            continue
        j = i
        while j < n and is_alnum(t[j]):
            j += 1
        while j + 1 < n and t[j] in "./" and is_digit(t[j + 1]):
            j = digit_run(t, j + 1)
        out.append(t[i:j])
        i = j
    return out


# --- references the question or a chunk may carry ------------------------------------------------------------------

def _skip_space(t: str, i: int) -> int:
    while i < len(t) and t[i].isspace():
        i += 1
    return i


def tmd_numbers(text: str) -> list[int]:
    """Diagram numbers written as "TMD 4", "TMD-04", "TMD Sheet 04" or "TMD No. 4", in order of appearance."""
    t = text.lower()
    out, i = [], t.find("tmd")
    while i != -1:
        j = i + 3
        if not (i and is_word(t[i - 1])):
            j = _skip_space(t, j)
            if j < len(t) and t[j] in "-–":
                j = _skip_space(t, j + 1)
            if t.startswith("sheet", j):
                j = _skip_space(t, j + 5)
            elif t.startswith("no", j):
                j += 2
                if j < len(t) and t[j] == ".":
                    j += 1
                j = _skip_space(t, j)
            k = digit_run(t, j)
            digits = t[j:k]
            if digits and len(digits.lstrip("0")) <= 2 and not (k < len(t) and is_word(t[k])):
                out.append(int(digits))
        i = t.find("tmd", i + 3)
    return out


def rp_values(text: str) -> list[float]:
    """Route positions written as "RP 0.465" or "rp0.41", in order of appearance."""
    t = text.lower()
    out, i = [], t.find("rp")
    while i != -1:
        j = i + 2
        if not (i and is_word(t[i - 1])):
            j = _skip_space(t, j)
            k = digit_run(t, j)
            if k > j:
                end = k
                if end < len(t) and t[end] == "." and digit_run(t, end + 1) > end + 1:
                    end = digit_run(t, end + 1)
                if not (end < len(t) and is_word(t[end])):
                    out.append(float(t[j:end]))
        i = t.find("rp", i + 2)
    return out


def phone_numbers(text: str) -> list[str]:
    """New Zealand style phone numbers: "09 355 3553", "021 733 454", "0800 JAMMED"; spaces and dashes inside."""
    out, n, i = [], len(text), 0
    while i < n:
        if text[i] == "0" and not (i and is_word(text[i - 1])):
            end = _phone_end(text, i)
            if end:
                out.append(text[i:end])
                i = end
                continue
        i += 1
    return out


def _phone_end(text: str, i: int) -> int:
    """End index of a phone number starting at the 0 at ``i``, or 0."""
    n = len(text)
    if text.startswith("0800", i):                                    # "0800 JAMMED"
        k = _skip_space(text, i + 4)
        m = k
        while m < n and text[m].isascii() and text[m].isupper():
            m += 1
        if 4 <= m - k <= 8 and not (m < n and is_word(text[m])):
            return m
    after_zero = digit_run(text, i + 1) - (i + 1)
    for head in range(min(3, after_zero), 0, -1):                     # 0 plus 1-3 digits, then 3, then 3-4 digits
        k = i + 1 + head
        if k < n and (text[k].isspace() or text[k] == "-"):
            k += 1
        if digit_run(text, k) - k < 3:
            continue
        b = k + 3
        if b < n and (text[b].isspace() or text[b] == "-"):
            b += 1
        c = digit_run(text, b)
        if 3 <= c - b <= 4 and not (c < n and is_word(text[c])):
            return c
    return 0


def number_tokens(text: str) -> list[str]:
    """Numbers as written: digits with "." "," or ":" inside ("7.5", "1,000", "15:00"). A number glued to letters
    ("7am") or following a letter or a dot is not one; "1.5m" yields "1" (the part not glued to a letter)."""
    out, n, i = [], len(text), 0
    while i < n:
        if not is_digit(text[i]) or (i and (is_word(text[i - 1]) or text[i - 1] == ".")):
            i += 1
            continue
        ends = [digit_run(text, i)]
        while ends[-1] < n and text[ends[-1]] in ".,:" and digit_run(text, ends[-1] + 1) > ends[-1] + 1:
            ends.append(digit_run(text, ends[-1] + 1))
        while ends and ends[-1] < n and is_word(text[ends[-1]]):
            ends.pop()
        if ends:
            out.append(text[i:ends[-1]])
            i = ends[-1]
        else:
            i = digit_run(text, i)
    return out


def number_forms(token: str) -> set[str]:
    """Equivalent spellings of a number: 15:00 / 1500, 0.150 / 0.15, 1,000 / 1000."""
    out = {token, token.replace(",", "")}
    for sep in ":.":
        a, _, b = token.partition(sep)
        if sep in token and a.isdigit() and b.isdigit() and 1 <= len(a) <= 2 and len(b) == 2 and token.count(sep) == 1:
            out |= {f"{int(a):02d}{b}", f"{int(a)}:{b}", f"{int(a):02d}:{b}"}
    if len(token) == 4 and token.isdigit() and (token[:2] in [f"{h:02d}" for h in range(24)]) and token[2] in "012345":
        out |= {f"{token[:2]}:{token[2:]}", f"{int(token[:2])}:{token[2:]}"}
    with contextlib.suppress(ValueError):
        out.add(f"{float(token.replace(',', '')):g}")
    return out


def strip_page_tail(text: str) -> str:
    """Remove a trailing " — page 3" / " — pages 3, 11" from a breadcrumb."""
    i = text.rfind(" — ")
    if i == -1:
        return text
    tail = text[i + 3:]
    for key in ("pages ", "page "):
        if tail.startswith(key):
            rest = tail[len(key):]
            if rest and all(is_digit(c) or c in ", –-" for c in rest) and any(is_digit(c) for c in rest):
                return text[:i].rstrip()
    return text


def cited_pages(answer: str) -> list[int]:
    """Pages an answer cites: "p.12", "pp.3, 10", "page 5", "pages 3–5", "p. 3 and p. 7"; a range of two pages
    written with a dash or "to" counts every page in between (up to 40)."""
    t = answer.lower()
    out: list[int] = []
    i, n = 0, len(t)
    while i < n:
        key = next((k for k in ("pp.", "p.", "pages", "page") if t.startswith(k, i)), None)
        if key is None or (i and is_word(t[i - 1])):
            i += 1
            continue
        j = _skip_space(t, i + len(key))
        k = digit_run(t, j)
        if k == j:
            i += len(key)
            continue
        end = k
        while True:
            m = _skip_space(t, end)
            sep = next((s for s in (",", "and", "&", "–", "-", "to") if t.startswith(s, m)), None)
            if sep is None:
                break
            m = _skip_space(t, m + len(sep))
            if t.startswith("p.", m):
                m = _skip_space(t, m + 2)
            q = digit_run(t, m)
            if q == m:
                break
            end = q
        span = t[j:end]
        nums = [int(d) for d in digit_runs(span)]
        ranged = "–" in span or "-" in span or "to" in span
        if len(nums) == 2 and ranged and nums[1] > nums[0] and nums[1] - nums[0] < 40:
            out += list(range(nums[0], nums[1] + 1))
        else:
            out += nums
        i = end
    return out


def sentences(text: str) -> list[str]:
    """Pieces between sentence ends and line breaks, empty ones dropped."""
    out: list[str] = []
    cur: list[str] = []
    for ch in text:
        if ch in ".!?\n":
            if cur:
                out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def drop_list_numbers(text: str) -> str:
    """Remove the numbers of a numbered list ("1. ", "2) " at line starts, counting 1, 2, 3 ... in order); any other
    number at a line start is left to be checked."""
    lines = text.splitlines(keepends=True)
    marks: list[tuple[int, int, str]] = []                # (line index, number, text after the marker)
    for idx, line in enumerate(lines):
        body = line.lstrip()
        lead = line[:len(line) - len(body)]
        k = digit_run(body, 0)
        if 1 <= k <= 2 and k < len(body) and body[k] in ".)" and k + 1 < len(body) and body[k + 1].isspace():
            marks.append((idx, int(body[:k]), lead + body[k + 1:].lstrip(" \t")))
    if marks and [m[1] for m in marks] == list(range(1, len(marks) + 1)):
        for idx, _, new in marks:
            lines[idx] = new
        return "".join(lines)
    return text


def rstrip_lines(text: str) -> str:
    """Trailing spaces and tabs removed from every line; the text trimmed."""
    return "\n".join(line.rstrip(" \t") for line in text.split("\n")).strip()


def has_any(text: str, phrases: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(p in low for p in phrases)
