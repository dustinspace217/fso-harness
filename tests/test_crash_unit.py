from pathlib import Path

from fsoctl import crash, session

SAMPLE = (Path(__file__).parent / "data" / "coredumpctl.json").read_text()  # real output, trimmed


def _s(**kw):
    s = session.new_session(kind="game", binary="/bin/true", fs2path="/tmp", mods=[],
                            mission=None, renderer="opengl", width=1, height=1, switches={},
                            allow_eval=False, extra_args=[])
    for k, v in kw.items():
        setattr(s, k, v)
    session.save(s)
    return s


def test_parse_coredumpctl_picks_the_pid():
    e = crash.parse_coredumpctl(SAMPLE, 321342)
    assert e["pid"] == 321342 and e["corefile"] == "present" and e["signal"] == "SIGABRT"
    assert crash.parse_coredumpctl(SAMPLE, 1) is None
    assert crash.parse_coredumpctl("not json", 321342) is None


def test_report_without_core(tmp_runtime, monkeypatch):
    s = _s(status="exited", engine_pid=4242,
           exit_info={"returncode": -11, "signal": "SIGSEGV", "at": 1.0})
    (session.run_dir(s) / "engine.log").write_text('WARNING: "x" at a.cpp:1\nError: boom\n')
    monkeypatch.setattr(crash, "_coredumps_json", lambda since: "[]")
    # No real systemctl in the unit tier (QA TA-15), and no 10 s wait for a core.
    monkeypatch.setattr(crash.launcher, "alive", lambda sess: False)
    monkeypatch.setattr(crash.scope, "state", lambda unit: "inactive")
    monkeypatch.setattr(crash, "_CORE_WAIT", 0)
    r = crash.report(s.id)
    assert r["signal"] == "SIGSEGV" and r["core"] is None and r["backtrace"] is None
    assert [p["kind"] for p in r["problems"]] == ["warning", "error"]
    assert any("coredumpctl" in h for h in r["hints"])


def test_report_on_running_session_says_so(tmp_runtime, monkeypatch):
    s = _s(status="running", engine_pid=4242)
    monkeypatch.setattr(crash.launcher, "alive", lambda sess: True)
    r = crash.report(s.id)
    assert r["status"] == "running" and any("still running" in h for h in r["hints"])
