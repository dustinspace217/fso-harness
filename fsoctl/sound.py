"""sound: a session's own audio output, for `launch --sound`.

The game normally runs with -nosound. With --sound it plays into a null sink of its own on the
user's PulseAudio/PipeWire server: a sink that outputs nowhere, so nothing reaches the speakers,
and whose monitor source a recorder can capture (`ffmpeg -f pulse -i <sink>.monitor`), one
session's audio only. The sink name is fsoh_<root tag>_<session id>; the tag (from the runtime
root) lets `cleanup` tell this root's sinks from another root's on the one shared server.

WHY a null sink on the user's server and not a private sound server: the engine needs a Pulse
server to talk to, and a second server per session is far more machinery than one module load
that the server removes again on unload. The session keeps its private runtime folder (envsetup:
no desktop D-Bus); the Pulse socket's path is given to it in PULSE_SERVER.

WHY write_device_prefs: FSO does not open OpenAL's default device; it opens playback and capture
devices BY NAME, sorting the configured device first, then the server's default (sound/openal.cpp
find_playback_device, find_capture_device), so PULSE_SINK alone is ignored. Seen 2026-10-15: the
first --sound session opened the user's USB output, and every --sound launch opened and closed
the default capture device (the desk mic). Naming the null sink and its monitor in the engine's
own settings makes it open those. PULSE_SINK stays for anything that opens the default (the
speech daemon the engine starts inside its own scope). ALSOFT_DRIVERS=pulse keeps OpenAL Soft on
the Pulse backend, whose device names are the sink descriptions written here.

WHY node.dont-reconnect and node.dont-fallback (PULSE_PROP): when a sink disappears, PipeWire
moves its streams to the default sink (the speakers); with dont-reconnect the engine's stream is
left on no sink, silent, instead (probed 2026-10-15 on the engine: the stream stayed on "none",
the engine kept running, stop took 0.3 s). A stream that names a sink that does not exist is
linked to the default at once (probed: a client naming a missing sink played on the USB output);
dont-fallback makes it fail to open instead. dont-reconnect also makes moving a stream
impossible, so verify_routing cannot repair a stream on the wrong sink: it fails the launch, and
the launch is abandoned.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import paths, session
from .errors import EXIT_LAUNCH, HarnessError

PREFIX = "fsoh_"
_PACTL_TIMEOUT = 10
_LOCK_WAIT = 10.0
# Worst cases the MCP server's time budgets include (tests/mcp_check.py checks the sums):
# Every harness audio client gets these (engine via engine_env, the recorder via its script).
STREAM_PROPS = "node.dont-reconnect=true node.dont-fallback=true"
REMOVE_WORST_SEC = _LOCK_WAIT + 2 * _PACTL_TIMEOUT          # remove_sink: lock, list, unload
LAUNCH_WORST_SEC = _LOCK_WAIT + 4 * _PACTL_TIMEOUT    # create_sink, device-name check, verify (2)


class PactlMissing(HarnessError):
    """pactl is not installed: there is no sound server to talk to, so nothing to clean up."""


def _runtime_dir() -> str:
    """Return the user's real runtime folder (where the Pulse socket lives)."""
    return os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"


def server_address() -> str:
    """Return the local Pulse socket address, used by both pactl and the engine.

    WHY one fixed address and not an inherited PULSE_SERVER: the sink must be created on the same
    server the engine plays to, and an inherited remote PULSE_SERVER would send game audio over
    the network.
    """
    return f"unix:{_runtime_dir()}/pulse/native"


def root_tag() -> str:
    """Return a short tag for this runtime root, part of every sink name it creates."""
    return hashlib.sha256(str(paths.runtime_root()).encode()).hexdigest()[:6]


def sink_name(sid: str) -> str:
    """Return the null sink name for session `sid` (its monitor is sink_name + ".monitor")."""
    return f"{PREFIX}{root_tag()}_{sid}"


