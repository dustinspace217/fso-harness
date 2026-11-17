"""Where the harness keeps things.

Runtime root: $FSO_HARNESS_RUNTIME (tests) else the OS's default (platform.dirs: on Linux
$XDG_RUNTIME_DIR/fso-harness), mode 0700 so other users cannot read session channels. Session
ids are validated so a caller-supplied id can never escape the root.
"""
import os
import re
from pathlib import Path

from . import platform
from .errors import EXIT_NOT_FOUND, EXIT_USAGE, HarnessError

BRIDGE_MOD = "fso_harness_bridge"
_SID_RE = re.compile(r"^s[0-9a-f]{8}$")


def runtime_root_path() -> Path:
    """Return where the runtime root is, or would be made: $FSO_HARNESS_RUNTIME, else the
    platform's own place. Creates nothing (a launch can be refused over this path before
    any folder exists)."""
    override = os.environ.get("FSO_HARNESS_RUNTIME")
    return Path(override) if override else platform.dirs.runtime_root()


def runtime_root() -> Path:
    """Return the private runtime root, creating it with mode 0700."""
    root = runtime_root_path()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def state_dir() -> Path:
    """Return the harness's persistent state folder (mode 0700), creating it.

    $FSO_HARNESS_STATE (tests) else $XDG_STATE_HOME/fso-harness else ~/.local/state/fso-harness.
    WHY not the runtime root: that is tmpfs, emptied at logout and by `fsoctl cleanup`; the audit
    log must outlive both (QA SA-8).
    """
    override = os.environ.get("FSO_HARNESS_STATE")
    root = Path(override) if override else platform.dirs.state_root()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def sandbox_root() -> Path:
    """Return the folder (mode 0700, created) holding sandbox sessions' prefs folders on disk.

    $FSO_HARNESS_SANDBOX_ROOT (tests) else $XDG_CACHE_HOME/fso-harness/sandbox-runs else
    ~/.cache/fso-harness/sandbox-runs. WHY on disk and not in the runtime root (QA 2026-11-13
    AT2-1/SA2-1): the runtime root is the user's /run/user tmpfs (a few GB free); an untrusted
    engine filling its prefs there fills the folder the desktop's sockets live in, and the
    scope's memory limit is larger than that tmpfs. On disk the per-file limit and the runner's
    size watchdog (runner._Watchdog) stop it first.
    """
    override = os.environ.get("FSO_HARNESS_SANDBOX_ROOT")
    root = Path(override) if override else platform.dirs.sandbox_root()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def is_session_id(name: str) -> bool:
    """True if `name` has the shape of a session id ("s" and 8 hex digits)."""
    return bool(_SID_RE.match(name))


def session_dir(sid: str) -> Path:
    """Return the run folder for session `sid` (validated id; the folder may not exist yet)."""
    if not _SID_RE.match(sid):
        raise HarnessError(EXIT_NOT_FOUND, f"not a session id: {sid!r}", "use `fsoctl list`")
    return runtime_root() / sid


_MID_RE = re.compile(r"^m[0-9a-f]{8}$")


def matches_root() -> Path:
    """Return the folder that holds one subfolder per match (created, under the runtime root)."""
    root = runtime_root() / "matches"
    root.mkdir(exist_ok=True)
    return root


def match_dir(mid: str) -> Path:
    """Return the folder for match `mid` (validated id; the folder may not exist yet)."""
    if not _MID_RE.match(mid):
        raise HarnessError(EXIT_NOT_FOUND, f"not a match id: {mid!r}", "use `fsoctl match list`")
    return matches_root() / mid


def fs2path() -> Path:
    """Return the FreeSpace 2 game directory from $FS2PATH (must exist)."""
    value = os.environ.get("FS2PATH")
    if not value or not Path(value).is_dir():
        raise HarnessError(EXIT_USAGE, "FS2PATH is not set to a game directory",
                           "export FS2PATH=/path/to/FS2 (the folder with root_fs2.vp)")
    return Path(value)


def bridge_source() -> Path:
    """Return the bridge mod folder shipped inside the package."""
    return Path(__file__).resolve().parent / "bridge" / BRIDGE_MOD


def channel_dir(prefs: Path) -> Path:
    """Return the command channel folder for a session whose FSO_PREFERENCES_PATH is `prefs`.

    The engine resolves cf path "data/cache" to <prefs>/<first mod>/data/cache (probed 2026-10-12),
    and the bridge is always first in -mod.
    """
    return prefs / BRIDGE_MOD / "data" / "cache"


# The same folder relative to the session's prefs folder (session.prefs_dir), for safefs, which
# reaches every engine-written file from the prefs folder down (QA 2026-11-13 SA-1).
CHANNEL_REL = f"{BRIDGE_MOD}/data/cache"
