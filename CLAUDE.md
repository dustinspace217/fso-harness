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
  aborted a session) and the string-call form `me:f"x"`. Never widen an exemption without a red
  test.
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
- **Sexp text built from caller input** (found by QA, confirmed on the engine 2026-10-12): FSO turns
  `!` into `"` and `"` is the real delimiter, so a name with `"` escapes its quotes and anything in
  a delay slot is evaluated. Names are checked to exist and refused if they contain `" ! ( )`; every
  assembled sexp goes through the script check; sexps are capped below 8000 bytes (the engine's
  8192-byte buffer aborts on overflow). Never build sexp text from caller input any other way.
- **Session isolation:** each engine/qtFRED gets HOME and XDG_* inside its run folder (FSO legacy
  `~/.fs2_open` mode, qtFRED autosave/settings) and no desktop D-Bus/session-manager address.
- **Bridge refresh is shared state:** flock + temp folder + rename, refused while another session
  lives, never deletes an unstamped folder. No versioned folder names (fixed `-mod`/channel paths).
- **Commands are claimed by rename** (bridge) and withdrawn by rename (Python), so a timeout can say
  for certain whether the command ran.
- **Exit codes 8 (bridge Lua error) and 9 (work failed)**; a finding is not a failure (exit 0).
- **Multiplayer runs inside a match**: an unprivileged user+network namespace per game
  (`unshare --map-root-user --net` holder process in its own scope; sessions join through
  `nsenter`). Nothing is ever bound on the real network; never add a port forward or firewall
  rule to "help". EVERY session's ENGINE runs in a private network by default (a match's via
  nsenter, or its own via launcher.PRIVATE_NET_PREFIX in runner.engine_argv; `--network real`
  opts out) because the engine binds UDP 7808 on every interface at startup. The runner and
  Xvfb stay on the HOST: abstract X sockets are per network namespace, so an Xvfb inside a
  namespace takes the desktop's display number as seen from the host (QA 2026-10-13). Every
  engine process binds its own UDP port; `-almission`
  belongs to the host player, never the standalone; the bridge posts the join, start-game and
  sync-launch events for clients (spec `2026-10-12-fso-harness-multiplayer-design.md`,
  probe-results section 21).

## Build, test, lint
- `pytest` (unit) · `bounded-run pytest -m engine -p no:xdist` · `-m qtfred` · `-m vulkan`
  (needs `VK_ICD_FILENAMES`). Engine tiers need FS2PATH, FSO_BIN (+ FSO_BIN_DEBUG, QTFRED_BIN).
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
- Parallel sessions: each Vulkan session costs ~1 core, ~0.55 GB RAM, ~0.5 GB VRAM; the software
  renderer ~8 cores. `FSO_HARNESS_MAX_SESSIONS` caps them (default 3).

## Etiquette
- Engine tests always via `bounded-run`; sessions never touch the user's desktop (Xvfb only).
- New engine behaviour goes into the probe-results doc with its source reference.
- This repo keeps the Claude co-author trailer; anything sent to FSO upstream carries no
  Claude trailer, and Dustin approves every upstream post.
