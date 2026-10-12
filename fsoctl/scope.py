"""Named systemd user scopes: the resource ceiling and the kill switch for every run.

WHY systemd-run --scope and not bounded-run: we need a KNOWN unit name so `stop` and `status` can
find the run later from another process; stopping the scope reaps the runner, Xvfb and the engine
together (cgroup), which a process-group kill does not guarantee. Caps follow bounded-run's idea
(a share of MemAvailable, no swap, CPU and task ceilings) plus a runtime limit.
"""
import shutil
import subprocess
import time
from pathlib import Path

from .errors import EXIT_ENGINE, EXIT_LAUNCH, HarnessError

_MEM_FLOOR_KB = 2 * 1024 * 1024
_MEM_SHARE = 0.60
_START_TIMEOUT = 10.0
_KILL_WAIT = 10.0     # seconds to wait for a SIGKILLed scope to end
# States in which nothing of the scope is left running, as `systemctl is-active` prints them.
_GONE_STATES = ("inactive", "failed", "unknown")


def _mem_available_kb() -> int:
    """Return MemAvailable from /proc/meminfo in KiB."""
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1])
    raise HarnessError(EXIT_LAUNCH, "cannot read MemAvailable", "is /proc mounted?")


def caps(runtime_sec: int = 1800, cpu_cores: int = 8, tasks: int = 512,
         mem_kb: int | None = None) -> list[str]:
    """Return systemd scope properties capping memory, swap, CPU, task count and runtime."""
    if mem_kb is None:
        mem_kb = max(_MEM_FLOOR_KB, int(_mem_available_kb() * _MEM_SHARE))
    return [f"MemoryMax={mem_kb}K", "MemorySwapMax=0", f"CPUQuota={cpu_cores * 100}%",
            f"TasksMax={tasks}", f"RuntimeMaxSec={runtime_sec}"]


def _wait_active(unit: str, proc: subprocess.Popen, log_path: Path) -> None:
    """Block (bounded) until systemd reports the scope active; raise if systemd-run exits first.

    WHY: systemd-run registers the scope over D-Bus after Popen returns, so a caller that checks
    is_active() straight away can see a live session as dead (found in Task 7's engine test).
    """
    deadline = time.monotonic() + _START_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise HarnessError(EXIT_LAUNCH, f"systemd-run exited with status {proc.returncode}",
                               f"see {log_path}")
        if is_active(unit):
            return
        time.sleep(0.05)
    raise HarnessError(EXIT_LAUNCH, f"scope {unit} did not become active in {_START_TIMEOUT:.0f}s",
                       "is systemd --user running? `systemctl --user status`")


