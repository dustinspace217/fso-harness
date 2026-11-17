"""Build an FSO worktree inside a capped scope and return the compiler errors.

WHY a scope: a full FSO build uses every core and several GB; the scope applies the same memory,
CPU and runtime ceilings as engine sessions, and a timeout stops the whole build tree at once.
The worktree must already be configured (cmake -S ... -B ...); configuring is a project decision
(generator, build type, Vulkan on/off) this tool does not guess.
"""
import argparse
import contextlib
import hashlib
import itertools
import re
import subprocess
from pathlib import Path

from . import paths, platform
from .cli import register
from .errors import EXIT_FAILED, EXIT_REFUSED, EXIT_TIMEOUT, EXIT_USAGE, HarnessError
from .platform import programs, scope

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
# Microsoft's tools. Seen in real output (tests/data/build_msvc_*.txt, from `cmake --build`
# with the Visual Studio generator; MSBuild 17.14, MSVC 19.44): compiler errors and warnings
# as `file(line,col): error CODE: text [project]`, the linker's three position-less forms,
# MSBuild's own `MSBUILD : error MSB1009: ...`, and the compiler driver's `cl : command line
# warning D9002: ...` (a warning: not reported, as GNU ld's warnings are not). Accepted too,
# from the tools' documented formats and NOT seen here: a diagnostic with no column or with
# a four-number range, a leading `N>`, and the driver's line as an error.
# A diagnostic with a place: file(line[,col]) : error|warning CODE: message [project].
_DIAG_MSVC = re.compile(r"^\s*(?:\d+>)?(.+?)\((\d+)(?:,(\d+))?(?:,\d+,\d+)?\) ?: "
                        r"(fatal error|error|warning) ([A-Z]+\d+) ?: (.*)$")
# An error with no place, in MSBuild's own form "origin : [words] error CODE: text": the
# linker (LNK...), MSBuild itself (MSB...), the compiler driver (D..., C...). The space before
# the first colon keeps GCC's "collect2: error: ..." out.
_MS_TOOL = re.compile(r"^\s*(?:\d+>)?\S.*? : (?:[A-Za-z ]+ )?error ([A-Z]+)\d+ ?: ")
_MS_SOURCES = {"LNK": "linker", "MSB": "msbuild"}        # any other code: the compiler
# MSBuild ends each line it relays with the project it came from; the reader has the file.
_MS_PROJECT = re.compile(r" \[[^\[\]]*proj\]$")
_CACHE_LINES_MAX = 20000   # lines of a CMakeCache.txt read to find CMAKE_COMMAND
_CMAKE_CONT_MAX = 4   # indented explanation lines joined onto one "CMake Error at" line
_TAIL_LINES = 40
DEFAULT_JOBS = 14  # leaves a desktop usable while compiling; pass --jobs for the machine at hand


def _diag(line: str) -> dict | None:
    """Return one compiler diagnostic parsed from a line, or None.

    GCC/Clang lines give file, line and column. MSVC lines give the same from
    `file(line,col)`; "col" is None when MSVC printed no column, and the message starts
    with MSVC's code (C2065: ...), without the project name MSBuild appends.
    """
    m = _DIAG.match(line)
    if m:
        return {"file": m.group(1), "line": int(m.group(2)), "col": int(m.group(3)),
                "kind": "warning" if m.group(4) == "warning" else "error",
                "message": m.group(5), "source": "compiler"}
    m = _DIAG_MSVC.match(line)
    if not m:
        return None
    return {"file": m.group(1), "line": int(m.group(2)),
            "col": int(m.group(3)) if m.group(3) else None,
            "kind": "warning" if m.group(4) == "warning" else "error",
            "message": f"{m.group(5)}: {_MS_PROJECT.sub('', m.group(6))}", "source": "compiler"}


