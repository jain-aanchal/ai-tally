# SPDX-License-Identifier: Apache-2.0
"""Onboarding MCP tool tests (CTO-261 sections 4.2, 13).

Detection on sample manifests, recipe retrieval, middleware generation bound to a given
header, call-site adaptation, and the no-recipe gap path (a reported gap, never a
fabricated record).
"""

from __future__ import annotations

import ast

from onboarding_mcp import (
    coverage_report,
    detect_stack,
    explain_layer,
    generate_middleware,
    generate_startup,
    get_recipe,
    instrument_call_site,
)
from onboarding_mcp.sdk_surface import emitted_tally_calls

# A representative P1 stack: FastAPI + Pinecone + openai (section 12 "Done when").
SAMPLE_MANIFEST = """
fastapi==0.115.0
uvicorn==0.30.0
openai==1.40.0
pinecone-client==5.0.0
"""

SAMPLE_IMPORTS = """
import openai
from pinecone import Pinecone
from fastapi import FastAPI, Request
results = index.query(vector=embedding, top_k=5)
"""


def test_detect_stack_on_sample_manifest():
    result = detect_stack(SAMPLE_MANIFEST, SAMPLE_IMPORTS)
    assert result["web_framework"] == "fastapi"
    assert "openai" in result["llm_providers"]
    assert "pinecone" in result["vector_dbs"]
    assert "vector.pinecone.query" in result["matched_recipes"]
    assert "middleware.fastapi.account" in result["matched_recipes"]


def test_detect_stack_manifest_only_still_matches():
    # Manifests are the cheap default (section 4.2); detection works without excerpts.
    result = detect_stack(SAMPLE_MANIFEST)
    assert "vector.pinecone.query" in result["matched_recipes"]
    assert "middleware.fastapi.account" in result["matched_recipes"]


def test_detect_stack_reports_all_web_frameworks():
    # Two web frameworks in one stack: both must be reported, not just the alphabetically-first,
    # and web_framework (singular) stays the first for prior callers (finding #8).
    manifest = "django==5.0\nfastapi==0.115.0\nuvicorn==0.30.0\n"
    result = detect_stack(manifest)
    assert result["web_frameworks"] == ["django", "fastapi"]
    assert result["web_framework"] == "django"


def test_detect_stack_no_gap_when_every_web_framework_is_handled():
    result = detect_stack(SAMPLE_MANIFEST, SAMPLE_IMPORTS)
    assert not [g for g in result["gaps"] if g.startswith("account:")]


def test_detect_stack_gap_names_the_unhandled_web_framework():
    # The account gap is checked per detected framework, not "did any middleware match", so a
    # framework whose middleware recipe was dropped is named rather than masked by another that
    # matched (CTO-261 §4.2, finding #8). Use a catalog with only the fastapi middleware recipe so
    # a detected flask has no recipe to match.
    from onboarding_mcp.catalog import RecipeCatalog, get_catalog

    full = get_catalog()
    only_fastapi = RecipeCatalog(
        [r for r in full.recipes if r.detect.get("web_framework") != "flask"],
        full.schema,
    )
    manifest = "fastapi==0.115.0\nflask==3.0.0\n"
    imports = "from fastapi import FastAPI\nfrom flask import Flask\n"
    result = detect_stack(manifest, imports, catalog=only_fastapi)
    assert result["web_frameworks"] == ["fastapi", "flask"]
    account_gaps = [g for g in result["gaps"] if g.startswith("account:")]
    # flask is detected but unhandled and named; fastapi matched so is not in the gap.
    assert account_gaps and "flask" in account_gaps[0]
    assert "fastapi" not in account_gaps[0]


# --------------------------------------------------------------------------- #
# The manual LLM recipe must not fire on an already-patched call, and must not fire
# on an app with no LLM at all (CTO-261 review findings 1 and 2).
# --------------------------------------------------------------------------- #
def test_patched_openai_call_does_not_match_the_manual_llm_recipe():
    # tally.init monkeypatches client.chat.completions.create (CTO-260). Matching the
    # manual recipe here made an agent add a second record_llm_call beside a metered
    # call, doubling reported cost: the product's core number.
    result = detect_stack(
        "openai==1.0\nfastapi==0.115",
        "r = client.chat.completions.create(model=m, messages=msgs)",
    )
    assert "llm.generic.call" not in result["matched_recipes"]


