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
- Last commit: `b41aad8` Remove the veins feature entirely.

## Session 2026-07-02: veins removed

Veins (random bevelled-curve splines boolean-unioned onto the shaft, baked into a
normal map) never fully stabilized across several rounds of fixes — geometry
mismatches between bake-time and the real mesh, coverage gaps, one-sided angular
clustering, flat-looking relief, finicky cage-extrusion tuning. Removed entirely
rather than continuing to chase it. Gone: all `vein_*`/`veins_*` config, UI
properties and panel, `build_vein`/`_vein_wobble`/`_vein_girth_wobble`/
`build_bake_highpoly_with_veins`/`local_surface_radius`, `GEOMETRY_PARAM_KEYS` and
the `assetgen_gen_params`/`assetgen_has_knot` custom-property stashing (all existed
solely to match vein geometry to the real mesh at bake time).

**Kept:** normal-map baking itself (`bake_normal_map()`, "Normal Map" panel) — still
useful for capturing fine highpoly detail (sulcus groove, crevice slit) that the
coarse retopo grid loses. Now bakes the plain highpoly directly, no extra geometry
step. The combined convenience button survived, renamed:
`ASSETGEN_OT_bake_and_setup_material` / `assetgen.bake_and_setup_material` ("Bake
Normal Map & Setup Material") — bakes, wires the image into a persistent
`GameAsset_NormalPreview` material (Image Texture → Normal Map → Principled BSDF),
assigns it to the retopo, switches every 3D viewport to Material Preview shading.
Sits in the main panel just above "Optional Parts". Note: calling a nested operator
via `bpy.ops` that reports an ERROR raises `RuntimeError` rather than returning
`{'CANCELLED'}` — this operator wraps the delegated `bake_normal_map` call in
try/except for that reason; keep that in mind if wiring more operators together.

**Also fixed:** `rig_mode`/`curve_mode` had been left altered in the live scene from
earlier vein-debugging test scripts (`rig_mode` stuck on `NEVER`), which is why rig
and the baked curve appeared to have "disappeared" for the user — not a code bug.
Reset in the live scene; defaults in code are `rig_mode='ALWAYS'`,
`curve_mode='NEVER'`.

Verified: clean addon reload (no syntax/import errors), 25-asset randomized
regression sweep (0 boundary-edge failures, rig/curve appearing normally), renamed
bake+material button working end to end.

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
- Retopo UV islands: up to 4 clean islands (balls / base cap or cup /
  head / shaft body+knot), split with exact ring seams at the shaft/head
  join and the flat-base plane rather than heuristics. Bare-shaft
  (no head) case keeps a pinch-avoidance margin since its own tip pole
  has nowhere else to go.
- Normal Map UI: 512/2048 resolution picker, "Bake Normal Map" button,
  plus the one-click "Bake Normal Map & Setup Material" button.
- Rig-vs-bake pose mismatch: `bake_normal_map()` force-sets any armature
  driving the highpoly/retopo to `pose_position = 'REST'` for the
  duration of the bake and restores it after, regardless of build order.
- Regression: 0% boundary-edge failures across 150+ randomized
  generations (all part combos, rig/curve included).

## What's left / open

- Nothing outstanding from the original request — all done, veins removed per
  explicit ask.
- Not stress-tested: bake operator with `asset_count > 1` (currently
  gated to `asset_count == 1` by design, not a bug).
- If new bake artifacts turn up: check `_detect_bake_bright_artifacts()`
  output first (it logs a warning, doesn't fail the bake) before
  assuming it's a ray-miss.

## Useful test snippets

Regression sweep (boundary-edge check):
```python
import bpy, bmesh
s = bpy.context.scene.assetgen_settings
s.head_mode='RANDOM'; s.crevice_mode='RANDOM'; s.balls_mode='RANDOM'
s.cup_mode='RANDOM'; s.knot_mode='RANDOM'; s.curve_mode='RANDOM'; s.rig_mode='RANDOM'
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

Bake + material preview (one call now):
```python
bpy.ops.assetgen.generate()
bpy.ops.assetgen.bake_and_setup_material()
# viewport is already Material Preview afterward; screenshot with
# mcp__blender__get_viewport_screenshot
```
