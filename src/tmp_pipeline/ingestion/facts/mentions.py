"""Every reference to a diagram sheet in the document's text (plan section 3.2, "one ID per diagram").

A mention is a number ("TMD4", "TMD 04", "TMD-04", "TMD Sheet 04") with, when written, the title that follows it
("TMD4: Parking Management"). Records on the diagram sheets' own pages are not scanned: a sheet's title block is
the master list, not a mention of it. Within a record the same mention counts once; a number-only mention on a
page that also gives that number with a title (a cut-off copy of the same text) is dropped.
"""
from __future__ import annotations

import re

from ...config import FactsCfg
from ...records import RecordStore
from ..catalog import normalise
from .common import record_texts, source_of
from .model import TmdMention

_LEAD = re.compile(r"\s*[:\-–—]?\s*")


def _title(rest: str, cfg: FactsCfg) -> tuple[str | None, int]:
    """The title after a mention's number and how many characters of ``rest`` it spans."""
    lead = _LEAD.match(rest)
    start = lead.end() if lead else 0
    body = rest[start:]
    stop = cfg.title_stop.search(body)
    body = body[:stop.start()] if stop else body
    words = re.match(rf"\S+(?:\s+\S+){{0,{cfg.title_max_words - 1}}}", body)
    if not words:
        return None, 0
    raw = words.group(0)
    title = raw.strip(" -–—:,")
    if not title or not title[0].isalpha() or not re.search(r"[A-Za-z]{2}", title):
        return None, 0
    return title, start + len(raw)


def find_mentions(records: RecordStore, cfg: FactsCfg) -> list[TmdMention]:
    sheet_pages = {r.page for r in records.of_kind("diagram_sheet")}
    found: list[TmdMention] = []
    for rec in records.records.values():
        if rec.page in sheet_pages:
            continue
        seen: set[tuple[int, str]] = set()
        for text, context in record_texts(rec):
            for m in cfg.tmd_mention.finditer(text):
                number = int(m.group(1))
                title, span = _title(text[m.end():], cfg)
                key = (number, normalise(title or ""))
                if key in seen:
                    continue
                seen.add(key)
                written = text[m.start():m.end() + span].strip() if title else m.group(0)
                found.append(TmdMention("", " ".join(written.split()), number, title, rec.page,
                                        source_of(rec, " ".join(written.split())), context))
    titled = {(x.page, x.number) for x in found if x.title}
    kept = [x for x in found if x.title or (x.page, x.number) not in titled]
    for i, x in enumerate(kept):
        x.mention_id = f"m{x.page}:{i}"
    return kept


def stage_lists(mentions: list[TmdMention], cfg: FactsCfg) -> dict[tuple[int, str], list[TmdMention]]:
    """Mentions that list the stages of the works, grouped by page and the label or heading that holds them."""
    lists: dict[tuple[int, str], list[TmdMention]] = {}
    for x in mentions:
        if x.context and x.title and cfg.stage_list_context.search(x.context):
            lists.setdefault((x.page, x.context), []).append(x)
    return lists
