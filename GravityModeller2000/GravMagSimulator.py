# main.py
import eel
import io
import base64
import matplotlib.pyplot as plt
import csv
import numpy as np
from tkinter import Tk, filedialog
from PIL import Image

class Jind:
    def __init__(self, mod, Ideg, Ddeg):
        self.mod = mod
        self.Ideg = Ideg
        self.Ddeg = Ddeg

class Jrem:
    def __init__(self, mod, Ideg, Ddeg):
        self.mod = mod
        self.Ideg = Ideg
        self.Ddeg = Ddeg

class Body:
    def __init__(self, ver1, ver2, nsegm):
        self.ver1 = ver1
        self.ver2 = ver2
        self.nsegm = nsegm

class Bodies:
    def __init__(self):
        self.bo = []

def magcomp(Jmod, I, D, Jrmod, Ir, Dr, C):
    Jx = Jmod * np.cos(I) * np.cos(D - C) + Jrmod * np.cos(Ir) * np.cos(Dr - C)
    Jy = Jmod * np.cos(I) * np.sin(D - C) + Jrmod * np.cos(Ir) * np.sin(Dr - C)
    Jz = Jmod * np.sin(I) + Jrmod * np.sin(Ir)
    return Jx, Jy, Jz

def convert_H_to_B_nT(tsum):
    #return tsum*np.pi *200
    return tsum * 4 * np.pi * 100  # Simplified conversion from A/m to nT

def _arccotangent(theta):
    if theta == 0.0:
        return np.pi / 2
    return np.arctan2(1, theta)

def checkanticlockwiseorder(body):
    encarea2 = 0.0
    for ise in range(body.nsegm):
        x1 = body.ver1[ise, 0]
        z1 = body.ver1[ise, 1] 
        x2 = body.ver2[ise, 0]
        z2 = body.ver2[ise, 1]
        encarea2 += (x2 - x1) * (z2 + z1)

    return encarea2 < 0.0

def tmagtalwani(x1, z1, x2, z2, Jx, Jz, Iind, Dind, C):
    eps = np.finfo(np.float64).eps
    small = 1e4 * eps
    anglelim = 0.995 * np.pi

    x21 = x2 - x1
    z21 = z2 - z1
    s = np.sqrt(x21**2 + z21**2)

    if s < small:
        return 0.0
    
    theta1 = np.arctan2(z1, x1)
    theta2 = np.arctan2(z2, x2)

    if z21 != 0.0 :
        g = -x21/z21
    else :
        return 0.0

    #g = -x21 / z21
    phi = _arccotangent(g)



    thetadiff = theta2 - theta1
    if thetadiff < -np.pi:
        thetadiff += 2.0 * np.pi
    elif thetadiff > np.pi:
        thetadiff -= 2.0 * np.pi

    if (abs(x1) < small and abs(z1) < small) or (abs(x2) < small and abs(z2) < small):
        warnings.warn("Corner too close to observation point")

    if abs(thetadiff) > anglelim:
        warnings.warn("Polygon side too close to observation point")

    r1 = np.sqrt(x1**2 + z1**2)
    r2 = np.sqrt(x2**2 + z2**2)

    flog = np.log(r2)-np.log(r1
                             )
    V = 2.0 * np.sin(phi) * (Jx * (thetadiff * np.cos(phi) + np.sin(phi) * flog) - Jz * (thetadiff * np.sin(phi) - np.cos(phi) * flog))

    H = 2.0 * np.sin(phi) * (Jx * (thetadiff * np.sin(phi) - np.cos(phi) * flog) + Jz * (thetadiff * np.cos(phi) + np.sin(phi) * flog))

    #sign_v = 1.0 if Iind >= 0 else -1.0
    totfield = (-1.0 / (4.0 * np.pi)) * (H * np.cos(Iind) * np.cos(C - Dind) + V*np.sin(Iind))
    
    return totfield

def checkanticlockwiseorder(body):
    encarea2 = 0.0
    for ise in range(body.nsegm):
        x1 = body.ver1[ise, 0]
        z1 = body.ver1[ise, 1] 
        x2 = body.ver2[ise, 0]
        z2 = body.ver2[ise, 1]
        encarea2 += (x2 - x1) * (z2 + z1)
    return encarea2 < 0.0

