"""Audit technique et SEO d'un site vitrine, et complétude de la fiche Google.

Objectif : ne jamais écrire « votre site est obsolète » en l'air. Chaque phrase du
mail doit s'appuyer sur un défaut mesuré ici, avec sa preuve.
"""

from __future__ import annotations

import codecs
import datetime as _dt
import gzip
import io
import json
import re
import socket
import ssl
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser

UA = "Mozilla/5.0 (compatible; AuditSiteBot/1.0; audit de site public avant prise de contact)"
# Un mutualisé qui rame à 19 h n'est pas un site mort : on laisse du temps, et on
# réessaie, avant d'oser écrire quoi que ce soit au commerçant.
TIMEOUT = 25
TENTATIVES = 3
ATTENTES = (2, 6)          # secondes entre deux essais
TAILLE_MAX = 3_000_000     # porte sur le HTML décompressé
HOTE_TEMOIN = "www.google.com"   # sonde : est-ce NOTRE réseau qui est tombé ?

# Les deux seuls codes qui autorisent à dire « votre site est mort ». Tout le reste
# (403 de pare-feu, 429, 5xx, TLS, timeout) dit seulement qu'on n'a pas pu regarder.
HTTP_MORT = (404, 410)

RESEAUX = ("facebook.com", "instagram.com", "linktr.ee", "tripadvisor.", "thefork.",
           "ubereats.com", "deliveroo.", "just-eat.", "pagesjaunes.fr", "linkedin.com")

# Signatures de constructeurs de sites : (nom, motifs de <meta generator>,
# hôtes de ressources chargées, suffixes du domaine audité).
# On n'accepte QUE des preuves non ambiguës. Une sous-chaîne libre dans le HTML ne
# suffit pas : « sitewide » n'est pas SiteW, et Jetpack n'est pas WordPress.com.
CONSTRUCTEURS = (
    ("Wix", ("wix",),
     ("static.parastorage.com", "static.wixstatic.com"), (".wixsite.com",)),
    ("Jimdo", ("jimdo",),
     ("jimdo-storage", "assets.jimstatic.com"), (".jimdosite.com", ".jimdo.com")),
    ("e-monsite", ("e-monsite",), ("e-monsite.com",), (".e-monsite.com",)),
    ("1&1 / IONOS SiteBuilder", ("1&1", "ionos", "mywebsite"),
     ("mywebsite-editor.com",), ()),
    ("Solocal / PagesJaunes", ("solocal",), ("solocal.com",), ()),
    ("Webnode", ("webnode",), ("webnode.com",), (".webnode.fr", ".webnode.com")),
    ("SiteW", ("sitew",), ("sitew.com",), (".sitew.com",)),
    ("WordPress.com", ("wordpress.com",), ("files.wordpress.com",), (".wordpress.com",)),
)

# Dernière année de correctif de chaque branche jQuery abandonnée : on ne date plus
# au hasard (1.12.4 et 2.2.4 sont de 2016, pas de 2012).
JQUERY_FIN_SUPPORT = {"1": "2016", "2": "2016"}

# Identifiants de conteneur racine des frameworks : le HTML servi est alors vide,
# tout le contenu arrive par JavaScript. On ne peut rien constater dessus.
RACINES_SPA = {"root", "app", "__next", "__nuxt", "__layout", "q-app", "svelte"}
# Angular ne pose aucun id : il pose un ÉLÉMENT personnalisé <app-root>. D'autres
# gabarits écrivent id="app-root" ou id="react-root". Les trois formes signent la
# même chose : une page dont le contenu n'arrive qu'après le JavaScript.
ELEMENT_RACINE = re.compile(r"^[a-z][a-z0-9]*-(?:root|app)$")
ID_RACINE = re.compile(r"(?:^|[-_])root$")

# Une feuille d'impression n'a jamais rendu un site adaptatif, et la quasi-totalité
# des thèmes 2005-2015 — le parc visé — en portent une. Accepter « @media » tout
# court faisait passer ces sites pour adaptatifs : on exige une media query de
# LARGEUR, seul marqueur réel d'une mise en page qui s'adapte au téléphone.
MEDIA_LARGEUR = re.compile(
    r"@media[^{]{0,400}(?:(?:max|min)-(?:device-)?width"
    r"|\bwidth\s*[<>]=?|[<>]=?\s*\d+\s*(?:px|em|rem))", re.I)

# Types schema.org qui intéressent un commerce local, mis en avant devant WebSite.
TYPES_LD_PARLANTS = ("localbusiness", "restaurant", "bakery", "store", "cafe",
                     "hairsalon", "beautysalon", "foodestablishment", "organization")

TITRES_GENERIQUES = {"accueil", "home", "bienvenue", "index", "untitled document",
                     "nouveau site", "site en construction", "mon site"}


class SiteInjoignable(Exception):
    """Le site est réellement mort : 404/410, ou domaine qui n'existe pas.

    C'est un constat qu'on peut écrire au commerçant.
    """

    def __init__(self, raison: str, preuve: str = ""):
        super().__init__(raison)
        self.raison = raison
        self.preuve = preuve


class AuditIndisponible(Exception):
    """On n'a pas pu observer le site : 403, 429, 5xx, TLS, timeout, anti-bot.

    On n'écrit RIEN à ce prospect : le verdict est `non_auditable`.
    """

    def __init__(self, raison: str, preuve: str = ""):
        super().__init__(raison)
        self.raison = raison
        self.preuve = preuve


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
        self.styles_inline = []
        self._dans_style = False
        self._dans_script = False
        self.favicon = False
        self.images = 0
        self.images_lazy = 0
        self.images_srcset = 0
        self.liens_tel = 0
        self.liens_mailto = []
        self.balises_obsoletes = set()
        self.tableaux = 0
        self.scripts = []
        self.scripts_module = 0
        self.bundles = 0
        self.iframes = []
        self.texte = []
        self.formulaires = 0
        self.a_hreflang = False
        self.generateur = ""
        # Preuve d'un VRAI objet Flash (<object>/<embed>), pas un mot croisé au
        # hasard dans un script d'analytics.
        self.flash = ""
        # <div id="root"> & co : signature d'une page rendue côté navigateur.
        self.conteneur_racine = ""

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
            elif nom == "generator":
                self.generateur = a.get("content", "")
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
            self._dans_script = True
            type_script = a.get("type", "").lower()
            if type_script == "application/ld+json":
                self._dans_jsonld = True
            if type_script == "module":
                self.scripts_module += 1
            if a.get("src"):
                self.scripts.append(a["src"])
                # Empreinte d'un bundle moderne (Vite, Next, webpack) : utile pour
                # reconnaître une page dont le contenu n'arrive qu'après le JS.
                if re.search(r"/_next/|/assets/index-|[.-][0-9a-f]{8,}\.m?js|chunk",
                             a["src"], re.I):
                    self.bundles += 1
        elif tag == "style":
            self._dans_style = True
        elif tag in ("object", "embed", "param"):
            valeurs = " ".join((a.get("type", ""), a.get("src", ""),
                                a.get("data", ""), a.get("value", ""))).lower()
            if "application/x-shockwave-flash" in valeurs or ".swf" in valeurs:
                self.flash = (a.get("src") or a.get("data") or a.get("value")
                              or a.get("type") or f"<{tag}>")[:120]
        elif tag in ("div", "main", "section"):
            ident = a.get("id", "").strip()
            bas = ident.lower()
            if bas and (bas in RACINES_SPA or ID_RACINE.search(bas)):
                self.conteneur_racine = ident
        elif ELEMENT_RACINE.match(tag):
            # <app-root> (Angular), <ng-app>, <my-app> : un élément personnalisé,
            # sans id, que la garde précédente ne pouvait pas voir.
            self.conteneur_racine = f"<{tag}>"
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
            self._dans_script = False
        elif tag == "style":
            self._dans_style = False

    def handle_data(self, data):
        if self._dans_titre:
            self.titre += data.strip()
        elif self._dans_h1:
            self.h1.append(data.strip())
        elif self._dans_jsonld:
            self.jsonld.append(data)
        elif self._dans_style:
            self.styles_inline.append(data)
        elif self._dans_script:
            # Le code d'un script n'est pas du texte visible : on n'en déduit rien.
            # C'est ce qui faisait citer le « © 2018 » d'une bannière de licence.
            pass
        else:
            t = data.strip()
            if t:
                self.texte.append(t)


