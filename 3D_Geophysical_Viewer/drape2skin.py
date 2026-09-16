#!/usr/bin/env python3
"""
drape2skin.py -- drape GeoTIFF(s) -> PNG "skins" aligned to a DEM's extent.

Every skin is warped onto the DEM's exact CRS + extent + aspect, so all skins
share one UV layout and can be swapped on the terrain mesh at runtime with no
geometry change. Texture resolution is independent of mesh resolution.

BROKEN CRS TAGS ARE HANDLED. Plenty of LiDAR products ship with the projection
written as a LOCAL_CS / EngineeringCRS -- labelled e.g. "GDA2020 / MGA zone 56"
but with an unknown engineering datum. PROJ cannot build a transform between a
real projected CRS and an engineering one, so reprojection fails outright. This
script detects that, recovers the intended EPSG code from the CRS name, and
tells you what it did. Override with --dem-crs / --drape-crs at any time.

PARTIAL OVERLAP IS EXPECTED. Drapes rarely cover the whole LiDAR footprint, and
rewarping between grids leaves hairline nodata fringes at the edges. So:
  * coverage is measured against the *valid* part of the DEM, not its bbox;
  * uncovered ground is neutral grey with alpha=0, so the viewer can show it as
    grey terrain or punch it out, your choice, without a rebuild;
  * --fill N (default 2 px) closes resampling seams by nearest-neighbour
    dilation, clamped to the DEM footprint, and reports how much it invented;
  * --min-coverage refuses to silently emit a near-empty skin.

  python drape2skin.py kilcoy_lidar.tif U_GDA2020_resampled.tif --check
      Report CRS/extent/resolution/overlap for each input and build nothing.

  python drape2skin.py kilcoy_lidar.tif U_GDA2020_resampled.tif -o skins/
  python drape2skin.py dem.tif u.tif th.tif k.tif -o skins/ --tex-size 8192
  python drape2skin.py dem.tif u.tif -o skins/ --cmap magma --clip 1 99
  python drape2skin.py dem.tif geology.tif -o skins/ --categorical
  python drape2skin.py dem.tif u.tif -o skins/ --dem-crs EPSG:7856

Writes/updates skins/skins.json, the manifest the viewer reads. Re-running with
a new raster appends to it, so drapes can be added one at a time.

Requires: numpy, rasterio, pillow  (matplotlib optional, for more colormaps)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np

try:
    import rasterio
    from rasterio.crs import CRS
    from rasterio.warp import Resampling, reproject, transform_bounds
except ImportError:  # pragma: no cover
    sys.exit("This script needs rasterio:  pip install rasterio")

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    sys.exit("This script needs pillow:  pip install pillow")


NEUTRAL = np.array([140, 140, 140], dtype="uint8")


# --------------------------------------------------------------------------
# CRS repair
# --------------------------------------------------------------------------

def _guess_epsg_from_name(wkt: str):
    """Recover an EPSG code from a LOCAL_CS whose *name* still says what it is."""
    m = re.search(r"zone\s*[_ ]?(\d{1,2})", wkt, re.I)
    if not m:
        return None, None
    z = int(m.group(1))
    if not 1 <= z <= 60:
        return None, None
    if re.search(r"GDA[\s_]*2020", wkt, re.I) or re.search(r"\b2020\b", wkt):
        return 7800 + z, "GDA2020 / MGA"          # 7846..7859
    if re.search(r"GDA[\s_]*(19)?94", wkt, re.I):
        return 28300 + z, "GDA94 / MGA"           # 28346..28356
    if re.search(r"WGS[\s_]*84", wkt, re.I):
        south = re.search(r"south", wkt, re.I)
        return (32700 if south else 32600) + z, "WGS84 / UTM"
    return None, None


def resolve_crs(crs, override, label: str, fallback=None):
    """Return (usable CRS, note). Raises SystemExit with advice if impossible."""
    if override:
        try:
            return CRS.from_user_input(override), f"forced to {override}"
        except Exception as e:
            sys.exit(f"--{label}-crs '{override}' is not a valid CRS: {e}")

    if crs is None:
        if fallback is not None:
            return fallback, f"untagged, assuming {fallback.to_string()}"
        sys.exit(f"{label} has no CRS at all. Pass --{label}-crs EPSG:7856 "
                 f"(or whatever it really is).")

    # A genuine projected or geographic CRS is fine even without an EPSG code.
    try:
        if crs.is_projected or crs.is_geographic:
            return crs, None
    except Exception:
        pass

    # Engineering / LOCAL_CS: PROJ will refuse to transform to or from it.
    wkt = ""
    try:
        wkt = crs.to_wkt()
    except Exception:
        wkt = str(crs)
    epsg, family = _guess_epsg_from_name(wkt)
    if epsg:
        try:
            fixed = CRS.from_epsg(epsg)
            return fixed, (f"LOCAL_CS/engineering tag recovered as EPSG:{epsg} "
                           f"({family} zone {epsg % 100})")
        except Exception:
            pass
    if fallback is not None:
        return fallback, (f"LOCAL_CS/engineering tag unrecognised, assuming "
                          f"{fallback.to_string()}")
    sys.exit(
        f"\n{label} carries a LOCAL_CS / EngineeringCRS that PROJ cannot "
        f"reproject, and its name does not say which grid it is.\n"
        f"Tell the script directly:\n"
        f"    --{label}-crs EPSG:7856\n"
        f"or fix the file itself, permanently:\n"
        f"    gdal_edit.py -a_srs EPSG:7856 <file.tif>\n"
    )


# --------------------------------------------------------------------------
# colormaps
# --------------------------------------------------------------------------

_FALLBACK = {
    "viridis": ["#440154", "#3b528b", "#21918c", "#5ec962", "#fde725"],
    "magma":   ["#000004", "#51127c", "#b73779", "#fc8961", "#fcfdbf"],
    "inferno": ["#000004", "#57106e", "#bc3754", "#f98e09", "#fcffa4"],
    "plasma":  ["#0d0887", "#7e03a8", "#cc4778", "#f89540", "#f0f921"],
    "cividis": ["#00224e", "#35456c", "#666970", "#9d9268", "#e1cc55"],
    "terrain": ["#333399", "#00b2b2", "#99e699", "#e6cc80", "#804d3b", "#ffffff"],
    "gray":    ["#000000", "#ffffff"],
    "turbo":   ["#30123b", "#4675ed", "#1bd0d5", "#a4fc3c",
                "#fbb938", "#e5460a", "#7a0403"],
    "rdylbu":  ["#a50026", "#f46d43", "#fee090", "#abd9e9", "#313695"],
}

_CATEGORICAL = [
    "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f", "#edc948",
    "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac", "#86bcb6", "#d37295",
]


def _hex_to_rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def lut256(name: str) -> np.ndarray:
    try:
        import matplotlib
        cmap = matplotlib.colormaps[name]
        return (np.asarray(cmap(np.linspace(0, 1, 256)))[:, :3] * 255).astype("uint8")
    except Exception:
        pass
    anchors = _FALLBACK.get(name.lower(), _FALLBACK["viridis"])
    stops = np.linspace(0, 255, len(anchors))
    rgb = np.array([_hex_to_rgb(c) for c in anchors], dtype="float64")
    x = np.arange(256)
    return np.stack([np.interp(x, stops, rgb[:, i]) for i in range(3)],
                    axis=-1).astype("uint8")


def ramp_hex(name: str, n: int = 9):
    """A few stops from the colormap, for the viewer's legend gradient."""
    lut = lut256(name)
    return ["#%02x%02x%02x" % tuple(lut[i])
            for i in np.linspace(0, 255, n).astype(int)]


