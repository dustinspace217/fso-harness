"""Shared pytest fixtures.

tmp_runtime: every unit test that touches sessions gets its own private runtime root and state
folder, so tests never see each other's sessions or the user's real ones, and never write the
real audit log.
fresh_scope_cache (autouse): launcher's per-process scope cache starts empty in every test.
no_real_pulse (autouse): sound._pactl raises PactlMissing and sound.check_device_names passes
unless a test replaces them, and the sound lock lives in the test's temp folder, so no test
loads, unloads or lists anything on the user's real sound server. Engine-tier tests included:
only a test marked "sound" talks to the real server (none does yet).
no_host_ports (autouse): launcher.port_free answers "free" without binding anything (and launch does
not wait for a game port no engine can own), so the unit
tier never opens a socket on the host and parallel workers cannot race for one; tests of the
test bind itself mark themselves "host_ports". Real-engine tiers keep the real test bind.
engine_env: engine-tier tests run only when the real game folder and engine binary are configured.

Which tests run on which OS (cross-platform Plan B): the unit tier grew up on Linux, and part
of it tests Linux facilities (systemd scopes, fcntl locks, the sandbox, PipeWire) or features
other systems do not have yet (the editors, matches). LINUX_ONLY_FILES lists the files that
are wholly so, each with its reason; they are not collected anywhere else. Single tests in
mixed files carry the marker `linux`. Tests that create symbolic links carry `symlinks` and
are skipped where the account may not create them (Windows without Developer Mode).
"""
import functools
import os
import sys
import tempfile
from pathlib import Path

import pytest

from fsoctl import launcher, platform, scope, sound
from fsoctl.errors import EXIT_LAUNCH

# Test file -> what it tests that only Linux has (so far). Not collected on other systems:
# several cannot even be imported there (fcntl, resource, os.O_DIRECTORY).
LINUX_ONLY_FILES = {
    "test_b_bridge_install.py": "holds the bridge lock with fcntl, standing in for another process",
    "test_b_crash.py": "the Linux crash report (coredumpctl and gdb)",
    "test_b_launcher.py": "holds the launch lock with fcntl; the systemd launch path",
    "test_b_runner.py": "the Linux runner (Xvfb, signals, process groups)",
    "test_crash_unit.py": "the Linux crash report; Windows has tests/test_platform_crash.py",
    "test_d_build.py": "holds the build lock with fcntl; the systemd-run build command",
    "test_d_docs.py": "holds the api_docs lock with fcntl",
    "test_d_fred.py": "the editor verbs (Windows: Plan C)",
    "test_fred_check_unit.py": "the editor verbs (Windows: Plan C)",
    "test_match_real.py": "matches (Windows: Plan E)",
    "test_match_unit.py": "matches (Windows: Plan E)",
    "test_platform_select.py": "reloads the platform package as another OS around the Linux "
                               "modules, which import only on Linux",
    "test_safefs.py": "the Linux safefs (O_NOFOLLOW walks); Windows has "
                      "tests/test_platform_safefs.py",
    "test_sandbox.py": "the bwrap sandbox",
    "test_sandbox_mutation3e.py": "the bwrap sandbox",
    "test_sandbox_plants.py": "the bwrap sandbox",
    "test_sandbox_round3.py": "the bwrap sandbox",
    "test_sandbox_round3b.py": "the bwrap sandbox",
    "test_sandbox_round3c.py": "the bwrap sandbox",
    "test_sound.py": "per-session sound through PipeWire (Windows: Plan E)",
    "test_web_panel.py": "the standalone server's web panel (multiplayer; Windows: Plan E)",
}


def pytest_ignore_collect(collection_path, config):
    """Leave the Linux-only test files uncollected on every other system (see above)."""
    if sys.platform != "linux" and collection_path.name in LINUX_ONLY_FILES:
        return True
    return None


def pytest_report_header(config):
    """Say in the run's header how much was left out, so a short Windows run is not mistaken
    for the whole tier."""
    if sys.platform == "linux":
        return None
    return (f"fso-harness: {len(LINUX_ONLY_FILES)} test files test Linux facilities and are "
            "not collected on this OS (tests/conftest.py, LINUX_ONLY_FILES)")


@functools.cache
def _symlinks_allowed() -> bool:
    """True when this account may create a symbolic link (asked once, in a temp folder)."""
    with tempfile.TemporaryDirectory() as folder:
        try:
            os.symlink(os.path.join(folder, "target"), os.path.join(folder, "link"))
        except OSError:
            return False
    return True


