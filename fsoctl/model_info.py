"""model_info: per-model face counts from the engine's -pofspew.

-pofspew makes the engine load every .pof it can find (up to 1000), write pofspew.txt through its
data write path (<prefs>/<first mod>/data/pofspew.txt in a harness session) and exit by itself
(freespace.cpp). A full MediaVPs run took 14 s and covered 363 models (2026-10-12).
"""
import argparse
import os
import re

from . import launcher, session
from .cli import register
from .errors import EXIT_ENGINE, EXIT_USAGE, HarnessError
from .launcher import LaunchRequest

_SEPARATOR = re.compile(r"^-{20,}\s*$")
_MODEL_TOTAL = re.compile(r"^Model total (\d+)")
_LOD = re.compile(r"^LOD (\d+)")
_LOD_TOTAL = re.compile(r"^TOTAL: (\d+)")
_LOD_DESTROYED = re.compile(r"^TOTAL destroyed faces (\d+)")


def _parse_block(lines: list[str]) -> dict | None:
    """Parse one model's block; None when the block has no model name."""
    if not lines or not lines[0].lower().endswith(".pof"):
        return None
    model = {"name": lines[0], "total_faces": None, "lods": []}
    for line in lines[1:]:
        if m := _MODEL_TOTAL.match(line):
            model["total_faces"] = int(m.group(1))
        elif m := _LOD.match(line):
            model["lods"].append({"lod": int(m.group(1)), "faces": None, "destroyed": None})
        elif (m := _LOD_TOTAL.match(line)) and model["lods"]:
            model["lods"][-1]["faces"] = int(m.group(1))
        elif (m := _LOD_DESTROYED.match(line)) and model["lods"]:
            model["lods"][-1]["destroyed"] = int(m.group(1))
    return model


def parse(text: str) -> list[dict]:
    """Return [{"name", "total_faces", "lods": [{"lod", "faces", "destroyed"}]}] per model."""
    models, block = [], []
    for raw in [*text.splitlines(), "-" * 40]:   # trailing separator closes the last block
        line = raw.strip()
        if _SEPARATOR.match(line):
            parsed = _parse_block(block)
            if parsed:
                models.append(parsed)
            block = []
        elif line:
            block.append(line)
    return models


def model_info(mods: list[str], name_filter: str | None = None, binary: str | None = None,
               timeout: float = 900) -> dict:
    """Run -pofspew and return the parsed models (optionally only names containing the filter).

    The result carries the engine's exit_info and "complete": True only for a clean exit
    (return code 0; game_main returns 0 after the spew, freespace.cpp). WHY: an engine that
    crashes part-way leaves a partial pofspew.txt that parses fine, and without this flag the
    short list would read as the whole model set (QA SF-9).
    """
    exe = binary or os.environ.get("FSO_BIN")
    if not exe:
        raise HarnessError(EXIT_USAGE, "no engine binary", "pass --binary or export FSO_BIN")
    sess = launcher.run_to_exit(LaunchRequest(binary=exe, mods=mods, extra_args=["-pofspew"]),
                                timeout)
    found = sorted(session.prefs_dir(sess).rglob("pofspew.txt"))
    if not found:
        raise HarnessError(EXIT_ENGINE, "the engine wrote no pofspew.txt",
                           f"see `fsoctl log {sess.id}` (exit {sess.exit_info})")
    models = parse(found[0].read_text(errors="replace"))
    if name_filter:
        models = [m for m in models if name_filter.lower() in m["name"].lower()]
    info = sess.exit_info
    out = {"id": sess.id, "count": len(models), "models": models, "file": str(found[0]),
           "exit_info": info, "complete": bool(info) and info.get("returncode") == 0}
    if not out["complete"]:
        out["hint"] = (f"the engine did not exit cleanly, so the list may be partial; "
                       f"run `fsoctl crash_report {sess.id}`")
    return out


def _cfg(p: argparse.ArgumentParser) -> None:
    """Add the `model_info` verb's arguments."""
    p.add_argument("--mods", default="")
    p.add_argument("--filter", help="only models whose file name contains this")
    p.add_argument("--binary")
    p.add_argument("--timeout", type=float, default=900)


@register("model_info", "face counts per model and LOD (engine -pofspew)", _cfg)
def _cli(args: argparse.Namespace) -> dict:
    """CLI wrapper for model_info()."""
    return model_info([m for m in args.mods.split(",") if m], args.filter, args.binary,
                      args.timeout)
