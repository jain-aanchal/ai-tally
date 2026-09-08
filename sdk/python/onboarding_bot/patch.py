# SPDX-License-Identifier: Apache-2.0
"""Turn a proposal into files on disk inside the working clone (CTO-261 section 3 step 4).

The only module that writes anything, and it writes only inside the clone that the run
deletes afterwards. Nothing here composes code: every block comes from the catalog through
:mod:`onboarding_bot.propose`, so this is placement and indentation, not generation.

Placement is a real question, not a line-number increment. A matched line is often the
first line of a longer statement (``app = FastAPI(\\n    title="demo",\\n)``), and inserting
after it would land the block between a call's open paren and its arguments, producing a
branch that does not compile. Every insertion point here is a statement boundary resolved
with :mod:`ast`, every edit's ``placement`` is honoured, and nothing is written until the
patched file has been through :func:`compile`. A file that would not compile is reported as
a gap and left untouched, because pushing broken code into a customer repo is the one
failure this component must not have (CLAUDE.md: honest under uncertainty).
"""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .guards import SecurityViolation
from .propose import Proposal, ProposedEdit

# The marker that makes an inserted block obvious in review. The diff is the product here;
# a reviewer should never have to guess which lines the bot added.
MARKER = "# ai-tally onboarding bot (CTO-261): review this block before merging."

# A block with an unfilled hole is inserted commented out. The alternative would be to
# invent a value for the hole, which is exactly what CLAUDE.md forbids, or to write
# `index=<FILL:index_name>` into the file, which is not valid Python and would break the
# repo the PR is trying to help. Inactive and visibly incomplete is the honest third option.
HOLE_NOTICE = (
    "# INCOMPLETE: fill the <FILL:...> values below and uncomment. The bot leaves them "
    "blank rather than guessing."
)

# Placements the catalog emits. An unrecognised placement is refused rather than guessed at:
# a block put somewhere its recipe did not intend is a block that meters nothing.
_KNOWN_PLACEMENTS = frozenset({"startup", "wrap_handler", "after_call", "config"})

# Statement nodes that own an indented suite. A match on one of these headers belongs
# inside the suite (``if __name__ == "__main__":`` wants tally.init() as its first
# statement), never after the whole block.
_SUITE_FIELDS = ("body", "orelse", "finalbody")


@dataclass
class PatchResult:
    """What was written and what could not be.

    ``gaps`` carries the same reported-gap shape the rest of the run uses, so a file the
    patcher refuses to touch reaches the PR body as a gap instead of vanishing.
    """

    files_changed: list[str] = field(default_factory=list)
    applied: list[ProposedEdit] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)


def apply_proposal(repo_dir: Path, proposal: Proposal) -> PatchResult:
    """Write every edit into the clone. Returns what landed and what was refused.

    Nothing is written until every target file has been patched in memory and compiled, so
    a file the bot cannot patch safely leaves the clone exactly as it found it.
    """
    by_path: dict[str, list[ProposedEdit]] = defaultdict(list)
    for edit in proposal.edits:
        by_path[edit.path].append(edit)

    result = PatchResult()
    pending: list[tuple[Path, str]] = []
    for rel_path, edits in sorted(by_path.items()):
        target = _resolve_inside(repo_dir, rel_path)
        source = _read_source(target)
        if source is None:
            result.gaps.append(
                _gap(
                    f"{rel_path} is not decodable as UTF-8, so the bot did not rewrite it; "
                    f"the edits for this file were dropped rather than written blind"
                )
            )
            continue
        patched, applied, gaps = _patch_file(rel_path, source, edits)
        result.gaps.extend(gaps)
        if not applied:
            continue
        pending.append((target, patched))
        result.files_changed.append(rel_path)
        result.applied.extend(applied)

    for target, text in pending:
        # newline="" so the line endings assembled above survive the write verbatim: a
        # whole-file CRLF rewrite would be a diff on every line of the developer's file.
        with target.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    return result


def _gap(reason: str, **extra: Any) -> dict[str, Any]:
    """The one reported-gap shape, matching onboarding_mcp.generate._gap."""
    return {"gap": True, "reason": reason, **extra}


