"""Command journal for record/replay.

Every action sent to a session (bridge commands that change something, keys, clicks, drags,
scrolls) is appended to <run>/journal.jsonl with its time since launch and the game state it was
issued in. `replay` launches a new session with the same settings and re-issues the actions in
order, waiting for the same game state before each one.

Honest limit (spec section 7): replay repeats the commands, not the results. The engine has no
fixed timestep (frame times come from the wall clock) and seeds some randomness from the OS, so
two runs drift apart.
"""
import argparse
import json
import secrets
import time
from pathlib import Path
from typing import Any

from . import paths, session, shadowdir
from .cli import register
from .errors import (
    EXIT_BRIDGE,
    EXIT_ENGINE,
    EXIT_NOT_FOUND,
    EXIT_REFUSED,
    EXIT_TIMEOUT,
    EXIT_USAGE,
    HarnessError,
)

JOURNAL = "journal.jsonl"
MAX_ENTRIES = 100_000          # bounds load() and replay(); a longer journal is refused
# Bridge commands that only read; replaying them changes nothing, so they are not journaled.
READ_ONLY = frozenset({"ping", "basic_state", "bridge_info", "state", "check", "control_mode"})
# Not journaled at all: the reads above, plus pilot_cancel (ruling R6): it is cleanup for one
# timed-out flight, identified by that flight's seq, so replaying it could only cancel nothing.
UNJOURNALED = READ_ONLY | {"pilot_cancel"}
# A timed-out command's fate (channel._timeout_error puts it in the error's details): "withdrawn"
# means it provably did not run; "taken_unknown" means the bridge took it and may have run it.
TIMEOUT_FATES = ("withdrawn", "taken_unknown")
# Exit code of a failed bridge command -> the journal's outcome word for it (QA SL-11).
OUTCOMES = {EXIT_REFUSED: "refused", EXIT_TIMEOUT: "timeout", EXIT_ENGINE: "engine_dead",
            EXIT_BRIDGE: "bridge_error"}
REPLAY_NOTE = ("same commands, not guaranteed same results: the engine has no fixed timestep and "
               "seeds some randomness from the OS")
_STATE_WAIT_SEC = 120.0


def _current_state(sess: session.Session) -> str | None:
    """Return the game state from the bridge heartbeat file, or None (qtFRED, not started)."""
    hb = paths.channel_dir(session.prefs_dir(sess)) / "ready.json"
    try:
        return json.loads(hb.read_text()).get("state")
    except (OSError, ValueError):
        return None


def entry(sess: Any, kind: str, args: dict, seq: int | None = None) -> dict:
    """Return a journal entry stamped at issue time: time since launch, kind, args, game state.

    WHY stamped before the action but written after it (QA SL-11): replay waits for the state the
    action was ISSUED in, while only the reply says whether it ran. The channel builds the entry
    before sending and writes it with its outcome once the reply (or the failure) is known.
    """
    ent = {"t": round(time.time() - sess.created, 3), "kind": kind, "args": args,
           "state": _current_state(sess)}
    if seq is not None:
        ent["seq"] = seq          # the channel's issue order; breaks ties in t (Y-5)
    return ent


def outcome_of(err: HarnessError) -> str:
    """Return the journal outcome word for a failed bridge command (QA SL-11, ruling R6)."""
    fate = err.details.get("fate")
    if err.code == EXIT_TIMEOUT and fate in TIMEOUT_FATES:
        return fate
    return OUTCOMES.get(err.code, "error")


def write(sess: Any, ent: dict, outcome: str) -> None:
    """Append an entry from entry() with its outcome (one of OUTCOMES' values, or "ok")."""
    with open(session.run_dir(sess) / JOURNAL, "a") as f:
        f.write(json.dumps({**ent, "outcome": outcome}) + "\n")


def append(sess: Any, kind: str, args: dict) -> None:
    """Journal an action that has already succeeded (xinput's keys, clicks, drags, scrolls)."""
    write(sess, entry(sess, kind, args), "ok")


def load(sid: str) -> list[dict]:
    """Return a session's journal entries in order (bounded by MAX_ENTRIES)."""
    path = session.run_dir(session.load(sid)) / JOURNAL
    if not path.exists():
        return []
    out = []
    with open(path) as f:
        for line in f:
            if len(out) >= MAX_ENTRIES:
                raise HarnessError(EXIT_USAGE, f"journal longer than {MAX_ENTRIES} entries",
                                   "replay a shorter session")
            out.append(json.loads(line))
    return out


