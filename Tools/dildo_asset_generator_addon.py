"""
Dildo Asset Generator -- single-file Blender addon.

Procedurally builds a game-ready, cylinder-based organic asset (lathed
shaft + glans head, optional meatus crevice, optional balls), an optional
game-ready retopology pass, and an optional bendable bone rig -- all driven
from a sidebar panel (View3D > Sidebar > "Asset Gen" tab). Includes a
one-click updater that pulls this file's latest committed version straight
from GitHub.

Install: Edit > Preferences > Add-ons > Install..., point at this .py file
(or drop it directly into your Blender scripts/addons folder) and enable
"Dildo Asset Generator".

To update after a change is pushed to the tracked branch, use the "Check
for Updates" / "Update Now" buttons at the top of the panel -- no need to
reinstall by hand. After an update, disable and re-enable the addon (or
restart Blender) so the new code is loaded.
"""

import json
import math
import os
import random
import tempfile
import urllib.error
import urllib.parse
import urllib.request

import bmesh
import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import AddonPreferences, Operator, Panel, PropertyGroup

bl_info = {
    "name": "Dildo Asset Generator",
    "author": "Drone project",
    "version": (1, 0, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > Asset Gen",
    "description": (
        "Procedurally generate game-ready cylinder-based organic assets "
        "with a bendable rig, retopology, and one-click GitHub updates"
    ),
    "category": "Add Mesh",
}


# ══════════════════════════════════════════════════════════════════════════════
#  DEFAULT CONFIGURATION  – the addon UI overrides these per generation
# ══════════════════════════════════════════════════════════════════════════════
DEFAULT_CONFIG = {
    # ── Shaft (metres) ──────────────────────────────────────────────────────
    "shaft_length": 0.14,
    "shaft_radius": 0.018,        # radius at the top of the shaft (head join)
    # Shaft flare: the base radius is shaft_radius * (1 + flare), tapering
    # smoothly up to shaft_radius where it meets the head.  Positive = the
    # bottom is wider than the top.  When variation > 0 the flare is redrawn
    # per run from [shaft_flare_min, shaft_flare_max], so some come out wider,
    # some roughly straight, and a few slightly narrower -- not always wider.
    "shaft_flare": 0.0,          # used when variation == 0 (deterministic)
    "shaft_flare_min": -0.06,    # per-run random lower bound (slightly narrower)
    "shaft_flare_max": 0.35,     # per-run random upper bound (bottom wider)

    # ── Head (glans) ────────────────────────────────────────────────────────
    # Profile (bottom -> top): shaft blends up into a sulcus groove, swells
    # out to a rounded corona overhang (widest ring), then continues as an
    # elongated prolate-spheroid dome that rounds off to the tip.  The dome
    # is taller than it is wide (prolate) and more convex than the underside,
    # giving the teardrop silhouette in side view.
    "head_length": 0.045,        # length of the lathed head profile
    "head_corona_radius": 0.021, # widest point of the head (the corona ridge)
    "head_tip_radius": 0.003,    # small radius at the very tip (rounded, not sharp)
    "head_corona_pos": 0.30,     # where the corona (widest ring) sits along the
                                  #   head, 0 = at the shaft join, 1 = at the tip.
                                  #   Lower = more of the head is convex dome.
    "head_sulcus_pos": 0.45,     # position of the sulcus groove *below* the
                                  #   corona, as a fraction of head_corona_pos
    "head_sulcus_factor": 0.92,  # sulcus groove radius as a fraction of
                                  #   shaft_radius (<1 = a groove behind the corona)

    # ── Head skew / sulcus tilt (applied after the lathe) ───────────────────
    "head_skew": -0.30,          # tip lean along Y, as a fraction of head_length;
                                  #   eased in from the shaft join (0 = straight)
    "head_skew_dir": 0.30,       # direction/magnitude multiplier for the tip lean
                                  #   (+ leans toward +Y, - leans toward -Y)
    "head_sulcus_tilt": 0.15,    # tilt the sulcus/corona collar into a diagonal:
                                  #   the -Y side rides up, the +Y side drops down,
                                  #   as a fraction of head_length (negative flips
                                  #   which side rides up)

    # ── Head crevice (meatus slit at the tip) ───────────────────────────────
    # A thin slot is boolean-cut into the tip, lying in the X=0 symmetry plane
    # and running along Y, so the highpoly shows a urethral-style slit.
    "head_crevice": True,
    "crevice_length": 0.031,     # slit length along Y (metres)
    "crevice_width": 0.0020,     # slit width along X (metres)
    "crevice_depth": 0.006,      # how deep the slit cuts into the tip (metres)
    "crevice_y_bias": 0.01,      # shift the slit toward -Y (metres) so it runs
                                  #   further down that side instead of centred

    # ── Balls ────────────────────────────────────────────────────────────────
    # Each ball is bisected at z=0 so only the top hemisphere shows, sitting
    # flush with the flat base -- the pair is merged together against the side
    # of the shaft via boolean union.
    "balls_enabled": None,       # True = always, False = never, None = random
                                  #   (decided per run using balls_chance)
    "balls_chance": 0.6,         # probability of balls when balls_enabled is None
    "ball_radius": 0.022,
    "ball_spacing": 0.014,       # distance between the two ball centres (X axis)
    "ball_side_overlap": 0.5,    # how far each ball pokes into the shaft wall,
                                  #   as a fraction of ball_radius

    # ── Randomness ──────────────────────────────────────────────────────────
    "variation": 0.2,            # 0.0 = fully deterministic, 1.0 = large swings
    "seed": None,                # integer for reproducible results; None = new
                                  #   random shape every run

    # ── Mesh quality ────────────────────────────────────────────────────────
    "profile_segments": 32,      # vertical resolution of the lathed profile
    "radial_segments": 48,       # segments around the revolve axis
    "ball_segments": 32,
    "subsurf_levels": 1,         # applied Subdivision Surface levels

    # ── Retopology (game-ready low-poly pass) ───────────────────────────────
    # The build above is the highpoly.  When enabled, a clean low-poly copy is
    # generated from it and (optionally) shrink-wrapped back onto the highpoly
    # so the silhouette is preserved.  Keep both so the highpoly can be used to
    # bake normal/AO maps onto the retopo in Unreal's pipeline.
    "retopo_enabled": True,
    "retopo_method": "quadriflow",  # "quadriflow" (clean quads) or "voxel"
                                     #   (voxel remesh + decimate fallback)
    "retopo_target_faces": 2000,    # quad target for quadriflow (game budget)
    "retopo_voxel_size": 0.006,     # voxel size (m) for the voxel method
    "retopo_decimate_ratio": 0.5,   # collapse ratio after voxel remesh
    "retopo_shrinkwrap": True,      # pull the low-poly back onto the highpoly
    "retopo_smooth_normals": True,  # smooth normals during quadriflow
    "retopo_symmetry_axis": "POSITIVE_X",  # mirror the retopo across this plane so
                                     #   the topology is always symmetric.  The asset
                                     #   is mirror-equal across X=0 (ball pair + Y/Z
                                     #   head deforms).  "" / None disables it.
    "retopo_keep_highpoly": True,   # keep the highpoly in the scene alongside
    "retopo_offset_x": 0.12,        # place the retopo this far in +X from the
                                     #   highpoly for side-by-side compare (0 = in place)

    # ── Rigging ─────────────────────────────────────────────────────────────
    # A multi-segment spine is built up the length of the asset (bones named
    # spine_0 at the base .. spine_N-1 at the tip) and the game mesh is skinned
    # to it with automatic weights, so it exports to Unreal as a skeletal mesh.
    "rig_enabled": True,
    "rig_segments": 5,              # number of spine bones (more = smoother bend)
    "rig_x_bend": 0.0,              # TOTAL X bend in degrees, spread across the
                                     #   spine bones above the base so the shaft
                                     #   curves like a bend (0 = straight)
    "rig_x_bend_random": 25.0,      # per-run random bend (deg): when variation > 0
                                     #   a value from [-this, +this] is added to
                                     #   rig_x_bend, so each asset curves differently
                                     #   (0 disables the random bend)
    "rig_bone_x_rotations": {},     # optional explicit per-bone X degrees, keyed
                                     #   by bone name (e.g. {"spine_3": 20}); these
                                     #   override the distributed bend for that bone
}


# ══════════════════════════════════════════════════════════════════════════════
#  SCENE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def clear_scene() -> None:
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    for block in bpy.data.meshes:
        bpy.data.meshes.remove(block)


def set_active(obj: bpy.types.Object) -> None:
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


# ══════════════════════════════════════════════════════════════════════════════
#  RANDOMISATION
# ══════════════════════════════════════════════════════════════════════════════

def jitter(value: float, fraction: float, rng: random.Random) -> float:
    """Return value offset by a uniform ±fraction relative deviation."""
    return value * (1.0 + rng.uniform(-fraction, fraction))


def randomise(cfg: dict, rng: random.Random) -> dict:
    """Return a new parameter dict with each dimension slightly jittered."""
    v = cfg["variation"]
    # Shaft flare is drawn from an explicit range (not multiplicative jitter,
    # which can't move a 0.0 base) so it can land wider, straight, or narrower.
    if v > 0.0:
        shaft_flare = rng.uniform(cfg["shaft_flare_min"], cfg["shaft_flare_max"])
    else:
        shaft_flare = cfg["shaft_flare"]
    # Random bend is additive (not multiplicative jitter, which can't move a
    # 0.0 base) so a straight rig_x_bend of 0 can still curve either way.
    if v > 0.0 and cfg["rig_x_bend_random"] != 0.0:
        rig_x_bend = cfg["rig_x_bend"] + rng.uniform(
            -cfg["rig_x_bend_random"], cfg["rig_x_bend_random"]
        )
    else:
        rig_x_bend = cfg["rig_x_bend"]
    return {
        **cfg,
        "shaft_flare":       shaft_flare,
        "rig_x_bend":        rig_x_bend,
        "shaft_length":      jitter(cfg["shaft_length"],      0.20 * v, rng),
        "shaft_radius":      jitter(cfg["shaft_radius"],      0.15 * v, rng),
        "head_length":       jitter(cfg["head_length"],       0.25 * v, rng),
        "head_corona_radius":jitter(cfg["head_corona_radius"],0.15 * v, rng),
        "head_tip_radius":   jitter(cfg["head_tip_radius"],   0.50 * v, rng),
        "head_corona_pos":   jitter(cfg["head_corona_pos"],   0.15 * v, rng),
        "head_skew":         jitter(cfg["head_skew"],         0.40 * v, rng),
        "head_sulcus_tilt":  jitter(cfg["head_sulcus_tilt"],  0.40 * v, rng),
        "ball_radius":       jitter(cfg["ball_radius"],       0.20 * v, rng),
        "ball_spacing":      jitter(cfg["ball_spacing"],      0.30 * v, rng),
        "ball_side_overlap": jitter(cfg["ball_side_overlap"], 0.20 * v, rng),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  PROFILE / LATHE
# ══════════════════════════════════════════════════════════════════════════════

def smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def shaft_and_head_radius(z: float, p: dict) -> float:
    """
    Radius of the revolve profile at height z.

    Below the head it is a straight cylinder (flat base at z=0).  The head is
    built from three C1-continuous pieces so tangents match at every join:

      shaft -> sulcus : smoothstep dip from shaft_r down to the groove
      sulcus -> corona: smoothstep swell up to the widest ring (the overhang)
      corona -> tip   : quarter-ellipse (prolate spheroid) rounding to the tip

    The corona sits low on the head, so most of the head is the tall convex
    dome -- more convex up top than the tucked underside -> teardrop profile.
    """
    shaft_len = p["shaft_length"]
    shaft_r   = p["shaft_radius"]
    head_len  = p["head_length"]
    corona_r  = p["head_corona_radius"]
    tip_r     = p["head_tip_radius"]

    if z <= shaft_len:
        # Flared shaft: base_r at z=0 tapering to shaft_r at the head join.
        # 1 - smoothstep keeps zero slope at the top so it meets the head
        # cleanly (no crease) and gives a rounded flare rather than a cone.
        flare = p.get("shaft_flare", 0.0)
        if flare == 0.0 or shaft_len <= 0.0:
            return shaft_r
        base_r = shaft_r * (1.0 + flare)
        return shaft_r + (base_r - shaft_r) * (1.0 - smoothstep(z / shaft_len))

    u = (z - shaft_len) / head_len          # 0 at shaft join, 1 at tip
    u = max(0.0, min(1.0, u))

    u_corona = p["head_corona_pos"]
    u_sulcus = u_corona * p["head_sulcus_pos"]
    sulcus_r = shaft_r * p["head_sulcus_factor"]

    if u >= u_corona:
        # Prolate-spheroid dome: quarter-ellipse from corona (widest, vertical
        # tangent) up to a rounded tip.  sqrt() keeps the crown convex/bulbous.
        s = (u - u_corona) / (1.0 - u_corona)          # 0 at corona, 1 at tip
        dome = math.sqrt(max(0.0, 1.0 - s * s))
        return tip_r + (corona_r - tip_r) * dome

    if u >= u_sulcus:
        # Underside of the corona: swell from the groove out to the ridge.
        frac = smoothstep((u - u_sulcus) / (u_corona - u_sulcus))
        return sulcus_r + (corona_r - sulcus_r) * frac

    # Shaft blending down into the sulcus groove.
    frac = smoothstep(u / u_sulcus)
    return shaft_r + (sulcus_r - shaft_r) * frac


def tilt_sulcus(bm: bmesh.types.BMesh, p: dict) -> None:
    """
    Tilt the sulcus/corona collar into a diagonal instead of a flat ring.

    A Z offset proportional to -Y is applied so the -Y side of the collar
    rides up and the +Y side drops down.  It is windowed around the
    sulcus -> corona band (fading to zero at the shaft join below and part way
    up the dome above), so the flat base and the tip stay put and only the
    ridge/groove slants.
    """
    shaft_len = p["shaft_length"]
    head_len  = p["head_length"]
    corona_r  = p["head_corona_radius"]
    tilt      = p["head_sulcus_tilt"] * head_len
    if tilt == 0.0 or corona_r <= 0.0:
        return

    u_sulcus = max(1e-3, p["head_corona_pos"] * p["head_sulcus_pos"])
    u_corona = p["head_corona_pos"]
    u_fade   = u_corona + (1.0 - u_corona) * 0.5   # tilt gone by mid-dome

    for v in bm.verts:
        if v.co.z <= shaft_len:
            continue
        u = (v.co.z - shaft_len) / head_len
        if u < u_sulcus:
            w = smoothstep(u / u_sulcus)                       # base -> sulcus
        elif u <= u_corona:
            w = 1.0                                            # sulcus..corona
        elif u < u_fade:
            w = 1.0 - smoothstep((u - u_corona) / (u_fade - u_corona))
        else:
            w = 0.0
        if w <= 0.0:
            continue
        v.co.z += tilt * (-v.co.y / corona_r) * w


def skew_head(bm: bmesh.types.BMesh, p: dict) -> None:
    """
    Lean the head tip sideways along the Y axis so it tilts off the Z axis.

    Only the head (z > shaft_length) moves, eased in with smoothstep from the
    shaft join so the shaft stays straight and there is no kink at the seam --
    the tip ends up head_skew * head_length off-axis in Y.
    """
    shaft_len = p["shaft_length"]
    head_len  = p["head_length"]
    skew_amt  = p["head_skew"] * head_len * p["head_skew_dir"]
    if skew_amt == 0.0:
        return

    for v in bm.verts:
        if v.co.z <= shaft_len:
            continue
        u = max(0.0, min(1.0, (v.co.z - shaft_len) / head_len))
        v.co.y += skew_amt * smoothstep(u)


def build_shaft_and_head(p: dict) -> bpy.types.Object:
    """Single lathed mesh: flat-bottomed cylinder flowing into a glans head."""
    total_length = p["shaft_length"] + p["head_length"]

    bm = bmesh.new()
    verts = []
    for i in range(p["profile_segments"] + 1):
        t = i / p["profile_segments"]
        z = t * total_length
        r = shaft_and_head_radius(z, p)
        verts.append(bm.verts.new((r, 0.0, z)))
    for a, b in zip(verts, verts[1:]):
        bm.edges.new((a, b))

    bmesh.ops.spin(
        bm,
        geom=list(bm.verts) + list(bm.edges),
        cent=(0.0, 0.0, 0.0),
        axis=(0.0, 0.0, 1.0),
        angle=math.tau,
        steps=p["radial_segments"],
        use_duplicate=False,
    )
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
    # Cap the open bottom ring (flat base) and the small tip ring.
    bmesh.ops.holes_fill(bm, edges=bm.edges, sides=0)
    # Slant the sulcus/corona collar into a diagonal, then lean the tip in Y.
    tilt_sulcus(bm, p)
    skew_head(bm, p)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)

    mesh = bpy.data.meshes.new("ShaftHeadMesh")
    bm.to_mesh(mesh)
    bm.free()

    obj = bpy.data.objects.new("ShaftHead", mesh)
    bpy.context.collection.objects.link(obj)
    return obj


# ══════════════════════════════════════════════════════════════════════════════
#  BALLS
# ══════════════════════════════════════════════════════════════════════════════

def build_balls(p: dict) -> list:
    """
    Two hemisphere bumps against the shaft side.

    Each ball is a UV sphere with its lower half deleted at z=0 so it sits
    flush with the flat base.  Centres are placed at z ≈ 0 (tiny epsilon
    above the base plane to avoid a coplanar boolean artefact) and offset
    in Y to overlap the cylinder wall for a clean boolean union.
    """
    ball_r       = p["ball_radius"]
    half_spacing = p["ball_spacing"] * 0.5
    z_center     = ball_r * 0.01  # keep centre just above z=0
    # Offset from the actual shaft wall at the ball height, so a flared (wider)
    # base still gets a clean overlap instead of the balls sinking inside.
    wall_r       = shaft_and_head_radius(z_center, p)
    side_offset  = wall_r + ball_r * (1.0 - p["ball_side_overlap"])

    objs = []
    for sign, label in ((-1, "L"), (1, "R")):
        bpy.ops.mesh.primitive_uv_sphere_add(
            radius=ball_r,
            segments=p["ball_segments"],
            ring_count=max(8, p["ball_segments"] // 2),
            location=(sign * half_spacing, -side_offset, z_center),
        )
        obj = bpy.context.active_object
        obj.name = f"Ball_{label}"

        # Bisect at the equator: remove the lower hemisphere (local z < 0).
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        below = [v for v in bm.verts if v.co.z < -1e-5]
        bmesh.ops.delete(bm, geom=below, context='VERTS')
        bmesh.ops.holes_fill(bm, edges=bm.edges, sides=0)
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
        bm.to_mesh(obj.data)
        bm.free()
        obj.data.update()

        objs.append(obj)
    return objs


# ══════════════════════════════════════════════════════════════════════════════
#  MESH POST-PROCESSING
# ══════════════════════════════════════════════════════════════════════════════

def boolean_union(target: bpy.types.Object, cutter: bpy.types.Object) -> None:
    set_active(target)
    mod = target.modifiers.new(name=f"Union_{cutter.name}", type='BOOLEAN')
    mod.operation = 'UNION'
    mod.object = cutter
    mod.solver = 'EXACT'
    bpy.ops.object.modifier_apply(modifier=mod.name)
    bpy.data.objects.remove(cutter, do_unlink=True)


def carve_head_crevice(asset: bpy.types.Object, p: dict, cfg: dict) -> None:
    """
    Boolean-cut a thin meatus-style slit into the tip of the head.

    The cutter is a flattened ellipsoid centred on the tip, thin in X and
    elongated in Y, lying in the X=0 symmetry plane.  Its top half sits in
    empty space above the tip (removes nothing) while its lower half carves a
    narrow slot down into the glans -> a urethral slit in the highpoly.
    """
    tip_z = p["shaft_length"] + p["head_length"]
    # The tip is displaced in Y by the head skew (its local axis stays ~vertical
    # because the skew eases out with zero slope at the tip).
    skew_amt = p["head_skew"] * p["head_length"] * p["head_skew_dir"]
    # Bias the slit toward -Y so it runs down that side instead of centred.
    tip = (0.0, skew_amt - cfg["crevice_y_bias"], tip_z)

    bpy.ops.mesh.primitive_uv_sphere_add(
        radius=1.0, segments=24, ring_count=12, location=tip,
    )
    cutter = bpy.context.active_object
    cutter.name = "CreviceCutter"
    cutter.scale = (
        cfg["crevice_width"] * 0.5,
        cfg["crevice_length"] * 0.5,
        cfg["crevice_depth"],
    )
    set_active(cutter)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

    set_active(asset)
    mod = asset.modifiers.new(name="Crevice", type='BOOLEAN')
    mod.operation = 'DIFFERENCE'
    mod.object = cutter
    mod.solver = 'EXACT'
    bpy.ops.object.modifier_apply(modifier=mod.name)
    bpy.data.objects.remove(cutter, do_unlink=True)


def apply_subsurf(obj: bpy.types.Object, levels: int) -> None:
    set_active(obj)
    mod = obj.modifiers.new(name="Subsurf", type='SUBSURF')
    mod.levels = levels
    mod.render_levels = levels
    bpy.ops.object.modifier_apply(modifier=mod.name)


def recalc_normals(obj: bpy.types.Object) -> None:
    set_active(obj)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode='OBJECT')


def shade_smooth(obj: bpy.types.Object) -> None:
    for poly in obj.data.polygons:
        poly.use_smooth = True


# ══════════════════════════════════════════════════════════════════════════════
#  RETOPOLOGY  (game-ready low-poly pass)
# ══════════════════════════════════════════════════════════════════════════════

def duplicate_object(obj: bpy.types.Object, name: str) -> bpy.types.Object:
    """Return an independent copy of obj (own mesh datablock) linked to the scene."""
    copy = obj.copy()
    copy.data = obj.data.copy()
    copy.name = name
    copy.data.name = name
    bpy.context.collection.objects.link(copy)
    return copy


def shrinkwrap_to(obj: bpy.types.Object, target: bpy.types.Object) -> None:
    """Snap obj's verts onto the target surface and apply, to recover volume."""
    set_active(obj)
    mod = obj.modifiers.new(name="Shrinkwrap", type='SHRINKWRAP')
    mod.target = target
    mod.wrap_method = 'NEAREST_SURFACEPOINT'
    bpy.ops.object.modifier_apply(modifier=mod.name)


def symmetrize_mesh(obj: bpy.types.Object, direction: str) -> None:
    """Mirror one half of obj onto the other so the topology is exactly
    symmetric across the mesh's local mirror plane (X=0 for this asset)."""
    set_active(obj)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.symmetrize(direction=direction)
    bpy.ops.mesh.remove_doubles(threshold=1e-5)   # weld the seam at X=0
    bpy.ops.object.mode_set(mode='OBJECT')


def clean_mesh(obj: bpy.types.Object) -> None:
    """Merge coincident verts, drop loose geometry and fix normals so the
    remeshers get well-formed input (boolean unions can leave doubles and
    stray faces)."""
    set_active(obj)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.remove_doubles(threshold=1e-5)
    bpy.ops.mesh.delete_loose()
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode='OBJECT')


