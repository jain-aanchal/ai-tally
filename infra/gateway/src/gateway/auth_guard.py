# SPDX-License-Identifier: Apache-2.0
"""Boot-time guard on the gateway's authentication escape hatch (CTO-268, gateway half).

WHY this file exists. ``TALLY_REQUIRE_API_KEY=false`` is one environment variable that turns
authentication OFF for the whole gateway: ``/v1/batches`` accepts whatever ``tenant_id`` the caller
puts in the body, and the control-plane service-token gate on ``/v1/tenant/*`` goes quiet too,
because that gate is active only when auth is ON and a token is configured (see
``app._require_service_token``). Anyone who can reach the port can then write spans against any
tenant and read and rewrite any tenant's control-plane configuration. That is correct and wanted for
``make up``, for the test suite and for a laptop; it is a catastrophe on a reachable deployment.

The web tier got this guard first (``web/lib/authGuard.ts``, PR #345) and its author deferred the
gateway half for a stated reason: ``gateway/config.py`` had no notion of a deployment environment,
so there was nothing to test ``TALLY_REQUIRE_API_KEY`` against, and introducing that notion is a
design decision rather than a line of code. This module makes that decision.

THE DECISION, and the honest limitation in it. The web tier gets ``NODE_ENV=production`` for free:
``next start`` and the standalone server set it, so the guard fires without an operator doing
anything. A Python process has no equivalent signal, and there is nothing in the gateway's existing
configuration that reliably means "this is a real deployment" (a Postgres DSN pointing at a remote
host is a guess, and guessing is exactly what the honest-under-uncertainty invariant forbids). So
this introduces an explicit one, ``TALLY_ENV``, defaulting to ``development``:

* ``development`` / ``dev`` / ``local`` / ``test``: no guard. ``make up``, CI and pytest are
  unaffected and need no new variable.
* ``production`` / ``prod`` / ``staging`` / ``stage``: auth must be on, OR the operator must say
  ``TALLY_ALLOW_INSECURE_NO_AUTH=1`` and live with a warning banner on every boot.
* anything else: treated as a deployment, because an unrecognized value is far more likely to be a
  typo in a deployment manifest than a laptop, and the fail-closed direction is the safe one.

The limitation is that an operator who never sets ``TALLY_ENV`` is not protected. That is real, and
it is why the deployment manifests must set it (``deploy/aws/README.md`` says so, and the ECS task
definition already sets ``TALLY_REQUIRE_API_KEY=true`` so the guard is a backstop there, not the
thing that saves it). A guard that defaulted the other way would refuse to start every developer's
``make up`` and every CI run, which is how guards get deleted.

WHY THIS DOES NOT BREAK THE AUTH-DISABLED SHAPE. ``infra/edge-proxy/README.md`` documents
``EDGE_PROXY_TENANT_ID`` as "Required when the gateway runs with auth disabled", so an auth-disabled
gateway is a supported configuration somewhere and must keep working. It does: it is untouched in
every development environment, and it remains available in a deployment behind the same explicit
opt-in the web tier introduced. We deliberately reuse ``TALLY_ALLOW_INSECURE_NO_AUTH`` rather than
inventing a second spelling, so one operator sentence covers both tiers of one deployment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Env keys this guard reads. Named once so the message and the checks cannot drift.
ENV_VAR = "TALLY_ENV"
REQUIRE_API_KEY_ENV = "TALLY_REQUIRE_API_KEY"
ALLOW_INSECURE_ENV = "TALLY_ALLOW_INSECURE_NO_AUTH"

#: Environment names that mean "a laptop, CI, or the test suite": the guard stands down.
LOCAL_ENVIRONMENTS: frozenset[str] = frozenset({"development", "dev", "local", "test", "ci"})

#: Environment names that mean "a real, reachable deployment". Anything unrecognized joins them.
DEPLOYED_ENVIRONMENTS: frozenset[str] = frozenset({"production", "prod", "staging", "stage"})


class InsecureAuthConfigError(RuntimeError):
    """Auth is off in a deployed environment with no explicit opt-in. Raised at boot."""


def is_truthy(value: object) -> bool:
    """Same truthiness spelling ``web/lib/authGuard.ts`` and ``deploy/demo/lib-tenant.sh`` accept."""
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return False
    return value.strip().lower() in ("1", "true", "yes", "on")


def is_deployed_environment(env: object) -> bool:
    """Whether ``TALLY_ENV`` names a real deployment. Unrecognized values fail closed (deployed)."""
    name = (env if isinstance(env, str) else "").strip().lower()
    if not name:
        # Empty is the default and means "unset", which is a laptop until someone says otherwise.
        return False
    return name not in LOCAL_ENVIRONMENTS


@dataclass(frozen=True, slots=True)
class AuthGuardVerdict:
    """What the guard decided. ``kind`` is one of ``ok`` / ``insecure-allowed`` / ``refuse``."""

    kind: str
    message: str = ""


def _refusal_message(env_name: str) -> str:
    return f"""
