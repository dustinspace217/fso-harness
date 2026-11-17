"""The "sandbox" network on the real engine (design: the FSO project folder's docs/plans/
2026-11-13-nightly-sandbox.md, probes T4 and T5).

T4 (isolation with the real engine): a real build plays a retail mission inside the sandbox; the
harness must still screenshot it, send it keys, read its state and stop it; the engine's view of
the filesystem is read from the HOST through /proc/<pid>/root (no code inside the sandbox needed);
the engine must sit in the session's scope cgroup, so the scope's memory cap still applies, and a
stand-in that outgrows a small memory cap must be OOM-killed and recorded as such. The hostile
probes themselves (writes, connects, reads) run in tests/test_sandbox.py with a stand-in engine,
since a hostile engine build would need a modified FSO tree.

T5 (rendering parity): the same build, mission and stage switches, OpenGL, run on the private
network and in the sandbox; screenshots compared with the harness's own compare metric, against a
private-vs-private baseline of the same build (the engine has no fixed timestep, so two runs of
one build already differ a little).

Run: FS2PATH=<game> FSO_BIN=<master Release build> FSO_TEST_MODS=<mods>
     bounded-run pytest -m engine -p no:xdist tests/test_sandbox_engine.py
"""
import json
import os
import shutil
from pathlib import Path

import pytest

from fsoctl import compare, game, launcher, sandbox, scope, session, xinput, xwin
from fsoctl.launcher import LaunchRequest
from tests.engine_helpers import MISSION, MODS, launch_in_mission

pytestmark = [pytest.mark.engine,
              pytest.mark.skipif(not shutil.which("bwrap"), reason="needs bubblewrap")]
Image = pytest.importorskip("PIL.Image")

# Where the cells' numbers are kept for the report (outside any repo).
_ARTIFACTS = Path(os.environ.get("FSO_SANDBOX_ARTIFACTS",
                                 str(Path.home() / "Claude/fso-artifacts/nightly-sandbox")))
STEPS = [{"wait_for": {"kind": "mission_time", "value": 5, "timeout": 180}},
         {"snapshot": "t5"},
         {"wait_for": {"kind": "mission_time", "value": 15, "timeout": 180}},
         {"snapshot": "t15"}]


def _save(name: str, data: dict) -> None:
    """Write one cell's result as JSON into the artifacts folder."""
    _ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (_ARTIFACTS / name).write_text(json.dumps(data, indent=1, default=str))


def _engine_view(sess: session.Session) -> dict:
    """Read, from the host, what the sandboxed engine's filesystem holds at the paths that
    matter (through /proc/<engine pid>/root, the engine's own root directory)."""
    root = Path(f"/proc/{sess.engine_pid}/root")
    home, uid = Path.home(), os.getuid()
    paths = [str(home), str(home / ".ssh"), str(home / ".cache" / "fso-harness" / "cache"),
             "/home", "/run", f"/run/user/{uid}/bus", f"/run/user/{uid}/pulse", "/sys",
             "/dev/dri", "/dev/snd", "/dev/input", str(session.run_dir(sess)),
             str(session.run_dir(sess) / "session.json"),
             str(session.run_dir(sess) / "runner.log")]
    return {"exists": {p: (root / p.lstrip("/")).exists() for p in paths},
            "x11": sorted(os.listdir(root / "tmp" / ".X11-unix")),
            "fs2path_listed": (root / sess.fs2path.lstrip("/") / "root_fs2.vp").exists()}


def _mount_options(pid: int, path: str) -> str:
    """Return the per-mount options of the mount at `path` in process `pid`'s mount table."""
    for line in Path(f"/proc/{pid}/mountinfo").read_text().splitlines():
        fields = line.split()
        if fields[4] == path:
            return fields[5]
    return ""


