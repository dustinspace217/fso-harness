"""Host-side file access inside a folder an engine writes, which the engine cannot redirect.

WHY: the engine writes into its prefs folder (the bridge channel, logs, pilots), and the harness
then reads and writes files there as the user. A path-based open follows whatever the engine
left at that name: a symbolic link sends a host write into any file the user can write (QA
2026-11-13 SA-1, probed: a planted cmd-N.tmp link made the harness overwrite a victim file), a
host read into a secret, a FIFO hangs the reader, a link to /dev/zero exhausts its memory
(SA-2). A sandboxed engine cannot reach the host's files itself; this module makes sure the
harness does not reach them on its behalf.

How: every access starts from a base folder the engine cannot replace (its prefs folder, which
for a sandbox is the mount point it cannot rename, or the run folder) and opens each path
component through the previous one's directory handle with O_NOFOLLOW, so no component, the last
one included, can be a link; the base itself is opened with O_NOFOLLOW too. Reads take only
regular files (fstat after an O_NONBLOCK open, so a FIFO is refused instead of waited on) and at
most a byte cap. Writes create a fresh temp file with O_EXCL and rename it through the folder's
handle. WHY not resolve-then-check (realpath, then open): the engine can swap a folder for a link
between the check and the open; holding a handle on each folder leaves no gap (the race test in
tests/test_safefs.py exchanges a channel folder and a link while 500 commands are sent).

Paths are base-relative strings ("fso_harness_bridge/data/cache/ready.json" under prefs). Every
failure is an OSError (Unsafe for a link, a non-regular file or an oversized one), which callers
already treat as "missing or unreadable".
"""
import contextlib
import errno
import io
import os
import stat
from collections.abc import Iterator
from pathlib import Path

READ_CAP = 16 << 20          # bytes: the largest file read whole (bridge replies are a few KiB)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_NEW_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_FIND_DEPTH = 6              # folder levels find() descends at most
_FIND_ENTRIES = 10000        # entries find() looks at in total (an engine can make many)
_USAGE_DEPTH = 64            # folder levels usage() descends at most
_LISTING_MAX = 200_000       # entries usage() reads from one folder (more trips its cap anyway)


class Unsafe(OSError):
    """A path component is a link, or the file is not a regular file, or it is too large."""


def _parts(rel: str) -> list[str]:
    """Split a run-folder-relative path; refuse absolute paths, "." and ".." (ValueError)."""
    parts = rel.split("/")
    if not rel or rel.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"not a plain relative path: {rel!r}")
    return parts


def _open_child_dir(parent: int, name: str, create: bool) -> int:
    """Open folder `name` under the folder handle `parent` without following a link.

    With create, a missing folder is made first; the folder is reopened with O_NOFOLLOW after
    the mkdir either way, because mkdir's "already exists" also answers for a planted link
    (Phase B refinement a)."""
    if create:
        with contextlib.suppress(FileExistsError):
            os.mkdir(name, 0o755, dir_fd=parent)
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise Unsafe(e.errno, f"{name} is not a plain folder") from None
        raise


@contextlib.contextmanager
def folder(run: Path, rel: str | None = None, create: bool = False) -> Iterator[int]:
    """Yield a handle on run/rel (the run folder itself when rel is None), walking from the run
    folder one component at a time with O_NOFOLLOW; with create, missing folders are made.
    The handle is closed when the `with` block ends."""
    fd = os.open(run, _DIR_FLAGS)
    try:
        for name in _parts(rel) if rel else []:
            child = _open_child_dir(fd, name, create)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _split(rel: str) -> tuple[str | None, str]:
    """Return (folder part or None, file name) of a run-folder-relative file path."""
    parts = _parts(rel)
    return ("/".join(parts[:-1]) or None), parts[-1]


def _open_regular(dfd: int, name: str) -> int:
    """Open `name` in folder handle `dfd` for reading if it is a regular file (not a link, FIFO,
    device or folder); return the descriptor. Raises FileNotFoundError or Unsafe."""
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dfd)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise Unsafe(e.errno, f"{name} is a link") from None
        raise
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise Unsafe(errno.EINVAL, f"{name} is not a regular file")
    return fd


