"""fsoctl command line: one verb per harness tool, JSON or readable output, stable exit codes.

Verbs live in their own modules and register here with @register so cli.py stays a thin
dispatcher (Power of Ten rule 4: no giant switch).

Exit codes: 0 when the verb did its job, even if the answer is a finding (validate's
aborted:true, warnings in a log); the HarnessError code when it raised; and for a verb that
returns {"ok": false, ...} (a failed build), that dict's "code" or EXIT_FAILED (contract C11).
"""
import argparse
import json
import signal
import subprocess
import sys
import threading
from collections.abc import Callable

from .errors import EXIT_ENGINE, EXIT_FAILED, EXIT_OK, EXIT_USAGE, HarnessError

VERSION = "0.1.0"
VERBS: dict[str, tuple[str, Callable, Callable]] = {}
# Modules that register verbs, imported once by main(). A new verb module goes in this list.
_VERB_MODULES = ("launcher", "xinput", "logs", "game", "record", "build", "scratch", "vp", "fred",
                 "crash", "validate", "frame_stats", "model_info", "docs",
                 "compare")


def register(name: str, help_text: str, configure: Callable[[argparse.ArgumentParser], None]):
    """Decorator: add a verb `name` whose function takes the parsed args and returns a dict."""
    def wrap(fn: Callable[[argparse.Namespace], dict]):
        VERBS[name] = (help_text, configure, fn)
        return fn
    return wrap


@register("version", "print the harness version", lambda p: None)
def _version(args: argparse.Namespace) -> dict:
    """Return the harness version."""
    return {"version": VERSION}


def _load_verb_modules() -> None:
    """Import every module that registers verbs (kept explicit; no plugin discovery)."""
    for name in _VERB_MODULES:
        __import__(f"fsoctl.{name}")


def _render(result: dict) -> str:
    """Render a result dict for humans: one key per line."""
    return "\n".join(f"{k}: {v}" for k, v in result.items() if k != "ok")


class _Parser(argparse.ArgumentParser):
    """ArgumentParser that raises HarnessError(EXIT_USAGE) instead of printing and exiting, so
    usage errors reach the --json output too (QA TA-15). Subparsers inherit this class
    (add_subparsers defaults parser_class to the parent's type)."""

    def error(self, message: str):
        """argparse calls this on any usage error; raise instead of exiting."""
        raise HarnessError(EXIT_USAGE, message, f"see `{self.prog} --help`")


def _on_sigterm(signum: int, frame: object) -> None:
    """SIGTERM handler: exit with 143 by raising SystemExit, so `finally:` blocks run (C8).

    WHY: the MCP server ends a timed-out fsoctl with SIGTERM; Python's default action kills the
    process at once, skipping every verb's `finally: stop(...)` and leaving the engine running.
    Further SIGTERMs are ignored from here on, so a repeated signal cannot cut that cleanup short
    (fix-wave-2 R3); SIGKILL still ends the process.
    """
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise SystemExit(143)


def _as_harness_error(e: Exception) -> HarnessError:
    """Map an unexpected exception from a verb to a structured error (QA CR-9).

    Corrupt state files and OS/tool failures are the harness's environment (EXIT_ENGINE); any
    other ValueError comes from a bad value the caller supplied (EXIT_USAGE).
    """
    text = f"{type(e).__name__}: {e}"
    if isinstance(e, (OSError, json.JSONDecodeError, subprocess.TimeoutExpired)):
        return HarnessError(EXIT_ENGINE, text, "a file or helper tool failed; see the message, "
                            "then `fsoctl status <id>` or `fsoctl crash_report <id>`")
    return HarnessError(EXIT_USAGE, text, "check the arguments and environment variables")


def _run(args: argparse.Namespace) -> tuple[dict, int]:
    """Run the chosen verb; return (result dict, exit code), never raising for verb errors.

    Exit codes (contract C11): a verb that RETURNS a dict with "ok": False reports a failed
    operation (e.g. a build that did not compile); that dict is printed unchanged and the exit
    code is its "code", or EXIT_FAILED if it has none. Shell callers branching on $? would
    otherwise read the failure as success (cross-family review X-3 / XK-2). A result with
    findings in it is NOT a failure: validate's {"ok": true, "aborted": true} or a non-zero
    warning count still exits 0, because the harness did its job and the finding is the answer.
    """
    try:
        result = VERBS[args.verb][2](args)
        if result.get("ok") is False:
            return result, result.get("code", EXIT_FAILED)
        return {"ok": True, **result}, EXIT_OK
    except HarnessError as e:
        return e.to_dict(), e.code
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        err = _as_harness_error(e)
        return err.to_dict(), err.code


def build_parser() -> argparse.ArgumentParser:
    """Return the full fsoctl argument parser (every registered verb).

    Used by main and by the MCP wrapper's test, which checks every tool's command line against
    this exact parser (QA TA-5) instead of a copy of it.
    """
    _load_verb_modules()
    parser = _Parser(prog="fsoctl")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="verb", required=True)
    for name, (help_text, configure, _fn) in sorted(VERBS.items()):
        configure(sub.add_parser(name, help=help_text))
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse argv, run one verb, print its result, and return the exit code."""
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:          # --help / --version style exits
        return EXIT_USAGE if e.code else EXIT_OK
    except HarnessError as e:
        if "--json" in argv:
            print(json.dumps(e.to_dict()))
        else:
            print(f"{parser.format_usage()}fsoctl: error: {e.message}", file=sys.stderr)
        return e.code
    # signal.signal works only in the main thread; an fsoctl run as a command always is there.
    in_main = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGTERM, _on_sigterm) if in_main else None
    try:
        result, code = _run(args)
    finally:
        if in_main:
            signal.signal(signal.SIGTERM, previous)
    print(json.dumps(result) if args.json else _render(result))
    return code


if __name__ == "__main__":
    sys.exit(main())
