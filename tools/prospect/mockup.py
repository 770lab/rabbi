"""Génération de la maquette « on s'est permis de refaire votre site ».

Une page unique, autonome, rapide, mobile d'abord, avec les données réelles de la
fiche Google : c'est la preuve qui remplace tout l'argumentaire commercial.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import html
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config as config_mod
from . import places

BASE = Path(__file__).resolve().parent
GABARIT = Path(__file__).parent / "templates" / "mockup" / "index.html"
SORTIE = BASE / "out" / "maquettes"

# Une teinte par famille de métier : la maquette ne doit pas avoir l'air d'un gabarit.
TEINTES = {
    "restaurant": "b4472f", "food": "b4472f", "bakery": "a9762d", "cafe": "8a5a3b",
    "bar": "6b3f5e", "hair_care": "2f6b6b", "beauty_salon": "9b4a6b",
    "plumber": "2f5ea8", "electrician": "b8862b", "car_repair": "44506b",
    "dentist": "2f7f8f", "doctor": "2f7f8f", "real_estate_agency": "3d6b45",
    "lawyer": "3a3f5c", "gym": "39603f", "store": "6b5030",
}

ACCROCHES = {
    "restaurant": "Une cuisine {qualificatif}, servie {lieu}. Réservez votre table en un appel.",
    "bakery": "Pains, viennoiseries et pâtisseries préparés chaque matin {lieu}.",
    "cafe": "Un café de quartier {lieu}, du matin au soir.",
    "bar": "Le rendez-vous du quartier {lieu} : bonne ambiance, bonnes adresses.",
    "hair_care": "Coupe, couleur et conseils sur mesure {lieu}. Prenez rendez-vous en un appel.",
    "beauty_salon": "Soins et beauté {lieu}, dans un cadre pensé pour souffler.",
    "plumber": "Dépannage, installation et rénovation {lieu}. Devis clair, intervention rapide.",
    "electrician": "Installation, mise aux normes et dépannage électrique {lieu}.",
    "car_repair": "Entretien, réparation et diagnostic {lieu}. Devis avant travaux.",
    "dentist": "Cabinet dentaire {lieu} : soins, prévention et urgences.",
    "_defaut": "{categorie} {lieu}. Une équipe joignable, des horaires clairs, un contact direct.",
}

QUALIFICATIFS = ["généreuse", "de saison", "faite maison", "sincère", "soignée"]

# Alertes déjà écrites : un lot fait vingt maquettes, un défaut de configuration y
# est vingt fois le même. On le dit une fois — mais on le dit.
_ALERTES_VUES: set[str] = set()


def _alerte(message: str) -> None:
    """Signale un défaut sur la sortie d'erreur, une seule fois par exécution."""
    if message in _ALERTES_VUES:
        return
    _ALERTES_VUES.add(message)
    print("    ! " + message, file=sys.stderr)


def creneau(texte: str) -> str:
    """Nom de dossier propre à partir du nom de l'établissement.

    Ne suffit PAS à identifier un établissement : passer par `slug_pour`.
    """
    t = unicodedata.normalize("NFKD", texte or "").encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-zA-Z0-9]+", "-", t).strip("-").lower()
    return t[:48] or "etablissement"


def slug_pour(etab: dict) -> str:
    """Identifiant unique d'un établissement : dossier de maquette, URL, nom de .eml.

    Le nom seul ne suffit pas — sur une zone dense comme Paris 19e, deux « Le Petit
    Bouchon » partageraient un dossier (la seconde maquette écrasant la première) et
    un .eml écraserait l'autre. On suffixe donc le nom lisible par une empreinte
    stable du `place_id`, qui est la clé primaire de la base.

    On hache le place_id plutôt que d'en couper les derniers caractères : les
    identifiants Google sont sensibles à la casse et se ressemblent souvent par la
    fin, deux propriétés qu'une simple troncature perd.
    """
    base = creneau(etab.get("nom", ""))
    pid = (etab.get("place_id") or "").strip()
    if not pid:
        return base
    empreinte = hashlib.sha1(pid.encode("utf-8")).hexdigest()[:8]
    return f"{base}-{empreinte}"


