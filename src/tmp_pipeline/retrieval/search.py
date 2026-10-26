"""Hybrid keyword + vector search over one TMP's chunks, fusion, ranking and expansion.

Filters first (doc, access, applicability). Keyword candidates come from BM25 over the content, summary, breadcrumb,
glossary expansions and codes (so "RP 0.465", "TMD 04" and phone numbers match exactly); vector candidates from the
embedded question when an embedder is configured. Reciprocal rank fusion merges the two lists. Ranking rules then keep
at most two general-standard chunks and collapse identical boilerplate. Expansion adds the chunks a hit needs to be
read correctly: the parent table, the linked sheet's overview, sibling parts, the rest of a form section and
the other side of a conflict.

``LocalSearch`` does all of this in memory over ``chunks.jsonl``; the optional ``search`` package does the
candidate and fusion step in Azure AI Search and reuses ``rank_and_expand``.
"""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .. import logging_ as lg
from ..cache import FileCache
from ..config import Config
from ..ingestion.chunking.model import Chunk, ChunkSet
from ..ingestion.chunking.summarise import fill_embedding_text
from ..ingestion.embedding.embeddings import CachedEmbeddings, EmbeddingClient
from .store import FactsStore, User
from .words import alnum_only, has_word, phone_numbers, raw_tokens, rp_values, split_letters_digits, tmd_numbers

STOPWORDS = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from", "has", "have", "how", "i", "if", "in", "is", "it", "its", "of", "on", "or", "our", "the", "this", "that", "to", "was", "we", "what", "when", "where", "which", "who", "will", "with", "would", "there", "their", "they", "them", "any", "all"])
FORM_TYPES = ("form_fields", "yes_no_questions", "vulnerable_user")
ROW_TYPES = ("narrative_table_row", "register_row")
COLUMN_VIEWS = {"register_row": "register_summary"}    # row chunk type -> the chunks that group its table by value
MAX_VIEWS = 3           # column views that may replace row hits in one search
MAX_GENERIC = 2                 # general-standard chunks one search keeps; the rest are site-specific
K1, B = 1.2, 0.75


@dataclass
class SearchRequest:
    doc_id: str
    query: str
    k: int = 6
    include_not_applicable: bool = False


@dataclass
class Hit:
    chunk: Chunk
    score: float
    via: str = "match"                  # match | parent | linked_sheet | sibling | form_section | conflict

    @property
    def page(self) -> int | None:
        return self.chunk.metadata.get("page")


class ChunkSearch(Protocol):
    def search(self, req: SearchRequest, user: User) -> list[Hit]: ...


_UNIT_WORDS = {"minutes": "min", "minute": "min", "mins": "min", "hours": "h", "hour": "h", "hrs": "h", "hr": "h",
               "metres": "m", "meters": "m", "metre": "m", "meter": "m", "kilometres": "km", "kph": "km/h"}


def stem(w: str) -> str:
    """A light suffix strip so "exceeding", "exceeds" and "exceed" meet; numbers and codes are left alone."""
    if w in _UNIT_WORDS:
        return _UNIT_WORDS[w]
    if not w.isalpha() or len(w) <= 4:
        return w
    for suffix in ("ing", "ed", "es", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3 and not w.endswith(("ss", "eed")):
            return w[:-len(suffix)]
    return w


def tokens(text: str) -> list[str]:
    out = []
    for w in raw_tokens(text):
        for part in split_letters_digits(w):
            if part and part not in STOPWORDS:
                out.append(stem(part))
    return out


def code_key(code: str) -> str:
    """One spelling per code: "TMD 4", "TMD-04" -> "tmd04"; "RP 0.41" -> "rp0.410"; phones -> digits/letters."""
    c = code.strip()
    flat = alnum_only(c)
    if flat.startswith("tmd") and len(tmd_numbers(c)) == 1:
        rest = flat[3:]
        for word in ("sheet", "no"):
            if rest.startswith(word):
                rest = rest[len(word):]
        if rest.isdigit():
            return f"tmd{int(rest):02d}"
    if c.lower().startswith("rp") and len(rp_values(c)) == 1:
        return f"rp{rp_values(c)[0]:.3f}"
    return "".join(ch for ch in c.lower() if (ch.isascii() and ch.isalnum()) or ch == ".")


def query_codes(query: str) -> list[str]:
    """The codes a query names: diagram numbers, route positions and phone numbers, each in one spelling."""
    out = [f"tmd{n:02d}" for n in tmd_numbers(query)]
    out += [f"rp{v:.3f}" for v in rp_values(query)]
    out += [code_key(p) for p in phone_numbers(query)]
    return out


def glossary_terms(text: str, glossary: dict[str, str]) -> list[str]:
    """The expansions of the glossary abbreviations written in ``text`` as whole words."""
    return [full for abbr, full in glossary.items() if has_word(text, abbr)]


@dataclass
class _Index:
    chunks: list[Chunk]
    docs: list[Counter] = field(default_factory=list)
    lengths: list[int] = field(default_factory=list)
    df: Counter = field(default_factory=Counter)
    avg: float = 1.0
    vectors: list[list[float]] | None = None

    @classmethod
    def build(cls, chunks: list[Chunk], glossary: dict[str, str]) -> _Index:
        ix = cls(chunks)
        for c in chunks:
            crumb = c.header.split("\n", 1)[0]
            words = tokens(f"{crumb}\n{c.content}\n{c.summary or ''}")
            words += tokens(" ".join(glossary_terms(c.content, glossary)))
            words += tokens(" ".join(c.keywords))            # catalog names for what the chunk states (chunks.keywords)
            codes = [code_key(x) for x in c.metadata.get("codes") or []]
            bag = Counter(words)
            for code in codes:
                bag[code] += 3          # an exact code match outweighs ordinary words
            ix.docs.append(bag)
            ix.lengths.append(sum(bag.values()))
            ix.df.update(set(bag))
        ix.avg = (sum(ix.lengths) / len(ix.lengths)) if ix.lengths else 1.0
        return ix

    def bm25(self, query: list[str]) -> list[float]:
        n = len(self.docs)
        scores = []
        for bag, length in zip(self.docs, self.lengths):
            s = 0.0
            for q in query:
                tf = bag.get(q, 0)
                if not tf:
                    continue
                idf = math.log(1 + (n - self.df[q] + 0.5) / (self.df[q] + 0.5))
                s += idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * length / self.avg))
            scores.append(s)
        return scores


