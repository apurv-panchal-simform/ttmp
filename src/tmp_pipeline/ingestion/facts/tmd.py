"""One id per diagram (plan section 3.2).

The master list comes from the drawings' title blocks. A mention is matched by number and by title; when they
disagree the title wins, the mention is kept as an alias of the sheet it names and ``id_conflict`` is raised.
A number-only mention matches by number. A title that matches no sheet by its words or the catalog's synonyms
goes to X11; without a model it stays unmatched and unverified (never guessed).
"""
from __future__ import annotations

from ...config import FactsCfg
from ...llm import ModelClient, ask
from ...records import RecordStore, Status
from .. import prompts
from ..catalog import Catalog, normalise
from ..validators import check_x11
from .common import norm_name
from .model import TmdMention, TmdSheet


def sheet_register(records: RecordStore) -> list[TmdSheet]:
    out = []
    for r in sorted(records.of_kind("diagram_sheet"), key=lambda r: r.page):
        tid = r.fields.get("tmd_id")
        if tid is None or not tid.value:
            continue
        title = r.fields.get("title")
        rev = r.fields.get("rev")
        out.append(TmdSheet(str(tid.value), str(r.data.get("sheet") or ""), str(title.value if title else ""),
                            r.page, r.record_id, str(rev.value) if rev and rev.value else None,
                            list(r.data.get("aliases") or [])))
    return out


def _tokens(title: str, cfg: FactsCfg) -> set[str]:
    return {w for w in norm_name(title, cfg).split() if w not in cfg.title_stopwords}


def match_title(title: str, sheets: list[TmdSheet], cfg: FactsCfg, catalog: Catalog) -> tuple[TmdSheet | None, str]:
    """The one sheet whose title shares the most content words with ``title`` (catalog synonyms count as a full
    match), or None with the reason."""
    words = _tokens(title, cfg)
    said = f" {normalise(title)} "
    scores: list[tuple[float, TmdSheet]] = []
    for s in sheets:
        score = float(len(words & _tokens(s.title, cfg)))
        for canonical, names in catalog.diagram_title_synonyms.items():
            if normalise(canonical) == normalise(s.title) and any(f" {normalise(n)} " in said for n in names):
                score += 10
        scores.append((score, s))
    if not words and not any(sc for sc, _ in scores):
        return None, "the title has no words that name a sheet"
    best = max((sc for sc, _ in scores), default=0.0)
    if best == 0:
        return None, "the title matches no sheet"
    top = [s for sc, s in scores if sc == best]
    if len(top) > 1:
        return None, f"the title matches {len(top)} sheets equally ({', '.join(s.tmd_id for s in top)})"
    return top[0], ""


def map_mentions(mentions: list[TmdMention], sheets: list[TmdSheet], cfg: FactsCfg, catalog: Catalog,
                 model: ModelClient | None = None, contexts: dict[str, str] | None = None) -> bool:
    """Map every mention in place. Returns True when X11 was asked at least once."""
    by_number: dict[int, TmdSheet] = {}
    for s in sheets:
        if s.sheet.isdigit():
            by_number.setdefault(int(s.sheet), s)
    used_model = False
    for m in mentions:
        numbered = by_number.get(m.number) if m.number is not None else None
        if not m.title:
            if numbered:
                m.mapped_to, m.method = numbered.tmd_id, "number"
            else:
                m.status = Status.UNVERIFIED
                m.notes.append(f"no sheet is numbered {m.number}")
            continue
        hit, why = match_title(m.title, sheets, cfg, catalog)
        method = "number_and_title" if hit is not None and hit is numbered else "title"
        if hit is None:
            if model is not None:
                used_model = True
                result = ask(model, prompts.x11_match_mention([(s.tmd_id, s.title) for s in sheets], m.text,
                                                              (contexts or {}).get(m.mention_id, m.text)))
                tid, problems = check_x11(result, [s.tmd_id for s in sheets])
                hit = next((s for s in sheets if s.tmd_id == tid), None)
                method = "X11"
                if hit is None:
                    m.status = Status.UNVERIFIED
                    m.notes += [why, *problems]
                    continue
                if problems:
                    m.status = Status.UNVERIFIED
                    m.notes += problems
            else:
                m.status = Status.UNVERIFIED
                m.notes += [why, "no model answered (X11)"]
                continue
        m.mapped_to, m.method = hit.tmd_id, method
        if numbered is not hit:
            m.id_conflict = True
            m.notes.append(f"the number says {numbered.tmd_id if numbered else 'no sheet'}, the title says "
                           f"{hit.tmd_id}: the title wins")
            if m.text not in hit.aliases:
                hit.aliases.append(m.text)
    return used_model
