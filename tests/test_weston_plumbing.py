"""How the display choice travels: launcher (choice, refusals, counting GPU sessions still
starting), CLI and MCP entry points, and the runner's main() on a Weston record (QA TA-1, TA-4,
SA-1, CR-8, CR-11, SL-6).

Stand-ins only: no Weston, Xvfb or engine is started. The host probes are stubbed by conftest's
hermetic_display fixture; tests that need a particular choice stub display.choose directly.
"""
import signal
from pathlib import Path

import pytest

from fsoctl import cli, display, launcher, runner, session
from fsoctl.errors import EXIT_REFUSED, EXIT_USAGE, HarnessError
from fsoctl.launcher import LaunchRequest


def _mk(**kw):
    base = dict(kind="game", binary="/bin/true", fs2path="/tmp", mods=[], mission=None,
                renderer="opengl", width=320, height=240, switches={}, allow_eval=False,
                extra_args=[])
    base.update(kw)
    return session.new_session(**base)


@pytest.fixture
def sigmask():
    """Restore this thread's signal mask and SIGTERM handler after runner.main() ran."""
    saved = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    yield
    signal.pthread_sigmask(signal.SIG_SETMASK, saved)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


def test_display_for_passes_network_renderer_and_starting_count(tmp_runtime, monkeypatch):
    seen = []
    monkeypatch.setattr(display, "choose", lambda *a: seen.append(a) or ("xvfb", None))
    monkeypatch.setattr(launcher, "_live_gpu_sessions", lambda: 3)
    assert launcher._display_for(LaunchRequest(binary="/bin/true", renderer="vulkan",
                                               display="weston")) == ("xvfb", None)
    assert seen == [("weston", "real", "vulkan", 3)]


def test_a_standalone_has_no_display_and_refuses_one(tmp_runtime, monkeypatch):
    monkeypatch.setattr(display, "choose", lambda *a: pytest.fail("chose a display"))
    assert launcher._display_for(LaunchRequest(binary="/bin/true", role="standalone")) \
        == (None, None)
    with pytest.raises(HarnessError) as e:
        launcher._display_for(LaunchRequest(binary="/bin/true", role="standalone",
                                            display="weston"))
    assert e.value.code == EXIT_USAGE
    assert (e.value.message, e.value.hint) == ("a standalone server has no display",
                                               "drop --display")


def test_no_display_choice_off_linux(tmp_runtime, monkeypatch):
    monkeypatch.setattr(launcher.platform, "NAME", "windows")
    monkeypatch.setattr(display, "choose", lambda *a: pytest.fail("chose a display"))
    assert launcher._display_for(LaunchRequest(binary="/bin/true")) == (None, None)


def test_an_explicit_display_is_refused_off_linux(tmp_runtime, monkeypatch):
    monkeypatch.setattr(launcher.platform, "NAME", "windows")
    with pytest.raises(HarnessError) as e:
        launcher._refuse_unavailable(LaunchRequest(binary="", display="weston"))
    assert e.value.code == EXIT_REFUSED
    assert "--display weston" in e.value.message
    assert e.value.hint == "tracked in the Linux display options (Windows sessions run on desktops)"


def test_only_live_gpu_sessions_are_counted(tmp_runtime):
    # QA XFA-2/XFK-2: "running" is set when the engine process starts, before its textures load.
    def mk(status, server, renderer="opengl"):
        s = _mk(renderer=renderer, display_server=server)
        session.update(s.id, lambda r: setattr(r, "status", status))
    mk("starting", "weston")
    mk("running", "weston")
    mk("running", "xvfb", renderer="vulkan")
    mk("starting", "xvfb")                      # software only: no video memory
    mk("starting", "xvfb")                      # (three times: fewer let a flipped test pass)
    mk("running", "xvfb")
    mk("exited", "weston")                      # gone: its memory is back
    mk("failed", "xvfb", renderer="vulkan")
    assert launcher._live_gpu_sessions() == 3


def test_an_impossible_explicit_weston_creates_no_session(tmp_runtime, monkeypatch):
    # Hermetic host: no weston installed, so the explicit ask is refused before any folder.
    with pytest.raises(HarnessError) as e:
        launcher.launch(LaunchRequest(binary="/bin/true", display="weston"))
    assert e.value.code == EXIT_USAGE
    assert session.list_sessions() == []


def test_the_cli_takes_display():
    assert cli.build_parser().parse_args(["launch"]).display == "auto"
    assert cli.build_parser().parse_args(["launch", "--display", "xvfb"]).display == "xvfb"
    with pytest.raises(HarnessError) as e:        # the harness parser raises, never exits
        cli.build_parser().parse_args(["launch", "--display", "wayland"])
    assert e.value.code == EXIT_USAGE


