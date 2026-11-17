"""The "sandbox" network (fsoctl/sandbox.py; design: the FSO project folder's docs/plans/
2026-11-13-nightly-sandbox.md, stage b).

Three groups:
- argv composition and the helpers, pure (no process started);
- the launcher's refusals: what a sandbox session may not ask for, and that --sound stays
  allowed for every other network (Dustin's trim, 2026-11-13);
- T1/T4 with a HOSTILE stand-in engine: a Python script run by the real runner inside the real
  bwrap, which tries to reach the network, the user's home, the session bus, the desktop X
  socket, the run folder's records, the shared cache and the game data, and writes what
  happened into its prefs folder. The assertions check the HOST side (no canary file anywhere,
  records unchanged), since a write inside the sandbox's private /tmp "succeeds" harmlessly.
  Needs bwrap and Xvfb; skipped without them.
"""
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from fsoctl import cli, launcher, runner, sandbox, session
from fsoctl.errors import EXIT_LAUNCH, EXIT_REFUSED, EXIT_USAGE, HarnessError
from fsoctl.launcher import LaunchRequest

_REPO = Path(__file__).resolve().parent.parent
# The sandbox's command line is prlimit -> keyctl -> bwrap (sandbox.py), so all three must be
# there: a CI runner image without keyutils failed these tests instead of skipping them.
_HAVE_TOOLS = (Path("/usr/bin/bwrap").exists() and Path("/usr/bin/Xvfb").exists()
               and all(shutil.which(tool) for tool in ("keyctl", "prlimit")))


def _sess(tmp_path: Path, **kw) -> session.Session:
    """A sandbox game session whose binary lives in tmp_path/bin and data in tmp_path/fs2."""
    (tmp_path / "bin").mkdir(exist_ok=True)
    (tmp_path / "fs2").mkdir(exist_ok=True)
    base = dict(kind="game", binary=str(tmp_path / "bin" / "fs2_open"),
                fs2path=str(tmp_path / "fs2"), mods=[], mission=None, renderer="opengl",
                width=320, height=240, switches={}, allow_eval=False, extra_args=[],
                network="sandbox", display=":7")
    base.update(kw)
    return session.new_session(**base)


def _pairs(argv: list[str], flag: str) -> list[tuple[str, str]]:
    """Return every (src, dest) that follows `flag` in a bwrap argv."""
    return [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == flag]


# ---- argv composition ----------------------------------------------------------------------

def test_argv_isolates_namespaces_and_clears_the_environment(tmp_runtime, tmp_path):
    s = _sess(tmp_path)
    argv = sandbox.argv(s, {"DISPLAY": ":7"}, 9, 10)
    # AT2-1/SA2-1: prlimit sets the per-file limit (soft = hard); keyctl gives a fresh
    # session keyring; both exec, ending in bwrap.
    assert argv[:8] == ["prlimit", f"--fsize={256 << 20}:{256 << 20}", "--nofile=512:512",
                        "--", "keyctl", "session", "-", "bwrap"]   # SA3-1: 512 open files
    assert argv[-1] == "--"
    for flag in ("--unshare-all", "--unshare-user", "--disable-userns", "--die-with-parent",
                 "--new-session", "--clearenv"):
        assert flag in argv, flag
    assert "--as-pid-1" not in argv          # see sandbox.ENGINE_NS_PID for why
    assert argv[argv.index("--info-fd") + 1] == "9"
    assert argv[argv.index("--seccomp") + 1] == "10"          # seccomp_filter (keyring probe)
    assert argv[argv.index("--chdir") + 1] == s.fs2path


def test_argv_mounts_exactly_this_list(tmp_runtime, tmp_path):
    # TA-2: the whole mount list, literally, so any added bind (a host folder, another X socket,
    # the run folder) fails here instead of passing a forbidden-paths check.
    s = _sess(tmp_path, display=":12")
    argv = sandbox.argv(s, {}, 3, 4)
    run, bindir = session.run_dir(s), str(tmp_path / "bin")
    mounts = argv[argv.index("/usr/bin") + 1:]           # after the fixed PATH setting
    assert mounts == [
        "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib64", "/lib64",
        "--symlink", "usr/lib", "/lib", "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/sbin", "/sbin",
        "--ro-bind-try", "/etc/ld.so.cache", "/etc/ld.so.cache",
        "--ro-bind-try", "/etc/fonts", "/etc/fonts",
        "--ro-bind-try", "/etc/alternatives", "/etc/alternatives",
        "--proc", "/proc", "--dev", "/dev",
        "--ro-bind", "/dev/null", "/proc/keys", "--ro-bind", "/dev/null", "/proc/key-users",
        "--size", str(64 << 20), "--tmpfs", "/tmp",
        "--size", str(64 << 20), "--tmpfs", "/fso-harness/home",
        "--size", str(16 << 20), "--perms", "0700", "--tmpfs", "/fso-harness/xdg-runtime",
        "--size", str(512 << 20), "--tmpfs", "/fso-harness/cache",
        "--ro-bind", "/tmp/.X11-unix/X12", "/tmp/.X11-unix/X12",
        "--ro-bind", bindir, bindir,
        "--ro-bind", s.fs2path, s.fs2path,
        "--ro-bind", str(run / "passwd"), "/etc/passwd",
        "--ro-bind", str(run / "group"), "/etc/group",
        # On disk under the sandbox root, never in the run folder on /run/user (AT2-1).
        "--bind", str(tmp_path / "sandbox-runs" / s.id / "prefs"), "/fso-harness/prefs",
        "--remount-ro", "/", "--chdir", s.fs2path, "--"]


def test_argv_passes_only_the_listed_environment(tmp_runtime, tmp_path):
    s = _sess(tmp_path)
    env = {"DISPLAY": ":7", "HOME": "/r/home", "SSH_AUTH_SOCK": "/run/user/1/ssh",
           "GH_TOKEN": "secret", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/r/no-bus",
           "LIBGL_ALWAYS_SOFTWARE": "1"}
    set_vars = dict(_pairs(sandbox.argv(s, env, 3, 4), "--setenv"))
    assert set_vars == {"PATH": "/usr/bin", "DISPLAY": ":7", "HOME": "/r/home",
                        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/r/no-bus",
                        "LIBGL_ALWAYS_SOFTWARE": "1"}


def test_env_allowlist_is_exactly_these_names():
    # TA-4: pinned literally, not checked against itself.
    assert sandbox._ENV_KEYS == (
        "DISPLAY", "SDL_VIDEODRIVER", "GDK_BACKEND", "QT_QPA_PLATFORM",
        "SDL_VIDEO_ALLOW_SCREENSAVER", "LIBGL_ALWAYS_SOFTWARE", "FSO_PREFERENCES_PATH", "HOME",
        "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")


