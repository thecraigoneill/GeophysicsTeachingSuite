#!/usr/bin/env python3
"""
vec2overlay.py -- GeoPackage / shapefile vectors -> terrain-draped overlays.

Reads line and point vectors, reprojects them to the DEM's grid, densifies them
so they follow topography instead of cutting through ridges, samples the LiDAR
surface for elevation, and writes a compact JSON the viewer draws as real
antialiased vector geometry -- mitred line ribbons and round dots.

Coordinates come out in the SAME local frame as terrain.glb, read straight from
that file's asset.extras, so overlays cannot drift out of register with the mesh.

COLOUR, three ways
  1. default          -- each layer gets the next colour off a palette
  2. --colour SPEC    -- one blunt colour for everything in this run. Any
                         matplotlib spec: "xkcd:blood red", "tab:orange",
                         "firebrick", "#c2185b", "0.35"
  3. --attribute F --cmap NAME
                      -- colour by an attribute field through a colormap.
                         Numeric fields get a continuous ramp with a percentile
                         stretch; text fields become classes. Lines colour per
                         feature, points per point.

CLICK INSPECTION
  Attribute values travel with the geometry, so clicking a point or line in the
  viewer reports its real value, not a colour. --attribute is carried
  automatically; --carry-fields adds more.

  python vec2overlay.py kilcoy_lidar.tif kilcoy_lines.gpkg --list
      Layers, geometry, counts, CRS, and every attribute field with its type
      and numeric range -- so you know what to pass to --attribute.

  python vec2overlay.py kilcoy_lidar.tif kilcoy_lines.gpkg -o overlays/
  python vec2overlay.py dem.tif "MagLine 1 (long).shp" -o overlays/ \
      --colour "xkcd:blood red" --width 3
  python vec2overlay.py dem.tif mag_points.gpkg -o overlays/ \
      --attribute Mag --cmap magma --carry-fields Alt,Line,Fid

Requires: numpy, rasterio, pyproj, and either fiona or geopandas.
          matplotlib strongly recommended -- it provides the xkcd/tab/CSS
          colour names and the full colormap set.
"""
from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from pathlib import Path

import numpy as np

try:
    import rasterio
    from rasterio.crs import CRS
except ImportError:  # pragma: no cover
    sys.exit("This script needs rasterio:  pip install rasterio")

_READER = None
try:
    import fiona
    _READER = "fiona"
except ImportError:
    try:
        import geopandas as gpd
        _READER = "geopandas"
    except ImportError:
        sys.exit("Need a vector reader:  pip install fiona   (or geopandas)")

try:
    from pyproj import Transformer
except ImportError:  # pragma: no cover
    sys.exit("This script needs pyproj:  pip install pyproj")

try:
    import matplotlib
    from matplotlib.colors import to_hex as _mpl_to_hex
    _HAVE_MPL = True
except ImportError:
    _HAVE_MPL = False


PALETTE = ["#ff3b30", "#00e5ff", "#ffcc00", "#ff2d95", "#7cff00",
           "#ff9500", "#b388ff", "#00ffa3", "#ff6e6e", "#5ac8fa"]

CLASS_PALETTE = ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
                 "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac"]

LABEL_FIELDS = ["name", "label", "line", "line_id", "id", "title", "survey",
                "linename", "line_name", "ident"]

_CSS = {
    "black": "#000000", "white": "#ffffff", "red": "#ff0000", "green": "#008000",
    "blue": "#0000ff", "yellow": "#ffff00", "cyan": "#00ffff", "magenta": "#ff00ff",
    "orange": "#ffa500", "purple": "#800080", "brown": "#a52a2a", "pink": "#ffc0cb",
    "grey": "#808080", "gray": "#808080", "lime": "#00ff00", "navy": "#000080",
    "teal": "#008080", "olive": "#808000", "maroon": "#800000", "gold": "#ffd700",
    "crimson": "#dc143c", "firebrick": "#b22222", "tomato": "#ff6347",
    "salmon": "#fa8072", "coral": "#ff7f50", "turquoise": "#40e0d0",
    "violet": "#ee82ee", "indigo": "#4b0082", "khaki": "#f0e68c",
}

