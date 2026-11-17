# Cross-platform probe results (Plan A, Task 10)

Date: 2026-10-25. Probes run on GitHub's windows-latest, macos-latest (arm64) and ubuntu-latest
runners from the private CI repo (workflow `.github/workflows/probes.yml`, manual dispatch), and
on Linux by hand. Final results were taken on commit a002363 (after the probes' QA fix wave):
CI runs 40254479081 (probe 1), 40254480848 (probe 2), 40254483041 (probe 3), 40254484927
(probe 5), 40254487289 (probe 6). Raw results: `docs/research/xplat/probe-*.json`.
Everything ran with no game data (licensing). QA record:
`fso-harness-artifacts/qa-xplat-plan-a-probes/` (outside the repo).

## Summary for Plan B

| Probe (design list) | Result | What it means for Plan B |
|---|---|---|
| 1. Engine renders on a hidden Windows desktop; input and UI Automation reach it | Yes, with software OpenGL (Mesa llvmpipe) | Hidden desktops are a workable "out of sight" mechanism. Real-GPU behaviour on a hidden desktop is not yet measured. |
| 2. Qt widgets by object name in each accessibility layer | Yes on all three, except combo selection | Accessibility driving works for text fields, buttons and trees; combo boxes need another route. |
| 3. FRED2 controls by resource ID in UI Automation | Not measurable on CI | Needs the manual run with retail data on the owner's Windows 10. |
| 4. macOS Accessibility permission on a CI runner | Already granted on the runner | macOS accessibility tests can run on CI; users must still grant it themselves. |
| 5. Release builds available; the engine starts on a GPU-less runner | Builds yes; start: Linux yes, Windows with Mesa, macOS no | CI can run the engine on Linux and Windows (with Mesa); not on macOS runners. |
| 6. Per-session audio without admin rights | Yes on all three | `--sound` can be cross-platform through OpenAL Soft's wave writer. |

## Probe 1: a hidden Windows desktop (Task 5)

**What ran.** `probes/xplat/win_hidden_desktop.py` made a second desktop (CreateDesktopW) in the
runner's WinSta0, started a small SDL2 OpenGL program (`gl_square.py`) and the FSO 26.0.1
FastDebug engine on it, and checked rendering, PrintWindow capture, UI Automation and a posted
key. Each check also ran on the normal desktop as a control, once with the runner's OpenGL and
once with Mesa 26.2.4 (llvmpipe, software) next to the program.

**Result.**
- On Mesa, the hidden desktop behaved exactly like the normal one: an OpenGL 4.6 core context,
  the cleared colour read back exactly, the same colour captured by PrintWindow, the window
  found by UI Automation, the posted Space received by SDL, and the engine reached "OpenGL init
  is complete!".
- The runner's own OpenGL is Windows' GDI renderer, version 1.1. The small program still drew
  and was captured, but the engine refused it: "Current GL Version of 1.1 is less than the
  required version of 3.2" (`gl_meets_minimum: false` in those rows).

**Not measured.** A real GPU driver on a hidden desktop (the plan's optional Step 4 on the
owner's Windows 10; the probe is safe to run there since the QA fix wave: it uses its own
preferences folder and a copy of the engine). Vulkan (no GPU on the runner).

**For Plan B.** Keep the hidden-desktop design for Windows sessions. CI needs Mesa beside the
engine. Whether a hidden desktop gets hardware rendering on a real GPU decides whether hidden
sessions are fast or software-rendered on users' machines; that is the first manual check.

## Probe 2: Qt widgets through each accessibility layer (Task 6)

**What ran.** `qt_standin.py` (PySide6, Qt 6.12.0) shows a dialog with a line edit, a combo box,
a tree and an OK button named like qtFRED's widgets; `a11y_probe.py` drove it only through UI
Automation (Windows), the Accessibility API (macOS) or AT-SPI (Linux, by hand under
`linux_a11y_session.sh`). On OK the stand-in records what Qt itself holds (`values.json`).

**Result, the same on all three.**
- Object names are exposed, as the same dotted path everywhere:
  `QApplication.standinDialog.shipNameEdit` in AutomationId / AXIdentifier / accessible-id.
- Setting the line edit's text, finding both tree items (parent and child), and pressing OK all
  work, and Qt confirms the text (`qt_saw_name: true`).
- **Selecting a combo item does not take.** Every method tried reported success and Qt kept the
  first item (`qt_saw_class: false`):
  - Windows: expand + SelectionItem.Select; expand + Invoke on the item.
  - macOS: open + AXPress on the item; setting the combo's AXValue.
  - Linux: Selection.selectChild on the popup list; the item's Toggle action; both again after
    the combo's "Show Menu" action (by hand, 2026-10-25).
- macOS: the runner's process is already trusted for accessibility (`needs_permission: false`).

**Version note.** qtFRED builds with Qt 6 on master (`find_package(Qt6 ...)`); the 26.0.1
release source still used Qt 5. qtFRED is not in any release binary, and the harness builds it
from source, so the Qt 6 stand-in matches what the harness drives. A Qt 5 qtFRED was not tested.

**For Plan B.** Use the accessibility method for fields, buttons and trees. Combo boxes need a
different route; the candidate is focusing the combo through accessibility and choosing the
item with keyboard input, which needs its own small probe before Plan B relies on it. On Linux
the existing pixel method stays available.

## Probe 3: FRED2 in UI Automation (Task 7)

**What ran.** `fred2_uia.py` started the FRED2 26.0.1 release with no game data and dumped the
first window it showed, sorting each numeric AutomationId against `fred2/resource.h`.

**Result.** With no data, FRED2 only reached SDL's own error boxes: first "OpenAL 1.1 or newer
is required" (no audio device), then, with OpenAL's null output, the OpenGL 3.2 error, then,
with Mesa beside it, the missing-data error. Numeric control IDs do come through as
AutomationIds (100, 101 and -2, SDL's button and text IDs), so the mechanism works for Win32
controls; none of them is a FRED2 ID, as expected for an SDL box.

**For Plan B.** Whether FRED2's own dialogs expose their resource IDs, and whether its event
tree can be walked, is still open. It needs the plan's Step 2: FRED2 with retail data on the
owner's Windows 10, opening the Ship Editor. Until then, FRED2 driving is a design assumption.

## Probe 4: macOS Accessibility permission (Task 8, via probe 2)

The macOS runner's process is already trusted (`AXIsProcessTrusted`), so accessibility tests
run on CI without setup. On a user's Mac the permission has to be granted by the user once.

## Probe 5: release builds and starting the engine (Task 8)

**Builds.** FSO 26.0.1 publishes Windows zips (x64 and Win32, AVX and SSE2; engine and FRED2,
each with a FastDebug build), macOS tarballs (arm64 and x86_64; engine only, with its own
OpenAL framework), and Linux AppImages. None contains qtFRED.

**Start, no data, `-window -nosound`.**
- Linux (Xvfb, Mesa): reached OpenGL init, then stopped at the missing-data error. The AppImage
  does not include `libopenal.so.1`; without the system package it exits 127 at once.
- Windows: the runner's OpenGL 1.1 is refused; with Mesa it reaches OpenGL init (probe 1).
- macOS: no OpenGL pixel format at all ("Failed creating OpenGL pixel format", then "Unable to
  initialize display device"). The runner is a VM without a GPU and the engine asks for an
  accelerated pixel format.
- No UDP port was held in any run: the engine opens its game port after graphics init, and with
  no data it stops inside graphics init. The real-network default could not be observed here.

**For Plan B.** Live engine tests can run on Linux and Windows CI (Windows with Mesa), not on
macOS runners; macOS engine runs are the owner's or a user's. Document the Linux AppImage's
OpenAL requirement.

## Probe 6: per-session audio (Task 9)

**What ran.** `openal_wave.py` loaded the OpenAL library each release ships, pointed it at a
per-session config (`ALSOFT_CONF`, `drivers = wave`, `[wave] file = ...`), played a one-second
440 Hz tone, and read the resulting file's peak level.

**Result.** The Windows and macOS releases ship OpenAL Soft 1.23.1; the Linux AppImage ships
none and uses the system's (probe 5), so the Linux run used the engine build's own libopenal,
also 1.23.1. On all three the tone landed in the session's own file (peak 0.29 on a 0-1 scale)
with no audio device and no admin rights. The
wave writer's device reports itself as "OpenAL Soft" in 1.23.1, so the probe uses the file's
appearance, not the device name, to know the writer opened.

**For Plan B.** Make `--sound` cross-platform with a per-session `ALSOFT_CONF` and wave file.
No virtual audio device is needed on any OS.

## Open items handed to Plan B
1. Real-GPU rendering on a hidden desktop (owner's Windows 10, probe 1 Step 4).
2. FRED2 resource IDs and event tree with retail data (owner's Windows 10, probe 3 Step 2).
3. A combo-box route (accessibility focus plus keyboard input), probed on all three OSes.
4. Vulkan on a hidden desktop (needs a GPU; owner's Windows 10).
