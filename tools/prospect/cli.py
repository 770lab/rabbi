"""Ligne de commande de la prospection.

    python3 -m prospect chercher --demo
    python3 -m prospect auditer
    python3 -m prospect maquette
    python3 -m prospect rediger
    python3 -m prospect exporter --format json
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from pathlib import Path

from . import audit as audit_mod
from . import config as config_mod
from . import copy as copy_mod
from . import enrich as enrich_mod
from . import mockup as mockup_mod
from . import places as places_mod
from . import store as store_mod

BASE = Path(__file__).resolve().parent
EXPORTS = BASE / "out" / "exports"


# --------------------------------------------------------------------------- outils
def _zones(cfg: dict, filtre: str | None) -> list[dict]:
    zs = cfg.get("zones") or []
    if not zs:
        raise SystemExit(
            "Aucune zone dans la configuration. Copiez prospect.config.example.json "
            "en prospect.config.json et renseignez au moins une zone (ville, lat, lng, rayon_m)."
        )
    if filtre:
        zs = [z for z in zs if filtre.lower() in (z.get("ville", "")).lower()]
        if not zs:
            raise SystemExit(f"Zone « {filtre} » introuvable dans la configuration.")
    return zs


def _categories(cfg: dict, args) -> list[str]:
    if args.categorie:
        return args.categorie
    if args.priorite == "prioritaires":
        return cfg["categories_prioritaires"]
    if args.priorite == "secondaires":
        return cfg["categories_secondaires"]
    return cfg["categories_prioritaires"] + cfg["categories_secondaires"]


def _ouvrir(args):
    """Ouvre la base et complète le schéma que `store.py` ne porte pas encore."""
    con = store_mod.ouvrir(args.db)
    _migrer(con)
    return con


def _migrer(con) -> None:
    """Colonnes manquantes, ajoutées ici tant que `store.SCHEMA` ne les porte pas.

    `raison` : pourquoi un site est `non_auditable` (403 ? SPA ? réseau ?) — c'est
    ce qui permet à l'opérateur de décider de repasser à la main.
    `site_audite` : le site tel qu'il était au moment de l'audit, pour périmer un
    verdict dès que la fiche Google change.
    `demarchable` : le feu vert d'expédition calculé par l'audit lui-même.
    `meme_domaine` : l'adresse appartient-elle au domaine audité, ou à un tiers
    (agence web, franchiseur) cité en pied de page ?
    """
    for table, colonne, type_sql in (("audits", "raison", "TEXT"),
                                     ("audits", "site_audite", "TEXT"),
                                     ("audits", "demarchable", "INTEGER"),
                                     ("contacts", "meme_domaine", "INTEGER DEFAULT 0")):
        noms = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        if colonne not in noms:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {colonne} {type_sql}")
    con.commit()


def _exclu(con, email: str) -> bool:
    """`store.est_exclu`, élargi aux sous-domaines.

    Une exclusion posée sur « chezmario.fr » doit couvrir « patron@mail.chezmario.fr » :
    le mail promet « je supprime définitivement votre adresse de mes fichiers », et
    c'est le seul mécanisme d'opposition du système. Une adresse vide n'est pas une
    adresse exclue : ces prospects-là se travaillent au téléphone.
    """
    email = (email or "").strip().lower()
    if not email:
        return False
    if store_mod.est_exclu(con, email):
        return True
    domaine = email.split("@")[-1]
    for (valeur,) in con.execute("SELECT valeur FROM exclusions"):
        if valeur and domaine.endswith("." + valeur):
            return True
    return False


def _prospect_exclu(con, place_id: str, destinataire: str = "") -> bool:
    """Un prospect qui a dit STOP sort de tout, y compris de la feuille d'appels.

    L'opposition arrive rarement depuis l'adresse démarchée (on écrit à contact@,
    le patron répond depuis la sienne) : on regarde le destinataire du jour, les
    adresses connues de la maison, et les mails déjà désinscrits.
    """
    if _exclu(con, destinataire):
        return True
    if con.execute("SELECT 1 FROM emails WHERE place_id=? AND statut='desinscrit' LIMIT 1",
                   (place_id,)).fetchone():
        return True
    for (email,) in con.execute("SELECT email FROM contacts WHERE place_id=?", (place_id,)):
        if _exclu(con, email):
            return True
    return False


def _ecrire_email(con, place_id: str, mail: dict, destinataire: str,
                  url_maquette: str, reecrire: bool = False) -> bool:
    """Seule porte d'entrée de la table `emails` : aucun chemin ne contourne le STOP.

    Refuse aussi le doublon : rejouer `rediger` ne doit pas produire un second
    exemplaire du même mail au même commerçant. Renvoie True si une ligne a été
    écrite.
    """
    if _prospect_exclu(con, place_id, destinataire):
        return False
    existant = con.execute(
        "SELECT id, statut FROM emails WHERE place_id=? AND type=? ORDER BY id LIMIT 1",
        (place_id, mail["type"]),
    ).fetchone()
    if existant:
        if not reecrire or existant["statut"] != "brouillon":
            return False          # déjà écrit, ou déjà parti : on ne double pas
        con.execute(
            """UPDATE emails SET objet=?, corps=?, destinataire=?, maquette=?,
                                 cree_le=datetime('now') WHERE id=?""",
            (mail["objet"], mail["corps"], destinataire, url_maquette, existant["id"]),
        )
        return True
    con.execute(
        """INSERT INTO emails (place_id,type,objet,corps,destinataire,maquette)
           VALUES (?,?,?,?,?,?)""",
        (place_id, mail["type"], mail["objet"], mail["corps"], destinataire, url_maquette),
    )
    return True


# Contrôle de publication des maquettes : une requête par URL au plus, et on
# abandonne le contrôle dès que c'est le réseau qui manque (inutile d'attendre
# le délai de garde une fois par prospect).
_PUBLICATION: dict[str, bool] = {}
_RESEAU = {"hs": False}


def _maquette_publiee(url: str) -> bool:
    """La page est-elle réellement servie ? Le mail affirme « elle est en ligne ».

    Générer un fichier dans `out/maquettes/` ne le met pas en ligne : rien dans ce
    paquet ne publie. Tant que l'URL ne répond pas, le mail part sans lien plutôt
    qu'avec une promesse en 404 — le lien est sa seule preuve.
    """
    if url in _PUBLICATION:
        return _PUBLICATION[url]
    if _RESEAU["hs"]:
        return False
    ok = False
    for methode in ("HEAD", "GET"):
        req = urllib.request.Request(url, method=methode,
                                     headers={"User-Agent": audit_mod.UA})
        try:
            with urllib.request.urlopen(req, timeout=6) as r:
                ok = 200 <= r.status < 300
            break
        except urllib.error.HTTPError as e:
            if methode == "HEAD" and e.code in (405, 501):
                continue          # serveur qui refuse HEAD : on retente en GET
            ok = 200 <= e.code < 300
            break
        except OSError:
            # DNS, TLS, coupure : on ne sait pas, donc on n'affirme rien.
            _RESEAU["hs"] = True
            break
    _PUBLICATION[url] = ok
    return ok


def _maquette_avec_photos(dossier) -> bool:
    """Le mail ne parle de « vos photos » que si la maquette en contient vraiment."""
    return dossier.is_dir() and any(dossier.glob("photo-*.jpg"))


def _quota_epuise(exc: BaseException) -> bool:
    """Un 429 ne se rattrape pas en passant à la catégorie suivante : on s'arrête."""
    m = str(exc).lower()
    return "429" in m or "resource_exhausted" in m or "quota" in m


