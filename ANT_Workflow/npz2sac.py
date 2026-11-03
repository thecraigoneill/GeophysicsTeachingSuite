#!/usr/bin/env python3
"""Convert xcorr .npz to .sac for FTANos."""
import sys, numpy as np
from obspy import Trace, UTCDateTime

npz_file = sys.argv[1]
dist_m = float(sys.argv[2]) if len(sys.argv) > 2 else 0

d = np.load(npz_file)
cc = d['xcorr']
fs = float(d['sample_rate'])
sta = str(d['station_a'])
stb = str(d['station_b'])

# Symmetric component (positive lags only)
mid = len(cc) // 2
causal = cc[mid:]
acausal = cc[mid::-1]
ml = min(len(causal), len(acausal))
sym = ((causal[:ml] + acausal[:ml]) / 2).astype(np.float32)

tr = Trace(data=sym)
tr.stats.sampling_rate = fs
tr.stats.station = sta[:8]
tr.stats.network = 'AS'
tr.stats.channel = 'BHZ'
tr.stats.starttime = UTCDateTime(0)
tr.stats.sac = {'b': 0.0, 'e': (ml-1)/fs, 'dist': dist_m/1000,
                 'kstnm': sta[:8], 'kevnm': stb[:8], 'delta': 1/fs}

out = npz_file.replace('.npz', '.sac')
tr.write(out, format='SAC')
print(f"Saved: {out} ({ml} samples, dist={dist_m}m)")

