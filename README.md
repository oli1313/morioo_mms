# Morioo MMS — Marine Management System

![Dashboard Morioo MMS](docs/screenshot.png)

## Overview
Dashboard tactile embarqué pour le *Boesch 510* (1964, V8 Crusader/Indmar). Tourne sur un Raspberry Pi monté en baie 1-DIN, affiché sur un écran Carpuride sans fil.

**Fonctionnalités :**
- Jauges temps réel : vitesse (km/h), profondeur (m), tension batterie (V)
- Carte nautique live (Leaflet + OpenSeaMap)
- Contrôle des feux de navigation et projecteurs sous-marins OceanLED X-Series
- Compteur de trip ODO (km, nautiques, temps de navigation) avec reset
- Lecture Spotify avec contrôle playback et playlists
- Communication USB/Série vers Wemos D1 Mini (ESP8266) pour le relais

---

## Stack technique
| Couche | Technologie |
|--------|-------------|
| Backend | FastAPI (Python 3.13) + `pyserial` |
| Frontend | HTML5 + Canvas + Leaflet + vanilla JS |
| Matériel | Raspberry Pi 3, Carpuride, Wemos D1 Mini + shield relais |
| Musique | Spotify API via `spotipy` |

---

## Restore après crash

En cas de perte du système, cloner le repo et lancer le script de restore :

```bash
git clone https://github.com/oli1313/morioo_mms /home/ode/boesch_os
bash /home/ode/boesch_os/install/restore.sh
sudo reboot
```

Le script remet en place automatiquement :
- Les paquets système (`xdotool`, `fonts-noto-color-emoji`, etc.)
- L'environnement Python (venv + dépendances)
- Le service systemd `boesch_backend.service` (démarrage automatique)
- La règle udev pour le Wemos (port USB stable via `by-id`)
- Le cron de refresh Chromium toutes les 5 minutes

> **Spotify :** le token n'est pas dans le repo. Après un restore, aller sur `http://<IP-RASP>:8000/login` une fois pour se ré-authentifier.

---

## Installation manuelle (dev)

```bash
# 1. Cloner
git clone https://github.com/oli1313/morioo_mms /home/ode/boesch_os
cd /home/ode/boesch_os

# 2. Venv et dépendances
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# 3. Lancer
venv/bin/python main.py
```

Interface accessible sur `http://<IP>:8000/`

---

## Structure du repo

```
.
├── main.py                  # Backend FastAPI — API, serial, Spotify, ODO
├── requirements.txt         # Dépendances Python
├── templates/
│   └── index.html           # Dashboard (jauges, carte, boutons, ODO, audio)
├── relais_usb/
│   └── relais_usb.ino       # Firmware Wemos D1 Mini (Arduino C++)
├── install/
│   ├── restore.sh           # Script de restore complet
│   ├── boesch_backend.service  # Service systemd
│   └── 99-wemos.rules       # Règle udev CH340 → /dev/ttyWEMOS
├── refresh_chromium.sh      # Envoi F5 à Chromium (appelé par cron)
├── trip.json                # Données ODO persistées (ignoré par git)
└── bin/                     # arduino-cli binary
```

---

## API

| Méthode | Route | Description |
|---------|-------|-------------|
| `GET` | `/api/status` | Toutes les données bateau + trip ODO |
| `POST` | `/api/switch/feux_navigation` | Toggle feux de navigation |
| `POST` | `/api/switch/lumieres_sous_marines` | Toggle projecteurs OceanLED |
| `POST` | `/api/trip/reset` | Remet le compteur ODO à zéro |
| `POST` | `/api/spotify/{action}` | play / pause / next / previous |
| `POST` | `/api/spotify/playlist?playlist_id=` | Lancer une playlist |

---

## Wemos D1 Mini — Firmware

Le sketch `relais_usb.ino` écoute sur le port série (115200 baud) :
- `1` → active le relais (lumières ON)
- `0` → désactive le relais (lumières OFF)

Le port est fixé via udev : le Wemos (chip CH340, `idVendor=1a86`) est toujours accessible via `/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0`, peu importe le port USB physique utilisé.

---

## TODO
- [ ] GPS réel — parse NMEA depuis `/dev/ttyACM0`
- [ ] Capteur de profondeur réel — parse sonar NMEA
- [ ] Tension batterie réelle — lecture analogique depuis le Wemos
- [ ] Contrôle couleur projecteurs OceanLED

---

## Licence
Projet privé — usage personnel sur le *Boesch 510*. Redistribution interdite sans autorisation explicite.
