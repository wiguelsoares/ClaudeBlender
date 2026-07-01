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

    # ── Veins (random splines merged into the shaft) ────────────────────────
    # Each vein is a bevelled curve running up part of the shaft's surface,
    # wandering side to side, then boolean-unioned in like the balls/knot.
    "veins_enabled": False,      # True/False/None like balls_enabled -- None =
                                  #   random per run using veins_chance
    "veins_chance": 0.4,
    "vein_count_min": 3,
    "vein_count_max": 7,
    "vein_girth_min": 0.0007,    # tube radius (metres)
    "vein_girth_max": 0.0016,
    "vein_bend_min": 8.0,        # degrees of angular wander as the vein climbs
    "vein_bend_max": 35.0,
    "vein_length_min": 0.85,     # fraction of shaft_length each vein spans --
                                  #   high by default so veins run nearly the
                                  #   full base-to-head length, not a random
                                  #   segment stuck in the middle
    "vein_length_max": 1.0,
    "vein_segments": 12,         # spline control point count (curve smoothness)

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
    "retopo_grid_ball_segments": 16,     # sphere resolution for the merged-in balls
    "retopo_target_faces": 2000,    # quad target for quadriflow (game budget)
    "retopo_voxel_size": 0.002,     # voxel size (m) for the voxel-heal pass and
                                     #   the voxel method -- fine enough to keep
                                     #   thin details (cup, veins) from collapsing
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
    "retopo_uv_unwrap": True,       # mark seams by part (balls / cup / rest)
                                     #   and unwrap into up to 3 clean islands
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


def local_surface_radius(z: float, p: dict, has_knot: bool) -> float:
    """Radius of the asset's actual outer surface at height z, accounting for
    the knot's spherical bulge (if present) as well as the shaft/head profile.
    Both the shaft/head profile and the knot sphere are centred on the axis
    (r=0), so whichever reaches further out at this z wins -- lets veins hug
    the knot's surface instead of disappearing inside it when their path
    crosses its z-range."""
    r = shaft_and_head_radius(z, p)
    if has_knot:
        knot_z = p["knot_position"] * p["shaft_length"]
        knot_r = p["knot_radius"]
        dz = z - knot_z
        if abs(dz) < knot_r:
            r = max(r, math.sqrt(knot_r * knot_r - dz * dz))
    return r


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


