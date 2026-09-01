"""Envoi de la base locale vers la base D1 du tableau de bord en ligne.

La machine reste la source de vérité : c'est ici que `chercher`, `auditer` et
`rediger` travaillent. Le tableau de bord en ligne sert à consulter et à marquer
depuis n'importe où — il reçoit donc une copie, réduite à ce qu'il affiche.

Ce qui ne monte jamais : le corps des mails, le détail des audits, les scores.
Moins il y a de données là-haut, moins il y a à protéger.

Le marquage fait en ligne (envoyé, a répondu, STOP) redescend avec `tirer`, à
lancer avant toute nouvelle poussée — sinon la copie locale, plus ancienne,
écraserait les statuts modifiés depuis le navigateur.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

from . import store

BASE_WEB = Path.home() / "prospect-suivi-web"
NOM_D1 = "prospect-suivi"

# Une table, ses colonnes : ni corps de mail, ni défauts d'audit.
TABLES = {
    "etablissements": "place_id,nom,categorie,ville,telephone,note,nb_avis,maps_url",
    "audits": "place_id,verdict",
    "contacts": "place_id,email,generique,meme_domaine",
    "emails": "id,place_id,type,objet,destinataire,statut,cree_le,envoye_le",
    "exclusions": "valeur,motif,ajoute_le",
}


def _litteral(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def _wrangler() -> list[str]:
    """Wrangler passe par npx : rien à installer globalement pour publier."""
    if shutil.which("wrangler"):
        return ["wrangler"]
    if shutil.which("npx"):
        return ["npx", "--yes", "wrangler@latest"]
    raise SystemExit("Ni wrangler ni npx trouvés — impossible de parler à Cloudflare.")


def _executer_sql(fichier: Path) -> None:
    cmd = _wrangler() + ["d1", "execute", NOM_D1, "--remote", f"--file={fichier}", "--yes"]
    r = subprocess.run(cmd, cwd=BASE_WEB, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"Envoi vers D1 refusé :\n{(r.stderr or r.stdout)[-1200:]}")


def pousser(chemin_db, verbose: bool = True) -> dict:
    """Remplace le contenu de D1 par celui de la base locale."""
    con = store.ouvrir(chemin_db)
    con.row_factory = sqlite3.Row
    dossier = BASE_WEB / "envois"
    dossier.mkdir(parents=True, exist_ok=True)
    fichier = dossier / "donnees.sql"

    lignes, comptes = ["-- copie de la base locale ; ne pas versionner"], {}
    for table, cols in TABLES.items():
        rows = con.execute(f"SELECT {cols} FROM {table}").fetchall()
        comptes[table] = len(rows)
        lignes.append(f"DELETE FROM {table};")
        for r in rows:
            valeurs = ",".join(_litteral(v) for v in tuple(r))
            lignes.append(f"INSERT INTO {table} ({cols}) VALUES ({valeurs});")

    fichier.write_text("\n".join(lignes) + "\n", encoding="utf-8")
    _executer_sql(fichier)
    # Le fichier contient des coordonnées de commerçants : il ne reste pas sur le
    # disque une fois parti.
    fichier.unlink(missing_ok=True)

    if verbose:
        for t, n in comptes.items():
            print(f"  {t:16s} {n:5d} ligne(s)")
        print("→ base en ligne remplacée par la copie locale.")
    return comptes


def tirer(chemin_db, verbose: bool = True) -> int:
    """Récupère les statuts marqués depuis le navigateur, et rien d'autre.

    Seuls `statut`, `envoye_le` et les exclusions changent en ligne : le reste est
    produit ici. On ne redescend donc que ça, pour ne pas écraser en local une
    donnée fraîche avec une copie plus vieille.
    """
    cmd = _wrangler() + [
        "d1", "execute", NOM_D1, "--remote", "--json", "--yes",
        "--command",
        "SELECT place_id, statut, envoye_le FROM emails;",
    ]
    r = subprocess.run(cmd, cwd=BASE_WEB, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"Lecture de D1 refusée :\n{(r.stderr or r.stdout)[-1200:]}")
    try:
        blocs = json.loads(r.stdout)
        distants = blocs[0]["results"]
    except (ValueError, KeyError, IndexError):
        raise SystemExit("Réponse de D1 illisible — rien n'a été modifié en local.")

    con = store.ouvrir(chemin_db)
    n = 0
    for d in distants:
        n += con.execute(
            """UPDATE emails SET statut=?, envoye_le=?
               WHERE place_id=? AND (statut IS NOT ? OR envoye_le IS NOT ?)""",
            (d["statut"], d["envoye_le"], d["place_id"], d["statut"], d["envoye_le"]),
        ).rowcount

    cmd_exc = _wrangler() + [
        "d1", "execute", NOM_D1, "--remote", "--json", "--yes",
        "--command", "SELECT valeur, motif, ajoute_le FROM exclusions;",
    ]
    r2 = subprocess.run(cmd_exc, cwd=BASE_WEB, capture_output=True, text=True)
    if r2.returncode == 0:
        try:
            for e in json.loads(r2.stdout)[0]["results"]:
                con.execute(
                    "INSERT OR REPLACE INTO exclusions (valeur, motif, ajoute_le) VALUES (?,?,?)",
                    (e["valeur"], e["motif"], e["ajoute_le"]),
                )
        except (ValueError, KeyError, IndexError):
            pass
    con.commit()
    if verbose:
        print(f"→ {n} statut(s) mis à jour depuis le tableau de bord en ligne.")
    return n