def score_opportunite(etab: dict, a: dict | None) -> int:
    """Qui appeler en premier : traction commerciale × marge de progression."""
    s = min(etab.get("nb_avis") or 0, 200) / 4          # 0 → 50
    note = etab.get("note") or 0
    if note:
        s += max(0.0, min((note - 3.4) * 18, 28))        # 0 → 28
    if a:
        if a.get("verdict") == "absent":
            s += 40
        elif a.get("verdict") == "injoignable":
            s += 45
        elif a.get("verdict") == "obsolete":
            s += 34 * (1 - (a.get("score") or 0) / 100)
        fiche = a.get("score_fiche")
        if isinstance(fiche, int):
            s += (100 - fiche) * 0.12
    if (etab.get("telephone") or "").strip():
        s += 4
    if (etab.get("statut") or "") not in ("", "OPERATIONAL"):
        s -= 60                                          # fermé définitivement
    return int(max(0, min(round(s), 150)))


# --------------------------------------------------------------------------- chercher
def cmd_chercher(args, cfg):
    con = _ouvrir(args)
    total = 0        # fiches enregistrées (nouvelles ou revues)
    nouveaux = 0     # insertions réelles : c'est ce que --limite doit borner

    if args.demo:
        for brut in places_mod.charger_demo():
            e = places_mod.normaliser(brut, "Démo")
            store_mod.upsert_etablissement(con, e)
            total += 1
        con.commit()
        print(f"[démo] {total} établissements chargés depuis le jeu d'essai.")
        return

    cle = config_mod.cle_api()
    limite = args.limite or 0
    echecs = 0
    quota_epuise = False

    def _ranger(lot: list[dict], ville: str, vus: set) -> None:
        """Enregistre et commite au fil de l'eau : un appel déjà payé n'est jamais jeté."""
        nonlocal total, nouveaux
        for brut in lot:
            pid = brut.get("id")
            if not pid or pid in vus:
                continue
            vus.add(pid)
            e = places_mod.normaliser(brut, ville)
            if (e.get("nb_avis") or 0) < cfg["seuils"]["avis_min"]:
                continue
            connu = con.execute("SELECT 1 FROM etablissements WHERE place_id=?",
                                (pid,)).fetchone()
            store_mod.upsert_etablissement(con, e)
            total += 1
            nouveaux += 0 if connu else 1
        con.commit()

    for zone in _zones(cfg, args.zone):
        if quota_epuise or (limite and nouveaux >= limite):
            break
        ville = zone.get("ville", "")
        for cat in _categories(cfg, args):
            if quota_epuise or (limite and nouveaux >= limite):
                break
            vus = set()
            requete = f"{cat.replace('_', ' ')} à {ville}".strip()
            print(f"→ {requete} ({zone['lat']:.4f},{zone['lng']:.4f} r={zone.get('rayon_m')}m)")
            try:
                _ranger(places_mod.recherche_texte(requete, zone, cle), ville, vus)
            except (SystemExit, OSError) as exc:
                # OSError couvre URLError et TimeoutError : une coupure réseau ne
                # doit pas emporter le balayage entier en traceback.
                echecs += 1
                print(f"  ! {exc}", file=sys.stderr)
                quota_epuise = quota_epuise or _quota_epuise(exc)
            for tuile in (places_mod.pavage(zone, args.pavage) if args.pavage else []):
                if quota_epuise or (limite and nouveaux >= limite):
                    break
                try:
                    _ranger(places_mod.recherche_proximite([cat], tuile, cle), ville, vus)
                except (SystemExit, OSError) as exc:
                    echecs += 1
                    print(f"  ! {exc}", file=sys.stderr)
                    quota_epuise = quota_epuise or _quota_epuise(exc)
                time.sleep(0.15)
            print(f"  {len(vus)} fiches vues")

    if quota_epuise:
        print("! Quota Google Places épuisé : le balayage s'arrête ici. Tout ce qui a été "
              "obtenu est déjà en base, rien n'est perdu — reprenez plus tard.",
              file=sys.stderr)
    print(f"{total} établissement(s) enregistré(s) dans la base, dont {nouveaux} nouveau(x).")
    if echecs:
        print(f"{echecs} appel(s) en échec (lignes « ! » ci-dessus) : le quartier n'est "
              f"peut-être pas vu en entier.")
    print(places_mod.resume_appels())


