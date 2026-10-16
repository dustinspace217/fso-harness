# FSO harness — design spec

## Status (updated 2026-10-12)
Phase: design approved; implementation plan next
Done: spec approved by Dustin 2026-10-12 ("1. Approve."); brainstorming sections 1-2 approved; tool list approved incl. all nine extras; engine research + bridge spike
Next: writing-plans -> implementation plan -> Dustin picks the execution method
Blocked: nothing

## 1. Purpose (Dustin's words)
- "the point of this is to enable and equip LLMs to autonomously test and trial-and-error code changes, mod creation,
  campaign/map creation, feature work, etc. It doesn't necessarily need to serve folks without LLM tools, but if it
  can, that's a nice side benefit."
- "I want to showcase it as a project but I want it filled in to FSO so other FSO contributors can use it."
- v1 drives the game AND qtFRED. "There should also be flags/tools for each of the following: skip briefing,
  auto-create and/or auto-select pilot, skip the opening cutscene, launch right into a mission skipping command
  briefing and mission briefing. That way, any stage of the process can be skipped, or not, depending on what needs
  testing."
- On scripted piloting: "you could even play missions."
- Tool list approved verbatim: "All 9. Do it. And everything necessary to get it done right."

Success: an agent with only this MCP server can build an FSO worktree, launch it on a mission (OpenGL or Vulkan),
skip any startup stage, read state and the log, fly or hand control to the AI, speed time up, wait for conditions,
compare two builds or two renderers, validate a mod, get a crash report, and drive qtFRED, all without a window on
the user's desktop and within resource ceilings. Prior art: none (GitHub search 2026-10-12).

## 2. Non-goals (v1)
Multiplayer; video capture; exact deterministic replay (impossible without engine changes, see research §Randomness);
Windows/macOS hosts (Linux only, documented); a GUI; driving FRED2 (MFC, Windows only).

## 3. Architecture
```
agent (any MCP client)
  -> fso-harness MCP server (thin; one tool = one fsoctl call)
       -> fsoctl (Python library + CLI; all logic lives here)
            -> per session: systemd --user scope "fso-harness-<id>" containing
                 Xvfb :N (dynamic display) + engine or qtFRED, cwd = FS2PATH
            -> run folder $XDG_RUNTIME_DIR/fso-harness/<id>/ : session.json, prefs/, logs, shots/, audit.log
  <- bridge mod "fso_harness_bridge" (Lua, first in -mod list) inside the engine, polling the command channel
```
- No daemon. Session state is session.json + the scope; every fsoctl call reads it fresh (pattern from an earlier harness;
  avoids the stale-broker class seen in an earlier server).
- Language: Python >= 3.11. Core is stdlib only. The MCP layer uses the official `mcp` SDK (FastMCP) as an optional
  extra. Packaged with uv; ruff + pytest from the first commit.
- Repo: this repository. Two homes later: Dustin's public showcase repo, and an upstream contribution
  of the same tool to FSO (`scripts/headless/` or wherever maintainers prefer), issue-first.

## 4. Session lifecycle
- `launch` allocates a session id, a run folder, a private preferences folder (FSO_PREFERENCES_PATH), an X display
  (Xvfb -displayfd), installs/refreshes the bridge mod into FS2PATH/fso_harness_bridge, and starts the engine in a
  scope with MemoryMax/CPUQuota/TasksMax and a runtime limit (default 30 min).
- Environment always set: DISPLAY, SDL_VIDEODRIVER=x11, GDK_BACKEND=x11, QT_QPA_PLATFORM=xcb, WAYLAND_DISPLAY unset,
  LIBGL_ALWAYS_SOFTWARE=1 for OpenGL, VK_ICD_FILENAMES only if the caller sets it (no vendor default), FSO flags
  -window -res WxH -nosound -no_unfocused_pause -noninteractive (default; switchable) plus caller flags. (-nosound became switchable 2026-10-15: `launch --sound`, README "Sound".)
- Renderer check after the window appears: report the graphics libraries from /proc/<pid>/maps and classify
  (mesa GL / NVIDIA Vulkan / other); `launch` fails loudly if Vulkan was requested and the engine fell back.
