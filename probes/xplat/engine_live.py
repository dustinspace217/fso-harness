"""Probe 9: the engine with game data, as a Windows session would run it.

Probes 1 and 5 ran the engine with no data, so it stopped inside graphics start-up. With data
it goes on to its first screen and opens its network port, which is what a harness session
needs. Measured here (on whatever desktop this script is started on; use hidden_run.py for a
hidden one):

    gl_init                 the log shows graphics start-up finished, and with which renderer
    udp_ports               UDP ports the engine process holds (the real-network default: one
                            game port, the one asked for with -port)
    capture                 PrintWindow of the engine's window: size, distinct colours, a .bmp
    idle_frames             how many different pictures several captures taken while nothing
                            is sent show (more than one: the engine is drawing new frames)
    foreground              whether the engine's window is the foreground window of its
                            desktop, and whether this process can make it so
    posted_key              posted key presses ("A", Return on the pilot screen) reached the
                            engine: a pilot file was made or the game moved on; and whether
                            Windows accepted each post

--behind opens another window after the engine so the engine's window is NOT the foreground one on
the normal desktop: the cell that separates "hidden desktop" from "not in front" (QA CR-3).

The engine gets a preferences folder of its own (FSO_PREFERENCES_PATH), so no pilot, setting or
log of the machine's user is read or written. Needs a FastDebug build: release builds write no
log. Exit 0 whatever is found.

    python engine_live.py --out DIR --label NAME --fso EXE --game-dir DIR [--port N]
                          [--pause-when-unfocused] [--behind]
"""
import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import fred2_live as fl                # win32(), capture(), top_windows(): shared with probe 7

GL_INIT_LINE = "OpenGL init is complete"
INIT_WAIT_S = 120
SCREEN_WAIT_S = 45
SETTLE_S = 3
KEY_GAP_S = 1.0
KEY_WAIT_S = 20
IDLE_CAPTURES = 8
IDLE_GAP_S = 0.3
# A plain window of this probe's own that closes itself after a minute (Notepad on Windows 11
# hands over to another process, which could not be ended afterwards).
FRONT_WINDOW = "import tkinter as t; r = t.Tk(); r.after(60000, r.destroy); r.mainloop()"
WM_KEYDOWN, WM_KEYUP, VK_A, VK_RETURN = 0x0100, 0x0101, 0x41, 0x0D
A_DOWN, A_UP = 0x001E0001, 0xC01E0001             # repeat 1, scan code 0x1E (the A key)
RETURN_DOWN, RETURN_UP = 0x001C0001, 0xC01C0001   # scan code 0x1C


def log_text(prefs: Path) -> str:
    """The engine's log so far ("" until it exists)."""
    try:
        return (prefs / "data" / "fs2_open.log").read_text(errors="replace")
    except OSError:
        return ""


def lines_with(text: str, *needles: str) -> list:
    """The stripped lines of text that contain any needle."""
    return [ln.strip() for ln in text.splitlines() if any(n in ln for n in needles)]


def udp_ports(pid: int) -> list:
    """UDP ports process pid holds, as "address:port" strings."""
    import psutil  # noqa: PLC0415 - probe requirement, not a harness dependency
    conns = psutil.Process(pid).net_connections(kind="udp")
    return sorted({f"{c.laddr.ip}:{c.laddr.port}" for c in conns if c.laddr})


def engine_argv(fso: Path, port: int, pause_when_unfocused: bool) -> list:
    """The command line: windowed, silent, its own game port. The harness always passes
    -no_unfocused_pause ("Don't pause if the window isn't focused", cmdline.cpp;
    launcher.build_argv), and a window on a hidden desktop was never the focused one when
    measured; pause_when_unfocused leaves the switch out to compare."""
    argv = [str(fso), "-window", "-nosound", "-nomusic", "-port", str(port)]
    return argv if pause_when_unfocused else [*argv, "-no_unfocused_pause"]