def test_argv_uses_the_shadow_folder_read_only_as_working_folder(tmp_runtime, tmp_path):
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    argv = sandbox.argv(_sess(tmp_path, cwd=str(shadow)), {}, 3, 4)
    assert (str(shadow), str(shadow)) in _pairs(argv, "--ro-bind")
    assert argv[argv.index("--chdir") + 1] == str(shadow)


@pytest.mark.parametrize("display", ["", ":", ":0.0", ":x", "0", ":1/../X0"])
def test_x_socket_refuses_anything_but_a_display_number(display):
    with pytest.raises(HarnessError) as e:
        sandbox.x_socket(display)
    assert e.value.code == EXIT_LAUNCH


def test_mod_targets_binds_symlinked_mods_outside_fs2path(tmp_path):
    fs2, lib = tmp_path / "fs2", tmp_path / "library"
    (fs2 / "Inside").mkdir(parents=True)
    (lib / "Outside-1.0").mkdir(parents=True)
    (fs2 / "Outside").symlink_to(lib / "Outside-1.0")
    (fs2 / "Dangling").symlink_to(lib / "gone")
    got = sandbox.mod_targets(str(fs2), ["Inside,Outside", "Dangling", "Missing", ""])
    assert got == [str((lib / "Outside-1.0").resolve())]


@pytest.mark.parametrize("code, want", [(0, 0), (1, 1), (128, 128), (129, -1), (134, -6),
                                        (139, -11), (143, -15), (192, -64), (193, 193),
                                        (-9, -9), (None, None)])
def test_exit_code_turns_bwrap_signal_codes_back_into_signals(code, want):
    assert sandbox.exit_code(code) == want


def test_sandbox_env_points_every_folder_inside():
    env = {"XDG_CACHE_HOME": "/home/u/.cache/fso-harness/cache", "HOME": "/r/home",
           "DISPLAY": ":7"}
    assert sandbox.sandbox_env(env) is env
    assert env == {"DISPLAY": ":7", "FSO_PREFERENCES_PATH": "/fso-harness/prefs/",
                   "HOME": "/fso-harness/home",
                   "XDG_CONFIG_HOME": "/fso-harness/home/.config",
                   "XDG_DATA_HOME": "/fso-harness/home/.local/share",
                   "XDG_STATE_HOME": "/fso-harness/home/.local/state",
                   "XDG_CACHE_HOME": "/fso-harness/cache",
                   "XDG_RUNTIME_DIR": "/fso-harness/xdg-runtime",
                   "DBUS_SESSION_BUS_ADDRESS": "unix:path=/fso-harness/no-bus"}


# ---- bwrap's info pipe and the engine lookup -------------------------------------------------

def _pipe_with(data: bytes, close: bool = True) -> tuple[int, int | None]:
    """Return (read end, write end or None if closed) of a pipe holding `data`."""
    r, w = os.pipe()
    os.write(w, data)
    if close:
        os.close(w)
        return r, None
    return r, w


@pytest.mark.parametrize("data, want", [(b'{"child-pid": 4242, "net-namespace": 1}', 4242),
                                        (b"", None), (b"not json", None), (b"[1]", None),
                                        (b'{"child-pid": "4242"}', None),
                                        (b'{"child-pid": 0}', None),
                                        (b'{"child-pid": 1}', 1)])
def test_read_info(data, want):
    r, _ = _pipe_with(data)
    try:
        assert sandbox.read_info(r, 2.0) == want
    finally:
        os.close(r)


def test_read_info_gives_up_on_a_silent_open_pipe():
    r, w = _pipe_with(b"", close=False)
    try:
        start = time.monotonic()
        assert sandbox.read_info(r, 0.3) is None
        assert time.monotonic() - start < 2
    finally:
        os.close(r)
        os.close(w)


def test_find_engine_returns_the_inits_child_and_its_pidfd():
    init = subprocess.Popen(["sh", "-c", "sleep 30 & wait"])
    try:
        pid, fd = sandbox.find_engine(init.pid, lambda: init.poll() is None, 5.0)
        try:
            assert Path(f"/proc/{pid}/cmdline").read_bytes().startswith(b"sleep")
            assert Path(f"/proc/self/fdinfo/{fd}").read_text().count(f"Pid:\t{pid}") == 1
        finally:
            os.close(fd)
        assert sandbox.find_engine(init.pid, lambda: False, 5.0) is None   # bwrap ended
    finally:
        init.kill()
        init.wait(timeout=5)
    assert sandbox.find_engine(init.pid, lambda: True, 1.0) is None       # init gone


def test_find_engine_gives_up_when_no_child_appears():
    init = subprocess.Popen(["sleep", "30"])
    try:
        start = time.monotonic()
        assert sandbox.find_engine(init.pid, lambda: True, 0.3) is None
        assert time.monotonic() - start < 2
    finally:
        init.kill()
        init.wait(timeout=5)


class _Bwrap:
    """Popen stand-in recording what the adapter did to it."""
    def __init__(self, code=None):
        self.calls, self.code = [], code

    def wait(self, timeout=None):
        self.calls.append(("wait", timeout))
        return self.code

    def terminate(self):
        self.calls.append("terminate")

    def kill(self):
        self.calls.append("kill")


def _no_bare_pid_kill(monkeypatch):
    """Make any os.kill from sandbox.py fail the test: the stop path must use the pidfd."""
    def refuse(pid, sig):
        raise AssertionError(f"os.kill({pid}, {sig}) on the sandbox stop path")
    monkeypatch.setattr(sandbox.os, "kill", refuse)


def test_engine_adapter_signals_the_engine_through_its_pidfd(monkeypatch):
    # TA-3: a real child, signalled through the pidfd find_engine would hand over.
    _no_bare_pid_kill(monkeypatch)
    child = subprocess.Popen(["sleep", "30"])
    proc = _Bwrap(code=139)
    eng = sandbox.Engine(proc, child.pid, os.pidfd_open(child.pid))
    eng.terminate()
    assert child.wait(timeout=5) == -signal.SIGTERM and proc.calls == []
    assert eng.wait(timeout=3) == -11 and proc.calls == [("wait", 3)]
    assert eng.pidfd is None                                   # closed once bwrap is done
    eng.kill()
    assert proc.calls[-1] == "kill"


def test_a_reaped_engines_pidfd_signals_nothing(monkeypatch):
    # SA-4/AT-4/CR-4: once the engine is gone and reaped, its number may belong to another
    # process; the pidfd still names the dead one, so terminate() reaches no one.
    _no_bare_pid_kill(monkeypatch)
    child = subprocess.Popen(["true"])
    fd = os.pidfd_open(child.pid)
    child.wait(timeout=5)
    proc = _Bwrap()
    sandbox.Engine(proc, child.pid, fd).terminate()           # ProcessLookupError, suppressed
    assert proc.calls == []                                   # TA2-3: bwrap was not signalled
    os.close(fd)


def test_engine_adapter_without_an_engine_terminates_bwrap():
    proc = _Bwrap()
    sandbox.Engine(proc, None, None).terminate()
    assert proc.calls == ["terminate"]


