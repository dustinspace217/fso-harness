"""compare / renderer_compare: run one scenario twice and diff the snapshots.

compare runs two engine builds (e.g. master vs a branch); renderer_compare runs one build on OpenGL
then Vulkan. The runs happen one after the other, never at the same time, so both see the same
machine load. Each snapshot is diffed two ways: game state (ships, goals, events; positions and
hull with tolerances) and the screenshots (ImageMagick's mean absolute error, 0..1).

Honest limit: the engine has no fixed timestep, so two runs of the SAME build drift a little; use
the tolerances, and treat goal/event/ship-list differences as the strong signal.
"""
import argparse
import json
import math
import os
import re
import subprocess
from pathlib import Path

from . import launcher, scenario, session
from .cli import register
from .errors import EXIT_LAUNCH, EXIT_USAGE, HarnessError
from .launcher import LaunchRequest

_MAE = re.compile(r"\(([0-9.eE+-]+)\)")   # "123.4 (0.00188)": the normalised value
_MAGICK_TIMEOUT = 60
# Set by the bridge's state reply when the ship list hit its cap (QA SF-15). A truncated list
# cannot prove two runs had the same ships, so it blocks an "identical" verdict.
SHIPS_TRUNCATED_KEY = "ships_truncated"
# Stage switches every comparison run uses, so both runs start the mission the same way.
_RUN_DEFAULTS = {"pilot_mode": "create", "skip_movies": True, "dismiss_popups": True,
                 "skip_command_briefing": True, "skip_briefing": True}


def _by_name(items: list[dict]) -> dict[str, dict]:
    """Index a list of {"name": ...} records by name."""
    return {x["name"]: x for x in items}


def _ship_diffs(name: str, a: dict, b: dict, pos_tol: float, hull_tol: float) -> list[dict]:
    """Return position/hull differences for one ship present in both snapshots.

    A field the bridge could not read is null (QA SF-15): null against a number is a difference,
    null against null is not (nothing can be said either way).
    """
    out = []
    pa, pb = a.get("pos"), b.get("pos")
    if (pa is None) != (pb is None):
        out.append({"kind": "position", "name": name, "a": pa, "b": pb})
    elif pa is not None:
        delta = math.dist(pa, pb)
        if delta > pos_tol:
            out.append({"kind": "position", "name": name, "delta": round(delta, 3)})
    ha, hb = a.get("hull_pct"), b.get("hull_pct")
    if (ha is None) != (hb is None):
        out.append({"kind": "hull", "name": name, "a": ha, "b": hb})
    elif ha is not None and abs(ha - hb) > hull_tol:
        out.append({"kind": "hull", "name": name, "delta": round(abs(ha - hb), 3)})
    return out


def diff_states(a: dict, b: dict, pos_tol: float = 1.0, hull_tol: float = 1.0) -> list[dict]:
    """Return the differences between two state snapshots that exceed the tolerances.

    Goals and events present in only one run are differences too ({"kind": "only_in_a" |
    "only_in_b", "of": "goal" | "event", "name"}): a goal that exists in one build only is
    exactly the kind of change a comparison is for.
    """
    diffs: list[dict] = []
    if a.get("state") != b.get("state"):
        diffs.append({"kind": "state", "a": a.get("state"), "b": b.get("state")})
    sa, sb = _by_name(a.get("ships", [])), _by_name(b.get("ships", []))
    for name in sorted(sa.keys() - sb.keys()):
        diffs.append({"kind": "missing_in_b", "name": name})
    for name in sorted(sb.keys() - sa.keys()):
        diffs.append({"kind": "missing_in_a", "name": name})
    for name in sorted(sa.keys() & sb.keys()):
        diffs += _ship_diffs(name, sa[name], sb[name], pos_tol, hull_tol)
    for key, kind in (("goals", "goal"), ("events", "event")):
        ga, gb = _by_name(a.get(key, [])), _by_name(b.get(key, []))
        for name in sorted(ga.keys() - gb.keys()):
            diffs.append({"kind": "only_in_a", "of": kind, "name": name})
        for name in sorted(gb.keys() - ga.keys()):
            diffs.append({"kind": "only_in_b", "of": kind, "name": name})
        for name in sorted(ga.keys() & gb.keys()):
            if ga[name]["status"] != gb[name]["status"]:
                diffs.append({"kind": kind, "name": name, "a": ga[name]["status"],
                              "b": gb[name]["status"]})
    return diffs


