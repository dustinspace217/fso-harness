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

Linux, and Windows for game sessions (see [Windows](#windows) for what works there and what
does not yet). License: [Unlicense](LICENSE), matching FSO's newer code.

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
  folder and, on Linux, its own `HOME`/`XDG_*` folders inside its run folder (FSO's legacy
  `~/.fs2_open` mode and qtFRED's autosave/settings files otherwise land in your home). On
  Windows only the preferences folder is the session's own (see Windows, Limits). qtFRED only ever opens
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

## Windows

Game sessions run on Windows 10 and 11. What follows was measured on Windows 11 in a virtual
machine with FSO 26.2 and on GitHub's Windows runner; the measurements, with the commands
that made them, are in `docs/research/2026-11-16-xplat-vm-results.md`.

**What works** (each run against the real engine there): `launch` (a mission, every skip
switch, a created pilot), `status`, `list`, `stop`, `cleanup`, `state`, `lua`, `sexp`,
`wait_for`, `pilot`, `time`, `autopilot`, `screenshot` (with `--crop` and `--scale`), `log`,
`crash_report`, `build`, `api_docs`, `frame_stats`, `validate`, `model_info`, `guide`, and
the MCP server with the same tools. Nothing else needs installing: process groups, the
hidden desktop and screenshots use Windows' own interfaces from Python's standard library.

**Not run on Windows yet** (not refused, simply untried): `answer_popup`, `journal`,
`post_event`, `replay`, `scratch_mod`, `telemetry`, `vp`; and `compare` and
`renderer_compare`, which need ImageMagick's `magick` on PATH and say so when it is missing.

**Out of sight.** Each session runs on a Windows desktop of its own, never on yours: no
window, no taskbar button, no change of focus. `launch --visible` puts the game window on
your desktop instead (run in the test machine: launch, screenshot, stop).

**Setup** (PowerShell):

```
pip install -e .
$env:FS2PATH = 'C:\Games\FS2'                                  # the folder with root_fs2.vp
$env:FSO_BIN = 'C:\Games\FS2\fs2_open_26_2_0_x64_SSE2.exe'
powershell -ExecutionPolicy Bypass -File mcp\setup.ps1          # MCP server venv (mcp==2.0.0)
powershell -ExecutionPolicy Bypass -File mcp\register.ps1       # -WhatIf shows the command only
```

`register.ps1` stores the variables in the registration, as `register.sh` does. Optional
extras: Microsoft's Debugging Tools for Windows (`cdb`) for crash backtraces; CMake and
Visual Studio's Build Tools for `build` (`--config Release` names the configuration; a
cmake that is not on PATH is taken from the build folder's own record); Developer Mode for
the three verbs that need to create links.

**The first time,** Windows Firewall asks once for each engine file (each build folder and
configuration is a new file) whether to allow it: the engine opens a UDP port when it starts,
as the stock game does. The question appears on YOUR desktop; it is the one window a hidden
session can put in front of you. Answer Cancel: Windows sessions here are single-player and
need no incoming traffic. Measured: Windows adds blocking rules for the file at that moment
(its firewall log), and the question was seen for Python. Not measured: whether the question
shows for an engine on a hidden desktop, and what Cancel does to those rules. The harness
itself opens no port and adds no firewall rule.

**Limits on Windows,** each with its reason:

- **Nothing confines a Windows session.** The engine and every mod's scripts run as you, with
  your files and the whole environment of the command that started `fsoctl` in reach. Do
  not run a build or a mod you do not trust on Windows; use Linux with `--network sandbox`
  for that. The memory and process limits, `stop` and the hidden desktop hold for an engine
  that does not work against them.
- **The harness's own file guard is weaker there.** What the harness reads and writes in a
  session's preferences folder still refuses links and checks the file it opened, but the
  harness, which uses only Python's standard library, cannot close the moment between
  checking a folder and using it. With the engine unconfined in any case, that is accepted.
- **The run folder's path must be plain ASCII.** FSO on Windows cannot keep its
  preferences under a path with a letter outside ASCII (its source says so; measured: under
  a folder named "José" the engine never came up). The default run folder is under your
  profile, so with such a letter in your user name `launch` refuses and names the way out:
  set `FSO_HARNESS_RUNTIME` to an ASCII path such as `C:\fso-harness\run`, and run
  `mcp\register.ps1` again so the MCP server gets it too. Make that folder one only your
  account can write: a folder made under `C:\` is by default open to the PC's other
  accounts (see the last limit in this list), and the run folder holds each session's
  command channel. A space and an apostrophe in the path are fine (measured: launch,
  state, screenshot, stop, cleanup).
- **Do not run the harness from an administrator prompt.** The engine and every mod would
  run with those rights.
- **One Windows login per session.** A session's process group is known only inside the
  Windows login that started it (measured: asked from an ssh login, a live desktop session
  shows as not alive). From another login `cleanup` leaves the session alone and `stop`
  leaves its request in the run folder, where the session's runner finds it and closes the
  engine (it allows 20 seconds for that, then ends it by force); `stop` answers before that
  and says so. Tools that need a running session say it runs in another login; `status`
  and `list` only show it as not alive. Everything else
  is to be done from the login that started the session. If that session's runner is gone
  and only its engine is left, `stop` from another login refuses and says where to go. What a session started from an
  administrator prompt looks like from an ordinary one is not measured.
- **A runner that is ended by force leaves its engine running** until the next `launch` or
  `cleanup`, which ends it and records why (measured), or a `stop` (measured). One case the
  automatic step misses: the runner's process number has since passed to a process your
  account may not ask about; then only `stop` ends that engine. A runner that is alive but
  stuck is not noticed. On Linux the scope's own time limit covers all of these.
- **A session's time limit keeps counting while the PC sleeps** (by the clock Python reads
  on Windows; not measured here), so a session can be over when the PC wakes.
- **The bridge refresh looks at one runtime root.** "Refused while another session is
  running" holds for sessions of the same runtime root and login. A session under another
  root (a test run with `FSO_HARNESS_RUNTIME` set) is not seen, and its bridge can be
  replaced under it.
- **A runtime folder outside your profile is as open as its parent folder.** The default
  folders are under your profile, which other accounts cannot read. Windows has no
  equivalent of the `chmod 700` the harness sets on Linux. Measured: a run folder the
  harness makes under `C:\` inherits "Authenticated Users: modify". To keep such a folder
  to yourself, make it first and run (Command Prompt)
  `icacls C:\fso-harness /inheritance:r /grant:r "%USERNAME%:(OI)(CI)F"`; a session was
  run under a folder narrowed this way (under an account with a plain name; with a letter
  outside ASCII in the account's name the line was not tried).
- **No editor sessions yet** (`launch --qtfred`, every `fred_*` tool). The editor is driven
  through its windows, and that route is the next piece of work.
- **No input tools yet** (`keys`, `click`, `drag`, `move`, `scroll`). Posted key presses
  reached the engine only while its window was the foreground one (measured), which a
  hidden session's never is. A `--visible` session refuses them too: real input to a window
  on your desktop is not built or measured. Scripts (`lua`, `sexp`) need no input.
- **No `--sound`.** Per-session audio is built on PipeWire; the Windows route is not built.
- **No matches** (the multiplayer bench), **no `--network private`, no `--network
  sandbox`.** The last two are Linux namespaces and bwrap, which Windows does not have.
- **`screenshot --full` is refused.** A Windows session has a desktop of its own, not a
  display of a fixed size; the plain screenshot is the game's picture at its resolution.
- **`api_docs`, `frame_stats` and `replay` of a recorded session need the right to create
  links** (Developer Mode); they refuse otherwise and say so.
  `api_docs` and `frame_stats` were run this way; `replay` was not.
- **Crash backtraces need `cdb`.** Without it `crash_report` still names the engine's own
  crash dump, the exit code and the log. Function names need the engine's `.pdb` beside it.
- **A session at its memory limit is not ended.** A Windows job's limit makes the engine's
  memory requests fail; what the engine does then is up to the engine. Linux ends the run.
- **`launch` refuses when less than about 10 GB of memory is free** (8 GB kept for your
  desktop, 2 GB the least a session gets), the same rule as on Linux. On a 16 GB PC with
  other programs open this can refuse.
- **A session stopped while a popup is up in the main hall** takes 20 seconds and is ended
  by force. Launch with `--dismiss-popups`, or stop from inside a mission.
- **A timed-out tool call is cleaned up only if the MCP server has a console** (it does
  when Claude Code runs in a terminal). Without one the command is ended at once, and a
  session it had started runs until its own limit; `list` shows it.
- **Hidden sessions were verified with software rendering only** (Mesa's OpenGL beside the
  engine). A real graphics card and Vulkan on a hidden desktop are not measured; if the
  picture is black there, use `--visible` (itself run with software rendering only). Which
  renderer a session reports rests, for the vendors' drivers and Vulkan, on library names
  that were not seen in a run.
- **Not run:** `register.ps1` without `-WhatIf` (the test machine has no Claude Code); a
  game folder or a mod name with a letter outside ASCII (only the run folder was tried); a
  Windows in a language other than English (the harness reads `netstat` and the debugger by
  their digits and symbols, not their words, but no such system was tried).

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

Every `launch` first runs `cleanup` on its own: run folders of sessions that ended cleanly over a
day ago go, crashed ones (signal, non-zero exit, a runtime/memory/disk limit) after a week, so
`crash_report` still works; `cleanup` deletes on demand with any `--older-than`.

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

A multiplayer game runs inside a *match*: a record that groups the sessions of one game. By
default a match is on the machine's real network, as the stock game is: its engines bind their
UDP game ports on the host, each on its own port (the same host-wide rule as ordinary sessions,
below), and a client joins its match's server through loopback. `match create --network private`
makes a private match instead: one loopback-only network held open by a sleeping holder process
in its own systemd scope (`unshare --user --map-root-user --net`, no root anywhere). The engine
binds its UDP game port on every interface and has no bind-address option, and the standalone
server opens a web panel on TCP, so in a private match containment comes from the network itself:
inside it those sockets exist, on the real network they never do, and a packet sent out of it
fails with "network unreachable" (kernel-enforced; probed and tested on every run). The online
tracker is never enabled by the harness, in either kind of match.

```
fsoctl match create                      # -> {"id": "m1a2b3c4d", "network": "real", ...}
fsoctl match create --network private    # -> {"id": ..., "network": "private", "holder_pid": ...}
fsoctl launch --match m1a2b3c4d --role standalone
fsoctl launch --match m1a2b3c4d --role client --pilot create:Alpha --mission M-01.fs2 \
       --skip-movies --dismiss-popups --skip-briefing
fsoctl match list                        # matches, liveness, the sessions in each
fsoctl match remove m1a2b3c4d            # stops the sessions first, then the match
```

Roles: `standalone` (`-standalone`, no window, no pilot, no mission; readiness is its listening
socket), `host` (`-startgame`, a player who hosts), `client` (`-connect`, auto-joins; the first
client of a standalone game becomes the host). Every engine process binds its own port: in a
private match the sessions get 7808, 7809, ... inside its namespace; in a real match they take
the next free port on the host, so two real matches never collide, and a client launched before
its real match has a running server is refused (pass `--connect` to join elsewhere). A `--mission` on a host or client becomes the engine's
`-almission`, which the host player consumes to start that mission with no human; the bridge
posts the join-screen, start-game and sync-launch events a human would click. Each session is an
ordinary harness session otherwise (own prefs, pilot, bridge, scope, caps, `stop`); `status`
adds `match_network: alive|dead` (its `network` is the record's `real|private`), and a dead
match makes its sessions `dead-network` in `status` and `list`: a private match whose holder
died, or a real match ended because it was older than a day with no live session (a real match
has no holder to lose; otherwise it lives until `match remove`).

What a private match is and is not. The containment is IP containment: inside it there is no route out
and no interface but loopback, so no packet can leave. Pathname unix sockets are not part of a
network namespace and cross the boundary both ways; the one the engine uses is the X socket of
its own Xvfb, and the host's resolver answers name lookups the same way (hostnames resolve, nothing
can be sent to the answer). The holder's death is a loss of the recorded handle, not of the
network: the engine keeps its namespace alive until it exits and is still stopped through its own
scope; `status` and `list` say `dead-network`, and the match is written back as `dead` once.

An ordinary (non-match) session uses the machine's real network by default, as the stock game
does: the engine binds its UDP game port on every interface at startup, single-player included
(freespace.cpp game_init). Each real-network session gets its own port, passed as `-port`: the
first from 7808 up that no live harness session holds and that a test bind on the host accepts
(made the way the engine binds, so a game you run yourself or another program on the port is
skipped), because an engine whose port is taken logs the failure and carries on with networking
off (psnet2.cpp). A program that takes the port in the moment between the test and the engine's
bind still wins; a standalone's readiness counts only its own engine's socket, so that case
fails the launch instead of reporting a server nobody can join. `launch --network
private` puts the engine in its own loopback-only network instead (the runner starts it under `unshare --user --map-root-user
--net`; no holder is needed for one session); a match session always lives in its match's
network (`--network` on such a launch must agree with it). In
private mode only the engine is in the namespace: the runner and its Xvfb stay on the host, so
Xvfb allocates its display number where the host's X tools look for it (X display numbers are
claimed through the abstract socket, which is per network namespace; an Xvfb inside a namespace
took the desktop's number as seen from the host). The engine reaches Xvfb through the filesystem
socket. Requirements: unprivileged user namespaces (some distributions restrict them for
unconfined programs, Ubuntu's AppArmor setting being one) and `ip` from iproute2; a launch that cannot make the network fails with the reason
from engine.log and names the opt-out.

`launch --network sandbox` is for engine builds you do not trust (a pull request by someone
else): the private network plus a bubblewrap (`bwrap`) sandbox around the engine
(`fsoctl/sandbox.py`). Inside, the engine sees `/usr` read-only, its own Xvfb socket and no other
X socket, and its binary's folder, FS2PATH and the named mods read-only at their usual paths. Its
prefs folder is the one host folder it can write, mounted at `/fso-harness/prefs` and kept on
disk (`~/.cache/fso-harness/sandbox-runs/<id>/prefs`, override with `FSO_HARNESS_SANDBOX_ROOT`),
not on your `/run/user` tmpfs; its HOME,
XDG runtime folder, cache and `/tmp` are size-capped tmpfs that exist only inside. The root is
read-only, and `/run` (the session bus, the sound server), `/sys`, your home, the GPU and input
devices, the run folder's `session.json` and logs, and the shared Mesa cache do not exist there
(`/home` and `/mnt` only hold the path down to FS2PATH or the build when those lie there). Its environment holds only the display and folder variables the harness sets, and it cannot
reach your kernel keys: a seccomp filter makes every key syscall (`add_key`, `request_key`,
`keyctl`) fail, `/proc/keys` is unreadable, and it starts in a fresh session keyring. bwrap stays
in the session's scope, so the memory and runtime caps and `stop` work as usual; the harness's
own tools (screenshots, input, state) run on the host and are unchanged. Limits: game sessions
only, OpenGL only (no GPU inside), no `--sound` (it loads the build's OpenAL library in a host
process), no match, and a pilot copied only from a harness run folder. `crash_report` runs no
debugger for a sandboxed build (gdb would parse its binary and core on the host); it still
reports the signal, exit code and log. Requires `bwrap`, `prlimit` and `keyctl` (Fedora: `dnf install bubblewrap util-linux keyutils`).

What the sandbox does not cover:
- No seccomp filter: a kernel bug reachable from an unprivileged user namespace is out of scope.
- Xvfb runs on the host, outside the sandbox; the engine talks to it over its X socket.
- How much the engine can write: no single file past 256 MiB and at most 512 files open at once
  (limits on everything in the sandbox, its stdout log included), and no `fallocate`, io_uring
  or extended attributes (the calls fail, so space cannot be reserved or hidden without
  ordinary writes). The session is ended at once (SIGKILL, no
  grace), with a warning in `runner.log` and the journal, and its prefs folder emptied, when
  that folder holds more than 1 GiB or 100,000 entries, when it cannot be measured (locked
  folders are unlocked and measured; folders that keep vanishing under the walk, or anything
  else, count as over the cap), when free space on that disk has dropped by more than the cap
  plus 2 GiB since launch, or when the disk holding it has less than 10 GiB free (`FSO_HARNESS_SANDBOX_PREFS_MAX` and
  `FSO_HARNESS_SANDBOX_FREE_MIN` change the two numbers; launch refuses a disk already under
  the floor; `FSO_HARNESS_SANDBOX_DROP_MARGIN` sets the 2 GiB). The drop since launch is what
  sees files the engine unlinked but keeps open, which no folder walk counts; another program
  writing to the same disk during the run can trip it too, which ends the run early (the safe
  direction). It assumes one sandbox session per disk at a time; launch warns when another
  sandbox session is live on the same disk. The check runs every 2 s, so what the engine writes in those 2 s
  can pass both: a few GB on a fast disk. The only hard bound is a filesystem quota on the
  sandbox root (`FSO_HARNESS_SANDBOX_ROOT`), which the harness does not set up.
  `crash_report` and `status` show such a session as ended by `disk_limit`, also when a stop
  follows. `fsoctl cleanup` removes a stopped session's folder whatever modes or depth the
  engine left in it, and also sandbox folders whose session record is gone (records live on
  the `/run/user` tmpfs and vanish at a reboot).
- Everything the engine writes or draws (logs, bridge replies, game state, screenshots) is
  content the build chose. An agent reading it should treat it as data, not instructions.

Every host-side read or write in a session's prefs folder (the bridge channel, logs, popups,
pilots copied by a replay, `pofspew.txt`) goes through `fsoctl/safefs.py`, for every network: no
link is followed, no FIFO is waited on, and reads are size-capped, so an engine cannot point the
harness at your files. When an engine has put a link or a non-file where a channel file belongs,
bridge calls fail with code 7 (refused), message `channel tampered: ...` and
`details.kind = "channel_tampered"`. Do not retry: the same link is still there; stop the session
and treat the build as suspect.

On Windows the same checks are made by weaker means and the engine is not confined at all
(Windows, Limits). There a channel file that another program has open for a moment gives
`the session's command channel is unusable` with a hint to try again; that is a busy file,
not tampering.

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
the mission to the first client; the standalone's web panel (basic auth) never runs with the
engine's `admin`/`admin` default, and each standalone gets an empty web folder of its own
(`+webui_root`): every standalone gets a random login
(`fsoh-` plus 6 hex digits, a 24-character password) and, in a real match, a TCP port no other
live real-network standalone holds and a test bind accepts (8080 up; 8080 inside a private
match's namespace), all in the launch result and `status` under `web_panel` (only while the
standalone's own engine holds that port; otherwise `web_panel` is null and launch warns) and in
no audit log; its mission/netgame copies
were stale in testing; a match counts zero sessions against the session cap (default 3: a server and two
clients) and live matches of either kind have their own cap (`FSO_HARNESS_MAX_MATCHES`, default
8; a real match counts until it is removed, or until it is a day old with no live session); a
private match's holder lives 24 h and sessions in
it may not be given a longer `--runtime`; the harness launches every session on one machine (a
real match is reachable from the LAN like the stock game, but no LAN peer is set up or tested).

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
- Linux, and Windows for game sessions (the Windows section lists what is missing there);
  one machine; not for multiplayer.

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

On Windows:

```
pytest                               # the part of the unit tier that applies there
pytest -m winlive tests/winlive      # real Win32 calls: process groups, the hidden desktop
```

`tests/conftest.py` lists the test files that test Linux facilities and are not collected
on other systems; CI runs both lines above on GitHub's Windows runner and they must pass.
