# SPDX-License-Identifier: Apache-2.0
"""Placement: a generated block lands on a statement boundary or not at all (CTO-261).

The bot commits and pushes what it writes into a customer's repository, so "the branch
still compiles" is not a nicety here. These tests cover the shapes that broke the
line-number insertion: multi-line calls, decorators, nested brackets and suite headers,
plus the compile() safety net that refuses to commit a file it could not patch cleanly.
"""

from __future__ import annotations

from pathlib import Path

from onboarding_bot.patch import MARKER, apply_proposal
from onboarding_bot.propose import Proposal, ProposedEdit, build_proposal

MULTILINE_APP = '''\
"""Demo service."""
import os

from fastapi import FastAPI
from pinecone import Pinecone

app = FastAPI(
    title="demo",
)
index = Pinecone(api_key=os.environ["PINECONE_KEY"]).Index("docs")


@app.post("/ask")
def ask(question: str):
    hits = index.query(
        vector=[0.1],
        top_k=3,
    )
    return hits
'''


def _repo(tmp_path: Path, source: str, name: str = "app/main.py") -> Path:
    repo = tmp_path / "repo"
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    (repo / "requirements.txt").write_text("fastapi\nopenai\npinecone\n")
    target.write_text(source)
    return repo


def _edit(**kwargs) -> ProposedEdit:
    base = {
        "path": "app/main.py",
        "line_no": 1,
        "kind": "startup",
        "recipe_id": "startup.tally.init",
        "code": "tally.init(feature_tag=None)\n",
        "imports_to_add": ["import tally"],
        "placement": "startup",
        "indent": "",
    }
    base.update(kwargs)
    return ProposedEdit(**base)


def test_blocks_never_land_inside_a_multi_line_call(tmp_path: Path):
    # The exact shape that produced a branch that did not compile: both blocks were
    # inserted between `FastAPI(` and `title="demo",`.
    repo = _repo(tmp_path, MULTILINE_APP)
    proposal = build_proposal(
        repo, account_source='request.headers.get("X-Customer-Id")', feature_tag="ask"
    )
    result = apply_proposal(repo, proposal)
    assert result.files_changed == ["app/main.py"]

    patched = (repo / "app" / "main.py").read_text()
    compile(patched, "main.py", "exec")
    lines = patched.splitlines()
    # Nothing was inserted between the open paren and the argument it belongs to.
    open_paren = lines.index("app = FastAPI(")
    assert lines[open_paren + 1].strip() == 'title="demo",'
    assert lines[open_paren + 2].strip() == ")"
    assert MARKER in patched
    assert "tally.init(" in patched


def test_a_block_after_a_multi_line_call_site_lands_after_the_closing_paren(tmp_path: Path):
    repo = _repo(tmp_path, MULTILINE_APP)
    proposal = build_proposal(repo, account_source="x")
    apply_proposal(repo, proposal)
    patched = (repo / "app" / "main.py").read_text()
    compile(patched, "main.py", "exec")
    lines = patched.splitlines()
    # The record_* block sits after the call's closing paren, inside the function, at the
    # call's own indentation.
    marker = next(i for i, line in enumerate(lines) if MARKER in line and line.startswith("    "))
    # A blank line, then the marked block: the call's closing paren is immediately above.
    assert [line.strip() for line in lines[marker - 2 : marker]] == [")", ""]
    assert lines[marker].startswith("    #")


def test_a_suite_header_match_puts_the_block_inside_the_suite(tmp_path: Path):
    source = 'import openai\n\n\nif __name__ == "__main__":\n    print("go")\n'
    repo = _repo(tmp_path, source, name="main.py")
    proposal = build_proposal(repo, account_source=None)
    apply_proposal(repo, proposal)
    patched = (repo / "main.py").read_text()
    compile(patched, "main.py", "exec")
    # Inside the guard, not after it: init has to run in the process that starts here.
    body = patched.split('if __name__ == "__main__":\n', 1)[1]
    assert "    tally.init(" in body


def test_a_decorated_call_site_keeps_the_decorator_attached_to_its_function(tmp_path: Path):
    source = (
        "import functools\n"
        "from pinecone import Pinecone\n\n"
        "index = Pinecone().Index('docs')\n\n\n"
        "@functools.cache\n"
        "def ask(q):\n"
        "    hits = index.query(vector=[0.1], top_k=2)\n"
        "    return hits\n"
    )
    repo = _repo(tmp_path, source, name="svc.py")
    proposal = build_proposal(repo, account_source=None)
    apply_proposal(repo, proposal)
    patched = (repo / "svc.py").read_text()
    compile(patched, "svc.py", "exec")
    assert "@functools.cache\ndef ask(q):" in patched, "nothing was inserted between the two"


