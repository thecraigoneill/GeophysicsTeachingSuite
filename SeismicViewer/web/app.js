// app.js — SeismicViewer

// ── State ─────────────────────────────────────────────────────────────────────
let fileLoaded    = false;
let nShots        = 0;
let shotIndices   = [];
let pickMode      = false;
let picks         = {};         // { shotIdx: { shotIdx, sampleIdx, timeS } }
let activeTraces  = new Set();

// Exact axes bounds returned from Python after each render
// axes_norm: figure-fraction coords of axes box (x0,y0,x1,y1)
// data_bounds: data-space limits { xmin, xmax, ymin, ymax }
let axesNorm   = null;
let dataBounds = null;

// Active trace colour palette (mirrors Python ACTIVE_COLORS)
const ACTIVE_COLORS = [
    '#ff6b6b','#ffd93d','#6bcb77','#4d96ff',
    '#ff922b','#cc5de8','#20c997','#f06595',
];

// ── DOM helper ────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);

function setStatus(msg, color = '#60a5fa') {
    $('status-bar').textContent = msg;
    $('status-bar').style.color = color;
}
function setSpinner(on) { $('plot-spinner').style.display = on ? 'block' : 'none'; }

// ── Enable controls after file load ──────────────────────────────────────────
function enableControls(n) {
    ['n-scale','window-samples','t-max','y-centre','y-half',
     'plot-btn','save-btn','trace-dropdown-btn'].forEach(id => {
        const el = $(id);
        if (el) el.disabled = false;
    });

    // Y-centre: range 0 → n-1, start centred
    const yc = $('y-centre');
    yc.min = 0; yc.max = n - 1; yc.step = 0.5;
    yc.value = (n - 1) / 2;
    $('yc-max-label').textContent = n - 1;
    $('yc-min-label').textContent = 0;
    $('yc-val').textContent = ((n - 1) / 2).toFixed(1);

    // Y-half: max = half of shots, start at 10 or half
    const yh = $('y-half');
    yh.max   = Math.max(1, Math.floor(n / 2));
    yh.value = Math.min(10, Math.floor(n / 2));
    $('yh-val').textContent = `±${yh.value}`;
}

// ── File open ─────────────────────────────────────────────────────────────────
function openFile() {
    setStatus('Opening file dialog…');
    eel.open_dat_file()(function(result) {
        if (!result || !result.success) {
            setStatus('⚠️  ' + (result ? result.error : 'Unknown error'), '#f87171');
            return;
        }
        fileLoaded   = true;
        nShots       = result.n_shots;
        shotIndices  = result.shot_indices || [];
        picks        = {};
        activeTraces = new Set();
        axesNorm     = null;
        dataBounds   = null;

        $('file-label').textContent = result.filename;
        $('pick-total').textContent = result.n_shots;
        $('file-summary').style.display = 'block';
        $('file-summary-text').innerHTML =
            `<span>📄 ${result.filename}</span>` +
            `<span>Samples: ${result.n_samples.toLocaleString()}</span>` +
            `<span>Duration: ${result.duration_s} s</span>` +
            `<span>Shots: <strong style="color:#34d399">${result.n_shots}</strong></span>`;

        // t-max slider: hard cap 3 s, fine step 0.001 s
        const tSlider  = $('t-max');
        tSlider.min    = 0.001;
        tSlider.max    = 3.0;
        tSlider.step   = 0.001;
        tSlider.value  = Math.min(result.win_s, 3.0);
        $('tmax-val').textContent = tSlider.value + ' s';

        enableControls(result.n_shots);
        buildTraceChecklist(result.n_shots);
        refreshPicksTable();
        setStatus(`✅ Loaded: ${result.filename}  |  ${result.n_shots} shots`, '#34d399');
        generatePlot();
    });
}

// ── Slider handlers ───────────────────────────────────────────────────────────
function onScaleSlider(val) {
    $('n-scale-label').textContent = parseFloat(val).toFixed(1);
    debounce('scale', 280, generatePlot);
}

function onWindowSlider(val) {
    $('win-label').textContent = parseInt(val);
    debounce('window', 280, generatePlot);
}

function onTMaxSlider(val) {
    // Show fine precision for small values
    const v = parseFloat(val);
    $('tmax-val').textContent = (v < 0.1 ? v.toFixed(4) : v < 1 ? v.toFixed(3) : v.toFixed(2)) + ' s';
    debounce('tmax', 180, generatePlot);
}

