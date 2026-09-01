"""Rédaction des emails de prospection.

Principe : aucune phrase générique. Chaque mail s'appuie sur un fait vérifié
(note Google, nombre d'avis, défaut mesuré par l'audit) et se termine par une
seule action possible : choisir un créneau.

Règle de tenue, valable pour tous les gabarits : on n'écrit que ce que la chaîne
a réellement mesuré. Ce qui n'a pas été vu — une page qui n'a jamais répondu, des
photos qui ne sont pas dans la maquette, un manque que l'audit n'a pas relevé —
ne s'affirme pas. Le destinataire vérifie en un clic, et un seul faux constat
disqualifie tout le reste du message.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import textwrap
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LARGEUR = 78

# Au-dessus de ces deux seuils seulement, la note devient un compliment. En
# dessous de 4,0 l'audit la compte comme un défaut (`fiche_note_basse`) : on ne
# félicite pas quelqu'un pour le chiffre qu'on vient de lui compter en moins.
NOTE_ELOGIEUSE = 4.3
AVIS_ELOGIEUX = 20
NOTE_MENTIONNABLE = 4.0

# Sans ces champs, l'expéditeur n'est pas identifiable et le rendez-vous n'est
# pas prenable : aucun message ne part. Contrôlé à chaque rédaction, pas
# seulement au chargement de la configuration.
CHAMPS_EXPEDITION = (
    ("identite", "nom"),
    ("identite", "societe"),
    ("identite", "email"),
    ("rdv", "lien"),
)

# Facultatifs — on a le droit de les laisser vides — mais recopiés tels quels dans
# le corps du message : le site en signature, le prix en relance 2. Un gabarit
# oublié là part chez le commerçant aussi sûrement qu'ailleurs. Vide veut dire
# « je n'en parle pas » ; « A_REMPLIR » veut dire « j'ai oublié », et ça se refuse.
CHAMPS_SANS_GABARIT = (
    ("identite", "site"),
    ("identite", "telephone"),
    ("identite", "adresse_postale"),
    ("offre", "prix"),
)


class ConfigExpeditionIncomplete(RuntimeError):
    """Configuration d'expédition trouée : on ne rédige rien du tout."""

    def __init__(self, manquants: list[str]):
        self.manquants = list(manquants)
        super().__init__(
            "Configuration d'expédition incomplète : aucun message ne peut être "
            "rédigé. Champs vides ou restés à « A_REMPLIR » : "
            + ", ".join(self.manquants)
            + ". Complétez tools/prospect/prospect.config.json."
        )


class VerdictNonRedigeable(ValueError):
    """Verdict d'audit auquel aucun gabarit ne correspond — dont `non_auditable`."""


def _rempli(valeur) -> bool:
    """« A_REMPLIR » est un trou, pas une valeur : il ne doit jamais partir en mail."""
    v = str(valeur or "").strip()
    return bool(v) and "a_remplir" not in v.lower()


def verifier_expedition(cfg: dict) -> None:
    """Lève `ConfigExpeditionIncomplete` tant que l'expéditeur n'est pas complet.

    Appelée par tous les gabarits : c'est la garantie qu'aucun brouillon sans
    identification de l'expéditeur ni lien de rendez-vous ne peut être produit,
    quel que soit le chemin d'appel.
    """
    manquants = [f"{section}.{cle}" for section, cle in CHAMPS_EXPEDITION
                 if not _rempli((cfg.get(section) or {}).get(cle))]
    # Les facultatifs ne sont exigés que s'ils sont renseignés : on ne réclame pas
    # un prix à qui n'en affiche pas, mais on refuse un « A_REMPLIR » en toutes
    # lettres dans la signature ou dans la phrase de prix.
    manquants += [f"{section}.{cle}" for section, cle in CHAMPS_SANS_GABARIT
                  if str((cfg.get(section) or {}).get(cle) or "").strip()
                  and not _rempli((cfg.get(section) or {}).get(cle))]
    if manquants:
        raise ConfigExpeditionIncomplete(manquants)


