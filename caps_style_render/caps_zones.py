# Splits a model into zones WITHOUT selecting faces by hand.
#
# A generated mesh is one solid lump of about half a million triangles, so its
# parts cannot be selected. But the generator exports baked textures that already
# contain the layout:
#
#   texture_metallic.png  ready-made metal mask
#   texture_diffuse.png   skin and accent are recognised by colour
#
# Zones are computed per texel. Material slots are not needed.

import bpy, math
from mathutils import Color

ZONES = ("skin", "cloth", "metal", "accent")
PALETTE_MATS = {z: "Z_" + z for z in ZONES}


# ── view and side ─────────────────────────────────────────────────────────
#
# The camera tilt (66 degrees, or 24 from the horizon) never changes: all sprites
# are rendered from one height, which should match the middle of the game camera's
# range. Only the yaw rotates.
#
# Lights are placed by azimuth FROM THE CAMERA (KEY -40, RIM +150, FILL +75), so the
# camera cannot rotate alone: a front view would become backlit. The whole rig
# moves together and keeps its relative layout.

RIG = ("CAPS_Cam", "KEY", "RIM", "FILL")
BASE_YAW = -35.0

VIEWS = {
    "tq": -35.0,            # three-quarter: enemies
    "three-quarter": -35.0,
    # Allies turn further right than three-quarter, looking less into the camera.
    # Enemies stay at -35 with a mirror, so the two sides are not a mirrored pair.
    "squad": -55.0,
    "front": 0.0,           # facing the player
    "profile": -90.0,       # exact side view
    "back": 180.0,          # from behind
}

# side -> (folder in Art, prefix in Assets, view, mirror)
#
# Allies stand on the left of the field and face right, enemies stand on the right
# and face left. Rendering both the same way would turn enemies' backs to the allies.
SIDES = {
    "enemy": ("Characters/enemies", "characters/enemies", "tq", True),
    "squad": ("Characters/squad",   "characters/squad",   "squad", False),
    "prop":  ("Props",              "props",              "front", False),
}

# A 1.9 m person equals 72 s&box units. This sets the scale for everything else:
# a 0.8 m chest comes out at 30 units, not at human height.
UNITS_PER_M = 72.0 / 1.9


def parse_view(token, side):
    """A view name or degrees. Empty means the default view for the side."""
    if not token:
        return VIEWS[SIDES[side][2]]
    t = str(token).strip().lower()
    if t in VIEWS:
        return VIEWS[t]
    try:
        return float(t)
    except ValueError:
        raise RuntimeError("unknown view '%s'. Available: %s, or a number of degrees"
                           % (token, ", ".join(sorted(set(VIEWS)))))


def set_view(yaw_deg, mirror=False):
    """Orbit the rig around Z, then mirror if needed. The tilt does not change.

    Mirroring reflects the rig across x=0 instead of flipping the PNG: the camera and
    all three lights change the sign of x and yaw. An asymmetric model truthfully
    shows its other side.

    The light is reflected too, so it falls on the figure the same way. A plain orbit
    cannot do this: at +35 degrees the key light would move to the figure's front.
    """
    d = math.radians(yaw_deg - BASE_YAW)
    if abs(d) > 1e-9:
        c, s = math.cos(d), math.sin(d)
        for name in RIG:
            o = bpy.data.objects.get(name)
            if o is None:
                continue
            x, y = o.location.x, o.location.y
            o.location.x = x * c - y * s
            o.location.y = x * s + y * c
            o.rotation_euler.z += d
    if mirror:
        for name in RIG:
            o = bpy.data.objects.get(name)
            if o is None:
                continue
            o.location.x = -o.location.x
            o.rotation_euler.z = -o.rotation_euler.z
    bpy.context.view_layer.update()


def hex_to_rgb(h):
    """#rrggbb -> linear RGB."""
    h = (h or "").lstrip("#").strip()
    if len(h) != 6:
        return None
    out = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255.0
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return tuple(out)


def hex_to_hsv(h):
    """#rrggbb -> (H, S, V), as seen by Separate Color in HSV mode."""
    rgb = hex_to_rgb(h)
    if rgb is None:
        return None
    c = Color(rgb)
    return (c.h, c.s, c.v)


