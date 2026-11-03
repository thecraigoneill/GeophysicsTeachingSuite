#!/usr/bin/env python3
"""
Allseis Ambient Noise Cross-Correlation
=========================================
Cross-correlates hourly .BIN files between two stations, stacks, and
outputs an empirical Green's function for FTAN.

CC methods (following NoisePy / Jiang & Denolle 2020):
    coherency:  conj(A)*B / (|A|*|B|)  — normalised per frequency bin (default)
    deconv:     conj(A)*B / |A|^2      — deconvolution
    raw:        conj(A)*B / sqrt(E_a * E_b)  — energy-normalised

Overlapping windows per Seats et al. (2012) GJI for improved SNR.

Outputs .npz, .sac (symmetric component for FTAN), and .png QC plot.

Usage:
    python3 allseis_xcorr.py ./A0097786 ./A0042914 --dist 5.2 \\
        --bandpass 0.1 20 --whiten --one-bit --max-lag 0.5 -j 8 \\
        --from-date 260602 --to-date 260624

Requirements: numpy, scipy, matplotlib, obspy (for SAC)
"""

import argparse, os, re, sys, time, logging
import numpy as np
from concurrent.futures import ProcessPoolExecutor, as_completed
from scipy.signal import detrend, butter, sosfiltfilt
from scipy.signal.windows import tukey
from scipy.fft import rfft, irfft, rfftfreq, next_fast_len

RECORD_SIZE = 14080
SAMPLE_RATE = 1000
META_THRESHOLD = 500_000

# ─── BIN reader ────────────────────────────────────────────────────────────

def read_bin(filepath):
    with open(filepath, 'rb') as f:
        data = f.read()
    if len(data) < RECORD_SIZE * 2 or all(b == 0 for b in data[:1024]):
        return None, SAMPLE_RATE
    raw = np.frombuffer(data[RECORD_SIZE:], dtype=np.uint8)
    n = len(raw) // 4
    raw = raw[:n * 4].reshape(-1, 4)
    counters = raw[:, 0]
    values = (raw[:, 1].astype(np.int32)
              | (raw[:, 2].astype(np.int32) << 8)
              | (raw[:, 3].astype(np.int32) << 16))
    values[values >= 0x800000] -= 0x1000000
    diffs = np.diff(counters.astype(np.int16))
    normal = (diffs == 1) | (diffs == -255)
    bad = np.zeros(len(counters), dtype=bool)
    for loc in np.where(~normal)[0]:
        bad[max(0, loc):min(len(bad), loc + 2)] = True
    bad |= np.abs(values) > META_THRESHOLD
    return values[~bad].astype(np.float64), SAMPLE_RATE

# ─── Pre-processing ────────────────────────────────────────────────────────

def preprocess(trace, fs, bandpass=None, one_bit=False, whiten=False):
    trace = detrend(trace, type='linear')
    if bandpass:
        fmin, fmax = bandpass
        nyq = fs / 2
        sos = butter(4, [max(fmin/nyq, 0.001), min(fmax/nyq, 0.999)],
                     btype='band', output='sos')
        trace = sosfiltfilt(sos, trace)
    if one_bit:
        trace = np.sign(trace)
    else:
        win = int(fs)
        if win > 1 and len(trace) > win:
            kernel = np.ones(win) / win
            envelope = np.convolve(np.abs(trace), kernel, mode='same')
            envelope[envelope < 1e-10] = 1e-10
            trace = trace / envelope
    if whiten:
        nfft = next_fast_len(len(trace))
        spec = rfft(trace, n=nfft)
        amp = np.abs(spec)
        kernel = np.ones(31) / 31
        amp = np.convolve(amp, kernel, mode='same')
        amp[amp < 1e-10] = 1e-10
        trace = irfft(spec / amp, n=nfft)[:len(trace)]
    return trace

# ─── Cross-correlation methods ─────────────────────────────────────────────

def xcorr_coherency(a, b, max_lag_samples, water_level=1e-5):
    """Coherency: conj(A)*B / (|A|*|B|) — normalised per frequency bin."""
    nfft = next_fast_len(len(a) + len(b) - 1)
    A = rfft(a, n=nfft)
    B = rfft(b, n=nfft)
    amp_A = np.abs(A)
    amp_B = np.abs(B)
    denom = amp_A * amp_B
    denom[denom < water_level * denom.max()] = water_level * denom.max()
    cc_spec = np.conj(A) * B / denom
    cc = irfft(cc_spec, n=nfft)
    return np.concatenate([cc[-max_lag_samples:], cc[:max_lag_samples + 1]])


