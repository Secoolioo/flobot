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

from PIL import Image, ImageDraw, ImageFont, ImageFilter

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


def _pill_c(d: ImageDraw.ImageDraw, cx: float, y: int, text: str, size: int,
            bg: tuple, fg: tuple) -> int:
    """Wie _pill, aber horizontal um cx zentriert."""
    f = _font(size)
    w = int(d.textlength(text, font=f) + 24)
    return _pill(d, int(cx - w / 2), y, text, size, bg, fg)


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


_CRASH_TOP = (22, 28, 44)
_CRASH_BOT = (9, 12, 20)


def _glow_line(base: Image.Image, pts: list, color: tuple, width: int,
               blur: int) -> Image.Image:
    """Legt eine weiche Leucht-Linie unter die eigentliche Kurve."""
    if len(pts) < 2:
        return base
    glow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).line(pts, fill=color, width=width, joint="curve")
    glow = glow.filter(ImageFilter.GaussianBlur(blur))
    return Image.alpha_composite(base, glow)


def crash_chart(crash_point: float, target: float, cashed: bool) -> io.BytesIO:
    """Zeichnet die Crash-Kurve: Multiplikator ueber die Zeit, Ziel-Linie,
    Cashout-Punkt bzw. Explosion - mit Verlauf, Leuchtkurve und Multiplikator-Badge."""
    W, H = 820, 420
    L, R, T, B = 72, 30, 70, 50
    x0, x1, y0, y1 = L, W - R, T, H - B
    cp = max(1.001, float(crash_point))
    ymax = max(cp, target) * 1.16
    ymax = max(ymax, 1.6)

    def px(t: float) -> float:
        return x0 + t * (x1 - x0)

    def py(m: float) -> float:
        f = (m - 1.0) / (ymax - 1.0)
        return y1 - f * (y1 - y0)

    img = _vgrad(W, H, _CRASH_TOP, _CRASH_BOT).convert("RGBA")
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle([6, 6, W - 6, H - 6], radius=20, outline=(255, 255, 255, 26), width=2)
    # Gitter + y-Beschriftung
    for m in _nice_ticks(ymax):
        yy = py(m)
        d.line([(x0, yy), (x1, yy)], fill=(255, 255, 255, 18), width=1)
        d.text((x0 - 12, yy), f"{m:g}×", font=_font(18),
               fill=(150, 160, 176), anchor="rm")
    d.line([(x0, y0), (x0, y1)], fill=(80, 90, 106), width=2)
    d.line([(x0, y1), (x1, y1)], fill=(80, 90, 106), width=2)

    N = 140
    full = [(i / N, cp ** (i / N)) for i in range(N + 1)]
    win = cashed and target <= cp

    # Flaeche unter der Kurve (halbtransparent)
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    def fill_under(pts, color):
        poly = pts + [(pts[-1][0], y1), (pts[0][0], y1)]
        od.polygon(poly, fill=color)

    if win:
        tc = min(max(math.log(target) / math.log(cp), 0.0), 1.0)
        win_pts = [(px(t), py(m)) for t, m in full if t <= tc] + [(px(tc), py(target))]
        fill_under(win_pts, (46, 204, 113, 60))
    else:
        all_pts = [(px(t), py(m)) for t, m in full]
        fill_under(all_pts, (231, 76, 60, 52))
    img = Image.alpha_composite(img, overlay)

    # Leuchtkurve + opake Kurve obendrauf
    if win:
        tc = min(max(math.log(target) / math.log(cp), 0.0), 1.0)
        win_pts = [(px(t), py(m)) for t, m in full if t <= tc] + [(px(tc), py(target))]
        rest = [(px(tc), py(target))] + [(px(t), py(m)) for t, m in full if t > tc]
        img = _glow_line(img, win_pts, (46, 204, 113, 140), 16, 7)
        d = ImageDraw.Draw(img, "RGBA")
        if len(rest) >= 2:
            d.line(rest, fill=(120, 92, 92), width=3, joint="curve")
        d.line(win_pts, fill=_GREEN, width=6, joint="curve")
        _dashed_h(d, py(target), x0, x1, (46, 204, 113, 170))
        cx, cy = px(tc), py(target)
        d.ellipse([cx - 11, cy - 11, cx + 11, cy + 11], fill=_GREEN, outline=_WHITE, width=3)
        _burst(d, px(1.0), py(cp), (150, 96, 96), outline=(200, 200, 200))
        badge_col = _GREEN
    else:
        all_pts = [(px(t), py(m)) for t, m in full]
        img = _glow_line(img, all_pts, (231, 76, 60, 130), 16, 7)
        d = ImageDraw.Draw(img, "RGBA")
        d.line(all_pts, fill=_RED_HOT, width=6, joint="curve")
        _dashed_h(d, py(target), x0, x1, (241, 196, 15, 150))
        d.text((x1 - 4, py(target) - 22), f"Ziel {target:.2f}×", font=_font(18),
               fill=_GOLD, anchor="ra")
        _burst(d, px(1.0), py(cp), _RED_HOT)
        badge_col = _RED_HOT

    # Kopfzeile + grosses Multiplikator-Badge oben rechts
    d.text((x0, 18), "CRASH", font=_font(28), fill=(236, 240, 246))
    badge = f"{cp:.2f}×"
    bf = _font(40)
    tw = d.textlength(badge, font=bf)
    bx0, by0, bx1, by1 = x1 - tw - 36, 14, x1, 64
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=16, fill=(0, 0, 0, 130),
                        outline=badge_col, width=3)
    d.text(((bx0 + bx1) / 2, (by0 + by1) / 2), badge, font=bf, fill=badge_col, anchor="mm")
    return _png(img)


# --- Slot-Machine --------------------------------------------------------
# Symbol-Schluessel in fallender Wertigkeit. games.py waehlt aus SLOT_KEYS und
# legt die Auszahlung fest; hier wird nur gezeichnet.
SLOT_KEYS = ["seven", "diamond", "star", "bar", "grape", "lemon", "cherry"]


