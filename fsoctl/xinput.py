"""Screenshots and keyboard/mouse input on a session's virtual display.

WHY XTEST (plain `xdotool key` / `click`) and never `xdotool key --window`: SDL ignores synthetic
XSendEvent input; XTEST events look like real hardware. Coordinates are relative to the session's
main window: the pointer is placed with `mousemove --window <id>` (a pointer warp, not a synthetic
event), because windows are not at 0,0 (qtFRED's editor appeared at 512,258 with no window manager,
2026-10-12). Prefer bridge commands over keys where one exists (an earlier lab test showed F-keys
sent via XTEST never reached SDL).

The main window is found by process id (xwin.main_window): for qtFRED that skips the splash screen
and dialogs and picks the window titled with the mission file name.
"""
import argparse
import contextlib
import os
import re
import subprocess
import time
from pathlib import Path

from . import launcher, record, session, xwin
from .cli import register
from .errors import EXIT_NOT_FOUND, EXIT_REFUSED, EXIT_USAGE, HarnessError

_CROP = re.compile(r"^(\d+)x(\d+)\+(\d+)\+(\d+)$")
_KEY = re.compile(r"^[A-Za-z0-9_+]+$")
_TIMEOUT = 15
_SCROLL_MAX = 50  # wheel clicks per call; bounds the loop
_SHOT_NAME_TRIES = 1000
HOLD_MS_MAX = 10_000   # one key hold; longer would outlast the MCP call that asked for it


def parse_crop(text: str) -> tuple[int, int, int, int]:
    """Parse "WxH+X+Y" into (w, h, x, y); reject anything else with EXIT_USAGE."""
    m = _CROP.match(text or "")
    if not m or int(m.group(1)) == 0 or int(m.group(2)) == 0:
        raise HarnessError(EXIT_USAGE, f"bad crop {text!r}", "use WxH+X+Y, e.g. 400x300+10+20")
    w, h, x, y = (int(g) for g in m.groups())
    return w, h, x, y


def check_scale(value: float) -> float:
    """Return a scale factor in [0.05, 1]; reject others with EXIT_USAGE."""
    if not 0.05 <= value <= 1.0:
        raise HarnessError(EXIT_USAGE, f"bad scale {value}", "use a value between 0.05 and 1.0")
    return value


def check_keys(keys: list[str]) -> list[str]:
    """Return key names after checking they are plain xdotool key names (no shell syntax)."""
    for k in keys:
        if not _KEY.match(k):
            raise HarnessError(EXIT_USAGE, f"bad key name {k!r}",
                               "use xdotool names like Return, alt+r, KP_4")
    return keys


def _x(sess: session.Session, *args: str) -> str:
    """Run an X tool against the session display (never the user's) and return its stdout."""
    env = {**os.environ, "DISPLAY": sess.display or ""}
    env.pop("WAYLAND_DISPLAY", None)
    r = subprocess.run(list(args), env=env, capture_output=True, text=True, timeout=_TIMEOUT,
                       check=False)
    if r.returncode != 0:
        raise HarnessError(EXIT_NOT_FOUND, f"{args[0]} failed: {r.stderr.strip()[:200]}",
                           "is the session still running? `fsoctl status <id>`")
    return r.stdout


def _release(sess: session.Session, *args: str) -> None:
    """Best-effort key/button release on an error path (keyup, mouseup).

    Called only while another exception is already on its way out, so a failing release must not
    replace it: that first error is the one the caller needs. Errors here are dropped on purpose.
    """
    # The display may be gone; then nothing more can be released.
    with contextlib.suppress(HarnessError, subprocess.SubprocessError, OSError):
        _x(sess, *args)


def window_id(sess: session.Session) -> str:
    """Return the X window id of the session's game window or qtFRED editor window."""
    wid = xwin.main_window(sess.display or "", sess.engine_pid or 0, sess.kind, sess.mission)
    if not wid:
        raise HarnessError(EXIT_NOT_FOUND, "no main window for the session's process",
                           "the engine may still be starting; check `fsoctl status <id>`")
    return wid


def _move(sess: session.Session, x: int, y: int) -> None:
    """Place the pointer at (x, y) relative to the session's main window."""
    _x(sess, "xdotool", "mousemove", "--window", window_id(sess), str(x), str(y))


