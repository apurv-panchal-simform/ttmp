"""tmp-pipeline-index: the Azure AI Search index (plan 4.7).

    tmp-pipeline-index create [--index tmp-chunks-v1]
    tmp-pipeline-index push <doc_dir> [--index ...] [--embed-live]

``push`` reads ``chunks.jsonl`` and the vectors stored under ``vectors/``. With ``--embed-live`` the missing vectors are
made with Azure OpenAI (and cached for the next push); without it, a chunk with no stored vector is pushed without
one (it is still found by keyword and filters).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import tracing
from ..cache import CacheMiss, FileCache
from ..chunks.model import ChunkSet
from ..chunks.summarise import fill_embedding_text
from ..config import load_config
from ..errors import PipelineError
from ..model.embeddings import CachedEmbeddings
from .azure_openai import AzureEmbeddings
from .index import INDEX, SearchConfig, SearchIndex


def stored_vectors(doc_dir: Path, chunks: ChunkSet, live: bool) -> dict[str, list[float]]:
    """Vectors by each chunk's ``vector_key``; with ``live``, chunks without one are embedded first."""
    cache = FileCache(doc_dir / "vectors")
    if live:
        client = CachedEmbeddings(AzureEmbeddings(), cache)
        for c, v in zip(chunks.chunks, client.embed([c.embedding_text for c in chunks.chunks]), strict=True):
            c.vector_key = client.key(c.embedding_text) if v else None
        chunks.dump_jsonl(doc_dir / "chunks.jsonl")
    out = {}
    for c in chunks.chunks:
        if c.vector_key:
            try:
                out[c.vector_key] = cache.get(c.vector_key)["vector"]
            except CacheMiss:
                continue
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tmp-pipeline-index")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--index", default=INDEX)
    p = sub.add_parser("push")
    p.add_argument("doc_dir")
    p.add_argument("--index", default=INDEX)
    p.add_argument("--embed-live", action="store_true")
    a = ap.parse_args(argv)
    tracing.configure()            # exports spans to Langfuse only when LANGFUSE_PUBLIC_KEY/SECRET_KEY are set
    try:
        idx = SearchIndex(SearchConfig.from_env(a.index))
        if a.cmd == "create":
            idx.create()
            print(f"index {a.index} created or updated")
            return 0
        d = Path(a.doc_dir)
        chunks = ChunkSet.load_jsonl(d / "chunks.jsonl")
        fill_embedding_text(chunks.chunks, load_config())
        vectors = stored_vectors(d, chunks, a.embed_live)
        res = idx.push(chunks, vectors, dict(load_config().chunks.glossary))
        print(f"pushed {res['uploaded']} chunks ({len(vectors)} with vectors), deleted {res['deleted']}")
    except (PipelineError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
