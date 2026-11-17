"""File locks between fsoctl processes on Linux: flock (moved here from session.py, Plan B).

The kernel drops a flock when the holder's file closes or the holder dies, so a crashed fsoctl
never leaves a lock held. win_locks.py is the Windows twin (same three functions).
"""
import fcntl
from typing import IO


def lock_exclusive(f: IO) -> bool:
    """Try to take the exclusive lock on open file f without waiting; True when taken."""
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def lock_wait(f: IO) -> None:
    """Take the exclusive lock on open file f, waiting for the holder to let go. Used for the
    short read-change-write sections (session.json, the sequence number), where the holder is
    another fsoctl in the middle of a few file operations."""
    fcntl.flock(f, fcntl.LOCK_EX)


def unlock(f: IO) -> None:
    """Release the lock on open file f."""
    fcntl.flock(f, fcntl.LOCK_UN)
