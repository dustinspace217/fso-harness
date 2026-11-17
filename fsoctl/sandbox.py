"""The "sandbox" network: the engine runs inside bubblewrap (bwrap) with no network, no home and
almost no filesystem, so an engine build from an untrusted pull request can be playtested.

WHY a sandbox at all: `--network private` only takes the network away. The engine still runs as
the user and can reach the whole filesystem by absolute path (the SSH folder, the gh login, the
desktop's X server, the session bus, the game folder and MediaVPs, its own session.json and the
shared Mesa cache). The nightly review launches engines built from other people's pull requests;
"sandbox" is the network value its head agent passes for those (design: the FSO project
folder's docs/plans/2026-11-13-nightly-sandbox.md, stage b).

What the engine sees (sandbox.argv): /usr read-only; its own Xvfb socket and no other X socket;
its binary's folder, FS2PATH and the named mods read-only, at their host paths; the session's
prefs folder read-write at INSIDE/prefs (the only folder the host can see it write); HOME,
XDG_RUNTIME_DIR, the cache and /tmp as size-capped tmpfs that exist only inside and vanish with
it; a one-user /etc/passwd and /etc/group the runner writes (write_identity); its own /proc and
a minimal /dev. The root is read-only. Nothing else of the host exists inside: no /run or
/sys, no /home or /mnt beyond the path down to FS2PATH or the binary's folder when those lie
there, and the run folder's session.json, journal and logs are not there (tests/
test_sandbox.py, hostile stand-in, on both a temporary and the real runtime folder). No kernel
key is reachable: key syscalls fail (seccomp_filter, a seccomp program), /proc/keys is
unreadable, and the sandbox starts in a fresh session keyring (KEYCTL).

WHY prefs is at a fixed inside path and not at its host path (QA 2026-11-13 CR-6, Phase B c):
the inside view should not depend on where the host keeps it. The harness reads and writes
prefs on the host side only through safefs.

Where prefs lives, and its bound (QA AT2-1/SA2-1): on disk, under paths.sandbox_root()
(~/.cache/fso-harness/sandbox-runs/<id>/prefs), never on the user's /run/user tmpfs, whose few
GB of free space hold the desktop's sockets. Every file the sandbox writes is capped at
FILE_SIZE_LIMIT and open files at OPEN_FILES_LIMIT (prlimit; the engine's stdout log in the run
folder included), fallocate, io_uring and setxattr fail (seccomp_filter), and Watchdog kills
the sandbox and empties prefs once it holds more than PREFS_MAX_BYTES (or PREFS_MAX_ENTRIES
entries), cannot be measured, or the disk's free space falls under FREE_MIN_BYTES or drops more
than PREFS_MAX_BYTES + DROP_MARGIN_BYTES since launch (files unlinked but kept open), checked
every WATCH_INTERVAL seconds and logged at warning level. What can still pass: what
the engine writes in one WATCH_INTERVAL; a filesystem quota would be the hard bound.

WHY bwrap and not podman (Dustin's ruling P1, 2026-11-13): bwrap is a plain child process, so it
stays in the session's systemd scope; the scope's memory, CPU and task caps, `fsoctl stop` and
the runtime limit keep working unchanged. A rootless podman container moves into its own cgroup,
outside the scope. Only the ENGINE is wrapped: the runner, Xvfb, the bridge files, screenshots and
input stay on the host, exactly as for a private session.

Not covered (design, "Risks and out of scope"): bwrap applies no seccomp filter, so a kernel bug
reachable through the user-namespace or syscall surface, or an X server bug reachable through the
session's Xvfb socket, is outside what this protects. No GPU is bound, so the sandbox is OpenGL
only (Mesa's software renderer); the launcher refuses Vulkan here (Dustin's ruling P3).
"""
import contextlib
import errno
import json
import os
import select
import signal
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import session
from .errors import EXIT_LAUNCH, HarnessError
from .platform import safefs