_reseau_ok = False


def _reseau_operationnel() -> bool:
    """Notre machine résout-elle encore les noms ?

    Sans cette sonde, une coupure DNS locale ferait passer TOUT un lot de prospects
    en « injoignable » — et donc en mails affirmant que leur site est mort.
    """
    global _reseau_ok
    if _reseau_ok:
        return True
    try:
        socket.getaddrinfo(HOTE_TEMOIN, 443)
        _reseau_ok = True
    except OSError:
        _reseau_ok = False
    return _reseau_ok


def _classer_echec(e: Exception) -> Exception:
    """Traduit une exception réseau en l'un des deux seuls constats possibles."""
    if isinstance(e, urllib.error.HTTPError):
        if e.code in HTTP_MORT:
            return SiteInjoignable(f"erreur HTTP {e.code}", f"réponse HTTP {e.code}")
        return AuditIndisponible(f"réponse HTTP {e.code}", f"HTTP {e.code}")
    raison = getattr(e, "reason", None) or e
    if isinstance(raison, socket.gaierror):
        if _reseau_operationnel():
            return SiteInjoignable("le nom de domaine ne résout pas",
                                   "le domaine n'existe plus (DNS)")
        return AuditIndisponible("notre propre accès réseau est coupé", "réseau local")
    return AuditIndisponible(f"{type(raison).__name__}: {raison}"[:160],
                             type(raison).__name__)


def _erreur_certificat(e: Exception):
    """Isole une erreur de VÉRIFICATION du certificat parmi les pannes TLS.

    Un certificat expiré, auto-signé ou émis pour un autre domaine n'est pas une
    incertitude de mesure : c'est un constat daté, que le commerçant vérifie en
    ouvrant son propre site. On le distingue donc d'un handshake qui casse ou d'un
    délai dépassé, qui eux ne disent rien.
    """
    for candidat in (e, getattr(e, "reason", None)):
        if isinstance(candidat, ssl.SSLCertVerificationError):
            return candidat
    return None


