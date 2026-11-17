# Linux-only call sites in fsoctl (inventory, 2026-11-16)

Provenance: written by a read-only exploration agent for cross-platform Plan B. It is a MAP,
not evidence: line numbers and "called from" lists were not re-checked by the head session.
Verify each entry against the source before editing (workspace rule: Verify Over Recall).

I read every in-scope file in full: launcher.py (lines 1-1827), runner, scope, envsetup, xwin, xinput, sound, crash, sandbox, match, paths, session, channel, safefs, renderer, shadowdir, bridge_install and conftest. For the lighter modules (fred, build, docs, compare, cli, lua_check, mcp/server.py, the two .sh scripts) I read the relevant sections and grepped the rest. Callers below come from cross-module grep. I discarded matches that only appear in comments.

## 1. Linux/POSIX use, per module
Format: function | lines | facility | purpose | called from

**scope.py** (the whole module is Linux-only)
- _meminfo_kb | 25-37 | /proc/meminfo | read memory fields | _mem_available_kb
- _mem_available_kb | 40-42 | /proc/meminfo | MemAvailable | mem_cap_kb, room_for_engine
- room_for_engine | 52-74 | /proc/meminfo | refuse a launch when RAM is short | launcher.check_capacity
- caps | 77-83 | systemd properties MemoryMax, MemorySwapMax, CPUQuota, TasksMax, RuntimeMaxSec | resource caps | launcher._start, match.create, build.build
- _wait_active | 86-101 | polls systemctl | wait for the scope to register | start
- start | 104-128 | `systemd-run --user --scope --unit`, start_new_session=True | run argv in a named scope | launcher._start, match.create
- is_active | 131-135 | `systemctl --user is-active` | liveness | launcher (_scope_active_cached, _is_live, run_to_exit), match.alive, build.build
- active_units | 138-150 | `systemctl --user list-units fso-harness-s*` | any session running? | bridge_install.install
- state / is_gone | 153-171 | systemctl is-active state word | "fully ended" check | launcher (host_port, web_port, assign_port, default_connect, check_capacity, _check_match_room, _stop_scope_only, _removable, _cleanup_sandbox_orphans), match._abandoned, crash._end_hints
- _kill | 174-191 | `systemctl kill --signal=SIGKILL` | force kill | stop
- stop | 194-213 | `systemctl --user stop` | polite stop, then escalate to _kill | launcher.stop, launcher._stop_scope_only, match.end_holder, build.build

**envsetup.py**
- _DESKTOP_SESSION_VARS | 33-35 | WAYLAND_DISPLAY, XAUTHORITY, SESSION_MANAGER, XDG_* desktop variables | removed from the child env | child_env
- child_env | 38-74 | DISPLAY, x11/xcb backends, HOME/XDG redirection, D-Bus address, Mesa LIBGL_ALWAYS_SOFTWARE | headless child environment | runner.main
- for_kind | 77-92 | SDL_NO_SIGNAL_HANDLERS (how SIGTERM is handled) | per-kind tweak | runner.main

**xwin.py**
- _xdotool | 15-20 | xdotool with DISPLAY, WAYLAND_DISPLAY removed | run X queries | windows
- windows | 23-26 | `xdotool search --onlyvisible --pid`, `getwindowname` | list windows by pid | main_window, fred._visible_titles, fred._dialog_id
- main_window | 34-40 | via windows | find the main window | launcher._wait_window, xinput.window_id

**xinput.py** (portable here: parse_crop, check_scale, new_shot_path 111-128, which uses O_EXCL)
- check_keys | 62-68 | xdotool keysym names | validate key names | keys
- _x | 71-80 | runs any X tool with DISPLAY | X tool runner | every function below; fred.open, save, _close_dialog, check
- _release | 83-91 | xdotool keyup/mouseup | cleanup on an error path | keys, _presses, drag
- window_id | 94-101 | xwin.main_window | window id | _move, screenshot, window_origin, _focus; fred._window
- _move | 104-108 | `xdotool mousemove --window` | move the pointer | click, drag, move, focus_neutral, scroll
- window_origin | 131-139 | `xdotool getwindowgeometry` | window position | screenshot
- screenshot | 142-175 | ImageMagick `import -window`, `identify` | capture a PNG | scenario.run, fred.selection, CLI
- _focus | 178-180 | `xdotool windowfocus` | give focus | keys
- keys | 183-203 | xdotool key/keydown/keyup (XTEST) | press keys | record._issue, scenario._do
- _presses / click | 206-238 | xdotool mousedown, sleep, mouseup | held clicks | record._issue
- drag | 241-261 | xdotool mousedown/mouseup | drag | fred.select, record._issue
- move | 264-282 | `xdotool getmouselocation` | hover | record._issue
- focus_neutral | 285-288 | `xdotool click 1` | click a neutral spot | fred._focus
- scroll | 291-302 | xdotool click 4/5 | mouse wheel | record._issue

