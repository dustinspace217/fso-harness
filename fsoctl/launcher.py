"""launch / status / stop / list / cleanup.

A launch: create the session, install the bridge, hand the bridge its startup switches, start the
runner in a named scope, wait (bounded) for the bridge heartbeat (game) or the window (qtFRED),
then check which renderer really came up and fail loudly on a Vulkan request that did not get
Vulkan.
"""
import argparse
import json
import os
import re
import shutil
import signal
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import bridge_install, channel, paths, renderer, scope, session, xwin
from .cli import register
from .errors import (EXIT_ENGINE, EXIT_LAUNCH, EXIT_NOT_FOUND, EXIT_REFUSED, EXIT_TIMEOUT,
                     EXIT_USAGE, HarnessError)

DEFAULT_MAX_SESSIONS = 3
STALE_SEC = 5.0         # contract C3: a heartbeat older than this is stale
_LOCK_WAIT = 60.0       # seconds to wait for launch.lock (held for one install + scope start)
_STARTING_GRACE = 120.0  # cleanup leaves a "starting" record alone this long (see _removable)
# Extra engine arguments the caller may not pass (contract C10), see _check_extra_args.
_HARNESS_ONLY_ARGS = ("-portable_mode", "-mod", "-start_mission", "-pilot")
# Flags that make the engine write a file into its working folder (fopen with a relative name):
# -output_* (sexps.html, scripting.*), -profile_write_file (profiling.csv), -get_flags
# (flags.lch, cmdline.cpp:1517), -controlconfig_tbl (controlconfigdefaults.tbl,
# controlsconfigcommon.cpp:2021). Lower case: the checks compare arg.lower().
_OUTPUT_ARGS = ("-output_", "-profile_write_file", "-get_flags", "-controlconfig_tbl")
_SIDE_FOLDERS = ("shadows", "fred-missions")
_SIDE_MIN_AGE = 3600.0   # side folders are never removed younger than this (cleanup, R4)
# How long stop() waits for the runner to finish after asking it to: the runner gives the engine
# up to runner._REAP_TIMEOUT (20 s) to quit, then ends Xvfb; 5 s on top for that and its record.
_RUNNER_EXIT_WAIT = 25.0
# Seconds between the runner's own deadline (runtime_sec) and the scope's RuntimeMaxSec: covers
# the runner's worst-case reap (20 s polite + 20 s after SIGKILL) and ending Xvfb.
RUNTIME_BACKSTOP_SEC = 60
_RUNNER_POLL = 0.1
_SIDE_NAME = re.compile(r"^[0-9a-f]{8}$")   # token_hex(4), how fred/docs/frame_stats name them
_SWITCH_NAMES = ("skip_movies", "pilot_mode", "pilot_name", "dismiss_popups",
                 "skip_command_briefing", "skip_briefing", "noninteractive")


@dataclass
class LaunchRequest:
    """Everything `launch` needs, as one value (CLI, MCP and tests all build these)."""
    binary: str
    mission: str | None = None
    mods: list[str] = field(default_factory=list)
    renderer: str = "opengl"
    width: int = 1280
    height: int = 720
    skip_movies: bool = False
    pilot_mode: str = "chooser"          # "chooser" | "existing" | "create"
    pilot_name: str = "HarnessPilot"
    pilot_source: str | None = None      # a data/players folder to copy the pilot from
    dismiss_popups: bool = False
    skip_command_briefing: bool = False
    skip_briefing: bool = False
    noninteractive: bool = True
    allow_eval: bool = False
    extra_args: list[str] = field(default_factory=list)
    kind: str = "game"                   # "game" | "qtfred"
    runtime_sec: int = 1800
    ready_timeout: float = 240.0
    cwd: str | None = None               # working folder; None = FS2PATH (see shadowdir)


def _qtfred_mission_path(sess: session.Session) -> str:
    """Resolve a mission name to the absolute path qtFRED needs (it crashes on relative paths)."""
    if not sess.mission or sess.mission.startswith("/"):
        return sess.mission or ""
    fs2 = Path(sess.fs2path)
    for mod in sess.mods:
        cand = fs2 / mod / "data" / "missions" / sess.mission
        if cand.exists():
            return str(cand)
    return str(fs2 / "data" / "missions" / sess.mission)


def _qtfred_argv(sess: session.Session) -> list[str]:
    """Return qtFRED's command line. -noninteractive keeps warning boxes from blocking startup."""
    argv = [sess.binary]
    if sess.mods:
        argv += ["-mod", ",".join(sess.mods)]
    if sess.mission:
        argv += ["-start_mission", _qtfred_mission_path(sess)]
    return [*argv, "-nosound", "-nomovies", "-noninteractive", *sess.extra_args]


