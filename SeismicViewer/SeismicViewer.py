# SeismicViewer.py
import eel
import io
import os
import sys
import csv
import glob
import base64
import subprocess
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# ══════════════════════════════════════════════════════════════════════════════
# FILE DIALOG — runs in a clean subprocess to avoid Tk/Eel focus conflicts
# ══════════════════════════════════════════════════════════════════════════════

HERE            = os.path.dirname(os.path.abspath(__file__))
DIALOG_SCRIPT   = os.path.join(HERE, '_filedialog.py')
PYTHON_EXE      = sys.executable   # same Python that's running us


def _run_dialog(args):
    """Invoke the standalone dialog helper script; return the selected path ('' if cancelled)."""
    try:
        proc = subprocess.run(
            [PYTHON_EXE, DIALOG_SCRIPT] + args,
            capture_output=True, text=True, timeout=300
        )
        return (proc.stdout or '').strip()
    except Exception as e:
        print(f"Dialog subprocess error: {e}")
        return ''


def _open_dialog(title, filetypes_spec):
    return _run_dialog(['open', title, filetypes_spec])


def _save_dialog(title, initial_name, filetypes_spec):
    return _run_dialog(['save', title, initial_name, filetypes_spec])


def _folder_dialog(title):
    return _run_dialog(['folder', title])


# ══════════════════════════════════════════════════════════════════════════════
# APP STATE
# ══════════════════════════════════════════════════════════════════════════════
state = {
    'file_path':      None,
    'z1':             None,
    'w1':             None,
    't1':             None,
    'idc':            None,
    'sampling':       512,
    'skip_header':    33,
    'col_trace':      2,
    'col_trigger':    3,
    'n_scale':        2.0,
    'window_samples': 1024,
    'last_click':     None,
    't_max':          2.0,
    'y_centre':       0.0,
    'y_half':         10.0,
    'active_traces':  [],
}


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1: VIEWER — file loading
# ══════════════════════════════════════════════════════════════════════════════
@eel.expose
def open_dat_file():
    file_path = _open_dialog("Select .dat seismic file", "DAT files=*.dat|All files=*.*")
    if not file_path:
        return {"success": False, "error": "No file selected"}
    return _load_file(file_path)


@eel.expose
def reload_file():
    if not state['file_path']:
        return {"success": False, "error": "No file loaded"}
    return _load_file(state['file_path'])


def _load_file(file_path):
    try:
        z1 = np.genfromtxt(file_path, usecols=state['col_trace'],
                           skip_header=state['skip_header'])
        w1 = np.genfromtxt(file_path, usecols=state['col_trigger'],
                           skip_header=state['skip_header'])
        if z1 is None or len(z1) == 0:
            return {"success": False,
                    "error": "No data read — check column indices / header skip"}
        dt  = 1.0 / state['sampling']
        t1  = np.arange(0, len(z1)) * dt
        idc = np.argwhere(w1 != 0)[:, 0]
        state['file_path'] = file_path
        state['z1']  = z1
        state['w1']  = w1
        state['t1']  = t1
        state['idc'] = idc
        win_s   = state['window_samples'] / state['sampling']
        n_shots = len(idc)
        state['t_max']    = round(min(win_s, 3.0), 3)
        state['y_centre'] = (n_shots - 1) / 2.0
        state['y_half']   = min(10.0, n_shots / 2.0)
        state['active_traces'] = []
        fname = file_path.replace('\\', '/').split('/')[-1]
        print(f"Loaded: {fname}  |  {len(z1)} samples  |  {n_shots} shots")
        return {
            "success":      True,
            "filename":     fname,
            "n_samples":    int(len(z1)),
            "n_shots":      int(n_shots),
            "duration_s":   round(float(t1[-1]), 3),
            "shot_indices": idc.tolist(),
            "win_s":        round(min(win_s, 3.0), 4),
        }
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"success": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1: VIEWER — plot generation
# ══════════════════════════════════════════════════════════════════════════════
ACTIVE_COLORS = [
    '#ff6b6b', '#ffd93d', '#6bcb77', '#4d96ff',
    '#ff922b', '#cc5de8', '#20c997', '#f06595',
]