def read_palette():
    """The palette lives in the template's Z_* materials."""
    pal = {}
    for z, name in PALETTE_MATS.items():
        m = bpy.data.materials.get(name)
        if not m or not m.use_nodes:
            raise RuntimeError("template has no material " + name)
        g = m.node_tree.nodes.get("Group")
        pal[z] = {
            "dark": tuple(g.inputs["Dark"].default_value),
            "mid": tuple(g.inputs["Mid"].default_value),
            "light": tuple(g.inputs["Light"].default_value),
            "b1": g.inputs["Break 1"].default_value,
            "b2": g.inputs["Break 2"].default_value,
        }
    return pal


def find_source_textures(meshes):
    """Find the diffuse and metallic images in imported materials."""
    diffuse = metallic = None
    for o in meshes:
        for m in o.data.materials:
            if not m or not m.use_nodes:
                continue
            for n in m.node_tree.nodes:
                if n.type != 'TEX_IMAGE' or not n.image:
                    continue
                targets = {l.to_socket.name for out in n.outputs for l in out.links}
                if "Base Color" in targets and diffuse is None:
                    diffuse = n.image
                elif "Metallic" in targets and metallic is None:
                    metallic = n.image
    return diffuse, metallic


# ── material build ────────────────────────────────────────────────────────
#
# Everything an artist adjusts (palette, skin and accent colours, tolerances, ink
# threshold) is a separate Value or RGB node with a fixed name. The panel changes a
# node's value and the figure updates in the viewport without rebuilding the tree.

def _value(nt, name, v, loc):
    k = nt.nodes.new('ShaderNodeValue'); k.name = k.label = name; k.location = loc
    k.outputs[0].default_value = float(v)
    return k.outputs[0]


def _rgb(nt, name, c, loc):
    k = nt.nodes.new('ShaderNodeRGB'); k.name = k.label = name; k.location = loc
    k.outputs[0].default_value = tuple(c)[:3] + (1.0,)
    return k.outputs[0]


def _math(nt, op, a, b, loc):
    m = nt.nodes.new('ShaderNodeMath'); m.operation = op; m.location = loc
    for sock, v in ((m.inputs[0], a), (m.inputs[1], b)):
        if isinstance(v, (int, float)):
            sock.default_value = v
        else:
            nt.links.new(v, sock)
    return m.outputs[0]


def _colour_mask(nt, hsv_out, ref, tol, y):
    """Mask for texels similar to a given colour. Hue wraps around.

    ref and tol are (h, s, v) sockets, not numbers: the panel drives them.
    """
    H, S, V = hsv_out
    d = _math(nt, 'ABSOLUTE', _math(nt, 'SUBTRACT', H, ref[0], (-1500, y)), 0.0, (-1340, y))
    dH = _math(nt, 'MINIMUM', d, _math(nt, 'SUBTRACT', 1.0, d, (-1340, y - 120)), (-1180, y))
    mH = _math(nt, 'LESS_THAN', dH, tol[0], (-1020, y))

    dS = _math(nt, 'ABSOLUTE', _math(nt, 'SUBTRACT', S, ref[1], (-1500, y - 240)), 0.0, (-1340, y - 240))
    mS = _math(nt, 'LESS_THAN', dS, tol[1], (-1180, y - 240))

    dV = _math(nt, 'ABSOLUTE', _math(nt, 'SUBTRACT', V, ref[2], (-1500, y - 380)), 0.0, (-1340, y - 380))
    mV = _math(nt, 'LESS_THAN', dV, tol[2], (-1180, y - 380))

    return _math(nt, 'MULTIPLY', _math(nt, 'MULTIPLY', mH, mS, (-860, y)), mV, (-700, y))


def _mix_float_link(nt, a, b, fac, loc):
    m = nt.nodes.new('ShaderNodeMix'); m.data_type = 'FLOAT'; m.clamp_factor = True
    m.location = loc
    fi = next(s for s in m.inputs if s.name == 'Factor' and s.type == 'VALUE')
    ai = next(s for s in m.inputs if s.name == 'A' and s.type == 'VALUE')
    bi = next(s for s in m.inputs if s.name == 'B' and s.type == 'VALUE')
    nt.links.new(a, ai); nt.links.new(b, bi); nt.links.new(fac, fi)
    return next(s for s in m.outputs if s.type == 'VALUE')