def _serveur_demo(port: int = 8765):
    """Sert les faux sites de tools/prospect/fixtures/sites pour la démo hors ligne."""
    import http.server
    import threading

    dossier = Path(__file__).parent / "fixtures" / "sites"

    class Silencieux(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(dossier), **k)

        def log_message(self, *a):
            pass

    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Silencieux)
    except OSError as exc:
        # Les URL du jeu d'essai référencent ce port en dur : on ne peut pas en
        # changer ici, mais on peut le dire au lieu d'un traceback.
        raise SystemExit(
            f"Le port {port} est déjà occupé ({exc.strerror}) : impossible de servir les "
            f"faux sites de la démo.\n"
            f"  Libérez-le (lsof -ti tcp:{port} | xargs kill), puis relancez."
        ) from exc
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# --------------------------------------------------------------------------- auditer
def cmd_auditer(args, cfg):
    con = _ouvrir(args)
    srv = _serveur_demo() if getattr(args, "demo", False) else None
    fraicheur = int(getattr(args, "fraicheur", 30) or 0)
    sql = """SELECT e.* FROM etablissements e
             LEFT JOIN audits a ON a.place_id = e.place_id
             WHERE (e.statut IS NULL OR e.statut = '' OR e.statut = 'OPERATIONAL')"""
    if not args.refaire:
        # Un verdict ne survit ni à un changement de site sur la fiche (sinon on
        # écrit « votre bouton Site Web est vide » à quelqu'un qui vient d'en
        # ouvrir un), ni indéfiniment.
        sql += """ AND (a.place_id IS NULL
                        OR ifnull(a.site_audite,'') <> ifnull(e.site,'')"""
        if fraicheur:
            sql += f" OR a.audite_le < datetime('now','-{fraicheur} days')"
        sql += ")"
    sql += " ORDER BY e.nb_avis DESC"
    if args.limite:
        sql += f" LIMIT {int(args.limite)}"

    lignes = con.execute(sql).fetchall()
    print(f"{len(lignes)} établissement(s) à auditer.")
    compteur = {"absent": 0, "obsolete": 0, "correct": 0,
                "injoignable": 0, "non_auditable": 0}
    marque = {"absent": "○", "obsolete": "▲", "correct": "●",
              "injoignable": "✕", "non_auditable": "?"}

    try:
        for i, row in enumerate(lignes, 1):
            e = dict(row)
            score_fiche, manques = audit_mod.auditer_fiche(e)
            site = (e.get("site") or "").strip()
            if site:
                res = audit_mod.auditer_site(site, cfg["seuils"]["site_obsolete"],
                                             ville=e.get("ville"),
                                             categorie=e.get("categorie"),
                                             seuils=cfg["seuils"])
            else:
                res = {"verdict": "absent", "score": None, "atouts": [],
                       "defauts": [audit_mod._defaut(
                           "aucun_site", "Aucun site web sur la fiche Google", 0,
                           "champ « site web » vide sur la fiche Google",
                           "Le bouton « Site Web » de votre fiche Google est vide : "
                           "les clients qui vous découvrent n'ont nulle part où aller.")]}
            # Les manques de la fiche valent pour tous les verdicts (une fiche sans
            # horaires coûte des clients même avec un bon site) — sauf `non_auditable`,
            # où l'on n'a rien mesuré et où l'on n'écrira rien.
            if res["verdict"] != "non_auditable":
                utiles = [m for m in manques if m["code"] != "fiche_sans_site"]
                res["defauts"] = sorted(res["defauts"] + utiles,
                                        key=lambda d: -d.get("poids", 0))
            res["score_fiche"] = score_fiche
            store_mod.enregistrer_audit(con, e["place_id"], res)
            # `store.enregistrer_audit` ne connaît pas encore ces trois colonnes.
            con.execute(
                "UPDATE audits SET raison=?, site_audite=?, demarchable=? WHERE place_id=?",
                (res.get("raison", ""), site, int(res.get("demarchable", True)),
                 e["place_id"]))
            compteur[res["verdict"]] = compteur.get(res["verdict"], 0) + 1
            print(f"  [{i}/{len(lignes)}] {marque.get(res['verdict'],'?')} {e['nom'][:38]:<38} "
                  f"{res['verdict']:<14} site={res.get('score') if res.get('score') is not None else '—'}"
                  f" fiche={score_fiche}")
            if res.get("raison"):
                print(f"          ↳ {res['raison']}")
            con.commit()
            if site:
                time.sleep(args.pause)
    finally:
        if srv:
            srv.shutdown()
            srv.server_close()

    print("\nRécapitulatif : "
          + (" · ".join(f"{k} {v}" for k, v in compteur.items() if v)
             or "rien à auditer — les verdicts en base sont à jour "
                "(`--refaire` pour tout reprendre, `--fraicheur JOURS` pour élargir)"))
    if compteur.get("non_auditable"):
        print(f"{compteur['non_auditable']} site(s) non auditable(s) : on ne sait pas ce "
              f"qu'ils valent (403, délai dépassé, page rendue par le navigateur…). "
              f"Aucun mail ne sera écrit pour eux — la raison est en base, colonne `raison`.")


# --------------------------------------------------------------------------- enrichir
def cmd_enrichir(args, cfg):
    con = _ouvrir(args)
    lignes = con.execute(
        """SELECT e.* FROM etablissements e
           JOIN audits a ON a.place_id = e.place_id
           LEFT JOIN contacts c ON c.place_id = e.place_id
           WHERE a.verdict IN ('absent','obsolete','injoignable')
             AND c.place_id IS NULL AND e.site <> ''
           ORDER BY e.nb_avis DESC LIMIT ?""",
        (args.limite or 100,),
    ).fetchall()
    print(f"{len(lignes)} site(s) à explorer pour trouver une adresse de contact.")
    for row in lignes:
        e = dict(row)
        emails = enrich_mod.trouver_emails(e["site"])
        for info in emails[:3]:
            if _exclu(con, info["email"]):
                continue
            # `meme_domaine` est le seul signal qui distingue l'adresse de la maison
            # de celle d'un tiers cité sur le site (agence web, franchiseur) : il est
            # calculé par enrich, il doit être gardé pour choisir le destinataire.
            con.execute(
                """INSERT OR IGNORE INTO contacts
                       (place_id,email,source,generique,meme_domaine)
                   VALUES (?,?,?,?,?)""",
                (e["place_id"], info["email"], info["source"], info["generique"],
                 info.get("meme_domaine", 0)),
            )
        con.commit()
        print(f"  {e['nom'][:40]:<40} {', '.join(i['email'] for i in emails[:2]) or '—'}")
        time.sleep(args.pause)
    print("\nRappel : les établissements sans site n'ont pas d'email récupérable ici. "
          "Pour eux, le téléphone de la fiche Google reste le meilleur canal "
          "(voir `exporter --format csv`).")


