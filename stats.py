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
        "final":     874,                     # repas à 13h30 (absent si relevés incomplets)
        "partial":   true,                    # (optionnel) le bot n'a pas pu suivre toute la journée
        "forecast":  {...}                    # prévision faite le matin même (voir forecast())
    }

Les jours fermés / sans service ne sont PAS enregistrés (ils faussaient les prévisions) ;
leurs dates sont listées dans `meta.no_service`.

Créneaux : le service 11h30 → 13h30 est découpé en 8 créneaux de 15 min. Le nombre
de repas d'un créneau = repas cumulés à la fin − repas cumulés au début (interpolés
linéairement entre deux relevés).
"""
import json
import os
import statistics
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
LOOKBACK    = 40                  # nombre de jours fiables pris en compte (niveau général)
MAX_SAME_WD = 8                   # nombre de « mêmes jours de la semaine » pris en compte
MAX_DAYS    = 400                 # taille max de l'historique conservé
FINAL_FRESH = END_MIN * 60 - 15 * 60   # un total n'est fiable que si le dernier relevé est ≥ 13h15
JAUGE_DEFAUT = 1100

# ── Prévision ────────────────────────────────────────────────────────────────
# prévision = (1 − W_LAST_WEEK) × « jour type » + W_LAST_WEEK × « même jour la semaine dernière »
#   « jour type » = moyenne/médiane des mardis (si on prévoit un mardi), tirée vers le niveau
#   général des autres jours tant qu'on a peu de mardis (PRIOR_DAYS = poids de ce niveau général
#   exprimé en « nombre de mardis équivalents »).
W_LAST_WEEK = 0.25
PRIOR_DAYS  = 1.0


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
    migrate(h)
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
    if samples and int(repas) < samples[-1][1]:
        return          # le compteur est cumulatif : une baisse = remise à zéro de l'API en fin de service
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
    """Journée suffisamment suivie (début de service ET fin ou fermeture)."""
    s = day.get("samples") or []
    if not s or day.get("partial"):
        return False
    first, last = hms_to_sec(s[0][0]), hms_to_sec(s[-1][0])
    if first > (START_MIN + 25) * 60:              # run démarré après ~11h55
        return False
    return bool(day.get("closed_at")) or last >= FINAL_FRESH


def is_reliable(day: dict) -> bool:
    """Jour utilisable comme référence (prévisions) ET affichable dans le bilan :
    suivi complet, au moins MIN_TOTAL repas."""
    if not is_complete(day):
        return False
    total = day_total(day)
    return total is not None and total >= MIN_TOTAL


def looks_closed(day: dict) -> bool:
    """Des relevés, mais jamais un seul repas : restaurant fermé / pas de service."""
    s = day.get("samples") or []
    return bool(s) and max(c for _, c in s) == 0


def discard_day(history: dict, key: str, reason: str = "no_service") -> None:
    """Supprime un jour de l'historique et note sa date (jour sans service)."""
    history["days"].pop(key, None)
    lst = history["meta"].setdefault(reason, [])
    if key not in lst:
        lst.append(key)
        lst.sort()
        del lst[:-60]


# ══════════════════════════════════════════════════════════════════════════════
# NETTOYAGE / MIGRATION (idempotent, appliqué à chaque lecture de l'historique)
# ══════════════════════════════════════════════════════════════════════════════

def migrate(h: dict) -> None:
    """Retire ce qui a mal été enregistré par les anciennes versions :
      • jours vides (créés sans aucun relevé) ;
      • jours où le compteur est resté à 0 (fermeture, week-end prolongé, fin de service seule) ;
      • totaux « final » non fiables (relevés interrompus avant 13h15 sans fermeture) ;
      • anciennes clés de prévision (`n` → `ref_days`)."""
    for key in list(h["days"]):
        d = h["days"][key]
        samples = d.get("samples") or []
        if not samples:
            h["days"].pop(key)
            continue
        if looks_closed(d):
            discard_day(h, key)
            continue
        if d.get("final") is not None and not d.get("closed_at"):
            if hms_to_sec(samples[-1][0]) < FINAL_FRESH:
                d.pop("final", None)
                d["partial"] = True
        fc = d.get("forecast")
        if isinstance(fc, dict) and "n" in fc:
            fc["ref_days"] = fc.pop("n")