def _read_source(path: Path) -> str | None:
    """Read a file the bot is about to rewrite, strictly.

    Strict on purpose, unlike the tolerant scan read: a lossy decode here would be written
    back and would silently corrupt the developer's file. Undecodable means not patched.
    """
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _patch_file(
    rel_path: str, source: str, edits: list[ProposedEdit]
) -> tuple[str, list[ProposedEdit], list[dict[str, Any]]]:
    """Patch one file in memory. Returns the new text, the edits that landed, and gaps."""
    newline = "\r\n" if "\r\n" in source else "\n"
    ends_with_newline = source.endswith(("\n", "\r"))
    lines = source.splitlines()
    spans = _statement_spans(source)

    gaps: list[dict[str, Any]] = []
    applied: list[ProposedEdit] = []
    # (index, order) descending so an insertion never shifts a later one's index. Two edits
    # at the same index insert in reverse proposal order, which leaves them in proposal
    # order in the file: tally.init() must land above the middleware that assumes it.
    plan: list[tuple[int, int, list[str]]] = []
    for order, edit in enumerate(edits):
        if edit.line_no < 1 or edit.line_no > len(lines):
            raise SecurityViolation(
                f"edit for {rel_path} targets line {edit.line_no}, outside the file"
            )
        if edit.placement not in _KNOWN_PLACEMENTS:
            gaps.append(
                _gap(
                    f"{rel_path}:{edit.line_no} recipe {edit.recipe_id} asks for placement "
                    f"{edit.placement!r}, which this bot does not know how to place safely",
                    recipe_id=edit.recipe_id,
                    code=edit.code,
                )
            )
            continue
        resolved = _resolve_insertion(lines, spans, edit)
        if resolved is None:
            gaps.append(
                _gap(
                    f"{rel_path}:{edit.line_no} has no safe statement boundary for a "
                    f"{edit.placement!r} block, so it was not inserted; place the code below "
                    f"by hand",
                    recipe_id=edit.recipe_id,
                    code=edit.code,
                )
            )
            continue
        index, indent = resolved
        plan.append((index, order, _block(edit, indent)))
        applied.append(edit)

    if not applied:
        return source, [], gaps

    # Only active blocks pull in imports: an import added for a commented-out block would be
    # an unused import the developer's own linter then flags.
    active_imports = sorted({i for e in applied if not e.holes_to_fill for i in e.imports_to_add})
    import_index = _import_insertion_point(lines, spans)
    for offset, statement in enumerate(active_imports):
        if any(line.strip() == statement for line in lines):
            continue
        plan.append((import_index, -1_000_000 + offset, [statement]))

    patched_lines = list(lines)
    for index, _order, block in sorted(plan, key=lambda item: (item[0], item[1]), reverse=True):
        patched_lines[index:index] = block
    text = newline.join(patched_lines) + (newline if ends_with_newline else "")

    if rel_path.endswith(".py"):
        try:
            compile(text, rel_path, "exec")
        except SyntaxError as exc:
            # The safety net. A block that would not compile is never committed, and the
            # reason travels to the PR body instead of to the customer's CI.
            gaps.append(
                _gap(
                    f"{rel_path} would not compile after the generated blocks were inserted "
                    f"({exc.msg} at line {exc.lineno}), so the file was left unchanged and "
                    f"nothing was committed for it",
                    recipe_ids=[e.recipe_id for e in applied],
                )
            )
            return source, [], gaps
    return text, applied, gaps


# -- insertion points ------------------------------------------------------- #


@dataclass(frozen=True)
class _Span:
    """One statement's extent, plus where its suite starts and whether it is module level."""

    start: int
    end: int
    body_start: int | None
    module_level: bool