def xcorr_deconv(a, b, max_lag_samples, water_level=1e-5):
    """Deconvolution: conj(A)*B / |A|^2."""
    nfft = next_fast_len(len(a) + len(b) - 1)
    A = rfft(a, n=nfft)
    B = rfft(b, n=nfft)
    denom = np.abs(A) ** 2
    denom[denom < water_level * denom.max()] = water_level * denom.max()
    cc_spec = np.conj(A) * B / denom
    cc = irfft(cc_spec, n=nfft)
    return np.concatenate([cc[-max_lag_samples:], cc[:max_lag_samples + 1]])


def xcorr_raw(a, b, max_lag_samples):
    """Raw: conj(A)*B normalised by total energy."""
    nfft = next_fast_len(len(a) + len(b) - 1)
    cc = irfft(np.conj(rfft(a, n=nfft)) * rfft(b, n=nfft), n=nfft)
    norm = np.sqrt(np.sum(a**2) * np.sum(b**2))
    if norm > 0: cc /= norm
    return np.concatenate([cc[-max_lag_samples:], cc[:max_lag_samples + 1]])

# ─── Per-hour worker ───────────────────────────────────────────────────────

def process_one_hour(args):
    (file_a, file_b, cc_len_samples, step_samples, max_lag_samples,
     bandpass, one_bit, whiten, pws, cc_method) = args
    fname = os.path.basename(file_a)
    try:
        trace_a, fs = read_bin(file_a)
        trace_b, _  = read_bin(file_b)
        if trace_a is None or trace_b is None: return fname, None, 0

        min_len = min(len(trace_a), len(trace_b))
        trace_a, trace_b = trace_a[:min_len], trace_b[:min_len]
        if min_len < cc_len_samples: return fname, None, 0

        # Sliding overlapping windows (Seats et al. 2012)
        cc_len = 2 * max_lag_samples + 1
        window_xcorrs = []
        start = 0
        while start + cc_len_samples <= min_len:
            end = start + cc_len_samples
            ca = preprocess(trace_a[start:end].copy(), fs, bandpass, one_bit, whiten)
            cb = preprocess(trace_b[start:end].copy(), fs, bandpass, one_bit, whiten)
            ca *= tukey(len(ca), alpha=0.1)
            cb *= tukey(len(cb), alpha=0.1)

            if np.std(ca) < 1e-10 or np.std(cb) < 1e-10:
                start += step_samples
                continue

            if cc_method == 'coherency':
                cc = xcorr_coherency(ca, cb, max_lag_samples)
            elif cc_method == 'deconv':
                cc = xcorr_deconv(ca, cb, max_lag_samples)
            else:
                cc = xcorr_raw(ca, cb, max_lag_samples)

            if len(cc) == cc_len:
                window_xcorrs.append(cc)
            start += step_samples

        if not window_xcorrs: return fname, None, 0
        window_xcorrs = np.array(window_xcorrs)

        if pws and len(window_xcorrs) > 1:
            from scipy.signal import hilbert
            analytic = np.array([np.exp(1j * np.angle(hilbert(cc))) for cc in window_xcorrs])
            hourly = np.mean(window_xcorrs, axis=0) * np.abs(np.mean(analytic, axis=0))
        else:
            hourly = np.mean(window_xcorrs, axis=0)

        return fname, hourly, len(window_xcorrs)
    except Exception as ex:
        logging.warning(f'{fname}: {ex}')
        return fname, None, 0

# ─── File matching ──────────────────────────────────────────────────────────

def find_matching_files(dir_a, dir_b, date_from=None, date_to=None):
    from datetime import datetime
    pat = re.compile(r'^\d{8}\.BIN$', re.I)
    files_a = {f.upper(): os.path.join(dir_a, f) for f in os.listdir(dir_a) if pat.match(f)}
    files_b = {f.upper(): os.path.join(dir_b, f) for f in os.listdir(dir_b) if pat.match(f)}
    pairs = []
    for k in sorted(set(files_a) & set(files_b)):
        if date_from or date_to:
            try:
                s = k.replace('.BIN', '')
                dt = datetime(2000+int(s[:2]), int(s[2:4]), int(s[4:6]), int(s[6:8]))
                if date_from and dt < date_from: continue
                if date_to and dt > date_to: continue
            except ValueError: continue
        pairs.append((files_a[k], files_b[k], k))
    return pairs

