"""Run under mcp/.venv/bin/python by test_mcp_wrapper.py: exercise the server's tool functions
with the fsoctl subprocess replaced, so no engine is needed. Prints OK or raises.

Checks: results and errors are passed through; every tool's command line parses with fsoctl's
REAL parser, including values that start with "-" (QA TA-5); a timed-out fsoctl gets SIGTERM
before SIGKILL so its cleanup runs (QA SF-13).
"""
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp"))
import server  # path set up above; it also puts the repo on sys.path

from fsoctl import cli  # importable because server added the repo to sys.path

calls = []
PNG = Path(sys.argv[1])
real_exec = server._exec


def fake_exec(cmd, timeout):
    calls.append((cmd, timeout))
    verb = cmd[cmd.index("--json") + 1]
    if verb == "status":
        body = json.dumps({"ok": True, "status": "running"})
        return subprocess.CompletedProcess(cmd, 0, body, "")
    if verb == "screenshot":
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"ok": True, "path": str(PNG),
                                                               "width": 1, "height": 1}), "")
    if verb == "stop":
        return subprocess.CompletedProcess(cmd, 6, json.dumps({"ok": False, "code": 6,
                                                               "message": "dead", "hint": "h"}), "")
    if verb == "wait_for" and "--timeout=1" in cmd:
        raise subprocess.TimeoutExpired(cmd, timeout)
    return subprocess.CompletedProcess(cmd, 0, json.dumps({"ok": True}), "")


server._exec = fake_exec

assert json.loads(server.status("s12345678"))["status"] == "running"
assert calls[-1][0][-3:] == ["status", "--", "s12345678"]

server.keys("s12345678", ["alt+r", "Return"], hold_ms=50)
assert calls[-1][0][-5:] == ["--hold-ms=50", "--", "s12345678", "alt+r", "Return"], calls[-1][0]

out = server.screenshot("s12345678")
assert any(type(x).__name__ == "Image" for x in out), out

err = json.loads(server.stop("s12345678"))       # non-zero exit: JSON error, not an exception
assert err["ok"] is False and err["code"] == 6

t = json.loads(server.wait_for("s12345678", "mission_time", "5", timeout=1))
assert t["ok"] is False and "timed out" in t["message"]

# ---- launch's budget covers the waits a launch can sit in before readiness ------------------
from fsoctl import bridge_install, launcher  # noqa: E402  (after server put the repo on sys.path)

server.launch(ready_timeout=240)
_, budget = calls[-1]
# plan A QA TA-9: no network given -> no --network, so the CLI's own default (real) decides
assert not any(a.startswith("--network") for a in calls[-1][0]), calls[-1][0]
# launch.lock wait + bridge.lock wait + scope start (10 s) + readiness + a stop on failure: the
# runner wait, the scope's polite stop (20 s) and its kill wait (QA CR-1/SL-2/TA-4).
from fsoctl import scope  # noqa: E402
from fsoctl import sound  # noqa: E402
STOP_WORST = (launcher._RUNNER_EXIT_WAIT + scope.POLITE_STOP_SEC + scope._KILL_WAIT
              + sound.REMOVE_WORST_SEC)
assert budget >= (240 + launcher._LOCK_WAIT + bridge_install._LOCK_WAIT + 10 + STOP_WORST
                  + sound.LAUNCH_WORST_SEC), budget
assert server.TERM_GRACE_SEC >= STOP_WORST + 5, (server.TERM_GRACE_SEC, STOP_WORST)
assert "--interactive" not in calls[-1][0]          # default: -noninteractive stays on
server.launch(interactive=True, runtime=600)
assert "--interactive" in calls[-1][0] and "--runtime=600" in calls[-1][0]

