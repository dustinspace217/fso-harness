"""Probe 5 (cross-platform Plan A, Task 8): does the FSO 26.0.1 release start on this OS?

Starts the release's FastDebug engine (the build that writes a log) with -window, sound off and
no game data, for up to START_WAIT_S seconds, and records:

    exit_code        the engine's exit code, or None if it was still running at the deadline
    seconds          how long it ran before exiting or being stopped
    reached_gl_init  its log has "OpenGL init is complete!" (graphics came up)
    udp_ports        every UDP port the engine (or a child) held while watched. The engine opens
                     its game port after graphics init, so with no data, where it stops at a
                     missing bitmap inside graphics init, this is expected to stay empty.
    log_tail         the last LOG_TAIL_LINES lines of fs2_open.log
    argv, notes

FSO_PREFERENCES_PATH (read by the engine, osapi.cpp) points its preferences, and so its log,
at a folder of this run's own. Sound is off because CI runners have no audio device and probe
1 saw the engine stop at OpenAL init; audio is probe 6's question. Exit 0 whatever is found.

    python fso_start.py --out DIR --engine PATH
"""
import argparse
import contextlib
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

START_WAIT_S = 30
POLL_S = 0.5
LOG_TAIL_LINES = 20
GL_INIT_LINE = "OpenGL init is complete"
ENGINE_ARGS = ("-window", "-nosound", "-nomusic")


def os_name() -> str:
    """Return "linux", "windows" or "macos" for the result file name."""
    return {"Linux": "linux", "Windows": "windows", "Darwin": "macos"}.get(platform.system(),
                                                                          platform.system())


def read_log(prefs: Path) -> list:
    """Return the lines of the engine's log under prefs ([] when it has not written one)."""
    log = prefs / "data" / "fs2_open.log"
    return log.read_text(errors="replace").splitlines() if log.exists() else []


def udp_ports(proc) -> list:
    """Return the local UDP ports held by proc and its children (psutil.Process)."""
    import psutil  # noqa: PLC0415 - a probe-only package; the unit tier does not install it
    ports = []
    for p in [proc, *proc.children(recursive=True)]:
        try:
            ports += [c.laddr.port for c in p.net_connections(kind="udp") if c.laddr]
        except psutil.Error:      # the process ended between listing and asking
            continue
    return sorted(set(ports))


def watch(proc, prefs: Path) -> dict:
    """Poll the running engine until it exits or START_WAIT_S pass; return what was seen."""
    import psutil  # noqa: PLC0415 - see udp_ports
    seen = {"reached_gl_init": False, "ports": []}
    ps = psutil.Process(proc.pid)
    start = time.monotonic()
    while time.monotonic() - start < START_WAIT_S and proc.poll() is None:   # bounded
        seen["reached_gl_init"] = any(GL_INIT_LINE in ln for ln in read_log(prefs))
        with contextlib.suppress(psutil.Error):   # it exited mid-poll; proc.poll() ends the loop
            seen["ports"] = sorted(set(seen["ports"]) | set(udp_ports(ps)))
        time.sleep(POLL_S)
    seen["seconds"] = round(time.monotonic() - start, 1)
    return seen


def stop(proc) -> None:
    """End the engine and anything it started (an AppImage may run the engine as a child, and
    the engine's error boxes are child processes on Linux)."""
    import psutil  # noqa: PLC0415 - see udp_ports
    try:
        family = [*psutil.Process(proc.pid).children(recursive=True)]
    except psutil.Error:
        family = []
    for p in family:
        try:
            p.kill()
        except psutil.Error:
            continue
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def run_engine(argv: list, env: dict, out: Path, r: dict) -> None:
    """Start the engine in out/game, watch it, stop it; fill exit_code, seconds,
    reached_gl_init and udp_ports. exit_code is read before stop(): a killed engine must not
    look like it exited by itself."""
    with open(out / "engine-stdout.txt", "w") as console:   # the child keeps its own handle
        proc = subprocess.Popen(argv, cwd=out / "game", env=env, stdout=console,
                                stderr=subprocess.STDOUT)
    try:
        seen = watch(proc, out / "prefs")
    finally:
        r["exit_code"] = proc.poll()
        stop(proc)
    r["seconds"], r["reached_gl_init"] = seen["seconds"], seen["reached_gl_init"]
    r["udp_ports"] = seen["ports"]


def main() -> int:
    """Start the engine once and write probe-fso-start-<os>.json; always exit 0."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--engine", type=Path, required=True)
    args = ap.parse_args()
    out = args.out.resolve()
    prefs, game = out / "prefs", out / "game"     # game: an empty folder, no data by design
    prefs.mkdir(parents=True, exist_ok=True)
    game.mkdir(parents=True, exist_ok=True)
    argv = [str(args.engine.resolve()), *ENGINE_ARGS]
    # APPIMAGE_EXTRACT_AND_RUN: the Linux release is an AppImage; this runs it without FUSE.
    env = dict(os.environ, FSO_PREFERENCES_PATH=str(prefs), APPIMAGE_EXTRACT_AND_RUN="1")
    r = {"os": os_name(), "argv": [Path(argv[0]).name, *ENGINE_ARGS], "exit_code": None,
         "seconds": None, "reached_gl_init": False, "udp_ports": [], "log_tail": [],
         "notes": []}
    # A log from an earlier run with the same --out must not pass as this run's (QA TA-4).
    (prefs / "data" / "fs2_open.log").unlink(missing_ok=True)
    try:
        run_engine(argv, env, out, r)
    except OSError as e:                          # the engine could not be started at all
        r["notes"].append(f"{type(e).__name__}: {e}")
    lines = read_log(prefs)
    r["reached_gl_init"] = r["reached_gl_init"] or any(GL_INIT_LINE in ln for ln in lines)
    r["log_tail"] = [ln.rstrip() for ln in lines[-LOG_TAIL_LINES:]]
    if not lines:
        r["notes"].append("the engine wrote no log under FSO_PREFERENCES_PATH")
    (out / f"probe-fso-start-{os_name()}.json").write_text(json.dumps(r, indent=1))
    print(json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
