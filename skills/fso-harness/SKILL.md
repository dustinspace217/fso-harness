---
name: fso-harness
description: Test FreeSpace Open engine changes, mods and missions headlessly through the fso-harness tools (fsoctl CLI or the fso-harness MCP server). Use when asked to test, playtest, validate, compare or debug anything that runs in the FSO engine or qtFRED.
---

# fso-harness

The harness runs the real engine on a private virtual display and gives you tools to launch,
look, act, wait, build, validate and compare. The tools are the same under two names: MCP tools
(`launch`, `state`, `sexp`, ...) or `fsoctl --json <tool> ...` on the command line.

## The one thing not to miss: test by script, not by flying

A mission or an engine change is tested fastest from outside the cockpit:

1. `launch` with `mission`, `pilot: "create:Test"`, all `skip_*` flags, `dismiss_popups` and
   `allow_eval: true`.
2. `wait_for state GS_STATE_GAME_PLAY`.
3. `state` to read the goals, events and ships; those names are what you wait on.
4. `autopilot on`, `time` compression 400 or more.
5. `sexp` mode `eval` asks (`( is-destroyed-delay 0 !Alpha 2! )`; `!` quotes strings), mode
   `number` reads a value, mode `run` makes anything happen that a mission event could.
6. `lua` reaches the whole scripting API of this build (`api_docs` writes it out).
7. `wait_for event_true | goal | destroyed` pins the moments; `state` at each; `log` for
   warnings; `stop`.

`replay` repeats a session's actions on another build; `compare` runs a scenario file on two
builds side by side; `renderer_compare` on OpenGL and Vulkan.

## Other recipes

In the MCP server they are commands: `/fso-harness:test_engine_change`, `check_mod`,
`compare_builds`, `read_crash`, `reproduce_multiplayer_bug`. The full text is
`docs/AGENT-GUIDE.md` in the harness repo. Multiplayer: `match_create`, then `launch` with
`match` and `role` (`standalone`, `host`, `client`) into that private network, which nothing on
the LAN or the internet can reach; `match_remove` ends it.

## What the tools refuse, and why

- `lua`, `post_event` and sexps that run Lua need a session launched with `allow_eval` (audited).
- Event and ship names must exist in the mission and may not contain `" ! ( )`; sexps are capped
  below 8000 bytes (both would otherwise reach the engine's sexp parser in ways it does not
  survive).
- qtFRED opens a copy of a mission, never the original; `launch --qtfred` refuses anything else.
- Mods under test go in a `scratch_mod`; the harness never writes into the user's game folder
  apart from its own bridge mod and those scratch folders.

## Reading a failure

Every call returns JSON with `message` and `hint`. Exit codes: 5 timeout (the message says
whether the command was withdrawn or already taken), 6 the engine is gone (`crash_report`),
7 refused (your input), 8 a Lua error in the bridge or your code, 9 the work failed (a compile).
A `validate` that finds problems is a result, not a failure. Always `stop` sessions you started.