@eel.expose
def generate_plot(n_scale=None, window_samples=None,
                  t_max=None, y_centre=None, y_half=None,
                  active_traces=None, picks_data=None):
    if state['z1'] is None:
        return {"success": False, "error": "No data loaded"}

    if n_scale        is not None: state['n_scale']        = float(n_scale)
    if window_samples is not None: state['window_samples'] = int(window_samples)
    if t_max          is not None: state['t_max']          = float(t_max)
    if y_centre       is not None: state['y_centre']       = float(y_centre)
    if y_half         is not None: state['y_half']         = float(y_half)
    if active_traces  is not None: state['active_traces']  = [int(x) for x in active_traces]

    z1, t1, idc = state['z1'], state['t1'], state['idc']
    n, win      = state['n_scale'], state['window_samples']
    t_mx, yc, yh = state['t_max'], state['y_centre'], state['y_half']
    act         = set(state['active_traces'])
    has_active  = len(act) > 0

    if len(idc) == 0:
        return {"success": False, "error": "No shot triggers found"}

    fig, ax = plt.subplots(figsize=(10, 6))
    fig.patch.set_facecolor('#1a1d22')
    ax.set_facecolor('#1a1d22')

    active_list  = sorted(act)
    active_color = {idx: ACTIVE_COLORS[k % len(ACTIVE_COLORS)]
                    for k, idx in enumerate(active_list)}

    for i in range(len(idc)):
        n1    = int(idc[i])
        n2    = int(min(idc[i] + win, len(z1)))
        t_seg = t1[n1:n2] - t1[n1]
        z_seg = n * z1[n1:n2] + i
        if i in act:
            ax.plot(t_seg, z_seg, color=active_color[i],
                    linewidth=2.0, alpha=1.0, zorder=3)
        else:
            alpha = 0.35 if has_active else 0.9
            ax.plot(t_seg, z_seg, color='#60a5fa',
                    linewidth=0.8, alpha=alpha, zorder=2)

    if picks_data:
        for pk in picks_data:
            si  = int(pk['shot_idx'])
            pt  = float(pk['time_s'])
            col = active_color.get(si, '#ffd93d')
            ax.plot(pt, si, marker='x', markersize=12, markeredgewidth=2.5,
                    color=col, zorder=5, clip_on=True)
            ax.axvline(x=pt, ymin=0, ymax=1,
                       color=col, linewidth=0.4, alpha=0.25, zorder=4)

    ax.set_xlim(0, t_mx)
    ax.set_ylim(yc - yh, yc + yh)
    ax.set_xlabel("Time (s)", color='#a0a0b0')
    ax.set_ylabel("Shot index", color='#a0a0b0')
    fname = state['file_path'].replace('\\', '/').split('/')[-1]
    ax.set_title(f"{fname}  —  {len(idc)} shots", color='#ffffff', fontsize=11)
    ax.tick_params(colors='#a0a0b0')
    for sp in ax.spines.values():
        sp.set_edgecolor('#444')
    ax.grid(True, alpha=0.2, color='#888')

    if has_active:
        from matplotlib.lines import Line2D
        handles = [Line2D([0], [0], color=active_color[i], linewidth=2,
                          label=f"Shot {i+1}") for i in active_list]
        ax.legend(handles=handles, loc='upper right',
                  facecolor='#1e2128', edgecolor='#444',
                  labelcolor='white', fontsize=8)

    fig.tight_layout()
    fig.canvas.draw()
    bbox      = ax.get_position()
    axes_norm = {"x0": bbox.x0, "y0": bbox.y0,
                 "x1": bbox.x1, "y1": bbox.y1}

    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=130, facecolor=fig.get_facecolor())
    buf.seek(0)
    img_b64 = base64.b64encode(buf.read()).decode('utf-8')
    plt.close(fig)

    return {
        "success":     True,
        "image":       f"data:image/png;base64,{img_b64}",
        "axes_norm":   axes_norm,
        "data_bounds": {"xmin": 0, "xmax": t_mx,
                        "ymin": yc - yh, "ymax": yc + yh},
    }


@eel.expose
def save_plot_png():
    if state['z1'] is None:
        return {"success": False, "error": "No data loaded"}
    file_path = _save_dialog("Save plot as PNG", "plot.png", "PNG files=*.png")
    if not file_path:
        return {"success": False, "error": "Cancelled"}
    result = generate_plot()
    if not result['success']:
        return result
    img_data = base64.b64decode(result['image'].split(',')[1])
    with open(file_path, 'wb') as f:
        f.write(img_data)
    print(f"Saved: {file_path}")
    return {"success": True, "path": file_path}


