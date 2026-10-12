# Engine facts for the FSO harness (2026-10-12)

Source: FSO master a1250d716 (a clean checkout), read by an Opus Explore agent
and spot-checked; "spike" = run on this machine 2026-10-12. Paths relative to the FSO repo.

## Verified by spike
- A mod's Lua script (`data/tables/*-sct.tbm`, `#Conditional Hooks`, `$On Frame`) can poll for a command file with
  `cf.fileExists(name, "data/cache")`, read it with `cf.openFile(name, "r", "data/cache")`, delete it with
  `cf.deleteFile`, and append a reply with `cf.openFile(name, "a", "data/cache")`.
- With `-mod a,b,c` and FSO_PREFERENCES_PATH=P, path "data/cache" resolves to `P/a/data/cache` (the FIRST mod).
- On Frame fired in GS_STATE_MAIN_MENU, GS_STATE_START_GAME, GS_STATE_GAME_PLAY; replies carried mn.getMissionTime()
  advancing (2.05 s, then 12.04 s). Commands written during the ~55 s load (software GL, MediaVPs) were never seen:
  the engine wasn't running frames yet and a single command file was overwritten => numbered command files and a
  ready heartbeat.
- `-pilot TestPilot1` skipped the pilot chooser (states began at GS_STATE_MAIN_MENU).
- `.tbm` files treat `;` as a comment: never put `;` on a Lua code line.
- Vulkan: works headless on the NVIDIA ICD into Xvfb; Mesa lavapipe is rejected (VulkanRendererSetup.cpp
  isDeviceUnsuitable rejects CPU device types). Building the Vulkan renderer needs libshaderc-devel.
- Error boxes: SDL shows them via zenity/kdialog, which reach a Wayland desktop through the default socket unless
  GDK_BACKEND=x11 QT_QPA_PLATFORM=xcb are exported (verified the box then lands on Xvfb).
- Input: XTEST keys reach SDL for Enter; F-keys sent via XTEST never reached SDL in an earlier lab test; synthetic
  XSendEvent keys are ignored. Prefer in-game Lua actions over keys.
- qtFRED: Delete prompts per referenced object; Ctrl+S after breaking references is blocked by the error checker;
  after closing a modal dialog with no window manager, click a neutral spot before wheel events.

## From source (not yet run)
- Hooks: On Frame (inside gr_flip; skipped while a popup is up and in GAME_PLAY outside GM_IN_MISSION); On Dialog
  Frame (popups; hv.Submit(choice) resolves them, popup.cpp:947-958); On Simulation; On State Start/End; On Game Init;
  On Mission Start; On Mission End.
- State: ba.getCurrentGameState().Name; ba.postGameEvent(ba.GameEvents["GS_EVENT_X"]).
- Skips: briefing commit = ui.Briefing.commitToMission() (ui.cpp:879; loadout validation can raise blocking popups);
  command briefing -> post GS_EVENT_START_BRIEFING (GS_EVENT_RED_ALERT for red-alert missions); main hall ->
  -start_mission, or mn.startMission(name, briefing) in MAIN_MENU (its existence check at mission.cpp:1941 looks
  inverted: probe before use); pilot -> -pilot <name>, or ui.PilotSelect.createPilot/selectPilot (ui.cpp:368/395);
  -benchmark_mode auto-accepts the pilot and quits when gameplay ends. A startup warning popup (>10 warnings or any
  error) appears before the -pilot check (playermenu.cpp:395-400).
- Queries: mn.getMissionTime, mn.isInMission, mn.getMissionFilename, mn.Ships (Position, Orientation, HitpointsLeft,
  Shields, Team, Orders, Target, Class), hv.Player (set each gameplay frame), mn.Goals[x].isGoalSatisfied (0 failed,
  1 complete, 2 incomplete). Events have NO status field: use sexps is-event-true-delay / is-event-false-delay /
  is-event-incomplete. mn.evaluateSEXP (bool), evaluateNumericSEXP, runSEXP (wraps in `when`). dkjson is built in
  (`require "dkjson"`).
- Flying: ba.getControlInfo() Pitch/Heading/Bank/Vertical/Sideways/Forward/ForwardCruise/PrimaryCount/SecondaryCount/
  CountermeasureCount (control_info.cpp:141-276); player-use-ai / player-not-use-ai sexps.
- Time: set-time-compression <percent> / reset-time-compression sexps; pause by posting GS_EVENT_PAUSE_GAME;
  -no_unfocused_pause. NO fixed timestep (frametime from the wall clock, freespace.cpp:4526-4620).
- Randomness: global mt19937 reseeded only with -seed N; particles, decals, sounds and some ship systems use
  std::random_device => exact replay is impossible without engine changes.
- Validation: no parse-only flag. -noninteractive: Warning -> log+stderr and continue (release builds compile Warning
  out), Error -> log+stderr then abort (no stack trace); Message() and in-game popups still block. Logging only in
  debug builds (or FSO_RELEASE_LOGGING). -stdout_log sends the log to stdout. ui.PilotSelect.WarningCount/ErrorCount.
- Crashes: no Linux signal handler in the engine (core dump, nothing in the log); Assert (debug) -> Error with stack
  trace -> abort under -noninteractive; Int3 logs a trace then SDL_TriggerBreakpoint (likely SIGTRAP).
- Docs flags: -output_sexps / -output_scripting / -output_script_json / -output_script_lua write to the working
  directory and do NOT exit. -pofspew writes data/pofspew.txt in the first writable root and exits.
  -profile_write_file writes profiling.csv (end_ts;duration per frame) to the working directory.
- qtFRED: no headless error check; ErrorChecker needs Editor + EditorViewport + FredRenderer. It runs after load and
  before save (FredView.cpp:3024-3048, 413-471). qtFRED installs a SIGSEGV stack-trace handler (main.cpp:79-86).
- Lua sandbox note: io.lines/io.input/io.output open regular files at any path (liolib.c:205, 257) although io.open
  is restricted to FIFOs/char devices. The harness must NOT rely on this (unintended gap; may be closed upstream).
