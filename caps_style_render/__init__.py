# CAPS Style Render: model -> weapon -> animations -> style -> sprite frames.
#
# A panel in the 3D View sidebar (N key, CAPS tab). Zones, palette and camera
# angles live in caps_zones.py and caps_style.py; this file holds the interface
# and the order of operations.

bl_info = {
    "name": "CAPS Style Render",
    "author": "Chakar3285",
    "version": (1, 0, 0),
    "blender": (5, 2, 0),
    "location": "3D View > Sidebar > CAPS",
    "description": "Render 3D models into hand-drawn looking sprite frames",
    "category": "Render",
}

import bpy, os, sys, json, math, shutil, importlib, time
import numpy as np
from mathutils import Color, Vector
from bpy.props import (StringProperty, EnumProperty, FloatProperty, IntProperty,
                       BoolProperty, FloatVectorProperty, PointerProperty,
                       CollectionProperty)
from bpy_extras.io_utils import ImportHelper

ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(ADDON_DIR, "render_template.blend")
if ADDON_DIR not in sys.path:
    sys.path.insert(0, ADDON_DIR)

import caps_zones as cz
import caps_style as st

MODEL_COLL = "CAPS_Model"
RIG = cz.RIG

# Default frame count per animation; matched by a word in the animation name.
BUDGET = {"idle": 5, "walk": 8, "run": 8, "light": 6, "heavy": 10,
          "block": 3, "dodge": 5, "hit": 2, "death": 8}
LOOPING = ("idle", "walk", "run")


def budget_for(anim):
    for k, v in BUDGET.items():
        if k in anim:
            return v
    return 6


# ── scene state ───────────────────────────────────────────────────────────

def is_studio():
    return bpy.data.objects.get("CAPS_Cam") is not None


def model_objects():
    c = bpy.data.collections.get(MODEL_COLL)
    return list(c.all_objects) if c else []


def body_meshes():
    return [o for o in model_objects() if o.type == 'MESH' and not o.get("caps_weapon")]


def weapon_meshes():
    return [o for o in model_objects() if o.type == 'MESH' and o.get("caps_weapon")]


def armature():
    return next((o for o in model_objects() if o.type == 'ARMATURE'), None)


def world_height(meshes):
    """Height from the world bounding box. dimensions is wrong for Mixamo: a x100 mesh inside a x0.01 armature."""
    zs = [(o.matrix_world @ Vector(c)).z for o in meshes for c in o.bound_box]
    return (max(zs) - min(zs)) if zs else 0.0


def model_collection():
    c = bpy.data.collections.get(MODEL_COLL)
    if c is None:
        c = bpy.data.collections.new(MODEL_COLL)
        bpy.context.scene.collection.children.link(c)
    return c


def import_any(path):
    """Import by file extension. Returns the new objects."""
    before = set(bpy.data.objects)
    ext = os.path.splitext(path)[1].lower()
    if ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    else:
        raise RuntimeError("unsupported format " + ext)
    return [o for o in bpy.data.objects if o not in before]


def fix_centimetres(objs):
    """Mixamo and some generators export in centimetres: a person 190 units tall.
    The reverse also happens: a metre-scale OBJ passed through Mixamo and its
    x0.01 skeleton shrinks the model to two centimetres."""
    meshes = [o for o in objs if o.type == 'MESH']
    tall = world_height(meshes)
    k = 0.01 if tall > 20.0 else 100.0 if 0.0 < tall < 0.2 else None
    if k is None:
        return False
    for o in objs:
        if o.parent is None:
            o.scale *= k
    bpy.context.view_layer.update()
    return True


def move_to(coll, objs):
    for o in objs:
        for c in list(o.users_collection):
            c.objects.unlink(o)
        coll.objects.link(o)


def remove_objects(objs):
    for o in objs:
        data = o.data
        bpy.data.objects.remove(o, do_unlink=True)
        if data is not None and data.users == 0:
            if isinstance(data, bpy.types.Mesh):
                bpy.data.meshes.remove(data)
            elif isinstance(data, bpy.types.Armature):
                bpy.data.armatures.remove(data)


# ── view ──────────────────────────────────────────────────────────────────
#
# cz.set_view rotates the rig RELATIVE to where it is now. The panel changes the
# view any number of times, so the rig first returns to the template's original
# position, which is remembered on the objects themselves.

def rig_reset():
    for name in RIG:
        o = bpy.data.objects.get(name)
        if o is None:
            continue
        if "caps_base" not in o:
            o["caps_base"] = list(o.location) + [o.rotation_euler.z]
        b = o["caps_base"]
        o.location = b[:3]
        o.rotation_euler.z = b[3]


def side_key(s):
    return s.kind.lower()


def current_yaw(s):
    if s.view == 'CUSTOM':
        return s.yaw
    if s.view == 'AUTO':
        return cz.VIEWS[cz.SIDES[side_key(s)][2]]
    return cz.VIEWS[s.view.lower()]


def current_mirror(s):
    if s.mirror == 'AUTO':
        return cz.SIDES[side_key(s)][3]
    return s.mirror == 'ON'


def apply_view(s):
    if not is_studio():
        return
    rig_reset()
    cz.set_view(current_yaw(s), current_mirror(s))


# ── materials ─────────────────────────────────────────────────────────────

def lin_hsv(rgb):
    c = Color(tuple(rgb)[:3])
    return (c.h, c.s, c.v)


def palette_from_settings(s):
    pal = {}
    for z in cz.ZONES:
        pal[z] = {k: tuple(getattr(s, "%s_%s" % (z, k))) + (1.0,) for k in st.KEYS}
        pal[z]["b1"] = getattr(s, z + "_b1")
        pal[z]["b2"] = getattr(s, z + "_b2")
    return pal


def drop_auto_materials():
    for m in cz.auto_materials():
        if m.users == 0:
            bpy.data.materials.remove(m)


_tint_ref_cache = {}