================================================================================
ai-tally gateway REFUSES TO START: authentication is disabled in {ENV_VAR}={env_name}.

{REQUIRE_API_KEY_ENV} is false, which turns authentication OFF for the whole
gateway, not just for ingest:

  * POST /v1/batches trusts the tenant_id in the request body, so any caller can
    write spans, cost and business events against ANY tenant.
  * The control-plane service-token gate on /v1/tenant/* is inert (it is enforced
    only when auth is on), so any caller can read and rewrite any tenant's
    connectors, budgets, guardrails and ingest keys.

There is no safe fallback here, so this process stops instead of serving.

You are in ONE OF TWO situations:

  1. You want a REAL deployment. This is the normal case.

         {REQUIRE_API_KEY_ENV}=true
         TALLY_GATEWAY_SERVICE_TOKEN=<a long random server-only secret>

     The service token authenticates the WEB SERVER on /v1/tenant/* calls; the
     gateway already refuses to boot with auth on and that token empty.
     deploy/aws/ecs/gateway.taskdef.json sets both.

  2. You genuinely want an OPEN gateway, the way a laptop or the public demo runs.

     Then say so, explicitly:

         {ALLOW_INSECURE_ENV}=1

     Only do this on a port nobody else can reach, or behind access control you
     supply yourself. Note that the edge proxy's EDGE_PROXY_TENANT_ID exists for
     exactly this shape (infra/edge-proxy/README.md), and it keeps working.

     Or set {ENV_VAR}=development, which is what a laptop is.

Checked: {ENV_VAR}={env_name}, {REQUIRE_API_KEY_ENV}=false, {ALLOW_INSECURE_ENV} not set.
================================================================================
""".strip()


def _insecure_warning(env_name: str) -> str:
    return f"""
================================================================================
ai-tally gateway WARNING: serving with NO AUTHENTICATION.

{ENV_VAR}={env_name} and {REQUIRE_API_KEY_ENV} is false, with
{ALLOW_INSECURE_ENV} set, so this gateway starts wide open: ingest trusts the
tenant_id in the body and the control plane is ungated.

This is a deliberate configuration. If you did not mean it, set
{REQUIRE_API_KEY_ENV}=true and give the gateway a service token.
================================================================================
""".strip()


def check_auth_config(
    *, env: object, require_api_key: object, allow_insecure: object
) -> AuthGuardVerdict:
    """Decide whether this process may serve. Pure, so the decision table is testable.

    ``assert_auth_config`` is the one that stops the boot; this is separated out so tests and the
    allowed-but-insecure log path can inspect the decision without catching an exception.
    """
    if require_api_key is True or is_truthy(require_api_key):
        return AuthGuardVerdict("ok")
    if not is_deployed_environment(env):
        return AuthGuardVerdict("ok")
    env_name = (env if isinstance(env, str) else "").strip()
    if is_truthy(allow_insecure):
        return AuthGuardVerdict("insecure-allowed", _insecure_warning(env_name))
    return AuthGuardVerdict("refuse", _refusal_message(env_name))


def assert_auth_config(settings: object) -> AuthGuardVerdict:
    """Raise :class:`InsecureAuthConfigError` on a refusal, warn loudly on an opted-in open gateway.

    Called from the FastAPI lifespan, which is the gateway's equivalent of the web tier's
    ``instrumentation.ts``: it runs once, before the first request is served, in every way this
    process is started (uvicorn, the container entrypoint, a TestClient). A per-request check would
    be skippable by any route that forgot it.
    """
    verdict = check_auth_config(
        env=getattr(settings, "env", ""),
        require_api_key=getattr(settings, "require_api_key", False),
        allow_insecure=getattr(settings, "allow_insecure_no_auth", ""),
    )
    if verdict.kind == "refuse":
        raise InsecureAuthConfigError(verdict.message)
    if verdict.kind == "insecure-allowed":
        logger.warning("%s", verdict.message)
    return verdict
