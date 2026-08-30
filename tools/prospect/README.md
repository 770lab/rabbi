# 📍 Prospect

**Google Maps → audit du site → maquette de refonte → mail + créneau d'appel.**

On ne vend pas un site. On montre le site. Pour chaque commerce repéré, la
chaîne produit une maquette réelle de sa page d'accueil et un mail qui ne dit
que des choses mesurées — pas une phrase de vendeur.

Aucune dépendance à installer : bibliothèque standard Python uniquement.

---

## Démarrage en trois minutes (hors ligne, sans clé API)

```bash
cp tools/prospect/prospect.config.example.json tools/prospect/prospect.config.json   # puis remplissez-le
python3 -m tools.prospect chercher --demo
python3 -m tools.prospect auditer --demo
python3 -m tools.prospect maquette
python3 -m tools.prospect rediger --afficher
python3 -m http.server -d tools/prospect/out/maquettes 8080           # allez voir les maquettes
```

Le mode `--demo` travaille sur huit commerces fictifs et trois faux sites
(un site de 2011 en tableaux, un Wix daté, une boulangerie bien faite) servis
localement. C'est le meilleur moyen de comprendre la chaîne avant de dépenser
un centime d'API.

## En vrai

```bash
export GOOGLE_MAPS_API_KEY='...'        # console.cloud.google.com → Places API (New)

python3 -m tools.prospect chercher --zone "Lyon 3e" --priorite prioritaires
python3 -m tools.prospect auditer
python3 -m tools.prospect enrichir
python3 -m tools.prospect maquette --photos
python3 -m tools.prospect rediger --creneaux tools/prospect/out/creneaux.json
python3 -m tools.prospect exporter --format json        # → brouillons Gmail
python3 -m tools.prospect exporter --format appels      # → feuille d'appels
```

---

## Les trois populations

L'audit range chaque établissement dans une case, et une seule.

| Verdict | Ce qu'on a trouvé | Ce qu'on envoie |
|---|---|---|
| `absent` | Aucun site, ou seulement une page Facebook | **Mail A** — « vos 428 avis ne mènent nulle part » |
| `obsolete` | Un site, sous le seuil de l'audit | **Mail B** — les trois défauts mesurés, chiffrés |
| `injoignable` | Le site de la fiche ne répond plus | **Mail B**, angle « vos clients tombent sur une erreur » |
| `correct` | Site au-dessus du seuil | **Rien.** On ne démarche pas quelqu'un qui n'a pas de problème. |

Les restaurants passent d'abord (`--priorite prioritaires`), les autres métiers
ensuite (`--priorite secondaires`). C'est réglable dans la configuration.

## Ce que l'audit mesure vraiment

Vingt-deux contrôles, chacun avec sa preuve et son argument métier — pour que
le mail cite des faits, jamais des impressions :

- **Transport** : HTTPS, site qui répond.
- **Mobile** : balise viewport, media queries. *(Le défaut le plus lourd : c'est
  là que se joue la recherche locale.)*
- **Référencement** : `title` (absent, générique, trop court), meta description,
  H1, données structurées schema.org, Open Graph, sitemap, robots, favicon.
- **Performance** : poids de la page, temps de réponse serveur, images non
  différées.
- **Technologies datées** : Flash, jQuery 1.x/2.x, `<font>`, `<center>`,
  `<marquee>`, mise en page en tableaux, constructeurs vieillissants
  (Wix ancienne génération, Jimdo, e-monsite, Solocal…), pied de page daté.
- **Conversion locale** : numéro cliquable sur mobile, téléphone visible,
  horaires affichés, appel à l'action.

En parallèle, la **fiche Google** est notée séparément (site, téléphone,
horaires, photos, nombre d'avis, note). Une fiche à 55/100 est souvent un
meilleur angle d'attaque qu'un site moyen : c'est gratuit à corriger, et ça
ouvre la conversation.

## Le score de priorité

Qui appeler en premier ? `traction commerciale × marge de progression` :
le nombre d'avis (jusqu'à 50 points), la note (jusqu'à 28), le verdict de
l'audit (jusqu'à 45), l'état de la fiche Google, la présence d'un téléphone.
Un établissement fermé définitivement tombe à zéro.

Concrètement : un restaurant à 4,6/5 avec 428 avis **et sans site** sort en tête.
C'est exactement le profil qui perd le plus d'argent chaque jour.

## La maquette

Une page unique, autonome, sans dépendance externe : photo en pleine largeur,
note Google, bouton d'appel toujours visible sur mobile, horaires avec calcul
**ouvert / fermé en temps réel**, données structurées schema.org, thème clair et
sombre. Elle se génère en une seconde et pèse quelques kilo-octets.

