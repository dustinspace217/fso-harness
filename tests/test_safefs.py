"""safefs: host file access in a run folder that an engine cannot redirect (QA 2026-11-13 SA-1,
SA-2, AT-2, AT-3, CR-1, TA-1).

Every planted link points at a canary under the test's own temp folder, never at a real file.
The red-proof tests swap in a link-FOLLOWING variant of safefs and check that the same planted
link then does reach the canary: without that, a passing test could not tell "safe" from
"never exercised".
"""
import contextlib
import ctypes
import errno
import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from fsoctl import channel, record, safefs, session
from fsoctl.errors import EXIT_BRIDGE, EXIT_REFUSED, HarnessError


@pytest.fixture
def run(tmp_path):
    """A run folder with a canary folder beside it: secret.txt and victim.txt."""
    r = tmp_path / "run"
    (r / "prefs" / "data").mkdir(parents=True)
    canary = tmp_path / "canary"
    canary.mkdir()
    (canary / "secret.txt").write_text("CANARY-SECRET")
    (canary / "victim.txt").write_text("VICTIM-ORIGINAL")
    return r


def _canary(run: Path) -> Path:
    return run.parent / "canary"


def test_read_returns_a_regular_files_bytes_and_stat(run):
    (run / "prefs" / "data" / "a.json").write_bytes(b"{}")
    data, st = safefs.read(run, "prefs/data/a.json")
    assert data == b"{}" and st.st_size == 2


def test_read_refuses_a_link_as_the_file(run):
    (run / "prefs" / "data" / "a.json").symlink_to(_canary(run) / "secret.txt")
    with pytest.raises(safefs.Unsafe):
        safefs.read(run, "prefs/data/a.json")


def test_read_refuses_a_link_as_a_folder(run):
    os.rename(run / "prefs" / "data", run / "prefs" / "real")
    (run / "prefs" / "data").symlink_to(_canary(run))
    with pytest.raises(safefs.Unsafe):
        safefs.read(run, "prefs/data/secret.txt")


def test_read_refuses_a_fifo_without_waiting(run):
    os.mkfifo(run / "prefs" / "data" / "a.json")
    start = time.monotonic()
    with pytest.raises(safefs.Unsafe):
        safefs.read(run, "prefs/data/a.json")
    assert time.monotonic() - start < 1


def test_read_refuses_a_link_to_dev_zero(run):
    (run / "prefs" / "data" / "a.json").symlink_to("/dev/zero")
    with pytest.raises(safefs.Unsafe):
        safefs.read(run, "prefs/data/a.json")


def test_read_refuses_a_file_over_the_cap(run):
    (run / "prefs" / "data" / "big").write_bytes(b"x" * 101)
    with pytest.raises(safefs.Unsafe):
        safefs.read(run, "prefs/data/big", cap=100)
    assert safefs.read(run, "prefs/data/big", cap=101)[0] == b"x" * 101


def test_read_tail_returns_the_end_and_the_size(run):
    (run / "prefs" / "log").write_bytes(b"0123456789")
    assert safefs.read_tail(run, "prefs/log", 4) == (b"6789", 10)
    assert safefs.read_tail(run, "prefs/log", 40) == (b"0123456789", 10)


@pytest.mark.parametrize("rel", ["", "/etc/passwd", "../x", "prefs/../x", "prefs//x", "./x"])
def test_paths_must_be_plain_and_relative(run, rel):
    with pytest.raises(ValueError):
        safefs.read(run, rel)


def test_write_atomic_replaces_a_planted_temp_link_instead_of_following_it(run):
    d = run / "prefs" / "data"
    (d / "cmd.json.tmp").symlink_to(_canary(run) / "victim.txt")
    safefs.write_atomic(run, "prefs/data/cmd.json", b"NEW")
    assert (_canary(run) / "victim.txt").read_text() == "VICTIM-ORIGINAL"
    assert (d / "cmd.json").read_bytes() == b"NEW" and not (d / "cmd.json.tmp").exists()


