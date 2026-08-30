"""Recherche de l'adresse de contact publique d'un établissement.

On ne collecte que des adresses génériques publiées sur le site de l'entreprise
(contact@, info@, reservation@…). Les adresses nominatives sont marquées comme
telles : en prospection B2B, elles relèvent de la donnée personnelle et il vaut
mieux s'en abstenir.
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.robotparser

from .audit import UA, _telecharger

MOTIF_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

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


def _robots_autorise(base: str, chemin: str) -> bool:
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(base + "/robots.txt")
    try:
        rp.read()
    except Exception:
        return True  # pas de robots.txt lisible : comportement par défaut, on continue
    try:
        return rp.can_fetch(UA, base + chemin)
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
        if chemin and not _robots_autorise(base, chemin):
            continue
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
