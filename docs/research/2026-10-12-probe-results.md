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