def test_detected_auto_instrumented_provider_is_flagged_as_already_covered():
    # A marker in the tool OUTPUT is not optional: the recipe template's comment never
    # reaches the agent reading detect_stack's result.
    result = detect_stack("openai==1.0\nanthropic==0.34\n")
    assert result["already_covered"], "an auto-instrumented provider must be flagged"
    marker = result["already_covered"][0]
    assert "openai" in marker
    assert "twice" in marker
    assert "tally.init()" in marker


def test_no_llm_provider_means_no_already_covered_marker():
    result = detect_stack("requests==2.32.0\nflask==3.0\n")
    assert result["already_covered"] == []


def test_plain_requests_and_flask_app_does_not_match_the_llm_recipe():
    # httpx / requests / boto3 are in nearly every Python app. Matching on them told an
    # app with no LLM to add record_llm_call while llm_providers was empty.
    result = detect_stack("requests==2.32.0\nflask==3.0\n")
    assert result["llm_providers"] == []
    assert "llm.generic.call" not in result["matched_recipes"]


def test_boto3_alone_does_not_match_but_bedrock_runtime_does():
    # boto3 is overwhelmingly S3 / DynamoDB, so it only signals an LLM call site with a
    # bedrock-runtime call pattern alongside it.
    plain = detect_stack("boto3==1.34.0\n", "s3 = boto3.client('s3')")
    assert "llm.generic.call" not in plain["matched_recipes"]
    bedrock = detect_stack("boto3==1.34.0\n", "brt = boto3.client('bedrock-runtime')")
    assert "llm.generic.call" in bedrock["matched_recipes"]


def test_genuinely_unpatched_providers_still_match():
    # Narrowing must not blind the recipe to the call sites it exists for.
    assert "llm.generic.call" in detect_stack("ollama==0.3.0\n")["matched_recipes"]
    assert (
        "llm.generic.call"
        in detect_stack("", 'httpx.post("https://x/v1/chat/completions")')["matched_recipes"]
    )


def test_explain_layer_on_boto3_is_a_gap_not_a_confident_llm_answer():
    # A confidently wrong grounded answer is worse than a gap (CLAUDE.md). boto3 must not
    # resolve to record_llm_call / GenAiOperation='chat'.
    result = explain_layer("what records this boto3 call?")
    assert result["gap"] is True
    result = explain_layer("boto3")
    assert result["gap"] is True


def test_explain_layer_on_ambiguous_common_word_prose_is_a_gap():
    # "together" is a real package (together.ai) AND an everyday adverb, so free-text prose
    # containing it must not ground on the LLM layer. A gap beats a confident wrong answer
    # (CTO-261 review finding 2, CLAUDE.md "honest under uncertainty").
    for prose in (
        "let us work through this together",
        "do these two calls get metered together?",
        "how are my agents metered?",
        "do the numbers cohere across layers?",
    ):
        result = explain_layer(prose)
        assert result["gap"] is True, prose


def test_explain_layer_still_answers_an_ambiguous_token_in_package_shaped_context():
    # The stronger signal is import-shaped or package-shaped context, so a genuine
    # together.ai question still gets the grounded LLM answer.
    for excerpt in (
        "import together",
        "what records this together.ai call?",
        "from together import Together",
    ):
        result = explain_layer(excerpt)
        assert result.get("gap") is not True, excerpt
        assert result["call"] == "tally.record_llm_call", excerpt


def test_detect_stack_still_detects_together_from_a_manifest_and_from_imports():
    # The explain_layer guard must not cost together.ai manifest / import detection: in
    # detect_stack the token appearing really does mean the dependency is present.
    manifest = detect_stack("together==1.2.3\n")
    assert "llm.generic.call" in manifest["matched_recipes"]
    excerpt = detect_stack("", "import together\nclient = together.Together()")
    assert "llm.generic.call" in excerpt["matched_recipes"]