def build_argv(sess: session.Session) -> list[str]:
    """Return the engine or qtFRED command line for a session."""
    if sess.kind == "qtfred":
        return _qtfred_argv(sess)
    sw = sess.switches
    argv = [sess.binary, "-mod", ",".join([paths.BRIDGE_MOD, *sess.mods]),
            "-window", "-res", f"{sess.width}x{sess.height}", "-nosound", "-no_unfocused_pause"]
    if sw.get("noninteractive", True):
        argv.append("-noninteractive")
    if sw.get("skip_movies"):
        argv.append("-nomovies")
    if sw.get("pilot_mode") == "existing":
        argv += ["-pilot", sw.get("pilot_name", "HarnessPilot")]
    if sess.mission:
        argv += ["-start_mission", sess.mission]
    if sess.renderer == "vulkan":
        argv.append("-vulkan")
    return argv + sess.extra_args


# unit -> (monotonic time of the last systemd check, scope was active). Lives for one fsoctl
# process only (fsoctl is a short-lived command); it exists for polling loops such as
# Channel.send's 50 ms liveness check. Capped so a long-lived importer cannot grow it unbounded.
_scope_seen: dict[str, tuple[float, bool]] = {}
_SCOPE_RECHECK = 1.0      # seconds between systemd checks of one scope
_SCOPE_SEEN_MAX = 256


def _scope_active_cached(unit: str) -> bool:
    """Return scope.is_active(unit), asking systemd at most once per _SCOPE_RECHECK seconds."""
    now = time.monotonic()
    seen = _scope_seen.get(unit)
    if seen and now - seen[0] < _SCOPE_RECHECK:
        return seen[1]
    if len(_scope_seen) >= _SCOPE_SEEN_MAX:
        _scope_seen.clear()
    active = scope.is_active(unit)
    _scope_seen[unit] = (now, active)
    return active


def alive(sess: session.Session) -> bool:
    """Return True while the session's engine process exists and its scope is active.

    The pid check runs first and spawns nothing; systemd is asked only when the pid exists, and
    at most once a second per session (QA CR-13: polling loops call this every 50 ms, and each
    call used to spawn `systemctl`). A dead engine pid answers False at once, uncached.
    """
    if not sess.engine_pid:
        return False
    try:
        os.kill(sess.engine_pid, 0)
    except OSError:
        return False
    return _scope_active_cached(sess.scope)


def _is_live(s: session.Session) -> bool:
    """True while a session is alive, or still starting inside an active scope."""
    if s.status == "starting":
        return scope.is_active(s.scope)
    return alive(s)


def _alive_or_starting(sid: str) -> bool:
    """True while a session is still starting inside a live scope, or is alive."""
    return _is_live(session.load(sid))


def check_capacity() -> list[session.Session]:
    """Refuse a new launch when the concurrent-session cap is reached; return the live sessions.

    Counts every session whose scope is not fully gone: starting, running, or still stopping
    ("deactivating" scopes still hold their engine and Xvfb; QA SL-1, Y-6). Call it under
    launch.lock, as _start() does; otherwise two launches can both pass it.
    """
    cap = int(os.environ.get("FSO_HARNESS_MAX_SESSIONS", DEFAULT_MAX_SESSIONS))
    live = [s for s in session.list_sessions() if not scope.is_gone(s.scope)]
    if len(live) >= cap:
        raise HarnessError(EXIT_REFUSED, f"{len(live)} sessions already running (cap {cap})",
                           "stop one with `fsoctl stop <id>` or raise FSO_HARNESS_MAX_SESSIONS")
    return live


def _copy_pilot(src: Path, name: str, prefs: Path) -> None:
    """Copy one pilot's files (NAME.json, NAME.*.csg) from a players folder into the session."""
    files = [p for p in src.glob(f"{name}.*") if p.suffix in (".json", ".csg")]
    if not files:
        raise HarnessError(EXIT_LAUNCH, f"no pilot files for {name} in {src}",
                           "point --pilot-source at a data/players folder that has NAME.json")
    dest = prefs / "data" / "players"
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy2(f, dest / f.name)


def _inside(path: str, root: Path) -> bool:
    """True if `path` (resolved, symlinks followed) lies inside `root`."""
    return Path(path).resolve().is_relative_to(root.resolve())


def _check_binary(binary: str) -> None:
    """Refuse (EXIT_USAGE) an engine/qtFRED path that is not an executable file."""
    b = Path(binary)
    if not b.is_file() or not os.access(b, os.X_OK):
        raise HarnessError(EXIT_USAGE, f"engine binary {binary} is not an executable file",
                           "check --binary, $FSO_BIN or $QTFRED_BIN")


