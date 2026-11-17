"""win_capture: a session window's pixels and a PNG of them without ImageMagick
(cross-platform Plan B, Task 6). The pixel work (PNG, crop, scale, colour order) is plain
Python and runs everywhere; the Win32 capture itself is faked here and run for real in
tests/winlive/test_capture_live.py."""
import struct
import types
import zlib
from pathlib import Path

import pytest

from fsoctl import launcher, platform, session, xinput
from fsoctl.errors import EXIT_NOT_FOUND, EXIT_REFUSED, EXIT_USAGE, HarnessError
from fsoctl.platform import win_api, win_capture, win_desktop


def chunks(png: bytes) -> list:
    """Split a PNG into (type, data) chunks, checking each chunk's CRC on the way."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    out, at = [], 8
    while at < len(png):
        (length,) = struct.unpack(">I", png[at:at + 4])
        kind, data = png[at + 4:at + 8], png[at + 8:at + 8 + length]
        (crc,) = struct.unpack(">I", png[at + 8 + length:at + 12 + length])
        assert crc == zlib.crc32(kind + data)
        out.append((kind, data))
        at += 12 + length
    return out


def test_png_bytes_writes_a_valid_two_pixel_image():
    png = win_capture.png_bytes(2, 1, b"\xff\x00\x00\x00\xff\x00")
    parts = chunks(png)
    assert [kind for kind, _ in parts] == [b"IHDR", b"IDAT", b"IEND"]
    width, height, depth, colour, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", parts[0][1])
    assert (width, height, depth, colour) == (2, 1, 8, 2)        # 8-bit RGB
    assert (compression, filtering, interlace) == (0, 0, 0)
    assert zlib.decompress(parts[1][1]) == b"\x00\xff\x00\x00\x00\xff\x00"   # filter byte, row
    assert parts[2][1] == b""


def test_png_rows_each_start_with_a_filter_byte():
    rgb = bytes(range(18))                              # 3 x 2 pixels
    raw = zlib.decompress(chunks(win_capture.png_bytes(3, 2, rgb))[1][1])
    assert raw == b"\x00" + rgb[:9] + b"\x00" + rgb[9:]
    with pytest.raises(ValueError):
        win_capture.png_bytes(3, 2, rgb[:-1])           # not width * height * 3 bytes


def grid(width, height):
    """An image whose pixel at (x, y) is the colour (x, y, 0)."""
    return width, height, b"".join(bytes((x, y, 0)) for y in range(height) for x in range(width))


def test_crop_returns_the_middle_four_pixels():
    w, h, rgb = win_capture.crop(grid(4, 4), (2, 2, 1, 1))       # "2x2+1+1"
    assert (w, h) == (2, 2)
    assert rgb == bytes((1, 1, 0, 2, 1, 0, 1, 2, 0, 2, 2, 0))


def test_crop_keeps_what_overlaps_and_refuses_what_does_not():
    w, h, rgb = win_capture.crop(grid(4, 4), (10, 10, 3, 2))     # runs off the right and bottom
    assert (w, h) == (1, 2) and rgb == bytes((3, 2, 0, 3, 3, 0))
    with pytest.raises(HarnessError) as e:
        win_capture.crop(grid(4, 4), (2, 2, 4, 0))
    assert e.value.code == EXIT_USAGE and "outside the 4x4 window" in e.value.message


def test_scale_takes_every_second_pixel_for_one_half():
    w, h, rgb = win_capture.scale(grid(4, 4), 0.5)
    assert (w, h) == (2, 2)
    assert rgb == bytes((0, 0, 0, 2, 0, 0, 0, 2, 0, 2, 2, 0))    # (0,0) (2,0) (0,2) (2,2)
    assert win_capture.scale(grid(4, 4), 1.0) == grid(4, 4)
    assert win_capture.scale(grid(4, 4), 0.05)[:2] == (1, 1)     # never smaller than a pixel


def test_bgra_rows_become_rgb():
    # what GetDIBits returns for a top-down 32-bit bitmap: blue, green, red, unused
    assert win_capture.rgb_from_bgra(b"\x01\x02\x03\xff\x0a\x0b\x0c\x00") == bytes(
        (3, 2, 1, 0x0c, 0x0b, 0x0a))


class FakeGdi:
    """user32 + gdi32 as win_capture uses them; one 2x1 window whose pixels are `bgra`."""

    def __init__(self, bgra=b"\x00\x00\xff\xff\x00\xff\x00\xff", size=(2, 1), fail=()):
        self.bgra, self.size, self.fail = bgra, size, set(fail)
        self.calls, self.print_flags, self.height_asked = [], None, None
        self.seen, self.header = [], None         # every call with its arguments, in order

    def _handle(self, name, value, *arguments):
        self.calls.append(name)
        self.seen.append((name, *arguments))
        return 0 if name in self.fail else value

    def GetClientRect(self, hwnd, rect):  # the Win32 names
        rect._obj.right, rect._obj.bottom = self.size
        return self._handle("GetClientRect", 1, hwnd)

    def GetDC(self, hwnd):
        return self._handle("GetDC", 11, hwnd)

    def ReleaseDC(self, hwnd, dc):
        return self._handle("ReleaseDC", 1, hwnd, dc)

    def PrintWindow(self, hwnd, dc, flags):
        self.print_flags = flags
        return self._handle("PrintWindow", 1, hwnd, dc)

    def CreateCompatibleDC(self, dc):
        return self._handle("CreateCompatibleDC", 12, dc)

    def CreateCompatibleBitmap(self, dc, width, height):
        return self._handle("CreateCompatibleBitmap", 13, dc, width, height)

    def SelectObject(self, dc, obj):
        return self._handle("SelectObject", 14, dc, obj)

    def GetDIBits(self, *arguments):
        dc, bitmap, start, lines, buffer, info, usage = arguments
        head = info._obj
        self.height_asked = head.biHeight
        self.header = (head.biSize, head.biWidth, head.biPlanes, head.biBitCount)
        self.seen.append(("GetDIBits", dc, bitmap, start, lines, usage))
        buffer.raw = self.bgra
        return 0 if "GetDIBits" in self.fail else lines

    def DeleteObject(self, obj):
        return self._handle("DeleteObject", 1, obj)

    def DeleteDC(self, dc):
        return self._handle("DeleteDC", 1, dc)


@pytest.fixture
def gdi(monkeypatch):
    def make(**kw):
        fake = FakeGdi(**kw)
        monkeypatch.setattr(win_api, "user32", lambda: fake)
        monkeypatch.setattr(win_api, "gdi32", lambda: fake)
        monkeypatch.setattr(win_api, "last_error", lambda: 6)
        return fake
    return make


def test_capture_asks_for_the_client_area_with_gpu_content_top_down(gdi):
    g = gdi()
    assert win_capture.capture(77) == (2, 1, b"\xff\x00\x00\x00\xff\x00")    # red, green
    assert g.print_flags == win_api.PW_CLIENTONLY | win_api.PW_RENDERFULLCONTENT
    assert g.height_asked == -1                         # negative: rows come top first
    # everything made is let go, in reverse order of making
    assert g.calls[-3:] == ["DeleteObject", "DeleteDC", "ReleaseDC"]


@pytest.mark.parametrize("broken", ["GetClientRect", "GetDC", "CreateCompatibleDC",
                                    "CreateCompatibleBitmap", "PrintWindow", "GetDIBits"])
def test_a_failed_capture_step_is_an_error_and_leaks_nothing(gdi, broken):
    g = gdi(fail=[broken])
    with pytest.raises(OSError) as e:
        win_capture.capture(77)
    assert broken in str(e.value)
    made = [c for c in g.calls if c in ("GetDC", "CreateCompatibleDC", "CreateCompatibleBitmap")
            and c != broken]
    freed = {"GetDC": "ReleaseDC", "CreateCompatibleDC": "DeleteDC",
             "CreateCompatibleBitmap": "DeleteObject"}
    for name in made:
        assert freed[name] in g.calls, f"{name} was not released after {broken} failed"


def test_an_empty_window_is_not_captured(gdi):
    gdi(size=(0, 0))
    with pytest.raises(OSError) as e:
        win_capture.capture(77)
    assert "has no area" in str(e.value)


# ---- gaps a mutation run found (QA Plan B, MU-11..13): each test names what it pins ----
@pytest.mark.parametrize("size", [(0, 5), (5, 0), (-1, 5), (5, -1)])
def test_a_window_with_no_width_or_no_height_is_not_captured(gdi, size):
    g = gdi(size=size)
    with pytest.raises(OSError, match="has no area"):
        win_capture.capture(77)
    assert "GetDC" not in g.calls                       # nothing was made, nothing to release


def test_a_window_one_pixel_wide_is_captured(gdi):
    gdi(size=(1, 1), bgra=b"\x01\x02\x03\xff")
    assert win_capture.capture(77) == (1, 1, b"\x03\x02\x01")


def test_capture_makes_these_calls_on_these_handles_and_releases_each_one(gdi):
    # The fake cannot tell a right argument from a wrong one; Windows can, and
    # tests/winlive/test_capture_live.py runs this same code there. Written out here so that
    # a changed argument shows on every system, not only on the Windows job.
    g = gdi()
    win_capture.capture(77)
    window_dc, memory_dc, bitmap, previous = 11, 12, 13, 14
    assert g.seen == [
        ("GetClientRect", 77),
        ("GetDC", 77),
        ("CreateCompatibleDC", window_dc),
        ("CreateCompatibleBitmap", window_dc, 2, 1),
        ("SelectObject", memory_dc, bitmap),
        ("PrintWindow", 77, memory_dc),
        ("SelectObject", memory_dc, previous),          # the bitmap is taken out again
        ("GetDIBits", memory_dc, bitmap, 0, 1, 0),      # from row 0, 1 row, plain RGB
        ("DeleteObject", bitmap),
        ("DeleteDC", memory_dc),
        ("ReleaseDC", 77, window_dc),
    ]
    import ctypes  # noqa: PLC0415
    assert g.header == (ctypes.sizeof(win_api.BITMAPINFOHEADER), 2, 1, 32)


def test_a_crop_with_no_width_or_no_height_is_refused():
    for box in ((0, 2, 1, 1), (2, 0, 1, 1)):
        with pytest.raises(HarnessError) as e:
            win_capture.crop(grid(4, 4), box)
        assert e.value.code == EXIT_USAGE
    # the last column and the last row are still inside
    assert win_capture.crop(grid(4, 4), (1, 1, 3, 3)) == (1, 1, bytes((3, 3, 0)))
    for box in ((1, 1, 4, 0), (1, 1, 0, 4)):            # one past the right edge, the bottom
        with pytest.raises(HarnessError):
            win_capture.crop(grid(4, 4), box)


def test_scale_reaches_the_last_column_and_row_and_never_enlarges():
    w, h, rgb = win_capture.scale(grid(3, 3), 0.5)      # 3 * 0.5 rounds to 2 each way
    assert (w, h) == (2, 2)
    assert rgb == bytes((0, 0, 0, 2, 0, 0, 0, 2, 0, 2, 2, 0))    # (0,0) (2,0) (0,2) (2,2)
    assert win_capture.scale(grid(4, 4), 1.5) == grid(4, 4)


def test_window_pixels_goes_onto_the_sessions_desktop_for_its_engines_window(monkeypatch):
    seen = {}

    def on_desktop(name, fn):
        seen["desktop"] = name
        return fn()
    monkeypatch.setattr(win_desktop, "on_desktop", on_desktop)
    monkeypatch.setattr(win_desktop, "windows_of", lambda pid: [501, 502] if pid == 4242 else [])
    monkeypatch.setattr(win_capture, "capture", lambda hwnd: (1, 1, bytes((hwnd % 256, 0, 0))))
    monkeypatch.setattr(win_capture.win_procs, "in_group",
                        lambda pid, unit: (pid, unit) == (4242, "fso-harness-s0000abcd"))
    sess = types.SimpleNamespace(engine_pid=4242, desktop="fso-harness-s0000abcd",
                                 scope="fso-harness-s0000abcd")
    assert win_capture.window_pixels(sess) == (1, 1, bytes((501 % 256, 0, 0)))   # first window
    assert seen["desktop"] == "fso-harness-s0000abcd"


def test_no_window_is_captured_for_a_pid_that_is_not_in_the_sessions_group(monkeypatch):
    # QA Plan B, SA-15 / ST-14: between an engine's end and its record a pid can be
    # another program's. A --visible session's windows are looked up on the user's own
    # desktop, so that program's window would be the picture handed to an agent.
    def never(*arguments):
        raise AssertionError("looked for windows of a process that is not the session's")
    monkeypatch.setattr(win_desktop, "on_desktop", never)
    monkeypatch.setattr(win_capture.win_procs, "in_group", lambda pid, unit: False)
    sess = types.SimpleNamespace(engine_pid=4242, desktop=None, scope="fso-harness-s0000abcd")
    with pytest.raises(HarnessError) as e:
        win_capture.window_pixels(sess)
    assert e.value.code == EXIT_NOT_FOUND and "engine" in e.value.message


def test_a_window_past_the_size_limit_is_refused_before_memory_is_set_aside(gdi, monkeypatch):
    # QA Plan B, SA-11: the engine sizes its window; everything else it can size is capped.
    # The limit is made small here: the test must not itself need memory by the size it names.
    monkeypatch.setattr(win_capture, "MAX_PIXELS", 8)
    g = gdi(size=(3, 3))                                # 9 pixels: one too many
    with pytest.raises(OSError, match="larger than"):
        win_capture.capture(77)
    assert "GetDC" not in g.calls
    gdi(size=(4, 2), bgra=b"\x00" * 32)                 # 8 pixels: allowed
    assert win_capture.capture(77)[:2] == (4, 2)


def test_the_size_limit_is_an_8k_screen():
    assert win_capture.MAX_PIXELS == 7680 * 4320


def test_a_session_with_no_window_says_so_in_the_linux_words(monkeypatch):
    monkeypatch.setattr(win_desktop, "on_desktop", lambda name, fn: fn())
    monkeypatch.setattr(win_desktop, "windows_of", lambda pid: [])
    monkeypatch.setattr(win_capture.win_procs, "in_group", lambda pid, unit: True)
    sess = types.SimpleNamespace(engine_pid=4242, desktop=None, scope="fso-harness-s0000abcd")
    with pytest.raises(HarnessError) as e:
        win_capture.window_pixels(sess)
    assert e.value.code == EXIT_NOT_FOUND
    assert e.value.message == "no main window for the session's process"
    assert e.value.hint == "the engine may still be starting; check `fsoctl status <id>`"


def _running(monkeypatch):
    """A running session, with the platform's capture replaced by a 4x4 grid image."""
    s = session.new_session(kind="game", binary="/bin/true", fs2path="/tmp", mods=[],
                            mission=None, renderer="opengl", width=1280, height=720,
                            switches={}, allow_eval=False, extra_args=[])
    monkeypatch.setattr(launcher, "require_running", lambda sid: s)
    fake = types.SimpleNamespace(window_pixels=lambda sess: grid(4, 4), crop=win_capture.crop,
                                 scale=win_capture.scale, png_bytes=win_capture.png_bytes)
    monkeypatch.setattr(platform, "capture", fake)
    return s


