# SPDX-License-Identifier: Apache-2.0
"""MCP protocol binding for the onboarding tools (CTO-261 section 4.2).

Thin: it maps the plain functions in this package onto MCP tools. The tools are fully
usable and testable without this module (they are ordinary functions); this only wires
them to an MCP transport for a developer's coding agent to call. The ``mcp`` package is
an optional runtime dependency, imported lazily so the SDK test suite needs no MCP stack.
"""

from __future__ import annotations

import importlib.util
from typing import Any

from onboarding_mcp import (
    coverage_report,
    detect_stack,
    explain_layer,
    generate_middleware,
    generate_startup,
    get_recipe,
    instrument_call_site,
)

SERVER_NAME = "ai-tally-onboarding"

# The section 4.2 tool names, in registration order. Kept as data so the wiring is
# assertable without an MCP transport installed (tests/test_onboarding_mcp_server.py).
TOOL_NAMES = (
    "detect_stack",
    "get_recipe",
    "generate_startup",
    "generate_middleware",
    "instrument_call_site",
    "explain_layer",
    "coverage_report",
)


_MISSING_MCP = (
    "the 'mcp' package is required to run the onboarding MCP server; install it with "
    "`pip install 'tally-sdk[mcp]'`. That extra also brings pyyaml, which the recipe "
    "catalog needs, so the onboarding tools are importable and usable only with it "
    "(onboarding_mcp is in the base wheel purely so this entrypoint resolves)."
)


def _mcp_installed() -> bool:
    """True when a top-level ``mcp`` package is importable in this environment."""
    try:
        return importlib.util.find_spec("mcp") is not None
    except (ImportError, ValueError):
        # ValueError: sys.modules holds a None entry for mcp (how tests simulate absence).
        return False


def load_server_class() -> Any:
    """Return the MCP server class from whichever ``mcp`` generation is installed.

    mcp 2.x renamed FastMCP to MCPServer, so pinning either name alone leaves half the
    installed base unable to launch the server. Both expose the ``tool()`` decorator and
    ``run()`` this module uses, so trying 2.x first and falling back to 1.x is enough.
    Neither present is an honest RuntimeError, never a degraded no-op server.
    """
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2

        return MCPServer
    except ImportError:
        # The mcp 2.x module imports siblings of its own, so a partial or broken mcp-2
        # install raises ImportError for a reason that is NOT "mcp is not installed".
        # Swallowing it fell through to the 1.x path and reported "install the mcp
        # package" for an environment where mcp IS installed, the exact misleading error
        # this work set out to remove. Only a genuinely absent mcp falls through
        # (CTO-261 review finding 5).
        if _mcp_installed():
            raise
    try:
        from mcp.server.fastmcp import FastMCP  # mcp 1.x

        return FastMCP
    except ImportError as exc:
        if _mcp_installed():
            raise
        raise RuntimeError(_MISSING_MCP) from exc


def build_server(server_class: Any = None) -> Any:
    """Build the MCP server with the section 4.2 tools registered.

    ``server_class`` is an injection seam for tests: the registration wiring is the part
    worth asserting, and it must be assertable without an MCP transport installed.
    Tool names are pinned to the section 4.2 table (``TOOL_NAMES``) rather than taken
    from the Python function names, so the surface a coding agent sees matches the spec.
    """
    cls = server_class or load_server_class()
    server = cls(SERVER_NAME)

    @server.tool(name="detect_stack")
    def detect_stack_tool(manifest: str = "", import_excerpts: str = "") -> dict[str, Any]:
        """Detect providers, frameworks, vector DBs, and web framework from a manifest."""
        return detect_stack(manifest, import_excerpts)

    @server.tool(name="get_recipe")
    def get_recipe_tool(name: str) -> dict[str, Any]:
        """Return the machine-readable recipe for a recipe id or framework / provider name."""
        return get_recipe(name)

    @server.tool(name="generate_startup")
    def generate_startup_tool(feature_tag: str | None = None) -> dict[str, Any]:
        """Generate the tally.init() startup snippet the proposed diff opens with."""
        return generate_startup(feature_tag)

    @server.tool(name="generate_middleware")
    def generate_middleware_tool(
        web_framework: str, account_source: str, feature_tag: str | None = None
    ) -> dict[str, Any]:
        """Generate account / feature middleware plus the startup snippet, bound to the answer."""
        return generate_middleware(web_framework, account_source, feature_tag)

    @server.tool(name="instrument_call_site")
    def instrument_call_site_tool(call_site: str, recipe_id: str) -> dict[str, Any]:
        """Adapt a recipe's record_* edit to a concrete call site."""
        return instrument_call_site(call_site, recipe_id)

    @server.tool(name="explain_layer")
    def explain_layer_tool(query: str) -> dict[str, Any]:
        """Explain which record_* method covers a layer, grounded on the SDK surface."""
        return explain_layer(query)

    @server.tool(name="coverage_report")
    def coverage_report_tool(tenant_key: str) -> dict[str, Any]:
        """Per-layer coverage (stubbed against the spec contract; probe ships later)."""
        return coverage_report(tenant_key)

    return server


def main() -> None:  # pragma: no cover - entrypoint, exercised by launching the server
    """Console entrypoint (``tally-onboarding-mcp``). Runs the server over stdio."""
    build_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()
