#!/usr/bin/env python3
"""
Bilan hebdomadaire de l'affluence — Restaurant Porte Océane

Chaque vendredi soir : génère une image (même construction que le bot météo : une
carte par jour, de lundi à vendredi, thème aubergine du bot menu) et l'envoie sur
Discord. Pour chaque jour :
    • le nombre total de repas (et le % de la jauge) ;
    • le créneau de 15 min où il y a eu le plus de repas (+ le plus calme) ;
    • le nombre de repas qui avait été estimé le matin, et l'écart avec le réel.

Tout vient de data/history.json (alimenté par affluence_bot.py).

    python bilan.py            # envoi (une seule fois par semaine)
    DRY_RUN=1 python bilan.py  # écrit bilan_preview.png sans rien envoyer
"""
import json
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

try:
    import numpy as np
    NUMPY_OK = True
except ImportError:
    NUMPY_OK = False

import stats

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
BOT_NAME    = "Le GOAT du Crous"
DRY_RUN     = os.getenv("DRY_RUN", "0") in ("1", "true", "True")
FORCE_RUN   = os.getenv("FORCE_RUN", "0") in ("1", "true", "True")   # renvoie même si déjà envoyé
TZ          = ZoneInfo("Europe/Paris")

_base_dir = os.getenv("GITHUB_WORKSPACE") or os.path.dirname(os.path.abspath(__file__))
FONT_DIR  = os.path.join(_base_dir, "fonts")
IMG_PATH  = os.path.join(_base_dir, "bilan_preview.png") if DRY_RUN else "/tmp/bilan_semaine.png"

JAUGE_DEFAUT = 1100

# ── Palette (celle du bot menu) ───────────────────────────────────────────────
VERT   = (121, 214, 150)
ORANGE = (255, 150, 60)
CORAIL = (255, 111, 90)
LILAS  = (196, 150, 255)
JAUNE  = (255, 196, 84)
BLEU   = (110, 180, 255)
GRIS   = (150, 145, 165)
BLANC  = (255, 255, 255)

JOURS_COURT = ["LUN", "MAR", "MER", "JEU", "VEN"]
JOURS_LONG  = ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]

# ══════════════════════════════════════════════════════════════════════════════
# DONNÉES DE LA SEMAINE
# ══════════════════════════════════════════════════════════════════════════════

def build_row(d: date, rec) -> dict:
    row = {"date": d, "data": False}
    if not rec or not rec.get("samples"):
        return row
    total = stats.day_total(rec)
    if not total:
        return row
    profil = stats.day_profile(rec)
    peak, low = stats.peak_low(profil)
    fc = rec.get("forecast") or {}
    row.update(
        data=True, total=total, jauge=int(rec.get("jauge") or JAUGE_DEFAUT),
        closed_at=rec.get("closed_at"), profil=profil, peak=peak, low=low,
        forecast=fc.get("total"),
    )
    return row


def week_rows(history: dict, monday: date) -> list:
    return [build_row(monday + timedelta(days=i), history["days"].get((monday + timedelta(days=i)).isoformat()))
            for i in range(5)]


def week_summary(rows: list) -> dict:
    ok = [r for r in rows if r["data"]]
    s = {"n": len(ok), "total": sum(r["total"] for r in ok)}
    if ok:
        s["moyenne"] = round(s["total"] / len(ok))
        best = max(ok, key=lambda r: r["total"])
        s["best_day"] = JOURS_LONG[best["date"].weekday()]
    errs = [abs(r["total"] - r["forecast"]) / r["forecast"] * 100
            for r in ok if r["forecast"]]
    s["ecart"] = round(sum(errs) / len(errs), 1) if errs else None
    return s

# ══════════════════════════════════════════════════════════════════════════════
# DESSIN
# ══════════════════════════════════════════════════════════════════════════════

def _F(name, size):
    try:
        return ImageFont.truetype(os.path.join(FONT_DIR, f"Poppins-{name}.ttf"), size)
    except Exception:
        return ImageFont.load_default()


