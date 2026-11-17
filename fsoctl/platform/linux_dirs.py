"""Where the harness keeps things by default on Linux (moved here from paths.py, Plan B).

Only the defaults live here. paths.py still reads the FSO_HARNESS_* overrides first and
creates the folders; win_dirs.py is the Windows twin (same three functions).
"""
import os
from pathlib import Path

from ..errors import EXIT_USAGE, HarnessError


def runtime_root() -> Path:
    """$XDG_RUNTIME_DIR/fso-harness: the user's private tmpfs, emptied at logout."""
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if not xdg:
        raise HarnessError(EXIT_USAGE, "XDG_RUNTIME_DIR is not set",
                           "run inside a user session or set FSO_HARNESS_RUNTIME")
    return Path(xdg) / "fso-harness"


def state_root() -> Path:
    """$XDG_STATE_HOME/fso-harness, else ~/.local/state/fso-harness (outlives a logout)."""
    xdg = os.environ.get("XDG_STATE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "state") / "fso-harness"


def sandbox_root() -> Path:
    """$XDG_CACHE_HOME/fso-harness/sandbox-runs, else ~/.cache/fso-harness/sandbox-runs."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "fso-harness" / "sandbox-runs"
