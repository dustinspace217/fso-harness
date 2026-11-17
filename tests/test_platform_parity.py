"""Each facility's Linux module and its Windows twin offer the same public functions with the
same signatures (cross-platform Plan B). A caller written against one works against the other;
a twin that drifts fails here, on Linux, before anyone runs it on Windows.
"""
import importlib
import inspect
from pathlib import Path

import pytest

import fsoctl.platform

# Functions whose result type differs by OS, each with the reason. Parameters must still match.
RETURNS_DIFFER = {
    "fsoctl.scope.start": "a Popen on Linux; Windows starts the process with CreateProcessW "
                          "and returns Started(pid). Callers use only .pid.",
}
# Functions only the Windows twin has, each with the reason Linux needs none.
WINDOWS_ONLY = {
    "fsoctl.platform.win_scope.run": "the wait-for-it form `fsoctl build` uses. On Linux "
                                     "`systemd-run --scope` is itself that form: build.py "
                                     "runs it with subprocess.run and scope.py has nothing "
                                     "to add.",
}
# The mirror: functions only the Linux module has, each with its reason. Kept as a table of
# names, not as "whatever Windows happens to reach": the rule stays mechanical.
LINUX_ONLY = {
    "fsoctl.safefs.usage": "the sandbox's size watchdog is its only caller (sandbox.py), and "
                           "a sandbox session is refused on Windows",
    "fsoctl.safefs.clear_tree": "the sandbox's cleanup of a prefs folder is its only caller "
                                "(sandbox.py), and a sandbox session is refused on Windows",
}
PAIRS = [
    ("fsoctl.platform.linux_locks", "fsoctl.platform.win_locks"),
    ("fsoctl.platform.linux_dirs", "fsoctl.platform.win_dirs"),
    ("fsoctl.platform.linux_procs", "fsoctl.platform.win_procs"),
    ("fsoctl.safefs", "fsoctl.platform.win_safefs"),
    ("fsoctl.scope", "fsoctl.platform.win_scope"),
]


def public_functions(module) -> dict:
    """name -> signature for the functions a module defines itself and does not hide."""
    return {name: inspect.signature(fn) for name, fn in vars(module).items()
            if inspect.isfunction(fn) and fn.__module__ == module.__name__
            and not name.startswith("_")}


@pytest.mark.linux
@pytest.mark.parametrize("linux_name, windows_name", PAIRS)
def test_every_facility_pair_has_the_same_public_signatures(linux_name, windows_name):
    ours = public_functions(importlib.import_module(linux_name))
    theirs = public_functions(importlib.import_module(windows_name))
    assert ours, f"{linux_name} has no public functions: the comparison would be empty"
    extra = {name for name in theirs if f"{windows_name}.{name}" in WINDOWS_ONLY}
    ours = {name: sig for name, sig in ours.items() if f"{linux_name}.{name}" not in LINUX_ONLY}
    assert sorted(set(theirs) - extra) == sorted(ours)
    for name, signature in ours.items():
        assert str(theirs[name].parameters) == str(signature.parameters), name
        if f"{linux_name}.{name}" not in RETURNS_DIFFER:
            assert theirs[name].return_annotation == signature.return_annotation, name


def test_win_modules_import_on_linux():
    # Windows-only calls live inside functions; importing must work everywhere
    folder = Path(fsoctl.platform.__file__).parent
    names = sorted(p.stem for p in folder.glob("win_*.py"))
    assert {"win_locks", "win_dirs", "win_scope", "win_safefs"} <= set(names)
    for name in names:
        importlib.import_module(f"fsoctl.platform.{name}")


def test_every_windows_only_function_exists_and_has_no_linux_twin():
    # an entry left behind after its function is gone, or after Linux gains the same
    # function, would quietly excuse a real drift
    for full_name in WINDOWS_ONLY:
        module_name, name = full_name.rsplit(".", 1)
        assert name in public_functions(importlib.import_module(module_name)), full_name
        linux_name = next(lin for lin, win in PAIRS if win == module_name)
        assert name not in public_functions(importlib.import_module(linux_name)), full_name


@pytest.mark.linux                  # it imports the Linux modules, as the comparison does
def test_every_linux_only_function_exists_and_has_no_windows_twin():
    # as above, the other way round: code kept on Windows only to satisfy this test is
    # code written for the guard and not for the goal (QA Plan B, AR-3)
    for full_name in LINUX_ONLY:
        module_name, name = full_name.rsplit(".", 1)
        assert name in public_functions(importlib.import_module(module_name)), full_name
        windows_name = next(win for lin, win in PAIRS if lin == module_name)
        assert name not in public_functions(importlib.import_module(windows_name)), full_name
