"""win_crash: the engine's own crash dump on Windows, and a backtrace from it
(cross-platform Plan B, Task 8).

The engine writes `<exe stem>_YYYYMMDD_HHMMSS.mdmp` beside its executable when an exception
goes unhandled (code/globalincs/crashdump.cpp; the stamp is UTC). Everything here runs on
Linux with files in a temp folder and a faked debugger; the real dump and the real cdb were
run in the VM (docs/research/2026-11-16-xplat-vm-results.md, "Crash reports")."""
import os
import struct
import subprocess
import types
from pathlib import Path

import pytest

from fsoctl import crash, platform, session
from fsoctl.platform import win_crash

STEM = "fs2_open_26_2_0_x64_SSE2-FASTDBG"
T0 = 1794900140.6                      # a session's start (2026-11-17 07:22:20 UTC)
REAL_CDB = (Path(__file__).parent / "data" / "cdb_ecxr_k.txt").read_text()   # real output


def minidump(pid=4242, misc=True) -> bytes:
    """The smallest file win_crash reads a process id from: a minidump header, a one-entry
    stream directory, and (misc=True) a MiscInfo stream that carries the pid."""
    header = struct.pack("<4sIIIIIQ", b"MDMP", 0xA793, 1, 32, 0, 0, 0)
    if not misc:
        return header + struct.pack("<III", 3, 0, 0)            # a thread list, no MiscInfo
    return (header + struct.pack("<III", 15, 24, 44)
            + struct.pack("<IIIIII", 24, 0x1, pid, 0, 0, 0))


def put(folder, name, pid=4242, data=None):
    """Write one dump file and return its path."""
    path = folder / name
    path.write_bytes(minidump(pid) if data is None else data)
    return path


def sess(folder, created=T0, pid=4242, **more):
    """A stand-in session record: the fields find_dump and end_hints read."""
    base = dict(binary=str(folder / f"{STEM}.exe"), created=created, engine_pid=pid,
                status="exited", ended_by="exit", runtime_sec=1800, scope="fso-harness-s1",
                exit_info={"returncode": 3765269347, "signal": None, "at": created + 250})
    base.update(more)
    return types.SimpleNamespace(**base)


def test_the_dump_stamp_is_read_as_utc(tmp_path):
    path = put(tmp_path, f"{STEM}_20261117_072630.mdmp")
    found = win_crash.find_dump(sess(tmp_path))
    assert found == {"path": str(path), "size": path.stat().st_size, "time": 1794900390,
                     "pid": 4242}


def test_a_dump_stamped_before_the_session_started_is_ignored(tmp_path):
    put(tmp_path, f"{STEM}_20261117_072219.mdmp")               # one second before T0
    assert win_crash.find_dump(sess(tmp_path)) is None
    put(tmp_path, f"{STEM}_20261117_072220.mdmp")               # the second it started in
    assert win_crash.find_dump(sess(tmp_path))["time"] == 1794900140


def test_the_newest_matching_dump_wins(tmp_path):
    put(tmp_path, f"{STEM}_20261117_072630.mdmp")
    newest = put(tmp_path, f"{STEM}_20261117_072901.mdmp")
    put(tmp_path, f"{STEM}_20261117_072700.mdmp")
    still_running = sess(tmp_path, status="running", exit_info=None)
    assert win_crash.find_dump(still_running)["path"] == str(newest)


def test_a_dump_stamped_after_the_session_ended_is_not_its_own(tmp_path):
    # cross-family review XFA-5: process ids are reused. A later session of the same build
    # that got this session's engine pid and crashed would be reported as this one's crash.
    ours = put(tmp_path, f"{STEM}_20261117_072630.mdmp")        # T0 + 250 s: the recorded end
    put(tmp_path, f"{STEM}_20261117_072901.mdmp")               # 151 s after it ended
    assert win_crash.find_dump(sess(tmp_path))["path"] == str(ours)
    within = put(tmp_path, f"{STEM}_20261117_072632.mdmp")      # inside the allowance
    assert win_crash.find_dump(sess(tmp_path))["path"] == str(within)
    last = put(tmp_path, f"{STEM}_20261117_072635.mdmp")        # the allowance's last second
    assert win_crash.find_dump(sess(tmp_path))["path"] == str(last)
    put(tmp_path, f"{STEM}_20261117_072636.mdmp")               # one second past it
    assert win_crash.find_dump(sess(tmp_path))["path"] == str(last)