def offsets(entries: list[dict], speed: float = 1.0) -> list[float]:
    """Return when to dispatch each entry, in seconds after the first: (t_i - t_0) / speed.

    WHY offsets and not gaps (cross-family review X-4): replay used to sleep the whole recorded
    gap after each command, but commands run synchronously, so their run time was counted twice
    and the replay drifted later with every step. Offsets are targets on one clock; replay sleeps
    only what remains until the next target, and not at all when it is already late.
    """
    if speed <= 0:
        raise HarnessError(EXIT_USAGE, f"bad speed {speed}", "use a positive number, 1 = real time")
    if not entries:
        return []
    t0 = entries[0]["t"]
    return [max(0.0, (e["t"] - t0) / speed) for e in entries]


def launch_request_for(sess: session.Session, binary: str | None = None,
                       renderer: str | None = None):
    """Rebuild the LaunchRequest a session was started with (optionally another binary/renderer).

    An existing pilot is copied again from the source session's private prefs folder.
    """
    from .launcher import LaunchRequest  # noqa: PLC0415 - launcher imports channel imports record
    sw = sess.switches
    players = session.prefs_dir(sess) / "data" / "players"
    return LaunchRequest(
        binary=binary or sess.binary, mission=sess.mission, mods=list(sess.mods),
        renderer=renderer or sess.renderer, width=sess.width, height=sess.height,
        skip_movies=sw.get("skip_movies", False), pilot_mode=sw.get("pilot_mode", "chooser"),
        pilot_name=sw.get("pilot_name", "HarnessPilot"),
        pilot_source=str(players) if sw.get("pilot_mode") == "existing" else None,
        dismiss_popups=sw.get("dismiss_popups", False),
        skip_command_briefing=sw.get("skip_command_briefing", False),
        skip_briefing=sw.get("skip_briefing", False),
        noninteractive=sw.get("noninteractive", True), allow_eval=sess.allow_eval,
        extra_args=list(sess.extra_args), kind=sess.kind, cwd=_replay_cwd(sess))


def _replay_cwd(sess: session.Session) -> str | None:
    """Return the working folder for a replay of `sess`: None (FS2PATH) only if it had none.

    WHY (cross-family review X-1): frame_stats and docs run from a shadow folder because their
    extra args make the engine write files into the working folder (-profile_write_file writes
    profiling.csv). Dropping cwd made the replay write those files into the user's FS2PATH. A
    shadow source gets a FRESH shadow folder: the old one belongs to the source session and may
    already be cleaned up. Any other cwd is kept as it was.
    """
    if sess.cwd is None:
        return None
    shadows = paths.runtime_root() / "shadows"
    if not Path(sess.cwd).resolve().is_relative_to(shadows.resolve()):
        return sess.cwd
    return str(shadowdir.make(Path(sess.fs2path), shadows / secrets.token_hex(4)))


def _wait_state(sess: session.Session, want: str | None) -> None:
    """Block (bounded) until the session is in game state `want` (no-op when want is None)."""
    deadline = time.monotonic() + _STATE_WAIT_SEC
    while want is not None and _current_state(sess) != want:
        if time.monotonic() > deadline:
            from .channel import Channel  # noqa: PLC0415 - channel imports this module
            raise HarnessError(EXIT_TIMEOUT, f"replay: never reached {want} "
                               f"({Channel(sess).describe_heartbeat()})",
                               "the runs diverged, or the engine hung if stale; see the log")
        time.sleep(0.25)


def _issue(sid: str, kind: str, args: dict) -> None:
    """Re-issue one journaled action. Eval actions go through the gated, audited verbs."""
    from . import game, xinput  # noqa: PLC0415 - both import this module (journaling)
    if kind == "keys":
        xinput.keys(sid, args["keys"], args.get("hold_ms", 0))
    elif kind == "click":
        xinput.click(sid, args["x"], args["y"], args.get("button", 1), args.get("double", False))
    elif kind == "drag":
        xinput.drag(sid, tuple(args["from"]), tuple(args["to"]), args.get("button", 1))
    elif kind == "scroll":
        xinput.scroll(sid, args["x"], args["y"], args["clicks"])
    elif kind == "lua":
        game.lua(sid, args["code"])
    elif kind == "post_event":
        game.post_event(sid, args["name"])
    elif kind == "sexp":
        game.sexp(sid, args["expr"], args.get("mode", "eval"))
    else:
        game.call(sid, kind, args, timeout=30 + float(args.get("seconds", 0)) * 10)


