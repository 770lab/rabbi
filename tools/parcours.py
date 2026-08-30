#!/usr/bin/env python3
"""Reconstruit l'approche du 770 à partir des VRAIES photos.

Les frames 1 à 53 du hero sont regénérées depuis `photos-src/` : trois temps
(le trottoir, l'allée, la porte) enchaînés par fondu, chacun avec un lent
travelling avant. Les frames 54 à 145 — la traversée de la porte et
l'intérieur de la synagogue — sont conservées telles quelles.

Aucune image n'est générée ni interpolée : on ne recadre et on n'étalonne
que des photographies réelles. C'est un lieu réel et identifiable.

    python3 tools/parcours.py
"""
import pathlib
import numpy as np
from PIL import Image, ImageEnhance

ICI    = pathlib.Path(__file__).resolve().parent.parent
PHOTOS = ICI / "photos-src"
JONCTION = 53          # dernière frame refaite ; f_0054 ouvre déjà la porte

FORMATS = {"seq169": (1440, 810), "seq916": (810, 1440)}

# deb/fin se chevauchent de 4 frames : c'est la durée du fondu.
# z0 -> z1 : le travelling avant. cy < .5 remonte le cadre vers la porte.
TEMPS = [
    dict(nom="trottoir", seq169="p09", seq916="p06", deb=1,  fin=24,
         z0=1.00, z1=1.22, cy0=.54, cy1=.50),
    dict(nom="allée",    seq169="p08", seq916="p04", deb=20, fin=42,
         z0=1.04, z1=3.00, cy0=.48, cy1=.62),   # descend vers la porte en poussant
    dict(nom="porte",    seq169="p01", seq916="p01", deb=36, fin=53,
         z0=1.00, z1=2.15, cy0=.46, cy1=.50),
]


def cadrer(src, taille, zoom, cy):
    """Découpe dans la photo le plus grand rectangle au bon format, réduit par `zoom`."""
    W, H = taille
    r = W / H
    sw, sh = src.size
    cw, ch = (sh * r, sh) if sw / sh > r else (sw, sw / r)
    cw, ch = cw / zoom, ch / zoom
    cx = sw / 2
    cyy = min(max(sh * cy, ch / 2), sh - ch / 2)      # jamais hors de la photo
    box = (cx - cw / 2, cyy - ch / 2, cx + cw / 2, cyy + ch / 2)
    return src.resize((W, H), Image.LANCZOS, box=box)


def etalonner(im):
    """Réchauffe la photo vers la palette du site (brique, or, crème).

    Les photos sont prises en fin de journée, ciel bleu : elles arrivent
    froides, alors que la séquence intérieure conservée est chaude. Sans ça
    le raccord à f_0054 saute aux yeux.
    """
    r, g, b = im.split()
    r = r.point(lambda v: min(255, int(v * 1.02)))
    b = b.point(lambda v: int(v * 0.965))
    im = Image.merge("RGB", (r, g, b))
    im = ImageEnhance.Color(im).enhance(0.92)
    return ImageEnhance.Contrast(im).enhance(1.04)


def lisse(t):
    t = min(max(t, 0.0), 1.0)
    return t * t * (3 - 2 * t)


def cible_jonction(seq):
    """Moyenne et écart-type de f_0054 : le plan sur lequel il faut atterrir."""
    a = np.asarray(Image.open(ICI / "img" / seq / "f_0054.jpg").convert("RGB"), float)
    a = a.reshape(-1, 3)
    return a.mean(0), a.std(0)


def vers_jonction(im, cible, poids):
    """Rapproche progressivement l'image de la luminosité de f_0054.

    La photo de la porte est prise en plein jour : elle est deux fois plus
    claire que la frame générée qui suit. Sans ce recalage le raccord fait
    un flash. Le poids monte sur la fin du temps « porte », ce qui se lit
    comme on entre dans l'ombre du porche.
    """
    if poids <= 0:
        return im
    cm, cs = cible
    a = np.asarray(im, float)
    plat = a.reshape(-1, 3)
    m, s = plat.mean(0), plat.std(0)
    cale = (a - m) * (cs / np.maximum(s, 1e-6)) + cm
    return Image.fromarray(np.clip(a * (1 - poids) + cale * poids, 0, 255).astype("uint8"))


def rendu(temps, i, seq, taille, cache, cible):
    p = temps[seq]
    if p not in cache:
        cache[p] = etalonner(Image.open(PHOTOS / f"{p}.jpeg").convert("RGB"))
    t = (i - temps["deb"]) / (temps["fin"] - temps["deb"])
    z  = temps["z0"] + (temps["z1"] - temps["z0"]) * t
    cy = temps["cy0"] + (temps["cy1"] - temps["cy0"]) * t
    im = cadrer(cache[p], taille, z, cy)
    if temps["nom"] == "porte":
        # rien sur le premier tiers, puis on rejoint f_0054 exactement
        im = vers_jonction(im, cible, lisse((t - 0.34) / 0.66))
    return im


def construire(seq):
    taille = FORMATS[seq]
    dest = ICI / "img" / seq
    cache = {}
    cible = cible_jonction(seq)
    for i in range(1, JONCTION + 1):
        actifs = [t for t in TEMPS if t["deb"] <= i <= t["fin"]]
        if len(actifs) == 1:
            im = rendu(actifs[0], i, seq, taille, cache, cible)
        else:                                   # fondu entre deux temps
            a, b = actifs[0], actifs[1]
            t = (i - b["deb"]) / (a["fin"] - b["deb"])
            im = Image.blend(rendu(a, i, seq, taille, cache, cible),
                             rendu(b, i, seq, taille, cache, cible), lisse(t))
        im.save(dest / f"f_{i:04d}.jpg", quality=88, optimize=True)
    print(f"  {seq}  frames 1-{JONCTION} refaites  ({taille[0]}x{taille[1]})")


if __name__ == "__main__":
    for seq in FORMATS:
        construire(seq)
    print(f"\n  frames {JONCTION+1}-145 inchangées : la porte s'ouvre et l'intérieur suit.")
