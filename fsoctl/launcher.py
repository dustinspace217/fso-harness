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
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import bridge_install, channel, match, paths, renderer, scope, session, sound, xwin
from .cli import register
from .errors import (EXIT_ENGINE, EXIT_LAUNCH, EXIT_NOT_FOUND, EXIT_REFUSED, EXIT_TIMEOUT,
                     EXIT_USAGE, HarnessError)

DEFAULT_MAX_SESSIONS = 3
STALE_SEC = 5.0         # contract C3: a heartbeat older than this is stale
_LOCK_WAIT = 60.0       # seconds to wait for launch.lock (held for one install + scope start)
_STARTING_GRACE = 120.0  # cleanup leaves a "starting" record alone this long (see _removable)
# Extra engine arguments the caller may not pass (contract C10), see _check_extra_args.
_HARNESS_ONLY_ARGS = ("-portable_mode", "-mod", "-start_mission", "-pilot",
                      # Multiplayer flags (QA SA-2): from an ordinary session they would start a
                      # server or host on the REAL network; --match/--role is the only way in.
                      "-standalone", "-startgame", "-connect", "-port", "-gamename", "-closed",
                      "-almission", "-gateway_ip")
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
                 "skip_command_briefing", "skip_briefing", "noninteractive",
                 "pilot_multi", "multi_role", "multi_launch")
ROLES = ("standalone", "host", "client")
DEFAULT_GAME_PORT = 7808                      # DEFAULT_GAME_PORT in the engine's psnet2.h


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
    match: str | None = None             # a `fsoctl match create` id; required with role
    role: str | None = None              # one of ROLES
    port: int | None = None              # local UDP port; None = next free in the match
    connect: str | None = None           # client: server IP:PORT; None = the match's live server
    game_name: str | None = None         # host/standalone: shown in the join list; None = match id
    closed: bool = False                 # host: -closed
    network: str = "private"             # "private" (own loopback-only namespace) | "real"
    sound: bool = False                  # play audio into the session's own sink (sound.py)

    @property
    def pilot_multi(self) -> bool:
        """Multiplayer roles create multiplayer pilots (a single-player pilot cannot join)."""
        return self.role in ("host", "client")

    @property
    def multi_role(self) -> str | None:
        """What the bridge needs to know: it posts the join-screen events only for clients."""
        return self.role

    @property
    def multi_launch(self) -> bool:
        """Only the session given --mission (the intended host) presses Launch on the sync screen
        (QA SL-3); every other client would otherwise press it too."""
        return self.role in ("host", "client") and bool(self.mission)


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
    argv = [sess.binary, "-mod", ",".join([paths.BRIDGE_MOD, *sess.mods])]
    if not sess.sound:
        argv.append("-nosound")
    if sess.role != "standalone":
        # The standalone has no window (stand_gui-unix.cpp); everything else draws into Xvfb.
        argv += ["-window", "-res", f"{sess.width}x{sess.height}", "-no_unfocused_pause"]
    if sw.get("noninteractive", True):
        argv.append("-noninteractive")
    if sw.get("skip_movies"):
        argv.append("-nomovies")
    if sw.get("pilot_mode") == "existing":
        argv += ["-pilot", sw.get("pilot_name", "HarnessPilot")]
    if sess.role is None and sess.mission:
        argv += ["-start_mission", sess.mission]
    if sess.role is not None:
        argv += _multi_argv(sess)
    if sess.renderer == "vulkan":
        argv.append("-vulkan")
    return argv + sess.extra_args


def _multi_argv(sess: session.Session) -> list[str]:
    """Return the role's engine flags (spec section 4; cmdline.cpp:210-303, 429)."""
    name = sess.game_name or sess.match
    out = ["-port", str(sess.port)]
    if sess.role == "standalone":
        out.insert(0, "-standalone")
    elif sess.role == "host":
        out += ["-startgame", "-gamename", name]
        if sess.closed:
            out.append("-closed")
    else:
        out += ["-connect", sess.connect]
    if sess.mission:
        # -almission takes the mission NAME; the engine adds .fs2 (multiui.cpp:3674). The host
        # player consumes it and starts the mission with no human (probe-results 21.2).
        name = sess.mission[:-4] if sess.mission.lower().endswith(".fs2") else sess.mission
        out += ["-almission", name]
    return out


