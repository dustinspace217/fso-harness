"""Which virtual display a Linux session gets (fsoctl/display.py) and how the runner starts Weston.

Every branch of the choice: asked for Xvfb, Weston available, Weston's programs missing, no GPU
render node, the sandbox network, video memory (low, unmeasured, reserved for sessions still
starting), Vulkan, and an explicit --display weston that cannot be honoured. Then the runner's
Weston start and stop with stand-in processes.

The host probes (_has_program, _has_render_node, _free_video_memory_mib, gl_renderer) are stubbed
for every unit test by conftest's hermetic_display fixture; the tests of the probes themselves
call the real functions, captured here at import time, before any fixture runs.
"""
import os
import stat
import subprocess
import time

import pytest

from fsoctl import display, envsetup, runner
from fsoctl.errors import EXIT_REFUSED, EXIT_USAGE, HarnessError

_REAL_HAS_PROGRAM = display._has_program
_REAL_HAS_RENDER_NODE = display._has_render_node
_REAL_FREE_VIDEO_MEMORY = display._free_video_memory_mib
_REAL_GL_RENDERER = display.gl_renderer


@pytest.fixture
def weston_ready(monkeypatch):
    """A machine with weston, Xwayland and a GPU render node."""
    monkeypatch.setattr(display, "_has_program", lambda name: True)
    monkeypatch.setattr(display, "_has_render_node", lambda: True)
    monkeypatch.setattr(display, "_free_video_memory_mib", lambda: 12000)


def test_xvfb_when_asked(weston_ready):
    assert display.choose("xvfb", "real") == ("xvfb", None)


def test_weston_by_default_when_everything_is_there(weston_ready):
    assert display.choose("auto", "real") == ("weston", None)
    assert display.choose("auto", "private") == ("weston", None)
    assert display.choose("weston", "real") == ("weston", None)


@pytest.mark.parametrize("missing", ["weston", "Xwayland"])
def test_xvfb_with_a_reason_when_a_program_is_missing(monkeypatch, missing):
    monkeypatch.setattr(display, "_has_program", lambda name: name != missing)
    monkeypatch.setattr(display, "_has_render_node", lambda: True)
    server, note = display.choose("auto", "real")
    assert server == "xvfb"
    assert note.startswith("Xvfb (software OpenGL): ")
    assert f"{missing} not installed" in note
    assert "dnf install weston xorg-x11-server-Xwayland" in note


def test_both_missing_programs_are_named(monkeypatch):
    monkeypatch.setattr(display, "_has_program", lambda name: False)
    monkeypatch.setattr(display, "_has_render_node", lambda: True)
    _, note = display.choose("auto", "real")
    assert "weston and Xwayland not installed" in note


def test_xvfb_with_a_reason_without_a_render_node(monkeypatch):
    monkeypatch.setattr(display, "_has_program", lambda name: True)
    monkeypatch.setattr(display, "_has_render_node", lambda: False)
    server, note = display.choose("auto", "real")
    assert server == "xvfb"
    assert "no GPU render node (/dev/dri/renderD*)" in note


def test_the_sandbox_always_gets_xvfb(weston_ready):
    server, note = display.choose("auto", "sandbox")
    assert server == "xvfb"
    assert "the sandbox network uses Xvfb only" in note


@pytest.mark.parametrize("network,programs,node", [
    ("sandbox", True, True),     # the sandbox uses Xvfb only
    ("real", False, True),       # not installed
    ("real", True, False),       # no render node
])
def test_an_explicit_weston_that_cannot_run_is_refused(monkeypatch, network, programs, node):
    monkeypatch.setattr(display, "_has_program", lambda name: programs)
    monkeypatch.setattr(display, "_has_render_node", lambda: node)
    monkeypatch.setattr(display, "_free_video_memory_mib", lambda: 12000)
    with pytest.raises(HarnessError) as e:
        display.choose("weston", network)
    assert e.value.code == EXIT_USAGE
    assert e.value.message.startswith("--display weston is not possible here: ")
    assert "drop --display" in e.value.hint