# --------------------------------------------------------------------------- maquette
def cmd_maquette(args, cfg):
    con = _ouvrir(args)
    cle = None
    if args.photos:
        cle = config_mod.cle_api()
    limite = args.limite or 20
    # `non_auditable` reste dehors : on ne refait pas la page de quelqu'un dont on
    # n'a pas pu voir le site.
    eligibles = con.execute(
        """SELECT count(*) FROM etablissements e JOIN audits a ON a.place_id = e.place_id
           WHERE a.verdict IN ('absent','obsolete','injoignable')
             AND ifnull(a.demarchable,1) = 1"""
    ).fetchone()[0]
    lignes = con.execute(
        """SELECT e.*, a.verdict, a.score, a.score_fiche FROM etablissements e
           JOIN audits a ON a.place_id = e.place_id
           WHERE a.verdict IN ('absent','obsolete','injoignable')
             AND ifnull(a.demarchable,1) = 1
           ORDER BY e.nb_avis DESC LIMIT ?""",
        (limite,),
    ).fetchall()
    for row in lignes:
        e = dict(row)
        chemin = mockup_mod.generer(e, cfg, cle_api=cle)
        print(f"  {e['nom'][:40]:<40} → {chemin.relative_to(BASE.parents[1])}")
    print(f"\n{len(lignes)} maquette(s) générée(s) dans tools/prospect/out/maquettes/.")
    reste = eligibles - len(lignes)
    if reste > 0:
        print(f"⚠ {reste} prospect(s) éligible(s) restent sans maquette (limite {limite}) : "
              f"leur mail partira sans lien. Relancez avec --limite {eligibles}.")
    print("Aperçu local :  python3 -m http.server -d tools/prospect/out/maquettes 8080")
    print("Ces pages ne sont PAS en ligne tant que personne ne les publie : `rediger` "
          "vérifie l'URL avant d'écrire « elle est en ligne ».")


# --------------------------------------------------------------------------- rediger
def cmd_rediger(args, cfg):
    config_mod.exiger_expedition(cfg)
    con = _ouvrir(args)
    creneaux = None
    if args.creneaux:
        creneaux = json.loads(Path(args.creneaux).read_text(encoding="utf-8"))
        if isinstance(creneaux, dict):
            creneaux = creneaux.get("creneaux", [])

    base_maquette = (cfg.get("maquettes", {}).get("base_url") or "").rstrip("/")
    reecrire = bool(getattr(args, "reecrire", False))
    # `non_auditable` n'entre pas ici : sans défaut mesuré, il n'y a rien à écrire.
    # `demarchable` est le feu vert que l'audit calcule lui-même (il est faux dès
    # qu'on n'a pas pu voir la page) : on ne passe jamais outre.
    sql = """SELECT e.*, a.verdict, a.score, a.score_fiche, a.defauts, a.audite_le
             FROM etablissements e JOIN audits a ON a.place_id = e.place_id
             WHERE a.verdict IN ('absent','obsolete','injoignable')
               AND ifnull(a.demarchable,1) = 1"""
    if not reecrire:
        # Un prospect déjà démarché ne l'est pas deux fois : rejouer la commande
        # (ou le pipeline) ne doit pas produire un second exemplaire du même mail.
        sql += " AND NOT EXISTS (SELECT 1 FROM emails m WHERE m.place_id = e.place_id)"
    sql += " ORDER BY e.nb_avis DESC"
    lignes = con.execute(sql).fetchall()
    deja = con.execute(
        """SELECT count(DISTINCT e.place_id) FROM etablissements e
           JOIN audits a ON a.place_id = e.place_id
           JOIN emails m ON m.place_id = e.place_id
           WHERE a.verdict IN ('absent','obsolete','injoignable')
             AND ifnull(a.demarchable,1) = 1"""
    ).fetchone()[0]

    prospects = []
    for row in lignes:
        e = dict(row)
        e["_score_opportunite"] = score_opportunite(e, e)
        prospects.append(e)
    prospects.sort(key=lambda x: -x["_score_opportunite"])
    if args.limite:
        prospects = prospects[: args.limite]

    # Le quota est une limite, pas un conseil : on ne prépare pas plus de mails
    # qu'on ne peut en envoyer aujourd'hui (voir la commande `marquer envoye`).
    quota = int(cfg["quotas"].get("max_emails_par_jour") or 0)
    envoyes_24h = con.execute(
        "SELECT count(*) FROM emails WHERE envoye_le > datetime('now','-1 day')"
    ).fetchone()[0]
    reportes = 0
    if quota:
        reste = max(quota - envoyes_24h, 0)
        if len(prospects) > reste:
            reportes = len(prospects) - reste
            prospects = prospects[:reste]

    ecrits, sans_maquette, exclus = 0, 0, 0
    for e in prospects:
        contact = con.execute(
            """SELECT email, generique FROM contacts WHERE place_id=?
               ORDER BY meme_domaine DESC, generique DESC LIMIT 1""",
            (e["place_id"],),
        ).fetchone()
        destinataire = contact["email"] if contact else ""
        if _prospect_exclu(con, e["place_id"], destinataire):
            exclus += 1
            continue

        # Le lien de la maquette est l'unique preuve du mail : il n'y est que si la
        # page existe ET répond. Sinon les gabarits ont déjà leur variante sans lien.
        slug = mockup_mod.slug_pour(e)
        dossier = mockup_mod.SORTIE / slug
        url_maquette = ""
        if (dossier / "index.html").exists() and base_maquette:
            candidate = f"{base_maquette}/{slug}/"
            if _maquette_publiee(candidate):
                url_maquette = candidate
        if not url_maquette:
            sans_maquette += 1

        mail = copy_mod.rediger(
            e, e, cfg, url_maquette, creneaux,
            maquette_photos=bool(url_maquette) and _maquette_avec_photos(dossier),
            contact_generique=bool(contact["generique"]) if contact else None,
        )
        if not mail:
            continue
        if not _ecrire_email(con, e["place_id"], mail, destinataire, url_maquette, reecrire):
            continue
        ecrits += 1
        if args.afficher:
            print("=" * 78)
            print(f"À : {destinataire or '(téléphone uniquement : ' + (e.get('telephone') or '—') + ')'}"
                  f"   [priorité {e['_score_opportunite']}]")
            print(f"Objet : {mail['objet']}\n")
            print(mail["corps"])
            print()
    con.commit()
    print(f"{ecrits} brouillon(s) enregistré(s) dans la table `emails` (statut brouillon).")
    if sans_maquette:
        print(f"  · {sans_maquette} sans lien de maquette : la page n'est pas générée, ou "
              f"pas encore en ligne. Le mail ne l'annonce donc pas.")
    if exclus:
        print(f"  · {exclus} prospect(s) écarté(s) : adresse ou domaine sur la liste STOP.")
    if reportes:
        print(f"  · {reportes} prospect(s) reporté(s) : quota de {quota} envoi(s)/jour, "
              f"{envoyes_24h} déjà parti(s) dans les 24 h. Relancez `rediger` demain.")
    if deja and not reecrire:
        print(f"  · {deja} prospect(s) ont déjà leur mail et ne sont pas redémarchés "
              f"(`rediger --reecrire` reprend les brouillons qui ne sont pas partis).")


