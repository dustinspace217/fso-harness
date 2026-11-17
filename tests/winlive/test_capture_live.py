"""win_capture against a real window on a hidden desktop (plan B Task 6, Step 2)."""
import contextlib
import sys
import time
import types

from fsoctl.errors import HarnessError
from fsoctl.platform import win_capture, win_scope

COLOUR = (0x33, 0x66, 0xCC)
# A borderless-content window filled with one colour, up for a minute.
WINDOW = ("import tkinter as t\n"
          "r = t.Tk()\n"
          "r.geometry('320x240')\n"
          "r.configure(bg='#3366cc')\n"
          "r.after(60000, r.destroy)\n"
          "r.mainloop()\n")


def test_the_captured_centre_of_a_hidden_window_is_the_colour_it_was_filled_with(
        tmp_path, job, child_env):
    started = win_scope.start(job, [sys.executable, "-c", WINDOW], child_env,
                              tmp_path / "w.log", [f"Desktop={job}"])
    sess = types.SimpleNamespace(engine_pid=started.pid, desktop=job, scope=job)
    image = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:            # bounded: the window takes a moment to draw
        with contextlib.suppress(OSError, HarnessError):     # not up yet: ask again
            image = win_capture.window_pixels(sess)
            if _pixel(image, image[0] // 2, image[1] // 2) == COLOUR:
                break
        time.sleep(0.3)
    assert image is not None, (tmp_path / "w.log").read_text(errors="replace")
    width, height, rgb = image
    assert (width, height) == (320, 240)          # the client area: no title bar, no frame
    assert len(rgb) == width * height * 3
    assert _pixel(image, width // 2, height // 2) == COLOUR
    assert _pixel(image, 5, 5) == COLOUR and _pixel(image, width - 5, height - 5) == COLOUR
    png = win_capture.png_bytes(*image)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) < len(rgb)


def _pixel(image, x, y):
    """The (r, g, b) of one pixel."""
    width, _, rgb = image
    at = (y * width + x) * 3
    return tuple(rgb[at:at + 3])
