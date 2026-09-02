# SPDX-License-Identifier: Apache-2.0
"""``detect_stack`` MCP tool (CTO-261 sections 3 step 1, 4.2).

Reads dependency-manifest contents (and optional import-site excerpts the developer's
agent chooses to pass, section 4.2) and reports the detected providers, frameworks,
vector DBs, and web frameworks, plus the recipe ids that match. Detection is grounded
on the catalog's ``detect`` blocks: a component with no recipe is reported as a gap,
never filled by guessing (section 2 decision 2).
"""

from __future__ import annotations

import re
from typing import Any

from onboarding_mcp.catalog import RecipeCatalog, get_catalog

# Classification tokens for the human-facing summary. LLM providers appear here even
# though P1 ships no manual LLM recipe: they are auto-instrumented by tally.init
# (CTO-260 section 4), so detecting them tells the developer "already covered", not a gap.
_LLM_PROVIDERS = {"openai", "anthropic", "google", "cohere", "mistralai", "vertexai"}
_WEB_FRAMEWORKS = {"fastapi", "flask", "django"}
_AGENT_FRAMEWORKS = {"langchain", "langchain_core", "agents", "mcp", "llama_index"}
_VECTOR_DBS = {"pinecone", "weaviate", "qdrant_client", "chromadb", "pgvector"}

_TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text)}


def _classify(tokens: set[str], vocabulary: set[str]) -> list[str]:
    return sorted(tokens & vocabulary)


def detect_stack(
    manifest: str = "",
    import_excerpts: str = "",
    *,
    catalog: RecipeCatalog | None = None,
) -> dict[str, Any]:
    """Detect the stack from a manifest and optional import-site excerpts.

    Args:
        manifest: contents of ``requirements.txt`` / ``pyproject.toml`` / a lockfile.
        import_excerpts: optional source excerpts; only these are matched against
            ``call_patterns`` (section 4.2: manifests are the cheap default, excerpts opt-in).
        catalog: override for tests; defaults to the in-tree catalog.

    Returns a dict of detected components plus ``matched_recipes`` (recipe ids that apply)
    and ``gaps`` (detected components with no recipe).
    """
    cat = catalog or get_catalog()
    manifest_tokens = _tokens(manifest)
    excerpt_tokens = _tokens(import_excerpts)
    all_tokens = manifest_tokens | excerpt_tokens

    matched: list[str] = []
    for recipe in cat.recipes:
        import_hit = any(_tokens(imp) & all_tokens for imp in recipe.imports)
        pattern_hit = any(pat in import_excerpts for pat in recipe.call_patterns)
        if import_hit or pattern_hit:
            matched.append(recipe.id)

    web = _classify(all_tokens, _WEB_FRAMEWORKS)
    vector = _classify(all_tokens, _VECTOR_DBS)
    agents = _classify(all_tokens, _AGENT_FRAMEWORKS)
    llm = _classify(all_tokens, _LLM_PROVIDERS)

    # A gap is a detected component category that surfaced no matching recipe.
    gaps: list[str] = []
    matched_kinds = {cat.get(rid).kind for rid in matched}
    if vector and "vector" not in matched_kinds:
        gaps.append("vector: detected a vector DB but no recipe matched")

    # Check each detected web framework against a matched middleware recipe for that framework, not
    # just whether *some* middleware matched: with two frameworks present (e.g. flask + fastapi) a
    # blanket "middleware in matched_kinds" check would report the whole stack handled while the
    # framework with no recipe was silently dropped (CTO-261 §4.2, review finding).
    matched_web_frameworks = {
        str(cat.get(rid).detect.get("web_framework", "")).lower()
        for rid in matched
        if cat.get(rid).kind == "middleware"
    }
    unhandled_web = [f for f in web if f not in matched_web_frameworks]
    if unhandled_web:
        gaps.append(
            "account: detected web framework(s) "
            f"{', '.join(unhandled_web)} but no middleware recipe matched"
        )

    return {
        # All detected web frameworks, so a second framework is never dropped. web_framework keeps
        # the single-framework field prior callers read (the first, alphabetically).
        "web_frameworks": web,
        "web_framework": web[0] if web else None,
        "llm_providers": llm,
        "agent_frameworks": agents,
        "vector_dbs": vector,
        "matched_recipes": sorted(matched),
        "gaps": gaps,
    }
