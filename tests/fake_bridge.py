"""Emulates the Lua bridge's file protocol in a thread for unit tests.

Commands it understands beyond the plain echo (every other command echoes its args):
- "refuse": an H.raise refusal (reply kind "refused");
- "refuse_nokind": a refusal from an older bridge that sends no kind at all;
- "boom": a Lua runtime error (reply kind "script_error", contract C2);
- "pilot_cancel {seq}": cancels only the hung "pilot" whose seq matches (ruling R5), replying
  {"cancelled": True} and then, like the real bridge, the cancelled pilot's own late reply;
  otherwise {"cancelled": False, "reason": "no matching flight"};
- "clock": {"frametime_overall", "mission_time"} (contract C4);
- "check": wait_for's condition check, always {"met": False, "detail": "pending"}.
`script` maps a command name to how the fake treats it (one dict instead of four keyword
arguments, the repo's way of staying under the argument-count lint):
- "hang": taken but never answered, like a deferred command that outlives its caller's timeout;
  a hung "pilot" becomes the fake's current flight until a matching "pilot_cancel";
- "ignore": never taken at all (a bridge stuck before reading its commands);
- "fail": answered with a "refused" reply, as if the handler could not run;
- a number: answered that many seconds after it was taken.
"""
import json
import threading
import time
from pathlib import Path

# Replies that never depend on the fake's state.
_FIXED = {
    "refuse": {"ok": False, "error": "nope", "hint": "try x", "kind": "refused"},
    "refuse_nokind": {"ok": False, "error": "nope", "hint": "try x"},
    "boom": {"ok": False, "error": "attempt to index a nil value", "hint": "see bridge_info",
             "kind": "script_error"},
    "clock": {"ok": True, "result": {"frametime_overall": 12.5, "mission_time": None}},
    "check": {"ok": True, "result": {"met": False, "detail": "pending"}},   # never met
}


class FakeBridge:
    """Answers cmd-*.json files in `directory` like the real bridge would."""

    def __init__(self, directory: Path, state: str = "GS_STATE_MAIN_MENU",
                 script: dict[str, str | float] | None = None) -> None:
        self.dir = directory
        self.state = state
        script = script or {}
        self.hang = {n for n, how in script.items() if how == "hang"}
        self.fail = {n for n, how in script.items() if how == "fail"}
        self.ignore = {n for n, how in script.items() if how == "ignore"}
        self.delay = {n: float(how) for n, how in script.items() if not isinstance(how, str)}
        self.flight_seq: int | None = None   # seq of the hung pilot that is "flying", if any
        self.stop_flag = threading.Event()
        self.seen: list[int] = []
        self.cmds: list[str] = []          # command names in the order the fake took them
        self.calls: list[tuple[str, dict]] = []   # (name, args) in the same order
        self._due: list[tuple[float, dict]] = []  # delayed replies: (monotonic due time, body)
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop_flag.set()
        self.thread.join(5)

    def _cancel(self, seq: int, args: dict) -> dict:
        """Answer pilot_cancel: stop the flight only if args["seq"] names it (ruling R5)."""
        if self.flight_seq is None or args.get("seq") != self.flight_seq:
            return {"seq": seq, "ok": True,
                    "result": {"cancelled": False, "reason": "no matching flight"}}
        # The real bridge answers the stopped pilot too, after its caller gave up (Y-12).
        self._write({"seq": self.flight_seq, "ok": True,
                     "result": {"completed": False, "stopped_because": "cancelled"}})
        self.flight_seq = None
        return {"seq": seq, "ok": True, "result": {"cancelled": True}}

    def _reply(self, cmd: dict) -> dict | None:
        """Return the reply body for one command, or None to leave it unanswered."""
        name, seq = cmd["cmd"], cmd["seq"]
        if name in self.hang:
            if name == "pilot":
                self.flight_seq = seq
            return None
        if name in self.fail:
            name = "refuse"
        if name in _FIXED:
            return {"seq": seq, **_FIXED[name]}
        if name == "pilot_cancel":
            return self._cancel(seq, cmd.get("args", {}))
        return {"seq": seq, "ok": True, "result": {"echo": cmd["args"]}}

    def _claim(self, f: Path) -> dict | None:
        """Take a command like the real bridge: rename it first, then read it (QA TA2-7).

        Returns the command, or None when the caller withdrew it before the rename landed. With
        read-then-unlink, a withdraw between the two killed the fake's thread silently and the
        withdraw tests could not tell the protocols apart.
        """
        taken = f.with_name(f.name + ".run")
        try:
            f.rename(taken)
        except FileNotFoundError:
            return None
        cmd = json.loads(taken.read_text())
        taken.unlink()
        return cmd

    @staticmethod
    def _peek(f: Path) -> str | None:
        """Return a waiting command's name without taking it (None if it was withdrawn)."""
        try:
            return json.loads(f.read_text())["cmd"]
        except FileNotFoundError:
            return None

    def _write(self, body: dict) -> None:
        """Publish one reply file atomically (temp file + rename), as the bridge does."""
        tmp = self.dir / f"res-{body['seq']:06d}.json.tmp"
        tmp.write_text(json.dumps(body))
        tmp.rename(self.dir / f"res-{body['seq']:06d}.json")

    def _take_all(self) -> None:
        """Claim and answer (or schedule, or drop) every waiting command once."""
        for f in sorted(self.dir.glob("cmd-*.json")):
            if self.ignore and self._peek(f) in self.ignore:
                continue
            cmd = self._claim(f)
            if cmd is None:
                continue
            self.seen.append(cmd["seq"])
            self.cmds.append(cmd["cmd"])
            self.calls.append((cmd["cmd"], cmd.get("args", {})))
            body = self._reply(cmd)
            if body is None:
                continue
            wait = self.delay.get(cmd["cmd"], 0.0)
            if wait:
                self._due.append((time.monotonic() + wait, body))
            else:
                self._write(body)

    def _loop(self) -> None:
        """Write a heartbeat and answer pending commands every ~10 ms until stopped."""
        frame = 0
        while not self.stop_flag.is_set():   # bounded by the test: __exit__ sets the flag
            frame += 1
            (self.dir / "ready.json").write_text(json.dumps({"state": self.state, "frame": frame}))
            self._take_all()
            now = time.monotonic()
            for due, body in [d for d in self._due if d[0] <= now]:
                self._write(body)
                self._due.remove((due, body))
            time.sleep(0.01)
