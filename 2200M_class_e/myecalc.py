import pygame
import json
import math

pygame.init()

WIDTH = 900
HEIGHT = 600
screen = pygame.display.set_mode((WIDTH, HEIGHT))
pygame.display.set_caption("Class-E Calculator")

font = pygame.font.SysFont(None, 28)
clock = pygame.time.Clock()

data_model = {
    "inputs": {
        "Vcc": "",
        "Coss_pf": "",
        "Freq_Hz": "",
        "Pout_W": "",
        "L1_uH": ""
    },
    "outputs": {
        "Rload_ohms": 0,
        "C1_shunt_pF": 0,
        "C2_tank_pF": 0,
        "L2_tank_uH": 0,
        "XL2_minus_XC2_ohms": 0,
        "Estimated_DC_current_A": 0
    }
}

class InputBox:
    def __init__(self, x, y, w, h, key):
        self.rect = pygame.Rect(x, y, w, h)
        self.color = (200, 200, 200)
        self.text = ""
        self.active = False
        self.key = key

    def handle_event(self, event):
        if event.type == pygame.MOUSEBUTTONDOWN:
            self.active = self.rect.collidepoint(event.pos)
        if event.type == pygame.KEYDOWN and self.active:
            if event.key == pygame.K_BACKSPACE:
                self.text = self.text[:-1]
            else:
                self.text += event.unicode
            data_model["inputs"][self.key] = self.text

    def draw(self, surface):
        pygame.draw.rect(surface, self.color, self.rect, 2)
        txt_surface = font.render(self.text, True, (255, 255, 255))
        surface.blit(txt_surface, (self.rect.x + 5, self.rect.y + 5))

def safe_float(value):
    try:
        return float(value)
    except:
        return None

def calculate():
    Vcc = safe_float(data_model["inputs"]["Vcc"])
    Coss_pf = safe_float(data_model["inputs"]["Coss_pf"])
    Freq = safe_float(data_model["inputs"]["Freq_Hz"])
    Pout = safe_float(data_model["inputs"]["Pout_W"])
    L1_uH = safe_float(data_model["inputs"]["L1_uH"])

    if None in (Vcc, Coss_pf, Freq, Pout, L1_uH):
        return
    if Vcc <= 0 or Freq <= 0 or Pout <= 0 or L1_uH <= 0:
        return

    pi = math.pi
    w = 2.0 * pi * Freq
    Coss = Coss_pf * 1e-12
    L1 = L1_uH * 1e-6

    Rload = (8.0 / (pi * pi + 4.0)) * (Vcc * Vcc) / Pout

    k = (w * L1) / Rload

    if k <= 0:
        return

    C1_total = (0.1836 / (w * Rload)) * (1.0 / (1.0 + 1.0 / k))

    C1_ext = C1_total - Coss
    if C1_ext < 0.0:
        C1_ext = 0.0

    Q_L = 5.0
    L2 = (Q_L * Rload) / w

    Xc2 = Rload * (Q_L - 1.1525)
    if Xc2 <= 0.0:
        C2 = 0.0
    else:
        C2 = 1.0 / (w * Xc2)

    XL2 = w * L2
    XC2 = 0.0
    if C2 > 0.0:
        XC2 = 1.0 / (w * C2)

    data_model["outputs"]["Rload_ohms"] = Rload
    data_model["outputs"]["C1_shunt_pF"] = C1_ext * 1e12
    data_model["outputs"]["C2_tank_pF"] = C2 * 1e12
    data_model["outputs"]["L2_tank_uH"] = L2 * 1e6
    data_model["outputs"]["XL2_minus_XC2_ohms"] = XL2 - XC2
    data_model["outputs"]["Estimated_DC_current_A"] = Pout / Vcc

input_boxes = []
y_start = 50
for key in data_model["inputs"]:
    input_boxes.append(InputBox(250, y_start, 200, 32, key))
    y_start += 60

running = True
while running:
    clock.tick(60)
    screen.fill((30, 30, 30))
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False
        for box in input_boxes:
            box.handle_event(event)

    calculate()

    y = 50
    for key in data_model["inputs"]:
        label = font.render(key, True, (255, 255, 255))
        screen.blit(label, (50, y))
        y += 60

    for box in input_boxes:
        box.draw(screen)

    y = 50
    for key, value in data_model["outputs"].items():
        text = key + " : " + "{:.6f}".format(value)
        out_surface = font.render(text, True, (0, 255, 0))
        screen.blit(out_surface, (500, y))
        y += 40

    pygame.display.flip()

pygame.quit()