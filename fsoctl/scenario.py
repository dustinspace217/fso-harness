"""Scenarios: a list of steps run against a session, with named snapshots (used by compare).

Each step is a one-key object:
  {"wait_for": {"kind", "value", "timeout"?}}   game.wait_for
  {"sexp": {"expr", "mode"?}}                     game.sexp
  {"pilot": {pitch, heading, bank, forward, primary, secondary, cm, seconds}}
  {"autopilot": true|false}
  {"time": {"compression"?, "pause"?, "resume"?}}
  {"keys": ["t", "Return", ...]}
  {"sleep": seconds}                              at most 120
  {"snapshot": "label"}                           full game state + screenshot
"""
import time

from . import game, xinput
from .errors import EXIT_USAGE, HarnessError

_KINDS = ("wait_for", "sexp", "pilot", "autopilot", "time", "keys", "sleep", "snapshot")
MAX_SLEEP = 120.0
MAX_STEPS = 1000
# Keys a step must carry: without them run() would stop mid-scenario with a KeyError, after the
# engine was already launched (cross-family XK-4).
_REQUIRED = {"wait_for": ("kind", "value"), "sexp": ("expr",)}


def _bad(i: int, why: str) -> HarnessError:
    """Return the usage error for scenario step i."""
    return HarnessError(EXIT_USAGE, f"scenario step {i}: {why}", f"step kinds: {', '.join(_KINDS)}")


def _check_step(i: int, step) -> None:
    """Raise EXIT_USAGE naming step i if it is not a well-formed one-key step."""
    if not isinstance(step, dict) or len(step) != 1:
        raise _bad(i, "each step is an object with exactly one key")
    kind, value = next(iter(step.items()))
    if kind not in _KINDS:
        raise _bad(i, f"unknown step {kind!r}")
    if kind == "sleep" and not (isinstance(value, int | float) and 0 <= value <= MAX_SLEEP):
        raise _bad(i, f"sleep must be 0..{MAX_SLEEP:.0f} seconds")
    if kind == "snapshot" and not (isinstance(value, str) and value):
        raise _bad(i, "snapshot needs a non-empty label")
    if kind in ("wait_for", "sexp", "pilot", "time") and not isinstance(value, dict):
        raise _bad(i, f"{kind} takes an object")
    if kind == "keys" and not isinstance(value, list):
        raise _bad(i, "keys takes a list of key names")
    missing = [k for k in _REQUIRED.get(kind, ()) if k not in value]
    if missing:
        raise _bad(i, f"{kind} needs {', '.join(missing)}")


def validate(steps) -> list:
    """Return the steps if every one is well formed; raise EXIT_USAGE naming the first bad one.

    Snapshot labels must be unique: run() keys snapshots by label, so a repeated label would
    silently replace the earlier snapshot and compare would diff fewer than were declared.
    """
    if not isinstance(steps, list):
        raise HarnessError(EXIT_USAGE, "a scenario is a list of steps", "e.g. [{\"sleep\": 1}]")
    if len(steps) > MAX_STEPS:
        raise HarnessError(EXIT_USAGE, f"more than {MAX_STEPS} steps", "split the scenario")
    labels: set[str] = set()
    for i, step in enumerate(steps):
        _check_step(i, step)
        label = step.get("snapshot")
        if label is not None:
            if label in labels:
                raise _bad(i, f"snapshot label {label!r} is used twice")
            labels.add(label)
    return steps


def _do(sid: str, kind: str, v) -> None:
    """Run one non-snapshot step."""
    if kind == "wait_for":
        game.wait_for(sid, v["kind"], v["value"], v.get("timeout", 60.0))
    elif kind == "sexp":
        game.sexp(sid, v["expr"], v.get("mode", "eval"))
    elif kind == "pilot":
        game.pilot(sid, **v)
    elif kind == "autopilot":
        game.autopilot(sid, bool(v))
    elif kind == "time":
        game.set_time(sid, v.get("compression"), v.get("pause", False), v.get("resume", False))
    elif kind == "keys":
        xinput.keys(sid, v)
    elif kind == "sleep":
        time.sleep(v)


def run(sid: str, steps: list) -> dict:
    """Run validated steps; return {label: {"state", "screenshot"}} for each snapshot."""
    snaps = {}
    for step in validate(steps):
        kind, v = next(iter(step.items()))
        if kind == "snapshot":
            snaps[v] = {"state": game.state(sid), "screenshot": xinput.screenshot(sid)["path"]}
        else:
            _do(sid, kind, v)
    return snaps