def tint_ref(image):
    """The most saturated colour of the weapon texture (linear HSV): the part to repaint."""
    if image.name not in _tint_ref_cache:
        w, h = image.size
        px = np.empty(w * h * 4, np.float32)
        image.pixels.foreach_get(px)
        px = px.reshape(-1, 4)[::7, :3]
        # pixels returns raw sRGB values, while the shader sees linear ones
        lin = np.where(px <= 0.04045, px / 12.92, ((px + 0.055) / 1.055) ** 2.4)
        mx, mn = lin.max(1), lin.min(1)
        sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
        pick = lin[(sat > 0.5) & (mx > 0.02)]
        _tint_ref_cache[image.name] = lin_hsv(np.median(pick, 0)) if len(pick) else None
    return _tint_ref_cache[image.name]


# Nodes of a custom-coloured weapon stay independent of the panel:
# set_live does not find them with the W_ prefix.
_TINT_NODES = (["ACCENT_ON"] + ["REF_accent_" + c for c in "hsv"] + ["TOL_" + c for c in "hsv"]
               + [cz.live_name("accent", k) for k in st.KEYS + ("b1", "b2")])
WEAPON_TOL = (0.06, 0.45, 0.6)


def apply_tint(mat, s):
    nodes = mat.node_tree.nodes
    for nm in _TINT_NODES:
        nodes[nm].name = "W_" + nm
    t = Vector(s.weapon_tint)
    # Light is the same colour made brighter, not mixed with white, which would turn red into pink
    light = Vector([min(1.0, c * 1.8 + 0.02) for c in t])
    pal = {"dark": t * 0.18, "mid": t, "light": light}
    for k, v in pal.items():
        nodes["W_" + cz.live_name("accent", k)].outputs[0].default_value = tuple(v) + (1.0,)
    nodes["W_" + cz.live_name("accent", "b1")].outputs[0].default_value = s.metal_b1
    nodes["W_" + cz.live_name("accent", "b2")].outputs[0].default_value = s.metal_b2
    for c, v in zip("hsv", WEAPON_TOL):
        nodes["W_TOL_" + c].outputs[0].default_value = v


def build_for(meshes, diffuse, metallic, s, accent=True, tint=False, emissive=None):
    if not meshes:
        return "no meshes"
    if diffuse is None:
        # No textures: zones come from material slot names, palette from Z_*.
        for o in meshes:
            me = o.data
            if not me.materials:
                me.materials.append(bpy.data.materials["Z_cloth"])
            for i, m in enumerate(me.materials):
                me.materials[i] = bpy.data.materials[cz.zone_by_name(m.name if m else "")]
        return "no textures, zones taken from slot names"
    ref = tint_ref(diffuse) if tint else None
    if ref is not None:
        acc = ref
    else:
        acc = lin_hsv(s.accent_color) if s.accent_on and accent else None
    mat = cz.build_auto_material(
        diffuse, metallic, palette_from_settings(s), lin_hsv(s.skin_color), acc,
        (s.tol_h, s.tol_s, s.tol_v), zonemap=s.zonemap, ink=s.ink, metal_t=s.metal_t,
        emissive=emissive, glow=s.glow_color)
    if emissive is not None and not s.glow_on:
        mat.node_tree.nodes["GLOW_ON"].outputs[0].default_value = 0.0
    if ref is not None:
        if not s.zonemap:
            apply_tint(mat, s)
    elif not accent:
        # The live Accent checkbox must not turn it back on: after the rename
        # set_live cannot find this node.
        mat.node_tree.nodes["ACCENT_ON"].name = "ACCENT_LOCKED"
    for o in meshes:
        o.data.materials.clear()
        o.data.materials.append(mat)
    return "metal %s" % ("from map" if metallic else "map missing")


def rebuild_materials(s):
    """Rebuild model and weapon materials from the current panel settings."""
    body = body_meshes()
    report = []
    if body:
        d, m = s.diffuse, s.metallic
        if d is None:
            d, m = cz.find_source_textures(body)
            s["_skip"] = 1
            s.diffuse, s.metallic = d, m
            del s["_skip"]
        report.append("body: " + build_for(body, d, m, s, emissive=s.emissive))
    weapons = {}
    for o in weapon_meshes():
        weapons.setdefault(o.get("caps_weapon"), []).append(o)
    for key, meshes in weapons.items():
        d, m = cz.find_source_textures(meshes)
        if d is None:
            # Repainting already removed the source material; the object remembers its textures.
            d = bpy.data.images.get(meshes[0].get("caps_diffuse", ""))
            m = bpy.data.images.get(meshes[0].get("caps_metallic", ""))
        report.append("%s: %s" % (key, build_for(meshes, d, m, s, accent=s.weapon_accent,
                                                 tint=s.weapon_tint_on)))
    drop_auto_materials()
    st.set_smooth(body + weapon_meshes(), s.smooth)
    return "; ".join(report)


def remember_textures(meshes):
    """Remember the source textures on the object before repainting removes them."""
    d, m = cz.find_source_textures(meshes)
    for o in meshes:
        if d is not None:
            o["caps_diffuse"] = d.name
        if m is not None:
            o["caps_metallic"] = m.name


# ── live panel updates ────────────────────────────────────────────────────

def _busy(s):
    return bool(s.get("_skip"))


def upd_zone(zone, key):
    def f(self, ctx):
        if not _busy(self):
            st.write_zone(zone, key, getattr(self, "%s_%s" % (zone, key)))
    return f


def upd_ref(prefix, prop):
    def f(self, ctx):
        if _busy(self):
            return
        for c, v in zip("hsv", lin_hsv(getattr(self, prop))):
            cz.set_live("REF_%s_%s" % (prefix, c), v)
    return f


def upd_accent_on(self, ctx):
    if not _busy(self):
        cz.set_live("ACCENT_ON", 1.0 if self.accent_on else 0.0)
        upd_ref("accent", "accent_color")(self, ctx)


def upd_tol(self, ctx):
    if not _busy(self):
        for c in "hsv":
            cz.set_live("TOL_" + c, getattr(self, "tol_" + c))


def upd_ink(self, ctx):
    if not _busy(self):
        cz.set_live("INK", self.ink)


def upd_glow(self, ctx):
    if not _busy(self):
        cz.set_live("GLOW_ON", 1.0 if self.glow_on else 0.0)
        cz.set_live("GLOW_COL", self.glow_color)