def test_window_owner_prefers_the_window_pid(tmp_runtime, tmp_path):
    s = _sess(tmp_path)
    assert session.window_owner(s) == 0
    s.engine_pid = 900
    assert session.window_owner(s) == 900
    s.window_pid = sandbox.ENGINE_NS_PID
    assert session.window_owner(s) == 2


def test_engine_argv_never_falls_back_to_the_private_prefix(tmp_runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "build_argv", lambda sess: ["fs2", "-nosound"])
    s = _sess(tmp_path)
    with pytest.raises(HarnessError):
        runner.engine_argv(s)
    got = runner.engine_argv(s, {"DISPLAY": ":7"}, 5, 6)
    assert got[7] == "bwrap" and got[-3:] == ["--", "fs2", "-nosound"]
    assert "unshare" not in got


# ---- launcher refusals ------------------------------------------------------------------------

def _req(tmp_path: Path, **kw) -> LaunchRequest:
    binary = tmp_path / "fs2_open"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    return LaunchRequest(binary=str(binary), network="sandbox", **kw)


@pytest.mark.parametrize("kw, code, word", [
    ({"renderer": "vulkan"}, EXIT_USAGE, "OpenGL"),
    ({"kind": "qtfred"}, EXIT_USAGE, "game sessions"),
    ({"sound": True}, EXIT_REFUSED, "--sound")])
def test_sandbox_refuses_what_it_cannot_do(tmp_runtime, tmp_path, kw, code, word):
    with pytest.raises(HarnessError) as e:
        launcher._validate(_req(tmp_path, **kw))
    assert e.value.code == code and word in e.value.message


@pytest.mark.parametrize("network", [None, "real", "private"])
def test_sound_is_still_allowed_outside_the_sandbox(tmp_runtime, tmp_path, network):
    # Dustin's trim (2026-11-13): refusing sound applies to sandboxed sessions only.
    req = _req(tmp_path, sound=True)
    req.network = network
    launcher._validate(req)


@pytest.mark.parametrize("tool, package", [("bwrap", "bubblewrap"), ("prlimit", "util-linux"),
                                           ("keyctl", "keyutils")])
def test_sandbox_needs_its_tools(tmp_runtime, tmp_path, monkeypatch, tool, package):
    monkeypatch.setattr(launcher.shutil, "which",
                        lambda name: None if name == tool else "/usr/bin/" + name)
    with pytest.raises(HarnessError) as e:
        launcher._validate(_req(tmp_path))
    assert e.value.code == EXIT_LAUNCH
    assert e.value.message == f"cannot make a sandbox: `{tool}` not found"
    assert e.value.hint == f"install {package} (Fedora: dnf install {package})"


def test_sandbox_refuses_root(tmp_runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda tool: "/usr/bin/" + tool)
    monkeypatch.setattr(launcher.os, "geteuid", lambda: 0)
    with pytest.raises(HarnessError) as e:
        launcher._validate(_req(tmp_path))
    assert e.value.code == EXIT_REFUSED and "root" in e.value.message


def test_a_plain_sandbox_request_passes(tmp_runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda tool: "/usr/bin/" + tool)
    launcher._validate(_req(tmp_path))


def test_a_match_session_cannot_ask_for_the_sandbox(tmp_runtime, tmp_path, monkeypatch):
    class Match:
        network = "private"
    monkeypatch.setattr(launcher.match, "load", lambda mid: Match())
    with pytest.raises(HarnessError) as e:
        launcher._check_network(_req(tmp_path, match="m0a1b2c3d", role="client"))
    assert "match's network" in e.value.message


def test_cli_accepts_the_sandbox_network():
    args = cli.build_parser().parse_args(["launch", "--network", "sandbox"])
    assert args.network == "sandbox"


# ---- T1/T4: a hostile stand-in engine inside the real sandbox ---------------------------------

_HOSTILE = r'''#!/usr/bin/python3
"""Stand-in for an engine built from a hostile pull request (runs INSIDE the sandbox)."""
import json, os, signal, socket, subprocess, sys, time
cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "cfg.json")))
out = {}

def attempt(name, fn):
    try:
        out[name] = ["ok", repr(fn())[:200]]
    except Exception as e:
        out[name] = ["fail", type(e).__name__ + ": " + str(e)[:150]]

def connect_inet():
    s = socket.create_connection(("1.1.1.1", 443), timeout=3); s.close(); return "connected"

def connect_unix(path):
    s = socket.socket(socket.AF_UNIX); s.settimeout(3); s.connect(path); s.close()
    return "connected"

def write(path):
    with open(path, "w") as f:
        f.write("CANARY")
    return "written"

def fill(path, mib):
    with open(path, "wb") as f:
        for _ in range(mib):
            f.write(b"x" * (1 << 20))
    return "filled"

out["interfaces"] = sorted(l.split(":")[0].strip()
                           for l in open("/proc/net/dev").read().splitlines()[2:])
attempt("inet_connect", connect_inet)
out["env"] = sorted(os.environ)
out["x11_sockets"] = sorted(os.listdir("/tmp/.X11-unix"))
for p in cfg["must_be_missing"]:
    out["exists:" + p] = os.path.exists(p)
attempt("read_gh_hosts", lambda: open(cfg["gh_hosts"]).read())
attempt("x_desktop", lambda: connect_unix("/tmp/.X11-unix/X0"))
attempt("x_session", lambda: connect_unix("/tmp/.X11-unix/X" + os.environ["DISPLAY"][1:]))
attempt("session_bus", lambda: connect_unix(cfg["bus"]))
for name, path in cfg["writes"].items():
    attempt("write:" + name, lambda path=path: write(path))
attempt("nested_userns", lambda: subprocess.run(["unshare", "--user", "true"],
                                                capture_output=True, timeout=5).returncode)
attempt("keyring", lambda: subprocess.run(["keyctl", "show", "@u"], capture_output=True,
                                          text=True, timeout=5).stdout.strip()[:100])
def print_key(kid):
    r = subprocess.run(["keyctl", "print", kid], capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return r.stdout.strip()

for name, kid in cfg.get("keys", {}).items():
    attempt("key_" + name, lambda kid=kid: print_key(kid))
attempt("proc_keys", lambda: open("/proc/keys").read())
attempt("root_write", lambda: write("/CANARY"))
attempt("tmp_fill", lambda: fill("/tmp/fill", 70))
attempt("home_fill", lambda: fill(os.path.join(os.environ["HOME"], "fill"), 70))
for p in ("/tmp/fill", os.path.join(os.environ["HOME"], "fill")):
    if os.path.exists(p):
        os.unlink(p)
big = os.path.join(os.environ["FSO_PREFERENCES_PATH"], "big")
r = subprocess.run(["dd", "if=/dev/zero", "of=" + big, "bs=1M", "count=300"],
                   capture_output=True, timeout=60)
out["file_limit"] = [r.returncode, os.path.getsize(big)]
os.unlink(big)
out["pid_inside"] = os.getpid()
out["etc_passwd"] = open("/etc/passwd").read()
prefs = os.environ["FSO_PREFERENCES_PATH"]
tmp = os.path.join(prefs, "probe.json.tmp")
with open(tmp, "w") as f:
    json.dump(out, f)
os.replace(tmp, os.path.join(prefs, "probe.json"))
time.sleep(120)
'''


