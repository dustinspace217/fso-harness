"""Host-side file access inside a folder an engine writes, on Windows: the twin of safefs.py
(same public functions and signatures; tests/test_platform_parity.py holds it to that).

What it guards against is the same as on Linux: the engine writes into its prefs folder, and
the harness then reads and writes files there as the user. A plain open follows whatever the
engine left at that name, so a link there would send a harness write into another file of the
user's, or a harness read into one.

How, and how far (plan B call 3): every access starts from a base folder and checks each path
component, the last one included, for a REPARSE POINT (symbolic links and junctions both are)
before using it; a component that is one is refused. Reads take regular files only, up to a
byte cap. Writes create a fresh temp file (O_EXCL) and move it into place.

WHY this is weaker than the Linux module, and said so in README Limits: Linux opens each
component THROUGH the previous one's folder handle with O_NOFOLLOW, so there is no moment
between "checked" and "used". Python on Windows has no handle-relative open, so the check and
the open are two steps and an engine could swap a path in between. Two things narrow that:
a file opened for reading is compared with the file that was checked (_open_checked), and
nothing here is ever written THROUGH an existing name (a new temp file, then a move, which
replaces a link instead of following it). It is not closed for folders. That is accepted
because a Windows session is not sandboxed: the engine runs as the user and can already write
anything the user can, so there is no boundary here for it to cross; what this module still
prevents is the harness being steered by leftovers and accidents.

The small path helpers and find() are the same as in safefs.py. They are repeated, not
imported, because safefs.py reads POSIX-only open flags when it loads and so cannot be
imported on Windows, and the Linux file is not to change for this (plan B call 1).

Paths are base-relative strings with "/" ("fso_harness_bridge/data/cache/ready.json"). Every
failure is an OSError (Unsafe for a link, a non-regular file or an oversized one), which
callers already treat as "missing or unreadable".
"""
import contextlib
import errno
import io
import os
import secrets
import stat
from collections.abc import Iterator
from pathlib import Path

READ_CAP = 16 << 20          # bytes: the largest file read whole (bridge replies are a few KiB)
_FIND_DEPTH = 6              # folder levels find() descends at most
_FIND_ENTRIES = 10000        # entries find() looks at in total (an engine can make many)
_REMOVE_OPS = 10_000_000     # entries remove_tree removes at most per call
_NEW_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)


class Unsafe(OSError):
    """A path component is a link, or the file is not a regular file, or it is too large."""


def _is_link(path: str | os.PathLike) -> bool:
    """True when `path` itself is a link: on Windows a reparse point (symbolic link or
    junction). Raises FileNotFoundError when nothing is there. Elsewhere (this module's tests
    run on every OS) the POSIX link bit answers the same question."""
    st = os.lstat(path)
    attributes = getattr(st, "st_file_attributes", None)
    if attributes is None:
        return stat.S_ISLNK(st.st_mode)
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _parts(rel: str) -> list[str]:
    """Split a base-relative path; refuse absolute paths, "." and ".." and, because Windows
    reads them as a separator, a drive and a named stream, any backslash or colon
    (ValueError)."""
    parts = rel.split("/")
    if (not rel or rel.startswith("/") or "\\" in rel or ":" in rel
            or any(p in ("", ".", "..") for p in parts)):
        raise ValueError(f"not a plain relative path: {rel!r}")
    return parts


def _split(rel: str) -> tuple[str | None, str]:
    """Return (folder part or None, file name) of a base-relative file path."""
    parts = _parts(rel)
    return ("/".join(parts[:-1]) or None), parts[-1]


def _plain_folder(path: Path, name: str) -> None:
    """Raise Unsafe unless `path` is a folder that is not a link (FileNotFoundError when it is
    missing). `name` is what the error calls it."""
    if _is_link(path) or not path.is_dir():
        raise Unsafe(errno.ENOTDIR, f"{name} is not a plain folder")


def _walk(run: Path, rel: str | None, create: bool) -> Path:
    """Return run/rel after checking the base and every component on the way: each must be a
    plain folder. With create, a missing folder is made first and then checked like any
    other, because "already exists" also answers for a planted link."""
    current = Path(run)
    _plain_folder(current, current.name)
    for name in _parts(rel) if rel else []:
        current = current / name
        if create:
            with contextlib.suppress(FileExistsError):
                os.mkdir(current)
        _plain_folder(current, name)
    return current


