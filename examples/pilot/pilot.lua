-- pilot.lua: a scripted fighter pilot for the player ship (v2, 2026-10-15).
--
-- Loaded once through the harness's `lua` tool (loadfile + pcall). It installs an "On Frame"
-- hook that flies the player ship every frame, then stays resident until A1.enabled = false.
-- Escort duty is optional. Before loading, set fields on A1.cfg (do not replace A1 itself: it
-- holds the flags that stop a reload from adding a second hook):
--   A1 = A1 or {}; A1.cfg = { guard = { "Iota 1", "Iota 2" }, guard_away = "Psamtik" }
-- (the SM1-01 setup). With no guard list it fights the nearest hostile fighter.
--
-- v2 over v1 (the 2026-10-12 SM1-01 demo pilot); README.md lists what was measured and how often:
--   * lead from the target's ABSOLUTE velocity (lasers do not inherit ours; weapons.cpp:7401);
--   * lead from the gun mount, nose aimed at (intercept - mount): FS2 guns do not converge;
--   * acceleration in the lead (a turning fighter drifts ~10 m off a straight-line lead);
--   * a tracking law built on the ship's turn-rate lag ($Rotdamp) with line-of-sight feedforward;
--   * throttle for turn radius (rate is speed-independent) and a tail-chase speed match;
--   * heat seekers fired without a lock, by target agility, one in flight per target;
--   * countermeasures only inside 300 m (from engine source, not measured).
--
-- THE RULE THIS SCRIPT KEEPS: it may only do what a human player can do.
--   Inputs  = what a pilot can see: ship positions/velocities (radar + HUD target box), own hull,
--             shields, ammo, incoming missiles (the HUD missile warning).
--   Outputs = player controls only:
--     * stick / throttle  -> ba.getControlInfo() axes under FLIGHT_CONTROL_LUA_FULL
--     * trigger           -> ship:firePrimary() / ship:fireSecondary(); these call the same
--                            ship_fire_primary/secondary the trigger calls, so refire timers,
--                            weapon energy and ammo all still apply (ship.cpp / ship API).
--     * target selection  -> ship.Target (a human does this with target-in-reticle / F-keys /
--                            "target closest attacking"; the script just picks which one)
--     * shields, countermeasures -> control_info.useButtonControl(-1, "<bound control>"),
--                            which sets the same button bits a key press sets (keycontrol.cpp).
--                            DOT, not colon: the binding parses "i|s" with no self handle
--                            (control_info.cpp:354), so `ci:` passes the handle as arg 1 and the
--                            engine aborts (first test flight, 2026-10-12). The docs say method.
--   Never: sexps, ship:kill(), setting hull/shields/positions, AI orders on the player ship.
--
-- WHY a frame hook and not the harness `pilot` tool: `pilot` holds fixed stick values for N
-- seconds; dogfighting needs a new aim every frame (the target moves ~70 m/s).
--
-- Lua note for readers new to it: `local` limits a variable to its block; tables `{}` are the
-- only data structure (arrays and records); `a and b or c` is Lua's ternary idiom.

A1 = A1 or {}                    -- global so later `lua` calls can read status / switch it off
A1.enabled = true
A1.log = A1.log or {}            -- ring buffer of notable moments, capped below
A1.stats = A1.stats or { primary_shots = 0, secondary_shots = 0, cms = 0, retargets = 0 }
-- Flight-quality counters (added for the flight-fix test): frames spent tracking a fighter within
-- 800 m, how many of those had the nose within 3 degrees of the aim point, avoids and extends.
A1.fq = A1.fq or { track = 0, on = 0, avoids = 0, extends = 0, frames = 0, t0 = mn.getMissionTime() }

local MAX_LOG = 300              -- Power-of-Ten rule 3: bounded accumulator
-- Ships to protect, by name. Set before loading the file (see the header), e.g. for SM1-01:
--   A1 = A1 or {}; A1.cfg = { guard = { "Iota 1", "Iota 2" }, guard_away = "Psamtik" }
-- Empty (the default) means: no escort duty, fight the nearest hostile fighter.
A1.cfg = A1.cfg or {}
local GUARD = A1.cfg.guard or {}

-- Append one line to A1.log with the mission time, dropping the oldest past MAX_LOG.
local function note(msg)
	local line = string.format("%7.1f %s", mn.getMissionTime(), msg)
	A1.log[#A1.log + 1] = line
	if #A1.log > MAX_LOG then table.remove(A1.log, 1) end
end
A1.note = note

-- Clamp x into [lo, hi].
local function clamp(x, lo, hi)
	if x < lo then return lo elseif x > hi then return hi end
	return x
end

-- Returns a ship handle by name if it exists, is alive and is not exploding; else nil.
local function live_ship(name)
	local s = mn.Ships[name]
	if s and s:isValid() and not s:isDying() then return s end
	return nil
end

-- Is this ship a fighter or bomber? (The Belisarius corvette is never a dogfight target: it is
-- surrendering, and the mission's own script has the Psamtik deal with it.)
local function is_small(s)
	local t = s.Class.Type
	if not t:isValid() then return false end
	return t.Name == "Fighter" or t.Name == "Bomber"
end

-- Lead solution: the point to aim at so a bolt of speed `spd` meets a target at position rp
-- (relative to the GUN, not the ship centre) moving with velocity rv. Solves |rp + rv*t| = spd*t
-- for the smallest positive t; falls back to plain distance/speed.
-- v2 (2026-10-15): rv is the target's ABSOLUTE velocity. Retail lasers fly at their table speed
-- along the barrel and do NOT inherit the shooter's velocity (weapons.cpp:7401-7414; ai_profiles
-- "$use additive weapon velocity" defaults to NO). v1 passed (target vel - own vel), which put the
-- aim point own-speed x flight-time off: about 30 m at 500 m, more than a fighter's width.
local function lead_point(tpos, rp, rv, spd)
	local a = rv:getDotProduct(rv) - spd * spd
	local b = 2 * rp:getDotProduct(rv)
	local c = rp:getDotProduct(rp)
	local t
	if math.abs(a) < 1e-3 then
		t = (b ~= 0) and (-c / b) or -1
	else
		local disc = b * b - 4 * a * c
		if disc >= 0 then
			local r = math.sqrt(disc)
			local t1, t2 = (-b - r) / (2 * a), (-b + r) / (2 * a)
			t = (t1 > 0 and t2 > 0) and math.min(t1, t2) or math.max(t1, t2)
		else
			t = -1
		end
	end
	if not t or t <= 0 or t > 5 then t = rp:getMagnitude() / spd end
	return tpos + rv * t, t
end

-- Roll command chosen by the most recent steer_to call this frame; fly() writes it to ci.Bank.
-- WHY an upvalue and not a 4th return value: five call sites already take (heading, pitch[, ang]);
-- the last steer_to of a frame is always the one whose heading/pitch are used, so its roll goes
-- with them.
local last_bank = 0

-- Damping gain on turn rate, as a fraction of the ship's max turn rate. The engine sets desired
-- turn rate = stick x max turn rate per axis, in the ship's own frame (physics.cpp:497-539), and
-- the ship takes time to reach it. Pure proportional steering therefore overshot and wiggled
-- (playtests 2026-10-12); subtracting the current rate stops it.
-- Tunable per run through A1.cfg (set before loading the file); defaults are the first guess.
A1.cfg = A1.cfg or {}
A1.cfg.KD = A1.cfg.KD or 0.3            -- tuned 2026-10-12: KD 0 lost on every metric; 0.3 ~ 0.6
A1.cfg.wide_deg = A1.cfg.wide_deg or 45   -- bank to turn beyond this; 25 scored 27%, 45 scored 29-40%
A1.cfg.gain = A1.cfg.gain or 3.0          -- aim gain (stick per radian) in the narrow regime
-- Roll direction. +bank turns the ship's right axis toward its up axis (the same axis cycle that
-- gives +heading = nose right and +pitch = nose down, both flown and confirmed), which INCREASES
-- the target's on-screen angle phi = atan2(x, y). To roll the target toward "up", command -phi.
-- Derived, then flown and confirmed 2026-10-12 (phi held at 0.2-0.25 degrees in a hard turn).
local BANK_SIGN = -1

-- ---------------------------------------------------------------- v2: gun mount offset
-- FS2 guns do not converge: each bolt flies parallel to the nose from its own mount, so to hit
-- point P the NOSE must point at P minus the mount offset (a gun above the cockpit means aiming
-- low). Measured, not read from the model: own bolts are projected onto our right and up axes.
-- A bolt is first seen a frame or so after it fired, so its position is back-projected to the
-- moment of firing (bolt and ship both moved for `age` seconds), and samples are taken only while
-- we are hardly rotating or sliding: a turning or slipping ship drags an unprojected sample ~1 m
-- toward the turn, the same way every time, so averaging cannot remove it (QA CR-3/SL-3,
-- 2026-10-15). Learned per ship class and primary weapon, because the mounts differ.
A1.guns = A1.guns or {}
local GUN_SAMPLES = 12
local gun_seen = {}                 -- bolt signatures already sampled; cleared on a mission restart

-- The learned-offset record for this ship class + bank-1 primary, created empty on first use.
local function gun_entry(me)
	local b = me.PrimaryBanks[1]
	local wname = (b and b:isValid() and b.WeaponClass:isValid()) and b.WeaponClass.Name or "?"
	local key = me.Class.Name .. "/" .. wname
	local g = A1.guns[key]
	if not g then
		g = { x = 0, y = 0, n = 0 }
		A1.guns[key] = g
	end
	return g, key
end

-- Sample fresh own bolts into the offset average (stops after GUN_SAMPLES samples).
local function learn_gun_offset(me)
	local g, key = gun_entry(me)
	if g.n >= GUN_SAMPLES then return end
	local rv = me.Physics.RotationalVelocity
	if math.abs(rv.x) > 0.15 or math.abs(rv.y) > 0.15 or math.abs(rv.z) > 0.3 then return end
	local o = me.Orientation
	local mvel = me.Physics.Velocity
	if math.abs(mvel:getDotProduct(o:getRvec())) + math.abs(mvel:getDotProduct(o:getUvec())) > 3 then return end
	local sig = me:getSignature()
	for i = 1, #mn.Weapons do
		local w = mn.Weapons[i]
		if w:isValid() and not w.Class:isMissile() and w.Parent:isValid() and w.Parent:getSignature() == sig then
			local ws = w:getSignature()
			-- Age from lifetime used: exact for weapons with a fixed lifetime (all retail lasers),
			-- wrong for tables that set a $Life range, where each bolt's life is randomized
			-- (weapons.cpp:7347-7350) and Lua cannot read the range (QA XFK-2). Negative ages
			-- are therefore skipped; a positive wrong age remains a known limit.
			local age = w.Class.LifeMax - w.LifeLeft
			if not gun_seen[ws] and age >= 0 and age < 0.1 then
				gun_seen[ws] = true
				local d = (w.Position - w.Physics.Velocity * age) - (me.Position - mvel * age)
				local n = g.n
				g.x = (g.x * n + d:getDotProduct(o:getRvec())) / (n + 1)
				g.y = (g.y * n + d:getDotProduct(o:getUvec())) / (n + 1)
				g.n = n + 1
				if g.n >= GUN_SAMPLES then
					note(string.format("gun offset learned for %s: right %.2f up %.2f m", key, g.x, g.y))
					return
				end
			end
		end
	end
end

-- World-space mount offset for the current ship and weapon (zero until learned).
local function gun_vec(me)
	local g = gun_entry(me)
	local o = me.Orientation
	return o:getRvec() * g.x + o:getUvec() * g.y
end

-- ---------------------------------------------------------------- v2: second-order lead
-- A fighter turning at 1.5 rad/s at 60 m/s accelerates ~90 m/s^2 sideways; over a 0.5 s bolt
-- flight that is ~11 m off a straight-line prediction, the size of the target. So estimate the
-- target's acceleration from its velocity history and lead with p + v*t + a*t^2/2.
-- (The HUD lead indicator has the same optional term: hudtarget.cpp:3957-3993.)
-- Smoothing is a time constant, not a per-frame weight, so it behaves the same at any frame rate
-- (QA CR-7/SL-7; 0.09 s matches the per-frame 0.3 that was tuned at ~30 fps).
A1.cfg.accel_lead = (A1.cfg.accel_lead == nil) and true or A1.cfg.accel_lead
A1.cfg.accel_tau = A1.cfg.accel_tau or 0.09
local acc_est = { sig = nil, v = nil, t = -1, a = nil }
local function target_accel(tgt, now)
	local sig = tgt:getSignature()
	local v = tgt.Physics.Velocity
	local dt = now - acc_est.t
	if acc_est.sig ~= sig or acc_est.v == nil or dt > 0.25 or dt < 0 then
		acc_est.sig, acc_est.v, acc_est.t, acc_est.a = sig, v, now, ba.createVector(0, 0, 0)
		return acc_est.a
	end
	if dt == 0 then return acc_est.a end
	local raw = (v - acc_est.v) / dt
	if raw:getMagnitude() > 200 then raw = raw:getNormalized() * 200 end   -- one bad frame
	local k = 1 - math.exp(-dt / A1.cfg.accel_tau)
	acc_est.a = acc_est.a * (1 - k) + raw * k
	acc_est.v, acc_est.t = v, now
	return acc_est.a
end

-- Refine a straight-line lead with acceleration: three fixed-point passes of
-- t = |p + v t + a t^2/2 - gun| / spd. Converges in 2-3 passes for t under ~1.5 s.
local function lead_point_accel(tgt, gunpos, spd, t0, a)
	local p, v = tgt.Position, tgt.Physics.Velocity
	local t = t0
	local pred = p
	for _ = 1, 3 do
		pred = p + v * t + a * (0.5 * t * t)
		t = (pred - gunpos):getMagnitude() / spd
		if t > 3 then break end
	end
	return pred
end