def test_another_executables_dump_in_the_same_folder_is_ignored(tmp_path):
    put(tmp_path, "fs2_open_26_2_0_x64_SSE2_20261117_072630.mdmp")       # the release build
    put(tmp_path, "qtfred_26_2_0_x64_SSE2-FASTDBG_20261117_072630.mdmp")
    put(tmp_path, f"{STEM}_20261117_072630.mdmp.bak")
    put(tmp_path, f"old-{STEM}_20261117_072630.mdmp")
    assert win_crash.find_dump(sess(tmp_path)) is None


def test_a_dump_another_session_of_the_same_engine_wrote_is_not_ours(tmp_path):
    # Two sessions of one build at once is the harness's normal use, and the file name
    # carries no process id: by name and time alone the later crash would be reported for
    # both sessions. The dump itself records the process that wrote it.
    mine = put(tmp_path, f"{STEM}_20261117_072630.mdmp", pid=4242)
    put(tmp_path, f"{STEM}_20261117_072645.mdmp", pid=5151)              # newer, not ours
    assert win_crash.find_dump(sess(tmp_path, pid=4242))["path"] == str(mine)
    other = sess(tmp_path, pid=5151, exit_info={"returncode": 1, "signal": None,
                                                "at": T0 + 266})        # it ended later
    assert win_crash.find_dump(other)["time"] == 1794900405
    assert win_crash.find_dump(sess(tmp_path, pid=6000)) is None


@pytest.mark.parametrize("data", [minidump(misc=False), b"MDMP", b"not a dump at all" * 4,
                                  minidump()[:40], b""])
def test_a_dump_whose_process_id_cannot_be_read_is_matched_by_name_and_time(tmp_path, data):
    path = put(tmp_path, f"{STEM}_20261117_072630.mdmp", data=data)
    found = win_crash.find_dump(sess(tmp_path))
    assert found["path"] == str(path) and found["pid"] is None


def test_a_stream_directory_that_points_outside_the_file_is_not_followed():
    # 4 billion streams at an offset past the end: a bounded read, never a huge loop
    header = struct.pack("<4sIIIIIQ", b"MDMP", 0xA793, 0xFFFFFFFF, 0x7FFFFFFF, 0, 0, 0)
    assert win_crash.dump_pid(header + b"\0" * 64) is None
    wild = (struct.pack("<4sIIIIIQ", b"MDMP", 0xA793, 1, 32, 0, 0, 0)
            + struct.pack("<III", 15, 24, 0x7FFFFFF0))
    assert win_crash.dump_pid(wild) is None
    assert win_crash.dump_pid(minidump(77)) == 77


def test_file_names_match_whatever_their_case(tmp_path):
    put(tmp_path, f"{STEM.upper()}_20261117_072630.MDMP")
    assert win_crash.find_dump(sess(tmp_path))["time"] == 1794900390


@pytest.mark.symlinks
def test_a_stamp_that_is_not_a_date_and_a_link_are_ignored(tmp_path):
    put(tmp_path, f"{STEM}_20261317_072630.mdmp")               # month 13
    put(tmp_path, f"{STEM}_2026111_072630.mdmp")
    elsewhere = put(tmp_path, "elsewhere.bin")
    os.symlink(elsewhere, tmp_path / f"{STEM}_20261117_072900.mdmp")
    (tmp_path / f"{STEM}_20261117_072901.mdmp").mkdir()
    assert win_crash.find_dump(sess(tmp_path)) is None


def test_no_engine_folder_means_no_dump(tmp_path):
    assert win_crash.find_dump(sess(tmp_path / "gone")) is None


def test_a_folder_with_too_many_files_is_said_not_scanned_forever(tmp_path, monkeypatch):
    monkeypatch.setattr(win_crash, "_SCAN_MAX", 3)
    for n in range(5):
        (tmp_path / f"file{n}.txt").write_text("x")
    with pytest.raises(OSError) as e:
        win_crash.find_dump(sess(tmp_path))
    assert "more than 3 files" in str(e.value)