def start(unit: str, argv: list[str], env: dict, log_path: Path,
          props: list[str]) -> subprocess.Popen:
    """Start `argv` inside a new named scope, detached; return the Popen of `argv` itself.

    Returns only once the scope is active. systemd-run --scope runs the command in its own
    process, so the Popen's pid is the command (checked 2026-10-12: comm "sleep", cgroup
    fso-harness-<id>.scope). A caller that outlives the scope can wait() on it to reap it;
    `fsoctl launch` exits while its runner keeps going, so it simply drops it.
    """
    if shutil.which(argv[0], path=env.get("PATH")) is None:
        raise HarnessError(EXIT_LAUNCH, f"not an executable: {argv[0]}", "check the binary path")
    cmd = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--unit", unit]
    for p in props:
        cmd += ["-p", p]
    log = open(log_path, "ab")  # noqa: SIM115 - handed to the child, closed by the OS on exit
    try:
        proc = subprocess.Popen(cmd + argv, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as e:
        raise HarnessError(EXIT_LAUNCH, f"systemd-run failed: {e}",
                           "is systemd --user running?") from e
    finally:
        log.close()
    _wait_active(unit, proc, log_path)
    return proc


def is_active(unit: str) -> bool:
    """Return True while the scope exists and is running."""
    r = subprocess.run(["systemctl", "--user", "is-active", "--quiet", f"{unit}.scope"],
                       timeout=10, check=False)
    return r.returncode == 0


def active_units() -> list[str]:
    """Return every harness session scope systemd has that is not yet gone, e.g.
    ["fso-harness-s1a2b3c4d.scope"]: active, activating or deactivating, from any runtime root.

    Raises EXIT_LAUNCH if systemctl fails, so callers never read "none running" from an error.
    """
    r = subprocess.run(["systemctl", "--user", "list-units", "fso-harness-s*",
                        "--state=active,activating,deactivating", "--plain", "--no-legend"],
                       capture_output=True, text=True, timeout=10, check=False)
    if r.returncode != 0:
        raise HarnessError(EXIT_LAUNCH, f"cannot list harness scopes: {r.stderr.strip()}",
                           "is systemd --user running? `systemctl --user status`")
    return [line.split()[0] for line in r.stdout.splitlines() if line.strip()]


def state(unit: str) -> str:
    """Return systemd's state word for the scope: "active", "deactivating", "inactive", ...

    `systemctl is-active` prints the state even when it exits non-zero; a unit systemd has never
    heard of (or has collected) prints "inactive" (checked 2026-10-12). Returns "" when systemctl
    printed nothing (e.g. the user manager is unreachable), which no caller treats as gone.
    """
    r = subprocess.run(["systemctl", "--user", "is-active", f"{unit}.scope"],
                       capture_output=True, text=True, timeout=10, check=False)
    return r.stdout.strip()


def is_gone(unit: str) -> bool:
    """Return True once the scope has fully ended, so nothing in it can still use its files.

    WHY not `not is_active()`: a scope whose stop is still running reports "deactivating" (and
    is-active exits non-zero); its engine and Xvfb may still be alive (QA SL-2).
    """
    return state(unit) in _GONE_STATES


def _kill(unit: str) -> None:
    """SIGKILL every process in the scope, then wait (bounded) for systemd to finish it.

    Raises EXIT_ENGINE if the scope is still not gone after _KILL_WAIT seconds.
    """
    # --kill-whom defaults to "all" (systemctl(1)). The return code is ignored on purpose: it is
    # non-zero when the unit vanished between our check and the kill, which is the outcome we want;
    # the state check below decides success either way.
    _ = subprocess.run(["systemctl", "--user", "kill", "--signal=SIGKILL", f"{unit}.scope"],
                       capture_output=True, timeout=10, check=False)
    deadline = time.monotonic() + _KILL_WAIT
    while time.monotonic() < deadline:
        if is_gone(unit):
            return
        time.sleep(0.1)
    raise HarnessError(EXIT_ENGINE, f"scope {unit} is still running after SIGKILL",
                       f"check `systemctl --user status {unit}.scope`; a process stuck in the "
                       "kernel (D state) cannot be killed until its I/O finishes")


def stop(unit: str, timeout: float = 20) -> None:
    """Stop the scope (kills everything in it); succeeds if it is already gone.

    Polite stop first (SIGTERM, which SDL turns into a quit event); if that does not finish within
    `timeout` seconds, or systemctl reports failure, escalate to SIGKILL. WHY escalate ourselves:
    the user manager waits DefaultTimeoutStopUSec (45 s on this machine, checked 2026-10-12)
    before its own SIGKILL, and a hung engine never processes the quit event (QA SL-4 / CR-1).
    Raises EXIT_ENGINE only if the scope survives SIGKILL; never lets TimeoutExpired escape.
    """
    if is_gone(unit):
        return
    try:
        r = subprocess.run(["systemctl", "--user", "stop", f"{unit}.scope"], capture_output=True,
                           timeout=timeout, check=False)
        stopped = r.returncode == 0
    except subprocess.TimeoutExpired:
        stopped = False
    if stopped and is_gone(unit):
        return
    _kill(unit)
