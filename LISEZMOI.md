# Veille podcasts automatique

Chaîne complète : flux RSS → téléchargement des MP3 → transcription
(faster-whisper, gratuit) → **verbatim intégral** dans `verbatims/`
→ **synthèse resserrée** (le novateur et le spécifique, sans remplissage)
dans `syntheses/`. Tout tourne sur les machines de GitHub, gratuitement,
sans ordinateur allumé chez vous.

## Mise en place (une seule fois, ~30 minutes)

1. **Créer un compte** sur github.com si vous n'en avez pas.

2. **Créer un dépôt** : bouton « New repository », nom libre
   (p. ex. `veille-podcasts`), visibilité **Private**, cocher
   « Add a README », puis « Create repository ».

   > Nota : sur un dépôt privé, le quota gratuit est de 2 000 minutes
   > de calcul par mois. Un dépôt **public** bénéficie de minutes
   > illimitées — mais vos verbatims seraient alors visibles de tous.
   > À vous d'arbitrer ; pour quarante émissions, le public est plus
   > confortable.

3. **Téléverser les fichiers** de ce dossier dans le dépôt :
   « Add file → Upload files », puis glisser-déposer `pipeline.py`,
   `feeds.txt` et `LISEZMOI.md`. Valider (« Commit changes »).

4. **Créer le fichier de l'automate** (l'interface de téléversement ne
   prend pas les dossiers cachés) : « Add file → Create new file »,
   taper comme nom exactement `.github/workflows/podcasts.yml`
   (les barres obliques créent les dossiers), coller le contenu du
   fichier `podcasts.yml` fourni, valider.

5. **Renseigner vos émissions** : ouvrir `feeds.txt` dans le dépôt,
   cliquer sur le crayon, ajouter une ligne par émission au format
   `NomCourt | URL_du_flux_RSS`, valider. Les URL RSS se trouvent sur
   le site de chaque émission ou via un annuaire comme
   castos.com/tools/find-podcast-rss-feed (coller le nom de l'émission).

6. **Premier essai** : onglet « Actions » du dépôt → « Veille podcasts »
   → bouton « Run workflow ». Suivre l'exécution en direct ; au terme,
   les dossiers `verbatims/` et `syntheses/` apparaissent, classés par
   émission puis par date.

Ensuite, l'automate passe seul quatre fois par jour et ne traite que
les épisodes nouveaux.

## Où lire les résultats

Directement dans le dépôt, depuis n'importe quel navigateur (y compris
sur téléphone) : un fichier par épisode, lisible en ligne. Pour une
remise par courriel ou un dépôt dans Google Drive, c'est une extension
possible dans un second temps.

## Réglages utiles (dans `.github/workflows/podcasts.yml`)

- `MODELE_WHISPER` : `base` (rapide, qualité moindre), `small`
  (réglage actuel, bon compromis), `medium` (fidèle, trois fois plus lent).
- `MAX_EPISODES` : nombre d'épisodes traités par passage (6 par défaut,
  soit jusqu'à 24 par jour). À la première exécution, seuls les deux
  derniers épisodes de chaque flux sont pris — on n'aspire pas des années
  d'archives.
- Fréquence des passages : ligne `cron` en tête du fichier.

## Limites connues

- La transcription automatique fait des coquilles, surtout sur les noms
  propres ; suffisant pour la synthèse, à vérifier avant citation.
- La synthèse passe par GitHub Models (gratuit) ; en cas de saturation
  ponctuelle du service, le verbatim est conservé et la synthèse pourra
  être refaite (ou demandée à Claude en collant le verbatim).
- Épisodes très longs (> 3 h) : traités, mais lentement.

## En cas de panne

Onglet « Actions » : l'exécution en échec s'affiche en rouge, avec le
journal détaillé. Copier-coller le message d'erreur à Claude suffit
généralement à obtenir le correctif.

## Liaison avec Inoreader (gratuite, sans API)

- **Importer vos abonnements** : dans Inoreader, Préférences → « Exporter »
  → télécharger le fichier OPML, puis le téléverser tel quel à la racine du
  dépôt. Le script y lit automatiquement tous les flux, en plus de ceux de
  `feeds.txt`. (Attention : si votre Inoreader contient aussi des flux
  d'actualité non audio, ils seront ignorés faute de fichier MP3, mais mieux
  vaut exporter uniquement le dossier « Podcasts » si vous en avez un.)
- **Lire les synthèses dans Inoreader** : le système génère un flux RSS,
  `flux-syntheses.xml`. Dans Inoreader, « Ajouter un abonnement » et coller :
  `https://raw.githubusercontent.com/VOTRE_COMPTE/VOTRE_DEPOT/main/flux-syntheses.xml`
  Chaque synthèse arrivera comme un article ordinaire, texte intégral inclus,
  avec un lien vers le verbatim.

L'API officielle d'Inoreader exige un abonnement Pro payant pour les scripts
personnels ; les deux mécanismes ci-dessus l'évitent entièrement.