Elle porte **toujours** un bandeau en haut : *« Maquette de refonte proposée
par X — page non officielle »*, avec le bouton de prise de rendez-vous.
Ce bandeau ne s'enlève pas. C'est ce qui sépare une démarche commerciale
honnête d'une usurpation d'identité.

## Les agents

Trois agents Claude prennent le relais là où le code s'arrête :

| Agent | Rôle |
|---|---|
| `prospecteur` | Exécute la chaîne sur une zone, rend le compte rendu |
| `redacteur-mails` | Relit chaque brouillon, corrige, dépose dans Gmail — **n'envoie jamais** |
| `closeur-agenda` | Lit Google Agenda, sort trois créneaux, pose le rendez-vous accepté |

Et une compétence `/prospection` qui orchestre le tout.

Pour les créneaux d'appel, deux approches :
- **Lien permanent** (recommandé) : une page de rendez-vous Google Agenda dans
  `rdv.lien`. Elle ne périme jamais.
- **Créneaux en dur** : `closeur-agenda` écrit `tools/prospect/out/creneaux.json`, injecté par
  `rediger --creneaux`. Ça convertit mieux — surtout en relance — mais ça
  périme en quelques jours.

---

## Ce que le code refuse de faire

- **Envoyer.** Il produit des brouillons. C'est vous qui appuyez sur envoyer.
- **Écrire une phrase non mesurée.** Chaque reproche fait à un site vient d'un
  défaut relevé par l'audit, avec sa preuve entre parenthèses.
- **Démarcher un site correct.**
- **Ramasser des adresses nominatives.** Seules les adresses génériques
  publiées par l'entreprise (`contact@`, `info@`, `reservation@`) sont
  retenues ; les autres sont marquées et passent en dernier.
- **Oublier un « STOP »** : `python3 -m tools.prospect stop untel@exemple.fr`
  exclut définitivement l'adresse ou le domaine, de toutes les campagnes.

### Le cadre

La prospection B2B par email est licite en France sans consentement préalable,
à trois conditions : le message concerne l'activité professionnelle du
destinataire, l'expéditeur est identifiable, et l'opposition est possible à
tout moment. Les trois sont intégrées au gabarit de mail — n'y touchez pas.

Côté Google, on passe par l'**API Places officielle**, jamais par le scraping
de `maps.google.com`, qui est contraire aux conditions d'utilisation et cassé
une semaine sur deux. Les photos sont téléchargées puis servies localement :
la clé API ne se retrouve jamais dans une page publiée.

Enfin : envoyez depuis un **domaine dédié** à la prospection, pas depuis votre
domaine principal, et respectez `quotas.max_emails_par_jour`. Un domaine grillé,
c'est toute la campagne qui meurt — et votre messagerie avec.

---

## Mise en route (à faire une fois)

### 1. La clé Google Places

