"""Session records: one JSON file per run folder, read fresh by every fsoctl call.

WHY files and no daemon: a long-lived broker holding session state goes stale when it outlives or
loses its processes (a lesson from an earlier MCP server). Here all state lives on disk next to
the run, and every caller (CLI, MCP, another agent) reads it fresh.
"""
import contextlib
import json
import os
import secrets
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import paths, platform
from .errors import EXIT_NOT_FOUND, EXIT_TIMEOUT, HarnessError

# Created in the run folder by `fsoctl stop` just before it stops the scope, so the runner (which
# gets SIGTERM from that stop) can record ended_by="stop" rather than "signal".
STOP_MARKER = "stop-requested"

# Windows only: how often, and how far apart, a record's rename or read is tried again while
# another process has the file open (_while_busy). 100 tries of 10 ms; a read or a rename
# holds the file for well under a millisecond.
_BUSY_TRIES = 100
_BUSY_GAP_SEC = 0.01
# Windows only: how many times a record that is not there is looked for before the answer is
# "no such session" (_read_record). Five looks 10 ms apart: the file is absent for an instant.
_GONE_LOOKS = 5


@dataclass
class Session:
    """Everything known about one harness run."""
    id: str
    kind: str
    binary: str
    fs2path: str
    mods: list[str]
    mission: str | None
    renderer: str
    width: int
    height: int
    switches: dict
    allow_eval: bool
    extra_args: list[str]
    scope: str
    display: str | None = None
    runner_pid: int | None = None
    # When that runner process started (platform.procs.pid_start), so a later process that
    # got the same pid is not taken for it. None in records written before this field
    # existed and by the Linux runner, where the scope check alone decides.
    runner_start: int | None = None
    engine_pid: int | None = None
    # When the engine process started, recorded with its pid by the Windows runner for the
    # same reason as runner_start (launcher._gone asks about both). None on Linux.
    engine_start: int | None = None
    xvfb_pid: int | None = None
    created: float = field(default_factory=time.time)
    status: str = "starting"
    exit_info: dict | None = None
    # Windows: the session's own desktop (win_desktop), named like its scope; None for a
    # --visible session, which runs on the user's desktop, and on Linux (see `display`).
    desktop: str | None = None
    visible: bool = False
    # Working folder: a shadow folder for runs that write output files; None means fs2path.
    cwd: str | None = None
    # How the run ended: "stop" | "runtime_limit" | "disk_limit" | "oom" | "signal" | "exit",
    # written by the runner (QA SL-7), so a scope killed by its own limits is not left
    # "running"; on Windows also "runner_error" (the runner failed and ended its engine) and,
    # written by a later cleanup, "runner_lost" | "abandoned_start". None while it runs.
    ended_by: str | None = None
    runtime_sec: int | None = None    # the runner's own deadline; RuntimeMaxSec is 60 s later
    memory_max: str | None = None     # the scope's MemoryMax property value, e.g. "2097152K"
    # Multiplayer (spec section 4): the match this session joined, its role, the local UDP port
    # the engine binds, and for clients the server address. None/False for ordinary sessions.
    match: str | None = None
    role: str | None = None           # "standalone" | "host" | "client"
    port: int | None = None
    connect: str = "127.0.0.1:7808"
    game_name: str | None = None
    closed: bool = False
    # "real": the machine's network (the default since 2026-10-16); "private": the engine runs
    # in its own loopback-only network; None: a record written before the field existed (status
    # shows null rather than a guess, QA XFA-6). A match session takes its match's network. The
    # runner reads this field.
    network: str | None = None
    # Standalone only: its web panel's TCP port and basic-auth login, written into its multi.cfg
    # (launcher.multi_cfg_text) so the engine's admin/admin default is never in force. None for
    # every other session. Reported by launch and status; never written to an audit log.
    web_port: int | None = None
    web_user: str | None = None
    web_password: str | None = None
    # launch --sound: the engine plays into its own null sink (fsoctl/sound.py); sound_module is
    # that sink's module index on the user's Pulse server, used by stop and by a failed launch
    # to unload it (cleanup finds leftover sinks by name instead).
    sound: bool = False
    sound_module: int | None = None
    # The process id the engine's window carries in _NET_WM_PID, when it differs from engine_pid:
    # a sandboxed engine is pid 2 inside its own pid namespace (sandbox.ENGINE_NS_PID) while
    # engine_pid is its host pid. None for every other session. Written by the runner.
    window_pid: int | None = None
    # A sandbox session's prefs folder (FSO_PREFERENCES_PATH) on disk under paths.sandbox_root(),
    # set by new_session; None means run_dir/prefs (every other session). See prefs_dir.
    prefs_root: str | None = None


