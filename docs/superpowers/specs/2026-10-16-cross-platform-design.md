# fso-harness on Windows and macOS: design

Status: draft for review, 2026-10-16. Linux is the existing, working platform; this document adds
Windows (first) and macOS (second).

## Intent

fso-harness lets an AI agent drive FreeSpace Open and its mission editors headlessly. Today it
runs only on Linux. The people who will use it are agent-assisted developers: mod and campaign
makers, and engine contributors, many of whom work on Windows or macOS. They can debug and send
pull requests themselves once the repository is public, so the bar is "works and is documented
well enough to be fixed by its users", not "polished for every edge case".

Use cases, in the order they matter (from the project owner):

1. **Mod and campaign work.** The user describes a mission, campaign or mod idea; the agent
   builds it in the mission editor, playtests it by flying it the way a human would (the scripted
   pilot in `examples/pilot/`), reads what happened, goes back to the editor, repeats.
2. **Engine work.** The same loop where the thing changing is the engine: build it, launch a
   test mission, drive it, compare builds, read the crash when it dies.
3. **Exploring the agent's piloting** out of curiosity. Rides on the same machinery as 1.
4. **Multiplayer test bench.** Owner, on the purpose of the 2026-10-13 multiplayer work: "The
   goal ... was to make the harness capable of running multiple game sessions at once across
   network protocols, to test the netcode and reproduce and fix bugs affecting multiplayer."
   This is a core use case on every platform, not an optional extra. (Builder's reading, to
   confirm in the plan: "network protocols" = the engine's IPv4 and IPv6 paths, plus LAN
   discovery vs direct-IP joins.)

## Goals

- **Parity with today's Linux harness on Windows, then macOS, except where the operating
  system genuinely cannot do something.** Owner, verbatim: "More or less the same functionality
  we have today on Linux, except where OS limitations force disparity." Every remaining gap is
  named in the README's Limits list with its reason.
- **Editors driven through their own windows.** On Windows both FRED2 (the native Windows
  editor) and qtFRED; on macOS and Linux qtFRED. Owner: "I find that most LLMs make a more
  human-expected result when working with human tools than with the code directly ... So I'd
  say using FRED/qtFRED is important." FRED2 and qtFRED are meant to be at parity (qtFRED is the
  Qt port of FRED), so the agent should be able to do the same editing tasks in either.
- **Sessions run out of sight by default, with an option to make them visible.** Owner: "It
  needs to be able to work out of sight, with the option to make it visible." And why: "The
  point of running these hidden is to minimize disruption to the user's workflow while you
  work. Two entities using the same computer at the same time for different tasks - the two
  should not cross paths except by intention." So a hidden session never takes the user's
  keyboard, mouse or focus, never appears in the taskbar/Dock or Alt-Tab, and never raises a
  window; tools that need OS-level input to the window (`keys`, `click`, `drag`, `scroll`, the
  pixel-driven `fred_*` paths) refuse in a hidden session on Windows/macOS with an error that
  says to relaunch with `--visible`. Editor driving by accessibility and the in-engine scripted
  pilot do not need OS input and work hidden.
- **Sessions use the real network on every OS, as the stock engine does.** Owner: "Sessions
  should be able to reach the real network on all OSes. I didn't want our multiplayer test bench
  to compromise computer security - I did not mean it should not be able to run a game server,
  I meant it shouldn't be exploitable to take over my computer." and, on the Linux default,
  "Real network. I never meant to change this about how the engine works." Stock FSO opens UDP
  7808 on every interface at startup, single-player included (freespace.cpp game_init ->
  psnet_init; psnet2.cpp binds with no host address). Linux's private per-session network
  becomes opt-in (`--network private`); the default changes to the real network.
- **CI on Windows and macOS** (GitHub Actions runners) is the test gate for those platforms.
  The owner's machine dual-boots Windows 10 as a fallback for things CI cannot show.

## Non-goals

- Placing ships by clicking and dragging in the editors' 3D view on Windows/macOS. Owner:
  "typing positions is sufficient". Positions and orientations are typed into the editor
  dialogs' fields; the 3D view is used for screenshots. (Linux keeps its existing drag/marquee
  tools; see the anti-goals.)
- Byte-for-byte identical behaviour across platforms. Parity is "can do the same job", not "same
  implementation".
- Supporting Windows versions or macOS versions that the current FSO release itself does not.

## Anti-goals (would make this a failure even if it technically works)

- **Removing or weakening any capability that works on Linux today, in the name of
  cross-platform sameness.** Owner, verbatim: "I don't think we should diminish capability that
  currently exists in the name of cross-compatibility. If Linux can click and drag OR type
  positions today, leave it. If Windows can only do one of those two things, that's an
  acceptable loss of parity. Let's call it a nice-to-have where it's possible, but unnecessary
  where it isn't possible." New methods are added alongside the old, never instead of them.