def test_write_atomic_replaces_a_planted_target_link(run):
    d = run / "prefs" / "data"
    (d / "cmd.json").symlink_to(_canary(run) / "victim.txt")
    safefs.write_atomic(run, "prefs/data/cmd.json", b"NEW")
    assert (_canary(run) / "victim.txt").read_text() == "VICTIM-ORIGINAL"
    assert not (d / "cmd.json").is_symlink() and (d / "cmd.json").read_bytes() == b"NEW"


def test_write_atomic_replaces_a_planted_temp_fifo(run):
    os.mkfifo(run / "prefs" / "data" / "cmd.json.tmp")
    safefs.write_atomic(run, "prefs/data/cmd.json", b"NEW")
    assert (run / "prefs" / "data" / "cmd.json").read_bytes() == b"NEW"


def test_create_refuses_a_link_where_a_folder_should_be(run):
    # Phase B refinement a: mkdir's "exists" also answers for a link; the reopen must refuse it.
    (run / "prefs" / "cache").symlink_to(_canary(run))
    with pytest.raises(safefs.Unsafe), safefs.folder(run, "prefs/cache/x", create=True):
        pass
    assert sorted(p.name for p in _canary(run).iterdir()) == ["secret.txt", "victim.txt"]


def test_create_makes_missing_folders(run):
    with safefs.folder(run, "prefs/a/b/c", create=True):
        pass
    assert (run / "prefs" / "a" / "b" / "c").is_dir()


def test_unlink_and_rename_act_on_the_link_itself(run):
    d = run / "prefs" / "data"
    (d / "l").symlink_to(_canary(run) / "victim.txt")
    safefs.rename(run, "prefs/data/l", "m")
    assert (d / "m").is_symlink()
    assert safefs.unlink(run, "prefs/data/m") is True
    assert safefs.unlink(run, "prefs/data/m") is False
    assert (_canary(run) / "victim.txt").read_text() == "VICTIM-ORIGINAL"


def test_is_file_only_for_regular_files(run):
    d = run / "prefs" / "data"
    (d / "f").write_text("x")
    (d / "l").symlink_to(d / "f")
    os.mkfifo(d / "p")
    assert safefs.is_file(run, "prefs/data/f")
    assert not safefs.is_file(run, "prefs/data/l")
    assert not safefs.is_file(run, "prefs/data/p")
    assert not safefs.is_file(run, "prefs/data/missing")
    assert not safefs.is_file(run, "../x")


def test_find_skips_links_and_finds_the_file(run):
    (run / "prefs" / "data" / "deep").mkdir()
    (run / "prefs" / "data" / "deep" / "pofspew.txt").write_text("x")
    (run / "prefs" / "a-link").symlink_to(_canary(run))
    (_canary(run) / "pofspew.txt").write_text("CANARY")
    assert safefs.find(run, "prefs", "pofspew.txt") == "prefs/data/deep/pofspew.txt"
    (run / "prefs" / "data" / "deep" / "pofspew.txt").unlink()
    assert safefs.find(run, "prefs", "pofspew.txt") is None


def test_names_lists_a_folder(run):
    (run / "prefs" / "data" / "b").write_text("")
    (run / "prefs" / "data" / "a").write_text("")
    assert safefs.names(run, "prefs/data") == ["a", "b"]
    assert safefs.names(run, "prefs/none") == []


# ---- red proofs: a following open DOES reach the canary --------------------------------------