def live_name(zone, key):
    """Palette node name in the material: P_skin_dark, P_cloth_b1 and so on."""
    return "P_%s_%s" % (zone, key)


def build_auto_material(diffuse, metallic, pal, skin_ref, accent_ref, tol,
                        zonemap=False, ink=0.0, metal_t=None, emissive=None,
                        glow=None):
    """One material that sorts the model into zones by itself."""
    m = bpy.data.materials.new("CAPS_Auto")
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    n = nt.nodes
    L = nt.links.new

    tex_d = n.new('ShaderNodeTexImage'); tex_d.image = diffuse; tex_d.location = (-2100, 300)
    tex_d.interpolation = 'Closest'          # classification, not display
    hsv = n.new('ShaderNodeSeparateColor'); hsv.mode = 'HSV'; hsv.location = (-1800, 300)
    L(tex_d.outputs['Color'], hsv.inputs['Color'])
    hsv_out = (hsv.outputs[0], hsv.outputs[1], hsv.outputs[2])

    # Metal comes straight from the generator's map
    if metallic is not None:
        metallic.colorspace_settings.name = 'Non-Color'
        tex_m = n.new('ShaderNodeTexImage'); tex_m.image = metallic
        tex_m.interpolation = 'Closest'; tex_m.location = (-2100, -300)
        # The threshold is live: some generations have a weak map (0.36 at most)
        mt = _value(nt, "METAL_T", DEFAULT_METAL_T if metal_t is None else metal_t, (-2000, -480))
        m_metal = _math(nt, 'GREATER_THAN', tex_m.outputs['Color'], mt, (-1800, -300))
    else:
        m_metal = _value(nt, "NO_METAL", 0.0, (-1800, -300))

    tol_s = tuple(_value(nt, "TOL_" + c, v, (-1700, -700 - i * 80))
                  for i, (c, v) in enumerate(zip("hsv", tol)))
    skin_s = tuple(_value(nt, "REF_skin_" + c, v, (-1700, 900 - i * 80))
                   for i, (c, v) in enumerate(zip("hsv", skin_ref)))
    m_skin = _colour_mask(nt, hsv_out, skin_s, tol_s, 600)

    # The accent is always built and enabled by a multiplier, so the panel can
    # toggle it without rebuilding the material.
    acc_s = tuple(_value(nt, "REF_accent_" + c, v, (-1700, 100 - i * 80))
                  for i, (c, v) in enumerate(zip("hsv", accent_ref or (0.0, 0.0, 0.0))))
    acc_on = _value(nt, "ACCENT_ON", 1.0 if accent_ref else 0.0, (-860, -260))
    m_acc = _math(nt, 'MULTIPLY', _colour_mask(nt, hsv_out, acc_s, tol_s, -100), acc_on, (-560, -100))

    # priority: accent > metal > skin > cloth
    not_acc = _math(nt, 'SUBTRACT', 1.0, m_acc, (-540, -160))
    m_metal_e = _math(nt, 'MULTIPLY', m_metal, not_acc, (-380, -300))
    m_skin_e = _math(nt, 'MULTIPLY',
                     _math(nt, 'MULTIPLY', m_skin, not_acc, (-540, 600)),
                     _math(nt, 'SUBTRACT', 1.0, m_metal, (-540, 450)), (-380, 600))

    order = (("skin", m_skin_e), ("metal", m_metal_e), ("accent", m_acc))
    chans = {}
    for key, y in (("dark", 1500), ("mid", 1200), ("light", 900)):
        cur = _rgb(nt, live_name("cloth", key), pal["cloth"][key], (-400, y))
        for i, (z, mask) in enumerate(order):
            col = _rgb(nt, live_name(z, key), pal[z][key], (-200 + i * 200, y + 150))
            cur = _mix_rgba_link(nt, cur, col, mask, (-200 + i * 200, y))
        chans[key] = cur
    for key, y in (("b1", 300), ("b2", 100)):
        cur = _value(nt, live_name("cloth", key), pal["cloth"][key], (-400, y))
        for i, (z, mask) in enumerate(order):
            v = _value(nt, live_name(z, key), pal[z][key], (-200 + i * 200, y + 60))
            cur = _mix_float_link(nt, cur, v, mask, (-200 + i * 200, y))
        chans[key] = cur

    out = n.new('ShaderNodeOutputMaterial'); out.location = (1600, 0)

    if zonemap:
        # debug: flat zone colours show what went where
        c = _rgb(nt, "ZM_cloth", (0.20, 0.20, 0.22), (400, -400))
        for i, (z, mask) in enumerate(order):
            col = {"skin": (0.95, 0.45, 0.30), "metal": (0.20, 0.60, 1.00),
                   "accent": (1.00, 0.90, 0.10)}[z]
            c = _mix_rgba_link(nt, c, _rgb(nt, "ZM_" + z, col, (600 + i * 180, -250)),
                               mask, (600 + i * 180, -400))
        e = n.new('ShaderNodeEmission'); e.location = (1150, -400)
        L(c, e.inputs['Color'])
        L(e.outputs[0], out.inputs['Surface'])
        return m

    dif = n.new('ShaderNodeBsdfDiffuse'); dif.location = (400, -200)
    dif.inputs['Color'].default_value = (1, 1, 1, 1)
    dif.inputs['Roughness'].default_value = 1.0
    s2r = n.new('ShaderNodeShaderToRGB'); s2r.location = (580, -200)
    bw = n.new('ShaderNodeRGBToBW'); bw.location = (740, -200)
    L(dif.outputs[0], s2r.inputs[0]); L(s2r.outputs['Color'], bw.inputs[0])

    g1 = _math(nt, 'GREATER_THAN', bw.outputs[0], chans["b1"], (900, -100))
    g2 = _math(nt, 'GREATER_THAN', bw.outputs[0], chans["b2"], (900, -260))

    c1 = _mix_rgba_link(nt, chans["dark"], chans["mid"], g1, (1060, 100))
    c2 = _mix_rgba_link(nt, c1, chans["light"], g2, (1220, 100))

    # Ink: texels that are dark in the diffuse map (eyes, brows, mouth, seams, straps)
    # go to their zone's dark tone regardless of light. Without this a face stays one
    # flat patch, because Line Art finds no 80 degree edges on a rounded head.
    # The node always exists: INK = 0 means off, since V is never below zero.
    ink_s = _value(nt, "INK", max(ink, 0.0), (740, -420))
    lt = _math(nt, 'LESS_THAN', hsv_out[2], ink_s, (900, -420))
    c2 = _mix_rgba_link(nt, c2, chans["dark"], lt, (1300, -100))

    # Glow goes on top of everything, ignoring light and ink.
    # Its colour is the generator's map multiplied by the live GLOW_COL.
    # GLOW_ON = 0 turns it off without a rebuild.
    if emissive is not None:
        tex_e = n.new('ShaderNodeTexImage'); tex_e.image = emissive
        tex_e.interpolation = 'Closest'; tex_e.location = (900, -650)
        ebw = n.new('ShaderNodeRGBToBW'); ebw.location = (1080, -650)
        L(tex_e.outputs['Color'], ebw.inputs[0])
        g_on = _value(nt, "GLOW_ON", 1.0, (1080, -800))
        g_col = _rgb(nt, "GLOW_COL", tuple(glow or (1.0, 1.0, 1.0)), (1080, -900))
        m_glow = _math(nt, 'MULTIPLY', _math(nt, 'GREATER_THAN', ebw.outputs[0], 0.3, (1240, -650)),
                       g_on, (1380, -650))
        tint = n.new('ShaderNodeMix'); tint.data_type = 'RGBA'; tint.blend_type = 'MULTIPLY'
        tint.location = (1240, -900)
        next(s for s in tint.inputs if s.name == 'Factor' and s.type == 'VALUE').default_value = 1.0
        L(tex_e.outputs['Color'], next(s for s in tint.inputs if s.name == 'A' and s.type == 'RGBA'))
        L(g_col, next(s for s in tint.inputs if s.name == 'B' and s.type == 'RGBA'))
        c2 = _mix_rgba_link(nt, c2, next(s for s in tint.outputs if s.type == 'RGBA'),
                            m_glow, (1450, -300))

    emi = n.new('ShaderNodeEmission'); emi.location = (1450, 200)
    emi.inputs['Strength'].default_value = 1.0
    L(c2, emi.inputs['Color'])
    L(emi.outputs[0], out.inputs['Surface'])
    return m