# ══════════════════════════════════════════════════════════════════════════════
# PRÉVISION
# ══════════════════════════════════════════════════════════════════════════════

def _central(vals: list):
    """Valeur centrale d'une série (du plus ancien au plus récent) : moitié médiane
    (insensible aux jours exceptionnels), moitié moyenne pondérée par la récence."""
    if not vals:
        return None
    w = [1 + 0.25 * i for i in range(len(vals))]        # récence : le plus récent pèse un peu plus
    wmean = sum(wi * v for wi, v in zip(w, vals)) / sum(w)
    return 0.5 * statistics.median(vals) + 0.5 * wmean


def _estimate(same_wd: list, everyday: list, last_week):
    """Estimation d'une valeur (total ou créneau) pour le jour à prévoir."""
    base_all = _central(everyday)
    base_wd  = _central(same_wd)
    if base_wd is None:
        base = base_all
    elif base_all is None:
        base = base_wd
    else:
        k = len(same_wd)
        base = (k * base_wd + PRIOR_DAYS * base_all) / (k + PRIOR_DAYS)
    if base is None:
        return last_week
    if last_week is None:
        return base
    return (1 - W_LAST_WEEK) * base + W_LAST_WEEK * last_week


def forecast(history: dict, today_key: str):
    """
    Prévision pour `today_key`. Retourne None sans historique fiable.

    Elle repose SURTOUT sur le jour de la semaine : pour un mardi, moyenne/médiane des
    mardis précédents ; elle est complétée par le niveau général de fréquentation (tant
    qu'on a peu de mardis) et par l'affluence du même jour la semaine précédente.

        {"total": 850,                       # repas attendus (arrondi à 10, plafonné à la jauge)
         "peak": 4, "peak_flow": 142,        # créneau le plus chargé (index) + repas attendus dedans
         "low": 0,  "low_flow": 31,          # créneau le plus calme
         "ref_days": 12,                     # nombre de jours fiables utilisés au total
         "same_weekday_days": 3,             # dont nombre de jours de la même semaine (3 mardis)
         "last_week": 984 | null}            # repas le même jour la semaine précédente
    """
    today = date.fromisoformat(today_key)
    wd = today.weekday()
    days = []
    for k, d in history["days"].items():
        if k < today_key and is_reliable(d):
            days.append((k, d, day_total(d), day_profile(d)))
    days.sort(key=lambda x: x[0])
    days = days[-LOOKBACK:]
    if not days:
        return None

    wd_days = [x for x in days if date.fromisoformat(x[0]).weekday() == wd][-MAX_SAME_WD:]

    # Semaine précédente : le même jour il y a 7 jours ; à défaut (fermé ce jour-là), le plus
    # récent même jour de la semaine des 14 derniers jours.
    lw = None
    for x in reversed(wd_days):
        if 0 < (today - date.fromisoformat(x[0])).days <= 14:
            lw = x
            break

    total = _estimate([x[2] for x in wd_days], [x[2] for x in days], lw[2] if lw else None)
    jauge = int(days[-1][1].get("jauge") or JAUGE_DEFAUT)
    out = {
        "total": int(round(min(total, jauge) / 10.0)) * 10,
        "ref_days": len(days),
        "same_weekday_days": len(wd_days),
        "last_week": lw[2] if lw else None,
    }

    avg = []
    for i in range(N_SLOTS):
        avg.append(_estimate(
            [x[3][i] for x in wd_days if x[3][i] is not None],
            [x[3][i] for x in days if x[3][i] is not None],
            lw[3][i] if lw and lw[3][i] is not None else None))
    peak, low = peak_low(avg)
    if peak is not None:
        out.update(peak=peak, peak_flow=int(round(avg[peak])),
                   low=low, low_flow=int(round(avg[low])))
    return out
