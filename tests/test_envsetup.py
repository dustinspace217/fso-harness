from pathlib import Path

from fsoctl.envsetup import child_env, for_kind

_BASE = {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0", "HOME": "/home/x", "PATH": "/usr/bin",
         "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
         "SESSION_MANAGER": "local/h:@/tmp/.ICE-unix/1", "XAUTHORITY": "/run/user/1000/xauth",
         "XDG_CONFIG_HOME": "/home/x/.config",
         "XDG_RUNTIME_DIR": "/run/user/1000", "XDG_CURRENT_DESKTOP": "KDE",
         "KDE_FULL_SESSION": "true", "DESKTOP_SESSION": "plasma",
         "XDG_SESSION_DESKTOP": "KDE"}


def test_child_env_pins_toolkits_to_the_virtual_display():
    base = dict(_BASE)
    env = child_env(base, ":93", Path("/r/s1/prefs"), "opengl", Path("/r/s1"))
    assert "WAYLAND_DISPLAY" not in env
    assert env["DISPLAY"] == ":93"
    assert env["SDL_VIDEODRIVER"] == "x11"
    assert env["GDK_BACKEND"] == "x11"
    assert env["QT_QPA_PLATFORM"] == "xcb"
    assert env["FSO_PREFERENCES_PATH"] == "/r/s1/prefs/"
    assert env["LIBGL_ALWAYS_SOFTWARE"] == "1"
    assert base == _BASE  # caller's dict untouched


def test_child_env_keeps_the_child_out_of_the_users_home_and_session():
    # QA SA-2: FSO's legacy config mode and qtFRED's autosave/QSettings write under $HOME and the
    # XDG folders. QA SA-3/CR-2: SDL inhibits the screensaver over the session D-Bus, and Qt
    # registers with the session manager, both reaching the user's real desktop session.
    env = child_env(dict(_BASE), ":93", Path("/r/s1/prefs"), "opengl", Path("/r/s1"))
    assert env["HOME"] == "/r/s1/home"
    assert env["XDG_CONFIG_HOME"] == "/r/s1/home/.config"
    assert env["XDG_DATA_HOME"] == "/r/s1/home/.local/share"
    assert env["XDG_STATE_HOME"] == "/r/s1/home/.local/state"
    # Shader caches persist across runs, in a folder the harness owns under the REAL home.
    assert env["XDG_CACHE_HOME"] == "/home/x/.cache/fso-harness/cache"
    for var in ("SESSION_MANAGER", "XAUTHORITY"):
        assert var not in env
    assert env["SDL_VIDEO_ALLOW_SCREENSAVER"] == "1"


def test_child_env_cuts_every_route_to_the_session_bus():
    # Fix-wave-2 R2 (SA2-1 / CR2-2, probe-confirmed): with DBUS_SESSION_BUS_ADDRESS merely
    # unset, libdbus falls back to $XDG_RUNTIME_DIR/bus and the engine and qtFRED joined the
    # user's session bus. A private runtime folder plus an address that never exists cut it.
    env = child_env(dict(_BASE), ":93", Path("/r/s1/prefs"), "opengl", Path("/r/s1"))
    assert env["XDG_RUNTIME_DIR"] == "/r/s1/xdg-runtime"
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/r/s1/no-bus"
    for var in ("XDG_CURRENT_DESKTOP", "KDE_FULL_SESSION", "DESKTOP_SESSION",
                "XDG_SESSION_DESKTOP"):
        assert var not in env, var


def test_child_env_vulkan_passes_icd_only_if_caller_set_it():
    env = child_env({"PATH": "/usr/bin", "HOME": "/h"}, ":93", Path("/p"), "vulkan", Path("/r"))
    assert "VK_ICD_FILENAMES" not in env
    assert "LIBGL_ALWAYS_SOFTWARE" not in env
    env2 = child_env({"VK_ICD_FILENAMES": "/x.json", "HOME": "/h"}, ":93", Path("/p"), "vulkan",
                     Path("/r"))
    assert env2["VK_ICD_FILENAMES"] == "/x.json"


def test_qtfred_gets_no_sdl_signal_handlers_and_the_game_keeps_them():
    # SDL turns SIGTERM into a quit event. The engine reads it and shuts down; qtFRED (a Qt app
    # that also starts SDL) never reads it, so it ignored the stop for the runner's whole 20 s
    # grace and was then SIGKILLed (2026-10-12). Without SDL's handler, SIGTERM just ends qtFRED.
    q = for_kind(child_env(dict(_BASE), ":93", Path("/p"), "opengl", Path("/r")), "qtfred")
    g = for_kind(child_env(dict(_BASE), ":93", Path("/p"), "opengl", Path("/r")), "game")
    assert q["SDL_NO_SIGNAL_HANDLERS"] == "1"
    assert "SDL_NO_SIGNAL_HANDLERS" not in g
    # QA CR-6: a game must not inherit it from the caller (it would skip the clean shutdown).
    inherited = {**_BASE, "SDL_NO_SIGNAL_HANDLERS": "1"}
    g2 = for_kind(child_env(inherited, ":93", Path("/p"), "opengl", Path("/r")), "game")
    assert "SDL_NO_SIGNAL_HANDLERS" not in g2
