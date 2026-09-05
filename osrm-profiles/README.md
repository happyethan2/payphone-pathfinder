# OSRM profiles

Australia drives on the left. Out of the box, OSRM does not route as though it does.

## The problem

OSRM prices a turn as `angle²`, scaled by a `turn_bias` that makes one direction
cheaper than the other — the direction that does not cross oncoming traffic. Which
direction that is depends on which side of the road you drive on.

Stock `bicycle.lua` applies its `turn_bias = 1.4` **unconditionally**: turns with
`angle >= 0` (right turns, per OSRM's convention that positive = right) have their
penalty divided by the bias, left turns multiplied by it. That is correct for
right-hand-traffic countries and exactly backwards here — it makes the right turn,
the one that *does* cross oncoming traffic, cheaper for cyclists.

For a 90-degree turn (`turn_penalty = 6`, `turn_bias = 1.4`):

| turn | stock | corrected |
|---|---|---|
| left turn in Australia | 8.40 s | **4.29 s** |
| right turn in Australia | 4.29 s | **8.40 s** |

## Two things were needed, and the second is easy to miss

`car.lua` already gets this right:

```lua
local turn_bias = turn.is_left_hand_driving and 1. / profile.turn_bias or profile.turn_bias
```

**Copying that line into the bicycle profile on its own does nothing.**
`turn.is_left_hand_driving` is populated by `WayHandlers.driving_side`, and stock
`bicycle.lua` never calls that handler — `car.lua` does, `bicycle.lua` simply omits
it. So the flag is `nil` on every bicycle turn and the ternary always takes the
right-hand-traffic branch. Nothing errors; routes still come back; they are just
still wrong. `bicycle-lht.lua` therefore does **both**: runs the handler, and
re-prices the turn.

`foot.lua` needs no change — it applies no left/right turn bias at all, which is
correct for pedestrians.

## And the extract flag

`WayHandlers.driving_side` reads `way:get_location_tag('driving_side')`, which only
resolves if `osrm-extract` is handed the polygons that define those regions. They
ship inside the image:

```
--location-dependent-data /usr/local/share/osrm/data/driving_side.geojson
```

Without that flag the lookup returns nil and everything falls through to
`profile.properties.left_hand_driving`, which stock `bicycle.lua` never defines and
`car.lua` defaults to `false`. **So this flag alone is what fixes car routing**, which
was mis-biased for the same reason despite having correct profile logic.

A successful extract logs:

```
[info] Parsed 11 location-dependent features with 11 GeoJSON polygons
```

If that line is absent, driving side was not applied. Verified: those polygons put
Adelaide, Melbourne, Sydney and Perth inside a `driving_side=left` region, with
London inside and Paris outside as controls.

## Why a wrapper instead of a patched copy

`bicycle-lht.lua` loads the stock profile with `require("bicycle")` and overrides two
functions, rather than vendoring a modified copy of the ~800-line original. Upstream
speed tables, access rules and tag handling stay inherited, so an OSRM image bump
does not silently revert them or require re-patching.

The trade-off: `process_turn` is a full replacement, so genuine upstream changes to
*that one function* would need porting. It is written as a replacement rather than a
correction applied afterwards because the stock version folds the u-turn and
traffic-signal penalties into the same `turn.duration`, and those must not be
re-biased.

The file is mounted **into** `/opt`, not over it:

```
-v "$(pwd)/osrm-profiles/bicycle-lht.lua:/opt/bicycle-lht.lua:ro"
```

Mounting a directory over `/opt` would hide the image's `lib/` and stock
`bicycle.lua`, which this wrapper depends on.
