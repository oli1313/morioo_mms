import asyncio
import random
import uvicorn
import serial
import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
import spotipy
from spotipy.oauth2 import SpotifyOAuth

from dotenv import load_dotenv
import os
import logging
import time
import socket
load_dotenv(Path(__file__).parent / ".env")


def _sd_notify(state):
    """Notifie systemd (protocole sd_notify). No-op hors systemd, c.-à-d. quand
    NOTIFY_SOCKET est absent (dev local) — donc sans effet de bord."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr[0] == '@':            # socket abstrait
        addr = '\0' + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(state.encode())
    except Exception:
        pass

# Logs vers stdout/stderr → captés par journald (journalctl -u boesch_backend).
# Pour ne pas user la SD, configurer journald en Storage=volatile (cf. CLAUDE.md).
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("morioo")

# Compteurs de diagnostic exposés via /api/diag. But : après une sortie en
# bateau, récupérer (journalctl + /api/diag) de quoi comprendre ce qui s'est
# passé — reconnexions matériel, pertes de fix GPS, erreurs d'I/O, etc.
_STARTED_AT = time.time()
diag = {
    "gps_fix_acquired": 0,   # nb d'acquisitions de fix GPS
    "gps_fix_lost": 0,       # nb de pertes de fix
    "gps_reconnects": 0,     # nb de reconnexions du port GPS
    "gps_read_errors": 0,    # nb d'erreurs d'I/O série GPS (→ reconnexion)
    "gps_parse_errors": 0,   # nb de trames NMEA illisibles (ignorées, pas de reconnexion)
    "wemos_reconnects": 0,   # nb de reconnexions du Wemos
    "relay_send_ok": 0,      # nb d'ordres relais envoyés avec succès
    "relay_send_fail": 0,    # nb d'ordres relais non envoyés (Wemos absent)
    "save_errors": 0,        # nb d'échecs de sauvegarde disque
    "spotify_errors": 0,     # nb d'erreurs de lecture Spotify
    "task_restarts": 0,      # nb de relances de boucle par le supervisor
}

CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
REDIRECT_URI  = "http://127.0.0.1:8000/callback"
SCOPE         = "user-modify-playback-state user-read-playback-state"
REFRESH_TOKEN = os.getenv("SPOTIFY_REFRESH_TOKEN", "")

def _build_sp_oauth():
    """Construit le client OAuth Spotify, ou None si non configuré.

    Sans cette garde, spotipy lève une exception dès l'import quand
    client_id est vide (.env absent/incomplet) — et TOUT le backend refuse
    alors de démarrer. On préfère démarrer sans Spotify (dégradation
    gracieuse, comme pour le GPS/Wemos absents)."""
    if not CLIENT_ID or not CLIENT_SECRET:
        log.warning("Spotify non configuré (.env absent/incomplet) — fonctionnalité désactivée.")
        return None
    return SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=SCOPE,
        cache_path="/home/ode/boesch_os/.cache",
        open_browser=False,
        requests_timeout=5,   # évite les hangs réseau
    )

sp_oauth = _build_sp_oauth()

# Cache du token en mémoire — refresh seulement quand expiré (~1×/heure)
_spotify_token_info = None

_ENV_PATH = Path("/home/ode/boesch_os/.env")

def _save_refresh_token(new_rt: str) -> None:
    """Écrit le refresh token dans .env (écriture atomique .tmp→rename)."""
    try:
        lines = _ENV_PATH.read_text().splitlines()
        lines = [l for l in lines if not l.startswith("SPOTIFY_REFRESH_TOKEN")]
        lines.append(f"SPOTIFY_REFRESH_TOKEN={new_rt}")
        tmp = _ENV_PATH.with_name(_ENV_PATH.name + ".tmp")
        tmp.write_text("\n".join(lines) + "\n")
        tmp.rename(_ENV_PATH)
    except Exception as e:
        log.warning("Impossible d'écrire le refresh token dans .env : %s", e)

def get_spotify_client_sync():
    """Retourne un client Spotipy prêt à l'emploi, ou None si impossible."""
    global _spotify_token_info, REFRESH_TOKEN
    if not sp_oauth or not REFRESH_TOKEN:
        return None
    try:
        if not _spotify_token_info or sp_oauth.is_token_expired(_spotify_token_info):
            _spotify_token_info = sp_oauth.refresh_access_token(REFRESH_TOKEN)
            # Sauvegarde le nouveau refresh_token s'il a changé
            new_rt = _spotify_token_info.get("refresh_token")
            if new_rt and new_rt != REFRESH_TOKEN:
                REFRESH_TOKEN = new_rt
                _save_refresh_token(new_rt)
        # requests_timeout : sinon un réseau lent (fréquent en bateau) peut faire
        # traîner les appels Spotify indéfiniment.
        return spotipy.Spotify(auth=_spotify_token_info["access_token"], requests_timeout=5)
    except Exception:
        return None


