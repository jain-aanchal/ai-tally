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