def _check_extra_args(req: LaunchRequest) -> None:
    """Refuse (EXIT_USAGE) extra engine arguments that would escape the session (contract C10).

    -mod/-start_mission/-pilot are the harness's own (a second -mod could push the bridge out of
    first place) and -portable_mode moves the engine's config into the working folder. The
    -output_* and -profile_write_file flags write files into the working folder, which is FS2PATH
    unless the run uses a harness shadow folder (api_docs and frame_stats do), so they are
    allowed only there (QA SA-5). Matching ignores case because FSO's own does (stricmp,
    cmdline.cpp:822), so -MOD is -mod to the engine (QA SA2-2 / CR2-3).
    """
    in_shadow = req.cwd is not None and _inside(req.cwd, paths.runtime_root() / "shadows")
    for arg in req.extra_args:
        flag = arg.lower()
        if flag.startswith(_HARNESS_ONLY_ARGS):
            raise HarnessError(EXIT_USAGE, f"extra engine argument {arg} is not allowed",
                               "the harness sets -mod, -start_mission and -pilot itself (use "
                               "--mods, --mission, --pilot); -portable_mode would move the "
                               "engine's config into the game folder")
        if flag.startswith(_OUTPUT_ARGS) and not in_shadow:
            raise HarnessError(EXIT_USAGE, f"extra engine argument {arg} is not allowed here",
                               "it writes files into the working folder, which is the game "
                               "folder; use api_docs or frame_stats, which run in a shadow folder")


def _check_folders(req: LaunchRequest) -> None:
    """Refuse (EXIT_USAGE) a working folder outside the runtime root, and mod names that are not
    plain folder names (QA SA2-2): -mod names resolve under FS2PATH, so "../x" or "/x" would point
    the engine elsewhere, and a working folder elsewhere receives the engine's output files."""
    if req.cwd is not None and not _inside(req.cwd, paths.runtime_root()):
        raise HarnessError(EXIT_USAGE, f"working folder {req.cwd} is outside the harness's "
                           "runtime folder", "leave cwd unset; verbs that need a working "
                           "folder make a shadow folder under the runtime root")
    for mod in req.mods:
        if mod in ("", ".", "..") or "/" in mod or "\0" in mod:
            raise HarnessError(EXIT_USAGE, f"mod name {mod!r} is not a plain folder name",
                               "name mod folders inside FS2PATH, e.g. MVPS-4.7.3")


def _validate(req: LaunchRequest) -> None:
    """Refuse a launch request before anything is created (EXIT_USAGE / EXIT_REFUSED)."""
    _check_binary(req.binary)
    _check_folders(req)
    _check_extra_args(req)
    if not isinstance(req.runtime_sec, int) or req.runtime_sec < 1:
        # Every session runs under a hard runtime cap; 0 was never "unlimited" (QA XFK-2).
        raise HarnessError(EXIT_USAGE, f"runtime {req.runtime_sec!r} is not a positive number of "
                           "seconds", "pass --runtime N with N >= 1 (default 1800)")
    if req.pilot_mode == "existing" and not req.pilot_source:
        raise HarnessError(EXIT_USAGE, f"pilot existing:{req.pilot_name} needs a pilot source",
                           "pass --pilot-source <data/players folder>: each session starts with "
                           "an empty prefs folder, so there is no pilot to select otherwise")
    # qtFRED saves over the file it opened, so it may only open a harness copy (QA SA-4, C9).
    if (req.kind == "qtfred" and req.mission and
            not (Path(req.mission).is_absolute()
                 and _inside(req.mission, paths.runtime_root() / "fred-missions"))):
        raise HarnessError(EXIT_REFUSED,
                           f"qtFRED may only open a staged copy of a mission, not {req.mission}",
                           "use `fsoctl fred_open <mission>`: it copies the mission into the "
                           "harness's folder and opens the copy")


def _mark_failed(sid: str, err: Exception) -> None:
    """Record a launch that failed after its session was created (so cleanup can remove it)."""
    def apply(s: session.Session) -> None:
        s.status, s.exit_info = "failed", {"error": str(err), "at": time.time()}
    session.update(sid, apply)


def _prepare(req: LaunchRequest,
             memory_max: str | None = None) -> tuple[session.Session, bool]:
    """Validate, install the bridge, create the session, hand it its switches, copy a pilot.

    Receives: memory_max (the scope's MemoryMax value, stored in the session). Returns (session,
    bridge_refreshed). The switches file is switches.txt, not .json: OpenGL startup deletes
    *.json and *.bin from mod cache folders before the bridge can read them (probe results
    section 14).
    """
    _validate(req)
    fs2 = paths.fs2path()
    # Before new_session: bridge_install refuses a refresh while any harness scope runs, and this
    # session has no scope yet, so it never counts itself.
    refreshed = bridge_install.install(fs2)["updated"] if req.kind == "game" else False
    sess = session.new_session(kind=req.kind, binary=str(Path(req.binary).resolve()),
                               fs2path=str(fs2), mods=req.mods, mission=req.mission,
                               renderer=req.renderer, width=req.width, height=req.height,
                               switches={k: getattr(req, k) for k in _SWITCH_NAMES},
                               allow_eval=req.allow_eval, extra_args=req.extra_args,
                               cwd=req.cwd, runtime_sec=req.runtime_sec, memory_max=memory_max)
    try:
        if req.kind == "game":
            chan = paths.channel_dir(session.prefs_dir(sess))
            chan.mkdir(parents=True)
            (chan / "switches.txt").write_text(json.dumps(sess.switches))
            if req.allow_eval:
                (chan / "allow_eval").touch()
        if req.pilot_source:
            _copy_pilot(Path(req.pilot_source), req.pilot_name, session.prefs_dir(sess))
    except BaseException as e:      # incl. SystemExit/KeyboardInterrupt (fix-wave-2 R3)
        _mark_failed(sess.id, e)
        if isinstance(e, HarnessError):
            e.details.setdefault("id", sess.id)
        raise
    return sess, refreshed


