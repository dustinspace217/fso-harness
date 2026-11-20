"""Which virtual display a Linux session draws into: Weston (GPU) or Xvfb (software).

WHY two: under Xvfb every OpenGL session renders in software (Mesa llvmpipe), single-digit
frames a second for a debug build, a few a second with three sessions of a heavy mod. A headless
Weston on the GPU with a full-screen Xwayland inside it gives the same X display to the game and
to the harness's X tools (screenshots, xdotool input) at about 60 frames a second (probe
2026-11-18, plan docs/superpowers/plans/2026-11-18-weston-display.md). Xvfb stays: CI runners
and machines without a GPU or without Weston have nothing else, the sandbox network has no GPU,
and `compare` and screenshot diffs want software rendering's identical pixels (`--display xvfb`).

The choice is made at launch (launcher) and recorded in the session; the runner starts what was
recorded. This module only decides; runner._start_weston / _start_xvfb do the starting.
"""
import glob
import shutil
import subprocess

from .errors import EXIT_REFUSED, EXIT_USAGE, HarnessError

XVFB = "xvfb"
WESTON = "weston"
CHOICES = ("auto", XVFB, WESTON)

_INSTALL_HINT = "sudo dnf install weston xorg-x11-server-Xwayland"

# A GPU session shares video memory with the desktop. 2026-10-15: four sessions at about 2.45 GB
# each used it up and the desktop went black. 3 GB free before a launch leaves room for one more
# heavy session plus the desktop's own needs (mine: sized from that incident, not measured).
MIN_FREE_VIDEO_MEMORY_MIB = 3072
# Charged for each live GPU session (Weston or Vulkan) on top of the floor: what it may still
# claim after its launch (mine: between the ~314 MiB a retail session took and the ~2.45 GB of a
# heavy mod on 2026-10-15; the floor above covers the rest).
LIVE_SESSION_RESERVE_MIB = 1024
_QUERY_TIMEOUT = 10.0       # a hung nvidia-smi or glxinfo must not hold up a launch for long


def _has_program(name: str) -> bool:
    """True when `name` is an executable on PATH (receives a program name, e.g. "weston")."""
    return shutil.which(name) is not None


def _has_render_node() -> bool:
    """True when the machine exposes a GPU render node (/dev/dri/renderD128 and up).

    WHY a render node and not "any GPU": a render node is what a headless compositor opens to
    draw on the GPU without a monitor; machines without one (CI, most VMs) cannot run Weston's GL
    renderer.
    """
    return bool(glob.glob("/dev/dri/renderD*"))


def _why_not_weston(network: str) -> str | None:
    """Return the reason Weston cannot run for a session on `network` (sandbox, programs, render
    node), or None when it can. Video memory is judged separately (choose)."""
    if network == "sandbox":
        # Xwayland could hand a sandboxed engine a GPU device over its X socket, and the sandbox
        # binds no GPU device of its own (sandbox.py; QA SA-1).
        return "the sandbox network uses Xvfb only"
    missing = [p for p in ("weston", "Xwayland") if not _has_program(p)]
    if missing:
        return f"{' and '.join(missing)} not installed (install with: {_INSTALL_HINT})"
    if not _has_render_node():
        return "no GPU render node (/dev/dri/renderD*)"
    return None


def _memory_shortfall(free: int | None, live_gpu_sessions: int) -> str | None:
    """Return why the GPU has too little free video memory for one more session, or None.

    Receives: free (MiB, from _free_video_memory_mib; None when unmeasured) and the number of live
    GPU sessions (launcher._live_gpu_sessions). WHY charge every live one: a session's textures
    load after its launch returns (each mission loads its own), so the free amount read now does
    not yet hold what the live sessions will still claim; each is charged
    LIVE_SESSION_RESERVE_MIB (QA CR-7/SL-2, XFA-2/XFK-2). Sessions already loaded are charged
    too, which costs concurrency, not safety: past the floor an OpenGL session on "auto" falls
    back to Xvfb and a Vulkan session is refused.
    """
    if free is None:
        return None
    if free - live_gpu_sessions * LIVE_SESSION_RESERVE_MIB >= MIN_FREE_VIDEO_MEMORY_MIB:
        return None
    live = f" with {live_gpu_sessions} GPU session(s) running" if live_gpu_sessions else ""
    return (f"only {free} MiB of video memory free{live} (one more GPU session needs "
            f"{MIN_FREE_VIDEO_MEMORY_MIB} MiB plus {LIVE_SESSION_RESERVE_MIB} MiB per running one)")