def multi_cfg_text(game_name: str) -> str:
    """Return the standalone's multi.cfg: a name and the (private) web panel port; never +pxo."""
    return f"+name {game_name}\n+webapi_server_port 8080\n"


def assign_port(mid: str, wanted: int | None) -> int:
    """Return the local port for a new session in match `mid`: `wanted` if free there, else the
    first free port from DEFAULT_GAME_PORT up. Every engine binds [::]:<port>, so two sessions in
    one namespace cannot share a port (probe-results 21.2; plan Review Focus 1)."""
    # Live scopes only (the cap's predicate): a stopped server's port is free again, otherwise a
    # relaunched server moves to 7809 while clients still default to 7808 (QA SL-1/CR-7/TA-3).
    used = {s.port for s in sessions_in_match(mid) if s.port and not scope.is_gone(s.scope)}
    if wanted is not None:
        if wanted in used:
            raise HarnessError(EXIT_USAGE, f"port {wanted} is already used in match {mid}",
                               "leave --port unset to take the next free one")
        return wanted
    port = DEFAULT_GAME_PORT
    while port in used:          # bounded: `used` holds at most one port per live session
        port += 1
    return port


def default_connect(mid: str) -> str:
    """Return the address a client joins by default: the match's live standalone or host, else
    the default port (QA CR-6/SL-1: the docs promised "the match's server"; the code had a
    constant)."""
    for s in sessions_in_match(mid):
        if s.role in ("standalone", "host") and s.port and not scope.is_gone(s.scope):
            return f"127.0.0.1:{s.port}"
    return f"127.0.0.1:{DEFAULT_GAME_PORT}"


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
                               "engine's config into the game folder; multiplayer flags are set "
                               "by --match/--role (fsoctl match create)")
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


def _check_role(req: LaunchRequest) -> None:
    """Refuse a multiplayer request that is incomplete or points at a dead match."""
    if req.role is None and req.match is None:
        # QA TA-7: multiplayer values without a role used to be dropped silently.
        if req.port is not None or req.connect is not None or req.game_name or req.closed:
            raise HarnessError(EXIT_USAGE, "--port/--connect/--game-name/--closed need --match "
                               "and --role", "fsoctl match create, then launch --match <id> "
                               "--role standalone|host|client")
        return
    if req.role is None or req.match is None:
        raise HarnessError(EXIT_USAGE, "--match and --role go together",
                           "fsoctl match create, then launch --match <id> "
                           "--role standalone|host|client")
    if req.role not in ROLES:
        raise HarnessError(EXIT_USAGE, f"unknown role {req.role!r}",
                           f"use one of {', '.join(ROLES)}")
    if req.kind != "game":
        raise HarnessError(EXIT_USAGE, "qtFRED has no multiplayer role", "drop --match/--role")
    if os.getuid() == 0:                        # spec 5.4 (QA TA-10): same rule as match create
        raise HarnessError(EXIT_REFUSED, "multiplayer sessions refuse to run as root",
                           "run as your normal user")
    if req.role == "standalone" and (req.mission or req.pilot_source):
        # -almission is consumed by the host player, never by the standalone (probe-results 21.2);
        # a standalone has no pilot at all.
        raise HarnessError(EXIT_USAGE, "a standalone server takes no mission and no pilot",
                           "give --mission to the host or to the first client")
    if req.role == "standalone" and req.renderer != "opengl":
        # It draws nothing (QA XFA-5): the Vulkan check after launch would stop it for no reason.
        raise HarnessError(EXIT_USAGE, "a standalone server has no renderer", "drop --vulkan")
    if req.role != "standalone" and req.pilot_mode != "create":
        # The bridge creates a MULTIPLAYER pilot only in create mode; the chooser sits on the
        # pilot screen and an existing single-player pilot is refused at the multiplayer door
        # (QA CR-8). An existing multiplayer pilot is a later feature.
        raise HarnessError(EXIT_USAGE, f"role {req.role} needs a created multiplayer pilot",
                           "pass --pilot create:<name>")
    if req.runtime_sec > match.HOLDER_RUNTIME_SEC:
        # The holder lives HOLDER_RUNTIME_SEC at most; a longer session would outlive its
        # network by design (QA SL-9 / CR-12).
        raise HarnessError(EXIT_USAGE, f"--runtime {req.runtime_sec} is longer than a match's "
                           f"network lives ({match.HOLDER_RUNTIME_SEC} s)",
                           "pass a shorter --runtime, or create a new match later")
    _check_multi_values(req)
    _check_match_room(req)


