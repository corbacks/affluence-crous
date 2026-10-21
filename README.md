# Affluence du Crous 🏃

Bot GitHub Actions qui surveille l'affluence en direct du **Restaurant Porte Océane**
(CROUS Normandie), prévient sur Discord (embed aux couleurs du bot menu), **mémorise
les repas** pour faire des **prévisions**, et publie le **vendredi soir** une image de bilan
dans le style du bot météo.

Source : section « Affluence en direct » de la page du restaurant = iframe
`https://restos.lescrous.fr/fiche/90-12-33-1`, alimentée par
`https://restos.lescrous.fr/api/ecran/90-12-33-1`.

## Un seul message par jour, modifié en direct

Le bot crée **un unique message Discord** au premier relevé valide à partir de 11h30, puis le
**modifie** (requête `PATCH` du webhook) à chaque contrôle, toutes les 2 min. Tout l'embed est
recalculé à chaque fois : nombre de repas, %, barre de progression, couleur, accroche, prochain
palier, repas des 10 dernières minutes, prévision et heure des données.

| Moment | Contenu du message |
|---|---|
| 11h30 → jauge | Embed « live » : l'accroche et la couleur suivent les paliers — < 100 « Le Crous est ouvert » · 100 🟢 « Il reste de la place » · 500 🟠 « Ça se remplit — il faut se dépêcher !!! » · 1 000 🔴 « Presque complet — il faut se dépêcher !!! » |
| Jauge atteinte (1 100) | ⛔ « Capacité atteinte — le Crous est fermé pour aujourd'hui » (le bot arrête de lire l'API) |
| **13h30** | 🏁 Le même message devient le **bilan** : nombre final de repas, écart avec la prévision, pic et creux réels (+ heure de fermeture si la jauge a été atteinte) |
| **Vendredi ~18h15** | 📊 Image de la semaine (lundi → vendredi), dans un message à part |

L'identifiant du message est gardé dans `affluence_state.json` (cache GitHub, remis à zéro
chaque jour) : le lendemain, un nouveau message est créé. Si le message est supprimé à la main,
le bot en recrée un au tour suivant. Les jours sans service (API sans jauge), il ne publie rien.

⚠️ Discord n'envoie une notification qu'à la **création** du message, pas à ses modifications :
les passages de paliers ne « sonnent » plus.

À 13h30 le bot utilise le **dernier relevé valide** (l'API peut déjà être repassée « hors service »).

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

`affluence_bot.py` : `PALIERS` (100, 500, 1000 : accroches et couleurs), `CROUS_ID`, `POLL_SECONDS`.
`stats.py` : `START_MIN` / `END_MIN` (11h30 / 13h30), `SLOT_MIN` (15), `LOOKBACK` (30 jours),
`SAME_WEEKDAY_WEIGHT` (2.0), `MIN_TOTAL` (50).