def _wait_window(sid: str, timeout: float) -> None:
    """Block (bounded) until qtFRED's editor window is up on the session display.

    Its splash screen appears first under the same process; the editor is the window titled with
    the mission file name (xwin.main_window), so readiness waits for that one.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = session.load(sid)
        if s.display and s.engine_pid and xwin.main_window(s.display, s.engine_pid, s.kind,
                                                           s.mission):
            return
        if s.status in ("exited", "failed"):
            raise HarnessError(EXIT_ENGINE, "qtFRED exited during startup", "see `fsoctl log <id>`")
        time.sleep(0.5)
    # EXIT_TIMEOUT like the game's readiness timeout, so agents branch on one code (QA CR-15).
    raise HarnessError(EXIT_TIMEOUT, f"qtFRED's editor window did not appear in {timeout:.0f}s",
                       "see `fsoctl log <id>` and take a screenshot")


def _runner_env() -> dict:
    """Environment for the runner: the caller's, plus PYTHONPATH so `-m fsoctl.runner` imports
    this same package even when fsoctl is not installed (e.g. run from a checkout)."""
    env = dict(os.environ)
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (pkg_parent, env.get("PYTHONPATH")) if p)
    return env


def _abandon(sid: str, err: HarnessError) -> None:
    """Stop a session whose launch failed after it was created; put its id in err.details.

    A failure to stop is reported in err.details["stop_error"] instead of replacing `err`, so the
    caller still sees why the launch failed (and can run crash_report on the id).
    """
    err.details.setdefault("id", sid)
    try:
        stop(sid)
    except HarnessError as stop_err:
        err.details["stop_error"] = stop_err.message


def _stop_on_cancel(sid: str) -> None:
    """Stop a session whose launch is being cancelled (SystemExit from SIGTERM, Ctrl-C).

    WHY: those are not HarnessErrors, so the failure handlers never ran and the engine held a
    session slot until RuntimeMaxSec (fix-wave-2 R3). The caller re-raises the cancellation; a
    stop failure can only be reported on stderr, since the cancellation is what propagates.
    """
    try:
        stop(sid)
    except HarnessError as e:
        print(f"fsoctl: could not stop session {sid}: {e.message}", file=sys.stderr)


def _start(req: LaunchRequest) -> tuple[session.Session, bool]:
    """Under launch.lock: check capacity, prepare the session, and start its runner's scope.

    Returns (session, bridge_refreshed). WHY the lock also spans scope.start (QA SL-1): a
    "starting" session counts toward the cap only once its scope is active, so the next launch
    must not look before this one's scope exists.
    """
    # The runner ends the run itself at runtime_sec (engine first, Xvfb last); RuntimeMaxSec is
    # the backstop for a runner that cannot, RUNTIME_BACKSTOP_SEC later (QA SL-1).
    props = scope.caps(runtime_sec=req.runtime_sec + RUNTIME_BACKSTOP_SEC)
    memory_max = next((p.split("=", 1)[1] for p in props if p.startswith("MemoryMax=")), None)
    with session.file_lock(paths.runtime_root() / "launch.lock", _LOCK_WAIT):
        check_capacity()
        sess, refreshed = _prepare(req, memory_max)
        try:
            scope.start(sess.scope, [sys.executable, "-m", "fsoctl.runner", sess.id],
                        _runner_env(), session.run_dir(sess) / "runner.log", props)
        except HarnessError as e:
            _abandon(sess.id, e)
            raise
        except BaseException:
            _stop_on_cancel(sess.id)
            raise
    return sess, refreshed


def _quote_heartbeat(err: HarnessError, sess: session.Session) -> None:
    """Add the bridge's error count, last error and switch outcomes to a failed launch (SF-1)."""
    if sess.kind != "game":
        return
    hb = channel.Channel(sess).heartbeat()
    if not hb:
        return
    err.details["heartbeat"] = {k: hb.get(k) for k in ("state", "errors", "last_error",
                                                       "switches")}
    if hb.get("errors"):
        err.message += f" (bridge errors: {hb['errors']}, last: {hb.get('last_error')})"


