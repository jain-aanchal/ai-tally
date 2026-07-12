"""Cloud-billing connectors (CTO-143).

A small reusable skeleton for daily connectors that pull cloud spend from a provider's billing API
and land it as synthetic cost-layer spans in ``otel_spans`` — populating a cost layer that no live
telemetry feeds today. CTO-143 ships the ``compute`` layer (AWS Cost Explorer / GCP Cloud Billing);
CTO-144 (egress) subclasses the same base with ``operation='egress'``.

The base (:mod:`gateway.connectors.base`) is operation-agnostic on purpose: everything provider- or
operation-specific is injected (the billing client, the ``operation`` literal), so the run contract,
the synthetic-span emitter, and run-status recording are shared verbatim.
"""

from gateway.connectors.base import (
    BillingClient,
    CloudBillingConnector,
    ConnectorConfig,
    DailyCost,
    RunResult,
    synthetic_span_id,
)

__all__ = [
    "BillingClient",
    "CloudBillingConnector",
    "ConnectorConfig",
    "DailyCost",
    "RunResult",
    "synthetic_span_id",
]
