# Affluence du Crous 🏃

Bot GitHub Actions qui surveille l'affluence en direct du **Restaurant Porte Océane**
(CROUS Normandie) et prévient sur Discord avec un embed aux couleurs du bot menu
(barre de progression en emojis, pas d'image à générer).

Source : section « Affluence en direct » de la page du restaurant = iframe
`https://restos.lescrous.fr/fiche/90-12-33-1`, alimentée par
`https://restos.lescrous.fr/api/ecran/90-12-33-1` (la même API que `fiche.js`).

## Fiabilité horaire

GitHub ne garantit pas l'heure d'un cron `schedule` (retards de 10 à 60 min, runs
parfois sautés). Le bot ne dépend donc du cron **que pour démarrer** :

- Départ visé 11h07 (Paris), + deux déclencheurs de secours à 11h37. Heure d'été et
  d'hiver sont programmées ; le step « Gate » écarte les départs hors de 11h00–13h30.
- Une fois lancé, le script boucle lui-même : un contrôle toutes les 2 min
  (`POLL_SECONDS`) jusqu'à 13h35 (`LOOP_UNTIL`), ou jusqu'à la fermeture.
- Un seul run à la fois (`concurrency`) : un départ de secours attend, puis sort.

À savoir : sur un dépôt **privé**, un run d'environ 2h30 par jour dépasse le quota
gratuit (2 000 min/mois) ; un dépôt public est gratuit. GitHub désactive aussi les
crons d'un dépôt public sans activité depuis 60 jours (à réactiver dans l'onglet Actions).

## Quand le bot parle

Le script n'a **aucune restriction horaire** : hors service l'API n'a pas de jauge,
donc le bot se tait. Un message est envoyé :

| Événement | Couleur | Message |
|---|---|---|
| 100 repas | 🟩 vert | Le service est lancé |
| 500 repas | 🟧 orange | Ça se remplit — il faut se dépêcher !!! |
| 1 000 repas | 🟥 corail | Presque complet — il faut se dépêcher !!! |
| Jauge atteinte (1 100) | aubergine | Crous fermé pour aujourd'hui (une seule fois, puis silence) |
| Écart ≥ 200 repas vs le dernier message (plus ou moins) | selon remplissage | Nouveau nombre + il faut se dépêcher !!! |

Chaque palier n'est notifié qu'une fois par jour ; si plusieurs événements tombent au
même passage, un seul message est envoyé. L'état (`affluence_state.json`) est gardé
entre les runs avec `actions/cache` et repart de zéro chaque jour.

## Installation

1. Mettre ces fichiers dans un dépôt GitHub.
2. Settings → Secrets and variables → Actions → secret `WEBHOOK_URL` (webhook Discord).
3. Actions → « Affluence du Crous » → *Run workflow* (avec `dry_run` pour un 1er test).

## Test local

```bash
pip install -r requirements.txt
DRY_RUN=1 FORCE_RUN=1 python affluence_bot.py
```

Variables : `SEUIL` (200 par défaut), `CROUS_ID` (90-12-33-1). Les paliers se
changent dans `PALIERS` (en haut de `affluence_bot.py`).