def _cosine(a: list[float], b: list[float]) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def allowed(c: Chunk, req: SearchRequest) -> bool:
    return req.include_not_applicable or c.metadata.get("applicability", "active") == "active"


def _part_number(chunk_id: str) -> int | None:
    """The n of an id ending ":part-n", else None."""
    head, sep, tail = chunk_id.rpartition(":part-")
    return int(tail) if sep and head and tail.isdigit() else None


def _neighbours(c: Chunk) -> list[str]:
    """The parts just before and after a split chunk's part (they overlap it, so a list stays whole); a chunk that
    was split without part numbers brings all its siblings."""
    n = _part_number(c.chunk_id)
    if n is None:
        return list(c.siblings)
    return [s for s in c.siblings if (k := _part_number(s)) is not None and abs(k - n) == 1]


def _column_views(hits: list[Hit], scored: list[tuple[Chunk, float]], all_chunks: list[Chunk],
                  req: SearchRequest) -> list[Hit]:
    """When 3 or more hits are rows of a table that has column views ("Initial risk Very High (11): …"), the question
    is about the table, and k rows can only show part of it: the best views take the place of the lowest rows, so the
    hits stay k. Needed because keyword ranking (Cosmos BM25 above all) puts short rows above the longer views."""
    rows = Counter(h.chunk.chunk_type for h in hits if h.chunk.chunk_type in COLUMN_VIEWS)
    row_type = next((t for t, n in rows.most_common() if n >= 3), None)
    if row_type is None:
        return hits
    view_type = COLUMN_VIEWS[row_type]
    have = {h.chunk.chunk_id for h in hits}
    score = {c.chunk_id: s for c, s in scored}
    views = sorted((c for c in all_chunks if c.chunk_type == view_type and c.chunk_id not in have and allowed(c, req)),
                   key=lambda c: (-score.get(c.chunk_id, 0.0), all_chunks.index(c)))[:MAX_VIEWS]
    if not views:
        return hits
    row_hits = [h for h in hits if h.chunk.chunk_type == row_type]
    drop = {id(h) for h in row_hits[max(0, len(row_hits) - len(views)):]}      # the lowest-ranked rows
    kept = [h for h in hits if id(h) not in drop]
    top = kept[0].score if kept else 0.0
    return [Hit(c, max(score.get(c.chunk_id, 0.0), top)) for c in views] + kept


