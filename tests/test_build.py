import subprocess
from pathlib import Path

import pytest

from fsoctl import build, cli, paths, scratch
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


# ---- Windows (cross-platform Plan B, Task 9) ----
# The four data files are real output of `cmake --build ... --config Release` with the Visual
# Studio 17 2022 generator (MSBuild 17.14, MSVC 19.44), captured in the VM on 2026-11-16 from
# a three-file sample project: a compile error with a warning beside it, an unresolved
# symbol, a missing library, and a clean build.
def _msvc(name: str) -> str:
    return (Path(__file__).parent / "data" / f"build_msvc_{name}.txt").read_text()


def test_an_msvc_error_line_is_parsed_into_file_line_column_and_message():
    d = build._diag(r"src\a.cpp(12,5): error C2065: 'x': undeclared identifier")
    assert d == {"file": r"src\a.cpp", "line": 12, "col": 5, "kind": "error",
                 "message": "C2065: 'x': undeclared identifier", "source": "compiler"}
    no_column = build._diag(r"C:\src\a.cpp(40): fatal error C1083: Cannot open include file: "
                            "'x.h': No such file or directory")
    assert (no_column["line"], no_column["col"], no_column["kind"]) == (40, None, "error")
    assert no_column["message"].startswith("C1083: Cannot open include file")


def test_real_msvc_output_gives_the_errors_then_the_warning_without_the_project_suffix():
    found = build.compiler_errors(_msvc("compile"))
    assert [(e["kind"], e["line"], e["col"]) for e in found] == [
        ("error", 5, 14), ("error", 7, 1), ("warning", 1, 26)]
    assert found[0]["file"] == r"C:\xplat\msvc\bad_compile.cpp"
    assert found[0]["message"] == "C2065: 'undeclared_name': undeclared identifier"
    assert found[2]["message"] == "C4100: 'never_read': unreferenced parameter"
    assert not any("vcxproj" in e["message"] for e in found)


def test_a_message_with_its_own_brackets_keeps_them():
    d = build._diag(r"a.cpp(3,9): error C2440: 'initializing': cannot convert from 'int [3]' "
                    r"to 'int' [C:\b\code.vcxproj]")
    assert d["message"] == "C2440: 'initializing': cannot convert from 'int [3]' to 'int'"


def test_linker_lines_are_tool_errors():
    line = "LINK : fatal error LNK1104: cannot open file 'foo.lib'"
    errors, warnings = build.scan(line)
    assert warnings == [] and errors == [
        {"file": None, "line": None, "col": None, "kind": "error", "message": line,
         "source": "linker"}]
    errors, _ = build.scan(_msvc("link"))
    assert [e["source"] for e in errors] == ["linker", "linker"]
    assert errors[0]["message"].startswith("bad_link.obj : error LNK2019: unresolved external")
    assert errors[1]["message"].endswith("fatal error LNK1120: 1 unresolved externals")
    errors, _ = build.scan(_msvc("missing_lib"))
    assert [e["message"] for e in errors] == [
        "LINK : fatal error LNK1181: cannot open input file 'no_such_library_at_all.lib'"]


def test_other_microsoft_tool_errors_are_reported_by_their_tool():
    errors, _ = build.scan(_msvc("no_target"))          # real: a target that does not exist
    assert [(e["source"], e["message"]) for e in errors] == [
        ("msbuild", "MSBUILD : error MSB1009: Project file does not exist.")]
    # The compiler driver's line in the shape its real warning has (two spaces, lower case),
    # as an error; not seen as an error in real output.
    errors, _ = build.scan("cl : command line  error D8021: invalid numeric argument '/Ob9' "
                           "[C:\\x\\a.vcxproj]\n")
    assert [(e["source"], e["file"]) for e in errors] == [("compiler", None)]
    assert errors[0]["message"].endswith("argument '/Ob9'")


def test_the_compiler_drivers_real_warning_is_not_an_error():
    assert "warning D9002" in _msvc("option_warning")
    assert build.scan(_msvc("option_warning")) == ([], [])


def test_a_clean_msvc_build_and_gcc_output_have_no_microsoft_errors():
    assert build.scan(_msvc("ok")) == ([], [])
    gcc_errors, _ = build.scan(GCC)
    assert gcc_errors and all(e["source"] != "msbuild" for e in gcc_errors)
    # "error:" with no code is GCC's linker driver, already reported as source "linker"
    errors, _ = build.scan("collect2: error: ld returned 1 exit status\n")
    assert [e["source"] for e in errors] == ["linker"]


