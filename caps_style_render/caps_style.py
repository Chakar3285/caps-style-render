# Render style: zone palette, lines, smoothing.
#
# The palette lives in two places on purpose:
#   - the template's Z_* materials: a fallback for models without textures;
#   - live P_* nodes in auto materials (caps_zones.build_auto_material):
#     what the viewport shows right now.
# apply_preset writes to both.

import bpy
import caps_zones as cz

# Presets are sRGB hex; nodes receive linear values.
#
# Ink is the main style. Saturation is almost zero everywhere except the accent, the
# one coloured spot on a figure. The dark cloth tone is near black so masses read as
# ink rather than grey, and light thresholds are high so the light tone stays on top
# surfaces instead of breaking masses into patches.
PRESETS = {
    "INK": {
        "label": "Ink",
        "zones": {
            "skin":   ("#24201e", "#7a6b62", "#c4b2a0", 0.24, 0.92),
            "cloth":  ("#111214", "#4a4a48", "#b6b2a8", 0.30, 0.90),
            "metal":  ("#0a0b0c", "#474b4e", "#c2c5c7", 0.30, 0.95),
            "accent": ("#06181a", "#17958a", "#d0a747", 0.22, 0.78),
        },
        "contour_px": 7.5, "crease_px": 3.0, "ink": 0.13,
        "line": "#0b0b0c", "smooth": 6,
    },
    "COLOUR": {
        "label": "Colour",
        "zones": {
            "skin":   ("#23261d", "#96786d", "#d9b394", 0.22, 0.92),
            "cloth":  ("#0d1116", "#4a5250", "#b9b2a3", 0.24, 0.88),
            "metal":  ("#07080a", "#495259", "#d2d8dc", 0.28, 0.95),
            "accent": ("#06181a", "#17958a", "#d0a747", 0.22, 0.78),
        },
        "contour_px": 6.0, "crease_px": 2.4, "ink": 0.11,
        "line": "#000000", "smooth": 0,
    },
}
DEFAULT_PRESET = "INK"

KEYS = ("dark", "mid", "light")
SMOOTH_MOD = "CAPS_Smooth"


def preset_palette(preset):
    """Preset -> palette in the form build_auto_material expects."""
    pal = {}
    for z, (d, m, l, b1, b2) in PRESETS[preset]["zones"].items():
        pal[z] = {"dark": cz.hex_to_rgb(d) + (1.0,), "mid": cz.hex_to_rgb(m) + (1.0,),
                  "light": cz.hex_to_rgb(l) + (1.0,), "b1": b1, "b2": b2}
    return pal


def write_zone(zone, key, value):
    """Write one palette value to the Z_* material and the live nodes."""
    m = bpy.data.materials.get(cz.PALETTE_MATS[zone])
    g = m.node_tree.nodes.get("Group") if m and m.use_nodes else None
    sock = {"dark": "Dark", "mid": "Mid", "light": "Light",
            "b1": "Break 1", "b2": "Break 2"}[key]
    if g is not None:
        if key in KEYS:
            g.inputs[sock].default_value = tuple(value)[:3] + (1.0,)
        else:
            g.inputs[sock].default_value = float(value)
    cz.set_live(cz.live_name(zone, key), value)


# ── lines ─────────────────────────────────────────────────────────────────
#
# Grease Pencil v3 Line Art thickness is a radius in world units. Pixel widths hold
# because the camera's ortho_scale is constant: radius = ortho / res_y * px / 2.

def _px_to_radius(px):
    cam = bpy.data.objects.get("CAPS_Cam")
    S = bpy.context.scene
    ortho = cam.data.ortho_scale if cam else 2.5011
    return ortho / S.render.resolution_y * px / 2.0


def set_line_px(modifier, px):
    gp = bpy.data.objects.get("CAPS_Lines")
    if gp is None:
        return
    m = gp.modifiers.get(modifier)
    if m is not None:
        m.radius = _px_to_radius(px)


def line_px(modifier):
    gp = bpy.data.objects.get("CAPS_Lines")
    m = gp.modifiers.get(modifier) if gp else None
    if m is None:
        return 0.0
    return m.radius / _px_to_radius(1.0)


def set_line_colour(rgb):
    for name in ("L_contour", "L_crease"):
        m = bpy.data.materials.get(name)
        if m is not None and m.grease_pencil is not None:
            m.grease_pencil.color = tuple(rgb)[:3] + (1.0,)


# ── smoothing ─────────────────────────────────────────────────────────────
#
# A generated model is lumpy, and the three tones break up on every bump, causing
# ripple in shadows. Smooth after the armature evens the surface for both light and
# Line Art while barely changing the silhouette.

def set_smooth(meshes, iterations):
    for o in meshes:
        m = o.modifiers.get(SMOOTH_MOD)
        if iterations <= 0:
            if m is not None:
                o.modifiers.remove(m)
            continue
        if m is None:
            m = o.modifiers.new(SMOOTH_MOD, 'SMOOTH')
        m.factor = 0.5
        m.iterations = int(iterations)


def apply_preset(preset, meshes=()):
    """Apply a preset to the template, live nodes and model."""
    p = PRESETS[preset]
    pal = preset_palette(preset)
    for z in cz.ZONES:
        for key in KEYS + ("b1", "b2"):
            write_zone(z, key, pal[z][key])
    set_line_px("LA_contour", p["contour_px"])
    set_line_px("LA_crease", p["crease_px"])
    set_line_colour(cz.hex_to_rgb(p["line"]))
    cz.set_live("INK", p["ink"])
    set_smooth(meshes, p["smooth"])
