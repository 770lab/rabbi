"""Audit technique et SEO d'un site vitrine, et complétude de la fiche Google.

Objectif : ne jamais écrire « votre site est obsolète » en l'air. Chaque phrase du
mail doit s'appuyer sur un défaut mesuré ici, avec sa preuve.
"""

from __future__ import annotations

import datetime as _dt
import gzip
import io
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

UA = "Mozilla/5.0 (compatible; AuditSiteBot/1.0; audit de site public avant prise de contact)"
TIMEOUT = 15
TAILLE_MAX = 3_000_000

RESEAUX = ("facebook.com", "instagram.com", "linktr.ee", "tripadvisor.", "thefork.",
           "ubereats.com", "deliveroo.", "just-eat.", "pagesjaunes.fr", "linkedin.com")

CONSTRUCTEURS_DATES = {
    "wix.com": "Wix (ancienne génération)",
    "jimdo": "Jimdo",
    "e-monsite": "e-monsite",
    "1and1": "1&1 / IONOS SiteBuilder",
    "solocal": "Solocal / PagesJaunes",
    "webnode": "Webnode",
    "sitew": "SiteW",
    "wordpress.com": "WordPress.com gratuit",
}

TITRES_GENERIQUES = {"accueil", "home", "bienvenue", "index", "untitled document",
                     "nouveau site", "site en construction", "mon site"}


class _Extracteur(HTMLParser):
    """Relève ce dont l'audit a besoin, sans dépendance externe."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.titre = ""
        self._dans_titre = False
        self.meta_description = ""
        self.viewport = ""
        self.h1 = []
        self._dans_h1 = False
        self.og = 0
        self.jsonld = []
        self._dans_jsonld = False
        self.css_externes = []
        self.favicon = False
        self.images = 0
        self.images_lazy = 0
        self.images_srcset = 0
        self.liens_tel = 0
        self.liens_mailto = []
        self.balises_obsoletes = set()
        self.tableaux = 0
        self.scripts = []
        self.iframes = []
        self.texte = []
        self.formulaires = 0
        self.a_hreflang = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._dans_titre = True
        elif tag == "h1":
            self._dans_h1 = True
        elif tag == "meta":
            nom = a.get("name", "").lower()
            prop = a.get("property", "").lower()
            if nom == "description":
                self.meta_description = a.get("content", "")
            elif nom == "viewport":
                self.viewport = a.get("content", "")
            if prop.startswith("og:"):
                self.og += 1
        elif tag == "link":
            rel = a.get("rel", "").lower()
            if "stylesheet" in rel:
                self.css_externes.append(a.get("href", ""))
            if "icon" in rel:
                self.favicon = True
            if "alternate" in rel and a.get("hreflang"):
                self.a_hreflang = True
        elif tag == "script":
            if a.get("type", "").lower() == "application/ld+json":
                self._dans_jsonld = True
            if a.get("src"):
                self.scripts.append(a["src"])
        elif tag == "img":
            self.images += 1
            if a.get("loading", "").lower() == "lazy":
                self.images_lazy += 1
            if a.get("srcset"):
                self.images_srcset += 1
        elif tag == "a":
            href = a.get("href", "")
            if href.startswith("tel:"):
                self.liens_tel += 1
            elif href.startswith("mailto:"):
                self.liens_mailto.append(href[7:].split("?")[0])
        elif tag == "table":
            self.tableaux += 1
        elif tag == "iframe":
            self.iframes.append(a.get("src", ""))
        elif tag == "form":
            self.formulaires += 1
        elif tag in ("font", "center", "marquee", "blink", "frameset", "frame", "applet"):
            self.balises_obsoletes.add(tag)

    def handle_endtag(self, tag):
        if tag == "title":
            self._dans_titre = False
        elif tag == "h1":
            self._dans_h1 = False
        elif tag == "script":
            self._dans_jsonld = False

    def handle_data(self, data):
        if self._dans_titre:
            self.titre += data.strip()
        elif self._dans_h1:
            self.h1.append(data.strip())
        elif self._dans_jsonld:
            self.jsonld.append(data)
        else:
            t = data.strip()
            if t:
                self.texte.append(t)


def _telecharger(url: str, timeout: int = TIMEOUT) -> tuple[str, str, int, int, dict]:
    """Renvoie (contenu, url_finale, ttfb_ms, octets, entetes)."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Encoding": "gzip",
            "Accept-Language": "fr-FR,fr;q=0.9",
        },
    )
    ctx = ssl.create_default_context()
    debut = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        ttfb = int((time.monotonic() - debut) * 1000)
        brut = r.read(TAILLE_MAX)
        entetes = {k.lower(): v for k, v in r.headers.items()}
        url_finale = r.geturl()
    octets = len(brut)
    if entetes.get("content-encoding", "").lower() == "gzip":
        try:
            brut = gzip.GzipFile(fileobj=io.BytesIO(brut)).read()
        except OSError:
            pass
    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", entetes.get("content-type", ""), re.I)
    if m:
        charset = m.group(1)
    return brut.decode(charset, "replace"), url_finale, ttfb, octets, entetes