def _fetch_now_playing_sync(sp_client):
    """Récupère le titre/artiste en cours (appel réseau bloquant — à exécuter
    dans un thread, jamais directement dans l'event loop)."""
    current = sp_client.current_playback()
    item = current['item'] if current else None
    if item:
        # Un titre a 'artists' ; une pub ou un épisode de podcast peut ne pas
        # en avoir → on évite le KeyError.
        artists = item.get('artists') or []
        return item.get('name', ''), (artists[0]['name'] if artists else '')
    return "Pas de lecture en cours", ""

async def get_spotify_client():
    """Version async : exécute get_spotify_client_sync dans un thread avec timeout."""
    try:
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(None, get_spotify_client_sync),
            timeout=6.0
        )
    except Exception:
        return None

arduino    = None
gps_serial = None
gps_has_fix = False

WEMOS_PORT = '/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0'
GPS_PORT   = '/dev/ttyACM0'


def connect_arduino():
    """(Re)connecte le Wemos. Laisse arduino à None s'il est absent (mode virtuel)."""
    global arduino
    try:
        arduino = serial.Serial(port=WEMOS_PORT, baudrate=115200, timeout=1)
        log.info("Connecté au Wemos D1 Mini")
    except Exception as e:
        log.warning("Wemos non connecté : %s — mode virtuel.", e)
        arduino = None
    return arduino


def connect_gps():
    """(Re)connecte le GPS. Laisse gps_serial à None s'il est absent (position simulée)."""
    global gps_serial
    try:
        gps_serial = serial.Serial(GPS_PORT, baudrate=9600, timeout=1)
        log.info("GPS u-blox connecté sur %s", GPS_PORT)
    except Exception as e:
        log.warning("GPS non connecté : %s — position simulée.", e)
        gps_serial = None
    return gps_serial


async def _supervise(name, coro_factory):
    """Relance une boucle de fond si elle meurt sur une exception non gérée.

    Sans ce garde-fou, une exception non rattrapée tue la tâche en silence :
    uvicorn continue de répondre, systemd ne redémarre rien (le process n'a
    pas crashé), et l'UI se fige sur la dernière valeur connue. On relance.
    """
    while True:
        try:
            await coro_factory()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            diag["task_restarts"] += 1
            log.error("Tâche '%s' a planté : %r — redémarrage dans 2 s.", name, e)
            await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    connect_arduino()
    if arduino:
        await asyncio.sleep(2)   # laisse le Wemos booter avant le premier write
    connect_gps()
    asyncio.create_task(_supervise("gps", read_gps))
    asyncio.create_task(_supervise("boat", simulate_boat_and_spotify))
    _sd_notify("READY=1")   # ignoré si le service n'est pas en Type=notify
    yield
    # Sauvegarde finale à l'arrêt propre : comme on écrit moins souvent en
    # marche, on garantit au moins une écriture des dernières données ici.
    try:
        save_trip()
        save_trail()
    except Exception as e:
        diag["save_errors"] += 1
        log.error("Sauvegarde finale échouée : %s", e)
    if arduino:
        arduino.close()
    if gps_serial:
        gps_serial.close()

app = FastAPI(lifespan=lifespan)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------------------
# Chargement automatique des modules optionnels (dossier modules/).
# Chaque module est ISOLÉ : une erreur de chargement ou d'exécution est loggée
# et n'affecte jamais le reste de l'application (cf. modules/__init__.py).
# Ajouter une fonctionnalité = déposer un fichier dans modules/ (rien à modifier
# ici ni dans index.html grâce à /api/modules).
# ---------------------------------------------------------------------------
import importlib
import pkgutil

LOADED_MODULES = []
_MODULES_DIR = Path(__file__).parent / "modules"
if _MODULES_DIR.is_dir():
    for _info in pkgutil.iter_modules([str(_MODULES_DIR)]):
        try:
            _mod = importlib.import_module(f"modules.{_info.name}")
            if hasattr(_mod, "router"):
                app.include_router(_mod.router)
            LOADED_MODULES.append(_mod)
            log.info("Module chargé : %s", _info.name)
        except Exception as e:
            log.error("Module '%s' ignoré (erreur de chargement) : %s", _info.name, e)


