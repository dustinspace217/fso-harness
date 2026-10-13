"""Session records: one JSON file per run folder, read fresh by every fsoctl call.

WHY files and no daemon: a long-lived broker holding session state goes stale when it outlives or
loses its processes (a lesson from an earlier MCP server). Here all state lives on disk next to
the run, and every caller (CLI, MCP, another agent) reads it fresh.
"""
import contextlib
import fcntl
import json
import os
import secrets
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import paths
from .errors import EXIT_NOT_FOUND, EXIT_TIMEOUT, HarnessError

# Created in the run folder by `fsoctl stop` just before it stops the scope, so the runner (which
# gets SIGTERM from that stop) can record ended_by="stop" rather than "signal".
STOP_MARKER = "stop-requested"


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
    engine_pid: int | None = None
    xvfb_pid: int | None = None
    created: float = field(default_factory=time.time)
    status: str = "starting"
    exit_info: dict | None = None
    # Working folder: a shadow folder for runs that write output files; None means fs2path.
    cwd: str | None = None
    # How the run ended: "stop" | "runtime_limit" | "oom" | "signal" | "exit"; None while it runs.
    # Written by the runner (QA SL-7), so a scope killed by its own limits is not left "running".
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
    # "private": the engine runs in its own loopback-only network (the default, DEF-MP-03);
    # "real": the machine's network; None: a record written before the field existed (its engine
    # ran on the real network; status shows null rather than a false "private", QA XFA-6).
    # A match session is always "private" (its match's). The runner reads this field.
    network: str | None = None


def run_dir(sess: Session) -> Path:
    """Return the session's run folder."""
    return paths.session_dir(sess.id)


def prefs_dir(sess: Session) -> Path:
    """Return the session's private FSO_PREFERENCES_PATH folder."""
    return run_dir(sess) / "prefs"


def scope_name(sid: str) -> str:
    """Return the systemd scope name (without ".scope") of session `sid`."""
    return f"fso-harness-{sid}"


def new_session(**fields) -> Session:
    """Allocate an id, create the run and prefs folders, save, and return the session."""
    for _ in range(16):  # bounded: 16 collisions in 2^32 is effectively impossible
        sid = "s" + secrets.token_hex(4)
        if not paths.session_dir(sid).exists():
            break
    else:
        raise HarnessError(EXIT_NOT_FOUND, "could not allocate a session id", "retry")
    sess = Session(id=sid, scope=scope_name(sid), **fields)
    prefs_dir(sess).mkdir(parents=True)
    save(sess)
    return sess


def save(sess: Session) -> None:
    """Write session.json atomically (temp file + rename)."""
    target = run_dir(sess) / "session.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(sess), indent=1))
    os.replace(tmp, target)


@contextlib.contextmanager
def file_lock(path: Path, timeout: float) -> Iterator[None]:
    """Hold an exclusive flock on `path` (created if missing) for the `with` block.

    Waits at most `timeout` seconds for another holder, then raises EXIT_TIMEOUT. The kernel
    drops the lock if the holder dies, so a crashed fsoctl can never leave it held.
    """
    with open(path, "a") as f:
        deadline = time.monotonic() + timeout
        while True:   # bounded by the deadline check below
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise HarnessError(EXIT_TIMEOUT, f"{path.name} is held by another fsoctl",
                                       "another launch or bridge install is running; retry "
                                       "in a moment") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


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
    with open(lock_path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            sess = load(sid)
            fn(sess)
            save(sess)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return sess


def load(sid: str) -> Session:
    """Read a session by id; raise EXIT_NOT_FOUND if it doesn't exist."""
    target = paths.session_dir(sid) / "session.json"
    if not target.exists():
        raise HarnessError(EXIT_NOT_FOUND, f"no session {sid}", "use `fsoctl list`")
    return Session(**json.loads(target.read_text()))


def list_sessions() -> list[Session]:
    """Return every session under the runtime root, oldest first."""
    out = []
    for d in sorted(paths.runtime_root().iterdir()):
        f = d / "session.json"
        if f.exists():
            out.append(Session(**json.loads(f.read_text())))
    return sorted(out, key=lambda s: s.created)


def next_seq(sess: Session) -> int:
    """Return the next command sequence number, unique across concurrent callers (flock)."""
    lock_path = run_dir(sess) / "seq.lock"
    seq_path = run_dir(sess) / "seq"
    with open(lock_path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            value = int(seq_path.read_text()) + 1 if seq_path.exists() else 1
            # Temp file + rename (QA SL2-9): write_text truncates first, so an interruption
            # (SIGTERM -> SystemExit) mid-write left an empty file that broke every later call.
            tmp = seq_path.with_suffix(".tmp")
            tmp.write_text(str(value))
            os.replace(tmp, seq_path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return value


def audit(sess: Session, kind: str, text: str) -> None:
    """Append one JSON line describing a privileged action to the session's audit.log AND to the
    persistent audit.log in paths.state_dir().

    WHY two copies: the run folder lives on tmpfs and `fsoctl cleanup` deletes it, which used to
    erase every record of eval use with it (QA SA-8). The persistent copy carries the session id.
    """
    now = time.time()
    with open(run_dir(sess) / "audit.log", "a") as f:
        f.write(json.dumps({"t": now, "kind": kind, "text": text}) + "\n")
    with open(paths.state_dir() / "audit.log", "a") as f:
        f.write(json.dumps({"t": now, "sid": sess.id, "kind": kind, "text": text}) + "\n")
