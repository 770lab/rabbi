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
import sys
import time
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
    con = store_mod.ouvrir(args.db)
    total = 0

    if args.demo:
        for brut in places_mod.charger_demo():
            e = places_mod.normaliser(brut, "Démo")
            store_mod.upsert_etablissement(con, e)
            total += 1
        con.commit()
        print(f"[démo] {total} établissements chargés depuis le jeu d'essai.")
        return

    cle = config_mod.cle_api()
    for zone in _zones(cfg, args.zone):
        for cat in _categories(cfg, args):
            vus = set()
            requete = f"{cat.replace('_', ' ')} à {zone.get('ville','')}".strip()
            print(f"→ {requete} ({zone['lat']:.4f},{zone['lng']:.4f} r={zone.get('rayon_m')}m)")
            try:
                lots = places_mod.recherche_texte(requete, zone, cle)
                if args.pavage:
                    for tuile in places_mod.pavage(zone, args.pavage):
                        lots += places_mod.recherche_proximite([cat], tuile, cle)
                        time.sleep(0.15)
            except SystemExit as e:
                print(f"  ! {e}", file=sys.stderr)
                continue
            for brut in lots:
                pid = brut.get("id")
                if not pid or pid in vus:
                    continue
                vus.add(pid)
                e = places_mod.normaliser(brut, zone.get("ville", ""))
                if (e.get("nb_avis") or 0) < cfg["seuils"]["avis_min"]:
                    continue
                store_mod.upsert_etablissement(con, e)
                total += 1
            con.commit()
            print(f"  {len(vus)} fiches vues")
            if args.limite and total >= args.limite:
                break
    print(f"{total} établissements enregistrés dans la base.")


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

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Silencieux)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# --------------------------------------------------------------------------- auditer
def cmd_auditer(args, cfg):
    con = store_mod.ouvrir(args.db)
    srv = _serveur_demo() if getattr(args, "demo", False) else None
    sql = """SELECT e.* FROM etablissements e
             LEFT JOIN audits a ON a.place_id = e.place_id
             WHERE (e.statut IS NULL OR e.statut = '' OR e.statut = 'OPERATIONAL')"""
    if not args.refaire:
        sql += " AND a.place_id IS NULL"
    sql += " ORDER BY e.nb_avis DESC"
    if args.limite:
        sql += f" LIMIT {int(args.limite)}"

    lignes = con.execute(sql).fetchall()
    print(f"{len(lignes)} établissement(s) à auditer.")
    compteur = {"absent": 0, "obsolete": 0, "correct": 0, "injoignable": 0}

    for i, row in enumerate(lignes, 1):
        e = dict(row)
        score_fiche, manques = audit_mod.auditer_fiche(e)
        site = (e.get("site") or "").strip()
        if site:
            res = audit_mod.auditer_site(site, cfg["seuils"]["site_obsolete"])
        else:
            res = {"verdict": "absent", "score": None, "atouts": [],
                   "defauts": [audit_mod._defaut(
                       "aucun_site", "Aucun site web sur la fiche Google", 0, "",
                       "Le bouton « Site Web » de votre fiche Google est vide : "
                       "les clients qui vous découvrent n'ont nulle part où aller.")]}
        if res["verdict"] == "absent":
            res["defauts"] = res["defauts"] + [m for m in manques
                                               if m["code"] != "fiche_sans_site"]
        res["score_fiche"] = score_fiche
        store_mod.enregistrer_audit(con, e["place_id"], res)
        compteur[res["verdict"]] = compteur.get(res["verdict"], 0) + 1
        marque = {"absent": "○", "obsolete": "▲", "correct": "●", "injoignable": "✕"}
        print(f"  [{i}/{len(lignes)}] {marque.get(res['verdict'],'?')} {e['nom'][:38]:<38} "
              f"{res['verdict']:<11} site={res.get('score') if res.get('score') is not None else '—'}"
              f" fiche={score_fiche}")
        con.commit()
        if site:
            time.sleep(args.pause)

    if srv:
        srv.shutdown()
    print("\nRécapitulatif : " + " · ".join(f"{k} {v}" for k, v in compteur.items() if v))