**sound.py** (the whole module is PulseAudio/PipeWire)
- _runtime_dir | 57-59 | XDG_RUNTIME_DIR, os.getuid, /run/user/<uid> | where the Pulse socket is | server_address, sound_lock
- server_address | 62-69 | `unix:` Pulse socket path | server address | _pactl, engine_env
- _pactl / _pactl_json | 87-120 | `pactl` subprocess | talk to the sound server | everything below
- sound_lock | 123-131 | flock via session.file_lock | serialise module load/unload | create_sink, remove_sink
- create_sink | 134-144 | `pactl load-module module-null-sink` | per-session sink | launcher._prepare
- write_device_prefs | 147-160 | portable file write (Pulse device names) | name the devices in fs2_open.ini | launcher._prepare
- _openal_lib | 163-183 | `ldd`, libopenal.so | find the engine's OpenAL | check_device_names
- check_device_names | 186-217 | child `python -I openal_names.py` (ctypes.CDLL of the .so), HOME/XDG_CONFIG_HOME set to a temp folder | check device names | launcher._prepare
- _null_sinks | 220-232 | `pactl list short modules` | list sinks | remove_sink, orphan_sinks
- remove_sink | 235-254 | `pactl unload-module` | unload a sink | launcher._prepare (error path), stop, _cleanup_sinks
- engine_env | 257-266 | PULSE_SERVER, PULSE_SINK, ALSOFT_DRIVERS=pulse, PULSE_PROP | route the engine's audio | runner.main, check_device_names
- _sinks / streams_of / verify_routing | 269-305 | `pactl list sink-inputs` (application.process.id) | check streams | launcher.launch
- _recorded_sinks / orphan_sinks | 308-329 | pactl sources/source-outputs | cleanup | launcher._cleanup_sinks
- openal_names.main | 33-39 | ctypes.CDLL of a .so | list devices | child process only

**crash.py**
- _CORE_SIGNALS | 26-27 | POSIX signal names | which signals dump core | _find_core, _end_hints
- _coredumps_json | 33-38 | `coredumpctl --json=short list` | find dumps | _find_core
- parse_coredumpctl | 58-79 | coredumpctl JSON, signal.Signals | match the dump | _find_core
- _backtrace | 82-106 | `coredumpctl debug` + gdb (`-nx -iex 'set auto-load off'`), DEBUGINFOD_URLS | backtrace | report
- _find_core | 127-143 | polls coredumpctl | wait for the core | report
- _end_hints | 146-172 | journalctl hint text, scope.is_gone, systemd-coredump wording | explain the end | report
- report | 175-199 | launcher.alive | build the report | CLI only

**sandbox.py** (the whole module is Linux-only)
- constants | 66-74, 124-146 | bwrap, prlimit, keyctl; /tmp, /usr, /lib64 links, /etc/ld.so.cache, /proc, /dev, /dev/null over /proc/keys | sandbox layout | argv, _binds
- seccomp_filter | 149-174 | x86_64 seccomp BPF | block syscalls | runner._popen_engine
- x_socket | 177-189 | /tmp/.X11-unix/XN | bind the X socket | _binds
- mod_targets | 192-207 | resolves symlinks | bind mod targets | _binds
- _binds | 210-231 | bwrap --tmpfs/--ro-bind/--bind | mounts | argv
- write_identity | 234-244 | os.getuid/getgid, /etc/passwd format, /sbin/nologin | identity files | runner.main
- argv | 247-270 | prlimit --fsize/--nofile, `keyctl session -`, bwrap | sandbox prefix | runner.engine_argv
- sandbox_env | 273-285 | HOME/XDG/DBUS paths inside the sandbox | env rewrite | runner.main
- read_info | 288-310 | select() on a pipe fd | bwrap's --info-fd | runner._popen_engine
- _children | 313-318 | /proc/<pid>/task/<pid>/children | find the engine | find_engine
- _open_pidfd / _close_fd | 321-328 | os.pidfd_open | process handle | find_engine, runner._popen_engine
- find_engine | 331-359 | pidfd | engine host pid | runner._popen_engine
- exit_code | 362-373 | 128+N signal convention | translate exit status | Engine.wait
- Engine | 376-440 | signal.pidfd_send_signal SIGTERM/SIGKILL | process adapter | runner
- free_bytes | 502-506 | os.statvfs | free disk | launcher._check_sandbox, runner._run_engine, Watchdog.check
- warn_loudly | 509-516 | `logger -p user.warning` (journal) | warnings | launcher._warn_shared_sandbox_disk, Watchdog
- Watchdog | 519-621 | pthread_sigmask, statvfs, safefs | size watchdog | runner._watch