def voxel_heal(obj: bpy.types.Object, voxel_size: float) -> None:
    """Voxel-remesh obj into a single watertight manifold.  This 'heals' the
    non-manifold / self-intersecting / coplanar geometry left by the ball
    boolean unions, which is what QuadriFlow chokes on."""
    set_active(obj)
    obj.data.remesh_voxel_size = max(1e-4, voxel_size)
    obj.data.remesh_voxel_adaptivity = 0.0
    bpy.ops.object.voxel_remesh()


def decimate_to_faces(obj: bpy.types.Object, target_faces: int) -> None:
    """Collapse-decimate obj down to roughly target_faces."""
    set_active(obj)
    current = len(obj.data.polygons)
    ratio = 1.0 if current <= 0 else min(1.0, max(0.02, target_faces / float(current)))
    mod = obj.modifiers.new(name="Decimate", type='DECIMATE')
    mod.ratio = ratio
    bpy.ops.object.modifier_apply(modifier=mod.name)


def retopo_quadriflow(obj: bpy.types.Object, target_faces: int, smooth_normals: bool) -> bool:
    """Rebuild obj as clean all-quad topology via QuadriFlow. Returns success."""
    set_active(obj)
    try:
        res = bpy.ops.object.quadriflow_remesh(
            mode='FACES',
            target_faces=int(target_faces),
            use_preserve_sharp=False,
            use_preserve_boundary=False,
            smooth_normals=smooth_normals,
            use_mesh_symmetry=False,
        )
    except (RuntimeError, TypeError) as exc:
        print(f"  ! QuadriFlow raised: {exc}")
        return False
    # An operator can cancel without raising; also treat an empty result as fail.
    if 'FINISHED' not in res or len(obj.data.polygons) == 0:
        print("  ! QuadriFlow did not finish cleanly")
        return False
    return True


