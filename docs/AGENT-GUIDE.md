# Agent guide: recipes

Short recipes for common FSO tasks. Tool names are the same in the MCP server and the CLI
(`fsoctl --json <tool> ...`). Every call returns JSON; on failure read `message` and `hint`.
Always `stop` sessions you started.

## Test an engine code change

1. `build` the worktree (it must already be configured with cmake). Read `errors`.
2. `launch` with `binary` = the new build, a `mission`, `pilot: "create:Test"`, `skip_movies`,
   `dismiss_popups`, `skip_command_briefing`, `skip_briefing`.
3. `wait_for` kind `state`, value `GS_STATE_GAME_PLAY`.
4. Exercise the change: `sexp`, `pilot`, `wait_for` an event, then `state` and `screenshot`.
5. `log` for warnings; `stop`. If the engine died: `crash_report` (use a debug build for a useful
   backtrace).

## Compare a branch with master

1. Write a scenario file: `[{"wait_for": {"kind": "mission_time", "value": 20}}, {"snapshot": "t20"}]`.
2. `compare` with `a` = master build, `b` = branch build, `launch` = `{"mods": [...], "mission": "..."}`.
3. Look at `state_diffs` (ship list, goals, events are the strong signals; positions drift a
   little between any two runs) and `image.mean_abs_diff` plus the diff image.

## Check a rendering bug on Vulkan

1. Make sure `VK_ICD_FILENAMES` points at the GPU's Vulkan ICD and the build has Vulkan.
2. `renderer_compare` with a scenario that reaches the scene in question. The result says which
   renderer each run really used (`renderers`), so a silent fallback cannot fool you.

## Write and check a mission or a table in a scratch mod

1. `scratch_mod` `create` gives a folder with `data/missions`, `data/tables`, ... Write files there.
2. `validate` with `mods` = the scratch mod name (plus any mods it needs), a debug `binary` if you
   have one, and `mission` to also load it into gameplay. Read `warnings`, `problems`, `popups`;
   `aborted: true` means validation stopped early: `abort_reason.kind` says whether the engine
   died (`engine_died`) or did not get there in time (`timeout`); read `problems` either way.
3. `fred_open` the mission (absolute path is fine), then `fred_check` for the editor's own error
   check. Fix, repeat. `scratch_mod remove` when done.

## Playtest a mission with scripts

This is the fast way to test a mission, and the one most easily missed: you do not have to fly
it. The engine's sexp and Lua interpreters are available from outside, so a script can check and
change mission state while the AI plays.

1. `launch` with `mission`, `pilot: "create:Test"`, every `skip_*` flag, `dismiss_popups`, and
   `allow_eval: true` (without it, `lua` and script-running sexps are refused).
2. `wait_for` kind `state`, value `GS_STATE_GAME_PLAY`.
3. Read the mission's goals and events: `state`. These names are what you wait on.
4. Let the AI fly (`autopilot on`) and speed time up (`time` compression 400 or more).
5. Steer the test with sexps:
   - ask a question: `sexp` mode `eval`, e.g. `( is-destroyed-delay 0 !Alpha 2! )` (`!` quotes
     strings), or mode `number` for a value;
   - make something happen: `sexp` mode `run` runs the expression inside a `when`, so any
     mission-editor action is available (destroy a ship, set a variable, send a message, warp in
     a wing, fail or complete a goal) without editing the mission.
6. Reach past sexps with `lua`: the full scripting API of this build is in `api_docs`
   (`scripting.lua`). `mn.Ships["Alpha 1"]`, `mn.Events`, `mn.Goals` cover most reads.
   Longer scripts can live in files: `local f, e = loadfile("/abs/path/script.lua")` then
   `pcall(f)`. A chunk loaded that way sees the engine's own `error()` (fatal), not the harness's
   harmless one, so it must not call `error()`.
7. `wait_for` kind `event_true` / `goal` / `destroyed` to pin the moments that matter, with
   `state` at each one; `screenshot` only where a picture adds something.
8. `log` for warnings the mission raised; `stop`.

Everything you send is journaled, so `replay` repeats the test on another build and `compare`
runs the same scenario on two builds side by side (write the steps as a scenario file).

## Fly a mission

1. Read the mission's events and goals first: `state` lists them with their status.
2. Either `autopilot on` and watch (`time` compression 400 speeds it up), or fly with `pilot`:
   short calls (1-3 mission seconds), `state` in between to see where things are.
3. `wait_for` `event_true` / `destroyed` / `goal` to know when something happened.
4. For a pilot that decides every frame (aiming, dogfighting), install your own controller from
   `lua`: `engine.addHook("On Frame", fn)`, where fn calls `ba.setControlMode(FLIGHT_CONTROL_LUA_FULL)`,
   writes `ba.getControlInfo()` axes (+Heading = nose right, +Pitch = nose down) and fires with
   `ship:firePrimary()` / `fireSecondary()`. Wrap fn's body in `pcall` and give control back
   (`FLIGHT_CONTROL_NORMAL`) on error: an error escaping a hook ends the session.