def test_the_debugger_is_found_on_path_or_where_the_windows_sdk_puts_it(tmp_path, monkeypatch):
    # Measured in the VM: the SDK's installer puts cdb.exe under Windows Kits and does not
    # add it to PATH.
    monkeypatch.setattr(win_crash.programs, "find", lambda name: None)
    monkeypatch.setenv("PROGRAMFILES(X86)", str(tmp_path / "pf86"))
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "pf"))
    assert win_crash.find_debugger() is None
    kit = tmp_path / "pf86" / "Windows Kits" / "10" / "Debuggers" / "x64"
    kit.mkdir(parents=True)
    (kit / "cdb.exe").write_bytes(b"")
    assert win_crash.find_debugger() == str(kit / "cdb.exe")
    monkeypatch.setattr(win_crash.programs, "find",
                        lambda name: r"D:\tools\cdb.exe" if name == "cdb" else None)
    assert win_crash.find_debugger() == r"D:\tools\cdb.exe"     # PATH comes first


def fake_cdb(monkeypatch, stdout=REAL_CDB, error=None):
    """Make the debugger 'installed' and record how it is run."""
    seen = {}
    monkeypatch.setattr(win_crash, "find_debugger", lambda: r"C:\kits\cdb.exe")

    def run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        seen["cwd_listing"] = sorted(os.listdir(kw["cwd"]))
        if error:
            raise error
        out = stdout if kw.get("capture_output") is True else None
        if out is not None and kw.get("text") is not True:
            out = out.encode()
        return subprocess.CompletedProcess(argv, 0, out, "")
    monkeypatch.setattr(win_crash.subprocess, "run", run)
    return seen


def test_backtrace_is_none_without_the_debugger(monkeypatch):
    monkeypatch.setattr(win_crash, "find_debugger", lambda: None)

    def never(*a, **kw):
        raise AssertionError("ran something with no debugger installed")
    monkeypatch.setattr(win_crash.subprocess, "run", never)
    assert win_crash.backtrace({"path": r"C:\FS2\x.mdmp"}) is None


def test_backtrace_gives_the_frames_of_the_crash_from_real_cdb_output(monkeypatch):
    seen = fake_cdb(monkeypatch)
    frames = win_crash.backtrace({"path": r"C:\FS2\x_20261117_072630.mdmp"})
    assert frames[0].endswith("KERNELBASE!RaiseException+0x8a")
    assert frames[-1].endswith("fs2_open_26_2_0_x64_SSE2_FASTDBG+0x43ee1c")    # frame 30
    assert all("`" in f for f in frames)                # frame rows only: no banner, no quit
    assert len(frames) == 30 and not any("WARNING" in f for f in frames)
    assert seen["argv"] == [r"C:\kits\cdb.exe", "-z", r"C:\FS2\x_20261117_072630.mdmp",
                            "-y", r"C:\FS2", "-c", ".ecxr; k 0n30; q"]


def test_the_debugger_runs_bounded_offline_and_in_an_empty_folder(monkeypatch):
    monkeypatch.setenv("_NT_SYMBOL_PATH", "srv*https://msdl.microsoft.com/download/symbols")
    monkeypatch.setenv("_NT_ALT_SYMBOL_PATH", "srv*x")
    monkeypatch.setenv("KEEP_ME", "1")
    # the debugger reads more than the two symbol paths (Microsoft's list of its variables):
    # an image path "used in some situations such as user mode minidump files" among them
    others = ("_NT_EXECUTABLE_IMAGE_PATH", "_NT_SOURCE_PATH", "_NT_DEBUGGER_EXTENSION_PATH",
              "_NT_DEBUG_LOG_FILE_OPEN", "DBGHELP_LOG", "DBGENG_X", "SRCSRV_SHOW_TF_PROMPT",
              "_nt_source_path_lower")
    for name in others:
        monkeypatch.setenv(name, "srv*https://example.invalid")
    seen = fake_cdb(monkeypatch)
    win_crash.backtrace({"path": r"C:\FS2\x.mdmp"})
    kw = seen["kw"]
    assert kw["timeout"] == win_crash.DEBUGGER_TIMEOUT and kw["check"] is False
    assert kw["errors"] == "replace"                    # a dump's path may hold any letters
    names = {k.upper() for k in kw["env"]}
    assert "_NT_SYMBOL_PATH" not in names and "_NT_ALT_SYMBOL_PATH" not in names
    assert not any(name.upper() in names for name in others)
    assert kw["env"]["KEEP_ME"] == "1"
    assert seen["cwd_listing"] == []                    # no ntsd.ini for cdb to run
    assert not os.path.exists(kw["cwd"])                # and the folder is gone afterwards