def upd_metal_t(self, ctx):
    if not _busy(self):
        cz.set_live("METAL_T", self.metal_t)


def upd_lines(self, ctx):
    if _busy(self):
        return
    st.set_line_px("LA_contour", self.contour_px)
    st.set_line_px("LA_crease", self.crease_px)
    st.set_line_colour(self.line_color)


def upd_smooth(self, ctx):
    if not _busy(self):
        st.set_smooth(body_meshes() + weapon_meshes(), self.smooth)


def upd_zonemap(self, ctx):
    if not _busy(self):
        rebuild_materials(self)


def upd_view(self, ctx):
    if not _busy(self):
        apply_view(self)


def load_preset(s, preset):
    """Preset -> panel properties -> all live nodes at once."""
    p = st.PRESETS[preset]
    pal = st.preset_palette(preset)
    s["_skip"] = 1
    try:
        for z in cz.ZONES:
            for k in st.KEYS:
                setattr(s, "%s_%s" % (z, k), pal[z][k][:3])
            setattr(s, z + "_b1", pal[z]["b1"])
            setattr(s, z + "_b2", pal[z]["b2"])
        s.contour_px, s.crease_px = p["contour_px"], p["crease_px"]
        s.line_color = cz.hex_to_rgb(p["line"])
        s.ink, s.smooth = p["ink"], p["smooth"]
    finally:
        del s["_skip"]
    st.apply_preset(preset, body_meshes() + weapon_meshes())


def upd_preset(self, ctx):
    if not _busy(self):
        load_preset(self, self.preset)


# ── properties ────────────────────────────────────────────────────────────

def bone_items(self, ctx):
    arm = armature()
    items = [(b.name, b.name, "") for b in arm.data.bones] if arm else []
    # The list must stay alive while displayed, or Blender reads garbage.
    bone_items.cache = items or [("", "no armature", "")]
    return bone_items.cache


class CapsAnim(bpy.types.PropertyGroup):
    action: PointerProperty(type=bpy.types.Action)
    frames: IntProperty(name="Frames", default=6, min=1, max=64)
    loop: BoolProperty(name="Loop", default=False)
    render: BoolProperty(name="Render", default=True)


_pal_defaults = st.preset_palette(st.DEFAULT_PRESET)
_p = st.PRESETS[st.DEFAULT_PRESET]


class CapsSettings(bpy.types.PropertyGroup):
    kind: EnumProperty(name="Type", items=[
        ('ENEMY', "Enemy", "three-quarter view, faces left, saved to Art/Characters/enemies"),
        ('SQUAD', "Ally", "-55 degrees, faces right, saved to Art/Characters/squad"),
        ('PROP', "Prop", "front view, saved to Art/Props")], default='ENEMY', update=upd_view)
    name: StringProperty(name="Name", description="Latin letters: boxer, raider, barrel")

    view: EnumProperty(name="View", items=[
        ('AUTO', "By type", "enemy: three-quarter (-35), ally: -55, prop: front"),
        ('TQ', "Three-quarter", ""), ('FRONT', "Front", ""),
        ('PROFILE', "Profile", ""), ('BACK', "Back", ""),
        ('CUSTOM', "Custom angle", "")], default='AUTO', update=upd_view)
    yaw: FloatProperty(name="Yaw", default=-35.0, min=-180, max=180, update=upd_view)
    mirror: EnumProperty(name="Mirror", items=[
        ('AUTO', "By type", "enemies are mirrored, others are not"),
        ('ON', "On", ""), ('OFF', "Off", "")], default='AUTO', update=upd_view)

    preset: EnumProperty(name="Style", items=[
        (k, v["label"], "") for k, v in st.PRESETS.items()],
        default=st.DEFAULT_PRESET, update=upd_preset)

    skin_color: FloatVectorProperty(name="Skin", subtype='COLOR', size=3, min=0, max=1,
                                    default=cz.hex_to_rgb(cz.DEFAULT_SKIN),
                                    update=upd_ref("skin", "skin_color"))
    accent_on: BoolProperty(name="Accent", default=False, update=upd_accent_on)
    weapon_accent: BoolProperty(name="Accent on weapon", default=False, update=upd_zonemap,
                                description="Off: the weapon is painted without the accent. A blade in the accent "
                                            "colour would take all the colour from the figure")
    weapon_tint_on: BoolProperty(name="Custom blade colour", default=False, update=upd_zonemap,
                                 description="The most saturated colour of the weapon texture becomes three tones "
                                             "of this colour. The hilt and other metal stay as they are")
    weapon_tint: FloatVectorProperty(name="Blade colour", subtype='COLOR', size=3, min=0, max=1,
                                     default=cz.hex_to_rgb("#a3191c"), update=upd_zonemap)
    accent_color: FloatVectorProperty(name="Accent colour", subtype='COLOR', size=3, min=0, max=1,
                                      default=(0.5, 0.5, 0.5), update=upd_ref("accent", "accent_color"))
    tol_h: FloatProperty(name="H", default=cz.DEFAULT_TOL[0], min=0, max=0.5, update=upd_tol)
    tol_s: FloatProperty(name="S", default=cz.DEFAULT_TOL[1], min=0, max=1, update=upd_tol)
    tol_v: FloatProperty(name="V", default=cz.DEFAULT_TOL[2], min=0, max=1, update=upd_tol)
    ink: FloatProperty(name="Ink", default=_p["ink"], min=0, max=0.5,
                       description="Diffuse texels darker than this go to the dark tone. 0 turns it off",
                       update=upd_ink)
    metal_t: FloatProperty(name="Metal threshold", default=cz.DEFAULT_METAL_T, min=0, max=1,
                           description="Metallic map values above this become the metal zone. Lower it for a weak map",
                           update=upd_metal_t)
    zonemap: BoolProperty(name="Zone map", default=False, update=upd_zonemap,
                          description="Flat zone colours instead of the style: orange skin, blue metal, yellow accent")

    contour_px: FloatProperty(name="Contour, px", default=_p["contour_px"], min=0, max=30, update=upd_lines)
    crease_px: FloatProperty(name="Creases, px", default=_p["crease_px"], min=0, max=30, update=upd_lines)
    line_color: FloatVectorProperty(name="Line", subtype='COLOR', size=3, min=0, max=1,
                                    default=cz.hex_to_rgb(_p["line"]), update=upd_lines)
    smooth: IntProperty(name="Smooth shapes", default=_p["smooth"], min=0, max=40, update=upd_smooth,
                        description="Smooth iterations: removes shadow ripple on lumpy generated meshes")

    diffuse: PointerProperty(type=bpy.types.Image, name="Diffuse")
    metallic: PointerProperty(type=bpy.types.Image, name="Metallic")
    emissive: PointerProperty(type=bpy.types.Image, name="Emissive")
    glow_on: BoolProperty(name="Glow", default=True, update=upd_glow,
                          description="Bright areas of the Rodin emissive map become a flat bright colour "
                                      "that ignores light and ink: lenses, lamps")
    glow_color: FloatVectorProperty(name="Glow colour", subtype='COLOR', size=3, min=0, max=1,
                                    default=(1.0, 1.0, 1.0), update=upd_glow,
                                    description="Multiplied by the map colour. White keeps the generated colour")

    anims: CollectionProperty(type=CapsAnim)
    anim_index: IntProperty()

    weapon_bone: EnumProperty(name="Bone", items=bone_items)

    origin: FloatVectorProperty(name="Origin", size=2, default=(0.5, 0.9))
    sprite_size: FloatProperty(name="Size", default=96.1)
    has_origin: BoolProperty(default=False)

    show_palette: BoolProperty(name="Palette", default=False)
    show_zones: BoolProperty(name="Zones", default=False)