-- ---------------------------------------------------------------- v2: tracking law
-- The ship's turn rate approaches stick x max rate with time constant $Rotdamp
-- (physics.cpp:203-205; 0.35 s for the Myrmidon), so the nose is a second-order system. Per axis,
-- with e = angle to the aim point (stick signs) and w = our own turn rate:
--     rate command u = w + Kp*e + (1 + Kd)*de/dt
-- which makes  tau*e'' + (1 + Kd)*e' + Kp*e = 0  for a target moving at a steady angular rate
-- (w + de/dt is the line-of-sight rate, so the steady tracking error is zero, not rate/Kp).
-- Kp sets the speed (natural frequency sqrt(Kp/tau)), Kd the damping. The defaults are derived
-- from the ship's own tau: natural frequency ~4.1 rad/s, damping ratio 0.8 (Kp 6.0, Kd 1.3 for
-- the Myrmidon, the only ship they were flown on). v1 used stick = 3*e - 0.3*w: no feedforward.
A1.cfg.heat = A1.cfg.heat or { Rockeye = true, ["Stiletto II"] = true }   -- retail HEAT seekers (weapons.tbl)
-- Aspect seekers that reward a long lock: seconds to hold the nose on, and the farthest launch.
-- Trebuchet (retail weapons.tbl): +Min Lock Time 3.5 plus the pixel travel, 280 m/s for 18 s, no
-- "no dumbfire" flag, so an unlocked launch flies blind. It is the anti-bomber missile: players
-- fire it at bombers kilometres out so they die before releasing (lparchive FS2 update 34,
-- nightsolo FS2 guide). Others keep the default proxy: 2 s under 900 m.
A1.cfg.aspect = A1.cfg.aspect or { Trebuchet = { lock = 4.5, range = 3000 } }
A1.cfg.rate_tau = A1.cfg.rate_tau or 0.08   -- smoothing of de/dt in seconds (0.35/frame at ~30 fps)
local trk = { h = nil, p = nil, dh = 0, dp = 0, t = -1 }

-- Narrow-regime aim: returns heading, pitch and bank stick values.
local function track_axes(me, loc, now)
	local eh = math.atan2(loc.x, loc.z)
	local ep = -math.atan2(loc.y, loc.z)
	local ph = me.Physics
	local rv = ph.RotationalVelocity
	local mx = ph.RotationalVelocityMax
	local dt = now - trk.t
	if trk.h == nil or dt <= 0 or dt > 0.25 then
		trk.dh, trk.dp = 0, 0              -- fresh entry: no derivative history to trust
	else
		local a = 1 - math.exp(-dt / A1.cfg.rate_tau)
		trk.dh = (1 - a) * trk.dh + a * (eh - trk.h) / dt
		trk.dp = (1 - a) * trk.dp + a * (ep - trk.p) / dt
	end
	trk.h, trk.p, trk.t = eh, ep, now
	local tau = math.max(ph.RotationalVelocityDamping, 0.05)
	local Kp = A1.cfg.Kp or 17 * tau
	local Kd = A1.cfg.Kd or (1.6 * math.sqrt(Kp * tau) - 1)
	local uh = rv.y + Kp * eh + (1 + Kd) * trk.dh
	local up = rv.x + Kp * ep + (1 + Kd) * trk.dp
	local h = (mx.y > 0) and uh / mx.y or 0
	local p = (mx.x > 0) and up / mx.x or 0
	-- Yaw makes the ship roll by -heading x max yaw rate x $Banking Constant (physics.cpp:532,
	-- :538). Cancel it so the axes we just aimed with stay put, and damp leftover roll.
	-- (A ship class flagged "don't bank when turning" would need this term off; not handled.)
	local b = (mx.z > 0) and (clamp(h, -1, 1) * mx.y * ph.BankingConstant - 0.5 * rv.z) / mx.z or 0
	return clamp(h, -1, 1), clamp(p, -1, 1), clamp(b, -1, 1)
end

-- Stick values that swing the nose toward world point `p`. Returns heading, pitch and the angle
-- (radians) between the nose and p; leaves the roll command in last_bank. Conventions:
-- +Heading = nose right, +Pitch = nose DOWN (a point above needs negative pitch).
--
-- Two regimes, the way a pilot flies:
--   * point within wide_deg (45) degrees of the nose: aim with yaw and pitch, wings level;
--   * anything wider, or behind: BANK TO TURN. Roll the point to "straight up" on the screen,
--     then pull the nose up toward it; yaw only assists. The old flat pitch+yaw turn looped
--     vertically, and its pitch flipped sign whenever a point behind us crossed our wing
--     plane (atan2 jumps from +pi to -pi), which made it twitch.
local function steer_to(me, p, gain, precise)
	-- Project the offset onto our own right/up/forward axes (x right, y up, z forward).
	-- WHY dot products and not Orientation:unrotateVector: unrotate is local->world (the matrix
	-- transpose, vecmat.cpp:1068); the first test flight used it and steered toward nonsense.
	-- Explicit projections cannot be mixed up.
	local o = me.Orientation
	local off = p - me.Position
	local loc = { x = off:getDotProduct(o:getRvec()), y = off:getDotProduct(o:getUvec()), z = off:getDotProduct(o:getFvec()) }
	local dist = off:getMagnitude()
	if dist < 1 then last_bank = 0; return 0, 0, 0 end
	local ang = math.acos(clamp(loc.z / dist, -1, 1))

	-- Current turn rate as a fraction of the maximum, per axis (x = pitch, y = heading, z = roll),
	-- in the same sign convention as the stick.
	local rv = me.Physics.RotationalVelocity
	local mx = me.Physics.RotationalVelocityMax
	local wx = (mx.x > 0) and rv.x / mx.x or 0
	local wy = (mx.y > 0) and rv.y / mx.y or 0
	local wz = (mx.z > 0) and rv.z / mx.z or 0

	local h, pt, b
	-- v2: aiming at a target (precise) inside the narrow cone uses the tracking law instead.
	if precise and loc.z > 0 and ang < math.rad(A1.cfg.wide_deg) then
		h, pt, b = track_axes(me, loc, mn.getMissionTime())
		last_bank = b
		return h, pt, ang
	end
	trk.h = nil                                        -- leaving the narrow regime resets its history
	if loc.z > 0 and ang < math.rad(A1.cfg.wide_deg) then
		h = gain * math.atan2(loc.x, loc.z)
		pt = -gain * math.atan2(loc.y, loc.z)
		b = 0
	else
		local phi = math.atan2(loc.x, loc.y)          -- 0 = straight up on screen, +/-pi = below
		b = BANK_SIGN * 2.0 * phi
		pt = -math.max(math.cos(phi), 0)               -- pull up only once the point is above
		h = 0.5 * clamp(gain * math.atan2(loc.x, math.abs(loc.z) + 1), -1, 1)
		A1.dbg_phi = math.deg(phi)
	end
	local KD = A1.cfg.KD
	last_bank = clamp(b - KD * wz, -1, 1)
	return clamp(h - KD * wy, -1, 1), clamp(pt - KD * wx, -1, 1), ang
end

-- Threat score for an enemy fighter: lower = more urgent. Distance to the nearest freighter
-- counts fully, distance to us at half weight, and a fighter already aiming at a freighter is
-- pulled 1500 m closer. WHY freighters first: the only goal is "Protect Iota"; kills that let a
-- freighter take hull damage cost the debriefing more than they earn.
A1.cfg.bomber_bonus = A1.cfg.bomber_bonus or 2000
A1.cfg.leash = A1.cfg.leash or 2500
-- Shooters of the guarded ships: signature -> mission time a hostile bolt of theirs last arrived
-- within 30 m of a guarded hull's radius. The stock guard AI turns on whoever hits its charge
-- (aicode.cpp guard_object_was_hit), and in the escort drill guns, not bombs, did most of the
-- freighter's damage (tally 2026-10-15), so a shooter counts A1.cfg.hitter_bonus metres closer
-- for 4 s after its last hit. It outranks the bomber bonus on purpose: solo, the escorting Mara
-- killed the freighter with lasers in ~45 s while a 1500 m bonus left the pilot on bombers 97%
-- of the time (escort drill 2026-10-15). 0 switches it off for comparisons.
A1.cfg.hitter_bonus = A1.cfg.hitter_bonus or 3500
-- An enemy whose target is a guarded ship counts this many metres closer (default 1500).
A1.cfg.aimed_bonus = A1.cfg.aimed_bonus or 1500
local hitters = {}
local function note_hitters(me, guards, now)
	if #guards == 0 or A1.cfg.hitter_bonus == 0 then return end
	for sig, t in pairs(hitters) do                -- bounded: entries older than 10 s are dropped
		if now - t > 10 or now < t then hitters[sig] = nil end
	end
	for i = 1, #mn.Weapons do
		local w = mn.Weapons[i]
		if w:isValid() and not w.Class.Bomb and w.Team:attacks(me.Team) and w.Parent:isValid() then
			for _, g in ipairs(guards) do
				if w.Position:getDistance(g.Position) < g.Radius + 30 then
					hitters[w.Parent:getSignature()] = now
					break
				end
			end
		end
	end
end
local function threat(e, me, guards)
	-- The bonus is applied once, after the minimum: inside the loop a later guard could overwrite
	-- it, so the same enemy scored differently depending on guard-list order (QA XFA-4).
	local dg, aimed = 1e9, false
	local t = e.Target
	local tsig = (t and t:isValid()) and t:getSignature() or nil
	for _, g in ipairs(guards) do
		local d = e.Position:getDistance(g.Position)
		if d < dg then dg = d end
		if tsig and tsig == g:getSignature() then aimed = true end
	end
	if aimed then dg = dg - A1.cfg.aimed_bonus end
	-- Escort priorities (players' rule, lparchive FS2 Let's Play updates 4 and 30: bombs, then
	-- bombers, then the rest): a bomber counts A1.cfg.bomber_bonus metres closer. Leash: a
	-- fighter far from every guarded ship and not shooting at one is not worth leaving them for.
	local t_name = e.Class.Type:isValid() and e.Class.Type.Name or ""
	if t_name == "Bomber" then dg = dg - A1.cfg.bomber_bonus end
	local hit_t = hitters[e:getSignature()]
	local hitting = hit_t and mn.getMissionTime() - hit_t < 4
	if hitting then dg = dg - A1.cfg.hitter_bonus end
	if t_name ~= "Bomber" and not aimed and not hitting and dg > A1.cfg.leash then dg = dg + 5000 end
	return dg + 0.5 * e.Position:getDistance(me.Position)
end

