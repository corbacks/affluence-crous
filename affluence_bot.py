#!/usr/bin/env python3
"""
Affluence CROUS Bot — Restaurant Porte Océane (CROUS Normandie)

Lit l'affluence « en direct » (la même que l'iframe de la page du restaurant :
https://restos.lescrous.fr/fiche/90-12-33-1 → API /api/ecran/90-12-33-1)
et prévient sur Discord avec un embed (barre de progression, couleurs Crous).

UN SEUL message Discord par jour : créé au premier relevé valide à partir de 11h30, puis
MODIFIÉ (PATCH du webhook) à chaque contrôle — repas, %, barre, couleur, accroche, prochain
palier, prévision et heure des données sont recalculés à chaque fois :
    • en service : embed « live » (la couleur et l'accroche suivent les paliers 100/500/1000) ;
    • jauge atteinte : l'embed passe en « Crous fermé pour aujourd'hui » ;
    • 13h30 : le même message devient le BILAN FINAL (nombre final de repas, écart avec la
      prévision, pic et creux réels).

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
    "message_id": None,      # identifiant du message Discord du jour (celui qu'on modifie)
    "closed": False,         # jauge atteinte (message passé en « fermé »)
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


def prochain_palier(repas: int, jauge: int):
    return next((p for p in (*PALIERS, jauge) if p > repas), None)


def accroche_live(repas: int) -> str:
    if repas >= PALIERS[2]:
        return "🔴 **Presque complet** — il faut se dépêcher !!!"
    if repas >= PALIERS[1]:
        return "🟠 **Ça se remplit** — il faut se dépêcher !!!"
    if repas >= PALIERS[0]:
        return "🟢 **Il reste de la place** — le service bat son plein."
    return "🍽️ **Le Crous est ouvert** — service jusqu'à 13h30."


def forecast_fields(forecast, jauge: int) -> list:
    """Champs « prévision » : total attendu, pic et creux (créneaux de 15 min)."""
    if not forecast:
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
        fields.append({"name": "😌 Le plus calme",
                       "value": f"**{stats.slot_label(forecast['low'])}**\n~{fmt(forecast['low_flow'])} repas",
                       "inline": True})
    return fields


def build_embed(kind: str, repas: int, jauge: int, state: dict, maj,
                day=None, now=None) -> dict:
    """Embed COMPLET, recalculé à chaque appel. kind : 'live' | 'ferme' | 'final'."""
    carre, couleur = tier(repas, jauge)
    pct = pourcentage(repas, jauge)
    restant = max(jauge - repas, 0)
    forecast = state.get("forecast")
    quand = (maj or datetime.now(TZ)).strftime("%H:%M")
    footer = f"Données de {quand} · CROUS Normandie"

    if kind == "ferme":
        accroche = "⛔ **Capacité atteinte — le Crous est fermé pour aujourd'hui.**"
    elif kind == "final":
        accroche = "🏁 **Service terminé** — voici le bilan de la journée."
        footer = "Bilan de 13h30 · CROUS Normandie"
    else:
        accroche = accroche_live(repas)

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

    else:  # live
        fields.append({"name": "Restants", "value": f"**{fmt(restant)}**", "inline": True})
        flux = stats.recent_flow(day, now) if day and now else None
        if flux is not None:
            fields.append({"name": "10 dernières minutes", "value": f"**+{fmt(flux)}**", "inline": True})
        suivant = prochain_palier(repas, jauge)
        if suivant:
            fields.append({"name": "Prochain palier",
                           "value": "Complet" if suivant == jauge else f"{fmt(suivant)} repas",
                           "inline": True})
        fields += forecast_fields(forecast, jauge)
        if forecast:
            footer = f"Prévision basée sur {forecast['n']} jour{'s' if forecast['n'] > 1 else ''} · {footer}"

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


def send(embed: dict, state: dict) -> None:
    """Crée le message du jour la première fois, puis le MODIFIE (même identifiant)."""
    if DRY_RUN or not WEBHOOK_URL:
        print("[DRY-RUN] " + json.dumps(embed, ensure_ascii=False, indent=2))
        return
    mid = state.get("message_id")
    if mid:
        r = requests.patch(f"{WEBHOOK_URL}/messages/{mid}", json={"embeds": [embed]}, timeout=20)
        if r.status_code != 404:
            r.raise_for_status()
            print(f"✅ Message {mid} mis à jour")
            return
        print("Message introuvable (supprimé ?) : on en recrée un.")
        state["message_id"] = None
    # wait=true : Discord renvoie le message créé, donc son identifiant
    r = requests.post(WEBHOOK_URL, params={"wait": "true"},
                      json={"username": BOT_NAME, "embeds": [embed]}, timeout=20)
    r.raise_for_status()
    state["message_id"] = r.json()["id"]
    save_state(state)        # tout de suite : un plantage ne doit pas faire perdre l'identifiant
    print(f"✅ Message {state['message_id']} créé")


def publish(embed: dict, state: dict) -> bool:
    """send() sans lever d'exception : en cas d'échec réseau, on réessaiera au prochain tour."""
    try:
        send(embed, state)
        return True
    except (requests.RequestException, KeyError, ValueError) as e:
        print(f"Discord : échec ({e})")
        return False

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
        print(f"{now:%H:%M} — {repas}/{jauge}")

    # 2) 13h30 → le même message devient le bilan final (valeur du dernier relevé valide :
    #    à 13h30 l'API peut déjà être repassée « hors service »). Pas de service = pas de message.
    if _after(now, stats.END_MIN):
        if state["last_repas"] is not None:
            day["final"] = state["last_repas"]
            if not publish(build_embed("final", state["last_repas"], state["jauge"], state, maj,
                                       day=day, now=now), state):
                return False                     # échec Discord : on retente au prochain tour
        else:
            print("Aucun relevé aujourd'hui : pas de message.")
        state["final_sent"] = True
        return True

    # 3) Pas de message avant l'ouverture (11h30), ni les jours sans service (pas de relevé valide)
    if not result or not _after(now, stats.START_MIN):
        return False

    # 4) Jauge atteinte → « fermé pour aujourd'hui » ; plus de lecture jusqu'au bilan de 13h30
    if repas >= jauge:
        day.setdefault("closed_at", now.strftime("%H:%M:%S"))
        day["final"] = repas
        if publish(build_embed("ferme", repas, jauge, state, maj, day=day, now=now), state):
            state["closed"] = True
        return False

    # 5) Cas normal : création du message au 1er relevé, puis mise à jour à chaque tour
    publish(build_embed("live", repas, jauge, state, maj, day=day, now=now), state)
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