_FALLBACK_CMAP = {
    "viridis": ["#440154", "#3b528b", "#21918c", "#5ec962", "#fde725"],
    "magma":   ["#000004", "#51127c", "#b73779", "#fc8961", "#fcfdbf"],
    "inferno": ["#000004", "#57106e", "#bc3754", "#f98e09", "#fcffa4"],
    "plasma":  ["#0d0887", "#7e03a8", "#cc4778", "#f89540", "#f0f921"],
    "cividis": ["#00224e", "#35456c", "#666970", "#9d9268", "#e1cc55"],
    "turbo":   ["#30123b", "#4675ed", "#1bd0d5", "#a4fc3c",
                "#fbb938", "#e5460a", "#7a0403"],
    "gray":    ["#000000", "#ffffff"],
    "rdylbu":  ["#a50026", "#f46d43", "#fee090", "#abd9e9", "#313695"],
    "coolwarm": ["#3b4cc0", "#8db0fe", "#dddddd", "#f49a7b", "#b40426"],
}


# --------------------------------------------------------------------------
# colour handling
# --------------------------------------------------------------------------

def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def resolve_colour(spec):
    if spec is None:
        return None
    s = str(spec).strip()
    if _HAVE_MPL:
        try:
            return _mpl_to_hex(s)
        except Exception:
            sys.exit(f"'{spec}' is not a colour matplotlib recognises. Try a hex "
                     f"value, a CSS name, 'tab:blue', or an xkcd name such as "
                     f"\"xkcd:blood red\" (see xkcd.com/color/rgb/).")
    if re.fullmatch(r"#?[0-9a-fA-F]{6}", s):
        return "#" + s.lstrip("#").lower()
    key = s.lower().split(":")[-1].strip()
    if key in _CSS:
        return _CSS[key]
    sys.exit(f"Cannot resolve colour '{spec}' without matplotlib installed.\n"
             f"  pip install matplotlib      (gives you xkcd:/tab:/CSS names)\n"
             f"or pass a hex value like '#c2185b'.")


def lut256(name: str) -> np.ndarray:
    if _HAVE_MPL:
        try:
            cm = matplotlib.colormaps[name]
            return (np.asarray(cm(np.linspace(0, 1, 256)))[:, :3] * 255).astype("uint8")
        except Exception:
            sys.exit(f"'{name}' is not a matplotlib colormap. Try magma, viridis, "
                     f"turbo, RdYlBu_r, coolwarm ...")
    anchors = _FALLBACK_CMAP.get(name.lower().replace("_r", ""),
                                 _FALLBACK_CMAP["viridis"])
    if name.lower().endswith("_r"):
        anchors = anchors[::-1]
    stops = np.linspace(0, 255, len(anchors))
    rgb = np.array([_hex_to_rgb(c) for c in anchors], dtype="float64")
    x = np.arange(256)
    return np.stack([np.interp(x, stops, rgb[:, i]) for i in range(3)],
                    axis=-1).astype("uint8")


def ramp_hex(name: str, n: int = 9):
    lut = lut256(name)
    return ["#%02x%02x%02x" % tuple(lut[i])
            for i in np.linspace(0, 255, n).astype(int)]


# --------------------------------------------------------------------------
# CRS repair
# --------------------------------------------------------------------------

def _guess_epsg_from_name(wkt: str):
    m = re.search(r"zone\s*[_ ]?(\d{1,2})", wkt, re.I)
    if not m:
        return None
    z = int(m.group(1))
    if not 1 <= z <= 60:
        return None
    if re.search(r"GDA[\s_]*2020", wkt, re.I) or re.search(r"\b2020\b", wkt):
        return 7800 + z
    if re.search(r"GDA[\s_]*(19)?94", wkt, re.I):
        return 28300 + z
    if re.search(r"WGS[\s_]*84", wkt, re.I):
        return (32700 if re.search(r"south", wkt, re.I) else 32600) + z
    return None


