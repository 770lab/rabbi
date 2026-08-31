"""Recherche de l'adresse de contact publique d'un établissement.

On ne collecte que des adresses génériques publiées sur le site de l'entreprise
(contact@, info@, reservation@…). Les adresses nominatives sont marquées comme
telles : en prospection B2B, elles relèvent de la donnée personnelle et il vaut
mieux s'en abstenir.
"""

from __future__ import annotations

import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

from .audit import UA, _telecharger

MOTIF_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# `can_fetch` ne retient de l'UA que ce qui précède le premier « / » : lui passer UA
# entier lui ferait croire que notre robot s'appelle « Mozilla », et une règle visant
# AuditSiteBot ne s'appliquerait jamais. On lui donne donc le jeton, pas la chaîne.
_JETON = re.search(r"[A-Za-z][A-Za-z0-9.\-]*[Bb]ot[A-Za-z0-9.\-]*", UA)
JETON_ROBOTS = (_JETON.group(0) if _JETON else "AuditSiteBot").split("/")[0]

PAUSE_S = 0.5  # entre deux pages d'un même domaine : on ne tire pas en rafale

PREFIXES_GENERIQUES = {
    "contact", "info", "infos", "bonjour", "hello", "reservation", "reservations",
    "commande", "commandes", "accueil", "direction", "restaurant", "boutique",
    "secretariat", "cabinet", "devis", "rdv", "service", "commercial", "admin",
    "administration", "boulangerie", "salon", "atelier", "agence",
}

PAGES_CANDIDATES = ["", "/contact", "/contact.html", "/contact.php", "/nous-contacter",
                    "/nous-contacter.html", "/mentions-legales", "/mentions-legales.html",
                    "/legal", "/a-propos", "/infos-pratiques"]

A_IGNORER = ("@sentry", "@example", "@wix", "@2x.png", ".png", ".jpg", ".jpeg",
             ".gif", ".webp", "@domain", "@email", "@votredomaine", "@sitename")


_ROBOTS: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def _robots(base: str) -> urllib.robotparser.RobotFileParser | None:
    """Télécharge le robots.txt d'un domaine UNE seule fois et le mémorise.

    Il était auparavant retéléchargé pour chaque chemin candidat, en rafale (relevé :
    10 requêtes en 0,02 s) — le motif de trafic qui fait blacklister une IP — et avec
    l'ouvreur par défaut d'urllib, donc l'UA « Python-urllib/3.x » que filtrent les WAF,
    alors que les pages, elles, passent avec l'UA du projet.

    Renvoie None quand il n'y a aucune règle exploitable : fichier absent, illisible,
    ou protégé (401/403). `RobotFileParser.read()` traduisait ces deux derniers cas en
    « tout interdit » sans rien lever — l'outil renonçait alors silencieusement à
    toutes les pages de contact et rendait un brouillon sans destinataire.
    """
    if base in _ROBOTS:
        return _ROBOTS[base]
    rp = None
    req = urllib.request.Request(base + "/robots.txt", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            texte = r.read(500_000).decode("utf-8", "replace")
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(texte.splitlines())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            print(f"  ! robots.txt inaccessible ({e.code}) sur {base} : aucune règle "
                  "exploitable, on s'en tient aux pages publiques habituelles",
                  file=sys.stderr)
    except Exception:
        pass  # domaine injoignable, TLS, timeout : le téléchargement des pages tranchera
    _ROBOTS[base] = rp
    return rp


def _robots_autorise(base: str, chemin: str) -> bool:
    rp = _robots(base)
    if rp is None:
        return True  # pas de robots.txt exploitable : comportement par défaut, on continue
    try:
        return rp.can_fetch(JETON_ROBOTS, base + chemin)
    except Exception:
        return True


def _classer(email: str, domaine_site: str) -> dict:
    prefixe = email.split("@")[0].lower()
    domaine = email.split("@")[-1].lower()
    generique = (
        prefixe in PREFIXES_GENERIQUES
        or any(prefixe.startswith(p) for p in PREFIXES_GENERIQUES)
        or "." not in prefixe and "-" not in prefixe and len(prefixe) <= 4
    )
    return {
        "email": email.lower(),
        "generique": 1 if generique else 0,
        "meme_domaine": 1 if domaine_site and domaine.endswith(domaine_site) else 0,
    }


def trouver_emails(url: str, max_pages: int = 4) -> list[dict]:
    """Parcourt quelques pages de contact et renvoie les adresses trouvées, triées."""
    if not url:
        return []
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parties = urllib.parse.urlsplit(url)
    base = f"{parties.scheme}://{parties.netloc}"
    domaine_site = parties.netloc.lower().removeprefix("www.")

    trouves: dict[str, dict] = {}
    visitees = 0
    for chemin in PAGES_CANDIDATES:
        if visitees >= max_pages:
            break
        # `chemin or "/"` : la racine est la seule page toujours visitée, c'était
        # aussi la seule qu'on ne soumettait pas au contrôle — l'exact inverse de
        # l'intention, puisque `chemin` vide court-circuitait le test.
        if not _robots_autorise(base, chemin or "/"):
            continue
        if visitees:
            time.sleep(PAUSE_S)
        try:
            html, _, _, _, _ = _telecharger(base + chemin, timeout=12)
        except Exception:
            continue
        visitees += 1
        # Les adresses écrites « nom (at) domaine.fr » pour tromper les robots.
        html = re.sub(r"\s*\(\s*(?:at|arobase)\s*\)\s*", "@", html, flags=re.I)
        for brut in MOTIF_EMAIL.findall(html):
            e = brut.lower()
            if any(x in e for x in A_IGNORER) or len(e) > 90:
                continue
            info = _classer(e, domaine_site)
            info["source"] = "mailto" if f"mailto:{e}" in html.lower() else "texte"
            trouves.setdefault(e, info)
        if any(v["generique"] and v["meme_domaine"] for v in trouves.values()):
            break  # on a l'adresse publique de la maison, inutile d'insister

    return sorted(
        trouves.values(),
        key=lambda v: (-v["generique"], -v["meme_domaine"], v["email"]),
    )
