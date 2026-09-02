# SPDX-License-Identifier: Apache-2.0
"""ai-tally onboarding MCP server (CTO-261 section 4.2).

A thin server over the recipe catalog (``sdk/python/recipes/``) and the real SDK
surface. The developer's own coding agent (Claude Code, Cursor) connects to it and
applies what it returns locally; source never leaves the developer's machine
(section 9 security posture). The server holds only recipes and generated code,
never a repo.

The MCP tools (section 4.2) are exposed as plain, importable functions here so they
are unit-testable without an MCP transport; :mod:`onboarding_mcp.server` binds them
to the MCP protocol.
"""

from onboarding_mcp.catalog import Recipe, RecipeCatalog, get_catalog
from onboarding_mcp.detect import detect_stack
from onboarding_mcp.generate import (
    coverage_report,
    explain_layer,
    generate_middleware,
    get_recipe,
    instrument_call_site,
)

__all__ = [
    "Recipe",
    "RecipeCatalog",
    "get_catalog",
    "detect_stack",
    "get_recipe",
    "generate_middleware",
    "instrument_call_site",
    "explain_layer",
    "coverage_report",
]
