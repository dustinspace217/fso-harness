#!/usr/bin/env python3
"""MCP server for the FSO harness: one tool per fsoctl verb, nothing more.

WHY stateless: every tool runs `python -m fsoctl --json <verb> ...` and returns what it printed.
The server holds no sessions and no connections, so if this process ends (MCP servers do not
outlive their client), nothing is lost: sessions live in their systemd scopes and runtime files,
and any later call, from any agent or the plain CLI, picks them up. Same design as an earlier
harness of mine for another engine.

Errors are returned, never raised: the CLI's JSON (code, message, hint) is what tells an agent
what to try next.

Argument order matters. Every tool builds its command as: verb, options (always `--name=value`),
then `--`, then positional values. WHY: a positional value that starts with "-" (scrolling down
is `-3` clicks, a log regex like `-ERR`, Lua code like `--[[x]]`) would otherwise be read by
argparse as an unknown option (QA TA-5). After `--`, argparse treats everything as positional.

Timeouts: each tool's limit is derived from the verb's own internal waits plus a margin, so the
server never gives up while fsoctl is still inside a bounded wait. If the limit is reached anyway,
fsoctl gets SIGTERM first (it turns that into a normal exit so its cleanup can stop sessions it
started; QA SF-13/SL-9) and SIGKILL only if it has not exited TERM_GRACE_SEC later.
"""
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

# MCP SDK 2.0: the high-level server is mcp.server.MCPServer (1.x called it FastMCP); checked
# against the installed 2.0.0 package (mcp/setup.sh pins it).
from mcp.server import MCPServer
from mcp.server.mcpserver import Image

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
# Loaded only to confirm the package imports from this checkout (fails fast at startup).
importlib.import_module("fsoctl.errors")