for _z in cz.ZONES:
    for _k in st.KEYS:
        CapsSettings.__annotations__["%s_%s" % (_z, _k)] = FloatVectorProperty(
            name=_k, subtype='COLOR', size=3, min=0, max=1,
            default=_pal_defaults[_z][_k][:3], update=upd_zone(_z, _k))
    for _k in ("b1", "b2"):
        CapsSettings.__annotations__["%s_%s" % (_z, _k)] = FloatProperty(
            name="Threshold " + _k[1], min=0, max=1, default=_pal_defaults[_z][_k],
            update=upd_zone(_z, _k))


# ── project folder ────────────────────────────────────────────────────────

def addon_prefs():
    a = bpy.context.preferences.addons.get(__name__)
    return a.preferences if a else None


def find_project_root():
    """Add-on preference, then a project containing the add-on, then the open file's project."""
    p = addon_prefs()
    if p and p.project_root:
        return os.path.normpath(bpy.path.abspath(p.project_root))
    pipe = os.path.normpath(os.path.join(ADDON_DIR, "..", "..", ".."))
    if os.path.basename(pipe) == "_pipeline" and os.path.basename(os.path.dirname(pipe)) == "Art":
        return os.path.dirname(os.path.dirname(pipe))
    cur = os.path.dirname(bpy.data.filepath) if bpy.data.filepath else ""
    while cur and os.path.dirname(cur) != cur:
        if os.path.basename(cur) == "Art":
            return os.path.dirname(cur)
        cur = os.path.dirname(cur)
    return None


def project_root():
    root = find_project_root()
    if root is None:
        raise RuntimeError("Set the project folder in Preferences > Add-ons > CAPS Style Render")
    return root


class CAPS_AP_prefs(bpy.types.AddonPreferences):
    bl_idname = __name__
    project_root: StringProperty(name="Project folder", subtype='DIR_PATH',
                                 description="Output goes to Art/ and Assets/ inside this folder")

    def draw(self, ctx):
        self.layout.prop(self, "project_root")


# ── operators ─────────────────────────────────────────────────────────────

def art_dir(s):
    return os.path.join(project_root(), "Art", *cz.SIDES[side_key(s)][0].split("/"), s.name)


def asset_dir(s):
    return os.path.join(project_root(), "Assets", *cz.SIDES[side_key(s)][1].split("/"), s.name)


def asset_ref(s):
    return "%s/%s" % (cz.SIDES[side_key(s)][1], s.name)


class CAPS_OT_new(bpy.types.Operator):
    """Create a character file from the render template: Art/.../<name>/<name>.blend"""
    bl_idname = "caps.new"
    bl_label = "Create File"

    kind: EnumProperty(items=CapsSettings.__annotations__["kind"].keywords["items"])
    name: StringProperty(name="Name")

    def invoke(self, ctx, event):
        s = ctx.scene.limbus if hasattr(ctx.scene, "limbus") else None
        if s is not None:
            self.kind, self.name = s.kind, s.name
        return ctx.window_manager.invoke_props_dialog(self)

    def execute(self, ctx):
        name = self.name.strip().lower().replace(" ", "_")
        if not name:
            self.report({'ERROR'}, "name required")
            return {'CANCELLED'}
        sub = cz.SIDES[self.kind.lower()][0]
        try:
            root = project_root()
        except RuntimeError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        folder = os.path.join(root, "Art", *sub.split("/"), name)
        path = os.path.join(folder, name + ".blend")
        if os.path.exists(path):
            bpy.ops.wm.open_mainfile(filepath=path)
            self.report({'INFO'}, "file already exists, opened: " + path)
            return {'FINISHED'}
        create_from_template(self.kind, name, path)
        # Do not touch self after open_mainfile: the operator belonged to the old file
        return {'FINISHED'}


def is_template():
    return os.path.normcase(os.path.abspath(bpy.data.filepath or "")) == os.path.normcase(TEMPLATE)


