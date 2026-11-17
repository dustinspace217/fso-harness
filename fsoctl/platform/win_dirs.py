"""Where the harness keeps things by default on Windows: under %LOCALAPPDATA%\\fso-harness.

The twin of linux_dirs.py. WHY LOCALAPPDATA: it is the per-user, not-roaming application
folder, private to the user by its default permissions, and it is on disk. Windows has no
per-login tmpfs like /run/user; the run folder therefore survives a logout, and `fsoctl
cleanup` (and each launch's own clean-up of old runs) is what empties it.
"""
import os
from pathlib import Path

from ..errors import EXIT_USAGE, HarnessError


def _base() -> Path:
    """%LOCALAPPDATA%\\fso-harness (EXIT_USAGE when the variable is not set)."""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise HarnessError(EXIT_USAGE, "LOCALAPPDATA is not set",
                           "run as a logged-in user or set FSO_HARNESS_RUNTIME")
    return Path(local) / "fso-harness"


def runtime_root() -> Path:
    """%LOCALAPPDATA%\\fso-harness\\run: one folder per session."""
    return _base() / "run"


def state_root() -> Path:
    """%LOCALAPPDATA%\\fso-harness\\state: the audit log."""
    return _base() / "state"


def sandbox_root() -> Path:
    """%LOCALAPPDATA%\\fso-harness\\sandbox (named for parity; Windows has no sandbox mode)."""
    return _base() / "sandbox"
