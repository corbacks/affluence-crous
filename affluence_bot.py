#!/usr/bin/env python3
"""
Affluence CROUS Bot — Restaurant Porte Océane (CROUS Normandie)

Lit l'affluence « en direct » (la même que l'iframe de la page du restaurant :
https://restos.lescrous.fr/fiche/90-12-33-1 → API /api/ecran/90-12-33-1)
et prévient sur Discord avec un embed (barre de progression, couleurs Crous).

Messages envoyés (chacun au plus une fois par jour) :
    • OUVERTURE (≈ 11h30)   : le Crous est ouvert + nombre de repas + PRÉVISION du jour
                              (total attendu, créneau de 15 min le plus chargé / le plus calme) ;
    • PALIERS 100 / 500 / 1000 repas (+ prévision et pic prévu) ;
    • JAUGE atteinte        : « Crous fermé pour aujourd'hui » ;
    • BILAN FINAL (13h30)   : nombre final de repas, écart avec la prévision,
                              pic et creux réels.

Chaque relevé est enregistré dans data/history.json : c'est cet historique qui
alimente les prévisions et l'image du bilan hebdomadaire (bilan.py, vendredi soir).
"""
import copy
import json
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

import stats

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
RESTO_ID    = os.getenv("CROUS_ID", "90-12-33-1")          # identifiant de la fiche
API_URL     = f"https://restos.lescrous.fr/api/ecran/{RESTO_ID}"
FICHE_URL   = f"https://restos.lescrous.fr/fiche/{RESTO_ID}"
NOM         = "Restaurant Porte Océane"
BOT_NAME    = "Le GOAT du Crous"

PALIERS     = (100, 500, 1000)                  # la jauge pleine (1100) est gérée à part

# Mode « boucle » : le script se contrôle lui-même au lieu de dépendre de la précision
# du cron GitHub (qui n'est pas garantie). POLL_SECONDS=0 → un seul contrôle puis sortie.
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "0"))
LOOP_UNTIL   = os.getenv("LOOP_UNTIL", "13:35")      # heure de Paris, fin de boucle (garde-fou)

FORCE_RUN = os.getenv("FORCE_RUN", "0") in ("1", "true", "True")  # ignore le garde-fou week-end
DRY_RUN   = os.getenv("DRY_RUN", "0") in ("1", "true", "True")    # affiche au lieu d'envoyer

_base_dir  = os.getenv("GITHUB_WORKSPACE") or os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(_base_dir, "affluence_state.json")
TZ         = ZoneInfo("Europe/Paris")

# Palette reprise du bot menu (thème aubergine, accents chauds)
COL_VERT    = 0x79D696   # (121, 214, 150)
COL_ORANGE  = 0xFF963C   # (255, 150, 60)
COL_CORAIL  = 0xFF6F5A   # (255, 111, 90)
COL_FERME   = 0x7A2E5B   # aubergine clair, lisible sur fond sombre

# ══════════════════════════════════════════════════════════════════════════════
# ÉTAT DU JOUR (conservé entre les runs via actions/cache, remis à zéro chaque jour)
# ══════════════════════════════════════════════════════════════════════════════

STATE_DEFAULTS = {
    "last_sent": None,       # repas au dernier message (pour « depuis le dernier point »)
    "closed": False,         # jauge atteinte
    "paliers": [],           # paliers déjà notifiés
    "opened": False,         # message d'ouverture envoyé
    "final_sent": False,     # bilan de 13h30 envoyé
    "last_repas": None,      # dernier nombre de repas valide (sert au bilan de 13h30)
    "jauge": None,
    "forecast_done": False,  # prévision du jour calculée (une seule fois, le matin)
    "forecast": None,
}


def load_state(today: str) -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        if state.get("date") == today:
            for k, v in STATE_DEFAULTS.items():
                state.setdefault(k, copy.deepcopy(v))
            return state
    except (OSError, ValueError):
        pass
    return {"date": today, **copy.deepcopy(STATE_DEFAULTS)}


def save_state(state: dict) -> None:
    if DRY_RUN:
        return   # un test à blanc ne doit pas marquer des événements comme « déjà notifiés »
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)

# ══════════════════════════════════════════════════════════════════════════════
# RÉCUPÉRATION DE L'AFFLUENCE
# ══════════════════════════════════════════════════════════════════════════════

