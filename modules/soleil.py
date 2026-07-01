"""Module Soleil ☀️ — lever/coucher du soleil + crépuscule nautique.

Calcule les heures de lever/coucher (et l'aube/le crépuscule nautiques) à partir
de la position GPS du bateau. **100 % local** : algorithme astronomique (NOAA /
« Almanac for Computers »), aucune clé API, aucun réseau, aucun matériel — donc
rien à figer si le bateau n'a pas de connexion.

Utilité à bord : savoir quand allumer les feux de navigation et jusqu'à quand
il fait jour pour planifier le retour.

Module autonome : il reçoit la position via tick() et ne dépend de rien dans
main.py. Une exception au chargement ou dans tick() est attrapée par le cœur
sans jamais bloquer le dashboard.
Pour retirer la fonctionnalité : supprimer ce fichier + soleil.js.
"""
import math
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

log = logging.getLogger("morioo")
UI_LABEL = "Soleil"
router = APIRouter(prefix="/api/soleil", tags=["soleil"])

# Angles zénithaux (degrés depuis le zénith) des différents « événements » solaires.
_ZENITH_OFFICIAL = 90.833   # lever/coucher (bord du disque + réfraction atmosphérique)
_ZENITH_NAUTICAL = 102.0    # crépuscule nautique (soleil 12° sous l'horizon)

_state = {
    "ok":        False,
    "sunrise":   None,   # "05:35" (heure locale) ou None
    "sunset":    None,   # "21:58"
    "dawn_naut": None,   # aube nautique — peut être None l'été (pas de nuit nautique)
    "dusk_naut": None,   # crépuscule nautique
    "is_day":    None,   # True si l'on est actuellement entre lever et coucher
    "day_len":   None,   # "16h23" — durée du jour
    "date":      None,   # "2026-07-01" — date du calcul (heure locale)
    "polar":     None,   # None | "day" (soleil de minuit) | "night" (nuit polaire)
    "error":     "",
}

# Position mémorisée (alimentée par tick) et date du dernier calcul : sert à ne
# recalculer qu'une fois par jour, sans que le module touche à autre chose.
_last = {"lat": None, "lon": None}
_computed_for = {"date": None}
_ts = {"sunrise": None, "sunset": None}   # epochs, pour recalculer is_day à chaque tick


def _sun_event_ut(year, month, day, lat, lon, zenith, rising):
    """Heure UTC (en heures décimales) d'un événement solaire pour une date/position.

    Algorithme « Sunrise/Sunset » de l'Almanac for Computers (précision ~1 min).
    Renvoie (status, ut) : status vaut "ok", "always_up" (soleil toujours au-dessus
    du zénith visé — ex. soleil de minuit) ou "always_down" (jamais atteint — ex.
    nuit polaire) ; ut est None hors du cas "ok".
    """
    # Jour de l'année
    n1 = math.floor(275 * month / 9)
    n2 = math.floor((month + 9) / 12)
    n3 = 1 + math.floor((year - 4 * math.floor(year / 4) + 2) / 3)
    n = n1 - (n2 * n3) + day - 30

    lng_hour = lon / 15.0
    t = n + ((6 - lng_hour) / 24) if rising else n + ((18 - lng_hour) / 24)

    # Anomalie moyenne puis longitude vraie du soleil
    m = (0.9856 * t) - 3.289
    lsun = (m + 1.916 * math.sin(math.radians(m))
            + 0.020 * math.sin(math.radians(2 * m)) + 282.634) % 360

    # Ascension droite, ramenée dans le même quadrant que la longitude
    ra = math.degrees(math.atan(0.91764 * math.tan(math.radians(lsun)))) % 360
    ra += (math.floor(lsun / 90) * 90) - (math.floor(ra / 90) * 90)
    ra /= 15.0

    # Déclinaison
    sin_dec = 0.39782 * math.sin(math.radians(lsun))
    cos_dec = math.cos(math.asin(sin_dec))

    cos_h = ((math.cos(math.radians(zenith)) - sin_dec * math.sin(math.radians(lat)))
             / (cos_dec * math.cos(math.radians(lat))))
    if cos_h > 1:
        return "always_down", None   # l'événement ne se produit pas ce jour-là
    if cos_h < -1:
        return "always_up", None

    if rising:
        h = 360 - math.degrees(math.acos(cos_h))
    else:
        h = math.degrees(math.acos(cos_h))
    h /= 15.0

    t_local = h + ra - (0.06571 * t) - 6.622
    ut = (t_local - lng_hour) % 24
    return "ok", ut


def _fmt_dur(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h{m:02d}"


def _recompute(now, lat, lon):
    """Recalcule l'ensemble des heures pour la date locale `now` et la position."""
    y, mo, d = now.year, now.month, now.day

    def local_event(zenith, rising):
        status, ut = _sun_event_ut(y, mo, d, lat, lon, zenith, rising)
        if status != "ok":
            return status, None, None
        dt_local = (datetime(y, mo, d, tzinfo=timezone.utc)
                    + timedelta(hours=ut)).astimezone()
        return "ok", dt_local.strftime("%H:%M"), dt_local.timestamp()

    sr_status, sr_str, sr_ts = local_event(_ZENITH_OFFICIAL, True)
    ss_status, ss_str, ss_ts = local_event(_ZENITH_OFFICIAL, False)
    _, dawn_str, _ = local_event(_ZENITH_NAUTICAL, True)
    _, dusk_str, _ = local_event(_ZENITH_NAUTICAL, False)

    # Cas polaires (le soleil ne se lève / ne se couche pas ce jour-là)
    polar = None
    if sr_status == "always_up":       # soleil toujours au-dessus de l'horizon
        polar = "day"
    elif sr_status == "always_down":   # soleil toujours en dessous
        polar = "night"

    day_len = _fmt_dur(ss_ts - sr_ts) if (sr_ts and ss_ts and ss_ts > sr_ts) else None

    _ts["sunrise"], _ts["sunset"] = sr_ts, ss_ts
    _state.update({
        "ok":        True,
        "sunrise":   sr_str,
        "sunset":    ss_str,
        "dawn_naut": dawn_str,
        "dusk_naut": dusk_str,
        "day_len":   day_len,
        "date":      now.strftime("%Y-%m-%d"),
        "polar":     polar,
        "error":     "",
    })
    log.info("Soleil : lever %s, coucher %s (jour %s)", sr_str, ss_str, day_len)


def tick(boat_data):
    """Appelé ~1×/s par le cœur : mémorise la position, recalcule 1×/jour, met à jour is_day."""
    lat, lon = boat_data.get("lat"), boat_data.get("lon")
    if lat is not None:
        _last["lat"] = lat
    if lon is not None:
        _last["lon"] = lon
    if _last["lat"] is None or _last["lon"] is None:
        return

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if _computed_for["date"] != today:
        _recompute(now, _last["lat"], _last["lon"])
        _computed_for["date"] = today

    # is_day : recalculé à chaque tick (peu coûteux) à partir des epochs mémorisés.
    if _state["polar"] == "day":
        _state["is_day"] = True
    elif _state["polar"] == "night":
        _state["is_day"] = False
    elif _ts["sunrise"] and _ts["sunset"]:
        _state["is_day"] = _ts["sunrise"] <= now.timestamp() <= _ts["sunset"]


@router.get("")
def get_soleil():
    return _state


@router.get("/ui.js")
def ui_js():
    """Sert le frontend du module (fichier compagnon soleil.js)."""
    js = (Path(__file__).parent / "soleil.js").read_text()
    return PlainTextResponse(js, media_type="application/javascript")
