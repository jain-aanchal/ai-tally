# SPDX-License-Identifier: Apache-2.0
"""Export the public SDK surface as JSON for the docs site (CTO-371, CTO-375).

The docs at ai-tally.com/docs render SDK signatures from this file rather than retyping them, so a
renamed argument or a changed default shows up as a diff here instead of as a silently wrong page.
``tests/test_sdk_reference.py`` fails when the committed copy is stale, which is what keeps the two
in step.

Usage (from sdk/python):
    uv run python scripts/sdk_reference.py            # rewrite docs/public-api/sdk-reference.json
    uv run python scripts/sdk_reference.py --check    # exit 1 if the committed copy is stale
"""

from __future__ import annotations

import argparse
import dataclasses
import inspect
import json
import sys
from pathlib import Path
from typing import Any

import tally
from tally.client import TallyClient
from tally.pricing import Usage

OUTPUT = Path(__file__).resolve().parents[3] / "docs" / "public-api" / "sdk-reference.json"

# The customer-facing surface. Listed explicitly, not taken from ``tally.__all__`` wholesale, so
# adding an export is a deliberate docs decision and removing a documented one fails loudly here.
TOP_LEVEL = [
    "init",
    "flush",
    "uninstrument",
    "get_client",
    "with_account",
    "start_trace",
    "with_trace_context",
    "hash_account",
]

# The module-level record_* helpers take ``**kwargs`` and forward to the global client, so their
# real keyword arguments live on TallyClient. Documenting the wrapper's ``**kwargs`` would tell a
# reader nothing.
RECORD_FUNCTIONS = [
    "record_llm_call",
    "record_tool_call",
    "record_vector_call",
    "record_embedding_call",
]


def _param(p: inspect.Parameter) -> dict[str, Any]:
    return {
        "name": p.name,
        "kind": p.kind.name.lower(),
        "annotation": None if p.annotation is inspect.Parameter.empty else str(p.annotation),
        "default": None if p.default is inspect.Parameter.empty else repr(p.default),
        "required": p.default is inspect.Parameter.empty
        and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD),
    }


def _describe(name: str, fn: Any, *, drop_self: bool = False, source: str) -> dict[str, Any]:
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if drop_self and params and params[0].name == "self":
        params = params[1:]
        sig = sig.replace(parameters=params)
    empty = inspect.Signature.empty
    returns = None if sig.return_annotation is empty else str(sig.return_annotation)
    return {
        "name": f"tally.{name}",
        "source": source,
        "signature": f"{name}{sig}",
        "parameters": [_param(p) for p in params],
        "returns": returns,
        "docstring": inspect.getdoc(fn),
    }


def build_reference() -> dict[str, Any]:
    missing = [n for n in TOP_LEVEL + RECORD_FUNCTIONS if n not in tally.__all__]
    if missing:
        raise RuntimeError(f"documented SDK names are no longer exported from tally: {missing}")

    functions = [
        _describe(n, getattr(tally, n), source=f"tally.{n}") for n in TOP_LEVEL
    ]
    functions += [
        _describe(
            n,
            getattr(TallyClient, n),
            drop_self=True,
            source=f"tally.client.TallyClient.{n} (tally.{n} forwards its keyword arguments)",
        )
        for n in RECORD_FUNCTIONS
    ]
    return {
        "generated_from": "sdk/python/src/tally",
        "sdk_version": tally.__version__,
        "install": (
            'pip install "git+https://github.com/jain-aanchal/ai-tally'
            '#subdirectory=sdk/python"'
        ),
        "functions": functions,
        "types": [
            {
                "name": "tally.pricing.Usage",
                "fields": [
                    {"name": f.name, "annotation": str(f.type), "default": repr(f.default)}
                    for f in dataclasses.fields(Usage)
                ],
                "docstring": inspect.getdoc(Usage),
            }
        ],
    }


def render() -> str:
    return json.dumps(build_reference(), indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the committed copy is stale")
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text() != text:
            hint = "uv run python scripts/sdk_reference.py"
            print(f"{OUTPUT} is stale; run: {hint}", file=sys.stderr)
            return 1
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(text)
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
