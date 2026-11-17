"""Probe 12: does a session work when its run folder's name has a space, an apostrophe and
letters outside ASCII? (cross-platform Plan B, Review Focus 3)

The default run folder on Windows is under %LOCALAPPDATA%, which is under the user's name, so
a user called José has such a folder without asking for one. The engine is told where its
preferences folder is through an environment variable, and what it makes of letters outside
ASCII there can only be seen by running it. This probe makes a runtime root named
"José Müller's run" under the temp folder and runs a session through its whole life there:
launch, state, screenshot, stop, cleanup. It prints one JSON line with what each step said.

    python probes/xplat/accent_root.py --binary <engine> [--fs2path <game folder>]
                                       [--name <folder name>]

`--name` tries another folder name (letters only, a space only, ...), to find which kind of
character a failure belongs to. A name given as \\uXXXX escapes is decoded, so it can be
typed on a command line that mangles letters outside ASCII.

Nothing outside that temp folder is written (the game folder is only read).
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
NAME = "José Müller's run"


def fsoctl(env: dict, *args: str, timeout: float = 400) -> dict:
    """Run one fsoctl verb with --json; return its reply (or what went wrong running it)."""
    try:
        r = subprocess.run([sys.executable, "-m", "fsoctl", "--json", *args], cwd=REPO, env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": f"fsoctl {args[0]} did not return in {timeout:.0f} s"}
    for line in reversed(r.stdout.splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                break
    return {"ok": False, "message": "no JSON reply", "stdout": r.stdout[-400:],
            "stderr": r.stderr[-400:]}


def left_behind(run: Path) -> dict:
    """What a failed launch left in its run folder: every file with its size, and the end of
    the runner's and the engine's own logs."""
    found: dict = {"files": {}}
    for path in sorted(run.rglob("*"))[:60]:                    # bounded: a run folder is small
        if path.is_file():
            found["files"][str(path.relative_to(run))] = path.stat().st_size
    for log in ("runner.log", "engine.log", "prefs/data/fs2_open.log"):
        path = run / log
        if path.is_file():
            found[log] = path.read_text(encoding="utf-8", errors="replace")[-600:]
    return found


def main() -> int:
    """Run the session's life in the oddly named root and print what happened."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--fs2path", default=os.environ.get("FS2PATH"))
    parser.add_argument("--name", default=NAME)
    args = parser.parse_args()
    name = args.name.encode("ascii", "backslashreplace").decode("unicode_escape")
    out: dict = {"os": sys.platform, "root_name": name}
    with tempfile.TemporaryDirectory(prefix="fso-harness-probe12-") as base:
        root = Path(base) / name
        root.mkdir()
        env = {**os.environ, "FSO_HARNESS_RUNTIME": str(root),
               "FSO_HARNESS_STATE": str(Path(base) / "state"), "PYTHONPATH": str(REPO)}
        if args.fs2path:
            env["FS2PATH"] = args.fs2path
        launched = fsoctl(env, "launch", "--binary", args.binary, "--runtime", "180")
        out["launch"] = {k: launched.get(k) for k in ("ok", "id", "renderer", "state",
                                                      "message", "hint") if k in launched}
        sid = launched.get("id") or (launched.get("details") or {}).get("id")
        if not sid:                    # a failed launch still made a run folder: find it
            made = sorted(d.name for d in root.iterdir() if d.is_dir() and d.name.startswith("s"))
            sid = made[-1] if made else None
        if sid and not launched.get("ok"):
            out["left_behind"] = left_behind(root / sid)
        if sid:
            run = root / sid
            state = fsoctl(env, "state", sid, timeout=120)
            out["state"] = {k: state.get(k) for k in ("ok", "state", "message") if k in state}
            shot = fsoctl(env, "screenshot", sid, timeout=120)
            picture = Path(shot["path"]) if shot.get("path") else None
            out["screenshot"] = {
                "ok": shot.get("ok"), "message": shot.get("message"),
                "bytes": picture.stat().st_size if picture and picture.is_file() else None,
                "inside_the_root": bool(picture and str(picture).startswith(str(root)))}
            # what the engine itself wrote under the oddly named folder
            prefs = run / "prefs"
            out["engine_wrote"] = {
                "engine_log_bytes": (run / "engine.log").stat().st_size
                if (run / "engine.log").is_file() else None,
                "prefs_entries": sorted(p.name for p in prefs.iterdir())[:12]
                if prefs.is_dir() else None}
            stopped = fsoctl(env, "stop", sid, timeout=120)
            out["stop"] = {k: stopped.get(k) for k in ("ok", "status", "exit_info",
                                                       "runner_first", "message")
                           if k in stopped}
            cleaned = fsoctl(env, "cleanup", "--older-than", "0", timeout=120)
            out["cleanup"] = {k: cleaned.get(k) for k in ("ok", "removed", "failed", "message")
                              if k in cleaned}
            out["folder_left"] = run.exists()
    print(json.dumps(out, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