def _slot_symbol(d: ImageDraw.ImageDraw, cx: float, cy: float, R: float, key: str) -> None:
    if key == "seven":
        d.text((cx, cy), "7", font=_font(int(R * 1.95)), fill=(228, 46, 52), anchor="mm")
    elif key == "bar":
        w, h = R * 1.5, R * 0.6
        d.rounded_rectangle([cx - w, cy - h, cx + w, cy + h], radius=10,
                            fill=(142, 68, 173), outline=(92, 42, 118), width=3)
        d.text((cx, cy), "BAR", font=_font(int(R * 0.8)), fill=(245, 240, 250), anchor="mm")
    elif key == "star":
        pts = []
        for i in range(10):
            ang = -math.pi / 2 + i * math.pi / 5
            rr = R if i % 2 == 0 else R * 0.42
            pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
        d.polygon(pts, fill=(245, 197, 24), outline=(190, 150, 12))
    elif key == "diamond":
        t = (cx, cy - R); b = (cx, cy + R * 1.05)
        l = (cx - R * 0.82, cy - R * 0.18); r = (cx + R * 0.82, cy - R * 0.18)
        d.polygon([t, r, b, l], fill=(52, 170, 219), outline=(28, 108, 150))
        d.line([l, r], fill=(205, 236, 250), width=2)
        d.line([t, b], fill=(205, 236, 250), width=1)
    elif key == "cherry":
        rr = R * 0.5
        d.line([(cx, cy - R), (cx - R * 0.55, cy + R * 0.25)], fill=(80, 140, 40), width=4)
        d.line([(cx, cy - R), (cx + R * 0.55, cy + R * 0.25)], fill=(80, 140, 40), width=4)
        for ox in (cx - R * 0.85, cx + R * 0.55):
            oy = cy + R * 0.25
            d.ellipse([ox - rr, oy - rr, ox + rr, oy + rr], fill=(214, 48, 49),
                      outline=(150, 20, 25))
            d.ellipse([ox - rr * 0.5, oy - rr * 0.55, ox - rr * 0.05, oy - rr * 0.1],
                      fill=(245, 150, 150))
    elif key == "lemon":
        d.ellipse([cx - R * 0.92, cy - R * 0.62, cx + R * 0.92, cy + R * 0.62],
                  fill=(245, 210, 38), outline=(200, 165, 10), width=2)
        d.polygon([(cx + R * 0.45, cy - R * 0.45), (cx + R * 0.98, cy - R * 0.88),
                   (cx + R * 0.7, cy - R * 0.3)], fill=(80, 160, 55))
    elif key == "grape":
        rr = R * 0.32
        for gx, gy in [(-0.5, 0.15), (0.5, 0.15), (0.0, 0.05), (-0.25, 0.6),
                       (0.25, 0.6), (0.0, 1.05)]:
            ox, oy = cx + gx * R, cy + gy * R - R * 0.2
            d.ellipse([ox - rr, oy - rr, ox + rr, oy + rr], fill=(142, 82, 190),
                      outline=(95, 50, 140))
        d.line([(cx, cy - R), (cx, cy - R * 0.5)], fill=(110, 80, 40), width=4)
        d.polygon([(cx, cy - R), (cx + R * 0.42, cy - R * 1.1), (cx + R * 0.15, cy - R * 0.6)],
                  fill=(80, 160, 55))


def _slot_window(img: Image.Image, x: int, y: int, s: int, key: str) -> None:
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle([x, y, x + s, y + s], radius=16, fill=(248, 249, 252),
                        outline=(60, 64, 80), width=3)
    d.rounded_rectangle([x + 5, y + 5, x + s - 5, y + 18], radius=8, fill=(255, 255, 255, 60))
    _slot_symbol(d, x + s / 2, y + s / 2, s * 0.3, key)


def slot_machine(symbols: list, *, win: int = 0, jackpot: bool = False) -> io.BytesIO:
    """Rendert drei Slot-Walzen. ``symbols``: 3 Schluessel aus SLOT_KEYS.
    ``win``: Gewinn in Coins (0 = nichts). ``jackpot``: drei Gleiche."""
    pad, tile, gap, top_h, bot_h = 26, 150, 18, 72, 58
    W = pad * 2 + 3 * tile + 2 * gap
    H = pad * 2 + top_h + tile + bot_h
    img = _vgrad(W, H, (34, 18, 48), (16, 10, 26)).convert("RGBA")
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle([8, 8, W - 8, H - 8], radius=20, outline=(245, 197, 24, 200), width=4)
    d.text((W / 2, pad + 2), "★  FLO  SLOTS  ★", font=_font(34),
           fill=(245, 197, 24), anchor="ma")

    ry, rx = pad + top_h, pad
    d.rounded_rectangle([pad - 8, ry - 8, W - pad + 8, ry + tile + 8], radius=16,
                        fill=(8, 6, 14))
    for key in symbols:
        _slot_window(img, rx, ry, tile, key)
        rx += tile + gap

    d = ImageDraw.Draw(img, "RGBA")
    line_col = (46, 204, 113) if win > 0 else (96, 100, 118)
    ly = ry + tile / 2
    d.line([(pad - 2, ly), (W - pad + 2, ly)], fill=line_col, width=3)

    by = ry + tile + 14
    if jackpot:
        _pill_c(d, W / 2, int(by), "JACKPOT!", 30, (245, 197, 24), (28, 16, 4))
    elif win > 0:
        _pill_c(d, W / 2, int(by), f"GEWINN  +{win}", 26, (46, 204, 113), (8, 28, 16))
    else:
        _pill_c(d, W / 2, int(by), "leider nichts", 24, (70, 74, 92), (228, 230, 238))
    return _png(img)


# --- Coinflip ------------------------------------------------------------
def _crown(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float, col: tuple) -> None:
    base = cy + s * 0.45
    pts = [(cx - s, base), (cx - s, cy - s * 0.1), (cx - s * 0.5, cy + s * 0.2),
           (cx, cy - s * 0.55), (cx + s * 0.5, cy + s * 0.2), (cx + s, cy - s * 0.1),
           (cx + s, base)]
    d.polygon(pts, fill=col)
    d.rectangle([cx - s, base, cx + s, base + s * 0.3], fill=col)
    for px_ in (cx - s, cx, cx + s):
        d.ellipse([px_ - s * 0.12, cy - s * 0.62, px_ + s * 0.12, cy - s * 0.38], fill=col)


def coin_flip(result: str) -> io.BytesIO:
    """Goldmuenze, die ``kopf`` (Krone) oder ``zahl`` (Stern) zeigt."""
    W = H = 360
    img = _vgrad(W, H, (26, 32, 52), (12, 15, 26)).convert("RGBA")
    d = ImageDraw.Draw(img, "RGBA")
    cx, cy, R = W / 2, H / 2 - 6, 118
    d.ellipse([cx - R + 8, cy - R + 16, cx + R + 8, cy + R + 16], fill=(0, 0, 0, 90))
    for i in range(int(R), 0, -2):       # radialer Goldverlauf
        f = i / R
        col = (int(252 - 70 * (1 - f)), int(206 - 80 * (1 - f)), int(44 + 8 * (1 - f)))
        d.ellipse([cx - i, cy - i, cx + i, cy + i], fill=col)
    d.ellipse([cx - R, cy - R, cx + R, cy + R], outline=(150, 110, 10), width=5)
    d.ellipse([cx - R * 0.82, cy - R * 0.82, cx + R * 0.82, cy + R * 0.82],
              outline=(214, 172, 40), width=3)
    if result == "kopf":
        _crown(d, cx, cy - R * 0.12, R * 0.42, (150, 110, 10))
        label = "KOPF"
    else:
        d.text((cx, cy - R * 0.12), "★", font=_font(int(R * 0.95)),
               fill=(150, 110, 10), anchor="mm")
        label = "ZAHL"
    d.text((cx, cy + R * 0.52), label, font=_font(36), fill=(120, 88, 8), anchor="mm")
    return _png(img)


