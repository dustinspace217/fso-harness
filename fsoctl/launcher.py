"""launch / status / stop / list / cleanup.

A launch: create the session, install the bridge, hand the bridge its startup switches, start the
runner in a named scope, wait (bounded) for the bridge heartbeat (game) or the window (qtFRED),
then check which renderer really came up and fail loudly on a Vulkan request that did not get
Vulkan.
"""
import argparse
import errno
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import (bridge_install, channel, match, paths, platform, renderer, sandbox,
               session, sound, xwin)
from .cli import register
from .errors import (EXIT_ENGINE, EXIT_LAUNCH, EXIT_NOT_FOUND, EXIT_REFUSED, EXIT_TIMEOUT,
                     EXIT_USAGE, HarnessError)
from .platform import procs, safefs, scope

DEFAULT_MAX_SESSIONS = 3
STALE_SEC = 5.0         # contract C3: a heartbeat older than this is stale
_LOCK_WAIT = 60.0       # seconds to wait for launch.lock (held for one install + scope start)
_STARTING_GRACE = 120.0  # cleanup leaves a "starting" record alone this long (see _removable)
AUTO_CLEAN_AGE = 86400.0   # each launch deletes finished runs older than a day (_auto_cleanup)
# A crashed run's record and logs stay a week for crash_report; bounded so nothing grows forever.
AUTO_CLEAN_CRASHED_AGE = 7 * 86400.0
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
DEFAULT_WEB_PORT = 8080                       # webapiPort in the engine's multi_options.h


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
    network: str | None = None           # one of NETWORKS; None = not given (resolve_network)
    sound: bool = False                  # play audio into the session's own sink (sound.py)
    visible: bool = False                # Windows: run on the user's desktop, not a hidden one

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
    elif sess.network == "real" and sess.port:
        argv += ["-port", str(sess.port)]
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


_PORT_TRIES = 64     # candidate ports host_port / web_port test before giving up


def _socket_listing(argv: list[str]) -> subprocess.CompletedProcess:
    """Run the socket listing for port_free on Windows; EXIT_LAUNCH when it cannot be had.

    WHY not the lookup's own way of running it (QA Plan B, CR-4 / ST-9): that one answers
    "no owners" when the listing fails. For "does my engine hold this port" that is the
    careful answer. Here it would read as "every port is free", and every session would be
    handed the same port. Decoded with errors="replace": the listing's headers may be in
    the system's language and in another code page (not run on a non-English Windows), and
    for UDP rows only digits and the protocol column are read (a TCP row is still matched by
    the English word LISTENING: DEF-XB-16).
    """
    try:
        r = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=10,
                           check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HarnessError(EXIT_LAUNCH, f"cannot list the machine's sockets ({argv[0]}: {e})",
                           "a port cannot be tested without the listing; run "
                           f"`{' '.join(argv)}` yourself to see what is wrong") from e
    if r.returncode != 0:
        raise HarnessError(EXIT_LAUNCH, f"cannot list the machine's sockets ({argv[0]} exited "
                           f"{r.returncode})", "a port cannot be tested without the listing; "
                           f"run `{' '.join(argv)}` yourself to see what is wrong")
    return r


def port_free(proto: str, port: int) -> bool:
    """True when this host would let the engine bind `port` now ("udp" or "tcp").

    UDP is tested the way the engine binds its game socket: IPv6 dual stack on [::], no
    SO_REUSEADDR (psnet2.cpp psnet_set_socket_options / psnet_init_socket). TCP the way the web
    panel's mongoose binds: IPv4 on 0.0.0.0 with SO_REUSEADDR (mongoose.c set_ports_option;
    USE_IPV6 is not defined in FSO's build). WHY a test bind and not only the records: the
    records know this runtime root's sessions only, and a stock game, another runtime root or
    any program on the port leaves the engine with networking off or no panel (both carry on
    after a failed bind) while the readiness check could mistake that program for it (QA CR-1,
    SL-1, CR-2). The socket is closed at once; the launch lock keeps launches in one runtime root
    from racing for it, but another root or another program still can (for a standalone the
    readiness owner check, engine_holds, catches that).
    Raises EXIT_LAUNCH on any failure other than "address in use".

    On Windows nothing is bound: the port counts as free when no process holds it in the
    socket table, in either address family (procs.socket_owner_pids, `netstat`). WHY: a
    program that binds a UDP port on every address makes Windows Firewall add a rule for it
    and ask the user whether to allow it. Measured in the VM on 2026-11-16 (probe-results
    section 23): the rule for python.exe appeared one second after the first session was
    created and the question was on the desktop; the same bind from a program with no rule
    added one at once. A session must never put a window in front of the user. What the
    lookup cannot see is a port Windows would refuse for another reason; the engine then
    starts without networking and _check_game_port says so. (The engine's own bind gets a
    rule once per engine file, as the stock game's does; the harness adds no firewall rule
    to prevent it.)
    """
    if platform.NAME == "windows":
        return not procs.socket_owner_pids(proto, port, run=_socket_listing)
    if proto == "udp":
        try:
            sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            # No IPv6 on this host: the engine creates only an IPv6 socket and runs with
            # networking off (psnet2.cpp psnet_init_socket), whatever port it is given, so no
            # choice of port matters here (plan A QA XFA-5).
            return True
        addr: tuple = ("::", port)
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        addr = ("0.0.0.0", port)
    with sock:
        try:
            sock.bind(addr)
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                return False
            raise HarnessError(EXIT_LAUNCH, f"cannot test {proto} port {port}: {e}",
                               "check the host's network setup") from e
    return True


def _host_game_port(s: session.Session) -> int | None:
    """Return the UDP port a session's engine binds on the HOST, or None (a private session or a
    private-match session binds inside its own namespace; qtFRED binds nothing).

    A record from before Task 1 has a real-network game session with no port: its engine bound
    the default 7808 (QA CR-6/SL-3), and so did a pre-2026-10-13 record whose network is None.
    """
    if s.kind != "game":
        return None
    if s.network == "real" or (s.network is None and not s.match):
        return s.port or DEFAULT_GAME_PORT
    return None


def host_port(wanted: int | None) -> int:
    """Return the UDP game port for a new session on the real network: `wanted` if no live
    real-network session holds it and the host would let the engine bind it (EXIT_USAGE
    otherwise), else the first such port from DEFAULT_GAME_PORT up (EXIT_LAUNCH after
    _PORT_TRIES candidates).

    WHY a port per session: the engine binds its game port on every interface at startup, and
    when the bind fails it logs and carries on with networking off rather than exiting
    (psnet2.cpp), so a second session on 7808 would run with a silently dead network. Sessions
    in real matches count too (their engines bind on the host as well); private sessions and
    private-match sessions do not: their ports live in their own namespaces.
    """
    used = {_host_game_port(s) for s in session.list_sessions() if not _gone(s)}
    if wanted is not None:
        if wanted in used:
            raise HarnessError(EXIT_USAGE, f"port {wanted} is already used by another session",
                               "leave --port unset to take the next free one")
        if not port_free("udp", wanted):
            raise HarnessError(EXIT_USAGE, f"port {wanted} is held by another program on this "
                               "host", "leave --port unset to take the next free one")
        return wanted
    for port in range(DEFAULT_GAME_PORT, DEFAULT_GAME_PORT + _PORT_TRIES):
        if port not in used and port_free("udp", port):
            return port
    raise HarnessError(EXIT_LAUNCH, f"no free UDP game port in {DEFAULT_GAME_PORT}.."
                       f"{DEFAULT_GAME_PORT + _PORT_TRIES - 1}", "stop some sessions or games")


def web_port() -> int:
    """Return the web panel's TCP port for a new standalone on the real network: the first port
    from DEFAULT_WEB_PORT up that no live real-network standalone holds and the host would let
    the panel bind (port_free), EXIT_LAUNCH after _PORT_TRIES candidates. A private standalone's
    panel lives in its namespace, so it is not counted (it always gets DEFAULT_WEB_PORT). A
    program that takes the port between this test and the engine's bind still wins; launch then
    reports no panel (web_panel_up).
    """
    used = {s.web_port for s in session.list_sessions()
            if s.web_port and s.network == "real" and not _gone(s)}
    for port in range(DEFAULT_WEB_PORT, DEFAULT_WEB_PORT + _PORT_TRIES):
        if port not in used and port_free("tcp", port):
            return port
    raise HarnessError(EXIT_LAUNCH, f"no free TCP web panel port in {DEFAULT_WEB_PORT}.."
                       f"{DEFAULT_WEB_PORT + _PORT_TRIES - 1}", "stop some standalones")