def lut_string(name: str) -> str:
    """All 256 entries as one hex string, so the viewer can invert the ramp
    exactly -- including matplotlib colormaps it has never heard of."""
    return "".join("%02x%02x%02x" % tuple(c) for c in lut256(name))


def encode_values(band, valid, out_path):
    """Write an exact value grid as a 24-bit fixed-point RGB PNG.

    Canvas2D always hands JavaScript 8-bit channels, so a 16-bit PNG would be
    silently truncated on read. Packing the value across R/G/B instead survives
    the round trip intact: ~1/16.7M of the range, i.e. exact for any practical
    purpose. Alpha 0 marks nodata. Crucially this stores the TRUE min/max, not
    the display stretch, so values in the clipped tails still read back.
    """
    vals = band[valid]
    vmin, vmax = float(np.nanmin(vals)), float(np.nanmax(vals))
    span = (vmax - vmin) if vmax > vmin else 1.0
    q = np.nan_to_num(np.clip((band - vmin) / span, 0, 1), nan=0.0)
    i = np.rint(q * 16777215.0).astype("uint32")
    rgba = np.dstack([((i >> 16) & 255).astype("uint8"),
                      ((i >> 8) & 255).astype("uint8"),
                      (i & 255).astype("uint8"),
                      valid.astype("uint8") * 255])
    Image.fromarray(rgba, mode="RGBA").save(out_path, optimize=True)
    return vmin, vmax


