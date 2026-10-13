"""Matches: one private, loopback-only network shared by the sessions of a multiplayer game.

WHY a namespace and not firewall rules: the engine binds its UDP game port on every interface
and has no bind-address option (psnet_init_socket, [::]:7808), and the standalone server opens a
web panel on TCP 8080. Rules on the real loopback need root and leave the ports on the LAN between
rule changes. An unprivileged user+network namespace (unshare --user --map-root-user --net) gives
the engine a network with only loopback and no route out; the kernel enforces it, no root is
needed, and nothing exists on the real network at any moment. Probed 2026-10-12 (spec section 3,
probe-results section 21.2).

WHY a holder process: a network namespace lives as long as something holds it. The holder is a
`sleep infinity` inside the namespace, in its own systemd scope so `stop` and `status` can find
it, and its pid is what sessions join through (`nsenter --target <pid>`). The namespace dies with
the holder, which is exactly the lifetime the spec wants (section 5.6).

Records live in <runtime root>/matches/<id>/match.json, read fresh by every verb (no daemon).
"""
import json
import os
import secrets
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import paths, scope, session
from .errors import EXIT_LAUNCH, EXIT_NOT_FOUND, EXIT_REFUSED, HarnessError

# --map-root-user, not --map-current-user: only the namespace's own root may configure loopback
# (probed 2026-10-12: --map-current-user fails with "Operation not permitted" on
# `ip link set lo up`).
# The uid-0 inside maps to the real user outside; it carries no privilege on the host.
HOLDER_ARGV = ["unshare", "--user", "--map-root-user", "--net", "sh", "-c",
               "ip link set lo up && exec sleep infinity"]
HOLDER_RUNTIME_SEC = 24 * 3600      # a match never outlives a day; its sessions have their own caps
DEFAULT_MAX_MATCHES = 8             # live holders at once (FSO_HARNESS_MAX_MATCHES overrides)
_HOLDER_MEM_KB = 131072             # 128 MiB is far more than a sleep needs
_LO_UP_TIMEOUT = 5.0
LOCK_WAIT = 60.0                    # seconds to wait for launch.lock (launcher._LOCK_WAIT)


@dataclass
class Match:
    """One private network and what is known about its holder."""
    id: str
    scope: str
    holder_pid: int | None = None
    # /proc/<pid>/stat field 22 (start time in clock ticks since boot): with the pid it makes a
    # handle no later process can reuse by accident (spec section 6).
    holder_start: int | None = None
    created: float = field(default_factory=time.time)
    label: str = ""
    # starting | alive | removing | dead | removed. "removing" is written under the launch lock
    # by remove_match so a concurrent launch is refused (QA SL-4); "dead" is written back by the
    # first read that finds the holder gone (QA SL-7), with one audit line (QA SA-7).
    status: str = "starting"


def scope_name(mid: str) -> str:
    """Return the holder's systemd scope name (without ".scope")."""
    return f"fso-harness-match-{mid}"


def pid_start(pid: int) -> int | None:
    """Return the process's start time (clock ticks since boot) from /proc, or None if it is gone.

    /proc/<pid>/stat: "pid (comm) state ..." and comm may contain spaces or parentheses, so the
    fields are taken after the LAST ')'; field 22 of the man page is then index 19.
    """
    try:
        with open(f"/proc/{pid}/stat") as f:
            text = f.read()
    except OSError:
        return None
    rest = text.rsplit(")", 1)[1].split()
    return int(rest[19])


def save(m: Match) -> None:
    """Write match.json atomically (temp file + rename), creating the match folder."""
    d = paths.match_dir(m.id)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "match.json.tmp"
    tmp.write_text(json.dumps(asdict(m), indent=1))
    os.replace(tmp, d / "match.json")


def load(mid: str) -> Match:
    """Read a match by id; EXIT_NOT_FOUND when there is none."""
    f = paths.match_dir(mid) / "match.json"
    if not f.exists():
        raise HarnessError(EXIT_NOT_FOUND, f"no match {mid}", "use `fsoctl match list`")
    return Match(**json.loads(f.read_text()))


def list_matches() -> list[Match]:
    """Return every match record under the runtime root, oldest first."""
    out = []
    for d in sorted(paths.matches_root().iterdir()):
        f = d / "match.json"
        if f.exists():
            out.append(Match(**json.loads(f.read_text())))
    return sorted(out, key=lambda m: m.created)