def test_real_engine_plays_in_the_sandbox_and_the_harness_still_drives_it(engine_env,
                                                                         tmp_runtime):
    _, fso = engine_env
    sid = launch_in_mission(fso, network="sandbox")
    try:
        sess = session.load(sid)
        cgroup = Path(f"/proc/{sess.engine_pid}/cgroup").read_text()
        nspid = next(line for line in Path(f"/proc/{sess.engine_pid}/status").read_text()
                     .splitlines() if line.startswith("NSpid:")).split()
        view = _engine_view(sess)
        ro_opts = {p: _mount_options(sess.engine_pid, p)
                   for p in (sess.fs2path, str(Path(sess.binary).parent))}
        rw_opts = _mount_options(sess.engine_pid, f"{sandbox.INSIDE}/prefs")
        tmpfs_opts = {p: _mount_options(sess.engine_pid, p) for p, _ in sandbox.TMPFS}
        root_opts = _mount_options(sess.engine_pid, "/")
        shot = xinput.screenshot(sid)
        pressed = xinput.keys(sid, ["Escape"])["pressed"]
        st = game.state(sid)
        by_host_pid = xwin.main_window(sess.display, sess.engine_pid, "game", None)
        by_window_pid = xwin.main_window(sess.display, sess.window_pid, "game", None)
    finally:
        out = launcher.stop(sid)
    lum = Image.open(shot["path"]).convert("L").getextrema()[1]
    _save("t4-real-engine.json", {"binary": str(fso), "mods": MODS, "mission": MISSION,
                                  "cgroup": cgroup.strip(), "nspid": nspid, "view": view,
                                  "ro_mount_options": ro_opts, "prefs_mount_options": rw_opts,
                                  "tmpfs_mount_options": tmpfs_opts, "root_mount": root_opts,
                                  "screenshot": shot, "max_luminance": lum,
                                  "keys": pressed, "state_keys": sorted(st),
                                  "window_by_host_pid": by_host_pid,
                                  "window_by_window_pid": by_window_pid, "stop": out})
    assert f"{sess.scope}.scope" in cgroup            # bwrap stayed in the session's scope
    assert nspid[-1] == str(sandbox.ENGINE_NS_PID) and sess.window_pid == 2
    assert not any(view["exists"].values()), view["exists"]
    assert view["x11"] == [Path(sandbox.x_socket(sess.display)).name]
    assert view["fs2path_listed"]
    assert all(o.split(",")[0] == "ro" for o in ro_opts.values()), ro_opts
    assert rw_opts.split(",")[0] == "rw" and root_opts.split(",")[0] == "ro"
    assert all(o.split(",")[0] == "rw" for o in tmpfs_opts.values()), tmpfs_opts
    assert lum > 40 and pressed == ["Escape"] and st
    assert by_window_pid is not None and by_host_pid is None   # why window_pid exists
    assert out["status"] == "stopped" and out["exit_info"]["signal"] is None, out["exit_info"]


_HOG = "#!/usr/bin/python3\nimport time\nhog = [b\"x\" * (64 << 20) for _ in range(64)]\n" \
       "time.sleep(60)\n"


def test_the_scope_memory_cap_still_kills_a_sandboxed_engine(engine_env, tmp_runtime,
                                                              tmp_path, monkeypatch):
    # A stand-in that touches 4 GiB under a 512 MiB cap: the kernel OOM-kills it inside the
    # scope's cgroup and the runner records "oom", exactly as for an unsandboxed engine.
    hog = tmp_path / "hog"
    hog.write_text(_HOG)
    hog.chmod(0o755)
    real_caps = scope.caps
    monkeypatch.setattr(launcher.scope, "caps",
                        lambda **kw: real_caps(**{**kw, "mem_kb": 512 * 1024}))
    rec = launcher.run_to_exit(LaunchRequest(binary=str(hog), network="sandbox",
                                             runtime_sec=120), timeout=90)
    _save("t4-oom.json", {"ended_by": rec.ended_by, "exit_info": rec.exit_info,
                          "memory_max": rec.memory_max})
    assert rec.ended_by == "oom", (rec.ended_by, rec.exit_info)


def _runs(fso: Path, network: str, renderer: str = "opengl") -> dict:
    """One compare run (launch, scenario, stop) on `network`; adds its engine.log text."""
    launch = {"mods": MODS, "mission": MISSION, "network": network}
    r = compare._run_once(str(fso), launch, STEPS, renderer)
    r["engine_log"] = (session.run_dir(session.load(r["id"])) / "engine.log").read_text()
    return r


def _pair(a: dict, b: dict) -> dict:
    """Diff two runs with the harness's compare metric; return the per-snapshot numbers."""
    tol = {"pos": 1e9, "hull": 1e9, "image": 0.2}
    snaps = compare._diff_runs(a, b, tol)
    verdict = compare._verdict(a, b, snaps, tol)
    return {"a": a["id"], "b": b["id"], "verdict": verdict,
            "mae": {k: v["image"]["mean_abs_diff"] for k, v in snaps.items()},
            "state_diffs": {k: v["state_diffs"] for k, v in snaps.items()}}


def _problem_lines(text: str) -> set[str]:
    """Return engine.log lines that look like warnings or errors (order and count ignored)."""
    words = ("warning", "error", "fail", "cannot", "can't", "unable")
    return {ln.strip() for ln in text.splitlines() if any(w in ln.lower() for w in words)}


def test_sandbox_renders_like_the_private_network(engine_env, tmp_runtime):
    _, fso = engine_env
    private_a = _runs(fso, "private")
    sandboxed = _runs(fso, "sandbox")
    private_b = _runs(fso, "private")
    baseline, parity = _pair(private_a, private_b), _pair(private_a, sandboxed)
    new_lines = sorted(_problem_lines(sandboxed["engine_log"])
                       - _problem_lines(private_a["engine_log"])
                       - _problem_lines(private_b["engine_log"]))
    _save("t5-parity.json", {"binary": str(fso), "mods": MODS, "mission": MISSION,
                             "renderers": [private_a["renderer"], sandboxed["renderer"]],
                             "private_vs_private": baseline, "private_vs_sandbox": parity,
                             "new_log_problem_lines_in_sandbox": new_lines})
    assert sandboxed["renderer"] == private_a["renderer"] == "opengl"
    assert parity["verdict"]["states_identical"], parity
    for label, mae in parity["mae"].items():
        # Within the run-to-run drift of the same build, with a small margin.
        assert mae <= max(0.02, 2 * baseline["mae"][label]), (label, mae, baseline["mae"])
    assert new_lines == [], new_lines
