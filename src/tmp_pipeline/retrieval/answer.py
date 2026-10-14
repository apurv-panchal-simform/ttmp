"""Step 5, write the answer (plan 5.11), the one repair (5.12) and the fixed responses (5.13).

Q3 writes the answer from the context only; Q4 rewrites it once when the check fails. When there is no model, or
the repair still fails, the safe answer is built in code from the same context: the relevant facts, results and
passages with their sources, so it passes the check by construction.
"""
from __future__ import annotations

import re

from ..config import Config
from ..model import prompts
from ..model.client import ModelClient, ask
from .classify import Reading
from .context import Context, conflict_line, page_span, show, versions_text
from .hybrid import tokens
from .recipes import Evidence
from .store import DocInfo
from .tools import FactItem, Value

_CHUNK_CHARS = 420


def role_style(cfg: Config, role: str) -> str:
    styles = cfg.retrieval.role_styles
    return styles.get(role) or styles[cfg.retrieval.default_role]


def fixed(cfg: Config, key: str, **values: object) -> str:
    return cfg.retrieval.responses[key].format(**values)


def write(model: ModelClient | None, reading: Reading, ctx: Context, style: str) -> str | None:
    out = ask(model, prompts.q3_answer(reading.standalone, ctx.text, style))
    text = str((out or {}).get("answer") or "").strip()
    return text or None


def repair(model: ModelClient | None, problems: list[str], answer: str, reading: Reading, ctx: Context) -> str | None:
    out = ask(model, prompts.q4_repair(problems, answer, reading.standalone, ctx.text))
    text = str((out or {}).get("answer") or "").strip()
    return text or None


# --- the safe answer ---------------------------------------------------------------------------------------------

def _value(v: Value) -> str:
    if v.status == "conflict" and v.value is None and v.versions:
        return f"{v.field.replace('_', ' ')}: the document gives different values, {versions_text(v)}"
    text = f"{v.field.replace('_', ' ')} {show(v.value)}" + (f" {v.unit}" if v.unit and v.value is not None else "")
    if v.status == "unverified":
        text += " (unconfirmed)"
    elif v.status == "conflict":
        text += f" (conflict {', '.join(v.conflict_ids)}; see below)" if v.conflict_ids else " (conflict)"
    return text


def _fact(it: FactItem, question: str = "", fields: set[str] | None = None) -> str:
    vals = [v for v in it.values if (fields is None or v.field in fields) and not isinstance(v.value, dict)
            and not (isinstance(v.value, list) and any(isinstance(x, dict) for x in v.value))]
    vals = vals or [v for v in it.values if fields is None or v.field in fields]
    q = set(re.findall(r"[a-z]+", question.lower()))
    vals.sort(key=lambda v: -sum(w in q for w in v.field.split("_")))      # the fields the question names first
    struck = " (struck through, not applicable)" if it.applicability == "not_applicable" else ""
    return f"{it.label}{struck}: " + "; ".join(_value(v) for v in vals[:10]) + f" ({it.source})."


