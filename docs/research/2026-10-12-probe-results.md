# Probe results (Task 1), 2026-10-12

Engine: a release build (26.1.0, OpenGL + Vulkan) and a debug build
(26.1.0 debug, with qtFRED). Data: retail FS2 + `MVPS-4.7.3`. Mission `SM1-01.fs2` (command briefing +
briefing). Every run on a private Xvfb with a private preferences folder; the probe mod and the engine's
working directory live in a **shadow game folder** (symlinks to FS2PATH's directories and `.vp` files),
so nothing was written into the user's game folder.

Scripts: `probes/run_probe.sh` (engine), `probes/fred_ocr_probe.sh` (qtFRED). Probe table:
`probes/probe_mod/data/tables/probe-sct.tbm`. Lua API reference generated from this build:
`docs/research/scripting.lua` and `docs/research/sexps.html`.

Verdicts: CONFIRMED (works as the spec says), DIFFERENT (works, but by another detail the owning task
must use), UNTESTED (with reason). No result contradicts an approved spec mechanism, so no Proposal
was needed before any owning task.

## 0. Generating the API reference (`-output_script_lua -output_script_json -output_sexps`)
CONFIRMED with a change of location. The engine wrote all three files within 3 s and kept running.
FS2PATH already held a `sexps.html` (273,424 bytes, 02:19 today, origin not provable), and the plan's
`rm -f` would have deleted it, so the run used a shadow folder instead; the engine loaded retail data
through the links and wrote the outputs beside them; FS2PATH's file was untouched (mtime checked).
**Consequence for Task 19:** the shadow-folder approach works. `shadowdir.make` must link only
directories and `.vp` archives, never loose files: a link named `sexps.html` would let the engine's
write follow it into FS2PATH.

## 1. dkjson, loadstring, cf.listFiles
CONFIRMED. `require("dkjson")` returns a table; `loadstring` is a function; `cf.listFiles("data/cache",
"*.log")` returns a table. `cf.renameFile(Current, New, Path)` exists with that argument order.

## 2. Popups (spec risk 3) — DIFFERENT
First run: the "new settings file" popup closed, but the NEW USER TIP popup stayed up forever and
blocked `-start_mission` (the tip is shown inside main-hall init, before the start-mission check).
Root cause, `code/popup/popup.cpp:940-990`: `hv.Submit(n)` sets `choice` and `done`, but unless the
hook declares an override, the popup then runs `popup_process_keys`, which overwrites `choice` with
`POPUP_NOCHANGE` (100). The tips loop (`playermenu.cpp:1459`) repeats while the result is > 0, so it
shows the tip again every time. A single-button popup survives this by accident.
**Mechanism to use (Task 8):** answer a popup by calling `hv.Submit(n)` AND returning true from the
hook's `+Override:` block on that frame only (set a flag in the hook, read-and-clear it in the
override). With that, every popup in the run closed. Hook variables available: `hv.Text`, `hv.Title`,
`hv.Choices` (nil for these popups), `hv.IsDeathPopup`, `hv.Submit`.
Possible upstream report (Dustin's call, later): `Submit` without override is silently ignored.

## 3. Pilot creation and selection (spec risk 2)
- Create: DIFFERENT detail. `ui.PilotSelect.createPilot(callsign, is_multi, copy_from?)` and
  `selectPilot(callsign, is_multi)` need the `is_multi` argument: `createPilot("ProbePilot", false)`
  returned true; `selectPilot("ProbePilot", false)` moved to `GS_STATE_MAIN_MENU` the next frame.
- A fresh preferences folder shows a "new settings file has been created" popup in pilot select
  first; it is answered like any popup.
- Existing pilot: CONFIRMED. With the pilot files copied into the prefs folder and `-pilot ProbePilot`,
  the engine went straight to `GS_STATE_MAIN_MENU` at frame 1 (no chooser).

## 4. Skipping the command briefing and the briefing (spec risk 1)
CONFIRMED. In `GS_STATE_CMD_BRIEF`, posting `GS_EVENT_START_BRIEFING` returned true and the next
state was `GS_STATE_BRIEFING`. Five frames into the briefing, `ui.Briefing.commitToMission()`
returned `COMMIT_SUCCESS` and the mission started (`GS_STATE_GAME_PLAY`). `mn.isRedAlertMission()`
was false for SM1-01 (red-alert path not exercised).
Order note: `On Frame` can run in a new state before that state's `On State Start` hook is logged,
and both fire. The probe drove the switches from `On Frame` (polling the state name); that is the
proven path.

## 5. In-mission state
CONFIRMED. In `On Frame` during play: `hv.Player` is the player ship (`Alpha 1`, hitpoints 290),
`#mn.Ships` 6, `#mn.Goals` 1 (`isGoalSatisfied` 2 = incomplete), `#mn.Events` 62 with names.
`mn.getMissionFilename()` returns the name without extension (`SM1-01`).

## 6. Flying (pitch, heading, throttle, fire) — DIFFERENT
- Writing `ba.getControlInfo()` fields from `On Frame` alone does nothing (pitch 19 → 19).
  The engine only copies Lua control values after `ba.setControlMode(FLIGHT_CONTROL_LUA_FULL)`
  (`playercontrol.cpp:1139`). With that set, 60 frames at `Pitch = 1, Forward = 1` changed pitch
  19 → 35 (OpenGL) and 19 → 58 (Vulkan). Restore with `ba.setControlMode(FLIGHT_CONTROL_NORMAL)`.
- Firing: in the Lua control modes the fire counts are not copied (enum doc: "Weapon firing inputs
  are not affected"), and continuous buttons like FIRE_PRIMARY are read from hardware in
  `check_control`, so `ci.useButtonControl(-1, "FIRE_PRIMARY")` fires nothing. `hv.Player:firePrimary()`
  works: 16 player shots in flight after 30 frames (returns 0 while the weapon is cycling).
  `fireSecondary()` and `fireCountermeasure()` exist with the same shape.
- The button-control functions are plain functions, not methods: `ci.useButtonControl(...)`,
  `ci.getButtonControlNumber("FIRE_PRIMARY")` (= 17). Calling them with `:` raises a Lua error.
**Mechanism to use (Task 12):** set FLIGHT_CONTROL_LUA_FULL for the duration of a `pilot` command,
write Pitch/Heading/Bank/Forward each frame, call `firePrimary()` / `fireSecondary()` /
`fireCountermeasure()` for fire requests, restore FLIGHT_CONTROL_NORMAL when done (also on
mission end).

## 7. Time compression and pause (spec risk 4)
- CONFIRMED above 4x: `( set-time-compression 800 )` raised the average mission frame time from
  0.0185 s to 0.157 s, ratio 8.48 (OpenGL), 7.95 (Vulkan). The sexp also locks the player's own
  compression keys; `( reset-time-compression )` returns to 1x and unlocks (sexps.html).
- CONFIRMED pause: posting `GS_EVENT_PAUSE_GAME` moved to `GS_STATE_GAME_PAUSED`; `On Frame` keeps
  running while paused; posting `GS_EVENT_PREVIOUS_STATE` from there returned to play.
- `ba.getFrametimeOverall()` read 0 in every hook, so the bridge has no usable real-time clock;
  wall-clock comparisons are made on the Python side.

## 8. Vulkan session (spec risk 5)
CONFIRMED. Same probe with `-vulkan` and `VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/nvidia_icd.x86_64.json`:
identical state sequence, pilot, briefing skips, control, compression, pause; `import -window root`
captured the in-mission frame. (Which renderer actually ran is checked per launch from the loaded
libraries in Task 7; it was verified for this binary earlier today in the headless README.)

## 9. Errors and crashes in a debug build (spec risk 7)
- CONFIRMED: `ba.error("probe deliberate error")` under `-noninteractive` printed `Error: probe
  deliberate error` with a backtrace to stdout and to `<prefs>/data/fs2_open.log`, then the engine
  aborted (status 134, SIGABRT). `coredumpctl list` showed the core as present, size 683 MB.
  **Consequence for Task 18:** never copy the core into the run folder; read it in place
  (`coredumpctl debug <pid> --debugger-arguments=...` or gdb on `coredumpctl`'s path).
- NEW finding: in the debug build, `cf.openFile(name, "a", path)` on an existing file fails the
  assertion `raw_position == cfile->raw_position` (cfilearchive.cpp:52) and aborts. The release
  build appends silently. **Consequence for Tasks 6 and 8:** the bridge never uses append mode;
  logs are rewritten whole (bounded in memory) or written as numbered files.
- NEW finding: without `-noninteractive`, any Lua error in a hook opens a blocking error box (it
  appeared on the Xvfb, as the toolkit pinning intends). **Consequence for Task 6:** the bridge wraps
  its entire per-frame work in `pcall`, not just command handlers.
- The debug build also prints `WARNING:` lines to stdout (useful for `validate`); MediaVPs 4.7.3
  raises one at startup (legacy PSPEW spawn frequency, weapons.cpp:5104).

## 10. qtFRED error checker by OCR (spec risk 6)
CONFIRMED with details.
- qtFRED (debug) stops at startup on the MediaVPs warning box unless `-noninteractive` is passed;
  with it, the warning goes to stdout and the mission loads.
- The main window's title is the mission file name (or `Untitled`), never "FRED": find it by
  process id (`xdotool search --pid`). Default window size is small (926x351); resize it.
- No window manager: click a neutral spot (status bar) before sending keys.
- Ctrl+S on a mission whose event names a missing ship opens a window titled
  **"Pre-save Error Check"**: a severity legend (Critical Error / Error / Warning / Potential Issue,
  shown by the colour of each entry's left bar), the entry text with the offending sexp and
  `(Bad node appears to be: "Ghost 9")`, a count line `1 error(s) found.`, and buttons
  Fix and Save / Save Anyway / Cancel.
- OCR of the whole screen is noise; OCR of the dialog window alone is legible, and a 2x upscale
  fixes the remaining misreads ("Tota" → "Iota", "Joadout" → "loadout").
  Evidence: `docs/research/fred-checker.png`, `docs/research/fred-checker-ocr.txt`.
**Mechanism to use (Task 17):** find the "Pre-save Error Check" window, crop the screenshot to its
geometry, scale 2x, OCR; the count line decides clean/dirty; severity comes from the entry text
and can be cross-checked by the bar colour.

## 11. Sexp names the later tasks use
`set-time-compression`, `reset-time-compression`, `player-use-ai`, `player-not-use-ai`,
`is-event-true-delay`, `is-event-false-delay`, `is-destroyed-delay`, `get-object-pitch` exist.
DIFFERENT: there is no `is-departed-delay`; the operator is **`has-departed-delay`** (Task 13).

## 12. Shutdown
Sending SIGTERM to the engine moved it to `GS_STATE_QUIT_GAME` (it shuts down on its own path).

## 13. `mn.startMission` inverted check (research note, mission.cpp:1941)
UNTESTED: the design launches missions with `-start_mission` and never calls `mn.startMission`, so
the result could not change any task.

## 14. Startup files in the channel folder (found in Task 6's real-engine smoke test)
DIFFERENT. A `switches.json` placed in `<prefs>/fso_harness_bridge/data/cache` before launch was gone
by the first heartbeat, as was a `marker.json`; `marker.txt` and an extensionless `allow_eval`
survived. Cause, `code/graphics/opengl/gropenglshader.cpp` `opengl_purge_shader_cache_type`: when a
mod is loaded, OpenGL startup deletes every `*.json` and `*.bin` in mod cache folders (cleanup of an
old shader-cache location), before scripting's On Game Init. **Mechanism to use:** the launch-time
switches file is `switches.txt` (JSON content); command/reply files may stay `*.json` because the
harness sends commands only after the first heartbeat. Verified: with `switches.txt` + `allow_eval`
the bridge's `bridge_info` reported both. This likely also explains the spike's "commands written
during loading were lost".

## 16. Sexps outside a mission, and Lua-running sexps (Task 11 follow-up)
- In pilot select, `mn.evaluateSEXP("( true )")` and `( + 2 3 )` both returned false. Sexps have no
  meaning outside a mission, so the bridge refuses them there.
- This build's sexps include operators that run Lua: `script-eval`, `script-eval-block`,
  `script-eval-bool`, `script-eval-num`, `script-eval-string`, `multi-eval`. A background security
  review of the Task 11 commit pointed out that `sexp` could therefore bypass `--allow-eval`; both
  fsoctl and the bridge now refuse those operators without the opt-in, and fsoctl audits them.
  Without the gate, `( script-eval-num !return 1! )` sent in pilot select left the bridge silent for
  10 s (not investigated further: the gate and the not-in-mission refusal both stop it first).

## 17. Found during the HIGH-RISK QA and its fix wave (2026-10-12)
Sources: the QA record (kept outside this repository).
- **Sexp quoting.** run_sexp turns a single `!` into `"` and `!!` into `!` before parsing
  (sexp.cpp:31241-31255); `"` is the real string delimiter. A caller value with `"` inside a
  `!name!` ends the string early. `is-event-true-delay` evaluates its delay argument with eval_num
  (sexp.cpp:19039), so an operator smuggled there runs. Probed on the debug engine: a
  `wait_for event_true` name carrying `script-eval-num` ran its Lua on a session without
  --allow-eval, then the engine aborted (SIGABRT) on the malformed remainder.
- **Sexp length.** run_sexp copies the text into a 8192-byte buffer with strcpy_s
  (sexp.cpp:31237-31239); a longer sexp is a fatal Error(), i.e. an abort under -noninteractive.
- **Lua compile errors in script-eval-num abort** under -noninteractive (seen in the SA-1 red runs:
  uncaught exception, terminate). The `lua` verb is not affected (the bridge compiles caller code
  inside pcall); a typo in a `sexp` that uses script-eval-num on an --allow-eval session is.
- **cf.renameFile's return is inverted:** false on success, true on failure (`cf_rename(...) != 0`
  with CF_RENAME_SUCCESS = 0). It is rename(2) underneath (cfile.cpp:553), so it replaces the
  target atomically on Linux. The bridge checks for the renamed file rather than the return value.
- **cf only sees indexed extensions per folder type** (cfile.cpp Pathtypes): data/scripts indexes
  .lua/.lc/.fnl only (:86), data/text indexes .txt/.net (:56). A .txt under data/scripts is invisible
  to cf.openFile; the bridge's version file lives in data/text.
- **profiling.csv covers gameplay frame flips only:** the MainFrame trace begins and ends in
  game_enter_state / game_leave_state for GS_STATE_GAME_PLAY (freespace.cpp:6232, :5572) and is
  split per flip (:3719-3720). One probe (release build, software GL, SM1-01): 207 rows summing to
  9.0 s, while mission time read just after gameplay began was already 1.79 s; mission time then
  advanced at about wall-clock speed.
- **ba.getFrametimeOverall** returns seconds (ade 'x' format converts from fix) and is a running sum
  of per-frame times from engine start (freespace.cpp:4642), not wall time.
- **Unknown names in check sexps:** mn.Events / mn.Ships indexers fall back to atoi(name), so a
  lookup by an unknown name can return a valid handle; match on the handle's .Name instead.
- **coredumpctl's `time`** is the journal entry time, which trails the crash (7 s for a 700 MB
  core); COREDUMP_TIMESTAMP holds the crash time.
- **systemd user manager on this machine:** DefaultTimeoutStopUSec=45s; a stop that times out
  leaves the scope "deactivating"; `systemctl --user kill --signal=SIGKILL` then takes it to
  inactive; DefaultOOMPolicy=stop, so a scope OOM reaches the runner as SIGTERM.
- **Mission lookup order** (cfilesystem.cpp:574-608): each -mod folder, then that folder's
  archives, before the next mod; FS2PATH and its archives last; first match wins; within one folder
  a loose file beats an archive.

## 18. Found during the SM1-01 scripted-playtest demo (2026-10-12, afternoon)
- **FSO's UI ignores a sub-frame click.** ui_mouse_process samples the left button once per
  frame and only then folds in the down/up counts (code/ui/uimouse.cpp:43-58): a press and a
  release inside one frame leave b1_status RELEASED after a RELEASED frame, so neither
  BUTTON_JUST_PRESSED nor BUTTON_JUST_RELEASED is set (:67-75). Three `xdotool click`s on the
  debriefing's Statistics tab did nothing; a press held across frames (0.9 s via drag, then 0.15 s
  via the new held click) switched the tab every time. qtFRED is unaffected either way.
- **Stopping the scope aborted the engine.** `systemctl --user stop` SIGTERMs every process in the
  scope at once, Xvfb included. Core backtrace: UI_WINDOW::process -> SDL event poll ->
  _XIOError -> exit() -> exit handlers -> SDLGraphicsOperations dtor -> XCloseDisplay -> a second
  _XIOError -> exit() -> PathRenderer dtor -> nvgDeleteTexture -> free() -> abort (SIGABRT, a
  12-34 MB core per stop). SIGTERM to the runner alone (it reaps the engine, then ends Xvfb):
  engine exit code 0, no core, scope gone in about 2 s.
- **qtFRED's save runs the error check itself** (qtfred/src/ui/FredView.cpp:433) and shows the
  dialog only when errors were found (:439-442); potential-issue checks are switched off for that
  run (:427). A save that goes through is therefore a clean check for errors, not for potential
  issues (Tools > Error Checker, Shift+H, runs those).
- **Qt popup menus and qtFRED dialogs are separate top-level windows.** `import -window <editor>`
  shows an open menu as a black box and leaves dialogs out; `import -window root` shows both. The
  Xvfb screen is the requested height + 80 px (runner._XVFB_EXTRA_HEIGHT).
- **Scripting facts used by the demo pilot:** `ba.getGameDifficulty()` is 1-based
  (Game_skill_level+1, scripting/api/libs/base.cpp:506; a new pilot defaults to index 1, Easy,
  freespace2/freespace.cpp:259); `orientation:rotateVector` maps world to ship-local; under
  FLIGHT_CONTROL_LUA_FULL, +Heading turns the nose right and +Pitch pushes it down;
  `engine.addHook("On Frame", fn)` works from the `lua` verb; `loadfile` is available;
  mission_goal has `isGoalSatisfied` (0 failed, 1 complete, 2 incomplete) and no `Status` field.
- **qtFRED ignores SIGTERM** because it starts SDL (os_init at qtfred/src/mission/management.cpp:71
  -> SDL_Init at code/osapi/osapi.cpp:424) and SDL3
  installs a SIGINT/SIGTERM handler that only posts a quit event, which a Qt app never reads. Under
  the runner-first stop it sat out the 20 s grace and ended in SIGKILL. SDL_NO_SIGNAL_HANDLERS=1
  (SDL_hints.h, SDL_HINT_NO_SIGNAL_HANDLERS) in qtFRED's environment: SIGTERM ends it in well
  under 10 s. The engine keeps SDL's handler: it reads the quit event and shuts down on its own path.
- **Negative pointer coordinates** (QA CR-4): `xdotool mousemove --window <id> -40 12` fails with
  "unrecognized option '-40'"; with `--` before the coordinates it works (a negative y landed above
  the window as expected). The game window sits at display (0,40) under Xvfb, so the top 40 px of
  the display are at negative window y.
- **Runtime limit aborted the engine too** (QA SL-1, probed): a session left to reach RuntimeMaxSec
  ended with ended_by "runtime_limit", SIGABRT and an 11.8 MB core: systemd stops the whole scope,
  Xvfb included. Fixed by letting the runner end the run at runtime_sec (engine first, Xvfb last)
  with RuntimeMaxSec = runtime_sec + 60 as the backstop.
- **FSO's double click** is two presses within 250 ms as sampled per frame (ui/uimouse.cpp:68),
  stricter than Qt's 400 ms; `click double=true` uses 0.08 s holds and gap in one xdotool process.

## 19. Found flying SM1-01 from a Lua controller (2026-10-12, evening demo)
Everything here was observed in a session or read at the cited line; "observed" means it happened
on this build (26.1.0, release, llvmpipe under Xvfb).
- **pcall does not contain API errors.** A missing index (`ba.getControlMode`; `.Name` on a
  `weapon` handle) or a wrong argument type throws `os::dialogs::LuaErrorException`; under
  `-noninteractive` it reached `std::terminate` and SIGABRT, even though the call sat inside
  `pcall` (observed 3 times: engine.log "terminate called after throwing an instance of
  'os::dialogs::LuaErrorException'"). Only plain Lua errors are catchable.
- **control_info functions take no self.** `useButtonControl` parses `"i|s"` (control_info.cpp:354),
  `getButtonControlName` `"i"` (:387), `getButtonControlNumber` `"s"` (:401). The generated docs
  present them as methods; `ci:useButtonControl(-1, "X")` passes the handle as argument 1 and aborts
  the engine (observed); `ci.useButtonControl(-1, "X")` works (observed).
- **Lua button presses.** Control names are `plr_commands` (control_info.cpp:9). With
  `BUTTON_CONTROL_LUA_ADDITIVE` the Lua bits are OR'd into the frame's button bits and then the
  merged set is copied back into the Lua buffer (io/keycontrol.cpp:1773-1777), so call
  `clearLuaButtonInfo` every frame or a press repeats. `button_info_do` runs one-shot controls from
  those bits (observed: SHIELD_EQUALIZE, INCREASE_WEAPON/ENGINE, END_MISSION all acted). Held
  controls (FIRE_PRIMARY/SECONDARY, AFTERBURNER: playerman/playercontrol.cpp:796, 898, 911) go through
  `check_control`, whose `check_control_used` reads keyboard, joystick and mouse state only
  (controlconfig/controlsconfig.cpp), so Lua bits never reach them.
- **Trigger under FLIGHT_CONTROL_LUA_FULL.** `copy_control_info` copies axes and throttle but not
  `fire_primary_count`/`fire_secondary_count` (playercontrol.cpp:1035-1055); use
  `ship:firePrimary()`, which calls `ship_fire_primary` (scripting/api/objs/ship.cpp:1655).
- **ETS taps.** Two INCREASE_WEAPON taps from 4/4/4 gave 2/2/8 (engine/
  shield/weapon index, observed); one INCREASE_ENGINE then gave 4/1/7.
- **Turn dynamics.** Desired turn rate = stick x max turn rate per axis, in the ship frame
  (physics/physics.cpp:497-498, :539); `Physics.RotationalVelocity` is that local vector
  (physics_info.cpp:149). +bank rolls the ship's right axis toward its up axis, so the target's
  on-screen angle phi = atan2(x, y) grows; bank = -k*phi rolls a target to "up" (derived from the
  axis cycle, then observed: phi held at 0.2-0.25 degrees during a hard turn in the test flight).
- **Frame rate.** The `pilot` verb applied 111 frames in 4.0 mission seconds and 229 in about 12 s
  at 1x (about 20-28 fps, software rendering). Time compression divides a frame hook's control rate
  per mission second by the same factor.
- **Player missile lock.** HUD lock needs the lock point inside dot > 0.85 of the nose
  (hud/hudlock.cpp:539) and in weapon range (:690), held for the weapon's lock time; not readable from
  Lua. `weapon.HomingObject` read the frame after launch tells whether a missile is homing
  (observed: Rockeyes "homing", Tempests "NOT homing").
- **Friendly fire** (ai/aicode.cpp:16177-16280): each friendly hit counts weapon damage divided by
  min(100, max hull / 1000) (x ship-type FF multiplier, x 1/5 if the hit ship is not your target),
  capped per burst at MAX_BURST_DAMAGE 20 (ai.h:490). Any gap over 2 s between hits subtracts the gap
  length in points; 60 s without a hit resets. Warnings above 50 (FRIENDLY_DAMAGE_THRESHOLD,
  aicode.cpp:16102); above 50 x (1 + (6 - Game_skill_level)/3) (133 at Easy) it sends "All units,
  terminate the traitor!", calls `mission_goal_fail_all()` and sets the player's team to Traitor
  (observed at about t=700 after ~830 hits on the Psamtik from a station 500-750 m off its hull).
- **Retail SM1-01 has duplicate event names** ("Return To Base" and "Protect Iota" twice each), so a
  name-based `wait_for` sees whichever comes first.
- **Hull distance.** `ship:checkRayCollision(from, to)` returns the world hit point (or nil); a
  destroyer's `Radius` (Psamtik 1609 m) is its bounding sphere, so being "inside" it is normal.

## 20. The "On Ship Collision" hook (probed 2026-10-12, session sd270dbee; live test s88406660)
- **It fires for weapon hits too.** In a dogfight it ran 84 times in a few seconds with `Self` a
  weapon, `Weapon` set and `Ship` the ship hit (Leo 2). A ship-ship contact fires it twice in one
  frame, `Ship` and `ShipB` swapped (Alpha 1 / Iota 2, then Iota 2 / Alpha 1).
- **Reading its variables is safe.** The `hv` indexer returns nil for a variable this hook did not
  set (scripting/api/libs/hookvars.cpp, ADE_INDEXER l_HookVar). `hv` is userdata, so `rawget(hv,
  k)` raises. `Ship`/`ShipB` are ship-typed (their `.Name` exists); `Self`/`Object` are
  object-typed; `Hitpos` is a vector (no `isValid`).
- **Hitpos is the world contact point.** A nose-on ram read (0, -6, 1) m in the player's own axes.
  A 61 m/s ram cost 8 hull points; head-on contacts in the live test cost about 2%.
- **A freighter early in SM1-01 moves at about 52 m/s**, so a straight run at its centre misses;
  stage contacts head-on in its path.
- **Grinding fires it every frame** (QA SLA-1/CR-1, source): an overlapping pair is re-tested each
  frame with no cooldown (collideshipship.cpp:1525/1541 `next_check_time = timestamp(0)`), so the
  bridge counts a contact when it STARTS (nothing of that object touched us the frame before).
- **Not every contact reaches the hook** (source): debris and asteroid contacts with impulse below
  0.5 return before it (collidedebrisship.cpp:110-111, :308-309); prop contacts only ask the hook
  whether it overrides and never run it (collideshipship.cpp:1664-1690), so they never reach a
  handler that does not override. All run sites pass Hitpos (collideshipship.cpp:1421/1433,
  collidedebrisship.cpp:193/380).
- Bridge (fsoharness-sct.tbm `$On Ship Collision`): player contacts without Weapon/Beam, counted on
  contact start; `pilot` stops on a new one; `state` and `wait_for collision` report it. Engine
  runs: the live-test record kept with the QA record outside this repo (one mission,
  one freighter; rows say which cells ran).