# --- Wuerfel -------------------------------------------------------------
def _pips(d: ImageDraw.ImageDraw, x: int, y: int, s: int, val: int) -> None:
    r = s * 0.085
    cxs = [x + s * 0.28, x + s * 0.5, x + s * 0.72]
    cys = [y + s * 0.28, y + s * 0.5, y + s * 0.72]
    layout = {
        1: [(1, 1)], 2: [(0, 0), (2, 2)], 3: [(0, 0), (1, 1), (2, 2)],
        4: [(0, 0), (0, 2), (2, 0), (2, 2)],
        5: [(0, 0), (0, 2), (1, 1), (2, 0), (2, 2)],
        6: [(0, 0), (0, 1), (0, 2), (2, 0), (2, 1), (2, 2)],
    }
    for ci, ri in layout.get(val, []):
        ox, oy = cxs[ci], cys[ri]
        d.ellipse([ox - r, oy - r, ox + r, oy + r], fill=(40, 44, 60))


def _die(img: Image.Image, x: int, y: int, s: int, val: int, sides: int) -> None:
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle([x + 5, y + 7, x + s + 5, y + s + 7], radius=22, fill=(0, 0, 0, 80))
    d.rounded_rectangle([x, y, x + s, y + s], radius=22, fill=(248, 249, 252),
                        outline=(60, 64, 80), width=3)
    if sides == 6 and 1 <= val <= 6:
        _pips(d, x, y, s, val)
    else:
        d.text((x + s / 2, y + s / 2 - 6), str(val), font=_font(int(s * 0.46)),
               fill=(40, 44, 60), anchor="mm")
        d.text((x + s / 2, y + s - 18), f"W{sides}", font=_font(16),
               fill=(150, 154, 170), anchor="mm")


def dice_roll(rolls: list, sides: int) -> io.BytesIO:
    """Wuerfel als Bild. d6 zeigt Augen, sonst die Zahl + 'W<n>'. Bei vielen
    Wuerfeln (>8) wird in mehrere Reihen umgebrochen, damit es kompakt bleibt."""
    n = max(1, len(rolls))
    die, gap, pad = 120, 20, 28
    per_row = min(n, 8)
    nrows = (n + per_row - 1) // per_row
    W = max(260, pad * 2 + per_row * die + (per_row - 1) * gap)
    grid_h = nrows * die + (nrows - 1) * gap
    H = pad * 2 + grid_h + (44 if n > 1 else 0)
    img = _vgrad(W, H, (30, 34, 46), (14, 16, 22)).convert("RGBA")
    x0 = (W - (per_row * die + (per_row - 1) * gap)) // 2
    for i, r in enumerate(rolls):
        rr, cc = divmod(i, per_row)
        _die(img, x0 + cc * (die + gap), pad + rr * (die + gap), die, r, sides)
    if n > 1:
        d = ImageDraw.Draw(img, "RGBA")
        _pill_c(d, W / 2, pad + grid_h + 8, f"Summe  {sum(rolls)}", 24,
                (245, 197, 24), (22, 14, 4))
    return _png(img)


# --- Roulette ------------------------------------------------------------
_ROUL_RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
_ROUL_ORDER = [0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23,
               10, 5, 24, 16, 33, 1, 20, 14, 31, 9, 22, 18, 29, 7, 28, 12, 35, 3, 26]


def _roul_color(num: int) -> tuple:
    if num == 0:
        return (39, 174, 96)
    return (192, 57, 43) if num in _ROUL_RED else (30, 32, 40)


def roulette_wheel(spin: int, won: bool) -> io.BytesIO:
    """Roulette-Kessel mit Zahlenring, Kugel am Gewinnerfach und Ergebnis-Hub."""
    W = H = 440
    img = _vgrad(W, H, _FELT_TOP, _FELT_BOT).convert("RGBA")
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle([6, 6, W - 6, H - 6], radius=20,
                        outline=(46, 204, 113) if won else (231, 76, 60), width=4)
    cx, cy, Ro, Ri = W / 2, H / 2 + 4, 196, 150
    n = len(_ROUL_ORDER)
    seg = 360.0 / n
    idx = _ROUL_ORDER.index(spin) if spin in _ROUL_ORDER else 0
    base = -90.0 - idx * seg - seg / 2          # Gewinner-Fach nach oben drehen
    for i, num in enumerate(_ROUL_ORDER):
        a0 = base + i * seg
        d.pieslice([cx - Ro, cy - Ro, cx + Ro, cy + Ro], a0, a0 + seg,
                   fill=_roul_color(num), outline=(18, 18, 22))
    Rm = (Ro + Ri) / 2 + 6
    for i, num in enumerate(_ROUL_ORDER):
        a = math.radians(base + i * seg + seg / 2)
        d.text((cx + Rm * math.cos(a), cy + Rm * math.sin(a)), str(num),
               font=_font(14), fill=(245, 245, 245), anchor="mm")
    d.ellipse([cx - Ri, cy - Ri, cx + Ri, cy + Ri], fill=(12, 60, 40),
              outline=(245, 197, 24), width=4)
    # Kugel + Zeiger oben
    d.polygon([(cx - 15, cy - Ro - 24), (cx + 15, cy - Ro - 24), (cx, cy - Ro + 10)],
              fill=(245, 245, 245), outline=(20, 20, 20))
    d.ellipse([cx - 11, cy - Ro + 8, cx + 11, cy - Ro + 30], fill=(250, 250, 252),
              outline=(60, 60, 60), width=2)
    # Ergebnis-Hub
    d.ellipse([cx - 72, cy - 72, cx + 72, cy + 72], fill=_roul_color(spin),
              outline=(245, 197, 24), width=4)
    d.text((cx, cy - 8), str(spin), font=_font(60), fill=(250, 250, 252), anchor="mm")
    name = "GRÜN" if spin == 0 else ("ROT" if spin in _ROUL_RED else "SCHWARZ")
    d.text((cx, cy + 42), name, font=_font(22), fill=(250, 250, 252), anchor="mm")
    return _png(img)