# --- Objets, en variantes A/B/C ------------------------------------------------
OBJETS_A = [
    "{nom} : vos {avis} avis Google ne mènent nulle part",
    "Je me suis permis de faire un site pour {nom}",
    "{nom}, {ville} — il vous manque une adresse à donner",
]
OBJETS_B = [
    "{nom} : votre site s'affiche mal sur téléphone",
    "Un détail sur le site de {nom} qui vous coûte des appels",
    "J'ai refait la page d'accueil de {nom} — 2 minutes pour voir ?",
]
# Objets du gabarit « injoignable » : ils énoncent l'erreur constatée, jamais un
# jugement sur une page que personne n'a pu ouvrir.
OBJETS_I = [
    "{nom} : le lien de votre fiche Google ne mène nulle part",
    "{nom} — votre site ne répond plus",
]
# Objets de relance 1 valables dans tous les cas : ils ne promettent rien que le
# corps du message ne porte.
OBJETS_R1 = [
    "Re : {objet_initial}",
    "{nom} — une question, et vous répondez en un mot",
]
# Objet réservé aux relances qui portent réellement un lien de maquette. Un mail
# `absent` n'a jamais parlé de maquette, et une maquette dépubliée n'est plus en
# ligne : dans les deux cas, cet objet inventerait une pièce que le corps ne
# montre pas.
OBJETS_R1_MAQUETTE = [
    "{nom} — la maquette est toujours en ligne",
]
OBJETS_R2 = [
    "Je referme le dossier {nom} ?",
]


def _variante(liste: list[str], place_id: str) -> tuple[str, str]:
    """Choix stable et réparti : le même prospect garde toujours la même variante."""
    h = int(hashlib.sha256(place_id.encode()).hexdigest()[:8], 16)
    i = h % len(liste)
    return liste[i], "ABC"[i] if i < 3 else str(i)


# « Épicerie Julien », « Institut Beauté », « Hôtel du Nord »… : devant une voyelle
# l'élision est obligatoire. Le h est rangé ici avec les voyelles (h muet) : les h
# aspirés sont assez rares en enseigne pour ne pas justifier un dictionnaire.
VOYELLES = "aàâäeéèêëiîïoôöuùûüyh"


def de(nom: str) -> str:
    """« Le Comptoir » → « du Comptoir ». Un mail qui écorche le nom est jeté."""
    n = (nom or "").strip()
    bas = n.lower()
    if not n:
        return "de cet établissement"  # repli sûr : jamais « la fiche Google de . »
    if bas.startswith(("les ", "aux ")):
        return "des " + n[4:]
    if bas.startswith(("le ", "au ")):
        return "du " + n[3:]
    if bas.startswith("la "):
        return "de la " + n[3:]
    if bas.startswith(("l'", "l\u2019")):
        return "de " + n
    if bas[0] in VOYELLES:
        return "d'" + n
    return "de " + n


def _plier(texte: str) -> str:
    sorties = []
    for para in texte.strip().split("\n"):
        if not para.strip():
            sorties.append("")
        else:
            sorties.append(textwrap.fill(para.strip(), LARGEUR))
    return "\n".join(sorties)


def _signature(cfg: dict) -> str:
    i = cfg["identite"]
    lignes = [i.get("nom", ""), i.get("societe", "")]
    contact = " · ".join(x for x in (i.get("telephone"), i.get("site")) if x)
    if contact:
        lignes.append(contact)
    return "\n".join(l for l in lignes if l)


def _mentions(cfg: dict, contact_generique: bool | None = None) -> str:
    """Identification de l'expéditeur + droit d'opposition : obligatoires partout.

    `contact_generique` dit ce qu'on sait de l'adresse retenue : générique
    (contact@, info@…), nominative, ou inconnue. On n'affirme « adresse de contact
    publique de l'établissement » que dans le premier cas — une adresse nominative
    est une donnée personnelle, et le prétendre publique serait faux.
    """
    verifier_expedition(cfg)
    i = cfg["identite"]
    if contact_generique is True:
        origine = ("Ce message professionnel est adressé à l'adresse de contact "
                   "générique publiée par l'établissement, au titre de la prospection "
                   "entre professionnels.")
    elif contact_generique is False:
        origine = ("Ce message professionnel est adressé à une adresse relevée sur "
                   "votre site, au titre de la prospection entre professionnels. Si "
                   "cette adresse est personnelle, dites-le moi : je la retire.")
    else:
        origine = ("Ce message professionnel est adressé à l'adresse de contact que "
                   "j'ai relevée sur vos pages publiques, au titre de la prospection "
                   "entre professionnels.")
    # L'identification de l'expéditeur et le droit d'opposition ne sont pas
    # négociables ; l'adresse postale, elle, ne s'écrit que si elle est renseignée.
    # À défaut, l'adresse e-mail d'expédition tient lieu de coordonnée de contact.
    postale = str(i.get("adresse_postale") or "").strip()
    identite = f"{i['societe']} ({i['nom']}), " + (f"{postale}. " if postale
                                                   else f"{i['email']}. ")
    return _plier(
        f"{origine} {identite}"
        f"Répondez « STOP » et je supprime définitivement votre adresse de mes "
        f"fichiers, sans autre message."
    )


