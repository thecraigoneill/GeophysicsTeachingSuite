// web/script.js

let canvas = document.getElementById('canvas');
let ctx = canvas.getContext('2d');

// ─── Reference plot canvas ──────────────────────────────────────────────────
let plotCanvas = document.getElementById('plotCanvas');
let plotCtx = plotCanvas ? plotCanvas.getContext('2d') : null;
let backgroundDensity = 0;
let plotData = [];  // [{x: number, y: number}] in meters
//let plotYMin = -100;  // initial default range in nT
//let plotYMax = 100;
let mode = 'mag';  // 'mag' or 'grav'
let magData = [];
let gravData = [];
let magLoadedData = [];   // loaded CSV for magnetics
let magModelledData = []; // calculated for magnetics

let gravLoadedData = [];   // loaded CSV for gravity
let gravModelledData = []; // calculated for gravity

let showLoaded = true;     // toggles
let showModelled = true;

// ─── Polygon editing variables ──────────────────────────────────────────────
let points = [];                    // CURRENT polygon's points — in METERS
let currentPolyIndex = -1;
let editingIndex = -1;
let isDragging = false;

let allPolygons = [];               // all polygons — in METERS

const POLYGON_COLORS = [
    '#60a5fa', '#34d399', '#f472b6', '#fbbf24',
    '#a78bfa', '#f87171', '#4ade80', '#38bdf8'
];

// ─── Bounds (meters) ────────────────────────────────────────────────────────
let bounds = {
    xmin: 0,
    xmax: 5000,
    ymin: 0,
    ymax: 1000
};

function setMode(newMode) {
    if (newMode === mode) return;  // no change

    mode = newMode;

    // Update button appearance
    document.getElementById('magButton').style.opacity = mode === 'mag' ? '1.0' : '0.4';
    document.getElementById('gravButton').style.opacity = mode === 'grav' ? '1.0' : '0.4';

    // Switch data
    plotData = mode === 'mag' ? magModelledData : gravModelledData;

    // Redraw plot (will show "No data" if empty)
    redrawPlot();

    console.log("Switched to mode:", mode, "with", plotData.length, "points");
}

function loadMagCSV() {
    eel.load_plot_csv()(function(result) {
        if (result && result.success && result.points) {
            magLoadedData = result.points.map(([x, y]) => ({x: Number(x), y: Number(y)}));
            if (mode === 'mag') redrawPlot();
        }
    });
}

function loadGravCSV() {
    eel.load_plot_csv()(function(result) {
        if (result && result.success && result.points) {
            gravLoadedData = result.points.map(([x, y]) => ({x: Number(x), y: Number(y)}));
            if (mode === 'grav') redrawPlot();
        }
    });
}

function saveProfile() {
    const currentData = mode === 'mag' ? magData : gravData;
    if (currentData.length === 0) {
        alert("No profile data to save.");
        return;
    }
    const csvContent = "data:text/csv;charset=utf-8," + currentData.map(p => p.x + "," + p.y).join("\n");
    const link = document.createElement("a");
    link.setAttribute("href", encodeURI(csvContent));
    link.setAttribute("download", mode + "_profile.csv");
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
}

function getPlotYRange() {
    if (plotData.length === 0) return { min: -100, max: 100 }; // fallback

    let minY = Infinity;
    let maxY = -Infinity;

    plotData.forEach(p => {
        if (!isNaN(p.y)) {
            minY = Math.min(minY, p.y);
            maxY = Math.max(maxY, p.y);
        }
    });

    if (minY === Infinity) return { min: -100, max: 100 };

    const dy = maxY - minY;
    const buffer = dy * 0.1 || 10; // 10% buffer or min 10 nT

    return {
        min: minY - buffer,
        max: maxY + buffer
    };
}

function pixelToMeters(px, py) {
    const w = canvas.width;
    const h = canvas.height;
    const realX = bounds.xmin + (px / w) * (bounds.xmax - bounds.xmin);
    const realY = bounds.ymin + (py / h) * (bounds.ymax - bounds.ymin);
    return { x: realX, y: realY };
}

function metersToPixel(mx, my) {
    const w = canvas.width;
    const h = canvas.height;
    const dx = bounds.xmax - bounds.xmin;
    const dy = bounds.ymax - bounds.ymin;

    if (dx <= 0 || dy <= 0 || isNaN(dx) || isNaN(dy)) {
        console.warn("Invalid bounds in metersToPixel", bounds);
        return { x: w / 2, y: h / 2 }; // center fallback
    }

    let px = ((mx - bounds.xmin) / dx) * w;
    let py = ((my - bounds.ymin) / dy) * h;

    // Clamp to canvas
    px = Math.max(0, Math.min(w, px));
    py = Math.max(0, Math.min(h, py));

    return { x: px, y: py };
}

