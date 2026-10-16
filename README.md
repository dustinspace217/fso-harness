# fso-harness

A headless test harness for the [FreeSpace Open](https://github.com/scp-fs2open/fs2open.github.com)
engine and qtFRED, built so an AI agent (or a person with a terminal) can build an FSO change,
launch it on a mission, skip any startup stage, look at the screen, read the game state, fly the
ship or hand it to the AI, speed time up, wait for mission events, compare two builds or two
renderers, validate a mod, read a crash, and drive the mission editor, all on a private virtual
display that never touches your desktop, inside resource limits.

It has two faces that do the same things:

- `fsoctl`, a command-line tool (Python standard library only), and
- an MCP server (`mcp/server.py`) exposing every `fsoctl` command as a tool.

Linux only. License: [Unlicense](LICENSE), matching FSO's newer code.

## How it works

```
agent (MCP client)  or  you (shell)
   -> mcp/server.py (thin, stateless)  ->  fsoctl --json <verb>
        -> per session: a systemd --user scope "fso-harness-<id>" holding
             Xvfb (its own display) + the engine or qtFRED (private preferences folder)
        -> run folder $XDG_RUNTIME_DIR/fso-harness/<id>/ : session.json, logs, shots, journal
   <- bridge mod "fso_harness_bridge" (Lua, first in -mod) inside the engine, answering commands
```

- No daemon. Every call reads the session from disk, so any client can pick up any session.
- The bridge mod is copied into your game folder as `fso_harness_bridge/` and refreshed when its
  contents differ from the harness's copy (refused, with the ids, while another session is
  running). Nothing else in your game folder is written, except scratch mods you create on
  purpose (`fso_harness_scratch_<id>/`) and files you extract with `vp extract`.