def _phrase_note(etab: dict) -> str:
    """En français, on écrit 4,6/5 — pas 4.6/5. Le détail se remarque.

    Sous 4,0, l'audit compte la note comme un défaut (`fiche_note_basse`) : on ne
    la sort pas du tout, plutôt que de l'exhiber au client comme un atout.
    """
    note, avis = etab.get("note"), etab.get("nb_avis") or 0
    if not note or note < NOTE_MENTIONNABLE:
        return ""
    fr = str(note).replace(".", ",")
    if avis >= 20:
        return f"{fr}/5 sur {avis} avis Google"
    if avis:
        return f"{fr}/5 sur Google"
    return ""


def _note_flatteuse(etab: dict) -> bool:
    """Le compliment (« une réputation solide ») demande une note qui le mérite."""
    note, avis = etab.get("note") or 0, etab.get("nb_avis") or 0
    return note >= NOTE_ELOGIEUSE and avis >= AVIS_ELOGIEUX


def _accroche_note(etab: dict, compliment: str) -> str:
    """« . 4,6/5 sur 428 avis Google, <compliment>. » — ou le fait seul, ou rien."""
    note = _phrase_note(etab)
    if not note:
        return ". "
    if _note_flatteuse(etab):
        return f". {note}, {compliment}. "
    return f". {note}. "


def _defauts_prouves(defauts: list[dict]) -> list[dict]:
    """Dernier filtre avant le commerçant : pas de preuve, pas de constat écrit."""
    return [d for d in defauts or [] if (d.get("preuve") or "").strip()]


def _bloc_defauts(defauts: list[dict], maxi: int = 3) -> str:
    """Les constats, en puces, avec l'argument métier plutôt que le jargon."""
    lignes = []
    for d in _defauts_prouves(defauts)[:maxi]:
        arg = d.get("argument") or d.get("libelle")
        lignes.append(_plier(f"— {d['libelle']} ({d['preuve']}). {arg}"))
    return "\n\n".join(lignes)


def _accord_points(n: int) -> str:
    """« les trois points » quand il n'y en a qu'un trahit le mail automatique."""
    if n <= 1:
        return "le point qui vous coûte"
    if n == 2:
        return "les deux points qui vous coûtent"
    return "les trois points qui vous coûtent"


def _enumerer(morceaux: list[str]) -> str:
    """« a, b et c » — une énumération française, sans virgule avant le « et »."""
    morceaux = [m for m in morceaux if m]
    if len(morceaux) <= 1:
        return morceaux[0] if morceaux else ""
    return ", ".join(morceaux[:-1]) + " et " + morceaux[-1]


def _a_horaires(etab: dict) -> bool:
    """La maquette ne calcule « ouvert / fermé » que si la fiche a des horaires."""
    try:
        return bool(json.loads(etab.get("horaires") or "[]"))
    except (ValueError, TypeError):
        return False


def _prix(cfg: dict) -> str:
    """Le prix ne sort qu'au dernier message : en premier contact il fait fuir,
    en dernier il trie ceux qui hésitaient encore."""
    o = cfg.get("offre") or {}
    if not (o.get("prix") or "").strip():
        return ""
    phrase = f"Pour situer, sans que vous ayez à demander : {o['prix']}"
    if (o.get("delai") or "").strip():
        phrase += f", livré en {o['delai']}"
    phrase += "."
    if (o.get("garantie") or "").strip():
        phrase += f" {o['garantie']}"
    return _plier(phrase)


def _lien_rdv(cfg: dict, creneaux: list[str] | None, avec_maquette: bool = True,
              tour: str | None = None) -> str:
    """Sans maquette en ligne, on ne propose pas d'en « faire le tour » au téléphone.

    `tour` permet au gabarit A de dire « je vous montre le site » : c'est le seul
    endroit où il peut le voir, puisque le mail ne porte plus le lien.
    """
    lien = cfg["rdv"].get("lien", "")
    tour = tour or ("je vous fais le tour de la maquette au téléphone" if avec_maquette else
                    "je vous montre ce que ça donnerait")
    if creneaux:
        liste = "\n".join(f"  · {c}" for c in creneaux[:3])
        base = _plier(
            "Si ça vous parle, je vous montre tout au téléphone. "
            "Voici mes créneaux libres :") + f"\n\n{liste}\n"
        if lien:
            base += f"\nUn autre moment vous arrange mieux ? Tout mon agenda est ici :\n{lien}"
        else:
            base += "\nDites-moi simplement lequel vous convient."
        return base
    if lien:
        return _plier(
            f"Si ça vous parle, prenez un créneau dans mon agenda, au moment qui "
            f"vous arrange — {tour} :") + f"\n{lien}"
    return _plier("Si ça vous parle, répondez-moi avec deux ou trois moments qui vous "
                  "arrangent cette semaine, et je vous appelle.")