# --------------------------------------------------------------------------
# reference grid, taken from the DEM
# --------------------------------------------------------------------------


def dem_grid(dem_path: Path, tex_size: int, crs_override):
    with rasterio.open(dem_path) as src:
        w, h = src.width, src.height
        bounds, raw_crs, nodata = src.bounds, src.crs, src.nodata

        scale = min(1.0, tex_size / max(w, h))
        tw = max(1, int(round(w * scale)))
        th = max(1, int(round(h * scale)))

        dem = src.read(1, out_shape=(th, tw),
                       resampling=Resampling.nearest, masked=True)

    crs, note = resolve_crs(raw_crs, crs_override, "dem")
    if note:
        print(f"  DEM CRS  : {note}")

    transform = rasterio.transform.from_bounds(
        bounds.left, bounds.bottom, bounds.right, bounds.top, tw, th)

    d = np.ma.filled(dem.astype("float64"), np.nan)
    if nodata is not None:
        d = np.where(np.isclose(d, float(nodata)), np.nan, d)
    dem_valid = np.isfinite(d) & (np.abs(d) < 1e30)

    return dict(crs=crs, raw_crs=raw_crs, transform=transform, tw=tw, th=th,
                bounds=bounds, dem_valid=dem_valid, native=(w, h),
                cell=((bounds.right - bounds.left) / w,
                      (bounds.top - bounds.bottom) / h))


def warp(src_path: Path, g, resampling, crs_override, assume_aligned=False):
    with rasterio.open(src_path) as src:
        src_crs, note = resolve_crs(
            src.crs, crs_override, "drape",
            fallback=g["crs"] if assume_aligned else None)
        if assume_aligned:
            src_crs = g["crs"]
            note = f"assumed already on the DEM grid ({g['crs'].to_string()})"

        n = src.count
        out = np.full((n, g["th"], g["tw"]), np.nan, dtype="float32")
        for b in range(1, n + 1):
            dst = np.full((g["th"], g["tw"]), np.nan, dtype="float32")
            reproject(
                source=rasterio.band(src, b),
                destination=dst,
                src_transform=src.transform,
                src_crs=src_crs,
                dst_transform=g["transform"],
                dst_crs=g["crs"],
                resampling=resampling,
                src_nodata=src.nodata,
                dst_nodata=np.nan,
            )
            out[b - 1] = dst

        valid = np.isfinite(out[0]) & (np.abs(out[0]) < 1e30)
        if src.nodata is not None:
            valid &= ~np.isclose(out[0], float(src.nodata))
        if n == 4:
            valid &= np.nan_to_num(out[3]) > 0

        info = dict(count=n, crs=src_crs.to_string(), crs_note=note,
                    raw_crs=(src.crs.to_string() if src.crs else "none"),
                    res=(abs(src.transform.a), abs(src.transform.e)),
                    size=(src.width, src.height),
                    native_bounds=tuple(src.bounds),
                    nodata=src.nodata, dtype=src.dtypes[0])
    return out, valid, info, src_crs


# --------------------------------------------------------------------------
# gap closing
# --------------------------------------------------------------------------

_NEIGH = ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1))


def _shift(a: np.ndarray, dy: int, dx: int) -> np.ndarray:
    out = np.zeros_like(a)
    h, w = a.shape[:2]
    out[slice(max(0, dy), h - max(0, -dy)), slice(max(0, dx), w - max(0, -dx))] = \
        a[slice(max(0, -dy), h - max(0, dy)), slice(max(0, -dx), w - max(0, dx))]
    return out