def tmagpoly2Dgen(xzobs, Jind, Jrem, northxax, body, forwardtype="talwani"):
    if forwardtype not in ["talwani", "talwani_red", "krav", "wonbev"]:
        raise ValueError("Invalid forwardtype")

    anticlockw = checkanticlockwiseorder(body)
    if not anticlockw:
        raise ValueError("Vertices not ordered anticlockwise.")

    Cnorth = np.deg2rad(northxax)
    Iind = np.deg2rad(Jind.Ideg)
    Dind = np.deg2rad(Jind.Ddeg)
    Irem = np.deg2rad(Jrem.Ideg)
    Drem = np.deg2rad(Jrem.Ddeg)

    Jtotx, Jtoty, Jtotz = magcomp(Jind.mod, Iind, Dind, Jrem.mod, Irem, Drem, Cnorth)

    nobs = xzobs.shape[0]
    totfield = np.zeros(nobs)

    for iob in range(nobs):
        xo = xzobs[iob, 0]
        zo = xzobs[iob, 1]

        tsum = 0.0
        for ise in range(body.nsegm):
            x1 = body.ver1[ise, 0] - xo
            z1 = body.ver1[ise, 1] - zo
            x2 = body.ver2[ise, 0] - xo
            z2 = body.ver2[ise, 1] - zo

            tsum += tmagtalwani(x1, z1, x2, z2, Jtotx, Jtotz, Iind, Dind, Cnorth)  # Use talwani for now

        totfield[iob] = convert_H_to_B_nT(tsum)

    return totfield

def tmagpolybodies2D(xzobs, Jinds, Jrems, northxax, bodies):
    """
    Compute total magnetic anomaly from multiple 2D polygonal bodies.
    Calls tmagpoly2Dgen for each body and sums the contributions.
    """
    if len(bodies.bo) != len(Jinds) or len(bodies.bo) != len(Jrems):
        raise ValueError("Number of bodies, Jinds, and Jrems must match")

    total_field = np.zeros(xzobs.shape[0])

    for i, body in enumerate(bodies.bo):
        print(f"  - Computing for body {i+1}/{len(bodies.bo)}")
        field_i = tmagpoly2Dgen(
            xzobs,
            Jinds[i],
            Jrems[i],
            northxax,
            body,
            forwardtype="talwani"  # or "talwani_red", "krav", "wonbev" if implemented
        )
        total_field += field_i

    return total_field


@eel.expose
def calculate_magnetic_response(polygons_data, inc, dec, orientation, bounds_js):
    print("Python: calculate_magnetic_response called")
    print(f"  - Polygons count: {len(polygons_data)}")    
    print(f"  - inc={inc}, dec={dec}, orient={orientation}")
    print(f"  - Received bounds: {bounds_js}")
    Hscale = (57000e-9)/(4*np.pi*1e-7)
    try:
        bodies = Bodies()
        j_inds = []
        j_rems = []
        for i, poly_data in enumerate(polygons_data):
            poly = poly_data['points']
            sus = poly_data['susceptibility']*Hscale # or 0.01
            print(f"  - Polygon {i+1}: {len(poly)} points, sus={sus}")

            if len(poly) < 3:
                continue

            poly_points = [[float(p['x']), float(p['y'])] for p in poly]
            nsegm = len(poly_points)
            ver1 = np.array(poly_points, dtype=float)
            # Check winding
            anticlockw = checkanticlockwiseorder(Body(ver1, np.roll(ver1, -1, axis=0), len(ver1)))

            if not anticlockw:
                print(f"Polygon {i+1} is clockwise — automatically reversing vertices")
                ver1 = np.flip(ver1, axis=0)  # reverse order
            ver2 = np.roll(ver1, -1, axis=0)
            body = Body(ver1, ver2, nsegm)
            bodies.bo.append(body)

            j_ind = Jind(sus, inc, dec)
            j_rem = Jrem(0.0, 0.0, 0.0)
            j_inds.append(j_ind)
            j_rems.append(j_rem)

        if not bodies.bo:
            return []

        xmin = bounds_js['xmin']
        xmax = bounds_js['xmax']
        #xobs = np.arange(xmin - 500, xmax + 500, 10)
        ext = 0.1 * (xmax - xmin)
        xobs = np.linspace(xmin - ext, xmax + ext,200)

        zobs = np.zeros_like(xobs)
        xzobs = np.column_stack((xobs, zobs))

        response = tmagpolybodies2D(xzobs, j_inds, j_rems, orientation, bodies)
        return list(zip(xobs.tolist(), response.tolist()))

    except Exception as e:
        print("Calculation error:", str(e))
        import traceback
        traceback.print_exc()
        return []