# ---- every tool's argv against the real parser (values chosen to start with "-") -------------
S = "s12345678"
CASES = {
    "launch": dict(mods="MVPS", mission="m.fs2", pilot="existing:P", pilot_source="/tmp/p",
                   skip_movies=True, interactive=True, runtime=600, extra_args=["-window"],
                   network="real", sound=True),
    "status": dict(id=S), "stop": dict(id=S), "list_sessions": {}, "cleanup": {},
    "version": {}, "guide": dict(section="-fly"),
    "screenshot": dict(id=S, crop="10x10+0+0", scale=0.5, full=True),
    "log": dict(id=S, grep="-ERR"), "state": dict(id=S, ships=False), "journal": dict(id=S),
    "crash_report": dict(id=S),
    "sexp": dict(id=S, expr="-( true )", mode="eval"), "lua": dict(id=S, code="--[[x]] return 1"),
    "post_event": dict(id=S, name="GS_EVENT_QUIT_GAME"), "answer_popup": dict(id=S, choice=1),
    "pilot": dict(id=S, pitch=-0.5, primary=True, seconds=2, stop_on_collision=False),
    "telemetry": dict(id=S, action="read", seconds=30, hz=4),
    "autopilot": dict(id=S, on=True), "time": dict(id=S, compression=400),
    "keys": dict(id=S, keys=["-", "minus"], hold_ms=0),
    "click": dict(id=S, x=-1, y=5, double=True), "drag": dict(id=S, x1=1, y1=2, x2=-3, y2=4),
    "scroll": dict(id=S, x=10, y=10, clicks=-3), "move": dict(id=S, x=-1, y=5),
    "wait_for": dict(id=S, kind="log", value="-ERR", timeout=5),
    "replay": dict(source_id=S, speed=2), "build": dict(worktree="/tmp/w", targets=["-x", "fs2"]),
    "scratch_mod": dict(action="remove", name="fso_harness_scratch_abcdef", label="-l"),
    "validate": dict(mods="M", mission="m.fs2"),
    "vp": dict(action="extract", archive="a.vp", pattern="-*", dest="/tmp/d", overwrite=True),
    "fred_open": dict(mission="m.fs2", mods="M"), "fred_save": dict(id=S),
    "fred_camera": dict(id=S), "fred_check": dict(id=S),
    "fred_select": dict(id=S, x1=-1, y1=0, x2=5, y2=5), "fred_selection": dict(id=S),
    "api_docs": dict(refresh=True), "model_info": dict(filter="-x"),
    "frame_stats": dict(mission="m.fs2", seconds=5),
    "compare": dict(a="/a", b="/b", steps_file="/s.json", image_tol=0.02),
    "renderer_compare": dict(steps_file="/s.json"),
    "match_create": dict(label="-l", network="private"), "match_list": {},
    "match_remove": dict(id="m0a1b2c3d"),
}
parser = cli.build_parser()
tool_names = {t.name for t in asyncio.run(server.mcp.list_tools())}
covered = {("list" if k == "list_sessions" else k) for k in CASES}
assert covered == tool_names, (f"untested tools: {tool_names - covered}; "
                               f"stale: {covered - tool_names}")
for fn_name, kwargs in CASES.items():
    calls.clear()
    getattr(server, fn_name)(**kwargs)
    argv = calls[-1][0][calls[-1][0].index("--json"):]
    try:
        ns = vars(parser.parse_args(argv))
    except BaseException as e:   # _Parser raises HarnessError; --help style exits SystemExit
        raise AssertionError(f"{fn_name}: {argv} does not parse: {e}") from e
    # Each plain value must land in the parser field of the same name (QA TA2-11): a tool that
    # swapped x and y, or mission and mods, would still parse but act on the wrong thing.
    for name, value in kwargs.items():
        if name in ns and isinstance(value, (str, int, float)) and not isinstance(value, bool):
            got = ns[name]
            same = (float(got) == float(value) if isinstance(value, (int, float))
                    else str(got) == value)
            assert same, f"{fn_name}: {name}={value!r} landed as {got!r} ({argv})"
        elif name in ns and isinstance(value, bool):   # a flag the tool dropped (QA TA-8)
            assert ns[name] is value, f"{fn_name}: {name}={value!r} landed as {ns[name]!r}"

