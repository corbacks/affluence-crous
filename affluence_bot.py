#!/usr/bin/env python3
"""
Affluence CROUS Bot — Restaurant Porte Océane (CROUS Normandie)

Lit l'affluence « en direct » (la même que l'iframe de la page du restaurant :
https://restos.lescrous.fr/fiche/90-12-33-1 → API /api/ecran/90-12-33-1)
et prévient sur Discord avec un embed (barre de progression, couleurs Crous).

Règles :
- Aucune restriction horaire dans le script : c'est le cron du workflow qui
  décide quand il tourne. Hors service, l'API n'a pas de jauge exploitable et
  le bot reste silencieux tout seul.
- Un message est envoyé :
    • quand un PALIER est atteint (100, 500, 1000 repas), une seule fois par jour ;
    • quand la jauge est atteinte → « Crous fermé pour aujourd'hui », une seule
      fois, puis silence jusqu'au lendemain ;
    • quand l'écart avec le dernier message est d'au moins 200 repas (plus ou
      moins) → nouveau nombre + « Il faut se dépêcher !!! ».
  Si plusieurs de ces événements tombent au même passage : un seul message.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
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
BOT_NAME    = "Le GOAT du Crous"

SEUIL       = int(os.getenv("SEUIL", "2000"))   # écart déclenchant un message
PALIERS     = (100, 500, 1000)                  # la jauge pleine (1100) est gérée à part

# Mode « boucle » : le script se contrôle lui-même au lieu de dépendre de la précision
# du cron GitHub (qui n'est pas garantie). POLL_SECONDS=0 → un seul contrôle puis sortie.
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "0"))
LOOP_UNTIL   = os.getenv("LOOP_UNTIL", "13:35")      # heure de Paris, fin de la boucle

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
# ÉTAT (conservé entre les runs via actions/cache, remis à zéro chaque jour)
# ══════════════════════════════════════════════════════════════════════════════

def load_state(today: str) -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        if state.get("date") == today:
            state.setdefault("last_sent", None)
            state.setdefault("closed", False)
            state.setdefault("paliers", [])
            return state
    except (OSError, ValueError):
        pass
    return {"date": today, "last_sent": None, "closed": False, "paliers": []}


def save_state(state: dict) -> None:
    if DRY_RUN:
        return   # un test à blanc ne doit pas marquer des paliers comme « déjà notifiés »
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


def build_embed(kind: str, repas: int, jauge: int, state: dict, maj, palier=None) -> dict:
    """kind : 'palier' | 'ecart' | 'ferme'."""
    carre, couleur = tier(repas, jauge)
    pct = pourcentage(repas, jauge)
    restant = max(jauge - repas, 0)

    if kind == "ferme":
        accroche = "⛔ **Capacité atteinte — le Crous est fermé pour aujourd'hui.**"
    elif kind == "palier" and palier == PALIERS[0]:
        accroche = "🟢 **Le service est lancé** — il reste de la place."
    elif kind == "palier" and palier == PALIERS[1]:
        accroche = "🟠 **Ça se remplit** — il faut se dépêcher !!!"
    elif kind == "palier":
        accroche = "🔴 **Presque complet** — il faut se dépêcher !!!"
    else:
        accroche = "⚡ **Ça bouge vite** — il faut se dépêcher !!!"

    description = (
        f"# {fmt(repas)} repas\n"
        f"sur {fmt(jauge)} · **{pct} %**\n\n"
        f"{barre(repas, jauge)}\n\n"
        f"{accroche}"
    )

    fields = []
    if kind == "ferme":
        fields.append({"name": "Repas servis", "value": f"{fmt(repas)} / {fmt(jauge)}", "inline": True})
        fields.append({"name": "Réouverture", "value": "Prochain service", "inline": True})
    else:
        fields.append({"name": "Restants", "value": f"**{fmt(restant)}**", "inline": True})
        if state["last_sent"] is not None:
            fields.append({"name": "Depuis le dernier point",
                           "value": f"**{repas - state['last_sent']:+d}**".replace("-", "−"),
                           "inline": True})
        suivant = prochain_palier(repas, jauge, state)
        if suivant:
            fields.append({"name": "Prochain palier",
                           "value": "Complet" if suivant == jauge else f"{fmt(suivant)} repas",
                           "inline": True})

    quand = (maj or datetime.now(TZ)).strftime("%H:%M")
    return {
        "author": {"name": f"{BOT_NAME} 🐐 · Affluence en direct"},
        "title": NOM,
        "url": FICHE_URL,
        "description": description,
        "color": couleur,
        "fields": fields,
        "footer": {"text": f"Données de {quand} · CROUS Normandie"},
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

def check_once() -> bool:
    """Un contrôle. Retourne True s'il n'y a plus rien à surveiller aujourd'hui (fermé)."""
    now = datetime.now(TZ)
    state = load_state(now.strftime("%Y-%m-%d"))
    if state["closed"]:
        print("Déjà marqué fermé aujourd'hui.")
        return True

    result = fetch_affluence()
    if result is None:
        return False
    repas, jauge, maj = result
    print(f"{now:%H:%M} — {repas}/{jauge} (dernier message : {state['last_sent']})")

    # 1) Jauge atteinte → fermé pour aujourd'hui (une seule fois)
    if repas >= jauge:
        send(build_embed("ferme", repas, jauge, state, maj))
        state["closed"], state["last_sent"] = True, repas
        state["paliers"] = sorted(set(state["paliers"]) | {p for p in PALIERS if p < jauge})
        save_state(state)
        return True

    # 2) Palier(s) franchi(s) : un seul message pour le plus haut, tous marqués faits
    franchis = [p for p in PALIERS if p < jauge and repas >= p and p not in state["paliers"]]
    if franchis:
        send(build_embed("palier", repas, jauge, state, maj, palier=max(franchis)))
        state["paliers"] = sorted(set(state["paliers"]) | set(franchis))
        state["last_sent"] = repas
        save_state(state)
        return False

    # 3) Écart d'au moins SEUIL (plus ou moins) par rapport au dernier message
    reference = state["last_sent"] if state["last_sent"] is not None else 0
    if abs(repas - reference) >= SEUIL:
        send(build_embed("ecart", repas, jauge, state, maj))
        state["last_sent"] = repas
        save_state(state)

    return False


def deadline() -> datetime:
    h, m = (int(x) for x in LOOP_UNTIL.split(":"))
    return datetime.now(TZ).replace(hour=h, minute=m, second=0, microsecond=0)


def main() -> int:
    if datetime.now(TZ).weekday() >= 5 and not FORCE_RUN:
        print("Week-end : silence.")
        return 0

    while True:
        try:
            if check_once():
                break
        except requests.RequestException as e:
            # Panne réseau/API ponctuelle : on réessaie au prochain tour
            print(f"Erreur réseau : {e}")
        if POLL_SECONDS <= 0 or datetime.now(TZ) >= deadline():
            break
        time.sleep(POLL_SECONDS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