def test_backtrace_is_bounded_and_survives_a_debugger_that_fails(monkeypatch):
    rows = "\n".join(f"00000000`0015e{n:03x} 00000001`403ea877     mod+0x{n:x}"
                     for n in range(900))
    assert win_crash._frames(f"Child-SP x\n{rows}\nquit:\n")[899].endswith("mod+0x383")
    fake_cdb(monkeypatch, stdout=f"Child-SP          RetAddr               Call Site\n{rows}\n"
                                 "quit:\n")
    assert len(win_crash.backtrace({"path": r"C:\FS2\x.mdmp"})) == win_crash.BT_MAX_LINES
    fake_cdb(monkeypatch, error=subprocess.TimeoutExpired("cdb", 1))
    assert win_crash.backtrace({"path": r"C:\FS2\x.mdmp"}) is None
    fake_cdb(monkeypatch, error=OSError("cannot run"))
    assert win_crash.backtrace({"path": r"C:\FS2\x.mdmp"}) is None
    fake_cdb(monkeypatch, stdout="")
    assert win_crash.backtrace({"path": r"C:\FS2\x.mdmp"}) is None


def test_output_in_a_shape_not_seen_before_is_passed_on_not_dropped(monkeypatch):
    # No "Child-SP" header (another cdb version, or the command failed): give the reader
    # what the debugger said after our command instead of nothing.
    fake_cdb(monkeypatch, stdout="banner\n0:000> cdb: Reading initial command '.ecxr; k 0n30; q'\n"
                                 "Unable to get exception context, HRESULT 0x8000FFFF\n\n"
                                 "something else\nquit:\nNatVis script unloaded\n")
    assert win_crash.backtrace({"path": r"C:\FS2\x.mdmp"}) == [
        "Unable to get exception context, HRESULT 0x8000FFFF", "something else"]


def test_end_hints_name_the_windows_exit_code(tmp_path):
    hints = win_crash.end_hints(sess(tmp_path), {"path": "x"})
    assert any("0xE06D7363" in h and "C++ exception" in h for h in hints)
    crashed = sess(tmp_path, exit_info={"returncode": 3221225477, "signal": None, "at": 1.0})
    assert any("0xC0000005" in h and "access violation" in h
               for h in win_crash.end_hints(crashed, {"path": "x"}))
    unknown = sess(tmp_path, exit_info={"returncode": 0xC0000142, "signal": None, "at": 1.0})
    assert any("0xC0000142" in h for h in win_crash.end_hints(unknown, {"path": "x"}))
    clean = sess(tmp_path, exit_info={"returncode": 0, "signal": None, "at": 1.0})
    assert win_crash.end_hints(clean, None) == []
    asked = sess(tmp_path, exit_info={"returncode": 1, "signal": None, "at": 1.0})
    assert win_crash.end_hints(asked, None) == []       # a small code is the engine's own


def test_end_hints_say_why_there_may_be_no_dump_and_what_ended_the_run(tmp_path):
    hints = win_crash.end_hints(sess(tmp_path), None)
    assert any("no crash dump" in h and "beside its executable" in h for h in hints)
    limit = sess(tmp_path, ended_by="runtime_limit", runtime_sec=60,
                 exit_info={"returncode": 1, "signal": None, "at": 1.0})
    assert any("runtime limit (60 s)" in h and "--runtime" in h
               for h in win_crash.end_hints(limit, None))
    assert not any("journalctl" in h for h in win_crash.end_hints(limit, None))


def _record(tmp_path, **kw):
    """A saved session whose engine is the fake build in tmp_path."""
    s = session.new_session(kind="game", binary=str(tmp_path / f"{STEM}.exe"), fs2path="/tmp",
                            mods=[], mission=None, renderer="opengl", width=1, height=1,
                            switches={}, allow_eval=False, extra_args=[])
    fields = dict(status="exited", engine_pid=4242, ended_by="exit", created=T0,
                  exit_info={"returncode": 3765269347, "signal": None, "at": T0 + 250})
    fields.update(kw)
    for k, v in fields.items():
        setattr(s, k, v)
    session.save(s)
    return s


@pytest.fixture
def windows_report(monkeypatch):
    """crash.report as Windows runs it: the platform has a crash module, the engine is gone."""
    monkeypatch.setattr(platform, "crash", win_crash)
    monkeypatch.setattr(crash.launcher, "alive", lambda sess: False)
    monkeypatch.setattr(win_crash.scope, "is_gone", lambda unit: True)
    # the launcher's own answer to "is the session over" asks its group module (launcher._gone)
    monkeypatch.setattr(crash.launcher.scope, "is_gone", lambda unit: True)

    def no_coredumpctl(*a, **kw):
        raise AssertionError("asked systemd for a core dump on Windows")
    monkeypatch.setattr(crash, "_coredumps_json", no_coredumpctl)