def auto_materials():
    """All materials made by build_auto_material."""
    return [m for m in bpy.data.materials
            if m.name.startswith("CAPS_Auto") and m.use_nodes]


def set_live(name, value):
    """Set a live node in all auto materials. Returns how many were found."""
    hit = 0
    for m in auto_materials():
        k = m.node_tree.nodes.get(name)
        if k is None:
            continue
        if k.type == 'RGB':
            k.outputs[0].default_value = tuple(value)[:3] + (1.0,)
        else:
            k.outputs[0].default_value = float(value)
        hit += 1
    return hit


def _mix_rgba_link(nt, a, b, fac, loc):
    """Mix two RGBA sockets by a factor socket."""
    m = nt.nodes.new('ShaderNodeMix'); m.data_type = 'RGBA'; m.clamp_factor = True
    m.location = loc
    fi = next(s for s in m.inputs if s.name == 'Factor' and s.type == 'VALUE')
    ai = next(s for s in m.inputs if s.name == 'A' and s.type == 'RGBA')
    bi = next(s for s in m.inputs if s.name == 'B' and s.type == 'RGBA')
    nt.links.new(a, ai); nt.links.new(b, bi); nt.links.new(fac, fi)
    return next(s for s in m.outputs if s.type == 'RGBA')


# ── helpers for command-line rendering ────────────────────────────────────

