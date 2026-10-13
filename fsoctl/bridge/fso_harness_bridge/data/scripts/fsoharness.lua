-- FSO harness bridge: runs inside the engine, polls numbered command files in its cache folder,
-- runs each command and writes a numbered reply. Loaded by fsoharness-sct.tbm via require().
--
-- Rules this file keeps (each learned on the real engine, docs/research/2026-10-12-probe-results.md):
--   * File access only through the engine's cf API, never io.* (research note on io.lines/io.input).
--   * Never open a file in append mode: the debug build asserts on cf append to an existing file.
--   * All per-frame work runs inside pcall: a Lua error escaping a hook opens a blocking error box
--     (or aborts under -noninteractive), which would hang or kill the session being tested.
--   * Popups are answered by Submit AND an override on that frame (take_popup_override), because
--     the engine discards a bare Submit and re-shows some popups forever.
--   * Never call error(): the engine's built-in forwarders.lua replaces the global error (and
--     print) with ba.error, which is a fatal engine Error() even inside pcall. Refusals go through
--     H.raise (assert, which is not replaced). Never read a field that does not exist on a handle:
--     engine-raised Lua errors cannot be caught by pcall under -noninteractive and end the engine.
--   * Startup files the harness leaves here are never *.json or *.bin: with a mod loaded, the
--     OpenGL renderer deletes those extensions from mod cache folders while it starts
--     (gropenglshader.cpp opengl_purge_shader_cache_type), before On Game Init runs. Command and
--     reply files are *.json but only exist after the first heartbeat, i.e. after that purge.
local json = require("dkjson")

local H = {}
H.VERSION = "unknown"          -- replaced at init from fsoharness-version.txt (see read_version)
H.DIR = "data/cache"           -- resolves to <prefs>/<first mod>/data/cache for writes
H.HEARTBEAT_EVERY = 30         -- at most this many frames between ready.json writes
H.HEARTBEAT_SEC = 1.0          -- or this much real frame time, whichever comes first
H.frames = 0
H.gameplay_frames = 0          -- normal GS_STATE_GAME_PLAY frames (see H.frame)
H.handlers = {}                -- name -> function(args, seq) returning a result or H.DEFERRED
H.state_hooks = {}             -- {name=, fn=} called once when the game state changes to `name`
H.pending_hooks = {}           -- {hook=, tries=} hooks of the current state not yet run cleanly
H.HOOK_TRIES = 3               -- attempts (one per frame) before a failing state hook is dropped
H.timers = {}                  -- {tick=fn() -> done, on_error=fn(err)} for multi-frame commands
H.switches = {}                -- startup switches (switches.txt) written by the harness
-- Outcome of each startup stage switch: name -> "pending" | "done" | "failed: <why>", reported in
-- the heartbeat (QA finding SF-1). The metatable makes dkjson write {} rather than [] when empty.
H.switch_status = setmetatable({}, { __jsontype = "object" })
H.allow_eval = false
H.popup = nil                  -- text of the popup currently up, or nil
H.last_state = ""
H.last_error = nil             -- last Lua error caught by the frame guard (reported in heartbeat)
H.errors = 0
H.override_pending = false
H.DEFERRED = {}                -- sentinel: the handler will reply later (from a timer)

