# Affluence du Crous 🏃

Bot GitHub Actions qui surveille l'affluence en direct du **Restaurant Porte Océane**
(CROUS Normandie), prévient sur Discord (embed aux couleurs du bot menu), **mémorise
les repas** pour faire des **prévisions**, et publie le **vendredi soir** une image de bilan
dans le style du bot météo.

Source : section « Affluence en direct » de la page du restaurant = iframe
`https://restos.lescrous.fr/fiche/90-12-33-1`, alimentée par
`https://restos.lescrous.fr/api/ecran/90-12-33-1`.

## Ce que le bot envoie

| Quand | Message |
|---|---|
| **Ouverture** (premier relevé valide à partir de 11h30) | Le Crous est ouvert + nombre de repas + **prévision du jour** : total attendu, **créneau de 15 min le plus chargé** (ex. `12h15–12h30`) et **le plus calme** |
| 100 repas | 🟢 Le service est lancé (+ prévision et pic prévu) |
| 500 repas | 🟠 Ça se remplit — il faut se dépêcher !!! (+ prévision et pic prévu) |
| 1 000 repas | 🔴 Presque complet — il faut se dépêcher !!! (+ prévision et pic prévu) |
| Jauge atteinte (1 100) | ⛔ Crous fermé pour aujourd'hui (une seule fois) |
| **13h30** | 🏁 **Nombre final de repas** + écart avec la prévision + pic et creux réels |
| **Vendredi ~18h15** | 📊 Image de la semaine (lundi → vendredi) |

Le palier « tous les 200 repas » a été supprimé (et le réglage `SEUIL` avec). Chaque
événement n'est envoyé qu'une fois par jour ; hors service l'API n'a pas de jauge et le bot se tait.

À 13h30 le bot utilise le **dernier relevé valide** (l'API peut déjà être repassée « hors service »).
Si la jauge a été atteinte plus tôt, le bilan de 13h30 est quand même envoyé.

## Prévisions

Chaque relevé (toutes les 2 min) est enregistré dans `data/history.json`, commité par le
workflow en fin de service. Le service 11h30–13h30 est découpé en **8 créneaux de 15 min** ;
les repas d'un créneau sont calculés par différence des repas cumulés (interpolés).

La prévision du jour = **moyenne pondérée des 30 derniers jours complets** (un lundi compte
double pour prévoir un lundi) : total attendu (arrondi à 10), créneau le plus chargé, créneau
le plus calme. Elle est calculée une seule fois le matin et stockée, pour être comparée au réel.

- Un jour n'entre dans l'historique de référence que s'il est complet (suivi dès ~11h55 jusqu'à
  ~13h00 ou jauge atteinte) et à ≥ 50 repas.
- Un jour où la jauge a été atteinte n'alimente pas les créneaux **après** la fermeture (le flux est tronqué).
- Dès le 1er jour complet enregistré, une prévision apparaît (de plus en plus fiable avec le temps).

## Image du vendredi (`bilan.py`)

Une carte par jour : mini-histogramme des 8 créneaux (pic en corail, creux en vert), repas
servis et % de la jauge, **créneau de pic** et **créneau le plus calme** (avec nombre de repas),
**repas estimés** vs réel (pastille PRÉCIS ≤ 5 %, CORRECT ≤ 15 %, ÉCART au-delà). Bandeau :
total de la semaine, moyenne par jour, jour le plus chargé, écart moyen des prévisions.
Un seul envoi par semaine (drapeau `bilan_sent` dans l'historique).

## Fiabilité horaire

GitHub ne garantit pas l'heure d'un cron (retards de 10 à 60 min). Le bot ne dépend du cron
**que pour démarrer** (11h07 Paris + secours à 11h37) ; ensuite le script boucle lui-même
(un contrôle toutes les 2 min, dernier tour calé pile à 13h30). Un seul run à la fois (`concurrency`).

À savoir : sur un dépôt **privé**, ~2h30 de run par jour dépassent le quota gratuit
(2 000 min/mois) ; un dépôt public est gratuit. GitHub désactive les crons d'un dépôt public
sans activité depuis 60 jours (à réactiver dans l'onglet Actions) — les commits de stats comptent
comme de l'activité.

## Installation

1. Mettre ces fichiers dans un dépôt GitHub.
2. **Settings → Actions → General → Workflow permissions → Read and write permissions**
   (nécessaire pour commiter `data/history.json`).
3. Settings → Secrets and variables → Actions → secret `WEBHOOK_URL` (webhook Discord).
4. Actions → « Affluence du Crous » → *Run workflow* (avec `dry_run`) pour un 1er test ;
   « Bilan hebdo du Crous » → *Run workflow* avec `dry_run` génère l'image (artifact `bilan-preview`).

## Test local

```bash
pip install -r requirements.txt
DRY_RUN=1 FORCE_RUN=1 python affluence_bot.py     # un contrôle, affiche l'embed
DRY_RUN=1 python bilan.py                          # écrit bilan_preview.png
```

## Réglages

`affluence_bot.py` : `PALIERS` (100, 500, 1000), `CROUS_ID`, `POLL_SECONDS`.
`stats.py` : `START_MIN` / `END_MIN` (11h30 / 13h30), `SLOT_MIN` (15), `LOOKBACK` (30 jours),
`SAME_WEEKDAY_WEIGHT` (2.0), `MIN_TOTAL` (50).
