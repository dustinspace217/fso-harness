"""win_safefs: the Windows twin of safefs (cross-platform Plan B, Task 3). It walks from a base
folder one component at a time and refuses anything reached through a link.

These tests run on every OS: the module's one OS-specific question, "is this path a link?",
is answered by _is_link, which reads Windows' reparse-point attribute there and the POSIX link
bit elsewhere. Each red proof of tests/test_safefs.py that applies to Windows is repeated here
by name. What Windows cannot give (no handle-relative opens, so a check and the open that
follows are two steps) is narrowed by comparing the opened file with the one that was checked;
the test for that swaps the file between the two steps."""
import errno
import os
import stat
import subprocess
import sys

import pytest

from fsoctl.platform import win_safefs as fs


def plant_link(target, link):
    """Make `link` point at `target` (a symbolic link; on Windows a junction for a folder when
    the account may not create symbolic links; a file link then skips the test)."""
    try:
        os.symlink(target, link, target_is_directory=os.path.isdir(target))
    except OSError:
        if sys.platform != "win32":
            raise
        if not os.path.isdir(target):
            pytest.skip("this Windows account may not create file symbolic links")
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True,
                       capture_output=True, timeout=30)


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


def canary(run):
    return run.parent / "canary"


def test_read_returns_a_regular_files_bytes_and_stat(run):
    (run / "prefs" / "data" / "a.json").write_bytes(b"{}")
    data, st = fs.read(run, "prefs/data/a.json")
    assert data == b"{}" and st.st_size == 2 and stat.S_ISREG(st.st_mode)


def test_read_refuses_a_link_as_the_file(run):
    plant_link(canary(run) / "secret.txt", run / "prefs" / "data" / "res.json")
    with pytest.raises(fs.Unsafe) as e:
        fs.read(run, "prefs/data/res.json")
    assert e.value.errno == errno.ELOOP and e.value.strerror == "res.json is a link"


def test_read_refuses_a_link_as_a_folder(run):
    os.rename(run / "prefs" / "data", run / "prefs" / "real")
    (run / "prefs" / "real" / "a.json").write_bytes(b"{}")
    plant_link(run / "prefs" / "real", run / "prefs" / "data")
    with pytest.raises(fs.Unsafe) as e:
        fs.read(run, "prefs/data/a.json")
    assert e.value.errno == errno.ENOTDIR and e.value.strerror == "data is not a plain folder"
    assert fs.is_file(run, "prefs/data/a.json") is False
    assert fs.read(run, "prefs/real/a.json")[0] == b"{}"            # the plain way still works


def test_read_refuses_a_file_over_the_cap(run):
    (run / "prefs" / "data" / "big").write_bytes(b"x" * 11)
    with pytest.raises(fs.Unsafe) as e:
        fs.read(run, "prefs/data/big", cap=10)
    assert e.value.errno == errno.EFBIG
    assert e.value.strerror == "prefs/data/big is larger than 10 bytes"
    assert fs.read(run, "prefs/data/big", cap=11)[0] == b"x" * 11


def test_read_refuses_a_folder_where_a_file_should_be(run):
    with pytest.raises(fs.Unsafe) as e:
        fs.read(run, "prefs/data")
    assert e.value.errno == errno.EINVAL and e.value.strerror == "data is not a regular file"


def test_a_file_swapped_for_a_link_after_the_check_is_refused(run, monkeypatch):
    # The gap Windows leaves: the check and the open are two steps. The engine swaps the
    # checked file for a link in between; the opened file is then not the checked one.
    target = run / "prefs" / "data" / "res.json"
    target.write_bytes(b"honest")
    real_open = fs._open_binary

    def swap_then_open(path):
        os.unlink(path)
        plant_link(canary(run) / "secret.txt", path)
        return real_open(path)
    monkeypatch.setattr(fs, "_open_binary", swap_then_open)
    with pytest.raises(fs.Unsafe) as e:
        fs.read(run, "prefs/data/res.json")
    assert e.value.errno == errno.ELOOP
    assert e.value.strerror == "res.json changed while it was being opened"


def test_read_tail_returns_the_end_and_the_size(run):
    (run / "prefs" / "data" / "log").write_bytes(b"0123456789")
    assert fs.read_tail(run, "prefs/data/log", 4) == (b"6789", 10)
    assert fs.read_tail(run, "prefs/data/log", 99) == (b"0123456789", 10)


@pytest.mark.parametrize("rel", ["", "/abs", "a//b", "a/./b", "a/../b", "..", "a\\b", "C:/x",
                                 "a/b:stream"])
def test_paths_must_be_plain_and_relative(run, rel):
    # backslash and colon too: on Windows they are a separator, a drive and a named stream
    with pytest.raises(ValueError) as e:
        fs.read(run, rel)
    assert str(e.value) == f"not a plain relative path: {rel!r}"


