# SPDX-License-Identifier: Apache-2.0
"""``tally.init`` - one-line connect (CTO-260 §3).

``tally.init(key)`` installs one process-global :class:`~tally.client.TallyClient`, wires a
background batching transport (CTO-260 §5), boots the per-tenant HMAC key off-thread (CTO-260 §3.2),
and monkeypatches the official ``openai`` / ``anthropic`` clients (CTO-260 §4). It is:

- **Tenant-free.** The ingest key is tenant-bound at the gateway, so no ``tenant_id`` is needed; the
  tenant UUID comes back once from the bootstrap and is used only as a local hash-registry key,
  never sent on the wire (CTO-260 §3.1).
- **Idempotent.** A second call returns the same client and does not double-patch.
- **Non-blocking.** No network I/O runs on the calling thread; the HMAC bootstrap and transport run
  off-thread.
- **Never-raise.** A bad key, an unreachable gateway, or a missing provider library degrades to
  unattributed or disabled instrumentation with a one-time warning, never an exception into the
  caller.

Before the bootstrap completes (or if it fails), account ids land in the ``UNATTRIBUTED`` bucket
rather than raw on the wire (CTO-260 §3.3): LLM cost and model still flow, only the per-customer
dimension waits for the key.
"""

from __future__ import annotations

import logging
import os
import threading

from tally import context
from tally.client import TallyClient
from tally.hmac_keys import HmacKeyBootstrap, HmacKeyRegistry, RemoteKeyMaterialProvider
from tally.instrumentation.patch import patch_anthropic, patch_openai, unpatch_all
from tally.pricing import PriceCatalog, seed_catalog
from tally.safety import SelfObservability
from tally.transport import DEFAULT_ENDPOINT, BatchingTransport, fetch_hmac_key

_log = logging.getLogger("tally")

_lock = threading.RLock()
_client: TallyClient | None = None
_transport: BatchingTransport | None = None
_obs: SelfObservability | None = None
_key: str | None = None
_endpoint: str | None = None
_warned_uninit: set[str] = set()
_warned_bootstrap = False


def init(
    key: str | None = None,
    *,
    endpoint: str | None = None,
    feature_tag: str | None = None,
    instrument: bool = True,
    instrument_stream_usage: bool = False,
    flush_interval_s: float = 1.0,
    catalog: PriceCatalog | None = None,
) -> TallyClient:
    """Connect the process to ai-tally in one line. Idempotent, non-blocking, never raises.

    Args mirror the spec (CTO-260 §3). ``key`` falls back to ``TALLY_KEY``; ``endpoint`` to
    ``TALLY_ENDPOINT`` then the hosted default. Returns the process-global client (also usable
    directly, though the module-level ``record_*`` helpers make that unnecessary).
    """
    global _client, _transport, _obs, _key, _endpoint

    with _lock:
        if _client is not None:
            # Idempotent: a second init does not rebuild the client or double-patch.
            return _client

        obs = SelfObservability()
        _obs = obs
        try:
            resolved_key = key or os.environ.get("TALLY_KEY")
            resolved_endpoint = (
                endpoint or os.environ.get("TALLY_ENDPOINT") or DEFAULT_ENDPOINT
            )
            resolved_catalog = catalog or seed_catalog()

            if feature_tag:
                context.set_default_feature_tag(feature_tag)

            if not resolved_key:
                # No key: degrade to a disabled client (spans buffer nowhere) with one warning,
                # rather than raising. Instrumentation is skipped since it could not ship anyway.
                _log.warning(
                    "tally.init() called with no key (and no TALLY_KEY); telemetry disabled"
                )
                _client = TallyClient(catalog=resolved_catalog, observability=obs)
                return _client

            from tally import __version__

            transport = BatchingTransport(
                resolved_endpoint,
                resolved_key,
                sdk_version=__version__,
                observability=obs,
                flush_interval_s=flush_interval_s,
            )
            transport.start()

            client = TallyClient(
                api_key=resolved_key,
                endpoint=resolved_endpoint,
                exporter=transport,
                catalog=resolved_catalog,
                observability=obs,
            )

            _client = client
            _transport = transport
            _key = resolved_key
            _endpoint = resolved_endpoint

            # HMAC bootstrap off-thread: never block init (CTO-260 §3.2).
            threading.Thread(
                target=_bootstrap_hmac,
                args=(client, resolved_endpoint, resolved_key, obs),
                name="tally-hmac-bootstrap",
                daemon=True,
            ).start()

            if instrument:
                account_resolver = lambda: client._resolve_account(None, None)  # noqa: E731
                patch_openai(
                    on_span=client.ingest_span,
                    obs=obs,
                    catalog=resolved_catalog,
                    account_resolver=account_resolver,
                    instrument_stream_usage=instrument_stream_usage,
                )
                patch_anthropic(
                    on_span=client.ingest_span,
                    obs=obs,
                    catalog=resolved_catalog,
                    account_resolver=account_resolver,
                )

            return client
        except BaseException as exc:  # noqa: BLE001 - init must never raise into the caller
            obs.record_error(exc, "tally.init")
            if _client is None:
                _client = TallyClient(observability=obs)
            return _client


