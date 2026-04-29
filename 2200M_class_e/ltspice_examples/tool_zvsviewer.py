"""
LTspice .asc Schematic Viewer + PySpice / ngspice Simulation  –  Pygame
------------------------------------------------------------------------
Screen 1 : File browser – lists *.asc files in the working directory.
Screen 2 : Schematic view
   • Wires drawn and live-coloured by net voltage (when sim runs)
   • Junction dots auto-detected at T / cross intersections
   • Net labels (FLAG) rendered at named nodes
   • Each component drawn as a colour-coded rectangle (name + value inside)
   • Net-map built from wire connectivity (union-find)
   • PySpice + ngspice used for simulation – same pattern as your example code
   • Click any wire / node to probe waveform in the mini panel
   Controls: scroll=zoom · drag/WASD/arrows=pan · ESC=back
"""

import os, re, sys, math
from collections import defaultdict
import pygame

try:
    import PySpice.Logging.Logging as Logging
    from PySpice.Spice.Netlist import Circuit
    from PySpice.Unit import *
    Logging.setup_logging()
    PYSPICE_OK = True
except ImportError:
    PYSPICE_OK = False


BG          = (15,  17,  23)
PANEL       = (24,  27,  36)
BORDER      = (45,  50,  68)
ACCENT      = (82, 175, 255)
ACCENT2     = (255, 165,  60)
TEXT_MAIN   = (220, 225, 235)
TEXT_DIM    = (110, 118, 140)
TEXT_BRIGHT = (255, 255, 255)
SEL_BG      = (30,  60, 100)
SEL_BORDER  = (82, 175, 255)
GRID_COL    = (25,  28,  38)
WIRE_BASE   = (60,  200,  90)
JUNCTION_C  = (255, 220,  50)
FLAG_C      = (120, 220, 255)

TYPE_PALETTE = {
    "res":     ((40,  90, 160), (100, 180, 255)),
    "cap":     ((30, 110,  70), ( 80, 210, 130)),
    "ind":     ((110, 50, 140), (200, 120, 240)),
    "nmos":    ((140, 80,  20), (255, 160,  60)),
    "pmos":    ((120, 60,  20), (220, 130,  50)),
    "voltage": ((130, 30,  50), (240,  80, 100)),
    "current": ((100, 30,  80), (200,  80, 180)),
    "diode":   (( 30, 90, 110), ( 70, 190, 220)),
    "default": (( 50, 50,  70), (130, 140, 170)),
}

W, H = 1280, 800
COMP_W, COMP_H = 144, 68
GRID = 16

_SUFFIXES = [
    ("meg", 1e6), ("k", 1e3), ("mil", 25.4e-6),
    ("µ", 1e-6),   # U+00B5 MICRO SIGN  (correct after latin-1 read)
    ("μ", 1e-6),   # U+03BC GREEK MU    (some editors)
    ("�", 1e-6),   # replacement char   (UTF-8 fallback safety)
    ("u",      1e-6),   # ASCII fallback
    ("n",  1e-9), ("p", 1e-12), ("f", 1e-15),
    ("m",  1e-3), ("g",  1e9),
]

def parse_value_float(s):
    """
    Convert an LTspice value string to a plain Python float.
    Returns float or None if unparseable.
    Examples: '800µ' → 8e-4, '29n' → 2.9e-8, '1k' → 1000, '72' → 72.0
    """
    s = s.strip().lower()
    for suffix, mult in _SUFFIXES:
        if s.endswith(suffix):
            try:
                return float(s[:-len(suffix)]) * mult
            except ValueError:
                return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_comp_value(raw_val):
    """
    Extract the numeric value from a component value string.
    Ignores trailing SPICE parameters like 'Rser=0.1', 'Ic=0', etc.
    e.g. '29.0n Rser=0.1' → 29e-9,  '800µ' → 8e-4,  '{ltank}' → None
    """
    if not raw_val:
        return None
    first_token = raw_val.strip().split()[0]
    return parse_value_float(first_token)

def eval_spice_expr(expr_str, context):
    """
    Evaluate a SPICE arithmetic expression.
    Handles SI suffixes (200u, 136k) and variable references.
    context: {name: float}
    """
    s = expr_str.strip()

    # Replace SI-suffixed numbers — meg before m to avoid partial match
    def replace_si(m):
        mults = {"meg":1e6,"k":1e3,"n":1e-9,"u":1e-6,"p":1e-12,
                 "f":1e-15,"m":1e-3,"g":1e9}
        try:
            return repr(float(m.group(1)) * mults.get(m.group(2).lower(), 1.0))
        except ValueError:
            return m.group(0)

    s = re.sub(r'(\d+\.?\d*(?:[eE][+-]?\d+)?)(meg|[kunpfmg])(?=[^a-zA-Z_]|$)',
               replace_si, s, flags=re.IGNORECASE)

    # Replace variable names, longest first to avoid partial matches
    for name in sorted(context.keys(), key=lambda x: -len(x)):
        s = re.sub(r'(?<![a-zA-Z_])' + re.escape(name) + r'(?![a-zA-Z_0-9])',
                   repr(float(context[name])), s)
    try:
        return float(eval(s, {"__builtins__": {}, "sqrt": math.sqrt,
                              "log10": math.log10, "log": math.log,
                              "abs": abs, "pow": pow}, {}))
    except Exception:
        return None


def resolve_dot_params(dot_params_raw, user_values):
    """
    Resolve .param dependency chain.
    dot_params_raw : {name: expr_str}  — from .param lines in .asc
    user_values    : {name: str}       — from param panel (may be empty)
    Returns        : {name: float}
    """
    context = {}

    # User panel values take priority
    for name, val_str in user_values.items():
        v = parse_value_float(val_str.strip()) if val_str.strip() else None
        if v is not None:
            context[name] = v

    # Iteratively resolve .param chain (up to 30 passes for deep deps)
    for _ in range(30):
        changed = False
        for name, expr in dot_params_raw.items():
            if name in context:
                continue
            expr_clean = expr.strip()
            if expr_clean.startswith('{') and expr_clean.endswith('}'):
                expr_clean = expr_clean[1:-1]
            v = parse_value_float(expr_clean)        # try plain SI first
            if v is None:
                v = eval_spice_expr(expr_clean, context)  # then arithmetic
            if v is not None:
                context[name] = v
                changed = True
        if not changed:
            break

    return context


def _float_to_si(v):
    if v == 0: return "0"
    av = abs(v)
    for suffix, mult in [("g",1e9),("meg",1e6),("k",1e3),
                         ("",1),("m",1e-3),("u",1e-6),
                         ("n",1e-9),("p",1e-12),("f",1e-15)]:
        if av >= mult * 0.9999:
            val = v / mult
            return f"{val:.6g}{suffix}"
    return repr(v)



def parse_pulse_params(val):
    """
    Parse 'PULSE(v1 v2 td tr tf pw per)' → dict of floats.
    """
    m = re.search(r'PULSE\s*\(([^)]+)\)', val, re.IGNORECASE)
    if not m:
        return None
    parts = m.group(1).split()
    keys = ['initial_value', 'pulsed_value', 'delay_time',
            'rise_time', 'fall_time', 'pulse_width', 'period']
    out = {}
    for k, p in zip(keys, parts):
        v = parse_value_float(p)
        if v is not None:
            out[k] = v
    return out


def parse_sin_params(val):
    """
    Parse 'SIN(vo va freq td df phase)' → dict of floats.
    """
    m = re.search(r'SIN\s*\(([^)]+)\)', val, re.IGNORECASE)
    if not m:
        return None
    parts = m.group(1).split()
    keys = ['offset', 'amplitude', 'frequency', 'delay',
            'damping_factor', 'phase']
    out = {}
    for k, p in zip(keys, parts):
        v = parse_value_float(p)
        if v is not None:
            out[k] = v
    return out


def parse_asc(path):
    try:
        with open(path, "r", encoding="latin-1") as fh:
            text = fh.read()
    except OSError as e:
        return [], [], [], None, {}, str(e)

    components, wires, flags = [], [], []
    current_comp = None
    tran_cmd  = None
    dot_params = {}          # ← new

    for raw in text.splitlines():
        line = raw.strip()

        m = re.match(r'^WIRE\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)', line)
        if m:
            wires.append((int(m.group(1)), int(m.group(2)),
                          int(m.group(3)), int(m.group(4))))
            continue

        m = re.match(r'^FLAG\s+(-?\d+)\s+(-?\d+)\s+(.+)', line)
        if m:
            flags.append({"x": int(m.group(1)), "y": int(m.group(2)),
                          "label": m.group(3).strip()})
            continue

        m = re.match(r'^SYMBOL\s+(\S+)\s+(-?\d+)\s+(-?\d+)\s+(\S+)', line)
        if m:
            if current_comp:
                components.append(current_comp)
            current_comp = {
                "type": m.group(1).lower(), "x": int(m.group(2)),
                "y": int(m.group(3)), "rot": m.group(4),
                "name": "", "value": "",
            }
            continue

        if current_comp:
            n = re.match(r'^SYMATTR\s+InstName\s+(.+)', line)
            if n:
                current_comp["name"] = n.group(1).strip(); continue
            v = re.match(r'^SYMATTR\s+Value\s+(.+)', line)
            if v:
                current_comp["value"] = v.group(1).strip(); continue

        # .tran
        if re.search(r'\.tran\b', line, re.IGNORECASE) and tran_cmd is None:
            m2 = re.search(r'\.tran[^\n\\]+', line, re.IGNORECASE)
            tran_cmd = m2.group(0).strip() if m2 else None

        # .param  — may be multiple on one TEXT line
        for pm in re.finditer(r'\.param\s+(\w+)\s*=\s*(\S+)', line, re.IGNORECASE):
            dot_params[pm.group(1)] = pm.group(2)

    if current_comp:
        components.append(current_comp)

    return components, wires, flags, tran_cmd, dot_params, None




def read_net_file(asc_path):
    """
    Read the LTspice-exported .net file alongside the .asc.
    Returns (dot_params, tran_cmd) — both more reliable than parsing .asc TEXT blocks.
    """
    net_path = os.path.splitext(asc_path)[0] + ".net"
    if not os.path.exists(net_path):
        return {}, None

    try:
        raw = open(net_path, "rb").read()
        text = None
        for enc in ["utf-16-le", "utf-16", "utf-16-be", "utf-8", "latin-1"]:
            try:
                decoded = raw.decode(enc)
                if any(k in decoded.lower() for k in [".param", ".tran", ".end"]):
                    text = decoded
                    break
            except Exception:
                continue
        if text is None:
            return {}, None
    except OSError:
        return {}, None

    dot_params = {}
    tran_cmd   = None

    for line in text.splitlines():
        line = line.strip()

        # collect every .param name=value on its own line
        for m in re.finditer(r'\.param\s+(\w+)\s*=\s*([^\s\\]+)', line, re.IGNORECASE):
            dot_params[m.group(1)] = m.group(2)

        # grab .tran — prefer the one from the .net file
        if re.match(r'\.tran\b', line, re.IGNORECASE) and tran_cmd is None:
            tran_cmd = line.strip()

    return dot_params, tran_cmd