def close_fringes(rgb, valid, iters, limit):
    """Dilate valid colour into holes, but never outside `limit` (the DEM)."""
    if iters <= 0:
        return rgb, valid, 0
    out, cur, invented = rgb.copy(), valid.copy(), 0
    for _ in range(iters):
        holes = (~cur) & limit
        if not holes.any():
            break
        acc = np.zeros(out.shape, dtype="float32")
        cnt = np.zeros(cur.shape, dtype="float32")
        for dy, dx in _NEIGH:
            sv = _shift(cur.astype("float32"), dy, dx)
            acc += _shift(out.astype("float32"), dy, dx) * sv[..., None]
            cnt += sv
        fillable = holes & (cnt > 0)
        if not fillable.any():
            break
        out[fillable] = (acc[fillable] / cnt[fillable][..., None]).astype("uint8")
        cur |= fillable
        invented += int(fillable.sum())
    return out, cur, invented


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def render_continuous(band, valid, cmap, clip, vmin, vmax):
    vals = band[valid]
    lo = vmin if vmin is not None else float(np.percentile(vals, clip[0]))
    hi = vmax if vmax is not None else float(np.percentile(vals, clip[1]))
    if hi <= lo:
        hi = lo + 1e-6
    norm = np.clip(np.nan_to_num((band - lo) / (hi - lo), nan=0.0), 0, 1)
    rgb = lut256(cmap)[(norm * 255).astype("uint8")]
    rgb[~valid] = NEUTRAL
    return rgb, lo, hi


def render_categorical(band, valid):
    vals = np.unique(band[valid])
    if vals.size > 64:
        sys.exit(f"--categorical found {vals.size} distinct values; that is almost "
                 "certainly continuous data. Drop the flag.")
    lut = np.array([_hex_to_rgb(_CATEGORICAL[i % len(_CATEGORICAL)])
                    for i in range(vals.size)], dtype="uint8")
    idx = np.clip(np.searchsorted(vals, np.nan_to_num(band, nan=vals[0])),
                  0, vals.size - 1)
    rgb = lut[idx]
    rgb[~valid] = NEUTRAL
    classes = [{"value": float(v), "color": "#%02x%02x%02x" % tuple(lut[i])}
               for i, v in enumerate(vals)]
    return rgb, classes


def to_uint8_rgb(stack, valid):
    rgb = stack[:3]
    out = np.zeros(rgb.shape, dtype="uint8")
    for i in range(3):
        b = rgb[i]
        f = np.isfinite(b)
        if not f.any():
            continue
        if b[f].max() <= 255 and b[f].min() >= 0:
            out[i] = np.clip(np.nan_to_num(b), 0, 255).astype("uint8")
        else:
            lo, hi = np.percentile(b[f], [2, 98])
            out[i] = (np.clip((b - lo) / max(hi - lo, 1e-9), 0, 1) * 255).astype("uint8")
    out = np.moveaxis(out, 0, -1)
    out[~valid] = NEUTRAL
    return out


# --------------------------------------------------------------------------


def fmt_bounds(b):
    return f"{b[0]:,.1f}, {b[1]:,.1f} -> {b[2]:,.1f}, {b[3]:,.1f}"


