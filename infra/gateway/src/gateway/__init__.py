"""ai-tally ingest gateway.

Accepts SDK batches (the :mod:`tally.wire` envelope), recomputes cost authoritatively via
:mod:`tally.enrichment`, clamps clock skew via :mod:`tally.timekeeping`, dedupes idempotently, and
writes spans/business-events/identity-links into ClickHouse. A thin FastAPI service that reuses the
already-tested SDK logic — no Go required for the local stack.
"""

__version__ = "0.1.0"