def open_read(run: Path, rel: str) -> io.BufferedReader:
    """Return a binary reader on the regular file run/rel (any size: the caller bounds its own
    reads, e.g. a log tail). Raises FileNotFoundError or Unsafe."""
    folder_rel, name = _split(rel)
    with folder(run, folder_rel) as dfd:
        return os.fdopen(_open_regular(dfd, name), "rb")


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
        with folder(run, folder_rel) as dfd:
            return stat.S_ISREG(os.stat(name, dir_fd=dfd, follow_symlinks=False).st_mode)
    except (OSError, ValueError):
        return False


def write_atomic(run: Path, rel: str, data: bytes, create_dirs: bool = False) -> None:
    """Write `data` to run/rel through a fresh temp file (O_EXCL) renamed into place.

    A leftover temp name (a crash, or a planted link) is removed and the create retried once;
    unlink and rename act on the name itself and never follow a link."""
    folder_rel, name = _split(rel)
    tmp = name + ".tmp"
    with folder(run, folder_rel, create=create_dirs) as dfd:
        try:
            fd = os.open(tmp, _NEW_FLAGS, 0o644, dir_fd=dfd)
        except FileExistsError:
            os.unlink(tmp, dir_fd=dfd)
            fd = os.open(tmp, _NEW_FLAGS, 0o644, dir_fd=dfd)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)


def unlink(run: Path, rel: str) -> bool:
    """Remove run/rel (a link is removed, never followed); False if it was not there."""
    folder_rel, name = _split(rel)
    try:
        with folder(run, folder_rel) as dfd:
            os.unlink(name, dir_fd=dfd)
    except FileNotFoundError:
        return False
    return True


def rename(run: Path, rel: str, new_name: str) -> None:
    """Rename run/rel to `new_name` in the same folder (FileNotFoundError if it is gone)."""
    folder_rel, name = _split(rel)
    _parts(new_name)
    with folder(run, folder_rel) as dfd:
        os.rename(name, new_name, src_dir_fd=dfd, dst_dir_fd=dfd)


def names(run: Path, rel: str | None, limit: int = _FIND_ENTRIES) -> list[str]:
    """Return the entry names of the folder run/rel, sorted ([] if it is missing). Raises Unsafe
    past `limit` entries, counted while listing (QA CR2-4: an engine-made folder can hold
    millions; it is never read whole)."""
    out: list[str] = []
    try:
        with folder(run, rel) as dfd, os.scandir(dfd) as it:
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


def usage(base: Path, max_entries: int) -> tuple[int, int, bool]:
    """Return (bytes on disk, entries, vanished) of everything under the folder `base`, links
    counted but never followed. Bytes are allocated blocks (st_blocks * 512), what actually
    fills a disk. vanished is True when a folder listed by the walk was gone when the walk
    reached it: that walk undercounts, and the caller walks again (QA 3c; an engine can rename
    folders in a loop to dodge a walk; the watchdog's free-space-drop check is the backstop).
    One folder handle is open at a time: the stack holds relative paths, not descriptors.

    Fails closed (QA 3b, 2026-11-13): a folder the engine made unreadable (chmod 000, the
    prefs folder itself included) is made 0700 again and measured; if it still cannot be read,
    or any other error than "it vanished" happens, Unsafe is raised and the caller trips.
    Unsafe also once more than `max_entries` entries were seen (an engine can make millions of
    empty files; the walk must stay bounded) or a folder is deeper than _USAGE_DEPTH."""
    total, seen, vanished = 0, 0, False
    stack: list[tuple[str | None, int]] = [(None, 0)]
    while stack:                      # bounded: each pop lists one folder, entries are capped
        rel, depth = stack.pop()
        listing = _listing(base, rel)
        if listing is None:
            vanished = True
            continue
        entries, lost_folder = listing
        vanished = vanished or lost_folder
        for entry, st in entries:
            seen += 1
            if seen > max_entries:
                raise Unsafe(errno.EFBIG, f"more than {max_entries} entries")
            total += st.st_blocks * 512
            if stat.S_ISDIR(st.st_mode):
                if depth >= _USAGE_DEPTH:
                    raise Unsafe(errno.ELOOP, f"deeper than {_USAGE_DEPTH} folders")
                stack.append((entry if rel is None else f"{rel}/{entry}", depth + 1))
    return total, seen, vanished