def test_an_unknown_display_is_refused(weston_ready):
    with pytest.raises(HarnessError) as e:
        display.choose("wayland", "real")
    assert e.value.code == EXIT_USAGE
    assert "'wayland'" in e.value.message
    assert "auto" in e.value.hint and "weston" in e.value.hint and "xvfb" in e.value.hint


def test_low_video_memory_falls_back_to_xvfb_when_automatic(weston_ready, monkeypatch):
    monkeypatch.setattr(display, "_free_video_memory_mib", lambda: 1200)
    server, note = display.choose("auto", "real")
    assert server == "xvfb"
    assert note == (f"Xvfb (software OpenGL): only 1200 MiB of video memory free (one more GPU "
                    f"session needs {display.MIN_FREE_VIDEO_MEMORY_MIB} MiB plus "
                    f"{display.LIVE_SESSION_RESERVE_MIB} MiB per running one)")


def test_low_video_memory_refuses_an_explicit_weston(weston_ready, monkeypatch):
    monkeypatch.setattr(display, "_free_video_memory_mib", lambda: 1200)
    with pytest.raises(HarnessError) as e:
        display.choose("weston", "real")
    assert e.value.code == EXIT_USAGE


def test_exactly_the_floor_keeps_weston(weston_ready, monkeypatch):
    monkeypatch.setattr(display, "_free_video_memory_mib",
                        lambda: display.MIN_FREE_VIDEO_MEMORY_MIB)
    assert display.choose("auto", "real") == ("weston", None)
    monkeypatch.setattr(display, "_free_video_memory_mib",
                        lambda: display.MIN_FREE_VIDEO_MEMORY_MIB - 1)
    assert display.choose("auto", "real")[0] == "xvfb"


def test_unmeasurable_video_memory_keeps_weston_and_says_so(weston_ready, monkeypatch):
    monkeypatch.setattr(display, "_free_video_memory_mib", lambda: None)
    assert display.choose("auto", "real") == (
        "weston", "video memory not checked (no nvidia-smi answer)")


def test_video_memory_is_read_once_per_choice(weston_ready, monkeypatch):
    # QA CR-2/SA-5/SL-8: it was read twice, up to 20 s, and the two answers could disagree.
    calls = []
    monkeypatch.setattr(display, "_free_video_memory_mib", lambda: calls.append(1) or None)
    display.choose("auto", "real")
    assert len(calls) == 1


def test_video_memory_is_not_read_when_weston_cannot_run_anyway(monkeypatch):
    calls = []
    monkeypatch.setattr(display, "_has_program", lambda name: False)
    monkeypatch.setattr(display, "_has_render_node", lambda: True)
    monkeypatch.setattr(display, "_free_video_memory_mib", lambda: calls.append(1) or 12000)
    display.choose("auto", "real")
    assert calls == []


def test_live_gpu_sessions_are_charged_a_reserve(weston_ready, monkeypatch):
    # QA SL-2/CR-7, XFA-2/XFK-2: back-to-back launches all read the same free amount before any of
    # them had loaded its textures, so all of them passed the floor.
    free = display.MIN_FREE_VIDEO_MEMORY_MIB + display.LIVE_SESSION_RESERVE_MIB
    monkeypatch.setattr(display, "_free_video_memory_mib", lambda: free)
    assert display.choose("auto", "real", "opengl", 1) == ("weston", None)
    server, note = display.choose("auto", "real", "opengl", 2)
    assert server == "xvfb"
    assert f"only {free} MiB of video memory free with 2 GPU session(s) running" in note


@pytest.mark.parametrize("requested", ["auto", "weston", "xvfb"])
def test_vulkan_short_of_video_memory_is_refused(weston_ready, monkeypatch, requested):
    # Owner, 2026-11-20: "refuse it. We need to make sure to not crash users' computers." A Vulkan
    # session draws on the GPU on any display, so moving it to Xvfb would save nothing.
    monkeypatch.setattr(display, "_free_video_memory_mib", lambda: 1200)
    with pytest.raises(HarnessError) as e:
        display.choose(requested, "real", "vulkan")
    assert e.value.code == EXIT_REFUSED
    assert e.value.message.startswith("not enough video memory for a Vulkan session: only 1200")
    assert "launch with OpenGL" in e.value.hint


