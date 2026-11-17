"""One module per OS-dependent facility, picked once by sys.platform (cross-platform Plan B).

The rest of the harness gets every OS-dependent FACILITY from this package and never reads
sys.platform itself:

    from . import platform
    platform.scope.start(...)        # systemd scope on Linux, Job Object on Windows
    platform.locks.lock_exclusive(f)
    platform.procs.pid_alive(pid)    # /proc on Linux, a process handle on Windows
    platform.needs_linux("the keys verb", "Plan C")

Where a caller's own logic differs by OS (which command line, which words in a hint, whether
to retry), it asks a capability flag below when the difference is a property of the OS
(GROUP_TIME_LIMIT, BUSY_FILES_REFUSE, GROUPS_PER_LOGIN, PREFS_PATH_ASCII_ONLY), or
platform.NAME when it is one of a kind. NAME is read in launcher.py, build.py and
compare.py; a new OS (Plan D) has to visit each of those and each flag.

WHY modules picked at import and not classes or a registry: every facility already is a module
of plain functions on Linux (scope.py, safefs.py, runner.py). Keeping that shape means the
Linux code does not move or change, its tests keep their patch targets, and "which code runs
on Windows" is answered by reading the dozen lines below. tests/test_platform_parity.py holds
each Windows module to its Linux twin's public names and signatures.

WHY the Linux modules stay where they are (fsoctl/scope.py, not platform/linux_scope.py): the
spec says "moved, not rewritten", and not moving at all is the smallest form of that. Only
code that sat inside mixed modules (session's flock calls, paths' default folders) moved here.

macOS gets a name and nothing else yet (Plan D): it falls through to the Linux modules, and
every feature guarded by needs_linux refuses there.
"""
import sys
from typing import NoReturn

from ..errors import EXIT_REFUSED, HarnessError


def name_for(sys_platform: str) -> str:
    """Our name for an OS, from Python's sys.platform: "linux", "windows" or "macos"."""
    if sys_platform == "win32":
        return "windows"
    if sys_platform == "darwin":
        return "macos"
    return "linux"


NAME = name_for(sys.platform)

if NAME == "windows":
    from . import win_capture as capture
    from . import win_crash as crash
    from . import win_dirs as dirs
    from . import win_locks as locks
    from . import win_procs as procs
    from . import win_safefs as safefs
    from . import win_scope as scope
    # The runner is named, not imported: the launcher starts it as `python -m <module>`.
    RUNNER_MODULE = "fsoctl.platform.win_runner"
else:
    from .. import safefs, scope
    from . import linux_dirs as dirs
    from . import linux_locks as locks
    from . import linux_procs as procs
    RUNNER_MODULE = "fsoctl.runner"
    # Linux takes screenshots with ImageMagick from the session's X display (xinput.py); only
    # an OS without that needs a capture module of its own.
    capture = None
    # Linux reads a crash from systemd-coredump with gdb (crash.py); only an OS where the
    # engine leaves its own dump needs a crash module.
    crash = None

# True where a file that another process has open refuses to be renamed over, and may refuse
# to be read or deleted for that moment (Windows; measured in probes/xplat/record_race.py).
# Code that replaces or reads a file other processes read tries again there (session.py,
# channel.py). On Linux a rename swaps the file under its readers and nothing is retried.
BUSY_FILES_REFUSE = NAME == "windows"

# True where the engine cannot keep its preferences in a folder whose path has a letter
# outside ASCII. On Windows FSO reads FSO_PREFERENCES_PATH as bytes in the local code page
# and then walks it as UTF-8 (code/osapi/osapi.cpp, getPreferencesPath: "On Windows, we
# currently do not support Unicode paths"); by that source it then reports an error or
# uses the game folder instead. Measured with probe 12 (probes/xplat/accent_root.py, VM,
# 2026-11-17): under a folder named "José" the engine never started its bridge and did not
# close when asked, and the launch ended it after its whole wait; the same run with a plain
# name, a space and an apostrophe worked from launch to cleanup. Which of the source's two
# branches the engine took was not looked at.
PREFS_PATH_ASCII_ONLY = NAME == "windows"

# True where a process group's name is known only inside the login that made it: a Windows
# job, which another login of the same account sees as "no such job" while its session
# runs (measured over ssh). Only there does the launcher also ask about a session's
# recorded runner and engine before it calls the session gone (launcher._gone).
GROUPS_PER_LOGIN = NAME == "windows"

# True where the OS itself ends a process group at its time limit (a systemd scope's
# RuntimeMaxSec). A Windows job can cap CPU time but has no wall-clock limit: there the
# runner ends the run, and a session whose runner never got going, or is known to be gone,
# is ended by the next cleanup (launcher._end_unattended).
GROUP_TIME_LIMIT = NAME != "windows"

__all__ = ["BUSY_FILES_REFUSE", "GROUPS_PER_LOGIN", "GROUP_TIME_LIMIT", "NAME",
           "PREFS_PATH_ASCII_ONLY",
           "RUNNER_MODULE", "capture", "crash",
           "dirs", "locks", "name_for", "needs_linux", "procs", "safefs", "scope", "unsupported"]


def unsupported(feature: str, tracked: str) -> NoReturn:
    """Refuse a feature this OS does not have yet: EXIT_REFUSED naming the feature, the OS and
    the plan that tracks it. WHY always loud: a verb that quietly did nothing on Windows would
    read to an agent as "it worked" (the spec's no-silent-fallback rule)."""
    raise HarnessError(EXIT_REFUSED, f"{feature} is not available on {NAME}",
                       f"tracked in {tracked}")


def needs_linux(feature: str, tracked: str) -> None:
    """Refuse `feature` (see unsupported) unless this is Linux. Called first thing by every
    verb and launch option that only Linux has, before any session or file is looked at, so
    the answer on Windows is the refusal and not some later error about a missing tool."""
    if NAME != "linux":
        unsupported(feature, tracked)
