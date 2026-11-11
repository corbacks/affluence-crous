"""Tests hors-ligne.  Lancer :  python tests/test_all.py"""
import os, sys, tempfile
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
os.environ["GITHUB_WORKSPACE"] = tempfile.mkdtemp()
os.environ["DRY_RUN"] = "1"
import stats, closure, affluence_bot as bot

fails = 0
def check(name, cond, extra=""):
    global fails
    print(("✅" if cond else "❌"), name, extra)
    fails += 0 if cond else 1

def make_day(total, jauge=1100, start="11:31:00"):
    """Journée complète synthétique : courbe linéaire jusqu'à `total` repas."""
    samples, t = [], datetime(2026, 1, 1, 11, 31)
    while t.hour < 13 or (t.hour == 13 and t.minute <= 28):
        frac = ((t.hour * 60 + t.minute) - 690) / 120
        samples.append([t.strftime("%H:%M:%S"), max(0, round(total * frac))])
        t += timedelta(minutes=2)
    return {"samples": samples, "jauge": jauge, "final": total}

def hist(days):
    return {"meta": {}, "days": {k: make_day(v) for k, v in days.items()}}

# ── Prévision : le jour de la semaine compte surtout ────────────────────────────
# 4 semaines : mardis ≈ 1000, autres jours ≈ 600
days = {}
d0 = date(2026, 10, 5)   # lundi
for w in range(4):
    for i in range(5):
        d = d0 + timedelta(days=7 * w + i)
        days[d.isoformat()] = 1000 if i == 1 else 600
h = hist(days)
fc_tue = stats.forecast(h, "2026-11-03")   # mardi
fc_wed = stats.forecast(h, "2026-11-04")   # mercredi
check("mardi prévu ≈ moyenne des mardis", 940 <= fc_tue["total"] <= 1060, fc_tue["total"])
check("mercredi prévu ≈ moyenne des mercredis", 560 <= fc_wed["total"] <= 650, fc_wed["total"])
check("ref_days / same_weekday_days renseignés", fc_tue["ref_days"] == 20 and fc_tue["same_weekday_days"] == 4)
check("aucune ancienne clé 'n'", "n" not in fc_tue)

# ── La semaine précédente influence un peu ──────────────────────────────────────
days2 = dict(days); days2["2026-10-27"] = 700      # le dernier mardi a été creux
fc2 = stats.forecast(hist(days2), "2026-11-03")
check("dernier mardi creux → prévision un peu plus basse, mais pas écrasée",
      fc2["total"] < fc_tue["total"] and fc2["total"] > 800, f"{fc_tue['total']} → {fc2['total']}")
check("last_week = total du mardi précédent", fc2["last_week"] == 700)

# ── Peu de données pour ce jour : on s'appuie sur le niveau général ────────────
h3 = hist({"2026-11-02": 800, "2026-11-04": 600, "2026-11-05": 700})
fc3 = stats.forecast(h3, "2026-11-10")    # mardi, aucun mardi connu
check("aucun mardi connu → niveau général", fc3 and fc3["same_weekday_days"] == 0 and 650 <= fc3["total"] <= 750, fc3)
check("pas d'historique → pas de prévision", stats.forecast({"meta": {}, "days": {}}, "2026-11-10") is None)

# ── Plafond = jauge ─────────────────────────────────────────────────────────────
h4 = hist({"2026-11-02": 1100, "2026-11-09": 1100})
check("prévision jamais > jauge", stats.forecast(h4, "2026-11-16")["total"] <= 1100)

# ── Jours non fiables exclus ────────────────────────────────────────────────────
partial = make_day(409); partial["samples"] = partial["samples"][:20]; partial["final"] = 409
h5 = hist({"2026-11-03": 900}); h5["days"]["2026-11-09"] = partial
check("jour interrompu non utilisé", stats.forecast(h5, "2026-11-10")["ref_days"] == 1)

# ── Nettoyage de l'ancien historique ────────────────────────────────────────────
old = {"meta": {}, "days": {
    "2026-10-27": {"samples": []},
    "2026-11-05": {"samples": [["13:27:52", 0], ["13:30:01", 0]], "final": 0, "forecast": {"total": 1100, "n": 1}},
    "2026-11-09": {"samples": [["11:32:31", 8], ["12:12:46", 409]], "jauge": 1100, "final": 409, "forecast": {"total": 1020, "n": 2}},
    "2026-11-10": {**make_day(984), "forecast": {"total": 1020, "n": 2}},
}}
stats.migrate(old)
check("jour vide supprimé", "2026-10-27" not in old["days"])
check("jour à 0 repas supprimé et noté", "2026-11-05" not in old["days"] and "2026-11-05" in old["meta"]["no_service"])
check("final non fiable retiré (jour partiel)", "final" not in old["days"]["2026-11-09"] and old["days"]["2026-11-09"]["partial"])
check("n → ref_days", old["days"]["2026-11-10"]["forecast"] == {"total": 1020, "ref_days": 2})
before = repr(old); stats.migrate(old)
check("migration idempotente", before == repr(old))

