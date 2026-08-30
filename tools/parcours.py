#!/usr/bin/env python3
"""Reconstruit l'approche du 770 à partir des VRAIES photos.

Les frames 1 à 53 du hero sont regénérées depuis `photos-src/`. Les frames
54 à 145 — la traversée de la porte et l'intérieur — sont conservées.

    python3 tools/parcours.py

MÉTHODE — pourquoi ce n'est pas un simple enchaînement de trois photos.

Un fondu entre deux photos cadrées indépendamment se voit toujours : au
moment du croisement, le bâtiment n'est ni à la même échelle ni au même
endroit dans les deux images, et l'œil décroche.

Ici c'est l'inverse. On décrit d'abord le mouvement de caméra dans
« l'espace de la porte » : à chaque frame, la porte doit occuper telle
largeur de l'écran, à telle position. Cette courbe est continue et
monotone du début à la fin. Ensuite seulement, pour chaque photo, on
résout le recadrage qui pose la porte exactement là.

Résultat : au moment du fondu, les deux photos montrent la porte à la même
taille et au même endroit. Le fondu devient un raccord.

Aucune image n'est générée ni interpolée : on ne recadre et on n'étalonne
que des photographies. C'est un lieu réel et identifiable.
"""
import pathlib
import numpy as np
from PIL import Image, ImageEnhance

ICI      = pathlib.Path(__file__).resolve().parent.parent
PHOTOS   = ICI / "photos-src"
JONCTION = 53
FORMATS  = {"seq169": (1440, 810), "seq916": (810, 1440)}
SUR_ECH  = 1.75          # agrandissement maximal toléré avant que ça pâteuse

# Boîte de la porte dans chaque photo, en fractions de l'image (relevé à la grille).
PORTE = {
    "p09": (.487, .445, .514, .545),
    "p06": (.484, .466, .532, .518),
    "p08": (.484, .596, .533, .724),
    "p04": (.467, .503, .559, .585),
    "p01": (.349, .359, .618, .692),
}

# deb/fin se recouvrent largement : le fondu dure une douzaine de frames.
TEMPS = [
    dict(nom="trottoir", seq169="p09", seq916="p06", deb=1,  fin=24),
    dict(nom="allée",    seq169="p08", seq916="p04", deb=14, fin=45),
    dict(nom="porte",    seq169="p01", seq916="p01", deb=36, fin=53),
]

# La porte passe de 3 % à 60 % de la largeur de l'écran. Progression
# géométrique : c'est ce que donne une caméra qui avance à vitesse constante.
LARG_DEB, LARG_FIN = .030, .600
CENTRE_DEB, CENTRE_FIN = (.50, .430), (.50, .500)


def visee(i):
    """Largeur et centre visés pour la porte, à l'écran, à la frame i."""
    t = (i - 1) / (JONCTION - 1)
    larg = LARG_DEB * (LARG_FIN / LARG_DEB) ** t
    cu = CENTRE_DEB[0] + (CENTRE_FIN[0] - CENTRE_DEB[0]) * t
    cv = CENTRE_DEB[1] + (CENTRE_FIN[1] - CENTRE_DEB[1]) * t
    return larg, cu, cv


def cadre_pour(photo, src, taille, larg, cu, cv):
    """Recadrage de `src` qui pose la porte à `larg` de large, centrée en (cu, cv).

    Renvoie la boîte de découpe. Si la photo ne peut pas donner cette taille
    sans être trop agrandie, on prend le maximum tolérable : la porte sera un
    peu plus petite que visé, ce que le fondu absorbe.
    """
    W, H = taille
    sw, sh = src.size
    x0, y0, x1, y1 = PORTE[photo]
    porte_w = (x1 - x0) * sw                      # largeur de la porte, en pixels source
    porte_cx = (x0 + x1) / 2 * sw
    porte_cy = (y0 + y1) / 2 * sh

    cw = porte_w / larg                           # largeur de découpe qui donne `larg`
    cw = max(cw, W / SUR_ECH)                     # jamais plus agrandi que SUR_ECH
    cw = min(cw, sw, sh * W / H)                  # ni plus grand que la photo
    ch = cw * H / W

    gx = porte_cx - cu * cw                       # bord gauche de la découpe
    gy = porte_cy - cv * ch
    gx = min(max(gx, 0), sw - cw)                 # rester dans la photo
    gy = min(max(gy, 0), sh - ch)
    return (gx, gy, gx + cw, gy + ch)


def etalonner(im):
    """Réchauffe légèrement : les photos sont froides, la suite générée est chaude."""
    r, g, b = im.split()
    r = r.point(lambda v: min(255, int(v * 1.02)))
    b = b.point(lambda v: int(v * 0.965))
    im = Image.merge("RGB", (r, g, b))
    return ImageEnhance.Contrast(ImageEnhance.Color(im).enhance(0.92)).enhance(1.04)


def cible_jonction(seq):
    a = np.asarray(Image.open(ICI / "img" / seq / "f_0054.jpg").convert("RGB"), float)
    return a.reshape(-1, 3).mean(0), a.reshape(-1, 3).std(0)


def vers_jonction(im, cible, poids):
    """Rejoint la densité de f_0054 : la photo de jour est 2x plus claire."""
    if poids <= 0:
        return im
    cm, cs = cible
    a = np.asarray(im, float)
    m, s = a.reshape(-1, 3).mean(0), a.reshape(-1, 3).std(0)
    cale = (a - m) * (cs / np.maximum(s, 1e-6)) + cm
    return Image.fromarray(np.clip(a * (1 - poids) + cale * poids, 0, 255).astype("uint8"))


def lisse(t):
    t = min(max(t, 0.0), 1.0)
    return t * t * (3 - 2 * t)


def rendu(temps, i, seq, taille, cache, cible):
    photo = temps[seq]
    if photo not in cache:
        cache[photo] = etalonner(Image.open(PHOTOS / f"{photo}.jpeg").convert("RGB"))
    src = cache[photo]
    im = src.resize(taille, Image.LANCZOS, box=cadre_pour(photo, src, taille, *visee(i)))
    if temps["nom"] == "porte":
        t = (i - temps["deb"]) / (temps["fin"] - temps["deb"])
        im = vers_jonction(im, cible, lisse((t - 0.30) / 0.70))
    return im


def construire(seq):
    taille, dest, cache = FORMATS[seq], ICI / "img" / seq, {}
    cible = cible_jonction(seq)
    for i in range(1, JONCTION + 1):
        actifs = [t for t in TEMPS if t["deb"] <= i <= t["fin"]]
        im = rendu(actifs[0], i, seq, taille, cache, cible)
        for suivant in actifs[1:]:               # fondu vers le temps suivant
            t = (i - suivant["deb"]) / (actifs[0]["fin"] - suivant["deb"])
            im = Image.blend(im, rendu(suivant, i, seq, taille, cache, cible), lisse(t))
        im.save(dest / f"f_{i:04d}.jpg", quality=88, optimize=True)
    print(f"  {seq}  frames 1-{JONCTION}  ({taille[0]}x{taille[1]})")


if __name__ == "__main__":
    for seq in FORMATS:
        construire(seq)
    print(f"\n  frames {JONCTION+1}-145 inchangées.")
