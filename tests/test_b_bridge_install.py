"""Bridge install/refresh safety (QA SL-3 / CR-8 / MU-1 / TA-17, contract C5)."""
import fcntl
import hashlib
import os
import shutil
import threading
from pathlib import Path

import pytest

from fsoctl import bridge_install, paths, scope
from fsoctl.errors import EXIT_REFUSED, EXIT_TIMEOUT, HarnessError

VERSION_FILE = ("data", "text", "fsoharness-version.txt")


@pytest.fixture
def src(tmp_path, monkeypatch):
    """A private copy of the shipped bridge that the test may edit, used as the install source."""
    copy = tmp_path / "src" / paths.BRIDGE_MOD
    shutil.copytree(paths.bridge_source(), copy)
    monkeypatch.setattr(bridge_install.paths, "bridge_source", lambda: copy)
    return copy


@pytest.fixture
def fs2(tmp_path):
    """An empty game folder."""
    d = tmp_path / "FS2"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def units(monkeypatch):
    """Fake systemd's list of running harness scopes (empty unless a test fills it)."""
    running: list[str] = []
    monkeypatch.setattr(scope, "active_units", lambda: list(running))
    return running


def _lua(root):
    return root / "data" / "scripts" / "fsoharness.lua"


def test_editing_a_source_file_changes_the_version_and_refreshes(tmp_runtime, src, fs2):
    first = bridge_install.install(fs2)
    _lua(src).write_text(_lua(src).read_text() + "\n-- edited\n")
    second = bridge_install.install(fs2)
    assert second["version"] != first["version"] and second["updated"] is True
    assert _lua(fs2 / paths.BRIDGE_MOD).read_text().endswith("-- edited\n")


def test_renaming_a_source_file_changes_the_version(tmp_runtime, src, fs2):
    first = bridge_install.install(fs2)
    tables = sorted((src / "data" / "tables").iterdir())
    tables[0].rename(tables[0].with_name("renamed-" + tables[0].name))
    second = bridge_install.install(fs2)
    assert second["version"] != first["version"] and second["updated"] is True


def test_an_edited_installed_copy_is_refreshed(tmp_runtime, src, fs2):
    # The old staleness check compared only the stamp file, so an installed copy edited in place
    # (stamp untouched) kept running.
    bridge_install.install(fs2)
    target = fs2 / paths.BRIDGE_MOD
    _lua(target).write_text("-- tampered")
    assert bridge_install.install(fs2)["updated"] is True
    assert _lua(target).read_text() == _lua(src).read_text()


def test_install_writes_the_version_file(tmp_runtime, src, fs2):
    r = bridge_install.install(fs2)
    target = fs2 / paths.BRIDGE_MOD
    assert target.joinpath(*VERSION_FILE).read_text() == r["version"]
    target.joinpath(*VERSION_FILE).unlink()     # an install from before C5 has none
    assert bridge_install.install(fs2)["updated"] is True
    assert target.joinpath(*VERSION_FILE).read_text() == r["version"]


def test_a_folder_without_the_harness_stamp_is_never_deleted(tmp_runtime, src, fs2):
    mine = fs2 / paths.BRIDGE_MOD
    mine.mkdir()
    (mine / "users-own-file.txt").write_text("keep")
    with pytest.raises(HarnessError) as e:
        bridge_install.install(fs2)
    assert e.value.code == EXIT_REFUSED and e.value.message and e.value.hint
    assert (mine / "users-own-file.txt").read_text() == "keep"


def test_a_refresh_is_refused_while_other_sessions_use_the_bridge(tmp_runtime, src, fs2,
                                                                  units):
    bridge_install.install(fs2)
    _lua(src).write_text("-- newer")
    units.extend(["fso-harness-s0000aaaa.scope", "fso-harness-s0000bbbb.scope"])
    with pytest.raises(HarnessError) as e:
        bridge_install.install(fs2)
    assert e.value.code == EXIT_REFUSED and e.value.message
    assert "s0000aaaa" in e.value.hint and "s0000bbbb" in e.value.hint
    assert "another game folder" in e.value.hint      # R1: the conservative reason is stated
    assert _lua(fs2 / paths.BRIDGE_MOD).read_text() != "-- newer"


