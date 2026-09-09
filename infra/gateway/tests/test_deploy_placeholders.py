# SPDX-License-Identifier: Apache-2.0
"""The deploy/aws/ placeholder tokens are delimited, and a blanket substitution is safe (CTO-360).

The bug this suite exists to hold shut. ``deploy/aws/README.md`` tells an operator to render the
task definitions with a blanket ``sed``. The placeholders used to be the bare words ``ACCOUNT`` and
``REGION``, and ``gateway.taskdef.json`` carries the environment variable NAMES ``AWS_REGION`` and
``TALLY_REPLAY_S3_REGION``, so ``s/REGION/us-east-1/g`` rewrote those names into ``AWS_us-east-1``
and ``TALLY_REPLAY_S3_us-east-1``. The output was still valid JSON, ``register-task-definition``
accepted it, the task started, and both settings were simply absent: the AWS default credential
chain ran with no region and the S3 replay store with no region, with nothing in the logs naming
the cause.

The fix is the delimiters, not a cleverer ``sed``, so the test asserts the property rather than the
recipe: every placeholder is ``__DELIMITED__``, and substituting every one of them with a blanket
replace leaves a document whose variable names are untouched.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
ECS_DIR = REPO_ROOT / "deploy" / "aws" / "ecs"

# What an operator substitutes. Values are deliberately the shapes that used to collide: a region
# and an account id that appear as substrings of nothing, and a bucket name built from the account.
SUBSTITUTIONS = {
    "__ACCOUNT__": "123456789012",
    "__REGION__": "us-east-1",
    "__REPLAY_BUCKET__": "123456789012-ai-tally-replay",
    "__KMS_KEY_ID__": "abcd1234-1111-2222-3333-444455556666",
    "__CLICKHOUSE_HOST__": "abc123.us-east-1.aws.clickhouse.cloud",
    "__CLICKHOUSE_URL__": "https://abc123.us-east-1.aws.clickhouse.cloud:8443",
    "__GATEWAY_URL__": "https://ingest.example.com",
}

# Anything that looks like a placeholder: a run of capitals/underscores standing alone as a word.
# The point is to catch a NEW bare-word token being added, not to enumerate today's tokens.
BARE_TOKEN = re.compile(r"(?<![A-Za-z0-9_])(ACCOUNT|REGION|REPLACE_[A-Z0-9_]+)(?![A-Za-z0-9_])")

DOCUMENTS = sorted(ECS_DIR.rglob("*.json"))


def test_documents_are_present() -> None:
    # A path typo would otherwise make every parametrised test below vacuously pass.
    assert DOCUMENTS, f"no JSON documents under {ECS_DIR}"


@pytest.mark.parametrize("path", DOCUMENTS, ids=lambda p: p.name)
def test_no_bare_word_placeholders(path: pathlib.Path) -> None:
    text = path.read_text()
    found = sorted(set(BARE_TOKEN.findall(text)))
    assert not found, (
        f"{path.relative_to(REPO_ROOT)} carries the bare-word placeholder(s) {found}. "
        "Use the delimited form (__ACCOUNT__, __REGION__, ...): a bare word is a substring of real "
        "identifiers such as AWS_REGION, and the README's blanket sed rewrites those names."
    )


@pytest.mark.parametrize("path", DOCUMENTS, ids=lambda p: p.name)
def test_blanket_substitution_renders_valid_json_and_leaves_nothing_behind(
    path: pathlib.Path,
) -> None:
    text = path.read_text()
    for token, value in SUBSTITUTIONS.items():
        text = text.replace(token, value)

    rendered = json.loads(text)
    leftover = re.findall(r"__[A-Z0-9_]+__", json.dumps(rendered))
    assert not leftover, (
        f"{path.relative_to(REPO_ROOT)} has placeholder(s) {sorted(set(leftover))} that no "
        "documented substitution fills in. Add it to the README recipe and to SUBSTITUTIONS here."
    )


def test_gateway_region_variables_survive_substitution() -> None:
    """The specific regression: both region settings keep their names and gain their values."""
    text = (ECS_DIR / "gateway.taskdef.json").read_text()
    for token, value in SUBSTITUTIONS.items():
        text = text.replace(token, value)

    container = json.loads(text)["containerDefinitions"][0]
    environment = {entry["name"]: entry["value"] for entry in container["environment"]}

    for name in ("AWS_REGION", "TALLY_REPLAY_S3_REGION"):
        assert name in environment, f"{name} was renamed by the substitution"
        assert environment[name] == SUBSTITUTIONS["__REGION__"]

    corrupted = [name for name in environment if SUBSTITUTIONS["__REGION__"] in name]
    assert not corrupted, f"substitution rewrote variable NAMES: {corrupted}"