@eel.expose
def calculate_gravity_response(polygons_data, background_density, bounds_js):
    G = 6.6743e-11  # Gravitational constant, m³ kg⁻¹ s⁻²
    bodies = Bodies()
    for i, poly_data in enumerate(polygons_data):
        poly = poly_data['points']
        density = poly_data.get('density', 1000.0)
        relative_density = density - background_density
        if len(poly) < 3:
            continue

        poly_points = [[float(p['x']), float(p['y'])] for p in poly]

        ver1 = np.array(poly_points, dtype=float)

        # Check anticlockwise order (error if not, as in Julia code)
        encarea2 = 0.0
        n = len(ver1)
        for j in range(n):
            x1, z1 = ver1[j]
            x2, z2 = ver1[(j + 1) % n]
            encarea2 += (x2 - x1) * (z2 + z1)
        if encarea2 > 0:
            print(f"Polygon {i+1} is clockwise — automatically reversing vertices")
            ver1 = np.flip(ver1, axis=0
                           )
        ver2 = np.roll(ver1, -1, axis=0)
        body = Body(ver1, ver2, len(ver1))
        bodies.bo.append(body)

    if not bodies.bo:
        return []

    xmin = bounds_js['xmin']
    xmax = bounds_js['xmax']
    # Symmetric extension
    ext = 0.1 * (xmax - xmin)
    #xobs = np.arange(xmin - ext, xmax + ext, 5)  # finer step
    xobs = np.linspace(xmin - ext, xmax + ext,200)
    zobs = np.zeros_like(xobs)
    xzobs = np.column_stack((xobs, zobs))

    response = np.zeros(len(xobs))
    for i in range(len(xobs)):
       for body in bodies.bo:
            xo = xobs[i]
            zo = zobs[i]
            gsum = 0.0
            for j in range(body.nsegm):
                x1 = body.ver1[j, 0] - xo
                z1 = body.ver1[j, 1] - zo
                x2 = body.ver2[j, 0] - xo
                z2 = body.ver2[j, 1] - zo
                gsum += gravtalwani(x1, z1, x2, z2, relative_density)

            response[i] += gsum

    return list(zip(xobs.tolist(), response.tolist()))

def gravtalwani(x1, z1, x2, z2, rho):
    small = 1e4 * np.finfo(float).eps
    anglelim = 0.995 * np.pi
    G = 6.6743e-11

    # Error if corner too close (slightly move away)
    if abs(x1) < small and abs(z1) < small:
        x1 = np.sign(x1) * small if x1 != 0 else small
        z1 = np.sign(z1) * small if z1 != 0 else small
        print("Warning: Corner too close to observation point (calculation continues)")

    if abs(x2) < small and abs(z2) < small:
        x2 = np.sign(x2) * small if x2 != 0 else small
        z2 = np.sign(z2) * small if z2 != 0 else small
        print("Warning: Corner too close to observation point (calculation continues)")

    denom = z2 - z1
    if denom == 0.0:
        denom = small

    r1sq = x1**2 + z1**2
    r2sq = x2**2 + z2**2

    θdiff = np.arctan2(z2, x2) - np.arctan2(z1, x1)

    # Handle crossing x-axis
    if np.sign(z1) != np.sign(z2):
        test = x1 * z2 - x2 * z1
        if test > 0.0:
            if z1 >= 0.0:
                θdiff = θdiff + 2 * np.pi
        elif test < 0.0:
            if z2 >= 0.0:
                θdiff = θdiff + 2 * np.pi

    # Error if side too close
    if abs(θdiff) > anglelim:
        print("Warning: Polygon side too close to observation point (calculation continues)")

    alpha = (x2 - x1) / denom
    beta = (x1 * z2 - x2 * z1) / denom
    term1 = beta / (1.0 + alpha**2)
    term2 = 0.5 * (np.log(r2sq) - np.log(r1sq))

    eq = term1 * (term2 - alpha * θdiff)

    # Factor for mGal (negative for Julia convention? -2e5 * rho * G)
    factor = 2.0 * 1e5 * rho * G  # adjust sign if needed for positive downward

    g = factor * eq

    return g

eel.init('web')

# Globals
polygons = []                   # list of list of [x, y]
current_poly_index = -1

root = Tk()

root.attributes('-topmost', True)   # helps macOS bring dialog forward
root.attributes('-topmost', False)
root.withdraw()

@eel.expose
def get_current_poly_index():
    return current_poly_index

@eel.expose
def add_new_polygon():
    global polygons, current_poly_index
    polygons.append([])  # empty polygon
    current_poly_index = len(polygons) - 1
    eel.js_update_polygons_list()
    eel.js_set_current_polygon(current_poly_index)
    #eel.js_update_all_polygons_points(polygons)  # NEW: send all immediately
    return None

@eel.expose
def set_current_polygon(index):
    global current_poly_index
    try:
        idx = int(index)
        if 0 <= idx < len(polygons):
            current_poly_index = idx
            points_to_send = polygons[current_poly_index]
            eel.js_update_polygon_points(points_to_send)
            eel.js_update_all_polygons_points(polygons)  # NEW: send all for drawing
            eel.js_set_current_polygon(current_poly_index)
            return True
    except Exception as e:
        print(f"Error setting polygon: {e}")
    return False

