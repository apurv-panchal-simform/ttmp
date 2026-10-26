"""``tmp-pipeline-ui [--out out_live] [streamlit options]``: start the Streamlit app."""
from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    try:
        from streamlit.web import cli as stcli
    except ImportError:
        print("error: the UI needs the 'ui' extra: uv sync --extra ui", file=sys.stderr)
        return 2
    args = list(sys.argv[1:] if argv is None else argv)
    app_args: list[str] = []
    if "--out" in args:
        i = args.index("--out")
        app_args = ["--out", args[i + 1]]
        del args[i:i + 2]
    sys.argv = ["streamlit", "run", str(Path(__file__).with_name("app.py")), *args, "--", *app_args]
    return stcli.main()


if __name__ == "__main__":
    sys.exit(main())