def test_nested_brackets_do_not_confuse_the_boundary(tmp_path: Path):
    source = (
        "from pinecone import Pinecone\n\n"
        "index = Pinecone().Index('docs')\n\n\n"
        "def ask(q):\n"
        "    hits = index.query(\n"
        "        vector=[[0.1, 0.2], [0.3]],\n"
        "        filter={'a': ('b', 'c')},\n"
        "        top_k=2,\n"
        "    )\n"
        "    return hits\n"
    )
    repo = _repo(tmp_path, source, name="svc.py")
    proposal = build_proposal(repo, account_source=None)
    apply_proposal(repo, proposal)
    patched = (repo / "svc.py").read_text()
    compile(patched, "svc.py", "exec")
    assert "        top_k=2,\n    )\n" in patched


def test_a_file_that_would_not_compile_is_left_alone_and_reported_as_a_gap(tmp_path: Path):
    repo = _repo(tmp_path, "import os\n\nx = 1\n", name="svc.py")
    original = (repo / "svc.py").read_text()
    # A block the catalog would never emit, standing in for any future recipe or placement
    # bug: the safety net is that broken output is never committed.
    proposal = Proposal(
        detection={},
        edits=[_edit(path="svc.py", line_no=3, code="def (\n")],
    )
    result = apply_proposal(repo, proposal)

    assert result.files_changed == []
    assert result.applied == []
    assert (repo / "svc.py").read_text() == original
    assert any("would not compile" in g["reason"] for g in result.gaps)


def test_a_non_utf8_file_is_a_gap_and_does_not_stop_the_other_files(tmp_path: Path):
    repo = _repo(tmp_path, "import os\n\nx = 1\n", name="ok.py")
    (repo / "bad.py").write_bytes(b"# \xff\xfe not utf-8\nx = 1\n")
    proposal = Proposal(
        detection={},
        edits=[
            _edit(path="bad.py", line_no=2),
            _edit(path="ok.py", line_no=3),
        ],
    )
    result = apply_proposal(repo, proposal)

    assert result.files_changed == ["ok.py"]
    assert any("not decodable as UTF-8" in g["reason"] for g in result.gaps)
    assert (repo / "bad.py").read_bytes().startswith(b"# \xff\xfe")


def test_line_endings_and_a_missing_trailing_newline_survive_the_patch(tmp_path: Path):
    repo = _repo(tmp_path, "x = 1\r\ny = 2", name="crlf.py")
    proposal = Proposal(detection={}, edits=[_edit(path="crlf.py", line_no=1)])
    apply_proposal(repo, proposal)

    raw = (repo / "crlf.py").read_bytes()
    assert b"\n" not in raw.replace(b"\r\n", b""), "CRLF was not rewritten to LF"
    assert not raw.endswith(b"\r\n"), "a trailing newline was not invented"


def test_an_import_inside_a_docstring_is_not_mistaken_for_the_files_imports(tmp_path: Path):
    source = '"""Usage:\n\nimport somelib\n"""\nimport os\n\nx = os.getpid()\n'
    repo = _repo(tmp_path, source, name="doc.py")
    proposal = Proposal(detection={}, edits=[_edit(path="doc.py", line_no=7)])
    apply_proposal(repo, proposal)

    patched = (repo / "doc.py").read_text()
    compile(patched, "doc.py", "exec")
    lines = patched.splitlines()
    # After the real import, not after the one quoted in the docstring.
    assert lines.index("import tally") == lines.index("import os") + 1


def test_an_unknown_placement_is_reported_rather_than_placed(tmp_path: Path):
    repo = _repo(tmp_path, "x = 1\n", name="svc.py")
    proposal = Proposal(
        detection={},
        edits=[_edit(path="svc.py", line_no=1, placement="somewhere_new")],
    )
    result = apply_proposal(repo, proposal)
    assert result.applied == []
    assert any("does not know how to place" in g["reason"] for g in result.gaps)


def test_module_scope_middleware_is_not_inserted_inside_a_function(tmp_path: Path):
    source = "def build():\n    app = object()\n    return app\n"
    repo = _repo(tmp_path, source, name="svc.py")
    proposal = Proposal(
        detection={},
        edits=[
            _edit(
                path="svc.py",
                line_no=2,
                kind="account",
                recipe_id="middleware.fastapi.account",
                placement="wrap_handler",
                code="@app.middleware('http')\nasync def mw(request, call_next):\n    pass\n",
            )
        ],
    )
    result = apply_proposal(repo, proposal)
    assert result.applied == []
    assert any("no safe statement boundary" in g["reason"] for g in result.gaps)