def fetch_affluence():
    """Retourne (repas, jauge, maj) ou None si aucune donnée exploitable."""
    r = requests.get(
        API_URL,
        headers={"Accept": "application/json", "Referer": FICHE_URL,
                 "User-Agent": "Mozilla/5.0 (affluence-crous-bot)"},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()

    lieux = data.get("lieux") or []
    if not lieux:
        print("API sans lieu : rien à afficher.")
        return None
    if data.get("donneesAJour") is False:
        print("Données signalées comme anciennes (donneesAJour=false) : on ignore.")
        return None

    lieu = lieux[0]
    if lieu.get("transition"):
        print("Transition de service : on ignore.")
        return None
    jauge, repas = lieu.get("jauge"), lieu.get("repas")
    if jauge is None or repas is None:
        print("Pas de jauge (restaurant fermé ?) : on ignore.")
        return None

    maj = None
    try:
        maj = datetime.fromisoformat(str(data["majLe"]).replace("Z", "+00:00")).astimezone(TZ)
    except (KeyError, ValueError):
        pass
    # Comme fiche.js : jamais plus de repas que la capacité
    return min(int(repas), int(jauge)), int(jauge), maj

# ══════════════════════════════════════════════════════════════════════════════
# PRÉSENTATION (embed Discord, sans image)
# ══════════════════════════════════════════════════════════════════════════════

def fmt(n: int) -> str:
    return f"{n:,}".replace(",", "\u202f")          # 1 100 (espace fine insécable)


def signed(n: int) -> str:
    return f"{n:+d}".replace("-", "−")


def pourcentage(repas: int, jauge: int) -> int:
    pct = repas * 100 // jauge
    return 100 if repas >= jauge else min(pct, 99)


def tier(repas: int, jauge: int):
    """(carré de la barre, couleur de l'embed) : vert < 500, orange ≥ 500, rouge ≥ 1000, aubergine = complet."""
    if repas >= jauge:
        return "🟥", COL_FERME
    if repas >= PALIERS[2]:
        return "🟥", COL_CORAIL
    if repas >= PALIERS[1]:
        return "🟧", COL_ORANGE
    return "🟩", COL_VERT


def barre(repas: int, jauge: int, segments: int = 12) -> str:
    plein = round(repas / jauge * segments)
    if repas > 0:
        plein = max(plein, 1)                 # un début de remplissage reste visible
    if repas < jauge:
        plein = min(plein, segments - 1)      # jamais pleine tant que ce n'est pas complet
    carre, _ = tier(repas, jauge)
    return carre * plein + "⬛" * (segments - plein)


def prochain_palier(repas: int, jauge: int, state: dict):
    for p in (*PALIERS, jauge):
        if p > repas and p not in state["paliers"]:
            return p
    return None


def forecast_fields(forecast, jauge: int, compact: bool = False) -> list:
    """Champs « prévision » : total attendu, pic (créneau de 15 min) et, hors mode compact, creux."""
    if not forecast:
        if compact:
            return []
        return [{"name": "📈 Prévision du jour",
                 "value": "Pas encore d'historique — elle apparaîtra dès qu'un service complet aura été enregistré.",
                 "inline": False}]
    pct = pourcentage(min(forecast["total"], jauge), jauge)
    fields = [{"name": "📈 Prévision du jour",
               "value": f"**≈ {fmt(forecast['total'])}** repas ({pct} %)", "inline": True}]
    if forecast.get("peak") is not None:
        fields.append({"name": "🔥 Pic prévu",
                       "value": f"**{stats.slot_label(forecast['peak'])}**\n~{fmt(forecast['peak_flow'])} repas",
                       "inline": True})
        if not compact:
            fields.append({"name": "😌 Le plus calme",
                           "value": f"**{stats.slot_label(forecast['low'])}**\n~{fmt(forecast['low_flow'])} repas",
                           "inline": True})
    return fields


def build_embed(kind: str, repas: int, jauge: int, state: dict, maj,
                palier=None, day=None) -> dict:
    """kind : 'ouverture' | 'palier' | 'ferme' | 'final'."""
    carre, couleur = tier(repas, jauge)
    pct = pourcentage(repas, jauge)
    restant = max(jauge - repas, 0)
    forecast = state.get("forecast")
    quand = (maj or datetime.now(TZ)).strftime("%H:%M")
    footer = f"Données de {quand} · CROUS Normandie"

    if kind == "ferme":
        accroche = "⛔ **Capacité atteinte — le Crous est fermé pour aujourd'hui.**"
    elif kind == "ouverture":
        accroche = "🍽️ **Le Crous est ouvert** — service jusqu'à 13h30."
    elif kind == "final":
        accroche = "🏁 **Service terminé** — voici le bilan de la journée."
        footer = "Bilan de 13h30 · CROUS Normandie"
    elif palier == PALIERS[0]:
        accroche = "🟢 **Le service est lancé** — il reste de la place."
    elif palier == PALIERS[1]:
        accroche = "🟠 **Ça se remplit** — il faut se dépêcher !!!"
    else:
        accroche = "🔴 **Presque complet** — il faut se dépêcher !!!"

    titre_nb = f"# {fmt(repas)} repas servis" if kind == "final" else f"# {fmt(repas)} repas"
    description = (
        f"{titre_nb}\n"
        f"sur {fmt(jauge)} · **{pct} %**\n\n"
        f"{barre(repas, jauge)}\n\n"
        f"{accroche}"
    )

    fields = []
    if kind == "ferme":
        fields.append({"name": "Repas servis", "value": f"{fmt(repas)} / {fmt(jauge)}", "inline": True})
        fields.append({"name": "Réouverture", "value": "Prochain service", "inline": True})

    elif kind == "ouverture":
        fields += forecast_fields(forecast, jauge)
        if forecast:
            footer = f"Prévision basée sur {forecast['n']} jour{'s' if forecast['n'] > 1 else ''} · {footer}"

    elif kind == "final":
        if forecast:
            ecart = repas - forecast["total"]
            rel = round(ecart / forecast["total"] * 100) if forecast["total"] else 0
            fields.append({"name": "📈 Prévision",
                           "value": f"≈ {fmt(forecast['total'])} → **{signed(ecart)}** ({signed(rel)} %)",
                           "inline": True})
        else:
            fields.append({"name": "📈 Prévision", "value": "Pas d'historique", "inline": True})
        profil = stats.day_profile(day) if day else []
        peak, low = stats.peak_low(profil)
        if peak is not None:
            fields.append({"name": "🔥 Pic de fréquentation",
                           "value": f"**{stats.slot_label(peak)}**\n{fmt(profil[peak])} repas", "inline": True})
            fields.append({"name": "😌 Moment le plus calme",
                           "value": f"**{stats.slot_label(low)}**\n{fmt(profil[low])} repas", "inline": True})
        if day and day.get("closed_at"):
            fields.append({"name": "⛔ Jauge atteinte à",
                           "value": day["closed_at"][:5].replace(":", "h"), "inline": True})

    else:  # palier
        fields.append({"name": "Restants", "value": f"**{fmt(restant)}**", "inline": True})
        if state["last_sent"] is not None:
            fields.append({"name": "Depuis le dernier point",
                           "value": f"**{signed(repas - state['last_sent'])}**", "inline": True})
        suivant = prochain_palier(repas, jauge, state)
        if suivant:
            fields.append({"name": "Prochain palier",
                           "value": "Complet" if suivant == jauge else f"{fmt(suivant)} repas",
                           "inline": True})
        fields += forecast_fields(forecast, jauge, compact=True)

    return {
        "author": {"name": f"{BOT_NAME} 🐐 · Affluence en direct"},
        "title": NOM,
        "url": FICHE_URL,
        "description": description,
        "color": couleur,
        "fields": fields,
        "footer": {"text": footer},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def send(embed: dict) -> None:
    if DRY_RUN or not WEBHOOK_URL:
        print("[DRY-RUN] " + json.dumps(embed, ensure_ascii=False, indent=2))
        return
    r = requests.post(WEBHOOK_URL, json={"username": BOT_NAME, "embeds": [embed]}, timeout=20)
    r.raise_for_status()
    print(f"✅ Discord → {r.status_code}")

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def _after(now: datetime, minute_of_day: int) -> bool:
    return now.hour * 3600 + now.minute * 60 + now.second >= minute_of_day * 60


def _step(now: datetime, state: dict, history: dict, day: dict) -> bool:
    """Un contrôle. Retourne True quand il n'y a plus rien à faire aujourd'hui."""
    # 0) Prévision du jour : calculée une seule fois, à partir des jours PRÉCÉDENTS
    if not state["forecast_done"]:
        state["forecast"] = stats.forecast(history, state["date"])
        state["forecast_done"] = True
    if state["forecast"] is not None:
        day["forecast"] = state["forecast"]      # conservée pour l'image du vendredi

    # 1) Lecture de l'API (inutile une fois la jauge atteinte : on attend simplement 13h30)
    result = None
    if not state["closed"]:
        try:
            result = fetch_affluence()
        except requests.RequestException as e:
            print(f"Erreur réseau : {e}")        # ponctuel : on réessaie au prochain tour
    repas = jauge = maj = None
    if result:
        repas, jauge, maj = result
        stats.record(day, now, repas, jauge)
        state["last_repas"] = max(repas, state["last_repas"] or 0)
        state["jauge"] = jauge
        print(f"{now:%H:%M} — {repas}/{jauge} (dernier message : {state['last_sent']})")

    # 2) 13h30 → nombre final de repas (valeur du dernier relevé valide : à 13h30 l'API
    #    peut déjà être repassée « hors service »). Rien n'est envoyé s'il n'y a pas eu de service.
    if _after(now, stats.END_MIN):
        if state["last_repas"] is not None:
            day["final"] = state["last_repas"]
            send(build_embed("final", state["last_repas"], state["jauge"], state, maj, day=day))
        else:
            print("Aucun relevé aujourd'hui : pas de bilan.")
        state["final_sent"] = True
        return True

    if not result:
        return False

    # 3) Jauge atteinte → fermé pour aujourd'hui (une seule fois). Le bilan viendra à 13h30.
    if repas >= jauge:
        send(build_embed("ferme", repas, jauge, state, maj, day=day))
        state["closed"], state["last_sent"] = True, repas
        state["paliers"] = sorted(set(state["paliers"]) | {p for p in PALIERS if p < jauge})
        day["closed_at"], day["final"] = now.strftime("%H:%M:%S"), repas
        return False

    franchis = [p for p in PALIERS if p < jauge and repas >= p and p not in state["paliers"]]

    # 4) Ouverture : premier relevé valide à partir de 11h30 (pas de message les jours sans service)
    if not state["opened"] and _after(now, stats.START_MIN):
        send(build_embed("ouverture", repas, jauge, state, maj, day=day))
        state["opened"], state["last_sent"] = True, repas
        state["paliers"] = sorted(set(state["paliers"]) | set(franchis))   # run tardif : pas de doublon
        return False

    # 5) Palier(s) franchi(s) : un seul message pour le plus haut, tous marqués faits
    if franchis:
        send(build_embed("palier", repas, jauge, state, maj, palier=max(franchis), day=day))
        state["paliers"] = sorted(set(state["paliers"]) | set(franchis))
        state["last_sent"] = repas
    return False


def check_once(now: datetime = None) -> bool:
    now = now or datetime.now(TZ)
    state = load_state(now.strftime("%Y-%m-%d"))
    if state["final_sent"]:
        print("Bilan final déjà envoyé aujourd'hui.")
        return True
    history = stats.load_history()
    day = stats.get_day(history, state["date"])

    done = _step(now, state, history, day)

    save_state(state)
    if not DRY_RUN:
        stats.save_history(history)
    return done


def deadline() -> datetime:
    h, m = (int(x) for x in LOOP_UNTIL.split(":"))
    return datetime.now(TZ).replace(hour=h, minute=m, second=0, microsecond=0)


def seconds_to_final(now: datetime) -> float:
    target = now.replace(hour=stats.END_MIN // 60, minute=stats.END_MIN % 60, second=0, microsecond=0)
    return (target - now).total_seconds()


def main() -> int:
    if datetime.now(TZ).weekday() >= 5 and not FORCE_RUN:
        print("Week-end : silence.")
        return 0

    while True:
        try:
            if check_once():
                break
        except requests.RequestException as e:
            print(f"Erreur réseau : {e}")
        now = datetime.now(TZ)
        if POLL_SECONDS <= 0 or now >= deadline():
            break
        wait = POLL_SECONDS
        to_final = seconds_to_final(now)
        if 0 < to_final < wait:
            wait = to_final + 1          # le dernier tour tombe pile à 13h30
        time.sleep(wait)
    return 0


if __name__ == "__main__":
    sys.exit(main())
