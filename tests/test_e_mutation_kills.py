"""Tests written to kill mutants that survived the stabilization mutation run (QA MU2-1, 2, 4).

Each test names the surviving mutant it kills; see the QA record
(MU2-mutation.md, kept outside this repository).
"""
from pathlib import Path

from fsoctl import bridge_install, envsetup, paths, vp
from tests.test_vp import write_vp


def test_one_byte_entry_is_extracted_whole(tmp_path):
    # MU2-1: `while left > 0` -> `while left > 1` skipped the copy loop for a 1-byte entry,
    # extracting it empty; no archive in the suite had an entry that small.
    arc = tmp_path / "a.vp"
    write_vp(arc, {"data/one.txt": b"x", "data/two.txt": b"yz"})
    out = tmp_path / "out"
    vp.extract(arc, "*", out)
    assert (out / "data" / "one.txt").read_bytes() == b"x"
    assert (out / "data" / "two.txt").read_bytes() == b"yz"


def test_bridge_refresh_stages_in_a_hidden_folder_inside_the_game_folder(tmp_path, monkeypatch):
    # MU2-2: the temp folder must be in FS2PATH (dir=fs2) so the final rename stays on one
    # filesystem, and hidden (".fso_harness_bridge." prefix) so the game never sees it as a mod.
    seen = {}
    real = bridge_install.tempfile.mkdtemp

    def spy(*a, **k):
        seen.update(k)
        return real(*a, **k)
    monkeypatch.setattr(bridge_install.tempfile, "mkdtemp", spy)
    fs2 = tmp_path / "fs2"
    fs2.mkdir()
    bridge_install._replace(fs2, fs2 / paths.BRIDGE_MOD, "v1")
    assert Path(seen["dir"]) == fs2
    assert seen["prefix"] == f".{paths.BRIDGE_MOD}."
    assert (fs2 / paths.BRIDGE_MOD / bridge_install.STAMP).read_text() == "v1"
    assert [p.name for p in fs2.iterdir()] == [paths.BRIDGE_MOD]   # temp folder removed


def test_vulkan_child_does_not_inherit_forced_software_gl(tmp_path):
    # MU2-4: a caller environment with LIBGL_ALWAYS_SOFTWARE=1 must not force software GL on a
    # Vulkan session (env.pop(..., None) -> env.pop(None, None) survived).
    base = {"LIBGL_ALWAYS_SOFTWARE": "1", "PATH": "/usr/bin"}
    env = envsetup.child_env(base, ":7", tmp_path / "prefs", "vulkan", tmp_path / "run")
    assert "LIBGL_ALWAYS_SOFTWARE" not in env
    env = envsetup.child_env(base, ":7", tmp_path / "prefs", "opengl", tmp_path / "run")
    assert env["LIBGL_ALWAYS_SOFTWARE"] == "1"