def _hostile_session(tmp_runtime: Path, tmp_path: Path) -> tuple[session.Session, dict]:
    """Create the hostile stand-in, its game folder (one in-tree mod, one symlinked mod) and a
    sandbox session for it; return (session, the stand-in's config)."""
    bindir, fs2, lib = tmp_path / "bin", tmp_path / "fs2", tmp_path / "library"
    for d in (bindir, fs2 / "MVPS", lib / "Linked-1.0"):
        d.mkdir(parents=True)
    (fs2 / "Linked").symlink_to(lib / "Linked-1.0")
    exe = bindir / "fs2_open"
    exe.write_text(_HOSTILE)
    exe.chmod(0o755)
    s = session.new_session(kind="game", binary=str(exe), fs2path=str(fs2),
                            mods=["MVPS", "Linked"], mission=None, renderer="opengl",
                            width=320, height=240, switches={}, allow_eval=False, extra_args=[],
                            network="sandbox")
    run, home, uid = session.run_dir(s), Path.home(), os.getuid()
    cache = home / ".cache" / "fso-harness" / "cache"
    cfg = {"must_be_missing": [str(home), str(home / ".ssh"), f"/run/user/{uid}", "/run/user",
                               "/run", "/home", "/mnt", "/sys", "/dev/dri", "/dev/snd",
                               "/dev/input", str(run), str(run / "session.json"),
                               str(run / "runner.log"), str(cache)],
           "gh_hosts": str(home / ".config" / "gh" / "hosts.yml"),
           "bus": f"/run/user/{uid}/bus",
           "writes": {"fs2path": str(fs2 / "CANARY"), "mod": str(fs2 / "MVPS" / "CANARY"),
                      "linked_mod": str(lib / "Linked-1.0" / "CANARY"),
                      "bindir": str(bindir / "CANARY"),
                      "session_json": str(run / "session.json"),
                      "journal": str(run / "journal.jsonl"),
                      "shared_cache": str(cache / "CANARY"),
                      "home": str(home / "fso-sandbox-CANARY")}}
    return s, cfg


def _add_canary_keys() -> dict:
    """Add a canary user key to @s and to @u of this process; return {"s": id, "u": id}."""
    out = {}
    for ring in ("s", "u"):
        r = subprocess.run(["keyctl", "add", "user", f"fsoh-canary-{ring}",
                            f"CANARY-KEY-{ring}", f"@{ring}"],
                           capture_output=True, text=True, timeout=5, check=True)
        out[ring] = r.stdout.strip()
    return out


def _run_hostile(tmp_runtime: Path, tmp_path: Path, runtime: str) -> tuple:
    """Run the hostile stand-in through the real runner and bwrap until it has written its
    probe; stop it; return (probe output, cfg, record while running, final record, run folder,
    session.json text before the run). A production-root run folder is removed afterwards."""
    s, cfg = _hostile_session(tmp_runtime, tmp_path)
    run = session.run_dir(s)
    before = (run / "session.json").read_text()
    # Canary keys in this process's session and user keyrings, which the runner and bwrap
    # inherit; the stand-in tries to print them (stage-a builder's keyring probe).
    cfg["keys"] = _add_canary_keys() if shutil.which("keyctl") else {}
    # The runner's own environment carries a fake secret; --clearenv must keep it out.
    env = {**os.environ, "PYTHONPATH": str(_REPO), "GH_TOKEN": "canary-secret"}
    proc = None
    try:
        (Path(s.binary).parent / "cfg.json").write_text(json.dumps(cfg))
        proc = subprocess.Popen([sys.executable, "-m", "fsoctl.runner", s.id], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        probe = session.prefs_dir(s) / "probe.json"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not probe.exists():
            time.sleep(0.2)
        assert probe.exists(), (run / "engine.log").read_text()[-2000:]
        rec = session.load(s.id)
        out = json.loads(probe.read_text())
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=40) == 143
    finally:
        if proc is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
        for kid in cfg["keys"].values():
            subprocess.run(["keyctl", "unlink", kid], capture_output=True, timeout=5, check=False)
        final = session.load(s.id)
        # AT2-1: the engine's writable folder is on disk, never in the run folder on /run/user.
        out_host = {"run_has_prefs": (run / "prefs").exists(),
                    "prefs": str(session.prefs_dir(final))}
        if runtime == "production":
            shutil.rmtree(run)
    out["host"] = out_host
    (tmp_runtime.parent / f"hostile-probe-{runtime}.json").write_text(json.dumps(out, indent=1))
    return out, cfg, rec, final, run, before


