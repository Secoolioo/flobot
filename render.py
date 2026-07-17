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
import random
import unicodedata

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageFilter, ImageOps

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
    """Vertikaler Farbverlauf (ohne numpy). Schnell: 1px-Spalte zeichnen und
    auf volle Breite skalieren statt h einzelne Linien."""
    col = Image.new("RGB", (1, h))
    px = col.load()
    span = max(1, h - 1)
    for y in range(h):
        f = y / span
        px[0, y] = tuple(round(top[i] + (bot[i] - top[i]) * f) for i in range(3))
    return col.resize((w, h))


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


def _card_tile(rank: "str | None" = None, suit: str = "") -> Image.Image:
    """Eine einzelne Karte (oder Rueckseite bei rank=None) als eigenes Bild -
    so laesst sie sich fuer Flip-/Slide-Animationen skalieren und verschieben."""
    tile = Image.new("RGBA", (CARD_W + 6, CARD_H + 8), (0, 0, 0, 0))
    if rank is None:
        _draw_back(tile, 0, 0)
    else:
        _draw_card(tile, 0, 0, rank, suit)
    return tile


def _bj_img(dealer: list, player: list, *, hide_hole: bool,
            dealer_value: int, player_value: int, player_state: str = "",
            n_dealer: "int | None" = None, n_player: "int | None" = None,
            slide: "tuple[str, int] | None" = None,
            hole_flip: "float | None" = None,
            hide_values: bool = False) -> Image.Image:
    """Blackjack-Tisch als Image. Animations-Parameter:
    ``n_dealer``/``n_player``: nur die ersten n Karten zeigen (Deal-Animation),
    ``slide``: ('player'|'dealer', dx) - letzte gezeigte Karte um dx versetzt,
    ``hole_flip``: 0..1 - die verdeckte Dealer-Karte dreht sich um,
    ``hide_values``: Wert-Pillen zeigen '-' (waehrend noch ausgeteilt wird)."""
    gap, pad, label_h, row_gap = 18, 30, 44, 24
    max_cards = max(len(dealer), len(player), 2)
    inner = max_cards * CARD_W + (max_cards - 1) * gap
    W = max(620, pad * 2 + inner)
    H = pad * 2 + 2 * (label_h + CARD_H) + row_gap
    nd = len(dealer) if n_dealer is None else min(n_dealer, len(dealer))
    np_ = len(player) if n_player is None else min(n_player, len(player))

    img = _vgrad(W, H, _FELT_TOP, _FELT_BOT).convert("RGBA")
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle([6, 6, W - 6, H - 6], radius=18, outline=(255, 255, 255, 40), width=2)

    def put(tile: Image.Image, x: int, y: int, squeeze: float = 1.0) -> None:
        if squeeze < 1.0:
            w = max(4, int(tile.width * squeeze))
            tile = tile.resize((w, tile.height))
            x += (CARD_W + 6 - w) // 2
        img.paste(tile, (x, y), tile)

    # Dealer-Reihe
    d.text((pad, pad - 2), "DEALER", font=_font(26), fill=_WHITE)
    dv = "–" if hide_values else ("?" if hide_hole else str(dealer_value))
    dv_bg = (231, 76, 60) if (not hide_hole and not hide_values
                              and dealer_value > 21) else (0, 0, 0, 110)
    _pill(d, pad + 132, pad - 6, dv, 22, dv_bg, _WHITE)
    ry = pad + label_h
    rx = _row_start(W, len(dealer), gap)
    for i, (r, s) in enumerate(dealer[:nd]):
        dx = slide[1] if (slide and slide[0] == "dealer" and i == nd - 1) else 0
        if i == 1 and hole_flip is not None:
            # Hole-Card dreht sich: erst Ruecken schmaler, dann Vorderseite breiter.
            if hole_flip < 0.5:
                put(_card_tile(), rx + dx, ry, squeeze=1.0 - 2.0 * hole_flip)
            else:
                put(_card_tile(r, s), rx + dx, ry, squeeze=2.0 * hole_flip - 1.0)
        elif hide_hole and i == 1:
            put(_card_tile(), rx + dx, ry)
        else:
            put(_card_tile(r, s), rx + dx, ry)
        rx += CARD_W + gap

    # Spieler-Reihe
    py = pad + label_h + CARD_H + row_gap
    d = ImageDraw.Draw(img, "RGBA")
    d.text((pad, py - 2), "DU", font=_font(26), fill=_WHITE)
    pstate_col = {
        "bust": (231, 76, 60), "lose": (231, 76, 60),
        "blackjack": (241, 196, 15), "win": (46, 204, 113),
        "push": (120, 130, 145),
    }.get(player_state, (0, 0, 0, 110))
    _pill(d, pad + 70, py - 6, "–" if hide_values else str(player_value),
          22, pstate_col, _WHITE)
    ry2 = py + label_h
    rx = _row_start(W, len(player), gap)
    for i, (r, s) in enumerate(player[:np_]):
        dx = slide[1] if (slide and slide[0] == "player" and i == np_ - 1) else 0
        put(_card_tile(r, s), rx + dx, ry2)
        rx += CARD_W + gap

    return img


def blackjack_table(dealer: list, player: list, *, hide_hole: bool,
                    dealer_value: int, player_value: int,
                    player_state: str = "") -> io.BytesIO:
    """Rendert den Blackjack-Tisch als ein Bild (statisch - Fallback und
    Grundlage der Frames von blackjack_table_anim)."""
    return _png(_bj_img(dealer, player, hide_hole=hide_hole,
                        dealer_value=dealer_value, player_value=player_value,
                        player_state=player_state))


def blackjack_table_anim(dealer: list, player: list, *, hide_hole: bool,
                         dealer_value: int, player_value: int,
                         player_state: str = "", mode: str = "hit") -> io.BytesIO:
    """Blackjack als GIF. ``mode``:
    - 'deal'   : die Startkarten werden einzeln ausgeteilt
    - 'hit'    : die zuletzt gezogene Spieler-Karte slidet ein
    - 'reveal' : die Hole-Card flippt um, Dealer-Karten erscheinen nacheinander
    Gewinn/Blackjack endet mit Blitz + Konfetti, Bust mit rotem Blitz."""
    kw = dict(hide_hole=hide_hole, dealer_value=dealer_value,
              player_value=player_value)
    frames: list[Image.Image] = []
    durations: list[int] = []

    if mode == "deal":
        # Reihenfolge wie am Tisch: Du, Dealer, Du, Dealer(verdeckt).
        schritte = [("player", 1, 1), ("dealer", 1, 1),
                    ("player", 1, 2), ("dealer", 2, 2)]
        for hand, ndl, npl in schritte:
            for dx in (46, 0):
                frames.append(_bj_img(dealer, player, **kw, n_dealer=ndl,
                                      n_player=npl, slide=(hand, dx),
                                      hide_values=True))
                durations.append(70)
    elif mode == "reveal":
        for flip in (0.12, 0.5, 0.88):
            frames.append(_bj_img(dealer, player, **{**kw, "hide_hole": False},
                                  n_dealer=2, hole_flip=flip))
            durations.append(90)
        for k in range(3, len(dealer) + 1):
            for dx in (46, 0):
                frames.append(_bj_img(dealer, player, **{**kw, "hide_hole": False},
                                      n_dealer=k, slide=("dealer", dx)))
                durations.append(80)
    else:  # 'hit'
        for dx in (64, 22):
            frames.append(_bj_img(dealer, player, **kw, slide=("player", dx)))
            durations.append(65)

    final = _bj_img(dealer, player, **kw, player_state=player_state)
    if player_state in ("win", "blackjack"):
        farbe = (241, 196, 15) if player_state == "blackjack" else (46, 204, 113)
        frames.append(_flash(final, farbe, 55))
        durations.append(100)
        for ct in (0.3, 0.65):
            conf = final.copy()
            _confetti(conf, ct, seed=31)
            frames.append(conf)
            durations.append(140)
    elif player_state == "bust":
        frames.append(_flash(final, (231, 76, 60), 60))
        durations.append(110)
    frames.append(final)
    durations.append(3600)
    return _gif(frames, durations)


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


def _dice_img(rolls: list, sides: int, *, jitter: int = 0,
              show_sum: bool = True, seed: int = 0) -> Image.Image:
    """Wuerfelbild als Image. ``jitter``: max. Versatz in px (Kullern)."""
    n = max(1, len(rolls))
    die, gap, pad = 120, 20, 28
    per_row = min(n, 8)
    nrows = (n + per_row - 1) // per_row
    W = max(260, pad * 2 + per_row * die + (per_row - 1) * gap)
    grid_h = nrows * die + (nrows - 1) * gap
    H = pad * 2 + grid_h + (44 if n > 1 else 0)
    img = _vgrad(W, H, (30, 34, 46), (14, 16, 22)).convert("RGBA")
    rng = random.Random(seed)
    x0 = (W - (per_row * die + (per_row - 1) * gap)) // 2
    for i, r in enumerate(rolls):
        rr, cc = divmod(i, per_row)
        jx = rng.randint(-jitter, jitter) if jitter else 0
        jy = rng.randint(-jitter, jitter) if jitter else 0
        _die(img, x0 + cc * (die + gap) + jx, pad + rr * (die + gap) + jy, die, r, sides)
    if n > 1 and show_sum:
        d = ImageDraw.Draw(img, "RGBA")
        _pill_c(d, W / 2, pad + grid_h + 8, f"Summe  {sum(rolls)}", 24,
                (245, 197, 24), (22, 14, 4))
    return img