def _phrase_score(score, defauts: list[dict]) -> str:
    """Le score est un relevé maison, pas la grille de classement de Google.

    Il additionne des pénalités décidées ici (favicon, robots.txt, Open Graph…) :
    le présenter comme « les critères que Google utilise », et surtout comme ce qui
    décide du classement face au voisin, c'est promettre un résultat de
    référencement que personne n'a mesuré.
    """
    if not isinstance(score, int):
        return ""
    codes = {d.get("code") for d in defauts or []}
    documentes = []
    if "pas_https" in codes:
        documentes.append("le HTTPS")
    if codes & {"pas_responsive", "responsive_partiel"}:
        documentes.append("l'affichage mobile")
    rappel = ""
    if documentes:
        rappel = (f" Parmi eux, {_enumerer(documentes)} : Google documente ces deux "
                  f"points-là comme des signaux de classement." if len(documentes) == 2
                  else f" Parmi eux, {_enumerer(documentes)} : Google documente ce "
                       f"point-là comme un signal de classement.")
    if score >= 25:
        return _plier(
            f"Au total, votre page ressort à {score}/100 sur mon relevé — ma grille de "
            f"contrôle technique, pas une note officielle de Google.{rappel} Le reste se "
            f"voit surtout du côté du client, et c'est déjà beaucoup."
        )
    # Sous 25, le chiffre humilie au lieu de convaincre : on dit la même chose
    # sans le brandir.
    return _plier(
        f"Sur ma grille de contrôle technique, votre page manque presque tous les points "
        f"que je vérifie.{rappel} C'est aussi une bonne nouvelle — il n'y a que du terrain "
        f"à gagner."
    )


def _fait_injoignable(audit: dict) -> tuple[str, str]:
    """L'adresse testée et la raison de l'échec, telles que l'audit les a écrites."""
    d = next((x for x in (audit.get("defauts") or []) if x.get("code") == "site_hs"), None)
    preuve = ((d or {}).get("preuve") or "").strip()
    if "\u2192" in preuve:
        url, raison = (p.strip() for p in preuve.split("\u2192", 1))
    else:
        url, raison = preuve, ""
    return url or (audit.get("url_finale") or ""), raison


def _raison_lisible(raison: str) -> str:
    """« réponse HTTP 404 » → « renvoie une erreur 404 (page introuvable) ».

    L'audit n'envoie ici que les échecs qu'on peut écrire au commerçant (404, 410,
    451, domaine qui ne résout plus). Tout le reste part en `non_auditable` et
    n'atteint jamais ce gabarit.
    """
    r = (raison or "").strip()
    glose = {"404": " (page introuvable)", "410": " (page supprimée)",
             "451": " (page rendue indisponible)"}
    code = re.search(r"\b(\d{3})\b", r)
    if code:
        return f"renvoie une erreur {code.group(1)}{glose.get(code.group(1), '')}"
    if "domaine" in r.lower() or "dns" in r.lower():
        return "ne mène nulle part : le nom de domaine n'existe plus"
    return "ne répond pas"


def _fuseau(cfg: dict | None):
    """Le fuseau de `rdv.fuseau`, ou l'heure locale du poste si on ne l'a pas.

    Renvoyer None n'est pas un échec : `astimezone(None)` bascule sur l'heure
    locale, ce qui reste bien plus juste que de laisser l'UTC brut.
    """
    nom = str(((cfg or {}).get("rdv") or {}).get("fuseau") or "").strip()
    if not nom:
        return None
    try:
        return ZoneInfo(nom)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def _date_audit(audit: dict, cfg: dict | None = None) -> str:
    """« le 31/08/2026 » : la date du relevé, jamais celle de l'envoi.

    `audite_le` vient de SQLite (`datetime('now')`), c'est-à-dire de l'UTC. Le
    formater tel quel date de la veille tout audit lancé entre minuit et 2 h à
    Paris — et c'est justement la seule phrase du gabarit sur laquelle le
    commerçant peut nous répondre « non, c'était réparé ce jour-là ». On le
    ramène donc dans le fuseau du rendez-vous avant d'écrire la date.
    """
    brut = str(audit.get("audite_le") or "").strip()
    for taille, motif in ((19, "%Y-%m-%d %H:%M:%S"), (10, "%Y-%m-%d")):
        try:
            t = _dt.datetime.strptime(brut[:taille], motif)
        except ValueError:
            continue
        if taille == 19:  # une date seule ne porte pas d'heure : rien à convertir
            t = t.replace(tzinfo=_dt.timezone.utc).astimezone(_fuseau(cfg))
        return t.strftime("le %d/%m/%Y")
    return ""


