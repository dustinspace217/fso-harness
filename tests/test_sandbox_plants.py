"""TA-1 and Phase B: a hostile engine plants links and FIFOs where the HOST reads and writes.

A stand-in engine runs inside the real bwrap (through the real runner) and, inside its prefs
folder, puts a symbolic link (one run) or a FIFO (the other run) at every name the harness
reads or writes there: the bridge's ready.json, popups.txt, command temp files and reply files,
the debug log, and the pilot files a replay copies. Links point at host canaries under
/tmp/claude/ (a secret to leak and a victim to overwrite) or at /dev/zero (memory exhaustion).
Then the host side does what an agent would: bridge commands, status, log, the log-wait cursor;
after the stop: crash_report, validate's findings, and a replay's pilot copy. They run in a
child process under a 2 GiB address-space limit with a deadline, so a hang or a read of
/dev/zero fails the test instead of the machine.

Pass: the canaries are byte-for-byte unchanged, the secret appears in no output, every call
returns within its bound, and the replay copies nothing. Needs bwrap and Xvfb.
"""
import contextlib
import json
import os
import resource
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fsoctl import session

_REPO = Path(__file__).resolve().parent.parent
# The sandbox's command line is prlimit -> keyctl -> bwrap (sandbox.py), so all three must be
# there: a CI runner image without keyutils failed these tests instead of skipping them.
_HAVE_TOOLS = (Path("/usr/bin/bwrap").exists() and Path("/usr/bin/Xvfb").exists()
               and all(shutil.which(tool) for tool in ("keyctl", "prlimit")))
_AS_LIMIT = 2 << 30            # bytes of address space the host-side calls may use

_PLANTER = r'''#!/usr/bin/python3
"""Stand-in engine: plants a link or a FIFO at every host-touched name in its prefs folder."""
import json, os, sys, time
cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "cfg.json")))
prefs = os.environ["FSO_PREFERENCES_PATH"]
cache = os.path.join(prefs, "fso_harness_bridge", "data", "cache")
players = os.path.join(prefs, "data", "players")
for d in (cache, players):
    os.makedirs(d, exist_ok=True)
plants = {os.path.join(cache, "ready.json"): "/dev/zero",
          os.path.join(cache, "popups.txt"): cfg["secret"],
          os.path.join(prefs, "data", "fs2_open.log"): cfg["secret"],
          os.path.join(players, "Pilot.json"): cfg["secret"],
          os.path.join(players, "Pilot.csg"): "/dev/zero"}
for i in range(1, 7):
    plants[os.path.join(cache, "res-%06d.json" % i)] = cfg["secret"]
    plants[os.path.join(cache, "cmd-%06d.json.tmp" % i)] = cfg["victim"]
    plants[os.path.join(cache, "cmd-%06d.json" % i)] = cfg["victim"]
for path, target in plants.items():
    if cfg["mode"] == "link":
        os.symlink(target, path)
    else:
        os.mkfifo(path)
open(os.path.join(prefs, "planted"), "w").close()
time.sleep(120)
'''

_HOST = r'''
"""Host side: the calls an agent makes, each timed; prints one JSON object."""
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[4])
from fsoctl import channel, crash, game, launcher, logs, record, session, validate
sid, phase, dest = sys.argv[1:4]
out = {}

def timed(name, fn, bound):
    t = time.monotonic()
    try:
        res = repr(fn())               # whole: the secret is searched before any truncation
    except BaseException as e:
        res = type(e).__name__ + ": " + str(getattr(e, "message", e))
    out[name] = {"sec": round(time.monotonic() - t, 2), "bound": bound, "result": res}

sess = session.load(sid)
if phase == "running":
    for i in range(3):
        timed("bridge_command_%d" % i, lambda: channel.Channel(sess).send("state", timeout=2), 10)
    timed("status", lambda: launcher.status(sid), 15)
    timed("log", lambda: logs.read(sid), 10)
    timed("log_wait_cursor", lambda: game._new_log_lines(game._log_start(sess)), 10)
else:
    timed("crash_report", lambda: crash.report(sid), 60)
    timed("validate_findings", lambda: validate._findings(sid), 10)

    def replay_copy():
        req = record.launch_request_for(sess)
        d = Path(dest)
        d.mkdir()
        try:
            launcher._copy_pilot(Path(req.pilot_source), req.pilot_name, d)
        finally:
            out["replay_dest_files"] = sorted(str(p) for p in d.rglob("*") if p.is_file())
    timed("replay_pilot_copy", replay_copy, 10)
print(json.dumps(out))
'''


