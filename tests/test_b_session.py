"""session.update (locked read-modify-write), the C6 fields, the persistent audit (C7), next_seq
order, and runtime_root's XDG branch (QA SL-7, SA-8, MU-7)."""
import json
import multiprocessing
import os
import stat
import shutil

import pytest

from fsoctl import paths, session
from fsoctl.errors import EXIT_USAGE, HarnessError

# spawn: children must see this test's FSO_HARNESS_RUNTIME (see test_session.py).
_MP = multiprocessing.get_context("spawn")


def _mk(**kw):
    base = dict(kind="game", binary="/bin/true", fs2path="/tmp", mods=[], mission=None,
                renderer="opengl", width=1280, height=720, switches={}, allow_eval=False,
                extra_args=[])
    base.update(kw)
    return session.new_session(**base)


def test_old_session_json_without_new_fields_still_loads(tmp_runtime):
    s = _mk()
    f = session.run_dir(s) / "session.json"
    data = json.loads(f.read_text())
    for k in ("ended_by", "runtime_sec", "memory_max"):
        data.pop(k, None)
    f.write_text(json.dumps(data))
    loaded = session.load(s.id)
    assert loaded.ended_by is None and loaded.runtime_sec is None and loaded.memory_max is None


def _bump_counter(sid, n):
    for _ in range(n):
        session.update(sid, lambda s: s.switches.__setitem__("n", s.switches.get("n", 0) + 1))


def test_update_loses_no_writes_across_processes(tmp_runtime):
    # Without the lock, two writers that both load before either saves lose one update: the
    # runner's exit record and stop()'s status write raced exactly like this (QA SL-7).
    s = _mk()
    procs = [_MP.Process(target=_bump_counter, args=(s.id, 25)) for _ in range(4)]
    try:
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
        assert [p.exitcode for p in procs] == [0, 0, 0, 0]
    finally:
        for p in procs:
            if p.is_alive():
                p.terminate()
    assert session.load(s.id).switches["n"] == 100


def test_update_returns_the_saved_session(tmp_runtime):
    s = _mk()
    out = session.update(s.id, lambda x: setattr(x, "status", "running"))
    assert out.status == "running" and session.load(s.id).status == "running"


def test_audit_survives_cleanup_of_the_run_folder(tmp_runtime, tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("FSO_HARNESS_STATE", str(state))
    s = _mk()
    session.audit(s, "lua", "return 1")
    shutil.rmtree(session.run_dir(s))
    rows = [json.loads(x) for x in (state / "audit.log").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["sid"] == s.id and rows[0]["kind"] == "lua" and rows[0]["text"] == "return 1"
    assert isinstance(rows[0]["t"], float)
    assert stat.S_IMODE(os.stat(state).st_mode) == 0o700


def test_state_dir_follows_xdg_then_home(tmp_path, monkeypatch):
    monkeypatch.delenv("FSO_HARNESS_STATE", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xs"))
    assert paths.state_dir() == tmp_path / "xs" / "fso-harness"
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    assert paths.state_dir() == tmp_path / "h" / ".local" / "state" / "fso-harness"
    assert stat.S_IMODE(os.stat(paths.state_dir()).st_mode) == 0o700


def test_next_seq_counts_up_from_one(tmp_runtime):
    # The bridge runs commands in number order, so the exact sequence matters, not only
    # uniqueness (a -1 step would produce cmd--00001.json, which the bridge cannot parse).
    s = _mk()
    assert [session.next_seq(s) for _ in range(3)] == [1, 2, 3]


def test_runtime_root_needs_xdg_runtime_dir(monkeypatch):
    monkeypatch.delenv("FSO_HARNESS_RUNTIME", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    with pytest.raises(HarnessError) as e:
        paths.runtime_root()
    assert e.value.code == EXIT_USAGE
    assert "XDG_RUNTIME_DIR" in e.value.message and e.value.hint


def test_runtime_root_under_xdg_creates_parents(tmp_path, monkeypatch):
    monkeypatch.delenv("FSO_HARNESS_RUNTIME", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "a" / "b"))
    root = paths.runtime_root()
    assert root == tmp_path / "a" / "b" / "fso-harness" and root.is_dir()
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700


def test_state_dir_honours_fso_harness_state(tmp_path, monkeypatch):
    monkeypatch.setenv("FSO_HARNESS_STATE", str(tmp_path / "override"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xs"))     # the override wins
    assert paths.state_dir() == tmp_path / "override"


def test_tmp_runtime_keeps_the_audit_out_of_the_real_state_dir(tmp_runtime, tmp_path):
    # conftest's tmp_runtime sets FSO_HARNESS_STATE, so unit tests that audit (game verbs)
    # never append to the user's real ~/.local/state/fso-harness/audit.log.
    assert paths.state_dir() == tmp_path / "state"
