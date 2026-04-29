import subprocess
import os
import re
import sys
import time
import threading
import shutil
from concurrent.futures import ProcessPoolExecutor
import pygame

W, H = 1060, 780

C_BG       = (235, 236, 240)
C_PANEL    = (220, 222, 228)
C_PANEL2   = (210, 212, 218)
C_BORDER   = (170, 172, 182)
C_ACCENT   = (30,  100, 210)
C_ITER_BG  = (205, 220, 245)
C_ITER_BD  = (30,  100, 210)
C_STA_BG   = (225, 226, 232)
C_STA_BD   = (160, 162, 175)
C_TEXT     = (25,  25,  35)
C_DIM      = (100, 102, 118)
C_GREEN    = (30,  140, 70)
C_GREEN_BG = (195, 235, 210)
C_RED      = (180, 40,  40)
C_RED_BG   = (245, 210, 210)
C_YELLOW   = (160, 110, 0)
C_WHITE    = (255, 255, 255)
C_HOVER    = (200, 210, 235)

RES_Y = 490

def read_netlist(net_path):
    """Read a LTspice .net file detecting UTF-16 or UTF-8 encoding.
    Returns (content_str, encoding_str) so we can write back in the same encoding."""
    with open(net_path, "rb") as f:
        raw = f.read()
    for enc in ["utf-16", "utf-16-le", "utf-16-be", "utf-8"]:
        try:
            text = raw.decode(enc)
            if any(kw in text.lower() for kw in [".param", ".tran", ".subckt", "version"]):
                return text, enc
        except Exception:
            continue
    return raw.decode("utf-8", errors="ignore"), "utf-8"

def find_ltspice():
    for p in [
        r"C:\Users\{}\AppData\Local\Programs\ADI\LTspice\LTspice.exe".format(os.getlogin()),
        r"C:\Program Files\LTC\LTspiceXVII\LTspiceXVII.exe",
        r"C:\Program Files\ADI\LTspice\LTspice.exe",
    ]:
        if os.path.exists(p): return p
    return None

def find_asc_files():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    files = sorted(f for f in os.listdir(script_dir) if f.lower().endswith(".asc"))
    return script_dir, files

def parse_params(sch_path):
    params = []
    seen   = set()
    if not os.path.exists(sch_path):
        return params
    with open(sch_path, "r", errors="ignore") as f:
        for line in f:
            stripped = re.sub(r"^TEXT\s+[^\!]*!", "", line, flags=re.IGNORECASE).strip()
            for m in re.finditer(r"\.param\s+(\w+)\s*=\s*([^\s;,\\]+)", stripped, re.IGNORECASE):
                name = m.group(1)
                if name not in seen:
                    params.append((name, m.group(2).strip()))
                    seen.add(name)
    return params

def split_value_unit(val_str):
    m = re.match(r"^([0-9]*\.?[0-9]+(?:[eE][+\-]?\d+)?)([a-zA-Z]*)$", val_str.strip())
    if m: return float(m.group(1)), m.group(2)
    return 0.0, ""

def fmt_val(num, unit):
    s = str(int(num)) if num == int(num) else str(round(num, 9)).rstrip("0")
    return f"{s}{unit}"