function onYCentreSlider(val) {
    $('yc-val').textContent = parseFloat(val).toFixed(1);
    debounce('yview', 180, generatePlot);
}

function onYHalfSlider(val) {
    $('yh-val').textContent = `±${parseFloat(val).toFixed(1)}`;
    debounce('yview', 180, generatePlot);
}

function debounce(key, ms, fn) {
    if (!debounce._t) debounce._t = {};
    clearTimeout(debounce._t[key]);
    debounce._t[key] = setTimeout(fn, ms);
}

// ── Build picks payload for Python ───────────────────────────────────────────
function picksPayload() {
    return Object.values(picks).map(p => ({
        shot_idx: p.shotIdx,
        time_s:   p.timeS,
    }));
}

// ── Plot generation ───────────────────────────────────────────────────────────
function generatePlot() {
    if (!fileLoaded) return;

    const nScale  = parseFloat($('n-scale').value)      || 2.0;
    const win     = parseInt($('window-samples').value) || 1024;
    const tMax    = parseFloat($('t-max').value)        || 2.0;
    const yCentre = parseFloat($('y-centre').value);
    const yHalf   = parseFloat($('y-half').value)       || 10;
    const actArr  = Array.from(activeTraces);
    const pks     = picksPayload();

    setSpinner(true);
    $('plot-placeholder').style.display = 'none';
    setStatus('Rendering…');

    eel.generate_plot(nScale, win, tMax, yCentre, yHalf, actArr, pks)(function(result) {
        setSpinner(false);
        if (!result || !result.success) {
            setStatus('⚠️  ' + (result ? result.error : 'Plot failed'), '#f87171');
            return;
        }

        const img = $('plot-img');
        img.src = result.image;
        img.style.display = 'block';
        $('plot-placeholder').style.display = 'none';

        // Store EXACT axes bounds from Python for pixel-perfect click mapping
        axesNorm   = result.axes_norm;    // {x0,y0,x1,y1} in figure fractions
        dataBounds = result.data_bounds;  // {xmin,xmax,ymin,ymax} in data space

        setStatus(`✅ Plot ready  |  t: 0 → ${tMax.toFixed(3)} s`, '#34d399');
    });
}

// ── Plot click → exact data coordinates ──────────────────────────────────────
function handlePlotClick(event) {
    const img = $('plot-img');
    if (!img || img.style.display === 'none' || !axesNorm || !dataBounds) return;

    // img element size in screen pixels
    const rect   = img.getBoundingClientRect();
    const imgW   = rect.width;
    const imgH   = rect.height;

    // Click position as figure fractions
    const figFracX = (event.clientX - rect.left)  / imgW;
    const figFracY = (event.clientY - rect.top)   / imgH;

    // Map figure fraction → axes fraction using exact normalised axes bbox
    // Note: matplotlib y0 is BOTTOM, browser y increases downward so we invert
    const an = axesNorm;
    const axFracX = (figFracX - an.x0) / (an.x1 - an.x0);
    const axFracY = (figFracY - (1 - an.y1)) / (an.y1 - an.y0);  // invert y

    // Guard: outside axes area
    if (axFracX < 0 || axFracX > 1 || axFracY < 0 || axFracY > 1) return;

    // Map axes fraction → data space
    const db    = dataBounds;
    const dataX = db.xmin + axFracX * (db.xmax - db.xmin);
    const dataY = db.ymax - axFracY * (db.ymax - db.ymin);   // y is inverted

    const tDisp = dataX.toFixed(dataX < 0.01 ? 6 : dataX < 0.1 ? 5 : 4);
    $('click-info').innerHTML =
        (pickMode ? '🎯 <strong style="color:#fbbf24;">PICKING</strong>' : '🖱️ Coords') +
        `&nbsp;|&nbsp; <strong>t: ${tDisp} s</strong>` +
        `&nbsp;|&nbsp; shot: ${dataY.toFixed(2)}`;

    if (pickMode) {
        const shotIdx = Math.max(0, Math.min(nShots - 1, Math.round(dataY)));
        recordPick(shotIdx, dataX);
        // Replot immediately so cross appears
        generatePlot();
    }

    eel.record_click(dataX, dataY)(() => {});
}

// ── Pick mode ─────────────────────────────────────────────────────────────────
function togglePickMode(on) {
    pickMode = on;
    $('plot-container').classList.toggle('pick-active', on);
    setStatus(on ? '🎯 Pick mode ON — click to record first-break time' : 'Pick mode OFF',
              on ? '#fbbf24' : '#34d399');
}

