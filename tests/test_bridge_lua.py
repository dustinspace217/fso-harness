"""Unit tier for the bridge Lua: runs tests/lua/run.lua under luajit, one pytest case per Lua test.

The Lua tests drive the real fsoharness.lua against stand-ins for the engine libraries
(tests/lua/stubs.lua), so bridge logic is checked without launching the engine (QA finding TA-10).
Skipped when luajit is not installed; the engine itself runs LuaJIT-compatible Lua 5.1.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from fsoctl import paths

LUAJIT = shutil.which("luajit")
RUNNER = Path(__file__).parent / "lua" / "run.lua"
BRIDGE = paths.bridge_source() / "data" / "scripts" / "fsoharness.lua"


def _names() -> list[str]:
    """Ask the Lua runner for its test names (empty when luajit is missing)."""
    if not LUAJIT:
        return []
    out = subprocess.run([LUAJIT, str(RUNNER), str(BRIDGE), "--list"], capture_output=True,
                         text=True, timeout=30, check=False)
    # A failing runner yields no names; test_lua_tier_lists_its_tests then fails instead of the
    # whole module erroring at collection.
    return out.stdout.split() if out.returncode == 0 else []


# The Lua tier had 43 tests when this floor was set. WHY a floor: a runner that lists nothing
# (a syntax error in run.lua, a broken --list) would otherwise turn the whole tier into zero
# parametrized cases, which pytest reports as a skip, not a failure (QA finding TA2-9).
MIN_LUA_TESTS = 70


@pytest.mark.skipif(not LUAJIT, reason="luajit is not installed")
def test_lua_tier_lists_its_tests():
    names = _names()
    assert len(names) >= MIN_LUA_TESTS, f"only {len(names)} Lua tests listed"
    # Every test( ... ) in run.lua must be listed: five tests appended BELOW the runner block
    # (whose --list branch calls os.exit) were defined after the listing and never ran (QA TA-1).
    defined = RUNNER.read_text().count('\ntest("')
    assert len(names) == defined, f"{defined} tests defined in run.lua, {len(names)} listed"


@pytest.mark.skipif(not LUAJIT, reason="luajit is not installed")
@pytest.mark.parametrize("name", _names())
def test_bridge_lua(name):
    r = subprocess.run([LUAJIT, str(RUNNER), str(BRIDGE), name], capture_output=True, text=True,
                       timeout=60, check=False)
    assert r.returncode == 0, r.stderr or r.stdout