def foreground_check(hwnd: int, leave_behind: bool) -> dict:
    """Is hwnd the foreground window of this desktop, and (unless leave_behind) can this
    process make it so? Measured beside key_check, to see whether posted keys reaching the
    engine goes with its window being in front."""
    user32 = ctypes.WinDLL("user32")
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    result = {"before": user32.GetForegroundWindow() == hwnd, "set_call_ok": None}
    if not leave_behind:
        result["set_call_ok"] = bool(user32.SetForegroundWindow(hwnd))
        time.sleep(KEY_GAP_S)
    result["at_key_time"] = user32.GetForegroundWindow() == hwnd
    return result


def idle_frames(w, hwnd: int, shot: Path, notes: list) -> dict:
    """Capture the window IDLE_CAPTURES times, IDLE_GAP_S apart, sending nothing; return how
    many different pictures came back. The pilot screen has a blinking text cursor, so a
    running frame loop shows at least two; exactly one means no new frame was seen."""
    pictures, taken = set(), 0
    for _ in range(IDLE_CAPTURES):                # bounded
        if fl.attempt(notes, "idle capture", lambda: fl.capture(w, hwnd, shot)):
            pictures.add(shot.read_bytes())
            taken += 1
        time.sleep(IDLE_GAP_S)
    # captures = how many succeeded: failed captures must not read as "no new frames"
    return {"captures": taken, "different_pictures": len(pictures)}


def key_check(w, hwnd: int, prefs: Path) -> dict:
    """Post "A" then Return to the engine's window. The first screen on a fresh preferences
    folder asks for a pilot callsign, so those keys create a pilot: a file under data/players
    and a game event in the log. Key down and up only: a posted WM_CHAR as well typed the
    letter twice (seen in a capture). posts_accepted says whether Windows took every post, so
    "the engine ignored the keys" is not confused with "the keys were never queued"."""
    events_before = len(lines_with(log_text(prefs), "Got event"))
    players = prefs / "data" / "players"
    # files already there are not this check's doing (QA SC-5)
    pilots_before = {p.name for p in players.iterdir()} if players.is_dir() else set()
    accepted = []
    for vk, down, up in ((VK_A, A_DOWN, A_UP), (VK_RETURN, RETURN_DOWN, RETURN_UP)):
        accepted.append(bool(w.u.PostMessageW(hwnd, WM_KEYDOWN, vk, down)))
        accepted.append(bool(w.u.PostMessageW(hwnd, WM_KEYUP, vk, up)))
        time.sleep(KEY_GAP_S)
    def new_pilots():
        """Pilot files that were not there before the keys were posted."""
        now = {p.name for p in players.iterdir()} if players.is_dir() else set()
        return sorted(now - pilots_before)

    fl.wait_value(new_pilots, KEY_WAIT_S)
    pilot_files = new_pilots()
    new_events = lines_with(log_text(prefs), "Got event")[events_before:]
    return {"posts_accepted": all(accepted), "pilot_files": pilot_files,
            "new_game_events": new_events[:6],
            "reached_engine": bool(pilot_files or new_events)}


def retry_in_front(w, hwnd: int, prefs: Path, front) -> dict:
    """The control for "the keys did not arrive" (QA SC-1): on the SAME engine, close the
    window that was in front, bring the engine's window forward and post the same keys again.
    If they arrive now, the engine was on the pilot screen and reading keys all along, and the
    earlier failure goes with not being in front rather than with not being ready."""
    front.kill()
    front.wait(timeout=10)
    time.sleep(SETTLE_S)
    return {"foreground": foreground_check(hwnd, leave_behind=False),
            "posted_key": key_check(w, hwnd, prefs)}