**paths.py**
- runtime_root | 17-30 | XDG_RUNTIME_DIR (/run/user tmpfs), chmod 0700 | runtime root | session, launcher, match, sound.root_tag, fred._stage, docs, frame_stats, record, build
- state_dir | 33-48 | XDG_STATE_HOME or ~/.local/state, chmod 0700 | audit log, locks | session.audit, match.audit, bridge_install._lock_path
- sandbox_root | 51-69 | XDG_CACHE_HOME or ~/.cache, chmod 0700 | sandbox prefs | session.new_session; launcher._engine_roots, _check_sandbox, _warn_shared_sandbox_disk, _cleanup_sandbox_orphans

**runner.py**
- _on_sigterm | 37-45 | SIGTERM handler | record how the run ended | main
- _block_sigterm | 48-58 | signal.pthread_sigmask | protect the exit record | _run_engine, main
- _read_display | 61-80 | select() on a pipe | read Xvfb's display number | _start_xvfb
- _start_xvfb | 83-103 | `Xvfb -displayfd`, os.pipe, pass_fds | virtual display | main
- _signal_name | 106-117 | negative return code means a signal | record the signal | _record_exit
- _memory_events_path / _oom_kills | 120-136 | /proc/self/cgroup, /sys/fs/cgroup/.../memory.events | detect OOM | _record_exit, _termination_cause
- _termination_cause | 162-176 | OOM via cgroup, systemd stop | why the run ended | _run_engine, main
- _reap | 179-196 | Popen.terminate (SIGTERM), then kill | stop the engine | _run_engine
- engine_argv | 199-219 | nsenter / bwrap / PRIVATE_NET_PREFIX (unshare) | network wrapper | _popen_engine
- _popen_engine | 222-271 | os.pipe, pass_fds, pidfd, bwrap info-fd | start the engine | _run_engine
- _run_engine | 274-325 | statvfs baseline, _block_sigterm | supervise the engine | main
- make_first_oom_victim | 353-364 | /proc/self/oom_score_adj=1000 | OOM priority | main
- main | 367-419 | signal.signal, Xvfb, run/xdg-runtime mode 0700 | entry point | `python -m fsoctl.runner`

**match.py**
- HOLDER_ARGV | 39-40 | `unshare --user --map-root-user --net sh -c "ip link set lo up && exec sleep infinity"` | namespace holder | create
- pid_start | 75-87 | /proc/<pid>/stat field 22 | stable process handle | alive, create
- alive | 124-134 | scope.is_active + pid_start | liveness | launcher (_check_match_room, _network_state, _cleanup_locked), observe, nsenter_prefix, CLI
- _abandoned | 158-168 | scope.is_gone | expire old real matches | observe
- nsenter_prefix | 171-192 | `nsenter --preserve-credentials --user --net --target` | join the namespace | runner.engine_argv, run_inside
- run_inside | 195-198 | subprocess with nsenter | run a command inside | launcher.engine_holds, _wait_loopback
- _wait_loopback | 201-211 | `ip -brief link show lo` | wait for loopback | create
- create | 214-271 | os.getuid, flock, scope.caps/start | create a match | CLI
- end_holder | 283-291 | scope.stop | end the holder | launcher.remove_match, create