5. Lessons from flying SM1-01 this way (probe-results section 19 has the sources):
   - **pcall does not save you from API mistakes.** A missing name or a wrong argument type
     throws a C++ exception that ends the engine even inside `pcall`. The `lua` tool refuses the
     two shapes it can see in the text (an unknown library function such as `ba.getControlMode`,
     and a colon call on the no-self `control_info` functions), including in files loaded with
     `loadfile("/abs/path")`, whenever `api_docs` is cached for the build (the reply's `lua_check`
     says `passed` or `skipped: <why>`). It cannot see handle types, so check handle methods in
     `api_docs` `scripting.lua`. Compare handles with `getSignature()`, never `.Name`:
     a ship's `Target` or a weapon's `HomingObject` can be a missile, and reading `.Name` on a
     weapon is fatal. Read ship-only fields after `getBreedName() == "Ship"`. Call
     `control_info` functions with a dot (`ci.useButtonControl(-1, "SHIELD_EQUALIZE")`); a colon
     passes the handle as argument 1 and aborts the engine.
   - **Player keys from Lua:** `ci.useButtonControl(-1, name)` presses any one-shot bound control
     (names: `plr_commands` in control_info.cpp, e.g. TARGET_NEXT_CLOSEST_HOSTILE, SHIELD_EQUALIZE,
     INCREASE_WEAPON, LAUNCH_COUNTERMEASURE, END_MISSION) after
     `ba.setButtonControlMode(BUTTON_CONTROL_LUA_ADDITIVE)`. Call `ci:clearLuaButtonInfo()` at the
     start of each frame, or a press repeats. Held controls (trigger, afterburner) do not go through
     this path, and under LUA_FULL the control_info trigger counts are ignored: fire with
     `firePrimary()`.
   - **Steering:** world to ship-local is `orientation:rotateVector` (unrotate is the reverse); dot
     products with `getRvec/getUvec/getFvec` cannot be mixed up. The stick sets turn rate = stick x
     max rate, which the ship reaches with a lag, so pure proportional steering overshoots and
     wiggles. `Physics.RotationalVelocity` (ship-local, same signs as the stick) is the damping
     input. Bank to turn works with bank = -phi, where phi = atan2(x, y) of the target in ship
     coordinates: one flight held phi near 0 degrees. A re-decided-every-frame avoid manoeuvre
     yo-yos, so commit to it. Measured honestly: one flight with damping (KD 0.6), bank-to-turn
     and committed avoids hit 27% of primaries against 35% for plain proportional steering. Not a
     proven improvement; tune and compare over several runs before relying on it.
   - **Frame rate:** the software renderer runs about 20-28 fps, and a controller acts once per
     frame. Time compression divides its control rate by the same factor, so keep compression at
     100 while it flies.
   - **Missile lock is not exposed to Lua.** After firing, check that your missile's `HomingObject`
     is set. **Hull distance:** `ship:checkRayCollision(from, to)` returns the hit point; a capital
     ship's `Radius` is a bounding sphere and useless for range.
   - **Reloadable controller:** make the hook call a global (`A1.fly`) so running the file again
     replaces the logic without stacking a second hook.
6. **Collisions.** The bridge catches the player's contacts with ships, debris and asteroids
   (weapon hits are not contacts; the engine skips very light debris or asteroid brushes and
   never reports props to this hook). A contact counts once when it starts, not once per frame
   of grinding. `pilot` stops
   at once on a new contact: `stopped_because: "collision"`, and `collision.direction` is the
   contact point in the ship's own axes (`right`, `up`, `forward`, unit length; null if the
   engine gave no point). Steer the other way, reversing if the contact is ahead, then fly off:
   the escape flight is not stopped by the contact it is escaping, and its reply's `touching`
   says whether you are still in contact. `state` shows the contact under `player.collision` for
   5 mission seconds of the current run (`new` false means a contact still going on), with
   `player.collisions_total`, `player.touching` and `player.collision_errors` (above 0 means the
   collision hook failed; read `bridge_info` last_error).
   `wait_for` kind `collision` (any value) waits for the next new contact, which is how to catch
   one during autopilot or your own controller. `stop_on_collision=false` keeps a `pilot` flight
   going (a deliberate ram); its reply still counts `collisions`. The collision hook is always on
   in every engine the harness launches; it only reads, so engine behaviour is unchanged (worth
   knowing when the change under test is collision code).
7. **Telemetry** (off unless switched on; it costs work every frame; `on` starts a fresh
   recording, dropping earlier rows): `telemetry on` before the
   part of the run you want to measure, `telemetry read` (seconds up to 120, hz up to 10) for a
   summary and samples, `telemetry off` after. The summary has aim error to the target's centre
   (a controller leading a moving target is off-centre on purpose, so read it as a trend), the
   share of frames within 3 degrees, wiggle (turn-rate sign flips per second; under ~0.5 is
   smooth), peak turn rate, mean speed and contacts. In samples, aim and distance read -1 when
   there is no target; with no rows there is no summary, and fps and wiggle are null with fewer
   than two. "Per second" means per mission second (divide by the time compression for real
   time; `frame_stats` measures real frame times). Every reply carries
   `error` (an error switches telemetry off), `cleared` (a new mission run emptied it) and
   `last_row_t` beside `now` (a gap means rows stopped, e.g. no player ship). 60 s at 1 Hz is
   about 4.6 KB.

