"""Command channel to the in-engine bridge: numbered files in the session's cache folder.

WHY files: FSO's Lua can't open sockets and io.open is restricted to FIFOs; a FIFO read would
block the game frame. The bridge polls with the engine's cf API each frame (On Frame and On Dialog
Frame), so commands also work while a popup is up. Sequence numbers come from session.next_seq
(flock), so concurrent callers never share a reply file.
"""
import errno
import json
import time
from collections.abc import Callable
from typing import Any

from . import paths, platform, record, session
from .errors import EXIT_BRIDGE, EXIT_ENGINE, EXIT_REFUSED, EXIT_TIMEOUT, HarnessError
from .platform import safefs

_POLL_SEC = 0.05
STALE_SEC = 5.0     # heartbeat older than this = stale (contract C3)
# Windows only: how often, and how far apart, the heartbeat is read again when it is missing
# or busy (see Channel._read_heartbeat). 5 tries, 10 ms apart: several frames' worth.
_REPLACE_TRIES = 5
_REPLACE_GAP_SEC = 0.01
# Every channel file access goes through safefs from the run folder, because the engine writes
# this folder and could leave links or FIFOs in it (QA 2026-11-13 SA-1/SA-2).
REL = paths.CHANNEL_REL
# What json.loads raises on text the engine wrote: ValueError for bad JSON, and RecursionError
# for deep nesting (probed 2026-11-13 on Python 3.14: 100,000 "[" raised RecursionError, which is
# not a ValueError; QA CR2-1/AT2-3). Every parse of engine-written JSON catches both.
BAD_JSON = (ValueError, RecursionError)


