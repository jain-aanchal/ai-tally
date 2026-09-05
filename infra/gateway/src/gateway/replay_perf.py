# SPDX-License-Identifier: Apache-2.0
"""Performance primitives for the replay + eval hot path (CTO-243).

The Compare and Recoverable-Cost pages are the only surfaces that reach the gateway's
``/v1/replay`` + ``/v1/eval`` LLM-judge path; every other page is a plain ClickHouse read. Three
accidental costs made that path degrade the longer the process ran, and could wedge the single
event loop so unrelated reads (the Home budget) timed out:

* ``/v1/replay`` appended a run row per (sample, candidate) to an UNBOUNDED in-memory list on every
  call, and ``/v1/eval`` then re-judged EVERY accumulated run for the selected samples. A nominal
  50-sample request grew to judging 800+ pairs. :class:`ReplayRunStore` fixes this at the source:
  runs are keyed by (tenant, sample, candidate), newest wins, so a repeat replay REPLACES the prior
  run instead of stacking, and eval judges exactly one pair per selected sample.
* Every judged pair did a blocking blob fetch (MinIO/S3) with no cache. :class:`EnvelopeCache` plus
  :func:`prime_envelopes` load the DISTINCT bodies once, off the event loop, so the judge loop never
  blocks on I/O.
* Even with the web layer's own cache, a web restart forced a full gateway recompute.
  :class:`ProjectionCache` memoizes the finished projection per (tenant, feature, params) against a
  cheap corpus signature, so a warm result is returned instantly and survives web restarts.

All three are pure and in-process. Nothing here touches telemetry, bodies, or money math.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Hashable, Iterator

# Safety cap: even with per-key dedup, bound how many run rows one tenant can hold in memory so a
# very large corpus cannot grow the process without limit. Newest-by-``ran_at`` are kept.
REPLAY_RUNS_PER_TENANT_CAP = 2000
# Distinct scrubbed envelopes cached in memory. Bodies are small JSON; a few thousand is cheap and
# covers the whole selected corpus of every feature on a demo tenant.
ENVELOPE_CACHE_MAX = 4096
# How long a memoized projection stays fresh before it is recomputed even when the corpus signature
# is unchanged. Short enough that config/pricing changes surface quickly.
PROJECTION_CACHE_TTL_S = 15 * 60


class ReplayRunStore:
    """Deduped, per-tenant-bounded store of replay run rows.

    Drop-in for the plain ``list`` the sink used to append to: it exposes ``append`` (an UPSERT keyed
    by (tenant, sample, candidate), newest wins) and iterates as a list, which is all the eval reader
    and the today's-spend sum need. This is the single change that stops eval's workload from growing
    without bound as the app is used.
    """

    def __init__(self, per_tenant_cap: int = REPLAY_RUNS_PER_TENANT_CAP) -> None:
        self._by_key: "OrderedDict[tuple[str, str, str, str], Any]" = OrderedDict()
        self._cap = per_tenant_cap

    @staticmethod
    def _key(row: Any) -> tuple[str, str, str, str]:
        return (row.tenant_id, str(row.sample_id), row.candidate_provider, row.candidate_model)

    def append(self, row: Any) -> None:
        key = self._key(row)
        # Newest wins: drop any prior run for this exact pair, re-insert at the end (most-recent).
        self._by_key.pop(key, None)
        self._by_key[key] = row
        self._trim_tenant(row.tenant_id)

    def _trim_tenant(self, tenant_id: str) -> None:
        # Fast path: a per-key upsert cannot push a tenant over its cap unless the whole store already
        # holds at least cap rows, so skip the O(n) scan until then. When it does run, it evicts that
        # tenant's oldest rows (insertion order tracks ``ran_at`` because rows are appended as they run).
        if len(self._by_key) <= self._cap:
            return
        keys = [k for k in self._by_key if k[0] == tenant_id]
        excess = len(keys) - self._cap
        for k in keys[:excess] if excess > 0 else ():
            self._by_key.pop(k, None)

    def clear(self) -> None:
        self._by_key.clear()

    def __iter__(self) -> Iterator[Any]:
        return iter(list(self._by_key.values()))

    def __len__(self) -> int:
        return len(self._by_key)


class EnvelopeCache:
    """Thread-safe LRU of parsed replay envelopes keyed by blob object key.

    The judge path hits the same bodies repeatedly (every eval pass over a feature) and one body can
    back several candidates. Caching the parsed dict turns ~800 blocking blob GETs per feature into a
    handful of misses. Thread-safe because :func:`prime_envelopes` fills it from a worker thread.
    """

    def __init__(self, max_size: int = ENVELOPE_CACHE_MAX) -> None:
        self._data: "OrderedDict[str, dict]" = OrderedDict()
        self._max = max_size
        self._lock = threading.Lock()

    def get(self, key: str) -> dict | None:
        with self._lock:
            val = self._data.get(key)
            if val is not None:
                self._data.move_to_end(key)
            return val

    def put(self, key: str, value: dict) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


def prime_envelopes(
    cache: EnvelopeCache,
    object_keys: set[str],
    loader: Callable[[str], dict],
) -> None:
    """Load every not-yet-cached object key through ``loader`` into ``cache``.

    Blocking (blob I/O); call it via ``asyncio.to_thread`` so the event loop stays free. A load that
    raises is skipped, not fatal: the judge path already tolerates a missing/blank envelope, so one
    unreadable body must not sink the whole pass.
    """
    for key in object_keys:
        if cache.get(key) is not None:
            continue
        try:
            cache.put(key, loader(key))
        except Exception:  # noqa: BLE001 - a bad body is skipped, never fatal to the pass
            continue


class ProjectionCache:
    """TTL + corpus-signature memo for finished ``/v1/replay`` and ``/v1/eval`` projections.

    A cached entry is served only when BOTH its signature (a cheap fingerprint of the inputs, e.g.
    corpus size) matches the current one AND it is within ``ttl_s``. That keeps a warm result instant
    and restart-independent (on the web side) while still recomputing the moment the corpus or config
    that feeds it changes.
    """

    def __init__(self, ttl_s: float = PROJECTION_CACHE_TTL_S) -> None:
        self._data: dict[Hashable, tuple[float, str, dict]] = {}
        self._ttl = ttl_s
        self._lock = threading.Lock()

    def get(self, key: Hashable, signature: str) -> dict | None:
        with self._lock:
            hit = self._data.get(key)
        if hit is None:
            return None
        at, sig, value = hit
        if sig != signature or (time.monotonic() - at) > self._ttl:
            return None
        return value

    def put(self, key: Hashable, signature: str, value: dict) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), signature, value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


def candidates_key(candidates: list[dict]) -> tuple[tuple[str, str], ...]:
    """Order-independent key for a candidate-model set, for use in a projection cache key."""
    return tuple(sorted((str(c.get("provider", "")), str(c.get("model", ""))) for c in candidates))
