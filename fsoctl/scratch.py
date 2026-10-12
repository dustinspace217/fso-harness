"""Scratch mods: throwaway mod folders in FS2PATH that the harness creates and may delete.

A scratch mod is FS2PATH/fso_harness_scratch_<6 hex> with the usual data/ subfolders and a
.harness-scratch stamp. remove() deletes only folders whose name has exactly that shape AND that
carry the stamp, so it can never delete a user's mod, even when handed a hostile name.
"""
import argparse
import json
import re
import secrets
import shutil
import time

from . import paths
from .cli import register
from .errors import EXIT_REFUSED, HarnessError

PREFIX = "fso_harness_scratch_"
STAMP = ".harness-scratch"
_NAME = re.compile(r"^fso_harness_scratch_[0-9a-f]{6}$")
_SUBDIRS = ("missions", "tables", "scripts", "models", "maps", "effects")


def create(label: str | None = None) -> dict:
    """Create a new scratch mod; return {"name", "path", "dirs"}. Use the name in --mods."""
    fs2 = paths.fs2path()
    for _ in range(16):  # bounded: 16 collisions in 16^6 names is effectively impossible
        name = PREFIX + secrets.token_hex(3)
        if not (fs2 / name).exists():
            break
    else:
        raise HarnessError(EXIT_REFUSED, "could not pick a free scratch name", "retry")
    root = fs2 / name
    dirs = {}
    for sub in _SUBDIRS:
        (root / "data" / sub).mkdir(parents=True)
        dirs[sub] = str(root / "data" / sub)
    (root / STAMP).write_text(json.dumps({"label": label, "created": time.time()}))
    return {"name": name, "path": str(root), "dirs": dirs}


def list_all() -> dict:
    """Return every stamped scratch mod in FS2PATH with its label and creation time."""
    mods = []
    for d in sorted(paths.fs2path().glob(PREFIX + "*")):
        stamp = d / STAMP
        if _NAME.match(d.name) and stamp.is_file():
            info = json.loads(stamp.read_text())
            mods.append({"name": d.name, "label": info.get("label"),
                         "created": info.get("created")})
    return {"mods": mods}


def remove(name: str) -> dict:
    """Delete one scratch mod; refuse anything that is not a stamped harness scratch folder."""
    if not _NAME.match(name):
        raise HarnessError(EXIT_REFUSED, f"{name!r} is not a scratch mod name",
                           f"scratch mods are named {PREFIX}<6 hex>; see `fsoctl scratch_mod list`")
    root = paths.fs2path() / name
    if not (root / STAMP).is_file():
        raise HarnessError(EXIT_REFUSED, f"{name} has no {STAMP} stamp",
                           "only folders created by `fsoctl scratch_mod create` are deleted")
    shutil.rmtree(root)
    return {"removed": name}


def _cfg_scratch(p: argparse.ArgumentParser) -> None:
    """Add the `scratch_mod` verb's arguments."""
    p.add_argument("action", choices=("create", "list", "remove"))
    p.add_argument("name", nargs="?", help="scratch mod name (remove)")
    p.add_argument("--label", help="free text kept in the stamp (create)")


@register("scratch_mod", "create, list or remove throwaway mod folders", _cfg_scratch)
def _cli_scratch(args: argparse.Namespace) -> dict:
    """CLI wrapper: create / list / remove."""
    if args.action == "create":
        return create(args.label)
    if args.action == "list":
        return list_all()
    if not args.name:
        raise HarnessError(EXIT_REFUSED, "remove needs a name", "fsoctl scratch_mod remove <name>")
    return remove(args.name)