def _check_match_room(req: LaunchRequest) -> None:
    """Refuse a join into a match that is not alive, already has a server, or already has the
    session that carries the mission."""
    m = match.load(str(req.match))              # EXIT_NOT_FOUND
    if not match.alive(m):
        raise HarnessError(EXIT_REFUSED, f"match {req.match} is dead",
                           "create a new one with `fsoctl match create`")
    live = [s for s in sessions_in_match(str(req.match)) if not scope.is_gone(s.scope)]
    if req.role in ("standalone", "host") and any(s.role in ("standalone", "host") for s in live):
        # One server per match (QA XFK-2): the standalone's web panel port (8080) is not per
        # session, and a second server in one network would collide on it.
        raise HarnessError(EXIT_USAGE, f"match {req.match} already has a server",
                           "create another match for a second server")
    if req.mission and any(s.mission for s in live):
        # One mission per match (QA XFK-5): -almission is consumed by whichever session becomes
        # host, so two carriers would make which mission starts order-dependent.
        raise HarnessError(EXIT_USAGE, f"match {req.match} already has a session carrying a "
                           "mission", "give --mission to one session per match")


# What may appear in the values that reach the engine's command line or multi.cfg. WHY so strict:
# the engine joins argv back into one string and re-splits it on spaces and dashes (cmdline.cpp),
# so a game name "x -pilot y" would add a flag, and multi.cfg is line-based, so a newline in the
# name would add a `+pxo` line (spec section 5.3). A mission name is a file stem: no separators,
# and no leading "-" or ".": a value starting with "-" is not taken as the flag's value, it is
# parsed as a flag of its own (cmdline.cpp:745-749; QA SA-1: `--mission -portable_mode`).
_GAME_NAME_RE = re.compile(r"^[A-Za-z0-9_.'!]+$")
_CONNECT_RE = re.compile(r"^(\d{1,3}(\.\d{1,3}){3}|\[[0-9A-Fa-f:]+\]):\d{1,5}$")
_MISSION_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
# The engine takes a port <= 1023 as its default (psnet2.cpp:441-443, psnet_init), and a port
# is a 16-bit value, so a recorded port and the bound one would disagree (QA SA-4).
PORT_MIN, PORT_MAX = 1024, 65535


NETWORKS = ("private", "real")
# The ENGINE (not the runner, not Xvfb) starts inside a fresh user+network namespace with only
# loopback: the engine binds its UDP game port on every interface at startup, single-player
# included (freespace.cpp game_init -> psnet_init; measured 2026-10-13), so without this every
# harness session is as visible on the LAN as the game itself for its whole run. No holder is
# needed for one session: the namespace lives as long as the engine process. WHY only the engine:
# X display numbers are allocated by Xvfb itself through the ABSTRACT socket, which is per network
# namespace; an Xvfb inside the namespace took the desktop's number as seen from the host and
# replaced the host's socket file (QA 2026-10-13), and a host-side allocator could not hold the
# number for the session's life (SL-1/SA-1/XFA-1). Xvfb on the host owns both its sockets, the
# engine reaches it through the filesystem socket (probed 2026-10-13: xdpyinfo in a fresh
# user+net namespace reaches a host Xvfb), and the host's X tools reach it as they always did.
# Same facts as a match: uid 0 inside maps to the real user; pathname unix sockets cross the
# boundary; nothing the engine needs is on the real network. `--network real` is the opt-out.
# The prefix execs the program via "$@" so no argument is re-parsed by the shell and the
# engine's pid is the pid Popen returns (unshare and sh both exec; probed 2026-10-13).
PRIVATE_NET_PREFIX = ["unshare", "--user", "--map-root-user", "--net", "sh", "-c",
                      'ip link set lo up && exec "$@"', "sh"]


