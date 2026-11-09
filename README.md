# CAPS Style Render

A Blender add-on that renders 3D models into hand-drawn looking sprite frames: flat tones,
ink outlines and one fixed camera, so every character looks like part of the same game.

It is built for generated models. You do not need clean topology or manual material setup:
zones such as skin, cloth and metal are read from the model's textures.

## Requirements

- Blender 5.2. Tested on 5.2.3 LTS on Windows.
- A GPU that can run EEVEE.

## Installation

1. Download `caps_style_render_vX.Y.Z.zip` from
   [Releases](../../releases). Do not unpack it.
2. In Blender, open **Edit > Preferences > Add-ons**.
3. Open the arrow menu in the top right corner and choose **Install from Disk**.
4. Select the zip and enable **CAPS Style Render**.
5. In the add-on's preferences, set **Project folder**. Output is saved there.
6. In the 3D View, press **N** and open the **CAPS** tab.

## Workflow

The panel goes from top to bottom.

1. **Create a file.** Choose Enemy, Ally or Prop, enter a name and click **New Character**.
2. **Load the model.** Click **Load Model** and choose an FBX, GLB or OBJ file.
   - If the textures are missing, use **Load Rodin Textures**.
   - For models with animations, rig the model first, for example in Mixamo, without a weapon in
     its hands.
3. **Attach a weapon (optional).** Pick a bone and click **Attach Weapon**, then move and rotate
   the `W_<name>` empty.
4. **Add animations.** Click **Add Animations**. The file name becomes the animation name. In
   Mixamo, download every animation for the same uploaded character with **In Place** enabled.
5. **Adjust the style.** Use **Camera View** to see the result and **Zone map** to check the zones.
6. **Pack textures.** Choose **File > External Data > Pack Resources**, then save. Otherwise the
   textures stay in their original folder and the model renders grey on another computer, without
   an error.
7. **Render.** Click **Render Still** first, then **Render Animations**.

## Output

```
<Project folder>/
├── Art/
│   ├── Characters/enemies/<name>/      .blend file and render/ frames
│   ├── Characters/squad/<name>/
│   └── Props/<name>/
└── Assets/
    ├── characters/enemies/<name>/      frames/ and <name>.sprite
    ├── characters/squad/<name>/
    └── props/<name>/
```

Frames are PNG files with transparency, so they work in any engine. The `.sprite` file is for
[s&box](https://sbox.game); other engines can ignore it.

**Render Still** also shows two values shared by all animations:

- **Origin** is the pivot at the character's feet, as a fraction of the frame.
- **Size** is the frame height in s&box units, where 72 units equal a 1.9 m person. Rescale it for
  other engines.

## Troubleshooting

| Problem | Try this |
|---|---|
| Armour is not recognised as metal | Lower **Metal threshold** |
| Skin colour spreads onto light cloth | Pick the skin colour from the skin itself and lower the S tolerance |
| Dark armour is completely black | Set **Ink** to 0 |
| Ripple in the shadows | Raise **Smooth shapes** |
| The model is huge or tiny | Check its height on the panel. Centimetre and metre scale mistakes are corrected on import |
| A warning says the animation drifts | Download it again with **In Place** enabled |

## Camera

The camera is orthographic and tilted 24 degrees down. It is the same for every character, so size
differences between characters survive. Match your game camera to this tilt, or change it in
`render_template.blend`.

## License

See [LICENSE](LICENSE).