# --------------------------------------------------------------------------- enrichir
def cmd_enrichir(args, cfg):
    con = store_mod.ouvrir(args.db)
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
            if store_mod.est_exclu(con, info["email"]):
                continue
            con.execute(
                "INSERT OR IGNORE INTO contacts (place_id,email,source,generique) VALUES (?,?,?,?)",
                (e["place_id"], info["email"], info["source"], info["generique"]),
            )
        con.commit()
        print(f"  {e['nom'][:40]:<40} {', '.join(i['email'] for i in emails[:2]) or '—'}")
        time.sleep(args.pause)
    print("\nRappel : les établissements sans site n'ont pas d'email récupérable ici. "
          "Pour eux, le téléphone de la fiche Google reste le meilleur canal "
          "(voir `exporter --format csv`).")


# --------------------------------------------------------------------------- maquette
def cmd_maquette(args, cfg):
    con = store_mod.ouvrir(args.db)
    cle = None
    if args.photos:
        cle = config_mod.cle_api()
    lignes = con.execute(
        """SELECT e.*, a.verdict, a.score, a.score_fiche FROM etablissements e
           JOIN audits a ON a.place_id = e.place_id
           WHERE a.verdict IN ('absent','obsolete','injoignable')
           ORDER BY e.nb_avis DESC LIMIT ?""",
        (args.limite or 20,),
    ).fetchall()
    for row in lignes:
        e = dict(row)
        chemin = mockup_mod.generer(e, cfg, cle_api=cle)
        print(f"  {e['nom'][:40]:<40} → {chemin.relative_to(BASE.parents[1])}")
    print(f"\n{len(lignes)} maquette(s) générée(s) dans tools/prospect/out/maquettes/.")
    print("Aperçu local :  python3 -m http.server -d tools/prospect/out/maquettes 8080")


# --------------------------------------------------------------------------- rediger
def cmd_rediger(args, cfg):
    con = store_mod.ouvrir(args.db)
    creneaux = None
    if args.creneaux:
        creneaux = json.loads(Path(args.creneaux).read_text(encoding="utf-8"))
        if isinstance(creneaux, dict):
            creneaux = creneaux.get("creneaux", [])

    base_maquette = (cfg.get("maquettes", {}).get("base_url") or "").rstrip("/")
    lignes = con.execute(
        """SELECT e.*, a.verdict, a.score, a.score_fiche, a.defauts
           FROM etablissements e JOIN audits a ON a.place_id = e.place_id
           WHERE a.verdict IN ('absent','obsolete','injoignable')
           ORDER BY e.nb_avis DESC""",
    ).fetchall()

    prospects = []
    for row in lignes:
        e = dict(row)
        e["_score_opportunite"] = score_opportunite(e, e)
        prospects.append(e)
    prospects.sort(key=lambda x: -x["_score_opportunite"])
    if args.limite:
        prospects = prospects[: args.limite]

    ecrits = 0
    for e in prospects:
        slug = mockup_mod.creneau(e["nom"])
        url_maquette = f"{base_maquette}/{slug}/" if base_maquette else ""
        if not url_maquette and (mockup_mod.SORTIE / slug / "index.html").exists():
            url_maquette = f"(maquette locale : out/maquettes/{slug}/index.html)"
        mail = copy_mod.rediger(e, e, cfg, url_maquette, creneaux)
        if not mail:
            continue
        contact = con.execute(
            "SELECT email FROM contacts WHERE place_id=? ORDER BY generique DESC LIMIT 1",
            (e["place_id"],),
        ).fetchone()
        destinataire = contact["email"] if contact else ""
        if destinataire and store_mod.est_exclu(con, destinataire):
            continue
        con.execute(
            """INSERT INTO emails (place_id,type,objet,corps,destinataire,maquette)
               VALUES (?,?,?,?,?,?)""",
            (e["place_id"], mail["type"], mail["objet"], mail["corps"],
             destinataire, url_maquette),
        )
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
    quota = cfg["quotas"]["max_emails_par_jour"]
    if ecrits > quota:
        print(f"⚠ Quota conseillé : {quota} envois/jour. Étalez sur "
              f"{-(-ecrits // quota)} jour(s) pour préserver la réputation de votre domaine.")


