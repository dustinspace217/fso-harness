"""A session window's pixels on Windows, and a PNG of them, with nothing but Python.

On Linux a screenshot is ImageMagick's `import` reading the session's X display. Windows has
no such tool installed, and Plan B adds no package on any OS, so this module does the three
jobs itself: ask Windows to draw the engine's window into memory, crop and scale the pixels,
and write a PNG.

The capture is PrintWindow with PW_RENDERFULLCONTENT, which includes what the graphics card
(or Mesa) drew; measured on a hidden desktop with the engine's own window on 2026-11-16
(docs/research/2026-11-16-xplat-vm-results.md, probe 9). Only the client area is taken (no
title bar or borders), so the picture is the game's own resolution, as on Linux, and pixel
positions in it are the positions inside the game.

WHY a hand-written PNG: the format needs only zlib and a checksum, both in the standard
library, for the one kind of image made here (8-bit RGB, no interlace). A few lines replace a
dependency every Windows user would have to install.
"""
import ctypes
import struct
import zlib

from ..errors import EXIT_NOT_FOUND, EXIT_USAGE, HarnessError
from . import win_api, win_desktop, win_procs

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# The largest picture taken: an 8K screen. The engine sizes its window, and the buffers
# for a capture are sized by the window (four bytes a pixel, then three more).
MAX_PIXELS = 7680 * 4320

Image = tuple[int, int, bytes]       # width, height, RGB bytes (3 per pixel, top row first)


def _chunk(kind: bytes, data: bytes) -> bytes:
    """One PNG chunk: length, type, data, CRC of type and data."""
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png_bytes(width: int, height: int, rgb: bytes) -> bytes:
    """Return a PNG file for `rgb` (width * height * 3 bytes, top row first): the signature,
    IHDR (8-bit RGB, no interlace), one IDAT (zlib over the rows, each led by filter byte 0,
    "no filter") and IEND. ValueError if the byte count does not fit the size."""
    stride = width * 3
    if len(rgb) != stride * height:
        raise ValueError(f"{len(rgb)} bytes is not a {width}x{height} RGB image")
    rows = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (PNG_SIGNATURE + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(rows))
            + _chunk(b"IEND", b""))


def crop(image: Image, box: tuple[int, int, int, int]) -> Image:
    """Return the part of `image` inside box (w, h, x, y: xinput.parse_crop's "WxH+X+Y").
    A box that runs past an edge keeps what overlaps, as ImageMagick does on Linux; one that
    misses the image altogether is EXIT_USAGE."""
    width, height, rgb = image
    w, h, x, y = box
    right, bottom = min(x + w, width), min(y + h, height)
    # right and bottom are already cut to the picture, so a box that starts past an edge
    # ends up with no width or no height, like one that was given none
    if right <= x or bottom <= y:
        raise HarnessError(EXIT_USAGE, f"crop {w}x{h}+{x}+{y} is outside the {width}x{height} "
                           "window", "use a crop that overlaps the picture")
    rows = [rgb[(row * width + x) * 3:(row * width + right) * 3] for row in range(y, bottom)]
    return right - x, bottom - y, b"".join(rows)


def scale(image: Image, factor: float) -> Image:
    """Return `image` shrunk by `factor` (0.05 to 1, checked by the caller), taking the
    nearest source pixel for each result pixel: for 0.5, every second pixel of every second
    row. Coarser than ImageMagick's resize on Linux, and enough for looking at a scene."""
    width, height, rgb = image
    if factor >= 1.0:
        return image
    new_w, new_h = max(1, round(width * factor)), max(1, round(height * factor))
    columns = [min(width - 1, int(i / factor)) * 3 for i in range(new_w)]
    out = bytearray()
    for j in range(new_h):                        # bounded by the picture's height
        row = min(height - 1, int(j / factor)) * width * 3
        for column in columns:
            out += rgb[row + column:row + column + 3]
    return new_w, new_h, bytes(out)