def _contexte_sans_verification() -> ssl.SSLContext:
    """Contexte TLS qui ne vérifie rien. Uniquement pour aller CONSTATER une page
    dont on sait déjà que le certificat est refusé par les navigateurs."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# Dates de validité dans un certificat DER : ASN.1 UTCTime (0x17, 13 octets) ou
# GeneralizedTime (0x18, 15 octets). Sans bibliothèque tierce, c'est la seule façon
# de lire la date d'expiration d'un certificat que la pile TLS a refusé.
_DATES_DER = re.compile(rb"\x17\x0d(\d{12})Z|\x18\x0f(\d{14})Z")


def _fin_validite(hote: str, port: int) -> str:
    """Date d'expiration du certificat (AAAA-MM-JJ), lue sans le vérifier. "" = illisible.

    On ne cite une date au commerçant que si les deux dates du certificat se lisent
    ET s'ordonnent : une preuve approximative vaut moins que pas de preuve du tout.
    """
    try:
        with socket.create_connection((hote, port), timeout=8) as prise:
            with _contexte_sans_verification().wrap_socket(
                    prise, server_hostname=hote) as tls:
                der = tls.getpeercert(True) or b""
    except (OSError, ValueError):
        return ""
    dates = []
    for court, long in _DATES_DER.findall(der):
        texte = (court or long).decode()
        try:
            dates.append(_dt.datetime.strptime(
                texte, "%y%m%d%H%M%S" if court else "%Y%m%d%H%M%S"))
        except ValueError:
            return ""
        if len(dates) == 2:
            break
    if len(dates) != 2 or dates[0] >= dates[1]:
        return ""
    return dates[1].date().isoformat()


def _constat_certificat(url: str, faute) -> dict:
    """Transforme un certificat refusé en constat écrit, avec sa preuve."""
    try:
        p = urllib.parse.urlsplit(url)
        hote, port = p.hostname or "", p.port or 443
    except ValueError:
        hote, port = "", 443
    motif = (getattr(faute, "verify_message", "") or str(faute)).strip()
    motif = re.sub(r"\s*\(_ssl\.c:\d+\)", "", motif)[:120]
    fin = _fin_validite(hote, port) if hote else ""
    preuve = f"{hote or url} : certificat refusé par le navigateur — {motif}"
    if fin:
        preuve += f" ; fin de validité au {fin}"
    return {"motif": motif, "expire_le": fin, "preuve": preuve}


def _reessayable(e: Exception) -> bool:
    """Cet échec mérite-t-il une seconde chance ? (pic de charge, coupure passagère)"""
    if isinstance(e, urllib.error.HTTPError):
        return e.code == 429 or 500 <= e.code <= 599
    if isinstance(e, urllib.error.URLError):
        return not isinstance(getattr(e, "reason", None), socket.gaierror)
    return isinstance(e, (TimeoutError, ssl.SSLError, ConnectionError, OSError))


def _degzip(donnees: bytes) -> bytes | None:
    """Décompresse un flux gzip, y compris tronqué par TAILLE_MAX. None = illisible.

    `gzip.GzipFile.read()` lève EOFError sur un flux coupé, et EOFError n'hérite pas
    d'OSError : l'exception remontait jusqu'à interrompre le run entier.
    """
    try:
        return gzip.GzipFile(fileobj=io.BytesIO(donnees)).read(TAILLE_MAX + 1)
    except (OSError, EOFError):
        pass
    try:
        return zlib.decompressobj(31).decompress(donnees, TAILLE_MAX + 1)
    except zlib.error:
        return None


def _charset(entetes: dict, brut: bytes) -> str:
    """Charset de l'en-tête HTTP, sinon du <meta charset> du document. "" = inconnu.

    Les vieux hébergements — c'est-à-dire notre cible — servent souvent du
    ISO-8859-1 sans le déclarer dans l'en-tête. Décoder en utf-8 « à l'aveugle »
    remplissait la page de U+FFFD : toutes les regex accentuées (« réserver »)
    échouaient et le titre cité dans le mail était illisible.
    """
    m = re.search(r"charset\s*=\s*[\"']?([\w.:+-]+)", entetes.get("content-type", ""), re.I)
    if not m:
        tete = brut[:4096].decode("ascii", "ignore")
        m = re.search(r"<meta[^>]+charset\s*=\s*[\"']?\s*([\w.:+-]+)", tete, re.I)
    if not m:
        return ""
    nom = m.group(1).strip().strip("\"';")
    try:
        codecs.lookup(nom)   # un charset exotique ne doit pas tuer le run entier
    except (LookupError, ValueError):
        return ""
    return nom


# Relire de l'utf-8 en cp1252 laisse une signature reconnaissable (« é » → « Ã© »,
# « — » → « â€” ») : c'est elle qui permet de départager deux décodages, alors que
# le mojibake, lui, ne contient aucun U+FFFD et passait tous les garde-fous.
# On exige la séquence utf-8 COMPLÈTE (une tête, puis le bon nombre de suites), sans
# quoi un « l'été… » parfaitement décodé serait compté comme abîmé.
_SUITE = "[" + re.escape(bytes(range(0x80, 0xC0)).decode("cp1252", "ignore")) + "]"
_MOJIBAKE = re.compile(f"[\u00c2-\u00df]{_SUITE}"
                       f"|[\u00e0-\u00ef]{_SUITE}{{2}}"
                       f"|[\u00f0-\u00f4]{_SUITE}{{3}}")


def _suspects(texte: str) -> int:
    """Caractères qui trahissent un mauvais décodage : perdus (U+FFFD) ou mojibake."""
    return texte.count("\ufffd") + sum(1 for _ in _MOJIBAKE.finditer(texte))


def _decoder(brut: bytes, entetes: dict) -> str:
    """Décode le HTML en gardant TOUJOURS le décodage le moins abîmé.

    Compter les U+FFFD ne suffit pas : une page réellement utf-8 portant six octets
    invalides — sortie banale d'un vieux CMS français — basculait tout entière en
    cp1252, et chaque accent devenait du mojibake ; comme le mojibake ne contient
    aucun U+FFFD, plus aucun garde-fou ne le rattrapait. Et un en-tête
    « iso-8859-1 » posé sur une page utf-8 se décode sans la moindre erreur, tout en
    donnant le même charabia.

    On met donc en concurrence les décodages plausibles et on garde celui qui abîme
    le moins de caractères. À égalité, c'est ce que le serveur annonce qui gagne.
    """
    annonce = _charset(entetes, brut) or "utf-8"
    candidats: list[str] = []

    def proposer(nom: str, erreurs: str) -> None:
        try:
            candidats.append(brut.decode(nom, erreurs))
        except (UnicodeDecodeError, LookupError):
            pass

    proposer(annonce, "strict")
    if candidats and not _suspects(candidats[0]):
        return candidats[0]        # l'en-tête dit vrai et rien n'est abîmé : terminé
    proposer("utf-8", "strict")
    # Les décodages « avec pertes » passent AVANT le repli cp1252 : à dégât égal, six
    # caractères perdus valent mieux qu'une page entière de mojibake.
    proposer(annonce, "replace")
    proposer("utf-8", "replace")
    proposer("cp1252", "strict")                 # le vieux web francophone, en dernier
    return min(candidats, key=_suspects) if candidats else brut.decode("cp1252", "replace")


def _telecharger_detail(url: str, timeout: int = TIMEOUT,
                        tentatives: int = TENTATIVES) -> dict:
    """Télécharge une page, avec reprises. Lève SiteInjoignable / AuditIndisponible.

    `octets_html` est le poids du HTML DÉCOMPRESSÉ : c'est lui qu'on cite dans le
    mail (« X Ko de HTML »). Mesuré avant décompression, il valait le poids gzip et
    aucun seuil de poids ne pouvait plus se déclencher.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Encoding": "gzip",
            "Accept-Language": "fr-FR,fr;q=0.9",
        },
    )

    def _essai(contexte: ssl.SSLContext) -> dict:
        debut = time.monotonic()
        with urllib.request.urlopen(req, timeout=timeout, context=contexte) as r:
            ttfb = int((time.monotonic() - debut) * 1000)
            # Un octet de plus que le plafond : c'est ce qui permet de SAVOIR qu'on
            # a coupé, au lieu de le deviner à un poids rond.
            transfere = r.read(TAILLE_MAX + 1)
            entetes = {k.lower(): v for k, v in r.headers.items()}
            url_finale = r.geturl()
        tronque = len(transfere) > TAILLE_MAX
        transfere = transfere[:TAILLE_MAX]
        brut = transfere
        if entetes.get("content-encoding", "").lower() in ("gzip", "x-gzip"):
            brut = _degzip(transfere)
            if brut is None:
                # HTML illisible : surtout ne pas auditer une page vide, sinon
                # tous les contrôles de contenu se déclencheraient à tort.
                raise AuditIndisponible("réponse compressée illisible", "gzip corrompu")
            if len(brut) > TAILLE_MAX:
                brut, tronque = brut[:TAILLE_MAX], True
        return {
            "html": _decoder(brut, entetes),
            "url_finale": url_finale,
            "ttfb_ms": ttfb,
            "octets_html": len(brut),
            "octets_transferes": len(transfere),
            "entetes": entetes,
            "tronque": tronque,
            "certificat_invalide": None,
        }

    ctx = ssl.create_default_context()
    certificat: dict | None = None
    dernier: Exception | None = None
    for essai in range(max(1, tentatives)):
        if essai:
            time.sleep(ATTENTES[min(essai - 1, len(ATTENTES) - 1)])
        try:
            reponse = _essai(ctx)
            reponse["certificat_invalide"] = certificat
            return reponse
        except (AuditIndisponible, SiteInjoignable):
            raise
        except Exception as e:   # noqa: BLE001 - on classe ensuite, on ne masque rien
            faute = _erreur_certificat(e)
            if faute is not None and certificat is None:
                # Certificat expiré ou auto-signé : on le retient comme constat, puis
                # on va lire la page sans vérifier — sinon on jetterait le prospect le
                # plus démarchable de la campagne pour la raison même qui le rend
                # démarchable.
                certificat = _constat_certificat(url, faute)
                ctx = _contexte_sans_verification()
                try:
                    reponse = _essai(ctx)
                    reponse["certificat_invalide"] = certificat
                    return reponse
                except (AuditIndisponible, SiteInjoignable):
                    raise
                except Exception as apres:   # noqa: BLE001
                    e = apres
            dernier = e
            if not _reessayable(e):
                break
    raise _classer_echec(dernier or AuditIndisponible("échec inconnu"))


def _telecharger(url: str, timeout: int = TIMEOUT) -> tuple[str, str, int, int, dict]:
    """Compat : (contenu, url_finale, ttfb_ms, octets_html, entetes). Une seule tentative."""
    d = _telecharger_detail(url, timeout=timeout, tentatives=1)
    return d["html"], d["url_finale"], d["ttfb_ms"], d["octets_html"], d["entetes"]


# Mémoire des petites sondes (robots.txt, sitemap, favicon) le temps d'un audit :
# robots.txt est interrogé pour lui-même et pour sa directive Sitemap:.
_SONDES: dict[str, tuple[int | None, bytes]] = {}
# Contexte TLS des sondes, le temps d'un audit. Il ne s'écarte du contexte vérifié
# que sur un site dont on a DÉJÀ constaté le certificat invalide : sans cela, ce
# site perdait aussi ses contrôles robots.txt, sitemap, favicon et feuilles de style.
_CONTEXTE_SONDE: ssl.SSLContext | None = None