def _teinte(types: list[str]) -> str:
    for t in types:
        if t in TEINTES:
            return TEINTES[t]
    return "8a5a3b"


# Demi-étoile dessinée en CSS (.etoiles .demi) : aucun glyphe exotique à installer.
_DEMI = '<span class="demi">☆</span>'


def _etoiles(note) -> str:
    """Étoiles fidèles à la note : partie entière + demie, jamais d'arrondi vers le haut.

    Arrondir au plus proche affichait cinq étoiles pleines pour une fiche notée 4,6 —
    une note parfaite prêtée à un vrai commerce sur une page que nous hébergeons — et
    l'arrondi bancaire de Python rendait 3,5 et 4,5 à l'identique.
    """
    try:
        n = float(note)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    n = min(n, 5.0)
    pleines = int(n)
    demie = 1 if (pleines < 5 and n - pleines >= 0.25) else 0
    return "★" * pleines + _DEMI * demie + "☆" * (5 - pleines - demie)


def _accroche(etab: dict, types: list[str]) -> str:
    lieu = f"à {etab['ville']}" if etab.get("ville") else "dans le quartier"
    for t in types:
        if t in ACCROCHES:
            return ACCROCHES[t].format(
                qualificatif=QUALIFICATIFS[len(etab.get("nom", "")) % len(QUALIFICATIFS)],
                lieu=lieu,
                categorie=etab.get("categorie") or "Notre établissement",
            )
    return ACCROCHES["_defaut"].format(
        lieu=lieu, categorie=etab.get("categorie") or "Notre établissement",
        qualificatif="soignée")


def _presentation(etab: dict) -> str:
    if (etab.get("resume") or "").strip():
        base = etab["resume"].strip()
    else:
        base = (f"{etab['nom']} accueille ses clients "
                f"{'à ' + etab['ville'] if etab.get('ville') else 'dans le quartier'}.")
    avis, note = etab.get("nb_avis") or 0, etab.get("note")
    if avis and note:
        base += (f" {avis} personnes ont laissé un avis sur Google, pour une note "
                 f"moyenne de {note}/5.")
    return base


def _telecharger_photos(photos: list, cle: str, dossier: Path,
                        maxi: int = 6) -> tuple[list[dict], list[str]]:
    """Rapatrie les photos de la fiche : la clé API ne doit jamais finir dans le HTML.

    Renvoie `(locales, echecs)`, où chaque entrée de `locales` porte son fichier ET
    son crédit d'auteur : `{"fichier": "photo-1.jpg", "credit": "Photo : … via Google"}`.
    Le crédit vient des métadonnées de la fiche (`places.credit_photo`), pas du
    fichier : une photo déjà en cache garde donc son attribution. La licence de la
    Places API impose d'afficher l'auteur partout où l'image est montrée, et ces
    photos sont pour l'essentiel des contributions de clients que nous réhébergeons
    sur notre propre domaine.

    Deux garde-fous conservés :
    - une photo déjà sur le disque n'est PAS retéléchargée. Place Photo est facturé à
      la requête et on régénère les mêmes 20 maquettes à chaque mise au point ;
    - un échec (403, quota, référence périmée, réseau) est collecté et remonté. Avant,
      il était avalé et la maquette basculait en silence sur le dégradé de repli.
    """
    dossier.mkdir(parents=True, exist_ok=True)
    locales, echecs = [], []
    for i, photo in enumerate(photos[:maxi]):
        # Le nom du fichier porte l'empreinte de la photo, pas seulement son rang :
        # la liste de la fiche change d'ordre ou perd une entrée entre deux passages,
        # et un cache nommé par position aurait servi l'image d'un autre auteur sous
        # le crédit du rang. Un décalage d'attribution est une faute de licence.
        reference = photo.get("nom", "") if isinstance(photo, dict) else str(photo)
        empreinte = hashlib.sha1(reference.encode("utf-8")).hexdigest()[:8]
        cible = dossier / f"photo-{i+1}-{empreinte}.jpg"
        entree = {"fichier": cible.name, "credit": places.credit_photo(photo),
                  "auteurs": places.auteurs_photo(photo)}
        if cible.exists() and cible.stat().st_size > 0:
            locales.append(entree)  # déjà payée lors d'une exécution précédente
            continue
        try:
            req = urllib.request.Request(
                places.photo_url(photo, cle, 1600),
                headers={"User-Agent": "Mozilla/5.0 (maquette)"},
            )
            with urllib.request.urlopen(req, timeout=25) as r:
                donnees = r.read()
            if not donnees:
                echecs.append(f"photo-{i+1} : réponse vide")
                continue
            cible.write_bytes(donnees)
            locales.append(entree)
        except urllib.error.HTTPError as e:
            echecs.append(f"photo-{i+1} : HTTP {e.code}")
        except Exception as e:  # réseau, TLS, timeout…
            echecs.append(f"photo-{i+1} : {type(e).__name__}")
    return locales, echecs