def _gradient_bg(w, h):
    top, bot = (26, 20, 46), (64, 26, 54)
    if NUMPY_OK:
        ys = np.linspace(0, 1, h).reshape(h, 1, 1)
        grad = np.array(top).reshape(1, 1, 3) + (np.array(bot) - np.array(top)).reshape(1, 1, 3) * ys
        return Image.fromarray(np.repeat(grad, w, axis=1).astype("uint8"), "RGB")
    img = Image.new("RGB", (w, h), top)
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    return img


def _glow(img, cx, cy, r, color, alpha):
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(ov).ellipse([cx - r, cy - r, cx + r, cy + r], fill=color + (alpha,))
    ov = ov.filter(ImageFilter.GaussianBlur(r / 2.2))
    return Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")


def _card(base, x, y, w, h, radius=34):
    shadow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle([x, y + 8, x + w, y + h + 8], radius=radius, fill=(0, 0, 0, 90))
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))
    base = Image.alpha_composite(base.convert("RGBA"), shadow)
    card = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(card).rounded_rectangle([x, y, x + w, y + h], radius=radius, fill=(255, 255, 255, 14))
    return Image.alpha_composite(base, card).convert("RGB")


def _fr(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _signed(n: int) -> str:
    return f"{n:+d}".replace("-", "−")


def _tier_color(total: int, jauge: int, closed: bool):
    if closed or total >= jauge:
        return LILAS
    if total >= 1000:
        return CORAIL
    if total >= 500:
        return ORANGE
    return VERT


def _descr(total: int, jauge: int, closed: bool) -> str:
    if closed or total >= jauge:
        return "Complet"
    pct = total / jauge
    return "Très chargé" if pct >= 0.8 else "Animé" if pct >= 0.5 else "Calme"


def _text(d, xy, txt, font, fill, anchor="la"):
    d.text(xy, txt, font=font, fill=fill, anchor=anchor)


def _bar(d, x, y, w, frac, color, h=8):
    d.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=(255, 255, 255, 215))
    fw = max(h, int(w * max(0.0, min(1.0, frac)))) if frac > 0 else 0
    if fw:
        d.rounded_rectangle([x, y, x + fw, y + h], radius=h // 2, fill=color + (255,))


def _draw_card(base, x, y, w, h, row, max_flow):
    base = _card(base, x, y, w, h)
    ov = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    ix, iw = x + 18, w - 36
    wd = row["date"].weekday()
    date_txt = f"{row['date'].day} {MOIS[row['date'].month - 1]}"
    f_lab, f_sm, f_val = _F("SemiBold", 12), _F("Regular", 15), _F("Bold", 24)
    line = (255, 255, 255, 70)
    lab = (255, 255, 255, 120)

    def chip(color):
        font = _F("SemiBold", 17)
        bb = d.textbbox((0, 0), JOURS_COURT[wd], font=font)
        cw = bb[2] - bb[0] + 26
        d.rounded_rectangle([ix, y + 18, ix + cw, y + 54], radius=18, fill=color + (240,))
        d.text((ix + 13, y + 36), JOURS_COURT[wd], font=font, fill=(20, 14, 24), anchor="lm")

    if not row["data"]:
        chip(GRIS)
        _text(d, (ix, y + 60), date_txt, f_sm, lab)
        _text(d, (x + w / 2, y + h / 2 - 10), "Pas de service", _F("Medium", 19), (255, 255, 255, 150), "mm")
        _text(d, (x + w / 2, y + h / 2 + 18), "ou aucune donnée", f_sm, lab, "mm")
        return Image.alpha_composite(base.convert("RGBA"), ov).convert("RGB")

    total, jauge = row["total"], row["jauge"]
    closed = bool(row["closed_at"])
    col = _tier_color(total, jauge, closed)
    chip(col)
    _text(d, (ix, y + 60), date_txt, f_sm, lab)

    # mini-histogramme des 8 créneaux de 15 min (pic en corail, creux en vert)
    base_y, max_h = y + 158, 62
    n = stats.N_SLOTS
    bw = 24
    gap = (iw - n * bw) / (n - 1)
    for i, flow in enumerate(row["profil"]):
        bx = ix + i * (bw + gap)
        if flow is None:
            d.rounded_rectangle([bx, base_y - 3, bx + bw, base_y], radius=2, fill=(255, 255, 255, 40))
            continue
        bh = max(5, int(flow / max_flow * max_h)) if max_flow else 5
        c = CORAIL + (255,) if i == row["peak"] else VERT + (255,) if i == row["low"] else (255, 255, 255, 85)
        d.rounded_rectangle([bx, base_y - bh, bx + bw, base_y], radius=5, fill=c)
    _text(d, (x + w / 2, y + 182), _descr(total, jauge, closed), _F("Regular", 16), BLANC + (255,), "mm")

    d.line([(ix, y + 208), (ix + iw, y + 208)], fill=line, width=1)

    # repas + % de la jauge
    _text(d, (ix, y + 226), "REPAS SERVIS", f_lab, lab)
    _text(d, (ix + iw, y + 226), "JAUGE", f_lab, lab, "ra")
    f_big = _F("Bold", 40)
    _text(d, (ix, y + 240), _fr(total), f_big, col + (255,))
    pct = 100 if total >= jauge else min(99, total * 100 // jauge)
    _text(d, (ix + iw, y + 240), f"{pct}%", f_big, col + (255,), "ra")
    sous = f"sur {_fr(jauge)}"
    if closed:
        sous += f" · fermé à {row['closed_at'][:5].replace(':', 'h')}"
    _text(d, (x + w / 2, y + 318), sous, _F("Regular", 14), (255, 255, 255, 200), "mm")

    d.line([(ix, y + 338), (ix + iw, y + 338)], fill=line, width=1)

    # pic / creux
    prof = row["profil"]
    for top, label, idx, color in ((356, "PIC DE FRÉQUENTATION", row["peak"], CORAIL),
                                   (436, "LE PLUS CALME", row["low"], VERT)):
        _text(d, (ix, y + top), label, f_lab, lab)
        if idx is None:
            _text(d, (ix, y + top + 16), "—", f_val, (255, 255, 255, 120))
            continue
        _text(d, (ix + iw, y + top), f"{_fr(prof[idx])} repas", _F("Medium", 13), (255, 255, 255, 150), "ra")
        _text(d, (ix, y + top + 16), stats.slot_label(idx), f_val, color + (255,))
        _bar(d, ix, y + top + 56, iw, prof[idx] / max_flow if max_flow else 0, color)

    d.line([(ix, y + 520), (ix + iw, y + 520)], fill=line, width=1)

    # prévision vs réel
    _text(d, (ix, y + 538), "PRÉVISION", f_lab, lab)
    fc = row["forecast"]
    if fc:
        _text(d, (ix, y + 554), f"≈ {_fr(fc)}", f_val, BLANC + (255,))
        _text(d, (ix + iw, y + 562), "repas estimés", f_sm, lab, "ra")
        ecart = total - fc
        rel = abs(ecart) / fc * 100
        label, pc = (("PRÉCIS", VERT) if rel <= 5 else ("CORRECT", ORANGE) if rel <= 15 else ("ÉCART", CORAIL))
        txt = f"{_signed(ecart)} · {label}"
    else:
        txt, pc = "PAS DE PRÉVISION", GRIS
        _text(d, (ix, y + 554), "—", f_val, (255, 255, 255, 120))
    font = _F("SemiBold", 14)
    bb = d.textbbox((0, 0), txt, font=font)
    pw, ph = bb[2] - bb[0] + 24, 30
    d.rounded_rectangle([ix, y + 596, ix + pw, y + 596 + ph], radius=ph // 2, fill=pc + (240,))
    d.text((ix + 12, y + 596 + ph / 2), txt, font=font, fill=(20, 14, 24), anchor="lm")

    return Image.alpha_composite(base.convert("RGBA"), ov).convert("RGB")


def monday_label(monday: date) -> str:
    fri = monday + timedelta(days=4)
    if monday.month == fri.month:
        return f"Du {monday.day} au {fri.day} {MOIS[fri.month - 1]}"
    return f"Du {monday.day} {MOIS[monday.month - 1]} au {fri.day} {MOIS[fri.month - 1]}"


def render(rows: list, summary: dict, monday: date, updated: datetime) -> Image.Image:
    W, H, PAD = 1760, 960, 44
    CARD_W, GAP, CARD_Y, CARD_H = 317, 21, 176, 640

    img = _gradient_bg(W, H)
    img = _glow(img, 1500, 120, 340, (255, 111, 90), 38)
    img = _glow(img, 160, 880, 380, (110, 60, 160), 55)

    # en-tête
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    d.text((PAD, 44), "Affluence de la semaine", font=_F("Medium", 22), fill=(255, 255, 255, 235))
    d.text((PAD, 70), monday_label(monday), font=_F("Black", 54), fill=(255, 255, 255, 255))
    f = _F("SemiBold", 17)
    bb = d.textbbox((0, 0), "LE GOAT DU CROUS", font=f)
    pw = bb[2] - bb[0] + 30
    d.rounded_rectangle([W - PAD - pw, 44, W - PAD, 85], radius=21, fill=(255, 255, 255, 38))
    d.text((W - PAD - pw / 2, 64.5), "LE GOAT DU CROUS", font=f, fill=(255, 255, 255, 255), anchor="mm")
    img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")

    # cartes
    flows = [v for r in rows if r["data"] for v in r["profil"] if v is not None]
    max_flow = max(flows) if flows else 1
    for i, row in enumerate(rows):
        img = _draw_card(img, PAD + i * (CARD_W + GAP), CARD_Y, CARD_W, CARD_H, row, max_flow)

    # bandeau récapitulatif
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    stats_row = [("TOTAL SEMAINE", f"{_fr(summary['total'])} repas" if summary["n"] else "—", BLANC)]
    if summary["n"]:
        stats_row += [("MOYENNE / JOUR", f"{_fr(summary['moyenne'])} repas", VERT),
                      ("JOUR LE PLUS CHARGÉ", summary["best_day"].capitalize(), CORAIL)]
    ec = summary.get("ecart")
    stats_row.append(("ÉCART MOYEN DES PRÉVISIONS", f"{str(ec).replace('.', ',')} %" if ec is not None else "—", JAUNE))
    sx = PAD + 2
    for label, value, color in stats_row:
        d.text((sx, 838), label, font=_F("SemiBold", 12), fill=(255, 255, 255, 120))
        d.text((sx, 856), value, font=_F("Bold", 24), fill=color + (255,))
        sx += 380
    # pied de page
    d.line([(PAD, H - 62), (W - PAD, H - 62)], fill=(255, 255, 255, 70), width=1)
    d.text((PAD, H - 50), "Restaurant Porte Océane · restos.lescrous.fr · prévision = moyenne pondérée des derniers jours",
           font=_F("Regular", 15), fill=(255, 255, 255, 120))
    d.text((W - PAD, H - 50), f"Mis à jour {updated:%H:%M}", font=_F("Regular", 15),
           fill=(255, 255, 255, 120), anchor="ra")
    return Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")

# ══════════════════════════════════════════════════════════════════════════════
# ENVOI
# ══════════════════════════════════════════════════════════════════════════════

def send_image(path: str, content: str) -> None:
    if DRY_RUN or not WEBHOOK_URL:
        print(f"[DRY-RUN] image enregistrée : {path}\n{content}")
        return
    with open(path, "rb") as f:
        r = requests.post(
            WEBHOOK_URL,
            data={"payload_json": json.dumps({"username": BOT_NAME, "content": content})},
            files={"file": ("bilan_semaine.png", f, "image/png")},
            timeout=60,
        )
    r.raise_for_status()
    print(f"✅ Discord → {r.status_code}")


def main() -> int:
    today = datetime.now(TZ)
    monday = today.date() - timedelta(days=today.weekday())
    week_key = f"{monday.isocalendar()[0]}-W{monday.isocalendar()[1]:02d}"

    history = stats.load_history()
    if history["meta"].get("bilan_sent") == week_key and not FORCE_RUN:
        print(f"Bilan de la semaine {week_key} déjà envoyé.")
        return 0

    rows = week_rows(history, monday)
    summary = week_summary(rows)
    if not summary["n"]:
        print("Aucune donnée cette semaine : pas de bilan.")
        return 0

    img = render(rows, summary, monday, today)
    img.save(IMG_PATH, "PNG", optimize=True)
    send_image(IMG_PATH, f"📊 **Bilan de la semaine** — {monday_label(monday).lower()}")

    if not DRY_RUN:
        history["meta"]["bilan_sent"] = week_key
        stats.save_history(history)
    return 0


if __name__ == "__main__":
    sys.exit(main())