## Reproduce a multiplayer bug

Every multiplayer session runs inside a *match*: a private, loopback-only network that the LAN and
the internet cannot see (no port is ever opened on the real network; the online tracker is never
enabled). Create one, launch a server and clients into it, drive them like any session, remove it.

1. `match_create()` -> `{"id": "m...", ...}`.
2. Server: `launch(match=<id>, role="standalone")` (no window; `status` shows `listening`), or a
   player host: `launch(match=<id>, role="host", pilot="create:Host", mission="M-01.fs2")`.
3. Clients: `launch(match=<id>, role="client", pilot="create:Alpha", skip_movies=true,
   dismiss_popups=true)`; each gets the next free port and auto-joins the server. The first
   client of a standalone game is the game host; give it `mission="M-01.fs2"` (a multiplayer
   mission; retail coop missions are M-01..M-04) plus `skip_briefing=true` and the bridge takes
   it from the join screen through host setup, the briefing and the launch countdown into
   `GS_STATE_GAME_PLAY` with no human. A standalone takes no mission and no pilot.
4. `wait_for(id, state="GS_STATE_MULTI_HOST_SETUP")` / `..._CLIENT_SETUP` / `GS_STATE_GAME_PLAY`;
   then `sexp`, `lua`, `state`, `pilot`, `screenshot` as usual on each client.
5. Read both sides: `log(<client>)`; the standalone has no bridge, so read its
   `<prefs>/<mod>/data/multi.log` and `event.log` from the run folder (`status` shows the paths).
6. `match_remove(<id>)` stops every session in the match first, then the network.

Facts: a session's port is in `launch`'s result and a client's default `connect` is the match's
live server; `status` and `list` show `network: alive|dead` and `dead-network`. One server and
one mission-carrying session per match; give the mission to the first client of a standalone
game (a host given a mission starts it at once, before anyone can join). Host and client need
`pilot: "create:<name>"`. The `dismiss_popups` switch leaves the "Joining netgame" box alone
(its only button is Cancel) and a cancelled join is final. Hostnames still resolve inside a
match (the host's resolver answers over a local socket) but nothing can be sent to the answer;
every session, match or not, runs in its own loopback-only network (`network="real"` opts out). The
standalone's web panel (TCP 8080, basic auth admin/admin, engine defaults) exists only inside
the match and its mission/netgame copies were stale in testing; do not rely on it. Open
multiplayer issues: label `multi` upstream; the maintainers' checklist is #6927.

## Read a crash

`crash_report <id>`: exit signal, the core's backtrace (debug builds name the functions), the last
log lines and the problems in the whole log, the popups that came up, the last heartbeat.

## Things that will surprise you

- `sexp` refuses outside a mission (the engine evaluates even `( true )` as false there).
- `lua`, `post_event`, and any sexp containing "eval" need a session launched with `allow_eval`.
- In FSO Lua, the global `error()` ends the game. In `lua` commands the harness swaps it for a
  harmless one; avoid reading fields a handle does not have (that is a fatal engine error).
- qtFRED windows: `fred_*` tools handle focus and geometry; raw `click` coordinates are relative
  to the editor window (1280x720 by default). Dialogs and open menus are separate windows that a
  plain `screenshot` does not show: use `screenshot full=true`. To click a point seen in it:
  divide by `scale` (if you gave one), add the crop's X/Y (with `full`, crop is in display
  coordinates), then subtract the returned `window_origin`.
- `click` holds the button for 0.15 s: FSO's own menus ignore a press and release that fall
  inside one frame. `move` puts the pointer somewhere without clicking (hover) and reports where
  it really is.
- `wait_for` event/ship names must exist in the mission, and may not contain `"` `!` `(` `)`:
  in FSO's sexp text `"` ends a string, so such a name could smuggle in code. Sexps are capped
  below 8000 bytes (the engine aborts on longer ones).
- Exit codes worth branching on: 5 timeout (the message says whether the command was withdrawn
  or the bridge already took it, so do not retry a non-idempotent command blindly), 6 the engine
  is gone (`crash_report`), 7 refused (your input), 8 a Lua error inside the bridge or your code,
  9 the work failed (a compile).
- `pilot` is refused while the game is paused or a dialog is open; its reply says whether the
  flight completed and why it stopped. A timed-out `pilot` is cancelled for you.
- `status` shows `heartbeat_age_sec`; above 5 s the engine is busy (loading) or hung.
- `replay` only re-issues actions that succeeded the first time; the rest are listed in `skipped`.
- `pilot`'s `stopped_because` is one of `time`, `player_dead`, `mission_end`, `cancelled`,
  `paused` (the game paused or a dialog opened mid-flight) or `frame_limit` (a backstop on
  frames). A Lua error during a flight is not a stop reason: the call fails with exit 8.
- qtFRED: open and save missions only with `fred_open` / `fred_save`. Do not drive qtFRED's own
  File > Open or Save As dialogs with keys or clicks: they can reach the user's real missions.