def capture_name(sid: str) -> str:
    """Return the OpenAL capture-device name of the session sink's monitor (probed 2026-10-15)."""
    return "Monitor of " + sink_name(sid)


def _pactl(*args: str) -> str:
    """Run pactl against server_address() with `args`; return stdout.

    Raises PactlMissing when pactl is not installed, EXIT_LAUNCH with pactl's message otherwise.
    """
    env = dict(os.environ, PULSE_SERVER=server_address())
    try:
        out = subprocess.run(["pactl", *args], capture_output=True, text=True, check=False,
                             timeout=_PACTL_TIMEOUT, env=env)
    except FileNotFoundError as e:
        raise PactlMissing(EXIT_LAUNCH, "pactl is not installed (needed for --sound)",
                           "install pulseaudio-utils, or launch without --sound") from e
    except subprocess.TimeoutExpired as e:
        raise HarnessError(EXIT_LAUNCH, f"pactl {args[0]} did not answer in {_PACTL_TIMEOUT}s",
                           "check the PulseAudio/PipeWire server, or launch without --sound") from e
    if out.returncode != 0:
        raise HarnessError(EXIT_LAUNCH, f"pactl {args[0]} failed: {out.stderr.strip()}",
                           "check the PulseAudio/PipeWire server, or launch without --sound")
    return out.stdout


def _pactl_json(*args: str) -> list[dict]:
    """Run `pactl -f json <args>`; return the list. A reply that is not a JSON list raises
    EXIT_LAUNCH (pactl older than 16 has no -f json)."""
    text = _pactl("-f", "json", *args)
    try:
        data = json.loads(text)
    except ValueError as e:
        raise HarnessError(EXIT_LAUNCH, f"pactl {' '.join(args)} did not return JSON",
                           "--sound needs pactl 16 or newer") from e
    if not isinstance(data, list):
        raise HarnessError(EXIT_LAUNCH, f"pactl {' '.join(args)} returned {type(data).__name__}",
                           "--sound needs pactl 16 or newer")
    return [d for d in data if isinstance(d, dict)]


def sound_lock():
    """Return the lock held around loading and unloading session sinks.

    WHY a user-wide file and not launch.lock: PipeWire hands a freed module index to the next
    module loaded at once, so a check-then-unload must not interleave with any load on the same
    server, from any runtime root; and launch.lock is already held by _start when _abandon calls
    stop, so taking it again there would wait on itself.
    """
    return session.file_lock(Path(_runtime_dir()) / "fso-harness-sound.lock", _LOCK_WAIT)


def create_sink(sid: str) -> int:
    """Load a null sink named sink_name(sid); return its module index (for remove_sink)."""
    name = sink_name(sid)
    with sound_lock():
        out = _pactl("load-module", "module-null-sink", f"sink_name={name}",
                     f"sink_properties=device.description={name}")
    try:
        return int(out.strip())
    except ValueError as e:
        raise HarnessError(EXIT_LAUNCH, f"pactl load-module returned {out.strip()!r}",
                           "launch without --sound") from e


def write_device_prefs(prefs: Path, sid: str) -> None:
    """Append the device settings naming the session's sink to prefs/fs2_open.ini.

    Receives: prefs (the session's private prefs folder), sid. Playback: [Audio] PlaybackDevice
    (read when in-game options are on; a JSON string, OptionsManager) and the legacy [Sound]
    PlaybackDevice (options off). Capture: only the legacy [Sound] CaptureDevice can apply; the
    in-game branch indexes a device list that is still empty at that point (openal.cpp
    find_capture_device). A fresh prefs folder has no mod settings file, so the engine reads all
    of them from fs2_open.ini (osregistry.cpp).
    """
    name = sink_name(sid)
    with open(prefs / "fs2_open.ini", "a", encoding="utf-8") as f:
        f.write(f"\n[Sound]\nPlaybackDevice={name}\nCaptureDevice={capture_name(sid)}\n"
                f"\n[Audio]\nPlaybackDevice=\"{name}\"\n")