@app.get("/api/modules")
def list_modules():
    """Liste les modules chargés + l'URL de leur frontend, pour que l'UI charge
    leur JS automatiquement (sans modifier index.html à chaque nouveau module)."""
    out = []
    for m in LOADED_MODULES:
        entry = {"name": getattr(m, "UI_LABEL", m.__name__.rsplit(".", 1)[-1])}
        if hasattr(m, "UI_LABEL") and hasattr(m, "router"):
            entry["ui_js"] = m.router.prefix + "/ui.js"
        out.append(entry)
    return out


TRIP_FILE = Path("/home/ode/boesch_os/trip.json")

def load_trip():
    if TRIP_FILE.exists():
        try:
            return json.loads(TRIP_FILE.read_text())
        except Exception:
            pass
    return {"km": 0.0, "nm": 0.0, "secondes": 0}

# Intervalle d'écriture sur disque (s). Compromis entre usure de la carte SD
# (écrire le moins souvent possible) et perte de données sur coupure brutale.
SAVE_INTERVAL = 60

def _atomic_write(path, text):
    """Écrit dans un fichier temporaire puis rename atomique : évite de laisser
    un .json tronqué/corrompu si une coupure survient pendant l'écriture."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)

def save_trip():
    _atomic_write(TRIP_FILE, json.dumps(trip_data))

trip_data = load_trip()

TRAIL_FILE = Path("/home/ode/boesch_os/trail.json")

def load_trail():
    if TRAIL_FILE.exists():
        try:
            return json.loads(TRAIL_FILE.read_text())
        except Exception:
            pass
    return []

def save_trail():
    _atomic_write(TRAIL_FILE, json.dumps(trail))

# Position par défaut (Andenne centre) — affichée tant qu'il n'y a pas de fix GPS.
_DEFAULT_LAT = 50.4901
_DEFAULT_LON = 5.1002

_trip_dirty  = False   # données ODO modifiées depuis la dernière sauvegarde
_trail_dirty = False   # trace modifiée depuis la dernière sauvegarde

# Trace GPS (max 600 points ≈ 10 min)
trail = load_trail()

boat_data = {
    "vitesse": 0.0,
    "profondeur": 5.0,
    "pompe_de_cale": False,
    "pompe_timer": 0,          # secondes restantes avant arrêt automatique
    "lumieres_sous_marines": False,
    "music_title": "Spotify Déconnecté",
    "music_artist": "",
    "lat": _DEFAULT_LAT,
    "lon": _DEFAULT_LON,
    "batterie": 12.6,
    "gps_fix": False,
}

# ---------------------------------------------------------------------------
# Tâche GPS : lit les trames NMEA $GPRMC en continu
# ---------------------------------------------------------------------------
def _parse_nmea_coord(raw, direction):
    """Convertit 'DDDMM.MMMMM' + 'N/S/E/W' en degrés décimaux."""
    if not raw:
        return None
    dot = raw.index('.')
    degrees = int(raw[:dot - 2])
    minutes = float(raw[dot - 2:])
    val = degrees + minutes / 60.0
    if direction in ('S', 'W'):
        val = -val
    return round(val, 6)

async def read_gps():
    global gps_has_fix, gps_serial
    loop = asyncio.get_running_loop()
    while True:
        if not gps_serial:
            # (Re)connexion à chaud : permet de brancher/rebrancher le GPS
            # sans redémarrer le service.
            await loop.run_in_executor(None, connect_gps)
            if gps_serial:
                diag["gps_reconnects"] += 1
            else:
                gps_has_fix = False
                boat_data["gps_fix"] = False
                await asyncio.sleep(5)
                continue
        # 1) Lecture série : une erreur ici = port perdu → reconnexion.
        try:
            raw = await loop.run_in_executor(None, gps_serial.readline)
        except Exception as e:
            diag["gps_read_errors"] += 1
            log.warning("Lecture GPS échouée : %s — reconnexion.", e)
            try:
                gps_serial.close()
            except Exception:
                pass
            gps_serial = None
            gps_has_fix = False
            boat_data["gps_fix"] = False
            await asyncio.sleep(1)
            continue

        # 2) Parsing de la trame : une trame bruitée/corrompue (fréquent sur une
        # liaison série) est simplement IGNORÉE — surtout pas de reconnexion,
        # sinon le moindre parasite ferait perdre le fix.
        try:
            line = raw.decode('ascii', errors='ignore').strip()
            # Les puces GPS seul émettent $GPRMC ; les u-blox multi-constellation
            # (GPS+GLONASS+Galileo) émettent $GNRMC. On accepte les deux, sinon
            # aucun fix réel n'arrive jamais sur un module moderne.
            if not line.startswith(('$GPRMC', '$GNRMC')):
                continue
            parts = line.split(',')
            # $G[PN]RMC,hhmmss.ss,A,llll.ll,a,yyyyy.yy,a,x.x,x.x,ddmmyy,...
            if len(parts) < 8:
                continue
            status = parts[2]   # A = fix valide, V = invalide
            if status != 'A':
                if gps_has_fix:
                    diag["gps_fix_lost"] += 1
                    log.info("Perte du fix GPS")
                gps_has_fix = False
                boat_data["gps_fix"] = False
                continue
            lat = _parse_nmea_coord(parts[3], parts[4])
            lon = _parse_nmea_coord(parts[5], parts[6])
            speed_knots = float(parts[7]) if parts[7] else 0.0
            if lat is not None and lon is not None:
                if not gps_has_fix:
                    diag["gps_fix_acquired"] += 1
                    log.info("Fix GPS acquis (%.6f, %.6f)", lat, lon)
                boat_data["lat"]     = lat
                boat_data["lon"]     = lon
                boat_data["vitesse"] = round(speed_knots, 1)
                boat_data["gps_fix"] = True
                gps_has_fix = True
        except Exception:
            diag["gps_parse_errors"] += 1
            continue

# ---------------------------------------------------------------------------
# Boucle principale : profondeur/batterie simulées + ODO + Spotify
# ---------------------------------------------------------------------------
async def simulate_boat_and_spotify():
    global _trip_dirty, _trail_dirty
    save_counter    = 0
    spotify_counter = 0
    while True:
        # Watchdog : on signale à systemd qu'on est vivant à chaque tour. Si
        # cette boucle se fige (réseau bloquant, deadlock…), le ping s'arrête et
        # systemd redémarre le service (WatchdogSec dans le .service).
        _sd_notify("WATCHDOG=1")

        # Profondeur et batterie : toujours simulées (pas de capteur réel)
        boat_data["profondeur"] = round(random.uniform(2.0, 8.0), 1)
        boat_data["batterie"]   = round(random.uniform(12.4, 13.1), 2)

        # Vitesse réelle depuis le GPS ; 0 si pas de fix
        vitesse_kmh = boat_data["vitesse"] * 1.852 if gps_has_fix else 0.0
        if not gps_has_fix:
            boat_data["vitesse"] = 0.0

        # Trace et ODO : uniquement si fix GPS confirmé et bateau en mouvement
        # (seuil 3 km/h pour filtrer le bruit GPS à l'arrêt).
        position_ok = gps_has_fix and vitesse_kmh > 3.0
        pt = [boat_data["lat"], boat_data["lon"]]
        if position_ok and (not trail or trail[-1] != pt):
            trail.append(pt)
            if len(trail) > 600:
                trail.pop(0)
            _trail_dirty = True

        # Pompe de cale : décompte auto-OFF
        if boat_data["pompe_timer"] > 0:
            boat_data["pompe_timer"] -= 1
            if boat_data["pompe_timer"] == 0:
                boat_data["pompe_de_cale"] = False
                _send_relay("pompe_de_cale", False)
                log.info("Pompe de cale : arrêt automatique après 30 s")


        # ODO : seuil à 3 km/h pour filtrer le bruit GPS à l'arrêt (~0.2 km/h parasites)
        if vitesse_kmh > 3.0:
            trip_data["km"]       = round(trip_data["km"] + vitesse_kmh / 3600, 4)
            trip_data["nm"]       = round(trip_data["nm"] + boat_data["vitesse"] / 3600, 4)
            trip_data["secondes"] += 1
            _trip_dirty = True

        # Sauvegarde périodique, et seulement si quelque chose a changé :
        # un bateau à l'arrêt n'écrit plus rien sur la SD.
        save_counter += 1
        if save_counter >= SAVE_INTERVAL:
            try:
                # On n'écrit que ce qui a changé (usure SD) ; le drapeau n'est
                # remis à False qu'après une écriture réussie, sinon on réessaie.
                if _trip_dirty:
                    save_trip()
                    _trip_dirty = False
                if _trail_dirty:
                    save_trail()
                    _trail_dirty = False
            except Exception as e:
                # SD pleine, FS en lecture seule… ne doit pas tuer la boucle.
                diag["save_errors"] += 1
                log.error("Sauvegarde échouée : %s", e)
            save_counter = 0

        # Polling Spotify toutes les 10 secondes
        spotify_counter += 1
        if spotify_counter >= 10:
            spotify_counter = 0
            sp_client = await get_spotify_client()
            if sp_client:
                try:
                    # Appel réseau exécuté dans un thread avec timeout : un réseau
                    # lent/coupé ne doit JAMAIS bloquer l'event loop (sinon jauges,
                    # GPS et /api/status se figent toutes les 10 s).
                    loop = asyncio.get_running_loop()
                    title, artist = await asyncio.wait_for(
                        loop.run_in_executor(None, _fetch_now_playing_sync, sp_client),
                        timeout=6.0
                    )
                    boat_data["music_title"]  = title
                    boat_data["music_artist"] = artist
                except Exception as e:
                    diag["spotify_errors"] += 1
                    log.warning("Lecture Spotify échouée : %s", e)
                    boat_data["music_title"]  = "Erreur de lecture"
                    boat_data["music_artist"] = ""
            else:
                boat_data["music_title"]  = "En attente de connexion..."
                boat_data["music_artist"] = ""

        # Modules optionnels : chaque tick est ISOLÉ — une panne d'un module ne
        # doit affecter ni le dashboard ni les autres modules.
        for _mod in LOADED_MODULES:
            _tick = getattr(_mod, "tick", None)
            if _tick:
                try:
                    _tick(boat_data)
                except Exception as e:
                    log.error("Module '%s' tick a échoué : %s", _mod.__name__, e)

        await asyncio.sleep(1)

# Les routes d'authentification
@app.get("/login")
def login():
    if not sp_oauth:
        return HTMLResponse("<h2>Spotify non configuré (.env manquant).</h2>", status_code=503)
    auth_url = sp_oauth.get_authorize_url()
    return RedirectResponse(auth_url)

@app.get("/callback")
def callback(request: Request):
    if not sp_oauth:
        return HTMLResponse("<h2>Spotify non configuré (.env manquant).</h2>", status_code=503)
    code = request.query_params.get("code")
    if not code:
        return HTMLResponse("<h2>Erreur : code manquant.</h2>", status_code=400)
    try:
        token_info = sp_oauth.get_access_token(code)
        new_refresh = token_info["refresh_token"]
        _save_refresh_token(new_refresh)
        global REFRESH_TOKEN
        REFRESH_TOKEN = new_refresh
    except Exception as e:
        return HTMLResponse(f"<h2>Erreur Spotify : {e}</h2>", status_code=500)
    return HTMLResponse("""
        <html><body style="background:#0d0802;color:#ffeedd;font-family:monospace;padding:40px;text-align:center">
        <h1>✅ Spotify connecté</h1>
        <p>Token sauvegardé. Vous pouvez fermer cette page.</p>
        </body></html>
    """)

@app.get("/api/status")
def get_status():
    return {**boat_data, "trip": trip_data}


@app.get("/api/trail")
def get_trail():
    return trail

@app.get("/api/diag")
def get_diag():
    """Compteurs de diagnostic + uptime. À consulter (ou logger) après une
    sortie pour repérer reconnexions, pertes de fix, erreurs d'I/O, etc."""
    return {
        **diag,
        "uptime_s": round(time.time() - _STARTED_AT, 1),
        "gps_fix": boat_data["gps_fix"],
        "wemos_connected": arduino is not None,
        "gps_connected": gps_serial is not None,
    }

