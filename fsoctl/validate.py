"""validate: load a mod (and optionally a mission) and report what the engine complained about.

Launches with every startup stage handled (new pilot, popups answered, briefings skipped when a
mission is given), waits for the main menu or for gameplay, collects the engine's own warning and
error counts, the problem lines in the log, and the popups that came up, then stops the session.
An engine that dies while loading is a finding, not a harness failure: the result says aborted and
why (abort_reason).

Release builds compile Warning() out and write no fs2_open.log, so validating with a debug engine
(FSO_BIN_DEBUG) shows far more; the result says which kind of build ran.
"""
import argparse
import json
import os

from . import channel, game, launcher, logs, paths, session
from .cli import register
from .errors import EXIT_ENGINE, EXIT_TIMEOUT, EXIT_USAGE, HarnessError
from .launcher import LaunchRequest

RELEASE_NOTE = ("release build: Warning() is compiled out and there is no fs2_open.log; "
                "validate with a debug engine (FSO_BIN_DEBUG) for the full picture")


def _binary(binary: str | None) -> str:
    """Return the engine to validate with: given, else $FSO_BIN_DEBUG, else $FSO_BIN."""
    exe = binary or os.environ.get("FSO_BIN_DEBUG") or os.environ.get("FSO_BIN")
    if not exe:
        raise HarnessError(EXIT_USAGE, "no engine binary", "pass --binary or export FSO_BIN_DEBUG")
    return exe


def _findings(sid: str) -> dict:
    """Return problems from the log and the popups the bridge answered."""
    sess = session.load(sid)
    out: dict = {"problems": [], "problems_total": 0, "popups": []}
    try:
        found = logs.read(sid, tail=1)
        out["problems"], out["problems_total"] = found["problems"], found["problems_total"]
    except HarnessError as e:
        out["log_error"] = e.message
    popups = channel.Channel(sess).dir / "popups.txt"
    if popups.exists():
        out["popups"] = [json.loads(x) for x in popups.read_text().splitlines() if x.strip()]
    out["build"] = "debug" if logs.sources(sess)["fso"].exists() else "release"
    return out


def _check_mods(mods: list[str]) -> None:
    """Refuse mod folders that do not exist under FS2PATH (EXIT_USAGE).

    WHY: the engine skips a -mod folder it cannot find, so a misspelled mod "validated" clean
    with 0 warnings and 0 errors while nothing of it was loaded (QA SF-7).
    """
    fs2 = paths.fs2path()
    missing = [m for m in mods if not (fs2 / m).is_dir()]
    if missing:
        raise HarnessError(EXIT_USAGE, f"mod folder(s) not found in {fs2}: {missing}",
                           "check the spelling; --mods takes folder names inside FS2PATH")


def _abort_reason(sid: str, e: HarnessError) -> dict:
    """Describe why validation stopped early: engine_died or timeout, the message, last state.

    The kind comes from the error code alone (EXIT_ENGINE = died, EXIT_TIMEOUT = timeout), never
    from a liveness read: launch stops the session before it raises its readiness timeout, so the
    engine is gone by the time anyone looks, and a timeout would read as a death (CR2-4 / Y-8).
    """
    sess = session.load(sid)
    died = e.code == EXIT_ENGINE
    hb = channel.Channel(sess).heartbeat() or {}
    return {"kind": "engine_died" if died else "timeout", "message": e.message,
            "last_state": hb.get("state")}


def validate(mods: list[str], mission: str | None = None, binary: str | None = None,
             timeout: float = 240) -> dict:
    """Load the mods (and mission) and return counts, problems, popups, and whether it aborted.

    aborted=True comes with abort_reason {"kind": "engine_died" | "timeout", "message",
    "last_state"}. On a release build the warning/error counts are null (Warning() is compiled
    out, so a 0 there would claim a clean load nobody checked).
    """
    exe = _binary(binary)
    _check_mods(mods)
    req = LaunchRequest(binary=exe, mods=mods, mission=mission, pilot_mode="create",
                        pilot_name="Validate", skip_movies=True, dismiss_popups=True,
                        skip_command_briefing=bool(mission), skip_briefing=bool(mission),
                        ready_timeout=timeout)
    want = "GS_STATE_GAME_PLAY" if mission else "GS_STATE_MAIN_MENU"
    sid, reason, counts = None, None, {"warnings": None, "errors": None}
    try:
        sid = launcher.launch(req)["id"]
        game.wait_for(sid, "state", want, timeout=timeout)
        counts = game.call(sid, "counts")
    except HarnessError as e:
        sid = sid or e.details.get("id")
        if e.code not in (EXIT_ENGINE, EXIT_TIMEOUT) or not sid:
            raise
        reason = _abort_reason(sid, e)   # the engine died or never got there: the finding
    finally:
        if sid:
            launcher.stop(sid)
    if sid is None:   # unreachable: every path above sets sid or re-raises
        raise HarnessError(EXIT_ENGINE, "validate lost its session id", "report this as a bug")
    sess = session.load(sid)
    result = {"id": sid, "aborted": reason is not None, "abort_reason": reason,
              "reached": None if reason else want, **counts, **_findings(sid),
              "exit_info": sess.exit_info}
    if result["build"] == "release":
        result.update(warnings=None, errors=None, note=RELEASE_NOTE)
    return result


def _cfg_validate(p: argparse.ArgumentParser) -> None:
    """Add the `validate` verb's arguments."""
    p.add_argument("--mods", default="", help="comma-separated mod folders")
    p.add_argument("--mission", help="also load this mission into gameplay")
    p.add_argument("--binary", help="engine (default $FSO_BIN_DEBUG, then $FSO_BIN)")
    p.add_argument("--timeout", type=float, default=240)


@register("validate", "load mods/mission, report warnings, errors and popups", _cfg_validate)
def _cli_validate(args: argparse.Namespace) -> dict:
    """CLI wrapper for validate()."""
    return validate([m for m in args.mods.split(",") if m], args.mission, args.binary,
                    args.timeout)