def web_login() -> tuple[str, str]:
    """Return a fresh (user, password) for a standalone's web panel.

    WHY random and per session: the engine's default login is admin/admin (multi_options.h), and
    on the real network the panel is on the LAN; its API can kick players and change the server.
    The values hold only [A-Za-z0-9_-]: multi.cfg takes the rest of the line as the value,
    trimmed (multi_options.cpp NEXT_TOKEN), so a newline or leading/trailing whitespace would
    change it.
    """
    return f"fsoh-{secrets.token_hex(3)}", secrets.token_urlsafe(18)


def web_panel(sess: session.Session) -> dict | None:
    """Return {port, user, password} for a standalone, None for every other session."""
    if sess.web_port is None:
        return None
    return {"port": sess.web_port, "user": sess.web_user, "password": sess.web_password}


_CFG_LINE_MAX = 511          # the engine reads multi.cfg lines into a 512-byte buffer
_CGI_SUFFIXES = (".cgi", ".pl", ".php")   # names the engine's web server treats as scripts


def _check_web_root(web_root: str | None) -> None:
    """Refuse (EXIT_LAUNCH) a web root the engine would not use exactly as given.

    - It must be one line: no newline or carriage return (the engine's line reader drops "\r").
    - Its `+webui_root` line must fit the engine's 512-byte line buffer (multi_options.cpp
      cfgets(str, 512)): a longer one is cut, and a cut can land on the session's run folder,
      whose session.json holds the login (plan A QA XFA-2).
    - No folder on its path may end in .cgi, .pl or .php: the engine's web server gives such
      names a meaning of their own, so a root under one would not be used as given (plan A QA
      XFA-1).
    """
    if not web_root or "\n" in web_root or "\r" in web_root:
        raise HarnessError(EXIT_LAUNCH, "a web panel needs a one-line document root",
                           "this is a harness bug; report it")
    if len(f"+webui_root {web_root}".encode()) > _CFG_LINE_MAX - 1:
        raise HarnessError(EXIT_LAUNCH, f"the runtime folder path is too long for the engine's "
                           f"config line ({len(web_root)} characters)",
                           "set FSO_HARNESS_RUNTIME to a shorter folder")
    bad = [part for part in Path(web_root).parts if part.lower().endswith(_CGI_SUFFIXES)]
    if bad:
        raise HarnessError(EXIT_LAUNCH, f"the runtime folder path has a folder named {bad[0]!r}, "
                           "which the engine's web server would treat as a script",
                           "set FSO_HARNESS_RUNTIME to a path with no .cgi/.pl/.php folder")


def multi_cfg_text(game_name: str, port: int | None, user: str | None,
                   password: str | None, web_root: str | None = None) -> str:
    """Return a match session's multi.cfg: its name and, for a standalone (port given), its web
    panel's port, login and document root; never +pxo. Only the standalone starts the panel
    (multi_options.cpp: std_configLoaded runs when Is_standalone), so other roles get no web
    lines. Raises EXIT_LAUNCH for a panel without a root, or a root holding a newline.

    The root is required (QA SA-1, 2026-10-17, in that review's record): an empty folder as
    the root leaves nothing to serve, run or list.
    """
    text = f"+name {game_name}\n"
    if port is not None:
        _check_web_root(web_root)
        text += (f"+webapi_server_port {port}\n+webapi_username {user}\n"
                 f"+webapi_password {password}\n+webui_root {web_root}\n")
    return text


def assign_port(mid: str, wanted: int | None) -> int:
    """Return the local port for a new session in match `mid`: `wanted` if free there, else the
    first free port from DEFAULT_GAME_PORT up. Every engine binds [::]:<port>, so two sessions in
    one namespace cannot share a port (probe-results 21.2; plan Review Focus 1). A real match has
    no namespace: its sessions share the host with every other real-network session, so the
    host-wide rule applies (host_port)."""
    if match.load(mid).network == "real":
        return host_port(wanted)
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
    constant). A real match with no live server is refused (EXIT_USAGE): its server's port is
    not known in advance, and 7808 on the host may be another match's server or a real game."""
    for s in sessions_in_match(mid):
        if s.role in ("standalone", "host") and s.port and not scope.is_gone(s.scope):
            return f"127.0.0.1:{s.port}"
    if match.load(mid).network == "real":
        raise HarnessError(EXIT_USAGE, f"match {mid} has no running server to join",
                           "launch its standalone or host first, or pass --connect IP:PORT")
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


def _known_alive(pid: int | None, start: int | None) -> bool:
    """True when a recorded process is known to run still: a process has its pid, has not
    ended, and started at the recorded time. False without a recorded start time (every
    Linux record): there the process group's own answer is complete.

    WHY "has not ended" is asked separately: while anyone holds a handle to it, an ended
    Windows process can still be opened and still answers with its start time
    (tests/winlive/test_procs_live.py holds Windows to that)."""
    return bool(pid and start is not None and procs.pid_alive(pid)
                and procs.pid_start(pid) == start)


def _known_gone(pid: int | None, start: int | None) -> bool:
    """True only when it is known that a recorded process no longer runs: no process has
    its pid, or the process that has it now started at another time (the pid was reused).
    False when no process is recorded and when the OS will not say: a lookup that failed
    is never read as "gone" (a session started at another privilege level may fail every
    lookup; whether it does was not measured)."""
    if not pid:
        return False
    if procs.pid_ended(pid):
        return True
    started = procs.pid_start(pid)
    return started is not None and start is not None and started != start


def _runner_known_alive(sess: session.Session) -> bool:
    """True when the session's recorded runner is known to run still (_known_alive)."""
    return _known_alive(sess.runner_pid, sess.runner_start)


def _engine_known_alive(sess: session.Session) -> bool:
    """True when the session's recorded engine is known to run still (_known_alive)."""
    return _known_alive(sess.engine_pid, sess.engine_start)


def _runs_out_of_sight(sess: session.Session) -> bool:
    """True when the session runs where this login cannot see it: its process group is not
    found here, yet its recorded runner or engine is known alive (a session of another
    Windows login).

    WHY the group is asked first (round-2 STF-1): "a recorded process is alive and the
    session is not" is also true in the session's own login: for the half second in which
    the runner outlives an engine that exited or crashed, while a session starts, and for
    as long as a runner is stuck. There the group is visible; the session is over or
    starting, and the caller must get the ordinary answer (a crash report, its report).
    Where groups are seen from every login this is never true and the record is not asked
    (round-2 TAF-11)."""
    return (platform.GROUPS_PER_LOGIN and scope.is_gone(sess.scope)
            and (_runner_known_alive(sess) or _engine_known_alive(sess)))


def _gone(sess: session.Session) -> bool:
    """True once nothing of the session runs, as far as that can be known: its process
    group is gone, and what the record says of its runner and engine agrees.

    WHY more than the group (QA Plan B, ST-4 / TA-12): on Windows a job's name is known
    only inside the Windows login that made it. Asked from another login (ssh, a service),
    a live session's group is "not found", which read as gone: cleanup would delete what
    it could of a live session's folder, its record included, and the session would not
    count against the session cap or keep its port. The runner's and the engine's pid and
    start time are in the record and can be asked about from any login of the same account
    (measured in the VM, 2026-11-17, for the runner: from an ssh login the desktop
    session's job was not found, its runner's start time was read, and `cleanup
    --older-than 0` left the session alone).

    The rule, where start times are recorded (Windows). A record with no end yet is gone
    only when its runner AND its engine are KNOWN gone (_known_gone): "not known alive" is
    not enough (cross-family review XFA-1, read in the code: a runner that cannot be asked
    about, or an engine that outlived its runner, would lose its record to a cleanup from
    another login; with this rule the second case was run in the VM and the record stayed). A
    record that has its end (the runner wrote it after the engine was over) is gone unless
    one of the two is known to run still, so a lookup that fails cannot keep an ended
    session's folder for good. The cost of the first half: an unended record whose pid now
    belongs to a process that cannot be asked about stays until that process ends.

    Where a group's name is seen from every login (Linux), and for a record without start
    times, this is the group's answer alone, as before: the record is not asked, whatever
    it holds (round-2 SAF-1). On Windows the record is still asked in the caller's own
    login, where the group's answer would be enough; DEF-XB-24 has what that costs.
    """
    if not scope.is_gone(sess.scope):
        return False
    if not platform.GROUPS_PER_LOGIN or sess.runner_start is None:
        return True
    if sess.status not in ("starting", "running"):
        return not (_runner_known_alive(sess) or _engine_known_alive(sess))
    return _runner_known_gone(sess) and (not sess.engine_pid
                                         or _known_gone(sess.engine_pid, sess.engine_start))