function metersToPixelPlot(mx, my, plotYMin, plotYMax) {
    if (!plotCanvas) return { x: 0, y: 0 };
    const w = plotCanvas.width;
    const h = plotCanvas.height;

    const dx = bounds.xmax - bounds.xmin;
    if (dx <= 0 || isNaN(dx)) return { x: w / 2, y: h / 2 };

    const px = ((mx - bounds.xmin) / dx) * w;

    // Use independent plot y range for scaling
    const dy = plotYMax - plotYMin;
    if (dy <= 0 || isNaN(dy)) return { x: px, y: h / 2 };
    const py = h - ((my - plotYMin) / dy) * h;  // flip y so positive nT up

    return { x: Math.max(0, Math.min(w, px)), y: Math.max(0, Math.min(h, py)) };
}

// ─── Apply new bounds ───────────────────────────────────────────────────────
function applyBounds() {
    const xmin = parseFloat(document.getElementById('xmin').value);
    const xmax = parseFloat(document.getElementById('xmax').value);
    const ymin = parseFloat(document.getElementById('ymin').value);
    const ymax = parseFloat(document.getElementById('ymax').value);

    if (isNaN(xmin) || isNaN(xmax) || isNaN(ymin) || isNaN(ymax) ||
        xmax <= xmin || ymax <= ymin) {
        alert("Invalid bounds.\nMake sure xmax > xmin and ymax > ymin.");
        return;
    }

    bounds = { xmin, xmax, ymin, ymax };

    redraw();
    redrawPlot();
}

// ─── Redraw main polygon canvas ─────────────────────────────────────────────
function redraw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = '#1a1d22';
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    allPolygons.forEach((polyObj, idx) => {
        const polyMeters = polyObj.points || [];  // ← only ONE declaration here
        if (polyMeters.length === 0) return;

        const isActive = (idx === currentPolyIndex);
        const color = POLYGON_COLORS[idx % POLYGON_COLORS.length];

        const polyPixels = polyMeters.map(p => metersToPixel(p.x, p.y));

        // Lines
        if (polyPixels.length >= 2) {
            ctx.strokeStyle = color;
            ctx.lineWidth = isActive ? 3 : 1.8;
            ctx.globalAlpha = isActive ? 1.0 : 0.3;
            ctx.beginPath();
            ctx.moveTo(polyPixels[0].x, polyPixels[0].y);
            for (let i = 1; i < polyPixels.length; i++) {
                ctx.lineTo(polyPixels[i].x, polyPixels[i].y);
            }
            ctx.closePath();
            ctx.stroke();
        }

        // Nodes
        if (isActive) {
            ctx.globalAlpha = 1.0;
            ctx.fillStyle = '#ef4444';
            ctx.strokeStyle = '#ffffff';
            ctx.lineWidth = 1.5;
            polyPixels.forEach(p => {
                ctx.beginPath();
                ctx.arc(p.x, p.y, 6, 0, 2 * Math.PI);
                ctx.fill();
                ctx.stroke();
            });
        }

        ctx.globalAlpha = 1.0;
    });

    drawScale();
}

