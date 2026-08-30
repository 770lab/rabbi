"""Chargement de la configuration de prospection."""

from __future__ import annotations

import json
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent
CHEMIN_DEFAUT = BASE / "prospect.config.json"
CHEMIN_EXEMPLE = BASE / "prospect.config.example.json"

DEFAUTS = {
    "identite": {
        "nom": "",
        "societe": "",
        "email": "",
        "telephone": "",
        "site": "",
        "adresse_postale": "",
    },
    "rdv": {
        "lien": "",
        "duree_min": 20,
        "fuseau": "Europe/Paris",
    },
    "offre": {
        "prix": "",
        "delai": "7 jours",
        "garantie": "",
    },
    "maquettes": {
        "base_url": "",
    },
    "zones": [],
    "categories_prioritaires": ["restaurant"],
    "categories_secondaires": [
        "bakery",
        "cafe",
        "bar",
        "hair_care",
        "beauty_salon",
        "plumber",
        "electrician",
        "car_repair",
        "dentist",
        "real_estate_agency",
    ],
    "seuils": {
        # En dessous de ce score /100, le site est jugé obsolète -> mail type B.
        "site_obsolete": 60,
        # Au dessus, on ne démarche pas : leur site est correct.
        "site_correct": 75,
        # Nombre d'avis minimum pour considérer l'établissement comme actif.
        "avis_min": 5,
    },
    "quotas": {
        "max_emails_par_jour": 40,
        "delai_relance_1_jours": 4,
        "delai_relance_2_jours": 9,
    },
    "langue": "fr",
}


def _fusion(base: dict, sur: dict) -> dict:
    """Fusion récursive : `sur` écrase `base`."""
    res = dict(base)
    for cle, val in (sur or {}).items():
        if isinstance(val, dict) and isinstance(res.get(cle), dict):
            res[cle] = _fusion(res[cle], val)
        else:
            res[cle] = val
    return res


def charger(chemin: str | Path | None = None) -> dict:
    """Charge prospect.config.json, complété par les valeurs par défaut."""
    p = Path(chemin) if chemin else CHEMIN_DEFAUT
    brut = {}
    if p.exists():
        brut = json.loads(p.read_text(encoding="utf-8"))
    elif chemin:
        raise SystemExit(f"Configuration introuvable : {p}")
    cfg = _fusion(DEFAUTS, brut)
    cfg["_chemin"] = str(p)
    return cfg


def cle_api() -> str:
    """Clé Google Places, lue dans l'environnement uniquement (jamais commitée)."""
    cle = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not cle:
        raise SystemExit(
            "GOOGLE_MAPS_API_KEY absente de l'environnement.\n"
            "  export GOOGLE_MAPS_API_KEY='...'  (console.cloud.google.com > Places API New)\n"
            "  ou utilisez --demo pour travailler sur le jeu de données d'exemple."
        )
    return cle
