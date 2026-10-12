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