def create_from_template(kind, name, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    bpy.ops.wm.open_mainfile(filepath=TEMPLATE)
    s = bpy.context.scene.limbus
    s["_skip"] = 1
    s.kind, s.name = kind, name
    del s["_skip"]
    load_preset(s, st.DEFAULT_PRESET)
    apply_view(s)
    # save_as moves the open file to the new path; the template on disk stays untouched
    bpy.ops.wm.save_as_mainfile(filepath=path)
    print("CAPS: created", path)


class CAPS_OT_import_model(bpy.types.Operator, ImportHelper):
    """Load the model: from Rodin (no skeleton) or Mixamo (with skeleton). Replaces the current model"""
    bl_idname = "caps.import_model"
    bl_label = "Load Model"
    filter_glob: StringProperty(default="*.fbx;*.glb;*.gltf;*.obj", options={'HIDDEN'})

    def execute(self, ctx):
        s = ctx.scene.limbus
        old = model_objects()
        remove_objects(old)
        s.diffuse = s.metallic = s.emissive = None
        try:
            objs = import_any(self.filepath)
        except RuntimeError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        cm = fix_centimetres(objs)
        move_to(model_collection(), objs)

        # A skinned Mixamo file carries its own animation; add it to the list right away
        arm = armature()
        stem = os.path.splitext(os.path.basename(self.filepath))[0].lower().replace(" ", "_")
        if arm and arm.animation_data and arm.animation_data.action:
            add_anim(s, arm.animation_data.action, stem)
        if arm and "mixamorig:RightHand" in arm.data.bones:
            s.weapon_bone = "mixamorig:RightHand"

        rep = rebuild_materials(s)
        apply_view(s)
        if not s.name:
            s.name = stem
        msg = "height %.2f m%s; %s" % (world_height(body_meshes()),
                                      ", scale corrected" if cm else "", rep)
        if s.metallic is None and s.diffuse is not None:
            msg += ". No metal map: load the Rodin textures with the button below"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


IMAGE_EXT = (".png", ".jpg", ".jpeg", ".tga", ".webp")


def textures_from_folder(path):
    """Images from a Rodin download folder: texture_diffuse, texture_metallic and
    texture_emissive (the last may be missing and is almost always black).
    A picked image with any other name is used as the colour map."""
    folder = os.path.dirname(path)
    names = sorted(os.listdir(folder))

    def pick(word):
        for n in names:
            if word in n.lower() and n.lower().endswith(IMAGE_EXT):
                return bpy.data.images.load(os.path.join(folder, n), check_existing=True)
        return None

    d, m, e = pick("diffuse"), pick("metal"), pick("emissive")
    base = os.path.basename(path).lower()
    if d is None and path.lower().endswith(IMAGE_EXT) and "metal" not in base and "emissive" not in base:
        d = bpy.data.images.load(path, check_existing=True)
    if e is not None and not has_glow(e):
        bpy.data.images.remove(e)
        e = None
    return d, m, e


def has_glow(image):
    """Rodin often exports an empty emissive map; it should not enter the material."""
    px = np.empty(image.size[0] * image.size[1] * 4, np.float32)
    image.pixels.foreach_get(px)
    return float(px.reshape(-1, 4)[::3, :3].max()) > 0.3


class CAPS_OT_textures(bpy.types.Operator, ImportHelper):
    """Load generator textures from a *_basic_pbr.fbx file or from images
(pick any of texture_diffuse.png and texture_metallic.png in the folder).
Mixamo returns one image and no metal map, but keeps the UVs,
so Rodin textures fit its mesh as they are"""
    bl_idname = "caps.textures"
    bl_label = "Load Rodin Textures"
    filter_glob: StringProperty(default="*.fbx;*.glb;*.gltf;*.png;*.jpg;*.jpeg;*.tga;*.webp",
                                options={'HIDDEN'})

    def execute(self, ctx):
        s = ctx.scene.limbus
        e = None
        if self.filepath.lower().endswith(IMAGE_EXT):
            d, m, e = textures_from_folder(self.filepath)
        else:
            objs = import_any(self.filepath)
            d, m = cz.find_source_textures([o for o in objs if o.type == 'MESH'])
            mats = {sl.material for o in objs if o.type == 'MESH' for sl in o.material_slots if sl.material}
            remove_objects(objs)
            for mt in mats:
                if mt.users == 0:
                    bpy.data.materials.remove(mt)
        if d is None:
            self.report({'ERROR'}, "no textures in this file")
            return {'CANCELLED'}
        for im in (d, m, e):
            if im is not None:
                im.use_fake_user = True
        s.diffuse, s.metallic, s.emissive = d, m, e
        self.report({'INFO'}, rebuild_materials(s))
        return {'FINISHED'}


class CAPS_OT_weapon(bpy.types.Operator, ImportHelper):
    """Attach a separate weapon model to a bone. It follows the bone in every animation"""
    bl_idname = "caps.weapon"
    bl_label = "Attach Weapon"
    filter_glob: StringProperty(default="*.fbx;*.glb;*.gltf;*.obj", options={'HIDDEN'})
    length: FloatProperty(name="Length, m", default=0.0, min=0.0,
                          description="Scale the longest side to this value. 0 keeps the imported size")

    def execute(self, ctx):
        s = ctx.scene.limbus
        arm = armature()
        bone = s.weapon_bone
        if arm is None or bone not in arm.data.bones:
            self.report({'ERROR'}, "load a model with a skeleton (Mixamo) and pick a bone first")
            return {'CANCELLED'}
        objs = import_any(self.filepath)
        fix_centimetres(objs)
        key = os.path.splitext(os.path.basename(self.filepath))[0].lower().replace(" ", "_")
        meshes = [o for o in objs if o.type == 'MESH']
        remember_textures(meshes)

        # A holder empty: the artist moves this, the model stays as imported inside it.
        holder = bpy.data.objects.new("W_" + key, None)
        holder.empty_display_type = 'ARROWS'
        holder.empty_display_size = 0.1
        model_collection().objects.link(holder)
        for o in objs:
            if o.parent is None:
                o.parent = holder
        bpy.context.view_layer.update()

        if self.length > 0 and meshes:
            pts = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
            size = max(max(p[i] for p in pts) - min(p[i] for p in pts) for i in range(3))
            if size > 0:
                holder.scale = (self.length / size,) * 3

        # The Mixamo skeleton is scaled x0.01; without compensation the weapon
        # inherits that and becomes a hundred times smaller.
        want = holder.scale.x
        holder.parent = arm
        holder.parent_type = 'BONE'
        holder.parent_bone = bone
        holder.location = (0.0, 0.0, 0.0)   # the empty's origin sits at the bone tip, in the palm
        holder.rotation_euler = (0.0, 0.0, 0.0)
        holder.scale = (want / arm.matrix_world.to_scale().x,) * 3
        holder["caps_weapon"] = key
        for o in objs:
            o["caps_weapon"] = key
        move_to(model_collection(), objs)
        rebuild_materials(s)
        bpy.ops.object.select_all(action='DESELECT')
        holder.select_set(True)
        ctx.view_layer.objects.active = holder
        self.report({'INFO'}, "%s on bone %s. Move and rotate the empty W_%s (G/R), "
                    "the weapon follows it" % (key, bone, key))
        return {'FINISHED'}


def add_anim(s, action, name):
    action.name = name
    action.use_fake_user = True
    for a in s.anims:
        if a.action == action or a.action is None:
            break
    else:
        a = s.anims.add()
    a.action = action
    a.frames = budget_for(name)
    a.loop = any(k in name for k in LOOPING)
    return a


def assign_action(arm, act):
    ad = arm.animation_data or arm.animation_data_create()
    ad.action = act
    # Blender 4.4+: actions have slots, and without a slot nothing moves
    if hasattr(ad, "action_slot") and ad.action_slot is None and len(act.slots):
        ad.action_slot = act.slots[0]


class CAPS_OT_add_anims(bpy.types.Operator, ImportHelper):
    """Add Mixamo animations, several at once. The file name becomes the animation name:
idle, walk, light, heavy, block, dodge, hit, death. Download them all for the same
uploaded Mixamo character, with In Place enabled"""
    bl_idname = "caps.add_anims"
    bl_label = "Add Animations"
    filter_glob: StringProperty(default="*.fbx", options={'HIDDEN'})
    files: CollectionProperty(type=bpy.types.OperatorFileListElement)
    directory: StringProperty(subtype='DIR_PATH')

    def execute(self, ctx):
        s = ctx.scene.limbus
        arm = armature()
        if arm is None:
            self.report({'ERROR'}, "load a model with a skeleton first")
            return {'CANCELLED'}
        names = [f.name for f in self.files] or [os.path.basename(self.filepath)]
        added = []
        for fn in names:
            path = os.path.join(self.directory or os.path.dirname(self.filepath), fn)
            before = {k: set(getattr(bpy.data, k)) for k in ("materials", "images")}
            objs = import_any(path)
            src = next((o for o in objs if o.type == 'ARMATURE'
                        and o.animation_data and o.animation_data.action), None)
            act = src.animation_data.action if src else None
            remove_objects(objs)
            for k in before:
                coll = getattr(bpy.data, k)
                for d in set(coll) - before[k]:
                    if d.users == 0:
                        coll.remove(d)
            if act is None:
                continue
            stem = os.path.splitext(fn)[0].lower().replace(" ", "_")
            add_anim(s, act, stem)
            added.append(stem)
        if s.anims:
            s.anim_index = len(s.anims) - 1
            show_anim(s, s.anims[s.anim_index])
        self.report({'INFO'}, "added: " + (", ".join(added) or "nothing"))
        return {'FINISHED'}


def show_anim(s, a):
    arm = armature()
    if arm is None or a.action is None:
        return
    assign_action(arm, a.action)
    arm.data.pose_position = 'POSE'
    fr = a.action.frame_range
    sc = bpy.context.scene
    sc.frame_start, sc.frame_end = int(fr[0]), int(fr[1])
    sc.frame_set(int(fr[0]))


class CAPS_OT_show_anim(bpy.types.Operator):
    """Apply the animation to the skeleton and play it"""
    bl_idname = "caps.show_anim"
    bl_label = "Play"
    index: IntProperty()

    def execute(self, ctx):
        s = ctx.scene.limbus
        s.anim_index = self.index
        show_anim(s, s.anims[self.index])
        if not ctx.screen.is_animation_playing:
            bpy.ops.screen.animation_play()
        return {'FINISHED'}


class CAPS_OT_remove_anim(bpy.types.Operator):
    """Remove the animation from the list"""
    bl_idname = "caps.remove_anim"
    bl_label = "Remove"

    def execute(self, ctx):
        s = ctx.scene.limbus
        if 0 <= s.anim_index < len(s.anims):
            a = s.anims[s.anim_index]
            if a.action:
                a.action.use_fake_user = False
            s.anims.remove(s.anim_index)
            s.anim_index = max(0, s.anim_index - 1)
        return {'FINISHED'}


class CAPS_OT_camera(bpy.types.Operator):
    """Look through the render camera with the style applied"""
    bl_idname = "caps.camera"
    bl_label = "Camera View"

    def execute(self, ctx):
        ctx.scene.camera = bpy.data.objects.get("CAPS_Cam")
        for area in ctx.screen.areas:
            if area.type == 'VIEW_3D':
                sp = area.spaces.active
                sp.region_3d.view_perspective = 'CAMERA'
                sp.shading.type = 'RENDERED'
        return {'FINISHED'}


class CAPS_OT_preset(bpy.types.Operator):
    """Reset all style settings to the selected preset"""
    bl_idname = "caps.preset"
    bl_label = "Reset to Preset"

    def execute(self, ctx):
        s = ctx.scene.limbus
        load_preset(s, s.preset)
        return {'FINISHED'}


class CAPS_OT_rebuild(bpy.types.Operator):
    """Rebuild materials if something was painted incorrectly"""
    bl_idname = "caps.rebuild"
    bl_label = "Rebuild"

    def execute(self, ctx):
        self.report({'INFO'}, rebuild_materials(ctx.scene.limbus))
        return {'FINISHED'}


# ── render ────────────────────────────────────────────────────────────────

def _prepare_render(s):
    sc = bpy.context.scene
    sc.camera = bpy.data.objects.get("CAPS_Cam")
    sc.render.film_transparent = True
    sc.render.image_settings.file_format = 'PNG'
    sc.render.image_settings.color_mode = 'RGBA'
    if s.zonemap:
        s.zonemap = False


def _render_to(path):
    sc = bpy.context.scene
    bpy.ops.render.render()
    # Overwriting a PNG on Windows sometimes fails ("cannot save"): Explorer previews
    # or antivirus hold the old file for a moment. Save manually, with retries.
    res = bpy.data.images["Render Result"]
    for attempt in range(10):
        try:
            if os.path.exists(path):
                os.remove(path)
            res.save_render(filepath=path, scene=sc)
            break
        except (OSError, RuntimeError):
            if attempt == 9:
                raise
            time.sleep(0.3)
    im = bpy.data.images.load(path)
    w, h = im.size
    al = np.array(im.pixels[:], dtype=np.float32).reshape(h, w, 4)[:, :, 3]
    bpy.data.images.remove(im)
    return al, w, h


def render_still(s):
    """Rest pose -> PNG, Origin and Size. These values are shared by all animations."""
    _prepare_render(s)
    apply_view(s)
    arm = armature()
    old = arm.data.pose_position if arm else None
    if arm:
        arm.data.pose_position = 'REST'
    bpy.context.view_layer.update()
    folder = os.path.join(art_dir(s), "render")
    os.makedirs(folder, exist_ok=True)
    out = os.path.join(folder, s.name + "_alpha.png")
    # In the rest pose a weapon sticks out sideways and would pull the pivot
    # away from the feet. Measure the body alone.
    weapons = [o for o in weapon_meshes() if not o.hide_render]
    for o in weapons:
        o.hide_render = True
    try:
        al, w, h = _render_to(out)
    finally:
        for o in weapons:
            o.hide_render = False
        if arm:
            arm.data.pose_position = old
    rows = np.where(al.max(axis=1) > 0.02)[0]
    cols = np.where(al.max(axis=0) > 0.02)[0]
    if len(rows) == 0:
        raise RuntimeError("empty frame: is the model outside the camera view?")
    # pixel rows go bottom-up: rows.min() is the lower edge of the figure
    fig = int(rows.max()) - int(rows.min()) + 1
    s.origin = ((int(cols.min()) + int(cols.max())) / 2 / w, 1.0 - int(rows.min()) / h)
    units = world_height(body_meshes()) * cz.UNITS_PER_M
    s.sprite_size = units * h / fig
    s.has_origin = True
    return out


class CAPS_OT_render_still(bpy.types.Operator):
    """Render the still frame: PNG plus the Origin and Size values"""
    bl_idname = "caps.render_still"
    bl_label = "Render Still"

    def execute(self, ctx):
        s = ctx.scene.limbus
        if not s.name or not body_meshes():
            self.report({'ERROR'}, "name and model required")
            return {'CANCELLED'}
        try:
            out = render_still(s)
        except RuntimeError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, "%s  Origin %.3f,%.3f  Size %.1f" % (out, s.origin[0], s.origin[1], s.sprite_size))
        return {'FINISHED'}