def wait_for_log(log_path, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(log_path):
            time.sleep(3)
            return True
        time.sleep(3)
    return False

def run_sim(iter_name, iter_val, iter_unit, static_overrides,
            ltspice_exe, netlist_content, net_enc, out_dir, script_dir):

    tag      = re.sub(r"[^a-zA-Z0-9_]", "_", f"{iter_name}_{iter_val}{iter_unit}")
    temp_net = os.path.join(script_dir, f"_sweep_{tag}.net")
    temp_log = os.path.join(script_dir, f"_sweep_{tag}.log")

    if os.path.exists(temp_log):
        os.remove(temp_log)

    net = netlist_content

    # substitute iterated param — stop at whitespace or backslash (literal \n separator)
    net = re.sub(
        rf"(\.param\s+{re.escape(iter_name)}\s*=)\s*[^\s\\\n\r]+",
        rf"\g<1>{fmt_val(iter_val, iter_unit)}", net, flags=re.IGNORECASE,
    )
    # substitute static params
    for name, val_str in static_overrides.items():
        net = re.sub(
            rf"(\.param\s+{re.escape(name)}\s*=)\s*[^\s\\\n\r]+",
            rf"\g<1>{val_str}", net, flags=re.IGNORECASE,
        )

    # write back in the SAME encoding LTspice used
    with open(temp_net, "w", encoding=net_enc, errors="replace") as f:
        f.write(net)

    subprocess.run([ltspice_exe, "-Run", "-b", temp_net], check=True)

    if not wait_for_log(temp_log):
        return iter_val, iter_unit, "N/A", "N/A", "N/A", "N/A"

    result = _parse_log(temp_log)

    # archive log then clean up temp files
    archive_log = os.path.join(out_dir, f"{tag}.log")
    if os.path.exists(temp_log):
        shutil.copy(temp_log, archive_log)
        os.remove(temp_log)
    if os.path.exists(temp_net):
        os.remove(temp_net)

    return (iter_val, iter_unit) + result

def _parse_log(log_path):
    pin = pout = eff = thd = "N/A"
    for _ in range(10):
        if os.path.exists(log_path) and os.path.getsize(log_path) > 100: break
        time.sleep(0.5)
    if not os.path.isfile(log_path): return pin, pout, eff, thd
    with open(log_path, "rb") as f:
        raw = f.read()
    content = None
    for enc in ["utf-16", "utf-16-le", "utf-16-be", "utf-8"]:
        try:
            dec = raw.decode(enc)
            if "LTspice" in dec: content = dec; break
        except Exception: continue
    if content is None: return pin, pout, eff, thd

    def get_val(name, text):
        for pat in [rf"^{name}\b.*?=\s*([0-9\.eE\+\-]+)",
                    rf"^{name}\b.*?AVG.*?=\s*([0-9\.eE\+\-]+)"]:
            m = re.search(pat, text, re.MULTILINE | re.IGNORECASE)
            if m: return m.group(1)
        return "N/A"

    pin  = get_val("Pin_Watts",  content)
    pout = get_val("Pout_Watts", content)
    eff  = get_val("System_Eff", content)
    psw = get_val("Pswitch", content)
    swpct = get_val("Switch_Loss_Pct", content)
    m = re.search(r"Total Harmonic Distortion:\s*([0-9\.eE\+\-]+)%", content, re.IGNORECASE)
    if m: thd = m.group(1)
    return pin, pout, eff, thd, psw, swpct


def sweep_thread(app):
    app.status   = "Exporting netlist…"
    app.results  = []
    app.progress = 0

    ltspice_exe = find_ltspice()
    if not ltspice_exe:
        app.status = "ERROR: LTspice not found"; app.running = False; return

    script_dir = os.path.dirname(os.path.abspath(__file__))
    out_dir    = os.path.join(script_dir, "runs")
    os.makedirs(out_dir, exist_ok=True)

    sch_path = os.path.join(script_dir, app.asc_file)
    try:
        subprocess.run([ltspice_exe, "-netlist", sch_path], check=True)
    except Exception as e:
        app.status = f"ERROR: {e}"; app.running = False; return

    net_file = sch_path.replace(".asc", ".net")
    netlist_content, net_enc = read_netlist(net_file)
    app.status = f"Netlist read OK  (encoding: {net_enc})"

    iter_idx            = app.iter_idx
    iter_name, iter_raw = app.params[iter_idx]
    _, iter_unit        = split_value_unit(iter_raw)

    start   = app.box_start.get_float()
    stop    = app.box_stop.get_float()
    inc     = app.box_inc.get_float(1.0)
    workers = app.box_workers.get_int(4)

    static_overrides = {
        name: app.static_boxes[i].text.strip()
        for i, (name, _) in enumerate(app.params)
        if i != iter_idx
    }

    vals, v = [], start
    while v <= stop + 1e-9:
        vals.append(round(v, 9)); v += inc
    app.total = len(vals)

    with ProcessPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(run_sim, iter_name, v, iter_unit,
                      static_overrides, ltspice_exe, netlist_content,
                      net_enc, out_dir, script_dir): v
            for v in vals
        }
        raw = {}
        for fut in futures:
            v = futures[fut]
            try:   raw[v] = fut.result()
            except Exception as e:
                raw[v] = (v, iter_unit, "N/A", "N/A", "N/A", "N/A", "N/A", "N/A")
            app.progress += 1
            app.status = f"Completed {app.progress} / {app.total}"

    for v in sorted(raw):
        pval, u, pin, pout, eff, thd, psw, swpct = raw[v]
        app.results.append((fmt_val(pval, u), pin, pout, eff, thd, psw, swpct))

    summary = os.path.join(out_dir, "summary.txt")
    with open(summary, "w") as f:
        f.write(f"{iter_name:<14} | {'Pin(W)':<16} | {'Pout(W)':<16} | {'Eff(%)':<16} | THD(%)\n")
        f.write("-" * 76 + "\n")
        for row in app.results:
            f.write(" | ".join(f"{c:<16}" for c in row) + "\n")

    app.status  = f"Done  →  {summary}"
    app.running = False