- Game or editor windows popping up on the user's desktop when they did not ask for visibility.
- Touching the user's own pilots, settings, missions or game folder beyond what Linux touches
  today (the bridge mod, scratch mods, extracted files). Editor settings on Windows/macOS: see
  "Editor settings" below.
- Silent fallbacks: a platform that cannot provide a feature says so (an error naming the
  limit), it does not pretend.

## Constraints

- Python 3.12+, no runtime dependencies on Linux today (`dependencies = []`). Platform-specific
  Python packages are allowed on Windows/macOS but must be optional extras, so a Linux install
  stays dependency-free.
- The MCP server stays a thin, stateless layer over `fsoctl --json`; no daemon.
- The command channel (files in the session's preferences folder), the Lua bridge mod and the
  game/sexp/lua/state/wait/pilot commands are already platform-neutral and must stay that way.
- Linux behaviour is the reference: its existing tests keep passing unchanged.

## The current Linux-only surface (inventory, 2026-10-16)

About 15 of the 35 `fsoctl` modules use Linux-only facilities:

| Facility | Used for | Modules |
|---|---|---|
| Xvfb + DISPLAY | a private display per session | launcher, scope, envsetup, match, xinput, xwin, runner |
| systemd user scopes | caps (memory/CPU/tasks/runtime), group stop, cleanup | build, bridge_install, crash, launcher, scope, runner, session, match, sound |
| unshare / nsenter | private network per session; shared network per multiplayer match | launcher, match, runner |
| PipeWire / pactl, OpenAL device names | per-session audio sink | envsetup, launcher, openal_names, sound |
| xdotool (XTEST), ImageMagick import, tesseract | keys/clicks/drags/scroll, screenshots, OCR of qtFRED text | fred, xinput, xwin |
| coredumpctl + gdb, /proc | crash reports, process liveness and start times | crash, match, launcher, runner, scope, renderer |
| fcntl, setsid/start_new_session | locks, detaching | build, docs, session, scope |
| XDG_RUNTIME_DIR | run folders | envsetup, paths, sound |
| ldd | find the engine's own libopenal | sound |

## Design

### 1. A platform layer

Move every Linux-only call behind a small set of platform modules (`fsoctl/platform/linux.py`,
`windows.py`, `macos.py`) with one interface, chosen once by `sys.platform`. The interface covers:
process groups and caps, locks, run-folder location, liveness checks, the hidden display, input,
screenshots, crash capture, audio isolation, network isolation. Linux's implementation is the
current code, moved, not rewritten; its tests must pass unchanged.

A feature a platform cannot provide raises a `HarnessError` naming the limit and the platform
(exit code as for other refusals), never a silent no-op.

### 2. Out-of-sight sessions

- **Linux:** unchanged (Xvfb per session).
- **Windows:** first choice is a separate Windows desktop per session (the desktop API keeps its
  windows off the user's screen). Unknowns to probe before relying on it: whether the engine's
  OpenGL/Vulkan rendering works on a non-interactive desktop, and whether accessibility
  automation and input reach a window on another desktop. Fallbacks, in order: windows placed
  off-screen; an optional virtual-monitor driver the user installs.
- **macOS:** no built-in invisible display. Options: windows placed off-screen, or a virtual
  display. If neither is reliable, macOS ships "visible by default, hidden where possible" and
  the README says so.
- **Visible option:** a launch flag (`--visible`) on every platform. On Linux it would show the
  session on the user's display; whether to add that on Linux is left to the builder (it is
  new capability, not a removal).

### 3. Editor driving

Two methods, side by side:

- **Existing pixel method (Linux, kept):** raw input at window coordinates, screenshots, OCR.
  All current `fred_*`, `click`, `drag`, `keys`, `scroll` tools stay.
- **New accessibility method (all platforms):** find controls by name through the operating
  system's accessibility layer (UI Automation on Windows, the Accessibility API on macOS,
  AT-SPI on Linux), read and set field values, press buttons, pick list and combo-box items,
  walk tree controls (the event editor), and read dialog text (the error checker) without OCR.
  - qtFRED: 54 dialogs, ~1,400 standard Qt widgets with descriptive object names in the `.ui`
    files (e.g. `shipNameEdit`, `shipClassLabel`); Qt's accessibility bridge exposes standard
    widgets on all three platforms.
  - FRED2: 121 dialogs in `fred2/fred.rc`, standard Windows controls with resource IDs; Windows
    UI Automation exposes standard Win32 controls.
  - The 3D view in both editors is custom-drawn and invisible to accessibility; on
    Windows/macOS it is used for screenshots only.