def write_sprite(s, blocks):
    """Add animations to the .sprite file: same-named ones are replaced, others stay."""
    path = os.path.join(asset_dir(s), s.name + ".sprite")
    data = {"Animations": [], "__references": [], "__version": 2}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    names = {b["Name"] for b in blocks}
    data["Animations"] = [a for a in data.get("Animations", []) if a.get("Name") not in names] + blocks
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def render_anim(s, a, progress=None):
    arm = armature()
    sc = bpy.context.scene
    show_anim(s, a)
    anim = a.action.name
    start, end = (int(round(v)) for v in a.action.frame_range)
    n = max(a.frames, 1)
    if a.loop or n == 1:
        picks = [start + round(i * (end - start) / n) for i in range(n)]
    else:
        picks = [start + round(i * (end - start) / (n - 1)) for i in range(n)]
    folder = os.path.join(art_dir(s), "render", anim)
    frames = os.path.join(asset_dir(s), "frames")
    os.makedirs(folder, exist_ok=True)
    os.makedirs(frames, exist_ok=True)
    centres = []
    for i, f in enumerate(picks):
        sc.frame_set(int(f))
        path = os.path.join(folder, "%s_%04d.png" % (anim, i))
        al, w, h = _render_to(path)
        cols = np.where(al.max(axis=0) > 0.02)[0]
        centres.append((int(cols.min()) + int(cols.max())) / 2 / w if len(cols) else 0.5)
        shutil.copy2(path, os.path.join(frames, os.path.basename(path)))
        if progress:
            progress()
    fps = sc.render.fps / sc.render.fps_base
    dur = max(end - start, 1) / fps
    block = {
        "Name": anim,
        "FrameRate": max(round(n / dur), 1),
        "Origin": "%.3f,%.3f" % tuple(s.origin),
        "LoopMode": "Loop" if a.loop else "None",   # s&box values: None, Loop, PingPong
        "LoopStart": -1, "LoopEnd": -1,
        "Frames": [{"Texture": "%s/frames/%s_%04d.png" % (asset_ref(s), anim, i),
                    "BroadcastMessages": []} for i in range(n)],
    }
    drift = (max(centres) - min(centres)) if centres else 0.0
    return block, drift