def rgb_from_bgra(bgra: bytes) -> bytes:
    """Reorder Windows' 32-bit pixels (blue, green, red, unused) into RGB. Slices, not a
    loop: a 1280x720 picture is 921,600 pixels."""
    rgb = bytearray(len(bgra) // 4 * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    return bytes(rgb)


def capture(hwnd: int) -> Image:
    """Return the client area of window hwnd as an Image. The calling thread must be on the
    window's desktop (window_pixels sees to that). Raises OSError naming the step that
    failed; every handle made here is released on every path."""
    u, g = win_api.user32(), win_api.gdi32()
    rect = win_api.RECT()
    if not u.GetClientRect(hwnd, ctypes.byref(rect)):
        raise OSError(f"GetClientRect error {win_api.last_error()}")
    width, height = rect.right, rect.bottom
    if width <= 0 or height <= 0:
        raise OSError(f"the window has no area ({width}x{height}); is it minimised?")
    if width * height > MAX_PIXELS:               # before any memory is set aside for it
        raise OSError(f"the window is {width}x{height}, larger than the largest picture "
                      f"taken ({MAX_PIXELS} pixels)")
    window_dc = u.GetDC(hwnd)
    if not window_dc:
        raise OSError(f"GetDC error {win_api.last_error()}")
    memory_dc = bitmap = None
    try:
        memory_dc = g.CreateCompatibleDC(window_dc)
        if not memory_dc:
            raise OSError(f"CreateCompatibleDC error {win_api.last_error()}")
        bitmap = g.CreateCompatibleBitmap(window_dc, width, height)
        if not bitmap:
            raise OSError(f"CreateCompatibleBitmap error {win_api.last_error()}")
        previous = g.SelectObject(memory_dc, bitmap)
        flags = win_api.PW_CLIENTONLY | win_api.PW_RENDERFULLCONTENT
        printed = u.PrintWindow(hwnd, memory_dc, flags)
        g.SelectObject(memory_dc, previous)       # GetDIBits wants the bitmap out of any DC
        if not printed:
            raise OSError(f"PrintWindow error {win_api.last_error()}")
        # A negative height asks for the rows top first (a positive one gives them bottom up).
        info = win_api.BITMAPINFOHEADER(biSize=ctypes.sizeof(win_api.BITMAPINFOHEADER),
                                        biWidth=width, biHeight=-height, biPlanes=1,
                                        biBitCount=32)
        pixels = ctypes.create_string_buffer(width * height * 4)
        if g.GetDIBits(memory_dc, bitmap, 0, height, pixels, ctypes.byref(info), 0) != height:
            raise OSError(f"GetDIBits error {win_api.last_error()}")
    finally:
        if bitmap:
            g.DeleteObject(bitmap)
        if memory_dc:
            g.DeleteDC(memory_dc)
        u.ReleaseDC(hwnd, window_dc)
    return width, height, rgb_from_bgra(pixels.raw)


def window_pixels(sess) -> Image:
    """Return the session's engine window as an Image: the first visible top-level window of
    its engine process, found and captured on the session's own desktop (the user's, for a
    --visible session). EXIT_NOT_FOUND, in the words the Linux screenshot uses, when the
    engine has no window (yet)."""
    # Only a process that is in the session's own group: between an engine's end and its
    # record a pid can be another program's, and a --visible session's windows are looked
    # up on the user's own desktop (QA Plan B, SA-15).
    if not win_procs.in_group(sess.engine_pid or 0, sess.scope):
        raise HarnessError(EXIT_NOT_FOUND, "the session's engine process is not running",
                           "check `fsoctl status <id>`")

    def grab() -> Image:
        """Runs on the session's desktop: find the window, capture it."""
        windows = win_desktop.windows_of(sess.engine_pid)
        if not windows:
            raise HarnessError(EXIT_NOT_FOUND, "no main window for the session's process",
                               "the engine may still be starting; check `fsoctl status <id>`")
        return capture(windows[0])
    return win_desktop.on_desktop(sess.desktop, grab)
