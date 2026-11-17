"""Probe 0 (cross-platform Plan A, Task 4): which unit tests already pass on this OS.

Runs the default unit tier with a JUnit XML report and writes census-<os>.json next to it:
per test file, how many tests passed, failed, errored and were skipped, plus the first line of
each failure. It never fails itself (exit 0 whatever the tests do): on Windows and macOS the
census is a measurement, not a gate. Run from the repository root:

    python probes/xplat/census.py [out_dir]
"""
import json
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

_TIMEOUT = 1800   # seconds for the whole unit tier; it takes ~30 s on Linux


def _os_name() -> str:
    """Return "linux", "windows" or "macos" for the file name."""
    return {"Linux": "linux", "Windows": "windows", "Darwin": "macos"}.get(platform.system(),
                                                                          platform.system())


def summarize(junit: Path) -> dict:
    """Read a JUnit XML report and return {file: {passed, failed, errored, skipped, first}}.

    WHY the stdlib parser and not defusedxml: the report is written by our own pytest run a
    moment earlier in this process, never by an outside party, so entity-expansion attacks are
    out of scope; defusedxml would be the probe's only dependency.
    """
    files: dict = {}
    for case in ET.parse(junit).getroot().iter("testcase"):
        # The test FILE (module part of classname), so tests inside a class still count under
        # their file; a collection error has no classname and counts under "?" (plan A QA TA-13).
        parts = (case.get("classname") or "").split(".")
        name = next((x for x in parts if x.startswith("test_")), "?")
        row = files.setdefault(name, {"passed": 0, "failed": 0, "errored": 0, "skipped": 0,
                                      "first": {}})
        bad = case.find("failure") if case.find("failure") is not None else case.find("error")
        if case.find("skipped") is not None:
            row["skipped"] += 1
        elif bad is not None:
            row["failed" if bad.tag == "failure" else "errored"] += 1
            text = (bad.get("message") or bad.text or "").strip().splitlines()
            row["first"][case.get("name")] = text[0][:300] if text else ""
        else:
            row["passed"] += 1
    return files


def main() -> int:
    """Run the unit tier, write census-<os>.json, always exit 0."""
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    junit = out / f"junit-{_os_name()}.xml"
    junit.unlink(missing_ok=True)     # a report from an earlier run must not pass as this one's
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", f"--junitxml={junit}"],
                           capture_output=True, text=True, timeout=_TIMEOUT, check=False)
        tail = (r.stdout or "").strip().splitlines()[-1:]
        # Exit 4/5 (usage error, nothing collected) leaves no report; keep pytest's own words.
        errs = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-25:]
        result = {"os": _os_name(), "python": platform.python_version(), "exit": r.returncode,
                  "summary": tail[0] if tail else "", "output_tail": errs,
                  "files": summarize(junit) if junit.exists() else {}}
    except subprocess.TimeoutExpired:
        # Same keys as a finished run (plan A QA CR-11).
        result = {"os": _os_name(), "python": platform.python_version(), "exit": None,
                  "summary": f"timed out after {_TIMEOUT} s", "output_tail": [], "files": {}}
    (out / f"census-{_os_name()}.json").write_text(json.dumps(result, indent=1))
    print(result["summary"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