def test_screenshot_writes_a_png_from_the_platforms_pixels(tmp_runtime, monkeypatch):
    s = _running(monkeypatch)
    shot = xinput.screenshot(s.id)
    assert shot["width"] == 4 and shot["height"] == 4
    assert shot["path"].endswith("shot-0001.png")
    png = Path(shot["path"]).read_bytes()
    assert struct.unpack(">II", chunks(png)[0][1][:8]) == (4, 4)
    cropped = xinput.screenshot(s.id, crop="2x2+1+1", scale=0.5)
    assert (cropped["width"], cropped["height"]) == (1, 1)
    assert cropped["path"].endswith("shot-0002.png")


def test_a_bad_crop_leaves_no_empty_file_behind(tmp_runtime, monkeypatch):
    s = _running(monkeypatch)
    with pytest.raises(HarnessError):
        xinput.screenshot(s.id, crop="2x2+9+9")
    assert list((session.run_dir(s) / "shots").glob("*.png")) == []


def test_a_whole_display_shot_is_refused_by_name_where_there_is_no_display(tmp_runtime,
                                                                           monkeypatch):
    s = _running(monkeypatch)
    monkeypatch.setattr(platform, "NAME", "windows")
    with pytest.raises(HarnessError) as e:
        xinput.screenshot(s.id, full=True)
    assert e.value.code == EXIT_REFUSED
    assert e.value.message == "screenshot --full is not available on windows"


@pytest.mark.linux
def test_linux_has_no_capture_module_and_keeps_its_own_way():
    assert platform.capture is None


# ---- gap a mutation run found (QA Plan B, MU-21) ----
def test_the_screenshot_is_taken_of_the_session_asked_for(tmp_runtime, monkeypatch):
    s = _running(monkeypatch)
    asked = []
    platform.capture.window_pixels = lambda sess: asked.append(sess.id) or grid(4, 4)
    xinput.screenshot(s.id)
    assert asked == [s.id]
