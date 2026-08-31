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
    """Charge prospect.config.json, complété par les valeurs par défaut.

    On ne valide ici que la lisibilité du fichier : `chercher`, `auditer` et
    `maquette` ne s'adressent à personne et doivent rester utilisables sans
    configuration d'expédition. Le contrôle de l'expéditeur est fait par
    `exiger_expedition()`, appelée par `rediger`, `relancer` et `exporter`.
    """
    p = Path(chemin) if chemin else CHEMIN_DEFAUT
    brut = {}
    if p.exists():
        try:
            brut = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise SystemExit(
                f"Configuration illisible : {p}\n"
                f"  ligne {e.lineno}, colonne {e.colno} : {e.msg}\n"
                "  (le JSON n'accepte ni commentaire, ni virgule après le dernier élément)"
            ) from e
        if not isinstance(brut, dict):
            raise SystemExit(f"Configuration invalide : {p} doit contenir un objet JSON.")
    elif chemin:
        raise SystemExit(f"Configuration introuvable : {p}")
    cfg = _fusion(DEFAUTS, brut)
    cfg["_chemin"] = str(p)
    return cfg


# Jetons laissés par les gabarits (« A_REMPLIR » dans le fichier livré,
# « votredomaine » dans l'exemple) : ce sont des trous, pas des valeurs. Ils sont
# partis en signature et en mention légale ; ils ne doivent plus jamais sortir.
JETONS_GABARIT = ("a_remplir", "votredomaine", "votre-page-de-reservation")


def champs_expedition_manquants(cfg: dict) -> list[str]:
    """Champs d'identification de l'expéditeur vides ou restés à l'état de gabarit.

    La liste des champs obligatoires est celle de `copy.CHAMPS_EXPEDITION` : une
    seule source de vérité pour le contrat « pas d'expéditeur identifiable, pas de
    message ». L'import est tardif pour ne pas créer de dépendance au chargement.
    """
    from . import copy as copy_mod

    manquants = []
    for section, cle in copy_mod.CHAMPS_EXPEDITION:
        valeur = str((cfg.get(section) or {}).get(cle) or "").strip()
        if not valeur or any(j in valeur.lower() for j in JETONS_GABARIT):
            manquants.append(f"{section}.{cle}")
    return manquants


def exiger_expedition(cfg: dict) -> None:
    """Garde-fou de `rediger`, `relancer` et `exporter` — et d'eux seuls.

    Un mail de prospection B2B doit identifier son expéditeur et permettre de le
    joindre : sans identité, e-mail, téléphone, adresse postale et lien de
    rendez-vous, aucun brouillon n'est écrit et aucun export n'est produit.
    """
    manquants = champs_expedition_manquants(cfg)
    if not manquants:
        return
    raise SystemExit(
        "Configuration d'expédition incomplète : rien n'a été écrit ni exporté.\n"
        "Champs vides ou restés à l'état de gabarit :\n"
        + "\n".join(f"  - {m}" for m in manquants)
        + f"\n\nComplétez {cfg.get('_chemin') or CHEMIN_DEFAUT}, puis relancez.\n"
        "(`chercher`, `auditer` et `maquette` fonctionnent sans.)"
    )


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