-- Raise a refusal the bridge can catch: assert is the standard Lua one (error is not, see above).
-- The message carries msg and hint between \1 markers so run_command can split them again.
-- Replies built from it say kind "refused" (contract C2: the caller's request was not acceptable).
function H.raise(msg, hint)
	assert(false, "\1" .. tostring(msg) .. "\1" .. tostring(hint or ""))
end

-- Like H.raise, but for a failure inside Lua the caller supplied (the lua command): the reply says
-- kind "script_error", which fsoctl maps to its own exit code instead of "refused" (QA SF-18).
function H.raise_script(msg, hint)
	assert(false, "\2" .. tostring(msg) .. "\2" .. tostring(hint or ""))
end

-- Split an error value from pcall into (message, hint, kind). Errors from H.raise are "refused";
-- H.raise_script and every plain Lua error (a bridge bug, or a runtime error in caller Lua) are
-- "script_error".
function H.decode_error(err)
	local text = tostring(err)
	local msg, hint = string.match(text, "\1(.-)\1(.*)$")
	if msg then return msg, hint, "refused" end
	msg, hint = string.match(text, "\2(.-)\2(.*)$")
	if msg then return msg, hint, "script_error" end
	return text, "bridge or script error", "script_error"
end

-- Count an error caught by one of the bridge's guards; the heartbeat and bridge_info report both.
function H.note_error(err)
	H.last_error = tostring(err)
	H.errors = H.errors + 1
end

-- Read a whole file from the channel folder; nil if it does not exist or cannot be opened.
local function read_all(name)
	local f = cf.openFile(name, "r", H.DIR)
	if not (f and f:isValid()) then return nil end
	local s = f:read("*a")
	f:close()
	return s
end
H.read_all = read_all

-- Write a file so readers never see half of it: write a temp name, then rename over the target.
-- No delete first: cf.renameFile is rename(2) (cfile.cpp cf_rename), which replaces an existing
-- target atomically on Linux, so readers always find either the old file or the new one. Deleting
-- first left a gap with no ready.json, which status read as "no heartbeat" (QA finding XK-6).
-- The rename's return value is not used: the engine inverts it (see run_command).
local function write_atomic(name, text)
	local tmp = name .. ".tmp"
	local f = cf.openFile(tmp, "w", H.DIR)
	if not (f and f:isValid()) then return false end
	f:write(text)
	f:close()
	cf.renameFile(tmp, name, H.DIR)
	return cf.fileExists(name, H.DIR) and not cf.fileExists(tmp, H.DIR)
end
H.write_atomic = write_atomic

-- Name of the current game state, e.g. "GS_STATE_MAIN_MENU".
function H.state_name()
	local st = ba.getCurrentGameState()
	if st and st:isValid() then return st.Name end
	return "none"
end

-- Write the reply for command `seq`: ok=true with a result, or ok=false with an error, a hint and
-- a kind ("refused" unless given; see H.decode_error and contract C2).
function H.reply(seq, ok, payload, hint, kind)
	local body
	if ok then
		body = { seq = seq, ok = true, result = payload }
	else
		body = { seq = seq, ok = false, error = tostring(payload), hint = hint or "",
		         kind = kind or "refused" }
	end
	write_atomic(string.format("res-%06d.json", seq), json.encode(body))
end

-- Register fn to run once each time the game enters the state called `name`. `switch` names the
-- startup switch the hook carries out, so a hook that keeps failing marks that switch failed.
function H.on_state(name, fn, switch)
	H.state_hooks[#H.state_hooks + 1] = { name = name, fn = fn, switch = switch }
end

-- Record a stage switch's outcome; a failure also counts as a bridge error, so the heartbeat's
-- errors / last_error show it without a bridge_info call.
function H.set_switch(name, outcome)
	H.switch_status[name] = outcome
	if string.sub(outcome, 1, 7) == "failed:" then H.note_error(name .. " " .. outcome) end
end

-- Ask for an override on this dialog frame (used together with hv.Submit by popup handling).
function H.request_popup_override()
	H.override_pending = true
end

-- Called by the table's +Override block on dialog frames: true once after a request, then false.
function H.take_popup_override()
	local o = H.override_pending
	H.override_pending = false
	return o
end

-- The installed bridge's version: the hash fsoctl's bridge_install writes into the bridge mod's
-- data/text folder (contract C5), so bridge_info shows which copy the engine actually loaded (QA
-- finding TA-17). Read through the cf path type "data/text", which finds the file in the bridge
-- mod (first in -mod). NOT data/scripts: the engine indexes only .lua/.lc/.fnl files there
-- (cfile.cpp:86), so a .txt in that folder is invisible to cf; data/text indexes .txt
-- (cfile.cpp:56). "unknown" when the file is missing or unreadable.
local function read_version()
	local ok, text = pcall(function()
		local f = cf.openFile("fsoharness-version.txt", "r", "data/text")
		if not (f and f:isValid()) then return nil end
		local s = f:read("*a")
		f:close()
		return s
	end)
	local v = ok and type(text) == "string" and string.match(text, "^%s*(.-)%s*$")
	if v and v ~= "" then return v end
	return "unknown"
end

-- The stage switches the bridge itself carries out; each starts "pending" when it is on.
local function init_switch_status()
	if H.switches.pilot_mode == "create" then H.switch_status.pilot_mode = "pending" end
	if H.switches.skip_command_briefing then H.switch_status.skip_command_briefing = "pending" end
	if H.switches.skip_briefing then H.switch_status.skip_briefing = "pending" end
	if H.switches.multi_role == "client" then H.switch_status.multi_join = "pending" end
end

-- On Game Init: read the startup switches and the eval opt-in the harness left in the channel.
function H.init()
	H.VERSION = read_version()
	local sw = read_all("switches.txt")
	if sw then H.switches = json.decode(sw) or {} end
	H.allow_eval = cf.fileExists("allow_eval", H.DIR)
	init_switch_status()
end

-- Take, read and run one command file; any failure becomes an error reply, never a Lua error.
-- WHY take it by renaming first: a caller that times out withdraws its command by renaming it to
-- .withdrawn and then reports "it did not run" (channel.py, QA finding SL-6). If the bridge read
-- first, the caller's rename could still succeed after the read and the command would run anyway.
-- With rename-then-read exactly one rename wins. cf.renameFile's return value is inverted in this
-- engine (it returns cf_rename(...) != 0 and success is 0, cfile.cpp; probed 2026-10-12), so the
-- claim is judged by whether the claimed name exists, which holds whichever way that is fixed.
local function run_command(name)
	local taken = name .. ".run"
	cf.renameFile(name, taken, H.DIR)
	if not cf.fileExists(taken, H.DIR) then return end   -- withdrawn by the caller first
	local text = read_all(taken)
	cf.deleteFile(taken, H.DIR)
	local cmd = text and json.decode(text)
	if type(cmd) ~= "table" or not cmd.seq then return end
	local handler = H.handlers[cmd.cmd]
	if not handler then
		H.reply(cmd.seq, false, "unknown command " .. tostring(cmd.cmd), "see `fsoctl --help`")
		return
	end
	local ok, result = pcall(handler, cmd.args or {}, cmd.seq)
	if ok then
		if result ~= H.DEFERRED then H.reply(cmd.seq, true, result) end
	else
		H.reply(cmd.seq, false, H.decode_error(result))
	end
end

-- Run every waiting command file in sequence-number order (gaps are fine: a caller that timed
-- out withdraws its file, so the bridge must not wait for a missing number).
local function poll_commands()
	-- Only command files: replies nobody collected (a caller that timed out) would otherwise make
	-- this per-frame scan grow (QA finding SL2-8). The filter must start with * (cfile.cpp
	-- listFiles); folder files are matched with fnmatch, so "*cmd-*.json" selects cmd-N.json.
	local files = cf.listFiles(H.DIR, "*cmd-*.json")
	if not files then return end
	local pending = {}
	for _, f in ipairs(files) do
		local n = string.match(f, "^cmd%-(%d+)%.json$")
		if n then pending[#pending + 1] = { n = tonumber(n), f = f } end
	end
	table.sort(pending, function(a, b) return a.n < b.n end)
	for _, p in ipairs(pending) do run_command(p.f) end
end

-- Advance multi-frame commands; a timer returns true when it has finished (and replied). A timer
-- that raises is dropped, and its on_error (if any) runs so the command can release what it holds
-- and answer its caller (QA findings SL-5 / CR-10: a failing pilot tick used to leave the player's
-- controls locked and the caller waiting for a reply that never came).
local function tick_timers()
	local keep = {}
	for _, t in ipairs(H.timers) do
		local ok, done = pcall(t.tick)
		if not ok then
			H.note_error(done)
			if t.on_error then
				local ok2, err2 = pcall(t.on_error, done)
				if not ok2 then H.note_error(err2) end
			end
		elseif not done then
			keep[#keep + 1] = t
		end
	end
	H.timers = keep
end

-- Run the current state's hooks that have not yet run cleanly, each in its own pcall so one
-- failing hook neither skips the others nor the rest of the frame (QA finding SF-1). A failing
-- hook is tried again on the next frames, up to H.HOOK_TRIES times, then its switch is marked
-- failed.
local function run_pending_hooks()
	local keep = {}
	for _, p in ipairs(H.pending_hooks) do
		local ok, err = pcall(p.hook.fn)
		if not ok then
			p.tries = p.tries + 1
			H.note_error(err)
			if p.tries < H.HOOK_TRIES then
				keep[#keep + 1] = p
			elseif p.hook.switch then
				H.set_switch(p.hook.switch, "failed: " .. (H.decode_error(err)))
			end
		end
	end
	H.pending_hooks = keep
end

-- Queue the registered state hooks when the state name changes (polled each frame, which the
-- probes showed is reliable; On State Start can run after the first frame of the new state), then
-- run whatever is still pending. Leaving a state drops its unfinished hooks.
local function check_state_change()
	local name = H.state_name()
	if name ~= H.last_state then
		H.last_state = name
		H.pending_hooks = {}
		for _, h in ipairs(H.state_hooks) do
			if h.name == name then H.pending_hooks[#H.pending_hooks + 1] = { hook = h, tries = 0 } end
		end
		-- Every startup stage lies before gameplay, so a switch still pending now is for a screen
		-- this run never showed (no command briefing in the mission, say), not one in progress
		-- (QA finding CR2-9).
		if name == "GS_STATE_GAME_PLAY" then
			for k, v in pairs(H.switch_status) do
				if v == "pending" then H.switch_status[k] = "not shown" end
			end
		end
	end
	run_pending_hooks()
end

-- Write ready.json on the first frame, then whenever H.HEARTBEAT_EVERY frames or H.HEARTBEAT_SEC
-- of real frame time have passed since the last write, whichever comes first. WHY the time rule:
-- fsoctl treats a heartbeat older than 5 s as a hung engine, and below 6 fps 30 frames take
-- longer than that (7.5 s at 4 fps), so a slow but healthy engine would read as hung (QA SL2-3).
local heartbeat
local since_hb = { frames = 0, sec = 0 }
local function maybe_heartbeat()
	if H.frames == 1 or since_hb.frames >= H.HEARTBEAT_EVERY or since_hb.sec >= H.HEARTBEAT_SEC then
		since_hb.frames, since_hb.sec = 0, 0
		heartbeat()
	end
	-- Frames first: if the time read fails, the frame rule still keeps the heartbeat going.
	since_hb.frames = since_hb.frames + 1
	since_hb.sec = since_hb.sec + (ba.getRealFrametime() or 0)
end

function heartbeat()
	write_atomic("ready.json", json.encode({
		version = H.VERSION, frame = H.frames, state = H.state_name(),
		mission_time = mn.getMissionTime(), mission = mn.getMissionFilename(),
		popup = H.popup or json.null, errors = H.errors, last_error = H.last_error or json.null,
		switches = H.switch_status, telemetry_on = H.telem.on,
	}))
end

-- Everything the bridge does in one frame. in_dialog is true when called from On Dialog Frame.
-- Popups seen, oldest first, bounded; rewritten whole to popups.txt (append mode is unsafe).
H.popups = {}
H.POPUP_LOG_MAX = 200
H.pending_popup_choice = nil   -- set by the answer_popup command, used on the next dialog frame

local function log_popup(text, choice)
	H.popups[#H.popups + 1] = json.encode({ frame = H.frames, state = H.state_name(),
	                                        text = text, choice = choice })
	if #H.popups > H.POPUP_LOG_MAX then table.remove(H.popups, 1) end
	write_atomic("popups.txt", table.concat(H.popups, "\n") .. "\n")
end

-- On a dialog frame: answer the popup if the harness asked to (answer_popup) or if the session
-- was launched with dismiss_popups (button 0, the first button). Submit alone is not enough; the
-- override request makes the engine keep our choice (probe results section 2).
local function handle_popup()
	local choice = H.pending_popup_choice
	-- The join screen's "Joining netgame" box has one button, Cancel (multiui.cpp:1000):
	-- dismissing it abandons the join (probe-results 21.1), so dismiss_popups leaves it alone.
	-- An explicit answer_popup still works, which is how an agent cancels a stuck join.
	-- Prefix match (QA XFK-4): a build that changes the text's casing or punctuation must not
	-- turn the guard into a join-cancel loop.
	local joining = H.popup ~= nil and string.sub(H.popup, 1, 7) == "Joining"
	if choice == nil and H.switches.dismiss_popups and not joining then
		choice = 0
	end
	if choice == nil then return end
	H.pending_popup_choice = nil
	log_popup(H.popup, choice)
	hv.Submit(choice)
	H.request_popup_override()
end

-- Startup and stage events (pilot created, briefing skipped...), bounded, shown by bridge_info.
H.events = {}
H.EVENT_LOG_MAX = 50
function H.event(text)
	H.events[#H.events + 1] = string.format("frame %d %s: %s", H.frames, H.state_name(), text)
	if #H.events > H.EVENT_LOG_MAX then table.remove(H.events, 1) end
end

local function frame_body(in_dialog)
	H.in_dialog = in_dialog
	-- Normal gameplay frames map one to one to profiling.csv rows (the engine times exactly the
	-- gameplay frame flips; popups run their own loop), so frame_stats cuts its window by this
	-- count. Mission time does not line up: one probe (2026-10-12) read 1.8 s just after gameplay
	-- began. Counted here, inside the frame's pcall, because reading the state can fail (CR2-7).
	if not in_dialog and H.state_name() == "GS_STATE_GAME_PLAY" then
		H.gameplay_frames = H.gameplay_frames + 1
		-- Own pcalls: neither may stop the commands and timers below (QA SF-2).
		local ok, err = pcall(H.track_run)
		if not ok then H.note_error(err) end
		H.telemetry_frame()
	end
	if in_dialog then
		H.popup = (hv.Text and tostring(hv.Text)) or "(popup without text)"
	else
		H.popup = nil
		-- State hooks only on normal frames: acting on a state (e.g. selecting a pilot) while
		-- a popup is still up would run game code underneath a modal dialog.
		check_state_change()
	end
	poll_commands()
	tick_timers()
	if in_dialog then handle_popup() end
end

-- Hook entry point: the whole frame is guarded so a bridge bug can never become an engine error.
-- The heartbeat goes first, in its own pcall: it is the only thing that tells the harness the
-- bridge is alive and what went wrong, so an error later in the frame must not stop it (QA SF-1).
function H.frame(in_dialog)
	H.frames = H.frames + 1
	local ok, err = pcall(maybe_heartbeat)
	if not ok then H.note_error(err) end
	ok, err = pcall(frame_body, in_dialog)
	if not ok then H.note_error(err) end
end

H.handlers.ping = function() return { pong = true, frame = H.frames } end

-- What the bridge believes about itself: lets the harness confirm the startup files arrived.
H.handlers.bridge_info = function()
	return { version = H.VERSION, switches = H.switches, allow_eval = H.allow_eval,
	         errors = H.errors, last_error = H.last_error or json.null, events = H.events,
	         telemetry_on = H.telem.on,
	         switch_status = H.switch_status }
end

H.handlers.basic_state = function()
	return { state = H.state_name(), mission_time = mn.getMissionTime(),
	         mission = mn.getMissionFilename() }
end

H.handlers.quit = function()
	ba.postGameEvent(ba.GameEvents["GS_EVENT_QUIT_GAME"])
	return {}
end

H.handlers.answer_popup = function(args)
	if not H.popup then
		H.raise("no popup is open (state " .. H.state_name() .. ")", "check the heartbeat's popup field")
	end
	H.pending_popup_choice = args.choice or 0
	return { answered = H.popup, choice = H.pending_popup_choice }
end

-- ---- game state, sexps, opt-in Lua and events (Task 11) ----------------------------------------

local function vec(v) return { v.x, v.y, v.z } end

-- Read a value through pcall so a plain Lua error (e.g. indexing a nil handle) costs one field,
-- not the whole report. pcall cannot catch engine-raised errors, so only fields every ship has
-- are read this way.
local function try(fn, fallback)
	local ok, v = pcall(fn)
	if ok and v ~= nil then return v end
	return fallback
end

-- One ship as the state report shows it. A field that cannot be read is null, never a made-up
-- 0 / false / "?" that reads like a real value (QA finding SF-15: an unreadable hull showed as 0%).
local function ship_entry(s)
	local maxhp = try(function() return s.HitpointsMax end, nil)
	local shields_max = try(function() return s.Shields.CombinedMax end, nil)
	local entry = {
		name = s.Name, class = try(function() return s.Class.Name end, json.null),
		team = try(function() return s.Team.Name end, json.null),
		pos = vec(s.Position), fvec = try(function() return vec(s:getfvec(true)) end, json.null),
		hull_pct = json.null, shield_pct = json.null,
		dying = try(function() return s:isDying() end, json.null),
		orders = try(function() return #s.Orders end, json.null),
		target = json.null,
	}
	-- Target returns a ship-typed handle only when the target is a ship (ade_set_object_with_breed);
	-- reading .Name on a plain object handle would be a fatal engine-raised error.
	local t = s.Target
	if t and t:isValid() and t:getBreedName() == "Ship" then entry.target = t.Name end
	if maxhp and maxhp > 0 then
		entry.hull_pct = try(function() return s.HitpointsLeft * 100 / maxhp end, json.null)
	end
	-- A ship without shields has CombinedMax 0: its shield_pct stays null (nothing to measure).
	if shields_max and shields_max > 0 then
		entry.shield_pct = try(function() return s.Shields.CombinedLeft * 100 / shields_max end,
		                       json.null)
	end
	return entry
end

-- Sexp string quoting for a name the caller gave us. run_sexp turns a single ! into " before
-- parsing (sexp.cpp run_sexp, "HACK: ! -> \""), and " is the real string delimiter, so a name
-- holding ! or " ends the string early and the rest of the name is parsed as sexp text: that is
-- how QA finding SA-1 ran script-eval-num through a wait_for event name on a session without
-- --allow-eval (confirmed on the engine). ( and ) are refused as well so a name can never open or
-- close an expression, and control characters because a name never needs them.
local function quoted(name)
	name = tostring(name)
	if name == "" or string.find(name, '[!"()%c]') then
		H.raise("name cannot be used in a sexp: " .. string.format("%q", name),
		        'names containing ! " ( ) or control characters are refused')
	end
	return "!" .. name .. "!"
end
H.quoted = quoted

-- Longest sexp text the bridge passes on. run_sexp copies the text into an 8192-byte buffer with
-- strcpy_s (sexp.cpp run_sexp), and an overflow there is a fatal Error(), which aborts the engine
-- under -noninteractive. The margin leaves room for the "( when ( true ) ... )" runSEXP adds.
H.SEXP_MAX = 8000

-- One line in the engine's debug log for every Lua-running action the bridge carries out (QA
-- finding SA-8): the Python side audits what it sends, this records what actually ran, including
-- a command file written into the channel by something other than fsoctl. ba.println is compiled
-- out of release builds (base.cpp, #ifndef NDEBUG), so only debug builds carry these lines.
function H.audit(kind, text)
	local line = string.gsub(string.sub(tostring(text), 1, 200), "%c", " ")
	pcall(ba.println, "FSOH-AUDIT " .. kind .. " " .. line)
end

-- The one place the bridge evaluates sexp text. Every expression it builds from caller input
-- (check, event_status, the sexp command) comes through here, so the eval opt-in is checked on the
-- final text, after any name has been spliced in, not on the pieces. mode: "eval" (boolean),
-- "number" or "run" (inside a when).
function H.eval_sexp(expr, mode)
	if type(expr) ~= "string" then H.raise("a sexp must be text", 'e.g. "( true )"') end
	if #expr >= H.SEXP_MAX then
		H.raise("sexp is " .. #expr .. " characters; the engine's limit is about 8190",
		        "split it into smaller expressions")
	end
	if H.runs_script(expr) then
		if not H.allow_eval then
			H.raise("this sexp runs Lua, which is off for this session", "relaunch with --allow-eval")
		end
		H.audit("sexp-script", expr)
	end
	if mode == "run" then return mn.runSEXP(expr) end
	if mode == "number" then return mn.evaluateNumericSEXP(expr) end
	return mn.evaluateSEXP(expr)
end

-- True when `handle` is valid and carries exactly this name (case aside). The engine's name
-- indexers fall back to atoi(name) as a 1-based index (mission.cpp ADE_INDEXER for Events, Wings,
-- ShipRegistry), so "2" would find the second event; comparing .Name rules that out.
local function named(handle, name)
	return handle ~= nil and handle:isValid() and string.lower(handle.Name) == string.lower(name)
end

-- Refuse unless the mission has an event with this name (QA finding SF-2: a misspelled event used
-- to read as "pending" until the caller's timeout).
function H.require_event(name)
	name = tostring(name)
	if not named(mn.Events[name], name) then
		H.raise("no event named " .. name, "list events with `fsoctl state`")
	end
	return name
end

-- Refuse unless a ship (present, destroyed, departed or not yet arrived: the ship registry keeps
-- them all, mission.cpp "all ships parsed, created, or exited") or a wing has this name.
function H.require_ship(name)
	name = tostring(name)
	if not (named(mn.ShipRegistry[name], name) or named(mn.Wings[name], name)) then
		H.raise("no ship named " .. name, "wings count too; list ships with `fsoctl state`")
	end
	return name
end

function H.event_status(name)
	local q = quoted(H.require_event(name))
	if H.eval_sexp("( is-event-true-delay " .. q .. " 0 )") then return "true" end
	if H.eval_sexp("( is-event-false-delay " .. q .. " 0 )") then return "false" end
	return "pending"
end

-- mission_goal.isGoalSatisfied: 0 failed, 1 complete, 2 incomplete (scripting.lua). Any other
-- value reads as "unknown", never as a guessed status (QA finding TA-12: the old `or
-- "incomplete"` fallback hid a broken map behind the most common answer).
H.GOAL = { [0] = "failed", [1] = "complete", [2] = "incomplete" }
function H.goal_text(g)
	return H.GOAL[g.isGoalSatisfied] or "unknown"
end

local function add_lists(out, args)
	local limit = args.limit or 200
	if args.ships ~= false then
		-- ships_total / ships_truncated tell a reader (compare, an agent) that the list was cut at
		-- `limit` rather than being every ship (QA finding SF-15 / SF-12).
		local total = #mn.Ships
		for i = 1, math.min(total, limit) do out.ships[#out.ships + 1] = ship_entry(mn.Ships[i]) end
		out.ships_total = total
		out.ships_truncated = total > limit
	end
	if args.goals ~= false then
		for i = 1, math.min(#mn.Goals, limit) do
			local g = mn.Goals[i]
			out.goals[#out.goals + 1] = { name = g.Name, status = H.goal_text(g) }
		end
	end
	if args.events ~= false then
		for i = 1, math.min(#mn.Events, limit) do
			local e = mn.Events[i]
			-- One event whose name cannot go into a sexp (see quoted) reports "unknown" instead of
			-- failing the whole report (QA finding CR-12).
			local ok, st = pcall(H.event_status, e.Name)
			out.events[#out.events + 1] = { name = e.Name, status = ok and st or "unknown" }
		end
	end
end

H.handlers.state = function(args)
	local out = { state = H.state_name(), mission = mn.getMissionFilename(),
	              mission_time = mn.getMissionTime(), paused = H.state_name() == "GS_STATE_GAME_PAUSED",
	              popup = H.popup or json.null, ships = {}, goals = {}, events = {}, player = json.null }
	if not mn.isInMission() then return out end
	if hv.Player and hv.Player:isValid() then
		out.player = ship_entry(hv.Player)
		out.player.collision = H.recent_collision() or json.null
		out.player.collisions_total = H.collisions_total
		out.player.touching = H.touching()
		out.player.collision_errors = H.collision_errors
	end
	add_lists(out, args)
	return out
end

-- True when a sexp may run Lua (script-eval*, multi-eval): held to the same opt-in as the lua
-- command (the Python side checks too, and audits). Any text containing "eval" counts: FSO treats
-- a token as an operator only on an exact name match (sexp.cpp get_operator_index) and no alias
-- maps to these, so any expression able to run one contains that text.
function H.runs_script(expr)
	return string.find(string.lower(expr), "eval", 1, true) ~= nil
end

H.handlers.sexp = function(args)
	local mode = args.mode or "eval"
	-- Eval gate first, so a Lua-running sexp is refused the same way in and out of a mission.
	if H.runs_script(tostring(args.expr or "")) and not H.allow_eval then
		H.raise("this sexp runs Lua, which is off for this session", "relaunch with --allow-eval")
	end
	-- Outside a mission the engine evaluates even ( true ) as false, so refuse rather than return
	-- a silently wrong answer.
	if not mn.isInMission() then
		H.raise("sexps need a running mission (state " .. H.state_name() .. ")",
		        "launch with --mission, or wait until the state is GS_STATE_GAME_PLAY")
	end
	local value = H.eval_sexp(args.expr, mode)
	local out = { value = value }
	-- The engine answers false (eval) or 0 / NaN (number) for an expression it could not parse,
	-- the same as a real false or 0 (probed 2026-10-12: an unbalanced expression and an unknown
	-- operator; run_sexp only logs a Warning). Say so whenever the answer could be that case (SF-2).
	if (mode ~= "number" and mode ~= "run" and value == false)
			or (mode == "number" and (value == 0 or value ~= value)) then
		out.note = "this answer is also what the engine returns when the expression did not parse;"
			.. " a debug build's fs2_open.log shows a sexp warning in that case"
	end
	return out
end

local function require_eval()
	if not H.allow_eval then
		H.raise("free-form Lua and raw events are off for this session", "relaunch with --allow-eval")
	end
end

H.handlers.lua = function(args)
	require_eval()
	local fn, err = loadstring(tostring(args.code))
	if not fn then H.raise_script("compile error: " .. tostring(err), "Lua 5.1 syntax") end
	H.audit("lua", args.code)
	-- The chunk sees the normal globals, except error() raises a catchable script error instead of
	-- the engine's fatal forwarder. Engine-raised errors (a missing field on a handle) stay fatal.
	setfenv(fn, setmetatable({ error = function(m) H.raise_script(m, "error() in your Lua") end },
	                         { __index = _G, __newindex = _G }))
	return { value = tostring(fn()) }
end

H.handlers.post_event = function(args)
	require_eval()
	local ev = ba.GameEvents[args.name]
	if not (ev and ev:isValid()) then
		H.raise("unknown game event " .. tostring(args.name), "names as in gamesequence.h")
	end
	H.audit("post_event", args.name)
	return { posted = ba.postGameEvent(ev) }
end

-- ---- flying, autopilot and time (Task 12) --------------------------------------------------------
-- Lua flight input only reaches the ship while the control mode is FLIGHT_CONTROL_LUA_FULL, and
-- weapons are fired through the ship's firePrimary/fireSecondary/fireCountermeasure, because
-- continuous fire buttons are read from the keyboard (probe results section 6).

local function clamp(v, lo, hi)
	v = tonumber(v) or 0
	if v < lo then return lo end
	if v > hi then return hi end
	return v
end

H.piloting = false

local function restore_control()
	ba.setControlMode(FLIGHT_CONTROL_NORMAL)
	H.piloting = false
end

-- Apply one frame of flight input; returns the number of shots the fire calls reported.
local function apply_controls(args)
	local ci = ba.getControlInfo()
	ci.Pitch = clamp(args.pitch, -1, 1)
	ci.Heading = clamp(args.heading, -1, 1)
	ci.Bank = clamp(args.bank, -1, 1)
	ci.Forward = clamp(args.forward, -1, 1)
	local shots = 0
	if args.primary then shots = shots + (hv.Player:firePrimary() or 0) end
	if args.secondary then shots = shots + (hv.Player:fireSecondary() or 0) end
	if args.cm then hv.Player:fireCountermeasure() end
	return shots
end

-- The flight in progress, or nil: { seq = <the pilot command's seq>, stop = function(reason) }.
-- stop ends it, restores control and answers the pilot command. pilot_cancel uses it.
H.flight = nil

-- Backstop for a flight whose mission clock stops while the game still shows the flight screen
-- (pausing itself ends a flight, see flight_stop_reason): end after this many frames per second
-- of the caller's own wait. game.pilot waits seconds*10+10 wall seconds; 120 is
-- the engine's default frame cap (freespace.cpp Framerate_cap), so with that cap in place the
-- bound is reached no sooner than the caller's own timeout, and later at lower frame rates (one
-- probe under Xvfb measured about 16 fps). An engine running uncapped could reach it sooner.
H.PILOT_FPS_BOUND = 120

-- Why a flight that has not yet stopped should stop now, or nil to keep flying. "paused" covers
-- the pause screen, any other state over the flight, and dialog frames (QA finding SL2-4): mission
-- time stops there, so the flight would otherwise hold the controls until a cancel arrived, and
-- the pilot's input would resume, out of date, after the pause.
-- `collisions_at_start` is nil when the caller asked to keep flying through contacts.
local function flight_stop_reason(stop_at, ticks, max_ticks, collisions_at_start)
	if not mn.isInMission() then return "mission_end" end
	-- Death first: a ram that kills the player is reported as player_dead, not as a contact to
	-- steer away from (QA CR-11).
	if not (hv.Player and hv.Player:isValid()) then return "player_dead" end
	-- A NEW contact during the flight hands control back at once, the way a pilot reacts, with
	-- the direction to what was hit in the reply. Contact that began before the flight (still
	-- grinding) does not count, so an escape flight can fly off (QA SLA-1). Before "time" so a
	-- contact on the last tick is still reported.
	if collisions_at_start and H.collisions_total > collisions_at_start then return "collision" end
	if mn.getMissionTime() >= stop_at then return "time" end
	if H.in_dialog or H.state_name() ~= "GS_STATE_GAME_PLAY" then return "paused" end
	if ticks > max_ticks then return "frame_limit" end
	return nil
end

H.handlers.pilot = function(args, seq)
	if not mn.isInMission() then
		H.raise("not in a mission (state " .. H.state_name() .. ")", "wait for GS_STATE_GAME_PLAY")
	end
	-- Paused: mission time stands still, so a flight would hold the controls until the caller
	-- gave up and then fly out-of-date input after the resume (QA finding SL-5). Under a popup the
	-- input would go to the ship beneath a modal dialog.
	if H.state_name() ~= "GS_STATE_GAME_PLAY" then
		H.raise("cannot fly while the game is paused or off the flight screen (state "
		        .. H.state_name() .. ")", "resume with `fsoctl time <id> --resume` first")
	end
	if H.in_dialog then
		H.raise("cannot fly while a dialog is up: " .. tostring(H.popup), "answer the popup first")
	end
	if H.piloting then H.raise("a pilot command is already running", "wait for it to finish") end
	if not (hv.Player and hv.Player:isValid()) then
		H.raise("no player ship to fly", "the player may be dead or not yet arrived")
	end
	local seconds = clamp(args.seconds or 1, 0.05, 60)
	local stop_at = mn.getMissionTime() + seconds
	local max_ticks = math.ceil((seconds * 10 + 10) * H.PILOT_FPS_BOUND)
	local frames, shots, ticks, over = 0, 0, 0, false
	local start_total = H.collisions_total
	local collisions_at_start = args.stop_on_collision ~= false and start_total or nil
	ba.setControlMode(FLIGHT_CONTROL_LUA_FULL)
	H.piloting = true
	local function stop(reason)
		over = true
		H.flight = nil
		restore_control()
		H.reply(seq, true, { applied_frames = frames, shots = shots,
		                     mission_time = mn.getMissionTime(),
		                     completed = reason == "time", stopped_because = reason,
		                     -- Contacts started during the flight and the latest one, also
		                     -- when it kept flying through them (QA CR-5).
		                     collisions = H.collisions_total - start_total,
		                     collision = H.collisions_total > start_total and H.last_new_collision
		                                 or json.null,
		                     touching = H.touching() })
	end
	H.flight = { seq = seq, stop = stop }
	H.timers[#H.timers + 1] = {
		tick = function()
			if over then return true end           -- already stopped by pilot_cancel
			ticks = ticks + 1
			local reason = flight_stop_reason(stop_at, ticks, max_ticks, collisions_at_start)
			if reason then
				stop(reason)
				return true
			end
			shots = shots + apply_controls(args)
			frames = frames + 1
			return false
		end,
		-- A Lua error in a tick (QA findings SL-5 / CR-10): give the controls back, release the
		-- latch and tell the caller, instead of leaving LUA_FULL on for the rest of the session.
		on_error = function(err)
			over = true
			H.flight = nil
			restore_control()
			local msg = H.decode_error(err)
			H.reply(seq, false, "pilot stopped by a Lua error: " .. msg,
			        "control was handed back; see bridge_info last_error", "script_error")
		end,
	}
	return H.DEFERRED
end

-- Stop the flight started by pilot command `args.seq` (contract C4, ruling R5): control goes back
-- to the player, the latch is released and the flight's own reply says stopped_because
-- "cancelled". fsoctl sends this when a pilot call times out after the bridge took it (QA SL-5).
-- WHY the seq must match: a late cancel for a flight that already ended would otherwise stop the
-- next flight, started by someone else (QA finding Y-4).
H.handlers.pilot_cancel = function(args)
	if not (H.flight and H.flight.seq == tonumber(args.seq)) then
		return { cancelled = false, reason = "no matching flight" }
	end
	H.flight.stop("cancelled")
	return { cancelled = true }
end

-- Current flight control mode as the engine reports it ("NORMAL", "FULL", ... or ""), and the seq
-- of the pilot command flying, if any (what pilot_cancel needs).
H.handlers.control_mode = function()
	return { mode = ba.setControlMode(), flight_seq = H.flight and H.flight.seq or json.null }
end

-- applied is runSEXP's own answer (QA findings SF-14 / TA-6: the reply used to echo the request).
H.handlers.autopilot = function(args)
	if not mn.isInMission() then H.raise("not in a mission", "wait for GS_STATE_GAME_PLAY") end
	local applied
	if args.on then applied = mn.runSEXP("( player-use-ai )")
	else applied = mn.runSEXP("( player-not-use-ai )") end
	return { on = args.on == true, applied = applied == true }
end

-- Frames between a time change and its read-back reply.
H.TIME_READBACK_FRAMES = 3

-- What the game shows now. There is no Lua getter for time compression, so it is measured: this
-- frame's mission frame time over its real frame time, in percent. While paused that ratio means
-- nothing, so compression is null then.
local function time_readback(requested)
	local st = H.state_name()
	local comp = json.null
	local real = ba.getRealFrametime()
	if st == "GS_STATE_GAME_PLAY" and real > 0 then
		comp = math.floor(ba.getMissionFrametime() / real * 100 + 0.5)
	end
	return { compression = comp, requested_compression = requested,
	         paused = st == "GS_STATE_GAME_PAUSED", state = st }
end

H.handlers.time = function(args, seq)
	local st = H.state_name()
	if st ~= "GS_STATE_GAME_PLAY" and st ~= "GS_STATE_GAME_PAUSED" then
		H.raise("not in a mission (state " .. st .. ")", "wait for GS_STATE_GAME_PLAY")
	end
	if args.compression then
		-- set-time-compression also locks the player's compression keys; reset-time-compression
		-- returns to 1x and unlocks (sexps.html), so 100% uses the reset.
		if math.floor(args.compression) == 100 then
			mn.runSEXP("( reset-time-compression )")
		else
			mn.runSEXP("( set-time-compression " .. math.floor(args.compression) .. " )")
		end
	end
	if args.pause then
		if st ~= "GS_STATE_GAME_PLAY" then H.raise("already paused", "use resume") end
		ba.postGameEvent(ba.GameEvents["GS_EVENT_PAUSE_GAME"])
	end
	if args.resume then
		if st ~= "GS_STATE_GAME_PAUSED" then H.raise("not paused (state " .. st .. ")", "use pause") end
		ba.postGameEvent(ba.GameEvents["GS_EVENT_PREVIOUS_STATE"])
	end
	-- Reply a few frames later with what the game actually shows (QA finding SF-14): a posted
	-- pause or resume only takes effect when the engine processes the event on a later frame.
	local requested, waited = args.compression or json.null, 0
	H.timers[#H.timers + 1] = {
		tick = function()
			waited = waited + 1
			if waited <= H.TIME_READBACK_FRAMES then return false end
			H.reply(seq, true, time_readback(requested))
			return true
		end,
		on_error = function(err)
			H.reply(seq, false, "time read-back failed: " .. (H.decode_error(err)),
			        "the change itself was sent; check `fsoctl state`", "script_error")
		end,
	}
	return H.DEFERRED
end

-- ---- conditions for wait_for (Task 13) -----------------------------------------------------------
-- One evaluation per call; fsoctl polls. Ship and event checks use this build's sexps:
-- is-event-true-delay / is-event-false-delay, is-destroyed-delay, has-departed-delay
-- (there is no is-departed-delay; probe results section 11).

local function goal_status(name)
	for i = 1, #mn.Goals do
		local g = mn.Goals[i]
		if g.Name == name then return H.goal_text(g) end
	end
	H.raise("no goal named " .. tostring(name), "list goals with `fsoctl state`")
end

local function check_in_mission(kind, v)
	if kind == "event_true" or kind == "event_false" then
		local st = H.event_status(v)
		return st == (kind == "event_true" and "true" or "false"), st
	end
	if kind == "destroyed" then
		return H.eval_sexp("( is-destroyed-delay 0 " .. quoted(H.require_ship(v)) .. " )"), v
	end
	if kind == "departed" then
		return H.eval_sexp("( has-departed-delay 0 " .. quoted(H.require_ship(v)) .. " )"), v
	end
	if kind == "goal" then
		local name, want = string.match(tostring(v), "^(.-)=(%a+)$")
		if not name then H.raise("goal condition must be NAME=status", "e.g. Protect Iota=complete") end
		local st = goal_status(name)
		return st == want, st
	end
	H.raise("unknown wait kind " .. tostring(kind),
	        "state, mission_time, event_true, event_false, destroyed, departed, goal")
end

H.handlers.check = function(args)
	local kind, v = args.kind, args.value
	if kind == "state" then return { met = H.state_name() == v, detail = H.state_name() } end
	if kind == "mission_time" then
		return { met = mn.getMissionTime() >= (tonumber(v) or 0), detail = mn.getMissionTime() }
	end
	if kind == "collision" then   -- met once the engine-run count passes the caller's baseline
		return { met = H.collisions_total > (tonumber(v) or 0),
		         detail = { total = H.collisions_total, last = H.last_new_collision or json.null,
		                    touching = H.touching(), errors = H.collision_errors,
		                    last_error = H.collision_last_error or json.null } }
	end
	if not mn.isInMission() then return { met = false, detail = "not in mission: " .. H.state_name() } end
	local met, detail = check_in_mission(kind, v)
	return { met = met == true, detail = detail }
end

-- Warnings and errors the engine counted while loading the mod (validate, Task 18). Release builds
-- compile Warning() out, so there these counts stay 0.
H.handlers.counts = function()
	return { warnings = ui.PilotSelect.WarningCount, errors = ui.PilotSelect.ErrorCount }
end

-- Engine clocks for frame_stats' measuring window (contract C4, QA finding SF-10).
-- ba.getFrametimeOverall is documented as fix units, but the engine converts on the way out (it
-- pushes with the "x" format, which is f2fl: ade_args.h set_single_arg), so it is already seconds.
-- It is the running sum of the engine's per-frame Frametime (freespace.cpp game_set_frametime),
-- which is not wall time since start (one probe read 3.2 s a few seconds into a mission), so only
-- differences between two readings mean anything.
H.handlers.clock = function()
	local mt = json.null
	if mn.isInMission() then mt = mn.getMissionTime() end
	return { frametime_overall = ba.getFrametimeOverall(), mission_time = mt,
	         gameplay_frames = H.gameplay_frames }
end

-- ---- collision alerts and telemetry (2026-10-12, Dustin: "You should get an alert on collision
-- along with which direction the nearest object is so you can turn and fly away"; "make it
-- switchable, so the agent only turns it on for runs where it's necessary") ---------------------
-- Vector maths here reads .x/.y/.z only (no engine vector operators), so it means the same on an
-- engine vector and on the unit tier's plain tables.

-- Small vector helpers for plain {x, y, z} tables and engine vectors alike.
-- sub3(a, b): a - b. dot3(a, b): dot product. len3(a): length. r2(v): v rounded to 2 decimals.
local function sub3(a, b) return { x = a.x - b.x, y = a.y - b.y, z = a.z - b.z } end
local function dot3(a, b) return a.x * b.x + a.y * b.y + a.z * b.z end
local function len3(a) return math.sqrt(dot3(a, a)) end
local function r2(v) return math.floor(v * 100 + 0.5) / 100 end

-- World vector `w` in the player ship's own axes, as {right, up, forward}. With `unit` true the
-- result is normalised, and a zero vector gives nil: it has no direction, and {0,0,0} would read
-- as one (QA SF-5a).
local function ship_local(ship, w, unit)
	local o = ship.Orientation
	local x, y, z = dot3(w, o:getRvec()), dot3(w, o:getUvec()), dot3(w, o:getFvec())
	local n = 1
	if unit then
		n = math.sqrt(x * x + y * y + z * z)
		if n == 0 then return nil end
	end
	return { right = r2(x / n), up = r2(y / n), forward = r2(z / n) }
end

-- Run epoch: goes up each time a new run of a mission starts (another mission file, or the same
-- one restarted, which makes its clock jump back). Contacts and telemetry rows remember the epoch
-- they were recorded in, so after a restart `state` never shows the previous run's contact and
-- telemetry never mixes two timelines (QA SLA-3, SF-4). One detector serves both, so they cannot
-- disagree (QA Phase B). Called from the collision hook, every gameplay frame and each telemetry
-- command; repeat calls within one frame change nothing.
H.run_epoch = 0
H.run_mark = { mission = nil, t = -1 }
function H.track_run()
	local m, t = mn.getMissionFilename(), mn.getMissionTime()
	local R = H.run_mark
	if m ~= R.mission or t < R.t - 0.001 then
		if R.mission ~= nil then H.run_epoch = H.run_epoch + 1 end
		H.contact_cur, H.contact_prev, H.contact_frame = {}, {}, nil
	end
	R.mission, R.t = m, t
end

-- Contacts the player STARTED, counted over the whole engine run. WHY starts and not contact
-- frames: the engine re-tests an overlapping pair every frame with no cooldown
-- (collideshipship.cpp:1525/1541), so a ship grinding along a hull fires the hook every frame.
-- Counting every frame made `pilot` stop on the first frame of an escape that began while still
-- touching, trapping the agent on the hull (QA SLA-1 / CR-1). A contact counts when its object
-- touched nothing of ours in the previous simulation frame, or is a different object.
-- WHY never reset: a wait_for compares against the count it saw when it started, so a reset
-- would make that baseline stale (Architect audit).
H.collisions_total = 0
H.collision_errors = 0          -- collision-hook errors, shown in `state` and `check` (QA SF-3)
H.collision_last_error = nil
H.last_collision = nil          -- the latest contact frame's record, new contact or ongoing
H.last_new_collision = nil      -- the latest NEW contact (what pilot and check report)
H.COLLISION_SHOW_SEC = 5        -- `state` shows the last contact for this many mission seconds
-- Contact sets keyed by object: this simulation frame's (contact_cur, for H.gameplay_frames
-- value contact_frame) and the previous simulation frame's (contact_prev). WHY gameplay_frames
-- identifies a simulation frame: it goes up on each gameplay On Frame, which the engine runs
-- after each simulation (2d.cpp:3283-3289), and the hook runs inside the simulation (QA P3).
-- Not H.frames: that also counts paused and dialog frames, which have no simulation, so a pause
-- while touching would have turned the same contact into a new one (cross-family XFA-4).
H.contact_cur, H.contact_prev, H.contact_frame = {}, {}, nil

-- True while the player is in contact: the latest simulation frame had a contact. Read after a
-- gameplay frame, the last simulation stored gameplay_frames - 1 (the counter goes up after it).
function H.touching()
	return H.contact_frame ~= nil and H.contact_frame >= H.gameplay_frames - 1
end

-- Object hit by the player in this hook call: (valid ship or nil, kind, key for the contact sets).
-- Keys use getSignature, which ships, debris and asteroids all have (each handle type derives
-- from object: debris.cpp:16, asteroid.cpp:14, object.cpp:344), so two asteroids touched one
-- after the other are two contacts (cross-family XFA-1). Props are not here: their collision path
-- only asks this hook whether it overrides and never runs it (collideshipship.cpp:1664-1690,
-- XFA-2), and an override would change the collision itself.
local function contact_object(other)
	if other and other:isValid() then return other, "ship", "ship " .. tostring(other:getSignature()) end
	-- "unknown" is usually a ship destroyed in this very frame (handle no longer valid); two such
	-- in one frame share this key and count once, which is rare and bounded (XFK-8).
	local kind = (hv.Debris and "debris") or (hv.Asteroid and "asteroid") or "unknown"
	local obj = hv.Debris or hv.Asteroid
	if obj and obj:isValid() then return nil, kind, kind .. " " .. tostring(obj:getSignature()) end
	return nil, kind, kind
end

-- "On Ship Collision" handler (fsoharness-sct.tbm). Probe 2026-10-12 (session sd270dbee): the hook
-- also fires for every weapon hit on any ship (hv.Weapon set) and twice per ship-ship contact
-- (Ship and ShipB swapped), so only contacts that include the player and no weapon or beam count.
-- hv[...] of a variable this hook did not set is nil (hookvars.cpp indexer), so each read is
-- safe; hv.Ship and hv.ShipB are ship-typed, so their .Name exists.
local function on_ship_collision()
	if hv.Weapon ~= nil or hv.Beam ~= nil then return end
	local me = hv.Player
	if not (me and me:isValid()) then return end
	local sig, a, b = me:getSignature(), hv.Ship, hv.ShipB
	local other
	if a and a:isValid() and a:getSignature() == sig then other = b
	elseif b and b:isValid() and b:getSignature() == sig then other = a
	else return end
	H.track_run()
	local f = H.gameplay_frames
	if H.contact_frame ~= f then
		H.contact_prev = (H.contact_frame == f - 1) and H.contact_cur or {}
		H.contact_cur, H.contact_frame = {}, f
	end
	local ship, kind, key = contact_object(other)
	if H.contact_cur[key] then return end          -- the swapped second call for this contact
	-- Every engine call site passes Hitpos (collideshipship.cpp:1421/1433, collidedebrisship.cpp
	-- :193/380); without one the direction is null rather than invented (QA CR-7, SF-5a).
	local point = hv.Hitpos
	local c = {
		frame = f, epoch = H.run_epoch, mission = mn.getMissionFilename(),
		mission_time = r2(mn.getMissionTime()), other = ship and ship.Name or json.null,
		other_kind = kind,
		direction = (point and ship_local(me, sub3(point, me.Position), true)) or json.null,
		hull_pct = r2(me.HitpointsLeft * 100 / me.HitpointsMax),
		speed = r2(len3(me.Physics.Velocity)),
	}
	-- Everything is updated only after the record built, so an error building it changes no
	-- count and no dedupe set (QA SF-1); the swapped second call then tries again.
	H.contact_cur[key] = true
	c.new = not H.contact_prev[key]
	if c.new then H.collisions_total = H.collisions_total + 1 end
	c.count = H.collisions_total
	H.last_collision = c
	-- pilot and check report the contact that raised the count, not one still grinding beside it
	-- (cross-family XFK-3: grinding A while B hits would have named A).
	if c.new then H.last_new_collision = c end
end

-- Hook entry point: an error is counted and kept for `state` and `check`, never raised.
function H.ship_collision()
	local ok, err = pcall(on_ship_collision)
	if not ok then
		H.collision_errors = H.collision_errors + 1
		H.collision_last_error = tostring(err)
		H.note_error(err)
	end
end

-- The last contact if it belongs to this run of this mission and is at most COLLISION_SHOW_SEC
-- mission seconds old, else nil. Used by `state`.
function H.recent_collision()
	local c = H.last_collision
	if not c or c.epoch ~= H.run_epoch or c.mission ~= mn.getMissionFilename() then return nil end
	-- mission_time is stored rounded to 0.01 s, so a contact from this very frame can read up to
	-- 0.005 s in the future (cross-family XFA-6).
	local age = mn.getMissionTime() - c.mission_time
	if age < -0.01 or age > H.COLLISION_SHOW_SEC then return nil end
	return c
end

-- Telemetry: while on, one row per gameplay frame into a ring of TELEM_CAP rows (125 s at the
-- engine's default 120 fps cap, freespace.cpp:370). Off by default; off costs one function call
-- and one table read per frame.
H.TELEM_CAP = 15000
H.TELEM_COLUMNS = { "t", "fwd_x", "fwd_y", "fwd_z", "rate_pitch", "rate_heading", "rate_bank",
                    "speed", "aim_err_deg", "target_dist", "hull_pct" }
H.telem = { on = false, buf = {}, head = 0, n = 0, epoch = 0, cleared = 0, error = nil }

-- Empty the ring and tie it to the current run.
local function telem_clear()
	local T = H.telem
	T.buf, T.head, T.n, T.epoch = {}, 0, 0, H.run_epoch
end

-- The i-th oldest row (1-based) of the ring.
local function telem_row(i)
	local T = H.telem
	return T.buf[((T.head - T.n + i - 1) % H.TELEM_CAP) + 1]
end

-- Aim error (degrees between the nose `f` and the target's centre) and target distance, or
-- -1, -1 without a target; 0, 0 if the target sits exactly at our position.
local function aim_of(me, f)
	local t = me.Target
	if not (t and t:isValid()) then return -1, -1 end
	local off = sub3(t.Position, me.Position)
	local d = len3(off)
	if d <= 0 then return 0, 0 end
	return math.deg(math.acos(math.max(-1, math.min(1, dot3(f, off) / d)))), d
end

-- Turn rate v as a share of its maximum m (0 when the ship cannot turn on that axis).
local function rate(v, m) return m > 0 and v / m or 0 end

-- Rows belong to one mission run: when a new run has started, empty the ring and say so in the
-- event log (QA SLA-4). Called before recording AND before every telemetry reply, so rows from an
-- earlier run are never returned beside the new run's clock, even while recording is off
-- (cross-family XFA-5).
local function telem_sync()
	local T = H.telem
	if T.epoch ~= H.run_epoch then
		if T.n > 0 then
			T.cleared = T.cleared + 1
			H.event("telemetry cleared: a new mission run started")
		end
		telem_clear()
	end
end

-- Append one row for the player ship (nothing while there is no player).
local function telem_record()
	local me = hv.Player
	if not (me and me:isValid()) then return end
	telem_sync()
	local T, t = H.telem, mn.getMissionTime()
	local f = me.Orientation:getFvec()
	local rv, mx = me.Physics.RotationalVelocity, me.Physics.RotationalVelocityMax
	local aim, dist = aim_of(me, f)
	-- The whole row first, then cursor and count together: an error building it must leave the
	-- ring as it was (cross-family XFA-3).
	local row = { t, f.x, f.y, f.z, rate(rv.x, mx.x), rate(rv.y, mx.y), rate(rv.z, mx.z),
	              len3(me.Physics.Velocity), aim, dist, me.HitpointsLeft * 100 / me.HitpointsMax,
	              H.collisions_total }
	T.head = T.head % H.TELEM_CAP + 1
	T.buf[T.head] = row
	if T.n < H.TELEM_CAP then T.n = T.n + 1 end
end

-- Called from the frame body on normal gameplay frames. Its own pcall: an error here must not
-- stop the commands and timers that run after it in the frame, among them the `telemetry off`
-- that would end it (QA SF-2 / TA-5 / SLA-2). On an error telemetry switches itself off and
-- keeps the message for `telemetry read` / `off`.
function H.telemetry_frame()
	if not H.telem.on then return end
	local ok, err = pcall(telem_record)
	if not ok then
		H.telem.on = false
		H.telem.error = tostring(err)
		H.note_error(err)
		H.event("telemetry switched off by an error: " .. H.telem.error)
	end
end

-- Summary of rows[a..b]: aim error, share of frames within 3 degrees, wiggle (sign flips of a
-- turn rate above 5% of max, per second), peak turn rate, mean speed, contacts started in the
-- window. fps and wiggle need two rows spanning time; otherwise they are null (QA CR-3). Every
-- "per second" here is per MISSION second: under time compression the renderer's real frame
-- rate is fps x compression / 100 (cross-family XFA-7; frame_stats measures real frame times).
local function telem_summary(a, b)
	local first, last = telem_row(a), telem_row(b)
	local secs = last[1] - first[1]
	local aim_n, aim_sum, aim_min, aim_max, on3 = 0, 0, math.huge, -1, 0
	local flips, prev, peak, speed = { 0, 0, 0 }, { 0, 0, 0 }, 0, 0
	for i = a, b do
		local r = telem_row(i)
		if r[9] >= 0 then
			aim_n, aim_sum = aim_n + 1, aim_sum + r[9]
			aim_min, aim_max = math.min(aim_min, r[9]), math.max(aim_max, r[9])
			if r[9] <= 3 then on3 = on3 + 1 end
		end
		for k = 1, 3 do
			local w = r[4 + k]
			peak = math.max(peak, math.abs(w))
			if math.abs(w) > 0.05 then
				if prev[k] ~= 0 and (w > 0) ~= (prev[k] > 0) then flips[k] = flips[k] + 1 end
				prev[k] = w
			end
		end
		speed = speed + r[8]
	end
	local frames = b - a + 1
	local timed = frames >= 2 and secs >= 0.001
	return {
		frames = frames, seconds = r2(secs),
		fps = timed and r2((frames - 1) / secs) or json.null,
		aim_err_deg = aim_n > 0 and { mean = r2(aim_sum / aim_n), min = r2(aim_min),
		                              max = r2(aim_max), within_3deg_pct = r2(100 * on3 / aim_n) }
		              or json.null,
		wiggle_per_sec = timed and { pitch = r2(flips[1] / secs), heading = r2(flips[2] / secs),
		                             bank = r2(flips[3] / secs) } or json.null,
		peak_rate = r2(peak), mean_speed = r2(speed / frames),
		collisions = last[12] - first[12],
	}
end

-- State fields every telemetry reply carries: whether it is on, rows held, the mission clock
-- now and at the newest row (a gap means recording stopped, e.g. no player ship), how many
-- times a new run emptied it, and the error that switched it off (QA SF-4, SLA-4).
local function telem_status()
	local T = H.telem
	return { on = T.on, rows = T.n, now = r2(mn.getMissionTime()),
	         last_row_t = T.n > 0 and r2(telem_row(T.n)[1]) or json.null,
	         cleared = T.cleared, error = T.error or json.null }
end

-- Status plus rows of the last `seconds`, one per 1/hz seconds, as compact arrays under one
-- column list (aim and distance read -1 without a target).
local function telem_read(seconds, hz)
	local T, out = H.telem, telem_status()
	if T.n == 0 then return out end
	local from = telem_row(T.n)[1] - seconds
	local a = T.n
	while a > 1 and telem_row(a - 1)[1] >= from do a = a - 1 end
	local samples, next_t = {}, -math.huge
	for i = a, T.n do
		local r = telem_row(i)
		if r[1] >= next_t then
			local s = {}
			for k = 1, #H.TELEM_COLUMNS do s[k] = r2(r[k]) end
			samples[#samples + 1] = s
			next_t = r[1] + 1 / hz
		end
	end
	out.summary, out.columns, out.samples = telem_summary(a, T.n), H.TELEM_COLUMNS, samples
	return out
end

-- `telemetry` command: on (empties the ring, clears an old error), off, clear, read.
H.handlers.telemetry = function(args)
	if mn.isInMission() then H.track_run() end   -- notice a restart even with recording off
	telem_sync()
	local action = args.action
	if action == "on" then
		telem_clear()
		H.telem.on, H.telem.error = true, nil
		return telem_status()
	elseif action == "off" then
		H.telem.on = false
		return telem_status()
	elseif action == "clear" then
		telem_clear()
		return telem_status()
	elseif action == "read" then
		return telem_read(clamp(args.seconds or 10, 0.1, 120), clamp(args.hz or 2, 0.1, 10))
	end
	H.raise("unknown telemetry action " .. tostring(action), "on, off, read or clear")
end

-- ---- startup stage switches (spec section 5); each one is off unless the harness set it -------

-- Each hook below records its switch's outcome in H.switch_status (heartbeat "switches"); a hook
-- that raises is retried and finally marked failed by run_pending_hooks.

H.on_state("GS_STATE_INITIAL_PLAYER_SELECT", function()
	if H.switches.pilot_mode ~= "create" then return end
	local name = H.switches.pilot_name or "HarnessPilot"
	-- Multiplayer roles need a multiplayer pilot: the main hall refuses the multiplayer door
	-- to a single-player one (probe-results 21.1: createPilot(name, true) works).
	local multi = H.switches.pilot_multi == true
	local created = ui.PilotSelect.createPilot(name, multi)
	H.event("createPilot " .. name .. " multi=" .. tostring(multi) .. " -> " .. tostring(created))
	if created ~= true then
		H.set_switch("pilot_mode", "failed: createPilot(" .. name .. ") returned " .. tostring(created))
		return
	end
	ui.PilotSelect.selectPilot(name, multi)
	H.set_switch("pilot_mode", "done")
end, "pilot_mode")

-- Clients: the main hall's multiplayer button posts GS_EVENT_MULTI_JOIN_GAME (mainhallmenu.cpp:
-- 387); nothing on the command line does, and -connect only acts once the join screen is up.
-- Posting the event is enough (probe-results 21.1).
H.on_state("GS_STATE_MAIN_MENU", function()
	if H.switches.multi_role ~= "client" then return end
	-- Once (QA CR-5): hooks run on every entry to a state, and the main hall is where a
	-- cancelled join returns, so a cancel (answer_popup) is final rather than retried forever.
	if H.switch_status.multi_join ~= "pending" then return end
	local ok = ba.postGameEvent(ba.GameEvents["GS_EVENT_MULTI_JOIN_GAME"])
	H.event("multi join -> " .. tostring(ok))
	H.set_switch("multi_join", ok and "done" or "failed: postGameEvent returned false")
end, "multi_join")

-- The first player to join a standalone becomes the host and lands on the "start game" screen
-- (game name, password, rank limits), which waits for Accept; Accept only posts
-- GS_EVENT_MULTI_HOST_SETUP (multiui.cpp:2662). Without this the client sat there for the whole
-- run (probe-results 21.2). Not a switch of its own: it is part of joining.
-- After the briefing, multiplayer goes back to the sync screen, where the HOST presses Launch;
-- Launch is multi_sync_start_countdown() (multiui.cpp:7887), exposed as ui.MultiSync.
-- startCountdown. The sync screen also appears BEFORE the briefing (mission load sync), where a
-- launch would be wrong, so it fires only once the briefing has been seen (engine test:
-- the client sat in GS_STATE_MULTI_MISSION_SYNC for 300 s before this hook existed).
-- Only the session the harness marked multi_launch (the one given --mission, the intended host)
-- presses it: a non-host client's call reaches the server too, and on a debug build trips a
-- live Assert (QA SL-3, XFA-2). The latch is cleared after it fires and whenever a mission's
-- flow restarts (host/client setup, debrief), so a second mission's pre-briefing sync does not
-- launch (QA SL-2, CR-4, TA-9).
H.multi_briefed = false
H.on_state("GS_STATE_BRIEFING", function()
	if H.switches.multi_launch == true then H.multi_briefed = true end
end)
for _, reset_state in ipairs({ "GS_STATE_MULTI_HOST_SETUP", "GS_STATE_MULTI_CLIENT_SETUP",
                               "GS_STATE_DEBRIEF" }) do
	H.on_state(reset_state, function() H.multi_briefed = false end)
end

H.on_state("GS_STATE_MULTI_MISSION_SYNC", function()
	if H.switches.multi_launch ~= true or not H.multi_briefed then return end
	H.multi_briefed = false
	if not (ui.MultiSync and ui.MultiSync.startCountdown) then
		H.event("multi launch: ui.MultiSync.startCountdown missing in this build")
		return
	end
	ui.MultiSync.startCountdown()
	H.event("multi launch countdown started")
end)

H.on_state("GS_STATE_MULTI_START_GAME", function()
	if H.switches.multi_role ~= "client" then return end
	local ok = ba.postGameEvent(ba.GameEvents["GS_EVENT_MULTI_HOST_SETUP"])
	H.event("multi start-game accept -> " .. tostring(ok))
end, "multi_join")

H.on_state("GS_STATE_CMD_BRIEF", function()
	if not H.switches.skip_command_briefing then return end
	local ok = ba.postGameEvent(ba.GameEvents["GS_EVENT_START_BRIEFING"])
	H.event("skip command briefing -> " .. tostring(ok))
	H.set_switch("skip_command_briefing", ok and "done" or "failed: postGameEvent returned false")
end, "skip_command_briefing")

-- WHY a few frames before committing: the probe committed on the fifth briefing frame and that
-- worked; committing on the very first frame would act before the briefing screen has finished
-- setting up the loadout.
H.BRIEFING_COMMIT_FRAME = 5
H.on_state("GS_STATE_BRIEFING", function()
	if not H.switches.skip_briefing then return end
	local seen = 0
	H.timers[#H.timers + 1] = {
		tick = function()
			if H.state_name() ~= "GS_STATE_BRIEFING" then return true end
			-- Count only frames the briefing screen itself ran: on a dialog frame a popup is up over
			-- it, and committing then would act underneath the popup (QA finding CR-11).
			if H.in_dialog then return false end
			seen = seen + 1
			if seen < H.BRIEFING_COMMIT_FRAME then return false end
			local r = ui.Briefing.commitToMission()
			H.event("skip briefing: commitToMission -> " .. tostring(r))
			if r == COMMIT_SUCCESS then H.set_switch("skip_briefing", "done")
			else H.set_switch("skip_briefing", "failed: commitToMission -> " .. tostring(r)) end
			return true
		end,
		on_error = function(err)
			H.set_switch("skip_briefing", "failed: " .. (H.decode_error(err)))
		end,
	}
end, "skip_briefing")

-- Red-alert missions show the red alert screen instead of the briefings; its Continue button posts
-- GS_EVENT_ENTER_GAME (redalert.cpp red_alert_button_pressed). Not exercised by the test mission.
H.on_state("GS_STATE_RED_ALERT", function()
	if not H.switches.skip_briefing then return end
	local ok = ba.postGameEvent(ba.GameEvents["GS_EVENT_ENTER_GAME"])
	H.event("skip red alert screen -> " .. tostring(ok))
	H.set_switch("skip_briefing", ok and "done" or "failed: postGameEvent returned false")
end, "skip_briefing")

return H