def _check_network(req: LaunchRequest) -> None:
    """Refuse (EXIT_USAGE) an unknown network choice, or `real` on a match session (a match
    session always lives in its match's network; nothing else makes sense)."""
    if req.network not in NETWORKS:
        raise HarnessError(EXIT_USAGE, f"unknown --network {req.network!r}",
                           f"use one of {', '.join(NETWORKS)}")
    if req.match and req.network != "private":
        raise HarnessError(EXIT_USAGE, "a match session always runs in its match's network",
                           "drop --network")
    if req.network == "private" and not req.match:
        for tool in ("unshare", "ip"):     # checked here so a missing tool never starts a scope
            if not shutil.which(tool):
                raise HarnessError(EXIT_LAUNCH,
                                   f"cannot make a private network: `{tool}` not found",
                                   "install util-linux and iproute2, or launch with --network real")
        if os.geteuid() == 0:              # same rule as match.create (QA SA-5)
            raise HarnessError(EXIT_REFUSED, "refusing to start a private-network session as root",
                               "run the harness as an ordinary user")


def _check_mission_name(req: LaunchRequest) -> None:
    """Refuse (EXIT_USAGE) a mission value that is not a plain file name, for EVERY game session:
    both -start_mission and -almission would otherwise carry it to the engine's re-split."""
    if req.kind == "game" and req.mission and not _MISSION_RE.match(req.mission):
        raise HarnessError(EXIT_USAGE, f"mission {req.mission!r} is not a plain file name",
                           "pass the mission's file name, e.g. SM1-01.fs2 (letters, digits, "
                           "_ . - and no leading - or .)")


def _check_multi_values(req: LaunchRequest) -> None:
    """Refuse (EXIT_USAGE) multiplayer values that could carry extra flags or config lines."""
    if req.game_name is not None and not _GAME_NAME_RE.match(req.game_name):
        raise HarnessError(EXIT_USAGE, f"game name {req.game_name!r} may only contain letters, "
                           "digits and _ . ' !", "the name reaches the engine command line and "
                           "multi.cfg; pick a plain one")
    if req.role == "client" and req.connect is not None and not _CONNECT_RE.match(req.connect):
        raise HarnessError(EXIT_USAGE, f"--connect {req.connect!r} is not IP:PORT",
                           "use 127.0.0.1:7808 or [::1]:7808 (a match is loopback only, so a "
                           "hostname cannot be right)")
    if req.port is not None and not PORT_MIN <= req.port <= PORT_MAX:
        raise HarnessError(EXIT_USAGE, f"--port {req.port} is outside {PORT_MIN}..{PORT_MAX}",
                           "the engine silently remaps ports below 1024 and wraps larger ones; "
                           "leave --port unset to take the next free one")


def _validate(req: LaunchRequest) -> None:
    """Refuse a launch request before anything is created (EXIT_USAGE / EXIT_REFUSED)."""
    _check_binary(req.binary)
    _check_folders(req)
    _check_extra_args(req)
    _check_mission_name(req)
    _check_network(req)
    _check_role(req)
    if req.sound and (req.kind != "game" or req.role == "standalone"):
        raise HarnessError(EXIT_USAGE, "--sound is for game sessions with a window",
                           "drop --sound (qtFRED and a standalone server play no audio)")
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
                               cwd=req.cwd, runtime_sec=req.runtime_sec, memory_max=memory_max,
                               match=req.match, role=req.role,
                               port=assign_port(req.match, req.port) if req.match else None,
                               connect=(req.connect or default_connect(req.match)
                                        if req.match else f"127.0.0.1:{DEFAULT_GAME_PORT}"),
                               game_name=req.game_name, closed=req.closed,
                               network=req.network, sound=req.sound)
    try:
        if req.kind == "game":
            chan = paths.channel_dir(session.prefs_dir(sess))
            chan.mkdir(parents=True)
            (chan / "switches.txt").write_text(json.dumps(sess.switches))
            if req.allow_eval:
                (chan / "allow_eval").touch()
        if req.role is not None:
            # Read by the engine through cfile (CF_TYPE_DATA), so the session's private prefs
            # data/ wins over anything in the game folder (spec section 3). Every match role gets
            # one (QA SA-8): a +pxo left in the user's game folder must never reach a host.
            cfg = session.prefs_dir(sess) / "data" / "multi.cfg"
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text(multi_cfg_text(str(req.game_name or req.match)))
        if req.pilot_source:
            _copy_pilot(Path(req.pilot_source), req.pilot_name, session.prefs_dir(sess))
        if req.sound:
            module = sound.create_sink(sess.id)

            def keep_module(s: session.Session) -> None:
                s.sound_module = module
            sess = session.update(sess.id, keep_module)      # first: the except below needs it
            sound.write_device_prefs(session.prefs_dir(sess), sess.id)
            sound.check_device_names(sess.binary, sess.id)
    except BaseException as e:      # incl. SystemExit/KeyboardInterrupt (fix-wave-2 R3)
        # No scope exists yet, so nothing calls stop for this session: unload its sink here.
        sink_error = (sound.remove_sink(sess.sound_module, sess.id)
                      if sess.sound_module is not None else None)
        _mark_failed(sess.id, e)
        if isinstance(e, HarnessError) and sink_error:
            e.details["sound_sink_error"] = sink_error + " (`fsoctl cleanup` retries)"
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
            # The runner (and its Xvfb) stay on the host; the runner puts the ENGINE into the
            # session's network (runner.engine_argv: the match's namespace via nsenter, or its own
            # via PRIVATE_NET_PREFIX). See the PRIVATE_NET_PREFIX comment for why not the runner.
            runner_argv = [sys.executable, "-m", "fsoctl.runner", sess.id]
            scope.start(sess.scope, runner_argv, _runner_env(),
                        session.run_dir(sess) / "runner.log", props)
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


