# Cross-platform VM results (probes 7-9), 2026-11-16

Plan A left four items open for the owner's Windows 10 boot. On his "Continue. Test with VMs"
they were measured in a Windows 11 virtual machine instead. This note records what ran, what
was found, and what a VM cannot show. Raw results: `docs/research/xplat/probe-fred2-live-*.json`,
`probe-qt-combo-*.json`, `probe-engine-live-*.json`. Captured window images (retail art, not in
the repository) and the VM tooling: `fso-harness-artifacts/xplat-vm-2026-11-16/`.

**This is the second version of this note.** The first (commit 261bfcf) was reviewed
(`fso-harness-artifacts/qa-xplat-vm-probes/`), and the review found that its central FRED2
claim rested on a check that could not fail, and that several sentences had no result file
behind them. The probes were fixed and every one was run again; what changed is listed under
"What the review changed". Every number below is in a result file unless it says otherwise.

## What ran, and where

- **Machine:** Windows 11 Enterprise Evaluation (build 26100) under QEMU/KVM, 8 cores, 12 GB,
  no graphics card. The disk is a private copy of the FRED test VM built on 2026-11-11
  (an earlier VM image on another drive, left untouched): it already held FSO 26.2.0 (engine, FRED2,
  qtFRED), retail FreeSpace 2 data and Mesa 26.2.6 beside the executables. Python 3.12.10 and
  the probe packages were added to the copy.
- **Rendering:** Mesa's software OpenGL (llvmpipe) in every run. No hardware driver anywhere.
- **Desktops:** each Windows probe ran on the logged-in desktop ("Default") and, through
  `probes/xplat/hidden_run.py`, on a desktop of its own made with CreateDesktopW
  ("fsoharness-hidden"). Every result file carries the desktop name the probe itself read.
- **Linux:** the combo probe ran locally in the private Xvfb + AT-SPI session. **macOS and a
  second Windows machine:** GitHub runners (CI run 43965077819), combo probe only.
- **Runs:** every result file here except the two CI ones is from one pass on 2026-11-16,
  20:03 to 20:19, with the probe code as committed (after two review rounds). The CI combo run
  used the first version of the combo probe (its routes are the same).

## Summary

| Question | Result |
|---|---|
| FRED2 with retail data: are its controls reachable by control ID? | Yes. Every numbered control in the Ship Editor has an ID that `fred2/resource.h` defines, on both desktops; and acting on six of them by ID did what their names say |
| Can a mission be opened, a ship picked, fields changed, with no keyboard, mouse or focus? | Yes, by menu commands and window messages, hidden too. FRED2's mission data took every text and window-message change (each confirmed by the dialog's own refill; the saved file agrees with the last value of each field) |
| Does UI Automation's "select" work on FRED2's combo boxes? | Not as tried (Expand, Select, Collapse). On the normal desktop the item is selected while the list is open and reverts when it closes; on the hidden desktop the list shows no items at all |
| Is the Events editor's tree readable? | Yes: 62 items with their names, expandable (72 after expanding ten), hidden too |
| A Qt combo box: is there a route that works? | Yes on Windows, Linux and macOS; not the one Plan A guessed |
| The engine with data on a hidden desktop | Starts, draws new frames, is captured, holds its UDP game port. Posted key presses do not reach it |
| Why the keys do not reach it | They also fail on the normal desktop when the engine's window is not the front one, and the same engine takes the same keys once it is brought to the front. So it goes with "not in front", not with "hidden" or "not ready" |
| A real graphics card on a hidden desktop; Vulkan | Not measurable in a VM |

## Probe 7: FRED2 with retail data (`fred2_live.py`)