- **Task maps:** a small per-editor table from editing tasks ("set ship name", "set position",
  "add event", "run the error check") to controls, so the agent asks for a task and the harness
  finds the controls. FRED2 and qtFRED get separate maps; the task names are shared, which is
  what parity between the editors means in practice.
- **Linux gains the accessibility method** in addition to the pixel method, so the same task
  calls work on every platform.

### 4. Engine sessions, builds, crashes, audio, network

| Capability | Windows plan | macOS plan |
|---|---|---|
| Caps and group stop | Job Objects (memory, CPU, process count, kill on close) | process groups; caps where the OS allows (rlimits); documented gap otherwise |
| Liveness / start time | Win32 process APIs | `proc_pidinfo`/`ps` |
| Locks | `msvcrt.locking` or a lock file with exclusive create | `fcntl` (works) |
| Run folders | `%LOCALAPPDATA%\fso-harness\...` | `~/Library/Caches/fso-harness/...` |
| Screenshots | window capture API | window capture API |
| Input (keys, clicks, scroll) | SendInput to the session's window (desktop-aware) | CGEvent posting |
| Crash reports | Windows Error Reporting local dumps, read with the debugger tools available on the runner | macOS crash reports in `~/Library/Logs/DiagnosticReports` |
| Builds (`build`) | cmake with the MSVC toolchain | cmake with clang/Xcode tools |
| Per-session audio | needs a virtual audio device; if none is available, `--sound` refuses and the README says why | same |
| Network | real network (the default everywhere now) | same |
| Multiplayer matches | standalone/host/client on the real network (loopback or LAN), distinct ports; the private match network stays Linux-only and opt-in | same |
| FRED2 | Windows only (it is an MFC application) | n/a |

Every row marked "documented gap" goes into the README Limits list for that platform.

### 4b. Editor settings (Windows/macOS)