- `stop` stops the scope (kills everything in it), keeps the run folder for inspection; `cleanup` removes run folders.
- Several sessions may run at once; each owns its display, prefs and scope.

## 5. Startup stage switches (all default OFF = engine behaves normally)
| switch | mechanism |
|---|---|
| skip_movies | `-nomovies` |
| pilot = existing NAME | `-pilot NAME` (requires the pilot in the session prefs; `launch` can copy one in) |
| pilot = create NAME | bridge in pilot select calls ui.PilotSelect.createPilot + selectPilot |
| dismiss_popups | bridge answers popups in On Dialog Frame with their default choice and logs each one's text |
| skip_mainhall MISSION | `-start_mission MISSION` |
| skip_command_briefing | bridge posts GS_EVENT_START_BRIEFING (or GS_EVENT_RED_ALERT for red-alert missions) |
| skip_briefing | bridge calls ui.Briefing.commitToMission(); a blocking validation popup is reported, not hidden |
Each switch is independent so any stage can be tested on its own.

## 6. Bridge mod and command channel
- `fso_harness_bridge/data/tables/fsoharness-sct.tbm` (no `;` on code lines) + `data/scripts/fsoharness.lua` loaded via
  require. Hooks: On Game Init (setup), On Frame and On Dialog Frame (poll + heartbeat), On State Start (stage switches).
- Channel folder: PREFS/fso_harness_bridge/data/cache/ (cf path "data/cache"; verified to resolve here).
- Harness writes `cmd-NNNNNN.json` atomically (temp name then rename). Bridge reads the next expected number each
  frame, deletes it, executes, writes `res-NNNNNN.json` via temp+rename. JSON via the built-in dkjson.
- Heartbeat `ready.json` every 30 frames: game state, mission time, frame count, mission filename, popup text if any.
  The harness only sends commands after the first heartbeat and treats a stale heartbeat as "engine busy/hung".
- Commands are small and synchronous per frame; long operations (pilot for N seconds, wait conditions) run as
  bridge-side timers and reply when done or timed out.
- `lua` (free-form eval) is refused unless the session was launched with `allow_eval=true`; every eval is appended
  to audit.log with the code. Same rule for raw `post_event`.
- The bridge uses only the engine's cf API, never the io.lines/io.input gap (research note).

## 7. Tools (MCP and CLI share names; every call has a timeout and returns JSON; errors say what to try next)
Run: `launch`, `status`, `stop`, `list`, `cleanup`.
Observe: `screenshot` (PNG, optional crop/scale), `log` (tail/filter; debug-build fs2_open.log or -stdout_log stream;
"problems" = warnings/errors/asserts extracted), `state` (game state, mission time, ships with position/orientation/
hull/shields/team/orders/target, goals, event status via sexps, player ship).
Act: `sexp` (run or evaluate), `pilot` (controls for a duration: pitch, heading, bank, throttle, fire), `autopilot`
(on/off via player-use-ai), `time` (compression %, pause, resume), `keys`/`click` (XTEST, for menus and qtFRED),
`lua` (opt-in), `answer_popup`.
Wait: `wait_for` (game state / event true|false / goal status / ship destroyed|departed / log regex / mission time),
always with a timeout.
Make: `build` (cmake --build a worktree inside a scope; returns compiler errors), `scratch_mod` (create a throwaway mod
folder in FS2PATH named fso_harness_scratch_<id>, registered for cleanup).
qtFRED: `fred_open`, `fred_save`, `fred_camera` (save, parse +Viewer pos/orient), `fred_check` (trigger the
error checker via save and read the dialog by OCR (tesseract) plus a screenshot; v1 limitation documented),
`fred_select` (scene browser / marquee helpers).
Extras:
1. `compare` — same scenario script on two builds; diff state snapshots (numeric tolerance) and screenshots.
2. `renderer_compare` — same scenario on OpenGL and Vulkan; diff frames + state.
3. `crash_report` — on abnormal exit: exit signal, coredumpctl info + gdb backtrace when a core exists, log tail,
   the last popup/Error text; debug build recommended.
4. `validate` — launch with -noninteractive and quit right after load (bridge posts GS_EVENT_QUIT_GAME); return
   warnings/errors and counts; optionally load a named mission. Warns that release builds hide Warning().