def resolve_crs(crs, override, label, fallback=None):
    if override:
        try:
            return CRS.from_user_input(override), f"forced to {override}"
        except Exception as e:
            sys.exit(f"CRS override '{override}' invalid: {e}")
    if crs is None:
        if fallback is not None:
            return fallback, f"untagged, assuming {fallback.to_string()}"
        sys.exit(f"{label} has no CRS. Pass --vector-crs EPSG:7856.")
    try:
        if crs.is_projected or crs.is_geographic:
            return crs, None
    except Exception:
        pass
    wkt = crs.to_wkt() if hasattr(crs, "to_wkt") else str(crs)
    epsg = _guess_epsg_from_name(wkt)
    if epsg:
        try:
            return CRS.from_epsg(epsg), f"LOCAL_CS recovered as EPSG:{epsg}"
        except Exception:
            pass
    if fallback is not None:
        return fallback, f"LOCAL_CS unrecognised, assuming {fallback.to_string()}"
    sys.exit(f"{label} has a LOCAL_CS/engineering CRS. Pass --vector-crs EPSG:7856 "
             f"or fix it:  gdal_edit.py -a_srs EPSG:7856 <file>")


# --------------------------------------------------------------------------

def read_model_extras(path: Path) -> dict:
    data = path.read_bytes()
    if data[:4] == b"glTF":
        ln, ctype = struct.unpack("<II", data[12:20])
        if ctype != 0x4E4F534A:
            raise ValueError("first GLB chunk is not JSON")
        js = json.loads(data[20:20 + ln].decode("utf-8"))
    else:
        js = json.loads(data.decode("utf-8"))
    extras = js.get("asset", {}).get("extras", {})
    if "origin" not in extras:
        raise ValueError("no asset.extras.origin -- was this built by dem2gltf.py?")
    return extras


class Surface:
    def __init__(self, dem_path: Path, crs_override=None):
        with rasterio.open(dem_path) as src:
            arr = src.read(1, masked=True)
            self.transform, self.inv = src.transform, ~src.transform
            raw_crs, nodata, self.bounds = src.crs, src.nodata, src.bounds
        z = np.ma.filled(arr.astype("float32"), np.nan)
        if nodata is not None:
            z = np.where(np.isclose(z, float(nodata)), np.nan, z)
        self.z = np.where(np.abs(z) > 1e30, np.nan, z)
        self.h, self.w = self.z.shape
        self.crs, self.crs_note = resolve_crs(raw_crs, crs_override, "DEM")

    def sample(self, xs, ys):
        cols, rows = self.inv * (xs, ys)
        cols = np.asarray(cols, dtype="float64") - 0.5
        rows = np.asarray(rows, dtype="float64") - 0.5
        c0 = np.floor(cols).astype("int64")
        r0 = np.floor(rows).astype("int64")
        fc, fr = cols - c0, rows - r0
        out = np.full(cols.shape, np.nan, dtype="float64")
        ok = (c0 >= 0) & (r0 >= 0) & (c0 < self.w - 1) & (r0 < self.h - 1)
        if not ok.any():
            return out
        c, r, a, b = c0[ok], r0[ok], fc[ok], fr[ok]
        top = self.z[r, c] * (1 - a) + self.z[r, c + 1] * a
        bot = self.z[r + 1, c] * (1 - a) + self.z[r + 1, c + 1] * a
        out[ok] = top * (1 - b) + bot * b
        return out


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------

def geom_parts(geom):
    if geom is None:
        return None, []
    t, c = geom["type"], geom["coordinates"]
    if t == "LineString":
        return "line", [c]
    if t == "MultiLineString":
        return "line", list(c)
    if t == "Polygon":
        return "polygon", list(c)
    if t == "MultiPolygon":
        return "polygon", [ring for poly in c for ring in poly]
    if t == "Point":
        return "point", [[c]]
    if t == "MultiPoint":
        return "point", [list(c)]
    return None, []


def densify(xs, ys, spacing):
    if xs.size < 2 or spacing <= 0:
        return xs, ys
    dx, dy = np.diff(xs), np.diff(ys)
    seg = np.hypot(dx, dy)
    n = np.maximum(1, np.ceil(seg / spacing).astype("int64"))
    if n.sum() > 2_000_000:
        return xs, ys
    ox, oy = [], []
    for i in range(xs.size - 1):
        t = np.linspace(0, 1, n[i], endpoint=False)
        ox.append(xs[i] + dx[i] * t)
        oy.append(ys[i] + dy[i] * t)
    ox.append(xs[-1:]); oy.append(ys[-1:])
    return np.concatenate(ox), np.concatenate(oy)


