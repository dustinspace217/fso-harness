import json
import multiprocessing

import pytest

from fsoctl import session
from fsoctl.errors import EXIT_NOT_FOUND, HarnessError


# spawn, not the platform default: Python 3.14's forkserver keeps the environment from when
# its server first started (an earlier test), so children would read another test's
# FSO_HARNESS_RUNTIME. spawn copies the current environment at each start.
_MP = multiprocessing.get_context("spawn")

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
    assert "s00000000" in e.value.message and e.value.hint


def test_list_sessions_sorted_by_creation(tmp_runtime):
    # Fixed creation times in the OPPOSITE order of creation: folder-name order or creation
    # order alone cannot pass this, only the sort by `created` (QA TA-12).
    a = _mk()
    b = _mk()
    session.update(a.id, lambda s: setattr(s, "created", 2000.0))
    session.update(b.id, lambda s: setattr(s, "created", 1000.0))
    assert [x.id for x in session.list_sessions()] == [b.id, a.id]


def _bump(sid, n, out):
    s = session.load(sid)
    out.extend([session.next_seq(s) for _ in range(n)])


def test_next_seq_is_unique_across_processes(tmp_runtime):
    s = _mk()
    with _MP.Manager() as m:
        out = m.list()
        procs = [_MP.Process(target=_bump, args=(s.id, 25, out)) for _ in range(4)]
        try:
            for p in procs:
                p.start()
            for p in procs:
                p.join(30)
            assert [p.exitcode for p in procs] == [0, 0, 0, 0]
        finally:
            for p in procs:
                if p.is_alive():
                    p.terminate()
        values = list(out)
    assert sorted(values) == list(range(1, 101))


def test_next_seq_writes_atomically(tmp_runtime, monkeypatch):
    # SL2-9: write_text truncates first, so a SIGTERM (SystemExit) mid-write could leave an
    # empty seq file, and int('') then failed on every later command of the session.
    s = _mk()
    assert session.next_seq(s) == 1
    real_replace = session.os.replace

    def interrupted(src, dst):
        raise SystemExit(143)
    monkeypatch.setattr(session.os, "replace", interrupted)
    with pytest.raises(SystemExit):
        session.next_seq(s)
    monkeypatch.setattr(session.os, "replace", real_replace)
    assert (session.run_dir(s) / "seq").read_text() == "1"
    assert session.next_seq(s) == 2


def test_audit_appends_json_lines(tmp_runtime, tmp_path, monkeypatch):
    monkeypatch.setenv("FSO_HARNESS_STATE", str(tmp_path / "state"))  # never the real one
    s = _mk()
    session.audit(s, "lua", "return 1")
    session.audit(s, "post_event", "GS_EVENT_QUIT_GAME")
    lines = (session.run_dir(s) / "audit.log").read_text().splitlines()
    assert [json.loads(x)["kind"] for x in lines] == ["lua", "post_event"]