def _openal_lib(binary: str) -> str:
    """Return the path of the libopenal the engine at `binary` loads, as the dynamic loader
    resolves it (`ldd`: RPATH/RUNPATH, then the system paths). Raises EXIT_LAUNCH when it cannot
    be resolved.

    WHY ldd and not a guess: the build may link a prebuilt OpenAL Soft through its RPATH or the
    system one (lib/OpenAL.cmake), and the check is only worth anything if it lists the same
    library's devices (verified 2026-10-15: build-vk resolves to the copy beside the binary).
    """
    try:
        out = subprocess.run(["ldd", binary], capture_output=True, text=True, check=False,
                             timeout=_PACTL_TIMEOUT).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HarnessError(EXIT_LAUNCH, f"ldd {binary} failed: {e}",
                           "launch without --sound") from e
    for line in out.splitlines():
        name, _, rest = line.strip().partition(" => ")
        if name.startswith("libopenal.so") and rest.startswith("/"):
            return rest.split(" (", 1)[0]
    raise HarnessError(EXIT_LAUNCH, f"cannot tell which libopenal {binary} loads (ldd)",
                       "launch without --sound")


def check_device_names(binary: str, sid: str) -> None:
    """Fail unless the engine's OpenAL lists the session sink and its monitor under the names
    write_device_prefs gives the engine.

    WHY before the engine starts: FSO matches a configured device name exactly and, on a miss,
    opens the server default instead (openal.cpp find_playback_device / find_capture_device),
    which is the speakers and the microphone. A name that drifts (another OpenAL Soft, another
    description format) would fail open with no error, so a miss refuses the launch here.
    Runs openal_names.py on the engine's own libopenal with the engine's sound variables and an
    empty HOME (the engine has a private one, so neither reads the user's alsoft.conf); opens
    no device.
    """
    lib = _openal_lib(binary)
    script = Path(__file__).with_name("openal_names.py")
    with tempfile.TemporaryDirectory(prefix="fsoh-openal-") as home:
        env = dict(os.environ, **engine_env(sid), HOME=home, XDG_CONFIG_HOME=home)
        try:
            out = subprocess.run([sys.executable, "-I", str(script), lib], env=env,
                                 capture_output=True, text=True, check=False,
                                 timeout=_PACTL_TIMEOUT)
            lists = json.loads(out.stdout) if out.returncode == 0 else None
        except (subprocess.TimeoutExpired, ValueError):
            lists = None
    if not isinstance(lists, dict):
        raise HarnessError(EXIT_LAUNCH, "could not list the engine's OpenAL devices",
                           "launch without --sound")
    missing = [n for n, kind in ((sink_name(sid), "playback"), (capture_name(sid), "capture"))
               if n not in (lists.get(kind) or [])]
    if missing:
        raise HarnessError(EXIT_LAUNCH, f"the engine's OpenAL does not list {missing}",
                           "the engine would fall back to the default speakers and microphone; "
                           "launch without --sound")


def _null_sinks() -> dict[int, str]:
    """Return {module index: sink_name} for every module-null-sink on the server."""
    # The text listing, not -f json: PipeWire's JSON module list carries no index (pactl 17.0,
    # probed 2026-10-15), and the index is what unload-module needs. Lines: index TAB name TAB args.
    out = {}
    for line in _pactl("list", "short", "modules").splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or parts[1] != "module-null-sink" or not parts[0].isdigit():
            continue
        args = parts[2].split()
        out[int(parts[0])] = next((a.split("=", 1)[1] for a in args
                                   if a.startswith("sink_name=")), "")
    return out