// ─── Redraw reference plot canvas ───────────────────────────────────────────
// Y-axis ticks & labels - every tick gets a label
function redrawPlot() {
	if (!plotCtx) return;

    plotCtx.clearRect(0, 0, plotCanvas.width, plotCanvas.height);
    plotCtx.fillStyle = '#0f1117';
    plotCtx.fillRect(0, 0, plotCanvas.width, plotCanvas.height);

    let loadedData = mode === 'mag' ? magLoadedData : gravLoadedData;
    let modelledData = mode === 'mag' ? magModelledData : gravModelledData;

    if (loadedData.length === 0 && modelledData.length === 0) {
        plotCtx.fillStyle = '#888';
        plotCtx.font = '16px Arial';
        plotCtx.textAlign = 'center';
        plotCtx.fillText(`No ${mode === 'mag' ? 'magnetics' : 'gravity'} data or model yet`, plotCanvas.width/2, plotCanvas.height/2);
        return;
    }

    // Compute y-range from BOTH datasets if present
    let plotYMin = Infinity;
    let plotYMax = -Infinity;

    [loadedData, modelledData].forEach(dataset => {
        dataset.forEach(p => {
            if (!isNaN(p.y)) {
                plotYMin = Math.min(plotYMin, p.y);
                plotYMax = Math.max(plotYMax, p.y);
            }
        });
    });

    if (plotYMin === Infinity) return;

    const dy = plotYMax - plotYMin;
    const buffer = dy * 0.1 || 10;
    plotYMin -= buffer;
    plotYMax += buffer;

    // Zero line
    const zeroY = metersToPixelPlot(0, 0, plotYMin, plotYMax).y;
    plotCtx.strokeStyle = '#555';
    plotCtx.lineWidth = 1;
    plotCtx.beginPath();
    plotCtx.moveTo(0, zeroY);
    plotCtx.lineTo(plotCanvas.width, zeroY);
    plotCtx.stroke();
    // 3. Data line
// Draw loaded data (if present)
    if (showLoaded && loadedData.length >= 2) {
        plotCtx.strokeStyle = '#a5d6ff'; // blue for loaded
        plotCtx.lineWidth = 2.5;
        plotCtx.beginPath();
        let first = metersToPixelPlot(loadedData[0].x, loadedData[0].y, plotYMin, plotYMax);
        plotCtx.moveTo(first.x, first.y);
        for (let i = 1; i < loadedData.length; i++) {
            let p = metersToPixelPlot(loadedData[i].x, loadedData[i].y, plotYMin, plotYMax);
            plotCtx.lineTo(p.x, p.y);
        }
        plotCtx.stroke();
    }

    // Draw modelled response (if present)
    if (showModelled && modelledData.length >= 2) {
        plotCtx.strokeStyle = '#f87171'; // red for modelled
        plotCtx.lineWidth = 2.5;
        plotCtx.beginPath();
        let first = metersToPixelPlot(modelledData[0].x, modelledData[0].y, plotYMin, plotYMax);
        plotCtx.moveTo(first.x, first.y);
        for (let i = 1; i < modelledData.length; i++) {
            let p = metersToPixelPlot(modelledData[i].x, modelledData[i].y, plotYMin, plotYMax);
            plotCtx.lineTo(p.x, p.y);
        }
        plotCtx.stroke();
    }

    // 5. Y-axis ticks & numeric labels (every tick, no units)
    // Y-axis ticks & numeric labels - every tick, no units, forced visibility
	const tickLength = 8;          // longer for better visibility
	const textOffset = 30;          // pushed further inside to avoid any clipping
	const fontSize = 12;            // larger and clearer

	plotCtx.strokeStyle = '#aaa' //ffffff';  // bright white ticks

	//ctx.strokeStyle = '#aaa';
        //ctx.fillStyle   = '#ddd';
	plotCtx.fillStyle   = '#ddd';  // YELLOW labels — impossible to miss
	plotCtx.lineWidth   = 2;          // thicker ticks
	plotCtx.font        = `bold ${fontSize}px Arial, sans-serif`;
	plotCtx.textBaseline = 'middle';

	const rangeY = plotYMax - plotYMin;
	if (rangeY > 0) {
    		const stepY = niceStep(rangeY / 12);  // aim for 8–12 labels
    		//console.log(`Attempting ${Math.round(rangeY / stepY)} y-labels: range ${plotYMin.toFixed(1)} to ${plotYMax.toFixed(1)}, step ${stepY}`);

   	 // Left side
    	plotCtx.textAlign = 'right';
    	for (let val = Math.ceil(plotYMin / stepY) * stepY; val <= plotYMax + 1e-6; val += stepY) {
        	const py = metersToPixelPlot(0, val, plotYMin, plotYMax).y;
        	if (isNaN(py) || py < -10 || py > plotCanvas.height + 10) continue;

        	// Tick inward
        	plotCtx.beginPath();
        	plotCtx.moveTo(0, py);
        	plotCtx.lineTo(tickLength, py);
        	plotCtx.stroke();

        	// Numeric label (yellow, bold, larger offset)
        	const decimals = Math.abs(val) < 10 ? 1 : 0;
        	const labelText = val.toFixed(decimals);
        	plotCtx.fillText(labelText, textOffset, py);

        	//console.log(`Left label: "${labelText}" at y=${py.toFixed(1)}, x=${py.toFixed(1)}`);
   	 }

    	// Right side (same)
    	plotCtx.textAlign = 'left';
    	for (let val = Math.ceil(plotYMin / stepY) * stepY; val <= plotYMax + 1e-6; val += stepY) {
        	const py = metersToPixelPlot(0, val, plotYMin, plotYMax).y;
        	if (isNaN(py) || py < -10 || py > plotCanvas.height + 10) continue;

        	plotCtx.beginPath();
        	plotCtx.moveTo(plotCanvas.width, py);
        	plotCtx.lineTo(plotCanvas.width - tickLength, py);
        	plotCtx.stroke();

        	const decimals = Math.abs(val) < 10 ? 1 : 0;
        	const labelText = val.toFixed(decimals);
        	plotCtx.fillText(labelText, plotCanvas.width + textOffset, py);

        	console.log(`Right label: "${labelText}" at y=${py.toFixed(1)}`);
   	 }
	} else {
    		console.warn("No valid y-range for labels (rangeY <= 0)");

	}
    // 6. Dynamic axis title (no units on ticks)
    plotCtx.save();
    plotCtx.translate(30, plotCanvas.height / 2);
    plotCtx.rotate(-Math.PI / 2);
    plotCtx.textAlign = 'center';
    plotCtx.fillStyle = '#ffffff';
    plotCtx.font = 'bold 14px Arial, sans-serif';
    const titleText = mode === 'mag' ? 'Magnetic Anomaly (nT)' : 'Gravity (mGal)';
    plotCtx.fillText(titleText, 0, 1.01*textOffset);
    plotCtx.restore();
}