def test_write_atomic_never_touches_a_link_planted_at_a_guessable_temp_name(run):
    victim = canary(run) / "victim.txt"
    planted = run / "prefs" / "data" / "cmd.json.tmp"
    plant_link(victim, planted)
    fs.write_atomic(run, "prefs/data/cmd.json", b"NEW")
    assert victim.read_text() == "VICTIM-ORIGINAL"
    assert (run / "prefs" / "data" / "cmd.json").read_bytes() == b"NEW"
    assert os.path.islink(planted)                  # left as it was: not followed, not used


def test_write_atomic_uses_a_temp_name_nobody_can_know_in_advance_and_leaves_none(run,
                                                                                 monkeypatch):
    # Measured on real Windows (2026-11-17, the test below): an exclusive create FOLLOWS a
    # link whose target does not exist and creates the target. With a fixed temp name an
    # engine could plant such a link and have the harness's bytes written where it chose.
    real, made = os.open, []

    def note(path, flags, mode=0o777):
        made.append(os.path.basename(path))
        return real(path, flags, mode)
    monkeypatch.setattr(fs.os, "open", note)
    fs.write_atomic(run, "prefs/data/cmd.json", b"ONE")
    fs.write_atomic(run, "prefs/data/cmd.json", b"TWO")
    assert len(made) == 2 and made[0] != made[1]
    for name in made:
        assert name.startswith("cmd.json.") and name.endswith(".tmp") and len(name) == 29
    assert sorted(os.listdir(run / "prefs" / "data")) == ["cmd.json"]
    assert (run / "prefs" / "data" / "cmd.json").read_bytes() == b"TWO"


def test_write_atomic_leaves_no_temp_file_when_the_move_fails(run, monkeypatch):
    # round-2 CRF-8: a random name is never reused, so a temp file whose move failed stayed
    # for good (with the old fixed name the next write took its place)
    fs.write_atomic(run, "prefs/data/cmd.json", b"ONE")

    def refuse(src, dst):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(fs.os, "replace", refuse)
    with pytest.raises(PermissionError):
        fs.write_atomic(run, "prefs/data/cmd.json", b"TWO")
    assert sorted(os.listdir(run / "prefs" / "data")) == ["cmd.json"]
    assert (run / "prefs" / "data" / "cmd.json").read_bytes() == b"ONE"


def test_write_atomic_reports_the_first_error_when_the_tidying_fails_too(run, monkeypatch):
    def refuse_move(src, dst):
        raise PermissionError(13, "the move was refused")

    def refuse_unlink(path, **keywords):
        raise OSError(5, "the temp file could not be removed")
    monkeypatch.setattr(fs.os, "replace", refuse_move)
    monkeypatch.setattr(fs.os, "unlink", refuse_unlink)
    with pytest.raises(PermissionError, match="the move was refused"):
        fs.write_atomic(run, "prefs/data/cmd.json", b"ONE")


def test_write_atomic_does_not_write_through_a_planted_temp_link_to_a_file_not_yet_there(run):
    # QA Plan B, SA-4: the test above plants a link to a file that EXISTS, so the exclusive
    # create fails whether or not the link is followed, and it cannot tell which happened.
    # A link to a file that does not exist decides it: followed, the harness's bytes would
    # land in a new file at a place the engine chose.
    nowhere = canary(run) / "made-by-the-harness.txt"
    plant_link(nowhere, run / "prefs" / "data" / "cmd.json.tmp")
    fs.write_atomic(run, "prefs/data/cmd.json", b"NEW")
    assert not nowhere.exists()
    target = run / "prefs" / "data" / "cmd.json"
    assert target.read_bytes() == b"NEW" and not os.path.islink(target)


def test_write_atomic_replaces_a_planted_target_link(run):
    victim = canary(run) / "victim.txt"
    plant_link(victim, run / "prefs" / "data" / "cmd.json")
    fs.write_atomic(run, "prefs/data/cmd.json", b"NEW")
    assert victim.read_text() == "VICTIM-ORIGINAL"
    assert not fs._is_link(run / "prefs" / "data" / "cmd.json")
    assert (run / "prefs" / "data" / "cmd.json").read_bytes() == b"NEW"


def test_a_write_never_leaves_a_half_written_file(run, monkeypatch):
    target = run / "prefs" / "data" / "cmd.json"
    target.write_bytes(b"OLD")

    class Breaks:
        """A file whose write fails after the temp file was created."""
        def __init__(self, f):
            self.f = f
        def __enter__(self):
            return self
        def __exit__(self, *a):
            self.f.close()
        def write(self, data):
            self.f.write(data[:1])
            raise OSError(errno.ENOSPC, "No space left on device")
    real_fdopen = os.fdopen
    monkeypatch.setattr(fs.os, "fdopen", lambda fd, mode: Breaks(real_fdopen(fd, mode)))
    with pytest.raises(OSError):
        fs.write_atomic(run, "prefs/data/cmd.json", b"NEW-CONTENT")
    assert target.read_bytes() == b"OLD"                # the reader never sees a part


def test_create_refuses_a_link_where_a_folder_should_be(run):
    plant_link(canary(run), run / "prefs" / "mod")
    with pytest.raises(fs.Unsafe), fs.folder(run, "prefs/mod/data/cache", create=True):
        pass
    assert sorted(p.name for p in canary(run).iterdir()) == ["secret.txt", "victim.txt"]