@pytest.fixture
def configured(tmp_path, tmp_runtime, monkeypatch):
    """A configured-looking worktree with the scope and the build command faked; returns
    (worktree, list of commands run)."""
    wt = tmp_path / "wt"
    (wt / "build").mkdir(parents=True)
    (wt / "build" / "CMakeCache.txt").write_text("")
    monkeypatch.setattr(build.scope, "caps", lambda **kw: ["MemoryMax=1K"])
    monkeypatch.setattr(build.scope, "is_active", lambda unit: False)
    monkeypatch.setattr(build.scope, "stop", lambda unit, *a, **k: None)
    monkeypatch.setattr(build.programs, "find", lambda name: f"/usr/bin/{name}")
    # The command line under test is the Linux one unless a test says "windows" itself.
    monkeypatch.setattr(build.platform, "NAME", "linux")
    calls = []

    def run(cmd, stdout=None, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(build.subprocess, "run", run)
    return wt, calls


def test_config_adds_the_configuration_to_the_build_command(configured):
    wt, calls = configured
    assert build.build(str(wt), config="Release", targets=["Freespace2"])["ok"] is True
    tail = calls[0][calls[0].index("cmake"):]
    assert tail == ["cmake", "--build", str(wt / "build"), "-j", "14", "--config", "Release",
                    "--target", "Freespace2"]
    build.build(str(wt))
    assert "--config" not in calls[1]                   # a single-config tree is not told one


def test_on_windows_the_build_runs_in_a_job_and_never_through_systemd(configured, monkeypatch):
    wt, calls = configured
    monkeypatch.setattr(build.platform, "NAME", "windows")
    seen = {}

    def run(unit, argv, log, props, timeout):
        seen.update(unit=unit, argv=argv, props=props, timeout=timeout)
        log.write(_msvc("compile").encode())
        return 1
    monkeypatch.setattr(build.scope, "run", run, raising=False)
    r = build.build(str(wt), config="FastDebug", timeout=90)
    assert calls == []                                  # no systemd-run command at all
    # by the full path it was found at: Windows looks for a bare name in the current folder
    # first, and that folder may be the tree being built (QA Plan B, SA-1)
    assert seen["argv"] == ["/usr/bin/cmake", "--build", str(wt / "build"), "-j", "14",
                            "--config", "FastDebug"]
    assert seen["unit"].startswith("fso-harness-build-") and seen["timeout"] == 90
    assert seen["props"] == ["MemoryMax=1K"]
    assert r["ok"] is False and r["error_count"] == 2 and r["warning_count"] == 1
    assert r["errors"][0]["line"] == 5 and r["oom_suspected"] is False and "note" not in r


def test_on_windows_a_timeout_and_a_cancel_end_the_job_as_on_linux(configured, monkeypatch):
    wt, _ = configured
    monkeypatch.setattr(build.platform, "NAME", "windows")
    stopped = []
    monkeypatch.setattr(build.scope, "stop", lambda unit, *a, **k: stopped.append(unit))

    def too_slow(unit, argv, log, props, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)
    monkeypatch.setattr(build.scope, "run", too_slow, raising=False)
    with pytest.raises(HarnessError) as e:
        build.build(str(wt), timeout=5)
    assert e.value.code == 5 and len(stopped) == 1

    def cancelled(unit, argv, log, props, timeout):
        raise KeyboardInterrupt
    monkeypatch.setattr(build.scope, "run", cancelled, raising=False)
    with pytest.raises(KeyboardInterrupt):
        build.build(str(wt))
    assert len(stopped) == 2


def test_an_exit_code_that_means_a_kill_on_linux_is_no_memory_hint_on_windows(configured,
                                                                             monkeypatch):
    wt, _ = configured
    monkeypatch.setattr(build.platform, "NAME", "windows")
    monkeypatch.setattr(build.scope, "run", lambda *a: 137, raising=False)
    r = build.build(str(wt))
    assert r["ok"] is False and r["oom_suspected"] is False and "note" not in r


def test_the_refusal_while_a_build_runs_names_no_linux_command_on_windows(configured,
                                                                         monkeypatch):
    wt, _ = configured
    monkeypatch.setattr(build.scope, "is_active", lambda unit: True)
    with pytest.raises(HarnessError) as e:
        build.build(str(wt))
    assert e.value.code == EXIT_REFUSED and "systemctl --user stop" in e.value.hint
    monkeypatch.setattr(build.platform, "NAME", "windows")
    with pytest.raises(HarnessError) as e:
        build.build(str(wt))
    assert e.value.code == EXIT_REFUSED and "systemctl" not in e.value.hint


def test_cmake_comes_from_path_or_from_the_build_folders_own_record(configured, monkeypatch,
                                                                   tmp_path):
    # Seen in the VM: Visual Studio's Build Tools bring their own cmake and do not put it on
    # PATH; the build folder's CMakeCache.txt names the cmake that configured it.
    wt, calls = configured
    mine = tmp_path / "vs" / "cmake.exe"
    mine.parent.mkdir()
    mine.write_bytes(b"")
    (wt / "build" / "CMakeCache.txt").write_text(
        "CMAKE_GENERATOR:INTERNAL=Visual Studio 17 2022\n"
        f"CMAKE_COMMAND:INTERNAL={mine.as_posix()}\n")
    build.build(str(wt))
    assert "cmake" in calls[0]                          # on PATH: the plain name, as before
    monkeypatch.setattr(build.programs, "find", lambda name: None)
    build.build(str(wt))
    assert str(mine) in calls[1] and "cmake" not in calls[1]
    mine.unlink()
    with pytest.raises(HarnessError) as e:
        build.build(str(wt))
    assert e.value.code == EXIT_USAGE and "cmake" in e.value.message
    assert len(calls) == 2


# ---- gaps a mutation run found (QA Plan B, MU-1, MU-2): each test names what it pins ----
def test_on_linux_the_build_is_one_systemd_run_scope_with_each_limit(configured, monkeypatch):
    wt, _ = configured
    seen = {}

    def caps(**kw):
        seen["caps"] = kw
        return ["MemoryMax=1K", "TasksMax=9"]

    def run(cmd, **kw):
        seen.update(cmd=cmd, kw=kw)
        return subprocess.CompletedProcess(cmd, 3)
    monkeypatch.setattr(build.scope, "caps", caps)
    monkeypatch.setattr(build.subprocess, "run", run)
    # only the name "cmake" is on PATH: asking for any other name finds nothing
    monkeypatch.setattr(build.programs, "find",
                        lambda name: "/usr/bin/cmake" if name == "cmake" else None)
    r = build.build(str(wt), jobs=6, timeout=120)
    unit = seen["cmd"][seen["cmd"].index("--unit") + 1]
    assert unit.startswith("fso-harness-build-")
    assert seen["cmd"] == ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                           "--unit", unit, "-p", "MemoryMax=1K", "-p", "TasksMax=9",
                           "cmake", "--build", str(wt / "build"), "-j", "6"]
    # the scope outlives the command's own timeout by a minute, and gets one core per job
    assert seen["caps"] == {"runtime_sec": 180, "cpu_cores": 6}
    kw = seen["kw"]
    assert kw["cwd"] == wt.resolve() and kw["stdin"] == subprocess.DEVNULL
    assert kw["stderr"] == subprocess.STDOUT and kw["timeout"] == 120 and kw["check"] is False
    assert str(kw["stdout"].name) == str(paths.runtime_root() / "builds" / f"{unit}.log")
    assert r["ok"] is False and r["returncode"] == 3     # a failed build is a result, not a raise