@pytest.mark.skipif(not _HAVE_TOOLS, reason="needs bwrap, keyctl, prlimit and Xvfb")
@pytest.mark.parametrize("runtime", ["temporary", "production"])
def test_hostile_engine_reaches_nothing_outside_its_sandbox(tmp_runtime, tmp_path, monkeypatch,
                                                             runtime):
    # "production" (QA CR-6/TA-5): the real runtime root under /run/user/<uid>, where the run
    # folder lives in normal use; "temporary": the test's own folder under /tmp.
    if runtime == "production":
        monkeypatch.delenv("FSO_HARNESS_RUNTIME")
    out, cfg, rec, final, run, before = _run_hostile(tmp_runtime, tmp_path, runtime)
    # Network: loopback only, and nothing outside reachable.
    assert out["interfaces"] == ["lo"]
    assert out["inet_connect"][0] == "fail"
    # Environment: only what the sandbox sets.
    # PWD is set by bwrap for --chdir; LC_CTYPE by the stand-in's own Python (PEP 538 locale
    # coercion), not taken from the host.
    assert set(out["env"]) <= {"PATH", "PWD", "LC_CTYPE", *sandbox._ENV_KEYS}
    assert "GH_TOKEN" not in out["env"] and "SSH_AUTH_SOCK" not in out["env"]
    # Only this session's X socket; the desktop's and the session bus are unreachable.
    assert out["x11_sockets"] == [Path(sandbox.x_socket(rec.display)).name]
    assert out["x_session"][0] == "ok", out["x_session"]          # its own display works
    # "x_desktop" is display 0, standing for the user's desktop. Where no desktop runs (CI's
    # runner) the session's own Xvfb takes number 0, and reaching display 0 is then right:
    # the socket list above already showed it to be the only one inside. First seen when
    # this file first ran on GitHub's runner (2026-11-16: x_session ok, x_desktop ok).
    assert out["x_desktop"][0] == ("ok" if rec.display == ":0" else "fail")
    assert out["session_bus"][0] == "fail"
    assert out["read_gh_hosts"][0] == "fail"
    for p in cfg["must_be_missing"]:
        assert out["exists:" + p] is False, p
    assert out["nested_userns"] != ["ok", "0"]          # a nested user namespace is refused
    assert out["pid_inside"] == sandbox.ENGINE_NS_PID
    passwd = out["etc_passwd"]           # the one-user file, not the host's
    assert passwd.startswith(f"fso:x:{os.getuid()}:") and passwd.count("\n") == 1
    if Path("/usr/bin/keyctl").exists():
        # The design's open question: the kernel user keyring. A new user namespace does NOT
        # cut it (the canary-key probe below read host keys through plain bwrap); the host
        # user keyring's id must not show inside because key syscalls are filtered.
        host = subprocess.run(["keyctl", "show", "@u"], capture_output=True, text=True,
                              timeout=5, check=False).stdout.split()
        host_id = next((w for w in host if w.isdigit()), None)
        assert host_id and host_id not in out["keyring"][1]
    # Writes: the read-only binds refuse; anything else landed in the sandbox's own tmpfs.
    for name in ("fs2path", "mod", "linked_mod", "bindir"):
        assert out["write:" + name][0] == "fail", name
    for path in cfg["writes"].values():
        if not path.endswith(("session.json", "journal.jsonl")):
            assert not Path(path).exists(), f"canary reached the host: {path}"
    assert not (run / "journal.jsonl").exists()
    # The root is read-only; /tmp and HOME are tmpfs capped at 64 MiB (QA AT-5/CR-3).
    assert out["root_write"][0] == "fail"
    # Kernel keyrings (keyring probes 2026-11-13): no key syscall works inside (seccomp) and
    # /proc/keys cannot be read, so the host's keys and keyring names stay out of reach.
    for ring in cfg["keys"]:
        assert out["key_" + ring][0] == "fail", out["key_" + ring]
        assert "Operation not permitted" in out["key_" + ring][1]
    assert out["keyring"] == ["ok", "'Keyring'"]   # `keyctl show @u`: its header, no keyring ids
    assert out["proc_keys"][0] == "fail" and "Permission denied" in out["proc_keys"][1]
    assert "CANARY-KEY" not in json.dumps(out)
    # Per-file limit (AT2-1/SA2-1): dd past 256 MiB fails and the file stops at the limit.
    assert out["file_limit"][0] != 0 and out["file_limit"][1] <= 256 << 20, out["file_limit"]
    assert out["host"]["run_has_prefs"] is False
    assert not out["host"]["prefs"].startswith("/run/"), out["host"]
    assert out["tmp_fill"][0] == "fail" and "No space left" in out["tmp_fill"][1]
    assert out["home_fill"][0] == "fail" and "No space left" in out["home_fill"][1]
    # The host's session record was not replaced (the runner updated it, never the stand-in).
    assert final.binary == json.loads(before)["binary"] and final.network == "sandbox"
    # The runner found the engine's host pid, recorded window_pid, and stopped it politely
    # (SIGTERM to the engine itself, translated back from bwrap's 143).
    assert rec.engine_pid and rec.window_pid == sandbox.ENGINE_NS_PID
    assert final.ended_by == "signal" and final.exit_info["signal"] == "SIGTERM"


# ---- runner._popen_engine's sandbox branch ------------------------------------------------------

@pytest.fixture
def stand_in_child():
    """A short-lived `sleep` child whose pid stands in for bwrap's (QA TA5-2: never this test
    process's own pid, which a pidfd signal would hit); killed and reaped after the test."""
    child = subprocess.Popen(["sleep", "30"])
    yield child.pid
    child.kill()
    child.wait(timeout=5)


class _FakeBwrap:
    """Popen stand-in for bwrap: records kill(), answers poll() with `code` (None = running).
    Its pid is a real stand-in child's, so the runner's pidfd on "bwrap" opens (TA4-3)."""

    def __init__(self, argv, pid, code=None, **kw):
        self.argv, self.pid, self.code, self.killed = argv, pid, code, False
        self.pass_fds = kw.get("pass_fds")

    def poll(self):
        return self.code

    def kill(self):
        self.killed = True


def _popen_with(monkeypatch, pid, code, init, engine):
    """Run runner._popen_engine for a sandbox session with bwrap, read_info and find_engine
    replaced; return (result or the raised HarnessError, the fake bwrap)."""
    made = []
    monkeypatch.setattr(runner.subprocess, "Popen",
                        lambda argv, **kw: made.append(_FakeBwrap(argv, pid, code, **kw))
                        or made[-1])
    monkeypatch.setattr(runner.sandbox, "read_info", lambda rfd, timeout: init)
    monkeypatch.setattr(runner.sandbox, "find_engine",
                        lambda pid, running, timeout: None if engine is None else (engine, 99))
    monkeypatch.setattr(launcher, "build_argv", lambda sess: ["fs2"])
    return made


def test_popen_engine_wraps_the_found_engine(tmp_runtime, tmp_path, monkeypatch,
                                             stand_in_child):
    made = _popen_with(monkeypatch, stand_in_child, None, 100, 101)
    got = runner._popen_engine(_sess(tmp_path), {"DISPLAY": ":7"}, None)
    assert isinstance(got, sandbox.Engine) and got.pid == 101 and got.proc is made[0]
    assert got.pidfd == 99 and got.bwrap_pidfd is not None     # TA4-3: a pidfd on bwrap
    os.close(got.bwrap_pidfd)
    assert made[0].argv[7] == "bwrap" and len(made[0].pass_fds) == 2 and not made[0].killed


def test_popen_engine_kills_a_sandbox_whose_engine_never_appears(tmp_runtime, tmp_path,
                                                                  monkeypatch, stand_in_child):
    made = _popen_with(monkeypatch, stand_in_child, None, 100, None)
    with pytest.raises(HarnessError) as e:
        runner._popen_engine(_sess(tmp_path), {"DISPLAY": ":7"}, None)
    assert e.value.code == EXIT_LAUNCH and made[0].killed


def test_popen_engine_hands_back_a_sandbox_whose_engine_already_ended(tmp_runtime, tmp_path,
                                                                      monkeypatch, stand_in_child):
    # The engine started and ended at once: the caller's wait() records that exit.
    made = _popen_with(monkeypatch, stand_in_child, 1, 100, None)
    got = runner._popen_engine(_sess(tmp_path), {"DISPLAY": ":7"}, None)
    assert got.pid is None and got.pidfd is None and not made[0].killed
    os.close(got.bwrap_pidfd)


@pytest.mark.parametrize("code", [1, None])
def test_popen_engine_reports_a_sandbox_that_never_started(tmp_runtime, tmp_path, monkeypatch,
                                                            code, stand_in_child):
    # CR-2: bwrap reported no process; the runner records that itself (not from log text).
    made = _popen_with(monkeypatch, stand_in_child, code, None, None)
    with pytest.raises(HarnessError) as e:
        runner._popen_engine(_sess(tmp_path), {"DISPLAY": ":7"}, None)
    assert e.value.code == EXIT_LAUNCH and "sandbox did not start" in e.value.message
    assert made[0].killed


