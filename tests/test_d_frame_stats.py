"""Group D fix-wave tests for frame_stats' measurement window (QA SF-10), engine faked.

The bridge's `clock` handler (contract C4) is group A's; here game.call is patched to return the
readings it would.
"""
from pathlib import Path

import pytest

from fsoctl import frame_stats, game, launcher, session
from fsoctl.errors import EXIT_ENGINE, HarnessError

MS = 1_000_000   # nanoseconds per millisecond, the CSV's unit


def _csv(frames_ms: list[float]) -> str:
    """profiling.csv text: "<end ns>;<duration ns>" per frame (MainFrameTimer.cpp's format)."""
    t, rows = 5_000_000_000, []
    for d in frames_ms:
        t += int(d * MS)
        rows.append(f"{t};{int(d * MS)}")
    return "\n".join(rows) + "\n"


# 20 loading-ish frames of 10 ms (0.0-0.2 s), 30 frames of 50 ms (0.2-1.7 s), 30 of 10 ms after.
FRAMES = [10.0] * 20 + [50.0] * 30 + [10.0] * 30


@pytest.fixture
def run(tmp_path, tmp_runtime, monkeypatch):
    """Fake the engine run: launch writes profiling.csv, game.call answers clock, quit exits."""
    fs2 = tmp_path / "fs2"
    (fs2 / "data").mkdir(parents=True)
    monkeypatch.setenv("FS2PATH", str(fs2))
    s = session.new_session(kind="game", binary="/bin/true", fs2path=str(fs2), mods=[],
                            mission="m.fs2", renderer="opengl", width=1, height=1, switches={},
                            allow_eval=False, extra_args=[])
    box = {"csv": _csv(FRAMES), "clocks": [], "exit": {"returncode": 0, "signal": None, "at": 1},
           "calls": []}

    def launch(req):
        (Path(req.cwd) / "profiling.csv").write_text("")   # the engine opens it at start
        box["cwd"] = Path(req.cwd)
        return {"id": s.id}

    def call(sid, cmd, args=None, timeout=10.0):
        box["calls"].append(cmd)
        if cmd == "clock":
            return box["clocks"].pop(0)
        raise AssertionError(f"unexpected bridge command {cmd}")

    def quit_(sid, timeout=30.0):
        (box["cwd"] / "profiling.csv").write_text(box["csv"])   # flushed on the clean exit
        sess = session.load(sid)
        sess.status, sess.exit_info = "exited", box["exit"]
        session.save(sess)
        return {"id": sid}
    monkeypatch.setattr(launcher, "launch", launch)
    monkeypatch.setattr(launcher, "quit", quit_)
    monkeypatch.setattr(launcher, "stop", lambda sid: {"id": sid})
    monkeypatch.setattr(game, "call", call)
    monkeypatch.setattr(game, "wait_for", lambda *a, **k: {"met": True})
    return box


def _clock(mission_time, overall, frames):
    return {"frametime_overall": overall, "mission_time": mission_time,
            "gameplay_frames": frames}


def test_stats_cover_only_the_window(run):
    run["clocks"] = [_clock(0.2, 30.2, 20), _clock(1.7, 31.7, 50)]
    r = frame_stats.frame_stats([], "m.fs2", seconds=1.5, binary="/bin/true")
    assert r["frames"] == 30 and r["p50_ms"] == 50.0 and r["max_ms"] == 50.0
    assert r["window_method"] == frame_stats.WINDOW_METHOD
    assert r["whole_run"]["frames"] == len(FRAMES)
    assert r["window"] == {"start": _clock(0.2, 30.2, 20), "end": _clock(1.7, 31.7, 50)}
    assert r["clean_exit"] is True and r["exit_info"]["returncode"] == 0
    assert run["calls"] == ["clock", "clock"]


def test_window_is_cut_by_frame_count_not_mission_time(run):
    # One engine probe (2026-10-12) read mission time 1.8 s just after gameplay began, while the
    # profiler's rows start at gameplay, so a cut by mission time lands late. The bridge's
    # gameplay frame counter counts the same frames the CSV has rows for.
    run["clocks"] = [_clock(2.0, 32.0, 20), _clock(3.5, 33.5, 50)]
    r = frame_stats.frame_stats([], "m.fs2", seconds=1.5, binary="/bin/true")
    assert r["frames"] == 30 and r["p50_ms"] == 50.0


def test_unclean_exit_is_reported(run):
    run["clocks"] = [_clock(0.2, 30.2, 20), _clock(1.7, 31.7, 50)]
    run["exit"] = {"returncode": -11, "signal": "SIGSEGV", "at": 1}
    r = frame_stats.frame_stats([], "m.fs2", seconds=1.5, binary="/bin/true")
    assert r["clean_exit"] is False and r["exit_info"]["signal"] == "SIGSEGV"


def test_no_frame_count_fails_loudly(run):
    run["clocks"] = [{"mission_time": 0.2}, {"mission_time": 1.7}]
    with pytest.raises(HarnessError) as e:
        frame_stats.frame_stats([], "m.fs2", seconds=1.5, binary="/bin/true")
    assert e.value.code == EXIT_ENGINE and "gameplay_frames" in e.value.message and e.value.hint


def test_no_mission_time_fails_loudly(run):
    run["clocks"] = [_clock(None, 30.2, 20), _clock(None, 31.7, 50)]
    with pytest.raises(HarnessError) as e:
        frame_stats.frame_stats([], "m.fs2", seconds=1.5, binary="/bin/true")
    assert e.value.code == EXIT_ENGINE and "mission_time" in e.value.message and e.value.hint


def test_window_past_the_recorded_frames_fails_loudly(run):
    # The profile holds 80 frames; a window claimed to end at frame 120 cannot be cut from it.
    run["clocks"] = [_clock(7.5, 37.5, 90), _clock(9.0, 39.0, 120)]
    with pytest.raises(HarnessError) as e:
        frame_stats.frame_stats([], "m.fs2", seconds=1.5, binary="/bin/true")
    assert e.value.code == EXIT_ENGINE and "window" in e.value.message
    assert e.value.details.get("id")