BWRAP = "bwrap"
PRLIMIT = "prlimit"             # util-linux; wraps bwrap with the per-file size limit
# keyutils; `keyctl session - CMD` runs CMD in a new anonymous session keyring. WHY: bwrap's new
# user namespace does not cut the kernel keyrings; probed 2026-11-13, a process inside plain bwrap
# printed canary keys added to the host's @s and @u. The fresh session keyring alone is not
# enough (a later probe still listed the host's _uid.1000 and _ses keyrings in /proc/keys and
# could `keyctl show` them), so the sandbox also gets seccomp_filter() and an unreadable
# /proc/keys; keyctl stays as a second layer. keyctl execs CMD: the pid Popen returns is bwrap's.
KEYCTL = "keyctl"
# Classic BPF for seccomp (struct sock_filter: u16 code, u8 jt, u8 jf, u32 k).
_BPF_LD_ABS, _BPF_JEQ, _BPF_JGE, _BPF_RET = 0x20, 0x15, 0x35, 0x06
_SECCOMP_ALLOW = 0x7FFF0000
_SECCOMP_EPERM = 0x00050000 | 1          # SECCOMP_RET_ERRNO with errno 1 (EPERM)
_SECCOMP_EOPNOTSUPP = 0x00050000 | 95    # SECCOMP_RET_ERRNO with errno 95 (EOPNOTSUPP)
_SECCOMP_ENOSYS = 0x00050000 | 38        # SECCOMP_RET_ERRNO with errno 38 (ENOSYS)
_AUDIT_ARCH_X86_64 = 0xC000003E
_X32_SYSCALL_BIT = 0x40000000
# x86_64 syscall numbers (asm/unistd_64.h, checked 2026-11-13).
KEY_SYSCALLS = (248, 249, 250)          # add_key, request_key, keyctl
FALLOCATE = 285
IO_URING = (425, 426, 427)              # io_uring_setup, io_uring_enter, io_uring_register
XATTR_SET = (188, 189, 190, 463)        # setxattr, lsetxattr, fsetxattr, setxattrat
# What the filter refuses, and how, in the order it checks. Every other x86_64 call is allowed.
# - fallocate -> EOPNOTSUPP (QA AT3-2): space cannot be reserved without writing it; glibc's
#   posix_fallocate falls back to ordinary writes, which RLIMIT_FSIZE and the watchdog see.
# - io_uring -> ENOSYS (QA AT4-1): its operations (IORING_OP_FALLOCATE among them) run in the
#   kernel without passing this filter; ENOSYS reads as "kernel without io_uring" to callers.
# - add_key, request_key, keyctl -> EPERM (keyring probe 2026-11-13).
# - setxattr family -> EPERM (QA SA4-1): extended attributes take space no folder walk sees.
#   A real sandboxed engine runs fine without them (engine cells, 2026-11-13).
SECCOMP_RULES = ((FALLOCATE, _SECCOMP_EOPNOTSUPP), *((n, _SECCOMP_ENOSYS) for n in IO_URING),
                 *((n, _SECCOMP_EPERM) for n in KEY_SYSCALLS),
                 *((n, _SECCOMP_EPERM) for n in XATTR_SET))