def test_write_identity_names_one_fixed_user(tmp_path):
    sandbox.write_identity(tmp_path)
    uid, gid = os.getuid(), os.getgid()
    assert (tmp_path / "passwd").read_text() == \
        f"fso:x:{uid}:{gid}:fso-harness sandbox:/fso-harness/home:/sbin/nologin\n"
    assert (tmp_path / "group").read_text() == f"fso:x:{gid}:\n"


# ---- tests added for the mutation pass (each names the survivor it kills) ---------------------

def test_argv_keeps_every_bind_with_mods_and_a_shadow_folder(tmp_runtime, tmp_path):
    # _binds mutants 20/24: "out +=" turned into "out =" dropped every earlier bind silently.
    lib, shadow = tmp_path / "library" / "Linked-1.0", tmp_path / "shadow"
    lib.mkdir(parents=True)
    shadow.mkdir()
    s = _sess(tmp_path, cwd=str(shadow), mods=["Linked"])
    (Path(s.fs2path) / "Linked").symlink_to(lib)
    ro = _pairs(sandbox.argv(s, {}, 3, 4), "--ro-bind")
    for path in ("/tmp/.X11-unix/X7", str(tmp_path / "bin"), s.fs2path, str(lib.resolve()),
                 str(shadow)):
        assert (path, path) in ro, path


def test_x_socket_error_names_the_display_and_the_fix():
    # x_socket mutants 8/11/13: the message and hint were not pinned.
    with pytest.raises(HarnessError) as e:
        sandbox.x_socket(":x")
    assert e.value.message == "session display ':x' is not :N"
    assert e.value.hint == "the sandbox binds one X socket by number; relaunch the session"


def test_mod_targets_skips_an_empty_name_and_keeps_going(tmp_path):
    # mod_targets mutant 9: "continue" turned into "break" stopped at the first empty name.
    fs2, lib = tmp_path / "fs2", tmp_path / "library"
    fs2.mkdir()
    (lib / "Outside-1.0").mkdir(parents=True)
    (fs2 / "Outside").symlink_to(lib / "Outside-1.0")
    assert sandbox.mod_targets(str(fs2), [",Outside"]) == [str((lib / "Outside-1.0").resolve())]


def test_read_info_works_with_less_than_a_second_left():
    # read_info mutant 11: "left <= 1" gave up with data waiting when the timeout was short.
    r, _ = _pipe_with(b'{"child-pid": 7}')
    try:
        assert sandbox.read_info(r, 0.5) == 7
    finally:
        os.close(r)


def test_read_info_joins_a_reply_split_across_reads():
    # read_info mutant 30: "data = chunk" kept only the last piece of a reply bwrap wrote in parts.
    r, w = os.pipe()

    def write_in_two_parts():
        os.write(w, b'{"child-pid": ')
        time.sleep(0.2)
        os.write(w, b"4242}")
        os.close(w)
    t = threading.Thread(target=write_in_two_parts)
    t.start()
    try:
        assert sandbox.read_info(r, 3.0) == 4242
    finally:
        t.join(5)
        os.close(r)


# ---- SA-3: pilots; SA-5: mod names per comma part ---------------------------------------------

@pytest.mark.parametrize("mods", [["MVPS,.."], ["A,,B"], ["A,/etc"], [".,A"]])
def test_every_comma_part_of_a_mod_entry_must_be_a_plain_name(tmp_runtime, tmp_path, mods):
    with pytest.raises(HarnessError) as e:
        launcher._check_folders(LaunchRequest(binary="/bin/true", mods=mods))
    assert e.value.code == EXIT_USAGE


def test_plain_comma_lists_of_mods_pass(tmp_runtime):
    launcher._check_folders(LaunchRequest(binary="/bin/true", mods=["MVPS-5.0.2,GOTY-1.0.3"]))


@pytest.mark.parametrize("network", [None, "private", "sandbox"])
@pytest.mark.parametrize("name", ["*", "a?", "[x]"])
def test_a_pilot_name_with_glob_characters_is_refused(tmp_runtime, tmp_path, monkeypatch,
                                                      network, name):
    monkeypatch.setattr(launcher.shutil, "which", lambda tool: "/usr/bin/" + tool)
    req = _req(tmp_path, pilot_name=name)
    req.network = network
    with pytest.raises(HarnessError) as e:
        launcher._validate(req)
    assert e.value.code == EXIT_USAGE


def test_a_sandbox_pilot_name_is_letters_digits_dash_underscore(tmp_runtime, tmp_path,
                                                                monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda tool: "/usr/bin/" + tool)
    with pytest.raises(HarnessError) as e:
        launcher._validate(_req(tmp_path, pilot_name="a.b"))
    assert "not allowed in the sandbox" in e.value.message
    launcher._validate(_req(tmp_path, pilot_name="Ab_9-x"))


def test_a_sandbox_pilot_comes_only_from_a_run_folder(tmp_runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda tool: "/usr/bin/" + tool)
    outside = tmp_path / "players"
    outside.mkdir()
    with pytest.raises(HarnessError) as e:
        launcher._validate(_req(tmp_path, pilot_mode="existing", pilot_source=str(outside)))
    assert e.value.code == EXIT_REFUSED
    inside = tmp_runtime / "s0a1b2c3d" / "prefs" / "data" / "players"
    inside.mkdir(parents=True)
    launcher._validate(_req(tmp_path, pilot_mode="existing", pilot_source=str(inside)))


def _replay_players(tmp_runtime: Path, tmp_path: Path) -> tuple[Path, Path]:
    """An earlier session's players folder (inside the runtime root) and a canary secret."""
    players = tmp_runtime / "s0a1b2c3d" / "prefs" / "data" / "players"
    players.mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("CANARY-SECRET")
    return players, secret


def test_replay_copy_skips_a_planted_link_and_copies_the_plain_file(tmp_runtime, tmp_path):
    players, secret = _replay_players(tmp_runtime, tmp_path)
    (players / "X.json").symlink_to(secret)
    (players / "X.0.csg").write_bytes(b"csg")
    dest = tmp_path / "dest"
    launcher._copy_pilot(players, "X", dest)
    got = {p.name: p.read_bytes() for p in (dest / "data" / "players").iterdir()}
    assert got == {"X.0.csg": b"csg"}


def test_replay_with_a_glob_name_and_a_planted_link_copies_nothing(tmp_runtime, tmp_path):
    # Phase B (test seat): pilot_name "*" plus a link at players/X.json.
    players, secret = _replay_players(tmp_runtime, tmp_path)
    (players / "X.json").symlink_to(secret)
    dest = tmp_path / "dest"
    with pytest.raises(HarnessError) as e:
        launcher._copy_pilot(players, "*", dest)
    assert "no pilot files" in e.value.message and not dest.exists()


