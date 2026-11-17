"""Tests added for the 2026-11-13 round-3 mutation pass over the sandbox fix round (QA AT2-1,
AT2-2, CR2-2 and the helpers they added). Each test names the survivors it kills; the
survivors left after it are listed with reasons in the round's MUTATION line.
"""
import errno
import os
import secrets
import struct
from pathlib import Path

import pytest

from fsoctl import launcher, logs, paths, runner, safefs, sandbox, session


def _lstat_total(top: Path) -> tuple[int, int]:
    """Independent count for usage(): (sum of st_blocks*512 by lstat, entries) under `top`."""
    total, n = 0, 0
    for dirpath, dirnames, filenames in os.walk(top):
        for name in dirnames + filenames:
            st = os.lstat(os.path.join(dirpath, name))
            total, n = total + st.st_blocks * 512, n + 1
    return total, n


# ---- safefs.usage --------------------------------------------------------------------------------

def test_usage_counts_exactly_what_lstat_counts(tmp_path):
    # usage 2/3/14/22/23/26: totals and entry counts against an independent lstat walk, with a
    # link to a big file that must count as the link only.
    top = tmp_path / "prefs"
    (top / "a" / "b").mkdir(parents=True)
    (top / "a" / "f").write_bytes(b"x" * 50_000)
    (top / "a" / "b" / "g").write_bytes(b"y" * 9000)
    big = tmp_path / "big"
    big.write_bytes(b"z" * 300_000)
    (top / "link").symlink_to(big)
    assert safefs.usage(top, 100) == (*_lstat_total(top), False)


def test_usage_entry_cap_is_inclusive_and_says_so(tmp_path):
    # usage 15/16: exactly max_entries is allowed; one more is refused with EFBIG.
    top = tmp_path / "prefs"
    top.mkdir()
    for i in range(3):
        (top / f"f{i}").write_text("")
    assert safefs.usage(top, 3)[1] == 3
    with pytest.raises(safefs.Unsafe) as e:
        safefs.usage(top, 2)
    assert e.value.errno == errno.EFBIG and e.value.strerror == "more than 2 entries"


def test_usage_depth_bound(tmp_path, monkeypatch):
    # usage 5/28/29-32/36/37: a/b/c is three folder levels; a bound of 3 walks it, 2 refuses.
    top = tmp_path / "prefs"
    (top / "a" / "b" / "c").mkdir(parents=True)
    monkeypatch.setattr(safefs, "_USAGE_DEPTH", 3)
    assert safefs.usage(top, 100)[1] == 3
    monkeypatch.setattr(safefs, "_USAGE_DEPTH", 2)
    with pytest.raises(safefs.Unsafe) as e:
        safefs.usage(top, 100)
    assert e.value.errno == errno.ELOOP and e.value.strerror == "deeper than 2 folders"


def test_usage_skips_an_unreadable_folder_and_keeps_walking(tmp_path):
    # usage 38: "continue" turned into "break" stopped at the first folder it could not open.
    top = tmp_path / "prefs"
    (top / "a").mkdir(parents=True)
    (top / "b").mkdir()
    (top / "b" / "f").write_bytes(b"x" * 10_000)
    os.chmod(top / "a", 0)
    try:
        assert safefs.usage(top, 100)[1] == 3          # a, b, b/f
    finally:
        os.chmod(top / "a", 0o700)


# ---- safefs.remove_tree ------------------------------------------------------------------

def test_remove_tree_removes_a_chain_deeper_than_the_old_limit(tmp_path):
    # AT3-3 red test: 600 nested folders (the old remover refused past 256), some locked 000,
    # built with relative opens so no path gets long.
    top = tmp_path / "root" / "s1"
    top.mkdir(parents=True)
    fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
    for i in range(600):
        os.mkdir("d", dir_fd=fd)
        nxt = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
        if i % 100 == 99:
            os.chmod(f"/proc/self/fd/{fd}", 0)
        os.close(fd)
        fd = nxt
    with open(os.open("f", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd), "w") as f:
        f.write("deep")
    os.close(fd)
    safefs.remove_tree(tmp_path / "root", "s1")
    assert not top.exists()