class Channel:
    """Send commands to one session's bridge and read its heartbeat."""

    def __init__(self, sess: session.Session) -> None:
        self.sess = sess
        self.dir = paths.channel_dir(session.prefs_dir(sess))   # for display and tests only
        self.base = session.prefs_dir(sess)   # safefs walks from here (prefs-relative paths)

    def heartbeat(self) -> dict | None:
        """Return the bridge's latest ready.json plus `age_sec`, or None if absent or mid-write.

        age_sec = now minus the file's modification time (contract C3). The bridge rewrites the
        file every frame, so an old file means a hung or busy engine (spec section 6); callers
        treat age_sec > STALE_SEC as stale. The time comes from the same open file that was read,
        so a rewrite between the read and the stat cannot pair new contents with an old time.
        """
        try:
            data, st = self._read_heartbeat()
            hb, mtime = json.loads(data), st.st_mtime
        except (OSError, *BAD_JSON):
            return None
        if not isinstance(hb, dict):
            return None
        return {**hb, "age_sec": round(max(0.0, time.time() - mtime), 2)}

    def _read_heartbeat(self):
        """Read ready.json: (contents, stat), or the OSError that says why not.

        On Linux the bridge replaces the file with one rename, so it is never missing between
        two writes and a missing file means "no heartbeat yet". On Windows rename cannot
        replace a file, so the bridge deletes the old one and then renames (fsoharness.lua
        write_atomic); a read that lands in that gap finds the file missing or busy, or
        finds a different file than the one it had just checked (safefs.Unsafe). There, and
        only there, the read is tried again a few times before the answer is "none"."""
        tries = _REPLACE_TRIES if platform.BUSY_FILES_REFUSE else 1
        for attempt in range(tries):              # bounded
            try:
                return safefs.read(self.base, f"{REL}/ready.json")
            except (FileNotFoundError, PermissionError, safefs.Unsafe):
                if attempt + 1 == tries:
                    raise
                time.sleep(_REPLACE_GAP_SEC)
        raise FileNotFoundError(errno.ENOENT, "ready.json")   # not reached; keeps the result typed

    def popups(self) -> list:
        """Return the popups the bridge answered (popups.txt, one JSON object per line); [] when
        there is no such plain file. Lines that are not JSON are skipped: the engine wrote them,
        and a bad line must not fail the crash report or validation that reads this."""
        try:
            data, _ = safefs.read(self.base, f"{REL}/popups.txt")
        except OSError:
            return []
        out = []
        for line in data.decode(errors="replace").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except BAD_JSON:
                    continue
        return out

    def describe_heartbeat(self) -> str:
        """Return "bridge state S, heartbeat N s old[, stale]" for error messages (QA SL-10)."""
        hb = self.heartbeat()
        if hb is None:
            return "no bridge heartbeat"
        stale = ", stale: the engine is busy or hung" if hb["age_sec"] > STALE_SEC else ""
        state = hb.get("state", "unknown")
        return f"bridge state {state}, heartbeat {hb['age_sec']:.0f}s old{stale}"

    def wait_ready(self, timeout: float, alive: Callable[[], bool]) -> dict:
        """Block until the first heartbeat (bounded); raise if the engine dies or time runs out."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            hb = self.heartbeat()
            if hb:
                return hb
            if not alive():
                raise HarnessError(EXIT_ENGINE, "engine exited before the bridge started",
                                   "run `fsoctl crash_report <id>`")
            time.sleep(0.25)
        raise HarnessError(EXIT_TIMEOUT, f"no bridge heartbeat after {timeout:.0f}s",
                           "the engine may be stuck loading or on a popup; take a screenshot")

    def send(self, cmd: str, args: dict | None = None, timeout: float = 10.0,
             alive: Callable[[], bool] | None = None) -> Any:
        """Send one command and return its result; raise HarnessError on refusal/timeout/death.

        Commands that change something are journaled once the outcome is known (QA SL-11), with
        the game state and seq from when they were issued; record.UNJOURNALED commands are not.
        A timeout's error carries details {"seq", "fate"}: fate "withdrawn" (it did not run) or
        "taken_unknown" (the bridge took it; it may still be running).
        """
        try:
            with safefs.folder(self.base, REL, create=True):
                pass
        except OSError as e:
            raise _unusable(e) from None
        seq = session.next_seq(self.sess)
        journal = (None if cmd in record.UNJOURNALED
                   else record.entry(self.sess, cmd, args or {}, seq=seq))
        try:
            result = self._exchange(seq, cmd, args or {}, timeout, alive)
        except HarnessError as e:
            if journal is not None:
                record.write(self.sess, journal, record.outcome_of(e))
            raise
        if journal is not None:
            record.write(self.sess, journal, "ok")
        return result

    def _exchange(self, seq: int, cmd: str, args: dict, timeout: float,
                  alive: Callable[[], bool] | None) -> Any:
        """Write command `seq`, then poll (bounded by `timeout`) for its reply file. A channel
        file or folder the engine replaced with a link or a non-file raises EXIT_REFUSED."""
        try:
            return self._exchange_files(seq, cmd, args, timeout, alive)
        except OSError as e:
            raise _unusable(e) from None

    def _exchange_files(self, seq: int, cmd: str, args: dict, timeout: float,
                        alive: Callable[[], bool] | None) -> Any:
        """The body of _exchange; file errors propagate as OSError."""
        cmd_rel = f"{REL}/cmd-{seq:06d}.json"
        res_rel = f"{REL}/res-{seq:06d}.json"
        safefs.write_atomic(self.base, cmd_rel,
                            json.dumps({"seq": seq, "cmd": cmd, "args": args}).encode())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, _ = safefs.read(self.base, res_rel)
            except FileNotFoundError:
                data = None
            except PermissionError:
                # Where a file another program has open refuses (Windows): the reply is
                # there and busy. Look again; the command was sent and may have run, so
                # this must end as its result or as a timeout that says so, never as
                # "try the command again" (cross-family review XFA-3).
                if not platform.BUSY_FILES_REFUSE:
                    raise
                data = None
            if data is not None:
                try:
                    body = json.loads(data)
                except BAD_JSON as e:
                    raise HarnessError(EXIT_BRIDGE, f"unreadable reply to {cmd}: {e}",
                                       "the bridge wrote a reply that is not JSON") from None
                self._drop(res_rel)
                if not isinstance(body, dict):
                    raise HarnessError(EXIT_BRIDGE, f"unreadable reply to {cmd}",
                                       "the bridge wrote a reply that is not a JSON object")
                if body.get("ok"):
                    return body.get("result")
                # Contract C2: "script_error" is a Lua error (the harness or the user's script
                # broke); "refused", or no kind from an older bridge, is a deliberate refusal.
                code = EXIT_BRIDGE if body.get("kind") == "script_error" else EXIT_REFUSED
                raise HarnessError(code, body.get("error", "refused"), body.get("hint", ""))
            if alive is not None and not alive():
                self._drop(cmd_rel)
                raise HarnessError(EXIT_ENGINE, f"engine is not running ({cmd})",
                                   "run `fsoctl crash_report <id>`")
            time.sleep(_POLL_SEC)
        raise self._timeout_error(seq, cmd, cmd_rel, timeout)

    def _drop(self, rel: str) -> None:
        """Delete a channel file whose part is over: a reply that has been read, a command
        nobody will take, a withdrawn command. Where a busy file refuses (Windows) a
        refusal is left alone: what the caller is owed (the result, "the engine is not
        running", "withdrawn, it did not run") is already known and must reach it; the
        file's number is never used again, and the folder goes with the session
        (round-2 TAF-6 for the last two)."""
        try:
            safefs.unlink(self.base, rel)
        except PermissionError:
            if not platform.BUSY_FILES_REFUSE:
                raise

    def reap_reply(self, seq: int, within: float = 2.0) -> bool:
        """Delete command `seq`'s late reply file, waiting up to `within` seconds for it.

        Returns True if a reply was found and removed. WHY (QA SL2-8, Y-12): a timed-out command
        that the bridge took still answers when it finishes (a cancelled pilot does at once), and
        nobody reads that file; repeated timeouts would pile them up in the channel folder.
        """
        res_rel = f"{REL}/res-{seq:06d}.json"
        deadline = time.monotonic() + within
        while True:   # bounded: returns at the deadline
            try:
                if safefs.unlink(self.base, res_rel):
                    return True
            except OSError:
                return False             # the channel folder is not usable: nothing to reap
            if time.monotonic() >= deadline:
                return False
            time.sleep(_POLL_SEC)

    def _timeout_error(self, seq: int, cmd: str, cmd_rel: str, timeout: float) -> HarnessError:
        """Withdraw an unanswered command if the bridge has not taken it; return the error to raise.

        WHY a rename and not an unlink (QA SL-6): the bridge deletes a command file as it takes
        it, and deferred commands (pilot) keep running after that with no reply yet. The rename
        fails with FileNotFoundError exactly when the bridge already deleted the file, so the two
        outcomes can be told apart and reported differently. A ".withdrawn" name no longer matches
        the bridge's ^cmd-N.json$ pattern. The bridge claims a command by renaming it before it
        reads it (fsoharness.lua run_command), so exactly one of the two renames wins: when this
        one succeeds, the bridge never read the command.
        """
        withdrawn = cmd_rel.rsplit("/", 1)[1] + ".withdrawn"
        try:
            safefs.rename(self.base, cmd_rel, withdrawn)
        except FileNotFoundError:
            fate, said = "taken_unknown", "the bridge took the command; outcome unknown"
            hint = "do not retry a non-idempotent command blindly; check state"
        except PermissionError:
            # Windows: someone has the command file open, most likely the bridge taking it.
            if not platform.BUSY_FILES_REFUSE:
                raise
            fate, said = "taken_unknown", ("the command file was in use (the bridge may "
                                           "have been taking it); outcome unknown")
            hint = "do not retry a non-idempotent command blindly; check state"
        else:
            self._drop(f"{REL}/{withdrawn}")
            fate, said = "withdrawn", "withdrawn; it did not run"
            hint = "take a screenshot; a popup or a loading screen may be blocking"
        return HarnessError(EXIT_TIMEOUT, f"no reply to {cmd} in {timeout:.0f}s: {said} "
                                          f"({self.describe_heartbeat()})", hint,
                            details={"seq": seq, "fate": fate})


# details["kind"] of the error a tampered channel gives (QA SA2-2). The nightly review holds a PR
# on it instead of retrying: a retry meets the same planted link.
TAMPERED = "channel_tampered"


def _unusable(err: OSError) -> HarnessError:
    """Return the error for a channel file or folder the harness will not touch (EXIT_REFUSED).

    safefs.Unsafe (a link, a FIFO or another non-file where a channel file or folder should be)
    means the engine tampered with its channel: message "channel tampered: ...", details
    {"kind": TAMPERED}, and a hint that says not to retry. Any other OSError (a folder that
    vanished, a full disk) keeps the plain "unusable" message."""
    # EACCES/EPERM count as tampering too (QA AT3-4): the harness made every channel folder
    # and file itself, so only the engine can have taken the permission away (chmod 000).
    # Not on Windows (QA Plan B, ST-8): there the same error is what a file says while
    # another program has it open, and a session is not confined in any case, so the
    # inference has no force; such an error gets the plain message and a hint to try again.
    denied = not platform.BUSY_FILES_REFUSE and err.errno in (errno.EACCES, errno.EPERM)
    if isinstance(err, safefs.Unsafe) or denied:
        return HarnessError(EXIT_REFUSED, f"channel tampered: {err.strerror or err}",
                            "the engine left a link, a FIFO or another non-file where the "
                            "harness reads or writes its channel files; do not retry, stop the "
                            "session (`fsoctl stop <id>`) and treat the build as suspect",
                            details={"kind": TAMPERED})
    if platform.BUSY_FILES_REFUSE and isinstance(err, PermissionError):
        return HarnessError(EXIT_REFUSED, f"the session's command channel is unusable: {err}",
                            "another program may have had a channel file open for a moment. "
                            "If the command changes something, check the game's state before "
                            "sending it again; stop the session (`fsoctl stop <id>`) if it "
                            "keeps happening")
    return HarnessError(EXIT_REFUSED, f"the session's command channel is unusable: {err}",
                        "stop the session (`fsoctl stop <id>`)")