def _recuperer(url: str, taille: int = 4096, timeout: int = 8,
               memoire: bool = True) -> tuple[int | None, bytes]:
    """GET court. Renvoie (code HTTP, début du corps). code None = on n'a pas pu regarder."""
    if memoire and url in _SONDES:
        return _SONDES[url]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA}, method="GET")
        with urllib.request.urlopen(req, timeout=timeout, context=_CONTEXTE_SONDE) as r:
            res = (r.status, r.read(taille))
    except urllib.error.HTTPError as e:
        res = (e.code, b"")
    except Exception:
        res = (None, b"")
    if memoire:
        _SONDES[url] = res
    return res


def _sonder(url: str) -> tuple[bool | None, str]:
    """(existe, motif). True / False (404-410 ou fichier vide) / None = indéterminé.

    Le `return False` sur exception d'avant transformait n'importe quel aléa réseau
    en défaut constaté, donc en reproche écrit au commerçant. Le motif, lui, sert à
    écrire la preuve exacte : un robots.txt vide servi en 200 n'est pas un 404.
    """
    code, corps = _recuperer(url)
    if code is None:
        return None, "aucune réponse"
    if code == 200:
        if corps.strip():
            return True, "HTTP 200"
        return False, "répond HTTP 200 mais le fichier est vide"
    if code in HTTP_MORT:
        return False, f"renvoie {code}"
    return None, f"HTTP {code}"


def _bases(url_finale: str) -> list[str]:
    """Racine du domaine ET répertoire du site (page perso, sous-dossier mutualisé).

    Un site servi sous /mon-commerce/ déclare ses fichiers dans son répertoire :
    les chercher uniquement à la racine du domaine les déclarait toujours absents.
    """
    try:
        p = urllib.parse.urlsplit(url_finale)
    except ValueError:
        return []          # URL inexploitable : on ne conclut rien, on ne sonde rien
    racine = f"{p.scheme}://{p.netloc}"
    dossier = p.path.rsplit("/", 1)[0]
    bases = [racine + dossier] if dossier and dossier != "/" else []
    bases.append(racine)
    return bases


def _diagnostic_fichier(url_finale: str,
                        chemins: tuple[str, ...]) -> tuple[bool | None, str, str]:
    """(existe, où, motif). Cherche dans le répertoire du site puis à la racine.

    Le motif est la formulation exacte du constat : c'est lui qui part en preuve,
    plutôt qu'un « renvoie 404 » écrit en dur qui pouvait être faux.
    """
    bases = _bases(url_finale)
    if not bases:
        return None, "", "URL inexploitable"
    indetermine = False
    motif = "introuvable"
    for base in bases:
        for chemin in chemins:
            r, m = _sonder(base + chemin)
            if r:
                return True, base + chemin, m
            if r is None:
                indetermine = True
            else:
                motif = m
    return (None if indetermine else False), "", motif


def _cherche_fichier(url_finale: str, chemins: tuple[str, ...]) -> tuple[bool | None, str]:
    """Cherche un fichier dans le répertoire du site puis à la racine du domaine."""
    existe, ou, _ = _diagnostic_fichier(url_finale, chemins)
    return existe, ou


def _cherche_sitemap(url_finale: str) -> tuple[bool | None, str]:
    """Sitemap là où Google le cherche : /sitemap.xml, /sitemap_index.xml, robots.txt."""
    trouve, ou = _cherche_fichier(url_finale, ("/sitemap.xml", "/sitemap_index.xml"))
    if trouve:
        return True, ou
    indetermine = trouve is None
    for base in _bases(url_finale):
        code, corps = _recuperer(base + "/robots.txt")
        if code == 200:
            m = re.search(rb"(?im)^\s*sitemap\s*:\s*(\S+)", corps)
            if m:
                return True, m.group(1).decode("utf-8", "replace")[:160]
        elif code is None:
            indetermine = True
    return (None if indetermine else False), ""


def _media_queries_externes(feuilles: list[str], url_finale: str,
                            maxi: int = 5) -> tuple[bool | None, int]:
    """(réponse, feuilles réellement lues). None = illisible, on ne conclut rien.

    Sans ce contrôle, tout site responsive dont le CSS est externe — c'est-à-dire
    la norme — se voyait reprocher une « mise en page peu adaptative ». Et l'ordre
    de chargement d'un thème (fonts, icônes, thème, framework) place presque
    toujours les media queries dans la DERNIÈRE feuille : s'arrêter à deux feuilles
    de 400 ko revenait à ne pas regarder là où elles sont.
    """
    hrefs = [h for h in feuilles if h][:maxi]
    if not hrefs:
        return False, 0
    lues = 0
    for href in hrefs:
        code, corps = _recuperer(urllib.parse.urljoin(url_finale, href),
                                 taille=1_000_000, timeout=8, memoire=False)
        if code != 200 or not corps:
            continue
        lues += 1
        if MEDIA_LARGEUR.search(corps.decode("utf-8", "replace")):
            return True, lues
    return (False if lues else None), lues


def _pluriel(n: int, singulier: str, pluriel: str) -> str:
    """« 1 photo », « 3 photos ». Un « photo(s) » non résolu partait jusque dans le
    corps du mail envoyé au commerçant."""
    return f"{n} {singulier if abs(n) <= 1 else pluriel}"


def _liste_json(valeur) -> list:
    """Liste d'un champ stocké en JSON. Tolérante : un champ abîmé en base ne doit
    pas interrompre l'audit de tout un lot."""
    if isinstance(valeur, list):
        return valeur
    if not valeur:
        return []
    try:
        charge = json.loads(valeur)
    except (TypeError, ValueError):
        return []
    return charge if isinstance(charge, list) else []


def _entier(valeur, defaut: int = 0) -> int:
    """Entier d'un champ de base, ou `defaut` si la valeur est inexploitable."""
    try:
        return int(float(valeur))
    except (TypeError, ValueError):
        return defaut


def _decimal(valeur):
    """Nombre décimal d'un champ de base, ou None si la valeur est inexploitable."""
    try:
        return float(valeur)
    except (TypeError, ValueError):
        return None


def _defaut(code, libelle, poids, preuve="", argument=""):
    return {"code": code, "libelle": libelle, "poids": poids,
            "preuve": preuve, "argument": argument}


def _resultat(verdict: str, url_finale: str = "", **extra) -> dict:
    """Squelette commun : toutes les sorties portent les mêmes clés, quel que soit
    le chemin pris. `demarchable` dit si on a le droit d'écrire à ce prospect."""
    res = {
        "verdict": verdict,
        "score": None,
        "score_brut_penalites": None,
        "url_finale": url_finale,
        "poids_ko": None,
        "poids_transfere_ko": None,
        "ttfb_ms": None,
        "defauts": [],
        "atouts": [],
        "emails_trouves": [],
        "titre": "",
        "raison": "",
        "demarchable": verdict in ("absent", "obsolete", "injoignable"),
    }
    res.update(extra)
    return res


def _types_jsonld(blocs: list[str]) -> list[str]:
    """Types schema.org, y compris la forme @graph (Yoast, Rank Math, WordPress).

    C'est la forme dominante chez les petits commerces : la lire à plat revenait à
    reprocher « aucune donnée structurée » à des sites qui en ont.
    """
    types: list[str] = []

    def _ajouter(noeud):
        if isinstance(noeud, list):
            for x in noeud:
                _ajouter(x)
        elif isinstance(noeud, dict):
            t = noeud.get("@type")
            if isinstance(t, list):
                types.extend(str(x) for x in t if x)
            elif t:
                types.append(str(t))
            for x in noeud.get("@graph") or []:
                _ajouter(x)

    for bloc in blocs:
        try:
            _ajouter(json.loads(bloc))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    # Un « LocalBusiness » parle davantage au commerçant qu'un « WebSite ».
    types.sort(key=lambda t: 0 if any(x in t.lower() for x in TYPES_LD_PARLANTS) else 1)
    return types