def parse_mae(text: str) -> float:
    """Return the normalised (0..1) mean absolute error from `magick compare -metric MAE`."""
    m = _MAE.search(text)
    if not m:
        raise HarnessError(EXIT_LAUNCH, f"could not read the image difference: {text[:120]!r}",
                           "is ImageMagick installed?")
    return float(m.group(1))


def diff_images(a_path: str, b_path: str, out_path: str) -> dict:
    """Compare two screenshots; return {"mean_abs_diff" (0..1), "diff_image"}."""
    r = subprocess.run(["magick", "compare", "-metric", "MAE", a_path, b_path, out_path],
                       capture_output=True, text=True, timeout=_MAGICK_TIMEOUT, check=False)
    if r.returncode not in (0, 1):   # 0 = same, 1 = different, 2 = error
        raise HarnessError(EXIT_LAUNCH, f"magick compare failed: {r.stderr.strip()[:200]}",
                           "screenshots must have the same size")
    return {"mean_abs_diff": parse_mae(r.stderr), "diff_image": out_path}


def _run_once(binary: str, launch: dict, steps: list, renderer: str) -> dict:
    """Launch, run the scenario, stop; return {"id", "renderer", "snapshots"}."""
    fields = {**_RUN_DEFAULTS, **launch, "binary": binary, "renderer": renderer}
    try:
        req = LaunchRequest(**fields)
    except TypeError as e:
        raise HarnessError(EXIT_USAGE, f"bad launch field: {e}", "use LaunchRequest fields") from e
    r = launcher.launch(req)
    try:
        snaps = scenario.run(r["id"], steps)
    finally:
        launcher.stop(r["id"])
    return {"id": r["id"], "renderer": r["renderer"], "snapshots": snaps}


# metres of position, hull percentage points, image mean absolute error (0..1)
_DEFAULT_TOL = {"pos": 1.0, "hull": 1.0, "image": 0.01}


def _truncated(a: dict, b: dict) -> bool | None:
    """True if either snapshot's ship list was cut, False if both say complete, None if either
    lacks the flag (a bridge older than SF-15): unknown, which never counts as complete."""
    flags = [a.get(SHIPS_TRUNCATED_KEY), b.get(SHIPS_TRUNCATED_KEY)]
    if True in flags:
        return True
    return None if None in flags else False


def _diff_runs(a: dict, b: dict, tol: dict) -> dict:
    """Diff every snapshot label both runs produced; tol = {"pos": m, "hull": pct, ...}."""
    out = {}
    for label in sorted(a["snapshots"].keys() & b["snapshots"].keys()):
        sa, sb = a["snapshots"][label], b["snapshots"][label]
        diff_png = str(Path(session.run_dir(session.load(b["id"]))) / f"diff-{label}.png")
        out[label] = {"state_diffs": diff_states(sa["state"], sb["state"], tol["pos"],
                                                 tol["hull"]),
                      "ships_truncated": _truncated(sa["state"], sb["state"]),
                      "image": diff_images(sa["screenshot"], sb["screenshot"], diff_png)}
    return out


def _verdict(a: dict, b: dict, snaps: dict, tol: dict) -> dict:
    """Decide whether two runs match, and say why not.

    WHY every condition: each one is a way the old all()-over-shared-snapshots check returned
    "identical" without comparing anything (QA SF-12, TA-9): no snapshots at all, a label only
    one run took, a ship list cut at the bridge's cap, and screenshots that were never judged.
    states_identical keeps the state-only answer for callers who expect render differences.
    """
    only_a = sorted(a["snapshots"].keys() - b["snapshots"].keys())
    only_b = sorted(b["snapshots"].keys() - a["snapshots"].keys())
    why: list[str] = []
    if not snaps:
        why.append("no snapshot was taken by both runs; add a snapshot step")
    if only_a or only_b:
        why.append(f"snapshot labels differ (only in a: {only_a}, only in b: {only_b})")
    for label, s in snaps.items():
        if s["state_diffs"]:
            why.append(f"{label}: {len(s['state_diffs'])} state difference(s)")
        if s["ships_truncated"] is True:
            why.append(f"{label}: ship list truncated by the bridge, ships not fully compared")
        elif s["ships_truncated"] is None:
            why.append(f"{label}: unknown whether the ship list is complete (no "
                       f"{SHIPS_TRUNCATED_KEY} in the state); update the bridge")
        if s["image"]["mean_abs_diff"] > tol["image"]:
            why.append(f"{label}: image differs by {s['image']['mean_abs_diff']:.4f} "
                       f"(tolerance {tol['image']})")
    states_same = bool(snaps) and not (only_a or only_b) and all(
        not s["state_diffs"] and s["ships_truncated"] is False for s in snaps.values())
    return {"identical_within_tolerance": not why, "states_identical": states_same,
            "not_identical_because": why, "only_in_a": only_a, "only_in_b": only_b}