def _wait_ready(req: LaunchRequest, sid: str) -> dict:
    """Block (bounded) until the session is ready; return the first heartbeat ({} for qtFRED)."""
    if req.kind == "game":
        return channel.Channel(session.load(sid)).wait_ready(
            req.ready_timeout, lambda: _alive_or_starting(sid))
    _wait_window(sid, req.ready_timeout)
    return {}


def _warnings(req: LaunchRequest, found: dict) -> list[str]:
    """Return launch warnings: an OpenGL game whose renderer could not be confirmed (SF-16)."""
    if req.kind == "game" and req.renderer == "opengl" and found["renderer"] == "unknown":
        return ["renderer unknown: the engine had mapped neither Mesa GL nor a Vulkan driver, so "
                "the harness cannot confirm what draws the frames"]
    return []


def launch(req: LaunchRequest) -> dict:
    """Start a session and return it once ready.

    Returns {id, display, renderer, libraries, state, bridge_refreshed, errors, last_error,
    switches, warnings}; errors/last_error/switches come from the bridge heartbeat (contract C3).
    Every failure after the session exists stops it and carries details["id"] (QA CR-5, TA-4).
    """
    sess, refreshed = _start(req)
    try:
        hb = _wait_ready(req, sess.id)
        sess = session.load(sess.id)
        found = renderer.classify(renderer.read_maps(sess.engine_pid or 0))
        if req.kind == "game" and req.renderer == "vulkan" and found["renderer"] != "vulkan":
            raise HarnessError(EXIT_LAUNCH,
                               f"asked for Vulkan, engine came up as {found['renderer']}",
                               "check VK_ICD_FILENAMES and that the engine was built with Vulkan")
    except HarnessError as e:
        _quote_heartbeat(e, sess)
        _abandon(sess.id, e)
        raise
    except OSError as e:
        err = HarnessError(EXIT_LAUNCH, f"launch failed: {e}", "see `fsoctl log <id>`")
        _abandon(sess.id, err)
        raise err from e
    except BaseException:
        _stop_on_cancel(sess.id)
        raise
    return {"id": sess.id, "display": sess.display, **found, "state": hb.get("state"),
            "bridge_refreshed": refreshed, "errors": hb.get("errors"),
            "last_error": hb.get("last_error"), "switches": hb.get("switches"),
            "warnings": _warnings(req, found)}


def run_to_exit(req: LaunchRequest, timeout: float) -> session.Session:
    """Start a session that ends by itself (e.g. -pofspew) and wait (bounded) until it exits.

    Unlike launch() there is no readiness wait: such runs may exit before the bridge ever starts.
    Returns the final session record; on timeout the session is stopped and EXIT_TIMEOUT raised.
    """
    sess, _refreshed = _start(req)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            s = session.load(sess.id)
            if s.status in ("exited", "failed") or not scope.is_active(s.scope):
                return session.load(sess.id)
            time.sleep(0.5)
    except BaseException:
        _stop_on_cancel(sess.id)
        raise
    stop(sess.id)
    raise HarnessError(EXIT_TIMEOUT, f"engine did not exit within {timeout:.0f}s",
                       "see `fsoctl log <id>`", {"id": sess.id})


def quit(sid: str, timeout: float = 30.0) -> dict:
    """Ask the engine to exit cleanly (bridge posts GS_EVENT_QUIT_GAME), wait, then stop.

    WHY not just stop(): stopping the scope kills Xvfb and the engine at once, so files the
    engine writes on shutdown (profiling.csv) end up empty. A clean exit flushes them.
    """
    sess = require_running(sid)
    channel.Channel(sess).send("quit", timeout=10, alive=lambda: alive(sess))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and alive(session.load(sid)):
        time.sleep(0.25)
    return stop(sid)


def require_running(sid: str) -> session.Session:
    """Return the session if its engine is running; else raise EXIT_ENGINE with exit info."""
    sess = session.load(sid)
    if not alive(sess):
        raise HarnessError(EXIT_ENGINE,
                           f"session {sid} is not running ({sess.status}, exit {sess.exit_info})",
                           f"run `fsoctl crash_report {sid}` or relaunch")
    return sess


def status(sid: str) -> dict:
    """Return the session record plus liveness, the latest bridge heartbeat, and its age.

    heartbeat_age_sec comes from the channel (contract C3: seconds since ready.json was written);
    stale is True when that age exceeds STALE_SEC (QA SL-10 / SF-5). The bridge rewrites the
    heartbeat after at most 30 frames or 1 s of frame time (fix-wave-2, group A), so a heartbeat
    older than STALE_SEC means frames have stopped: a hung or blocked engine. Both are None when
    the age is unknown.
    """
    sess = session.load(sid)
    hb = channel.Channel(sess).heartbeat()
    age = hb.get("age_sec") if hb else None
    return {**asdict(sess), "alive": alive(sess), "heartbeat": hb, "heartbeat_age_sec": age,
            "stale": None if age is None else age > STALE_SEC}