# --- Type A : aucun site --------------------------------------------------------
def _clients_qui_recommandent(avis: int) -> str:
    """« plusieurs centaines » ne s'écrit qu'au-dessus de plusieurs centaines.

    Le commerçant connaît son nombre d'avis par cœur : un ordre de grandeur faux
    dans la première phrase suffit à faire jeter le message.
    """
    if avis >= 200:
        return "plusieurs centaines de clients"
    if avis >= 100:
        return "plus de cent clients"
    if avis >= 20:
        return f"{avis} clients"
    return "des clients"


def _requetes(metier: str, ville: str, restaurant: bool) -> str:
    """Les recherches où il n'apparaît pas — construites sur son métier et sa ville,
    jamais sur un quartier qu'on n'a pas relevé."""
    lignes = [f"« {metier} {ville} »",
              f"« meilleur {metier} {ville} »",
              # « bonne adresse » ne se dit pas d'un plombier : hors restauration,
              # la requête qui compte est celle de la proximité.
              f"« bonne adresse {ville} »" if restaurant else f"« {metier} près de chez moi »"]
    bloc = "\n".join(f"  {x}" for x in lignes)
    if restaurant:
        bloc += "\n\n" + _plier("ou même le type de cuisine qu'elle a envie de manger ce soir.")
    return bloc


def _rdv_decouverte(cfg: dict, creneaux: list[str] | None) -> str:
    """Clôture du gabarit A : le rendez-vous est la seule façon de voir le site."""
    lien = cfg["rdv"].get("lien", "")
    if creneaux:
        liste = "\n".join(f"  · {c}" for c in creneaux[:3])
        base = _plier("Si vous voulez le découvrir, voici mes créneaux :") + f"\n\n{liste}\n"
        base += (f"\nUn autre moment vous arrange mieux ? Tout mon agenda est ici :\n{lien}"
                 if lien else "\nDites-moi simplement lequel vous convient.")
        return base
    if lien:
        return _plier("Si vous voulez le découvrir, choisissez simplement le créneau qui vous "
                      "convient ici :") + f"\n{lien}"
    return _plier("Si vous voulez le découvrir, répondez-moi avec deux ou trois moments "
                  "qui vous arrangent cette semaine, et je vous appelle.")


