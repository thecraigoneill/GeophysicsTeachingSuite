# Terrain Modeller: 3D LiDAR terrain, drape skins, draped vectors, and 2-D potential-field modelling

`terrain_modeller.html` is `terrain_viewer.html` (kept frozen as the pure
viewer) plus in-browser 2-D gravity and magnetic modelling of survey lines
(section 6). Everything in sections 0–5 applies to both; the same `terrain.glb`,
skins and `overlays.json` load in either.

GeoTIFF and GPKG/SHP in, glTF out. One terrain mesh, any number of draped
raster layers you can flip between at runtime, vector lines and points draped
on the surface, and click-to-inspect that returns real values in real
coordinates.

<img width="1495" height="802" alt="Screenshot 2026-09-16 at 8 41 57 AM" src="https://github.com/user-attachments/assets/a60799b0-906c-4e91-aaba-786de5df54ef" />


```
kilcoy_lidar.tif            ──dem2gltf.py────▶  terrain.glb            mesh + UVs
U_GDA2020_resampled.tif  ─┐
<next drape>.tif         ─┼──drape2skin.py──▶  skins/*.png            display skins
<another drape>.tif      ─┘                    skins/*_val.png        exact values
                                               skins/skins.json       manifest
kilcoy_lines.gpkg        ─┐
MagLine 1 (long).shp     ─┼──vec2overlay.py─▶  overlays/overlays.json
Line 2 W.shp             ─┘
                                               terrain_modeller.html  the modeller
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



---

## 1. Build the mesh

```bash
python dem2gltf.py kilcoy_lidar.tif -o terrain.glb --stride 2
```

**Pick a stride deliberately.** The DEM is 3000 × 2000, so full native
resolution is 6 M vertices / 12 M triangles — about 336 MB of GLB, which will
not orbit in a browser. The texture grid is *also* 3000 × 2000, so striding the mesh while keeping the texture full:

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

### Partial overlap

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
shapefile becomes one.

You can also run it once per file into the same `-o` folder: each run adds
its layers to the existing `overlays.json`. Re-running a file replaces that
file's layer (matched by file and layer name) and leaves the others alone.
`--reset` starts a fresh `overlays.json`.

```bash
python vec2overlay.py dem.tif mag_lines.gpkg  -o overlays/ -attribute mag_corrected -cmap jet
python vec2overlay.py dem.tif grav_lines.gpkg -o overlays/ -attribute bouguer
``` What happens to them:

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

### Carrying attributes

`--attribute` is always carried into the viewer; it also sets the colours.

**Point layers carry every attribute by default** (line numbers, station
numbers, times), because they are survey data. Line and polygon layers carry
only what you name. To change this:

```bash
--carry-fields LINES,STATION   # just these (names are case-insensitive)
--carry-fields all             # everything, any geometry
--carry-fields none            # only --attribute
```

A name that isn't in the layer is reported, along with the fields that are.
Carried fields show up in the click banner, and the profile importer uses a
line-number field to split a survey into its lines.

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

Double click on `terrain_modeller.html` (or the frozen `terrain_viewer.html`).


The viewer shows a drop zone — drag in `terrain.glb`, `skins.json`, the PNGs
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


## 6. Model potential-field profiles

The Terrain Modeller adds a 2-D gravity and magnetic modeller (GravityModeller2000's
bodies-and-properties workflow on a pyGIMLi-style triangular mesh). It runs
entirely in the page, with no Python and no server.

### Bring in a line

- **Import line…** (or drop the file on the window) takes `.csv`, `.txt`/`.tsv`,
  or `.geojson`. A dialog guesses the columns: easting, northing, optional
  elevation, and a magnetics (nT) and/or gravity (mGal) column. Coordinates
  must be in the terrain's CRS. If none of them land on the terrain, the dialog
  says so, and it flags anything that looks like lon/lat.
- **From overlay layer…** turns any loaded vec2overlay layer with a numeric
  attribute into a profile line, e.g. `-attribute mag_corrected`.
- **Several lines in one file** become separate profile lines, split by
  their line-ID field. The importer picks that field automatically when it is
  named like one (`LINE`, `LINES`, `line_id`, `traverse`…) or when each of its
  values occupies one unbroken run of rows. Lines are named by their real IDs
  (e.g. `LINES 101`), and you can choose a different column. With no ID field
  at all, the importer falls back to splitting at gaps between stations and
  warns that this is only a guess. Tick the lines you want; lines imported
  together share one colour scale.

The line is drawn on the terrain coloured by its field, with one clickable
node per station. Stations are projected onto their best-fit straight line,
which sets the profile azimuth and the bodies' strike. The perpendicular
scatter is reported, so you can see when a line is too crooked to model in 2-D.

### Model data

**Model data** opens the modeller as a tab over the 3-D view (**◀ 3D view**
goes back, and nothing is lost).

- **Top:** observed stations, the calculated response, and optionally the
  residual on its own scale. *Magnetics* and *Gravity* switch fields. Both are
  always computed, so you can see the response of a field you have no data
  for. An auto base-level shift (or a manual one) absorbs the regional or
  datum offset, and the RMS misfit is shown.
- **Bottom:** the section along the line. Topography is sampled from the
  terrain mesh itself, and the base and side padding can be set.
- **Bodies:** use **+ New body**, then click vertices; double-click, Enter or a
  click on the first vertex closes the body. You can then:
  - drag vertices or whole bodies;
  - double-click an edge to add a vertex;
  - right-click a vertex to delete it;
  - undo with Ctrl/⌘-Z.

  Each body has a density, a susceptibility (SI), optional remanence
  (J, I, D), and strike extents (see below). Parts drawn above the ground are
  clipped automatically.
- **Background (host rock):** density and susceptibility. Bodies act through
  their contrast with these.
- **Strike and 2.5-D:** the strike azimuth is editable. It defaults to square
  to the profile; when oblique, the section is converted to the across-strike
  frame. Each body can have a finite strike extent either side of the line
  (blank = infinite, 2-D). Finite bodies get a closed-form end correction along
  strike for gz and for the full ΔT, including strike-parallel magnetisation.
  The bodies in the 3-D view use the same strike and extents.
- **Earth's field:** F, I, D (set these from IGRF). Optionally, the terrain
  mass above a datum can be included (2-D, so only approximate).
- **Observations and regional:** observation heights come from the data
  elevations or topography plus a sensor height.
- **Zero level:** the model's zero is the all-background model. Bodies act
  only through their contrast with the background, so with no bodies, or
  bodies at background values, the calculation is exactly 0. The regional ties
  the data's own datum to that zero. Choose *none* when the data are already
  anomalies; this is the default unless the data sit on an obvious absolute
  datum, such as raw TMI. Otherwise use a *constant* or *linear trend*, typed
  in or set by **Fit regional to data (once)**. That is a one-off
  least-absolute-deviation fit, so peaks the model doesn't explain yet barely
  move it. It never re-fits while you model.

### Recalculating

Edits to geometry don't trigger a calculation, so zooming, panning and
dragging vertices stay responsive. Changes to properties, background, field,
strike and regional don't need a new mesh, so they apply at once. When the
geometry has changed, the **Recalculate**
button (or the **R** key) turns orange and the calculated curve is dashed as
out of date. Pressing it re-meshes and recomputes once. Ticking **auto** next
to the button recalculates after every edit instead (live while dragging, if
"Live remesh" is on). Saving, exporting and returning to the 3-D view always
bring the model up to date first.

### Save and load state

**Save state** (in the side panel) downloads `terrain_modeller_state.json`,
one self-contained file holding:

- every profile line: its data, settings, bodies, regional, strike and
  modeller view;
- the profile display options;
- the 3-D view: camera, exaggeration, sun, skin and overlay visibility.

It also records the names and extent of the terrain, skins and overlays it was
made with. Those files aren't copied, because they're already preloaded.

**Load state** restores all of it after the terrain has loaded, and warns if
the terrain doesn't match. The same button also takes any number of
`*_model.json` files at once (e.g. select everything in a `model/` folder).
A line of the same name is replaced rather than duplicated. You can also drop
state or model files on the window.

### What's computed

Each edit re-meshes the section as a conforming Delaunay triangulation, in
the spirit of pyGIMLi's Triangle meshes. The mesh is bounded by the
topography, so it follows the ground surface. It honours every body boundary,
with cell size growing with depth, and Ruppert-style refinement gives a
minimum angle of about 26°.

For each cell and station, the response is a closed-form line integral round
the cell boundary: Talwani / Won & Bevis for gz, and the 2-D dipole equivalent
for total-field ΔT. Assembled, these form a kernel like pyGIMLi's
`GravityModelling2D` Jacobian, so `response = K · m`:

- Changing a property is a single matrix–vector product.
- Moving geometry re-meshes and recomputes, typically 50–100 ms for a few
  thousand cells.

The kernels agree with numerical integration and with GravMagSimulator.py's
Talwani code to machine precision.

### Save and see it in 3-D

- **Save model** downloads `<line>_model.json`: the line, settings, bodies (as
  distance/elevation and easting/northing) and the observed/calculated
  response. Drop it back on the Terrain Modeller to restore everything.
- **Response CSV** and **Bodies CSV** give the same results as plain tables.
- Back in 3-D, each model's bodies are extruded along their strike, to their
  own extents. Infinite (2-D) bodies are drawn at ±½ *2-D body display length*. You can orbit and compare them
  with other lines.
- Options:
  - **Terrain opacity** and **X-ray bodies** let you see bodies through the
    ground.
  - **Sections** shows the model curtain.
  - **Traces** stands the observed (blue) and calculated (red) profiles above
    each line.