# --- Keno ----------------------------------------------------------------
def _legend(d: ImageDraw.ImageDraw, x: int, y: int, col: tuple, text: str) -> int:
    f = _font(16)
    d.rounded_rectangle([x, y, x + 22, y + 22], radius=5, fill=col)
    d.text((x + 30, y + 11), text, font=f, fill=(210, 214, 228), anchor="lm")
    return 30 + int(d.textlength(text, font=f)) + 26


def keno_grid(picks: list, draw: list, hits: list) -> io.BytesIO:
    """Zahlenraster 1-40: Treffer (gold), eigener Tipp (blau), gezogen (grau)."""
    cols, rows = 8, 5
    cell, gap, pad, top = 58, 10, 28, 60
    W = pad * 2 + cols * cell + (cols - 1) * gap
    H = top + rows * cell + (rows - 1) * gap + 56
    img = _vgrad(W, H, (22, 28, 40), (12, 15, 24)).convert("RGBA")
    d = ImageDraw.Draw(img, "RGBA")
    d.text((pad, 16), "KENO", font=_font(30), fill=(241, 196, 15))
    pickset, drawset, hitset = set(picks), set(draw), set(hits)
    y = top
    for r in range(rows):
        x = pad
        for c in range(cols):
            num = r * cols + c + 1
            if num in hitset:
                bg, fg, ol = (241, 196, 15), (24, 16, 4), (255, 220, 90)
            elif num in pickset:
                bg, fg, ol = (41, 55, 90), (220, 228, 245), (90, 130, 220)
            elif num in drawset:
                bg, fg, ol = (66, 70, 88), (235, 235, 240), (108, 112, 132)
            else:
                bg, fg, ol = (26, 30, 44), (120, 126, 144), (44, 48, 64)
            d.rounded_rectangle([x, y, x + cell, y + cell], radius=12, fill=bg,
                                outline=ol, width=2)
            d.text((x + cell / 2, y + cell / 2), str(num), font=_font(22),
                   fill=fg, anchor="mm")
            x += cell + gap
        y += cell + gap
    ly = y + 8
    lx = pad
    lx += _legend(d, lx, ly, (241, 196, 15), f"Treffer {len(hits)}")
    lx += _legend(d, lx, ly, (41, 55, 90), "dein Tipp")
    _legend(d, lx, ly, (66, 70, 88), "gezogen")
    return _png(img)


# --- Shop-Banner (v1.2) --------------------------------------------------
def _hex(value: int) -> tuple[int, int, int]:
    return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)


def _mix(a: tuple, b: tuple, f: float) -> tuple:
    return tuple(round(a[i] + (b[i] - a[i]) * f) for i in range(3))


def _round_grad(w: int, h: int, radius: int, top: tuple, bot: tuple) -> Image.Image:
    """RGBA-Kachel mit vertikalem Verlauf (top->bot) und abgerundeten Ecken."""
    grad = _vgrad(w, h, top, bot).convert("RGBA")
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    grad.putalpha(mask)
    return grad


def _fit_font(d: ImageDraw.ImageDraw, text: str, max_w: int,
              start: int, floor: int) -> tuple[ImageFont.FreeTypeFont, str]:
    """Groesste Schrift <= start, bei der 'text' in max_w passt; sonst kuerzen mit '…'."""
    size = start
    while size > floor and d.textlength(text, font=_font(size)) > max_w:
        size -= 2
    f = _font(size)
    if d.textlength(text, font=f) > max_w:
        while text and d.textlength(text + "…", font=f) > max_w:
            text = text[:-1]
        text += "…"
    return f, text


def shop_banner(items: list[dict], *, date: str = "") -> io.BytesIO | None:
    """Schoenes Banner fuer den Tages-Shop: je Titel eine Zeile, eingefaerbt in
    der Seltenheits-Farbe (gruen/blau/lila/gold). Erwartet je Item ein Dict mit
    'n', 'text', 'price', 'color', 'rarity_label'. Gibt PNG (BytesIO) zurueck.

    Bewusst OHNE Emoji (die Pillow-Schrift kann keine Farb-Emojis) – die
    Seltenheit wird ueber Farbe + gefuellte Pills transportiert."""
    if not items:
        return None
    W = 1000
    pad = 36
    head = 150
    row_h = 96
    gap = 18
    n = len(items)
    H = head + n * row_h + (n - 1) * gap + pad

    # Hintergrund: tiefer Verlauf + zwei weiche, farbige Lichthoefe (Glow).
    img = _vgrad(W, H, (24, 27, 42), (10, 12, 20)).convert("RGBA")
    gw, gh = W // 2, H // 2
    glow = Image.new("RGBA", (gw, gh), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([-110, -150, 250, 130], fill=(70, 96, 210, 70))        # blau, links oben
    gd.ellipse([gw - 240, -160, gw + 120, 110], fill=(150, 70, 185, 55))  # lila, rechts oben
    glow = glow.filter(ImageFilter.GaussianBlur(46)).resize((W, H))
    img = Image.alpha_composite(img, glow)
    d = ImageDraw.Draw(img, "RGBA")

    # --- Kopfzeile: "FLO" weiss + "SHOP" gold, Untertitel-Pill, Datum rechts ---
    hf = _font(56)
    d.text((pad, 30), "FLO", font=hf, fill=_WHITE)
    flo_w = d.textlength("FLO ", font=hf)
    d.text((pad + flo_w, 30), "SHOP", font=hf, fill=_GOLD)
    _pill(d, pad + 3, 102, "TÄGLICH 2 UHR NEU", 16, (44, 50, 76), (188, 198, 222))
    if date:
        d.text((W - pad, 112), f"Stand {date}", font=_font(18),
               fill=(150, 158, 188), anchor="rm")
    d.line([(pad, head - 14), (W - pad, head - 14)], fill=(54, 60, 86), width=2)

    # --- Karten-Schatten (eine geblurrte Ebene unter allen Karten) ---
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    y = head
    for _ in items:
        sd.rounded_rectangle([pad, y + 7, W - pad, y + row_h + 7], radius=22,
                             fill=(0, 0, 0, 120))
        y += row_h + gap
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(9)))
    d = ImageDraw.Draw(img, "RGBA")

    # --- Karten ---
    cw = W - 2 * pad
    y = head
    for e in items:
        col = _hex(int(e.get("color", 0x57F287)))
        # Karte: dezenter Verlauf, in der Seltenheitsfarbe getoent + farbiger Rand
        card = _round_grad(cw, row_h, 22, _mix((32, 36, 54), col, 0.20),
                           _mix((17, 19, 30), col, 0.06))
        img.paste(card, (pad, y), card)
        d.rounded_rectangle([pad, y, W - pad, y + row_h], radius=22,
                            outline=col, width=2)

        # Nummern-Kachel (gerundetes Quadrat, Seltenheitsfarbe + Glanz)
        ks = 66
        kx, ky = pad + 16, y + (row_h - ks) // 2
        tile = _round_grad(ks, ks, 16, _mix(col, _WHITE, 0.30),
                           _mix(col, (0, 0, 0), 0.20))
        img.paste(tile, (kx, ky), tile)
        d.rounded_rectangle([kx, ky, kx + ks, ky + ks], radius=16,
                            outline=_mix(col, _WHITE, 0.45), width=1)
        d.text((kx + ks / 2, ky + ks / 2 - 1), str(e.get("n", "?")),
               font=_font(34), fill=(14, 16, 24), anchor="mm")

        # Preis-Pill rechts (dunkel, Goldmuenze + Betrag) – zuerst, fuer Titelbreite
        price = f"{e.get('price', 0)}"
        pf = _font(27)
        pw = d.textlength(price, font=pf)
        pill_w = int(pw + 30 + 28)
        pill_h = 46
        px1 = W - pad - 18 - pill_w
        py = y + (row_h - pill_h) // 2
        d.rounded_rectangle([px1, py, px1 + pill_w, py + pill_h], radius=pill_h // 2,
                            fill=(13, 15, 24), outline=_mix((13, 15, 24), _GOLD, 0.45),
                            width=1)
        cx0, cy0 = px1 + 14, py + (pill_h - 28) // 2
        d.ellipse([cx0, cy0, cx0 + 28, cy0 + 28], fill=_GOLD, outline=(176, 136, 8), width=2)
        d.text((cx0 + 14, cy0 + 14), "C", font=_font(18), fill=(120, 90, 0), anchor="mm")
        d.text((px1 + pill_w - 16, y + row_h / 2), price, font=pf, fill=_GOLD, anchor="rm")

        # Titel (adaptiv) + gefuellter Seltenheits-Pill
        tx = kx + ks + 24
        max_tw = px1 - tx - 20
        tf, title = _fit_font(d, str(e.get("text", "?")), max_tw, 31, 21)
        d.text((tx, y + 19), title, font=tf, fill=_WHITE)
        _pill(d, tx, y + row_h - 38, str(e.get("rarity_label", "")).upper(), 15,
              col, (15, 17, 25))

        y += row_h + gap

    return _png(img)