def report(dem: Path, drapes, g, args):
    dv = g["dem_valid"]
    print(f"\nDEM  {dem.name}")
    print(f"  crs tag  : {g['raw_crs'].to_string()[:70] if g['raw_crs'] else 'none'}")
    print(f"  crs used : {g['crs'].to_string()}")
    print(f"  size     : {g['native'][0]} x {g['native'][1]} cells "
          f"@ {g['cell'][0]:g} x {g['cell'][1]:g}")
    print(f"  extent   : {fmt_bounds(tuple(g['bounds']))}")
    print(f"  valid    : {dv.mean()*100:.1f}% of its bounding box")
    print(f"  tex grid : {g['tw']} x {g['th']}")

    for d in drapes:
        if not d.exists():
            print(f"\nDRAPE {d.name}\n  !! not found")
            continue
        stack, valid, info, src_crs = warp(
            d, g, Resampling.bilinear, args.drape_crs, args.assume_aligned)
        both = valid & dv
        cov = both.sum() / max(dv.sum(), 1)
        spill = (valid & ~dv).sum() / max(valid.sum(), 1)
        try:
            nb = transform_bounds(src_crs, g["crs"], *info["native_bounds"])
        except Exception:
            nb = info["native_bounds"]
        print(f"\nDRAPE {d.name}")
        print(f"  crs used : {info['crs']}"
              + (f"   [{info['crs_note']}]" if info["crs_note"] else ""))
        print(f"  size     : {info['size'][0]} x {info['size'][1]} cells "
              f"@ {info['res'][0]:g} x {info['res'][1]:g}")
        print(f"  bands    : {info['count']} ({info['dtype']}), nodata={info['nodata']}")
        print(f"  extent   : {fmt_bounds(nb)}")
        print(f"  COVERAGE : {cov*100:5.1f}% of the valid DEM")
        print(f"  spill    : {spill*100:5.1f}% of the drape falls outside the DEM")
        if cov < 0.5:
            print("  !! under half the terrain would be grey. Check the CRSs are "
                  "really the same grid and that the datasets overlap.")