def bridge_info(sid: str) -> dict:
    """Ask the running bridge what it read at startup (switches, eval opt-in, error count)."""
    sess = require_running(sid)
    return channel.Channel(sess).send("bridge_info", timeout=10, alive=lambda: alive(sess))


def _stop_scope_only(sid: str, not_found: HarnessError) -> dict:
    """Stop the scope of a session whose run folder is gone; re-raise `not_found` if none is left.

    WHY: a concurrent cleanup can delete a folder while its scope still runs (QA SL-2); launch's
    error path must still be able to stop it.
    """
    paths.session_dir(sid)            # raises EXIT_NOT_FOUND for something that is not an id
    unit = session.scope_name(sid)
    if scope.is_gone(unit):
        raise not_found
    scope.stop(unit)
    return {"id": sid, "status": "stopped", "exit_info": None,
            "runner_first": "skipped:run folder already removed",
            "note": "its run folder had already been removed"}


def _cgroup_has_scope(cgroup_text: str, unit: str) -> bool:
    """True when /proc/<pid>/cgroup text places the process in `<unit>.scope` (exact name)."""
    return any(line.rsplit("/", 1)[-1] == f"{unit}.scope" for line in cgroup_text.splitlines())


def _in_scope(pid: int, unit: str) -> bool:
    """True when process `pid` exists and lives in the session's scope.

    WHY check: runner_pid is a stored number; after the session ended it may belong to an
    unrelated process, which must never be signalled.
    """
    try:
        return _cgroup_has_scope(Path(f"/proc/{pid}/cgroup").read_text(), unit)
    except OSError:
        return False