class CAPS_OT_render_anims(bpy.types.Operator):
    """Render the checked animations and write the frames and .sprite file to Assets"""
    bl_idname = "caps.render_anims"
    bl_label = "Render Animations"

    def execute(self, ctx):
        s = ctx.scene.limbus
        todo = [a for a in s.anims if a.render and a.action]
        if not s.name or not body_meshes() or not todo:
            self.report({'ERROR'}, "name, model and at least one checked animation required")
            return {'CANCELLED'}
        try:
            if not s.has_origin:
                render_still(s)
            _prepare_render(s)
            apply_view(s)
            wm = ctx.window_manager
            total = sum(a.frames for a in todo)
            done = [0]
            wm.progress_begin(0, total)

            def tick():
                done[0] += 1
                wm.progress_update(done[0])

            blocks, warn = [], []
            for a in todo:
                b, drift = render_anim(s, a, tick)
                blocks.append(b)
                if drift > 0.12:
                    warn.append("%s drifts across the frame by %.0f%%: download it with In Place" % (b["Name"], drift * 100))
            wm.progress_end()
        except RuntimeError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        path = write_sprite(s, blocks)
        msg = "%d animations -> %s" % (len(blocks), path)
        if warn:
            msg += ". !! " + "; ".join(warn)
            self.report({'WARNING'}, msg)
        else:
            self.report({'INFO'}, msg)
        return {'FINISHED'}