def window_owner(sess: Session) -> int:
    """Return the pid to look the session's windows up by (xwin): window_pid when the runner set
    one, else engine_pid, else 0 (no process yet, which matches no window)."""
    return sess.window_pid or sess.engine_pid or 0


def run_dir(sess: Session) -> Path:
    """Return the session's run folder."""
    return paths.session_dir(sess.id)


def prefs_dir(sess: Session) -> Path:
    """Return the session's private FSO_PREFERENCES_PATH folder: <sandbox root>/<id>/prefs for a
    sandbox session (on disk, see paths.sandbox_root), <run folder>/prefs otherwise."""
    return Path(sess.prefs_root) if sess.prefs_root else run_dir(sess) / "prefs"


def scope_name(sid: str) -> str:
    """Return the systemd scope name (without ".scope") of session `sid`."""
    return f"fso-harness-{sid}"


def new_session(**fields) -> Session:
    """Allocate an id, create the run and prefs folders, save, and return the session."""
    sandboxed = fields.get("network") == "sandbox"
    for _ in range(16):  # bounded: 16 collisions in 2^32 is effectively impossible
        sid = "s" + secrets.token_hex(4)
        if not paths.session_dir(sid).exists() and not (
                sandboxed and (paths.sandbox_root() / sid).exists()):
            break
    else:
        raise HarnessError(EXIT_NOT_FOUND, "could not allocate a session id", "retry")
    paths.session_dir(sid).mkdir(parents=True)
    if sandboxed:
        # Made fresh with os.mkdir (fails if anything is already there), private to the user.
        folder = paths.sandbox_root() / sid
        os.mkdir(folder, 0o700)
        os.mkdir(folder / "prefs", 0o700)
        fields = {**fields, "prefs_root": str(folder / "prefs")}
    sess = Session(id=sid, scope=scope_name(sid), **fields)
    prefs_dir(sess).mkdir(exist_ok=True)
    save(sess)
    return sess


def _while_busy(action: Callable):
    """Return action(); on Windows, try again (bounded) while it fails with PermissionError.

    WHY: readers of a record take no lock. On Linux that is safe: a rename swaps the file under
    a reader, who sees the old record or the new one. On Windows a file that any process has
    open cannot be renamed over, and a read can meet the file while it is being swapped; both
    fail with PermissionError. Measured (probes/xplat/record_race.py, 2026-11-17, three runs,
    two readers looping against one writer): about a third of the renames (31 to 33%) and a
    tenth of the reads failed on Windows, none on Linux. `fsoctl launch` reads the record
    several times a second while the runner makes its first writes, so without this a runner
    could fail to record that it had started the engine. On Linux nothing is tried twice: a
    refusal there is a real fault. The last try's error is the caller's.

    The cleanup removes a run folder through this too (launcher._cleanup_locked): a process
    ended an instant ago can still have a file in the folder open.
    """
    tries = _BUSY_TRIES if platform.BUSY_FILES_REFUSE else 1
    for _ in range(tries - 1):                    # bounded
        try:
            return action()
        except PermissionError:
            time.sleep(_BUSY_GAP_SEC)
    return action()


def _read_record(path: Path, look_again: bool = True) -> dict | None:
    """Return the JSON object in record file `path`, or None when there is no such file.

    WHY more than one look on Windows: while a record is swapped the file is, for an instant,
    not there at all. Seen on real Windows (2026-11-17): with the retry above in place, the
    three-process test in tests/test_session.py still failed in 2 of 8 runs, each some tens of
    thousands of reads; the failure whose text was read was a reader's FileNotFoundError. One
    look would report a live session as "no session". `look_again=False` is for a folder that is
    not a session's, where a missing record is the expected answer and waiting would only slow
    every listing. On Linux one look is the answer: there a rename never leaves a gap.
    """
    looks = _GONE_LOOKS if platform.BUSY_FILES_REFUSE and look_again else 1
    for look in range(looks):                     # bounded
        if look:
            time.sleep(_BUSY_GAP_SEC)
        if not path.exists():
            continue
        try:
            return json.loads(_while_busy(lambda: path.read_text(encoding="utf-8")))
        except FileNotFoundError:                 # gone between the look and the read
            continue
    return None


