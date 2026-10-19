#!/usr/bin/env python3
"""
Affluence CROUS Bot — Restaurant Porte Océane (CROUS Normandie)

Lit l'affluence « en direct » (la même que l'iframe de la page du restaurant :
https://restos.lescrous.fr/fiche/90-12-33-1 → API /api/ecran/90-12-33-1)
et prévient sur Discord.

Règles :
- Actif du lundi au vendredi, de 11h30 à 13h45 (heure de Paris).
- 1er passage de la journée : message d'état initial (point de référence).
- Ensuite : un nouveau message UNIQUEMENT si l'écart avec le dernier nombre
  envoyé est d'au moins 200 repas (en plus ou en moins) → nouveau nombre
  + « Il faut se dépêcher !!! ».
- Quand la jauge est atteinte : « Crous fermé pour aujourd'hui », une seule
  fois, puis le bot ne dit plus rien jusqu'au lendemain.
"""
import json
import os
import sys
from datetime import datetime, time
from zoneinfo import ZoneInfo

import requests

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
RESTO_ID    = os.getenv("CROUS_ID", "90-12-33-1")          # identifiant de la fiche
API_URL     = f"https://restos.lescrous.fr/api/ecran/{RESTO_ID}"
FICHE_URL   = f"https://restos.lescrous.fr/fiche/{RESTO_ID}"
NOM         = "Restaurant Porte Océane"

SEUIL       = int(os.getenv("SEUIL", "200"))     # écart déclenchant un message
DEBUT       = time(11, 30)                        # début de la surveillance
FIN         = time(13, 45)                        # service fini à 13h30 + marge de retard cron

FORCE_RUN = os.getenv("FORCE_RUN", "0") in ("1", "true", "True")  # ignore jour/heure
DRY_RUN   = os.getenv("DRY_RUN", "0") in ("1", "true", "True")    # affiche au lieu d'envoyer

_base_dir  = os.getenv("GITHUB_WORKSPACE") or os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(_base_dir, "affluence_state.json")
TZ         = ZoneInfo("Europe/Paris")

# ══════════════════════════════════════════════════════════════════════════════
# ÉTAT (conservé entre les runs via actions/cache)
# ══════════════════════════════════════════════════════════════════════════════

def load_state(today: str) -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        if state.get("date") == today:
            return state
    except (OSError, ValueError):
        pass
    # Nouveau jour (ou pas d'état) : on repart de zéro
    return {"date": today, "last_sent": None, "closed": False}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)

# ══════════════════════════════════════════════════════════════════════════════
# RÉCUPÉRATION DE L'AFFLUENCE
# ══════════════════════════════════════════════════════════════════════════════

def fetch_affluence():
    """Retourne (repas, jauge) ou None si aucune donnée exploitable."""
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
    # Comme fiche.js : jamais plus de repas que la capacité
    return min(int(repas), int(jauge)), int(jauge)

# ══════════════════════════════════════════════════════════════════════════════
# DISCORD
# ══════════════════════════════════════════════════════════════════════════════

def fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")          # 1 100


def send(message: str) -> None:
    if DRY_RUN or not WEBHOOK_URL:
        print(f"[DRY-RUN] {message}")
        return
    r = requests.post(WEBHOOK_URL, json={"content": message}, timeout=20)
    r.raise_for_status()

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> int:
    now = datetime.now(TZ)
    if not FORCE_RUN:
        if now.weekday() >= 5:
            print("Week-end : silence.")
            return 0
        if not (DEBUT <= now.time() <= FIN):
            print(f"{now:%H:%M} hors de la plage {DEBUT:%H:%M}–{FIN:%H:%M} : on ne fait rien.")
            return 0

    state = load_state(now.strftime("%Y-%m-%d"))
    if state["closed"]:
        print("Déjà marqué fermé aujourd'hui.")
        return 0

    result = fetch_affluence()
    if result is None:
        return 0
    repas, jauge = result
    print(f"{now:%H:%M} — {repas}/{jauge} (dernier envoi : {state['last_sent']})")

    # 1) Capacité atteinte → fermé pour aujourd'hui
    if repas >= jauge:
        send(f"🔒 **{NOM}** : capacité atteinte ({fmt(repas)} / {fmt(jauge)}). "
             f"Le Crous est **fermé pour aujourd'hui** ❌")
        state["closed"], state["last_sent"] = True, repas
        save_state(state)
        return 0

    # 2) Premier message de la journée : point de départ
    if state["last_sent"] is None:
        send(f"🍽️ **{NOM}** : {fmt(repas)} / {fmt(jauge)} repas servis pour l'instant.")
        state["last_sent"] = repas
        save_state(state)
        return 0

    # 3) Écart d'au moins SEUIL par rapport au dernier message
    if abs(repas - state["last_sent"]) >= SEUIL:
        send(f"🚨 **{NOM}** : **{fmt(repas)} / {fmt(jauge)}** repas servis "
             f"({repas - state['last_sent']:+d} depuis le dernier message). "
             f"**Il faut se dépêcher !!!** 🏃💨")
        state["last_sent"] = repas
        save_state(state)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.RequestException as e:
        # Panne réseau/API ponctuelle : on n'échoue pas le workflow, le prochain run réessaiera
        print(f"Erreur réseau : {e}")
        sys.exit(0)
