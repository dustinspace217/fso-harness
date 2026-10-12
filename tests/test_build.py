import subprocess
from pathlib import Path

import pytest

from fsoctl import build, scratch
from fsoctl.errors import EXIT_REFUSED, EXIT_USAGE, HarnessError

GCC = (Path(__file__).parent / "data" / "gcc_errors.txt").read_text()


def test_compiler_errors_parsed():
    # Errors first, then warnings (QA SF-11); the ninja line is a build-tool error, not shown here.
    errs = build.compiler_errors(GCC)
    assert [(e["file"], e["line"], e["kind"]) for e in errs if e["source"] == "compiler"] == [
        ("/w/code/ai/aicode.cpp", 120, "error"),
        ("/w/code/ship/ship.h", 44, "error"),
        ("/w/code/io/key.cpp", 7, "error"),
        ("/w/code/ai/aicode.cpp", 121, "warning"),
    ]


def test_compiler_errors_limit():
    assert len(build.compiler_errors(GCC, limit=2, warning_limit=0)) == 2


def test_build_refuses_unconfigured(tmp_path, tmp_runtime):
    with pytest.raises(HarnessError) as e:
        build.build(str(tmp_path))
    assert e.value.code == EXIT_USAGE and "configure" in e.value.hint


def test_scratch_create_list_remove(tmp_path, monkeypatch):
    monkeypatch.setenv("FS2PATH", str(tmp_path))
    r = scratch.create("glow test")
    p = Path(r["path"])
    assert p.name.startswith(scratch.PREFIX) and (p / "data" / "missions").is_dir()
    assert [m["name"] for m in scratch.list_all()["mods"]] == [r["name"]]
    assert scratch.list_all()["mods"][0]["label"] == "glow test"
    scratch.remove(r["name"])
    assert not p.exists()


def test_scratch_remove_refuses_foreign_folders(tmp_path, monkeypatch):
    monkeypatch.setenv("FS2PATH", str(tmp_path))
    (tmp_path / "MVPS-4.7.3").mkdir()
    (tmp_path / f"{scratch.PREFIX}abcdef").mkdir()  # right name shape, but no stamp
    for name in ("MVPS-4.7.3", f"{scratch.PREFIX}abcdef", "../etc", f"{scratch.PREFIX}../x"):
        with pytest.raises(HarnessError) as e:
            scratch.remove(name)
        assert e.value.code == EXIT_REFUSED
    assert (tmp_path / "MVPS-4.7.3").exists()
    assert (tmp_path / f"{scratch.PREFIX}abcdef").exists()


@pytest.mark.engine
def test_build_reports_real_compiler_error(tmp_path, tmp_runtime):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20)\nproject(t CXX)\nadd_executable(t main.cpp)\n")
    (src / "main.cpp").write_text("int main() { return undeclared; }\n")
    subprocess.run(["cmake", "-S", str(src), "-B", str(src / "build"), "-G", "Ninja"],
                   check=True, capture_output=True, timeout=120)
    r = build.build(str(src), timeout=300)
    assert r["ok"] is False and r["returncode"] != 0
    assert r["errors"][0]["file"].endswith("main.cpp") and r["errors"][0]["line"] == 1
    (src / "main.cpp").write_text("int main() { return 0; }\n")
    assert build.build(str(src), timeout=300)["ok"] is True
