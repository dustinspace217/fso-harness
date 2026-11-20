# fso-harness: project context

Headless test harness for the FreeSpace Open engine and qtFRED, for AI agents. Dustin's purpose,
verbatim: "the point of this is to enable and equip LLMs to autonomously test and trial-and-error
code changes, mod creation, campaign/map creation, feature work, etc." and "I want to showcase it
as a project but I want it filled in to FSO so other FSO contributors can use it."

Authoritative documents: spec `docs/superpowers/specs/2026-10-12-fso-harness-design.md` (approved
2026-10-12), plan `docs/superpowers/plans/2026-10-12-fso-harness.md`, engine behaviour
`docs/research/2026-10-12-probe-results.md`. Status and QA history live in the plan doc, not here.

## Load-bearing decisions (do not change without new evidence)
- **No daemon.** State = systemd scopes + `$XDG_RUNTIME_DIR/fso-harness/<id>/`; every call reads
  it fresh. The MCP server is a thin, stateless wrapper over `fsoctl --json`.
- **Bridge = Lua mod first in `-mod`**, talking through numbered JSON files in
  `<prefs>/fso_harness_bridge/data/cache` (cf API only, never `io.*`).
- **Bridge Lua must never call `error()`** (FSO's forwarders.lua makes it a fatal `ba.error`) and
  must never read a field a handle lacks (fatal under `-noninteractive`). Refusals use `H.raise`.
  The whole frame runs in `pcall`. Never open a file in append mode (debug-build assert).
- **Popups:** `hv.Submit` plus an `+Override` on that dialog frame; Submit alone is ignored.
- **Startup handoff file is `switches.txt`**: OpenGL startup deletes `*.json`/`*.bin` in mod cache
  folders.
- **Lua flight control needs `FLIGHT_CONTROL_LUA_FULL`**; fire with `ship:firePrimary()` etc.
- **Agent-written Lua can abort the engine past `pcall`** (missing name, wrong argument type,
  `.Name` on a non-ship handle, colon calls on `control_info`; probe-results section 19). Facts an
  agent needs while it plays go where agents read them: the MCP INSTRUCTIONS (2,048-character cap,
  checked in tests/mcp_check.py) and AGENT-GUIDE recipes (the server's prompts). The probe-results
  doc is the evidence, not the delivery: 2026-10-12's demo hit `rotateVector` and 1-based
  `getGameDifficulty`, both already recorded in section 18 that the agent never read.
- **The `lua` pre-check (fsoctl/lua_check.py) is the only guard against fatal API names**, and it
  only sees text: library functions, `control_info` colon calls, and capitalised `.Field` /
  `:method(` names that no API class or library has (names the code defines itself are exempt).
  Its API cache is keyed by binary AND mod list (mods add Lua). Known holes, pinned in
  tests/test_lua_check.py: a real name on the wrong handle type (`Linked` read on a `weaponbank`
  aborted a session), the string-call form `me:f"x"`, and a load not written as
  `loadfile("...")`/`dofile('...')` with a quoted path (long-bracket string, no parentheses,
  a variable: neither read nor listed as unchecked; ended a Windows session 2026-11-17).
  Never widen an exemption without a red test.
- **The guide is delivered by a `guide` verb/tool**, because the INSTRUCTIONS budget is full and
  prompts cover only some sections. A new agent-facing lesson goes into a docs/AGENT-GUIDE.md
  section (reachable via `guide`), not into the instructions.
- **qtFRED** only ever opens a copy of a mission; its editor is the window titled with the mission
  file name (found by pid; the first window is a splash); needs `-noninteractive`.
- **Pointer input is window-relative** (`mousemove --window`): windows are not at 0,0.
- **Shadow working folder** (`shadowdir`) for runs that write files into the working directory:
  links to FS2PATH's folders and `.vp` files only, never loose files.
- **Eval gate:** `lua`, `post_event` and any sexp containing "eval" (the built-in Lua operators;
  mod-defined Lua operators are not recognised) need `allow_eval`; both the
  Python side and the bridge check; Python audits.
- **No vendor defaults:** `VK_ICD_FILENAMES` only if the caller sets it.
- Everything runs in a named scope with caps; every wait is bounded.
- **Display: headless Weston (default shell) + a FULL-SCREEN Xwayland** when weston, Xwayland and
  a GPU render node exist; Xvfb otherwise, for the sandbox, and on `--display xvfb`
  (fsoctl/display.py; plan docs/superpowers/plans/2026-11-18-weston-display.md). Probed
  2026-11-18: Weston's kiosk shell crashed with a full-screen X server, and its own (rootless) X
  layer shrank the window, broke whole-screen grabs and lost XTEST keys. Software OpenGL
  (`LIBGL_ALWAYS_SOFTWARE`) only on Xvfb (display.gl_env). GPU sessions share video memory with
  the desktop: display.choose reads it before choosing Weston (2026-10-15 black-desktop incident).
- **Sexp text built from caller input** (found by QA, confirmed on the engine 2026-10-12): FSO turns
  `!` into `"` and `"` is the real delimiter, so a name with `"` escapes its quotes and anything in
  a delay slot is evaluated. Names are checked to exist and refused if they contain `" ! ( )`; every
  assembled sexp goes through the script check; sexps are capped below 8000 bytes (the engine's
  8192-byte buffer aborts on overflow). Never build sexp text from caller input any other way.
- **Engine-written files only through `fsoctl/safefs.py`** (QA 2026-11-13 SA-1, probed: a
  planted link made a host write clobber a victim file). Anything the host reads or writes
  under a session's prefs folder walks from that folder with O_NOFOLLOW per component; reads
  are regular-file-only and size-capped, writes are O_EXCL temp + rename. Never add a plain
  `open()`/`Path.read_text()` on such a path; tests/test_safefs.py has the red proofs. A
  sandbox session's prefs lives on disk (`paths.sandbox_root()`), never on the /run/user
  tmpfs, under a per-file limit and the runner's size watchdog (QA AT2-1/SA2-1). On Windows
  the same interface is `platform/win_safefs.py`, by weaker means: it refuses links and
  compares the file it opened with the one it checked, but cannot close the moment between
  checking a folder and using it, and a Windows session is not confined at all (README).
- **Session isolation (Linux):** each engine/qtFRED gets HOME and XDG_* inside its run folder
  (FSO legacy `~/.fs2_open` mode, qtFRED autosave/settings) and no desktop
  D-Bus/session-manager address. On Windows only the prefs folder is the session's own.
- **Bridge refresh is shared state:** flock + temp folder + rename, refused while another session
  lives, never deletes an unstamped folder. No versioned folder names (fixed `-mod`/channel paths).
  On Windows "another session" means one of the same runtime root and login: jobs cannot be
  listed by name, so `active_units` asks about this root's run folders only (README Limits).
- **Commands are claimed by rename** (bridge) and withdrawn by rename (Python), so a timeout can say
  for certain whether the command ran.
- **Exit codes 8 (bridge Lua error) and 9 (work failed)**; a finding is not a failure (exit 0).
- **Multiplayer runs inside a match**: a REAL match (the default since 2026-10-16, same ruling
  as below) is a record with no holder; its sessions take host-unique ports (launcher.assign_port
  -> host_port) and a client with no running server to join is refused; match.observe ends an
  empty real match a day after creation (otherwise it would fill the match cap for good). Every
  host port (UDP game, TCP web panel) is TEST-BOUND before it is handed out (launcher.port_free,
  bound the way the engine binds), and readiness/web_panel count only a socket whose owner pid is
  the session's engine (launcher.engine_holds): the records know one runtime root, the host is
  shared (QA 2026-10-17 CR-1/SL-1). The unit tier stubs port_free (conftest no_host_ports). A
  PRIVATE match
  (`match create --network private`) is an unprivileged user+network namespace per game
  (`unshare --map-root-user --net` holder process in its own scope; sessions join through
  `nsenter`). Match.network defaults to "private" so pre-2026-10-17 records load as what they
  were. Never add a port forward or firewall rule to "help". NON-MATCH sessions use the
  REAL network by default since 2026-10-16 (owner: "Real network. I never meant to change this
  about how the engine works."; the security concern is exploitability, not a listening port),
  each with its own UDP port from launcher.host_port passed as -port (a taken port leaves the
  engine running with networking off, psnet2.cpp); `--network private` runs the engine in its
  own namespace via launcher.PRIVATE_NET_PREFIX in runner.engine_argv. In private mode the runner and
  Xvfb stay on the HOST: abstract X sockets are per network namespace, so an Xvfb inside a
  namespace takes the desktop's display number as seen from the host (QA 2026-10-13). Every
  engine process binds its own UDP port; `-almission`
  belongs to the host player, never the standalone; the bridge posts the join, start-game and
  sync-launch events for clients (spec `2026-10-12-fso-harness-multiplayer-design.md`,
  probe-results section 21).
- **The standalone's web panel never runs with the engine's admin/admin**: every standalone gets
  a random login (launcher.web_login) and, on the real network, a TCP port unique among live
  real-network standalones (launcher.web_port), written into its multi.cfg; reported in launch
  and status as `web_panel`, never in an audit log. Only the standalone starts the panel on
  Linux (multi_options.cpp, Is_standalone); the login guards only the `/api/1/...` paths, so
  every standalone also gets `+webui_root` = an empty 0700 folder in its run folder: with no root
  the engine serves the whole filesystem with no login (QA SA-1 2026-10-17, probe-results 21).
- **`--sound` never reaches the speakers or the mic** (fsoctl/sound.py, README "Sound"): a
  per-session null sink `fsoh_<root tag>_<id>`, named as FSO's PlaybackDevice AND CaptureDevice
  (its monitor) in the session prefs, because FSO opens OpenAL devices BY NAME and falls back to
  the defaults (2026-10-15: the first --sound session opened the USB output; every one opened
  and closed the desk mic). check_device_names refuses a launch whose OpenAL does not list both
  names; streams carry node.dont-reconnect + node.dont-fallback (a gone or missing sink = silence,
  never the default); verify_routing after ready fails CLOSED (no moves: dont-reconnect makes
  them impossible). Sink load/unload under the user-wide sound_lock (NOT launch.lock: _abandon
  calls stop under it); unload only after checking the index still names this session's sink
  (PipeWire reuses a freed index at once). cleanup touches only its own root's sinks. Tests:
  conftest stubs the sound server for EVERY test; only a `sound`-marked test may reach it.

- **Windows sessions (Plan B, 2026-11-16/17; measurements in
  docs/research/2026-11-16-xplat-vm-results.md and probe-results section 23).** One module
  per facility, picked once by `fsoctl/platform/__init__.py`; Linux modules stay where they
  are and the `win_*` twins must import on Linux (tests/test_platform_parity.py holds the
  pairs to the same public functions). What each Windows piece rests on:
  - A session is a named **Job Object** (`fso-harness-<id>`). Its NAME lasts only while a
    handle is open, so the runner inherits a handle to it; never set KILL_ON_JOB_CLOSE on a
    session's job (the launching command exits first by design). A BUILD's job is the
    opposite on purpose (`win_scope.run`): MSBuild leaves workers behind.
  - The session's first process gets an explicit handle list (log, job, desktop). Plain
    inheritance hands it the caller's stdout pipe and the MCP server then waits forever.
  - A session runs on a **desktop of its own**, made by `win_scope.start` from a
    `Desktop=<name>` property and kept alive by an inherited handle. `--visible` is the only
    way onto the user's desktop.
  - The runner is stopped by the **stop marker** in the run folder (`session.STOP_MARKER`,
    the one file `stop` has always written; the runner also looks for it before it starts
    the engine), then the engine by `WM_CLOSE`; there are no signals.
  - A job has no wall-clock limit (`platform.GROUP_TIME_LIMIT`), so **the runner is the only
    thing that ends an engine** at its limit or on a stop. A runner that fails after
    starting the engine ends it first (`win_runner._end_after_failure`). Cleanup ends a
    session still "starting" after its grace, and one whose recorded runner is KNOWN to be
    gone while its group still runs (`launcher._end_unattended`). "Known": `procs.pid_ended`
    or another start time; a lookup that failed is never read as gone, and a record that
    already has an end keeps it.
  - **`launcher._gone(sess)`, not `scope.is_gone`, says whether a session is over.** A job's
    name is known only inside the Windows login that made it, so from another login a live
    session's job is "not found"; the recorded runner's pid and start time can be asked
    about from any login. Only "no job of that name" means a job is gone
    (`win_scope._may_be_there`); alive means the process has not ENDED (an ended process can
    still be opened while a handle to it is held and still gives its start time). The rule:
    a record with no end is gone only when its runner AND engine are KNOWN gone
    (`_known_gone`; "not known alive" is not enough: both outside reviewers found a
    cleanup from another login deleting such a record); a record that has its end is gone
    unless one of the two is known alive. The engine's start time is recorded for this
    (`engine_start`). Where groups are seen from every login (`platform.GROUPS_PER_LOGIN`
    False: Linux) the record is never asked, by `_gone`, `stop` or any tool. `stop` refuses,
    and does not mark the record, when the engine runs on with no runner and the group
    cannot be seen. "Runs in another Windows login" is said only when the group is NOT
    found here (`launcher._runs_out_of_sight`): a runner outlives its engine by half a
    second in the session's own login too, and there the ordinary answer is the right one.
    `stop` asks whether it sees the group BEFORE it stops anything, for the same reason.
  - **A file another process has open refuses to be renamed over and may refuse a read**
    (`platform.BUSY_FILES_REFUSE`, probe 11: a third of renames, a tenth of reads). Session
    records go through `session._while_busy` / `_read_record`; never add a bare `os.replace`
    onto, or read of, a file other processes read. A refused channel file is "busy" there,
    not "tampered". Such a file also refuses to be DELETED, and an empty job does not mean
    its files are free (CI's Windows machine, 2026-11-17: the engine's process was in its
    last instant and its log "in use"; 16 ms later the folder could be removed). The
    cleanup's folder removal goes through `_while_busy` for that; never write code or a
    live test that deletes a just-ended session's files in one try.
  - **An exclusive create FOLLOWS a planted link whose target does not exist** (measured).
    `win_safefs.write_atomic` uses a random temp name; never go back to a fixed one. That
    is the only place guarded this way: the screenshot's claimed name, the record's and
    the sequence number's temp files and the stop marker are plain creates at fixed names
    in the run folder, within "nothing confines a Windows session". Do not read an
    exclusive create in the run folder as a guard.
  - **A busy channel file after a command was sent never ends in "try again"**: the reply
    read rides it out, a reply that cannot be deleted still gives its result, and a
    command file in use at the timeout is "taken, outcome unknown" (the command may have
    run; a second send would run it twice).
  - **The runtime root must be ASCII on Windows** (`platform.PREFS_PATH_ASCII_ONLY`,
    `launcher._check_run_folder`): the engine cannot keep its preferences under a path with
    a letter outside ASCII (its own source; probe 12: it never came up). The launch refuses
    and names `FSO_HARNESS_RUNTIME`. Never drop the refusal without re-running probe 12.
  - Programs are found without the current folder and started by full path
    (`platform/programs.py`; netstat from the Windows folder): Windows looks for a bare
    name in the current folder first, and that folder may be the checkout under test.
  - Windows sessions get `[Game] UnfocusedPause=false` in their prefs: a hidden window is
    never in front and `-no_unfocused_pause` is ignored by current engines.
  - **Never bind a port to test it on Windows** (`launcher.port_free` reads the socket table
    there): a bind on every address makes Windows Firewall add a rule for Python and ask the
    user. The owner lookup reads plain `netstat -ano` (`-p UDP` omits IPv6 sockets).
  - The engine's rename cannot replace a file on Windows: the bridge deletes and renames
    again, and the heartbeat reader retries.
  - `crash_report` matches a minidump to its session by the process id inside the dump, not
    by name and time alone (two sessions of one build share those). cdb needs `.ecxr` and a
    decimal frame count.
  - The MCP server shares its console with `fsoctl` when it has one (Ctrl-Break is the only
    stop request, and it travels through a shared console) and starts it with no window when
    it has none.
  - No sandbox, no private network, no matches, no sound, no editors, no input verbs on
    Windows yet: each refuses by name (`platform.needs_linux`). Never make one "work" by
    weakening the refusal.
- **Text files are UTF-8 by name** (`encoding="utf-8"` on every open/read_text/write_text;
  ruff's PLW1514 sees `open()` only, so tests/test_text_encoding.py reads the syntax tree
  and fails on any call without it).
- **The runner is started with `python -P`** (both systems): under `-m` the caller's folder
  comes first on the module path, and a folder named fsoctl or a json.py in a checkout
  under test was what the host-side runner loaded, outside the sandbox too.
- **Names that become paths refuse `\` and `:` on every system** (mod parts, VP entries), and
  the Lua pre-check never opens a path that starts with two separators (a network share on
  Windows). Windows' default is the local code page.
- **Which tests run where** is in tests/conftest.py: `LINUX_ONLY_FILES` (not collected off
  Linux), the `linux` mark for single tests, the `symlinks` mark. A test that fails on
  Windows is read before it is marked: three of the first batch were product faults.

## Build, test, lint
- `pytest` (unit) · `bounded-run pytest -m engine -p no:xdist` · `-m qtfred` · `-m vulkan`
  (needs `VK_ICD_FILENAMES`). Engine tiers need FS2PATH, FSO_BIN (+ FSO_BIN_DEBUG, QTFRED_BIN).
- Windows: `pytest` and `pytest -m winlive tests/winlive` (CI's `windows-unit` job runs both
  and must pass). Locally that means the VM: tools and how to use them are in the
  artifacts folder beside this repo, `fso-harness-artifacts/xplat-vm-2026-11-16/tools/`
  (`xvm.sh`, `xsync.sh`, `xrun.sh`, `fsovm.sh`).
- `ruff check .` clean; `shellcheck probes/*.sh mcp/*.sh` clean.
- Lua: `luajit -bl fsoctl/bridge/fso_harness_bridge/data/scripts/fsoharness.lua > /dev/null` for a
  syntax check (the engine itself is the real test).

## examples/pilot (a consumer of the harness, not part of the package)
- One change per iteration, each from what the last run's log showed; a fresh session per run
  (a mission restart keeps Lua state). Mission-specific setup (target lists, waiting points)
  stays in a setup file outside the repo; pilot.lua carries only general behaviour, with a
  comment naming the run that motivated each rule.
- Behaviour shared by every mode (the idle hold, the ram check, ETS) changes every drill: re-run
  the drills before calling a change safe. Results in its README carry sample sizes; two runs is
  not a rate.
- Mission-specific knowledge (waiting points, cue chains, which wing to spare) lives in setup
  files outside the repo; the README says when a result rests on it (a veteran's knowledge).
- Measure before explaining: per-frame tallies (hits by weapon class, contacts) and the mission
  file's cues overturned three confident explanations in one day (2026-10-15); a 1 Hz sampler
  cannot rule out a sub-second event.
- Parallel sessions: each Vulkan session costs ~1 core, ~0.55 GB RAM, ~0.5 GB VRAM; the software
  renderer ~8 cores. `FSO_HARNESS_MAX_SESSIONS` caps them (default 3).

## Etiquette
- Engine tests always via `bounded-run`; sessions never touch the user's desktop (Weston or
  Xvfb, never the desktop).
- New engine behaviour goes into the probe-results doc with its source reference.
- This repo keeps the Claude co-author trailer; anything sent to FSO upstream carries no
  Claude trailer, and Dustin approves every upstream post (except bug issues: the FSO
  project folder's CLAUDE.md, "Bugs found outside the current PR").