def zone_by_name(slot_name):
    n = (slot_name or "").lower()
    for z in ZONES:
        if z in n:
            return "Z_" + z
    return "Z_cloth"


DEFAULT_SKIN = "#c9a184"
# Measured: saturation tolerance can be wide, value tolerance cannot. Khaki cloth
# matches skin in value and differs in saturation, so a wide V pulls trousers and
# boots into skin while a wide S changes nothing. A narrow S lost half of a face.
DEFAULT_TOL = (0.045, 0.45, 0.34)
DEFAULT_METAL_T = 0.5   # metallic map above this -> metal zone
DEFAULT_INK = 0.13  # above 0.15 the AO baked into the diffuse map starts turning to ink


def apply_zones(meshes, skin_hex=None, accent_hex=None, tol=None, zonemap=False,
                ink=None):
    """Returns a text report of how the zones were assigned."""
    diffuse, metallic = find_source_textures(meshes)
    lines = []

    if diffuse is None:
        # no textures: use material slot names
        lines.append("  no textures in FBX, zones from slot names:")
        for o in meshes:
            me = o.data
            if not me.materials:
                me.materials.append(bpy.data.materials["Z_cloth"])
                lines.append("    %-24s (no slots) -> Z_cloth" % o.name)
                continue
            for i, m in enumerate(me.materials):
                src = m.name if m else "<empty>"
                dst = zone_by_name(src)
                me.materials[i] = bpy.data.materials[dst]
                lines.append("    %-24s %-16s -> %s" % (o.name, src, dst))
        return "\n".join(lines)

    pal = read_palette()
    skin_ref = hex_to_hsv(skin_hex or DEFAULT_SKIN)
    accent_ref = hex_to_hsv(accent_hex) if accent_hex else None
    ink_v = DEFAULT_INK if ink is None else float(ink)
    mat = build_auto_material(diffuse, metallic, pal, skin_ref, accent_ref,
                              tol or DEFAULT_TOL, zonemap=zonemap, ink=ink_v)
    for o in meshes:
        o.data.materials.clear()
        o.data.materials.append(mat)

    lines.append("  zones from generator textures:")
    lines.append("    diffuse   %s (%dx%d)" % (diffuse.name, diffuse.size[0], diffuse.size[1]))
    if metallic is not None:
        lines.append("    metal     from %s, used as is" % metallic.name)
    else:
        lines.append("    metal     no map, zone is empty")
    lines.append("    skin      by colour %s" % (skin_hex or DEFAULT_SKIN))
    lines.append("    accent    %s" % (("by colour " + accent_hex) if accent_hex else "not set"))
    lines.append("    the rest -> cloth")
    if ink_v > 0:
        lines.append("    ink       texels darker than V=%.2f -> zone dark tone" % ink_v)
    return "\n".join(lines)
