"""Matches: the sessions of one multiplayer game, on the real network or a private one.

A REAL match (the default since 2026-10-16; owner: "Real network. I never meant to change this
about how the engine works.") is only a record that groups sessions: it has no holder and no
namespace, and its sessions bind their game ports on the host like the stock game, each on its
own port (launcher.host_port). A PRIVATE match (`--network private`) is the loopback-only
namespace described below, kept as an opt-in.

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

from . import paths, platform, session
from .errors import EXIT_LAUNCH, EXIT_NOT_FOUND, EXIT_REFUSED, EXIT_USAGE, HarnessError
from .platform import procs, scope

# --map-root-user, not --map-current-user: only the namespace's own root may configure loopback
# (probed 2026-10-12: --map-current-user fails with "Operation not permitted" on
# `ip link set lo up`).
# The uid-0 inside maps to the real user outside; it carries no privilege on the host.
HOLDER_ARGV = ["unshare", "--user", "--map-root-user", "--net", "sh", "-c",
               "ip link set lo up && exec sleep infinity"]
# A private match's holder lives a day; an empty real match is ended a day after creation
# (observe). Sessions have their own caps.
HOLDER_RUNTIME_SEC = 24 * 3600
DEFAULT_MAX_MATCHES = 8             # live matches at once (FSO_HARNESS_MAX_MATCHES overrides)
_HOLDER_MEM_KB = 131072             # 128 MiB is far more than a sleep needs
_LO_UP_TIMEOUT = 5.0
LOCK_WAIT = 60.0                    # seconds to wait for launch.lock (launcher._LOCK_WAIT)


@dataclass
class Match:
    """One match: its network and, for a private one, what is known about its holder."""
    id: str
    scope: str
    # "real" | "private". The default stays "private" so a record written before the field
    # existed (every match was private then) loads as what it was; create() passes it explicitly.
    network: str = "private"
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


def save(m: Match) -> None:
    """Write match.json atomically (temp file + rename), creating the match folder."""
    d = paths.match_dir(m.id)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "match.json.tmp"
    tmp.write_text(json.dumps(asdict(m), indent=1), encoding="utf-8")
    os.replace(tmp, d / "match.json")


def load(mid: str) -> Match:
    """Read a match by id; EXIT_NOT_FOUND when there is none."""
    f = paths.match_dir(mid) / "match.json"
    if not f.exists():
        raise HarnessError(EXIT_NOT_FOUND, f"no match {mid}", "use `fsoctl match list`")
    return Match(**json.loads(f.read_text(encoding="utf-8")))


def list_matches() -> list[Match]:
    """Return every match record under the runtime root, oldest first."""
    out = []
    for d in sorted(paths.matches_root().iterdir()):
        f = d / "match.json"
        if f.exists():
            out.append(Match(**json.loads(f.read_text(encoding="utf-8"))))
    return sorted(out, key=lambda m: m.created)


def audit(m: Match, kind: str, text: str) -> None:
    """Append one JSON line to the persistent audit log: a private network is a resource worth a
    record (spec section 7), and the runtime copy would vanish with the match folder."""
    with open(paths.state_dir() / "audit.log", "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.time(), "mid": m.id, "kind": kind, "text": text}) + "\n")


def alive(m: Match) -> bool:
    """True while the record says "alive" and, for a private match, the holder's scope is active
    AND the recorded pid still has the recorded start time. All three: the scope alone cannot tell
    a reused pid; the pid alone cannot tell a scope systemd is tearing down; a record being
    removed must refuse new sessions before its holder is gone (QA SL-4). A real match has no
    holder, so its record is all there is."""
    if m.network == "real":
        return m.status == "alive"
    if m.holder_pid is None or m.holder_start is None or m.status != "alive":
        return False
    return scope.is_active(m.scope) and procs.pid_start(m.holder_pid) == m.holder_start


def observe(m: Match) -> Match:
    """Return the match with a dead holder written back as status "dead", once, with one audit
    line. Every list/status read goes through here (QA SL-7, SA-7, TA-4, TA-13): "dead" is loss
    of the recorded handle, not death of the network (member processes keep it alive until they
    exit), and sessions in it are still stoppable through their own scopes.

    A real match has no holder to lose, so it is ended the same way once it is older than
    HOLDER_RUNTIME_SEC and no session in it is live (QA CR-4/TA-6/SL-4): otherwise an agent that
    never calls `match remove` would fill the match cap for good, and cleanup never removes a
    live match."""
    if m.status == "alive" and m.network == "real" and _abandoned(m):
        m.status = "dead"
        save(m)
        audit(m, "match_expired", "real match older than a day with no live session")
    elif m.status == "alive" and not alive(m):
        m.status = "dead"
        save(m)
        audit(m, "match_dead", f"holder pid {m.holder_pid} gone; sessions keep their own scopes")
    return m


def _abandoned(m: Match) -> bool:
    """True when real match `m` is older than HOLDER_RUNTIME_SEC and none of its sessions is
    live. Live: a scope not fully gone, or a "starting" record younger than the launch grace,
    whose scope may not exist yet (plan A QA XFA-4: observe runs without the launch lock, and a
    launch between creating the record and starting its scope must not see its match ended)."""
    now = time.time()
    if now - m.created <= HOLDER_RUNTIME_SEC:
        return False
    return not any(not scope.is_gone(s.scope)
                   or (s.status == "starting" and now - s.created < launcher._STARTING_GRACE)
                   for s in launcher.sessions_in_match(m.id))


def nsenter_prefix(m: Match) -> list[str]:
    """Return the command prefix that runs something inside the match's network.

    Inside the namespace the caller's real uid IS uid 0 (--map-root-user), so the engine, Xvfb
    and the runner see geteuid() == 0 there; it carries no right on the host beyond the real
    user's. --preserve-credentials is REQUIRED, not a choice: it skips nsenter's setgroups
    call, which --map-root-user denies (QA SA-5, CR-3, SL-8; probed). --user is needed because
    the network namespace is owned by the user namespace. Refuses (EXIT_REFUSED) a match that is
    not alive: a reused pid would join the wrong namespace, and a match being removed must take
    no new session. A real match has no namespace, so its prefix is empty (same refusal rule).
    """
    if not alive(m):
        why = ("being removed" if m.status == "removing"
               else "removed" if m.network == "real" and m.status == "removed"
               else "ended (a day old with no live session)" if m.network == "real"
               else "dead (holder gone)")
        raise HarnessError(EXIT_REFUSED, f"match {m.id} is {why}",
                           "create a new match with `fsoctl match create`")
    if m.network == "real":
        return []
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


def create(label: str = "", network: str = "real") -> Match:
    """Record a match and return it: a real one at once, a private one after its holder's
    loopback is up.

    Refuses to run as the real root (spec section 5.4): the whole design is unprivileged, and a
    root holder would own a namespace with real capabilities.
    """
    platform.needs_linux("match create", "Plan E")
    if network not in ("real", "private"):
        raise HarnessError(EXIT_USAGE, f"unknown network {network!r}", "use real or private")
    if os.getuid() == 0:
        raise HarnessError(EXIT_REFUSED, "fsoctl match verbs refuse to run as root",
                           "run as your normal user")
    # Under the launch lock (spec section 6, QA SL-11): the same lock a launch holds while it
    # checks the cap and joins a match, so create/launch/remove never interleave.
    with session.file_lock(paths.runtime_root() / "launch.lock", LOCK_WAIT):
        cap = int(os.environ.get("FSO_HARNESS_MAX_MATCHES", DEFAULT_MAX_MATCHES))
        live = [x for x in map(observe, list_matches()) if alive(x)]
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
        m = Match(id=mid, scope=scope_name(mid), label=label, network=network)
        if network == "real":
            m.status = "alive"
            save(m)
            audit(m, "match_create", f"real network, label {label!r}")
            return m
        save(m)
        log = paths.match_dir(mid) / "holder.log"
        props = scope.caps(runtime_sec=HOLDER_RUNTIME_SEC, cpu_cores=1, tasks=16,
                           mem_kb=_HOLDER_MEM_KB)
        try:
            proc = scope.start(m.scope, HOLDER_ARGV, dict(os.environ), log, props)
            # unshare execs sh, and sh execs sleep, so the scope's command pid IS the holder.
            m.holder_pid, m.holder_start = proc.pid, procs.pid_start(proc.pid)
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
        text = log.read_text(errors="replace", encoding="utf-8").strip()
    except OSError:
        return "(no holder.log)"
    return " | ".join(text.splitlines()[-lines:]) if text else "(holder.log is empty)"


def end_holder(m: Match, kind: str = "match_remove", text: str | None = None) -> None:
    """Stop the holder's scope (the network dies with it) and mark the match removed. A real
    match has no holder, so only its record changes."""
    if m.network != "real":
        scope.stop(m.scope)
    m.status = "removed"
    save(m)
    audit(m, kind, text or (f"holder pid {m.holder_pid}" if m.network != "real"
                            else "real network"))


# ---- command line ----------------------------------------------------------------------------
import argparse  # noqa: E402  (verb registration lives at the bottom, as in launcher.py)

from . import launcher  # noqa: E402  (module object only: launcher imports this module at its top,
#                                 so names are looked up at call time, never at import time)
from .cli import register  # noqa: E402


def _configure_match(p: argparse.ArgumentParser) -> None:
    """`match create [--label L] [--network real|private]` | `match list` | `match remove <id>`."""
    p.add_argument("action", choices=("create", "list", "remove"))
    p.add_argument("id", nargs="?", help="match id (remove)")
    p.add_argument("--label", default="", help="free text shown by `match list`")
    p.add_argument("--network", choices=("real", "private"), default="real",
                   help="real (default): the machine's network, as the stock game; private: a "
                        "loopback-only network nothing outside can reach")


@register("match", "create, list or remove a multiplayer match", _configure_match)
def _cli_match(args: argparse.Namespace) -> dict:
    """Dispatch the match actions. `remove` lives in launcher (it stops sessions first)."""
    if args.action == "create":
        m = create(args.label, args.network)
        return {"id": m.id, "network": m.network, "holder_pid": m.holder_pid, "scope": m.scope,
                "status": m.status}
    if args.action == "list":
        return {"matches": [{"id": m.id, "network": m.network, "status": m.status,
                             "alive": alive(m), "label": m.label,
                             "holder_pid": m.holder_pid,
                             "sessions": [s.id for s in launcher.sessions_in_match(m.id)]}
                            for m in map(observe, list_matches())]}
    if not args.id:
        raise HarnessError(EXIT_USAGE, "match remove needs an id", "fsoctl match remove <id>")
    return launcher.remove_match(args.id)
