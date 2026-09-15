# SPDX-License-Identifier: Apache-2.0
"""Write docs/public-api/public-openapi.json from the gateway app (CTO-371, CTO-375).

Usage (from infra/gateway):
    uv run python scripts/export_public_openapi.py           # rewrite the committed spec
    uv run python scripts/export_public_openapi.py --check   # exit 1 if it is stale

Building the schema imports the app but never starts its lifespan, so no ClickHouse, Postgres or
network is needed.
"""

from __future__ import annotations

import argparse
import sys

from gateway.app import app
from gateway.public_openapi import OUTPUT, render


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export the public OpenAPI spec for the docs.")
    parser.add_argument("--check", action="store_true", help="fail if the committed spec is stale")
    args = parser.parse_args(argv)
    text = render(app.openapi())
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text() != text:
            print(
                f"{OUTPUT} is stale; run: uv run python scripts/export_public_openapi.py",
                file=sys.stderr,
            )
            return 1
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(text)
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