def test_replay_copy_from_a_link_into_a_run_folder_is_still_strict(tmp_runtime, tmp_path):
    players, secret = _replay_players(tmp_runtime, tmp_path)
    (players / "X.json").symlink_to(secret)
    alias = tmp_path / "alias"
    alias.symlink_to(players)
    with pytest.raises(HarnessError):
        launcher._copy_pilot(alias, "X", tmp_path / "dest")


def test_a_user_named_pilot_folder_outside_run_folders_may_use_links(tmp_runtime, tmp_path):
    # Phase B refinement b: the user's own folder is trusted, links and all.
    real = tmp_path / "real.json"
    real.write_bytes(b"{}")
    folder = tmp_path / "mine"
    folder.mkdir()
    (folder / "X.json").symlink_to(real)
    launcher._copy_pilot(folder, "X", tmp_path / "dest")
    assert (tmp_path / "dest" / "data" / "players" / "X.json").read_bytes() == b"{}"


# ---- CR-2: advice for a sandbox session never comes from its log or names another network -----

def test_a_sandbox_early_exit_ignores_the_logs_words(tmp_runtime, tmp_path):
    s = _sess(tmp_path)
    s.exit_info = {"error": "engine: the sandbox did not start (bwrap reported no process)"}
    session.save(s)
    (session.run_dir(s) / "engine.log").write_text("unshare: launch with --network real\n")
    err = launcher._early_exit_error(s.id, HarnessError(7, "exited", "hint"))
    assert err.code == EXIT_LAUNCH
    assert err.message.endswith("the sandbox did not start (bwrap reported no process)")
    assert "private network" not in err.message and "--network" not in err.hint.split("(")[0]
    assert "engine.log (the build's own output, and bwrap's)" in err.hint


def test_a_sandbox_early_exit_without_a_runner_reason_keeps_the_code(tmp_runtime, tmp_path):
    s = _sess(tmp_path)
    (session.run_dir(s) / "engine.log").write_text("x" * 100_000 + "\nlast line\n")
    err = launcher._early_exit_error(s.id, HarnessError(6, "exited", "hint"))
    assert err.code == 6 and err.message == "the sandboxed engine ended before it was ready"
    assert err.hint.endswith(" | last line")          # tail read, at most 64 KiB


@pytest.mark.parametrize("kw", [{"renderer": "vulkan"}, {"kind": "qtfred"}, {"sound": True},
                                {"pilot_name": "a.b"}])
def test_sandbox_refusals_never_suggest_another_network(tmp_runtime, tmp_path, monkeypatch, kw):
    monkeypatch.setattr(launcher.shutil, "which", lambda tool: "/usr/bin/" + tool)
    with pytest.raises(HarnessError) as e:
        launcher._validate(_req(tmp_path, **kw))
    assert "network" not in e.value.hint


def test_mod_targets_never_binds_a_parent_or_a_path(tmp_path):
    # mod_targets mutants 8/12/13: ".." or a name with "/" would bind a folder beside FS2PATH.
    fs2 = tmp_path / "fs2"
    fs2.mkdir()
    (tmp_path / "beside").mkdir()
    assert sandbox.mod_targets(str(fs2), ["..", "../beside", "."]) == []


# ---- mutation pass: exact refusal texts and pilot-copy edges ------------------------------------

_SANDBOX_REFUSALS = [
    ({"kind": "qtfred"}, EXIT_USAGE, "only game sessions can run in the sandbox",
     "qtFRED does not run sandboxed"),
    ({"renderer": "vulkan"}, EXIT_USAGE, "the sandbox has no GPU: OpenGL only", "drop --vulkan"),
    ({"pilot_name": "a.b"}, EXIT_USAGE, "pilot name 'a.b' is not allowed in the sandbox",
     "use letters, digits, _ and - only"),
    ({"sound": True}, EXIT_REFUSED, "--sound is refused in the sandbox: it loads the build's "
     "OpenAL library in a host process", "drop --sound")]


@pytest.mark.parametrize("case", _SANDBOX_REFUSALS)
def test_sandbox_refusal_texts(tmp_runtime, tmp_path, monkeypatch, case):
    kw, code, message, hint = case
    monkeypatch.setattr(launcher.shutil, "which", lambda tool: "/usr/bin/" + tool)
    with pytest.raises(HarnessError) as e:
        launcher._check_sandbox(_req(tmp_path, **kw))
    assert (e.value.code, e.value.message, e.value.hint) == (code, message, hint)


def test_sandbox_pilot_source_refusal_text(tmp_runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda tool: "/usr/bin/" + tool)
    with pytest.raises(HarnessError) as e:
        launcher._check_sandbox(_req(tmp_path, pilot_source=str(tmp_path)))
    assert (e.value.code, e.value.message, e.value.hint) == (
        EXIT_REFUSED, "a sandbox session takes a pilot only from a harness run folder",
        "use pilot create:<name>, or replay a session")


def test_replay_copy_takes_only_the_pilots_plain_files_after_a_link(tmp_runtime, tmp_path):
    players, secret = _replay_players(tmp_runtime, tmp_path)
    (players / "X.0.csg").symlink_to(secret)            # sorted first: a link must not stop it
    (players / "X.json").write_bytes(b"{}")
    (players / "Y.json").write_bytes(b"other pilot")
    (players / "X.txt").write_bytes(b"not a pilot file")
    dest = tmp_path / "dest"
    (dest / "data" / "players").mkdir(parents=True)     # an existing folder is fine
    launcher._copy_pilot(players, "X", dest)
    assert sorted(p.name for p in (dest / "data" / "players").iterdir()) == ["X.json"]


def test_a_players_folder_replaced_by_a_link_cannot_be_read(tmp_runtime, tmp_path):
    players, _ = _replay_players(tmp_runtime, tmp_path)
    players.rmdir()
    players.symlink_to(tmp_path)
    with pytest.raises(HarnessError) as e:
        launcher._copy_pilot(players, "X", tmp_path / "dest")
    assert e.value.code == EXIT_LAUNCH
    assert e.value.message.startswith(f"cannot read the pilot folder {players}: ")
    assert e.value.hint == "point --pilot-source at a data/players folder"


def test_no_pilot_files_text(tmp_runtime, tmp_path):
    players, _ = _replay_players(tmp_runtime, tmp_path)
    with pytest.raises(HarnessError) as e:
        launcher._copy_pilot(players, "X", tmp_path / "dest")
    assert e.value.hint == "point --pilot-source at a data/players folder that has NAME.json"



# ---- AT2-1/SA2-1: the prefs total cap ends the session, loudly -------------------------------