def mail_sans_site(etab: dict, manques_fiche: list[dict], cfg: dict,
                   url_maquette: str = "", creneaux: list[str] | None = None, *,
                   maquette_photos: bool = False,
                   contact_generique: bool | None = None) -> dict:
    """Fiche Google sans site (ou renvoyant vers un réseau social).

    Le mail annonce un site déjà fait et ne donne pas son adresse : le rendez-vous
    est la seule façon de le voir. L'annonce ne dépend pas de `url_maquette` — le
    site se fabrique entre la prise de rendez-vous et le rendez-vous lui-même.
    """
    verifier_expedition(cfg)
    nom = etab.get("nom", "")
    ville = etab.get("ville") or "votre quartier"
    avis = etab.get("nb_avis") or 0
    metier = (etab.get("categorie") or "").lower() or "votre métier"
    restaurant = "restaurant" in (str(etab.get("categorie", "")) + str(etab.get("types", ""))).lower()
    objet_tpl, variante = _variante(OBJETS_A, etab["place_id"])
    objet = objet_tpl.format(nom=nom, avis=avis, ville=ville)

    codes = {d.get("code") for d in (manques_fiche or [])}
    reseaux = "reseaux_seulement" in codes

    blocs = ["Bonjour,",
             _plier(f"Je suis tombé cette semaine sur la fiche Google {de(nom)}.")]

    # La note ne sort jamais sous 4,0 (`_phrase_note`), et le commentaire qui la
    # salue demande une note qui le mérite : sinon on félicite quelqu'un pour un
    # chiffre que l'audit vient de lui compter en moins.
    note = _phrase_note(etab)
    if note:
        bloc = note.replace(" avis Google", " avis") + "."
        if _note_flatteuse(etab):
            bloc += "\nÀ ce niveau-là, votre réputation fait déjà une bonne partie du travail."
        blocs.append(bloc)

    if reseaux:
        blocs.append(_plier("Par curiosité, j'ai cliqué sur « Site Web » : on arrive sur une "
                            "page de réseau social, pas sur un site à vous."))
    else:
        blocs += ["Par curiosité, j'ai cliqué sur « Site Web ».", "Rien."]

    blocs.append(_plier(
        f"Et je me suis dit que c'était dommage : vous avez "
        f"{_clients_qui_recommandent(avis)} qui recommandent votre établissement, mais aucun "
        f"site pour transformer cette réputation en visibilité supplémentaire."))
    blocs.append(_plier(
        "Parce qu'un bon site ne sert pas seulement aux gens qui vous connaissent déjà."))
    blocs.append(_plier(
        "Bien référencé, il peut vous faire apparaître lorsqu'une personne cherche "
        "simplement :"))
    blocs.append(_requetes(metier, ville, restaurant))
    blocs.append(_plier(
        "Ce sont potentiellement de nouveaux clients qui vous découvrent chaque jour sans "
        "avoir jamais entendu parler de vous auparavant."))

    # Décision commerciale assumée : le site est annoncé fait, que la maquette soit
    # déjà générée ou non. Le clic sur l'agenda déclenche la production, et le délai
    # avant le rendez-vous suffit à la fabriquer — c'est une précommande, pas un
    # constat d'audit. Ne pas « réparer » ce bloc en le reconditionnant sur
    # `url_maquette` : la contrepartie est opérationnelle, pas rédactionnelle — tout
    # rendez-vous pris doit trouver un site prêt le jour dit.
    blocs += [
        _plier("Alors plutôt que de vous envoyer un mail pour vous expliquer ce qu'il "
               "faudrait faire…"),
        "je l'ai fait.",
        _plier(f"J'ai préparé une première version du site {de(nom)}."),
        _plier("Il fonctionne sur mobile, reprend les informations essentielles de votre "
               "établissement et a été pensé pour donner envie de venir chez vous dès les "
               "premières secondes."),
        _plier("Je pourrais simplement vous envoyer le lien, mais je préfère vous le "
               "montrer directement : vous verrez immédiatement ce que j'ai imaginé pour "
               "votre établissement et vous pourrez me dire ce que vous en pensez."),
        "Le site existe déjà. Vous n'avez rien à imaginer.",
    ]

    blocs.append(_rdv_decouverte(cfg, creneaux))
    blocs.append("À bientôt,\n" + _signature(cfg))
    blocs.append("—\n" + _mentions(cfg, contact_generique))

    return {"type": "A_sans_site", "objet": objet,
            "corps": "\n\n".join(x for x in blocs if x), "variante": variante}


# --- Type B : site obsolète -----------------------------------------------------
def mail_site_obsolete(etab: dict, audit: dict, cfg: dict,
                       url_maquette: str = "", creneaux: list[str] | None = None, *,
                       maquette_photos: bool = False,
                       contact_generique: bool | None = None) -> dict:
    """Site atteint et analysé : là, et seulement là, on peut parler de la page."""
    verifier_expedition(cfg)
    nom = etab.get("nom", "")
    tel = (etab.get("telephone") or "").strip()
    score = audit.get("score")
    defauts = _defauts_prouves(audit.get("defauts", []))
    objet_tpl, variante = _variante(OBJETS_B, etab["place_id"])
    objet = objet_tpl.format(nom=nom)

    accroche = (
        "Bonjour,\n\n"
        + _plier(
            f"J'ai regardé {nom} de près cette semaine"
            + _accroche_note(etab, "une réputation solide")
            + "Puis j'ai ouvert votre site avec les yeux d'un client qui vous découvre "
              "sur son téléphone, et l'écart m'a sauté aux yeux. Votre maison mérite "
              "bien mieux que ce que cette page renvoie."
        )
    )

    # Le compteur annonce ce que la liste montre, pas ce que l'audit a rendu : on
    # calcule les puces d'abord, on les compte ensuite. Sans ça, trois défauts
    # dont un seul prouvé annoncent « les trois points » et n'en listent qu'un.
    montres = _defauts_prouves(defauts)[:3]
    constats = _bloc_defauts(montres, 3)
    constat_intro = _plier(
        f"Je ne dis pas ça au hasard, j'ai mesuré. Voici "
        f"{_accord_points(len(montres))} le plus cher aujourd'hui :"
    ) if constats else ""

    chiffre = _phrase_score(score, defauts)

    preuve = ""
    if url_maquette:
        # Rien sur la maquette qui n'y soit : les photos n'arrivent qu'avec --photos,
        # et le calcul « ouvert / fermé » suppose des horaires sur la fiche.
        memes = ("Mêmes informations, mêmes photos" if maquette_photos
                 else "Mêmes informations")
        details = []
        if tel:
            details.append("le bouton d'appel toujours visible")
        if _a_horaires(etab):
            details.append("les horaires « ouvert / fermé » calculés en temps réel")
        vous_verrez = (_plier("Vous verrez notamment " + _enumerer(details) + ". C'est un "
                              "brouillon, pas une facture — dites-moi ce que vous en pensez, "
                              "même si c'est non.")
                       if details else
                       _plier("C'est un brouillon, pas une facture — dites-moi ce que vous en "
                              "pensez, même si c'est non."))
        preuve = _plier(
            f"Alors je me suis permis quelque chose : j'ai refait votre page d'accueil. "
            f"{memes}, mais pensée pour un client qui vous cherche sur son téléphone, dans "
            f"la rue, à 19 h :"
        ) + f"\n\n  {url_maquette}\n\n" + vous_verrez
    else:
        preuve = _plier(
            f"Chacun de ces points se corrige. Je peux vous montrer à quoi ressemblerait "
            f"{nom} en version corrigée."
        )

    corps = "\n\n".join(
        x for x in (accroche, constat_intro, constats, chiffre, preuve,
                    _lien_rdv(cfg, creneaux, bool(url_maquette)),
                    "Bien à vous,\n" + _signature(cfg),
                    "—\n" + _mentions(cfg, contact_generique)) if x)
    return {"type": "B_site_obsolete", "objet": objet, "corps": corps, "variante": variante}


