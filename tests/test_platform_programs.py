"""Finding a program by name without ever looking in the current folder (QA Plan B, SA-1).

On Windows a bare program name is looked for in the current folder before PATH, by Windows
itself when a process is started and by Python's shutil.which. The folder a command is typed
in may be a checkout or a mod under test, so the harness looks on PATH itself and starts what
it found by its full path. The lookup is plain file work and runs on every system here."""
import os
import sys

import pytest

from fsoctl.platform import programs


@pytest.fixture
def folders(tmp_path, monkeypatch):
    """PATH holds tmp/tools; the current folder is tmp/here. Returns (tools, here)."""
    tools, here = tmp_path / "tools", tmp_path / "here"
    tools.mkdir()
    here.mkdir()
    monkeypatch.chdir(here)
    monkeypatch.setenv("PATH", str(tools))
    return tools, here


def test_a_program_in_the_current_folder_is_never_the_one_found(folders):
    tools, here = folders
    (here / "cmake.exe").write_bytes(b"planted")
    assert programs.find("cmake", windows=True) is None
    (tools / "cmake.exe").write_bytes(b"real")
    assert programs.find("cmake", windows=True) == str(tools / "cmake.exe")


@pytest.mark.parametrize("entry", ["", ".", "bin", "..", "./bin"])
def test_path_entries_that_depend_on_the_current_folder_are_skipped(folders, monkeypatch,
                                                                    entry):
    _, here = folders
    (here / "bin").mkdir()
    for planted in (here / "cmake.exe", here / "bin" / "cmake.exe", here.parent / "cmake.exe"):
        planted.write_bytes(b"planted")
    monkeypatch.setenv("PATH", entry)
    assert programs.find("cmake", windows=True) is None


def test_the_first_folder_on_path_wins_and_quotes_around_an_entry_are_dropped(folders,
                                                                             monkeypatch):
    tools, here = folders
    second = here.parent / "second folder"
    second.mkdir()
    (second / "cdb.exe").write_bytes(b"")
    monkeypatch.setenv("PATH", os.pathsep.join([str(tools), f'"{second}"']))
    assert programs.find("cdb", windows=True) == str(second / "cdb.exe")
    (tools / "cdb.exe").write_bytes(b"")
    assert programs.find("cdb", windows=True) == str(tools / "cdb.exe")


def test_only_program_files_count(folders):
    tools, _ = folders
    (tools / "magick.bat").write_text("echo planted")
    (tools / "magick.cmd").write_text("echo planted")
    (tools / "magick").write_text("not a Windows program")
    assert programs.find("magick", windows=True) is None
    (tools / "magick.com").write_bytes(b"")
    assert programs.find("magick", windows=True) == str(tools / "magick.com")
    (tools / "magick.exe").mkdir()                              # a folder is not a program
    assert programs.find("magick", windows=True) == str(tools / "magick.com")


def test_on_other_systems_the_lookup_is_the_usual_one(monkeypatch):
    monkeypatch.setattr(programs.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert programs.find("cmake", windows=False) == "/usr/bin/cmake"


def test_an_entry_that_is_skipped_does_not_end_the_search(folders, monkeypatch):
    tools, here = folders
    (here / "bin").mkdir()
    (tools / "cmake.exe").write_bytes(b"real")
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "bin", str(tools)]))
    assert programs.find("cmake", windows=True) == str(tools / "cmake.exe")


def test_no_path_at_all_finds_nothing(monkeypatch):
    monkeypatch.delenv("PATH", raising=False)
    assert programs.find("cmake", windows=True) is None


def test_without_being_told_the_rules_are_this_systems(folders):
    tools, _ = folders
    if sys.platform == "win32":
        tool = tools / "fsotool.exe"
        tool.write_bytes(b"")
    else:
        tool = tools / "fsotool"                  # here a program has no suffix and an x bit
        tool.write_text("#!/bin/sh\n", encoding="utf-8")
        tool.chmod(0o755)
    assert programs.find("fsotool") == str(tool)


@pytest.mark.parametrize("entry, rules, one_place", [
    ("C:\\tools", "nt", True), ("\\\\server\\share\\bin", "nt", True),
    ("\\tools", "nt", False),        # on the CURRENT drive: depends on where it was typed
    ("C:tools", "nt", False), ("tools", "nt", False), ("", "nt", False),
    ("/usr/bin", "posix", True), ("bin", "posix", False), ("", "posix", False)])
def test_only_an_entry_that_names_one_place_is_searched(entry, rules, one_place):
    # round-2 SAF-6: Python 3.12 (the floor, and CI's version) calls a Windows path with one
    # leading backslash absolute; 3.13 does not. The rule here must not depend on that.
    import ntpath  # noqa: PLC0415
    import posixpath  # noqa: PLC0415
    module = ntpath if rules == "nt" else posixpath
    assert programs._names_one_place(entry, module) is one_place


@pytest.mark.skipif(sys.platform != "win32", reason="a path with no drive is only odd on Windows")
def test_find_skips_an_entry_with_no_drive_on_windows(folders, monkeypatch):
    # round-2 TAF-12: the table above tests the helper; this holds find() to using it.
    # Python 3.12 calls such an entry absolute, and it means "on the current drive".
    tools, _ = folders
    (tools / "cmake.exe").write_bytes(b"real")
    monkeypatch.setenv("PATH", os.path.splitdrive(str(tools))[1])
    assert programs.find("cmake", windows=True) is None