def _constructeur(p: "_Extracteur", url_finale: str) -> tuple[str, str]:
    """Constructeur du site, sur signature non ambiguë. Renvoie (nom, preuve).

    L'ancienne recherche par sous-chaîne libre classait « sitewide » en SiteW et
    tout WordPress équipé de Jetpack en « WordPress.com gratuit ».
    """
    generateur = (p.generateur or "").strip()
    hote = urllib.parse.urlsplit(url_finale).netloc.lower()
    ressources = " ".join(p.scripts + p.css_externes + p.iframes).lower()
    for nom, generateurs, hotes, suffixes in CONSTRUCTEURS:
        if generateur and any(g in generateur.lower() for g in generateurs):
            return nom, f'<meta name="generator" content="{generateur[:70]}">'
        for h in hotes:
            if h in ressources:
                return nom, f"ressources chargées depuis {h}"
        for s in suffixes:
            if hote.endswith(s) or hote == s.lstrip("."):
                return nom, f"domaine {hote}"
    return "", ""


def _annee_copyright(textes: list[str], annee: int) -> tuple[int, str]:
    """Année de copyright la plus récente du texte VISIBLE. (0, "") si aucune.

    Lue dans le HTML brut, la regex tombait sur les bannières de licence des
    bibliothèques (« Copyright 2011-2018 Twitter, Inc. ») : le texte des <script>
    est donc exclu en amont, par l'extracteur.

    Ne regarder que le dernier quart des nœuds ratait en revanche tous les pieds de
    page suivis de mentions légales, d'un bandeau cookies ou d'un plan de site —
    disposition très courante, et sur un site réellement abandonné c'est l'argument
    qui parle le plus au commerçant. On lit donc tout, et c'est l'année la plus
    RÉCENTE qui décide : un « © 2011 » ne peut pas faire passer pour à l'abandon un
    site qui affiche 2025 ailleurs.
    """
    meilleure, phrase = 0, ""
    for t in textes:
        for m in re.finditer(r"(?:©|\(c\)|copyright)\s*(?:\d{4}\s*[-–—]\s*)?(\d{4})",
                             t, re.I):
            an = int(m.group(1))
            # `>=` : à année égale, on garde la mention la plus basse dans la page,
            # c'est-à-dire le pied de page plutôt qu'un rappel de haut de colonne.
            if 1995 <= an <= annee and an >= meilleure:
                meilleure, phrase = an, " ".join(t.split())[:120]
    return meilleure, phrase


def _sans_accent(chaine: str) -> str:
    """« pâtisserie » et « patisserie » doivent se rencontrer."""
    return "".join(c for c in unicodedata.normalize("NFD", chaine.lower())
                   if unicodedata.category(c) != "Mn")


def _mots_utiles(chaine: str) -> list[str]:
    """Mots signifiants d'un libellé, sans accents ni ponctuation."""
    return [m for m in re.split(r"[^0-9a-z]+", _sans_accent(chaine)) if len(m) > 3]


def _meme_radical(a: str, b: str) -> bool:
    """« pizzeria » et « pizza », « coiffure » et « coiffeur » : le même métier.

    La catégorie Google est un libellé normalisé (« Pizzeria »,
    « Boulangerie-pâtisserie ») qui ne coïncide presque jamais avec le mot
    d'enseigne. Comparer les mots entiers faisait écrire au patron de la « Pizza
    Vesuvio » que son titre ne dit pas son activité — sous ses yeux. On compare
    donc les radicaux, et dans le doute on considère que le métier EST dit : mieux
    vaut un argument de moins qu'un argument faux.
    """
    commun = 0
    for x, y in zip(a, b):
        if x != y:
            break
        commun += 1
    return commun >= 4 and commun >= min(len(a), len(b)) * 0.5


def _manques_titre(titre: str, ville: str | None, categorie: str | None) -> list[str]:
    """Ce qui manque VRAIMENT au titre, quand on connaît la ville et le métier.

    Sans ces données, on ne prétend rien : l'argument se rabat sur la longueur.
    """
    bas = _sans_accent(titre)
    mots_titre = _mots_utiles(titre)
    absents = []
    if categorie and categorie.strip():
        mots = _mots_utiles(categorie)
        if mots and not any(_meme_radical(m, t) for m in mots for t in mots_titre):
            absents.append("votre activité")
    if ville and ville.strip():
        vue = (_sans_accent(ville.strip()) in bas
               or any(_meme_radical(m, t)
                      for m in _mots_utiles(ville) for t in mots_titre))
        if not vue:
            absents.append("votre ville")
    return absents


def _ni(manques: list[str]) -> str:
    """« pas votre ville » / « ni votre activité ni votre ville » : la conjonction
    correcte, plutôt qu'un « ni » orphelin dans un mail dont tout l'argumentaire
    repose sur le soin apporté aux détails."""
    if len(manques) == 1:
        return "pas " + manques[0]
    return " ".join("ni " + m for m in manques)


def _reseaux_seulement(hote: str, url_finale: str = "") -> dict:
    """Verdict « pas de site » quand la seule vitrine est une page de plateforme."""
    via = (f"votre domaine renvoie vers {hote}" if url_finale
           else f"la fiche Google pointe vers {hote}")
    return _resultat(
        "absent", url_finale or hote,
        defauts=[_defaut(
            "reseaux_seulement",
            "Aucun site web : la seule vitrine est une page tierce",
            0, via,
            "Une page Facebook ou une fiche annuaire ne se positionne pas sur "
            "Google et ne vous appartient pas : la plateforme peut la fermer "
            "ou la monétiser du jour au lendemain.")])


def _netloc(url: str) -> str | None:
    """Hôte d'une URL, ou None si l'URL est inexploitable.

    `urlsplit` lève ValueError sur une IPv6 mal formée : sans ce filet, un seul
    champ « site » abîmé sur une fiche Google interrompait l'audit de tout le lot.
    """
    try:
        return urllib.parse.urlsplit(url).netloc.lower()
    except ValueError:
        return None


def _site_mort(url: str, e: SiteInjoignable) -> dict:
    """Verdict `injoignable` : le seul cas où l'on ose écrire « votre site ne
    répond pas »."""
    return _resultat(
        "injoignable", url, score=0, raison=e.raison,
        defauts=[_defaut("site_hs", "Le site ne répond pas", 100,
                         f"{url} → {e.preuve or e.raison}",
                         "Le site renseigné sur votre fiche Google est inaccessible : "
                         "chaque visiteur qui clique tombe sur une erreur.")])