def test_create_makes_missing_folders_and_write_makes_none_unless_asked(run):
    with fs.folder(run, "prefs/mod/data/cache", create=True):
        pass
    assert (run / "prefs" / "mod" / "data" / "cache").is_dir()
    with pytest.raises(FileNotFoundError):
        fs.write_atomic(run, "prefs/new/x.json", b"1")
    fs.write_atomic(run, "prefs/new/x.json", b"1", create_dirs=True)
    assert (run / "prefs" / "new" / "x.json").read_bytes() == b"1"


def test_a_base_folder_that_is_a_link_is_refused(run, tmp_path):
    plant_link(run, tmp_path / "linked-run")
    with pytest.raises(fs.Unsafe):
        fs.read(tmp_path / "linked-run", "prefs/data/a.json")


def test_unlink_and_rename_act_on_the_link_itself(run):
    d = run / "prefs" / "data"
    victim = canary(run) / "victim.txt"
    plant_link(victim, d / "l1")
    plant_link(victim, d / "l2")
    assert fs.unlink(run, "prefs/data/l1") is True and victim.exists()
    assert fs.unlink(run, "prefs/data/l1") is False
    fs.rename(run, "prefs/data/l2", "l3")
    assert fs._is_link(d / "l3") and victim.read_text() == "VICTIM-ORIGINAL"
    (d / "a").write_bytes(b"1")
    (d / "b").write_bytes(b"2")
    fs.rename(run, "prefs/data/a", "b")                 # replaces, as rename does on Linux
    assert (d / "b").read_bytes() == b"1" and not (d / "a").exists()
    with pytest.raises(FileNotFoundError):
        fs.rename(run, "prefs/data/gone", "x")


def test_is_file_only_for_regular_files(run):
    d = run / "prefs" / "data"
    (d / "f").write_bytes(b"")
    plant_link(d / "f", d / "l")
    assert fs.is_file(run, "prefs/data/f") is True
    assert fs.is_file(run, "prefs/data/l") is False
    assert fs.is_file(run, "prefs/data") is False
    assert fs.is_file(run, "prefs/data/none") is False
    assert fs.is_file(run, "../x") is False


def test_names_lists_a_folder_and_stops_past_its_limit(run):
    d = run / "prefs" / "data"
    for n in ("b", "a", "c"):
        (d / n).write_bytes(b"")
    assert fs.names(run, "prefs/data") == ["a", "b", "c"]
    assert fs.names(run, "prefs/none") == []
    with pytest.raises(fs.Unsafe) as e:
        fs.names(run, "prefs/data", limit=2)
    assert e.value.errno == errno.EFBIG


def test_find_skips_links_and_finds_the_file(run):
    (canary(run) / "model.pof").write_bytes(b"x")
    plant_link(canary(run), run / "prefs" / "linked")
    assert fs.find(run, "prefs", "model.pof") is None
    (run / "prefs" / "data" / "models").mkdir()
    (run / "prefs" / "data" / "models" / "model.pof").write_bytes(b"x")
    assert fs.find(run, "prefs", "model.pof") == "prefs/data/models/model.pof"
    assert fs.find(run, None, "nothing.pof") is None


def test_remove_tree_never_follows_links_and_removes_read_only_files(tmp_path):
    root = tmp_path / "root"
    victim_dir = tmp_path / "victim"
    (root / "s1" / "deep" / "er").mkdir(parents=True)
    victim_dir.mkdir()
    (victim_dir / "keep.txt").write_text("KEEP")
    locked = root / "s1" / "deep" / "locked.txt"
    locked.write_text("x")
    os.chmod(locked, stat.S_IREAD)                      # Windows refuses to delete these as is
    plant_link(victim_dir, root / "s1" / "deep" / "er" / "link")
    fs.remove_tree(root, "s1")
    assert not (root / "s1").exists()
    assert (victim_dir / "keep.txt").read_text() == "KEEP"
    fs.remove_tree(root, "s1")                          # a missing name is not an error


def test_remove_tree_of_a_link_removes_only_the_link(tmp_path):
    root = tmp_path / "root"
    victim_dir = tmp_path / "victim"
    root.mkdir()
    victim_dir.mkdir()
    (victim_dir / "keep.txt").write_text("KEEP")
    plant_link(victim_dir, root / "s1")
    fs.remove_tree(root, "s1")
    assert not os.path.lexists(root / "s1") and (victim_dir / "keep.txt").exists()


def test_remove_tree_is_bounded_by_steps(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "s1").mkdir(parents=True)
    for n in range(5):
        (root / "s1" / f"f{n}").write_bytes(b"")
    monkeypatch.setattr(fs, "_REMOVE_OPS", 3)
    with pytest.raises(fs.Unsafe) as e:
        fs.remove_tree(root, "s1")
    assert e.value.errno == errno.EFBIG and "more than 3 removal steps" in e.value.strerror