def _tete(url: str, chemin: str) -> bool:
    """Le fichier existe-t-il (robots.txt, sitemap.xml) ?"""
    base = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(url))
    try:
        req = urllib.request.Request(base + chemin, headers={"User-Agent": UA}, method="GET")
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status == 200 and len(r.read(2048)) > 0
    except Exception:
        return False


def _defaut(code, libelle, poids, preuve="", argument=""):
    return {"code": code, "libelle": libelle, "poids": poids,
            "preuve": preuve, "argument": argument}


def auditer_site(url: str, seuil_obsolete: int = 60) -> dict:
    """Audit d'une URL. Renvoie verdict, score /100, défauts détaillés."""
    defauts: list[dict] = []
    atouts: list[str] = []
    url = url.strip()
    if not url:
        return {"verdict": "absent", "score": None, "defauts": [], "atouts": []}

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    hote = urllib.parse.urlsplit(url).netloc.lower()
    if any(r in hote for r in RESEAUX):
        return {
            "verdict": "absent",
            "score": None,
            "url_finale": url,
            "atouts": [],
            "defauts": [
                _defaut(
                    "reseaux_seulement",
                    "Aucun site web : la seule vitrine est une page tierce",
                    0,
                    hote,
                    "Une page Facebook ou une fiche annuaire ne se positionne pas sur "
                    "Google et ne vous appartient pas : la plateforme peut la fermer "
                    "ou la monétiser du jour au lendemain.",
                )
            ],
        }

    try:
        html, url_finale, ttfb, octets, entetes = _telecharger(url)
    except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout,
            ssl.SSLError, ConnectionError, OSError) as e:
        raison = getattr(e, "code", None) or type(e).__name__
        return {
            "verdict": "injoignable",
            "score": 0,
            "url_finale": url,
            "atouts": [],
            "defauts": [
                _defaut("site_hs", "Le site ne répond pas", 100, f"{url} → {raison}",
                        "Le site renseigné sur votre fiche Google est inaccessible : "
                        "chaque visiteur qui clique tombe sur une erreur.")
            ],
        }

    p = _Extracteur()
    try:
        p.feed(html)
    except Exception:
        pass

    bas = html.lower()
    poids_ko = round(octets / 1024)
    annee = _dt.date.today().year

    # --- Sécurité / transport -------------------------------------------------
    if url_finale.startswith("http://"):
        defauts.append(_defaut(
            "pas_https", "Pas de HTTPS", 20, url_finale,
            "Depuis 2018, Chrome affiche « Non sécurisé » dans la barre d'adresse de "
            "vos visiteurs, et Google privilégie les sites sécurisés dans son "
            "classement."))
    else:
        atouts.append("HTTPS actif")

    # --- Mobile ---------------------------------------------------------------
    if not p.viewport:
        defauts.append(_defaut(
            "pas_responsive", "Le site n'est pas adapté au téléphone", 22, "",
            "L'essentiel des recherches locales se fait sur mobile. Sur un téléphone, "
            "votre site s'affiche en version « bureau » miniature : il faut zoomer pour "
            "lire, et la plupart des visiteurs referment avant d'avoir trouvé vos "
            "horaires."))
    elif "@media" not in bas:
        defauts.append(_defaut(
            "responsive_partiel", "Mise en page peu adaptative", 8,
            "aucune media query dans le HTML",
            "L'affichage mobile n'est que partiellement géré."))
    else:
        atouts.append("Affichage mobile pris en charge")

    # --- Référencement --------------------------------------------------------
    titre = p.titre.strip()
    if not titre:
        defauts.append(_defaut(
            "titre_absent", "Balise <title> absente", 14, "",
            "Google n'a aucun titre à afficher dans ses résultats : votre ligne bleue "
            "est générée au hasard à partir du contenu."))
    elif titre.lower() in TITRES_GENERIQUES or len(titre) < 15:
        defauts.append(_defaut(
            "titre_generique", "Titre générique ou trop court", 12, f"« {titre} »",
            f"Votre titre Google est « {titre} » : ni le métier, ni la ville. "
            "C'est la première raison pour laquelle on ne vous trouve pas en tapant "
            "votre activité + votre ville."))
    else:
        atouts.append("Balise title renseignée")

    if not p.meta_description:
        defauts.append(_defaut(
            "meta_description_absente", "Meta description absente", 9, "",
            "Le texte affiché sous votre lien dans Google est bricolé automatiquement : "
            "aucune promesse, aucune raison de cliquer plutôt que sur le concurrent."))
    if not p.h1:
        defauts.append(_defaut(
            "h1_absent", "Aucun titre H1", 7, "",
            "La page n'annonce pas son sujet : Google doit deviner de quoi vous parlez."))

    types_ld = []
    for bloc in p.jsonld:
        try:
            d = json.loads(bloc)
            types_ld += [d.get("@type")] if isinstance(d, dict) else \
                        [x.get("@type") for x in d if isinstance(x, dict)]
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
    types_ld = [t for t in types_ld if t]
    if not types_ld:
        defauts.append(_defaut(
            "donnees_structurees_absentes", "Aucune donnée structurée (schema.org)", 9, "",
            "Horaires, avis, adresse et menu ne remontent pas en « résultat enrichi ». "
            "C'est aussi ce que lisent désormais les assistants IA quand on leur demande "
            "où manger dans le quartier."))
    else:
        atouts.append("Données structurées présentes (" + ", ".join(map(str, types_ld[:3])) + ")")

    if p.og < 3:
        defauts.append(_defaut(
            "og_absent", "Partage social non configuré (Open Graph)", 4, f"{p.og} balises og:",
            "Quand un client colle votre lien sur WhatsApp ou Instagram, il n'apparaît "
            "ni image ni description : le lien fait amateur, personne ne clique."))

    if not _tete(url_finale, "/sitemap.xml"):
        defauts.append(_defaut("sitemap_absent", "Pas de sitemap.xml", 5, "",
                               "Google explore votre site à l'aveugle."))
    if not _tete(url_finale, "/robots.txt"):
        defauts.append(_defaut("robots_absent", "Pas de robots.txt", 3, "", ""))
    if not p.favicon:
        defauts.append(_defaut("favicon_absente", "Pas d'icône d'onglet (favicon)", 3, "",
                               "Votre onglet est une page blanche anonyme."))

    # --- Performance ----------------------------------------------------------
    if poids_ko > 2500:
        defauts.append(_defaut(
            "page_tres_lourde", "Page d'accueil très lourde", 12, f"{poids_ko} Ko de HTML",
            f"{poids_ko} Ko rien que pour le texte de la page : en 4G dans la rue, elle "
            "met plusieurs secondes à apparaître, et la plupart des visiteurs n'attendent "
            "pas."))
    elif poids_ko > 1200:
        defauts.append(_defaut("page_lourde", "Page d'accueil lourde", 6, f"{poids_ko} Ko", ""))

    if ttfb > 1500:
        defauts.append(_defaut(
            "serveur_lent", "Serveur lent", 10, f"{ttfb} ms avant le premier octet",
            f"Votre serveur met {ttfb} ms à répondre. Google en fait un critère de "
            "classement depuis les Core Web Vitals."))
    elif ttfb < 400:
        atouts.append(f"Serveur réactif ({ttfb} ms)")

    if p.images and p.images_lazy == 0 and p.images > 5:
        defauts.append(_defaut(
            "images_non_optimisees", "Images non optimisées", 5,
            f"{p.images} images, aucune en chargement différé",
            "Toutes les photos se chargent d'un coup, y compris celles tout en bas de "
            "page que le visiteur ne verra jamais. D'où l'attente."))

    # --- Technologies datées --------------------------------------------------
    obsoletes = []
    if p.balises_obsoletes:
        obsoletes.append("mise en forme héritée des années 2000")
    if ".swf" in bas or "shockwave" in bas:
        obsoletes.append("Flash, technologie morte depuis 2020")
    mj = re.search(r"jquery[.-](\d+)\.(\d+)[\d.]*(?:\.min)?\.js", bas)
    if mj and int(mj.group(1)) < 3:
        obsoletes.append(f"jQuery {mj.group(1)}.{mj.group(2)}, une version de 2012")
    if p.tableaux >= 3 and not p.viewport:
        obsoletes.append("mise en page construite en tableaux")
    if obsoletes:
        defauts.append(_defaut(
            "techno_obsolete", "Le site est bâti sur des techniques abandonnées", 14,
            " ; ".join(obsoletes),
            "Concrètement : l'affichage peut casser selon le navigateur du visiteur, "
            "et les briques les plus anciennes ne reçoivent plus de correctif de "
            "sécurité."))

    for motif, nom in CONSTRUCTEURS_DATES.items():
        if motif in bas:
            defauts.append(_defaut(
                "constructeur_date", f"Site bâti sur {nom}", 6, motif,
                f"Le site tourne sur {nom} : gabarit partagé avec des milliers d'autres, "
                "peu personnalisable et peu performant côté référencement."))
            break

    m = re.search(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?(\d{4})", bas)
    if m:
        an = int(m.group(1))
        if an <= annee - 2:
            defauts.append(_defaut(
                "copyright_perime", f"Pied de page daté {an}", 7, f"© {an}",
                f"Le pied de page affiche encore « © {an} ». Un visiteur en déduit "
                "en une seconde que l'établissement a peut-être fermé — et vos "
                "horaires ou vos prix ne sont sans doute plus à jour non plus."))

    # --- Conversion / informations locales ------------------------------------
    corps = " ".join(p.texte).lower()
    if p.liens_tel == 0:
        defauts.append(_defaut(
            "pas_de_clic_pour_appeler", "Numéro non cliquable sur mobile", 8,
            "aucun lien tel:",
            "Sur téléphone, le client doit recopier votre numéro à la main pour vous "
            "appeler. À ce moment-là, la plupart renoncent ou appellent le concurrent."))
    if not re.search(r"\b(?:0\d[\s.\-]?(?:\d{2}[\s.\-]?){4}|\+33)", corps + html[:20000]):
        defauts.append(_defaut("telephone_absent", "Téléphone introuvable sur la page", 8,
                               "", "Le premier réflexe d'un client local est d'appeler."))
    if not re.search(r"\b(?:lundi|mardi|horaires?|ouvert)\b", corps):
        defauts.append(_defaut(
            "horaires_absents", "Horaires non affichés", 7, "",
            "« Est-ce ouvert maintenant ? » est LA question du client local. "
            "Sans réponse sur le site, il retourne sur Google — et voit vos concurrents."))
    if not re.search(r"\b(r[ée]server|commander|prendre rendez-vous|devis|contact)\b", corps):
        defauts.append(_defaut(
            "pas_d_appel_a_action", "Aucun appel à l'action visible", 9, "",
            "La page informe mais ne fait rien faire : ni réservation, ni commande, "
            "ni demande de devis. Le visiteur repart sans laisser de trace."))

    poids_total = sum(d["poids"] for d in defauts)
    # Plancher à 10 : un score de 0/100 sonne comme une insulte dans un mail,
    # alors qu'on veut ouvrir une conversation.
    score = max(10, 100 - poids_total)
    defauts.sort(key=lambda d: -d["poids"])
    return {
        "verdict": "obsolete" if score < seuil_obsolete else "correct",
        "score": score,
        "score_brut_penalites": poids_total,
        "url_finale": url_finale,
        "poids_ko": poids_ko,
        "ttfb_ms": ttfb,
        "defauts": defauts,
        "atouts": atouts,
        "emails_trouves": p.liens_mailto,
        "titre": titre,
    }


def auditer_fiche(etab: dict) -> tuple[int, list[dict]]:
    """Complétude de la fiche Google Business Profile, à partir des données Places.

    Utile même quand l'établissement a un bon site : c'est un second angle d'attaque,
    et c'est ce qui pèse le plus sur la visibilité locale.
    """
    manques = []
    score = 100
    if not (etab.get("site") or "").strip():
        score -= 25
        manques.append(_defaut("fiche_sans_site", "Fiche Google sans site web", 25, "",
                               "Le bouton « Site Web » de votre fiche est vide."))
    if not (etab.get("telephone") or "").strip():
        score -= 15
        manques.append(_defaut("fiche_sans_tel", "Fiche sans numéro de téléphone", 15, "", ""))
    horaires = json.loads(etab.get("horaires") or "[]")
    if not horaires:
        score -= 18
        manques.append(_defaut(
            "fiche_sans_horaires", "Fiche sans horaires d'ouverture", 18, "",
            "Google ne peut pas afficher « Ouvert » sur votre fiche : vous disparaissez "
            "des recherches « ouvert maintenant », qui sont les plus rentables."))
    photos = json.loads(etab.get("photos") or "[]")
    if len(photos) < 3:
        score -= 15
        manques.append(_defaut(
            "fiche_peu_de_photos", f"Seulement {len(photos)} photo(s) sur la fiche", 15,
            f"{len(photos)} photos",
            "Les fiches avec plus de dix photos reçoivent nettement plus d'appels "
            "et d'itinéraires que les fiches quasi vides."))
    avis = etab.get("nb_avis") or 0
    if avis < 10:
        score -= 12
        manques.append(_defaut("fiche_peu_d_avis", f"{avis} avis seulement", 12, f"{avis} avis",
                               "Peu d'avis : Google vous fait remonter moins haut, et le "
                               "client hésite avant de pousser la porte."))
    note = etab.get("note")
    if note is not None and note < 4.0:
        score -= 10
        manques.append(_defaut("fiche_note_basse", f"Note de {note}/5", 10, f"{note}/5",
                               "Sous 4,0, une part importante des clients écarte "
                               "l'établissement sans même lire les avis."))
    if not (etab.get("resume") or "").strip():
        score -= 5
    return max(0, score), manques