@eel.expose
def update_polygon_from_js(js_points):
    global polygons, current_poly_index
    if current_poly_index >= 0 and current_poly_index < len(polygons):
        polygons[current_poly_index] = js_points[:]  # copy
        eel.js_update_all_polygons_points(polygons)  # NEW: sync all after update
        return True
    return False

@eel.expose
def get_all_polygons_summary():
    return [
        {"index": i, "point_count": len(p), "name": f"Polygon {i+1}"}
        for i, p in enumerate(polygons)
    ]

# NEW: Expose full points for all polygons (for JS drawing)
@eel.expose
def get_all_polygons_points():
    return polygons

@eel.expose
def load_polygon_from_file():
    global polygons, current_poly_index
    try:
        file_path = filedialog.askopenfilename(
            title="Select CSV polygon file",
            filetypes=[("CSV files", "*.csv")]
        )
        root.update()  # Force Tkinter event processing (anti-freeze on Mac)
    except Exception as e:
        print(f"Dialog error: {e}")
        return False

    if not file_path:
        return False

    points = []
    with open(file_path, 'r') as f:
        reader = csv.reader(f)
        header = next(reader, None)  # skip if header
        for row in reader:
            if len(row) >= 2:
                try:
                    points.append([float(row[0]), float(row[1])])
                except ValueError:
                    pass

    if points:
        polygons.append(points)
        current_poly_index = len(polygons) - 1
        eel.js_update_polygons_list()
        eel.js_set_current_polygon(current_poly_index)
        eel.js_update_all_polygons_points(polygons)  # NEW: send all
        return True
    return False

@eel.expose
def save_current_polygon():
    global polygons, current_poly_index
    if not (0 <= current_poly_index < len(polygons)):
        return False
    points = polygons[current_poly_index]
    if not points:
        return False

    try:
        file_path = filedialog.asksaveasfilename(
            title="Save polygon CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")]
        )
        root.update()  # Anti-freeze
        if not file_path:
            return False

        with open(file_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['x', 'y'])
            writer.writerows(points)
        return True
    except Exception as e:
        print(f"Save error: {e}")
        return False

@eel.expose
def generate_polygon_image():
    global polygons, current_poly_index
    if not (0 <= current_poly_index < len(polygons)):
        return None
    points = polygons[current_poly_index]
    if len(points) < 3:
        return None

    pts = np.array(points)
    x, y = pts[:, 0], pts[:, 1]

    fig, ax = plt.subplots(figsize=(6, 4))
    ax.plot(np.append(x, x[0]), np.append(y, y[0]), 'b-', lw=2)
    ax.plot(x, y, 'ro', ms=6)
    ax.set_aspect('equal')
    ax.grid(True, alpha=0.3)
    ax.set_title(f"Polygon {current_poly_index + 1}")

    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=120, bbox_inches='tight')
    buf.seek(0)
    img_base64 = base64.b64encode(buf.read()).decode('utf-8')
    plt.close(fig)
    return f"data:image/png;base64,{img_base64}"

@eel.expose
def load_plot_csv():
    global root
    print("Python: load_plot_csv started")   # ← debug

    try:
        file_path = filedialog.askopenfilename(
            title="Select CSV for reference plot (x,y in meters)",
            filetypes=[("CSV files", "*.csv")]
        )
        root.update()           # important on macOS
        root.update_idletasks() # extra force refresh
    except Exception as e:
        print(f"Dialog error: {e}")
        return {"success": False, "error": str(e)}

    if not file_path:
        print("Python: No file selected")
        return {"success": False}


    # TEMP: ignore real file, return tiny test data
    #dummy_points = [[0.0, 0.0], [777.0, 50.0], [1554.0, 0.0]]
    #print("Python: Returning dummy 3 points")
    #return {"success": True, "points": dummy_points}
    
    
    points = []
    try:
        with open(file_path, 'r', encoding='utf-8') as f:   # explicit encoding helps
            reader = csv.reader(f)
            header = next(reader, None)  # skip header safely
            row_count = 0
            for row in reader:
                row_count += 1
                if row_count > 50000:                    # safety limit
                    print("Warning: CSV too large, stopping at 50k rows")
                    break
                if len(row) >= 2:
                    try:
                        x = float(row[0])
                        y = float(row[1])
                        points.append([x, y])
                    except (ValueError, IndexError):
                        continue
        print(f"Python: Loaded {len(points)} points")
        return {"success": True, "points": points}
    except Exception as e:
        print(f"CSV read error: {e}")
        return {"success": False, "error": str(e)}
    
if __name__ == '__main__':
    eel.start('index.html', size=(1000, 750))