@eel.expose
def update_params(sampling, skip_header, col_trace, col_trigger):
    state['sampling']    = int(sampling)
    state['skip_header'] = int(skip_header)
    state['col_trace']   = int(col_trace)
    state['col_trigger'] = int(col_trigger)
    return True


@eel.expose
def record_click(x, y):
    state['last_click'] = {'x': x, 'y': y}
    print(f"Click — t: {x:.5f}s  shot: {y:.2f}")
    return {"x": round(x, 6), "y": round(y, 6)}


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1: VIEWER — first-break picks
# ══════════════════════════════════════════════════════════════════════════════
picks_store = {}

@eel.expose
def store_pick(shot_idx, sample_idx, time_s):
    picks_store[int(shot_idx)] = {
        'shot_number':  int(shot_idx) + 1,
        'sample_index': int(sample_idx),
        'time_s':       float(time_s),
    }
    print(f"Pick — Shot {int(shot_idx)+1}: {float(time_s):.5f} s")
    return True


@eel.expose
def delete_pick(shot_idx):
    picks_store.pop(int(shot_idx), None)
    return True


@eel.expose
def clear_picks():
    picks_store.clear()
    return True


@eel.expose
def save_picks_csv(payload):
    file_path = _save_dialog("Save first-break picks", "firstbreaks.csv", "CSV files=*.csv")
    if not file_path:
        return {"success": False, "error": "Cancelled"}
    try:
        with open(file_path, 'w', newline='') as f:
            writer = csv.DictWriter(
                f, fieldnames=['shot_number', 'sample_index', 'time_s',
                               'shot_x', 'shot_y', 'receiver_x', 'receiver_y'])
            writer.writeheader()
            for row in sorted(payload, key=lambda r: r['shot_number']):
                # Ensure geometry fields are present (blank if not provided)
                for k in ('shot_x', 'shot_y', 'receiver_x', 'receiver_y'):
                    row.setdefault(k, '')
                writer.writerow(row)
        print(f"Picks saved: {file_path}  ({len(payload)} rows)")
        return {"success": True, "path": file_path}
    except Exception as e:
        return {"success": False, "error": str(e)}


@eel.expose
def get_shot_info(shot_index):
    idc = state['idc']
    t1  = state['t1']
    if idc is None or shot_index < 0 or shot_index >= len(idc):
        return None
    n1 = int(idc[shot_index])
    n2 = int(min(n1 + state['window_samples'], len(t1)))
    return {
        "shot_index":   shot_index,
        "sample_start": n1,
        "sample_end":   n2,
        "time_start_s": round(float(t1[n1]), 5),
        "time_end_s":   round(float(t1[n2 - 1]), 5),
    }


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2: EDIT DATA
# ══════════════════════════════════════════════════════════════════════════════
@eel.expose
def load_picks_csv_for_edit():
    """Open any picks CSV and return its rows for editing."""
    file_path = _open_dialog("Open picks CSV", "CSV files=*.csv")
    if not file_path:
        return {"success": False, "error": "Cancelled"}
    try:
        rows = []
        with open(file_path, newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append({
                    'shot_number':  row.get('shot_number', ''),
                    'sample_index': row.get('sample_index', ''),
                    'time_s':       row.get('time_s', ''),
                    'shot_x':       row.get('shot_x', ''),
                    'shot_y':       row.get('shot_y', ''),
                    'receiver_x':   row.get('receiver_x', ''),
                    'receiver_y':   row.get('receiver_y', ''),
                })
        return {
            "success":  True,
            "path":     file_path,
            "rows":     rows,
            "filename": file_path.replace('\\', '/').split('/')[-1],
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@eel.expose
def save_edited_csv(orig_path, rows):
    """Save edited rows back to CSV via save dialog."""
    default_name = os.path.basename(orig_path) if orig_path else 'picks_edited.csv'
    save_path = _save_dialog("Save edited CSV", default_name, "CSV files=*.csv")
    if not save_path:
        return {"success": False, "error": "Cancelled"}
    try:
        fieldnames = ['shot_number', 'sample_index', 'time_s',
                      'shot_x', 'shot_y', 'receiver_x', 'receiver_y']
        with open(save_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, '') for k in fieldnames})
        print(f"Saved edited CSV: {save_path}")
        return {"success": True, "path": save_path}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3: INVERSION DATA — PyGIMLi assembler