def _following(monkeypatch):
    """Turn safefs into what it replaces: opens that follow links."""
    monkeypatch.setattr(safefs, "_DIR_FLAGS", os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(safefs, "_NEW_FLAGS", os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    monkeypatch.setattr(safefs, "_READ_FLAGS", os.O_RDONLY)
    monkeypatch.setattr(safefs, "_open_regular",
                        lambda dfd, name: os.open(name, os.O_RDONLY, dir_fd=dfd))


def test_red_proof_a_following_write_clobbers_the_victim(run, monkeypatch):
    _following(monkeypatch)
    (run / "prefs" / "data" / "cmd.json.tmp").symlink_to(_canary(run) / "victim.txt")
    with contextlib.suppress(FileExistsError):
        safefs.write_atomic(run, "prefs/data/cmd.json", b"NEW")
    # With the guard gone the same plant reaches the victim (or the rename moves it away).
    victim = _canary(run) / "victim.txt"
    assert not victim.exists() or victim.read_text() != "VICTIM-ORIGINAL"


def test_red_proof_a_following_read_returns_the_secret(run, monkeypatch):
    _following(monkeypatch)
    (run / "prefs" / "data" / "a.json").symlink_to(_canary(run) / "secret.txt")
    assert safefs.read(run, "prefs/data/a.json")[0] == b"CANARY-SECRET"


# ---- the race (Phase B, test seat): a folder swapped for a link while commands are sent -----

_RENAME_EXCHANGE = 2          # renameat2 flag (linux/fs.h): swap two names atomically
_AT_FDCWD = -100


def _swap(cache: Path, link: Path, stop: threading.Event, done: list) -> None:
    """Atomically exchange `cache` (a real folder) and `link` (a link to the canary folder)
    until `stop` is set (bounded: 200,000 exchanges). Atomic, so `cache` is never missing: a
    missing folder would be re-created by send() and stall a rename-based swapper. Appends the
    number of exchanges that succeeded (renameat2 returned 0) to `done` when it ends."""
    libc = ctypes.CDLL(None, use_errno=True)
    succeeded = 0
    for _ in range(200_000):
        if stop.is_set():
            break
        rc = libc.renameat2(_AT_FDCWD, bytes(cache), _AT_FDCWD, bytes(link), _RENAME_EXCHANGE)
        succeeded += rc == 0
    done.append(succeeded)


def test_channel_writes_never_land_in_a_folder_swapped_for_a_link(tmp_runtime, tmp_path):
    s = session.new_session(kind="game", binary="/bin/true", fs2path="/tmp", mods=[],
                            mission=None, renderer="opengl", width=320, height=240,
                            switches={}, allow_eval=False, extra_args=[], network="sandbox")
    ch = channel.Channel(s)
    ch.dir.mkdir(parents=True)
    canary = tmp_path / "race-canary"
    canary.mkdir()
    # The folder's change times, not only its contents: a command written through the link is
    # withdrawn and deleted at once (timeout 0), so an empty folder at the end would miss it;
    # any create, rename or unlink inside updates both times.
    before = os.stat(canary)
    stop, done = threading.Event(), []
    link = ch.dir.with_name("cache-link")
    link.symlink_to(canary)
    t = threading.Thread(target=_swap, args=(ch.dir, link, stop, done))
    t.start()
    swaps_left, tampered = True, 0
    try:
        for _ in range(500):                       # 500 commands while the folder toggles
            try:
                ch.send("state", timeout=0.0)
            except HarnessError as e:
                tampered += e.details.get("kind") == channel.TAMPERED
        swaps_left = not done                      # the swapper was still running throughout
    finally:
        stop.set()
        t.join(30)
    # TA2-2: the race really ran: exchanges succeeded, and some sends met the link and were
    # refused as tampering (a test where neither happens proves nothing).
    assert swaps_left and done and done[0] > 0 and tampered > 0, (done, tampered)
    after = os.stat(canary)
    assert list(canary.iterdir()) == [], "a command file landed through the swapped link"
    assert (after.st_mtime_ns, after.st_ctime_ns) == (before.st_mtime_ns, before.st_ctime_ns), \
        "something was written into the canary folder through the swapped link"


# ---- tests added for the mutation pass --------------------------------------------------------

def test_refusals_carry_their_errno_and_say_why(run):
    d = run / "prefs" / "data"
    (d / "l").symlink_to(_canary(run) / "secret.txt")
    os.mkfifo(d / "p")
    (d / "big").write_bytes(b"xx")
    os.rename(d, run / "prefs" / "real")
    (run / "prefs" / "data").symlink_to(run / "prefs" / "real")
    cases = [("prefs/real/l", errno.ELOOP, "l is a link"),
             ("prefs/real/p", errno.EINVAL, "p is not a regular file"),
             ("prefs/data/l", errno.ENOTDIR, "data is not a plain folder")]   # O_DIRECTORY wins
    for rel, code, words in cases:
        with pytest.raises(safefs.Unsafe) as e:
            safefs.read(run, rel)
        assert e.value.errno == code and e.value.strerror == words, rel
    with pytest.raises(safefs.Unsafe) as e:
        safefs.read(run, "prefs/real/big", cap=1)
    assert e.value.errno == errno.EFBIG
    assert e.value.strerror == "prefs/real/big is larger than 1 bytes"
    with pytest.raises(ValueError) as v:
        safefs.read(run, "../x")
    assert str(v.value) == "not a plain relative path: '../x'"


def test_find_stops_after_its_entry_budget(run, monkeypatch):
    d = run / "prefs" / "data"
    for name in ("a", "b", "pofspew.txt"):
        (d / name).write_text("x")
    monkeypatch.setattr(safefs, "_FIND_ENTRIES", 3)
    assert safefs.find(run, "prefs/data", "pofspew.txt") == "prefs/data/pofspew.txt"
    monkeypatch.setattr(safefs, "_FIND_ENTRIES", 2)
    assert safefs.find(run, "prefs/data", "pofspew.txt") is None


def test_find_returns_only_the_named_file(run):
    (run / "prefs" / "aaa.txt").write_text("x")
    assert safefs.find(run, "prefs", "pofspew.txt") is None


def test_created_folders_and_files_get_fixed_modes(run):
    old = os.umask(0o022)
    try:
        safefs.write_atomic(run, "prefs/new/sub/f.json", b"x", create_dirs=True)
    finally:
        os.umask(old)
    assert (run / "prefs" / "new").stat().st_mode & 0o777 == 0o755
    assert (run / "prefs" / "new" / "sub" / "f.json").stat().st_mode & 0o777 == 0o644


def test_write_atomic_makes_no_folder_unless_asked(run):
    with pytest.raises(FileNotFoundError):
        safefs.write_atomic(run, "prefs/missing/f.json", b"x")
    assert not (run / "prefs" / "missing").exists()


# ---- mutation pass: the channel's new error paths ----------------------------------------------

def _live_channel(tmp_runtime) -> channel.Channel:
    s = session.new_session(kind="game", binary="/bin/true", fs2path="/tmp", mods=[],
                            mission=None, renderer="opengl", width=320, height=240,
                            switches={}, allow_eval=False, extra_args=[], network="sandbox")
    ch = channel.Channel(s)
    ch.dir.mkdir(parents=True)
    return ch


@pytest.mark.parametrize("reply, message, hint", [
    (b"not json", "unreadable reply to state: ", "the bridge wrote a reply that is not JSON"),
    (b"[1]", "unreadable reply to state", "the bridge wrote a reply that is not a JSON object")])
def test_an_unreadable_reply_is_a_bridge_error(tmp_runtime, reply, message, hint):
    ch = _live_channel(tmp_runtime)
    (ch.dir / "res-000001.json").write_bytes(reply)
    with pytest.raises(HarnessError) as e:
        ch.send("state", timeout=1)
    assert e.value.code == EXIT_BRIDGE and e.value.message.startswith(message)
    assert e.value.hint == hint


def test_a_refusal_without_text_says_refused(tmp_runtime):
    ch = _live_channel(tmp_runtime)
    (ch.dir / "res-000001.json").write_bytes(b'{"ok": false}')
    with pytest.raises(HarnessError) as e:
        ch.send("state", timeout=1)
    assert (e.value.message, e.value.hint) == ("refused", "")


def test_a_linked_channel_folder_is_reported_as_tampering(tmp_runtime, tmp_path):
    # SA2-2: a distinct, machine-readable result the nightly holds on instead of retrying.
    ch = _live_channel(tmp_runtime)
    ch.dir.rmdir()
    ch.dir.symlink_to(tmp_path)
    with pytest.raises(HarnessError) as e:
        ch.send("state", timeout=1)
    assert e.value.code == EXIT_REFUSED and e.value.details == {"kind": "channel_tampered"}
    assert e.value.message == "channel tampered: cache is not a plain folder"
    assert e.value.hint.startswith("the engine left a link, a FIFO or another non-file") \
        and "do not retry" in e.value.hint
    assert ch.reap_reply(1, within=0.1) is False


def test_a_planted_reply_link_is_tampering(tmp_runtime, tmp_path):
    ch = _live_channel(tmp_runtime)
    (ch.dir / "res-000001.json").symlink_to(tmp_path / "secret")
    with pytest.raises(HarnessError) as e:
        ch.send("state", timeout=1)
    assert e.value.details == {"kind": "channel_tampered"}
    assert e.value.message == "channel tampered: res-000001.json is a link"


def test_other_channel_errors_stay_plain_unusable():
    err = channel._unusable(FileNotFoundError(2, "gone"))
    assert err.code == EXIT_REFUSED and err.details == {}
    assert err.message == "the session's command channel is unusable: [Errno 2] gone"
    assert err.hint == "stop the session (`fsoctl stop <id>`)"


def test_reap_reply_removes_a_late_reply(tmp_runtime):
    ch = _live_channel(tmp_runtime)
    assert ch.reap_reply(3, within=0.1) is False
    (ch.dir / "res-000003.json").write_text("{}")
    assert ch.reap_reply(3, within=0.1) is True
    assert not (ch.dir / "res-000003.json").exists()


def test_popups_skip_bad_lines_and_keep_reading(tmp_runtime):
    ch = _live_channel(tmp_runtime)
    (ch.dir / "popups.txt").write_bytes(b'not json\n{"a": 1}\n\n{"b": "\xff"}\n')
    assert ch.popups() == [{"a": 1}, {"b": "\ufffd"}]   # undecodable bytes become U+FFFD
    (ch.dir / "popups.txt").write_bytes(b'{"a": 1}\n{"b": 2}\n')
    assert ch.popups() == [{"a": 1}, {"b": 2}]


# ---- AT2-2: removal an engine's chmod cannot block --------------------------------------------

def test_remove_tree_beats_mode_000_and_never_follows_links(tmp_path):
    canary = tmp_path / "canary"
    canary.mkdir()
    (canary / "keep.txt").write_text("keep")
    top = tmp_path / "root" / "s1"
    deep = top / "prefs" / "data" / "deep"
    deep.mkdir(parents=True)
    (deep / "f").write_text("x")
    (top / "prefs" / "data" / "link").symlink_to(canary)
    os.mkfifo(top / "prefs" / "fifo")
    os.chmod(deep, 0)
    os.chmod(top / "prefs" / "data", 0)
    os.chmod(top / "prefs", 0)                         # prefs itself too
    safefs.remove_tree(tmp_path / "root", "s1")
    assert not top.exists()
    assert (canary / "keep.txt").read_text() == "keep"
    safefs.remove_tree(tmp_path / "root", "s1")       # already gone: no error


def test_red_proof_rmtree_fails_on_mode_000(tmp_path):
    top = tmp_path / "s1"
    (top / "prefs" / "data").mkdir(parents=True)
    (top / "prefs" / "data" / "f").write_text("x")
    os.chmod(top / "prefs", 0)
    try:
        with pytest.raises(PermissionError):
            shutil.rmtree(top)
    finally:
        os.chmod(top / "prefs", 0o700)


def test_remove_tree_is_bounded_by_steps_not_depth(tmp_path, monkeypatch):
    # AT3-3: no depth refusal any more; the bound is the number of removal steps.
    deep = tmp_path / "root" / "s1"
    for _ in range(4):
        deep = deep / "d"
    deep.mkdir(parents=True)
    monkeypatch.setattr(safefs, "_REMOVE_OPS", 2)
    with pytest.raises(safefs.Unsafe) as e:
        safefs.remove_tree(tmp_path / "root", "s1")
    assert e.value.errno == errno.EFBIG and e.value.strerror == "more than 2 removal steps"
    monkeypatch.setattr(safefs, "_REMOVE_OPS", 100)
    safefs.remove_tree(tmp_path / "root", "s1")
    assert not (tmp_path / "root" / "s1").exists()


def test_remove_tree_removes_more_than_one_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(safefs, "_REMOVE_BATCH", 3)
    top = tmp_path / "root" / "s1"
    top.mkdir(parents=True)
    for i in range(10):
        (top / f"f{i}").write_text("x")
    safefs.remove_tree(tmp_path / "root", "s1")
    assert not top.exists()


# ---- CR2-1/AT2-3: deeply nested JSON from the engine -------------------------------------------

_DEEP = b"[" * 100_000


def test_deep_json_raises_recursion_error_on_this_python():
    # The premise the fix rests on: this is not a ValueError.
    with pytest.raises(RecursionError):
        json.loads(_DEEP)


def test_deep_json_in_engine_files_is_handled_everywhere(tmp_runtime):
    ch = _live_channel(tmp_runtime)
    (ch.dir / "ready.json").write_bytes(_DEEP)
    (ch.dir / "popups.txt").write_bytes(_DEEP + b'\n{"a": 1}\n')
    (ch.dir / "res-000001.json").write_bytes(_DEEP)
    assert ch.heartbeat() is None
    assert ch.popups() == [{"a": 1}]
    assert record._current_state(ch.sess) is None
    with pytest.raises(HarnessError) as e:
        ch.send("state", timeout=1)
    assert e.value.code == EXIT_BRIDGE and e.value.message.startswith("unreadable reply to state")


# ---- TA2-4: reads stay capped even when the size check is fooled ------------------------------

class _ReadSpy:
    """Wraps a binary file and records the size every read() asked for."""
    def __init__(self, f, asked):
        self.f, self.asked = f, asked

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.f.close()

    def fileno(self):
        return self.f.fileno()

    def seek(self, *a):
        return self.f.seek(*a)

    def read(self, n=-1):
        self.asked.append(n)
        return self.f.read(n)


def _spy(monkeypatch):
    asked = []
    real = os.fdopen
    monkeypatch.setattr(safefs.os, "fdopen", lambda fd, mode: _ReadSpy(real(fd, mode), asked))
    return asked


def test_read_asks_for_at_most_the_cap_even_if_the_file_grew(run, monkeypatch):
    # read_13: f.read(None) would read a file that grew after the size check whole.
    (run / "prefs" / "big").write_bytes(b"x" * 1000)
    asked = _spy(monkeypatch)
    real_fstat = os.fstat

    def small_fstat(fd):                      # the file "was" 10 bytes at the check
        st = real_fstat(fd)
        return os.stat_result((st.st_mode, *st[1:6], 10, *st[7:]))
    monkeypatch.setattr(safefs.os, "fstat", small_fstat)
    with pytest.raises(safefs.Unsafe):
        safefs.read(run, "prefs/big", cap=100)
    assert asked == [101]


def test_read_tail_asks_for_exactly_nbytes(run, monkeypatch):
    # read_tail_14: f.read(None) would read from the seek point to whatever the end became.
    (run / "prefs" / "log").write_bytes(b"0123456789")
    asked = _spy(monkeypatch)
    assert safefs.read_tail(run, "prefs/log", 4) == (b"6789", 10)
    assert asked == [4]


def test_names_stops_counting_past_its_limit(run):
    # CR2-4: an engine-made folder is never listed whole.
    for i in range(5):
        (run / "prefs" / "data" / f"f{i}").write_text("")
    assert safefs.names(run, "prefs/data", limit=5) == [f"f{i}" for i in range(5)]
    with pytest.raises(safefs.Unsafe) as e:
        safefs.names(run, "prefs/data", limit=4)
    assert e.value.strerror == "more than 4 entries in prefs/data"