def auditer_site(url: str, seuil_obsolete: int = 60, *,
                 ville: str | None = None, categorie: str | None = None,
                 seuils: dict | None = None) -> dict:
    """Audit d'une URL. Renvoie verdict, score /100, défauts détaillés.

    `ville` et `categorie` sont facultatifs : fournis, ils permettent de dire ce qui
    manque vraiment au titre plutôt que de l'affirmer. `seuils` accepte le bloc
    `seuils` de la configuration (`site_obsolete`, `site_correct`).

    Verdicts possibles : absent, obsolete, correct, injoignable, non_auditable.
    `demarchable` dit, en plus du verdict, si l'on a le droit d'écrire à ce
    prospect : c'est là que `site_correct` agit, sur la bande des sites moyens.
    """
    global _CONTEXTE_SONDE
    defauts: list[dict] = []
    atouts: list[str] = []
    seuils = seuils or {}
    _CONTEXTE_SONDE = None
    seuil_obsolete = int(seuils.get("site_obsolete", seuil_obsolete))
    # `site_correct` borné par `site_obsolete` : un site jugé obsolète doit rester
    # démarchable quoi qu'on mette dans la configuration, sinon un seuil mal réglé
    # vide silencieusement la campagne de ses meilleurs prospects.
    seuil_correct = max(int(seuils.get("site_correct", max(seuil_obsolete, 75))),
                        seuil_obsolete)
    _SONDES.clear()
    url = url.strip()
    if not url:
        return _resultat("absent")

    schema_ajoute = not url.startswith(("http://", "https://"))
    if schema_ajoute:
        url = "https://" + url

    hote = _netloc(url)
    if hote is None:
        return _resultat("non_auditable", url, demarchable=False,
                         raison="URL inexploitable sur la fiche Google")
    if any(r in hote for r in RESEAUX):
        return _reseaux_seulement(hote)

    try:
        reponse = _telecharger_detail(url)
    except SiteInjoignable as e:
        return _site_mort(url, e)
    except AuditIndisponible as e:
        # Le https:// vient de nous, pas de la fiche : un serveur qui n'écoute qu'en
        # clair n'est pas « non auditable », il est en HTTP — et c'est un défaut à
        # 20 points qu'on jetterait avec le prospect.
        reponse = None
        if schema_ajoute:
            repli = "http://" + url[len("https://"):]
            try:
                reponse = _telecharger_detail(repli)
            except SiteInjoignable as mort:
                return _site_mort(repli, mort)
            except AuditIndisponible:
                reponse = None
        if reponse is None:
            # On n'a pas pu regarder : un 403 de pare-feu, un 5xx ou un timeout ne
            # dit rien du site. Aucun défaut, aucun mail — le contraire serait une
            # phrase fausse envoyée à quelqu'un.
            return _resultat("non_auditable", url, demarchable=False,
                             raison=f"site non auditable ({e.raison})")

    html = reponse["html"]
    url_finale = reponse["url_finale"]
    ttfb = reponse["ttfb_ms"]

    # Une redirection vers Facebook & co n'est pas un site : c'est le cas fréquent
    # du vieux domaine laissé pointer vers la page de la plateforme.
    hote_final = _netloc(url_finale) or ""
    if any(r in hote_final for r in RESEAUX):
        return _reseaux_seulement(hote_final, url_finale)

    # Réponse vide ou tronquée : sans HTML, tous les contrôles se déclencheraient et
    # on reprocherait quinze manques à une page qu'on n'a pas vraiment reçue.
    if len(html.strip()) < 200:
        return _resultat("non_auditable", url_finale, demarchable=False, ttfb_ms=ttfb,
                         raison=f"réponse vide ou tronquée ({len(html.strip())} caractères "
                                "de HTML) : rien à auditer")

    p = _Extracteur()
    try:
        p.feed(html)
    except Exception:
        pass

    texte_visible = " ".join(p.texte).strip()
    # Page rendue par JavaScript : le HTML servi est un gabarit vide. Tout contrôle
    # de contenu y devient un reproche inventé (« pas de téléphone », « pas
    # d'horaires ») alors que le visiteur, lui, les voit.
    #
    # Exiger un conteneur racine laissait passer Angular, qui ne pose aucun id mais
    # un élément <app-root>. Un bundle moderne (script type=module, /_next/, nom de
    # fichier haché) suffit donc désormais à lui seul. Un site classique quasi vide,
    # lui, n'a ni conteneur racine ni bundle : il reste auditable, et c'est bien
    # l'un des meilleurs prospects de la campagne.
    if len(texte_visible) < 200 and p.scripts and (p.conteneur_racine or p.bundles):
        motif = (f"conteneur « {p.conteneur_racine} » vide" if p.conteneur_racine
                 else f"{p.bundles} script(s) de bundle, aucun texte servi")
        return _resultat(
            "non_auditable", url_finale, demarchable=False, ttfb_ms=ttfb,
            titre=p.titre.strip(),
            poids_ko=round(reponse["octets_html"] / 1024),
            poids_transfere_ko=round(reponse["octets_transferes"] / 1024),
            raison=f"site rendu côté navigateur ({motif}) : le contenu n'est pas "
                   "dans le HTML, il faudrait un vrai navigateur pour l'auditer")

    poids_ko = round(reponse["octets_html"] / 1024)
    annee = _dt.date.today().year
    # Un décodage raté sème des U+FFFD : dans ce cas on se tait sur tout ce qui se
    # lit dans le texte, plutôt que de citer un titre en charabia.
    texte_fiable = _suspects(texte_visible + p.titre) <= 2
    # Page coupée à TAILLE_MAX : le pied de page — donc le téléphone, les horaires
    # et le bouton de réservation — n'est pas dans ce qu'on a lu. On ne reproche pas
    # au commerçant ce qu'on n'a pas reçu.
    tronque = bool(reponse.get("tronque"))
    contenu_fiable = texte_fiable and not tronque
    mesure_poids = f"{'au moins ' if tronque else ''}{poids_ko} Ko de HTML"

    # --- Sécurité / transport -------------------------------------------------
    certificat = reponse.get("certificat_invalide")
    if certificat:
        # Les sondes qui suivent iraient droit dans le mur du même certificat.
        _CONTEXTE_SONDE = _contexte_sans_verification()
        # Constat daté et vérifiable en un clic par le commerçant lui-même : c'est
        # l'argument le plus fort de toute la campagne, il n'a rien à faire dans un
        # verdict « non auditable ».
        defauts.append(_defaut(
            "certificat_invalide", "Certificat de sécurité refusé par les navigateurs",
            25, certificat["preuve"],
            "Avant même d'atteindre votre site, le visiteur reçoit l'écran rouge "
            "« Votre connexion n'est pas privée » et un bouton « Retour à la "
            "sécurité ». La quasi-totalité des visiteurs s'arrêtent là, et Google "
            "déclasse le site."))
    if url_finale.startswith("http://"):
        defauts.append(_defaut(
            "pas_https", "Pas de HTTPS", 20, url_finale,
            "Depuis 2018, Chrome affiche « Non sécurisé » dans la barre d'adresse de "
            "vos visiteurs, et Google privilégie les sites sécurisés dans son "
            "classement."))
    elif not certificat:
        atouts.append("HTTPS actif")

    # --- Mobile ---------------------------------------------------------------
    if not p.viewport:
        defauts.append(_defaut(
            "pas_responsive", "Le site n'est pas adapté au téléphone", 22,
            "aucune balise <meta name=\"viewport\"> dans la page",
            "L'essentiel des recherches locales se fait sur mobile. Sur un téléphone, "
            "votre site s'affiche en version « bureau » miniature : il faut zoomer pour "
            "lire, et la plupart des visiteurs referment avant d'avoir trouvé vos "
            "horaires."))
    elif MEDIA_LARGEUR.search(" ".join(p.styles_inline)):
        atouts.append("Affichage mobile pris en charge")
    else:
        # Les media queries sont presque toujours dans une feuille externe : il faut
        # aller les y lire avant d'oser dire que le site s'adapte mal.
        externe, lues = _media_queries_externes(p.css_externes, url_finale)
        if externe is True:
            atouts.append("Affichage mobile pris en charge")
        elif externe is False:
            # La preuve ne parle que des feuilles RÉELLEMENT lues : annoncer quatre
            # feuilles quand on en a ouvert deux, c'est déjà une preuve fausse.
            defauts.append(_defaut(
                "responsive_partiel", "Mise en page peu adaptative", 8,
                "aucune media query de largeur (max-width / min-width), ni dans la "
                "page ni dans " + ("la seule feuille de style lue" if lues == 1
                                   else f"les {lues} feuilles de style lues" if lues
                                   else "aucune feuille de style liée"),
                "L'affichage mobile n'est que partiellement géré : la page ne change "
                "pas de mise en page selon la largeur de l'écran."))
        # externe is None : feuilles illisibles, on ne conclut rien.

    # --- Référencement --------------------------------------------------------
    titre = p.titre.strip()
    if not titre:
        defauts.append(_defaut(
            "titre_absent", "Balise <title> absente", 14,
            "aucune balise <title> dans le <head>",
            "Google n'a aucun titre à afficher dans ses résultats : votre ligne bleue "
            "est générée au hasard à partir du contenu."))
    elif texte_fiable and (titre.lower() in TITRES_GENERIQUES or len(titre) < 15):
        # On ne dit « ni le métier, ni la ville » que si on les a vraiment cherchés.
        manques = _manques_titre(titre, ville, categorie)
        if manques:
            argument = (f"Votre titre Google est « {titre} » : il ne dit "
                        + _ni(manques)
                        + ". C'est la première raison pour laquelle on ne vous trouve "
                        "pas en tapant votre activité + votre ville.")
        else:
            argument = (f"Votre titre Google est « {titre} », soit {len(titre)} "
                        "caractères : trop court pour porter à la fois votre nom, "
                        "votre activité et votre ville.")
        defauts.append(_defaut(
            "titre_generique", "Titre générique ou trop court", 12,
            f"« {titre} » ({len(titre)} caractères)", argument))
    elif texte_fiable:
        atouts.append("Balise title renseignée")

    if not p.meta_description:
        defauts.append(_defaut(
            "meta_description_absente", "Meta description absente", 9,
            "aucune balise <meta name=\"description\">",
            "Le texte affiché sous votre lien dans Google est bricolé automatiquement : "
            "aucune promesse, aucune raison de cliquer plutôt que sur le concurrent."))
    if not p.h1:
        defauts.append(_defaut(
            "h1_absent", "Aucun titre H1", 7, "aucune balise <h1> dans la page",
            "La page n'annonce pas son sujet : Google doit deviner de quoi vous parlez."))

    types_ld = _types_jsonld(p.jsonld)
    if not types_ld:
        defauts.append(_defaut(
            "donnees_structurees_absentes", "Aucune donnée structurée (schema.org)", 9,
            "aucun bloc application/ld+json exploitable"
            + (f" ({_pluriel(len(p.jsonld), 'bloc trouvé', 'blocs trouvés')})"
               if p.jsonld else ""),
            "Horaires, avis, adresse et menu ne remontent pas en « résultat enrichi ». "
            "C'est aussi ce que lisent désormais les assistants IA quand on leur demande "
            "où manger dans le quartier."))
    else:
        atouts.append("Données structurées présentes (" + ", ".join(types_ld[:3]) + ")")

    if p.og < 3:
        defauts.append(_defaut(
            "og_absent", "Partage social non configuré (Open Graph)", 4,
            (_pluriel(p.og, "balise og:", "balises og:") + " sur la page"
             if p.og else "aucune balise og: sur la page"),
            "Quand un client colle votre lien sur WhatsApp ou Instagram, il n'apparaît "
            "ni image ni description : le lien fait amateur, personne ne clique."))

    if _cherche_sitemap(url_finale)[0] is False:
        defauts.append(_defaut(
            "sitemap_absent", "Pas de sitemap.xml", 5,
            "ni /sitemap.xml, ni /sitemap_index.xml, ni directive Sitemap: dans robots.txt",
            "Google explore votre site à l'aveugle."))
    # Un robots.txt vide servi en 200 déclenche le même défaut qu'un 404 : la preuve
    # doit dire lequel des deux on a constaté, sinon elle est fausse une fois sur deux.
    robots, _, motif_robots = _diagnostic_fichier(url_finale, ("/robots.txt",))
    if robots is False:
        defauts.append(_defaut("robots_absent", "Pas de robots.txt", 3,
                               f"/robots.txt {motif_robots}", ""))
    if not p.favicon:
        favicon, _, motif_favicon = _diagnostic_fichier(url_finale, ("/favicon.ico",))
        if favicon is False:
            defauts.append(_defaut(
                "favicon_absente", "Pas d'icône d'onglet (favicon)", 3,
                f"aucun <link rel=\"icon\"> et /favicon.ico {motif_favicon}",
                "Votre onglet est une page blanche anonyme."))

    # --- Performance ----------------------------------------------------------
    if poids_ko > 2500:
        defauts.append(_defaut(
            "page_tres_lourde", "Page d'accueil très lourde", 12, mesure_poids,
            f"{'Au moins ' if tronque else ''}{poids_ko} Ko rien que pour le texte de "
            "la page : en 4G dans la rue, elle met plusieurs secondes à apparaître, et "
            "la plupart des visiteurs n'attendent pas."))
    elif poids_ko > 1200:
        defauts.append(_defaut("page_lourde", "Page d'accueil lourde", 6,
                               mesure_poids, ""))

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
    # Aucun de ces constats ne se fait par sous-chaîne dans le HTML brut : un mot
    # croisé au hasard dans un script d'analytics devenait « Flash », et le défaut
    # `techno_obsolete` étant le plus lourd du barème, il ouvrait le mail.
    obsoletes = []
    if p.balises_obsoletes:
        obsoletes.append("balises " + ", ".join(f"<{b}>" for b in sorted(p.balises_obsoletes)))
    if p.flash:
        obsoletes.append(f"objet Flash ({p.flash}), technologie morte depuis 2020")
    mj = re.search(r"jquery[.-](\d+)\.(\d+)(?:\.(\d+))?(?:\.min)?\.js",
                   " ".join(p.scripts).lower())
    if mj and int(mj.group(1)) < 3:
        version = ".".join(g for g in mj.groups() if g)
        fin = JQUERY_FIN_SUPPORT.get(mj.group(1), "")
        obsoletes.append(f"jQuery {version}, branche {mj.group(1)}.x abandonnée"
                         + (f" (plus aucun correctif depuis {fin})" if fin else ""))
    if p.tableaux >= 3 and not p.viewport:
        obsoletes.append(f"mise en page construite en tableaux ({p.tableaux} <table>)")
    if obsoletes:
        defauts.append(_defaut(
            "techno_obsolete", "Le site est bâti sur des techniques abandonnées", 14,
            " ; ".join(obsoletes),
            "Concrètement : l'affichage peut casser selon le navigateur du visiteur, "
            "et les briques les plus anciennes ne reçoivent plus de correctif de "
            "sécurité."))

    nom_constructeur, preuve_constructeur = _constructeur(p, url_finale)
    if nom_constructeur:
        defauts.append(_defaut(
            "constructeur_date", f"Site bâti sur {nom_constructeur}", 6,
            preuve_constructeur,
            f"Le site tourne sur {nom_constructeur} : gabarit partagé avec des "
            "milliers d'autres, et vous ne maîtrisez ni le code ni l'hébergement."))

    if contenu_fiable:
        an, phrase = _annee_copyright(p.texte, annee)
        if an and an <= annee - 2:
            defauts.append(_defaut(
                "copyright_perime", f"Mention de copyright datée {an}", 7,
                f"« {phrase} »",
                f"La page affiche encore « © {an} ». Un visiteur en déduit "
                "en une seconde que l'établissement a peut-être fermé — et vos "
                "horaires ou vos prix ne sont sans doute plus à jour non plus."))

    # --- Conversion / informations locales ------------------------------------
    # Le titre, les H1 et la meta description font partie du texte de la page : les
    # exclure du corps revenait à chercher l'appel à l'action partout sauf là où il est.
    corps = " ".join(p.texte + p.h1 + [titre, p.meta_description]).lower()
    if p.liens_tel == 0 and not tronque:
        defauts.append(_defaut(
            "pas_de_clic_pour_appeler", "Numéro non cliquable sur mobile", 8,
            "aucun lien tel: dans la page",
            "Sur téléphone, le client doit recopier votre numéro à la main pour vous "
            "appeler. À ce moment-là, la plupart renoncent ou appellent le concurrent."))
    if contenu_fiable and not p.liens_tel and not re.search(
            r"\b(?:0\d[\s.\-]?(?:\d{2}[\s.\-]?){4}|\+33)", corps):
        defauts.append(_defaut("telephone_absent", "Téléphone introuvable sur la page", 8,
                               "aucun numéro français dans le texte visible",
                               "Le premier réflexe d'un client local est d'appeler."))
    if contenu_fiable and not re.search(r"\b(?:lundi|mardi|horaires?|ouvert)\b", corps):
        defauts.append(_defaut(
            "horaires_absents", "Horaires non affichés", 7,
            "aucun des mots « lundi », « mardi », « horaires », « ouvert » dans le texte",
            "« Est-ce ouvert maintenant ? » est LA question du client local. "
            "Sans réponse sur le site, il retourne sur Google — et voit vos concurrents."))
    if contenu_fiable and not re.search(
            r"\b(r[ée]server|commander|prendre rendez-vous|devis|contact)\b", corps):
        defauts.append(_defaut(
            "pas_d_appel_a_action", "Aucun appel à l'action visible", 9,
            "aucun « réserver », « commander », « devis » ni « contact » dans la page",
            "La page informe mais ne fait rien faire : ni réservation, ni commande, "
            "ni demande de devis. Le visiteur repart sans laisser de trace."))

    # Contrat : un défaut sans preuve n'est jamais émis. C'est ce garde-fou qui
    # empêche une phrase non mesurée d'arriver dans un mail.
    defauts = [d for d in defauts if (d.get("preuve") or "").strip()]
    poids_total = sum(d["poids"] for d in defauts)
    # Plancher à 10 : un score de 0/100 sonne comme une insulte dans un mail,
    # alors qu'on veut ouvrir une conversation.
    score = max(10, 100 - poids_total)
    defauts.sort(key=lambda d: -d["poids"])
    # Un certificat refusé par les navigateurs tranche à lui seul : quelle que soit
    # la qualité du reste, personne n'atteint la page. Sans cela, un site par
    # ailleurs correct restait classé « correct » — et la campagne perdait son
    # meilleur argument faute d'un verdict qui la laisse écrire.
    return _resultat(
        "obsolete" if (score < seuil_obsolete or certificat) else "correct",
        url_finale,
        score=score,
        score_brut_penalites=poids_total,
        poids_ko=poids_ko,
        poids_transfere_ko=round(reponse["octets_transferes"] / 1024),
        ttfb_ms=ttfb,
        defauts=defauts,
        atouts=atouts,
        emails_trouves=p.liens_mailto,
        titre=titre,
        # `seuils.site_correct` sert enfin à quelque chose : au dessus, on ne démarche
        # pas. Ce n'est pas un verdict de plus, c'est un feu vert d'expédition.
        demarchable=bool(certificat) or score < seuil_correct,
    )


