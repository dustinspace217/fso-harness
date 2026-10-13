"""Unit tests for fsoctl.match: records, dead-holder detection, nsenter prefix. The real holder
(systemd + unshare) is exercised in tests/test_multi_engine.py; here scope is stubbed."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from fsoctl import cli, launcher, match, paths, scope, session
from fsoctl.errors import EXIT_LAUNCH, EXIT_NOT_FOUND, EXIT_REFUSED, EXIT_USAGE, HarnessError


class _FakeProc:
    def __init__(self, pid):
        self.pid = pid


def _stub_scope(monkeypatch, pid, active=True):
    """Make scope.start 'start' a holder whose pid is `pid` and report its scope active."""
    monkeypatch.setattr(scope, "start", lambda unit, argv, env, log, props: _FakeProc(pid))
    monkeypatch.setattr(scope, "is_active", lambda unit: active)
    monkeypatch.setattr(scope, "state", lambda unit: "active" if active else "inactive")
    # The post-start check runs `ip -brief link show lo` inside the namespace; stub it to "UP".
    up = subprocess.CompletedProcess(
        [], 0, stdout="lo  UNKNOWN  00:00:00:00:00:00 <LOOPBACK,UP,LOWER_UP>\n", stderr="")
    monkeypatch.setattr(match, "run_inside", lambda m, argv, timeout=10: up)


def test_pid_start_reads_field_22_of_proc_stat():
    me = match.pid_start(os.getpid())
    assert isinstance(me, int) and me > 0
    assert match.pid_start(2**22 + 12345) is None     # no such pid


def test_create_records_holder_and_is_alive(tmp_runtime, monkeypatch):
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        _stub_scope(monkeypatch, sleeper.pid)
        m = match.create(label="t")
        assert m.id.startswith("m") and len(m.id) == 9
        assert m.holder_pid == sleeper.pid
        assert m.holder_start == match.pid_start(sleeper.pid)
        assert m.status == "alive" and m.label == "t"
        assert match.load(m.id).holder_pid == sleeper.pid
        assert [x.id for x in match.list_matches()] == [m.id]
        assert match.alive(m)
        assert match.nsenter_prefix(m)[:6] == ["nsenter", "--preserve-credentials", "--user",
                                                "--net", "--target", str(sleeper.pid)]
    finally:
        sleeper.kill()
        sleeper.wait()


def test_dead_holder_detected_by_pid_start(tmp_runtime, monkeypatch):
    sleeper = subprocess.Popen(["sleep", "30"])
    _stub_scope(monkeypatch, sleeper.pid)
    m = match.create()
    sleeper.kill()
    sleeper.wait()
    assert not match.alive(m)            # pid gone
    m.holder_pid = os.getpid()           # a reused pid with a different start time
    assert not match.alive(m)
    with pytest.raises(HarnessError) as e:
        match.nsenter_prefix(m)
    assert e.value.code == EXIT_REFUSED


def test_inactive_scope_means_dead_even_with_live_pid(tmp_runtime, monkeypatch):
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        _stub_scope(monkeypatch, sleeper.pid, active=True)
        m = match.create()
        monkeypatch.setattr(scope, "is_active", lambda unit: False)
        assert not match.alive(m)
    finally:
        sleeper.kill()
        sleeper.wait()


def test_load_unknown_and_bad_ids(tmp_runtime):
    with pytest.raises(HarnessError) as e:
        match.load("m00000000")
    assert e.value.code == EXIT_NOT_FOUND
    with pytest.raises(HarnessError):
        paths.match_dir("../etc")


def test_refuses_root(tmp_runtime, monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 0)
    with pytest.raises(HarnessError) as e:
        match.create()
    assert e.value.code == EXIT_REFUSED


def test_audit_writes_persistent_line(tmp_runtime, monkeypatch):
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        _stub_scope(monkeypatch, sleeper.pid)
        m = match.create()
        text = (paths.state_dir() / "audit.log").read_text()
        assert m.id in text and '"kind": "match_create"' in text
    finally:
        sleeper.kill()
        sleeper.wait()


def test_cli_match_create_list_remove(tmp_runtime, monkeypatch, capsys):
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        _stub_scope(monkeypatch, sleeper.pid)
        stopped = []
        monkeypatch.setattr(scope, "stop", stopped.append)
        rc = cli.main(["--json", "match", "create", "--label", "repro"])
        out = capsys.readouterr().out
        assert rc == 0 and '"holder_pid": %d' % sleeper.pid in out
        mid = json.loads(out)["id"]
        cli.main(["--json", "match", "list"])
        listed = json.loads(capsys.readouterr().out)["matches"]
        assert listed[0]["id"] == mid and listed[0]["alive"] is True
        assert listed[0]["sessions"] == [] and listed[0]["label"] == "repro"
        rc = cli.main(["--json", "match", "remove", mid])
        assert rc == 0 and stopped == [match.scope_name(mid)]
        assert match.load(mid).status == "removed"
    finally:
        sleeper.kill()
        sleeper.wait()


def test_cli_match_bad_action_and_missing_id(tmp_runtime, capsys):
    assert cli.main(["--json", "match", "frobnicate"]) == EXIT_USAGE
    assert "frobnicate" in json.loads(capsys.readouterr().out)["message"]   # argparse choices
    assert cli.main(["--json", "match", "remove"]) == EXIT_USAGE
    assert "needs an id" in json.loads(capsys.readouterr().out)["message"]


# ---- pinned by the mutation pass (plan Task 11) ---------------------------------------------

def test_pid_start_parses_a_comm_with_a_parenthesis_and_is_recent(tmp_path):
    # /proc/<pid>/stat is "pid (comm) ..." and comm (the executable's file name, not argv[0]) may
    # contain ')': the fields come after the LAST ')'. A fresh process's start time (ticks since
    # boot) is within seconds of uptime.
    exe = tmp_path / "s) 1"
    exe.write_bytes(Path("/bin/sleep").read_bytes())
    exe.chmod(0o755)
    proc = subprocess.Popen([str(exe), "30"])
    try:
        got = match.pid_start(proc.pid)
        uptime = Path("/proc/uptime").read_text().split()[0]
        uptime_ticks = float(uptime) * os.sysconf("SC_CLK_TCK")
        assert got is not None and abs(uptime_ticks - got) < 5 * os.sysconf("SC_CLK_TCK")
    finally:
        proc.kill()
        proc.wait()


def test_alive_needs_a_recorded_holder_and_a_live_record(tmp_runtime, monkeypatch):
    asked = []
    monkeypatch.setattr(scope, "is_active", lambda unit: asked.append(unit) or True)
    me = match.Match(id="m0a1b2c3d", scope="fso-harness-match-m0a1b2c3d",
                     holder_pid=os.getpid(), holder_start=match.pid_start(os.getpid()),
                     status="alive")
    assert match.alive(me) and asked == ["fso-harness-match-m0a1b2c3d"]
    assert not match.alive(match.Match(id="m0a1b2c3d", scope="s", holder_pid=None,
                                       holder_start=me.holder_start))
    assert not match.alive(match.Match(id="m0a1b2c3d", scope="s", holder_pid=os.getpid(),
                                       holder_start=None))
    me.status = "removed"
    assert not match.alive(me)             # end_holder marks it; a live pid no longer counts


def test_create_starts_the_holder_argv_in_its_own_scope(tmp_runtime, monkeypatch):
    sleeper = subprocess.Popen(["sleep", "30"])
    seen = {}

    def fake_start(unit, argv, env, log, props):
        seen.update(unit=unit, argv=argv, log=log, props=props)
        return _FakeProc(sleeper.pid)
    try:
        _stub_scope(monkeypatch, sleeper.pid)
        monkeypatch.setattr(scope, "start", fake_start)
        m = match.create()
        assert seen["unit"] == m.scope == f"fso-harness-match-{m.id}"
        assert seen["argv"] == match.HOLDER_ARGV and "--map-root-user" in seen["argv"]
        assert seen["log"] == paths.match_dir(m.id) / "holder.log"
        assert any(p.startswith("RuntimeMaxSec=") for p in seen["props"])
        line = (paths.state_dir() / "audit.log").read_text().splitlines()[-1]
        rec = json.loads(line)
        assert rec["mid"] == m.id and rec["kind"] == "match_create" and "t" in rec
        assert "text" in rec and str(sleeper.pid) in rec["text"]
    finally:
        sleeper.kill()
        sleeper.wait()


def test_list_matches_is_oldest_first(tmp_runtime):
    a = match.Match(id="m0000000a", scope="s", created=200.0)
    b = match.Match(id="m0000000b", scope="s", created=100.0)
    match.save(a)
    match.save(b)
    assert [m.id for m in match.list_matches()] == ["m0000000b", "m0000000a"]


# ---- QA fix wave, batch 2: lifetime (SL-4/CR-1/TA-8, SL-11, SA-6/SL-12, CR-2/SL-5, SL-6,
# SL-7/TA-4, SA-7/TA-13)

def _live_holder(monkeypatch):
    """A real sleep as the holder; returns (match, sleeper). Caller kills the sleeper."""
    sleeper = subprocess.Popen(["sleep", "30"])
    _stub_scope(monkeypatch, sleeper.pid)
    return match.create(), sleeper


def test_create_takes_the_launch_lock(tmp_runtime, monkeypatch):
    locks = []
    real = session.file_lock

    def spy(path, timeout):
        locks.append(path.name)
        return real(path, timeout)
    monkeypatch.setattr(session, "file_lock", spy)
    _, sleeper = _live_holder(monkeypatch)
    try:
        assert "launch.lock" in locks                       # spec section 6 (QA SL-11)
    finally:
        sleeper.kill()
        sleeper.wait()


def test_create_failure_paths_end_the_holder_and_say_why(tmp_runtime, monkeypatch):
    # CR-2 / SL-5: a holder that exits at once must give EXIT_LAUNCH with holder.log's tail,
    # and a scope start that raises must still end the scope and mark the record removed.
    gone = subprocess.Popen(["true"])
    gone.wait()
    _stub_scope(monkeypatch, gone.pid)
    stopped = []
    monkeypatch.setattr(scope, "stop", stopped.append)

    def start_and_log(unit, argv, env, log, props):
        Path(log).write_text("unshare: unshare failed: Operation not permitted\n")
        return _FakeProc(gone.pid)
    monkeypatch.setattr(scope, "start", start_and_log)
    with pytest.raises(HarnessError) as e:
        match.create()
    assert e.value.code == EXIT_LAUNCH and "Operation not permitted" in e.value.message
    assert len(stopped) == 1 and match.list_matches()[0].status == "removed"

    def start_raises(unit, argv, env, log, props):
        raise HarnessError(EXIT_LAUNCH, "scope never became active", "")
    monkeypatch.setattr(scope, "start", start_raises)
    with pytest.raises(HarnessError):
        match.create()
    assert len(stopped) == 2
    assert all(m.status == "removed" for m in match.list_matches())
    text = (paths.state_dir() / "audit.log").read_text()
    assert text.count('"kind": "match_create_failed"') == 2


def test_alive_is_false_while_removing_and_nsenter_is_refused(tmp_runtime, monkeypatch):
    m, sleeper = _live_holder(monkeypatch)
    try:
        assert match.alive(m)
        m.status = "removing"
        match.save(m)
        assert not match.alive(match.load(m.id))
        with pytest.raises(HarnessError) as e:
            match.nsenter_prefix(match.load(m.id))
        assert e.value.code == EXIT_REFUSED
    finally:
        sleeper.kill()
        sleeper.wait()


def test_run_inside_errors_become_false_in_listening(tmp_runtime, monkeypatch):
    # SL-6: a status read must report, never raise, when the holder dies mid-read or ss hangs.
    m, sleeper = _live_holder(monkeypatch)
    try:
        s = session.new_session(kind="game", binary="/bin/true", fs2path="/g", mods=[],
                                 mission=None, renderer="opengl", width=1, height=1, switches={},
                                 allow_eval=False, extra_args=[], match=m.id, role="standalone",
                                 port=7808)
        monkeypatch.setattr(match, "run_inside", lambda mm, argv, timeout=10: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(argv, timeout)))
        assert launcher.listening(s) is False
        monkeypatch.setattr(match, "run_inside", lambda mm, argv, timeout=10: (_ for _ in ()).throw(
            HarnessError(EXIT_REFUSED, "dead", "")))
        assert launcher.listening(s) is False
    finally:
        sleeper.kill()
        sleeper.wait()


def test_dead_match_is_written_back_once_with_one_audit_line(tmp_runtime, monkeypatch, capsys):
    # SL-7 / TA-4 / SA-7 / TA-13: the first read that sees a dead holder writes status "dead"
    # and ONE audit line; later reads repeat neither; list_all shows dead-network.
    m, sleeper = _live_holder(monkeypatch)
    s = session.new_session(kind="game", binary="/bin/true", fs2path="/g", mods=[], mission=None,
                             renderer="opengl", width=1, height=1, switches={}, allow_eval=False,
                             extra_args=[], match=m.id, role="client", port=7809)
    s.status = "running"
    session.save(s)
    sleeper.kill()
    sleeper.wait()
    cli.main(["--json", "match", "list"])
    listed = json.loads(capsys.readouterr().out)["matches"][0]
    assert listed["status"] == "dead" and listed["alive"] is False
    assert match.load(m.id).status == "dead"
    cli.main(["--json", "match", "list"])
    capsys.readouterr()
    text = (paths.state_dir() / "audit.log").read_text()
    assert text.count('"kind": "match_dead"') == 1
    assert launcher.list_all()["sessions"][0]["status"] == "dead-network"


def test_match_cap(tmp_runtime, monkeypatch):
    # XFK-3: matches are holders that live a day; cap them like sessions.
    monkeypatch.setenv("FSO_HARNESS_MAX_MATCHES", "1")
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        _stub_scope(monkeypatch, sleeper.pid)
        match.create()
        with pytest.raises(HarnessError) as e:
            match.create()
        assert e.value.code == EXIT_REFUSED and "match" in e.value.message
    finally:
        sleeper.kill()
        sleeper.wait()