def _tool_error(lines: list[str], i: int) -> dict | None:
    """Return a linker / CMake / make / ninja / OOM error starting at lines[i], or None; and
    a Microsoft tool's error with no file position (source "linker", "msbuild" or "compiler").

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
    m = _MS_TOOL.match(lines[i])
    if m:
        return {"file": None, "line": None, "col": None, "kind": "error",
                "message": _MS_PROJECT.sub("", lines[i].strip()),
                "source": _MS_SOURCES.get(m.group(1), "compiler")}
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
    # "a": never truncates the lock file
    with open(bdir / ".fsoctl-build.lock", "a", encoding="utf-8") as f:
        if not platform.locks.lock_exclusive(f):
            raise HarnessError(EXIT_REFUSED, f"a build of {wt} is already running",
                               "wait for it to finish; one build per tree at a time")
        yield


def _cmake(bdir: Path) -> str:
    """Return the cmake to run: the one on PATH, by its plain name as before; else the one
    that configured this build folder, which its CMakeCache.txt records as CMAKE_COMMAND.

    WHY the second place: Visual Studio's Build Tools bring a cmake of their own and do not
    put it on PATH (seen in the VM), so a tree configured from a developer prompt could not
    be built from anywhere else. Running the recorded cmake trusts nothing new: building
    the folder already runs whatever its build rules say. EXIT_USAGE when neither exists.
    """
    found = programs.find("cmake")
    if found:
        # Windows: by the full path, so nothing is looked for in the current folder when it
        # is started (programs.py). Elsewhere the plain name, as before.
        return found if platform.NAME == "windows" else "cmake"
    with open(bdir / "CMakeCache.txt", errors="replace", encoding="utf-8") as cache:
        for line in itertools.islice(cache, _CACHE_LINES_MAX):      # bounded read
            if line.startswith("CMAKE_COMMAND:"):
                recorded = Path(line.split("=", 1)[1].strip())
                if recorded.is_file():
                    return str(recorded)
                break
    raise HarnessError(EXIT_USAGE, "cmake is not on PATH, and the build folder records no "
                       "cmake that still exists", "add cmake's folder to PATH (Visual Studio "
                       "keeps its own under Common7\\IDE\\CommonExtensions\\Microsoft\\CMake)")


def _argv(bdir: Path, jobs: int, config: str | None, targets: list[str] | None) -> list[str]:
    """Return the cmake command that builds `bdir`."""
    argv = [_cmake(bdir), "--build", str(bdir), "-j", str(jobs)]
    if config:
        argv += ["--config", config]
    if targets:
        argv += ["--target", *targets]
    return argv


def _result(returncode: int, log: Path) -> dict:
    """Return the build result; failures add the last log lines and name a likely OOM kill.
    Never on Windows: nothing there is ended by SIGKILL, and a job's memory limit makes
    allocations fail instead (what the compiler then prints is not measured)."""
    text = log.read_text(errors="replace", encoding="utf-8")
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
    out["oom_suspected"] = platform.NAME != "windows" and (
        returncode in (-9, 137) or any(e["source"] == "oom" for e in errors))
    if out["oom_suspected"]:
        out["note"] = ("a build process was killed (SIGKILL), most likely out of memory: the "
                       "build scope caps memory; retry with fewer --jobs")
    return out


# One parameter per option of the verb. `config` is the sixth; the argument-count rule is
# off for this one line instead of hiding two options in a dict (the bypass ledger,
# 2026-11-16).
def build(worktree: str, build_dir: str = "build", targets: list[str] | None = None,  # noqa: PLR0913
          jobs: int = DEFAULT_JOBS, timeout: float = 3600, *,
          config: str | None = None) -> dict:
    """Run cmake --build in a scope; return {"ok", "returncode", "errors", "log", ...}.

    On failure also "code" (EXIT_FAILED), "log_tail" (last 40 lines) and "oom_suspected"
    (+ "note" when true).

    config: the configuration to build where one build folder holds several (Visual
    Studio's generators: Debug, Release, FastDebug); passed to cmake as --config. A
    single-configuration folder (Ninja, Makefiles) needs none and cmake ignores it there.

    On Linux the build runs in a systemd scope; on Windows in a job (platform scope.run)
    that ends with this command. Both carry the same limits and stop on the same timeout.
    """
    wt = Path(worktree).resolve()
    bdir = wt / build_dir
    if not (bdir / "CMakeCache.txt").exists():
        raise HarnessError(EXIT_USAGE, f"{bdir} is not a configured build folder",
                           f"configure first: cmake -S {wt} -B {bdir}")
    unit = _unit_for(wt)
    with _build_lock(bdir, wt):
        if scope.is_active(unit):   # e.g. a build started by an fsoctl without the lock
            how = ("wait; ending the `fsoctl build` that runs it ends its compilers"
                   if platform.NAME == "windows"
                   else f"wait, or stop it: systemctl --user stop {unit}.scope")
            raise HarnessError(EXIT_REFUSED, f"a build of {wt} is already running", how)
        log = paths.runtime_root() / "builds" / f"{unit}.log"
        log.parent.mkdir(exist_ok=True)
        argv = _argv(bdir, jobs, config, targets)
        props = scope.caps(runtime_sec=int(timeout) + 60, cpu_cores=jobs)
        with open(log, "wb") as out:
            try:
                if platform.NAME == "windows":
                    returncode = scope.run(unit, argv, out, props, timeout)
                else:
                    cmd = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                           "--unit", unit]
                    for prop in props:
                        cmd += ["-p", prop]
                    returncode = subprocess.run(
                        cmd + argv, cwd=wt, stdin=subprocess.DEVNULL, stdout=out,
                        stderr=subprocess.STDOUT, timeout=timeout, check=False).returncode
            except subprocess.TimeoutExpired as e:
                scope.stop(unit)
                raise HarnessError(EXIT_TIMEOUT, f"build did not finish in {timeout:.0f}s",
                                   f"see {log}; raise --timeout for a full build") from e
            except BaseException:
                # SIGTERM (SystemExit from fsoctl's handler, C8) or Ctrl-C: subprocess.run kills
                # cmake, but the compilers it started live on in the scope until RuntimeMaxSec.
                # Stopping the scope ends them all (ruling R3). On Windows the job has ended
                # by the time this runs (scope.run closes it) and the stop finds nothing.
                scope.stop(unit)
                raise
        # Read the log while the lock is still held: once it is released another build of this
        # tree may truncate the same log file (cross-family Y-9).
        return _result(returncode, log)


def _cfg_build(p: argparse.ArgumentParser) -> None:
    """Add the `build` verb's arguments."""
    p.add_argument("worktree", help="FSO source tree with a configured build folder")
    p.add_argument("--build-dir", default="build", help="relative to the worktree")
    p.add_argument("--target", action="append", dest="targets", help="repeatable")
    p.add_argument("--jobs", type=int, default=DEFAULT_JOBS)
    p.add_argument("--timeout", type=float, default=3600)
    p.add_argument("--config", help="configuration to build in a build folder that holds "
                   "several (Visual Studio: Debug, Release, FastDebug)")


@register("build", "build a configured FSO worktree in a capped scope", _cfg_build)
def _cli_build(args: argparse.Namespace) -> dict:
    """CLI wrapper for build()."""
    return build(args.worktree, args.build_dir, args.targets, args.jobs, args.timeout,
                 config=args.config)
