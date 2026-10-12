"""tmp-pipeline-db: the PostgreSQL facts store (optional ``postgres`` extra; DSN in ``TMP_PG_DSN``).

    tmp-pipeline-db migrate
    tmp-pipeline-db write <doc_dir> [--access-group NAME ...]
"""
from __future__ import annotations

import argparse
import sys

from ..errors import PipelineError
from ..models import SchemaError
from .writer import connect, migrate, write_doc_dir


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tmp-pipeline-db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="create or update the schema")
    w = sub.add_parser("write", help="replace one document's rows from its facts.json")
    w.add_argument("doc_dir")
    w.add_argument("--access-group", action="append", default=[], help="group allowed to read the rows")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "migrate":
            with connect() as conn:
                print(f"schema version {migrate(conn)}")
        else:
            counts = write_doc_dir(a.doc_dir, access_groups=a.access_group)
            print(", ".join(f"{t} {n}" for t, n in counts.items() if n))
    except (PipelineError, SchemaError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