def listening(sess: session.Session) -> bool:
    """True when a UDP socket on the session's port exists inside its match (standalone readiness).

    Asked through nsenter because the socket exists only in the private network; `ss` on the real
    network must show nothing, which is the point (spec section 5.1).
    """
    try:
        r = match.run_inside(match.load(str(sess.match)),
                             ["ss", "-H", "-lun", f"sport = :{sess.port}"])
    except (HarnessError, subprocess.TimeoutExpired):
        # A holder that died between two reads, or an `ss` that hangs, is "not listening", never
        # a crashed status read (QA SL-6).
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


def _wait_listening(req: LaunchRequest, sid: str) -> None:
    """Block (bounded) until the standalone listens; EXIT_TIMEOUT / EXIT_ENGINE otherwise.

    A standalone has no bridge (no window, no Lua UI), so the heartbeat cannot be its readiness;
    the socket it binds is.
    """
    deadline = time.monotonic() + req.ready_timeout
    port = None
    while time.monotonic() < deadline:      # bounded by the deadline
        s = session.load(sid)
        port = s.port
        if s.status in ("exited", "failed"):
            raise HarnessError(EXIT_ENGINE, "the standalone server exited during startup",
                               "see `fsoctl log <id>`")
        if s.status == "running" and listening(s):
            return
        time.sleep(0.5)
    raise HarnessError(EXIT_TIMEOUT, f"the standalone never listened on port {port} in "
                       f"{req.ready_timeout:.0f}s", "see `fsoctl log <id>`")


def _wait_ready(req: LaunchRequest, sid: str) -> dict:
    """Block (bounded) until the session is ready; return the first heartbeat ({} for qtFRED and
    for a standalone server, which has no bridge)."""
    if req.role == "standalone":
        _wait_listening(req, sid)
        return {}
    try:
        if req.kind == "game":
            return channel.Channel(session.load(sid)).wait_ready(
                req.ready_timeout, lambda: _alive_or_starting(sid))
        _wait_window(sid, req.ready_timeout)
        return {}
    except HarnessError as e:
        if e.code != EXIT_ENGINE:
            raise
        raise _early_exit_error(sid, e) from None


_NAMESPACE_WORDS = ("unshare:", "nsenter:", "ip:")


def _early_exit_error(sid: str, err: HarnessError) -> HarnessError:
    """Turn "the engine exited before it was ready" into an error that says why, from engine.log's
    tail. The unshare/nsenter/ip prefix runs in front of the engine, so when the private network
    cannot be made (user namespaces disabled, iproute2 missing) the only evidence is that log line;
    without this the user got "run crash_report" for an engine that never ran (QA SL-2, TA-4)."""
    tail = match._log_tail(session.run_dir(session.load(sid)) / "engine.log")
    if any(w in tail for w in _NAMESPACE_WORDS):
        return HarnessError(EXIT_LAUNCH,
                            f"the engine could not start in its private network: {tail}",
                            "user namespaces or `ip` are unavailable on this host; "
                            "launch with --network real to use the machine's network")
    return HarnessError(err.code, err.message, f"{err.hint} (engine.log: {tail})")


def _warnings(req: LaunchRequest, found: dict) -> list[str]:
    """Return launch warnings: an OpenGL game whose renderer could not be confirmed (SF-16)."""
    if req.role == "standalone":
        return []                     # draws nothing; "renderer unknown" would be noise
    if req.kind == "game" and req.renderer == "opengl" and found["renderer"] == "unknown":
        return ["renderer unknown: the engine had mapped neither Mesa GL nor a Vulkan driver, so "
                "the harness cannot confirm what draws the frames"]
    return []


