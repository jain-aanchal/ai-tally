# SPDX-License-Identifier: Apache-2.0
"""Typed errors for the replay path (CTO-241).

Kept in a tiny standalone module on purpose: the exception's class identity must be STABLE across
an ``importlib.reload`` of :mod:`gateway.replay_store` (that module's optional-dependency tests
reload it to prove it imports without boto3 / google-cloud-storage). If ``ReplayBodyMissing`` were
defined inside ``replay_store`` itself, a reload would rebind it to a fresh class object, and the
``except ReplayBodyMissing`` clause in :mod:`gateway.replay_executor` (bound against the pre-reload
class) would silently stop catching it, turning a benign skip back into the uncaught 500 this ticket
set out to kill. Defining it here, in a module nothing reloads, keeps that identity fixed.
"""

from __future__ import annotations


class ReplayBodyMissing(KeyError):
    """A replay sample's INDEX row exists but its scrubbed BODY is absent from the blob store.

    CTO-241: the ClickHouse ``replay_samples`` index is durable and is re-hydrated on boot, but the
    body blobs are not always co-durable. In local dev the blob store is
    ``InMemoryReplayBlobStore`` (a plain in-process dict), so a gateway restart wipes every body
    while the index still points at them. A bare dict/``KeyError`` lookup then surfaced as an HTTP
    500 out of ``/v1/replay`` and blanked the Recoverable Cost page.

    A ``ReplayBlobStore.get_bytes`` raises this typed exception instead, so a missing body is a
    KNOWN, non-fatal condition the executor can skip over rather than an uncaught error. It
    subclasses ``KeyError`` so any legacy caller that caught the old bare-dict ``KeyError`` keeps
    working. ``key`` is the object key that missed (safe to log: it carries no body, only the
    deterministic ``tenants/.../{sample_id}.json`` path).
    """

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key
