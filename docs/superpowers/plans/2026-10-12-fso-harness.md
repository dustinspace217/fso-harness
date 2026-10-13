# FSO Harness Implementation Plan

## Status (updated 2026-10-12 evening, telemetry + collision alerts)
Phase: post-v1 maintenance (v1 Tasks 1-23 done and published)
Done: v1; demo-findings fix wave + QA; Lua playtest guidance (de45e8c); `lua` pre-send check
(ROUTINE QA, 70/70); switchable telemetry + collision alerts (HIGH-RISK QA + cross-family closed,
manifest 58/58; UNCOMMITTED, awaiting Dustin's word to commit)
Next: Dustin: commit/push, and the Proposal below (throttle after a collision stop); then the
agreed input work (named-action input, relative mouse, keyboard/F-key; joystick OUT)
Blocked: nothing

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `fsoctl` (a stdlib-only Python CLI + library), a Lua bridge mod, and a thin MCP server that let an AI agent build, launch, observe, drive, compare and validate the FreeSpace Open engine and qtFRED headlessly on Linux.

**Architecture:** Each session runs inside a named `systemd --user` scope that contains a runner process, a private Xvfb display, and the engine (or qtFRED) with a private preferences folder. Inside the engine, the bridge mod (Lua, first in `-mod`) polls numbered command files in its cache folder every frame and writes replies; outside, `fsoctl` reads `session.json` fresh on every call (no daemon). The MCP server shells out to `fsoctl --json`.

**Tech Stack:** Python 3.12 (stdlib only for `fsoctl`), Lua 5.1 inside FSO (dkjson built in), Xvfb, xdotool, ImageMagick `import`, tesseract (OCR), gdb + coredumpctl, systemd-run, MCP Python SDK 2.x (`mcp.server.MCPServer`) in an isolated venv, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-10-12-fso-harness-design.md` (approved 2026-10-12). Research: `docs/research/2026-10-12-engine-facts.md`.

## Global Constraints

- Linux only. Python >= 3.12. `fsoctl` imports only the standard library; `mcp` lives only in `mcp/.venv`.
- License: Unlicense ("Match FSO", Dustin 2026-10-12). Commits in this repo keep the Claude co-author trailer (own repo).
- Nothing may reach the user's desktop: every launched process gets `DISPLAY` = the session's Xvfb, `WAYLAND_DISPLAY` unset, `SDL_VIDEODRIVER=x11`, `GDK_BACKEND=x11`, `QT_QPA_PLATFORM=xcb`.
- No vendor defaults: `VK_ICD_FILENAMES` is passed through only if the caller's environment sets it.
- Every engine, qtFRED and build process runs inside a named scope `fso-harness-<id>` with MemoryMax, CPUQuota, TasksMax and RuntimeMaxSec (default 1800 s). Default concurrent sessions cap: 3.
- Every external wait is bounded and fails loudly with a hint (Power of Ten rule 2). No unbounded loops.
- The harness writes only inside its runtime root, `FS2PATH/fso_harness_bridge`, and scratch mods it created (`FS2PATH/fso_harness_scratch_<id>`). It never edits the user's real pilot/settings folder or their mission files.
- The bridge uses only the engine's `cf` file API (never `io.lines`/`io.input`/`io.output`, see research note). No `;` on any Lua code line inside a `.tbm`.
- Free-form `lua` and raw `post_event` are refused unless the session was launched with `allow_eval=true`; every use is appended to `audit.log`.
- Every CLI verb supports `--json` and exits with the codes in `fsoctl/errors.py`; errors carry `message` and `hint`.
- `replay` promises the same commands, not the same results (no fixed timestep; `std::random_device` sources).
- Ruff clean (`ruff check .`) and the default `pytest` (unit tier) green at every commit. Engine tiers always run via `bounded-run`.
- Comments: workspace commenting rules apply to this repo (what + why). The FSO "no comments" rule applies only to code sent upstream into the FSO tree.
- Test environment variables: `FS2PATH` (game dir), `FSO_BIN` (engine binary), `FSO_BIN_DEBUG` (debug engine binary), `QTFRED_BIN`, `FSO_TEST_MODS` (default `MVPS-4.7.3`), `FSO_TEST_MISSION` (default `SM1-01.fs2`, a retail mission with command briefing + briefing).

## Review Focus

1. Engine started but stuck in a loading screen or popup for longer than the readiness timeout: `launch` must return a structured timeout naming the last heartbeat state (or "no heartbeat") and leave the session stoppable, never hang the agent. Test in Task 8.
2. Agent calls a game verb (`sexp`, `pilot`, `state`) on a session whose engine has died: must return EXIT_ENGINE with the exit status and a pointer to `crash_report`, not a 30 s timeout. Test in Task 12.
3. Two `fsoctl` calls on the same session at the same time (MCP + CLI): sequence numbers must not collide and replies must not be read by the wrong caller. Test in Task 6.
4. `stop` on a session whose scope already exited, or `stop` twice: idempotent success with the final status. Test in Task 7.
5. User's FS2PATH contains a stale `fso_harness_bridge` from an older harness version: `launch` must refresh it (version stamp) rather than run old Lua silently. Test in Task 6.

---

## File Structure

```
fso-harness/
  pyproject.toml              package metadata (no runtime deps), console script fsoctl
  ruff.toml, pytest.ini       lint + test tiers (unit default; engine/vulkan/qtfred/slow markers)
  LICENSE                     Unlicense
  README.md                   agent-facing tool reference + human CLI usage
  CLAUDE.md                   project context for future sessions
  fsoctl/
    __init__.py, __main__.py  package + `python -m fsoctl`
    errors.py                 exit codes, HarnessError
    paths.py                  runtime root, session dirs, FS2PATH, bridge paths
    session.py                Session record, atomic load/save, ids, locks, seq allocation
    envsetup.py               child-process environment (toolkit pinning, renderer env)
    scope.py                  systemd-run scope wrapper: caps, start, is_active, stop
    renderer.py               classify loaded graphics libraries from /proc/<pid>/maps
    runner.py                 process that runs INSIDE the scope: Xvfb + engine/qtFRED, writes pids
    launcher.py               LaunchRequest, launch/status/stop/list/cleanup, readiness, stage switches
    channel.py                command/reply files: send, heartbeat, wait_ready
    bridge_install.py         copies the bridge mod into FS2PATH with a version stamp
    bridge/fso_harness_bridge/data/tables/fsoharness-sct.tbm   hook table (thin)
    bridge/fso_harness_bridge/data/scripts/fsoharness.lua      bridge logic (require'd)
    xinput.py                 screenshot (import), keys/click (xdotool XTEST)
    logs.py                   log tail/filter, problems extraction
    game.py                   state, sexp, answer_popup, lua, post_event, pilot, autopilot, time, wait_for
    record.py                 command journal + replay
    build.py                  build a worktree inside a scope
    scratch.py                scratch mod create/cleanup
    fred.py                   qtFRED open/save/camera/select/check
    validate.py               validate (load and quit, collect warnings)
    crash.py                  crash_report
    docs.py                   api_docs (sexps.html, scripting.lua, scripting.json), cached per binary hash
    model_info.py             -pofspew wrapper + parser
    frame_stats.py            -profile_write_file wrapper + percentiles
    vp.py                     VP archive list/extract
    scenario.py               scenario steps: validate and run (used by compare)
    compare.py                compare (two builds) + renderer_compare
    shadowdir.py              symlinked working directory so engine output files never land in FS2PATH
    cli.py                    argparse dispatch, --json output, exit codes
  mcp/server.py, mcp/setup.sh MCP wrapper (stateless) + venv/registration script
  probes/                     Task 1 probe mods and scripts (kept; they document behaviour)
  tests/                      unit tests at top level; engine tests marked
```

## Shared conventions (every task)

- Every public function has a docstring: what it does, what it receives, what it returns (workspace rules).
- `--json` output shape: success `{"ok": true, ...}`; failure `{"ok": false, "code": <int>, "message": str, "hint": str}`.
- Engine tests: `@pytest.mark.engine`, use the `engine_env` fixture (Task 2), skip with a clear reason when `FS2PATH`/`FSO_BIN` are unset, run only under `bounded-run pytest -m engine -p no:xdist`.
- Every new test is run red first (break the code or assert the wrong value, watch it fail, restore) before it counts. Record the red result in the commit message body (`red: <test> failed as expected`).

---

### Task 1: Probes (risks before design hardens)

Throwaway-but-kept probe mods answering the spec's §12 risks plus two found in research. Results go into `docs/research/2026-10-12-probe-results.md`; each result either confirms the spec's mechanism or names the fallback the later task must use. A probe that contradicts an approved spec mechanism is surfaced to Dustin as a `Proposal:` line before the owning task starts (plan-binding rule).

**Files:**
- Create: `probes/README.md`, `probes/probe_mod/data/tables/probe-sct.tbm`, `probes/run_probe.sh`, `docs/research/2026-10-12-probe-results.md`

**Interfaces:**
- Consumes: research facts doc.
- Produces: confirmed Lua API names/behaviour used by Tasks 6, 9, 11, 12, 13; the generated `docs/research/scripting.lua` (engine's own Lua API stubs) referenced by every bridge task.

- [ ] **Step 1: Generate the engine's Lua API reference for this build**

```bash
mkdir -p /tmp/claude/fsoh-docs && cd "$FS2PATH"
timeout 120 env SDL_VIDEODRIVER=dummy "$FSO_BIN" -output_script_lua -output_sexps -nosound -nomovies -noninteractive -window & sleep 60; kill %1
cp scripting.lua sexps.html docs/research/ && rm -f scripting.lua sexps.html
```
Expected: `docs/research/scripting.lua` contains `--- @class` lines for `ba`, `mn`, `ui`, `cf`. If the dummy video driver refuses to start, rerun under Xvfb with `DISPLAY=:9x`. (Engine flags write to the working directory and do not exit; research §8.)

- [ ] **Step 2: Write `probes/run_probe.sh`** (launch the engine on a private Xvfb with a private prefs dir and the probe mod first in `-mod`, run N seconds, print the probe's log file)

```bash
#!/usr/bin/env bash
# usage: run_probe.sh <seconds> <mission|-> [extra fso args...]   env: FS2PATH FSO_BIN [FSO_TEST_MODS]
set -u
SECS=$1; MISSION=$2; shift 2
D=$(mktemp -d /tmp/claude/fsoh-probe.XXXX); mkdir -p "$D/prefs"
rm -rf "$FS2PATH/fsoh_probe"; cp -r "$(dirname "$0")/probe_mod" "$FS2PATH/fsoh_probe"
Xvfb :91 -screen 0 1280x800x24 -nolisten tcp >/dev/null 2>&1 & XP=$!
sleep 1
unset WAYLAND_DISPLAY
export DISPLAY=:91 SDL_VIDEODRIVER=x11 GDK_BACKEND=x11 QT_QPA_PLATFORM=xcb LIBGL_ALWAYS_SOFTWARE=1 FSO_PREFERENCES_PATH="$D/prefs/"
cd "$FS2PATH" || exit 1
ARGS=(-mod "fsoh_probe,${FSO_TEST_MODS:-MVPS-4.7.3}" -window -res 1280x720 -nosound -nomovies -no_unfocused_pause)
[ "$MISSION" != "-" ] && ARGS+=(-start_mission "$MISSION")
"$FSO_BIN" "${ARGS[@]}" "$@" > "$D/engine.out" 2>&1 < /dev/null & EP=$!
sleep "$SECS"; kill $EP 2>/dev/null; sleep 1; kill -9 $EP 2>/dev/null; kill $XP
echo "== probe log"; cat "$D/prefs/fsoh_probe/data/cache/probe.log" 2>/dev/null
echo "== engine tail"; tail -5 "$D/engine.out"
rm -rf "$FS2PATH/fsoh_probe"
```

- [ ] **Step 3: Write the probe table** `probes/probe_mod/data/tables/probe-sct.tbm`. It logs every state change, answers every popup with its default after logging the popup's hook variables, creates and selects a pilot named `ProbePilot` in pilot select, skips the command briefing and briefing, and after 10 s of mission time logs: dkjson availability, a control-info write, `set-time-compression 400` effect on mission time, and pause/resume via game events.

```lua
#Conditional Hooks
$Application: FS2_Open
$On Game Init: [
	PrbLast = ""
	PrbT0 = nil
	PrbDone = {}
	function PrbLog(s)
		local f = cf.openFile("probe.log", "a", "data/cache")
		if f and f:isValid() then
			f:write(s .. "\n")
			f:close()
		end
	end
	local ok, j = pcall(require, "dkjson")
	PrbLog("dkjson=" .. tostring(ok) .. " type=" .. type(j))
]
$On Dialog Frame: [
	local txt = "?"
	if hv.Text then txt = tostring(hv.Text) end
	PrbLog("popup text=" .. txt .. " choices=" .. tostring(hv.Choices))
	hv.Submit(0)
]
$On Frame: [
	local st = ba.getCurrentGameState()
	local name = "none"
	if st and st:isValid() then name = st.Name end
	if name ~= PrbLast then
		PrbLog("state=" .. name)
		PrbLast = name
	end
	if name == "GS_STATE_INITIAL_PLAYER_SELECT" and not PrbDone.pilot then
		PrbDone.pilot = true
		local c = ui.PilotSelect.createPilot("ProbePilot")
		PrbLog("createPilot=" .. tostring(c))
		local s = ui.PilotSelect.selectPilot("ProbePilot")
		PrbLog("selectPilot=" .. tostring(s))
	end
	if name == "GS_STATE_CMD_BRIEF" and not PrbDone.cmd then
		PrbDone.cmd = true
		ba.postGameEvent(ba.GameEvents["GS_EVENT_START_BRIEFING"])
		PrbLog("posted START_BRIEFING")
	end
	if name == "GS_STATE_BRIEFING" and not PrbDone.brief then
		PrbDone.brief = true
		local r = ui.Briefing.commitToMission()
		PrbLog("commitToMission=" .. tostring(r))
	end
	if name == "GS_STATE_GAME_PLAY" then
		local t = mn.getMissionTime()
		if t > 10 and not PrbDone.ctl then
			PrbDone.ctl = true
			local ci = ba.getControlInfo()
			ci.Pitch = 1.0
			PrbLog("controlinfo pitch set; hv.Player=" .. tostring(hv.Player))
			mn.runSEXP("( set-time-compression 400 )")
			PrbT0 = t
			PrbLog("tc set at t=" .. t)
		end
		if PrbT0 and t > PrbT0 + 8 and not PrbDone.tc then
			PrbDone.tc = true
			PrbLog("8 mission s reached at real frame; t=" .. t)
			ba.postGameEvent(ba.GameEvents["GS_EVENT_PAUSE_GAME"])
			PrbLog("posted PAUSE")
		end
	end
	if name == "GS_STATE_GAME_PAUSED" and not PrbDone.resume then
		PrbDone.resume = true
		ba.postGameEvent(ba.GameEvents["GS_EVENT_PREVIOUS_STATE"])
		PrbLog("posted PREVIOUS_STATE from pause")
	end
]
#End
```

- [ ] **Step 4: Run the probes** (fresh prefs → pilot creation path; retail mission with briefings)

```bash
bounded-run probes/run_probe.sh 150 "$FSO_TEST_MISSION"
```
Expected log lines answer, in order: dkjson availability; popup texts seen and dismissed (new-user tip); `createPilot`/`selectPilot` results; states `GS_STATE_CMD_BRIEF` → `GS_STATE_BRIEFING` → `GS_STATE_GAME_PLAY` without input; control-info write accepted; time compression (8 mission seconds should take ~2 real seconds); pause then resume.

- [ ] **Step 5: Probe the remaining risks**
  - Pilot ordering: rerun with `-pilot ProbePilot` against the prefs dir produced by Step 4 (copy it) and confirm no chooser; run once with a mod that triggers >10 warnings is NOT required (note as untested if no such mod is at hand).
  - `mn.startMission` inverted check (mission.cpp:1941): add a one-shot line in MAIN_MENU `PrbLog("startMission missing=" .. tostring(mn.startMission("does_not_exist.fs2", true)))` on a separate run without `-start_mission`.
  - Vulkan: rerun Step 4 with `VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/nvidia_icd.x86_64.json` and `-vulkan` (needs an engine built with Vulkan) and confirm the same log.
  - Debug Assert/crash: with `$FSO_BIN_DEBUG`, add a probe line `ba.error("probe")` behind a mission-time trigger in a copy of the probe; confirm what `-noninteractive` does (log text, exit signal, `coredumpctl list` entry).
  - qtFRED OCR: open a retail mission in qtFRED on Xvfb, break a reference (delete one ship referenced by an event, confirm Yes), press Ctrl+S, screenshot, run `tesseract shot.png - --psm 6` and check the dialog text is readable.

- [ ] **Step 6: Record results** in `docs/research/2026-10-12-probe-results.md`: one section per risk with the observed log lines, verdict (CONFIRMED / DIFFERENT: <what> / UNTESTED: <why>), and the mechanism each later task must use. Any DIFFERENT that changes an approved mechanism → `Proposal:` to Dustin before the owning task.

- [ ] **Step 7: Commit**

```bash
git add probes docs/research
git commit -m "Probes for bridge mechanisms and recorded results"
```

---

### Task 2: Scaffolding, errors, paths, test fixtures

**Files:**
- Create: `pyproject.toml`, `ruff.toml`, `pytest.ini`, `LICENSE`, `fsoctl/__init__.py`, `fsoctl/__main__.py`, `fsoctl/errors.py`, `fsoctl/paths.py`, `tests/conftest.py`, `tests/test_errors.py`, `tests/test_paths.py`

**Interfaces:**
- Produces: `errors.EXIT_OK=0, EXIT_USAGE=2, EXIT_NOT_FOUND=3, EXIT_LAUNCH=4, EXIT_TIMEOUT=5, EXIT_ENGINE=6, EXIT_REFUSED=7`; `class HarnessError(Exception)` with `.code: int`, `.message: str`, `.hint: str`, `.to_dict() -> dict`; `paths.runtime_root() -> Path`, `paths.session_dir(sid: str) -> Path`, `paths.fs2path() -> Path`, `paths.BRIDGE_MOD = "fso_harness_bridge"`, `paths.bridge_source() -> Path`, `paths.channel_dir(prefs: Path) -> Path`; fixtures `tmp_runtime` (sets `FSO_HARNESS_RUNTIME` to a tmp dir) and `engine_env` (skips unless `FS2PATH` and `FSO_BIN` exist).

- [ ] **Step 1: Write tooling files**

`pyproject.toml`:
```toml
[project]
name = "fso-harness"
version = "0.1.0"
description = "Headless automation harness for FreeSpace Open, built for AI agents"
requires-python = ">=3.12"
license = {text = "Unlicense"}
dependencies = []

[project.scripts]
fsoctl = "fsoctl.cli:main"

[tool.setuptools.packages.find]
include = ["fsoctl*"]

[tool.setuptools.package-data]
fsoctl = ["bridge/**/*"]
```

`ruff.toml` (strict from the first commit, P10 rule 10):
```toml
line-length = 100
target-version = "py312"
[lint]
select = ["E", "F", "W", "B", "SIM", "PL", "RUF"]
ignore = ["PLR2004"]
[lint.pylint]
max-statements = 50
max-branches = 12
```

`pytest.ini`:
```ini
# Default run = unit tier only. A command-line -m REPLACES this -m.
#   pytest                         unit only, no engine processes
#   bounded-run pytest -m engine -p no:xdist     real engine on Xvfb (single worker)
#   bounded-run pytest -m vulkan -p no:xdist     needs a GPU Vulkan driver
#   bounded-run pytest -m qtfred -p no:xdist     real qtFRED on Xvfb
[pytest]
markers =
    engine: launches the real FSO engine
    vulkan: needs a GPU with a Vulkan driver
    qtfred: launches the real qtFRED
    slow: waits out a real production timeout
addopts = -m "not engine and not vulkan and not qtfred"
```

`LICENSE`: the Unlicense text from https://unlicense.org/ (copy verbatim).

- [ ] **Step 2: Write the failing tests**

`tests/test_errors.py`:
```python
from fsoctl.errors import EXIT_TIMEOUT, HarnessError


def test_harness_error_to_dict_carries_code_message_hint():
    err = HarnessError(EXIT_TIMEOUT, "no reply", "check `fsoctl status`")
    assert err.to_dict() == {
        "ok": False, "code": EXIT_TIMEOUT, "message": "no reply", "hint": "check `fsoctl status`",
    }
    assert str(err) == "no reply"
```

`tests/test_paths.py`:
```python
import os
import stat

import pytest

from fsoctl import paths
from fsoctl.errors import EXIT_USAGE, HarnessError


def test_runtime_root_uses_override_and_is_private(tmp_runtime):
    root = paths.runtime_root()
    assert root == tmp_runtime
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700


def test_session_dir_rejects_path_tricks(tmp_runtime):
    with pytest.raises(HarnessError):
        paths.session_dir("../escape")
    assert paths.session_dir("s0123abcd").parent == tmp_runtime


def test_fs2path_requires_env(monkeypatch):
    monkeypatch.delenv("FS2PATH", raising=False)
    with pytest.raises(HarnessError) as e:
        paths.fs2path()
    assert e.value.code == EXIT_USAGE


def test_channel_dir_is_under_first_mod(tmp_path):
    assert paths.channel_dir(tmp_path) == tmp_path / "fso_harness_bridge" / "data" / "cache"


def test_bridge_source_exists_in_package():
    assert (paths.bridge_source() / "data" / "tables").is_dir()
```

`tests/conftest.py`:
```python
import os
from pathlib import Path

import pytest


@pytest.fixture
def tmp_runtime(tmp_path, monkeypatch):
    """Point the harness runtime root at a private temp dir for one test."""
    root = tmp_path / "rt"
    monkeypatch.setenv("FSO_HARNESS_RUNTIME", str(root))
    return root


@pytest.fixture
def engine_env():
    """Return (fs2path, fso_bin) or skip when the real engine isn't configured."""
    fs2 = os.environ.get("FS2PATH")
    fso = os.environ.get("FSO_BIN")
    if not fs2 or not fso or not Path(fso).exists() or not Path(fs2).is_dir():
        pytest.skip("FS2PATH and FSO_BIN must point at a game dir and an engine binary")
    return Path(fs2), Path(fso)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest -q`
Expected: FAIL / import errors (`fsoctl` does not exist yet).

- [ ] **Step 4: Implement `fsoctl/errors.py` and `fsoctl/paths.py`** (plus empty `fsoctl/__init__.py`, `fsoctl/__main__.py` calling `cli.main` once Task 5 adds it — for now `from .cli import main` is deferred; write `__main__.py` in Task 5)

```python
"""Exit codes and the structured error every fsoctl verb raises.

WHY codes: the MCP wrapper and agents branch on the code (timeout vs engine dead vs refused) without
parsing message text; the hint tells the agent the next thing to try.
"""
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_LAUNCH = 4
EXIT_TIMEOUT = 5
EXIT_ENGINE = 6
EXIT_REFUSED = 7


class HarnessError(Exception):
    """An fsoctl failure with an exit code, a message, and a next-step hint."""

    def __init__(self, code: int, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict:
        """Return the JSON shape used by --json output and the MCP wrapper."""
        return {"ok": False, "code": self.code, "message": self.message, "hint": self.hint}
```

```python
"""Where the harness keeps things.

Runtime root: $FSO_HARNESS_RUNTIME (tests) else $XDG_RUNTIME_DIR/fso-harness, mode 0700 so other
users cannot read session channels. Session ids are validated so a caller-supplied id can never
escape the root.
"""
import os
import re
from pathlib import Path

from .errors import EXIT_NOT_FOUND, EXIT_USAGE, HarnessError

BRIDGE_MOD = "fso_harness_bridge"
_SID_RE = re.compile(r"^s[0-9a-f]{8}$")


def runtime_root() -> Path:
    """Return the private runtime root, creating it with mode 0700."""
    override = os.environ.get("FSO_HARNESS_RUNTIME")
    if override:
        root = Path(override)
    else:
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        if not xdg:
            raise HarnessError(EXIT_USAGE, "XDG_RUNTIME_DIR is not set",
                               "run inside a user session or set FSO_HARNESS_RUNTIME")
        root = Path(xdg) / "fso-harness"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def session_dir(sid: str) -> Path:
    """Return the run folder for session `sid` (validated id; folder may not exist yet)."""
    if not _SID_RE.match(sid):
        raise HarnessError(EXIT_NOT_FOUND, f"not a session id: {sid!r}", "use `fsoctl list`")
    return runtime_root() / sid


def fs2path() -> Path:
    """Return the FreeSpace 2 game directory from $FS2PATH (must exist)."""
    value = os.environ.get("FS2PATH")
    if not value or not Path(value).is_dir():
        raise HarnessError(EXIT_USAGE, "FS2PATH is not set to a game directory",
                           "export FS2PATH=/path/to/FS2 (the folder with root_fs2.vp)")
    return Path(value)


def bridge_source() -> Path:
    """Return the bridge mod folder shipped inside the package."""
    return Path(__file__).resolve().parent / "bridge" / BRIDGE_MOD


def channel_dir(prefs: Path) -> Path:
    """Return the command channel folder for a session whose FSO_PREFERENCES_PATH is `prefs`.

    The engine resolves cf path "data/cache" to <prefs>/<first mod>/data/cache (probed 2026-10-12),
    and the bridge is always first in -mod.
    """
    return prefs / BRIDGE_MOD / "data" / "cache"
```

Also create the empty folders `fsoctl/bridge/fso_harness_bridge/data/tables/` and `.../data/scripts/` with a `.gitkeep` each so `test_bridge_source_exists_in_package` passes.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest -q && ruff check .`
Expected: PASS (5 tests), ruff clean.

- [ ] **Step 6: Red-proof** — change `0o700` to `0o755` in `runtime_root`, run `pytest tests/test_paths.py -q`, see `test_runtime_root_uses_override_and_is_private` fail, restore.

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "Scaffold fsoctl: errors, paths, tooling, fixtures"
```

---

### Task 3: Session records, locks and sequence numbers

**Files:**
- Create: `fsoctl/session.py`, `tests/test_session.py`

**Interfaces:**
- Consumes: `paths.session_dir`, `paths.runtime_root`, `errors`.
- Produces:
  - `@dataclass Session` fields: `id: str, kind: str ("game"|"qtfred"), binary: str, fs2path: str, mods: list[str], mission: str | None, renderer: str ("opengl"|"vulkan"), width: int, height: int, switches: dict, allow_eval: bool, extra_args: list[str], scope: str, display: str | None, runner_pid: int | None, engine_pid: int | None, xvfb_pid: int | None, created: float, status: str ("starting"|"running"|"stopped"|"failed"|"exited"), exit_info: dict | None`
  - `new_session(**fields) -> Session` (allocates id `s` + 8 hex, creates run folder + `prefs/`, saves)
  - `save(sess: Session) -> None` (atomic: write `session.json.tmp` then `os.replace`)
  - `load(sid: str) -> Session` (raises EXIT_NOT_FOUND)
  - `list_sessions() -> list[Session]`
  - `prefs_dir(sess) -> Path`, `run_dir(sess) -> Path`
  - `next_seq(sess) -> int` (flock on `<run>/seq.lock`, reads/increments `<run>/seq`, returns the new value; safe across processes)
  - `audit(sess, kind: str, text: str) -> None` (append one JSON line to `<run>/audit.log`)

- [ ] **Step 1: Write the failing tests**

```python
import json
import multiprocessing

import pytest

from fsoctl import session
from fsoctl.errors import EXIT_NOT_FOUND, HarnessError


def _mk(**kw):
    base = dict(kind="game", binary="/bin/true", fs2path="/tmp", mods=[], mission=None,
                renderer="opengl", width=1280, height=720, switches={}, allow_eval=False,
                extra_args=[])
    base.update(kw)
    return session.new_session(**base)


def test_new_session_creates_run_and_prefs(tmp_runtime):
    s = _mk()
    assert session.run_dir(s).is_dir()
    assert session.prefs_dir(s).is_dir()
    assert s.status == "starting"
    assert s.scope == f"fso-harness-{s.id}"


def test_save_load_round_trip_and_atomicity(tmp_runtime):
    s = _mk(mission="m.fs2")
    s.status = "running"
    session.save(s)
    assert not (session.run_dir(s) / "session.json.tmp").exists()
    assert session.load(s.id).mission == "m.fs2"
    assert json.loads((session.run_dir(s) / "session.json").read_text())["status"] == "running"


def test_load_unknown_session(tmp_runtime):
    with pytest.raises(HarnessError) as e:
        session.load("s00000000")
    assert e.value.code == EXIT_NOT_FOUND


def test_list_sessions_sorted_by_creation(tmp_runtime):
    a = _mk()
    b = _mk()
    assert [x.id for x in session.list_sessions()] == [a.id, b.id]


def _bump(sid, n, out):
    s = session.load(sid)
    out.extend(session.next_seq(s) for _ in range(n))


def test_next_seq_is_unique_across_processes(tmp_runtime):
    s = _mk()
    with multiprocessing.Manager() as m:
        out = m.list()
        procs = [multiprocessing.Process(target=_bump, args=(s.id, 25, out)) for _ in range(4)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(30)
        values = list(out)
    assert sorted(values) == list(range(1, 101))


def test_audit_appends_json_lines(tmp_runtime):
    s = _mk()
    session.audit(s, "lua", "return 1")
    session.audit(s, "post_event", "GS_EVENT_QUIT_GAME")
    lines = (session.run_dir(s) / "audit.log").read_text().splitlines()
    assert [json.loads(x)["kind"] for x in lines] == ["lua", "post_event"]
```

- [ ] **Step 2: Run** `pytest tests/test_session.py -q` — Expected: FAIL (module missing).

- [ ] **Step 3: Implement `fsoctl/session.py`**

```python
"""Session records: one JSON file per run folder, read fresh by every fsoctl call.

WHY files and no daemon: an MCP server cannot own a process across turns (a lesson from an earlier MCP server), so
all state lives on disk next to the run; any caller (CLI, MCP, another agent) picks it up.
"""
import fcntl
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import paths
from .errors import EXIT_NOT_FOUND, HarnessError


@dataclass
class Session:
    """Everything known about one harness run."""
    id: str
    kind: str
    binary: str
    fs2path: str
    mods: list[str]
    mission: str | None
    renderer: str
    width: int
    height: int
    switches: dict
    allow_eval: bool
    extra_args: list[str]
    scope: str
    display: str | None = None
    runner_pid: int | None = None
    engine_pid: int | None = None
    xvfb_pid: int | None = None
    created: float = field(default_factory=time.time)
    status: str = "starting"
    exit_info: dict | None = None


def run_dir(sess: Session) -> Path:
    """Return the session's run folder."""
    return paths.session_dir(sess.id)


def prefs_dir(sess: Session) -> Path:
    """Return the session's private FSO_PREFERENCES_PATH folder."""
    return run_dir(sess) / "prefs"


def new_session(**fields) -> Session:
    """Allocate an id, create the run and prefs folders, save, and return the session."""
    for _ in range(16):  # bounded: 16 collisions in 2^32 is effectively impossible
        sid = "s" + secrets.token_hex(4)
        if not paths.session_dir(sid).exists():
            break
    else:
        raise HarnessError(EXIT_NOT_FOUND, "could not allocate a session id", "retry")
    sess = Session(id=sid, scope=f"fso-harness-{sid}", **fields)
    prefs_dir(sess).mkdir(parents=True)
    save(sess)
    return sess


def save(sess: Session) -> None:
    """Write session.json atomically (temp file + rename)."""
    target = run_dir(sess) / "session.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(sess), indent=1))
    os.replace(tmp, target)


def load(sid: str) -> Session:
    """Read a session by id; raise EXIT_NOT_FOUND if it doesn't exist."""
    target = paths.session_dir(sid) / "session.json"
    if not target.exists():
        raise HarnessError(EXIT_NOT_FOUND, f"no session {sid}", "use `fsoctl list`")
    return Session(**json.loads(target.read_text()))


def list_sessions() -> list[Session]:
    """Return every session under the runtime root, oldest first."""
    out = []
    for d in sorted(paths.runtime_root().iterdir()):
        f = d / "session.json"
        if f.exists():
            out.append(Session(**json.loads(f.read_text())))
    return sorted(out, key=lambda s: s.created)


def next_seq(sess: Session) -> int:
    """Return the next command sequence number, unique across concurrent callers (flock)."""
    lock_path = run_dir(sess) / "seq.lock"
    seq_path = run_dir(sess) / "seq"
    with open(lock_path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            value = int(seq_path.read_text()) + 1 if seq_path.exists() else 1
            seq_path.write_text(str(value))
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return value


def audit(sess: Session, kind: str, text: str) -> None:
    """Append one JSON line describing a privileged action to the session's audit.log."""
    line = json.dumps({"t": time.time(), "kind": kind, "text": text})
    with open(run_dir(sess) / "audit.log", "a") as f:
        f.write(line + "\n")
```

- [ ] **Step 4: Run** `pytest tests/test_session.py -q && ruff check .` — Expected: PASS (6).
- [ ] **Step 5: Red-proof** — remove the two `flock` calls, rerun `test_next_seq_is_unique_across_processes` until it fails (it may need a few runs; note how many), restore.
- [ ] **Step 6: Commit** `git commit -am "Session records, atomic saves, cross-process sequence numbers"` (add new files first).


---

### Task 4: Child environment and renderer classification

**Files:**
- Create: `fsoctl/envsetup.py`, `fsoctl/renderer.py`, `tests/test_envsetup.py`, `tests/test_renderer.py`

**Interfaces:**
- Produces: `envsetup.child_env(base: dict, display: str, prefs: Path, renderer: str) -> dict`; `renderer.classify(maps_text: str) -> dict` returning `{"renderer": "vulkan"|"opengl"|"unknown", "libraries": [sorted unique names]}`; `renderer.read_maps(pid: int) -> str` (empty string if the process is gone).

- [ ] **Step 1: Write the failing tests**

```python
from pathlib import Path

from fsoctl.envsetup import child_env


def test_child_env_pins_toolkits_to_the_virtual_display():
    base = {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0", "HOME": "/home/x", "PATH": "/usr/bin"}
    env = child_env(base, ":93", Path("/r/s1/prefs"), "opengl")
    assert "WAYLAND_DISPLAY" not in env
    assert env["DISPLAY"] == ":93"
    assert env["SDL_VIDEODRIVER"] == "x11"
    assert env["GDK_BACKEND"] == "x11"
    assert env["QT_QPA_PLATFORM"] == "xcb"
    assert env["FSO_PREFERENCES_PATH"] == "/r/s1/prefs/"
    assert env["LIBGL_ALWAYS_SOFTWARE"] == "1"
    assert env["HOME"] == "/home/x"
    assert base["DISPLAY"] == ":0"  # caller's dict untouched


def test_child_env_vulkan_passes_icd_only_if_caller_set_it():
    env = child_env({"PATH": "/usr/bin"}, ":93", Path("/p"), "vulkan")
    assert "VK_ICD_FILENAMES" not in env
    assert "LIBGL_ALWAYS_SOFTWARE" not in env
    env2 = child_env({"VK_ICD_FILENAMES": "/x.json"}, ":93", Path("/p"), "vulkan")
    assert env2["VK_ICD_FILENAMES"] == "/x.json"
```

```python
from fsoctl.renderer import classify

GL_MAPS = """7f00-7f01 r-xp 0 08:01 1 /usr/lib64/libgallium-26.2.3.so
7f02-7f03 r-xp 0 08:01 2 /usr/lib64/libGLX_mesa.so.0.0.0
"""
VK_MAPS = """7f00-7f01 r-xp 0 08:01 1 /usr/lib64/libGLX_nvidia.so.595.71
7f02-7f03 r-xp 0 08:01 2 /usr/lib64/libnvidia-glvkspirv.so.595.71
7f04-7f05 r-xp 0 08:01 3 /usr/lib64/libvulkan.so.1.4.341
"""
FALLBACK_MAPS = VK_MAPS + GL_MAPS


def test_classify_opengl():
    assert classify(GL_MAPS)["renderer"] == "opengl"


def test_classify_vulkan_nvidia():
    r = classify(VK_MAPS)
    assert r["renderer"] == "vulkan"
    assert "libnvidia-glvkspirv.so" in r["libraries"]


def test_classify_mesa_gl_loaded_means_fallback_to_opengl():
    assert classify(FALLBACK_MAPS)["renderer"] == "opengl"


def test_classify_mesa_vulkan_driver():
    maps = "7f00-7f01 r-xp 0 08:01 1 /usr/lib64/libvulkan_radeon.so\n"
    assert classify(maps)["renderer"] == "vulkan"


def test_classify_nothing_known():
    assert classify("")["renderer"] == "unknown"
```

- [ ] **Step 2: Run** `pytest tests/test_envsetup.py tests/test_renderer.py -q` — Expected: FAIL (modules missing).

- [ ] **Step 3: Implement**

```python
"""Environment for every process the harness launches.

WHY each pin: SDL draws on the virtual display only with DISPLAY + SDL_VIDEODRIVER=x11; FSO's error
boxes go through zenity/kdialog, which reached the user's Wayland desktop through the default
wayland-0 socket until GDK_BACKEND/QT_QPA_PLATFORM were pinned (incident 2026-10-12). Software GL is
forced for OpenGL so runs never touch the user's GPU GL context. No Vulkan driver is chosen here:
VK_ICD_FILENAMES passes through only if the caller set it (no vendor defaults).
"""
from pathlib import Path


def child_env(base: dict, display: str, prefs: Path, renderer: str) -> dict:
    """Return a copy of `base` set up for a headless FSO/qtFRED child process."""
    env = dict(base)
    env.pop("WAYLAND_DISPLAY", None)
    env.update({
        "DISPLAY": display,
        "SDL_VIDEODRIVER": "x11",
        "GDK_BACKEND": "x11",
        "QT_QPA_PLATFORM": "xcb",
        "FSO_PREFERENCES_PATH": str(prefs).rstrip("/") + "/",
    })
    if renderer == "opengl":
        env["LIBGL_ALWAYS_SOFTWARE"] = "1"
    else:
        env.pop("LIBGL_ALWAYS_SOFTWARE", None)
    return env
```

```python
"""Which renderer actually came up, judged from the libraries the engine process mapped.

WHY maps and not the log: release builds write no log, and FSO silently falls back to OpenGL when
Vulkan fails, so the only reliable evidence is what got loaded. Under Xvfb, OpenGL always goes
through Mesa (libgallium / libGLX_mesa); a Vulkan run maps a Vulkan ICD and no Mesa GL.
"""
import re

_VK = re.compile(r"(libnvidia-glvkspirv\.so|libvulkan_[a-z0-9_]+\.so)")
_GL = re.compile(r"(libgallium[-0-9.]*\.so|libGLX_mesa\.so)")
_ANY = re.compile(r"(libgallium[-0-9.]*\.so|libGLX_[a-z]+\.so|libnvidia-gl[a-z]+\.so|"
                  r"libvulkan_[a-z0-9_]+\.so|libvulkan\.so)")


def classify(maps_text: str) -> dict:
    """Return {"renderer": vulkan|opengl|unknown, "libraries": [...]} for a /proc/<pid>/maps dump."""
    libs = sorted(set(_ANY.findall(maps_text)))
    has_gl = bool(_GL.search(maps_text))
    has_vk = bool(_VK.search(maps_text))
    if has_gl:
        kind = "opengl"
    elif has_vk:
        kind = "vulkan"
    else:
        kind = "unknown"
    return {"renderer": kind, "libraries": libs}


def read_maps(pid: int) -> str:
    """Return /proc/<pid>/maps text, or "" if the process is gone or unreadable."""
    try:
        with open(f"/proc/{pid}/maps") as f:
            return f.read()
    except OSError:
        return ""
```

- [ ] **Step 4: Run** the two test files + `ruff check .` — Expected: PASS (7).
- [ ] **Step 5: Red-proof** — swap the `if has_gl` / `elif has_vk` order, see `test_classify_mesa_gl_loaded_means_fallback_to_opengl` fail, restore.
- [ ] **Step 6: Commit** `git add -A && git commit -m "Child environment pinning and renderer classification"`

---

### Task 5: Scopes and the CLI frame

**Files:**
- Create: `fsoctl/scope.py`, `fsoctl/cli.py`, `fsoctl/__main__.py`, `tests/test_scope.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `errors`.
- Produces:
  - `scope.caps(runtime_sec: int = 1800, cpu_cores: int = 8, tasks: int = 512, mem_kb: int | None = None) -> list[str]` — list of systemd property strings (`"MemoryMax=…K"`, `"MemorySwapMax=0"`, `"CPUQuota=800%"`, `"TasksMax=512"`, `"RuntimeMaxSec=1800"`); default memory = 60% of MemAvailable, floor 2 GiB.
  - `scope.start(unit: str, argv: list[str], env: dict, log_path: Path, props: list[str]) -> int` — starts `systemd-run --user --scope --quiet --collect --unit <unit> -p … <argv>` detached; returns its pid (systemd-run execs the command, so this is the runner's pid).
  - `scope.is_active(unit: str) -> bool`; `scope.stop(unit: str, timeout: float = 20) -> None` (idempotent).
  - `cli.main(argv: list[str] | None = None) -> int`; `cli.VERBS: dict[str, Callable[[argparse.Namespace], dict]]`; `cli.register(name: str, help: str, configure: Callable[[ArgumentParser], None]) -> decorator`. Every verb returns a dict; `main` prints JSON (`--json`) or a readable rendering; `HarnessError` → its code.

- [ ] **Step 1: Write the failing tests**

`tests/test_scope.py`:
```python
import os
import time

import pytest

from fsoctl import scope


def test_caps_has_all_ceilings_and_floor(monkeypatch):
    monkeypatch.setattr(scope, "_mem_available_kb", lambda: 1_000_000)
    props = scope.caps(runtime_sec=60, cpu_cores=4, tasks=128)
    assert "MemoryMax=2097152K" in props          # floor 2 GiB beats 60% of ~1 GB
    assert "MemorySwapMax=0" in props
    assert "CPUQuota=400%" in props
    assert "TasksMax=128" in props
    assert "RuntimeMaxSec=60" in props


@pytest.mark.engine
def test_scope_start_is_active_and_stop_is_idempotent(tmp_path):
    unit = f"fso-harness-test{os.getpid()}"
    scope.start(unit, ["sleep", "60"], dict(os.environ), tmp_path / "log", scope.caps(runtime_sec=120))
    for _ in range(50):
        if scope.is_active(unit):
            break
        time.sleep(0.1)
    assert scope.is_active(unit)
    scope.stop(unit)
    assert not scope.is_active(unit)
    scope.stop(unit)  # second stop must not raise
```

`tests/test_cli.py`:
```python
import json

from fsoctl import cli
from fsoctl.errors import EXIT_NOT_FOUND


def test_version_json(capsys):
    assert cli.main(["--json", "version"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["version"] == "0.1.0"


def test_unknown_session_maps_to_exit_code(capsys, tmp_runtime):
    assert cli.main(["--json", "status", "s00000000"]) == EXIT_NOT_FOUND
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["hint"]
```
(`status` is registered in Task 7; until then this second test is expected to fail with "unknown verb" — mark it `@pytest.mark.xfail(strict=True, reason="status lands in Task 7")` here and remove the marker in Task 7.)

- [ ] **Step 2: Run** `pytest tests/test_scope.py tests/test_cli.py -q` — Expected: FAIL (modules missing).

- [ ] **Step 3: Implement `fsoctl/scope.py`**

```python
"""Named systemd user scopes: the resource ceiling and the kill switch for every run.

WHY systemd-run --scope and not bounded-run: we need a KNOWN unit name so `stop` and `status` can
find the run later from another process; stopping the scope reaps the runner, Xvfb and the engine
together (cgroup), which a process-group kill does not guarantee. Caps mirror bounded-run's policy.
"""
import subprocess
from pathlib import Path

from .errors import EXIT_LAUNCH, HarnessError

_MEM_FLOOR_KB = 2 * 1024 * 1024
_MEM_SHARE = 0.60


def _mem_available_kb() -> int:
    """Return MemAvailable from /proc/meminfo in KiB."""
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1])
    raise HarnessError(EXIT_LAUNCH, "cannot read MemAvailable", "is /proc mounted?")


def caps(runtime_sec: int = 1800, cpu_cores: int = 8, tasks: int = 512,
         mem_kb: int | None = None) -> list[str]:
    """Return systemd scope properties capping memory, swap, CPU, task count and runtime."""
    mem = mem_kb if mem_kb is not None else max(_MEM_FLOOR_KB, int(_mem_available_kb() * _MEM_SHARE))
    return [f"MemoryMax={mem}K", "MemorySwapMax=0", f"CPUQuota={cpu_cores * 100}%",
            f"TasksMax={tasks}", f"RuntimeMaxSec={runtime_sec}"]


def start(unit: str, argv: list[str], env: dict, log_path: Path, props: list[str]) -> int:
    """Start `argv` inside a new named scope, detached; return the runner pid."""
    cmd = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--unit", unit]
    for p in props:
        cmd += ["-p", p]
    log = open(log_path, "ab")  # noqa: SIM115 - handed to the child, closed by the OS on exit
    try:
        proc = subprocess.Popen(cmd + argv, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as e:
        raise HarnessError(EXIT_LAUNCH, f"systemd-run failed: {e}", "is systemd --user running?") from e
    finally:
        log.close()
    return proc.pid


def is_active(unit: str) -> bool:
    """Return True while the scope exists and is running."""
    r = subprocess.run(["systemctl", "--user", "is-active", "--quiet", f"{unit}.scope"],
                       timeout=10, check=False)
    return r.returncode == 0


def stop(unit: str, timeout: float = 20) -> None:
    """Stop the scope (kills everything in it). Succeeds if it is already gone."""
    if not is_active(unit):
        return
    subprocess.run(["systemctl", "--user", "stop", f"{unit}.scope"], timeout=timeout, check=False)
```

- [ ] **Step 4: Implement `fsoctl/cli.py` and `fsoctl/__main__.py`**

```python
"""fsoctl command line: one verb per harness tool, JSON or readable output, stable exit codes.

Verbs live in their own modules and register here with @register so cli.py stays a thin
dispatcher (Power of Ten rule 4: no giant switch).
"""
import argparse
import json
import sys
from collections.abc import Callable

from .errors import EXIT_OK, EXIT_USAGE, HarnessError

VERSION = "0.1.0"
VERBS: dict[str, tuple[str, Callable, Callable]] = {}


def register(name: str, help_text: str, configure: Callable[[argparse.ArgumentParser], None]):
    """Decorator: add a verb `name` whose function takes the parsed args and returns a dict."""
    def wrap(fn: Callable[[argparse.Namespace], dict]):
        VERBS[name] = (help_text, configure, fn)
        return fn
    return wrap


@register("version", "print the harness version", lambda p: None)
def _version(args: argparse.Namespace) -> dict:
    """Return the harness version."""
    return {"version": VERSION}


def _load_verb_modules() -> None:
    """Import every module that registers verbs (kept explicit; no plugin discovery)."""
    # Each later task adds its module name to this tuple.
    for name in ():
        __import__(f"fsoctl.{name}")


def _render(result: dict) -> str:
    """Render a result dict for humans: one key per line."""
    return "\n".join(f"{k}: {v}" for k, v in result.items() if k != "ok")


def main(argv: list[str] | None = None) -> int:
    """Parse argv, run one verb, print its result, and return the exit code."""
    _load_verb_modules()
    parser = argparse.ArgumentParser(prog="fsoctl")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="verb", required=True)
    for name, (help_text, configure, _fn) in sorted(VERBS.items()):
        configure(sub.add_parser(name, help=help_text))
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code else EXIT_OK
    try:
        result = {"ok": True, **VERBS[args.verb][2](args)}
        code = EXIT_OK
    except HarnessError as e:
        result, code = e.to_dict(), e.code
    print(json.dumps(result) if args.json else _render(result))
    return code


if __name__ == "__main__":
    sys.exit(main())
```

`fsoctl/__main__.py`:
```python
"""Allow `python -m fsoctl`."""
import sys

from .cli import main

sys.exit(main())
```

- [ ] **Step 5: Run** `pytest -q && bounded-run pytest -m engine -p no:xdist tests/test_scope.py -q && ruff check .` — Expected: unit PASS (the status test xfails), scope engine test PASS.
- [ ] **Step 6: Red-proof** — make `stop` raise when the unit is inactive; see the idempotence test fail; restore.
- [ ] **Step 7: Commit** `git add -A && git commit -m "Named scopes with ceilings; CLI dispatcher with exit codes"`

---

### Task 6: Bridge core, bridge install, command channel

**Files:**
- Create: `fsoctl/bridge/fso_harness_bridge/data/tables/fsoharness-sct.tbm`, `fsoctl/bridge/fso_harness_bridge/data/scripts/fsoharness.lua`, `fsoctl/bridge_install.py`, `fsoctl/channel.py`, `tests/test_bridge_install.py`, `tests/test_channel.py`, `tests/fake_bridge.py`

**Interfaces:**
- Consumes: `paths`, `session` (`next_seq`, `prefs_dir`), `errors`.
- Produces:
  - Bridge protocol (files in `channel_dir`): harness writes `cmd-NNNNNN.json` = `{"seq": int, "cmd": str, "args": object}`; bridge writes `res-NNNNNN.json` = `{"seq": int, "ok": true, "result": any}` or `{"seq": int, "ok": false, "error": str, "hint": str}`; bridge writes `ready.json` every 30 frames = `{"version": str, "frame": int, "state": str, "mission_time": number, "mission": str, "popup": str|null}`. Harness writes `switches.json` (object) and, when eval is allowed, `allow_eval` (empty file) before launch.
  - Bridge Lua module API (used by later tasks to add handlers): `H.handlers[name] = function(args) return result end` (raise with `error({msg=..., hint=...})` to refuse); `H.state_name() -> string`; `H.on_state(name, fn)` registers a callback for On State Start; `H.timers` list of `{tick=function() -> done:boolean, reply=function()}` for long operations.
  - `bridge_install.install(fs2: Path) -> dict` → `{"path": str, "version": str, "updated": bool}`; version = first 12 hex of sha256 over the source tree's relative paths + bytes.
  - `class channel.Channel(sess: Session)`: `.dir: Path`, `.heartbeat() -> dict | None`, `.wait_ready(timeout: float, alive: Callable[[], bool]) -> dict`, `.send(cmd: str, args: dict | None = None, timeout: float = 10.0, alive: Callable[[], bool] | None = None) -> Any`.
  - Built-in handlers in this task: `ping` → `{"pong": true, "frame": n}`, `basic_state` → `{"state", "mission_time", "mission"}`, `quit` → posts `GS_EVENT_QUIT_GAME`, returns `{}`.

- [ ] **Step 1: Write the bridge files**

`fsoharness-sct.tbm` (thin; no `;` anywhere):
```
#Conditional Hooks
$Application: FS2_Open
$On Game Init: [
	FsoHarness = require("fsoharness")
	FsoHarness.init()
]
$On Frame: [
	FsoHarness.frame(false)
]
$On Dialog Frame: [
	FsoHarness.frame(true)
]
$On State Start: [
	FsoHarness.state_start()
]
#End
```

`fsoharness.lua`:
```lua
-- FSO harness bridge: polls numbered command files in its cache folder and writes replies.
-- Runs inside the engine. Uses only the engine's cf API (never io.lines/io.input, see research).
local json = require("dkjson")
local H = {}
H.VERSION = "1"
H.DIR = "data/cache"
H.frames = 0
H.handlers = {}
H.state_hooks = {}
H.timers = {}
H.switches = {}
H.allow_eval = false
H.popup = nil

local function read_all(name)
	local f = cf.openFile(name, "r", H.DIR)
	if not (f and f:isValid()) then return nil end
	local s = f:read("*a")
	f:close()
	return s
end

local function write_atomic(name, text)
	local tmp = name .. ".tmp"
	local f = cf.openFile(tmp, "w", H.DIR)
	if not (f and f:isValid()) then return false end
	f:write(text)
	f:close()
	if cf.fileExists(name, H.DIR) then cf.deleteFile(name, H.DIR) end
	return cf.renameFile(tmp, name, H.DIR)
end
H.write_atomic = write_atomic

function H.state_name()
	local st = ba.getCurrentGameState()
	if st and st:isValid() then return st.Name end
	return "none"
end

function H.reply(seq, ok, payload, hint)
	local body
	if ok then
		body = { seq = seq, ok = true, result = payload }
	else
		body = { seq = seq, ok = false, error = tostring(payload), hint = hint or "" }
	end
	write_atomic(string.format("res-%06d.json", seq), json.encode(body))
end

function H.on_state(name, fn)
	H.state_hooks[#H.state_hooks + 1] = { name = name, fn = fn }
end

function H.init()
	local sw = read_all("switches.json")
	if sw then H.switches = json.decode(sw) or {} end
	H.allow_eval = cf.fileExists("allow_eval", H.DIR)
end

local function run_command(name)
	local text = read_all(name)
	cf.deleteFile(name, H.DIR)
	local cmd = text and json.decode(text)
	if not cmd or not cmd.seq then return end
	local handler = H.handlers[cmd.cmd]
	if not handler then
		H.reply(cmd.seq, false, "unknown command " .. tostring(cmd.cmd), "see fsoctl --help")
		return
	end
	local ok, result = pcall(handler, cmd.args or {}, cmd.seq)
	if ok then
		if result ~= H.DEFERRED then H.reply(cmd.seq, true, result) end
	elseif type(result) == "table" then
		H.reply(cmd.seq, false, result.msg, result.hint)
	else
		H.reply(cmd.seq, false, result, "bridge error")
	end
end
H.DEFERRED = {}

local function poll_commands()
	local files = cf.listFiles(H.DIR, "*.json")
	if not files then return end
	local pending = {}
	for _, f in ipairs(files) do
		local n = string.match(f, "^cmd%-(%d+)%.json$")
		if n then pending[#pending + 1] = { n = tonumber(n), f = f } end
	end
	table.sort(pending, function(a, b) return a.n < b.n end)
	for _, p in ipairs(pending) do run_command(p.f) end
end

local function tick_timers()
	local keep = {}
	for _, t in ipairs(H.timers) do
		if not t.tick() then keep[#keep + 1] = t end
	end
	H.timers = keep
end

local function heartbeat()
	write_atomic("ready.json", json.encode({
		version = H.VERSION, frame = H.frames, state = H.state_name(),
		mission_time = mn.getMissionTime(), mission = mn.getMissionFilename(),
		popup = H.popup,
	}))
end

function H.frame(in_dialog)
	H.frames = H.frames + 1
	if not in_dialog then H.popup = nil end
	poll_commands()
	tick_timers()
	if H.frames % 30 == 1 then heartbeat() end
end

function H.state_start()
	local name = H.state_name()
	for _, h in ipairs(H.state_hooks) do
		if h.name == name then h.fn() end
	end
end

H.handlers.ping = function() return { pong = true, frame = H.frames } end
H.handlers.basic_state = function()
	return { state = H.state_name(), mission_time = mn.getMissionTime(),
	         mission = mn.getMissionFilename() }
end
H.handlers.quit = function()
	ba.postGameEvent(ba.GameEvents["GS_EVENT_QUIT_GAME"])
	return {}
end

return H
```
Before committing, check every API name used above against `docs/research/scripting.lua` from Task 1 (e.g. `cf.listFiles` return shape, `cf.renameFile` argument order, `mn.getMissionFilename`) and fix any mismatch; note fixes in the commit body.

- [ ] **Step 2: Write the failing tests**

`tests/fake_bridge.py` (a Python stand-in for the Lua bridge so channel logic is unit-testable):
```python
"""Emulates the Lua bridge's file protocol in a thread for unit tests."""
import json
import threading
import time
from pathlib import Path


class FakeBridge:
    """Answers cmd-*.json files in `directory` like the real bridge would."""

    def __init__(self, directory: Path, state: str = "GS_STATE_MAIN_MENU") -> None:
        self.dir = directory
        self.state = state
        self.stop_flag = threading.Event()
        self.seen: list[int] = []
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop_flag.set()
        self.thread.join(5)

    def _loop(self) -> None:
        frame = 0
        while not self.stop_flag.is_set():
            frame += 1
            (self.dir / "ready.json").write_text(json.dumps({"state": self.state, "frame": frame}))
            for f in sorted(self.dir.glob("cmd-*.json")):
                cmd = json.loads(f.read_text())
                f.unlink()
                self.seen.append(cmd["seq"])
                body = ({"seq": cmd["seq"], "ok": True, "result": {"echo": cmd["args"]}}
                        if cmd["cmd"] != "refuse" else
                        {"seq": cmd["seq"], "ok": False, "error": "nope", "hint": "try x"})
                tmp = self.dir / f"res-{cmd['seq']:06d}.json.tmp"
                tmp.write_text(json.dumps(body))
                tmp.rename(self.dir / f"res-{cmd['seq']:06d}.json")
            time.sleep(0.01)
```

`tests/test_channel.py`:
```python
import multiprocessing

import pytest

from fsoctl import channel, session
from fsoctl.errors import EXIT_ENGINE, EXIT_REFUSED, EXIT_TIMEOUT, HarnessError
from tests.fake_bridge import FakeBridge


def _sess():
    return session.new_session(kind="game", binary="/bin/true", fs2path="/tmp", mods=[],
                               mission=None, renderer="opengl", width=1, height=1, switches={},
                               allow_eval=False, extra_args=[])


def test_send_round_trip(tmp_runtime):
    s = _sess()
    ch = channel.Channel(s)
    with FakeBridge(ch.dir):
        assert ch.send("ping", {"a": 1}) == {"echo": {"a": 1}}
    assert not list(ch.dir.glob("res-*.json"))  # reply consumed


def test_refusal_becomes_harness_error(tmp_runtime):
    ch = channel.Channel(_sess())
    with FakeBridge(ch.dir), pytest.raises(HarnessError) as e:
        ch.send("refuse")
    assert e.value.code == EXIT_REFUSED and e.value.hint == "try x"


def test_timeout_names_heartbeat_state_and_withdraws_command(tmp_runtime):
    ch = channel.Channel(_sess())
    ch.dir.mkdir(parents=True)
    (ch.dir / "ready.json").write_text('{"state": "GS_STATE_BRIEFING", "frame": 3}')
    with pytest.raises(HarnessError) as e:
        ch.send("ping", timeout=0.3, alive=lambda: True)
    assert e.value.code == EXIT_TIMEOUT and "GS_STATE_BRIEFING" in e.value.message
    assert not list(ch.dir.glob("cmd-*.json"))


def test_dead_engine_is_reported_not_timed_out(tmp_runtime):
    ch = channel.Channel(_sess())
    ch.dir.mkdir(parents=True)
    with pytest.raises(HarnessError) as e:
        ch.send("ping", timeout=5, alive=lambda: False)
    assert e.value.code == EXIT_ENGINE and "crash_report" in e.value.hint


def _send_many(sid, n, out):
    ch = channel.Channel(session.load(sid))
    for i in range(n):
        out.append(ch.send("ping", {"i": i, "who": sid})["echo"]["i"])


def test_concurrent_callers_get_their_own_replies(tmp_runtime):
    s = _sess()
    ch = channel.Channel(s)
    with FakeBridge(ch.dir) as fb, multiprocessing.Manager() as m:
        outs = [m.list(), m.list()]
        procs = [multiprocessing.Process(target=_send_many, args=(s.id, 20, o)) for o in outs]
        for p in procs:
            p.start()
        for p in procs:
            p.join(30)
        assert [list(o) for o in outs] == [list(range(20)), list(range(20))]
        assert sorted(fb.seen) == list(range(1, 41))
```

`tests/test_bridge_install.py`:
```python
from fsoctl import bridge_install, paths


def test_install_copies_and_stamps(tmp_path):
    r = bridge_install.install(tmp_path)
    target = tmp_path / paths.BRIDGE_MOD
    assert r["updated"] is True
    assert (target / "data" / "scripts" / "fsoharness.lua").exists()
    assert (target / ".harness-version").read_text() == r["version"]


def test_install_is_noop_when_current(tmp_path):
    bridge_install.install(tmp_path)
    assert bridge_install.install(tmp_path)["updated"] is False


def test_install_replaces_stale_bridge(tmp_path):
    bridge_install.install(tmp_path)
    target = tmp_path / paths.BRIDGE_MOD
    (target / ".harness-version").write_text("old")
    (target / "data" / "scripts" / "fsoharness.lua").write_text("-- stale")
    (target / "leftover.txt").write_text("x")
    r = bridge_install.install(tmp_path)
    assert r["updated"] is True
    assert "stale" not in (target / "data" / "scripts" / "fsoharness.lua").read_text()
    assert not (target / "leftover.txt").exists()
```

- [ ] **Step 3: Run** `pytest tests/test_channel.py tests/test_bridge_install.py -q` — Expected: FAIL (modules missing).

- [ ] **Step 4: Implement `fsoctl/bridge_install.py` and `fsoctl/channel.py`**

```python
"""Install the bridge mod into the game directory, refreshing it when the harness version changes.

WHY a content hash: an older bridge left in FS2PATH would otherwise run silently with a newer
harness (Review Focus 5). The stamp makes "is this the bridge I ship?" a file comparison.
"""
import hashlib
import shutil
from pathlib import Path

from . import paths


def _source_version() -> str:
    """Return a short hash over the shipped bridge tree (paths + bytes)."""
    h = hashlib.sha256()
    src = paths.bridge_source()
    for f in sorted(p for p in src.rglob("*") if p.is_file() and p.name != ".gitkeep"):
        h.update(str(f.relative_to(src)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:12]


def install(fs2: Path) -> dict:
    """Ensure FS2PATH/fso_harness_bridge matches the shipped bridge; return path/version/updated."""
    version = _source_version()
    target = fs2 / paths.BRIDGE_MOD
    stamp = target / ".harness-version"
    if stamp.exists() and stamp.read_text() == version:
        return {"path": str(target), "version": version, "updated": False}
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(paths.bridge_source(), target, ignore=shutil.ignore_patterns(".gitkeep"))
    stamp.write_text(version)
    return {"path": str(target), "version": version, "updated": True}
```

```python
"""Command channel to the in-engine bridge: numbered files in the session's cache folder.

WHY files: FSO's Lua can't open sockets and io.open is restricted to FIFOs; a FIFO read would
block the game frame. The bridge polls with the engine's cf API each frame (On Frame and On Dialog
Frame), so commands also work while a popup is up. Sequence numbers come from session.next_seq
(flock), so concurrent callers never share a reply file.
"""
import json
import os
import time
from collections.abc import Callable
from typing import Any

from . import paths, session
from .errors import EXIT_ENGINE, EXIT_REFUSED, EXIT_TIMEOUT, HarnessError

_POLL_SEC = 0.05


class Channel:
    """Send commands to one session's bridge and read its heartbeat."""

    def __init__(self, sess: session.Session) -> None:
        self.sess = sess
        self.dir = paths.channel_dir(session.prefs_dir(sess))

    def heartbeat(self) -> dict | None:
        """Return the bridge's latest ready.json, or None if absent or mid-write."""
        try:
            return json.loads((self.dir / "ready.json").read_text())
        except (OSError, ValueError):
            return None

    def wait_ready(self, timeout: float, alive: Callable[[], bool]) -> dict:
        """Block until the first heartbeat (bounded); raise if the engine dies or time runs out."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            hb = self.heartbeat()
            if hb:
                return hb
            if not alive():
                raise HarnessError(EXIT_ENGINE, "engine exited before the bridge started",
                                   "run `fsoctl crash_report <id>`")
            time.sleep(0.25)
        raise HarnessError(EXIT_TIMEOUT, f"no bridge heartbeat after {timeout:.0f}s",
                           "the engine may be stuck loading or on a popup; take a screenshot")

    def send(self, cmd: str, args: dict | None = None, timeout: float = 10.0,
             alive: Callable[[], bool] | None = None) -> Any:
        """Send one command and return its result; raise HarnessError on refusal/timeout/death."""
        self.dir.mkdir(parents=True, exist_ok=True)
        seq = session.next_seq(self.sess)
        cmd_path = self.dir / f"cmd-{seq:06d}.json"
        res_path = self.dir / f"res-{seq:06d}.json"
        tmp = self.dir / f"cmd-{seq:06d}.tmp"
        tmp.write_text(json.dumps({"seq": seq, "cmd": cmd, "args": args or {}}))
        os.replace(tmp, cmd_path)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if res_path.exists():
                body = json.loads(res_path.read_text())
                res_path.unlink()
                if body.get("ok"):
                    return body.get("result")
                raise HarnessError(EXIT_REFUSED, body.get("error", "refused"), body.get("hint", ""))
            if alive is not None and not alive():
                cmd_path.unlink(missing_ok=True)
                raise HarnessError(EXIT_ENGINE, f"engine is not running ({cmd})",
                                   "run `fsoctl crash_report <id>`")
            time.sleep(_POLL_SEC)
        cmd_path.unlink(missing_ok=True)
        hb = self.heartbeat() or {}
        raise HarnessError(EXIT_TIMEOUT,
                           f"no reply to {cmd} in {timeout:.0f}s (bridge state {hb.get('state', 'unknown')})",
                           "take a screenshot; a popup or a loading screen may be blocking")
```

- [ ] **Step 5: Run** `pytest -q && ruff check .` — Expected: PASS.
- [ ] **Step 6: Red-proof** — in `send`, use a fixed `seq = 1`; run `test_concurrent_callers_get_their_own_replies`, see it fail; restore.
- [ ] **Step 7: Commit** `git add -A && git commit -m "Bridge core (Lua), versioned install, file command channel"`

---

### Task 7: Runner and launcher (launch, status, stop, list, cleanup)

**Files:**
- Create: `fsoctl/runner.py`, `fsoctl/launcher.py`, `tests/test_launcher_unit.py`, `tests/test_launch_engine.py`
- Modify: `fsoctl/cli.py` (`_load_verb_modules` tuple gains `"launcher"`), `tests/test_cli.py` (drop the xfail marker)

**Interfaces:**
- Consumes: `session`, `scope`, `envsetup`, `renderer`, `channel`, `bridge_install`, `paths`.
- Produces:
  - `@dataclass LaunchRequest`: `binary: str, mission: str | None = None, mods: list[str] = [], renderer: str = "opengl", width: int = 1280, height: int = 720, skip_movies: bool = False, pilot_mode: str = "chooser"` ("chooser"|"existing"|"create"), `pilot_name: str = "HarnessPilot", pilot_source: str | None = None, dismiss_popups: bool = False, skip_command_briefing: bool = False, skip_briefing: bool = False, noninteractive: bool = True, allow_eval: bool = False, extra_args: list[str] = [], kind: str = "game", runtime_sec: int = 1800, ready_timeout: float = 240.0`.
  - `launcher.build_argv(sess: Session) -> list[str]`; `launcher.launch(req: LaunchRequest) -> dict` (`{"id", "display", "renderer", "libraries", "state"}`); `launcher.status(sid) -> dict`; `launcher.stop(sid) -> dict`; `launcher.list_all() -> dict`; `launcher.cleanup(older_than_sec: float = 0) -> dict`; `launcher.alive(sess) -> bool`; `launcher.require_running(sid) -> Session` (raises EXIT_ENGINE with exit info if not running).
  - `runner.main(sid: str) -> int` — run inside the scope: start Xvfb (`-displayfd`), save display, start the engine/qtFRED with `child_env`, save pids, wait, record `exit_info = {"returncode": int, "signal": str | None, "at": float}`, status `exited`, stop Xvfb.
  - CLI verbs: `launch`, `status`, `stop`, `list`, `cleanup` (flags mirror LaunchRequest: `--mission`, `--mods a,b`, `--vulkan`, `--res 1280x720`, `--skip-movies`, `--pilot existing:NAME|create:NAME|chooser`, `--pilot-source DIR`, `--dismiss-popups`, `--skip-command-briefing`, `--skip-briefing`, `--interactive`, `--allow-eval`, `--qtfred`, `--runtime SEC`, `--ready-timeout SEC`, trailing `-- <fso args>`).
  - Concurrency cap: `launch` refuses (EXIT_REFUSED) when 3 sessions are `running`/`starting` (env `FSO_HARNESS_MAX_SESSIONS` overrides).

- [ ] **Step 1: Write the failing unit tests** (`tests/test_launcher_unit.py`)

```python
from fsoctl import launcher, paths, session


def _sess(**sw):
    base = dict(kind="game", binary="/x/fs2_open", fs2path="/g", mods=["MVPS"], mission="m.fs2",
                renderer="opengl", width=1280, height=720, switches={}, allow_eval=False,
                extra_args=["-seed", "5"])
    base.update(sw)
    return session.new_session(**base)


def test_argv_defaults(tmp_runtime):
    argv = launcher.build_argv(_sess())
    assert argv[:3] == ["/x/fs2_open", "-mod", f"{paths.BRIDGE_MOD},MVPS"]
    for flag in ("-window", "-nosound", "-no_unfocused_pause", "-noninteractive"):
        assert flag in argv
    assert argv[argv.index("-res") + 1] == "1280x720"
    assert argv[argv.index("-start_mission") + 1] == "m.fs2"
    assert argv[-2:] == ["-seed", "5"]
    assert "-nomovies" not in argv and "-pilot" not in argv and "-vulkan" not in argv


def test_argv_switches(tmp_runtime):
    s = _sess(renderer="vulkan", switches={"skip_movies": True, "pilot_mode": "existing",
                                           "pilot_name": "Alpha", "noninteractive": False})
    argv = launcher.build_argv(s)
    assert "-nomovies" in argv and "-vulkan" in argv and "-noninteractive" not in argv
    assert argv[argv.index("-pilot") + 1] == "Alpha"


def test_argv_qtfred_uses_full_mission_path_and_no_bridge(tmp_runtime):
    s = _sess(kind="qtfred", mission="m.fs2", mods=["MVPS"])
    argv = launcher.build_argv(s)
    assert paths.BRIDGE_MOD not in " ".join(argv)
    assert argv[argv.index("-start_mission") + 1].endswith("/m.fs2")
    assert argv[argv.index("-start_mission") + 1].startswith("/")


def test_session_cap_refuses_fourth(tmp_runtime, monkeypatch):
    monkeypatch.setenv("FSO_HARNESS_MAX_SESSIONS", "1")
    s = _sess()
    s.status = "running"
    session.save(s)
    import pytest

    from fsoctl.errors import EXIT_REFUSED, HarnessError
    with pytest.raises(HarnessError) as e:
        launcher.check_capacity()
    assert e.value.code == EXIT_REFUSED
```
For qtFRED, `build_argv` resolves a bare mission name against `<fs2path>/<first mod>/data/missions/` then `<fs2path>/data/missions/` (qtFRED segfaults on a relative `-start_mission`, observed 2026-10-11); if neither exists it uses `<fs2path>/data/missions/<name>` so the error is the engine's, visible in the log.

- [ ] **Step 2: Write the failing engine test** (`tests/test_launch_engine.py`)

```python
import os

import pytest

from fsoctl import launcher, session
from fsoctl.launcher import LaunchRequest

pytestmark = pytest.mark.engine


def test_launch_heartbeat_status_stop(engine_env, tmp_runtime):
    fs2, fso = engine_env
    req = LaunchRequest(binary=str(fso), mods=[os.environ.get("FSO_TEST_MODS", "MVPS-4.7.3")],
                        pilot_mode="create", skip_movies=True, dismiss_popups=True)
    r = launcher.launch(req)
    try:
        assert r["renderer"] == "opengl"
        st = launcher.status(r["id"])
        assert st["status"] == "running" and st["heartbeat"]["state"].startswith("GS_STATE_")
    finally:
        out = launcher.stop(r["id"])
    assert out["status"] in ("stopped", "exited")
    assert launcher.stop(r["id"])["status"] in ("stopped", "exited")  # idempotent (Review Focus 4)
    assert not launcher.alive(session.load(r["id"]))
```
(`pilot_mode="create"` needs Task 8's bridge switch; until Task 8 lands, run this test with `pilot_mode="existing"` and `pilot_source` pointing at a copied pilot, then switch it in Task 8.)

- [ ] **Step 3: Run** both files — Expected: FAIL (modules missing).

- [ ] **Step 4: Implement `fsoctl/runner.py`**

```python
"""The process that runs INSIDE a session's scope: Xvfb, then the engine or qtFRED.

WHY a runner process: the scope's main process must outlive `fsoctl launch` (which returns as soon
as the session is ready) and must own both Xvfb and the engine so stopping the scope ends both. It
also records how the engine exited, which crash_report needs.
"""
import os
import signal
import subprocess
import sys
import time

from . import envsetup, launcher, session

_XVFB_READY_TIMEOUT = 10.0


def _start_xvfb(width: int, height: int) -> tuple[str, subprocess.Popen]:
    """Start Xvfb on a free display (it reports the number on a pipe); return (":N", process)."""
    rfd, wfd = os.pipe()
    proc = subprocess.Popen(["Xvfb", "-displayfd", str(wfd), "-screen", "0",
                             f"{width}x{height + 80}x24", "-nolisten", "tcp"],
                            pass_fds=(wfd,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.close(wfd)
    deadline = time.monotonic() + _XVFB_READY_TIMEOUT
    data = b""
    while time.monotonic() < deadline and not data.endswith(b"\n"):
        chunk = os.read(rfd, 16)
        if not chunk:
            break
        data += chunk
    os.close(rfd)
    if not data.strip():
        proc.kill()
        raise RuntimeError("Xvfb did not report a display")
    return f":{data.strip().decode()}", proc


def main(sid: str) -> int:
    """Run one session to completion; return the engine's exit code (or 1 on runner failure)."""
    sess = session.load(sid)
    sess.runner_pid = os.getpid()
    try:
        display, xvfb = _start_xvfb(sess.width, sess.height)
    except (OSError, RuntimeError) as e:
        sess.status, sess.exit_info = "failed", {"error": f"xvfb: {e}", "at": time.time()}
        session.save(sess)
        return 1
    sess.display, sess.xvfb_pid = display, xvfb.pid
    env = envsetup.child_env(dict(os.environ), display, session.prefs_dir(sess), sess.renderer)
    log = open(session.run_dir(sess) / "engine.log", "ab")  # noqa: SIM115 - child owns it
    engine = subprocess.Popen(launcher.build_argv(sess), cwd=sess.fs2path, env=env,
                              stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    sess.engine_pid, sess.status = engine.pid, "running"
    session.save(sess)
    code = engine.wait()  # bounded by the scope's RuntimeMaxSec
    log.close()
    sig = signal.Signals(-code).name if code < 0 else None
    sess = session.load(sid)
    if sess.status != "stopped":
        sess.status = "exited"
    sess.exit_info = {"returncode": code, "signal": sig, "at": time.time()}
    session.save(sess)
    xvfb.terminate()
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
```

- [ ] **Step 5: Implement `fsoctl/launcher.py`** (≤ 60-line functions; split helpers as needed)

```python
"""launch / status / stop / list / cleanup.

A launch: create the session, install the bridge, write switches for the bridge, start the runner
in a named scope, wait (bounded) for the bridge heartbeat (game) or the window (qtFRED), then check
which renderer really came up and fail loudly on a Vulkan fallback.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import bridge_install, channel, paths, renderer, scope, session
from .cli import register
from .errors import EXIT_ENGINE, EXIT_LAUNCH, EXIT_REFUSED, HarnessError

DEFAULT_MAX_SESSIONS = 3


@dataclass
class LaunchRequest:
    """Everything `launch` needs, as one value (CLI, MCP and tests all build these)."""
    binary: str
    mission: str | None = None
    mods: list[str] = field(default_factory=list)
    renderer: str = "opengl"
    width: int = 1280
    height: int = 720
    skip_movies: bool = False
    pilot_mode: str = "chooser"
    pilot_name: str = "HarnessPilot"
    pilot_source: str | None = None
    dismiss_popups: bool = False
    skip_command_briefing: bool = False
    skip_briefing: bool = False
    noninteractive: bool = True
    allow_eval: bool = False
    extra_args: list[str] = field(default_factory=list)
    kind: str = "game"
    runtime_sec: int = 1800
    ready_timeout: float = 240.0


def _switches(req: LaunchRequest) -> dict:
    """Return the switch dict saved in the session and read by the bridge."""
    return {k: getattr(req, k) for k in ("skip_movies", "pilot_mode", "pilot_name",
                                          "dismiss_popups", "skip_command_briefing",
                                          "skip_briefing", "noninteractive")}


def _qtfred_mission_path(sess: session.Session) -> str:
    """Resolve a mission name to the absolute path qtFRED needs."""
    if not sess.mission or sess.mission.startswith("/"):
        return sess.mission or ""
    fs2 = Path(sess.fs2path)
    for mod in sess.mods:
        cand = fs2 / mod / "data" / "missions" / sess.mission
        if cand.exists():
            return str(cand)
    return str(fs2 / "data" / "missions" / sess.mission)


def build_argv(sess: session.Session) -> list[str]:
    """Return the engine or qtFRED command line for a session."""
    sw = sess.switches
    if sess.kind == "qtfred":
        argv = [sess.binary]
        if sess.mods:
            argv += ["-mod", ",".join(sess.mods)]
        if sess.mission:
            argv += ["-start_mission", _qtfred_mission_path(sess)]
        return argv + ["-nosound", "-nomovies"] + sess.extra_args
    argv = [sess.binary, "-mod", ",".join([paths.BRIDGE_MOD, *sess.mods]),
            "-window", "-res", f"{sess.width}x{sess.height}", "-nosound", "-no_unfocused_pause"]
    if sw.get("noninteractive", True):
        argv.append("-noninteractive")
    if sw.get("skip_movies"):
        argv.append("-nomovies")
    if sw.get("pilot_mode") == "existing":
        argv += ["-pilot", sw.get("pilot_name", "HarnessPilot")]
    if sess.mission:
        argv += ["-start_mission", sess.mission]
    if sess.renderer == "vulkan":
        argv.append("-vulkan")
    return argv + sess.extra_args


def check_capacity() -> None:
    """Refuse a new launch when the concurrent-session cap is reached."""
    cap = int(os.environ.get("FSO_HARNESS_MAX_SESSIONS", DEFAULT_MAX_SESSIONS))
    live = [s for s in session.list_sessions() if s.status in ("starting", "running") and alive(s)]
    if len(live) >= cap:
        raise HarnessError(EXIT_REFUSED, f"{len(live)} sessions already running (cap {cap})",
                           "stop one with `fsoctl stop <id>` or raise FSO_HARNESS_MAX_SESSIONS")


def alive(sess: session.Session) -> bool:
    """Return True while the session's engine process exists and its scope is active."""
    if not sess.engine_pid or not scope.is_active(sess.scope):
        return False
    try:
        os.kill(sess.engine_pid, 0)
    except OSError:
        return False
    return True
```

Continue in the same file:

```python
def _prepare(req: LaunchRequest) -> session.Session:
    """Create the session, install the bridge, seed pilot files and bridge switches."""
    fs2 = paths.fs2path()
    sess = session.new_session(kind=req.kind, binary=str(Path(req.binary).resolve()),
                               fs2path=str(fs2), mods=req.mods, mission=req.mission,
                               renderer=req.renderer, width=req.width, height=req.height,
                               switches=_switches(req), allow_eval=req.allow_eval,
                               extra_args=req.extra_args)
    if req.kind == "game":
        bridge_install.install(fs2)
        chan = paths.channel_dir(session.prefs_dir(sess))
        chan.mkdir(parents=True)
        (chan / "switches.json").write_text(json.dumps(sess.switches))
        if req.allow_eval:
            (chan / "allow_eval").touch()
    if req.pilot_source:
        _copy_pilot(Path(req.pilot_source), req.pilot_name, session.prefs_dir(sess))
    return sess


def _copy_pilot(src: Path, name: str, prefs: Path) -> None:
    """Copy one pilot's files (NAME.json, NAME.*.csg) from a players folder into the session."""
    files = [p for p in src.glob(f"{name}.*") if p.suffix in (".json", ".csg")]
    if not files:
        raise HarnessError(EXIT_LAUNCH, f"no pilot files for {name} in {src}",
                           "point --pilot-source at a data/players folder that has NAME.json")
    dest = prefs / "data" / "players"
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy2(f, dest / f.name)


def _wait_window(sess: session.Session, timeout: float) -> None:
    """Block (bounded) until qtFRED's window is on the session display."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = session.load(sess.id)
        if s.display and subprocess.run(["xdotool", "search", "--onlyvisible", "--name", "FRED"],
                                        env={**os.environ, "DISPLAY": s.display},
                                        capture_output=True, timeout=10, check=False).stdout.strip():
            return
        if s.status in ("exited", "failed"):
            raise HarnessError(EXIT_ENGINE, "qtFRED exited during startup", "see `fsoctl log <id>`")
        time.sleep(0.5)
    raise HarnessError(EXIT_LAUNCH, "qtFRED window did not appear", "see `fsoctl log <id>`")


def launch(req: LaunchRequest) -> dict:
    """Start a session and return {id, display, renderer, libraries, state} once it is ready."""
    check_capacity()
    sess = _prepare(req)
    run_dir = session.run_dir(sess)
    props = scope.caps(runtime_sec=req.runtime_sec)
    scope.start(sess.scope, [sys.executable, "-m", "fsoctl.runner", sess.id], dict(os.environ),
                run_dir / "runner.log", props)
    try:
        if req.kind == "game":
            hb = channel.Channel(sess).wait_ready(req.ready_timeout, lambda: _alive_or_starting(sess.id))
        else:
            _wait_window(sess, req.ready_timeout)
            hb = {}
        sess = session.load(sess.id)
        r = renderer.classify(renderer.read_maps(sess.engine_pid or 0))
    except HarnessError:
        stop(sess.id)
        raise
    if req.kind == "game" and req.renderer == "vulkan" and r["renderer"] != "vulkan":
        stop(sess.id)
        raise HarnessError(EXIT_LAUNCH, f"asked for Vulkan, engine came up as {r['renderer']}",
                           "check VK_ICD_FILENAMES and that the engine was built with Vulkan")
    return {"id": sess.id, "display": sess.display, **r, "state": hb.get("state")}


def _alive_or_starting(sid: str) -> bool:
    """True while a session is starting or alive (used during readiness waits)."""
    s = session.load(sid)
    return s.status == "starting" or alive(s)
```

And the remaining verbs:

```python
def require_running(sid: str) -> session.Session:
    """Return the session if its engine is running; else raise EXIT_ENGINE with exit info."""
    sess = session.load(sid)
    if not alive(sess):
        raise HarnessError(EXIT_ENGINE, f"session {sid} is not running ({sess.status}, "
                           f"exit {sess.exit_info})", f"run `fsoctl crash_report {sid}` or relaunch")
    return sess


def status(sid: str) -> dict:
    """Return the session record plus liveness and the latest bridge heartbeat."""
    sess = session.load(sid)
    return {**asdict(sess), "alive": alive(sess), "heartbeat": channel.Channel(sess).heartbeat()}


def stop(sid: str) -> dict:
    """Stop a session's scope (idempotent) and mark it stopped; keep the run folder."""
    sess = session.load(sid)
    scope.stop(sess.scope)
    sess = session.load(sid)
    if sess.status in ("starting", "running"):
        sess.status = "stopped"
        session.save(sess)
    return {"id": sid, "status": sess.status, "exit_info": sess.exit_info}


def list_all() -> dict:
    """Return a short line per session."""
    return {"sessions": [{"id": s.id, "kind": s.kind, "status": s.status, "alive": alive(s),
                          "mission": s.mission, "renderer": s.renderer}
                         for s in session.list_sessions()]}


def cleanup(older_than_sec: float = 0) -> dict:
    """Delete run folders of sessions that are not alive and older than the given age."""
    removed = []
    now = time.time()
    for s in session.list_sessions():
        if not alive(s) and now - s.created >= older_than_sec:
            shutil.rmtree(session.run_dir(s))
            removed.append(s.id)
    return {"removed": removed}
```

Register the CLI verbs at the bottom of `launcher.py` with `@register("launch", ...)` etc. (configure functions add the flags listed in Interfaces; `--pilot existing:NAME` parsing splits on `:`; `--res WxH` parsing validates two positive ints and raises EXIT_USAGE otherwise). Add `"launcher"` to the tuple in `cli._load_verb_modules`.

- [ ] **Step 6: Run** `pytest -q && ruff check .` then `bounded-run pytest -m engine -p no:xdist tests/test_launch_engine.py -q` — Expected: unit PASS; engine PASS (with the pilot workaround noted in Step 2).
- [ ] **Step 7: Red-proofs** — (a) make `stop` raise when already stopped → idempotence assertion fails; (b) drop `-noninteractive` from defaults → `test_argv_defaults` fails. Restore both.
- [ ] **Step 8: Commit** `git add -A && git commit -m "Runner in a named scope, launch/status/stop/list/cleanup"`

---

### Task 8: Startup stage switches in the bridge

**Files:**
- Modify: `fsoctl/bridge/fso_harness_bridge/data/scripts/fsoharness.lua`
- Create: `tests/test_stages_engine.py`
- Modify: `tests/test_launch_engine.py` (switch to `pilot_mode="create"`)

**Interfaces:**
- Consumes: bridge core (`H.on_state`, `H.switches`, `H.handlers`), probe results (Task 1) for exact API behaviour.
- Produces: bridge behaviour per switch (spec §5); handler `answer_popup` `{"choice": int}` → resolves the current popup (only valid while one is up, else refusal naming the state); heartbeat `popup` field = popup text while one is up; every auto-dismissed popup appended to `popups.log` in the channel folder (text + chosen button).

- [ ] **Step 1: Write the failing engine tests** (`tests/test_stages_engine.py`)

```python
import os

import pytest

from fsoctl import channel, launcher, session
from fsoctl.launcher import LaunchRequest

pytestmark = pytest.mark.engine
MODS = [os.environ.get("FSO_TEST_MODS", "MVPS-4.7.3")]
MISSION = os.environ.get("FSO_TEST_MISSION", "SM1-01.fs2")


def _run(engine_env, **kw):
    _, fso = engine_env
    return launcher.launch(LaunchRequest(binary=str(fso), mods=MODS, **kw))


def _wait_state(sid, want, timeout=180):
    import time
    ch = channel.Channel(session.load(sid))
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        hb = ch.heartbeat() or {}
        if hb.get("state") == want:
            return hb
        time.sleep(1)
    pytest.fail(f"never reached {want}; last {ch.heartbeat()}")


def test_create_pilot_reaches_main_menu(engine_env, tmp_runtime):
    r = _run(engine_env, pilot_mode="create", pilot_name="HTest", skip_movies=True,
             dismiss_popups=True)
    try:
        _wait_state(r["id"], "GS_STATE_MAIN_MENU")
    finally:
        launcher.stop(r["id"])


def test_full_skip_lands_in_gameplay(engine_env, tmp_runtime):
    r = _run(engine_env, pilot_mode="create", skip_movies=True, dismiss_popups=True,
             mission=MISSION, skip_command_briefing=True, skip_briefing=True)
    try:
        hb = _wait_state(r["id"], "GS_STATE_GAME_PLAY")
        assert hb["mission"].lower().startswith(MISSION.lower().removesuffix(".fs2"))
    finally:
        launcher.stop(r["id"])


def test_no_skips_stops_at_command_briefing(engine_env, tmp_runtime):
    r = _run(engine_env, pilot_mode="create", skip_movies=True, dismiss_popups=True,
             mission=MISSION)
    try:
        _wait_state(r["id"], "GS_STATE_CMD_BRIEF")
    finally:
        launcher.stop(r["id"])


def test_popup_without_dismiss_is_visible_and_answerable(engine_env, tmp_runtime):
    r = _run(engine_env, pilot_mode="create", skip_movies=True)
    try:
        ch = channel.Channel(session.load(r["id"]))
        hb = _wait_state(r["id"], "GS_STATE_MAIN_MENU")
        if not hb.get("popup"):
            pytest.skip("no startup popup on this setup (record in probe results)")
        ch.send("answer_popup", {"choice": 0}, timeout=20)
    finally:
        launcher.stop(r["id"])


def test_ready_timeout_returns_structured_error(engine_env, tmp_runtime):
    from fsoctl.errors import EXIT_TIMEOUT, HarnessError
    with pytest.raises(HarnessError) as e:
        _run(engine_env, ready_timeout=1.0)
    assert e.value.code == EXIT_TIMEOUT and "screenshot" in e.value.hint
    assert all(not launcher.alive(s) for s in session.list_sessions())  # Review Focus 1
```
(State names come from `gamesequence.cpp`; confirm `GS_STATE_CMD_BRIEF` and `GS_STATE_INITIAL_PLAYER_SELECT` against the probe log before running.)

- [ ] **Step 2: Run** `bounded-run pytest -m engine -p no:xdist tests/test_stages_engine.py -q` — Expected: FAIL (switches not implemented: create-pilot test times out at pilot select).

- [ ] **Step 3: Implement the switches in `fsoharness.lua`** (append before `return H`)

```lua
local function log_popup(text, choice)
	local f = cf.openFile("popups.log", "a", H.DIR)
	if f and f:isValid() then
		f:write(json.encode({ frame = H.frames, text = text, choice = choice }) .. "\n")
		f:close()
	end
end

H.pending_popup_choice = nil

-- popups: called from On Dialog Frame through H.frame(true)
local base_frame = H.frame
function H.frame(in_dialog)
	if in_dialog then
		H.popup = hv.Text and tostring(hv.Text) or "popup"
		if H.pending_popup_choice ~= nil then
			local c = H.pending_popup_choice
			H.pending_popup_choice = nil
			log_popup(H.popup, c)
			hv.Submit(c)
		elseif H.switches.dismiss_popups then
			log_popup(H.popup, 0)
			hv.Submit(0)
		end
	end
	base_frame(in_dialog)
end

H.handlers.answer_popup = function(args)
	if not H.popup then
		error({ msg = "no popup is open (state " .. H.state_name() .. ")", hint = "check the heartbeat popup field" })
	end
	H.pending_popup_choice = args.choice or 0
	return { answered = H.popup }
end

H.on_state("GS_STATE_INITIAL_PLAYER_SELECT", function()
	if H.switches.pilot_mode == "create" then
		local name = H.switches.pilot_name or "HarnessPilot"
		ui.PilotSelect.createPilot(name)
		ui.PilotSelect.selectPilot(name)
	end
end)

H.on_state("GS_STATE_CMD_BRIEF", function()
	if H.switches.skip_command_briefing then
		ba.postGameEvent(ba.GameEvents["GS_EVENT_START_BRIEFING"])
	end
end)

H.briefing_pending = false
H.on_state("GS_STATE_BRIEFING", function()
	if H.switches.skip_briefing then H.briefing_pending = true end
end)

H.timers[#H.timers + 1] = { tick = function()
	if H.briefing_pending and H.state_name() == "GS_STATE_BRIEFING" then
		H.briefing_pending = false
		H.last_commit = ui.Briefing.commitToMission()
	end
	return false
end }
```
WHY the briefing commit runs from a frame timer and not directly in On State Start: the briefing screen initialises after the state-start hook; committing before its init would act on half-built loadout state. Adjust only if the Task 1 probe showed a direct call works (record the choice in the commit body). If the probe showed a red-alert mission posts a different event, branch on `mn.isRedAlertMission()` (or the API the probe found) and post `GS_EVENT_RED_ALERT`. If the probe showed popup text is not `hv.Text`, use the name it found.

- [ ] **Step 4: Run** the stage tests and `tests/test_launch_engine.py` (now with `pilot_mode="create"`) — Expected: PASS (the popup test may skip with a recorded reason).
- [ ] **Step 5: Red-proof** — comment out the `GS_EVENT_START_BRIEFING` post; `test_full_skip_lands_in_gameplay` must fail (stuck at command briefing); restore.
- [ ] **Step 6: Commit** `git add -A && git commit -m "Bridge stage switches: create pilot, popups, skip briefings"`


---

### Task 9: Screenshots and X input

**Files:**
- Create: `fsoctl/xinput.py`, `tests/test_xinput_unit.py`, `tests/test_xinput_engine.py`
- Modify: `fsoctl/cli.py` (add `"xinput"` to the verb-module tuple)

**Interfaces:**
- Consumes: `launcher.require_running`, `session.run_dir`.
- Produces:
  - `xinput.window_id(sess) -> str` (game: title contains "FreeSpace"; qtFRED: "FRED"; raises EXIT_NOT_FOUND)
  - `xinput.screenshot(sid: str, crop: str | None = None, scale: float | None = None) -> dict` → `{"path": str, "width": int, "height": int}`; files in `<run>/shots/shot-NNNN.png`; `crop` = `WxH+X+Y`; `scale` 0.1-1.0.
  - `xinput.keys(sid: str, keys: list[str], hold_ms: int = 0) -> dict` (xdotool key names, e.g. `Return`, `alt+r`, `KP_4`; XTEST only)
  - `xinput.click(sid: str, x: int, y: int, button: int = 1, double: bool = False) -> dict`
  - `xinput.drag(sid: str, x1: int, y1: int, x2: int, y2: int, button: int = 1) -> dict`
  - `xinput.scroll(sid: str, x: int, y: int, clicks: int) -> dict` (positive = up/in, negative = down/out)
  - `xinput.focus_neutral(sess) -> None` — click a neutral point (bottom-right status area) before wheel/keys after a modal dialog closed (no window manager; finding 2026-10-12).
  - `xinput.journal(sess, kind, args)` is called by every action (record/replay, Task 14).
  - CLI verbs: `screenshot`, `keys`, `click`, `drag`, `scroll`.

- [ ] **Step 1: Write the failing unit test** (argument validation only; no X)

```python
import pytest

from fsoctl import xinput
from fsoctl.errors import EXIT_USAGE, HarnessError


@pytest.mark.parametrize("crop", ["10x10", "axb+1+1", "0x5+0+0", "5x5+-1+0"])
def test_bad_crop_rejected(crop):
    with pytest.raises(HarnessError) as e:
        xinput.parse_crop(crop)
    assert e.value.code == EXIT_USAGE


def test_good_crop_parsed():
    assert xinput.parse_crop("400x300+10+20") == (400, 300, 10, 20)


def test_scale_bounds():
    with pytest.raises(HarnessError):
        xinput.check_scale(1.5)
    assert xinput.check_scale(0.5) == 0.5


def test_key_names_reject_shell_metacharacters():
    with pytest.raises(HarnessError):
        xinput.check_keys(["Return; rm -rf ~"])
    assert xinput.check_keys(["alt+r", "KP_4", "Return"]) == ["alt+r", "KP_4", "Return"]
```

- [ ] **Step 2: Write the failing engine test**

```python
import os

import pytest
from PIL import Image  # test-only dependency (python3-pillow); not imported by fsoctl

from fsoctl import launcher, xinput
from fsoctl.launcher import LaunchRequest

pytestmark = pytest.mark.engine


def test_screenshot_is_the_game_window_and_not_blank(engine_env, tmp_runtime):
    _, fso = engine_env
    r = launcher.launch(LaunchRequest(binary=str(fso), mods=[os.environ.get("FSO_TEST_MODS", "MVPS-4.7.3")],
                                      pilot_mode="create", skip_movies=True, dismiss_popups=True,
                                      width=1024, height=768))
    try:
        shot = xinput.screenshot(r["id"])
        assert (shot["width"], shot["height"]) == (1024, 768)
        im = Image.open(shot["path"]).convert("L")
        assert max(im.getdata()) > 40   # the main hall is not black
        small = xinput.screenshot(r["id"], scale=0.5)
        assert small["width"] == 512
    finally:
        launcher.stop(r["id"])
```

- [ ] **Step 3: Run** — Expected: FAIL (module missing).

- [ ] **Step 4: Implement `fsoctl/xinput.py`**

```python
"""Screenshots and keyboard/mouse input on a session's virtual display.

WHY XTEST (plain `xdotool key`) and never `xdotool --window`: SDL ignores synthetic XSendEvent
input; XTEST events look like real hardware. There is no window manager, so the game window sits at
0,0 and root coordinates equal window coordinates. Prefer bridge commands over keys where one exists
(an earlier lab test showed F-keys sent via XTEST never reached SDL).
"""
import os
import re
import subprocess
import time
from pathlib import Path

from . import launcher, session
from .cli import register
from .errors import EXIT_NOT_FOUND, EXIT_USAGE, HarnessError

_CROP = re.compile(r"^(\d+)x(\d+)\+(\d+)\+(\d+)$")
_KEY = re.compile(r"^[A-Za-z0-9_+]+$")
_TIMEOUT = 15


def parse_crop(text: str) -> tuple[int, int, int, int]:
    """Parse WxH+X+Y into ints; reject anything else."""
    m = _CROP.match(text or "")
    if not m or int(m.group(1)) == 0 or int(m.group(2)) == 0:
        raise HarnessError(EXIT_USAGE, f"bad crop {text!r}", "use WxH+X+Y, e.g. 400x300+10+20")
    return tuple(int(g) for g in m.groups())


def check_scale(value: float) -> float:
    """Return a scale factor in (0, 1]; reject others."""
    if not 0.05 <= value <= 1.0:
        raise HarnessError(EXIT_USAGE, f"bad scale {value}", "use a value between 0.05 and 1.0")
    return value


def check_keys(keys: list[str]) -> list[str]:
    """Return key names after checking they are plain xdotool key names."""
    for k in keys:
        if not _KEY.match(k):
            raise HarnessError(EXIT_USAGE, f"bad key name {k!r}", "use xdotool names like Return, alt+r")
    return keys


def _x(sess: session.Session, *args: str) -> str:
    """Run an X tool against the session display and return stdout."""
    env = {**os.environ, "DISPLAY": sess.display or ""}
    env.pop("WAYLAND_DISPLAY", None)
    r = subprocess.run(list(args), env=env, capture_output=True, text=True, timeout=_TIMEOUT, check=False)
    if r.returncode != 0:
        raise HarnessError(EXIT_NOT_FOUND, f"{args[0]} failed: {r.stderr.strip()[:200]}",
                           "is the session still running? `fsoctl status <id>`")
    return r.stdout


def window_id(sess: session.Session) -> str:
    """Return the X window id of the game or qtFRED window."""
    name = "FRED" if sess.kind == "qtfred" else "FreeSpace"
    out = _x(sess, "xdotool", "search", "--onlyvisible", "--name", name).split()
    if not out:
        raise HarnessError(EXIT_NOT_FOUND, "no game window on the session display", "take a screenshot")
    return out[0]
```

Continue (screenshot, keys, click, drag, scroll, journal hook, CLI registration):

```python
def screenshot(sid: str, crop: str | None = None, scale: float | None = None) -> dict:
    """Capture the session window to <run>/shots/shot-NNNN.png and return path and size."""
    sess = launcher.require_running(sid)
    shots = session.run_dir(sess) / "shots"
    shots.mkdir(exist_ok=True)
    path = shots / f"shot-{len(list(shots.glob('shot-*.png'))) + 1:04d}.png"
    args = ["import", "-window", window_id(sess)]
    if crop:
        w, h, x, y = parse_crop(crop)
        args += ["-crop", f"{w}x{h}+{x}+{y}", "+repage"]
    if scale:
        args += ["-resize", f"{check_scale(scale) * 100:.0f}%"]
    _x(sess, *args, str(path))
    size = _x(sess, "identify", "-format", "%w %h", str(path)).split()
    return {"path": str(path), "width": int(size[0]), "height": int(size[1])}


def _focus(sess: session.Session) -> None:
    """Give the session window keyboard focus (works without a window manager)."""
    _x(sess, "xdotool", "windowfocus", window_id(sess))


def journal(sess: session.Session, kind: str, args: dict) -> None:
    """Record an input action in the session journal (record/replay, Task 14)."""
    from . import record
    record.append(sess, kind, args)


def keys(sid: str, names: list[str], hold_ms: int = 0) -> dict:
    """Press keys via XTEST (each optionally held for hold_ms)."""
    sess = launcher.require_running(sid)
    check_keys(names)
    _focus(sess)
    for k in names:
        if hold_ms:
            _x(sess, "xdotool", "keydown", k)
            time.sleep(hold_ms / 1000)
            _x(sess, "xdotool", "keyup", k)
        else:
            _x(sess, "xdotool", "key", k)
    journal(sess, "keys", {"keys": names, "hold_ms": hold_ms})
    return {"pressed": names}


def click(sid: str, x: int, y: int, button: int = 1, double: bool = False) -> dict:
    """Move to (x, y) and click; coordinates are window coordinates (window at 0,0)."""
    sess = launcher.require_running(sid)
    extra = ["--repeat", "2"] if double else []
    _x(sess, "xdotool", "mousemove", str(x), str(y), "click", *extra, str(button))
    journal(sess, "click", {"x": x, "y": y, "button": button, "double": double})
    return {"clicked": [x, y]}


def drag(sid: str, x1: int, y1: int, x2: int, y2: int, button: int = 1) -> dict:
    """Press at (x1,y1), move through the midpoint to (x2,y2), release (marquee/orbit drags)."""
    sess = launcher.require_running(sid)
    mid = ((x1 + x2) // 2, (y1 + y2) // 2)
    _x(sess, "xdotool", "mousemove", str(x1), str(y1), "mousedown", str(button))
    for px, py in (mid, (x2, y2)):
        time.sleep(0.3)
        _x(sess, "xdotool", "mousemove", str(px), str(py))
    time.sleep(0.3)
    _x(sess, "xdotool", "mouseup", str(button))
    journal(sess, "drag", {"from": [x1, y1], "to": [x2, y2], "button": button})
    return {"dragged": [[x1, y1], [x2, y2]]}


def focus_neutral(sess: session.Session) -> None:
    """Click a neutral spot so a closed modal dialog no longer swallows input."""
    _x(sess, "xdotool", "mousemove", str(sess.width - 20), str(sess.height + 40), "click", "1")


def scroll(sid: str, x: int, y: int, clicks: int) -> dict:
    """Scroll the wheel at (x, y): positive clicks = up (button 4), negative = down (button 5)."""
    sess = launcher.require_running(sid)
    button = "4" if clicks > 0 else "5"
    for _ in range(min(abs(clicks), 50)):
        _x(sess, "xdotool", "mousemove", str(x), str(y), "click", button)
    journal(sess, "scroll", {"x": x, "y": y, "clicks": clicks})
    return {"scrolled": clicks}
```

Register verbs `screenshot` (`id`, `--crop`, `--scale`), `keys` (`id`, `keys...`, `--hold-ms`), `click` (`id x y`, `--button`, `--double`), `drag` (`id x1 y1 x2 y2`, `--button`), `scroll` (`id x y clicks`). Until Task 14 exists, `journal` must not fail: create `fsoctl/record.py` now with `def append(sess, kind, args): pass` and a docstring saying Task 14 fills it in.

- [ ] **Step 5: Run** unit + engine tests — Expected: PASS.
- [ ] **Step 6: Red-proof** — screenshot the root window instead of the game window; the size assertion fails (root is height + 80); restore.
- [ ] **Step 7: Commit** `git add -A && git commit -m "Screenshots and XTEST input on the session display"`

---

### Task 10: Logs and problems

**Files:**
- Create: `fsoctl/logs.py`, `tests/test_logs.py`, `tests/data/fs2_open_sample.log`
- Modify: `fsoctl/cli.py` (module tuple gains `"logs"`)

**Interfaces:**
- Produces:
  - `logs.sources(sess) -> dict[str, Path]` → `{"engine": <run>/engine.log, "fso": <prefs>/data/fs2_open.log}` (fso log exists only for debug builds or -stdout_log, which also lands in engine.log)
  - `logs.read(sid: str, source: str = "auto", tail: int = 200, grep: str | None = None) -> dict` → `{"source": str, "lines": [...], "problems": [...]}`; `auto` = fso log if present else engine log; `tail` capped at 5000; `grep` is a Python regex (invalid → EXIT_USAGE).
  - `logs.problems(lines: list[str]) -> list[dict]` → `{"kind": "warning"|"error"|"assert"|"lua"|"int3", "line": int, "text": str}`.
  - CLI verb `log`.

- [ ] **Step 1: Write the sample log fixture** `tests/data/fs2_open_sample.log` (lines copied from a real debug log shape; keep it small):

```
FreeSpace 2 Open version: 26.1.0
Initializing OpenGL graphics device at 1280x720 with 32-bit color...
WARNING: "Ship class 'GTF Ulysses' has no $Subsystem: entries" at ship.cpp:4021
Warning: Missing table entry for hud gauge
LUA ERROR: [string "fsoharness.lua"]:12: attempt to index a nil value
ASSERTION: "objnum >= 0" at object.cpp:812
Int3(): From aicode.cpp at line 4410
Error: Unable to find model 'missing.pof'
Mission loaded: sm1-01.fs2
```

- [ ] **Step 2: Write the failing tests**

```python
from pathlib import Path

import pytest

from fsoctl import logs
from fsoctl.errors import EXIT_USAGE, HarnessError

SAMPLE = (Path(__file__).parent / "data" / "fs2_open_sample.log").read_text().splitlines()


def test_problems_classifies_each_kind():
    kinds = [p["kind"] for p in logs.problems(SAMPLE)]
    assert kinds == ["warning", "warning", "lua", "assert", "int3", "error"]


def test_problems_carries_line_numbers():
    first = logs.problems(SAMPLE)[0]
    assert first["line"] == 3 and "GTF Ulysses" in first["text"]


def test_filter_and_tail():
    out = logs.filter_lines(SAMPLE, tail=3, grep=None)
    assert out == SAMPLE[-3:]
    assert logs.filter_lines(SAMPLE, tail=100, grep=r"Mission loaded") == [SAMPLE[-1]]


def test_bad_regex():
    with pytest.raises(HarnessError) as e:
        logs.filter_lines(SAMPLE, tail=10, grep="(")
    assert e.value.code == EXIT_USAGE
```

- [ ] **Step 3: Run** — Expected: FAIL.

- [ ] **Step 4: Implement `fsoctl/logs.py`**

```python
"""Engine logs: tail, filter, and pull out the lines an agent must not miss.

WHY two sources: release builds write no fs2_open.log (logging is compiled out), so the engine's
stdout/stderr (engine.log, written by the runner) is the only text; debug builds write the full
log under the session prefs. Problems are matched case-insensitively at line start.
"""
import re

from . import session
from .cli import register
from .errors import EXIT_NOT_FOUND, EXIT_USAGE, HarnessError

_TAIL_MAX = 5000
_PATTERNS = [
    ("lua", re.compile(r"^\s*LUA ERROR", re.I)),
    ("assert", re.compile(r"^\s*ASSERTION", re.I)),
    ("int3", re.compile(r"^\s*Int3\(\)", re.I)),
    ("warning", re.compile(r"^\s*WARNING", re.I)),
    ("error", re.compile(r"^\s*(ERROR|Fatal)", re.I)),
]


def problems(lines: list[str]) -> list[dict]:
    """Return warning/error/assert/lua/int3 lines with 1-based line numbers."""
    out = []
    for i, line in enumerate(lines, 1):
        for kind, pat in _PATTERNS:
            if pat.match(line):
                out.append({"kind": kind, "line": i, "text": line.strip()})
                break
    return out


def filter_lines(lines: list[str], tail: int, grep: str | None) -> list[str]:
    """Apply an optional regex filter, then keep the last `tail` lines (capped)."""
    if grep:
        try:
            pat = re.compile(grep)
        except re.error as e:
            raise HarnessError(EXIT_USAGE, f"bad regex: {e}", "use Python regex syntax") from e
        lines = [x for x in lines if pat.search(x)]
    return lines[-min(max(tail, 1), _TAIL_MAX):]


def sources(sess: session.Session) -> dict:
    """Return the engine stdout log and the FSO debug log paths for a session."""
    return {"engine": session.run_dir(sess) / "engine.log",
            "fso": session.prefs_dir(sess) / "data" / "fs2_open.log"}


def read(sid: str, source: str = "auto", tail: int = 200, grep: str | None = None) -> dict:
    """Return filtered log lines and the problems found in the whole chosen log."""
    sess = session.load(sid)
    srcs = sources(sess)
    if source == "auto":
        source = "fso" if srcs["fso"].exists() else "engine"
    path = srcs.get(source)
    if path is None or not path.exists():
        raise HarnessError(EXIT_NOT_FOUND, f"no {source} log yet",
                           "release builds only have the engine log; debug builds add fs2_open.log")
    lines = path.read_text(errors="replace").splitlines()
    return {"source": source, "lines": filter_lines(lines, tail, grep), "problems": problems(lines)}
```
Register verb `log` (`id`, `--source {auto,engine,fso}`, `--tail N`, `--grep REGEX`).

- [ ] **Step 5: Run** — Expected: PASS.
- [ ] **Step 6: Red-proof** — change the `int3` pattern to match `Int4\(\)`; `test_problems_classifies_each_kind` fails; restore.
- [ ] **Step 7: Commit** `git add -A && git commit -m "Log tail/filter and problems extraction"`

---

### Task 11: Game state, sexps, popups, opt-in Lua and events

**Files:**
- Modify: `fsoctl/bridge/fso_harness_bridge/data/scripts/fsoharness.lua`
- Create: `fsoctl/game.py`, `tests/test_game_unit.py`, `tests/test_game_engine.py`
- Modify: `fsoctl/cli.py` (module tuple gains `"game"`)

**Interfaces:**
- Consumes: `channel.Channel`, `launcher.require_running`, `launcher.alive`, `session.audit`.
- Produces:
  - Bridge handlers: `state` `{ships: bool, goals: bool, events: bool, limit: int}` → `{state, mission, mission_time, paused, player: {name, class, pos, hull_pct, target} | null, ships: [...], goals: [...], events: [...]}`; ship entry `{name, class, team, pos: [x,y,z], hull_pct, dying, departed}`; goal `{name, status: "complete"|"failed"|"incomplete"}`; event `{name, status: "true"|"false"|"pending"}` (via `is-event-true-delay`/`is-event-false-delay` sexps; names quoted with `!`); `sexp` `{expr, mode: "eval"|"run"|"number"}` → `{"value": bool|number}`; `lua` `{code}` → `{"value": string}` (refused unless `allow_eval` file present); `post_event` `{name}` (same gate).
  - `game.ch(sid) -> tuple[Session, Channel]`; `game.call(sid, cmd, args, timeout=10) -> Any` (uses `alive` for EXIT_ENGINE detection); `game.state(sid, ships=True, goals=True, events=True, limit=200)`; `game.sexp(sid, expr, mode="eval")`; `game.lua(sid, code)` (checks `allow_eval`, audits, then sends); `game.post_event(sid, name)` (same); `game.answer_popup(sid, choice)`.
  - CLI verbs: `state`, `sexp`, `lua`, `post_event`, `answer_popup`.

- [ ] **Step 1: Write the failing unit tests** (gate + audit, with the FakeBridge)

```python
import json

import pytest

from fsoctl import game, session
from fsoctl.errors import EXIT_ENGINE, EXIT_REFUSED, HarnessError
from tests.fake_bridge import FakeBridge


def _sess(allow):
    return session.new_session(kind="game", binary="/bin/true", fs2path="/tmp", mods=[],
                               mission=None, renderer="opengl", width=1, height=1, switches={},
                               allow_eval=allow, extra_args=[])


def test_lua_refused_without_opt_in(tmp_runtime, monkeypatch):
    s = _sess(False)
    monkeypatch.setattr(game.launcher, "require_running", lambda sid: s)
    with pytest.raises(HarnessError) as e:
        game.lua(s.id, "return 1")
    assert e.value.code == EXIT_REFUSED and "allow-eval" in e.value.hint


def test_lua_allowed_is_audited(tmp_runtime, monkeypatch):
    s = _sess(True)
    monkeypatch.setattr(game.launcher, "require_running", lambda sid: s)
    monkeypatch.setattr(game.launcher, "alive", lambda sess: True)
    with FakeBridge(game.channel.Channel(s).dir):
        game.lua(s.id, "return 1")
    entry = json.loads((session.run_dir(s) / "audit.log").read_text().splitlines()[0])
    assert entry == {**entry, "kind": "lua", "text": "return 1"}


def test_call_on_dead_engine_is_engine_error(tmp_runtime, monkeypatch):
    s = _sess(False)
    monkeypatch.setattr(game.launcher, "require_running",
                        lambda sid: (_ for _ in ()).throw(HarnessError(EXIT_ENGINE, "dead", "crash_report")))
    with pytest.raises(HarnessError) as e:
        game.state(s.id)
    assert e.value.code == EXIT_ENGINE  # Review Focus 2: no 30 s timeout


def test_sexp_mode_validated(tmp_runtime):
    with pytest.raises(HarnessError):
        game.check_mode("maybe")
```

- [ ] **Step 2: Write the failing engine test**

```python
import os

import pytest

from fsoctl import game, launcher
from fsoctl.launcher import LaunchRequest

pytestmark = pytest.mark.engine


@pytest.fixture
def in_mission(engine_env, tmp_runtime):
    _, fso = engine_env
    r = launcher.launch(LaunchRequest(binary=str(fso), mods=[os.environ.get("FSO_TEST_MODS", "MVPS-4.7.3")],
                                      pilot_mode="create", skip_movies=True, dismiss_popups=True,
                                      mission=os.environ.get("FSO_TEST_MISSION", "SM1-01.fs2"),
                                      skip_command_briefing=True, skip_briefing=True, allow_eval=True))
    game.wait_for(r["id"], "state", "GS_STATE_GAME_PLAY", timeout=180)
    yield r["id"]
    launcher.stop(r["id"])


def test_state_lists_player_and_ships(in_mission):
    st = game.state(in_mission)
    assert st["state"] == "GS_STATE_GAME_PLAY"
    assert st["player"] and st["player"]["hull_pct"] > 0
    assert any(s["name"] == st["player"]["name"] for s in st["ships"])
    assert all(e["status"] in ("true", "false", "pending") for e in st["events"])


def test_sexp_eval_and_number(in_mission):
    assert game.sexp(in_mission, "( true )")["value"] is True
    assert game.sexp(in_mission, "( + 2 3 )", mode="number")["value"] == 5


def test_lua_eval_when_allowed(in_mission):
    assert game.lua(in_mission, "return 6*7")["value"] == "42"
```
(`wait_for` lands in Task 13; implement Task 13's `state` condition first if executing strictly in order, or reorder Task 13 Step 3 before this engine test — note the order used in the commit.)

- [ ] **Step 3: Run** — Expected: FAIL.

- [ ] **Step 4: Add the bridge handlers** (append before `return H`; verify every property name against `docs/research/scripting.lua` first — e.g. `HitpointsMax`, `Team.Name`, `Class.Name`, `isDying`, `isDeparting` or the names it actually has)

```lua
local function vec(v) return { v.x, v.y, v.z } end

local function ship_entry(s)
	local maxhp = s.HitpointsMax or 0
	local pct = 0
	if maxhp > 0 then pct = s.HitpointsLeft * 100 / maxhp end
	return {
		name = s.Name, class = s.Class and s.Class.Name or "?",
		team = s.Team and s.Team.Name or "?", pos = vec(s.Position),
		hull_pct = pct, dying = s:isDying() == true,
	}
end

local function quoted(name) return "!" .. string.gsub(name, "!", "!!") .. "!" end

local function event_status(name)
	if mn.evaluateSEXP("( is-event-true-delay " .. quoted(name) .. " 0 )") then return "true" end
	if mn.evaluateSEXP("( is-event-false-delay " .. quoted(name) .. " 0 )") then return "false" end
	return "pending"
end

local GOAL = { [0] = "failed", [1] = "complete", [2] = "incomplete" }

H.handlers.state = function(args)
	local out = { state = H.state_name(), mission = mn.getMissionFilename(),
	              mission_time = mn.getMissionTime(), paused = H.state_name() == "GS_STATE_GAME_PAUSED",
	              ships = {}, goals = {}, events = {}, player = json.null }
	if not mn.isInMission() then return out end
	local limit = args.limit or 200
	if hv.Player and hv.Player:isValid() then
		local p = ship_entry(hv.Player)
		local t = hv.Player.Target
		p.target = (t and t:isValid() and t.Name) or json.null
		out.player = p
	end
	if args.ships ~= false then
		for i = 1, math.min(#mn.Ships, limit) do out.ships[#out.ships + 1] = ship_entry(mn.Ships[i]) end
	end
	if args.goals ~= false then
		for i = 1, math.min(#mn.Goals, limit) do
			local g = mn.Goals[i]
			out.goals[#out.goals + 1] = { name = g.Name, status = GOAL[g.isGoalSatisfied] or "incomplete" }
		end
	end
	if args.events ~= false then
		for i = 1, math.min(#mn.Events, limit) do
			local e = mn.Events[i]
			out.events[#out.events + 1] = { name = e.Name, status = event_status(e.Name) }
		end
	end
	return out
end

H.handlers.sexp = function(args)
	local mode = args.mode or "eval"
	if mode == "run" then return { value = mn.runSEXP(args.expr) } end
	if mode == "number" then return { value = mn.evaluateNumericSEXP(args.expr) } end
	return { value = mn.evaluateSEXP(args.expr) }
end

local function require_eval()
	if not H.allow_eval then
		error({ msg = "free-form Lua and raw events are off for this session", hint = "relaunch with --allow-eval" })
	end
end

H.handlers.lua = function(args)
	require_eval()
	local fn, err = loadstring(args.code)
	if not fn then error({ msg = "compile error: " .. tostring(err), hint = "Lua 5.1 syntax" }) end
	return { value = tostring(fn()) }
end

H.handlers.post_event = function(args)
	require_eval()
	local ev = ba.GameEvents[args.name]
	if not ev then error({ msg = "unknown game event " .. tostring(args.name), hint = "see gamesequence.h names" }) end
	ba.postGameEvent(ev)
	return {}
end
```

- [ ] **Step 5: Implement `fsoctl/game.py`** (send through the channel with liveness; gate + audit on the Python side too, so a refusal never depends on the bridge alone)

```python
"""Game verbs that talk to the bridge: state, sexp, popups, opt-in Lua and raw events."""
from typing import Any

from . import channel, launcher, session
from .cli import register
from .errors import EXIT_REFUSED, EXIT_USAGE, HarnessError

_MODES = ("eval", "run", "number")


def check_mode(mode: str) -> str:
    """Return a valid sexp mode or raise EXIT_USAGE."""
    if mode not in _MODES:
        raise HarnessError(EXIT_USAGE, f"bad mode {mode!r}", f"use one of {', '.join(_MODES)}")
    return mode


def call(sid: str, cmd: str, args: dict | None = None, timeout: float = 10.0) -> Any:
    """Send one bridge command to a running session, with engine-death detection."""
    sess = launcher.require_running(sid)
    return channel.Channel(sess).send(cmd, args, timeout=timeout, alive=lambda: launcher.alive(sess))


def state(sid: str, ships: bool = True, goals: bool = True, events: bool = True, limit: int = 200) -> dict:
    """Return game state, player, ships, goals and events."""
    return call(sid, "state", {"ships": ships, "goals": goals, "events": events, "limit": min(limit, 1000)},
                timeout=20)


def sexp(sid: str, expr: str, mode: str = "eval") -> dict:
    """Evaluate (bool), run (inside `when`) or evaluate numerically a sexp."""
    return call(sid, "sexp", {"expr": expr, "mode": check_mode(mode)})


def _gate(sid: str, kind: str, text: str) -> None:
    """Refuse unless the session allows eval; audit the action when it does."""
    sess = launcher.require_running(sid)
    if not sess.allow_eval:
        raise HarnessError(EXIT_REFUSED, f"{kind} is off for this session", "relaunch with --allow-eval")
    session.audit(sess, kind, text)


def lua(sid: str, code: str) -> dict:
    """Run free-form Lua in the engine (opt-in, audited)."""
    _gate(sid, "lua", code)
    return call(sid, "lua", {"code": code})


def post_event(sid: str, name: str) -> dict:
    """Post a raw game event by name (opt-in, audited)."""
    _gate(sid, "post_event", name)
    return call(sid, "post_event", {"name": name})


def answer_popup(sid: str, choice: int = 0) -> dict:
    """Answer the open popup with the given button index."""
    return call(sid, "answer_popup", {"choice": choice})
```
Register CLI verbs `state` (`id`, `--no-ships`, `--no-goals`, `--no-events`, `--limit`), `sexp` (`id expr`, `--mode`), `lua` (`id code`), `post_event` (`id name`), `answer_popup` (`id`, `--choice`).

- [ ] **Step 6: Run** unit then engine tests — Expected: PASS.
- [ ] **Step 7: Red-proof** — remove the Python-side `_gate` check in `lua`; `test_lua_refused_without_opt_in` must fail; restore.
- [ ] **Step 8: Commit** `git add -A && git commit -m "Game state, sexps, popups, opt-in Lua and events"`

---

### Task 12: Piloting, autopilot and time control

**Files:**
- Modify: `fsoharness.lua`, `fsoctl/game.py`
- Create: `tests/test_control_engine.py`

**Interfaces:**
- Consumes: Task 11 `call`, probe results for ControlInfo behaviour.
- Produces:
  - Bridge handler `pilot` `{pitch, heading, bank, forward, primary, secondary, cm, seconds}` (each axis -1..1, fire counts 0..n, seconds 0.05..60 mission-time) → deferred reply `{"applied_frames": n, "mission_time": t}` when the duration ends (ControlInfo reset to 0 after).
  - `autopilot` `{on: bool}` → runs `( player-use-ai )` / `( player-not-use-ai )`.
  - `time` `{compression: number|null, pause: bool, resume: bool}` → `{"compression": pct|null, "state": name}`.
  - `game.pilot(sid, *, pitch=0, heading=0, bank=0, forward=0, primary=0, secondary=0, cm=0, seconds=1.0) -> dict` (timeout = seconds × 4 + 10, since compression may be < 1x); `game.autopilot(sid, on: bool)`; `game.set_time(sid, compression=None, pause=False, resume=False)`.
  - CLI verbs `pilot`, `autopilot`, `time`.

- [ ] **Step 1: Write the failing engine tests**

```python
import math
import os

import pytest

from fsoctl import game, launcher
from fsoctl.errors import EXIT_ENGINE, HarnessError
from fsoctl.launcher import LaunchRequest

pytestmark = pytest.mark.engine


@pytest.fixture
def mission(engine_env, tmp_runtime):
    _, fso = engine_env
    r = launcher.launch(LaunchRequest(binary=str(fso), mods=[os.environ.get("FSO_TEST_MODS", "MVPS-4.7.3")],
                                      pilot_mode="create", skip_movies=True, dismiss_popups=True,
                                      mission=os.environ.get("FSO_TEST_MISSION", "SM1-01.fs2"),
                                      skip_command_briefing=True, skip_briefing=True))
    game.wait_for(r["id"], "state", "GS_STATE_GAME_PLAY", timeout=180)
    yield r["id"]
    launcher.stop(r["id"])


def _heading(st):
    return st["player"]["pos"]


def test_pilot_pitch_changes_orientation(mission):
    before = game.sexp(mission, "( get-object-pitch !" + game.state(mission)["player"]["name"] + "! )", mode="number")
    game.pilot(mission, pitch=1.0, seconds=2.0)
    after = game.sexp(mission, "( get-object-pitch !" + game.state(mission)["player"]["name"] + "! )", mode="number")
    assert abs(after["value"] - before["value"]) > 5


def test_time_compression_speeds_mission_clock(mission):
    import time as wall
    game.set_time(mission, compression=400)
    t0, w0 = game.state(mission, ships=False, goals=False, events=False)["mission_time"], wall.monotonic()
    wall.sleep(4)
    t1, w1 = game.state(mission, ships=False, goals=False, events=False)["mission_time"], wall.monotonic()
    assert (t1 - t0) / (w1 - w0) > 2.0
    game.set_time(mission, compression=100)


def test_pause_and_resume(mission):
    game.set_time(mission, pause=True)
    assert game.state(mission, ships=False, goals=False, events=False)["paused"]
    game.set_time(mission, resume=True)
    assert not game.state(mission, ships=False, goals=False, events=False)["paused"]


def test_autopilot_toggle(mission):
    assert game.autopilot(mission, True)["on"] is True
    assert game.autopilot(mission, False)["on"] is False


def test_call_after_engine_killed_reports_engine(mission):
    import signal
    from fsoctl import session
    os.kill(session.load(mission).engine_pid, signal.SIGKILL)
    with pytest.raises(HarnessError) as e:
        game.state(mission)
    assert e.value.code == EXIT_ENGINE  # Review Focus 2
```
(`get-object-pitch` exists in the sexp list? Confirm in `docs/research/sexps.html`; if not, compare `Orientation` from a `lua` call in an allow-eval session instead and note it.)

- [ ] **Step 2: Run** — Expected: FAIL.

- [ ] **Step 3: Add bridge handlers** (append before `return H`; use the probe result for where ControlInfo writes stick — default: every frame from a timer)

```lua
local function clamp(v, lo, hi)
	if v == nil then return 0 end
	if v < lo then return lo end
	if v > hi then return hi end
	return v
end

H.handlers.pilot = function(args, seq)
	if not mn.isInMission() then
		error({ msg = "not in a mission (state " .. H.state_name() .. ")", hint = "wait_for state GS_STATE_GAME_PLAY" })
	end
	local secs = clamp(args.seconds or 1, 0.05, 60)
	local stop_at = mn.getMissionTime() + secs
	local frames = 0
	H.timers[#H.timers + 1] = { tick = function()
		local ci = ba.getControlInfo()
		if mn.getMissionTime() >= stop_at or not mn.isInMission() then
			ci.Pitch, ci.Heading, ci.Bank, ci.Forward = 0, 0, 0, 0
			ci.PrimaryCount, ci.SecondaryCount, ci.CountermeasureCount = 0, 0, 0
			H.reply(seq, true, { applied_frames = frames, mission_time = mn.getMissionTime() })
			return true
		end
		ci.Pitch = clamp(args.pitch, -1, 1)
		ci.Heading = clamp(args.heading, -1, 1)
		ci.Bank = clamp(args.bank, -1, 1)
		ci.Forward = clamp(args.forward, -1, 1)
		ci.PrimaryCount = clamp(args.primary, 0, 10)
		ci.SecondaryCount = clamp(args.secondary, 0, 10)
		ci.CountermeasureCount = clamp(args.cm, 0, 10)
		frames = frames + 1
		return false
	end }
	return H.DEFERRED
end

H.handlers.autopilot = function(args)
	if args.on then mn.runSEXP("( player-use-ai )") else mn.runSEXP("( player-not-use-ai )") end
	return { on = args.on == true }
end

H.handlers.time = function(args)
	if args.pause then ba.postGameEvent(ba.GameEvents["GS_EVENT_PAUSE_GAME"]) end
	if args.resume then ba.postGameEvent(ba.GameEvents["GS_EVENT_PREVIOUS_STATE"]) end
	if args.compression then
		mn.runSEXP("( set-time-compression " .. math.floor(args.compression) .. " )")
	end
	return { compression = args.compression or json.null, state = H.state_name() }
end
```
Note: the paused state stops On Frame? If the probe showed On Frame does not run in `GS_STATE_GAME_PAUSED`, the resume command will never be read; then also register `On Frame` equivalent for the pause screen (the probe names the hook that runs there) or resume through `ui.PauseScreen` API found in the probe. Decide from the Task 1 log, record in the commit body.

- [ ] **Step 4: Add the Python verbs to `game.py`** (`pilot`, `autopilot`, `set_time` calling `call(...)` with the timeouts in Interfaces; validate ranges in Python too and raise EXIT_USAGE before sending) and register CLI verbs.
- [ ] **Step 5: Run** — Expected: PASS.
- [ ] **Step 6: Red-proof** — make the bridge `time` handler ignore `compression`; the time test fails; restore.
- [ ] **Step 7: Commit** `git add -A && git commit -m "Piloting, autopilot, time compression and pause"`

---

### Task 13: wait_for

**Files:**
- Modify: `fsoharness.lua`, `fsoctl/game.py`
- Create: `tests/test_wait_unit.py`, `tests/test_wait_engine.py`

**Interfaces:**
- Produces:
  - Bridge handler `check` `{kind, value}` → `{"met": bool, "detail": any}` for kinds `state` (value = state name), `event_true`/`event_false` (value = event name), `goal` (value = `name=complete|failed|incomplete`), `destroyed`/`departed` (value = ship name; `is-destroyed-delay 0 !name!` / `is-departed-delay 0 !name!`), `mission_time` (value = seconds, met when `>=`).
  - `game.wait_for(sid, kind, value, timeout=60.0, poll=0.25) -> dict` → `{"met": true, "after_sec": float, "detail": ...}`; kind `log` (value = regex) is checked on the Python side against `logs.read(...)` lines appended since the call started; timeout → EXIT_TIMEOUT naming the last detail; engine death → EXIT_ENGINE.
  - CLI verb `wait_for` (`id kind value`, `--timeout`).

- [ ] **Step 1: Unit tests** (FakeBridge extended: `check` returns met after N polls; `log` kind with a fake log file growing)

```python
import threading
import time

import pytest

from fsoctl import game, logs, session
from fsoctl.errors import EXIT_TIMEOUT, EXIT_USAGE, HarnessError


def test_bad_kind():
    with pytest.raises(HarnessError) as e:
        game.check_wait_kind("sometime")
    assert e.value.code == EXIT_USAGE


def test_log_wait_sees_new_line(tmp_runtime, monkeypatch):
    s = session.new_session(kind="game", binary="/bin/true", fs2path="/tmp", mods=[], mission=None,
                            renderer="opengl", width=1, height=1, switches={}, allow_eval=False,
                            extra_args=[])
    monkeypatch.setattr(game.launcher, "require_running", lambda sid: s)
    monkeypatch.setattr(game.launcher, "alive", lambda sess: True)
    log = logs.sources(s)["engine"]
    log.write_text("Mission loaded: old\n")

    def later():
        time.sleep(0.5)
        with open(log, "a") as f:
            f.write("Mission loaded: sm1-01\n")
    threading.Thread(target=later).start()
    r = game.wait_for(s.id, "log", r"Mission loaded: sm1", timeout=5)
    assert r["met"] and r["after_sec"] >= 0.4


def test_log_wait_ignores_lines_before_the_call(tmp_runtime, monkeypatch):
    s = session.new_session(kind="game", binary="/bin/true", fs2path="/tmp", mods=[], mission=None,
                            renderer="opengl", width=1, height=1, switches={}, allow_eval=False,
                            extra_args=[])
    monkeypatch.setattr(game.launcher, "require_running", lambda sid: s)
    monkeypatch.setattr(game.launcher, "alive", lambda sess: True)
    logs.sources(s)["engine"].write_text("Mission loaded: sm1-01\n")
    with pytest.raises(HarnessError) as e:
        game.wait_for(s.id, "log", r"Mission loaded", timeout=0.6)
    assert e.value.code == EXIT_TIMEOUT
```

- [ ] **Step 2: Engine test** `test_wait_engine.py`: in a mission (fixture as Task 12), `wait_for(..., "mission_time", 5, timeout=60)` met; `wait_for(..., "event_true", "<an event the test mission completes early>")` — choose the event from `state()` output of the test mission and hard-code it with a comment naming the mission; `wait_for(..., "state", "GS_STATE_BRIEFING", timeout=2)` → EXIT_TIMEOUT whose message names `GS_STATE_GAME_PLAY`.

- [ ] **Step 3: Implement** the Lua `check` handler (reuse `event_status`, `GOAL`, `quoted` from Task 11) and `game.wait_for`/`check_wait_kind` in Python:

```lua
H.handlers.check = function(args)
	local k, v = args.kind, args.value
	if k == "state" then return { met = H.state_name() == v, detail = H.state_name() } end
	if k == "mission_time" then return { met = mn.getMissionTime() >= v, detail = mn.getMissionTime() } end
	if not mn.isInMission() then return { met = false, detail = "not in mission: " .. H.state_name() } end
	if k == "event_true" then return { met = event_status(v) == "true", detail = event_status(v) } end
	if k == "event_false" then return { met = event_status(v) == "false", detail = event_status(v) } end
	if k == "destroyed" then return { met = mn.evaluateSEXP("( is-destroyed-delay 0 " .. quoted(v) .. " )"), detail = v } end
	if k == "departed" then return { met = mn.evaluateSEXP("( is-departed-delay 0 " .. quoted(v) .. " )"), detail = v } end
	if k == "goal" then
		local name, want = string.match(v, "^(.-)=(%a+)$")
		for i = 1, #mn.Goals do
			local g = mn.Goals[i]
			if g.Name == name then
				local st = GOAL[g.isGoalSatisfied] or "incomplete"
				return { met = st == want, detail = st }
			end
		end
		error({ msg = "no goal named " .. tostring(name), hint = "list goals with fsoctl state" })
	end
	error({ msg = "unknown wait kind " .. tostring(k), hint = "state, mission_time, event_true, event_false, destroyed, departed, goal" })
end
```

```python
_WAIT_KINDS = ("state", "mission_time", "event_true", "event_false", "destroyed", "departed", "goal", "log")


def check_wait_kind(kind: str) -> str:
    """Return a valid wait kind or raise EXIT_USAGE."""
    if kind not in _WAIT_KINDS:
        raise HarnessError(EXIT_USAGE, f"bad wait kind {kind!r}", f"use one of {', '.join(_WAIT_KINDS)}")
    return kind


def wait_for(sid: str, kind: str, value, timeout: float = 60.0, poll: float = 0.25) -> dict:
    """Block until a condition holds (bounded); return {met, after_sec, detail}."""
    check_wait_kind(kind)
    sess = launcher.require_running(sid)
    start = time.monotonic()
    log_start = _log_len(sess) if kind == "log" else 0
    detail = None
    while time.monotonic() - start < timeout:
        if kind == "log":
            met, detail = _log_met(sess, value, log_start)
        else:
            r = call(sid, "check", {"kind": kind, "value": value}, timeout=10)
            met, detail = r["met"], r.get("detail")
        if met:
            return {"met": True, "after_sec": round(time.monotonic() - start, 2), "detail": detail}
        if not launcher.alive(sess):
            raise HarnessError(EXIT_ENGINE, "engine exited while waiting", f"run `fsoctl crash_report {sid}`")
        time.sleep(poll)
    raise HarnessError(EXIT_TIMEOUT, f"{kind} {value!r} not met in {timeout:.0f}s (last: {detail})",
                       "take a screenshot or check `fsoctl state`")
```
(`_log_len` returns the current line count of the auto-chosen log; `_log_met` reads lines after that index and searches the compiled regex; both ≤ 15 lines; invalid regex → EXIT_USAGE before the loop.)

- [ ] **Step 4: Run** unit + engine — Expected: PASS.
- [ ] **Step 5: Red-proof** — make `_log_met` scan from line 0; `test_log_wait_ignores_lines_before_the_call` fails; restore.
- [ ] **Step 6: Commit** `git add -A && git commit -m "wait_for with bridge-side conditions and log matching"`

---

### Task 14: Record and replay

**Files:**
- Modify: `fsoctl/record.py` (replace the Task 9 stub), `fsoctl/channel.py` (journal every `send` except `ping`, `basic_state`, `check`, `state`)
- Create: `tests/test_record.py`

**Interfaces:**
- Produces:
  - `record.append(sess, kind: str, args: dict) -> None` — one JSON line `{"t": seconds since session start, "kind": kind, "args": args}` to `<run>/journal.jsonl` (kinds: bridge command names and `keys`/`click`/`drag`/`scroll`).
  - `record.load(sid) -> list[dict]`.
  - `record.replay(source_sid: str, speed: float = 1.0, launch_overrides: dict | None = None) -> dict` — launches a new session with the source session's launch fields (binary, mods, mission, renderer, size, switches, extra_args incl. any `-seed`), waits for the same first state the source journal's first entry was issued in (recorded as `{"kind": "_state", ...}` marker appended by `launch`), then re-issues entries with the recorded gaps divided by `speed`; returns `{"id": new_id, "replayed": n, "note": "same commands, not guaranteed same results (no fixed timestep; OS randomness)"}`.
  - CLI verbs `journal` (`id`) and `replay` (`source_id`, `--speed`, `--binary` override, `--renderer` override).

- [ ] **Step 1: Unit tests**

```python
import json

from fsoctl import record, session


def _s():
    return session.new_session(kind="game", binary="/bin/true", fs2path="/tmp", mods=[], mission=None,
                               renderer="opengl", width=1, height=1, switches={}, allow_eval=False,
                               extra_args=["-seed", "7"])


def test_append_and_load_preserve_order_and_relative_time(tmp_runtime):
    s = _s()
    record.append(s, "keys", {"keys": ["Return"]})
    record.append(s, "sexp", {"expr": "( true )"})
    entries = record.load(s.id)
    assert [e["kind"] for e in entries] == ["keys", "sexp"]
    assert entries[0]["t"] <= entries[1]["t"]


def test_replay_plan_scales_gaps(tmp_runtime):
    entries = [{"t": 1.0, "kind": "keys", "args": {}}, {"t": 3.0, "kind": "sexp", "args": {}}]
    assert record.gaps(entries, speed=2.0) == [0.0, 1.0]


def test_replay_note_is_honest():
    assert "not guaranteed" in record.REPLAY_NOTE
```

- [ ] **Step 2: Engine test** (`tests/test_record_engine.py`, marked engine): launch with `-seed 7`, run `sexp ( true )` and `keys Return`, call `replay(...)`, assert the new session's journal has the same kinds in the same order and the replay result carries the note.
- [ ] **Step 3: Implement** `record.py` (append/load/gaps/replay; replay dispatches `keys`/`click`/`drag`/`scroll` to `xinput` and everything else to `game.call` with the original command name and args), and add the journal call in `Channel.send` (skip list above). Keep `record` free of imports from `game`/`xinput` at module level (import inside `replay`) to avoid the import cycle.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Red-proof** — drop the speed division in `gaps`; the scaling test fails; restore.
- [ ] **Step 6: Commit** `git add -A && git commit -m "Command journal and replay (same commands, honest about results)"`


---

### Task 15: Build and scratch mods

**Files:**
- Create: `fsoctl/build.py`, `fsoctl/scratch.py`, `tests/test_build.py`, `tests/test_scratch.py`, `tests/data/gcc_errors.txt`
- Modify: `fsoctl/cli.py` (tuple gains `"build"`, `"scratch"`)

**Interfaces:**
- Produces:
  - `build.compiler_errors(text: str, limit: int = 50) -> list[dict]` → `{"file", "line", "col", "kind": "error"|"warning", "message"}` for GCC/Clang `file:line:col: error|warning: msg` lines (warnings included because FSO CI builds with warnings as errors).
  - `build.build(worktree: str, build_dir: str = "build", targets: list[str] | None = None, jobs: int = 14, timeout: float = 3600) -> dict` → `{"ok": bool, "returncode": int, "errors": [...], "log": str}`; runs the CMake build tool inside a scope `fso-harness-build-<8 hex of worktree path>` with the same caps policy (`systemd-run --user --scope --wait`), output to `<runtime>/builds/<that id>.log`. Refuses (EXIT_USAGE) when `<worktree>/<build_dir>/CMakeCache.txt` is missing ("configure first").
  - `scratch.PREFIX = "fso_harness_scratch_"`; `scratch.create(label: str | None = None) -> dict` → `{"name", "path", "dirs": {missions, tables, scripts, models, maps, effects}}` under `FS2PATH`, with a `.harness-scratch` stamp; `scratch.list_all() -> dict`; `scratch.remove(name: str) -> dict` (refuses unless the name has the prefix AND the stamp exists).
  - CLI verbs `build`, `scratch_mod` (`create [--label]`, `list`, `remove NAME`).

- [ ] **Step 1: Fixture** `tests/data/gcc_errors.txt`:
```
/w/code/ai/aicode.cpp:120:5: error: 'foo' was not declared in this scope
/w/code/ai/aicode.cpp:121:9: warning: unused variable 'x' [-Wunused-variable]
In file included from /w/code/ai/ai.h:3:
/w/code/ship/ship.h:44:1: error: expected ';' after class definition
ninja: build stopped: subcommand failed.
```

- [ ] **Step 2: Failing tests**

```python
from pathlib import Path

import pytest

from fsoctl import build, scratch
from fsoctl.errors import EXIT_REFUSED, EXIT_USAGE, HarnessError

GCC = (Path(__file__).parent / "data" / "gcc_errors.txt").read_text()


def test_compiler_errors_parsed():
    errs = build.compiler_errors(GCC)
    assert [(e["file"], e["line"], e["kind"]) for e in errs] == [
        ("/w/code/ai/aicode.cpp", 120, "error"),
        ("/w/code/ai/aicode.cpp", 121, "warning"),
        ("/w/code/ship/ship.h", 44, "error"),
    ]


def test_build_refuses_unconfigured(tmp_path, tmp_runtime):
    with pytest.raises(HarnessError) as e:
        build.build(str(tmp_path))
    assert e.value.code == EXIT_USAGE and "configure" in e.value.hint


def test_scratch_create_list_remove(tmp_path, monkeypatch):
    monkeypatch.setenv("FS2PATH", str(tmp_path))
    r = scratch.create("glow test")
    p = Path(r["path"])
    assert p.name.startswith(scratch.PREFIX) and (p / "data" / "missions").is_dir()
    assert [m["name"] for m in scratch.list_all()["mods"]] == [r["name"]]
    scratch.remove(r["name"])
    assert not p.exists()


def test_scratch_remove_refuses_foreign_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("FS2PATH", str(tmp_path))
    (tmp_path / "MVPS-4.7.3").mkdir()
    (tmp_path / f"{scratch.PREFIX}fake").mkdir()  # prefix but no stamp
    for name in ("MVPS-4.7.3", f"{scratch.PREFIX}fake", "../etc"):
        with pytest.raises(HarnessError) as e:
            scratch.remove(name)
        assert e.value.code == EXIT_REFUSED
    assert (tmp_path / "MVPS-4.7.3").exists()


@pytest.mark.engine
def test_build_reports_real_compiler_error(tmp_path, tmp_runtime):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.20)\nproject(t CXX)\nadd_executable(t main.cpp)\n")
    (src / "main.cpp").write_text("int main() { return undeclared; }\n")
    import subprocess
    subprocess.run(["cmake", "-S", str(src), "-B", str(src / "build"), "-G", "Ninja"], check=True,
                   capture_output=True, timeout=120)
    r = build.build(str(src), timeout=300)
    assert r["ok"] is False and r["errors"][0]["file"].endswith("main.cpp")
```

- [ ] **Step 3: Run** — Expected: FAIL.
- [ ] **Step 4: Implement** `build.py` (regex `^(.+?):(\d+):(\d+): (fatal error|error|warning): (.*)$`, `fatal error` → kind `error`; scope argv = `["systemd-run", "--user", "--scope", "--quiet", "--collect", "--wait", "--unit", unit, *props, "cmake", "--build", build_dir, "-j", str(jobs), *(["--target", *targets] if targets else [])]`, `cwd=worktree`, `timeout=timeout` → on `TimeoutExpired` stop the scope and raise EXIT_TIMEOUT) and `scratch.py` (names `PREFIX + secrets.token_hex(3)`; label stored in the stamp file as JSON `{"label":…, "created":…}`; `remove` validates name with `^fso_harness_scratch_[0-9a-f]{6}$` before touching the filesystem).
- [ ] **Step 5: Run** unit + engine — Expected: PASS.
- [ ] **Step 6: Red-proof** — drop the stamp check in `remove`; the foreign-folder test fails for `f"{PREFIX}fake"`; restore.
- [ ] **Step 7: Commit** `git add -A && git commit -m "Build inside a scope with parsed compiler errors; scratch mods"`

---

### Task 16: qtFRED sessions: open, save, camera, selection

**Files:**
- Create: `fsoctl/fred.py`, `tests/test_fred_unit.py`, `tests/test_fred_qtfred.py`, `tests/data/viewer_mission_snippet.fs2`
- Modify: `fsoctl/cli.py` (tuple gains `"fred"`)

**Interfaces:**
- Consumes: `launcher.launch` (kind `qtfred`), `xinput` (keys, drag, screenshot, focus_neutral), `paths`, `session`.
- Produces:
  - `fred.open(mission: str, mods: list[str] | None = None, binary: str | None = None) -> dict` — copies the mission (resolved like Task 7) into `<run>/missions/` and launches qtFRED on the COPY (the user's mission file is never opened for writing); returns `{"id", "mission_copy"}`. Binary default `$QTFRED_BIN`.
  - `fred.save(sid) -> dict` — Ctrl+S, wait (bounded 15 s) for the copy's mtime to change; returns `{"path", "saved": bool, "dialog": bool}` (`dialog` true when a window other than the main one appeared — the error checker, see Task 17).
  - `fred.parse_viewer(text: str) -> dict` → `{"pos": [x,y,z], "orient": [[r],[u],[f]], "fvec": [x,y,z]}` from `+Viewer pos:` and `+Viewer orient:` blocks.
  - `fred.camera(sid) -> dict` = save + parse_viewer.
  - `fred.select(sid, rect: tuple[int,int,int,int]) -> dict` — click empty space, marquee-drag rect, return `{"selection": <status-bar text via OCR>}`.
  - `fred.selection(sid) -> dict` — OCR of the status line only.
  - `fred.ocr(path: str, crop: str | None = None) -> str` — `tesseract <img> - --psm 6` (bounded 30 s).
  - CLI verbs `fred_open`, `fred_save`, `fred_camera`, `fred_select`, `fred_selection`.

- [ ] **Step 1: Fixture** `tests/data/viewer_mission_snippet.fs2` (the real shape, CRLF line endings as qtFRED writes them):
```
$Name: Test
+Viewer pos: -2759.583008, 11636.937500, 12197.359375
+Viewer orient:
	1.000000, 0.000000, -0.000189,
	-0.000113, 0.802291, -0.596933,
	0.000151, -0.596933, -0.802291
```

- [ ] **Step 2: Failing unit test**

```python
from pathlib import Path

import pytest

from fsoctl import fred

SNIPPET = (Path(__file__).parent / "data" / "viewer_mission_snippet.fs2").read_bytes().decode("latin-1")


def test_parse_viewer():
    v = fred.parse_viewer(SNIPPET)
    assert v["pos"] == pytest.approx([-2759.583008, 11636.9375, 12197.359375])
    assert v["fvec"] == pytest.approx([0.000151, -0.596933, -0.802291])
    assert len(v["orient"]) == 3


def test_parse_viewer_missing_block():
    from fsoctl.errors import HarnessError
    with pytest.raises(HarnessError):
        fred.parse_viewer("$Name: nothing here")
```

- [ ] **Step 3: Failing qtFRED test** (`@pytest.mark.qtfred`; needs `QTFRED_BIN`, `FS2PATH`, `FSO_TEST_MISSION`):

```python
import os

import pytest

from fsoctl import fred, launcher

pytestmark = pytest.mark.qtfred


@pytest.fixture
def fred_session(tmp_runtime):
    if not os.environ.get("QTFRED_BIN") or not os.environ.get("FS2PATH"):
        pytest.skip("QTFRED_BIN and FS2PATH required")
    r = fred.open(os.environ.get("FSO_TEST_MISSION", "SM1-01.fs2"),
                  mods=[os.environ.get("FSO_TEST_MODS", "MVPS-4.7.3")])
    yield r
    launcher.stop(r["id"])


def test_camera_round_trip_and_original_untouched(fred_session):
    from fsoctl import paths
    cam = fred.camera(fred_session["id"])
    assert len(cam["fvec"]) == 3
    assert fred_session["mission_copy"].startswith(str(paths.runtime_root()))


def test_select_reports_status_text(fred_session):
    sel = fred.select(fred_session["id"], (520, 145, 740, 290))
    assert "selected" in sel["selection"].lower() or "selection" in sel["selection"].lower()
```
(Rect coordinates assume the 1280x720 default window; the test documents that the marquee lands on the mission's ship cluster for the retail mission used — if `FSO_TEST_MISSION` differs, take a screenshot and adjust the rect in the test, noting why.)

- [ ] **Step 4: Run** — Expected: FAIL.
- [ ] **Step 5: Implement `fred.py`** — `open` builds a LaunchRequest with `kind="qtfred"`, `binary=binary or os.environ["QTFRED_BIN"]`, `mission=<absolute copy path>`; `parse_viewer` with regex over the block (accepts `\r\n`); `save` uses `xinput.keys(sid, ["ctrl+s"])` then polls mtime (0.25 s × 60); detects a dialog by counting visible windows with `xdotool search --onlyvisible --name .` before/after; `select` = `xinput.click` on an empty viewport point, `xinput.drag`, then `selection`; `selection` = screenshot cropped to the status line (`420x30+0+70` at 1280x720 — derive from window size: `f"{min(420, w)}x30+0+70"`) + `ocr`.
- [ ] **Step 6: Run** unit + `bounded-run pytest -m qtfred -p no:xdist tests/test_fred_qtfred.py` — Expected: PASS.
- [ ] **Step 7: Red-proof** — open the original mission instead of the copy; add an assertion in the test that the original file's mtime is unchanged after `camera` (keep this assertion permanently: it pins "never edits the user's mission files"); see it fail; restore.
- [ ] **Step 8: Commit** `git add -A && git commit -m "qtFRED sessions on mission copies: save, camera, selection via OCR"`

---

### Task 17: fred_check (qtFRED error checker via OCR)

**Files:**
- Modify: `fsoctl/fred.py`
- Create: `tests/test_fred_check_unit.py`, `tests/test_fred_check_qtfred.py`, `tests/data/checker_ocr.txt`

**Interfaces:**
- Produces: `fred.check(sid) -> dict` → `{"clean": bool, "entries": [{"severity": "error"|"warning"|"potential"|"unknown", "text": str}], "screenshot": str, "raw_text": str, "note": "read by OCR from the qtFRED error checker dialog; verify against the screenshot"}`. Mechanism: Ctrl+S on the session's mission copy; if a checker dialog appears, screenshot it, OCR, parse, dismiss with Escape, `focus_neutral`; if no dialog appears within 5 s and the file saved, `clean=true`. `fred.parse_checker(text) -> list[dict]`.

- [ ] **Step 1: Fixture** `tests/data/checker_ocr.txt` — the tesseract output of the real dialog captured in Task 1 Step 5 (copy it verbatim from the probe results; it must include the severity legend line and at least one entry).
- [ ] **Step 2: Unit test** — `parse_checker(fixture)` returns ≥1 entry whose text contains the ship name the probe deleted, legend lines are not entries, and severity is inferred from leading words (`Error in`/`Warning`/`Potential`) else `unknown`.
- [ ] **Step 3: qtFRED test** — open a mission copy, break a reference by editing the COPY's text before launch (remove the `$Name:` block of a ship that an event references; choose the ship from the retail mission and note it), call `check`, assert `clean is False` and an entry mentions that ship; second test: unmodified copy → `clean is True`.
- [ ] **Step 4: Run** — Expected: FAIL.
- [ ] **Step 5: Implement** `check` and `parse_checker` (normalise OCR: collapse whitespace, drop lines shorter than 4 chars, drop the legend line `Critical Error Error Warning Potential Issue`).
- [ ] **Step 6: Run** — Expected: PASS. **Step 7: Red-proof** — return `clean=True` unconditionally; the broken-mission test fails; restore.
- [ ] **Step 8: Commit** `git add -A && git commit -m "fred_check: qtFRED error checker read by OCR, with screenshot"`

---

### Task 18: validate and crash_report

**Files:**
- Modify: `fsoharness.lua` (handler `counts` → `{"warnings": ui.PilotSelect.WarningCount, "errors": ui.PilotSelect.ErrorCount}` or the names the probe found), `fsoctl/cli.py` (tuple gains `"validate"`, `"crash"`)
- Create: `fsoctl/validate.py`, `fsoctl/crash.py`, `tests/test_crash_unit.py`, `tests/test_validate_engine.py`, `tests/test_crash_engine.py`

**Interfaces:**
- Produces:
  - `validate.validate(mods: list[str], mission: str | None = None, binary: str | None = None, timeout: float = 240) -> dict` → launch (skip_movies, pilot create, dismiss_popups, noninteractive, mission + skip briefings if given), wait for `GS_STATE_MAIN_MENU` (or `GS_STATE_GAME_PLAY` with a mission), collect `counts`, log problems, `popups.log` entries, then stop; returns `{"warnings", "errors", "problems", "popups", "build": "debug"|"release", "note": str}`; `build` = debug when `<prefs>/data/fs2_open.log` exists; release note: "Warning() is compiled out of release builds; use a debug engine (FSO_BIN_DEBUG) for full validation". If the engine exits during load (an Error() under -noninteractive aborts), return a normal result with `"aborted": true`, the exit info and the log problems: an aborted validation is a successful finding, not a harness failure.
  - `crash.parse_coredumpctl(json_text: str, pid: int) -> dict | None`; `crash.report(sid: str) -> dict` → `{"status", "exit_info", "signal", "core": {...}|null, "backtrace": [frames]|null, "log_tail": [...], "problems": [...], "popups": [...], "last_heartbeat": {...}|null, "hints": [...]}`; core lookup: `coredumpctl --no-pager --json=short list --since=@<created>` filtered by `pid == engine_pid`; backtrace: `coredumpctl dump <pid> -o <run>/core` then `gdb -batch -ex "thread apply all bt 30" <binary> <run>/core` (timeouts 60 s each; missing tools or cores → field null plus a hint).
  - CLI verbs `validate`, `crash_report`.

- [ ] **Step 1: Unit tests** — `parse_coredumpctl` on a captured JSON sample (capture one from `coredumpctl --json=short list` on this machine into `tests/data/coredumpctl.json`, trimming to 2 entries) picks the entry for the given pid; `report` on a session with `exit_info={"returncode": -11, "signal": "SIGSEGV"}` and no core returns `signal == "SIGSEGV"`, `core is None`, and a hint mentioning `coredumpctl`.
- [ ] **Step 2: Engine tests** — validate retail data with `FSO_BIN` (expect `build == "release"` with the note) and, when `FSO_BIN_DEBUG` is set, a scratch mod (Task 15) containing a deliberately broken table entry (e.g. a ships `.tbm` with an unknown `$Name` modification target) → `warnings >= 1` or a problem line mentioning the table; crash test: launch, `os.kill(engine_pid, SIGSEGV)`, `crash.report` → `signal == "SIGSEGV"`, and if `coredumpctl` recorded a core, `backtrace` non-empty (else skip that assertion with the recorded reason).
- [ ] **Step 3: Run** — Expected: FAIL. **Step 4: Implement** both modules (functions ≤ 60 lines; split `report` into `_core_for`, `_backtrace`, `_artifacts`). **Step 5: Run** — Expected: PASS.
- [ ] **Step 6: Red-proof** — make `parse_coredumpctl` ignore the pid filter; the unit test with two entries fails; restore.
- [ ] **Step 7: Commit** `git add -A && git commit -m "validate (load and quit) and crash_report (core + gdb backtrace)"`

---

### Task 19: api_docs, model_info, frame_stats

**Files:**
- Create: `fsoctl/shadowdir.py`, `fsoctl/docs.py`, `fsoctl/model_info.py`, `fsoctl/frame_stats.py`, `tests/test_shadowdir.py`, `tests/test_frame_stats.py`, `tests/test_model_info.py`, `tests/test_docs_engine.py`, `tests/data/profiling_sample.csv`, `tests/data/pofspew_sample.txt`
- Modify: `fsoctl/launcher.py` (`LaunchRequest.cwd: str | None = None`), `fsoctl/session.py` (`Session.cwd: str | None = None`, default None so old session files still load), `fsoctl/runner.py` (use `sess.cwd or sess.fs2path` as the working directory), `fsoctl/cli.py`

**Interfaces:**
- Produces:
  - `shadowdir.make(fs2: Path, dest: Path) -> Path` — a folder whose entries are symlinks to every entry of FS2PATH (so the engine finds its data) while files the engine writes to its working directory land in `dest`, not in the user's game folder (spec §9). `shadowdir.collect(dest, names) -> dict[name, Path]`.
  - `docs.api_docs(binary: str | None = None, mods: list[str] | None = None, refresh: bool = False) -> dict` → `{"dir": str, "files": {"sexps.html", "scripting.lua", "scripting.json"}, "cached": bool}`; cache `~/.cache/fso-harness/docs/<sha256(binary)[:12]>-<mods hash>/`; runs the engine with `-output_sexps -output_script_lua -output_script_json`, cwd = shadow dir, waits for the bridge heartbeat (outputs are written before game init finishes), quits via the bridge `quit` handler.
  - `model_info.parse(text: str) -> list[dict]`; `model_info.model_info(binary=None, mods=None, name_filter: str | None = None) -> dict` (runs `-pofspew`, which exits by itself; output found by globbing `<prefs>/**/pofspew.txt`).
  - `frame_stats.parse(csv_text: str) -> dict` → `{"frames", "mean_ms", "p50_ms", "p95_ms", "p99_ms", "max_ms"}` from `end_ts;duration` lines (duration in the unit the engine writes — confirm from a real file in Step 1 and state the unit in the docstring); `frame_stats.frame_stats(seconds: float = 30, **launch_fields) -> dict` (launch with `-profile_write_file`, cwd = shadow dir, wait `mission_time` or seconds, quit, parse).
  - CLI verbs `api_docs`, `model_info`, `frame_stats`.

- [ ] **Step 1: Probe the shadow directory** — make a shadow dir of FS2PATH by hand, run `run_probe.sh`-style launch with cwd set to it and `-output_sexps`; confirm the engine loads retail data through the symlinks and `sexps.html` appears in the shadow dir and NOT in FS2PATH. Capture a real `profiling.csv` (run 20 s with `-profile_write_file`) and a real `pofspew.txt` into `tests/data/` (trim to a few models / 200 lines). If the engine does not follow symlinks, STOP and send Dustin a `Proposal:` (alternatives: write into FS2PATH and move out immediately, refusing to clobber existing files) — this is a spec §9 change.
- [ ] **Step 2: Unit tests** for `shadowdir.make` (every FS2PATH entry is a symlink; writing a file in the shadow dir does not create it in FS2PATH), `frame_stats.parse` (percentiles on the sample; empty file → EXIT_ENGINE "no frames recorded"), `model_info.parse` (sample → at least one model with a name and per-LOD counts).
- [ ] **Step 3: Engine test** — `api_docs()` twice: first `cached False`, second `cached True`; `scripting.lua` contains `--- @class`; FS2PATH has no `sexps.html` after the run.
- [ ] **Step 4: Run** — FAIL. **Step 5: Implement.** **Step 6: Run** — PASS.
- [ ] **Step 7: Red-proof** — make `shadowdir.make` copy instead of symlink a small file and write to FS2PATH root instead of dest in a deliberately broken variant; the "not in FS2PATH" assertion fails; restore.
- [ ] **Step 8: Commit** `git add -A && git commit -m "api_docs, model_info, frame_stats via a shadow working directory"`

---

### Task 20: VP archives

**Files:**
- Create: `fsoctl/vp.py`, `tests/test_vp.py`
- Modify: `fsoctl/cli.py`

**Interfaces:**
- Produces: `vp.read_index(path: Path) -> list[dict]` → `{"path": "data/tables/ships.tbl", "size": int, "offset": int, "timestamp": int}` (directories folded into paths; `..` pops); `vp.list_files(path: str, pattern: str = "*") -> dict`; `vp.extract(path: str, pattern: str, dest: str) -> dict` (refuses any entry whose resolved path escapes `dest`; caps total extracted bytes at 2 GiB); CLI verb `vp` (`list PATH [--pattern]`, `extract PATH PATTERN DEST`).
- Format (Volition VP): header `"VPVP"`, int32 version (2), int32 directory offset, int32 entry count; each 44-byte entry: int32 offset, int32 size, char[32] name (NUL-padded), int32 timestamp; a directory entry has size 0 and a name; `..` (size 0) closes the current directory. Cross-check against an independent VP extractor before implementing.

- [ ] **Step 1: Unit tests** with a test-built archive (helper `write_vp(path, {"data/tables/a.tbl": b"...", "data/missions/m.fs2": b"..."})` in the test file that writes the format above, nesting directory entries): index lists both files with correct sizes; `extract` with `*.tbl` writes only `a.tbl` with identical bytes; an archive containing an entry named `../../evil` is refused; a truncated archive raises EXIT_USAGE "not a VP archive".
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** (`struct.unpack("<4siii")`, `struct.unpack("<ii32si")`; bounded loop over entry count ≤ 1,000,000). **Step 4: Run** — PASS; also run `fsoctl vp list "$FS2PATH/root_fs2.vp" --pattern '*.tbl'` once and confirm `ships.tbl` is listed (record in commit body).
- [ ] **Step 5: Red-proof** — skip the path-escape check; the evil-entry test fails; restore.
- [ ] **Step 6: Commit** `git add -A && git commit -m "VP archive list and extract (stdlib, path-safe)"`

---

### Task 21: compare and renderer_compare

**Files:**
- Create: `fsoctl/scenario.py`, `fsoctl/compare.py`, `tests/test_scenario.py`, `tests/test_compare_unit.py`, `tests/test_compare_engine.py`
- Modify: `fsoctl/cli.py`

**Interfaces:**
- Consumes: `launcher`, `game`, `xinput`.
- Produces:
  - Scenario format (JSON list of steps): `{"wait_for": {"kind", "value", "timeout"}}`, `{"sexp": {"expr", "mode"}}`, `{"pilot": {...}}`, `{"autopilot": bool}`, `{"time": {...}}`, `{"keys": [...]}`, `{"sleep": seconds}` (≤ 120), `{"snapshot": "label"}`. `scenario.validate(steps) -> list` (EXIT_USAGE naming the bad step index); `scenario.run(sid, steps, out_dir) -> dict[label, {"state": ..., "screenshot": path}]`.
  - `compare.diff_states(a: dict, b: dict, pos_tol: float = 1.0, hull_tol: float = 1.0) -> list[dict]` (ships missing/added, position deltas over tolerance, hull deltas, goal/event status changes, mission state mismatch).
  - `compare.diff_images(a_path, b_path, out_path) -> dict` → `{"mean_abs_diff": float, "max_diff": int, "diff_image": str}` (via `magick compare -metric MAE` with bounded timeout; parse the normalised value).
  - `compare.compare(binary_a, binary_b, launch: dict, steps: list, pos_tol=1.0, hull_tol=1.0) -> dict` → per snapshot `{"state_diffs": [...], "image": {...}}` + `{"identical_within_tolerance": bool}`; sessions run one after another (not concurrently: identical load on the machine).
  - `compare.renderer_compare(binary, launch, steps, ...) -> dict` (same, OpenGL vs Vulkan on one binary; requires the caller's `VK_ICD_FILENAMES`; reports the renderer each side really used).
  - CLI verbs `compare` (`--a BIN --b BIN --launch JSON --steps FILE`) and `renderer_compare`.

- [ ] **Step 1: Unit tests** — `scenario.validate` rejects unknown step keys and `sleep` > 120 with the step index in the message; `diff_states` on two hand-built states: a ship moved 0.5 m (under tolerance, no diff), a ship moved 50 m (diff with the delta), an event `pending`→`true` (diff), a ship missing in b (diff).
- [ ] **Step 2: Engine tests** — `compare(FSO_BIN, FSO_BIN, launch=<full skip into FSO_TEST_MISSION>, steps=[wait_for mission_time 5, snapshot "t5"])` → the run completes, every diff entry is a position within a generous tolerance (document: two runs of the same build diverge slightly because there is no fixed timestep — the test asserts the report shape and that the mission state matches, not zero diffs); `@pytest.mark.vulkan` `renderer_compare` → both sides report their renderer (`opengl`, `vulkan`) and `mean_abs_diff` is below 0.05 for the snapshot (OpenGL vs Vulkan rendered the base mission within 0.1 grey levels on 2026-10-12).
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement.** **Step 5: Run** — PASS.
- [ ] **Step 6: Red-proof** — invert the tolerance comparison in `diff_states`; the 0.5 m case reports a diff; restore.
- [ ] **Step 7: Commit** `git add -A && git commit -m "Scenarios, compare across builds, compare across renderers"`

---

### Task 22: MCP server

**Files:**
- Create: `mcp/server.py`, `mcp/setup.sh`, `tests/test_mcp_wrapper.py`

**Interfaces:**
- Consumes: the `fsoctl` CLI (`python -m fsoctl --json ...`).
- Produces: an MCP server named `fso-harness` exposing one tool per CLI verb with the same names and arguments (`launch`, `status`, `stop`, `list`, `cleanup`, `screenshot` (returns an MCP `Image` plus the JSON), `log`, `state`, `sexp`, `lua`, `post_event`, `answer_popup`, `keys`, `click`, `drag`, `scroll`, `pilot`, `autopilot`, `time`, `wait_for`, `journal`, `replay`, `build`, `scratch_mod`, `fred_open`, `fred_save`, `fred_camera`, `fred_select`, `fred_selection`, `fred_check`, `validate`, `crash_report`, `api_docs`, `model_info`, `frame_stats`, `vp`, `compare`, `renderer_compare`). Stateless: every tool shells out with a per-tool timeout (default 90 s; `launch`/`validate`/`build`/`compare` use their own `timeout` argument + 30 s), returns the CLI's JSON (errors included, never raised).
- `mcp/setup.sh`: creates `mcp/.venv`, installs `mcp`, registers `claude mcp add --scope user fso-harness -- <venv>/bin/python <repo>/mcp/server.py`.

- [ ] **Step 1: Test** (runs only when `mcp/.venv` exists; else skip with the reason) — import `server` with a fake `fsoctl` on PATH (a script echoing its argv as JSON), call three tools (`status`, `keys`, `screenshot` with a 1×1 PNG path), assert the argv mapping and that `screenshot` returns image content; assert an `fsoctl` non-zero exit becomes a JSON error string, not an exception; assert a call that exceeds its timeout returns a timeout error.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** following the earlier harness's MCP server (MCP SDK 2.x `from mcp.server import MCPServer` and `from mcp.server.mcpserver import Image` — verify both imports against the installed package before relying on them; cap inline image size at 16 MiB and 4096×4096). **Step 4: Run** `bash mcp/setup.sh` then the test — PASS.
- [ ] **Step 5: Red-proof** — let `_ctl` raise on non-zero exit; the error-string test fails; restore.
- [ ] **Step 6: Commit** `git add -A && git commit -m "Stateless MCP wrapper over fsoctl"`

---

### Task 23: Documentation, full QA, showcase and upstream preparation

**Files:**
- Create: `README.md`, `CLAUDE.md`, `docs/AGENT-GUIDE.md`, `docs/superpowers/plans/…` status updates
- Modify: spec Status block

- [ ] **Step 1: README.md** — what it is (one paragraph), requirements (Linux, Xvfb, xdotool, ImageMagick, tesseract, gdb/coredumpctl, systemd user session, Python 3.12, an FSO build; Vulkan needs a GPU driver and `-DFSO_BUILD_WITH_VULKAN=ON` + libshaderc-devel), install (`uv tool install .` or `pip install -e .`; MCP: `bash mcp/setup.sh`), quick start (launch with full skip, state, screenshot, stop), every verb with one example, limits (no exact replay; release builds hide warnings; qtFRED checker via OCR; Linux only).
- [ ] **Step 2: docs/AGENT-GUIDE.md** — task recipes for agents: "test a code change" (build → launch → wait_for → state/screenshot → stop), "compare my branch with master", "check a Vulkan bug", "write and validate a mission in a scratch mod", "fly a mission" (read the mission's events, autopilot vs pilot, time compression), "read a crash". Each recipe ≤ 10 commands.
- [ ] **Step 3: CLAUDE.md** — project context (purpose quotes, architecture, load-bearing decisions: no daemon, files channel, cf API only, mission copies for qtFRED, shadow dir, no vendor defaults, opt-in eval), build/test commands and tiers, etiquette (engine tests via bounded-run; never on the user's desktop).
- [ ] **Step 4: Full test sweep** — `pytest -q`, `bounded-run pytest -m engine -p no:xdist -q`, `bounded-run pytest -m qtfred -p no:xdist -q`, `bounded-run pytest -m vulkan -p no:xdist -q`, `ruff check .` — all green; paste the summary lines into the plan Status block.
- [ ] **Step 5: QA** — HIGH-RISK tier per the workspace rules (long-lived processes, kill/cleanup behaviour, desktop isolation): state-lifetime-auditor (premise challenger), code-reviewer, test-analyzer, security-auditor (opt-in eval, path handling in vp/scratch, audit log), silent-failure-hunter; Phase A/B/C with the manifest check; mutation pass with mutmut scoped to `fsoctl/` (tests import the package in-process); cross-family pass (an external-model seat, a second external-model seat); receipt line. Fix wave as new commits.
- [ ] **Step 6: Showcase and upstream preparation (stop for Dustin)** — prepare, do not publish: (a) `gh repo create` command for a PUBLIC repo under dustinspace217 named `fso-harness` (Dustin runs or approves), (b) the upstream issue text for scp-fs2open asking whether they want the tool and in what shape (draft in chat for Dustin; posting needs his yes), (c) a one-paragraph showcase description.
- [ ] **Step 7: Commit** `git add -A && git commit -m "Docs, agent guide, QA record"`

---

## Appendix: execution deviations (Tasks 1-22)

Every departure from the plan as written, from the execution ledger, classified. Engine facts
behind them are in docs/research/2026-10-12-probe-results.md.

- **Task 1** [behavioral-change (engine fact)] API docs generated in a shadow game folder (symlinks to FS2PATH dirs + .vp), not in FS2PATH — FS2PATH already held a sexps.html of unprovable origin that the plan's rm -f would delete — cost if wrong: none, outputs identical.
- **Task 1** [behavioral-change (engine fact)] run_probe.sh puts the probe mod in the shadow folder and uses Xvfb -displayfd instead of copying into FS2PATH and a fixed :91 — keeps the spec's "never write the user's game folder" rule — cost if wrong: none.
- **Task 1** [other] probe adds Submit+Override popup answering, LUA_FULL control mode, firePrimary, frame-time ratio for compression, whole-file log writes — each forced by a probe finding (engine source cited in probe-results) — cost if wrong: none, probe-only.
- **Task 1** [deferment] mn.startMission inverted-check probe skipped (UNTESTED) — design never calls mn.startMission — cost if wrong: one probe run.
- **Task 1** [behavioral-change (security fix)] background security review flagged insecure temp dir in probe scripts — fixed (mktemp -d under $TMPDIR, 10 random chars, verified 0700) in a follow-up commit — cost if wrong: none.
- **Task 2** [other] added [build-system] and testpaths=tests to the plan's tooling, and tests/__init__.py (later tasks import tests.fake_bridge) — cost if wrong: none. Plan's 'PASS (5 tests)' is 6 (1 errors + 5 paths).
- **Task 3** [other] plan test used out.extend(generator) on a Manager list, which cannot be pickled — changed to a list; module docstring reworded to the spec's an earlier MCP server wording — cost if wrong: none.
- **Task 4** [other] added test_classify_real_captures (real maps from Vulkan and OpenGL runs); module comments corrected — 'FSO silently falls back to OpenGL' had no source support (no fallback code found) and kdialog was never observed — cost if wrong: none.
- **Task 6** [behavioral-change (engine fact)] bridge polls with cf.listFiles (gap-tolerant) instead of the spec's "next expected number" — the plan's channel withdraws a timed-out command file, which would stall a next-number reader forever — cost if wrong: none, ordering kept by numeric sort.
- **Task 6** [behavioral-change (engine fact)] launch-time switches file is switches.txt, not switches.json — OpenGL startup purges *.json/*.bin from mod cache folders (verified on the real engine, source cited in probe results §14) — Task 7 _prepare must write switches.txt — cost if wrong: switches silently ignored.
- **Task 6** [behavioral-change (engine fact)] dropped the plan's On State Start hook; state hooks fire from per-frame state polling (probe-proven); added +Override on On Dialog Frame (probe finding) and a bridge_info handler — cost if wrong: none.
- **Task 6** [other] test concurrency uses multiprocessing spawn context — Python 3.14 default forkserver keeps the env of the first test that started it — cost if wrong: none (test-only).
- **Task 7** [behavioral-change (engine fact)] _prepare writes switches.txt (Task 6 ruling) and tests that no startup file in the channel is *.json/*.bin — cost if wrong: none.
- **Task 7** [behavioral-change (engine fact)] qtFRED argv always gets -noninteractive and its window is found by pid (probe §10), not by the title "FRED" — cost if wrong: none.
- **Task 7** [behavioral-change (engine fact)] scope.start now waits (bounded 10 s) for the unit to be active and pre-checks the executable; _alive_or_starting treats a starting session as alive only while its scope is active — found by the engine test (race), fixed TDD — cost if wrong: up to 10 s extra on a broken systemd.
- **Task 7** [other] runner gets PYTHONPATH pointing at this package so `python -m fsoctl.runner` works from a checkout; added launcher.bridge_info(); engine test uses the pilot chooser (pilot create arrives in Task 8) instead of the plan's pilot_source workaround — cost if wrong: none.
- **Task 8** [behavioral-change (engine fact)] stage switches fire from per-frame state polling on normal frames only (not On State Start, not under a popup); popup log is popups.txt rewritten whole (append unsafe in debug builds); dismiss uses button 0 (the first button) where the spec says "default choice" — FSO popups have no separate default index exposed to Lua — cost if wrong: a popup whose first button is destructive would be chosen; popups are logged so it is visible.
- **Task 8** [other] popup-visible test uses the pilot-select settings popup (what a fresh prefs folder actually shows) rather than the plan's main-menu wait; ready-timeout test uses 0.01 s because the bridge is ready in ~1 s (plan's 1.0 s would be flaky) — cost if wrong: none.
- **Task 8** [deferment] red-alert skip implemented from source (GS_EVENT_ENTER_GAME, redalert.cpp:242) but UNTESTED — no red-alert mission in the test set — cost if wrong: red-alert missions stop at the red alert screen with skip_briefing on.
- **Task 9** [behavioral-change (engine fact)] window_id finds the window by engine pid for both kinds (probe §10); drag(sid, start, end) takes (x, y) pairs instead of 4 ints (PLR0913); engine test adds crop + keys/click/scroll smoke and uses getextrema (getdata deprecated) — cost if wrong: none.
- **Task 10** [behavioral-change (engine fact)] problem patterns and the fixture use the engine's real formats (source + debug runs) instead of the plan's guessed ones (ASSERTION, Warning:), and a noise-exclusion test was added; red-proof is a loosened warning pattern — cost if wrong: none.
- **Task 11** [behavioral-change (engine fact)] bridge refusals use H.raise (assert-based) instead of the plan's error({msg,hint}) — FSO's forwarders.lua makes error() a fatal ba.error (verified; probe results §15) — every later bridge task must use H.raise, never error() — cost if wrong: an engine abort on the first refusal.
- **Task 11** [behavioral-change (engine fact)] harness does not pass -luadev by default (would silence a mod's own Lua errors under test); bridge avoids engine-raised errors by reading only fields that exist (target via getBreedName check) — cost if wrong: a bridge field mistake aborts the session; tests exercise every field read.
- **Task 11** [other] engine tests use a shared heartbeat-wait helper (tests/engine_helpers.py) instead of Task 13's wait_for, so Task 11 does not depend on Task 13; state reports fvec, shield_pct, orders count and a ship-only target (spec's orientation/shields/orders/target) — cost if wrong: none.
- **Task 11** [behavioral-change (security fix)] background security review (authorization bypass in game.py) — sexp could run Lua via script-eval*/multi-eval; fixed in both layers with tests (commit after ac09ce5); also refuse sexps outside a mission (they evaluate false there) — cost if wrong: none.
- **Task 12** [behavioral-change (engine fact)] pilot uses FLIGHT_CONTROL_LUA_FULL + ship fire methods (probe §6) instead of the plan's ControlInfo counts; fire flags are booleans (fire every frame) not counts; pilot(sid, **controls) builds a Flight dataclass (PLR0913 limit); compression range 10..6400%, 100% = reset-time-compression; added control_mode handler; a second concurrent pilot is refused — cost if wrong: none.
- **Task 12** [behavioral-change (security fix)] second background security review (parser differential in the sexp eval gate) — replaced the operator regex/pattern with a contains-"eval" check in both layers (sound: exact operator match, no aliases) — cost if wrong: harmless false positives need --allow-eval.
- **Task 13** [behavioral-change (engine fact)] departed uses has-departed-delay (probe §11); engine test waits on 'Addressing Transports' (observed true at 5-20 s) instead of an unspecified early event; waits capped at 3600 s; CLI mission_time parse errors are EXIT_USAGE — cost if wrong: none.
- **Task 14** [other] journal entries also carry the game state at issue and replay waits for it (instead of the plan's single _state marker); replay routes lua/post_event/sexp through game.* so the eval gate and audit apply; journal bounded at 100k entries — cost if wrong: none.
- **Task 15** [other] build runs systemd-run --scope synchronously (no --wait, which is for services), refuses a concurrent build of the same worktree, defaults to 14 jobs (workspace core cap) with CPUQuota = jobs; scratch remove also rejects a name with path parts — cost if wrong: none.
- **Task 16/20** [scope/order] Task 20 (VP archives) executed before Task 16 — fred.open must extract retail missions from root_fs2.vp, which needs vp.py — cost if wrong: none (order only).
- **Task 20** [other] added vp.read_file (case-insensitive single-file read) for qtFRED mission copies; all entries are path-checked before any write — cost if wrong: none.
- **Task 20** [behavioral-change (security fix)] third background security review (resource-cap bypass in vp.py, signed sizes) — fixed with bounds checks against the file size and a read_file cap, tests first — cost if wrong: none.
- **Task 16** [behavioral-change (engine fact)] fred.open reads retail missions out of .vp archives (vp.read_file) and stages the copy under the runtime root (the plan resolved loose files only); readiness waits for the editor window by title (new xwin module) because the first window is the splash; all xinput pointer moves became window-relative because windows are not at 0,0 — this changed Task 9's xinput (focus_neutral now takes window coordinates); select test drags across most of the 3D view instead of a pixel-tuned rectangle — cost if wrong: none.
- **Task 17** [other] dialog captured with import -window <dialog id> -resize 200% (probe: dialog-only 2x OCR reads cleanly); entries carry bad_node; severity from the leading word (colour cross-check not implemented); dialog closed with Escape and verified closed — cost if wrong: OCR misreads names; result carries the screenshot and a verify note.
- **Task 18** [behavioral-change (engine fact)] backtrace via `coredumpctl debug <pid> --debugger-arguments=...` (core read in place, probe §9) instead of the plan's dump-to-run-folder; HarnessError gained an optional `details` dict and launch() attaches the failed session id (validate needs it to report an abort); log warning pattern now keys on `WARNING: "` and joins multi-line warnings (real output) — this changes Task 10's matcher; validate prefers FSO_BIN_DEBUG — cost if wrong: none.
- **Task 19** [behavioral-change (engine fact)] added launcher.run_to_exit (pofspew exits before any heartbeat) and launcher.quit (clean exit; killed engines leave profiling.csv empty); shadowdir also links the bridge folder ahead of install; frame_stats skips the first 5 frames (loading) and requires a mission; parsers built against real captures (pofspew.txt, profiling.csv in ns) instead of the plan's guessed formats — cost if wrong: none.
- **Task 21** [other] tolerances passed as one tol dict (PLR0913); compare/renderer_compare apply the same stage-skip defaults to both runs; engine tests use huge position/hull tolerances (drift) and assert ship list/goals/events/state equality instead of the plan's mean_abs_diff < 0.05 claim — measured MAE 0.0021 — cost if wrong: none.
- **Task 22** [scope/order] setup.sh only builds the venv; the user-scope `claude mcp add` moved to register.sh and was NOT run — it changes the user's global Claude Code config (needs explicit approval); one-line command goes in the final report — cost if wrong: one command for Dustin.
- **Task 22** [scope/order] mcp pinned to 2.0.0 (the version working in an earlier harness on this Python); per-file ruff ignore of PLR0913/PLR0917 for mcp/server.py (logged) — cost if wrong: none.

---

## Appendix: Deferments originated in Task 23 (HIGH-RISK QA, 2026-10-12)

QA record: kept outside this repository. Decided at commit bf9fa76 (build/v1). Defer target for all: "v1.1 hardening" (no plan doc yet).

| ID | Found by | Severity | What was deferred | Why deferred | Fix direction | Detection / revisit | Obsolete when |
|---|---|---|---|---|---|---|---|
| DEF-QA-01 | SA-6, CR-16 (converged), SL-12 | MEDIUM | engine.log / fs2_open.log size and screenshots per session are uncapped on the runtime tmpfs (shared with desktop sockets) | side folders, problem lists and cleanup were fixed; log rotation inside the runner is a larger change than the QA fix pass | cap or rotate the two logs in the runner; cap screenshots per session; or move bulky files to ~/.cache | `du -sh $XDG_RUNTIME_DIR/fso-harness` after a long agent loop; revisit if it ever passes 500 MB | the runtime root moves off tmpfs |
| DEF-QA-02 | SA-7, CR-6 (converged) | MEDIUM | no total memory limit across sessions and builds; each scope gets 60% of MemAvailable measured at its own launch | the cap of 3 sessions now counts starting and stopping sessions; a shared systemd slice with MemoryMax is a design change | put all harness scopes under one `fso-harness.slice` with a MemoryMax | `systemd-cgtop` during 3 sessions + a build; revisit on any OOM kill recorded as ended_by "oom" | a slice is added |
| DEF-QA-03 | CR-9 residual (stabilization) | LOW | a mistyped path (e.g. `vp list /nope`) exits 6 (engine) instead of 2 (usage) | the message is clear and structured; the code is the only wrong part | map FileNotFoundError on caller-supplied paths to EXIT_USAGE at the verb boundary | unit test on `vp list` with a missing file | — |
| DEF-QA-04 | TA-7 residual | LOW | no engine test crashes the engine through a test mod (spec §10 "deliberate crash") | crash_report's backtrace and core polling are unit-tested; the existing engine test kills the engine from outside | add a scratch-mod table or Lua that calls ba.error / an Error() path and assert crash_report's popup text and backtrace | revisit when crash_report changes | — |
| DEF-QA-05 | MU-4, MU-6, MU-8, MU2-5 | LOW | surviving mutants: argparse configuration (~339), message/hint text, timing constants, and the remaining logic survivors listed in MU2-mutation.md | safety-relevant survivors were read by hand and the real gaps killed; the rest change wording, help text or bounds already pinned by engine tests | per-verb parse-table tests; message/hint asserts across all modules | next mutation run (`mutmut run`, also_copy docs/research) | — |
| DEF-QA-06 | SA2-5 | LOW | the persistent audit log has no size cap (a lua call stores the full code, up to the 128 KiB argv limit, twice) | single-user tool; the log is the opt-in's only record and must not drop entries silently | rotate at a size with a marker line, or store a hash plus the first N bytes | `du -h ~/.local/state/fso-harness/audit.log`; revisit past 50 MB | — |

## Post-v1: SM1-01 demo findings fix wave (2026-10-12, commit a8af1df)
Asked by Dustin ("fix the harness issues you noted and get ready for a re-run"), list = demo README
section 5 items 1-5, plus his mid-turn ask for pointer control ("a way to control the mouse").
Deviations from that list, each with its reason:
- **Added: `move` verb** (scope-change, asked mid-turn): click/drag/scroll existed, hover did not.
- **Added: qtFRED SDL_NO_SIGNAL_HANDLERS** (behavioral-change, found by the fix): with the runner-first
  stop qtFRED ignored SIGTERM for 20 s and ended in SIGKILL; root cause SDL's own SIGTERM handler.
- **Finding 4 reclassified** (other): fred_check's clean result was real evidence all along (qtFRED's
  save runs the check); only the note was wrong, plus potential issues are not covered on save.
- **Finding 2 root cause** (other): not a coordinate problem; sub-frame clicks (ui/uimouse.cpp:43-58).
Effect on functionality: every click is 0.15 s longer; every stop now takes about 2 s (game) instead of
aborting; qtFRED stops end on SIGTERM. Joystick input: OUT by Dustin's ruling (hardware-compat, not mods).


## Appendix: Deferments originated in the demo-findings QA (HIGH-RISK, 2026-10-12)
| ID | Found by | Severity | What was deferred | Why deferred | Fix direction | Detection / revisit | Obsolete when |
|---|---|---|---|---|---|---|---|
| DEF-DF-01 | SL-5 | LOW | stop never executed in gameplay, mid-load, with a popup open, on Vulkan, on a debug build, or with a runner stuck past 25 s | the main-hall and qtFRED cells pass, and the runtime-limit cell now has an engine test; each other cell is its own engine scenario | engine tests that stop in each state and assert exit_info.signal is None | `coredumpctl list` after agent loops; revisit on any core whose backtrace shows _XIOError | — |
| DEF-DF-02 | CR-2 residual | LOW | `click double=true` is not engine-verified on an FSO double-click target (pilot list) | a pilot list needs an existing pilot without auto-select; the harness has no such launch mode | add a launch mode that copies a pilot but stays in the chooser, then double-click it and assert GS_STATE_MAIN_MENU | revisit when a test needs FSO double-click | — |
| DEF-DF-03 | MU-6 | LOW | surviving mutants in pre-existing branches of the touched functions (screenshot crop/scale argv, stop's missing-session paths, record._issue's other verbs, fred.check's OCR path) | not introduced by this change; same class as DEF-QA-05 | argv asserts per option; per-verb replay tests; OCR path unit tests with captured text | next full mutation run | — |


## Post-v1: Lua pre-send check (2026-10-12 evening)
Dustin: "is there any way to harden the harness so some of them [the MCP instructions] become
unnecessary? If so, do it. If not, leave the instructions in." The `lua` verb now checks code (and
absolute-path loadfile/dofile targets, one level) against the build's cached scripting.json and
refuses unknown members of the checked libraries and colon calls on the no-self control_info
functions (fsoctl/lua_check.py). Handle types are invisible to it, so those instruction rules stay;
the INSTRUCTIONS now say what the tool enforces and when it is skipped. Architect: SHIP with
conditions. QA (ROUTINE): record kept outside this repo.

## Appendix: Deferments originated in the Lua pre-send check QA (ROUTINE, 2026-10-12)
| ID | Found by | Severity | What was deferred | Why deferred | Fix direction | Detection / revisit | Obsolete when |
|---|---|---|---|---|---|---|---|
| DEF-TC-01 | MU-4 | LOW | surviving mutants in pre-existing wait_for and pilot lines (telemetry/collision change) | not introduced by this change | keyword/argv asserts; log-kind wait test; timeout-message asserts | next full mutation run | — |
| DEF-LC-01 | CR-4 | LOW | the docs cache key is computed per `lua` call from disk (binary hash, mod walk, FS2PATH env), so a binary rebuilt in place under a running session gives the new build's API to the old engine | needs a launch-time field in the session record; today's flows stop sessions before rebuilding | store the cache key (or the docs dir) in session.json at launch and use it in `lua()` | a refusal naming a function the running build does have; revisit if test_engine_change gains rebuild-without-relaunch | session records carry their docs key |

## Post-v1: switchable telemetry and collision alerts (2026-10-12 evening)
Dustin: "Add the telemetry feature but make it switchable, so the agent only turns it on for runs
where it's necessary. There should also be better collision avoidance. No human player would allow
grinding on the hull for so long. You should get an alert on collision along with which direction
the nearest object is so you can turn and fly away". Architect: SHIP; trims taken (no suggested
escape, no direction words, one direction, last contact + count). Bridge: `$On Ship Collision`
handler (player + no weapon/beam, once per frame), engine-run count (never reset, so wait baselines
stay valid), `pilot` stop reason "collision" (stop_on_collision, default on), `state`
player.collision/collisions_total, `check` kind collision; `telemetry` command (per-frame ring of
15000 rows only while on; read = summary + downsampled samples, 120 s / 10 Hz caps; cleared when
mission time jumps back). Python: telemetry verb, pilot flag, wait kind; MCP: telemetry tool.
Engine runs on 26.1.0: live-test record kept outside this repo (rows A-O; one mission, one freighter). Probe facts: probe-results section 20.

QA (HIGH-RISK, 4 Opus seats + mutation + two external model seats): record kept outside this repo.
Deviations from the Architect-approved design, each from a review finding:
- behavioral-change: collisions count contact STARTS (nothing of that object touched us the
  previous simulation frame), not frames in contact, so an escape flight is not stopped by the
  contact it escapes (SLA-1/CR-1); continuity counts gameplay frames so a pause does not split a
  contact (XFA-4/XFK-1); debris and asteroids are keyed by signature (XFA-1).
- behavioral-change: a run epoch (other mission file or clock jumping back) replaces the clock-only
  telemetry clear and hides the previous run's contact from `state` (SLA-3, SF-4, XFA-5).
- scope-change (small): pilot replies gain `collisions` and `touching`; state gains `touching` and
  `collision_errors`; check detail gains touching/errors; telemetry replies gain now / last_row_t /
  cleared / error; heartbeat and bridge_info gain telemetry_on. pilot and check report the latest
  NEW contact (XFK-3).
- behavioral-change: player_dead is checked before collision (CR-11); telemetry runs in its own
  pcall and switches itself off on error (SF-2).
- scope-change: props are NOT reported (the engine never runs this hook for them,
  collideshipship.cpp:1664-1690); light debris/asteroid brushes are not reported by the engine.
- other: game.py `lua()` initialises `ref = None` before its try (a Pyright unbound warning);
  unrelated to the feature, kept because it is a one-line correctness fix in a touched file.
- Demo controller (outside the repo): contact reflex via its own collision hook; five review fixes;
  one bug introduced by those fixes found live and fixed (record row J).

Proposal (open, Dustin's call): after a collision stop the ship keeps its cruise throttle and, in
smoke row N, drifted into the freighter again before the agent's next call (~7 mission s). Option:
zero (or briefly reverse) the throttle on a collision stop before handing control back. Tradeoff:
the bridge would change the player's controls on its own.

Appendix: Deferments originated in this change
- DEF-TC-01 | found by mutation pass (MU-4) | uncommitted (2026-10-12) | defer: next full mutation
  run | LOW | 41 surviving mutants in pre-existing lines of the touched functions wait_for (log
  routing, timeout loop, messages) and pilot (TypeError path, check_pilot_args keywords, the call's
  session id was pinned now) | not introduced here; minimal-diff | fix: argv/keyword asserts in
  pilot tests, a log-kind wait test, timeout-message asserts | obsolete if wait_for/pilot are
  rewritten.
