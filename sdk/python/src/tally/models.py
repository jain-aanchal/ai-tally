# SPDX-License-Identifier: Apache-2.0
"""Model auto-discovery — fetch the live model lineup from provider APIs.

Implements CTO-109. Solves the "claude-3-5-haiku-latest got retired and broke the demo"
class of problem: callers ask for ``latest_anthropic("haiku")`` instead of naming a SKU,
and the resolver returns whatever the provider currently advertises as the cheapest
in-family model.

Discovery flow (see :func:`discover_models`):
    cache hit (fresh) → use it
    cache stale       → fetch live, save, return
    fetch fails       → use stale cache + warn
    no cache + fail   → return empty list, caller boots fail-soft

The SDK is dep-light (no ``httpx``/``requests``), so this module uses ``urllib.request``
just like ``examples/aider-demo/_emit_batch.py``.

API keys come from ``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY`` / ``GOOGLE_API_KEY`` (or
``GEMINI_API_KEY``). They are never logged.

Env overrides:
    ``TALLY_MODELS_REFRESH=1``      — bypass the cache TTL, always fetch live.
    ``TALLY_PINNED_MODELS=<path>``  — skip discovery entirely, load this file.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("tally.models")

# 24h TTL on the cached models file. Provider model lineups change in days, not seconds —
# refreshing on every boot would just spam their /v1/models endpoint.
CACHE_TTL_SECONDS = 24 * 60 * 60

DEFAULT_CACHE_PATH = Path(".tally/models.json")


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """A single model entry as returned by a provider's ``/v1/models`` endpoint."""

    provider: str  # "openai" | "anthropic" | "google"
    id: str  # canonical id, e.g. "claude-sonnet-4-5" or "gpt-4o-mini"
    family: str  # see classify_family()
    created_at: datetime | None
    deprecated_at: datetime | None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat() if self.created_at else None
        d["deprecated_at"] = self.deprecated_at.isoformat() if self.deprecated_at else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> ModelInfo:
        def _parse(v: str | None) -> datetime | None:
            if not v:
                return None
            return datetime.fromisoformat(v)

        return cls(
            provider=d["provider"],
            id=d["id"],
            family=d["family"],
            created_at=_parse(d.get("created_at")),
            deprecated_at=_parse(d.get("deprecated_at")),
        )


# Family classification regexes. Order matters: the first match wins, so the more-specific
# keywords (haiku/sonnet/opus/mini/embedding) sit ahead of the flagship catch-alls.
_FAMILY_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"haiku", re.I), "haiku"),
    (re.compile(r"sonnet", re.I), "sonnet"),
    (re.compile(r"opus", re.I), "opus"),
    (re.compile(r"(text-embedding|embedding)", re.I), "embedding"),
    # Google Gemini families (CTO-149). "flash" is the cheap/fast tier, "pro" the
    # flagship. These sit ahead of "mini"/flagship so a Gemini id never lands in an
    # OpenAI-shaped bucket.
    (re.compile(r"flash", re.I), "flash"),
    (re.compile(r"gemini.*pro|pro.*gemini", re.I), "pro"),
    # Non-chat OpenAI SKUs. `GET /v1/models` lists audio / realtime / tts / transcribe /
    # image / moderation / instruct / search / legacy-base models, and many carry a chat
    # keyword in their id (e.g. "gpt-4o-mini-tts", "gpt-4o-realtime-preview"). Without this
    # guard they'd fall through to the "mini"/"flagship" buckets below, and resolveLatest
    # could hand one to the chat-completions path — which 404s with "not a chat model".
    # Route them to "other" so a chat family only ever contains chat-capable models. This
    # sits AFTER the embedding rule (embeddings are a real cost family) and BEFORE the
    # OpenAI chat buckets. o1/o3/o4 reasoning "mini"s are chat-capable, so they're not listed.
    (
        re.compile(
            r"(audio|realtime|transcribe|whisper|tts|moderation|dall-e|image|"
            r"instruct|search-preview|davinci|babbage|computer-use|codex)",
            re.I,
        ),
        "other",
    ),
    (re.compile(r"mini", re.I), "mini"),
    (re.compile(r"^gpt-4o(?!-mini)", re.I), "flagship"),
    (re.compile(r"^gpt-5(?!-mini)", re.I), "flagship"),
]


def classify_family(model_id: str) -> str:
    """Bucket a model id into a coarse capability/price family.

    The buckets are deliberately coarse — they're consumer-facing labels ("the current
    cheapest Claude") not a faithful model taxonomy. Anything that doesn't match one
    of the well-known keywords lands in ``"other"`` (legacy GPT-3.5, custom fine-tunes, etc).
    """
    for pattern, family in _FAMILY_RULES:
        if pattern.search(model_id):
            return family
    return "other"