def _runnable(entries: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Order a journal by issue time and split it: (to re-issue, skipped, uncertain).

    Order (cross-family review Y-5): entries are appended when their reply arrives, so a slow
    command issued first can be written after a fast one issued later. Replay sorts by the issue
    time `t` (then the channel seq) instead of trusting append order.
    Selection (QA SL-11, ruling R6): outcome "ok" is re-issued; "taken_unknown" (the bridge took
    it, then the caller timed out) may have run, so it is re-issued AND listed in `uncertain`;
    every other outcome, including "withdrawn" and the older undifferentiated "timeout", did
    not run and is listed in `skipped`. A line without an outcome predates outcomes (journaled
    before sending, fate unknown) and is re-issued, as replay always did.
    `index` in both lists is the entry's line number in the journal.
    """
    order = sorted(enumerate(entries), key=lambda ie: (ie[1]["t"], ie[1].get("seq", 0)))
    keep, skipped, uncertain = [], [], []
    for i, e in order:
        outcome = e.get("outcome", "ok")
        if outcome in ("ok", "taken_unknown"):
            keep.append(e)
            if outcome == "taken_unknown":
                uncertain.append({"index": i, "kind": e["kind"]})
        else:
            skipped.append({"index": i, "kind": e["kind"], "outcome": outcome})
    return keep, skipped, uncertain


def _abort(new: str, err: BaseException) -> BaseException:
    """Stop the replay's own session after a failure; return the exception to raise, naming it.

    WHY (QA SL-9, TA-8): without this the launched engine kept running, holding a session slot
    with no id in the error, until its runtime cap. A failed stop is added to the hint, never
    allowed to replace the original error. Cancellation (ruling R3: SystemExit from fsoctl's
    SIGTERM handler, KeyboardInterrupt) also stops the session, and is returned unchanged so the
    process still exits the way the signal asked.
    """
    from . import launcher  # noqa: PLC0415 - launcher imports channel imports record
    stop_note = ""
    try:
        launcher.stop(new)
    except HarnessError as e:
        stop_note = f"; stopping session {new} also failed ({e.message}): run `fsoctl stop {new}`"
    if not isinstance(err, Exception):
        return err
    if isinstance(err, HarnessError):
        err.details = {**err.details, "id": new}
        err.hint = (err.hint or "") + stop_note
        return err
    return HarnessError(EXIT_USAGE, f"replay stopped: {type(err).__name__}: {err}",
                        "the journal entry may be malformed; see `fsoctl journal`" + stop_note,
                        details={"id": new})


def replay(source_sid: str, speed: float = 1.0, binary: str | None = None,
           renderer: str | None = None) -> dict:
    """Launch a new session like `source_sid` and re-issue its journal; return the new id.

    Returns {"id", "replayed", "skipped", "uncertain", "note"} (see _runnable). On any failure
    after the launch the new session is stopped and the error carries its id in details.
    """
    from . import launcher  # noqa: PLC0415 - launcher imports channel imports record
    entries, skipped, uncertain = _runnable(load(source_sid))
    plan = offsets(entries, speed)
    new = launcher.launch(launch_request_for(session.load(source_sid), binary, renderer))["id"]
    try:
        sess = session.load(new)
        start = time.monotonic()
        for entry, at in zip(entries, plan, strict=True):
            time.sleep(max(0.0, start + at - time.monotonic()))
            _wait_state(sess, entry.get("state"))
            _issue(new, entry["kind"], entry["args"])
    except BaseException as e:   # every failure AND cancellation stops the session (R3); re-raised
        err = _abort(new, e)
        if err is e:
            raise
        raise err from e
    return {"id": new, "replayed": len(entries), "skipped": skipped, "uncertain": uncertain,
            "note": REPLAY_NOTE}


def _cfg_journal(p: argparse.ArgumentParser) -> None:
    """Add the `journal` verb's arguments."""
    p.add_argument("id")


@register("journal", "list the actions recorded for a session", _cfg_journal)
def _cli_journal(args: argparse.Namespace) -> dict:
    """CLI wrapper for load()."""
    if not session.run_dir(session.load(args.id)).exists():
        raise HarnessError(EXIT_NOT_FOUND, f"no session {args.id}", "use `fsoctl list`")
    return {"entries": load(args.id)}


def _cfg_replay(p: argparse.ArgumentParser) -> None:
    """Add the `replay` verb's arguments."""
    p.add_argument("source_id")
    p.add_argument("--speed", type=float, default=1.0, help="2 = twice as fast")
    p.add_argument("--binary", help="replay on another engine build")
    p.add_argument("--renderer", choices=("opengl", "vulkan"))


@register("replay", "launch a new session and re-issue a recorded session's actions", _cfg_replay)
def _cli_replay(args: argparse.Namespace) -> dict:
    """CLI wrapper for replay()."""
    return replay(args.source_id, args.speed, args.binary, args.renderer)
