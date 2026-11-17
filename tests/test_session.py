import json
import multiprocessing
import pathlib

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


def _rewrite_many(sid, times, errors):
    """Child process: rewrite the record `times` times; note the first error and stop."""
    for _ in range(times):
        try:
            session.update(sid, lambda rec: None)
        except Exception as e:  # every kind: which one it is, is what the test reports
            errors.append(f"writer: {type(e).__name__}: {e}")
            return


def _read_until(sid, done, errors, whole_list, reads):
    """Child process: read the record (or the whole list) until `done`, checking each read
    holds the session; note the first error, and at the end how many reads were made."""
    count = 0
    while not done.is_set():
        try:
            if whole_list:
                ids = [rec.id for rec in session.list_sessions()]
            else:
                ids = [session.load(sid).id]
        except Exception as e:  # as above
            errors.append(f"reader: {type(e).__name__}: {e}")
            return
        if sid not in ids:       # a record skipped while it was being rewritten
            errors.append(f"reader: read {count} did not hold {sid}: {ids}")
            return
        count += 1
    reads.append(count)


def test_a_record_is_rewritten_and_read_by_several_processes_at_once(tmp_runtime):
    # Readers take no lock, by design: on Linux a rename swaps the file under them and they
    # see the old record or the new one. On Windows a file that any process has open cannot
    # be renamed over, and a read can meet the file mid-swap (QA Plan B: ST-1, CR-10, TA-3).
    # The launcher polls the record several times a second while the runner makes its first
    # writes, so both sides must ride that out: no reader and no writer may fail.
    s = _mk()
    with _MP.Manager() as m:
        errors, done, reads = m.list(), m.Event(), m.list()
        readers = [_MP.Process(target=_read_until, args=(s.id, done, errors, whole, reads))
                   for whole in (False, True)]
        writer = _MP.Process(target=_rewrite_many, args=(s.id, 3000, errors))
        procs = [*readers, writer]
        try:
            for p in procs:
                p.start()
            writer.join(120)
            done.set()
            for p in readers:
                p.join(30)
            assert list(errors) == []
            assert [p.exitcode for p in procs] == [0, 0, 0]
            # both readers really read (cross-family review XFA-8: a reader that never got
            # a turn, or threw its answers away, proved nothing)
            assert len(reads) == 2 and min(reads) >= 1
        finally:
            done.set()
            for p in procs:
                if p.is_alive():
                    p.terminate()
    assert session.load(s.id).id == s.id


def _busy_replace(monkeypatch, failures):
    """Make os.replace fail `failures` times as Windows does while a reader has the record
    open, then work; return (the calls made, the waits between them)."""
    real, calls, gaps = session.os.replace, [], []

    def replace(src, dst):
        calls.append(dst)
        if len(calls) <= failures:
            raise PermissionError(13, "Permission denied")
        real(src, dst)
    monkeypatch.setattr(session.os, "replace", replace)
    monkeypatch.setattr(session.time, "sleep", gaps.append)
    return calls, gaps


def test_on_windows_a_save_waits_out_a_reader_that_has_the_record_open(tmp_runtime, monkeypatch):
    # Measured on Windows (probes/xplat/record_race.py, 2026-11-17): with two readers
    # looping, 31% of the writer's renames failed with PermissionError; on Linux none.
    s = _mk()
    monkeypatch.setattr(session.platform, "BUSY_FILES_REFUSE", True)
    calls, gaps = _busy_replace(monkeypatch, 3)
    s.status = "running"
    session.save(s)
    assert session.load(s.id).status == "running"
    assert len(calls) == 4 and gaps == [session._BUSY_GAP_SEC] * 3


def test_a_record_that_stays_busy_is_an_error_after_a_bounded_number_of_tries(tmp_runtime,
                                                                             monkeypatch):
    s = _mk()
    monkeypatch.setattr(session.platform, "BUSY_FILES_REFUSE", True)
    calls, _ = _busy_replace(monkeypatch, 10 ** 6)
    with pytest.raises(PermissionError):
        session.save(s)
    assert len(calls) == session._BUSY_TRIES