def audit(m: Match, kind: str, text: str) -> None:
    """Append one JSON line to the persistent audit log: a private network is a resource worth a
    record (spec section 7), and the runtime copy would vanish with the match folder."""
    with open(paths.state_dir() / "audit.log", "a") as f:
        f.write(json.dumps({"t": time.time(), "mid": m.id, "kind": kind, "text": text}) + "\n")


def alive(m: Match) -> bool:
    """True while the record says "alive", the holder's scope is active AND the recorded pid still
    has the recorded start time. All three: the scope alone cannot tell a reused pid; the pid
    alone cannot tell a scope systemd is tearing down; a record being removed must refuse new
    sessions before its holder is gone (QA SL-4)."""
    if m.holder_pid is None or m.holder_start is None or m.status != "alive":
        return False
    return scope.is_active(m.scope) and pid_start(m.holder_pid) == m.holder_start


def observe(m: Match) -> Match:
    """Return the match with a dead holder written back as status "dead", once, with one audit
    line. Every list/status read goes through here (QA SL-7, SA-7, TA-4, TA-13): "dead" is loss
    of the recorded handle, not death of the network (member processes keep it alive until they
    exit), and sessions in it are still stoppable through their own scopes."""
    if m.status == "alive" and not alive(m):
        m.status = "dead"
        save(m)
        audit(m, "match_dead", f"holder pid {m.holder_pid} gone; sessions keep their own scopes")
    return m


def nsenter_prefix(m: Match) -> list[str]:
    """Return the command prefix that runs something inside the match's network.

    Inside the namespace the caller's real uid IS uid 0 (--map-root-user), so the engine, Xvfb
    and the runner see geteuid() == 0 there; it carries no right on the host beyond the real
    user's. --preserve-credentials is REQUIRED, not a choice: it skips nsenter's setgroups
    call, which --map-root-user denies (QA SA-5, CR-3, SL-8; probed). --user is needed because
    the network namespace is owned by the user namespace. Refuses (EXIT_REFUSED) a match that is
    not alive: a reused pid would join the wrong namespace, and a match being removed must take
    no new session.
    """
    if not alive(m):
        why = "being removed" if m.status == "removing" else "dead (holder gone)"
        raise HarnessError(EXIT_REFUSED, f"match {m.id} is {why}",
                           "create a new match with `fsoctl match create`")
    return ["nsenter", "--preserve-credentials", "--user", "--net", "--target",
            str(m.holder_pid), "--"]


