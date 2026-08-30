"""Base SQLite : établissements, audits, emails, historique d'envoi."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

BASE = Path(__file__).resolve().parent
CHEMIN_DB = BASE / "out" / "prospection.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS etablissements (
    place_id        TEXT PRIMARY KEY,
    nom             TEXT NOT NULL,
    categorie       TEXT,
    types           TEXT,
    adresse         TEXT,
    ville           TEXT,
    lat             REAL,
    lng             REAL,
    telephone       TEXT,
    site            TEXT,
    note            REAL,
    nb_avis         INTEGER,
    horaires        TEXT,
    photos          TEXT,
    maps_url        TEXT,
    statut          TEXT,
    resume          TEXT,
    zone            TEXT,
    vu_le           TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audits (
    place_id        TEXT PRIMARY KEY REFERENCES etablissements(place_id),
    verdict         TEXT NOT NULL,          -- absent | obsolete | correct | injoignable
    score           INTEGER,                -- /100, NULL si pas de site
    score_fiche     INTEGER,                -- /100, complétude de la fiche Google
    defauts         TEXT,                   -- JSON [{code,libelle,poids,preuve}]
    atouts          TEXT,                   -- JSON [str]
    url_finale      TEXT,
    poids_ko        INTEGER,
    ttfb_ms         INTEGER,
    audite_le       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS contacts (
    place_id        TEXT REFERENCES etablissements(place_id),
    email           TEXT,
    source          TEXT,                   -- mailto | texte | formulaire | manuel
    generique       INTEGER DEFAULT 0,      -- 1 = contact@/info@ (RGPD B2B : à privilégier)
    PRIMARY KEY (place_id, email)
);

CREATE TABLE IF NOT EXISTS emails (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    place_id        TEXT REFERENCES etablissements(place_id),
    type            TEXT NOT NULL,          -- A_sans_site | B_site_obsolete | relance_1 | relance_2
    objet           TEXT NOT NULL,
    corps           TEXT NOT NULL,
    destinataire    TEXT,
    maquette        TEXT,
    statut          TEXT DEFAULT 'brouillon', -- brouillon | envoye | repondu | desinscrit
    draft_id        TEXT,                   -- id du brouillon Gmail
    cree_le         TEXT DEFAULT (datetime('now')),
    envoye_le       TEXT
);

CREATE TABLE IF NOT EXISTS exclusions (
    valeur          TEXT PRIMARY KEY,       -- email ou domaine
    motif           TEXT,
    ajoute_le       TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_etab_zone ON etablissements(zone);
CREATE INDEX IF NOT EXISTS idx_audit_verdict ON audits(verdict);
CREATE INDEX IF NOT EXISTS idx_emails_statut ON emails(statut);
"""


def ouvrir(chemin: str | Path | None = None) -> sqlite3.Connection:
    p = Path(chemin) if chemin else CHEMIN_DB
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def upsert_etablissement(con: sqlite3.Connection, e: dict) -> None:
    con.execute(
        """
        INSERT INTO etablissements
            (place_id, nom, categorie, types, adresse, ville, lat, lng, telephone,
             site, note, nb_avis, horaires, photos, maps_url, statut, resume, zone)
        VALUES (:place_id, :nom, :categorie, :types, :adresse, :ville, :lat, :lng, :telephone,
                :site, :note, :nb_avis, :horaires, :photos, :maps_url, :statut, :resume, :zone)
        ON CONFLICT(place_id) DO UPDATE SET
            nom=excluded.nom, categorie=excluded.categorie, types=excluded.types,
            adresse=excluded.adresse, ville=excluded.ville, telephone=excluded.telephone,
            site=excluded.site, note=excluded.note, nb_avis=excluded.nb_avis,
            horaires=excluded.horaires, photos=excluded.photos, maps_url=excluded.maps_url,
            statut=excluded.statut, resume=excluded.resume, vu_le=datetime('now')
        """,
        e,
    )


def enregistrer_audit(con: sqlite3.Connection, place_id: str, a: dict) -> None:
    con.execute(
        """
        INSERT INTO audits (place_id, verdict, score, score_fiche, defauts, atouts,
                            url_finale, poids_ko, ttfb_ms)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(place_id) DO UPDATE SET
            verdict=excluded.verdict, score=excluded.score, score_fiche=excluded.score_fiche,
            defauts=excluded.defauts, atouts=excluded.atouts, url_finale=excluded.url_finale,
            poids_ko=excluded.poids_ko, ttfb_ms=excluded.ttfb_ms, audite_le=datetime('now')
        """,
        (
            place_id,
            a["verdict"],
            a.get("score"),
            a.get("score_fiche"),
            json.dumps(a.get("defauts", []), ensure_ascii=False),
            json.dumps(a.get("atouts", []), ensure_ascii=False),
            a.get("url_finale"),
            a.get("poids_ko"),
            a.get("ttfb_ms"),
        ),
    )


def est_exclu(con: sqlite3.Connection, email: str) -> bool:
    if not email:
        return True
    domaine = email.split("@")[-1].lower()
    cur = con.execute(
        "SELECT 1 FROM exclusions WHERE valeur IN (?,?) LIMIT 1",
        (email.lower(), domaine),
    )
    return cur.fetchone() is not None
