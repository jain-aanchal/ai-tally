# SPDX-License-Identifier: Apache-2.0
"""The detect -> retrieve -> ask -> propose loop over a sample repo (CTO-261 section 3)."""

from __future__ import annotations

import json
from pathlib import Path

from onboarding_bot.patch import HOLE_NOTICE, MARKER, apply_proposal
from onboarding_bot.propose import ACCOUNT_QUESTION, account_question, build_proposal

SAMPLE_MAIN = '''\
"""A small sample app."""
import os

from fastapi import FastAPI
from openai import OpenAI
from pinecone import Pinecone

app = FastAPI()
client = OpenAI()
index = Pinecone(api_key=os.environ["PINECONE_KEY"]).Index("docs")


@app.post("/ask")
def ask(question: str):
    customer = request.headers.get("X-Customer-Id")
    hits = index.query(vector=[0.1], top_k=5)
    return client.chat.completions.create(model="gpt-4o-mini", messages=[])
'''


def sample_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sample"
    (repo / "app").mkdir(parents=True)
    (repo / "requirements.txt").write_text("fastapi\nopenai\npinecone\nuvicorn\n")
    (repo / "app" / "main.py").write_text(SAMPLE_MAIN)
    return repo


def test_it_detects_the_stack_from_the_repo(tmp_path: Path):
    proposal = build_proposal(sample_repo(tmp_path), account_source=None)
    detection = proposal.detection
    assert detection["web_frameworks"] == ["fastapi"]
    assert "openai" in detection["llm_providers"]
    assert "pinecone" in detection["vector_dbs"]
    assert "vector.pinecone.query" in detection["matched_recipes"]


def test_it_wires_init_and_the_vector_call_site(tmp_path: Path):
    proposal = build_proposal(
        sample_repo(tmp_path),
        account_source='request.headers.get("X-Customer-Id")',
        feature_tag="ask",
    )
    kinds = {edit.kind for edit in proposal.edits}
    assert {"startup", "account", "vector"} <= kinds

    startup = next(e for e in proposal.edits if e.kind == "startup")
    assert "tally.init(feature_tag='ask')" in startup.code

    vector = next(e for e in proposal.edits if e.kind == "vector")
    assert "tally.record_vector_call(" in vector.code
    # top_k=5 is derivable from the call site; the index name is not, so it stays a hole.
    assert "record_count=5" in vector.code
    assert "<FILL:index_name>" in vector.code
    assert vector.holes_to_fill == ["index_name"]


def test_the_middleware_is_bound_to_the_answer_and_never_to_a_guess(tmp_path: Path):
    answer = "request.state.tenant_id"
    proposal = build_proposal(sample_repo(tmp_path), account_source=answer)
    middleware = next(e for e in proposal.edits if e.kind == "account")
    assert f"account_id = {answer}" in middleware.code


def test_an_unanswered_account_question_is_a_gap_and_a_question_not_an_invention(tmp_path: Path):
    proposal = build_proposal(sample_repo(tmp_path), account_source=None)

    assert not any(edit.kind == "account" for edit in proposal.edits)
    gap = next(g for g in proposal.gaps if "account-identity question is unanswered" in g["reason"])
    # The one honesty shape, shared with the MCP server rather than re-invented here.
    assert gap["gap"] is True

    question = proposal.questions[0]
    assert question["question"] == ACCOUNT_QUESTION
    # Candidates are offered for confirmation, never adopted.
    assert all(c["status"] == "candidate, unconfirmed" for c in question["candidates"])
    assert any(c["token"] == "X-Customer-Id" for c in question["candidates"])


def test_the_question_surfaces_tokens_not_the_developers_source_lines(tmp_path: Path):
    question = account_question(sample_repo(tmp_path))
    serialized = json.dumps(question)
    assert "index.query(" not in serialized
    assert "client.chat.completions" not in serialized


def test_a_proposal_carries_generated_code_only_never_the_matched_source_line(tmp_path: Path):
    proposal = build_proposal(sample_repo(tmp_path), account_source="x")
    serialized = json.dumps([edit.__dict__ for edit in proposal.edits])
    assert "hits = index.query" not in serialized
    assert "Pinecone(api_key=" not in serialized


def test_a_call_site_returned_directly_is_a_reported_gap_not_dead_code(tmp_path: Path):
    repo = tmp_path / "direct"
    repo.mkdir()
    (repo / "requirements.txt").write_text("pinecone\n")
    (repo / "svc.py").write_text(
        "from pinecone import Pinecone\n\n\ndef go(index):\n"
        "    return index.query(vector=[0.1], top_k=2)\n"
    )
    proposal = build_proposal(repo, account_source=None)
    assert not any(e.kind == "vector" for e in proposal.edits)
    assert any("returns the call's result directly" in g["reason"] for g in proposal.gaps)


def test_a_repo_with_no_matching_recipe_proposes_no_call_site_edit(tmp_path: Path):
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "requirements.txt").write_text("requests\n")
    (bare / "util.py").write_text("import requests\n\n\ndef go():\n    return requests.get('x')\n")
    proposal = build_proposal(bare, account_source=None)
    # No record_* edit is placed: nothing in this repo is a vector, tool or embedding call
    # site, and the bot does not manufacture one to have something to show.
    assert not any(e.kind in ("vector", "tool", "embedding") for e in proposal.edits)
    assert not any(
        rid.startswith(("vector.", "tool.", "embedding."))
        for rid in proposal.detection["matched_recipes"]
    )


def test_applying_the_proposal_writes_valid_marked_python(tmp_path: Path):
    repo = sample_repo(tmp_path)
    proposal = build_proposal(
        repo, account_source='request.headers.get("X-Customer-Id")', feature_tag="ask"
    )
    touched = apply_proposal(repo, proposal)
    assert touched == ["app/main.py"]

    patched = (repo / "app" / "main.py").read_text()
    compile(patched, "main.py", "exec")  # the diff must be syntactically valid Python
    assert MARKER in patched
    assert "tally.init(" in patched
    assert "tally.with_account(" in patched
    # import tally is added once, not per edit.
    assert patched.count("import tally\n") == 1


def test_a_block_with_an_unfilled_hole_is_inserted_inactive_not_invented(tmp_path: Path):
    repo = sample_repo(tmp_path)
    proposal = build_proposal(repo, account_source="x")
    apply_proposal(repo, proposal)
    patched = (repo / "app" / "main.py").read_text()

    # The vector edit could not derive the index name, so it lands commented out with the
    # hole visible. Never a guessed index, never a file that no longer parses.
    compile(patched, "main.py", "exec")
    assert HOLE_NOTICE in patched
    assert "# tally.record_vector_call(" in patched
    assert "#     index=<FILL:index_name>," in patched
