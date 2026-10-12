"""PostgreSQL facts store (plan section 3.4), an optional extra (``uv sync --extra postgres``).

Nothing in the core pipeline imports this package (a test enforces it). It reads a finished run's ``facts.json``
and writes it to the tables in ``schema.sql``; the DSN comes from ``TMP_PG_DSN``. Until operations (phase 8) the
canonical snapshot is ``facts.json`` and the database is loaded from it, the reverse of the plan's final flow.
"""