def new_shot_path(shots: Path, prefix: str) -> Path:
    """Claim and return the next free <shots>/<prefix>-NNNN.png (the file is created empty).

    WHY claim with O_EXCL: two callers that both counted the existing files and picked the same
    number would overwrite each other's image (QA CR-7). Creating the file exclusively means only
    one caller gets each name; the other moves on to the next number. Numbers stay in order, so
    a folder listing still reads as a timeline.
    """
    start = len(list(shots.glob(f"{prefix}-*.png"))) + 1
    for n in range(start, start + _SHOT_NAME_TRIES):   # bounded; collisions are rare
        path = shots / f"{prefix}-{n:04d}.png"
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            continue
        return path
    raise HarnessError(EXIT_REFUSED, f"no free {prefix} file name in {shots}",
                       "clean up old screenshots with `fsoctl cleanup`")


def screenshot(sid: str, crop: str | None = None, scale: float | None = None) -> dict:
    """Capture the session window to <run>/shots/shot-NNNN.png; return path, width and height."""
    sess = launcher.require_running(sid)
    shots = session.run_dir(sess) / "shots"
    shots.mkdir(exist_ok=True)
    args = ["import", "-window", window_id(sess)]
    if crop:
        w, h, x, y = parse_crop(crop)
        args += ["-crop", f"{w}x{h}+{x}+{y}", "+repage"]
    if scale:
        args += ["-resize", f"{check_scale(scale) * 100:.0f}%"]
    path = new_shot_path(shots, "shot")   # claimed last, so a bad argument leaves no empty file
    try:
        _x(sess, *args, str(path))
    except HarnessError:
        path.unlink(missing_ok=True)   # drop the empty placeholder new_shot_path created
        raise
    size = _x(sess, "identify", "-format", "%w %h", str(path)).split()
    return {"path": str(path), "width": int(size[0]), "height": int(size[1])}


def _focus(sess: session.Session) -> None:
    """Give the session window keyboard focus (works without a window manager)."""
    _x(sess, "xdotool", "windowfocus", window_id(sess))


def keys(sid: str, names: list[str], hold_ms: int = 0) -> dict:
    """Press keys via XTEST, each optionally held for hold_ms milliseconds."""
    if not 0 <= hold_ms <= HOLD_MS_MAX:
        raise HarnessError(EXIT_USAGE, f"bad hold_ms {hold_ms}",
                           f"use 0 (a tap) to {HOLD_MS_MAX} milliseconds")
    sess = launcher.require_running(sid)
    check_keys(names)
    _focus(sess)
    for k in names:
        if hold_ms:
            _x(sess, "xdotool", "keydown", k)
            try:   # a key left down would keep acting in the session (cross-family X-6/XK-5)
                time.sleep(hold_ms / 1000)
            except BaseException:
                _release(sess, "xdotool", "keyup", k)
                raise
            _x(sess, "xdotool", "keyup", k)
        else:
            _x(sess, "xdotool", "key", k)
    record.append(sess, "keys", {"keys": names, "hold_ms": hold_ms})
    return {"pressed": names}


def click(sid: str, x: int, y: int, button: int = 1, double: bool = False) -> dict:
    """Move to (x, y) and click; coordinates are window coordinates (window at 0,0)."""
    sess = launcher.require_running(sid)
    extra = ["--repeat", "2"] if double else []
    _move(sess, x, y)
    _x(sess, "xdotool", "click", *extra, str(button))
    record.append(sess, "click", {"x": x, "y": y, "button": button, "double": double})
    return {"clicked": [x, y]}


