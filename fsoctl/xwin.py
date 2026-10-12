"""Find a session's windows on its virtual display.

Windows are looked up by the engine's process id. For qtFRED that returns the splash screen too
("qtFRED", shown while it loads) and later its dialogs, so the editor is the window whose title is
the mission file name, or "Untitled" when no mission was given (seen on the real program,
2026-10-12). For the game there is one window.
"""
import os
import subprocess
from pathlib import Path

_TIMEOUT = 10


def _xdotool(display: str, *args: str) -> subprocess.CompletedProcess:
    """Run xdotool against `display` (never the user's display)."""
    env = {**os.environ, "DISPLAY": display}
    env.pop("WAYLAND_DISPLAY", None)
    return subprocess.run(["xdotool", *args], env=env, capture_output=True, text=True,
                          timeout=_TIMEOUT, check=False)


def windows(display: str, pid: int) -> list[tuple[str, str]]:
    """Return [(window id, title)] for every visible window of process `pid` ([] if none)."""
    found = _xdotool(display, "search", "--onlyvisible", "--pid", str(pid)).stdout.split()
    return [(w, _xdotool(display, "getwindowname", w).stdout.strip()) for w in found]


def editor_title(mission: str | None) -> str:
    """Return the title qtFRED gives its editor window for this mission."""
    return Path(mission).name if mission else "Untitled"


def main_window(display: str, pid: int, kind: str, mission: str | None) -> str | None:
    """Return the session's main window id, or None if it is not up yet."""
    wins = windows(display, pid)
    if kind != "qtfred":
        return wins[0][0] if wins else None
    want = editor_title(mission)
    return next((w for w, title in wins if title == want), None)