**launcher.py**
- PRIVATE_NET_PREFIX | 676-677 | `unshare ... sh -c 'ip link set lo up && exec "$@"'` | private network | runner.engine_argv
- _qtfred_mission_path | 114-123 | `startswith("/")` absolute-path test | mission path | _qtfred_argv
- port_free | 187-224 | socket IPV6_V6ONLY, SO_REUSEADDR (Windows SO_REUSEADDR semantics differ) | test bind | host_port, web_port
- host_port, web_port, assign_port, default_connect | 241-389 | scope.is_gone | port allocation | _prepare, _web_fields
- _scope_active_cached | 400-410 | systemctl (cached) | liveness | alive
- alive | 413-426 | **os.kill(pid, 0)** (on Windows this does not probe: it sends CTRL_C_EVENT or kills) | engine liveness | crash.report, game, quit, status, list_all, require_running, bridge_info
- _is_live / _alive_or_starting | 429-438 | scope.is_active | readiness | _wait_ready
- check_capacity | 441-458 | scope.is_gone, room_for_engine | session cap | _start
- _pilot_files / _run_folder_pilot_files | 461-499 | safefs (O_NOFOLLOW, dir_fd) | copy a pilot | _copy_pilot
- _check_binary | 529-534 | os.access X_OK | binary check | _validate
- _check_folders | 562-576 | `"/"` separator test | mod names | _validate
- _check_role | 579-621 | **os.getuid()** (597) | refuse root | _validate
- _check_network | 693-712 | which unshare/ip, os.geteuid | private-network checks | _validate
- _check_sandbox | 715-757 | which bwrap/prlimit/keyctl, geteuid, statvfs | sandbox checks | _check_network
- _warn_shared_sandbox_disk | 760-781 | st_dev, logger | warning | _check_sandbox
- _prepare | 862-937 | bridge_install (systemctl, flock), webui mkdir mode 0700, sound.* (pactl, ldd) | create the session | _start
- _wait_window | 940-957 | xwin.main_window (xdotool) | qtFRED readiness | _wait_ready
- _start | 995-1023 | scope.caps (/proc), flock, scope.start (systemd-run) | start the runner | launch, run_to_exit
- engine_holds | 1039-1064 | `ss -H -lunp/-ltnp`, nsenter | who owns the socket | listening, _check_game_port, web_panel_up
- _early_exit_error | 1152-1176 | matches "unshare:", "nsenter:", "ip:" in the log | error message | _wait_ready
- launch | 1200-1250 | renderer.read_maps (/proc), sound.verify_routing | main verb | CLI, compare, docs, fred, frame_stats, record, validate
- run_to_exit | 1253-1272 | scope.is_active | -pofspew runs | model_info
- _stop_scope_only | 1344-1357 | scope.stop | stop without a run folder | stop
- _cgroup_has_scope / _in_scope | 1360-1374 | /proc/<pid>/cgroup | check the pid is in the scope | _stop_runner_first
- _gone | 1377-1383 | /proc/<pid>/stat zombie check | runner exit | _stop_runner_first
- _stop_runner_first | 1386-1417 | **os.kill(pid, SIGTERM)** | stop the runner first | stop
- stop | 1420-1464 | scope.stop, sound.remove_sink | stop a session | many callers
- remove_match / cleanup | 1472-1599 | flock, match.end_holder | | match CLI, CLI
- _cleanup_side_folders | 1529-1561 | is_symlink | cleanup | _cleanup_locked
- _cleanup_sandbox_orphans | 1564-1586 | scope.is_gone, safefs.remove_tree | cleanup | _cleanup_locked
- _cleanup_sinks | 1602-1626 | pactl | cleanup | _cleanup_locked
- _cleanup_locked | 1665-1704 | safefs.remove_tree (O_PATH, /proc/self/fd) | cleanup | cleanup, _auto_cleanup

**session.py**
- scope_name | 105-107 | systemd unit naming | | new_session, launcher
- new_session | 110-130 | os.mkdir mode 0700 | | launcher._prepare
- file_lock | 141-163 | fcntl.flock | lock | launcher, match.create, bridge_install, sound
- update | 166-185 | fcntl.flock | read-modify-write | runner, launcher, sandbox
- next_seq | 206-221 | fcntl.flock | sequence numbers | channel.send
- window_owner | 88-91 | `_NET_WM_PID` idea (X11) | which pid owns the window | launcher._wait_window, xinput.window_id

**safefs.py** (the whole module is POSIX)
- Flags at 34-36 use O_DIRECTORY, O_NOFOLLOW, O_CLOEXEC, O_NONBLOCK. Every function opens through `dir_fd`. `_chmod_private` (349-357) uses O_PATH plus chmod of `/proc/self/fd/N`.
- Callers: channel, logs, record, game, model_info, launcher, sandbox.

