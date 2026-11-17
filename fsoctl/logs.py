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
import codecs
import collections
import os
import re
from collections.abc import Iterator
from pathlib import Path

from . import session
from .cli import register
from .errors import EXIT_NOT_FOUND, EXIT_USAGE, HarnessError
from .platform import safefs

_TAIL_MAX = 5000
# Bytes of a log scanned at most. Below it, problems are counted over the whole log, as before
# (QA CR2-2: a 32 MiB tail read had made problems_total a tail count). Above it only the last
# _READ_MAX bytes are scanned and the result says so (truncated_to_last_bytes). The scan streams
# _CHUNK bytes at a time and keeps only the tail lines and the first PROBLEMS_MAX problems, so
# memory stays small whatever the size. Reads go through safefs (no links, no FIFOs; QA SA-2).
_READ_MAX = 1 << 30
_CHUNK = 8 << 20
_LINE_MAX = 4 << 20             # characters held for one unfinished line at most
# Where each log is: the engine's stdout log in the run folder (the runner writes it), the debug
# log in the prefs folder (the engine writes it). sources() gives the same files as paths.
REL = {"engine": "engine.log", "fso": "data/fs2_open.log"}
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


def base(sess: session.Session, source: str) -> Path:
    """Return the folder safefs walks from for `source` ("engine": the run folder; "fso": the
    prefs folder, which is on disk for a sandbox session)."""
    return session.run_dir(sess) if source == "engine" else session.prefs_dir(sess)


def exists(sess: session.Session, source: str) -> bool:
    """True if the session's `source` log ("engine" | "fso") is a regular file (not a link)."""
    return safefs.is_file(base(sess, source), REL[source])


def read(sid: str, source: str = "auto", tail: int = 200, grep: str | None = None) -> dict:
    """Return filtered log lines plus the problems found in the chosen log: the whole log, or its
    last _READ_MAX bytes when it is larger (then truncated_to_last_bytes is set).

    source: "engine" (stdout/stderr), "fso" (debug-build log) or "auto" (fso if it exists).
    problems holds the first PROBLEMS_MAX; problems_total counts all of them.
    """
    sess = session.load(sid)
    if source == "auto":
        source = "fso" if exists(sess, "fso") else "engine"
    if source not in REL or not exists(sess, source):
        raise HarnessError(EXIT_NOT_FOUND, f"no {source} log yet",
                           "release builds only have the engine log; debug builds add fs2_open.log")
    pat = _compile_grep(grep)
    try:
        with safefs.open_read(base(sess, source), REL[source]) as f:
            size = os.fstat(f.fileno()).st_size
            skip = max(0, size - _READ_MAX)
            f.seek(skip)
            lines = _lines(f, size - skip)
            if skip:
                next(lines, None)        # the first line was cut by the seek
            kept, found, total = scan(lines, min(max(tail, 1), _TAIL_MAX), pat)
    except OSError as e:
        raise HarnessError(EXIT_NOT_FOUND, f"the {source} log cannot be read: {e}",
                           "it is no longer a plain file") from None
    out = {"source": source, "lines": kept, "problems": found, "problems_total": total}
    if skip:
        out["truncated_to_last_bytes"] = _READ_MAX
    return out


def _compile_grep(grep: str | None) -> re.Pattern | None:
    """Compile the optional line filter or raise EXIT_USAGE."""
    if not grep:
        return None
    try:
        return re.compile(grep)
    except re.error as e:
        raise HarnessError(EXIT_USAGE, f"bad regex: {e}", "use Python regex syntax") from e


def _lines(f, limit: int) -> Iterator[str]:
    """Yield the lines of binary file `f` from its position, at most `limit` bytes, decoded with
    errors="replace" and split exactly as str.splitlines() splits the whole text (a line break
    split across two chunks, such as CR then LF, is held back until the next chunk)."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    carry, left = "", limit
    while left > 0:                  # bounded: each pass consumes up to _CHUNK of `limit` bytes
        chunk = f.read(min(_CHUNK, left))
        if not chunk:
            break
        left -= len(chunk)
        pieces = (carry + decoder.decode(chunk)).splitlines(keepends=True)
        carry = ""
        # The last piece is unfinished when it has no line break yet, or ends in CR (an LF
        # may follow in the next chunk, and CR LF is one break): keep it for the next pass.
        if pieces and (pieces[-1].splitlines()[0] == pieces[-1] or pieces[-1].endswith("\r")):
            carry = pieces.pop()
        for piece in pieces:
            yield piece.splitlines()[0]  # the piece without its line break
        if len(carry) > _LINE_MAX:   # a log with no line breaks: memory stays bounded; such
            yield carry              # a "line" is split here, the only place output differs
            carry = ""
    tail = carry + decoder.decode(b"", final=True)
    yield from tail.splitlines()


def scan(lines, keep: int, pat: re.Pattern | None) -> tuple[list[str], list[dict], int]:
    """One pass over `lines`: return (the last `keep` lines matching `pat`, or all lines when
    pat is None; the first PROBLEMS_MAX problems; the number of problems). Gives exactly what
    problems() and filter_lines() give on the same list, without holding the list: a warning's
    continuation lines are joined as they arrive (at most _WARNING_CONT_MAX open at once)."""
    kept: collections.deque = collections.deque(maxlen=keep)
    found: list[dict] = []
    total, open_warnings = 0, []
    for i, line in enumerate(lines, 1):
        if pat is None or pat.search(line):
            kept.append(line)
        still_open = []
        for w in open_warnings:
            w["parts"].append(line.strip())
            if '" at ' not in line.strip() and len(w["parts"]) <= _WARNING_CONT_MAX:
                still_open.append(w)
        open_warnings = still_open
        for kind, kind_pat in _PATTERNS:
            if kind_pat.match(line):
                total += 1
                entry = {"kind": kind, "line": i, "text": line.strip()}
                if kind == "warning":
                    w = {"parts": [line.strip()]}
                    if '" at ' not in line.strip():
                        open_warnings.append(w)
                    entry["_w"] = w
                if len(found) < PROBLEMS_MAX:
                    found.append(entry)
                break
    for e in found:
        w = e.pop("_w", None)
        if w is not None:
            e["text"] = " ".join(w["parts"])
    return list(kept), found, total


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