def alive(sess: session.Session) -> bool:
    """Return True while the session's engine process exists and its scope is active.

    The pid check runs first and spawns nothing; systemd is asked only when the pid exists, and
    at most once a second per session (QA CR-13: polling loops call this every 50 ms, and each
    call used to spawn `systemctl`). A dead engine pid answers False at once, uncached.
    """
    if not sess.engine_pid or not procs.pid_alive(sess.engine_pid):
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
    """Refuse a new launch when the concurrent-session cap is reached or the machine is short of
    available RAM (scope.room_for_engine); return the live sessions.

    Counts every session whose scope is not fully gone: starting, running, or still stopping
    ("deactivating" scopes still hold their engine and Xvfb; QA SL-1, Y-6). Call it under
    launch.lock, as _start() does; otherwise two launches can both pass it.
    """
    cap = int(os.environ.get("FSO_HARNESS_MAX_SESSIONS", DEFAULT_MAX_SESSIONS))
    live = [s for s in session.list_sessions() if not _gone(s)]
    if len(live) >= cap:
        raise HarnessError(EXIT_REFUSED, f"{len(live)} sessions already running (cap {cap})",
                           "stop one with `fsoctl stop <id>` or raise FSO_HARNESS_MAX_SESSIONS")
    reason = scope.room_for_engine()
    if reason:
        raise HarnessError(EXIT_REFUSED, f"no memory room for another engine: {reason}",
                           "close other programs or sessions (`fsoctl stop <id>`) and launch again")
    return live


def _pilot_files(src: Path, name: str) -> dict[str, bytes] | None:
    """Return {file name: contents} of one pilot's files (NAME.json, NAME.*.csg) in `src`.

    A source inside a folder engines write (the runtime root, or the sandbox sessions' prefs root
    on disk: a replay copies from an earlier session's prefs) is read through safefs: links,
    FIFOs and other non-files are skipped, never followed (QA 2026-11-13 SA-3). Returns None for
    any other source: a folder the user named may legitimately be or contain links, so the
    caller copies it as before (Phase B refinement b).
    """
    for root in _engine_roots():
        # Inside the root as written, or once its links are resolved (a link INTO a run folder
        # must not turn the strict read off).
        for base, cand in ((root, Path(os.path.abspath(src))), (root.resolve(), src.resolve())):
            if cand.is_relative_to(base) and cand != base:
                return _run_folder_pilot_files(base, str(cand.relative_to(base)), name)
    return None


def _engine_roots() -> list[Path]:
    """Return the folders engines write into: the runtime root, the sandbox prefs root, and the
    sandbox root each existing session recorded (QA CR3-3: an older session's prefs_root may sit
    under another root, e.g. after XDG_CACHE_HOME changed)."""
    roots = [paths.runtime_root(), paths.sandbox_root()]
    for s in session.list_sessions():
        if s.prefs_root and Path(s.prefs_root).parent.parent not in roots:
            roots.append(Path(s.prefs_root).parent.parent)
    return roots


def _run_folder_pilot_files(root: Path, rel: str, name: str) -> dict[str, bytes]:
    """Return {file name: contents} of the pilot's regular files in root/rel, via safefs."""
    out = {}
    for entry in safefs.names(root, rel):
        if entry.startswith(name + ".") and Path(entry).suffix in (".json", ".csg"):
            try:
                out[entry] = safefs.read(root, f"{rel}/{entry}")[0]
            except OSError:
                continue              # a link or a non-file left by that session's engine
    return out


def _copy_pilot(src: Path, name: str, prefs: Path) -> None:
    """Copy one pilot's files (NAME.json, NAME.*.csg) from a players folder into the session
    (a fresh prefs folder no engine has run in yet): through safefs from an engine-written
    folder, with shutil.copy2 (contents, mode and times, QA CR2-3) from a folder the user named."""
    try:
        strict = _pilot_files(src, name)
        loose = (None if strict is not None else
                 [p for p in sorted(src.glob(f"{name}.*")) if p.suffix in (".json", ".csg")])
    except OSError as e:
        raise HarnessError(EXIT_LAUNCH, f"cannot read the pilot folder {src}: {e}",
                           "point --pilot-source at a data/players folder") from None
    if not strict and not loose:
        raise HarnessError(EXIT_LAUNCH, f"no pilot files for {name} in {src}",
                           "point --pilot-source at a data/players folder that has NAME.json")
    dest = prefs / "data" / "players"
    dest.mkdir(parents=True, exist_ok=True)
    for file_name, data in (strict or {}).items():
        (dest / file_name).write_bytes(data)
    for f in loose or []:
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
        # Per comma-separated part (QA SA-5/CR-5): the engine splits -mod on commas, so an entry
        # "X,.." passed the whole-string check while naming FS2PATH's parent.
        # A backslash and a colon too, on every system (QA Plan B, SA-2): on Windows they
        # are a separator and a drive, and "..\\x" or "C:x" pointed the engine elsewhere
        # while this check knew "/" only. No FSO mod folder needs either character.
        # And a part made only of dots and spaces (".. ", "..."): no mod folder is named so,
        # and Windows is said to drop trailing dots and spaces from a name, which could
        # leave "..". (One check in the VM, 2026-11-17: Python's listing of
        # "C:/Windows/.. " was "path not found", so that form did not reach the parent.
        # The engine builds its paths itself and was not tried; the refusal costs nothing.)
        for part in mod.split(","):
            if not part.rstrip(". ") or any(c in part for c in "/\\:\0"):
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
    # hasattr: Windows has no user ids of this kind (and no root to refuse).
    if hasattr(os, "getuid") and os.getuid() == 0:   # spec 5.4 (QA TA-10): as match create
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
    if req.runtime_sec > match.HOLDER_RUNTIME_SEC and resolve_network(req) == "private":
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
    m = match.observe(match.load(str(req.match)))    # EXIT_NOT_FOUND
    if not match.alive(m):
        raise HarnessError(EXIT_REFUSED, f"match {req.match} is dead",
                           "create a new one with `fsoctl match create`")
    live = [s for s in sessions_in_match(str(req.match)) if not scope.is_gone(s.scope)]
    if req.role in ("standalone", "host") and any(s.role in ("standalone", "host") for s in live):
        # One server per match (QA XFK-2): a client's default connect is "the match's server",
        # which two would make ambiguous, and in a private match both panels would take 8080.
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
_SANDBOX_PILOT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# The engine takes a port <= 1023 as its default (psnet2.cpp:441-443, psnet_init), and a port
# is a 16-bit value, so a recorded port and the bound one would disagree (QA SA-4).
PORT_MIN, PORT_MAX = 1024, 65535


NETWORKS = ("real", "private", "sandbox")   # "sandbox": private + bwrap, see fsoctl/sandbox.py
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
# boundary; nothing the engine needs is on the real network. `--network private` is the opt-in;
# the real network is the default (plan A Task 1).
# The prefix execs the program via "$@" so no argument is re-parsed by the shell and the
# engine's pid is the pid Popen returns (unshare and sh both exec; probed 2026-10-13).
PRIVATE_NET_PREFIX = ["unshare", "--user", "--map-root-user", "--net", "sh", "-c",
                      'ip link set lo up && exec "$@"', "sh"]


def resolve_network(req: LaunchRequest) -> str:
    """Return the network a session will use: a match session's is its match's (EXIT_NOT_FOUND
    for an unknown match), otherwise what was asked for, and "real" when nothing was.

    WHY "real" by default: the owner's ruling (2026-10-16), "Real network. I never meant to change
    this about how the engine works." The stock engine opens its UDP game port on every
    interface at startup; the private network is kept as an opt-in (`--network private`).
    """
    if req.match:
        return match.load(req.match).network
    return req.network or "real"


def _check_network(req: LaunchRequest) -> None:
    """Refuse (EXIT_USAGE) an unknown network choice, or an explicit network on a match session
    that differs from its match's (a match session always lives in its match's network)."""
    if req.network is not None and req.network not in NETWORKS:
        raise HarnessError(EXIT_USAGE, f"unknown --network {req.network!r}",
                           f"use one of {', '.join(NETWORKS)}")
    if req.match and req.network not in (None, resolve_network(req)):
        raise HarnessError(EXIT_USAGE, "a match session always runs in its match's network",
                           "drop --network; choose the network at `fsoctl match create`")
    if resolve_network(req) == "private" and not req.match:
        for tool in ("unshare", "ip"):     # checked here so a missing tool never starts a scope
            if not shutil.which(tool):
                raise HarnessError(EXIT_LAUNCH,
                                   f"cannot make a private network: `{tool}` not found",
                                   "install util-linux and iproute2, or launch with --network real")
        if os.geteuid() == 0:              # same rule as match.create (QA SA-5)
            raise HarnessError(EXIT_REFUSED, "refusing to start a private-network session as root",
                               "run the harness as an ordinary user")
    if resolve_network(req) == "sandbox":
        _check_sandbox(req)