# --------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("dem", type=Path, help="the DEM used to build terrain.glb")
    p.add_argument("drapes", type=Path, nargs="+", help="one or more drape GeoTIFFs")
    p.add_argument("-o", "--out", type=Path, default=Path("skins"))
    p.add_argument("--check", action="store_true",
                   help="report CRS/extent/overlap for every input, build nothing")
    p.add_argument("--dem-crs", default=None, metavar="CRS",
                   help="override the DEM's CRS, e.g. EPSG:7856 (use when it is "
                        "tagged LOCAL_CS / engineering)")
    p.add_argument("--drape-crs", default=None, metavar="CRS",
                   help="override every drape's CRS")
    p.add_argument("--assume-aligned", action="store_true",
                   help="assume drapes are already on the DEM's grid; resample "
                        "by position without reprojecting")
    p.add_argument("--tex-size", type=int, default=4096,
                   help="max texture dimension (default 4096; 8192 for detail)")
    p.add_argument("--cmap", default="viridis",
                   help="viridis|magma|inferno|plasma|cividis|turbo|terrain|gray|"
                        "rdylbu, or any matplotlib colormap name")
    p.add_argument("--clip", type=float, nargs=2, default=(2.0, 98.0),
                   metavar=("LO", "HI"), help="percentile stretch (default 2 98)")
    p.add_argument("--vmin", type=float, default=None)
    p.add_argument("--vmax", type=float, default=None)
    p.add_argument("--categorical", action="store_true",
                   help="treat single-band values as discrete classes")
    p.add_argument("--nearest", action="store_true",
                   help="nearest-neighbour warp (use for classified rasters)")
    p.add_argument("--fill", type=int, default=2, metavar="N",
                   help="dilate valid data N px into nodata to close rewarping "
                        "seams (default 2; 0 disables)")
    p.add_argument("--min-coverage", type=float, default=2.0, metavar="PCT",
                   help="skip a drape covering less than this %% of the valid DEM")
    p.add_argument("--values", dest="values", action="store_true", default=True,
                   help="also write an exact value grid (<name>_val.png) so the "
                        "viewer reports real numbers on click (default: on)")
    p.add_argument("--no-values", dest="values", action="store_false",
                   help="skip the value grid; the viewer then inverts the colour "
                        "ramp instead -- quantised to 1/256 of the range, and "
                        "blind to anything outside the percentile clip")
    p.add_argument("--label", default=None, help="legend label, e.g. 'U (ppm)'")
    p.add_argument("--reset", action="store_true",
                   help="start a fresh manifest instead of appending")
    args = p.parse_args()

    if not args.dem.exists():
        sys.exit(f"DEM not found: {args.dem}")

    g = dem_grid(args.dem, args.tex_size, args.dem_crs)

    if args.check:
        report(args.dem, args.drapes, g, args)
        return

    args.out.mkdir(parents=True, exist_ok=True)
    manifest_path = args.out / "skins.json"

    manifest = {
        "crs": g["crs"].to_string(),
        "epsg": g["crs"].to_epsg(),
        "bounds": [g["bounds"].left, g["bounds"].bottom,
                   g["bounds"].right, g["bounds"].top],
        "texture_size": [g["tw"], g["th"]],
        "skins": [],
    }
    if manifest_path.exists() and not args.reset:
        try:
            manifest = json.loads(manifest_path.read_text())
            manifest.setdefault("skins", [])
        except Exception:
            pass

    dv = g["dem_valid"]
    print(f"  reference grid : {g['tw']} x {g['th']}  ({g['crs'].to_string()})")
    print(f"  valid DEM      : {dv.sum():,} px\n")

    resampling = (Resampling.nearest if (args.nearest or args.categorical)
                  else Resampling.bilinear)

    for drape in args.drapes:
        if not drape.exists():
            print(f"  !! {drape} not found, skipping", file=sys.stderr)
            continue

        stack, valid, info, _ = warp(
            drape, g, resampling, args.drape_crs, args.assume_aligned)
        if info["crs_note"]:
            print(f"  {drape.name}: {info['crs_note']}")

        cov_raw = (valid & dv).sum() / max(dv.sum(), 1)
        name = drape.stem

        if not valid.any():
            print(f"  !! {drape.name}: no valid pixels, skipped", file=sys.stderr)
            continue
        if cov_raw * 100 < args.min_coverage:
            print(f"  !! {drape.name}: only {cov_raw*100:.2f}% overlap with the "
                  f"valid DEM -- skipped. Run --check, or lower --min-coverage.",
                  file=sys.stderr)
            continue

        entry = {"id": name, "name": name.replace("_", " "), "source": drape.name}

        if info["count"] >= 3:
            rgb = to_uint8_rgb(stack, valid)
            entry["legend"] = {"type": "rgb"}
        elif args.categorical:
            rgb, classes = render_categorical(stack[0], valid)
            entry["legend"] = {"type": "categorical", "classes": classes,
                               "label": args.label or name}
        else:
            rgb, lo, hi = render_continuous(
                stack[0], valid, args.cmap, args.clip, args.vmin, args.vmax)
            entry["legend"] = {"type": "continuous", "cmap": args.cmap,
                               "vmin": lo, "vmax": hi, "label": args.label or name,
                               "ramp": ramp_hex(args.cmap),
                               "lut": lut_string(args.cmap)}

        # exact value grid for click inspection (single-band rasters only)
        if args.values and info["count"] == 1:
            vpng = args.out / f"{name}_val.png"
            tmin, tmax = encode_values(stack[0], valid, vpng)
            entry["values"] = {"file": vpng.name, "vmin": tmin, "vmax": tmax,
                               "label": args.label or name}

        rgb, valid_f, invented = close_fringes(rgb, valid, args.fill, dv)
        cov = (valid_f & dv).sum() / max(dv.sum(), 1)

        rgba = np.dstack([rgb, (valid_f.astype("uint8") * 255)])
        png = args.out / f"{name}.png"
        Image.fromarray(rgba, mode="RGBA").save(png, optimize=True)

        entry["file"] = png.name
        entry["coverage"] = round(float(cov), 4)
        entry["filled_px"] = invented

        manifest["skins"] = [s for s in manifest["skins"] if s.get("id") != name]
        manifest["skins"].append(entry)

        extra = ""
        if entry["legend"]["type"] == "continuous":
            extra = (f"  range {entry['legend']['vmin']:.4g} .. "
                     f"{entry['legend']['vmax']:.4g}")
        print(f"  {png.name:<34} {png.stat().st_size/1024:8.0f} KB  "
              f"cover {cov*100:5.1f}%{extra}")
        if "values" in entry:
            v = entry["values"]
            print(f"     {v['file']:<31} {(args.out / v['file']).stat().st_size/1024:8.0f} KB"
                  f"  exact values {v['vmin']:.4g} .. {v['vmax']:.4g}")
        if invented:
            print(f"     (+{invented:,} px filled to close warping seams, "
                  f"{invented/max(dv.sum(),1)*100:.2f}% of terrain)")
        if cov < 0.5:
            print(f"     !! {(1-cov)*100:.0f}% of the terrain has no {name} data "
                  f"and will render grey", file=sys.stderr)

    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"\nwrote {manifest_path}  ({len(manifest['skins'])} skin(s))")


if __name__ == "__main__":
    main()
