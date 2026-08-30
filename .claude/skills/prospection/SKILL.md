---
name: prospection
description: Lance une campagne de prospection locale de bout en bout — Google Maps, audit des sites, maquettes de refonte, brouillons d'emails et créneaux d'appel. À utiliser quand on demande de prospecter une ville, un quartier ou un métier, de trouver des commerces sans site, ou de préparer des mails de démarchage local.
---

# Campagne de prospection locale

## Le principe

On ne vend pas un site : on montre le site. La chaîne produit, pour chaque
commerce, une maquette réelle de sa page d'accueil, et un mail qui ne dit que
des choses mesurées.

Trois populations, trois traitements :

| Verdict | Ce qu'on a trouvé | Ce qu'on envoie |
|---|---|---|
| `absent` | Pas de site, ou seulement une page Facebook | Mail A — « votre fiche ne mène nulle part » |
| `obsolete` | Un site, mais sous le seuil de l'audit | Mail B — les trois défauts mesurés |
| `injoignable` | Le site de la fiche ne répond plus | Mail B, angle « vos clients tombent sur une erreur » |
| `correct` | Site au-dessus du seuil | **Rien.** On ne démarche pas. |

## Déroulé

1. **Vérifier la configuration.** `tools/prospect/prospect.config.json` doit exister (modèle :
   `tools/prospect/prospect.config.example.json`) avec l'identité de l'expéditeur, le lien de
   réservation et au moins une zone. `GOOGLE_MAPS_API_KEY` doit être dans
   l'environnement — sinon, `--demo` permet de tout essayer hors ligne.

2. **Lancer le balayage** via l'agent `prospecteur`, restaurants d'abord :

   ```
   python3 -m tools.prospect chercher --zone "Lyon 3e" --priorite prioritaires
   python3 -m tools.prospect auditer
   python3 -m tools.prospect enrichir
   python3 -m tools.prospect maquette --photos
   ```

3. **Préparer les créneaux** avec l'agent `closeur-agenda`, qui écrit
   `tools/prospect/out/creneaux.json` depuis Google Agenda.

4. **Rédiger** :
   `python3 -m tools.prospect rediger --creneaux tools/prospect/out/creneaux.json`
   puis `python3 -m tools.prospect exporter --format json`.

5. **Déposer les brouillons Gmail** via l'agent `redacteur-mails`.
   Il relit chaque mail avant de le déposer. **Personne n'envoie à la place de
   l'utilisateur.**

6. **Relancer** à J+4 puis J+9 :
   `python3 -m tools.prospect relancer --rang 1`

## Règles à ne pas franchir

- **Jamais d'envoi automatique.** On produit des brouillons, l'utilisateur
  appuie sur envoyer.
- **Jamais d'affirmation non mesurée** dans un mail. Chaque reproche fait au
  site doit venir d'un défaut relevé par `auditer`, avec sa preuve.
- **Adresses génériques** (`contact@`, `info@`) de préférence. Une adresse
  nominative est une donnée personnelle : on s'en abstient.
- **Une demande d'arrêt est définitive** :
  `python3 -m tools.prospect stop adresse@exemple.fr`
- **La maquette porte toujours son bandeau** « proposition, page non
  officielle ». On ne l'enlève pas : c'est ce qui distingue une démarche
  commerciale honnête d'une usurpation.
- **Quota d'envoi** : ce que dit `quotas.max_emails_par_jour`, pas plus. Au-delà,
  le domaine finit en spam et toute la campagne meurt.

## Si l'utilisateur veut aller plus loin

Voir la section « Pistes » du `README.md` : appel téléphonique pour
les fiches sans email, angle « fiche Google incomplète », page de vente
personnalisée, suivi des ouvertures.