def _credit_html(photo: dict) -> str:
    """Crédit d'une photo, nom d'auteur cliquable quand Google donne son profil.

    La licence Places demande l'attribution telle qu'elle est fournie, lien compris.
    Sans lien exploitable, on retombe sur le texte de `places.credit_photo`.
    """
    auteurs = photo.get("auteurs") or []
    if not auteurs:
        return html.escape(photo.get("credit") or "Photo via Google")
    morceaux = []
    for a in auteurs:
        nom = html.escape(a.get("nom", ""))
        uri = (a.get("uri") or "").strip()
        if uri.lower().startswith(("https://", "http://")):
            morceaux.append(f'<a href="{html.escape(uri, quote=True)}" '
                            'rel="nofollow noopener" target="_blank">' + nom + "</a>")
        else:
            morceaux.append(nom)
    return "Photo : " + ", ".join(morceaux) + " via Google"


def _json_pour_script(valeur) -> str:
    """Sérialise en JSON sûr à l'intérieur d'un <script>.

    json.dumps n'échappe ni `<` ni `>` : un nom Google contenant « </script> » fermait
    l'élément et le reste de la fiche était rendu comme du HTML exécutable, sur notre
    domaine. Les trois échappements ci-dessous sont du JSON valide et restent
    invisibles pour les consommateurs (JSON.parse, moteurs de recherche).
    """
    return (json.dumps(valeur, ensure_ascii=False, indent=2)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def _lien_maps(etab: dict) -> str:
    """Lien Maps prêt pour un attribut href, et seulement si le schéma est sûr.

    `maps_url` était la seule interpolation issue de Google à ne pas être échappée : un
    guillemet suffisait à sortir de l'attribut. On refuse en plus tout ce qui n'est pas
    https:// (un `javascript:` transitait sans contrôle depuis SQLite) en retombant sur
    une recherche Maps reconstruite à partir du nom et de l'adresse.
    """
    brut = (etab.get("maps_url") or "").strip()
    if brut.lower().startswith("https://"):
        return html.escape(brut)
    requete = " ".join(x for x in (etab.get("nom"), etab.get("adresse")) if x).strip()
    if requete:
        return html.escape("https://www.google.com/maps/search/?api=1&query="
                           + urllib.parse.quote(requete))
    return "#"


def _lien_rdv(cfg: dict) -> str:
    """Destination du bouton « Choisir un créneau » — même prudence que pour Maps.

    La configuration livrée contient des « A_REMPLIR » : tel quel, le href devenait un
    lien relatif mort sur une page publiée au nom du commerçant. On n'accepte donc que
    http(s):// et mailto:, en repli sur l'adresse puis sur le site du studio.
    """
    ident = cfg.get("identite", {})
    email = (ident.get("email") or "").strip()
    for candidat in ((cfg.get("rdv", {}).get("lien") or "").strip(),
                     f"mailto:{email}" if email and "@" in email else "",
                     (ident.get("site") or "").strip()):
        bas = candidat.lower()
        if (bas.startswith(("https://", "http://", "mailto:"))
                and not any(j in bas for j in config_mod.JETONS_GABARIT)):
            return html.escape(candidat)
    return "#"


def _poser_robots(racine: Path, base_url: str = "") -> None:
    """Dépose un robots.txt à la racine du dossier de maquettes — qui AUTORISE le crawl.

    La protection qui fait foi est le `<meta name="robots" content="noindex">` de
    chaque page, et un moteur ne peut obéir à un noindex qu'il n'a pas le droit de
    télécharger. L'ancien `Disallow: /` produisait donc l'anti-pattern que Google
    documente : publié sur le sous-domaine dédié que propose la configuration
    d'exemple, ce fichier EST à la racine et empêchait la lecture du noindex — l'URL
    pouvait rester listée « sans description disponible » ; publié sous un chemin
    (770lab.com/maquettes/), il n'était de toute façon jamais lu.

    Les deux cas de figure sont couverts par le même fichier permissif : juste à la
    racine d'un domaine, inoffensif sous un chemin. Quand `maquettes.base_url` révèle
    une publication sous chemin, le fichier le dit et renvoie vers la seule mesure qui
    s'y applique — le noindex par page, doublé si besoin d'un en-tête
    `X-Robots-Tag: noindex` côté serveur.
    """
    chemin = urllib.parse.urlsplit(base_url or "").path.strip("/")
    lignes = [
        "# Maquettes de démonstration : jamais indexées, jamais présentées",
        "# comme le site officiel d'un commerce.",
        "#",
        "# Le crawl est autorisé EXPRÈS. L'exclusion est portée par",
        '#     <meta name="robots" content="noindex, nofollow">',
        "# dans chaque page ; un « Disallow: / » ici empêcherait les moteurs de lire",
        "# ce noindex, et les URL resteraient listées sans description.",
    ]
    if chemin:
        lignes += [
            "#",
            f"# Publication prévue sous /{chemin}/ : à cet emplacement, ce fichier n'est",
            "# PAS lu (un robots.txt ne vaut qu'à la racine d'un domaine). Ne rien",
            "# ajouter au robots.txt du domaine : le noindex de chaque page suffit et",
            "# doit rester. Pour une couche de plus, servir l'en-tête",
            "#     X-Robots-Tag: noindex",
        ]
    contenu = "\n".join(lignes) + "\nUser-agent: *\nAllow: /\n"
    cible = racine / "robots.txt"
    if not cible.exists() or cible.read_text(encoding="utf-8") != contenu:
        racine.mkdir(parents=True, exist_ok=True)
        cible.write_text(contenu, encoding="utf-8")


def _numero_appelable(tel: str) -> tuple[str, str]:
    r"""Numéro réellement composable pour un href `tel:` — ou rien du tout.

    Renvoie `(composable, lisible)` : le second sert de libellé au bouton, pour que
    la page n'affiche pas non plus les caractères parasites du champ.

    Le champ de la fiche n'est pas toujours un numéro nu : « 04 72 00 11 22 (poste 3) »,
    « 01 23 45 67 89 / 06 12 … », un libellé, voire du texte injecté. Garder tous les
    chiffres du champ (l'ancien `re.sub(r"[^\d+]", "", tel)`) soudait ces chiffres
    parasites au numéro, et le bouton d'appel — seul appel à l'action de la page,
    doublé en barre fixe sur mobile — composait un numéro faux au nom du commerce.

    On lit donc la première suite de chiffres ASSEZ LONGUE pour être un numéro (six
    chiffres au moins, sinon le « 5 » de « poste 5 : 04 72 … » l'emporterait), on
    traite les groupes entre parenthèses (« (0) » ne se compose pas, « (800) » si), et
    on ne garde le « + » que s'il est en tête. Sans candidat, on renonce : mieux vaut
    pas de bouton d'appel qu'un bouton qui appelle ailleurs.
    """
    brut = (tel or "").replace("\u00a0", " ").replace("\u202f", " ").strip()

    def _parenthese(m):
        dedans = m.group(1).strip()
        if re.fullmatch(r"\d+", dedans):
            return "" if dedans == "0" else dedans  # +33 (0)4 … -> +334…
        return " "  # (poste 3), (bureau), (sur RDV)…

    brut = re.sub(r"\(([^)]*)\)", _parenthese, brut)
    for m in re.finditer(r"\+?\d[\d .\-]*", brut):  # s'arrête à la 1re lettre, / ou ,
        lisible = re.sub(r"\s{2,}", " ", m.group(0)).strip(" .-")
        chiffres = re.sub(r"\D", "", lisible)
        if len(chiffres) >= 6:
            return ("+" if lisible.startswith("+") else "") + chiffres, lisible
    return "", ""


def _societe(cfg: dict) -> str:
    """Nom sous lequel la maquette est signée — jamais un jeton de gabarit.

    `cfg['identite'].get('societe') or …` ne filtrait que la chaîne vide : trois
    espaces passaient, et surtout « A_REMPLIR » de la configuration livrée, si bien
    que la page partait signée d'un trou. `config.JETONS_GABARIT` sait les
    reconnaître ; on s'en sert ici comme `rediger` s'en sert.

    Une maquette porte le nom d'un commerce : elle doit dire qui l'a faite. À défaut
    d'identité, on retombe sur le domaine du studio (encore identifiable) ; s'il n'y
    en a pas non plus, on renvoie "" et l'appelant le signale. `maquette` reste
    utilisable sans configuration d'expédition, mais plus en silence.
    """
    ident = cfg.get("identite") or {}

    def _propre(valeur: str) -> bool:
        bas = valeur.lower()
        return bool(valeur) and not any(j in bas for j in config_mod.JETONS_GABARIT)

    for cle in ("societe", "nom"):
        valeur = str(ident.get(cle) or "").strip()
        if _propre(valeur):
            return valeur
    for cle in ("site", "email"):
        valeur = str(ident.get(cle) or "").strip()
        if _propre(valeur):
            domaine = re.sub(r"^[a-zA-Z][\w+.-]*://", "", valeur).split("/")[0]
            domaine = domaine.split("@")[-1].strip()
            if "." in domaine:
                return domaine
    return ""


def _fuseau_reel(nom: str) -> bool:
    """Ce nom de fuseau existe-t-il vraiment dans la base tzdata ?

    Un nom inventé passerait jusqu'au navigateur, où `Intl` le rejette sans bruit : la
    page retomberait alors sur l'horloge du VISITEUR, c'est-à-dire précisément ce
    qu'on cherche à éviter. Faute de base tzdata ici, on ne conclut rien (on répond
    oui) : ce contrôle ne doit pas se transformer en refus systématique.
    """
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo("UTC")  # la base est-elle seulement lisible ?
    except Exception:
        return True
    try:
        ZoneInfo(nom)
    except Exception:
        return False
    return True


def _fuseau_plausible(nom: str, lng) -> bool:
    """Ce fuseau peut-il décrire ce point ? Comparaison avec l'heure solaire du lieu.

    Sans coordonnées ni base tzdata, on ne conclut rien et on garde le fuseau : ce
    contrôle sert à écarter une erreur grossière — un fuseau d'un autre continent posé
    sur un commerce français — pas à arbitrer les cas limites, que l'heure solaire ne
    permet de toute façon pas de trancher (en hiver, Lyon lu à l'heure de Jérusalem
    n'est qu'à 1 h 40 de son heure solaire). La marge de 3 h 30 laisse passer les
    écarts légitimes les plus larges du monde — Galice à l'heure de Madrid, Xinjiang à
    l'heure de Pékin — et attrape les continents.
    """
    try:
        longitude = float(lng)
    except (TypeError, ValueError):
        return True
    try:
        from zoneinfo import ZoneInfo

        decalage = _dt.datetime.now(ZoneInfo(nom)).utcoffset()
    except Exception:  # tzdata absente, nom inconnu…
        return True
    if decalage is None:
        return True
    return abs(decalage.total_seconds() / 3600 - longitude / 15) <= 3.5


def _fuseau_commerce(etab: dict, cfg: dict):
    """Fuseau dans lequel « ouvert / fermé » se calcule : celui du COMMERCE.

    On lisait `rdv.fuseau`, qui est le fuseau des RENDEZ-VOUS du studio. Sur une page
    qui porte le nom du commerçant, un studio installé ailleurs faisait afficher
    « Fermé en ce moment » en plein service. On prend donc, dans l'ordre :

    1. le décalage porté par la fiche elle-même (`utc_offset_minutes`, ce que renvoie
       Places API (New)) : transmis tel quel au gabarit, en minutes ;
    2. son fuseau IANA (`fuseau`), si la fiche en porte un ;
    3. `maquettes.fuseau`, le fuseau des zones prospectées, à régler à côté de
       `maquettes.base_url` ;
    4. Europe/Paris, défaut de la campagne.

    Renvoie `None` si le fuseau retenu contredit franchement la position de
    l'établissement : la page affiche alors ses horaires sans rien affirmer.
    """
    offset = etab.get("utc_offset_minutes")
    if isinstance(offset, (int, float)) and not isinstance(offset, bool):
        if -14 * 60 <= offset <= 14 * 60:
            return int(offset)
    for candidat in (etab.get("fuseau"),
                     (cfg.get("maquettes") or {}).get("fuseau"),
                     "Europe/Paris"):
        nom = str(candidat or "").strip()
        if not nom or not re.fullmatch(r"[A-Za-z0-9_+/-]{1,64}", nom):
            continue
        if not _fuseau_reel(nom):
            _alerte(f"fuseau « {nom} » inconnu de la base des fuseaux horaires : "
                    "ignoré (voir maquettes.fuseau).")
            continue
        if _fuseau_plausible(nom, etab.get("lng")):
            return nom
        _alerte(f"fuseau « {nom} » incompatible avec la position de "
                f"{etab.get('nom', '?')} : la maquette montrera ses horaires sans dire "
                "si c'est ouvert.")
        return None
    return None


def _jsonld(etab: dict, cfg: dict, canonical: str, societe: str) -> str:
    """Données structurées de LA MAQUETTE — jamais de l'établissement.

    Avant, ce bloc déclarait un Restaurant/LocalBusiness complet (nom, adresse postale,
    GPS, téléphone, note agrégée) dont l'`url` officielle était… notre page. C'était le
    dossier d'usurpation d'identité prêt à l'emploi : un moteur pouvait indexer la page
    770lab comme le site du commerce. Le seul sujet décrit ici est donc la proposition
    de refonte, et son auteur.
    """
    d = {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "name": f"[Maquette] Proposition de refonte — {etab.get('nom', '')}",
        "description": (
            "Maquette de démonstration réalisée à partir des informations publiques de "
            "la fiche Google. Page non officielle, sans lien avec l'établissement."),
        "inLanguage": "fr",
        "isPartOf": {"@type": "WebSite", "name": f"Maquettes de démonstration — {societe}"},
        "creator": {"@type": "Organization", "name": societe,
                    "url": cfg.get("identite", {}).get("site", "")},
        "creativeWorkStatus": "Draft",
    }
    if canonical:
        d["url"] = canonical  # l'URL de la maquette, qui est bien celle de cette page
    return _json_pour_script(d)


def generer(etab: dict, cfg: dict, cle_api: str | None = None,
            avis: list[dict] | None = None, dossier_sortie: Path | None = None) -> Path:
    """Écrit out/maquettes/<slug>/index.html et renvoie le chemin."""
    types = json.loads(etab.get("types") or "[]")
    slug = slug_pour(etab)
    racine = dossier_sortie or SORTIE
    dossier = racine / slug
    dossier.mkdir(parents=True, exist_ok=True)
    _poser_robots(racine, (cfg.get("maquettes") or {}).get("base_url") or "")

    photos_api = json.loads(etab.get("photos") or "[]")
    photos_locales, echecs = ([], [])
    if cle_api and photos_api:
        photos_locales, echecs = _telecharger_photos(photos_api, cle_api, dossier)
    if echecs:
        # Jamais muet : sans cette ligne, une clé sans Place Photo activée produisait
        # vingt maquettes en aplat de couleur et un « 20 maquette(s) générée(s) » serein.
        print(f"    ! {len(echecs)} photo(s) non récupérée(s) pour "
              f"{etab.get('nom', '?')} — {', '.join(echecs[:3])}", file=sys.stderr)

    teinte = _teinte(types)
    if photos_locales:
        # La photo de tête part en fond CSS : le crédit d'auteur ne peut donc pas
        # vivre dans un <figcaption>, il est posé en clair au bas du bandeau.
        photo_principale = photos_locales[0]["fichier"]
        fond_hero = f"#111 url('{photo_principale}') center/cover no-repeat"
        credit_hero = ('<p class="credit-photo">'
                       + _credit_html(photos_locales[0]) + "</p>")
    else:
        fond_hero = (f"linear-gradient(135deg,#{teinte} 0%,#1b1614 100%)")
        photo_principale = ""
        credit_hero = ""

    horaires = json.loads(etab.get("horaires") or "[]")
    lignes_h = "".join(
        "<li><span>{}</span><span>{}</span></li>".format(
            html.escape(l.split(":", 1)[0]),
            html.escape(l.split(":", 1)[1].strip() if ":" in l else ""))
        for l in horaires
    ) or '<li><span>Horaires à confirmer</span><span>—</span></li>'

    if photos_locales[1:]:
        # Un <figcaption> par photo : la licence Places exige l'auteur sous CHAQUE
        # image réhébergée, et ces images sont pour l'essentiel des photos de clients.
        vignettes = "".join(
            '<figure><span class="cadre"><img src="{f}" alt="{alt}" loading="lazy" '
            'width="800" height="600"></span><figcaption>{c}</figcaption></figure>'.format(
                f=ph["fichier"], alt=html.escape(etab.get("nom", "")),
                c=_credit_html(ph))
            for ph in photos_locales[1:]
        )
        section_photos = (
            '<section style="background:var(--carte)"><div class="enveloppe">'
            '<h2>En images</h2><div class="galerie">' + vignettes + "</div></div></section>"
        )
    else:
        section_photos = ""

    if avis:
        cartes = "".join(
            '<div class="carte"><div class="etoiles">{}</div><blockquote><p>{}</p>'
            '<footer>{}</footer></blockquote></div>'.format(
                _etoiles(a.get("note", 5)),
                html.escape((a.get("texte") or "")[:260]),
                html.escape(a.get("auteur") or "Client Google"))
            for a in avis[:3]
        )
        section_avis = ('<section><div class="enveloppe"><h2>Ils sont passés</h2>'
                        '<div class="grille g3">' + cartes + "</div></div></section>")
    else:
        section_avis = ""

    bloc_note = ""
    if etab.get("note"):
        bloc_note = ('<div class="note"><span class="etoiles">{}</span>'
                     '<span>{}/5 · {} avis Google</span></div>').format(
            _etoiles(etab["note"]), str(etab["note"]).replace(".", ","),
            etab.get("nb_avis") or 0)

    tel = etab.get("telephone") or ""
    tel_brut, tel_lisible = _numero_appelable(tel)
    if tel_brut:
        bouton_appel = ('<a class="btn btn-plein" href="tel:' + tel_brut + '">Appeler '
                        + html.escape(tel_lisible) + "</a>")
        barre_tel = ('<a class="barre-tel" href="tel:' + tel_brut
                     + '">Appeler maintenant</a>')
    else:
        # Pas de numéro composable : pas de bouton d'appel. Un href="tel:" vide, ou
        # bâti sur des chiffres parasites, est pire que son absence.
        bouton_appel, barre_tel = "", ""

    canonical = (cfg.get("maquettes", {}).get("base_url") or "").rstrip("/")
    canonical = f"{canonical}/{slug}/" if canonical else ""
    # Open Graph impose une URL ABSOLUE : un "photo-1.jpg" relatif (ou vide) laissait
    # l'aperçu WhatsApp / Slack sans vignette, sur le support même où la maquette se
    # partage. Sans base_url ou sans photo, on n'écrit pas la balise du tout.
    if canonical and photo_principale:
        meta_og_image = ('<meta property="og:image" content="'
                         + html.escape(canonical + photo_principale) + '">')
    else:
        meta_og_image = ("<!-- og:image omise : Open Graph exige une URL absolue, et "
                         "il manque une photo locale ou maquettes.base_url. -->")

    societe = _societe(cfg)
    if not societe:
        _alerte("identite.societe / identite.nom absents ou restés en gabarit : les "
                "maquettes seront publiées sans auteur identifiable. Complétez "
                f"identite dans {cfg.get('_chemin') or 'prospect.config.json'}.")
        societe = "un studio indépendant"
    lien_rdv = _lien_rdv(cfg)
    # Ouvert/fermé se calcule dans le fuseau du COMMERCE, pas dans celui du visiteur
    # — ni dans celui des rendez-vous du studio (voir _fuseau_commerce).
    fuseau = _fuseau_commerce(etab, cfg)

    remplacements = {
        "NOM": html.escape(etab.get("nom", "")),
        "CATEGORIE": html.escape(etab.get("categorie") or "Établissement"),
        "VILLE": html.escape(etab.get("ville") or ""),
        "ADRESSE": html.escape(etab.get("adresse") or ""),
        "TEL": html.escape(tel_lisible or tel or "—"),
        "BOUTON_APPEL": bouton_appel,
        "BARRE_TEL": barre_tel,
        "MAPS_URL": _lien_maps(etab),
        "META_DESCRIPTION": html.escape(
            f"{etab.get('nom','')} — {etab.get('categorie','')} "
            f"{'à ' + etab['ville'] if etab.get('ville') else ''}. "
            f"Horaires, adresse, téléphone et itinéraire."),
        "META_OG_IMAGE": meta_og_image,
        "TEINTE_HEX": teinte,
        "INITIALE": html.escape((etab.get("nom") or "?")[:1].upper()),
        "FOND_HERO": fond_hero,
        "CREDIT_HERO": credit_hero,
        "BLOC_NOTE": bloc_note,
        "ACCROCHE": html.escape(_accroche(etab, types)),
        "TITRE_PRESENTATION": "La maison",
        "PRESENTATION": html.escape(_presentation(etab)),
        "HORAIRES": lignes_h,
        "HORAIRES_JSON": _json_pour_script(horaires),
        "FUSEAU_JSON": _json_pour_script(fuseau),
        "SECTION_PHOTOS": section_photos,
        "SECTION_AVIS": section_avis,
        "TITRE_FINAL": "Une question, une table, un devis ?",
        "TEXTE_FINAL": "Un appel suffit. Nous répondons pendant les heures d'ouverture.",
        "SOCIETE": html.escape(societe),
        "LIEN_RDV": lien_rdv,  # déjà échappé et validé par _lien_rdv
        "DATE": _dt.date.today().strftime("%d/%m/%Y"),
        "JSONLD": _jsonld(etab, cfg, canonical, societe),
    }

    # Substitution en UNE passe. Un str.replace par clé relisait le texte déjà
    # substitué : un nom d'établissement contenant « {{JSONLD}} » réinjectait tout le
    # bloc de données structurées — guillemets bruts compris — dans le <title> et
    # cassait l'attribut content= de la meta description.
    page = GABARIT.read_text(encoding="utf-8")
    inconnus = []

    def _valeur(m):
        cle = m.group(1)
        if cle not in remplacements:
            inconnus.append(cle)
            return ""  # jamais de {{JETON}} visible sur une page publiée
        return str(remplacements[cle])

    page = re.sub(r"\{\{(\w+)\}\}", _valeur, page)
    if inconnus:
        print("    ! jeton(s) du gabarit sans valeur, laissés vides : "
              + ", ".join(sorted(set(inconnus))), file=sys.stderr)

    cible = dossier / "index.html"
    cible.write_text(page, encoding="utf-8")
    return cible
