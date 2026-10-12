"""frame_stats: frame-time percentiles from the engine's own profiler, for a measured window.

Runs a mission with -profile_write_file from a shadow working folder (the engine writes
profiling.csv into its working directory), lets it play for N mission seconds, quits cleanly so the
file is flushed (a killed engine leaves it empty, seen 2026-10-12), and summarises the frame times.

profiling.csv lines are "<end timestamp>;<frame duration>", both in nanoseconds
(MainFrameTimer.cpp; a 2026-10-12 software-GL run had a 42.7 ms median frame).

The window (QA SF-10): the bridge's `clock` command is read when measuring starts and ends; it
returns gameplay_frames, a count of normal GS_STATE_GAME_PLAY frames, and the window is CSV rows
[start count, end count). The CSV covers gameplay frame flips only (fs2open-3424 freespace.cpp:6232
and :5572 begin and end the MainFrame trace on entering and leaving GS_STATE_GAME_PLAY; :3719-3720
split it per flip), so the two count the same frames. WHY not a clock: ba.getFrametimeOverall
counts from engine start (freespace.cpp:4642), and one probe (2026-10-12) read mission time
1.8 s just after gameplay began while the profiler's rows start at gameplay, so either clock
cuts the window in the wrong place. The counts can differ by a frame or two at the edges
(which frame a command lands in), so the result still says "approximate" and carries both
clock readings.
The whole run is summarised under whole_run.
"""
import argparse
import math
import os
import secrets
from pathlib import Path

from . import game, launcher, paths, session, shadowdir
from .cli import register
from .errors import EXIT_ENGINE, EXIT_USAGE, HarnessError
from .launcher import LaunchRequest

_NS_PER_MS = 1_000_000
WINDOW_METHOD = "gameplay frame count (approximate to a frame or two)"


def _percentile(sorted_ms: list[float], pct: float) -> float:
    """Nearest-rank percentile of an ascending list."""
    rank = max(1, math.ceil(pct / 100 * len(sorted_ms)))
    return sorted_ms[rank - 1]


def durations(csv_text: str) -> list[int]:
    """Return every frame duration in profiling.csv, in nanoseconds, in file order."""
    out = []
    for line in csv_text.splitlines():
        parts = line.strip().split(";")
        if len(parts) == 2 and parts[1].isdigit():
            out.append(int(parts[1]))
    return out


def summarise(frames_ns: list[int]) -> dict:
    """Return frames, mean, p50, p95, p99, max (milliseconds) of the given durations."""
    if not frames_ns:
        raise HarnessError(EXIT_ENGINE, "no frames recorded",
                           "the engine must exit cleanly to flush profiling.csv")
    ms = sorted(d / _NS_PER_MS for d in frames_ns)
    return {"frames": len(ms), "mean_ms": round(sum(ms) / len(ms), 3),
            "p50_ms": round(_percentile(ms, 50), 3), "p95_ms": round(_percentile(ms, 95), 3),
            "p99_ms": round(_percentile(ms, 99), 3), "max_ms": round(ms[-1], 3)}


def parse(csv_text: str, skip: int = 0) -> dict:
    """Summarise profiling.csv: frames, mean, p50, p95, p99, max (milliseconds).

    skip drops that many frames from the start (the first ones include loading).
    """
    return summarise(durations(csv_text)[skip:])


def _windowed(sid: str, frames_ns: list[int], c0: dict, c1: dict) -> dict:
    """Cut the rows between two clock readings' gameplay_frames and summarise them; raise
    EXIT_ENGINE (with the session id) when the counts are missing or run past the CSV."""
    f0, f1 = c0.get("gameplay_frames"), c1.get("gameplay_frames")
    if not isinstance(f0, int) or not isinstance(f1, int):
        raise HarnessError(EXIT_ENGINE, "the bridge clock gave no gameplay_frames count",
                           "the installed bridge may be older than this harness; relaunch so "
                           "it is refreshed", {"id": sid})
    if f1 > len(frames_ns):
        raise HarnessError(EXIT_ENGINE,
                           f"the measured window (gameplay frames {f0}-{f1}) ends after the last "
                           f"recorded frame ({len(frames_ns)} rows)",
                           "the engine may not have exited cleanly; see whole_run in a rerun or "
                           "report this", {"id": sid})
    return summarise(frames_ns[f0:f1])


def frame_stats(mods: list[str], mission: str, seconds: float = 30.0, binary: str | None = None,
                renderer: str = "opengl") -> dict:
    """Play `mission` for `seconds` of mission time and return frame-time statistics.

    The top-level stats cover only the measured window; whole_run summarises every frame.
    exit_info / clean_exit say whether the engine quit cleanly (an unclean exit can cut the CSV).
    """
    exe = binary or os.environ.get("FSO_BIN")
    if not exe:
        raise HarnessError(EXIT_USAGE, "no engine binary", "pass --binary or export FSO_BIN")
    shadow = shadowdir.make(paths.fs2path(),
                            paths.runtime_root() / "shadows" / secrets.token_hex(4))
    req = LaunchRequest(binary=exe, mods=mods, mission=mission, renderer=renderer,
                        pilot_mode="create", skip_movies=True, dismiss_popups=True,
                        skip_command_briefing=True, skip_briefing=True, cwd=str(shadow),
                        extra_args=["-profile_write_file"])
    sid = launcher.launch(req)["id"]
    try:
        game.wait_for(sid, "state", "GS_STATE_GAME_PLAY", timeout=240)
        c0 = game.call(sid, "clock")
        if c0.get("mission_time") is None:
            raise HarnessError(EXIT_ENGINE, "the bridge clock gave no mission_time",
                               "the mission must be running while frame_stats measures",
                               {"id": sid})
        game.wait_for(sid, "mission_time", c0["mission_time"] + seconds,
                      timeout=seconds * 10 + 60)
        c1 = game.call(sid, "clock")
        launcher.quit(sid)
    finally:
        launcher.stop(sid)
    csv = Path(shadow) / "profiling.csv"
    if not csv.exists():
        raise HarnessError(EXIT_ENGINE, "profiling.csv was not written", "see `fsoctl log`",
                           {"id": sid})
    frames_ns = durations(csv.read_text())
    info = session.load(sid).exit_info
    return {"id": sid, "renderer": renderer, **_windowed(sid, frames_ns, c0, c1),
            "window_method": WINDOW_METHOD, "window": {"start": c0, "end": c1},
            "whole_run": summarise(frames_ns), "exit_info": info,
            "clean_exit": bool(info) and info.get("returncode") == 0, "csv": str(csv)}


def _cfg(p: argparse.ArgumentParser) -> None:
    """Add the `frame_stats` verb's arguments."""
    p.add_argument("mission")
    p.add_argument("--mods", default="")
    p.add_argument("--seconds", type=float, default=30.0, help="mission seconds to measure")
    p.add_argument("--binary")
    p.add_argument("--vulkan", action="store_true")


@register("frame_stats", "frame-time percentiles for a mission (engine profiler)", _cfg)
def _cli(args: argparse.Namespace) -> dict:
    """CLI wrapper for frame_stats()."""
    return frame_stats([m for m in args.mods.split(",") if m], args.mission, args.seconds,
                       args.binary, "vulkan" if args.vulkan else "opengl")
