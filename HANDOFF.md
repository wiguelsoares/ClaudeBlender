# Handoff: Dildo Asset Generator addon

**Style note for the assistant reading this:** be terse. Short sentences,
no recaps, no filler. Answer/act, don't narrate.

## Where things stand

- Repo: `wiguelsoares/ClaudeBlender`, branch `master`, clean, pushed.
- File: `Tools/dildo_asset_generator_addon.py` (single-file Blender addon).
- Live copy: `C:\Users\wigue\AppData\Roaming\Blender Foundation\Blender\5.1\scripts\addons\dildo_asset_generator_addon.py`
  — after editing the repo file, `cp` it here and reload via
  `bpy.ops.preferences.addon_disable/addon_enable(module='dildo_asset_generator_addon')`.
- Blender is connected via `mcp__blender__execute_blender_code` /
  `mcp__blender__get_viewport_screenshot`.
- Last 3 commits (most recent first):
  - `3de6042` Tighten normal-map bake cage to eliminate bright-green artifacts
  - `50ef7c1` Move veins to bake-time; fix diamond pole cap and ball-seam gap bugs
  - `9bc4a0b` Add normal map baking (512/2048) via Cycles Selected-to-Active

## Session 2026-07-02: bake fixes (veins flat / short of shaft / missing on knot)

Root causes found and fixed in `bake_normal_map()` / `ASSETGEN_OT_bake_normal_map` /
`build_vein()`:

- **Geometry mismatch (the big one):** the bake operator independently called
  `randomise(cfg, rng)` with a fresh `random.Random(cfg["seed"])`, which -- especially
  with the default "Random Seed Each Run" -- drew a *different* shaft/head/knot shape
  and a different `has_knot` coin flip than the highpoly actually in the scene. Veins
  got embedded against the wrong radii: sunk inside the real surface (bakes flat), used
  the plain shaft radius instead of the knot's bigger radius when the re-rolled
  `has_knot` came out False (veins vanish crossing the knot), and drifted off the real
  `shaft_length` at the ends. Fix: `generate()` now stores the resolved params + knot
  decision as custom props on the highpoly (`assetgen_gen_params` JSON,
  `assetgen_has_knot`); the bake operator restores just the geometry-critical keys
  (`GEOMETRY_PARAM_KEYS`) from there instead of re-rolling them, while still pulling
  vein-slider fields (girth/bend/length/segments/count) from the live UI settings.
- **Rig-before-bake:** `rig_chance` defaults to 0.9, so a rig with a random pose bend is
  built automatically on most `Generate Asset` clicks. The Armature modifier defaults to
  POSE position, so Selected-to-Active was ray-casting the *bent* retopo against the
  *straight* highpoly -- worse toward the tip, reads as flat/missing coverage. Fix:
  `bake_normal_map()` now force-sets any armature driving the highpoly/retopo to
  `pose_position = 'REST'` for the duration of the bake and restores it after,
  regardless of build order.
- **Vein span:** `start_z` was `uniform(0, shaft_length - span)`, so a vein could float
  short of *both* ends when `span < shaft_length` (the common case at the default
  0.85-1.0 length range). Fixed to anchor at `z=0` or `z=shaft_length-span` (50/50)
  instead, so every vein always reaches one end.
- **Cage extrusion floor too small:** the auto-growing cage loop always terminated at
  its first candidate (`0.0005 * diag`) since the ray-miss check passes almost
  immediately -- but an empirical sweep (multiple seeds, veins+knot forced on) showed
  that cage under-captures relief by 15-40% (p99 texel deviation) vs. `0.002 * diag`,
  while bright-artifact fraction at 0.002 stayed under the existing 0.05% warning
  threshold on every seed tested. New floor: `0.002`.

Verified: 0/25 boundary-edge failures on the regression sweep (RANDOM everything,
rig included); direct inspection of the real bake-time vein geometry (not just the
baked map) confirms veins now run from just below the head join down to near the base
and visibly wrap the knot bulge; rig+bake interaction (`pose_position` REST/restore)
exercised without error.

**For testing purposes, always bake at 512x512** (`s.bake_resolution = '512'`) --
faster iteration, default UI resolution is still 2048.

## What's done (verified)

- Vertical shaft seam always reaches the cup boundary seam.
- Suction cup tip converges to a single pole vertex.
- Ball bottoms: real boolean intersect + triangulation, no n-gons.
- Diamond pole cap: fixed a vertex-drop bug (ring size not a multiple of
  4, e.g. after a head crevice cut) and an angle-sort adjacency bug
  (broke on non-convex/dented loops). Generic closed-loop bridge now
  handles any boundary size.
- Low-poly ball-boolean seam gap (no-cup case): fixed a coplanar
  trim-plane conflict between the ball's flush base cut and the grid's
  flat base cap.
- Normal Map UI: 512/2048 resolution picker, Bake Normal Map
  button/panel.
- Veins are bake-only now — zero vein geometry on generated assets;
  they only show up once you bake.
- Bake artifact fix: tightened cage_extrusion/max_ray_distance defaults
  (grazing-angle hits at vein grooves were baking bright-green
  garbage; bigger cage made it worse, not better).
- Regression: 0% boundary-edge failures across ~150+ randomized
  generations (all balls/cup combos); bake miss-rate 0%, worst-case
  bright-artifact fraction ~0.0005% across 12 configs.

## What's left / open

- Nothing outstanding from the original 6-part request — all done.
- Not stress-tested: bake operator with `asset_count > 1` (currently
  gated to `asset_count == 1` by design, not a bug).
- No aesthetic/manual review of baked vein maps beyond the automated
  miss/artifact checks + a couple of screenshots this session.
- If new artifacts turn up in bakes: check
  `_detect_bake_bright_artifacts()` output first (it logs a warning,
  doesn't fail the bake) before assuming it's a ray-miss.

## Useful test snippets

Regression sweep (boundary-edge check):
```python
import bpy, bmesh
s = bpy.context.scene.assetgen_settings
s.head_mode='RANDOM'; s.crevice_mode='RANDOM'; s.balls_mode='RANDOM'
s.cup_mode='RANDOM'; s.knot_mode='RANDOM'; s.veins_mode='RANDOM'; s.curve_mode='RANDOM'
bad = []
for i in range(40):
    bpy.ops.assetgen.generate()
    obj = bpy.data.objects["GameAsset_Retopo"]
    bm = bmesh.new(); bm.from_mesh(obj.data); bm.edges.ensure_lookup_table()
    n = len([e for e in bm.edges if len(e.link_faces) == 1])
    bm.free()
    if n: bad.append((i, n))
print(len(bad), "/", 40, bad)
```

Bake + material preview:
```python
bpy.ops.assetgen.generate()
bpy.ops.assetgen.bake_normal_map()
# then wire s.last_bake_image_name into a Principled BSDF via a
# ShaderNodeNormalMap on GameAsset_Retopo, screenshot with
# mcp__blender__get_viewport_screenshot
```
