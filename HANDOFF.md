# Handoff: Dilbo Asset Generator addon

**Style note for the assistant reading this:** be terse. Short sentences,
no recaps, no filler. Answer/act, don't narrate.

## Where things stand

- Repo: `wiguelsoares/ClaudeBlender`, branch `master`, clean, pushed.
- **Tag `v0.1.0-beta`** at `842cac9` -- user-declared Beta milestone.
  Core pipeline (highpoly gen, retopo, rig, bake, checker material, seed
  traceability) is regression-stable as of this tag. Treat further work
  past this point as post-Beta iteration, not first-time-stabilization.
- File: `Tools/dilbo_asset_generator_addon.py` (single-file Blender addon).
- Live copy: `C:\Users\wigue\AppData\Roaming\Blender Foundation\Blender\5.1\scripts\addons\dilbo_asset_generator_addon.py`
  — after editing the repo file, `cp` it here and reload via
  `bpy.ops.preferences.addon_disable/addon_enable(module='dilbo_asset_generator_addon')`.
- Blender is connected via `mcp__blender__execute_blender_code` /
  `mcp__blender__get_viewport_screenshot`.
- Last commit: `096653a` Bake Normal Map bakes every selected lowpoly, pairing each
  with its own highpoly (see part 11).

## Session 2026-07-02 (part 12): Apply Checker Pattern applies to every selected lowpoly too

User: "the apply checker pattern is only applying to one, it should be
applied to all the lowpoly selected on the scene" -- immediate follow-up
to part 11's batch-bake change, same request applied to the sibling
button.

`ASSETGEN_OT_apply_checker_material.execute()` now mirrors the batch
bake operator exactly: iterates `context.selected_objects`, skips
anything named `GameAsset_HighPoly*` as a target (never touched), and
applies the single shared `GameAsset_CheckerPattern` material to every
remaining selected mesh with a canonical UV map. A selected object
missing that UV map is a per-object failure (WARNING, listed by name),
not a whole-selection abort. One material datablock is reused across
every target -- its UV Map node looks up `"UVMap_Canonical"` by *name*
on whichever mesh is currently being shaded, so sharing one material
across objects with independent mesh data is correct as-is, no
per-object node duplication needed.

Verified: 5-asset batch, selected all 5 retopos *and* all 5 highpolies
together, one click -- all 5 retopos got the checker material, all 5
highpolies' material slots stayed exactly as before (`None`/empty, i.e.
untouched). Confirmed visually (viewport screenshot, framed to all 5)
that every one of the 5 shows the checker pattern.

## Session 2026-07-02 (part 11): batch bake selected lowpolies + a real alignment bug fix