def build_balls(p: dict, segments: int = None) -> list:
    """
    Two hemisphere bumps against the shaft side.

    Each ball is a UV sphere with its lower half deleted at z=0 so it sits
    flush with the flat base.  Centres are placed at z ≈ 0 (tiny epsilon
    above the base plane to avoid a coplanar boolean artefact) and offset
    in Y to overlap the cylinder wall for a clean boolean union.  Pass
    `segments` to override p["ball_segments"] (used to build a lower-res
    pair for the grid retopo).
    """
    centers, ball_r = ball_centers_and_radius(p)
    segs = segments if segments is not None else p["ball_segments"]

    objs = []
    for center, label in zip(centers, ("L", "R")):
        bpy.ops.mesh.primitive_uv_sphere_add(
            radius=ball_r,
            segments=segs,
            ring_count=max(8, segs // 2),
            location=center,
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
#  VEINS
# ══════════════════════════════════════════════════════════════════════════════

def _vein_wobble(rng: random.Random, segments: int) -> list:
    """Return `segments+1` smooth values in roughly [-1, 1], built by summing
    two random-phase sine waves so the vein bends gradually rather than
    jittering point to point."""
    freqs  = [rng.uniform(1.0, 2.5) for _ in range(2)]
    phases = [rng.uniform(0.0, math.tau) for _ in range(2)]
    weights = (1.0, 0.5)
    out = []
    for i in range(segments + 1):
        t = i / segments
        v = sum(w * math.sin(t * math.tau * f + ph) for w, f, ph in zip(weights, freqs, phases))
        out.append(v / sum(weights))
    return out


def build_vein(p: dict, rng: random.Random, has_knot: bool) -> bpy.types.Object:
    """A single bendy vein running up nearly the full length of the shaft's
    surface, built as a bevelled Bezier curve so it reads as a rounded ridge
    once boolean-unioned into the highpoly. Quantity/girth/bend are drawn
    fresh per vein from the configured ranges, independent of `variation`.

    Hugs the knot's bulge too (via local_surface_radius) when a knot is
    present, instead of tracing the plain shaft radius and disappearing
    inside it.

    Both ends dive inward toward the shaft's central axis and taper to a
    point over the last bit of their length, well inside the solid, instead
    of stopping at/near the surface -- a vein that merely touches the
    surface at its tips leaves a boolean seam right at the union boundary,
    which is exactly the kind of paper-thin, barely-manifold geometry that
    makes the retopo remesher choke. Ending buried inside guarantees a
    robust union regardless of exactly where the tip lands.
    """
    segments = max(2, int(p["vein_segments"]))
    theta0 = rng.uniform(0.0, math.tau)
    girth  = rng.uniform(p["vein_girth_min"], p["vein_girth_max"])
    bend   = math.radians(rng.uniform(p["vein_bend_min"], p["vein_bend_max"]))
    span   = rng.uniform(p["vein_length_min"], p["vein_length_max"]) * p["shaft_length"]
    start_z = rng.uniform(0.0, max(0.0, p["shaft_length"] - span))
    wobble = _vein_wobble(rng, segments)

    # Fraction of the vein's own length, at each end, over which it dives
    # from the surface down to the axis and tapers to a point -- kept small
    # so the vein reads at full girth across nearly its whole length instead
    # of looking like it only really shows in the middle.
    bury_frac = min(0.12, 1.5 / segments)

    curve_data = bpy.data.curves.new("VeinCurve", type='CURVE')
    curve_data.dimensions = '3D'
    curve_data.bevel_depth = girth
    curve_data.bevel_resolution = 3
    curve_data.fill_mode = 'FULL'
    curve_data.use_fill_caps = True   # cap the tube ends -- without this
                                      #   they're literal open holes

    spline = curve_data.splines.new('BEZIER')
    spline.bezier_points.add(segments)  # spline already has 1 point
    for i in range(segments + 1):
        t = i / segments
        z = start_z + t * span
        theta = theta0 + bend * wobble[i]

        # embed: 1.0 = riding just under the surface (normal vein depth),
        # 0.0 = right on the shaft's central axis.  Both ends ease down to
        # 0 over `bury_frac` of the vein's length.
        end_dist = min(t, 1.0 - t) / bury_frac if bury_frac > 0 else 1.0
        embed = smoothstep(min(1.0, end_dist))
        # Inset by a fixed slice of the vein's own girth, not a percentage
        # of the local surface radius -- a %-based inset sinks the vein far
        # too deep (in absolute terms) once the local surface is the much
        # bigger knot sphere rather than the thin shaft, swallowing it
        # instead of letting it poke through the knot's surface.
        surface_r = local_surface_radius(z, p, has_knot)
        r = max(0.0, surface_r - girth * 0.5) * embed

        pt = spline.bezier_points[i]
        pt.co = (r * math.cos(theta), r * math.sin(theta), z)
        pt.handle_left_type = 'AUTO'
        pt.handle_right_type = 'AUTO'
        # Taper the tube's own girth down toward each buried tip too, so it
        # narrows to a point rather than carrying full width to the axis.
        pt.radius = 0.15 + 0.85 * embed

    obj = bpy.data.objects.new("Vein", curve_data)
    bpy.context.collection.objects.link(obj)
    set_active(obj)
    bpy.ops.object.convert(target='MESH')
    obj = bpy.context.active_object
    obj.name = "Vein"

    # Weld the bevel profile's start/end seam left behind by the curve-to-
    # mesh conversion -- without this the tube is still non-manifold even
    # with the caps filled.
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()

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


def build_grid_retopo(p: dict, cfg: dict, has_cup: bool) -> bpy.types.Object:
    """Build a clean quad-grid revolve mesh -- shaft + head, and the cup's
    own profile if present, as one continuous lathe -- at game-appropriate
    resolution, the same way the highpoly itself is built. This gives a
    guaranteed grid of quads instead of hoping QuadriFlow/voxel-remesh
    happens to produce one. The mesh only provides the base grid structure;
    shrinkwrapping it onto the highpoly afterwards is what picks up the true
    (possibly asymmetric) surface, so this profile never needs to know
    about veins or the knot."""
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

    cap_ends_with_quads(obj)

    return obj


def _boundary_edge_loops(mesh_data: bpy.types.Mesh) -> list:
    """Group this mesh's open-boundary edges (1 linked face) into separate
    connected loops, returned as lists of edge indices."""
    bm = bmesh.new()
    bm.from_mesh(mesh_data)
    bm.edges.ensure_lookup_table()
    remaining = {e for e in bm.edges if len(e.link_faces) == 1}
    loops = []
    while remaining:
        start = next(iter(remaining))
        loop = []
        stack = [start]
        while stack:
            e = stack.pop()
            if e not in remaining:
                continue
            remaining.discard(e)
            loop.append(e.index)
            for v in e.verts:
                for e2 in v.link_edges:
                    if e2 in remaining:
                        stack.append(e2)
        loops.append(loop)
    bm.free()
    return loops


def cap_ends_with_quads(obj: bpy.types.Object) -> None:
    """Cap each open boundary loop (the flat base or cup tip, and the
    head's small tip ring) with Grid Fill instead of a single n-gon or a
    pole-like fan of triangles, so both ends stay genuine quad topology.

    A disc can't be tiled with pure quads without *some* convergence
    somewhere. Grid Fill needs to pick 4 "corners" to map a plain circular
    loop onto a rectangle; left to its own defaults on a perfectly even
    circle it picks them arbitrarily, giving a lopsided spiral that all but
    merges to one side. Passing span = (loop length / 4) places the 4
    corners exactly 90 degrees apart instead, so the result is 4-fold
    rotationally symmetric -- distributed evenly around the cap rather than
    bunched up. (A pre-inset ring was tried to shrink the converged centre
    further, but it reliably shrinks some vertices below shrinkwrap's
    ability to tell them apart on the highpoly surface, leaving degenerate
    faces; not worth trading away the guaranteed all-quad result for.)"""
    set_active(obj)
    # Recompute the remaining open boundary loops fresh before each pass --
    # capping one loop appends new geometry, which isn't guaranteed to
    # leave a previously-computed loop's stored edge indices pointing at
    # the same edges, so a second stale pass could select/fill garbage.
    while True:
        loops = _boundary_edge_loops(obj.data)
        if not loops:
            break
        loop_edge_indices = loops[0]

        bpy.ops.object.mode_set(mode='EDIT')
        bm = bmesh.from_edit_mesh(obj.data)
        bm.edges.ensure_lookup_table()
        bpy.ops.mesh.select_all(action='DESELECT')
        for idx in loop_edge_indices:
            bm.edges[idx].select = True
        bm.select_flush(True)
        bmesh.update_edit_mesh(obj.data)

        span = max(1, len(loop_edge_indices) // 4)
        bpy.ops.mesh.fill_grid(span=span, offset=0)
        bpy.ops.object.mode_set(mode='OBJECT')


def merge_balls_into_grid(retopo: bpy.types.Object, p: dict, cfg: dict) -> None:
    """Boolean-union a reduced-resolution ball pair into the grid retopo so
    they get their own (lower-poly) dedicated geometry for the UV split --
    the boolean seam itself won't be a perfect quad grid, but the balls'
    own surface reads as one otherwise."""
    segs = max(6, int(cfg.get("retopo_grid_ball_segments", 16)))
    for ball in build_balls(p, segments=segs):
        boolean_union(retopo, ball)


def _classify_retopo_faces(bm: bmesh.types.BMesh, p: dict, has_balls: bool, has_cup: bool) -> tuple:
    """Split bm's faces into (bottom_faces, ball_faces, rest_faces) by
    simple geometric position. bottom_faces is the suction cup when one is
    present (anything below z=0); when there isn't one, the flat base cap
    itself is split off the same way instead of being left merged into the
    cylindrical body, so the base always gets its own island. The balls
    are whatever sits near their known centres."""
    centers, ball_r = ball_centers_and_radius(p) if has_balls else ([], 0.0)
    margin = ball_r * 1.25

    bottom_faces, ball_faces, rest_faces = [], [], []
    for f in bm.faces:
        c = f.calc_center_median()
        if has_cup and c.z < -0.0008:
            bottom_faces.append(f)
        elif has_balls and any(
            (c.x - cx) ** 2 + (c.y - cy) ** 2 + (c.z - cz) ** 2 < margin ** 2
            for cx, cy, cz in centers
        ):
            ball_faces.append(f)
        else:
            rest_faces.append(f)

    if not has_cup and rest_faces:
        z_min = min(f.calc_center_median().z for f in rest_faces)
        z_max = max(f.calc_center_median().z for f in rest_faces)
        cap_margin = (z_max - z_min) * 0.05
        still_rest = []
        for f in rest_faces:
            if f.calc_center_median().z < z_min + cap_margin:
                bottom_faces.append(f)
            else:
                still_rest.append(f)
        rest_faces = still_rest

    return bottom_faces, ball_faces, rest_faces


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


def uv_seams_and_unwrap(obj: bpy.types.Object, p: dict, has_balls: bool, has_cup: bool, island_margin: float) -> None:
    """Mark seams so the UV layout splits into up to three islands instead
    of an arbitrary Smart-UV-Project layout: the balls (one island, one
    seam looping around both combined), the base (one island, one seam
    around where it meets the shaft -- the suction cup's neck if there is
    one, otherwise the flat base cap itself), and everything else -- the
    shaft/head cylindrical body and the knot -- as one island with a
    single seam cutting straight from bottom to top (kept on the +Y side,
    opposite the balls which always sit on -Y)."""
    set_active(obj)
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()

    bottom_faces, ball_faces, rest_faces = _classify_retopo_faces(bm, p, has_balls, has_cup)
    _mark_region_boundary_seam(bottom_faces)
    _mark_region_boundary_seam(ball_faces)

    # Keep the cut within the regular grid body, clear of the polar cap
    # regions at the very top/bottom -- their spiral/pinwheel fill pattern
    # can otherwise let the same x/y test stray onto a second nearby edge
    # and pinch off a stray single-face "island".
    rest_set = set(rest_faces)
    rest_zs = [v.co.z for f in rest_faces for v in f.verts]
    if rest_zs:
        z_lo, z_hi = min(rest_zs), max(rest_zs)
        z_margin = (z_hi - z_lo) * 0.03
    else:
        z_lo = z_hi = z_margin = 0.0

    for f in rest_faces:
        for e in f.edges:
            if all(lf in rest_set for lf in e.link_faces):
                v1, v2 = e.verts
                if (abs(v1.co.x) < 5e-4 and abs(v2.co.x) < 5e-4
                        and v1.co.y > 0.0 and v2.co.y > 0.0
                        and z_lo + z_margin < v1.co.z < z_hi - z_margin
                        and z_lo + z_margin < v2.co.z < z_hi - z_margin):
                    e.seam = True

    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()

    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.uv.unwrap(method='ANGLE_BASED', margin=island_margin)
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


def dissolve_degenerate_faces(obj: bpy.types.Object, dist: float = 1e-6) -> None:
    """Clean up zero/near-zero-area faces -- shrinkwrap can collapse
    closely-packed vertices (most likely in the grid cap's small converged
    centre) onto the same point on the highpoly surface, leaving degenerate
    faces behind."""
    set_active(obj)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.dissolve_degenerate(threshold=dist)
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
        if has_balls:
            merge_balls_into_grid(retopo, p, cfg)
        clean_mesh(retopo)
        if cfg["retopo_shrinkwrap"]:
            shrinkwrap_to(retopo, highpoly)
            dissolve_degenerate_faces(retopo)
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
            dissolve_degenerate_faces(retopo)

        if cfg.get("retopo_symmetry_axis"):
            symmetrize_mesh(retopo, cfg["retopo_symmetry_axis"])
            if cfg["retopo_shrinkwrap"]:
                shrinkwrap_to(retopo, highpoly)
                dissolve_degenerate_faces(retopo)

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
    has_veins   = _resolve_tristate(p["veins_enabled"], p["veins_chance"], rng)
    has_rig     = _resolve_tristate(p["rig_enabled"], p["rig_chance"], rng)
    has_retopo  = _resolve_tristate(p["retopo_enabled"], p["retopo_chance"], rng)

    if not has_head:
        # Cascades safely everywhere: with head_length == 0 the profile never
        # exceeds shaft_length, so the head-only branches (sulcus/corona/dome,
        # skew, tilt) are simply never reached -- a bare flat-topped shaft.
        p["head_length"] = 0.0

    vein_count = 0
    if has_veins:
        lo, hi = sorted((int(cfg["vein_count_min"]), int(cfg["vein_count_max"])))
        vein_count = rng.randint(lo, hi)

    if clear:
        clear_scene()

    asset = build_shaft_and_head(p)
    for _ in range(vein_count):
        boolean_union(asset, build_vein(p, rng, has_knot))
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
    print(f"  Veins        : {vein_count}")
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
            _generate_batch_from_settings(s)
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

    # ── Veins ────────────────────────────────────────────────────────────
    veins_mode: EnumProperty(
        name="Veins",
        items=[
            ('NEVER', "Never", "No veins"),
            ('RANDOM', "Random", "Decide per run using Veins Chance"),
            ('ALWAYS', "Always", "Every generated asset has veins"),
        ],
        default='NEVER', update=_on_prop_changed,
        description="Random bendy splines boolean-unioned onto the shaft as raised veins",
    )
    veins_chance: FloatProperty(name="Chance", default=0.4, min=0.0, max=1.0, subtype='FACTOR', update=_on_prop_changed)
    vein_count_min: IntProperty(name="Count Min", default=3, min=0, max=40, update=_on_prop_changed)
    vein_count_max: IntProperty(name="Count Max", default=7, min=0, max=40, update=_on_prop_changed)
    vein_girth_min: FloatProperty(name="Girth Min", default=0.0007, min=0.0001, unit='LENGTH', update=_on_prop_changed)
    vein_girth_max: FloatProperty(name="Girth Max", default=0.0016, min=0.0001, unit='LENGTH', update=_on_prop_changed)
    vein_bend_min: FloatProperty(name="Bend Min", default=math.radians(8.0), subtype='ANGLE', min=0.0, update=_on_prop_changed)
    vein_bend_max: FloatProperty(name="Bend Max", default=math.radians(35.0), subtype='ANGLE', min=0.0, update=_on_prop_changed)
    vein_length_min: FloatProperty(name="Length Min", default=0.85, min=0.05, max=1.0, subtype='FACTOR', update=_on_prop_changed)
    vein_length_max: FloatProperty(name="Length Max", default=1.0, min=0.05, max=1.0, subtype='FACTOR', update=_on_prop_changed)
    vein_segments: IntProperty(name="Vein Segments", default=12, min=2, max=64, update=_on_prop_changed)

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
    retopo_grid_ball_segments: IntProperty(name="Grid Ball Segments", default=16, min=6, max=64, update=_on_prop_changed)
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

        "veins_enabled": {"RANDOM": None, "ALWAYS": True, "NEVER": False}[s.veins_mode],
        "veins_chance": s.veins_chance,
        "vein_count_min": s.vein_count_min,
        "vein_count_max": s.vein_count_max,
        "vein_girth_min": s.vein_girth_min,
        "vein_girth_max": s.vein_girth_max,
        "vein_bend_min": math.degrees(s.vein_bend_min),
        "vein_bend_max": math.degrees(s.vein_bend_max),
        "vein_length_min": s.vein_length_min,
        "vein_length_max": s.vein_length_max,
        "vein_segments": s.vein_segments,

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
        _prop(layout, s, "asset_count")
        sub = _prop(layout, s, "batch_spacing")
        sub.enabled = s.asset_count > 1

        parts = layout.box()
        parts.label(text="Optional Parts", icon='MODIFIER')
        _tristate(parts, s, "head_mode", "Head")
        sub = _tristate(parts, s, "crevice_mode", "Crevice")
        sub.enabled = s.head_mode != 'NEVER'
        _tristate(parts, s, "balls_mode", "Balls")
        _tristate(parts, s, "knot_mode", "Knot")
        _tristate(parts, s, "cup_mode", "Cup")
        _tristate(parts, s, "veins_mode", "Veins")
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


class ASSETGEN_PT_veins(Panel):
    bl_idname = "ASSETGEN_PT_veins"
    bl_label = "Veins"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_parent_id = "ASSETGEN_PT_main"

    def draw(self, context):
        s = context.scene.assetgen_settings
        layout = self.layout
        layout.enabled = s.veins_mode != 'NEVER'
        sub = _prop(layout, s, "veins_chance")
        sub.enabled = s.veins_mode == 'RANDOM'
        row = layout.row(align=True)
        _prop(row, s, "vein_count_min")
        _prop(row, s, "vein_count_max")
        row = layout.row(align=True)
        _prop(row, s, "vein_girth_min")
        _prop(row, s, "vein_girth_max")
        row = layout.row(align=True)
        _prop(row, s, "vein_bend_min")
        _prop(row, s, "vein_bend_max")
        row = layout.row(align=True)
        _prop(row, s, "vein_length_min")
        _prop(row, s, "vein_length_max")
        if s.show_advanced:
            _prop(layout, s, "vein_segments")


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
    ASSETGEN_PT_knot,
    ASSETGEN_PT_cup,
    ASSETGEN_PT_veins,
    ASSETGEN_PT_curve,
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