function recordPick(shotIdx, timeS) {
    picks[shotIdx] = { shotIdx, sampleIdx: shotIndices[shotIdx], timeS };
    refreshPicksTable();
    eel.store_pick(shotIdx, shotIndices[shotIdx], timeS)(() => {});
    setStatus(`🎯 Shot ${shotIdx + 1} picked: ${timeS.toFixed(6)} s`, '#34d399');
}

function clearAllPicks() {
    if (!confirm('Clear all picks?')) return;
    picks = {};
    refreshPicksTable();
    eel.clear_picks()(() => {});
    generatePlot();
    setStatus('Picks cleared.', '#fbbf24');
}

function deletePick(shotIdx) {
    delete picks[shotIdx];
    refreshPicksTable();
    eel.delete_pick(shotIdx)(() => {});
    generatePlot();
}

function refreshPicksTable() {
    const tbody   = $('picks-tbody');
    const section = $('picks-section');
    const saveBtn = $('save-picks-btn');
    const list    = Object.values(picks).sort((a, b) => a.shotIdx - b.shotIdx);

    $('pick-count').textContent = list.length;
    saveBtn.disabled = list.length === 0;
    section.style.display = list.length ? 'block' : 'none';

    tbody.innerHTML = list.map(p => {
        const col = ACTIVE_COLORS[p.shotIdx % ACTIVE_COLORS.length];
        return `<tr style="border-bottom:1px solid #252830;">
            <td style="padding:5px 10px;color:#e0e0e0;">
                <span style="display:inline-block;width:10px;height:10px;
                border-radius:50%;background:${col};margin-right:6px;"></span>
                Shot ${p.shotIdx + 1}
            </td>
            <td style="padding:5px 10px;color:#9ca3af;">${p.sampleIdx}</td>
            <td style="padding:5px 10px;color:#34d399;font-weight:600;">${p.timeS.toFixed(6)}</td>
            <td style="padding:5px 10px;">
                <button onclick="deletePick(${p.shotIdx})" class="secondary"
                        style="padding:2px 7px;font-size:0.72rem;">✕</button>
            </td>
        </tr>`;
    }).join('');
}

function savePicks() {
    const list = Object.values(picks).sort((a, b) => a.shotIdx - b.shotIdx);
    if (!list.length) return;
    const payload = list.map(p => ({
        shot_number:  p.shotIdx + 1,
        sample_index: p.sampleIdx,
        time_s:       p.timeS,
    }));
    setStatus('Opening save dialog…');
    eel.save_picks_csv(payload)(function(result) {
        setStatus(result && result.success
            ? `💾 Picks saved → ${result.path}`
            : '⚠️  ' + (result ? result.error : 'Save failed'),
            result && result.success ? '#34d399' : '#f87171');
    });
}

// ── Active trace checklist ────────────────────────────────────────────────────
function buildTraceChecklist(n) {
    const list = $('trace-checklist');
    list.innerHTML = '';
    for (let i = 0; i < n; i++) {
        const color = ACTIVE_COLORS[i % ACTIVE_COLORS.length];
        const div   = document.createElement('div');
        div.className = 'trace-check-item';
        div.innerHTML = `
            <input type="checkbox" id="tc-${i}" value="${i}"
                   onchange="onTraceCheck(${i}, this.checked)">
            <span class="trace-color-dot" style="background:${color};opacity:0.5;"
                  id="tc-dot-${i}"></span>
            <label for="tc-${i}" style="margin:0;color:#c0c0d0;cursor:pointer;font-size:0.8rem;">
                Shot ${i + 1}
            </label>`;
        list.appendChild(div);
    }
    updateTraceBtnLabel();
}

function onTraceCheck(idx, checked) {
    if (checked) {
        activeTraces.add(idx);
        const dot = $(`tc-dot-${idx}`);
        if (dot) dot.style.opacity = '1';
    } else {
        activeTraces.delete(idx);
        const dot = $(`tc-dot-${idx}`);
        if (dot) dot.style.opacity = '0.5';
    }
    updateTraceBtnLabel();
    debounce('active', 150, generatePlot);
}