function calculateMagResponse() {
    const inclination = parseFloat(document.getElementById('inclination').value);
    const declination = parseFloat(document.getElementById('declination').value);
    const orientation = parseFloat(document.getElementById('orientation').value);

    const currentBounds = {
        xmin: parseFloat(document.getElementById('xmin').value),
        xmax: parseFloat(document.getElementById('xmax').value),
        ymin: parseFloat(document.getElementById('ymin').value),
        ymax: parseFloat(document.getElementById('ymax').value)
    };

    if ([inclination, declination, orientation, currentBounds.xmin, currentBounds.xmax, currentBounds.ymin, currentBounds.ymax].some(isNaN)) {
        alert("Please enter valid numbers for all fields.");
        return;
    }

    const polygonsWithSus = allPolygons.map(polyObj => ({
        points: polyObj.points,
        susceptibility: polyObj.susceptibility || 0.01
    }));

    console.log("Sending to Python:", {
        polygonsCount: polygonsWithSus.length,
        bounds: currentBounds
    });

    eel.calculate_magnetic_response(
        polygonsWithSus,
        inclination,
        declination,
        orientation,
        currentBounds
    )((result) => {
        console.log("Python returned mag result:", result);
        if (result && result.length > 0) {
            magModelledData = result.map(([x, y]) => ({x: Number(x), y: Number(y)}));
            if (mode === 'mag') {
                plotData = magModelledData;
                redrawPlot();
            }
        } else {
            console.warn("No magnetic response data");
        }
    });
}

function calculateGravResponse() {
    const density = parseFloat(prompt("Enter density (kg/m3):", "1000"));
    if (isNaN(density)) return;

    eel.calculate_gravity_response(polygonsWithSus, density, currentBounds)((result) => {
        if (result && result.length > 0) {
            gravModelledData = result.map(([x, y]) => ({x: Number(x), y: Number(y)}));
            if (mode === 'grav') plotData = gravModelledData;
            redrawPlot();
        }
    });
}

// Updated calculateResponse() to compute both mag and grav
function calculateResponse() {
    const inclination = parseFloat(document.getElementById('inclination').value);
    const declination = parseFloat(document.getElementById('declination').value);
    const orientation = parseFloat(document.getElementById('orientation').value);
    const polygonsWithSus = allPolygons.map(polyObj => ({
        points: polyObj.points,
        susceptibility: polyObj.susceptibility || 0.01, density: polyObj.density || 1000 
    }));
    const currentBounds = {
        xmin: parseFloat(document.getElementById('xmin').value),
        xmax: parseFloat(document.getElementById('xmax').value),
        ymin: parseFloat(document.getElementById('ymin').value),
        ymax: parseFloat(document.getElementById('ymax').value)
    };

    if ([inclination, declination, orientation, currentBounds.xmin, currentBounds.xmax, currentBounds.ymin, currentBounds.ymax].some(isNaN)) {
        alert("Please enter valid numbers for all fields.");
        return;
    }

    // Compute magnetics
    eel.calculate_magnetic_response(polygonsWithSus, inclination, declination, orientation, currentBounds)((magResult) => {
        if (magResult && magResult.length > 0) {
            magModelledData = magResult.map(([x, y]) => ({x: Number(x), y: Number(y)}));
        } else {
            console.warn("No magnetics data");
        }
    });

    // Compute gravity — prompt for density
    const backgroundDensity = parseFloat(document.getElementById('backgroundDensity').value);
    if (isNaN(backgroundDensity)) { alert("Invalid background density."); return; }

    eel.calculate_gravity_response(polygonsWithSus, backgroundDensity, currentBounds)((gravResult) => {
            if (gravResult && gravResult.length > 0) {
                gravModelledData = gravResult.map(([x, y]) => ({x: Number(x), y: Number(y)}));
            } else {
                console.warn("No gravity data");
            }
        }); 

    // After calculations, update plot to show current mode
    setTimeout(() => {  // small delay to ensure data is set
        plotData = mode === 'mag' ? magModelledData : gravModelledData;
        redrawPlot();
        //alert("Responses calculated for both modes.");
    }, 500);  // adjust delay if needed
}



