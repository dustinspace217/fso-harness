"""Game verbs that talk to the bridge: state, sexps, popups, opt-in Lua and raw events.

Every call first requires a running engine (launcher.require_running), so a dead session answers
at once with EXIT_ENGINE and a crash_report hint instead of waiting out a timeout (Review Focus 2).
The eval gate is checked here as well as in the bridge, so a refusal never depends on the bridge
alone, and every allowed eval is written to the session's audit.log before it is sent.
"""
import argparse
import os
import re
import time
from dataclasses import asdict, dataclass
from typing import Any

from . import channel, docs, launcher, logs, lua_check, session
from .cli import register
from .errors import EXIT_BRIDGE, EXIT_ENGINE, EXIT_REFUSED, EXIT_TIMEOUT, EXIT_USAGE, HarnessError

_MODES = ("eval", "run", "number")
_LIST_LIMIT_MAX = 1000
# Sexp operators that run Lua: script-eval, script-eval-block/-bool/-num/-string, multi-eval
# (this build's sexps.html). Without a check, `sexp` would be a way around --allow-eval (found by a
# background security review of the Task 11 commit). WHY "contains eval" and not a parser of our
# own: a second review found our regex and FSO's tokenizer could disagree (e.g. a zero-width space
# before the operator). FSO only treats a token as an operator on an exact name match, and no
# deprecated-name alias maps to these (sexp.cpp get_sexp), so every Lua-running sexp contains the
# letters "eval". Any other expression that happens to contain them also needs --allow-eval.
_SCRIPT_SEXP = re.compile("eval", re.IGNORECASE)
# The engine copies a sexp into `char buf[8192]` before parsing (sexp.cpp run_sexp, strcpy_s) and
# aborts on overflow. Counted in UTF-8 bytes, as the copy is. QA fix wave 1 has the bridge refuse
# 8000 bytes or more of assembled sexp text; this side uses the same boundary.
_SEXP_MAX_BYTES = 8000
# wait_for names are pasted into a sexp the bridge wraps in its own text, which eats into the 8000;
# event and ship names are normally short, so names get a much lower cap.
_NAME_MAX_BYTES = 1024
_ECHO_MAX = 200     # caller text quoted back in a message is clipped to this many characters


def _clip(text: str) -> str:
    """Return caller text shortened to _ECHO_MAX characters plus "…", for error messages."""
    return text if len(text) <= _ECHO_MAX else text[:_ECHO_MAX] + "…"


def check_sexp_length(what: str, text: str, limit: int = _SEXP_MAX_BYTES) -> None:
    """Raise EXIT_USAGE if `text` is `limit` UTF-8 bytes or more; `what` names it in the message."""
    size = len(text.encode("utf-8", errors="surrogatepass"))
    if size >= limit:
        raise HarnessError(EXIT_USAGE, f"{what} is {size} bytes; it must be under {limit}",
                           "the engine's sexp buffer is 8192 bytes; shorten or split it")


def check_mode(mode: str) -> str:
    """Return a valid sexp mode ("eval" | "run" | "number") or raise EXIT_USAGE."""
    if mode not in _MODES:
        raise HarnessError(EXIT_USAGE, f"bad mode {_clip(mode)!r}",
                           f"use one of {', '.join(_MODES)}")
    return mode


def _require_game(sid: str) -> session.Session:
    """Return the running session; refuse at once (EXIT_REFUSED) if it is not a game session.

    WHY (QA CR-15): a qtFRED session has no bridge, so a game verb sent to one used to wait out
    its whole timeout and report EXIT_TIMEOUT, which reads as "the game is stuck".
    """
    sess = launcher.require_running(sid)
    if sess.kind != "game":
        raise HarnessError(EXIT_REFUSED, f"session {sid} is a {sess.kind} session; game verbs "
                           "need a game session", "use the fred_* verbs for qtFRED sessions")
    return sess


def call(sid: str, cmd: str, args: dict | None = None, timeout: float = 10.0) -> Any:
    """Send one bridge command to a running game session, with engine-death detection."""
    sess = _require_game(sid)
    return channel.Channel(sess).send(cmd, args, timeout=timeout,
                                      alive=lambda: launcher.alive(sess))


