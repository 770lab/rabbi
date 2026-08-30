#!/usr/bin/env python3
"""Reconstruit l'approche du 770 à partir d'UNE vraie photo par format.

Les frames 1 à 53 du hero sont regénérées depuis `photos-src/`. Les frames
54 à 145 — la traversée de la porte et l'intérieur — sont conservées.

    python3 tools/parcours.py

MÉTHODE. Une seule photo, un seul mouvement : un travelling avant continu
sur la vue de loin. Aucun fondu, aucune coupe — donc rien qui puisse se
voir. Un premier essai enchaînait trois photos ; même avec un raccord
calculé sur la porte, le changement d'image restait perceptible.

Le cadrage est piloté dans « l'espace de la porte » : à chaque frame elle
doit occuper telle largeur d'écran, à telle position. La courbe est
géométrique, ce que donne une caméra qui avance à vitesse constante, et
la porte reste ainsi rigoureusement centrée pendant tout le zoom.

Le raccord vers les frames générées (f_0054) est masqué par le voile noir
du site, déplacé pour tomber pile dessus — voir `veil` dans manifest.json.

Aucune image n'est générée ni interpolée : on ne recadre et on n'étalonne
que des photographies. C'est un lieu réel et identifiable.
"""
import json
import pathlib
import numpy as np
from PIL import Image, ImageEnhance

ICI      = pathlib.Path(__file__).resolve().parent.parent
PHOTOS   = ICI / "photos-src"
JONCTION = 53
COUNT    = 145
FORMATS  = {"seq169": (1440, 810), "seq916": (810, 1440)}

# La vue de loin, celle qu'Anthony voulait garder : le trottoir et la
# mosaïque 770 au premier plan, le bâtiment entier.
VUE  = {"seq169": "p09", "seq916": "p06"}
ZOOM = 5.5                      # facteur de travelling du début à f_0053

# Boîte de la porte dans la photo, en fractions (relevé à la grille).
PORTE = {"p09": (.487, .445, .514, .545),
         "p06": (.484, .466, .532, .518)}

# Le site mappe l'index de frame sur la progression du scroll par
# `f = min(1, p / 0.88) * (COUNT - 1)`. f_0054 est l'index 53.
VEIL_C = round(53 / (COUNT - 1) * 0.88, 4)
VEIL_W = 0.055


def cadre(photo, src, taille, larg):
    """Découpe qui pose la porte à `larg` de large, centrée à l'écran."""
    W, H = taille
    sw, sh = src.size
    x0, y0, x1, y1 = PORTE[photo]
    cw = (x1 - x0) * sw / larg
    cw = min(cw, sw, sh * W / H)
    ch = cw * H / W
    gx = min(max((x0 + x1) / 2 * sw - cw / 2, 0), sw - cw)
    gy = min(max((y0 + y1) / 2 * sh - ch / 2, 0), sh - ch)
    return (gx, gy, gx + cw, gy + ch)


def etalonner(im):
    r, g, b = im.split()
    r = r.point(lambda v: min(255, int(v * 1.02)))
    b = b.point(lambda v: int(v * 0.965))
    im = Image.merge("RGB", (r, g, b))
    return ImageEnhance.Contrast(ImageEnhance.Color(im).enhance(0.92)).enhance(1.04)


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


def construire(seq):
    taille = FORMATS[seq]
    photo  = VUE[seq]
    dest   = ICI / "img" / seq
    src    = etalonner(Image.open(PHOTOS / f"{photo}.jpeg").convert("RGB"))

    a = np.asarray(Image.open(dest / "f_0054.jpg").convert("RGB"), float).reshape(-1, 3)
    cible = (a.mean(0), a.std(0))

    x0, _, x1, _ = PORTE[photo]
    larg0 = (x1 - x0) * src.width / min(src.width, src.height * taille[0] / taille[1])

    for i in range(1, JONCTION + 1):
        t = (i - 1) / (JONCTION - 1)
        larg = larg0 * ZOOM ** t                       # travelling à vitesse constante
        im = src.resize(taille, Image.LANCZOS, box=cadre(photo, src, taille, larg))
        im = vers_jonction(im, cible, lisse((t - 0.45) / 0.55))
        im.save(dest / f"f_{i:04d}.jpg", quality=88, optimize=True)

    m = json.loads((dest / "manifest.json").read_text())
    m["veilC"], m["veilW"] = VEIL_C, VEIL_W
    (dest / "manifest.json").write_text(json.dumps(m, separators=(",", ":")))
    print(f"  {seq}  {photo}  zoom x1 -> x{ZOOM}  ({taille[0]}x{taille[1]})")


if __name__ == "__main__":
    for seq in FORMATS:
        construire(seq)
    print(f"\n  voile noir recentré sur f_0054 : veilC={VEIL_C}, veilW={VEIL_W}")