# --------------------------------------------------------------------------- relancer
def cmd_relancer(args, cfg):
    config_mod.exiger_expedition(cfg)
    con = _ouvrir(args)
    delai = cfg["quotas"][f"delai_relance_{args.rang}_jours"]
    # Un site injoignable se relance comme les autres ; un prospect qui a répondu
    # ou s'est désinscrit, jamais.
    lignes = con.execute(
        f"""SELECT m.*, e.nom, e.ville, e.note, e.nb_avis, e.telephone, e.place_id AS pid
            FROM emails m JOIN etablissements e ON e.place_id = m.place_id
            WHERE m.statut = 'envoye'
              AND m.type IN ('A_sans_site','B_site_obsolete','I_site_injoignable')
              AND julianday('now') - julianday(m.envoye_le) >= {int(delai)}
              AND NOT EXISTS (SELECT 1 FROM emails r WHERE r.place_id = m.place_id
                              AND (r.type = 'relance_{args.rang}'
                                   OR r.statut IN ('repondu','desinscrit')))
            ORDER BY m.id""",
    ).fetchall()

    faites, exclues, vus = 0, 0, set()
    for row in lignes:
        if row["pid"] in vus:
            continue
        vus.add(row["pid"])
        if _prospect_exclu(con, row["pid"], row["destinataire"]):
            exclues += 1
            continue
        e = {"place_id": row["pid"], "nom": row["nom"], "ville": row["ville"],
             "note": row["note"], "nb_avis": row["nb_avis"]}
        # « La maquette est toujours en ligne » : on le revérifie, elle a pu tomber.
        maquette = row["maquette"] or ""
        if maquette and not _maquette_publiee(maquette):
            maquette = ""
        contact = con.execute(
            "SELECT generique FROM contacts WHERE place_id=? AND email=?",
            (row["pid"], row["destinataire"] or ""),
        ).fetchone()
        r = copy_mod.relance(e, {"objet": row["objet"]}, cfg, args.rang, maquette,
                             contact_generique=bool(contact["generique"]) if contact else None)
        if not _ecrire_email(con, row["pid"], r, row["destinataire"], maquette):
            continue
        faites += 1
        print(f"  relance {args.rang} : {row['nom']}")
    con.commit()
    print(f"{faites} relance(s) préparée(s).")
    if exclues:
        print(f"  · {exclues} non relancé(s) : adresse ou domaine sur la liste STOP.")


# --------------------------------------------------------------------------- exporter
# Seuls ces deux formats reprennent l'identité de l'expéditeur : `json` sort les
# brouillons prêts à coller dans Gmail, `eml` fabrique des messages avec un
# en-tête From. `csv` est un dump de la base et `appels` une feuille d'appel
# téléphonique : ni l'un ni l'autre n'envoie quoi que ce soit, et bloquer celui
# qui veut juste sa liste d'appels derrière une page de réservation n'a pas de
# sens.
FORMATS_EXPEDITION = ("json", "eml", "mail")

# Une accroche par verdict démarchable, et rien d'autre. La clé sert deux fois :
# elle fournit la phrase, et elle définit à elle seule qui figure sur la feuille
# d'appel. `correct` et `non_auditable` en sont absents — pour l'un on n'a rien à
# reprocher, pour l'autre on n'a rien vu.
ACCROCHES_APPEL = {
    "absent": "vous n'aviez pas de site.",
    "injoignable": "le lien de votre fiche ne répond plus.",
    "obsolete": "votre site avait pris un coup de vieux.",
}


# AppleScript de dépôt : il enregistre un brouillon et s'arrête là. Pas de `send`
# dans ce fichier — l'envoi reste un geste humain, comme le reste de la chaîne.
_APPLESCRIPT_BROUILLON = """
on run argv
  set leSujet to item 1 of argv
  set leCorps to item 2 of argv
  set lExpediteur to item 3 of argv
  set leDestinataire to item 4 of argv
  tell application "Mail"
    set m to make new outgoing message with properties {subject:leSujet, content:leCorps, visible:false}
    tell m
      set sender to lExpediteur
      if leDestinataire is not "" then
        make new to recipient at end of to recipients with properties {address:leDestinataire}
      end if
    end tell
    save m
  end tell
  return "ok"
end run
"""


def _deposer_dans_mail(objet: str, corps: str, expediteur: str, destinataire: str) -> str:
    """Dépose un brouillon dans Mail.app (macOS). Rend '' si tout va bien, sinon l'erreur."""
    try:
        r = subprocess.run(["osascript", "-", objet, corps, expediteur, destinataire or ""],
                           input=_APPLESCRIPT_BROUILLON, capture_output=True, text=True,
                           timeout=30)
    except FileNotFoundError:
        return "osascript introuvable : le format « mail » ne marche que sur macOS."
    except subprocess.TimeoutExpired:
        return "Mail n'a pas répondu en 30 s."
    return "" if r.returncode == 0 else (r.stderr or "").strip()