def launch(req: LaunchRequest) -> dict:
    """Start a session and return it once ready.

    Returns {id, display, renderer, libraries, state, bridge_refreshed, errors, last_error,
    switches, warnings, sound, match, role, port, holder_pid}; errors/last_error/switches come
    from the bridge heartbeat (contract C3); sound is {sink, streams} for a --sound session.
    Every failure after the session exists stops it and carries details["id"] (QA CR-5, TA-4).
    """
    sess, refreshed = _start(req)
    try:
        hb = _wait_ready(req, sess.id)
        sess = session.load(sess.id)
        if req.role == "standalone":
            found = {"renderer": "none", "libraries": []}      # no window, nothing mapped
        else:
            found = renderer.classify(renderer.read_maps(sess.engine_pid or 0))
        if req.kind == "game" and req.renderer == "vulkan" and found["renderer"] != "vulkan":
            raise HarnessError(EXIT_LAUNCH,
                               f"asked for Vulkan, engine came up as {found['renderer']}",
                               "check VK_ICD_FILENAMES and that the engine was built with Vulkan")
        # Inside the try: a stream on another sink (or none) fails the launch and stops the
        # engine. The engine has opened its audio device by now (the bridge reported a state).
        routing = None
        if sess.sound:
            routing = sound.verify_routing(sess.engine_pid or 0, sess.id)
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
            "warnings": _warnings(req, found), "sound": routing, "match": sess.match,
            "role": sess.role, "port": sess.port,
            "holder_pid": match.load(sess.match).holder_pid if sess.match else None}


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
    network = _network_state(sess)
    out = {**asdict(sess), "alive": alive(sess), "heartbeat": hb, "heartbeat_age_sec": age,
           "stale": None if age is None else age > STALE_SEC, "match_network": network,
           "listening": (listening(sess) if sess.role == "standalone" and network == "alive"
                         else None)}
    if network == "dead" and sess.status in ("starting", "running"):
        out["status"] = "dead-network"     # see _network_state
    return out


def _network_state(sess: session.Session) -> str | None:
    """Return "alive" | "dead" for a match session, None for an ordinary one.

    Spec section 5.6: the session record is not rewritten; the read says what the record cannot
    know. The MATCH record is written back once ("dead", one audit line) by match.observe. The
    engine may still be running: a member process holds the namespace until it exits, and its
    own scope can still stop it.
    """
    if not sess.match:
        return None
    return "alive" if match.alive(match.observe(match.load(sess.match))) else "dead"


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
    sink_error = None
    if sess.sound_module is not None:
        # After the engine is gone: its streams go with it (node.dont-reconnect would leave them
        # silent anyway). A recorder still on the monitor without that property would be moved
        # to the desktop's audio, so recorders stop first (README "Sound").
        sink_error = sound.remove_sink(sess.sound_module, sid)

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
    out = {"id": sid, "status": sess.status, "exit_info": sess.exit_info,
           "runner_first": runner_first}
    if sink_error:
        out["sound_sink_error"] = sink_error + " (`fsoctl cleanup` retries)"
    return out


def sessions_in_match(mid: str) -> list[session.Session]:
    """Return the sessions launched into match `mid` (the `match` field; None for ordinary ones)."""
    return [s for s in session.list_sessions() if getattr(s, "match", None) == mid]


def remove_match(mid: str) -> dict:
    """Stop every session in the match (normal stop path, runner first), then end the holder.

    WHY sessions first (spec section 5.6): an engine must never outlive its network. (A member
    process keeps the namespace alive on its own and could still be entered through its pid; the
    point is bookkeeping and the agent's view, not kernel reachability.) WHY a short critical
    section (QA SL-4/CR-1/TA-8): the launch lock makes "removing" visible to a concurrent launch
    before the holder dies, so no engine is born into a network being removed; the stops run
    OUTSIDE the lock because one can take 30 s and would starve every other launch. WHY continue
    past a failed stop (QA SA-6/SL-12): a scope that survived SIGKILL is stuck in the kernel
    either way; the holder still ends and the failure is reported.
    """
    with session.file_lock(paths.runtime_root() / "launch.lock", _LOCK_WAIT):
        m = match.load(mid)
        if m.status in ("alive", "dead"):
            m.status = "removing"
            match.save(m)
        targets = [s for s in sessions_in_match(mid) if s.status in ("starting", "running")]
    stopped, failed = [], []
    for s in targets:
        try:
            stop(s.id)
            stopped.append(s.id)
        except HarnessError as e:
            failed.append({"id": s.id, "error": e.message})
    match.end_holder(m)
    return {"id": mid, "stopped": stopped, "failed": failed, "status": "removed"}


