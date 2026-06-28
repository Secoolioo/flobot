"""Grafana-artiges Leaderboard-Bild fuer Flo.

Rendert die Bestenliste (Level, Nachrichten, Voice-Zeit, Coins) als dunkles
Dashboard-PNG im Grafana-Stil mit Balken-Anzeigen ("Bar Gauges").

Braucht Pillow. Fehlt das Paket, gibt ``render_png()`` ``None`` zurueck und
``is_available()`` meldet ``False`` - der Bot faellt dann automatisch auf die
normale Embed-Bestenliste zurueck (kein Absturz).
"""
from __future__ import annotations

import io
import logging
import re
import unicodedata

log = logging.getLogger("dcbot.leaderboard")

try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_OK = True
except ImportError:  # pragma: no cover - nur ohne Pillow relevant
    _PIL_OK = False


def is_available() -> bool:
    """True, wenn Pillow installiert ist (sonst nutzt der Bot das Embed)."""
    return _PIL_OK


# --- Grafana-Dark-Farbpalette (RGB) --------------------------------------
_BG = (17, 18, 23)          # Seitenhintergrund  #111217
_PANEL = (24, 27, 31)       # Panel-Flaeche       #181b1f
_PANEL_ALT = (30, 34, 39)   # jede 2. Zeile etwas heller
_BORDER = (44, 50, 53)      # Rahmen/Trennlinien  #2c3235
_TRACK = (38, 42, 47)       # leerer Balken-Hintergrund
_FG = (204, 204, 220)       # Haupttext           #ccccdc
_FG_DIM = (123, 128, 135)   # Nebentext           #7b8087
_GREEN = (115, 191, 105)    # Voice-Balken        #73bf69
_BLUE = (87, 148, 242)      # Nachrichten-Balken  #5794f2
_ORANGE = (255, 152, 48)    # Akzent              #ff9830
_GOLD = (250, 222, 42)      # Platz 1             #fade2a
_SILVER = (176, 176, 184)   # Platz 2
_BRONZE = (205, 127, 50)    # Platz 3
_DARK = (17, 18, 23)        # Text auf hellen Medaillen
_BG_TOP = (25, 27, 34)      # Hintergrund-Verlauf oben (leicht heller)
_BG_BOT = (14, 15, 20)      # Hintergrund-Verlauf unten (dunkler) -> Tiefe
_ZEBRA = (27, 30, 36)       # jede 2. Zeile
_ROW1 = (42, 36, 20)        # Platz 1: zarter Gold-Schimmer
_ACCENT2 = (255, 200, 90)   # helleres Gold fuer Header-Akzent
_WHITE = (236, 238, 245)

# Schriftarten: erst DejaVu (Fedora/Ubuntu/macOS), sonst PIL-Standard.
_FONT_REG = [
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/DejaVuSans.ttf",
]
_FONT_BOLD = [
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/DejaVuSans-Bold.ttf",
]

_font_cache: dict = {}


def _font(size: int, bold: bool = False):
    """Laedt (und cached) eine Schrift der gewuenschten Groesse."""
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    paths = _FONT_BOLD if bold else _FONT_REG
    font = None
    for p in paths:
        try:
            font = ImageFont.truetype(p, size)
            break
        except OSError:
            continue
    if font is None:  # pragma: no cover - sehr seltenes Fallback
        font = ImageFont.load_default()
    _font_cache[key] = font
    return font


# --- Layout-Masse --------------------------------------------------------
_W = 1000
_PAD = 22
_HEADER_H = 104
_ROW_H = 78
_FOOTER_H = 46

# Spalten-Anker (x in Pixeln)
_X_RANK = 26
_X_AVATAR = 72        # Profilbild-Kreis
_AVA_D = 52           # Durchmesser des Profilbilds
_X_NAME = 142         # Name beginnt rechts vom Avatar
_NAME_W = 256
_X_MSG = 410          # Balken-Start Nachrichten
_BAR_W = 188          # Balken-Breite (beide Gauges gleich)
_X_VOICE = 712        # Balken-Start Voice


def _clean_title(title: str) -> str:
    """Entfernt fuehrende Emojis/Symbole vom Shop-Titel ('🗿 Sigma' -> 'Sigma')."""
    return re.sub(r"^\W+", "", title or "").strip()


def _fmt_num(n: int) -> str:
    """1234 -> '1.2k', 2500000 -> '2.5M' (kompakt fuer enge Spalten)."""
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return str(n)