def retopologize(highpoly: bpy.types.Object, cfg: dict) -> bpy.types.Object:
    """
    Build a game-ready low-poly retopo from the highpoly.

    Duplicates the highpoly and cleans it, then rebuilds topology.  For the
    QuadriFlow path the mesh is first healed into a watertight manifold with a
    voxel remesh -- boolean-union artefacts from the balls otherwise make
    QuadriFlow fail -- and only then rebuilt as quads (with a decimate
    fallback).  The result is shrink-wrapped back onto the highpoly to keep
    the silhouette, then shaded smooth.
    """
    retopo = duplicate_object(highpoly, "GameAsset_Retopo")
    clean_mesh(retopo)

    voxel_size = cfg["retopo_voxel_size"]
    target = cfg["retopo_target_faces"]

    if cfg["retopo_method"] == "voxel":
        voxel_heal(retopo, voxel_size)
        if cfg["retopo_decimate_ratio"] < 1.0:
            decimate_to_faces(retopo, target)
    else:  # quadriflow
        # Heal first so QuadriFlow always gets a clean manifold (fixes balls).
        voxel_heal(retopo, voxel_size)
        if not retopo_quadriflow(retopo, target, cfg["retopo_smooth_normals"]):
            print("  ! Falling back to voxel remesh + decimate.")
            decimate_to_faces(retopo, target)

    if cfg["retopo_shrinkwrap"]:
        shrinkwrap_to(retopo, highpoly)

    # Mirror one half onto the other so the retopo is always symmetric.
    if cfg.get("retopo_symmetry_axis"):
        symmetrize_mesh(retopo, cfg["retopo_symmetry_axis"])

    recalc_normals(retopo)
    shade_smooth(retopo)

    if cfg["retopo_offset_x"]:
        retopo.location.x += cfg["retopo_offset_x"]

    return retopo


