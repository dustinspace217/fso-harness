"""Install the bridge mod into the game directory, refreshing it when the harness version changes.

WHY a content hash: an older bridge left in FS2PATH would otherwise run silently with a newer
harness (Review Focus 5). The hash covers the INSTALLED tree's contents, not just its stamp file,
so an installed copy edited in place is caught too (QA MU-1 / SL-3).

The installed folder is shared by every session (the engine finds the bridge as the mod folder
FS2PATH/fso_harness_bridge, first in -mod), so a refresh is a takeover of shared state (QA SL-3):
- it runs under a lock keyed by the game folder (state_dir()/locks/bridge-<hash of FS2PATH>.lock),
  so two launches never copy over each other, whatever runtime root each one uses; nothing extra
  is written into FS2PATH for it (fix-wave-2 ruling R1);
- it is refused while ANY harness scope is running (systemd's list, not one runtime root's
  sessions): replacing the folder under a starting engine can hand it a half-old, half-new
  bridge. Scopes are not tagged with their game folder, so a session on another FS2PATH also
  blocks it; that conservative refusal is accepted (R1) and the hint says so;
- the new copy is built in a temporary folder inside FS2PATH and renamed into place;
- a folder of that name without the harness stamp is never deleted (it is not ours).
WHY not a versioned folder per hash: the fixed folder name is assumed by -mod, channel_dir and the
shadow folders, and old versions would pile up in the user's game folder (QA fix-wave ruling).
"""
import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from . import paths, scope, session
from .errors import EXIT_REFUSED, HarnessError

STAMP = ".harness-version"
# Contract C5: the bridge reads its version from this file at load time (bridge_info "version").
VERSION_FILE = Path("data") / "text" / "fsoharness-version.txt"
# Files that are not part of the bridge's content: placeholders, and the two files install writes.
_NOT_CONTENT = {Path(".gitkeep"), Path(STAMP), VERSION_FILE}
_LOCK_WAIT = 60.0   # seconds to wait for another install; copying the bridge takes well under 1 s


def _tree_hash(root: Path) -> str:
    """Return a short hash over a bridge tree's relative paths and bytes (see _NOT_CONTENT)."""
    h = hashlib.sha256()
    files = sorted(p for p in root.rglob("*") if p.is_file())
    for f in files:
        rel = f.relative_to(root)
        if rel in _NOT_CONTENT or rel.name == ".gitkeep":
            continue
        h.update(rel.as_posix().encode() + b"\0")   # \0: a name can never run into the bytes
        h.update(f.read_bytes() + b"\0")
    return h.hexdigest()[:12]


def _current(target: Path, version: str) -> bool:
    """True if the installed bridge's contents AND its version file match `version`."""
    if not target.is_dir():
        return False
    vfile = target / VERSION_FILE
    return (vfile.is_file() and vfile.read_text() == version
            and _tree_hash(target) == version)


def _replace(fs2: Path, target: Path, version: str) -> None:
    """Copy the shipped bridge into a temp folder in FS2PATH, stamp it, and rename it into place.

    The old folder (already checked to carry the stamp) is renamed into the temp folder and
    deleted with it, so nothing half-copied is ever at the real path. If the new copy cannot be
    renamed into place, the old one is renamed back first (QA YK-6), so FS2PATH is never left
    without a bridge.
    """
    work = Path(tempfile.mkdtemp(prefix=f".{paths.BRIDGE_MOD}.", dir=fs2))
    try:
        new = work / "new"
        shutil.copytree(paths.bridge_source(), new, ignore=shutil.ignore_patterns(".gitkeep"))
        (new / VERSION_FILE).parent.mkdir(parents=True, exist_ok=True)
        (new / VERSION_FILE).write_text(version)
        (new / STAMP).write_text(version)
        old = work / "old"
        if target.exists():
            os.rename(target, old)
        try:
            os.rename(new, target)
        except OSError:
            if old.exists():
                os.rename(old, target)
            raise
    finally:
        shutil.rmtree(work)


def _lock_path(fs2: Path) -> Path:
    """Return this game folder's bridge lock: state_dir()/locks/bridge-<16 hex>.lock (0700 dir)."""
    key = hashlib.sha256(os.path.realpath(fs2).encode()).hexdigest()[:16]
    locks = paths.state_dir() / "locks"
    locks.mkdir(mode=0o700, exist_ok=True)
    return locks / f"bridge-{key}.lock"


def install(fs2: Path) -> dict:
    """Ensure FS2PATH/fso_harness_bridge matches the shipped bridge; return path/version/updated.

    Raises EXIT_REFUSED when a refresh is needed but any harness scope is running (launch calls
    this before its own session's scope exists, so every listed scope is another session), or
    when the folder exists without the harness stamp.
    """
    version = _tree_hash(paths.bridge_source())
    target = fs2 / paths.BRIDGE_MOD
    with session.file_lock(_lock_path(fs2), _LOCK_WAIT):
        if _current(target, version):
            return {"path": str(target), "version": version, "updated": False}
        busy = [u.removeprefix("fso-harness-").removesuffix(".scope")
                for u in scope.active_units()]
        if busy:
            raise HarnessError(EXIT_REFUSED,
                               f"the bridge in {target} is out of date, but harness sessions are "
                               "running",
                               f"stop sessions {', '.join(busy)} (`fsoctl stop <id>`), then "
                               "launch again. Sessions on another game folder count too: "
                               "running scopes do not record which game folder they use")
        if target.exists() and not (target / STAMP).is_file():
            raise HarnessError(EXIT_REFUSED,
                               f"{target} exists but was not installed by fso-harness (no {STAMP})",
                               "move or rename that folder; the harness needs this folder name")
        _replace(fs2, target, version)
    return {"path": str(target), "version": version, "updated": True}
