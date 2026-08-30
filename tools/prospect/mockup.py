"""Génération de la maquette « on s'est permis de refaire votre site ».

Une page unique, autonome, rapide, mobile d'abord, avec les données réelles de la
fiche Google : c'est la preuve qui remplace tout l'argumentaire commercial.
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import re
import unicodedata
import urllib.request
from pathlib import Path

from . import places

BASE = Path(__file__).resolve().parent
GABARIT = Path(__file__).parent / "templates" / "mockup" / "index.html"
SORTIE = BASE / "out" / "maquettes"

# Une teinte par famille de métier : la maquette ne doit pas avoir l'air d'un gabarit.
TEINTES = {
    "restaurant": "b4472f", "food": "b4472f", "bakery": "a9762d", "cafe": "8a5a3b",
    "bar": "6b3f5e", "hair_care": "2f6b6b", "beauty_salon": "9b4a6b",
    "plumber": "2f5ea8", "electrician": "b8862b", "car_repair": "44506b",
    "dentist": "2f7f8f", "doctor": "2f7f8f", "real_estate_agency": "3d6b45",
    "lawyer": "3a3f5c", "gym": "39603f", "store": "6b5030",
}

ACCROCHES = {
    "restaurant": "Une cuisine {qualificatif}, servie {lieu}. Réservez votre table en un appel.",
    "bakery": "Pains, viennoiseries et pâtisseries préparés chaque matin {lieu}.",
    "cafe": "Un café de quartier {lieu}, du matin au soir.",
    "bar": "Le rendez-vous du quartier {lieu} : bonne ambiance, bonnes adresses.",
    "hair_care": "Coupe, couleur et conseils sur mesure {lieu}. Prenez rendez-vous en un appel.",
    "beauty_salon": "Soins et beauté {lieu}, dans un cadre pensé pour souffler.",
    "plumber": "Dépannage, installation et rénovation {lieu}. Devis clair, intervention rapide.",
    "electrician": "Installation, mise aux normes et dépannage électrique {lieu}.",
    "car_repair": "Entretien, réparation et diagnostic {lieu}. Devis avant travaux.",
    "dentist": "Cabinet dentaire {lieu} : soins, prévention et urgences.",
    "_defaut": "{categorie} {lieu}. Une équipe joignable, des horaires clairs, un contact direct.",
}

QUALIFICATIFS = ["généreuse", "de saison", "faite maison", "sincère", "soignée"]


def creneau(texte: str) -> str:
    """Nom de dossier propre à partir du nom de l'établissement."""
    t = unicodedata.normalize("NFKD", texte or "").encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-zA-Z0-9]+", "-", t).strip("-").lower()
    return t[:60] or "etablissement"


def _teinte(types: list[str]) -> str:
    for t in types:
        if t in TEINTES:
            return TEINTES[t]
    return "8a5a3b"


def _etoiles(note) -> str:
    if not note:
        return ""
    pleines = int(round(float(note)))
    return "★" * pleines + "☆" * (5 - pleines)


def _accroche(etab: dict, types: list[str]) -> str:
    lieu = f"à {etab['ville']}" if etab.get("ville") else "dans le quartier"
    for t in types:
        if t in ACCROCHES:
            return ACCROCHES[t].format(
                qualificatif=QUALIFICATIFS[len(etab.get("nom", "")) % len(QUALIFICATIFS)],
                lieu=lieu,
                categorie=etab.get("categorie") or "Notre établissement",
            )
    return ACCROCHES["_defaut"].format(
        lieu=lieu, categorie=etab.get("categorie") or "Notre établissement",
        qualificatif="soignée")


def _presentation(etab: dict) -> str:
    if (etab.get("resume") or "").strip():
        base = etab["resume"].strip()
    else:
        base = (f"{etab['nom']} accueille ses clients "
                f"{'à ' + etab['ville'] if etab.get('ville') else 'dans le quartier'}.")
    avis, note = etab.get("nb_avis") or 0, etab.get("note")
    if avis and note:
        base += (f" {avis} personnes ont laissé un avis sur Google, pour une note "
                 f"moyenne de {note}/5.")
    return base


def _telecharger_photos(noms: list[str], cle: str, dossier: Path, maxi: int = 6) -> list[str]:
    """Rapatrie les photos de la fiche : la clé API ne doit jamais finir dans le HTML."""
    dossier.mkdir(parents=True, exist_ok=True)
    fichiers = []
    for i, nom in enumerate(noms[:maxi]):
        cible = dossier / f"photo-{i+1}.jpg"
        try:
            req = urllib.request.Request(
                places.photo_url(nom, cle, 1600),
                headers={"User-Agent": "Mozilla/5.0 (maquette)"},
            )
            with urllib.request.urlopen(req, timeout=25) as r:
                cible.write_bytes(r.read())
            fichiers.append(cible.name)
        except Exception:
            continue
    return fichiers


def _jsonld(etab: dict, types: list[str], canonical: str) -> str:
    type_ld = "Restaurant" if any(t in ("restaurant", "food", "cafe", "bar", "bakery")
                                  for t in types) else "LocalBusiness"
    d = {
        "@context": "https://schema.org",
        "@type": type_ld,
        "name": etab.get("nom", ""),
        "address": {"@type": "PostalAddress", "streetAddress": etab.get("adresse", ""),
                    "addressLocality": etab.get("ville", "")},
        "telephone": etab.get("telephone", ""),
        "url": canonical,
    }
    if etab.get("lat"):
        d["geo"] = {"@type": "GeoCoordinates", "latitude": etab["lat"],
                    "longitude": etab["lng"]}
    if etab.get("note") and etab.get("nb_avis"):
        d["aggregateRating"] = {"@type": "AggregateRating",
                                "ratingValue": etab["note"],
                                "reviewCount": etab["nb_avis"]}
    horaires = json.loads(etab.get("horaires") or "[]")
    if horaires:
        d["openingHours"] = horaires
    return json.dumps(d, ensure_ascii=False, indent=2)