def _statement_spans(source: str) -> list[_Span] | None:
    """Every statement in the file as a 1-based line span. ``None`` if it does not parse."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    spans: list[_Span] = []
    _collect_spans(tree.body, spans, module_level=True)
    return spans


def _collect_spans(body: list[ast.stmt], spans: list[_Span], *, module_level: bool) -> None:
    for node in body:
        start = node.lineno
        # Decorators sit above the `def` line but belong to the same statement: a match on
        # `@app.post("/ask")` must resolve to the function, not to module scope.
        for decorator in getattr(node, "decorator_list", []) or []:
            start = min(start, decorator.lineno)
        end = getattr(node, "end_lineno", None) or node.lineno
        suites = [
            getattr(node, name, None) for name in _SUITE_FIELDS
        ] + [h.body for h in getattr(node, "handlers", [])] + [
            c.body for c in getattr(node, "cases", [])
        ]
        suites = [s for s in suites if s]
        body_start = min(s[0].lineno for s in suites) if suites else None
        spans.append(_Span(start=start, end=end, body_start=body_start, module_level=module_level))
        for suite in suites:
            _collect_spans(suite, spans, module_level=False)


def _resolve_insertion(
    lines: list[str], spans: list[_Span] | None, edit: ProposedEdit
) -> tuple[int, str] | None:
    """Where a block goes: a 0-based insertion index plus the indent to write it at."""
    if spans is None:
        return _fallback_insertion(lines, edit)

    containing = [s for s in spans if s.start <= edit.line_no <= s.end]
    if not containing:
        # A match outside every statement is a comment or a blank line. There is no
        # statement to attach to, so nothing is inserted.
        return None
    span = max(containing, key=lambda s: (s.start, -s.end))

    if edit.placement == "wrap_handler" and not span.module_level:
        # Middleware recipes are module-scope definitions. Inserted inside a function they
        # would be a local def that never attaches to the app.
        return None

    if span.body_start is not None and edit.line_no < span.body_start:
        # The match is in a compound statement's header, so the block belongs as the first
        # statement of its suite: `if __name__ == "__main__":` wants init inside the block.
        if edit.placement == "wrap_handler":
            return None
        index = span.body_start - 1
        return index, _indent_of(lines[index])
    return span.end, _indent_of(lines[span.start - 1])


def _fallback_insertion(lines: list[str], edit: ProposedEdit) -> tuple[int, str] | None:
    """Bracket balancing for a file that does not parse (already broken, or not Python).

    Weaker than the ast path and used only where the ast path cannot run. It still refuses
    to insert mid-statement: it walks forward until brackets balance and no continuation is
    open, and gives up rather than guessing if the statement never closes.
    """
    depth = 0
    index = edit.line_no - 1
    while index < len(lines):
        line = lines[index]
        depth += _bracket_delta(line)
        stripped = line.rstrip()
        if depth <= 0 and not stripped.endswith("\\"):
            if stripped.endswith(":"):
                # A suite header: the block goes inside, at the suite's own indent.
                nxt = index + 1
                while nxt < len(lines) and not lines[nxt].strip():
                    nxt += 1
                if nxt >= len(lines):
                    return None
                return nxt, _indent_of(lines[nxt])
            return index + 1, _indent_of(lines[edit.line_no - 1])
        index += 1
    return None


def _bracket_delta(line: str) -> int:
    """Net bracket depth of a line, ignoring brackets inside strings and comments."""
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if line.startswith(quote, i):
                i += len(quote)
                quote = None
                continue
        elif ch in "\"'":
            quote = line[i : i + 3] if line.startswith(line[i] * 3, i) else ch
            i += len(quote)
            continue
        elif ch == "#":
            break
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        i += 1
    return depth


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _block(edit: ProposedEdit, indent: str) -> list[str]:
    """Indent a generated block to its insertion point and mark it for review."""
    commented = bool(edit.holes_to_fill)
    out = ["", f"{indent}{MARKER}"]
    if commented:
        out.append(f"{indent}{HOLE_NOTICE}")
    for line in edit.code.rstrip("\n").splitlines():
        if not line.strip():
            out.append("")
        elif commented and not line.lstrip().startswith("#"):
            out.append(f"{indent}# {line}")
        else:
            out.append(f"{indent}{line}")
    return out


def _import_insertion_point(lines: list[str], spans: list[_Span] | None) -> int:
    """After the file's last top-level import, else after the module docstring and shebang.

    Resolved from the parsed module when possible so an unindented ``import`` inside a
    docstring or a string literal cannot be mistaken for the file's imports.
    """
    try:
        tree = ast.parse("\n".join(lines))
    except (SyntaxError, ValueError):
        tree = None
    if tree is not None:
        last_import = 0
        for node in tree.body:
            if isinstance(node, ast.Import | ast.ImportFrom):
                last_import = max(last_import, getattr(node, "end_lineno", node.lineno))
        if last_import:
            return last_import
        first = tree.body[0] if tree.body else None
        if (
            first is not None
            and isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            return getattr(first, "end_lineno", first.lineno)
        return _prelude_end(lines)
    return _prelude_end(lines)


def _prelude_end(lines: list[str]) -> int:
    """First line after a shebang and any leading comments. Used when the file does not parse."""
    i = 0
    if i < len(lines) and lines[i].startswith("#!"):
        i += 1
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith("#")):
        i += 1
    return i


def _resolve_inside(repo_dir: Path, rel_path: str) -> Path:
    """Refuse any path that escapes the clone, so a crafted repo cannot steer a write out."""
    root = repo_dir.resolve()
    target = (root / rel_path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise SecurityViolation(f"refusing to write outside the working clone: {rel_path!r}")
    return target