function autoFitBoundsToPlotData(paddingPercent = 0.0) {
    if (plotData.length < 1) return;

    console.time("autoFit");

    let minX = Infinity, maxX = -Infinity;
    let minY = Infinity, maxY = -Infinity;

    plotData.forEach(p => {
        if (typeof p.x !== 'number' || typeof p.y !== 'number' || isNaN(p.x) || isNaN(p.y)) return;
        minX = Math.min(minX, p.x);
        maxX = Math.max(maxX, p.x);
        minY = Math.min(minY, p.y);
        maxY = Math.max(maxY, p.y);
    });

    if (minX === Infinity || maxX === -Infinity) {
        console.warn("No valid data points for fitting");
        return;
    }

    let dx = maxX - minX;
    let dy = maxY - minY;

    // Prevent zero width/height
    if (dx < 1e-6) dx = 1;
    if (dy < 1e-6) dy = 1;

    const padX = dx * paddingPercent;
    const padY = dy * paddingPercent;

    //bounds = {
    //    xmin: minX - padX,
    //    xmax: maxX + padX,
    //    ymin: minY - padY,
    //    ymax: maxY + padY
    //};
    bounds.xmin = minX - padX;
    bounds.xmax = maxX + padX;

    // Final safety clamp
    if (bounds.xmax <= bounds.xmin) bounds.xmax = bounds.xmin + 10;
    //if (bounds.ymax <= bounds.ymin) bounds.ymax = bounds.ymin + 10;

    // Update text boxes
    document.getElementById('xmin').value = bounds.xmin.toFixed(2);
    document.getElementById('xmax').value = bounds.xmax.toFixed(2);
    //document.getElementById('ymin').value = bounds.ymin.toFixed(2);
    //document.getElementById('ymax').value = bounds.ymax.toFixed(2);

    //console.timeEnd("autoFit");
    redraw();
    redrawPlot();
}

