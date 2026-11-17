"""probes/xplat/win_hidden_desktop.py's platform-free parts (plan A Task 5). The Win32 parts run
only on the Windows CI runner; these pin the judgement and bookkeeping the results rest on, and
that the file imports on Linux (the Windows-only imports are deferred).

Loaded under its package-style name so mutmut can match the mutants to these tests."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

_SPEC = importlib.util.spec_from_file_location(
    "probes.xplat.win_hidden_desktop",
    Path(__file__).resolve().parents[1] / "probes" / "xplat" / "win_hidden_desktop.py")
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)


def test_colour_close_accepts_the_clear_colour_within_two_per_channel():
    assert probe.colour_close((64, 128, 191))
    assert probe.colour_close((66, 126, 193))          # rounding either way, every channel
    assert not probe.colour_close((67, 128, 191))      # one channel three off
    assert not probe.colour_close((0, 0, 0))
    assert not probe.colour_close((64, 128))           # a short read is not a match


def test_colorref_is_unpacked_red_first():
    assert probe.colorref_rgb(0x00BF8040) == (64, 128, 191)


def test_gl_meets_minimum_reads_the_version_the_context_reported():
    assert probe.gl_meets_minimum("4.6 (Core Profile) Mesa 26.2.4 (git-9a0169f2a7)")
    assert probe.gl_meets_minimum("3.2.0 NVIDIA 615.78")
    assert not probe.gl_meets_minimum("3.1 Mesa")
    assert not probe.gl_meets_minimum("1.1.0")         # the runner's GDI Generic, run 1
    assert not probe.gl_meets_minimum(None)
    assert not probe.gl_meets_minimum("error: SDL_CreateWindow: ...")


def test_env_block_sorts_names_ignoring_case_and_ends_with_two_nuls():
    block = probe.env_block({"b": "2", "PATH": "x", "Alpha": "1"})
    assert block == "Alpha=1\0b=2\0PATH=x\0\0"


def test_wait_for_returns_once_the_predicate_turns_true(monkeypatch):
    monkeypatch.setattr(probe, "POLL_S", 0)
    answers = iter([False, False, True])
    assert probe.wait_for(lambda: next(answers), 5)


def test_wait_for_gives_up_at_the_deadline(monkeypatch):
    monkeypatch.setattr(probe, "POLL_S", 0.01)
    calls = []
    assert not probe.wait_for(lambda: calls.append(1) or False, 0.05)
    assert calls                                       # it polled


def test_wait_for_asks_once_more_at_the_deadline(monkeypatch):
    # One poll inside the loop (False), whose sleep outlasts the deadline, then the final
    # re-check (True): the final answer must count.
    monkeypatch.setattr(probe, "POLL_S", 0.05)
    answers = iter([False, True])
    assert probe.wait_for(lambda: next(answers), 0.02)


def test_find_log_prefers_the_prefs_folder_then_the_game_folder(tmp_path):
    prefs, game = tmp_path / "prefs", tmp_path / "game"
    assert probe.find_log(prefs, game) is None
    legacy = game / "data" / "fs2_open.log"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("x")
    assert probe.find_log(prefs, game) == legacy
    pref = prefs / "data" / "fs2_open.log"
    pref.parent.mkdir(parents=True)
    pref.write_text("x")
    assert probe.find_log(prefs, game) == pref


def test_log_lines_finds_the_gl_init_marker_the_engine_writes(tmp_path):
    # The marker and renderer lines as the engine wrote them in a no-data run (2026-10-25).
    log = tmp_path / "fs2_open.log"
    log.write_text("  Initializing SDL video...\n"
                   "  OpenGL Renderer  : llvmpipe (LLVM 19.1.1, 256 bits)\n"
                   "... OpenGL init is complete!\n"
                   "Error: Web cursor bitmap not found.\n")
    assert probe.log_lines(log, probe.GL_INIT_LINE) == ["... OpenGL init is complete!"]
    assert probe.log_lines(log, "OpenGL Renderer") == [
        "OpenGL Renderer  : llvmpipe (LLVM 19.1.1, 256 bits)"]
    assert probe.log_lines(None, probe.GL_INIT_LINE) == []


def test_the_engine_gets_a_prefs_folder_of_the_cell_never_the_real_profile(monkeypatch, tmp_path):
    # QA CR-6: on the owner's Windows the real profile log must be neither deleted nor written.
    seen = {}

    def fake_spawn(_w, argv, _desktop, env, cwd):
        seen.update(argv=argv, env=env, cwd=cwd)
        log = Path(env["FSO_PREFERENCES_PATH"]) / "data" / "fs2_open.log"
        log.parent.mkdir(parents=True)
        log.write_text("... OpenGL init is complete!\n")
        return "handle", 1

    monkeypatch.setattr(probe, "spawn", fake_spawn)
    monkeypatch.setattr(probe, "end_process", lambda *_a, **_k: None)
    profile = tmp_path / "AppData" / "HardLightProductions" / "FreeSpaceOpen"
    real_log = profile / "data" / "fs2_open.log"
    real_log.parent.mkdir(parents=True)
    real_log.write_text("the owner's own log")
    cell = SimpleNamespace(w=None, desktop="d", fso_exe=Path("fs2.exe"), notes=[],
                           work=tmp_path / "work", env={"APPDATA": str(tmp_path / "AppData")})
    cell.work.mkdir()
    assert probe.fso_check(cell) is True
    assert seen["env"]["FSO_PREFERENCES_PATH"] == str(cell.work / "prefs")
    assert "-nosound" in seen["argv"]
    assert real_log.read_text() == "the owner's own log"


def test_mesa_cells_run_a_copy_and_leave_the_given_engine_folder_alone(tmp_path):
    fso, mesa, out = tmp_path / "fso", tmp_path / "mesa", tmp_path / "out"
    for folder, names in ((fso, [probe.FSO_EXE, "SDL2.dll"]), (mesa, list(probe.MESA_DLLS))):
        folder.mkdir()
        for name in names:
            (folder / name).write_text(name)
    target = probe.mesa_engine_dir(fso, mesa, out)
    assert sorted(p.name for p in target.iterdir()) == sorted(
        [probe.FSO_EXE, "SDL2.dll", *probe.MESA_DLLS])
    assert sorted(p.name for p in fso.iterdir()) == sorted([probe.FSO_EXE, "SDL2.dll"])


def test_runner_cells_run_before_mesa_and_use_the_given_folder(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(probe, "one_cell", lambda c: calls.append((c.gl, c.desk,
                                                                   c.fso_exe.parent.name)) or {})
    monkeypatch.setattr(probe, "mesa_env", lambda _m: calls.append(("mesa_env",)) or {})
    monkeypatch.setattr(probe, "mesa_engine_dir", lambda *_a: tmp_path / "fso-mesa")
    args = SimpleNamespace(mesa_dir=tmp_path / "mesa", fso_dir=tmp_path / "fso", out=tmp_path)
    probe.run_cells(None, args, "WinSta0", None)
    assert calls == [("runner", "normal", "fso"), ("runner", "hidden", "fso"), ("mesa_env",),
                     ("mesa", "normal", "fso-mesa"), ("mesa", "hidden", "fso-mesa")]


def test_a_hidden_row_without_a_desktop_says_so_and_runs_nothing():
    row = probe.one_cell(SimpleNamespace(gl="runner", desk="hidden", hdesk=None))
    assert row["desktop_created"] is False
    assert row["notes"] == ["the hidden desktop was not created; nothing ran"]


def test_env_block_sorts_like_windows_upper_case_by_name_not_value():
    # '_' sorts between the upper- and lower-case letters, so upper- and lower-casing disagree.
    block = probe.env_block({"A_B": "1", "AB": "2", "a": "z", "B": "a"})
    assert block == "a=z\0AB=2\0A_B=1\0B=a\0\0"


def test_gl_meets_minimum_refuses_versions_it_cannot_read():
    assert [probe.gl_meets_minimum(v) for v in ("4", "3.x", "x.5", "", "4.0")] == [
        False, False, False, False, True]


def test_log_lines_survives_bytes_that_are_not_utf8(tmp_path):
    log = tmp_path / "fs2_open.log"
    log.write_bytes(b"\xff\xfe junk\n... OpenGL init is complete!\n")
    assert probe.log_lines(log, probe.GL_INIT_LINE) == ["... OpenGL init is complete!"]


def test_mesa_engine_dir_is_rebuilt_on_a_rerun(tmp_path):
    fso, mesa, out = tmp_path / "fso", tmp_path / "mesa", tmp_path / "out"
    for folder, names in ((fso, [probe.FSO_EXE]), (mesa, list(probe.MESA_DLLS))):
        folder.mkdir()
        for name in names:
            (folder / name).write_text(name)
    first = probe.mesa_engine_dir(fso, mesa, out)
    (first / "left-over.dll").write_text("x")
    second = probe.mesa_engine_dir(fso, mesa, out)
    assert first == second == out / "fso-mesa"
    assert not (second / "left-over.dll").exists()


def test_each_cell_gets_its_desktop_handle_and_environment(monkeypatch, tmp_path):
    cells = []
    monkeypatch.setattr(probe, "one_cell", lambda c: cells.append(c) or {})
    monkeypatch.setattr(probe, "mesa_env", lambda m: {"mesa_from": m})
    monkeypatch.setattr(probe, "mesa_engine_dir", lambda f, m, o: o / f"copy-{f.name}-{m.name}")
    args = SimpleNamespace(mesa_dir=tmp_path / "mesa", fso_dir=tmp_path / "fso", out=tmp_path)
    probe.run_cells("w32", args, "WinSta0", "hdesk")
    assert [(c.w, c.gl, c.desk, c.hdesk, c.desktop) for c in cells] == [
        ("w32", "runner", "normal", None, "WinSta0\\Default"),
        ("w32", "runner", "hidden", "hdesk", f"WinSta0\\{probe.DESKTOP_NAME}"),
        ("w32", "mesa", "normal", None, "WinSta0\\Default"),
        ("w32", "mesa", "hidden", "hdesk", f"WinSta0\\{probe.DESKTOP_NAME}")]
    assert "PATH" in cells[0].env and cells[2].env == {"mesa_from": tmp_path / "mesa"}
    assert cells[2].fso_exe == tmp_path / "copy-fso-mesa" / probe.FSO_EXE
    assert cells[0].fso_exe == tmp_path / "fso" / probe.FSO_EXE


def test_without_mesa_only_runner_cells_run(monkeypatch, tmp_path):
    cells = []
    monkeypatch.setattr(probe, "one_cell", lambda c: cells.append(c.gl) or {})
    args = SimpleNamespace(mesa_dir=None, fso_dir=tmp_path, out=tmp_path)
    probe.run_cells(None, args, "WinSta0", "hdesk")
    assert cells == ["runner", "runner"]