# ─── SAC export ─────────────────────────────────────────────────────────────

def write_sac(lags, cc_data, fs, sta_a, sta_b, dist_m, filepath):
    """Write symmetric component to SAC for FTANos."""
    from obspy import Trace, UTCDateTime
    mid = len(cc_data) // 2
    causal = cc_data[mid:]
    acausal = cc_data[mid::-1]
    min_len = min(len(causal), len(acausal))
    symmetric = (causal[:min_len] + acausal[:min_len]) / 2.0

    tr = Trace(data=symmetric.astype(np.float32))
    tr.stats.sampling_rate = fs
    tr.stats.station = sta_a[:8]
    tr.stats.network = 'AS'
    tr.stats.channel = 'BHZ'
    tr.stats.starttime = UTCDateTime(0)
    tr.stats.sac = {
        'b': 0.0, 'e': (min_len - 1) / fs,
        'dist': dist_m / 1000.0,
        'kstnm': sta_a[:8], 'kevnm': sta_b[:8],
        'kcmpnm': 'BHZ', 'knetwk': 'AS',
        'lcalda': False, 'delta': 1.0 / fs,
    }
    tr.write(filepath, format='SAC')
    print(f"  SAC:             {filepath}  (symmetric, {min_len} samples, dist={dist_m}m)")

# ─── Main workflow ──────────────────────────────────────────────────────────

def run_xcorr(dir_a, dir_b, cc_len_min=5, step_min=2.5, max_lag_sec=0.5,
              bandpass=None, one_bit=False, whiten=False, pws=False,
              cc_method='coherency', n_jobs=1,
              output=None, plot_output=None,
              date_from=None, date_to=None, dist_m=None):

    fs = SAMPLE_RATE
    cc_len_samples = int(cc_len_min * 60 * fs)
    step_samples = int(step_min * 60 * fs)
    max_lag_samples = int(max_lag_sec * fs)

    pairs = find_matching_files(dir_a, dir_b, date_from, date_to)
    if not pairs:
        print("No matching files found."); return None

    sta = os.path.basename(os.path.normpath(dir_a))
    stb = os.path.basename(os.path.normpath(dir_b))

    # Default output uses station names
    if output is None:
        output = f"{sta}_x_{stb}.npz"

    n_windows_per_hr = int((60 * fs - cc_len_samples) / step_samples) + 1

    print(f"\nCross-correlation: {sta} × {stb}")
    print(f"  Matching hours:  {len(pairs)}")
    print(f"  CC method:       {cc_method}")
    print(f"  Window:          {cc_len_min} min, step {step_min} min "
          f"(~{n_windows_per_hr} overlapping windows/hr)")
    print(f"  Max lag:         ±{max_lag_sec} s")
    print(f"  Bandpass:        {bandpass or 'none'}")
    print(f"  Norm:            {'1-bit' if one_bit else 'running-abs-mean'}")
    print(f"  Whitening:       {'yes' if whiten else 'no'}")
    print(f"  Stack:           {'phase-weighted' if pws else 'linear'}")
    print(f"  Workers:         {n_jobs}")
    if dist_m: print(f"  Distance:        {dist_m} m")
    print()

    work = [(fa, fb, cc_len_samples, step_samples, max_lag_samples,
             bandpass, one_bit, whiten, pws, cc_method) for fa, fb, _ in pairs]

    all_hourly = []; total_wins = 0; done = 0; t0 = time.time()

    if n_jobs > 1:
        with ProcessPoolExecutor(max_workers=n_jobs) as pool:
            futs = {pool.submit(process_one_hour, w): pairs[i][2] for i, w in enumerate(work)}
            for fut in as_completed(futs):
                name, hourly, n_w = fut.result(); done += 1
                if hourly is not None:
                    all_hourly.append(hourly); total_wins += n_w
                    print(f"  ✓ {name}  {n_w} wins  [{done}/{len(pairs)}]")
                else:
                    print(f"  ✗ {name}  skipped  [{done}/{len(pairs)}]")
    else:
        for i, w in enumerate(work):
            name, hourly, n_w = process_one_hour(w); done += 1
            if hourly is not None:
                all_hourly.append(hourly); total_wins += n_w
                print(f"  ✓ {name}  {n_w} wins  [{done}/{len(pairs)}]")
            else:
                print(f"  ✗ {name}  skipped  [{done}/{len(pairs)}]")

    elapsed = time.time() - t0
    if not all_hourly:
        print("\nNo valid cross-correlations."); return None

    all_hourly = np.array(all_hourly)
    final_cc = np.mean(all_hourly, axis=0)
    lags = np.arange(-max_lag_samples, max_lag_samples + 1) / fs

    print(f"\n  Stacked hours:   {len(all_hourly)}")
    print(f"  Total windows:   {total_wins}")
    print(f"  Time:            {elapsed:.0f}s")

    # Save NPZ
    np.savez_compressed(output,
                        xcorr=final_cc, lags=lags, hourly_stacks=all_hourly,
                        sample_rate=fs, station_a=sta, station_b=stb,
                        n_hours=len(all_hourly), n_windows=total_wins,
                        cc_len_min=cc_len_min, step_min=step_min,
                        max_lag_sec=max_lag_sec, cc_method=cc_method,
                        bandpass=np.array(bandpass or []),
                        one_bit=one_bit, whiten=whiten,
                        dist_m=np.array(dist_m or 0))
    print(f"  NPZ:             {output}")

    # SAC
    sac_path = output.replace('.npz', '.sac')
    try:
        write_sac(lags, final_cc, fs, sta, stb, dist_m or 0, sac_path)
    except Exception as e:
        print(f"  SAC failed: {e} (install obspy)")

    # Plot
    pout = plot_output or output.replace('.npz', '.png')
    plot_xcorr(lags, final_cc, all_hourly, fs, sta, stb, bandpass,
               len(all_hourly), total_wins, pout, dist_m, cc_method)

    return final_cc, lags

