#!/usr/bin/env bash
# Publie les maquettes générées sur https://770lab.com/maquettes/
#
# Le dépôt des pages est SÉPARÉ de celui-ci (770lab/maquettes, privé, servi par
# GitHub Pages) : on ne veut pas des pages d'établissements réels dans l'historique
# du site 770lab.com/rabbi.
#
#   ./tools/prospect/publier.sh              # publie tout ce qui est dans out/maquettes
#   ./tools/prospect/publier.sh chez-mario   # publie une seule maquette
#
# Rien n'est écrasé côté distant sans passer par un commit : `git status` du dépôt
# des maquettes reste lisible, et un retrait se fait avec `git rm`.
set -euo pipefail

SOURCE="$(cd "$(dirname "$0")" && pwd)/out/maquettes"
DEPOT="${MAQUETTES_DEPOT:-$HOME/maquettes-770lab}"

[ -d "$SOURCE" ] || { echo "Aucune maquette dans $SOURCE — lancez d'abord « python3 -m tools.prospect maquette »." >&2; exit 1; }

if [ ! -d "$DEPOT/.git" ]; then
  echo "Première publication : clonage de 770lab/maquettes dans $DEPOT"
  git clone -q https://github.com/770lab/maquettes.git "$DEPOT"
fi

git -C "$DEPOT" pull -q --rebase

if [ $# -gt 0 ]; then
  for slug in "$@"; do
    [ -d "$SOURCE/$slug" ] || { echo "Maquette inconnue : $slug" >&2; exit 1; }
    rm -rf "${DEPOT:?}/$slug"
    cp -R "$SOURCE/$slug" "$DEPOT/$slug"
  done
else
  for chemin in "$SOURCE"/*/; do
    slug="$(basename "$chemin")"
    rm -rf "${DEPOT:?}/$slug"
    cp -R "$chemin" "$DEPOT/$slug"
  done
fi

cd "$DEPOT"
if git diff --quiet && git diff --cached --quiet && [ -z "$(git status --porcelain)" ]; then
  echo "Rien de nouveau à publier."
  exit 0
fi

git add -A
git commit -q -m "Maquettes : $(git status --porcelain | wc -l | tr -d ' ') entrée(s) mise(s) à jour"
git push -q origin main

echo "Publié. Compter une minute avant que GitHub Pages serve les pages :"
for chemin in "$SOURCE"/*/; do
  echo "  https://770lab.com/maquettes/$(basename "$chemin")/"
done
