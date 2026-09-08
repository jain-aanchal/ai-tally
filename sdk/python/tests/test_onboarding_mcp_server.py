# SPDX-License-Identifier: Apache-2.0
"""MCP protocol binding tests (CTO-261 sections 4.2, 12 P1).

The P1 "Done when" is a developer's coding agent actually connecting to the server, so
the wiring itself has to be covered: every section 4.2 tool registered under its spec
name, each delegating to the plain function, the declared console entrypoint resolving,
and an honest RuntimeError (never a degraded no-op server) when ``mcp`` is absent.

The registration assertions run without an MCP transport installed by injecting a
recording stand-in for the server class, which is why ``build_server`` takes one.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import tomllib
from onboarding_mcp import server as server_mod

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


class RecordingServer:
    """Stand-in for FastMCP / MCPServer: records what ``build_server`` registers."""

    def __init__(self, name: str):
        self.name = name
        self.tools: dict[str, Any] = {}

    def tool(self, name: str | None = None):
        def decorator(fn):
            self.tools[name or fn.__name__] = fn
            return fn

        return decorator


@pytest.fixture()
def built() -> RecordingServer:
    return server_mod.build_server(server_class=RecordingServer)


def test_server_registers_every_section_4_2_tool(built: RecordingServer):
    assert built.name == server_mod.SERVER_NAME
    # Registered under the spec's names, not the Python function names.
    assert tuple(built.tools) == server_mod.TOOL_NAMES


def test_every_registered_tool_has_a_docstring(built: RecordingServer):
    # MCP clients show the docstring as the tool description; a blank one is unusable.
    assert all(fn.__doc__ for fn in built.tools.values())


def test_registered_tools_delegate_to_the_plain_functions(built: RecordingServer):
    detect = built.tools["detect_stack"]("fastapi==0.115.0\npinecone-client==5.0.0\n")
    assert "vector.pinecone.query" in detect["matched_recipes"]

    recipe = built.tools["get_recipe"]("vector.pinecone.query")
    assert recipe["sdk_surface"]["call"] == "tally.record_vector_call"

    startup = built.tools["generate_startup"]("chatbot")
    assert "tally.init(" in startup["code"]

    middleware = built.tools["generate_middleware"](
        "fastapi", 'request.headers.get("X-Customer-Id")', "chatbot"
    )
    assert "with_account" in middleware["code"]
    assert "tally.init(" in middleware["startup"]["code"]

    site = built.tools["instrument_call_site"](
        "index.query(vector=v, top_k=5)", "vector.pinecone.query"
    )
    assert site["sdk_call"] == "tally.record_vector_call"

    assert built.tools["explain_layer"]("vector")["operation_name"] == "vector"
    assert built.tools["coverage_report"]("tally_sk_live_x")["probe_available"] is False


def test_unknown_input_stays_a_gap_through_the_tool_layer(built: RecordingServer):
    # The honesty invariant must survive the protocol binding, not only the plain call.
    assert built.tools["get_recipe"]("cassandra")["gap"] is True
    assert built.tools["generate_middleware"]("fastapi", "  ")["gap"] is True


def _simulate_mcp_absent(monkeypatch) -> None:
    """Make mcp look uninstalled regardless of what this environment actually has.

    The None entries make the submodule imports fail the way an uninstalled extra does;
    the top-level None entry is what ``_mcp_installed`` reads, so the "is it really
    missing?" check agrees instead of re-raising (CTO-261 review finding 5).
    """
    monkeypatch.setitem(sys.modules, "mcp", None)
    monkeypatch.setitem(sys.modules, "mcp.server.mcpserver", None)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", None)


def test_missing_mcp_dependency_raises_a_clear_runtime_error(monkeypatch):
    _simulate_mcp_absent(monkeypatch)
    with pytest.raises(RuntimeError) as excinfo:
        server_mod.load_server_class()
    message = str(excinfo.value)
    assert "mcp" in message
    assert "tally-sdk[mcp]" in message


def test_build_server_propagates_the_missing_dependency_error(monkeypatch):
    _simulate_mcp_absent(monkeypatch)
    with pytest.raises(RuntimeError):
        server_mod.build_server()


def test_broken_mcp_install_reraises_instead_of_claiming_mcp_is_missing(monkeypatch):
    # Finding 5: the mcp 2.x module imports siblings of its own, so a partial or broken
    # mcp-2 install raises ImportError for a reason other than "not installed". The old
    # bare `except ImportError: pass` fell through and told the developer to install a
    # package they already had. With mcp importable, the real ImportError must surface.
    monkeypatch.setitem(sys.modules, "mcp.server.mcpserver", None)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", None)
    monkeypatch.setattr(server_mod, "_mcp_installed", lambda: True)
    with pytest.raises(ImportError):
        server_mod.load_server_class()


def test_mcp_extra_requires_a_version_that_actually_ships_fastmcp():
    # FastMCP was merged into the official mcp SDK in 1.2.0; the 1.0.0 wheel has no
    # fastmcp module, so a resolver picking 1.0.x / 1.1.x would produce the misleading
    # "install the mcp package" error for an installed mcp (finding 3).
    pyproject = tomllib.loads(PYPROJECT.read_text())
    mcp_extra = pyproject["project"]["optional-dependencies"]["mcp"]
    pins = [p for p in mcp_extra if p.startswith("mcp")]
    assert pins == ["mcp>=1.2"], f"unexpected mcp pin: {pins}"


def test_missing_mcp_message_does_not_claim_the_tools_import_without_the_extra():
    # Finding 4: onboarding_mcp is in the base wheel, but catalog.py needs pyyaml, which
    # only the mcp extra brings. The message must not claim otherwise.
    message = server_mod._MISSING_MCP
    assert "importable without it" not in message
    assert "pyyaml" in message


def test_declared_console_entrypoint_resolves():
    # The P1 blocker was a server with no way to launch it. Resolve exactly what
    # pyproject declares, so a renamed main() or a dropped script fails here.
    pyproject = tomllib.loads(PYPROJECT.read_text())
    target = pyproject["project"]["scripts"]["tally-onboarding-mcp"]
    module_path, _, attr = target.partition(":")
    module = __import__(module_path, fromlist=[attr])
    assert callable(getattr(module, attr))


def test_onboarding_mcp_is_packaged_in_the_wheel():
    # Without this the console entrypoint above cannot import at install time.
    pyproject = tomllib.loads(PYPROJECT.read_text())
    packages = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert "onboarding_mcp" in packages
    assert "mcp" in pyproject["project"]["optional-dependencies"]
    # The catalog is data the server reads, so it has to ship too (catalog.py resolves
    # the installed location first, the in-tree one second).
    force_include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert force_include["recipes"] == "onboarding_mcp/recipes"