# ══════════════════════════════════════════════════════════════════════════════
#  RIGGING
# ══════════════════════════════════════════════════════════════════════════════

def build_rig(target: bpy.types.Object, p: dict, cfg: dict) -> bpy.types.Object:
    """
    Build an N-segment spine up the length of the asset (spine_0 at the base ..
    spine_{N-1} at the tip), skin `target` to it with automatic weights, then
    bend it by rotating the individual bones about X.

    The total bend (p["rig_x_bend"], which already includes the per-run random
    offset from rig_x_bend_random when variation > 0) is spread evenly across
    every bone *above* the base -- so the base stays planted and the shaft
    curves into an arc; per-bone entries in rig_bone_x_rotations override the
    share for named bones.  Returns the armature object.
    """
    shaft_len = p["shaft_length"]
    head_len  = p["head_length"]
    total_len = shaft_len + head_len
    skew_amt  = p["head_skew"] * head_len * p["head_skew_dir"]

    segments = max(2, int(cfg["rig_segments"]))
    # Joint positions up the centreline; the final joint is the (skewed) tip.
    joints = [(0.0, 0.0, total_len * i / segments) for i in range(segments + 1)]
    joints[-1] = (0.0, skew_amt, total_len)

    arm_data = bpy.data.armatures.new("GameAsset_Armature")
    arm_obj  = bpy.data.objects.new("GameAsset_Rig", arm_data)
    arm_obj.location = target.location.copy()   # match the (possibly offset) mesh
    bpy.context.collection.objects.link(arm_obj)

    # Build the connected bone chain in the armature's local space (+Z up).
    set_active(arm_obj)
    bpy.ops.object.mode_set(mode='EDIT')
    ebones = arm_data.edit_bones
    bone_names = []
    prev = None
    for i in range(segments):
        b = ebones.new(f"spine_{i}")
        b.head = joints[i]
        b.tail = joints[i + 1]
        if prev is not None:
            b.parent = prev
            b.use_connect = True
        prev = b
        bone_names.append(b.name)
    bpy.ops.object.mode_set(mode='OBJECT')

    # Skin the mesh to the armature with automatic (heat-map) weights.
    bpy.ops.object.select_all(action='DESELECT')
    target.select_set(True)
    arm_obj.select_set(True)
    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.parent_set(type='ARMATURE_AUTO')

    # Bend: distribute the total X bend across every bone above the base, and
    # apply any explicit per-bone overrides.  Each bone rotates relative to its
    # parent, so the shares accumulate into a smooth curve.
    overrides = cfg.get("rig_bone_x_rotations") or {}
    bendable = bone_names[1:]  # keep spine_0 (the base) planted
    share = math.radians(p["rig_x_bend"]) / len(bendable) if bendable else 0.0

    if share != 0.0 or overrides:
        set_active(arm_obj)
        bpy.ops.object.mode_set(mode='POSE')
        for name in bone_names:
            pbone = arm_obj.pose.bones.get(name)
            if pbone is None:
                continue
            pbone.rotation_mode = 'XYZ'
            if name in overrides:
                pbone.rotation_euler.x = math.radians(overrides[name])
            elif name in bendable:
                pbone.rotation_euler.x = share
        bpy.ops.object.mode_set(mode='OBJECT')

    return arm_obj