def _parse_unix_or_iso(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _http_get_json(url: str, headers: dict[str, str], timeout: float) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - HTTPS only
        return json.loads(resp.read().decode("utf-8"))


def fetch_openai_models(api_key: str, *, timeout: float = 5.0) -> list[ModelInfo]:
    """Hit OpenAI's ``GET /v1/models`` and return the parsed lineup.

    OpenAI returns ``{"data": [{"id": ..., "created": <unix-ts>}, ...]}`` — there's no
    explicit "deprecated_at" field, so we leave it ``None``.
    """
    payload = _http_get_json(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
    )
    out: list[ModelInfo] = []
    for row in payload.get("data", []):
        model_id = row.get("id")
        if not model_id:
            continue
        out.append(
            ModelInfo(
                provider="openai",
                id=model_id,
                family=classify_family(model_id),
                created_at=_parse_unix_or_iso(row.get("created")),
                deprecated_at=None,
            )
        )
    return out


def fetch_anthropic_models(api_key: str, *, timeout: float = 5.0) -> list[ModelInfo]:
    """Hit Anthropic's ``GET /v1/models`` and return the parsed lineup.

    Anthropic returns ``{"data": [{"id": ..., "created_at": <iso8601>, ...}]}``. The
    API requires the ``anthropic-version`` header — without it the request 400s.
    """
    payload = _http_get_json(
        "https://api.anthropic.com/v1/models",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        timeout=timeout,
    )
    out: list[ModelInfo] = []
    for row in payload.get("data", []):
        model_id = row.get("id")
        if not model_id:
            continue
        out.append(
            ModelInfo(
                provider="anthropic",
                id=model_id,
                family=classify_family(model_id),
                created_at=_parse_unix_or_iso(row.get("created_at")),
                deprecated_at=_parse_unix_or_iso(row.get("deprecated_at")),
            )
        )
    return out


def fetch_google_models(api_key: str, *, timeout: float = 5.0) -> list[ModelInfo]:
    """Hit Google's Gemini ``GET /v1beta/models`` and return the parsed lineup.

    The Gemini API returns ``{"models": [{"name": "models/gemini-2.5-flash", ...}]}`` — the
    id lives under ``name`` with a ``models/`` prefix that we strip to the bare id (so it
    matches the catalog + Compare ``gemini-*`` slugs). The key is passed as the ``?key=``
    query param (Gemini's convention), not a bearer header. There's no per-model created/
    deprecated timestamp in the list response, so both are left ``None``.

    Parsing is deliberately tolerant: the Vertex/Gemini list shape has shifted across API
    versions, so anything without a usable id is skipped rather than raising.
    """
    payload = _http_get_json(
        f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}",
        headers={},
        timeout=timeout,
    )
    out: list[ModelInfo] = []
    for row in payload.get("models", []):
        raw_id = row.get("name") or row.get("id")
        if not raw_id or not isinstance(raw_id, str):
            continue
        # "models/gemini-2.5-flash" -> "gemini-2.5-flash"
        model_id = raw_id.split("/", 1)[-1] if raw_id.startswith("models/") else raw_id
        if not model_id:
            continue
        out.append(
            ModelInfo(
                provider="google",
                id=model_id,
                family=classify_family(model_id),
                created_at=None,
                deprecated_at=None,
            )
        )
    return out