def _check_sandbox(req: LaunchRequest) -> None:
    """Refuse (EXIT_USAGE / EXIT_REFUSED / EXIT_LAUNCH) what a sandbox session cannot do.

    Only an ordinary OpenGL game session runs sandboxed. Match sessions take their match's
    network (refused by _check_network before this). Vulkan needs the GPU, which the sandbox does
    not bind (Dustin's ruling P3, 2026-11-13). qtFRED is refused because the sandbox was built and
    tested for game cells only (what the nightly review plays). --sound is refused HERE ONLY
    (Dustin's trim, 2026-11-13: never for ordinary sessions): sound.check_device_names runs `ldd`
    on the build and loads the build's own libopenal in a host Python process, outside any
    sandbox.
    """
    # The hints never suggest another network (QA CR-2): a sandbox session runs a build nobody
    # has vouched for, and the way out of a refusal must not be to run it unsandboxed.
    if req.kind != "game":
        raise HarnessError(EXIT_USAGE, "only game sessions can run in the sandbox",
                           "qtFRED does not run sandboxed")
    if req.renderer != "opengl":
        raise HarnessError(EXIT_USAGE, "the sandbox has no GPU: OpenGL only", "drop --vulkan")
    if req.pilot_name and not _SANDBOX_PILOT_RE.fullmatch(req.pilot_name):
        raise HarnessError(EXIT_USAGE, f"pilot name {req.pilot_name!r} is not allowed in the "
                           "sandbox", "use letters, digits, _ and - only")
    if req.pilot_source and not any(_inside(req.pilot_source, r) for r in _engine_roots()):
        # SA-3: a sandbox session copies a pilot only from a harness run folder (a replay).
        raise HarnessError(EXIT_REFUSED, "a sandbox session takes a pilot only from a harness "
                           "run folder", "use pilot create:<name>, or replay a session")
    if req.sound:
        raise HarnessError(EXIT_REFUSED, "--sound is refused in the sandbox: it loads the build's "
                           "OpenAL library in a host process", "drop --sound")
    for tool, package in ((sandbox.BWRAP, "bubblewrap"), (sandbox.PRLIMIT, "util-linux"),
                          (sandbox.KEYCTL, "keyutils")):
        if not shutil.which(tool):
            raise HarnessError(EXIT_LAUNCH, f"cannot make a sandbox: `{tool}` not found",
                               f"install {package} (Fedora: dnf install {package})")
    if os.geteuid() == 0:
        raise HarnessError(EXIT_REFUSED, "refusing to start a sandbox session as root",
                           "run the harness as an ordinary user")
    free, floor = sandbox.free_bytes(paths.sandbox_root()), sandbox.free_min_bytes()
    if free < floor:
        # The watchdog would end the session at its first check (QA CR3-1); say why up front.
        raise HarnessError(EXIT_REFUSED, f"the sandbox disk has {free} bytes free, under the "
                           f"{floor}-byte floor", "free disk space, or point "
                           "FSO_HARNESS_SANDBOX_ROOT at a disk with room")
    _warn_shared_sandbox_disk()


def _warn_shared_sandbox_disk() -> None:
    """Warn (never refuse) when another sandbox session is live on the disk this one will use.

    WHY (QA AT5-1): each sandbox's watchdog measures how far the disk's free space has dropped
    since its own launch, which assumes one sandbox session per disk at a time (the nightly
    review runs its cells one after another). A second live sandbox's writes count against this
    one's drop and can end it early: the safe direction, but worth saying up front."""
    try:
        dev = os.stat(paths.sandbox_root()).st_dev
    except OSError:
        return                        # the floor check above already read this folder
    for s in session.list_sessions():
        if s.network != "sandbox" or s.status not in ("starting", "running") or not s.prefs_root:
            continue
        try:
            same = os.stat(s.prefs_root).st_dev == dev
        except OSError:
            continue                  # its folder is gone: it is not writing to this disk
        if same:
            sandbox.warn_loudly(f"sandbox session {s.id} is live on the same disk; its writes "
                                "count against this session's free-space drop limit")
            return


def _check_mission_name(req: LaunchRequest) -> None:
    """Refuse (EXIT_USAGE) a mission value that is not a plain file name, for EVERY game session:
    both -start_mission and -almission would otherwise carry it to the engine's re-split."""
    if req.kind == "game" and req.mission and not _MISSION_RE.fullmatch(req.mission):
        raise HarnessError(EXIT_USAGE, f"mission {req.mission!r} is not a plain file name",
                           "pass the mission's file name, e.g. SM1-01.fs2 (letters, digits, "
                           "_ . - and no leading - or .)")


def _check_multi_values(req: LaunchRequest) -> None:
    """Refuse (EXIT_USAGE) multiplayer values that could carry extra flags or config lines."""
    if req.game_name is not None and not _GAME_NAME_RE.fullmatch(req.game_name):
        raise HarnessError(EXIT_USAGE, f"game name {req.game_name!r} may only contain letters, "
                           "digits and _ . ' !", "the name reaches the engine command line and "
                           "multi.cfg; pick a plain one")
    if req.role == "client" and req.connect is not None and not _CONNECT_RE.fullmatch(req.connect):
        raise HarnessError(EXIT_USAGE, f"--connect {req.connect!r} is not IP:PORT",
                           "use an IP address and port, e.g. 127.0.0.1:7808 or [::1]:7808 "
                           "(hostnames are not resolved)")
    if req.port is not None and not PORT_MIN <= req.port <= PORT_MAX:
        raise HarnessError(EXIT_USAGE, f"--port {req.port} is outside {PORT_MIN}..{PORT_MAX}",
                           "the engine silently remaps ports below 1024 and wraps larger ones; "
                           "leave --port unset to take the next free one")


def _refuse_unavailable(req: LaunchRequest) -> None:
    """Refuse (EXIT_REFUSED) the launch options only Linux has so far, by name. Editors are
    Plan C; sound, matches and the private and sandbox networks are Plan E. A no-op on Linux."""
    if req.kind == "qtfred":
        platform.needs_linux("an editor session (kind=qtfred)", "Plan C")
    if req.sound:
        platform.needs_linux("--sound", "Plan E")
    if req.match or req.role:
        platform.needs_linux("a match session", "Plan E")
    if req.network in ("private", "sandbox"):
        platform.needs_linux(f"--network {req.network}", "Plan E")


def _check_run_folder() -> None:
    """Refuse (EXIT_LAUNCH) a runtime root the engine could not keep its preferences under:
    on Windows, one whose path has a letter outside ASCII (platform.PREFS_PATH_ASCII_ONLY
    has the engine's reason). The harness's own folder names under the root are ASCII, so
    the root decides. (A mod's name need not be ASCII, and the engine keeps a folder per
    mod under its preferences; that, and a game folder with such a letter, are not
    measured: README, "Not run".)

    WHY refuse instead of launching (probe 12, VM, 2026-11-17): the engine there never
    starts its bridge and does not close when asked, so the launch waits its whole time,
    ends the engine by force and reports "engine exited before the bridge started" with an
    empty log and nothing to say why. The default root is under the user's profile, so a
    user whose name has such a letter meets this on the first launch and needs to be told
    the way out."""
    if not platform.PREFS_PATH_ASCII_ONLY:
        return
    root = str(paths.runtime_root_path().absolute())
    if not root.isascii():
        raise HarnessError(
            EXIT_LAUNCH,
            f"the session folder {root} has a letter outside ASCII, and FSO on Windows "
            "cannot keep its preferences under such a path (the engine would never come "
            "up)",
            "set FSO_HARNESS_RUNTIME to a folder whose whole path is ASCII (for example "
            "C:\\fso-harness\\run; spaces are fine) and that only your account can write, "
            "then launch again. For the MCP server, run mcp\\register.ps1 again with the "
            "variable set: the server does not see your shell's variables")