@contextlib.contextmanager
def folder(run: Path, rel: str | None = None, create: bool = False) -> Iterator[int]:
    """Check (and with create, make) the folder run/rel, walking from the run folder one
    component at a time; raise Unsafe if any is a link. Yields -1: Windows has no folder
    descriptors, and the harness's callers use this only to make or check the folder."""
    _walk(run, rel, create)
    yield -1


def _open_binary(path: Path) -> io.BufferedReader:
    """Open `path` for reading in binary (one place, so the check around it can be tested)."""
    return open(path, "rb")


def _open_checked(path: Path, name: str) -> io.BufferedReader:
    """Open the regular file `path` for reading, refusing a link or anything that is not a
    regular file, and refusing a file that was swapped between the check and the open: the
    file that was opened must be the one that was checked (same volume and file number)."""
    if _is_link(path):
        raise Unsafe(errno.ELOOP, f"{name} is a link")
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise Unsafe(errno.EINVAL, f"{name} is not a regular file")
    f = _open_binary(path)
    after = os.fstat(f.fileno())
    if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
        f.close()
        raise Unsafe(errno.ELOOP, f"{name} changed while it was being opened")
    return f


def open_read(run: Path, rel: str) -> io.BufferedReader:
    """Return a binary reader on the regular file run/rel (any size: the caller bounds its own
    reads, e.g. a log tail). Raises FileNotFoundError or Unsafe."""
    folder_rel, name = _split(rel)
    return _open_checked(_walk(run, folder_rel, False) / name, name)


def read(run: Path, rel: str, cap: int = READ_CAP) -> tuple[bytes, os.stat_result]:
    """Return (contents, stat) of the regular file run/rel; Unsafe if it is larger than `cap`.

    The stat comes from the open file, so contents and modification time belong together."""
    with open_read(run, rel) as f:
        st = os.fstat(f.fileno())
        if st.st_size > cap:
            raise Unsafe(errno.EFBIG, f"{rel} is larger than {cap} bytes")
        data = f.read(cap + 1)
    if len(data) > cap:              # grew between the stat and the read
        raise Unsafe(errno.EFBIG, f"{rel} is larger than {cap} bytes")
    return data, st


def read_tail(run: Path, rel: str, nbytes: int) -> tuple[bytes, int]:
    """Return (the last `nbytes` bytes, the file's full size) of the regular file run/rel."""
    with open_read(run, rel) as f:
        size = os.fstat(f.fileno()).st_size
        f.seek(max(0, size - nbytes))
        return f.read(nbytes), size


def is_file(run: Path, rel: str) -> bool:
    """True if run/rel is a regular file reached without links (False for anything else)."""
    try:
        folder_rel, name = _split(rel)
        path = _walk(run, folder_rel, False) / name
        return not _is_link(path) and stat.S_ISREG(os.lstat(path).st_mode)
    except (OSError, ValueError):
        return False


def write_atomic(run: Path, rel: str, data: bytes, create_dirs: bool = False) -> None:
    """Write `data` to run/rel through a fresh temp file (O_EXCL) moved into place, so a
    reader sees the old file or the new one, never a part.

    The temp file's name has 16 random hex digits in it. WHY (QA Plan B, SA-4; measured on
    real Windows, 2026-11-17, tests/test_platform_safefs.py): an exclusive create there
    FOLLOWS a link whose target does not exist and creates the target. With a fixed temp
    name an engine could plant such a link beforehand and have the harness's bytes written
    to a place it chose; a name it cannot know cannot be planted. The create is still
    exclusive, and the move acts on the name itself and never follows a link. A temp file
    whose write or move fails is removed again (its name would never be reused); one left by
    a command that was ended mid-write stays until the run folder is removed.
    """
    folder_rel, name = _split(rel)
    where = _walk(run, folder_rel, create_dirs)
    tmp = where / f"{name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp, _NEW_FLAGS, 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, where / name)
    except OSError:
        with contextlib.suppress(OSError):     # the first error is the one to report
            os.unlink(tmp)
        raise