def test_busy_sessions_are_seen_across_runtime_roots(tmp_path, src, fs2, units, monkeypatch):
    # QA SL2-2 / TA2-4: a session launched with another FSO_HARNESS_RUNTIME (another worktree,
    # every engine test) uses the same FS2PATH bridge; it must still block a refresh.
    monkeypatch.setenv("FSO_HARNESS_RUNTIME", str(tmp_path / "root-a"))
    monkeypatch.setenv("FSO_HARNESS_STATE", str(tmp_path / "state"))
    bridge_install.install(fs2)
    units.append("fso-harness-s1111aaaa.scope")            # running under root-a
    monkeypatch.setenv("FSO_HARNESS_RUNTIME", str(tmp_path / "root-b"))
    _lua(src).write_text("-- from another checkout")
    with pytest.raises(HarnessError) as e:
        bridge_install.install(fs2)
    assert e.value.code == EXIT_REFUSED and "s1111aaaa" in e.value.hint


def test_a_current_bridge_is_fine_while_other_sessions_run(tmp_runtime, src, fs2, units):
    bridge_install.install(fs2)
    units.append("fso-harness-s0000aaaa.scope")
    assert bridge_install.install(fs2)["updated"] is False


def test_the_lock_is_keyed_by_game_folder_in_the_state_dir(tmp_runtime, src, fs2, monkeypatch):
    key = hashlib.sha256(os.path.realpath(fs2).encode()).hexdigest()[:16]
    lock = paths.state_dir() / "locks" / f"bridge-{key}.lock"
    monkeypatch.setattr(bridge_install, "_LOCK_WAIT", 0.3)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a") as held:              # another fsoctl, any runtime root, holds it
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(HarnessError) as e:
            bridge_install.install(fs2)
    assert e.value.code == EXIT_TIMEOUT and e.value.hint
    assert bridge_install.install(fs2)["updated"] is True
    assert sorted(p.name for p in fs2.iterdir()) == [paths.BRIDGE_MOD]   # nothing else in FS2


def test_install_leaves_no_temporary_folders(tmp_runtime, src, fs2):
    bridge_install.install(fs2)
    _lua(src).write_text("-- v2")
    bridge_install.install(fs2)
    assert sorted(p.name for p in fs2.iterdir()) == [paths.BRIDGE_MOD]


def test_concurrent_first_installs_both_succeed(tmp_runtime, src, fs2):
    # Before the lock, two first installs raced copytree into the same folder (FileExistsError).
    results, errors = [], []

    def go():
        try:
            results.append(bridge_install.install(fs2)["updated"])
        except Exception as e:   # any failure at all is reported by the assert below
            errors.append(repr(e))

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == [] and sorted(results) == [False, False, False, True]


def test_a_failed_swap_puts_the_old_bridge_back(tmp_runtime, src, fs2, monkeypatch):
    # YK-6: if renaming the new copy into place failed after the old one was moved aside, the
    # cleanup deleted the only copy and FS2PATH had no bridge at all.
    bridge_install.install(fs2)
    target = fs2 / paths.BRIDGE_MOD
    old_lua = _lua(target).read_text()
    _lua(src).write_text("-- newer")
    real_rename = os.rename

    def fail_into_place(a, b):
        if Path(a).name == "new" and Path(b) == target:      # only the new copy's swap fails
            raise OSError(16, "Device or resource busy")
        real_rename(a, b)
    monkeypatch.setattr(bridge_install.os, "rename", fail_into_place)
    with pytest.raises(OSError):
        bridge_install.install(fs2)
    assert _lua(target).read_text() == old_lua
    assert sorted(p.name for p in fs2.iterdir()) == [paths.BRIDGE_MOD]
