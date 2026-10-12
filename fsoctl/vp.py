"""Volition VP archives (FreeSpace data packs): list, extract, and read single files.

Format (little-endian), cross-checked against an independent extractor:
  header:    "VPVP", int32 version, int32 directory offset, int32 entry count
  directory: entry count x 44 bytes: int32 offset, int32 size, char[32] name (NUL padded),
             int32 timestamp. A size-0 entry opens a folder; a size-0 ".." closes it.

Archives are untrusted input: every entry must lie inside the file (no negative sizes or
offsets: a background security review found a negative size slipping under the extraction cap,
and read(-1) reads to the end of the file), entry names are never joined into a destination path
without a check that the result stays inside it, and reads and extraction have size caps.
"""
import argparse
import fnmatch
import os
import struct
from pathlib import Path

from .cli import register
from .errors import EXIT_NOT_FOUND, EXIT_REFUSED, EXIT_USAGE, HarnessError

_HEADER = struct.Struct("<4siii")
_ENTRY = struct.Struct("<ii32si")
MAX_ENTRIES = 1_000_000
MAX_EXTRACT_BYTES = 2 * 1024 ** 3
MAX_READ_BYTES = 64 * 1024 ** 2   # one file read into memory (missions are kilobytes)
COPY_CHUNK = 1024 ** 2            # extraction streams entries in 1 MiB pieces


def _bad(path: Path, why: str) -> HarnessError:
    """Return the error for a file that is not a readable VP archive."""
    return HarnessError(EXIT_USAGE, f"{path}: not a VP archive ({why})", "check the file path")


def read_index(path: Path) -> list[dict]:
    """Return every file entry as {"path", "size", "offset", "timestamp"} (folders folded in)."""
    path = Path(path)
    with open(path, "rb") as f:
        file_size = os.fstat(f.fileno()).st_size
        head = f.read(_HEADER.size)
        if len(head) < _HEADER.size:
            raise _bad(path, "short header")
        magic, _version, diroff, count = _HEADER.unpack(head)
        if magic != b"VPVP" or not 0 <= count <= MAX_ENTRIES:
            raise _bad(path, "bad magic or entry count")
        # A negative offset would make seek() raise a bare OSError (cross-family XK-3).
        if not _HEADER.size <= diroff <= file_size:
            raise _bad(path, f"directory offset {diroff} lies outside the file")
        f.seek(diroff)
        raw = f.read(count * _ENTRY.size)
    if len(raw) < count * _ENTRY.size:
        raise _bad(path, "directory cut short")
    stack: list[str] = []
    entries = []
    for i in range(count):
        off, size, name_raw, ts = _ENTRY.unpack_from(raw, i * _ENTRY.size)
        name = name_raw.split(b"\0", 1)[0].decode("latin-1")
        if size == 0:
            if name == "..":
                if stack:
                    stack.pop()
            else:
                stack.append(name)
            continue
        if size < 0 or off < 0 or off + size > file_size:
            raise _bad(path, f"entry {name!r} lies outside the file")
        entries.append({"path": "/".join([*stack, name]), "size": size, "offset": off,
                        "timestamp": ts})
    return entries


def list_files(path: str, pattern: str = "*") -> dict:
    """Return {"files": [entries whose path matches the glob, case-insensitive]}."""
    pat = pattern.lower()
    return {"files": [e for e in read_index(Path(path)) if fnmatch.fnmatch(e["path"].lower(), pat)]}


def read_file(archive: Path, inner: str, max_bytes: int = MAX_READ_BYTES) -> bytes:
    """Return the bytes of one file inside an archive (path compared case-insensitively)."""
    want = inner.lower().replace("\\", "/")
    for e in read_index(Path(archive)):
        if e["path"].lower() == want:
            if e["size"] > max_bytes:
                raise HarnessError(EXIT_REFUSED, f"{inner} is {e['size']} bytes (cap {max_bytes})",
                                   "extract it to disk with `fsoctl vp extract` instead")
            with open(archive, "rb") as f:
                f.seek(e["offset"])
                return f.read(e["size"])
    raise HarnessError(EXIT_NOT_FOUND, f"{inner} is not in {archive}", "list it: fsoctl vp list")


def _safe_target(dest: Path, inner: str) -> Path:
    """Return dest/inner, refusing absolute names and anything that resolves outside dest."""
    target = (dest / inner).resolve()
    if inner.startswith("/") or not target.is_relative_to(dest.resolve()):
        raise HarnessError(EXIT_REFUSED, f"archive entry {inner!r} would land outside {dest}",
                           "the archive is damaged or hostile; nothing was written for it")
    return target


def _copy_entry(src, out_path: Path, offset: int, size: int) -> None:
    """Copy `size` bytes starting at `offset` of the open archive `src` into out_path.

    WHY chunks: an entry may be up to the 2 GiB extraction cap; reading it whole would hold all of
    it in memory at once. The loop is bounded by size / COPY_CHUNK reads.
    """
    src.seek(offset)
    left = size
    with open(out_path, "wb") as dst:
        while left > 0:
            chunk = src.read(min(COPY_CHUNK, left))
            if not chunk:   # the archive shrank under us since the index was read
                raise HarnessError(EXIT_USAGE, f"{out_path.name}: archive ended early",
                                   "the archive changed while extracting; run it again")
            dst.write(chunk)
            left -= len(chunk)


def extract(path: str, pattern: str, dest: str, overwrite: bool = False) -> dict:
    """Extract matching files under dest, keeping their folder paths.

    Returns {"files", "dest", "overwritten"}. Existing files are refused unless overwrite is set:
    dest can be any folder (even a mod's data folder), and a silent overwrite would replace a
    user's own same-named mission with the archive's copy.
    """
    arc, out = Path(path), Path(dest)
    chosen = list_files(path, pattern)["files"]
    targets = [(e, _safe_target(out, e["path"])) for e in chosen]  # check all before writing
    if sum(e["size"] for e in chosen) > MAX_EXTRACT_BYTES:
        raise HarnessError(EXIT_REFUSED, "more than 2 GiB would be extracted",
                           "narrow the pattern")
    existing = [e["path"] for e, target in targets if target.exists()]
    if existing and not overwrite:
        raise HarnessError(EXIT_REFUSED, f"{len(existing)} file(s) already exist under {out}, "
                           f"e.g. {existing[:3]}",
                           "pass --overwrite to replace them, or extract into an empty folder")
    with open(arc, "rb") as f:
        for e, target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_entry(f, target, e["offset"], e["size"])
    return {"files": [e["path"] for e in chosen], "dest": str(out), "overwritten": existing}


def _cfg_vp(p: argparse.ArgumentParser) -> None:
    """Add the `vp` verb's arguments."""
    p.add_argument("action", choices=("list", "extract"))
    p.add_argument("archive")
    p.add_argument("pattern", nargs="?", default="*", help="glob on the path inside the archive")
    p.add_argument("dest", nargs="?", help="destination folder (extract)")
    p.add_argument("--overwrite", action="store_true",
                   help="extract: replace files that already exist under dest")


@register("vp", "list or extract files in a .vp archive", _cfg_vp)
def _cli_vp(args: argparse.Namespace) -> dict:
    """CLI wrapper: list or extract."""
    if args.action == "list":
        return list_files(args.archive, args.pattern)
    if not args.dest:
        raise HarnessError(EXIT_USAGE, "extract needs a destination",
                           "vp extract ARCHIVE PATTERN DEST")
    return extract(args.archive, args.pattern, args.dest, args.overwrite)
