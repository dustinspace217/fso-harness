# Weston display: GPU OpenGL for Linux sessions

## Status (updated 2026-11-20)
Phase: 3 of 3 done (HIGH-RISK QA closed; the review record is kept outside the repo)
Done: Tasks 1-3; review fix wave (4 review seats, mutation, 2 outside-model seats), 55 findings
Next: merge to main and publish (owner's yes 2026-11-20: "1. Yes."); Vulkan below the floor
is now refused (owner: "Yeah refuse it.")
Blocked: nothing

## Goal (quoted)
Dustin, 2026-11-18: "Let's just implement Weston. It sounds superior to xvfb as a default."
Earlier, on the problem it answers (relayed from BMagnu, FSO maintainer): debug builds run at
single-digit fps because every Linux OpenGL session renders in software, so he can run only
one session at a time.

## What the probe measured (2026-11-18, RTX 5080, NVIDIA 615.78.08, Weston 15.0.1, Xwayland 24.1.14)
Engine: upstream master 5af50bd25c, Release; retail data, no mods; SM1-01; 1280x720. Scratch
copy of the repo at /tmp/claude/fsoh-weston-probe (runner.py and envsetup.py patched), private
runtime root. Scripts and outputs: /tmp/claude/weston-probe/.

| Setup | OpenGL | Frame rate (5 one-frame samples) | Window shot | Whole-screen shot | Keys (`t` = target next) | Stop |
|---|---|---|---|---|---|---|
| Xvfb (today) | Mesa llvmpipe (software) | 31, 49, 15, 48, 52 | 1280x720 | 1280x800 | target changed twice | clean |
| Weston headless, its own X layer (rootless) | NVIDIA | ~60 | 1242x629 (window shrunk) | FAILS | NOT received | clean |
| same, kiosk shell | NVIDIA | ~60 | 1280x720 | not tried | NOT received | clean |
| Weston kiosk + full-screen Xwayland | - | - | - | - | - | Weston crashed (kiosk-shell.so, `kiosk_shell_output_set_active_surface_tree`, two core dumps) |
| **Weston headless (default shell) + full-screen Xwayland** | **NVIDIA** | **59, 63, 60, 63, 58** | **1280x720** | **1280x800** | **target changed twice** | **clean, nothing left running** |

So the shape that works is: a headless Weston on the GPU with its default shell, and inside it
a full-screen ("rootful", Xwayland's default) X server that the game and the harness's X tools
use exactly as they use Xvfb today. The frame rate is held at about 60 by the 60 Hz headless
output; the harness never needed more (time compression does not depend on it).

Not yet measured: qtFRED under it; Vulkan sessions under it; video memory per session and with
several sessions at once; AMD and Intel graphics (Mesa's hardware drivers); a machine without
a GPU (CI); sound sessions.

## Scope (what changes)
1. **Display choice, in the runner.** Default: Weston + full-screen Xwayland when both programs
   are installed and the machine has a GPU render node (`/dev/dri/renderD*`). Otherwise Xvfb,
   and the launch result says so with the reason and the install command. A launch option
   `--display xvfb` forces Xvfb (software rendering: identical pixels on every machine, for
   `compare` and screenshot diffs). The sandbox network always uses Xvfb (no GPU in the sandbox).
2. **Software OpenGL only on Xvfb.** `LIBGL_ALWAYS_SOFTWARE=1` is set on the Xvfb path only.
3. **Renderer report.** The launch result gains which display was used and whether OpenGL ran
   on the GPU or in software. The current library check reads NVIDIA's OpenGL as "unknown"
   (seen in the probe) and cannot tell Mesa hardware from Mesa software, so the check is made
   on the session's display before the engine starts (the OpenGL renderer string).
4. **Video memory guard.** On a GPU display, refuse a launch when free video memory is below a
   floor (NVIDIA through `nvidia-smi`; other vendors: a warning, not a refusal). Born from the
   2026-10-15 incident (four sessions at 2.45 GB each turned the desktop black).
5. **Lifecycle.** Weston and Xwayland live in the session's scope beside the engine, are ended
   on every path the runner ends Xvfb today, and their logs land in the run folder.
6. **Docs.** README (Requirements: `weston`, `xorg-x11-server-Xwayland`; a "Display" section),
   AGENT-GUIDE (frame rate line), project notes (the load-bearing decision and the etiquette
   line "Xvfb only" become "Weston or Xvfb, never the desktop"), probe-results (this probe).

Not in scope: Windows (unchanged), macOS, making Vulkan sessions use Weston unless Task 2's
check shows a reason, a higher frame-rate cap.

## Tasks
1. Display choice and lifecycle in the runner, software flag on the Xvfb path only, the
   `--display` option through the CLI and the MCP tool; unit tests for every branch of the
   choice (installed or not, GPU or not, sandbox, forced). Test-first.
2. Renderer report and the video memory guard; unit tests. Engine tier: run the engine,
   qtFRED and Vulkan tests under both displays; measure video memory with 1 and 3 sessions.
3. Docs; HIGH-RISK review (runner lifecycle: long-lived processes and their teardown),
   mutation pass on the touched files, cross-family pass, receipt; CI green.

## Risks the owner should know
- Every GPU session shares video memory with the desktop (Task 2's guard).
- Weston's kiosk shell crashes with a full-screen X server (probed twice); the default shell is
  used instead. A future Weston update could change behaviour under us; the Xvfb fallback stays.
- CI runners have no GPU, so CI keeps testing the Xvfb path; the Weston path is tested locally.


## Deviations (2026-11-20)
- Video-memory guard: the plan said "refuse". An automatic launch below the floor falls back to Xvfb
  with the reason; only an explicit `--display weston` is refused. Why: refusing would break
  multi-session runs that work today on Xvfb, which uses no video memory. Floor 3 GB (my number,
  from the 2026-10-15 incident's 2.45 GB sessions).
- `_start_xvfb` was NOT moved into display.py (architect note: a moved-and-edited function hides
  changes in a HIGH-RISK review); `_start_weston` sits beside it in runner.py; display.py decides.
- Software OpenGL: a `display.gl_env` helper drops LIBGL_ALWAYS_SOFTWARE for Weston sessions in
  the runner instead of widening envsetup.child_env (lint: too many arguments, and every caller
  would change).
- Not in scope, noted: the sandbox engine test fails on main too (the sandbox sees /home);
  TRACKED-LOCAL.

## Deviations: QA fix wave (2026-11-20)
- Vulkan and video memory (SL-3/TA-8): a Vulkan session is never moved to Xvfb for low video
  memory (it draws on the GPU on either display, so the move saved nothing). First built as a
  warning; since 2026-11-20 refused (owner: "Yeah refuse it. We need to make sure to not crash
  users' computers."), EXIT_REFUSED on every display path.
- Live GPU sessions (SL-2/CR-7, cross-family XFA-2/XFK-2): every live (starting or running)
  Weston or Vulkan session is charged LIVE_SESSION_RESERVE_MIB (1024, my number between the 314
  MiB retail and 2.45 GB heavy-mod measurements) on top of the 3 GB floor. The first fix charged
  only "starting" sessions, but the runner marks a session "running" as soon as the engine
  process exists, before any texture loads, so the reserve vanished almost at once (both external
  seats found it). Effect: fewer GPU sessions at once (default cap is 3 sessions anyway); beyond
  the floor, "auto" falls back to Xvfb.
- Vulkan memory check on every path (XFA-5/XFK-3): an explicit `--display xvfb` or a missing
  Weston no longer skips it.
- compare's Xvfb default applies on Linux only (XFA-1/XFK-1: Windows refuses any non-auto
  display); renderer_compare reports both runs' displays and warns when they differ (XFA-6).
- Replay keeps the recorded display (XFK-4); a Linux record without one replays on Xvfb.
- Runner start errors (a display that did not start) are the launch error for every network,
  not only the sandbox (XFA-8).
- Weston no longer reads weston.ini (SL-1): `--no-config`. Effect: a user's kiosk-shell or
  autolaunch config can no longer reach harness sessions.
- Logs (SA-4): Xwayland's stderr goes to /dev/null like Xvfb's; weston.log stays (error messages
  name it), measured size in the QA synthesis.
- compare (CR-1): runs default to `display: "xvfb"`, each run records its display, and runs on
  different displays are "not identical" with the reason.
- Display env (SA-3): Weston and Xwayland drop the desktop session variables envsetup already
  drops for the engine (DESKTOP_SESSION_VARS, now public) and get a D-Bus path nothing listens on.

## Appendix: Deferments originated in this plan
- DEF-W-01 · found by SL (SL-6) · decided at the fix-wave commit · defer target: indefinite ·
  LOW. What: a display process (Weston/Xwayland/Xvfb) that dies mid-session shows only as the
  engine's X error; its own exit code is never recorded. Why deferred: the engine's exit record
  already marks the session failed, and recording display exits needs a reaper thread in the
  runner (new state for a rare case). Fix direction: in runner.main's finally, poll() each display
  process and write its returncode into exit_info before _stop_display. Obsolete when: the runner
  gains a general child-supervision loop.