DEFAULT_TIMEOUT = 90
# Time fsoctl gets to clean up after SIGTERM before SIGKILL. Must cover a stop of the session
# it started: up to 25 s for the runner (launcher._RUNNER_EXIT_WAIT), then the scope's 20 s
# polite stop and 10 s kill wait, then status polls (QA SL2-1; raised for runner-first stop,
# QA CR-1/SL-2/TA-4). tests/mcp_check.py checks the sum.
TERM_GRACE_SEC = 75
INLINE_IMAGE_MAX_BYTES = 16 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Loaded by Claude Code at session start for every connected server (truncated at 2,048
# characters, so the first lines carry the most). Its job is the one thing agents miss most: a
# mission can be tested by script, not by flying it (QA follow-up, Dustin 2026-10-12).
INSTRUCTIONS = """\
fso-harness drives the FreeSpace Open engine and the qtFRED mission editor headlessly for you:
build an engine change, launch a mission, read and change game state, wait for events, compare
builds or renderers, validate a mod, read a crash, edit a mission.

The fast way to test a mission or a change is by SCRIPT, not by flying: launch with
allow_eval=true and every skip_* flag, wait_for state GS_STATE_GAME_PLAY, autopilot on, time
compression up, then use sexp (eval to ask, run to make anything happen that a mission event
could) and lua (the build's own API, see api_docs) while wait_for pins goals, events and ship
deaths. Prompts: /fso-harness:playtest_mission, test_engine_change, check_mod, compare_builds,
read_crash. Recipes in docs/AGENT-GUIDE.md.

Rules the tools enforce: lua, post_event and Lua-running sexps need allow_eval (audited); event
and ship names must exist and may not contain " ! ( ); sexps are capped below 8000 bytes;
qtFRED only ever edits a copy of a mission; write mods into a scratch_mod, never the game folder.
Every call returns JSON; on failure read message and hint. Exit codes: 5 timeout (the message
says whether the command ran), 6 engine gone (crash_report), 7 refused, 8 Lua error, 9 the work
failed. Always stop sessions you started.

Lua that cannot crash the engine (a bad name or argument aborts it; pcall does NOT catch that):
the lua tool refuses unknown library functions (ba.x, mn.x...) and colon calls on the no-self
control_info functions (call ci.useButtonControl(-1, "X") with a dot) when api_docs is cached
for the build; its reply's lua_check says passed or skipped. It cannot see handle types:
compare handles with getSignature(), read ship-only fields only after getBreedName() == "Ship"
(a Target or HomingObject can be a missile), and check handle methods in api_docs scripting.lua.
Flying the player from Lua: docs/AGENT-GUIDE.md "Fly a mission" (steering conventions, reloadable
hook; keep time compression at 100 while a controller flies)."""

mcp = MCPServer("fso-harness", instructions=INSTRUCTIONS)


def _exec(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Run one fsoctl command; on timeout send SIGTERM, then SIGKILL (replaced in tests).

    Receives the full command and the limit in seconds. Returns the finished process, or raises
    subprocess.TimeoutExpired after the process has been stopped either way.
    WHY not subprocess.run(timeout=...): that sends SIGKILL at once, which skips fsoctl's
    cleanup and leaves a launched engine holding a session slot until its runtime limit.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            cwd=REPO, env=os.environ.copy())
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.communicate(timeout=TERM_GRACE_SEC)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _ctl(*args: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Run `fsoctl --json <args>` and return its JSON output, or a JSON error."""
    cmd = [sys.executable, "-m", "fsoctl", "--json", *args]
    try:
        r = _exec(cmd, timeout)
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "message": f"fsoctl timed out after {timeout:.0f}s",
                           "hint": "fsoctl was asked to stop (SIGTERM) and then killed if needed; "
                                   "a session it started may still be running: check `list`"})
    out = r.stdout.strip()
    if out.startswith("{"):
        return out
    return json.dumps({"ok": r.returncode == 0, "message": (r.stderr or out).strip()[-2000:],
                       "exit_code": r.returncode})


def _opt(name: str, value) -> list[str]:
    """Render an optional flag: [] when unset, ["--name"] for True, ["--name=value"] otherwise."""
    if value is None or value is False:
        return []
    if value is True:
        return [f"--{name}"]
    return [f"--{name}={value}"]


def _pos(*values) -> list[str]:
    """Positional values, after `--` so none of them can be read as an option (see module doc)."""
    return ["--", *(str(v) for v in values)]


# ---- prompts: the agent guide's recipes as /fso-harness:<name> commands ----------------------
# One source: each prompt returns the matching "## <heading>" section of docs/AGENT-GUIDE.md, so
# the guide and the commands cannot drift apart (tests/mcp_check.py checks every heading exists).

GUIDE = REPO / "docs" / "AGENT-GUIDE.md"
PROMPTS = {
    "playtest_mission": "Playtest a mission with scripts",
    "test_engine_change": "Test an engine code change",
    "check_mod": "Write and check a mission or a table in a scratch mod",
    "compare_builds": "Compare a branch with master",
    "read_crash": "Read a crash",
}


def _guide_section(heading: str) -> str:
    """Return the guide's `## heading` section (heading line included) up to the next heading."""
    text = GUIDE.read_text()
    start = text.index(f"\n## {heading}\n") + 1
    end = text.find("\n## ", start + 1)
    body = text[start:] if end == -1 else text[start:end]
    return (body.strip()
            + "\n\nTools are listed by the fso-harness MCP server; every call returns JSON.")


def _register_prompt(name: str, heading: str) -> None:
    """Register one guide recipe as an MCP prompt named `name`."""
    def recipe() -> str:
        return _guide_section(heading)
    recipe.__name__ = name
    recipe.__doc__ = f"fso-harness recipe: {heading.lower()}."
    mcp.prompt(name=name, description=recipe.__doc__)(recipe)


for _name, _heading in PROMPTS.items():
    _register_prompt(_name, _heading)


# ---- sessions --------------------------------------------------------------------------------

# Seconds a launch can spend BEFORE its readiness wait starts, plus a stop on failure: the
# launch.lock wait (60 s), the bridge.lock wait (60 s), the scope start (10 s) and a stop (~55 s:
# runner wait 25 s + scope stop 20 s + kill wait 10 s).
# Keep in step with launcher._LOCK_WAIT and bridge_install._LOCK_WAIT (tests/mcp_check.py
# checks the sum). With a smaller margin a slow but healthy launch was cancelled by SIGTERM.
LAUNCH_EXTRA_SEC = 60 + 60 + 10 + 55 + 40


@mcp.tool()
def launch(mods: str = "", mission: str | None = None, vulkan: bool = False,
           res: str = "1280x720", skip_movies: bool = False, pilot: str = "chooser",
           pilot_source: str | None = None, dismiss_popups: bool = False,
           skip_command_briefing: bool = False, skip_briefing: bool = False,
           allow_eval: bool = False, qtfred: bool = False, binary: str | None = None,
           interactive: bool = False, runtime: int = 1800, ready_timeout: float = 240.0,
           extra_args: list[str] | None = None) -> str:
    """Start a game (or qtFRED) session on a private virtual display; returns its id once ready.

    pilot: "chooser" | "existing:NAME" (needs pilot_source: a folder holding that pilot's files)
    | "create:NAME". mods: comma-separated mod folders. Startup stages can each be skipped:
    skip_movies, dismiss_popups, skip_command_briefing, skip_briefing (with mission = straight
    into gameplay). allow_eval enables lua/post_event. interactive drops -noninteractive (an
    engine Error() then waits on a dialog instead of ending the session). runtime is the hard
    limit in seconds after which the session is killed (default 30 min). For qtFRED, open
    missions with fred_open (it works on a copy); launch refuses a qtFRED mission outside the
    harness's copy folder.
    """
    args = ["launch", *_opt("mods", mods or None), *_opt("mission", mission),
            *_opt("vulkan", vulkan), f"--res={res}", *_opt("skip-movies", skip_movies),
            f"--pilot={pilot}", *_opt("pilot-source", pilot_source),
            *_opt("dismiss-popups", dismiss_popups),
            *_opt("skip-command-briefing", skip_command_briefing),
            *_opt("skip-briefing", skip_briefing), *_opt("allow-eval", allow_eval),
            *_opt("qtfred", qtfred), *_opt("binary", binary), *_opt("interactive", interactive),
            f"--runtime={runtime}", f"--ready-timeout={ready_timeout}"]
    if extra_args:
        args += ["--", *extra_args]
    return _ctl(*args, timeout=ready_timeout + LAUNCH_EXTRA_SEC)


@mcp.tool()
def status(id: str) -> str:
    """Session record, whether it is alive, and the bridge heartbeat with its age (stale > 5 s)."""
    return _ctl("status", *_pos(id))


@mcp.tool()
def stop(id: str) -> str:
    """Stop a session (kills it; idempotent). The run folder is kept for crash_report."""
    return _ctl("stop", *_pos(id), timeout=120)


@mcp.tool(name="list")
def list_sessions() -> str:
    """List all sessions."""
    return _ctl("list")


@mcp.tool()
def version() -> str:
    """The harness version this server runs (compare with bridge_info's version from status)."""
    return _ctl("version", timeout=30)


@mcp.tool()
def cleanup(older_than: float = 600) -> str:
    """Delete run folders of finished sessions older than `older_than` seconds (default 10 min).

    Sessions still starting, running or shutting down are never touched.
    """
    return _ctl("cleanup", f"--older-than={older_than}")


# ---- observing -------------------------------------------------------------------------------

@mcp.tool()
def screenshot(id: str, crop: str | None = None, scale: float | None = None,
               full: bool = False) -> list:
    """Screenshot the session window; returns the image and its path/size. crop: WxH+X+Y.

    full=True shows the whole display, including qtFRED dialogs and open menus, and returns
    window_origin. With full=True, crop is in display coordinates. To click a point in the image:
    divide by scale (if given), add the crop's X/Y (if given), subtract window_origin.
    """
    text = _ctl("screenshot", *_opt("crop", crop), *_opt("scale", scale), *_opt("full", full),
                *_pos(id))
    result: list = [text]
    try:
        path = Path(json.loads(text)["path"])
        if path.stat().st_size <= INLINE_IMAGE_MAX_BYTES and path.read_bytes()[:8] == PNG_SIGNATURE:
            result.insert(0, Image(path=str(path)))
    except (KeyError, ValueError, OSError):
        pass   # the JSON text (with the error) is still returned
    return result


@mcp.tool()
def log(id: str, source: str = "auto", tail: int = 200, grep: str | None = None) -> str:
    """Tail the session log (source auto|engine|fso) and list warnings/errors/asserts."""
    return _ctl("log", f"--source={source}", f"--tail={tail}", *_opt("grep", grep), *_pos(id))


@mcp.tool()
def state(id: str, ships: bool = True, goals: bool = True, events: bool = True,
          limit: int = 200) -> str:
    """Game state, mission time, player, ships (pos, hull, shields, target), goals, events."""
    return _ctl("state", *_opt("no-ships", not ships), *_opt("no-goals", not goals),
                *_opt("no-events", not events), f"--limit={limit}", *_pos(id), timeout=60)


@mcp.tool()
def journal(id: str) -> str:
    """Actions recorded for a session (for replay), each with its outcome."""
    return _ctl("journal", *_pos(id))


@mcp.tool()
def crash_report(id: str) -> str:
    """How a session's engine ended: signal, core dump, gdb backtrace, log problems, popups."""
    return _ctl("crash_report", *_pos(id), timeout=300)


# ---- acting ----------------------------------------------------------------------------------

@mcp.tool()
def sexp(id: str, expr: str, mode: str = "eval") -> str:
    """Evaluate a sexp in the running mission: eval (bool), number, or run (inside a when).
    Strings use ! as quotes, e.g. ( is-destroyed-delay 0 !Alpha 2! ). Max 7999 bytes."""
    return _ctl("sexp", f"--mode={mode}", *_pos(id, expr))


@mcp.tool()
def lua(id: str, code: str) -> str:
    """Run free-form Lua in the engine (session launched with allow_eval; audited)."""
    return _ctl("lua", *_pos(id, code))


@mcp.tool()
def post_event(id: str, name: str) -> str:
    """Post a raw game event such as GS_EVENT_QUIT_GAME (allow_eval sessions; audited)."""
    return _ctl("post_event", *_pos(id, name))


@mcp.tool()
def answer_popup(id: str, choice: int = 0) -> str:
    """Answer the popup that is open now with button `choice` (0 = first button)."""
    return _ctl("answer_popup", f"--choice={choice}", *_pos(id))


@mcp.tool()
def pilot(id: str, pitch: float = 0.0, heading: float = 0.0, bank: float = 0.0,
          forward: float = 0.0, primary: bool = False, secondary: bool = False, cm: bool = False,
          seconds: float = 1.0, stop_on_collision: bool = True) -> str:
    """Fly the player ship for `seconds` of mission time (axes -1..1; fire flags fire every
    frame), then hand control back. Refused while paused. The reply says whether the flight
    completed and why it stopped. A NEW contact stops the flight (stopped_because "collision");
    `collision.direction` gives where it is in the ship's axes (right/up/forward, unit length):
    steer the other way, reversing if it is ahead, and fly off. Contact that began before the
    flight does not stop it, so the escape flight works; `touching` says whether you are still
    in contact. stop_on_collision=false keeps flying (`collisions` still counts contacts)."""
    return _ctl("pilot", f"--pitch={pitch}", f"--heading={heading}", f"--bank={bank}",
                f"--forward={forward}", *_opt("primary", primary), *_opt("secondary", secondary),
                *_opt("cm", cm), f"--seconds={seconds}",
                *([] if stop_on_collision else ["--no-stop-on-collision"]), *_pos(id),
                timeout=seconds * 10 + 60)


@mcp.tool()
def telemetry(id: str, action: str, seconds: float = 10.0, hz: float = 2.0) -> str:
    """Per-frame flight telemetry, OFF unless switched on: action on | off | read | clear.
    Turn it on only for runs that need flight-quality data (it costs work every frame). read
    returns a summary (aim error, % of frames within 3 degrees of the target, wiggle = turn-rate
    sign flips per second, collisions) plus samples at `hz` (max 10) over the last `seconds`
    (max 120), as compact arrays under one column list (aim/distance -1 = no target). Every
    reply has `error` (an error switches it off) and `last_row_t` vs `now` (rows stopped)."""
    return _ctl("telemetry", f"--seconds={seconds}", f"--hz={hz}", *_pos(id, action))


@mcp.tool()
def autopilot(id: str, on: bool) -> str:
    """Let the AI fly the player ship (on) or take control back (off)."""
    return _ctl("autopilot", *_pos(id, "on" if on else "off"))


@mcp.tool()
def time(id: str, compression: float | None = None, pause: bool = False,
         resume: bool = False) -> str:
    """Time compression in percent (100 = normal, 400 = 4x), or pause / resume. The reply reads
    back the values actually in effect."""
    return _ctl("time", *_opt("compression", compression), *_opt("pause", pause),
                *_opt("resume", resume), *_pos(id))


@mcp.tool()
def keys(id: str, keys: list[str], hold_ms: int = 0) -> str:
    """Press keys on the session window (xdotool names: Return, alt+r, KP_4). hold_ms 0..10000."""
    return _ctl("keys", f"--hold-ms={hold_ms}", *_pos(id, *keys))


@mcp.tool()
def click(id: str, x: int, y: int, button: int = 1, double: bool = False) -> str:
    """Click at window coordinates."""
    return _ctl("click", f"--button={button}", *_opt("double", double), *_pos(id, x, y))


@mcp.tool()
def move(id: str, x: int, y: int) -> str:
    """Move the pointer to window coordinates without clicking (hover). Returns where the
    pointer really is, in display coordinates."""
    return _ctl("move", *_pos(id, x, y))


@mcp.tool()
def drag(id: str, x1: int, y1: int, x2: int, y2: int, button: int = 1) -> str:
    """Press at (x1,y1), move to (x2,y2), release (window coordinates)."""
    return _ctl("drag", f"--button={button}", *_pos(id, x1, y1, x2, y2))


@mcp.tool()
def scroll(id: str, x: int, y: int, clicks: int) -> str:
    """Turn the mouse wheel at a point: positive = up, negative = down (at most 50 clicks)."""
    return _ctl("scroll", *_pos(id, x, y, clicks))


@mcp.tool()
def wait_for(id: str, kind: str, value: str, timeout: float = 60.0) -> str:
    """Block until a condition holds: kind = state | mission_time | event_true | event_false |
    goal ("Name=complete") | destroyed | departed | log (regex on new log lines) | collision
    (any value; the next contact the player starts, with its direction). Event and ship names
    must exist in the mission and may not contain " ! ( or )."""
    return _ctl("wait_for", f"--timeout={timeout}", *_pos(id, kind, value), timeout=timeout + 30)


@mcp.tool()
def replay(source_id: str, speed: float = 1.0, binary: str | None = None,
           renderer: str | None = None) -> str:
    """Launch a new session like source_id and re-issue the actions that succeeded there (same
    commands, not guaranteed same results). Failed actions are listed under `skipped`."""
    return _ctl("replay", f"--speed={speed}", *_opt("binary", binary),
                *_opt("renderer", renderer), *_pos(source_id), timeout=3600)


# ---- making and checking ---------------------------------------------------------------------

@mcp.tool()
def build(worktree: str, build_dir: str = "build", targets: list[str] | None = None,
          jobs: int = 14, timeout: float = 3600) -> str:
    """Build a configured FSO worktree in a capped scope; returns parsed compiler errors (errors
    first), and the log tail when it fails. A failed build is ok:false with code 9."""
    tgt = [f"--target={t}" for t in targets or []]
    return _ctl("build", f"--build-dir={build_dir}", *tgt, f"--jobs={jobs}",
                f"--timeout={timeout}", *_pos(worktree), timeout=timeout + 60)


@mcp.tool()
def scratch_mod(action: str, name: str | None = None, label: str | None = None) -> str:
    """create / list / remove throwaway mod folders (fso_harness_scratch_*) in FS2PATH."""
    return _ctl("scratch_mod", *_opt("label", label), *_pos(action, *([name] if name else [])))


@mcp.tool()
def validate(mods: str = "", mission: str | None = None, binary: str | None = None,
             timeout: float = 240) -> str:
    """Load mods (and a mission) and report warnings, errors, problems and popups. aborted:true
    comes with abort_reason (engine_died or timeout). Missing mod folders are refused."""
    # Two bounded waits of `timeout` each (launch readiness, then the target state), plus stop.
    return _ctl("validate", *_opt("mods", mods or None), *_opt("mission", mission),
                *_opt("binary", binary), f"--timeout={timeout}", timeout=2 * timeout + 180)


@mcp.tool()
def vp(action: str, archive: str, pattern: str = "*", dest: str | None = None,
       overwrite: bool = False) -> str:
    """list or extract files in a .vp archive (glob pattern on paths inside it). extract refuses
    to overwrite existing files unless overwrite=true; the result lists what it overwrote."""
    return _ctl("vp", *_opt("overwrite", overwrite),
                *_pos(action, archive, pattern, *([dest] if dest else [])), timeout=600)


# ---- qtFRED ----------------------------------------------------------------------------------

@mcp.tool()
def fred_open(mission: str, mods: str = "", binary: str | None = None) -> str:
    """Open a COPY of a mission in qtFRED (found in data/missions or inside .vp archives, in the
    engine's mod order). Damaged archives are skipped and listed."""
    return _ctl("fred_open", *_opt("mods", mods or None), *_opt("binary", binary),
                *_pos(mission), timeout=330)


@mcp.tool()
def fred_save(id: str) -> str:
    """Save the mission copy (Ctrl+S); reports whether the error check opened instead."""
    return _ctl("fred_save", *_pos(id))


@mcp.tool()
def fred_camera(id: str) -> str:
    """Save, then return qtFRED's camera position, orientation and forward vector."""
    return _ctl("fred_camera", *_pos(id))


@mcp.tool()
def fred_check(id: str) -> str:
    """Run qtFRED's pre-save error check and read it by OCR (with a screenshot to verify)."""
    return _ctl("fred_check", *_pos(id), timeout=120)


@mcp.tool()
def fred_select(id: str, x1: int, y1: int, x2: int, y2: int) -> str:
    """Marquee-select a window rectangle in qtFRED; returns the selection status line (OCR),
    whether it changed, and how many drags it took (one retry when nothing changed)."""
    return _ctl("fred_select", *_pos(id, x1, y1, x2, y2))


@mcp.tool()
def fred_selection(id: str) -> str:
    """Read qtFRED's selection status line (OCR from a fixed crop)."""
    return _ctl("fred_selection", *_pos(id))


# ---- analysis --------------------------------------------------------------------------------

@mcp.tool()
def api_docs(binary: str | None = None, mods: str = "", refresh: bool = False) -> str:
    """Paths to this build's own sexp reference (sexps.html) and Lua API (scripting.lua/json)."""
    # Launch readiness (240 s) + file wait (120 s) + clean quit + margin.
    return _ctl("api_docs", *_opt("binary", binary), *_opt("mods", mods or None),
                *_opt("refresh", refresh), timeout=720)


@mcp.tool()
def model_info(mods: str = "", filter: str | None = None, binary: str | None = None) -> str:
    """Face counts per model and LOD from the engine's -pofspew; complete=false if it crashed."""
    return _ctl("model_info", *_opt("mods", mods or None), *_opt("filter", filter),
                *_opt("binary", binary), timeout=1020)


@mcp.tool()
def frame_stats(mission: str, mods: str = "", seconds: float = 30.0,
                binary: str | None = None, vulkan: bool = False) -> str:
    """Frame-time percentiles (engine profiler) for `seconds` of mission time; the whole run is
    under whole_run. The window cut is approximate (see window_method)."""
    # Launch + reach gameplay (2 x 240 s) + the window (seconds*10 + 60) + clean quit + margin.
    return _ctl("frame_stats", *_opt("mods", mods or None), f"--seconds={seconds}",
                *_opt("binary", binary), *_opt("vulkan", vulkan), *_pos(mission),
                timeout=seconds * 10 + 720)


@mcp.tool()
def compare(a: str, b: str, steps_file: str, launch: str = "{}", pos_tol: float = 1.0,
            hull_tol: float = 1.0, image_tol: float = 0.01) -> str:
    """Run a scenario (JSON steps file) on builds a and b; diff state and screenshots.
    identical_within_tolerance needs matching snapshots, goals/events and image difference."""
    return _ctl("compare", f"--a={a}", f"--b={b}", f"--steps={steps_file}", f"--launch={launch}",
                f"--pos-tol={pos_tol}", f"--hull-tol={hull_tol}", f"--image-tol={image_tol}",
                timeout=7200)


@mcp.tool()
def renderer_compare(steps_file: str, launch: str = "{}", binary: str | None = None) -> str:
    """Run a scenario on OpenGL then Vulkan; diff state and screenshots."""
    return _ctl("renderer_compare", f"--steps={steps_file}", f"--launch={launch}",
                *_opt("binary", binary), timeout=7200)


if __name__ == "__main__":
    mcp.run()
