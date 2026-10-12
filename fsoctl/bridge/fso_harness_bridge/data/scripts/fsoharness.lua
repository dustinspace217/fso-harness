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
		switches = H.switch_status,
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
	if choice == nil and H.switches.dismiss_popups then choice = 0 end
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
	if hv.Player and hv.Player:isValid() then out.player = ship_entry(hv.Player) end
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
local function flight_stop_reason(stop_at, ticks, max_ticks)
	if not mn.isInMission() then return "mission_end" end
	if mn.getMissionTime() >= stop_at then return "time" end
	if H.in_dialog or H.state_name() ~= "GS_STATE_GAME_PLAY" then return "paused" end
	if not (hv.Player and hv.Player:isValid()) then return "player_dead" end
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
	ba.setControlMode(FLIGHT_CONTROL_LUA_FULL)
	H.piloting = true
	local function stop(reason)
		over = true
		H.flight = nil
		restore_control()
		H.reply(seq, true, { applied_frames = frames, shots = shots,
		                     mission_time = mn.getMissionTime(),
		                     completed = reason == "time", stopped_because = reason })
	end
	H.flight = { seq = seq, stop = stop }
	H.timers[#H.timers + 1] = {
		tick = function()
			if over then return true end           -- already stopped by pilot_cancel
			ticks = ticks + 1
			local reason = flight_stop_reason(stop_at, ticks, max_ticks)
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

-- ---- startup stage switches (spec section 5); each one is off unless the harness set it -------

-- Each hook below records its switch's outcome in H.switch_status (heartbeat "switches"); a hook
-- that raises is retried and finally marked failed by run_pending_hooks.

H.on_state("GS_STATE_INITIAL_PLAYER_SELECT", function()
	if H.switches.pilot_mode ~= "create" then return end
	local name = H.switches.pilot_name or "HarnessPilot"
	local created = ui.PilotSelect.createPilot(name, false)
	H.event("createPilot " .. name .. " -> " .. tostring(created))
	if created ~= true then
		H.set_switch("pilot_mode", "failed: createPilot(" .. name .. ") returned " .. tostring(created))
		return
	end
	ui.PilotSelect.selectPilot(name, false)
	H.set_switch("pilot_mode", "done")
end, "pilot_mode")

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