# pilot with stop_on_collision left at its default must keep the stop on (QA TA-10): the loop
# above only sees the False case, so a tool that always sent --no-stop-on-collision would pass.
calls.clear()
server.pilot(id=S, seconds=1)
argv = calls[-1][0][calls[-1][0].index("--json"):]
assert "--no-stop-on-collision" not in argv, argv
assert vars(parser.parse_args(argv))["stop_on_collision"] is True, argv

# ---- the two Windows options reach the real parser (plan B Tasks 5 and 9) --------------------
server.launch(visible=True)
argv = calls[-1][0][calls[-1][0].index("--json"):]
assert "--visible" in argv and vars(parser.parse_args(argv))["visible"] is True, argv
server.launch()
assert "--visible" not in calls[-1][0], calls[-1][0]
server.build("/w/tree", config="FastDebug")
argv = calls[-1][0][calls[-1][0].index("--json"):]
assert "--config=FastDebug" in argv and vars(parser.parse_args(argv))["config"] == "FastDebug"
server.build("/w/tree")
assert not any(a.startswith("--config") for a in calls[-1][0]), calls[-1][0]

# ---- timeout path: a stop request first, so fsoctl's cleanup can run -------------------------
# SIGTERM on Linux; Ctrl-Break (SIGBREAK in the child) on Windows, where it arrives through
# the console this process shares with the child. The child waits in short sleeps, as
# fsoctl's own waits do: on Windows one long sleep is not interrupted by the request.
STOP_NAME = "SIGBREAK" if sys.platform == "win32" else "SIGTERM"
with tempfile.TemporaryDirectory() as d:
    marker = Path(d) / "got-stop-request"
    child = ("import signal, sys, time\n"
             f"signal.signal(signal.{STOP_NAME}, lambda *a: (open({str(marker)!r}, 'w').close(), "
             "sys.exit(0)))\n"
             "for _ in range(300):\n"
             "    time.sleep(0.1)\n")
    try:
        real_exec([sys.executable, "-c", child], 1)
        raise AssertionError("expected TimeoutExpired")
    except subprocess.TimeoutExpired:
        pass
    assert marker.exists(), f"timed-out child was not sent {STOP_NAME} before being killed"


# ---- how fsoctl is started and asked to stop, per OS (plan B Task 10; Popen replaced) --------
# Measured in the VM on 2026-11-16 (results note, "Stopping a timed-out fsoctl"): Ctrl-Break
# reaches a child only through a console it shares with the sender. A child given
# CREATE_NO_WINDOW has a console of its own and never gets it; a sender with no console at all
# cannot send it (WinError 6), and its child would open a visible console window unless
# started with CREATE_NO_WINDOW.
class FakeProc:
    """Stands in for subprocess.Popen: records how it was started and what was done to it."""
    hangs = 1                 # how many communicate() calls time out before one returns
    signal_fails = False      # whether send_signal raises as it does with no shared console
    last = None

    def __init__(self, cmd, **kw):
        self.cmd, self.kw, self.calls, self.waits = cmd, kw, [], 0
        FakeProc.last = self

    def communicate(self, timeout=None):
        self.calls.append(("communicate", timeout))
        self.waits += 1
        if self.waits <= FakeProc.hangs:
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        return "", ""

    def send_signal(self, sig):
        self.calls.append(("send_signal", sig))
        if FakeProc.signal_fails:
            raise OSError(6, "The handle is invalid")

    def terminate(self):
        self.calls.append(("terminate",))

    def kill(self):
        self.calls.append(("kill",))


def timed_out(windows, console=True, hangs=1, signal_fails=False):
    """Run the real _exec against FakeProc as on the given OS; return the fake process."""
    FakeProc.hangs, FakeProc.signal_fails = hangs, signal_fails
    saved = (server._WINDOWS, server._has_console, server.subprocess.Popen)
    server._WINDOWS, server._has_console = windows, lambda: console
    server.subprocess.Popen = FakeProc
    try:
        real_exec(["fsoctl-stand-in"], 7)
        raise AssertionError("expected TimeoutExpired")
    except subprocess.TimeoutExpired:
        return FakeProc.last
    finally:
        server._WINDOWS, server._has_console, server.subprocess.Popen = saved


