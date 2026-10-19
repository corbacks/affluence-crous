# Affluence du Crous 🏃

Bot GitHub Actions qui surveille l'affluence en direct du **Restaurant Porte Océane**
(CROUS Normandie) et prévient sur Discord.

Source : section « Affluence en direct » de la page du restaurant, qui est l'iframe
`https://restos.lescrous.fr/fiche/90-12-33-1`. Les chiffres viennent de
`https://restos.lescrous.fr/api/ecran/90-12-33-1` (la même API que `fiche.js`).

## Comportement

- Lun–ven, toutes les 15 min à partir de 11h30 (jusqu'à 13h45, heure de Paris).
- 1er passage : message de départ (« X / 1 100 repas servis »).
- Puis un message seulement si l'écart avec le **dernier nombre envoyé** est ≥ 200
  (plus ou moins) : nouveau nombre + « Il faut se dépêcher !!! ».
- Jauge atteinte : « Crous fermé pour aujourd'hui », une seule fois, puis silence.
- L'état (`affluence_state.json`) est gardé entre les runs avec `actions/cache`,
  et repart de zéro chaque jour.

## Installation

1. Mettre ces fichiers dans un dépôt GitHub.
2. Settings → Secrets and variables → Actions → secret `WEBHOOK_URL` (webhook Discord).
3. Actions → « Affluence du Crous » → *Run workflow* (avec `dry_run` pour un 1er test).

## Test local

```bash
pip install -r requirements.txt
DRY_RUN=1 FORCE_RUN=1 python affluence_bot.py
```

Variables : `SEUIL` (200 par défaut), `CROUS_ID` (90-12-33-1).