def save(sess: Session) -> None:
    """Write session.json atomically (temp file + rename)."""
    target = run_dir(sess) / "session.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(sess), indent=1), encoding="utf-8")
    _while_busy(lambda: os.replace(tmp, target))


@contextlib.contextmanager
def file_lock(path: Path, timeout: float) -> Iterator[None]:
    """Hold an exclusive lock on `path` (created if missing) for the `with` block.

    Waits at most `timeout` seconds for another holder, then raises EXIT_TIMEOUT. The OS
    drops the lock if the holder dies, so a crashed fsoctl can never leave it held (flock on
    Linux, a byte-range lock on Windows: platform.locks).
    """
    with open(path, "a", encoding="utf-8") as f:
        deadline = time.monotonic() + timeout
        while not platform.locks.lock_exclusive(f):   # bounded by the deadline check below
            if time.monotonic() >= deadline:
                raise HarnessError(EXIT_TIMEOUT, f"{path.name} is held by another fsoctl",
                                   "another launch or bridge install is running; retry "
                                   "in a moment") from None
            time.sleep(0.05)
        try:
            yield
        finally:
            platform.locks.unlock(f)


def update(sid: str, fn: Callable[[Session], None]) -> Session:
    """Load session `sid`, let `fn` change it in place, save it, and return it, all under a lock.

    WHY a lock: the runner (recording the exit) and `fsoctl stop` (recording the stop) both
    read-modify-write session.json; without it the later save silently drops the earlier one's
    fields (QA SL-7). save() alone is atomic but not exclusive. Every read-modify-write goes
    through here; a plain save() is only for a record no one else can see yet.
    """
    lock_path = paths.session_dir(sid) / "session.lock"
    if not lock_path.parent.is_dir():
        raise HarnessError(EXIT_NOT_FOUND, f"no session {sid}", "use `fsoctl list`")
    with open(lock_path, "a", encoding="utf-8") as lock:
        platform.locks.lock_wait(lock)
        try:
            sess = load(sid)
            fn(sess)
            save(sess)
        finally:
            platform.locks.unlock(lock)
    return sess


def load(sid: str) -> Session:
    """Read a session by id; raise EXIT_NOT_FOUND if it doesn't exist."""
    record = _read_record(paths.session_dir(sid) / "session.json")
    if record is None:
        raise HarnessError(EXIT_NOT_FOUND, f"no session {sid}", "use `fsoctl list`")
    return Session(**record)


def list_sessions() -> list[Session]:
    """Return every session under the runtime root, oldest first."""
    out = []
    for d in sorted(paths.runtime_root().iterdir()):
        record = _read_record(d / "session.json", look_again=paths.is_session_id(d.name))
        if record is not None:
            out.append(Session(**record))
    return sorted(out, key=lambda s: s.created)


def next_seq(sess: Session) -> int:
    """Return the next command sequence number, unique across concurrent callers (a lock)."""
    lock_path = run_dir(sess) / "seq.lock"
    seq_path = run_dir(sess) / "seq"
    with open(lock_path, "a", encoding="utf-8") as lock:
        platform.locks.lock_wait(lock)
        try:
            value = int(seq_path.read_text(encoding="utf-8")) + 1 if seq_path.exists() else 1
            # Temp file + rename (QA SL2-9): write_text truncates first, so an interruption
            # (SIGTERM -> SystemExit) mid-write left an empty file that broke every later call.
            tmp = seq_path.with_suffix(".tmp")
            tmp.write_text(str(value), encoding="utf-8")
            os.replace(tmp, seq_path)
        finally:
            platform.locks.unlock(lock)
    return value


def audit(sess: Session, kind: str, text: str) -> None:
    """Append one JSON line describing a privileged action to the session's audit.log AND to the
    persistent audit.log in paths.state_dir().

    WHY two copies: the run folder lives on tmpfs and `fsoctl cleanup` deletes it, which used to
    erase every record of eval use with it (QA SA-8). The persistent copy carries the session id.
    """
    now = time.time()
    with open(run_dir(sess) / "audit.log", "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": now, "kind": kind, "text": text}) + "\n")
    with open(paths.state_dir() / "audit.log", "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": now, "sid": sess.id, "kind": kind, "text": text}) + "\n")