GRACE = server.TERM_GRACE_SEC
NEW_GROUP, NO_WINDOW, CTRL_BREAK = 0x200, 0x08000000, 1      # WinBase.h / WinCon.h values
if sys.platform == "win32":                                    # the names Windows itself gives
    assert (NEW_GROUP, NO_WINDOW, CTRL_BREAK) == (
        subprocess.CREATE_NEW_PROCESS_GROUP, subprocess.CREATE_NO_WINDOW,
        __import__("signal").CTRL_BREAK_EVENT)

p = timed_out(windows=False)                                   # Linux: unchanged
assert "creationflags" not in p.kw, p.kw
assert p.calls == [("communicate", 7), ("terminate",), ("communicate", GRACE)], p.calls
p = timed_out(windows=False, hangs=2)
assert p.calls[-2:] == [("kill",), ("communicate", None)], p.calls

p = timed_out(windows=True, console=True)                      # shares this server's console
assert p.kw["creationflags"] == NEW_GROUP, hex(p.kw["creationflags"])
assert p.calls == [("communicate", 7), ("send_signal", CTRL_BREAK), ("communicate", GRACE)]
p = timed_out(windows=True, console=True, hangs=2)             # asked, did not go: ended
assert p.calls == [("communicate", 7), ("send_signal", CTRL_BREAK), ("communicate", GRACE),
                   ("kill",), ("communicate", None)], p.calls
assert ("terminate",) not in p.calls                           # a hard kill on Windows: never first

p = timed_out(windows=True, console=False)                     # no console: no window, no asking
assert p.kw["creationflags"] == NEW_GROUP | NO_WINDOW, hex(p.kw["creationflags"])
assert p.calls == [("communicate", 7), ("kill",), ("communicate", None)], p.calls

p = timed_out(windows=True, console=True, signal_fails=True)   # the request could not be sent
assert p.calls == [("communicate", 7), ("send_signal", CTRL_BREAK), ("kill",),
                   ("communicate", None)], p.calls

server._exec = real_exec                                       # what the timeout says, per OS
saved = (server._WINDOWS, server._has_console, server.subprocess.Popen)
FakeProc.hangs, FakeProc.signal_fails = 1, False
server._WINDOWS, server._has_console, server.subprocess.Popen = True, lambda: False, FakeProc
try:
    said = json.loads(server._ctl("status", "s12345678", timeout=7))
finally:
    server._WINDOWS, server._has_console, server.subprocess.Popen = saved
    server._exec = fake_exec
assert said["ok"] is False and "SIGTERM" not in said["hint"] and "list" in said["hint"], said

# ---- guidance the server carries: instructions at session start, prompts as /commands --------
ins = server.mcp.instructions
assert ins and len(ins) <= 2048, len(ins or "")            # Claude Code truncates longer text
for word in ("allow_eval", "sexp", "wait_for", "lua", "scratch_mod"):
    assert word in ins, word
prompts = {p.name: p for p in asyncio.run(server.mcp.list_prompts())}
assert set(prompts) == set(server.PROMPTS), set(prompts) ^ set(server.PROMPTS)
guide = (Path(server.REPO) / "docs" / "AGENT-GUIDE.md").read_text()
for name, heading in server.PROMPTS.items():
    assert f"\n## {heading}\n" in guide, heading             # every prompt is a guide recipe
    got = asyncio.run(server.mcp.get_prompt(name))
    text = got.messages[0].content.text
    assert heading in text and len(text) > 200, name
assert "sexp" in asyncio.run(server.mcp.get_prompt("playtest_mission")).messages[0].content.text

print("OK", len(tool_names), "tools", len(prompts), "prompts")
