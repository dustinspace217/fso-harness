"""Environment for every process the harness launches.

WHY each pin: SDL draws on the virtual display only with DISPLAY + SDL_VIDEODRIVER=x11; FSO's error
boxes are zenity windows, which reached the user's Wayland desktop through the default wayland-0
socket until GDK_BACKEND/QT_QPA_PLATFORM were pinned (incident 2026-10-12). Xvfb has no hardware GL,
so OpenGL runs force Mesa's software renderer explicitly instead of leaving the driver to guess.
No Vulkan driver is chosen here: VK_ICD_FILENAMES passes through only if the caller set it.

The child also gets a private HOME and XDG folders inside its run folder, and loses the variables
that connect it to the user's desktop session (QA SA-2, SA-3, CR-2):
- HOME/XDG_*: FSO's legacy config mode uses ~/.fs2_open when ~/.fs2_open/fs2_open.ini exists, and
  qtFRED autosaves missions and writes its settings under the XDG data/config folders, all of which
  would land in the user's real home (source-read by the QA security pass).
- The session bus: the engine's SDL3 contains an org.freedesktop.ScreenSaver D-Bus client (seen
  in libSDL3's strings, 2026-10-12) and qtFRED links Qt's D-Bus module. Removing
  DBUS_SESSION_BUS_ADDRESS was NOT enough: libdbus then connects to $XDG_RUNTIME_DIR/bus, and
  both programs showed up in `busctl --user list` (probe SA2-1, 2026-10-12). So the child gets a
  private XDG_RUNTIME_DIR inside its run folder and a bus address that never exists
  (fix-wave-2 ruling R2). The game always runs with -nosound, so audio does not need the real
  runtime folder. SDL_VIDEO_ALLOW_SCREENSAVER=1 is SDL's own switch for not inhibiting the
  screensaver.
- SESSION_MANAGER / XAUTHORITY: SESSION_MANAGER is how X11 programs join the desktop's logout
  handshake (XSMP). XDG_CURRENT_DESKTOP / KDE_FULL_SESSION / DESKTOP_SESSION /
  XDG_SESSION_DESKTOP make toolkits load desktop-specific integration; a headless child has none.
XDG_CACHE_HOME stays persistent (~/.cache/fso-harness/cache) so Mesa's shader cache survives
between runs instead of being rebuilt in every run folder.
"""
from pathlib import Path

_DESKTOP_SESSION_VARS = ("WAYLAND_DISPLAY", "SESSION_MANAGER", "XAUTHORITY",
                         "XDG_CURRENT_DESKTOP", "KDE_FULL_SESSION", "DESKTOP_SESSION",
                         "XDG_SESSION_DESKTOP")


def child_env(base: dict, display: str | None, prefs: Path, renderer: str, run: Path) -> dict:
    """Return a copy of `base` set up for a headless FSO/qtFRED child process.

    Receives: base (the caller's environment; not modified), display (":N" from Xvfb), prefs (the
    session's FSO_PREFERENCES_PATH folder), renderer ("opengl" | "vulkan"), run (the session's
    run folder; the runner creates run/home and run/xdg-runtime, and never creates run/no-bus).
    The real home, for the shared cache, is read from base["HOME"].
    """
    home = run / "home"
    env = dict(base)
    real_home = Path(base.get("HOME") or Path.home())
    for var in _DESKTOP_SESSION_VARS:
        env.pop(var, None)
    env.update({
        "DISPLAY": display,
        "SDL_VIDEODRIVER": "x11",
        "GDK_BACKEND": "x11",
        "QT_QPA_PLATFORM": "xcb",
        "SDL_VIDEO_ALLOW_SCREENSAVER": "1",
        "FSO_PREFERENCES_PATH": str(prefs).rstrip("/") + "/",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_CACHE_HOME": str(real_home / ".cache" / "fso-harness" / "cache"),
        "XDG_RUNTIME_DIR": str(run / "xdg-runtime"),
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={run / 'no-bus'}",
    })
    if display is None:
        # A standalone multiplayer server: no window, so no display of any kind (the caller's
        # DISPLAY would otherwise leak through to the engine).
        env.pop("DISPLAY", None)
    if renderer == "opengl":
        env["LIBGL_ALWAYS_SOFTWARE"] = "1"
    else:
        env.pop("LIBGL_ALWAYS_SOFTWARE", None)
    return env


def for_kind(env: dict, kind: str) -> dict:
    """Return `env` (changed in place) with what a session of this kind ("game" | "qtfred") needs.

    qtfred: SDL_NO_SIGNAL_HANDLERS=1. SDL turns SIGTERM into a quit event; the engine reads it and
    shuts down, but qtFRED (a Qt app that also starts SDL: os_init, qtfred/src/mission/
    management.cpp:71 -> code/osapi/osapi.cpp:424) never does,
    so a stop waited out the runner's whole 20 s grace and ended in SIGKILL (2026-10-12). With
    SDL's handler off, SIGTERM ends qtFRED at once; it only ever edits a copy, so nothing is lost.
    """
    if kind == "qtfred":
        env["SDL_NO_SIGNAL_HANDLERS"] = "1"
    else:
        # The game must keep SDL's handler even if the caller's environment turned it off: its
        # quit event is the clean shutdown the runner-first stop relies on (QA CR-6).
        env.pop("SDL_NO_SIGNAL_HANDLERS", None)
    return env
