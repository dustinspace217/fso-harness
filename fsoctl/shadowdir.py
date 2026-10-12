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


def make(fs2: Path, dest: Path) -> Path:
    """Create `dest` and fill it with links to FS2PATH's folders and .vp archives; return dest."""
    dest.mkdir(parents=True, exist_ok=True)
    for entry in sorted(fs2.iterdir()):
        if entry.is_dir() or entry.suffix.lower() == ".vp":
            (dest / entry.name).symlink_to(entry)
    # launch() installs the bridge mod into FS2PATH after this folder is made, so link it even if
    # it does not exist yet; the link becomes valid once the bridge is installed.
    bridge = dest / paths.BRIDGE_MOD
    if not bridge.is_symlink():
        bridge.symlink_to(fs2 / paths.BRIDGE_MOD)
    return dest


def collect(dest: Path, names: list[str]) -> dict[str, Path]:
    """Return {name: path} for the named output files that exist in the shadow folder."""
    return {n: dest / n for n in names if (dest / n).is_file() and not (dest / n).is_symlink()}