def list_all() -> dict:
    """Return a short line per session."""
    rows = []
    for s in session.list_sessions():
        status = s.status
        if s.status in ("starting", "running") and _network_state(s) == "dead":
            status = "dead-network"          # same read-time rule as status() (QA TA-4)
        rows.append({"id": s.id, "kind": s.kind, "status": status, "alive": alive(s),
                     "mission": s.mission, "renderer": s.renderer, "role": s.role,
                     "network": s.network,
                     "match": s.match})
    return {"sessions": rows}


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

    Returns {"removed": [ids], "removed_folders": [paths], "failed": [{path, error}],
    "removed_sinks": [names], "kept_sinks": [names]} (session sound sinks); a folder
    that cannot be deleted is reported, not fatal. Runs under launch.lock (R4), so no launch is
    between staging and saving its session while cleanup decides what is unreferenced.
    """
    with session.file_lock(paths.runtime_root() / "launch.lock", _LOCK_WAIT):
        return _cleanup_locked(older_than_sec)


def _cleanup_sinks(kept: list[session.Session], failed: list[dict]) -> dict:
    """Unload this runtime root's session sinks whose session is not starting or running (it
    exited by itself, a stop could not reach the sound server, fsoctl crashed).

    Returns {"removed_sinks": [names], "kept_sinks": [names a recorder still captures]}. Other
    roots' sinks are never touched (sound.orphan_sinks). A machine without pactl has no sinks,
    so that case is silent; any other pactl failure is reported in `failed`. The caller holds
    launch.lock, so a session of this root between create_sink and its scope is "starting".
    """
    out: dict = {"removed_sinks": [], "kept_sinks": []}
    live = {s.id for s in kept if s.status in ("starting", "running")}
    try:
        orphans, out["kept_sinks"] = sound.orphan_sinks(live)
    except sound.PactlMissing:
        return out
    except HarnessError as e:
        failed.append({"path": "pulse sinks", "error": e.message})
        return out
    for module, name in orphans:
        err = sound.remove_sink(module, name.rsplit("_", 1)[1])
        if err:
            failed.append({"path": f"pulse sink {name}", "error": err})
        else:
            out["removed_sinks"].append(name)
    return out


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
    sinks = _cleanup_sinks(kept, failed)
    # After the session pass, so a match whose last session was just removed goes in the same
    # run. A dead or removed match with no session left is just a folder; same age rule.
    for m in match.list_matches():
        if match.alive(m) or sessions_in_match(m.id) or now - m.created < older_than_sec:
            continue
        try:
            shutil.rmtree(paths.match_dir(m.id))
            removed.append(m.id)
        except OSError as e:
            failed.append({"path": str(paths.match_dir(m.id)), "error": str(e)})
    return {"removed": removed, "removed_folders": folders, "failed": failed, **sinks}


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
    p.add_argument("--match", help="multiplayer: the match id from `fsoctl match create`")
    p.add_argument("--role", choices=ROLES, help="multiplayer: standalone | host | client")
    p.add_argument("--port", type=int, help="multiplayer: local UDP port (default: next free)")
    p.add_argument("--connect", help="client: server IP:PORT (default: the match's live server)")
    p.add_argument("--game-name", help="host/standalone: game name (default: the match id)")
    p.add_argument("--closed", action="store_true", help="host: start the game closed")
    p.add_argument("--network", choices=NETWORKS, default="private",
                   help="private: own loopback-only network (default); real: the machine's")
    p.add_argument("--sound", action="store_true",
                   help="play audio into the session's own null sink (named in the result's "
                        "sound.sink; record <sink>.monitor); default: -nosound")
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
        ready_timeout=args.ready_timeout, match=args.match, role=args.role, port=args.port,
        connect=args.connect, game_name=args.game_name, closed=args.closed,
        network=args.network, sound=args.sound)
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
