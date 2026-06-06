"""Grafische Darstellungen fuer Flo (mit Pillow, ohne Netz/Downloads).

Erzeugt fertige PNGs als ``io.BytesIO``, die direkt als ``discord.File``
verschickt werden koennen:

- ``blackjack_table(dealer, player, ...)`` -> Casino-Tisch mit echten Karten.
- ``crash_chart(crash_point, target, cashed)`` -> Multiplikator-Kurve.

Alles wird selbst gezeichnet (keine externen Bild-Dateien), damit es auch auf
einem frisch aufgesetzten Server ohne zusaetzliche Assets funktioniert.
"""
from __future__ import annotations

import io
import math

from PIL import Image, ImageDraw, ImageFont

# --- Schriften -----------------------------------------------------------
# DejaVu Sans deckt die Kartensymbole ♠♥♦♣ und × ab. Mehrere Pfade, damit es
# sowohl auf Fedora (dein PC) als auch auf Debian/Ubuntu (Server) klappt.
_FONT_PATHS = [
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/liberation-sans-fonts/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
_FONT_FILE: str | None = None
for _p in _FONT_PATHS:
    try:
        ImageFont.truetype(_p, 12)
        _FONT_FILE = _p
        break
    except OSError:
        continue

_font_cache: dict[int, ImageFont.FreeTypeFont] = {}


def _font(size: int) -> ImageFont.FreeTypeFont:
    f = _font_cache.get(size)
    if f is None:
        if _FONT_FILE:
            f = ImageFont.truetype(_FONT_FILE, size)
        else:  # Notnagel - sollte praktisch nie passieren
            f = ImageFont.load_default()
        _font_cache[size] = f
    return f


def _png(img: Image.Image) -> io.BytesIO:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf


# --- Farben --------------------------------------------------------------
_RED = (197, 32, 48)
_BLACK = (24, 26, 33)
_FELT_TOP = (18, 116, 72)
_FELT_BOT = (8, 74, 46)
_WHITE = (244, 246, 250)
_GREEN = (46, 204, 113)
_RED_HOT = (231, 76, 60)
_GOLD = (241, 196, 15)
_INK = (13, 17, 23)


# --- kleine Zeichen-Helfer ----------------------------------------------
def _vgrad(w: int, h: int, top: tuple, bot: tuple) -> Image.Image:
    """Vertikaler Farbverlauf (ohne numpy)."""
    img = Image.new("RGB", (w, h), top)
    d = ImageDraw.Draw(img)
    for y in range(h):
        f = y / max(1, h - 1)
        col = tuple(round(top[i] + (bot[i] - top[i]) * f) for i in range(3))
        d.line([(0, y), (w, y)], fill=col)
    return img


def _pill(d: ImageDraw.ImageDraw, x: int, y: int, text: str, size: int,
          bg: tuple, fg: tuple) -> int:
    """Zeichnet eine abgerundete 'Pille' mit Text. Gibt die Breite zurueck."""
    f = _font(size)
    tw = d.textlength(text, font=f)
    h = size + 12
    w = int(tw + 24)
    d.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=bg)
    d.text((x + w / 2, y + h / 2), text, font=f, fill=fg, anchor="mm")
    return w


# --- Spielkarten ---------------------------------------------------------
CARD_W, CARD_H, CARD_R = 124, 174, 14


