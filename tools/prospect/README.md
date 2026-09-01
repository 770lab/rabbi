# 📍 Prospect

**Google Maps → audit du site → maquette de refonte → mail + créneau d'appel.**

On ne vend pas un site. On montre le site. Pour chaque commerce repéré, la
chaîne produit une maquette réelle de sa page d'accueil et un mail qui ne dit
que des choses mesurées — pas une phrase de vendeur.

Aucune dépendance à installer : bibliothèque standard Python uniquement.

---

## Démarrage en trois minutes (hors ligne, sans clé API)

```bash
python3 -m tools.prospect chercher --demo
python3 -m tools.prospect auditer --demo
python3 -m tools.prospect maquette
python3 -m http.server -d tools/prospect/out/maquettes 8080           # allez voir les maquettes
python3 -m tools.prospect exporter --format appels                    # la feuille d'appel
```

Ces cinq commandes ne s'adressent à personne : elles tournent telles quelles,
sans clé API et sans configuration.

Pour aller jusqu'au mail, il faut d'abord vous déclarer :

```bash
cp tools/prospect/prospect.config.example.json tools/prospect/prospect.config.json
$EDITOR tools/prospect/prospect.config.json      # ← à faire vraiment, voir ci-dessous
python3 -m tools.prospect rediger --afficher
```

**La copie de l'exemple ne suffit pas** : `rediger` la refuse, exprès. Le fichier
livré comme le fichier d'exemple sont truffés de trous (`A_REMPLIR`,
`vous@votredomaine.fr`, `VOTRE-PAGE-DE-RESERVATION`), et un trou n'est pas une
valeur. Tant qu'il en reste un, `rediger`, `relancer` et les exports qui
fabriquent des messages (`json`, `eml`) s'arrêtent en listant les champs
fautifs, sans rien écrire. Ce n'est pas une panne, c'est le verrou : un mail
signé « A_REMPLIR » vaut une plainte, pas un rendez-vous.

Sont exigés : `identite.nom`, `identite.societe`, `identite.email`,
`identite.telephone`, `identite.adresse_postale` et `rdv.lien`. Deux champs
facultatifs — `identite.site` (repris dans la signature) et `offre.prix` (repris
dans la relance 2) — peuvent rester **vides**, jamais à l'état de gabarit.

Le mode `--demo` travaille sur neuf commerces fictifs et quatre faux sites
servis localement : un site de 2011 en tableaux, un Wix daté, une boulangerie
bien faite, et un site que l'audit n'arrive pas à lire (celui-là finit en
`non_auditable`, et personne ne le démarche). C'est le meilleur moyen de
comprendre la chaîne avant de dépenser un centime d'API.

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

python3 -m tools.prospect exporter --format mail --avec-email   # → brouillons dans Mail.app