**Other modules**
- renderer.py: read_maps (34-40) reads /proc/<pid>/maps, called by launcher.launch. classify (17-31) matches Linux .so names.
- shadowdir.py: make (23-34) uses symlink_to, called by docs, frame_stats, record._replay_cwd. collect (37-39) uses is_symlink.
- bridge_install.py: install (96-122) uses scope.active_units and flock, called by launcher._prepare.
- build.py: _build_lock (102-116) uses flock. build (140-181) uses `systemd-run --user --scope` with `cmake`. _result (119-137) treats -9/137 as SIGKILL (OOM).
- docs.py: cache_dir (85-98) uses XDG_CACHE_HOME or ~/.cache. _key_lock (201-224) uses flock. Line 177 calls shadowdir.make.
- compare.py: diff_images (100-107) runs `magick compare`.
- fred.py:
  - open (153-154) runs xdotool windowmove/windowsize.
  - _visible_titles (164-166) and _dialog_id (315-318) use xwin.windows.
  - save (190-191) runs xdotool windowfocus and `key ctrl+s`.
  - ocr (217-224) runs `tesseract`.
  - _close_dialog (323-324) runs xdotool Escape.
  - check (360) runs `import -window -resize 200%`.
- cli.py: _on_sigterm (63-72) and main (139-146) install a SIGTERM handler.
- lua_check.py: _loaded_paths (274) and line 309 use `startswith("/")` to spot absolute paths.
- mcp/server.py: _exec (86-106) calls proc.terminate() for a SIGTERM grace period, then kill. On Windows terminate is already a hard kill, so fsoctl's cleanup would be skipped.
- register.sh and setup.sh: bash, `.venv/bin/python` and `.venv/bin/pip` (the POSIX venv layout), `python3`.

## 2. Public signatures imported by other modules
- scope:
  - `def caps(runtime_sec: int = 1800, cpu_cores: int = 8, tasks: int = 512, mem_kb: int | None = None) -> list[str]:`
  - `def start(unit: str, argv: list[str], env: dict, log_path: Path, props: list[str]) -> subprocess.Popen:`
  - `def is_active(unit: str) -> bool:`
  - `def active_units() -> list[str]:`
  - `def is_gone(unit: str) -> bool:`
  - `def stop(unit: str, timeout: float = POLITE_STOP_SEC) -> None:`
  - `def room_for_engine() -> str | None:`
- envsetup:
  - `def child_env(base: dict, display: str | None, prefs: Path, renderer: str, run: Path) -> dict:`
  - `def for_kind(env: dict, kind: str) -> dict:`
- xwin:
  - `def windows(display: str, pid: int) -> list[tuple[str, str]]:`
  - `def main_window(display: str, pid: int, kind: str, mission: str | None) -> str | None:`
- xinput:
  - `def _x(sess: session.Session, *args: str) -> str:` (private, but fred uses it)
  - `def window_id(sess: session.Session) -> str:`
  - `def new_shot_path(shots: Path, prefix: str) -> Path:`
  - `def screenshot(sid: str, crop: str | None = None, scale: float | None = None, full: bool = False) -> dict:`
  - `def keys(sid: str, names: list[str], hold_ms: int = 0) -> dict:`
  - `def click(sid: str, x: int, y: int, button: int = 1, double: bool = False) -> dict:`
  - `def drag(sid: str, start: tuple[int, int], end: tuple[int, int], button: int = 1) -> dict:`
  - `def move(sid: str, x: int, y: int) -> dict:`
  - `def focus_neutral(sess: session.Session, x: int, y: int) -> None:`
  - `def scroll(sid: str, x: int, y: int, clicks: int) -> dict:`
- sound:
  - `class PactlMissing(HarnessError):`
  - `def create_sink(sid: str) -> int:`
  - `def write_device_prefs(prefs: Path, sid: str) -> None:`
  - `def check_device_names(binary: str, sid: str) -> None:`
  - `def remove_sink(module: int, sid: str) -> str | None:`
  - `def engine_env(sid: str) -> dict:`
  - `def verify_routing(pid: int, sid: str) -> dict:`
  - `def orphan_sinks(live_ids: set[str]) -> tuple[list[tuple[int, str]], list[str]]:`