def _listing(base: Path, rel: str | None
             ) -> tuple[list[tuple[str, os.stat_result]], bool] | None:
    """Return ([(name, lstat)], lost_folder) of the folder base/rel for usage() (see
    _stat_entries for lost_folder). A subfolder that vanished gives None; base itself missing
    raises FileNotFoundError; a folder that cannot be read is made
    0700 and read again (one retry); anything else, a folder that became a link included,
    raises Unsafe. At most _LISTING_MAX entries are read; usage() caps the total."""
    for attempt in range(2):          # bounded: one retry after restoring the folder's mode
        try:
            with folder(base, rel) as fd, os.scandir(fd) as it:
                return _stat_entries(it)
        except FileNotFoundError:
            if rel is None:
                raise                 # the measured folder itself is gone: the caller decides
            return None               # gone since it was listed: usage() reports the walk
        except PermissionError as e:
            if attempt:
                raise Unsafe(e.errno, f"cannot read {rel or '.'}: {e.strerror}") from None
            try:
                _make_private(base, rel)
            except OSError as err:
                raise Unsafe(err.errno or errno.EACCES,
                             f"cannot restore {rel or '.'}: {err.strerror}") from None
        except Unsafe:
            raise
        except OSError as e:
            raise Unsafe(e.errno or errno.EIO, f"cannot read {rel or '.'}: {e.strerror}") from None
    return [], False                  # not reached: the loop returns or raises


def _stat_entries(it: Iterator[os.DirEntry]) -> tuple[list[tuple[str, os.stat_result]], bool]:
    """Return ([(name, lstat)], lost_folder) for at most _LISTING_MAX entries of the scandir
    iterator `it`. An entry removed between the listing and its stat is left out (QA CR5-1:
    the bridge deletes each command file it has claimed, and 3b counted one such file as its
    whole folder vanishing; at the top of prefs that read as "prefs missing" and tripped).

    WHY files and folders differ: a deleted file's blocks are freed unless something holds it
    open, and an open one shows in the free-space-drop check, so skipping it hides nothing. A
    folder that vanished was most likely renamed and still holds its bytes somewhere this walk
    may not reach, so it sets lost_folder, which usage() reports as vanished (the watchdog
    walks again and trips after VANISHING_WALKS such walks in a row). The type comes from the
    listing itself (d_type, no stat); an unknown type counts as a folder."""
    entries, lost_folder = [], False
    for _, e in zip(range(_LISTING_MAX), it, strict=False):   # bounded by _LISTING_MAX
        try:
            entries.append((e.name, e.stat(follow_symlinks=False)))
        except FileNotFoundError:
            lost_folder = lost_folder or not _listed_as_file(e)
    return entries, lost_folder


def _listed_as_file(e: os.DirEntry) -> bool:
    """True when the listing named `e` a regular file or a link. is_file/is_symlink read the
    listing's d_type without a stat; when d_type is unknown they stat instead, which for a
    vanished entry answers False: the entry then counts as a folder (fail closed), as does
    anything else the listing named (FIFO, socket)."""
    try:
        return e.is_file(follow_symlinks=False) or e.is_symlink()
    except OSError:
        return False


def _make_private(base: Path, rel: str | None) -> None:
    """Make the folder base/rel mode 0700 again without following a link (base itself when rel
    is None)."""
    if rel is None:
        _chmod_private(None, str(base))
        return
    parent, name = _split(rel)
    with folder(base, parent) as pfd:
        _chmod_private(pfd, name)


_REMOVE_OPS = 10_000_000        # unlinks, rmdirs and renames remove_tree does at most per call
_REMOVE_BATCH = 1000            # entries listed per pass over one folder
_RENAME_TRIES = 16              # fresh names tried when moving an entry up collides


def _open_writable_dir(parent: int, name: str) -> int:
    """Make folder `name` (in folder handle `parent`) mode 0700 and open it; never follows a
    link. WHY the O_PATH handle: an engine can chmod a folder to 000 (QA AT2-2), which makes a
    plain open fail with EACCES; an O_PATH open needs no permission, and chmod through its
    /proc/self/fd entry changes that exact folder, not whatever the name points at later."""
    _chmod_private(parent, name)
    return os.open(name, _DIR_FLAGS, dir_fd=parent)


