"""The platform-free parts of the 2026-11-16 VM probes (probes/xplat/fred2_live.py,
engine_live.py, qt_combo_routes.py): the helpers that decide what a result says.

The probes import each other by plain name (they are run as scripts from their own folder), so
that folder goes on sys.path before they are loaded under package-style names. Mutation runs
need `probes` added to [tool.mutmut] source_paths for the run (pyproject.toml lists only fsoctl;
see fso-harness-artifacts/qa-xplat-vm-probes/run-mutmut.sh)."""
import importlib.util
import json
import struct
import sys
import types
from pathlib import Path

XPLAT = Path(__file__).resolve().parents[1] / "probes" / "xplat"
sys.path.insert(0, str(XPLAT))


def load(name: str):
    """Load probes/xplat/<name>.py as module probes.xplat.<name>."""
    spec = importlib.util.spec_from_file_location(f"probes.xplat.{name}", XPLAT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fred2 = load("fred2_live")
hidden = load("hidden_run")
engine = load("engine_live")
combo = load("qt_combo_routes")


def test_bmp_header_describes_a_bottom_up_32_bit_image_of_the_given_size():
    pixels = bytes(range(24))                       # 3 x 2 pixels, 4 bytes each
    data = fred2.bmp_bytes(3, 2, pixels)
    assert data[:2] == b"BM"
    size, _, _, offset = struct.unpack("<IHHI", data[2:14])
    assert (size, offset) == (54 + 24, 54)
    header_size, width, height, planes, bits = struct.unpack("<IiiHH", data[14:30])
    assert (header_size, width, height, planes, bits) == (40, 3, 2, 1, 32)
    assert data[54:] == pixels
    # The whole header, byte for byte (mutation pass MU-1: reserved fields, compression, the
    # pixels-per-metre pair and the two colour counts were free to change): 2835 px/m = 72 dpi.
    assert data[:54] == (b"BM" + struct.pack("<IHHI", 78, 0, 0, 54)
                         + struct.pack("<IiiHHIIiiII", 40, 3, 2, 1, 32, 0, 24, 2835, 2835, 0, 0))


def test_distinct_colours_default_sample_step_is_4099_pixels():
    # MU-1: with the default step, pixel 4099 is sampled and pixel 4100 is not.
    blank = bytearray(b"\x00\x00\x00\xff" * 8200)
    blank[4 * 4099:4 * 4099 + 4] = b"\x01\x02\x03\xff"
    assert fred2.distinct_colours(bytes(blank)) == 2
    blank[4 * 4099:4 * 4099 + 4] = b"\x00\x00\x00\xff"
    blank[4 * 4100:4 * 4100 + 4] = b"\x01\x02\x03\xff"
    assert fred2.distinct_colours(bytes(blank)) == 1


def test_distinct_colours_tells_a_blank_capture_from_a_drawn_one():
    blank = b"\x10\x20\x30\xff" * 5000
    assert fred2.distinct_colours(blank, step=7) == 1
    drawn = blank[:4 * 2500] + b"\x00\x00\xff\xff" * 2500
    assert fred2.distinct_colours(drawn, step=7) == 2
    assert fred2.distinct_colours(b"", step=7) == 0


def test_a_message_box_has_only_windows_own_ids_and_an_answer_button():
    assert fred2.is_message_box([6, 7, 65535, 20]) is True          # Yes / No / text / icon
    assert fred2.is_message_box([1, 65535]) is True                 # OK
    assert fred2.is_message_box([1, 2, 1148, 1136]) is False        # the Open dialog
    assert fred2.is_message_box([65535, 20]) is False               # no answer button
    assert fred2.is_message_box([]) is False
    assert fred2.is_message_box([7, 100]) is False                  # 100 is not Windows' own
    # QA TA-5: an ordinary dialog with a static (-1) or an unnumbered control (0) is not one
    assert fred2.is_message_box([1, 2, -1]) is False
    assert fred2.is_message_box([1, 2, 0]) is False


def test_the_probe_answers_no_then_cancel_then_ok():
    assert fred2.pick_answer([6, 7, 65535]) == 7                    # Yes/No: No
    assert fred2.pick_answer([1, 2, 65535]) == 2                    # OK/Cancel: Cancel
    assert fred2.pick_answer([1, 65535]) == 1                       # OK only
    assert fred2.pick_answer([6, 65535]) is None                    # nothing it may press


def test_other_index_is_the_next_item_wrapping_and_never_the_same():
    assert fred2.other_index({"index": 0, "count": 7}) == 1
    assert fred2.other_index({"index": 6, "count": 7}) == 0
    assert fred2.other_index({"index": -1, "count": 7}) == 1         # nothing selected yet
    assert fred2.other_index({"index": 0, "count": 0}) == 0          # empty: no crash


def test_parse_size_and_command_wparam():
    assert fred2.parse_size("640X480") == (640, 480)
    # WM_COMMAND from a control: low word the ID, high word the notification
    assert fred2.command_wparam(1003, 1) == 0x000103EB
    assert fred2.command_wparam(1373, 0) == 1373
    assert fred2.command_wparam(65535, 1) == 0x0001FFFF
    assert fred2.command_wparam(-1, 1) == 0x0001FFFF                 # a negative ID keeps 16 bits


def test_attempt_records_the_error_under_its_label_and_goes_on():
    notes = []
    assert fred2.attempt(notes, "fine", lambda: 5) == 5
    assert fred2.attempt(notes, "set name", lambda: 1 / 0) is None
    assert notes == ["set name: ZeroDivisionError: division by zero"]


def test_wait_value_returns_the_first_truthy_value_and_asks_once_more_at_the_end(monkeypatch):
    clock = iter([0.0, 0.1, 0.2, 99.0])
    monkeypatch.setattr(fred2.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(fred2.time, "sleep", lambda _s: None)
    answers = iter([None, 0, "ready"])
    assert fred2.wait_value(lambda: next(answers), 5) == "ready"
    clock = iter([0.0, 99.0])
    assert fred2.wait_value(lambda: "late", 5) == "late"             # past the deadline: one ask
    # QA TA-8: a value that arrives before the deadline ends the wait at once
    clock = iter([0.0, 0.1, 0.2, 0.3, 0.4])
    asked = []
    assert fred2.wait_value(lambda: asked.append(1) or "early", 5) == "early"
    assert len(asked) == 1


MISSION = """#Objects

$Name: Alpha 1\t\t;! Object #0
$Class: GTF Myrmidon
$Team: Friendly

$Name: Probe 7b\t\t;! Object #1
$Class: SF Mara (terrans)
$Team: Friendly
+AI Class: Lieutenant

#Messages

$Name: Probe 7b
$Team: -1
"""


def test_saved_ship_reads_the_ship_entry_not_a_message_of_the_same_name():
    assert fred2.saved_ship(MISSION, "Probe 7b") == {"class": "SF Mara (terrans)",
                                                     "ai_class": "Lieutenant"}
    assert fred2.saved_ship(MISSION, "Alpha 1") == {"class": "GTF Myrmidon", "ai_class": None}
    assert fred2.saved_ship(MISSION, "Probe 7") is None             # no prefix matches
    assert fred2.saved_ship(MISSION, "Nobody") is None


def test_saved_ship_does_not_take_a_turrets_ai_class_for_the_ships():
    # QA ST-2: a ship on its default AI class (no line of its own) with one retuned turret
    text = ("$Name: Psamtik\n$Class: GVD Hatshepsut\n$Team: Friendly\n"
            "+Subsystem: turret01\n+AI Class: General\n\n$Name: Next\n$Class: X\n")
    assert fred2.saved_ship(text, "Psamtik") == {"class": "GVD Hatshepsut", "ai_class": None}
    own = text.replace("$Team: Friendly\n", "$Team: Friendly\n+AI Class: Major  \n")
    assert fred2.saved_ship(own, "Psamtik") == {"class": "GVD Hatshepsut", "ai_class": "Major"}
    # a name with regular-expression characters is matched as written
    assert fred2.saved_ship("$Name: A.1 (x)\n$Class: C\n", "A.1 (x)")["class"] == "C"
    assert fred2.saved_ship("$Name: AB1 (x)\n$Class: C\n", "A.1 (x)") is None


class FakeEditor:
    """A Ship Editor stand-in for refill_from_data: Next and Prev step through `ships`; with
    refills=False the buttons do nothing, as a dialog that never refills would look."""

    def __init__(self, ships, refills=True, prev_step=-1):
        self.ships, self.at, self.refills, self.pressed = ships, 0, refills, []
        self.prev_step = prev_step            # how far Prev moves; -1 is the real dialog

    def press(self, w, editor, control_id):
        self.pressed.append((w, editor, control_id))
        if self.refills:
            step = 1 if control_id == fred2.IDC_NEXT else self.prev_step
            self.at = (self.at + step) % len(self.ships)


def refill_ctx(monkeypatch, editor):
    """Point refill_from_data at a FakeEditor; no waiting."""
    ctx = type("Ctx", (), {"w": "user32", "notes": []})()

    def name(got_ctx, got_editor):
        assert got_ctx is ctx and got_editor == 1   # the probe's own context and dialog
        return editor.ships[editor.at]
    monkeypatch.setattr(fred2, "press", editor.press)
    monkeypatch.setattr(fred2, "ship_name", name)
    ctx.boxes_answered = 0

    def answer(got_ctx):
        got_ctx.boxes_answered += 1
    monkeypatch.setattr(fred2, "answer_boxes", answer)
    monkeypatch.setattr(fred2, "STEP_WAIT_S", 0)
    monkeypatch.setattr(fred2, "SETTLE_S", 0)
    return ctx


def test_refill_goes_to_another_ship_and_back(monkeypatch):
    editor = FakeEditor(["Alpha 2", "Alpha 3", "Alpha 4"])
    ctx = refill_ctx(monkeypatch, editor)
    assert fred2.refill_from_data(ctx, 1) == {"other_ship": "Alpha 3"}
    assert ctx.boxes_answered == 2                 # once after each change of ship
    assert editor.pressed == [("user32", 1, fred2.IDC_NEXT), ("user32", 1, fred2.IDC_PREV)]
    assert editor.at == 0


def test_refill_refuses_to_vouch_when_the_dialog_did_not_change_ship(monkeypatch):
    # The negative control QA CR-1/TA-1 asked for: a dialog that keeps showing the same
    # controls must not be read as "the data took it".
    editor = FakeEditor(["Alpha 2", "Alpha 3"], refills=False)
    try:
        fred2.refill_from_data(refill_ctx(monkeypatch, editor), 1)
    except LookupError as e:
        assert str(e) == "Next did not move to another ship (the fields were refused?)"
    else:
        raise AssertionError("refill_from_data vouched for a dialog that never refilled")


def test_refill_refuses_to_vouch_unless_prev_came_back_to_the_same_ship(monkeypatch):
    # prev_step 0: still on the other ship; prev_step 1: on a third ship (QA SC-3/ST-3)
    for prev_step in (0, 1):
        editor = FakeEditor(["Alpha 2", "Alpha 3", "Alpha 4"], prev_step=prev_step)
        try:
            fred2.refill_from_data(refill_ctx(monkeypatch, editor), 1)
        except LookupError as e:
            assert str(e) == "Prev did not come back to the ship it started from"
        else:
            raise AssertionError(f"vouched for another ship's controls (step {prev_step})")


def test_exit_code_for_passes_the_childs_code_and_marks_timeouts_and_failures():
    assert hidden.exit_code_for(hidden.WAIT_OBJECT_0, 0) == 0
    assert hidden.exit_code_for(hidden.WAIT_OBJECT_0, 3) == 3
    assert hidden.exit_code_for(hidden.WAIT_TIMEOUT, None) == 5
    assert hidden.exit_code_for(0xFFFFFFFF, None) == 2               # WAIT_FAILED
    assert hidden.exit_code_for(hidden.WAIT_OBJECT_0, None) == 2     # code could not be read


class FakeWin:
    """The one call key_check makes on user32, recording every post."""

    def __init__(self, accept=True, on_post=None):
        self.posts, self.accept, self.on_post = [], accept, on_post
        self.u = self

    def PostMessageW(self, hwnd, msg, key, lparam):  # the Win32 name
        self.posts.append((hwnd, msg, key, lparam))
        if self.on_post:
            self.on_post(len(self.posts))
        return 1 if self.accept else 0


def key_env(tmp_path, monkeypatch, log="Got event GS_EVENT_GAME_INIT (49) in state X\n"):
    """A prefs folder with an engine log in it, and key_check's waits set to nothing."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "fs2_open.log").write_text(log)
    monkeypatch.setattr(engine, "KEY_GAP_S", 0)
    monkeypatch.setattr(engine, "KEY_WAIT_S", 0)
    monkeypatch.setattr(engine.fl, "POLL_S", 0)
    return tmp_path


def test_keys_that_change_nothing_did_not_reach_the_engine(tmp_path, monkeypatch):
    # QA TA-3: the start-up event already in the log must not count
    prefs = key_env(tmp_path, monkeypatch)
    win = FakeWin()
    assert engine.key_check(win, 77, prefs) == {
        "posts_accepted": True, "pilot_files": [], "new_game_events": [],
        "reached_engine": False}
    assert [(m, k) for _, m, k, _ in win.posts] == [
        (0x0100, 0x41), (0x0101, 0x41), (0x0100, 0x0D), (0x0101, 0x0D)]
    assert [lp for *_, lp in win.posts] == [
        engine.A_DOWN, engine.A_UP, engine.RETURN_DOWN, engine.RETURN_UP]
    assert {h for h, *_ in win.posts} == {77}


def test_a_new_game_event_alone_counts_as_reaching_the_engine(tmp_path, monkeypatch):
    prefs = key_env(tmp_path, monkeypatch)

    def moves_on(count):
        if count == 4:
            with open(prefs / "data" / "fs2_open.log", "a") as f:
                f.write("Got event GS_EVENT_MAIN_MENU (0) in state Y\n")
    result = engine.key_check(FakeWin(on_post=moves_on), 77, prefs)
    assert result["reached_engine"] is True and result["pilot_files"] == []
    assert result["new_game_events"] == ["Got event GS_EVENT_MAIN_MENU (0) in state Y"]


def test_the_result_keeps_at_most_six_new_game_events(tmp_path, monkeypatch):
    prefs = key_env(tmp_path, monkeypatch)

    def floods(count):
        if count == 4:
            with open(prefs / "data" / "fs2_open.log", "a") as f:
                f.writelines(f"Got event GS_EVENT_{n} (0) in state Y\n" for n in range(9))
    result = engine.key_check(FakeWin(on_post=floods), 77, prefs)
    assert len(result["new_game_events"]) == 6 and result["reached_engine"] is True


def test_a_pilot_file_alone_counts_and_refused_posts_are_reported(tmp_path, monkeypatch):
    prefs = key_env(tmp_path, monkeypatch)

    def makes_pilot(count):
        if count == 4:
            (prefs / "data" / "players").mkdir()
            (prefs / "data" / "players" / "a.json").write_text("{}")
    result = engine.key_check(FakeWin(accept=False, on_post=makes_pilot), 77, prefs)
    assert result["reached_engine"] is True and result["pilot_files"] == ["a.json"]
    assert result["posts_accepted"] is False


def test_one_refused_post_is_reported_and_old_pilot_files_do_not_count(tmp_path, monkeypatch):
    # QA ST-4 (any() for all(), or a dropped key-up) and SC-5 (a pilot left by something else)
    prefs = key_env(tmp_path, monkeypatch)
    (prefs / "data" / "players").mkdir()
    (prefs / "data" / "players" / "old.json").write_text("{}")
    win = FakeWin()
    win.PostMessageW = lambda *a: (win.posts.append(a), 0 if len(win.posts) == 4 else 1)[1]
    result = engine.key_check(win, 77, prefs)
    assert result["posts_accepted"] is False and len(win.posts) == 4
    assert result["pilot_files"] == [] and result["reached_engine"] is False


class FakeKernel:
    """kernel32 as release_into_job uses it; assign and resume results are set per test."""

    def __init__(self, assign=1, resume=1):
        self.assign, self.resume, self.calls = assign, resume, []

    def AssignProcessToJobObject(self, job, proc):  # the Win32 names
        self.calls.append(("assign", job, proc))
        return self.assign

    def ResumeThread(self, thread):
        self.calls.append(("resume", thread))
        return self.resume

    def TerminateProcess(self, proc, code):
        self.calls.append(("terminate", proc, code))
        return 1

    def CloseHandle(self, handle):
        self.calls.append(("close", handle))
        return 1


def test_a_child_that_cannot_join_the_job_or_resume_is_ended_not_left_suspended(monkeypatch):
    # QA SC-4. ctypes.WinError exists only on Windows; here it is an OSError factory.
    monkeypatch.setattr(hidden.ctypes, "WinError", lambda code=0: OSError(code), raising=False)
    monkeypatch.setattr(hidden.ctypes, "get_last_error", lambda: 5, raising=False)
    ok = FakeKernel()
    hidden.release_into_job(ok, "job", "proc", "thread")
    assert ok.calls == [("assign", "job", "proc"), ("resume", "thread"), ("close", "thread")]
    no_assign, no_resume = FakeKernel(assign=0), FakeKernel(resume=0xFFFFFFFF)
    for broken in (no_assign, no_resume):
        try:
            hidden.release_into_job(broken, "job", "proc", "thread")
        except OSError as e:
            assert e.args == (5,)                  # Windows' own error code is carried
        else:
            raise AssertionError("a child outside the job was reported as started")
        assert broken.calls[-3:] == [("terminate", "proc", 2), ("close", "proc"),
                                     ("close", "thread")]
    assert ("resume", "thread") not in no_assign.calls     # never run outside the job
    assert ("resume", "thread") in no_resume.calls


def test_udp_ports_asks_psutil_for_udp_sockets_of_that_pid(monkeypatch):
    asked = {}
    addr = types.SimpleNamespace

    class Proc:
        def __init__(self, pid):
            asked["pid"] = pid

        def net_connections(self, kind):
            asked["kind"] = kind
            return [addr(laddr=addr(ip="0.0.0.0", port=7821)), addr(laddr=addr(ip="::", port=7821)),
                    addr(laddr=None), addr(laddr=addr(ip="0.0.0.0", port=7821))]
    monkeypatch.setitem(sys.modules, "psutil", types.SimpleNamespace(Process=Proc))
    assert engine.udp_ports(4242) == ["0.0.0.0:7821", ":::7821"]
    assert asked == {"pid": 4242, "kind": "udp"}


def test_the_engine_runs_unpaused_unless_the_probe_is_told_otherwise():
    base = ["fs2.exe", "-window", "-nosound", "-nomusic", "-port", "7821"]
    assert engine.engine_argv(Path("fs2.exe"), 7821, False) == [*base, "-no_unfocused_pause"]
    assert engine.engine_argv(Path("fs2.exe"), 7821, True) == base


def test_lines_with_strips_and_matches_any_needle():
    text = "  OpenGL Renderer  : llvmpipe \nnoise\n  OpenGL Version    : 4.6\n"
    assert engine.lines_with(text, "OpenGL Renderer", "OpenGL Version") == [
        "OpenGL Renderer  : llvmpipe", "OpenGL Version    : 4.6"]


def test_log_text_is_empty_until_the_engine_writes_one(tmp_path):
    assert engine.log_text(tmp_path) == ""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "fs2_open.log").write_bytes(b"ok \xff line\n")
    assert engine.log_text(tmp_path).startswith("ok ")


def test_read_state_is_none_for_a_missing_or_half_written_file(tmp_path):
    assert combo.read_state(tmp_path) is None
    (tmp_path / "state.json").write_text('{"index": 1')
    assert combo.read_state(tmp_path) is None
    (tmp_path / "state.json").write_text(json.dumps({"index": 1, "focus": "shipClassCombo"}))
    assert combo.read_state(tmp_path) == {"index": 1, "focus": "shipClassCombo"}


def test_a_route_took_only_when_qt_itself_ends_on_the_target_item(tmp_path, monkeypatch):
    monkeypatch.setattr(combo, "ROUTE_WAIT_S", 0)
    monkeypatch.setattr(combo, "END_SETTLE_S", 0)
    (tmp_path / "state.json").write_text(json.dumps({"index": 0, "focus": None}))

    def env():
        return type("Env", (), {"out": tmp_path, "row": None})()

    def picks(index):
        def route(given):
            assert given.row["route"]              # the route gets the environment and its row
            (tmp_path / "state.json").write_text(
                json.dumps({"index": index, "focus": "shipClassCombo"}))
        return route

    def breaks(_env):
        raise LookupError("no popup")

    def no_state(_env):
        (tmp_path / "state.json").unlink()

    rows = []
    combo.run_route("does nothing", lambda _env: None, env(), rows)       # QA TA-2
    combo.run_route("right item", picks(1), env(), rows)
    combo.run_route("one too far", picks(2), env(), rows)
    combo.run_route("raises", breaks, env(), rows)
    combo.run_route("no state file", no_state, env(), rows)
    assert [r["took"] for r in rows] == [False, True, False, False, False]
    assert rows[0] == {"route": "does nothing", "took": False, "starts_at": 0, "error": None,
                       "qt_index": 0, "qt_focus": None}
    assert rows[1] == {"route": "right item", "took": True, "starts_at": 0, "error": None,
                       "qt_index": 1, "qt_focus": "shipClassCombo"}
    assert rows[2]["qt_index"] == 2 and rows[2]["starts_at"] == 1
    assert rows[3]["error"] == "LookupError: no popup"
    assert rows[4]["qt_index"] is None and rows[4]["starts_at"] == 2


def test_passing_through_the_target_or_starting_on_it_is_not_taking_it(tmp_path, monkeypatch):
    # QA ST-1
    monkeypatch.setattr(combo, "ROUTE_WAIT_S", 0)
    state = tmp_path / "state.json"

    def env():
        return type("Env", (), {"out": tmp_path, "row": None})()

    def write(index):
        state.write_text(json.dumps({"index": index, "focus": None}))

    # a late second key moves Qt on while the probe lets things settle
    monkeypatch.setattr(combo.time, "sleep",
                        lambda s: write(2) if s == combo.END_SETTLE_S else None)
    rows = []
    write(0)
    combo.run_route("through the target", lambda _env: write(1), env(), rows)
    assert rows[0]["took"] is False and rows[0]["qt_index"] == 2
    monkeypatch.setattr(combo.time, "sleep", lambda s: None)
    write(1)
    combo.run_route("already there", lambda _env: None, env(), rows)
    assert rows[1]["took"] is False and (rows[1]["starts_at"], rows[1]["qt_index"]) == (1, 1)
    write(0)
    combo.run_route("takes it", lambda _env: write(1), env(), rows)
    assert rows[2]["took"] is True


def test_the_win32_values_the_probes_send_are_the_documented_ones():
    # QA ST-5: literals, so a wrong constant cannot agree with itself. Sources: WinUser.h
    # (WM_KEYDOWN 0x100, WM_KEYUP 0x101, VK_A 0x41, VK_RETURN 0x0D; a key lParam is repeat
    # count 1, scan code in bits 16-23, bits 30-31 set on release), WinBase.h (WAIT_TIMEOUT
    # 258, CREATE_SUSPENDED 4), WinNT.h (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE 0x2000,
    # JobObjectExtendedLimitInformation 9), fred2/resource.h (IDC_NEXT 1373, IDC_PREV 1382).
    assert (engine.WM_KEYDOWN, engine.WM_KEYUP, engine.VK_A, engine.VK_RETURN) == (
        0x100, 0x101, 0x41, 0x0D)
    assert (engine.A_DOWN, engine.A_UP) == (0x001E0001, 0xC01E0001)
    assert (engine.RETURN_DOWN, engine.RETURN_UP) == (0x001C0001, 0xC01C0001)
    assert (hidden.WAIT_OBJECT_0, hidden.WAIT_TIMEOUT, hidden.CREATE_SUSPENDED) == (0, 258, 4)
    assert hidden.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE == 0x2000
    assert hidden.JOB_EXTENDED_LIMIT_INFORMATION_CLASS == 9
    assert hidden.RESUME_FAILED == 0xFFFFFFFF
    assert (fred2.IDC_NEXT, fred2.IDC_PREV) == (1373, 1382)
    assert combo.TARGET_INDEX == 1


def test_idle_frames_counts_the_captures_that_worked_not_the_ones_asked_for(tmp_path, monkeypatch):
    # QA SC-8/ST-6: failed captures must not read as "one picture, no new frames"
    monkeypatch.setattr(engine, "IDLE_GAP_S", 0)
    shot = tmp_path / "shot.bmp"
    frames = iter([b"a", b"a", None, b"b", None, None, b"a", b"c"])

    def capture(w, hwnd, path):
        assert (w, hwnd) == ("user32", 77)
        frame = next(frames)
        if frame is None:
            raise OSError("capture failed")
        path.write_bytes(frame)
        return {"printwindow_ok": True}
    monkeypatch.setattr(engine.fl, "capture", capture)
    notes = []
    assert engine.idle_frames("user32", 77, shot, notes) == {"captures": 5,
                                                              "different_pictures": 3}
    assert notes == ["idle capture: OSError: capture failed"] * 3
