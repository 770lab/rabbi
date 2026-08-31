"""Client Google Places API (New) — découverte des établissements.

On passe par l'API officielle et non par le scraping de maps.google.com :
c'est ce qu'autorisent les CGU de Google, et c'est stable.
"""

from __future__ import annotations

import json
import math
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://places.googleapis.com/v1"

# Le field mask décide du SKU : Google facture la requête au palier le plus élevé
# de TOUS les champs demandés. Tous ceux ci-dessous plafonnent au palier Enterprise.
# Deux champs en ont été retirés et n'ont rien à y faire :
#   - editorialSummary, seul champ du palier Enterprise + Atmosphere : sa présence
#     faisait basculer 100 % des recherches sur le SKU le plus cher, pour une phrase
#     de présentation facultative qui a déjà un repli (mockup.py) ;
#   - priceLevel, qui n'était lu nulle part.
CHAMPS_LIEUX = ",".join(
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
    )
)

# nextPageToken n'existe que dans SearchTextResponse. L'envoyer à searchNearby fait
# répondre 400 INVALID_ARGUMENT (« Cannot find matching fields for path ») : c'est
# ce qui tuait silencieusement chaque tuile du pavage.
CHAMPS_TEXTE = CHAMPS_LIEUX + ",nextPageToken"

# Appels réellement facturés, par point d'entrée. Le pavage adaptatif peut en
# déclencher plusieurs par tuile : on veut pouvoir le dire à l'utilisateur.
APPELS: dict[str, int] = {}

PLAFOND_NEARBY = 20   # maxResultCount maximal accepté par searchNearby (New)
PROFONDEUR_PAVAGE = 2  # subdivisions d'une tuile saturée : au pire 1 + 4 + 16 appels


def _post(chemin: str, corps: dict, cle: str, champs: str = CHAMPS_LIEUX) -> dict:
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
    APPELS[chemin] = APPELS.get(chemin, 0) + 1
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise SystemExit(f"Places API {e.code} : {detail}")


def resume_appels() -> str:
    """Ligne récapitulative des appels facturés depuis le début du processus."""
    if not APPELS:
        return "aucun appel Places facturé"
    total = sum(APPELS.values())
    detail = " · ".join(f"{c.split(':')[-1]} ×{n}" for c, n in sorted(APPELS.items()))
    return f"{total} appel(s) Places facturé(s) : {detail}"


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
        rep = _post("places:searchText", corps, cle, champs=CHAMPS_TEXTE)
        resultats.extend(rep.get("places", []))
        token = rep.get("nextPageToken")
        if not token:
            break
        time.sleep(1.2)  # le pageToken n'est pas immédiatement actif
    return resultats


def recherche_proximite(types: list[str], zone: dict, cle: str,
                        profondeur_max: int = PROFONDEUR_PAVAGE,
                        _profondeur: int = 0,
                        _saturees: list[str] | None = None) -> list[dict]:
    """searchNearby : 20 résultats max, pas de pagination. À combiner au pavage.

    Une tuile qui renvoie exactement 20 fiches est tronquée par construction : avec
    rankPreference DISTANCE seuls les 20 établissements les plus proches du centre
    remontent (mesuré : les 20 tenaient dans un rayon de 159 m sur une tuile de
    800 m), et tout le reste du cercle est perdu. On redécoupe donc la tuile en
    quatre et on recommence, jusqu'à profondeur_max — et si elle sature encore, on
    le DIT au lieu d'enregistrer un quartier à moitié vu comme s'il était épuisé.

    On ne passe surtout pas CHAMPS_TEXTE ici : searchNearby refuse nextPageToken.
    """
    if _saturees is None:
        _saturees = []          # sous-tuiles encore tronquées : signalées en une ligne
    rayon = float(min(zone.get("rayon_m", 1500), 50000))
    corps = {
        "includedTypes": types,
        "maxResultCount": PLAFOND_NEARBY,
        "languageCode": "fr",
        "rankPreference": "DISTANCE",
        "locationRestriction": {
            "circle": {
                "center": {"latitude": zone["lat"], "longitude": zone["lng"]},
                "radius": rayon,
            }
        },
    }
    lieux = _post("places:searchNearby", corps, cle, champs=CHAMPS_LIEUX).get("places", [])
    if len(lieux) < PLAFOND_NEARBY:
        return lieux

    ou = f"{zone['lat']:.4f},{zone['lng']:.4f} r={int(rayon)}m"
    if _profondeur >= profondeur_max:
        _saturees.append(ou)
        return lieux

    if _profondeur == 0:
        print(f"  · tuile saturée en {ou} → subdivision (jusqu'à "
              f"{sum(4 ** k for k in range(profondeur_max + 1))} appels)", file=sys.stderr)
    fusion = {p["id"]: p for p in lieux if p.get("id")}
    for sous in _sous_tuiles(zone, rayon):
        for p in recherche_proximite(types, sous, cle, profondeur_max,
                                     _profondeur + 1, _saturees):
            if p.get("id"):
                fusion.setdefault(p["id"], p)
        time.sleep(0.15)
    if _profondeur == 0 and _saturees:
        print(f"  ! {len(_saturees)} sous-tuile(s) encore saturée(s) "
              f"({PLAFOND_NEARBY}/{PLAFOND_NEARBY}) autour de {ou} après {profondeur_max} "
              f"subdivision(s) : des établissements de ce secteur n'ont PAS été vus. "
              f"Relancez avec un --pavage plus fin.", file=sys.stderr)
    return list(fusion.values())


