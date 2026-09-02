# SPDX-License-Identifier: Apache-2.0
"""``hash_account`` helper + ``python -m tally.hash_account`` CLI (CTO-260 §7).

The proxy path holds no HMAC key and will not hash a raw customer id for you (that would route the
raw id through ai-tally, which is exactly what the hash prevents). This helper computes the account
hash the same way the SDK does, on the caller's own machine: it uses the tenant HMAC key fetched via
the bootstrap (CTO-260 §3.2) under the org's ingest key, so a proxy user can send a pre-hashed
``X-Tally-Account-Id-Hash`` and still get per-customer attribution.

If :func:`tally.init` has already run and bootstrapped, this reuses that in-process key. Otherwise
it performs a one-off synchronous bootstrap from ``key`` / ``TALLY_KEY`` (this helper is not on the
customer hot path, so a blocking fetch here is acceptable, unlike ``init``).
"""

from __future__ import annotations

import argparse
import os
import sys

from tally.hmac_keys import HmacKeyRegistry, RemoteKeyMaterialProvider
from tally.transport import DEFAULT_ENDPOINT, fetch_hmac_key


def hash_account(account_id: str, *, key: str | None = None, endpoint: str | None = None) -> str:
    """Return the HMAC-SHA256 hex of ``account_id`` under the tenant's active key.

    Reuses the process-global registry from :func:`tally.init` when available; else bootstraps once
    from ``key`` (or ``TALLY_KEY``) and ``endpoint`` (or ``TALLY_ENDPOINT``). Raises ``ValueError``
    when no key is available or the id is empty, and propagates a bootstrap/network error to the
    caller: this helper is a deliberate, explicit call, not the never-raise customer hot path.
    """
    if not account_id or not account_id.strip():
        raise ValueError("account_id must be non-empty")

    registry, tenant_id = _resolve_registry(key, endpoint)
    return registry.hash_account(tenant_id, account_id.strip()).value


def _resolve_registry(
    key: str | None, endpoint: str | None
) -> tuple[HmacKeyRegistry, str]:
    # Prefer an already-bootstrapped in-process registry (no second fetch).
    from tally import init as _init

    client = _init.get_client()
    if client is not None and client.hmac_registry is not None and client.tenant_id:
        return client.hmac_registry, client.tenant_id

    resolved_key = key or os.environ.get("TALLY_KEY")
    if not resolved_key:
        raise ValueError("no ingest key: pass key= or set TALLY_KEY")
    resolved_endpoint = endpoint or os.environ.get("TALLY_ENDPOINT") or DEFAULT_ENDPOINT

    boot = fetch_hmac_key(resolved_endpoint, resolved_key)
    provider = RemoteKeyMaterialProvider(
        fetch=lambda: fetch_hmac_key(resolved_endpoint, resolved_key)
    )
    provider._cache[boot.key_version] = (provider._clock(), boot.material)
    registry = HmacKeyRegistry(provider=provider)
    registry.provision(boot.tenant_id, initial_version=boot.key_version)
    return registry, boot.tenant_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tally.hash_account",
        description="Compute the ai-tally account hash for a raw account id, on your own machine.",
    )
    parser.add_argument("account_id", help="raw account id, e.g. acct_northwind")
    parser.add_argument("--key", default=None, help="ingest key (else TALLY_KEY)")
    parser.add_argument("--endpoint", default=None, help="ingest base URL (else TALLY_ENDPOINT)")
    args = parser.parse_args(argv)

    try:
        print(hash_account(args.account_id, key=args.key, endpoint=args.endpoint))
    except Exception as exc:  # noqa: BLE001 - CLI surfaces the error plainly and exits non-zero
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