@app.post("/api/trip/reset")
def reset_trip():
    trip_data["km"] = 0.0
    trip_data["nm"] = 0.0
    trip_data["secondes"] = 0
    save_trip()
    trail.clear()
    save_trail()
    return {"status": "ok"}

# Canal série de chaque relais : le firmware Wemos pilote DEUX relais adressés
# indépendamment (« P » = pompe, « L » = feux). Sans ce préfixe, les deux
# dispositifs partageraient le même relais et s'écraseraient mutuellement.
_RELAY_CHANNEL = {"pompe_de_cale": b"P", "lumieres_sous_marines": b"L"}


def _send_relay(device, state):
    """Envoie l'état au relais Wemos du `device` (pompe ou feux), avec une
    reconnexion auto si le port est mort. Commande = préfixe de canal + '1'/'0'.
    Retourne True si l'ordre a été physiquement envoyé, False si le Wemos est
    indisponible (mode virtuel ou déconnecté)."""
    global arduino
    prefix = _RELAY_CHANNEL.get(device)
    if prefix is None:
        return False   # device sans relais — ne devrait pas arriver (validé en amont)
    cmd = prefix + (b"1" if state else b"0")
    for _ in range(2):
        if arduino is None:
            connect_arduino()
        if arduino is None:
            diag["relay_send_fail"] += 1
            return False
        try:
            arduino.write(cmd)
            diag["relay_send_ok"] += 1
            return True
        except Exception as e:
            diag["wemos_reconnects"] += 1
            log.warning("Écriture Wemos échouée : %s — tentative de reconnexion.", e)
            try:
                arduino.close()
            except Exception:
                pass
            arduino = None
    diag["relay_send_fail"] += 1
    return False


