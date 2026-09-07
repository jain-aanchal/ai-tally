# SPDX-License-Identifier: Apache-2.0
"""Turn a proposal into files on disk inside the working clone (CTO-261 section 3 step 4).

The only module that writes anything, and it writes only inside the clone that the run
deletes afterwards. Nothing here composes code: every block comes from the catalog through
:mod:`onboarding_bot.propose`, so this is placement and indentation, not generation.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

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


def apply_proposal(repo_dir: Path, proposal: Proposal) -> list[str]:
    """Write every edit into the clone. Returns the repo-relative paths touched."""
    by_path: dict[str, list[ProposedEdit]] = defaultdict(list)
    for edit in proposal.edits:
        by_path[edit.path].append(edit)

    touched: list[str] = []
    for rel_path, edits in sorted(by_path.items()):
        target = _resolve_inside(repo_dir, rel_path)
        lines = target.read_text(encoding="utf-8").splitlines()
        # Descending so an insertion never shifts a later edit's line number. Two edits on
        # the same line insert in reverse proposal order, which leaves them in proposal
        # order in the file: tally.init() must land above the middleware that assumes it.
        ordered = sorted(
            enumerate(edits), key=lambda pair: (pair[1].line_no, pair[0]), reverse=True
        )
        for _, edit in ordered:
            if edit.line_no < 0 or edit.line_no > len(lines):
                raise SecurityViolation(
                    f"edit for {rel_path} targets line {edit.line_no}, outside the file"
                )
            lines[edit.line_no : edit.line_no] = _block(edit)
        # Only active blocks pull in imports: an import added for a commented-out block
        # would be an unused import the developer's own linter then flags.
        active_imports = {i for e in edits if not e.holes_to_fill for i in e.imports_to_add}
        lines = _ensure_imports(lines, sorted(active_imports))
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        touched.append(rel_path)
    return touched


def _block(edit: ProposedEdit) -> list[str]:
    """Indent a generated block to its call site and mark it for review."""
    commented = bool(edit.holes_to_fill)
    out = ["", f"{edit.indent}{MARKER}"]
    if commented:
        out.append(f"{edit.indent}{HOLE_NOTICE}")
    for line in edit.code.rstrip("\n").splitlines():
        if not line.strip():
            out.append("")
        elif commented and not line.lstrip().startswith("#"):
            out.append(f"{edit.indent}# {line}")
        else:
            out.append(f"{edit.indent}{line}")
    return out


def _ensure_imports(lines: list[str], imports: list[str]) -> list[str]:
    """Add each import once, after the file's existing imports.

    An import already present is left alone: re-adding it would be a diff line that says
    nothing, and this diff is read by a human.
    """
    result = list(lines)
    for statement in imports:
        if any(line.strip() == statement for line in result):
            continue
        result.insert(_import_insertion_point(result), statement)
    return result


def _import_insertion_point(lines: list[str]) -> int:
    """After the last top-level import, else after the module docstring and shebang."""
    last_import = -1
    for i, line in enumerate(lines):
        if line.startswith(("import ", "from ")):
            last_import = i
    if last_import >= 0:
        return last_import + 1

    i = 0
    if i < len(lines) and lines[i].startswith("#!"):
        i += 1
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith("#")):
        i += 1
    if i < len(lines) and lines[i].lstrip().startswith(('"""', "'''")):
        quote = lines[i].lstrip()[:3]
        # A single-line docstring opens and closes on the same line.
        if lines[i].strip().endswith(quote) and len(lines[i].strip()) > 3:
            return i + 1
        i += 1
        while i < len(lines) and quote not in lines[i]:
            i += 1
        return min(i + 1, len(lines))
    return i


def _resolve_inside(repo_dir: Path, rel_path: str) -> Path:
    """Refuse any path that escapes the clone, so a crafted repo cannot steer a write out."""
    root = repo_dir.resolve()
    target = (root / rel_path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise SecurityViolation(f"refusing to write outside the working clone: {rel_path!r}")
    return target
