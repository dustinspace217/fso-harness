"""Command channel to the in-engine bridge: numbered files in the session's cache folder.

WHY files: FSO's Lua can't open sockets and io.open is restricted to FIFOs; a FIFO read would
block the game frame. The bridge polls with the engine's cf API each frame (On Frame and On Dialog
Frame), so commands also work while a popup is up. Sequence numbers come from session.next_seq
(flock), so concurrent callers never share a reply file.
"""
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import paths, record, session
from .errors import EXIT_BRIDGE, EXIT_ENGINE, EXIT_REFUSED, EXIT_TIMEOUT, HarnessError

_POLL_SEC = 0.05
STALE_SEC = 5.0     # heartbeat older than this = stale (contract C3)


class Channel:
    """Send commands to one session's bridge and read its heartbeat."""

    def __init__(self, sess: session.Session) -> None:
        self.sess = sess
        self.dir = paths.channel_dir(session.prefs_dir(sess))

    def heartbeat(self) -> dict | None:
        """Return the bridge's latest ready.json plus `age_sec`, or None if absent or mid-write.

        age_sec = now minus the file's modification time (contract C3). The bridge rewrites the
        file every frame, so an old file means a hung or busy engine (spec section 6); callers
        treat age_sec > STALE_SEC as stale. The time comes from the same open file that was read,
        so a rewrite between the read and the stat cannot pair new contents with an old time.
        """
        try:
            with open(self.dir / "ready.json", "rb") as f:
                mtime = os.fstat(f.fileno()).st_mtime
                hb = json.loads(f.read())
        except (OSError, ValueError):
            return None
        if not isinstance(hb, dict):
            return None
        return {**hb, "age_sec": round(max(0.0, time.time() - mtime), 2)}

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
        self.dir.mkdir(parents=True, exist_ok=True)
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
        """Write command `seq`, then poll (bounded by `timeout`) for its reply file."""
        cmd_path = self.dir / f"cmd-{seq:06d}.json"
        res_path = self.dir / f"res-{seq:06d}.json"
        tmp = self.dir / f"cmd-{seq:06d}.tmp"
        tmp.write_text(json.dumps({"seq": seq, "cmd": cmd, "args": args}))
        os.replace(tmp, cmd_path)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if res_path.exists():
                body = json.loads(res_path.read_text())
                res_path.unlink()
                if body.get("ok"):
                    return body.get("result")
                # Contract C2: "script_error" is a Lua error (the harness or the user's script
                # broke); "refused", or no kind from an older bridge, is a deliberate refusal.
                code = EXIT_BRIDGE if body.get("kind") == "script_error" else EXIT_REFUSED
                raise HarnessError(code, body.get("error", "refused"), body.get("hint", ""))
            if alive is not None and not alive():
                cmd_path.unlink(missing_ok=True)
                raise HarnessError(EXIT_ENGINE, f"engine is not running ({cmd})",
                                   "run `fsoctl crash_report <id>`")
            time.sleep(_POLL_SEC)
        raise self._timeout_error(seq, cmd, cmd_path, timeout)

    def reap_reply(self, seq: int, within: float = 2.0) -> bool:
        """Delete command `seq`'s late reply file, waiting up to `within` seconds for it.

        Returns True if a reply was found and removed. WHY (QA SL2-8, Y-12): a timed-out command
        that the bridge took still answers when it finishes (a cancelled pilot does at once), and
        nobody reads that file; repeated timeouts would pile them up in the channel folder.
        """
        res_path = self.dir / f"res-{seq:06d}.json"
        deadline = time.monotonic() + within
        while True:   # bounded: returns at the deadline
            try:
                res_path.unlink()
                return True
            except FileNotFoundError:
                if time.monotonic() >= deadline:
                    return False
            time.sleep(_POLL_SEC)

    def _timeout_error(self, seq: int, cmd: str, cmd_path: Path, timeout: float) -> HarnessError:
        """Withdraw an unanswered command if the bridge has not taken it; return the error to raise.

        WHY a rename and not an unlink (QA SL-6): the bridge deletes a command file as it takes
        it, and deferred commands (pilot) keep running after that with no reply yet. The rename
        fails with FileNotFoundError exactly when the bridge already deleted the file, so the two
        outcomes can be told apart and reported differently. A ".withdrawn" name no longer matches
        the bridge's ^cmd-N.json$ pattern. The bridge claims a command by renaming it before it
        reads it (fsoharness.lua run_command), so exactly one of the two renames wins: when this
        one succeeds, the bridge never read the command.
        """
        withdrawn = cmd_path.with_name(cmd_path.name + ".withdrawn")
        try:
            os.rename(cmd_path, withdrawn)
        except FileNotFoundError:
            fate, said = "taken_unknown", "the bridge took the command; outcome unknown"
            hint = "do not retry a non-idempotent command blindly; check state"
        else:
            withdrawn.unlink(missing_ok=True)
            fate, said = "withdrawn", "withdrawn; it did not run"
            hint = "take a screenshot; a popup or a loading screen may be blocking"
        return HarnessError(EXIT_TIMEOUT, f"no reply to {cmd} in {timeout:.0f}s: {said} "
                                          f"({self.describe_heartbeat()})", hint,
                            details={"seq": seq, "fate": fate})