# ── panel ─────────────────────────────────────────────────────────────────

class CAPS_UL_anims(bpy.types.UIList):
    def draw_item(self, ctx, layout, data, item, icon, active_data, active_prop, index):
        row = layout.row(align=True)
        row.prop(item, "render", text="")
        row.label(text=item.action.name if item.action else "—")
        row.prop(item, "frames", text="")
        row.prop(item, "loop", text="", icon='FILE_REFRESH')
        op = row.operator("caps.show_anim", text="", icon='PLAY')
        op.index = index


class CAPS_PT_main(bpy.types.Panel):
    bl_label = "CAPS Style Render"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "CAPS"

    def draw(self, ctx):
        L = self.layout
        s = ctx.scene.limbus

        box = L.box()
        box.row().prop(s, "kind", expand=True)
        box.prop(s, "name")
        if find_project_root() is None and addon_prefs():
            box.label(text="Choose where to save output:", icon='ERROR')
            box.prop(addon_prefs(), "project_root")
        if not is_studio():
            box.label(text="This is not a render file.", icon='ERROR')
            box.operator("caps.new", icon='FILE_NEW')
            return
        if is_template():
            box.label(text="This is the template. Do not save it.", icon='ERROR')
            box.label(text="Work in a character file:")
            box.operator("caps.new", icon='FILE_NEW')
            return
        box.operator("caps.new", text="New Character...", icon='FILE_NEW')

        box = L.box()
        box.label(text="1. Model", icon='OUTLINER_OB_ARMATURE')
        box.operator("caps.import_model", icon='IMPORT')
        row = box.row()
        row.operator("caps.textures", icon='TEXTURE')
        if s.diffuse:
            box.label(text="textures: %s%s%s" % (s.diffuse.name, ", metal" if s.metallic else ", no metal",
                                                ", glow" if s.emissive else ""))
        h = world_height(body_meshes())
        if h:
            box.label(text="height %.2f m = %.1f units" % (h, h * cz.UNITS_PER_M))

        box = L.box()
        box.label(text="2. Weapon", icon='MOD_ARMATURE')
        if armature():
            box.prop(s, "weapon_bone")
            box.operator("caps.weapon", icon='LINKED')
            for o in model_objects():
                if o.type == 'EMPTY' and o.get("caps_weapon"):
                    box.label(text="%s → %s" % (o.name, o.parent_bone), icon='DOT')
        else:
            box.label(text="requires a model with a skeleton (Mixamo)")

        box = L.box()
        box.label(text="3. Animations", icon='ACTION')
        box.template_list("CAPS_UL_anims", "", s, "anims", s, "anim_index", rows=3)
        row = box.row(align=True)
        row.operator("caps.add_anims", icon='ADD')
        row.operator("caps.remove_anim", text="", icon='REMOVE')

        box = L.box()
        box.label(text="4. Style", icon='BRUSH_DATA')
        row = box.row(align=True)
        row.prop(s, "preset", text="")
        row.operator("caps.preset", text="", icon='LOOP_BACK')
        box.operator("caps.camera", icon='CAMERA_DATA')
        col = box.column(align=True)
        col.prop(s, "contour_px")
        col.prop(s, "crease_px")
        col.prop(s, "line_color")
        box.prop(s, "ink")
        box.prop(s, "smooth")

        box.prop(s, "show_palette", icon='TRIA_DOWN' if s.show_palette else 'TRIA_RIGHT')
        if s.show_palette:
            for z in cz.ZONES:
                sub = box.box()
                sub.label(text=z)
                row = sub.row(align=True)
                for k in st.KEYS:
                    row.prop(s, "%s_%s" % (z, k), text="")
                row = sub.row(align=True)
                row.prop(s, z + "_b1")
                row.prop(s, z + "_b2")

        box.prop(s, "show_zones", icon='TRIA_DOWN' if s.show_zones else 'TRIA_RIGHT')
        if s.show_zones:
            box.prop(s, "zonemap")
            box.prop(s, "skin_color")
            box.prop(s, "metal_t")
            row = box.row()
            row.prop(s, "accent_on")
            row.prop(s, "accent_color", text="")
            if s.emissive:
                row = box.row()
                row.prop(s, "glow_on")
                row.prop(s, "glow_color", text="")
            box.prop(s, "weapon_accent")
            row = box.row()
            row.prop(s, "weapon_tint_on")
            row.prop(s, "weapon_tint", text="")
            row = box.row(align=True)
            row.label(text="Tolerance")
            row.prop(s, "tol_h"); row.prop(s, "tol_s"); row.prop(s, "tol_v")
            box.operator("caps.rebuild", icon='FILE_REFRESH')

        box = L.box()
        box.label(text="5. Render", icon='RENDER_STILL')
        box.prop(s, "view")
        if s.view == 'CUSTOM':
            box.prop(s, "yaw")
        box.prop(s, "mirror")
        box.operator("caps.render_still", icon='IMAGE_DATA')
        if s.has_origin:
            box.label(text="Origin %.3f,%.3f   Size %.1f" % (s.origin[0], s.origin[1], s.sprite_size))
        box.operator("caps.render_anims", icon='RENDER_ANIMATION')


CLASSES = (CapsAnim, CapsSettings, CAPS_AP_prefs, CAPS_OT_new, CAPS_OT_import_model,
           CAPS_OT_textures, CAPS_OT_weapon, CAPS_OT_add_anims,
           CAPS_OT_show_anim, CAPS_OT_remove_anim, CAPS_OT_camera,
           CAPS_OT_preset, CAPS_OT_rebuild, CAPS_OT_render_still,
           CAPS_OT_render_anims, CAPS_UL_anims, CAPS_PT_main)


def register():
    importlib.reload(cz)
    importlib.reload(st)
    for c in CLASSES:
        bpy.utils.register_class(c)
    # Keep this key: existing .blend files store their settings under it
    bpy.types.Scene.limbus = PointerProperty(type=CapsSettings)


def unregister():
    del bpy.types.Scene.limbus
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
