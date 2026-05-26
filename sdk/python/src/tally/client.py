"""TallyClient — the SDK entrypoint skeleton.

Minimal, safe-by-construction surface (CTO-45). Every public method runs inside the safety
boundary so a bug in the SDK — or in a pluggable exporter — never escapes into the customer's
code path. Real export/batching lands in CTO-49; for now spans accumulate in an in-memory sink so
the boundary behavior is observable and testable.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from tally.safety import SelfObservability, safe
from tally.schema import SpanFields, build_span_attributes


class Exporter(Protocol):
    """Pluggable span sink. Implementations may raise — the client must absorb it."""

    def export(self, attributes: dict[str, object]) -> None: ...


class MemoryExporter:
    """Default no-network exporter: keeps spans in a list. Useful for tests and local dev."""

    def __init__(self) -> None:
        self.spans: list[dict[str, object]] = []

    def export(self, attributes: dict[str, object]) -> None:
        self.spans.append(attributes)


class TallyClient:
    """Customer-facing entrypoint.

    Args:
        api_key: tenant-scoped key (unused until egress lands; stored for later).
        endpoint: ingest endpoint (unused until egress lands).
        exporter: pluggable sink; defaults to an in-memory exporter.
        observability: optional shared :class:`SelfObservability`.
    """

    def __init__(
        self,
        api_key: str | None = None,
        endpoint: str | None = None,
        *,
        exporter: Exporter | None = None,
        observability: SelfObservability | None = None,
    ) -> None:
        self.obs = observability or SelfObservability()
        self._api_key = api_key
        self._endpoint = endpoint
        self._exporter: Exporter = exporter or MemoryExporter()

    @property
    def observability(self) -> SelfObservability:
        return self.obs

    def record_span(self, fields: SpanFields) -> None:
        """Record one span. Never raises — a faulty exporter or builder is absorbed."""
        self._record_span_impl(fields)

    # Wrapped so any failure (build or export) is recorded and swallowed.
    def _record_span_impl(self, fields: SpanFields) -> None:
        @safe(self.obs, where="TallyClient.record_span")
        def _do() -> None:
            attrs = build_span_attributes(fields)
            self._exporter.export(attrs)

        _do()


def make_client(record_fn_factory: Callable[[], Exporter] | None = None) -> TallyClient:
    """Convenience constructor used in tests/examples."""
    return TallyClient(exporter=record_fn_factory() if record_fn_factory else None)