function clearActiveTraces() {
    activeTraces.clear();
    document.querySelectorAll('#trace-checklist input[type=checkbox]')
             .forEach(cb => { cb.checked = false; });
    document.querySelectorAll('.trace-color-dot')
             .forEach(d => { d.style.opacity = '0.5'; });
    updateTraceBtnLabel();
    $('trace-dropdown').style.display = 'none';
    debounce('active', 150, generatePlot);
}

function updateTraceBtnLabel() {
    const n   = activeTraces.size;
    const lbl = $('trace-btn-label');
    lbl.textContent = n === 0
        ? 'None selected'
        : n === 1 ? `Shot ${[...activeTraces][0] + 1} active`
        : `${n} traces active`;
}

function toggleTraceDropdown() {
    const dd = $('trace-dropdown');
    dd.style.display = dd.style.display === 'none' ? 'block' : 'none';
}

// Close dropdown on outside click
document.addEventListener('click', function(e) {
    const wrap = $('trace-checklist-wrap');
    if (wrap && !wrap.contains(e.target)) {
        const dd = $('trace-dropdown');
        if (dd) dd.style.display = 'none';
    }
});

// ── Save plot ─────────────────────────────────────────────────────────────────
function savePlot() {
    if (!fileLoaded) return;
    setStatus('Opening save dialog…');
    eel.save_plot_png()(function(result) {
        setStatus(result && result.success
            ? `💾 Saved → ${result.path}`
            : '⚠️  ' + (result ? result.error : 'Save failed'),
            result && result.success ? '#34d399' : '#f87171');
    });
}

// ── Apply params & reload ─────────────────────────────────────────────────────
function applyParams() {
    const s  = parseInt($('sampling').value)    || 512;
    const sk = parseInt($('skip-header').value) || 33;
    const ct = parseInt($('col-trace').value)   || 2;
    const cg = parseInt($('col-trigger').value) || 3;

    eel.update_params(s, sk, ct, cg)(function() {
        if (!fileLoaded) { setStatus('Parameters saved.', '#fbbf24'); return; }
        setStatus('Reloading…');
        eel.reload_file()(function(result) {
            if (!result || !result.success) {
                setStatus('⚠️  Reload failed: ' + (result ? result.error : '?'), '#f87171');
                return;
            }
            nShots       = result.n_shots;
            shotIndices  = result.shot_indices || [];
            picks        = {};
            activeTraces = new Set();
            axesNorm     = null;
            dataBounds   = null;
            enableControls(result.n_shots);
            buildTraceChecklist(result.n_shots);
            refreshPicksTable();
            $('pick-total').textContent = result.n_shots;
            setStatus(`✅ Reloaded  |  ${result.n_shots} shots`, '#34d399');
            generatePlot();
        });
    });
}

// ── Eel hooks ─────────────────────────────────────────────────────────────────
eel.expose(js_log);
function js_log(msg) { console.log('[py]', msg); }


// ══════════════════════════════════════════════════════════════════════════════
// TAB SWITCHING
// ══════════════════════════════════════════════════════════════════════════════
function switchTab(name) {
    var tabs = ['viewer', 'editdata', 'inversion'];
    for (var i = 0; i < tabs.length; i++) {
        var t     = tabs[i];
        var panel = document.getElementById('tab-' + t);
        var btn   = document.getElementById('tab-btn-' + t);
        if (!panel || !btn) continue;
        if (t === name) {
            panel.classList.add('tab-active');
            btn.classList.add('active');
        } else {
            panel.classList.remove('tab-active');
            btn.classList.remove('active');
        }
    }
}


// ══════════════════════════════════════════════════════════════════════════════
// TAB 2: EDIT DATA
// ══════════════════════════════════════════════════════════════════════════════
let editRows     = [];
let editFilePath = '';

function editLoadCSV() {
    setStatus('Opening CSV…');
    eel.load_picks_csv_for_edit()(function(result) {
        if (!result || !result.success) {
            setStatus('⚠️  ' + (result ? result.error : 'Failed'), '#f87171');
            return;
        }
        editRows     = result.rows;
        editFilePath = result.path;
        $('edit-file-label').textContent = result.filename;
        $('edit-save-btn').disabled      = false;
        $('edit-row-count').textContent  = editRows.length + ' rows loaded';
        renderEditTable();
        setStatus('✅ Loaded for editing: ' + result.filename, '#34d399');
    });
}

