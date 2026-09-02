# SPDX-License-Identifier: Apache-2.0
"""MCP protocol binding for the onboarding tools (CTO-261 section 4.2).

Thin: it maps the plain functions in this package onto MCP tools. The tools are fully
usable and testable without this module (they are ordinary functions); this only wires
them to an MCP transport for a developer's coding agent to call. The ``mcp`` package is
an optional runtime dependency, imported lazily so the SDK test suite needs no MCP stack.
"""

from __future__ import annotations

from typing import Any

from onboarding_mcp import (
    coverage_report,
    detect_stack,
    explain_layer,
    generate_middleware,
    get_recipe,
    instrument_call_site,
)

SERVER_NAME = "ai-tally-onboarding"


def build_server() -> Any:
    """Build the FastMCP server with the section 4.2 tools registered.

    Raises a clear error if the optional ``mcp`` package is not installed, rather than
    failing at import time (so importing this module for its tool wiring stays cheap).
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - exercised only without the mcp extra
        raise RuntimeError(
            "the 'mcp' package is required to run the onboarding MCP server; install it "
            "with `pip install mcp`. The tools themselves are importable without it."
        ) from exc

    server = FastMCP(SERVER_NAME)

    @server.tool()
    def detect_stack_tool(manifest: str = "", import_excerpts: str = "") -> dict[str, Any]:
        """Detect providers, frameworks, vector DBs, and web framework from a manifest."""
        return detect_stack(manifest, import_excerpts)

    @server.tool()
    def get_recipe_tool(name: str) -> dict[str, Any]:
        """Return the machine-readable recipe for a recipe id or framework / provider name."""
        return get_recipe(name)

    @server.tool()
    def generate_middleware_tool(
        web_framework: str, account_source: str, feature_tag: str | None = None
    ) -> dict[str, Any]:
        """Generate account / feature middleware bound to the confirmed account resolver."""
        return generate_middleware(web_framework, account_source, feature_tag)

    @server.tool()
    def instrument_call_site_tool(call_site: str, recipe_id: str) -> dict[str, Any]:
        """Adapt a recipe's record_* edit to a concrete call site."""
        return instrument_call_site(call_site, recipe_id)

    @server.tool()
    def explain_layer_tool(query: str) -> dict[str, Any]:
        """Explain which record_* method covers a layer, grounded on the SDK surface."""
        return explain_layer(query)

    @server.tool()
    def coverage_report_tool(tenant_key: str) -> dict[str, Any]:
        """Per-layer coverage (stubbed against the spec contract; probe ships later)."""
        return coverage_report(tenant_key)

    return server


def main() -> None:  # pragma: no cover - entrypoint
    build_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()