def _clip(text: str, n: int = _CHUNK_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + " …"


def excerpt(content: str, question: str, n: int = _CHUNK_CHARS) -> str:
    """The part of a passage that answers the question: from the line sharing most words with it, onwards."""
    lines = [ln for ln in content.split("\n") if ln.strip()]
    if len(" ".join(content.split())) <= n or len(lines) < 2:
        return _clip(content, n)
    want = set(tokens(question))
    best = max(range(len(lines)), key=lambda i: (len(want & set(tokens(lines[i]))), -i))
    start = best if len(want & set(tokens(lines[best]))) else 0
    return ("… " if start else "") + _clip("\n".join(lines[start:]), n)


def _chunk(h_content: str, crumb: str, generic: bool, struck: bool, question: str = "") -> str:
    label = "General standard (the contractor's, not specific to this site): " if generic else \
        "Struck through (does not apply): " if struck else ""
    return f"{label}{excerpt(h_content, question)} ({crumb})"


def _crumb(header: str) -> str:
    first = header.split("\n", 1)[0]
    parts = [p.strip() for p in first.split("›")][1:]
    return ", ".join(parts).replace(" — page ", ", p.").replace(" — pages ", ", pp.")


def _yes(v: object) -> str:
    return "Yes" if v is True else "No" if v is False else "Not certain"


def safe_answer(reading: Reading, ev: Evidence, ctx: Context, cfg: Config, doc: DocInfo | None) -> str:
    out: list[str] = []
    types = set(reading.types)
    for c in ev.calcs:
        if "error" in c:
            continue
        cross = f", which {c['cross_check']}" if c.get("cross_check") else ""
        srcs = [s for s in c.get("sources") or [] if s not in cross]
        out.append(f"{c['result']} {c['unit']}: {c['working']}{cross}" + (f" ({'; '.join(srcs)})" if srcs else "") + ".")
    for t in ev.times:
        src = "; ".join(x for x in t.get("sources") or [] if x)
        if t.get("stages"):
            out.append("By stage: " + "; ".join(f"{s['stage']}: {_yes(s['allowed']).lower()} ({s['reason']})"
                                                for s in t["stages"]) + f" ({src}).")
        elif t.get("per_day_type"):
            out.append(f"Not certain: {t['reason']}. " + " ".join(
                f"On {p['day_type'].replace('_', ' ')}s: {_yes(p['allowed']).lower()}, {p['reason']} ({p['window']})."
                for p in t["per_day_type"]) + f" ({src}).")
        else:
            window = f" ({t['window']})" if t.get("window") else ""
            out.append(f"{_yes(t.get('allowed'))}. {t.get('reason', '')}{window} ({src}).")
    if ev.status:
        s = ev.status
        signed = [o for o in s["signoffs"] if o["status"] == "signed"]
        unsigned = [o for o in s["signoffs"] if o["status"] == "unsigned"]
        struck = [o for o in s["signoffs"] if o["status"] == "not_applicable"]
        out.append(f"Approval status: {s['approval'].replace('_', ' ')}.")
        if signed:
            out.append("Signed: " + "; ".join(f"{o['role'].replace('_', ' ')} ({o.get('name') or 'no name'}, "
                                              f"p.{o['page']})" for o in signed) + ".")
        if unsigned:
            out.append("Not signed (blank): " + "; ".join(f"{o['role'].replace('_', ' ')}"
                                                          + (f" ({o['name']})" if o.get("name") else "")
                                                          + f", p.{o['page']}" for o in unsigned) + ".")
        if struck:
            out.append("Struck through, so not applicable: " + "; ".join(
                f"{o['role'].replace('_', ' ')}, p.{o['page']}" for o in struck) + ".")
        if "status" in types:
            for c in s["conflicts"]:
                out.append(f"Conflict {c['id']} {c['title']}: " + "; ".join(
                    f"\"{v['value']}\" ({page_span(v.get('pages') or [])})" for v in c["versions"]) + ".")
            for m in s["mismatches"]:
                out.append(f"Rating mismatch: {m['detail']}.")
    for t in ev.tmd:
        if t.get("found"):
            out.append(f"{t['tmd_id']} is \"{t['title']}\" (p.{t['page']}).")
    if ev.tmps:
        out.append("Matching TMPs: " + "; ".join(
            f"{d.tmp_ref}, {d.site_address or ', '.join(d.roads)}, {d.work_start or '?'} to {d.work_end or '?'}"
            for d in ev.tmps) + ".")
    elif "across_tmps" in types:
        out.append("No TMP you can see matches.")
    if ev.versions:
        out.append(f"{len(ev.versions['changed'])} value(s) changed between {' and '.join(ev.versions.get('labels', []))}.")
    show_facts = bool(types & {"fact", "time_check", "calculation", "diagram"}) or not types or not out
    if show_facts:
        items = [it for it in ev.facts if not (ev.calcs and it.entity not in {c.get("what", "").split("[")[0] for c in ev.calcs})]
        for it in items[:10]:
            out.append(_fact(it, reading.standalone))
    show_chunks = bool(types & {"procedure", "general_rule", "summary", "diagram", "fact"}) or not types or not out
    if show_chunks:
        used = 0
        for h_id in ctx.chunk_ids:
            h = next((x for x in ev.chunks if x.chunk.chunk_id == h_id), None)
            if h is None:
                continue
            md = h.chunk.metadata
            out.append(_chunk(h.chunk.content, _crumb(h.chunk.header), md.get("scope") == "generic",
                              md.get("applicability") == "not_applicable", reading.standalone))
            used += 1
            if used >= (3 if types & {"procedure", "general_rule", "diagram"} else 2):
                break
    named = {c["id"] for c in (ev.status or {}).get("conflicts", [])} if "status" in types else set()
    for conflict in ev.conflicts:
        if conflict.conflict_id not in named:
            out.append(f"Note, conflict {conflict_line(conflict)}.")
    for n in ev.notes:
        out.append(f"Not checked: {n}.")
    if "out_of_scope" in types:
        lead = fixed(cfg, "out_of_scope", what="").rstrip(": ").rstrip()
        out.insert(0, lead + (":" if out else "."))
    if not out:
        return fixed(cfg, "not_in_tmp", what="anything that answers this question")
    return "\n".join(out)


def clean(text: str) -> str:
    return re.sub(r"[ \t]+\n", "\n", text).strip()