def test_moving_up_retries_a_taken_name(tmp_path, monkeypatch):
    (tmp_path / "root" / "s1" / "a" / "b").mkdir(parents=True)
    real = os.rename
    calls = []

    def rename_once_taken(src, dst, **kw):
        calls.append(dst)
        if len(calls) == 1:
            raise OSError(errno.EEXIST, "taken")
        return real(src, dst, **kw)
    monkeypatch.setattr(safefs.os, "rename", rename_once_taken)
    safefs.remove_tree(tmp_path / "root", "s1")
    assert len(calls) == 2 and calls[0] != calls[1] and not (tmp_path / "root" / "s1").exists()


def test_clear_tree_keeps_the_folder(tmp_path):
    base = tmp_path / "prefs"
    (base / "x" / "y").mkdir(parents=True)
    (base / "x" / "y" / "f").write_text("x")
    os.chmod(base / "x", 0)
    os.chmod(base, 0)
    safefs.clear_tree(base)
    assert base.is_dir() and list(base.iterdir()) == []


def test_open_writable_dir_refuses_a_link_and_never_chmods_its_target(tmp_path, monkeypatch):
    # _open_writable_dir 8/9/15: a link (even to a folder) is refused, the target keeps its mode;
    # a real folder is made exactly 0700.
    target = tmp_path / "target"
    target.mkdir()
    os.chmod(target, 0o755)
    (tmp_path / "link").symlink_to(target)
    pfd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError) as e:
            safefs._open_writable_dir(pfd, "link")
        assert e.value.errno in (errno.ENOTDIR, errno.ELOOP)
        assert target.stat().st_mode & 0o777 == 0o755
        modes = []
        real = os.chmod
        monkeypatch.setattr(safefs.os, "chmod", lambda p, m: modes.append(m) or real(p, m))
        os.close(safefs._open_writable_dir(pfd, "target"))
        assert modes == [0o700] and target.stat().st_mode & 0o777 == 0o700
    finally:
        os.close(pfd)


# ---- safefs small paths ------------------------------------------------------------------------

def test_write_atomic_retry_path_keeps_mode_0644(tmp_path):
    # write_atomic 35/37: the create after removing a planted temp name uses mode 0644 too.
    (tmp_path / "f.json.tmp").symlink_to(tmp_path / "nowhere")
    old = os.umask(0o022)
    try:
        safefs.write_atomic(tmp_path.parent, f"{tmp_path.name}/f.json", b"x")
    finally:
        os.umask(old)
    assert (tmp_path / "f.json").stat().st_mode & 0o777 == 0o644


def test_a_file_that_grew_past_the_cap_is_refused_with_its_reason(tmp_path, monkeypatch):
    # read 17-20: the second check (after the read) carries EFBIG and its message.
    (tmp_path / "big").write_bytes(b"x" * 1000)
    real_fstat = os.fstat
    monkeypatch.setattr(safefs.os, "fstat", lambda fd: os.stat_result(
        (*real_fstat(fd)[:6], 10, *real_fstat(fd)[7:])))
    with pytest.raises(safefs.Unsafe) as e:
        safefs.read(tmp_path.parent, f"{tmp_path.name}/big", cap=100)
    assert e.value.errno == errno.EFBIG
    assert e.value.strerror == f"{tmp_path.name}/big is larger than 100 bytes"


def test_names_refusal_carries_efbig(tmp_path):
    for i in range(3):
        (tmp_path / f"f{i}").write_text("")
    with pytest.raises(safefs.Unsafe) as e:
        safefs.names(tmp_path.parent, tmp_path.name, limit=2)
    assert e.value.errno == errno.EFBIG


# ---- runner._watch and the watchdog -------------------------------------------------------------

def _sbx(tmp_path, network="sandbox"):
    return session.new_session(kind="game", binary="/bin/true", fs2path=str(tmp_path),
                               mods=[], mission=None, renderer="opengl", width=320, height=240,
                               switches={}, allow_eval=False, extra_args=[], network=network)