- crash: no module imports it; `def report(sid: str) -> dict:` is reached only through the CLI.
- sandbox:
  - `def seccomp_filter() -> bytes:`
  - `def write_identity(run: Path) -> None:`
  - `def argv(sess: session.Session, env: dict, info_fd: int, seccomp_fd: int) -> list[str]:`
  - `def sandbox_env(env: dict) -> dict:`
  - `def read_info(rfd: int, timeout: float) -> int | None:`
  - `def _open_pidfd(pid: int) -> int:`
  - `def find_engine(init_pid: int, running: Callable[[], bool], timeout: float) -> tuple[int, int] | None:`
  - `class Engine:` with `__init__(self, proc, pid: int | None, pidfd: int | None, bwrap_pidfd: int | None = None)`
  - `def free_bytes(folder: Path) -> int:`
  - `def free_min_bytes() -> int:`
  - `def warn_loudly(text: str) -> None:`
  - `class Watchdog(threading.Thread):` with `__init__(self, sid: str, prefs: Path, engine: "Engine", baseline: int | None = None)`
  - Constants: BWRAP, PRLIMIT, KEYCTL, ENGINE_NS_PID, START_TIMEOUT, WATCHDOG_JOIN
- paths:
  - `def runtime_root() -> Path:`
  - `def state_dir() -> Path:`
  - `def sandbox_root() -> Path:`
  - `def is_session_id(name: str) -> bool:`
  - `def session_dir(sid: str) -> Path:`
  - `def matches_root() -> Path:`
  - `def match_dir(mid: str) -> Path:`
  - `def fs2path() -> Path:`
  - `def bridge_source() -> Path:`
  - `def channel_dir(prefs: Path) -> Path:`
  - Constants: BRIDGE_MOD, CHANNEL_REL
- runner: no module imports it; it only runs as a process. Tests import it.
- match:
  - `def load(mid: str) -> Match:`
  - `def save(m: Match) -> None:`
  - `def list_matches() -> list[Match]:`
  - `def alive(m: Match) -> bool:`
  - `def observe(m: Match) -> Match:`
  - `def nsenter_prefix(m: Match) -> list[str]:`
  - `def run_inside(m: Match, argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess:`
  - `def end_holder(m: Match, kind: str = "match_remove", text: str | None = None) -> None:`
  - Constant: HOLDER_RUNTIME_SEC
- launcher:
  - `class LaunchRequest` (a dataclass)
  - `def launch(req: LaunchRequest) -> dict:`
  - `def stop(sid: str) -> dict:`
  - `def quit(sid: str, timeout: float = 30.0) -> dict:`
  - `def run_to_exit(req: LaunchRequest, timeout: float) -> session.Session:`
  - `def require_running(sid: str) -> session.Session:`
  - `def alive(sess: session.Session) -> bool:`
  - `def build_argv(sess: session.Session) -> list[str]:`
  - `def remove_match(mid: str) -> dict:`
  - `def sessions_in_match(mid: str) -> list[session.Session]:`
  - Constants: PRIVATE_NET_PREFIX, _STARTING_GRACE

## 3. runner.py
Module docstring: "The process that runs INSIDE a session's scope: Xvfb, then the engine or qtFRED. WHY a runner process: the scope's main process must outlive `fsoctl launch` (which returns as soon as the session is ready) and must own both Xvfb and the engine, so stopping the scope ends both. It also records how the engine exited, which crash_report needs. Started by launcher.launch as `python -m fsoctl.runner <session id>` inside the named scope."

- **Who starts it:** launcher._start runs `[sys.executable, "-m", "fsoctl.runner", sid]` through scope.start (systemd-run), with PYTHONPATH set by _runner_env.
- **What it supervises:** Xvfb (except for a standalone), then the engine as a Popen, wrapped by unshare, nsenter or bwrap depending on the network. For sandbox sessions it also runs the Watchdog thread.
- **How long it lives:** until the engine exits, or until runtime_sec. The scope's RuntimeMaxSec is a backstop 60 s later.
- **How it is stopped:**
  - launcher._stop_runner_first sends SIGTERM to the runner pid after checking its /proc cgroup.
  - The runner's handler raises _Terminated and _reap passes SIGTERM to the engine, then SIGKILL after 20 s.
  - Xvfb is terminated last.
  - scope.stop (systemctl) is the backstop.
  - It writes ended_by and exit_info into session.json.

