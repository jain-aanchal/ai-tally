# SPDX-License-Identifier: Apache-2.0
"""Open the reviewed pull request (CTO-261 sections 4.3, 9).

Plain ``urllib`` against the REST API rather than a dependency or a shelled-out ``gh``:
the SDK runtime is dependency-free, and a hosted run should not need a CLI on the box.

Every request goes through :func:`github_request`, which checks the method and path against
the allowlist in :mod:`onboarding_bot.guards` first. Opening a PR is on that list. Merging
one is not, and cannot be reached from here: this module has no merge function to call, and
the transport would refuse the path if it did.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from .config import BotConfig, resolve_token
from .guards import SecurityViolation, assert_endpoint_allowed, redact

Transport = Callable[[str, str, dict[str, Any] | None, dict[str, str]], dict[str, Any]]
"""(method, url, payload, headers) -> decoded JSON. Injected in tests so the suite never
touches the network."""


def github_request(
    config: BotConfig,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    transport: Transport | None = None,
) -> dict[str, Any]:
    """Call one allowlisted GitHub endpoint with the scoped token."""
    assert_endpoint_allowed(method, path)
    token = resolve_token(config)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ai-tally-onboarding-bot",
    }
    url = f"{config.api_base.rstrip('/')}{path}"
    send = transport or _urllib_transport
    try:
        return send(method.upper(), url, payload, headers)
    except urllib.error.HTTPError as exc:  # pragma: no cover - network path
        body = redact(exc.read().decode("utf-8", errors="replace"), token)
        raise RuntimeError(f"GitHub {method.upper()} {path} failed ({exc.code}): {body}") from None


def _urllib_transport(
    method: str, url: str, payload: dict[str, Any] | None, headers: dict[str, str]
) -> dict[str, Any]:  # pragma: no cover - exercised only against the real API
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - https api_base
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def open_pull_request(
    config: BotConfig,
    *,
    head: str,
    base: str,
    title: str,
    body: str,
    transport: Transport | None = None,
) -> dict[str, Any]:
    """Open a PR from the bot's new branch. Never merges it, and never can (section 9).

    ``draft`` is not set: the PR is a normal reviewable PR the developer merges themselves.
    """
    if head.strip() == base.strip():
        raise SecurityViolation(
            f"refusing to open a PR from {head!r} onto itself; the bot always proposes from a "
            f"new branch (section 9)"
        )
    owner, name = config.owner_and_name
    payload: dict[str, Any] = {
        "title": title,
        "head": head,
        "base": base,
        "body": body,
        "maintainer_can_modify": True,
    }
    return github_request(
        config, "POST", f"/repos/{owner}/{name}/pulls", payload, transport=transport
    )
