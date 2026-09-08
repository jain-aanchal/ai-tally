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


def tokenize(text: str) -> set[str]:
    """Lowercased identifier tokens in ``text``. Shared with ``explain_layer``."""
    return {t.lower() for t in _TOKEN_RE.findall(text)}


# Catalog import tokens that are also ordinary English words a developer can write in a
# question without meaning the package (CTO-261 review finding 2). The ambiguity only bites
# in explain_layer, whose input is free-text prose: detect_stack reads a manifest or an
# import excerpt, where the token appearing genuinely means the dependency is present, so
# these stay fully live there and together.ai detection is untouched.
#
# The bar for entry is "the non-package sense plausibly occurs in a question about metering
# an AI app", not merely "the string is in a dictionary":
#   together - an everyday adverb ("let us work through this together"), the case that
#              motivated this change.
#   agents   - in this exact domain the general noun ("how are my agents metered?") is far
#              more common than the openai-agents package.
#   cohere   - an ordinary verb, and the useful package-sense questions ("this cohere.embed()
#              call") still carry the dotted shape below, so nothing real is lost.
# Deliberately NOT listed: flask and pinecone. They are concrete-object nouns whose non-package
# sense does not occur in an instrumentation question, so requiring a stronger signal there
# would only throw away correct answers ("what layer covers my flask app?").
AMBIGUOUS_IMPORT_TOKENS = frozenset({"together", "agents", "cohere"})


def _ambiguous_context_re(token: str) -> re.Pattern[str]:
    """Import-shaped or package-shaped uses of ``token``: the stronger signal prose must carry."""
    t = re.escape(token)
    return re.compile(
        # import together / from together import ...
        rf"\b(?:import|from)\s+{t}\b"
        # together==1.2.3, together>=, together~=, together[extra]
        rf"|\b{t}\s*(?:==|>=|<=|~=|!=|\[)"
        # together.ai, together.Complete(...): a dotted use is package-shaped, not prose.
        rf"|\b{t}\.[a-zA-Z_]",
        re.IGNORECASE,
    )


_AMBIGUOUS_CONTEXT_RES = {tok: _ambiguous_context_re(tok) for tok in AMBIGUOUS_IMPORT_TOKENS}


def import_token_matches_prose(token: str, prose: str, prose_tokens: set[str]) -> bool:
    """True when ``token`` is a trustworthy signal inside a human's free-text ``prose``.

    An unambiguous package name matches on an identifier-token boundary as before. An
    ambiguous common-word name additionally has to appear in an import-shaped or
    package-shaped context, because a confidently wrong grounded answer is worse than a
    gap (CTO-261 review finding 2, CLAUDE.md "honest under uncertainty").
    """
    token = token.lower()
    if token not in AMBIGUOUS_IMPORT_TOKENS:
        return token in prose_tokens
    return bool(_AMBIGUOUS_CONTEXT_RES[token].search(prose))


def _classify(tokens: set[str], vocabulary: set[str]) -> list[str]:
    return sorted(tokens & vocabulary)


# The manual LLM recipe. Named here because detect_stack has to say out loud when it
# would double-meter an already-patched call (CTO-261 review finding 1).
_MANUAL_LLM_RECIPE = "llm.generic.call"


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

    Returns a dict of detected components plus ``matched_recipes`` (recipe ids that apply),
    ``already_covered`` (coverage the agent must not add a second meter to), and ``gaps``
    (detected components with no recipe).
    """
    cat = catalog or get_catalog()
    manifest_tokens = tokenize(manifest)
    excerpt_tokens = tokenize(import_excerpts)
    all_tokens = manifest_tokens | excerpt_tokens

    matched: list[str] = []
    for recipe in cat.recipes:
        import_hit = any(tokenize(imp) & all_tokens for imp in recipe.imports)
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

    # An auto-instrumented provider is coverage the agent must not add to. tally.init
    # patches the openai / anthropic clients (CTO-260), so a manual record_llm_call beside
    # one of those call sites meters the SAME call twice and doubles reported cost, which
    # corrupts the product's core number. The recipe template carries the warning, but a
    # template comment is not in the tool OUTPUT: without this marker an agent reading
    # matched_recipes has nothing telling it the call is already covered (CTO-261 review
    # finding 1). The recipe still matches when a genuinely unpatched path is present too
    # (openai alongside ollama is a real stack), so this flags rather than silently drops.
    already_covered: list[str] = []
    if llm:
        already_covered.append(
            f"llm: {', '.join(llm)} detected. tally.init() auto-instruments these clients "
            "(CTO-260), so those call sites need no edit. Do NOT add "
            "tally.record_llm_call beside a patched "
            "client call: the same call would be metered twice and reported cost would "
            f"double. Apply {_MANUAL_LLM_RECIPE} only to a call site the patch does not "
            "reach (a raw /v1/chat/completions POST, bedrock-runtime, ollama, a "
            "self-hosted or gateway-fronted model)."
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
        "already_covered": already_covered,
        "gaps": gaps,
    }