def auditer_fiche(etab: dict) -> tuple[int, list[dict]]:
    """Complétude de la fiche Google Business Profile, à partir des données Places.

    Utile même quand l'établissement a un bon site : c'est un second angle d'attaque,
    et c'est ce qui pèse le plus sur la visibilité locale.
    """
    manques = []
    score = 100
    if not (etab.get("site") or "").strip():
        score -= 25
        manques.append(_defaut("fiche_sans_site", "Fiche Google sans site web", 25,
                               "champ « site web » vide sur la fiche Google",
                               "Le bouton « Site Web » de votre fiche est vide."))
    if not (etab.get("telephone") or "").strip():
        score -= 15
        manques.append(_defaut("fiche_sans_tel", "Fiche sans numéro de téléphone", 15,
                               "aucun numéro sur la fiche Google", ""))
    horaires = _liste_json(etab.get("horaires"))
    if not horaires:
        score -= 18
        manques.append(_defaut(
            "fiche_sans_horaires", "Fiche sans horaires d'ouverture", 18,
            "aucun horaire publié sur la fiche Google",
            "Google ne peut pas afficher « Ouvert » sur votre fiche : vous disparaissez "
            "des recherches « ouvert maintenant », qui sont les plus rentables."))
    nb_photos = len(_liste_json(etab.get("photos")))
    if nb_photos < 3:
        score -= 15
        # Le libellé et la preuve ne doivent pas dire deux fois la même chose : dans
        # le mail, ils s'affichent l'un derrière l'autre. La preuve commence par le
        # compte, c'est elle que la rédaction reprend telle quelle.
        manques.append(_defaut(
            "fiche_peu_de_photos",
            "Fiche Google sans aucune photo" if nb_photos == 0
            else "Fiche Google presque sans photo", 15,
            _pluriel(nb_photos, "photo", "photos") + " sur la fiche Google",
            "Les fiches avec plus de dix photos reçoivent nettement plus d'appels "
            "et d'itinéraires que les fiches quasi vides."))
    avis = _entier(etab.get("nb_avis"))
    if avis < 10:
        score -= 12
        manques.append(_defaut(
            "fiche_peu_d_avis",
            "Aucun avis client" if avis == 0 else "Trop peu d'avis clients", 12,
            _pluriel(avis, "avis", "avis") + " sur la fiche Google",
            "Peu d'avis : Google vous fait remonter moins haut, et le "
            "client hésite avant de pousser la porte."))
    note = _decimal(etab.get("note"))
    if note is not None and note < 4.0:
        score -= 10
        manques.append(_defaut("fiche_note_basse", f"Note de {note}/5", 10, f"{note}/5",
                               "Sous 4,0, une part importante des clients écarte "
                               "l'établissement sans même lire les avis."))
    if not (etab.get("resume") or "").strip():
        score -= 5
    # Même contrat que pour le site : pas de preuve, pas de reproche.
    manques = [m for m in manques if (m.get("preuve") or "").strip()]
    return max(0, score), manques