# --- Type I : site injoignable ---------------------------------------------------
def mail_site_injoignable(etab: dict, audit: dict, cfg: dict,
                          url_maquette: str = "", creneaux: list[str] | None = None, *,
                          maquette_photos: bool = False,
                          contact_generique: bool | None = None) -> dict:
    """Le site n'a jamais répondu : on écrit l'échec, pas la page.

    Le seul fait disponible est celui-là : telle adresse, tel code d'erreur, tel
    jour. Aucune phrase de ce gabarit ne décrit le contenu, la mise en page ou
    l'âge du site — on ne l'a pas vu. Le verdict `non_auditable` (403, délai
    dépassé, erreur TLS) ne passe jamais ici : à celui-là, on n'écrit rien.
    """
    verifier_expedition(cfg)
    nom = etab.get("nom", "")
    ville = etab.get("ville") or "votre quartier"
    url, raison = _fait_injoignable(audit)
    quand = _date_audit(audit, cfg)
    objet_tpl, variante = _variante(OBJETS_I, etab["place_id"])
    objet = objet_tpl.format(nom=nom)

    accroche = (
        "Bonjour,\n\n"
        + _plier(
            f"Je cherchais une adresse à {ville} cette semaine et je suis passé sur la "
            f"fiche Google {de(nom)}"
            + _accroche_note(etab, "une réputation qui se voit")
            + "Puis j'ai cliqué sur « Site Web »."
        )
    )

    fait = _plier(
        f"Le lien de votre fiche pointe vers {url or 'l’adresse renseignée'}, et cette "
        f"adresse {_raison_lisible(raison)}"
        + (f", vérifié {quand}. " if quand else ". ")
        + "Je n'ai donc pas vu votre site, et je ne vais pas juger une page que je n'ai "
          "pas pu ouvrir : peut-être qu'elle a déménagé, peut-être que l'hébergement "
          "s'est arrêté, peut-être que c'est le lien de la fiche qui est resté sur "
          "l'ancienne adresse."
    )

    consequence = _plier(
        "Ce qui est sûr, c'est ce que voit un client : celui qui clique sur ce bouton "
        "depuis votre fiche tombe sur la même erreur que moi, et repart."
    )

    preuve = ""
    if url_maquette:
        repris = ("vos photos, vos horaires, votre note" if maquette_photos
                  else "vos horaires, votre note, votre adresse")
        preuve = _plier(
            f"En attendant, j'ai fait une page d'exemple à partir des seules informations "
            f"de votre fiche — {repris}. Ce n'est pas votre site refait : je ne l'ai jamais "
            f"vu. C'est ce à quoi peut ressembler une adresse à votre nom, tout de suite :"
        ) + f"\n\n  {url_maquette}\n\n" + _plier(
            "Et si c'est déjà réglé de votre côté, dites-le moi en une ligne : je corrige "
            "ma note et je ne vous relance pas."
        )
    else:
        preuve = _plier(
            "Si c'est déjà réglé de votre côté, dites-le moi en une ligne : je corrige ma "
            "note et je ne vous relance pas. Sinon, je peux vous montrer à quoi "
            "ressemblerait une page à votre nom, en ligne cette semaine."
        )

    corps = "\n\n".join(
        x for x in (accroche, fait, consequence, preuve, _lien_rdv(cfg, creneaux, bool(url_maquette)),
                    "Bien à vous,\n" + _signature(cfg),
                    "—\n" + _mentions(cfg, contact_generique)) if x)
    return {"type": "I_site_injoignable", "objet": objet, "corps": corps, "variante": variante}


