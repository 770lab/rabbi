"""Client Google Places API (New) — découverte des établissements.

On passe par l'API officielle et non par le scraping de maps.google.com :
c'est ce qu'autorisent les CGU de Google, et c'est stable.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://places.googleapis.com/v1"

CHAMPS = ",".join(
    "places." + c
    for c in (
        "id",
        "displayName",
        "primaryTypeDisplayName",
        "types",
        "formattedAddress",
        "addressComponents",
        "location",
        "rating",
        "userRatingCount",
        "websiteUri",
        "nationalPhoneNumber",
        "regularOpeningHours",
        "photos",
        "googleMapsUri",
        "businessStatus",
        "editorialSummary",
        "priceLevel",
    )
) + ",nextPageToken"


def _post(chemin: str, corps: dict, cle: str, champs: str = CHAMPS) -> dict:
    req = urllib.request.Request(
        f"{BASE}/{chemin}",
        data=json.dumps(corps).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": cle,
            "X-Goog-FieldMask": champs,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise SystemExit(f"Places API {e.code} : {detail}")


def recherche_texte(requete: str, zone: dict, cle: str, pages: int = 3) -> list[dict]:
    """searchText avec pagination (20 résultats/page, 3 pages max côté Google)."""
    resultats, token = [], None
    for _ in range(pages):
        corps = {
            "textQuery": requete,
            "pageSize": 20,
            "languageCode": "fr",
            "locationRestriction": {
                "rectangle": _rectangle(zone["lat"], zone["lng"], zone.get("rayon_m", 2000))
            },
        }
        if token:
            corps["pageToken"] = token
        rep = _post("places:searchText", corps, cle)
        resultats.extend(rep.get("places", []))
        token = rep.get("nextPageToken")
        if not token:
            break
        time.sleep(1.2)  # le pageToken n'est pas immédiatement actif
    return resultats


def recherche_proximite(types: list[str], zone: dict, cle: str) -> list[dict]:
    """searchNearby : 20 résultats max, pas de pagination. À combiner au pavage."""
    corps = {
        "includedTypes": types,
        "maxResultCount": 20,
        "languageCode": "fr",
        "rankPreference": "DISTANCE",
        "locationRestriction": {
            "circle": {
                "center": {"latitude": zone["lat"], "longitude": zone["lng"]},
                "radius": float(min(zone.get("rayon_m", 1500), 50000)),
            }
        },
    }
    return _post("places:searchNearby", corps, cle).get("places", [])


def _rectangle(lat: float, lng: float, rayon_m: float) -> dict:
    dlat = rayon_m / 111_320.0
    dlng = rayon_m / (111_320.0 * max(math.cos(math.radians(lat)), 0.01))
    return {
        "low": {"latitude": lat - dlat, "longitude": lng - dlng},
        "high": {"latitude": lat + dlat, "longitude": lng + dlng},
    }


def pavage(zone: dict, pas_m: int = 800) -> list[dict]:
    """Découpe une zone en sous-cercles : searchNearby plafonne à 20 résultats,
    un quartier dense en contient bien plus. On balaie donc en damier."""
    lat, lng = zone["lat"], zone["lng"]
    rayon = zone.get("rayon_m", 2000)
    if rayon <= pas_m:
        return [zone]
    n = math.ceil(rayon / pas_m)
    dlat = pas_m / 111_320.0
    dlng = pas_m / (111_320.0 * max(math.cos(math.radians(lat)), 0.01))
    tuiles = []
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            if math.hypot(i * pas_m, j * pas_m) > rayon:
                continue
            tuiles.append(
                {
                    "ville": zone.get("ville", ""),
                    "lat": lat + i * dlat,
                    "lng": lng + j * dlng,
                    "rayon_m": pas_m,
                }
            )
    return tuiles


def photo_url(nom_photo: str, cle: str, largeur: int = 1200) -> str:
    return f"{BASE}/{nom_photo}/media?maxWidthPx={largeur}&key={cle}"


def _composant(place: dict, type_: str) -> str:
    for c in place.get("addressComponents", []):
        if type_ in c.get("types", []):
            return c.get("longText", "")
    return ""


def normaliser(place: dict, zone_nom: str = "") -> dict:
    """Réponse Places -> ligne de la table `etablissements`."""
    horaires = place.get("regularOpeningHours", {}).get("weekdayDescriptions", [])
    photos = [p.get("name") for p in place.get("photos", [])[:8] if p.get("name")]
    return {
        "place_id": place.get("id", ""),
        "nom": place.get("displayName", {}).get("text", ""),
        "categorie": place.get("primaryTypeDisplayName", {}).get("text", ""),
        "types": json.dumps(place.get("types", []), ensure_ascii=False),
        "adresse": place.get("formattedAddress", ""),
        "ville": _composant(place, "locality") or zone_nom,
        "lat": place.get("location", {}).get("latitude"),
        "lng": place.get("location", {}).get("longitude"),
        "telephone": place.get("nationalPhoneNumber", ""),
        "site": (place.get("websiteUri") or "").strip(),
        "note": place.get("rating"),
        "nb_avis": place.get("userRatingCount") or 0,
        "horaires": json.dumps(horaires, ensure_ascii=False),
        "photos": json.dumps(photos, ensure_ascii=False),
        "maps_url": place.get("googleMapsUri", ""),
        "statut": place.get("businessStatus", ""),
        "resume": place.get("editorialSummary", {}).get("text", ""),
        "zone": zone_nom,
    }


def charger_demo() -> list[dict]:
    """Jeu d'essai hors ligne (aucune clé API requise)."""
    f = Path(__file__).parent / "fixtures" / "demo_places.json"
    return json.loads(f.read_text(encoding="utf-8"))
