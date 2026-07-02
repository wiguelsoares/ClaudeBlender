# Handoff: Dilbo Asset Generator addon

**Style note for the assistant reading this:** be terse. Short sentences,
no recaps, no filler. Answer/act, don't narrate.

## Where things stand

- Repo: `wiguelsoares/ClaudeBlender`, branch `master`, clean, pushed.
- File: `Tools/dilbo_asset_generator_addon.py` (single-file Blender addon).
- Live copy: `C:\Users\wigue\AppData\Roaming\Blender Foundation\Blender\5.1\scripts\addons\dilbo_asset_generator_addon.py`
  — after editing the repo file, `cp` it here and reload via
  `bpy.ops.preferences.addon_disable/addon_enable(module='dilbo_asset_generator_addon')`.
- Blender is connected via `mcp__blender__execute_blender_code` /
  `mcp__blender__get_viewport_screenshot`.
- Last commit: `d9c6d79` Fix flat-base UV distortion and reduce ball/shaft seam raggedness.

## Session 2026-07-02 (part 4): ball connection rebuilt as a clean bridge

User manually cleaned up one generated asset in Blender directly (marked new
UV seams, scaled the flat base exactly flat, fixed some geometry) and asked
for it to be replicated automatically. Verified their edit first: it split
the mesh into 4 seam-bounded islands (rest/head/ball/bottom, up from the
previous ball+bottom-merged state) where "ball" is now a clean, largely
smooth loop bordering "rest" instead of the raw jagged boolean seam from
part 3.

Replicated with `_clean_up_ball_connection` (new function, called in
`retopologize()` right after `cap_ends_with_quads`): delete the messy
boolean-intersection band (faces within 0.9-1.3x ball_r of a ball centre,
excluding flat/head faces), then bridge the two resulting clean loops with
the existing `_bridge_closed_loops` helper (the same one the diamond pole
cap already uses).

A fixed dilation amount before deleting isn't reliable: near x=0 (where the
two overlapping balls' regions meet), "distance to the *nearest* ball
centre" has a kink, and a thin surviving spur there splits the hole into
several small dead-end sub-loops instead of one clean disk -- confirmed via
a 25-seed sweep where a fixed 2-ring dilation left roughly half in that
broken state. Fixed by retrying with a growing dilation (1..8 rings) on a
fresh scratch copy each attempt until exactly 2 clean loops appear and the
bridge produces a fully closed, manifold result -- 0/25 failures once the
retry was in place (typically converges in 1-4 rings). A separate rare
failure mode (`bridge_closed_loops` raising on a near-degenerate loop
pairing) is now also caught and treated as "this attempt didn't work, try
more rings," not a crash.