def rank_and_expand(fused: list[tuple[Chunk, float]], req: SearchRequest, all_chunks: list[Chunk], cfg: Config) -> list[Hit]:
    """Ranking rules and expansion over fused candidates, best first."""
    r = cfg.retrieval
    scored = sorted(((c, s) for c, s in fused if s > 0), key=lambda cs: -cs[1])
    seen_boiler: set[str] = set()
    hits: list[Hit] = []
    generic = 0
    for c, s in scored:
        if c.metadata.get("scope", "site_specific") == "generic":     # the contractor's general standard: a few at most
            generic += 1
            if generic > MAX_GENERIC:
                continue
        bh = c.metadata.get("boilerplate_hash") if c.metadata.get("is_boilerplate") else None
        if bh:
            if bh in seen_boiler:
                continue
            seen_boiler.add(bh)
        hits.append(Hit(c, s))
        if len(hits) >= req.k:
            break
    hits = _column_views(hits, scored, all_chunks, req)
    by_id = {c.chunk_id: c for c in all_chunks}
    have = {h.chunk.chunk_id for h in hits}
    extra: list[Hit] = []

    def add(cid: str | None, via: str, score: float) -> None:
        c = by_id.get(cid or "")
        if c is not None and c.chunk_id not in have and allowed(c, req):
            have.add(c.chunk_id)
            extra.append(Hit(c, score, via))

    parents = Counter(h.chunk.parent_id for h in hits if h.chunk.chunk_type in ROW_TYPES and h.chunk.parent_id)
    for pid, n in parents.items():
        if n >= 3:
            add(pid, "parent", 0.0)
    for h in hits:
        for sib in _neighbours(h.chunk):
            add(sib, "sibling", h.score)
        if h.chunk.chunk_type in FORM_TYPES and h.chunk.parent_id:
            for c in all_chunks:
                if c.parent_id == h.chunk.parent_id:
                    add(c.chunk_id, "form_section", h.score)
    room = r.max_linked_sheets             # an overview is about 450 tokens: only the best hits' sheets get one
    for h in hits[:3]:
        linked = h.chunk.metadata.get("linked_sheets") or []
        if h.chunk.chunk_type != "doc_card" and not h.chunk.chunk_type.startswith("sheet_") and 0 < len(linked) <= 2:
            for tid in linked:
                if room <= 0:
                    break
                ov = next((c for c in all_chunks if c.chunk_type == "sheet_overview" and tid in (c.metadata.get("tmd_ids") or [])), None)
                before = len(extra)
                add(ov.chunk_id if ov else None, "linked_sheet", h.score / 2)
                room -= len(extra) - before
    for h in hits[:2]:                          # the other side of a conflict the best hits carry
        if h.chunk.chunk_type == "doc_card":
            continue
        for cid in h.chunk.metadata.get("conflict_ids") or []:
            others = [c for c in all_chunks if cid in (c.metadata.get("conflict_ids") or [])
                      and c.chunk_type not in ("doc_card", "sheet_sign_schedule") and c.chunk_id != h.chunk.chunk_id]
            if others:      # any chunk that shows the other side will do: take the cheapest to put in the context
                add(min(others, key=lambda c: (c.tokens, c.chunk_id)).chunk_id, "conflict", h.score / 2)
    return hits + extra[:req.k]


class LocalSearch:
    """In-memory hybrid search over the stored chunks (the default backend)."""

    def __init__(self, store: FactsStore, cfg: Config, embedder: EmbeddingClient | None = None,
                 vector_dir: Callable[[str], Path | None] | None = None):
        self.store, self.cfg, self.embedder, self.vector_dir = store, cfg, embedder, vector_dir
        self._ix: dict[tuple[str, tuple[str, ...]], _Index] = {}

    def _index(self, chunks: ChunkSet, user: User) -> _Index:
        key = (chunks.doc_id, user.groups)
        if key not in self._ix:
            ix = _Index.build(chunks.chunks, self.cfg.chunks.glossary)
            if self.embedder is not None:
                folder = self.vector_dir(chunks.doc_id) if self.vector_dir else None
                emb: EmbeddingClient = (CachedEmbeddings(self.embedder, FileCache(folder / "vectors"))
                                        if folder else self.embedder)
                try:
                    ix.vectors = emb.embed([c.embedding_text for c in fill_embedding_text(chunks.chunks, self.cfg)])
                except Exception as e:            # noqa: BLE001  (no vectors: keyword search still works)
                    lg.log_warning("search", "vectors_unavailable", doc_id=chunks.doc_id, error=str(e))
            self._ix[key] = ix
        return self._ix[key]

    def search(self, req: SearchRequest, user: User) -> list[Hit]:
        chunks = self.store.chunks(req.doc_id, user)
        if chunks is None or not chunks.chunks:
            return []
        ix = self._index(chunks, user)
        keep = [i for i, c in enumerate(ix.chunks) if allowed(c, req)]
        if not keep:
            return []
        kw = ix.bm25(tokens(req.query) + query_codes(req.query))
        kw_rank = sorted((i for i in keep if kw[i] > 0), key=lambda i: -kw[i])[:self.cfg.retrieval.candidates]
        if kw_rank:                     # a chunk that shares one common word with the question is not worth a slot
            floor = self.cfg.retrieval.min_score_ratio * kw[kw_rank[0]]
            kw_rank = [i for i in kw_rank if kw[i] >= floor]
        vec_rank: list[int] = []
        if ix.vectors is not None and self.embedder is not None:
            try:
                qv = self.embedder.embed([req.query])[0]
                sims = {i: _cosine(qv, ix.vectors[i]) for i in keep}
                vec_rank = sorted(sims, key=lambda i: -sims[i])[:self.cfg.retrieval.candidates]
            except Exception as e:                # noqa: BLE001
                lg.log_warning("search", "query_embedding_failed", error=str(e))
        k = self.cfg.retrieval.rrf_k
        fused: dict[int, float] = {}
        for ranking in (kw_rank, vec_rank):
            for rank, i in enumerate(ranking, 1):
                fused[i] = fused.get(i, 0.0) + 1.0 / (k + rank)
        return rank_and_expand([(ix.chunks[i], s) for i, s in fused.items()], req, ix.chunks, self.cfg)