def unlink(run: Path, rel: str) -> bool:
    """Remove run/rel (a link is removed, never followed); False if it was not there."""
    folder_rel, name = _split(rel)
    try:
        os.unlink(_walk(run, folder_rel, False) / name)
    except FileNotFoundError:
        return False
    return True


def rename(run: Path, rel: str, new_name: str) -> None:
    """Rename run/rel to `new_name` in the same folder (FileNotFoundError if it is gone).
    os.replace, not os.rename: on Windows os.rename refuses an existing destination, where
    the Linux module's rename replaces it."""
    folder_rel, name = _split(rel)
    _parts(new_name)
    where = _walk(run, folder_rel, False)
    os.replace(where / name, where / new_name)


def names(run: Path, rel: str | None, limit: int = _FIND_ENTRIES) -> list[str]:
    """Return the entry names of the folder run/rel, sorted ([] if it is missing). Raises Unsafe
    past `limit` entries, counted while listing (an engine-made folder can hold millions; it
    is never read whole)."""
    out: list[str] = []
    try:
        with os.scandir(_walk(run, rel, False)) as it:
            for entry in it:
                if len(out) >= limit:
                    raise Unsafe(errno.EFBIG, f"more than {limit} entries in {rel}")
                out.append(entry.name)
    except FileNotFoundError:
        return []
    return sorted(out)


def find(run: Path, rel: str | None, name: str) -> str | None:
    """Return the run-relative path of the first regular file called `name` under run/rel (run
    itself when rel is None; breadth first, sorted, links never entered, at most _FIND_DEPTH
    levels and _FIND_ENTRIES entries), or None."""
    level, seen = [rel], 0
    for _ in range(_FIND_DEPTH):
        below = []
        for d in level:
            try:
                entries = names(run, d)
            except OSError:
                continue                # became a link since it was listed: skip it
            for entry in entries:
                seen += 1
                if seen > _FIND_ENTRIES:
                    return None
                path = entry if d is None else f"{d}/{entry}"
                if entry == name and is_file(run, path):
                    return path
                with contextlib.suppress(OSError, ValueError), folder(run, path):
                    below.append(path)
        level = below
    return None


def _spend(budget: list[int]) -> None:
    """Count one removal against budget[0]; raise Unsafe once it is used up."""
    budget[0] -= 1
    if budget[0] < 0:
        raise Unsafe(errno.EFBIG, f"more than {_REMOVE_OPS} removal steps")


def _remove_entry(path: str, is_folder: bool) -> None:
    """Remove one file or link (is_folder False), or one EMPTY folder (True). A link is
    always removed with unlink, whatever it points at: Python's unlink removes folder links
    and junctions too, and never what they point at. Windows refuses to delete a read-only
    file; an engine could leave those to make clean-up fail, so the flag is cleared and the
    removal tried once more."""
    remove = os.rmdir if is_folder else os.unlink
    try:
        remove(path)
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)
        remove(path)


def _empty(top: Path, budget: list[int]) -> None:
    """Remove everything inside the folder `top`, never entering a link (a link is removed as
    an entry; what it points at is left alone). Not recursive: folders still to be emptied
    wait on a stack, and a folder is removed on the second visit, once it is empty."""
    stack: list[tuple[str, bool]] = [(str(top), False)]
    while stack:                      # bounded by the removal budget
        where, emptied = stack.pop()
        if emptied:
            if where != str(top):
                _spend(budget)
                _remove_entry(where, True)
            continue
        stack.append((where, True))
        with os.scandir(where) as it:
            entries = [(e.path, e.is_dir(follow_symlinks=False)) for e in it]
        for path, is_dir in entries:
            link = _is_link(path)
            if is_dir and not link:
                stack.append((path, False))
            else:
                _spend(budget)
                _remove_entry(path, False)


def remove_tree(parent: Path, name: str) -> None:
    """Remove the folder parent/name and everything in it, never following a link. `parent`
    is trusted (the runtime root or the sandbox root); a missing `name` is not an error; a
    `name` that is itself a link is removed as a link."""
    _parts(name)
    top = Path(parent) / name
    try:
        link = _is_link(top)
    except FileNotFoundError:
        return
    if link:
        _remove_entry(str(top), False)
        return
    _empty(top, [_REMOVE_OPS])
    _remove_entry(str(top), True)