def _chmod_private(parent: int | None, name: str) -> None:
    """chmod 0700 the folder `name` in folder handle `parent` (or the absolute path `name` when
    parent is None) through an O_PATH handle: no permission needed, no link followed."""
    flags = os.O_PATH | os.O_NOFOLLOW | os.O_DIRECTORY | os.O_CLOEXEC
    path_fd = os.open(name, flags) if parent is None else os.open(name, flags, dir_fd=parent)
    try:
        os.chmod(f"/proc/self/fd/{path_fd}", 0o700)
    finally:
        os.close(path_fd)


def _spend(budget: list[int]) -> None:
    """Count one removal step against budget[0]; raise Unsafe once it is used up."""
    budget[0] -= 1
    if budget[0] < 0:
        raise Unsafe(errno.EFBIG, f"more than {_REMOVE_OPS} removal steps")


def _move_up(child: int, name: str, top: int, budget: list[int]) -> None:
    """Rename entry `name` of folder handle `child` into folder handle `top` under a fresh name
    (".fsoh-rm-N"), never following a link. A folder is made 0700 first: moving a folder needs
    write permission on it (its ".." changes). A collision with a name the engine chose tries
    the next N (bounded)."""
    if stat.S_ISDIR(os.stat(name, dir_fd=child, follow_symlinks=False).st_mode):
        _chmod_private(child, name)
    for _ in range(_RENAME_TRIES):    # bounded: a few fresh names, then give up loudly
        _spend(budget)
        target = f".fsoh-rm-{budget[0]}"   # the budget only falls, so each try is new
        try:
            os.rename(name, target, src_dir_fd=child, dst_dir_fd=top)
            return
        except OSError as e:
            if e.errno not in (errno.EEXIST, errno.ENOTEMPTY, errno.EISDIR, errno.ENOTDIR):
                raise
    raise Unsafe(errno.EEXIST, f"no free name to move {name} up")


def _empty(top: int, budget: list[int]) -> None:
    """Remove everything inside folder handle `top` (made 0700 by the caller), never following a
    link, whatever the depth (QA AT3-3: a chain of thousands of folders). Iterative and flat:
    files and links are unlinked, empty folders removed, and a non-empty folder's entries are
    moved up into `top` under fresh names, so the walk never goes more than one level down; a
    chain of D folders costs about 2D steps. Bounded by budget[0] steps in total."""
    while True:                       # bounded: every step is counted against the budget
        with os.scandir(top) as it:
            batch = [(e.name, e.is_dir(follow_symlinks=False))
                     for _, e in zip(range(_REMOVE_BATCH), it, strict=False)]
        if not batch:
            return
        for name, is_dir in batch:
            _spend(budget)
            if not is_dir:
                os.unlink(name, dir_fd=top)
                continue
            child = _open_writable_dir(top, name)
            try:
                with os.scandir(child) as it:
                    inside = [e.name for _, e in zip(range(_REMOVE_BATCH), it, strict=False)]
                for sub in inside:
                    _move_up(child, sub, top, budget)
            finally:
                os.close(child)
            if not inside:
                os.rmdir(name, dir_fd=top)


def remove_tree(parent: Path, name: str) -> None:
    """Remove the folder parent/name and everything in it, whatever modes and depth an engine
    left inside (folders are made 0700 before they are read, QA AT2-2/AT3-3), never following a
    link. `parent` is trusted (the runtime root or the sandbox root); a missing `name` is not
    an error."""
    pfd = os.open(parent, _DIR_FLAGS)
    try:
        top = _open_writable_dir(pfd, name)
    except FileNotFoundError:
        os.close(pfd)
        return
    try:
        _empty(top, [_REMOVE_OPS])
    finally:
        os.close(top)
    try:
        os.rmdir(name, dir_fd=pfd)
    finally:
        os.close(pfd)


def clear_tree(base: Path) -> None:
    """Remove everything inside the folder `base` and keep `base` itself (the watchdog empties a
    sandbox's prefs folder this way when it trips, QA AT3-2/SA3-2)."""
    _chmod_private(None, str(base))
    top = os.open(base, _DIR_FLAGS)
    try:
        _empty(top, [_REMOVE_OPS])
    finally:
        os.close(top)