def state(sid: str, ships: bool = True, goals: bool = True, events: bool = True,
          limit: int = 200) -> dict:
    """Return game state, mission time, player, ships, goals and event statuses."""
    return call(sid, "state", {"ships": ships, "goals": goals, "events": events,
                               "limit": min(limit, _LIST_LIMIT_MAX)}, timeout=20)


def sexp(sid: str, expr: str, mode: str = "eval") -> dict:
    """Evaluate a sexp as a boolean ("eval"), a number ("number"), or run it in a when ("run").

    Sexps that run Lua (script-eval*, multi-eval) need --allow-eval and are audited, like `lua`.
    """
    check_mode(mode)
    check_sexp_length("sexp", expr)
    if _SCRIPT_SEXP.search(expr):
        _gate(sid, "sexp-script", expr)
    return call(sid, "sexp", {"expr": expr, "mode": mode})


def _gate(sid: str, kind: str, text: str) -> None:
    """Refuse unless the session allows eval; audit the action when it does."""
    sess = _require_game(sid)
    if not sess.allow_eval:
        raise HarnessError(EXIT_REFUSED, f"{kind} is off for this session",
                           "relaunch with --allow-eval")
    session.audit(sess, kind, text)


def lua(sid: str, code: str) -> dict:
    """Run free-form Lua in the engine (opt-in, audited); returns {"value": tostring(result)}.

    Before sending, the code (and files it loadfile()s) is checked against this build's cached
    API reference for the mistakes that abort the engine past pcall (fsoctl/lua_check.py); the
    reply's "lua_check" says whether the check ran.
    """
    _gate(sid, "lua", code)
    sess = _require_game(sid)
    # The check is an extra: if anything in it fails (no FS2PATH, a binary being rebuilt, a
    # damaged cache), send the code as before and say why the check did not run (QA CR-3).
    ref = None
    try:
        ref = docs.cached_reference(sess.binary, sess.mods)
        found, unchecked = lua_check.check(code, ref / "scripting.json") if ref else ([], [])
        status = "passed" if ref else "skipped: no api_docs cache for this build"
    except Exception as e:   # deliberate fail-open (see above); the reply carries the reason
        found, unchecked, status = [], [], f"skipped: check error: {e}"
    if found:
        raise HarnessError(EXIT_REFUSED, "Lua refused before sending (it would abort the engine): "
                           + "; ".join(found), f"names checked against {ref}/scripting.json "
                           "(readable form: scripting.lua next to it)")
    reply = call(sid, "lua", {"code": code})
    reply["lua_check"] = status + (f" (not checked: {', '.join(unchecked)})" if unchecked else "")
    return reply


def post_event(sid: str, name: str) -> dict:
    """Post a raw game event by name, e.g. GS_EVENT_QUIT_GAME (opt-in, audited)."""
    _gate(sid, "post_event", name)
    return call(sid, "post_event", {"name": name})


def answer_popup(sid: str, choice: int = 0) -> dict:
    """Answer the popup that is open now with button index `choice` (0 = first button)."""
    return call(sid, "answer_popup", {"choice": choice})


_AXES = ("pitch", "heading", "bank", "forward")
_PILOT_SECONDS = (0.05, 60.0)
_COMPRESSION = (10, 6400)   # percent; FSO's own range tops out at 64x


def check_pilot_args(pitch: float = 0, heading: float = 0, bank: float = 0, forward: float = 0,
                     seconds: float = 1.0, **_fire) -> None:
    """Raise EXIT_USAGE unless every axis is in [-1, 1] and seconds in [0.05, 60]."""
    for name, value in zip(_AXES, (pitch, heading, bank, forward), strict=True):
        if not -1.0 <= value <= 1.0:
            raise HarnessError(EXIT_USAGE, f"{name} {value} is outside -1..1", "axes are -1..1")
    if not _PILOT_SECONDS[0] <= seconds <= _PILOT_SECONDS[1]:
        raise HarnessError(EXIT_USAGE, f"seconds {seconds} is outside 0.05..60",
                           "split longer flights into several pilot calls")


