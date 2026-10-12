"""Build an FSO worktree inside a capped scope and return the compiler errors.

WHY a scope: a full FSO build uses every core and several GB; the scope applies the same memory,
CPU and runtime ceilings as engine sessions, and a timeout stops the whole build tree at once.
The worktree must already be configured (cmake -S ... -B ...); configuring is a project decision
(generator, build type, Vulkan on/off) this tool does not guess.
"""
import argparse
import contextlib
import fcntl
import hashlib
import re
import subprocess
from pathlib import Path

from . import paths, scope
from .cli import register
from .errors import EXIT_FAILED, EXIT_REFUSED, EXIT_TIMEOUT, EXIT_USAGE, HarnessError

# GCC/Clang diagnostics: file:line:col: error|warning|fatal error: message
_DIAG = re.compile(r"^(.+?):(\d+):(\d+): (fatal error|error|warning): (.*)$")
# Failures that have no file:line:col, each checked against real logs (tests/data/build_*.txt):
# the linker (GNU ld via collect2), a CMake re-run during the build, and the build tool giving up.
_TOOL_ERRORS = (
    ("linker", re.compile(r"undefined reference to|\bld returned \d+ exit status")),
    ("cmake", re.compile(r"^CMake Error\b")),
    ("make", re.compile(r"^g?make(\[\d+\])?: \*\*\* ")),
    ("ninja", re.compile(r"^FAILED: |^ninja: (build stopped|error):")),
    # The GCC driver's "%s signal terminated program %s" with SIGKILL: the OOM killer's mark.
    ("oom", re.compile(r"Killed signal terminated program")),
)
_CMAKE_CONT_MAX = 4   # indented explanation lines joined onto one "CMake Error at" line
_TAIL_LINES = 40
DEFAULT_JOBS = 14  # leaves a desktop usable while compiling; pass --jobs for the machine at hand


def _diag(line: str) -> dict | None:
    """Return one compiler diagnostic parsed from a line, or None."""
    m = _DIAG.match(line)
    if not m:
        return None
    return {"file": m.group(1), "line": int(m.group(2)), "col": int(m.group(3)),
            "kind": "warning" if m.group(4) == "warning" else "error",
            "message": m.group(5), "source": "compiler"}


def _tool_error(lines: list[str], i: int) -> dict | None:
    """Return a linker / CMake / make / ninja / OOM error starting at lines[i], or None.

    A "CMake Error at" line is followed by its explanation on indented lines; those are joined
    on (bounded), because the header alone ("CMake Error at CMakeLists.txt:3") says nothing.
    """
    for source, pat in _TOOL_ERRORS:
        if pat.search(lines[i]):
            text = lines[i].strip()
            if source == "cmake":
                follow = [x.strip() for x in lines[i + 1:i + 1 + 2 * _CMAKE_CONT_MAX]
                          if x.startswith(" ") and x.strip()]
                text = " ".join([text, *follow[:_CMAKE_CONT_MAX]])
            return {"file": None, "line": None, "col": None, "kind": "error",
                    "message": text, "source": source}
    return None


def scan(text: str) -> tuple[list[dict], list[dict]]:
    """Return (errors, warnings): every diagnostic in the log, split by kind.

    Errors keep log order, except that make/ninja "giving up" lines go last: Ninja prints
    "FAILED: <target>" before the compiler or linker output that explains it, and that summary
    line must not take the first slot an agent reads.
    """
    lines = text.splitlines()
    causes, tool, warnings = [], [], []
    for i, line in enumerate(lines):
        d = _diag(line) or _tool_error(lines, i)
        if d is None:
            continue
        if d["kind"] == "warning":
            warnings.append(d)
        else:
            (tool if d["source"] in ("make", "ninja") else causes).append(d)
    return causes + tool, warnings


def compiler_errors(text: str, limit: int = 50, warning_limit: int = 20) -> list[dict]:
    """Return errors first (up to `limit`), then warnings (up to `warning_limit`).

    Each item is {"file", "line", "col", "kind", "message", "source"}; file/line/col are None for
    linker, CMake and build-tool lines. WHY separate caps with errors first: a build with hundreds
    of warnings once pushed the one real error out of a shared cap (QA SF-11). Warnings are still
    listed because FSO's CI builds with warnings as errors.
    """
    errors, warnings = scan(text)
    return errors[:limit] + warnings[:warning_limit]


def _unit_for(worktree: Path) -> str:
    """Return a stable scope name for builds of one worktree (one build at a time per tree)."""
    return "fso-harness-build-" + hashlib.sha256(str(worktree).encode()).hexdigest()[:8]