def _fmt_voice(secs: int) -> str:
    """Sekunden -> '3h 20m' / '12m' / '45s'."""
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m"
    return f"{s}s"


def _truncate(draw, text: str, font, max_w: int) -> str:
    """Kuerzt Text mit '…', bis er in max_w Pixel passt."""
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return (text + "…") if text else "…"


# --- Namen darstellbar machen (Emoji/Fancy-Unicode/Zalgo abfangen) -------
_notdef_cache: dict = {}
_glyph_cache: dict = {}


def _chbytes(font, ch: str) -> bytes:
    """Rendert EIN Zeichen auf eine kleine Canvas und gibt die Pixel zurueck."""
    im = Image.new("L", (48, 48), 0)
    ImageDraw.Draw(im).text((6, 6), ch, font=font, fill=255)
    return im.tobytes()


def _glyph_ok(font, ch: str) -> bool:
    """True, wenn die Schrift fuer ch ein echtes Glyph hat (kein Tofu/leer)."""
    fid = id(font)
    ck = (fid, ch)
    if ck in _glyph_cache:
        return _glyph_cache[ck]
    notdef = _notdef_cache.get(fid)
    if notdef is None:
        try:
            notdef = _chbytes(font, "￿")   # Nicht-Zeichen -> garantiert Tofu
        except Exception:  # noqa: BLE001
            notdef = b""
        _notdef_cache[fid] = notdef
    try:
        b = _chbytes(font, ch)
        ok = bool(b) and b != notdef and any(b)
    except Exception:  # noqa: BLE001
        ok = False
    _glyph_cache[ck] = ok
    return ok


def _safe_name(font, name: str, fallback: str = "Spieler") -> str:
    """Macht einen Discord-Namen darstellbar:
    - NFKC-Normalisierung (fancy 𝓒𝓸𝓸𝓵 -> Cool, vollbreite ＡＢ -> AB),
    - kombinierende Zeichen (Zalgo), Steuer-/Format-/Emoji-Glyphen raus,
    - alles, wofuer die Schrift kein Glyph hat (CJK etc.), faellt weg.
    Bleibt nichts Lesbares uebrig, kommt der Fallback (z. B. der Rang)."""
    name = unicodedata.normalize("NFKC", name or "")
    out: list[str] = []
    for ch in name:
        if ch == " ":
            out.append(" ")
            continue
        cat = unicodedata.category(ch)
        if cat[0] == "M":                       # kombinierende Zeichen (Zalgo)
            continue
        if cat in ("Cc", "Cf", "Cs", "Co", "Cn"):   # Steuer/Format/unbelegt
            continue
        if ord(ch) >= 0x1F000:                  # Emoji- & Symbol-Zusatzebenen
            continue
        if _glyph_ok(font, ch):
            out.append(ch)
    res = " ".join("".join(out).split()).strip()
    return res or fallback