def run_inside(m: Match, argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess:
    """Run `argv` inside the match's network and return the completed process (text output)."""
    return subprocess.run(nsenter_prefix(m) + argv, capture_output=True, text=True,
                          timeout=timeout, check=False)


def _wait_loopback(m: Match) -> None:
    """Block (bounded) until loopback is up inside the new namespace; EXIT_LAUNCH otherwise."""
    deadline = time.monotonic() + _LO_UP_TIMEOUT
    while time.monotonic() < deadline:     # bounded by the deadline
        r = run_inside(m, ["ip", "-brief", "link", "show", "lo"])
        if r.returncode == 0 and "UP" in r.stdout:
            return
        time.sleep(0.1)
    raise HarnessError(EXIT_LAUNCH, f"loopback never came up inside match {m.id}",
                       "is `unshare --user --net` allowed? check "
                       "/proc/sys/user/max_user_namespaces")


def create(label: str = "") -> Match:
    """Start a holder in its own scope and record the match; return it once loopback is up.

    Refuses to run as the real root (spec section 5.4): the whole design is unprivileged, and a
    root holder would own a namespace with real capabilities.
    """
    if os.getuid() == 0:
        raise HarnessError(EXIT_REFUSED, "fsoctl match verbs refuse to run as root",
                           "run as your normal user")
    # Under the launch lock (spec section 6, QA SL-11): the same lock a launch holds while it
    # checks the cap and joins a match, so create/launch/remove never interleave.
    with session.file_lock(paths.runtime_root() / "launch.lock", LOCK_WAIT):
        cap = int(os.environ.get("FSO_HARNESS_MAX_MATCHES", DEFAULT_MAX_MATCHES))
        live = [x for x in list_matches() if alive(x)]
        if len(live) >= cap:
            # QA XFK-3: a holder lives a day; a looping caller must not pile them up.
            raise HarnessError(EXIT_REFUSED, f"{len(live)} matches already alive (cap {cap})",
                               "remove one with `fsoctl match remove <id>` or raise "
                               "FSO_HARNESS_MAX_MATCHES")
        for _ in range(16):  # bounded: 16 collisions in 2^32 is effectively impossible
            mid = "m" + secrets.token_hex(4)
            if not paths.match_dir(mid).exists():
                break
        else:
            raise HarnessError(EXIT_NOT_FOUND, "could not allocate a match id", "retry")
        m = Match(id=mid, scope=scope_name(mid), label=label)
        save(m)
        log = paths.match_dir(mid) / "holder.log"
        props = scope.caps(runtime_sec=HOLDER_RUNTIME_SEC, cpu_cores=1, tasks=16,
                           mem_kb=_HOLDER_MEM_KB)
        try:
            proc = scope.start(m.scope, HOLDER_ARGV, dict(os.environ), log, props)
            # unshare execs sh, and sh execs sleep, so the scope's command pid IS the holder.
            m.holder_pid, m.holder_start = proc.pid, pid_start(proc.pid)
            if m.holder_start is None:
                # The holder exited at once (user namespaces refused, unshare missing...):
                # holder.log holds the reason, which the "dead match" refusal would hide (QA CR-2).
                raise HarnessError(EXIT_LAUNCH, f"the match holder exited at once: "
                                   f"{_log_tail(log)}", "is `unshare --user --net` allowed? "
                                   "check /proc/sys/user/max_user_namespaces")
            m.status = "alive"
            save(m)
            _wait_loopback(m)
        except HarnessError as e:
            # Whatever failed, the scope must not outlive the record (QA SL-5): scope.stop is a
            # no-op on a unit systemd never saw, so this is safe on every path.
            end_holder(m, "match_create_failed", e.message)
            raise
    audit(m, "match_create", f"holder pid {m.holder_pid} scope {m.scope} label {label!r}")
    return m


def _log_tail(log: Path, lines: int = 5) -> str:
    """Return the last `lines` lines of `log`, or a note that it is empty or missing."""
    try:
        text = log.read_text(errors="replace").strip()
    except OSError:
        return "(no holder.log)"
    return " | ".join(text.splitlines()[-lines:]) if text else "(holder.log is empty)"


def end_holder(m: Match, kind: str = "match_remove", text: str | None = None) -> None:
    """Stop the holder's scope (the network dies with it) and mark the match removed."""
    scope.stop(m.scope)
    m.status = "removed"
    save(m)
    audit(m, kind, text or f"holder pid {m.holder_pid}")


# ---- command line ----------------------------------------------------------------------------
import argparse  # noqa: E402  (verb registration lives at the bottom, as in launcher.py)

from . import launcher  # noqa: E402  (module object only: launcher imports this module at its top,
#                                 so names are looked up at call time, never at import time)
from .cli import register  # noqa: E402
from .errors import EXIT_USAGE  # noqa: E402


def _configure_match(p: argparse.ArgumentParser) -> None:
    """`match create [--label L]` | `match list` | `match remove <id>`."""
    p.add_argument("action", choices=("create", "list", "remove"))
    p.add_argument("id", nargs="?", help="match id (remove)")
    p.add_argument("--label", default="", help="free text shown by `match list`")


@register("match", "create, list or remove a private multiplayer network", _configure_match)
def _cli_match(args: argparse.Namespace) -> dict:
    """Dispatch the match actions. `remove` lives in launcher (it stops sessions first)."""
    if args.action == "create":
        m = create(args.label)
        return {"id": m.id, "holder_pid": m.holder_pid, "scope": m.scope, "status": m.status}
    if args.action == "list":
        return {"matches": [{"id": m.id, "status": m.status, "alive": alive(m), "label": m.label,
                             "holder_pid": m.holder_pid,
                             "sessions": [s.id for s in launcher.sessions_in_match(m.id)]}
                            for m in map(observe, list_matches())]}
    if not args.id:
        raise HarnessError(EXIT_USAGE, "match remove needs an id", "fsoctl match remove <id>")
    return launcher.remove_match(args.id)