def test_the_mcp_launch_tool_passes_display():
    # Read from the source: the unit tier may not have the mcp package (test_network_default).
    src = (Path(__file__).resolve().parents[1] / "mcp" / "server.py").read_text()
    start = src.index("def launch(")
    body = src[start:src.index("\n@", start)]
    assert "display: str | None = None" in body
    assert '_opt("display", display)' in body


class _Proc:
    """Stand-in display process: records terminate/wait in a shared log."""

    def __init__(self, name, pid, log):
        self.name, self.pid, self.log = name, pid, log

    def terminate(self):
        self.log.append(("terminate", self.name))

    def kill(self):
        self.log.append(("kill", self.name))

    def wait(self, timeout=None):
        self.log.append(("wait", self.name))
        return 0


def test_main_on_a_weston_record_records_and_ends_both_processes(tmp_runtime, sigmask,
                                                                 monkeypatch):
    # QA TA-1/CR-11/SL-6: no main() test ran with a Weston record; the pids were written but
    # never read, and nothing checked that both processes were ended.
    s = _mk()
    session.update(s.id, lambda r: setattr(r, "display_server", display.WESTON))
    log = []
    procs = [_Proc("Xwayland", 5001, log), _Proc("weston", 5002, log)]
    monkeypatch.setattr(runner, "_start_weston", lambda w, h, run: (":9", procs))
    monkeypatch.setattr(runner, "_start_xvfb", lambda w, h: pytest.fail("started Xvfb"))
    seen = {}

    def renderer_string(x_display, env):
        seen.update(display=x_display, soft=env.get("LIBGL_ALWAYS_SOFTWARE"))
        return "NVIDIA GeForce RTX 5080/PCIe/SSE2"
    monkeypatch.setattr(display, "gl_renderer", renderer_string)
    monkeypatch.setattr(runner, "_run_engine", lambda sid, env, started: 0)
    assert runner.main(s.id) == 0
    rec = session.load(s.id)
    assert (rec.display, rec.xwayland_pid, rec.weston_pid) == (":9", 5001, 5002)
    assert rec.xvfb_pid is None
    assert rec.gl_renderer == "NVIDIA GeForce RTX 5080/PCIe/SSE2"
    assert seen == {"display": ":9", "soft": None}      # read with the engine's (GPU) env
    assert log == [("terminate", "Xwayland"), ("wait", "Xwayland"),
                   ("terminate", "weston"), ("wait", "weston")]


def test_main_reads_no_renderer_string_for_vulkan(tmp_runtime, sigmask, monkeypatch):
    s = _mk(renderer="vulkan")
    session.update(s.id, lambda r: setattr(r, "display_server", display.WESTON))
    procs = [_Proc("Xwayland", 1, []), _Proc("weston", 2, [])]
    monkeypatch.setattr(runner, "_start_weston", lambda w, h, run: (":9", procs))
    monkeypatch.setattr(display, "gl_renderer", lambda *a: pytest.fail("read the renderer"))
    monkeypatch.setattr(runner, "_run_engine", lambda sid, env, started: 0)
    assert runner.main(s.id) == 0
    assert session.load(s.id).gl_renderer is None


def test_main_refuses_a_sandbox_record_that_names_weston(tmp_runtime, sigmask, monkeypatch):
    # QA SA-1: only the launcher kept sandbox sessions off Weston; the runner trusted the record.
    s = _mk()

    def mark(r):
        r.network, r.display_server = "sandbox", display.WESTON
    session.update(s.id, mark)
    monkeypatch.setattr(runner, "_start_weston", lambda *a: pytest.fail("started Weston"))
    monkeypatch.setattr(runner, "_start_xvfb", lambda *a: pytest.fail("started Xvfb"))
    assert runner.main(s.id) == 1
    rec = session.load(s.id)
    assert rec.status == "failed"
    assert rec.exit_info["error"] == "a sandbox session cannot use the Weston display"


def test_record_display_writes_the_xvfb_pid(tmp_runtime):
    s = _mk()
    runner._record_display(s, display.XVFB, ":4", [_Proc("Xvfb", 6001, [])], {})
    rec = session.load(s.id)
    assert (rec.display, rec.xvfb_pid, rec.weston_pid) == (":4", 6001, None)


def test_a_runner_failure_is_the_launch_error(tmp_runtime, monkeypatch):
    # QA XFA-8: a display that did not start was reported as "the engine exited" with an empty
    # engine log, because only sandbox sessions read the runner's recorded cause.
    s = _mk()
    runner._fail(s.id, "weston: Weston exited with code 1 before opening its socket; see x.log")
    err = launcher._early_exit_error(s.id, HarnessError(EXIT_REFUSED, "engine exited", "hint"))
    assert err.code == launcher.EXIT_LAUNCH
    assert err.message == ("the session could not start: weston: Weston exited with code 1 "
                           "before opening its socket; see x.log")