def _validate(req: LaunchRequest) -> None:
    """Refuse a launch request before anything is created (EXIT_USAGE / EXIT_REFUSED /
    EXIT_LAUNCH)."""
    # First, before any file or match is looked at: on an OS without the feature the answer
    # is the refusal, not a later error about a missing tool (plan B Review Focus 5).
    _refuse_unavailable(req)
    _check_run_folder()
    if req.visible and platform.NAME != "windows":
        raise HarnessError(EXIT_USAGE, "--visible is a Windows option",
                           "drop --visible: here every session runs on its own virtual "
                           "display, never on your desktop")
    _check_binary(req.binary)
    _check_folders(req)
    _check_extra_args(req)
    _check_mission_name(req)
    # Role first (QA CR-9): it checks --match/--role pairing before anything reads the match,
    # which the network check does for a match session.
    _check_role(req)
    _check_network(req)
    if req.sound and (req.kind != "game" or req.role == "standalone"):
        raise HarnessError(EXIT_USAGE, "--sound is for game sessions with a window",
                           "drop --sound (qtFRED and a standalone server play no audio)")
    if not isinstance(req.runtime_sec, int) or req.runtime_sec < 1:
        # Every session runs under a hard runtime cap; 0 was never "unlimited" (QA XFK-2).
        raise HarnessError(EXIT_USAGE, f"runtime {req.runtime_sec!r} is not a positive number of "
                           "seconds", "pass --runtime N with N >= 1 (default 1800)")
    if any(c in req.pilot_name for c in "*?["):
        # The name selects the pilot's files by prefix; a glob character would widen that
        # selection to other files of the folder (QA SA-3, Phase B refinement b).
        raise HarnessError(EXIT_USAGE, f"pilot name {req.pilot_name!r} contains * ? or [",
                           "pass the pilot's plain name")
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


def _web_fields(req: LaunchRequest) -> dict:
    """Return the web panel fields for a new session: a port and a fresh login for a standalone
    (a real one's port unique on the host, a private one's DEFAULT_WEB_PORT), nothing otherwise."""
    if req.role != "standalone":
        return {}
    user, password = web_login()
    port = web_port() if resolve_network(req) == "real" else DEFAULT_WEB_PORT
    return {"web_port": port, "web_user": user, "web_password": password}


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
                               port=(assign_port(req.match, req.port) if req.match
                                     else host_port(None) if resolve_network(req) == "real"
                                     and req.kind == "game" else None),
                               # Only a client joins anything: asking for "the match's server"
                               # while launching the server itself would refuse in a real match.
                               connect=(req.connect or (default_connect(str(req.match))
                                                        if req.role == "client"
                                                        else f"127.0.0.1:{DEFAULT_GAME_PORT}")),
                               game_name=req.game_name, closed=req.closed,
                               network=resolve_network(req), sound=req.sound,
                               visible=req.visible, **_web_fields(req))
    if platform.NAME == "windows" and not req.visible:
        # Out of sight: the session's own desktop, named like its scope (win_desktop). The
        # record is already on disk where other commands list it, so this goes through the
        # locked update like every later change.
        def on_its_own_desktop(rec: session.Session) -> None:
            rec.desktop = rec.scope
        sess = session.update(sess.id, on_its_own_desktop)
    try:
        if req.kind == "game":
            chan = paths.channel_dir(session.prefs_dir(sess))
            chan.mkdir(parents=True)
            (chan / "switches.txt").write_text(json.dumps(sess.switches), encoding="utf-8")
            if req.allow_eval:
                (chan / "allow_eval").touch()
            if platform.NAME == "windows":
                _write_engine_settings(sess)
        if req.role is not None:
            # Read by the engine through cfile (CF_TYPE_DATA), so the session's private prefs
            # data/ wins over anything in the game folder (spec section 3). Every match role gets
            # one (QA SA-8): a +pxo left in the user's game folder must never reach a host.
            cfg = session.prefs_dir(sess) / "data" / "multi.cfg"
            cfg.parent.mkdir(parents=True, exist_ok=True)
            web_root = None
            if sess.web_port is not None:
                # Empty and private to this session (QA SA-1); see multi_cfg_text.
                # Absolute: mongoose resolves a relative root against the engine's working
                # folder, not ours (a relative FSO_HARNESS_RUNTIME; Phase B SL NIT).
                web_root = (session.run_dir(sess) / "webui").resolve()
                web_root.mkdir(mode=0o700)
            cfg.write_text(multi_cfg_text(str(req.game_name or req.match), sess.web_port,
                                          sess.web_user, sess.web_password,
                                          str(web_root) if web_root else None), encoding="utf-8")
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


def _write_engine_settings(sess: session.Session) -> None:
    """Put the engine setting "do not pause when the window is not in front" into the
    session's prefs before the engine starts (Windows sessions).

    WHY (first Windows session, 2026-11-16: the mission paused itself one second in): the
    engine pauses a single-player mission whenever its window is not the foreground window
    (freespace.cpp game_poll). A window on a hidden desktop never is, and a visible one stops
    being it as soon as the user clicks elsewhere. The command-line switch for this,
    -no_unfocused_pause, is read only when the engine's in-game options are off
    (pause_if_unfocused), and they are on by default; the value that counts is the option
    Game.UnfocusedPause, read from data/<first mod>_settings.ini under the prefs folder
    (osregistry.cpp; the first mod is always the bridge). Source read at 26.2.0.
    WHY not on Linux: there the window is the only one on its own display and stays in
    front; nothing needs changing, so nothing is.
    """
    data = session.prefs_dir(sess) / "data"
    data.mkdir(exist_ok=True)
    (data / f"{paths.BRIDGE_MOD}_settings.ini").write_text("[Game]\nUnfocusedPause=false\n",
                                                         encoding="utf-8")