1. [console.cloud.google.com](https://console.cloud.google.com) → créer un projet.
2. Activer **Places API (New)** — pas l'ancienne, les deux coexistent.
3. Identifiants → Créer → Clé API. **Restreindre la clé à Places API (New)**,
   sinon une fuite coûte cher.
4. La facturation doit être active. Google offre un crédit mensuel qui couvre
   largement un balayage de quartier ; au-delà, `searchText` se facture au
   millier d'appels. Le mode `--pavage` multiplie les appels : à réserver aux
   zones vraiment denses.

```bash
export GOOGLE_MAPS_API_KEY='AIza...'
```

### 2. L'adresse d'expédition

Ne prospectez jamais depuis votre boîte personnelle : quelques signalements
spam suffisent à dégrader la délivrabilité de toutes vos conversations.

Le plus simple avec un domaine déjà en main : **Google Workspace**, un
utilisateur (~7 €/mois). Vous obtenez une vraie boîte, et surtout une signature
DKIM à votre domaine — ce qui décide de l'arrivée en boîte de réception ou en
indésirables.

Les quatre enregistrements à poser chez votre hébergeur DNS :

| Type | Nom | Valeur |
|---|---|---|
| MX | `@` | `smtp.google.com` (priorité 1) |
| TXT | `@` | `v=spf1 include:_spf.google.com ~all` |
| TXT | `google._domainkey` | la clé DKIM fournie par Workspace (Apps → Gmail → Authentifier les e-mails) |
| TXT | `_dmarc` | `v=DMARC1; p=none; rua=mailto:votre@adresse` |

Si le domaine est derrière Cloudflare, ces enregistrements se posent dans
l'onglet DNS, en mode **DNS only** (nuage gris) — jamais proxifiés.

Comptez 24 h de propagation, puis **chauffez l'adresse** : 5 envois le premier
jour, 10 le deuxième, puis doublez jusqu'au quota. Une adresse neuve qui envoie
40 mails d'un coup part directement en indésirables.

### 3. La page de rendez-vous

Google Agenda → Créer → **Plage de rendez-vous**. Réglez la durée sur 20
minutes, les disponibilités sur 10h–11h30 et 14h30–17h (un commerçant n'est
pas joignable pendant le service), un délai minimum de 12 h et un maximum de
3 semaines. Copiez le lien public dans `rdv.lien`.

C'est préférable aux créneaux en dur : le lien ne périme jamais.

### 4. Où tourne quoi

`chercher` n'a besoin que de l'API Google. **`auditer` et `enrichir` visitent
les sites des prospects** : il leur faut un accès web ordinaire. Sur une
machine dont la sortie réseau est filtrée, ces deux étapes échouent en
`injoignable` sur tout le monde — ce qui n'est pas un verdict, c'est une
panne. Vérifiez toujours le récapitulatif : si tout ressort `injoignable`,
c'est le réseau, pas les prospects.

---

## Pistes

Ce qui n'est pas encore fait, par ordre de rendement décroissant.

1. **Le téléphone d'abord pour les fiches sans site.** Elles n'ont presque
   jamais d'email récupérable — c'est justement le signe qu'elles ne sont pas
   sollicitées. `exporter --format appels` sort déjà la feuille d'appel avec
   l'accroche et les deux défauts principaux. C'est le canal le plus rentable
   de toute la chaîne, et le moins encombré.
2. **Le cheval de Troie « fiche Google ».** Proposer d'abord de corriger la
   fiche (horaires, photos, description) — c'est gratuit, visible en 48 h, et
   ça crée la confiance qui rend la vente du site évidente.
3. **Le classement concurrentiel.** « Sur *restaurant Lyon 3e*, vous sortez 7e ;
   voici les six qui passent devant, et pourquoi. » Rien ne pique plus qu'un
   nom de concurrent. Faisable avec l'API Places, en comparant les fiches d'une
   même zone.
4. **Une page de vente par prospect** plutôt qu'un simple lien de maquette :
   l'audit affiché en clair, le avant/après, le prix, le bouton créneau.
   Le lien étant unique, une visite = un signal d'intérêt, sans pixel espion.
5. **La preuve sociale de quartier.** Le premier client d'une rue ouvre la rue :
   « j'ai refait le site du salon au 14 ». Traiter une zone à fond plutôt que
   d'éparpiller.
6. **Le courrier papier** pour le haut du panier (priorité > 110). Dans le
   commerce local, une lettre avec la maquette imprimée obtient un taux de
   réponse sans commune mesure avec l'email — et personne ne le fait plus.
7. **La saisonnalité.** Attaquer les restaurants en janvier, les glaciers en
   février, les jardiniers en octobre : au creux de leur activité, ils ont le
   temps de lire et le budget de l'année à engager.
8. **Un tableau de bord** HTML statique : entonnoir par zone, taux de réponse
   par variante d'objet (les variantes A/B sont déjà enregistrées).
9. **La réponse aux avis Google** comme second service, récurrent celui-là :
   les prospects à 3,8/5 avec des avis sans réponse sont visibles dans la base.

---

## Commandes

| Commande | Ce qu'elle fait |
|---|---|
| `chercher` | Interroge Google Places, remplit la base (`--demo`, `--zone`, `--priorite`, `--pavage`) |
| `auditer` | Audite les sites et les fiches (`--demo`, `--refaire`, `--limite`) |
| `enrichir` | Cherche les adresses de contact publiques |
| `maquette` | Génère les pages de refonte (`--photos`) |
| `rediger` | Rédige les brouillons (`--creneaux`, `--afficher`) |
| `relancer` | Prépare les relances J+4 / J+9 (`--rang`) |
| `exporter` | `csv` · `json` (Gmail) · `eml` · `appels` |
| `liste` | La base, triée par priorité |
| `stop` | Exclut définitivement une adresse ou un domaine |
| `pipeline` | Tout d'un coup |

Tout vit dans `tools/prospect/out/prospection.sqlite3` — les maquettes dans `tools/prospect/out/maquettes/`,
les exports dans `tools/prospect/out/exports/`. Rien de tout ça n'est versionné.

## Le pavage

`searchNearby` plafonne à 20 résultats. Un quartier dense en contient bien plus.
`--pavage 800` découpe la zone en cercles de 800 m et balaie en damier — plus
lent, plus cher en appels d'API, mais exhaustif.

```bash
python3 -m tools.prospect chercher --zone "Lyon 3e" --pavage 800
```