@dataclass
class Flight:
    """One pilot command: axes in -1..1 (forward is throttle), fire flags, mission seconds."""
    pitch: float = 0.0
    heading: float = 0.0
    bank: float = 0.0
    forward: float = 0.0
    primary: bool = False
    secondary: bool = False
    cm: bool = False
    seconds: float = 1.0
    # A NEW contact with a ship, debris or an asteroid ends the flight at once
    # (stopped_because "collision", with the direction to what was hit), the way a pilot reacts
    # (Dustin 2026-10-12). Contact that began before the flight does not stop it, so an escape
    # flight can fly off (QA SLA-1). The engine does not report very light debris or asteroid
    # brushes (below its damage threshold), so neither does this (QA CR-4).
    stop_on_collision: bool = True


def pilot(sid: str, **controls: Any) -> dict:
    """Fly the player ship for `seconds` of mission time, then hand control back.

    Receives Flight's fields as keywords (pitch=1.0, primary=True, seconds=2, ...). primary and
    secondary fire every frame (the weapon's own refire rate applies); cm launches countermeasures.
    Returns {"applied_frames", "shots", "mission_time", "completed", "stopped_because",
    "collisions", "collision", "touching"}: collisions = contacts started during the flight,
    collision (or null) the latest one with its direction in the ship's own axes, touching
    whether the ship is still in contact when the flight ends.
    """
    try:
        flight = Flight(**controls)
    except TypeError as e:
        fields = ", ".join(Flight.__dataclass_fields__)
        raise HarnessError(EXIT_USAGE, f"bad pilot argument: {e}", f"use {fields}") from e
    check_pilot_args(pitch=flight.pitch, heading=flight.heading, bank=flight.bank,
                     forward=flight.forward, seconds=flight.seconds)
    # The bridge only skips the stop for a literal false, so "false" or 0 would still stop on
    # contact: refuse anything but a bool (QA CR-10).
    if not isinstance(flight.stop_on_collision, bool):
        raise HarnessError(EXIT_USAGE, "stop_on_collision must be true or false",
                           f"got {flight.stop_on_collision!r}")
    try:
        return call(sid, "pilot", asdict(flight), timeout=_pilot_timeout(flight.seconds))
    except HarnessError as e:
        # Ruling R5 / Y-4: cancel only a flight the bridge took. A withdrawn pilot never flew,
        # and a cancel sent anyway could stop the NEXT caller's flight.
        if e.code != EXIT_TIMEOUT or e.details.get("fate") != "taken_unknown":
            raise
        raise _cancel_after_timeout(sid, e) from e


def _pilot_timeout(seconds: float) -> float:
    """Return the wall-clock budget (seconds) for a pilot of `seconds` mission seconds."""
    # Mission seconds can take up to 10x longer in wall time at the lowest time compression.
    return seconds * 10 + 10


def _cancel_after_timeout(sid: str, err: HarnessError) -> HarnessError:
    """Cancel the timed-out flight by its seq; return the timeout error to raise, extended.

    WHY (QA SL-5): the bridge may still be flying the ship (a paused mission stops mission time,
    so the flight never ends), holding Lua flight control and refusing every later pilot. The
    cancel names the flight's seq (ruling R5): the bridge stops it only if that flight is still
    the one flying, otherwise it answers {"cancelled": False, "reason": "no matching flight"}.
    Best-effort: if the cancel fails too, the message says the flight may still be running.
    Once the cancel got through, the flight's own late reply is reaped (QA SL2-8, Y-12).
    details["pilot_cancel"] is the cancel's result, or None if it failed.
    """
    seq = err.details["seq"]
    try:
        res = call(sid, "pilot_cancel", {"seq": seq}, timeout=5)
    except HarnessError as c:
        return HarnessError(EXIT_TIMEOUT, f"{err.message}; pilot_cancel failed too ({c.message}), "
                            "so the flight may still be running",
                            "pilot_cancel did not get through: check `fsoctl state`, and stop the "
                            "session if the ship is still under harness control",
                            details={**err.details, "pilot_cancel": None})
    channel.Channel(session.load(sid)).reap_reply(seq, within=2.0)
    done = ("the flight was cancelled and control handed back" if res.get("cancelled")
            else f"pilot_cancel: {res.get('reason', 'no flight running')}")
    return HarnessError(EXIT_TIMEOUT, f"{err.message}; {done}", err.hint,
                        details={**err.details, "pilot_cancel": res})