function renderEditTable() {
    const wrap = $('edit-table-wrap');
    if (!editRows.length) {
        wrap.innerHTML = '<div style="color:#4b5563;padding:20px;">No rows</div>';
        return;
    }

    const cols = [
        {key: '_sel',        label: ''},
        {key: 'shot_number', label: 'Shot #',   ro: true},
        {key: 'sample_index',label: 'Sample',   ro: true},
        {key: 'time_s',      label: 'Time (s)', ro: true, cls: 'pick-cell'},
        {key: 'shot_x',      label: 'Shot X'},
        {key: 'shot_y',      label: 'Shot Y'},
        {key: 'receiver_x',  label: 'Recv X'},
        {key: 'receiver_y',  label: 'Recv Y'},
    ];

    const maxH = 500;
    let html = `<div style="max-height:${maxH}px;overflow-y:auto;">
    <table class="edit-table">
    <thead><tr>
        <th><input type="checkbox" onchange="editSelectAll(this.checked)" title="Select all"></th>`;
    cols.slice(1).forEach(c => { html += `<th>${c.label}</th>`; });
    html += `</tr></thead><tbody>`;

    editRows.forEach((row, ri) => {
        html += `<tr id="edit-row-${ri}" onclick="editRowClick(event,${ri})">
            <td><input type="checkbox" class="row-select-cb" id="edit-sel-${ri}"
                       onclick="event.stopPropagation()"
                       onchange="editCheckRow(${ri},this.checked)"></td>`;
        cols.slice(1).forEach(c => {
            if (c.ro) {
                html += `<td class="${c.cls || 'readonly-cell'}">${row[c.key] || ''}</td>`;
            } else {
                const v = (row[c.key] || '').toString().replace(/"/g, '&quot;');
                html += `<td><input type="text" value="${v}"
                              oninput="editCellChange(${ri},'${c.key}',this.value)"
                              onclick="event.stopPropagation()"
                              placeholder="—"></td>`;
            }
        });
        html += `</tr>`;
    });
    html += `</tbody></table></div>`;
    wrap.innerHTML = html;
}

function editRowClick(event, ri) {
    const cb = $(`edit-sel-${ri}`);
    if (!cb) return;
    cb.checked = !cb.checked;
    editCheckRow(ri, cb.checked);
}

function editCheckRow(ri, checked) {
    const row = $(`edit-row-${ri}`);
    if (row) row.classList.toggle('selected-row', checked);
}

function editSelectAll(checked) {
    editRows.forEach((_, ri) => {
        const cb = $(`edit-sel-${ri}`);
        if (cb) { cb.checked = checked; editCheckRow(ri, checked); }
    });
}

function editCellChange(ri, key, val) {
    editRows[ri][key] = val;
}

function _getSelectedIndices() {
    return editRows.map((_, ri) => ri).filter(ri => {
        const cb = $(`edit-sel-${ri}`);
        return cb && cb.checked;
    });
}

function editApplyGeom() {
    const sel = _getSelectedIndices();
    if (!sel.length) { setStatus('⚠️  No rows selected', '#fbbf24'); return; }
    _applyGeomToIndices(sel);
}

function editApplyGeomAll() {
    _applyGeomToIndices(editRows.map((_, i) => i));
}

function _applyGeomToIndices(indices) {
    const sx = $('edit-shot-x').value;
    const sy = $('edit-shot-y').value || '0';
    const rx = $('edit-recv-x').value;
    const ry = $('edit-recv-y').value || '0';
    indices.forEach(ri => {
        if (sx !== '') editRows[ri].shot_x     = sx;
        if (sy !== '') editRows[ri].shot_y     = sy;
        if (rx !== '') editRows[ri].receiver_x = rx;
        if (ry !== '') editRows[ri].receiver_y = ry;
    });
    renderEditTable();
    setStatus('✅ Applied geometry to ' + indices.length + ' row(s)', '#34d399');
}

function editSaveCSV() {
    if (!editRows.length) return;
    setStatus('Opening save dialog…');
    eel.save_edited_csv(editFilePath, editRows)(function(result) {
        setStatus(result && result.success
            ? '💾 Saved → ' + result.path
            : '⚠️  ' + (result ? result.error : 'Save failed'),
            result && result.success ? '#34d399' : '#f87171');
    });
}


// ══════════════════════════════════════════════════════════════════════════════
// TAB 3: INVERSION DATA
// ══════════════════════════════════════════════════════════════════════════════
let invFiles        = [];
let invAssembledTxt = '';

function invGlob() {
    setStatus('Selecting folder…');
    eel.glob_picks_files()(function(result) {
        if (!result || !result.success) {
            setStatus('⚠️  ' + (result ? result.error : 'Failed'), '#f87171');
            return;
        }
        invFiles = result.files;
        $('inv-folder-label').textContent = result.folder;
        $('inv-file-count').textContent   = result.count + ' CSV file(s) found';
        $('inv-preview-btn').disabled     = result.count === 0;
        $('inv-assemble-btn').disabled    = true;
        $('inv-save-btn').disabled        = true;
        $('inv-filelist-section').style.display = result.count ? 'block' : 'none';
        $('inv-preview-section').style.display  = 'none';
        renderInvFileList(invFiles.map(function(f) {
            return {
                file: f,
                name: f.replace(/\\/g, '/').split('/').pop(),
                ok: null
            };
        }));
        setStatus('Found ' + result.count + ' CSV file(s)', '#34d399');
    });
}

function renderInvFileList(items) {
    const list = $('inv-filelist');
    list.innerHTML = items.map(function(item) {
        const dot   = item.ok === null ? '#4b5563' :
                      item.ok          ? '#34d399' : '#f87171';
        const badge = item.ok === null ? '?' : item.ok ? '✓' : '✗';
        const info  = item.valid !== undefined
            ? item.valid + '/' + item.total + ' valid' : '';
        return '<div class="inv-file-row">' +
               '<div class="inv-file-status" style="background:' + dot + ';">' + badge + '</div>' +
               '<div class="inv-file-name" title="' + item.file + '">' + item.name + '</div>' +
               '<div class="inv-file-count">' + info + '</div>' +
               '</div>';
    }).join('');
}

function invPreview() {
    if (!invFiles.length) return;
    setStatus('Validating files…');
    eel.preview_csv_files(invFiles)(function(result) {
        if (!result || !result.success) {
            setStatus('⚠️  Validation failed', '#f87171');
            return;
        }
        const s       = result.summary;
        const allOk   = s.every(function(r) { return r.ok; });
        const totalV  = s.reduce(function(a, r) { return a + r.valid; }, 0);
        const totalR  = s.reduce(function(a, r) { return a + r.total; }, 0);
        const missing = s.reduce(function(a, r) { return a + r.missing; }, 0);

        renderInvFileList(s);
        $('inv-validation-summary').innerHTML =
            totalV + '/' + totalR + ' rows complete' +
            (missing ? '<br><span style="color:#fbbf24;">⚠️ ' + missing + ' rows missing geometry</span>' : '') +
            '<br><span style="color:' + (allOk ? '#34d399' : '#f87171') + ';">' +
            (allOk ? 'All files OK' : 'Some files have issues') + '</span>';
        $('inv-assemble-btn').disabled = totalV === 0;
        setStatus(allOk
            ? '✅ Validation passed — ' + totalV + ' picks ready'
            : '⚠️  ' + totalV + ' complete, ' + missing + ' incomplete',
            allOk ? '#34d399' : '#fbbf24');
    });
}

function invAssemble() {
    if (!invFiles.length) return;
    const elevY = $('inv-elev-y').checked;
    setStatus('Assembling PyGIMLi file…');
    eel.assemble_gimli(invFiles, elevY)(function(result) {
        if (!result || !result.success) {
            setStatus('⚠️  Assembly failed: ' + (result ? result.error : '?'), '#f87171');
            return;
        }
        invAssembledTxt = result.full_text;
        $('inv-preview-section').style.display = 'block';
        $('inv-preview-text').textContent = result.preview + '\n…';
        $('inv-assemble-summary').innerHTML =
            '<span style="color:#34d399;">✅ ' + result.n_positions + ' sensor positions</span><br>' +
            result.n_measurements + ' measurements' +
            (result.skipped ? '<br><span style="color:#fbbf24;">⚠️ ' + result.skipped + ' rows skipped</span>' : '');
        $('inv-save-btn').disabled = false;
        setStatus('✅ Assembled: ' + result.n_positions + ' positions, ' +
                  result.n_measurements + ' measurements', '#34d399');
    });
}

function invSave() {
    if (!invAssembledTxt) return;
    setStatus('Opening save dialog…');
    eel.save_gimli_file(invAssembledTxt)(function(result) {
        setStatus(result && result.success
            ? '💾 Saved → ' + result.path
            : '⚠️  ' + (result ? result.error : 'Save failed'),
            result && result.success ? '#34d399' : '#f87171');
    });
}