# ══════════════════════════════════════════════════════════════════════════════
@eel.expose
def glob_picks_files():
    """Choose a folder; return list of all CSV files found recursively."""
    folder = _folder_dialog("Select folder containing picks CSVs")
    if not folder:
        return {"success": False, "error": "Cancelled"}
    pattern = os.path.join(folder, '**', '*.csv')
    files   = sorted(glob.glob(pattern, recursive=True))
    return {"success": True, "folder": folder, "files": files, "count": len(files)}


@eel.expose
def preview_csv_files(file_list):
    """Validate each CSV; count rows with complete shot/receiver/time."""
    summary = []
    for fp in file_list:
        try:
            with open(fp, newline='') as f:
                rows = list(csv.DictReader(f))
            valid = [r for r in rows
                     if r.get('time_s', '').strip()
                     and r.get('shot_x', '').strip()
                     and r.get('receiver_x', '').strip()]
            missing = len(rows) - len(valid)
            summary.append({
                "file":    fp,
                "name":    os.path.basename(fp),
                "total":   len(rows),
                "valid":   len(valid),
                "missing": missing,
                "ok":      missing == 0 and len(valid) > 0,
            })
        except Exception as e:
            summary.append({"file": fp, "name": os.path.basename(fp),
                            "total": 0, "valid": 0, "missing": 0,
                            "ok": False, "error": str(e)})
    return {"success": True, "summary": summary}


@eel.expose
def assemble_gimli(file_list, elev_y=False):
    """Assemble all picks CSVs into PyGIMLi refraction format."""
    all_rows = []
    for fp in file_list:
        try:
            with open(fp, newline='') as f:
                for row in csv.DictReader(f):
                    if not row.get('time_s', '').strip():
                        continue
                    all_rows.append(row)
        except Exception as e:
            print(f"Skipping {fp}: {e}")

    if not all_rows:
        return {"success": False, "error": "No valid rows across selected files"}

    pos_index = {}
    positions = []

    def _register(x_s, y_s):
        try:
            x = float(x_s) if x_s.strip() else None
            y = float(y_s) if (y_s.strip() and elev_y) else 0.0
            if x is None:
                return None
        except ValueError:
            return None
        key = (round(x, 4), round(y, 4))
        if key not in pos_index:
            pos_index[key] = len(positions) + 1
            positions.append(key)
        return pos_index[key]

    measurements = []
    skipped = 0
    for row in all_rows:
        sx = row.get('shot_x',     '').strip()
        sy = row.get('shot_y',     '0').strip() or '0'
        rx = row.get('receiver_x', '').strip()
        ry = row.get('receiver_y', '0').strip() or '0'
        t  = row.get('time_s',     '').strip()

        s_idx = _register(sx, sy)
        g_idx = _register(rx, ry)
        try:
            t_val = float(t)
        except ValueError:
            skipped += 1; continue
        if s_idx is None or g_idx is None:
            skipped += 1; continue
        measurements.append((s_idx, g_idx, t_val))

    if not measurements:
        return {"success": False,
                "error": "No complete rows (need shot_x, receiver_x, time_s)"}

    lines = []
    lines.append(f"{len(positions)}\t# Shot geophone positions")
    lines.append("# x\ty")
    for (x, y) in positions:
        lines.append(f"{x}\t{y:.2f}")
    lines.append(f"{len(measurements)}\t# Measurements")
    lines.append("#s\tg\tt")
    for (s, g, t) in measurements:
        lines.append(f"{s}\t{g}\t{t:.4E}")

    output_text = "\n".join(lines)
    return {
        "success":        True,
        "n_positions":    len(positions),
        "n_measurements": len(measurements),
        "skipped":        skipped,
        "preview":        "\n".join(lines[:30]),
        "full_text":      output_text,
    }


@eel.expose
def save_gimli_file(text_content):
    """Save assembled PyGIMLi file."""
    file_path = _save_dialog("Save PyGIMLi refraction file",
                             "refraction_picks.txt",
                             "Text files=*.txt|All files=*.*")
    if not file_path:
        return {"success": False, "error": "Cancelled"}
    try:
        with open(file_path, 'w') as f:
            f.write(text_content)
        print(f"PyGIMLi file saved: {file_path}")
        return {"success": True, "path": file_path}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════════════════════════
eel.init('web')

if __name__ == '__main__':
    eel.start('index.html', size=(1200, 900))