def test_watch_starts_a_watchdog_only_for_a_sandboxed_engine(tmp_runtime, tmp_path, monkeypatch):
    started = []

    def start(self):                  # a function, so it binds as a method
        started.append(self)
    monkeypatch.setattr(sandbox.Watchdog, "start", start)
    eng = sandbox.Engine(object(), 1, None)
    assert runner._watch(_sbx(tmp_path, "private"), eng) is None
    assert runner._watch(_sbx(tmp_path), object()) is None
    s = _sbx(tmp_path)
    dog = runner._watch(s, eng)
    assert started == [dog] and (dog.sid, dog.prefs, dog.engine) == (s.id,
                                                                    session.prefs_dir(s), eng)


def test_watchdog_thread_shape(tmp_path):
    dog = sandbox.Watchdog("s9", tmp_path, None)
    assert dog.daemon is True and dog.name == "prefs-watchdog-s9" and dog.tripped is None


def test_watchdog_keeps_watching_until_the_cap_is_passed(tmp_path, monkeypatch):
    # run 5: a clean check must not end the loop; the trip comes on the third pass.
    answers = iter([None, None, "too big"])
    tripped = []
    monkeypatch.setattr(sandbox, "WATCH_INTERVAL", 0.01)
    dog = sandbox.Watchdog("s2", tmp_path, None)
    monkeypatch.setattr(dog, "check", lambda: next(answers))
    monkeypatch.setattr(dog, "trip", tripped.append)
    dog.start()
    dog.join(5)
    assert tripped == ["too big"] and not dog.is_alive()


def test_watchdog_cap_is_exclusive(tmp_path, monkeypatch):
    # check 7: exactly the cap is still allowed.
    (tmp_path / "f").write_bytes(b"x" * 4096)
    size = safefs.usage(tmp_path, 100)[0]
    monkeypatch.setenv("FSO_HARNESS_SANDBOX_PREFS_MAX", str(size))
    assert sandbox.Watchdog("s3", tmp_path, None).check() is None


def test_a_one_byte_prefs_cap_is_honoured(monkeypatch):
    monkeypatch.setenv("FSO_HARNESS_SANDBOX_PREFS_MAX", "1")
    assert sandbox.prefs_max_bytes() == 1
    monkeypatch.delenv("FSO_HARNESS_SANDBOX_PREFS_MAX")
    assert sandbox.prefs_max_bytes() == sandbox.PREFS_MAX_BYTES