# --- Relances -------------------------------------------------------------------
def relance(etab: dict, mail_initial: dict, cfg: dict, rang: int = 1,
            url_maquette: str = "", creneaux: list[str] | None = None, *,
            contact_generique: bool | None = None) -> dict:
    verifier_expedition(cfg)
    nom = etab.get("nom", "")
    if rang == 1:
        objets = OBJETS_R1 + OBJETS_R1_MAQUETTE if url_maquette else OBJETS_R1
        objet_tpl, variante = _variante(objets, etab["place_id"])
        objet = objet_tpl.format(nom=nom, objet_initial=mail_initial["objet"])
        corps = "\n\n".join(x for x in (
            "Bonjour,",
            _plier(
                f"Je remonte mon message — la période n'était peut-être pas la bonne."
            ),
            (_plier("La maquette de votre page est toujours en ligne, vous pouvez la regarder "
                    "quand vous avez cinq minutes :") + f"\n\n  {url_maquette}"
             if url_maquette else ""),
            _plier(
                "Une seule question, et vous pouvez répondre en un mot : est-ce un sujet pour "
                "vous cette année, oui ou non ? Si c'est non, je n'insiste plus, c'est une "
                "réponse parfaitement valable."
            ),
            _lien_rdv(cfg, creneaux, bool(url_maquette)),
            "Bien à vous,\n" + _signature(cfg),
            # Les mentions restent entières dans les relances : c'est ce qui rend
            # la prospection licite, et c'est là que le « STOP » est le plus utile.
            "—\n" + _mentions(cfg, contact_generique),
        ) if x)
    else:
        objet_tpl, variante = _variante(OBJETS_R2, etab["place_id"])
        objet = objet_tpl.format(nom=nom)
        corps = "\n\n".join(x for x in (
            "Bonjour,",
            _plier(
                f"Dernier message de ma part, promis. Sans réponse d'ici la fin de la semaine, "
                f"je considère que le sujet n'est pas d'actualité et je vous laisse tranquille."
            ),
            (_plier("Je laisse la maquette en ligne encore trente jours, elle est à vous si "
                    "elle vous sert :") + f"\n\n  {url_maquette}" if url_maquette else ""),
            _prix(cfg),
            _plier("Et si le moment n'est simplement pas le bon, dites-le moi : je vous "
                   "recontacte dans six mois, pas avant."),
            "Bien à vous,\n" + _signature(cfg),
            "—\n" + _mentions(cfg, contact_generique),
        ) if x)
    return {"type": f"relance_{rang}", "objet": objet, "corps": corps, "variante": variante}


def rediger(etab: dict, audit_row: dict, cfg: dict, url_maquette: str = "",
            creneaux: list[str] | None = None, *, maquette_photos: bool = False,
            contact_generique: bool | None = None) -> dict | None:
    """Choisit le bon gabarit selon le verdict de l'audit.

    Renvoie None quand il n'y a rien à écrire (`correct`), et lève
    `VerdictNonRedigeable` quand le verdict interdit d'écrire (`non_auditable`) ou
    n'est pas connu : mieux vaut une erreur bruyante qu'un mail au hasard.
    """
    verifier_expedition(cfg)
    verdict = audit_row.get("verdict")
    defauts = audit_row.get("defauts")
    if isinstance(defauts, str):
        defauts = json.loads(defauts or "[]")
    defauts = defauts or []
    audit = {**audit_row, "defauts": defauts}
    options = {"maquette_photos": maquette_photos, "contact_generique": contact_generique}
    if verdict == "absent":
        return mail_sans_site(etab, defauts, cfg, url_maquette, creneaux, **options)
    if verdict == "obsolete":
        return mail_site_obsolete(etab, audit, cfg, url_maquette, creneaux, **options)
    if verdict == "injoignable":
        return mail_site_injoignable(etab, audit, cfg, url_maquette, creneaux, **options)
    if verdict == "correct":
        return None  # leur site fait le travail : on ne démarche pas
    if verdict == "non_auditable":
        raise VerdictNonRedigeable(
            "Verdict « non_auditable » : le site n'a pas pu être analysé (403, 429, 5xx, "
            "délai dépassé, erreur TLS, anti-bot). On ne sait rien de vérifiable sur ce "
            "prospect — aucun gabarit ne s'applique et on ne lui écrit rien."
        )
    raise VerdictNonRedigeable(
        f"Verdict d'audit inconnu : {verdict!r}. Aucun gabarit ne s'applique."
    )