def _avatar_circle(data: bytes, diam: int) -> "Image.Image | None":
    """Macht aus den Avatar-Bytes ein rundes RGBA-Bild (diam x diam)."""
    try:
        im = Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:  # noqa: BLE001 - kaputter/leerer Download -> Platzhalter
        return None
    resample = getattr(Image, "Resampling", Image).LANCZOS
    im = im.resize((diam, diam), resample)
    mask = Image.new("L", (diam, diam), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, diam - 1, diam - 1], fill=255)
    out = Image.new("RGBA", (diam, diam), (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    return out


def _draw_avatar(img, d, row: dict, name: str, cy: int, ring) -> None:
    """Zeichnet das Profilbild (oder einen Initial-Platzhalter) mit farbigem Ring."""
    ax = _X_AVATAR
    ay = cy - _AVA_D // 2
    circ = None
    av = row.get("avatar")
    if av:
        circ = _avatar_circle(av, _AVA_D)
    if circ is not None:
        img.paste(circ, (ax, ay), circ)
    else:
        # Platzhalter: gefuellter Kreis + erster Buchstabe des Namens
        d.ellipse([ax, ay, ax + _AVA_D, ay + _AVA_D], fill=_PANEL_ALT)
        initial = (name[:1] or "?").upper()
        fi = _font(26, bold=True)
        iw = d.textlength(initial, font=fi)
        d.text((ax + _AVA_D / 2 - iw / 2, ay + _AVA_D / 2 - 16), initial,
               font=fi, fill=_FG_DIM)
    # Ring drumherum (Rang-Farbe fuer Top 3, sonst dezent)
    d.ellipse([ax - 2, ay - 2, ax + _AVA_D + 1, ay + _AVA_D + 1],
              outline=ring, width=3)


def _vgrad(w: int, h: int, top: tuple, bot: tuple) -> "Image.Image":
    """Senkrechter Farbverlauf (oben 'top' -> unten 'bot') als Hintergrund."""
    col = Image.new("RGB", (1, h))
    px = col.load()
    span = max(1, h - 1)
    for y in range(h):
        f = y / span
        px[0, y] = tuple(int(top[c] + (bot[c] - top[c]) * f) for c in range(3))
    return col.resize((w, h))


def _trophy(d, x: int, y: int, s: float, color) -> None:
    """Zeichnet einen kleinen Pokal (fuer den Header)."""
    cup_w = s
    # Henkel
    d.arc([x - s * 0.22, y, x + s * 0.30, y + s * 0.55], 70, 290, fill=color, width=3)
    d.arc([x + cup_w * 0.70, y, x + cup_w * 1.22, y + s * 0.55], 250, 110, fill=color, width=3)
    # Becher (Trapez)
    d.polygon([(x, y - s * 0.04), (x + cup_w, y - s * 0.04),
               (x + cup_w * 0.74, y + s * 0.5), (x + cup_w * 0.26, y + s * 0.5)], fill=color)
    # Stiel + Fuss
    d.rectangle([x + cup_w * 0.44, y + s * 0.5, x + cup_w * 0.56, y + s * 0.72], fill=color)
    d.rectangle([x + cup_w * 0.30, y + s * 0.72, x + cup_w * 0.70, y + s * 0.86], fill=color)


def _gauge(draw, x: int, y: int, w: int, h: int, frac: float, color) -> None:
    """Zeichnet einen Grafana-Balken (Track + gefuellter Teil)."""
    frac = max(0.0, min(1.0, frac))
    r = h // 2
    draw.rounded_rectangle([x, y, x + w, y + h], radius=r, fill=_TRACK)
    fw = int(round(w * frac))
    if frac > 0 and fw < h:        # Mindestbreite, damit der Rundbalken sichtbar ist
        fw = h
    if fw > 0:
        draw.rounded_rectangle([x, y, x + fw, y + h], radius=r, fill=color)


def _rank_color(i: int):
    return {0: _GOLD, 1: _SILVER, 2: _BRONZE}.get(i, None)


def render_png(rows: list[dict], *, title: str = "FLO  LEADERBOARD",
               subtitle: str = "") -> "bytes | None":
    """Rendert die Bestenliste als PNG (bytes). Ohne Pillow: None.

    ``rows``: Liste von Dicts mit name, level, xp, coins, voice_secs, msgs, title
    (z. B. aus ``economy.leaderboard_data()``), bereits nach XP sortiert.
    """
    if not _PIL_OK or not rows:
        return None
    try:
        return _render(rows, title, subtitle)
    except Exception:  # noqa: BLE001 - im Zweifel lieber kein Bild als ein Crash
        log.exception("Leaderboard-Render fehlgeschlagen")
        return None


def _render(rows: list[dict], title: str, subtitle: str) -> bytes:
    n = len(rows)
    height = _HEADER_H + _ROW_H * n + _FOOTER_H
    img = _vgrad(_W, height, _BG_TOP, _BG_BOT)   # Hintergrund mit Verlauf -> Tiefe
    d = ImageDraw.Draw(img)

    # Aeusserer Panel-Rahmen
    d.rounded_rectangle([6, 6, _W - 7, height - 7], radius=14,
                        outline=_BORDER, width=2)

    # Normierung der Balken auf den jeweils groessten Wert in der Spalte.
    max_msg = max((r.get("msgs", 0) for r in rows), default=0) or 1
    max_voice = max((r.get("voice_secs", 0) for r in rows), default=0) or 1

    _draw_header(d, title, subtitle)

    f_name = _font(23, bold=True)
    f_meta = _font(15)
    f_rank = _font(22, bold=True)
    f_val = _font(16, bold=True)

    for i, r in enumerate(rows):
        top = _HEADER_H + i * _ROW_H
        cy = top + _ROW_H // 2

        # Zeilen-Hintergrund: Platz 1 zart golden, sonst weiche Zebra-Streifen.
        if i == 0:
            d.rounded_rectangle([12, top + 3, _W - 13, top + _ROW_H - 3],
                                radius=10, fill=_ROW1)
        elif i % 2 == 1:
            d.rounded_rectangle([12, top + 3, _W - 13, top + _ROW_H - 3],
                                radius=10, fill=_ZEBRA)

        # --- Rang (Medaille fuer Top 3 mit Glow, sonst dezente Ziffer) ---
        rc = _rank_color(i)
        if rc is not None:
            cr = 15
            glow = tuple(min(255, c + 40) for c in rc)
            d.ellipse([_X_RANK - 2, cy - cr - 2, _X_RANK + 2 * cr + 2, cy + cr + 2],
                      outline=glow, width=2)
            d.ellipse([_X_RANK, cy - cr, _X_RANK + 2 * cr, cy + cr], fill=rc)
            num = str(i + 1)
            tw = d.textlength(num, font=f_rank)
            d.text((_X_RANK + cr - tw / 2, cy - 13), num, font=f_rank, fill=_DARK)
        else:
            txt = f"{i + 1}"
            tw = d.textlength(txt, font=f_rank)
            d.text((_X_RANK + 15 - tw / 2, cy - 12), txt, font=f_rank, fill=_FG_DIM)

        # --- Name erst darstellbar machen (Emoji/Fancy-Unicode/Zalgo abfangen) ---
        safe = _safe_name(f_name, r.get("name", ""), fallback=f"Spieler #{i + 1}")

        # --- Profilbild (oder Initial-Platzhalter) mit Rang-Ring ---
        _draw_avatar(img, d, r, safe, cy, rc or _BORDER)

        # --- Name + Meta-Zeile (Level · Coins · Titel) ---
        name = _truncate(d, safe, f_name, _NAME_W)
        d.text((_X_NAME, cy - 22), name, font=f_name, fill=_FG)
        meta = f"Lvl {r.get('level', 0)}  ·  {_fmt_num(r.get('coins', 0))} Coins"
        clean = _clean_title(r.get("title", ""))
        if clean:
            meta += f"  ·  {clean}"
        meta = _truncate(d, meta, f_meta, _NAME_W + 8)
        d.text((_X_NAME, cy + 4), meta, font=f_meta, fill=_FG_DIM)

        # --- Gauge: Nachrichten (Spaltenkopf labelt sie oben) ---
        _gauge(d, _X_MSG, cy - 8, _BAR_W, 16,
               r.get("msgs", 0) / max_msg, _BLUE)
        d.text((_X_MSG + _BAR_W + 12, cy - 11),
               _fmt_num(r.get("msgs", 0)), font=f_val, fill=_FG)

        # --- Gauge: Voice-Zeit ---
        _gauge(d, _X_VOICE, cy - 8, _BAR_W, 16,
               r.get("voice_secs", 0) / max_voice, _GREEN)
        d.text((_X_VOICE + _BAR_W + 12, cy - 11),
               _fmt_voice(r.get("voice_secs", 0)), font=f_val, fill=_FG)

    _draw_footer(d, n, height)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _draw_header(d, title: str, subtitle: str) -> None:
    # Goldener Pokal + Titel
    _trophy(d, 24, 30, 30, _GOLD)
    d.text((74, 28), title, font=_font(30, bold=True), fill=_WHITE)
    # Gold-Akzentlinie direkt unter dem Titel
    tw_title = d.textlength(title, font=_font(30, bold=True))
    d.rectangle([74, 66, 74 + tw_title, 69], fill=_ACCENT2)
    if subtitle:
        f = _font(15)
        tw = d.textlength(subtitle, font=f)
        d.text((_W - 30 - tw, 38), subtitle, font=f, fill=_FG_DIM)

    # Spaltenkoepfe
    fh = _font(13, bold=True)
    d.text((_X_RANK, 80), "RANG", font=fh, fill=_FG_DIM)
    d.text((_X_NAME, 80), "SPIELER", font=fh, fill=_FG_DIM)
    d.text((_X_MSG, 80), "NACHRICHTEN", font=fh, fill=_FG_DIM)
    d.text((_X_VOICE, 80), "VOICE-ZEIT", font=fh, fill=_FG_DIM)
    d.line([18, _HEADER_H - 2, _W - 18, _HEADER_H - 2], fill=_BORDER, width=2)


def _draw_footer(d, n: int, height: int) -> None:
    f = _font(13)
    txt = f"Flo  ·  sortiert nach XP  ·  {n} Spieler"
    tw = d.textlength(txt, font=f)
    d.text(((_W - tw) / 2, height - _FOOTER_H + 14), txt, font=f, fill=_FG_DIM)