class InputBox:
    def __init__(self, x, y, w, h, text=""):
        self.rect   = pygame.Rect(x, y, w, h)
        self.text   = str(text)
        self.active = False

    def handle(self, event):
        if event.type == pygame.MOUSEBUTTONDOWN:
            self.active = self.rect.collidepoint(event.pos)
        if event.type == pygame.KEYDOWN and self.active:
            if event.key == pygame.K_BACKSPACE: self.text = self.text[:-1]
            elif event.key not in (pygame.K_RETURN, pygame.K_TAB, pygame.K_ESCAPE):
                self.text += event.unicode

    def draw(self, surf, font, bg=C_WHITE, border=C_BORDER):
        pygame.draw.rect(surf, bg, self.rect, border_radius=5)
        pygame.draw.rect(surf, C_ACCENT if self.active else border,
                         self.rect, 2 if self.active else 1, border_radius=5)
        txt = font.render(self.text, True, C_TEXT)
        surf.blit(txt, (self.rect.x + 7, self.rect.y + (self.rect.h - txt.get_height()) // 2))

    def get_float(self, default=0.0):
        try:   return float(self.text)
        except: return default

    def get_int(self, default=1):
        try:   return int(self.text)
        except: return default


class App:
    def __init__(self):
        pygame.init()
        self.screen = pygame.display.set_mode((W, H))
        pygame.display.set_caption("LTspice Parameter Sweep")
        self.clock  = pygame.time.Clock()

        self.f_sm = pygame.font.SysFont("Consolas", 13)
        self.f_md = pygame.font.SysFont("Consolas", 15)
        self.f_lg = pygame.font.SysFont("Consolas", 18, bold=True)
        self.f_xl = pygame.font.SysFont("Consolas", 22, bold=True)

        self.screen_mode  = "picker"
        self.script_dir, self.asc_files = find_asc_files()
        self.asc_hover    = -1
        self.asc_file     = None
        self.file_rects   = []

        self.params       = []
        self.iter_idx     = None
        self.static_boxes = []

        self.box_start   = InputBox(0, 0, 110, 26, "")
        self.box_stop    = InputBox(0, 0, 110, 26, "")
        self.box_inc     = InputBox(0, 0, 110, 26, "1")
        self.box_workers = InputBox(0, 0, 110, 26, "6")

        self.running    = False
        self.status     = ""
        self.results    = []
        self.progress   = 0
        self.total      = 0
        self.res_scroll = 0

        self.param_row_rects = []
        self.run_rect        = None
        self.back_rect       = None

    def load_asc(self, filename):
        self.asc_file = filename
        sch_path      = os.path.join(self.script_dir, filename)
        self.params   = parse_params(sch_path)
        if not self.params:
            self.params = [("rload", "50")]

        self.iter_idx     = None
        self.static_boxes = []
        for name, val in self.params:
            self.static_boxes.append(InputBox(0, 0, 110, 26, val))

        self.results     = []
        self.progress    = 0
        self.total       = 0
        self.res_scroll  = 0
        self.running     = False
        self.status      = "Click a parameter row to select it for iteration  —  others stay static"
        self.screen_mode = "sweep"

    def select_param(self, idx):
        self.iter_idx = idx
        name, val_str = self.params[idx]
        num, unit = split_value_unit(val_str)
        self.box_start.text = fmt_val(num - 5, "")
        self.box_stop.text  = fmt_val(num + 5, "")
        self.status = (f"Iterating  .param {name}  "
                       f"({fmt_val(num-5,'')} → {fmt_val(num+5,'')} {unit})  "
                       f"|  other params locked to static values")

    def handle_events(self):
        mx, my = pygame.mouse.get_pos()
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit(); sys.exit()

            if self.screen_mode == "picker":
                if event.type == pygame.MOUSEBUTTONDOWN:
                    for i, r in enumerate(self.file_rects):
                        if r.collidepoint(event.pos):
                            self.load_asc(self.asc_files[i])

            elif self.screen_mode == "sweep":
                if event.type == pygame.MOUSEWHEEL and my > RES_Y:
                    self.res_scroll = max(0, self.res_scroll - event.y)

                if event.type == pygame.MOUSEBUTTONDOWN:
                    if self.back_rect and self.back_rect.collidepoint(event.pos):
                        self.screen_mode = "picker"; return

                    if not self.running:
                        for i, r in enumerate(self.param_row_rects):
                            if r.collidepoint(event.pos):
                                if not self.static_boxes[i].rect.collidepoint(event.pos):
                                    self.select_param(i)
                        if (self.run_rect and self.run_rect.collidepoint(event.pos)
                                and self.iter_idx is not None):
                            self.running = True
                            threading.Thread(target=sweep_thread, args=(self,), daemon=True).start()

                for box in [self.box_start, self.box_stop, self.box_inc, self.box_workers]:
                    box.handle(event)
                for i, box in enumerate(self.static_boxes):
                    if i != self.iter_idx:
                        box.handle(event)

        if self.screen_mode == "picker":
            self.asc_hover = -1
            for i, r in enumerate(self.file_rects):
                if r.collidepoint(mx, my):
                    self.asc_hover = i

    def draw_picker(self):
        s = self.screen
        s.fill(C_BG)
        s.blit(self.f_xl.render("LTspice Parameter Sweep", True, C_TEXT), (30, 28))
        s.blit(self.f_md.render("Select a schematic to simulate:", True, C_DIM), (30, 62))
        pygame.draw.line(s, C_BORDER, (0, 84), (W, 84), 2)

        self.file_rects = []

        if not self.asc_files:
            s.blit(self.f_md.render(
                f"No .asc files found in:  {self.script_dir}", True, C_RED), (30, 110))
            pygame.display.flip(); return

        s.blit(self.f_sm.render(f"Directory:  {self.script_dir}", True, C_DIM), (30, 92))

        for i, fname in enumerate(self.asc_files):
            y = 120 + i * 58
            r = pygame.Rect(30, y, W - 60, 48)
            self.file_rects.append(r)
            pygame.draw.rect(s, C_HOVER if i == self.asc_hover else C_PANEL, r, border_radius=8)
            pygame.draw.rect(s, C_BORDER, r, 1, border_radius=8)
            pygame.draw.rect(s, C_ACCENT, (r.x + 12, r.y + 10, 28, 28), border_radius=4)
            s.blit(self.f_sm.render(".asc", True, C_WHITE), (r.x + 14, r.y + 18))
            s.blit(self.f_lg.render(fname, True, C_TEXT), (r.x + 52, r.y + 8))
            p = parse_params(os.path.join(self.script_dir, fname))
            sub = (f"{len(p)} param(s): " + ",  ".join(f"{n}={v}" for n, v in p)) if p else "no .param lines found"
            s.blit(self.f_sm.render(sub, True, C_DIM), (r.x + 52, r.y + 30))

        pygame.display.flip()

    def draw_sweep(self):
        s = self.screen
        s.fill(C_BG)

        LEFT_X = 10
        ROW_H  = 56
        TOP_Y  = 52
        NAME_W = 240
        ROLE_W = 310
        CFG_X  = LEFT_X + NAME_W + ROLE_W + 20

        pygame.draw.rect(s, C_PANEL, (0, 0, W, 44))
        pygame.draw.line(s, C_BORDER, (0, 44), (W, 44), 2)

        self.back_rect = pygame.Rect(8, 7, 90, 30)
        pygame.draw.rect(s, C_BG, self.back_rect, border_radius=5)
        pygame.draw.rect(s, C_BORDER, self.back_rect, 1, border_radius=5)
        s.blit(self.f_sm.render("◀  Back", True, C_ACCENT),
               (self.back_rect.x + 10, self.back_rect.y + 8))
        s.blit(self.f_lg.render(self.asc_file or "", True, C_TEXT), (112, 12))

        s.blit(self.f_lg.render("Parameter",             True, C_TEXT), (LEFT_X + 8,          TOP_Y - 28))
        s.blit(self.f_lg.render("Role  /  Static Value", True, C_TEXT), (LEFT_X + NAME_W + 8, TOP_Y - 28))
        s.blit(self.f_lg.render("Sweep Config",          True, C_TEXT), (CFG_X,               TOP_Y - 28))
        pygame.draw.line(s, C_BORDER, (LEFT_X + NAME_W + 4, 44), (LEFT_X + NAME_W + 4, RES_Y), 1)
        pygame.draw.line(s, C_BORDER, (CFG_X - 12, 44), (CFG_X - 12, RES_Y), 1)

        self.param_row_rects = []

        for i, (name, raw_val) in enumerate(self.params):
            y       = TOP_Y + i * ROW_H
            is_iter = (i == self.iter_idx)
            _, unit = split_value_unit(raw_val)

            row_r = pygame.Rect(LEFT_X, y, NAME_W + ROLE_W + 8, ROW_H - 4)
            self.param_row_rects.append(row_r)
            pygame.draw.rect(s, C_ITER_BG if is_iter else C_STA_BG, row_r, border_radius=6)
            pygame.draw.rect(s, C_ITER_BD if is_iter else C_STA_BD, row_r,
                             2 if is_iter else 1, border_radius=6)

            s.blit(self.f_md.render(f".param {name}", True, C_TEXT), (LEFT_X + 10, y + 8))
            s.blit(self.f_sm.render(f"default: {raw_val}", True, C_DIM), (LEFT_X + 10, y + 28))

            mid_x = LEFT_X + NAME_W + 14
            if is_iter:
                badge = pygame.Rect(mid_x, y + 8, 148, 26)
                pygame.draw.rect(s, C_ACCENT, badge, border_radius=4)
                s.blit(self.f_md.render("▶  ITERATING", True, C_WHITE), (badge.x + 8, badge.y + 5))
                s.blit(self.f_sm.render(f"start / stop below  (unit: {unit or '#'})", True, C_ACCENT),
                       (mid_x, y + 38))
            else:
                badge = pygame.Rect(mid_x, y + 9, 82, 22)
                pygame.draw.rect(s, C_PANEL, badge, border_radius=4)
                pygame.draw.rect(s, C_BORDER, badge, 1, border_radius=4)
                s.blit(self.f_sm.render("STATIC", True, C_DIM), (badge.x + 8, badge.y + 4))
                box = self.static_boxes[i]
                box.rect = pygame.Rect(mid_x + 96, y + 8, 120, 26)
                box.draw(s, self.f_md, bg=C_WHITE, border=C_BORDER)
                s.blit(self.f_sm.render("value →", True, C_DIM), (mid_x + 96 - 54, y + 14))
                if self.iter_idx is None:
                    s.blit(self.f_sm.render("click row to iterate", True, C_DIM), (mid_x, y + 36))

        iter_unit = ""
        if self.iter_idx is not None:
            _, iter_unit = split_value_unit(self.params[self.iter_idx][1])

        for j, (lbl, box) in enumerate(zip(
            [f"Iter start  ({iter_unit or '#'})", f"Iter stop   ({iter_unit or '#'})",
             "Increment", "Workers"],
            [self.box_start, self.box_stop, self.box_inc, self.box_workers]
        )):
            fy = TOP_Y + j * 56
            s.blit(self.f_sm.render(lbl, True, C_DIM), (CFG_X, fy + 4))
            box.rect = pygame.Rect(CFG_X, fy + 22, 130, 28)
            box.draw(s, self.f_md, bg=C_WHITE, border=C_BORDER)

        self.run_rect = pygame.Rect(CFG_X, TOP_Y + 4 * 56 + 8, 210, 44)
        if self.running:
            bc, bg2, bl, tc = C_RED,    C_RED_BG,   "⏳  RUNNING…",  C_RED
        elif self.iter_idx is not None:
            bc, bg2, bl, tc = C_GREEN,  C_GREEN_BG, "▶   RUN SWEEP", C_GREEN
        else:
            bc, bg2, bl, tc = C_BORDER, C_PANEL,    "▶   RUN SWEEP", C_DIM
        pygame.draw.rect(s, bg2, self.run_rect, border_radius=8)
        pygame.draw.rect(s, bc,  self.run_rect, 2, border_radius=8)
        s.blit(self.f_md.render(bl, True, tc), (self.run_rect.x + 16, self.run_rect.y + 13))

        BAR_X, BAR_Y, BAR_W = CFG_X, self.run_rect.bottom + 14, 330
        if self.total > 0:
            pygame.draw.rect(s, C_BORDER, (BAR_X, BAR_Y, BAR_W, 9), border_radius=4)
            fill = int(BAR_W * self.progress / self.total)
            if fill > 0:
                pygame.draw.rect(s, C_ACCENT, (BAR_X, BAR_Y, fill, 9), border_radius=4)
            s.blit(self.f_sm.render(f"{int(100*self.progress/self.total)}%", True, C_DIM),
                   (BAR_X + BAR_W + 8, BAR_Y - 2))

        s.blit(self.f_sm.render(self.status, True, C_YELLOW), (LEFT_X, RES_Y - 22))

        pygame.draw.line(s, C_BORDER, (0, RES_Y), (W, RES_Y), 2)
        pygame.draw.rect(s, C_PANEL2, (0, RES_Y, W, H - RES_Y))

        if self.iter_idx is not None:
            hdr = f"{'Val':<13} | {'Pin(W)':<12} | {'Pout(W)':<12} | {'Eff(%)':<10} | {'THD(%)':<10} | {'Psw(W)':<10} | SwLoss%"
            s.blit(self.f_sm.render(hdr, True, C_ACCENT), (12, RES_Y + 8))
            pygame.draw.line(s, C_BORDER, (8, RES_Y + 26), (W - 8, RES_Y + 26), 1)
            rows_vis = (H - RES_Y - 34) // 20
            for k, row in enumerate(self.results[self.res_scroll: self.res_scroll + rows_vis]):
                line = f"{row[0]:<13} | {row[1]:<12} | {row[2]:<12} | {row[3]:<10} | {row[4]:<10} | {row[5]:<10} | {row[6]}"
                s.blit(self.f_sm.render(line, True, C_TEXT if k % 2 == 0 else C_DIM),
                       (12, RES_Y + 30 + k * 20))
            if len(self.results) > rows_vis:
                s.blit(self.f_sm.render(
                    f"scroll ↑↓   {self.res_scroll+1}–"
                    f"{min(self.res_scroll+rows_vis, len(self.results))} of {len(self.results)}",
                    True, C_DIM), (W - 280, RES_Y + 8))
        else:
            s.blit(self.f_sm.render("Results appear here after the sweep completes.", True, C_DIM),
                   (12, RES_Y + 12))

        pygame.display.flip()

    def run(self):
        while True:
            self.handle_events()
            if self.screen_mode == "picker":
                self.draw_picker()
            else:
                self.draw_sweep()
            self.clock.tick(30)

if __name__ == "__main__":
    App().run()