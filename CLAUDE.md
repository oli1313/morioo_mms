# CLAUDE.md — Morioo MMS

Dashboard embarqué (Boesch 510) tournant sur Raspberry Pi 3, affiché en
kiosk Chromium sur un écran tactile Carpuride. Voir README.md pour le
restore et l'usage.

## Commandes

```bash
# Lancer en dev
venv/bin/python main.py            # → http://<IP>:8000/

# Sur le Pi (prod) : géré par systemd
sudo systemctl restart boesch_backend.service
journalctl -u boesch_backend.service -f   # logs en direct
```

### Tests

Le projet vise **Python 3.13**. En dev, le matériel (Wemos/GPS) est absent
→ l'app tourne en mode virtuel, ce qui suffit pour un smoke test :

```bash
python3.13 -m venv venv
venv/bin/pip install -r requirements.txt httpx
venv/bin/python tests/smoke_test.py     # démarre l'app + tape les routes
```

`tests/smoke_test.py` redirige les écritures vers `/tmp` (les chemins prod
`/home/ode/boesch_os` n'existent pas en dev) et vérifie `/api/status`, les
toggles relais (mode virtuel), le reset trip et `/api/trail`. La validation
complète (série, GPS réel) ne se fait que sur le Pi.

## Architecture

- `main.py` — backend FastAPI mono-fichier. Tout l'état vit dans le dict
  global `boat_data` + `trip_data` + `trail`. Deux boucles async lancées
  au `lifespan` via le supervisor `_supervise` (qui les relance si elles
  plantent) :
  - `read_gps()` — parse les trames NMEA RMC (`$GPRMC`/`$GNRMC`) du GPS
    u-blox, avec reconnexion à chaud du port.
  - `simulate_boat_and_spotify()` — profondeur/batterie simulées, ODO,
    position simulée si pas de fix GPS, polling Spotify.
  - Diagnostic exposé sur `/api/diag` (compteurs + uptime).
- `templates/index.html` — UI complète (vanilla JS + Canvas + Leaflet).
  Poll `/api/status` chaque seconde, `/api/trail` toutes les 5 s.
- `relais_usb/relais_usb.ino` — firmware Wemos D1 Mini : lit '1'/'0' sur
  la série (115200) → relais ON/OFF.
- `install/` — restore.sh, service systemd, règle udev.

## Conventions & pièges (IMPORTANT)

- **Chemins absolus codés en dur** : tout pointe vers `/home/ode/boesch_os`
  (.env, trip.json, trail.json, cache Spotify). Ne pas « corriger » sans
  comprendre que la prod tourne sous cet utilisateur/chemin.
- **`boat_data["vitesse"]` est en NŒUDS**, pas en km/h. Le front
  re-multiplie par 1.852 pour l'affichage. Ne pas convertir deux fois.
- **GPS** : les u-blox multi-constellation émettent `$GNRMC`, pas
  forcément `$GPRMC`. Le filtre accepte désormais les deux (sinon le fix
  réel n'arrive jamais) — acquis sur `main`, à préserver.
- **Spotify redirect URI** = `http://127.0.0.1:8000/callback`. L'auth
  (`/login`) doit se faire DEPUIS le Pi, ou bien aligner l'URI sur l'IP
  réelle ET la déclarer dans le dashboard Spotify.
- **Dégradation gracieuse** : Wemos ou GPS absents ⇒ mode virtuel, jamais
  de crash. Garder ce principe pour tout nouveau matériel.

### Correctifs intégrés sur `main`

Ces points étaient des « pièges » ; ils sont désormais **mergés sur `main`**
et deviennent du comportement acquis à préserver :

- `$GNRMC` accepté en plus de `$GPRMC` (bug GPS ci-dessus).
- Supervisor de tâches async, reconnexion série à chaud, I/O protégées,
  `switch_device` fiable (renvoie `status: virtual` si le relais est absent).
- Écritures SD rares / atomiques (`.tmp`→rename) / conditionnelles
  (drapeaux *dirty*), via `SAVE_INTERVAL`.
- Spotify optionnel : l'app démarre même sans `.env`.
- Logging structuré + endpoint `/api/diag` (compteurs de diagnostic).

## Contraintes Raspberry Pi (à respecter dans tout changement)

- **Usure carte SD** : éviter les écritures fréquentes. `trip.json`/
  `trail.json` sont déjà écrits en boucle — ne pas augmenter la fréquence,
  plutôt la réduire ou batcher.
- **Pas de RTC garanti** : l'heure peut être fausse au boot à froid hors
  réseau ; ne pas se fier à l'horloge pour de la logique critique.
- **CPU/GPU limités** : le front redessine 3 jauges Canvas + recentre la
  carte chaque seconde. Ne pas alourdir la boucle de rendu.

## Logging (en place — sans tuer la carte SD)

Objectif : pouvoir récupérer des logs après une sortie en bateau pour
diagnostiquer un souci (et les transmettre pour analyse). Contrainte
absolue : **ne jamais écrire les logs en continu sur la carte SD** (même
raison que `trip.json`/`trail.json` — usure).

Deux stratégies acceptables, l'une ou l'autre :

1. **En continu mais en RAM (« doucement »)** — approche **retenue et en
   place** : le backend logge via le module `logging` (`logger "morioo"`)
   sur stdout/stderr, capté par journald. Reste l'étape ops côté Pi : passer
   le journal en RAM avec `Storage=volatile` dans
   `/etc/systemd/journald.conf` (logs perdus au reboot, mais récupérables
   tant que le Pi tourne).
2. **Dump unique à la demande (« en une fois »)** — bufferiser en RAM
   (ou tmpfs) et n'écrire le fichier sur SD qu'**une seule fois**, sur
   action manuelle ou au shutdown propre, jamais à chaque événement.

Récupération pour analyse (à me transmettre ensuite) :

```bash
journalctl -u boesch_backend.service --since "today" > /tmp/mms.log
# puis copier /tmp/mms.log (scp / clé USB) hors du Pi
```

Règle d'or : **aucune écriture de log à la seconde sur la SD**. Le logging
applicatif (option 1) est en place ; reste à configurer `Storage=volatile`
côté Pi.

## Résilience / éviter les plantages

Principe clé : une boucle async qui lève une exception non rattrapée
meurt en silence — uvicorn continue de servir, donc `systemd` ne
redémarre rien et l'UI fige sur la dernière valeur. À éviter absolument.

Les garde-fous ci-dessous sont **implémentés sur `main`** (supervisor
`_supervise`, écritures atomiques, reconnexion série) : c'est le comportement
à préserver, pas du travail à faire.

- Toute I/O fichier (`save_trip`, `save_trail`, lecture/écriture `.env`)
  doit être protégée par try/except + écriture atomique (`.tmp` → rename).
- Les boucles de fond doivent survivre à une exception (relance interne
  plutôt que mort de la tâche).
- Série (Wemos/GPS) : prévoir une reconnexion à chaud — un connecteur qui
  bouge ne doit pas nécessiter un reboot du service.
- `switch_device` : ne mettre à jour l'état logiciel que si le `write`
  série a réussi, sinon l'UI ment sur l'état réel du relais.

## Secrets

`.env` (jamais commité, voir `.env.example`) : `SPOTIFY_CLIENT_ID`,
`SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REFRESH_TOKEN`. Le refresh token est
réécrit dans `.env` à chaque renouvellement.

Audit (2026-06-01) : aucun secret en dur dans le code ni dans
l'historique git. Le `.gitignore` couvre bien `.env`, `.cache*`,
`trip.json`, `trail.json`. À durcir un jour : `chmod 600` sur `.env`/
`.cache` (appareil physiquement accessible) et migration de l'auth
Spotify vers PKCE (supprimerait le besoin de `client_secret`).

## Sécurité — état actuel & dette connue

ATTENTION : pour le moment, l'application n'a AUCUNE sécurité réseau.
C'est un choix assumé pour un usage sur réseau bateau isolé, mais à
réévaluer si le Pi est un jour exposé (Wi-Fi partagé, port forwarding…).

- **CORS grand ouvert** : `allow_origins=["*"]` + toutes méthodes/headers
  (`main.py`). N'importe quelle page web peut taper l'API.
- **Endpoints `POST` non authentifiés** : `/api/switch/*` (relais),
  `/api/trip/reset`, `/api/spotify/*`. Quiconque est sur le même réseau
  peut piloter le matériel et la musique sans aucune authentification.
- **`/login` et `/callback` Spotify** ouverts également.

Pistes si durcissement nécessaire (à ajouter PLUS TARD, pas urgent) :
restreindre `allow_origins` à l'IP/host réel, ajouter un token/clé
partagée ou un mot de passe sur les routes `POST`, ou binder le serveur
sur le réseau local uniquement plutôt que `0.0.0.0`.