def test_vulkan_with_enough_video_memory_is_not_refused(weston_ready):
    assert display.choose("auto", "real", "vulkan") == ("weston", None)
    assert display.choose("xvfb", "real", "vulkan") == ("xvfb", None)


def test_a_vulkan_fallback_does_not_claim_software_opengl(monkeypatch):
    monkeypatch.setattr(display, "_has_program", lambda name: False)
    monkeypatch.setattr(display, "_has_render_node", lambda: True)
    _, note = display.choose("auto", "real", "vulkan")
    assert note == ("Xvfb (Vulkan still on the GPU): weston and Xwayland not installed "
                    "(install with: sudo dnf install weston xorg-x11-server-Xwayland); "
                    "video memory not checked (no nvidia-smi answer)")


def test_vulkan_short_of_memory_is_refused_when_weston_is_missing(monkeypatch):
    # QA XFA-5/XFK-3: a missing Weston skipped the memory read, though Vulkan uses the GPU.
    monkeypatch.setattr(display, "_has_program", lambda name: False)
    monkeypatch.setattr(display, "_has_render_node", lambda: True)
    monkeypatch.setattr(display, "_free_video_memory_mib", lambda: 1200)
    with pytest.raises(HarnessError) as e:
        display.choose("auto", "real", "vulkan")
    assert e.value.code == EXIT_REFUSED


def test_an_opengl_xvfb_session_reads_no_video_memory(monkeypatch):
    monkeypatch.setattr(display, "_free_video_memory_mib", lambda: pytest.fail("read memory"))
    assert display.choose("xvfb", "real", "opengl") == ("xvfb", None)


class _Proc:
    """A stand-in Popen that records its arguments and how it was ended. `alive` False makes
    poll() report an exit (code 1); `stuck` makes wait() time out until killed."""

    def __init__(self, argv, log=None, alive=True, stuck=False, **kw):
        self.argv, self.kw = argv, kw
        self.pid = 4242
        self.ended = None
        self.alive, self.stuck = alive, stuck
        self.log = log if log is not None else []

    def poll(self):
        return None if self.alive else 1

    def terminate(self):
        self.ended = "terminate"
        self.log.append(("terminate", self.argv[0]))

    def kill(self):
        self.ended = "kill"
        self.log.append(("kill", self.argv[0]))

    def wait(self, timeout=None):
        self.log.append(("wait", self.argv[0]))
        self.timeouts = [*getattr(self, "timeouts", []), timeout]
        if self.stuck and self.ended != "kill":
            raise subprocess.TimeoutExpired(self.argv[0], timeout)
        return 0


def _fake_popen(tmp_path, started, opens_socket=True, weston_alive=True, xwayland_error=None):
    """Return a Popen stand-in: Weston (optionally) opens its socket; Xwayland may raise."""
    def popen(argv, **kw):
        if argv[0] == "Xwayland" and xwayland_error is not None:
            raise xwayland_error
        p = _Proc(argv, alive=weston_alive or argv[0] != "weston", **kw)
        started.append(p)
        if argv[0] == "weston" and opens_socket:
            (tmp_path / "weston-runtime" / "wayland-fsoh").touch()
        return p
    return popen


def test_weston_start_ends_weston_when_xwayland_reports_no_display(monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(tmp_path, started))
    monkeypatch.setattr(runner, "_read_display", lambda rfd, timeout: b"")
    with pytest.raises(RuntimeError, match="Xwayland did not report a display"):
        runner._start_weston(320, 240, tmp_path)
    assert [p.argv[0] for p in started] == ["weston", "Xwayland"]
    assert all(p.ended == "kill" for p in started)