def extract_param_names(components, tran_cmd=None, dot_params=None):
    pattern = re.compile(r'\{([A-Za-z_]\w*)\}')
    found = set()
    for comp in components:
        found.update(pattern.findall(comp.get("value", "")))
    if tran_cmd:
        found.update(pattern.findall(tran_cmd))
    # also expose any .param that appears in another .param's expression
    # so the user can override e.g. Ff or rload directly
    if dot_params:
        found.update(dot_params.keys())
    return sorted(found)


def substitute_params(components, param_values):
    """
    Return a shallow copy of components with every {name} in 'value'
    replaced by the user-supplied string from param_values.
    Components whose value contains an unresolved {name} are left as-is
    (simulation will catch the error naturally).
    """
    result = []
    for comp in components:
        c = dict(comp)          # shallow copy — don't mutate original
        val = c.get("value", "")
        for name, text in param_values.items():
            if text.strip():    # only substitute non-empty entries
                val = val.replace(f"{{{name}}}", text.strip())
        c["value"] = val
        result.append(c)
    return result



class UnionFind:
    def __init__(self):
        self._p = {}

    def _make(self, k):
        self._p.setdefault(k, k)

    def find(self, k):
        self._make(k)
        while self._p[k] != k:
            self._p[k] = self._p[self._p[k]]
            k = self._p[k]
        return k

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._p[rb] = ra

    def groups(self):
        g = defaultdict(set)
        for k in self._p:
            g[self.find(k)].add(k)
        return dict(g)


def _on_interior(px, py, x1, y1, x2, y2):
    if (px, py) in ((x1, y1), (x2, y2)):
        return False
    if x1 == x2 == px:
        return min(y1, y2) < py < max(y1, y2)
    if y1 == y2 == py:
        return min(x1, x2) < px < max(x1, x2)
    return False


def build_net_map(wires, flags):
    uf = UnionFind()
    for x1, y1, x2, y2 in wires:
        uf.union((x1, y1), (x2, y2))

    all_pts = set()
    for x1, y1, x2, y2 in wires:
        all_pts.add((x1, y1)); all_pts.add((x2, y2))

    # merge T-junctions
    for px, py in all_pts:
        for x1, y1, x2, y2 in wires:
            if _on_interior(px, py, x1, y1, x2, y2):
                uf.union((px, py), (x1, y1))

    # junction dots: points touched by 3+ wire ends / interiors
    touch = defaultdict(int)
    for x1, y1, x2, y2 in wires:
        touch[(x1, y1)] += 1; touch[(x2, y2)] += 1
    for px, py in all_pts:
        for x1, y1, x2, y2 in wires:
            if _on_interior(px, py, x1, y1, x2, y2):
                touch[(px, py)] += 1
    junctions = [pt for pt, c in touch.items() if c >= 3]

    # label nets from flags
    groups = uf.groups()
    root_label = {}
    for flag in flags:
        pt = (flag["x"], flag["y"])
        uf._make(pt)
        root_label[uf.find(pt)] = flag["label"]

    net_id = 1
    root_net = {}
    for root in groups:
        if root in root_label:
            root_net[root] = root_label[root]
        else:
            root_net[root] = f"N{net_id:03d}"; net_id += 1

    point_to_net = {}
    net_to_points = defaultdict(set)
    for root, pts in groups.items():
        name = root_net.get(root, "N???")
        for pt in pts:
            point_to_net[pt] = name
            net_to_points[name].add(pt)

    return point_to_net, dict(net_to_points), junctions


# ── Pin offsets are relative to the symbol origin in R0 orientation.
# These match the actual LTspice .asy symbol geometry (measured in schematic units).
#
# Offsets from actual LTspice .asy symbol files (multiples of 16-unit grid):
#   res/cap:          PIN 0 ±80
#   ind:              PIN 0 ±96  (longer body)
#   voltage/current:  PIN 0 ±80
#   nmos/pmos:        G at (±48, 0)  D/S at (0, ±96)
#   diode/bjt:        ±80
SYMBOL_PINS = {
    "res":     [("1",  0, -80), ("2",  0,  80)],
    "cap":     [("1",  0, -80), ("2",  0,  80)],
    "ind":     [("1",  0, -96), ("2",  0,  96)],
    "voltage": [("P",  0, -80), ("N",  0,  80)],
    "current": [("P",  0, -80), ("N",  0,  80)],
    "nmos":    [("D", 48, -96), ("G", -48,  0), ("S", 48,  96)],
    "pmos":    [("D", 48,  96), ("G", -48,  0), ("S", 48, -96)],
    "diode":   [("A",  0, -80), ("K",  0,  80)],
    "zener":   [("A",  0, -80), ("K",  0,  80)],
    "npn":     [("C",  0, -80), ("B", -48,  0), ("E",  0,  80)],
    "pnp":     [("C",  0,  80), ("B", -48,  0), ("E",  0, -80)],
}
ROT_MAT = {
    "R0":   ( 1, 0, 0, 1), "R90":  ( 0,-1, 1, 0),
    "R180": (-1, 0, 0,-1), "R270": ( 0, 1,-1, 0),
    "M0":   (-1, 0, 0, 1), "M90":  ( 0, 1, 1, 0),
    "M180": ( 1, 0, 0,-1), "M270": ( 0,-1,-1, 0),
}

def rotate_pin(dx, dy, rot):
    a, b, c, d = ROT_MAT.get(rot, (1,0,0,1))
    return a*dx+b*dy, c*dx+d*dy

def component_pin_positions(comp):
    ctype = comp["type"]
    for key in SYMBOL_PINS:
        if ctype.startswith(key):
            return [(pn,
                     comp["x"] + rotate_pin(dx, dy, comp["rot"])[0],
                     comp["y"] + rotate_pin(dx, dy, comp["rot"])[1])
                    for pn, dx, dy in SYMBOL_PINS[key]]
    return [("?", comp["x"], comp["y"])]


def _net_from_wire_interior(px, py, wires, point_to_net, tol=16):
    """
    If (px,py) lies on or very near the interior of any wire segment,
    return that segment's net name.  Used for MOSFET gate pins that
    connect to the side of a wire rather than at an endpoint.
    tol: perpendicular distance tolerance in schematic units.
    """
    for x1, y1, x2, y2 in wires:
        net = point_to_net.get((x1,y1)) or point_to_net.get((x2,y2))
        if net is None:
            continue
        if x1 == x2:  # vertical wire
            if abs(px - x1) <= tol and min(y1,y2) - tol <= py <= max(y1,y2) + tol:
                return net, x1, py, abs(px - x1)
        elif y1 == y2:  # horizontal wire
            if abs(py - y1) <= tol and min(x1,x2) - tol <= px <= max(x1,x2) + tol:
                return net, px, y1, abs(py - y1)
    return None, None, None, None


def assign_component_nets(components, point_to_net, wires=None, snap=64):
    """
    Map each component pin to a net name.

    Two-pass lookup per pin:
      Pass 1 – wire ENDPOINTS within `snap` (Manhattan distance).
               Tight snap (40 units = 2.5 grid steps) avoids bridging
               gaps between intentionally disconnected segments.
      Pass 2 – wire SEGMENT INTERIORS within 16 units perpendicular.
               Needed for MOSFET gates / BJT bases that T-connect to
               a wire rather than landing on an endpoint.

    Greedy anti-collision: two pins of the same component cannot claim
    the same wire point (prevents phantom shorts on voltage sources).

    Returns list of (comp, {pin_name: net_name_or_None}).
    Also attaches comp["_pin_debug"] for the diagnostics panel.
    """
    wire_pts = list(point_to_net.items())
    wires    = wires or []

    result = []
    for comp in components:
        pins = component_pin_positions(comp)

        # ── Pass 1: endpoint
        candidates = {}
        for pname, px, py in pins:
            hits = []
            for (wx, wy), net in wire_pts:
                d = abs(px-wx) + abs(py-wy)
                if d <= snap:
                    hits.append((d, net, wx, wy))
            hits.sort()
            candidates[pname] = hits

        # greedy assign from endpoints
        pin_nets   = {}
        used_pts   = set()
        debug_info = {}
        for pname, px, py in pins:
            for d, net, wx, wy in candidates[pname]:
                if (wx, wy) not in used_pts:
                    pin_nets[pname]   = net
                    used_pts.add((wx, wy))
                    debug_info[pname] = (px, py, d, net, wx, wy)
                    break

        # ── Pass 2: interior snap
        for pname, px, py in pins:
            if pin_nets.get(pname) is not None:
                continue
            net, hx, hy, d = _net_from_wire_interior(px, py, wires, point_to_net)
            if net is not None:
                pin_nets[pname]   = net
                debug_info[pname] = (px, py, d, net, hx, hy)
            else:
                pin_nets[pname]   = None
                debug_info[pname] = (px, py, -1, None, None, None)

        comp["_pin_debug"] = debug_info
        result.append((comp, pin_nets))
    return result


def validate_assignments(pin_assignments):
    """
    Check for common connectivity errors before passing to PySpice.
    Returns list of (severity, comp_name, message) tuples.
      severity: "ERROR" | "WARN"
    """
    issues = []
    for comp, nets in pin_assignments:
        name  = comp.get("name", "?")
        ctype = comp["type"]

        # unconnected pins
        for pin, net in nets.items():
            if net is None:
                issues.append(("WARN", name,
                    f"pin {pin} has no wire endpoint within snap distance – "
                    f"check pin offset table or schematic connection"))

        # shorted two-terminal sources
        if ctype.startswith("voltage") or ctype.startswith("current"):
            p, n = nets.get("P"), nets.get("N")
            if p is not None and n is not None and p == n:
                dbg = comp.get("_pin_debug", {})
                p_info = dbg.get("P", ())
                n_info = dbg.get("N", ())
                issues.append(("ERROR", name,
                    f"P and N both map to net '{p}'  "
                    f"(P snapped to {p_info[4:6] if len(p_info)>5 else '?'} dist={p_info[2] if len(p_info)>2 else '?'}, "
                    f"N snapped to {n_info[4:6] if len(n_info)>5 else '?'} dist={n_info[2] if len(n_info)>2 else '?'})"))

        # floating source
        if ctype.startswith("voltage") or ctype.startswith("current"):
            if nets.get("P") is None or nets.get("N") is None:
                issues.append(("ERROR", name,
                    f"source has a floating terminal – sim will fail"))

    return issues



def _net_to_pyspice(net_name, circuit):
    """Map net name to PySpice node – '0' becomes circuit.gnd."""
    return circuit.gnd if net_name == "0" else net_name


