from fsoctl import xwin


def test_editor_title():
    assert xwin.editor_title("/rt/fred-missions/ab12/SM1-01.fs2") == "SM1-01.fs2"
    assert xwin.editor_title(None) == "Untitled"


def test_qtfred_main_window_skips_splash_and_dialogs(monkeypatch):
    wins = [("1", "qtFRED"), ("2", "Pre-save Error Check"), ("3", "SM1-01.fs2")]
    monkeypatch.setattr(xwin, "windows", lambda display, pid: wins)
    assert xwin.main_window(":1", 5, "qtfred", "/x/SM1-01.fs2") == "3"
    monkeypatch.setattr(xwin, "windows", lambda display, pid: [("1", "qtFRED")])
    assert xwin.main_window(":1", 5, "qtfred", "/x/SM1-01.fs2") is None   # still loading


def test_game_main_window_is_its_only_window(monkeypatch):
    monkeypatch.setattr(xwin, "windows", lambda display, pid: [("9", "FreeSpace 2 Open")])
    assert xwin.main_window(":1", 5, "game", None) == "9"