-- Collect live hostile fighters (teams that attack ours). Bounded by the mission's ship list.
-- Also any small ship of another team whose target is a guarded ship, whatever its team's
-- standing: in FSPort's "The Hammer and the Anvil" the Vasudan "allies" Pi and Mu turn Neutral
-- and chase the cargo at priority 89 (sm1-08a.fs2 events "HOL revealed", "HOL Goal"), and a
-- team check alone never sees them. The mission's own tip: "Only attack ships that are
-- targeting the freighters or their cargo."
local function enemies(me)
	local list = {}
	local guarded = {}
	for _, n in ipairs(A1.cfg.guard or {}) do
		local g = mn.Ships[n]
		if g and g:isValid() then guarded[g:getSignature()] = true end
	end
	for i = 1, #mn.Ships do
		local s = mn.Ships[i]
		if s:isValid() and not s:isDying() and is_small(s) and not s:isArrivingWarp()
				and not (A1.spare_sig and not A1.spare_released and s:getSignature() == A1.spare_sig) then
			local hostile = s.Team:attacks(me.Team)
			if not hostile and next(guarded) and s.Team.Name ~= me.Team.Name then
				local t = s.Target
				hostile = t and t:isValid() and guarded[t:getSignature()] or false
			end
			if hostile then list[#list + 1] = s end
		end
	end
	return list
end

-- Nearest large ship we are about to fly into (within its radius + margin, closing).
-- Returns that ship or nil. Used to break off an attack run before ramming a capital ship.
-- ---------------------------------------------------------------- escort: bombs in flight
-- A hostile bomb heading for a guarded ship, the one that will arrive soonest, if we can reach
-- it (within 1500 m of us); else nil. "Heading for" = homing on that ship, or within 1500 m of it
-- and closing. Retail bombs have 50 hit points unless the table says otherwise
-- (weapons.cpp:4131-4133) and are hit with a doubled radius by lasers
-- (collideweaponweapon.cpp:65-68), so a fighter can shoot them down.
A1.cfg.intercept = (A1.cfg.intercept == nil) and true or A1.cfg.intercept   -- switch for comparisons
-- A1.cfg.missile_block = true: a homing missile (not a bomb: those are shot down above) on its
-- way to a guarded ship, which we can reach a point on its line before it does; returns the
-- missile and that point (just outside the guarded hull, on the missile's side), else nil.
-- The idea: lasers cannot shoot missiles down, but a missile hits whatever ship it meets, and
-- near the target every missile converges on one line. Measured in FSPort "The Hammer and the
-- Anvil" (2026-10-15): no effect, 29-33 MX-50 hits on the containers per run with it, 32
-- without; kept for comparison, off by default.
local function incoming_missile(me, guards)
	if #guards == 0 or not A1.cfg.missile_block then return nil end
	local best, best_t, best_p = nil, 1e9, nil
	local myspeed = math.max(me.Physics.VelocityMax.z, 1)
	for i = 1, #mn.Weapons do
		local w = mn.Weapons[i]
		local ho = w:isValid() and not w.Class.Bomb and w.HomingObject
		if ho and ho:isValid() and w.Team:attacks(me.Team) then
			local hs = ho:getSignature()
			for _, g in ipairs(guards) do
				if hs == g:getSignature() then
					local rel = w.Position - g.Position
					local d = rel:getMagnitude()
					local v = w.Physics.Velocity:getMagnitude()
					if d > 1 and d < 1500 and v > 1 then
						local p = g.Position + rel / d * (g.Radius + 40)
						local t = (d - g.Radius - 40) / v
						-- Reachable: our flight time to the point (straight line, top speed) is less.
						if t < best_t and p:getDistance(me.Position) / myspeed < t then best, best_t, best_p = w, t, p end
					end
					break
				end
			end
		end
	end
	return best, best_p
end

local function incoming_bomb(me, guards)
	if #guards == 0 or not A1.cfg.intercept then return nil end
	local best, best_t = nil, 1e9
	for i = 1, #mn.Weapons do
		local w = mn.Weapons[i]
		if w:isValid() and w.Class.Bomb and w.Team:attacks(me.Team)
				and w.Position:getDistance(me.Position) < 1500 then
			local ho = w.HomingObject
			local hsig = (ho and ho:isValid()) and ho:getSignature() or nil
			local wv = w.Physics.Velocity
			local wspd = math.max(wv:getMagnitude(), 1)
			for _, g in ipairs(guards) do
				local rp = g.Position - w.Position
				local d = rp:getMagnitude()
				local closing = wv:getDotProduct(rp) > 0
				if (hsig and hsig == g:getSignature()) or (d < 1500 and closing) then
					local t = d / wspd
					if t < best_t then best, best_t = w, t end
				end
			end
		end
	end
	return best
end

-- Hit point on ship s along the segment from -> to, or nil. checkRayCollision alone casts from
-- `from` THROUGH `to` on to infinity (MC_CHECK_RAY, model.h:1370), so a 40 m "ray" toward a
-- container 571 m away reported a hit (recon drill 2026-10-15); the length is checked here.
local function hull_hit(s, from, to)
	local hit = s:checkRayCollision(from, to)
	if hit and hit:getDistance(from) <= to:getDistance(from) then return hit end
	return nil
end

local function collision_threat(me, vel)
	for i = 1, #mn.Ships do
		local s = mn.Ships[i]
		if s:isValid() and s.Name ~= me.Name and not s:isDying() then
			local rp = s.Position - me.Position
			local d = rp:getMagnitude()
			local margin = s.Radius + me.Radius + (is_small(s) and 25 or 120)
			if is_small(s) then
				-- Fighters and bombers: closest approach along our velocity RELATIVE to theirs. With
				-- our own velocity alone, sitting 180 m behind a bomber flying the same way read as
				-- a collision 2.4 s ahead, and the sidestep and extend broke off every tail attack
				-- (escort drill, 2026-10-15).
				local rv = vel - s.Physics.Velocity
				if d < margin + 300 and rv:getDotProduct(rp) > 0 then
					local spd2 = rv:getDotProduct(rv)
					if spd2 > 1 then
						local tca = rp:getDotProduct(rv) / spd2
						local miss = (rp - rv * tca):getMagnitude()
						if tca < 2.5 and miss < margin then return s end
					end
				end
			elseif d < s.Radius + 600 then
				-- Big ships: a bounding sphere is useless next to a 3 km destroyer (we sat
				-- "inside" the Psamtik's sphere legitimately), so look at the real hull: cast a
				-- ray along the nose (250 m) and along 3 s of travel. This is the pilot's view
				-- of a hull wall ahead, not hidden information. Test flight 3 ground along the
				-- Psamtik for 16% hull because the old test only used our own velocity.
				-- The ship being scanned (its convoy, and the ship we fly to) is held nose-on on
				-- purpose, so it gets a shorter nose ray (the 250 m ray broke off every scan hold,
				-- recon drill 2026-10-15).
				local is_goal = A1.goal_sig and s:getSignature() == A1.goal_sig
				local scanning = (A1.scan_sig and s:getSignature() == A1.scan_sig) or is_goal
					or (A1.scan_group and A1.scan_group[s:getSignature()])
				-- That ray is the scan hold distance (A1.scan_ray, 30-250 m), 60 m for the ship we
				-- fly to: a fixed 40 m suited a 23 m container and let the pilot hit a manoeuvring
				-- 106 m cruiser five times (Playing Judas run 5).
				local fwd = me.Position + me.Orientation:getFvec() * (is_goal and 60 or (scanning and (A1.scan_ray or 40) or 250))
				-- Relative velocity, as for fighters: a destroyer moving at 40 m/s into our side never
				-- crossed a ray built from our own velocity (four contacts with the Lucifer as it
				-- arrived, Playing Judas 2026-10-15).
				local ahead = me.Position + (vel - s.Physics.Velocity) * (scanning and 0.5 or 3) + me.Orientation:getFvec() * (scanning and 20 or 60)
				if hull_hit(s, me.Position, fwd) or hull_hit(s, me.Position, ahead) then
					return s
				end
			end
		end
	end
	return nil
end

local last_t, last_cm, last_eq, last_sec = -1, -100, -100, -100
local lock_time = 0          -- seconds the target has stayed in the missile cone (lock proxy)
local cur_target_name = nil
local target_since = -100    -- mission time the current target was picked (commitment timer)
local missile_check = nil    -- {t=, target=} : verify the next frame that our missile is homing
local last_sname = nil       -- secondary weapon selected last frame (resets the lock proxy on change)
-- Wing orders: which guarded ship the wing was last told to protect, how many wingmen there were,
-- and the comm menu step in progress ("open": Protect My Target pressed, menu not yet answered).
local wing_order = { sig = nil, n = 0, step = nil, t = 0 }

-- Class name of the selected (armed) secondary bank, or "" if none is armed.
local function armed_secondary_name(me)
	for i = 1, #me.SecondaryBanks do
		local b = me.SecondaryBanks[i]
		if b and b:isValid() and b.Armed and b.WeaponClass:isValid() then return b.WeaponClass.Name end
	end
	return ""
end
local ets_presses = A1.ets_done and 0 or 2   -- ETS taps at the start (policy below); 0 on hot reload
A1.cfg.ets = A1.cfg.ets or "weapons"
local ETS_KEY = { weapons = "INCREASE_WEAPON", shields = "INCREASE_SHIELD" }   -- balanced: no taps

-- ---------------------------------------------------------------- v2-defence: jink
-- The AI's lead builds up over the time a target stays in its sights (about 2 s plus a skill
-- term, aicode.cpp:7215-7291), and Insane AI hits hard: v2 without any defence lost 7 of 8
-- Insane duels while hitting 71-90% of its own shots (2026-10-15). So when an enemy fighter has
-- us inside its gun cone at gun range, and we are not about to kill it, swerve off its line for
-- about a second (committed, not re-decided each frame), then go back to the attack.
-- Switch: A1.cfg.jink (default on).
A1.cfg.jink = (A1.cfg.jink == nil) and true or A1.cfg.jink
local jink = { till = -1, ready = -1, dir = nil }
-- Strike-mode state (strike_frame below); declared here so reset_after_restart can clear it.
local strike = { phase = "approach", until_t = -1, last_try = -100, runs = 0, side = nil, escaping = false }

-- The most dangerous enemy fighter pointing its guns at us: within 800 m and its nose within
-- ~14 degrees of us. Returns that ship or nil.
local function gun_threat(me, list)
	local best, best_d = nil, 800
	for _, e in ipairs(list) do
		local rp = me.Position - e.Position
		local d = rp:getMagnitude()
		if d < best_d and d > 1 and e.Orientation:getFvec():getDotProduct(rp / d) > 0.97 then
			best, best_d = e, d
		end
	end
	return best
end


-- A mission restart (player death, or a new mission in the same engine session) keeps this file's
-- state but sends the mission clock back toward 0, and object signatures restart too
-- (object.cpp:460). Every timestamp above would then lie in the future and block
-- countermeasures, missiles and shield equalizing until the clock caught up; an avoid could hold
-- a stale heading for minutes (QA CR-2/SL-2/SL-4/SL-5, 2026-10-15). Forget all of it.
local function reset_after_restart()
	last_cm, last_eq, last_sec = -100, -100, -100
	lock_time, cur_target_name, target_since, missile_check, last_sname = 0, nil, -100, nil, nil
	-- Flight-quality counters restart too, or a per-run frame rate spans two missions (QA XFK-1).
	A1.fq = { track = 0, on = 0, avoids = 0, extends = 0, frames = 0, t0 = mn.getMissionTime() }
	ets_presses = 2                       -- the new ship starts at default ETS
	A1.ets_done, A1.ets_applied = nil, nil
	A1.link_t, A1.link_tries = nil, nil
	trk.h = nil
	wing_order.sig, wing_order.n, wing_order.step, wing_order.check, wing_order.hold = nil, 0, nil, nil, nil
	acc_est.sig = nil
	gun_seen = {}
	A1.avoid, A1.extend, A1.contact, A1.escaping, A1.scan_sig = nil, nil, nil, false, nil
	A1.reached, A1.jump_t, A1.goal_sig, A1.goal_entry_done = nil, nil, nil, nil
	A1.amb_t, A1.amb_done, A1.amb_noted, A1.brake_till = nil, nil, nil, nil
	A1.scanned, A1.scan_group, A1.close_t, A1.idle_p = {}, nil, nil, nil
	A1.spare_seen, A1.spare_sig, A1.spare_step, A1.spare_menu, A1.spare_t, A1.spare_released = nil, nil, nil, nil, nil, nil
	jink.till, jink.ready, jink.dir = -1, -1, nil
	strike.phase, strike.until_t, strike.last_try, strike.disarming, strike.escaping = "approach", -1, -100, false, false
	note("mission clock went back: restart detected, pilot state reset")
end
A1.press = A1.press or {}    -- queue of one-shot control names the operator wants pressed (e.g. Alt-J)

-- Should a HEAT seeker be launched at tgt now? Pure read of the scene (and last_sec).
-- Retail Rockeye and Stiletto II are "+Type: HEAT" (weapons.tbl): no lock needed, the seeker
-- homes on our target at launch (weapons.cpp:7013-7019). Lua cannot read the homing type, so they
-- are listed by class name in A1.cfg.heat. The rules, each from a measured failure (2026-10-15):
--   * one missile in flight per target: a 1.5 s spacing fired 48 Rockeyes for 7 hits (SM1-01);
--   * against fighters that turn at least as fast as we do: tail shots only (nearly ahead, flying
--     away, 200-700 m); 20 degrees at 200-1200 m fired 23 Rockeyes for 2 hits at SF Dragons.
--     Our own yaw rate is the yardstick because a target we can out-turn is one we can stay
--     behind; the missile's own turn rate is not readable here;
--   * against bombers and slower-turning ships: 20 degrees, 200-1200 m (SM1-01: 5 fired, 5 hit).
local function heat_launch_ok(me, tgt, ang, away, dist, now)
	local tsig, msig = tgt:getSignature(), me:getSignature()
	for i = 1, #mn.Weapons do
		local w = mn.Weapons[i]
		if w:isValid() and w.Class:isMissile() and w.Parent:isValid() and w.Parent:getSignature() == msig then
			local ho = w.HomingObject
			if ho and ho:isValid() and ho:getSignature() == tsig then return false end
		end
	end
	local agile = tgt.Class.Type:isValid() and tgt.Class.Type.Name ~= "Bomber"
		and tgt.Physics.RotationalVelocityMax.y >= me.Physics.RotationalVelocityMax.y
	if agile then
		return ang < math.rad(10) and away > 0.7 and dist > 200 and dist < 700 and now - last_sec > 3
	end
	return ang < math.rad(20) and dist > 200 and dist < 1200 and now - last_sec > 1.5
end

-- ---------------------------------------------------------------- strike mode
-- A1.cfg.strike = "<capital ship name>": bomb that ship. Players' method (lparchive FS2 Let's
-- Play updates 7 and 23): close at full speed, throttle down inside ~2 km to hold the lock, hold
-- the nose on the target for the bomb's lock time, release, break away at full throttle, come
-- back. The lock itself cannot be read from Lua, but retail bombs are "No Dumbfire": the engine
-- refuses to launch one without a lock (ship.cpp:14265), so a launch attempt every 0.25 s with
-- the nose on target IS the lock check. Lock point: the hull centre while the nose is within
-- ~18 degrees of it (hudlock.cpp:2097-2153; no subsystem targeting yet).
-- Phases: "approach" (nose on target, bombs tried inside the window) -> "egress" (break away to
-- 2.5 km or 12 s) -> "approach".
-- (the `strike` state table is declared above, before reset_after_restart, which clears it)

-- Weapon class of the armed secondary bank, or nil.
local function armed_secondary(me)
	for i = 1, #me.SecondaryBanks do
		local b = me.SecondaryBanks[i]
		if b and b:isValid() and b.Armed and b.WeaponClass:isValid() then return b.WeaponClass end
	end
	return nil
end

-- Release distance (m from the hull): a Cyclops (95 m/s) let go at 2000 m spends 21 s inside
-- the target's turret coverage, and 0 of 20 hit a Fenris (strike drill 2026-10-15); players
-- release close (nightsolo FS2 guide, mission 30: "close range"), and knock out turrets first
-- (hard-light FS2 Tactical, "Assault Missions").
A1.cfg.strike_release = A1.cfg.strike_release or 900
-- The target's "weapons" subsystem while it still stands, else nil. With it destroyed the
-- target's turrets no longer fire at bombs (aiturret.cpp:803-805, 516) and aim worse (:1296);
-- the wiki's assault advice is to knock out turrets or the weapons subsystem before bombing.
local function weapons_subsys(cap)
	for i = 1, #cap do
		local ss = cap[i]
		if ss:isValid() and ss.Name:lower() == "weapons" and ss.HitpointsLeft > 0 then return ss end
	end
	return nil
end

-- One frame of the disarm phase: guns on subsystem `ss` of `cap` from a 500-800 m station, out
-- to 800 m and back when inside 350 m (the capital-ship attack's escape leg, kept short so
-- the fire gaps stay short). The subsystem is also made the HUD subtarget, as a player would.
local function disarm_frame(me, cap, ss, now)
	local mts = me.TargetSubsystem
	if not (mts and mts:isValid() and mts.Name == ss.Name) then me.TargetSubsystem = ss end
	local wp = ss.WorldPosition
	local d = wp:getDistance(me.Position)
	if strike.escaping and d > 800 then strike.escaping = false end
	if not strike.escaping and d < 350 then strike.escaping = true end
	if strike.escaping then
		local h, p = steer_to(me, me.Position * 2 - wp, 3.0)
		return h, p, 1.0
	end
	local h, p, ang = steer_to(me, wp, A1.cfg.gain, true)
	local throttle = (d > 900) and 1.0 or ((d > 500) and 0.5 or 0.2)
	local cone = math.atan(math.max(ss.Radius, 3) / math.max(d, 1))
	if ang < cone and d < 900 then
		A1.stats.primary_shots = A1.stats.primary_shots + (me:firePrimary() or 0)
	end
	return h, p, throttle
end

-- One frame of a strike on capital ship `cap`. Returns heading, pitch, throttle.
local function strike_frame(me, cap, now)
	if A1.cfg.strike_disarm ~= false then
		local ss = weapons_subsys(cap)
		if ss then
			if not strike.disarming then strike.disarming = true; note("strike: guns on " .. cap.Name .. "'s weapons subsystem") end
			return disarm_frame(me, cap, ss, now)
		elseif strike.disarming then
			strike.disarming = false
			note("strike: weapons subsystem down, bombing")
		end
	end
	local aimp = cap.Position
	local hit = cap:checkRayCollision(me.Position, aimp)
	local surf = hit and hit:getDistance(me.Position) or aimp:getDistance(me.Position)
	if strike.phase == "egress" then
		if surf > 2500 or now > strike.until_t then
			strike.phase = "approach"
			note(string.format("strike: turning in (%.0f m)", surf))
		else
			local away = (me.Position - aimp):getNormalized()
			local dir = (away + strike.side):getNormalized()
			local h, p = steer_to(me, me.Position + dir * 600, 3.0)
			return h, p, 1.0
		end
	end
	local h, p, ang = steer_to(me, aimp, A1.cfg.gain, true)
	local throttle = (surf > 2000) and 1.0 or 0.5
	local wc = armed_secondary(me)
	local wrange = wc and math.min(wc.Range, wc.Speed * wc.LifeMax) or 0
	if wc and surf > 400 and surf < A1.cfg.strike_release and aimp:getDistance(me.Position) < 0.9 * wrange
			and ang < math.rad(6) and now - strike.last_try > 0.25 then
		strike.last_try = now
		local n = me:fireSecondary()
		if n and n > 0 then
			A1.stats.secondary_shots = A1.stats.secondary_shots + n
			strike.runs = strike.runs + 1
			note(string.format("strike: bomb away at %.0f m (run %d)", surf, strike.runs))
			strike.phase, strike.until_t = "egress", now + 12
			local o = me.Orientation
			strike.side = (math.random() < 0.5) and o:getRvec() or (o:getRvec() * -1)
		end
	end
	-- Guns on the hull whenever they bear: free damage on a target this size.
	if ang < math.rad(4) and surf < 900 then
		A1.stats.primary_shots = A1.stats.primary_shots + (me:firePrimary() or 0)
	end
	if surf < 450 then                       -- too close to release safely: abort the run
		strike.phase, strike.until_t = "egress", now + 12
		strike.side = me.Orientation:getUvec()
	end
	A1.striking = (strike.phase == "approach" and surf < 2000)
	return h, p, throttle
end

-- ---------------------------------------------------------------- recon / stealth mode
-- A1.cfg.scan = { "<ship>", ... }: scan each in order. Scanning needs the target within
-- max(150, radius + 100) m and the nose within ~18 degrees (dot 0.95) for its scan time, 2 s by
-- default (playercontrol.cpp:1764-1823), and resets when either slips; so fly in, then hold
-- position near it with the nose on it until its "cargo-known" flag is set.
-- A1.cfg.avoid = { "<ship>", ... } and A1.cfg.avoid_radius (m): stay farther than that from each
-- of them. In FSPort's "Playing Judas" being seen is scripted until the Lucifer arrives: within
-- 486 m of a patrol fighter or sentry below Medium, 586 m at Medium, 610 m of a fighter (486 m of
-- a sentry) at Hard and Insane, or pressing a fire key (sm2-08a.fs2, the "Stop Ignoring" events);
-- with the engine's stealth flag the AI sees a ship within ~400 m x
-- skill and inside its forward view (aicode.cpp:1146-1187). A1.cfg.stealth = true holds all fire.
A1.cfg.avoid_radius = A1.cfg.avoid_radius or 750

-- The NEAREST ship on the scan list that is alive, arrived and not yet scanned, or nil. Nearest,
-- not list order: transports arrive in groups and leave again, and list order criss-crossed a
-- group while its members jumped out unscanned (Playing Judas, Kali/Durga/Ratri, 2026-10-15).
local function next_scan(me)
	-- A1.cfg.scan_first: ships scanned before anything else whenever present (Playing Judas: the
	-- Eva, which "leaves pretty quick", sm2-08a.fs2 debriefing).
	-- A1.cfg.scan_tiers = { {names}, {names}, ... }: the first tier with any ship present and
	-- unscanned wins, nearest-first inside it; ships on no tier come last. Playing Judas order,
	-- by what leaves first: the Eva; the Kali transports (they pass through in ~100 s); then the
	-- freighters with the cargo they dock with and carry away, and the slow cruisers; then the
	-- Lucifer (sm2-08a.fs2 AI goals and cues).
	A1.scanned = A1.scanned or {}                 -- names seen scanned (kept after they leave)
	-- A tier with `front_first = true` takes the ship furthest along its own direction of travel
	-- first: dropping back to the next one is easy, catching up from behind is slow (a Kali
	-- transport cruises at 55 m/s against our 75, and the leader was missed in every run).
	local function nearest(list)
		local best, bd = nil, 1e12
		for _, name in ipairs(list) do
			local s = live_ship(name)
			if s and s:getFlag("cargo-known") then A1.scanned[name] = true end
			if s and not s:getFlag("cargo-known") and not s:isArrivingWarp() then
				local d = s.Position:getDistance(me.Position)
				local v = s.Physics.Velocity
				local spd = v:getMagnitude()
				if list.front_first and spd > 10 then d = 5000 - (s.Position - me.Position):getDotProduct(v / spd) end
				-- Stick with the ship being scanned unless another is under half its distance:
				-- switching resets the scan timer (playercontrol.cpp cargo_inspect_time).
				if A1.scan_sig and s:getSignature() == A1.scan_sig then d = d * 0.5 end
				if d < bd then best, bd = s, d end
			end
		end
		return best
	end
	for _, tier in ipairs(A1.cfg.scan_tiers or { A1.cfg.scan_first or {} }) do
		local s = nearest(tier)
		if s then
			-- The rest of this tier (a convoy) is flown among at matched speed: the hull check
			-- treats them like the scan target (A1.scan_group), or every switch to the next ship
			-- set off a 3 s sidestep from the one just scanned (Kali, Playing Judas iteration 4).
			A1.scan_group = {}
			for _, n in ipairs(tier) do
				local o = live_ship(n)
				if o then A1.scan_group[o:getSignature()] = true end
			end
			return s
		end
	end
	A1.scan_group = nil
	return nearest(A1.cfg.scan or {})
end

-- Direction (unit vector) toward `goal` bent away from every ship on the avoid list that is
-- within avoid_radius + 400 m. Returns the direction and the closest avoid-ship distance.
local function avoid_dir(me, goal)
	local dir = (goal - me.Position):getNormalized()
	local closest = 1e9
	local r = A1.cfg.avoid_radius
	-- A1.cfg.avoid_until = "<ship>": the avoid list lapses once that ship is in the mission. In
	-- Playing Judas every detection event requires the Lucifer NOT to have arrived (sm2-08a.fs2),
	-- and a sentry gun sits 133 m from the fighter bay the pilot must then enter.
	-- A1.cfg.avoid_lapse = { names }: only these lapse (all of the list when it is absent). The
	-- Arjuna fighters attacked after the whole list lapsed and killed the pilot; only the sentry
	-- by the fighter bay needs passing (Playing Judas iteration 5).
	local lapsed = A1.cfg.avoid_until and live_ship(A1.cfg.avoid_until)
	if lapsed and not A1.cfg.avoid_lapse then return dir, closest end
	local lapse_set = {}
	for _, n in ipairs(A1.cfg.avoid_lapse or {}) do lapse_set[n] = true end
	for _, name in ipairs(A1.cfg.avoid or {}) do
		local a = not (lapsed and lapse_set[name]) and live_ship(name)
		if a then
			local away = me.Position - a.Position
			local d = away:getMagnitude()
			if d < closest then closest = d end
			-- Log every close pass (at most once per 2 s per ship): where detections come from.
			if d < r then
				A1.close_t = A1.close_t or {}
				local now = mn.getMissionTime()
				if not A1.close_t[name] or now - A1.close_t[name] > 2 then
					A1.close_t[name] = now
					note(string.format("close pass: %s at %.0f m", name, d))
				end
			end
			if d < r + 400 and d > 1 then
				local w = ((r + 400 - d) / 400)
				dir = dir + away / d * (3 * w * w)
			end
		end
	end
	return dir:getNormalized(), closest
end

-- One frame of recon on ship `tgt`. Returns heading, pitch, throttle.
local function scan_frame(me, tgt)
	-- Range is the HUD's distance to the target's bounding box, and a scan works inside
	-- max(150, radius + 100) x the scanner's range multiplier (playercontrol.cpp:1768; constants
	-- hudtarget.h:29-30). Holding a share of that off the BOX keeps a cruiser's hull clear (a
	-- hold measured from the centre rammed the Lilith in Playing Judas, 2026-10-15) and lets a
	-- 1 km warship be scanned from several hundred metres.
	local d = hu.getTargetDistance(tgt)
	local scan_dist = math.max(150, tgt.Radius + 100) * math.max(me.Class.ScanningRangeMultiplier, 0.1)
	-- 70% of scan range, with the ram check's nose ray as long as the hold: at 60% with the ray
	-- ending at 80% of the hold, a freighter turning into the pilot hit it eight times in one run
	-- (Playing Judas iteration 4).
	local hold = clamp(0.7 * scan_dist, 60, scan_dist - 30)
	-- The ray is capped at 250 m: uncapped, the Lucifer's ray (then 80% of a 967 m hold) reached
	-- the far end of its 3 km hull from most angles and held the pilot in an avoid loop 1.5 km
	-- out (Playing Judas run 6).
	A1.scan_ray = clamp(hold, 30, 250)
	local dir, closest = avoid_dir(me, tgt.Position)
	local h, p, ang
	local maxv = math.max(me.Physics.VelocityMax.z, 1)
	local throttle
	if closest < A1.cfg.avoid_radius + 150 then
		-- Inside the warning ring of a ship we must avoid: steer by the avoidance field only.
		h, p = steer_to(me, me.Position + dir * 600, 3.0)
		throttle = 1.0
	else
		-- Arrive slowly: a closing speed of 0.25 m/s per metre still to go, capped at 40 m/s, on
		-- top of the target's own velocity (the formation rule below).
		-- 0.4 per metre with the target's whole speed added came in at ~40 m/s closing on a
		-- moving freighter, overshot into the hull check and looped (Playing Judas 2026-10-15);
		-- a fixed 0.6 throttle earlier arrived at ~70 m/s (recon drill).
		local los = (tgt.Position - me.Position):getNormalized()
		if d > hold + 400 then
			h, p = steer_to(me, me.Position + dir * 600, 3.0)
			throttle = 1.0
		else
			-- Formation flying on a station `hold` metres off the target's box, on our side of it:
			-- wanted velocity = the target's velocity + 0.25/s x the offset to the station (capped
			-- at 40 m/s). Far from the station, fly along that velocity; on it, nose on the target
			-- (the scan needs it) with the throttle giving the wanted speed along our nose. Matching
			-- only the speed along the line of sight overshot a turning freighter (Playing Judas).
			-- Where to hold, from the mission's own debriefing tips (sm2-08a.fs2): a moving ship we
			-- can outpace, "get behind and match speed"; one too fast to catch, "wait for them to
			-- come to you": beside its path, a few seconds ahead of it. Otherwise on our side of
			-- it. `reach` is the centre-to-box depth along our line of sight.
			local reach = tgt.Position:getDistance(me.Position) - d
			local tv = tgt.Physics.Velocity
			local ts = tv:getMagnitude()
			local station
			-- Big ships (radius over 300 m) are scanned from our side of them: "behind" a 2.2 km
			-- destroyer is 2 km away, and heading there threw away a scan spot beside the Eva and
			-- let it leave unscanned (Playing Judas iteration 6).
			if ts > 10 and tgt.Radius <= 300 then
				-- Moving target, far out: aim beside its track where it WILL be (lead = distance /
				-- our top speed, 2-20 s). Chasing from behind from far out closed at ~7 m/s on a
				-- 68 m/s transport and lost a Kali every run (Playing Judas, early runs).
				local vdir = tv / ts
				local rel = me.Position - tgt.Position
				local side = rel - vdir * rel:getDotProduct(vdir)
				local sm = side:getMagnitude()
				side = (sm > 1) and side / sm or tgt.Orientation:getUvec()
				if d < scan_dist + 300 then
					-- Close: drop in BEHIND it and match its speed ("get behind and match speed",
					-- the mission's own tip). With the nose on the target the throttle pushes
					-- along the line to it, which only matches the target's motion when we are
					-- behind it; beside it the target slides away sideways (Kali 3 took 74 s of
					-- near-misses without a scan, Playing Judas iteration 2).
					station = tgt.Position - vdir * (reach + hold)
				else
					local lead = clamp(tgt.Position:getDistance(me.Position) / maxv, 2, 20)
					station = tgt.Position + tv * lead + side * (reach + hold)
				end
			else
				station = tgt.Position - los * (reach + hold)
			end
			local off = station - me.Position
			local om = off:getMagnitude()
			local corr = (om > 1) and off * (math.min(0.25 * om, 40) / om) or ba.createVector(0, 0, 0)
			local want = tgt.Physics.Velocity + corr
			-- Inside scan range the nose stays on the target whatever the station error: any turn
			-- away restarts the scan timer, and the Lucifer's scan takes 20 s (ScanTime 20000 ms).
			if om > 80 and d > scan_dist - 40 then
				h, p = steer_to(me, me.Position + want:getNormalized() * 600, A1.cfg.gain)
			else
				h, p, ang = steer_to(me, tgt.Position, A1.cfg.gain, true)
			end
			-- Reverse allowed (down to half): with the nose on the target the throttle pushes
			-- TOWARD it, so a hold that can only slow down drifts into the hull; a 0 floor did that
			-- to a Lilith cruiser in a loop of near-misses (Playing Judas iteration 1).
			throttle = clamp(want:getDotProduct(me.Orientation:getFvec()) / maxv, -0.5, 1)
		end
	end
	-- A1.cfg.trace = "<ship>": once a second while scanning that ship, log the range to its box,
	-- the scan range, the nearest avoided ship and our speed (diagnosis aid; off by default).
	if A1.cfg.trace == tgt.Name then
		local now = mn.getMissionTime()
		if not A1.trace_t or now - A1.trace_t >= 1 then
			A1.trace_t = now
			note(string.format("trace %s: box %.0f m (scan %.0f), avoid-nearest %.0f, v %.0f, thr %.2f",
				tgt.Name, d, scan_dist, closest, me.Physics.Velocity:getMagnitude(), throttle))
		end
	end
	return h, p, throttle
end

-- ---------------------------------------------------------------- go-to and jump-out
-- A1.cfg.go_to = { ship = "<name>", subsystem = "<name>" (optional), within = metres }: once the
-- scan list is done, fly to that point and get inside `within` (Playing Judas: "Enter Docking
-- Bay" = within 200 m of the Lucifer's "fighterbay 1" or "fighterbay 2", sm2-08a.fs2 event "New
-- orders"). A1.cfg.jump_out = true: after go_to is reached (or at once without go_to), press
-- Enter Subspace (Alt-J, END_MISSION) every 2 s until the ship departs, flying away from the
-- nearest hostile meanwhile (the mission returns the warp drive only when its pursuers arrive).
local function go_to_point(me)
	local g = A1.cfg.go_to
	if not g then return nil end
	local s = live_ship(g.ship)
	if not s then return nil end
	if g.subsystem then
		for i = 1, #s do
			local ss = s[i]
			if ss:isValid() and ss.Name:lower() == g.subsystem:lower() then
				-- Two stages for a subsystem on a big hull: first a point 400 m outside it (along
				-- centre -> subsystem), then the subsystem itself. Flying straight at the Lucifer's
				-- fighter bay ran the pilot along its hull into two contacts (Playing Judas run 6).
				local bay = ss.WorldPosition
				local out = (bay - s.Position):getNormalized()
				local entry = bay + out * 400
				if not A1.goal_entry_done then
					if entry:getDistance(me.Position) < 120 then A1.goal_entry_done = true; note("lined up outside " .. ss.Name) end
					return entry, s, true
				end
				return bay, s, false
			end
		end
	end
	return s.Position, s
end

-- A big ship still coming out of its warp portal within 3 km (plus its radius) whose path will
-- pass within half its width + 300 m of us, or nil. Returns the ship and a direction off its
-- line of travel. In
-- Playing Judas the Lucifer (radius 1.5 km) emerged on top of the pilot in two of three runs and
-- one contact destroyed the Dragon's navigation subsystem (5 hp), so it could not jump out.
-- Also any capital ship over 300 m radius under way: the space it sweeps in the next 15 s (its
-- half width + 300 m around its path). In run 4 the warp-in was cleared, then the pilot went back
-- to scanning a cruiser in front of the now-moving Lucifer and one contact killed it.
-- Half the ship's width (the larger of its model box's x and y half-extents), or its radius when
-- the model is unreadable. The radius is half the LENGTH: a 1.1 km "radius" Demon kept the pilot
-- farther from its centreline than its own scan range (an early Playing Judas run, the Eva).
local function half_width(s)
	local m = s.Class.Model
	if m and m:isValid() then
		local a, b = m.BoundingBoxMin, m.BoundingBoxMax
		return math.max(math.abs(a.x), math.abs(b.x), math.abs(a.y), math.abs(b.y))
	end
	return s.Radius
end

local function warp_in_hazard(me)
	for i = 1, #mn.Ships do
		local s = mn.Ships[i]
		local warping = s:isValid() and not is_small(s) and s:isArrivingWarp()
		local v = s:isValid() and s.Physics.Velocity or nil
		local spd = v and v:getMagnitude() or 0
		local underway = s:isValid() and not is_small(s) and s.Radius > 300 and spd > 5
		if (warping or underway) and s.Position:getDistance(me.Position) < 3000 + s.Radius then
			local fwd = (underway and not warping) and v / spd or s.Orientation:getFvec()
			local reach = (underway and not warping) and (s.Radius + spd * 15) or 1e9
			local rel = me.Position - s.Position
			local along = rel:getDotProduct(fwd)
			local side = rel - fwd * along
			-- Under way, only AHEAD of its centre counts: beside or behind a moving destroyer is
			-- where a scan of it is flown from.
			local lo = (underway and not warping) and 0 or -s.Radius
			if along > lo and along < reach and side:getMagnitude() < half_width(s) + 300 then
				local sm = side:getMagnitude()
				local out = (sm > 1) and side / sm or s.Orientation:getUvec()
				return s, out
			end
		end
	end
	return nil
end

-- Direction away from the nearest hostile fighter within 2 km, or our own nose if none.
local function away_from_hostiles(me)
	local best, bd = nil, 2000
	for _, e in ipairs(enemies(me)) do
		local d = e.Position:getDistance(me.Position)
		if d < bd then best, bd = e, d end
	end
	if not best then return me.Orientation:getFvec() end
	return (me.Position - best.Position):getNormalized()
end

-- One frame of flying. Wrapped in pcall by the hook below; any error hands control back.
local function fly()
	local me = hv.Player
	if not (me and me:isValid()) then return end
	me = mn.Ships[me.Name]              -- the ship-derived handle (Target, banks, firePrimary)
	if not (me and me:isValid()) or me:isDying() then return end

	local now = mn.getMissionTime()
	if last_t >= 0 and now < last_t - 0.5 then reset_after_restart() end
	local dt = (last_t >= 0) and (now - last_t) or 0
	last_t = now
	if dt <= 0 then return end          -- paused or the same frame twice
	A1.fq.frames = A1.fq.frames + 1       -- frame-rate control variable for tuning comparisons

	ba.setControlMode(FLIGHT_CONTROL_LUA_FULL)
	ba.setButtonControlMode(BUTTON_CONTROL_LUA_ADDITIVE)
	local ci = ba.getControlInfo()
	ci:clearLuaButtonInfo()             -- button bits are one-shot presses; never leave one held

	-- Queued one-shot presses (operator requests like END_MISSION) and the opening ETS taps,
	-- at most one control per frame, the way a hand presses one key at a time.
	if A1.link_cls ~= me.Class.Name then A1.link_cls, A1.link_tries = me.Class.Name, 0 end   -- a new ship class starts unlinked
	if #A1.press > 0 then
		local name = table.remove(A1.press, 1)
		ci.useButtonControl(-1, name)
		note("pressed " .. name)
	elseif A1.cfg.link_primaries ~= false and #me.PrimaryBanks >= 2 and not me.PrimaryBanks.Linked
			and now > 1 and now - (A1.link_t or -100) > 0.3 and (A1.link_tries or 0) < 2 * #me.PrimaryBanks then
		-- Fire every gun bank at once, as players do in a dogfight: Cycle Primary Weapon Forward
		-- (".") steps bank 1 -> bank 2 -> linked (ship.cpp ship_select_next_primary). The class
		-- study's Myrmidon fired only bank 1 (Prometheus R) and stalemated the Mara, 1 kill in 4
		-- (2026-10-15). Linked is a field of the bank LIST (weaponbanktype), not of one bank;
		-- reading it on a weaponbank aborted the engine that day.
		ci.useButtonControl(-1, "CYCLE_NEXT_PRIMARY")
		A1.link_t, A1.link_tries = now, (A1.link_tries or 0) + 1
		if me.PrimaryBanks.Linked == false and A1.link_tries == 1 then note("linking primary banks") end
	elseif A1.cfg.link_primaries == false and #me.PrimaryBanks >= 2 and me.PrimaryBanks.Linked
			and now - (A1.link_t or -100) > 0.3 then
		-- Comparisons only: one press takes linked back to bank 1 (ship_select_next_primary).
		ci.useButtonControl(-1, "CYCLE_NEXT_PRIMARY")
		A1.link_t = now
		note("unlinking primary banks")
	elseif A1.ets_applied and A1.ets_applied ~= A1.cfg.ets and now > 2 then
		-- The policy changed mid-session (a drill A/B): back to 4/4/4 with Equalize Energy
		-- (Alt-D), then the taps for the new policy on the following frames.
		ci.useButtonControl(-1, "ETS_EQUALIZE")
		note("ETS: equalize for " .. tostring(A1.cfg.ets))
		A1.ets_applied, ets_presses = A1.cfg.ets, 2
	elseif ets_presses > 0 and now > 2 then
		-- ETS policy (A1.cfg.ets): "weapons" taps energy from shields to guns (v1/v2 default,
		-- 4/4/4 -> 2/2/8); "shields" the reverse; "balanced" leaves the default 4/4/4.
		local key = ETS_KEY[A1.cfg.ets]
		if key then
			ci.useButtonControl(-1, key)
			note("ETS: " .. key)
		end
		ets_presses = ets_presses - 1
		if ets_presses == 0 then A1.ets_done, A1.ets_applied = true, A1.cfg.ets end
	end

	learn_gun_offset(me)
	local vel = me.Physics.Velocity
	last_bank = 0                        -- roll comes only from this frame's steering
	local guards = {}
	for _, n in ipairs(GUARD) do local g = live_ship(n); if g then guards[#guards + 1] = g end end
	note_hitters(me, guards, now)

	-- 0w. Wing orders (A1.cfg.order_guard, default on): the first thing every escort guide tells a
	-- player is to order the wing to protect the escorted ship (hard-light FS2 Tactical "Guard
	-- Missions"; nightsolo FS2 guide, missions 16, 18, 33). Done the player's way: target the
	-- ship, press Protect My Target (PROTECT_MESSAGE), then pick "All Fighters" from the comm menu, the
	-- menu item a player selects with a number key (mn.CommItems; the order lands as ORDER_GUARD,
	-- checked 2026-10-15). Repeated when the guarded ship changes or new wingmen arrive.
	local order_target = nil
	if A1.cfg.order_guard ~= false and #guards > 0 and not A1.cfg.stealth then
		local g = guards[1]
		local gsig = g:getSignature()
		local n, holding = 0, 0
		for i = 1, #mn.Ships do
			local s = mn.Ships[i]
			if s:isValid() and s:getSignature() ~= me:getSignature() and not s:isDying()
					and s.Team.Name == me.Team.Name and is_small(s) then
				n = n + 1
				for k = 1, #s.Orders do
					local o = s.Orders[k]
					if o:isValid() and o.Target:isValid() and o.Target:getSignature() == gsig then holding = holding + 1; break end
				end
			end
		end
		-- An order step older than a second belongs to a menu nobody is answering (a restart, or a
		-- menu opened before the wingmen existed, 2026-10-15): drop it, close the menu.
		if wing_order.step == "open" and now - wing_order.t > 1 then
			wing_order.step = nil
			if hu.isCommMenuOpen() then ci.useButtonControl(-1, "SQUADMSG_MENU") end
		end
		-- Verified, not assumed: two seconds after answering, if no wingman holds an order on the
		-- guarded ship, forget the order so it is given again.
		if wing_order.check and now > wing_order.check then
			wing_order.check = nil
			if holding == 0 and n > 0 then
				wing_order.sig = nil
				note("protect order did not reach the wing; ordering again")
			end
		end
		if wing_order.step == "open" then
			if hu.isCommMenuOpen() then
				local picked = false
				for i = 1, #mn.CommItems do
					local c = mn.CommItems[i]
					if c:isValid() and c.Name == "All Fighters" then
						if c.Active then picked = c:selectItem() end
						break
					end
				end
				if not picked then ci.useButtonControl(-1, "SQUADMSG_MENU") end    -- close it again
				note(picked and ("ordered all fighters to protect " .. g.Name) or "no wingman could take the protect order")
				wing_order.step, wing_order.sig, wing_order.n = nil, gsig, n
				if picked then wing_order.check, wing_order.hold = now + 2, now + 0.5 end
			end
		elseif n > 0 and (wing_order.sig ~= gsig or n > wing_order.n) and not hu.isCommMenuOpen() then
			ci.useButtonControl(-1, "PROTECT_MESSAGE")
			wing_order.step, wing_order.t = "open", now
		end
		-- The guarded ship stays targeted from the press until half a second after the menu is
		-- answered: the order is built from the player's target at the moment the engine sends it,
		-- a frame after the pick (hudsquadmsg.cpp:1305, ainfo->target_objnum), and "protect" on the
		-- hostile we were chasing sends nothing (the first in-pilot orders all failed, 2026-10-15).
		if wing_order.step == "open" or (wing_order.hold and now < wing_order.hold) then order_target = g end
		if n < wing_order.n then wing_order.n = n end
	end

	-- 0s. Spare the last ship of a wave (A1.cfg.spare = { wing = "<name prefix>", count = n,
	-- release = "<name prefix>" }): a mission's next wave can be cued on the previous one being
	-- destroyed, and some waves are worse than the ship that holds them back. In FSPort's "The
	-- Hammer and the Anvil" the Vasudan turncoats arrive 150 s after the Durga wave, which arrives
	-- 25 s after the last Rama dies (or 220 s after Rama arrives); with Rama dead by ~360 s they
	-- turned on the containers before the convoy could jump, in every run (sm1-08a.fs2 cues,
	-- rounds 1-5 of 2026-10-15). A player keeps one alive with the comm menu: target it and order
	-- all fighters to "Disable My Target" (sp.disable, re-sent every 3 s), else "Ignore My Target".
	-- The pilot never shoots it (enemies() skips A1.spare_sig) until a ship whose name starts with
	-- `release` appears.
	local sp = A1.cfg.spare
	if sp and not A1.spare_released then
		A1.spare_seen = A1.spare_seen or {}
		local alive, last = 0, nil
		for i = 1, #mn.Ships do
			local o = mn.Ships[i]
			if o:isValid() and o.Name:sub(1, #sp.wing + 1) == sp.wing .. " " then
				A1.spare_seen[o.Name] = true
				if not o:isDying() then alive, last = alive + 1, o end
			end
			if o:isValid() and sp.release and o.Name:sub(1, #sp.release + 1) == sp.release .. " " then
				A1.spare_released = true
				note("spare released: " .. o.Name .. " arrived")
			end
		end
		local seen = 0
		for _ in pairs(A1.spare_seen) do seen = seen + 1 end
		if not A1.spare_sig and alive == 1 and seen >= (sp.count or 1) then
			-- Measured in The Hammer and the Anvil: "Disable" kept the spared Scorpion alive (4 of
			-- 5 runs): the disable goal sets its Protected flag (aigoals.cpp AI_GOAL_DISABLE_SHIP)
			-- and the AI holds fire on a Protected ship whose hull is below the targeted
			-- subsystem's hit points + 50 (aicode.cpp), so it sat at 50% hull, never disabled.
			-- "Ignore" (AI_GOAL_IGNORE_NEW) sets no flag; it died within 25 s in 7 of 7.
			A1.spare_sig, A1.spare_step = last:getSignature(), sp.disable and "disable" or "ignore"
			note("sparing " .. last.Name .. " (last of " .. sp.wing .. ")")
		end
		local spare = nil
		if A1.spare_sig then
			for i = 1, #mn.Ships do
				local o = mn.Ships[i]
				if o:isValid() and o:getSignature() == A1.spare_sig then spare = o; break end
			end
			if not spare then A1.spare_released = true; note("spared ship lost") end
		end
		if spare and not A1.spare_released then
			if A1.spare_step == "disable" and spare.Disabled then A1.spare_step = "ignore"; note(spare.Name .. " disabled") end
			local want = (A1.spare_step == "disable") and "DISABLE_MESSAGE" or (A1.spare_step == "ignore" and "IGNORE_MESSAGE") or nil
			if want and not wing_order.step then
				if A1.spare_menu == nil and not hu.isCommMenuOpen() and now - (A1.spare_t or -100) > 3 then
					me.Target = spare
					ci.useButtonControl(-1, want)
					A1.spare_menu, A1.spare_t = want, now
				elseif A1.spare_menu and hu.isCommMenuOpen() then
					local picked = false
					for i = 1, #mn.CommItems do
						local c = mn.CommItems[i]
						if c:isValid() and c.Name == "All Fighters" then
							if c.Active then picked = c:selectItem() end
							break
						end
					end
					if not picked then ci.useButtonControl(-1, "SQUADMSG_MENU") end
					note((picked and "ordered: " or "could not order: ") .. A1.spare_menu .. " on " .. spare.Name)
					if picked and A1.spare_step == "ignore" then A1.spare_step = "done" end
					A1.spare_menu = nil
				elseif A1.spare_menu and now - A1.spare_t > 1 then
					A1.spare_menu = nil
				end
				if A1.spare_menu or (A1.spare_t and now - A1.spare_t < 0.6) then order_target = spare end
			end
		end
	end

	-- 0. Did the ship we were chasing just die? (Log only; kill credit is the engine's call.)
	if cur_target_name and not live_ship(cur_target_name) then
		note("target gone: " .. cur_target_name)
		cur_target_name = nil
	end

	-- 1. Choose a target (with hysteresis so we do not flip-flop between two close threats).
	local best, best_score, cur, cur_score = nil, 1e12, nil, 1e12
	for _, e in ipairs(enemies(me)) do
		local sc = (#guards > 0) and threat(e, me, guards) or e.Position:getDistance(me.Position)
		if sc < best_score then best, best_score = e, sc end
		if e.Name == cur_target_name then cur, cur_score = e, sc end
	end
	-- Commitment: kill credit goes to whoever does the most damage, and test flight 3 spread its
	-- hits across Aquarius 1/2/3 by switching every 1-3 s (0 kills, 3 assists). Keep a target
	-- for 6 s; switch sooner only if it is gone or the new one is shooting a freighter and ours
	-- is not.
	local tgt = best
	-- Friendly-fire playtest (operator request, 2026-10-12): A1.force_target names any ship,
	-- friendly or not, to attack instead of the threat pick. Still player controls only.
	if A1.force_target then
		local f = live_ship(A1.force_target)
		if f then best, cur, tgt = f, f, f end
	end
	if cur and best and cur.Name ~= best.Name then
		local held = now - target_since
		local best_hits_iota = false
		local cur_hits_iota = false
		for _, g in ipairs(guards) do
			if best.Target and best.Target:isValid() and best.Target:getSignature() == g:getSignature() then best_hits_iota = true end
			if cur.Target and cur.Target:isValid() and cur.Target:getSignature() == g:getSignature() then cur_hits_iota = true end
		end
		if held < 6 and not (best_hits_iota and not cur_hits_iota) then tgt = cur end
		if held >= 6 and cur_score < best_score + 400 then tgt = cur end
	end
	-- Stealth: never engage (firing gives the player away in stealth missions, e.g. "Playing
	-- Judas" checks the fire keys); scanning and evasion still run.
	if A1.cfg.stealth then tgt = nil end
	if tgt and tgt.Name ~= cur_target_name then
		target_since = now
		cur_target_name = tgt.Name
		A1.extend = nil                  -- an extend belongs to the target that caused it
		me.Target = tgt
		lock_time = 0
		trk.h = nil                      -- the old target's angle history would kick the stick
		A1.stats.retargets = A1.stats.retargets + 1
		note("target -> " .. tgt.Name .. string.format(" (%.0f m)", tgt.Position:getDistance(me.Position)))
	elseif not tgt then
		cur_target_name = nil
	end
	-- Keep the ship's own target in step with ours: heat seekers home on it at launch
	-- (weapons.cpp:7013-7019), so a mismatch would launch them at nothing (QA SL-4).
	if tgt then
		local mt = me.Target
		if not (mt and mt:isValid() and mt:getSignature() == tgt:getSignature()) then me.Target = tgt end
	end
	if order_target then me.Target = order_target end

	local heading, pitch, throttle = 0, 0, 0.66
	local bank = me.PrimaryBanks[1]
	local spd = (bank and bank:isValid() and bank.WeaponClass:isValid()) and bank.WeaponClass.Speed or 600
	local range = (bank and bank:isValid() and bank.WeaponClass:isValid())
			and math.min(bank.WeaponClass.Range, spd * bank.WeaponClass.LifeMax) or 700

	local bomb = incoming_bomb(me, guards)
	local blk_m, blk_p = incoming_missile(me, guards)
	if not bomb then A1.on_bomb = false end      -- the intercept log line fires once per bomb run
	local cap = A1.cfg.strike and live_ship(A1.cfg.strike)
	A1.striking = false
	local scan_tgt = A1.cfg.scan and next_scan(me)
	if not scan_tgt then A1.scan_sig = nil end
	if scan_tgt and scan_tgt:getSignature() ~= A1.scan_sig then
		A1.scan_sig = scan_tgt:getSignature()
		note("scan -> " .. scan_tgt.Name)
	end
	local warp_s, warp_out = warp_in_hazard(me)
	-- A1.cfg.ambush = { after = "<ship>", delay = s, target = "<ship>", at = {x, y, z},
	-- face = {x, y, z} (optional: where to point the nose while waiting), first = { names },
	-- max_delay = s (optional, see below) }: from
	-- `delay` seconds after `after` first appears until `target` appears, wait at `at` (beside
	-- the target's coming path, so it comes to us). Playing Judas: the Eva arrives 88 s after the
	-- Genma group and leaves 60 s later (sm2-08a.fs2 arrival and departure cues).
	local amb = A1.cfg.ambush
	local amb_p = nil
	if amb then
		local after = live_ship(amb.after)
		if after and not A1.amb_t then A1.amb_t = now end
		local tgt_live = live_ship(amb.target)
		-- amb.first = { names }: those are scanned before the ambush starts, unless amb.max_delay
		-- runs out first (the ambush pulled the pilot off the Genma cruisers, which then left
		-- unscanned and failed the primary goal, Playing Judas iteration 1).
		local first_done = true
		for _, n in ipairs(amb.first or {}) do
			if not (A1.scanned and A1.scanned[n]) then first_done = false end
		end
		local waited = A1.amb_t and now - A1.amb_t or -1
		local go = waited >= (amb.delay or 0) and (first_done or waited >= (amb.max_delay or 0))
		-- amb.hold_until = metres: keep waiting after the target arrives until its centre is that
		-- close. The Eva arrives at ~300 m/s and parks 6 km away; chasing it from its arrival
		-- left it unscanned (Playing Judas iterations 6-11).
		local held = tgt_live and amb.hold_until
			and tgt_live.Position:getDistance(ba.createVector(amb.at[1], amb.at[2], amb.at[3])) > amb.hold_until
		if A1.amb_t and go and (not tgt_live or held) and not A1.amb_done then
			amb_p = ba.createVector(amb.at[1], amb.at[2], amb.at[3])
		end
		if tgt_live and not held then A1.amb_done = true end
	end
	local goal_p, goal_s, goal_staging = nil, nil, false
	if not scan_tgt and A1.cfg.go_to and not A1.reached then goal_p, goal_s, goal_staging = go_to_point(me) end
	if amb_p and not warp_s then
		-- 2a. Waiting for a ship at its ambush point: fly there, then hold nose toward its entry.
		local d = amb_p:getDistance(me.Position)
		local dir, closest = avoid_dir(me, amb_p)
		local maxv = math.max(me.Physics.VelocityMax.z, 1)
		if closest < A1.cfg.avoid_radius + 150 then
			-- A patrol is coming: leave by the avoidance field and come back when it has gone.
			-- Holding still at the ambush point let an Arjuna fighter come within detection
			-- range and the whole patrol attacked (Playing Judas iteration 5).
			heading, pitch = steer_to(me, me.Position + dir * 600, 3.0)
			throttle = 1.0
		elseif d > 150 then
			heading, pitch = steer_to(me, me.Position + dir * 600, 3.0)
			throttle = clamp(0.25 * d / maxv, 0, 1)
		else
			local face = amb.face and ba.createVector(amb.face[1], amb.face[2], amb.face[3]) or (amb_p + me.Orientation:getFvec())
			heading, pitch = steer_to(me, face, A1.cfg.gain)
			throttle = 0
		end
		if not A1.amb_noted then A1.amb_noted = true; note("waiting for " .. amb.target .. " at its ambush point") end
	elseif warp_s then
		-- 2w. A capital ship is arriving through us: leave its path sideways at full throttle.
		if A1.warp_note ~= warp_s:getSignature() then
			A1.warp_note = warp_s:getSignature()
			note("clearing the path of " .. warp_s.Name)
		end
		heading, pitch = steer_to(me, me.Position + warp_out * 1000, 3.0)
		throttle = 1.0
	elseif (A1.reached or (A1.cfg.jump_out and not A1.cfg.go_to)) and A1.cfg.jump_out then
		-- 2j. Jump out: run from the nearest hostile AND from the ship we just visited, and keep
		-- asking for subspace. Pursuers can launch from that ship (Playing Judas: Rama wing comes
		-- out of the Lucifer's fighter bay); fleeing "away from hostiles" alone defaulted to our
		-- nose, which pointed at the bay, and the pilot died there (most likely to the six
		-- Manticores; not confirmed).
		-- Energy to shields for the run (a player's F5/F6/F7 move): the escape is a chase by six
		-- fighters for the ~30 s until the warp drive returns, and the guns are not the plan.
		if A1.cfg.ets ~= "shields" then A1.cfg.ets = "shields" end
		local flee = away_from_hostiles(me)
		local from = A1.cfg.go_to and live_ship(A1.cfg.go_to.ship)
		if from then flee = (flee + (me.Position - from.Position):getNormalized() * 1.5):getNormalized() end
		heading, pitch = steer_to(me, me.Position + flee * 1000, 3.0)
		throttle = 1.0
		-- Afterburner while fleeing, as a player holds Tab: the pursuers launch from the bay we
		-- just left, and with the stock speed six of them caught the pilot during the jump
		-- (Playing Judas with MediaVPs, 2026-10-15). Lua cannot hold the key, so the physics
		-- flag stands in for it, and only while there is fuel (the engine drains it).
		-- Only while the drive is out: once Alt-J engages it, the engine moves on only when the
		-- speed is within 5% of the warp speed (playercontrol.cpp:1150-1200, PCM_WARPOUT_STAGE1),
		-- and the afterburner held it above that, so the jump never completed (The Hammer and the
		-- Anvil, attempt B3: six minutes of "Subspace drive engaged").
		if not me:canWarp() and me.AfterburnerFuelLeft > 0.1 * me.AfterburnerFuelMax then
			me.Physics.AfterburnerActive = true
			A1.ab_frame = now
		end
		-- Jump only into clear space, as a player points at empty space before Alt-J: once the
		-- drive engages the engine flies the ship straight ahead and fast, and with six Manticores
		-- within 200 m one of them rammed or shot the pilot in the jump, 79% to 1% hull in a
		-- second (Playing Judas with MediaVPs, try 3, "killed by Rama 6"). Clear = no ship within
		-- 800 m inside 25 degrees of the nose.
		local blocker
		local fwd = me.Orientation:getFvec()
		for i = 1, #mn.Ships do
			local o = mn.Ships[i]
			if o:isValid() and o:getSignature() ~= me:getSignature() then
				local rel = o.Position - me.Position
				local d = rel:getMagnitude()
				if d < 800 + o.Radius and d > 1 and rel:getDotProduct(fwd) / d > 0.906 then blocker = o; break end
			end
		end
		if blocker and now - (A1.jump_block_t or -100) > 2 then
			A1.jump_block_t = now
			note(string.format("jump held: %s ahead (%.0f m)", blocker.Name, blocker.Position:getDistance(me.Position)))
		end
		-- One press, then hands off while the drive spins up: a second Alt-J during "Subspace drive
		-- engaged" cancels the jump, and pressing every 2 s kept the pilot in space for six minutes
		-- after the convoy left (The Hammer and the Anvil, recorded attempt B3). While the drive is
		-- out (canWarp false; Playing Judas breaks it until Rama arrives) keep trying every 2 s.
		local wait = me:canWarp() and 15 or 2
		if not blocker and now - (A1.jump_t or -100) > wait then
			A1.jump_t = now
			A1.press[#A1.press + 1] = "END_MISSION"
		end
	elseif goal_p then
		-- 2g. Go to the mission's point (docking bay). Same arrival speed rule as a scan hold.
		local d = goal_p:getDistance(me.Position)
		local within = A1.cfg.go_to.within or 150
		local maxv = math.max(me.Physics.VelocityMax.z, 1)
		local dir = avoid_dir(me, goal_p)
		A1.goal_sig = goal_s:getSignature()
		if d > 600 then
			heading, pitch = steer_to(me, me.Position + dir * 600, 3.0)
		else
			heading, pitch = steer_to(me, goal_p, A1.cfg.gain)
		end
		-- Closing speed capped at 30 m/s inside 600 m: at full speed along the Lucifer's 3 km hull
		-- two contacts killed the pilot on the way to its fighter bay (Playing Judas, 2026-10-15).
		local los = (goal_p - me.Position):getNormalized()
		local close = clamp(0.25 * (d - 0.5 * within), -10, (d > 600) and 75 or 30)
		throttle = clamp((goal_s.Physics.Velocity:getDotProduct(los) + close) / maxv, 0, 1)
		-- Only the real point counts; the lining-up point outside it is a waypoint (the first
		-- version flagged that as "reached" and jumped out 400 m short, Playing Judas run 6).
		if d < within and not goal_staging then
			A1.reached = true
			note(string.format("reached %s%s (%.0f m)", A1.cfg.go_to.ship, A1.cfg.go_to.subsystem and (" " .. A1.cfg.go_to.subsystem) or "", d))
		end
	elseif scan_tgt and (A1.cfg.stealth or not tgt) then
		-- 2r. Recon: scanning outranks fighting in stealth; otherwise fight first, scan when clear.
		local mt = me.Target
		if not (mt and mt:isValid() and mt:getSignature() == scan_tgt:getSignature()) then me.Target = scan_tgt end
		heading, pitch, throttle = scan_frame(me, scan_tgt)
	elseif cap then
		-- 2s. Strike mode: the capital ship is the target (the HUD lock needs it as our target).
		local mt = me.Target
		if not (mt and mt:isValid() and mt:getSignature() == cap:getSignature()) then me.Target = cap end
		heading, pitch, throttle = strike_frame(me, cap, now)
	elseif blk_m and not bomb then
		-- 2b. Missile block (see incoming_missile): fly onto the point on the missile's line,
		-- nose toward the missile so the shields' front quadrant takes it.
		local d = blk_p:getDistance(me.Position)
		if d > 30 then
			heading, pitch = steer_to(me, blk_p, 3.0)
			throttle = 1.0
		else
			heading, pitch = steer_to(me, blk_m.Position, A1.cfg.gain)
			throttle = 0.2
		end
		if A1.blk_sig ~= blk_m:getSignature() then
			A1.blk_sig = blk_m:getSignature()
			A1.stats.blocks = (A1.stats.blocks or 0) + 1
		end
	elseif bomb then
		-- 2a. Escort priority 1: shoot down a bomb heading for a guarded ship. Same lead and
		-- gun-mount aim as against fighters; the cone is wider because a bomb's hit radius is
		-- doubled against lasers. No missiles at bombs.
		local gv = gun_vec(me)
		local brp = bomb.Position - (me.Position + gv)
		local aim = lead_point(bomb.Position, brp, bomb.Physics.Velocity, spd) - gv
		local ang
		heading, pitch, ang = steer_to(me, aim, A1.cfg.gain, true)
		local adist = (aim - me.Position):getMagnitude()
		-- Stand off: a bomb is slow (Unknown Bomb 60 m/s) and kills a fighter it touches (1500
		-- damage); closing on it at 0.6 throttle flew one escort run into a bomb (2026-10-15), and
		-- players warn about bomb blasts at close range (nightsolo FS2 guide, mission 40).
		local bdist = bomb.Position:getDistance(me.Position)
		throttle = (adist > 600) and 1.0 or ((bdist > 300) and 0.5 or 0)
		if bdist < 120 then
			local away = (me.Position - bomb.Position):getNormalized()
			local side = me.Orientation:getUvec() - away * me.Orientation:getUvec():getDotProduct(away)
			heading, pitch = steer_to(me, me.Position + (away + side:getNormalized()) * 500, 3.0)
			throttle, ang = 1.0, math.pi
		end
		local cone = math.atan(math.max(bomb.Radius * 2, 4) / math.max(adist, 1))
		if ang < cone and adist < range * 0.95 then
			A1.stats.primary_shots = A1.stats.primary_shots + (me:firePrimary() or 0)
		end
		if not A1.on_bomb then note(string.format("intercepting a bomb (%.0f m)", adist)) end
		A1.on_bomb = true
		A1.dbg = { ang = math.deg(ang), dist = adist, h = heading, p = pitch }
	elseif tgt and not is_small(tgt) then
		-- 2b. Capital-ship attack (only reachable through A1.force_target). Its bounding radius
		-- is useless for range (the Psamtik's is 1.6 km), so measure to the real hull with a
		-- raycast at its center.
		local hit = tgt:checkRayCollision(me.Position, tgt.Position)
		local surf = hit and hit:getDistance(me.Position) or me.Position:getDistance(tgt.Position)
		-- Hold a firing station 450-650 m off the hull instead of strafing runs. WHY: FSO's
		-- friendly-fire meter (aicode.cpp:16190-16205) subtracts one point per second of any gap
		-- longer than 2 s between hits, and divides each hit by 100 for a ship this big, so the
		-- 20 s break-off legs of runs 1-3 bled the meter dry every time.
		local ang
		heading, pitch, ang = steer_to(me, hit or tgt.Position, 3.0)
		local on_hull = hull_hit(tgt, me.Position, me.Position + me.Orientation:getFvec() * 900)
		-- Range = the nearer of the center ray and the nose ray: the center ray missed the
		-- Psamtik's protruding hull and we ground along it at 6 m (hull 98% -> 92%).
		if on_hull then surf = math.min(surf, on_hull:getDistance(me.Position)) end
		-- Escape leg: the Psamtik maneuvers into a stationary fighter faster than reverse thrust
		-- backs away (ground to 84% hull at 7 m), so inside 300 m turn away at full throttle and
		-- come back at 650 m. A short leg keeps the fire gap, and the meter's decay, to seconds.
		if A1.escaping and surf > 650 then A1.escaping = false; note("back on station") end
		if not A1.escaping and surf < 300 then A1.escaping = true; note(string.format("escape leg (%.0f m)", surf)) end
		if A1.escaping then
			heading, pitch, ang = steer_to(me, me.Position * 2 - (hit or tgt.Position), 3.0)
			throttle = 1.0
			on_hull = nil
		elseif surf > 750 then throttle = 0.8 elseif surf < 500 then throttle = -0.6 else throttle = 0 end
		if on_hull then
			-- firePrimary returns the bolts actually fired (0 while the weapon cycles): count those,
			-- not trigger pulls, so the stat does not change with frame rate (QA XFA-6).
			A1.stats.primary_shots = A1.stats.primary_shots + (me:firePrimary() or 0)
			if surf < 800 and now - last_sec > 1.5 then
				local n = me:fireSecondary()
				last_sec = now
				if n and n > 0 then A1.stats.secondary_shots = A1.stats.secondary_shots + n end
			end
		end
		A1.dbg = { ang = math.deg(ang), dist = surf, h = heading, p = pitch }
	elseif tgt then
		-- 2. Attack: lead-aim, fire inside a tight cone.
		-- v2: solve from the gun mount with the target's absolute velocity, then point the NOSE at
		-- (intercept - mount offset) so the parallel bolts pass through the intercept.
		local gv = gun_vec(me)
		local rp = tgt.Position - (me.Position + gv)
		local dist = rp:getMagnitude()
		local aim, tflight = lead_point(tgt.Position, rp, tgt.Physics.Velocity, spd)
		if A1.cfg.accel_lead then
			aim = lead_point_accel(tgt, me.Position + gv, spd, tflight, target_accel(tgt, now))
		end
		aim = aim - gv
		local ang
		heading, pitch, ang = steer_to(me, aim, A1.cfg.gain, true)
		A1.dbg = { ang = math.deg(ang), dist = dist, h = heading, p = pitch, phi = A1.dbg_phi }
		-- Throttle (v2). Turn RATE does not depend on speed (physics.cpp:497-538), so the turn
		-- RADIUS is speed / rate: slower = tighter. Cases, in order:
		--   * far (> 700 m): full throttle to close;
		--   * target ahead and flying away from us (we are on its tail): hold a speed that closes
		--     gently toward 250 m, i.e. its speed plus a share of the extra distance (the stock AI
		--     matches speed in this geometry, aicode.cpp:7586);
		--   * target well off the nose at close range: a third throttle to turn inside it;
		--   * otherwise two thirds.
		local tvel = tgt.Physics.Velocity
		local tspd = tvel:getMagnitude()
		local maxv = math.max(me.Physics.VelocityMax.z, 1)
		local away = (tspd > 1) and tvel:getDotProduct(rp) / (tspd * math.max(dist, 1)) or 0
		if dist > 700 then
			throttle = 1.0
		elseif ang < math.rad(30) and away > 0.3 then
			throttle = clamp((tspd + 0.4 * (dist - 250)) / maxv, 0.15, 1.0)
		elseif ang > math.rad(60) then
			throttle = 0.33
		else
			throttle = 0.66
		end
		if dist < 800 then
			A1.fq.track = A1.fq.track + 1
			if ang < math.rad(3) then A1.fq.on = A1.fq.on + 1 end
		end
		-- Extend: after nearly colliding with this target, or inside 100 m, stop turning and run
		-- straight out to 600 m before coming back around (a yo-yo, instead of circling at
		-- point-blank range where it can out-turn us).
		if dist < 100 and not A1.extend then A1.extend = { till = now + 6 }; note("extend from " .. tgt.Name); A1.fq.extends = A1.fq.extends + 1 end
		if A1.extend then
			if dist > 600 or now > A1.extend.till then
				A1.extend = nil
			else
				heading, pitch, ang = steer_to(me, me.Position + me.Orientation:getFvec() * 1000, 1.0)
				throttle = 1.0
				ang = math.pi                      -- no firing while extending
			end
		end
		-- Fire when the bolts' predicted miss distance (angle error x distance to the INTERCEPT,
		-- where the bolt actually meets the target; QA CR-6) is inside 60% of the target's radius.
		-- v2 dropped v1's 1.2-degree floor: at 600 m that floor alone allowed a 12 m miss on a
		-- ~10 m fighter. NOTE for comparisons: this gate also decides how many shots are fired,
		-- so part of v2's higher hit percentage is firing more selectively, not only aiming better.
		local adist = (aim - me.Position):getMagnitude()
		local cone = math.atan((tgt.Radius * 0.6) / math.max(adist, 1))
		if ang < cone and adist < range * 0.95 then
			-- firePrimary returns the bolts actually fired (0 while the weapon cycles): count those,
			-- not trigger pulls, so the stat does not change with frame rate (QA XFA-6).
			A1.stats.primary_shots = A1.stats.primary_shots + (me:firePrimary() or 0)
		end
		-- Missiles: decide by the SELECTED secondary bank (weaponbank.Armed, ship_bank.cpp:312);
		-- fireSecondary fires that bank, and the engine switches banks itself when one runs dry
		-- (QA XFA-3). A new selection starts its lock proxy from zero.
		local sname = armed_secondary_name(me)
		if sname ~= last_sname then lock_time = 0; last_sname = sname end
		local launch
		if A1.cfg.heat[sname] then
			launch = heat_launch_ok(me, tgt, ang, away, dist, now)
		else
			-- Aspect seekers: the HUD lock needs the target inside a ~32 degree cone (dot > 0.85)
			-- and in range for the weapon's lock time (hudlock.cpp:539, :690); Lua cannot read
			-- it, so this is a conservative proxy: 12 degrees for 2 s, under 900 m.
			local asp = A1.cfg.aspect[sname] or { lock = 2.0, range = 900 }
			if ang < math.rad(12) and dist < asp.range then lock_time = lock_time + dt else lock_time = math.max(0, lock_time - 2 * dt) end
			launch = lock_time > asp.lock and now - last_sec > 1.5 and dist > 150
		end
		if launch then
			local n = me:fireSecondary()
			last_sec = now
			lock_time = 0
			if n and n > 0 then
				A1.stats.secondary_shots = A1.stats.secondary_shots + n
				missile_check = { t = now, target = tgt.Name }
				note(string.format("missile at %s (%.0f m)", tgt.Name, dist))
			end
		end
	elseif #guards > 0 then
		-- 3. Guard: hold station 500 m off the first live guarded ship, on the side AWAY from
		-- A1.cfg.guard_away if set (SM1-01: the Psamtik, which the freighters dock with, so a fixed
		-- side-offset ended up against its hull; test flight 3 got pinned there).
		local g = guards[1]
		-- A1.cfg.guard_weakest = true: hold station by the guarded ship with the least hull
		-- instead of the first on the list; it is the one the next attack finishes. In "The
		-- Hammer and the Anvil" each wave wore a container down and the Vasudan turncoats,
		-- arriving 150-275 m from the convoy, finished the weakest within 9 s of turning.
		if A1.cfg.guard_weakest then
			for _, c in ipairs(guards) do
				if c.HitpointsLeft / math.max(c.HitpointsMax, 1) < g.HitpointsLeft / math.max(g.HitpointsMax, 1) then g = c end
			end
		end
		-- A1.cfg.suspects = { names }: ships expected to turn on the convoy (a veteran's
		-- knowledge: in "The Hammer and the Anvil" the Vasudan wings Pi and Mu arrive friendly
		-- beside the convoy and turn Neutral ~40 s later, then shred the weakest containers in
		-- seconds). While one is still on our team and within 3 km, hold 250 m behind the one
		-- nearest the station ship, nose on it, guns cold; when it turns, enemies() picks it up
		-- with the pilot already in position. Measured: no help (round 4, 2026-10-15).
		local shadow
		for _, n in ipairs(A1.cfg.suspects or {}) do
			local sp = mn.Ships[n]
			if sp and sp:isValid() and not sp:isDying() and sp.Team.Name == me.Team.Name
					and sp.Position:getDistance(g.Position) < 3000
					and (not shadow or sp.Position:getDistance(g.Position) < shadow.Position:getDistance(g.Position)) then
				shadow = sp
			end
		end
		if shadow then
			local spot2 = shadow.Position - shadow.Orientation:getFvec() * 250
			local d2 = spot2:getDistance(me.Position)
			if d2 > 300 then
				heading, pitch = steer_to(me, spot2, 2.0)
			else
				heading, pitch = steer_to(me, shadow.Position, A1.cfg.gain)
			end
			local maxv = math.max(me.Physics.VelocityMax.z, 1)
			throttle = clamp((shadow.Physics.Velocity:getMagnitude() + 0.25 * (d2 - 50)) / maxv, 0, 1)
			if A1.shadow_sig ~= shadow:getSignature() then
				A1.shadow_sig = shadow:getSignature()
				note("shadowing " .. shadow.Name)
			end
		else
			local away = g.Orientation:getRvec()
			local psa = A1.cfg.guard_away and live_ship(A1.cfg.guard_away)
			if psa and psa.Position:getDistance(g.Position) < 6000 then
				away = (g.Position - psa.Position):getNormalized()
			end
			local spot = g.Position + away * 500
			-- A1.cfg.screen_ahead = metres: hold that far ahead of the guarded ship along its travel
			-- instead. In FSPort's "The Hammer and the Anvil" every Shivan wave arrives ahead of the
			-- convoy (Krishna 1100 m, Durga 1900 m in front of Theta 2, sm1-08a.fs2) and launches
			-- missiles at the cargo within ~10-20 s, which lasers cannot shoot down; a guard 500 m
			-- off the side lost a container to Krishna in all five runs of round 1. Measured: the
		-- screen did not help either (round 2), and alone up front the pilot took heavy damage.
			if A1.cfg.screen_ahead then
				local v = g.Physics.Velocity
				local vm = v:getMagnitude()
				local fwd = (vm > 1) and v / vm or g.Orientation:getFvec()
				spot = g.Position + fwd * A1.cfg.screen_ahead
			end
			local d = spot:getDistance(me.Position)
			heading, pitch = steer_to(me, spot, 2.0)
			throttle = (d > 1500) and 1.0 or (d > 300 and 0.6 or 0.2)
		end
	else
		-- 2i. Nothing to do yet: hold where this idle stretch began (or A1.cfg.idle_at = {x, y, z}),
		-- still keeping clear of the avoid list. Without this the default cruise (0.66 throttle,
		-- straight ahead) carried the pilot into a sentry's detection range while it waited for
		-- the Lucifer (Playing Judas iteration 12).
		if not A1.idle_p or now - (A1.idle_last or -10) > 1 then
			A1.idle_p = A1.cfg.idle_at and ba.createVector(A1.cfg.idle_at[1], A1.cfg.idle_at[2], A1.cfg.idle_at[3])
				or me.Position
		end
		A1.idle_last = now
		local d = A1.idle_p:getDistance(me.Position)
		local goal = (d > 1) and A1.idle_p or (me.Position + me.Orientation:getFvec())
		local dir, closest = avoid_dir(me, goal)
		local maxv = math.max(me.Physics.VelocityMax.z, 1)
		if closest < A1.cfg.avoid_radius + 150 then
			heading, pitch = steer_to(me, me.Position + dir * 600, 3.0)
			throttle = 1.0
			A1.idle_p = nil
		elseif d > 150 then
			heading, pitch = steer_to(me, me.Position + dir * 600, 3.0)
			throttle = clamp(0.25 * d / maxv, 0, 1)
		else
			throttle = 0
		end
	end

	-- 3a. Jink (v2-defence). Skipped when the threat is our target and our guns are on it.
	if A1.cfg.jink and not A1.striking and now >= jink.ready and now >= jink.till then
		local th = gun_threat(me, enemies(me))
		if th then
			-- Trade fire instead of jinking when the threat is our own target and our guns are on
			-- it: the first Insane run with jinking fired 16 shots in a 71 s duel, because an
			-- Insane AI keeps its nose on us almost all the time (2026-10-15).
			local on_it = tgt and th:getSignature() == tgt:getSignature()
				and A1.dbg and A1.dbg.ang and A1.dbg.ang < 8
			if not on_it then
				local o = me.Orientation
				local side = (math.random() < 0.5) and o:getRvec() or o:getUvec()
				if math.random() < 0.5 then side = side * -1 end
				jink.dir = (o:getFvec() * 0.6 + side):getNormalized()
				jink.till = now + 0.8 + 0.4 * math.random()
				-- At least 0.6 s of attack between jinks; none while running for the jump: six
				-- Manticores sat 150-200 m behind and a ram took half the hull in one contact
				-- (Playing Judas with MediaVPs, try 5's per-frame tally; tries 3-4 died the same
				-- way). Try 5, weaving without pause, was the one that got away.
				jink.ready = jink.till + ((A1.reached and A1.cfg.jump_out) and 0 or 0.6)
				A1.fq.jinks = (A1.fq.jinks or 0) + 1
			end
		end
	end
	if jink.dir and now < jink.till then
		heading, pitch = steer_to(me, me.Position + jink.dir * 500, 3.0)
		throttle = 1.0
	end

	-- 3b. Contact reflex: we just touched something (the collision hook below set A1.contact). No
	-- pilot grinds along a hull: back away from the contact point at once and keep at it for 2 s.
	-- While the escape direction is still behind the nose (we are pointed into the hull), reverse
	-- thrust instead of driving forward through it.
	-- The contact is used once and dropped, and its age must not be negative: A1 lives across
	-- missions, and after a restart an old contact's time would otherwise read as just now
	-- (QA SF-7).
	local age = A1.contact and now - A1.contact.t
	if A1.contact and (age < 0 or age >= 0.25) then A1.contact = nil end
	if A1.contact and not (A1.avoid and A1.avoid.reflex) then
		local away, what = A1.contact.dir * -1, A1.contact.other
		A1.contact = nil
		A1.avoid = { till = now + 2.0, dir = away, reflex = true }
		A1.fq.contacts = (A1.fq.contacts or 0) + 1
		note(string.format("contact with %s: escaping", tostring(what)))
	end

	-- 4. Do not ram anything: a COMMITTED sidestep. The old version pointed directly away for a
	-- single frame; the next frame the attack logic turned back, the hazard fired again, and we
	-- yo-yo'd in place at close range (playtests). Now: pick a direction sideways-and-away once,
	-- hold it 1.5 s (3 s for a big ship), and if the hazard was our own target, extend after.
	local hazard = nil              -- declared here so A1.last below can read it (QA SF-10)
	if A1.avoid and now < A1.avoid.till then
		heading, pitch = steer_to(me, me.Position + A1.avoid.dir * 500, 3.0)
		throttle = 1.0
		if A1.avoid.reflex and A1.avoid.dir:getDotProduct(me.Orientation:getFvec()) < 0.3 then
			throttle = -1.0
		end
		-- Near the ship we are flying to, sidestep slowly: full throttle along a 3 km hull
		-- scraped it (Playing Judas run 6).
		if A1.avoid.goal then throttle = 0.3 end
	else
		A1.avoid = nil
		hazard = collision_threat(me, vel)
		-- Too close to the ship being SCANNED: brake straight back with the nose kept on it, as a
		-- player pulls the throttle back. Swerving away for 3 s turned the nose off the target and
		-- restarted the scan every time (Durga 2, Playing Judas iteration 2).
		if hazard and ((A1.scan_sig and hazard:getSignature() == A1.scan_sig)
				or (A1.scan_group and A1.scan_group[hazard:getSignature()])) then
			A1.brake_till = now + 1.2
			hazard = nil
		end
		if hazard then
			local rp = (hazard.Position - me.Position):getNormalized()
			local f = me.Orientation:getFvec()
			local lat = f - rp * f:getDotProduct(rp)              -- our heading, minus the part toward it
			if lat:getMagnitude() < 0.2 then lat = me.Orientation:getUvec() end
			local dir = (lat:getNormalized() - rp * 0.5):getNormalized()
			A1.avoid = { till = now + (is_small(hazard) and 1.5 or 3.0), dir = dir,
				goal = A1.goal_sig and hazard:getSignature() == A1.goal_sig }
			note(string.format("avoid %s (%.0f m, %.0f m/s)", hazard.Name, hazard.Position:getDistance(me.Position), vel:getMagnitude()))
			A1.fq.avoids = A1.fq.avoids + 1
			if tgt and hazard:getSignature() == tgt:getSignature() then A1.extend = { till = now + 6 } end
			heading, pitch = steer_to(me, me.Position + dir * 500, 3.0)
			throttle = A1.avoid.goal and 0.3 or (is_small(hazard) and 1.0 or 0.5)
		end
	end

	-- Close quarters: within 300 m of any big hull, half throttle at most. At 71 m/s through a
	-- cluster of freighters carrying cargo, the pilot rammed one while scanning its neighbour
	-- (Playing Judas iteration 3). Fighters are exempt (they dodge as they go).
	if not warp_s and not A1.reached and throttle > 0.5 then
		for i = 1, #mn.Ships do
			local o = mn.Ships[i]
			if o:isValid() and not is_small(o) and o:getSignature() ~= me:getSignature()
					and o:getSignature() ~= A1.goal_sig
					and o.Position:getDistance(me.Position) < o.Radius + 300
					and hu.getTargetDistance(o) < 300 then
				throttle = 0.5
				break
			end
		end
	end

	if A1.brake_till and now < A1.brake_till and not (A1.avoid and now < A1.avoid.till) then
		throttle = -1.0
	end

	-- Missile audit (observation only): is the missile we just fired homing on anything?
	if missile_check and now - missile_check.t > 0.15 then
		-- Is ANY of our missiles homing on the ship we fired at? (One verdict about the launch, not
		-- about whichever missile happens to be last in the list; QA XFK-3.)
		local verdict = "no missile homing on " .. missile_check.target
		for i = 1, #mn.Weapons do
			local w = mn.Weapons[i]
			if w:isValid() and w.Class:isMissile() and not w.Class:isCountermeasure() and w.Parent:isValid() and w.Parent:getSignature() == me:getSignature() then
				local ho = w.HomingObject
				if ho and ho:isValid() and ho:getBreedName() == "Ship" and ho.Name == missile_check.target then
					verdict = "homing on " .. missile_check.target
					break
				end
			end
		end
		note("missile check: " .. verdict)
		missile_check = nil
	end

	ci.Heading = heading
	ci.Pitch = pitch
	ci.Bank = last_bank
	ci.Forward = throttle
	-- A1.cfg.ab_chase = metres: afterburner toward a fighter target farther than that, while fuel
	-- is above 30% (kept for the dogfight). Escort waves launch missiles at the cargo within
	-- ~10-20 s of arriving (FSPort "The Hammer and the Anvil"), so reaching them sooner looked
	-- like it would matter. Measured: no help (round 2, 2026-10-15); off by default.
	if A1.cfg.ab_chase and tgt and tgt:isValid() and not A1.cfg.stealth and throttle > 0.5
			and tgt.Position:getDistance(me.Position) > A1.cfg.ab_chase
			and me.AfterburnerFuelLeft > 0.3 * me.AfterburnerFuelMax then
		me.Physics.AfterburnerActive = true
		A1.ab_frame = now
	end
	-- Release the afterburner on the first frame nothing asked for it (a key let go).
	if A1.ab_frame and A1.ab_frame ~= now then
		me.Physics.AfterburnerActive = false
		A1.ab_frame = nil
	end
	-- The same trace, after every override: what was actually commanded, and how far our nose
	-- and our travel point from the line to the traced ship (degrees).
	if scan_tgt and A1.cfg.trace == scan_tgt.Name and A1.trace_t2 ~= A1.trace_t then
		A1.trace_t2 = A1.trace_t
		local los = (scan_tgt.Position - me.Position):getNormalized()
		local v = me.Physics.Velocity
		local vm = v:getMagnitude()
		note(string.format("  final: h %.2f p %.2f thr %.2f, nose-off %.0f, travel-off %.0f", heading, pitch, throttle,
			math.deg(math.acos(clamp(me.Orientation:getFvec():getDotProduct(los), -1, 1))),
			vm > 1 and math.deg(math.acos(clamp((v / vm):getDotProduct(los), -1, 1))) or -1))
	end

	-- 5. Defensive buttons, rate-limited like a human tapping keys.
	--    Missile warning: a homing weapon locked on us within 300 m -> countermeasure. A flare is
	--    tested against missiles only in the 2 frames after it is launched (weapons.cpp:7358-7360,
	--    object.cpp:1584-1589) and only within its effective radius (300 m default,
	--    cmeasure/cmeasure.h:24), so one dropped while the missile is farther out is wasted.
	--    Each flare is an independent roll: drop every 0.5 s while the missile is close.
	if now - last_cm > 0.5 then
		for i = 1, #mn.Weapons do
			local w = mn.Weapons[i]
			if w:isValid() then
				local ho = w.HomingObject
				if ho and ho:isValid() and ho:getSignature() == me:getSignature() and w.Position:getDistance(me.Position) < 300 then
					ci.useButtonControl(-1, "LAUNCH_COUNTERMEASURE")
					last_cm = now
					A1.stats.cms = A1.stats.cms + 1
					note("countermeasure")
					break
				end
			end
		end
	end
	if now - last_eq > 2.0 then            -- keep shields spread evenly (the Q key)
		ci.useButtonControl(-1, "SHIELD_EQUALIZE")
		last_eq = now
	end
	A1.last = { t = now, target = cur_target_name, throttle = throttle, hazard = hazard and hazard.Name or nil }
end

A1.fly = fly

-- Install the hook once; later loads only flip A1.enabled back on.
-- Collision sense: the player's own contacts (not weapon hits, which fire this hook too, and once
-- per frame although the engine reports each ship-ship contact twice). Same filter as the harness
-- bridge; hv[...] of a variable this hook did not set is nil, so each read is safe.
-- Record the player's contact for the reflex in fly(). A global, so loading this file again
-- replaces it like A1.fly (QA SF-8). A contact closer than 1 cm has no usable direction and is
-- skipped rather than invented (QA SF-5d).
function A1.on_contact()
	if hv.Weapon ~= nil or hv.Beam ~= nil then return end
	local me = hv.Player
	if not (me and me:isValid()) then return end
	local sig, a, b = me:getSignature(), hv.Ship, hv.ShipB
	local other
	if a and a:isValid() and a:getSignature() == sig then other = b
	elseif b and b:isValid() and b:getSignature() == sig then other = a
	else return end
	local point = hv.Hitpos or (other and other:isValid() and other.Position)
	if not point then return end
	local d = point - me.Position
	if d:getMagnitude() < 0.01 then return end
	local valid = other and other:isValid()
	A1.contact = { t = mn.getMissionTime(), dir = d:getNormalized(),
	               other = valid and other.Name or (hv.Asteroid and "asteroid") or "debris" }
end

if not A1.coll_hooked then
	engine.addHook("On Ship Collision", function()
		local ok, err = pcall(A1.on_contact)
		if not ok then
			-- Kept and counted, like the frame hook's A1.error (QA SF-6).
			A1.contact = nil
			A1.contact_error = tostring(err)
			A1.contact_errors = (A1.contact_errors or 0) + 1
		end
	end)
	A1.coll_hooked = true
end

if not A1.hooked then
	engine.addHook("On Frame", function()
		if not A1.enabled then
			-- Switched off from outside: hand the stick and buttons back exactly once.
			if A1.active then
				ba.setControlMode(FLIGHT_CONTROL_NORMAL)
				ba.setButtonControlMode(BUTTON_CONTROL_NORMAL)
				A1.active = false
			end
			return
		end
		A1.active = true
		if not (gr and ba.getCurrentGameState().Name == "GS_STATE_GAME_PLAY") then return end
		-- Call through the global so re-running this file swaps in the new fly() without
		-- stacking a second hook.
		local ok, err = pcall(A1.fly)
		if not ok then
			A1.enabled = false
			A1.error = tostring(err)
			ba.setControlMode(FLIGHT_CONTROL_NORMAL)
			ba.setButtonControlMode(BUTTON_CONTROL_NORMAL)
		end
	end)
	A1.hooked = true
end
note("controller loaded")