def measure(proc, prefs: Path, out: Path, r: dict, front) -> None:
    """Everything measured while the engine runs. front: the process whose window was opened
    in front of the engine's (the "behind" cell), or None."""
    behind = front is not None
    label = r["label"]
    notes = r["notes"]
    r["desktop"] = fl.attempt(notes, "desktop name", fl.desktop_name)
    r["gl_init"] = bool(fl.wait_value(lambda: GL_INIT_LINE in log_text(prefs), INIT_WAIT_S))
    r["renderer"] = lines_with(log_text(prefs), "OpenGL Renderer", "OpenGL Version")
    # the first screen after start-up (pilot selection on a fresh preferences folder)
    fl.wait_value(lambda: lines_with(log_text(prefs), "Got event"), SCREEN_WAIT_S)
    time.sleep(SETTLE_S)
    # None (not an empty list) when the ports could not be read (QA TA-4)
    r["udp_ports"] = fl.attempt(notes, "udp ports", lambda: udp_ports(proc.pid))
    w = fl.win32()
    tops = fl.attempt(notes, "windows", lambda: fl.top_windows(w, proc.pid)) or []
    r["windows"] = [{"class": fl.class_name(w, h), "title": fl.window_text(w, h)} for h in tops]
    if tops:
        hwnd = tops[0]
        shots = [out / f"engine-{label}-{n}.bmp" for n in (1, 2, 3)]
        r["capture"] = fl.attempt(notes, "capture", lambda: fl.capture(w, hwnd, shots[0]))
        r["idle_frames"] = idle_frames(w, hwnd, shots[1], notes)
        r["foreground"] = fl.attempt(notes, "foreground", lambda: foreground_check(hwnd, behind))
        r["posted_key"] = fl.attempt(notes, "posted keys", lambda: key_check(w, hwnd, prefs))
        if behind and r["posted_key"] and not r["posted_key"]["reached_engine"]:
            r["retry_in_front"] = fl.attempt(
                notes, "retry in front", lambda: retry_in_front(w, hwnd, prefs, front))
        time.sleep(SETTLE_S)
        r["capture_after_keys"] = fl.attempt(notes, "capture after keys",
                                             lambda: fl.capture(w, hwnd, shots[2]))
    r["game_events"] = lines_with(log_text(prefs), "Got event")[-6:]
    r["exited_early"] = proc.poll()
    r["log_tail"] = [ln.strip() for ln in log_text(prefs).splitlines()[-8:] if ln.strip()]


def main() -> int:
    """Start the engine, measure, write probe-engine-live-<label>.json; always exit 0."""
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--fso", type=Path, required=True)
    ap.add_argument("--game-dir", type=Path, required=True)
    ap.add_argument("--port", type=int, default=7821)
    ap.add_argument("--pause-when-unfocused", action="store_true")
    ap.add_argument("--behind", action="store_true")
    args = ap.parse_args()
    out = args.out.resolve()
    prefs = out / f"engine-prefs-{args.label}"
    if prefs.exists():
        # a pilot left by an earlier run would skip the pilot screen the key check relies on
        shutil.rmtree(prefs)
    (prefs / "data").mkdir(parents=True)
    r = {"label": args.label, "fso": args.fso.name, "port_asked": args.port,
         "no_unfocused_pause": not args.pause_when_unfocused, "gl_init": False,
         "behind": args.behind, "udp_ports": None, "notes": []}
    env = dict(os.environ, FSO_PREFERENCES_PATH=str(prefs), GALLIUM_DRIVER="llvmpipe")
    proc = subprocess.Popen(engine_argv(args.fso.resolve(), args.port, args.pause_when_unfocused),
                            cwd=args.game_dir.resolve(), env=env)
    front = None
    try:
        if args.behind:
            # a window started after the engine's takes the foreground from it
            fl.wait_value(lambda: GL_INIT_LINE in log_text(prefs), INIT_WAIT_S)
            time.sleep(SETTLE_S)
            front = subprocess.Popen([sys.executable, "-c", FRONT_WINDOW])
            time.sleep(SETTLE_S)
            # a front window that failed to start would make this a second "normal" cell
            r["front_window_running"] = front.poll() is None
        fl.attempt(r["notes"], "measure", lambda: measure(proc, prefs, out, r, front))
    finally:
        proc.kill()                               # the engine never outlives the probe
        proc.wait()
        if front is not None:
            front.kill()
        (out / f"probe-engine-live-{args.label}.json").write_text(json.dumps(r, indent=1))
    print(json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
