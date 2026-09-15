# SPDX-License-Identifier: Apache-2.0
"""The SDK reference export the docs render (CTO-371).

Fails when a documented function disappears or a signature changes without
docs/public-api/sdk-reference.json being regenerated, so the published docs cannot describe an SDK
that no longer exists.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sdk_reference.py"
_spec = importlib.util.spec_from_file_location("sdk_reference", _SCRIPT)
assert _spec and _spec.loader
sdk_reference = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sdk_reference)


def test_documented_functions_are_exported() -> None:
    names = [f["name"] for f in sdk_reference.build_reference()["functions"]]
    for expected in (
        "tally.init",
        "tally.flush",
        "tally.uninstrument",
        "tally.with_account",
        "tally.start_trace",
        "tally.hash_account",
        "tally.record_llm_call",
        "tally.record_tool_call",
        "tally.record_vector_call",
        "tally.record_embedding_call",
    ):
        assert expected in names


def test_init_signature_matches_the_docs() -> None:
    functions = sdk_reference.build_reference()["functions"]
    init = next(f for f in functions if f["name"] == "tally.init")
    params = {p["name"]: p for p in init["parameters"]}
    assert list(params) == [
        "key",
        "endpoint",
        "feature_tag",
        "instrument",
        "instrument_stream_usage",
        "flush_interval_s",
        "catalog",
    ]
    assert params["key"]["default"] == "None"
    assert params["endpoint"]["kind"] == "keyword_only"
    assert params["instrument"]["default"] == "True"
    assert params["instrument_stream_usage"]["default"] == "False"
    assert params["flush_interval_s"]["default"] == "1.0"


def test_record_functions_document_the_client_keywords_not_kwargs() -> None:
    fns = {f["name"]: f for f in sdk_reference.build_reference()["functions"]}
    llm = [p["name"] for p in fns["tally.record_llm_call"]["parameters"]]
    assert llm[:3] == ["provider", "model", "usage"]
    assert "self" not in llm
    assert "kwargs" not in llm


def test_committed_reference_is_current() -> None:
    out = sdk_reference.OUTPUT
    assert out.exists(), f"{out} is missing; run scripts/sdk_reference.py"
    assert out.read_text() == sdk_reference.render(), (
        "docs/public-api/sdk-reference.json is stale: the public SDK surface changed. Regenerate "
        "with `uv run python scripts/sdk_reference.py` from sdk/python and commit it."
    )
