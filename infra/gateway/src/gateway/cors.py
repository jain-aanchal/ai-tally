# SPDX-License-Identifier: Apache-2.0
"""Cross-origin policy for the gateway (CTO-337).

WHY this exists. Local `make up` puts the dashboard and the gateway behind one compose network and
the dashboard reaches the gateway from its OWN server process, so no browser ever performs a
cross-origin request and the gateway needed no CORS policy to work. The decided hosting shape
(`docs/aws-bundle-scope.md`) splits them: web on Vercel, gateway on ECS behind its own ALB, two
different origins. Nothing in the dashboard calls the gateway from the browser TODAY (every
`TALLY_GATEWAY_URL` fetch in `web/lib` runs in a route handler or a server action, and the variable
is deliberately not `NEXT_PUBLIC_`), so this is a policy the gateway is missing rather than a fire it
is currently on. It has to exist before the first browser-originated call, and it has to exist as an
ALLOWLIST from the start, because the moment a wildcard ships it is very hard to take back.

WHAT A CORS POLICY IS AND IS NOT. It is a browser-enforced rule about which page origins may READ a
response. It is not authentication and it is not CSRF protection: a browser can still SEND a simple
cross-origin POST regardless of what we answer here, and non-browser clients (the SDK, the edge
proxy, curl) ignore CORS entirely. Every endpoint stays gated by its own bearer key or the
control-plane service token; this module only decides whose page JavaScript may see the answer.

THE THREE DECISIONS, and why:

* **Allowlist, never ``*``.** The gateway holds per-tenant credentials and hands back HMAC key
  material on ``GET /v1/tenant/hmac-key``. A wildcard would let any page on the internet script a
  request from a visitor's browser and read the reply. :func:`resolve_allowed_origins` REFUSES a
  configured ``*`` at boot rather than honoring it.

* **Credentials mode off.** The gateway authenticates with an ``Authorization: Bearer`` header, not
  with cookies. ``allow_credentials`` governs cookies / TLS client certs / HTTP-auth, none of which
  this API uses, so turning it on would buy nothing and would make a future ``*`` (which the spec
  then forbids the browser from honoring, silently) look like it works. An explicitly listed
  ``authorization`` request header is what makes the bearer flow work, and that is unrelated to
  credentials mode.

* **A short preflight cache.** ``Access-Control-Max-Age`` caches a POSITIVE preflight in the browser
  per (origin, method, header set). Removing an origin from the allowlist therefore does not take
  effect in an already-warmed browser until the cached preflight expires, so this is a revocation
  delay, not just a performance knob. Ten minutes keeps the preflight off the hot path (one OPTIONS
  per browser per ten minutes, not per request) while bounding that delay; Chromium caps it at two
  hours and Firefox at 24, so a larger value would mostly buy a longer stale window.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import FastAPI

#: Origins allowed when nothing is configured. These are the `make up` / `npm run dev` dashboard
#: (RUNNING.md: dashboard on :3000), spelled both ways because a browser treats `localhost` and
#: `127.0.0.1` as different origins and operators reach for either. They are loopback-only, so a
#: deployment that forgets to configure the allowlist gets a policy that admits nobody on the
#: internet rather than one that admits everybody: wrong-by-default here fails closed.
DEFAULT_DEV_ORIGINS: tuple[str, ...] = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)

#: Methods the gateway actually routes (plus the preflight's own OPTIONS). Enumerated rather than
#: ``*`` so a new verb is a deliberate edit.
ALLOWED_METHODS: tuple[str, ...] = ("GET", "POST", "DELETE", "OPTIONS")

#: Request headers a browser caller may send. ``authorization`` carries the ingest key or the
#: control-plane service token; ``x-tenant-id`` / ``x-clerk-user-id`` are the control-plane's
#: resolved-tenant and audit headers; ``protocol-version`` is the ingest wire-version header.
#: ``stripe-signature`` is deliberately ABSENT: Stripe posts server-to-server and never from a page.
ALLOWED_HEADERS: tuple[str, ...] = (
    "authorization",
    "content-type",
    "protocol-version",
    "x-tenant-id",
    "x-clerk-user-id",
)

#: Seconds a browser may cache a successful preflight. See the module docstring: this is also how
#: long an origin removed from the allowlist can keep working in a warmed browser.
PREFLIGHT_MAX_AGE_S = 600


class CorsConfigError(ValueError):
    """The configured allowlist cannot be honored. Raised at boot, never at request time."""


def resolve_allowed_origins(settings: object) -> list[str]:
    """Resolve ``TALLY_CORS_ALLOWED_ORIGINS`` into the exact origin list to allow.

    Empty (the default) means :data:`DEFAULT_DEV_ORIGINS`, so `make up` keeps working with no
    configuration at all. Otherwise the value is a comma-separated list of origins, each of which
    must be a bare scheme://host[:port] with no path: browsers compare origins, not URLs, and a
    trailing path in the config would silently match nothing.

    ``*`` anywhere in the list is refused (see the module docstring), as is an empty explicit list.
    """
    raw = (getattr(settings, "cors_allowed_origins", "") or "").strip()
    if not raw:
        return list(DEFAULT_DEV_ORIGINS)

    origins: list[str] = []
    for piece in raw.split(","):
        origin = piece.strip()
        if not origin:
            continue
        if origin == "*" or "*" in origin:
            raise CorsConfigError(
                "TALLY_CORS_ALLOWED_ORIGINS must list exact origins, never a wildcard: the gateway "
                "serves per-tenant credentials and HMAC key material, so any page on the internet "
                f"could read them. Got {origin!r}."
            )
        if "://" not in origin:
            raise CorsConfigError(
                f"TALLY_CORS_ALLOWED_ORIGINS entry {origin!r} is not an origin: it needs a scheme, "
                "e.g. https://app.example.com"
            )
        scheme, _, host = origin.partition("://")
        if scheme not in ("http", "https"):
            raise CorsConfigError(
                f"TALLY_CORS_ALLOWED_ORIGINS entry {origin!r} must be http:// or https://"
            )
        if not host or "/" in host:
            raise CorsConfigError(
                f"TALLY_CORS_ALLOWED_ORIGINS entry {origin!r} must be a bare origin with no path: "
                "a browser sends scheme://host[:port] in the Origin header and nothing else"
            )
        if origin not in origins:
            origins.append(origin)

    if not origins:
        raise CorsConfigError(
            "TALLY_CORS_ALLOWED_ORIGINS was set but lists no origin. Leave it unset for the local "
            "dev default, or list the dashboard's origin."
        )
    return origins


def install_cors(app: "FastAPI", settings: object) -> list[str]:
    """Install the CORS policy on ``app`` and return the origins it allows.

    Kept as one call so the policy is described in exactly one place. A misconfiguration raises
    :class:`CorsConfigError` here, at import/boot, rather than producing a gateway that answers
    cross-origin requests in a way nobody intended.
    """
    from starlette.middleware.cors import CORSMiddleware

    origins = resolve_allowed_origins(settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        # See the module docstring: bearer header, not cookies. Off is the honest setting.
        allow_credentials=False,
        allow_methods=list(ALLOWED_METHODS),
        allow_headers=list(ALLOWED_HEADERS),
        max_age=PREFLIGHT_MAX_AGE_S,
    )
    return origins