## 4. launch sequence
1. cli.main installs the SIGTERM handler — PORTABLE (Windows semantics differ)
2. _cli_launch: _parse_res, _parse_pilot, _default_binary, LaunchRequest — PORTABLE
3. launch → _start: scope.caps (reads /proc/meminfo) — LINUX-ONLY
4. session.file_lock(launch.lock) (flock) — LINUX-ONLY (POSIX)
5. _auto_cleanup → _cleanup_locked (scope.is_gone, safefs, pactl) — LINUX-ONLY
6. check_capacity (scope.is_gone, room_for_engine) — LINUX-ONLY
7. _prepare → _validate:
   - _check_binary — PORTABLE (X_OK)
   - _check_folders — PORTABLE (assumes "/" separators)
   - _check_extra_args, _check_mission_name — PORTABLE
   - _check_role (os.getuid) — LINUX-ONLY
   - _check_network / _check_sandbox — LINUX-ONLY
8. paths.fs2path — PORTABLE
9. bridge_install.install (flock, systemctl list-units) — LINUX-ONLY
10. session.new_session, with assign_port/host_port/default_connect (scope.is_gone, port_free) and _web_fields — mixed: LINUX-ONLY via scope
11. Write switches.txt and allow_eval, multi.cfg, _copy_pilot — PORTABLE
12. sound.create_sink, write_device_prefs, check_device_names (pactl, ldd) — LINUX-ONLY
13. scope.start(runner) (systemd-run) — LINUX-ONLY
14. Inside the runner, main: oom_score_adj, _start_xvfb, child_env, engine_argv, _popen_engine — LINUX-ONLY
15. _wait_ready, which is one of:
    - Channel.wait_ready (heartbeat file) — PORTABLE logic, but safefs is POSIX
    - _wait_window (xdotool) — LINUX-ONLY
    - _wait_listening (`ss`/nsenter) — LINUX-ONLY
16. renderer.classify(read_maps) (/proc/<pid>/maps) — LINUX-ONLY
17. Vulkan check — PORTABLE
18. sound.verify_routing — LINUX-ONLY
19. _warnings — PORTABLE
20. _check_game_port and web_panel_up (`ss`) — LINUX-ONLY
21. Return the dict — the session is ready.

## 5. Isolation (envsetup.child_env, lines 38-74)
- **Removed:** WAYLAND_DISPLAY, SESSION_MANAGER, XAUTHORITY, XDG_CURRENT_DESKTOP, KDE_FULL_SESSION, DESKTOP_SESSION, XDG_SESSION_DESKTOP.
- **Set:**
  - DISPLAY=":N"
  - SDL_VIDEODRIVER=x11, GDK_BACKEND=x11, QT_QPA_PLATFORM=xcb
  - SDL_VIDEO_ALLOW_SCREENSAVER=1
  - FSO_PREFERENCES_PATH=<prefs>/ (the private preferences folder)
  - HOME=run/home
  - XDG_CONFIG_HOME, XDG_DATA_HOME, XDG_STATE_HOME under that home
  - XDG_CACHE_HOME=<real home>/.cache/fso-harness/cache
  - XDG_RUNTIME_DIR=run/xdg-runtime
  - DBUS_SESSION_BUS_ADDRESS=unix:path=run/no-bus
  - LIBGL_ALWAYS_SOFTWARE=1 for OpenGL
- DISPLAY is removed when display is None (standalone).
- Where the prefs folder lives: `run/prefs`, or for sandbox sessions `<sandbox_root>/<id>/prefs` (session.prefs_dir). Sandbox sessions get sandbox_env, which re-points every one of these variables inside `/fso-harness`.

The reasons, quoted from the docstring:
- "SDL draws on the virtual display only with DISPLAY + SDL_VIDEODRIVER=x11; FSO's error boxes are zenity windows, which reached the user's Wayland desktop through the default wayland-0 socket until GDK_BACKEND/QT_QPA_PLATFORM were pinned"
- "Xvfb has no hardware GL, so OpenGL runs force Mesa's software renderer explicitly"
- "HOME/XDG_*: FSO's legacy config mode uses ~/.fs2_open when ~/.fs2_open/fs2_open.ini exists, and qtFRED autosaves missions and writes its settings under the XDG data/config folders, all of which would land in the user's real home"
- "Removing DBUS_SESSION_BUS_ADDRESS was NOT enough: libdbus then connects to $XDG_RUNTIME_DIR/bus ... So the child gets a private XDG_RUNTIME_DIR inside its run folder and a bus address that never exists"
- "SESSION_MANAGER is how X11 programs join the desktop's logout handshake (XSMP)"
- "XDG_CACHE_HOME stays persistent ... so Mesa's shader cache survives between runs"