def test_explain_layer_does_not_match_an_import_name_buried_in_a_longer_identifier():
    # Imports match on identifier-token boundaries rather than bare substrings, so an
    # unrelated name that merely contains a provider name is a gap, not a confident
    # LLM answer. (Guards the same class of bug as the boto3 case above.)
    result = explain_layer("what records this vllmlike_helper call?")
    assert result["gap"] is True


def test_get_recipe_by_id_and_by_alias():
    by_id = get_recipe("vector.pinecone.query")
    assert by_id["id"] == "vector.pinecone.query"
    assert by_id["sdk_surface"]["call"] == "tally.record_vector_call"
    # Section 4.2 allows a friendly framework / provider name.
    by_alias = get_recipe("pinecone")
    assert by_alias["id"] == "vector.pinecone.query"
    by_framework = get_recipe("fastapi")
    assert by_framework["kind"] == "middleware"


def test_get_recipe_unknown_is_a_reported_gap():
    result = get_recipe("cassandra")
    assert result["gap"] is True
    assert "cassandra" in result["reason"]
    # Honest: it names what it knows instead of inventing a recipe.
    assert result["known_recipes"]


def test_generate_middleware_is_bound_to_the_given_header():
    account_source = 'request.headers.get("X-Customer-Id")'
    result = generate_middleware("fastapi", account_source, feature_tag="chatbot")
    assert result["recipe_id"] == "middleware.fastapi.account"
    assert account_source in result["code"]
    assert "chatbot" in result["code"]
    # The generated code parses and actually emits with_account / start_trace.
    ast.parse(result["code"])
    emitted = {c.name for c in emitted_tally_calls(result["code"])}
    assert "with_account" in emitted
    assert "start_trace" in emitted
    assert "<FILL:" not in result["code"]  # both holes were bound


def test_generate_middleware_bundles_the_startup_snippet():
    # Section 3 step 4: the proposed diff is init + middleware + record_*. Middleware
    # without the init line wires a process that was never connected.
    result = generate_middleware("fastapi", 'request.headers["X-Customer-Id"]', "chatbot")
    startup = result["startup"]
    assert startup["recipe_id"] == "startup.tally.init"
    assert startup["placement"] == "startup"
    assert "tally.init(" in startup["code"]
    ast.parse(startup["code"])
    assert {c.name for c in emitted_tally_calls(startup["code"])} == {"init"}


def test_generate_startup_binds_the_feature_tag_and_never_inlines_a_key():
    result = generate_startup("chatbot")
    assert "feature_tag='chatbot'" in result["code"]
    assert "<FILL:" not in result["code"]
    # Credentials by reference (CLAUDE.md): the key comes from the environment, so no
    # snippet ever puts a tally_sk_live_ secret into the developer's diff.
    assert "tally_sk_live_" not in result["code"]
    assert "TALLY_KEY" in result["code"]


def test_generate_startup_without_a_feature_tag_is_none_not_invented():
    result = generate_startup()
    assert "feature_tag=None" in result["code"]


def test_generate_startup_without_the_recipe_is_a_gap():
    # A catalog missing the startup recipe reports a gap rather than hand-writing init.
    from onboarding_mcp.catalog import RecipeCatalog, get_catalog

    full = get_catalog()
    without = RecipeCatalog(
        [r for r in full.recipes if r.id != "startup.tally.init"], full.schema
    )
    result = generate_startup("chatbot", catalog=without)
    assert result["gap"] is True
    assert "code" not in result


def test_middleware_startup_gap_is_readable_without_a_key_error():
    # Finding 6: generate_startup returns a union, and the gap arm carries no code /
    # placement / imports_to_add. A caller reading result["startup"]["code"] must see the
    # gap, not a KeyError.
    from onboarding_mcp.catalog import RecipeCatalog, get_catalog

    full = get_catalog()
    without = RecipeCatalog(
        [r for r in full.recipes if r.id != "startup.tally.init"], full.schema
    )
    result = generate_middleware(
        "fastapi", 'request.headers["X-Customer-Id"]', "chatbot", catalog=without
    )
    startup = result["startup"]
    assert startup["gap"] is True
    assert startup["code"] is None
    assert startup["placement"] is None
    assert startup["imports_to_add"] == []
    assert startup["reason"]