def _wait_window(sid: str, timeout: float) -> None:
    """Block (bounded) until qtFRED's editor window is up on the session display.

    Its splash screen appears first under the same process; the editor is the window titled with
    the mission file name (xwin.main_window), so readiness waits for that one.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = session.load(sid)
        if s.display and s.engine_pid and xwin.main_window(s.display, session.window_owner(s),
                                                           s.kind, s.mission):
            return
        if s.status in ("exited", "failed"):
            raise HarnessError(EXIT_ENGINE, "qtFRED exited during startup", "see `fsoctl log <id>`")
        time.sleep(0.5)
    # EXIT_TIMEOUT like the game's readiness timeout, so agents branch on one code (QA CR-15).
    raise HarnessError(EXIT_TIMEOUT, f"qtFRED's editor window did not appear in {timeout:.0f}s",
                       "see `fsoctl log <id>` and take a screenshot")


def _runner_argv(sid: str) -> list[str]:
    """The command that starts the runner of session `sid`.

    -P (Python 3.11+): leave the current folder off the module search path. WHY (QA Plan B,
    SA-1): under -m Python puts that folder first, ahead of PYTHONPATH. The runner is
    host-side code, outside the sandbox too, and the folder `fsoctl` was typed in may be the
    checkout or mod under test; a folder named fsoctl or a file named like a standard module
    lying there was what the runner loaded. -P drops only that folder; PYTHONPATH stays.
    """
    return [sys.executable, "-P", "-m", platform.RUNNER_MODULE, sid]


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
    # Before the runtime root exists: a root the engine cannot use is refused with nothing
    # made in it and no cleanup run there (cross-family review XFK-3).
    _check_run_folder()
    props = scope.caps(runtime_sec=req.runtime_sec + RUNTIME_BACKSTOP_SEC)
    memory_max = next((p.split("=", 1)[1] for p in props if p.startswith("MemoryMax=")), None)
    with session.file_lock(paths.runtime_root() / "launch.lock", _LOCK_WAIT):
        _auto_cleanup()
        check_capacity()
        sess, refreshed = _prepare(req, memory_max)
        try:
            # The runner (and its Xvfb) stay on the host; the runner puts the ENGINE into the
            # session's network (runner.engine_argv: the match's namespace via nsenter, or its own
            # via PRIVATE_NET_PREFIX). See the PRIVATE_NET_PREFIX comment for why not the runner.
            runner_argv = _runner_argv(sess.id)
            if sess.desktop:
                props = [*props, f"Desktop={sess.desktop}"]   # read by win_scope.start only
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


def engine_holds(sess: session.Session, proto: str, port: int | None) -> bool:
    """True when the session's OWN engine holds a listening `proto` socket ("udp"/"tcp") on
    `port` in its match's network.

    The query itself is the platform's (procs.socket_owner_pids: `ss` on Linux, `netstat` on
    Windows). For a private match it runs through nsenter because the socket exists only there
    (`ss` on the real network must show nothing, spec section 5.1); for a real match the prefix
    is empty and it runs on the host. WHY the owner check (QA CR-1/TA-3/SL-1): on the host any
    program can hold the port (a stock game, another runtime root), and an engine whose bind failed
    carries on without networking, so "some socket on the port" said ready for a server nobody
    could join. `-p` names the owning pid; the engine is the recorded engine_pid (nsenter and the
    private prefix exec it, so the pid is the engine's).
    """
    if port is None or sess.engine_pid is None:
        return False
    try:
        inside = None           # an ordinary real-network session: its socket is on the host
        if sess.match:
            its_match = match.load(sess.match)

            def inside(argv: list[str]) -> subprocess.CompletedProcess:
                """Run the query in the match's network."""
                return match.run_inside(its_match, argv)
        return sess.engine_pid in procs.socket_owner_pids(proto, port, inside)
    except (HarnessError, subprocess.TimeoutExpired, OSError):
        # A holder that died between two reads, or a query that hangs, is "not listening",
        # never a crashed status read (QA SL-6).
        return False


def listening(sess: session.Session) -> bool:
    """True when the standalone's own engine holds its UDP game port (standalone readiness)."""
    return engine_holds(sess, "udp", sess.port)


_WEB_PANEL_WAIT = 10.0    # seconds launch waits for the panel after the game port is up
_GAME_PORT_WAIT = 10.0    # seconds launch waits for a real-network engine's game port


def _check_game_port(sess: session.Session, warnings: list[str]) -> None:
    """After ready, make sure a real-network engine holds its own game port.

    The test bind (port_free) cannot close the gap before the engine binds: another runtime
    root or program can take the port, and the engine then carries on with networking off. A
    standalone's readiness is already its own socket. A HOST that lost its port would be a
    server nobody can join while clients are sent to the other program, so that fails the
    launch (EXIT_LAUNCH); any other session gets a warning (plan A QA XFA-3).
    """
    if sess.network != "real" or not sess.port or sess.role == "standalone":
        return
    deadline = time.monotonic() + _GAME_PORT_WAIT
    while not engine_holds(sess, "udp", sess.port):    # bounded by the deadline
        if time.monotonic() >= deadline:
            text = (f"the engine does not hold its UDP game port {sess.port} (another program "
                    "or harness root took it); it runs with networking off")
            if sess.role == "host":
                raise HarnessError(EXIT_LAUNCH, f"host: {text}", "launch again for a new port")
            warnings.append(text)
            return
        time.sleep(0.25)


def web_panel_up(sess: session.Session, wait: float = 0.0) -> bool:
    """True when the standalone's own engine holds its web panel's TCP port, polling up to
    `wait` seconds (bounded). The engine logs a failed panel bind and carries on (mongoose.c
    set_ports_option, stand_gui-unix.cpp std_configLoaded ignores mg_start's result), so the
    login is only reported while the panel is really this engine's (QA SA-2/SL-2)."""
    deadline = time.monotonic() + wait
    while True:                 # bounded by the deadline (one pass when wait is 0)
        if engine_holds(sess, "tcp", sess.web_port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


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
    sess = session.load(sid)
    tail = _engine_log_tail(sess)
    if sess.network == "sandbox":
        # QA CR-2: the advice never depends on text the build under test wrote, and never points
        # at another network. A sandbox that could not be set up is recorded by the runner
        # itself (exit_info "error"), not recognised in the log.
        reason = (sess.exit_info or {}).get("error")
        return HarnessError(EXIT_LAUNCH if reason else err.code,
                            f"the sandboxed engine ended before it was ready"
                            f"{': ' + reason if reason else ''}",
                            f"engine.log (the build's own output, and bwrap's): {tail}")
    if any(w in tail for w in _NAMESPACE_WORDS):
        return HarnessError(EXIT_LAUNCH,
                            f"the engine could not start in its private network: {tail}",
                            "user namespaces or `ip` are unavailable on this host; "
                            "launch with --network real to use the machine's network")
    return HarnessError(err.code, err.message, f"{err.hint} (engine.log: {tail})")


def _engine_log_tail(sess: session.Session, lines: int = 5) -> str:
    """Return the last `lines` lines of engine.log joined with " | ", read through safefs and
    at most 64 KiB from its end (the engine decides its size), or a note why there are none."""
    try:
        data, _ = safefs.read_tail(session.run_dir(sess), "engine.log", 64 << 10)
    except OSError:
        return "(no engine.log)"
    text = data.decode(errors="replace").strip()
    return " | ".join(text.splitlines()[-lines:]) if text else "(engine.log is empty)"


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
    switches, warnings, sound, match, role, port, web_panel, holder_pid}; errors/last_error/
    switches come
    from the bridge heartbeat (contract C3); sound is {sink, streams} for a --sound session.
    Every failure after the session exists stops it and carries details["id"] (QA CR-5, TA-4).
    """
    # Before the lock, the cleanup and the session count: on an OS without the feature the
    # answer is the refusal, whatever else is wrong (QA Plan B, TA-10).
    _refuse_unavailable(req)
    sess, refreshed = _start(req)
    try:
        hb = _wait_ready(req, sess.id)
        sess = session.load(sess.id)
        if req.role == "standalone":
            found = {"renderer": "none", "libraries": []}      # no window, nothing mapped
        else:
            found = renderer.classify(procs.loaded_libraries(sess.engine_pid or 0))
        if req.kind == "game" and req.renderer == "vulkan" and found["renderer"] != "vulkan":
            raise HarnessError(EXIT_LAUNCH,
                               f"asked for Vulkan, engine came up as {found['renderer']}",
                               "check VK_ICD_FILENAMES and that the engine was built with Vulkan")
        # Inside the try: a stream on another sink (or none) fails the launch and stops the
        # engine. The engine has opened its audio device by now (the bridge reported a state).
        routing = None
        if sess.sound:
            routing = sound.verify_routing(sess.engine_pid or 0, sess.id)
        warnings = _warnings(req, found)
        _check_game_port(sess, warnings)
        panel = web_panel(sess)
        if panel and not web_panel_up(sess, _WEB_PANEL_WAIT):
            # Not a failed launch: the game server is up; only its panel is not (QA SA-2/SL-2).
            warnings.append(f"web panel: the engine does not hold TCP port {sess.web_port} "
                            "(another program may); no login is reported")
            panel = None
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
            "warnings": warnings, "sound": routing, "match": sess.match,
            "role": sess.role, "port": sess.port, "web_panel": panel,
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
        if _runs_out_of_sight(sess):
            # One of its processes still runs and its group is not found here: this login
            # cannot see it (a job's name is known only inside the login that made it).
            # "Not running, relaunch" would be wrong advice and a second engine.
            raise HarnessError(
                EXIT_ENGINE,
                f"session {sid} runs in another Windows login: this one cannot see its "
                "process group",
                "run the command from the login that started the session; `stop` works "
                "from here (it leaves a request in the run folder)")
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
    record = {k: v for k, v in asdict(sess).items() if not k.startswith("web_")}
    # The login appears once, under web_panel, and only while this engine holds the panel's port
    # (QA SA-2/SA-4/CR-7).
    up = sess.role == "standalone" and network == "alive"
    out = {**record, "alive": alive(sess), "heartbeat": hb, "heartbeat_age_sec": age,
           "stale": None if age is None else age > STALE_SEC, "match_network": network,
           "listening": listening(sess) if up else None,
           "web_panel": web_panel(sess) if up and web_panel_up(sess) else None}
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


def _runner_is_ours(sess: session.Session) -> bool:
    """True when the recorded runner pid still is this session's runner: a process in the
    session's group (scope on Linux, job on Windows) and, where the record holds the runner's
    start time, one that started at that time.

    WHY check: runner_pid is a stored number; after the session ended it may belong to an
    unrelated process, which must never be signalled (plan B Review Focus 2). The start time
    is recorded by the Windows runner; a record without it rests on the group check alone.
    """
    pid = sess.runner_pid
    if not pid or not procs.in_group(pid, sess.scope):
        return False
    return sess.runner_start is None or procs.pid_start(pid) == sess.runner_start


def _stop_runner_first(sess: session.Session) -> str:
    """Ask the session's runner to stop (procs.ask_stop: SIGTERM on Linux, a stop file on
    Windows) and wait (at most _RUNNER_EXIT_WAIT s) for it to exit.

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
    if not _runner_is_ours(sess):
        return "skipped:runner not running in the session's scope"
    skipped = procs.ask_stop(sess)
    if skipped:              # gone in the meantime, or not ours to signal: the backstop handles it
        return skipped
    deadline = time.monotonic() + _RUNNER_EXIT_WAIT
    # Bounded by the deadline. The scope check too: a pid reused outside the scope is not our
    # runner, so there is nothing more to wait for (QA CR-7).
    while time.monotonic() < deadline:
        if procs.gone(pid) or not procs.in_group(pid, sess.scope):
            return "ok"
        time.sleep(_RUNNER_POLL)
    sys.stderr.write(f"fsoctl: the runner of {sess.id} did not exit within "
                     f"{_RUNNER_EXIT_WAIT:.0f} s; stopping the scope (the engine may abort and "
                     "leave a core dump)\n")
    return "timeout"


