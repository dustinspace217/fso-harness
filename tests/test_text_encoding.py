"""Text files are read and written as UTF-8 BY NAME, everywhere in the package.

WHY a test (QA Plan B, CR-12 / TA-15 / ST-12): Windows' default text encoding is the local
code page, and the first Windows run of the unit tier found three faults of that kind. The
lint rule that was switched on for it (ruff PLW1514) sees `open()` but not
`Path.read_text()` / `Path.write_text()` on a value whose type it cannot work out: 22 such
calls named no encoding while three documents said every one did. This test reads the
syntax tree, so it sees them all."""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted([*(ROOT / "fsoctl").rglob("*.py"), *(ROOT / "mcp").glob("*.py")])


def _names_no_encoding(call: ast.Call, own_open: bool = False) -> bool:
    """True for a call that reads or writes text without saying in which encoding.
    `own_open`: the module defines a function named open itself (fred.py's verb), so a bare
    `open(...)` there is not the built-in."""
    if any(k.arg == "encoding" for k in call.keywords):
        return False
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in ("read_text", "write_text"):
        return True
    if isinstance(func, ast.Name) and func.id == "open" and not own_open:
        mode = call.args[1] if len(call.args) > 1 else next(
            (k.value for k in call.keywords if k.arg == "mode"), None)
        return not (isinstance(mode, ast.Constant) and "b" in str(mode.value))
    return False


def test_there_are_sources_to_check():
    assert len(SOURCES) > 40 and any(p.name == "session.py" for p in SOURCES)


@pytest.mark.parametrize("path", SOURCES, ids=[str(p.relative_to(ROOT)) for p in SOURCES])
def test_every_text_read_and_write_names_its_encoding(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    own_open = any(isinstance(node, ast.FunctionDef) and node.name == "open"
                   for node in tree.body)
    missing = [f"{path.relative_to(ROOT)}:{node.lineno}" for node in ast.walk(tree)
               if isinstance(node, ast.Call) and _names_no_encoding(node, own_open)]
    assert missing == []


def test_the_check_itself_tells_the_cases_apart():
    def flagged(source):
        return [_names_no_encoding(n) for n in ast.walk(ast.parse(source))
                if isinstance(n, ast.Call)]
    assert flagged("p.read_text()") == [True]
    assert flagged("p.write_text(x)") == [True]
    assert flagged("p.read_text(encoding='utf-8')") == [False]
    assert flagged("open(p)") == [True]
    assert flagged("open(p, 'rb')") == [False]
    assert flagged("open(p, mode='wb')") == [False]
    assert flagged("open(p, 'a', encoding='utf-8')") == [False]
    assert flagged("os.open(p, 0)") == [False]