def test_weston_start_gives_up_when_weston_never_opens_its_socket(monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(tmp_path, started, opens_socket=False))
    monkeypatch.setattr(runner, "_WESTON_READY_TIMEOUT", 0.2)
    with pytest.raises(RuntimeError, match="did not open its socket within") as e:
        runner._start_weston(320, 240, tmp_path)
    assert str(tmp_path / "weston.log") in str(e.value)
    assert [p.argv[0] for p in started] == ["weston"]
    assert started[0].ended == "kill"


def test_a_socket_that_appears_later_is_waited_for(monkeypatch, tmp_path):
    # The success tests open the socket inside Popen, so the wait loop's body never ran.
    polls = []

    class Slow(_Proc):
        """Weston that opens its socket on its third poll."""
        def poll(self):
            polls.append(1)
            if len(polls) == 3:
                (tmp_path / "weston-runtime" / "wayland-fsoh").touch()
    monkeypatch.setattr(subprocess, "Popen", Slow)
    monkeypatch.setattr(runner, "_read_display", lambda rfd, timeout: b"3\n")
    t0 = time.monotonic()
    assert runner._start_weston(320, 240, tmp_path)[0] == ":3"
    assert len(polls) == 3
    assert time.monotonic() - t0 < 0.9                     # polled every 0.05 s, not once a second


def test_a_weston_that_exits_at_once_is_reported_at_once(monkeypatch, tmp_path):
    # QA CR-4/SL-5: the wait never polled Weston, so a dead Weston cost the whole timeout.
    started = []
    monkeypatch.setattr(subprocess, "Popen",
                        _fake_popen(tmp_path, started, opens_socket=False, weston_alive=False))
    monkeypatch.setattr(runner, "_WESTON_READY_TIMEOUT", 30.0)
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="exited with code 1") as e:
        runner._start_weston(320, 240, tmp_path)
    assert time.monotonic() - t0 < 2.0
    assert str(tmp_path / "weston.log") in str(e.value)


@pytest.mark.parametrize("error", [OSError("no Xwayland"), runner._Terminated()])
def test_weston_is_ended_when_anything_after_it_fails(monkeypatch, tmp_path, error):
    # QA TA-2/CR-3/SL-4: an OSError from Xwayland's start, or a stop landing during the waits,
    # left Weston running until the scope stopped.
    started = []
    monkeypatch.setattr(subprocess, "Popen",
                        _fake_popen(tmp_path, started, xwayland_error=error))
    pipes = []
    real_pipe = os.pipe

    def pipe():
        pipes.append(real_pipe())
        return pipes[-1]
    monkeypatch.setattr(runner.os, "pipe", pipe)
    with pytest.raises(type(error)):
        runner._start_weston(320, 240, tmp_path)
    assert [p.argv[0] for p in started] == ["weston"]
    assert started[0].ended == "kill"
    for fd in pipes[0]:                                   # QA XFA-7: the read end leaked
        with pytest.raises(OSError):
            os.fstat(fd)


def test_weston_start_returns_the_display_and_both_processes(monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(tmp_path, started))
    reads = []

    def read_display(rfd, timeout):
        reads.append((rfd, timeout))
        return b"17\n"
    monkeypatch.setattr(runner, "_read_display", read_display)
    (tmp_path / "weston-runtime").mkdir(mode=0o700)       # a folder left from an earlier start
    disp, procs = runner._start_weston(320, 240, tmp_path)
    assert disp == ":17"
    assert [p.argv[0] for p in procs] == ["Xwayland", "weston"]   # stop order: X first
    weston, xwayland = started
    total_h = 240 + runner._XVFB_EXTRA_HEIGHT
    assert weston.argv == ["weston", "--no-config", "--backend=headless", "--renderer=gl",
                           "--socket=wayland-fsoh", "--width=320", f"--height={total_h}",
                           "--idle-time=0", f"--log={tmp_path / 'weston.log'}"]
    assert xwayland.argv[:4] == ["Xwayland", "-fullscreen", "-geometry", f"320x{total_h}"]
    assert xwayland.argv[4] == "-displayfd" and xwayland.argv[-2:] == ["-nolisten", "tcp"]
    assert xwayland.kw["pass_fds"] == (int(xwayland.argv[5]),)
    assert len(reads) == 1 and isinstance(reads[0][0], int)
    assert reads[0][1] == runner._XVFB_READY_TIMEOUT
    for p in started:
        assert p.kw["stdout"] is subprocess.DEVNULL and p.kw["stderr"] is subprocess.DEVNULL