FRED2 writes its preferences to the user's registry on exit (fred2/fred.cpp, `SetRegistryKey`,
`WriteProfileInt`); qtFRED uses QSettings (registry on Windows, the user's preferences on macOS)
and autosaves under QStandardPaths' application data folder, which Qt resolves through OS calls,
so Linux's HOME/XDG redirection does not carry over. Owner's choice (b): snapshot the editor's
settings before an editor session and restore them after, and refuse to start an editor session
while the user has that editor open. Optional for users who want it airtight (c): run editor
sessions under a separate local account (one admin setup step), documented, not required.

### 4c. Network security

Not containment but exploitability: review whether the engine's network code and the standalone
server's web panel (TCP 8080, engine default login admin/admin) can be used from the network to
take over the machine. The harness should at least never start a standalone server with the
default panel credentials, and should say in the README what a running session exposes. A
deeper review of the engine's packet handling is its own piece of work, scoped separately.

### 5. Install and registration

- `pip install -e .` stays the install; Windows/macOS extras (e.g. `pip install -e .[windows]`)
  pull in the platform automation packages.
- `mcp/setup.sh` and `register.sh` get PowerShell equivalents on Windows; macOS uses the shell
  scripts.
- Environment variables keep their names (`FS2PATH`, `FSO_BIN`, `FSO_BIN_DEBUG`, `QTFRED_BIN`),
  plus `FRED2_BIN` on Windows.

### 6. Testing

- The existing unit tier runs on all three platforms in CI (GitHub Actions Linux, Windows,
  macOS runners). Platform modules get their own unit tests with the OS calls stubbed, in the
  style of the current tests.
- A live tier on CI where the runner allows: launch a release build of FSO against freely
  redistributable test data or a minimal mod, run the command channel, take a screenshot, stop.
  Whether the engine renders on a GPU-less runner (software OpenGL) is a probe, not an
  assumption.
- Editor-driving live tests on CI if the runner grants accessibility access (known to be hard
  on macOS runners); otherwise on the owner's Windows 10 boot for Windows, and documented as
  user-verified for macOS.
- No retail game data on CI (licensing).

## Probes before building on an assumption

Each of these gets a small throwaway test, and its result is recorded before the code that
depends on it is written:

1. Engine renders (OpenGL and Vulkan) on a separate Windows desktop; input and UI Automation
   reach it.
2. qtFRED's widgets appear in UI Automation with their object names (AutomationId) on Windows,
   in the macOS Accessibility API, and in AT-SPI on Linux inside an Xvfb session.
3. FRED2's dialog controls appear in UI Automation with their resource IDs; the event tree is
   walkable.
4. macOS: an automated process can be granted Accessibility permission on a CI runner.
5. FSO release builds (and qtFRED/FRED2 builds) are available for Windows and macOS in a form CI
   can download, and the engine starts on a GPU-less runner.
6. A virtual audio device can be created per session without admin rights (Windows/macOS);
   if not, `--sound` is a documented Linux-only feature.

**Outcomes (2026-10-25; detail in docs/research/2026-10-16-xplat-probe-results.md, raw JSON in
docs/research/xplat/):**
1. OpenGL: yes with Mesa llvmpipe; rendering, capture, UI Automation, posted input and engine GL
   init all work on a hidden desktop. Not measured: a real GPU on a hidden desktop, Vulkan.
2. Yes on all three (Qt 6.12; the object-name path is the id); text, buttons and trees work, but
   selecting a combo item through accessibility does not take on any OS.
3. Not measurable on CI (FRED2 without data shows only SDL error boxes); needs retail data on the
   owner's Windows 10.
4. The macOS runner is already trusted.
5. Release builds yes (qtFRED in none); the engine starts on Linux (system OpenAL required) and on
   Windows with Mesa; macOS runners cannot render.
6. No virtual device needed: OpenAL Soft's wave writer per session works on all three.

**Phase 2 outcomes (Plan B, 2026-11-17; detail in docs/research/2026-11-16-xplat-vm-results.md,
engine findings in docs/research/2026-10-12-probe-results.md section 23, plan and deviations in
docs/superpowers/plans/2026-11-16-cross-platform-plan-b.md):**
- Windows game sessions are built and were run for real in a Windows 11 VM (FSO 26.2,
  software OpenGL): a session per Job Object on a hidden desktop of its own; launch, state,
  lua, sexp, wait, pilot, time, autopilot, screenshots, stop, cleanup, crash reports from the
  engine's own minidump, and builds with Visual Studio's tools.
- Three things the design did not foresee, each measured and recorded as a plan deviation:
  the engine pauses when its window is not in front (a settings file turns that off); a test
  bind of a port brings up a Windows Firewall question (the port test reads the socket table
  there); a stop request for `fsoctl` (Ctrl-Break) arrives only through a console shared
  with the MCP server.
- CI: the unit tier as far as it applies to Windows, plus tests of the real Win32 calls,
  must pass on GitHub's Windows runner.
- Still not measured: a real graphics card and Vulkan on a hidden desktop.
- **Owner's ruling for the editor phase (2026-11-16, verbatim):** "If the harness needs to
  use qtFRED for best results, that's acceptable. We just have to note it in the agent
  instructions." So Phase 3 may make qtFRED the Windows editor, and the MCP instructions
  and the agent guide must then say so.

## Phases (suggested)

1. Platform layer on Linux only (move, do not change); all tests pass; CI matrix added with the
   unit tier. Separately, the one intended Linux behaviour change: sessions and matches default
   to the real network; `--network private` (and a private match network) become opt-in; README
   and AGENT-GUIDE updated; the standalone server never starts with the default panel login.
2. Windows sessions: launch, command channel, state/lua/sexp/pilot/wait, screenshots, stop,
   cleanup, crash reports, builds, hidden desktop (after probe 1).
3. Editor driving by accessibility: qtFRED on Linux and Windows, then FRED2, with shared task
   names.
4. macOS: sessions, then qtFRED driving (after probe 4).
5. Multiplayer test bench on Windows and macOS at parity with Linux (standalone, host and
   client sessions together, on the real network, every protocol path the engine supports),
   then per-session audio as far as the probes allow; documented gaps otherwise.

## Calls made in review (not the owner's; check these)

- macOS editor driving may ship marked "not yet verified on a Mac" for the community to verify,
  if no one on the project has a Mac; the README says so. (From the owner's description of the
  audience: developers who debug and send PRs.)
- Editor tools: the existing `fred_*` verbs keep their names on every platform (reimplemented
  per platform where needed), and the agent gets generic accessibility tools (find a control,
  read, set, press, pick, walk a tree) in addition to the task maps, so it can reach any control
  in either editor, not only mapped tasks.
- Phase 1 "moved, not rewritten" means same behaviour; test files may be edited mechanically
  where a patch target moves, with no change to what a test asserts.
- Windows Firewall's "allow access" prompt on the first launch of each new engine executable is
  stock engine behaviour; documented, not suppressed.

## Left to the builder

- Module names, the exact platform interface, and which Python packages to use for UI
  Automation, the macOS Accessibility API and AT-SPI (prefer well-maintained, permissively
  licensed ones; record why each was chosen).
- Whether Linux gets a `--visible` option.
- The task-map format and the first set of tasks (start with what the use cases need: open,
  save, error check, ship name/class/position/orientation, wings, events, goals, messages).
- How CI obtains FSO builds, as long as no retail data is used.
- The order of work inside each phase.