def test_startup_recipe_is_not_counted_as_llm_layer_coverage():
    # Finding 7: the init line is a startup recipe, not an LLM-call recipe. Counting it
    # under llm would mislead the first consumer that branches on the kind (the P3
    # coverage probe).
    from onboarding_mcp.catalog import get_catalog

    cat = get_catalog()
    startup = cat.get("startup.tally.init")
    assert startup.kind == "startup"
    assert startup.sdk_surface["layer"] == "startup"
    assert startup.verify["layer"] == "startup"
    assert startup.id not in {r.id for r in cat.by_kind("llm")}


def test_instrument_call_site_adapts_the_llm_recipe():
    # Section 5.3: LLM call sites CTO-260 auto-instrumentation does not cover.
    result = instrument_call_site(
        'r = httpx.post("/v1/chat/completions", json=payload)', "llm.generic.call"
    )
    assert result["sdk_call"] == "tally.record_llm_call"
    assert "record_llm_call" in result["emitted_calls"]
    # Token counts are left to fill from the provider's usage block, never guessed.
    assert "input_tokens" in result["holes_to_fill"]
    assert "<FILL:input_tokens>" in result["code"]


def test_generate_middleware_without_an_answer_is_a_gap_not_a_guess():
    result = generate_middleware("fastapi", "   ")
    assert result["gap"] is True
    assert "unanswered" in result["reason"]


def test_generate_middleware_unknown_framework_is_a_gap():
    result = generate_middleware("tornado", 'request.headers["X-Customer-Id"]')
    assert result["gap"] is True
    assert "tornado" in result["reason"]


def test_instrument_call_site_adapts_the_record_call_and_autofills_count():
    call_site = "results = index.query(vector=embedding, top_k=5)"
    result = instrument_call_site(call_site, "vector.pinecone.query")
    assert result["sdk_call"] == "tally.record_vector_call"
    assert "record_vector_call" in result["emitted_calls"]
    # top_k=5 fills the record_count hole; the index name is left to fill, never guessed.
    assert "record_count=5" in result["code"]
    assert "index_name" in result["holes_to_fill"]


def test_instrument_call_site_unknown_recipe_returns_a_gap_not_a_record():
    result = instrument_call_site("some.call()", "vector.milvus.query")
    assert result["gap"] is True
    assert "code" not in result  # no fabricated record emitted


def test_instrument_call_site_on_otel_recipe_is_a_gap():
    # The otel-ingest recipe emits config, not an SDK call: nothing to instrument.
    result = instrument_call_site("...", "otel.ingest.gen_ai")
    assert result["gap"] is True


def test_explain_layer_by_name_is_grounded_on_the_sdk():
    result = explain_layer("vector")
    assert result["call"] == "tally.record_vector_call"
    assert result["operation_name"] == "vector"
    assert result["signal"] == "GenAiOperation = 'vector'"


def test_explain_layer_by_excerpt_maps_to_a_recipe_layer():
    result = explain_layer("index.query(vector=v, top_k=5)")
    assert result["call"] == "tally.record_vector_call"
    assert result.get("matched_recipe") == "vector.pinecone.query"


def test_explain_layer_account_layer():
    result = explain_layer("account")
    assert result["call"] == "tally.with_account"
    assert result["signal"] == "AccountIdHash != ''"


def test_explain_layer_unknown_is_a_gap():
    result = explain_layer("quantum")
    assert result["gap"] is True


def test_coverage_report_is_honest_and_not_fabricated():
    result = coverage_report("tally_sk_live_deadbeef")
    assert result["probe_available"] is False
    statuses = {layer["status"] for layer in result["layers"]}
    assert statuses == {"not_probed"}
    # No layer is claimed covered without a proving span (section 7, CLAUDE.md).
    assert all(layer["status"] != "covered" for layer in result["layers"])
