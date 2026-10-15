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
     shapes it can see in the text: an unknown library function such as `ba.getControlMode`, a
     colon call on the no-self `control_info` functions, and a capitalised field or a method that
     no class or library in the build's API has (`Stats.MissionPrimaryFriendlyHits`, real name
     `...FriendlyHit`), including in files loaded with `loadfile("/abs/path")`, whenever
     `api_docs` is cached for the build AND its mod list (the reply's `lua_check` says `passed` or
     `skipped: <why>`; run `api_docs` with the session's `mods` if it says skipped). It cannot see
     handle types: a real name on the wrong handle still passes (`Linked` is a field of the bank
     LIST, `weaponbanktype`; read on one `weaponbank` it ended the session), so check which
     `@class` a name belongs to in `api_docs` `scripting.lua` before using it. Keep reads you repeat in a
     checked file and `loadfile` it. Compare handles with `getSignature()`, never `.Name`:
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
   - **Start from `examples/pilot/`** (pilot.lua, a drill, and what was measured with it, with
     sample sizes) rather than writing a controller from scratch. Its rules (sources in
     probe-results section 22; the README says which were measured and which come from source):
     - **Lead with the target's absolute velocity.** Lasers do not inherit your velocity (retail
       ai_profiles default). Subtracting your own velocity, the obvious-looking solve, aims
       ~30 m off at 500 m (the old controller: 0 hits in 30 shots on the drill).
     - **Aim from the gun mount.** FS2 guns do not converge; point the nose at the intercept minus
       the mount offset. Learn it from your own bolts, back-projected to the moment of firing and
       sampled only while you are hardly turning or sliding (otherwise it is biased ~1 m).
     - **Add the target's acceleration** to the lead (p + v t + a t^2/2, a from its velocity
       history). On the weaving drone this took hits from 26% to 65% (one run each).
     - **Steer for the turn-rate lag.** Turn rate = stick x max rate, reached with time constant
       `$Rotdamp` (0.35 s on a Myrmidon). Command rate = own rate + Kp x error + (1 + Kd) x
       d(error)/dt; own rate + d(error)/dt is the line-of-sight rate, so a crossing target is
       tracked without a steady lag. Smooth d(error)/dt with a time constant, not a per-frame
       weight. Cancel yaw-induced roll with bank = heading x max yaw x `BankingConstant` / max roll.
     - **Throttle for turn radius.** Turn rate does not depend on speed, so slower turns tighter:
       a third throttle when the target is >60 degrees off the nose; on its tail, hold its speed
       plus a little to close to ~250 m.
     - **Heat seekers (Rockeye) need no lock;** Lua cannot see lock or homing type. Fire one at a
       time per target: at bombers and slower-turning ships within 20 degrees; at fighters that
       turn as fast as you only from astern. Countermeasures are checked against missiles only in
       the 2 frames after launch and only within 300 m, so drop them when the missile is close.
     - Bank to turn beyond ~45 degrees (bank = -phi, phi = atan2(x, y) in ship coordinates), and
       commit to an avoid manoeuvre instead of re-deciding it every frame (it yo-yos).
     - **A mission restart keeps your Lua state but sends the clock back toward 0** (and object
       signatures restart): reset every stored timestamp when `mn.getMissionTime()` goes backwards.
     World to ship-local: project onto `getRvec/getUvec/getFvec` (cannot be mixed up).
     `Physics.RotationalVelocity` is ship-local with the stick's signs.
     - **Link the gun banks** (Cycle Primary Weapon Forward, ".", steps bank 1 -> bank 2 ->
       linked). Ships default to bank 1 alone. `ship.PrimaryBanks.Linked` reads it: `Linked`
       belongs to the bank LIST (`weaponbanktype`), and reading it on one `weaponbank` aborts
       the engine.
   - **Mission types** (examples/pilot README has the settings; measured at Medium, 2026-10-15):
     - **Escort:** order the wing first. Target the escorted ship, press Protect My Target
       (`PROTECT_MESSAGE`), then pick "All Fighters" from `mn.CommItems` (`:selectItem()`, the
       menu a player answers with a number key). Keep the escorted ship targeted until the order
       is SENT, a frame after the pick: the order uses the player's target at that moment
       (hudsquadmsg.cpp:1305), and "protect" on a hostile sends nothing. Check it landed:
       `wingman.Orders[1]:getType()` reads ORDER_GUARD. Then: bombs homing on the escort first,
       then whoever is shooting it (the stock guard AI's rule), then bombers. Solo, one fighter
       lost the freighter in all 14 runs; with an ordered wing it survived 15 of 17.
     - **Strike:** gun down the target's `weapons` subsystem before bombing (set
       `ship.TargetSubsystem`, like the target-subsystem key): with it destroyed, turrets stop
       firing at bombs (aiturret.cpp:803-805). Release bombs inside ~1 km. With both, 3/3
       cruisers destroyed with little damage taken; releasing at 900 m without disarming, 1/2
       (and the pilot down to 12% hull in the other); from 2 km, every bomb died to turrets.
     - **Scan / stealth:** a scan needs the HUD distance (to the target's bounding box,
       `hu.getTargetDistance`) under max(150, radius + 100) x the scanner's range multiplier,
       the nose within dot 0.95 of the centre, held for the scan time (playercontrol.cpp:1768).
       Arrive slowly (closing speed tied to distance left), scan the nearest unscanned ship
       first (transports leave), and keep clear of patrols: 3/3 drills scanned everything
       undetected, 0/3 without avoidance. Stealth means never firing: some missions read the
       fire keys. From FSPort "Playing Judas" (all goals on Medium after 13 runs):
       - A ship that warps in does not wait at its arrival point: the Eva destroyer crossed the
         area at up to ~300 m/s and parked near its exit for ~20 s. Wait where it parks, not
         where it arrives.
       - Check the ROUTE to a waiting point against patrol tracks, not only the point. A patrol
         fighter faster than you (88 vs 75 m/s) cannot be outrun once it heads your way.
       - Give the controller something to do when there is nothing to do: a default cruise
         drifted into a sentry's detection range while waiting for the next ship.
       - Read the mission's own debriefing tips (`#Debriefing_info`, the `$Recommendation text`
         lines, in the .fs2) before planning: they are the designers' hints.
   - **Diagnose a failed run before changing the pilot.** Each fix that worked in Playing Judas
     came from one observation, not from a guess:
     - When did an event fire? `( is-event-true-delay !Name! N )` is true once the event has been
       true for N seconds: bisect N to find the moment (the detection was 381 s, when one patrol
       fighter crossed 586 m).
     - Where was everything? A second `On Frame` hook that appends one row a second (positions of
       the player and the ships that matter) to a global table, read back with `lua`. FSO's `io`
       library is its own (no `io.open`); return the rows as text.
     - Which rule steered? Log the controls actually sent after every override, plus the angle
       between the nose and the target. A rule you did not suspect (a 0.66-throttle default, an
       avoidance push) shows up there.
     - Load your setup from a file with `loadfile` so every run starts identical, and run each
       iteration as a fresh session: a mission restart keeps Lua state you may have forgotten.
     - **`checkRayCollision(from, to)` casts THROUGH `to` to infinity** (MC_CHECK_RAY,
       model.h:1370). Check the hit's distance yourself or a ship far along the nose line reads
       as "about to hit".
   - **Frame rate:** the software renderer runs about 20-35 fps (it varies with machine load;
     record it per run), and a controller acts once per frame. Time compression divides its
     control rate by the same factor, so keep compression at 100 while it flies.
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
