"""Static checks for examples/pilot (the scripted pilot and its drill).

The examples run only inside the engine, so their behaviour is measured with the drill, not here
(examples/pilot/README.md). What can be checked without an engine is checked: the Lua parses
(luajit, which shares the engine's Lua 5.1 grammar), every library function it calls exists in this
build's API (lua_check, when an api_docs cache is present), and the docs were finished.
Honest limit: none of this sees fields or methods on handles (`ship.Foo`, `w:bar()`); a misspelt
handle field still passes here and aborts the engine (probe-results section 22).
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from fsoctl import lua_check

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples" / "pilot"
LUA_FILES = sorted(EXAMPLES.glob("*.lua"))
LUAJIT = shutil.which("luajit")


def test_examples_exist():
    # Guards the parametrized tests below: an empty glob would make them silently collect nothing.
    names = {p.name for p in LUA_FILES}
    assert {"pilot.lua", "drill.lua", "sm1_status.lua"} <= names


@pytest.mark.skipif(not LUAJIT, reason="luajit is not installed")
@pytest.mark.parametrize("path", LUA_FILES, ids=lambda p: p.name)
def test_example_lua_parses(path, tmp_path):
    # `luajit -b` compiles to bytecode, which fails on any syntax error without running the code.
    assert LUAJIT is not None                      # narrowed for the type checker; skipif covers it
    out = subprocess.run([LUAJIT, "-b", str(path), str(tmp_path / "out.bc")],
                         capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr


def _cached_api() -> Path | None:
    """The newest cached scripting.json from api_docs, or None (tests then skip)."""
    found = sorted((Path.home() / ".cache" / "fso-harness" / "docs").glob("*/scripting.json"),
                   key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


@pytest.mark.skipif(_cached_api() is None, reason="no api_docs cache on this machine")
@pytest.mark.parametrize("path", LUA_FILES, ids=lambda p: p.name)
def test_example_lua_uses_known_library_functions(path):
    problems, _ = lua_check.check(path.read_text(), _cached_api())
    assert problems == []


# Globals the examples may touch: the engine's libraries and enums, Lua's standard library, and
# the two state tables the scripts own. A name outside this list is almost always a local used
# before its `local` declaration (Lua then reads a nil global; the pilot hit this with its strike
# state on 2026-10-15) or a typo, and it fails only when that line first runs in the engine.
ALLOWED_GLOBALS = {
    "A1", "DR", "ba", "mn", "hv", "hu", "tb", "gr", "opt", "engine",
    "BUTTON_CONTROL_LUA_ADDITIVE", "BUTTON_CONTROL_NORMAL", "FLIGHT_CONTROL_LUA_FULL",
    "FLIGHT_CONTROL_NORMAL", "ORDER_ATTACK",
    "math", "string", "table", "ipairs", "pairs", "next", "pcall", "tostring", "type",
}


@pytest.mark.skipif(not LUAJIT, reason="luajit is not installed")
@pytest.mark.parametrize("path", LUA_FILES, ids=lambda p: p.name)
def test_example_lua_touches_only_known_globals(path):
    assert LUAJIT is not None                      # narrowed for the type checker; skipif covers it
    out = subprocess.run([LUAJIT, "-bl", str(path)], capture_output=True, text=True, timeout=30,
                         check=False)
    assert out.returncode == 0, out.stderr
    used = set(re.findall(r'G(?:GET|SET)\s.*"(\w+)"', out.stdout))
    assert used, "no globals found: the bytecode listing format changed"
    assert used <= ALLOWED_GLOBALS, sorted(used - ALLOWED_GLOBALS)


def test_readme_has_no_placeholder():
    text = (EXAMPLES / "README.md").read_text()
    assert "RESULTS_TABLE" not in text


def test_agent_guide_points_at_the_examples():
    # The MCP instructions send agents to the "Fly a mission" recipe; the recipe must still exist
    # and must name the example, or the measured controller is undiscoverable.
    guide = (ROOT / "docs" / "AGENT-GUIDE.md").read_text()
    assert "## Fly a mission" in guide
    assert "examples/pilot/" in guide


def test_setup_instructions_do_not_replace_a1():
    # `A1 = { cfg = ... }` after an earlier load wipes the flags that stop a second hook (QA SL-8).
    for path in (EXAMPLES / "README.md", EXAMPLES / "pilot.lua"):
        assert "A1 = { cfg" not in path.read_text(), path.name