def _stop_group(sess: session.Session, marker_left: bool) -> None:
    """Stop the session's process group: the backstop that ends whatever is left after the
    runner was asked (or everything, if the runner hung).

    When the stop fails and the marker was written, the error also says that the request
    is in the run folder: a live runner acts on it whatever this login may do to the group
    (round-2 CRF-4: the command reported a plain failure for a stop that was under way)."""
    try:
        scope.stop(sess.scope)
    except HarnessError as e:
        # Only where the runner is asked by the marker (Windows). The Linux runner acts on
        # a signal and the marker only labels the cause, so the sentence would be untrue
        # there (round-2 STF-5).
        if not marker_left or platform.NAME != "windows":
            raise
        left = "a stop request was left in the session's run folder; a live runner acts on it"
        raise HarnessError(e.code, e.message, f"{e.hint}; {left}" if e.hint else left,
                           details=e.details) from e


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
    except HarnessError as e:
        if e.code != EXIT_NOT_FOUND:
            raise
        return _stop_scope_only(sid, e)
    marker_error = None
    try:
        (session.run_dir(sess) / session.STOP_MARKER).touch()
    except FileNotFoundError:
        return _stop_scope_only(sid, HarnessError(EXIT_NOT_FOUND, f"no session {sid}",
                                                  "use `fsoctl list`"))
    except OSError as e:
        # The marker tells the runner this is a requested stop. Failing to write it must not
        # cancel the stop (cross-family review XFK-2, round-2 SAF-3): the group is stopped
        # below all the same, and the reply says the marker is missing.
        marker_error = e.strerror or str(e)
    # Asked BEFORE anything is stopped (round-2 STF-2): where this login sees the group, its
    # own stop below is the whole answer, and a process caught in its last moment after a
    # forced end must not read as "runs in another login". Only where group names are per
    # login can a group be out of sight at all.
    out_of_sight = platform.GROUPS_PER_LOGIN and scope.is_gone(sess.scope)
    runner_first = _stop_runner_first(sess)
    _stop_group(sess, marker_left=marker_error is None)
    # Not seen here, and its runner runs: a session of another login (_gone).
    unseen = out_of_sight and _runner_known_alive(sess)
    if unseen and marker_error:
        # Nothing was left for that runner and nothing here could be stopped (round-2 STF-3).
        raise HarnessError(
            EXIT_ENGINE,
            f"the stop request could not be left in session {sid}'s run folder "
            f"({marker_error}), and this Windows login cannot see its process group",
            "stop it from the login that started the session")
    if out_of_sight and not unseen and _engine_known_alive(sess):
        # No runner to read the stop request, and the group's stop reached nothing: the
        # engine runs on. Saying "stopped" would be false, and marking the record would
        # hide the engine from the login that can end it (cross-family review XFA-1).
        raise HarnessError(
            EXIT_ENGINE,
            f"session {sid}'s engine (pid {sess.engine_pid}) still runs: its runner is gone "
            "and this Windows login cannot see its process group to end it",
            "run `fsoctl stop` or `fsoctl cleanup` from the login that started the session")
    sink_error = None
    if sess.sound_module is not None:
        # After the engine is gone: its streams go with it (node.dont-reconnect would leave them
        # silent anyway). A recorder still on the monitor without that property would be moved
        # to the desktop's audio, so recorders stop first (README "Sound").
        sink_error = sound.remove_sink(sess.sound_module, sid)

    if unseen:
        # The runner was asked through the marker and does the stopping; it writes the
        # record ("stopped", ended_by "stop") when it has. Marking the record here would say
        # "stopped" before it is true, and for good if that runner were stuck (round-2 CRF-3).
        return {"id": sid, "status": sess.status, "exit_info": sess.exit_info,
                "runner_first": "asked",
                "note": "this login cannot see the session's process group (it was started "
                        "from another Windows login); the stop request was left in its run "
                        "folder, and its runner closes the engine (it allows 20 s for that, "
                        "then ends it by force); `status` shows when it has"}

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
        # The group is ended either way, so this stays a "stopped" answer. Only a record that
        # is gone means the folder was removed; anything else (a disk or permission fault) is
        # said as what it is, so nobody goes looking for a deleted folder that is still there.
        gone = isinstance(e, (HarnessError, FileNotFoundError))
        note = ("its run folder was removed while it was being stopped" if gone else
                f"the session was stopped, but its record could not be updated ({e})")
        return {"id": sid, "status": "stopped", "exit_info": None, "runner_first": runner_first,
                "note": note}
    out = {"id": sid, "status": sess.status, "exit_info": sess.exit_info,
           "runner_first": runner_first}
    if marker_error:
        out["note"] = (f"the stop marker could not be written ({marker_error}); the process "
                       "group was stopped directly")
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
    return _gone(s)


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


def _cleanup_sandbox_orphans(now: float, older_than: float, kept: list[session.Session],
                             failed: list[dict]) -> list[str]:
    """Remove sandbox prefs folders (<sandbox root>/s<8 hex>) whose session record is gone
    (QA CR3-2: the records live on the /run/user tmpfs and vanish at a reboot, the folders on
    disk do not). Kept: a folder a kept session uses, one whose scope still exists, and one
    younger than `older_than` (by its own modification time). Removed through safefs, never
    following a link; returns the removed paths."""
    root = paths.sandbox_root()
    used = {Path(s.prefs_root).parent for s in kept if s.prefs_root}
    removed = []
    for name in sorted(os.listdir(root)):     # bounded: one entry per session ever sandboxed
        folder = root / name
        if not paths.is_session_id(name) or folder in used or folder.is_symlink():
            continue
        if (now - folder.lstat().st_mtime < older_than
                or not scope.is_gone(session.scope_name(name))):
            continue
        try:
            safefs.remove_tree(root, name)
            removed.append(str(folder))
        except OSError as e:
            failed.append({"path": str(folder), "error": str(e)})
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
    if platform.NAME != "linux":
        # Sound sessions exist only on Linux so far (--sound is refused elsewhere: Plan E),
        # so there is no sink to find, and the code that asks the sound server is Linux's.
        return out
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


def _ended_cleanly(s: session.Session) -> bool:
    """True if a run ended with nothing for crash_report to explain: stopped through `stop`, or
    the engine exited by itself with code 0. Anything else counts as crashed: a signal or non-zero
    code, a runtime/memory/disk limit, a "failed" launch, or a record whose scope vanished before
    its end was written (still "starting"/"running").

    Known gap: a launch that failed while its engine still ran (ready timeout, wrong renderer) is
    ended by _abandon through stop(), so it reads as a clean stop. So does a Windows session
    whose runner was lost, if a stop was ever asked of it: the cleanup that ends it finds the
    stop marker and records a stop (_end_unattended; round-2 STFB-3).
    """
    if s.status == "stopped":
        return True
    return (s.status == "exited" and s.ended_by == "exit"
            and (s.exit_info or {}).get("returncode") == 0)


def _auto_cleanup() -> None:
    """Run cleanup's body with AUTO_CLEAN_AGE / AUTO_CLEAN_CRASHED_AGE; the caller holds
    launch.lock (_start).

    WHY at launch (2026-11-14: 929 finished run folders, 4.6 GB, filled the /run/user tmpfs over
    five days because only a manual `cleanup` deleted them): with no daemon, launch is the one
    moment run folders are made, so their number stays bounded by about a day of launches. WHY
    before _prepare: the session being launched has no record yet, so it can never be picked.
    A failure never fails the launch: it is printed on stderr and the next launch retries.
    """
    try:
        out = _cleanup_locked(AUTO_CLEAN_AGE, AUTO_CLEAN_CRASHED_AGE)
    except (HarnessError, OSError) as e:
        msg = e.message if isinstance(e, HarnessError) else str(e)
        sys.stderr.write(f"fsoctl: automatic cleanup failed: {msg}\n")
        return
    for f in out["failed"]:
        if f.get("said"):            # _end_unattended wrote its own line already
            continue
        sys.stderr.write(f"fsoctl: automatic cleanup could not remove {f['path']}: "
                         f"{f['error']}\n")


def _runner_known_gone(sess: session.Session) -> bool:
    """True only when it is known that the session's recorded runner no longer runs: no
    process has its pid, or the process that has it now started at another time (the pid was
    reused). False when no runner is recorded and when the OS will not say: a lookup that
    failed is never read as "gone" (a healthy session started at another privilege level
    may fail every lookup; whether it does was not measured)."""
    return _known_gone(sess.runner_pid, sess.runner_start)