def test_the_windows_report_keeps_the_linux_keys_and_names_the_dump(tmp_runtime, tmp_path,
                                                                   windows_report, monkeypatch):
    s = _record(tmp_path)
    dump = put(tmp_path, f"{STEM}_20261117_072630.mdmp")
    (session.run_dir(s) / "engine.log").write_text('WARNING: "x" at a.cpp:1\nError: boom\n')
    monkeypatch.setattr(win_crash, "find_debugger", lambda: None)
    r = crash.report(s.id)
    linux_keys = {"id", "status", "exit_info", "ended_by", "signal", "core", "backtrace",
                  "log_tail", "problems", "popups", "last_heartbeat", "log_error", "hints"}
    assert linux_keys <= set(r)
    assert r["dump"]["path"] == str(dump) and r["dump"]["pid"] == 4242
    assert r["exit_info"]["returncode"] == 3765269347 and r["signal"] is None
    assert r["core"] is None and r["backtrace"] is None
    assert [p["kind"] for p in r["problems"]] == ["warning", "error"]
    assert ("install the Debugging Tools for Windows (cdb) to get a backtrace" in r["hints"])
    assert any("0xE06D7363" in h for h in r["hints"])


def test_the_windows_report_carries_the_backtrace_when_the_debugger_is_there(
        tmp_runtime, tmp_path, windows_report, monkeypatch):
    s = _record(tmp_path)
    put(tmp_path, f"{STEM}_20261117_072630.mdmp")
    fake_cdb(monkeypatch)
    r = crash.report(s.id)
    assert r["backtrace"][0].endswith("KERNELBASE!RaiseException+0x8a")
    assert not any("install the Debugging Tools" in h for h in r["hints"])
    assert any(".pdb" in h for h in r["hints"])         # what the names in it are worth
    fake_cdb(monkeypatch, stdout="")
    r = crash.report(s.id)
    assert r["backtrace"] is None
    assert any("cdb produced no backtrace" in h for h in r["hints"])


def test_the_windows_report_without_a_dump_says_so_and_runs_no_debugger(
        tmp_runtime, tmp_path, windows_report, monkeypatch):
    s = _record(tmp_path)

    def never(dump):
        raise AssertionError("ran the debugger with no dump")
    monkeypatch.setattr(win_crash, "backtrace", never)
    r = crash.report(s.id)
    assert r["dump"] is None and r["backtrace"] is None
    assert any("no crash dump" in h for h in r["hints"])


def test_a_dump_search_that_fails_is_a_hint_not_a_failed_report(tmp_runtime, tmp_path,
                                                               windows_report, monkeypatch):
    s = _record(tmp_path)

    def broken(sess):
        raise OSError("more than 20000 files beside the engine")
    monkeypatch.setattr(win_crash, "find_dump", broken)
    r = crash.report(s.id)
    assert r["dump"] is None
    assert any("more than 20000 files" in h for h in r["hints"])


@pytest.mark.linux
def test_linux_has_no_crash_module_and_its_report_is_unchanged():
    assert platform.crash is None


def test_the_windows_debugger_limits_are_the_linux_ones():
    # win_crash cannot import crash.py (crash.py imports the platform package), so it
    # repeats the three limits; this holds the copies equal.
    assert (win_crash.DEBUGGER_TIMEOUT, win_crash.BT_FRAMES, win_crash.BT_MAX_LINES) == (
        crash._GDB_TIMEOUT, crash._BT_FRAMES, crash._BT_MAX_LINES)


# ---- gaps a mutation run found (QA Plan B, MU-14..17, MU-22): each test names what it pins ----
HEADER = "<4sIIIIIQ"


def test_the_process_id_is_read_from_a_misc_stream_that_is_not_the_first():
    two = (struct.pack(HEADER, b"MDMP", 0xA793, 2, 32, 0, 0, 0)
           + struct.pack("<III", 3, 0, 0)               # a thread list first
           + struct.pack("<III", 15, 24, 56)            # then MiscInfo, at offset 56
           + struct.pack("<IIIIII", 24, 0x1, 909, 0, 0, 0))
    assert win_crash.dump_pid(two) == 909


