"""Engine logs: tail, filter, and pull out the lines an agent must not miss.

WHY two sources: release builds write no fs2_open.log (logging is compiled out), so the engine's
stdout/stderr (engine.log, written by the runner) is the only text; debug builds also write the
full log under the session prefs.

WHY strict patterns: the debug log is full of look-alikes ("WARNING! A file being indexed...",
"Error reading in PXO login data") that are informational prints, not Warning()/Error() calls.
Only the formats osapi/dialogs.cpp produces count as problems (checked against real debug runs):
  WARNING: "<text>" at <file>:<line>      Warning(); <text> may span lines (table warnings do)
  Error: <text>                           Error()
  Assert: "<expression>"                  Assert failure
  LUA ERROR: <text>  /  Lua Error: <text> Lua error in a script
"""
import argparse
import re

from . import session
from .cli import register
from .errors import EXIT_NOT_FOUND, EXIT_USAGE, HarnessError

_TAIL_MAX = 5000
# A per-frame warner can produce thousands of identical problems; the first ones name the cause
# and the rest only bloat the reply an agent has to read. problems_total keeps the true count.
PROBLEMS_MAX = 50
_PATTERNS = [
    ("lua", re.compile(r"^\s*(LUA ERROR|Lua Error): ")),
    ("assert", re.compile(r'^\s*Assert: "')),
    ("int3", re.compile(r"^\s*Int3\(\)")),
    ("warning", re.compile(r'^\s*WARNING: "')),
    ("error", re.compile(r"^\s*Error: ")),
]


_WARNING_CONT_MAX = 5   # continuation lines joined onto one multi-line warning


def _warning_text(lines: list[str], start: int) -> str:
    """Join a warning's lines until the one ending its quoted text (bounded)."""
    parts = [lines[start].strip()]
    j = start
    while '" at ' not in parts[-1] and j + 1 < len(lines) and len(parts) <= _WARNING_CONT_MAX:
        j += 1
        parts.append(lines[j].strip())
    return " ".join(parts)


def problems(lines: list[str]) -> list[dict]:
    """Return {"kind", "line" (1-based), "text"} for every warning/error/assert/lua/int3."""
    out = []
    for i, line in enumerate(lines, 1):
        for kind, pat in _PATTERNS:
            if pat.match(line):
                text = _warning_text(lines, i - 1) if kind == "warning" else line.strip()
                out.append({"kind": kind, "line": i, "text": text})
                break
    return out


def filter_lines(lines: list[str], tail: int, grep: str | None) -> list[str]:
    """Apply an optional regex filter, then keep the last `tail` lines (capped at 5000)."""
    if grep:
        try:
            pat = re.compile(grep)
        except re.error as e:
            raise HarnessError(EXIT_USAGE, f"bad regex: {e}", "use Python regex syntax") from e
        lines = [x for x in lines if pat.search(x)]
    return lines[-min(max(tail, 1), _TAIL_MAX):]


def sources(sess: session.Session) -> dict:
    """Return the engine stdout log and the FSO debug log paths for a session."""
    return {"engine": session.run_dir(sess) / "engine.log",
            "fso": session.prefs_dir(sess) / "data" / "fs2_open.log"}


def read(sid: str, source: str = "auto", tail: int = 200, grep: str | None = None) -> dict:
    """Return filtered log lines plus the problems found in the whole chosen log.

    source: "engine" (stdout/stderr), "fso" (debug-build log) or "auto" (fso if it exists).
    problems holds the first PROBLEMS_MAX; problems_total counts all of them.
    """
    sess = session.load(sid)
    srcs = sources(sess)
    if source == "auto":
        source = "fso" if srcs["fso"].exists() else "engine"
    path = srcs.get(source)
    if path is None or not path.exists():
        raise HarnessError(EXIT_NOT_FOUND, f"no {source} log yet",
                           "release builds only have the engine log; debug builds add fs2_open.log")
    lines = path.read_text(errors="replace").splitlines()
    found = problems(lines)
    return {"source": source, "lines": filter_lines(lines, tail, grep),
            "problems": found[:PROBLEMS_MAX], "problems_total": len(found)}


def _cfg_log(p: argparse.ArgumentParser) -> None:
    """Add the `log` verb's arguments."""
    p.add_argument("id")
    p.add_argument("--source", choices=("auto", "engine", "fso"), default="auto")
    p.add_argument("--tail", type=int, default=200)
    p.add_argument("--grep", help="Python regex; only matching lines are returned")


@register("log", "tail a session's log and list warnings/errors/asserts", _cfg_log)
def _cli_log(args: argparse.Namespace) -> dict:
    """CLI wrapper for read()."""
    return read(args.id, args.source, args.tail, args.grep)