def cmd_exporter(args, cfg):
    if args.format in FORMATS_EXPEDITION:
        config_mod.exiger_expedition(cfg)
    con = _ouvrir(args)
    EXPORTS.mkdir(parents=True, exist_ok=True)
    lignes = con.execute(
        """SELECT e.*, a.verdict, a.score, a.score_fiche, a.defauts, a.atouts,
                  m.id AS mail_id, m.type AS mail_type, m.objet, m.corps,
                  m.destinataire, m.maquette, m.statut AS mail_statut
           FROM etablissements e
           LEFT JOIN audits a ON a.place_id = e.place_id
           LEFT JOIN emails m ON m.place_id = e.place_id AND m.statut = 'brouillon'
           ORDER BY e.nb_avis DESC"""
    ).fetchall()
    donnees = []
    for row in lignes:
        d = dict(row)
        d["priorite"] = score_opportunite(d, d)
        donnees.append(d)
    donnees.sort(key=lambda x: -x["priorite"])
    # La liste STOP prime sur tous les formats : une adresse (ou un domaine, ou un
    # sous-domaine) exclue ne ressort d'ici sous aucune forme.
    avant = len(donnees)
    donnees = [d for d in donnees
               if not _prospect_exclu(con, d["place_id"], d.get("destinataire") or "")]
    exclus = avant - len(donnees)
    if exclus:
        print(f"({exclus} ligne(s) retirée(s) : adresse ou domaine sur la liste STOP)")
    if args.verdict:
        donnees = [d for d in donnees if d.get("verdict") == args.verdict]
    if args.limite:
        donnees = donnees[: args.limite]
    # Trois des quatre formats servent à solliciter quelqu'un : `non_auditable`
    # veut dire « on ne sait pas », il n'y a rien à dire à ces prospects-là.
    sollicitables = [d for d in donnees if d.get("verdict") != "non_auditable"]
    # Le téléphone est le seul canal qui ne passe pas par `copy.rediger`, lequel
    # renvoie None sur `correct`. Sans ce filtre, la feuille d'appel démarcherait
    # des sites que l'audit vient de déclarer bons, avec un reproche que l'audit
    # contredit. Règle du projet : verdict `correct`, on ne démarche pas.
    appelables = [d for d in sollicitables if d.get("verdict") in ACCROCHES_APPEL]

    if args.format == "csv":
        cible = EXPORTS / "prospects.csv"
        champs = ["priorite", "nom", "categorie", "ville", "telephone", "site", "note",
                  "nb_avis", "verdict", "score", "score_fiche", "destinataire", "maquette",
                  "objet", "maps_url"]
        with cible.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=champs, extrasaction="ignore")
            w.writeheader()
            w.writerows(donnees)
        print(f"→ {cible.relative_to(BASE.parents[1])} ({len(donnees)} lignes)")

    elif args.format == "json":
        cible = EXPORTS / "brouillons.json"
        utiles = [
            {"place_id": d["place_id"], "nom": d["nom"], "priorite": d["priorite"],
             "verdict": d["verdict"], "telephone": d["telephone"],
             "destinataire": d["destinataire"], "objet": d["objet"], "corps": d["corps"],
             "maquette": d["maquette"], "type": d["mail_type"], "mail_id": d["mail_id"],
             "defauts": json.loads(d["defauts"] or "[]")[:3]}
            for d in sollicitables if d.get("objet")
        ]
        cible.write_text(json.dumps(utiles, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"→ {cible.relative_to(BASE.parents[1])} ({len(utiles)} brouillons prêts pour Gmail)")

    elif args.format == "appels":
        # La majorité des fiches sans site n'ont aucun email récupérable :
        # pour celles-là, le téléphone reste le seul canal. Voici la feuille d'appel.
        cible = EXPORTS / "feuille-d-appels.md"
        lignes_md = ["# Feuille d'appels", "",
                     "Ordre de priorité. Une ligne = un appel. Notez l'issue à droite.", ""]
        ecartes = len(sollicitables) - len(appelables)
        for d in appelables:
            if not (d.get("telephone") or "").strip():
                continue
            defauts = json.loads(d.get("defauts") or "[]")[:2]
            slug = mockup_mod.slug_pour(d)
            page = mockup_mod.SORTIE / slug / "index.html"
            # Ce qu'on met dans la bouche de l'appelant ne dit que ce qui a été
            # mesuré : pas de compliment sur une note moyenne, pas de « votre site a
            # pris un coup de vieux » pour une page que personne n'a pu ouvrir, pas
            # de maquette annoncée tant qu'elle n'est pas générée.
            note, avis = d.get("note"), d.get("nb_avis") or 0
            eloge = (", très bien notée" if note and note >= copy_mod.NOTE_ELOGIEUSE
                     and avis >= copy_mod.AVIS_ELOGIEUX else "")
            vue = f" — {str(note).replace('.', ',')}/5 sur {avis} avis{eloge} —" if note else ""
            # Pas de valeur par défaut : chaque accroche est écrite pour un
            # verdict précis, et un verdict sans accroche n'est pas appelé du tout
            # (voir `appelables`). Un reproche générique, c'est un reproche non
            # mesuré — exactement ce que le reste de la chaîne s'interdit.
            constat = ACCROCHES_APPEL.get(d.get("verdict"))
            if not constat:
                continue
            suite = " J'ai fait une page pour vous montrer. »" if page.exists() else " »"
            lignes_md += [
                f"## {d['priorite']} · {d['nom']} — {d['telephone']}",
                f"*{d.get('categorie') or ''} · {d.get('ville') or ''} · "
                f"{d.get('note') or '—'}/5 sur {avis} avis · "
                f"verdict : {d.get('verdict') or '—'}*",
                "",
                "**Accroche** : « Bonjour, je vous appelle parce que j'ai regardé votre "
                f"fiche Google{vue} et j'ai vu que {constat}{suite}",
                "",
            ]
            for df in defauts:
                lignes_md.append(f"- {df['libelle']}"
                                 + (f" ({df['preuve']})" if df.get("preuve") else ""))
            lignes_md += ["", (f"- Maquette : `out/maquettes/{slug}/index.html`"
                               if page.exists() else
                               "- Maquette : pas encore générée (`maquette --limite N`)"),
                          f"- Fiche : {d.get('maps_url') or '—'}",
                          "- Issue : [ ] rappeler  [ ] rendez-vous  [ ] non  [ ] STOP", "", "---", ""]
        cible.write_text("\n".join(lignes_md), encoding="utf-8")
        print(f"→ {cible.relative_to(BASE.parents[1])} "
              f"({sum(1 for d in appelables if (d.get('telephone') or '').strip())} appels)")
        if ecartes:
            print(f"  · {ecartes} écarté(s) : leur site fait le travail (verdict "
                  f"« correct »), on ne les démarche pas.")

    elif args.format == "eml":
        dossier = EXPORTS / "eml"
        dossier.mkdir(parents=True, exist_ok=True)
        n = 0
        ecrits = set()
        for d in sollicitables:
            if not d.get("objet"):
                continue
            msg = EmailMessage()
            msg["Subject"] = d["objet"]
            msg["From"] = f"{cfg['identite'].get('nom','')} <{cfg['identite'].get('email','')}>"
            if d.get("destinataire"):
                msg["To"] = d["destinataire"]
            msg["X-Prospection-Place-Id"] = d["place_id"]
            msg.set_content(d["corps"])
            # Un slug par place_id : deux homonymes ne s'écrasent plus l'un l'autre.
            fichier = dossier / f"{mockup_mod.slug_pour(d)}.eml"
            fichier.write_bytes(msg.as_bytes())
            ecrits.add(fichier)
            n += 1
        print(f"→ {dossier.relative_to(BASE.parents[1])}/ ({len(ecrits)} fichiers .eml "
              f"écrits sur {n} brouillon(s), glissables dans Gmail)")

    elif args.format == "mail":
        expediteur = cfg["identite"].get("email", "")
        deposes, sans_dest, echecs = 0, 0, []
        for d in sollicitables:
            if not d.get("objet"):
                continue
            erreur = _deposer_dans_mail(d["objet"], d["corps"], expediteur,
                                        d.get("destinataire") or "")
            if erreur:
                echecs.append((d["nom"], erreur))
                # Mail refuse en général pour la même raison à chaque fois (compte
                # absent, permission d'automatisation) : inutile d'insister 40 fois.
                if len(echecs) >= 3:
                    break
                continue
            deposes += 1
            if not d.get("destinataire"):
                sans_dest += 1
        print(f"→ {deposes} brouillon(s) déposé(s) dans Mail, expéditeur {expediteur}")
        if sans_dest:
            print(f"  · {sans_dest} sans destinataire : `enrichir` n'a pas trouvé "
                  f"d'adresse, à compléter à la main avant d'envoyer.")
        for nom, err in echecs:
            print(f"  ⚠️ {nom} : {err.splitlines()[0] if err else 'échec'}")
        if echecs:
            print("  Vérifiez que Mail est ouvert, que le compte "
                  f"« {expediteur} » y est configuré, et que Terminal est autorisé à "
                  "piloter Mail (Réglages → Confidentialité → Automatisation).")


# --------------------------------------------------------------------------- divers
def cmd_liste(args, cfg):
    con = _ouvrir(args)
    lignes = con.execute(
        """SELECT e.nom, e.ville, e.categorie, e.note, e.nb_avis, e.telephone, e.site,
                  e.statut, a.verdict, a.score, a.score_fiche
           FROM etablissements e LEFT JOIN audits a ON a.place_id = e.place_id"""
    ).fetchall()
    donnees = sorted(({**dict(r), "priorite": score_opportunite(dict(r), dict(r))}
                      for r in lignes), key=lambda x: -x["priorite"])
    if args.verdict:
        donnees = [d for d in donnees if d.get("verdict") == args.verdict]
    if getattr(args, "sans_site", False):
        donnees = [d for d in donnees if not (d.get("site") or "").strip()]
    if getattr(args, "avec_site", False):
        donnees = [d for d in donnees if (d.get("site") or "").strip()]
    print(f"{'prio':>4}  {'établissement':<32} {'ville':<13} {'avis':>5}  {'site ?':<8} "
          f"{'verdict':<14} {'note':>5} {'fiche':>5}")
    print("-" * 100)
    for d in donnees[: args.limite or 50]:
        a_un_site = "oui" if (d.get("site") or "").strip() else "AUCUN"
        print(f"{d['priorite']:>4}  {(d['nom'] or '')[:32]:<32} {(d['ville'] or '')[:13]:<13} "
              f"{d['nb_avis'] or 0:>5}  {a_un_site:<8} {(d['verdict'] or '—'):<14} "
              f"{(d['score'] if d['score'] is not None else '—'):>5} "
              f"{(d['score_fiche'] if d['score_fiche'] is not None else '—'):>5}")

    sans = sum(1 for d in donnees if not (d.get("site") or "").strip())
    print(f"\n{len(donnees)} établissement(s) · {sans} sans site · {len(donnees)-sans} avec site")
    inconnus = sum(1 for d in donnees if d.get("verdict") == "non_auditable")
    if inconnus:
        print(f"{inconnus} site(s) non auditable(s) : on ne sait pas ce qu'ils valent, "
              f"aucun mail ne leur sera écrit (`liste --verdict non_auditable` pour les voir).")
    if not any(d.get("verdict") for d in donnees):
        print("Aucun audit pour l'instant : lancez `auditer` pour départager les sites "
              "obsolètes des sites corrects.")


def cmd_stop(args, cfg):
    con = _ouvrir(args)
    touches = 0
    for v in args.valeur:
        val = v.lower().strip()
        con.execute("INSERT OR REPLACE INTO exclusions (valeur,motif) VALUES (?,?)",
                    (val, args.motif))
        # L'exclusion vaut pour l'adresse exacte, pour tout le domaine et pour ses
        # sous-domaines — sinon un STOP posé sur « chezmario.fr » laisserait partir
        # les brouillons destinés à contact@chezmario.fr.
        touches += con.execute(
            """UPDATE emails SET statut='desinscrit'
               WHERE statut <> 'desinscrit'
                 AND (lower(destinataire) = ?
                      OR lower(destinataire) LIKE '%@' || ?
                      OR lower(destinataire) LIKE '%.' || ?)""",
            (val, val, val),
        ).rowcount
    con.commit()
    print(f"{len(args.valeur)} exclusion(s) enregistrée(s), {touches} mail(s) désinscrit(s). "
          f"Ces adresses ne seront plus jamais sollicitées.")


def cmd_marquer(args, cfg):
    """Sans elle, `relancer` n'a jamais rien à faire : il attend le statut `envoye`.

    À appeler juste après le dépôt des brouillons dans Gmail — c'est aussi ce qui
    donne son sens au quota journalier, compté sur `envoye_le`.
    """
    con = _ouvrir(args)
    if args.tous:
        if args.statut != "envoye":
            raise SystemExit("--tous ne vaut que pour « envoye ».")
        n = con.execute("""UPDATE emails SET statut='envoye', envoye_le=datetime('now')
                           WHERE statut='brouillon'""").rowcount
    elif not args.place_id:
        raise SystemExit("Précisez un ou plusieurs place_id, ou --tous.")
    else:
        n = 0
        for pid in args.place_id:
            if args.statut == "envoye":
                n += con.execute(
                    """UPDATE emails SET statut='envoye', envoye_le=datetime('now')
                       WHERE place_id=? AND statut='brouillon'""", (pid,)).rowcount
            else:
                n += con.execute(
                    """UPDATE emails SET statut=? WHERE place_id=?
                       AND statut IN ('brouillon','envoye')""", (args.statut, pid)).rowcount
    con.commit()
    print(f"{n} mail(s) marqué(s) « {args.statut} ».")
    if args.statut == "repondu":
        print("Ces prospects ne seront plus relancés.")


def cmd_pipeline(args, cfg):
    # `maquette` ne fait plus partie de l'enchaînement : le mail annonce un site
    # fabriqué APRÈS la prise de rendez-vous, il n'a donc pas de page à montrer.
    # La commande reste disponible seule (`python3 -m tools.prospect maquette`).
    for etape, fn in (("chercher", cmd_chercher), ("auditer", cmd_auditer),
                      ("enrichir", cmd_enrichir),
                      ("rediger", cmd_rediger)):
        print(f"\n{'=' * 78}\n== {etape}\n{'=' * 78}")
        fn(args, cfg)


# --------------------------------------------------------------------------- main
def principal(argv=None):
    p = argparse.ArgumentParser(
        prog="python3 -m prospect",
        description="Prospection locale : Google Maps → audit → maquette → email.",
    )
    p.add_argument("--config", help="chemin du prospect.config.json")
    p.add_argument("--db", help="chemin de la base SQLite")
    sp = p.add_subparsers(dest="commande", required=True)

    c = sp.add_parser("chercher", help="interroger Google Places et remplir la base")
    c.add_argument("--zone", help="filtrer sur une ville de la configuration")
    c.add_argument("--categorie", action="append", help="type Places (répétable)")
    c.add_argument("--priorite", choices=["prioritaires", "secondaires", "toutes"],
                   default="prioritaires")
    c.add_argument("--pavage", type=int, metavar="METRES",
                   help="balayage en damier pour les quartiers denses (ex. 800)")
    c.add_argument("--limite", type=int,
                   help="nombre maximum de NOUVELLES fiches enregistrées ; ce n'est pas "
                        "un plafond d'appels API (une tuile déjà lancée va au bout)")
    c.add_argument("--demo", action="store_true", help="jeu d'essai hors ligne, sans clé API")
    c.set_defaults(fn=cmd_chercher)

    a = sp.add_parser("auditer", help="auditer les sites et les fiches Google")
    a.add_argument("--limite", type=int)
    a.add_argument("--refaire", action="store_true", help="ré-auditer même si déjà fait")
    a.add_argument("--fraicheur", type=int, default=30, metavar="JOURS",
                   help="ré-auditer au delà de cet âge (0 = jamais ; un site dont "
                        "l'adresse a changé sur la fiche est de toute façon repris)")
    a.add_argument("--pause", type=float, default=1.0, help="secondes entre deux sites")
    a.add_argument("--demo", action="store_true",
                   help="auditer les faux sites du jeu d'essai (aucun accès réseau)")
    a.set_defaults(fn=cmd_auditer)

    e = sp.add_parser("enrichir", help="chercher les adresses de contact publiques")
    e.add_argument("--limite", type=int)
    e.add_argument("--pause", type=float, default=1.0)
    e.set_defaults(fn=cmd_enrichir)

    m = sp.add_parser("maquette", help="générer les pages « on a refait votre site »")
    m.add_argument("--limite", type=int)
    m.add_argument("--photos", action="store_true",
                   help="télécharger les photos de la fiche (consomme le quota Places)")
    m.set_defaults(fn=cmd_maquette)

    r = sp.add_parser("rediger", help="rédiger les brouillons d'emails")
    r.add_argument("--limite", type=int)
    r.add_argument("--creneaux", help="JSON des créneaux libres (produit depuis Google Agenda)")
    r.add_argument("--afficher", action="store_true", help="afficher les mails dans le terminal")
    r.add_argument("--reecrire", action="store_true",
                   help="reprendre les brouillons existants (jamais un mail déjà parti)")
    r.set_defaults(fn=cmd_rediger)

    rl = sp.add_parser("relancer", help="préparer les relances J+4 / J+9")
    rl.add_argument("--rang", type=int, choices=[1, 2], default=1)
    rl.set_defaults(fn=cmd_relancer)

    x = sp.add_parser("exporter", help="exporter la base")
    x.add_argument("--format", choices=["csv", "json", "eml", "mail", "appels"],
                   default="csv",
                   help="« mail » dépose les brouillons dans Mail.app (macOS) ; "
                        "il n'envoie rien")
    x.add_argument("--verdict", choices=["absent", "obsolete", "correct",
                                        "injoignable", "non_auditable"])
    x.add_argument("--limite", type=int)
    x.set_defaults(fn=cmd_exporter)

    l = sp.add_parser("liste", help="afficher la base, triée par priorité")
    l.add_argument("--verdict", choices=["absent", "obsolete", "correct",
                                        "injoignable", "non_auditable"])
    l.add_argument("--sans-site", dest="sans_site", action="store_true",
                   help="uniquement les fiches sans site web (dispo dès `chercher`)")
    l.add_argument("--avec-site", dest="avec_site", action="store_true",
                   help="uniquement les fiches qui affichent un site")
    l.add_argument("--limite", type=int)
    l.set_defaults(fn=cmd_liste)

    mq = sp.add_parser("marquer", help="noter qu'un mail est parti, ou qu'on a répondu")
    mq.add_argument("statut", choices=["envoye", "repondu"])
    mq.add_argument("place_id", nargs="*")
    mq.add_argument("--tous", action="store_true",
                    help="tous les brouillons (uniquement avec « envoye »)")
    mq.set_defaults(fn=cmd_marquer)

    s = sp.add_parser("stop", help="ne plus jamais contacter une adresse ou un domaine")
    s.add_argument("valeur", nargs="+")
    s.add_argument("--motif", default="demande de désinscription")
    s.set_defaults(fn=cmd_stop)

    pl = sp.add_parser("pipeline", help="chercher + auditer + enrichir + rediger")
    pl.add_argument("--zone")
    pl.add_argument("--categorie", action="append")
    pl.add_argument("--priorite", choices=["prioritaires", "secondaires", "toutes"],
                    default="prioritaires")
    pl.add_argument("--pavage", type=int)
    pl.add_argument("--limite", type=int)
    pl.add_argument("--demo", action="store_true")
    pl.add_argument("--photos", action="store_true")
    pl.add_argument("--creneaux")
    pl.add_argument("--afficher", action="store_true")
    pl.add_argument("--refaire", action="store_true")
    pl.add_argument("--reecrire", action="store_true")
    pl.add_argument("--fraicheur", type=int, default=30)
    pl.add_argument("--pause", type=float, default=1.0)
    pl.set_defaults(fn=cmd_pipeline)

    args = p.parse_args(argv)
    cfg = config_mod.charger(args.config)
    try:
        return args.fn(args, cfg)
    except copy_mod.ConfigExpeditionIncomplete as exc:
        # Filet : les gabarits refusent aussi de leur côté (copy.verifier_expedition).
        # On sort avec le message, pas avec une trace.
        raise SystemExit(str(exc)) from exc