# ══════════════════════════════════════════════════════════════════════════════
#  GENERATION ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def generate(overrides: dict = None) -> bpy.types.Object:
    """Generate one asset. `overrides` is merged over DEFAULT_CONFIG, so
    callers only need to pass the keys they want to change."""
    cfg = {**DEFAULT_CONFIG, **(overrides or {})}

    rng = random.Random(cfg["seed"])
    p = randomise(cfg, rng)

    # Decide whether this build gets balls.  Draw from the same rng *before*
    # building so the choice is reproducible for a given seed.
    if p["balls_enabled"] is None:
        has_balls = rng.random() < p["balls_chance"]
    else:
        has_balls = bool(p["balls_enabled"])

    clear_scene()

    asset = build_shaft_and_head(p)
    if has_balls:
        for ball in build_balls(p):
            boolean_union(asset, ball)

    apply_subsurf(asset, p["subsurf_levels"])
    # Carve the tip slit after subsurf so it stays crisp/visible in the highpoly.
    if cfg["head_crevice"]:
        carve_head_crevice(asset, p, cfg)
    recalc_normals(asset)
    shade_smooth(asset)
    asset.name = "GameAsset_HighPoly"
    highpoly_polys = len(asset.data.polygons)

    # Optional game-ready retopology pass built from the highpoly.
    retopo = retopologize(asset, cfg) if cfg["retopo_enabled"] else None
    if retopo is not None and not cfg["retopo_keep_highpoly"]:
        bpy.data.objects.remove(asset, do_unlink=True)

    result = retopo if retopo is not None else asset

    # Optional rig: skin the game mesh to a bone chain and apply the X pose.
    rig = build_rig(result, p, cfg) if cfg["rig_enabled"] else None

    print("\n── Asset Report ──────────────────────────")
    print(f"  Seed         : {cfg['seed']}")
    print(f"  Variation    : {cfg['variation']:.2f}")
    print(f"  Shaft length : {p['shaft_length']:.4f} m")
    print(f"  Shaft radius : {p['shaft_radius']:.4f} m (top)")
    print(f"  Shaft flare  : {p['shaft_flare']:+.2f}  "
          f"(base {p['shaft_radius'] * (1.0 + p['shaft_flare']):.4f} m)")
    print(f"  Corona radius: {p['head_corona_radius']:.4f} m")
    print(f"  Balls        : {'yes' if has_balls else 'no'}")
    if has_balls:
        print(f"  Ball radius  : {p['ball_radius']:.4f} m")
    print(f"  Highpoly tris: {highpoly_polys}")
    if retopo is not None:
        print(f"  Retopo method: {cfg['retopo_method']}")
        print(f"  Retopo faces : {len(retopo.data.polygons)}")
    if rig is not None:
        print(f"  Rig          : {len(rig.data.bones)} bones, "
              f"X bend {p['rig_x_bend']:+.1f}° across the spine")
    print("──────────────────────────────────────────\n")

    return result


# ══════════════════════════════════════════════════════════════════════════════
#  GITHUB UPDATER
# ══════════════════════════════════════════════════════════════════════════════

UPDATER_USER_AGENT = "dildo-asset-generator-addon-updater"


class UpdateError(RuntimeError):
    pass


def addon_file_path() -> str:
    """Path to this addon file on disk."""
    return os.path.abspath(__file__)


def _marker_path() -> str:
    """Sibling marker file recording which commit is currently installed."""
    base = os.path.splitext(addon_file_path())[0]
    return base + ".installed_commit"


def get_installed_commit() -> str:
    path = _marker_path()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def _api_request(url: str, token: str = "", accept: str = "application/vnd.github+json",
                  timeout: float = 15.0) -> bytes:
    headers = {"User-Agent": UPDATER_USER_AGENT, "Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError(
                f"GitHub request failed (404): {url}\n"
                f"If this repository is private, GitHub returns 404 (not 403) for "
                f"unauthenticated requests. Add a personal access token with "
                f"read access to the repo in the addon preferences."
            ) from exc
        raise UpdateError(f"GitHub request failed ({exc.code}): {url}") from exc
    except urllib.error.URLError as exc:
        raise UpdateError(f"Network error reaching GitHub: {exc.reason}") from exc


def fetch_latest_file_commit(owner: str, repo: str, branch: str, path: str, token: str = ""):
    """Return (sha, short_message, iso_date) for the last commit that touched
    `path` on `branch` -- more precise than the branch tip, since unrelated
    commits elsewhere in the repo shouldn't report an update as available."""
    url = (
        f"https://api.github.com/repos/{owner}/{repo}/commits"
        f"?path={urllib.parse.quote(path)}&sha={urllib.parse.quote(branch)}&per_page=1"
    )
    data = json.loads(_api_request(url, token).decode("utf-8"))
    if not data:
        raise UpdateError(f"No commit history found for '{path}' on branch '{branch}'")
    commit = data[0]
    sha = commit["sha"]
    message = commit["commit"]["message"].splitlines()[0]
    date = commit["commit"]["author"]["date"]
    return sha, message, date


