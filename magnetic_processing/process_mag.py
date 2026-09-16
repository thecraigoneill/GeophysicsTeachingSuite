#!/usr/bin/env python3
"""
process_mag.py - one-shot G-857 magnetometer processing.

Takes two .stn files (rover + base station, auto-detected), splits the rover
into survey lines, removes the base-station diurnal variation by time
interpolation, writes per-line and combined CSVs, diagnostic PNGs, and a
single GeoPackage in GDA2020 / MGA Zone 56 (EPSG:7856).

Usage
-----
    python process_mag.py kilcoy_1.stn kilcoy_2.stn

    # or be explicit about which is which:
    python process_mag.py --rover kilcoy_1.stn --base kilcoy_2.stn -o results

Requires: pandas numpy matplotlib pyproj geopandas
    pip install pandas numpy matplotlib pyproj geopandas
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

EPSG_OUT = 7856          # GDA2020 / MGA Zone 56
EPSG_GPS = 4326          # WGS84 lat/lon as logged by the GPS


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------
def read_stn(path: Path) -> pd.DataFrame:
    """Read a Geometrics G-857 .stn file (header line, then a '# G857' comment)."""
    df = pd.read_csv(path, comment=None, skip_blank_lines=True)
    # drop the '# G857 v. 1.0' provenance row and any other comment rows
    first = df.columns[0]
    df = df[~df[first].astype(str).str.strip().str.startswith("#")].copy()

    df.columns = [c.strip().lstrip("#").strip() for c in df.columns]

    for col in ("MODE", "LINE", "STATION", "TRUNCATION", "GPSSTATUS"):
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    for col in ("MAGFIELD", "GPSLAT", "GPSLON", "SIGNAL"):
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["DATETIME"] = pd.to_datetime(
        df["DATE"].astype(str).str.strip() + " " + df["TIME"].astype(str).str.strip(),
        format="%m/%d/%y %H:%M:%S",
        errors="coerce",
    )
    df = df.dropna(subset=["DATETIME", "MAGFIELD"]).reset_index(drop=True)
    df["SRC_FILE"] = path.name
    return df.sort_values("DATETIME").reset_index(drop=True)


def gps_extent_m(df: pd.DataFrame) -> float:
    """Rough diagonal extent of the GPS track, in metres."""
    lat, lon = df["GPSLAT"].dropna(), df["GPSLON"].dropna()
    if lat.empty:
        return 0.0
    dy = (lat.max() - lat.min()) * 111_320.0
    dx = (lon.max() - lon.min()) * 111_320.0 * np.cos(np.radians(lat.mean()))
    return float(np.hypot(dx, dy))


def identify(a: pd.DataFrame, b: pd.DataFrame):
    """Base station = the static one (smallest GPS extent); mag range breaks ties."""
    ea, eb = gps_extent_m(a), gps_extent_m(b)
    ra = float(a["MAGFIELD"].max() - a["MAGFIELD"].min())
    rb = float(b["MAGFIELD"].max() - b["MAGFIELD"].min())
    print(f"  {a['SRC_FILE'].iloc[0]:>16}: {len(a):5d} rows | GPS extent {ea:8.1f} m | "
          f"mag range {ra:8.1f} nT | lines {sorted(a['LINE'].dropna().unique().tolist())}")
    print(f"  {b['SRC_FILE'].iloc[0]:>16}: {len(b):5d} rows | GPS extent {eb:8.1f} m | "
          f"mag range {rb:8.1f} nT | lines {sorted(b['LINE'].dropna().unique().tolist())}")

    if abs(ea - eb) > 20:                      # clear spatial separation
        base, rover = (a, b) if ea < eb else (b, a)
    else:                                       # fall back to the user's rule of thumb
        base, rover = (a, b) if ra < rb else (b, a)
    return base, rover


# --------------------------------------------------------------------------
# Correction
# --------------------------------------------------------------------------
def _step_metres(lat, lon):
    """Great-circle-ish distance between consecutive GPS fixes, in metres."""
    lat = np.asarray(lat, dtype=float)
    lon = np.asarray(lon, dtype=float)
    dy = np.diff(lat) * 111_320.0
    dx = np.diff(lon) * 111_320.0 * np.cos(np.radians((lat[:-1] + lat[1:]) / 2))
    return np.concatenate([[0.0], np.hypot(dx, dy)])


def split_lines(rover: pd.DataFrame, gap_min: float, dist_m: float,
                renumber: bool) -> pd.DataFrame:
    """Break a LINE wherever there is a long time gap or a big positional jump.

    The G-857 keeps the operator's LINE label across an overnight break, so a
    single label can cover two physically separate traverses. Segments are then
    renumbered 1..N in chronological order; ORIG_LINE and SEGMENT preserve the
    original labelling.
    """
    rover = rover.sort_values(["LINE", "DATETIME"]).reset_index(drop=True)
    seg_keys = []

    for line_id, ld in rover.groupby("LINE", sort=True):
        dt_min = ld["DATETIME"].diff().dt.total_seconds().to_numpy() / 60.0
        dt_min[0] = 0.0
        step_m = _step_metres(ld["GPSLAT"].to_numpy(), ld["GPSLON"].to_numpy())

        brk = (dt_min > gap_min) | (step_m > dist_m)
        seg = np.cumsum(brk) + 1

        for s in np.unique(seg):
            if s == 1:
                continue
            i = int(np.argmax(seg == s))
            print(f"  split LINE {line_id} before {ld['DATETIME'].iloc[i]:%Y-%m-%d %H:%M:%S} "
                  f"(gap {dt_min[i]:.1f} min, jump {step_m[i]:.0f} m) -> segment {s}")

        seg_keys.append(pd.Series(seg, index=ld.index))

    rover["SEGMENT"] = pd.concat(seg_keys).sort_index()
    rover["ORIG_LINE"] = rover["LINE"]

    if renumber:
        starts = (rover.groupby(["ORIG_LINE", "SEGMENT"])["DATETIME"]
                       .min().sort_values())
        mapping = {k: i + 1 for i, k in enumerate(starts.index)}
        rover["LINE"] = [mapping[(o, s)] for o, s in
                         zip(rover["ORIG_LINE"], rover["SEGMENT"])]
        print("\n  renumbered chronologically:")
        for (o, s), new in mapping.items():
            n = ((rover["ORIG_LINE"] == o) & (rover["SEGMENT"] == s)).sum()
            tag = f"{o}" if s == 1 and (rover["ORIG_LINE"] == o).sum() == n else f"{o} seg {s}"
            print(f"    original {tag:>12}  ->  LINE {new}   (n={n}, "
                  f"starts {starts[(o, s)]:%Y-%m-%d %H:%M})")

    return rover.sort_values(["LINE", "DATETIME"]).reset_index(drop=True)


def apply_base_correction(rover: pd.DataFrame, base: pd.DataFrame,
                          max_gap_min: float) -> pd.DataFrame:
    """Linear time-interpolation of base onto rover timestamps, then subtract."""
    base = base.sort_values("DATETIME")
    bt = base["DATETIME"].astype("int64").to_numpy() / 1e9
    bm = base["MAGFIELD"].to_numpy()
    # collapse duplicate base timestamps
    bt, idx = np.unique(bt, return_index=True)
    bm = bm[idx]

    rt = rover["DATETIME"].astype("int64").to_numpy() / 1e9
    interp = np.interp(rt, bt, bm)

    # distance (minutes) from each rover reading to the nearest base reading
    pos = np.clip(np.searchsorted(bt, rt), 1, len(bt) - 1)
    gap = np.minimum(np.abs(rt - bt[pos - 1]), np.abs(bt[pos] - rt)) / 60.0
    outside = (rt < bt[0]) | (rt > bt[-1])
    gap[outside] = np.inf

    bad = gap > max_gap_min
    interp[bad] = np.nan

    rover = rover.copy()
    rover["BASE_INTERP"] = interp
    rover["BASE_GAP_MIN"] = np.where(np.isfinite(gap), gap, np.nan)
    rover["mag_corrected"] = rover["MAGFIELD"] - rover["BASE_INTERP"]
    rover["BASE_MEAN"] = float(np.nanmean(bm))

    if bad.any():
        print(f"  ! {bad.sum()} rover readings are >{max_gap_min:g} min from any base "
              f"reading -> mag_corrected set to NaN")
    return rover


def add_progressive_distance(rover: pd.DataFrame) -> pd.DataFrame:
    """Cumulative along-line distance, in metres, from the projected E/N.

    Measured in MGA coordinates (not lat/lon) so it is a true ground distance,
    and reset to zero at the start of every line. STEP_M is the point-to-point
    spacing, which is useful for spotting GPS dropouts.
    """
    rover = rover.sort_values(["LINE", "DATETIME"]).reset_index(drop=True)
    steps, dists = [], []

    for _, ld in rover.groupby("LINE", sort=True):
        de = np.diff(ld["EASTING"].to_numpy())
        dn = np.diff(ld["NORTHING"].to_numpy())
        step = np.concatenate([[0.0], np.hypot(de, dn)])
        steps.append(pd.Series(step, index=ld.index))
        # nancumsum so one bad GPS fix doesn't void the rest of the line
        dists.append(pd.Series(np.nancumsum(step), index=ld.index))

    rover["STEP_M"] = pd.concat(steps).sort_index()
    rover["DIST_M"] = pd.concat(dists).sort_index()
    return rover


def to_mga56(df: pd.DataFrame) -> pd.DataFrame:
    from pyproj import Transformer
    tf = Transformer.from_crs(EPSG_GPS, EPSG_OUT, always_xy=True)
    e, n = tf.transform(df["GPSLON"].to_numpy(), df["GPSLAT"].to_numpy())
    df = df.copy()
    df["EASTING"] = e
    df["NORTHING"] = n
    return df


# --------------------------------------------------------------------------
# Plots
# --------------------------------------------------------------------------
def _timefmt(ax):
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b\n%H:%M"))
    ax.grid(True, alpha=0.3)


def plot_line(line_id, ld: pd.DataFrame, base: pd.DataFrame, outdir: Path):
    fig = plt.figure(figsize=(13, 11))
    ax1 = fig.add_subplot(3, 1, 1)
    ax2 = fig.add_subplot(3, 1, 2, sharex=ax1)
    ax3 = fig.add_subplot(3, 1, 3)

    ax1.plot(ld["DATETIME"], ld["MAGFIELD"], "-", color="tab:blue",
             lw=1.4, ms=3, marker="o", label=f"Line {line_id} raw")
    win = base[(base["DATETIME"] >= ld["DATETIME"].min() - pd.Timedelta("15min")) &
               (base["DATETIME"] <= ld["DATETIME"].max() + pd.Timedelta("15min"))]
    if not win.empty:
        axb = ax1.twinx()
        axb.plot(win["DATETIME"], win["MAGFIELD"], "-", color="0.4", lw=2,
                 label="Base station")
        axb.set_ylabel("Base MAGFIELD (nT)", color="0.4")
        axb.tick_params(axis="y", labelcolor="0.4")
    ax1.set_ylabel("Raw MAGFIELD (nT)")
    ax1.set_title(f"Line {line_id} — raw rover vs base station   (n={len(ld)})")
    ax1.legend(loc="upper left")
    _timefmt(ax1)

    ax2.plot(ld["DATETIME"], ld["mag_corrected"], "-", color="tab:green",
             lw=1.6, ms=3, marker="o", label="mag_corrected (rover − base)")
    ax2.set_ylabel("mag_corrected (nT)")
    ax2.set_xlabel("Time")
    ax2.set_title(f"Line {line_id} — base-corrected")
    ax2.legend(loc="upper left")
    _timefmt(ax2)

    # profile against along-line distance — the usual way to read a mag line
    if "DIST_M" in ld:
        ax3.plot(ld["DIST_M"], ld["mag_corrected"], "-", color="tab:red",
                 lw=1.6, ms=3, marker="o", label="mag_corrected")
        ax3.set_xlabel("Progressive distance along line (m)")
        ax3.set_ylabel("mag_corrected (nT)")
        ax3.set_title(f"Line {line_id} — corrected profile vs along-line distance "
                      f"(length {ld['DIST_M'].max():.1f} m)")
        ax3.legend(loc="upper left")
        ax3.grid(True, alpha=0.3)

    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(outdir / f"line_{line_id}_raw_vs_corrected.png", dpi=150)
    plt.close(fig)


def plot_overview(rover: pd.DataFrame, base: pd.DataFrame, outdir: Path):
    lines = sorted(rover["LINE"].dropna().unique())
    cmap = plt.get_cmap("tab20")
    colours = {l: cmap(i % 20) for i, l in enumerate(lines)}

    for col, fname, title in (
        ("MAGFIELD", "overview_raw_all_lines.png", "Raw MAGFIELD — base station and all survey lines"),
        ("mag_corrected", "overview_corrected_all_lines.png", "Base-corrected MAGFIELD — all survey lines"),
    ):
        fig, ax = plt.subplots(figsize=(15, 8))
        if col == "MAGFIELD":
            ax.plot(base["DATETIME"], base["MAGFIELD"], color="black", lw=2.5,
                    alpha=0.9, label="Base station")
        for l in lines:
            d = rover[rover["LINE"] == l]
            ax.plot(d["DATETIME"], d[col], lw=1.6, alpha=0.9,
                    color=colours[l], label=f"Line {l}")
        ax.set_xlabel("Time")
        ax.set_ylabel(f"{col} (nT)")
        ax.set_title(title)
        _timefmt(ax)
        ax.legend(loc="best", fontsize=8, ncol=2)
        fig.autofmt_xdate()
        fig.tight_layout()
        fig.savefig(outdir / fname, dpi=150)
        plt.close(fig)


def plot_map(rover: pd.DataFrame, outdir: Path):
    d = rover.dropna(subset=["EASTING", "NORTHING", "mag_corrected"])
    if d.empty:
        return
    fig, ax = plt.subplots(figsize=(12, 10))
    for l in sorted(d["LINE"].dropna().unique()):
        ld = d[d["LINE"] == l]
        ax.plot(ld["EASTING"], ld["NORTHING"], "-", color="0.7", lw=1.0, zorder=1)
        ax.annotate(f"L{l}", (ld["EASTING"].iloc[0], ld["NORTHING"].iloc[0]),
                    fontsize=8, color="0.3",
                    textcoords="offset points", xytext=(4, 4))
    sc = ax.scatter(d["EASTING"], d["NORTHING"], c=d["mag_corrected"],
                    cmap="RdYlBu_r", s=28, edgecolors="k", linewidth=0.3, zorder=5)
    fig.colorbar(sc, ax=ax, label="mag_corrected (nT)")
    ax.set_xlabel("Easting (m, GDA2020 MGA Zone 56)")
    ax.set_ylabel("Northing (m, GDA2020 MGA Zone 56)")
    ax.set_title("Survey map — all lines, coloured by mag_corrected")
    ax.set_aspect("equal", adjustable="datalim")
    ax.grid(True, alpha=0.3, ls="--")
    ax.ticklabel_format(style="plain", useOffset=False)
    fig.tight_layout()
    fig.savefig(outdir / "map_all_lines_corrected.png", dpi=150)
    plt.close(fig)


# --------------------------------------------------------------------------
# GeoPackage
# --------------------------------------------------------------------------
def write_gpkg(rover: pd.DataFrame, path: Path):
    import geopandas as gpd
    from shapely.geometry import LineString, Point

    if path.exists():
        path.unlink()          # GPKG append mode needs a clean start

    d = rover.dropna(subset=["EASTING", "NORTHING"]).copy()
    for c in d.columns:
        if str(d[c].dtype) == "Int64":
            d[c] = d[c].astype("float")
    if "DATETIME" in d:
        d["DATETIME"] = d["DATETIME"].dt.strftime("%Y-%m-%dT%H:%M:%S")

    pts = gpd.GeoDataFrame(
        d, geometry=[Point(x, y) for x, y in zip(d["EASTING"], d["NORTHING"])],
        crs=f"EPSG:{EPSG_OUT}")
    pts.to_file(path, layer="mag_points", driver="GPKG")

    rows = []
    for l, ld in d.groupby("LINE", sort=True):
        ld = ld.sort_values("DATETIME")
        if len(ld) < 2:
            continue
        rows.append({
            "LINE": int(l),
            "ORIG_LINE": int(ld["ORIG_LINE"].iloc[0]) if "ORIG_LINE" in ld else int(l),
            "n_points": len(ld),
            "length_m": float(ld["DIST_M"].max()) if "DIST_M" in ld else None,
            "mean_spacing_m": float(ld["STEP_M"].iloc[1:].mean()) if len(ld) > 1 else None,
            "start_time": str(ld["DATETIME"].min()),
            "end_time": str(ld["DATETIME"].max()),
            "mag_corrected_mean": float(ld["mag_corrected"].mean(skipna=True)),
            "mag_corrected_min": float(ld["mag_corrected"].min(skipna=True)),
            "mag_corrected_max": float(ld["mag_corrected"].max(skipna=True)),
            "geometry": LineString(list(zip(ld["EASTING"], ld["NORTHING"]))),
        })
    if rows:
        lines_gdf = gpd.GeoDataFrame(rows, crs=f"EPSG:{EPSG_OUT}")
        try:
            lines_gdf.to_file(path, layer="mag_lines", driver="GPKG", mode="a")
        except TypeError:          # geopandas < 0.8 has no mode= argument
            lines_gdf.to_file(path, layer="mag_lines", driver="GPKG")


# --------------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="*", help="two .stn files (rover and base, any order)")
    p.add_argument("--rover", help="explicit rover .stn")
    p.add_argument("--base", help="explicit base-station .stn")
    p.add_argument("-o", "--outdir", default="mag_output", help="output directory")
    p.add_argument("--max-gap-min", type=float, default=120.0,
                   help="max minutes between a rover reading and the nearest base "
                        "reading before the correction is treated as invalid "
                        "(default 120)")
    p.add_argument("--min-signal", type=float, default=None,
                   help="drop rover readings with SIGNAL below this (e.g. 1.0)")
    p.add_argument("--split-gap-min", type=float, default=30.0,
                   help="split a LINE at time gaps longer than this (default 30 min)")
    p.add_argument("--split-dist-m", type=float, default=200.0,
                   help="split a LINE at positional jumps larger than this "
                        "(default 200 m)")
    p.add_argument("--no-renumber", action="store_true",
                   help="detect splits but keep the original LINE labels")
    a = p.parse_args(argv)

    if a.rover and a.base:
        base, rover = read_stn(Path(a.base)), read_stn(Path(a.rover))
        print("Files (as specified):")
    elif len(a.files) == 2:
        print("Identifying base station vs rover:")
        base, rover = identify(read_stn(Path(a.files[0])), read_stn(Path(a.files[1])))
    else:
        p.error("give two .stn files, or both --rover and --base")

    print(f"\n  base  -> {base['SRC_FILE'].iloc[0]}")
    print(f"  rover -> {rover['SRC_FILE'].iloc[0]}")

    if a.min_signal is not None and "SIGNAL" in rover:
        n0 = len(rover)
        rover = rover[rover["SIGNAL"] >= a.min_signal].reset_index(drop=True)
        print(f"  dropped {n0 - len(rover)} rover readings with SIGNAL < {a.min_signal}")

    out = Path(a.outdir)
    png, csvd = out / "png", out / "csv"
    for d in (out, png, csvd):
        d.mkdir(parents=True, exist_ok=True)

    print("\nSplitting survey lines...")
    rover = split_lines(rover, a.split_gap_min, a.split_dist_m,
                        renumber=not a.no_renumber)

    print("\nCorrecting...")
    rover = apply_base_correction(rover, base, a.max_gap_min)
    rover = to_mga56(rover)
    rover = add_progressive_distance(rover)

    lead = ["LINE", "ORIG_LINE", "SEGMENT", "DATETIME", "STATION",
            "DIST_M", "STEP_M", "EASTING", "NORTHING", "GPSLAT", "GPSLON",
            "MAGFIELD", "BASE_INTERP", "mag_corrected", "BASE_GAP_MIN"]
    rover = rover[lead + [c for c in rover.columns if c not in lead]]

    print("\nPer-line output:")
    lines = sorted(rover["LINE"].dropna().unique())
    for l in lines:
        ld = rover[rover["LINE"] == l].sort_values("DATETIME")
        ld.to_csv(csvd / f"line_{l}.csv", index=False)
        plot_line(l, ld, base, png)
        print(f"  Line {str(l):>5}  n={len(ld):4d}  "
              f"length {ld['DIST_M'].max():7.1f} m "
              f"(spacing {ld['STEP_M'].iloc[1:].mean():5.1f} m)  "
              f"raw {ld['MAGFIELD'].min():9.1f}..{ld['MAGFIELD'].max():9.1f}  "
              f"corrected {ld['mag_corrected'].min():8.1f}..{ld['mag_corrected'].max():8.1f} nT")

    rover.to_csv(out / "all_lines_corrected.csv", index=False)
    base.to_csv(csvd / "base_station.csv", index=False)

    plot_overview(rover, base, png)
    plot_map(rover, png)

    gpkg = out / "kilcoy_mag_gda2020_mga56.gpkg"
    try:
        write_gpkg(rover, gpkg)
        print(f"\nGeoPackage: {gpkg}  (layers: mag_points, mag_lines; EPSG:{EPSG_OUT})")
    except ImportError as e:
        print(f"\n! geopandas/shapely not available ({e}) - GeoPackage skipped."
              "\n  pip install geopandas  then re-run.")
    except Exception:
        import traceback
        print("\n! GeoPackage write FAILED - full traceback follows:")
        traceback.print_exc()
        print(f"  (the CSVs in {out} are unaffected)")

    print(f"\nDone. {len(lines)} lines, {len(rover)} readings -> {out.resolve()}")
    print(f"  {out/'all_lines_corrected.csv'}")
    print(f"  {csvd}/line_*.csv")
    print(f"  {png}/*.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
