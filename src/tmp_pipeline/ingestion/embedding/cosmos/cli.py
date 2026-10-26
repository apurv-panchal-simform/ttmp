"""tmp-pipeline-cosmos: chunks and embeddings in Cosmos DB, and questions answered from there.

    tmp-pipeline-cosmos upload out/<doc_dir> [--no-embed]     embed (only new or changed chunks) and upload
    tmp-pipeline-cosmos ask [tmp-pipeline-ask arguments]       answer with Cosmos hybrid search
    tmp-pipeline-cosmos qa  [tmp-pipeline-qa arguments]        the question test set with Cosmos hybrid search

Needs AZURE_COSMOS_ENDPOINT/KEY and, for vectors, AZURE_OPENAI_ENDPOINT/KEY (``deploy/azure/provision.sh env``).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .... import tracing
from ....config import Config, load_config
from ....errors import PipelineError
from ....retrieval import cli as ask_cli
from ....retrieval import eval as questions
from ....retrieval.store import LocalStore
from ...chunking.model import ChunkSet
from ...chunking.summarise import fill_embedding_text
from ..search.azure_openai import AzureEmbeddings
from .store import CosmosChunks, CosmosSearch, open_container, upload


def backend(local: LocalStore, cfg: Config) -> tuple[LocalStore, CosmosSearch]:
    """Facts from the run folders, chunks and vectors from Cosmos."""
    container = open_container()
    chunks = CosmosChunks(container)
    return LocalStore(local.root, chunks_from=lambda doc_id: chunks.all(doc_id)), \
        CosmosSearch(cfg, container, AzureEmbeddings(), chunks)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["ask"]:
        return ask_cli.main(argv[1:], backend=backend)
    if argv[:1] == ["qa"]:
        return questions.main(argv[1:], backend=backend)
    ap = argparse.ArgumentParser(prog="tmp-pipeline-cosmos", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("upload", help="embed and upload one processed document's chunks")
    up.add_argument("doc_dir")
    up.add_argument("--no-embed", action="store_true", help="upload text only (keyword search still works)")
    sub.add_parser("ask", help="tmp-pipeline-ask with Cosmos search (same arguments)")
    sub.add_parser("qa", help="tmp-pipeline-qa with Cosmos search (same arguments)")
    a = ap.parse_args(argv)
    tracing.configure()            # exports spans to Langfuse only when LANGFUSE_PUBLIC_KEY/SECRET_KEY are set
    try:
        chunks = ChunkSet.load_jsonl(Path(a.doc_dir) / "chunks.jsonl")
        cfg = load_config()
        fill_embedding_text(chunks.chunks, cfg)
        res = upload(open_container(), chunks, None if a.no_embed else AzureEmbeddings(), dict(cfg.chunks.glossary))
    except (PipelineError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"{chunks.doc_id[:19]}…: uploaded {res['uploaded']} chunks, embedded {res['embedded']}, "
          f"reused {res['reused']} vectors, deleted {res['deleted']} stale, {res['without_vector']} without a vector")
    return 0
