"""Smoke test minimal de Morioo MMS — à lancer en dev (sans matériel).

Démarre l'app FastAPI via TestClient (le Wemos et le GPS sont absents en
dev → mode virtuel), puis vérifie les routes principales. Les écritures
sont redirigées vers /tmp car les chemins prod /home/ode/boesch_os
n'existent pas en dev.

Usage :
    venv/bin/python tests/smoke_test.py
"""
import os
import sys
from pathlib import Path

# Credentials Spotify factices : en dev il n'y a pas de .env, et spotipy
# refuse un client_id vide dès la construction de SpotifyOAuth. On évite
# ainsi un échec au chargement du module (aucun appel réseau n'est émis
# avec ces valeurs, le refresh_token restant vide).
os.environ.setdefault("SPOTIFY_CLIENT_ID", "dummy")
os.environ.setdefault("SPOTIFY_CLIENT_SECRET", "dummy")

# Permet d'importer main.py depuis la racine du repo
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main

# Rediriger les écritures hors de /home/ode/boesch_os (absent en dev)
main.TRIP_FILE = Path("/tmp/mms_trip_test.json")
main.TRAIL_FILE = Path("/tmp/mms_trail_test.json")

from fastapi.testclient import TestClient


def run():
    with TestClient(main.app) as c:
        # /api/status : toutes les clés attendues par le front
        r = c.get("/api/status")
        assert r.status_code == 200, r.status_code
        d = r.json()
        for k in ("vitesse", "profondeur", "batterie", "lat", "lon", "gps_fix", "trip"):
            assert k in d, f"clé manquante dans /api/status : {k}"
        print("OK GET /api/status :", {k: d[k] for k in ("vitesse", "profondeur", "batterie", "gps_fix")})

        # toggle relais (mode virtuel — pas de Wemos en dev)
        r = c.post("/api/switch/feux_navigation")
        assert r.status_code == 200, r.status_code
        print("OK POST /api/switch/feux_navigation :", r.json())

        # device inconnu : ne doit pas planter
        r = c.post("/api/switch/nimporte_quoi")
        print("OK POST /api/switch/nimporte_quoi :", r.json())

        # reset trip
        r = c.post("/api/trip/reset")
        assert r.status_code == 200, r.status_code
        print("OK POST /api/trip/reset :", r.json())

        # trail
        r = c.get("/api/trail")
        assert r.status_code == 200, r.status_code
        assert isinstance(r.json(), list)
        print("OK GET /api/trail : liste de", len(r.json()), "points")

    # fonctions pures (logique testable sans matériel ni I/O)
    assert main._haversine_km((50.0, 5.0), (50.0, 5.0)) == 0.0
    assert main._parse_nmea_coord("5029.000", "N") == 50.483333
    print("OK fonctions pures (_haversine_km, _parse_nmea_coord)")

    print("\nSMOKE TEST OK")


if __name__ == "__main__":
    run()
