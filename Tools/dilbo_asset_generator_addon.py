"""
Dilbo Asset Generator -- single-file Blender addon.

Procedurally builds a game-ready, cylinder-based organic asset (lathed
shaft + glans head, optional meatus crevice, optional balls), an optional
game-ready retopology pass, and an optional bendable bone rig -- all driven
from a sidebar panel (View3D > Sidebar > "Asset Gen" tab). Includes a
one-click updater that pulls this file's latest committed version straight
from GitHub.

Install: Edit > Preferences > Add-ons > Install..., point at this .py file
(or drop it directly into your Blender scripts/addons folder) and enable
"Dilbo Asset Generator".

To update after a change is pushed to the tracked branch, use the "Check
for Updates" / "Update Now" buttons at the top of the panel -- no need to
reinstall by hand. After an update, disable and re-enable the addon (or
restart Blender) so the new code is loaded.
"""

import bisect
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
import mathutils
from mathutils.bvhtree import BVHTree
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import AddonPreferences, Operator, Panel, PropertyGroup

bl_info = {
    "name": "Dilbo Asset Generator",
    "author": "Drone project",
    "version": (1, 2, 0),
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
    "head_enabled": True,        # True/False/None like balls_enabled -- None =
                                  #   random per run using head_chance
    "head_chance": 0.85,         # probability of a head when head_enabled is None
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
    "head_crevice": True,        # True/False/None like balls_enabled -- None =
                                  #   random per run using crevice_chance (only
                                  #   rolled when a head is actually present)
    "crevice_chance": 0.7,
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

    # ── Knot (mid-shaft bulge) ───────────────────────────────────────────────
    # A UV sphere centred on the shaft axis, boolean-unioned into the shaft
    # partway up its length -- same True/False/None (random via knot_chance)
    # semantics as balls_enabled.
    "knot_enabled": None,
    "knot_chance": 0.35,
    "knot_position": 0.55,       # fraction of shaft_length from the base
    "knot_radius": 0.026,
    "knot_segments": 32,

    # ── Suction cup (base attachment) ────────────────────────────────────────
    # A lathed flange profile that merges into the shaft's flat base -- same
    # True/False/None (random via cup_chance) semantics as balls_enabled.
    "cup_enabled": None,
    "cup_chance": 0.35,
    "cup_radius": 0.030,         # widest point of the flange (the rim)
    "cup_tip_radius": 0.004,     # small radius at the centre of the concave underside
    "cup_height": 0.014,         # depth of the rim below the shaft join
    "cup_flange_pos": 0.55,      # fraction of the profile where the rim sits
    "cup_concavity": 0.5,        # 0 = flat-bottomed disc, up to ~0.95 = deeply
                                  #   cupped (underside centre recessed toward
                                  #   the shaft, like a real suction cup)
    "cup_rim_thickness": 0.004,  # radius of the rounded rim fillet -- without
                                  #   this the rim is a knife-edge point, too
                                  #   thin for the retopo remesher to hold onto

    # ── Randomness ──────────────────────────────────────────────────────────
    "variation": 0.2,            # 0.0 = fully deterministic, 1.0 = large swings
    "seed": None,                # integer for reproducible results; None = new
                                  #   random shape every run

    # ── Random curve (baked into the mesh, independent of the Rig) ──────────
    # A Simple Deform Bend applied to the finished highpoly.  Unlike the Rig's
    # rig_x_bend/rig_x_bend_random (which only bend the *pose*, and need the
    # Rig enabled), this bends the rest geometry itself -- works with the Rig
    # off, and stacks with it if both are on.
    "curve_enabled": False,      # True/False/None like balls_enabled -- None =
                                  #   random per run using curve_chance
    "curve_chance": 0.3,
    "curve_angle_max": 35.0,     # degrees; a random angle in [-this, +this] is
                                  #   drawn each run whenever the curve is active

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
    "retopo_enabled": True,       # True/False/None like balls_enabled -- None =
                                   #   random per run using retopo_chance
    "retopo_chance": 0.9,
    "retopo_method": "grid",        # "grid" (purpose-built quad-grid lathe,
                                     #   shrinkwrapped onto the highpoly -- the
                                     #   default), "quadriflow" (auto-remesh,
                                     #   legacy) or "voxel" (remesh + decimate,
                                     #   legacy fallback)
    "retopo_grid_profile_segments": 40,  # vertical resolution of the grid lathe
    "retopo_grid_radial_segments": 48,   # segments around the grid lathe
    "retopo_grid_ball_segments": 32,     # sphere resolution for the merged-in balls
    "retopo_target_faces": 2000,    # quad target for quadriflow (game budget)
    "retopo_voxel_size": 0.002,     # voxel size (m) for the voxel-heal pass and
                                     #   the voxel method -- fine enough to keep
                                     #   thin details (cup) from collapsing
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
    "retopo_uv_unwrap": True,       # mark seams by part (balls / cup / head / shaft)
                                     #   and unwrap into up to 4 clean islands
    "retopo_uv_margin": 0.02,       # island margin between UV islands

    # ── Rigging ─────────────────────────────────────────────────────────────
    # A multi-segment spine is built up the length of the asset (bones named
    # spine_0 at the base .. spine_N-1 at the tip) and the game mesh is skinned
    # to it with automatic weights, so it exports to Unreal as a skeletal mesh.
    "rig_enabled": True,          # True/False/None like balls_enabled -- None =
                                   #   random per run using rig_chance
    "rig_chance": 0.9,
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

    # ── Batch (internal) ─────────────────────────────────────────────────────
    "batch_offset": 0.0,            # world-space X shift for this asset; set by
                                     #   the batch loop, not exposed in the UI
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


def _resolve_tristate(value, chance: float, rng: random.Random) -> bool:
    """value is True/False/None -- None (Random mode) rolls the dice using
    chance; True/False (Always/Never) pass straight through."""
    if value is None:
        return rng.random() < chance
    return bool(value)


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
    # Independent of `variation` -- when active this always draws a fresh
    # angle, the same way "Random Seed Each Run" ignores variation too.
    has_curve = _resolve_tristate(cfg["curve_enabled"], cfg["curve_chance"], rng)
    curve_angle = (
        rng.uniform(-cfg["curve_angle_max"], cfg["curve_angle_max"])
        if has_curve else 0.0
    )
    return {
        **cfg,
        "shaft_flare":       shaft_flare,
        "rig_x_bend":        rig_x_bend,
        "curve_angle":       curve_angle,
        "shaft_length":      jitter(cfg["shaft_length"],      0.20 * v, rng),
        "shaft_radius":      jitter(cfg["shaft_radius"],      0.15 * v, rng),
        "head_length":       jitter(cfg["head_length"],       0.25 * v, rng),
        "head_corona_radius":jitter(cfg["head_corona_radius"],0.15 * v, rng),
        "head_tip_radius":   jitter(cfg["head_tip_radius"],   0.50 * v, rng),
        "head_corona_pos":   jitter(cfg["head_corona_pos"],   0.15 * v, rng),
        "head_sulcus_pos":   jitter(cfg["head_sulcus_pos"],   0.20 * v, rng),
        "head_sulcus_factor":jitter(cfg["head_sulcus_factor"],0.15 * v, rng),
        "head_skew":         jitter(cfg["head_skew"],         0.40 * v, rng),
        "head_skew_dir":     jitter(cfg["head_skew_dir"],     0.35 * v, rng),
        "head_sulcus_tilt":  jitter(cfg["head_sulcus_tilt"],  0.40 * v, rng),
        "crevice_length":    jitter(cfg["crevice_length"],    0.25 * v, rng),
        "crevice_width":     jitter(cfg["crevice_width"],     0.25 * v, rng),
        "crevice_depth":     jitter(cfg["crevice_depth"],     0.30 * v, rng),
        "crevice_y_bias":    jitter(cfg["crevice_y_bias"],    0.35 * v, rng),
        "ball_radius":       jitter(cfg["ball_radius"],       0.20 * v, rng),
        "ball_spacing":      jitter(cfg["ball_spacing"],      0.30 * v, rng),
        "ball_side_overlap": jitter(cfg["ball_side_overlap"], 0.20 * v, rng),
        "knot_position":     jitter(cfg["knot_position"],     0.25 * v, rng),
        "knot_radius":       jitter(cfg["knot_radius"],       0.20 * v, rng),
        "cup_radius":        jitter(cfg["cup_radius"],        0.20 * v, rng),
        "cup_height":        jitter(cfg["cup_height"],        0.20 * v, rng),
        "cup_tip_radius":    jitter(cfg["cup_tip_radius"],    0.30 * v, rng),
        "cup_flange_pos":    jitter(cfg["cup_flange_pos"],    0.15 * v, rng),
        "cup_rim_thickness": jitter(cfg["cup_rim_thickness"], 0.15 * v, rng),
        "cup_concavity":     jitter(cfg["cup_concavity"],     0.20 * v, rng),
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

def ball_centers_and_radius(p: dict) -> tuple:
    """(centers, radius) of the two balls in local space -- shared by
    build_balls() and any code (retopo/UV) that needs to know where they
    are without actually building them."""
    ball_r       = p["ball_radius"]
    half_spacing = p["ball_spacing"] * 0.5
    z_center     = ball_r * 0.01  # keep centre just above z=0
    # Offset from the actual shaft wall at the ball height, so a flared (wider)
    # base still gets a clean overlap instead of the balls sinking inside.
    wall_r       = shaft_and_head_radius(z_center, p)
    side_offset  = wall_r + ball_r * (1.0 - p["ball_side_overlap"])
    centers = [
        (-half_spacing, -side_offset, z_center),
        (half_spacing, -side_offset, z_center),
    ]
    return centers, ball_r


def build_balls(p: dict, segments: int = None, trim_z: float = 0.0) -> list:
    """
    Two hemisphere bumps against the shaft side, built as a single combined
    solid rather than two independently-trimmed spheres.

    The two full, untrimmed spheres deeply overlap each other (that's what
    makes them read as one continuous sac), so trimming each one flush
    with the base *separately* leaves two coplanar flat discs overlapping
    each other right where the balls meet -- a classic hard case for the
    EXACT boolean solver, visible as thin sliver artefacts along the seam
    between them. Unioning the two full spheres together FIRST, then
    trimming the *combined* shape flush with a single half-space box
    INTERSECT (never a manual bisect + flat n-gon fill), leaves only one
    flat cut plane instead of two overlapping ones. The later union onto
    the shaft then only ever composes two already-closed,
    curvature-consistent solids, the same way the knot's full sphere does.

    Returns a single-item list (kept as a list since callers union
    whatever's returned in a loop). Pass `segments` to override
    p["ball_segments"] (used to build a lower-res pair for the grid
    retopo). Pass `trim_z` to raise the trim plane above world z=0 by that
    much -- needed only for the low-poly grid-retopo merge when there's no
    suction cup: the retopo's own flat base cap then sits exactly at
    z=0 too, so a trim flush with z=0 leaves the ball's cut face and the
    base cap's boundary ring sitting coplanar right on top of each other,
    the same kind of near-tangent case the flush trim above was written to
    avoid -- just one level up. Left at 0.0 (still exactly flush) for the
    highpoly path, which has no such coplanar neighbour to conflict with.
    """
    centers, ball_r = ball_centers_and_radius(p)
    segs = segments if segments is not None else p["ball_segments"]

    spheres = []
    for center, label in zip(centers, ("L", "R")):
        bpy.ops.mesh.primitive_uv_sphere_add(
            radius=ball_r,
            segments=segs,
            ring_count=max(8, segs // 2),
            location=center,
        )
        obj = bpy.context.active_object
        obj.name = f"Ball_{label}"
        spheres.append(obj)

    combined, other = spheres
    boolean_union(combined, other)
    combined.name = "Balls"

    # Half-space cutter: a box whose bottom face sits at world z=trim_z
    # (exactly z=0 by default), comfortably larger than the combined pair
    # in every other direction. Boolean modifiers compare geometry in
    # world space, so this trims to the base plane regardless of the
    # balls' own local coordinate origin.
    cx = (centers[0][0] + centers[1][0]) / 2.0
    cy = (centers[0][1] + centers[1][1]) / 2.0
    box_size = (abs(centers[0][0] - centers[1][0]) + ball_r * 2.0) * 3.0
    bpy.ops.mesh.primitive_cube_add(size=box_size, location=(cx, cy, trim_z + box_size / 2.0))
    half_space = bpy.context.active_object
    half_space.name = "BallsHalfSpace"

    set_active(combined)
    mod = combined.modifiers.new(name="TrimBelowBase", type='BOOLEAN')
    mod.operation = 'INTERSECT'
    mod.object = half_space
    mod.solver = 'EXACT'
    bpy.ops.object.modifier_apply(modifier=mod.name)
    bpy.data.objects.remove(half_space, do_unlink=True)

    # Triangulate any n-gons left by the booleans (the ball-ball union
    # seam and/or the flat cut) so the mesh is quads/triangles only.
    bm = bmesh.new()
    bm.from_mesh(combined.data)
    ngons = [f for f in bm.faces if len(f.verts) > 4]
    if ngons:
        bmesh.ops.triangulate(bm, faces=ngons, quad_method='BEAUTY', ngon_method='BEAUTY')
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(combined.data)
    bm.free()
    combined.data.update()

    return [combined]


# ══════════════════════════════════════════════════════════════════════════════
#  KNOT
# ══════════════════════════════════════════════════════════════════════════════

def build_knot(p: dict) -> bpy.types.Object:
    """A UV sphere centred on the shaft axis, boolean-unioned in to form a
    knot-style bulge partway up the shaft (position/radius are randomised
    the same way everything else is)."""
    z = p["knot_position"] * p["shaft_length"]
    bpy.ops.mesh.primitive_uv_sphere_add(
        radius=p["knot_radius"],
        segments=p["knot_segments"],
        ring_count=max(8, p["knot_segments"] // 2),
        location=(0.0, 0.0, z),
    )
    obj = bpy.context.active_object
    obj.name = "Knot"
    return obj


# ══════════════════════════════════════════════════════════════════════════════
#  SUCTION CUP
# ══════════════════════════════════════════════════════════════════════════════

def suction_cup_profile(t: float, p: dict) -> tuple:
    """(r, z) of the cup's lathed profile at parameter t in [0, 1].

    Unlike the shaft/head profile (radius as a function of height), a real
    suction cup's outline folds back on itself in Z, so this is parametrized
    by arc position instead of height: t=0 is the centre of the concave
    underside (recessed *up*, toward the shaft) -> a rounded rim fillet
    (the widest ring AND the lowest point -- what would touch a surface,
    given genuine thickness via cup_rim_thickness rather than meeting the
    underside and the flange wall at a knife-edge point, which is too thin
    for the retopo remesher to hold onto) -> t=1 is the neck blending into
    the shaft's own base radius.
    """
    flange_r  = p["cup_radius"]
    tip_r     = p["cup_tip_radius"]
    neck_r    = shaft_and_head_radius(0.0, p)
    depth     = p["cup_height"]
    concavity = max(0.0, min(0.95, p["cup_concavity"]))
    flange_pos = max(1e-3, min(1.0 - 1e-3, p["cup_flange_pos"]))
    rim_thickness = max(0.0, min(p["cup_rim_thickness"], flange_r * 0.45, depth * 0.45))

    rim_z    = -depth
    centre_z = -depth * (1.0 - concavity)   # recessed toward the shaft join

    # The fillet occupies a small band of t straddling flange_pos; the
    # underside and flange-wall legs are shortened to meet its two ends
    # instead of meeting each other directly.
    half = min(0.05, flange_pos * 0.5, (1.0 - flange_pos) * 0.5) if rim_thickness > 0.0 else 0.0
    t1, t2 = flange_pos - half, flange_pos + half

    if t <= t1:
        u = smoothstep(t / t1) if t1 > 0.0 else 1.0
        return tip_r + (flange_r - rim_thickness - tip_r) * u, centre_z + (rim_z - centre_z) * u

    if t < t2:
        # Quarter-circle fillet: from pointing straight down (meets the
        # underside) to pointing straight out (meets the flange wall).
        s = (t - t1) / (t2 - t1)
        angle = -math.pi / 2.0 + s * (math.pi / 2.0)
        fillet_r = flange_r - rim_thickness
        fillet_z = rim_z + rim_thickness
        return fillet_r + rim_thickness * math.cos(angle), fillet_z + rim_thickness * math.sin(angle)

    u = smoothstep((t - t2) / (1.0 - t2)) if t2 < 1.0 else 1.0
    return flange_r + (neck_r - flange_r) * u, (rim_z + rim_thickness) + (0.0 - (rim_z + rim_thickness)) * u


def build_suction_cup(p: dict) -> bpy.types.Object:
    """A lathed concave-dish profile merged into the shaft's flat base, the
    same way the shaft/head profile is built (spin a revolve profile)."""
    bm = bmesh.new()
    verts = []
    for i in range(p["profile_segments"] + 1):
        t = i / p["profile_segments"]
        r, z = suction_cup_profile(t, p)
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
    bmesh.ops.holes_fill(bm, edges=bm.edges, sides=0)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)

    mesh = bpy.data.meshes.new("SuctionCupMesh")
    bm.to_mesh(mesh)
    bm.free()

    obj = bpy.data.objects.new("SuctionCup", mesh)
    bpy.context.collection.objects.link(obj)
    return obj


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
    if levels <= 0:
        return  # a 0-level Subsurf is a no-op; Blender refuses to "apply" it
    set_active(obj)
    mod = obj.modifiers.new(name="Subsurf", type='SUBSURF')
    mod.levels = levels
    mod.render_levels = levels
    bpy.ops.object.modifier_apply(modifier=mod.name)


def apply_random_curve(obj: bpy.types.Object, angle_deg: float) -> None:
    """Bend the whole mesh into an arc via a baked Simple Deform modifier --
    a shape-level curve, distinct from the Rig's pose-space bend (this
    affects the rest mesh itself, so it shows even without a rig, and
    stacks with the rig's bend if both are enabled)."""
    set_active(obj)
    mod = obj.modifiers.new(name="RandomCurve", type='SIMPLE_DEFORM')
    mod.deform_method = 'BEND'
    mod.deform_axis = 'Z'
    mod.angle = math.radians(angle_deg)
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


def shrinkwrap_to(obj: bpy.types.Object, target: bpy.types.Object, exclude_vert_idx: set = None) -> None:
    """Snap obj's verts onto the target surface and apply, to recover volume.

    exclude_vert_idx (if given) leaves those vertices exactly where they
    already are instead of shrinkwrapping them too -- see the ball-merge
    self-intersection note on the caller in retopologize() for why this
    matters near the balls specifically. Implemented as a vertex group
    (weight 1 for every vertex that *should* move, none for the excluded
    ones) driving the modifier's own influence, not a post-hoc undo, so
    excluded vertices are never touched by the nearest-surface-point search
    in the first place."""
    set_active(obj)
    mod = obj.modifiers.new(name="Shrinkwrap", type='SHRINKWRAP')
    mod.target = target
    mod.wrap_method = 'NEAREST_SURFACEPOINT'
    vg_name = None
    if exclude_vert_idx:
        vg = obj.vertex_groups.new(name="_ShrinkwrapMask")
        vg_name = vg.name
        included = [v.index for v in obj.data.vertices if v.index not in exclude_vert_idx]
        vg.add(included, 1.0, 'REPLACE')
        mod.vertex_group = vg_name
    bpy.ops.object.modifier_apply(modifier=mod.name)
    if vg_name is not None:
        # Re-look-up by name rather than reusing the `vg` reference from
        # before the apply: modifier_apply can reallocate the mesh's
        # vertex-group data under the hood, leaving that Python reference
        # stale -- passing it to vertex_groups.remove() then raises
        # "DeformGroup not in object" even though the group (now under a
        # fresh internal pointer, same name) is still right there.
        stale = obj.vertex_groups.get(vg_name)
        if stale is not None:
            obj.vertex_groups.remove(stale)


def symmetrize_mesh(obj: bpy.types.Object, direction: str) -> None:
    """Mirror one half of obj onto the other so the topology is exactly
    symmetric across the mesh's local mirror plane (X=0 for this asset)."""
    set_active(obj)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.symmetrize(direction=direction)
    bpy.ops.mesh.remove_doubles(threshold=1e-5)   # weld the seam at X=0
    bpy.ops.object.mode_set(mode='OBJECT')


def add_support_loop(obj: bpy.types.Object, z: float) -> None:
    """Bisect the mesh at height z, inserting a clean edge loop there without
    removing anything.  Auto-remeshers (QuadriFlow/voxel+decimate) don't know
    to preserve quad flow across a hard curvature change like the knot's
    boundary, so this forces one in after the fact instead of hoping the
    remesh happens to land a loop there."""
    set_active(obj)
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bmesh.ops.bisect_plane(
        bm, geom=list(bm.verts) + list(bm.edges) + list(bm.faces),
        plane_co=(0.0, 0.0, z), plane_no=(0.0, 0.0, 1.0),
        clear_inner=False, clear_outer=False,
    )
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()


def _stash_original_uv_params(obj: bpy.types.Object) -> None:
    """Record every vertex's current (theta, z) into custom float vertex
    layers ("_orig_theta", "_orig_z") while the mesh is still its pristine
    lathed shape -- straight up the Z axis, no bend, no bulge -- so the
    canonical UV pass (see _assign_cylindrical_canonical_uv) can use these
    instead of the vertex's *final* position. Ball-merge, shrinkwrap and
    the random curve bend are all still ahead in the pipeline at the point
    this is called; without stashing beforehand, the same real point on
    two assets with different (random) curve bend angles would land at
    different final Z heights, and the checker/tiling pattern would show
    visibly non-matching rows between them even though the underlying
    formula is scale-correct for each mesh individually -- a texture
    correctly wrapping two *actually different* bent shapes just doesn't
    look aligned side by side. Reading the pre-bend parametrization instead
    keeps the pattern rectilinear and aligned regardless of pose."""
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    theta_layer = bm.verts.layers.float.new("_orig_theta")
    z_layer = bm.verts.layers.float.new("_orig_z")
    for v in bm.verts:
        v[theta_layer] = math.atan2(v.co.y, v.co.x)
        v[z_layer] = v.co.z
    bm.to_mesh(obj.data)
    bm.free()


def build_grid_retopo(p: dict, cfg: dict, has_cup: bool) -> bpy.types.Object:
    """Build a clean quad-grid revolve mesh -- shaft + head, and the cup's
    own profile if present, as one continuous lathe -- at game-appropriate
    resolution, the same way the highpoly itself is built. This gives a
    guaranteed grid of quads instead of hoping QuadriFlow/voxel-remesh
    happens to produce one. The mesh only provides the base grid structure;
    shrinkwrapping it onto the highpoly afterwards is what picks up the true
    (possibly asymmetric) surface, so this profile never needs to know
    about the knot. Both ends are left open -- cap_ends_with_quads
    runs afterwards, once the body has already been shrinkwrapped, so the
    pole caps' own tiny inner rings are built directly from (and never
    displaced off of) the true surface instead of being independently
    shrinkwrapped themselves."""
    profile_segs = max(3, int(cfg["retopo_grid_profile_segments"]))
    radial_segs = max(3, int(cfg["retopo_grid_radial_segments"]))

    bm = bmesh.new()
    verts = []

    if has_cup:
        cup_segs = max(4, profile_segs // 2)
        for i in range(cup_segs + 1):
            t = i / cup_segs
            r, z = suction_cup_profile(t, p)
            verts.append(bm.verts.new((r, 0.0, z)))

    total_length = p["shaft_length"] + p["head_length"]
    for i in range(profile_segs + 1):
        t = i / profile_segs
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
        steps=radial_segs,
        use_duplicate=False,
    )
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)

    mesh = bpy.data.meshes.new("GridRetopoMesh")
    bm.to_mesh(mesh)
    bm.free()

    obj = bpy.data.objects.new("GameAsset_Retopo", mesh)
    bpy.context.collection.objects.link(obj)

    return obj


def _ordered_boundary_loops(bm: bmesh.types.BMesh) -> list:
    """Return each open-boundary loop (1 linked face) as an ordered cyclic
    list of BMVerts, walking the loop rather than just grouping its edges
    -- needed so the loop's angular order can be mapped onto the pole
    cap's own perimeter walk."""
    boundary_edges = {e for e in bm.edges if len(e.link_faces) == 1}
    visited = set()
    loops = []
    while boundary_edges - visited:
        start = next(iter(boundary_edges - visited))
        v0, v1 = start.verts
        loop = [v0, v1]
        visited.add(start)
        current = v1
        while True:
            nxt_edge = None
            for e in current.link_edges:
                if e in boundary_edges and e not in visited:
                    nxt_edge = e
                    break
            if nxt_edge is None:
                break
            visited.add(nxt_edge)
            nxt = nxt_edge.other_vert(current)
            if nxt is loop[0]:
                break
            loop.append(nxt)
            current = nxt
        loops.append(loop)
    return loops


def _bridge_closed_loops(bm: bmesh.types.BMesh, inner: list, outer: list) -> None:
    """Bridge two closed vertex loops (already matched for winding
    direction and rough start alignment) with a ring of triangles, even
    when they have different vertex counts and that difference isn't a
    clean multiple -- walks both loops forward together, always advancing
    whichever loop is further behind in fractional progress around the
    ring, so every vertex on both loops is guaranteed to end up referenced
    by at least one new face. (A fixed "N quads + 1 closing triangle per
    step" assumption -- fine when the outer loop is known to have exactly
    4 more vertices than the inner one -- silently drops vertices whenever
    that assumption doesn't hold, e.g. len(outer) not landing on a clean
    multiple of len(inner), leaving them permanently unconnected to any
    new face and the mesh non-watertight.)"""
    ni, no = len(inner), len(outer)
    if ni == 0 or no == 0:
        return
    if ni == 1:
        v0 = inner[0]
        for j in range(no):
            bm.faces.new([v0, outer[j], outer[(j + 1) % no]])
        return
    i = j = 0
    while i < ni or j < no:
        prog_i = (i + 1) / ni
        prog_j = (j + 1) / no
        if j >= no or (i < ni and prog_i <= prog_j):
            bm.faces.new([inner[i % ni], inner[(i + 1) % ni], outer[j % no]])
            i += 1
        else:
            bm.faces.new([inner[i % ni], outer[j % no], outer[(j + 1) % no]])
            j += 1


def build_diamond_pole_cap(bm: bmesh.types.BMesh, boundary_verts_ordered: list, pole_co: tuple) -> None:
    """Cap an open boundary loop with a diamond/quad-sphere pole grid: ring
    N (graph distance N from the pole) has exactly 4*N vertices, so the
    cap grows gradually and evenly out from a single pole vertex instead
    of Grid Fill's uneven single-point spiral convergence. The pole itself
    closes with 4 triangles (one per quadrant) rather than forced quads --
    a small, standard, watertight-by-construction pole fan. Every interior
    face is a quad; only the outermost ring (bridging the last synthetic
    ring to the real boundary loop) may be triangles, since the boundary's
    actual vertex count is whatever it happens to be, not necessarily a
    multiple of 4 -- see _bridge_closed_loops.

    Every interior vertex is a straight 3D lerp from the pole toward the
    *real* boundary position in its own angular direction (interpolated
    between the two neighbouring boundary verts), rather than toward an
    idealized flat circle built from the boundary's averaged centre/radius
    -- that averaged-circle approach assumes the boundary is flat and
    round, true right after this mesh's own lathe build but not once it's
    been shrinkwrapped onto the highpoly.

    Deliberately a pure lerp, not snapped to the highpoly surface: an
    earlier version individually BVH-snapped each interior point to its
    nearest surface point for a closer approximation of a convex dome's
    true curvature, but that's provably unsafe -- right next to the pole
    "nearest surface point" is ambiguous/unstable for a tight dome tip,
    letting neighbouring ring points snap to crossing surface locations,
    and right next to the real boundary an over-eager snap can pull a
    point away from the position its neighbours in the closing bridge
    expect it to line up with, folding a bridge triangle back into the
    adjacent regular grid quad. Both failure modes are locally still
    2-manifold (the usual boundary-edge/manifold checks miss them) but
    reliably broke Blender's automatic rig weight solver outright across
    the *entire* mesh once they occurred anywhere on it. A pure radial-fan
    lerp can't self-intersect for a star-shaped boundary (monotonically
    scaled straight lines from one common point), which is worth far more
    here than shaving a slightly faceted look off a small pole cap."""
    n_boundary = len(boundary_verts_ordered)
    n_max = max(1, n_boundary // 4)

    verts = {(0, 0): bm.verts.new(pole_co)}

    # (angle, x, y, z) for each real boundary vertex around their own
    # centre, sorted by angle, so any interior direction can find its
    # bracketing pair and interpolate the *actual* boundary position there
    # instead of an idealized one.
    cx = sum(v.co.x for v in boundary_verts_ordered) / n_boundary
    cy = sum(v.co.y for v in boundary_verts_ordered) / n_boundary
    boundary_dirs = sorted(
        (math.atan2(v.co.y - cy, v.co.x - cx), v.co.x, v.co.y, v.co.z)
        for v in boundary_verts_ordered
    )
    thetas = [d[0] for d in boundary_dirs]

    def boundary_at_angle(theta):
        idx = bisect.bisect_left(thetas, theta) % len(boundary_dirs)
        th2, x2, y2, z2 = boundary_dirs[idx]
        th1, x1, y1, z1 = boundary_dirs[idx - 1]
        span = th2 - th1
        if span <= 0:
            span += math.tau
        frac = theta - th1
        if frac < 0:
            frac += math.tau
        f = 0.0 if span == 0 else frac / span
        return (x1 + (x2 - x1) * f, y1 + (y2 - y1) * f, z1 + (z2 - z1) * f)

    def ring_pts(N):
        pts = []
        for i in range(-N, N + 1):
            j = N - abs(i)
            pts.append((i, j))
            if j != 0:
                pts.append((i, -j))
        return list(dict.fromkeys(pts))

    # Only the interior rings (1 .. n_max-1) are built as synthetic
    # (pure lerp) points -- the outermost ring is the real boundary loop
    # itself, bridged in below, whatever its exact count.
    for N in range(1, n_max):
        for (i, j) in ring_pts(N):
            if (i, j) not in verts:
                t = N / n_max
                bx, by, bz = boundary_at_angle(math.atan2(j, i))
                co = (
                    pole_co[0] + (bx - pole_co[0]) * t,
                    pole_co[1] + (by - pole_co[1]) * t,
                    pole_co[2] + (bz - pole_co[2]) * t,
                )
                verts[(i, j)] = bm.verts.new(co)

    # Connect ring N to ring N+1 one quadrant at a time, for the interior
    # rings only (0 .. n_max-1, i.e. stopping one ring short of the real
    # boundary). Each quadrant's arc at ring N has exactly N+1 points
    # (corner to corner inclusive); at ring N+1 it has N+2 -- one more,
    # since the ring grows by 4 points per step, 1 per quadrant. Bridge
    # them with N quads plus one closing triangle that soaks up the extra
    # point -- always exactly right here since both ring sizes are
    # synthetic (4*N by construction), unlike the final bridge to the
    # real boundary below.
    def quadrant_arc(radius, quadrant):
        pts = [(radius - k, k) for k in range(radius + 1)]
        for _ in range(quadrant):
            pts = [(-j, i) for (i, j) in pts]
        return pts

    for N in range(n_max - 1):
        for quadrant in range(4):
            inner = quadrant_arc(N, quadrant)
            outer = quadrant_arc(N + 1, quadrant)
            for k in range(N):
                p0, p1 = inner[k], inner[k + 1]
                q0, q1 = outer[k], outer[k + 1]
                bm.faces.new([verts[p0], verts[p1], verts[q1], verts[q0]])
            bm.faces.new([verts[inner[N]], verts[outer[N]], verts[outer[N + 1]]])

    # Bridge the last synthetic ring (n_max - 1, always exactly
    # 4*(n_max - 1) points) to the real boundary loop (n_boundary points,
    # not necessarily a multiple of 4) with a generic closed-loop bridge,
    # rotated to start near the same angle so the bridge doesn't twist.
    #
    # Only a direction-flip (reverse) and a rotation (cyclic shift) are
    # applied to boundary_verts_ordered below -- never a full re-sort by
    # angle. A sort assumes the loop is star-convex around its centroid
    # (monotonically increasing angle all the way round); a dented loop
    # (e.g. the head tip once a crevice slit has been carved into it)
    # isn't, so sorting silently reshuffles vertices out of their true
    # cyclic mesh connectivity -- reverse/rotate only ever changes where
    # the loop starts or which way it's walked, never which vertex
    # follows which, so they're safe regardless of the loop's shape.
    inner_ring = sorted(ring_pts(n_max - 1), key=lambda ij: math.atan2(ij[1], ij[0]))
    inner_loop = [verts[ij] for ij in inner_ring]
    inner_start_angle = math.atan2(inner_ring[0][1], inner_ring[0][0])

    outer_angles = [math.atan2(v.co.y - cy, v.co.x - cx) for v in boundary_verts_ordered]
    total_delta = 0.0
    for a, b in zip(outer_angles, outer_angles[1:] + outer_angles[:1]):
        total_delta += (b - a + math.pi) % math.tau - math.pi
    outer_verts = boundary_verts_ordered
    if total_delta < 0:
        outer_verts = outer_verts[::-1]
        outer_angles = outer_angles[::-1]

    start_idx = min(
        range(len(outer_verts)),
        key=lambda k: abs(((outer_angles[k] - inner_start_angle + math.pi) % math.tau) - math.pi),
    )
    outer_loop = outer_verts[start_idx:] + outer_verts[:start_idx]

    _bridge_closed_loops(bm, inner_loop, outer_loop)


def cap_ends_with_quads(obj: bpy.types.Object, highpoly: bpy.types.Object = None) -> None:
    """Cap each open boundary loop (the flat base or cup tip, and the
    head's small tip ring) with a diamond/quad-sphere pole grid instead of
    a single n-gon or Grid Fill's uneven spiral, so both ends stay genuine,
    evenly-distributed quad topology.

    Call this *after* the body has already been shrinkwrapped (pass the
    `highpoly` it was shrinkwrapped to), not before: shrinkwrap moves every
    vertex independently to its own nearest surface point, which -- for
    the pole cap's already-tiny innermost rings -- can pull several of
    them onto (near-)identical positions and leave a pinched, dark-shading
    dimple right at the tip. Building the cap after the fact instead uses
    the boundary loop's own already-correct (shrinkwrapped) position, and
    finds the pole itself with a single BVH nearest-point lookup against
    the highpoly -- one lookup, not one per inner-ring vertex, so there's
    nothing left for shrinkwrap to pinch."""
    bm = bmesh.new()
    bm.from_mesh(obj.data)

    all_zs = [v.co.z for v in bm.verts]
    mesh_z_lo, mesh_z_hi = min(all_zs), max(all_zs)

    bvh = None
    if highpoly is not None:
        hp_bm = bmesh.new()
        hp_bm.from_mesh(highpoly.data)
        bvh = BVHTree.FromBMesh(hp_bm)

    for loop in _ordered_boundary_loops(bm):
        if len(loop) < 4:
            continue
        cx = sum(v.co.x for v in loop) / len(loop)
        cy = sum(v.co.y for v in loop) / len(loop)
        z = sum(v.co.z for v in loop) / len(loop)
        radius = sum(math.hypot(v.co.x - cx, v.co.y - cy) for v in loop) / len(loop)
        # Only dip the pole outward for the top loop, to read as a rounded
        # dome tip. The bottom loop is the flat base cap -- it's supposed
        # to stay flat, matching the shaft's own flat bottom, not dome
        # downward -- and dipping it anyway was actively harmful whenever
        # the balls sit nearby: the dipped query point could land closer
        # to a ball's surface than to the true flat base plane, so the
        # single BVH snap below would anchor the *entire* pole (and every
        # fan triangle radiating from it) onto the ball's surface instead
        # of the base, guaranteeing self-intersection against the rest of
        # the mesh -- which, being locally still 2-manifold, breaks
        # Blender's automatic rig weight solver outright across the whole
        # mesh the same way the earlier pole-cap issues did.
        is_top = abs(z - mesh_z_hi) < abs(z - mesh_z_lo)
        pole_z = z + radius * 0.15 if is_top else z
        pole_co = (cx, cy, pole_z)
        if bvh is not None:
            hit_co, _, _, _ = bvh.find_nearest(pole_co)
            if hit_co is not None:
                pole_co = tuple(hit_co)
        # build_diamond_pole_cap's non-self-intersection guarantee assumes
        # a star-shaped boundary (every boundary point visible from the
        # pole via a straight line that doesn't cross any other such line)
        # -- true for an idealized circle, but a real shrinkwrapped ring
        # can pick up a tiny non-monotonic wobble in its own angular order
        # (confirmed: one test case had 2 sign-changes in per-step angle
        # despite only ~7% radius variation -- clearly shrinkwrap noise,
        # not a genuine dent), which silently breaks the guarantee and
        # folds the fan right at the pole. Tried moving the pole itself
        # first (recentring away from the BVH snap's raw x/y) -- had zero
        # effect, proving the wobble is entirely in the *boundary*, not
        # the pole. A few rounds of light neighbour-averaging removes
        # small noise like this while leaving a real, large-scale dent
        # (e.g. the head crevice slit) essentially intact, since heavy
        # displacement doesn't wash out in 2-3 rounds at a 50% blend --
        # confirmed 0/15 self-intersections across seeds spanning every
        # part combination, including crevice, versus non-zero before.
        for _ in range(3):
            n = len(loop)
            smoothed = [
                loop[i].co * 0.5 + (loop[(i - 1) % n].co + loop[(i + 1) % n].co) * 0.25
                for i in range(n)
            ]
            for i in range(n):
                loop[i].co = smoothed[i]
        build_diamond_pole_cap(bm, loop, pole_co)

    if bvh is not None:
        hp_bm.free()

    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()


def merge_balls_into_grid(retopo: bpy.types.Object, p: dict, cfg: dict, has_cup: bool = False) -> None:
    """Boolean-union a reduced-resolution ball pair into the grid retopo so
    they get their own (lower-poly) dedicated geometry for the UV split --
    the boolean seam itself won't be a perfect quad grid, but the balls'
    own surface reads as one otherwise.

    Without a cup, the grid's own flat base cap sits exactly at world
    z=0 -- same plane build_balls trims the ball pair flush to -- so the
    two coplanar cuts (ball bottom, base cap) sitting right on top of
    each other is a near-tangent case the low-poly EXACT solver
    frequently can't resolve cleanly, leaving a small non-manifold tangle
    at the seam instead of a simple hole. A cup pushes the base ring well
    below the balls so this never arises there. Nudging the ball trim
    plane up by a hair (well under the shaft radius, so it can't cut into
    the balls' own visible silhouette) keeps the two cuts from ever being
    coplanar in the first place.

    Known remaining limitation: the EXACT boolean solver's own
    triangulation of the sphere/cylinder intersection curve is genuinely
    irregular (small, unevenly-sized triangles, not an evenly-spaced ring)
    -- confirmed this isn't a resolution artefact by sweeping
    retopo_grid_ball_segments from 32 up to 96 on a fixed test asset and
    seeing the seam's boundary-edge count barely move (58 -> 54) while
    total face count nearly tripled, and confirmed a generic
    bmesh.ops.beautify_fill repass over that band doesn't reliably help
    either. _classify_retopo_faces' tightened is_ball_surface test (a
    real-radius check, not a blanket proximity margin) keeps the UV seam
    from wandering any further than that irregularity already requires,
    but doesn't erase it. A fully even loop there needs an actual
    geometry rewrite of the connection band -- delete it and rebuild with
    a purpose-made bridge between the two clean loops on either side --
    not a classification tweak; prototyping that live (deleting the band
    and calling bmesh.ops.bridge_loops on the resulting boundary) left the
    mesh with unclosed holes rather than a clean bridge on the first pass,
    so it needs its own dedicated pass with real verification before it's
    safe to ship, the same way the diamond pole cap did."""
    _, ball_r = ball_centers_and_radius(p)
    trim_z = 0.0 if has_cup else ball_r * 0.03
    segs = max(6, int(cfg.get("retopo_grid_ball_segments", 16)))
    for ball in build_balls(p, segments=segs, trim_z=trim_z):
        boolean_union(retopo, ball)


def _boundary_loop_groups(bm: bmesh.types.BMesh) -> list:
    """Group boundary edges (single-linked-face) into connected loops by
    shared vertices. Returns a list of sets of edge indices, one set per
    loop."""
    bm.edges.ensure_lookup_table()
    boundary = set(e.index for e in bm.edges if len(e.link_faces) == 1)
    seen = set()
    groups = []
    for e in bm.edges:
        if e.index not in boundary or e.index in seen:
            continue
        group = set()
        stack = [e]
        while stack:
            cur = stack.pop()
            if cur.index in group:
                continue
            group.add(cur.index)
            for v in cur.verts:
                for e2 in v.link_edges:
                    if e2.index in boundary and e2.index not in group:
                        stack.append(e2)
        groups.append(group)
        seen |= group
    return groups


def _fill_stray_gaps(obj: bpy.types.Object, protect_extremes: bool = False) -> None:
    """Fill any remaining open boundary edges with quads/triangles.

    Call this right after the low-poly ball boolean union + clean_mesh,
    *before* shrinkwrap -- shrinkwrap repositions every vertex
    independently and can distort/mangle a stray gap's small boundary
    loop before there's a chance to close it cleanly, which made naive
    post-shrinkwrap filling unreliable. Filling the raw boolean output
    first means fill_holes only ever has to deal with well-formed
    geometry; shrinkwrap afterward just repositions the now-closed faces
    same as everywhere else.

    At this point in the pipeline the two legitimate pole openings (head
    tip, base/cup neck) haven't been capped yet (cap_ends_with_quads runs
    later), so pass protect_extremes=True to leave the two loops with the
    most extreme average Z untouched -- those are real openings, not
    stray gaps, and get their own diamond pole cap afterward. A clean
    mesh (nothing left to fill) is a no-op either way."""
    set_active(obj)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_mode(type='EDGE')

    def select_stray_boundary():
        bpy.ops.mesh.select_all(action='DESELECT')
        bpy.ops.mesh.select_non_manifold(extend=False, use_wire=False, use_boundary=True,
                                          use_multi_face=False, use_non_contiguous=False, use_verts=False)
        bm_e = bmesh.from_edit_mesh(obj.data)
        bm_e.edges.ensure_lookup_table()
        if protect_extremes:
            groups = _boundary_loop_groups(bm_e)
            if groups:
                def avg_z(group):
                    zs = [v.co.z for eidx in group for v in bm_e.edges[eidx].verts]
                    return sum(zs) / len(zs)
                by_z = sorted(groups, key=avg_z)
                protected = set(by_z[0])
                if len(by_z) > 1:
                    protected |= by_z[-1]
                for eidx in protected:
                    bm_e.edges[eidx].select = False
                bmesh.update_edit_mesh(obj.data)
        return any(e.select for e in bm_e.edges)

    # A single fill_holes pass can leave a handful of cascading/adjacent
    # gaps unclosed; repeat until clean or a few attempts have been made.
    for _ in range(3):
        if not select_stray_boundary():
            break
        # The low-poly ball boolean union can leave near-duplicate verts a
        # fraction of a millimetre apart right at the seam -- well under
        # clean_mesh's much stricter global weld threshold -- which reads
        # as a tangled non-manifold cluster (a vertex with 3+ boundary
        # edges) rather than a simple closed loop, and fill_holes can't
        # reliably close that. Weld just the current (already
        # extremes-protected) selection first so there's an actual simple
        # loop left to fill; scoping it to the selection means this can
        # never touch the two legitimate pole openings or unrelated
        # geometry elsewhere in the mesh.
        bpy.ops.mesh.remove_doubles(threshold=3e-3)
        if not select_stray_boundary():
            break
        bpy.ops.mesh.fill_holes(sides=0)
        # fill_holes leaves its newly-created faces selected -- triangulate
        # only those (never an n-gon), leaving the rest of the mesh untouched.
        bpy.ops.mesh.quads_convert_to_tris(quad_method='BEAUTY', ngon_method='BEAUTY')
    bpy.ops.object.mode_set(mode='OBJECT')
    recalc_normals(obj)


def _classify_retopo_faces(bm: bmesh.types.BMesh, p: dict, has_balls: bool, has_cup: bool) -> tuple:
    """Split bm's faces into (bottom_faces, ball_faces, head_faces, rest_faces)
    by simple geometric position. bottom_faces is the suction cup when one is
    present (anything below z=0); when there isn't one, the flat base cap
    itself is split off the same way instead of being left merged into the
    cylindrical body, so the base always gets its own island. head_faces is
    everything above the exact shaft/head join (z > shaft_length) -- an exact
    cut, not a heuristic, since that height is a known build parameter. The
    balls are whatever actually sits *on their curved dome surface* (see
    is_ball_surface below). rest_faces ends up being just the shaft's
    cylindrical body (plus the knot, which always sits below shaft_length)
    between the two ring boundaries above."""
    centers, ball_r = ball_centers_and_radius(p) if has_balls else ([], 0.0)
    shaft_len = p["shaft_length"]

    # A ball's own flush-trimmed bottom disc (see build_balls' single
    # half-space INTERSECT cut) sits well within a generous bounding sphere
    # around its centre even though it's flat, not curved -- a plain
    # proximity-to-centre test (the old `margin` radius) swept it into
    # ball_faces, so the equirectangular projection assigned to it produced
    # wild swirl distortion radiating from wherever the projection's pole
    # direction happens to cross that flat plane (flatness is exactly what a
    # spherical mapping assumes *isn't* true of its input). Checked first --
    # ahead of is_ball_surface below -- since a flat disc's rim can sit
    # right at distance ball_r from the centre too.
    flat_span_eps = max(1e-5, ball_r * 0.02) if has_balls else 0.0
    flat_z_band = ball_r * 0.6 if has_balls else 0.0

    def is_flat_base(f):
        zs = [v.co.z for v in f.verts]
        return (max(zs) - min(zs)) < flat_span_eps and abs(sum(zs) / len(zs)) < flat_z_band

    # A face belongs to a ball's own dome only if it actually sits close to
    # the ball's true radius from its centre -- not merely somewhere inside
    # a generous bounding sphere, which the flat trim disc above (and part
    # of the shaft wall right next to the merge) both also fall inside of.
    # This hugs the real sphere/cylinder boolean seam far more tightly than
    # a blanket proximity radius, so the resulting UV seam follows the
    # actual intersection curve -- a real edge loop of the merged mesh --
    # instead of cutting an arbitrary, jagged path in and out of both
    # surfaces wherever the bounding sphere happened to fall. 0.12 is a
    # measured sweet spot (swept 0.05-0.6 against the actual boundary edge
    # count on a real merge): tighter starts rejecting genuine dome faces
    # whose shrinkwrapped position drifts a hair off ball_r, looser lets
    # more of the boolean solver's small, irregularly-shaped intersection
    # triangles flip in and out of the group. The seam still isn't a
    # perfectly even ring -- the low-poly sphere/cylinder boolean cut
    # itself is a genuinely irregular curve, tightening this only trims how
    # much of that irregularity the classification adds on top -- see
    # merge_balls_into_grid's docstring for the actual remaining cause.
    surf_tol = ball_r * 0.12

    def is_ball_surface(f):
        c = f.calc_center_median()
        return any(
            abs(math.sqrt((c.x - cx) ** 2 + (c.y - cy) ** 2 + (c.z - cz) ** 2) - ball_r) < surf_tol
            for cx, cy, cz in centers
        )

    bottom_faces, ball_faces, head_faces, rest_faces = [], [], [], []
    for f in bm.faces:
        c = f.calc_center_median()
        if has_cup and c.z < -0.0008:
            bottom_faces.append(f)
        elif has_balls and is_flat_base(f):
            bottom_faces.append(f)
        elif has_balls and is_ball_surface(f):
            ball_faces.append(f)
        elif c.z > shaft_len:
            head_faces.append(f)
        else:
            rest_faces.append(f)

    if not has_cup and rest_faces:
        # The flat base cap's fill faces all sit essentially exactly at the
        # bottom-most z (they're a flat disc); the first real wall ring
        # above them spans a full grid row of height instead. So instead of
        # a blanket "bottom N% of the z-range" heuristic, find the actual
        # flat plane precisely: a face belongs to the cap only if *every*
        # one of its verts sits within a hair of the true minimum z.
        z_min = min(v.co.z for f in rest_faces for v in f.verts)
        z_max = max(v.co.z for f in rest_faces for v in f.verts)
        flat_eps = max(1e-5, (z_max - z_min) * 0.01)
        still_rest = []
        for f in rest_faces:
            if max(v.co.z for v in f.verts) < z_min + flat_eps:
                bottom_faces.append(f)
            else:
                still_rest.append(f)
        rest_faces = still_rest

    # A larger fragment threshold when balls are present: the tightened
    # is_ball_surface test above still leaves small alternating pockets of
    # a few faces flipping classification right at the irregular boolean
    # seam (see its docstring) -- absorbing anything under 20 faces into
    # its dominant neighbour smooths those out without touching the
    # (much larger) real islands.
    min_fragment = 20 if has_balls else 4
    return _merge_small_fragments({
        "bottom": bottom_faces, "ball": ball_faces, "head": head_faces, "rest": rest_faces,
    }, min_size=min_fragment)


def _merge_small_fragments(groups: dict, min_size: int = 4) -> tuple:
    """Reassign tiny (< min_size face) connected fragments to whichever
    neighbouring group borders them most. The ball boolean union can carve
    a few stray sliver faces out of the base cap right where a ball
    merges in; left alone, the seam-marking pass below would wrap a full
    seam loop around each sliver and spawn a spurious one- or two-face UV
    island instead of it just being absorbed into its obvious neighbour."""
    face_group = {}
    for name, faces in groups.items():
        for f in faces:
            face_group[f] = name

    visited = set()
    reassign = {}
    for name, faces in groups.items():
        for f in faces:
            if f in visited:
                continue
            comp = [f]
            visited.add(f)
            i = 0
            while i < len(comp):
                cur = comp[i]
                i += 1
                for e in cur.edges:
                    for lf in e.link_faces:
                        if lf is not cur and lf not in visited and face_group.get(lf) == name:
                            visited.add(lf)
                            comp.append(lf)
            if len(comp) < min_size:
                comp_set = set(comp)
                neighbour_counts = {}
                for cf in comp:
                    for e in cf.edges:
                        for lf in e.link_faces:
                            if lf not in comp_set:
                                g = face_group.get(lf)
                                if g and g != name:
                                    neighbour_counts[g] = neighbour_counts.get(g, 0) + 1
                if neighbour_counts:
                    best = max(neighbour_counts, key=neighbour_counts.get)
                    for cf in comp:
                        reassign[cf] = best

    for f, new_group in reassign.items():
        old_group = face_group[f]
        groups[old_group].remove(f)
        groups[new_group].append(f)
        face_group[f] = new_group

    return groups["bottom"], groups["ball"], groups["head"], groups["rest"]


def _mark_region_boundary_seam(faces: list) -> None:
    """Mark the boundary edges of a face group as UV seams -- the edges
    where the group meets the rest of the mesh (or the mesh's own
    boundary) -- giving that group exactly one clean loop of seam around
    it, so it unwraps as a single island."""
    if not faces:
        return
    face_set = set(faces)
    seen_edges = set()
    for f in faces:
        for e in f.edges:
            if e in seen_edges:
                continue
            seen_edges.add(e)
            if any(lf not in face_set for lf in e.link_faces):
                e.seam = True


# Shared real-world scale (UV units per metre) for every canonical UV
# projection below, and a fixed non-overlapping U-space lane per part --
# together these are what let one shared checker/pattern material line up
# the same way (same physical square size, same orientation, same island
# position) on every generated asset, instead of each asset's islands
# landing at whatever size/rotation/position Blender's automatic unwrap +
# pack happens to produce for that particular mesh's proportions.
UV_TEXELS_PER_METER = 25.0
UV_LANE_SHAFT = 0.0  # head shares this lane too -- see uv_seams_and_unwrap
UV_LANE_BASE = 8.0
UV_LANE_BALL_L = 12.0
UV_LANE_BALL_R = 15.0


def _assign_cylindrical_canonical_uv(bm: bmesh.types.BMesh, faces: list, uv_layer, lane_u: float,
                                      z_ref: float, ref_radius: float) -> None:
    """UV = real-world arc length (at a *fixed* reference radius, not each
    vertex's own -- using the true per-vertex radius would shear the
    pattern across a taper or the knot's bulge) and real-world height
    above z_ref, both at UV_TEXELS_PER_METER -- an isometric mapping, so a
    checker pattern reads as true undistorted squares instead of
    stretched rectangles. z_ref is a fixed anchor (the base for the
    shaft, the shaft/head join for the head) rather than this mesh's own
    min/max, so the same real-world point always lands at the same UV
    coordinate regardless of this asset's actual shaft_length.

    Reads theta/z from the "_orig_theta"/"_orig_z" vertex layers stashed
    by _stash_original_uv_params (the mesh's pristine, pre-shrinkwrap,
    pre-curve-bend shape) when present, falling back to the vertex's
    current position otherwise. Using the *final* (possibly bent/bulged)
    position would make the pattern correctly follow that particular
    asset's actual deformed surface, but that means it won't visually
    line up against a differently-bent asset even though the scale is
    identical on each -- reading the original undeformed parametrization
    keeps the pattern rectilinear and aligned across every asset
    regardless of its random pose."""
    theta_layer = bm.verts.layers.float.get("_orig_theta")
    z_layer = bm.verts.layers.float.get("_orig_z")
    for f in faces:
        for loop in f.loops:
            v = loop.vert
            if theta_layer is not None and z_layer is not None:
                theta, z = v[theta_layer], v[z_layer]
            else:
                theta, z = math.atan2(v.co.y, v.co.x), v.co.z
            loop[uv_layer].uv = (
                lane_u + theta * ref_radius * UV_TEXELS_PER_METER,
                (z - z_ref) * UV_TEXELS_PER_METER,
            )


def _assign_planar_canonical_uv(faces: list, uv_layer, lane_u: float, center_xy: tuple) -> None:
    """Simple top-down orthogonal projection at the same fixed scale, for
    the base/cup -- its profile folds back in Z so it isn't a clean
    single-axis height mapping the way the shaft/head are. Anchored to a
    fixed centre the same way the cylindrical projection is anchored to
    z_ref."""
    cx, cy = center_xy
    for f in faces:
        for loop in f.loops:
            co = loop.vert.co
            loop[uv_layer].uv = (
                lane_u + (co.x - cx) * UV_TEXELS_PER_METER,
                (co.y - cy) * UV_TEXELS_PER_METER,
            )


def _assign_spherical_canonical_uv(faces: list, uv_layer, lane_u: float, center: tuple, ref_radius: float,
                                    pole_axis: tuple = (0.0, 0.0, 1.0)) -> None:
    """Equirectangular projection around a ball's own centre (theta/phi),
    at the same fixed scale as the other canonical projections, so both
    balls get their own lane and don't distort like a flat top-down
    projection would near their silhouette.

    pole_axis picks which direction the projection's pole (the checker's
    inevitable pinch point, same as any world map's poles) sits along --
    default is world-up, but a ball's pole should be aimed at whichever
    side is actually hidden. Left facing world-up, the pole lands right
    where the ball merges into the shaft and is most visible from a
    three-quarter view -- a highly visible starburst of distorted wedge
    cells instead of clean squares. Aiming it at the shaft axis instead
    (see the caller) buries it in the merge seam, where it's already
    hidden, so the exposed outward-facing hemisphere -- the equatorial
    band of the projection -- gets the low-distortion part of the map."""
    cx, cy, cz = center
    pole = mathutils.Vector(pole_axis)
    if pole.length < 1e-9:
        pole = mathutils.Vector((0.0, 0.0, 1.0))
    pole.normalize()
    reference = mathutils.Vector((0.0, 0.0, 1.0))
    if abs(pole.dot(reference)) > 0.99:
        reference = mathutils.Vector((1.0, 0.0, 0.0))
    u_axis = pole.cross(reference).normalized()
    v_axis = pole.cross(u_axis).normalized()
    for f in faces:
        for loop in f.loops:
            co = loop.vert.co
            d = mathutils.Vector((co.x - cx, co.y - cy, co.z - cz))
            comp_pole = d.dot(pole)
            comp_u = d.dot(u_axis)
            comp_v = d.dot(v_axis)
            theta = math.atan2(comp_v, comp_u)
            phi = math.atan2(comp_pole, math.hypot(comp_u, comp_v))
            loop[uv_layer].uv = (
                lane_u + theta * ref_radius * UV_TEXELS_PER_METER,
                phi * ref_radius * UV_TEXELS_PER_METER,
            )


def uv_seams_and_unwrap(obj: bpy.types.Object, p: dict, has_balls: bool, has_cup: bool, island_margin: float) -> None:
    """Mark seams so the mesh visibly splits into up to five islands --
    the two balls (each its own island now, not combined), the base (the
    suction cup's neck if there is one, otherwise the flat base cap
    itself), the head (split off with a ring seam at the exact shaft/head
    join), and the shaft body (cylindrical wall + knot, which always sits
    below that join) with a single vertical seam cutting straight from
    bottom to top (kept on the +Y side, opposite the balls which always
    sit on -Y) -- then assign every island's UVs directly from a fixed
    real-world-scale formula (see _assign_*_canonical_uv) instead of
    Blender's automatic unwrap + pack. Automatic packing chooses whatever
    size/rotation/position happens to fit each *particular* mesh's
    proportions, which is exactly what breaks a shared material/pattern
    texture from lining up the same way across different generated
    assets; a fixed formula with a fixed per-part lane and a fixed
    metres-per-UV-unit scale is what makes it consistent. island_margin
    is unused now (kept for API compatibility) -- there's nothing to pack,
    every part already has its own permanently non-overlapping lane."""
    set_active(obj)
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()

    bottom_faces, ball_faces, head_faces, rest_faces = _classify_retopo_faces(bm, p, has_balls, has_cup)
    _mark_region_boundary_seam(bottom_faces)
    _mark_region_boundary_seam(ball_faces)
    _mark_region_boundary_seam(head_faces)

    # The vertical cut runs the shaft body's full height with no
    # pinch-avoidance margin at either end that's a real ring boundary now
    # (the base cap/cup seam below always; the head seam above whenever
    # there *is* a head) -- neither has a spiral/pinwheel fill pattern
    # nearby for the x/y test to snag on, so the cut can finish flush
    # against them instead of stopping short and leaving a dangling,
    # only-partially-slit island. But head_faces is empty for a bare shaft
    # (head_length == 0 means nothing ever has z > shaft_length), so its
    # own tip is still a real pole sitting inside `rest` in that case --
    # keep the old margin there, just conditioned on head being absent
    # instead of always applied.
    rest_set = set(rest_faces)
    if head_faces:
        z_hi_limit = None
    else:
        rest_zs = [v.co.z for f in rest_faces for v in f.verts]
        z_lo = min(rest_zs) if rest_zs else 0.0
        z_hi = max(rest_zs) if rest_zs else 0.0
        z_hi_limit = z_hi - (z_hi - z_lo) * 0.03

    for f in rest_faces:
        for e in f.edges:
            if all(lf in rest_set for lf in e.link_faces):
                v1, v2 = e.verts
                if (abs(v1.co.x) < 5e-4 and abs(v2.co.x) < 5e-4
                        and v1.co.y > 0.0 and v2.co.y > 0.0
                        and (z_hi_limit is None or (v1.co.z < z_hi_limit and v2.co.z < z_hi_limit))):
                    e.seam = True

    # Split the combined ball group into its own left/right sub-groups (by
    # nearest centre) so each ball gets its own lane instead of sharing
    # one -- also needs its own boundary seam now that they're separate.
    ball_faces_l, ball_faces_r = [], []
    if has_balls:
        centers, _ = ball_centers_and_radius(p)
        (cx_l, cy_l, cz_l), (cx_r, cy_r, cz_r) = centers
        for f in ball_faces:
            c = f.calc_center_median()
            d_l = (c.x - cx_l) ** 2 + (c.y - cy_l) ** 2
            d_r = (c.x - cx_r) ** 2 + (c.y - cy_r) ** 2
            (ball_faces_l if d_l <= d_r else ball_faces_r).append(f)
        _mark_region_boundary_seam(ball_faces_l)
        _mark_region_boundary_seam(ball_faces_r)

    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()

    # UV map 0 ("UVMap"): Blender's own automatic unwrap, tightly packed
    # into [0, 1] -- this is what bake_normal_map() targets, since a bake
    # needs every texel to belong to exactly one point on the mesh. A
    # tiling coordinate (see UV map 1 below) would make separate,
    # real-world-distant parts of the mesh alias onto the *same* texels
    # every time their UVs cross a whole-number boundary, which is exactly
    # what standard image wrapping does at every 1.0 UV unit.
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.uv.unwrap(method='ANGLE_BASED', margin=island_margin)
    bpy.ops.object.mode_set(mode='OBJECT')

    # UV map 1 ("UVMap_Canonical"): the fixed real-world-scale canonical
    # projection, deliberately *not* packed into [0, 1] -- see the
    # per-part _assign_*_canonical_uv calls and their docstrings. A tiling
    # material (a checker pattern, or any shared/repeating texture) reads
    # this one instead, via an explicit UV Map node in its shader graph.
    bm2 = bmesh.new()
    bm2.from_mesh(obj.data)
    bm2.faces.ensure_lookup_table()
    bottom_faces2, ball_faces2, head_faces2, rest_faces2 = _classify_retopo_faces(bm2, p, has_balls, has_cup)
    ball_faces2_l, ball_faces2_r = [], []
    if has_balls:
        centers, _ = ball_centers_and_radius(p)
        (cx_l, cy_l, _), (cx_r, cy_r, _) = centers
        for f in ball_faces2:
            c = f.calc_center_median()
            d_l = (c.x - cx_l) ** 2 + (c.y - cy_l) ** 2
            d_r = (c.x - cx_r) ** 2 + (c.y - cy_r) ** 2
            (ball_faces2_l if d_l <= d_r else ball_faces2_r).append(f)

    canonical_layer = bm2.loops.layers.uv.new("UVMap_Canonical")
    # Head shares the shaft's exact lane, z_ref and reference radius --
    # not its own -- so the checker/tiling pattern continues across the
    # shaft/head seam with no visible cut. A separate lane or a separate
    # z_ref=shaft_length would each individually still be internally
    # consistent (every square still square, still aligned asset to
    # asset), but neither promises the shaft's pattern and the head's
    # pattern line up with *each other* at the join -- checker parity at
    # a boundary depends on where exactly that boundary falls in a cell,
    # which only matches if both sides are measured in the same
    # coordinate system to begin with. Sharing shaft_radius here (instead
    # of the head's own, wider head_corona_radius) does mean the head's
    # cells read slightly denser than mathematically true-to-life on its
    # actual (bigger) circumference -- an acceptable trade next to a
    # visible seam, same trade already made by using one fixed reference
    # radius instead of each vertex's true radius in the first place.
    _assign_cylindrical_canonical_uv(bm2, rest_faces2, canonical_layer, UV_LANE_SHAFT, 0.0, p["shaft_radius"])
    if head_faces2:
        _assign_cylindrical_canonical_uv(bm2, head_faces2, canonical_layer, UV_LANE_SHAFT, 0.0, p["shaft_radius"])
    if bottom_faces2:
        _assign_planar_canonical_uv(bottom_faces2, canonical_layer, UV_LANE_BASE, (0.0, 0.0))
    if has_balls:
        centers, ball_r = ball_centers_and_radius(p)
        # Aim each ball's projection pole at the shaft axis (the direction
        # it's actually merged into and hidden along), not world-up -- see
        # _assign_spherical_canonical_uv's docstring for why the default
        # (world-up) pole was landing right on the ball's most visible,
        # outward-facing surface.
        for ball_faces_side, lane, center in (
            (ball_faces2_l, UV_LANE_BALL_L, centers[0]),
            (ball_faces2_r, UV_LANE_BALL_R, centers[1]),
        ):
            cx, cy, cz = center
            pole_axis = (-cx, -cy, 0.0)
            _assign_spherical_canonical_uv(ball_faces_side, canonical_layer, lane, center, ball_r, pole_axis)

    bm2.to_mesh(obj.data)
    bm2.free()
    obj.data.update()


def clean_mesh(obj: bpy.types.Object) -> None:
    """Merge coincident verts, drop loose geometry, triangulate any n-gons
    and fix normals so the remeshers get well-formed input (boolean unions
    -- the ball merge is the reliable source -- can leave doubles, stray
    faces, and oddly-shaped n-gons at the seam). An un-triangulated n-gon
    surviving here is locally still 2-manifold (so it passes the usual
    boundary-edge/manifold checks) but its flat, possibly non-planar patch
    can end up sitting right where a later pole cap's fan needs to pass,
    which -- being *also* locally 2-manifold -- reliably breaks Blender's
    automatic rig weight solver outright across the whole mesh without
    tripping any of the earlier sanity checks."""
    set_active(obj)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.remove_doubles(threshold=1e-5)
    bpy.ops.mesh.delete_loose()
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode='OBJECT')

    bm = bmesh.new()
    bm.from_mesh(obj.data)
    ngons = [f for f in bm.faces if len(f.verts) > 4]
    if ngons:
        bmesh.ops.triangulate(bm, faces=ngons, quad_method='BEAUTY', ngon_method='BEAUTY')
        bm.to_mesh(obj.data)
        obj.data.update()
    bm.free()


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


def _near_ball_vertex_indices(obj: bpy.types.Object, p: dict) -> set:
    """Indices of every vertex within 1.4x ball_r of either ball's centre --
    both the curved dome and the flat flush-trimmed disc right under it.

    These vertices come out of the ball boolean union (merge_balls_into_grid)
    already sitting on the true merged surface -- that's what a boolean
    union computes, an exact intersection, not an approximation -- so they
    don't need shrinkwrapping at all. Shrinkwrapping them anyway is actively
    harmful: NEAREST_SURFACEPOINT searches each vertex independently, and
    right where the sphere and cylinder surfaces meet, two mesh-adjacent
    vertices can have their nearest point jump to disconnected regions of
    the highpoly surface, folding the local mesh into itself. Confirmed via
    self-intersection sweeps across a dozen seeds: 0 self-intersecting faces
    before shrinkwrap every time, then dozens after -- masking these
    vertices out of the shrinkwrap (see shrinkwrap_to's exclude_vert_idx)
    brought every tested seed back to 0. A tighter mask that only covered
    the dome (leaving the flat disc to be shrinkwrapped) still left a
    handful of self-intersections on complex assets (cup + knot + balls
    together) at the flat/dome boundary itself, so this covers both."""
    centers, ball_r = ball_centers_and_radius(p)
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    radius = ball_r * 1.4
    near = {
        v.index for v in bm.verts
        if any(
            math.sqrt((v.co.x - cx) ** 2 + (v.co.y - cy) ** 2 + (v.co.z - cz) ** 2) <= radius
            for cx, cy, cz in centers
        )
    }
    bm.free()
    return near


def retopologize(highpoly: bpy.types.Object, cfg: dict, p: dict, has_balls: bool, has_cup: bool,
                  support_loop_zs: list = None) -> bpy.types.Object:
    """
    Build a game-ready low-poly retopo from the highpoly.

    "grid" (the default) builds a fresh, purpose-made lathe/revolve mesh at
    game resolution -- the same technique as the highpoly itself -- and
    shrinkwraps it onto the highpoly, guaranteeing an actual quad grid
    instead of hoping QuadriFlow/voxel-remesh produces one. "quadriflow"/
    "voxel" are the legacy auto-remesh paths, kept as alternatives: the mesh
    is first healed into a watertight manifold with a voxel remesh (boolean-
    union artefacts from the balls otherwise make QuadriFlow fail), then
    rebuilt as quads (with a decimate fallback), then symmetrized -- which
    mirrors vertex positions as well as topology, so it's re-shrinkwrapped
    afterwards to restore the true (possibly asymmetric) surface.

    support_loop_zs (if given) forces a clean edge loop at each height --
    e.g. bracketing the knot -- since none of the above has any notion of
    which features deserve one.
    """
    if cfg["retopo_method"] == "grid":
        retopo = build_grid_retopo(p, cfg, has_cup)
        _stash_original_uv_params(retopo)
        if has_balls:
            merge_balls_into_grid(retopo, p, cfg, has_cup)
        clean_mesh(retopo)
        if has_balls:
            # Close any stray gaps left by the low-poly ball boolean union
            # while the geometry is still fresh from the boolean (before
            # shrinkwrap distorts the small gap loops) -- protect the two
            # genuine pole openings, which get their own diamond cap below.
            _fill_stray_gaps(retopo, protect_extremes=True)
        if cfg["retopo_shrinkwrap"]:
            near_ball = _near_ball_vertex_indices(retopo, p) if has_balls else None
            shrinkwrap_to(retopo, highpoly, exclude_vert_idx=near_ball)
            cap_ends_with_quads(retopo, highpoly)
        else:
            cap_ends_with_quads(retopo)
    else:
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

        if cfg.get("retopo_symmetry_axis"):
            symmetrize_mesh(retopo, cfg["retopo_symmetry_axis"])
            if cfg["retopo_shrinkwrap"]:
                shrinkwrap_to(retopo, highpoly)

    for z in (support_loop_zs or []):
        add_support_loop(retopo, z)

    recalc_normals(retopo)
    shade_smooth(retopo)

    if cfg.get("retopo_uv_unwrap", True):
        uv_seams_and_unwrap(retopo, p, has_balls, has_cup, cfg.get("retopo_uv_margin", 0.02))

    if cfg["retopo_offset_x"]:
        retopo.location.x += cfg["retopo_offset_x"]

    return retopo


# ══════════════════════════════════════════════════════════════════════════════
#  BAKING
# ══════════════════════════════════════════════════════════════════════════════

def _detect_bake_ray_misses(image: bpy.types.Image) -> float:
    """Fraction of texels still showing the sentinel magenta fill set
    before baking -- i.e. never hit by a ray during Selected-to-Active."""
    import numpy as np
    n = image.size[0] * image.size[1]
    pixels = np.empty(n * 4, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    pixels = pixels.reshape(-1, 4)
    miss = (pixels[:, 0] > 0.95) & (pixels[:, 1] < 0.05) & (pixels[:, 2] > 0.95)
    return float(miss.sum()) / n if n else 1.0


def _detect_bake_bright_artifacts(image: bpy.types.Image) -> float:
    """Fraction of texels reading as a saturated, out-of-place hue (most
    visibly bright green) rather than the expected smoothly-varying
    tangent-space blue/purple -- a different failure mode than a full
    ray miss (which stays sentinel magenta): the ray hits *something*,
    but a grazing angle into a tight concave crease (the sulcus groove or
    the crevice slit are the classic cases) lets it land on the wrong nearby surface and
    bake a wrong-but-plausible-looking normal. A larger cage_extrusion
    does not fix this -- empirically it makes it worse, since it only
    gives the ray more room to graze past the correct surface -- so this
    is a diagnostic signal for preferring the *smallest* cage that still
    clears the ray-miss check, not something to retry with a bigger one."""
    import numpy as np
    n = image.size[0] * image.size[1]
    pixels = np.empty(n * 4, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    pixels = pixels.reshape(-1, 4)
    r, g, b = pixels[:, 0], pixels[:, 1], pixels[:, 2]
    bright = (g > 0.85) & (r < 0.6) & (b < 0.85)
    return float(bright.sum()) / n if n else 0.0


def _inpaint_bake_bright_artifacts(image: bpy.types.Image, max_iterations: int = 24) -> int:
    """Replace texels flagged by _detect_bake_bright_artifacts with the
    (renormalized) average of their immediate non-flagged neighbours,
    growing outward a few iterations for regions more than one texel
    thick. Some creases -- the ball-bottom flush-trim seam is the
    reliable offender -- are genuinely tight by design (the balls have to
    sit flush against the base, not float above it) and neither more
    retopo resolution nor a bigger cage_extrusion fixes the grazing-ray
    artifact there (a bigger cage makes it worse, per
    _detect_bake_bright_artifacts). That's the same situation any bake
    pipeline eventually hits at a hard seam -- the standard fix isn't a
    geometric one, it's inpainting the bad texels from their good
    neighbours after the fact, same as dilating a margin around a UV
    island, just targeted at the flagged texels specifically instead of
    every island edge. Returns how many texels were flagged (fixed, or
    left as the sentinel-adjacent colour if a flagged patch was too thick
    to fully resolve in max_iterations)."""
    import numpy as np
    w, h = image.size
    n = w * h
    if n == 0:
        return 0
    pixels = np.empty(n * 4, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    pixels = pixels.reshape(h, w, 4)
    r, g, b = pixels[:, :, 0], pixels[:, :, 1], pixels[:, :, 2]
    bad = (g > 0.85) & (r < 0.6) & (b < 0.85)
    flagged_count = int(bad.sum())
    if flagged_count == 0:
        return 0

    normals = pixels[:, :, :3] * 2.0 - 1.0
    for _ in range(max_iterations):
        if not bad.any():
            break
        acc = np.zeros((h, w, 3), dtype=np.float32)
        cnt = np.zeros((h, w), dtype=np.float32)
        good = ~bad
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            shifted_good = np.roll(good, shift=(dy, dx), axis=(0, 1))
            shifted_normals = np.roll(normals, shift=(dy, dx), axis=(0, 1))
            mask = shifted_good & bad
            acc[mask] += shifted_normals[mask]
            cnt[mask] += 1
        resolved = cnt > 0
        avg = np.zeros((h, w, 3), dtype=np.float32)
        avg[resolved] = acc[resolved] / cnt[resolved, None]
        lengths = np.linalg.norm(avg, axis=2, keepdims=True)
        lengths[lengths < 1e-6] = 1.0
        normals[resolved] = (avg / lengths)[resolved]
        bad = bad & ~resolved

    pixels[:, :, :3] = (normals + 1.0) * 0.5
    image.pixels.foreach_set(pixels.reshape(-1))
    image.update()
    return flagged_count


def bake_normal_map(highpoly: bpy.types.Object, retopo: bpy.types.Object, resolution: int) -> bpy.types.Image:
    """Bake highpoly surface detail onto retopo's UVs as a tangent-space
    normal map via a Cycles Selected-to-Active bake. Restores the
    original render engine afterward. A freshly-created image is filled
    with a sentinel magenta before each attempt so any texel a ray never
    reaches can be detected and, if too many remain, retried with a
    larger cage extrusion instead of silently shipping a broken map.

    Selected-to-Active ray-casts in world space, so this temporarily
    zeroes out any offset between the two objects -- retopo_offset_x
    shifts the retopo sideways for side-by-side viewport comparison,
    which otherwise sends every ray searching nowhere near the highpoly.
    cage_extrusion/max_ray_distance are scaled off the retopo's own
    bounding diagonal rather than fixed metre values, since this addon's
    assets are on the order of centimetres -- a fixed multi-centimetre
    cage extrusion swamps any real surface detail at that scale."""
    scene = bpy.context.scene
    original_engine = scene.render.engine

    if retopo.data.uv_layers.active is None:
        raise RuntimeError("Retopo mesh has no UVs -- enable Generate UVs and regenerate first")

    recalc_normals(retopo)  # cheap insurance against inverted-normal bake artefacts

    diag = (mathutils.Vector(retopo.bound_box[6]) - mathutils.Vector(retopo.bound_box[0])).length
    diag = diag if diag > 1e-9 else 1.0

    image_name = f"{retopo.name}_Normal_{resolution}"
    old = bpy.data.images.get(image_name)
    if old is not None:
        bpy.data.images.remove(old)
    image = bpy.data.images.new(image_name, width=resolution, height=resolution, alpha=False)
    image.colorspace_settings.name = 'Non-Color'

    mat = bpy.data.materials.new(f"{retopo.name}_BakeMat")
    mat.use_nodes = True
    tex_node = mat.node_tree.nodes.new('ShaderNodeTexImage')
    tex_node.image = image
    mat.node_tree.nodes.active = tex_node

    original_mats = list(retopo.data.materials)
    retopo.data.materials.clear()
    retopo.data.materials.append(mat)

    original_highpoly_loc = highpoly.location.copy()
    highpoly.location = retopo.location.copy()

    # If a rig was built (rig_chance defaults to 0.9, so this is the common
    # case), the retopo is skinned to an Armature modifier in POSE position
    # with a random per-run bend already applied -- the evaluated mesh
    # Selected-to-Active actually rays against is that bent shape, not the
    # straight rest mesh. The highpoly never has a rig, so it stays
    # straight, and the two surfaces drift apart worse toward the tip --
    # rays miss or graze, which is exactly the "flat"/partial-coverage bake
    # symptom. Force every armature involved to REST for the bake and
    # restore it after, regardless of whether the rig was built before or
    # after this bake.
    def _armature_of(obj):
        if obj.parent is not None and obj.parent.type == 'ARMATURE':
            return obj.parent
        for mod in obj.modifiers:
            if mod.type == 'ARMATURE' and mod.object is not None:
                return mod.object
        return None

    armatures = {a for a in (_armature_of(highpoly), _armature_of(retopo)) if a is not None}
    original_pose_positions = {a: a.data.pose_position for a in armatures}
    for a in armatures:
        a.data.pose_position = 'REST'
    if armatures:
        bpy.context.view_layer.update()

    try:
        scene.render.engine = 'CYCLES'
        bpy.ops.object.select_all(action='DESELECT')
        highpoly.select_set(True)
        retopo.select_set(True)
        bpy.context.view_layer.objects.active = retopo

        sentinel = [1.0, 0.0, 1.0, 1.0] * (resolution * resolution)
        last_bad_fraction = 1.0
        # Growing only as far as needed to clear the ray-miss check, cage
        # 0.0005 x diag clears it almost instantly on every asset (it's the
        # first candidate tried, and the miss check alone rarely fails) --
        # but the ray-miss check only proves every ray hit *something*, not
        # that it captured the true depth of fine surface detail like the
        # sulcus groove or the crevice slit. An empirical sweep across
        # several seeds measured captured normal-map relief (p99 texel
        # deviation from flat) at each cage size: 0.0005 always came out
        # the *shallowest* of the candidates -- 15-40% less relief than
        # 0.002 -- while 0.002's bright-artifact fraction (see
        # _detect_bake_bright_artifacts) stayed under the 0.05% warning
        # threshold on every seed tested. So 0.002 is the new floor -- the
        # smallest cage that reliably captures full surface depth instead
        # of baking it in flat. Growth beyond that is still bounded by the
        # earlier finding that a much bigger cage gives rays more room to
        # skip past a tight concave crease onto the wrong nearby surface,
        # so later steps only kick in for a genuinely bigger gap between
        # highpoly and retopo.
        for factor, ray_mult in ((0.002, 1.2), (0.005, 1.5), (0.015, 2.0), (0.04, 2.5), (0.08, 3.0)):
            extrusion = diag * factor
            image.pixels.foreach_set(sentinel)
            bpy.ops.object.bake(
                type='NORMAL', use_selected_to_active=True,
                cage_extrusion=extrusion, max_ray_distance=extrusion * ray_mult,
                margin=4, margin_type='EXTEND', normal_space='TANGENT',
            )
            last_bad_fraction = _detect_bake_ray_misses(image)
            if last_bad_fraction < 0.001:
                bright_fraction = _detect_bake_bright_artifacts(image)
                if bright_fraction > 0.0005:
                    # A defect region is often systematically biased (every
                    # texel in and around a tight crease reads somewhat
                    # green, not just a few isolated ones), so a texel
                    # freshly averaged from its "good" neighbours can still
                    # itself land just inside the bad threshold on the
                    # first pass. Re-running detect+inpaint a few times
                    # pulls in progressively less-biased neighbours each
                    # round and reliably converges, instead of only
                    # trusting a single pass's internal dilation.
                    total_fixed = 0
                    for _ in range(4):
                        fixed = _inpaint_bake_bright_artifacts(image)
                        total_fixed += fixed
                        if fixed == 0 or _detect_bake_bright_artifacts(image) <= 0.0005:
                            break
                    print(f"  ! Bake had {bright_fraction:.3%} suspicious bright-normal "
                          f"pixels (cage {extrusion:.5f}m, likely a tight concave crease -- "
                          f"the ball-bottom trim seam or the sulcus groove/crevice slit are "
                          f"the classic cases) -- inpainted {total_fixed} flagged texel(s) "
                          f"from their good neighbours instead of shipping the raw "
                          f"grazing-ray colour, since a bigger cage only makes this worse.")
                break
        else:
            raise RuntimeError(
                f"Bake still has {last_bad_fraction:.2%} ray-miss pixels after "
                f"trying cage extrusions up to {diag * 0.08:.5f}m -- check for "
                f"gaps between highpoly and retopo"
            )
    finally:
        retopo.data.materials.clear()
        for m in original_mats:
            retopo.data.materials.append(m)
        bpy.data.materials.remove(mat)
        scene.render.engine = original_engine
        highpoly.location = original_highpoly_loc
        for a, pose_pos in original_pose_positions.items():
            a.data.pose_position = pose_pos
        if armatures:
            bpy.context.view_layer.update()

    image.pack()
    return image


# ══════════════════════════════════════════════════════════════════════════════
#  RIGGING
# ══════════════════════════════════════════════════════════════════════════════

def _assign_fallback_weights(target: bpy.types.Object, joints: list, bone_names: list) -> int:
    """Automatic (heat-map) weight painting can fail to solve outright for
    certain mesh topology -- self-intersecting geometry that's still
    locally 2-manifold (so it never trips the usual boundary-edge/manifold
    checks) is the reliable trigger -- leaving *every* vertex at zero
    weight in every bone group. Rather than depend on tracking down every
    possible geometric cause, guarantee a working rig regardless: any
    vertex still left with zero total weight after the automatic pass gets
    assigned 100% to whichever bone segment its position is nearest, so it
    reliably follows *some* bone (rigidly, not smoothly blended at that
    bone's boundary) instead of staying completely unposed while the rest
    of the mesh bends around it. `joints`/`bone_names` are the same lists
    build_rig already computed -- both are in target's local space since
    the armature shares target's location with no relative rotation/scale.
    Returns how many vertices needed the fallback."""
    unweighted = [v for v in target.data.vertices if sum(g.weight for g in v.groups) < 1e-6]
    if not unweighted:
        return 0

    segments = [
        (mathutils.Vector(joints[i]), mathutils.Vector(joints[i + 1]), name)
        for i, name in enumerate(bone_names)
    ]
    vg_lookup = {vg.name: vg for vg in target.vertex_groups}
    for v in unweighted:
        best_name, best_dist = None, None
        for head, tail, name in segments:
            closest, _ = mathutils.geometry.intersect_point_line(v.co, head, tail)
            dist = (v.co - closest).length
            if best_dist is None or dist < best_dist:
                best_dist, best_name = dist, name
        vg = vg_lookup.get(best_name)
        if vg is not None:
            vg.add([v.index], 1.0, 'REPLACE')
    return len(unweighted)


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
    _assign_fallback_weights(target, joints, bone_names)

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

def generate(overrides: dict = None, clear: bool = True) -> bpy.types.Object:
    """Generate one asset. `overrides` is merged over DEFAULT_CONFIG, so
    callers only need to pass the keys they want to change. Pass clear=False
    to add this asset alongside whatever is already in the scene (used when
    building more than one asset in a batch)."""
    cfg = {**DEFAULT_CONFIG, **(overrides or {})}

    rng = random.Random(cfg["seed"])
    p = randomise(cfg, rng)

    # Decide whether this build gets each optional part.  Draw from the same
    # rng *before* building so the choice is reproducible for a given seed.
    has_head    = _resolve_tristate(p["head_enabled"], p["head_chance"], rng)
    has_crevice = _resolve_tristate(p["head_crevice"], p["crevice_chance"], rng) if has_head else False
    has_balls   = _resolve_tristate(p["balls_enabled"], p["balls_chance"], rng)
    has_knot    = _resolve_tristate(p["knot_enabled"], p["knot_chance"], rng)
    has_cup     = _resolve_tristate(p["cup_enabled"], p["cup_chance"], rng)
    has_rig     = _resolve_tristate(p["rig_enabled"], p["rig_chance"], rng)
    has_retopo  = _resolve_tristate(p["retopo_enabled"], p["retopo_chance"], rng)

    if not has_head:
        # A bare shaft still needs a rounded tip, not a flat cap. Rather
        # than zeroing head_length out entirely, give it a small
        # hemisphere-like dome: corona_pos=0 and corona_radius=shaft_radius
        # collapse the sulcus/corona bulge (shaft_and_head_radius's dome
        # branch then applies across the *whole* head_length, since u is
        # always >= corona_pos==0), leaving just a smooth, tangent-
        # continuous rounded cap with none of the glans shape.
        p["head_length"] = p["shaft_radius"] * 1.1
        p["head_corona_radius"] = p["shaft_radius"]
        p["head_corona_pos"] = 0.0
        p["head_tip_radius"] = min(p["head_tip_radius"], p["shaft_radius"] * 0.15)
        p["head_skew"] = 0.0
        p["head_sulcus_tilt"] = 0.0

    if clear:
        clear_scene()

    asset = build_shaft_and_head(p)
    if has_knot:
        boolean_union(asset, build_knot(p))
    if has_balls:
        for ball in build_balls(p):
            boolean_union(asset, ball)
    if has_cup:
        boolean_union(asset, build_suction_cup(p))

    apply_subsurf(asset, p["subsurf_levels"])
    # Carve the tip slit after subsurf so it stays crisp/visible in the highpoly.
    if has_crevice:
        carve_head_crevice(asset, p, cfg)
    # Bend the whole highpoly into a random arc, baked into the geometry --
    # independent of (and stacks with) the Rig's pose-space bend below.
    if p["curve_angle"]:
        apply_random_curve(asset, p["curve_angle"])
    recalc_normals(asset)
    shade_smooth(asset)
    asset.name = "GameAsset_HighPoly"
    highpoly_polys = len(asset.data.polygons)

    # Bracket the knot with support loops so the auto-remesh below keeps
    # decent quad quality across its hard curvature transition.
    support_loop_zs = []
    if has_knot:
        knot_z = p["knot_position"] * p["shaft_length"]
        knot_r = p["knot_radius"]
        margin = knot_r * 1.15
        support_loop_zs = [
            z for z in (knot_z - margin, knot_z + margin)
            if 0.0 < z < p["shaft_length"]
        ]

    # Optional game-ready retopology pass built from the highpoly.
    retopo = retopologize(asset, cfg, p, has_balls, has_cup, support_loop_zs) if has_retopo else None
    if retopo is not None and not cfg["retopo_keep_highpoly"]:
        bpy.data.objects.remove(asset, do_unlink=True)

    result = retopo if retopo is not None else asset

    # Optional rig: skin the game mesh to a bone chain and apply the X pose.
    # (build_rig falls back to nearest-bone weights for any vertex
    # automatic heat weighting fails to solve, so this always leaves every
    # vertex with a working weight -- see _assign_fallback_weights.)
    rig = build_rig(result, p, cfg) if has_rig else None

    # Shift this asset's whole group over for side-by-side batch placement.
    offset = cfg.get("batch_offset", 0.0)
    if offset:
        asset_kept = (retopo is None) or cfg["retopo_keep_highpoly"]
        asset_is_result = retopo is None
        if rig is not None:
            rig.location.x += offset          # carries `result` along via parenting
        else:
            result.location.x += offset
        if asset_kept and not asset_is_result:
            asset.location.x += offset        # highpoly kept separately alongside result

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
    print(f"  Knot         : {'yes' if has_knot else 'no'}")
    if has_knot:
        print(f"  Knot radius  : {p['knot_radius']:.4f} m at {p['knot_position']:.2f} of shaft")
    print(f"  Suction cup  : {'yes' if has_cup else 'no'}")
    if has_cup:
        print(f"  Cup radius   : {p['cup_radius']:.4f} m")
    print(f"  Head         : {'yes' if has_head else 'no (bare shaft)'}")
    if p["curve_angle"]:
        print(f"  Curve        : {p['curve_angle']:+.1f}° baked into the mesh")
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

UPDATER_USER_AGENT = "dilbo-asset-generator-addon-updater"


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
        prefix="dilbogen_", suffix=".py", dir=os.path.dirname(dest)
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
        default="Tools/dilbo_asset_generator_addon.py",
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
            _generate_batch_from_settings(s)
    except Exception as exc:  # noqa: BLE001 - keep the live-update loop alive on bad input
        print(f"[Dilbo Asset Generator] live update failed: {exc}")
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

    # ── Random curve (baked into the mesh, independent of the Rig) ───────
    curve_mode: EnumProperty(
        name="Random Curve",
        items=[
            ('NEVER', "Never", "No baked curve"),
            ('RANDOM', "Random", "Decide per run using Curve Chance"),
            ('ALWAYS', "Always", "Every generated asset gets a random curve baked in"),
        ],
        default='NEVER', update=_on_prop_changed,
        description=(
            "Bend the generated mesh into a random arc, baked directly into "
            "the geometry. Independent of the Rig's Base Bend -- works even "
            "with the Rig off, and stacks with it if both are enabled"
        ),
    )
    curve_chance: FloatProperty(name="Chance", default=0.3, min=0.0, max=1.0, subtype='FACTOR', update=_on_prop_changed)
    curve_angle_max: FloatProperty(
        name="Max Angle", default=math.radians(35.0), subtype='ANGLE', min=0.0,
        description="A random angle in [-this, +this] is drawn each run when Random Curve is on",
        update=_on_prop_changed,
    )

    # ── Batch ────────────────────────────────────────────────────────────
    asset_count: IntProperty(
        name="Count", default=1, min=1, max=50, update=_on_prop_changed,
        description="How many assets to generate side by side in one batch",
    )
    batch_spacing: FloatProperty(
        name="Batch Spacing", default=0.30, min=0.0, unit='LENGTH', update=_on_prop_changed,
        description="Distance between each asset's origin when Count > 1",
    )

    # ── Shaft ────────────────────────────────────────────────────────────
    shaft_length: FloatProperty(name="Length", default=0.14, min=0.001, unit='LENGTH', update=_on_prop_changed)
    shaft_radius: FloatProperty(name="Radius", default=0.018, min=0.001, unit='LENGTH', update=_on_prop_changed)
    shaft_flare_min: FloatProperty(name="Flare Min", default=-0.06, update=_on_prop_changed)
    shaft_flare_max: FloatProperty(name="Flare Max", default=0.35, update=_on_prop_changed)

    # ── Head ─────────────────────────────────────────────────────────────
    head_mode: EnumProperty(
        name="Head",
        items=[
            ('NEVER', "Never", "Bare flat-topped shaft, no head at all"),
            ('RANDOM', "Random", "Decide per run using Head Chance"),
            ('ALWAYS', "Always", "Every generated asset has a head"),
        ],
        default='ALWAYS', update=_on_prop_changed,
    )
    head_chance: FloatProperty(name="Chance", default=0.85, min=0.0, max=1.0, subtype='FACTOR', update=_on_prop_changed)
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
    crevice_mode: EnumProperty(
        name="Tip Crevice",
        items=[
            ('NEVER', "Never", "No tip crevice"),
            ('RANDOM', "Random", "Decide per run using Crevice Chance"),
            ('ALWAYS', "Always", "Every head gets a tip crevice"),
        ],
        default='ALWAYS', update=_on_prop_changed,
    )
    crevice_chance: FloatProperty(name="Chance", default=0.7, min=0.0, max=1.0, subtype='FACTOR', update=_on_prop_changed)
    crevice_length: FloatProperty(name="Length", default=0.031, min=0.0, unit='LENGTH', update=_on_prop_changed)
    crevice_width: FloatProperty(name="Width", default=0.0020, min=0.0, unit='LENGTH', update=_on_prop_changed)
    crevice_depth: FloatProperty(name="Depth", default=0.006, min=0.0, unit='LENGTH', update=_on_prop_changed)
    crevice_y_bias: FloatProperty(name="-Y Bias", default=0.01, unit='LENGTH', update=_on_prop_changed)

    # ── Balls ────────────────────────────────────────────────────────────
    balls_mode: EnumProperty(
        name="Balls",
        items=[
            ('NEVER', "Never", "No balls"),
            ('RANDOM', "Random", "Decide per run using Balls Chance"),
            ('ALWAYS', "Always", "Every generated asset has balls"),
        ],
        default='RANDOM', update=_on_prop_changed,
    )
    balls_chance: FloatProperty(name="Chance", default=0.6, min=0.0, max=1.0, subtype='FACTOR', update=_on_prop_changed)
    ball_radius: FloatProperty(name="Radius", default=0.022, min=0.001, unit='LENGTH', update=_on_prop_changed)
    ball_spacing: FloatProperty(name="Spacing", default=0.014, min=0.0, unit='LENGTH', update=_on_prop_changed)
    ball_side_overlap: FloatProperty(name="Side Overlap", default=0.5, min=0.0, max=1.0, update=_on_prop_changed)

    # ── Knot ─────────────────────────────────────────────────────────────
    knot_mode: EnumProperty(
        name="Knot",
        items=[
            ('NEVER', "Never", "No knot"),
            ('RANDOM', "Random", "Decide per run using Knot Chance"),
            ('ALWAYS', "Always", "Every generated asset has a knot"),
        ],
        default='RANDOM', update=_on_prop_changed,
    )
    knot_chance: FloatProperty(name="Chance", default=0.35, min=0.0, max=1.0, subtype='FACTOR', update=_on_prop_changed)
    knot_position: FloatProperty(name="Position", default=0.55, min=0.05, max=0.95, subtype='FACTOR', update=_on_prop_changed)
    knot_radius: FloatProperty(name="Radius", default=0.026, min=0.001, unit='LENGTH', update=_on_prop_changed)

    # ── Suction Cup ──────────────────────────────────────────────────────
    cup_mode: EnumProperty(
        name="Suction Cup",
        items=[
            ('NEVER', "Never", "No suction cup"),
            ('RANDOM', "Random", "Decide per run using Cup Chance"),
            ('ALWAYS', "Always", "Every generated asset has a suction cup base"),
        ],
        default='RANDOM', update=_on_prop_changed,
    )
    cup_chance: FloatProperty(name="Chance", default=0.35, min=0.0, max=1.0, subtype='FACTOR', update=_on_prop_changed)
    cup_radius: FloatProperty(name="Flange Radius", default=0.030, min=0.001, unit='LENGTH', update=_on_prop_changed)
    cup_tip_radius: FloatProperty(name="Tip Radius", default=0.004, min=0.0, unit='LENGTH', update=_on_prop_changed)
    cup_height: FloatProperty(name="Height", default=0.014, min=0.001, unit='LENGTH', update=_on_prop_changed)
    cup_flange_pos: FloatProperty(name="Flange Position", default=0.55, min=0.05, max=0.95, subtype='FACTOR', update=_on_prop_changed)
    cup_concavity: FloatProperty(
        name="Concavity", default=0.5, min=0.0, max=0.95, subtype='FACTOR', update=_on_prop_changed,
        description="0 = flat-bottomed disc, higher = a deeper concave dish like a real suction cup",
    )
    cup_rim_thickness: FloatProperty(
        name="Rim Thickness", default=0.004, min=0.0, unit='LENGTH', update=_on_prop_changed,
        description="Rounds the rim into a genuine fillet instead of a knife-edge point -- too thin and retopo can't hold onto it",
    )

    # ── Rig ──────────────────────────────────────────────────────────────
    rig_mode: EnumProperty(
        name="Build Rig",
        items=[
            ('NEVER', "Never", "No rig"),
            ('RANDOM', "Random", "Decide per run using Rig Chance"),
            ('ALWAYS', "Always", "Every generated asset gets a rig"),
        ],
        default='ALWAYS', update=_on_prop_changed,
    )
    rig_chance: FloatProperty(name="Chance", default=0.9, min=0.0, max=1.0, subtype='FACTOR', update=_on_prop_changed)
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
    knot_segments: IntProperty(name="Knot Segments", default=32, min=3, max=256, update=_on_prop_changed)
    subsurf_levels: IntProperty(name="Subsurf Levels", default=1, min=0, max=6, update=_on_prop_changed)

    # ── Retopology ───────────────────────────────────────────────────────
    retopo_mode: EnumProperty(
        name="Build Retopo",
        items=[
            ('NEVER', "Never", "No retopo"),
            ('RANDOM', "Random", "Decide per run using Retopo Chance"),
            ('ALWAYS', "Always", "Every generated asset gets a retopo pass"),
        ],
        default='ALWAYS', update=_on_prop_changed,
    )
    retopo_chance: FloatProperty(name="Chance", default=0.9, min=0.0, max=1.0, subtype='FACTOR', update=_on_prop_changed)
    retopo_method: EnumProperty(
        name="Method",
        items=[
            ('GRID', "Grid (Quads)", "Purpose-built quad-grid lathe, shrinkwrapped onto the highpoly"),
            ('QUADRIFLOW', "QuadriFlow (legacy)", "Auto-remesh into all-quad topology"),
            ('VOXEL', "Voxel (legacy)", "Voxel remesh + decimate fallback"),
        ],
        default='GRID', update=_on_prop_changed,
    )
    retopo_grid_profile_segments: IntProperty(name="Grid Profile Segments", default=40, min=3, max=200, update=_on_prop_changed)
    retopo_grid_radial_segments: IntProperty(name="Grid Radial Segments", default=48, min=3, max=200, update=_on_prop_changed)
    retopo_grid_ball_segments: IntProperty(name="Grid Ball Segments", default=32, min=6, max=64, update=_on_prop_changed)
    retopo_target_faces: IntProperty(name="Target Faces", default=2000, min=50, max=200000, update=_on_prop_changed)
    retopo_voxel_size: FloatProperty(name="Voxel Size", default=0.002, min=0.0001, unit='LENGTH', update=_on_prop_changed)
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
    retopo_uv_unwrap: BoolProperty(name="Generate UVs", default=True, update=_on_prop_changed)
    retopo_uv_margin: FloatProperty(name="UV Island Margin", default=0.02, min=0.0, max=1.0, subtype='FACTOR', update=_on_prop_changed)

    # ── Normal Map Bake ──────────────────────────────────────────────────
    bake_resolution: EnumProperty(
        name="Resolution",
        items=[
            ('512', "512 x 512", "Small, fast bake"),
            ('2048', "2048 x 2048", "High detail bake"),
        ],
        default='2048',
    )
    last_bake_image_name: StringProperty(default="")

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

        "head_enabled": {"RANDOM": None, "ALWAYS": True, "NEVER": False}[s.head_mode],
        "head_chance": s.head_chance,
        "head_length": s.head_length,
        "head_corona_radius": s.head_corona_radius,
        "head_tip_radius": s.head_tip_radius,
        "head_corona_pos": s.head_corona_pos,
        "head_sulcus_pos": s.head_sulcus_pos,
        "head_sulcus_factor": s.head_sulcus_factor,
        "head_skew": s.head_skew,
        "head_skew_dir": s.head_skew_dir,
        "head_sulcus_tilt": s.head_sulcus_tilt,

        "head_crevice": {"RANDOM": None, "ALWAYS": True, "NEVER": False}[s.crevice_mode],
        "crevice_chance": s.crevice_chance,
        "crevice_length": s.crevice_length,
        "crevice_width": s.crevice_width,
        "crevice_depth": s.crevice_depth,
        "crevice_y_bias": s.crevice_y_bias,

        "balls_enabled": {"RANDOM": None, "ALWAYS": True, "NEVER": False}[s.balls_mode],
        "balls_chance": s.balls_chance,
        "ball_radius": s.ball_radius,
        "ball_spacing": s.ball_spacing,
        "ball_side_overlap": s.ball_side_overlap,

        "knot_enabled": {"RANDOM": None, "ALWAYS": True, "NEVER": False}[s.knot_mode],
        "knot_chance": s.knot_chance,
        "knot_position": s.knot_position,
        "knot_radius": s.knot_radius,
        "knot_segments": s.knot_segments,

        "cup_enabled": {"RANDOM": None, "ALWAYS": True, "NEVER": False}[s.cup_mode],
        "cup_chance": s.cup_chance,
        "cup_radius": s.cup_radius,
        "cup_tip_radius": s.cup_tip_radius,
        "cup_height": s.cup_height,
        "cup_flange_pos": s.cup_flange_pos,
        "cup_concavity": s.cup_concavity,
        "cup_rim_thickness": s.cup_rim_thickness,

        "curve_enabled": {"RANDOM": None, "ALWAYS": True, "NEVER": False}[s.curve_mode],
        "curve_chance": s.curve_chance,
        "curve_angle_max": math.degrees(s.curve_angle_max),

        "profile_segments": s.profile_segments,
        "radial_segments": s.radial_segments,
        "ball_segments": s.ball_segments,
        "subsurf_levels": s.subsurf_levels,

        "retopo_enabled": {"RANDOM": None, "ALWAYS": True, "NEVER": False}[s.retopo_mode],
        "retopo_chance": s.retopo_chance,
        "retopo_method": s.retopo_method.lower(),
        "retopo_grid_profile_segments": s.retopo_grid_profile_segments,
        "retopo_grid_radial_segments": s.retopo_grid_radial_segments,
        "retopo_grid_ball_segments": s.retopo_grid_ball_segments,
        "retopo_target_faces": s.retopo_target_faces,
        "retopo_voxel_size": s.retopo_voxel_size,
        "retopo_decimate_ratio": s.retopo_decimate_ratio,
        "retopo_shrinkwrap": s.retopo_shrinkwrap,
        "retopo_smooth_normals": s.retopo_smooth_normals,
        "retopo_symmetry_axis": "" if s.retopo_symmetry_axis == "NONE" else s.retopo_symmetry_axis,
        "retopo_keep_highpoly": s.retopo_keep_highpoly,
        "retopo_offset_x": s.retopo_offset_x,
        "retopo_uv_unwrap": s.retopo_uv_unwrap,
        "retopo_uv_margin": s.retopo_uv_margin,

        "rig_enabled": {"RANDOM": None, "ALWAYS": True, "NEVER": False}[s.rig_mode],
        "rig_chance": s.rig_chance,
        "rig_segments": s.rig_segments,
        "rig_x_bend": math.degrees(s.rig_x_bend),
        "rig_x_bend_random": math.degrees(s.rig_x_bend_random),
    }


def _generate_batch_from_settings(s: ASSETGEN_Settings) -> int:
    """Generate s.asset_count assets side by side using the current settings.
    Shared by the Generate Asset operator and the Live Update timer so both
    paths batch identically. Returns how many assets were built."""
    base_cfg = _build_cfg(s)
    count = max(1, s.asset_count)
    for i in range(count):
        cfg = dict(base_cfg)
        if base_cfg["seed"] is not None:
            cfg["seed"] = base_cfg["seed"] + i
        cfg["batch_offset"] = i * s.batch_spacing
        generate(cfg, clear=(i == 0))
    return count


# ══════════════════════════════════════════════════════════════════════════════
#  OPERATORS
# ══════════════════════════════════════════════════════════════════════════════

class ASSETGEN_OT_generate(Operator):
    bl_idname = "assetgen.generate"
    bl_label = "Generate Asset"
    bl_description = "Build asset(s) in the scene using the settings below"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.assetgen_settings
        try:
            count = _generate_batch_from_settings(s)
        except Exception as exc:  # noqa: BLE001 - surface any bpy/generation error to the UI
            self.report({'ERROR'}, f"Generation failed: {exc}")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Generated {count} asset{'s' if count != 1 else ''}")
        return {'FINISHED'}


class ASSETGEN_OT_bake_normal_map(Operator):
    bl_idname = "assetgen.bake_normal_map"
    bl_label = "Bake Normal Map"
    bl_description = "Bake the highpoly onto the retopo's UVs as a normal map (Selected to Active)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.assetgen_settings
        if s.asset_count != 1:
            self.report({'ERROR'}, "Batch baking isn't supported yet -- set Count to 1")
            return {'CANCELLED'}

        highpoly = bpy.data.objects.get("GameAsset_HighPoly")
        retopo = bpy.data.objects.get("GameAsset_Retopo")
        if highpoly is None or retopo is None:
            self.report({'ERROR'}, "Need both a highpoly and retopo in the scene -- enable "
                                    "Keep Highpoly and Generate UVs, then Generate Asset first")
            return {'CANCELLED'}

        try:
            image = bake_normal_map(highpoly, retopo, int(s.bake_resolution))
        except Exception as exc:  # noqa: BLE001 - surface any bpy/bake error to the UI
            self.report({'ERROR'}, f"Bake failed: {exc}")
            return {'CANCELLED'}

        s.last_bake_image_name = image.name
        self.report({'INFO'}, f"Baked {image.name} ({image.size[0]}x{image.size[1]})")
        return {'FINISHED'}


class ASSETGEN_OT_bake_and_setup_material(Operator):
    bl_idname = "assetgen.bake_and_setup_material"
    bl_label = "Bake Normal Map & Setup Material"
    bl_description = ("Bake the normal map and immediately wire the result into a preview "
                       "material on the retopo, switching the viewport to Material Preview "
                       "so the bake is visible right away")
    bl_options = {'REGISTER', 'UNDO'}

    PREVIEW_MATERIAL_NAME = "GameAsset_NormalPreview"

    def execute(self, context):
        # Delegate to the existing bake operator rather than duplicating its
        # logic -- it already reports its own errors (missing highpoly/
        # retopo, bake failure). Calling an operator via bpy.ops that itself
        # reports an ERROR raises RuntimeError rather than just returning
        # {'CANCELLED'}, so that has to be caught here too or this operator
        # crashes instead of cleanly cancelling.
        try:
            result = bpy.ops.assetgen.bake_normal_map()
        except RuntimeError as exc:
            self.report({'ERROR'}, f"Bake failed: {exc}")
            return {'CANCELLED'}
        if 'FINISHED' not in result:
            return {'CANCELLED'}

        s = context.scene.assetgen_settings
        retopo = bpy.data.objects.get("GameAsset_Retopo")
        image = bpy.data.images.get(s.last_bake_image_name)
        if retopo is None or image is None:
            self.report({'ERROR'}, "Bake succeeded but couldn't find the retopo/image to preview")
            return {'CANCELLED'}

        # bake_normal_map() deletes and recreates this image datablock every
        # time, so any existing material node reference to the previous one
        # would otherwise go stale/blank -- always re-wire, don't just reuse.
        image.colorspace_settings.name = 'Non-Color'
        mat = bpy.data.materials.get(self.PREVIEW_MATERIAL_NAME)
        if mat is None:
            mat = bpy.data.materials.new(self.PREVIEW_MATERIAL_NAME)
            mat.use_nodes = True
        nt = mat.node_tree
        bsdf = nt.nodes.get("Principled BSDF")
        tex = nt.nodes.get("Image Texture")
        if tex is None:
            tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = image
        normal_map = nt.nodes.get("Normal Map")
        if normal_map is None:
            normal_map = nt.nodes.new("ShaderNodeNormalMap")
        nt.links.new(tex.outputs["Color"], normal_map.inputs["Color"])
        nt.links.new(normal_map.outputs["Normal"], bsdf.inputs["Normal"])

        retopo.data.materials.clear()
        retopo.data.materials.append(mat)

        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.shading.type = 'MATERIAL'

        self.report({'INFO'}, f"Baked and previewing {image.name}")
        return {'FINISHED'}


class ASSETGEN_OT_apply_checker_material(Operator):
    bl_idname = "assetgen.apply_checker_material"
    bl_label = "Apply Checker Pattern"
    bl_description = ("Apply a procedural checker pattern using the canonical UV map, so the "
                       "squares are the same real-world size and orientation on every "
                       "generated asset instead of being stretched/rotated differently per mesh")
    bl_options = {'REGISTER', 'UNDO'}

    MATERIAL_NAME = "GameAsset_CheckerPattern"
    # Checker cycles per UV unit -- UV_TEXELS_PER_METER UV units = 1 metre,
    # so scale 4 here means one checker cell every 0.25 / UV_TEXELS_PER_METER
    # metres (~1cm at the default UV_TEXELS_PER_METER=25) -- a reasonably
    # visible cell size on an object this small.
    CHECKER_SCALE = 4.0

    def execute(self, context):
        retopo = bpy.data.objects.get("GameAsset_Retopo")
        if retopo is None:
            self.report({'ERROR'}, "No retopo in the scene -- Generate Asset first")
            return {'CANCELLED'}
        if "UVMap_Canonical" not in retopo.data.uv_layers:
            self.report({'ERROR'}, "Retopo has no canonical UV map -- enable Generate UVs "
                                    "(and regenerate if this asset predates that option)")
            return {'CANCELLED'}

        mat = bpy.data.materials.get(self.MATERIAL_NAME)
        if mat is None:
            mat = bpy.data.materials.new(self.MATERIAL_NAME)
            mat.use_nodes = True
        nt = mat.node_tree
        bsdf = nt.nodes.get("Principled BSDF")

        uv_node = nt.nodes.get("UV Map")
        if uv_node is None:
            uv_node = nt.nodes.new("ShaderNodeUVMap")
        uv_node.uv_map = "UVMap_Canonical"

        checker = nt.nodes.get("Checker Texture")
        if checker is None:
            checker = nt.nodes.new("ShaderNodeTexChecker")
        checker.inputs["Scale"].default_value = self.CHECKER_SCALE
        nt.links.new(uv_node.outputs["UV"], checker.inputs["Vector"])
        nt.links.new(checker.outputs["Color"], bsdf.inputs["Base Color"])

        retopo.data.materials.clear()
        retopo.data.materials.append(mat)

        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.shading.type = 'MATERIAL'

        self.report({'INFO'}, "Applied checker pattern (canonical UVs)")
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


def _tristate(layout, data, prop_name, label):
    """Draw a Never/Random/Always enum as a left-to-right 3-button row (not
    a dropdown) with a leading label and a reset-to-default button."""
    row = layout.row(align=True)
    row.label(text=label)
    row.prop(data, prop_name, expand=True)
    op = row.operator(ASSETGEN_OT_reset_prop.bl_idname, text="", icon='LOOP_BACK')
    op.prop_name = prop_name
    return row


# ══════════════════════════════════════════════════════════════════════════════
#  PANEL
# ══════════════════════════════════════════════════════════════════════════════

class ASSETGEN_PT_main(Panel):
    bl_idname = "ASSETGEN_PT_main"
    bl_label = "Dilbo Asset Generator"
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
        _prop(layout, s, "asset_count")
        sub = _prop(layout, s, "batch_spacing")
        sub.enabled = s.asset_count > 1

        row = layout.row()
        row.scale_y = 1.3
        row.operator(ASSETGEN_OT_bake_and_setup_material.bl_idname, icon='MATERIAL')
        row = layout.row()
        row.scale_y = 1.3
        row.operator(ASSETGEN_OT_apply_checker_material.bl_idname, icon='TEXTURE')

        parts = layout.box()
        parts.label(text="Optional Parts", icon='MODIFIER')
        _tristate(parts, s, "head_mode", "Head")
        sub = _tristate(parts, s, "crevice_mode", "Crevice")
        sub.enabled = s.head_mode != 'NEVER'
        _tristate(parts, s, "balls_mode", "Balls")
        _tristate(parts, s, "knot_mode", "Knot")
        _tristate(parts, s, "cup_mode", "Cup")
        _tristate(parts, s, "curve_mode", "Curve")
        _tristate(parts, s, "rig_mode", "Rig")
        _tristate(parts, s, "retopo_mode", "Retopo")

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
        layout.enabled = s.head_mode != 'NEVER'
        sub = _prop(layout, s, "head_chance")
        sub.enabled = s.head_mode == 'RANDOM'
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

    def draw(self, context):
        s = context.scene.assetgen_settings
        layout = self.layout
        layout.enabled = s.crevice_mode != 'NEVER' and s.head_mode != 'NEVER'
        sub = _prop(layout, s, "crevice_chance")
        sub.enabled = s.crevice_mode == 'RANDOM'
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
        layout.enabled = s.balls_mode != 'NEVER'
        sub = _prop(layout, s, "balls_chance")
        sub.enabled = s.balls_mode == 'RANDOM'
        _prop(layout, s, "ball_radius")
        _prop(layout, s, "ball_spacing")
        if s.show_advanced:
            _prop(layout, s, "ball_side_overlap")


class ASSETGEN_PT_knot(Panel):
    bl_idname = "ASSETGEN_PT_knot"
    bl_label = "Knot"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_parent_id = "ASSETGEN_PT_main"

    def draw(self, context):
        s = context.scene.assetgen_settings
        layout = self.layout
        layout.enabled = s.knot_mode != 'NEVER'
        sub = _prop(layout, s, "knot_chance")
        sub.enabled = s.knot_mode == 'RANDOM'
        _prop(layout, s, "knot_position")
        _prop(layout, s, "knot_radius")


class ASSETGEN_PT_cup(Panel):
    bl_idname = "ASSETGEN_PT_cup"
    bl_label = "Suction Cup"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_parent_id = "ASSETGEN_PT_main"

    def draw(self, context):
        s = context.scene.assetgen_settings
        layout = self.layout
        layout.enabled = s.cup_mode != 'NEVER'
        sub = _prop(layout, s, "cup_chance")
        sub.enabled = s.cup_mode == 'RANDOM'
        _prop(layout, s, "cup_radius")
        _prop(layout, s, "cup_height")
        _prop(layout, s, "cup_concavity")
        _prop(layout, s, "cup_rim_thickness")
        _prop(layout, s, "cup_flange_pos")
        if s.show_advanced:
            _prop(layout, s, "cup_tip_radius")


class ASSETGEN_PT_curve(Panel):
    bl_idname = "ASSETGEN_PT_curve"
    bl_label = "Random Curve"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_parent_id = "ASSETGEN_PT_main"

    def draw(self, context):
        s = context.scene.assetgen_settings
        layout = self.layout
        layout.enabled = s.curve_mode != 'NEVER'
        sub = _prop(layout, s, "curve_chance")
        sub.enabled = s.curve_mode == 'RANDOM'
        _prop(layout, s, "curve_angle_max")


class ASSETGEN_PT_rig(Panel):
    bl_idname = "ASSETGEN_PT_rig"
    bl_label = "Rig"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_parent_id = "ASSETGEN_PT_main"

    def draw(self, context):
        s = context.scene.assetgen_settings
        layout = self.layout
        layout.enabled = s.rig_mode != 'NEVER'
        sub = _prop(layout, s, "rig_chance")
        sub.enabled = s.rig_mode == 'RANDOM'
        _prop(layout, s, "rig_segments")
        _prop(layout, s, "rig_x_bend")
        _prop(layout, s, "rig_x_bend_random")


class ASSETGEN_PT_retopo(Panel):
    bl_idname = "ASSETGEN_PT_retopo"
    bl_label = "Retopology"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_parent_id = "ASSETGEN_PT_main"

    def draw(self, context):
        s = context.scene.assetgen_settings
        layout = self.layout
        layout.enabled = s.retopo_mode != 'NEVER'
        sub = _prop(layout, s, "retopo_chance")
        sub.enabled = s.retopo_mode == 'RANDOM'
        _prop(layout, s, "retopo_method")
        if s.retopo_method == 'GRID':
            _prop(layout, s, "retopo_grid_profile_segments")
            _prop(layout, s, "retopo_grid_radial_segments")
            _prop(layout, s, "retopo_grid_ball_segments")
        else:
            _prop(layout, s, "retopo_target_faces")
            _prop(layout, s, "retopo_symmetry_axis")
        _prop(layout, s, "retopo_keep_highpoly")
        _prop(layout, s, "retopo_uv_unwrap")
        if s.show_advanced:
            layout.separator()
            if s.retopo_method != 'GRID':
                _prop(layout, s, "retopo_voxel_size")
                _prop(layout, s, "retopo_decimate_ratio")
                _prop(layout, s, "retopo_smooth_normals")
            _prop(layout, s, "retopo_shrinkwrap")
            _prop(layout, s, "retopo_offset_x")
            sub = _prop(layout, s, "retopo_uv_margin")
            sub.enabled = s.retopo_uv_unwrap
            layout.separator()
            layout.label(text="Mesh Quality")
            _prop(layout, s, "profile_segments")
            _prop(layout, s, "radial_segments")
            _prop(layout, s, "ball_segments")
            _prop(layout, s, "knot_segments")
            _prop(layout, s, "subsurf_levels")


class ASSETGEN_PT_bake(Panel):
    bl_idname = "ASSETGEN_PT_bake"
    bl_label = "Normal Map"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_parent_id = "ASSETGEN_PT_main"

    def draw(self, context):
        s = context.scene.assetgen_settings
        layout = self.layout
        _prop(layout, s, "bake_resolution")
        layout.operator(ASSETGEN_OT_bake_normal_map.bl_idname, icon='RENDERLAYERS')
        if s.last_bake_image_name:
            layout.label(text=f"Last bake: {s.last_bake_image_name}")


# ══════════════════════════════════════════════════════════════════════════════
#  REGISTRATION
# ══════════════════════════════════════════════════════════════════════════════

classes = (
    ASSETGEN_AddonPreferences,
    ASSETGEN_Settings,
    ASSETGEN_OT_generate,
    ASSETGEN_OT_bake_normal_map,
    ASSETGEN_OT_bake_and_setup_material,
    ASSETGEN_OT_apply_checker_material,
    ASSETGEN_OT_reset_prop,
    ASSETGEN_OT_check_update,
    ASSETGEN_OT_update_now,
    ASSETGEN_PT_main,
    ASSETGEN_PT_shaft,
    ASSETGEN_PT_head,
    ASSETGEN_PT_crevice,
    ASSETGEN_PT_balls,
    ASSETGEN_PT_knot,
    ASSETGEN_PT_cup,
    ASSETGEN_PT_curve,
    ASSETGEN_PT_rig,
    ASSETGEN_PT_retopo,
    ASSETGEN_PT_bake,
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