// ─── Load reference data from CSV ───────────────────────────────────────────
function loadPlotCSV() {
    console.log("JS: Starting loadPlotCSV()");
    eel.load_plot_csv()(function(result) {
        console.log("JS: Python returned →", result);
	//alert("Data received: " + (result?.points?.length || 0) + " points");
        if (result && result.success && result.points) {
            plotData = result.points.map(([x, y]) => ({
                x: Number(x),
                y: Number(y)
            }));
            autoFitBoundsToPlotData();   // ← we'll add this next
            redrawPlot();
            applyBounds();               // update main canvas too
        } 
	else {
            alert("Failed to load plot CSV" + (result?.error ? ": " + result.error : ""));
        }
    });
}
// ─── Scale drawing (tick marks around edge) ────────────────────────────────
function drawScale() {
    console.log("drawScale() called — current bounds:", JSON.stringify(bounds));

    const tickLength = 8;               // length inward
    const textOffset = 20;              // distance from edge to text
    const fontSize = 12;
    const majorEvery = 5;

    ctx.strokeStyle = '#aaa';
    ctx.fillStyle   = '#ddd';
    ctx.lineWidth   = 1.5;
    ctx.font        = `${fontSize}px Arial, sans-serif`;

    const rangeX = bounds.xmax - bounds.xmin;
    const rangeY = bounds.ymax - bounds.ymin;

    if (rangeX <= 0 || rangeY <= 0 || isNaN(rangeX) || isNaN(rangeY)) {
        console.warn("Invalid range — skipping scale");
        return;
    }

    const stepX = niceStep(rangeX / 10);
    const stepY = niceStep(rangeY / 8);

    console.log(`stepX: ${stepX}, stepY: ${stepY}`);

    // ─── Bottom edge — Distance (m) ────────────────────────────────────────
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    for (let val = Math.ceil(bounds.xmin / stepX) * stepX; val <= bounds.xmax + 1e-6; val += stepX) {
        const px = metersToPixel(val, bounds.ymin).x;
        if (isNaN(px) || px < 0 || px > canvas.width) continue;

        // Tick inward (up)
        ctx.beginPath();
        ctx.moveTo(px, canvas.height);
        ctx.lineTo(px, canvas.height - tickLength);
        ctx.stroke();

        // Major label
        if (Math.round(val / stepX) % majorEvery === 0 ||
            Math.abs(val - bounds.xmin) < 1e-6 ||
            Math.abs(val - bounds.xmax) < 1e-6) {
            ctx.fillText(val.toFixed(precision(val)), px, canvas.height - tickLength - textOffset);
        }
    }
    ctx.fillText("Distance (m)", canvas.width / 2, canvas.height - tickLength - textOffset - 20);

    // ─── Top edge — Distance (m) ───────────────────────────────────────────
    ctx.textBaseline = 'bottom';
    for (let val = Math.ceil(bounds.xmin / stepX) * stepX; val <= bounds.xmax + 1e-6; val += stepX) {
        const px = metersToPixel(val, bounds.ymin).x;
        if (isNaN(px) || px < 0 || px > canvas.width) continue;

        ctx.beginPath();
        ctx.moveTo(px, 0);
        ctx.lineTo(px, tickLength);
        ctx.stroke();

        if (Math.round(val / stepX) % majorEvery === 0 ||
            Math.abs(val - bounds.xmin) < 1e-6 ||
            Math.abs(val - bounds.xmax) < 1e-6) {
            ctx.fillText(val.toFixed(precision(val)), px, tickLength + textOffset);
        }
    }

    // ─── Left edge — Depth (m) ─────────────────────────────────────────────
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (let val = Math.ceil(bounds.ymin / stepY) * stepY; val <= bounds.ymax + 1e-6; val += stepY) {
        const py = metersToPixel(bounds.xmin, val).y;
        if (isNaN(py) || py < 0 || py > canvas.height) continue;

        ctx.beginPath();
        ctx.moveTo(0, py);
        ctx.lineTo(tickLength, py);
        ctx.stroke();

        if (Math.round(val / stepY) % majorEvery === 0 ||
            Math.abs(val - bounds.ymin) < 1e-6 ||
            Math.abs(val - bounds.ymax) < 1e-6) {
            ctx.fillText(val.toFixed(precision(val)), tickLength + textOffset, py);
        }
    }
    ctx.save();
    ctx.translate(textOffset + 30, canvas.height / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textAlign = 'center';
    ctx.fillText("Depth (m)", 0, 0);
    ctx.restore();

    // ─── Right edge — Depth (m) ────────────────────────────────────────────
    ctx.textAlign = 'left';
    for (let val = Math.ceil(bounds.ymin / stepY) * stepY; val <= bounds.ymax + 1e-6; val += stepY) {
        const py = metersToPixel(bounds.xmin, val).y;
        if (isNaN(py) || py < 0 || py > canvas.height) continue;

        ctx.beginPath();
        ctx.moveTo(canvas.width, py);
        ctx.lineTo(canvas.width - tickLength, py);
        ctx.stroke();

        if (Math.round(val / stepY) % majorEvery === 0 ||
            Math.abs(val - bounds.ymin) < 1e-6 ||
            Math.abs(val - bounds.ymax) < 1e-6) {
            ctx.fillText(val.toFixed(precision(val)), canvas.width - tickLength - textOffset, py);
        }
    }

    console.log("drawScale() finished — scale should now be visible");
}

function niceStep(range) {
    if (range <= 0) return 1;
    const magnitude = Math.pow(10, Math.floor(Math.log10(range)));
    const fraction = range / magnitude;
    let nice = 1;
    if (fraction > 1.5) nice = 2;
    if (fraction > 3)   nice = 5;
    if (fraction > 7)   nice = 10;
    return nice * magnitude;
}

function precision(val) {
    if (Math.abs(val) < 1e-6) return 0;
    return Math.max(0, -Math.floor(Math.log10(Math.abs(val))) + 1);
}

// ─── Mouse events ───────────────────────────────────────────────────────────
canvas.addEventListener('mousedown', (e) => {
    if (currentPolyIndex < 0) return;

    const rect = canvas.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;

    const nearby = findNearbyPoint(px, py);
    if (nearby !== -1) {
        editingIndex = nearby;
        isDragging = true;
        return;
    }

    const real = pixelToMeters(px, py);
    points.push(real);
    if (currentPolyIndex < allPolygons.length) {
        allPolygons[currentPolyIndex] = { 
    		points: points.slice(), 
    		susceptibility: allPolygons[currentPolyIndex]?.susceptibility || 0.01,
		density: allPolygons[currentPolyIndex]?.density || 1000 
	};
    }
    redraw();
    updatePointsList();
    sendPointsToPython();
});

canvas.addEventListener('mousemove', (e) => {
    if (!isDragging || editingIndex === -1) return;

    const rect = canvas.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;

    const real = pixelToMeters(px, py);
    points[editingIndex] = real;
    if (currentPolyIndex < allPolygons.length) {
         allPolygons[currentPolyIndex] = { 
    		points: points.slice(), 
    		susceptibility: allPolygons[currentPolyIndex]?.susceptibility || 0.01,
		density: allPolygons[currentPolyIndex]?.density || 1000 
	};

    }
    redraw();
});

canvas.addEventListener('mouseup', () => {
    if (isDragging && editingIndex !== -1) sendPointsToPython();
    isDragging = false;
    editingIndex = -1;
});

canvas.addEventListener('mouseleave', () => {
    isDragging = false;
    editingIndex = -1;
});

function findNearbyPoint(px, py, threshold = 14) {
    if (currentPolyIndex < 0) return -1;
    const meters = pixelToMeters(px, py);
    for (let i = 0; i < points.length; i++) {
        const p = points[i];
        if (Math.hypot(meters.x - p.x, meters.y - p.y) < 2) {
            return i;
        }
    }
    return -1;
}

// ─── UI / Controls ──────────────────────────────────────────────────────────
function updatePointsList() {
    const ul = document.getElementById('points-ul');
    ul.innerHTML = '';
    if (points.length === 0) {
        ul.innerHTML = '<li style="color:#666; font-style:italic;">No points yet</li>';
        return;
    }
    points.forEach((p, i) => {
        const li = document.createElement('li');
        li.innerHTML = `(${p.x.toFixed(3)}, ${p.y.toFixed(3)}) <button onclick="deletePoint(${i})">×</button>`;
        ul.appendChild(li);
    });
}

function updatePolygonsList() {
    eel.get_all_polygons_summary()(function(info) {
        const container = document.getElementById('polygons-list');
        container.innerHTML = '';
        if (info.length === 0) {
            container.innerHTML = '<div style="color:#666; padding:10px; font-style:italic;">No polygons yet</div>';
            return;
        }
        info.forEach(item => {
            const div = document.createElement('div');
            div.className = 'poly-option' + (item.index === currentPolyIndex ? ' selected' : '');
            div.textContent = `${item.name} (${item.point_count} pts)`;
            div.onclick = () => switchToPolygon(item.index);
            container.appendChild(div);
        });
    });
}

function switchToPolygon(index) {
    eel.set_current_polygon(index)(function(success) {
        if (success) redraw();
    });
}

function sendPointsToPython() {
    const pyPoints = points.map(p => [p.x, p.y]);
    eel.update_polygon_from_js(pyPoints);
}

function addNewPolygon() {
    eel.add_new_polygon()();  // no callback needed

    // Since we can't get the index back directly,
    // force a sync call to get the current index
    eel.get_current_poly_index()(function(idx) {
        currentPolyIndex = idx;
        points = [];
        while (allPolygons.length <= currentPolyIndex) {
            allPolygons.push({ points: [], susceptibility: 0.01, density: allPolygons[idx]?.density || 1000 });
        }
        allPolygons[currentPolyIndex] = { points: [], susceptibility: 0.01, density: allPolygons[idx]?.density || 1000  };
        redraw();
        updatePointsList();
        updateSusceptibilityDisplay();
        updatePolygonsList();
    });
}

function clearCurrentPolygon() {
    if (currentPolyIndex < 0) return;
    points = [];
    if (currentPolyIndex < allPolygons.length) allPolygons[currentPolyIndex] = { points: [], susceptibility: allPolygons[currentPolyIndex]?.susceptibility || 0.01, density: allPolygons[currentPolyIndex]?.density || 1000  };
    redraw();
    updatePointsList();
    sendPointsToPython();
}


function deletePoint(index) {
    if (currentPolyIndex < 0) return;
    points.splice(index, 1);
    if (currentPolyIndex < allPolygons.length) allPolygons[currentPolyIndex] = { points: points.slice(), susceptibility: allPolygons[currentPolyIndex]?.susceptibility || 0.01, density: allPolygons[currentPolyIndex]?.density || 1000 };
    redraw();
    updatePointsList();
    sendPointsToPython();
}

function loadPolygon() {
    eel.load_polygon_from_file()(function(success) {
        if (success) updatePolygonsList();
    });
}

function savePolygon() {
    eel.save_current_polygon()(function(success) {
        alert(success ? 'Saved successfully!' : 'Nothing to save.');
    });
}

function generateImage() {
    eel.generate_polygon_image()(function(dataUrl) {
        const cont = document.getElementById('image-container');
        const img = document.getElementById('polygon-image');
        if (dataUrl) {
            img.src = dataUrl;
            cont.style.display = 'block';
        } else {
            cont.style.display = 'none';
            alert('Need ≥ 3 points on current polygon.');
        }
    });
}

// ─── Init ───────────────────────────────────────────────────────────────────
function initialize() {
    allPolygons = [];
    eel.get_all_polygons_points()(function(pyAll) {
        js_update_all_polygons_points(pyAll);
        updatePolygonsList();
        if (allPolygons.length > 0) {
            switchToPolygon(0);
        } else {
            redraw();
        }
        redrawPlot();  // show default zero line
    });
}



eel.expose(js_update_all_polygons_points);
function js_update_all_polygons_points(pyAllPolygons) {
    // pyAllPolygons is list of list of [x,y]
    // Merge with existing susceptibility values
    allPolygons = pyAllPolygons.map((poly, idx) => ({
        points: poly.map(([x, y]) => ({ x: Number(x), y: Number(y) })),
        susceptibility: allPolygons[idx]?.susceptibility || 0.01,  // keep existing if present
	density: allPolygons[idx]?.density || 1000 
    }));
    redraw();
}

eel.expose(js_update_polygon_points);
function js_update_polygon_points(pyPoints) {
    // Your code here (update the active polygon's points)
    points = pyPoints.map(([x, y]) => ({ x: Number(x), y: Number(y) }));
    if (currentPolyIndex >= 0 && currentPolyIndex < allPolygons.length) {
        allPolygons[currentPolyIndex].points = points.slice();
    }
    redraw();
    updatePointsList();
}

eel.expose(js_update_polygons_list);
function js_update_polygons_list() {
    updatePolygonsList();
}



function updateDensityDisplay() {
    const denInput = document.getElementById('density');
    if (!denInput) return;
    if (currentPolyIndex >= 0 && allPolygons[currentPolyIndex]) {
        const den = allPolygons[currentPolyIndex].density || 1000;
        denInput.value = den.toFixed(0);
    } else {
        denInput.value = "1000";
    }
}

eel.expose(js_set_current_polygon);
function js_set_current_polygon(index) {
    currentPolyIndex = parseInt(index);
    points = allPolygons[currentPolyIndex]?.points || [];
    updateSusceptibilityDisplay();
    updateDensityDisplay();  // ← add this
    updatePolygonsList();
    redraw();
}

// Change listener
document.getElementById('density')?.addEventListener('change', (e) => {
    const newDen = parseFloat(e.target.value);
    if (isNaN(newDen) || newDen < 0) {
        alert("Density must be a positive number.");
        e.target.value = allPolygons[currentPolyIndex]?.density || 1000;
        return;
    }
    if (currentPolyIndex >= 0 && allPolygons[currentPolyIndex]) {
        allPolygons[currentPolyIndex].density = newDen;
        console.log(`Updated density for polygon ${currentPolyIndex + 1} to ${newDen}`);
    }
});

// Load current polygon's susceptibility into the textbox
function updateSusceptibilityDisplay() {
    const susInput = document.getElementById('susceptibility');
    if (!susInput) return;

    if (currentPolyIndex >= 0 && allPolygons[currentPolyIndex]) {
        const sus = allPolygons[currentPolyIndex].susceptibility || 0.01;
        susInput.value = sus.toFixed(4);  // show 4 decimals
    } else {
        susInput.value = "0.01";
    }
}

document.getElementById('showLoaded')?.addEventListener('change', (e) => {
    showLoaded = e.target.checked;
    redrawPlot();
});
document.getElementById('showModelled')?.addEventListener('change', (e) => {
    showModelled = e.target.checked;
    redrawPlot();
});

// When user changes the textbox → update active polygon only
document.getElementById('susceptibility')?.addEventListener('change', (e) => {
    const newSus = parseFloat(e.target.value);
    if (isNaN(newSus) || newSus < 0) {
        alert("Susceptibility must be a positive number.");
        e.target.value = allPolygons[currentPolyIndex]?.susceptibility?.toFixed(4) || "0.01";
        return;
    }

    if (currentPolyIndex >= 0 && allPolygons[currentPolyIndex]) {
        allPolygons[currentPolyIndex].susceptibility = newSus;
        console.log(`Updated susceptibility for polygon ${currentPolyIndex + 1} to ${newSus}`);
    }
});

document.getElementById('backgroundDensity')?.addEventListener('change', (e) => {
    const newBg = parseFloat(e.target.value);
    if (isNaN(newBg)) {
        alert("Invalid background density.");
        e.target.value = backgroundDensity;
        return;
    }
    backgroundDensity = newBg;
});

initialize();
