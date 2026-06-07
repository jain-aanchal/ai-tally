# SPDX-License-Identifier: Apache-2.0
from tally.compat import (
    Capabilities,
    Support,
    register,
    registered,
    render_markdown,
    render_matrix,
)


def test_openai_is_registered_from_instrumentor():
    caps = registered()["openai"]
    assert caps.provider == "openai"
    assert caps.token_usage is Support.FULL
    assert caps.cost is Support.FULL
    assert "gpt-5-mini" in caps.models


def test_overall_full_when_all_full():
    c = Capabilities(
        provider="x",
        token_usage=Support.FULL,
        cost=Support.FULL,
        streaming=Support.FULL,
        prompt_caching=Support.FULL,
        tool_calls=Support.FULL,
    )
    assert c.overall is Support.FULL


def test_overall_partial_when_mixed():
    c = Capabilities(provider="x", token_usage=Support.FULL)
    assert c.overall is Support.PARTIAL


def test_overall_planned_when_none():
    assert Capabilities(provider="x").overall is Support.PLANNED


def test_matrix_rows_sorted_and_have_overall():
    rows = render_matrix()
    providers = [r["provider"] for r in rows]
    assert providers == sorted(providers)
    assert all("overall" in r for r in rows)


def test_markdown_renders_header_and_openai():
    md = render_markdown()
    assert md.startswith("| provider |")
    assert "openai" in md
    assert "anthropic" in md  # planned, but listed honestly


def test_register_is_idempotent_by_provider():
    before = len(registered())
    register(Capabilities(provider="openai", token_usage=Support.FULL))  # overwrite
    after = len(registered())
    assert after == before  # same provider key, not a new row


def test_planned_providers_present():
    reg = registered()
    assert reg["anthropic"].overall is Support.PLANNED
    assert reg["vertex"].overall is Support.PLANNED