@app.post("/api/switch/{device}")
def switch_device(device: str):
    if device not in ("pompe_de_cale", "lumieres_sous_marines"):
        return {"status": "error", "message": f"device inconnu : {device}"}

    if device == "pompe_de_cale":
        # La pompe ne se toggle pas : chaque appui démarre un cycle de 30 s.
        # Si elle tourne déjà, on la coupe (double-appui = arrêt d'urgence).
        if boat_data["pompe_de_cale"]:
            boat_data["pompe_de_cale"] = False
            boat_data["pompe_timer"]   = 0
            sent = _send_relay("pompe_de_cale", False)
            log.info("Pompe de cale : arrêt manuel")
        else:
            boat_data["pompe_de_cale"] = True
            boat_data["pompe_timer"]   = 30
            sent = _send_relay("pompe_de_cale", True)
            log.info("Pompe de cale : démarrage (30 s)")
        return {
            "status": "ok" if sent else "virtual",
            "device": device,
            "state": boat_data["pompe_de_cale"],
            "timer": boat_data["pompe_timer"],
            "relay_sent": sent,
        }

    # lumieres_sous_marines : toggle classique
    new_state = not boat_data[device]
    sent = _send_relay(device, new_state)
    boat_data[device] = new_state
    return {
        "status": "ok" if sent else "virtual",
        "device": device,
        "state": new_state,
        "relay_sent": sent,
    }