# … vous relisez et vous envoyez à la main, puis :
python3 -m tools.prospect suivi                         # ← le tableau de bord
python3 -m tools.prospect marquer envoye --tous         # (ou en un clic dans le suivi)
python3 -m tools.prospect relancer --rang 1             # J+4
python3 -m tools.prospect relancer --rang 2             # J+9
python3 -m tools.prospect marquer repondu <place_id>    # il a répondu : on le laisse tranquille
```

### `suivi` : le tableau de bord

`python3 -m tools.prospect suivi` ouvre une page locale (127.0.0.1 uniquement, les
coordonnées ne sortent pas de la machine) qui montre, pour chaque prospect : son
verdict, son canal — e-mail ou téléphone —, son statut, le nombre de jours depuis
l'envoi, et si une relance est due. Trois boutons par ligne : **Envoyé**,
**A répondu**, **STOP**. Ils écrivent dans la même base et suivent les mêmes règles
que la commande `marquer`.

Les filtres qui servent vraiment : **À envoyer**, **Relance due** et **À appeler** —
ce dernier isole les prospects sans adresse e-mail, qui sont presque tous des
`absent`, c'est-à-dire les meilleurs.

Un « STOP » retire **toutes** les adresses connues du prospect, pas seulement celle
qui a servi : sinon il resterait joignable par les autres.

### `marquer` : le maillon qu'on oublie

Le code n'envoie rien, donc il ne peut pas savoir tout seul qu'un mail est
parti. C'est `marquer envoye` qui le lui dit, et c'est de là que découle tout le
reste : le quota journalier se compte sur la date d'envoi, et `relancer` ne
regarde que les mails passés en `envoye` depuis plus de `delai_relance_1_jours`
(4) ou `delai_relance_2_jours` (9). Sans ce coup de tampon, la séquence J+4 /
J+9 n'existe pas : `relancer` répondra éternellement « 0 relance(s)
préparée(s) », et vous chercherez le bug pendant une heure.

Le geste : vous déposez les brouillons dans Gmail, vous les envoyez, puis
`marquer envoye --tous`. Et dès qu'un prospect répond, `marquer repondu <place_id>` —
il sort définitivement de la file de relance.

---

## Les cinq verdicts

L'audit range chaque établissement dans une case, et une seule. Trois cases
mènent à une sollicitation, deux mènent au silence.

| Verdict | Ce qu'on a trouvé | Ce qu'on envoie |
|---|---|---|
| `absent` | Aucun site, ou seulement une page Facebook | **Mail A** — « vos 428 avis ne mènent nulle part » |
| `obsolete` | Un site, sous le seuil de l'audit | **Mail B** — les défauts mesurés, chiffrés |
| `injoignable` | Le lien de la fiche renvoie une erreur nette (404, 410, domaine mort) | **Mail I** — l'erreur constatée et sa date, rien sur une page qu'on n'a pas vue |
| `correct` | Site au-dessus du seuil | **Rien.** On ne démarche pas quelqu'un qui n'a pas de problème. |
| `non_auditable` | Le site existe peut-être, mais l'audit n'a rien pu lire (403, 429, 5xx, délai dépassé, erreur TLS, anti-bot) | **Rien non plus.** On ne sait rien de vérifiable : pas de mail, pas de maquette, pas d'appel. |

La différence entre `injoignable` et `non_auditable` est toute la prudence de
l'outil : dans le premier cas on a une erreur qu'on peut montrer au commerçant
et qu'il constatera lui-même ; dans le second on a un mur, qui vient peut-être
de notre côté. Un mur ne se reproche à personne.

Ces deux verdicts silencieux valent pour **tous** les canaux : le mail, la
maquette et la feuille d'appel les écartent chacun de leur côté. Un
`exporter --format appels` qui compte moins de lignes que la base, c'est normal
— il vous dit combien il a mis de côté et pourquoi.

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
honnête d'une usurpation d'identité. Les données structurées de la page
décrivent la maquette, jamais l'établissement : pas question qu'un moteur
indexe notre démo comme le site officiel du commerce.

### Les photos ne sont pas à nous

Sans `--photos`, la maquette n'affiche aucune image, et le mail ne dit pas un
mot de « vos photos ». C'est le réglage par défaut, et le plus sûr.

Avec `--photos`, on télécharge les images de la fiche Google et on les
réhéberge. Deux choses à savoir avant de le faire :

- **Ça coûte.** Chaque photo est un appel facturé à la Places API, en plus de
  la recherche. Sur une grosse zone, ça se voit.
- **Elles appartiennent à quelqu'un.** Ce sont pour l'essentiel des clichés
  déposés par des clients. La licence de la Places API impose d'afficher
  l'auteur **partout où l'image est montrée** : la normalisation conserve donc
  les attributions à côté du nom du fichier, `places.credit_photo()` fabrique
  la ligne de crédit (« Photo : Prénom Nom via Google »), et cette ligne doit
  apparaître sous chaque photo de la maquette. Une image reprise sans son
  auteur, c'est la seule chose de cette chaîne qui puisse valoir un courrier
  d'avocat — pas le mail.

Si vous ne voulez pas gérer ça, restez sans `--photos` : la maquette tient
très bien debout sur les horaires, la note et le bouton d'appel.

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
- **Démarcher un site correct.** Ni par mail, ni par la feuille d'appel : le
  verdict `correct` sort de tous les canaux.
- **Écrire à qui on n'a pas pu lire.** Verdict `non_auditable` : pas de mail,
  pas de maquette, pas d'appel. Un blocage n'est pas un défaut.
- **Signer un mail avec un champ resté à `A_REMPLIR`.** Le verrou couvre aussi
  les deux champs facultatifs recopiés dans le corps : le site en signature, le
  prix en relance 2.
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
| `rediger` | Rédige les brouillons (`--creneaux`, `--afficher`) — **exige la configuration d'expédition** |
| `marquer` | `marquer envoye --tous` après le dépôt dans Gmail, `marquer repondu <place_id>` quand ça répond. **Sans elle, `relancer` ne trouve jamais rien.** |
| `relancer` | Prépare les relances J+4 / J+9 (`--rang`) — **exige la configuration d'expédition** |
| `exporter` | `csv` (dump) · `appels` (feuille d'appel) — sans configuration ; `json` (Gmail) · `eml` — **exigent la configuration d'expédition**, ce sont eux qui portent votre identité |
| `liste` | La base, triée par priorité |
| `stop` | Exclut définitivement une adresse ou un domaine |
| `pipeline` | Tout d'un coup (`chercher` → `auditer` → `enrichir` → `maquette` → `rediger`) |

`chercher`, `auditer`, `maquette`, `liste`, `stop` et `exporter --format csv|appels`
n'écrivent à personne : ils tournent sans que vous vous soyez déclaré.

Tout vit dans `tools/prospect/out/prospection.sqlite3` — les maquettes dans `tools/prospect/out/maquettes/`,
les exports dans `tools/prospect/out/exports/`. Rien de tout ça n'est versionné.

## Le pavage

`searchNearby` plafonne à 20 résultats. Un quartier dense en contient bien plus.
`--pavage 800` découpe la zone en cercles de 800 m et balaie en damier — plus
lent, plus cher en appels d'API, mais exhaustif.

```bash
python3 -m tools.prospect chercher --zone "Lyon 3e" --pavage 800
```
