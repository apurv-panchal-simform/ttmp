"""``search_chunks`` (plan 5.7): hybrid keyword + vector search over one TMP's chunks, fusion, ranking and expansion.

Filters first (doc, access, applicability, chunk types, scope). Keyword candidates come from BM25 over the
content, summary, breadcrumb, glossary expansions and codes (so "RP 0.465", "TMD 04" and phone numbers match
exactly); vector candidates from the embedded question when an embedder is configured. Reciprocal rank fusion
merges the two lists. Ranking rules then put site-specific chunks before generic ones, collapse identical
boilerplate and boost chunks that match the question's TMD, road or RP. Expansion adds the chunks a hit needs to
be read correctly: the parent table, the linked sheet's overview, sibling parts, the rest of a form section and
the other side of a conflict.

``LocalSearch`` does all of this in memory over ``chunks.jsonl``; the optional ``search`` package does the
candidate and fusion step in Azure AI Search and reuses ``rank_and_expand``.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .. import logging_ as lg
from ..cache import FileCache
from ..chunks.model import Chunk, ChunkSet
from ..chunks.summarise import fill_embedding_text
from ..chunks.text import glossary_matches
from ..config import Config
from ..model.embeddings import CachedEmbeddings, EmbeddingClient
from .details import RP_REF, TMD_REF
from .store import FactsStore, User

STOPWORDS = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from", "has", "have", "how", "i", "if", "in", "is", "it", "its", "of", "on", "or", "our", "the", "this", "that", "to", "was", "we", "what", "when", "where", "which", "who", "will", "with", "would", "there", "their", "they", "them", "any", "all"])
_WORD = re.compile(r"[a-z0-9]+(?:[./][0-9]+)*")
_PHONE = re.compile(r"\b0\d{1,3}[\s-]?\d{3}[\s-]?\d{3,4}\b|\b0800\s*[A-Z]{4,8}\b")
FORM_TYPES = ("form_fields", "yes_no_questions", "vulnerable_user")
ROW_TYPES = ("narrative_table_row", "register_row")
K1, B = 1.2, 0.75


@dataclass
class SearchRequest:
    doc_id: str
    query: str
    chunk_types: tuple[str, ...] = ()
    scope: str = "any"                  # site_specific | generic | any (generic only when no site answer)
    include_not_applicable: bool = False
    k: int = 6
    tmd_ids: tuple[str, ...] = ()       # details from the question, for the ranking boost
    roads: tuple[str, ...] = ()
    rps: tuple[float, ...] = ()
    only_tmd: tuple[str, ...] = ()      # search only the chunks of these diagrams (a sheet named in the question)
    broad: bool = False                 # a survey (every sheet, a summary): keep the weak matches too, no score cutoff


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


_SPLIT = re.compile(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)")
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
    for w in _WORD.findall(text.lower()):
        for part in _SPLIT.split(w) if not re.fullmatch(r"[\d./]+", w) else [w]:
            if part and part not in STOPWORDS:
                out.append(stem(part))
    return out


def code_key(code: str) -> str:
    """One spelling per code: "TMD 4", "TMD-04" -> "tmd04"; "RP 0.41" -> "rp0.410"; phones -> digits/letters."""
    m = TMD_REF.fullmatch(code.strip())
    if m:
        return f"tmd{int(m.group(1)):02d}"
    m = RP_REF.fullmatch(code.strip())
    if m:
        return f"rp{float(m.group(1)):.3f}"
    return re.sub(r"[^a-z0-9.]", "", code.lower())


def query_codes(query: str) -> list[str]:
    out = [f"tmd{int(m.group(1)):02d}" for m in TMD_REF.finditer(query)]
    out += [f"rp{float(m.group(1)):.3f}" for m in RP_REF.finditer(query)]
    out += [code_key(m.group(0)) for m in _PHONE.finditer(query)]
    return out


def glossary_terms(text: str, glossary: dict[str, str]) -> list[str]:
    return [full for _, full in glossary_matches(text, glossary)]


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
    md = c.metadata
    if not req.include_not_applicable and md.get("applicability", "active") != "active":
        return False
    if req.chunk_types and c.chunk_type not in req.chunk_types:
        return False
    if req.only_tmd and not set(req.only_tmd) & set(md.get("tmd_ids") or []):
        return False
    scope = md.get("scope", "site_specific")
    return req.scope == "any" or scope == req.scope


def _boost(c: Chunk, req: SearchRequest) -> int:
    md, n = c.metadata, 0
    if req.tmd_ids and set(req.tmd_ids) & set(md.get("tmd_ids") or []):
        n += 1
    if req.roads and any(r.lower() in c.content.lower() for r in req.roads):
        n += 1
    if req.rps and set(req.rps) & {float(x) for x in md.get("rp_values") or []}:
        n += 1
    return n


_PART = re.compile(r":part-(\d+)$")


def _neighbours(c: Chunk) -> list[str]:
    """The parts just before and after a split chunk's part (they overlap it, so a list stays whole); a chunk that
    was split without part numbers brings all its siblings."""
    m = _PART.search(c.chunk_id)
    if not m:
        return list(c.siblings)
    n = int(m.group(1))
    return [s for s in c.siblings if (k := _PART.search(s)) and abs(int(k.group(1)) - n) == 1]


def rank_and_expand(fused: list[tuple[Chunk, float]], req: SearchRequest, all_chunks: list[Chunk], cfg: Config) -> list[Hit]:
    """Ranking rules and expansion (plan 5.7) over fused candidates, best first."""
    r = cfg.retrieval
    scored = [(c, s + r.detail_boost * _boost(c, req) / (r.rrf_k + 1)) for c, s in fused if s > 0]
    scored.sort(key=lambda cs: -cs[1])
    if req.scope == "any" and any(c.metadata.get("scope", "site_specific") != "generic" for c, _ in scored):
        scored = [(c, s) for c, s in scored if c.metadata.get("scope", "site_specific") != "generic"]
    seen_boiler: set[str] = set()
    hits: list[Hit] = []
    for c, s in scored:
        bh = c.metadata.get("boilerplate_hash") if c.metadata.get("is_boilerplate") else None
        if bh:
            if bh in seen_boiler:
                continue
            seen_boiler.add(bh)
        hits.append(Hit(c, s))
        if len(hits) >= req.k:
            break
    by_id = {c.chunk_id: c for c in all_chunks}
    have = {h.chunk.chunk_id for h in hits}
    extra: list[Hit] = []

    def add(cid: str | None, via: str, score: float) -> None:
        c = by_id.get(cid or "")
        if c is not None and c.chunk_id not in have and allowed(c, SearchRequest(req.doc_id, "", (), "any",
                                                                                   req.include_not_applicable)):
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
        if kw_rank and not req.broad:   # a chunk that shares one common word with the question is not worth a slot
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