5. `api_docs` — run once per build with -output_sexps/-output_script_lua/-output_script_json in a temp working copy
   location, quit via bridge, cache outputs per build hash.
6. `vp` — list/extract from .vp archives (format: header, file data, directory; reimplemented, stdlib only).
7. `record` — every session logs its command stream; `replay` re-issues it. Documented as same-commands, not
   same-results (no fixed timestep; random_device sources). `-seed` passed when given.
8. `model_info` — run -pofspew in a scratch prefs dir and return the parsed per-model data.
9. `frame_stats` — launch with -profile_write_file, parse profiling.csv into percentiles.

## 8. Error handling
- Every external wait is bounded (P10 rule 2). Engine death is detected by the scope/PID, not by timeouts alone.
- Popups never hang the agent: On Dialog Frame keeps the bridge alive; their text is surfaced in state/heartbeat.
- A command that cannot run in the current game state returns a structured refusal naming the state and the fix.
- Session files written atomically; a crashed session leaves its run folder intact for crash_report.

## 9. Safety
- Nothing reaches the user's desktop: Xvfb only, toolkit pinning (§4), no window manager, no compositor contact.
- Resource ceilings on every engine/build process (scope); concurrency cap (default 3 sessions).
- The harness writes only inside its run folders, FS2PATH/fso_harness_bridge, and scratch mods it created; it never
  edits the user's real pilot/settings folder or mission files.
- Free-form Lua and raw event posting are opt-in per session and audited.


**QA amendment 2026-10-12 (HIGH-RISK review, findings SA-1..SA-10):** the eval opt-in covers what the harness itself sends (lua, post_event, sexps using the engine's built-in Lua-running operators, wait_for names; a Lua operator a mod defines need not contain "eval" and is not gated; names with `" ! ( )` are refused, sexps capped below 8000 bytes). It does not stop Lua that a mod or mission runs on its own, which is the thing under test. Sessions also get their own HOME/XDG folders and no desktop D-Bus/session-manager address, qtFRED launches refuse missions outside the harness's copy folder, and the audit log is persistent (`~/.local/state/fso-harness/audit.log`).

## 10. Testing
- Unit (no engine): channel protocol, session state, VP parser (fixture archive built in-test), log parsing, CSV
  parsing, OCR text normalisation, renderer classification from sample maps.
- Integration (`pytest -m engine`, bounded-run, single worker): launch/stop, each stage switch, bridge round trip,
  state, sexp, pilot/autopilot, time, wait_for, validate, api_docs, model_info, frame_stats, compare,
  renderer_compare (marker `vulkan`, needs a GPU), crash_report (deliberate crash via a test mod), qtFRED tools
  (marker `qtfred`). Engine tests use retail data + the user's MediaVPs; paths from env (FS2PATH, FSO_BIN, QTFRED_BIN).
- Every test proven able to fail (red run) before it counts.
- QA: HIGH-RISK tier (long-lived processes, cleanup/kill behaviour, desktop isolation) + cross-family pass.

## 11. Packaging and upstream
- README for agents (tool reference, setup, limits) and for humans (CLI). LICENSE: Unlicense, matching FSO's newer code
  (Copying.md: SCP changes since 2020-11-01 are Unlicense). Dustin 2026-10-12: "Match FSO."
- Showcase: public GitHub repo under dustinspace217 when Dustin says so.
- Upstream: open an issue first describing the tool; contribution shape decided with the maintainers.

## 12. Risks to probe early (plan's first tasks)
1. ui.Briefing.commitToMission and GS_EVENT_START_BRIEFING from the bridge on a retail mission with briefings.
2. ui.PilotSelect.createPilot on a fresh prefs folder; startup warning popup ordering vs -pilot.
3. On Dialog Frame + hv.Submit dismissing the new-user tip.
4. Pause/resume via game events; time compression above 4x outside retail mode.
5. Vulkan session + screenshot via XGetImage on Xvfb (worked in 2026-10-12 tests).
6. fred_check OCR legibility at 1280x720.
7. crash_report: does a debug Assert under -noninteractive leave a core and a log trace.
