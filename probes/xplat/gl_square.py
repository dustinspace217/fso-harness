"""Probe 1 stand-in (cross-platform Plan A, Task 5): an SDL2 OpenGL window like the engine's.

FSO 26.0.1 is built on SDL2 and asks it for a core-profile OpenGL context, trying 4.5 down to its
minimum, 3.2 (gropengl.cpp). This program asks for that minimum, 3.2 core, in a 256x256 window,
clears it to a known colour every frame, and talks to the probe that started it
(win_hidden_desktop.py) through files in out_dir:

    ready.json  written once, after a few frames: pid, window handle, GL strings, and the
                centre pixel read back with glReadPixels (the probe judges it against CLEAR)
    key.txt     written when SDL delivers a Space key-down (the probe posts one)
    stop        created by the probe; the stand-in exits when it sees it

It exits by itself after LIFETIME_S, so a probe that dies never leaves it running.

    python gl_square.py <out_dir>
"""
import ctypes
import json
import os
import sys
import time
from pathlib import Path

import sdl2

LIFETIME_S = 150     # beyond the probe's worst case (~115 s: ready, three 30 s threads, key)
SIZE = 256
CLEAR = (0.25, 0.5, 0.75)        # win_hidden_desktop.EXPECT is this in 8-bit channels
READY_AFTER_FRAMES = 10          # a few presented frames before reading back


def write_json(path: Path, data: dict) -> None:
    """Write data as JSON through a temporary name, so a reader never sees half a file."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def make_window():
    """Create the SDL window and its GL context; return (window, context), or raise RuntimeError
    with SDL's own error text."""
    if sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO) != 0:
        raise RuntimeError("SDL_Init: " + sdl2.SDL_GetError().decode(errors="replace"))
    sdl2.SDL_GL_SetAttribute(sdl2.SDL_GL_CONTEXT_MAJOR_VERSION, 3)
    sdl2.SDL_GL_SetAttribute(sdl2.SDL_GL_CONTEXT_MINOR_VERSION, 2)
    sdl2.SDL_GL_SetAttribute(sdl2.SDL_GL_CONTEXT_PROFILE_MASK, sdl2.SDL_GL_CONTEXT_PROFILE_CORE)
    sdl2.SDL_GL_SetAttribute(sdl2.SDL_GL_DOUBLEBUFFER, 1)
    window = sdl2.SDL_CreateWindow(b"gl_square", sdl2.SDL_WINDOWPOS_CENTERED,
                                   sdl2.SDL_WINDOWPOS_CENTERED, SIZE, SIZE,
                                   sdl2.SDL_WINDOW_OPENGL | sdl2.SDL_WINDOW_SHOWN)
    if not window:
        raise RuntimeError("SDL_CreateWindow: " + sdl2.SDL_GetError().decode(errors="replace"))
    context = sdl2.SDL_GL_CreateContext(window)
    if not context:
        raise RuntimeError("SDL_GL_CreateContext: " + sdl2.SDL_GetError().decode(errors="replace"))
    return window, context


def window_handle(window):
    """Return the Win32 HWND of window as an int, or None on other platforms."""
    info = sdl2.SDL_SysWMinfo()
    sdl2.SDL_VERSION(info.version)
    if not sdl2.SDL_GetWindowWMInfo(window, ctypes.byref(info)):
        return None
    if info.subsystem != sdl2.SDL_SYSWM_WINDOWS:
        return None
    return info.info.win.window or None


def gl_strings(gl) -> dict:
    """Return the context's version and renderer strings."""
    return {"gl_version": gl.glGetString(gl.GL_VERSION).decode(errors="replace"),
            "gl_renderer": gl.glGetString(gl.GL_RENDERER).decode(errors="replace")}


def centre_pixel(gl):
    """Read back the centre pixel of the back buffer after a clear; return (r, g, b)."""
    gl.glFinish()
    raw = gl.glReadPixels(SIZE // 2, SIZE // 2, 1, 1, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
    return tuple(bytes(raw)[:3])


def space_pressed() -> bool:
    """Drain SDL's event queue; True if it held a Space key-down."""
    event = sdl2.SDL_Event()
    seen = False
    while sdl2.SDL_PollEvent(ctypes.byref(event)):
        if event.type == sdl2.SDL_KEYDOWN and event.key.keysym.sym == sdl2.SDLK_SPACE:
            seen = True
    return seen


def run(out: Path) -> int:
    """Open the window, report readiness, and serve frames until stopped. Returns an exit code."""
    try:
        window, _context = make_window()
    except RuntimeError as e:
        write_json(out / "ready.json", {"pid": os.getpid(), "error": str(e)})
        return 1
    # WHY imported only now: PyOpenGL binds opengl32 by name when it is imported, and Windows
    # hands a by-name load the module already loaded under that name. Importing after SDL created
    # the context makes PyOpenGL call the same OpenGL that SDL loaded (Mesa, when the probe sets
    # SDL_OPENGL_LIBRARY), not the system one.
    from OpenGL import GL  # noqa: PLC0415 - order matters, see above
    report = {"pid": os.getpid(), "hwnd": window_handle(window), **gl_strings(GL)}
    deadline = time.monotonic() + LIFETIME_S
    frame = 0
    while time.monotonic() < deadline and not (out / "stop").exists():
        GL.glClearColor(*CLEAR, 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        frame += 1
        if frame == READY_AFTER_FRAMES:
            write_json(out / "ready.json", {**report, "pixel": centre_pixel(GL)})
        sdl2.SDL_GL_SwapWindow(window)
        if space_pressed():
            (out / "key.txt").write_text(f"space at frame {frame}\n")
        sdl2.SDL_Delay(16)
    sdl2.SDL_Quit()
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python gl_square.py <out_dir>")
    sys.exit(run(Path(sys.argv[1])))
