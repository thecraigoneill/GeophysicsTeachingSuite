# LiDAR terrain + switchable drape skins

GeoTIFF in, glTF out, with any number of draped GeoTIFF layers you can flip
between at runtime without reloading the mesh.

```
kilcoy_lidar.tif            ──dem2gltf.py────▶  terrain.glb      (mesh + UVs)
U_GDA2020_resampled.tif  ─┐
<next drape>.tif         ─┼──drape2skin.py──▶  skins/*.png      (one per drape)
<another drape>.tif      ─┘                    skins/skins.json (manifest)
kilcoy_lines.gpkg        ─┐
MagLine 1 (long).shp     ─┼──vec2overlay.py─▶  overlays/overlays.json
Line 2 W.shp             ─┘                    terrain_viewer.html
```

Everything you supply is a GeoTIFF. The `.png` skins are the one forced format
change — WebGL samples textures from browser-decodable images, and GeoTIFF is
not one of them. Georeferencing survives anyway, because each PNG is your drape
warped onto the DEM's *exact* CRS, extent and aspect, so the UV layout carries
the registration. CRS, bounds and value ranges are recorded in both
`skins.json` and the GLB's `asset.extras`; click the terrain in the viewer and
you get true GDA2020 eastings/northings back.

## Install

```bash
pip install numpy rasterio pillow fiona pyproj
pip install matplotlib          # optional, unlocks all matplotlib colormaps
```

`fiona` is only needed for the vector overlays, and `geopandas` works instead
if you already have it.

## 1. Look before you build

```bash
python dem2gltf.py kilcoy_lidar.tif --info
python drape2skin.py kilcoy_lidar.tif U_GDA2020_resampled.tif --check
```

`--check` reports each raster's CRS, native resolution, extent, and — the number
that actually matters — **what fraction of the *valid* DEM each drape covers**,
plus how much of the drape spills outside the DEM. Neither command writes
anything.

## 2. Build the mesh

```bash
python dem2gltf.py kilcoy_lidar.tif -o terrain.glb
```

Full native resolution by default: every LiDAR cell becomes a vertex. If
orbiting stutters, `--stride 2` quarters the vertex count.

What it handles:

- **Local origin.** MGA56 eastings around 450 000 quantise to ~3 cm steps in
  float32, which is visible as terracing. Vertices are written relative to the
  SW corner and the origin is stored in `asset.extras.origin` for the viewer to
  add back.
- **Axes.** glTF is Y-up: easting → +X, elevation → +Y, northing → −Z.
- **NoData.** Triangles touching a nodata cell are dropped, so voids become
  holes rather than spikes to −9999.
- **Normals** are baked from the elevation gradient.
- **Tiling.** Large grids split across primitives (`--max-verts`).

Useful flags: `--z-exag` (bake exaggeration in — usually leave at 1.0, the
viewer has a live slider), `--origin centre`, `--stride N`.

## 3. Build the skins

```bash
python drape2skin.py kilcoy_lidar.tif U_GDA2020_resampled.tif -o skins/
```

Add more later, one at a time — the manifest is appended to, not overwritten:

```bash
python drape2skin.py kilcoy_lidar.tif thorium.tif -o skins/ --cmap magma --label "Th (ppm)"
python drape2skin.py kilcoy_lidar.tif geology.tif -o skins/ --categorical
```

### Partial overlap

Drapes usually don't cover the whole LiDAR footprint, and rewarping between
grids of different resolution leaves hairline nodata fringes at the edges. The
script treats that as normal, not as an error:

- Coverage is measured against the **valid** DEM, not its bounding box.
- Uncovered ground is written as neutral grey with `alpha = 0`. The viewer can
  then show it as plain grey terrain (default) or punch it out entirely
  ("Cut nodata") — your call at view time, no rebuild.
- `--fill N` (default 2 px) dilates valid colour into adjacent holes to close
  resampling seams, **but only inside the valid DEM footprint**, so it can heal
  a 1–2 px crack without inventing data off the edge of the survey. It tells
  you exactly how many pixels it filled.
- `--min-coverage PCT` (default 2) skips a drape that barely overlaps, rather
  than silently emitting an almost-empty skin. Usually a CRS mismatch.

Other flags: `--tex-size 8192` (texture detail, independent of mesh detail),
`--cmap`, `--clip LO HI`, `--vmin/--vmax` (fix the scale so two skins are
directly comparable), `--nearest`, `--reset`.

## 4. Drape the vectors

```bash
python vec2overlay.py kilcoy_lidar.tif kilcoy_lines.gpkg kilcoy_lines_2.gpkg \
    "MagLine 1 (long).shp" "Line 2 W.shp" -o overlays/
```

Look first — `--list` prints every layer, its geometry type, feature count, CRS
and attribute fields, and writes nothing:

```bash
python vec2overlay.py kilcoy_lidar.tif kilcoy_lines.gpkg --list
```

