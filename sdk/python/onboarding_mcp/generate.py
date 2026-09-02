# SPDX-License-Identifier: Apache-2.0
"""The code-returning MCP tools (CTO-261 section 4.2).

``get_recipe``, ``generate_middleware``, ``instrument_call_site``, ``explain_layer``,
and a ``coverage_report`` stub. Each returns recipes or generated code, never an edit
applied to a repo (section 4.2). Every path is honest under uncertainty: a stack with
no recipe returns a reported gap, never a fabricated ``record_*`` call (section 2
decision 2, section 9).
"""

from __future__ import annotations

import re
from typing import Any

from onboarding_mcp.catalog import RecipeCatalog, get_catalog
from onboarding_mcp.sdk_surface import emitted_tally_calls, layer_grounding

_HOLE_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _gap(reason: str, **extra: Any) -> dict[str, Any]:
    """A reported gap. The one shape every no-recipe / unanswered path returns."""
    return {"gap": True, "reason": reason, **extra}


def _holes(template: str) -> list[str]:
    """Distinct ``{hole}`` names in a template, in first-seen order."""
    seen: list[str] = []
    for name in _HOLE_RE.findall(template):
        if name not in seen:
            seen.append(name)
    return seen


def _fill(template: str, values: dict[str, str]) -> str:
    """Replace ``{hole}`` tokens from ``values``; leave unknown holes as a clear FILL marker."""
    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        return values[name] if name in values else f"<FILL:{name}>"

    return _HOLE_RE.sub(repl, template)


def get_recipe(name: str, *, catalog: RecipeCatalog | None = None) -> dict[str, Any]:
    """Return the machine-readable recipe for a recipe id or framework / provider name.

    A name that matches no recipe is a reported gap, not a guess (section 2 decision 2).
    """
    cat = catalog or get_catalog()
    recipe = cat.resolve_alias(name)
    if recipe is None:
        return _gap(
            f"no recipe for {name!r}",
            requested=name,
            known_recipes=[r.id for r in cat.recipes],
        )
    return dict(recipe.raw)


def generate_middleware(
    web_framework: str,
    account_source: str,
    feature_tag: str | None = None,
    *,
    catalog: RecipeCatalog | None = None,
) -> dict[str, Any]:
    """Generate drop-in account / feature middleware bound to the developer's answer (section 6).

    ``account_source`` is the confirmed resolver expression (an ``X-Customer-Id`` header, an
    auth dependency); it is injected verbatim, never inferred. Without an answer the account
    layer stays unattributed, so an empty ``account_source`` is a reported gap, not a guess.
    """
    cat = catalog or get_catalog()
    if not account_source.strip():
        return _gap(
            "the account-identity question is unanswered; middleware not generated "
            "(section 6). The account layer stays UNATTRIBUTED until a resolver is confirmed.",
            web_framework=web_framework,
        )
    recipe = None
    for candidate in cat.by_kind("middleware"):
        if str(candidate.detect.get("web_framework", "")).lower() == web_framework.lower():
            recipe = candidate
            break
    if recipe is None:
        return _gap(
            f"no middleware recipe for web framework {web_framework!r}",
            web_framework=web_framework,
            known_frameworks=[
                r.detect.get("web_framework") for r in cat.by_kind("middleware")
            ],
        )

    feature_expr = repr(feature_tag) if feature_tag else "None"
    code = _fill(
        recipe.edit["template"],
        {"account_source": account_source, "feature_tag": feature_expr},
    )
    return {
        "recipe_id": recipe.id,
        "web_framework": web_framework,
        "imports_to_add": recipe.edit["imports_to_add"],
        "code": code,
        "placement": recipe.edit["placement"],
        "bound_account_source": account_source,
        "feature_tag": feature_tag,
    }