def _sous_tuiles(zone: dict, rayon_m: float) -> list[dict]:
    """Découpe un cercle en quatre sous-cercles couvrant son carré inscrit.

    Carré inscrit de côté rayon×√2 ; quatre sous-carrés de côté rayon×√2/2, dont le
    cercle circonscrit a pour rayon rayon/2 et le centre est décalé de rayon×√2/4.
    """
    d = rayon_m * math.sqrt(2) / 4
    dlat = d / 111_320.0
    dlng = d / (111_320.0 * max(math.cos(math.radians(zone["lat"])), 0.01))
    return [
        {
            "ville": zone.get("ville", ""),
            "lat": zone["lat"] + si * dlat,
            "lng": zone["lng"] + sj * dlng,
            "rayon_m": rayon_m / 2,
        }
        for si in (-1, 1)
        for sj in (-1, 1)
    ]


def _rectangle(lat: float, lng: float, rayon_m: float) -> dict:
    dlat = rayon_m / 111_320.0
    dlng = rayon_m / (111_320.0 * max(math.cos(math.radians(lat)), 0.01))
    return {
        "low": {"latitude": lat - dlat, "longitude": lng - dlng},
        "high": {"latitude": lat + dlat, "longitude": lng + dlng},
    }


def pavage(zone: dict, pas_m: int = 800) -> list[dict]:
    """Découpe une zone en sous-cercles : searchNearby plafonne à 20 résultats,
    un quartier dense en contient bien plus. On balaie donc en damier.

    Le rayon d'une tuile vaut la demi-diagonale du pas (pas × 0,71) et non le pas :
    c'est le plus petit cercle qui couvre encore sa case. Avec rayon = pas on payait
    3,1 fois la surface utile en disques qui se recouvraient. En contrepartie on
    garde toutes les cases dont le disque touche encore la zone, sinon la couronne
    extérieure de la zone ne serait plus balayée du tout.
    """
    lat, lng = zone["lat"], zone["lng"]
    rayon = zone.get("rayon_m", 2000)
    if rayon <= pas_m:
        return [zone]
    rayon_tuile = pas_m * math.sqrt(2) / 2
    portee = rayon + rayon_tuile
    n = math.ceil(portee / pas_m)
    dlat = pas_m / 111_320.0
    dlng = pas_m / (111_320.0 * max(math.cos(math.radians(lat)), 0.01))
    tuiles = []
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            if math.hypot(i * pas_m, j * pas_m) > portee:
                continue
            tuiles.append(
                {
                    "ville": zone.get("ville", ""),
                    "lat": lat + i * dlat,
                    "lng": lng + j * dlng,
                    "rayon_m": rayon_tuile,
                }
            )
    return tuiles


def photo_url(photo: dict | str, cle: str, largeur: int = 1200) -> str:
    """`photo` = une entrée de la liste `photos` de normaliser() (dict), ou son seul
    nom (str) pour les fiches enregistrées avant l'ajout des attributions."""
    nom = photo.get("nom", "") if isinstance(photo, dict) else photo
    return f"{BASE}/{nom}/media?maxWidthPx={largeur}&key={cle}"


def credit_photo(photo: dict | str) -> str:
    """Crédit à afficher SOUS la photo : la licence Places l'exige dès qu'on
    réhéberge l'image. À poser tel quel dans un <figcaption>."""
    auteurs = photo.get("auteurs") or [] if isinstance(photo, dict) else []
    noms = [a.get("nom", "") for a in auteurs if a.get("nom")]
    return f"Photo : {', '.join(noms)} via Google" if noms else "Photo via Google"


def auteurs_photo(photo: dict | str) -> list[dict]:
    """Auteurs d'une photo, avec le lien vers leur profil quand Google le donne.

    La licence Places demande d'afficher l'attribution TELLE QU'ELLE EST FOURNIE :
    quand elle porte un `uri`, le nom doit y renvoyer. `credit_photo` en donne la
    version texte, pour les endroits où l'on ne peut pas poser de lien.
    """
    auteurs = photo.get("auteurs") or [] if isinstance(photo, dict) else []
    return [{"nom": a.get("nom", ""), "uri": a.get("uri", "")}
            for a in auteurs if a.get("nom")]


def _composant(place: dict, type_: str) -> str:
    for c in place.get("addressComponents", []):
        if type_ in c.get("types", []):
            return c.get("longText", "")
    return ""


def _photos(place: dict) -> list[dict]:
    """Photos de la fiche AVEC leurs attributions d'auteur.

    Ces photos sont pour l'essentiel des contributions de clients : la licence de la
    Places API impose d'afficher leur auteur partout où l'image est montrée. On ne
    peut donc pas ne garder que `name`, sinon l'attribution est détruite dès la
    normalisation et la maquette réhéberge la photo d'un tiers sans crédit.
    """
    sorties = []
    for p in place.get("photos", [])[:8]:
        nom = p.get("name")
        if not nom:
            continue
        sorties.append(
            {
                "nom": nom,
                "auteurs": [
                    {"nom": a.get("displayName", ""), "uri": a.get("uri", "")}
                    for a in p.get("authorAttributions", [])
                    if a.get("displayName")
                ],
            }
        )
    return sorties


def normaliser(place: dict, zone_nom: str = "") -> dict:
    """Réponse Places -> ligne de la table `etablissements`."""
    horaires = place.get("regularOpeningHours", {}).get("weekdayDescriptions", [])
    photos = _photos(place)
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
        # editorialSummary n'est plus demandé (SKU le plus cher) : `resume` reste
        # vide en usage réel et n'est renseigné que par le jeu d'essai hors ligne.
        "resume": place.get("editorialSummary", {}).get("text", ""),
        "zone": zone_nom,
    }


def charger_demo() -> list[dict]:
    """Jeu d'essai hors ligne (aucune clé API requise)."""
    f = Path(__file__).parent / "fixtures" / "demo_places.json"
    return json.loads(f.read_text(encoding="utf-8"))