def generer(etab: dict, cfg: dict, cle_api: str | None = None,
            avis: list[dict] | None = None, dossier_sortie: Path | None = None) -> Path:
    """Écrit out/maquettes/<slug>/index.html et renvoie le chemin."""
    types = json.loads(etab.get("types") or "[]")
    slug = creneau(etab.get("nom", ""))
    dossier = (dossier_sortie or SORTIE) / slug
    dossier.mkdir(parents=True, exist_ok=True)

    photos_api = json.loads(etab.get("photos") or "[]")
    fichiers = _telecharger_photos(photos_api, cle_api, dossier) if cle_api and photos_api else []

    teinte = _teinte(types)
    if fichiers:
        fond_hero = f"#111 url('{fichiers[0]}') center/cover no-repeat"
        photo_principale = fichiers[0]
    else:
        fond_hero = (f"linear-gradient(135deg,#{teinte} 0%,#1b1614 100%)")
        photo_principale = ""

    horaires = json.loads(etab.get("horaires") or "[]")
    lignes_h = "".join(
        "<li><span>{}</span><span>{}</span></li>".format(
            html.escape(l.split(":", 1)[0]),
            html.escape(l.split(":", 1)[1].strip() if ":" in l else ""))
        for l in horaires
    ) or '<li><span>Horaires à confirmer</span><span>—</span></li>'

    if fichiers[1:]:
        vignettes = "".join(
            f'<figure><img src="{f}" alt="{html.escape(etab["nom"])}" loading="lazy" '
            f'width="800" height="600"></figure>' for f in fichiers[1:]
        )
        section_photos = (
            '<section style="background:var(--carte)"><div class="enveloppe">'
            '<h2>En images</h2><div class="galerie">' + vignettes + "</div></div></section>"
        )
    else:
        section_photos = ""

    if avis:
        cartes = "".join(
            '<div class="carte"><div class="etoiles">{}</div><blockquote><p>{}</p>'
            '<footer>{}</footer></blockquote></div>'.format(
                _etoiles(a.get("note", 5)),
                html.escape((a.get("texte") or "")[:260]),
                html.escape(a.get("auteur") or "Client Google"))
            for a in avis[:3]
        )
        section_avis = ('<section><div class="enveloppe"><h2>Ils sont passés</h2>'
                        '<div class="grille g3">' + cartes + "</div></div></section>")
    else:
        section_avis = ""

    bloc_note = ""
    if etab.get("note"):
        bloc_note = ('<div class="note"><span class="etoiles">{}</span>'
                     '<span>{}/5 · {} avis Google</span></div>').format(
            _etoiles(etab["note"]), etab["note"], etab.get("nb_avis") or 0)

    tel = etab.get("telephone") or ""
    tel_brut = re.sub(r"[^\d+]", "", tel) or ""
    canonical = (cfg.get("maquettes", {}).get("base_url") or "").rstrip("/")
    canonical = f"{canonical}/{slug}/" if canonical else ""
    societe = cfg["identite"].get("societe") or cfg["identite"].get("nom") or "notre studio"
    lien_rdv = cfg["rdv"].get("lien") or f"mailto:{cfg['identite'].get('email','')}"

    remplacements = {
        "NOM": html.escape(etab.get("nom", "")),
        "CATEGORIE": html.escape(etab.get("categorie") or "Établissement"),
        "VILLE": html.escape(etab.get("ville") or ""),
        "ADRESSE": html.escape(etab.get("adresse") or ""),
        "TEL": html.escape(tel or "—"),
        "TEL_BRUT": tel_brut,
        "MAPS_URL": etab.get("maps_url") or "#",
        "META_DESCRIPTION": html.escape(
            f"{etab.get('nom','')} — {etab.get('categorie','')} "
            f"{'à ' + etab['ville'] if etab.get('ville') else ''}. "
            f"Horaires, adresse, téléphone et itinéraire."),
        "CANONICAL": canonical,
        "PHOTO_PRINCIPALE": photo_principale,
        "TEINTE_HEX": teinte,
        "INITIALE": html.escape((etab.get("nom") or "?")[:1].upper()),
        "FOND_HERO": fond_hero,
        "BLOC_NOTE": bloc_note,
        "ACCROCHE": html.escape(_accroche(etab, types)),
        "TITRE_PRESENTATION": "La maison",
        "PRESENTATION": html.escape(_presentation(etab)),
        "HORAIRES": lignes_h,
        "HORAIRES_JSON": json.dumps(horaires, ensure_ascii=False),
        "SECTION_PHOTOS": section_photos,
        "SECTION_AVIS": section_avis,
        "TITRE_FINAL": "Une question, une table, un devis ?",
        "TEXTE_FINAL": "Un appel suffit. Nous répondons pendant les heures d'ouverture.",
        "SOCIETE": html.escape(societe),
        "LIEN_RDV": html.escape(lien_rdv),
        "DATE": _dt.date.today().strftime("%d/%m/%Y"),
        "JSONLD": _jsonld(etab, types, canonical),
    }

    page = GABARIT.read_text(encoding="utf-8")
    for cle, val in remplacements.items():
        page = page.replace("{{" + cle + "}}", str(val))

    cible = dossier / "index.html"
    cible.write_text(page, encoding="utf-8")
    return cible
