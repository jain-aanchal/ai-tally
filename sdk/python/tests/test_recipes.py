# SPDX-License-Identifier: Apache-2.0
"""Recipe catalog validation (CTO-261 sections 5.2, 5.3, 13).

The anti-hallucination guard. Every recipe is validated against ``recipes/schema.json``,
then each ``edit.template``'s emitted ``tally.*`` call is resolved against the real SDK
public surface (the module-level convenience plus its ``delegates_to`` method) and the
passed kwargs are bound to the real signature. A template that calls a nonexistent
function, or passes an unknown kwarg, fails here rather than shipping a bad edit.
"""

from __future__ import annotations

import inspect

import jsonschema
import pytest
import yaml
from onboarding_mcp.catalog import RECIPES_DIR, Recipe, load_catalog
from onboarding_mcp.sdk_surface import (
    SurfaceError,
    emitted_tally_calls,
    resolve_dotted,
    validate_recipe_surface,
)

CATALOG = load_catalog(RECIPES_DIR)


def test_catalog_is_non_empty_and_covers_the_p1_stack():
    ids = {r.id for r in CATALOG.recipes}
    # Section 12 P1: FastAPI / Flask / Django middleware; Pinecone / Weaviate / Qdrant /
    # pgvector vector calls; a tool and an embedding recipe; the OTel-ingest recipe.
    expected = {
        "middleware.fastapi.account",
        "middleware.flask.account",
        "middleware.django.account",
        "vector.pinecone.query",
        "vector.weaviate.query",
        "vector.qdrant.search",
        "vector.pgvector.query",
        "tool.generic.call",
        "embedding.generic.call",
        "otel.ingest.gen_ai",
        # Section 3 step 4 / section 5.3: the startup line the proposed diff opens with,
        # and the LLM call sites CTO-260 auto-instrumentation does not reach.
        "startup.tally.init",
        "llm.generic.call",
    }
    assert expected <= ids


@pytest.mark.parametrize("recipe", CATALOG.recipes, ids=lambda r: r.id)
def test_recipe_matches_schema(recipe: Recipe):
    jsonschema.validate(instance=recipe.raw, schema=CATALOG.schema)


@pytest.mark.parametrize("recipe", CATALOG.recipes, ids=lambda r: r.id)
def test_recipe_emitted_call_resolves_against_the_real_sdk(recipe: Recipe):
    # The core guard: resolve the emitted call, bind the kwargs to the real signature.
    validate_recipe_surface(recipe)


def test_startup_recipe_binds_the_real_tally_init_signature():
    # The parametrized guard above covers this recipe too; this pins the specific claim
    # that matters for the startup line: the emitted call is tally.init and its kwargs
    # bind against the live init signature, not a look-alike.
    recipe = CATALOG.get("startup.tally.init")
    validate_recipe_surface(recipe)
    emitted = {c.name for c in emitted_tally_calls(recipe.edit["template"])}
    assert emitted == {"init"}
    assert recipe.sdk_surface["delegates_to"] == "tally.init.init"
    assert "feature_tag" in inspect.signature(resolve_dotted("tally.init.init")).parameters


def test_llm_call_site_recipe_binds_the_real_record_llm_call_signature():
    recipe = CATALOG.get("llm.generic.call")
    validate_recipe_surface(recipe)
    calls = emitted_tally_calls(recipe.edit["template"])
    emitted = [c for c in calls if c.name == "record_llm_call"]
    assert emitted, "the llm recipe must emit tally.record_llm_call(...)"
    sig = inspect.signature(resolve_dotted("tally.client.TallyClient.record_llm_call"))
    for kwarg in emitted[0].kwargs:
        assert kwarg in sig.parameters


def test_recipe_templates_carry_no_prompt_or_completion_text():
    # "No bodies in telemetry" (CLAUDE.md) applies to what the recipes tell an app to
    # emit: counts and names only, never a prompt, a completion, or retrieved text.
    forbidden = ("prompt=", "completion=", "messages=", "input_text", "response_text", "text=")
    for recipe in CATALOG.recipes:
        template = recipe.edit["template"]
        for token in forbidden:
            assert token not in template, f"{recipe.id} template passes {token!r}"


def test_index_and_files_agree():
    index = yaml.safe_load((RECIPES_DIR / "index.yaml").read_text())
    index_files = {entry["file"] for entry in index["recipes"]}
    on_disk = {p.name for p in RECIPES_DIR.glob("*.yaml") if p.name != "index.yaml"}
    assert index_files == on_disk, "every recipe file must be listed in index.yaml and vice versa"


# --------------------------------------------------------------------------- #
# The guard must actually reject bad recipes, not just pass the good ones.
# --------------------------------------------------------------------------- #
def _doc(**overrides):
    base = {
        "id": "vector.fake.query",
        "kind": "vector",
        "title": "fake",
        "detect": {"imports": ["fake"]},
        "sdk_surface": {
            "call": "tally.record_vector_call",
            "delegates_to": "tally.client.TallyClient.record_vector_call",
            "required_args": ["provider", "index", "operation"],
            "layer": "vector",
        },
        "edit": {
            "imports_to_add": ["import tally"],
            "template": (
                "tally.record_vector_call(provider='fake', index='i', operation='query')\n"
            ),
            "placement": "after_call",
        },
        "verify": {"layer": "vector", "operation_name": "vector"},
    }
    base.update(overrides)
    return base


def test_guard_passes_a_well_formed_synthetic_recipe():
    validate_recipe_surface(Recipe.from_doc(_doc()))


def test_guard_rejects_a_nonexistent_function():
    doc = _doc(
        sdk_surface={
            "call": "tally.record_bogus_call",
            "delegates_to": None,
            "required_args": [],
            "layer": "vector",
        },
        edit={
            "imports_to_add": ["import tally"],
            "template": "tally.record_bogus_call(provider='fake')\n",
            "placement": "after_call",
        },
    )
    with pytest.raises(SurfaceError):
        validate_recipe_surface(Recipe.from_doc(doc))


def test_guard_rejects_an_unknown_kwarg():
    doc = _doc(
        edit={
            "imports_to_add": ["import tally"],
            "template": (
                "tally.record_vector_call(provider='fake', index='i', "
                "operation='query', bogus_kwarg=1)\n"
            ),
            "placement": "after_call",
        }
    )
    with pytest.raises(SurfaceError):
        validate_recipe_surface(Recipe.from_doc(doc))


def test_guard_rejects_a_required_arg_that_is_not_a_real_parameter():
    doc = _doc(
        sdk_surface={
            "call": "tally.record_vector_call",
            "delegates_to": "tally.client.TallyClient.record_vector_call",
            "required_args": ["provider", "not_a_real_param"],
            "layer": "vector",
        }
    )
    with pytest.raises(SurfaceError):
        validate_recipe_surface(Recipe.from_doc(doc))


def test_guard_rejects_a_template_that_never_emits_the_declared_call():
    doc = _doc(
        edit={
            "imports_to_add": ["import tally"],
            "template": "tally.record_tool_call(provider='fake', tool='t')\n",
            "placement": "after_call",
        }
    )
    with pytest.raises(SurfaceError):
        validate_recipe_surface(Recipe.from_doc(doc))
