"""A shadow working folder: the engine sees the game data, its output files land elsewhere.

Some engine features write into the working directory (-output_sexps -> sexps.html,
-profile_write_file -> profiling.csv). Running from FS2PATH would drop those into the user's game
folder and overwrite any file of the same name there. The shadow folder holds a symlink to each of
FS2PATH's folders and .vp archives, so the engine finds its data as usual, while loose files it
writes at the TOP LEVEL of its working folder stay in the shadow folder (checked on the real
engine, probe results section 0).

Boundary (cross-family review XK-8): a write into a subfolder (e.g. data/...) follows that
folder's link and lands in FS2PATH. The writers the harness uses today (-output_sexps,
-output_script_lua/-output_script_json, -profile_write_file) write top-level files only;
anything that writes under data/ is not contained by this folder.

Only folders and .vp archives are linked, never loose files: a link named sexps.html would let the
engine's write follow it into FS2PATH.
"""
from pathlib import Path

from . import paths
from .errors import EXIT_REFUSED, HarnessError

_NO_LINK_PRIVILEGE = 1314     # Windows: ERROR_PRIVILEGE_NOT_HELD


def _link(link: Path, target: Path, folder: bool) -> None:
    """Make `link` a symbolic link to `target`.

    `folder` says what the target is. Linux does not care. Windows has two kinds of link and
    picks by looking at the target, so a link to a folder that does not exist yet would
    become a file link and never work; saying so here makes it a folder link either way.

    On Windows an account may create links only with Developer Mode on or as administrator
    (measured in the VM, 2026-11-17: error 1314 without, and api_docs worked with). That
    case is a refusal that says what to change; every other failure is passed on as it is.
    """
    try:
        link.symlink_to(target, target_is_directory=folder)
    except OSError as e:
        if getattr(e, "winerror", None) == _NO_LINK_PRIVILEGE:
            raise HarnessError(
                EXIT_REFUSED, "this needs a working folder made of links to the game "
                "folder, and Windows does not let this account create links",
                "turn on Developer Mode (Settings > System > For developers) and try "
                "again; an administrator prompt would also allow it, but the engine and "
                "its mods would then run with those rights") from e
        raise


def make(fs2: Path, dest: Path) -> Path:
    """Create `dest` and fill it with links to FS2PATH's folders and .vp archives; return dest."""
    dest.mkdir(parents=True, exist_ok=True)
    for entry in sorted(fs2.iterdir()):
        if entry.is_dir() or entry.suffix.lower() == ".vp":
            _link(dest / entry.name, entry, entry.is_dir())
    # launch() installs the bridge mod into FS2PATH after this folder is made, so link it even if
    # it does not exist yet; the link becomes valid once the bridge is installed.
    bridge = dest / paths.BRIDGE_MOD
    if not bridge.is_symlink():
        _link(bridge, fs2 / paths.BRIDGE_MOD, True)
    return dest


def collect(dest: Path, names: list[str]) -> dict[str, Path]:
    """Return {name: path} for the named output files that exist in the shadow folder."""
    return {n: dest / n for n in names if (dest / n).is_file() and not (dest / n).is_symlink()}