def _unattended(s: session.Session, now: float) -> str | None:
    """Why nothing looks after a session's process group any more, as the word recorded in
    ended_by, or None when the session is attended or nothing of it runs.

    "abandoned_start": still "starting" after _STARTING_GRACE with something running: a
    launch that was killed after it made the group (plan B Review Focus 1). The runner sets
    "running" the moment it has started the engine, seconds after launch.
    "runner_lost": a runner was recorded and is known to be gone (_runner_known_gone) while
    the group still has processes: the runner was ended from outside or crashed, or it
    recorded a normal end and left with a child of the engine still running.
    The runner is looked at first and the group second: the other way round, a runner in the
    last moment of a normal exit would be seen gone with a group that was only just active.
    """
    if s.status == "starting" and now - s.created >= _STARTING_GRACE:
        return None if scope.is_gone(s.scope) else "abandoned_start"
    if _runner_known_gone(s) and not scope.is_gone(s.scope):
        return "runner_lost"
    return None


def _end_unattended(now: float) -> list[dict]:
    """End the process group of every session nothing looks after (_unattended), and record
    that it was done.

    Only where the OS does not end a group at its time limit (platform.GROUP_TIME_LIMIT is
    False: Windows). There the runner is the only thing that ends an engine at its runtime
    limit or on a stop request, and a group with no runner would hold its job, its desktop,
    a session slot and its run folder for good. On Linux the scope's RuntimeMaxSec ends it,
    and this is not called.

    Acts only on what it found: a recorded runner known to be gone, or a start abandoned for
    two minutes, with the group still active. Each time it says so on stderr and in the
    audit log. The record is changed only if it has no end yet (status "starting" or
    "running" becomes "failed" with ended_by saying which rule, or "stopped" / "stop" when
    a stop request is in its run folder: the marker is never removed, so this also holds
    for a stop that was refused from another login; the audit line keeps the rule's name);
    a run that already recorded its end keeps it, and only its leftover processes are
    ended. One group that cannot be ended is reported and the others are still handled.
    The stop is given no
    polite wait (on Windows that shortens the wait for the job to empty): this runs under
    the launch lock.
    """
    not_ended: list[dict] = []
    for s in session.list_sessions():
        why = _unattended(s, now)
        if why is None:
            continue
        try:
            scope.stop(s.scope, 0)
        except HarnessError as e:
            sys.stderr.write(f"fsoctl: could not end session {s.id}, which nothing looks "
                             f"after ({why}): {e.message}\n")
            not_ended.append({"path": str(session.run_dir(s)), "said": True,
                              "error": f"could not end its process group, which nothing "
                                       f"looks after ({why}): {e.message}"})
            continue
        what = ("never finished starting" if why == "abandoned_start"
                else "its runner is gone and its process group was still running")
        sys.stderr.write(f"fsoctl: session {s.id}: {what}; ended its process group\n")
        unended = s.status in ("starting", "running")
        # A stop in flight has left its marker: this end is that stop's, whoever got to the
        # group first, and is recorded as one (round-2 STF-6).
        asked = (session.run_dir(s) / session.STOP_MARKER).exists()

        def mark(rec: session.Session, why: str = why, asked: bool = asked) -> None:
            if rec.status in ("starting", "running"):
                rec.status = "stopped" if asked else "failed"
                if rec.ended_by is None:
                    rec.ended_by = "stop" if asked else why
        try:
            session.update(s.id, mark)
            session.audit(s, "cleanup", f"ended the process group ({why})" if unended
                          else f"ended leftover processes of a run that had ended ({why})")
        except (HarnessError, OSError) as e:     # the folder can go at any moment (a stop)
            sys.stderr.write(f"fsoctl: could not record the end of session {s.id}: {e}\n")
    return not_ended


def _cleanup_locked(older_than_sec: float, crashed_older_than_sec: float | None = None) -> dict:
    """cleanup()'s body; the caller holds launch.lock. With `crashed_older_than_sec`, a session
    that did not end cleanly (_ended_cleanly) is kept until it is at least that old."""
    removed, kept, failed = [], [], []
    now = time.time()
    if not platform.GROUP_TIME_LIMIT:
        # In the answer as well as on stderr (cross-family review XFA-4): the MCP server
        # passes on the JSON and drops stderr.
        failed += _end_unattended(now)
    for s in session.list_sessions():
        # The crash check first: it is a field read, _removable asks systemd.
        if ((crashed_older_than_sec is not None and now - s.created < crashed_older_than_sec
                and not _ended_cleanly(s))
                or not _removable(s, now, older_than_sec)):
            kept.append(s)
            continue
        try:
            # Through safefs, which makes each folder 0700 before reading it and never follows a
            # link: an engine can chmod 000 a folder it writes, and rmtree then fails, leaving
            # the space used (QA AT2-2). The sandbox prefs folder on disk goes first, so a
            # failure there keeps the record and the next cleanup retries.
            if s.prefs_root:          # the stored location, not a rebuilt one (QA CR3-3)
                folder = Path(s.prefs_root).parent
                safefs.remove_tree(folder.parent, folder.name)
            # Tried again for a short, bounded time where a file in use refuses to be
            # deleted (Windows). Measured on CI's Windows machine, 2026-11-17 (run
            # 44083251849): the cleanup that had just ended a session's processes got
            # "in use" (error 32) for its runner.log. The job was already empty, the
            # engine's process not yet ended; 16 ms later the folder could be removed. In
            # that run the record was still there (the log was met before it), so the next
            # cleanup would have removed the folder. A busy file met after the record has
            # been deleted leaves a folder no later cleanup returns to (round-2 STF-7): for
            # that case this is the only second try. A folder whose file stays in use costs
            # a cleanup about a second, then is reported as before.
            session._while_busy(lambda sid=s.id: safefs.remove_tree(paths.runtime_root(), sid))
            removed.append(s.id)
        except OSError as e:
            failed.append({"path": str(session.run_dir(s)), "error": str(e)})
            kept.append(s)
    folders = _cleanup_side_folders(now, older_than_sec, kept, failed)
    folders += _cleanup_sandbox_orphans(now, older_than_sec, kept, failed)
    sinks = _cleanup_sinks(kept, failed)
    # After the session pass, so a match whose last session was just removed goes in the same
    # run. A dead or removed match with no session left is just a folder; same age rule.
    for m in match.list_matches():
        if (match.alive(match.observe(m)) or sessions_in_match(m.id)
                or now - m.created < older_than_sec):
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
    p.add_argument("--network", choices=NETWORKS, default=None,
                   help="real: the machine's network, as the stock game (default); private: "
                        "own loopback-only network; sandbox: private plus a bwrap sandbox "
                        "(no home, game data read-only; OpenGL only, no --sound)")
    p.add_argument("--sound", action="store_true",
                   help="play audio into the session's own null sink (named in the result's "
                        "sound.sink; record <sink>.monitor); default: -nosound")
    p.add_argument("--visible", action="store_true",
                   help="Windows: run on your desktop instead of a hidden one (the window "
                        "appears and can take focus)")
    p.add_argument("extra", nargs="*", help="extra engine arguments, after --")


@register("launch", "start a game or qtFRED session and wait until it is ready", _configure_launch)
def _cli_launch(args: argparse.Namespace) -> dict:
    """Build a LaunchRequest from CLI flags and launch it."""
    width, height = _parse_res(args.res)
    mode, name = _parse_pilot(args.pilot)
    kind = "qtfred" if args.qtfred else "game"
    # Before a program is even looked for (QA Plan B, TA-10): with QTFRED_BIN unset the
    # editor refusal was never reached.
    _refuse_unavailable(LaunchRequest(binary="", kind=kind, sound=args.sound, match=args.match,
                                      role=args.role, network=args.network))
    req = LaunchRequest(
        binary=args.binary or _default_binary(args.qtfred), mission=args.mission,
        mods=[m for m in args.mods.split(",") if m], renderer="vulkan" if args.vulkan else "opengl",
        width=width, height=height, skip_movies=args.skip_movies, pilot_mode=mode,
        pilot_name=name, pilot_source=args.pilot_source, dismiss_popups=args.dismiss_popups,
        skip_command_briefing=args.skip_command_briefing, skip_briefing=args.skip_briefing,
        noninteractive=not args.interactive, allow_eval=args.allow_eval, extra_args=args.extra,
        kind=kind, runtime_sec=args.runtime,
        ready_timeout=args.ready_timeout, match=args.match, role=args.role, port=args.port,
        connect=args.connect, game_name=args.game_name, closed=args.closed,
        network=args.network, sound=args.sound, visible=args.visible)
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