def test_weston_and_xwayland_get_a_private_runtime_folder_and_no_desktop(monkeypatch, tmp_path):
    # QA TA-6/SA-3: nothing checked the environment the two programs start with.
    for var in (*envsetup.DESKTOP_SESSION_VARS, "DISPLAY", "DBUS_SESSION_BUS_ADDRESS"):
        monkeypatch.setenv(var, "desktop")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    started = []
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(tmp_path, started))
    monkeypatch.setattr(runner, "_read_display", lambda rfd, timeout: b"17\n")
    runner._start_weston(320, 240, tmp_path)
    rt = tmp_path / "weston-runtime"
    assert stat.S_IMODE(os.stat(rt).st_mode) == 0o700
    weston_env, xwayland_env = (p.kw["env"] for p in started)
    for env in (weston_env, xwayland_env):
        assert env["XDG_RUNTIME_DIR"] == str(rt)
        assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={rt / 'no-bus'}"
        assert "DISPLAY" not in env
        for var in envsetup.DESKTOP_SESSION_VARS:
            if var != "WAYLAND_DISPLAY":
                assert var not in env
    assert "WAYLAND_DISPLAY" not in weston_env
    assert xwayland_env["WAYLAND_DISPLAY"] == "wayland-fsoh"


def test_start_display_picks_the_server(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner, "_start_weston",
                        lambda w, h, run: calls.append(("weston", w, h, run))
                        or (":5", ["xw", "we"]))
    monkeypatch.setattr(runner, "_start_xvfb",
                        lambda w, h: calls.append(("xvfb", w, h)) or (":6", "xvfb"))
    assert runner._start_display(display.WESTON, 320, 240, tmp_path) == (":5", ["xw", "we"])
    assert runner._start_display(display.XVFB, 320, 240, tmp_path) == (":6", ["xvfb"])
    assert calls == [("weston", 320, 240, tmp_path), ("xvfb", 320, 240)]
    monkeypatch.setattr(runner, "_start_xvfb", lambda w, h: (":6", None))
    assert runner._start_display(display.XVFB, 320, 240, tmp_path) == (":6", [])


def test_stop_display_lets_xwayland_go_before_asking_weston():
    # QA TA-7/CR-5/SL-7: the order was claimed but both got SIGTERM back to back.
    log = []
    a, b = _Proc(["Xwayland"], log), _Proc(["weston"], log)
    runner._stop_display([a, b])
    assert log == [("terminate", "Xwayland"), ("wait", "Xwayland"),
                   ("terminate", "weston"), ("wait", "weston")]
    for p in (a, b):
        assert 0.0 < p.timeouts[0] <= runner._DISPLAY_STOP_TIMEOUT


def test_stop_display_shares_one_bound_between_the_processes(monkeypatch):
    monkeypatch.setattr(runner, "_DISPLAY_STOP_TIMEOUT", 0.3)
    p = _Proc(["weston"])
    runner._stop_display([p])
    assert 0.0 < p.timeouts[0] <= 0.3


def test_stop_display_kills_a_process_stuck_past_the_bound(monkeypatch):
    monkeypatch.setattr(runner, "_DISPLAY_STOP_TIMEOUT", 0.1)
    log = []
    stuck = _Proc(["weston"], log, stuck=True)
    runner._stop_display([stuck])
    assert log == [("terminate", "weston"), ("wait", "weston"), ("kill", "weston")]


def test_software_opengl_is_dropped_only_under_weston():
    env = {"LIBGL_ALWAYS_SOFTWARE": "1", "PATH": "/usr/bin"}
    assert display.gl_env(env, display.XVFB) == env
    assert display.gl_env(env, display.WESTON) == {"PATH": "/usr/bin"}
    assert env == {"LIBGL_ALWAYS_SOFTWARE": "1", "PATH": "/usr/bin"}    # a copy, not in place
    assert display.gl_env({"PATH": "/usr/bin"}, display.WESTON) == {"PATH": "/usr/bin"}