First attempt this session ("bake every lowpoly selected, disregard the
highpoly") was misread as "self-bake each lowpoly's own normals, drop
the highpoly pipeline entirely" -- shipped as commit `3621eec`, user
immediately said "that's not what I asked" and gave the actual spec:
bake every *selected* lowpoly using the highpoly Selected-to-Active
pipeline (don't delete it), and if a highpoly happens to be in the
selection too, just skip it as a bake *target* -- don't touch/delete it,
still fine to use as a detail *source*. Reverted `3621eec` first
(`git revert`, matches this session's standing practice for "not what I
wanted" corrections), then built the real feature on the restored
highpoly pipeline.

`ASSETGEN_OT_bake_normal_map.execute()` now takes
`context.selected_objects`, filters out anything named
`GameAsset_HighPoly*` (never a valid target, silently skipped rather
than erroring), and for every remaining selected mesh finds its paired
highpoly by extracting the shared `_seed{N}` suffix from the name (new
`_paired_highpoly_name()` helper, regex-based so it survives Blender's
own `.001`-collision renaming) and looks up `GameAsset_HighPoly_seed{N}`
in the scene. Missing pairs (highpoly wasn't kept, or the object was
renamed) are per-object failures, not a whole-batch abort -- continues
baking the rest and reports a WARNING listing what failed. Dropped the
old `asset_count != 1` restriction; it's moot now.

**Found a real, previously-invisible bug while testing the batch case**:
baking a batch of >1 generated assets, only the first one ever came out
with real detail -- every other one baked "successfully" (no error, no
ray-miss warning) but completely flat. Root cause: `bake_normal_map()`
zeroes the highpoly/retopo offset via
`highpoly.location = retopo.location.copy()`, but when a Rig is built,
retopo gets parented to the armature (`build_rig`'s `ARMATURE_AUTO`
parent_set) -- `.location` is then relative to that armature, not world
space. Batch generation shifts each asset's rig sideways so they don't
overlap in the viewport, so for every asset after the first,
`retopo.location` (parent-relative) no longer matches its actual world
position, and the "zero the offset" line moves the highpoly to the
wrong place entirely. Cycles doesn't leave the ray-miss sentinel behind
in this failure mode either -- it writes a flat "no detail" normal to
every texel, so the existing `_detect_bake_ray_misses` check (which
only looks for the sentinel magenta) never catches it. This is why
earlier sessions' bake regression sweeps never surfaced it -- those all
used `Count = 1` with a full scene clear between assets, so the rig
that "happened" to be baked always sat at world origin (asset #1's
slot), masking the bug. Fixed by aligning to
`retopo.matrix_world.translation` instead of the bare `.location`.

Verified: 3-asset and 5-asset batches, all rigged, Keep Highpoly on,
selecting both the lowpoly *and* highpoly objects together and hitting
the one Bake button -- only the lowpolies get baked, each correctly
paired with its own highpoly by seed, highpolies untouched in the
scene, and (post alignment-fix) every image in the batch shows real
per-asset surface detail (std of 0.01-0.09 across R/G/B, not the ~1e-4
flat-bug signature). `ASSETGEN_OT_bake_and_setup_material` still pins
selection to just `s.last_retopo_name` before delegating, so its
"bake and preview the just-generated asset" behaviour stays independent
of whatever's selected for the plain batch-bake button.

## Session 2026-07-02 (part 10): random 1-3 knots, per-knot position/Z-scale variation

User: three asks at once -- "the knot right now is only one, I want it to
be a random number between 0 and 3", "the z position is always the same,
I want it to be variable", "the knot scale in Z is always the same, I
want a slight variation on that axis."

Replaced the single `build_knot(p)` call with `build_knots(p, rng)`,
which builds `p["knot_count"]` independent UV-sphere bulges and returns
`(object, z, radius)` tuples (boolean_union consumes the object, so z/
radius are captured up front for the support-loop and report code that
runs after the union). `knot_count` is resolved in `generate()` right
after `has_knot`: `rng.randint(1, 3) if has_knot else 0` -- Chance still
gates whether any knot shows up at all (unchanged semantics), how many
(1-3) is then independent, giving the requested 0-3 range overall.

Position and Z-scale are randomised per knot instance, independent of
the global `variation` slider (same "always-on" pattern as
`curve_angle`/`rig_x_bend_random` from parts 7-8) via two new
properties: `knot_position_spread` (default ±0.25 of shaft length around
the mean `knot_position`) and `knot_z_scale_variation` (default ±18%
elongation/squash on the sphere's Z axis before it's unioned in).
Verified directly that the scale actually reaches the mesh (dims.z/
dims.x ratio away from 1.0 pre-union) rather than being a no-op transform
lost before the boolean.

`support_loop_zs` (bracketing knots with clean edge loops for the
retopo remesh) already accepted a generic list, so it needed no change
beyond iterating every knot instance instead of the one hardcoded pair.

Verified: 25-asset random-seed sweep with `knot_mode=ALWAYS` (forcing
1-3 overlapping knots every run, stacked with balls/cup/curve/rig) --
observed counts spanning 1/2/3 and positions/radii spanning the full
range, 0 manifold or self-intersection issues.

## Session 2026-07-02 (part 9): embed seed in generated object names

User: "I need you to append the seed used to create the asset in its name
so we can debug the issue" -- so a specific broken asset from a batch can
be reproduced by re-running with its exact seed instead of guessing.

`generate()` now resolves an explicit integer `actual_seed` *before*
constructing the RNG -- `cfg["seed"]` if set, otherwise
`random.SystemRandom().randrange(0, 2**31 - 1)` -- so a concrete seed
always exists to embed and report, even in "Random Seed Each Run" mode
where the seed was previously opaque. All three generated objects are
renamed right after creation: `GameAsset_HighPoly_seed{N}`,
`GameAsset_Retopo_seed{N}`, `GameAsset_Rig_seed{N}`. The Asset Report
print now shows the resolved seed, tagged `(Random Seed Each Run)` when
applicable.

Caught and fixed a self-introduced bug before it shipped: an early
version read `asset.name` after `asset` could already have been removed
(when retopo replaces the highpoly), which raises `ReferenceError`
immediately -- `.name in bpy.data.objects` doesn't guard it, the
attribute access itself throws first. Fixed by capturing
`highpoly_name`/`highpoly_kept` before any possible removal.

Three operators previously looked up generated objects by hardcoded
literal name (`"GameAsset_HighPoly"` etc.), which broke once names carry
a seed suffix. Added scene-level tracking properties
(`last_highpoly_name`, `last_retopo_name`, `last_rig_name`, mirroring the
existing `last_bake_image_name`) set at the end of `generate()`, and
switched `ASSETGEN_OT_bake_normal_map`, `ASSETGEN_OT_bake_and_setup_material`,
and `ASSETGEN_OT_apply_checker_material` to look up objects via these
tracked names instead.

Verified: fixed-seed, random-seed, and 4-asset-batch generation all
produce correctly seed-named objects; all three affected operators run
clean against the tracked-name lookups. Final regression per standing
practice (`use_random_seed=True`, 30 assets, checking manifold /
self-intersection / rig-weight / bake): 0 issues.

## Session 2026-07-02 (part 8): fix wrong bone assignment in the rig fallback

User: "the retopo version presents severe topology issues" on one of their
own 30-asset batch, viewed with the rig posed -- a shredded/torn-looking
vertical streak from the head partway down the shaft. Diagnosed the
*rest-pose* mesh first (self-intersection, manifold, normals, face areas,
radius profile) and found it completely clean -- the bug wasn't in the
mesh at all. Comparing rest-pose vertex positions against the
depsgraph-evaluated (posed) mesh showed the real fault: individual
vertices jumping to wildly different positions than their immediate
neighbours once the armature bends, e.g. one vertex moving 0.05m in Y
under the pose while its neighbours barely moved.

Root cause: adjacent vertices (indices 498-502, all near z~0.02-0.03,
squarely inside spine_0's 0..0.036 range) were weighted 100% to
completely different, non-adjacent bones -- spine_1, spine_4, spine_0.
Traced to `_assign_fallback_weights` (the "nearest bone segment" fallback
for vertices the automatic Heat Weighting solver misses):
`mathutils.geometry.intersect_point_line` returns the closest point on
the *infinite* line through a bone's head/tail, not clamped to the actual
segment. Since a straight spine's segments are all collinear, an
unclamped projection from a bone far up the chain can occasionally beat
the correct nearby segment's real (clamped) distance by pure numerical
noise -- confirmed directly: spine_4 (z 0.145-0.181) measured 0.01826
unclamped vs. spine_0's correct 0.02057, when clamped to their actual
segments spine_0 correctly wins by a wide margin (0.02057 vs 0.14635).

Fixed by clamping the interpolation factor to [0, 1] before computing the
closest point, so "nearest bone segment" actually means nearest point *on
the segment*.

Verified per explicit instruction to use true random seeding (not a fixed
sequence, since a specific bug needs specific random draws to surface):
0/30 assets with the tearing artifact on a `use_random_seed=True` sweep,
many of which exercised the exact buggy code path (visible via the "Bone
Heat Weighting: failed to find solution" warning). Separate 40-asset
random-seed sweep covering manifold, self-intersection, tearing, rig
weights, and baking: 1 unrelated single non-manifold edge (a different,
much rarer pre-existing issue, not connected to this fix or the two
before it -- not investigated further this session).

## Session 2026-07-02 (part 7): fix head-tip pole self-intersection

User generated a 30-asset batch and asked to examine each one for problems.
Diagnosed all 25 retopo objects (`BVHTree.overlap()` self-intersection +
manifold checks): 13/25 had residual self-intersecting faces (2-6 each).
Located every one of them within ~1% of the mesh's own max Z -- the head
tip pole cap, not the balls (part 6's fix holds; this is the "still open"
item flagged at the end of that section).

Root cause, found by testing which factor actually mattered: NOT the
pole's position (proved by removing the BVH snap and even the outward dip
entirely -- self-intersection count didn't change at all). It's the
boundary ring itself: `build_diamond_pole_cap`'s non-self-intersection
proof assumes a star-shaped boundary (monotonically increasing angle
around the centroid), true for an idealized circle but not guaranteed for
a real shrinkwrapped ring -- confirmed one failing case had 2 sign-changes
in per-step angle despite only ~7% radius variation, clearly shrinkwrap
noise rather than a genuine dent. The code deliberately never re-sorts the
boundary by angle (a comment already explains why: a genuine dent, e.g.
the head crevice slit, would have its vertices silently reshuffled out of
true mesh connectivity) -- so small noise wobbles were never being caught.

Fixed in `cap_ends_with_quads`: 3 rounds of light neighbour-averaging
(50% self / 25%+25% neighbours) on the boundary loop right before building
its cap. Removes small-amplitude noise while leaving a genuine large-scale
dent essentially intact (confirmed: still 0 self-intersections on crevice
assets, so the slit shape survives). Verified 0/40 self-intersections and
0/40 manifold/rig/bake failures on a sweep spanning every part
combination -- both a fixed-seed sweep (1-40, for exact reproducibility
while iterating) and a `use_random_seed=True` sweep (per explicit
instruction: a fixed seed sequence can hide a bug that only shows up for
specific random draws).

## Session 2026-07-02 (part 6): fix real self-intersecting geometry near the balls

User: "the retopo version presents severe topology issues" -- looked at a
freshly generated asset (seed 1: head+balls+knot+cup+rig) and it had a
visibly crumpled/torn-looking patch of dark shading. Traced with
`BVHTree.overlap()` (self-intersection check, not just manifold/watertight
-- those were already clean and had been the only checks run so far): 46
genuinely self-intersecting, non-adjacent face pairs. A 20-seed sweep with
`balls_mode='ALWAYS'` found this on **20/20** seeds (6-66 self-intersecting
faces each) -- universal, not an edge case, and not the jagged-seam
limitation documented in part 3 (that was manifold/watertight, just an
ugly triangulation; this is actual folded/overlapping geometry).

Root cause, found by checking self-intersection count at every pipeline
stage: 0 right through `_fill_stray_gaps` (i.e. the ball boolean union
itself is fine), then jumps to 20+ immediately after `shrinkwrap_to`. The
generic Shrinkwrap modifier's NEAREST_SURFACEPOINT search moves every
vertex independently; right where the ball sphere and shaft cylinder meet,
two mesh-adjacent low-poly vertices can have their nearest point jump to
disconnected regions of the highpoly surface, folding the mesh into itself.
The irony: the boolean union already computed those vertices' correct
position exactly (that's what a boolean union *is*), so shrinkwrapping them
again was pure downside.

Fixed by not shrinkwrapping them a second time. `shrinkwrap_to` gained an
`exclude_vert_idx` parameter (implemented as a vertex group masking the
Shrinkwrap modifier's influence, not a post-hoc position undo, so excluded
vertices are never touched by the search in the first place).
`_near_ball_vertex_indices` (new) flags every vertex within `1.4x ball_r`
of either ball centre -- both the curved dome *and* the flat trim disc
under it (a tighter mask covering only the dome left a handful of
self-intersections at the flat/dome boundary on complex cup+knot+balls
assets; the wider one hit 0 on every seed tested).

Hit one real bug while wiring this up: `bpy.ops.object.modifier_apply`
can reallocate the mesh's vertex-group data, leaving the Python
`VertexGroup` reference captured before the apply stale -- passing it to
`vertex_groups.remove()` afterward raised "DeformGroup not in object" even
though a group with that name (just a new internal pointer) was still
right there. Fixed by re-looking the group up by name after the apply
instead of reusing the pre-apply reference.

Verified: 30-seed sweep (all optional parts randomised, balls always on) --
self-intersection count dropped from 6-66 per asset down to 0-6, and every
remaining one is at the head tip pole cap (a separate, smaller,
pre-existing issue -- confirmed by location, z ≈ mesh's own max height, in
every case checked -- not touched by this fix). 40-seed sweep with full
baking: 0 manifold/rig/bake failures. Visually confirmed the originally
crumpled seed-1 asset now shows a clean, fully rounded knot bulge, cup, and
balls with no dark torn patches anywhere.

**Still open:** the head tip's own small (2-6 face) self-intersection,
unrelated to balls -- not investigated this session, scope was the crumpled
ball area the user flagged. Likely the same class of bug (cap_ends_with_quads'
single BVH nearest-point lookup for the pole position itself, not the
already-fixed interior fan) but not confirmed.

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