- Your own pilots, settings and missions are never used: each session has its own preferences
  folder and its own `HOME`/`XDG_*` folders inside its run folder (FSO's legacy `~/.fs2_open`
  mode and qtFRED's autosave/settings files otherwise land in your home). qtFRED only ever opens
  a copy of a mission (`fred_open`); `launch --qtfred` refuses a mission outside that copy folder.
- Sessions get a private runtime folder and no D-Bus session-bus or session-manager address (the
  routes by which a game or editor can hold off screen locking or object to a logout). Checked on
  this machine: neither the engine nor qtFRED shows up on the desktop's session bus
  (`tests/test_b_envleak.py`).
- Every engine, qtFRED and build process runs in a scope with memory, CPU, task and runtime caps.
  At most 3 sessions run at once (`FSO_HARNESS_MAX_SESSIONS`), counting sessions still starting.
- `lua`, `post_event` and sexps that use the engine's built-in Lua-running operators need a
  session launched with `--allow-eval`; each is written to `~/.local/state/fso-harness/audit.log`.
  A Lua operator defined by a mod is not recognised by the gate. The gate covers what the harness sends. It
  does not stop Lua that a mod or a mission runs on its own (that is what you are testing).

## Requirements

- Linux with a systemd user session, Python 3.12+
- `Xvfb`, `xdotool`, ImageMagick (`import`, `identify`, `magick compare`), `tesseract`,
  `gdb` + `coredumpctl` (crash reports), `cmake` (builds)
- An FSO build. For Vulkan: built with `-DFSO_BUILD_WITH_VULKAN=ON` (needs `libshaderc-devel`) and
  a GPU Vulkan driver; point `VK_ICD_FILENAMES` at its ICD file (the harness never picks one).
- qtFRED for the editor tools. A debug engine build (`FSO_BIN_DEBUG`) for full validation and logs.

## Setup

```
pip install -e .                 # or: uv tool install .   -> the fsoctl command
export FS2PATH=/path/to/FS2      # the folder with root_fs2.vp
export FSO_BIN=/path/to/fs2_open_26_1_0_x64_AVX2
export FSO_BIN_DEBUG=/path/to/fs2_open_26_1_0_x64_AVX2-DEBUG   # optional, recommended
export QTFRED_BIN=/path/to/qtfred_26_1_0_x64_AVX2-DEBUG        # optional, for fred_* tools

bash mcp/setup.sh                # MCP server venv (mcp==2.0.0)
bash mcp/register.sh             # registers it with Claude Code at user scope (your choice);
                                 # stores the four variables above in the registration, because
                                 # Claude Code does not pass your shell's environment to the server
```

### Guidance the agent sees

- The MCP server sends **instructions** that Claude Code loads at session start, built around the
  thing agents most easily miss: a mission is tested fastest by script (`sexp`, `lua`, `wait_for`),
  not by flying it.
- The guide's recipes are MCP **prompts**, usable as commands: `/fso-harness:playtest_mission`,
  `test_engine_change`, `check_mod`, `compare_builds`, `read_crash`, `fly_mission`.
- Every other guide section is one call away: `guide` lists the sections, `guide fly` returns
  "Fly a mission" (the instructions' 2,048-character budget is full).
- `skills/fso-harness/SKILL.md` is the same guidance as an Agent Skill for tools that load skills
  from files (Claude Code: copy the folder to `~/.claude/skills/`; Codex and others read the same
  format).

## Quick start

```
fsoctl --json launch --mods MVPS-4.7.3 --mission SM1-01.fs2 \
    --pilot create:Tester --skip-movies --dismiss-popups --skip-command-briefing --skip-briefing
fsoctl --json wait_for <id> state GS_STATE_GAME_PLAY
fsoctl --json state <id>
fsoctl --json screenshot <id>
fsoctl --json pilot <id> --pitch 0.5 --forward 1 --primary --seconds 2
fsoctl --json stop <id>
```

Every command takes `--json` and exits with a code from `fsoctl/errors.py`
(0 ok, 2 usage, 3 not found, 4 launch, 5 timeout, 6 engine not running, 7 refused, 8 Lua error
inside the bridge or the code it ran, 9 the verb ran but its work failed, e.g. a compile). Errors
carry a `message` and a `hint` saying what to try next. A finding is not a failure: `validate`
that finds problems, or a mission that crashed the engine during validation, still exits 0 and
reports them.

## Commands

| | |
|---|---|
| **Sessions** | `launch`, `status`, `stop`, `list`, `cleanup` |
| **Startup stages** (launch flags, each independent) | `--skip-movies`, `--pilot chooser\|existing:NAME\|create:NAME`, `--dismiss-popups`, `--mission` (skips the main hall), `--skip-command-briefing`, `--skip-briefing` |
| **Look** | `screenshot` (crop/scale), `log` (tail, grep, problems: warnings/errors/asserts/Lua errors), `state` (game state, mission time, player, ships with position/forward vector/hull/shields/target, goals, events) |
| **Act** | `sexp` (eval / number / run), `pilot` (axes and fire for N mission seconds), `autopilot on\|off`, `time` (compression %, pause, resume), `keys`, `click`, `drag`, `scroll`, `answer_popup`, `lua` and `post_event` (only with `--allow-eval`, audited) |
| **Wait** | `wait_for` state / mission_time / event_true / event_false / goal / destroyed / departed / log regex (always bounded) |
| **Record** | `journal`, `replay` (same commands, not guaranteed same results) |
| **Make** | `build` (cmake --build in a scope, parsed compiler errors), `scratch_mod create\|list\|remove`, `vp list\|extract` |
| **qtFRED** | `fred_open` (opens a copy of the mission), `fred_save`, `fred_camera`, `fred_select`, `fred_selection`, `fred_check` (pre-save error check, read by OCR) |
| **Analyse** | `validate`, `crash_report`, `api_docs` (this build's sexp + Lua reference), `model_info` (-pofspew), `frame_stats` (engine profiler), `compare` (two builds), `renderer_compare` (OpenGL vs Vulkan) |
| **Read** | `guide` (a section of docs/AGENT-GUIDE.md, or the list of sections) |

`fsoctl <verb> --help` lists every option. Scenario files for `compare` are JSON lists of steps,
see `fsoctl/scenario.py`.

`lua` checks the code before sending it whenever `api_docs` is cached for the build and its mod
list: unknown library functions, colon calls on the no-self `control_info` functions, and any
capitalised field or method that no class or library in the build's API has are refused (exit 7)
instead of aborting the engine. It cannot see handle types; the reply's `lua_check` says whether
the check ran.

## Example: a scripted pilot

`examples/pilot/` is a fighter pilot written in the engine's Lua, flying with player controls
only, plus a drill that measures it. It has escort, strike, recon and stealth modes, and its
README records what was measured with each and how often. With it an agent finished FSPort's
"Playing Judas" (a stealth recon mission) on Medium with every goal met and without being
detected. That took 13 iterations, each fixing one failure it observed (2026-10-15).

## Multiplayer

A multiplayer game runs inside a *match*: one private, loopback-only network held open by a
sleeping holder process in its own systemd scope (`unshare --user --map-root-user --net`, no root
anywhere). The engine binds its UDP game port on every interface and has no bind-address option,
and the standalone server opens a web panel on TCP 8080, so containment comes from the network
itself: inside a match those sockets exist, on the real network they never do, and a packet sent
out of the match fails with "network unreachable" (kernel-enforced; probed and tested on every
run). The online tracker is never enabled by the harness.

```
fsoctl match create                      # -> {"id": "m1a2b3c4d", "holder_pid": ...}
fsoctl launch --match m1a2b3c4d --role standalone
fsoctl launch --match m1a2b3c4d --role client --pilot create:Alpha --mission M-01.fs2 \
       --skip-movies --dismiss-popups --skip-briefing
fsoctl match list                        # matches, liveness, the sessions in each
fsoctl match remove m1a2b3c4d            # stops the sessions first, then the network
```

Roles: `standalone` (`-standalone`, no window, no pilot, no mission; readiness is its listening
socket), `host` (`-startgame`, a player who hosts), `client` (`-connect`, auto-joins; the first
client of a standalone game becomes the host). Every engine process binds its own port, so the
sessions in a match get 7808, 7809, ... A `--mission` on a host or client becomes the engine's
`-almission`, which the host player consumes to start that mission with no human; the bridge
posts the join-screen, start-game and sync-launch events a human would click. Each session is an
ordinary harness session otherwise (own prefs, pilot, bridge, scope, caps, `stop`); `status`
adds `network: alive|dead`, and a dead network makes its sessions `dead-network`.

What it is and is not. The containment is IP containment: inside a match there is no route out
and no interface but loopback, so no packet can leave. Pathname unix sockets are not part of a
network namespace and cross the boundary both ways; the one the engine uses is the X socket of
its own Xvfb, and the host's resolver answers name lookups the same way (hostnames resolve, nothing
can be sent to the answer). The holder's death is a loss of the recorded handle, not of the
network: the engine keeps its namespace alive until it exits and is still stopped through its own
scope; `status` and `list` say `dead-network`, and the match is written back as `dead` once.

Every session is contained, not only match sessions: an ordinary single-player session's engine
runs in its own loopback-only network too (the runner starts the engine under `unshare --user
--map-root-user --net`; no holder is needed for one session). The engine binds UDP 7808 on every
interface at startup, single-player included, so without this a harness session was as visible
on the LAN as the game itself for the whole run (measured 2026-10-13). `launch --network real`
is the opt-out for the rare case that needs the machine's network; a match session always lives
in its match. Only the engine is in the namespace: the runner and its Xvfb stay on the host, so
Xvfb allocates its display number where the host's X tools look for it (X display numbers are
claimed through the abstract socket, which is per network namespace; an Xvfb inside a namespace
took the desktop's number as seen from the host). The engine reaches Xvfb through the filesystem
socket. Requirements: unprivileged user namespaces (some distributions restrict them for
unconfined programs, Ubuntu's AppArmor setting being one) and `ip` from iproute2; a launch that cannot make the network fails with the reason
from engine.log and names the opt-out.

### Sound

Sessions are silent by default (`-nosound`). `launch --sound` (MCP `launch(sound=true)`, game
sessions only) plays the game's audio into a null sink of its own on your PulseAudio/PipeWire
server: a sink that outputs nowhere, so nothing reaches your speakers, and whose monitor a
recorder can capture, one session's audio only. The launch result's `sound.sink` names it
(`fsoh_<runtime-root tag>_<session id>`); record `<sink>.monitor`, e.g.
`ffmpeg -f x11grab -i <display> -f pulse -i <sink>.monitor ...`.

What keeps it off your speakers and microphone: FSO opens OpenAL devices by name, falling back to
the defaults, so the harness writes the sink and its monitor into the session's prefs as the
engine's playback and capture devices, and before starting the engine checks that the engine's
own OpenAL lists both names (a miss refuses the launch). Its streams carry
`node.dont-reconnect` and `node.dont-fallback`: if the sink disappears the engine goes silent
rather than to the default output, and a stream naming a missing sink fails instead of starting
on it. After the engine is up the launch checks that every stream of the engine is on its sink;
a stream elsewhere, or none at all, stops the session and fails the launch. `stop` unloads the
sink after the engine is gone; `cleanup` unloads this runtime root's sinks whose session is gone,
leaves other roots' sinks alone, and keeps (and reports in `kept_sinks`) any whose monitor a
recorder is still capturing. Stop a recorder BEFORE `stop`: PipeWire moves a capture whose source
disappears onto the desktop's audio unless the recorder sets the same two properties
(`PULSE_PROP="node.dont-reconnect=true node.dont-fallback=true"`; ffmpeg then needs a kill path,
it can block and ignore SIGTERM). Needs `pactl` 16+ (pulseaudio-utils).

Limits: the routing check runs once, at launch. If a named device is listed but cannot be
opened, FSO still moves on to the next device, the default: for playback the launch's routing
check catches that; for capture (FSO opens and closes it once at startup) nothing does, so the
microphone could be opened for that moment.
And the Pulse socket is a full client connection as your user: the engine could record any
source or load server modules. It always could reach the socket by its absolute path (the
private runtime folder prevents accidental use, not deliberate use); `--sound` hands it the
address. Acceptable for a local test harness, not a sandbox.

Limits of this version: one server (standalone or host) and one mission-carrying session per
match; a host launched with `--mission` starts its mission at once, so a client cannot join it
(a client cannot enter a mission in progress), which is why the recipe uses a standalone and gives
the mission to the first client; the standalone's web panel (basic auth, engine defaults
`admin`/`admin`) is reachable only from inside the match and its mission/netgame copies were stale
in testing; a match counts zero sessions against the session cap (default 3: a server and two
clients) and matches have their own cap (`FSO_HARNESS_MAX_MATCHES`, default 8); the holder lives
24 h and sessions in a match may not be given a longer `--runtime`; one machine only (no LAN
peers) in this version.

## Limits (honest list)

- **No exact replay.** FSO has no fixed timestep and seeds some randomness from the OS; `replay`
  and `compare` repeat commands, and two runs drift slightly.
- **Release builds hide a lot.** `Warning()` is compiled out and there is no `fs2_open.log`; use a
  debug build for `validate` and `crash_report`.
- **qtFRED's error check is read by OCR.** The result includes the screenshot; check names there.
- **`-noninteractive` is on by default**, so an engine `Error()` ends the session (reported by
  `crash_report`) instead of waiting on a dialog. `--interactive` turns that off.
- **Engine Lua errors are fatal under `-noninteractive`**, and in FSO the global `error()` is the
  engine's fatal error. Inside `lua` commands the harness replaces `error()` with a harmless one;
  pass `-luadev` as an extra engine argument if you want engine-side Lua errors to be non-fatal.
- **`frame_stats` windows are approximate to a frame or two.** The profiler records gameplay
  frames only; the bridge counts the same frames, and the window is the rows between the counts
  read when measuring starts and stops. The whole run is reported alongside.
- **A stale heartbeat means busy or hung.** `status` reports `heartbeat_age_sec`; the bridge
  writes it every 30 frames, so long loads also look stale.
- **Shadow folders contain top-level writes only.** Verbs that make the engine write files run it
  in a folder of links to your game folder; a write under `data/` would follow the link.
- Linux only; one machine; not for multiplayer.

What the engine actually does under the harness, with sources, is recorded in
`docs/research/2026-10-12-probe-results.md`.

## Tests

```
pytest                                            # unit tier, no engine
bounded-run pytest -m engine -p no:xdist          # real engine on Xvfb (needs FS2PATH, FSO_BIN)
bounded-run pytest -m qtfred -p no:xdist          # real qtFRED (QTFRED_BIN)
bounded-run pytest -m vulkan -p no:xdist          # needs VK_ICD_FILENAMES and a GPU
```

(`bounded-run` is a local resource-cap wrapper; any equivalent works.)
