# SPDX-License-Identifier: Apache-2.0
"""Detect step: read the working clone, hold nothing (CTO-261 section 3 step 1, section 9).

The scan produces manifest text and call-site records for the proposal step and returns
them in memory only. Nothing here writes source anywhere, and :class:`CallSite` keeps the
matched line in ``source_line`` precisely so the proposal step can consume it and drop it:
what leaves the run is a path, a line number and generated code, never the developer's
source (section 9).

Detection itself is delegated to :func:`onboarding_mcp.detect.detect_stack` and the same
catalog every delivery form reads (section 4.1), so the PR bot cannot drift from the
MCP server's answers.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from onboarding_mcp.catalog import Recipe, RecipeCatalog, get_catalog
from onboarding_mcp.detect import detect_stack

MANIFEST_FILES = (
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "poetry.lock",
    "Pipfile",
    "setup.py",
    "setup.cfg",
)

# Directories that are never the developer's own code. Skipping them keeps the scan cheap
# and stops the bot proposing edits inside a vendored dependency.
_SKIP_DIRS = frozenset(
    {".git", ".venv", "venv", "env", "node_modules", "site-packages", "build", "dist", ".tox"}
)

# Caps. A scan that grows without bound is both a slow run and more source held in memory
# than the job needs.
_MAX_FILES = 2000
_MAX_EXCERPT_CHARS = 200_000

# Lines that mean "this is where the process starts", for placing tally.init(). Ordered:
# an app factory or an app object beats a __main__ guard, because that is where a web app
# is actually constructed.
_STARTUP_PATTERNS = ("FastAPI(", "Flask(", "def create_app(", "if __name__ ==")

# Directories and file names that are never the process the developer deploys. tally.init()
# landing in a test fixture would instrument the test run and leave the real entrypoint
# unmetered, silently, which is worse than reporting that no entrypoint was found (CTO-261).
_NON_ENTRYPOINT_DIRS = frozenset(
    {"tests", "test", "testing", "examples", "example", "samples", "docs", "doc",
     "scripts", "benchmarks", "bench", "fixtures", "migrations"}
)


@dataclass(frozen=True)
class CallSite:
    """One matched call site inside the clone. ``source_line`` never leaves the run."""

    path: str
    line_no: int
    indent: str
    source_line: str
    recipe_id: str


@dataclass(frozen=True)
class WeakMatch:
    """A pattern hit the bot refuses to turn into an edit, with the reason it refused.

    A substring hit is not evidence a line belongs to the detected library: ``.query(`` is
    SQLAlchemy's as often as Pinecone's. These reach the PR as gaps so the developer can
    place the block, rather than as an edit that meters the wrong thing (CTO-261).
    """

    path: str
    line_no: int
    recipe_id: str
    reason: str


def python_files(repo_dir: Path) -> list[Path]:
    """Every ``.py`` file that is plausibly the developer's own code."""
    found: list[Path] = []
    for path in sorted(repo_dir.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.relative_to(repo_dir).parts):
            continue
        found.append(path)
        if len(found) >= _MAX_FILES:
            break
    return found


def read_manifests(repo_dir: Path) -> str:
    """Concatenate the dependency manifests detect_stack reads (section 3 step 1)."""
    chunks: list[str] = []
    for name in MANIFEST_FILES:
        path = repo_dir / name
        if path.is_file():
            chunks.append(f"# {name}\n{_read(path)}")
    return "\n".join(chunks)


def import_excerpts(files: list[Path], repo_dir: Path) -> str:
    """Import lines plus the lines that look like call sites.

    Import lines alone miss a vector call reached through a helper, and whole files are
    more source than detection needs; the middle ground is the lines that carry a signal.
    """
    out: list[str] = []
    size = 0
    for path in files:
        for line in _read(path).splitlines():
            stripped = line.strip()
            if not (
                stripped.startswith(("import ", "from "))
                or any(p in line for p in _STARTUP_PATTERNS)
                or any(tok in stripped for tok in (".query(", ".search(", ".upsert(", ".embed("))
            ):
                continue
            out.append(stripped)
            size += len(stripped)
            if size >= _MAX_EXCERPT_CHARS:
                return "\n".join(out)
    return "\n".join(out)


def detect(repo_dir: Path, *, catalog: RecipeCatalog | None = None) -> dict[str, Any]:
    """Run the shared detector over the clone (section 4.1: one catalog, all three forms)."""
    files = python_files(repo_dir)
    return detect_stack(
        read_manifests(repo_dir),
        import_excerpts(files, repo_dir),
        catalog=catalog or get_catalog(),
    )


def find_call_sites(
    repo_dir: Path,
    recipe_ids: list[str],
    *,
    catalog: RecipeCatalog | None = None,
    limit: int = 25,
) -> tuple[list[CallSite], list[WeakMatch]]:
    """Locate the real call sites the matched record_* recipes apply to (section 3 step 4).

    Only recipes that emit an SDK call are placed. A middleware or startup recipe has its
    own placement and is handled by the proposal step, not here.

    Matching is done on the parsed call expression, not on raw text, so a hit inside a
    comment or a string literal is not a call site. On top of that a match counts only when
    the file imports the library the recipe is for: without that check a repo with both
    pinecone and sqlalchemy gets ``record_vector_call(provider="pinecone")`` after
    ``session.query(...)``. Everything that fails either test is returned as a
    :class:`WeakMatch` for the PR to report.
    """
    cat = catalog or get_catalog()
    sites: list[CallSite] = []
    weak: list[WeakMatch] = []
    placeable = []
    for recipe_id in recipe_ids:
        recipe = cat.get(recipe_id)
        if recipe is None or recipe.kind not in ("vector", "tool", "embedding"):
            continue
        if not recipe.sdk_surface.get("call") or not recipe.call_patterns:
            continue
        placeable.append(recipe)

    for path in python_files(repo_dir):
        text = _read(path)
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            # A file the bot cannot parse is a file it cannot place an edit in safely.
            continue
        lines = text.splitlines()
        rel = str(path.relative_to(repo_dir))
        imported = _imported_roots(tree)
        reported: set[str] = set()
        for line_no, recipe in sorted(_call_matches(tree, placeable), key=lambda m: m[0]):
            if not (set(recipe.imports) & imported):
                if recipe.id not in reported:
                    reported.add(recipe.id)
                    weak.append(
                        WeakMatch(
                            path=rel,
                            line_no=line_no,
                            recipe_id=recipe.id,
                            reason=(
                                f"{rel}:{line_no} looks like a {recipe.id} call site, but this "
                                f"file imports none of {', '.join(recipe.imports)}. The bot "
                                f"does not attribute a call to a library the file never "
                                f"imported; place the block by hand if it belongs here"
                            ),
                        )
                    )
                continue
            line = lines[line_no - 1] if line_no <= len(lines) else ""
            sites.append(
                CallSite(
                    path=rel,
                    line_no=line_no,
                    indent=line[: len(line) - len(line.lstrip())],
                    source_line=line.strip(),
                    recipe_id=recipe.id,
                )
            )
            if len(sites) >= limit:
                return sites, weak
    return sites, weak


def _imported_roots(tree: ast.Module) -> set[str]:
    """Root module names this file imports, which is what binds a match to a library."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _call_matches(tree: ast.Module, recipes: list[Recipe]) -> list[tuple[int, Recipe]]:
    """Line numbers where a parsed call or decorator matches a recipe's patterns.

    Only patterns that describe a call (``.query(``, ``Tool(``) or a decorator (``@tool``)
    are matched. A pattern that is neither, such as pgvector's ``<->`` SQL operator, cannot
    be recognised in the syntax tree and is left to the developer rather than matched as
    text inside a string.
    """
    out: list[tuple[int, Recipe]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            try:
                expr = ast.unparse(node.func) + "("
            except Exception:  # pragma: no cover - unparse is total for parsed trees
                continue
            for recipe in recipes:
                if any(_expression_matches(expr, p) for p in recipe.call_patterns):
                    out.append((node.lineno, recipe))
                    break
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names = {f"@{ast.unparse(d)}" for d in node.decorator_list}
            for recipe in recipes:
                if any(p.startswith("@") and p in names for p in recipe.call_patterns):
                    out.append((node.lineno, recipe))
                    break
    return out


def _expression_matches(expr: str, pattern: str) -> bool:
    """Match a call pattern against an unparsed call expression, on a name boundary.

    ``.query(`` matches ``index.query(`` and not ``index.subquery(``; ``Tool(`` matches
    ``Tool(`` and ``mod.Tool(`` and not ``MyTool(``.
    """
    if not pattern.endswith("("):
        return False
    if not expr.endswith(pattern):
        return False
    prefix = expr[: -len(pattern)]
    if pattern.startswith("."):
        # The pattern carries its own attribute boundary; it just needs something to be an
        # attribute of.
        return bool(prefix)
    return not prefix or prefix.endswith(".")


def find_startup_site(repo_dir: Path) -> CallSite | None:
    """Find where the process starts, so tally.init() lands before the first LLM call.

    Returns ``None`` when no startup line is recognisable. That is a gap the PR reports,
    not a file the bot picks arbitrarily (section 2 decision 2). Tests, examples and scripts
    are excluded: they are not the process the developer runs in production.
    """
    candidates = [p for p in python_files(repo_dir) if _is_entrypoint_candidate(p, repo_dir)]
    for pattern in _STARTUP_PATTERNS:
        for path in candidates:
            for line_no, line in enumerate(_read(path).splitlines(), start=1):
                if pattern in line:
                    indent = line[: len(line) - len(line.lstrip())]
                    # A module-level app object takes the module's indentation; a factory or
                    # a __main__ guard needs its body indented one level in.
                    if pattern in ("def create_app(", "if __name__ =="):
                        indent += "    "
                    return CallSite(
                        path=str(path.relative_to(repo_dir)),
                        line_no=line_no,
                        indent=indent,
                        source_line=line.strip(),
                        recipe_id="startup.tally.init",
                    )
    return None


def find_app_object_site(repo_dir: Path, framework: str) -> CallSite | None:
    """Find the module-level web-app object the middleware attaches to.

    Module level specifically: the middleware recipes are module-scope definitions, so
    inserting them after an app built inside a factory would be a syntax error. No
    module-level app object means the middleware is reported for manual placement instead
    of inserted somewhere it does not belong.
    """
    pattern = {"fastapi": "FastAPI(", "flask": "Flask("}.get(framework.lower())
    if pattern is None:
        return None
    for path in python_files(repo_dir):
        for line_no, line in enumerate(_read(path).splitlines(), start=1):
            if pattern in line and not line.startswith((" ", "\t")) and "=" in line:
                return CallSite(
                    path=str(path.relative_to(repo_dir)),
                    line_no=line_no,
                    indent="",
                    source_line=line.strip(),
                    recipe_id=f"middleware.{framework.lower()}.account",
                )
    return None


def _is_entrypoint_candidate(path: Path, repo_dir: Path) -> bool:
    """True when a file could plausibly be the deployed process, not a test or a sample."""
    parts = path.relative_to(repo_dir).parts
    if any(part.lower() in _NON_ENTRYPOINT_DIRS for part in parts[:-1]):
        return False
    name = parts[-1].lower()
    return not (name.startswith("test_") or name.endswith(("_test.py", "_tests.py")))


def _read(path: Path) -> str:
    """Read a repo file tolerantly. A file the bot cannot decode is skipped, never guessed."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