@app.post("/api/spotify/{action}")
def spotify_action(action: str, playlist_id: str = None):
    """Pilote le device Spotify actif (téléphone / Raspotify).
    Récupère le device_id en cours via current_playback() pour cibler
    explicitement le bon device. Erreurs Spotipy loguées sans crasher."""
    sp = get_spotify_client_sync()
    if not sp:
        return {"status": "error", "message": "Spotify non connecté"}
    try:
        # On cible explicitement le device en cours de lecture (Android Auto,
        # téléphone, Raspotify…). Sans device_id, Spotify choisit lui-même
        # et peut choisir Raspotify (inactif) au lieu du téléphone.
        playback = sp.current_playback()
        device_id = playback["device"]["id"] if playback and playback.get("device") else None
        log.debug("Spotify action %r on device_id=%s", action, device_id)
        if action == "play":
            sp.start_playback(device_id=device_id)
        elif action == "pause":
            sp.pause_playback(device_id=device_id)
        elif action == "next":
            sp.next_track(device_id=device_id)
        elif action == "previous":
            sp.previous_track(device_id=device_id)
        elif action == "playlist" and playlist_id:
            sp.start_playback(device_id=device_id, context_uri=f"spotify:playlist:{playlist_id}")
        else:
            return {"status": "error", "message": f"action inconnue: {action}"}
    except Exception as e:
        msg = repr(e)          # repr() est toujours safe, contrairement à str()
        try:
            log.warning("Spotify action %r failed: %s", action, msg)
        except Exception:
            pass               # ne jamais laisser le logger planter la route
        return {"status": "error", "message": msg}
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
def read_root():
    content = (Path(__file__).parent / "templates" / "index.html").read_text()
    return HTMLResponse(content=content, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache"
    })

@app.get("/android", response_class=HTMLResponse)
def read_android():
    content = (Path(__file__).parent / "templates" / "android.html").read_text()
    return HTMLResponse(content=content, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache"
    })

if __name__ == "__main__":
    # On passe l'objet `app` (et non la chaîne "main:app") pour qu'uvicorn ne
    # réimporte pas le module : sinon le chargement des modules s'exécute deux
    # fois (une fois en __main__, une fois au réimport "main"). reload non
    # utilisé en prod, donc l'objet direct convient.
    uvicorn.run(app, host="0.0.0.0", port=8000)