def _limit_memory() -> None:
    """preexec_fn: cap the child's address space so a read of /dev/zero fails, not the box."""
    resource.setrlimit(resource.RLIMIT_AS, (_AS_LIMIT, _AS_LIMIT))


def _host_calls(tmp_path: Path, sid: str, phase: str) -> dict:
    """Run the host-side calls for `phase` in a memory-capped child (90 s deadline)."""
    script = tmp_path / "host_calls.py"
    script.write_text(_HOST)
    r = subprocess.run([sys.executable, str(script), sid, phase, str(tmp_path / "replay-dest"),
                        str(_REPO)], capture_output=True, text=True, timeout=90, check=False,
                       preexec_fn=_limit_memory, env=os.environ.copy())
    assert r.returncode == 0, r.stderr[-3000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.fixture
def canaries():
    """A secret and a victim file under /tmp/claude/, removed afterwards."""
    root = Path("/tmp/claude") / f"plant-canary-{secrets.token_hex(4)}"
    root.mkdir(parents=True)
    token = f"CANARY-SECRET-{secrets.token_hex(8)}"
    # JSON-shaped (TA2-1): a reply read through a followed link parses as a real result and
    # reaches the output, instead of failing as "not JSON" and hiding the leak.
    (root / "secret.txt").write_text(json.dumps({"ok": True, "result": token}))
    (root / "victim.txt").write_text("VICTIM-ORIGINAL")
    try:
        yield root, token
    finally:
        shutil.rmtree(root)


def _start_planter(tmp_path: Path, mode: str, root: Path) -> tuple[session.Session, object]:
    """Create the planter session and start the real runner for it; return (session, runner)
    once the planter says it is done."""
    bindir, fs2 = tmp_path / "bin", tmp_path / "fs2"
    bindir.mkdir()
    fs2.mkdir()
    exe = bindir / "fs2_open"
    exe.write_text(_PLANTER)
    exe.chmod(0o755)
    (bindir / "cfg.json").write_text(json.dumps({"mode": mode, "secret": str(root / "secret.txt"),
                                                 "victim": str(root / "victim.txt")}))
    s = session.new_session(kind="game", binary=str(exe), fs2path=str(fs2), mods=[],
                            mission=None, renderer="opengl", width=320, height=240,
                            switches={"pilot_mode": "existing", "pilot_name": "Pilot"},
                            allow_eval=False, extra_args=[], network="sandbox")
    env = {**os.environ, "PYTHONPATH": str(_REPO)}
    proc = subprocess.Popen([sys.executable, "-m", "fsoctl.runner", s.id], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    marker = session.prefs_dir(s) / "planted"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.2)
    return s, proc


@pytest.mark.skipif(not _HAVE_TOOLS, reason="needs bwrap, keyctl, prlimit and Xvfb")
@pytest.mark.parametrize("mode", ["link", "fifo"])
def test_planted_links_and_fifos_reach_nothing_on_the_host(tmp_runtime, tmp_path, canaries,
                                                           mode):
    root, token = canaries
    s, proc = _start_planter(tmp_path, mode, root)
    try:
        assert (session.prefs_dir(s) / "planted").exists(), \
            (session.run_dir(s) / "engine.log").read_text()[-2000:]
        running = _host_calls(tmp_path, s.id, "running")
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=40) == 143
        stopped = _host_calls(tmp_path, s.id, "stopped")
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
    calls = {**running, **stopped}
    (tmp_runtime.parent / f"plants-{mode}.json").write_text(json.dumps(calls, indent=1))
    assert (root / "secret.txt").read_text() == json.dumps({"ok": True, "result": token})
    assert (root / "victim.txt").read_text() == "VICTIM-ORIGINAL"
    assert sorted(p.name for p in root.iterdir()) == ["secret.txt", "victim.txt"]
    assert token not in json.dumps(calls)
    for name, call in calls.items():
        if isinstance(call, dict):
            assert call["sec"] <= call["bound"], (name, call)
    assert "no pilot files" in calls["replay_pilot_copy"]["result"]
    assert calls["replay_dest_files"] == []
    assert "Unsafe" not in calls["status"]["result"]      # status reports, it does not fail