def save_cache(models: list[ModelInfo], path: Path = DEFAULT_CACHE_PATH) -> None:
    """Persist the discovered list to ``path`` (creating parent dirs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([m.to_dict() for m in models], indent=2))


def load_cached(path: Path = DEFAULT_CACHE_PATH) -> list[ModelInfo] | None:
    """Return the cached list iff the file exists and is younger than the TTL.

    Uses file mtime vs ``time.time()`` rather than parsing any "fetched_at" inside the
    JSON — saves a round-trip through the body and means a manual ``touch`` is enough
    to extend the TTL.
    """
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > CACHE_TTL_SECONDS:
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return [ModelInfo.from_dict(d) for d in raw]


def _load_unchecked(path: Path) -> list[ModelInfo] | None:
    """Read the cache file without applying the TTL — for fail-soft fallback."""
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return [ModelInfo.from_dict(d) for d in raw]


# Models whose id ends in a date stamp like "-20251015" are point-in-time pins; the
# undated id (e.g. "claude-sonnet-4-5") is the moving alias provider docs recommend.
_DATE_SUFFIX = re.compile(r"-\d{8}$")


def latest(provider: str, family: str, models: list[ModelInfo]) -> ModelInfo | None:
    """Pick the current best model in ``(provider, family)``.

    Rules, in order:
        1. Skip anything marked ``deprecated_at`` — these are sunset.
        2. Prefer ids *without* a date suffix (the moving alias beats the pinned snapshot).
        3. Within each preference tier, sort by ``created_at`` descending so newer wins.
    """
    candidates = [
        m
        for m in models
        if m.provider == provider and m.family == family and m.deprecated_at is None
    ]
    if not candidates:
        return None

    def sort_key(m: ModelInfo) -> tuple[int, float]:
        is_dated = 1 if _DATE_SUFFIX.search(m.id) else 0  # undated (0) sorts before dated (1)
        # Higher created_at first → negate.
        ts = -m.created_at.timestamp() if m.created_at else 0.0
        return (is_dated, ts)

    candidates.sort(key=sort_key)
    return candidates[0]


def latest_anthropic(family: str, models: list[ModelInfo]) -> ModelInfo | None:
    return latest("anthropic", family, models)


def latest_openai(family: str, models: list[ModelInfo]) -> ModelInfo | None:
    return latest("openai", family, models)


def discover_models(
    *,
    cache_path: Path = DEFAULT_CACHE_PATH,
    openai_api_key: str | None = None,
    anthropic_api_key: str | None = None,
    google_api_key: str | None = None,
    timeout: float = 5.0,
) -> list[ModelInfo]:
    """Boot-time discovery entry point. Fail-soft on every error path.

    Honors:
        - ``TALLY_PINNED_MODELS`` → load that file verbatim, skip the network.
        - ``TALLY_MODELS_REFRESH=1`` → ignore the cache TTL and refetch live.

    The gateway's lifespan calls this on startup; the demos read the saved cache directly.
    """
    pinned = os.environ.get("TALLY_PINNED_MODELS")
    if pinned:
        pinned_path = Path(pinned)
        loaded = _load_unchecked(pinned_path)
        if loaded is not None:
            logger.info("models: loaded pinned list from %s (%d entries)", pinned_path, len(loaded))
            return loaded
        logger.warning("models: TALLY_PINNED_MODELS=%s could not be loaded", pinned_path)

    force_refresh = os.environ.get("TALLY_MODELS_REFRESH") == "1"
    if not force_refresh:
        cached = load_cached(cache_path)
        if cached is not None:
            logger.info("models: cache hit (%d entries)", len(cached))
            return cached

    openai_key = openai_api_key or os.environ.get("OPENAI_API_KEY")
    anthropic_key = anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
    google_key = (
        google_api_key or os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    )

    discovered: list[ModelInfo] = []
    openai_err: Exception | None = None
    anthropic_err: Exception | None = None
    google_err: Exception | None = None

    if openai_key:
        try:
            discovered.extend(fetch_openai_models(openai_key, timeout=timeout))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            openai_err = exc
            logger.warning("models: openai fetch failed: %s", exc)
    else:
        logger.info("models: OPENAI_API_KEY not set — skipping openai discovery")

    if anthropic_key:
        try:
            discovered.extend(fetch_anthropic_models(anthropic_key, timeout=timeout))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            anthropic_err = exc
            logger.warning("models: anthropic fetch failed: %s", exc)
    else:
        logger.info("models: ANTHROPIC_API_KEY not set — skipping anthropic discovery")

    if google_key:
        try:
            discovered.extend(fetch_google_models(google_key, timeout=timeout))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            google_err = exc
            logger.warning("models: google fetch failed: %s", exc)
        except (KeyError, TypeError, ValueError) as exc:
            # Defensive: the Gemini/Vertex list shape has drifted across versions.
            # A malformed body should skip Google, not crash discovery.
            google_err = exc
            logger.warning("models: google parse failed: %s", exc)
    else:
        logger.info("models: GOOGLE_API_KEY/GEMINI_API_KEY not set — skipping google discovery")

    if discovered:
        try:
            save_cache(discovered, cache_path)
        except OSError as exc:
            logger.warning("models: could not write cache to %s: %s", cache_path, exc)
        _log_summary(discovered)
        return discovered

    # Both providers unreachable (or unconfigured) — fall back to whatever's on disk,
    # even if stale. Boot must not crash just because a /v1/models call timed out.
    stale = _load_unchecked(cache_path)
    if stale is not None:
        logger.warning(
            "models: live fetch failed (openai=%s anthropic=%s google=%s) — "
            "using stale cache (%d entries)",
            openai_err,
            anthropic_err,
            google_err,
            len(stale),
        )
        return stale

    logger.warning(
        "models: no live data and no cache (openai=%s anthropic=%s google=%s) — "
        "booting with empty list",
        openai_err,
        anthropic_err,
        google_err,
    )
    return []


def _log_summary(models: list[ModelInfo]) -> None:
    openai_ids = sorted(m.id for m in models if m.provider == "openai")
    anth_ids = sorted(m.id for m in models if m.provider == "anthropic")
    google_ids = sorted(m.id for m in models if m.provider == "google")
    logger.info("models: openai=%s anthropic=%s google=%s", openai_ids, anth_ids, google_ids)


__all__ = [
    "ModelInfo",
    "classify_family",
    "fetch_openai_models",
    "fetch_anthropic_models",
    "fetch_google_models",
    "save_cache",
    "load_cached",
    "latest",
    "latest_anthropic",
    "latest_openai",
    "discover_models",
    "CACHE_TTL_SECONDS",
    "DEFAULT_CACHE_PATH",
]