# =========================================================================
#  Minecraft-Leaderboard (Block-Style, Pixel-Optik)
# =========================================================================
# Bewusst OHNE Emoji (Pillow-Schrift kann keine Farb-Emojis) - die Minecraft-
# Anmutung kommt aus: Drop-Shadow-Text, abgeschraegte GUI-Panels (Bevel),
# selbstgezeichnete Pixel-Block-Icons mit Erz-Sprenkeln und der typischen Palette.

def _clamp(v: float) -> int:
    return max(0, min(255, int(v)))


# Palette je Block-Art: base = Grundfarbe, noise = Koernung, spec = Erz-Sprenkel,
# grass = gruene Oberkante (Gras-Block), bark = vertikale Holzmaserung.
_MC_PAL = {
    "stone":         {"base": (124, 124, 124), "noise": 14},
    "cobblestone":   {"base": (120, 120, 120), "noise": 26},
    "deepslate":     {"base": (74, 74, 79), "noise": 14},
    "dirt":          {"base": (134, 96, 67), "noise": 16},
    "grass_block":   {"base": (134, 96, 67), "noise": 16, "grass": (96, 160, 54)},
    "sand":          {"base": (219, 206, 160), "noise": 12},
    "gravel":        {"base": (128, 124, 122), "noise": 28},
    "netherrack":    {"base": (102, 40, 40), "noise": 20},
    "obsidian":      {"base": (24, 20, 38), "noise": 10, "spec": (74, 52, 110)},
    "ice":           {"base": (147, 184, 232), "noise": 9},
    "coal_ore":      {"base": (124, 124, 124), "noise": 14, "spec": (32, 32, 32)},
    "iron_ore":      {"base": (124, 124, 124), "noise": 14, "spec": (214, 176, 150)},
    "copper_ore":    {"base": (124, 124, 124), "noise": 14, "spec": (225, 140, 100)},
    "gold_ore":      {"base": (124, 124, 124), "noise": 14, "spec": (252, 219, 80)},
    "redstone_ore":  {"base": (124, 124, 124), "noise": 14, "spec": (220, 44, 44)},
    "lapis_ore":     {"base": (124, 124, 124), "noise": 14, "spec": (42, 84, 200)},
    "diamond_ore":   {"base": (124, 124, 124), "noise": 14, "spec": (92, 224, 222)},
    "emerald_ore":   {"base": (124, 124, 124), "noise": 14, "spec": (50, 204, 92)},
    "ancient_debris": {"base": (100, 74, 62), "noise": 16, "spec": (122, 92, 82)},
    "quartz":        {"base": (102, 40, 40), "noise": 18, "spec": (236, 231, 226)},
    "log":           {"base": (108, 82, 50), "noise": 13, "bark": True},
    "generic":       {"base": (122, 126, 132), "noise": 14},
}


def _block_tile(size: int, kind: str) -> Image.Image:
    """Selbstgezeichnetes Pixel-Block-Icon (Front-Textur) im Minecraft-Stil:
    Grundfarbe + Koernung, Erz-Sprenkel, Hell/Dunkel-Bevel, dunkler Rahmen."""
    pal = _MC_PAL.get(kind, _MC_PAL["generic"])
    base = pal["base"]
    noise = pal.get("noise", 14)
    grass = pal.get("grass")
    bark = pal.get("bark")
    spec = pal.get("spec")
    g = 8
    cell = size / g
    seed = sum(ord(c) for c in kind) + 7
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    td = ImageDraw.Draw(tile)
    for gy in range(g):
        for gx in range(g):
            h = (gx * 53 + gy * 131 + seed * 17) % 100
            delta = (h / 100 * 2 - 1) * noise
            col = base
            if grass is not None and gy == 0:
                col = grass
            elif grass is not None and gy == 1:
                col = _mix(grass, base, 0.5)
            elif bark and gx % 3 == 0:
                col = _mix(base, (0, 0, 0), 0.22)
            cc = tuple(_clamp(col[i] + delta) for i in range(3))
            td.rectangle([round(gx * cell), round(gy * cell),
                          round((gx + 1) * cell), round((gy + 1) * cell)], fill=cc + (255,))
    if spec is not None:
        hl = _mix(spec, (255, 255, 255), 0.5)
        for gy in range(g):
            for gx in range(g):
                if grass is not None and gy <= 1:
                    continue
                if (gx * 7 + gy * 5 + seed) % 11 < 2:
                    x0, y0 = round(gx * cell), round(gy * cell)
                    td.rectangle([x0, y0, round((gx + 1) * cell), round((gy + 1) * cell)],
                                 fill=spec + (255,))
                    td.rectangle([x0, y0, round(x0 + cell / 2), round(y0 + cell / 2)],
                                 fill=hl + (255,))
    lite = _mix(base, (255, 255, 255), 0.28)
    dark = _mix(base, (0, 0, 0), 0.38)
    td.line([(0, 0), (size - 1, 0)], fill=lite + (255,))
    td.line([(0, 0), (0, size - 1)], fill=lite + (255,))
    td.line([(0, size - 1), (size - 1, size - 1)], fill=dark + (255,))
    td.line([(size - 1, 0), (size - 1, size - 1)], fill=dark + (255,))
    td.rectangle([0, 0, size - 1, size - 1], outline=(16, 16, 20, 255), width=1)
    return tile