def autopilot(sid: str, on: bool) -> dict:
    """Hand the player ship to the AI (on) or take it back (off): player-use-ai / -not-use-ai."""
    return call(sid, "autopilot", {"on": on})


def check_time_args(compression: float | None = None, pause: bool = False,
                    resume: bool = False) -> None:
    """Raise EXIT_USAGE unless exactly one sensible time change is requested."""
    if compression is None and not pause and not resume:
        raise HarnessError(EXIT_USAGE, "nothing to do", "give compression, pause or resume")
    if pause and resume:
        raise HarnessError(EXIT_USAGE, "pause and resume together", "pick one")
    if compression is not None and not _COMPRESSION[0] <= compression <= _COMPRESSION[1]:
        raise HarnessError(EXIT_USAGE, f"compression {compression}% is outside 10..6400",
                           "100 = normal speed, 400 = 4x")


def set_time(sid: str, compression: float | None = None, pause: bool = False,
             resume: bool = False) -> dict:
    """Set time compression (percent of 1x), or pause / resume the mission."""
    check_time_args(compression, pause, resume)
    return call(sid, "time", {"compression": compression, "pause": pause, "resume": resume})


_TELEMETRY_ACTIONS = ("on", "off", "read", "clear")


def telemetry(sid: str, action: str, seconds: float = 10.0, hz: float = 2.0) -> dict:
    """Switch per-frame flight telemetry on or off, read it, or clear it.

    WHY switchable: recording costs work every frame, and most runs do not need it (Dustin
    2026-10-12). While on, the bridge keeps the last 15000 frames (nose vector, turn rates as a
    fraction of max, speed, aim error to the current target, hull). `read` returns a summary
    (aim error, % of frames within 3 degrees, wiggle = turn-rate sign flips per second) plus
    samples at `hz` over the last `seconds`: at most 120 s x 10 Hz, so a read stays small.
    `on` starts a fresh recording (earlier rows are dropped).
    """
    if action not in _TELEMETRY_ACTIONS:
        raise HarnessError(EXIT_USAGE, f"bad telemetry action {_clip(action)!r}",
                           f"use one of {', '.join(_TELEMETRY_ACTIONS)}")
    if not (0.1 <= seconds <= 120 and 0.1 <= hz <= 10):
        raise HarnessError(EXIT_USAGE, f"telemetry seconds {seconds} / hz {hz} out of range",
                           "seconds 0.1-120, hz 0.1-10")
    return call(sid, "telemetry", {"action": action, "seconds": seconds, "hz": hz})


_WAIT_KINDS = ("state", "mission_time", "event_true", "event_false", "destroyed", "departed",
               "goal", "log", "collision")
_WAIT_MAX_SEC = 3600.0


def check_wait_kind(kind: str) -> str:
    """Return a valid wait kind or raise EXIT_USAGE."""
    if kind not in _WAIT_KINDS:
        raise HarnessError(EXIT_USAGE, f"bad wait kind {_clip(kind)!r}",
                           f"use one of {', '.join(_WAIT_KINDS)}")
    return kind


# Characters FSO's sexp reader treats as structure, refused in names the bridge pastes into sexp
# text (QA SA-1): `"` delimits strings, `!` is turned into `"` before parsing (sexp.cpp, the
# "HACK: ! -> \"" loop), and ( ) open and close operators. The SA-1 probe ran Lua through an event
# name with no --allow-eval. Control characters (C0, DEL, C1) are refused too: the harness does
# not support names containing them. QA fix wave 1 has the bridge refuse these as well; this side
# keeps the refusal from depending on the bridge alone, as the eval gate does.
_SEXP_UNSAFE = re.compile(r'["!()\x00-\x1f\x7f-\x9f]')
_NAME_KINDS = ("event_true", "event_false", "destroyed", "departed")
_LOG_TAIL = 4096                  # bytes searched for the last newline when a log wait starts
_LOG_READ_MAX = 4 * 1024 * 1024   # bytes read per poll at most; the rest waits for the next poll