# ─── Plot ───────────────────────────────────────────────────────────────────

def plot_xcorr(lags, final_cc, hourly_stacks, fs, sta, stb,
               bandpass, n_hours, n_wins, output, dist_m=None, cc_method='coherency'):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    xlim = (lags[0], lags[-1])

    fig, axes = plt.subplots(3, 1, figsize=(14, 10),
                              gridspec_kw={'height_ratios': [2, 3, 2]})

    # Panel 1: xcorr + symmetric
    ax = axes[0]
    ax.plot(lags, final_cc, lw=0.8, color='#1a1a2e', label='Full xcorr')
    mid = len(final_cc) // 2
    causal = final_cc[mid:]
    acausal = final_cc[mid::-1]
    ml = min(len(causal), len(acausal))
    symmetric = (causal[:ml] + acausal[:ml]) / 2
    t_pos = lags[mid:mid + ml]
    ax.plot(t_pos, symmetric, lw=1.2, color='#e94560', alpha=0.8, label='Symmetric')
    ax.set_xlim(xlim)
    ax.set_ylabel('Amplitude')
    dist_str = f'  dist={dist_m:.1f}m' if dist_m else ''
    ax.set_title(f'{sta} × {stb}  ({n_hours} hrs, {n_wins} wins, {cc_method}){dist_str}',
                 fontsize=12, fontweight='bold')
    ax.axvline(0, color='grey', lw=0.5, alpha=0.5)
    ax.legend(fontsize=9, loc='upper right')
    ax.grid(True, alpha=0.15)

    if dist_m and dist_m > 0:
        for v, lab, col in [(200,'200 m/s','blue'), (500,'500','green'),
                             (1000,'1000','orange'), (2000,'2000','red')]:
            t_arr = dist_m / v
            if t_arr <= xlim[1]:
                ax.axvline(t_arr, color=col, ls='--', alpha=0.3, lw=0.8)
                ax.axvline(-t_arr, color=col, ls='--', alpha=0.3, lw=0.8)
                ax.text(t_arr, ax.get_ylim()[1]*0.9, lab, fontsize=7, color=col, alpha=0.6)

    # Panel 2: waterfall
    ax2 = axes[1]
    if len(hourly_stacks) > 1:
        vmax = np.percentile(np.abs(hourly_stacks), 95)
        ax2.imshow(hourly_stacks, aspect='auto',
                   extent=[lags[0], lags[-1], len(hourly_stacks)-0.5, -0.5],
                   cmap='seismic', vmin=-vmax, vmax=vmax, interpolation='nearest')
    ax2.set_xlim(xlim)
    ax2.set_ylabel('Hour index')
    ax2.set_xlabel('Lag time (s)')
    ax2.set_title('Hourly Cross-Correlations')
    ax2.axvline(0, color='black', lw=0.5, alpha=0.5)

    # Panel 3: spectrum
    ax3 = axes[2]
    nfft = next_fast_len(len(final_cc))
    spec = np.abs(rfft(final_cc, n=nfft))
    freqs = rfftfreq(nfft, d=1.0/fs)
    spec_db = 20*np.log10(spec+1e-15)
    spec_db -= spec_db.max()
    ax3.plot(freqs, spec_db, lw=0.6, color='#e94560')
    ax3.set_xlabel('Frequency (Hz)')
    ax3.set_ylabel('dB')
    ax3.set_title('Spectrum of Stacked Xcorr')
    ax3.set_xlim(0, min(fs/2, 50))
    ax3.set_ylim(-60, 5)
    ax3.grid(True, alpha=0.15)
    if bandpass:
        for f in bandpass: ax3.axvline(f, color='orange', ls='--', alpha=0.5)

    plt.tight_layout()
    plt.savefig(output, dpi=150, bbox_inches='tight')
    print(f"  Plot:            {output}")
    plt.close(fig)

