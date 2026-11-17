"""Probe 11: can a session record be read while another process rewrites it?

The harness writes session.json as "temp file, then rename over the old one" and reads it
with no lock. On Linux a rename swaps the file under a reader. This probe counts what
happens on the OS it runs on: one process rewrites a record `--writes` times while two
others read it as fast as they can (one with session.load, one with session.list_sessions),
and every error on either side is counted by kind. Prints one JSON line.

    python probes/xplat/record_race.py [--writes N]

Uses a runtime root of its own under the temp folder; nothing of the user's is touched.
"""
import argparse
import collections
import json
import multiprocessing
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))    # the repository root


def _rewrite(sid: str, times: int, out) -> None:
    """Rewrite the record `times` times; report {error kind: count} and the tries made."""
    from fsoctl import session  # noqa: PLC0415 - after the runtime root is in the environment
    errors: collections.Counter = collections.Counter()
    for _ in range(times):
        try:
            session.update(sid, lambda rec: None)
        except Exception as e:  # every kind: counting them is the probe
            errors[type(e).__name__] += 1
    out.put(("writer", dict(errors), times))


def _read(sid: str, done, out, whole_list: bool) -> None:
    """Read the record until `done`; report {error kind: count} and the reads made."""
    from fsoctl import session  # noqa: PLC0415
    errors: collections.Counter = collections.Counter()
    reads = 0
    while not done.is_set():    # bounded by the writer finishing (the parent sets `done`)
        reads += 1
        try:
            if whole_list:
                session.list_sessions()
            else:
                session.load(sid)
        except Exception as e:  # as above
            errors[type(e).__name__] += 1
    out.put(("list reader" if whole_list else "load reader", dict(errors), reads))


def main() -> int:
    """Run the race once and print what each side saw."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--writes", type=int, default=3000)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="fso-harness-probe11-") as root:
        os.environ["FSO_HARNESS_RUNTIME"] = root
        from fsoctl import session  # noqa: PLC0415
        s = session.new_session(kind="game", binary="x", fs2path="x", mods=[], mission=None,
                                renderer="opengl", width=1, height=1, switches={},
                                allow_eval=False, extra_args=[])
        mp = multiprocessing.get_context("spawn")
        out, done = mp.Queue(), mp.Event()
        readers = [mp.Process(target=_read, args=(s.id, done, out, whole))
                   for whole in (False, True)]
        writer = mp.Process(target=_rewrite, args=(s.id, args.writes, out))
        seen = {}
        try:
            for p in (*readers, writer):
                p.start()
            writer.join(600)
            unfinished = writer.is_alive()       # then it will never report: do not wait
            done.set()
            for _ in range(2 if unfinished else 3):
                who, errors, count = out.get(timeout=60)
                seen[who] = {"calls": count, "errors": errors}
            if unfinished:
                seen["writer"] = {"calls": None, "errors": {"not finished in 600 s": 1}}
            for p in readers:
                p.join(30)
        finally:                                 # no child outlives the folder it works in
            done.set()
            for p in (*readers, writer):
                if p.is_alive():
                    p.terminate()
                    p.join(10)
    print(json.dumps({"os": sys.platform, "writes": args.writes, **seen}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