# ── record : compteur cumulatif ─────────────────────────────────────────────────
day = {"samples": []}
TZ = ZoneInfo("Europe/Paris")
stats.record(day, datetime(2026, 11, 10, 13, 20, tzinfo=TZ), 900, 1100)
stats.record(day, datetime(2026, 11, 10, 13, 29, tzinfo=TZ), 0, 1100)     # remise à zéro de l'API
check("relevé qui retombe à 0 ignoré", day["samples"] == [["13:20:00", 900]])

# ── Détection de fermeture sur la page du CROUS ────────────────────────────────
def page(items, banner):
    return (f"<html><h1>Restaurant PORTE OCEANE{banner}</h1><h2>Menus</h2>"
            f"<h3>Menu du mardi 10 novembre 2026</h3><h4>Déjeuner</h4><ul><li>Entrées : 1 BOUCLIER<ul><li>Salade</li></ul></li></ul>"
            f"<h3>Menu du mercredi 11 novembre 2026</h3><h4>Déjeuner</h4>{items}"
            f"<h2>Description</h2><footer><ul><li>Plan du site</li></ul></footer></html>")
closed_page = page("<ul><li>Fermeture</li></ul>", " - Fermé aujourd'hui")
check("11/11 : bloc « Fermeture » → fermé", closure.analyze_page(closed_page, date(2026, 11, 11))["closed"])
check("10/11 : menu publié → ouvert (pas contaminé par le jour suivant)",
      not closure.analyze_page(closed_page, date(2026, 11, 10))["closed"])
check("bandeau seul → fermé", closure.analyze_page(page("", " - Fermé aujourd'hui"), date(2026, 11, 11))["closed"])

# ── Bot : un jour fermé ne lance rien ──────────────────────────────────────────
calls = []
bot.closure.is_closed_today = lambda d: {"closed": True, "reason": "test"}
bot.fetch_affluence = lambda: calls.append("api") or (0, 1100, None)
bot.SKIP_CLOSURE_CHECK = False
bot.POLL_SECONDS = 0
bot.FORCE_RUN = True
check("jour fermé : main() sort sans appeler l'API", bot.main() == 0 and calls == [])

# ── Bot : aucun repas à 12h → arrêt + rien d'enregistré ───────────────────────
bot.DRY_RUN = False
stats.HISTORY_FILE = os.path.join(tempfile.mkdtemp(), "history.json")
bot.STATE_FILE = os.path.join(tempfile.mkdtemp(), "state.json")
bot.send = lambda *a, **k: None
bot.fetch_affluence = lambda: (0, 1100, None)
bot.check_once(datetime(2026, 11, 11, 11, 40, tzinfo=TZ))
done = bot.check_once(datetime(2026, 11, 11, 12, 5, tzinfo=TZ))
h = stats.load_history()
check("0 repas à 12h : arrêt, jour non enregistré", done and "2026-11-11" not in h["days"] and "2026-11-11" in h["meta"]["no_service"])

# ── Bot : jour normal ───────────────────────────────────────────────────────────
vals = iter([(50, 1100, None), (400, 1100, None), (800, 1100, None), (1000, 1100, None)])
bot.STATE_FILE = os.path.join(tempfile.mkdtemp(), "state2.json")
bot.fetch_affluence = lambda: next(vals, None)
for hh, mm in [(11, 40), (12, 10), (12, 50), (13, 20)]:
    bot.check_once(datetime(2026, 11, 12, hh, mm, tzinfo=TZ))
bot.check_once(datetime(2026, 11, 12, 13, 30, 5, tzinfo=TZ))
d = stats.load_history()["days"].get("2026-11-12", {})
check("jour normal : total final enregistré", d.get("final") == 1000, d.get("final"))
check("libellé de la base de prévision",
      bot.forecast_basis({"same_weekday_days": 3, "last_week": 984, "ref_days": 12}, "2026-11-17")
      == "3 mardis + semaine dernière (12 jours au total)")

print("\nÉCHECS :", fails)
sys.exit(1 if fails else 0)