def compare(binary_a: str, binary_b: str, launch: dict, steps: list,
            tol: dict | None = None) -> dict:
    """Run the scenario on build A, then build B; return per-snapshot state and image diffs.

    tol: {"pos": metres, "hull": percentage points, "image": mean abs error 0..1};
    defaults 1.0 / 1.0 / 0.01.
    """
    tol = {**_DEFAULT_TOL, **(tol or {})}
    scenario.validate(steps)
    a = _run_once(binary_a, launch, steps, launch.get("renderer", "opengl"))
    b = _run_once(binary_b, launch, steps, launch.get("renderer", "opengl"))
    snaps = _diff_runs(a, b, tol)
    return {"a": a["id"], "b": b["id"], "snapshots": snaps, **_verdict(a, b, snaps, tol)}


def renderer_compare(binary: str, launch: dict, steps: list, tol: dict | None = None) -> dict:
    """Run the scenario on OpenGL, then Vulkan (needs a Vulkan driver; VK_ICD_FILENAMES is the
    caller's choice); return per-snapshot diffs and which renderer each run really used."""
    tol = {**_DEFAULT_TOL, **(tol or {})}
    scenario.validate(steps)
    a = _run_once(binary, launch, steps, "opengl")
    b = _run_once(binary, launch, steps, "vulkan")
    snaps = _diff_runs(a, b, tol)
    return {"opengl": a["id"], "vulkan": b["id"], "renderers": [a["renderer"], b["renderer"]],
            "snapshots": snaps}


def _json_arg(text: str, what: str):
    """Parse a JSON command-line argument or raise EXIT_USAGE."""
    try:
        return json.loads(text)
    except ValueError as e:
        raise HarnessError(EXIT_USAGE, f"--{what} is not valid JSON: {e}",
                           "check the quoting") from e


def _load_steps(path: str) -> list:
    """Read a scenario JSON file."""
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise HarnessError(EXIT_USAGE, f"cannot read scenario {path}: {e}", "a JSON list") from e


def _cfg_compare(p: argparse.ArgumentParser) -> None:
    """Add the `compare` verb's arguments."""
    p.add_argument("--a", required=True, help="engine build A")
    p.add_argument("--b", required=True, help="engine build B")
    p.add_argument("--launch", default="{}", help='JSON launch fields, e.g. {"mission": "x.fs2"}')
    p.add_argument("--steps", required=True, help="scenario JSON file")
    p.add_argument("--pos-tol", type=float, default=1.0)
    p.add_argument("--hull-tol", type=float, default=1.0)
    p.add_argument("--image-tol", type=float, default=0.01,
                   help="largest screenshot mean absolute error (0..1) still called identical")


@register("compare", "run one scenario on two builds and diff the snapshots", _cfg_compare)
def _cli_compare(args: argparse.Namespace) -> dict:
    """CLI wrapper for compare()."""
    return compare(args.a, args.b, _json_arg(args.launch, "launch"), _load_steps(args.steps),
                   {"pos": args.pos_tol, "hull": args.hull_tol, "image": args.image_tol})


def _cfg_rc(p: argparse.ArgumentParser) -> None:
    """Add the `renderer_compare` verb's arguments."""
    p.add_argument("--binary", default=os.environ.get("FSO_BIN"))
    p.add_argument("--launch", default="{}")
    p.add_argument("--steps", required=True)


@register("renderer_compare", "run one scenario on OpenGL and Vulkan and diff them", _cfg_rc)
def _cli_rc(args: argparse.Namespace) -> dict:
    """CLI wrapper for renderer_compare()."""
    if not args.binary:
        raise HarnessError(EXIT_USAGE, "no engine binary", "pass --binary or export FSO_BIN")
    return renderer_compare(args.binary, _json_arg(args.launch, "launch"),
                            _load_steps(args.steps))
