"""Finding a program to run by its name.

WHY this is not just shutil.which (QA Plan B, SA-1): on Windows a bare program name is looked
for in the CURRENT folder before PATH, both by Windows when it starts a process (Microsoft's
CreateProcessW page) and by shutil.which (its source). Read, not run here. The folder a harness
command is typed in may be the checkout or the mod under test, so a file named cmake.exe or
netstat.exe lying there would be what the harness runs, as the user, even for a session of a
trusted build. So on Windows the harness walks PATH itself, never looks in the current folder,
and callers start what was found by its full path (with a full path nothing is searched). Other
systems do not search the current folder and keep the usual lookup.
"""
import os
import shutil
import sys

# What counts as a program on Windows. Not .bat or .cmd: those are run by the command
# interpreter, which has search rules of its own.
_SUFFIXES = (".exe", ".com")


def _names_one_place(folder: str, rules=os.path) -> bool:
    """True for a PATH entry that means the same folder wherever the command was typed:
    absolute and, by Windows' path rules, with a drive or a share in front. `rules` is the
    path module to judge by (this system's; tests pass ntpath and posixpath).

    WHY the drive is asked for separately (round-2 SAF-6): a Windows entry like "\\tools"
    is on the CURRENT drive. Python 3.12 calls it absolute and 3.13 does not (its
    documentation says so); the answer here must not change with the Python version."""
    if not rules.isabs(folder):
        return False
    return rules.sep == "/" or bool(rules.splitdrive(folder)[0])


def find(name: str, windows: bool | None = None) -> str | None:
    """Return the full path of program `name` (a bare name, no folder) as found on PATH, or
    None. `windows` says which rules to use; None means this system's (tests pass it).

    On Windows: each PATH entry in order, skipping empty, "." and relative entries (all of
    which mean "wherever the command was typed"), taking the first `name`.exe or `name`.com
    that is a file. Quotes around an entry are dropped.
    """
    if windows is None:
        windows = sys.platform == "win32"
    if not windows:
        return shutil.which(name)
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        folder = entry.strip('"')
        if not _names_one_place(folder):          # "", "." and every relative entry
            continue
        for suffix in _SUFFIXES:
            candidate = os.path.join(folder, name + suffix)
            if os.path.isfile(candidate):
                return candidate
    return None
