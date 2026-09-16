#!/usr/bin/env python3
"""
dem2gltf.py -- LiDAR / DEM GeoTIFF  ->  binary glTF (.glb) terrain mesh.

Produces an UNTEXTURED terrain mesh with UVs already laid out over the raster
extent, so any number of drape rasters (made with drape2skin.py) can be swapped
onto it at runtime without touching the geometry.

Key behaviours
  * Local origin: projected coordinates (e.g. GDA2020 / MGA56 eastings ~ 450000)
    are shifted to a local origin before being written as float32, otherwise you
    get visible quantisation steps of ~3 cm at those magnitudes. The origin and
    CRS are recorded in asset.extras so a viewer can report true coordinates.
  * Axis convention: glTF is Y-up. Easting -> +X, Elevation -> +Y, Northing -> -Z.
  * NoData: triangles touching a nodata cell are dropped (holes, not spikes).
  * Normals are baked from the elevation gradient (smooth shading).
  * Large grids are split into multiple primitives so no single draw call is
    unreasonable.

Usage
    python dem2gltf.py kilcoy_lidar.tif -o terrain.glb
    python dem2gltf.py kilcoy_lidar.tif -o terrain.glb --stride 2 --z-exag 1.5
    python dem2gltf.py kilcoy_lidar.tif --info      # just report, build nothing

Requires: numpy, rasterio
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


# --------------------------------------------------------------------------
# CRS repair -- many LiDAR products are tagged LOCAL_CS / EngineeringCRS,
# labelled e.g. "GDA2020 / MGA zone 56" but with an unknown engineering datum.
# Nothing here reprojects, but we want the *recorded* CRS in asset.extras to be
# a real code so the viewer's coordinate readout means something.
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


def resolve_crs(crs, override):
    """Return (crs, note). Never fatal -- the mesh is built in native units."""
    if override:
        try:
            return CRS.from_user_input(override), f"forced to {override}"
        except Exception as e:
            sys.exit(f"--crs '{override}' is not a valid CRS: {e}")
    if crs is None:
        return None, "no CRS tag; coordinates recorded without one"
    try:
        if crs.is_projected or crs.is_geographic:
            return crs, None
    except Exception:
        pass
    wkt = crs.to_wkt() if hasattr(crs, "to_wkt") else str(crs)
    epsg = _guess_epsg_from_name(wkt)
    if epsg:
        try:
            return CRS.from_epsg(epsg), (
                f"LOCAL_CS/engineering tag recovered as EPSG:{epsg} "
                f"(pass --crs to override)")
        except Exception:
            pass
    return crs, ("LOCAL_CS/engineering tag kept as-is. Reprojection elsewhere "
                 "will fail; consider  gdal_edit.py -a_srs EPSG:7856 <file.tif>")


# --------------------------------------------------------------------------
# minimal GLB writer
# --------------------------------------------------------------------------

_CTYPE = {
    np.dtype("float32"): 5126,
    np.dtype("uint32"): 5125,
    np.dtype("uint16"): 5123,
}
ARRAY_BUFFER = 34962
ELEMENT_ARRAY_BUFFER = 34963


class GlbBuilder:
    """Accumulates accessors/bufferViews into one binary chunk."""

    def __init__(self) -> None:
        self.bin = bytearray()
        self.buffer_views: list[dict] = []
        self.accessors: list[dict] = []

    def _view(self, arr: np.ndarray, target: int | None) -> int:
        self.bin.extend(b"\x00" * ((-len(self.bin)) % 4))
        offset = len(self.bin)
        data = np.ascontiguousarray(arr).tobytes()
        self.bin.extend(data)
        view = {"buffer": 0, "byteOffset": offset, "byteLength": len(data)}
        if target is not None:
            view["target"] = target
        self.buffer_views.append(view)
        return len(self.buffer_views) - 1

    def accessor(
        self,
        arr: np.ndarray,
        type_: str,
        target: int | None,
        minmax: bool = False,
    ) -> int:
        view = self._view(arr, target)
        count = arr.shape[0] if arr.ndim > 1 else arr.size
        acc = {
            "bufferView": view,
            "componentType": _CTYPE[arr.dtype],
            "count": int(count),
            "type": type_,
        }
        if minmax:
            acc["min"] = [float(v) for v in np.atleast_1d(arr.min(axis=0))]
            acc["max"] = [float(v) for v in np.atleast_1d(arr.max(axis=0))]
        self.accessors.append(acc)
        return len(self.accessors) - 1

    def write(self, gltf: dict, path: Path) -> None:
        gltf["bufferViews"] = self.buffer_views
        gltf["accessors"] = self.accessors
        gltf["buffers"] = [{"byteLength": len(self.bin)}]

        json_bytes = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
        json_bytes += b" " * ((-len(json_bytes)) % 4)
        bin_bytes = bytes(self.bin) + b"\x00" * ((-len(self.bin)) % 4)

        total = 12 + 8 + len(json_bytes) + 8 + len(bin_bytes)
        with open(path, "wb") as fh:
            fh.write(struct.pack("<III", 0x46546C67, 2, total))
            fh.write(struct.pack("<II", len(json_bytes), 0x4E4F534A))
            fh.write(json_bytes)
            fh.write(struct.pack("<II", len(bin_bytes), 0x004E4942))
            fh.write(bin_bytes)


# --------------------------------------------------------------------------
# DEM -> mesh
# --------------------------------------------------------------------------


def load_dem(path: Path, stride: int, crs_override=None):
    with rasterio.open(path) as src:
        if abs(src.transform.b) > 1e-9 or abs(src.transform.d) > 1e-9:
            sys.exit(
                "Raster is rotated/sheared. Reproject to a north-up grid first, e.g.\n"
                "  gdalwarp -t_srs EPSG:7856 -r bilinear in.tif north_up.tif"
            )
        arr = src.read(1, masked=True)
        transform = src.transform
        raw_crs = src.crs
        nodata = src.nodata

    crs, note = resolve_crs(raw_crs, crs_override)
    if note:
        print(f"crs note    : {note}")

    if stride > 1:
        arr = arr[::stride, ::stride]
        transform = transform * rasterio.Affine.scale(stride, stride)

    z = np.ma.filled(arr.astype("float64"), np.nan)
    if nodata is not None:
        z = np.where(np.isclose(z, float(nodata)), np.nan, z)
    # Guard against the "-9999 / -3.4e38 but nodata tag missing" case
    z = np.where(np.abs(z) > 1e30, np.nan, z)
    return z, transform, crs


def build(
    z: np.ndarray,
    transform,
    crs,
    z_exag: float,
    origin_mode: str,
    max_verts: int,
):
    h, w = z.shape
    dx = abs(transform.a)
    dy = abs(transform.e)

    west = transform.c
    north = transform.f
    east = west + w * dx
    south = north - h * dy

    # cell-centre coordinates
    xs = west + (np.arange(w) + 0.5) * dx
    ys = north - (np.arange(h) + 0.5) * dy

    if origin_mode == "corner":
        ox, oy = west, south
    else:  # centre
        ox, oy = (west + east) / 2.0, (south + north) / 2.0

    valid = np.isfinite(z)
    if not valid.any():
        sys.exit("DEM contains no valid elevation values.")

    if h < 2 or w < 2:
        sys.exit(f"DEM is {w} x {h}; need at least 2 x 2 cells to build a surface.")

    zf = (np.where(valid, z, np.nanmean(z)) * z_exag).astype("float32")

    # Vertex normals from the elevation gradient, in (E, Up, N) then remapped.
    dz_de = np.gradient(zf, dx, axis=1)
    dz_dn = -np.gradient(zf, dy, axis=0)  # rows run north -> south
    nrm = np.stack([-dz_de, np.ones_like(zf), dz_dn], axis=-1)
    nrm /= np.linalg.norm(nrm, axis=-1, keepdims=True)
    # (E, Up, N) -> glTF (X, Y, Z) with Z = -N
    nrm[..., 2] *= -1.0
    del dz_de, dz_dn

    # UVs across the full raster extent; v=0 at the north edge (PNG row 0).
    u = ((xs - west) / (east - west)).astype("float32")
    v = ((north - ys) / (north - south)).astype("float32")

    # rows per tile, keeping one row of overlap between tiles
    rows_per_tile = max(2, min(h, max_verts // max(w, 1)))

    prims = []
    glb = GlbBuilder()
    total_v = total_t = 0

    r0 = 0
    while r0 < h - 1:
        r1 = min(h - 1, r0 + rows_per_tile - 1)
        rr = slice(r0, r1 + 1)
        th = r1 - r0 + 1

        zt = zf[rr]
        vt = valid[rr]

        pos = np.empty((th, w, 3), dtype="float32")
        pos[..., 0] = (xs - ox)[None, :]
        pos[..., 1] = zt
        pos[..., 2] = -(ys[rr] - oy)[:, None]

        uv = np.empty((th, w, 2), dtype="float32")
        uv[..., 0] = u[None, :]
        uv[..., 1] = v[rr][:, None]

        nt = nrm[rr].astype("float32")

        cell_ok = vt[:-1, :-1] & vt[:-1, 1:] & vt[1:, :-1] & vt[1:, 1:]
        if not cell_ok.any():
            r0 = r1
            continue

        rows, cols = np.nonzero(cell_ok)
        a = rows * w + cols
        b = a + 1
        d = a + w
        e = d + 1
        # CCW when viewed from +Y (up)
        idx = np.empty((rows.size, 6), dtype="uint32")
        idx[:, 0], idx[:, 1], idx[:, 2] = a, d, b
        idx[:, 3], idx[:, 4], idx[:, 5] = b, d, e
        idx = idx.reshape(-1)

        prims.append(
            {
                "attributes": {
                    "POSITION": glb.accessor(
                        pos.reshape(-1, 3), "VEC3", ARRAY_BUFFER, minmax=True
                    ),
                    "NORMAL": glb.accessor(nt.reshape(-1, 3), "VEC3", ARRAY_BUFFER),
                    "TEXCOORD_0": glb.accessor(
                        uv.reshape(-1, 2), "VEC2", ARRAY_BUFFER
                    ),
                },
                "indices": glb.accessor(idx, "SCALAR", ELEMENT_ARRAY_BUFFER),
                "material": 0,
                "mode": 4,
            }
        )
        total_v += th * w
        total_t += rows.size * 2
        r0 = r1

    if not prims:
        sys.exit("No complete 2x2 cell neighbourhoods survived the nodata mask; "
                 "nothing to triangulate.")

    zmin = float(np.nanmin(z))
    zmax = float(np.nanmax(z))

    gltf = {
        "asset": {
            "version": "2.0",
            "generator": "dem2gltf.py",
            "extras": {
                "units": "m",
                "crs": (crs.to_string() if crs else None),
                "epsg": (crs.to_epsg() if crs else None),
                # add these to a local vertex coord to recover true map coords
                "origin": [float(ox), float(oy)],
                "bounds": [float(west), float(south), float(east), float(north)],
                "cell_size": [float(dx), float(dy)],
                "grid_size": [int(w), int(h)],
                "z_exaggeration": float(z_exag),
                "z_range": [zmin, zmax],
                "axis_mapping": "X=easting-origin, Y=elevation*exag, Z=-(northing-origin)",
            },
        },
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"name": "terrain", "mesh": 0}],
        "meshes": [{"name": "terrain", "primitives": prims}],
        "materials": [
            {
                "name": "skin",
                "doubleSided": True,
                "pbrMetallicRoughness": {
                    "baseColorFactor": [1, 1, 1, 1],
                    "metallicFactor": 0.0,
                    "roughnessFactor": 1.0,
                },
            }
        ],
    }
    stats = dict(
        w=w, h=h, dx=dx, dy=dy, verts=total_v, tris=total_t,
        prims=len(prims), zmin=zmin, zmax=zmax,
        west=west, south=south, east=east, north=north,
        ox=ox, oy=oy, crs=(crs.to_string() if crs else "unknown"),
        valid_frac=float(valid.mean()),
    )
    return glb, gltf, stats


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("dem", type=Path, help="input DEM/LiDAR GeoTIFF")
    p.add_argument("-o", "--out", type=Path, default=Path("terrain.glb"))
    p.add_argument("--stride", type=int, default=1,
                   help="sample every Nth cell (1 = full native resolution)")
    p.add_argument("--z-exag", type=float, default=1.0,
                   help="bake a vertical exaggeration into the mesh "
                        "(the viewer can also do this live; leave at 1.0)")
    p.add_argument("--crs", default=None, metavar="CRS",
                   help="override the DEM's CRS tag, e.g. EPSG:7856 (use when it "
                        "is tagged LOCAL_CS / engineering)")
    p.add_argument("--origin", choices=["corner", "centre"], default="corner",
                   help="local origin: SW corner (default) or extent centre")
    p.add_argument("--max-verts", type=int, default=1_000_000,
                   help="max vertices per primitive before tiling")
    p.add_argument("--info", action="store_true", help="report and exit")
    args = p.parse_args()

    z, transform, crs = load_dem(args.dem, args.stride, args.crs)
    h, w = z.shape
    print(f"raster      : {w} x {h} cells @ {abs(transform.a):g} x {abs(transform.e):g} m")
    print(f"crs         : {crs.to_string() if crs else 'unknown'}")
    est_v = w * h
    print(f"vertices    : ~{est_v:,}   triangles: ~{(w-1)*(h-1)*2:,}")
    if args.info:
        return
    if est_v > 6_000_000:
        print(
            f"\n!! {est_v:,} vertices is a lot for a browser "
            f"(~{est_v*32/1e6:.0f} MB of attributes before indices).\n"
            f"   Consider --stride 2 (~{est_v//4:,} verts) if orbiting stutters.\n",
            file=sys.stderr,
        )

    glb, gltf, st = build(z, transform, crs, args.z_exag, args.origin, args.max_verts)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    glb.write(gltf, args.out)

    size_mb = args.out.stat().st_size / 1e6
    print(f"\nwrote {args.out}  ({size_mb:.1f} MB)")
    print(f"  primitives : {st['prims']}")
    print(f"  vertices   : {st['verts']:,}   triangles: {st['tris']:,}")
    print(f"  valid cells: {st['valid_frac']*100:.1f}%")
    print(f"  elevation  : {st['zmin']:.2f} .. {st['zmax']:.2f} m")
    print(f"  extent     : {st['west']:.1f}, {st['south']:.1f} -> {st['east']:.1f}, {st['north']:.1f}")
    print(f"  local origin: {st['ox']:.1f}, {st['oy']:.1f}  (added back by the viewer)")
    print("\nNext:  python drape2skin.py {} <drape1.tif> [drape2.tif ...] -o skins/".format(args.dem))


if __name__ == "__main__":
    main()
