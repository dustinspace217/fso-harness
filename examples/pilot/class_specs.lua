-- class_specs.lua: one line per fighter or bomber class the player may fly in the loaded data,
-- with the numbers that decide how it should be flown. Load with the harness `lua` tool:
--   return loadfile("<abs path>/class_specs.lua")()
-- Every field read here exists in this build's scripting.lua (shipclass: VelocityMax,
-- AfterburnerVelocityMax, RotationTime, RotationalVelocityDamping, HitpointsMax,
-- ShieldHitpointsMax, numPrimaryBanks, numSecondaryBanks, isPlayerAllowed, Type).
-- Columns: max speed / afterburner speed (m/s); seconds for a full 360 in pitch / yaw / roll;
-- turn lag ($Rotdamp, s); turn radius at max speed (speed x yaw time / 2 pi, m); hull; shields;
-- gun and missile banks.
local out = { "class | type | speed/ab | rot p/y/r (s) | lag | radius m | hull | shield | banks" }
for i = 1, #tb.ShipClasses do
	local c = tb.ShipClasses[i]
	if c:isValid() and c.Type:isValid() and (c.Type.Name == "Fighter" or c.Type.Name == "Bomber")
			and c:isPlayerAllowed() then
		local v, ab, rt = c.VelocityMax.z, c.AfterburnerVelocityMax.z, c.RotationTime
		local radius = (rt.y > 0) and v * rt.y / (2 * math.pi) or 0
		out[#out + 1] = string.format("%s | %s | %.0f/%.0f | %.1f/%.1f/%.1f | %.2f | %.0f | %.0f | %.0f | %d+%d",
			c.Name, c.Type.Name, v, ab, rt.x, rt.y, rt.z, c.RotationalVelocityDamping, radius,
			c.HitpointsMax, c.ShieldHitpointsMax, c.numPrimaryBanks, c.numSecondaryBanks)
	end
end
return table.concat(out, "\n")