def _infer_tran(tran_cmd, components):
    """
    Derive (step_time, end_time) for the transient sim.

    Priority:
      1. Parse literal numbers from .tran directive, ignoring {expr} tokens.
         LTspice format: .tran [tstep] tstop [tstart [tmax]]
         We skip step=0 (means auto) and end=tiny.
      2. Auto-detect from PULSE voltage sources (use period * 50 cycles,
         step = rise_time / 10).
      3. Auto-detect from LC tank frequency sqrt(1/LC).
      4. Hard fallback: step=5ns, end=500µs.
    """
    # parse .tran numbers
    step_time = None
    end_time  = None

    if tran_cmd:
        # strip {expr} blocks first, then grab bare numbers with SI suffixes
        # Use word-boundary regex to avoid matching the leading '.' in '.tran'
        clean = re.sub(r'\{[^}]*\}', '', tran_cmd)
        nums  = re.findall(r'(?<![a-zA-Z\.])([\d]+\.?[\d]*(?:[eE][+\-]?\d+)?[munpkKMGf]?)', clean)
        parsed = [parse_value_float(n) for n in nums if parse_value_float(n) is not None]
        # .tran format: tstep  tstop  [tstart  [tmax]]
        # tstep is often 0 (auto). tstop is the key value we need.
        # After stripping {expr}, tstop may be gone – only tmax (small) remains.
        # Rule: treat any value < 1µs as a step/tmax hint, not a stop time.
        nonzero = [v for v in parsed if v > 0]
        big   = [v for v in nonzero if v >= 1e-6]   # tstop candidates
        small = [v for v in nonzero if v <  1e-6]   # step / tmax candidates
        if big:
            end_time  = big[0]
            if len(big) >= 2:
                step_time = big[0]; end_time = big[1]
        if step_time is None and small:
            step_time = small[-1]    # tmax is the last small param

    # pass 2: detect from PULSE source
    pulse_period = None
    pulse_rise   = None
    for comp in components:
        if comp["type"].startswith("voltage") and comp["value"].upper().startswith("PULSE"):
            pp = parse_pulse_params(comp["value"])
            if pp:
                pulse_period = pp.get("period")
                pulse_rise   = pp.get("rise_time") or pp.get("fall_time")
                break

    if end_time is None and pulse_period:
        # Class-E / resonant converters need many cycles to reach steady state
        # (choke L/R time constant dominates). 300 cycles ≈ 5× τ for most designs.
        end_time = pulse_period * 300     # 300 cycles
    if step_time is None and pulse_rise:
        step_time = pulse_rise / 10       # 10 points per edge

    # pass 3: detect from LC tank
    if step_time is None or end_time is None:
        inductors  = [parse_value_float(c["value"]) for c in components
                      if c["type"].startswith("ind")]
        capacitors = [parse_value_float(c["value"]) for c in components
                      if c["type"].startswith("cap")]
        inductors  = [v for v in inductors  if v and v > 0]
        capacitors = [v for v in capacitors if v and v > 0]
        if inductors and capacitors:
            import math as _math
            # use smallest L and C (tank elements, not choke)
            L = min(inductors); C = min(capacitors)
            f_tank = 1.0 / (2 * _math.pi * _math.sqrt(L * C))
            T      = 1.0 / f_tank
            if step_time is None: step_time = T / 100   # 100 pts per cycle
            if end_time  is None: end_time  = T * 30    # 30 cycles

    # pass 4: hard fallback
    if step_time is None: step_time = 5e-9     # 5 ns
    if end_time  is None: end_time  = 500e-6   # 500 µs

    # sanity caps: step never larger than end/100, end never > 10s
    step_time = min(step_time, end_time / 100)
    end_time  = min(end_time, 10.0)
    step_time = max(step_time, 1e-12)   # floor at 1 ps

    return step_time, end_time


# MOSFET params
# Level-1 saturation:  Id = (Kp/2)*(Vgs-Vto)^2   (W/L=1)
# Kp is back-calculated so Id ≈ rated current at Vgs=12V.
# Coss is injected as a separate explicit capacitor for clean convergence.
#
# Format: (Vto, Kp, Rd, Rs, Cgso, Cgdo, Coss)
_MOSFET_DB = {
    #              Vto    Kp      Rd       Rs      Cgso     Cgdo     Coss
    "irfp240": (  3.6,  0.567, 0.090,  0.020,  1.3e-9,  0.25e-9, 500e-12 ),
    "irf540":  (  3.2,  0.723, 0.040,  0.010,  1.7e-9,  0.40e-9, 350e-12 ),
    "irf3205": (  2.8,  2.599, 0.005,  0.001,  6.0e-9,  1.20e-9, 2000e-12),
    "fqp30n06":(  3.0,  0.741, 0.020,  0.005,  2.4e-9,  0.50e-9, 650e-12 ),
    "irfz44n": (  3.0,  1.852, 0.017,  0.004,  3.0e-9,  0.60e-9, 900e-12 ),
    "irf630":  (  3.5,  0.421, 0.080,  0.018,  1.1e-9,  0.22e-9, 200e-12 ),
    "irf740":  (  3.8,  0.284, 0.180,  0.040,  0.8e-9,  0.16e-9, 120e-12 ),
    "irf840":  (  4.0,  0.200, 0.280,  0.060,  0.7e-9,  0.14e-9, 100e-12 ),
    "irfp460": (  4.0,  0.261, 0.270,  0.060,  0.8e-9,  0.16e-9, 200e-12 ),
    "stp10nk60":(  4.0,  0.300, 0.600,  0.120,  0.6e-9,  0.12e-9,  80e-12 ),
}

# Tuple: (Vto, Kp, Rd_factor, Coss)
_MOSFET_BY_VOLTAGE = {
     30:  (2.5, 3.0,  0.003, 2000e-12),
     60:  (2.8, 0.8,  0.015,  600e-12),
    100:  (3.0, 0.6,  0.050,  300e-12),
    150:  (3.3, 0.5,  0.100,  180e-12),
    200:  (3.6, 0.4,  0.150,  120e-12),
    400:  (4.0, 0.2,  0.400,   70e-12),
    600:  (4.5, 0.1,  1.000,   40e-12),
    900:  (5.0, 0.05, 3.000,   20e-12),
}

def _mosfet_params(model_name, is_pmos=False):
    """
    Return (model_dict, coss_farads) for the given MOSFET part name.
    model_dict is ready to pass directly to circuit.model() for level=1.
    Coss is returned separately so the caller injects it as an explicit cap.
    """
    key = model_name.lower().strip()

    # 1. exact lookup
    if key in _MOSFET_DB:
        vto, kp, rd, rs, cgso, cgdo, coss = _MOSFET_DB[key]
    else:
        # 2. heuristic voltage rating from part number digits
        nums = re.findall(r"\d{2,}", key)
        v_rating = None
        if nums:
            candidates = [int(n) for n in nums if 20 <= int(n) <= 1200]
            if candidates:
                v_rating = max(candidates)
        if v_rating:
            tiers = sorted(_MOSFET_BY_VOLTAGE.keys())
            tier  = min(tiers, key=lambda t: abs(t - v_rating))
            vto, kp, rd, coss = _MOSFET_BY_VOLTAGE[tier]
            rs = rd / 4; cgso = coss * 0.8; cgdo = coss * 0.3
        else:
            # 3. safe generic 200V fallback
            vto, kp, rd, rs, cgso, cgdo, coss = 3.6, 0.4, 0.15, 0.04, 1.0e-9, 0.2e-9, 150e-12

    if is_pmos:
        vto = -abs(vto)

    model = dict(
        level=1,
        Vto=vto,
        Kp=kp,
        Rd=rd,
        Rs=rs,
        Lambda=0.02,
        Gamma=0,
        Phi=0.6,
        Cgso=cgso,
        Cgdo=cgdo,
        Is=1e-14,
        Pb=0.8,
    )
    return model, coss


def build_and_run_sim(components, pin_assignments, tran_cmd=None):
    """
    Build a PySpice Circuit from parsed components, run ngspice transient,
    return (sim_data_dict, status_string).

    sim_data_dict: {net_name: [float, ...]}  keyed by LTspice net names.
    """
    if not PYSPICE_OK:
        return {}, "PySpice not installed  (pip install PySpice)"

    issues = validate_assignments(pin_assignments)
    errors = [(sev, name, msg) for sev, name, msg in issues if sev == "ERROR"]
    if errors:
        detail = "  |  ".join(f"{name}: {msg}" for _, name, msg in errors)
        return {}, f"Connectivity ERROR – sim blocked: {detail}"

    warnings = [f"{name}: {msg}" for sev, name, msg in issues if sev == "WARN"]

    if '{' in tran_cmd:
        tran_cmd = ".tran 0 2m 0 5n"   # 2ms @ 5ns step — good for 136kHz
        print(f"[TRAN] expression unresolvable, using default: {tran_cmd}")

    step_time, end_time = _infer_tran(tran_cmd, components)

    circuit = Circuit("LTspice Import")

    mosfet_models = {}
    bjt_models    = {}
    comp_counter  = defaultdict(int)

    for comp, nets in pin_assignments:
        ctype = comp["type"]
        raw_val = comp["value"] or ""

        def node(pin, _nets=nets):
            """
            Resolve a pin name to a PySpice node.
            None means the pin had no wire snap hit – use a dead stub net
            rather than defaulting to GND (which would cause phantom shorts).
            """
            net = _nets.get(pin)          # None if key missing OR value is None
            if net is None:
                return f"_UNCONNECTED_{comp['name']}_{pin}"
            return _net_to_pyspice(net, circuit)

        try:
            if ctype.startswith("res"):
                v = _parse_comp_value(raw_val)
                if v is None: v = 1e3
                circuit.R(comp["name"], node("1"), node("2"), v@u_Ohm)

            elif ctype.startswith("cap"):
                v = _parse_comp_value(raw_val)
                if v is None: v = 1e-6
                circuit.C(comp["name"], node("1"), node("2"), v@u_F)

            elif ctype.startswith("ind"):
                v = _parse_comp_value(raw_val)
                if v is None: v = 1e-3
                circuit.L(comp["name"], node("1"), node("2"), v@u_H)

            elif ctype.startswith("voltage"):
                val_up = raw_val.upper()
                p, n_ = node("P"), node("N")
                name  = comp["name"]

                if val_up.startswith("PULSE"):
                    pp = parse_pulse_params(raw_val)
                    if pp:
                        circuit.PulseVoltageSource(
                            name, p, n_,
                            initial_value    = pp.get("initial_value", 0)    @u_V,
                            pulsed_value     = pp.get("pulsed_value",  5)    @u_V,
                            delay_time       = pp.get("delay_time",    0)    @u_s,
                            rise_time        = pp.get("rise_time",     1e-9) @u_s,
                            fall_time        = pp.get("fall_time",     1e-9) @u_s,
                            pulse_width      = pp.get("pulse_width",   1e-3) @u_s,
                            period           = pp.get("period",        2e-3) @u_s,
                        )
                    else:
                        circuit.V(name, p, n_, 5@u_V)

                elif val_up.startswith("SIN"):
                    sp = parse_sin_params(raw_val)
                    if sp:
                        circuit.SinusoidalVoltageSource(
                            name, p, n_,
                            offset    = sp.get("offset",    0)   @u_V,
                            amplitude = sp.get("amplitude", 1)   @u_V,
                            frequency = sp.get("frequency", 1e3) @u_Hz,
                        )
                    else:
                        circuit.V(name, p, n_, 1@u_V)

                elif val_up.startswith("AC"):
                    amp = parse_value_float(re.sub(r'AC\s*', '', raw_val, flags=re.I))
                    circuit.V(name, p, n_, (amp or 1)@u_V)

                else:
                    v = parse_value_float(raw_val)
                    circuit.V(name, p, n_, (v or 0)@u_V)

            elif ctype.startswith("current"):
                v = parse_value_float(raw_val)
                circuit.I(comp["name"], node("P"), node("N"), (v or 1e-3)@u_A)

            elif ctype.startswith("nmos") or ctype.startswith("pmos"):
                model_name = raw_val or ctype.upper()
                # register a generic model if we haven't seen it
                if model_name not in mosfet_models:
                    is_pmos = ctype.startswith("pmos")
                    mtype   = "pmos" if is_pmos else "nmos"
                    model_params, coss_val = _mosfet_params(model_name, is_pmos=is_pmos)
                    circuit.model(model_name, mtype, **model_params)
                    mosfet_models[model_name] = coss_val   # store Coss for cap injection
                d, g, s = node("D"), node("G"), node("S")
                b = s   # body always tied to source (N-ch to GND, P-ch to VDD)
                circuit.MOSFET(comp["name"], d, g, s, b, model=model_name)
                # Inject Coss as explicit drain-source cap — clean convergence
                coss_val = mosfet_models.get(model_name, 0)
                if isinstance(coss_val, float) and coss_val > 0:
                    circuit.C(f"Coss_{comp['name']}", d, s, coss_val @u_F)

            elif ctype.startswith("npn") or ctype.startswith("pnp"):
                model_name = raw_val or ctype.upper()
                if model_name not in bjt_models:
                    btype = "NPN" if ctype.startswith("npn") else "PNP"
                    circuit.model(model_name, btype, Bf=100)
                    bjt_models[model_name] = True
                circuit.BJT(comp["name"],
                            node("C"), node("B"), node("E"), model=model_name)

            elif ctype.startswith("diode") or ctype.startswith("zener"):
                model_name = raw_val or "D"
                if model_name not in mosfet_models:
                    circuit.model(model_name, "D")
                    mosfet_models[model_name] = True
                circuit.Diode(comp["name"], node("A"), node("K"), model=model_name)

            else:
                warnings.append(f"Skipped unsupported type: {ctype} ({comp['name']})")

        except Exception as exc:
            warnings.append(f"{comp['name']}: {exc}")

    try:
        nl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_sim.spice")
        with open(nl_path, "w") as _f:
            _f.write(str(circuit))
            _f.write(f"\n* .tran step={step_time:.3e}  end={end_time:.3e}\n")
        print(f"[SIM] Netlist written to {nl_path}")
    except Exception as _e:
        print(f"[SIM] Could not write netlist: {_e}")

    try:
        simulator = circuit.simulator(temperature=25, nominal_temperature=25)
        analysis  = simulator.transient(
            step_time = step_time @u_s,
            end_time  = end_time  @u_s,
        )
    except Exception as exc:
        msg = str(exc)
        if warnings:
            msg += "  |  " + "; ".join(warnings)
        return {}, f"Sim error: {msg}"

    # store time array for ZVS and waveform code . sample counts does not equal time steps.
    sim_data = {}
    raw_keys = {}

    try:
        time_arr = [float(t) for t in analysis.time]
        sim_data["__time__"] = time_arr
    except Exception:
        time_arr = []

    try:
        node_iter = analysis.nodes.keys()
    except AttributeError:
        node_iter = []

    for raw_key in node_iter:
        name_str = str(raw_key)
        try:
            arr = [float(v) for v in analysis[raw_key]]
            if arr:
                sim_data[name_str]         = arr
                sim_data[name_str.lower()] = arr
                raw_keys[name_str.lower()] = name_str
        except Exception:
            pass

    sim_data["__raw_keys__"] = raw_keys

    status = "OK"
    if not [k for k in sim_data if not k.startswith("__")]:
        status = "OK but no node data returned – check ngspice/PySpice setup"
    elif warnings:
        status = "OK (warnings: " + "; ".join(warnings[:2]) + ")"

    return sim_data, status



