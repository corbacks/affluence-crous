#!/usr/bin/env python3
"""
Statistiques de fréquentation — Restaurant Porte Océane

Ce module ne parle ni à Discord ni à l'API : il stocke l'historique des repas et
en tire des prévisions. Il est partagé par `affluence_bot.py` (service du midi)
et `bilan.py` (image du vendredi soir).

Historique : `data/history.json`, versionné dans le dépôt (commité par le workflow
à la fin de chaque service). Un enregistrement par jour :

    "2026-10-20": {
        "samples":   [["11:31:02", 4], ["11:33:02", 11], ...],   # (heure, repas cumulés)
        "jauge":     1100,
        "closed_at": "12:41:07" | null,       # heure où la jauge a été atteinte
        "final":     874 | null,              # repas à 13h30
        "forecast":  {...} | null             # prévision faite le matin même
    }

Créneaux : le service 11h30 → 13h30 est découpé en 8 créneaux de 15 min. Le nombre
de repas d'un créneau = repas cumulés à la fin − repas cumulés au début (interpolés
linéairement entre deux relevés).
"""
import json
import os
from datetime import date

BASE_DIR     = os.getenv("GITHUB_WORKSPACE") or os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(BASE_DIR, "data", "history.json")

# ── Service ──────────────────────────────────────────────────────────────────
START_MIN = 11 * 60 + 30          # ouverture 11h30 (en minutes depuis minuit)
END_MIN   = 13 * 60 + 30          # fin de service 13h30
SLOT_MIN  = 15                    # taille d'un créneau
N_SLOTS   = (END_MIN - START_MIN) // SLOT_MIN      # 8

# ── Réglages des stats ───────────────────────────────────────────────────────
MAX_GAP     = 10 * 60             # trou max (s) entre deux relevés pour interpoler
RECORD_FROM = 11 * 3600           # on n'enregistre que dans cette plage (s)
RECORD_TO   = 14 * 3600
MIN_TOTAL   = 50                  # un « jour » à moins de 50 repas n'entre pas dans les prévisions
LOOKBACK    = 30                  # nombre de jours d'historique pris en compte
SAME_WEEKDAY_WEIGHT = 2.0         # un lundi pèse 2× plus pour prévoir un lundi
MAX_DAYS    = 400                 # taille max de l'historique conservé


# ══════════════════════════════════════════════════════════════════════════════
# OUTILS
# ══════════════════════════════════════════════════════════════════════════════