# --------------------------------------------------------------------------- relancer
def cmd_relancer(args, cfg):
    con = store_mod.ouvrir(args.db)
    delai = cfg["quotas"][f"delai_relance_{args.rang}_jours"]
    lignes = con.execute(
        f"""SELECT m.*, e.nom, e.ville, e.note, e.nb_avis, e.telephone, e.place_id AS pid
            FROM emails m JOIN etablissements e ON e.place_id = m.place_id
            WHERE m.statut = 'envoye'
              AND m.type IN ('A_sans_site','B_site_obsolete')
              AND julianday('now') - julianday(m.envoye_le) >= {int(delai)}
              AND NOT EXISTS (SELECT 1 FROM emails r WHERE r.place_id = m.place_id
                              AND r.type = 'relance_{args.rang}')""",
    ).fetchall()
    for row in lignes:
        e = {"place_id": row["pid"], "nom": row["nom"], "ville": row["ville"],
             "note": row["note"], "nb_avis": row["nb_avis"]}
        r = copy_mod.relance(e, {"objet": row["objet"]}, cfg, args.rang, row["maquette"] or "")
        con.execute(
            """INSERT INTO emails (place_id,type,objet,corps,destinataire,maquette)
               VALUES (?,?,?,?,?,?)""",
            (row["pid"], r["type"], r["objet"], r["corps"], row["destinataire"], row["maquette"]),
        )
        print(f"  relance {args.rang} : {row['nom']}")
    con.commit()
    print(f"{len(lignes)} relance(s) préparée(s).")


# --------------------------------------------------------------------------- exporter
def cmd_exporter(args, cfg):
    con = store_mod.ouvrir(args.db)
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
    if args.verdict:
        donnees = [d for d in donnees if d.get("verdict") == args.verdict]
    if args.limite:
        donnees = donnees[: args.limite]

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
            for d in donnees if d.get("objet")
        ]
        cible.write_text(json.dumps(utiles, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"→ {cible.relative_to(BASE.parents[1])} ({len(utiles)} brouillons prêts pour Gmail)")

    elif args.format == "appels":
        # La majorité des fiches sans site n'ont aucun email récupérable :
        # pour celles-là, le téléphone reste le seul canal. Voici la feuille d'appel.
        cible = EXPORTS / "feuille-d-appels.md"
        lignes_md = ["# Feuille d'appels", "",
                     "Ordre de priorité. Une ligne = un appel. Notez l'issue à droite.", ""]
        for d in donnees:
            if not (d.get("telephone") or "").strip():
                continue
            defauts = json.loads(d.get("defauts") or "[]")[:2]
            slug = mockup_mod.creneau(d["nom"] or "")
            lignes_md += [
                f"## {d['priorite']} · {d['nom']} — {d['telephone']}",
                f"*{d.get('categorie') or ''} · {d.get('ville') or ''} · "
                f"{d.get('note') or '—'}/5 sur {d.get('nb_avis') or 0} avis · "
                f"verdict : {d.get('verdict') or '—'}*",
                "",
                "**Accroche** : « Bonjour, je vous appelle parce que j'ai regardé votre "
                f"fiche Google — {d.get('note') or ''}/5, très bien notée — et j'ai vu que "
                + ("vous n'aviez pas de site. J'en ai fait un, il est déjà en ligne. »"
                   if d.get("verdict") == "absent"
                   else "votre site avait pris un coup de vieux. J'ai refait la page "
                        "d'accueil pour vous montrer. »"),
                "",
            ]
            for df in defauts:
                lignes_md.append(f"- {df['libelle']}"
                                 + (f" ({df['preuve']})" if df.get("preuve") else ""))
            lignes_md += ["", f"- Maquette : `out/maquettes/{slug}/index.html`",
                          f"- Fiche : {d.get('maps_url') or '—'}",
                          "- Issue : [ ] rappeler  [ ] rendez-vous  [ ] non  [ ] STOP", "", "---", ""]
        cible.write_text("\n".join(lignes_md), encoding="utf-8")
        print(f"→ {cible.relative_to(BASE.parents[1])} "
              f"({sum(1 for d in donnees if (d.get('telephone') or '').strip())} appels)")

    elif args.format == "eml":
        dossier = EXPORTS / "eml"
        dossier.mkdir(parents=True, exist_ok=True)
        n = 0
        for d in donnees:
            if not d.get("objet"):
                continue
            msg = EmailMessage()
            msg["Subject"] = d["objet"]
            msg["From"] = f"{cfg['identite'].get('nom','')} <{cfg['identite'].get('email','')}>"
            if d.get("destinataire"):
                msg["To"] = d["destinataire"]
            msg["X-Prospection-Place-Id"] = d["place_id"]
            msg.set_content(d["corps"])
            (dossier / f"{mockup_mod.creneau(d['nom'])}.eml").write_bytes(msg.as_bytes())
            n += 1
        print(f"→ {dossier.relative_to(BASE.parents[1])}/ ({n} fichiers .eml, glissables dans Gmail)")


# --------------------------------------------------------------------------- divers
def cmd_liste(args, cfg):
    con = store_mod.ouvrir(args.db)
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
          f"{'verdict':<11} {'note':>5} {'fiche':>5}")
    print("-" * 97)
    for d in donnees[: args.limite or 50]:
        a_un_site = "oui" if (d.get("site") or "").strip() else "AUCUN"
        print(f"{d['priorite']:>4}  {(d['nom'] or '')[:32]:<32} {(d['ville'] or '')[:13]:<13} "
              f"{d['nb_avis'] or 0:>5}  {a_un_site:<8} {(d['verdict'] or '—'):<11} "
              f"{(d['score'] if d['score'] is not None else '—'):>5} "
              f"{(d['score_fiche'] if d['score_fiche'] is not None else '—'):>5}")

    sans = sum(1 for d in donnees if not (d.get("site") or "").strip())
    print(f"\n{len(donnees)} établissement(s) · {sans} sans site · {len(donnees)-sans} avec site")
    if not any(d.get("verdict") for d in donnees):
        print("Aucun audit pour l'instant : lancez `auditer` pour départager les sites "
              "obsolètes des sites corrects.")