def test_on_linux_a_refused_rename_is_an_error_at_once(tmp_runtime, monkeypatch):
    # there a rename never fails because of a reader: a refusal is a real fault
    s = _mk()
    monkeypatch.setattr(session.platform, "BUSY_FILES_REFUSE", False)
    calls, gaps = _busy_replace(monkeypatch, 1)
    with pytest.raises(PermissionError):
        session.save(s)
    assert len(calls) == 1 and gaps == []


def test_on_windows_a_read_waits_out_the_record_being_swapped(tmp_runtime, monkeypatch):
    # the same probe: 10% of reads met the file mid-swap and failed with PermissionError
    s = _mk()
    monkeypatch.setattr(session.platform, "BUSY_FILES_REFUSE", True)
    real, calls, gaps = pathlib.Path.read_text, [], []

    def read_text(self, *arguments, **keywords):
        if self.name == "session.json":
            calls.append(self)
            if len(calls) in (1, 2, 4):          # twice for load, once for the listing
                raise PermissionError(13, "Permission denied")
        return real(self, *arguments, **keywords)
    monkeypatch.setattr(pathlib.Path, "read_text", read_text)
    monkeypatch.setattr(session.time, "sleep", gaps.append)
    assert session.load(s.id).id == s.id
    assert [r.id for r in session.list_sessions()] == [s.id]
    assert len(calls) == 5 and gaps == [session._BUSY_GAP_SEC] * 3
    monkeypatch.setattr(session.platform, "BUSY_FILES_REFUSE", False)
    calls.clear()
    with pytest.raises(PermissionError):
        session.load(s.id)                          # call 1 fails, and is not tried again


def test_on_windows_a_record_missing_for_a_moment_is_looked_for_again(tmp_runtime, monkeypatch):
    # Seen on Windows (2026-11-17: the three-process test above failed in 2 of 8 runs
    # before this was handled; the failure whose text was read was a reader's
    # FileNotFoundError): for an instant while a record is swapped the file is not there
    # at all. A live session must not read as "no session".
    s = _mk()
    monkeypatch.setattr(session.platform, "BUSY_FILES_REFUSE", True)
    real, calls, gaps = pathlib.Path.read_text, [], []

    def read_text(self, *arguments, **keywords):
        if self.name == "session.json":
            calls.append(self)
            if len(calls) in (1, 3):                # once for load, once for the listing
                raise FileNotFoundError(2, "No such file or directory")
        return real(self, *arguments, **keywords)
    monkeypatch.setattr(pathlib.Path, "read_text", read_text)
    monkeypatch.setattr(session.time, "sleep", gaps.append)
    assert session.load(s.id).id == s.id
    assert [r.id for r in session.list_sessions()] == [s.id]
    assert len(calls) == 4 and gaps == [session._BUSY_GAP_SEC] * 2


def test_a_session_that_is_not_there_is_said_so_after_a_bounded_look(tmp_runtime, monkeypatch):
    gaps = []
    monkeypatch.setattr(session.time, "sleep", gaps.append)
    monkeypatch.setattr(session.platform, "BUSY_FILES_REFUSE", True)
    with pytest.raises(HarnessError) as e:
        session.load("s0000dead")
    assert e.value.code == EXIT_NOT_FOUND
    assert gaps == [session._BUSY_GAP_SEC] * (session._GONE_LOOKS - 1)
    gaps.clear()
    monkeypatch.setattr(session.platform, "BUSY_FILES_REFUSE", False)
    with pytest.raises(HarnessError) as e:
        session.load("s0000dead")
    assert e.value.code == EXIT_NOT_FOUND and gaps == []    # there a missing file is missing


def test_folders_that_are_not_sessions_cost_the_listing_no_second_look(tmp_runtime, monkeypatch):
    s = _mk()
    for other in ("builds", "matches", "sandbox"):
        (tmp_runtime / other).mkdir()
    (tmp_runtime / "launch.lock").write_text("")
    gaps = []
    monkeypatch.setattr(session.time, "sleep", gaps.append)
    monkeypatch.setattr(session.platform, "BUSY_FILES_REFUSE", True)
    assert [r.id for r in session.list_sessions()] == [s.id] and gaps == []