def test_a_misc_stream_that_does_not_carry_a_process_id_gives_none():
    no_pid = (struct.pack(HEADER, b"MDMP", 0xA793, 1, 32, 0, 0, 0)
              + struct.pack("<III", 15, 24, 44)
              + struct.pack("<IIIIII", 24, 0x2, 909, 0, 0, 0))   # flags: no process id here
    assert win_crash.dump_pid(no_pid) is None


def test_fields_that_end_exactly_at_the_end_of_what_was_read_are_still_read():
    # the stream first, the directory entry last: both end where the bytes end
    tight = (struct.pack(HEADER, b"MDMP", 0xA793, 1, 44, 0, 0, 0)
             + struct.pack("<III", 12, 0x1, 707)        # MiscInfo at 32..44
             + struct.pack("<III", 15, 12, 32))         # its directory entry at 44..56
    assert len(tight) == 56 and win_crash.dump_pid(tight) == 707
    tail = (struct.pack(HEADER, b"MDMP", 0xA793, 1, 32, 0, 0, 0)
            + struct.pack("<III", 15, 12, 44)
            + struct.pack("<III", 12, 0x1, 808))        # MiscInfo is the last 12 bytes
    assert len(tail) == 56 and win_crash.dump_pid(tail) == 808
    assert win_crash.dump_pid(tail[:52]) is None        # four bytes short: not read past


class Listing:
    """A folder listing in a chosen order (os.scandir gives whatever order the disk has)."""

    def __init__(self, entries):
        self.entries = entries

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self.entries)


class Unreadable:
    """A directory entry with a dump's name whose details cannot be read (it went away)."""
    name, path = f"{STEM}_20261117_072600.mdmp", "gone"

    def stat(self, follow_symlinks=True):
        raise OSError("no such file")


def test_an_entry_that_is_not_a_dump_does_not_end_the_search(tmp_path, monkeypatch):
    (tmp_path / f"{STEM}_20261117_072601.mdmp").mkdir()         # a folder with a dump's name
    real = put(tmp_path, f"{STEM}_20261117_072630.mdmp")
    listed = sorted(os.scandir(tmp_path), key=lambda entry: entry.name)
    monkeypatch.setattr(win_crash.os, "scandir",
                        lambda folder: Listing([Unreadable(), *listed]))
    assert win_crash.find_dump(sess(tmp_path))["path"] == str(real)


def test_only_the_start_of_a_dump_is_read(tmp_path, monkeypatch):
    monkeypatch.setattr(win_crash, "_HEAD_MAX", 16)
    big = tmp_path / "big.mdmp"
    big.write_bytes(bytes(range(100)))
    assert win_crash._read_head(str(big)) == bytes(range(16))
    assert win_crash._read_head(str(tmp_path / "missing.mdmp")) == b""


def test_the_debugger_is_also_found_under_the_64_bit_program_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(win_crash.programs, "find", lambda name: None)
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "pf"))
    kit = tmp_path / "pf" / "Windows Kits" / "10" / "Debuggers" / "x64"
    kit.mkdir(parents=True)
    (kit / "cdb.exe").write_bytes(b"")
    assert win_crash.find_debugger() == str(kit / "cdb.exe")


def test_output_with_no_header_and_no_echo_of_the_command_is_passed_on_whole():
    assert win_crash._frames("first line\n\nsecond line\n") == ["first line", "second line"]


def test_end_hints_cover_every_code_with_the_top_bit_and_name_the_unknown_ones(tmp_path):
    lowest = sess(tmp_path, exit_info={"returncode": 0x80000000, "signal": None, "at": 1.0})
    assert any("0x80000000" in h for h in win_crash.end_hints(lowest, {"path": "x"}))
    below = sess(tmp_path, exit_info={"returncode": 0x7FFFFFFF, "signal": None, "at": 1.0})
    assert win_crash.end_hints(below, None) == []
    unknown = sess(tmp_path, exit_info={"returncode": 0xC0000142, "signal": None, "at": 1.0})
    assert win_crash.end_hints(unknown, {"path": "x"}) == [
        "exit code 3221225794 is 0xC0000142: a Windows exception or error status"]