def _bootstrap_hmac(
    client: TallyClient, endpoint: str, key: str, obs: SelfObservability
) -> None:
    """Fetch the tenant's active HMAC material once and install a cached registry. Never raises.

    On any failure the account dimension stays unattributed (CTO-260 §3.3) - the raw id is never
    substituted, cost/model spans keep flowing, and only the per-customer tag waits for the key.
    """
    global _warned_bootstrap
    try:
        boot = fetch_hmac_key(endpoint, key)
        registry = _registry_from_bootstrap(boot, endpoint, key)
        # Mutating these two attributes is the whole handoff: _resolve_account reads them live, so
        # spans after this point carry the HMAC'd account, and ones before stay honest-blank.
        client.tenant_id = boot.tenant_id
        client.hmac_registry = registry
    except BaseException as exc:  # noqa: BLE001 - bootstrap failure must never crash the SDK
        obs.record_error(exc, "tally.hmac_bootstrap")
        if not _warned_bootstrap:
            _warned_bootstrap = True
            _log.warning(
                "tally: HMAC bootstrap failed (%s); accounts emitted unattributed", exc
            )


def _registry_from_bootstrap(
    boot: HmacKeyBootstrap, endpoint: str, key: str
) -> HmacKeyRegistry:
    """Build a registry backed by a TTL-cached remote provider, primed with the first fetch."""
    provider = RemoteKeyMaterialProvider(fetch=lambda: fetch_hmac_key(endpoint, key))
    # Prime the cache so hashing does not re-fetch until the TTL lapses.
    provider._cache[boot.key_version] = (provider._clock(), boot.material)
    registry = HmacKeyRegistry(provider=provider)
    registry.provision(boot.tenant_id, initial_version=boot.key_version)
    return registry


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
def flush(timeout: float = 5.0) -> None:
    """Drain buffered spans to the gateway synchronously (bounded). Safe no-op before init."""
    transport = _transport
    if transport is not None:
        transport.flush(timeout=timeout)


def uninstrument() -> None:
    """Reverse all provider patches and tear down the process-global client (CTO-260 §4.1).

    Tests call this between cases to avoid cross-test leakage; a subsequent :func:`init` starts
    fresh.
    """
    global _client, _transport, _obs, _key, _endpoint
    with _lock:
        unpatch_all()
        if _transport is not None:
            _transport.stop(timeout=2.0)
        _client = None
        _transport = None
        _obs = None
        _key = None
        _endpoint = None


def get_client() -> TallyClient | None:
    """The process-global client, or ``None`` before :func:`init`."""
    return _client


# --------------------------------------------------------------------------- #
# Module-level convenience: delegate to the process-global client (CTO-260 §3.1)
# --------------------------------------------------------------------------- #
def _warn_uninit(name: str) -> None:
    if name in _warned_uninit:
        return
    _warned_uninit.add(name)
    _log.warning("tally.%s() called before tally.init(); ignored", name)


def record_llm_call(**kwargs):
    """Record an LLM call via the process-global client. Safe no-op before ``init``."""
    client = _client
    if client is None:
        _warn_uninit("record_llm_call")
        return None
    return client.record_llm_call(**kwargs)


def record_tool_call(**kwargs):
    """Record a tool call via the process-global client. Safe no-op before ``init``."""
    client = _client
    if client is None:
        _warn_uninit("record_tool_call")
        return None
    return client.record_tool_call(**kwargs)


def record_vector_call(**kwargs):
    """Record a vector-DB call via the process-global client. Safe no-op before ``init``."""
    client = _client
    if client is None:
        _warn_uninit("record_vector_call")
        return None
    return client.record_vector_call(**kwargs)


def record_embedding_call(**kwargs):
    """Record an embedding call via the process-global client. Safe no-op before ``init``."""
    client = _client
    if client is None:
        _warn_uninit("record_embedding_call")
        return None
    return client.record_embedding_call(**kwargs)