def check_wait_name(sid: str, kind: str, value: Any) -> None:
    """Refuse (EXIT_REFUSED) an event/ship name that could change the sexp the bridge builds.

    Receives the wait kind and its value; returns nothing. Over-long names are EXIT_USAGE. A name
    containing "eval" passes the same --allow-eval gate (and audit) as the sexp verb, matching its
    rule for script sexps.
    """
    if kind not in _NAME_KINDS:
        return
    text = str(value)
    check_sexp_length(f"{kind} name", text, _NAME_MAX_BYTES)
    bad = _SEXP_UNSAFE.search(text)
    if bad:
        raise HarnessError(EXIT_REFUSED, f"{kind} name {_clip(text)!r} contains "
                           f"{bad.group()!r}, which a sexp would read as structure",
                           'names cannot contain " ! ( ) or control characters; list the real '
                           "names with `fsoctl state`")
    if _SCRIPT_SEXP.search(text):
        _gate(sid, "wait_for-script", f"{kind} {text}")


def _log_start(sess: session.Session) -> dict:
    """Return the log-wait cursor: which log to read and the byte offset where new lines begin.

    The offset is just after the last complete line, so a line the engine is halfway through
    writing when the wait starts counts as new once it is finished (block-buffered stdout stops
    mid-line; QA CR-3, SF-6). No newline in the last _LOG_TAIL bytes of a longer file: start at
    the end.
    """
    srcs = logs.sources(sess)
    path = srcs["fso"] if srcs["fso"].exists() else srcs["engine"]
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            back = min(size, _LOG_TAIL)
            f.seek(size - back)
            nl = f.read(back).rfind(b"\n")
    except FileNotFoundError:
        return {"path": path, "offset": 0, "fso": srcs["fso"]}
    if nl < 0:   # no newline in the tail: a short file is one unfinished line, a long one is old
        nl = -1 if back == size else back - 1
    return {"path": path, "offset": size - back + nl + 1, "fso": srcs["fso"]}


def _new_log_lines(cur: dict) -> list[str]:
    """Return complete lines written since the last call, advancing the cursor past them only.

    Reads only the new bytes (QA CR-3: a debug log can run to tens of MB, and the old code re-read
    all of it every poll) and leaves an unfinished last line for the next poll. If the debug log
    appears after the wait started, the wait switches to it from its first byte: all of it is new.
    """
    if cur["path"] != cur["fso"] and cur["fso"].exists():
        cur.update(path=cur["fso"], offset=0)
    try:
        with open(cur["path"], "rb") as f:
            size = os.fstat(f.fileno()).st_size
            if size < cur["offset"]:          # truncated or replaced: read it from the start
                cur["offset"] = 0
            f.seek(cur["offset"])
            data = f.read(min(size - cur["offset"], _LOG_READ_MAX))
    except FileNotFoundError:
        return []
    end = data.rfind(b"\n")
    if end < 0:
        if len(data) < _LOG_READ_MAX:
            return []
        end = len(data) - 1   # one line longer than the cap: take it whole so the wait moves on
    cur["offset"] += end + 1
    return data[:end + 1].decode(errors="replace").splitlines()


def _compile(pattern: str) -> re.Pattern:
    """Compile a user regex or raise EXIT_USAGE."""
    try:
        return re.compile(pattern)
    except re.error as e:
        raise HarnessError(EXIT_USAGE, f"bad regex: {e}", "use Python regex syntax") from e


def _poll_once(sid: str, kind: str, value: Any, log: dict | None) -> tuple[bool, Any]:
    """Evaluate the condition once; for "log", only lines written after the wait began count."""
    if log is None:
        r = call(sid, "check", {"kind": kind, "value": value}, timeout=10)
        return r["met"], r.get("detail")
    hit = next((line for line in _new_log_lines(log["cursor"]) if log["pattern"].search(line)),
               None)
    return hit is not None, hit


def _collision_baseline(sid: str) -> int:
    """Return the engine-run contact count now, the baseline a collision wait compares against.

    Receives the session id. Raises EXIT_BRIDGE when the reply has no count, which is what a
    bridge older than collision alerts answers (QA SF-9: a raw index gave a TypeError traceback).
    """
    base = call(sid, "check", {"kind": "collision", "value": -1}, timeout=10)
    detail = base.get("detail")
    total = detail.get("total") if isinstance(detail, dict) else None
    if not isinstance(total, int) or isinstance(total, bool):
        raise HarnessError(EXIT_BRIDGE,
                           f"the bridge reported no collision count: {_clip(str(base))}",
                           "the loaded bridge may predate collision alerts; stop and launch again")
    return total