def split_on_nan(x, y, z):
    good = np.isfinite(z)
    if good.all():
        return [(x, y, z)]
    out, i = [], 0
    while i < good.size:
        if not good[i]:
            i += 1
            continue
        j = i
        while j < good.size and good[j]:
            j += 1
        if j - i >= 2:
            out.append((x[i:j], y[i:j], z[i:j]))
        i = j
    return out


# --------------------------------------------------------------------------
# vector reading
# --------------------------------------------------------------------------

def list_layers(path: Path):
    if _READER == "fiona":
        try:
            return fiona.listlayers(str(path))
        except Exception:
            return [None]
    return [None]


def read_layer(path: Path, layer):
    if _READER == "fiona":
        with fiona.open(str(path), layer=layer) as src:
            crs = CRS.from_wkt(src.crs_wkt) if src.crs_wkt else None
            feats = [(f["geometry"], dict(f["properties"])) for f in src]
        return feats, crs
    gdf = gpd.read_file(str(path), layer=layer) if layer else gpd.read_file(str(path))
    crs = CRS.from_wkt(gdf.crs.to_wkt()) if gdf.crs else None
    feats = []
    for _, row in gdf.iterrows():
        g = row.geometry.__geo_interface__ if row.geometry is not None else None
        feats.append((g, {k: v for k, v in row.items() if k != gdf.geometry.name}))
    return feats, crs


def pick_label(props):
    if not props:
        return None
    low = {str(k).lower(): k for k in props}
    for cand in LABEL_FIELDS:
        if cand in low and props[low[cand]] not in (None, ""):
            return str(props[low[cand]])
    for k, v in props.items():
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def sample_props(feats, n=500):
    """Union of attribute keys over the first n features.

    Looking at feature[0] alone is fragile: one feature with a null property
    dict is enough to make every --attribute lookup silently fail.
    """
    keys = {}
    for _, pr in feats[:n]:
        if pr:
            for k in pr:
                keys.setdefault(k, None)
    return keys


def find_field(props, wanted):
    if not props or wanted is None:
        return None
    if wanted in props:
        return wanted
    low = {str(k).lower(): k for k in props}
    return low.get(str(wanted).lower())


def jsonable(v):
    """Attribute values straight out of fiona can be dates, Decimals, numpy."""
    if v is None:
        return None
    if isinstance(v, (str, bool, int, float)):
        return v if not isinstance(v, float) or np.isfinite(v) else None
    try:
        f = float(v)
        return f if np.isfinite(f) else None
    except (TypeError, ValueError):
        return str(v)


# --------------------------------------------------------------------------
# attribute -> colour
# --------------------------------------------------------------------------

class Colouriser:
    def __init__(self, values, cmap, clip, vmin, vmax, label):
        num = []
        for v in values:
            try:
                f = float(v)
                num.append(f if np.isfinite(f) else np.nan)
            except (TypeError, ValueError):
                num.append(np.nan)
        num = np.asarray(num, dtype="float64")
        finite = np.isfinite(num)
        self.numeric = finite.sum() >= max(1, int(0.6 * len(values)))

        if self.numeric:
            vals = num[finite]
            self.lut = lut256(cmap)
            self.lo = vmin if vmin is not None else float(np.percentile(vals, clip[0]))
            self.hi = vmax if vmax is not None else float(np.percentile(vals, clip[1]))
            if self.hi <= self.lo:
                self.hi = self.lo + 1e-9
            self.legend = {"type": "continuous", "cmap": cmap, "ramp": ramp_hex(cmap),
                           "vmin": self.lo, "vmax": self.hi, "label": label,
                           "data_min": float(vals.min()), "data_max": float(vals.max())}
        else:
            uniq = sorted({str(v) for v in values if v not in (None, "")})
            self.classes = {u: CLASS_PALETTE[i % len(CLASS_PALETTE)]
                            for i, u in enumerate(uniq)}
            self.legend = {"type": "categorical", "label": label,
                           "classes": [{"value": k, "color": v}
                                       for k, v in self.classes.items()]}

    def one(self, v) -> str:
        if self.numeric:
            try:
                f = float(v)
            except (TypeError, ValueError):
                return "#888888"
            if not np.isfinite(f):
                return "#888888"
            t = np.clip((f - self.lo) / (self.hi - self.lo), 0, 1)
            return "#%02x%02x%02x" % tuple(self.lut[int(t * 255)])
        return self.classes.get(str(v), "#888888")

    def many(self, vals) -> list:
        out = []
        for v in vals:
            r, g, b = _hex_to_rgb(self.one(v))
            out += [round(r / 255, 3), round(g / 255, 3), round(b / 255, 3)]
        return out