_FILLER = r'''#!/usr/bin/python3
"""Stand-in engine that writes 1 MiB files into its prefs folder until stopped (bounded)."""
import os, time
prefs = os.environ["FSO_PREFERENCES_PATH"]
for i in range(64):
    with open(os.path.join(prefs, "fill-%02d" % i), "wb") as f:
        f.write(os.urandom(1 << 20))
time.sleep(120)
'''


@pytest.mark.skipif(not _HAVE_TOOLS, reason="needs bwrap, keyctl, prlimit and Xvfb")
def test_the_watchdog_ends_a_session_whose_prefs_outgrow_the_cap(tmp_runtime, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (tmp_path / "fs2").mkdir()
    exe = bindir / "fs2_open"
    exe.write_text(_FILLER)
    exe.chmod(0o755)
    s = session.new_session(kind="game", binary=str(exe), fs2path=str(tmp_path / "fs2"),
                            mods=[], mission=None, renderer="opengl", width=320, height=240,
                            switches={}, allow_eval=False, extra_args=[], network="sandbox")
    cap = 8 << 20
    env = {**os.environ, "PYTHONPATH": str(_REPO), "FSO_HARNESS_SANDBOX_PREFS_MAX": str(cap)}
    # stderr to runner.log, as scope.start does: warn_loudly writes its line there.
    with open(session.run_dir(s) / "runner.log", "ab") as runner_log:
        proc = subprocess.Popen([sys.executable, "-m", "fsoctl.runner", s.id], env=env,
                                stdout=runner_log, stderr=runner_log, start_new_session=True)
    try:
        rc = proc.wait(timeout=60)              # the watchdog ends it; nothing else would
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
    final = session.load(s.id)
    log = (session.run_dir(s) / "runner.log").read_text()
    assert final.ended_by == "disk_limit", (rc, final.ended_by, final.exit_info, log[-500:])
    # SA3-2: killed at once (no SIGTERM grace); AT3-2: prefs emptied after the kill.
    assert final.exit_info["signal"] == "SIGKILL", final.exit_info
    assert list(session.prefs_dir(s).iterdir()) == []
    assert "WARNING fso-harness: session " + s.id in log
    assert f"over the {cap}-byte cap; killing the sandboxed engine" in log


def test_watchdog_check_reports_bytes_and_entries(tmp_path, monkeypatch):
    prefs = tmp_path / "prefs"
    (prefs / "a").mkdir(parents=True)
    (prefs / "a" / "f").write_bytes(b"x" * 20000)
    dog = sandbox.Watchdog("s0", prefs, None)
    monkeypatch.setenv("FSO_HARNESS_SANDBOX_PREFS_MAX", "1000000")
    assert dog.check() is None
    monkeypatch.setenv("FSO_HARNESS_SANDBOX_PREFS_MAX", "4096")
    assert dog.check().endswith("over the 4096-byte cap")
    monkeypatch.setattr(sandbox, "PREFS_MAX_ENTRIES", 1)
    assert "cannot be measured within bounds: more than 1 entries" in dog.check()
    class Exited:
        def exited(self):
            return True
    assert sandbox.Watchdog("s0", tmp_path / "gone", Exited()).check() is None
    assert sandbox.Watchdog("s0", tmp_path / "gone", None).check() == (
        f"prefs folder {tmp_path / 'gone'} is missing while the engine runs")


@pytest.mark.parametrize("raw, want", [("", 1 << 30), ("abc", 1 << 30), ("0", 1 << 30),
                                       ("-5", 1 << 30), ("4096", 4096)])
def test_prefs_cap_falls_back_to_the_default(monkeypatch, raw, want):
    monkeypatch.setenv("FSO_HARNESS_SANDBOX_PREFS_MAX", raw)
    assert sandbox.prefs_max_bytes() == want


def test_a_trip_records_logs_kills_waits_and_empties(tmp_runtime, tmp_path, monkeypatch):
    # SA3-2/AT3-2/CR3-5: record disk_limit first, log, SIGKILL (no SIGTERM), wait, empty prefs.
    s = _sess(tmp_path)
    prefs = session.prefs_dir(s)
    (prefs / "junk").write_text("x")
    calls = []

    class Eng:
        alive = True

        def terminate(self):
            calls.append("terminate")

        def kill(self):
            calls.append("kill")
            self.alive = False

        def exited(self):
            return not self.alive
    monkeypatch.setattr(sandbox, "warn_loudly", calls.append)
    dog = sandbox.Watchdog(s.id, prefs, Eng())
    dog.trip("too big")
    assert calls == [f"session {s.id}: too big; killing the sandboxed engine", "kill"]
    assert dog.tripped == "too big" and session.load(s.id).ended_by == "disk_limit"
    assert list(prefs.iterdir()) == []


def test_cleanup_removes_a_sandbox_prefs_folder_an_engine_locked(tmp_runtime, tmp_path,
                                                                  monkeypatch):
    # AT2-2: chmod 000 on a prefs subfolder and on prefs itself used to make rmtree fail and
    # leave the space used; both the on-disk prefs folder and the run folder go.
    s = _sess(tmp_path, status="exited")
    prefs = session.prefs_dir(s)
    (prefs / "data" / "players").mkdir(parents=True)
    (prefs / "data" / "players" / "p.json").write_text("{}")
    os.chmod(prefs / "data", 0)
    os.chmod(prefs, 0)
    monkeypatch.setattr(launcher.scope, "is_gone", lambda unit: True)
    monkeypatch.setattr(launcher, "_cleanup_sinks", lambda kept, failed: {})
    out = launcher.cleanup()
    assert s.id in out["removed"] and out["failed"] == []
    assert not prefs.parent.exists() and not session.run_dir(s).exists()


def test_find_engine_drops_a_pid_that_stopped_being_the_inits_child(monkeypatch):
    # find_engine_18: between reading the children list and opening the pidfd, the engine can
    # exit and its number go to another process; the re-check must close that pidfd and give up.
    answers = iter([["4242"], []])
    closed = []
    monkeypatch.setattr(sandbox, "_children", lambda init: next(answers))
    monkeypatch.setattr(sandbox, "_open_pidfd", lambda pid: 77)      # TA3-2: module wrappers,
    monkeypatch.setattr(sandbox, "_close_fd", closed.append)          # not os process-wide
    assert sandbox.find_engine(1, lambda: True, 5.0) is None
    assert closed == [77]


def test_a_user_named_pilot_keeps_its_times_and_mode(tmp_runtime, tmp_path):
    # CR2-3: a pilot from the user's own folder is copied with copy2 (contents, mode, times).
    folder = tmp_path / "mine"
    folder.mkdir()
    src = folder / "X.json"
    src.write_bytes(b"{}")
    os.chmod(src, 0o600)
    os.utime(src, (1_000_000_000, 1_000_000_000))
    launcher._copy_pilot(folder, "X", tmp_path / "dest")
    got = (tmp_path / "dest" / "data" / "players" / "X.json").stat()
    assert got.st_mtime == 1_000_000_000 and got.st_mode & 0o777 == 0o600
