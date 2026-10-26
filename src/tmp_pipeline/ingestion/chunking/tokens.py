"""Token counting with the encoding of the embedding model (text-embedding-3-large uses ``cl100k_base``).

The encoding file ships with the package (``data/cl100k_base.tiktoken``, checked against tiktoken's pinned hash), so
counting never downloads anything and gives the same numbers offline, in tests and in production.
"""
from __future__ import annotations

import base64
import hashlib
import re
from functools import cache
from importlib import resources

import tiktoken

ENCODING = "cl100k_base"
_SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"     # tiktoken_ext.openai_public
_PATTERN = (r"""'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+| ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|"""
            r"""\s*[\r\n]|\s+(?!\S)|\s""")
_SPECIAL = {"<|endoftext|>": 100257, "<|fim_prefix|>": 100258, "<|fim_middle|>": 100259, "<|fim_suffix|>": 100260,
            "<|endofprompt|>": 100276}
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9•(\"'])|\n+")


@cache
def encoding() -> tiktoken.Encoding:
    raw = resources.files("tmp_pipeline.ingestion.chunking").joinpath("data", f"{ENCODING}.tiktoken").read_bytes()
    if hashlib.sha256(raw).hexdigest() != _SHA256:
        raise RuntimeError(f"bundled {ENCODING}.tiktoken does not match its pinned hash")
    ranks = {base64.b64decode(tok): int(rank) for tok, rank in (line.split() for line in raw.splitlines() if line)}
    return tiktoken.Encoding(ENCODING, pat_str=_PATTERN, mergeable_ranks=ranks, special_tokens=_SPECIAL)


def count(text: str) -> int:
    return len(encoding().encode(text, disallowed_special=()))


def sentences(text: str) -> list[str]:
    """Sentences and lines, in order, without empty pieces (the unit a split never cuts through)."""
    return [s.strip() for s in _SENTENCE.split(text) if s and s.strip()]