def dice_roll(rolls: list, sides: int) -> io.BytesIO:
    """Wuerfel als Bild. d6 zeigt Augen, sonst die Zahl + 'W<n>'. Bei vielen
    Wuerfeln (>8) wird in mehrere Reihen umgebrochen, damit es kompakt bleibt."""
    return _png(_dice_img(rolls, sides))


def dice_roll_anim(rolls: list, sides: int) -> io.BytesIO:
    """Wuerfeln als GIF: die Wuerfel kullern (zufaellige Zwischen-Augen +
    Wackeln), rasten dann nacheinander auf dem Ergebnis ein."""
    frames: list[Image.Image] = []
    durations: list[int] = []
    n = len(rolls)
    tumble = 6
    for f in range(tumble):
        zufall = [random.randint(1, sides) if f < tumble - 1 - (i % 2) else rolls[i]
                  for i in range(n)]
        frames.append(_dice_img(zufall, sides, jitter=max(1, 7 - f), show_sum=False,
                                seed=f))
        durations.append(80)
    frames.append(_dice_img(rolls, sides, jitter=1, show_sum=False, seed=99))
    durations.append(90)
    frames.append(_dice_img(rolls, sides))
    durations.append(3500)
    return _gif(frames, durations)


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
    return _png(_keno_img(picks, draw, hits))


def _keno_img(picks: list, draw: list, hits: list,
              pop: "int | None" = None) -> Image.Image:
    """Zeichnet das Keno-Raster als Image (fuer PNG und GIF-Frames).
    ``pop``: diese frisch gezogene Zahl wird groesser + heller gezeichnet."""
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
            grow = 4 if num == pop else 0     # frisch gezogen: kurz aufploppen
            d.rounded_rectangle([x - grow, y - grow, x + cell + grow, y + cell + grow],
                                radius=12 + grow, fill=bg,
                                outline=(255, 255, 255) if grow else ol,
                                width=3 if grow else 2)
            d.text((x + cell / 2, y + cell / 2), str(num),
                   font=_font(26 if grow else 22), fill=fg, anchor="mm")
            if grow and num in hitset:        # Treffer: Funkeln am Feld
                _sparkle(d, x - 6, y - 4, 8, (255, 240, 170))
                _sparkle(d, x + cell + 6, y + cell + 2, 7, (255, 240, 170))
            x += cell + gap
        y += cell + gap
    ly = y + 8
    lx = pad
    lx += _legend(d, lx, ly, (241, 196, 15), f"Treffer {len(hits)}")
    lx += _legend(d, lx, ly, (41, 55, 90), "dein Tipp")
    _legend(d, lx, ly, (66, 70, 88), "gezogen")
    return img


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


# === Quote-Meme ("Flo quote") ===========================================
_RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS
_notdef_q: dict[int, bytes] = {}


def _renderable_char(font, ch: str) -> bool:
    """True, wenn die Schrift fuer ch ein echtes Glyph hat (kein Tofu/leer)."""
    if ord(ch) >= 0x1F000:               # Emoji-/Symbol-Zusatzebenen
        return False
    fid = id(font)
    nd = _notdef_q.get(fid)
    if nd is None:
        im = Image.new("L", (48, 48), 0)
        ImageDraw.Draw(im).text((6, 6), "￿", font=font, fill=255)
        nd = im.tobytes()
        _notdef_q[fid] = nd
    im = Image.new("L", (48, 48), 0)
    try:
        ImageDraw.Draw(im).text((6, 6), ch, font=font, fill=255)
    except Exception:
        return False
    b = im.tobytes()
    return bool(b) and b != nd and any(b)


def _clean_text(s: str) -> str:
    """Macht Text darstellbar: NFKC, Emoji/Steuer-/Zalgo-/unbekannte Glyphen raus."""
    s = unicodedata.normalize("NFKC", s or "")
    ref = _font(40)
    out: list[str] = []
    for ch in s:
        if ch in ("\n", "\t", " "):
            out.append(" ")
            continue
        cat = unicodedata.category(ch)
        if cat[0] == "M" or cat in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue
        if _renderable_char(ref, ch):
            out.append(ch)
    return " ".join("".join(out).split()).strip()


def _wrap(d, text: str, font, max_w: int) -> list[str]:
    """Bricht Text auf max_w Pixel um (Wort-weise; lange Woerter hart trennen)."""
    lines: list[str] = []
    for word in text.split():
        if not lines or d.textlength(lines[-1] + " " + word, font=font) > max_w:
            if d.textlength(word, font=font) <= max_w or not lines:
                lines.append(word)
            else:
                cur = ""
                for ch in word:
                    if d.textlength(cur + ch, font=font) <= max_w:
                        cur += ch
                    else:
                        lines.append(cur)
                        cur = ch
                if cur:
                    lines.append(cur)
        else:
            lines[-1] += " " + word
    return lines or [""]


def quote_card(avatar: "bytes | None", text: str, author: str) -> io.BytesIO:
    """Zitat-Bild im 'make it a quote'-Stil: links das (graustufige) Profilbild
    mit Verlauf ins Schwarze, rechts das Zitat + '- Name' darunter."""
    W, H, AVW = 1200, 630, 620
    img = Image.new("RGB", (W, H), (0, 0, 0))

    # Profilbild links: Graustufen, quadratisch gefittet, Alpha-Verlauf nach schwarz.
    if avatar:
        try:
            av = Image.open(io.BytesIO(avatar)).convert("L")
            av = ImageOps.autocontrast(ImageOps.fit(av, (AVW, H), method=_RESAMPLE))
            grad = Image.new("L", (AVW, 1))
            gpx = grad.load()
            solid = int(AVW * 0.42)
            for x in range(AVW):
                gpx[x, 0] = 255 if x < solid else max(0, int(255 * (1 - (x - solid) / (AVW - solid))))
            img.paste(av.convert("RGB"), (0, 0), grad.resize((AVW, H)))
        except Exception:
            pass

    d = ImageDraw.Draw(img)
    tx0, tx1 = 640, W - 56
    tw = tx1 - tx0

    # Zitat: groesste Schrift, die in Breite UND Hoehe passt.
    quote = _clean_text(text) or "..."
    quote = f"„{quote}“"        # „..."
    chosen, lines = 50, [quote]
    for size in range(54, 23, -2):
        f = _font(size)
        ls = _wrap(d, quote, f, tw)
        if (size + 12) * len(ls) <= H - 210:
            chosen, lines = size, ls
            break
    f = _font(chosen)
    line_h = chosen + 12
    block_h = line_h * len(lines)
    y = (H - block_h) // 2 - 16
    for ln in lines:
        lw = d.textlength(ln, font=f)
        d.text((tx0 + (tw - lw) / 2, y), ln, font=f, fill=_WHITE)
        y += line_h

    # Autor darunter ("- Name").
    author = _clean_text(author) or "Unbekannt"
    fa = _font(30)
    aut = f"— {author}"
    aw = d.textlength(aut, font=fa)
    d.text((tx0 + (tw - aw) / 2, y + 20), aut, font=fa, fill=(160, 162, 172))

    return _png(img)


# === Ernaehrungs-Karte ("Kalorien-Channel") ===============================
def _round_img(data: bytes, w: int, h: int, radius: int = 0) -> "Image.Image | None":
    """Bild-Bytes -> RGBA, auf w x h gefittet, optional mit runden Ecken."""
    try:
        im = ImageOps.fit(Image.open(io.BytesIO(data)).convert("RGB"), (w, h),
                          method=_RESAMPLE)
    except Exception:  # noqa: BLE001
        return None
    out = im.convert("RGBA")
    if radius > 0:
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1],
                                               radius=radius, fill=255)
        out.putalpha(mask)
    return out


def _fnum(val) -> float:
    """Robuste Zahl aus beliebigen (LLM-)Werten: 1200, "1200", "ca. 1200 kcal",
    "8/10" -> erste Zahl; sonst 0. Schuetzt die Karte unabhaengig vom Aufrufer."""
    if isinstance(val, (int, float)):
        return float(val)
    import re as _re
    m = _re.search(r"-?\d+(?:[.,]\d+)?", str(val or ""))
    return float(m.group(0).replace(",", ".")) if m else 0.0


def _score_color(score: float) -> tuple:
    """0 (Industrie, rot) -> 10 (natuerlich, gruen), stufenlos."""
    f = max(0.0, min(1.0, score / 10.0))
    r1, g1, b1 = (231, 76, 60)     # rot
    r2, g2, b2 = (46, 204, 113)    # gruen
    return (round(r1 + (r2 - r1) * f), round(g1 + (g2 - g1) * f),
            round(b1 + (b2 - b1) * f))