FRED2 26.2.0 was started with `-nosound`, a copy of a mission opened (its Events tree lists 62
events), and the Ship and Events editors driven. Nothing used real input, and no window was
brought to the front. The test ship is the first one that is not in a wing and whose AI Class
field is enabled ("Psamtik", five presses of the dialog's Next button).

**How "FRED2's data took it" is judged.** After each change the probe presses the Ship
Editor's Next button and then Prev. In FRED2's source (fred2/shipeditordlg.cpp at 26.2.0) both
store the dialog's fields into the mission (`update_data`), mark another ship and fill every
control from the mission (`initialize_data`). The other ship's name appearing in between
("Epsilon 1" in every row) shows the controls really were refilled, and the probe then requires
the dialog to be back on the ship it started from, by name. Two reviewers read the 26.2.0
source and found this description holds; a change FRED2 refuses (its Cancel path) stops the
Next step, and the row then reads "data did not take".

At the end the probe saves the mission through File > Save and reads the file: the ship is
there as `Probe 7b`, class `SF Basilisk`, AI class `Colonel` (its own line, not a turret's).
That is a second witness only for the LAST change of each field: the `WM_SETTEXT` rename and
the two `CB_SETCURSEL` + `CBN_SELCHANGE` rows. The SetValue rename and the two "`CB_SETCURSEL`
alone" rows rest on the dialog's refill alone. Same on both desktops.

| Action | How | Control took | Data took |
|---|---|---|---|
| Open a mission | posted `WM_COMMAND` File > Open (57601); path into the common dialog's file name box (`WM_SETTEXT`); posted `WM_COMMAND` IDOK | title names the mission, 1.6 s (1.3 s hidden) | |
| Open an editor | posted `WM_COMMAND` Editors > Ships (32799), Events (32974) | dialog appears | |
| Mark a ship | the Ship Editor's Next button (1373), posted `BN_CLICKED` | fields fill and enable | |
| Rename the ship | UI Automation ValuePattern.SetValue | yes | yes |
| Rename the ship | `WM_SETTEXT` | yes | yes |
| AI class | `CB_SETCURSEL` alone | yes | yes |
| AI class | `CB_SETCURSEL` + posted `CBN_SELCHANGE` | yes | yes |
| Ship class | `CB_SETCURSEL` alone | yes | yes |
| Ship class | `CB_SETCURSEL` + posted `CBN_SELCHANGE` | yes | yes |
| AI class, ship class | UI Automation Expand + SelectionItem.Select + Collapse | no | no |
| Read the event tree | UI Automation from the tree's window handle | 62 items and names; Expand works | |
| Capture a window | PrintWindow with PW_RENDERFULLCONTENT | main window (3D view drawn), both editors | |

The table is the same on the hidden desktop, with one difference in how UI Automation's select
fails. Normal desktop: with the list open the control shows the chosen item
(`before_collapse`: "Major", "SF Dragon"), and after Collapse it is back on the old one, with
no error from any call. Hidden desktop: the expanded combo lists no items (`list_items_seen:
0`), so there is nothing to select. Only this one sequence was tried (Select without Expand
was not), so the finding is "this route does not work", not "no UI Automation route can".

**Control IDs.** Windows itself lists a program's windows and each control's ID (EnumWindows,
EnumChildWindows, GetDlgCtrlID) without asking the program. The Ship Editor has 82 controls;
54 carry a number that `resource.h` defines as an `IDC_` name, 25 are unnumbered labels, 3 are
the edit boxes inside combos, none is unexplained. This check is weak on its own (it accepts
any number that any `IDC_` name uses); the stronger evidence is that the IDs 1126 (ship name),
1003 (ship class), 1194 (AI class), 1373 (Next), 1382 (Prev) and 1321 (wing label) each did
what their names say. UI Automation shows the same IDs as AutomationId (two fewer: it does
not list 1100 and 1372).

**Speed.** Reading the Ship Editor's 113 elements through UI Automation took 44 to 45 s in
the final runs (4.2 s once, in an earlier run; cause not established). The six combo routes
together took 492 s on the normal desktop and 39 s on the hidden one; the split per route is
not recorded, and the difference between the desktops is that the expanded lists had items to
walk on the normal one: 140 and 2,020 list items seen (189 and 2,530 in the run before) for
combos that hold 7 and 112 items, which is unexplained. Windows' own enumeration and the
window messages answered at once; FRED2 answered an empty message in 17 to 30 ms.

**Things an editor driver has to handle** (each seen in a run):
- FRED2 refuses to rename a ship that is in a wing: "This ship is part of a wing, and its
  name cannot be changed" (18:00 run, ship "Iota 1"). The control showed the new name; the
  data kept the old one.
- Window messages work on a greyed-out control. An earlier run changed the AI Class combo of a
  player ship while it was disabled; UI Automation refused the same control ("element not
  enabled"). A driver that uses messages must check `IsWindowEnabled` itself.
- FRED2 asks and complains in modal boxes, and until one is answered nothing else responds.
  Seen: on Save, "Probe 7b (SF Basilisk) is used as an arrival anchor by wing Zeta ... but it
  does not..." (the class change broke a reference; the file was still written); in earlier
  runs an autosave question on Open and a mission-event error on Save. A driver needs a tool
  that lists a box's text and answers it; the probe answers No, else Cancel, else OK, and
  records each box.
- Acting too fast raises spurious boxes: a second Next pressed straight after the first
  produced repeated "Number of waves illegal" boxes (18:00 run); with a pause of about 2 s
  between steps the same steps raised none. Cause not established.
- FRED2 reopens the dialogs that were open when it last closed (the Ship Editor and the Jump
  Node Editor here), so "open the Ship Editor" must first check whether it is showing.
- FRED2's dialogs are owned by its main window, and UI Automation lists an owned window under
  its owner. Searching UI Automation "for the top-level window that holds control X" returns
  the main window (the first version of the probe closed FRED2 that way). Window handles
  from Windows' enumeration do not have this problem.

## Probe 8: Qt combo boxes (`qt_combo_routes.py`)

Each route gets a stand-in dialog of its own. "Took" means Qt's own record shows the second
item afterwards; every route in the VM and Linux runs started from the first item
(`starts_at: 0`; the two CI files predate that field).

| OS | Route | Normal | Hidden desktop |
|---|---|---|---|
| Windows | posted Down, no focus call (control) | no | no |
| Windows | UIA SetFocus + posted Down | no | no |
| Windows | UIA Expand + posted Down, Return to the popup window | TOOK | TOOK |
| Windows | UIA Expand + posted Down, Return to the dialog window | TOOK | TOOK |
| Windows | UIA Expand + posted mouse click on the item | TOOK | TOOK |
| Windows | UIA Expand + LegacyIAccessible default action | TOOK | no |
| Windows | UIA SetFocus + a real key press | no (the dialog was in front) | not judged (could not be brought to the front) |
| Linux | XTEST Down, no focus call (control) | no | |
| Linux | AT-SPI grabFocus + XTEST Down (with and without xdotool windowfocus) | no (grabFocus returns false) | |
| Linux | click at the combo's AT-SPI position + XTEST Down, Return | TOOK | |
| Linux | click at the combo's AT-SPI position + click at the item's | TOOK | |
| Linux | xdotool windowfocus + AT-SPI ShowMenu action + XTEST Down, Return | TOOK | |
| Linux | AT-SPI ShowMenu action + click at the item's AT-SPI position | TOOK | |
| macOS (CI) | posted Down, no focus call (control) | no | |
| macOS (CI) | AXFocused + posted keys (three variants) | no | |
| macOS (CI) | AXPress on the combo + Down, Return posted to the process | TOOK | |
| macOS (CI) | AXFrontmost + AXFocused + system keys | no | |
| macOS (CI) | AXFrontmost + AXPress + system keys | TOOK | |

The Windows runner on CI gave the same seven results as the VM's normal desktop.

Plan A's candidate ("focus through accessibility, then keys") works on none of the three: the
accessibility focus call does not move Qt's focus (Qt reported the line edit focused
throughout on Windows, even with the dialog in front and a real key sent). What works on all
three is opening the list through accessibility and then choosing in the list: posted keys or
a posted click on Windows (both work hidden), keys or a click on Linux, keys on macOS
(including keys posted straight to the process, with no system-wide input).

## Probe 9: the engine with retail data (`engine_live.py`)

FSO 26.2.0 FastDebug, `-window -nosound -nomusic -port 7821 -no_unfocused_pause`, a fresh
preferences folder of its own (which shows the pilot screen first). Three cells: normal
desktop, hidden desktop, and normal desktop with another window opened in front of the engine
("behind").

| Check | Normal | Hidden | Behind |
|---|---|---|---|
| Graphics start-up finished (llvmpipe, OpenGL 4.6) | yes | yes | yes |
| Holds UDP 7821 on 0.0.0.0 and :: (the engine's pid) | yes | yes | yes |
| PrintWindow capture, 1040x807 | yes | yes | yes |
| Different pictures in 8 captures 0.3 s apart, nothing sent | 3 | 3 | 4 |
| Its window is the foreground window when the keys are posted | yes | no (SetForegroundWindow fails) | no |
| Windows accepted all four posted key messages | yes | yes | yes |
| Posted "A", Return make pilot `a.json` and move to the main menu | yes | no | no |
| The same engine, then brought to the front, the same keys posted again | | | yes: pilot made, main menu |

So the real-network default and `-port` behave on Windows as on Linux, and a hidden session
starts, keeps drawing (the pilot screen's cursor blinks, so several different pictures means
new frames) and can be captured. Posted keys reached the engine only when its window was the
foreground one. "Behind" shows the hidden desktop is not what stops them, and its last row is
the control for "the engine was not ready yet": the probe closed the window in front, brought
the same engine forward and posted the same four messages, and this time the pilot was made.
The hidden cell has no such control (its window cannot be brought forward); it rests on the
same build drawing the same screen. Only posted key messages were tried: posted mouse
messages and real input (SendInput) are unmeasured. An earlier hidden run without
`-no_unfocused_pause` gave the same result. Why the engine ignores
them was not read from source (SDL delivering key events only to the focused window is the
likely reason: inference). The command channel does not use input; whether it works in a
hidden session is Plan B Task 5's first live check, not measured here.

## Probe 10: does a named job outlive the command that made it? (`win_job_name.py`)

Measured on 2026-11-16 at 23:20, before Plan B's Task 4 was built, because that task was
written on an assumption nothing had tested: that a Windows Job Object can still be opened by
its name for as long as a process is in it. `fsoctl launch` makes the session's job and
exits; a later `fsoctl stop` is a new process that has only the name. Result file:
`docs/research/xplat/probe-win-job-name.json`.

| Who holds a handle to the job after its creator exits | Job opened by name | Ended by name |
|---|---|---|
| nobody (the creator held the only one) | no (error 2), though its process was still running | |
| the process in the job, which inherited the creator's handle | yes | yes |
| the process in the job, which opened the job by name itself | yes | yes |

So the assumption was false: Windows drops a job's name when the last handle to it closes,
while the job itself lives on with its processes. Plan B's fix (in `win_scope.py`): the
session's first process inherits the job handle, and the runner passes it on to the engine,
so the name lasts as long as either lives.

Found the same evening by the first on-Windows test of that code: starting a process with
plain handle inheritance also handed it the launching command's own output pipe, so a caller
reading that command's output waited until the session ended. The new process is now given an
explicit list of the two handles it may have (its log and the job).

Also measured by those tests (`tests/winlive`, 9 passed in the VM): a job's memory limit makes
allocations fail (the process is not ended, as it is on Linux); the process-count limit
refuses the process over the limit; `netstat -ano` names the owner of a bound UDP port and of
a listening TCP port.

## Plan B live checks: real sessions through `fsoctl` (2026-11-16, 22:45 to 23:00)

The first Windows sessions started by the harness itself (Plan B Task 5, Step 4). Same VM
(now 18 GB), FSO 26.2.0 FastDebug, retail data, software OpenGL, every session on its own
hidden desktop. Outputs are quoted from the commands; nothing here is a rate (one or two runs
each).

| Check | Result |
|---|---|
| `fsoctl launch --mission sm1-01.fs2 --allow-eval` with every skip switch | ok; bridge heartbeat received; `renderer: opengl`, `libraries: [libgallium_wgl.dll]`; game port 7808 (sessions sbf9632cb, s7f975c09, s2733f946) |
| `status` | `status: running`, `alive: true`, heartbeat 0.09 s old at frame 2048 (after the bridge fix below) |
| `wait_for <id> state GS_STATE_GAME_PLAY` | met in 0.12 s (s2733f946) |
| The mission runs unattended | `state` twice, 10 s apart: `GS_STATE_GAME_PLAY`, `paused: false`, mission time 28.4 s then 38.5 s |
| `lua <id> "return mn.getMissionFilename()"` | `sm1-01` |
| Out of sight | no window title on the user's desktop belongs to the engine (`Get-Process` with a main window title: none); a screenshot of the VM screen shows no game window and no taskbar button |
| The user's own game settings | newest file under `%APPDATA%\HardLightProductions\FreeSpaceOpen` is from 20:15, before any session |
| The game port's owner | `netstat -ano -p UDP`: `0.0.0.0:7808` owned by the engine's pid |
| `stop` | `status: stopped`, `ended_by: stop`, `runner_first: ok`, engine exit code 0 (it closed when asked); no engine or runner process left |
| Two `launch` commands started at the same moment | two sessions, ports 7808 and 7809, each owned by its own engine (sd051f46c, sa09383b3) |

Three defects were found and fixed on the way, each with a unit test first:
- every launch failed in its own clean-up, which asked the Linux sound server for leftover
  sinks (needs a user id Windows does not have);
- the heartbeat was written once and never again, because the engine's rename cannot replace
  a file on Windows (probe-results section 23);
- the mission paused itself one second in, because a hidden window is never the foreground
  one and the engine reads "pause if unfocused" from a settings file, not from the
  command-line switch the harness passes (probe-results section 23).

Seen and left as it is: the launcher's own memory rule (8 GB kept for the desktop) refused
the launch while the VM had 12 GB (9 GB free); the VM was given 18 GB. A session stopped
while a popup is up in the main hall (a new pilot's tips, with `--dismiss-popups` off) does
not close when asked and is ended by force after 20 s, exit code 1; the quit request waits
behind the popup (the Linux runner waits the same 20 s there: inference from its code, not
rerun). And the screenshot showed a Windows Firewall question for Python on the VM's
desktop. Windows' own firewall log puts the rule for `python.exe` at 22:45:09, one second
after the first session's folder was created (sbf9632cb, 22:45:08); that launch's port test
bound a UDP port on every address. The same bind from a
program with no rule yet (`pythonw.exe`, 23:12:50) added two block rules for it at that
second. Task 7 takes the bind out of the port test on Windows.

### Ports (Plan B Task 7, 23:12 to 23:20)

| Check | Result |
|---|---|
| The port test while another program holds UDP 7808 on every address (pid 8484) | `port_free("udp", 7808)` is False; asking for 7808 by name is refused in the Linux words, "port 7808 is held by another program on this host"; the next free port is 7809 |
| `launch` in that state | session s27c03a74 got port 7809, no warning; `netstat`: `0.0.0.0:7808` pid 8484, `0.0.0.0:7809` pid 2260 (the engine) |
| `status` | `renderer: opengl`, `libraries: [libgallium_wgl.dll]`, `port: 7809`, `alive: true` |
| A holder with an IPv6-only socket (`[::1]:7852`) | missing from `netstat -ano -p UDP`, present in `-p UDPv6` and in plain `netstat -ano`; the lookup reads the plain listing |

Not run through the command line: `launch --port`. That option needs a match, and matches
are refused on Windows until Plan E, so the named-port refusal above is the launcher's
function called directly. The firewall probe left two block rules for `pythonw.exe` in the
VM's firewall; nothing uses that program.

### Crash reports (Plan B Task 8, 23:22 to 23:36)

Session s8bc8c2e3 (engine pid 9776), in the mission, then one script call the engine treats
as fatal: `fsoctl lua <id> "return ba.noSuchFunctionAtAll()"`.

| Check | Result |
|---|---|
| How the engine ended | `lua` returned exit 6, "engine is not running (lua)"; `status`: `exited`, `ended_by: exit`, exit code 3765269347 (0xE06D7363, the Microsoft C++ exception code); no window appeared and nothing hung |
| The engine's own dump | `C:\FS2\fs2_open_26_2_0_x64_SSE2-FASTDBG_20261117_072630.mdmp`, 515,757 bytes: the name's stamp is UTC (23:26:30 local) |
| Whose dump it is | the dump's MiscInfo stream holds process id 9776, the session's engine (read on Linux from a copy of the file) |
| The debugger | not present at first. The Windows SDK's debugger feature was then installed in the VM (winget, debugger feature only): `cdb.exe` landed in `C:\Program Files (x86)\Windows Kits\10\Debuggers\x64` and is NOT on PATH |
| The plan's command, `kb 30; q` | three frames, all inside the code that writes the dump |
| `.ecxr; k 30; q` | the real stack, 38 frames: cdb reads the bare 30 as hex |
| `.ecxr; k 0n30; q` | 30 frames, top `KERNELBASE!RaiseException+0x8a`; engine frames show as offsets because no `.pdb` is beside this engine |
| `fsoctl crash_report s8bc8c2e3` | `dump` with path, size, time 1794900390 and pid 9776; `exit_info` as above; `backtrace` 30 lines; `problems`: "Lua Error: Could not find index 'noSuchFunctionAtAll' in type 'Base'"; the log tail ends "CRASH DETECTED!!! Wrote crash dump to ..."; hints name the exit code and say what the names in the backtrace are worth |

A first attempt at the fatal call did nothing: the VM wrapper passed the script unquoted and
a `)` ended the batch block early, so `fsoctl` never ran. That was the test tooling, not the
harness. Left in the VM: the dump beside the engine and the installed debugger.

### Builds (Plan B Task 9, 23:44 to 23:58)

The VM has Visual Studio 2022 Build Tools (MSVC 19.44, MSBuild 17.14) and an engine tree at
`C:\src\fso`, configured with the "Visual Studio 17 2022" generator (configurations Debug,
Release, FastDebug). `cmake` is not on PATH there; the build folder's `CMakeCache.txt` names
the one inside the Build Tools.

| Check | Result |
|---|---|
| What the tools print through `cmake --build` (a three-file sample project) | compile lines `file(line,col): error C2065: ... [project.vcxproj]`, the same form for warnings; linker lines `x.obj : error LNK2019: ...`, `x.exe : fatal error LNK1120: ...`, `LINK : fatal error LNK1181: ...`; no summary block, no repeated lines; exit code 1 on every failure. The four outputs are the test data files `tests/data/build_msvc_*.txt` |
| `fsoctl build C:\src\fso --config FastDebug --target Freespace2` | `ok: true`, 2 minutes; cmake was found through the build folder's own record |
| While it ran | 14 MSBuild processes, `mspdbsrv`, `link`, `cmake`, all inside the build's job |
| After it returned | none of them left |
| The same build with `int harness_planted_error = ;` added to `code/globalincs/crashdump.cpp` | `ok: false` in 8 seconds, `fsoctl` exit code 9, one error row: that file, line 76, column 29, "C2059: syntax error: ';'", source `compiler`; `oom_suspected: false` |
| The file restored (`git checkout`) and built again | `ok: true`, 94 seconds |
| Three more forms from the sample project | a target that does not exist: `MSBUILD : error MSB1009: Project file does not exist.`; an option the compiler driver does not know: `cl : command line  warning D9002: ...`, a warning, and the build passed; `/diagnostics:classic` did not remove the column from the lines MSBuild relays |

The planted error went into the VM's own tree, which is itself a private copy, and not into
a scratch copy of 7 GB as the plan had it. Not measured: what the compiler prints when the
job's memory limit makes its allocations fail, so Windows builds carry no out-of-memory hint.

### Stopping a timed-out fsoctl (Plan B Task 10, 2026-11-16 23:58 to 2026-11-17 00:12)

On Linux the MCP server sends a timed-out `fsoctl` SIGTERM, and `fsoctl` turns that into a
normal exit so its cleanup runs. Windows has no SIGTERM one process can send another. What
it has is the console event Ctrl-Break, which Python hands to the child as SIGBREAK. A probe
started a child in a process group of its own, sent Ctrl-Break, and recorded what happened,
for a parent in three console situations:

| The parent (standing in for the MCP server) | The child was started | Result |
|---|---|---|
| has a console window | in a new process group | request arrived: the child's handler ran, its `finally` ran, exit 143 in 0.02 s |
| has a console window | the same, but waiting in ONE 30 s sleep | not acted on within 12 s: Python runs the handler only when the sleep returns |
| has a console with no window (CREATE_NO_WINDOW) | in a new process group | request arrived, exit 143; the child had no console window |
| has a console with no window | new group + CREATE_NO_WINDOW | sent without error and never arrived: the child had a console of its own |
| has a console window | new group + CREATE_NO_WINDOW | the same: never arrived |
| has NO console (DETACHED_PROCESS) | in a new process group | cannot be sent (WinError 6), and the child opened a VISIBLE console window |
| has NO console | new group + CREATE_NO_WINDOW | cannot be sent; no window |

`GetConsoleCP()` tells the three parents apart where `GetConsoleWindow()` does not: 437 with
a console window, 437 with a windowless console, 0 with none.

So the server starts `fsoctl` on its own console when it has one (the request can then
arrive, and no window opens because the console exists), and with CREATE_NO_WINDOW when it
has none (no window, and a timeout ends `fsoctl` at once, because it cannot be asked).

| Check with the real code | Result |
|---|---|
| `mcp\setup.ps1 -Python <python.exe>` | venv made, `mcp` 2.0.0 installed, exit 0 |
| `mcp\register.ps1 -WhatIf` | prints `claude mcp add --scope user fso-harness -e FS2PATH=... -e FSO_BIN=... -- <venv python> <server.py>`, changes nothing, exit 0 |
| `register.ps1` with no venv / no FS2PATH and FSO_BIN / no `claude` on PATH | one plain line each, exit 1 / 2 / 3 |
| PSScriptAnalyzer 1.25.0 on both scripts | 0 findings |
| `tests\test_mcp_wrapper.py` (the server's checks, run by the venv's Python) | passed: the tools map onto the real parser, and the real timeout path sent Ctrl-Break to a real child, which acted on it |
| A real `fsoctl` command sent Ctrl-Break (`tests\winlive\test_stop_request_live.py`) | exit 143 and its cleanup ran |

Not run: the registration itself. The VM has no Claude Code, so `claude mcp add` on Windows
is unverified, and so is which of the three console situations Claude Code's own MCP
servers start in.

### The unit tier and the engine tier on Windows (Plan B Task 11, 2026-11-17, 00:14 to 01:00)

**Unit tier.** Its first run on Windows could not import 13 test files (`fcntl`, `resource`,
`os.O_DIRECTORY`), lost the interpreter to a test that sends itself SIGTERM (a hard kill on
Windows), and failed about 250 tests. Three were faults in the harness: text opened in the
local code page instead of UTF-8, the Lua pre-check following only paths that start with
"/", and the shadow folder's links (a link to a folder that does not exist yet becomes a
file link on Windows; no clear message without the right to create links). The rest tested
Linux facilities and are now selected by `tests/conftest.py`. After that: 815 passed, 104
skipped, none failed in the VM with links allowed (807 before Developer Mode was switched
on); 810 passed, 109 skipped on GitHub's Windows runner; the 18 tests of real Win32 calls
passed in both places.

**Lua pre-check, live** (API reference generated by `fsoctl api_docs` with Developer Mode
on): `lua "return ba.noSuchFunctionAtAll()"` refused before sending, exit 7;
`lua "dofile('C:/xplat/bad.lua')"` refused, naming the file (session s7fa16b92, which stayed
up); good code answered with `lua_check: passed`. One call written
`dofile([[C:/xplat/bad.lua]])` was not checked and ended the engine (session sf4785860): the
pre-check reads quoted paths only, on every OS; pinned as a known hole, not fixed here.

**Engine tier** (real engine, hidden desktop, 42 tests of 10 files):

| File | Result on Windows |
|---|---|
| test_launch_engine, test_game_engine, test_control_engine | 19 passed: launch, status, stop; state, sexp, lua and their refusals; pilot (pitch, fire, cancel, pause), time compression, pause and resume, autopilot, clock; a call after the engine was killed reports it gone |
| test_record_engine | 1 skipped: it records a key press, and the input tools are not on Windows yet |
| test_wait_engine | 4 passed; 1 failed: the VM's mission has no event named "Addressing Transports" (game data, not the OS) |
| test_stages_engine | 5 passed; 2 failed: both expect the "new settings file" popup, which Windows sessions do not get because the harness has already written that file |
| test_tools_engine | 2 passed (`api_docs`, `frame_stats`); 1 failed: no model named "Ulysses" in the VM's data (`model_info` itself listed 176 models) |
| test_validate_engine | 1 passed (a bad table is reported); 2 failed: one needs the MediaVPs mod folder, one sends SIGSEGV, which is not a crash on Windows |
| test_compare_engine | 1 failed: the picture comparison needs ImageMagick, which the VM does not have (the verb now says so before starting any session) |
| test_bridge_guard_engine | 3 passed |

Also run once by hand: `version`, `guide`, `list`, `log`, `cleanup` (it removed the 12 ended
sessions' folders). Not run on Windows at all: `answer_popup`, `compare`,
`renderer_compare`, `journal`, `post_event`, `replay`, `scratch_mod`, `telemetry`, `vp`.

CI, first run on GitHub's Windows runner (run 44049152806): the Windows job passed. The
Linux job had been failing since this branch first reached CI, on sandbox tests from before
this plan that had never run there: display 0 is the session's own on a machine with no
desktop, and one runner image has no `keyctl`.

## What the Plan B review measured (2026-11-17, 01:10 to 03:10)

The review of Plan B (four reading seats and a mutation run) asked questions only Windows
could answer. Each row was run in the same VM; the commands are in `probes/xplat/` or are
plain `fsoctl` calls, and the live tests are `tests/winlive/`.

| Question | How | Result |
|---|---|---|
| Can a session record be rewritten while other processes read it? (probe 11, `record_race.py`) | one writer replacing a JSON file 2000 times, two readers looping on it | Windows: 31 to 33% of the writer's renames and about 10% of the reads failed with "permission denied"; the file was also momentarily absent to a reader (2 of 8 test runs). Linux: no failure. After the retry in `session._while_busy` / `_read_record`: no failure in three runs |
| Does an exclusive create refuse a planted link? | a link at the temp name whose target does not exist, then `win_safefs.write_atomic` | Windows FOLLOWED the link and created the target. With a link to an existing file the create is refused, which is why the first test could not tell. Fixed with a random temp name; the test failed before and passes after |
| Is "no windows" from `EnumWindows` a failure? | polling a session desktop while its first window is being made | it returns zero with error code 0 on a listing that worked; only a zero WITH an error code is a failure |
| What does asking about an unused process id say? | `OpenProcess` on a pid no process has | fails with "invalid parameter" (87); the live test holds `pid_ended` to it |
| Is an ended process gone at once? | a normal `stop` from the session's own login, before and after the change | for a moment it can still be opened and still gives its start time: the first version of "runner known alive" took an ended runner for a live one and `stop` carried the other-login note. Asking "has it ended" first removed it |
| What does a session look like from another Windows login? | a live desktop session, then `list`, `cleanup --older-than 0` and `stop` over ssh | its job is "not found" there and `list` shows it not alive; the runner's process id and start time can be read; cleanup removed only an old ended session; `stop` answered at once with a note, and the session's runner closed the engine (exit code 0) within 30 s |
| Does a runner that is ended by force leave its engine? | `taskkill /F` on the runner of a live session, then `cleanup` | yes: `status` still said running and alive with the engine up. The next cleanup ended it and recorded `ended_by: runner_lost`; `crash_report` says so in words |
| The same two, after the fourth fix wave (03:30) | a live desktop session, then over ssh: `state`, `crash_report`, `cleanup --older-than 0`, `stop`. Then a second session whose runner was ended by force, and over ssh: `cleanup --older-than 0`, `stop` | live session: `state` refuses with "runs in another Windows login", `crash_report` says it still runs there, cleanup removes nothing, `stop` answers `runner_first: asked` with status still "running", and 45 s later the desktop login shows it stopped (`ended_by: stop`, exit code 0). Engine without its runner: the ssh cleanup kept its record (it removed only the earlier, ended session), the ssh `stop` refused and named the engine's process id, the desktop login still showed it running and alive, and the desktop login's cleanup ended it (`ended_by: runner_lost`). A `--visible` screenshot through the bounded worker: 1280x720. Probe 11 again: 1500 writes, 7380 list reads and 8616 loads, no error |
| After the fifth fix wave (03:50 to 04:00) | a session whose runner was ended by force, then `stop` from the SAME login, three times; the engine-tier test that kills an engine and asks at once (`test_control_engine.py -k killed`), five times; a live session from a second login once more; Python's listing of `C:/Windows/.. ` | each `stop` answered "stopped" (`ended_by: stop`, no exit code: there was no runner to record one) and the session was not alive afterwards: no "another login" answer in the session's own login. The engine test passed five times of five. From the second login `state` refused by name, `stop` answered "asked", and 45 s later the session was stopped with exit code 0. The listing was "path not found": a name of two dots and a space did not reach the parent folder through that route |
| Is a folder the harness makes outside the profile private? | a run folder under `C:\`, its permissions as made and after one `icacls` line, and a session under it | as made it inherits "Authenticated Users: modify". After `icacls C:\fsoh-private /inheritance:r /grant:r "%USERNAME%:(OI)(CI)F"` only the user is listed, the run folder below inherits that, and a session ran its whole life there |
| Is a session's folder removed by the cleanup that ended it? (asked by CI, not planned) | the live test of an abandoned start, whose stand-in runner and engine have `runner.log` in the run folder open: on CI's Windows machine (GitHub's hosted Windows image) and in the VM | on CI, three runs of three failed (runs 44079561962, 44082540519, 44083251849): "in use" (error 32) on `runner.log`. The third run recorded more. When the cleanup returned, the job was empty, the runner read as ended and the engine still as not ended; the folder held `prefs`, `runner.log`, `session.json` and `session.lock`; a second cleanup 16 ms later removed it. The VM never showed it: 34 runs of 34. With the cleanup's bounded retry (commit 2ee0348) CI passed (run 44086283285) and the VM passed 22 of 22. So an empty job does not mean its files are free, and for an instant the last process of an ended job still reads as alive |
| Why did one unit test fail once on Windows? (asked by the VM, not planned) | `tests/test_record.py`, the journal test: once in a run of the whole unit tier, then alone, 140 runs with the old test stand-in for the bridge and 140 with the new | the whole tier failed it once ("taken_unknown" where "withdrawn" was expected). Alone, with the old stand-in, the test never failed, but in 5 runs of 140 the stand-in's thread ended with PermissionError in `_peek`: it read a command it was told to ignore on every turn of its loop, and on Windows a rename and an open of one file refuse each other, so the caller's withdraw and that read could meet and either could lose. With the stand-in reading once: 0 of 140, and the whole tier passed (1101). The real bridge never opens a command it has not claimed, so the product is not affected |
| Does the runtime limit end a real engine? | `launch --runtime 25`, then `status` | `exited`, `ended_by: runtime_limit`, exit code 0 (and again at 300 s on a later session) |
| Does `launch --visible` work? | launch, screenshot, status, stop | yes: 1280x720 picture, `desktop: None`, `visible: True`, stopped with exit code 0 (software rendering) |
| Is the CPU cap really put on the job? | a live test starts a job with a one-core quota and reads the limit back | hard cap, rate = 10000 / number of processors |
| Does a session work under a run folder with a space, an apostrophe, an accent? (probe 12, `accent_root.py`) | launch, state, screenshot, stop, cleanup under a runtime root of that name | "plainrun", "two words" and "o'brien": all five steps worked, the picture was inside the root. "José": the engine never started its bridge and did not close when asked; the launch gave up after its whole wait. The engine's source says it does not support such a preferences path on Windows (`code/osapi/osapi.cpp`, `getPreferencesPath`). `launch` now refuses such a root up front and names the way out |
| Do the builds and tools still start with programs found by full path? | `fsoctl build C:\src\fso --config FastDebug --target Freespace2`; the socket listing in every launch; a crash report | the build ran and reported ok; every launch found the socket listing (by its full path under the Windows folder); the report had a 30-line backtrace. The PATH walk itself has only ever answered "not found" in this VM: cmake comes from the build folder's record and cdb from the SDK folder. What holds the walk on real Windows is its unit tests on GitHub's Windows runner |

Seen, and the same on both systems: a launch reply's `state` field read "NOT A VALID STATE"
in every Windows launch here (the engine's state before its first game state), while `state`
a moment later gave `GS_STATE_INITIAL_PLAYER_SELECT`. A Linux launch the same night
(s4ef63510) replied the same way, so this is how a launch reply reads, not a Windows
difference; `wait_for state ...` is the documented next step after a launch.

Test counts on the final tree are in the plan's Status block.

## Not measured

1. **A real graphics card on a hidden desktop.** The VM has none. One command on any Windows PC
   with a graphics card settles it (Plan B, "Unmeasured").
2. **Vulkan** on any Windows desktop.
3. **macOS** beyond the combo routes (no Mac and no macOS VM; the runner cannot start the engine).
4. **qtFRED's real Windows build** through UI Automation (probes 2 and 8 used the Qt stand-in;
   the 26.2.0 Windows release contains qtFRED and it is in the VM). Plan C's first probe.
5. ~~The command channel and bridge mod on Windows.~~ Measured since: "Plan B live checks".
6. **Real input in a visible session** (SendInput to a foreground engine or editor).
7. **A game folder or a mod name with a letter outside ASCII.** Probe 12 tried the run
   folder only (it takes `--fs2path` for the other).
7a. **A session started from an administrator prompt, seen from an ordinary one.** The code
   treats any failed job lookup except "not found" as "may be there"; that case never ran.
8. **Whether the firewall question shows for an engine on a hidden desktop,** and what
   answering Cancel does to the rules Windows added.
9. **Which of the engine's two answers to a preferences path outside ASCII** was the one
   seen in probe 12 (an error it waits on, or its portable mode).
10. **A Windows in another language,** and **the time limit across the PC sleeping.**

## Cautions

One VM, one Windows build, software rendering, FSO 26.2.0, one mission, one test ship. Each
final result is one run per cell (FRED2: two cells; combo: two plus CI; engine: three). Two
earlier passes the same day with earlier probe versions gave the same yes/no in every row
they shared; they are named where they are the only source. The "TOOK" rows are not rates. Timings are from a VM under
software rendering.

## What the review changed

- **The "data took" check.** The first version closed and reopened the Ship Editor and read
  the control again. FRED2 only hides and shows that dialog, so the control was read twice and
  the check could not fail: it reported "data took" for a rename FRED2 had refused, and for a
  player ship's AI Class field that FRED2 never stores. Replaced by Next/Prev and the saved
  file (above), with a unit test in which a dialog that does not change ship is not vouched for.
- **The engine's keys.** The first note said keys arrive "only while its window is the
  foreground one" from two cells that differed in two ways. The "behind" cell, the frame
  count and the posted-message results were added.
- **Unsupported sentences** were removed or sourced: a "71-event mission" (the tree lists 62),
  "two captures differ" judged by eye (now `idle_frames`), a real-key combo row marked "no"
  that had not been judged on the hidden desktop, `CB_SETCURSEL` "with or without the notice"
  measured on one combo only (now both), "reports no error" for a route that raised on the
  hidden desktop.
- **The hidden-desktop launcher** ended only the probe on a timeout and left FRED2 or the
  engine running unseen; it now runs everything in a job that Windows ends with it. Checked
  in the VM (about 19:55, output not kept as a result file): with a 10 s limit
  on a script that starts a child of its own and sleeps, the launcher returned 5 and both
  processes were gone; a child that exits 3 gave 3.

A second review round read that fix wave and found more, all fixed before the final pass:
a combo route counted as "took" if Qt was ever on the target item, not if it ended there; the
saved-file reader could take a turret's AI class for the ship's; the Next/Prev check accepted
any ship but the neighbour as "back"; the launcher left a child behind if it could not be put
in its job; and nothing showed the engine was ready for keys in the cells where they failed
(now the "brought to the front" row). The first version of this note also said "two
independent witnesses" for every FRED2 row; the saved file covers only the last change.

## What this means for the plans

- **Plan B (platform layer, Windows game sessions):** keep the hidden-desktop design; the
  engine runs, binds, draws and is captured there. Posted keys cannot drive a hidden session
  (as the spec says), nor a visible one whose window is not in front; other input routes are
  unmeasured. Find windows
  through Windows' own enumeration, never through a UI Automation search from the root.
- **Plan C (editors):** FRED2 can be driven by control ID with window messages, hidden; use UI
  Automation only for trees, reached from a window handle. Combo boxes: messages for FRED2,
  "open through accessibility, then choose in the list" for qtFRED. The driver needs an
  enabled check, a message-box tool, a pause between steps, and a way to confirm a change in
  the data rather than in the control, from the start.