def _recorded_cmake(wt, tmp_path, folder, before=b""):
    """Make a cmake file in tmp_path/folder and a build folder record that names it."""
    mine = tmp_path / folder / "cmake.exe"
    mine.parent.mkdir(parents=True)
    mine.write_bytes(b"")
    (wt / "build" / "CMakeCache.txt").write_bytes(
        before + f"CMAKE_COMMAND:INTERNAL={mine.as_posix()}\n".encode())
    return mine


def test_a_recorded_cmake_path_may_hold_an_equals_sign_and_follow_bytes_that_are_not_text(
        configured, monkeypatch, tmp_path):
    wt, calls = configured
    monkeypatch.setattr(build.programs, "find", lambda name: None)
    mine = _recorded_cmake(wt, tmp_path, "a=b", before=b"\xff\xfe not text\n")
    build.build(str(wt))
    assert calls[0][calls[0].index("--build") - 1] == str(mine)


def test_only_the_start_of_the_build_folders_record_is_read(configured, monkeypatch, tmp_path):
    wt, calls = configured
    monkeypatch.setattr(build.programs, "find", lambda name: None)
    monkeypatch.setattr(build, "_CACHE_LINES_MAX", 2)
    _recorded_cmake(wt, tmp_path, "vs", before=b"one\ntwo\n")      # the record is line 3
    with pytest.raises(HarnessError) as e:
        build.build(str(wt))
    assert e.value.code == EXIT_USAGE and calls == []


def test_the_build_verb_takes_config_on_the_command_line(configured, capsys):
    wt, calls = configured
    assert cli.main(["--json", "build", str(wt), "--config", "Release"]) == 0
    assert calls[0][-2:] == ["--config", "Release"]


def test_a_gcc_diagnostic_carries_its_column():
    d = build._diag("/w/code/ai/aicode.cpp:120:7: error: 'x' was not declared in this scope")
    assert (d["file"], d["line"], d["col"]) == ("/w/code/ai/aicode.cpp", 120, 7)