## 6. Tests that stub Linux facilities
- **conftest.py:**
  - no_real_pulse: `sound._pactl`, `sound.check_device_names`, `sound._runtime_dir`
  - roomy_machine: `scope._mem_available_kb`
  - no_host_ports: `launcher.port_free`, `launcher._GAME_PORT_WAIT`
  - fresh_scope_cache clears `launcher._scope_seen`
  - tmp_runtime sets the FSO_HARNESS_RUNTIME, STATE, SANDBOX_ROOT and SANDBOX_FREE_MIN env vars
- test_b_scope.py: scope.subprocess.run, scope.state, _KILL_WAIT
- test_scope.py: scope._meminfo_kb, _mem_available_kb, scope.open
- test_b_launcher.py: scope.start/is_active/state/stop/active_units, launcher.os.kill, _in_scope, _gone, renderer.read_maps, xwin.main_window
- test_launcher_unit.py: launcher.scope.* (including _mem_available_kb and room_for_engine), launcher.os.geteuid, os.getuid, launcher.shutil.which, match.run_inside/alive/end_holder, session.file_lock
- test_b_runner.py: runner._start_xvfb, _memory_events_path, _oom_kills, _block_sigterm, runner.subprocess.Popen, match.nsenter_prefix
- test_b_crash.py / test_crash_unit.py: crash._coredumps_json, _backtrace, _find_core, crash.subprocess.run, scope.state
- test_match_unit.py / test_match_real.py: scope.*, match.run_inside, os.getuid, session.file_lock
- test_host_ports.py: launcher.socket.socket, launcher.subprocess.run, engine_holds, port_free, match.run_inside
- test_sound.py: sound._pactl, sound.subprocess.run, sound.os.getuid, _runtime_dir, openal_names.ctypes.CDLL, runner._start_xvfb, scope.*
- test_sandbox.py, test_sandbox_round3.py, test_sandbox_round3b.py, test_sandbox_round3c.py, test_sandbox_mutation3e.py: sandbox._children, _open_pidfd, _close_fd, sandbox.os.kill, sandbox.signal.pidfd_send_signal, sandbox.subprocess.run, warn_loudly, runner.sandbox.read_info/find_engine, runner.subprocess.Popen, launcher.shutil.which, launcher.os.geteuid, safefs._chmod_private, safefs.os.chmod
- test_safefs.py: safefs._DIR_FLAGS, _READ_FLAGS, _NEW_FLAGS, safefs.os.fstat
- test_d_build.py / test_d_cross.py: build.subprocess.run, scope.caps/is_active/stop, xinput._x/_move/window_id
- test_d_fred.py / test_d_small.py: fred.ocr, _window, _dialog_id, xinput._x, screenshot, window_id
- test_xwin.py: xwin.windows
- test_bridge_install.py / test_b_bridge_install.py: scope.active_units
- test_network_default.py, test_prepare_ports.py, test_web_panel.py: launcher.scope.is_gone and related scope calls
- No stubs: test_xinput_unit.py and test_envsetup.py test pure functions only. test_b_envleak.py reads the real /proc/<pid>/environ and runs `busctl`.

## 7. Already platform-neutral (or close)
- channel.py: the file protocol (cmd/res/ready.json) is neutral. But every access goes through safefs (O_NOFOLLOW, dir_fd, which is POSIX), and next_seq uses flock.
- session.py records: JSON plus os.replace are neutral. The flock locks and scope naming are not.
- record.py journal: neutral, except _replay_cwd, which uses shadowdir symlinks.
- game.py (lua, sexp, pilot, wait_for): neutral, apart from launcher.alive and safefs log reads.
- logs.py: neutral parsing, but reads through safefs.
- launcher.build_argv, _multi_argv, multi_cfg_text, _check_* validators, web_login: neutral.
- port_free: uses the standard socket API, so neutral (but SO_REUSEADDR behaves differently on Windows).
- vp.py, scratch.py, guide.py, errors.py, model_info parsing, frame_stats parsing, compare.diff_states, lua_check: neutral, except lua_check's "/" absolute-path test.
- renderer.classify: neutral regex, but the library names are Linux .so names.
- mcp/server.py: neutral, except the terminate/kill grace period and the venv paths in the scripts.