Each **layer** in a GeoPackage becomes its own toggleable overlay; each
shapefile becomes one. What the script does to them:

- **Reprojects** to the DEM's grid, with the same LOCAL_CS recovery as the
  other scripts (`--vector-crs` to override).
- **Densifies** to `--spacing` (default 2 DEM cells) so lines follow the
  topography instead of chording across gullies.
- **Samples** the LiDAR surface bilinearly for elevation, and lifts by
  `--offset` (default 1.5 m) so lines don't z-fight with the ground.
- **Splits** a line wherever it crosses DEM nodata, rather than interpolating
  across the void.
- **Reads the local origin out of `terrain.glb`** (`asset.extras`), so overlays
  can't drift out of register with the mesh. Pass `--model` if it isn't found
  automatically.
- Auto-detects a label field (`name`, `line`, `id`…), or set `--label-field`.

Colours cycle through a palette chosen to stay readable over both viridis and
grey; `--color` forces one. Re-running appends to `overlays/overlays.json`, so
you can add layers incrementally.

## 5. View

Put `terrain_viewer.html` next to `terrain.glb` and `skins/`, then:

```bash
python -m http.server
# open http://localhost:8000/terrain_viewer.html
```

Opening the file straight off disk works too, but `file://` blocks `fetch()`,
so in that case the viewer shows a drop zone — drag in `terrain.glb`,
`skins.json` and the PNGs together. (You can also drop loose PNGs with no
manifest; they become unlabelled skins.)

Controls: skin dropdown, `1`–`9` to jump, `[` / `]` to cycle, `0` for bare
terrain. Live vertical exaggeration, sun azimuth/elevation (gives you a
hillshade you can spin), wireframe, flat shading, nodata cutout, auto-rotate,
light/dark background. Click the terrain for easting / northing / elevation.

**Overlays** get one checkbox each, so any combination is visible at once, with
**All** / **None** for the two common cases. **Labels** (`L`) draws the
attribute label at each line's midpoint; **X-ray** disables depth testing so
lines show through ridges instead of disappearing behind them.

**Map furniture:**

- **Extent box** (`B`) draws the bounding box and labels all four ground
  corners with their true eastings and northings. The same four corners, plus
  the extent's width × height in metres, are listed in the side panel — so you
  get them whether you want them on the model or as text to copy.
- **North arrow**, bottom right, rotates live with the camera. North is −Z in
  the model, matching the axis convention `dem2gltf.py` writes.
- **Scale bar** snaps to a round 1/2/5 × 10ⁿ distance measured at the orbit
  target. It reads horizontal metres, so it stays correct when you change the
  vertical exaggeration.

Labels are 10.5 px monospace with tabular figures on a translucent dark pill —
small, but legible over both light and dark backgrounds and over any skin.

## Why skins live outside the glTF

One mesh, N images. Swapping is `material.map = tex` — the geometry never
reloads, the camera never resets, and adding a tenth drape next month doesn't
mean re-exporting a multi-million-vertex mesh. The alternatives, for the record:

| | geometry copies | new skin later | portable to other viewers |
|---|---|---|---|
| **skins outside the glTF** (this) | 1 | drop in a PNG | needs the viewer |
| all skins as extra materials in one glTF | 1 | re-export mesh | needs viewer code |
| `KHR_materials_variants` | 1 | re-export mesh | yes — Babylon, `<model-viewer>` |
| separate glTF per skin | N | export another | yes, but N× the bytes |

If you later want the model to open in a viewer you don't control, that's the
case for `KHR_materials_variants` — happy to add that export path.

## Folding into petroGLyph

The mesh is ordinary glTF, so `C_gltf_2_htmlPackage/pack.js` will inline it as
is, minus the skin switcher. Two routes to a single self-contained HTML:

1. Add a "Skins" control to `src/app.js` that reads a base64 skin table
   injected by `pack.js`, and rebuild the bundle. Keeps everything in your
   pipeline and gives every future model the feature.
2. Inline the GLB and PNGs into `terrain_viewer.html` as `data:` URIs for a
   standalone file.

Note `pack.js` currently reads the model as text for embedded `.gltf`. For
`.glb` it needs the binary-buffer tweak your README already mentions — or run
`dem2gltf.py` with an embedded-`.gltf` output path, which I can add.

## Caveats

- I was unable to open your two TIFs while writing this — the sandbox that
  would have let me inspect them was down. So the grid sizes, overlap and value
  ranges are unverified. **Run the two `--check` / `--info` commands in step 1
  first**; they exist precisely for that.
- Full native resolution on a large LiDAR tile can be tens of millions of
  vertices. `dem2gltf.py` warns past ~6 M and suggests a stride.
- Normals are baked at `--z-exag`, but the viewer's live exaggeration slider
  scales the node, and three.js corrects normals for non-uniform scale, so
  lighting stays right either way.