def test_has_program_asks_path(monkeypatch):
    monkeypatch.setattr(display.shutil, "which",
                        lambda name: "/usr/bin/weston" if name == "weston" else None)
    assert _REAL_HAS_PROGRAM("weston") is True
    assert _REAL_HAS_PROGRAM("Xwayland") is False


def test_has_render_node_looks_for_render_nodes(monkeypatch):
    seen = []
    monkeypatch.setattr(display.glob, "glob", lambda pattern: seen.append(pattern) or [])
    assert _REAL_HAS_RENDER_NODE() is False
    monkeypatch.setattr(display.glob, "glob", lambda pattern: ["/dev/dri/renderD128"])
    assert _REAL_HAS_RENDER_NODE() is True
    assert seen == ["/dev/dri/renderD*"]


@pytest.mark.parametrize("out,want", [
    ("11519\n", 11519),
    ("11519\n4000\n", 4000),           # several GPUs: the one with the least free memory
    ("garbage\n", None),
    ("", None),
])
def test_free_video_memory_reads_nvidia_smi(monkeypatch, out, want):
    seen = {}

    def run(argv, **kw):
        seen.update(argv=argv, **kw)
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
    monkeypatch.setattr(display, "_has_program", lambda name: name == "nvidia-smi")
    monkeypatch.setattr(subprocess, "run", run)
    assert _REAL_FREE_VIDEO_MEMORY() == want
    assert seen["argv"] == ["nvidia-smi", "--query-gpu=memory.free",
                            "--format=csv,noheader,nounits"]
    assert seen["timeout"] == display._QUERY_TIMEOUT and seen["check"] is False
    assert seen["capture_output"] is True and seen["text"] is True


@pytest.mark.parametrize("error", [OSError("gone"), subprocess.TimeoutExpired("nvidia-smi", 10)])
def test_free_video_memory_is_unknown_when_nvidia_smi_fails(monkeypatch, error):
    def run(argv, **kw):
        raise error
    monkeypatch.setattr(display, "_has_program", lambda name: True)
    monkeypatch.setattr(subprocess, "run", run)
    assert _REAL_FREE_VIDEO_MEMORY() is None


def test_free_video_memory_is_unknown_without_nvidia_smi(monkeypatch):
    monkeypatch.setattr(display, "_has_program", lambda name: False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("ran nvidia-smi"))
    assert _REAL_FREE_VIDEO_MEMORY() is None


@pytest.mark.parametrize("out,want", [
    ("name of display: :5\nOpenGL renderer string: NVIDIA GeForce RTX 5080/PCIe/SSE2\n",
     "NVIDIA GeForce RTX 5080/PCIe/SSE2"),
    ("OpenGL renderer string: llvmpipe (LLVM 20.1.8, 256 bits)\n",
     "llvmpipe (LLVM 20.1.8, 256 bits)"),
    ("OpenGL renderer string:   \n", None),
    ("OpenGL renderer string: Test GPU (driver: x 1.0: y)\n", "Test GPU (driver: x 1.0: y)"),
    ("Error: unable to open display :5\n", None),
])
def test_gl_renderer_reads_glxinfo_with_the_engine_environment(monkeypatch, out, want):
    seen = {}

    def run(argv, **kw):
        seen.update(argv=argv, **kw)
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
    monkeypatch.setattr(display, "_has_program", lambda name: name == "glxinfo")
    monkeypatch.setattr(subprocess, "run", run)
    engine_env = {"LIBGL_ALWAYS_SOFTWARE": "1", "DISPLAY": ":0"}
    assert _REAL_GL_RENDERER(":5", engine_env) == want
    assert seen["argv"] == ["glxinfo", "-B"]
    assert seen["env"] == {"LIBGL_ALWAYS_SOFTWARE": "1", "DISPLAY": ":5"}
    assert seen["timeout"] == display._QUERY_TIMEOUT and seen["check"] is False
    assert seen["capture_output"] is True and seen["text"] is True
    assert engine_env["DISPLAY"] == ":0"                         # the caller's env is untouched


