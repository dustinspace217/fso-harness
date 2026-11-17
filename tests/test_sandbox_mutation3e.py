"""Tests added for the round-3e mutation pass (2026-11-13); each names the survivor it kills."""
import os
import time

from fsoctl import launcher, paths, safefs, sandbox, session


def _sbx(tmp_path, **kw) -> session.Session:
    base = dict(kind="game", binary="/bin/true", fs2path=str(tmp_path), mods=[], mission=None,
                renderer="opengl", width=320, height=240, switches={}, allow_eval=False,
                extra_args=[], network="sandbox")
    base.update(kw)
    return session.new_session(**base)


class _Proc:
    """Popen stand-in: returncode stays None until `left` poll() calls have run out."""
    def __init__(self, left):
        self.returncode, self.left, self.polls = None, left, 0

    def poll(self):
        self.polls += 1
        if self.left <= 0:
            self.returncode = -9
        self.left -= 1
        return self.returncode


def test_exited_is_false_while_running_and_true_after():
    # Engine.exited mutants 2/3: "returncode is None or ..." and "... or poll() is None" both
    # answered True for a running sandbox (prefs-missing would then fail open).
    eng = sandbox.Engine(_Proc(left=5), None, None)
    assert eng.exited() is False
    eng.proc.left = 0
    assert eng.exited() is True and eng.exited() is True


def test_a_trip_waits_for_a_slow_exit_before_emptying(tmp_path, monkeypatch):
    # trip mutants 20/21/23/25: no deadline, a past deadline, an inverted test, sleep(None):
    # the wait either crashed the trip (prefs never emptied) or emptied a running sandbox.
    monkeypatch.setattr(sandbox, "warn_loudly", lambda text: None)
    monkeypatch.setattr(sandbox.session, "update", lambda sid, fn: None)
    order = []
    proc = _Proc(left=4)
    eng = sandbox.Engine(proc, None, None)
    monkeypatch.setattr(eng, "kill", lambda: order.append("kill"))
    monkeypatch.setattr(safefs, "clear_tree",
                        lambda base: order.append(("clear", proc.returncode)))
    (tmp_path / "x").write_text("x")
    started = time.monotonic()
    sandbox.Watchdog("s1", tmp_path, eng).trip("too big")
    assert order == ["kill", ("clear", -9)]          # emptied only once the sandbox was gone
    assert proc.polls >= 5 and time.monotonic() - started < 5


def test_drop_margin_default_when_the_variable_is_unset(monkeypatch):
    # drop_margin_bytes mutants 3/5: a None default crashed on .isdigit() when unset.
    monkeypatch.delenv("FSO_HARNESS_SANDBOX_DROP_MARGIN", raising=False)
    assert sandbox.drop_margin_bytes() == sandbox.DROP_MARGIN_BYTES


def test_engine_roots_skips_sessions_without_a_prefs_root(tmp_runtime, tmp_path):
    # _engine_roots mutant 2: "prefs_root or ..." read Path(None) for a non-sandbox session.
    plain = _sbx(tmp_path, network="real")
    assert plain.prefs_root is None
    assert launcher._engine_roots()[:2] == [paths.runtime_root(), paths.sandbox_root()]


def test_the_sweep_goes_on_past_a_young_folder(tmp_runtime, tmp_path, monkeypatch):
    # _cleanup_sandbox_orphans mutant 21: "continue" -> "break" after a young folder left
    # every later orphan in place.
    monkeypatch.setattr(launcher.scope, "is_gone", lambda unit: True)
    monkeypatch.setattr(launcher, "_cleanup_sinks", lambda kept, failed: {})
    root = paths.sandbox_root()
    (root / "s00000001").mkdir()                       # young: kept
    (root / "s99999999").mkdir()                       # old orphan: removed
    old = time.time() - 7200
    os.utime(root / "s99999999", (old, old))
    out = launcher.cleanup(older_than_sec=3600)
    assert out["removed_folders"] == [str(root / "s99999999")]
    assert (root / "s00000001").is_dir()


def test_a_sweep_failure_names_the_folder_and_the_error(tmp_runtime, tmp_path, monkeypatch):
    # _cleanup_sandbox_orphans mutants 28-34: the failure record's shape was not pinned.
    monkeypatch.setattr(launcher.scope, "is_gone", lambda unit: True)
    monkeypatch.setattr(launcher, "_cleanup_sinks", lambda kept, failed: {})

    def boom(parent, name):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(safefs, "remove_tree", boom)
    root = paths.sandbox_root()
    (root / "s0a1b2c3d").mkdir()
    failed: list[dict] = []
    assert launcher._cleanup_sandbox_orphans(time.time(), 0, [], failed) == []
    assert failed == [{"path": str(root / "s0a1b2c3d"),
                       "error": "[Errno 13] Permission denied"}]


def test_launch_warns_past_an_ended_sandbox_listed_first(tmp_runtime, tmp_path, monkeypatch):
    # _warn_shared_sandbox_disk mutants 14/18: "continue" -> "break" stopped at the first
    # session that did not count, missing a live one after it.
    said = []
    monkeypatch.setattr(sandbox, "warn_loudly", said.append)
    # In creation order, which is the order list_sessions returns. Sorting by the random id
    # (as this test first did) put the live session first one run in six, and the assert on
    # `listed` below then failed although nothing was wrong (seen 2026-11-16, twice).
    sessions = sorted((_sbx(tmp_path) for _ in range(6)), key=lambda s: s.created)
    for s in sessions:
        session.prefs_dir(s).mkdir(parents=True, exist_ok=True)

    def mark(st):
        def fn(rec):
            rec.status = st
        return fn
    for s in sessions[:-1]:
        session.update(s.id, mark("exited"))
    session.update(sessions[-1].id, mark("running"))
    listed = [s.id for s in session.list_sessions()]
    assert listed.index(sessions[-1].id) > 0           # an ended one is checked before it
    launcher._warn_shared_sandbox_disk()
    assert len(said) == 1 and sessions[-1].id in said[0]
    said.clear()
    gone = sessions[-1]
    session.prefs_dir(sessions[0]).rmdir()            # a running one whose folder is gone ...
    session.update(sessions[0].id, mark("running"))
    launcher._warn_shared_sandbox_disk()               # ... is skipped, the next one still seen
    assert len(said) == 1 and gone.id in said[0]


def test_a_nested_unreadable_folder_is_unlocked_and_measured(tmp_path):
    # _make_private mutants 9/11: chmod went to the top folder instead of the nested one's
    # parent, so a locked folder two levels down was never unlocked.
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "f").write_bytes(b"x" * 40_000)
    os.chmod(deep, 0)
    try:
        size, entries, vanished = safefs.usage(tmp_path, 100)
    finally:
        os.chmod(deep, 0o700)
    assert entries == 3 and not vanished
    assert size >= (deep / "f").stat().st_blocks * 512