def drag(sid: str, start: tuple[int, int], end: tuple[int, int], button: int = 1) -> dict:
    """Press at start, move through the midpoint to end, release (marquee/orbit drags).

    start and end are (x, y) window coordinates.
    """
    sess = launcher.require_running(sid)
    (x1, y1), (x2, y2) = start, end
    mid = ((x1 + x2) // 2, (y1 + y2) // 2)
    _move(sess, x1, y1)
    _x(sess, "xdotool", "mousedown", str(button))
    try:   # a button left down turns every later click into a drag (cross-family XK-5)
        for px, py in (mid, (x2, y2)):
            time.sleep(0.3)
            _move(sess, px, py)
        time.sleep(0.3)
    except BaseException:
        _release(sess, "xdotool", "mouseup", str(button))
        raise
    _x(sess, "xdotool", "mouseup", str(button))
    record.append(sess, "drag", {"from": [x1, y1], "to": [x2, y2], "button": button})
    return {"dragged": [[x1, y1], [x2, y2]]}


def focus_neutral(sess: session.Session, x: int, y: int) -> None:
    """Click a neutral spot (window coordinates) so a closed modal no longer swallows input."""
    _move(sess, x, y)
    _x(sess, "xdotool", "click", "1")


def scroll(sid: str, x: int, y: int, clicks: int) -> dict:
    """Scroll the wheel at (x, y): positive clicks = up (button 4), negative = down (button 5)."""
    if abs(clicks) > _SCROLL_MAX:   # refused, not clamped: the result must match what was sent
        raise HarnessError(EXIT_USAGE, f"scroll of {clicks} clicks is over the {_SCROLL_MAX} cap",
                           f"split it into calls of at most {_SCROLL_MAX}")
    sess = launcher.require_running(sid)
    button = "4" if clicks > 0 else "5"
    _move(sess, x, y)
    for _ in range(abs(clicks)):
        _x(sess, "xdotool", "click", button)
    record.append(sess, "scroll", {"x": x, "y": y, "clicks": clicks})
    return {"scrolled": clicks}


# ---- command line ----------------------------------------------------------------------------

def _cfg_shot(p: argparse.ArgumentParser) -> None:
    """Add the `shot` verb's arguments."""
    p.add_argument("id")
    p.add_argument("--crop", help="WxH+X+Y")
    p.add_argument("--scale", type=float, help="0.05-1.0")


@register("screenshot", "save a PNG of the session window", _cfg_shot)
def _cli_screenshot(args: argparse.Namespace) -> dict:
    """CLI wrapper for screenshot()."""
    return screenshot(args.id, args.crop, args.scale)


def _cfg_keys(p: argparse.ArgumentParser) -> None:
    """Add the `keys` verb's arguments."""
    p.add_argument("id")
    p.add_argument("keys", nargs="+", help="xdotool key names, e.g. Return alt+r KP_4")
    p.add_argument("--hold-ms", type=int, default=0, help=f"0-{HOLD_MS_MAX}")


@register("keys", "press keys on the session window (XTEST)", _cfg_keys)
def _cli_keys(args: argparse.Namespace) -> dict:
    """CLI wrapper for keys()."""
    return keys(args.id, args.keys, args.hold_ms)


def _cfg_click(p: argparse.ArgumentParser) -> None:
    """Add the `click` verb's arguments."""
    p.add_argument("id")
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p.add_argument("--button", type=int, default=1)
    p.add_argument("--double", action="store_true")


@register("click", "click at window coordinates", _cfg_click)
def _cli_click(args: argparse.Namespace) -> dict:
    """CLI wrapper for click()."""
    return click(args.id, args.x, args.y, args.button, args.double)


def _cfg_drag(p: argparse.ArgumentParser) -> None:
    """Add the `drag` verb's arguments."""
    p.add_argument("id")
    for name in ("x1", "y1", "x2", "y2"):
        p.add_argument(name, type=int)
    p.add_argument("--button", type=int, default=1)


@register("drag", "press, move and release the mouse", _cfg_drag)
def _cli_drag(args: argparse.Namespace) -> dict:
    """CLI wrapper for drag()."""
    return drag(args.id, (args.x1, args.y1), (args.x2, args.y2), args.button)


def _cfg_scroll(p: argparse.ArgumentParser) -> None:
    """Add the `scroll` verb's arguments."""
    p.add_argument("id")
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p.add_argument("clicks", type=int, help="positive = up, negative = down")


@register("scroll", "turn the mouse wheel at a point", _cfg_scroll)
def _cli_scroll(args: argparse.Namespace) -> dict:
    """CLI wrapper for scroll()."""
    return scroll(args.id, args.x, args.y, args.clicks)