def test_warn_loudly_writes_stderr_and_the_journal(monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(sandbox.subprocess, "run", lambda argv, **kw: ran.append((argv, kw)))
    sandbox.warn_loudly("prefs too big")
    assert capsys.readouterr().err == "WARNING fso-harness: prefs too big\n"
    assert ran[0][0] == ["logger", "-p", "user.warning", "-t", "fso-harness", "prefs too big"]
    assert ran[0][1]["timeout"] == 5 and ran[0][1]["check"] is False


def test_warn_loudly_survives_a_missing_logger(monkeypatch, capsys):
    def missing(*a, **kw):
        raise FileNotFoundError("logger")
    monkeypatch.setattr(sandbox.subprocess, "run", missing)
    sandbox.warn_loudly("x")
    assert "WARNING fso-harness: x" in capsys.readouterr().err


# ---- paths.sandbox_root and session ids --------------------------------------------------------

def test_sandbox_root_default_locations(tmp_path, monkeypatch):
    monkeypatch.delenv("FSO_HARNESS_SANDBOX_ROOT", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg" / "deep"))
    root = paths.sandbox_root()
    assert root == tmp_path / "xdg" / "deep" / "fso-harness" / "sandbox-runs"
    assert root.stat().st_mode & 0o777 == 0o700
    monkeypatch.delenv("XDG_CACHE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert paths.sandbox_root() == tmp_path / "home" / ".cache" / "fso-harness" / "sandbox-runs"


def test_a_sandbox_id_taken_on_disk_is_not_reused(tmp_runtime, tmp_path, monkeypatch):
    # new_session 16: the id must be free in the run folder AND under the sandbox root.
    ids = iter(["aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(secrets, "token_hex", lambda n: next(ids))
    (paths.sandbox_root() / "saaaaaaaa").mkdir()
    s = _sbx(tmp_path)
    assert s.id == "sbbbbbbbb" and session.prefs_dir(s) == paths.sandbox_root() / s.id / "prefs"


def test_running_out_of_ids_says_so(tmp_runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(secrets, "token_hex", lambda n: "cccccccc")
    (paths.sandbox_root() / "scccccccc").mkdir()
    with pytest.raises(session.HarnessError) as e:
        _sbx(tmp_path)
    assert (e.value.code, e.value.message, e.value.hint) == (
        session.EXIT_NOT_FOUND, "could not allocate a session id", "retry")


# ---- logs._lines edges ---------------------------------------------------------------------------

def _read_lines(tmp_path, raw: bytes, limit: int, chunk: int, monkeypatch) -> list[str]:
    path = tmp_path / "log"
    path.write_bytes(raw)
    monkeypatch.setattr(logs, "_CHUNK", chunk)
    with open(path, "rb") as f:
        return list(logs._lines(f, limit))


@pytest.mark.parametrize("chunk", [1, 3, 1 << 20])
def test_lines_edges(tmp_path, monkeypatch, chunk):
    # _lines 19/20/21: a limit past the end keeps the last line; a limit short of the end stops
    # there. 29: a multi-byte character split across 1-byte chunks. 49/51/53: a truncated
    # character at the end still becomes U+FFFD.
    assert _read_lines(tmp_path, b"a\nb", 100, chunk, monkeypatch) == ["a", "b"]
    assert _read_lines(tmp_path, b"abcdef\n", 3, chunk, monkeypatch) == ["abc"]
    assert _read_lines(tmp_path, "é\nü".encode(), 100, chunk, monkeypatch) == ["é", "ü"]
    assert _read_lines(tmp_path, b"ok\n\xc3", 100, chunk, monkeypatch) == ["ok", "�"]


# ---- cleanup failure record --------------------------------------------------------------------

def test_cleanup_reports_a_folder_it_could_not_remove(tmp_runtime, tmp_path, monkeypatch):
    s = _sbx(tmp_path)
    s.status = "exited"
    session.save(s)

    def refuse(parent, name):
        raise OSError(errno.EBUSY, "busy")
    monkeypatch.setattr(launcher.safefs, "remove_tree", refuse)
    monkeypatch.setattr(launcher.scope, "is_gone", lambda unit: True)
    monkeypatch.setattr(launcher, "_cleanup_sinks", lambda kept, failed: {})
    out = launcher.cleanup()
    assert out["removed"] == [] and out["failed"] == [
        {"path": str(session.run_dir(s)), "error": "[Errno 16] busy"}]
    assert session.run_dir(s).exists()


# ---- seccomp_filter: what the seccomp program decides ------------------------------------------

def _run_bpf(program: bytes, arch: int, nr: int) -> int:
    """Interpret the classic-BPF subset seccomp_filter uses (LD|W|ABS, JEQ, JGE, RET with K)
    over a seccomp_data with this arch and syscall number; return the RET value. Bounded: the
    program counter only moves forward."""
    ins = [struct.unpack("<HBBI", program[i:i + 8]) for i in range(0, len(program), 8)]
    data = {0: nr, 4: arch}
    acc, pc = 0, 0
    while pc < len(ins):
        code, jt, jf, k = ins[pc]
        if code == 0x20:
            acc = data[k]
        elif code in (0x15, 0x35):
            hit = acc == k if code == 0x15 else acc >= k
            pc += jt if hit else jf
        elif code == 0x06:
            return k
        else:
            raise AssertionError(f"unexpected BPF code {code:#x}")
        pc += 1
    raise AssertionError("fell off the end of the program")


X86_64, I386, ALLOW, EPERM = 0xC000003E, 0x40000003, 0x7FFF0000, 0x00050001
EOPNOTSUPP, ENOSYS = 0x0005005F, 0x00050026


@pytest.mark.parametrize("arch, nr, want", [
    (X86_64, 0, ALLOW), (X86_64, 1, ALLOW), (X86_64, 247, ALLOW), (X86_64, 251, ALLOW),
    (X86_64, 248, EPERM), (X86_64, 249, EPERM), (X86_64, 250, EPERM),
    (X86_64, 0x40000000 | 1, EPERM), (X86_64, 0x40000000 | 250, EPERM),
    (I386, 288, EPERM), (I386, 4, EPERM),
    (X86_64, 285, EOPNOTSUPP), (X86_64, 0x40000000 | 285, EPERM), (I386, 324, EPERM),
    (X86_64, 425, ENOSYS), (X86_64, 426, ENOSYS), (X86_64, 427, ENOSYS), (X86_64, 424, ALLOW),
    (X86_64, 428, ALLOW), (X86_64, 188, EPERM), (X86_64, 189, EPERM), (X86_64, 190, EPERM),
    (X86_64, 463, EPERM), (X86_64, 191, ALLOW), (X86_64, 0x40000000 | 426, EPERM)])
def test_seccomp_filter_decisions(arch, nr, want):
    assert _run_bpf(sandbox.seccomp_filter(), arch, nr) == want


def test_seccomp_filter_is_nineteen_instructions_ending_in_returns():
    prog = sandbox.seccomp_filter()
    assert len(prog) == 19 * 8
    assert [struct.unpack("<HBBI", prog[i:i + 8]) for i in (120, 128, 136, 144)] == [
        (0x06, 0, 0, ALLOW), (0x06, 0, 0, EPERM), (0x06, 0, 0, EOPNOTSUPP),
        (0x06, 0, 0, ENOSYS)]


def test_seccomp_filter_bytes_are_pinned():
    # seccomp_filter 2/3/7/8: any change to the security filter's bytes must be deliberate.
    assert sandbox.seccomp_filter().hex() == (
        "2000000004000000" "1500000e3e0000c0" "2000000000000000" "35000c0000000040"
        "15000c001d010000" "15000c00a9010000" "15000b00aa010000" "15000a00ab010000"
        "15000700f8000000" "15000600f9000000" "15000500fa000000" "15000400bc000000"
        "15000300bd000000" "15000200be000000" "15000100cf010000" "060000000000ff7f"
        "0600000001000500" "060000005f000500" "0600000026000500")


def test_warn_loudly_survives_a_hung_logger(monkeypatch, capsys):
    # warn_loudly 9/11: a logger that times out must not break the watchdog.
    def hung(*a, **kw):
        raise sandbox.subprocess.TimeoutExpired("logger", 5)
    monkeypatch.setattr(sandbox.subprocess, "run", hung)
    sandbox.warn_loudly("y")
    assert "WARNING fso-harness: y" in capsys.readouterr().err


def test_trip_steps_that_fail_are_logged_and_the_rest_still_run(tmp_path, monkeypatch):
    # TA3-3: kill() raising inside the watchdog is logged; prefs are still emptied, and a
    # missing session record (nothing to mark) is logged too.
    (tmp_path / "junk").write_text("x")
    logged = []

    class Eng:
        def kill(self):
            raise OSError(errno.ESRCH, "gone")

        def exited(self):
            return True
    monkeypatch.setattr(sandbox, "warn_loudly", logged.append)
    sandbox.Watchdog("s4", tmp_path, Eng()).trip("too big")
    assert logged[0].startswith("session s4: watchdog could not record: HarnessError")
    assert logged[1] == "session s4: too big; killing the sandboxed engine"
    assert logged[2] == ("session s4: watchdog could not kill: "
                         "ProcessLookupError(3, 'gone')")
    assert list(tmp_path.iterdir()) == []


def test_sandbox_root_refusal_text(tmp_runtime, tmp_path, monkeypatch):
    binary = tmp_path / "fs2_open"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setattr(launcher.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(launcher.os, "geteuid", lambda: 0)
    with pytest.raises(session.HarnessError) as e:
        launcher._check_sandbox(launcher.LaunchRequest(binary=str(binary), network="sandbox"))
    assert (e.value.message, e.value.hint) == ("refusing to start a sandbox session as root",
                                               "run the harness as an ordinary user")