def cmd_stop(args, cfg):
    con = store_mod.ouvrir(args.db)
    for v in args.valeur:
        con.execute("INSERT OR REPLACE INTO exclusions (valeur,motif) VALUES (?,?)",
                    (v.lower().strip(), args.motif))
        con.execute("UPDATE emails SET statut='desinscrit' WHERE destinataire=?",
                    (v.lower().strip(),))
    con.commit()
    print(f"{len(args.valeur)} exclusion(s) enregistrée(s). Ces adresses ne seront plus "
          f"jamais sollicitées.")


def cmd_pipeline(args, cfg):
    for etape, fn in (("chercher", cmd_chercher), ("auditer", cmd_auditer),
                      ("enrichir", cmd_enrichir), ("maquette", cmd_maquette),
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
    c.add_argument("--limite", type=int)
    c.add_argument("--demo", action="store_true", help="jeu d'essai hors ligne, sans clé API")
    c.set_defaults(fn=cmd_chercher)

    a = sp.add_parser("auditer", help="auditer les sites et les fiches Google")
    a.add_argument("--limite", type=int)
    a.add_argument("--refaire", action="store_true", help="ré-auditer même si déjà fait")
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
    r.set_defaults(fn=cmd_rediger)

    rl = sp.add_parser("relancer", help="préparer les relances J+4 / J+9")
    rl.add_argument("--rang", type=int, choices=[1, 2], default=1)
    rl.set_defaults(fn=cmd_relancer)

    x = sp.add_parser("exporter", help="exporter la base")
    x.add_argument("--format", choices=["csv", "json", "eml", "appels"], default="csv")
    x.add_argument("--verdict", choices=["absent", "obsolete", "correct", "injoignable"])
    x.add_argument("--limite", type=int)
    x.set_defaults(fn=cmd_exporter)

    l = sp.add_parser("liste", help="afficher la base, triée par priorité")
    l.add_argument("--verdict", choices=["absent", "obsolete", "correct", "injoignable"])
    l.add_argument("--sans-site", dest="sans_site", action="store_true",
                   help="uniquement les fiches sans site web (dispo dès `chercher`)")
    l.add_argument("--avec-site", dest="avec_site", action="store_true",
                   help="uniquement les fiches qui affichent un site")
    l.add_argument("--limite", type=int)
    l.set_defaults(fn=cmd_liste)

    s = sp.add_parser("stop", help="ne plus jamais contacter une adresse ou un domaine")
    s.add_argument("valeur", nargs="+")
    s.add_argument("--motif", default="demande de désinscription")
    s.set_defaults(fn=cmd_stop)

    pl = sp.add_parser("pipeline", help="chercher + auditer + enrichir + maquette + rediger")
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
    pl.add_argument("--pause", type=float, default=1.0)
    pl.set_defaults(fn=cmd_pipeline)

    args = p.parse_args(argv)
    cfg = config_mod.charger(args.config)
    return args.fn(args, cfg)