def remove_sink(module: int, sid: str) -> str | None:
    """Unload the session's sink module, if `module` is still that sink. Returns None, or the
    error message (stop must not fail on it).

    WHY the ownership check: PipeWire hands a freed module index to the next module loaded
    (probed 2026-10-15: unload 536870916, the next load got 536870916). Unloading a stale index
    could remove another session's sink, or one of the user's own modules, so the index is only
    unloaded while it still names sink_name(sid). Gone already = nothing to do. The check and the
    unload run under sound_lock, which every harness sink load also takes.
    """
    try:
        with sound_lock():
            if _null_sinks().get(module) != sink_name(sid):
                return None
            _pactl("unload-module", str(module))
    except HarnessError as e:
        return e.message
    except OSError as e:          # the lock file itself (a missing or unwritable runtime folder)
        return f"sound lock: {e}"
    return None


def engine_env(sid: str) -> dict:
    """Return the variables that route the engine's audio to the session's sink.

    PULSE_SERVER names the socket because the engine's XDG_RUNTIME_DIR is private and no longer
    leads to it; PULSE_PROP keeps the engine's streams off the default sink (module docstring).
    """
    return {"PULSE_SERVER": server_address(),
            "PULSE_SINK": sink_name(sid),
            "ALSOFT_DRIVERS": "pulse",
            "PULSE_PROP": STREAM_PROPS}


def _sinks() -> dict[int, str]:
    """Return {sink index: sink name} from the server."""
    return {s["index"]: s.get("name", "") for s in _pactl_json("list", "short", "sinks")
            if "index" in s}


def streams_of(pid: int) -> list[dict]:
    """Return the playback streams of process `pid`: [{"index", "sink"}], sink as a name."""
    names = _sinks()
    found = []
    for si in _pactl_json("list", "sink-inputs"):
        props = si.get("properties") or {}
        if str(props.get("application.process.id")) == str(pid) and "index" in si:
            sink = si.get("sink")
            found.append({"index": si["index"], "sink": names.get(sink, str(sink))})
    return found


def verify_routing(pid: int, sid: str) -> dict:
    """Check that the engine plays into its own sink; returns {"sink", "streams": n}.

    Raises EXIT_LAUNCH when the engine has no stream at all (it cannot be recorded) or any
    stream is on another sink (it may be playing on the speakers; with node.dont-reconnect it
    cannot be moved). The caller abandons the launch, which stops the engine.
    """
    want = sink_name(sid)
    streams = streams_of(pid)
    if not streams:
        raise HarnessError(EXIT_LAUNCH, f"the engine (pid {pid}) opened no audio stream",
                           "check that pactl and the engine talk to the same sound server")
    wrong = [s for s in streams if s["sink"] != want]
    if wrong:
        raise HarnessError(EXIT_LAUNCH,
                           f"audio stream(s) on {sorted({s['sink'] for s in wrong})}, not {want}",
                           "the session was stopped; the engine did not open the device "
                           "named in its prefs")
    return {"sink": want, "streams": len(streams)}


def _recorded_sinks() -> set[str]:
    """Return the names of sinks whose monitor has a capture stream (a recorder) attached."""
    sources = {s["index"]: s.get("name", "") for s in _pactl_json("list", "short", "sources")
               if "index" in s}
    return {sources.get(o.get("source", -1), "").removesuffix(".monitor")
            for o in _pactl_json("list", "short", "source-outputs")}


def orphan_sinks(live_ids: set[str]) -> tuple[list[tuple[int, str]], list[str]]:
    """Return (orphans, kept): this runtime root's sinks whose session id is not in live_ids,
    as (module index, sink name); and the names of such sinks kept because a recorder is still
    capturing their monitor (unloading moves that capture onto the desktop's audio, probed
    2026-10-15). Sinks of other runtime roots are never returned: their own cleanup owns them.
    """
    mine = f"{PREFIX}{root_tag()}_"
    candidates = [(index, name) for index, name in _null_sinks().items()
                  if name.startswith(mine) and name[len(mine):] not in live_ids]
    if not candidates:
        return [], []
    recorded = _recorded_sinks()
    return ([c for c in candidates if c[1] not in recorded],
            [name for _, name in candidates if name in recorded])
