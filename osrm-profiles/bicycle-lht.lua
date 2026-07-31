-- Bicycle profile with driving-side-aware turn costs.
--
-- Upstream's stock bicycle.lua applies its turn_bias unconditionally: turns with
-- angle >= 0 (right turns, per OSRM's convention that positive = right) have their
-- penalty divided by the bias, and left turns multiplied by it. That hardcodes a
-- right-hand-traffic assumption -- the turn that does NOT cross oncoming traffic is
-- the cheaper one -- and applies it everywhere in the world.
--
-- Australia drives on the left, so stock gets it exactly backwards here: it makes
-- the right turn (the one that does cross oncoming traffic) cheaper for cyclists.
--
-- The same image's car.lua already handles this correctly via
-- turn.is_left_hand_driving. The bicycle profile does not -- and, less obviously,
-- it never runs WayHandlers.driving_side either, so that flag is nil on every
-- bicycle turn. Copying car.lua's ternary across on its own would therefore be a
-- silent no-op: nothing errors, routes still return, they are just still biased the
-- wrong way. Both halves below are required.
--
-- Layered onto the stock profile rather than vendoring a copy of it, so upstream
-- speed tables, tag handling and access rules stay inherited.
--
-- REQUIRES osrm-extract --location-dependent-data with the driving_side.geojson
-- shipped in the image at /usr/local/share/osrm/data/driving_side.geojson.
-- Without it, way:get_location_tag('driving_side') never resolves. A good extract
-- logs "Parsed 11 location-dependent features with 11 GeoJSON polygons".
--
-- This file must be mounted INTO the image's /opt (not over it) so that it sits
-- alongside the stock bicycle.lua and lib/ that it loads:
--   -v "$(pwd)/osrm-profiles/bicycle-lht.lua:/opt/bicycle-lht.lua:ro"

local WayHandlers = require("lib/way_handlers")

-- Resolves to the image's /opt/bicycle.lua, since osrm-extract puts the profile's
-- own directory on package.path -- the same mechanism the stock profiles rely on
-- for their require('lib/...') calls.
local profile = require("bicycle")

local stock_process_way = profile.process_way

function profile.process_way(p, way, result, relations)
  stock_process_way(p, way, result, relations)

  -- Sets result.is_left_hand_driving from an explicit driving_side=* tag, falling
  -- back to location-dependent data, then to profile.properties.left_hand_driving.
  -- The fourth argument is the tag cache, which this handler does not read.
  WayHandlers.driving_side(p, way, result, nil)
end

-- Mirrors stock process_turn with the bias flipped for left-hand-traffic regions.
-- Written as a full replacement rather than a correction applied after the stock
-- function, because that function folds the u-turn and traffic-signal penalties
-- into the same turn.duration and they must not be re-biased.
function profile.process_turn(p, turn)
  local turn_bias = turn.is_left_hand_driving and (1. / p.turn_bias) or p.turn_bias

  local normalized_angle = turn.angle / 90.0
  if normalized_angle >= 0.0 then
    turn.duration = normalized_angle * normalized_angle * p.turn_penalty / turn_bias
  else
    turn.duration = normalized_angle * normalized_angle * p.turn_penalty * turn_bias
  end

  if turn.is_u_turn then
    turn.duration = turn.duration + p.properties.u_turn_penalty
  end

  if turn.has_traffic_light then
    turn.duration = turn.duration + p.properties.traffic_signal_penalty
  end

  if p.properties.weight_name == 'cyclability' then
    turn.weight = turn.duration
  end

  if turn.source_mode == mode.cycling and turn.target_mode ~= mode.cycling then
    turn.weight = turn.weight + p.properties.mode_change_penalty
  end
end

return profile