@pytest.mark.parametrize("error", [OSError("gone"), subprocess.TimeoutExpired("glxinfo", 10)])
def test_gl_renderer_is_unknown_when_glxinfo_fails(monkeypatch, error):
    def run(argv, **kw):
        raise error
    monkeypatch.setattr(display, "_has_program", lambda name: True)
    monkeypatch.setattr(subprocess, "run", run)
    assert _REAL_GL_RENDERER(":5", {}) is None


def test_gl_renderer_is_unknown_without_glxinfo(monkeypatch):
    monkeypatch.setattr(display, "_has_program", lambda name: False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("ran glxinfo"))
    assert _REAL_GL_RENDERER(":5", {}) is None


def test_vulkan_is_not_refused_when_memory_cannot_be_measured(weston_ready, monkeypatch):
    # Vulkan-refuse QA TA-1: an unmeasured GPU is reported, not treated as short.
    monkeypatch.setattr(display, "_free_video_memory_mib", lambda: None)
    assert display.choose("auto", "real", "vulkan") == (
        "weston", "video memory not checked (no nvidia-smi answer)")
    assert display.choose("xvfb", "real", "vulkan") == (
        "xvfb", "video memory not checked (no nvidia-smi answer)")


def test_vulkan_is_charged_the_live_session_reserve(weston_ready, monkeypatch):
    # Vulkan-refuse QA TA-2: every reserve test used OpenGL.
    free = display.MIN_FREE_VIDEO_MEMORY_MIB + display.LIVE_SESSION_RESERVE_MIB
    monkeypatch.setattr(display, "_free_video_memory_mib", lambda: free)
    assert display.choose("auto", "real", "vulkan", 1) == ("weston", None)
    with pytest.raises(HarnessError) as e:
        display.choose("auto", "real", "vulkan", 2)
    assert e.value.code == EXIT_REFUSED


def test_the_memory_refusal_comes_before_the_explicit_weston_refusal(monkeypatch):
    # Vulkan-refuse QA TA-3: Vulkan, explicit weston, Weston missing and low memory: the memory
    # refusal is the one reported (it names the danger; the missing program does not matter).
    monkeypatch.setattr(display, "_has_program", lambda name: False)
    monkeypatch.setattr(display, "_has_render_node", lambda: True)
    monkeypatch.setattr(display, "_free_video_memory_mib", lambda: 1200)
    with pytest.raises(HarnessError) as e:
        display.choose("weston", "real", "vulkan")
    assert e.value.code == EXIT_REFUSED


@pytest.mark.parametrize("live,stop", [(0, False), (1, True)])
def test_the_vulkan_refusal_hint_fits_the_case(weston_ready, monkeypatch, live, stop):
    # Vulkan-refuse QA CR-1: "stop a GPU session" with none running, and OpenGL "falls back" on an
    # explicit weston (it is refused there).
    monkeypatch.setattr(display, "_free_video_memory_mib", lambda: 1200)
    with pytest.raises(HarnessError) as e:
        display.choose("auto", "real", "vulkan", live)
    assert ("stop a GPU session" in e.value.hint) is stop
    assert "launch with OpenGL on --display auto or xvfb" in e.value.hint


def test_an_unmeasured_vulkan_session_on_xvfb_says_so(monkeypatch):
    # Vulkan-refuse QA CR-2: Xvfb paths returned no note when memory could not be read.
    monkeypatch.setattr(display, "_free_video_memory_mib", lambda: None)
    assert display.choose("xvfb", "real", "vulkan") == (
        "xvfb", "video memory not checked (no nvidia-smi answer)")
    _, note = display.choose("auto", "real", "vulkan")          # Weston missing (hermetic host)
    assert note.endswith("; video memory not checked (no nvidia-smi answer)")
    assert display.choose("xvfb", "real", "opengl") == ("xvfb", None)