@contextlib.contextmanager
def _build_lock(bdir: Path, wt: Path):
    """Hold <build dir>/.fsoctl-build.lock for the whole build; refuse if another holds it.

    WHY a lock and not only the scope check: two builds started together both saw no active
    scope, then the second truncated the first's log and failed on "unit exists" (QA SL-12).
    The lock is released when the file closes, including when fsoctl is killed.
    """
    with open(bdir / ".fsoctl-build.lock", "a") as f:   # "a": never truncates the lock file
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise HarnessError(EXIT_REFUSED, f"a build of {wt} is already running",
                               "wait for it to finish; one build per tree at a time") from e
        yield


def _result(returncode: int, log: Path) -> dict:
    """Return the build result; failures add the last log lines and name a likely OOM kill."""
    text = log.read_text(errors="replace")
    errors, warnings = scan(text)
    out = {"ok": returncode == 0, "returncode": returncode,
           "errors": compiler_errors(text), "error_count": len(errors),
           "warning_count": len(warnings), "log": str(log)}
    if returncode == 0:
        return out
    # The CLI turns "code" into fsoctl's exit status (contract C11), so `fsoctl build X && ...`
    # stops on a failed compile instead of carrying on with an old binary (cross-family X-3).
    out["code"] = EXIT_FAILED
    out["log_tail"] = text.splitlines()[-_TAIL_LINES:]
    # -9: cmake itself got SIGKILL; 137 = 128 + 9: a shell-run child did; the GCC line: cc1plus did.
    out["oom_suspected"] = returncode in (-9, 137) or any(e["source"] == "oom" for e in errors)
    if out["oom_suspected"]:
        out["note"] = ("a build process was killed (SIGKILL), most likely out of memory: the "
                       "build scope caps memory; retry with fewer --jobs")
    return out


def build(worktree: str, build_dir: str = "build", targets: list[str] | None = None,
          jobs: int = DEFAULT_JOBS, timeout: float = 3600) -> dict:
    """Run cmake --build in a scope; return {"ok", "returncode", "errors", "log", ...}.

    On failure also "code" (EXIT_FAILED), "log_tail" (last 40 lines) and "oom_suspected"
    (+ "note" when true).
    """
    wt = Path(worktree).resolve()
    bdir = wt / build_dir
    if not (bdir / "CMakeCache.txt").exists():
        raise HarnessError(EXIT_USAGE, f"{bdir} is not a configured build folder",
                           f"configure first: cmake -S {wt} -B {bdir}")
    unit = _unit_for(wt)
    with _build_lock(bdir, wt):
        if scope.is_active(unit):   # e.g. a build started by an fsoctl without the lock
            raise HarnessError(EXIT_REFUSED, f"a build of {wt} is already running",
                               f"wait, or stop it: systemctl --user stop {unit}.scope")
        log = paths.runtime_root() / "builds" / f"{unit}.log"
        log.parent.mkdir(exist_ok=True)
        cmd = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--unit", unit]
        for prop in scope.caps(runtime_sec=int(timeout) + 60, cpu_cores=jobs):
            cmd += ["-p", prop]
        cmd += ["cmake", "--build", str(bdir), "-j", str(jobs)]
        if targets:
            cmd += ["--target", *targets]
        with open(log, "wb") as out:
            try:
                proc = subprocess.run(cmd, cwd=wt, stdin=subprocess.DEVNULL, stdout=out,
                                      stderr=subprocess.STDOUT, timeout=timeout, check=False)
            except subprocess.TimeoutExpired as e:
                scope.stop(unit)
                raise HarnessError(EXIT_TIMEOUT, f"build did not finish in {timeout:.0f}s",
                                   f"see {log}; raise --timeout for a full build") from e
            except BaseException:
                # SIGTERM (SystemExit from fsoctl's handler, C8) or Ctrl-C: subprocess.run kills
                # cmake, but the compilers it started live on in the scope until RuntimeMaxSec.
                # Stopping the scope ends them all (ruling R3).
                scope.stop(unit)
                raise
        # Read the log while the lock is still held: once it is released another build of this
        # tree may truncate the same log file (cross-family Y-9).
        return _result(proc.returncode, log)


def _cfg_build(p: argparse.ArgumentParser) -> None:
    """Add the `build` verb's arguments."""
    p.add_argument("worktree", help="FSO source tree with a configured build folder")
    p.add_argument("--build-dir", default="build", help="relative to the worktree")
    p.add_argument("--target", action="append", dest="targets", help="repeatable")
    p.add_argument("--jobs", type=int, default=DEFAULT_JOBS)
    p.add_argument("--timeout", type=float, default=3600)


@register("build", "build a configured FSO worktree in a capped scope", _cfg_build)
def _cli_build(args: argparse.Namespace) -> dict:
    """CLI wrapper for build()."""
    return build(args.worktree, args.build_dir, args.targets, args.jobs, args.timeout)