def hms_to_sec(hms: str) -> int:
    parts = [int(x) for x in hms.split(":")]
    while len(parts) < 3:
        parts.append(0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def slot_label(i: int) -> str:
    """0 → '11h30–11h45', 5 → '12h45–13h00'."""
    a = START_MIN + i * SLOT_MIN
    b = a + SLOT_MIN
    return f"{a // 60}h{a % 60:02d}–{b // 60}h{b % 60:02d}"


# ══════════════════════════════════════════════════════════════════════════════
# HISTORIQUE (lecture / écriture)
# ══════════════════════════════════════════════════════════════════════════════

def load_history(path: str = None) -> dict:
    path = path or HISTORY_FILE
    try:
        with open(path, encoding="utf-8") as f:
            h = json.load(f)
    except (OSError, ValueError):
        h = {}
    if not isinstance(h, dict):
        h = {}
    h.setdefault("meta", {})
    h.setdefault("days", {})
    return h


def save_history(h: dict, path: str = None) -> None:
    """Écrit un jour par ligne : fichier compact, et diffs git lisibles."""
    path = path or HISTORY_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    keys = sorted(h["days"])[-MAX_DAYS:]
    lines = [
        f'  {json.dumps(k)}: {json.dumps(h["days"][k], ensure_ascii=False, separators=(",", ":"))}'
        for k in keys
    ]
    text = (
        "{\n"
        f' "meta": {json.dumps(h["meta"], ensure_ascii=False)},\n'
        ' "days": {\n' + ",\n".join(lines) + "\n }\n}\n"
    )
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def get_day(h: dict, key: str) -> dict:
    day = h["days"].setdefault(key, {})
    day.setdefault("samples", [])
    return day


def record(day: dict, now, repas: int, jauge: int) -> None:
    """Ajoute un relevé (hors plage de service : ignoré)."""
    secs = now.hour * 3600 + now.minute * 60 + now.second
    if not (RECORD_FROM <= secs <= RECORD_TO):
        return
    hms = now.strftime("%H:%M:%S")
    samples = day["samples"]
    if samples and samples[-1][0] == hms:
        return
    samples.append([hms, int(repas)])
    day["jauge"] = int(jauge)


# ══════════════════════════════════════════════════════════════════════════════
# PROFIL D'UNE JOURNÉE
# ══════════════════════════════════════════════════════════════════════════════

def _cum(samples, t: int):
    """Repas cumulés à l'instant t (secondes), interpolés ; None si on n'a pas de données fiables."""
    if t <= START_MIN * 60:
        return 0                                   # le compteur démarre à 0 à l'ouverture
    prev = None
    for s, c in samples:
        if s == t:
            return c
        if s > t:
            if prev is None or s - prev[0] > MAX_GAP:
                return None
            return prev[1] + (c - prev[1]) * (t - prev[0]) / (s - prev[0])
        prev = (s, c)
    if prev is None or t - prev[0] > MAX_GAP:
        return None
    return prev[1]


def day_profile(day: dict) -> list:
    """Repas servis dans chacun des 8 créneaux (None = pas de donnée fiable)."""
    samples = [(hms_to_sec(h), c) for h, c in (day.get("samples") or [])]
    samples.sort()
    closed = hms_to_sec(day["closed_at"]) if day.get("closed_at") else None
    out = []
    for i in range(N_SLOTS):
        a = (START_MIN + i * SLOT_MIN) * 60
        b = a + SLOT_MIN * 60
        if closed is not None and b > closed:
            out.append(None)                       # fermé en cours de créneau : le flux est tronqué
            continue
        ca, cb = _cum(samples, a), _cum(samples, b)
        out.append(None if ca is None or cb is None else max(0, round(cb - ca)))
    return out


def recent_flow(day: dict, now, window: int = 600):
    """Repas servis pendant les `window` dernières secondes (None si pas assez de relevés)."""
    samples = sorted((hms_to_sec(h), c) for h, c in (day.get("samples") or []))
    t = now.hour * 3600 + now.minute * 60 + now.second
    a, b = _cum(samples, t - window), _cum(samples, t)
    return None if a is None or b is None else max(0, round(b - a))


def peak_low(profile: list):
    """(index du créneau le plus chargé, index du plus calme) ; (None, None) sans donnée."""
    known = [i for i, v in enumerate(profile) if v is not None]
    if not known:
        return None, None
    return max(known, key=lambda i: profile[i]), min(known, key=lambda i: profile[i])


def day_total(day: dict):
    """Repas de la journée : valeur finale si connue, sinon le plus haut relevé."""
    if day.get("final") is not None:
        return int(day["final"])
    counts = [c for _, c in (day.get("samples") or [])]
    return max(counts) if counts else None


def is_complete(day: dict) -> bool:
    """Journée suffisamment suivie (début de service ET fin ou fermeture) pour servir de référence."""
    s = day.get("samples") or []
    if not s:
        return False
    first, last = hms_to_sec(s[0][0]), hms_to_sec(s[-1][0])
    if first > (START_MIN + 25) * 60:              # run démarré après ~11h55
        return False
    return bool(day.get("closed_at")) or last >= (END_MIN - 30) * 60


# ══════════════════════════════════════════════════════════════════════════════
# PRÉVISION
# ══════════════════════════════════════════════════════════════════════════════

def forecast(history: dict, today_key: str):
    """
    Prévision pour `today_key` à partir des jours précédents (moyenne pondérée :
    les mêmes jours de la semaine comptent double). Retourne None sans historique.

        {"total": 850,                       # repas attendus (arrondi à 10)
         "peak": 4, "peak_flow": 142,        # créneau le plus chargé (index) + repas attendus dedans
         "low": 0,  "low_flow": 31,          # créneau le plus calme
         "n": 8}                             # nombre de jours utilisés
    """
    today_wd = date.fromisoformat(today_key).weekday()
    cands = []
    for k, d in history["days"].items():
        if k >= today_key or not is_complete(d):
            continue
        total = day_total(d)
        if total is None or total < MIN_TOTAL:
            continue
        cands.append((k, d, total))
    cands = sorted(cands, key=lambda x: x[0])[-LOOKBACK:]
    if not cands:
        return None

    wsum = tsum = 0.0
    slot_w = [0.0] * N_SLOTS
    slot_s = [0.0] * N_SLOTS
    for k, d, total in cands:
        w = SAME_WEEKDAY_WEIGHT if date.fromisoformat(k).weekday() == today_wd else 1.0
        wsum += w
        tsum += w * total
        for i, flow in enumerate(day_profile(d)):
            if flow is not None:
                slot_w[i] += w
                slot_s[i] += w * flow

    out = {"total": int(round(tsum / wsum / 10.0)) * 10, "n": len(cands)}
    avg = [slot_s[i] / slot_w[i] if slot_w[i] else None for i in range(N_SLOTS)]
    peak, low = peak_low(avg)
    if peak is not None:
        out.update(peak=peak, peak_flow=int(round(avg[peak])),
                   low=low, low_flow=int(round(avg[low])))
    return out