def _corner(rank: str, suit: str, color: tuple) -> Image.Image:
    tile = Image.new("RGBA", (48, 66), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    d.text((24, 0), rank, font=_font(30), fill=color, anchor="ma")
    d.text((24, 34), suit, font=_font(28), fill=color, anchor="ma")
    return tile


def _draw_card(img: Image.Image, x: int, y: int, rank: str, suit: str) -> None:
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle([x + 4, y + 6, x + CARD_W + 4, y + CARD_H + 6],
                        radius=CARD_R, fill=(0, 0, 0, 70))            # Schatten
    d.rounded_rectangle([x, y, x + CARD_W, y + CARD_H], radius=CARD_R,
                        fill=(250, 250, 252))
    d.rounded_rectangle([x, y, x + CARD_W, y + CARD_H], radius=CARD_R,
                        outline=(208, 212, 222), width=2)
    color = _RED if suit in "♥♦" else _BLACK
    tile = _corner(rank, suit, color)
    img.paste(tile, (x + 10, y + 8), tile)
    flip = tile.rotate(180)
    img.paste(flip, (x + CARD_W - 10 - tile.width, y + CARD_H - 8 - tile.height), flip)
    d.text((x + CARD_W / 2, y + CARD_H / 2 + 4), suit, font=_font(82),
           fill=color, anchor="mm")


def _draw_back(img: Image.Image, x: int, y: int) -> None:
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle([x + 4, y + 6, x + CARD_W + 4, y + CARD_H + 6],
                        radius=CARD_R, fill=(0, 0, 0, 70))
    d.rounded_rectangle([x, y, x + CARD_W, y + CARD_H], radius=CARD_R,
                        fill=(38, 54, 120))
    d.rounded_rectangle([x + 9, y + 9, x + CARD_W - 9, y + CARD_H - 9],
                        radius=CARD_R - 4, outline=(126, 146, 224), width=3)
    cx, cy = x + CARD_W / 2, y + CARD_H / 2
    for dx in range(-1, 2):           # ein paar Rauten als Muster
        for dy in range(-2, 3):
            ox, oy = cx + dx * 30, cy + dy * 30
            r = 9
            d.polygon([(ox, oy - r), (ox + r * 0.7, oy), (ox, oy + r),
                       (ox - r * 0.7, oy)], fill=(94, 116, 196))
    r = 20
    d.polygon([(cx, cy - r), (cx + r * 0.72, cy), (cx, cy + r), (cx - r * 0.72, cy)],
              fill=(168, 186, 240))


def _row_start(w: int, n: int, gap: int) -> int:
    return (w - (n * CARD_W + (n - 1) * gap)) // 2


def blackjack_table(dealer: list, player: list, *, hide_hole: bool,
                    dealer_value: int, player_value: int,
                    player_state: str = "") -> io.BytesIO:
    """Rendert den Blackjack-Tisch als ein Bild.

    ``dealer``/``player``: Listen aus ``(rang, symbol)``.
    ``hide_hole``: zweite Dealer-Karte verdeckt + Wert als '?'.
    ``player_state``: '' / 'bust' / 'blackjack' / 'win' / 'lose' / 'push'
    (faerbt die Spieler-Pille).
    """
    gap, pad, label_h, row_gap = 18, 30, 44, 24
    max_cards = max(len(dealer), len(player), 2)
    inner = max_cards * CARD_W + (max_cards - 1) * gap
    W = max(620, pad * 2 + inner)
    H = pad * 2 + 2 * (label_h + CARD_H) + row_gap

    img = _vgrad(W, H, _FELT_TOP, _FELT_BOT).convert("RGBA")
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle([6, 6, W - 6, H - 6], radius=18, outline=(255, 255, 255, 40), width=2)

    # Dealer-Reihe
    d.text((pad, pad - 2), "DEALER", font=_font(26), fill=_WHITE)
    dv = "?" if hide_hole else str(dealer_value)
    dv_bg = (231, 76, 60) if (not hide_hole and dealer_value > 21) else (0, 0, 0, 110)
    _pill(d, pad + 132, pad - 6, dv, 22, dv_bg, _WHITE)
    ry = pad + label_h
    rx = _row_start(W, len(dealer), gap)
    for i, (r, s) in enumerate(dealer):
        if hide_hole and i == 1:
            _draw_back(img, rx, ry)
        else:
            _draw_card(img, rx, ry, r, s)
        rx += CARD_W + gap

    # Spieler-Reihe
    py = pad + label_h + CARD_H + row_gap
    d.text((pad, py - 2), "DU", font=_font(26), fill=_WHITE)
    pstate_col = {
        "bust": (231, 76, 60), "lose": (231, 76, 60),
        "blackjack": (241, 196, 15), "win": (46, 204, 113),
        "push": (120, 130, 145),
    }.get(player_state, (0, 0, 0, 110))
    _pill(d, pad + 70, py - 6, str(player_value), 22, pstate_col, _WHITE)
    ry2 = py + label_h
    rx = _row_start(W, len(player), gap)
    for r, s in player:
        _draw_card(img, rx, ry2, r, s)
        rx += CARD_W + gap

    return _png(img)


# --- Crash-Kurve ---------------------------------------------------------
def _nice_ticks(hi: float) -> list[float]:
    span = hi - 1.0
    if span <= 0:
        return [1.0]
    raw = span / 5.0
    k = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    step = k
    for m in (1, 2, 2.5, 5, 10):
        if span / (m * k) <= 6:
            step = m * k
            break
    ticks = [1.0]
    v = 1.0 + step
    while v <= hi + 1e-9:
        ticks.append(round(v, 2))
        v += step
    return ticks


def _dashed_h(d: ImageDraw.ImageDraw, y: float, x0: int, x1: int,
              color: tuple, dash: int = 12, gap: int = 9) -> None:
    x = x0
    while x < x1:
        d.line([(x, y), (min(x + dash, x1), y)], fill=color, width=2)
        x += dash + gap


def _burst(d: ImageDraw.ImageDraw, cx: float, cy: float, color: tuple,
           outline: tuple = (255, 255, 255)) -> None:
    pts = []
    for i in range(16):
        ang = math.pi * i / 8
        r = 17 if i % 2 == 0 else 7
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    d.polygon(pts, fill=color, outline=outline)


def crash_chart(crash_point: float, target: float, cashed: bool) -> io.BytesIO:
    """Zeichnet die Crash-Kurve: Multiplikator ueber die Zeit, Ziel-Linie,
    Cashout-Punkt bzw. Explosion."""
    W, H = 760, 380
    L, R, T, B = 66, 28, 40, 46
    x0, x1, y0, y1 = L, W - R, T, H - B
    cp = max(1.001, float(crash_point))
    ymax = max(cp, target) * 1.16
    ymax = max(ymax, 1.6)

    def px(t: float) -> float:
        return x0 + t * (x1 - x0)

    def py(m: float) -> float:
        f = (m - 1.0) / (ymax - 1.0)
        return y1 - f * (y1 - y0)

    img = Image.new("RGB", (W, H), _INK).convert("RGBA")
    d = ImageDraw.Draw(img, "RGBA")
    # Gitter + y-Beschriftung
    for m in _nice_ticks(ymax):
        yy = py(m)
        d.line([(x0, yy), (x1, yy)], fill=(255, 255, 255, 20), width=1)
        d.text((x0 - 10, yy), f"{m:g}×", font=_font(18),
               fill=(150, 160, 176), anchor="rm")
    d.line([(x0, y0), (x0, y1)], fill=(80, 90, 106), width=2)
    d.line([(x0, y1), (x1, y1)], fill=(80, 90, 106), width=2)

    N = 120
    full = [(i / N, cp ** (i / N)) for i in range(N + 1)]

    # Glow-Flaechen auf eigenem Overlay (mit Transparenz), dann druntermischen.
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    def fill_under(pts, color):
        poly = pts + [(pts[-1][0], y1), (pts[0][0], y1)]
        od.polygon(poly, fill=color)

    win = cashed and target <= cp
    if win:
        tc = min(max(math.log(target) / math.log(cp), 0.0), 1.0)
        win_pts = [(px(t), py(m)) for t, m in full if t <= tc] + [(px(tc), py(target))]
        fill_under(win_pts, (46, 204, 113, 70))
    else:
        all_pts = [(px(t), py(m)) for t, m in full]
        fill_under(all_pts, (231, 76, 60, 60))

    img = Image.alpha_composite(img, overlay)
    d = ImageDraw.Draw(img, "RGBA")

    # Kurve(n) opak drueber
    if win:
        tc = min(max(math.log(target) / math.log(cp), 0.0), 1.0)
        win_pts = [(px(t), py(m)) for t, m in full if t <= tc] + [(px(tc), py(target))]
        rest = [(px(tc), py(target))] + [(px(t), py(m)) for t, m in full if t > tc]
        if len(rest) >= 2:
            d.line(rest, fill=(132, 96, 96), width=3, joint="curve")
        d.line(win_pts, fill=_GREEN, width=6, joint="curve")
        _dashed_h(d, py(target), x0, x1, (46, 204, 113, 170))
        cx, cy = px(tc), py(target)
        d.ellipse([cx - 10, cy - 10, cx + 10, cy + 10], fill=_GREEN, outline=_WHITE, width=3)
        _burst(d, px(1.0), py(cp), (150, 96, 96), outline=(200, 200, 200))
        head = f"AUSGESTIEGEN bei {target:.2f}×"
        head_col = _GREEN
    else:
        all_pts = [(px(t), py(m)) for t, m in full]
        d.line(all_pts, fill=_RED_HOT, width=6, joint="curve")
        _dashed_h(d, py(target), x0, x1, (241, 196, 15, 150))
        d.text((x1 - 4, py(target) - 20), f"Ziel {target:.2f}×", font=_font(18),
               fill=_GOLD, anchor="ra")
        _burst(d, px(1.0), py(cp), _RED_HOT)
        head = f"ABGESTÜRZT bei {cp:.2f}×"
        head_col = _RED_HOT

    # Kopfzeile
    d.text((x0, 8), "CRASH", font=_font(24), fill=(236, 240, 246))
    d.text((x1, 10), head, font=_font(20), fill=head_col, anchor="ra")
    return _png(img)