Also added `_flatten_bottom_cap`: snaps the flat base island's vertices to
exactly z=0 (matching the user's manual "scale to truly flat"), skipped
when a suction cup is present since that island is the cup's own concave
underside.

Verified: 40-asset `balls_mode='ALWAYS'` regression sweep with baking
enabled -- 0 generation errors, 0 non-manifold/boundary failures, 0 rig
failures, 0 bake failures, ~2.6s/asset. Visually confirmed clean checker
pattern across the ball dome and flat base with no pinwheel distortion or
visible jagged seam at the connection.

## Session 2026-07-02 (part 3): ball classification fix

**Fix: flat base part had wild UV distortion; ball/shaft connection seam was
jagged.** Root cause (both symptoms, one cause): `_classify_retopo_faces`
classified "ball" faces by a blunt proximity test (within `ball_r * 1.25` of
a ball centre) instead of checking whether a face was actually *on* the
ball's curved surface. A ball's own flush-trimmed flat bottom disc (see
`build_balls`) sits well within that radius even though it's flat, not
curved — so it got the equirectangular ball projection, producing wild
swirl distortion (confirmed via UV-lane analysis: flat faces spanned both
the bottom lane and the ball lanes before the fix). The same blunt test also
let individual small, irregularly-shaped boolean-solver triangles flip in
and out of the "ball" group somewhat arbitrarily near the connection,
zigzagging the UV seam there.

Fixed `_classify_retopo_faces` with two precise geometric tests, checked in
order: `is_flat_base` (near-zero z-span across a face's own verts, near the
z=0 base plane) routes flat faces to `bottom_faces` regardless of proximity
to a ball centre; `is_ball_surface` (face centre within `ball_r * 0.12` of
the *true* ball radius from centre, not a blanket bounding sphere) routes
only genuinely curved dome faces to `ball_faces`. Also raised
`_merge_small_fragments`'s threshold to 20 faces when balls are present
(was 4), absorbing more of the remaining small alternating pockets at the
irregular seam into their dominant neighbour.

Verified: UV-lane analysis confirms bottom/ball/head/rest no longer overlap
(bottom 7.27–8.73, ball 10.27–15.02, head/rest -1.39–1.39, all previously
overlapping up to 16.7). Checker pattern on the flat base now reads as
clean, correctly-aligned squares (screenshot-verified). 20-asset
`balls_mode='ALWAYS'` regression sweep: 0 boundary/rig/bake failures.

**Not fully fixed:** the ball/shaft connection seam is measurably smoother
(boundary edge count ~89 → ~58 on a fixed test asset) but still visibly
jagged, not a clean even loop. Confirmed by testing that this is *not* a
resolution or classification-tuning problem: swept `retopo_grid_ball_segments`
32 → 96 with barely any change (58 → 54 boundary edges) while face count
nearly tripled, and a `bmesh.ops.beautify_fill` repass over the messy
triangles didn't help either (68, slightly worse). The EXACT boolean
solver's own triangulation of the sphere/cylinder intersection curve is
genuinely irregular by construction — fixing that properly needs deleting
the messy connection band and rebuilding it with a purpose-made bridge
between the two clean loops on either side, not a classification tweak.
Prototyped that live (delete band + `bmesh.ops.bridge_loops` on the
resulting boundary): left the mesh with unclosed holes on the first pass
rather than a clean bridge, so it needs its own dedicated pass with real
verification (manifold + rig-solver-safety checks) before it's safe to
ship — self-intersecting/non-manifold geometry from a botched bridge would
hit the exact same "breaks the rig solver across the whole mesh" failure
mode the diamond pole cap saga (see below) already burned several
iterations on.

## Session 2026-07-02 (part 2): big batch — 4 fixes + 2 features

**Fix: rig not following.** Root cause: `build_diamond_pole_cap`'s BVH-snap of
interior ring points could snap adjacent points onto crossing/overlapping surface
locations — for a tight dome tip (ambiguous nearest-surface-point near the pole)
or for the flat base pole dipping outward through nearby ball geometry. Result is
self-intersecting geometry that's still locally 2-manifold, so it passes every
boundary-edge/manifold check but reliably makes Blender's automatic rig weight
solver fail *outright* (every vertex zero-weighted, not just nearby ones — this is
what "doesn't follow the rig" actually was). Fixed the pole cap to a pure
radial-fan lerp (provably non-self-intersecting for a star-shaped boundary, no BVH
snap at all) and stopped dipping the flat base pole. A separate, deeper fragility
remains for some large-ball-relative-to-shaft combos even with clean geometry
(root cause not fully pinned after extensive testing — see `_assign_fallback_weights`
docstring). Defense in depth: `build_rig` now assigns any vertex the automatic
pass still misses to its nearest bone segment directly, so every vertex always
follows *some* bone regardless of what Blender's solver does. `clean_mesh` also
now triangulates stray n-gons from the ball boolean union. Verified 0/50 true rig
failures (excluding assets where rig was randomly disabled) across a full sweep —
was ~65-100% for ball+rig combinations before, including a still-broken case even
in the original pre-session code.

**Fix: bare shaft got a flat top.** Now gets a small rounded dome instead —
`corona_pos=0`/`corona_radius=shaft_radius` collapses the glans shape so
`shaft_and_head_radius`'s existing dome math produces a plain tangent-continuous
hemisphere cap over `head_length = shaft_radius * 1.1`.

**Fix: ball retopo resolution too low.** `retopo_grid_ball_segments` default
doubled 16 → 32.

**Fix: ball-bottom bake artifacts.** The flush-trim seam where a ball meets the
base is a genuinely tight concave crease by design (balls sit flush, not
floating) — more retopo resolution and a bigger cage both fail to fix it (a
bigger cage makes it *worse*, per the existing `_detect_bake_bright_artifacts`
finding). `bake_normal_map()` now inpaints flagged bright-artifact texels from
their good neighbours (renormalized average, several passes to converge past any
systematically-biased region) instead of just warning and shipping the raw
grazing-ray colour. See `_inpaint_bake_bright_artifacts`.

**Feature: canonical UV placement.** Retopo now has *two* UV maps.
`"UVMap"` (first/default) is Blender's normal automatic unwrap, tightly packed
into [0,1] — unchanged, still what `bake_normal_map()` targets. `"UVMap_Canonical"`
(second) is assigned directly from a fixed formula per part instead of an
automatic unwrap: cylindrical projection (real-world arc length × real-world
height, fixed reference radius so a taper/knot doesn't shear it) for the shaft and
head, top-down planar for the base/cup, equirectangular per ball. Every part gets
its own fixed UV-space lane (`UV_LANE_SHAFT/HEAD/BASE/BALL_L/BALL_R`) and the same
`UV_TEXELS_PER_METER` scale, so the same real-world point always lands at the same
UV coordinate on every generated asset — what makes a shared tiling material line
up consistently. **Important:** don't reuse the canonical map for baking — it's
deliberately *not* packed into [0,1], so a bake would alias unrelated mesh regions
onto the same texels wherever UVs cross a whole UV unit (found this the hard way,
~5x bright-artifact spike on a first single-UV-map attempt). Balls also now get
their own individual UV islands (previously combined into one).

**Feature: checker pattern material.** `ASSETGEN_OT_apply_checker_material`
("Apply Checker Pattern" button, next to "Bake Normal Map & Setup Material" in the
main panel) wires a procedural Checker Texture node through a `ShaderNodeUVMap`
pointing at `UVMap_Canonical` into Base Color — squares read as true undistorted
squares at a consistent physical size/orientation across assets.

Verified together: UV export shows clean evenly-spaced square grid cells; checker
pattern visually confirmed consistent scale across differently-seeded assets;
0/30 boundary-edge failures, 0/30 rig failures, 0/30 bake errors across a full
randomized sweep with baking enabled on every single asset.

**Fix: ball checker pattern distorted from the side.** `_assign_spherical_canonical_uv`
used a plain equirectangular (lat/long) projection pinned to world +Z. The pole
singularity — where the checker squares pinch into wedges — landed on each ball's
most visible surface (facing 3/4-view/side angles), not somewhere hidden. Added a
`pole_axis` parameter and an orthonormal-basis (`pole`/`u_axis`/`v_axis`) theta/phi
computation so the pole can be aimed anywhere; `uv_seams_and_unwrap` now points each
ball's pole toward the shaft's central axis (`pole_axis = (-cx, -cy, 0.0)`), i.e.
into the merge seam where it's occluded. Verified visually (side + 3/4 views, clean
squares, no pinwheel) and via a 15-asset `balls_mode='ALWAYS'` regression sweep:
0/15 boundary, rig, and bake failures.

## What's done (verified)

- Vertical shaft seam always reaches the cup boundary seam.
- Suction cup tip converges to a single pole vertex.
- Ball bottoms: real boolean intersect + triangulation, no n-gons.
- Diamond pole cap: pure radial-fan lerp, no BVH snap (see fix above) — provably
  can't self-intersect for a star-shaped boundary loop.
- Retopo UV islands: up to 5 clean islands (ball L / ball R / base cap or cup /
  head / shaft body+knot), split with exact ring seams. Bare-shaft (no head) case
  keeps a pinch-avoidance margin on the vertical cut since its own tip pole has
  nowhere else to go (rare now that bare shafts get a small dome, but head_faces
  can still end up empty in principle).
- Rig: automatic weights with a guaranteed nearest-bone fallback (see above) — 
  build_rig always leaves every vertex with a working weight.
- Rig-vs-bake pose mismatch: `bake_normal_map()` force-sets any armature driving
  the highpoly/retopo to `pose_position = 'REST'` for the duration of the bake.
- Bake: inpaints bright-artifact texels instead of shipping them raw.
- Normal Map UI: 512/2048 resolution picker, "Bake Normal Map" button, "Bake
  Normal Map & Setup Material" one-click button, "Apply Checker Pattern" button.
- Regression: 0% boundary-edge / rig / bake failures across 150+ randomized
  generations with baking enabled (all part combos).

## What's left / open

- The deeper rig-weighting fragility for some ball-heavy combos isn't fully
  root-caused, just fully compensated for (nearest-bone fallback). If it ever
  needs actually fixing: self-intersections showed up specifically in
  `merge_balls_into_grid`'s boolean-union output near the base, position varying
  with total mesh length in a way that didn't fully make sense on inspection —
  see the git log for `_assign_fallback_weights` for the full investigation trail.
- Not stress-tested: bake operator with `asset_count > 1` (still gated to
  `asset_count == 1` by design).
- Checker pattern cell size (`ASSETGEN_OT_apply_checker_material.CHECKER_SCALE`,
  currently 4.0) and `UV_TEXELS_PER_METER` (currently 25.0) are hardcoded, not
  exposed in the UI — revisit if the default cell size needs tuning.

## Useful test snippets

Regression sweep (boundary-edge + rig + bake, all in one):
```python
import bpy, bmesh
s = bpy.context.scene.assetgen_settings
s.head_mode='RANDOM'; s.crevice_mode='RANDOM'; s.balls_mode='RANDOM'
s.cup_mode='RANDOM'; s.knot_mode='RANDOM'; s.curve_mode='RANDOM'; s.rig_mode='RANDOM'
bad_boundary, bad_rig, bake_errors = [], [], []
for i in range(40):
    bpy.ops.assetgen.generate()
    rt = bpy.data.objects["GameAsset_Retopo"]
    bm = bmesh.new(); bm.from_mesh(rt.data); bm.edges.ensure_lookup_table()
    n = len([e for e in bm.edges if len(e.link_faces) == 1])
    bm.free()
    if n: bad_boundary.append((i, n))
    if len(rt.vertex_groups) > 0:
        uw = sum(1 for v in rt.data.vertices if sum(g.weight for g in v.groups) < 1e-6)
        if uw: bad_rig.append((i, uw))
    try:
        bpy.ops.assetgen.bake_normal_map()
    except Exception as exc:
        bake_errors.append((i, str(exc)))
print("boundary:", len(bad_boundary), bad_boundary)
print("rig:", len(bad_rig), bad_rig)
print("bake:", len(bake_errors), bake_errors)
```

Checker pattern preview:
```python
bpy.ops.assetgen.generate()
bpy.ops.assetgen.apply_checker_material()
# viewport is already Material Preview afterward; screenshot with
# mcp__blender__get_viewport_screenshot
```