# The engine's process id INSIDE the sandbox, which is what SDL writes into its window's
# _NET_WM_PID and so what `xdotool search --pid` must look for (session.window_pid). bwrap's own
# init is pid 1 of the new pid namespace and its first and only child, the engine, is pid 2
# (probed 2026-11-13: /proc/<child>/status "NSpid: <host pid> 2").
# WHY not bwrap's --as-pid-1 (which the design named): pid 1 of a pid namespace ignores every
# signal it has no handler for, its own included. Probed 2026-11-13: os.abort() as pid 1 ended
# with SIGSEGV (glibc's last-resort crash instruction) instead of SIGABRT, so an engine assert
# would show in crash_report as a segfault. As pid 2 the engine dies of the signals it raises.
ENGINE_NS_PID = 2
# Seconds to wait for bwrap to report its init's pid, and then for the engine to appear under it.
START_TIMEOUT = 10.0
_POLL = 0.05
# Environment variables passed into the sandbox, taken from the runner's prepared child
# environment (envsetup.child_env): bwrap's --clearenv removes everything else, the gh token,
# SSH_AUTH_SOCK and the session bus address included. PATH is set separately (fixed).
_ENV_KEYS = ("DISPLAY", "SDL_VIDEODRIVER", "GDK_BACKEND", "QT_QPA_PLATFORM",
             "SDL_VIDEO_ALLOW_SCREENSAVER", "LIBGL_ALWAYS_SOFTWARE", "FSO_PREFERENCES_PATH",
             "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
             "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
# Where the session's own folders sit inside the sandbox (see the module docstring).
INSIDE = "/fso-harness"
# Folders that exist only inside, as tmpfs capped at these sizes in bytes: the engine's HOME, its
# XDG runtime folder and its cache (Mesa's shader cache; a sandboxed engine never shares the
# user's ~/.cache/fso-harness/cache, so it cannot leave entries a later unsandboxed session would
# load), plus /tmp. They count against the scope's memory limit and vanish with the sandbox.
TMPFS = (("/tmp", 64 << 20), (f"{INSIDE}/home", 64 << 20),
         (f"{INSIDE}/xdg-runtime", 16 << 20), (f"{INSIDE}/cache", 512 << 20))
# Files of the run folder bound read-only over /etc/<name> inside (write_identity).
IDENTITY = ("passwd", "group")
# The fixed part of the bwrap command line. --unshare-all: new network, pid, ipc, uts, cgroup,
# user and mount namespaces (no network but loopback). --disable-userns: no further user
# namespace can be made inside (the main extra kernel surface). --die-with-parent: the engine is
# killed if the runner dies, so it can never outlive its session. --new-session: no access to a
# controlling terminal. --clearenv: start from an empty environment.
_FLAGS = ["--unshare-all", "--unshare-user", "--disable-userns", "--die-with-parent",
          "--new-session", "--clearenv"]
# The host's system files: /usr and the merged-/usr links Fedora (and most distributions) use,
# plus the few /etc files the dynamic linker and fonts read. "-try" binds skip a missing file.
_SYSTEM = ["--ro-bind", "/usr", "/usr", "--symlink", "usr/lib64", "/lib64",
           "--symlink", "usr/lib", "/lib", "--symlink", "usr/bin", "/bin",
           "--symlink", "usr/sbin", "/sbin",
           "--ro-bind-try", "/etc/ld.so.cache", "/etc/ld.so.cache",
           "--ro-bind-try", "/etc/fonts", "/etc/fonts",
           "--ro-bind-try", "/etc/alternatives", "/etc/alternatives",
           "--proc", "/proc", "--dev", "/dev",
           # Key descriptions are readable from /proc/keys without any keyctl call: hide both
           # files (a /dev/null bind there reads as "Permission denied", probed 2026-11-13).
           "--ro-bind", "/dev/null", "/proc/keys", "--ro-bind", "/dev/null", "/proc/key-users"]


def seccomp_filter() -> bytes:
    """Return the seccomp program bwrap loads (--seccomp), built from SECCOMP_RULES.

    Layout: load the architecture; anything but x86_64 (a 32-bit int 0x80 call would use other
    numbers) fails with EPERM; load the syscall number; any x32 call (number with the 0x40000000
    bit) fails with EPERM; one JEQ per rule, jumping to the RET of that rule's answer; RET ALLOW;
    then one RET per distinct answer (EPERM first). Jump offsets are computed, not hand-counted
    (they are relative to the next instruction and must fit in a byte).
    WHY hand-assembled: libseccomp's Python binding is not installed and adding a package needs
    Dustin's yes; a short fixed program is easy to pin and test (tests run it through a small BPF
    interpreter and pin its bytes). x86_64 only, like the engine builds it runs.
    """
    answers = [_SECCOMP_EPERM, *dict.fromkeys(r for _, r in SECCOMP_RULES if r != _SECCOMP_EPERM)]
    first_ret = 4 + len(SECCOMP_RULES) + 1          # header, rules, RET ALLOW
    ret_at = {a: first_ret + i for i, a in enumerate(answers)}
    ins = [(_BPF_LD_ABS, 0, 0, 4),                                   # A = seccomp_data.arch
           (_BPF_JEQ, 0, ret_at[_SECCOMP_EPERM] - 2, _AUDIT_ARCH_X86_64),
           (_BPF_LD_ABS, 0, 0, 0),                                   # A = seccomp_data.nr
           (_BPF_JGE, ret_at[_SECCOMP_EPERM] - 4, 0, _X32_SYSCALL_BIT)]
    for nr, answer in SECCOMP_RULES:
        ins.append((_BPF_JEQ, ret_at[answer] - len(ins) - 1, 0, nr))
    ins.append((_BPF_RET, 0, 0, _SECCOMP_ALLOW))
    ins += [(_BPF_RET, 0, 0, a) for a in answers]
    if any(not 0 <= jt < 256 or not 0 <= jf < 256 for _, jt, jf, _ in ins):
        raise ValueError("seccomp filter jump out of range")        # a rule table too long
    return b"".join(struct.pack("<HBBI", *i) for i in ins)


def x_socket(display: str) -> str:
    """Return the filesystem socket path of X display ":N" (/tmp/.X11-unix/XN).

    Receives display from the session record (the runner's Xvfb). Raises HarnessError for a
    value that is not ":<digits>", so a malformed record can never bind some other path.
    WHY only the filesystem socket: inside a new network namespace the abstract X socket is
    unreachable, and X clients fall back to this path (the same fact private sessions rely on).
    """
    num = display[1:] if display.startswith(":") else ""
    if not num.isdigit():
        raise HarnessError(EXIT_LAUNCH, f"session display {display!r} is not :N",
                           "the sandbox binds one X socket by number; relaunch the session")
    return f"/tmp/.X11-unix/X{num}"


def mod_targets(fs2path: str, mods: list[str]) -> list[str]:
    """Return the real folders of the named mods that lie OUTSIDE FS2PATH (sorted, no repeats).

    A mod folder in FS2PATH may be a symlink into another library; binding FS2PATH alone would
    leave that link dangling inside the sandbox. Each list entry may itself hold several
    comma-separated names, since the engine splits -mod on commas.
    """
    root = Path(fs2path).resolve()
    out = set()
    for name in ",".join(mods).split(","):
        if name in ("", ".", "..") or "/" in name:
            continue                  # never a mod folder; the launcher refuses these anyway
        target = (root / name).resolve()
        if target.is_dir() and not target.is_relative_to(root):
            out.add(str(target))
    return sorted(out)


def _binds(sess: session.Session) -> list[str]:
    """Return the session-specific mount arguments: the size-capped tmpfs folders, the X socket,
    the binary's folder and the game data (read-only, at their host paths, so the engine's
    argv and working folder need no translation), the identity files, and prefs (read-write).
    """
    out: list[str] = []
    for path, size in TMPFS:
        perms = ["--perms", "0700"] if path.endswith("xdg-runtime") else []   # XDG requires 0700
        out += ["--size", str(size), *perms, "--tmpfs", path]
    sock = x_socket(sess.display or "")
    out += ["--ro-bind", sock, sock,
            "--ro-bind", str(Path(sess.binary).parent), str(Path(sess.binary).parent),
            "--ro-bind", sess.fs2path, sess.fs2path]
    for target in mod_targets(sess.fs2path, sess.mods):
        out += ["--ro-bind", target, target]
    if sess.cwd:
        # A shadow working folder (shadowdir.py) holds links into FS2PATH; read-only like it.
        out += ["--ro-bind", sess.cwd, sess.cwd]
    run = session.run_dir(sess)
    for name in IDENTITY:
        out += ["--ro-bind", str(run / name), f"/etc/{name}"]
    return [*out, "--bind", str(session.prefs_dir(sess)), f"{INSIDE}/prefs"]


def write_identity(run: Path) -> None:
    """Write the sandbox's /etc/passwd and /etc/group into the run folder (read-only inside;
    the run folder itself is not visible to the engine). WHY: with no passwd entry for
    its user id, GLib inside the engine logged "getpwuid_r(): failed due to unknown user id"
    (T5 log diff, 2026-11-13), a line no private-network run has. One fixed name, not the host's
    files, so the sandbox learns nothing about the machine's accounts.
    """
    uid, gid = os.getuid(), os.getgid()
    (run / "passwd").write_text(f"fso:x:{uid}:{gid}:fso-harness sandbox:{INSIDE}/home:"
                                "/sbin/nologin\n", encoding="utf-8")
    (run / "group").write_text(f"fso:x:{gid}:\n", encoding="utf-8")


def argv(sess: session.Session, env: dict, info_fd: int, seccomp_fd: int) -> list[str]:
    """Return the bwrap command line that goes in front of the engine's own argv.

    Receives: sess (an ordinary game session with network "sandbox" and a display), env (the
    child environment the runner prepared, see sandbox_env; only _ENV_KEYS pass), info_fd (the
    write end of a pipe the runner inherits into bwrap: bwrap writes its init's host pid there as
    JSON, read_info), seccomp_fd (the read end of a pipe holding seccomp_filter(); bwrap reads it
    whole and applies it to the engine). Returns prlimit + keyctl + bwrap's command line, ending
    in "--", so the engine's argv follows unparsed.
    """
    # prlimit sets RLIMIT_FSIZE on itself and execs keyctl, which joins a fresh session keyring
    # and execs bwrap (same pid throughout), so bwrap and everything inside inherit both; soft =
    # hard, so nothing inside can raise the limit (see FILE_SIZE_LIMIT and KEYCTL).
    out = [PRLIMIT, f"--fsize={FILE_SIZE_LIMIT}:{FILE_SIZE_LIMIT}",
           f"--nofile={OPEN_FILES_LIMIT}:{OPEN_FILES_LIMIT}", "--", KEYCTL, "session", "-",
           BWRAP, *_FLAGS, "--info-fd", str(info_fd), "--seccomp", str(seccomp_fd),
           "--setenv", "PATH", "/usr/bin"]
    for key in _ENV_KEYS:
        if env.get(key) is not None:
            out += ["--setenv", key, env[key]]
    # --remount-ro / last: the root tmpfs bwrap builds the tree on becomes read-only; the mounts
    # on it (the tmpfs folders, prefs) keep their own modes (probed 2026-11-13).
    return [*out, *_SYSTEM, *_binds(sess), "--remount-ro", "/",
            "--chdir", sess.cwd or sess.fs2path, "--"]


def sandbox_env(env: dict) -> dict:
    """Return `env` (changed in place) with every folder variable pointing inside the sandbox:
    prefs, HOME and the XDG folders under INSIDE (see TMPFS), and a bus address that never
    exists. envsetup.child_env's host-side values for them name run-folder paths the sandbox
    does not have, and its shared cache folder is exactly what a sandboxed engine must not
    reach."""
    home = f"{INSIDE}/home"
    env.update({"FSO_PREFERENCES_PATH": f"{INSIDE}/prefs/", "HOME": home,
                "XDG_CONFIG_HOME": f"{home}/.config", "XDG_DATA_HOME": f"{home}/.local/share",
                "XDG_STATE_HOME": f"{home}/.local/state", "XDG_CACHE_HOME": f"{INSIDE}/cache",
                "XDG_RUNTIME_DIR": f"{INSIDE}/xdg-runtime",
                "DBUS_SESSION_BUS_ADDRESS": f"unix:path={INSIDE}/no-bus"})
    return env


def read_info(rfd: int, timeout: float) -> int | None:
    """Read bwrap's --info-fd JSON from `rfd` and return its "child-pid" (bwrap's init, as a
    host pid), or None if bwrap closed the pipe without one (it failed before starting the
    command; its error is in engine.log) or nothing complete arrived within `timeout` seconds.

    Bounded with select() so a wedged bwrap cannot block the runner (same pattern as the Xvfb
    display pipe). The JSON is a few hundred bytes; the read stops at end of file.
    """
    deadline = time.monotonic() + timeout
    data = b""
    while len(data) < 65536:          # bounded: bwrap writes ~300 bytes
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([rfd], [], [], left)[0]:
            return None
        chunk = os.read(rfd, 4096)
        if not chunk:
            break
        data += chunk
    try:
        pid = json.loads(data).get("child-pid")
    except (ValueError, AttributeError):
        return None
    return pid if isinstance(pid, int) and pid > 0 else None


def _children(init_pid: int) -> list[str] | None:
    """Return the host pids (as text) of bwrap's init's children, None once the init is gone."""
    try:
        children = Path(f"/proc/{init_pid}/task/{init_pid}/children")
        return children.read_text(encoding="utf-8").split()
    except OSError:
        return None


def _open_pidfd(pid: int) -> int:
    """os.pidfd_open, named here so tests replace this module's use only (QA TA3-2)."""
    return os.pidfd_open(pid)


def _close_fd(fd: int) -> None:
    """os.close for find_engine's pidfd, named here for the same reason as _open_pidfd."""
    os.close(fd)


def find_engine(init_pid: int, running: Callable[[], bool],
                timeout: float) -> tuple[int, int] | None:
    """Return (host pid, pidfd) of the engine: the one child of bwrap's init `init_pid`.

    Receives running (a function answering whether bwrap is still running). Polls the init's
    children list until it names one, bwrap exits (None: the engine already ended; the runner
    records that exit), or `timeout` passes (None as well; the caller treats a still-running
    sandbox without an engine as a failure).
    WHY a pidfd (QA SA-4/AT-4/CR-4): a pid number can be reused by an unrelated process once the
    engine is gone, and a later SIGTERM by number would hit that process. A pidfd names this one
    process for good; it is opened and THEN the children list is read again, so the pidfd is
    known to be the init's child and not a process that took the number in between.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and running():
        kids = _children(init_pid)
        if kids is None:
            return None               # the init is gone: so is the engine
        if kids:
            try:
                fd = _open_pidfd(int(kids[0]))
            except ProcessLookupError:
                return None
            if kids[0] in (_children(init_pid) or []):
                return int(kids[0]), fd
            _close_fd(fd)
            return None
        time.sleep(_POLL)
    return None


def exit_code(code: int | None) -> int | None:
    """Turn bwrap's exit status back into the engine's, in Popen's convention.

    bwrap exits with the engine's own code, or 128+N when the engine died of signal N (probed
    2026-11-13: a segfault gave 139, an abort 134). Popen reports a signal death as -N, which is
    what the runner records and crash_report reads (exit_info["signal"]). Returns -N for
    129..192, the code unchanged otherwise. Honest limit: an engine that itself exits with
    129..192 is read as a signal death; FSO exits with 0 or 1.
    """
    if code is not None and 128 < code <= 128 + 64:
        return -(code - 128)
    return code


class Engine:
    """A sandboxed engine behind Popen's three methods the runner uses (wait, terminate, kill).

    WHY an adapter: the runner's stop and runtime-limit paths (runner._reap, _run_engine) call
    those methods on the process they started, and for a sandbox that process is bwrap, not the
    engine. bwrap does not pass SIGTERM on: it dies of it and --die-with-parent then SIGKILLs the
    engine (probed 2026-11-13), which would cut the engine's clean shutdown. So terminate()
    signals the engine itself, kill() ends bwrap (and with it the whole sandbox), and wait()
    translates bwrap's status (exit_code). One code path for every network keeps the reap
    order the runner's tests pin.
    """

    def __init__(self, proc, pid: int | None, pidfd: int | None,
                 bwrap_pidfd: int | None = None) -> None:
        """Receives proc (the bwrap Popen), pid (the engine's host pid, for the record), pidfd
        (find_engine's; both None if the engine ended before it could be found) and bwrap_pidfd
        (a pidfd on bwrap itself, for kill(); None falls back to proc.kill()). The adapter owns
        both descriptors and closes them once bwrap has exited."""
        self.proc = proc
        self.pid = pid
        self.pidfd = pidfd
        self.bwrap_pidfd = bwrap_pidfd
        # The watchdog thread may call terminate() while the runner's thread closes the pidfd in
        # wait(); the lock keeps a closed (and possibly reused) descriptor number from being
        # signalled.
        self._lock = threading.Lock()

    def wait(self, timeout: float | None = None) -> int | None:
        """Wait for bwrap (and so the engine) to end; return the engine's code (exit_code)."""
        code = exit_code(self.proc.wait(timeout=timeout))
        with self._lock:
            for fd in (self.pidfd, self.bwrap_pidfd):
                if fd is not None:
                    os.close(fd)
            self.pidfd = self.bwrap_pidfd = None
        return code

    def terminate(self) -> None:
        """Send SIGTERM to the engine through its pidfd (SDL turns it into a quit event), never
        to bwrap and never by bare pid number."""
        with self._lock:
            if self.pidfd is None:
                self.proc.terminate()
                return
            # Already gone is fine (the pidfd then signals nothing): wait() collects bwrap's
            # status.
            with contextlib.suppress(ProcessLookupError):
                signal.pidfd_send_signal(self.pidfd, signal.SIGTERM)

    def kill(self) -> None:
        """SIGKILL bwrap through its pidfd (QA TA4-3: the watchdog calls this from its own
        thread, and a pid number could already belong to another process once the runner's
        thread has reaped bwrap); --die-with-parent then kills everything inside the sandbox."""
        with self._lock:
            if self.bwrap_pidfd is None:
                self.proc.kill()
                return
            with contextlib.suppress(ProcessLookupError):
                signal.pidfd_send_signal(self.bwrap_pidfd, signal.SIGKILL)

    def exited(self) -> bool:
        """True once bwrap (and so the engine) has exited. Reads returncode first: while the
        runner's thread waits on bwrap, poll() from another thread cannot take Popen's wait lock
        and answers None even after the exit."""
        return self.proc.returncode is not None or self.proc.poll() is not None


# Per-file size limit for everything in the sandbox (RLIMIT_FSIZE, set by prlimit, inherited):
# a write past it fails and the writer gets SIGXFSZ. It also bounds the engine's stdout log,
# which the runner keeps on the /run/user tmpfs. 256 MiB is far above any log or pilot file a
# normal run writes (QA AT2-1/SA2-1).
FILE_SIZE_LIMIT = 256 << 20
# Open files at once inside the sandbox (RLIMIT_NOFILE, set by prlimit). WHY: a file the engine
# unlinks but keeps open still fills the disk and no folder walk sees it; with up to a million
# descriptors (the hard limit here) that is 256 MiB times a million (QA SA3-1/CR3-1). A real
# sandboxed engine held 16 at the pilot screen and 17 in a mission (probed 2026-11-13,
# master Release build, MVPS 5.0.2); 512 leaves room for debug builds and mods. The statvfs
# floor below catches what is left.
OPEN_FILES_LIMIT = 512
# The prefs folder's total size (bytes on disk) and entry count at which the watchdog ends the
# session. FSO_HARNESS_SANDBOX_PREFS_MAX overrides the byte cap (the tests set a small one).
PREFS_MAX_BYTES = 1 << 30
PREFS_MAX_ENTRIES = 100_000
# Free space (bytes) the disk holding prefs must keep; below it the watchdog trips. It sees
# what the walk cannot: files unlinked but still open (QA CR3-1). Launch refuses below it too.
# FSO_HARNESS_SANDBOX_FREE_MIN overrides it (the tests set a small one).
FREE_MIN_BYTES = 10 << 30
# The drop in free space since launch (beyond the prefs cap) at which the watchdog trips (QA
# SA4-1/AT4-2): it counts whatever the sandbox takes from the disk however it takes it (files
# unlinked but kept open, extended attributes, metadata). Reflinks (FICLONE, copy_file_range on
# btrfs) share extents and cost nothing until they are written, and those writes show here too,
# so they need no rule of their own. Another program writing to the same disk during the run
# can trip it falsely; that ends a playtest early, which is the safe direction. It assumes one
# sandbox session per disk at a time (the nightly review runs cells one after another); launch
# warns when another sandbox session is live on the same disk (QA AT5-1).
# FSO_HARNESS_SANDBOX_DROP_MARGIN overrides it (the tests set a small one).
DROP_MARGIN_BYTES = 2 << 30
# Walks in a row that met folders vanishing under them before the watchdog trips (a walk that
# misses a renamed folder undercounts; renaming in a loop must not hide bytes).
VANISHING_WALKS = 3
WATCH_INTERVAL = 2.0            # seconds between the watchdog's walks of prefs
_EXIT_WAIT = 10.0               # seconds the watchdog waits for a killed sandbox before emptying
WATCHDOG_JOIN = 60.0            # seconds the runner waits for the watchdog thread to finish


def prefs_max_bytes() -> int:
    """Return the prefs byte cap: FSO_HARNESS_SANDBOX_PREFS_MAX if set to a positive integer,
    else PREFS_MAX_BYTES. A malformed value falls back to the default, never to no cap."""
    raw = os.environ.get("FSO_HARNESS_SANDBOX_PREFS_MAX", "")
    return int(raw) if raw.isdigit() and int(raw) > 0 else PREFS_MAX_BYTES


def free_min_bytes() -> int:
    """Return the free-space floor: FSO_HARNESS_SANDBOX_FREE_MIN if set to a positive integer,
    else FREE_MIN_BYTES (a malformed value falls back to the default, never to no floor)."""
    raw = os.environ.get("FSO_HARNESS_SANDBOX_FREE_MIN", "")
    return int(raw) if raw.isdigit() and int(raw) > 0 else FREE_MIN_BYTES


def drop_margin_bytes() -> int:
    """Return the drop margin: FSO_HARNESS_SANDBOX_DROP_MARGIN if set to a positive integer,
    else DROP_MARGIN_BYTES (a malformed value falls back to the default)."""
    raw = os.environ.get("FSO_HARNESS_SANDBOX_DROP_MARGIN", "")
    return int(raw) if raw.isdigit() and int(raw) > 0 else DROP_MARGIN_BYTES


def free_bytes(folder: Path) -> int:
    """Return the bytes an unprivileged user may still write on the filesystem holding
    `folder` (statvfs f_bavail * f_frsize)."""
    st = os.statvfs(folder)
    return st.f_bavail * st.f_frsize


def warn_loudly(text: str) -> None:
    """Write `text` at warning level where an admin sees it: the runner's log (stderr) and the
    user journal (logger -p user.warning), so a session ended for its size is never silent
    (workspace rule: corrective code logs when it acts)."""
    print(f"WARNING fso-harness: {text}", file=sys.stderr, flush=True)
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(["logger", "-p", "user.warning", "-t", "fso-harness", text],
                       timeout=5, check=False, capture_output=True)


class Watchdog(threading.Thread):
    """Ends a sandboxed engine whose prefs folder grows past its cap (QA AT2-1/SA2-1, 3b, 3c).

    Every WATCH_INTERVAL seconds it walks prefs through safefs.usage (no link followed, entries
    capped, unreadable folders made readable again) and reads the disk's free space. It fails
    closed and trips: past the byte cap, past the entry cap, below the free-space floor, when
    free space has dropped more than the cap plus DROP_MARGIN_BYTES since launch (what no walk
    can see), when VANISHING_WALKS walks in a row met folders vanishing under them, on a tree it
    cannot measure, or on prefs missing while the engine runs. A trip records ended_by
    "disk_limit" at once (a later stop must not overwrite it), logs loudly, SIGKILLs the sandbox
    with no grace (QA SA3-2), waits up to _EXIT_WAIT seconds for it to go, and empties prefs
    (AT3-2); every failing step is logged, not swallowed (TA3-3). WHY a poll and not inotify:
    the per-file RLIMIT_FSIZE stops one runaway file at once; a total needs a sum, and on disk a
    2-second poll is far faster than the disk fills.
    """

    def __init__(self, sid: str, prefs: Path, engine: "Engine", baseline: int | None = None
                 ) -> None:
        """Receives the session id (for the log and the record), its prefs folder, the engine
        handle and baseline (the prefs disk's free bytes before the engine started; None turns
        the drop check off)."""
        super().__init__(daemon=True, name=f"prefs-watchdog-{sid}")
        self.sid, self.prefs, self.engine, self.baseline = sid, prefs, engine, baseline
        self.tripped: str | None = None
        self._halt = threading.Event()

    def stop(self) -> None:
        """Ask the thread to end (the engine has exited); it ends within WATCH_INTERVAL."""
        self._halt.set()

    def check(self) -> str | None:
        """Return why prefs is over its caps, or None while it is within them."""
        try:
            size = self._size()
            free = free_bytes(self.prefs)
        except safefs.Unsafe as e:
            return f"prefs folder {self.prefs} cannot be measured within bounds: {e.strerror}"
        except FileNotFoundError:
            # Gone with the session is fine; gone while the engine still runs is not (it cannot
            # remove its own mount point, so something is wrong): fail closed (QA 3b).
            if self.engine is not None and self.engine.exited():
                return None
            return f"prefs folder {self.prefs} is missing while the engine runs"
        except OSError as e:
            return f"prefs folder {self.prefs} cannot be measured: {e}"
        return self._over(size, free)

    def _size(self) -> int:
        """Return prefs' size from a walk that met no vanishing folder, trying VANISHING_WALKS
        times; raise Unsafe when every walk met one (QA 3c: renaming folders in a loop)."""
        for _ in range(VANISHING_WALKS):      # bounded: a few walks, then trip
            size, _, vanished = safefs.usage(self.prefs, PREFS_MAX_ENTRIES)
            if not vanished:
                return size
        raise safefs.Unsafe(errno.EAGAIN, f"folders vanished during {VANISHING_WALKS} walks "
                            "in a row")

    def _over(self, size: int, free: int) -> str | None:
        """Return the reason when a limit is passed (see the class), else None."""
        cap, floor = prefs_max_bytes(), free_min_bytes()
        if size > cap:
            return f"prefs folder {self.prefs} holds {size} bytes, over the {cap}-byte cap"
        if free < floor:
            return (f"the disk holding {self.prefs} has {free} bytes free, under the "
                    f"{floor}-byte floor")
        if self.baseline is not None and self.baseline - free > cap + drop_margin_bytes():
            return (f"free space on the disk holding {self.prefs} dropped "
                    f"{self.baseline - free} bytes since launch, over the cap plus "
                    f"{drop_margin_bytes()}-byte margin")
        return None

    def run(self) -> None:
        """Intentionally loops until stop() (the runner calls it when the engine exits); each
        pass waits WATCH_INTERVAL seconds on the stop event. Ends after one trip.
        SIGTERM is blocked in this thread first (QA CR4-1): the runner was started before it
        blocks SIGTERM for its exit record, and a SIGTERM taken by this thread would run the
        runner's handler in its main thread mid-record and lose the exit code. The block keeps
        it pending for the main thread instead."""
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        while not self._halt.wait(WATCH_INTERVAL):
            reason = self.check()
            if reason is None or self._halt.is_set():
                continue                  # a stop that came during the check wins (CR4-2)
            self.trip(reason)
            return

    def trip(self, reason: str) -> None:
        """End the session for `reason`: record, log, kill, wait, empty prefs (see the class)."""
        self.tripped = reason
        self._step("record", lambda: session.update(self.sid, _mark_disk_limit))
        warn_loudly(f"session {self.sid}: {reason}; killing the sandboxed engine")
        self._step("kill", self.engine.kill)
        deadline = time.monotonic() + _EXIT_WAIT
        while not self.engine.exited() and time.monotonic() < deadline:   # bounded wait
            time.sleep(0.05)
        self._step("empty prefs", lambda: safefs.clear_tree(self.prefs))

    def _step(self, what: str, action: Callable[[], object]) -> None:
        """Run one trip step; log (never raise) an error, so the later steps still run."""
        try:
            action()
        except Exception as e:    # every failure is reported; none may stop the next step
            warn_loudly(f"session {self.sid}: watchdog could not {what}: {e!r}")


def _mark_disk_limit(sess: session.Session) -> None:
    """session.update callback: record that the size watchdog ended this session."""
    sess.ended_by = "disk_limit"