def _free_video_memory_mib() -> int | None:
    """Return the free video memory in MiB of the GPU with the least free, or None when it
    cannot be measured (no nvidia-smi, or it failed or hung).

    WHY only NVIDIA: nvidia-smi is the one reader this machine has; other vendors get a launch
    warning that memory was not checked, not a refusal.
    """
    if not _has_program("nvidia-smi"):
        return None
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True,
                             timeout=_QUERY_TIMEOUT, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    values = [int(line) for line in out.split() if line.isdigit()]
    return min(values) if values else None


def choose(requested: str, network: str, renderer: str = "opengl",
           live_gpu_sessions: int = 0) -> tuple[str, str | None]:
    """Decide the display server for a session.

    Receives: requested ("auto" | "xvfb" | "weston", from --display), network ("real" |
    "private" | "sandbox", already resolved by the launcher), renderer ("opengl" | "vulkan") and
    the number of live GPU sessions (launcher._live_gpu_sessions).
    Returns: (server, note). server is XVFB or WESTON; note is None or text for the launch
    warnings: why "auto" fell back to Xvfb, or that video memory could not be checked. Raises
    HarnessError(EXIT_USAGE) for an unknown value, and for an explicit "weston" that cannot run
    here: an agent that asked for the GPU by name should hear why, not get software rendering
    silently. Raises HarnessError(EXIT_REFUSED) for a Vulkan session short of video memory.

    WHY Vulkan is refused, not moved to Xvfb (QA SL-3/TA-8, XFA-5/XFK-3): a Vulkan session draws
    on the GPU on either display, so a move would save nothing, and running short of video memory
    blanks the desktop (2026-10-15). Owner, 2026-11-20: "refuse it. We need to make sure to not
    crash users' computers." The check runs on every path, an explicit Xvfb included.
    """
    if requested not in CHOICES:
        raise HarnessError(EXIT_USAGE, f"unknown --display {requested!r}",
                           f"use one of {', '.join(CHOICES)}")
    reason = None if requested == XVFB else _why_not_weston(network)
    on_gpu = renderer == "vulkan" or (requested != XVFB and reason is None)
    free = _free_video_memory_mib() if on_gpu else None      # read once (QA CR-2)
    shortfall = _memory_shortfall(free, live_gpu_sessions) if on_gpu else None
    if shortfall and renderer == "vulkan":
        stop = "stop a GPU session, or " if live_gpu_sessions else ""
        raise HarnessError(EXIT_REFUSED, f"not enough video memory for a Vulkan session: "
                           f"{shortfall}", f"{stop}launch with OpenGL on --display auto or xvfb "
                           "(software rendering, no video memory)")
    unchecked = "video memory not checked (no nvidia-smi answer)" if on_gpu and free is None \
        else None
    if requested == XVFB:
        return XVFB, unchecked
    if shortfall and reason is None:
        reason = shortfall
    if reason is None:
        return WESTON, unchecked
    if requested == WESTON:
        raise HarnessError(EXIT_USAGE, f"--display weston is not possible here: {reason}",
                           "drop --display to fall back to Xvfb, or fix what is named")
    kind = "software OpenGL" if renderer == "opengl" else "Vulkan still on the GPU"
    note = f"Xvfb ({kind}): {reason}"
    return XVFB, f"{note}; {unchecked}" if unchecked else note


def gl_env(env: dict, server: str) -> dict:
    """Return a copy of `env` with software OpenGL dropped when the session runs on Weston.
    Receives the engine environment from envsetup.child_env, which always asks for software OpenGL
    for an OpenGL session, and the session's display server.

    WHY here and not in child_env: child_env takes the renderer, not the display, and widening it
    would touch every caller; Xvfb has no GPU behind it, so software OpenGL is right there, and
    under Weston the GPU's own OpenGL is the point of the change.
    """
    env = dict(env)
    if server == WESTON:
        env.pop("LIBGL_ALWAYS_SOFTWARE", None)
    return env


def gl_renderer(x_display: str, engine_env: dict) -> str | None:
    """Return the OpenGL renderer string a program on `x_display` gets (glxinfo -B), or None when
    it cannot be read (no glxinfo, no answer within the timeout, no renderer line). Receives the
    engine's environment, so the answer is what the engine will get (QA CR-6).

    WHY ask the display and not the engine's libraries: Mesa's software renderer and Mesa's GPU
    drivers load the same library (libgallium), so the libraries cannot say whether a frame was
    drawn on the GPU; the renderer string can ("llvmpipe" is software).
    """
    if not _has_program("glxinfo"):
        return None
    env = dict(engine_env)
    env["DISPLAY"] = x_display
    try:
        out = subprocess.run(["glxinfo", "-B"], capture_output=True, text=True, env=env,
                             timeout=_QUERY_TIMEOUT, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        if line.startswith("OpenGL renderer string:"):
            return line.split(":", 1)[1].strip() or None
    return None
