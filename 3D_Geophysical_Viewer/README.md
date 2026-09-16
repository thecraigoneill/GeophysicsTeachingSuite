# Kilcoy 3D — LiDAR terrain, switchable drape skins, draped vectors

GeoTIFF and GPKG/SHP in, glTF out. One terrain mesh, any number of draped
raster layers you can flip between at runtime, vector lines and points draped
on the surface, and click-to-inspect that returns real values in real
coordinates.

```
kilcoy_lidar.tif            ──dem2gltf.py────▶  terrain.glb            mesh + UVs
U_GDA2020_resampled.tif  ─┐
<next drape>.tif         ─┼──drape2skin.py──▶  skins/*.png            display skins
<another drape>.tif      ─┘                    skins/*_val.png        exact values
                                               skins/skins.json       manifest
kilcoy_lines.gpkg        ─┐
MagLine 1 (long).shp     ─┼──vec2overlay.py─▶  overlays/overlays.json
Line 2 W.shp             ─┘
                                               terrain_viewer.html    the viewer
```

Everything you supply stays in its native format. The `.png` skins are the one
forced conversion — WebGL samples textures from browser-decodable images, and
GeoTIFF is not one. Georeferencing survives regardless: each PNG is your drape
warped onto the DEM's *exact* CRS, extent and aspect, so the UV layout carries
the registration, and CRS/bounds/value ranges live in `skins.json` and the
GLB's `asset.extras`.

## Install

```bash
pip install numpy rasterio pillow fiona pyproj matplotlib
```

`fiona` is only needed for vector overlays (`geopandas` works instead if you
have it). `matplotlib` is optional but strongly recommended — it supplies the
xkcd/tab/CSS colour names and the full colormap set.

---

## 0. Look before you build

Three dry-run commands. None of them write anything.

```bash
python dem2gltf.py   kilcoy_lidar.tif --info
python drape2skin.py kilcoy_lidar.tif U_GDA2020_resampled.tif --check
python vec2overlay.py kilcoy_lidar.tif kilcoy_lines.gpkg --list
```

- `--info` — raster dimensions, cell size, resulting vertex and triangle counts.
- `--check` — each raster's CRS, native resolution, extent, and the number that
  matters: **what fraction of the *valid* DEM each drape covers**, plus how much
  of the drape spills outside it.
- `--list` — layers, geometry types, feature counts, CRS, and every attribute
  field with its type and numeric range, so you know what to pass to
  `--attribute`.

### Your LiDAR's CRS is broken, and all three scripts work around it

`kilcoy_lidar.tif` carries a `LOCAL_CS` / EngineeringCRS: it is *named*
"GDA2020 / MGA zone 56" but has an "Unknown engineering datum". PROJ refuses to
build a transform between that and a real projected CRS, which is why a naive
reproject fails with `Cannot find coordinate operations from 'EPSG:7856'`.

Each script detects an engineering CRS and recovers the intended EPSG from the
name — GDA2020 MGA zone *n* is `7800 + n`, so yours resolves to **EPSG:7856** —
and says so in its output rather than guessing silently. Overrides:
`--dem-crs`, `--drape-crs`, `--vector-crs`, `--crs`, or `--assume-aligned`.

Better, fix it once at source and nothing downstream ever cares:

```bash
gdal_edit.py -a_srs EPSG:7856 kilcoy_lidar.tif
```

---

## 1. Build the mesh

```bash
python dem2gltf.py kilcoy_lidar.tif -o terrain.glb --stride 2
```

**Pick a stride deliberately.** Your DEM is 3000 × 2000, so full native
resolution is 6 M vertices / 12 M triangles — about 336 MB of GLB, which will
not orbit in a browser. The texture grid is *also* 3000 × 2000, and the drape is
what you actually look at, so striding the mesh while keeping the texture full
costs almost nothing visually:

| | vertices | GLB | texture |
|---|---|---|---|
| `--stride 1` | 6.0 M | ~336 MB | 3000 × 2000 |
| `--stride 2` | 1.5 M | ~84 MB | 3000 × 2000 |
| `--stride 3` | 0.67 M | ~37 MB | 3000 × 2000 |

What it handles:

- **Local origin.** MGA56 eastings near 450 000 quantise to ~3 cm steps in
  float32 — visible terracing. Vertices are written relative to the SW corner
  and the origin is stored in `asset.extras.origin` for the viewer to add back.
- **Axes.** glTF is Y-up: easting → +X, elevation → +Y, northing → −Z.
- **NoData.** Triangles touching a nodata cell are dropped, so voids become
  holes rather than spikes to −9999.
- **Normals** baked from the elevation gradient; **tiling** across primitives
  for large grids (`--max-verts`).

Other flags: `--z-exag` (bake exaggeration in — usually leave at 1.0, the viewer
has a live slider), `--origin centre`, `--crs`.

