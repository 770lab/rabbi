"""Rédaction des emails de prospection.

Principe : aucune phrase générique. Chaque mail s'appuie sur un fait vérifié
(note Google, nombre d'avis, défaut mesuré par l'audit) et se termine par une
seule action possible : choisir un créneau.
"""

from __future__ import annotations

import hashlib
import json
import textwrap

LARGEUR = 78

# --- Objets, en variantes A/B/C ------------------------------------------------
OBJETS_A = [
    "{nom} : vos {avis} avis Google ne mènent nulle part",
    "Je me suis permis de faire une page pour {nom}",
    "{nom}, {ville} — il vous manque une adresse à donner",
]
OBJETS_B = [
    "{nom} : votre site s'affiche mal sur téléphone",
    "Un détail sur le site de {nom} qui vous coûte des appels",
    "J'ai refait la page d'accueil de {nom} — 2 minutes pour voir ?",
]
OBJETS_R1 = [
    "Re : {objet_initial}",
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


def de(nom: str) -> str:
    """« Le Comptoir » → « du Comptoir ». Un mail qui écorche le nom est jeté."""
    n = (nom or "").strip()
    bas = n.lower()
    if bas.startswith("les "):
        return "des " + n[4:]
    if bas.startswith("le "):
        return "du " + n[3:]
    if bas.startswith("la "):
        return "de la " + n[3:]
    if bas.startswith(("l'", "l\u2019")):
        return "de " + n
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


def _mentions(cfg: dict, email: str = "") -> str:
    i = cfg["identite"]
    return _plier(
        f"Ce message professionnel est adressé à l'adresse de contact publique de "
        f"l'établissement, au titre de la prospection entre professionnels. "
        f"{i.get('societe') or i.get('nom')}"
        + (f", {i['adresse_postale']}" if i.get("adresse_postale") else "")
        + f". Répondez « STOP » et je supprime définitivement votre adresse de mes "
          f"fichiers, sans autre message."
    )


def _phrase_note(etab: dict) -> str:
    """En français, on écrit 4,6/5 — pas 4.6/5. Le détail se remarque."""
    note, avis = etab.get("note"), etab.get("nb_avis") or 0
    fr = str(note).replace(".", ",") if note else ""
    if note and avis >= 20:
        return f"{fr}/5 sur {avis} avis Google"
    if note and avis:
        return f"{fr}/5 sur Google"
    return ""


def _bloc_defauts(defauts: list[dict], maxi: int = 3) -> str:
    """Les constats, en puces, avec l'argument métier plutôt que le jargon."""
    lignes = []
    for d in defauts[:maxi]:
        arg = d.get("argument") or d.get("libelle")
        preuve = f" ({d['preuve']})" if d.get("preuve") else ""
        lignes.append(_plier(f"— {d['libelle']}{preuve}. {arg}"))
    return "\n\n".join(lignes)


def _lien_rdv(cfg: dict, creneaux: list[str] | None) -> str:
    lien = cfg["rdv"].get("lien", "")
    duree = cfg["rdv"].get("duree_min", 20)
    if creneaux:
        liste = "\n".join(f"  · {c}" for c in creneaux[:3])
        base = _plier(
            f"Si ça vous parle, je vous montre tout en {duree} minutes au téléphone. "
            f"Voici mes créneaux libres :") + f"\n\n{liste}\n"
        if lien:
            base += f"\nUn autre moment vous arrange mieux ? Tout mon agenda est ici :\n{lien}"
        else:
            base += "\nDites-moi simplement lequel vous convient."
        return base
    if lien:
        return _plier(
            f"Si ça vous parle, prenez {duree} minutes dans mon agenda, au moment qui "
            f"vous arrange — je vous fais le tour de la maquette au téléphone :") + f"\n{lien}"
    return _plier(f"Si ça vous parle, répondez-moi avec deux ou trois moments qui vous "
                  f"arrangent cette semaine, je vous appelle {duree} minutes.")


# --- Type A : aucun site --------------------------------------------------------
def mail_sans_site(etab: dict, manques_fiche: list[dict], cfg: dict,
                   url_maquette: str = "", creneaux: list[str] | None = None) -> dict:
    nom = etab.get("nom", "")
    ville = etab.get("ville") or "votre quartier"
    avis = etab.get("nb_avis") or 0
    note = _phrase_note(etab)
    objet_tpl, variante = _variante(OBJETS_A, etab["place_id"])
    objet = objet_tpl.format(nom=nom, avis=avis, ville=ville)

    reseaux = any(d["code"] == "reseaux_seulement" for d in manques_fiche)
    accroche = (
        f"Bonjour,\n\n"
        + _plier(
            f"Je cherchais une adresse à {ville} cette semaine et je suis tombé sur la fiche "
            f"Google {de(nom)}"
            + (f". {note}, c'est le genre de fiche qui donne envie de pousser la porte. "
               if note else ". ")
            + ("En cliquant sur « Site Web », on arrive sur une page de réseau social, "
               "pas sur un vrai site à vous."
               if reseaux else
               "Puis j'ai cliqué sur « Site Web », et il ne s'est rien passé : le bouton "
               "de votre fiche est vide.")
        )
    )

    consequence = _plier(
        f"Concrètement, ça veut dire trois choses. Les gens qui vous cherchent par votre nom "
        f"vous trouvent — mais ceux qui tapent « {etab.get('categorie','').lower() or 'votre métier'} "
        f"{ville} » ne vous voient jamais : Google n'a rien à leur montrer de votre côté. "
        f"Ceux qui hésitent entre vous et le voisin n'ont pas de photos, pas de carte, pas "
        f"d'histoire à lire. Et tout ce que vous avez construit — ces {avis} avis — repose sur "
        f"une page que Google peut modifier, suspendre ou monétiser sans vous demander votre avis."
    )

    preuve = ""
    if url_maquette:
        preuve = _plier(
            f"Plutôt que de vous l'expliquer, je me suis permis de la faire. J'ai repris vos "
            f"informations publiques — vos photos, vos horaires, votre note — et j'en ai fait "
            f"une page. Elle est en ligne, elle marche sur téléphone, on peut vous appeler d'un "
            f"doigt :"
        ) + f"\n\n  {url_maquette}\n\n" + _plier(
            "Regardez-la comme un brouillon : tout est modifiable, et elle vous appartient si "
            "vous la voulez."
        )
    else:
        preuve = _plier(
            "Je peux vous montrer en quelques minutes à quoi ressemblerait une page à votre nom, "
            "faite avec vos vraies photos et vos vrais horaires."
        )

    corps = "\n\n".join(
        x for x in (accroche, consequence, preuve, _lien_rdv(cfg, creneaux),
                    "Bien à vous,\n" + _signature(cfg), "—\n" + _mentions(cfg)) if x)
    return {"type": "A_sans_site", "objet": objet, "corps": corps, "variante": variante}


# --- Type B : site obsolète -----------------------------------------------------
def mail_site_obsolete(etab: dict, audit: dict, cfg: dict,
                       url_maquette: str = "", creneaux: list[str] | None = None) -> dict:
    nom = etab.get("nom", "")
    ville = etab.get("ville") or "votre quartier"
    note = _phrase_note(etab)
    score = audit.get("score")
    defauts = audit.get("defauts", [])
    principal = defauts[0] if defauts else {}
    objet_tpl, variante = _variante(OBJETS_B, etab["place_id"])
    objet = objet_tpl.format(nom=nom)

    accroche = (
        "Bonjour,\n\n"
        + _plier(
            f"J'ai regardé {nom} de près cette semaine"
            + (f". {note}, une réputation solide. " if note else ". ")
            + "Puis j'ai ouvert votre site sur mon téléphone, et l'écart m'a sauté aux yeux. "
              "Votre maison mérite bien mieux que ce que cette page renvoie."
        )
    )

    constat_intro = _plier(
        f"Je ne dis pas ça au hasard, j'ai mesuré. Voici les trois points qui vous coûtent "
        f"le plus cher aujourd'hui :"
    )
    constats = _bloc_defauts(defauts, 3)

    chiffre = ""
    if isinstance(score, int):
        chiffre = _plier(
            f"Au total, votre page ressort à {score}/100 sur les critères que Google utilise "
            f"pour classer les sites locaux. Ce n'est pas une question de goût : c'est ce qui "
            f"décide si vous apparaissez au-dessus ou en dessous du concurrent d'en face."
        )

    preuve = ""
    if url_maquette:
        preuve = _plier(
            "Alors je me suis permis quelque chose : j'ai refait votre page d'accueil. "
            "Mêmes informations, mêmes photos, mais pensée pour un client qui vous cherche "
            "sur son téléphone, dans la rue, à 19 h :"
        ) + f"\n\n  {url_maquette}\n\n" + _plier(
            "Vous verrez notamment le bouton d'appel toujours visible et les horaires "
            "« ouvert / fermé » calculés en temps réel. C'est un brouillon, pas une "
            "facture — dites-moi ce que vous en pensez, même si c'est non."
        )
    else:
        preuve = _plier(
            f"Chacun de ces points se corrige. Je peux vous montrer à quoi ressemblerait "
            f"{nom} en version corrigée."
        )

    corps = "\n\n".join(
        x for x in (accroche, constat_intro, constats, chiffre, preuve,
                    _lien_rdv(cfg, creneaux),
                    "Bien à vous,\n" + _signature(cfg), "—\n" + _mentions(cfg)) if x)
    return {"type": "B_site_obsolete", "objet": objet, "corps": corps, "variante": variante}


# --- Relances -------------------------------------------------------------------
def relance(etab: dict, mail_initial: dict, cfg: dict, rang: int = 1,
            url_maquette: str = "", creneaux: list[str] | None = None) -> dict:
    nom = etab.get("nom", "")
    if rang == 1:
        objet_tpl, variante = _variante(OBJETS_R1, etab["place_id"])
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
            _lien_rdv(cfg, creneaux),
            "Bien à vous,\n" + _signature(cfg),
            "—\n" + _mentions(cfg),
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
            _plier("Et si le moment n'est simplement pas le bon, dites-le moi : je vous "
                   "recontacte dans six mois, pas avant."),
            "Bien à vous,\n" + _signature(cfg),
            "—\n" + _mentions(cfg),
        ) if x)
    return {"type": f"relance_{rang}", "objet": objet, "corps": corps, "variante": variante}


def rediger(etab: dict, audit_row: dict, cfg: dict, url_maquette: str = "",
            creneaux: list[str] | None = None) -> dict | None:
    """Choisit le bon gabarit selon le verdict de l'audit."""
    verdict = audit_row.get("verdict")
    defauts = audit_row.get("defauts")
    if isinstance(defauts, str):
        defauts = json.loads(defauts or "[]")
    defauts = defauts or []
    if verdict == "absent":
        return mail_sans_site(etab, defauts, cfg, url_maquette, creneaux)
    if verdict in ("obsolete", "injoignable"):
        return mail_site_obsolete(etab, {**audit_row, "defauts": defauts}, cfg,
                                  url_maquette, creneaux)
    return None  # site correct : on ne démarche pas