def instrument_call_site(
    call_site: str,
    recipe_id: str,
    *,
    holes: dict[str, str] | None = None,
    catalog: RecipeCatalog | None = None,
) -> dict[str, Any]:
    """Adapt a recipe's ``record_*`` edit to a concrete call site (section 4.2).

    ``holes`` lets the caller supply values it derived from the call site; anything
    unfilled is surfaced as a FILL marker plus a ``holes_to_fill`` list, so the tool
    never invents an index name or a count. An unknown ``recipe_id``, or a recipe that
    emits no SDK call (the otel-ingest recipe), is a reported gap.
    """
    cat = catalog or get_catalog()
    recipe = cat.resolve_alias(recipe_id)
    if recipe is None:
        return _gap(
            f"no recipe {recipe_id!r}; cannot instrument this call site",
            requested=recipe_id,
            call_site=call_site,
        )
    if not recipe.sdk_surface.get("call"):
        return _gap(
            f"recipe {recipe.id!r} emits config, not an SDK call; nothing to instrument here",
            recipe_id=recipe.id,
        )

    supplied = dict(holes or {})
    supplied.update(_autofill_from_call_site(call_site, recipe))
    template = recipe.edit["template"]
    code = _fill(template, supplied)
    remaining = [h for h in _holes(template) if h not in supplied]
    emitted = emitted_tally_calls(template)
    return {
        "recipe_id": recipe.id,
        "sdk_call": recipe.sdk_surface["call"],
        "emitted_calls": [c.name for c in emitted],
        "imports_to_add": recipe.edit["imports_to_add"],
        "code": code,
        "placement": recipe.edit["placement"],
        "holes_to_fill": remaining,
    }


# Cheap, honest autofill: derive a result count from an obvious limit kwarg. Anything
# not confidently derivable is left as a FILL marker (never guessed).
_COUNT_HOLES = {"n_results", "row_count"}
_COUNT_RE = re.compile(r"\b(?:top_k|limit|k)\s*=\s*(\d+)")


def _autofill_from_call_site(call_site: str, recipe) -> dict[str, str]:
    filled: dict[str, str] = {}
    count_match = _COUNT_RE.search(call_site)
    if count_match:
        for hole in _holes(recipe.edit["template"]):
            if hole in _COUNT_HOLES:
                filled[hole] = count_match.group(1)
    return filled


def explain_layer(query: str, *, catalog: RecipeCatalog | None = None) -> dict[str, Any]:
    """Explain which ``record_*`` method covers a layer, grounded on the SDK surface (section 4.2).

    ``query`` is a layer name ("vector") or a call-site excerpt ("what records this
    pinecone call?"). For an excerpt, the matching recipe's layer is used.
    """
    cat = catalog or get_catalog()
    facts = layer_grounding(query.strip().lower())
    matched_recipe = None
    if facts is None:
        # Not a bare layer name: try to match the excerpt to a recipe by its detect block.
        for recipe in cat.recipes:
            if any(pat in query for pat in recipe.call_patterns) or any(
                imp in query for imp in recipe.imports
            ):
                facts = layer_grounding(recipe.verify.get("layer", ""))
                matched_recipe = recipe.id
                break
    if facts is None:
        return _gap(
            f"no known layer or recipe matches {query!r}",
            query=query,
            known_layers=["llm", "tool", "vector", "embeddings", "account"],
        )
    result = dict(facts)
    if matched_recipe:
        result["matched_recipe"] = matched_recipe
    return result


def coverage_report(tenant_key: str, *, layers: list[str] | None = None) -> dict[str, Any]:
    """Per-layer coverage (section 7). Stubbed against the spec contract for P1.

    The real ClickHouse per-layer existence probe is the gateway's work in a later PR
    (section 12 P3). Until then this reports each layer as not-yet-probed rather than
    fabricating a covered / dark verdict, honoring "honest under uncertainty": a layer
    is never marked covered without a span to prove it (section 7, CLAUDE.md).
    """
    layer_names = layers or ["llm", "tool", "vector", "embeddings", "account"]
    return {
        "tenant_key_present": bool(tenant_key),
        "probe_available": False,
        "note": (
            "Per-layer coverage probe ships in a later PR (CTO-261 section 7 / P3). "
            "No layer is reported as covered without a proving span."
        ),
        "layers": [
            {
                "layer": name,
                "signal": (grounding or {}).get("signal", ""),
                "status": "not_probed",
            }
            for name in layer_names
            for grounding in [layer_grounding(name)]
        ],
    }