def _mc_text(d, x, int_y, text, size, fill, anchor="la", shadow=0.30):
    """Text mit dem typischen Minecraft-Schlagschatten (dunkler, nach unten-rechts)."""
    f = _font(size)
    off = max(2, size // 9)
    sc = tuple(int(c * shadow) for c in fill)
    d.text((x + off, int_y + off), text, font=f, fill=sc, anchor=anchor)
    d.text((x, int_y), text, font=f, fill=fill, anchor=anchor)


def _mc_panel(d, box, base, depth=5, outline=(14, 14, 18)):
    """Klassisches Minecraft-GUI-Panel: heller Rand oben/links, dunkler Rand
    unten/rechts, schwarze Kontur - der ikonische Inventar-Bevel."""
    x0, y0, x1, y1 = box
    d.rectangle(box, fill=base)
    lite = _mix(base, (255, 255, 255), 0.30)
    dark = _mix(base, (0, 0, 0), 0.42)
    d.rectangle([x0, y0, x1, y0 + depth - 1], fill=lite)
    d.rectangle([x0, y0, x0 + depth - 1, y1], fill=lite)
    d.rectangle([x0, y1 - depth + 1, x1, y1], fill=dark)
    d.rectangle([x1 - depth + 1, y0, x1, y1], fill=dark)
    d.rectangle(box, outline=outline, width=2)


def _fmt_int(n) -> str:
    return f"{int(n):,}".replace(",", ".")


def _mc_grid(d, w, h):
    for x in range(0, w, 40):
        d.line([(x, 0), (x, h)], fill=(255, 255, 255, 8))
    for y in range(0, h, 40):
        d.line([(0, y), (w, y)], fill=(0, 0, 0, 30))


def _mc_header(img, d, box, data):
    x0, y0, x1, y1 = box
    _mc_panel(d, box, (126, 90, 62), depth=6)          # Erd-Korpus
    gh = 30
    d.rectangle([x0 + 6, y0 + 6, x1 - 6, y0 + 6 + gh], fill=(96, 160, 54))   # Gras-Oberkante
    d.rectangle([x0 + 6, y0 + 6 + gh - 7, x1 - 6, y0 + 6 + gh], fill=(80, 138, 46))
    # Block-Trio links als Deko
    for i, k in enumerate(("grass_block", "diamond_ore", "gold_ore")):
        t = _block_tile(46, k)
        img.paste(t, (x0 + 20 + i * 30, y1 - 60), t)
    # Titel (zweifarbig, mit MC-Schatten)
    tx = x0 + 168
    _mc_text(d, tx, y0 + 50, "MINECRAFT", 46, _GOLD, anchor="lm")
    mw = d.textlength("MINECRAFT ", font=_font(46))
    _mc_text(d, tx + mw, y0 + 50, "STATISTIK", 46, _WHITE, anchor="lm")
    ver = f"  ·  {data['version']}" if data.get("version") else ""
    sub = f"{data.get('server', '?')}{ver}  ·  {data.get('player_count', 0)} Spieler"
    _mc_text(d, tx, y0 + 90, sub, 19, (224, 228, 234), anchor="lm", shadow=0.32)


# --- Mob-Gesichter (einfache Pixel-Faces fuer die haeufigsten Mobs) -------
_MOB_PAL = {
    "zombie":   {"skin": (74, 122, 74), "eye": (22, 34, 22)},
    "skeleton": {"skin": (200, 202, 200), "eye": (40, 40, 44)},
    "creeper":  {"skin": (94, 184, 86), "eye": (18, 38, 16), "creeper": True},
    "spider":   {"skin": (62, 52, 48), "eye": (170, 44, 44)},
    "cave_spider": {"skin": (40, 70, 76), "eye": (170, 44, 44)},
    "enderman": {"skin": (26, 24, 32), "eye": (190, 130, 240)},
    "witch":    {"skin": (78, 112, 98), "eye": (40, 22, 42)},
    "slime":    {"skin": (112, 198, 94), "eye": (32, 64, 26), "slime": True},
    "blaze":    {"skin": (236, 184, 40), "eye": (120, 60, 0)},
    "piglin":   {"skin": (150, 120, 110), "eye": (40, 30, 24)},
    "zombified_piglin": {"skin": (120, 140, 96), "eye": (36, 44, 24)},
    "generic":  {"skin": (120, 122, 130), "eye": (30, 30, 36)},
}


def _mob_face(size: int, kind: str) -> Image.Image:
    """Einfaches Minecraft-Mob-Gesicht (Kopf + Augen) als Pixel-Icon."""
    pal = _MOB_PAL.get(kind, _MOB_PAL["generic"])
    skin, eye = pal["skin"], pal["eye"]
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    td = ImageDraw.Draw(tile)
    g = 8
    cell = size / g
    seed = sum(ord(c) for c in kind) + 3
    for gy in range(g):
        for gx in range(g):
            hh = (gx * 53 + gy * 131 + seed * 17) % 100
            delta = (hh / 100 * 2 - 1) * 9
            cc = tuple(_clamp(skin[i] + delta) for i in range(3))
            td.rectangle([round(gx * cell), round(gy * cell),
                          round((gx + 1) * cell), round((gy + 1) * cell)], fill=cc + (255,))

    def box(fx0, fy0, fx1, fy1):
        td.rectangle([round(size * fx0), round(size * fy0),
                      round(size * fx1), round(size * fy1)], fill=eye + (255,))
    if pal.get("creeper"):
        box(0.22, 0.30, 0.40, 0.50)
        box(0.60, 0.30, 0.78, 0.50)
        box(0.42, 0.52, 0.58, 0.86)
        box(0.30, 0.60, 0.70, 0.74)
    elif pal.get("slime"):
        box(0.28, 0.40, 0.40, 0.54)
        box(0.60, 0.40, 0.72, 0.54)
        box(0.42, 0.64, 0.58, 0.72)
    else:
        box(0.22, 0.36, 0.40, 0.52)
        box(0.60, 0.36, 0.78, 0.52)
    lite = _mix(skin, (255, 255, 255), 0.22)
    dark = _mix(skin, (0, 0, 0), 0.34)
    td.line([(0, 0), (size - 1, 0)], fill=lite + (255,))
    td.line([(0, 0), (0, size - 1)], fill=lite + (255,))
    td.line([(0, size - 1), (size - 1, size - 1)], fill=dark + (255,))
    td.line([(size - 1, 0), (size - 1, size - 1)], fill=dark + (255,))
    td.rectangle([0, 0, size - 1, size - 1], outline=(16, 16, 20, 255), width=1)
    return tile


# --- Item-Icons (Crafting/Benutzt): Inventar-Slot + einfaches Symbol ------
_ITEM_COL = {
    "ingot": (214, 216, 222), "plank": (162, 124, 74), "tool": (150, 152, 158),
    "sword": (206, 208, 214), "food": (198, 96, 72), "stick": (150, 116, 70),
    "torch": (252, 212, 92), "gem": (96, 222, 214), "generic": (152, 156, 164),
}


def _item_tile(size: int, family: str) -> Image.Image:
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    td = ImageDraw.Draw(tile)
    slot = (60, 62, 70)
    td.rectangle([0, 0, size - 1, size - 1], fill=slot + (255,))
    dk = _mix(slot, (0, 0, 0), 0.45)
    lt = _mix(slot, (255, 255, 255), 0.16)
    td.rectangle([0, 0, size - 1, 2], fill=dk + (255,))
    td.rectangle([0, 0, 2, size - 1], fill=dk + (255,))
    td.rectangle([0, size - 3, size - 1, size - 1], fill=lt + (255,))
    td.rectangle([size - 3, 0, size - 1, size - 1], fill=lt + (255,))
    col = _ITEM_COL.get(family, _ITEM_COL["generic"])

    def rc(fx0, fy0, fx1, fy1, c, rad=0):
        b = [round(size * fx0), round(size * fy0), round(size * fx1), round(size * fy1)]
        if rad:
            td.rounded_rectangle(b, radius=rad, fill=c + (255,))
        else:
            td.rectangle(b, fill=c + (255,))
    if family in ("tool", "pickaxe", "axe", "shovel", "hoe", "sword"):
        td.line([(round(size * 0.32), round(size * 0.74)),
                 (round(size * 0.62), round(size * 0.30))],
                fill=(150, 116, 70, 255), width=max(2, size // 12))
        if family == "sword":
            rc(0.54, 0.16, 0.70, 0.46, col)
        else:
            rc(0.52, 0.18, 0.80, 0.34, col, rad=max(2, size // 12))
    elif family == "ingot":
        rc(0.24, 0.40, 0.76, 0.62, col, rad=max(2, size // 12))
    elif family == "plank":
        for k in range(3):
            yy = 0.30 + 0.16 * k
            rc(0.22, yy, 0.78, yy + 0.10, _mix(col, (0, 0, 0), 0.08 * k))
    elif family == "food":
        td.ellipse([round(size * 0.28), round(size * 0.30),
                    round(size * 0.72), round(size * 0.74)], fill=col + (255,))
    elif family == "torch":
        rc(0.46, 0.42, 0.54, 0.82, (150, 116, 70))
        td.ellipse([round(size * 0.40), round(size * 0.22),
                    round(size * 0.60), round(size * 0.48)], fill=col + (255,))
    elif family == "gem":
        cx = size / 2
        td.polygon([(cx, round(size * 0.24)), (round(size * 0.74), round(size * 0.5)),
                    (cx, round(size * 0.78)), (round(size * 0.26), round(size * 0.5))],
                   fill=col + (255,))
    elif family == "stick":
        td.line([(round(size * 0.36), round(size * 0.74)),
                 (round(size * 0.64), round(size * 0.28))],
                fill=col + (255,), width=max(2, size // 10))
    else:
        rc(0.28, 0.28, 0.72, 0.72, col, rad=max(2, size // 10))
    td.rectangle([0, 0, size - 1, size - 1], outline=(16, 16, 20, 255), width=1)
    return tile


def _mc_icon(size: int, kind: str) -> Image.Image:
    """Dispatcher: 'mob:...' -> Mob-Gesicht, 'item:...' -> Item-Slot, sonst Block."""
    k = kind or "generic"
    if k.startswith("mob:"):
        return _mob_face(size, k[4:])
    if k.startswith("item:"):
        return _item_tile(size, k[5:])
    return _block_tile(size, k)


# --- Tab-Leiste (wie die Reiter im Ingame-Statistik-Menue) ----------------
_TABS = [("mined", "ABBAU"), ("playtime", "AKTIV"), ("crafted", "CRAFTING"),
         ("used", "BENUTZT"), ("killed", "MOBS")]


def _mc_tabs(d, box, active):
    x0, y0, x1, y1 = box
    n = len(_TABS)
    gap = 8
    cw = ((x1 - x0) - (n - 1) * gap) / n
    for i, (key, label) in enumerate(_TABS):
        bx0 = x0 + i * (cw + gap)
        bx1 = bx0 + cw
        on = key == active
        base = (242, 198, 50) if on else (58, 60, 70)
        _mc_panel(d, (round(bx0), y0, round(bx1), y1), base, depth=4)
        fg = (28, 26, 18) if on else (214, 218, 226)
        _mc_text(d, (bx0 + bx1) / 2, (y0 + y1) / 2, label, 17, fg,
                 anchor="mm", shadow=0.0 if on else 0.34)


_MEDAL = {1: (242, 198, 50), 2: (196, 202, 210), 3: (198, 132, 70)}


def _default_head(size: int) -> Image.Image:
    """Notfall-Kopf (Steve-artig), falls kein echter Skin geladen werden konnte."""
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    dd = ImageDraw.Draw(im)
    skin = (199, 159, 124)
    hair = (74, 54, 38)
    eye = (60, 70, 120)

    def rc(fx0, fy0, fx1, fy1, c):
        dd.rectangle([round(size * fx0), round(size * fy0),
                      round(size * fx1), round(size * fy1)], fill=c + (255,))
    rc(0, 0, 1, 1, skin)
    rc(0, 0, 1, 0.28, hair)
    rc(0, 0, 0.18, 0.5, hair)
    rc(0.82, 0, 1, 0.5, hair)
    rc(0.24, 0.42, 0.42, 0.56, (250, 250, 250))
    rc(0.32, 0.42, 0.42, 0.56, eye)
    rc(0.58, 0.42, 0.76, 0.56, (250, 250, 250))
    rc(0.58, 0.42, 0.68, 0.56, eye)
    rc(0.40, 0.62, 0.60, 0.70, _mix(skin, (0, 0, 0), 0.22))
    return im


def _player_head(size: int, head_bytes) -> Image.Image:
    """Echter Minecraft-Skin-Kopf (PNG-Bytes von der Bridge/Minotar) als Pixel-
    Icon; ohne Skin der Notfall-Kopf."""
    if head_bytes:
        try:
            im = Image.open(io.BytesIO(head_bytes)).convert("RGBA")
            return im.resize((size, size), Image.NEAREST)
        except Exception:  # noqa: BLE001 - kaputtes Bild -> Notfall-Kopf
            pass
    return _default_head(size)


def _mc_player_row(img, d, box, row):
    """Premium-Spieler-Reihe: Rang-Medaille, echter Skin-Kopf, Name + Spezialitaet
    (Top-Item/-Mob), und die grosse Vergleichszahl rechts."""
    x0, y0, x1, y1 = box
    rank = row.get("rank") or 0
    accent = _MEDAL.get(rank, (96, 100, 110))
    base = _mix((52, 55, 64), accent, 0.22 if rank <= 3 else 0.06)
    _mc_panel(d, box, base, depth=5)
    # Rang-Medaille
    ms = 58
    mx, my = x0 + 14, y0 + (y1 - y0 - ms) // 2
    _mc_panel(d, (mx, my, mx + ms, my + ms), accent, depth=4)
    _mc_text(d, mx + ms / 2, my + ms / 2, str(rank), 30, (28, 26, 20), anchor="mm", shadow=0.0)
    # Echter Spieler-Kopf (gerahmt)
    hs = y1 - y0 - 24
    hx, hy = mx + ms + 14, y0 + 12
    _mc_panel(d, (hx - 3, hy - 3, hx + hs + 3, hy + hs + 3), (38, 40, 48), depth=3)
    head = _player_head(hs, row.get("head"))
    img.paste(head, (hx, hy), head)
    # Grosse Vergleichszahl rechts (zuerst – fuer die Namensbreite)
    val = str(row.get("value", "0"))
    word = str(row.get("label", ""))
    rx = x1 - 24
    _mc_text(d, rx, y0 + 38, val, 38, (255, 214, 80), anchor="ra")
    _mc_text(d, rx, y0 + 80, word, 16, (200, 204, 212), anchor="ra", shadow=0.34)
    valw = d.textlength(val, font=_font(38))
    inner_right = rx - valw - 34
    # Name
    tx = hx + hs + 18
    nf, name = _fit_font(d, str(row.get("name", "?")), max(80, inner_right - tx), 28, 16)
    _mc_text(d, tx, y0 + 22, name, nf.size, _WHITE, anchor="la")
    # Spezialitaet: kleines Icon + Text
    top = row.get("top")
    if top:
        isz = 34
        icon = _mc_icon(isz, top.get("kind", "generic"))
        img.paste(icon, (tx, y0 + 58), icon)
        sx = tx + isz + 10
        sf, stext = _fit_font(
            d, f"{top.get('name', '?')}  x{_fmt_int(top.get('count', 0) or 0)}",
            max(60, inner_right - sx), 18, 13)
        _mc_text(d, sx, y0 + 62, stext, sf.size, (176, 210, 172), anchor="la", shadow=0.34)


def _mc_totals(img, d, box, t):
    x0, y0, x1, y1 = box
    _mc_panel(d, box, (40, 42, 50), depth=5)
    cells = [
        (_fmt_int(t.get("mined", 0)), "BLOECKE", (255, 210, 70)),
        (f"{t.get('play_h', 0):.0f}", "STUNDEN", (120, 200, 255)),
        (_fmt_int(t.get("kills", 0)), "MOB-KILLS", (240, 120, 120)),
        (_fmt_int(t.get("deaths", 0)), "TODE", (210, 210, 216)),
        (f"{t.get('km', 0):.1f}", "KM ZU FUSS", (150, 224, 140)),
    ]
    n = len(cells)
    inner = (x1 - x0) - 20
    cw = inner / n
    for i, (val, label, col) in enumerate(cells):
        cx = x0 + 10 + cw * (i + 0.5)
        _mc_text(d, cx, y0 + 30, val, 30, col, anchor="mm")
        _mc_text(d, cx, y0 + 58, label, 15, (190, 194, 202), anchor="mm", shadow=0.34)
        if i < n - 1:
            d.line([(x0 + 10 + cw * (i + 1), y0 + 14), (x0 + 10 + cw * (i + 1), y1 - 14)],
                   fill=(64, 66, 76))


def mc_stats(data: dict, category: str = None) -> "io.BytesIO | None":
    """Minecraft-Statistik im Ingame-Look: Gras-Header, Tab-Leiste (aktive
    Kategorie hervorgehoben) und die Spieler-Rangliste der Kategorie - jeder mit
    echtem Skin-Kopf, Vergleichszahl und Spezialitaet. Unten das Haupt-Stats-Band."""
    cats = data.get("cats") or {}
    if not category or category not in cats:
        category = data.get("default_cat") or next(iter(cats), "mined")
    rows = cats.get(category, {}).get("rows") or []

    W = 1000
    pad = 34
    head_h = 120
    tab_h = 50
    row_h = 108
    gap = 14
    foot_h = 92
    n = max(1, len(rows))
    H = pad + head_h + 12 + tab_h + 14 + n * row_h + (n - 1) * gap + 10 + foot_h + pad

    img = _vgrad(W, H, (46, 49, 56), (24, 25, 30)).convert("RGBA")
    d = ImageDraw.Draw(img, "RGBA")
    _mc_grid(d, W, H)

    y = pad
    _mc_header(img, d, (pad, y, W - pad, y + head_h), data)
    y += head_h + 12
    _mc_tabs(d, (pad, y, W - pad, y + tab_h), category)
    y += tab_h + 14
    if rows:
        for r in rows:
            _mc_player_row(img, d, (pad, y, W - pad, y + row_h), r)
            y += row_h + gap
    else:
        _mc_panel(d, (pad, y, W - pad, y + row_h), (50, 52, 60), depth=5)
        _mc_text(d, W / 2, y + row_h / 2, "Noch keine Daten in dieser Kategorie",
                 22, (200, 204, 212), anchor="mm", shadow=0.34)
    _mc_totals(img, d, (pad, H - pad - foot_h, W - pad, H - pad), data.get("totals") or {})

    return _png(img)


def mc_leaderboard(data: dict) -> "io.BytesIO | None":
    """Rueckwaerts-kompatibel: die Standard-Kategorie."""
    return mc_stats(data, None)