def pytest_collection_modifyitems(config, items):
    """Skip `linux` tests off Linux and `symlinks` tests where links cannot be made."""
    for item in items:
        if item.get_closest_marker("linux") and sys.platform != "linux":
            item.add_marker(pytest.mark.skip(reason="tests Linux behaviour (a Linux tool, "
                                                    "path or feature not on this OS yet)"))
        if item.get_closest_marker("symlinks") and not _symlinks_allowed():
            item.add_marker(pytest.mark.skip(reason="this account may not create symbolic "
                                                    "links (Windows: turn on Developer Mode)"))


@pytest.fixture
def tmp_runtime(tmp_path, monkeypatch):
    """Point the harness runtime root at a private temp dir for one test; return that dir.

    Also points the persistent state folder (the audit log) at tmp_path/"state", so no unit test
    appends to the user's real ~/.local/state/fso-harness/audit.log, and the sandbox sessions'
    on-disk prefs root at tmp_path/"sandbox-runs".
    """
    root = tmp_path / "rt"
    monkeypatch.setenv("FSO_HARNESS_RUNTIME", str(root))
    monkeypatch.setenv("FSO_HARNESS_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FSO_HARNESS_SANDBOX_ROOT", str(tmp_path / "sandbox-runs"))
    # The tests' sandbox root is on /tmp, which has less free space than the 10 GiB floor.
    monkeypatch.setenv("FSO_HARNESS_SANDBOX_FREE_MIN", str(1 << 20))
    return root


@pytest.fixture(autouse=True)
def fresh_scope_cache():
    """Clear launcher's in-process scope cache before every test (QA TA2-10), so one test's
    cached "scope active" answer can never reach another test."""
    launcher._scope_seen.clear()


@pytest.fixture(autouse=True)
def no_real_pulse(monkeypatch, request, tmp_path):
    """Keep every test off the user's sound server unless it is marked "sound" (see above)."""
    if request.node.get_closest_marker("sound"):
        return

    def refuse(*args):
        raise sound.PactlMissing(EXIT_LAUNCH, "pactl is not installed (test stub)", "")
    monkeypatch.setattr(sound, "_pactl", refuse)
    monkeypatch.setattr(sound, "check_device_names", lambda binary, sid: None)
    monkeypatch.setattr(sound, "_runtime_dir", lambda: str(tmp_path))


@pytest.fixture(autouse=True)
def roomy_machine(monkeypatch, request):
    """Give the unit tier a fixed machine with room to spare (64 GB available),
    so launch tests do not depend on the memory of the box they run on: the launch refusal
    (scope.room_for_engine) reads /proc/meminfo, and a 16 GB CI runner with 6 GB free refused
    every launch test while this workstation passed them. Tests of the refusal itself set this
    reader again with their own numbers, which wins over this default. Real-engine tiers
    (engine, qtfred, vulkan, sound) keep the real reader: they start real processes."""
    if any(request.node.get_closest_marker(m) for m in ("engine", "qtfred", "vulkan", "sound")):
        return
    monkeypatch.setattr(scope, "_mem_available_kb", lambda: 64 * 1048576)
    # The same for the scope module this OS really uses (the Job Object twin on Windows).
    monkeypatch.setattr(platform.scope, "_mem_available_kb", lambda: 64 * 1048576)


@pytest.fixture(autouse=True)
def no_host_ports(monkeypatch, request):
    """See the module docstring: port_free is a stub unless the test opts in or is a real tier."""
    if any(request.node.get_closest_marker(m)
           for m in ("engine", "qtfred", "vulkan", "sound", "host_ports")):
        return
    monkeypatch.setattr(launcher, "port_free", lambda proto, port: True)
    # Launch tests run no engine, so nothing can own the game port: do not wait for it.
    monkeypatch.setattr(launcher, "_GAME_PORT_WAIT", 0.0)


@pytest.fixture
def engine_env():
    """Return (fs2path, fso_bin) or skip when the real engine isn't configured."""
    fs2 = os.environ.get("FS2PATH")
    fso = os.environ.get("FSO_BIN")
    if not fs2 or not fso or not Path(fso).exists() or not Path(fs2).is_dir():
        pytest.skip("FS2PATH and FSO_BIN must point at a game dir and an engine binary")
    return Path(fs2), Path(fso)