# ─── CLI ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description='Allseis Ambient Noise Cross-Correlation',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s ./A0097786 ./A0042914 --dist 5.2 \\
      --bandpass 0.1 20 --whiten --one-bit --max-lag 0.5 -j 8 \\
      --from-date 260602 --to-date 260624

  %(prog)s ./A0097786 ./A0042914 --cc-method deconv --dist 10 -j 4
        """)
    ap.add_argument('dir_a', help='Station A directory')
    ap.add_argument('dir_b', help='Station B directory')
    ap.add_argument('-o', '--output', default=None,
                    help='Output .npz (default: StaA_x_StaB.npz)')
    ap.add_argument('--plot', default=None)
    ap.add_argument('--dist', type=float, default=None,
                    help='Interstation distance in metres')
    ap.add_argument('--cc-method', choices=['coherency', 'deconv', 'raw'],
                    default='coherency',
                    help='CC method (default: coherency)')
    ap.add_argument('--cc-len', type=float, default=5,
                    help='Window length in minutes (default: 5)')
    ap.add_argument('--step', type=float, default=2.5,
                    help='Window step in minutes (default: 2.5 = 50%% overlap)')
    ap.add_argument('--max-lag', type=float, default=0.5,
                    help='Max lag in seconds (default: 0.5)')
    ap.add_argument('--bandpass', type=float, nargs=2, metavar=('FMIN', 'FMAX'))
    ap.add_argument('--one-bit', action='store_true')
    ap.add_argument('--whiten', action='store_true')
    ap.add_argument('--pws', action='store_true', help='Phase-weighted stack')
    ap.add_argument('-j', '--jobs', type=int, default=1)
    ap.add_argument('--from-date', default=None, help='YYMMDD or YYMMDDHH')
    ap.add_argument('--to-date', default=None, help='YYMMDD or YYMMDDHH')
    args = ap.parse_args()

    for d in [args.dir_a, args.dir_b]:
        if not os.path.isdir(d):
            print(f"Error: {d} not found"); sys.exit(1)

    date_from = date_to = None
    if args.from_date or args.to_date:
        from datetime import datetime
        if args.from_date:
            s = args.from_date
            if len(s)==6: date_from = datetime(2000+int(s[:2]),int(s[2:4]),int(s[4:6]))
            elif len(s)==8: date_from = datetime(2000+int(s[:2]),int(s[2:4]),int(s[4:6]),int(s[6:8]))
        if args.to_date:
            s = args.to_date
            if len(s)==6: date_to = datetime(2000+int(s[:2]),int(s[2:4]),int(s[4:6]),23)
            elif len(s)==8: date_to = datetime(2000+int(s[:2]),int(s[2:4]),int(s[4:6]),int(s[6:8]))

    run_xcorr(args.dir_a, args.dir_b,
              cc_len_min=args.cc_len, step_min=args.step,
              max_lag_sec=args.max_lag,
              bandpass=tuple(args.bandpass) if args.bandpass else None,
              one_bit=args.one_bit, whiten=args.whiten, pws=args.pws,
              cc_method=args.cc_method, n_jobs=args.jobs,
              output=args.output, plot_output=args.plot,
              date_from=date_from, date_to=date_to, dist_m=args.dist)

if __name__ == '__main__':
    main()
