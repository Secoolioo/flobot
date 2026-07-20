"""Grafische Darstellungen fuer Flo (mit Pillow, ohne Netz/Downloads).

Erzeugt fertige PNGs als ``io.BytesIO``, die direkt als ``discord.File``
verschickt werden koennen:

- ``blackjack_table(dealer, player, ...)`` -> Casino-Tisch mit echten Karten.
- ``crash_chart(crash_point, target, cashed)`` -> Multiplikator-Kurve.

Alles wird selbst gezeichnet (keine externen Bild-Dateien), damit es auch auf
einem frisch aufgesetzten Server ohne zusaetzliche Assets funktioniert.
"""

import io
import math
import random
import unicodedata

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageFilter, ImageOps


class Render:
    """Buendelt alle Zeichen-Helfer und Render-Funktionen als Methoden.

    Veraenderlicher Zustand (Font-Datei, Font-Cache, Glyphen-Cache) lebt als
    Instanz-Attribut in ``__init__``; die Modul-Aliase am Dateiende halten
    die bisherige Funktions-Schnittstelle unveraendert.
    """

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

    def __init__(self):
        self._FONT_FILE = None
        for _p in self._FONT_PATHS:
            try:
                ImageFont.truetype(_p, 12)
                self._FONT_FILE = _p
                break
            except OSError:
                continue
        self._font_cache = {}
        self._notdef_q = {}


    def _font(self, size):
        f = self._font_cache.get(size)
        if f is None:
            if self._FONT_FILE:
                f = ImageFont.truetype(self._FONT_FILE, size)
            else:  # Notnagel - sollte praktisch nie passieren
                f = ImageFont.load_default()
            self._font_cache[size] = f
        return f


    def _png(self, img):
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
    def _vgrad(self, w, h, top, bot):
        """Vertikaler Farbverlauf (ohne numpy). Schnell: 1px-Spalte zeichnen und
        auf volle Breite skalieren statt h einzelne Linien."""
        col = Image.new("RGB", (1, h))
        px = col.load()
        span = max(1, h - 1)
        for y in range(h):
            f = y / span
            px[0, y] = tuple(round(top[i] + (bot[i] - top[i]) * f) for i in range(3))
        return col.resize((w, h))


    def _pill(self, d, x, y, text, size,
              bg, fg):
        """Zeichnet eine abgerundete 'Pille' mit Text. Gibt die Breite zurueck."""
        f = self._font(size)
        tw = d.textlength(text, font=f)
        h = size + 12
        w = int(tw + 24)
        d.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=bg)
        d.text((x + w / 2, y + h / 2), text, font=f, fill=fg, anchor="mm")
        return w


    def _pill_c(self, d, cx, y, text, size,
                bg, fg):
        """Wie _pill, aber horizontal um cx zentriert."""
        f = self._font(size)
        w = int(d.textlength(text, font=f) + 24)
        return self._pill(d, int(cx - w / 2), y, text, size, bg, fg)


    # --- Spielkarten ---------------------------------------------------------
    CARD_W, CARD_H, CARD_R = 124, 174, 14


    def _corner(self, rank, suit, color):
        tile = Image.new("RGBA", (48, 66), (0, 0, 0, 0))
        d = ImageDraw.Draw(tile)
        d.text((24, 0), rank, font=self._font(30), fill=color, anchor="ma")
        d.text((24, 34), suit, font=self._font(28), fill=color, anchor="ma")
        return tile


    def _draw_card(self, img, x, y, rank, suit):
        d = ImageDraw.Draw(img, "RGBA")
        d.rounded_rectangle([x + 4, y + 6, x + self.CARD_W + 4, y + self.CARD_H + 6],
                            radius=self.CARD_R, fill=(0, 0, 0, 70))            # Schatten
        d.rounded_rectangle([x, y, x + self.CARD_W, y + self.CARD_H], radius=self.CARD_R,
                            fill=(250, 250, 252))
        d.rounded_rectangle([x, y, x + self.CARD_W, y + self.CARD_H], radius=self.CARD_R,
                            outline=(208, 212, 222), width=2)
        color = self._RED if suit in "♥♦" else self._BLACK
        tile = self._corner(rank, suit, color)
        img.paste(tile, (x + 10, y + 8), tile)
        flip = tile.rotate(180)
        img.paste(flip, (x + self.CARD_W - 10 - tile.width, y + self.CARD_H - 8 - tile.height), flip)
        d.text((x + self.CARD_W / 2, y + self.CARD_H / 2 + 4), suit, font=self._font(82),
               fill=color, anchor="mm")


    def _draw_back(self, img, x, y):
        d = ImageDraw.Draw(img, "RGBA")
        d.rounded_rectangle([x + 4, y + 6, x + self.CARD_W + 4, y + self.CARD_H + 6],
                            radius=self.CARD_R, fill=(0, 0, 0, 70))
        d.rounded_rectangle([x, y, x + self.CARD_W, y + self.CARD_H], radius=self.CARD_R,
                            fill=(38, 54, 120))
        d.rounded_rectangle([x + 9, y + 9, x + self.CARD_W - 9, y + self.CARD_H - 9],
                            radius=self.CARD_R - 4, outline=(126, 146, 224), width=3)
        cx, cy = x + self.CARD_W / 2, y + self.CARD_H / 2
        for dx in range(-1, 2):           # ein paar Rauten als Muster
            for dy in range(-2, 3):
                ox, oy = cx + dx * 30, cy + dy * 30
                r = 9
                d.polygon([(ox, oy - r), (ox + r * 0.7, oy), (ox, oy + r),
                           (ox - r * 0.7, oy)], fill=(94, 116, 196))
        r = 20
        d.polygon([(cx, cy - r), (cx + r * 0.72, cy), (cx, cy + r), (cx - r * 0.72, cy)],
                  fill=(168, 186, 240))


    def _row_start(self, w, n, gap):
        return (w - (n * self.CARD_W + (n - 1) * gap)) // 2


    def _card_tile(self, rank = None, suit = ""):
        """Eine einzelne Karte (oder Rueckseite bei rank=None) als eigenes Bild -
        so laesst sie sich fuer Flip-/Slide-Animationen skalieren und verschieben."""
        tile = Image.new("RGBA", (self.CARD_W + 6, self.CARD_H + 8), (0, 0, 0, 0))
        if rank is None:
            self._draw_back(tile, 0, 0)
        else:
            self._draw_card(tile, 0, 0, rank, suit)
        return tile


    def _bj_img(self, dealer, player, *, hide_hole,
                dealer_value, player_value, player_state = "",
                n_dealer = None, n_player = None,
                slide = None,
                hole_flip = None,
                hide_values = False,
                labels = ("DEALER", "DU")):
        """Blackjack-Tisch als Image. Animations-Parameter:
        ``n_dealer``/``n_player``: nur die ersten n Karten zeigen (Deal-Animation),
        ``slide``: ('player'|'dealer', dx) - letzte gezeigte Karte um dx versetzt,
        ``hole_flip``: 0..1 - die verdeckte Dealer-Karte dreht sich um,
        ``hide_values``: Wert-Pillen zeigen '-' (waehrend noch ausgeteilt wird)."""
        gap, pad, label_h, row_gap = 18, 30, 44, 24
        max_cards = max(len(dealer), len(player), 2)
        inner = max_cards * self.CARD_W + (max_cards - 1) * gap
        W = max(620, pad * 2 + inner)
        H = pad * 2 + 2 * (label_h + self.CARD_H) + row_gap
        nd = len(dealer) if n_dealer is None else min(n_dealer, len(dealer))
        np_ = len(player) if n_player is None else min(n_player, len(player))

        img = self._vgrad(W, H, self._FELT_TOP, self._FELT_BOT).convert("RGBA")
        d = ImageDraw.Draw(img, "RGBA")
        d.rounded_rectangle([6, 6, W - 6, H - 6], radius=18, outline=(255, 255, 255, 40), width=2)

        def put(tile, x, y, squeeze = 1.0):
            if squeeze < 1.0:
                w = max(4, int(tile.width * squeeze))
                tile = tile.resize((w, tile.height))
                x += (self.CARD_W + 6 - w) // 2
            img.paste(tile, (x, y), tile)

        # Dealer-Reihe
        d.text((pad, pad - 2), labels[0], font=self._font(26), fill=self._WHITE)
        dv = "–" if hide_values else ("?" if hide_hole else str(dealer_value))
        dv_bg = (231, 76, 60) if (not hide_hole and not hide_values
                                  and dealer_value > 21) else (0, 0, 0, 110)
        self._pill(d, pad + 26 + int(d.textlength(labels[0], font=self._font(26))), pad - 6, dv, 22, dv_bg, self._WHITE)
        ry = pad + label_h
        rx = self._row_start(W, len(dealer), gap)
        for i, (r, s) in enumerate(dealer[:nd]):
            dx = slide[1] if (slide and slide[0] == "dealer" and i == nd - 1) else 0
            if i == 1 and hole_flip is not None:
                # Hole-Card dreht sich: erst Ruecken schmaler, dann Vorderseite breiter.
                if hole_flip < 0.5:
                    put(self._card_tile(), rx + dx, ry, squeeze=1.0 - 2.0 * hole_flip)
                else:
                    put(self._card_tile(r, s), rx + dx, ry, squeeze=2.0 * hole_flip - 1.0)
            elif hide_hole and i == 1:
                put(self._card_tile(), rx + dx, ry)
            else:
                put(self._card_tile(r, s), rx + dx, ry)
            rx += self.CARD_W + gap

        # Spieler-Reihe
        py = pad + label_h + self.CARD_H + row_gap
        d = ImageDraw.Draw(img, "RGBA")
        d.text((pad, py - 2), labels[1], font=self._font(26), fill=self._WHITE)
        pstate_col = {
            "bust": (231, 76, 60), "lose": (231, 76, 60),
            "blackjack": (241, 196, 15), "win": (46, 204, 113),
            "push": (120, 130, 145),
        }.get(player_state, (0, 0, 0, 110))
        self._pill(d, pad + 26 + int(d.textlength(labels[1], font=self._font(26))), py - 6,
              "–" if hide_values else str(player_value), 22, pstate_col, self._WHITE)
        ry2 = py + label_h
        rx = self._row_start(W, len(player), gap)
        for i, (r, s) in enumerate(player[:np_]):
            dx = slide[1] if (slide and slide[0] == "player" and i == np_ - 1) else 0
            put(self._card_tile(r, s), rx + dx, ry2)
            rx += self.CARD_W + gap

        return img


    def blackjack_table(self, dealer, player, *, hide_hole,
                        dealer_value, player_value,
                        player_state = "",
                        labels = ("DEALER", "DU")):
        """Rendert den Blackjack-Tisch als ein Bild (statisch - Fallback und
        Grundlage der Frames von blackjack_table_anim)."""
        return self._png(self._bj_img(dealer, player, hide_hole=hide_hole,
                            dealer_value=dealer_value, player_value=player_value,
                            player_state=player_state, labels=labels))


    def blackjack_table_anim(self, dealer, player, *, hide_hole,
                             dealer_value, player_value,
                             player_state = "", mode = "hit",
                             labels = ("DEALER", "DU")):
        """Blackjack als GIF. ``mode``:
        - 'deal'   : die Startkarten werden einzeln ausgeteilt
        - 'hit'    : die zuletzt gezogene Spieler-Karte slidet ein
        - 'reveal' : die Hole-Card flippt um, Dealer-Karten erscheinen nacheinander
        Gewinn/Blackjack endet mit Blitz + Konfetti, Bust mit rotem Blitz."""
        kw = dict(hide_hole=hide_hole, dealer_value=dealer_value,
                  player_value=player_value, labels=labels)
        frames = []
        durations = []

        if mode == "deal":
            # Reihenfolge wie am Tisch: Du, Dealer, Du, Dealer(verdeckt).
            schritte = [("player", 1, 1), ("dealer", 1, 1),
                        ("player", 1, 2), ("dealer", 2, 2)]
            for hand, ndl, npl in schritte:
                for dx in (46, 0):
                    frames.append(self._bj_img(dealer, player, **kw, n_dealer=ndl,
                                          n_player=npl, slide=(hand, dx),
                                          hide_values=True))
                    durations.append(70)
        elif mode == "reveal":
            for flip in (0.12, 0.5, 0.88):
                frames.append(self._bj_img(dealer, player, **{**kw, "hide_hole": False},
                                      n_dealer=2, hole_flip=flip))
                durations.append(90)
            for k in range(3, len(dealer) + 1):
                for dx in (46, 0):
                    frames.append(self._bj_img(dealer, player, **{**kw, "hide_hole": False},
                                          n_dealer=k, slide=("dealer", dx)))
                    durations.append(80)
        else:  # 'hit'
            for dx in (64, 22):
                frames.append(self._bj_img(dealer, player, **kw, slide=("player", dx)))
                durations.append(65)

        final = self._bj_img(dealer, player, **kw, player_state=player_state)
        if player_state in ("win", "blackjack"):
            farbe = (241, 196, 15) if player_state == "blackjack" else (46, 204, 113)
            frames.append(self._flash(final, farbe, 55))
            durations.append(100)
            for ct in (0.3, 0.65):
                conf = final.copy()
                self._confetti(conf, ct, seed=31)
                frames.append(conf)
                durations.append(140)
        elif player_state == "bust":
            frames.append(self._flash(final, (231, 76, 60), 60))
            durations.append(110)
        frames.append(final)
        durations.append(3600)
        return self._gif(frames, durations)


    # --- Crash-Kurve ---------------------------------------------------------
    def _nice_ticks(self, hi):
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


    def _dashed_h(self, d, y, x0, x1,
                  color, dash = 12, gap = 9):
        x = x0
        while x < x1:
            d.line([(x, y), (min(x + dash, x1), y)], fill=color, width=2)
            x += dash + gap


    def _burst(self, d, cx, cy, color,
               outline = (255, 255, 255)):
        pts = []
        for i in range(16):
            ang = math.pi * i / 8
            r = 17 if i % 2 == 0 else 7
            pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
        d.polygon(pts, fill=color, outline=outline)


    _CRASH_TOP = (22, 28, 44)
    _CRASH_BOT = (9, 12, 20)


    def _glow_line(self, base, pts, color, width,
                   blur):
        """Legt eine weiche Leucht-Linie unter die eigentliche Kurve."""
        if len(pts) < 2:
            return base
        glow = Image.new("RGBA", base.size, (0, 0, 0, 0))
        ImageDraw.Draw(glow).line(pts, fill=color, width=width, joint="curve")
        glow = glow.filter(ImageFilter.GaussianBlur(blur))
        return Image.alpha_composite(base, glow)


    def crash_chart(self, crash_point, target, cashed):
        """Zeichnet die Crash-Kurve: Multiplikator ueber die Zeit, Ziel-Linie,
        Cashout-Punkt bzw. Explosion - mit Verlauf, Leuchtkurve und Multiplikator-Badge."""
        W, H = 820, 420
        L, R, T, B = 72, 30, 70, 50
        x0, x1, y0, y1 = L, W - R, T, H - B
        cp = max(1.001, float(crash_point))
        ymax = max(cp, target) * 1.16
        ymax = max(ymax, 1.6)

        def px(t):
            return x0 + t * (x1 - x0)

        def py(m):
            f = (m - 1.0) / (ymax - 1.0)
            return y1 - f * (y1 - y0)

        img = self._vgrad(W, H, self._CRASH_TOP, self._CRASH_BOT).convert("RGBA")
        d = ImageDraw.Draw(img, "RGBA")
        d.rounded_rectangle([6, 6, W - 6, H - 6], radius=20, outline=(255, 255, 255, 26), width=2)
        # Gitter + y-Beschriftung
        for m in self._nice_ticks(ymax):
            yy = py(m)
            d.line([(x0, yy), (x1, yy)], fill=(255, 255, 255, 18), width=1)
            d.text((x0 - 12, yy), f"{m:g}×", font=self._font(18),
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
            img = self._glow_line(img, win_pts, (46, 204, 113, 140), 16, 7)
            d = ImageDraw.Draw(img, "RGBA")
            if len(rest) >= 2:
                d.line(rest, fill=(120, 92, 92), width=3, joint="curve")
            d.line(win_pts, fill=self._GREEN, width=6, joint="curve")
            self._dashed_h(d, py(target), x0, x1, (46, 204, 113, 170))
            cx, cy = px(tc), py(target)
            d.ellipse([cx - 11, cy - 11, cx + 11, cy + 11], fill=self._GREEN, outline=self._WHITE, width=3)
            self._burst(d, px(1.0), py(cp), (150, 96, 96), outline=(200, 200, 200))
            badge_col = self._GREEN
        else:
            all_pts = [(px(t), py(m)) for t, m in full]
            img = self._glow_line(img, all_pts, (231, 76, 60, 130), 16, 7)
            d = ImageDraw.Draw(img, "RGBA")
            d.line(all_pts, fill=self._RED_HOT, width=6, joint="curve")
            self._dashed_h(d, py(target), x0, x1, (241, 196, 15, 150))
            d.text((x1 - 4, py(target) - 22), f"Ziel {target:.2f}×", font=self._font(18),
                   fill=self._GOLD, anchor="ra")
            self._burst(d, px(1.0), py(cp), self._RED_HOT)
            badge_col = self._RED_HOT

        # Kopfzeile + grosses Multiplikator-Badge oben rechts
        d.text((x0, 18), "CRASH", font=self._font(28), fill=(236, 240, 246))
        badge = f"{cp:.2f}×"
        bf = self._font(40)
        tw = d.textlength(badge, font=bf)
        bx0, by0, bx1, by1 = x1 - tw - 36, 14, x1, 64
        d.rounded_rectangle([bx0, by0, bx1, by1], radius=16, fill=(0, 0, 0, 130),
                            outline=badge_col, width=3)
        d.text(((bx0 + bx1) / 2, (by0 + by1) / 2), badge, font=bf, fill=badge_col, anchor="mm")
        return self._png(img)


    # --- Slot-Machine --------------------------------------------------------
    # Symbol-Schluessel in fallender Wertigkeit. games.py waehlt aus SLOT_KEYS und
    # legt die Auszahlung fest; hier wird nur gezeichnet.
    SLOT_KEYS = ["seven", "diamond", "star", "bar", "grape", "lemon", "cherry"]


    def _ball(self, d, cx, cy, r,
              col, dark, light):
        """Kugel mit Fake-3D: dunkler Rand, Koerper, Schattierung, Glanzpunkt."""
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=dark)
        d.ellipse([cx - r * 0.93, cy - r * 0.93, cx + r * 0.93, cy + r * 0.93], fill=col)
        d.ellipse([cx - r * 0.55, cy - r * 0.05, cx + r * 0.8, cy + r * 0.85], fill=dark)
        d.ellipse([cx - r * 0.75, cy - r * 0.72, cx + r * 0.45, cy + r * 0.4], fill=col)
        d.ellipse([cx - r * 0.55, cy - r * 0.62, cx - r * 0.05, cy - r * 0.12], fill=light)


    def _slot_symbol(self, d, cx, cy, R, key):
        """Slot-Symbole mit Schattierung, Kontur und Glanz (kein Flat-Look)."""
        if key == "seven":
            f = self._font(int(R * 1.95))
            d.text((cx + R * 0.09, cy + R * 0.09), "7", font=f, fill=(120, 14, 20),
                   anchor="mm")                                   # Schlagschatten
            d.text((cx, cy), "7", font=f, fill=(232, 48, 56), anchor="mm")
            d.text((cx - R * 0.06, cy - R * 0.08), "7", font=self._font(int(R * 1.82)),
                   fill=(255, 110, 110), anchor="mm")             # Licht-Kante
        elif key == "bar":
            w, h = R * 1.5, R * 0.62
            d.rounded_rectangle([cx - w + 4, cy - h + 5, cx + w + 4, cy + h + 5],
                                radius=12, fill=(60, 26, 80))     # Schatten
            d.rounded_rectangle([cx - w, cy - h, cx + w, cy + h], radius=12,
                                fill=(142, 68, 173), outline=(245, 197, 24), width=3)
            d.rounded_rectangle([cx - w + 6, cy - h + 5, cx + w - 6, cy - h * 0.15],
                                radius=8, fill=(168, 96, 198))    # oberer Glanz
            f = self._font(int(R * 0.8))
            d.text((cx + 2, cy + 2), "BAR", font=f, fill=(70, 30, 95), anchor="mm")
            d.text((cx, cy), "BAR", font=f, fill=(255, 250, 235), anchor="mm")
        elif key == "star":
            def stern(rr_out, rr_in, off = 0.0):
                pts = []
                for i in range(10):
                    ang = -math.pi / 2 + i * math.pi / 5
                    rr = rr_out if i % 2 == 0 else rr_in
                    pts.append((cx + rr * math.cos(ang) + off,
                                cy + rr * math.sin(ang) + off))
                return pts
            d.polygon(stern(R, R * 0.42, off=R * 0.07), fill=(150, 110, 12))  # Schatten
            d.polygon(stern(R, R * 0.42), fill=(250, 204, 32), outline=(170, 125, 8))
            d.polygon(stern(R * 0.62, R * 0.26), fill=(255, 232, 120))
            self._sparkle(d, cx - R * 0.5, cy - R * 0.55, R * 0.2, (255, 255, 255))
        elif key == "diamond":
            t = (cx, cy - R); b = (cx, cy + R * 1.05)
            l = (cx - R * 0.85, cy - R * 0.18); r = (cx + R * 0.85, cy - R * 0.18)
            d.polygon([(t[0] + 3, t[1] + 4), (r[0] + 3, r[1] + 4),
                       (b[0] + 3, b[1] + 4), (l[0] + 3, l[1] + 4)],
                      fill=(20, 80, 112))                          # Schatten
            d.polygon([t, r, b, l], fill=(64, 186, 235), outline=(24, 100, 140))
            # Facetten: obere Kante hell, linke Flanke mittel
            lt = (cx - R * 0.42, cy - R * 0.62)
            rt = (cx + R * 0.42, cy - R * 0.62)
            d.polygon([t, rt, (cx, cy - R * 0.18), lt], fill=(168, 228, 250))
            d.polygon([l, lt, (cx, cy - R * 0.18), (cx, cy + R * 0.4)], fill=(110, 205, 242))
            d.polygon([lt, rt, (cx, cy - R * 0.18)], fill=(214, 242, 252))
            self._sparkle(d, cx + R * 0.4, cy - R * 0.7, R * 0.22, (255, 255, 255))
        elif key == "cherry":
            rr = R * 0.5
            # Stiele mit leichtem Bogen + Blatt mit Ader
            d.line([(cx, cy - R), (cx - R * 0.2, cy - R * 0.45),
                    (cx - R * 0.6, cy + R * 0.1)], fill=(96, 128, 48), width=5,
                   joint="curve")
            d.line([(cx, cy - R), (cx + R * 0.25, cy - R * 0.4),
                    (cx + R * 0.5, cy + R * 0.1)], fill=(96, 128, 48), width=5,
                   joint="curve")
            d.ellipse([cx - R * 0.1, cy - R * 1.25, cx + R * 0.65, cy - R * 0.8],
                      fill=(96, 168, 62), outline=(60, 118, 38), width=2)
            d.line([(cx + 0, cy - R * 1.02), (cx + R * 0.55, cy - R * 1.02)],
                   fill=(60, 118, 38), width=2)
            self._ball(d, cx - R * 0.72, cy + R * 0.3, rr, (222, 52, 58), (140, 18, 24),
                  (255, 170, 170))
            self._ball(d, cx + R * 0.5, cy + R * 0.38, rr, (236, 66, 72), (150, 22, 28),
                  (255, 190, 190))
        elif key == "lemon":
            d.ellipse([cx - R * 0.9, cy - R * 0.56, cx + R * 0.98, cy + R * 0.68],
                      fill=(190, 150, 10))                         # Schatten/Rand
            d.ellipse([cx - R * 0.92, cy - R * 0.62, cx + R * 0.92, cy + R * 0.62],
                      fill=(248, 214, 44), outline=(196, 158, 12), width=2)
            # Enden-Noppen + Schattierung + Glanz
            d.ellipse([cx - R * 1.04, cy - R * 0.16, cx - R * 0.78, cy + R * 0.16],
                      fill=(248, 214, 44), outline=(196, 158, 12), width=2)
            d.ellipse([cx + R * 0.78, cy - R * 0.16, cx + R * 1.04, cy + R * 0.16],
                      fill=(248, 214, 44), outline=(196, 158, 12), width=2)
            d.ellipse([cx - R * 0.55, cy - R * 0.45, cx + R * 0.2, cy - R * 0.05],
                      fill=(255, 240, 150))
            d.polygon([(cx + R * 0.4, cy - R * 0.5), (cx + R * 0.95, cy - R * 0.95),
                       (cx + R * 0.72, cy - R * 0.32)], fill=(88, 165, 58))
        elif key == "grape":
            rr = R * 0.34
            d.line([(cx, cy - R * 1.15), (cx, cy - R * 0.55)], fill=(112, 82, 42), width=5)
            d.ellipse([cx + R * 0.02, cy - R * 1.32, cx + R * 0.72, cy - R * 0.9],
                      fill=(96, 168, 62), outline=(60, 118, 38), width=2)
            # Beeren von hinten nach vorn - jede mit eigenem Glanzpunkt
            for gx, gy in [(-0.52, 0.0), (0.52, 0.0), (0.0, -0.12), (-0.28, 0.45),
                           (0.28, 0.45), (0.0, 0.88)]:
                ox, oy = cx + gx * R, cy + gy * R - R * 0.2
                self._ball(d, ox, oy, rr, (148, 88, 198), (92, 46, 138), (208, 168, 235))


    def _slot_window(self, img, x, y, s, key):
        d = ImageDraw.Draw(img, "RGBA")
        d.rounded_rectangle([x, y, x + s, y + s], radius=16, fill=(248, 249, 252),
                            outline=(60, 64, 80), width=3)
        d.rounded_rectangle([x + 5, y + 5, x + s - 5, y + 18], radius=8, fill=(255, 255, 255, 60))
        self._slot_symbol(d, x + s / 2, y + s / 2, s * 0.3, key)


    def slot_machine(self, symbols, *, win = 0, jackpot = False):
        """Rendert drei Slot-Walzen. ``symbols``: 3 Schluessel aus SLOT_KEYS.
        ``win``: Gewinn in Coins (0 = nichts). ``jackpot``: drei Gleiche."""
        pad, tile, gap, top_h, bot_h = 26, 150, 18, 72, 58
        W = pad * 2 + 3 * tile + 2 * gap
        H = pad * 2 + top_h + tile + bot_h
        img = self._vgrad(W, H, (34, 18, 48), (16, 10, 26)).convert("RGBA")
        d = ImageDraw.Draw(img, "RGBA")
        d.rounded_rectangle([8, 8, W - 8, H - 8], radius=20, outline=(245, 197, 24, 200), width=4)
        d.text((W / 2, pad + 2), "★  FLO  SLOTS  ★", font=self._font(34),
               fill=(245, 197, 24), anchor="ma")

        ry, rx = pad + top_h, pad
        d.rounded_rectangle([pad - 8, ry - 8, W - pad + 8, ry + tile + 8], radius=16,
                            fill=(8, 6, 14))
        for key in symbols:
            self._slot_window(img, rx, ry, tile, key)
            rx += tile + gap

        d = ImageDraw.Draw(img, "RGBA")
        line_col = (46, 204, 113) if win > 0 else (96, 100, 118)
        ly = ry + tile / 2
        d.line([(pad - 2, ly), (W - pad + 2, ly)], fill=line_col, width=3)

        by = ry + tile + 14
        if jackpot:
            self._pill_c(d, W / 2, int(by), "JACKPOT!", 30, (245, 197, 24), (28, 16, 4))
        elif win > 0:
            self._pill_c(d, W / 2, int(by), f"GEWINN  +{win}", 26, (46, 204, 113), (8, 28, 16))
        else:
            self._pill_c(d, W / 2, int(by), "leider nichts", 24, (70, 74, 92), (228, 230, 238))
        return self._png(img)


    # --- Coinflip ------------------------------------------------------------
    def _crown(self, d, cx, cy, s, col):
        base = cy + s * 0.45
        pts = [(cx - s, base), (cx - s, cy - s * 0.1), (cx - s * 0.5, cy + s * 0.2),
               (cx, cy - s * 0.55), (cx + s * 0.5, cy + s * 0.2), (cx + s, cy - s * 0.1),
               (cx + s, base)]
        d.polygon(pts, fill=col)
        d.rectangle([cx - s, base, cx + s, base + s * 0.3], fill=col)
        for px_ in (cx - s, cx, cx + s):
            d.ellipse([px_ - s * 0.12, cy - s * 0.62, px_ + s * 0.12, cy - s * 0.38], fill=col)


    def coin_flip(self, result):
        """Goldmuenze, die ``kopf`` (Krone) oder ``zahl`` (Stern) zeigt."""
        W = H = 360
        img = self._vgrad(W, H, (26, 32, 52), (12, 15, 26)).convert("RGBA")
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
            self._crown(d, cx, cy - R * 0.12, R * 0.42, (150, 110, 10))
            label = "KOPF"
        else:
            d.text((cx, cy - R * 0.12), "★", font=self._font(int(R * 0.95)),
                   fill=(150, 110, 10), anchor="mm")
            label = "ZAHL"
        d.text((cx, cy + R * 0.52), label, font=self._font(36), fill=(120, 88, 8), anchor="mm")
        return self._png(img)


    # --- Wuerfel -------------------------------------------------------------
    def _pips(self, d, x, y, s, val):
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


    def _die(self, img, x, y, s, val, sides):
        d = ImageDraw.Draw(img, "RGBA")
        d.rounded_rectangle([x + 5, y + 7, x + s + 5, y + s + 7], radius=22, fill=(0, 0, 0, 80))
        d.rounded_rectangle([x, y, x + s, y + s], radius=22, fill=(248, 249, 252),
                            outline=(60, 64, 80), width=3)
        if sides == 6 and 1 <= val <= 6:
            self._pips(d, x, y, s, val)
        else:
            d.text((x + s / 2, y + s / 2 - 6), str(val), font=self._font(int(s * 0.46)),
                   fill=(40, 44, 60), anchor="mm")
            d.text((x + s / 2, y + s - 18), f"W{sides}", font=self._font(16),
                   fill=(150, 154, 170), anchor="mm")


    def _dice_img(self, rolls, sides, *, jitter = 0,
                  show_sum = True, seed = 0):
        """Wuerfelbild als Image. ``jitter``: max. Versatz in px (Kullern)."""
        n = max(1, len(rolls))
        die, gap, pad = 120, 20, 28
        per_row = min(n, 8)
        nrows = (n + per_row - 1) // per_row
        W = max(260, pad * 2 + per_row * die + (per_row - 1) * gap)
        grid_h = nrows * die + (nrows - 1) * gap
        H = pad * 2 + grid_h + (44 if n > 1 else 0)
        img = self._vgrad(W, H, (30, 34, 46), (14, 16, 22)).convert("RGBA")
        rng = random.Random(seed)
        x0 = (W - (per_row * die + (per_row - 1) * gap)) // 2
        for i, r in enumerate(rolls):
            rr, cc = divmod(i, per_row)
            jx = rng.randint(-jitter, jitter) if jitter else 0
            jy = rng.randint(-jitter, jitter) if jitter else 0
            self._die(img, x0 + cc * (die + gap) + jx, pad + rr * (die + gap) + jy, die, r, sides)
        if n > 1 and show_sum:
            d = ImageDraw.Draw(img, "RGBA")
            self._pill_c(d, W / 2, pad + grid_h + 8, f"Summe  {sum(rolls)}", 24,
                    (245, 197, 24), (22, 14, 4))
        return img


    def dice_roll(self, rolls, sides):
        """Wuerfel als Bild. d6 zeigt Augen, sonst die Zahl + 'W<n>'. Bei vielen
        Wuerfeln (>8) wird in mehrere Reihen umgebrochen, damit es kompakt bleibt."""
        return self._png(self._dice_img(rolls, sides))


    def dice_roll_anim(self, rolls, sides):
        """Wuerfeln als GIF: die Wuerfel kullern (zufaellige Zwischen-Augen +
        Wackeln), rasten dann nacheinander auf dem Ergebnis ein."""
        frames = []
        durations = []
        n = len(rolls)
        tumble = 6
        for f in range(tumble):
            zufall = [random.randint(1, sides) if f < tumble - 1 - (i % 2) else rolls[i]
                      for i in range(n)]
            frames.append(self._dice_img(zufall, sides, jitter=max(1, 7 - f), show_sum=False,
                                    seed=f))
            durations.append(80)
        frames.append(self._dice_img(rolls, sides, jitter=1, show_sum=False, seed=99))
        durations.append(90)
        frames.append(self._dice_img(rolls, sides))
        durations.append(3500)
        return self._gif(frames, durations)


    # --- Roulette ------------------------------------------------------------
    _ROUL_RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
    _ROUL_ORDER = [0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23,
                   10, 5, 24, 16, 33, 1, 20, 14, 31, 9, 22, 18, 29, 7, 28, 12, 35, 3, 26]


    def _roul_color(self, num):
        if num == 0:
            return (39, 174, 96)
        return (192, 57, 43) if num in self._ROUL_RED else (30, 32, 40)


    def roulette_wheel(self, spin, won):
        """Roulette-Kessel mit Zahlenring, Kugel am Gewinnerfach und Ergebnis-Hub."""
        W = H = 440
        img = self._vgrad(W, H, self._FELT_TOP, self._FELT_BOT).convert("RGBA")
        d = ImageDraw.Draw(img, "RGBA")
        d.rounded_rectangle([6, 6, W - 6, H - 6], radius=20,
                            outline=(46, 204, 113) if won else (231, 76, 60), width=4)
        cx, cy, Ro, Ri = W / 2, H / 2 + 4, 196, 150
        n = len(self._ROUL_ORDER)
        seg = 360.0 / n
        idx = self._ROUL_ORDER.index(spin) if spin in self._ROUL_ORDER else 0
        base = -90.0 - idx * seg - seg / 2          # Gewinner-Fach nach oben drehen
        for i, num in enumerate(self._ROUL_ORDER):
            a0 = base + i * seg
            d.pieslice([cx - Ro, cy - Ro, cx + Ro, cy + Ro], a0, a0 + seg,
                       fill=self._roul_color(num), outline=(18, 18, 22))
        Rm = (Ro + Ri) / 2 + 6
        for i, num in enumerate(self._ROUL_ORDER):
            a = math.radians(base + i * seg + seg / 2)
            d.text((cx + Rm * math.cos(a), cy + Rm * math.sin(a)), str(num),
                   font=self._font(14), fill=(245, 245, 245), anchor="mm")
        d.ellipse([cx - Ri, cy - Ri, cx + Ri, cy + Ri], fill=(12, 60, 40),
                  outline=(245, 197, 24), width=4)
        # Kugel + Zeiger oben
        d.polygon([(cx - 15, cy - Ro - 24), (cx + 15, cy - Ro - 24), (cx, cy - Ro + 10)],
                  fill=(245, 245, 245), outline=(20, 20, 20))
        d.ellipse([cx - 11, cy - Ro + 8, cx + 11, cy - Ro + 30], fill=(250, 250, 252),
                  outline=(60, 60, 60), width=2)
        # Ergebnis-Hub
        d.ellipse([cx - 72, cy - 72, cx + 72, cy + 72], fill=self._roul_color(spin),
                  outline=(245, 197, 24), width=4)
        d.text((cx, cy - 8), str(spin), font=self._font(60), fill=(250, 250, 252), anchor="mm")
        name = "GRÜN" if spin == 0 else ("ROT" if spin in self._ROUL_RED else "SCHWARZ")
        d.text((cx, cy + 42), name, font=self._font(22), fill=(250, 250, 252), anchor="mm")
        return self._png(img)


    # --- Keno ----------------------------------------------------------------
    def _legend(self, d, x, y, col, text):
        f = self._font(16)
        d.rounded_rectangle([x, y, x + 22, y + 22], radius=5, fill=col)
        d.text((x + 30, y + 11), text, font=f, fill=(210, 214, 228), anchor="lm")
        return 30 + int(d.textlength(text, font=f)) + 26


    def keno_grid(self, picks, draw, hits):
        """Zahlenraster 1-40: Treffer (gold), eigener Tipp (blau), gezogen (grau)."""
        return self._png(self._keno_img(picks, draw, hits))


    def _keno_img(self, picks, draw, hits,
                  pop = None):
        """Zeichnet das Keno-Raster als Image (fuer PNG und GIF-Frames).
        ``pop``: diese frisch gezogene Zahl wird groesser + heller gezeichnet."""
        cols, rows = 8, 5
        cell, gap, pad, top = 58, 10, 28, 60
        W = pad * 2 + cols * cell + (cols - 1) * gap
        H = top + rows * cell + (rows - 1) * gap + 56
        img = self._vgrad(W, H, (22, 28, 40), (12, 15, 24)).convert("RGBA")
        d = ImageDraw.Draw(img, "RGBA")
        d.text((pad, 16), "KENO", font=self._font(30), fill=(241, 196, 15))
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
                       font=self._font(26 if grow else 22), fill=fg, anchor="mm")
                if grow and num in hitset:        # Treffer: Funkeln am Feld
                    self._sparkle(d, x - 6, y - 4, 8, (255, 240, 170))
                    self._sparkle(d, x + cell + 6, y + cell + 2, 7, (255, 240, 170))
                x += cell + gap
            y += cell + gap
        ly = y + 8
        lx = pad
        lx += self._legend(d, lx, ly, (241, 196, 15), f"Treffer {len(hits)}")
        lx += self._legend(d, lx, ly, (41, 55, 90), "dein Tipp")
        self._legend(d, lx, ly, (66, 70, 88), "gezogen")
        return img


    # --- Shop-Banner (v1.2) --------------------------------------------------
    def _hex(self, value):
        return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)


    def _mix(self, a, b, f):
        return tuple(round(a[i] + (b[i] - a[i]) * f) for i in range(3))


    def _round_grad(self, w, h, radius, top, bot):
        """RGBA-Kachel mit vertikalem Verlauf (top->bot) und abgerundeten Ecken."""
        grad = self._vgrad(w, h, top, bot).convert("RGBA")
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
        grad.putalpha(mask)
        return grad


    def _fit_font(self, d, text, max_w,
                  start, floor):
        """Groesste Schrift <= start, bei der 'text' in max_w passt; sonst kuerzen mit '…'."""
        size = start
        while size > floor and d.textlength(text, font=self._font(size)) > max_w:
            size -= 2
        f = self._font(size)
        if d.textlength(text, font=f) > max_w:
            while text and d.textlength(text + "…", font=f) > max_w:
                text = text[:-1]
            text += "…"
        return f, text


    def shop_banner(self, items, *, date = ""):
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
        img = self._vgrad(W, H, (24, 27, 42), (10, 12, 20)).convert("RGBA")
        gw, gh = W // 2, H // 2
        glow = Image.new("RGBA", (gw, gh), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gd.ellipse([-110, -150, 250, 130], fill=(70, 96, 210, 70))        # blau, links oben
        gd.ellipse([gw - 240, -160, gw + 120, 110], fill=(150, 70, 185, 55))  # lila, rechts oben
        glow = glow.filter(ImageFilter.GaussianBlur(46)).resize((W, H))
        img = Image.alpha_composite(img, glow)
        d = ImageDraw.Draw(img, "RGBA")

        # --- Kopfzeile: "FLO" weiss + "SHOP" gold, Untertitel-Pill, Datum rechts ---
        hf = self._font(56)
        d.text((pad, 30), "FLO", font=hf, fill=self._WHITE)
        flo_w = d.textlength("FLO ", font=hf)
        d.text((pad + flo_w, 30), "SHOP", font=hf, fill=self._GOLD)
        self._pill(d, pad + 3, 102, "TÄGLICH 2 UHR NEU", 16, (44, 50, 76), (188, 198, 222))
        if date:
            d.text((W - pad, 112), f"Stand {date}", font=self._font(18),
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
            col = self._hex(int(e.get("color", 0x57F287)))
            # Karte: dezenter Verlauf, in der Seltenheitsfarbe getoent + farbiger Rand
            card = self._round_grad(cw, row_h, 22, self._mix((32, 36, 54), col, 0.20),
                               self._mix((17, 19, 30), col, 0.06))
            img.paste(card, (pad, y), card)
            d.rounded_rectangle([pad, y, W - pad, y + row_h], radius=22,
                                outline=col, width=2)

            # Nummern-Kachel (gerundetes Quadrat, Seltenheitsfarbe + Glanz)
            ks = 66
            kx, ky = pad + 16, y + (row_h - ks) // 2
            tile = self._round_grad(ks, ks, 16, self._mix(col, self._WHITE, 0.30),
                               self._mix(col, (0, 0, 0), 0.20))
            img.paste(tile, (kx, ky), tile)
            d.rounded_rectangle([kx, ky, kx + ks, ky + ks], radius=16,
                                outline=self._mix(col, self._WHITE, 0.45), width=1)
            d.text((kx + ks / 2, ky + ks / 2 - 1), str(e.get("n", "?")),
                   font=self._font(34), fill=(14, 16, 24), anchor="mm")

            # Preis-Pill rechts (dunkel, Goldmuenze + Betrag) – zuerst, fuer Titelbreite
            price = f"{e.get('price', 0)}"
            pf = self._font(27)
            pw = d.textlength(price, font=pf)
            pill_w = int(pw + 30 + 28)
            pill_h = 46
            px1 = W - pad - 18 - pill_w
            py = y + (row_h - pill_h) // 2
            d.rounded_rectangle([px1, py, px1 + pill_w, py + pill_h], radius=pill_h // 2,
                                fill=(13, 15, 24), outline=self._mix((13, 15, 24), self._GOLD, 0.45),
                                width=1)
            cx0, cy0 = px1 + 14, py + (pill_h - 28) // 2
            d.ellipse([cx0, cy0, cx0 + 28, cy0 + 28], fill=self._GOLD, outline=(176, 136, 8), width=2)
            d.text((cx0 + 14, cy0 + 14), "C", font=self._font(18), fill=(120, 90, 0), anchor="mm")
            d.text((px1 + pill_w - 16, y + row_h / 2), price, font=pf, fill=self._GOLD, anchor="rm")

            # Titel (adaptiv) + gefuellter Seltenheits-Pill
            tx = kx + ks + 24
            max_tw = px1 - tx - 20
            tf, title = self._fit_font(d, str(e.get("text", "?")), max_tw, 31, 21)
            d.text((tx, y + 19), title, font=tf, fill=self._WHITE)
            self._pill(d, tx, y + row_h - 38, str(e.get("rarity_label", "")).upper(), 15,
                  col, (15, 17, 25))

            y += row_h + gap

        return self._png(img)


    # === Quote-Meme ("Flo quote") ===========================================
    _RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS


    def _renderable_char(self, font, ch):
        """True, wenn die Schrift fuer ch ein echtes Glyph hat (kein Tofu/leer)."""
        if ord(ch) >= 0x1F000:               # Emoji-/Symbol-Zusatzebenen
            return False
        fid = id(font)
        nd = self._notdef_q.get(fid)
        if nd is None:
            im = Image.new("L", (48, 48), 0)
            ImageDraw.Draw(im).text((6, 6), "￿", font=font, fill=255)
            nd = im.tobytes()
            self._notdef_q[fid] = nd
        im = Image.new("L", (48, 48), 0)
        try:
            ImageDraw.Draw(im).text((6, 6), ch, font=font, fill=255)
        except Exception:
            return False
        b = im.tobytes()
        return bool(b) and b != nd and any(b)


    def _clean_text(self, s):
        """Macht Text darstellbar: NFKC, Emoji/Steuer-/Zalgo-/unbekannte Glyphen raus."""
        s = unicodedata.normalize("NFKC", s or "")
        ref = self._font(40)
        out = []
        for ch in s:
            if ch in ("\n", "\t", " "):
                out.append(" ")
                continue
            cat = unicodedata.category(ch)
            if cat[0] == "M" or cat in ("Cc", "Cf", "Cs", "Co", "Cn"):
                continue
            if self._renderable_char(ref, ch):
                out.append(ch)
        return " ".join("".join(out).split()).strip()


    def _wrap(self, d, text, font, max_w):
        """Bricht Text auf max_w Pixel um (Wort-weise; lange Woerter hart trennen)."""
        lines = []
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


    def quote_card(self, avatar, text, author):
        """Zitat-Bild im 'make it a quote'-Stil: links das (graustufige) Profilbild
        mit Verlauf ins Schwarze, rechts das Zitat + '- Name' darunter."""
        W, H, AVW = 1200, 630, 620
        img = Image.new("RGB", (W, H), (0, 0, 0))

        # Profilbild links: Graustufen, quadratisch gefittet, Alpha-Verlauf nach schwarz.
        if avatar:
            try:
                av = Image.open(io.BytesIO(avatar)).convert("L")
                av = ImageOps.autocontrast(ImageOps.fit(av, (AVW, H), method=self._RESAMPLE))
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
        quote = self._clean_text(text) or "..."
        quote = f"„{quote}“"        # „..."
        chosen, lines = 50, [quote]
        for size in range(54, 23, -2):
            f = self._font(size)
            ls = self._wrap(d, quote, f, tw)
            if (size + 12) * len(ls) <= H - 210:
                chosen, lines = size, ls
                break
        f = self._font(chosen)
        line_h = chosen + 12
        block_h = line_h * len(lines)
        y = (H - block_h) // 2 - 16
        for ln in lines:
            lw = d.textlength(ln, font=f)
            d.text((tx0 + (tw - lw) / 2, y), ln, font=f, fill=self._WHITE)
            y += line_h

        # Autor darunter ("- Name").
        author = self._clean_text(author) or "Unbekannt"
        fa = self._font(30)
        aut = f"— {author}"
        aw = d.textlength(aut, font=fa)
        d.text((tx0 + (tw - aw) / 2, y + 20), aut, font=fa, fill=(160, 162, 172))

        return self._png(img)


    # === Ernaehrungs-Karte ("Kalorien-Channel") ===============================
    def _round_img(self, data, w, h, radius = 0):
        """Bild-Bytes -> RGBA, auf w x h gefittet, optional mit runden Ecken."""
        try:
            im = ImageOps.fit(Image.open(io.BytesIO(data)).convert("RGB"), (w, h),
                              method=self._RESAMPLE)
        except Exception:  # noqa: BLE001
            return None
        out = im.convert("RGBA")
        if radius > 0:
            mask = Image.new("L", (w, h), 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1],
                                                   radius=radius, fill=255)
            out.putalpha(mask)
        return out


    def _fnum(self, val):
        """Robuste Zahl aus beliebigen (LLM-)Werten: 1200, "1200", "ca. 1200 kcal",
        "8/10" -> erste Zahl; sonst 0. Schuetzt die Karte unabhaengig vom Aufrufer."""
        if isinstance(val, (int, float)):
            return float(val)
        import re as _re
        m = _re.search(r"-?\d+(?:[.,]\d+)?", str(val or ""))
        return float(m.group(0).replace(",", ".")) if m else 0.0


    def _score_color(self, score):
        """0 (Industrie, rot) -> 10 (natuerlich, gruen), stufenlos."""
        f = max(0.0, min(1.0, score / 10.0))
        r1, g1, b1 = (231, 76, 60)     # rot
        r2, g2, b2 = (46, 204, 113)    # gruen
        return (round(r1 + (r2 - r1) * f), round(g1 + (g2 - g1) * f),
                round(b1 + (b2 - b1) * f))


    def _hbar(self, d, x, y, w, h, frac, color,
              track=(38, 42, 50)):
        """Horizontaler Wert-Balken mit rundem Track."""
        frac = max(0.0, min(1.0, frac))
        r = h // 2
        d.rounded_rectangle([x, y, x + w, y + h], radius=r, fill=track)
        fw = int(round(w * frac))
        if frac > 0 and fw < h:
            fw = h
        if fw > 0:
            d.rounded_rectangle([x, y, x + fw, y + h], radius=r, fill=color)


    def nutrition_card(self, food_img, data):
        """Ernaehrungs-Karte: links das Essensfoto, rechts Kalorien, Makros,
        Natuerlichkeits-Score (gruen=natuerlich, rot=Industrie) und Fazit."""
        W, H, PW = 1200, 640, 470
        img = self._vgrad(W, H, (24, 27, 35), (13, 15, 20)).convert("RGBA")
        d = ImageDraw.Draw(img)

        # --- Foto links (abgerundet), sonst Emoji-Platzhalter ---
        photo = self._round_img(food_img, PW - 40, H - 48, radius=22) if food_img else None
        if photo is not None:
            img.paste(photo, (24, 24), photo)
        else:
            d.rounded_rectangle([24, 24, PW - 16, H - 24], radius=22, fill=(30, 34, 44))
            d.text((PW // 2 - 40, H // 2 - 60), "🍽", font=self._font(96), fill=(90, 96, 110))

        x0, x1 = PW + 24, W - 44
        tw = x1 - x0

        # --- Gerichtname (bis 2 Zeilen, automatisch verkleinert) ---
        name = self._clean_text(str(data.get("gericht") or "Unbekanntes Gericht"))[:80] or "Essen"
        nf, nlines = self._font(44), [name]
        for size in (44, 38, 32, 27):
            nf = self._font(size)
            nlines = self._wrap(d, name, nf, tw)
            if len(nlines) <= 2:
                break
        nlines = nlines[:2]
        y = 34
        for ln in nlines:
            d.text((x0, y), ln, font=nf, fill=self._WHITE)
            y += nf.size + 8

        # --- Kalorien gross ---
        y += 10
        kcal = int(self._fnum(data.get("kcal")))
        kmin, kmax = int(self._fnum(data.get("kcal_min"))), int(self._fnum(data.get("kcal_max")))
        d.text((x0, y), f"{kcal}", font=self._font(76), fill=self._GOLD)
        kw = d.textlength(f"{kcal}", font=self._font(76))
        d.text((x0 + kw + 14, y + 40), "kcal", font=self._font(30), fill=(150, 155, 168))
        if kmax > kmin > 0:
            d.text((x0 + kw + 100, y + 46), f"(≈ {kmin}–{kmax})", font=self._font(20),
                   fill=(120, 125, 138))
        y += 100

        # --- Makro-Balken ---
        macros = [
            ("EIWEISS", self._fnum(data.get("protein_g")), (46, 204, 113)),
            ("KOHLENHYDRATE", self._fnum(data.get("carbs_g")), (87, 148, 242)),
            ("FETT", self._fnum(data.get("fett_g")), (255, 152, 48)),
            ("ZUCKER", self._fnum(data.get("zucker_g")), (240, 98, 146)),
        ]
        peak = max([m[1] for m in macros] + [1.0])
        lf, vf = self._font(17), self._font(21)
        for label, grams, col in macros:
            d.text((x0, y), label, font=lf, fill=(140, 145, 158))
            self._hbar(d, x0 + 190, y + 3, tw - 300, 16, grams / peak, col)
            d.text((x1 - 92, y - 1), f"{grams:g} g", font=vf, fill=self._WHITE)
            y += 38
        y += 14

        # --- Natuerlichkeits-Score ---
        score = max(0.0, min(10.0, self._fnum(data.get("natur_score"))))
        scol = self._score_color(score)
        d.text((x0, y), "NATÜRLICHKEIT", font=lf, fill=(140, 145, 158))
        d.text((x1 - 92, y - 3), f"{score:g}/10", font=self._font(24), fill=scol)
        self._hbar(d, x0, y + 26, tw - 110, 20, score / 10.0, scol)
        y += 62
        verarbeitung = self._clean_text(str(data.get("verarbeitung") or ""))[:60]
        if verarbeitung:
            d.text((x0, y), verarbeitung, font=self._font(20), fill=scol)
            y += 34

        # --- Fazit-Pille + Flo-Spruch ---
        if score >= 7:
            pill_txt = "✓ GUT FÜR DEINEN KÖRPER"
        elif score >= 4:
            pill_txt = "~ GEHT SO – IN MASSEN"
        else:
            pill_txt = "✗ INDUSTRIE – LASS ES LIEBER"
        self._pill(d, x0, y + 4, pill_txt.replace("✓ ", "").replace("✗ ", "").replace("~ ", ""),
              22, scol, (13, 15, 20))
        y += 62
        spruch = self._clean_text(str(data.get("flo_spruch") or data.get("fazit") or ""))
        if spruch:
            sf = self._font(19)
            for ln in self._wrap(d, f"„{spruch[:180]}“", sf, tw)[:3]:
                d.text((x0, y), ln, font=sf, fill=(165, 170, 182))
                y += 26

        d.rounded_rectangle([6, 6, W - 7, H - 7], radius=18,
                            outline=(48, 53, 63), width=2)
        return self._png(img)


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


    def level_card(self, avatar, *, name, level, into,
                   step, place, total, xp, coins,
                   msgs, voice_secs, streak, title = "",
                   accent = None,
                   frame = None):
        """Rank-Card: Avatar mit Ring, Name, Titel, Level + Platz, XP-Balken und
        Stat-Zeile (Coins, Nachrichten, Voice, Streak). ``frame``: Luxus-Rahmen
        aus dem Flo-Luxus-Shop (bronze/silber/gold/diamant/galaxie/imperium)."""
        W, H = 1000, 320
        acc = accent or (88, 101, 242)   # Blurple, ausser eine Titel-Farbe kommt mit
        img = self._vgrad(W, H, (26, 29, 38), (13, 15, 20)).convert("RGBA")
        d = ImageDraw.Draw(img)

        # --- Avatar links (rund, mit Akzent-Ring) ---
        AD = 190
        ax, ay = 42, (H - AD) // 2
        circ = None
        if avatar:
            try:
                im = ImageOps.fit(Image.open(io.BytesIO(avatar)).convert("RGB"),
                                  (AD, AD), method=self._RESAMPLE)
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
            iw = d.textlength(ini, font=self._font(84))
            d.text((ax + AD / 2 - iw / 2, ay + AD / 2 - 52), ini, font=self._font(84),
                   fill=(120, 126, 140))
        d.ellipse([ax - 5, ay - 5, ax + AD + 4, ay + AD + 4], outline=acc, width=5)

        x0, x1 = ax + AD + 46, W - 46
        tw = x1 - x0

        # --- Name + Titel-Pille ---
        safe = self._clean_text(name)[:32] or "Spieler"
        nf = self._font(46) if d.textlength(safe, font=self._font(46)) <= tw - 220 else self._font(34)
        d.text((x0, 34), safe, font=nf, fill=self._WHITE)
        if title:
            t = self._clean_text(title)[:28]
            if t:
                tf = self._font(19)
                pw = int(d.textlength(t, font=tf) + 26)
                px = x1 - pw
                d.rounded_rectangle([px, 40, px + pw, 40 + 32], radius=16, fill=acc)
                d.text((px + 13, 45), t, font=tf, fill=(13, 15, 20))

        # --- Level + Platz (keine Emojis - die Schrift hat keine Emoji-Glyphen) ---
        d.text((x0, 96), f"Level {level}", font=self._font(34), fill=self._GOLD)
        lw = d.textlength(f"Level {level}", font=self._font(34))
        d.text((x0 + lw + 26, 104), f"Platz #{place} von {total}", font=self._font(22),
               fill=(150, 155, 168))

        # --- XP-Balken ---
        pct = 1.0 if step <= 0 else max(0.0, min(1.0, into / step))
        self._hbar(d, x0, 156, tw, 26, pct, acc)
        d.text((x0, 192), f"{into} / {step} XP bis Level {level + 1}",
               font=self._font(19), fill=(150, 155, 168))
        ptxt = f"{round(pct * 100)}%"
        d.text((x1 - d.textlength(ptxt, font=self._font(19)), 192), ptxt,
               font=self._font(19), fill=(150, 155, 168))

        # --- Stat-Zeile (Label ueber Wert; Text statt Emoji - kein Tofu) ---
        h, rem = divmod(int(voice_secs), 3600)
        m = rem // 60
        vtxt = f"{h}h {m}m" if h else f"{m}m"
        stats = [("COINS", f"{coins:,}".replace(",", ".")),
                 ("NACHRICHTEN", f"{msgs:,}".replace(",", ".")),
                 ("VOICE", vtxt),
                 ("STREAK", f"{streak} Tag(e)")]
        lf2, vf2 = self._font(15), self._font(23)
        seg = tw // len(stats)
        for i, (label, val) in enumerate(stats):
            sx = x0 + i * seg
            d.text((sx, 232), label, font=lf2, fill=(120, 126, 140))
            d.text((sx, 254), val, font=vf2, fill=(200, 205, 218))

        style = self._FRAME_STYLES.get(frame or "")
        if style is None:
            d.rounded_rectangle([6, 6, W - 7, H - 7], radius=18, outline=(48, 53, 63), width=2)
        else:
            # Luxus-Rahmen: kraeftiger Aussenrand + feine Innenlinie (zweifarbig
            # bei Diamant/Galaxie/Imperium), Funkeln ab Gold, Label unterm Avatar.
            col, col2 = style["col"], style["col2"] or style["col"]
            d.rounded_rectangle([4, 4, W - 5, H - 5], radius=20, outline=col, width=5)
            d.rounded_rectangle([12, 12, W - 13, H - 13], radius=14, outline=col2, width=2)
            if frame in self._FRAME_SPARKLE:
                self._sparkle(d, 26, 26, 9, col2)
                self._sparkle(d, W - 28, 30, 8, col)
                self._sparkle(d, W - 44, H - 30, 9, col2)
                self._sparkle(d, 40, H - 26, 7, col)
            lf3 = self._font(15)
            lbl = style["label"]
            lw = int(d.textlength(lbl, font=lf3)) + 22
            lx = ax + (AD - lw) // 2
            d.rounded_rectangle([lx, H - 40, lx + lw, H - 16], radius=12, fill=col)
            d.text((lx + 11, H - 36), lbl, font=lf3, fill=(18, 16, 10))
        return self._png(img)


    # === Animationen (GIF) ====================================================
    # Alle *_anim-Funktionen liefern ein animiertes GIF als io.BytesIO. Sie sind
    # CPU-gebunden (Pillow) - die Aufrufer starten sie deshalb via
    # asyncio.to_thread, damit der Event-Loop nie blockiert. Frame-Anzahl und
    # Groessen sind bewusst klein gehalten (Discord spielt GIFs im Embed ab).
    def _gif(self, frames, durations, *, colors = 144):
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


    def _ease_out(self, t):
        """Kubisches Aus-Bremsen: schnell starten, weich zum Stillstand."""
        return 1.0 - (1.0 - t) ** 3


    _CONFETTI_COLS = [(241, 196, 15), (46, 204, 113), (87, 148, 242),
                      (231, 76, 60), (155, 89, 182), (250, 250, 252)]


    def _confetti(self, img, t, *, seed = 7, n = 46):
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
            col = self._CONFETTI_COLS[i % len(self._CONFETTI_COLS)]
            y = t * speed * (H + 60) - 30
            x = x0 + drift * t
            if y < -12 or y > H + 12:
                continue
            a = rot + t * 7 + i
            dx, dy = math.cos(a) * size, math.sin(a) * size
            d.line([(x - dx, y - dy), (x + dx, y + dy)], fill=col, width=3)


    def _sparkle(self, d, x, y, r,
                 col = (255, 255, 255)):
        """Kleiner 4-Strahlen-Funkel-Stern."""
        d.line([(x - r, y), (x + r, y)], fill=col, width=2)
        d.line([(x, y - r), (x, y + r)], fill=col, width=2)
        rr = r * 0.45
        d.line([(x - rr, y - rr), (x + rr, y + rr)], fill=col, width=1)
        d.line([(x - rr, y + rr), (x + rr, y - rr)], fill=col, width=1)


    def _flash(self, img, color, alpha):
        """Kurzer Vollbild-Blitz (z. B. gruen bei Gewinn, rot bei Crash)."""
        overlay = Image.new("RGBA", img.size, (*color, alpha))
        return Image.alpha_composite(img.convert("RGBA"), overlay)


    # --- Muenzwurf (animiert) --------------------------------------------------
    def _coin_face(self, d, cx, cy, R,
                   face, squash, *, shadow_y = None,
                   shadow_scale = 1.0):
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
                self._crown(d, cx, cy - ry * 0.10, R * 0.40 * squash, (150, 110, 10))
            else:
                d.text((cx, cy - ry * 0.08), "★", font=self._font(int(R * 0.9 * squash)),
                       fill=(150, 110, 10), anchor="mm")


    def coin_flip_anim(self, result):
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
        frames = []
        for h, squash, face in seq:
            img = self._vgrad(W, H, (26, 32, 52), (12, 15, 26)).convert("RGBA")
            d = ImageDraw.Draw(img, "RGBA")
            cy = rest_y - h * 116
            self._coin_face(d, W / 2, cy, R, face, squash,
                       shadow_y=ground, shadow_scale=1.0 - 0.45 * h)
            frames.append(img)
        # Endbild: Ergebnis + Label + Funkeln (+ dezentes Konfetti).
        img = self._vgrad(W, H, (26, 32, 52), (12, 15, 26)).convert("RGBA")
        d = ImageDraw.Draw(img, "RGBA")
        self._coin_face(d, W / 2, rest_y, R, result, 1.0, shadow_y=ground)
        d.text((W / 2, H - 34), result.upper(), font=self._font(34),
               fill=(245, 197, 24), anchor="mm")
        for sx, sy, sr in ((W / 2 - 118, rest_y - 92, 9), (W / 2 + 112, rest_y - 60, 7),
                           (W / 2 + 86, rest_y + 88, 8), (W / 2 - 92, rest_y + 70, 6)):
            self._sparkle(d, sx, sy, sr, (255, 240, 170))
        frames.append(img)
        durations = [55] * (len(frames) - 1) + [3500]
        return self._gif(frames, durations)


    # --- Slots (animiert) ------------------------------------------------------
    def _slot_scroll_tile(self, s, sym_now, sym_next, frac):
        """Ein Walzenfenster mit ECHT durchlaufenden Symbolen: das aktuelle rollt
        nach unten raus, das naechste laeuft von oben ein. frac 0..1 = Fortschritt."""
        tile = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        td = ImageDraw.Draw(tile)
        td.rounded_rectangle([0, 0, s - 1, s - 1], radius=16, fill=(248, 249, 252),
                             outline=(60, 64, 80), width=3)
        dy = frac * s
        self._slot_symbol(td, s / 2, s / 2 + dy, s * 0.3, sym_now)
        self._slot_symbol(td, s / 2, s / 2 + dy - s, s * 0.3, sym_next)
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


    def _slot_still_tile(self, s, key, dy = 0.0):
        """Stehendes Walzenfenster (dy: kleiner Bounce-Versatz beim Einrasten)."""
        tile = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        td = ImageDraw.Draw(tile)
        td.rounded_rectangle([0, 0, s - 1, s - 1], radius=16, fill=(248, 249, 252),
                             outline=(60, 64, 80), width=3)
        self._slot_symbol(td, s / 2, s / 2 + dy, s * 0.3, key)
        return tile


    def _slot_stage(self, f, *, all_lit = False):
        """Grundbild der Maschine: Rahmen, Titel und blinkende Marquee-Lampen.
        Rueckgabe: (bild, pad, fenster_y, tile)."""
        pad, tile, gap, top_h, bot_h = 26, 150, 18, 72, 58
        W = pad * 2 + 3 * tile + 2 * gap
        H = pad * 2 + top_h + tile + bot_h
        img = self._vgrad(W, H, (34, 18, 48), (16, 10, 26)).convert("RGBA")
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
        d.text((W / 2, pad + 6), "★  FLO  SLOTS  ★", font=self._font(34),
               fill=(245, 197, 24), anchor="ma")
        ry = pad + top_h
        d.rounded_rectangle([pad - 8, ry - 8, W - pad + 8, ry + tile + 8], radius=16,
                            fill=(8, 6, 14))
        return img, pad, ry, tile


    def slot_machine_anim(self, symbols, *, win = 0, jackpot = False):
        """Slots als GIF: drei Walzen scrollen echt durch, stoppen nacheinander mit
        einem kleinen Bounce; bei Gewinn blitzt die Linie, beim Jackpot regnet
        Konfetti und alle Lampen leuchten."""
        pad, tile, gap = 26, 150, 18
        stops = (7, 11, 15)
        total = 17
        speed = (0.58, 0.66, 0.74)               # jede Walze etwas anders schnell
        # Scroll-Reihenfolge je Walze: gemischt, endet nahtlos im Zielsymbol.
        seqs = []
        for i in range(3):
            pool = [k for k in self.SLOT_KEYS if k != symbols[i]]
            random.shuffle(pool)
            seqs.append((pool * 4) + [symbols[i]])
        frames = []
        for f in range(total):
            img, _pad, ry, _tile = self._slot_stage(f)
            rx = pad
            for i in range(3):
                if f < stops[i]:
                    prog = (stops[i] - 1 - f) * speed[i]      # rueckwaerts bis 0 am Stop
                    idx = int(prog)
                    seq = seqs[i]
                    sym_now = seq[-(idx % len(seq)) - 1]
                    sym_next = seq[-((idx + 1) % len(seq)) - 1]
                    t = self._slot_scroll_tile(tile, sym_now, sym_next, 1.0 - (prog - idx))
                else:
                    settled = f - stops[i]
                    dy = {0: 13.0, 1: -5.0}.get(settled, 0.0)
                    t = self._slot_still_tile(tile, symbols[i], dy)
                img.paste(t, (rx, ry), t)
                rx += tile + gap
            d = ImageDraw.Draw(img, "RGBA")
            ly = ry + tile / 2
            d.line([(pad - 2, ly), (pad * 2 + 3 * tile + 2 * gap - pad + 2, ly)],
                   fill=(96, 100, 118), width=3)
            self._pill_c(d, img.width / 2, ry + tile + 14, "· · ·", 24, (40, 34, 58),
                    (180, 170, 200))
            frames.append(img)

        # Ergebnis-Frames: Gewinn blitzt, Jackpot bekommt Konfetti-Regen.
        def result_frame(lit, flash_line, conf_t):
            img, _pad, ry, _tile = self._slot_stage(0, all_lit=lit)
            rx = pad
            for i in range(3):
                t = self._slot_still_tile(tile, symbols[i])
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
                self._pill_c(d, img.width / 2, by, "JACKPOT!", 30, (245, 197, 24), (28, 16, 4))
            elif win > 0:
                self._pill_c(d, img.width / 2, by, f"GEWINN  +{win}", 26,
                        (46, 204, 113), (8, 28, 16))
            else:
                self._pill_c(d, img.width / 2, by, "leider nichts", 24,
                        (70, 74, 92), (228, 230, 238))
            if conf_t is not None:
                self._confetti(img, conf_t, seed=13)
            return img

        if jackpot:
            frames.append(self._flash(result_frame(True, True, None), (255, 240, 160), 60))
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
        return self._gif(frames, durations)


    # --- Roulette (animiert) ---------------------------------------------------
    def _roul_ring(self, size, spin):
        """Zeichnet den Zahlenring EINMAL (Gewinnerfach oben) - die Animation
        rotiert dann nur noch dieses Bild (schnell, C-Ebene)."""
        ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(ring, "RGBA")
        c = size / 2
        Ro = size / 2 - 2
        Ri = Ro * 0.765
        n = len(self._ROUL_ORDER)
        seg = 360.0 / n
        idx = self._ROUL_ORDER.index(spin) if spin in self._ROUL_ORDER else 0
        base = -90.0 - idx * seg - seg / 2
        for i, num in enumerate(self._ROUL_ORDER):
            a0 = base + i * seg
            d.pieslice([c - Ro, c - Ro, c + Ro, c + Ro], a0, a0 + seg,
                       fill=self._roul_color(num), outline=(18, 18, 22))
        Rm = (Ro + Ri) / 2 + 6
        for i, num in enumerate(self._ROUL_ORDER):
            a = math.radians(base + i * seg + seg / 2)
            d.text((c + Rm * math.cos(a), c + Rm * math.sin(a)), str(num),
                   font=self._font(14), fill=(245, 245, 245), anchor="mm")
        # Innenkreis gehoert zum Ring (dreht optisch mit, ist aber uni -> unsichtbar)
        d.ellipse([c - Ri, c - Ri, c + Ri, c + Ri], fill=(12, 60, 40),
                  outline=(245, 197, 24), width=4)
        return ring


    def roulette_wheel_anim(self, spin, won):
        """Roulette als GIF: der Kessel dreht sich aus, die KUGEL kreist gegenlaeufig
        aussen, spiralt nach innen und faellt oben ins Gewinnerfach; am Ende
        erscheint das Ergebnis-Hub (bei Gewinn mit Konfetti)."""
        W = H = 440
        ring_size = 392
        ring = self._roul_ring(ring_size, spin)
        cx, cy = W / 2, H / 2 + 4
        rx0, ry0 = int(cx - ring_size / 2), int(cy - ring_size / 2)
        Ro = ring_size / 2 - 2
        Rm = (Ro + Ro * 0.765) / 2 + 6           # Zahlenkranz = Pocket-Radius

        def frame(t, final, conf_t = None):
            img = self._vgrad(W, H, self._FELT_TOP, self._FELT_BOT).convert("RGBA")
            d = ImageDraw.Draw(img, "RGBA")
            outline = ((46, 204, 113) if won else (231, 76, 60)) if final else (245, 197, 24)
            d.rounded_rectangle([6, 6, W - 6, H - 6], radius=20, outline=outline, width=4)
            angle = (1.0 - self._ease_out(t)) * 900.0 if not final else 0.0
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
                e = self._ease_out(t)
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
                d.ellipse([cx - 72, cy - 72, cx + 72, cy + 72], fill=self._roul_color(spin),
                          outline=(245, 197, 24), width=4)
                d.text((cx, cy - 8), str(spin), font=self._font(60), fill=(250, 250, 252), anchor="mm")
                name = "GRÜN" if spin == 0 else ("ROT" if spin in self._ROUL_RED else "SCHWARZ")
                d.text((cx, cy + 42), name, font=self._font(22), fill=(250, 250, 252), anchor="mm")
            if conf_t is not None:
                self._confetti(img, conf_t, seed=29)
            return img

        N = 16
        frames = [frame(i / N, final=False) for i in range(N)]
        if won:
            frames.append(self._flash(frame(1.0, final=True), (46, 204, 113), 46))
            for ct in (0.3, 0.65):
                frames.append(frame(1.0, final=True, conf_t=ct))
            frames.append(frame(1.0, final=True))
            durations = [65] * N + [90, 140, 140, 4000]
        else:
            frames.append(frame(1.0, final=True))
            durations = [65] * N + [4000]
        return self._gif(frames, durations)


    # --- Crash (animiert) ------------------------------------------------------
    def crash_chart_anim(self, crash_point, target, cashed):
        """Crash als GIF: die Kurve waechst live mit laufendem Multiplikator-Badge,
        das Endbild ist der volle Chart (mit Glow, Ziel-Linie, Explosion/Cashout)."""
        W, H = 820, 420
        L, R, T, B = 72, 30, 70, 50
        x0, x1, y0, y1 = L, W - R, T, H - B
        cp = max(1.001, float(crash_point))
        ymax = max(max(cp, target) * 1.16, 1.6)

        def px(t):
            return x0 + t * (x1 - x0)

        def py(m):
            return y1 - (m - 1.0) / (ymax - 1.0) * (y1 - y0)

        base = self._vgrad(W, H, self._CRASH_TOP, self._CRASH_BOT).convert("RGBA")
        bd = ImageDraw.Draw(base, "RGBA")
        bd.rounded_rectangle([6, 6, W - 6, H - 6], radius=20, outline=(255, 255, 255, 26), width=2)
        for m in self._nice_ticks(ymax):
            yy = py(m)
            bd.line([(x0, yy), (x1, yy)], fill=(255, 255, 255, 18), width=1)
            bd.text((x0 - 12, yy), f"{m:g}×", font=self._font(18), fill=(150, 160, 176), anchor="rm")
        bd.line([(x0, y0), (x0, y1)], fill=(80, 90, 106), width=2)
        bd.line([(x0, y1), (x1, y1)], fill=(80, 90, 106), width=2)
        bd.text((x0, 18), "CRASH", font=self._font(28), fill=(236, 240, 246))

        N = 140
        full = [(i / N, cp ** (i / N)) for i in range(N + 1)]
        frames = []
        durations = []
        steps = 12
        for s in range(1, steps + 1):
            f = self._ease_out(s / steps) if s < steps else 1.0
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

            def rot(dx, dy):
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
            bf = self._font(40)
            tw = d.textlength(badge, font=bf)
            d.rounded_rectangle([x1 - tw - 36, 14, x1, 64], radius=16, fill=(0, 0, 0, 130),
                                outline=(120, 200, 255), width=3)
            d.text(((x1 - tw - 36 + x1) / 2, 39), badge, font=bf, fill=(120, 200, 255),
                   anchor="mm")
            frames.append(img)
            durations.append(75)

        # Endbild = der volle statische Chart (Glow, Ziel-Linie, Burst/Cashout).
        final = Image.open(self.crash_chart(crash_point, target, cashed)).convert("RGBA")
        if cashed:
            # Gewinn: gruener Blitz + Konfetti-Regen, dann das Endbild.
            frames.append(self._flash(final, (46, 204, 113), 55))
            durations.append(100)
            for ct in (0.3, 0.65):
                conf = final.copy()
                self._confetti(conf, ct, seed=21)
                frames.append(conf)
                durations.append(140)
        else:
            # Absturz: roter Blitz + Explosion mit wachsender Schockwelle.
            end_x, end_y = px(1.0), py(cp)
            frames.append(self._flash(final, (231, 76, 60), 70))
            durations.append(90)
            for radius in (26, 44):
                boom = final.copy()
                bd = ImageDraw.Draw(boom, "RGBA")
                bd.ellipse([end_x - radius, end_y - radius, end_x + radius, end_y + radius],
                           outline=(255, 190, 90), width=4)
                bd.ellipse([end_x - radius * 0.55, end_y - radius * 0.55,
                            end_x + radius * 0.55, end_y + radius * 0.55],
                           outline=(255, 120, 60), width=3)
                self._burst(bd, end_x, end_y, (231, 96, 60))
                frames.append(boom)
                durations.append(110)
        frames.append(final)
        durations.append(4500)
        return self._gif(frames, durations)


    # --- Keno (animiert) -------------------------------------------------------
    def keno_grid_anim(self, picks, draw, hits, *,
                       big_win = False):
        """Keno-Ziehung als GIF: die 10 Zahlen ploppen nacheinander auf (Treffer
        funkeln); bei einem grossen Gewinn regnet am Ende Konfetti."""
        hitset = set(hits)
        frames = [self._keno_img(picks, [], [])]
        durations = [180]
        for i in range(1, len(draw) + 1):
            part = draw[:i]
            part_hits = [n for n in part if n in hitset]
            frames.append(self._keno_img(picks, part, part_hits, pop=draw[i - 1]))
            durations.append(300)
        final = self._keno_img(picks, draw, hits)
        if big_win:
            frames.append(self._flash(final, (241, 196, 15), 55))
            durations.append(100)
            for ct in (0.3, 0.65):
                conf = final.copy().convert("RGBA")
                self._confetti(conf, ct, seed=17)
                frames.append(conf)
                durations.append(140)
        frames.append(final)
        durations.append(4500)
        return self._gif(frames, durations)


    # --- Gluecksrad ------------------------------------------------------------
    def _wheel_seg_color(self, mult):
        if mult <= 0:
            return (52, 56, 68)          # Niete - dunkelgrau
        if mult < 1.0:
            return (230, 126, 34)        # Teil vom Einsatz - orange
        if mult < 2.0:
            return (52, 152, 219)        # kleiner Gewinn - blau
        if mult < 5.0:
            return (46, 204, 113)        # guter Gewinn - gruen
        return (241, 196, 15)            # Jackpot - gold


    def _wheel_ring(self, size, mults, idx):
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
                       fill=self._wheel_seg_color(m), outline=(16, 18, 24))
        Rm = Ro * 0.72
        for i, m in enumerate(mults):
            a = math.radians(base + i * seg + seg / 2)
            label = "0" if m <= 0 else (f"×{m:g}")
            d.text((c + Rm * math.cos(a), c + Rm * math.sin(a)), label,
                   font=self._font(22), fill=(250, 250, 252), anchor="mm")
        d.ellipse([c - Ro * 0.30, c - Ro * 0.30, c + Ro * 0.30, c + Ro * 0.30],
                  fill=(24, 27, 38), outline=(245, 197, 24), width=4)
        return ring


    def wheel_fortune_anim(self, mults, idx):
        """Gluecksrad als GIF: dreht aus, Gewinnersegment landet oben am Zeiger,
        am Ende zeigt die Nabe den Multiplikator."""
        W = H = 440
        ring_size = 396
        ring = self._wheel_ring(ring_size, mults, idx)
        cx, cy = W / 2, H / 2 + 6
        rx0, ry0 = int(cx - ring_size / 2), int(cy - ring_size / 2)
        won = mults[idx] > 0

        seg_deg = 360.0 / len(mults)

        def frame(angle, final, *, highlight = False,
                  conf_t = None):
            img = self._vgrad(W, H, (30, 24, 52), (13, 11, 24)).convert("RGBA")
            d = ImageDraw.Draw(img, "RGBA")
            outline = ((46, 204, 113) if won else (231, 76, 60)) if final else (245, 197, 24)
            d.rounded_rectangle([6, 6, W - 6, H - 6], radius=20, outline=outline, width=4)
            d.text((18, 14), "GLÜCKSRAD", font=self._font(22), fill=(245, 197, 24))
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
                col = self._wheel_seg_color(m)
                d.ellipse([cx - 64, cy - 64, cx + 64, cy + 64], fill=(24, 27, 38),
                          outline=col, width=5)
                d.text((cx, cy), label, font=self._font(34 if len(label) <= 4 else 26),
                       fill=col, anchor="mm")
            if conf_t is not None:
                self._confetti(img, conf_t, seed=11)
            return img

        N = 15
        frames = [frame((1.0 - self._ease_out(i / N)) * 1080.0, final=False) for i in range(N)]
        if won:
            frames.append(frame(0.0, final=True, highlight=True))
            frames.append(frame(0.0, final=True, conf_t=0.3))
            frames.append(frame(0.0, final=True, highlight=True, conf_t=0.65))
            frames.append(frame(0.0, final=True))
            durations = [70] * N + [110, 140, 140, 4000]
        else:
            frames.append(frame(0.0, final=True))
            durations = [70] * N + [4000]
        return self._gif(frames, durations)


    # --- Rubbellos -------------------------------------------------------------
    def _scratch_img(self, keys, revealed, win_rows, win,
                     show_result, *, sparkle = False):
        """Rubbellos 3x3: ``revealed`` Felder sind schon freigerubbelt, der Rest
        zeigt die Rubbel-Schicht. ``win_rows``: Indizes (0-2) der Gewinn-Reihen."""
        pad, tile, gap, top = 26, 128, 14, 66
        W = pad * 2 + 3 * tile + 2 * gap
        H = top + 3 * tile + 2 * gap + 64
        img = self._vgrad(W, H, (36, 30, 18), (18, 14, 8)).convert("RGBA")
        d = ImageDraw.Draw(img, "RGBA")
        d.rounded_rectangle([8, 8, W - 8, H - 8], radius=20, outline=(245, 197, 24, 210), width=4)
        d.text((W / 2, 18), "FLO  RUBBELLOS", font=self._font(30), fill=(245, 197, 24), anchor="ma")
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
                self._slot_symbol(d, x + tile / 2, y + tile / 2, tile * 0.3, key)
            else:
                d.rounded_rectangle([x, y, x + tile, y + tile], radius=14, fill=(118, 122, 132),
                                    outline=(80, 84, 94), width=3)
                d.text((x + tile / 2, y + tile / 2), "?", font=self._font(52),
                       fill=(70, 74, 84), anchor="mm")
        if show_result:
            by = top + 3 * tile + 2 * gap + 12
            if win > 0:
                self._pill_c(d, W / 2, by, f"GEWINN  +{win}", 26, (46, 204, 113), (8, 28, 16))
            else:
                self._pill_c(d, W / 2, by, "leider kein Gewinn", 22, (70, 74, 92), (228, 230, 238))
        if sparkle and win_rows:
            # Funkeln entlang der Gewinn-Reihen.
            for r in win_rows:
                ry = top + r * (tile + gap) + tile / 2
                for sx in (pad + 8, pad + tile + gap / 2, W - pad - 8,
                           pad + 2 * tile + 1.5 * gap):
                    self._sparkle(d, sx, ry - tile * 0.42, 9, (255, 240, 170))
                    self._sparkle(d, sx + 14, ry + tile * 0.38, 7, (255, 250, 210))
        return img


    def scratch_card_anim(self, keys, win_rows, win):
        """Rubbellos als GIF: die 9 Felder werden nacheinander freigerubbelt;
        Gewinn-Reihen funkeln, grosse Gewinne bekommen Konfetti."""
        frames = [self._scratch_img(keys, i, win_rows, win, show_result=False)
                  for i in range(0, 9)]
        durations = [190] * 9
        final = self._scratch_img(keys, 9, win_rows, win, show_result=True)
        if win > 0:
            frames.append(self._flash(final, (255, 240, 160), 55))
            durations.append(100)
            glitzer = self._scratch_img(keys, 9, win_rows, win, show_result=True, sparkle=True)
            if win >= 500:
                for ct in (0.3, 0.65):
                    conf = glitzer.copy()
                    self._confetti(conf, ct, seed=23)
                    frames.append(conf)
                    durations.append(140)
            else:
                frames.append(glitzer)
                durations.append(260)
            frames.append(self._scratch_img(keys, 9, win_rows, win, show_result=True,
                                       sparkle=True))
            durations.append(4500)
        else:
            frames.append(final)
            durations.append(4500)
        return self._gif(frames, durations)


    # === Casino-Statistik-Karte ================================================
    def casino_stats_card(self, name, avatar, stats):
        """Persoenliche Casino-Bilanz: Kopf mit Avatar+Name, sechs Kennzahlen
        (inkl. Brutto-Gewonnen/-Verloren) und ein Balken je Spiel (Anzahl
        Runden, Netto eingefaerbt).

        ``stats``: {games, wagered, payout, best_win, won, lost,
                    per: {spiel: {n, net}}}
        """
        per = dict(stats.get("per") or {})
        rows = sorted(per.items(), key=lambda kv: kv[1].get("n", 0), reverse=True)
        W = 900
        head, row_h = 242, 52
        H = head + max(1, len(rows)) * row_h + 40
        img = self._vgrad(W, H, (26, 24, 40), (12, 11, 20)).convert("RGBA")
        d = ImageDraw.Draw(img, "RGBA")
        d.rounded_rectangle([6, 6, W - 6, H - 6], radius=18, outline=(48, 53, 63), width=2)

        # Avatar + Name
        AD = 84
        ax, ay = 34, 28
        circ = None
        if avatar:
            try:
                im = ImageOps.fit(Image.open(io.BytesIO(avatar)).convert("RGB"),
                                  (AD, AD), method=self._RESAMPLE)
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
        safe = self._clean_text(name)[:26] or "Spieler"
        d.text((ax + AD + 24, ay + 4), safe, font=self._font(38), fill=self._WHITE)
        d.text((ax + AD + 24, ay + 54), "CASINO-BILANZ", font=self._font(17),
               fill=(150, 155, 168))

        # Kennzahlen: zwei Zeilen a drei Werte. Oben die Klassiker, unten die
        # Brutto-Summen (wie viel insgesamt gewonnen bzw. verloren wurde) -
        # fehlen die Felder (Alt-Profil), werden sie aus dem Netto abgeleitet.
        def _tsd(n):
            return f"{n:,}".replace(",", ".")

        net = int(stats.get("payout", 0)) - int(stats.get("wagered", 0))
        won = int(stats.get("won", max(net, 0)))
        lost = int(stats.get("lost", max(-net, 0)))
        net_col = self._GREEN if net >= 0 else self._RED_HOT
        kennz_zeilen = [
            [
                ("RUNDEN", _tsd(stats.get("games", 0)), self._WHITE),
                ("EINSATZ", _tsd(stats.get("wagered", 0)), self._WHITE),
                ("NETTO", f"{'+' if net >= 0 else ''}{_tsd(net)}", net_col),
            ],
            [
                ("GEWONNEN", f"+{_tsd(won)}" if won else "0", self._GREEN),
                ("VERLOREN", f"-{_tsd(lost)}" if lost else "0", self._RED_HOT),
                ("BESTER GEWINN", f"+{_tsd(stats.get('best_win', 0))}", self._GOLD),
            ],
        ]
        seg = (W - 68) // 3
        for zi, zeile in enumerate(kennz_zeilen):
            ly, vy = 124 + zi * 66, 144 + zi * 66
            for i, (label, val, col) in enumerate(zeile):
                sx = 34 + i * seg
                d.text((sx, ly), label, font=self._font(15), fill=(120, 126, 140))
                d.text((sx, vy), val, font=self._font(26), fill=col)

        # Balken je Spiel (Anzahl Runden relativ zum Maximum, Netto rechts)
        if rows:
            peak = max(v.get("n", 0) for _g, v in rows) or 1
            y = head + 8
            for game, v in rows:
                n, gnet = int(v.get("n", 0)), int(v.get("net", 0))
                col = self._GREEN if gnet >= 0 else self._RED_HOT
                d.text((34, y + 2), game.upper()[:14], font=self._font(18), fill=(200, 205, 218))
                self._hbar(d, 220, y + 4, W - 470, 18, n / peak, (87, 148, 242))
                d.text((W - 232, y + 2), f"{n}×", font=self._font(18), fill=(150, 155, 168))
                d.text((W - 34, y + 2), f"{'+' if gnet >= 0 else ''}{gnet}",
                       font=self._font(18), fill=col, anchor="ra")
                y += row_h
        else:
            d.text((34, head + 10), "Noch keine Runden gespielt.", font=self._font(20),
                   fill=(150, 155, 168))
        return self._png(img)


    def handel_card(self, name, avatar, stats,
                    balance = 0):
        """Coin-Handelsbuch als Bild: Kopf mit Avatar+Name+Kontostand, vier
        Kennzahlen, Netto-Chart der letzten 14 Tage, Aufschluesselung nach
        Quelle und die juengsten Einzelbuchungen.

        ``stats``: {in, out, n, by: {quelle: {in, out, n}}, days:
                    {"YYYY-MM-DD": {in, out}}, last: [{t, src, amt, bal}]}
        """
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        import os as _os

        def _tsd(n):
            return f"{n:,}".replace(",", ".")

        by = dict(stats.get("by") or {})
        srcs = sorted(by.items(), key=lambda kv: kv[1].get("n", 0), reverse=True)[:8]
        letzte = list(stats.get("last") or [])[-6:][::-1]

        W = 900
        head = 176
        chart_h = 200          # Titel + Balkenfeld + Tages-Labels
        src_h = len(srcs) * 44 + (10 if srcs else 0)
        last_h = (30 + len(letzte) * 30 + 6) if letzte else 0
        H = head + chart_h + src_h + last_h + 34
        img = self._vgrad(W, H, (23, 28, 38), (11, 13, 20)).convert("RGBA")
        d = ImageDraw.Draw(img, "RGBA")
        d.rounded_rectangle([6, 6, W - 6, H - 6], radius=18, outline=(48, 53, 63), width=2)

        # Avatar + Name (gleiches Muster wie die Casino-Bilanz)
        AD = 84
        ax, ay = 34, 28
        circ = None
        if avatar:
            try:
                im = ImageOps.fit(Image.open(io.BytesIO(avatar)).convert("RGB"),
                                  (AD, AD), method=self._RESAMPLE)
                mask = Image.new("L", (AD, AD), 0)
                ImageDraw.Draw(mask).ellipse([0, 0, AD - 1, AD - 1], fill=255)
                circ = im.convert("RGBA")
                circ.putalpha(mask)
            except Exception:  # noqa: BLE001
                circ = None
        if circ is not None:
            img.paste(circ, (ax, ay), circ)
            d.ellipse([ax - 4, ay - 4, ax + AD + 3, ay + AD + 3],
                      outline=(87, 148, 242), width=4)
        safe = self._clean_text(name)[:26] or "Spieler"
        d.text((ax + AD + 24, ay + 4), safe, font=self._font(38), fill=self._WHITE)
        d.text((ax + AD + 24, ay + 54), "COIN-HANDELSBUCH", font=self._font(17),
               fill=(150, 155, 168))
        d.text((W - 34, ay + 8), "KONTOSTAND", font=self._font(15),
               fill=(120, 126, 140), anchor="ra")
        d.text((W - 34, ay + 30), _tsd(int(balance)), font=self._font(28),
               fill=self._GOLD, anchor="ra")

        # Kennzahlen-Zeile
        ein, aus = int(stats.get("in", 0)), int(stats.get("out", 0))
        net = ein - aus
        net_col = self._GREEN if net >= 0 else self._RED_HOT
        kennz = [
            ("TRANSAKTIONEN", _tsd(stats.get("n", 0)), self._WHITE),
            ("EINGENOMMEN", f"+{_tsd(ein)}" if ein else "0", self._GREEN),
            ("AUSGEGEBEN", f"-{_tsd(aus)}" if aus else "0", self._RED_HOT),
            ("NETTO", f"{'+' if net >= 0 else ''}{_tsd(net)}", net_col),
        ]
        seg = (W - 68) // len(kennz)
        for i, (label, val, col) in enumerate(kennz):
            sx = 34 + i * seg
            d.text((sx, 124), label, font=self._font(15), fill=(120, 126, 140))
            d.text((sx, 144), val, font=self._font(26), fill=col)

        # Netto-Chart: ein Balken je Tag (letzte 14 Tage), gruen hoch / rot runter.
        d.text((34, head + 6), "NETTO PRO TAG – LETZTE 14 TAGE",
               font=self._font(15), fill=(120, 126, 140))
        days = dict(stats.get("days") or {})
        tz = ZoneInfo(_os.getenv("TIMEZONE", "Europe/Berlin"))
        heute = datetime.now(tz).date()
        reihe = []
        for i in range(13, -1, -1):
            tag = heute - timedelta(days=i)
            e = days.get(tag.strftime("%Y-%m-%d")) or {}
            reihe.append((tag, int(e.get("in", 0)) - int(e.get("out", 0))))
        peak = max((abs(v) for _t, v in reihe), default=0) or 1
        cy = head + 34 + 66                      # Nulllinie
        half = 62                                # max. Balkenhoehe je Richtung
        d.line([(34, cy), (W - 34, cy)], fill=(58, 63, 74), width=2)
        slot = (W - 68) / 14
        for i, (tag, v) in enumerate(reihe):
            bx = 34 + i * slot + slot / 2
            bw = min(36, slot - 12)
            bh = int(round(abs(v) / peak * half))
            if v > 0:
                d.rounded_rectangle([bx - bw / 2, cy - max(bh, 3), bx + bw / 2, cy - 1],
                                    radius=3, fill=self._GREEN)
            elif v < 0:
                d.rounded_rectangle([bx - bw / 2, cy + 1, bx + bw / 2, cy + max(bh, 3)],
                                    radius=3, fill=self._RED_HOT)
            else:
                d.line([(bx - bw / 2, cy), (bx + bw / 2, cy)], fill=(90, 96, 108), width=2)
            if i % 2 == 0:                       # jeden 2. Tag beschriften
                d.text((bx, cy + half + 10), tag.strftime("%d.%m."),
                       font=self._font(13), fill=(120, 126, 140), anchor="ma")

        # Aufschluesselung nach Quelle (Balken = Anzahl, rechts das Netto)
        y = head + chart_h + 10
        if srcs:
            src_peak = max(v.get("n", 0) for _s, v in srcs) or 1
            for src, v in srcs:
                snet = int(v.get("in", 0)) - int(v.get("out", 0))
                col = self._GREEN if snet >= 0 else self._RED_HOT
                d.text((34, y + 2), src.upper()[:14], font=self._font(18),
                       fill=(200, 205, 218))
                self._hbar(d, 220, y + 4, W - 470, 18, v.get("n", 0) / src_peak,
                           (87, 148, 242))
                d.text((W - 232, y + 2), f"{v.get('n', 0)}×", font=self._font(18),
                       fill=(150, 155, 168))
                d.text((W - 34, y + 2), f"{'+' if snet >= 0 else ''}{_tsd(snet)}",
                       font=self._font(18), fill=col, anchor="ra")
                y += 44

        # Juengste Einzelbuchungen (neueste zuerst)
        if letzte:
            d.text((34, y + 4), "LETZTE TRANSAKTIONEN", font=self._font(15),
                   fill=(120, 126, 140))
            y += 30
            for e in letzte:
                amt = int(e.get("amt", 0))
                col = self._GREEN if amt >= 0 else self._RED_HOT
                d.text((34, y + 2), str(e.get("t", "")), font=self._font(16),
                       fill=(150, 155, 168))
                d.text((190, y + 2), str(e.get("src", ""))[:16], font=self._font(16),
                       fill=(200, 205, 218))
                d.text((W - 214, y + 2), f"{'+' if amt >= 0 else ''}{_tsd(amt)}",
                       font=self._font(16), fill=col, anchor="ra")
                d.text((W - 34, y + 2), f"Konto: {_tsd(int(e.get('bal', 0)))}",
                       font=self._font(16), fill=(120, 126, 140), anchor="ra")
                y += 30
        return self._png(img)


    # === HiLo-Karte ============================================================
    def hilo_card(self, rank, suit, *, streak, mult,
                  state = ""):
        """Hoeher/Tiefer: grosse Spielkarte + Serie/Multiplikator.
        ``state``: '' (laeuft) / 'win' (Cashout) / 'lose'."""
        W, H = 460, 300
        img = self._vgrad(W, H, (24, 30, 46), (11, 14, 22)).convert("RGBA")
        d = ImageDraw.Draw(img, "RGBA")
        rand = {"win": (46, 204, 113), "lose": (231, 76, 60)}.get(state, (245, 197, 24))
        d.rounded_rectangle([6, 6, W - 6, H - 6], radius=18, outline=rand, width=4)
        d.text((26, 20), "HÖHER / TIEFER", font=self._font(24), fill=(245, 197, 24))
        tile = self._card_tile(rank, suit)
        img.paste(tile, (44, 76), tile)
        x0 = 230
        d.text((x0, 84), "SERIE", font=self._font(16), fill=(140, 146, 160))
        d.text((x0, 106), f"{streak} richtig", font=self._font(26), fill=self._WHITE)
        d.text((x0, 152), "MULTIPLIKATOR", font=self._font(16), fill=(140, 146, 160))
        d.text((x0, 174), f"×{mult:.2f}", font=self._font(34), fill=(245, 197, 24))
        if state == "win":
            self._pill(d, x0, 224, "CASHOUT!", 20, (46, 204, 113), (8, 28, 16))
        elif state == "lose":
            self._pill(d, x0, 224, "VERLOREN", 20, (231, 76, 60), (30, 8, 8))
        else:
            d.text((x0, 226), "gleiche Karte = verloren", font=self._font(15),
                   fill=(120, 126, 140))
        return self._png(img)


    # === Hilfe-Karten ==========================================================
    def help_card(self, title, accent, entries,
                  *, subtitle = ""):
        """Befehls-Uebersicht als Bild: pro Zeile links der Befehl als Code-Pille,
        rechts die Kurzbeschreibung. ``entries``: [(befehl, beschreibung)] oder
        [(befehl, beschreibung, rgb-farbe)] fuer eigene Pillen-Farben."""
        W = 900
        head, row_h = 92, 54
        H = head + len(entries) * row_h + 24
        img = self._vgrad(W, H, (22, 24, 33), (12, 13, 19)).convert("RGBA")
        d = ImageDraw.Draw(img, "RGBA")
        d.rounded_rectangle([6, 6, W - 7, H - 7], radius=18, outline=accent, width=3)

        d.text((34, 24), self._clean_text(title), font=self._font(36), fill=self._WHITE)
        tw = d.textlength(self._clean_text(title), font=self._font(36))
        d.line([(34, 70), (34 + tw, 70)], fill=accent, width=3)
        if subtitle:
            d.text((W - 34, 40), subtitle, font=self._font(17), fill=(140, 146, 160),
                   anchor="rm")

        cf, df = self._font(19), self._font(19)
        y = head
        for entry in entries:
            cmd, desc = entry[0], entry[1]
            col = entry[2] if len(entry) > 2 and entry[2] else accent
            # Zebra-Hintergrund fuer Lesbarkeit (opak - Alpha wuerde beim
            # RGB-Export zu Weiss werden)
            if (y - head) // row_h % 2 == 1:
                d.rounded_rectangle([16, y - 4, W - 16, y + row_h - 10], radius=10,
                                    fill=(30, 33, 44))
            pw = int(d.textlength(cmd, font=cf)) + 26
            d.rounded_rectangle([34, y, 34 + pw, y + 34], radius=9,
                                fill=(10, 11, 16), outline=col, width=2)
            d.text((34 + 13, y + 7), cmd, font=cf, fill=(235, 238, 245))
            d.text((34 + pw + 20, y + 8), self._clean_text(desc), font=df,
                   fill=(165, 172, 186))
            y += row_h
        return self._png(img)


    # === Woerter-Top-Liste =====================================================
    def words_card(self, rows, *, total_words = 0, total_count = 0):
        """Top-Woerter des Servers als Balken-Karte. ``rows``: [(wort, anzahl), ...]"""
        W = 900
        head, row_h = 118, 46
        n = max(1, len(rows))
        H = head + n * row_h + 42
        img = self._vgrad(W, H, (22, 28, 40), (11, 14, 22)).convert("RGBA")
        d = ImageDraw.Draw(img, "RGBA")
        d.rounded_rectangle([6, 6, W - 6, H - 6], radius=18, outline=(48, 53, 63), width=2)
        hf = self._font(42)
        d.text((34, 26), "FLO", font=hf, fill=self._WHITE)
        d.text((34 + d.textlength("FLO ", font=hf), 26), "WÖRTER", font=hf, fill=self._GOLD)
        if total_words:
            d.text((W - 34, 44), f"{total_words:,}".replace(",", ".") + " Wörter erfasst",
                   font=self._font(18), fill=(150, 158, 188), anchor="rm")
        d.line([(34, head - 16), (W - 34, head - 16)], fill=(54, 60, 86), width=2)

        if rows:
            peak = max(c for _w, c in rows) or 1
            y = head
            medal = {0: (250, 222, 42), 1: (176, 176, 184), 2: (205, 127, 50)}
            for i, (wort, count) in enumerate(rows):
                col = medal.get(i, (87, 148, 242))
                d.text((34, y + 6), f"{i + 1}.", font=self._font(20),
                       fill=medal.get(i, (130, 136, 150)))
                word_txt = self._clean_text(str(wort))[:18] or "?"
                d.text((84, y + 6), word_txt, font=self._font(22), fill=self._WHITE)
                self._hbar(d, 320, y + 10, W - 560, 16, count / peak, col)
                d.text((W - 34, y + 6), f"{count:,}".replace(",", "."), font=self._font(21),
                       fill=(200, 205, 218), anchor="ra")
                y += row_h
        else:
            d.text((34, head + 8), "Noch keine Wörter gezählt.", font=self._font(20),
                   fill=(150, 155, 168))
        return self._png(img)


# --- Modul-Fassade --------------------------------------------------------
# Eine geteilte Instanz + Aliase, damit die bisherigen Modul-Funktionen
# (bot.py, casino.py, economy.py, games.py, words.py, food.py, media.py)
# unveraendert weiter funktionieren.
instance = Render()

SLOT_KEYS = Render.SLOT_KEYS

blackjack_table = instance.blackjack_table
blackjack_table_anim = instance.blackjack_table_anim
casino_stats_card = instance.casino_stats_card
handel_card = instance.handel_card
coin_flip = instance.coin_flip
coin_flip_anim = instance.coin_flip_anim
crash_chart = instance.crash_chart
crash_chart_anim = instance.crash_chart_anim
dice_roll = instance.dice_roll
dice_roll_anim = instance.dice_roll_anim
help_card = instance.help_card
hilo_card = instance.hilo_card
keno_grid = instance.keno_grid
keno_grid_anim = instance.keno_grid_anim
level_card = instance.level_card
nutrition_card = instance.nutrition_card
quote_card = instance.quote_card
roulette_wheel = instance.roulette_wheel
roulette_wheel_anim = instance.roulette_wheel_anim
scratch_card_anim = instance.scratch_card_anim
shop_banner = instance.shop_banner
slot_machine = instance.slot_machine
slot_machine_anim = instance.slot_machine_anim
wheel_fortune_anim = instance.wheel_fortune_anim
words_card = instance.words_card