def _gone(pid: int) -> bool:
    """True when `pid` no longer runs (absent, or a zombie waiting for its parent to reap it)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    return stat.rsplit(")", 1)[-1].split()[0] == "Z"   # field 3; the name in () may hold spaces


def _stop_runner_first(sess: session.Session) -> str:
    """SIGTERM the session's runner and wait (at most _RUNNER_EXIT_WAIT s) for it to exit.

    WHY: `systemctl stop` on the scope SIGTERMs Xvfb at the same moment as the engine. The engine
    then hit an X I/O error mid-frame, Xlib called exit(), and exit-time teardown aborted: every
    stop left a SIGABRT and a 12-34 MB core dump (2026-10-12; backtrace _XIOError -> exit ->
    nvgDeleteTexture -> free). The runner already ends things in the right order (it passes the
    stop to the engine, waits for it, and ends Xvfb last), so it is asked first; the scope stop
    that follows is the backstop for a runner that is gone, hung, or not ours.
    Returns what happened, for stop()'s result (QA CR-5/SL-4: the fallback must not be silent):
    "ok", "timeout", or "skipped:<why>". Any failure here leaves the scope stop to do the work.
    """
    pid = sess.runner_pid
    if not pid:
        return "skipped:no runner recorded"
    if not _in_scope(pid, sess.scope):
        return "skipped:runner not running in the session's scope"
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:     # gone in the meantime, or not ours to signal: the backstop handles it
        return f"skipped:{e.strerror or e}"
    deadline = time.monotonic() + _RUNNER_EXIT_WAIT
    # Bounded by the deadline. The scope check too: a pid reused outside the scope is not our
    # runner, so there is nothing more to wait for (QA CR-7).
    while time.monotonic() < deadline:
        if _gone(pid) or not _in_scope(pid, sess.scope):
            return "ok"
        time.sleep(_RUNNER_POLL)
    sys.stderr.write(f"fsoctl: the runner of {sess.id} did not exit within "
                     f"{_RUNNER_EXIT_WAIT:.0f} s; stopping the scope (the engine may abort and "
                     "leave a core dump)\n")
    return "timeout"


def stop(sid: str) -> dict:
    """Stop a session's scope (idempotent) and mark it stopped; keep the run folder.

    Creates the stop marker first, so the runner (which gets SIGTERM from this stop) records
    ended_by "stop". The runner is asked first and the scope stopped after it (see
    _stop_runner_first: stopping the scope alone took Xvfb away under a running engine).
    A session that already ended keeps its status and exit_info. The run
    folder can vanish at any point (a concurrent cleanup, QA YK-3); the scope is stopped anyway.
    """
    try:
        sess = session.load(sid)
        (session.run_dir(sess) / session.STOP_MARKER).touch()
    except HarnessError as e:
        if e.code != EXIT_NOT_FOUND:
            raise
        return _stop_scope_only(sid, e)
    except FileNotFoundError:
        return _stop_scope_only(sid, HarnessError(EXIT_NOT_FOUND, f"no session {sid}",
                                                  "use `fsoctl list`"))
    runner_first = _stop_runner_first(sess)
    scope.stop(sess.scope)   # backstop: ends whatever is left (or everything, if the runner hung)

    def apply(s: session.Session) -> None:
        if s.status in ("starting", "running"):
            s.status = "stopped"
            if s.ended_by is None:
                s.ended_by = "stop"
    try:
        sess = session.update(sid, apply)
    except (HarnessError, OSError) as e:
        if isinstance(e, HarnessError) and e.code != EXIT_NOT_FOUND:
            raise
        return {"id": sid, "status": "stopped", "exit_info": None, "runner_first": runner_first,
                "note": "its run folder was removed while it was being stopped"}
    return {"id": sid, "status": sess.status, "exit_info": sess.exit_info,
            "runner_first": runner_first}


def list_all() -> dict:
    """Return a short line per session."""
    return {"sessions": [{"id": s.id, "kind": s.kind, "status": s.status, "alive": alive(s),
                          "mission": s.mission, "renderer": s.renderer}
                         for s in session.list_sessions()]}


def _removable(s: session.Session, now: float, older_than: float) -> bool:
    """True if a session's run folder may be deleted: old enough, and nothing of it still runs.

    Kept (QA SL-2): a "starting" record younger than _STARTING_GRACE (its launch may be between
    creating it and starting its scope), and any session whose scope is not fully gone (a
    "deactivating" scope's engine can still be writing into the folder).
    """
    if now - s.created < older_than:
        return False
    if s.status == "starting" and now - s.created < _STARTING_GRACE:
        return False
    return scope.is_gone(s.scope)


def _cleanup_side_folders(now: float, older_than: float, kept: list[session.Session],
                          failed: list[dict]) -> list[str]:
    """Delete shadows/<x> and fred-missions/<x> folders older than max(`older_than`,
    _SIDE_MIN_AGE) that no kept session uses (as its working folder or its mission's folder);
    return the deleted paths.

    WHY the floor (fix-wave-2 R4): a verb creates its shadow folder or mission copy BEFORE its
    session exists, and the folder is only referenced once the session is saved (after a wait
    of up to a minute on launch.lock); after the run, docs/frame_stats read their output from it
    after the session has stopped. Every such window is far shorter than an hour.

    Only folder names the harness itself makes (8 hex digits) are touched, and never a symlink.
    shutil.rmtree removes the links inside a shadow folder without following them into FS2PATH.
    """
    refs = {os.path.realpath(s.cwd) for s in kept if s.cwd}
    refs |= {os.path.realpath(os.path.dirname(s.mission)) for s in kept
             if s.mission and os.path.isabs(s.mission)}
    removed = []
    for kind in _SIDE_FOLDERS:
        base = paths.runtime_root() / kind
        entries = sorted(base.iterdir()) if base.is_dir() else []
        for d in entries:
            if not _SIDE_NAME.match(d.name) or d.is_symlink() or not d.is_dir():
                continue
            if (os.path.realpath(d) in refs
                    or now - d.stat().st_mtime < max(older_than, _SIDE_MIN_AGE)):
                continue
            try:
                shutil.rmtree(d)
                removed.append(str(d))
            except OSError as e:
                failed.append({"path": str(d), "error": str(e)})
    return removed


def cleanup(older_than_sec: float = 0) -> dict:
    """Delete run folders of finished sessions at least `older_than_sec` old, and old shadow and
    fred-missions folders no remaining session uses.

    Returns {"removed": [ids], "removed_folders": [paths], "failed": [{path, error}]}; a folder
    that cannot be deleted is reported, not fatal. Runs under launch.lock (R4), so no launch is
    between staging and saving its session while cleanup decides what is unreferenced.
    """
    with session.file_lock(paths.runtime_root() / "launch.lock", _LOCK_WAIT):
        return _cleanup_locked(older_than_sec)


def _cleanup_locked(older_than_sec: float) -> dict:
    """cleanup()'s body; the caller holds launch.lock."""
    removed, kept, failed = [], [], []
    now = time.time()
    for s in session.list_sessions():
        if not _removable(s, now, older_than_sec):
            kept.append(s)
            continue
        try:
            shutil.rmtree(session.run_dir(s))
            removed.append(s.id)
        except OSError as e:
            failed.append({"path": str(session.run_dir(s)), "error": str(e)})
            kept.append(s)
    folders = _cleanup_side_folders(now, older_than_sec, kept, failed)
    return {"removed": removed, "removed_folders": folders, "failed": failed}


# ---- command line ----------------------------------------------------------------------------

def _parse_res(text: str) -> tuple[int, int]:
    """Parse "WxH" into two positive ints; raise EXIT_USAGE otherwise."""
    try:
        w, h = (int(x) for x in text.lower().split("x"))
    except ValueError as e:
        raise HarnessError(EXIT_USAGE, f"bad resolution {text!r}", "use WxH, e.g. 1280x720") from e
    if w <= 0 or h <= 0:
        raise HarnessError(EXIT_USAGE, f"bad resolution {text!r}", "use WxH, e.g. 1280x720")
    return w, h


def _parse_pilot(text: str) -> tuple[str, str]:
    """Parse "chooser" | "existing:NAME" | "create:NAME" into (mode, name)."""
    mode, _, name = text.partition(":")
    if mode == "chooser" and not name:
        return mode, "HarnessPilot"
    if mode in ("existing", "create") and name:
        return mode, name
    raise HarnessError(EXIT_USAGE, f"bad --pilot {text!r}",
                       "use chooser, existing:NAME or create:NAME")


def _default_binary(qtfred: bool) -> str:
    """Return $QTFRED_BIN or $FSO_BIN; raise EXIT_USAGE when it is not set."""
    var = "QTFRED_BIN" if qtfred else "FSO_BIN"
    value = os.environ.get(var)
    if not value:
        raise HarnessError(EXIT_USAGE, f"no binary given and {var} is not set",
                           f"pass --binary or export {var}")
    return value


def _configure_launch(p: argparse.ArgumentParser) -> None:
    """Add the launch flags (they mirror LaunchRequest)."""
    p.add_argument("--binary", help="engine or qtFRED binary (default $FSO_BIN / $QTFRED_BIN)")
    p.add_argument("--qtfred", action="store_true", help="launch qtFRED instead of the game")
    p.add_argument("--mission", help="mission file, e.g. SM1-01.fs2 (skips the main hall)")
    p.add_argument("--mods", default="", help="comma-separated mod folders, e.g. MVPS-4.7.3")
    p.add_argument("--vulkan", action="store_true", help="use the Vulkan renderer")
    p.add_argument("--res", default="1280x720", help="window size WxH")
    p.add_argument("--skip-movies", action="store_true")
    p.add_argument("--pilot", default="chooser", help="chooser | existing:NAME | create:NAME")
    p.add_argument("--pilot-source", help="data/players folder to copy an existing pilot from")
    p.add_argument("--dismiss-popups", action="store_true")
    p.add_argument("--skip-command-briefing", action="store_true")
    p.add_argument("--skip-briefing", action="store_true")
    p.add_argument("--interactive", action="store_true", help="do not pass -noninteractive")
    p.add_argument("--allow-eval", action="store_true", help="allow free-form lua/post_event")
    p.add_argument("--runtime", type=int, default=1800, help="hard runtime limit in seconds")
    p.add_argument("--ready-timeout", type=float, default=240.0)
    p.add_argument("extra", nargs="*", help="extra engine arguments, after --")


@register("launch", "start a game or qtFRED session and wait until it is ready", _configure_launch)
def _cli_launch(args: argparse.Namespace) -> dict:
    """Build a LaunchRequest from CLI flags and launch it."""
    width, height = _parse_res(args.res)
    mode, name = _parse_pilot(args.pilot)
    req = LaunchRequest(
        binary=args.binary or _default_binary(args.qtfred), mission=args.mission,
        mods=[m for m in args.mods.split(",") if m], renderer="vulkan" if args.vulkan else "opengl",
        width=width, height=height, skip_movies=args.skip_movies, pilot_mode=mode,
        pilot_name=name, pilot_source=args.pilot_source, dismiss_popups=args.dismiss_popups,
        skip_command_briefing=args.skip_command_briefing, skip_briefing=args.skip_briefing,
        noninteractive=not args.interactive, allow_eval=args.allow_eval, extra_args=args.extra,
        kind="qtfred" if args.qtfred else "game", runtime_sec=args.runtime,
        ready_timeout=args.ready_timeout)
    return launch(req)


def _configure_id(p: argparse.ArgumentParser) -> None:
    """Add the session id positional."""
    p.add_argument("id", help="session id from `fsoctl launch` or `fsoctl list`")


@register("status", "show a session's record, liveness and bridge heartbeat", _configure_id)
def _cli_status(args: argparse.Namespace) -> dict:
    """CLI wrapper for status()."""
    return status(args.id)


@register("stop", "stop a session (idempotent); keeps its run folder", _configure_id)
def _cli_stop(args: argparse.Namespace) -> dict:
    """CLI wrapper for stop()."""
    return stop(args.id)


@register("list", "list sessions", lambda p: None)
def _cli_list(args: argparse.Namespace) -> dict:
    """CLI wrapper for list_all()."""
    return list_all()


def _configure_cleanup(p: argparse.ArgumentParser) -> None:
    """Add the cleanup age filter."""
    # 600 s like the MCP tool (QA YK-5): with 0, a plain `fsoctl cleanup` deleted the run folder
    # of a session that had just failed, with the logs crash_report needs.
    p.add_argument("--older-than", type=float, default=600, help="seconds since launch")


@register("cleanup", "delete run folders of sessions that are not running", _configure_cleanup)
def _cli_cleanup(args: argparse.Namespace) -> dict:
    """CLI wrapper for cleanup()."""
    return cleanup(args.older_than)