def build_comp_layout(components):
    return [(c, pygame.Rect(c["x"]-COMP_W//2, c["y"]-COMP_H//2, COMP_W, COMP_H))
            for c in components]

def compute_bounds(layout, wires, flags):
    xs, ys = [], []
    for _, r in layout:
        xs += [r.left, r.right]; ys += [r.top, r.bottom]
    for x1,y1,x2,y2 in wires:
        xs += [x1,x2]; ys += [y1,y2]
    for f in flags:
        xs.append(f["x"]); ys.append(f["y"])
    if not xs:
        return pygame.Rect(0, 0, 800, 600)
    pad = 80
    return pygame.Rect(min(xs)-pad, min(ys)-pad,
                       max(xs)-min(xs)+2*pad, max(ys)-min(ys)+2*pad)


def voltage_to_color(v, v_min=-1.0, v_max=15.0):
    t = max(0., min(1., (v - v_min) / max(0.001, v_max - v_min)))
    if t < 0.5:
        t2 = t * 2
        r, g, b = int(t2*120), int(100+t2*155), int(220-t2*160)
    else:
        t2 = (t - 0.5) * 2
        r, g, b = int(120+t2*135), int(255-t2*200), int(60-t2*60)
    return (max(0,min(255,r)), max(0,min(255,g)), max(0,min(255,b)))


def draw_rrect(surf, color, rect, r=8, bw=0, bc=None):
    pygame.draw.rect(surf, color, rect, border_radius=r)
    if bw and bc:
        pygame.draw.rect(surf, bc, rect, bw, border_radius=r)

def fit_text(font, text, max_w):
    if font.size(text)[0] <= max_w: return text
    while text and font.size(text+"...")[0] > max_w: text = text[:-1]
    return text + "..."

def list_asc_files(d="."):
    try:
        return sorted([f for f in os.listdir(d) if f.lower().endswith(".asc")],
                      key=str.lower)
    except OSError:
        return []


class FileBrowser:
    ROW_H=46; LIST_X=60; LIST_Y=140; LIST_W=680; MAX_ROWS=11

    def __init__(self, screen, fonts):
        self.screen=screen; self.fonts=fonts
        self.files=list_asc_files(); self.selected=None
        self.scroll=0; self.open_cb=None; self.hover_btn=False
        self.btn=pygame.Rect(W//2-90, H-92, 180, 48)

    def _visible(self): return self.files[self.scroll:self.scroll+self.MAX_ROWS]
    def _row(self, i): return pygame.Rect(self.LIST_X, self.LIST_Y+i*self.ROW_H, self.LIST_W, self.ROW_H-4)
    def _open(self):
        if self.selected and self.open_cb: self.open_cb(os.path.join(".", self.selected))

    def handle_event(self, event):
        if event.type == pygame.MOUSEMOTION:
            self.hover_btn = self.btn.collidepoint(event.pos)
        elif event.type == pygame.MOUSEBUTTONDOWN:
            mx, my = event.pos
            if event.button==4: self.scroll=max(0,self.scroll-1)
            elif event.button==5: self.scroll=min(max(0,len(self.files)-self.MAX_ROWS),self.scroll+1)
            elif event.button==1:
                if self.btn.collidepoint(mx,my): self._open(); return
                for i,f in enumerate(self._visible()):
                    if self._row(i).collidepoint(mx,my):
                        if self.selected==f: self._open()
                        else: self.selected=f
                        break
        elif event.type==pygame.KEYDOWN:
            if not self.files: return
            idx=self.files.index(self.selected) if self.selected in self.files else -1
            if event.key==pygame.K_UP:
                new=max(0,idx-1); self.selected=self.files[new]
                if new<self.scroll: self.scroll=new
            elif event.key==pygame.K_DOWN:
                new=min(len(self.files)-1,idx+1); self.selected=self.files[new]
                if new>=self.scroll+self.MAX_ROWS: self.scroll=new-self.MAX_ROWS+1
            elif event.key in (pygame.K_RETURN,pygame.K_KP_ENTER): self._open()

    def draw(self):
        s=self.screen; s.fill(BG)
        pygame.draw.rect(s, PANEL, (0,0,W,96))
        pygame.draw.line(s, BORDER, (0,96),(W,96),1)
        s.blit(self.fonts["title"].render("LTspice Schematic Viewer",True,TEXT_BRIGHT),(self.LIST_X,22))
        s.blit(self.fonts["small"].render(f"  {os.path.abspath('.')}  —  {len(self.files)} .asc file(s)",True,TEXT_DIM),(self.LIST_X,62))
        s.blit(self.fonts["body"].render("FILE NAME",True,TEXT_DIM),(self.LIST_X+14,self.LIST_Y-28))
        pygame.draw.line(s, BORDER,(self.LIST_X,self.LIST_Y-8),(self.LIST_X+self.LIST_W,self.LIST_Y-8),1)
        for i,fname in enumerate(self._visible()):
            r=self._row(i); sel=(fname==self.selected)
            draw_rrect(s, SEL_BG if sel else PANEL, r, r=6, bw=1, bc=SEL_BORDER if sel else BORDER)
            s.blit(self.fonts["body"].render("»",True,ACCENT if sel else TEXT_DIM),(r.x+12,r.centery-self.fonts["body"].get_height()//2))
            lt=self.fonts["mono"].render(fit_text(self.fonts["mono"],fname,self.LIST_W-60),True,TEXT_BRIGHT if sel else TEXT_MAIN)
            s.blit(lt,(r.x+38,r.centery-lt.get_height()//2))
        if not self.files:
            s.blit(self.fonts["body"].render("No .asc files found in current directory.",True,TEXT_DIM),(self.LIST_X,self.LIST_Y+20))
        if len(self.files)>self.MAX_ROWS:
            bx=self.LIST_X+self.LIST_W+12; bht=self.MAX_ROWS*self.ROW_H
            th=max(24,bht*self.MAX_ROWS//len(self.files))
            ty=self.LIST_Y+(bht-th)*self.scroll//max(1,len(self.files)-self.MAX_ROWS)
            pygame.draw.rect(s,BORDER,(bx,self.LIST_Y,6,bht),border_radius=3)
            pygame.draw.rect(s,ACCENT,(bx,ty,6,th),border_radius=3)
        en=bool(self.selected)
        bc=ACCENT2 if (en and self.hover_btn) else (ACCENT if en else BORDER)
        bf=(50,70,110) if (en and self.hover_btn) else ((30,50,90) if en else (28,30,42))
        draw_rrect(s,bf,self.btn,r=8,bw=2,bc=bc)
        bl=self.fonts["body"].render("OPEN  →" if en else "OPEN",True,TEXT_BRIGHT if en else TEXT_DIM)
        s.blit(bl,(self.btn.centerx-bl.get_width()//2,self.btn.centery-bl.get_height()//2))

        # PySpice status badge
        if not PYSPICE_OK:
            badge=self.fonts["tiny"].render("⚠ PySpice not installed – sim disabled",True,(255,160,60))
            s.blit(badge,(W//2-badge.get_width()//2, H-56))

        hint=self.fonts["tiny"].render("↑↓ navigate   Enter or double-click to open",True,TEXT_DIM)
        s.blit(hint,(W//2-hint.get_width()//2,H-28))



class ZVSPlot:
    """
    Always-visible ZVS analysis panel.

    All analysis is done in REAL TIME (seconds) using the ngspice time
    vector — never in sample-index space — because ngspice uses adaptive
    timestep and indices do not equal time.

    Window zoom:
      The view is centred on the last gate rising edge.
      Half-window = 1 full switching cycle (1/Ff) so both the falling
      drain resonance AND the gate turn-on are visible together.
      Gate transition occupies roughly 50% of the horizontal axis.

    ZVS metric:
      drain_at_rise  = mean of drain voltage in the 100 ns window
                       ending exactly at t_edge (before MOSFET conducts)
      avg_peak       = mean of per-cycle drain peaks (last 10 cycles)
      zvs_err%       = drain_at_rise / avg_peak * 100
      zvs%           = 100 - zvs_err%
    """

    PW = 420
    PH = 230
    PAD_L = 10
    PAD_R = 10
    PAD_T = 38
    PAD_B = 30
    GATE_THRESH = 2.0    # V — gate "high" threshold
    GATE_HYST   = 0.5    # V — hysteresis

    def __init__(self, fonts):
        self.fonts        = fonts
        self.zvs_pct      = None
        self.zvs_label    = "---"
        self.edge_idx     = None   # sample index of last gate rise
        self.edge_time    = None   # seconds of last gate rise
        self.drain_at_rise= None
        self.drain_peak   = None
        self.gate_data    = None
        self.drain_data   = None
        self.time_arr     = None   # real time vector (seconds)
        self.period       = None   # detected switching period (seconds)

    @staticmethod
    def _interp(time_arr, val_arr, t_query):
        """Linear interpolation of val_arr at real time t_query."""
        n = len(time_arr)
        if t_query <= time_arr[0]:  return val_arr[0]
        if t_query >= time_arr[-1]: return val_arr[-1]
        # binary search
        lo, hi = 0, n - 1
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if time_arr[mid] <= t_query: lo = mid
            else: hi = mid
        dt = time_arr[hi] - time_arr[lo]
        if dt == 0: return val_arr[lo]
        frac = (t_query - time_arr[lo]) / dt
        return val_arr[lo] + frac * (val_arr[hi] - val_arr[lo])

    @staticmethod
    def _mean_in_window(time_arr, val_arr, t_start, t_end):
        """Mean of val_arr samples with time in [t_start, t_end]."""
        vals = [v for t, v in zip(time_arr, val_arr) if t_start <= t <= t_end]
        return (sum(vals) / len(vals)) if vals else 0.0

    @staticmethod
    def _max_in_window(time_arr, val_arr, t_start, t_end):
        vals = [v for t, v in zip(time_arr, val_arr) if t_start <= t <= t_end]
        return max(vals) if vals else 0.0

    def _idx_at_time(self, t_query):
        """Nearest sample index for a given time."""
        if not self.time_arr: return 0
        best, best_d = 0, float('inf')
        for i, t in enumerate(self.time_arr):
            d = abs(t - t_query)
            if d < best_d: best_d, best = d, i
            if t > t_query + best_d: break
        return best

    def update(self, gate_arr, drain_arr, time_arr=None):
        """
        Run ZVS analysis. time_arr must be the ngspice time vector (seconds).
        If None, falls back to uniform 5ns step (degraded accuracy).
        """
        if not gate_arr or not drain_arr:
            self.zvs_pct   = None
            self.zvs_label = "No sim data"
            return

        n = min(len(gate_arr), len(drain_arr))
        self.gate_data  = gate_arr[:n]
        self.drain_data = drain_arr[:n]

        # build time vector
        if time_arr and len(time_arr) >= n:
            self.time_arr = time_arr[:n]
        else:
            # fallback: assume uniform 5ns — will be imprecise
            self.time_arr = [i * 5e-9 for i in range(n)]

        t = self.time_arr
        g = self.gate_data
        d = self.drain_data
        edges_idx  = []   # sample indices
        edges_time = []   # seconds
        low = True
        for i in range(1, n):
            if low and g[i] >= self.GATE_THRESH + self.GATE_HYST:
                edges_idx.append(i)
                edges_time.append(t[i])
                low = False
            elif not low and g[i] < self.GATE_THRESH - self.GATE_HYST:
                low = True

        if not edges_idx:
            self.zvs_pct   = None
            self.zvs_label = "No gate edges found"
            self.edge_idx  = None
            return

        if len(edges_time) >= 2:
            periods = [edges_time[i+1] - edges_time[i]
                       for i in range(len(edges_time)-1)]
            self.period = sum(periods) / len(periods)
        else:
            self.period = t[-1] / max(1, len(edges_time))

        T = self.period

        # per-cycle drain peaks , last 10 cycles
        use_edges = edges_idx[-10:]
        use_times = edges_time[-10:]
        cycle_peaks = []
        for ei, et in zip(use_edges, use_times):
            pk = self._max_in_window(t, d, et - T, et)
            if pk > 0:
                cycle_peaks.append(pk)

        if not cycle_peaks:
            self.zvs_pct   = None
            self.zvs_label = "Cannot find drain peaks"
            return

        avg_peak = sum(cycle_peaks) / len(cycle_peaks)
        if avg_peak <= 0.5:
            self.zvs_pct   = 100.0
            self.zvs_label = "DRAIN FLAT"
            return

        # store drain voltage at last gate turn-on
        # measure the 100 ns window ending at t_edge
        last_edge_idx  = edges_idx[-1]
        last_edge_time = edges_time[-1]
        pre_window_s   = 100e-9   # 100 ns pre-edge window
        drain_at_rise  = max(0.0, self._mean_in_window(
            t, d,
            last_edge_time - pre_window_s,
            last_edge_time
        ))

        self.edge_idx      = last_edge_idx
        self.edge_time     = last_edge_time
        self.drain_at_rise = drain_at_rise
        self.drain_peak    = avg_peak

        zvs_err = (drain_at_rise / avg_peak) * 100.0
        self.zvs_pct = max(0.0, 100.0 - zvs_err)

        if zvs_err < 2.0:   self.zvs_label = "100% ZVS ✓"
        elif zvs_err < 10.0: self.zvs_label = "75% ZVS"
        elif zvs_err < 30.0: self.zvs_label = "50% ZVS"
        else:                 self.zvs_label = "NO ZVS"

    def draw(self, surf, x, y, sim_frame, total_frames):
        pw, ph = self.PW, self.PH
        pygame.draw.rect(surf, (14, 18, 28), (x, y, pw, ph), border_radius=8)
        pygame.draw.rect(surf, ACCENT,       (x, y, pw, ph), 1, border_radius=8)

        # header
        surf.blit(self.fonts["tiny"].render("ZVS ANALYSIS", True, ACCENT),
                  (x + 8, y + 6))

        if self.zvs_pct is None:
            ns = self.fonts["tiny"].render(self.zvs_label, True, TEXT_DIM)
            surf.blit(ns, (x + pw//2 - ns.get_width()//2, y + ph//2))
            return

        pct_col = ((80,220,80)   if self.zvs_pct > 95 else
                   (220,200,60)  if self.zvs_pct > 80 else
                   (220,80,60))
        ps  = self.fonts["body"].render(f"{self.zvs_pct:.1f}%", True, pct_col)
        lbl = self.fonts["tiny"].render(self.zvs_label,         True, pct_col)
        surf.blit(ps,  (x + pw - ps.get_width()   - 8, y + 4))
        surf.blit(lbl, (x + pw - lbl.get_width()  - 8, y + 22))

        if self.gate_data is None or self.edge_idx is None or self.time_arr is None:
            return

        ix = x + self.PAD_L
        iw = pw - self.PAD_L - self.PAD_R
        iy = y + self.PAD_T
        ih = ph - self.PAD_T - self.PAD_B
        gate_h  = ih // 2
        drain_h = ih - gate_h
        mid_y   = iy + gate_h

        pygame.draw.line(surf, (30,38,52), (ix, mid_y), (ix+iw, mid_y), 1)

        T          = self.period or 7.35e-6
        et         = self.edge_time
        win_t0     = et - T          # one full cycle before edge
        win_t1     = et + T * 0.15   # small margin after edge
        win_span   = win_t1 - win_t0

        def sx(t_val):
            frac = (t_val - win_t0) / max(1e-20, win_span)
            return ix + int(frac * iw)

        # collect samples in window
        t_arr = self.time_arr
        n     = len(t_arr)
        win_i0 = max(0, self._idx_at_time(win_t0) - 1)
        win_i1 = min(n-1, self._idx_at_time(win_t1) + 1)

        g_arr  = self.gate_data
        g_max  = max((g_arr[i] for i in range(win_i0, win_i1+1)), default=13.0)
        g_max  = max(g_max, self.GATE_THRESH + 1.0)

        def gy(v):
            frac = max(0., min(1., v / g_max))
            return iy + int((1.0 - frac) * (gate_h - 4)) + 2

        pts_g = [(sx(t_arr[i]), gy(g_arr[i])) for i in range(win_i0, win_i1+1)]
        if len(pts_g) > 1:
            pygame.draw.lines(surf, (80,220,80), False, pts_g, 2)

        # gate threshold marker
        thr_y = gy(self.GATE_THRESH)
        pygame.draw.line(surf, (40,100,40), (ix, thr_y), (ix+iw, thr_y), 1)
        surf.blit(self.fonts["tiny"].render(f"{self.GATE_THRESH:.0f}V", True, (40,140,40)),
                  (ix + iw - 22, thr_y - 12))

        d_arr = self.drain_data
        d_max = max((d_arr[i] for i in range(win_i0, win_i1+1)), default=self.drain_peak)
        d_max = max(d_max, self.drain_peak * 1.05, 1.0)

        def dy(v):
            frac = max(0., min(1., v / d_max))
            return mid_y + int((1.0 - frac) * (drain_h - 4)) + 2

        pts_d = [(sx(t_arr[i]), dy(d_arr[i])) for i in range(win_i0, win_i1+1)]
        if len(pts_d) > 1:
            pygame.draw.lines(surf, (255,140,40), False, pts_d, 2)

        # 0V rail
        pygame.draw.line(surf, (60,44,30), (ix, dy(0)), (ix+iw, dy(0)), 1)

        # ── edge marker
        ex = sx(et)
        pygame.draw.line(surf, (255,255,80), (ex, iy), (ex, iy+ih), 1)

        # drain dot & label at turn-on
        dv   = self.drain_at_rise
        dv_y = dy(dv)
        pygame.draw.circle(surf, (255,80,80), (ex, dv_y), 4)
        dv_s = self.fonts["tiny"].render(f"{dv:.2f}V", True, (255,100,100))
        surf.blit(dv_s, (min(ex+5, x+pw-dv_s.get_width()-4), dv_y - 12))

        # peak label
        pk_s = self.fonts["tiny"].render(f"pk={self.drain_peak:.1f}V", True, (180,120,60))
        surf.blit(pk_s, (ix+2, mid_y+4))

        # axis labels
        surf.blit(self.fonts["tiny"].render("GATE",  True, (60,180,60)),  (ix+2, iy+2))
        surf.blit(self.fonts["tiny"].render("DRAIN", True, (200,110,40)), (ix+2, mid_y+drain_h-14))

        # time axis tick at edge
        t_lbl = self.fonts["tiny"].render(f"t={et*1e6:.2f}µs", True, TEXT_DIM)
        surf.blit(t_lbl, (ex - t_lbl.get_width()//2, iy + ih + 2))

        # freq from detected period
        if self.period:
            f_lbl = self.fonts["tiny"].render(
                f"Fsw={1/self.period/1e3:.1f}kHz", True, TEXT_DIM)
            surf.blit(f_lbl, (x + pw - f_lbl.get_width() - 6,
                               y + ph - self.PAD_B + 2))

        # bottom info bar
        info = (f"drain@rise={dv:.3f}V   avg_peak={self.drain_peak:.2f}V   "
                f"err={100-self.zvs_pct:.1f}%")
        infs = self.fonts["tiny"].render(info, True, TEXT_DIM)
        surf.blit(infs, (x + pw//2 - infs.get_width()//2, y + ph - 14))

        # ── playhead ─────────────────────────────────────────────────────
        if total_frames > 0 and self.time_arr:
            t_total = self.time_arr[-1]
            t_now   = t_total * (sim_frame % total_frames) / max(1, total_frames)
            if win_t0 <= t_now <= win_t1:
                pygame.draw.line(surf, (100,100,200),
                                 (sx(t_now), iy), (sx(t_now), iy+ih), 1)




# ═══════════════════════════ PARAM PANEL ════════════════════════════════════

class ParamPanel:
    """
    Collapsible sidebar panel listing every {param} found in the schematic.
    Each row has a labelled text-input box.  The panel appears at the
    top-right of the schematic view and does not block the main canvas.
    """
    W_OPEN   = 230
    W_CLOSED = 36
    ROW_H    = 32
    PAD      = 8
    TOP      = 64          # y below the toolbar
    LABEL_W  = 88
    INPUT_W  = 108

    def __init__(self, param_names, fonts):
        self.fonts       = fonts
        self.param_names = param_names          # list of str, sorted
        self.values      = {n: "" for n in param_names}  # user text
        self.focused     = None                 # name of focused field, or None
        self.expanded    = bool(param_names)    # auto-open if params exist
        self._cursor_vis = True
        self._cursor_t   = 0

    def panel_rect(self):
        w = self.W_OPEN if self.expanded else self.W_CLOSED
        h = self.PAD * 2 + max(1, len(self.param_names)) * self.ROW_H + 36
        return pygame.Rect(W - w - 6, self.TOP, w, h)

    def toggle_rect(self):
        r = self.panel_rect()
        return pygame.Rect(r.x, r.y, 28, 28)

    def _input_rect(self, index):
        r = self.panel_rect()
        x = r.x + self.LABEL_W + self.PAD
        y = r.y + 34 + index * self.ROW_H + 4
        return pygame.Rect(x, y, self.INPUT_W, 22)


    def handle_event(self, event):
        """Return True if the event was consumed."""
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            mx, my = event.pos
            if self.toggle_rect().collidepoint(mx, my):
                self.expanded = not self.expanded
                self.focused  = None
                return True
            if not self.expanded:
                return False
            self.focused = None
            for i, name in enumerate(self.param_names):
                if self._input_rect(i).collidepoint(mx, my):
                    self.focused = name
                    return True
            if not self.panel_rect().collidepoint(mx, my):
                return False
            return True

        if event.type == pygame.KEYDOWN and self.focused is not None:
            name = self.focused
            if event.key == pygame.K_BACKSPACE:
                self.values[name] = self.values[name][:-1]
            elif event.key in (pygame.K_RETURN, pygame.K_TAB, pygame.K_KP_ENTER):
                idx = self.param_names.index(name)
                self.focused = self.param_names[(idx + 1) % len(self.param_names)]
            elif event.key == pygame.K_ESCAPE:
                self.focused = None
            elif event.unicode and event.unicode.isprintable():
                self.values[name] += event.unicode
            return True

        return False


    def draw(self, surf, tick_ms):
        # cursor blink
        self._cursor_t += tick_ms
        if self._cursor_t > 500:
            self._cursor_vis = not self._cursor_vis
            self._cursor_t   = 0

        r = self.panel_rect()
        pygame.draw.rect(surf, (18, 22, 34), r, border_radius=8)
        pygame.draw.rect(surf, ACCENT if self.expanded else BORDER,
                         r, 1, border_radius=8)

        # toggle arrow
        arrow = "▶" if not self.expanded else "▼"
        ar = self.fonts["tiny"].render(arrow, True,
                                       ACCENT if self.param_names else TEXT_DIM)
        surf.blit(ar, (r.x + 8, r.y + 8))

        if not self.expanded:
            # vertical label when collapsed
            label = self.fonts["tiny"].render(
                f"{len(self.param_names)}par", True, TEXT_DIM)
            surf.blit(label, (r.x + 4, r.y + 32))
            return

        # header
        hdr = self.fonts["tiny"].render(
            "PARAMETERS", True, ACCENT)
        surf.blit(hdr, (r.x + 32, r.y + 9))

        if not self.param_names:
            nm = self.fonts["tiny"].render(
                "No {params} found", True, TEXT_DIM)
            surf.blit(nm, (r.x + self.PAD, r.y + 38))
            return

        for i, name in enumerate(self.param_names):
            row_y = r.y + 34 + i * self.ROW_H

            # label
            lbl = self.fonts["tiny"].render(
                fit_text(self.fonts["tiny"], name, self.LABEL_W - 4),
                True, TEXT_MAIN)
            surf.blit(lbl, (r.x + self.PAD, row_y + 8))

            # input box
            ir   = self._input_rect(i)
            focused = (self.focused == name)
            val_str = self.values[name].strip()

            # parse check
            if val_str:
                # strip outer PULSE()/SIN() wrapper for the check
                bare = re.sub(r'^(PULSE|SIN)\s*\(.*\)$', 'ok', val_str, flags=re.I)
                parsed_ok = (bare == 'ok') or (parse_value_float(val_str) is not None)
            else:
                parsed_ok = None   # empty = neutral

            box_col  = ((28, 46, 28) if parsed_ok is True  else
                        (46, 28, 28) if parsed_ok is False else
                        (28, 32, 46))
            bord_col = ((80, 220, 80)  if parsed_ok is True  else
                        (220, 80, 80)  if parsed_ok is False else
                        (ACCENT        if focused            else BORDER))

            pygame.draw.rect(surf, box_col,  ir, border_radius=4)
            pygame.draw.rect(surf, bord_col, ir, 1, border_radius=4)

            display = val_str
            if focused and self._cursor_vis:
                display += "|"
            vt = self.fonts["tiny"].render(
                fit_text(self.fonts["tiny"], display, ir.w - 6),
                True, TEXT_BRIGHT if val_str else TEXT_DIM)
            surf.blit(vt, (ir.x + 4, ir.y + 4))

            # parsed float hint shown to right of box
            if val_str and parsed_ok:
                fv = parse_value_float(val_str)
                if fv is not None:
                    hint_str = (f"{fv*1e9:.3g}n"  if abs(fv) < 1e-6 else
                                f"{fv*1e6:.3g}µ"  if abs(fv) < 1e-3 else
                                f"{fv*1e3:.3g}m"  if abs(fv) < 1    else
                                f"{fv:.3g}")
                    hs = self.fonts["tiny"].render(hint_str, True, (80, 180, 80))
                    surf.blit(hs, (ir.x - hs.get_width() - 4, ir.y + 4))
            pygame.draw.rect(surf, (28, 32, 46), ir, border_radius=4)
            pygame.draw.rect(surf,
                             ACCENT if focused else BORDER,
                             ir, 1, border_radius=4)

            display = self.values[name]
            if focused and self._cursor_vis:
                display += "|"
            vt = self.fonts["tiny"].render(
                fit_text(self.fonts["tiny"], display, ir.w - 6),
                True, TEXT_BRIGHT if self.values[name] else TEXT_DIM)
            surf.blit(vt, (ir.x + 4, ir.y + 4))


    def all_filled(self):
        return all(v.strip() for v in self.values.values())

    def any_filled(self):
        return any(v.strip() for v in self.values.values())









class SchematicView:
    WAVE_W, WAVE_H = 290, 130

    def __init__(self, screen, fonts, filepath):
        self.screen=screen; self.fonts=fonts; self.filepath=filepath
        self.back_cb=None

        # parse
        self.components, self.wires, self.flags, self.tran_cmd, self.dot_params, self.err = parse_asc(filepath)
        self.layout = build_comp_layout(self.components)

        # .net file is more reliable than parsing .asc TEXT blocks
        net_params, net_tran = read_net_file(filepath)
        if net_params:
            self.dot_params.update(net_params)   # net file wins on conflict
        if net_tran:
            self.tran_cmd = net_tran

        self.param_names = extract_param_names(self.components, self.tran_cmd, self.dot_params)
        self.param_panel = ParamPanel(self.param_names, fonts)
        self.param_panel.run_cb = self._run_sim
        self._tick_ms = 0

        # pre-fill panel with .param defaults from file
        defaults = resolve_dot_params(self.dot_params, {})
        for name in self.param_names:
            if name in defaults:
                self.param_panel.values[name] = _float_to_si(defaults[name])

        # nets
        self.point_to_net, self.net_to_points, self.junctions = build_net_map(self.wires, self.flags)
        self.pin_assignments = assign_component_nets(self.components, self.point_to_net, wires=self.wires)

        # pre-sim validation – catches shorted VSRCs, floating pins, etc.
        self.conn_issues = validate_assignments(self.pin_assignments)
        errors = [i for i in self.conn_issues if i[0] == "ERROR"]

        # simulation (PySpice) – skip if hard errors found
        self.sim_data   = {}
        self.sim_frame  = 0
        self.probed_net = None
        if errors:
            self.sim_status = (
                f"{len(errors)} connectivity error(s) – sim blocked.  "
                "Open Netlist panel for details.")
        else:
            self.sim_status = "Not run"
            self._run_sim()

        # viewport
        bounds = compute_bounds(self.layout, self.wires, self.flags)
        sx = (W-80)/max(1,bounds.w); sy = (H-80)/max(1,bounds.h)
        self.zoom = max(0.1, min(sx,sy,2.5))
        self.offset = pygame.Vector2(
            W//2 - bounds.centerx*self.zoom,
            H//2 - bounds.centery*self.zoom)

        self.dragging=False; self.drag_last=(0,0)
        self.hovered_comp=None; self.hovered_net=None
        self.btn_back    = pygame.Rect(14, 12, 120, 36)
        self.btn_netlist = pygame.Rect(148, 12, 150, 36)
        self.show_netlist = False

        # voltage scale taken from sim outptu data
        all_v = [v for k,arr in self.sim_data.items() if not k.startswith('__') and isinstance(arr,list) for v in arr]
        self.v_min = min(all_v, default=-1.0)
        self.v_max = max(all_v, default=15.0)

        self.zvs_plot = ZVSPlot(fonts)
        self._update_zvs()


    def _run_sim(self):
        pv = self.param_panel.values if hasattr(self, 'param_panel') else {}
        resolved = substitute_params(self.components, pv)
        tran_cmd = self.tran_cmd
        if tran_cmd and '{' in tran_cmd:
            try:
                dot_params = getattr(self, 'dot_params', {})
                resolved_ctx = resolve_dot_params(dot_params, pv)

                def eval_brace(m):
                    v = eval_spice_expr(m.group(1), resolved_ctx)
                    return _float_to_si(v) if v is not None else m.group(0)

                tran_cmd = re.sub(r'\{([^}]+)\}', eval_brace, tran_cmd)

                if '{' in tran_cmd:
                    tran_cmd = ".tran 0 2m 0 5n"
                    print("[TRAN] using default: 2ms / 5ns")
                else:
                    print(f"[TRAN] resolved: {tran_cmd}")
            except Exception as e:
                tran_cmd = ".tran 0 2m 0 5n"
                print(f"[TRAN] eval error ({e}), using default")

        # begin sim run
        pin_assignments = assign_component_nets(
            resolved, self.point_to_net, wires=self.wires)
        data, status = build_and_run_sim(resolved, pin_assignments, tran_cmd)
        self.sim_data        = data
        self.sim_status      = status
        self.pin_assignments = pin_assignments

        # refresh voltage colour scale
        all_v = [v for k, arr in self.sim_data.items()
                 if not k.startswith('__') and isinstance(arr, list) for v in arr]
        self.v_min = min(all_v, default=-1.0)
        self.v_max = max(all_v, default=15.0)

        if hasattr(self, 'zvs_plot'):
            self._update_zvs()

    def _update_zvs(self):
        """Resolve gate/drain net names for M1 and feed data to ZVSPlot."""
        gate_net  = None
        drain_net = None
        for comp, nets in self.pin_assignments:
            if comp.get('name','').upper() == 'M1' or comp['type'].startswith('nmos'):
                gate_net  = nets.get('G') or nets.get('g')
                drain_net = nets.get('D') or nets.get('d')
                break
        gate_arr  = self._sim_get(gate_net)  if gate_net  else None
        drain_arr = self._sim_get(drain_net) if drain_net else None
        # pass the real time vector from ngspice — critical for correct timing
        time_arr  = self.sim_data.get('__time__')
        self.zvs_plot.update(gate_arr, drain_arr, time_arr=time_arr)
        self._zvs_gate_net  = gate_net
        self._zvs_drain_net = drain_net

    def s2w(self,sx,sy): return ((sx-self.offset.x)/self.zoom,(sy-self.offset.y)/self.zoom)
    def w2s(self,wx,wy): return (int(wx*self.zoom+self.offset.x), int(wy*self.zoom+self.offset.y))
    def w2s_rect(self,r):
        return pygame.Rect(int(r.x*self.zoom+self.offset.x), int(r.y*self.zoom+self.offset.y),
                           max(4,int(r.w*self.zoom)), max(4,int(r.h*self.zoom)))

    def _zoom_at(self,pivot,factor):
        wx,wy=self.s2w(*pivot); self.zoom=max(0.06,min(10.,self.zoom*factor))
        self.offset.x=pivot[0]-wx*self.zoom; self.offset.y=pivot[1]-wy*self.zoom

    def handle_event(self, event):
        if hasattr(self, 'param_panel'):
            consumed = self.param_panel.handle_event(event)
            if consumed:
                if (event.type == pygame.KEYDOWN and
                        event.key in (pygame.K_RETURN, pygame.K_KP_ENTER,
                                      pygame.K_TAB)):
                    self._run_sim()
                return                          # ← this return is critical

        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE:
                if self.show_netlist: self.show_netlist = False
                elif self.back_cb:   self.back_cb()
            spd = 28
            if event.key in (pygame.K_LEFT,  pygame.K_a): self.offset.x += spd
            if event.key in (pygame.K_RIGHT, pygame.K_d): self.offset.x -= spd
            if event.key in (pygame.K_UP,    pygame.K_w): self.offset.y += spd
            if event.key in (pygame.K_DOWN,  pygame.K_s): self.offset.y -= spd

        elif event.type == pygame.MOUSEBUTTONDOWN:
            if event.button == 4: self._zoom_at(event.pos, 1.12)
            elif event.button == 5: self._zoom_at(event.pos, 1/1.12)
            elif event.button == 1:
                if self.btn_back.collidepoint(event.pos):
                    if self.back_cb: self.back_cb()
                    return
                if self.btn_netlist.collidepoint(event.pos):
                    self.show_netlist = not self.show_netlist
                    return
                if self.hovered_net:
                    self.probed_net = (None if self.probed_net == self.hovered_net
                                       else self.hovered_net)
                self.dragging  = True
                self.drag_last = event.pos

        elif event.type == pygame.MOUSEBUTTONUP:
            if event.button == 1: self.dragging = False

        elif event.type == pygame.MOUSEMOTION:
            if self.dragging:
                dx = event.pos[0] - self.drag_last[0]
                dy = event.pos[1] - self.drag_last[1]
                self.offset   += (dx, dy)
                self.drag_last = event.pos
            wx, wy = self.s2w(*event.pos)
            self.hovered_comp = None
            for comp, rect in self.layout:
                if rect.collidepoint(wx, wy):
                    self.hovered_comp = comp
                    break
            self.hovered_net = self._nearest_net(wx, wy, 24)

    def _nearest_net(self,wx,wy,snap):
        best,best_d=None,snap
        for x1,y1,x2,y2 in self.wires:
            for px,py in ((x1,y1),(x2,y2)):
                d=math.hypot(px-wx,py-wy)
                if d<best_d:
                    net=self.point_to_net.get((px,py))
                    if net: best_d,best=d,net
        return best

    def _sim_get(self, net_name):
        """Case-insensitive lookup into sim_data. Returns list or None."""
        if not self.sim_data: return None
        if net_name in self.sim_data: return self.sim_data[net_name]
        lo = net_name.lower()
        if lo in self.sim_data: return self.sim_data[lo]
        return None

    def _net_color(self,net_name):
        data = self._sim_get(net_name)
        if data:
            fi=self.sim_frame%len(data)
            return voltage_to_color(data[fi], self.v_min, self.v_max)
        return WIRE_BASE

    def _palette(self,ctype):
        for k,v in TYPE_PALETTE.items():
            if ctype.startswith(k): return v
        return TYPE_PALETTE["default"]

    def _pin_nets(self,comp):
        for c,pn in self.pin_assignments:
            if c is comp: return pn
        return {}

    def draw(self):
        s=self.screen; s.fill(BG)

        # grid
        gs=max(4.,GRID*self.zoom); ox=self.offset.x%gs; oy=self.offset.y%gs
        gx=ox
        while gx<W:
            gy=oy
            while gy<H: pygame.draw.circle(s,GRID_COL,(int(gx),int(gy)),1); gy+=gs
            gx+=gs

        # wires
        ww=max(1,int(self.zoom*1.4))
        for x1,y1,x2,y2 in self.wires:
            sx1,sy1=self.w2s(x1,y1); sx2,sy2=self.w2s(x2,y2)
            net=self.point_to_net.get((x1,y1)) or self.point_to_net.get((x2,y2))
            col=self._net_color(net) if net else WIRE_BASE
            if net and net==self.probed_net:
                pygame.draw.line(s,(255,255,80),(sx1,sy1),(sx2,sy2),ww+4)
            pygame.draw.line(s,col,(sx1,sy1),(sx2,sy2),ww)

        # junctions
        jr=max(2,int(self.zoom*4))
        for jx,jy in self.junctions:
            net=self.point_to_net.get((jx,jy))
            col=self._net_color(net) if net else JUNCTION_C
            pygame.draw.circle(s,col,self.w2s(jx,jy),jr)

        # flags / net labels
        for flag in self.flags:
            sx,sy=self.w2s(flag["x"],flag["y"])
            lbl=flag["label"]
            if lbl=="0":
                gw=max(4,int(self.zoom*12))
                for i in range(3):
                    yoff=int(self.zoom*(i*5+2)); hw=gw-i*max(1,gw//3)
                    pygame.draw.line(s,FLAG_C,(sx-hw,sy+yoff),(sx+hw,sy+yoff),1)
            else:
                pygame.draw.circle(s,self._net_color(lbl),
                                   (sx,sy),max(3,int(self.zoom*3)))
                if self.zoom>0.4:
                    ls=self.fonts["tiny"].render(lbl,True,FLAG_C)
                    s.blit(ls,(sx+5,sy-ls.get_height()//2))

        # component boxes
        for comp,rect in self.layout:
            sr=self.w2s_rect(rect)
            fill_c,bord_c=self._palette(comp["type"])
            is_hov=(comp is self.hovered_comp)
            if is_hov: fill_c=tuple(min(255,c+50) for c in fill_c); bord_c=TEXT_BRIGHT
            pygame.draw.rect(s,(6,8,14),sr.move(3,4),border_radius=8)
            draw_rrect(s,fill_c,sr,r=8,bw=2,bc=bord_c)
            if sr.w<12 or sr.h<12: continue

            if self.zoom>0.5:
                for pname,px,py in component_pin_positions(comp):
                    spx,spy=self.w2s(px,py)
                    net=self._pin_nets(comp).get(pname)
                    pc=self._net_color(net) if net else TEXT_DIM
                    pygame.draw.circle(s,pc,(spx,spy),max(2,int(self.zoom*2.5)))

            tag=self.fonts["tiny"].render(comp["type"].upper(),True,tuple(min(255,c+70) for c in bord_c))
            s.blit(tag,(sr.x+5,sr.y+4))
            ns=self.fonts["label"].render(comp["name"] or "?",True,TEXT_BRIGHT)
            s.blit(ns,(sr.centerx-ns.get_width()//2, max(sr.y+3,sr.centery-ns.get_height()-1)))
            vs=self.fonts["small"].render(comp["value"] or "-",True,(200,220,160) if not is_hov else TEXT_BRIGHT)
            s.blit(vs,(sr.centerx-vs.get_width()//2, min(sr.bottom-vs.get_height()-2,sr.centery+2)))

        # top panel
        pygame.draw.rect(s,PANEL,(0,0,W,58)); pygame.draw.line(s,BORDER,(0,58),(W,58),1)
        draw_rrect(s,(35,40,55),self.btn_back,r=6,bw=1,bc=BORDER)
        bk=self.fonts["body"].render("< Back",True,TEXT_MAIN)
        s.blit(bk,(self.btn_back.centerx-bk.get_width()//2,self.btn_back.centery-bk.get_height()//2))

        has_errors = any(i[0]=="ERROR" for i in self.conn_issues)
        nl_f=(50,20,20) if has_errors else ((30,50,80) if self.show_netlist else (35,40,55))
        nl_c=(255,80,80) if has_errors else (ACCENT if self.show_netlist else BORDER)
        draw_rrect(s,nl_f,self.btn_netlist,r=6,bw=1,bc=nl_c)
        nl_label = f"Netlist ⚠{len(self.conn_issues)}" if has_errors else "Netlist"
        nb=self.fonts["body"].render(nl_label,True,(255,80,80) if has_errors else (ACCENT if self.show_netlist else TEXT_MAIN))
        s.blit(nb,(self.btn_netlist.centerx-nb.get_width()//2,self.btn_netlist.centery-nb.get_height()//2))

        ft=self.fonts["body"].render(os.path.basename(self.filepath),True,ACCENT)
        s.blit(ft,(W//2-ft.get_width()//2,18))

        stats=f"{len(self.components)} comps · {len(self.wires)} wires · {len(self.net_to_points)} nets"
        s.blit(self.fonts["small"].render(stats,True,TEXT_DIM),(W-self.fonts["small"].size(stats)[0]-14,20))

        # sim status pill
        sim_ok=bool(any(not k.startswith('__') for k in self.sim_data))
        pill_c=(40,160,70) if sim_ok else (160,60,40)
        pill_txt="SIM OK" if sim_ok else ("NO SIM" if not PYSPICE_OK else "SIM ERR")
        pygame.draw.rect(s,pill_c,(W-150,38,72,16),border_radius=4)
        ps=self.fonts["tiny"].render(pill_txt,True,TEXT_BRIGHT)
        s.blit(ps,(W-150+(72-ps.get_width())//2,40))

        # bottom left zvs plot
        zvs_x = 14
        zvs_y = H - self.zvs_plot.PH - 36
        total_frames = max(1, max(
            (len(v) for k,v in self.sim_data.items()
             if not k.startswith('__') and isinstance(v,list)),
            default=1))
        self.zvs_plot.draw(s, zvs_x, zvs_y, self.sim_frame, total_frames)

        # net labels under ZVS panel
        gn = getattr(self, '_zvs_gate_net',  None)
        dn = getattr(self, '_zvs_drain_net', None)
        if gn or dn:
            lab = self.fonts["tiny"].render(
                f"gate={gn or '?'}  drain={dn or '?'}", True, TEXT_DIM)
            s.blit(lab, (zvs_x + self.zvs_plot.PW//2 - lab.get_width()//2,
                         zvs_y + self.zvs_plot.PH + 2))

        # waveform panel
        if self.probed_net and self._sim_get(self.probed_net):
            self._draw_waveform(s)

        # netlist overlay
        if self.show_netlist:
            self._draw_netlist_panel(s)

        # tooltip
        self._draw_tooltip(s)

        # param panel (top-right, drawn last so it floats above canvas)
        if hasattr(self, 'param_panel'):
            self._tick_ms = self.clock.get_time() if hasattr(self,'clock') else 16
            self.param_panel.draw(s, self._tick_ms)

        hint=self.fonts["tiny"].render(
            "Scroll=zoom  Drag=pan  WASD/arrows=pan  click wire=probe net  ESC=back",True,TEXT_DIM)
        s.blit(hint,(W//2-hint.get_width()//2,H-18))

        if self.sim_data:
            max_frames=max((len(v) for k,v in self.sim_data.items() if not k.startswith('__') and isinstance(v,list)), default=1)
            self.sim_frame=(self.sim_frame+1)%max(1,max_frames)


    # sub panels
    def _draw_waveform(self,s):
        data=self._sim_get(self.probed_net)
        wx=W-self.WAVE_W-14; wy=H-self.WAVE_H-36
        pygame.draw.rect(s,(18,22,32),(wx,wy,self.WAVE_W,self.WAVE_H),border_radius=6)
        pygame.draw.rect(s,ACCENT,(wx,wy,self.WAVE_W,self.WAVE_H),1,border_radius=6)
        s.blit(self.fonts["tiny"].render(f"V({self.probed_net})",True,ACCENT),(wx+6,wy+4))
        d_min,d_max=min(data),max(data); d_rng=max(0.001,d_max-d_min)
        ix=wx+10; iw=self.WAVE_W-20; iy=wy+20; ih=self.WAVE_H-30
        if d_min<0<d_max:
            zy=iy+ih-int((0-d_min)/d_rng*ih)
            pygame.draw.line(s,(50,60,50),(ix,zy),(ix+iw,zy),1)
        n=len(data)
        pts=[(ix+i, iy+ih-int((data[int(i/iw*n)]-d_min)/d_rng*ih)) for i in range(iw)]
        if len(pts)>1: pygame.draw.lines(s,(80,220,120),False,pts,2)
        px_=ix+int((self.sim_frame%n)/n*iw)
        pygame.draw.line(s,(255,220,80),(px_,iy),(px_,iy+ih),1)
        fi=self.sim_frame%n
        cur=self.fonts["tiny"].render(f"{data[fi]:.3f} V",True,(200,255,160))
        s.blit(cur,(wx+self.WAVE_W-cur.get_width()-6,wy+4))
        # min/max labels
        s.blit(self.fonts["tiny"].render(f"{d_max:.2f}",True,TEXT_DIM),(ix,iy))
        s.blit(self.fonts["tiny"].render(f"{d_min:.2f}",True,TEXT_DIM),(ix,iy+ih-12))

    def _draw_netlist_panel(self,s):
        pw,ph=760,500; px,py=W//2-pw//2,H//2-ph//2
        pygame.draw.rect(s,(16,20,30),(px,py,pw,ph),border_radius=8)
        pygame.draw.rect(s,ACCENT,(px,py,pw,ph),1,border_radius=8)

        title_txt = "Connectivity Diagnostics" if self.conn_issues else "Net Map  /  Sim Status"
        s.blit(self.fonts["body"].render(title_txt,True,ACCENT),(px+12,py+10))
        pygame.draw.line(s,BORDER,(px,py+34),(px+pw,py+34),1)
        y_off=py+40; line_h=14

        def emit(text, color=TEXT_MAIN):
            nonlocal y_off
            if y_off > py+ph-22: return
            s.blit(self.fonts["tiny"].render(text,True,color),(px+10,y_off))
            y_off+=line_h

        if self.conn_issues:
            emit("─── CONNECTIVITY ISSUES ─────────────────────────────────────", (100,120,160))
            for sev,cname,msg in self.conn_issues:
                col=(255,80,80) if sev=="ERROR" else ACCENT2
                emit(f"  [{sev}] {cname}: {msg}", col)
            emit("")

        # per-component pin assignments
        emit("─── PIN → NET ASSIGNMENTS  (world_pos → snapped_wire_pt, dist) ─", (100,120,160))
        for comp, nets in self.pin_assignments:
            dbg = comp.get("_pin_debug", {})
            # header row
            has_issue = any(
                (sev, comp["name"]) in [(i[0],i[1]) for i in self.conn_issues]
                for sev,_,_ in self.conn_issues
                if _ == comp["name"]
            )
            hcol = (255,80,80) if any(i[1]==comp["name"] and i[0]=="ERROR"
                                      for i in self.conn_issues) else TEXT_MAIN
            emit(f"  {comp['type'].upper():10s} {comp['name']:8s}  rot={comp['rot']:5s}  val={comp['value']}", hcol)
            for pin, net in nets.items():
                info = dbg.get(pin, ())
                if len(info) >= 6:
                    px_w,py_w,dist,_,wx,wy = info
                    snap_txt = f"wire@({wx},{wy}) dist={dist}" if wx is not None else "NO WIRE IN SNAP RANGE"
                    snap_col = TEXT_DIM if wx is not None else (255,80,80)
                    net_str  = net or "⚠ UNCONNECTED"
                    net_col  = (255,80,80) if net is None else \
                               (255,220,80 if net=="0" else 0) if net=="0" else (140,220,140)
                    emit(f"      pin {pin:3s}  world=({px_w:5.0f},{py_w:5.0f})  →  {snap_txt}", snap_col)
                    emit(f"             net = {net_str}", net_col)
                else:
                    emit(f"      pin {pin}: {net}", TEXT_DIM)

        # match net names to ngspice keys
        emit("")
        emit("─── NGSPICE NODES RETURNED vs ASC NET NAMES ─────────────────", (100,120,160))
        raw_keys = self.sim_data.get("__raw_keys__", {})
        our_nets = sorted(self.net_to_points.keys())
        if raw_keys:
            emit(f"  ngspice returned {len(raw_keys)} nodes:", TEXT_DIM)
            for lo, orig in sorted(raw_keys.items()):
                matched = lo in [n.lower() for n in our_nets]
                col = (100,220,100) if matched else (255,200,60)
                mark = "✓" if matched else "? no .asc match"
                emit(f"    {orig:20s}  {mark}", col)
        else:
            emit("  (no ngspice node data – sim may not have run)", (255,120,60))
        emit("")
        emit(f"  Our .asc nets : {', '.join(our_nets)}", TEXT_DIM)
        emit("")
        emit(f"  .tran  : {self.tran_cmd or '(auto 1µs / 1ms)'}", TEXT_DIM)
        has_real = any(not k.startswith('__') for k in self.sim_data)
        status_col=(80,200,80) if has_real else (255,100,60)
        emit(f"  status : {self.sim_status}", status_col)

        s.blit(self.fonts["tiny"].render("ESC or click Netlist to close",True,TEXT_DIM),
               (px+pw-220,py+ph-18))

    def _draw_tooltip(self,s):
        rows=[]
        if self.hovered_comp:
            c=self.hovered_comp; pn=self._pin_nets(c)
            rows=[("Type",c["type"].upper()),("Name",c["name"] or "-"),("Val",c["value"] or "-")]
            for pin,net in pn.items():
                vc=""
                _pd = self._sim_get(net)
                if _pd:
                    fi=self.sim_frame%len(_pd)
                    vc=f"  {_pd[fi]:.3f}V"
                rows.append((f"  {pin}",net+vc))
        elif self.hovered_net:
            rows=[("Net",self.hovered_net)]
            _hd = self._sim_get(self.hovered_net)
            if _hd:
                d=_hd; fi=self.sim_frame%len(d)
                rows+=[("V",f"{d[fi]:.4f} V"),("Click","to probe waveform")]
            else:
                rows.append(("(no sim data)","click to probe"))
        if not rows: return
        tw=260; th=10+len(rows)*20; mx,my=pygame.mouse.get_pos()
        tx=min(mx+16,W-tw-6); ty=min(my+16,H-th-6)
        pygame.draw.rect(s,(16,20,30),(tx,ty,tw,th),border_radius=5)
        pygame.draw.rect(s,ACCENT,(tx,ty,tw,th),1,border_radius=5)
        for j,(lbl,val) in enumerate(rows):
            ls=self.fonts["tiny"].render(lbl+": ",True,TEXT_DIM)
            vs=self.fonts["tiny"].render(val,True,TEXT_BRIGHT)
            yy=ty+6+j*20; s.blit(ls,(tx+6,yy)); s.blit(vs,(tx+6+ls.get_width(),yy))


class App:
    def __init__(self):
        pygame.init()
        self.screen=pygame.display.set_mode((W,H))
        pygame.display.set_caption("LTspice Viewer + PySpice/ngspice")
        self.fonts=self._build_fonts(); self.clock=pygame.time.Clock()
        self.browser=FileBrowser(self.screen,self.fonts)
        self.browser.open_cb=self._open_file; self.active=self.browser

    def _build_fonts(self):
        def sf(names,size):
            for n in names:
                try:
                    f=pygame.font.SysFont(n,size)
                    if f: return f
                except Exception: pass
            return pygame.font.Font(None,size)
        mono=["consolas","couriernew","lucidaconsole","monospace"]
        return {"title":sf(mono,30),"body":sf(mono,20),"mono":sf(mono,18),
                "label":sf(mono,15),"small":sf(mono,13),"tiny":sf(mono,11)}

    def _open_file(self,filepath):
        view=SchematicView(self.screen,self.fonts,filepath)
        view.clock   = self.clock 
        view.back_cb=self._go_back; self.active=view

    def _go_back(self):
        self.browser.files=list_asc_files(); self.browser.selected=None
        self.browser.scroll=0; self.active=self.browser

    def run(self):
        while True:
            for event in pygame.event.get():
                if event.type==pygame.QUIT: pygame.quit(); sys.exit()
                self.active.handle_event(event)
            self.active.draw(); pygame.display.flip(); self.clock.tick(60)


if __name__=="__main__":
    App().run()