@pytest.mark.parametrize("status", ["starting", "running"])
def test_a_session_whose_processes_are_gone_with_no_end_recorded_is_said_so(tmp_path, monkeypatch,
                                                                            status):
    monkeypatch.setattr(win_crash.scope, "is_gone", lambda unit: unit == "fso-harness-s1")
    lost = sess(tmp_path, status=status, exit_info=None)
    hints = win_crash.end_hints(lost, None)
    assert len(hints) == 1 and "end was never recorded" in hints[0] and "runner.log" in hints[0]
    still_there = sess(tmp_path, status=status, exit_info=None, scope="fso-harness-s2")
    assert win_crash.end_hints(still_there, None) == []
    monkeypatch.setattr(win_crash.scope, "is_gone", lambda unit: True)
    assert win_crash.end_hints(sess(tmp_path, status="exited", exit_info=None), None) == []


def test_the_report_tells_a_found_dump_from_a_missing_one_and_a_debug_log_from_none(
        tmp_runtime, tmp_path, windows_report, monkeypatch):
    s = _record(tmp_path)
    put(tmp_path, f"{STEM}_20261117_072630.mdmp")
    monkeypatch.setattr(win_crash, "find_debugger", lambda: None)
    hints = crash.report(s.id)["hints"]
    assert not any("no crash dump" in h for h in hints)          # there is one
    assert any("release build: no fs2_open.log" in h for h in hints)
    log = session.prefs_dir(s) / "data" / "fs2_open.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("FreeSpace 2 Open version: 26.2.0\n", encoding="utf-8")
    assert not any("release build" in h for h in crash.report(s.id)["hints"])


def test_a_run_the_failing_runner_ended_points_at_the_runners_log(tmp_path):
    ended = sess(tmp_path, ended_by="runner_error",
                 exit_info={"returncode": 1, "signal": None, "at": 1.0})
    hints = win_crash.end_hints(ended, None)
    assert len(hints) == 1 and "runner failed after starting the engine" in hints[0]
    assert "runner.log" in hints[0]


@pytest.mark.parametrize("ended_by, words", [
    ("abandoned_start", "never finished starting"),
    ("runner_lost", "its runner was gone"),
])
def test_a_session_a_cleanup_ended_says_which_rule_ended_it(tmp_path, ended_by, words):
    ended = sess(tmp_path, status="failed", ended_by=ended_by, exit_info=None)
    hints = win_crash.end_hints(ended, None)
    assert len(hints) == 1 and words in hints[0] and "cleanup" in hints[0]


def test_the_gone_hint_takes_the_sessions_own_answer_when_given(tmp_path, monkeypatch):
    # cross-family review XFA-6: from another Windows login the job is "not found" for a
    # live session too; the launcher knows better and passes its answer in.
    monkeypatch.setattr(win_crash.scope, "is_gone", lambda unit: True)
    running = sess(tmp_path, status="running", ended_by=None, exit_info=None)
    assert any("never recorded" in h for h in win_crash.end_hints(running, None))
    assert win_crash.end_hints(running, None, gone=False) == []
    assert any("never recorded" in h for h in win_crash.end_hints(running, None, gone=True))


def test_the_windows_report_is_given_in_the_sessions_own_login_while_its_runner_winds_down(
        tmp_runtime, tmp_path, windows_report, monkeypatch):
    # round-2 STF-1: for half a second after a crash the runner still runs. In the session's
    # own login the group is visible, and the report must be the report, not "runs in
    # another Windows login".
    s = _record(tmp_path)
    monkeypatch.setattr(platform, "GROUPS_PER_LOGIN", True)
    monkeypatch.setattr(crash.launcher, "_runner_known_alive", lambda rec: True)
    monkeypatch.setattr(crash.launcher.scope, "is_gone", lambda unit: False)   # seen here
    r = crash.report(s.id)
    assert "dump" in r and not any("another Windows login" in h for h in r["hints"])


def test_the_windows_report_asks_the_launcher_whether_the_session_is_gone(
        tmp_runtime, tmp_path, windows_report, monkeypatch):
    # final mutation run: nothing held the report to passing the launcher's answer on. A
    # record with no end whose processes cannot be asked about is not "gone" to the
    # launcher, though its job is not found; the hint must follow the launcher.
    s = _record(tmp_path, status="running", ended_by=None, exit_info=None)
    monkeypatch.setattr(crash.launcher, "_gone", lambda rec: False)
    assert not any("never recorded" in h for h in crash.report(s.id)["hints"])
    monkeypatch.setattr(crash.launcher, "_gone", lambda rec: True)
    assert any("never recorded" in h for h in crash.report(s.id)["hints"])