# --------------------------------------------------------------------------

def describe_fields(feats):
    if not feats:
        return []
    keys = []
    for _, pr in feats[:200]:
        for k in (pr or {}):
            if k not in keys:
                keys.append(k)
    out = []
    for k in keys:
        vals = [pr.get(k) for _, pr in feats if pr and pr.get(k) is not None]
        if not vals:
            out.append(f"{k} (empty)")
            continue
        num = []
        for v in vals:
            try:
                num.append(float(v))
            except (TypeError, ValueError):
                pass
        if len(num) >= 0.6 * len(vals) and num:
            out.append(f"{k} [num {min(num):.4g}..{max(num):.4g}]")
        else:
            out.append(f"{k} [text, {len({str(v) for v in vals})} distinct]")
    return out


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("dem", type=Path, help="the DEM terrain.glb was built from")
    p.add_argument("vectors", type=Path, nargs="+",
                   help="GeoPackage(s) and/or .shp file(s)")
    p.add_argument("-o", "--out", type=Path, default=Path("overlays"))
    p.add_argument("--model", type=Path, default=None,
                   help="terrain.glb, to read the local origin from")
    p.add_argument("--list", action="store_true",
                   help="list layers, geometry, counts and attribute fields")

    p.add_argument("-colour", "--colour", "--color", dest="colour", default=None,
                   metavar="SPEC",
                   help='one colour for every layer in this run. Any matplotlib '
                        'spec: "xkcd:blood red", "tab:orange", "firebrick", "#c2185b"')
    p.add_argument("-attribute", "--attribute", "--attr", dest="attribute",
                   default=None, metavar="FIELD",
                   help="colour by this attribute instead (case-insensitive)")
    p.add_argument("-cmap", "--cmap", dest="cmap", default="viridis",
                   help="colormap for --attribute (default viridis)")
    p.add_argument("--clip", type=float, nargs=2, default=(2.0, 98.0),
                   metavar=("LO", "HI"),
                   help="percentile stretch for --attribute (default 2 98)")
    p.add_argument("--vmin", type=float, default=None,
                   help="fix the low end, so layers stay comparable")
    p.add_argument("--vmax", type=float, default=None)

    p.add_argument("--width", type=float, default=2.5, metavar="PX",
                   help="line width in screen pixels (default 2.5)")
    p.add_argument("--point-size", type=float, default=7.0, metavar="PX",
                   help="point diameter in screen pixels (default 7)")
    p.add_argument("--offset", type=float, default=1.5, metavar="M",
                   help="lift above the surface in metres (default 1.5)")
    p.add_argument("--spacing", type=float, default=None, metavar="M",
                   help="densify step in metres (default: 2 DEM cells)")
    p.add_argument("--vector-crs", default=None, metavar="CRS")
    p.add_argument("--dem-crs", default=None, metavar="CRS")
    p.add_argument("--label-field", default=None,
                   help="attribute to label features with (default: auto-detect)")
    p.add_argument("--carry-fields", default=None, metavar="F1,F2,...",
                   help="extra attributes to carry through so they appear in the "
                        "viewer's click banner, e.g. --carry-fields Mag,Alt,Line. "
                        "Use 'all' for every field in the layer.")
    p.add_argument("--reset", action="store_true",
                   help="start a fresh manifest instead of appending")
    args = p.parse_args()

    if not args.dem.exists():
        sys.exit(f"DEM not found: {args.dem}")
    if args.colour and args.attribute:
        sys.exit("--colour and --attribute are alternatives; pick one.")

    carry = [f.strip() for f in args.carry_fields.split(",")] if args.carry_fields else []

    fixed_colour = resolve_colour(args.colour) if args.colour else None
    if fixed_colour:
        print(f"  colour   : {args.colour} -> {fixed_colour}")

    surf = Surface(args.dem, args.dem_crs)
    if surf.crs_note:
        print(f"  DEM CRS  : {surf.crs_note}")

    cell = abs(surf.transform.a)
    spacing = args.spacing if args.spacing else cell * 2

    model = args.model
    if model is None:
        for cand in (args.out.parent / "terrain.glb", Path("terrain.glb")):
            if cand.exists():
                model = cand
                break
    if model and model.exists():
        extras = read_model_extras(model)
        ox, oy = extras["origin"]
        baked = float(extras.get("z_exaggeration", 1.0))
        print(f"  frame    : origin {ox:,.1f}, {oy:,.1f} (from {model.name})")
    else:
        ox, oy, baked = surf.bounds.left, surf.bounds.bottom, 1.0
        print(f"  frame    : origin {ox:,.1f}, {oy:,.1f} (SW corner of the DEM)\n"
              f"             !! terrain.glb not found -- pass --model if you built "
              f"it with --origin centre, or overlays will be offset")

    if args.list:
        for v in args.vectors:
            if not v.exists():
                print(f"\n{v}  !! not found")
                continue
            print(f"\n{v.name}")
            for lyr in list_layers(v):
                feats, crs = read_layer(v, lyr)
                kinds = {}
                for g, _ in feats:
                    k, _pp = geom_parts(g)
                    kinds[k] = kinds.get(k, 0) + 1
                print(f"  layer {lyr or '(default)'}: {len(feats)} features {kinds}")
                print(f"    crs    : {crs.to_string() if crs else 'none'}")
                for f in describe_fields(feats):
                    print(f"    field  : {f}")
        return

    args.out.mkdir(parents=True, exist_ok=True)
    manifest_path = args.out / "overlays.json"
    manifest = {"crs": surf.crs.to_string(), "origin": [float(ox), float(oy)],
                "z_exaggeration": baked, "offset": args.offset, "layers": []}
    if manifest_path.exists() and not args.reset:
        try:
            manifest = json.loads(manifest_path.read_text())
            manifest.setdefault("layers", [])
        except Exception:
            pass

    print(f"  spacing  : {spacing:g} m   offset: {args.offset:g} m\n")
    colour_i = len(manifest["layers"])

    for vec in args.vectors:
        if not vec.exists():
            print(f"  !! {vec} not found, skipping", file=sys.stderr)
            continue

        for lyr in list_layers(vec):
            try:
                feats, raw_crs = read_layer(vec, lyr)
            except Exception as e:
                print(f"  !! {vec.name}[{lyr}]: {e}", file=sys.stderr)
                continue
            if not feats:
                continue

            src_crs, note = resolve_crs(raw_crs, args.vector_crs,
                                        f"{vec.name}[{lyr}]", fallback=surf.crs)
            tf = None
            if src_crs.to_string() != surf.crs.to_string():
                tf = Transformer.from_crs(src_crs, surf.crs, always_xy=True)

            schema = sample_props(feats)

            col, field = None, None
            if args.attribute:
                field = find_field(schema, args.attribute)
                if field is None:
                    avail = ", ".join(sorted(schema)) or "(none)"
                    print(f"  !! {vec.name}[{lyr}]: no field '{args.attribute}'. "
                          f"Available: {avail}", file=sys.stderr)
                else:
                    col = Colouriser([pr.get(field) if pr else None for _, pr in feats],
                                     args.cmap, args.clip, args.vmin, args.vmax, field)

            if [c.lower() for c in carry] == ["all"]:
                carry_res = [k for k in schema if k != field]
            else:
                carry_res = [c for c in (find_field(schema, f) for f in carry) if c]

            lid = f"{vec.stem}" + (f"__{lyr}" if lyr and lyr != vec.stem else "")
            parts_out, part_colors, part_values, part_props, labels = [], [], [], [], []
            pt_xyz, pt_vals, pt_props = [], [], []
            kind_seen, dropped = None, 0

            for geom, props in feats:
                kind, parts = geom_parts(geom)
                if kind is None:
                    continue
                kind_seen = kind_seen or kind
                lab = (str(props.get(args.label_field))
                       if args.label_field and props and args.label_field in props
                       else pick_label(props))
                aval = jsonable(props.get(field)) if (field and props) else None
                extra = ({f: jsonable(props.get(f)) for f in carry_res}
                         if (carry_res and props) else None)

                for part in parts:
                    arr = np.asarray(part, dtype="float64")
                    if arr.ndim != 2 or arr.shape[0] < 1:
                        continue
                    xs, ys = arr[:, 0], arr[:, 1]
                    if tf is not None:
                        xs, ys = tf.transform(xs, ys)
                        xs, ys = np.asarray(xs), np.asarray(ys)
                    if kind != "point":
                        xs, ys = densify(xs, ys, spacing)
                    zs = surf.sample(xs, ys) + args.offset

                    if kind == "point":
                        good = np.isfinite(zs)
                        for gx, gy, gz in zip(xs[good], ys[good], zs[good]):
                            pt_xyz += [round(gx - ox, 3), round(gz * baked, 3),
                                       round(-(gy - oy), 3)]
                            pt_vals.append(aval)
                            pt_props.append(extra)
                        continue

                    for gx, gy, gz in split_on_nan(xs, ys, zs):
                        if gz.size < 2:
                            dropped += 1
                            continue
                        flat = np.empty(gx.size * 3, dtype="float64")
                        flat[0::3] = gx - ox
                        flat[1::3] = gz * baked
                        flat[2::3] = -(gy - oy)
                        parts_out.append([round(float(v), 3) for v in flat])
                        part_colors.append(col.one(aval) if col else None)
                        part_values.append(aval)
                        part_props.append(extra)
                        if lab:
                            m = gx.size // 2
                            labels.append({"text": lab,
                                           "at": [round(float(gx[m] - ox), 2),
                                                  round(float(gz[m] * baked), 2),
                                                  round(float(-(gy[m] - oy)), 2)]})

            is_points = (kind_seen == "point")
            if is_points and pt_xyz:
                parts_out = [pt_xyz]

            if not parts_out:
                print(f"  !! {lid}: nothing fell on the DEM -- check overlap/CRS",
                      file=sys.stderr)
                continue

            base = fixed_colour or PALETTE[colour_i % len(PALETTE)]
            colour_i += 1
            npts = sum(len(p) for p in parts_out) // 3

            entry = {
                "id": lid,
                "name": (lyr or vec.stem).replace("_", " "),
                "source": vec.name,
                "geom": kind_seen or "line",
                "color": base,
                "width": args.width,
                "point_size": args.point_size,
                "parts": parts_out,
                "labels": labels[:300],
                "n_parts": len(parts_out),
                "n_points": npts,
            }
            # values travel with the geometry, so a click returns a number
            if field:
                entry["attribute"] = field
                entry["values"] = pt_vals if is_points else part_values
            if carry_res:
                entry["fields"] = carry_res
                entry["props"] = pt_props if is_points else part_props
            if col:
                entry["legend"] = col.legend
                if is_points:
                    entry["colors"] = [col.many(pt_vals)]
                elif any(c for c in part_colors):
                    entry["part_colors"] = part_colors

            manifest["layers"] = [l for l in manifest["layers"] if l.get("id") != lid]
            manifest["layers"].append(entry)

            how = (f"by {field} ({args.cmap})" if col else base)
            print(f"  {lid:<28} {(kind_seen or '?'):7} {len(parts_out):4} part(s)  "
                  f"{npts:7,} pts  {how}")
            if col and col.numeric:
                print(f"     scale {col.lo:.4g} .. {col.hi:.4g}  "
                      f"(data {col.legend['data_min']:.4g} .. {col.legend['data_max']:.4g})")
            if carry_res:
                print(f"     carrying: {', '.join(carry_res)}")
            if note:
                print(f"     [{note}]")
            if dropped:
                print(f"     ({dropped} part(s) fell outside the DEM, dropped)")

    manifest_path.write_text(json.dumps(manifest, separators=(",", ":")))
    print(f"\nwrote {manifest_path}  "
          f"({len(manifest['layers'])} layer(s), "
          f"{manifest_path.stat().st_size/1024:.0f} KB)")


if __name__ == "__main__":
    main()