def download_and_install(owner: str, repo: str, path: str, sha: str, token: str = "") -> None:
    """Fetch `path` as it existed at `sha` and overwrite this addon file with it."""
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={sha}"
    content = _api_request(url, token, accept="application/vnd.github.raw")

    dest = addon_file_path()
    fd, tmp_path = tempfile.mkstemp(
        prefix="dildogen_", suffix=".py", dir=os.path.dirname(dest)
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.replace(tmp_path, dest)   # atomic on the same filesystem
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    with open(_marker_path(), "w", encoding="utf-8") as f:
        f.write(sha)


# ══════════════════════════════════════════════════════════════════════════════
#  ADDON PREFERENCES  (where to pull updates from)
# ══════════════════════════════════════════════════════════════════════════════

class ASSETGEN_AddonPreferences(AddonPreferences):
    bl_idname = __name__

    github_owner: StringProperty(name="Owner", default="wiguelsoares")
    github_repo: StringProperty(name="Repo", default="ClaudeBlender")
    github_branch: StringProperty(
        name="Branch", default="master"
    )
    repo_file_path: StringProperty(
        name="File path in repo",
        description="Path to this addon file inside the repository",
        default="Tools/dildo_asset_generator_addon.py",
    )
    github_token: StringProperty(
        name="Access token (optional)",
        description="Only needed if the repository is private",
        default="",
        subtype='PASSWORD',
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "github_owner")
        layout.prop(self, "github_repo")
        layout.prop(self, "github_branch")
        layout.prop(self, "repo_file_path")
        layout.prop(self, "github_token")


def _prefs(context) -> ASSETGEN_AddonPreferences:
    return context.preferences.addons[__name__].preferences


# ══════════════════════════════════════════════════════════════════════════════
#  LIVE UPDATE  (regenerate the viewport whenever a setting changes)
# ══════════════════════════════════════════════════════════════════════════════
#
# Property `update=` callbacks run in a restricted context where most bpy.ops
# calls are not safe to make directly.  So the callback just schedules a
# bpy.app.timers call (which runs in a normal, operator-safe context) on the
# next event-loop tick.  A pending flag debounces rapid slider drags into a
# single regeneration that reads whatever the settings are by the time it
# actually fires, instead of queuing a rebuild per pixel of mouse movement.

_live_update_pending = False


def _live_regenerate_timer():
    global _live_update_pending
    _live_update_pending = False
    try:
        s = bpy.context.scene.assetgen_settings
        if s.live_update:
            generate(_build_cfg(s))
    except Exception as exc:  # noqa: BLE001 - keep the live-update loop alive on bad input
        print(f"[Dildo Asset Generator] live update failed: {exc}")
    return None  # run once


def _on_prop_changed(self, context):
    global _live_update_pending
    if not self.live_update:
        return
    if not _live_update_pending:
        _live_update_pending = True
        bpy.app.timers.register(_live_regenerate_timer, first_interval=0.15)


# ══════════════════════════════════════════════════════════════════════════════
#  SETTINGS  (exposed generation parameters, mirroring DEFAULT_CONFIG)
# ══════════════════════════════════════════════════════════════════════════════

class ASSETGEN_Settings(PropertyGroup):
    show_advanced: BoolProperty(name="Show Advanced", default=False)
    live_update: BoolProperty(
        name="Live Update", default=False, update=_on_prop_changed,
        description=(
            "Regenerate the asset automatically whenever a value below changes. "
            "Heavy settings (QuadriFlow retopo, high segment counts, the rig's "
            "automatic weights) can make dragging feel laggy -- disable Retopo/"
            "Rig, or turn this off and use Generate Asset instead, if it's slow"
        ),
    )

    # ── Randomness ───────────────────────────────────────────────────────
    variation: FloatProperty(
        name="Variation", default=0.2, min=0.0, max=1.0, subtype='FACTOR',
        description="0 = fully deterministic, 1 = large swings", update=_on_prop_changed,
    )
    use_random_seed: BoolProperty(name="Random Seed Each Run", default=True, update=_on_prop_changed)
    seed_value: IntProperty(name="Seed", default=0, update=_on_prop_changed)

    # ── Shaft ────────────────────────────────────────────────────────────
    shaft_length: FloatProperty(name="Length", default=0.14, min=0.001, unit='LENGTH', update=_on_prop_changed)
    shaft_radius: FloatProperty(name="Radius", default=0.018, min=0.001, unit='LENGTH', update=_on_prop_changed)
    shaft_flare_min: FloatProperty(name="Flare Min", default=-0.06, update=_on_prop_changed)
    shaft_flare_max: FloatProperty(name="Flare Max", default=0.35, update=_on_prop_changed)

    # ── Head ─────────────────────────────────────────────────────────────
    head_length: FloatProperty(name="Length", default=0.045, min=0.001, unit='LENGTH', update=_on_prop_changed)
    head_corona_radius: FloatProperty(name="Corona Radius", default=0.021, min=0.0, unit='LENGTH', update=_on_prop_changed)
    head_tip_radius: FloatProperty(name="Tip Radius", default=0.003, min=0.0, unit='LENGTH', update=_on_prop_changed)
    head_corona_pos: FloatProperty(name="Corona Position", default=0.30, min=0.01, max=0.99, update=_on_prop_changed)
    head_sulcus_pos: FloatProperty(name="Sulcus Position", default=0.45, min=0.01, max=1.0, update=_on_prop_changed)
    head_sulcus_factor: FloatProperty(name="Sulcus Depth", default=0.92, min=0.1, max=1.0, update=_on_prop_changed)
    head_skew: FloatProperty(name="Tip Skew", default=-0.30, update=_on_prop_changed)
    head_skew_dir: FloatProperty(name="Skew Direction", default=0.30, update=_on_prop_changed)
    head_sulcus_tilt: FloatProperty(name="Sulcus Tilt", default=0.15, update=_on_prop_changed)

    # ── Crevice ──────────────────────────────────────────────────────────
    head_crevice: BoolProperty(name="Tip Crevice", default=True, update=_on_prop_changed)
    crevice_length: FloatProperty(name="Length", default=0.031, min=0.0, unit='LENGTH', update=_on_prop_changed)
    crevice_width: FloatProperty(name="Width", default=0.0020, min=0.0, unit='LENGTH', update=_on_prop_changed)
    crevice_depth: FloatProperty(name="Depth", default=0.006, min=0.0, unit='LENGTH', update=_on_prop_changed)
    crevice_y_bias: FloatProperty(name="-Y Bias", default=0.01, unit='LENGTH', update=_on_prop_changed)

    # ── Balls ────────────────────────────────────────────────────────────
    balls_mode: EnumProperty(
        name="Balls",
        items=[
            ('RANDOM', "Random", "Decide per run using Balls Chance"),
            ('ALWAYS', "Always", "Every generated asset has balls"),
            ('NEVER', "Never", "No balls"),
        ],
        default='RANDOM', update=_on_prop_changed,
    )
    balls_chance: FloatProperty(name="Chance", default=0.6, min=0.0, max=1.0, subtype='FACTOR', update=_on_prop_changed)
    ball_radius: FloatProperty(name="Radius", default=0.022, min=0.001, unit='LENGTH', update=_on_prop_changed)
    ball_spacing: FloatProperty(name="Spacing", default=0.014, min=0.0, unit='LENGTH', update=_on_prop_changed)
    ball_side_overlap: FloatProperty(name="Side Overlap", default=0.5, min=0.0, max=1.0, update=_on_prop_changed)

    # ── Rig ──────────────────────────────────────────────────────────────
    rig_enabled: BoolProperty(name="Build Rig", default=True, update=_on_prop_changed)
    rig_segments: IntProperty(name="Spine Bones", default=5, min=2, max=20, update=_on_prop_changed)
    rig_x_bend: FloatProperty(
        name="Base Bend", default=0.0, subtype='ANGLE',
        description="Total X bend distributed across the spine bones (the shaft curves)",
        update=_on_prop_changed,
    )
    rig_x_bend_random: FloatProperty(
        name="Random Bend ±", default=math.radians(25.0), subtype='ANGLE', min=0.0,
        description="Per-run random offset added to Base Bend when Variation > 0",
        update=_on_prop_changed,
    )

    # ── Mesh quality (advanced) ─────────────────────────────────────────
    profile_segments: IntProperty(name="Profile Segments", default=32, min=3, max=256, update=_on_prop_changed)
    radial_segments: IntProperty(name="Radial Segments", default=48, min=3, max=256, update=_on_prop_changed)
    ball_segments: IntProperty(name="Ball Segments", default=32, min=3, max=256, update=_on_prop_changed)
    subsurf_levels: IntProperty(name="Subsurf Levels", default=1, min=0, max=6, update=_on_prop_changed)

    # ── Retopology ───────────────────────────────────────────────────────
    retopo_enabled: BoolProperty(name="Build Retopo", default=True, update=_on_prop_changed)
    retopo_method: EnumProperty(
        name="Method",
        items=[
            ('QUADRIFLOW', "QuadriFlow", "Clean all-quad topology"),
            ('VOXEL', "Voxel", "Voxel remesh + decimate fallback"),
        ],
        default='QUADRIFLOW', update=_on_prop_changed,
    )
    retopo_target_faces: IntProperty(name="Target Faces", default=2000, min=50, max=200000, update=_on_prop_changed)
    retopo_voxel_size: FloatProperty(name="Voxel Size", default=0.006, min=0.0001, unit='LENGTH', update=_on_prop_changed)
    retopo_decimate_ratio: FloatProperty(name="Decimate Ratio", default=0.5, min=0.01, max=1.0, update=_on_prop_changed)
    retopo_shrinkwrap: BoolProperty(name="Shrinkwrap to Highpoly", default=True, update=_on_prop_changed)
    retopo_smooth_normals: BoolProperty(name="Smooth Normals (QuadriFlow)", default=True, update=_on_prop_changed)
    retopo_symmetry_axis: EnumProperty(
        name="Symmetrize",
        items=[
            ('POSITIVE_X', "+X", "Mirror the +X half onto -X"),
            ('NEGATIVE_X', "-X", "Mirror the -X half onto +X"),
            ('NONE', "Off", "Do not force symmetry"),
        ],
        default='POSITIVE_X', update=_on_prop_changed,
    )
    retopo_keep_highpoly: BoolProperty(name="Keep Highpoly", default=True, update=_on_prop_changed)
    retopo_offset_x: FloatProperty(name="Compare Offset X", default=0.12, unit='LENGTH', update=_on_prop_changed)

    # ── Update status (read-only display, refreshed by the check operator) ─
    latest_commit_sha: StringProperty(default="")
    latest_commit_msg: StringProperty(default="")
    latest_commit_date: StringProperty(default="")


def _build_cfg(s: ASSETGEN_Settings) -> dict:
    """Translate the addon's PropertyGroup into a DEFAULT_CONFIG-shaped dict."""
    return {
        "variation": s.variation,
        "seed": None if s.use_random_seed else s.seed_value,

        "shaft_length": s.shaft_length,
        "shaft_radius": s.shaft_radius,
        "shaft_flare_min": s.shaft_flare_min,
        "shaft_flare_max": s.shaft_flare_max,

        "head_length": s.head_length,
        "head_corona_radius": s.head_corona_radius,
        "head_tip_radius": s.head_tip_radius,
        "head_corona_pos": s.head_corona_pos,
        "head_sulcus_pos": s.head_sulcus_pos,
        "head_sulcus_factor": s.head_sulcus_factor,
        "head_skew": s.head_skew,
        "head_skew_dir": s.head_skew_dir,
        "head_sulcus_tilt": s.head_sulcus_tilt,

        "head_crevice": s.head_crevice,
        "crevice_length": s.crevice_length,
        "crevice_width": s.crevice_width,
        "crevice_depth": s.crevice_depth,
        "crevice_y_bias": s.crevice_y_bias,

        "balls_enabled": {"RANDOM": None, "ALWAYS": True, "NEVER": False}[s.balls_mode],
        "balls_chance": s.balls_chance,
        "ball_radius": s.ball_radius,
        "ball_spacing": s.ball_spacing,
        "ball_side_overlap": s.ball_side_overlap,

        "profile_segments": s.profile_segments,
        "radial_segments": s.radial_segments,
        "ball_segments": s.ball_segments,
        "subsurf_levels": s.subsurf_levels,

        "retopo_enabled": s.retopo_enabled,
        "retopo_method": s.retopo_method.lower(),
        "retopo_target_faces": s.retopo_target_faces,
        "retopo_voxel_size": s.retopo_voxel_size,
        "retopo_decimate_ratio": s.retopo_decimate_ratio,
        "retopo_shrinkwrap": s.retopo_shrinkwrap,
        "retopo_smooth_normals": s.retopo_smooth_normals,
        "retopo_symmetry_axis": "" if s.retopo_symmetry_axis == "NONE" else s.retopo_symmetry_axis,
        "retopo_keep_highpoly": s.retopo_keep_highpoly,
        "retopo_offset_x": s.retopo_offset_x,

        "rig_enabled": s.rig_enabled,
        "rig_segments": s.rig_segments,
        "rig_x_bend": math.degrees(s.rig_x_bend),
        "rig_x_bend_random": math.degrees(s.rig_x_bend_random),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  OPERATORS
# ══════════════════════════════════════════════════════════════════════════════

class ASSETGEN_OT_generate(Operator):
    bl_idname = "assetgen.generate"
    bl_label = "Generate Asset"
    bl_description = "Build a new asset in the scene using the settings below"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        cfg = _build_cfg(context.scene.assetgen_settings)
        try:
            generate(cfg)
        except Exception as exc:  # noqa: BLE001 - surface any bpy/generation error to the UI
            self.report({'ERROR'}, f"Generation failed: {exc}")
            return {'CANCELLED'}
        self.report({'INFO'}, "Asset generated")
        return {'FINISHED'}


class ASSETGEN_OT_reset_prop(Operator):
    bl_idname = "assetgen.reset_prop"
    bl_label = "Reset to Default"
    bl_description = "Reset this value to its default"
    bl_options = {'INTERNAL', 'UNDO'}

    prop_name: StringProperty()

    def execute(self, context):
        s = context.scene.assetgen_settings
        prop_rna = ASSETGEN_Settings.bl_rna.properties.get(self.prop_name)
        if prop_rna is None:
            self.report({'ERROR'}, f"Unknown property: {self.prop_name}")
            return {'CANCELLED'}
        setattr(s, self.prop_name, prop_rna.default)
        return {'FINISHED'}


class ASSETGEN_OT_check_update(Operator):
    bl_idname = "assetgen.check_update"
    bl_label = "Check for Updates"
    bl_description = "Ask GitHub for the latest commit that touched this addon file"

    def execute(self, context):
        prefs = _prefs(context)
        s = context.scene.assetgen_settings
        try:
            sha, msg, date = fetch_latest_file_commit(
                prefs.github_owner, prefs.github_repo, prefs.github_branch,
                prefs.repo_file_path, prefs.github_token,
            )
        except UpdateError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        s.latest_commit_sha = sha
        s.latest_commit_msg = msg
        s.latest_commit_date = date

        installed = get_installed_commit()
        if installed == sha:
            self.report({'INFO'}, f"Already up to date ({sha[:7]})")
        else:
            self.report({'INFO'}, f"Update available: {sha[:7]} - {msg}")
        return {'FINISHED'}


class ASSETGEN_OT_update_now(Operator):
    bl_idname = "assetgen.update_now"
    bl_label = "Update Now"
    bl_description = "Download the latest commit from GitHub and overwrite this addon file"

    def execute(self, context):
        prefs = _prefs(context)
        s = context.scene.assetgen_settings
        try:
            sha, msg, date = fetch_latest_file_commit(
                prefs.github_owner, prefs.github_repo, prefs.github_branch,
                prefs.repo_file_path, prefs.github_token,
            )
            download_and_install(
                prefs.github_owner, prefs.github_repo, prefs.repo_file_path,
                sha, prefs.github_token,
            )
        except UpdateError as exc:
            self.report({'ERROR'}, f"Update failed: {exc}")
            return {'CANCELLED'}

        s.latest_commit_sha = sha
        s.latest_commit_msg = msg
        s.latest_commit_date = date

        self.report(
            {'INFO'},
            f"Updated to {sha[:7]} ({msg}). Disable and re-enable this addon "
            f"(or restart Blender) to load the new code.",
        )
        return {'FINISHED'}


def _prop(layout, data, prop_name, **kwargs):
    """Draw a property with an inline "reset to default" button next to it."""
    row = layout.row(align=True)
    row.prop(data, prop_name, **kwargs)
    op = row.operator(ASSETGEN_OT_reset_prop.bl_idname, text="", icon='LOOP_BACK')
    op.prop_name = prop_name
    return row


# ══════════════════════════════════════════════════════════════════════════════
#  PANEL
# ══════════════════════════════════════════════════════════════════════════════

class ASSETGEN_PT_main(Panel):
    bl_idname = "ASSETGEN_PT_main"
    bl_label = "Dildo Asset Generator"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Asset Gen"

    def draw(self, context):
        layout = self.layout
        s = context.scene.assetgen_settings

        box = layout.box()
        box.label(text="Update", icon='FILE_REFRESH')
        row = box.row(align=True)
        row.operator(ASSETGEN_OT_check_update.bl_idname, icon='URL')
        row.operator(ASSETGEN_OT_update_now.bl_idname, icon='IMPORT')
        installed = get_installed_commit()
        box.label(text=f"Installed: {installed[:7] if installed else 'unknown'}")
        if s.latest_commit_sha:
            same = s.latest_commit_sha == installed
            icon = 'CHECKMARK' if same else 'ERROR'
            box.label(text=f"Latest: {s.latest_commit_sha[:7]}", icon=icon)
            box.label(text=s.latest_commit_msg)

        layout.separator()
        layout.operator(ASSETGEN_OT_generate.bl_idname, icon='MESH_CYLINDER')
        layout.prop(s, "live_update", icon='RADIOBUT_ON' if s.live_update else 'RADIOBUT_OFF')

        _prop(layout, s, "variation")
        row2 = layout.row(align=True)
        row2.prop(s, "use_random_seed")
        sub = _prop(row2, s, "seed_value", text="")
        sub.enabled = not s.use_random_seed

        layout.prop(s, "show_advanced")


class ASSETGEN_PT_shaft(Panel):
    bl_idname = "ASSETGEN_PT_shaft"
    bl_label = "Shaft"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_parent_id = "ASSETGEN_PT_main"

    def draw(self, context):
        s = context.scene.assetgen_settings
        layout = self.layout
        _prop(layout, s, "shaft_length")
        _prop(layout, s, "shaft_radius")
        _prop(layout, s, "shaft_flare_min")
        _prop(layout, s, "shaft_flare_max")


class ASSETGEN_PT_head(Panel):
    bl_idname = "ASSETGEN_PT_head"
    bl_label = "Head"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_parent_id = "ASSETGEN_PT_main"

    def draw(self, context):
        s = context.scene.assetgen_settings
        layout = self.layout
        _prop(layout, s, "head_length")
        _prop(layout, s, "head_corona_radius")
        _prop(layout, s, "head_tip_radius")
        _prop(layout, s, "head_skew")
        _prop(layout, s, "head_skew_dir")
        _prop(layout, s, "head_sulcus_tilt")
        if s.show_advanced:
            layout.separator()
            _prop(layout, s, "head_corona_pos")
            _prop(layout, s, "head_sulcus_pos")
            _prop(layout, s, "head_sulcus_factor")


class ASSETGEN_PT_crevice(Panel):
    bl_idname = "ASSETGEN_PT_crevice"
    bl_label = "Tip Crevice"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_parent_id = "ASSETGEN_PT_main"

    def draw_header(self, context):
        self.layout.prop(context.scene.assetgen_settings, "head_crevice", text="")

    def draw(self, context):
        s = context.scene.assetgen_settings
        layout = self.layout
        layout.enabled = s.head_crevice
        _prop(layout, s, "crevice_length")
        _prop(layout, s, "crevice_width")
        _prop(layout, s, "crevice_depth")
        _prop(layout, s, "crevice_y_bias")


class ASSETGEN_PT_balls(Panel):
    bl_idname = "ASSETGEN_PT_balls"
    bl_label = "Balls"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_parent_id = "ASSETGEN_PT_main"

    def draw(self, context):
        s = context.scene.assetgen_settings
        layout = self.layout
        _prop(layout, s, "balls_mode")
        sub = _prop(layout, s, "balls_chance")
        sub.enabled = s.balls_mode == 'RANDOM'
        _prop(layout, s, "ball_radius")
        _prop(layout, s, "ball_spacing")
        if s.show_advanced:
            _prop(layout, s, "ball_side_overlap")


class ASSETGEN_PT_rig(Panel):
    bl_idname = "ASSETGEN_PT_rig"
    bl_label = "Rig"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_parent_id = "ASSETGEN_PT_main"

    def draw_header(self, context):
        self.layout.prop(context.scene.assetgen_settings, "rig_enabled", text="")

    def draw(self, context):
        s = context.scene.assetgen_settings
        layout = self.layout
        layout.enabled = s.rig_enabled
        _prop(layout, s, "rig_segments")
        _prop(layout, s, "rig_x_bend")
        _prop(layout, s, "rig_x_bend_random")


class ASSETGEN_PT_retopo(Panel):
    bl_idname = "ASSETGEN_PT_retopo"
    bl_label = "Retopology"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_parent_id = "ASSETGEN_PT_main"

    def draw_header(self, context):
        self.layout.prop(context.scene.assetgen_settings, "retopo_enabled", text="")

    def draw(self, context):
        s = context.scene.assetgen_settings
        layout = self.layout
        layout.enabled = s.retopo_enabled
        _prop(layout, s, "retopo_method")
        _prop(layout, s, "retopo_target_faces")
        _prop(layout, s, "retopo_symmetry_axis")
        _prop(layout, s, "retopo_keep_highpoly")
        if s.show_advanced:
            layout.separator()
            _prop(layout, s, "retopo_voxel_size")
            _prop(layout, s, "retopo_decimate_ratio")
            _prop(layout, s, "retopo_shrinkwrap")
            _prop(layout, s, "retopo_smooth_normals")
            _prop(layout, s, "retopo_offset_x")
            layout.separator()
            layout.label(text="Mesh Quality")
            _prop(layout, s, "profile_segments")
            _prop(layout, s, "radial_segments")
            _prop(layout, s, "ball_segments")
            _prop(layout, s, "subsurf_levels")


# ══════════════════════════════════════════════════════════════════════════════
#  REGISTRATION
# ══════════════════════════════════════════════════════════════════════════════

classes = (
    ASSETGEN_AddonPreferences,
    ASSETGEN_Settings,
    ASSETGEN_OT_generate,
    ASSETGEN_OT_reset_prop,
    ASSETGEN_OT_check_update,
    ASSETGEN_OT_update_now,
    ASSETGEN_PT_main,
    ASSETGEN_PT_shaft,
    ASSETGEN_PT_head,
    ASSETGEN_PT_crevice,
    ASSETGEN_PT_balls,
    ASSETGEN_PT_rig,
    ASSETGEN_PT_retopo,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.assetgen_settings = bpy.props.PointerProperty(type=ASSETGEN_Settings)


def unregister():
    del bpy.types.Scene.assetgen_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