def wait_for(sid: str, kind: str, value: Any, timeout: float = 60.0, poll: float = 0.25) -> dict:
    """Block until a condition holds; return {"met": True, "after_sec", "detail"}.

    Kinds: state (name), mission_time (seconds, met when reached), event_true / event_false
    (event name), goal ("Name=complete|failed|incomplete"), destroyed / departed (ship or wing),
    log (Python regex on lines written after the call), collision (any value; met once the player
    starts a new contact after the call; detail.last is the latest contact when the poll saw it,
    detail.touching whether still in contact). Bounded by timeout (max
    3600 s); an engine that exits ends the wait at once with EXIT_ENGINE. Event and ship names
    with sexp delimiters are refused (check_wait_name). Every kind but log needs a game session
    (it asks the bridge).
    """
    check_wait_kind(kind)
    sess = launcher.require_running(sid) if kind == "log" else _require_game(sid)
    check_wait_name(sid, kind, value)
    timeout = min(timeout, _WAIT_MAX_SEC)
    log = None
    if kind == "log":
        log = {"pattern": _compile(str(value)), "cursor": _log_start(sess)}
    if kind == "collision":   # the value is ignored: met by the next new contact after this call
        value = _collision_baseline(sid)
    start = time.monotonic()
    detail = None
    while time.monotonic() - start < timeout:
        if not launcher.alive(sess):
            raise HarnessError(EXIT_ENGINE, "engine exited while waiting",
                               f"run `fsoctl crash_report {sid}`")
        met, detail = _poll_once(sid, kind, value, log)
        if met:
            return {"met": True, "after_sec": round(time.monotonic() - start, 2), "detail": detail}
        time.sleep(poll)
    what = "a new contact" if kind == "collision" else repr(_clip(str(value)))
    raise HarnessError(EXIT_TIMEOUT, f"{kind} {what} not met in {timeout:.0f}s "
                       f"(last: {_clip(str(detail))})",
                       "take a screenshot or check `fsoctl state`")


# ---- command line ----------------------------------------------------------------------------

def _cfg_state(p: argparse.ArgumentParser) -> None:
    """Add the `state` verb's arguments."""
    p.add_argument("id")
    p.add_argument("--no-ships", action="store_true")
    p.add_argument("--no-goals", action="store_true")
    p.add_argument("--no-events", action="store_true")
    p.add_argument("--limit", type=int, default=200)


@register("state", "game state, player, ships, goals and events", _cfg_state)
def _cli_state(args: argparse.Namespace) -> dict:
    """CLI wrapper for state()."""
    return state(args.id, not args.no_ships, not args.no_goals, not args.no_events, args.limit)


def _cfg_sexp(p: argparse.ArgumentParser) -> None:
    """Add the `sexp` verb's arguments."""
    p.add_argument("id")
    p.add_argument("expr", help='e.g. "( is-destroyed-delay 0 !Alpha 2! )"')
    p.add_argument("--mode", choices=_MODES, default="eval")


@register("sexp", "evaluate or run a sexp in the mission", _cfg_sexp)
def _cli_sexp(args: argparse.Namespace) -> dict:
    """CLI wrapper for sexp()."""
    return sexp(args.id, args.expr, args.mode)


def _cfg_lua(p: argparse.ArgumentParser) -> None:
    """Add the `lua` verb's arguments."""
    p.add_argument("id")
    p.add_argument("code", help="Lua 5.1 chunk; its return value is stringified")


@register("lua", "run free-form Lua (session must allow eval)", _cfg_lua)
def _cli_lua(args: argparse.Namespace) -> dict:
    """CLI wrapper for lua()."""
    return lua(args.id, args.code)


def _cfg_event(p: argparse.ArgumentParser) -> None:
    """Add the `post_event` verb's arguments."""
    p.add_argument("id")
    p.add_argument("name", help="game event, e.g. GS_EVENT_QUIT_GAME")


@register("post_event", "post a raw game event (session must allow eval)", _cfg_event)
def _cli_post_event(args: argparse.Namespace) -> dict:
    """CLI wrapper for post_event()."""
    return post_event(args.id, args.name)