def _hbar(d, x: int, y: int, w: int, h: int, frac: float, color,
          track=(38, 42, 50)) -> None:
    """Horizontaler Wert-Balken mit rundem Track."""
    frac = max(0.0, min(1.0, frac))
    r = h // 2
    d.rounded_rectangle([x, y, x + w, y + h], radius=r, fill=track)
    fw = int(round(w * frac))
    if frac > 0 and fw < h:
        fw = h
    if fw > 0:
        d.rounded_rectangle([x, y, x + fw, y + h], radius=r, fill=color)


def nutrition_card(food_img: "bytes | None", data: dict) -> io.BytesIO:
    """Ernaehrungs-Karte: links das Essensfoto, rechts Kalorien, Makros,
    Natuerlichkeits-Score (gruen=natuerlich, rot=Industrie) und Fazit."""
    W, H, PW = 1200, 640, 470
    img = _vgrad(W, H, (24, 27, 35), (13, 15, 20)).convert("RGBA")
    d = ImageDraw.Draw(img)

    # --- Foto links (abgerundet), sonst Emoji-Platzhalter ---
    photo = _round_img(food_img, PW - 40, H - 48, radius=22) if food_img else None
    if photo is not None:
        img.paste(photo, (24, 24), photo)
    else:
        d.rounded_rectangle([24, 24, PW - 16, H - 24], radius=22, fill=(30, 34, 44))
        d.text((PW // 2 - 40, H // 2 - 60), "🍽", font=_font(96), fill=(90, 96, 110))

    x0, x1 = PW + 24, W - 44
    tw = x1 - x0

    # --- Gerichtname (bis 2 Zeilen, automatisch verkleinert) ---
    name = _clean_text(str(data.get("gericht") or "Unbekanntes Gericht"))[:80] or "Essen"
    nf, nlines = _font(44), [name]
    for size in (44, 38, 32, 27):
        nf = _font(size)
        nlines = _wrap(d, name, nf, tw)
        if len(nlines) <= 2:
            break
    nlines = nlines[:2]
    y = 34
    for ln in nlines:
        d.text((x0, y), ln, font=nf, fill=_WHITE)
        y += nf.size + 8

    # --- Kalorien gross ---
    y += 10
    kcal = int(_fnum(data.get("kcal")))
    kmin, kmax = int(_fnum(data.get("kcal_min"))), int(_fnum(data.get("kcal_max")))
    d.text((x0, y), f"{kcal}", font=_font(76), fill=_GOLD)
    kw = d.textlength(f"{kcal}", font=_font(76))
    d.text((x0 + kw + 14, y + 40), "kcal", font=_font(30), fill=(150, 155, 168))
    if kmax > kmin > 0:
        d.text((x0 + kw + 100, y + 46), f"(≈ {kmin}–{kmax})", font=_font(20),
               fill=(120, 125, 138))
    y += 100

    # --- Makro-Balken ---
    macros = [
        ("EIWEISS", _fnum(data.get("protein_g")), (46, 204, 113)),
        ("KOHLENHYDRATE", _fnum(data.get("carbs_g")), (87, 148, 242)),
        ("FETT", _fnum(data.get("fett_g")), (255, 152, 48)),
        ("ZUCKER", _fnum(data.get("zucker_g")), (240, 98, 146)),
    ]
    peak = max([m[1] for m in macros] + [1.0])
    lf, vf = _font(17), _font(21)
    for label, grams, col in macros:
        d.text((x0, y), label, font=lf, fill=(140, 145, 158))
        _hbar(d, x0 + 190, y + 3, tw - 300, 16, grams / peak, col)
        d.text((x1 - 92, y - 1), f"{grams:g} g", font=vf, fill=_WHITE)
        y += 38
    y += 14

    # --- Natuerlichkeits-Score ---
    score = max(0.0, min(10.0, _fnum(data.get("natur_score"))))
    scol = _score_color(score)
    d.text((x0, y), "NATÜRLICHKEIT", font=lf, fill=(140, 145, 158))
    d.text((x1 - 92, y - 3), f"{score:g}/10", font=_font(24), fill=scol)
    _hbar(d, x0, y + 26, tw - 110, 20, score / 10.0, scol)
    y += 62
    verarbeitung = _clean_text(str(data.get("verarbeitung") or ""))[:60]
    if verarbeitung:
        d.text((x0, y), verarbeitung, font=_font(20), fill=scol)
        y += 34

    # --- Fazit-Pille + Flo-Spruch ---
    if score >= 7:
        pill_txt = "✓ GUT FÜR DEINEN KÖRPER"
    elif score >= 4:
        pill_txt = "~ GEHT SO – IN MASSEN"
    else:
        pill_txt = "✗ INDUSTRIE – LASS ES LIEBER"
    _pill(d, x0, y + 4, pill_txt.replace("✓ ", "").replace("✗ ", "").replace("~ ", ""),
          22, scol, (13, 15, 20))
    y += 62
    spruch = _clean_text(str(data.get("flo_spruch") or data.get("fazit") or ""))
    if spruch:
        sf = _font(19)
        for ln in _wrap(d, f"„{spruch[:180]}“", sf, tw)[:3]:
            d.text((x0, y), ln, font=sf, fill=(165, 170, 182))
            y += 26

    d.rounded_rectangle([6, 6, W - 7, H - 7], radius=18,
                        outline=(48, 53, 63), width=2)
    return _png(img)


# === Level-Karte (Rank-Card als Bild) =====================================
# Luxus-Rahmen (Flo Luxus Shop): je teurer, desto edler der Kartenrand.
_FRAME_STYLES = {
    "bronze": {"col": (205, 127, 50), "col2": None, "label": "BRONZE"},
    "silber": {"col": (200, 205, 214), "col2": None, "label": "SILBER"},
    "gold": {"col": (241, 196, 15), "col2": None, "label": "GOLD"},
    "diamant": {"col": (120, 220, 255), "col2": (210, 245, 255), "label": "DIAMANT"},
    "galaxie": {"col": (155, 89, 182), "col2": (87, 148, 242), "label": "GALAXIE"},
    "imperium": {"col": (241, 196, 15), "col2": (231, 76, 60), "label": "IMPERATOR"},
}
_FRAME_SPARKLE = ("gold", "diamant", "galaxie", "imperium")


def level_card(avatar: "bytes | None", *, name: str, level: int, into: int,
               step: int, place: int, total: int, xp: int, coins: int,
               msgs: int, voice_secs: int, streak: int, title: str = "",
               accent: "tuple | None" = None,
               frame: "str | None" = None) -> io.BytesIO:
    """Rank-Card: Avatar mit Ring, Name, Titel, Level + Platz, XP-Balken und
    Stat-Zeile (Coins, Nachrichten, Voice, Streak). ``frame``: Luxus-Rahmen
    aus dem Flo-Luxus-Shop (bronze/silber/gold/diamant/galaxie/imperium)."""
    W, H = 1000, 320
    acc = accent or (88, 101, 242)   # Blurple, ausser eine Titel-Farbe kommt mit
    img = _vgrad(W, H, (26, 29, 38), (13, 15, 20)).convert("RGBA")
    d = ImageDraw.Draw(img)

    # --- Avatar links (rund, mit Akzent-Ring) ---
    AD = 190
    ax, ay = 42, (H - AD) // 2
    circ = None
    if avatar:
        try:
            im = ImageOps.fit(Image.open(io.BytesIO(avatar)).convert("RGB"),
                              (AD, AD), method=_RESAMPLE)
            mask = Image.new("L", (AD, AD), 0)
            ImageDraw.Draw(mask).ellipse([0, 0, AD - 1, AD - 1], fill=255)
            circ = im.convert("RGBA")
            circ.putalpha(mask)
        except Exception:  # noqa: BLE001
            circ = None
    if circ is not None:
        img.paste(circ, (ax, ay), circ)
    else:
        d.ellipse([ax, ay, ax + AD, ay + AD], fill=(34, 38, 48))
        ini = (name[:1] or "?").upper()
        iw = d.textlength(ini, font=_font(84))
        d.text((ax + AD / 2 - iw / 2, ay + AD / 2 - 52), ini, font=_font(84),
               fill=(120, 126, 140))
    d.ellipse([ax - 5, ay - 5, ax + AD + 4, ay + AD + 4], outline=acc, width=5)

    x0, x1 = ax + AD + 46, W - 46
    tw = x1 - x0

    # --- Name + Titel-Pille ---
    safe = _clean_text(name)[:32] or "Spieler"
    nf = _font(46) if d.textlength(safe, font=_font(46)) <= tw - 220 else _font(34)
    d.text((x0, 34), safe, font=nf, fill=_WHITE)
    if title:
        t = _clean_text(title)[:28]
        if t:
            tf = _font(19)
            pw = int(d.textlength(t, font=tf) + 26)
            px = x1 - pw
            d.rounded_rectangle([px, 40, px + pw, 40 + 32], radius=16, fill=acc)
            d.text((px + 13, 45), t, font=tf, fill=(13, 15, 20))

    # --- Level + Platz (keine Emojis - die Schrift hat keine Emoji-Glyphen) ---
    d.text((x0, 96), f"Level {level}", font=_font(34), fill=_GOLD)
    lw = d.textlength(f"Level {level}", font=_font(34))
    d.text((x0 + lw + 26, 104), f"Platz #{place} von {total}", font=_font(22),
           fill=(150, 155, 168))

    # --- XP-Balken ---
    pct = 1.0 if step <= 0 else max(0.0, min(1.0, into / step))
    _hbar(d, x0, 156, tw, 26, pct, acc)
    d.text((x0, 192), f"{into} / {step} XP bis Level {level + 1}",
           font=_font(19), fill=(150, 155, 168))
    ptxt = f"{round(pct * 100)}%"
    d.text((x1 - d.textlength(ptxt, font=_font(19)), 192), ptxt,
           font=_font(19), fill=(150, 155, 168))

    # --- Stat-Zeile (Label ueber Wert; Text statt Emoji - kein Tofu) ---
    h, rem = divmod(int(voice_secs), 3600)
    m = rem // 60
    vtxt = f"{h}h {m}m" if h else f"{m}m"
    stats = [("COINS", f"{coins:,}".replace(",", ".")),
             ("NACHRICHTEN", f"{msgs:,}".replace(",", ".")),
             ("VOICE", vtxt),
             ("STREAK", f"{streak} Tag(e)")]
    lf2, vf2 = _font(15), _font(23)
    seg = tw // len(stats)
    for i, (label, val) in enumerate(stats):
        sx = x0 + i * seg
        d.text((sx, 232), label, font=lf2, fill=(120, 126, 140))
        d.text((sx, 254), val, font=vf2, fill=(200, 205, 218))

    style = _FRAME_STYLES.get(frame or "")
    if style is None:
        d.rounded_rectangle([6, 6, W - 7, H - 7], radius=18, outline=(48, 53, 63), width=2)
    else:
        # Luxus-Rahmen: kraeftiger Aussenrand + feine Innenlinie (zweifarbig
        # bei Diamant/Galaxie/Imperium), Funkeln ab Gold, Label unterm Avatar.
        col, col2 = style["col"], style["col2"] or style["col"]
        d.rounded_rectangle([4, 4, W - 5, H - 5], radius=20, outline=col, width=5)
        d.rounded_rectangle([12, 12, W - 13, H - 13], radius=14, outline=col2, width=2)
        if frame in _FRAME_SPARKLE:
            _sparkle(d, 26, 26, 9, col2)
            _sparkle(d, W - 28, 30, 8, col)
            _sparkle(d, W - 44, H - 30, 9, col2)
            _sparkle(d, 40, H - 26, 7, col)
        lf3 = _font(15)
        lbl = style["label"]
        lw = int(d.textlength(lbl, font=lf3)) + 22
        lx = ax + (AD - lw) // 2
        d.rounded_rectangle([lx, H - 40, lx + lw, H - 16], radius=12, fill=col)
        d.text((lx + 11, H - 36), lbl, font=lf3, fill=(18, 16, 10))
    return _png(img)


# === Animationen (GIF) ====================================================
# Alle *_anim-Funktionen liefern ein animiertes GIF als io.BytesIO. Sie sind
# CPU-gebunden (Pillow) - die Aufrufer starten sie deshalb via
# asyncio.to_thread, damit der Event-Loop nie blockiert. Frame-Anzahl und
# Groessen sind bewusst klein gehalten (Discord spielt GIFs im Embed ab).
def _gif(frames: list, durations, *, colors: int = 192) -> io.BytesIO:
    """Frames (RGB/RGBA-Images) -> animiertes GIF. Die gemeinsame Palette wird
    aus Anfangs-, Mittel- UND Endframe gebaut - nur das Endbild wuerde Farben
    verlieren, die es selbst nicht enthaelt (z. B. Walzen-Symbole mitten im
    Lauf oder die blaue Steigkurve beim Crash)."""
    if len(frames) > 2:
        a = frames[0].convert("RGB")
        m = frames[len(frames) // 2].convert("RGB")
        z = frames[-1].convert("RGB")
        ref = Image.new("RGB", (a.width * 3, a.height))
        ref.paste(a, (0, 0))
        ref.paste(m, (a.width, 0))
        ref.paste(z, (a.width * 2, 0))
    else:
        ref = frames[-1].convert("RGB")
    pal = ref.convert("P", palette=Image.ADAPTIVE, colors=colors)
    quant = [f.convert("RGB").quantize(palette=pal, dither=Image.Dither.NONE)
             for f in frames]
    if isinstance(durations, int):
        durations = [durations] * len(quant)
    buf = io.BytesIO()
    quant[0].save(buf, format="GIF", save_all=True, append_images=quant[1:],
                  duration=durations, loop=0, optimize=False)
    buf.seek(0)
    return buf


def _ease_out(t: float) -> float:
    """Kubisches Aus-Bremsen: schnell starten, weich zum Stillstand."""
    return 1.0 - (1.0 - t) ** 3


_CONFETTI_COLS = [(241, 196, 15), (46, 204, 113), (87, 148, 242),
                  (231, 76, 60), (155, 89, 182), (250, 250, 252)]


def _confetti(img: Image.Image, t: float, *, seed: int = 7, n: int = 46) -> None:
    """Fallendes Konfetti ueber das ganze Bild. ``t``: 0..1 Fortschritt der
    Animation - gleiche Seed ergibt eine koherente Flugbahn ueber die Frames."""
    rng = random.Random(seed)
    W, H = img.size
    d = ImageDraw.Draw(img, "RGBA")
    for i in range(n):
        x0 = rng.uniform(0, W)
        speed = rng.uniform(0.6, 1.25)
        size = rng.uniform(3.0, 6.5)
        drift = rng.uniform(-36, 36)
        rot = rng.uniform(0, math.pi)
        col = _CONFETTI_COLS[i % len(_CONFETTI_COLS)]
        y = t * speed * (H + 60) - 30
        x = x0 + drift * t
        if y < -12 or y > H + 12:
            continue
        a = rot + t * 7 + i
        dx, dy = math.cos(a) * size, math.sin(a) * size
        d.line([(x - dx, y - dy), (x + dx, y + dy)], fill=col, width=3)


def _sparkle(d: ImageDraw.ImageDraw, x: float, y: float, r: float,
             col: tuple = (255, 255, 255)) -> None:
    """Kleiner 4-Strahlen-Funkel-Stern."""
    d.line([(x - r, y), (x + r, y)], fill=col, width=2)
    d.line([(x, y - r), (x, y + r)], fill=col, width=2)
    rr = r * 0.45
    d.line([(x - rr, y - rr), (x + rr, y + rr)], fill=col, width=1)
    d.line([(x - rr, y + rr), (x + rr, y - rr)], fill=col, width=1)


def _flash(img: Image.Image, color: tuple, alpha: int) -> Image.Image:
    """Kurzer Vollbild-Blitz (z. B. gruen bei Gewinn, rot bei Crash)."""
    overlay = Image.new("RGBA", img.size, (*color, alpha))
    return Image.alpha_composite(img.convert("RGBA"), overlay)


# --- Muenzwurf (animiert) --------------------------------------------------
def _coin_face(d: ImageDraw.ImageDraw, cx: float, cy: float, R: float,
               face: str, squash: float, *, shadow_y: float | None = None,
               shadow_scale: float = 1.0) -> None:
    """Zeichnet die Muenze mit vertikaler Stauchung (squash 0..1 = Kante..voll).
    Der Schatten bleibt am Boden (shadow_y) und schrumpft, wenn die Muenze
    hoch fliegt (shadow_scale)."""
    ry = max(5.0, R * squash)
    if shadow_y is not None:
        sw = R * shadow_scale
        d.ellipse([cx - sw, shadow_y - sw * 0.16, cx + sw, shadow_y + sw * 0.16],
                  fill=(0, 0, 0, 90))
    d.ellipse([cx - R, cy - ry, cx + R, cy + ry], fill=(250, 202, 46),
              outline=(150, 110, 10), width=5)
    d.ellipse([cx - R * 0.82, cy - ry * 0.82, cx + R * 0.82, cy + ry * 0.82],
              outline=(214, 172, 40), width=3)
    if squash >= 0.6:
        if face == "kopf":
            _crown(d, cx, cy - ry * 0.10, R * 0.40 * squash, (150, 110, 10))
        else:
            d.text((cx, cy - ry * 0.08), "★", font=_font(int(R * 0.9 * squash)),
                   fill=(150, 110, 10), anchor="mm")


def coin_flip_anim(result: str) -> io.BytesIO:
    """Muenzwurf als GIF: die Muenze fliegt im Bogen hoch, flippt dabei,
    landet mit zwei kleinen Huepfern und funkelt auf dem Ergebnis."""
    W = H = 320
    other = "zahl" if result == "kopf" else "kopf"
    ground = H / 2 + 96          # Boden fuer den Schatten
    rest_y = H / 2 - 4           # Ruhelage der Muenze
    R = 96
    # Flugbahn: (hoehe 0..1, squash, face) - hoch mit schnellen Flips, runter
    # mit langsameren, dann zwei kleine Bounces auf dem Ergebnis.
    seq = [
        (0.10, 1.00, other), (0.42, 0.45, other), (0.72, 0.10, result),
        (0.92, 0.50, result), (1.00, 1.00, other), (0.96, 0.45, other),
        (0.80, 0.10, result), (0.58, 0.55, other), (0.34, 1.00, other),
        (0.16, 0.45, result), (0.04, 0.85, result),
        (0.10, 1.00, result),    # Bounce 1
        (0.00, 0.90, result),
        (0.03, 1.00, result),    # Bounce 2
        (0.00, 1.00, result),
    ]
    frames: list[Image.Image] = []
    for h, squash, face in seq:
        img = _vgrad(W, H, (26, 32, 52), (12, 15, 26)).convert("RGBA")
        d = ImageDraw.Draw(img, "RGBA")
        cy = rest_y - h * 116
        _coin_face(d, W / 2, cy, R, face, squash,
                   shadow_y=ground, shadow_scale=1.0 - 0.45 * h)
        frames.append(img)
    # Endbild: Ergebnis + Label + Funkeln (+ dezentes Konfetti).
    img = _vgrad(W, H, (26, 32, 52), (12, 15, 26)).convert("RGBA")
    d = ImageDraw.Draw(img, "RGBA")
    _coin_face(d, W / 2, rest_y, R, result, 1.0, shadow_y=ground)
    d.text((W / 2, H - 34), result.upper(), font=_font(34),
           fill=(245, 197, 24), anchor="mm")
    for sx, sy, sr in ((W / 2 - 118, rest_y - 92, 9), (W / 2 + 112, rest_y - 60, 7),
                       (W / 2 + 86, rest_y + 88, 8), (W / 2 - 92, rest_y + 70, 6)):
        _sparkle(d, sx, sy, sr, (255, 240, 170))
    frames.append(img)
    durations = [55] * (len(frames) - 1) + [3500]
    return _gif(frames, durations)


# --- Slots (animiert) ------------------------------------------------------
def _slot_scroll_tile(s: int, sym_now: str, sym_next: str, frac: float) -> Image.Image:
    """Ein Walzenfenster mit ECHT durchlaufenden Symbolen: das aktuelle rollt
    nach unten raus, das naechste laeuft von oben ein. frac 0..1 = Fortschritt."""
    tile = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    td = ImageDraw.Draw(tile)
    td.rounded_rectangle([0, 0, s - 1, s - 1], radius=16, fill=(248, 249, 252),
                         outline=(60, 64, 80), width=3)
    dy = frac * s
    _slot_symbol(td, s / 2, s / 2 + dy, s * 0.3, sym_now)
    _slot_symbol(td, s / 2, s / 2 + dy - s, s * 0.3, sym_next)
    # Bewegungs-Streifen + Walzen-Woelbung: sauber per alpha_composite blenden
    # (direktes Zeichnen mit Alpha ERSETZT auf RGBA-Bildern die Pixel und
    # wuerde das Fenster durchsichtig machen).
    overlay = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle([3, 3, s - 4, 16], fill=(150, 155, 172, 80))
    od.rectangle([3, s - 17, s - 4, s - 4], fill=(150, 155, 172, 80))
    for off in (int(s * 0.30), int(s * 0.62)):
        od.rounded_rectangle([10, off, s - 10, off + 9], radius=4,
                             fill=(255, 255, 255, 120))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1], radius=16, fill=255)
    overlay.putalpha(ImageChops.multiply(overlay.getchannel("A"), mask))
    return Image.alpha_composite(tile, overlay)


def _slot_still_tile(s: int, key: str, dy: float = 0.0) -> Image.Image:
    """Stehendes Walzenfenster (dy: kleiner Bounce-Versatz beim Einrasten)."""
    tile = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    td = ImageDraw.Draw(tile)
    td.rounded_rectangle([0, 0, s - 1, s - 1], radius=16, fill=(248, 249, 252),
                         outline=(60, 64, 80), width=3)
    _slot_symbol(td, s / 2, s / 2 + dy, s * 0.3, key)
    return tile


def _slot_stage(f: int, *, all_lit: bool = False) -> tuple[Image.Image, int, int, int]:
    """Grundbild der Maschine: Rahmen, Titel und blinkende Marquee-Lampen.
    Rueckgabe: (bild, pad, fenster_y, tile)."""
    pad, tile, gap, top_h, bot_h = 26, 150, 18, 72, 58
    W = pad * 2 + 3 * tile + 2 * gap
    H = pad * 2 + top_h + tile + bot_h
    img = _vgrad(W, H, (34, 18, 48), (16, 10, 26)).convert("RGBA")
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle([8, 8, W - 8, H - 8], radius=20, outline=(245, 197, 24, 200), width=4)
    # Marquee-Lampen oben + unten: laufendes Blinken wie am echten Automaten.
    n_bulbs = 11
    for i in range(n_bulbs):
        bx = 26 + i * (W - 52) / (n_bulbs - 1)
        lit = all_lit or ((i + f) % 2 == 0)
        col = (255, 214, 74) if lit else (74, 58, 96)
        for by in (18, H - 18):
            d.ellipse([bx - 5, by - 5, bx + 5, by + 5], fill=col,
                      outline=(140, 110, 20) if lit else (40, 32, 56), width=1)
    d.text((W / 2, pad + 6), "★  FLO  SLOTS  ★", font=_font(34),
           fill=(245, 197, 24), anchor="ma")
    ry = pad + top_h
    d.rounded_rectangle([pad - 8, ry - 8, W - pad + 8, ry + tile + 8], radius=16,
                        fill=(8, 6, 14))
    return img, pad, ry, tile


def slot_machine_anim(symbols: list, *, win: int = 0, jackpot: bool = False) -> io.BytesIO:
    """Slots als GIF: drei Walzen scrollen echt durch, stoppen nacheinander mit
    einem kleinen Bounce; bei Gewinn blitzt die Linie, beim Jackpot regnet
    Konfetti und alle Lampen leuchten."""
    pad, tile, gap = 26, 150, 18
    stops = (7, 11, 15)
    total = 17
    speed = (0.58, 0.66, 0.74)               # jede Walze etwas anders schnell
    # Scroll-Reihenfolge je Walze: gemischt, endet nahtlos im Zielsymbol.
    seqs: list[list[str]] = []
    for i in range(3):
        pool = [k for k in SLOT_KEYS if k != symbols[i]]
        random.shuffle(pool)
        seqs.append((pool * 4) + [symbols[i]])
    frames: list[Image.Image] = []
    for f in range(total):
        img, _pad, ry, _tile = _slot_stage(f)
        rx = pad
        for i in range(3):
            if f < stops[i]:
                prog = (stops[i] - 1 - f) * speed[i]      # rueckwaerts bis 0 am Stop
                idx = int(prog)
                seq = seqs[i]
                sym_now = seq[-(idx % len(seq)) - 1]
                sym_next = seq[-((idx + 1) % len(seq)) - 1]
                t = _slot_scroll_tile(tile, sym_now, sym_next, 1.0 - (prog - idx))
            else:
                settled = f - stops[i]
                dy = {0: 13.0, 1: -5.0}.get(settled, 0.0)
                t = _slot_still_tile(tile, symbols[i], dy)
            img.paste(t, (rx, ry), t)
            rx += tile + gap
        d = ImageDraw.Draw(img, "RGBA")
        ly = ry + tile / 2
        d.line([(pad - 2, ly), (pad * 2 + 3 * tile + 2 * gap - pad + 2, ly)],
               fill=(96, 100, 118), width=3)
        _pill_c(d, img.width / 2, ry + tile + 14, "· · ·", 24, (40, 34, 58),
                (180, 170, 200))
        frames.append(img)

    # Ergebnis-Frames: Gewinn blitzt, Jackpot bekommt Konfetti-Regen.
    def result_frame(lit: bool, flash_line: bool, conf_t: float | None) -> Image.Image:
        img, _pad, ry, _tile = _slot_stage(0, all_lit=lit)
        rx = pad
        for i in range(3):
            t = _slot_still_tile(tile, symbols[i])
            img.paste(t, (rx, ry), t)
            rx += tile + gap
        d = ImageDraw.Draw(img, "RGBA")
        ly = ry + tile / 2
        line_col = (46, 204, 113) if win > 0 else (96, 100, 118)
        if flash_line:
            line_col = (140, 255, 190)
        d.line([(pad - 2, ly), (img.width - pad + 2, ly)], fill=line_col,
               width=5 if flash_line else 3)
        by = ry + tile + 14
        if jackpot:
            _pill_c(d, img.width / 2, by, "JACKPOT!", 30, (245, 197, 24), (28, 16, 4))
        elif win > 0:
            _pill_c(d, img.width / 2, by, f"GEWINN  +{win}", 26,
                    (46, 204, 113), (8, 28, 16))
        else:
            _pill_c(d, img.width / 2, by, "leider nichts", 24,
                    (70, 74, 92), (228, 230, 238))
        if conf_t is not None:
            _confetti(img, conf_t, seed=13)
        return img

    if jackpot:
        frames.append(_flash(result_frame(True, True, None), (255, 240, 160), 60))
        for ct in (0.25, 0.55, 0.85):
            frames.append(result_frame(True, False, ct))
        frames.append(result_frame(True, False, None))
        durations = [65] * total + [90, 140, 140, 140, 3500]
    elif win > 0:
        frames.append(result_frame(False, True, None))
        frames.append(result_frame(False, False, None))
        durations = [65] * total + [120, 3500]
    else:
        frames.append(result_frame(False, False, None))
        durations = [65] * total + [3500]
    return _gif(frames, durations)


# --- Roulette (animiert) ---------------------------------------------------
def _roul_ring(size: int, spin: int) -> Image.Image:
    """Zeichnet den Zahlenring EINMAL (Gewinnerfach oben) - die Animation
    rotiert dann nur noch dieses Bild (schnell, C-Ebene)."""
    ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(ring, "RGBA")
    c = size / 2
    Ro = size / 2 - 2
    Ri = Ro * 0.765
    n = len(_ROUL_ORDER)
    seg = 360.0 / n
    idx = _ROUL_ORDER.index(spin) if spin in _ROUL_ORDER else 0
    base = -90.0 - idx * seg - seg / 2
    for i, num in enumerate(_ROUL_ORDER):
        a0 = base + i * seg
        d.pieslice([c - Ro, c - Ro, c + Ro, c + Ro], a0, a0 + seg,
                   fill=_roul_color(num), outline=(18, 18, 22))
    Rm = (Ro + Ri) / 2 + 6
    for i, num in enumerate(_ROUL_ORDER):
        a = math.radians(base + i * seg + seg / 2)
        d.text((c + Rm * math.cos(a), c + Rm * math.sin(a)), str(num),
               font=_font(14), fill=(245, 245, 245), anchor="mm")
    # Innenkreis gehoert zum Ring (dreht optisch mit, ist aber uni -> unsichtbar)
    d.ellipse([c - Ri, c - Ri, c + Ri, c + Ri], fill=(12, 60, 40),
              outline=(245, 197, 24), width=4)
    return ring


def roulette_wheel_anim(spin: int, won: bool) -> io.BytesIO:
    """Roulette als GIF: der Kessel dreht sich aus, die KUGEL kreist gegenlaeufig
    aussen, spiralt nach innen und faellt oben ins Gewinnerfach; am Ende
    erscheint das Ergebnis-Hub (bei Gewinn mit Konfetti)."""
    W = H = 440
    ring_size = 392
    ring = _roul_ring(ring_size, spin)
    cx, cy = W / 2, H / 2 + 4
    rx0, ry0 = int(cx - ring_size / 2), int(cy - ring_size / 2)
    Ro = ring_size / 2 - 2
    Rm = (Ro + Ro * 0.765) / 2 + 6           # Zahlenkranz = Pocket-Radius

    def frame(t: float, final: bool, conf_t: float | None = None) -> Image.Image:
        img = _vgrad(W, H, _FELT_TOP, _FELT_BOT).convert("RGBA")
        d = ImageDraw.Draw(img, "RGBA")
        outline = ((46, 204, 113) if won else (231, 76, 60)) if final else (245, 197, 24)
        d.rounded_rectangle([6, 6, W - 6, H - 6], radius=20, outline=outline, width=4)
        angle = (1.0 - _ease_out(t)) * 900.0 if not final else 0.0
        r = ring.rotate(angle, resample=Image.BILINEAR) if angle else ring
        img.paste(r, (rx0, ry0), r)
        d = ImageDraw.Draw(img, "RGBA")
        # Zeiger oben
        d.polygon([(cx - 15, cy - ring_size / 2 - 26), (cx + 15, cy - ring_size / 2 - 26),
                   (cx, cy - ring_size / 2 + 8)], fill=(245, 245, 245), outline=(20, 20, 20))
        # Kugel: gegenlaeufig kreisen, nach innen spiralen, oben einrasten.
        if final:
            ba, br = math.radians(-90), Rm
        else:
            e = _ease_out(t)
            ba = math.radians(-90.0 - (1.0 - e) * 1440.0)
            br = Ro * 0.94 - (Ro * 0.94 - Rm) * e
            if t > 0.75:                      # kurz vorm Einrasten leicht huepfen
                br += math.sin(t * 60) * 4
        bx, by = cx + br * math.cos(ba), cy + br * math.sin(ba)
        d.ellipse([bx - 8, by - 6, bx + 10, by + 12], fill=(0, 0, 0, 70))
        d.ellipse([bx - 9, by - 9, bx + 9, by + 9], fill=(250, 250, 252),
                  outline=(60, 60, 60), width=2)
        d.ellipse([bx - 4, by - 5, bx, by - 1], fill=(255, 255, 255))
        if final:
            d.ellipse([cx - 72, cy - 72, cx + 72, cy + 72], fill=_roul_color(spin),
                      outline=(245, 197, 24), width=4)
            d.text((cx, cy - 8), str(spin), font=_font(60), fill=(250, 250, 252), anchor="mm")
            name = "GRÜN" if spin == 0 else ("ROT" if spin in _ROUL_RED else "SCHWARZ")
            d.text((cx, cy + 42), name, font=_font(22), fill=(250, 250, 252), anchor="mm")
        if conf_t is not None:
            _confetti(img, conf_t, seed=29)
        return img

    N = 20
    frames = [frame(i / N, final=False) for i in range(N)]
    if won:
        frames.append(_flash(frame(1.0, final=True), (46, 204, 113), 46))
        for ct in (0.3, 0.65):
            frames.append(frame(1.0, final=True, conf_t=ct))
        frames.append(frame(1.0, final=True))
        durations = [55] * N + [90, 140, 140, 4000]
    else:
        frames.append(frame(1.0, final=True))
        durations = [55] * N + [4000]
    return _gif(frames, durations)


# --- Crash (animiert) ------------------------------------------------------
def crash_chart_anim(crash_point: float, target: float, cashed: bool) -> io.BytesIO:
    """Crash als GIF: die Kurve waechst live mit laufendem Multiplikator-Badge,
    das Endbild ist der volle Chart (mit Glow, Ziel-Linie, Explosion/Cashout)."""
    W, H = 820, 420
    L, R, T, B = 72, 30, 70, 50
    x0, x1, y0, y1 = L, W - R, T, H - B
    cp = max(1.001, float(crash_point))
    ymax = max(max(cp, target) * 1.16, 1.6)

    def px(t: float) -> float:
        return x0 + t * (x1 - x0)

    def py(m: float) -> float:
        return y1 - (m - 1.0) / (ymax - 1.0) * (y1 - y0)

    base = _vgrad(W, H, _CRASH_TOP, _CRASH_BOT).convert("RGBA")
    bd = ImageDraw.Draw(base, "RGBA")
    bd.rounded_rectangle([6, 6, W - 6, H - 6], radius=20, outline=(255, 255, 255, 26), width=2)
    for m in _nice_ticks(ymax):
        yy = py(m)
        bd.line([(x0, yy), (x1, yy)], fill=(255, 255, 255, 18), width=1)
        bd.text((x0 - 12, yy), f"{m:g}×", font=_font(18), fill=(150, 160, 176), anchor="rm")
    bd.line([(x0, y0), (x0, y1)], fill=(80, 90, 106), width=2)
    bd.line([(x0, y1), (x1, y1)], fill=(80, 90, 106), width=2)
    bd.text((x0, 18), "CRASH", font=_font(28), fill=(236, 240, 246))

    N = 140
    full = [(i / N, cp ** (i / N)) for i in range(N + 1)]
    frames: list[Image.Image] = []
    durations: list[int] = []
    steps = 12
    for s in range(1, steps + 1):
        f = _ease_out(s / steps) if s < steps else 1.0
        cut = max(2, int(f * (N + 1)))
        pts = [(px(t), py(m)) for t, m in full[:cut]]
        img = base.copy()
        d = ImageDraw.Draw(img, "RGBA")
        d.line(pts, fill=(120, 200, 255), width=5, joint="curve")
        # Rakete an der Kurvenspitze: Nase in Flugrichtung, Flamme flackert.
        tip_x, tip_y = pts[-1]
        prev_x, prev_y = pts[-2] if len(pts) > 1 else (tip_x - 6, tip_y)
        ang = math.atan2(tip_y - prev_y, tip_x - prev_x)
        ca, sa = math.cos(ang), math.sin(ang)

        def rot(dx: float, dy: float) -> tuple[float, float]:
            return (tip_x + dx * ca - dy * sa, tip_y + dx * sa + dy * ca)

        flame = 22 + (8 if s % 2 else 0)
        d.polygon([rot(-10, -6), rot(-10 - flame, 0), rot(-10, 6)],
                  fill=(255, 168, 40))
        d.polygon([rot(-10, -3), rot(-10 - flame * 0.55, 0), rot(-10, 3)],
                  fill=(255, 235, 120))
        d.polygon([rot(20, 0), rot(-8, -10), rot(-8, 10)],
                  fill=(236, 240, 246), outline=(140, 150, 170))
        cur = full[cut - 1][1]
        badge = f"{cur:.2f}×"
        bf = _font(40)
        tw = d.textlength(badge, font=bf)
        d.rounded_rectangle([x1 - tw - 36, 14, x1, 64], radius=16, fill=(0, 0, 0, 130),
                            outline=(120, 200, 255), width=3)
        d.text(((x1 - tw - 36 + x1) / 2, 39), badge, font=bf, fill=(120, 200, 255),
               anchor="mm")
        frames.append(img)
        durations.append(75)

    # Endbild = der volle statische Chart (Glow, Ziel-Linie, Burst/Cashout).
    final = Image.open(crash_chart(crash_point, target, cashed)).convert("RGBA")
    if cashed:
        # Gewinn: gruener Blitz + Konfetti-Regen, dann das Endbild.
        frames.append(_flash(final, (46, 204, 113), 55))
        durations.append(100)
        for ct in (0.3, 0.65):
            conf = final.copy()
            _confetti(conf, ct, seed=21)
            frames.append(conf)
            durations.append(140)
    else:
        # Absturz: roter Blitz + Explosion mit wachsender Schockwelle.
        end_x, end_y = px(1.0), py(cp)
        frames.append(_flash(final, (231, 76, 60), 70))
        durations.append(90)
        for radius in (26, 44):
            boom = final.copy()
            bd = ImageDraw.Draw(boom, "RGBA")
            bd.ellipse([end_x - radius, end_y - radius, end_x + radius, end_y + radius],
                       outline=(255, 190, 90), width=4)
            bd.ellipse([end_x - radius * 0.55, end_y - radius * 0.55,
                        end_x + radius * 0.55, end_y + radius * 0.55],
                       outline=(255, 120, 60), width=3)
            _burst(bd, end_x, end_y, (231, 96, 60))
            frames.append(boom)
            durations.append(110)
    frames.append(final)
    durations.append(4500)
    return _gif(frames, durations)


# --- Keno (animiert) -------------------------------------------------------
def keno_grid_anim(picks: list, draw: list, hits: list, *,
                   big_win: bool = False) -> io.BytesIO:
    """Keno-Ziehung als GIF: die 10 Zahlen ploppen nacheinander auf (Treffer
    funkeln); bei einem grossen Gewinn regnet am Ende Konfetti."""
    hitset = set(hits)
    frames: list[Image.Image] = [_keno_img(picks, [], [])]
    durations: list[int] = [180]
    for i in range(1, len(draw) + 1):
        part = draw[:i]
        part_hits = [n for n in part if n in hitset]
        frames.append(_keno_img(picks, part, part_hits, pop=draw[i - 1]))
        durations.append(300)
    final = _keno_img(picks, draw, hits)
    if big_win:
        frames.append(_flash(final, (241, 196, 15), 55))
        durations.append(100)
        for ct in (0.3, 0.65):
            conf = final.copy().convert("RGBA")
            _confetti(conf, ct, seed=17)
            frames.append(conf)
            durations.append(140)
    frames.append(final)
    durations.append(4500)
    return _gif(frames, durations)


# --- Gluecksrad ------------------------------------------------------------
def _wheel_seg_color(mult: float) -> tuple:
    if mult <= 0:
        return (52, 56, 68)          # Niete - dunkelgrau
    if mult < 1.0:
        return (230, 126, 34)        # Teil vom Einsatz - orange
    if mult < 2.0:
        return (52, 152, 219)        # kleiner Gewinn - blau
    if mult < 5.0:
        return (46, 204, 113)        # guter Gewinn - gruen
    return (241, 196, 15)            # Jackpot - gold


def _wheel_ring(size: int, mults: list, idx: int) -> Image.Image:
    """Gluecksrad-Ring einmal zeichnen (Gewinnersegment oben), dann rotieren."""
    ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(ring, "RGBA")
    c = size / 2
    Ro = size / 2 - 2
    n = len(mults)
    seg = 360.0 / n
    base = -90.0 - idx * seg - seg / 2
    for i, m in enumerate(mults):
        a0 = base + i * seg
        d.pieslice([c - Ro, c - Ro, c + Ro, c + Ro], a0, a0 + seg,
                   fill=_wheel_seg_color(m), outline=(16, 18, 24))
    Rm = Ro * 0.72
    for i, m in enumerate(mults):
        a = math.radians(base + i * seg + seg / 2)
        label = "0" if m <= 0 else (f"×{m:g}")
        d.text((c + Rm * math.cos(a), c + Rm * math.sin(a)), label,
               font=_font(22), fill=(250, 250, 252), anchor="mm")
    d.ellipse([c - Ro * 0.30, c - Ro * 0.30, c + Ro * 0.30, c + Ro * 0.30],
              fill=(24, 27, 38), outline=(245, 197, 24), width=4)
    return ring


def wheel_fortune_anim(mults: list, idx: int) -> io.BytesIO:
    """Gluecksrad als GIF: dreht aus, Gewinnersegment landet oben am Zeiger,
    am Ende zeigt die Nabe den Multiplikator."""
    W = H = 440
    ring_size = 396
    ring = _wheel_ring(ring_size, mults, idx)
    cx, cy = W / 2, H / 2 + 6
    rx0, ry0 = int(cx - ring_size / 2), int(cy - ring_size / 2)
    won = mults[idx] > 0

    seg_deg = 360.0 / len(mults)

    def frame(angle: float, final: bool, *, highlight: bool = False,
              conf_t: float | None = None) -> Image.Image:
        img = _vgrad(W, H, (30, 24, 52), (13, 11, 24)).convert("RGBA")
        d = ImageDraw.Draw(img, "RGBA")
        outline = ((46, 204, 113) if won else (231, 76, 60)) if final else (245, 197, 24)
        d.rounded_rectangle([6, 6, W - 6, H - 6], radius=20, outline=outline, width=4)
        d.text((18, 14), "GLÜCKSRAD", font=_font(22), fill=(245, 197, 24))
        r = ring.rotate(angle, resample=Image.BILINEAR) if angle else ring
        img.paste(r, (rx0, ry0), r)
        d = ImageDraw.Draw(img, "RGBA")
        if final and highlight:
            # Gewinnersegment oben hell aufblitzen lassen.
            Ro = ring_size / 2 - 2
            d.pieslice([cx - Ro, cy - Ro, cx + Ro, cy + Ro],
                       -90 - seg_deg / 2, -90 + seg_deg / 2,
                       fill=(255, 255, 255, 76))
        d.polygon([(cx - 16, cy - ring_size / 2 - 18), (cx + 16, cy - ring_size / 2 - 18),
                   (cx, cy - ring_size / 2 + 16)], fill=(245, 245, 245), outline=(20, 20, 20))
        if final:
            m = mults[idx]
            label = "NIETE" if m <= 0 else f"×{m:g}"
            col = _wheel_seg_color(m)
            d.ellipse([cx - 64, cy - 64, cx + 64, cy + 64], fill=(24, 27, 38),
                      outline=col, width=5)
            d.text((cx, cy), label, font=_font(34 if len(label) <= 4 else 26),
                   fill=col, anchor="mm")
        if conf_t is not None:
            _confetti(img, conf_t, seed=11)
        return img

    N = 18
    frames = [frame((1.0 - _ease_out(i / N)) * 1080.0, final=False) for i in range(N)]
    if won:
        frames.append(frame(0.0, final=True, highlight=True))
        frames.append(frame(0.0, final=True, conf_t=0.3))
        frames.append(frame(0.0, final=True, highlight=True, conf_t=0.65))
        frames.append(frame(0.0, final=True))
        durations = [60] * N + [110, 140, 140, 4000]
    else:
        frames.append(frame(0.0, final=True))
        durations = [60] * N + [4000]
    return _gif(frames, durations)


# --- Rubbellos -------------------------------------------------------------
def _scratch_img(keys: list, revealed: int, win_rows: list, win: int,
                 show_result: bool, *, sparkle: bool = False) -> Image.Image:
    """Rubbellos 3x3: ``revealed`` Felder sind schon freigerubbelt, der Rest
    zeigt die Rubbel-Schicht. ``win_rows``: Indizes (0-2) der Gewinn-Reihen."""
    pad, tile, gap, top = 26, 128, 14, 66
    W = pad * 2 + 3 * tile + 2 * gap
    H = top + 3 * tile + 2 * gap + 64
    img = _vgrad(W, H, (36, 30, 18), (18, 14, 8)).convert("RGBA")
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle([8, 8, W - 8, H - 8], radius=20, outline=(245, 197, 24, 210), width=4)
    d.text((W / 2, 18), "FLO  RUBBELLOS", font=_font(30), fill=(245, 197, 24), anchor="ma")
    for i, key in enumerate(keys):
        r, c = divmod(i, 3)
        x = pad + c * (tile + gap)
        y = top + r * (tile + gap)
        if i < revealed:
            in_win = show_result and (r in win_rows)
            bgc = (255, 249, 224) if in_win else (248, 249, 252)
            d.rounded_rectangle([x, y, x + tile, y + tile], radius=14, fill=bgc,
                                outline=(245, 197, 24) if in_win else (60, 64, 80),
                                width=4 if in_win else 3)
            _slot_symbol(d, x + tile / 2, y + tile / 2, tile * 0.3, key)
        else:
            d.rounded_rectangle([x, y, x + tile, y + tile], radius=14, fill=(118, 122, 132),
                                outline=(80, 84, 94), width=3)
            d.text((x + tile / 2, y + tile / 2), "?", font=_font(52),
                   fill=(70, 74, 84), anchor="mm")
    if show_result:
        by = top + 3 * tile + 2 * gap + 12
        if win > 0:
            _pill_c(d, W / 2, by, f"GEWINN  +{win}", 26, (46, 204, 113), (8, 28, 16))
        else:
            _pill_c(d, W / 2, by, "leider kein Gewinn", 22, (70, 74, 92), (228, 230, 238))
    if sparkle and win_rows:
        # Funkeln entlang der Gewinn-Reihen.
        for r in win_rows:
            ry = top + r * (tile + gap) + tile / 2
            for sx in (pad + 8, pad + tile + gap / 2, W - pad - 8,
                       pad + 2 * tile + 1.5 * gap):
                _sparkle(d, sx, ry - tile * 0.42, 9, (255, 240, 170))
                _sparkle(d, sx + 14, ry + tile * 0.38, 7, (255, 250, 210))
    return img


def scratch_card_anim(keys: list, win_rows: list, win: int) -> io.BytesIO:
    """Rubbellos als GIF: die 9 Felder werden nacheinander freigerubbelt;
    Gewinn-Reihen funkeln, grosse Gewinne bekommen Konfetti."""
    frames = [_scratch_img(keys, i, win_rows, win, show_result=False)
              for i in range(0, 9)]
    durations = [190] * 9
    final = _scratch_img(keys, 9, win_rows, win, show_result=True)
    if win > 0:
        frames.append(_flash(final, (255, 240, 160), 55))
        durations.append(100)
        glitzer = _scratch_img(keys, 9, win_rows, win, show_result=True, sparkle=True)
        if win >= 500:
            for ct in (0.3, 0.65):
                conf = glitzer.copy()
                _confetti(conf, ct, seed=23)
                frames.append(conf)
                durations.append(140)
        else:
            frames.append(glitzer)
            durations.append(260)
        frames.append(_scratch_img(keys, 9, win_rows, win, show_result=True,
                                   sparkle=True))
        durations.append(4500)
    else:
        frames.append(final)
        durations.append(4500)
    return _gif(frames, durations)


# === Casino-Statistik-Karte ================================================
def casino_stats_card(name: str, avatar: "bytes | None", stats: dict) -> io.BytesIO:
    """Persoenliche Casino-Bilanz: Kopf mit Avatar+Name, vier Kennzahlen und
    ein Balken je Spiel (Anzahl Runden, Netto eingefaerbt).

    ``stats``: {games, wagered, payout, best_win, per: {spiel: {n, net}}}
    """
    per = dict(stats.get("per") or {})
    rows = sorted(per.items(), key=lambda kv: kv[1].get("n", 0), reverse=True)
    W = 900
    head, row_h = 176, 52
    H = head + max(1, len(rows)) * row_h + 40
    img = _vgrad(W, H, (26, 24, 40), (12, 11, 20)).convert("RGBA")
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle([6, 6, W - 6, H - 6], radius=18, outline=(48, 53, 63), width=2)

    # Avatar + Name
    AD = 84
    ax, ay = 34, 28
    circ = None
    if avatar:
        try:
            im = ImageOps.fit(Image.open(io.BytesIO(avatar)).convert("RGB"),
                              (AD, AD), method=_RESAMPLE)
            mask = Image.new("L", (AD, AD), 0)
            ImageDraw.Draw(mask).ellipse([0, 0, AD - 1, AD - 1], fill=255)
            circ = im.convert("RGBA")
            circ.putalpha(mask)
        except Exception:  # noqa: BLE001
            circ = None
    if circ is not None:
        img.paste(circ, (ax, ay), circ)
        d.ellipse([ax - 4, ay - 4, ax + AD + 3, ay + AD + 3],
                  outline=(241, 196, 15), width=4)
    safe = _clean_text(name)[:26] or "Spieler"
    d.text((ax + AD + 24, ay + 4), safe, font=_font(38), fill=_WHITE)
    d.text((ax + AD + 24, ay + 54), "CASINO-BILANZ", font=_font(17),
           fill=(150, 155, 168))

    # Kennzahlen-Zeile
    net = int(stats.get("payout", 0)) - int(stats.get("wagered", 0))
    net_col = _GREEN if net >= 0 else _RED_HOT
    kennz = [
        ("RUNDEN", f"{stats.get('games', 0):,}".replace(",", "."), _WHITE),
        ("EINSATZ", f"{stats.get('wagered', 0):,}".replace(",", "."), _WHITE),
        ("NETTO", f"{'+' if net >= 0 else ''}{net:,}".replace(",", "."), net_col),
        ("BESTER GEWINN", f"+{stats.get('best_win', 0):,}".replace(",", "."), _GOLD),
    ]
    seg = (W - 68) // len(kennz)
    for i, (label, val, col) in enumerate(kennz):
        sx = 34 + i * seg
        d.text((sx, 124), label, font=_font(15), fill=(120, 126, 140))
        d.text((sx, 144), val, font=_font(26), fill=col)

    # Balken je Spiel (Anzahl Runden relativ zum Maximum, Netto rechts)
    if rows:
        peak = max(v.get("n", 0) for _g, v in rows) or 1
        y = head + 8
        for game, v in rows:
            n, gnet = int(v.get("n", 0)), int(v.get("net", 0))
            col = _GREEN if gnet >= 0 else _RED_HOT
            d.text((34, y + 2), game.upper()[:14], font=_font(18), fill=(200, 205, 218))
            _hbar(d, 220, y + 4, W - 470, 18, n / peak, (87, 148, 242))
            d.text((W - 232, y + 2), f"{n}×", font=_font(18), fill=(150, 155, 168))
            d.text((W - 34, y + 2), f"{'+' if gnet >= 0 else ''}{gnet}",
                   font=_font(18), fill=col, anchor="ra")
            y += row_h
    else:
        d.text((34, head + 10), "Noch keine Runden gespielt.", font=_font(20),
               fill=(150, 155, 168))
    return _png(img)


# === Woerter-Top-Liste =====================================================
def words_card(rows: list, *, total_words: int = 0, total_count: int = 0) -> io.BytesIO:
    """Top-Woerter des Servers als Balken-Karte. ``rows``: [(wort, anzahl), ...]"""
    W = 900
    head, row_h = 118, 46
    n = max(1, len(rows))
    H = head + n * row_h + 42
    img = _vgrad(W, H, (22, 28, 40), (11, 14, 22)).convert("RGBA")
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle([6, 6, W - 6, H - 6], radius=18, outline=(48, 53, 63), width=2)
    hf = _font(42)
    d.text((34, 26), "FLO", font=hf, fill=_WHITE)
    d.text((34 + d.textlength("FLO ", font=hf), 26), "WÖRTER", font=hf, fill=_GOLD)
    if total_words:
        d.text((W - 34, 44), f"{total_words:,}".replace(",", ".") + " Wörter erfasst",
               font=_font(18), fill=(150, 158, 188), anchor="rm")
    d.line([(34, head - 16), (W - 34, head - 16)], fill=(54, 60, 86), width=2)

    if rows:
        peak = max(c for _w, c in rows) or 1
        y = head
        medal = {0: (250, 222, 42), 1: (176, 176, 184), 2: (205, 127, 50)}
        for i, (wort, count) in enumerate(rows):
            col = medal.get(i, (87, 148, 242))
            d.text((34, y + 6), f"{i + 1}.", font=_font(20),
                   fill=medal.get(i, (130, 136, 150)))
            word_txt = _clean_text(str(wort))[:18] or "?"
            d.text((84, y + 6), word_txt, font=_font(22), fill=_WHITE)
            _hbar(d, 320, y + 10, W - 560, 16, count / peak, col)
            d.text((W - 34, y + 6), f"{count:,}".replace(",", "."), font=_font(21),
                   fill=(200, 205, 218), anchor="ra")
            y += row_h
    else:
        d.text((34, head + 8), "Noch keine Wörter gezählt.", font=_font(20),
               fill=(150, 155, 168))
    return _png(img)