---

## 2. Build the skins

```bash
python drape2skin.py kilcoy_lidar.tif U_GDA2020_resampled.tif -o skins/
```

Add more later, one at a time — the manifest is appended to, not overwritten:

```bash
python drape2skin.py kilcoy_lidar.tif thorium.tif -o skins/ --cmap magma --label "Th (ppm)"
python drape2skin.py kilcoy_lidar.tif geology.tif -o skins/ --categorical
```

### Partial overlap is expected, not an error

Drapes rarely cover the whole LiDAR footprint, and rewarping between grids of
different resolution leaves hairline nodata fringes. So:

- Coverage is measured against the **valid** DEM, not its bounding box.
- Uncovered ground is written as neutral grey with `alpha = 0`. The viewer then
  shows it as plain grey terrain (default) or punches it out entirely
  ("Cut nodata") — decided at view time, no rebuild.
- `--fill N` (default 2 px) dilates valid colour into adjacent holes to close
  resampling seams, **clamped to the valid DEM footprint**, so it heals a 1–2 px
  crack without inventing data past the survey edge. It reports how many pixels
  it filled.
- `--min-coverage PCT` (default 2) skips a drape that barely overlaps rather
  than emitting an almost-empty skin. Usually a CRS mismatch.

### Value grids

By default each single-band skin also gets a `<name>_val.png` — see
[Click inspection](#4-click-inspection) for why. `--no-values` skips it.

Other flags: `--tex-size 8192` (texture detail, independent of mesh detail),
`--cmap`, `--clip LO HI`, `--vmin/--vmax` (fix the scale so two skins are
directly comparable), `--nearest`, `--reset`.

---

## 3. Drape the vectors

```bash
python vec2overlay.py kilcoy_lidar.tif kilcoy_lines.gpkg kilcoy_lines_2.gpkg \
    "MagLine 1 (long).shp" "Line 2 W.shp" -o overlays/
```

Each **layer** in a GeoPackage becomes its own toggleable overlay; each
shapefile becomes one. What happens to them:

- **Reprojected** to the DEM's grid, with the same LOCAL_CS recovery.
- **Densified** to `--spacing` (default 2 DEM cells) so lines follow topography
  instead of chording across gullies.
- **Elevation sampled** bilinearly from the LiDAR, then lifted by `--offset`
  (default 1.5 m) so they don't z-fight with the ground.
- **Split** wherever they cross DEM nodata, rather than interpolating over voids.
- **Registered** using the local origin read out of `terrain.glb`'s
  `asset.extras`, so overlays cannot drift relative to the mesh. Pass `--model`
  if it isn't found automatically.

Rendering is real vector geometry — mitred `Line2` ribbons with a true pixel
width, and round antialiased dots for points (a bare `PointsMaterial` draws
literal squares, which is what "blocky pixels" looks like).

### Colour, three ways

**1 — default.** Each layer takes the next colour off a palette chosen to stay
readable over both viridis and grey.

**2 — one blunt colour.** Any matplotlib colour spec:

```bash
python vec2overlay.py dem.tif "MagLine 1 (long).shp" -o overlays/ \
    -colour "xkcd:blood red" --width 3
```

`xkcd:blood red`, `tab:orange`, `firebrick`, `#c2185b`, `0.35` all resolve.
There's a hex/CSS fallback without matplotlib, but `xkcd:` names need it.

**3 — an attribute through a colormap.**

```bash
python vec2overlay.py dem.tif mag_points.gpkg -o overlays/ \
    -attribute "Mag" -cmap "magma" --clip 2 98
```

Field lookup is case-insensitive (`mag` finds `Mag`). Numeric fields get a
percentile stretch; text fields are auto-detected and treated as classes.
**Lines colour per feature, points per point.** `--vmin/--vmax` pin the scale
when two layers need to be directly comparable. The colorbar and its range
appear in the viewer panel, and the layer's swatch becomes a mini-ramp.

Single-dash `-colour`, `-attribute`, `-cmap` work, as do the `--` forms.
`--colour` and `--attribute` are alternatives; pick one.

### Carrying attributes for inspection

`--attribute` is carried into the viewer automatically. For anything else:

```bash
--carry-fields Mag,Alt,Line,Fid
```

Those show up in the click banner for whichever node or segment you hit.

---

## 4. Click inspection

Click the terrain and you get easting, northing, elevation, **and the value of
the drape at that point** — a real number in real units, not a colour. Click a
vector node or segment instead and you get its attribute values plus its
coordinates. `Escape` or the × closes the banner.

**Read all** samples every loaded skin at the clicked point, so one click gives
you U, Th and K together with the active layer bolded.

### How the value comes back, and what to trust

Two paths, and the viewer tells you which it used.

**Exact (default).** `drape2skin.py` writes `<name>_val.png` alongside the
display skin: the value packed as 24-bit fixed point across R/G/B, spanning the
**true** data min/max rather than the display stretch. Precision is ~1/16.7M of
the range — exact for any practical purpose, including values in the clipped
tails. These print plain.

> Why not a 16-bit PNG? Canvas2D hands JavaScript 8-bit channels no matter what
> the file contains, so a 16-bit grid is silently truncated on read. Packing
> across three 8-bit channels survives the round trip intact.

**Ramp inversion (fallback).** With `--no-values`, or for skins from an older
run, the viewer inverts the colour ramp instead — matching the sampled pixel
against the 256-entry LUT carried in the manifest, so it works for any
matplotlib colormap. Two honest limits, both surfaced in the UI:

- quantised to 1/256 of the **display** range — these print with `≈`
- anything outside the percentile clip is saturated in the image and its true
  value is simply not there — these print `≤` or `≥` with a footnote

For geophysics that second one matters, since the anomalies you care about tend
to live in exactly those tails. Hence exact-by-default.

---

## 5. View

Put `terrain_viewer.html` next to `terrain.glb`, `skins/` and `overlays/`, then:

```bash
python -m http.server
# open http://localhost:8000/terrain_viewer.html
```

Opening the file straight off disk works too, but `file://` blocks `fetch()`, so
the viewer shows a drop zone — drag in `terrain.glb`, `skins.json`, the PNGs
(including the `_val.png` grids) and `overlays.json` together. Loose PNGs with
no manifest become unlabelled skins.

**Skins:** dropdown, `1`–`9` to jump, `[` / `]` to cycle, `0` for bare terrain.
Coverage is stated next to the selector so grey ground is never mistaken for a
low value.

**Overlays:** one checkbox each, so any combination shows at once, with
**All** / **None**. **Labels** (`L`) draws the attribute label at each line's
midpoint. **X-ray** (`X`) disables depth testing so lines show through ridges.
A live line-width slider.

**Map furniture:**

- **Extent box** (`B`) — bounding box with all four ground corners labelled in
  true eastings and northings. The same four, plus the extent's width × height
  in metres, are listed in the side panel.
- **North arrow**, bottom right, rotating live with the camera.
- **Scale bar** snapping to a round 1/2/5 × 10ⁿ distance. It reads horizontal
  metres, so it stays correct when you change vertical exaggeration.

Labels are 10.5 px monospace with tabular figures on a translucent dark pill —
small, but legible over both backgrounds and any skin.

Plus: live vertical exaggeration, sun azimuth/elevation (a hillshade you can
spin), wireframe, flat shading, nodata cutout, auto-rotate, light/dark
background, reset view.

---

## Why the skins live outside the glTF

One mesh, N images. Swapping is `material.map = tex` — the geometry never
reloads, the camera never resets, and adding a tenth drape next month doesn't
mean re-exporting a multi-million-vertex mesh. The alternatives:

| | geometry copies | new skin later | portable to other viewers |
|---|---|---|---|
| **skins outside the glTF** (this) | 1 | drop in a PNG | needs the viewer |
| all skins as extra materials in one glTF | 1 | re-export mesh | needs viewer code |
| `KHR_materials_variants` | 1 | re-export mesh | yes — Babylon, `<model-viewer>` |
| separate glTF per skin | N | export another | yes, but N× the bytes |

If you later need the model to open in a viewer you don't control, that's the
case for `KHR_materials_variants` — happy to add that export path.

## Folding into petroGLyph

The mesh is ordinary glTF, so `C_gltf_2_htmlPackage/pack.js` will inline it as
is, minus the skin switcher. Two routes to a single self-contained HTML:

1. Add a "Skins" control to `src/app.js` reading a base64 skin table injected by
   `pack.js`, and rebuild the bundle. Keeps everything in your pipeline and
   gives every future model the feature.
2. Inline the GLB, PNGs and JSON into `terrain_viewer.html` as `data:` URIs.

Note `pack.js` currently reads the model as text for embedded `.gltf`. For
`.glb` it needs the binary-buffer tweak your README already mentions — or I can
add a `--format gltf` flag to `dem2gltf.py`.

## Caveats

- **None of this has been run against your actual data.** The sandbox that would
  have let me open your files was down for the whole session, so grid sizes,
  overlap, attribute names and value ranges are all unverified. The three
  dry-run commands in step 0 exist precisely for that — run them first.
- The viewer loads three.js from unpkg, so it needs a network connection.
  For a fully offline file, use the petroGLyph packing route above.
- Canvas value sampling needs the images to be same-origin. Over
  `python -m http.server` or via drag-and-drop it's fine; loading images by
  relative path from a `file://` page will taint the canvas and the viewer will
  report values as unavailable.
- Normals are baked at `--z-exag`, but the viewer's exaggeration slider scales
  the node and three.js corrects normals for non-uniform scale, so lighting
  stays right either way.