def _cfg_popup(p: argparse.ArgumentParser) -> None:
    """Add the `answer_popup` verb's arguments."""
    p.add_argument("id")
    p.add_argument("--choice", type=int, default=0, help="button index, 0 = first")


@register("answer_popup", "answer the open popup", _cfg_popup)
def _cli_answer_popup(args: argparse.Namespace) -> dict:
    """CLI wrapper for answer_popup()."""
    return answer_popup(args.id, args.choice)


def _cfg_pilot(p: argparse.ArgumentParser) -> None:
    """Add the `pilot` verb's arguments."""
    p.add_argument("id")
    for axis in _AXES:
        p.add_argument(f"--{axis}", type=float, default=0.0)
    p.add_argument("--primary", action="store_true", help="fire primaries every frame")
    p.add_argument("--secondary", action="store_true")
    p.add_argument("--cm", action="store_true", help="launch countermeasures")
    p.add_argument("--seconds", type=float, default=1.0, help="mission seconds, 0.05-60")
    p.add_argument("--no-stop-on-collision", dest="stop_on_collision", action="store_false",
                   help="keep flying through a contact (default: stop and report it)")


@register("pilot", "fly the player ship for a few seconds", _cfg_pilot)
def _cli_pilot(args: argparse.Namespace) -> dict:
    """CLI wrapper for pilot()."""
    return pilot(args.id, pitch=args.pitch, heading=args.heading, bank=args.bank,
                 forward=args.forward, primary=args.primary, secondary=args.secondary,
                 cm=args.cm, seconds=args.seconds, stop_on_collision=args.stop_on_collision)


def _cfg_autopilot(p: argparse.ArgumentParser) -> None:
    """Add the `autopilot` verb's arguments."""
    p.add_argument("id")
    p.add_argument("state", choices=("on", "off"))


@register("autopilot", "let the AI fly the player ship, or take it back", _cfg_autopilot)
def _cli_autopilot(args: argparse.Namespace) -> dict:
    """CLI wrapper for autopilot()."""
    return autopilot(args.id, args.state == "on")


def _cfg_time(p: argparse.ArgumentParser) -> None:
    """Add the `time` verb's arguments."""
    p.add_argument("id")
    p.add_argument("--compression", type=float, help="percent of normal speed, 10-6400")
    p.add_argument("--pause", action="store_true")
    p.add_argument("--resume", action="store_true")


@register("time", "time compression, pause and resume", _cfg_time)
def _cli_time(args: argparse.Namespace) -> dict:
    """CLI wrapper for set_time()."""
    return set_time(args.id, args.compression, args.pause, args.resume)


def _cfg_telemetry(p: argparse.ArgumentParser) -> None:
    """Add the `telemetry` verb's arguments."""
    p.add_argument("id")
    p.add_argument("action", choices=_TELEMETRY_ACTIONS)
    p.add_argument("--seconds", type=float, default=10.0, help="read window, 0.1-120")
    p.add_argument("--hz", type=float, default=2.0, help="read sample rate, 0.1-10")


@register("telemetry", "per-frame flight telemetry: on, off, read, clear", _cfg_telemetry)
def _cli_telemetry(args: argparse.Namespace) -> dict:
    """CLI wrapper for telemetry()."""
    return telemetry(args.id, args.action, args.seconds, args.hz)


def _cfg_wait(p: argparse.ArgumentParser) -> None:
    """Add the `wait_for` verb's arguments."""
    p.add_argument("id")
    p.add_argument("kind", choices=_WAIT_KINDS)
    p.add_argument("value", help="state name, seconds, event/ship name, Goal=status, regex, "
                   "or anything for collision")
    p.add_argument("--timeout", type=float, default=60.0)


@register("wait_for", "block until a game condition holds (bounded)", _cfg_wait)
def _cli_wait_for(args: argparse.Namespace) -> dict:
    """CLI wrapper for wait_for(); mission_time takes a number."""
    value: Any = args.value
    if args.kind == "mission_time":
        try:
            value = float(args.value)
        except ValueError as e:
            raise HarnessError(EXIT_USAGE,
                               f"mission_time needs seconds, got {_clip(args.value)!r}",
                               "e.g. wait_for <id> mission_time 30") from e
    return wait_for(args.id, args.kind, value, args.timeout)
