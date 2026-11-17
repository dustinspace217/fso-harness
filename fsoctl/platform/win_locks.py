"""File locks between fsoctl processes on Windows: msvcrt.locking on the file's first byte.

The twin of linux_locks.py. Windows has no flock; msvcrt.locking locks a byte range of an open
file, and Windows releases it when the handle closes or the holder dies, which is the property
the harness relies on (a crashed fsoctl must not leave a lock held).

WHY byte 0 and a seek first: locking acts at the file position. The lock files are opened in
append mode, where the position starts at the end, so two processes at different lengths would
lock different bytes and both "hold" the lock. One byte at offset 0 is the same byte for
everyone; Windows allows locking a byte past the end of an empty file.

msvcrt is imported inside the functions: this module must import on Linux too (the parity test
loads it there), and msvcrt exists only on Windows.
"""
import errno
from typing import IO

# lock_wait: msvcrt's own waiting mode retries once a second for about ten seconds and then
# raises. This many rounds of that (about five minutes) before the error is let through.
WAIT_TRIES = 30

# What msvcrt.locking raises for bytes another handle has locked: "permission denied" at
# once in the no-wait mode (tests/winlive/test_procs_live.py holds real Windows to that),
# and "deadlock" from the waiting mode after its ten tries (by the C runtime's documentation
# of _locking; not run here, and lock_exclusive does not use that mode).
_HELD = (errno.EACCES, errno.EDEADLK)


def lock_exclusive(f: IO) -> bool:
    """Try to take the exclusive lock on open file f without waiting; True when taken."""
    import msvcrt  # noqa: PLC0415 - Windows-only module
    f.seek(0)
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as e:
        # Only "it is held" means not taken; any other error is a fault and is raised, as
        # the Linux twin does (it catches "would block" alone).
        if e.errno not in _HELD:
            raise
        return False
    return True


def lock_wait(f: IO) -> None:
    """Take the exclusive lock on open file f, waiting for the holder to let go; OSError if it
    is still held after WAIT_TRIES rounds (see above). Linux waits without a limit here; the
    holders are fsoctl processes doing a few file operations, so the limit is never near."""
    import msvcrt  # noqa: PLC0415 - Windows-only module
    for attempt in range(WAIT_TRIES):             # bounded
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            return
        except OSError:
            if attempt == WAIT_TRIES - 1:
                raise


def unlock(f: IO) -> None:
    """Release the lock on open file f."""
    import msvcrt  # noqa: PLC0415 - Windows-only module
    f.seek(0)
    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
