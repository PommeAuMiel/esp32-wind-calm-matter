# Contribuer

Le profil de livraison est `firmware/WindCalmThreadOTA`. Les autres sketches
documentent les étapes antérieures ; conserver leurs tests sans les proposer
comme méthode de flash pour un appareil OTA.

## Valider une modification

Exécuter les [commandes de tests](docs/DEVELOPMENT.md#développement-et-compilation) du guide de développement. Les suites C++ compilent le
code du projet avec les sanitizers ; les interfaces SDK sont simulées lorsque
le SDK complet n'est pas nécessaire. Une suite ignorée faute de SDK ou d'archive
privée doit être indiquée dans la description du changement. La CI de compilation installe le SDK épinglé depuis un clone neuf puis
compile et vérifie le profil ESP32-C6. La CI hôte exécute les régressions
portables sur Python 3.10 et 3.12. Une compilation et un essai matériel sont
des validations distinctes.

L'application partagée expose `Application.h` : `begin`, `tick`, `snapshot`
et `endpointsMatch`. `snapshot` est réservé à la tâche Arduino propriétaire du
contrôleur ; la supervision inter-tâches utilise des copies atomiques.
Les callbacks Matter déposent seulement des demandes dans les mailboxes sous
verrou CHIP. Préserver ce modèle, les budgets UART/USB et les endpoints 1/2/3.

Une modification OTA doit préserver les contrôles d'intégrité et de version,
les limites de taille, le reçu d'import et les protections des archives. Changer
ensemble `version.cmake`, le minimum applicable et le contrat du client HA.
Ne pas réutiliser une version publiée avec un contenu différent.

## Régression et preuves

Ajouter un test lorsqu'il couvre un risque réel : trame invalide, expiration,
concurrence, publication échouée, commande refusée ou récupération bornée. Éviter
les tests qui recopient seulement l'implémentation. Les tests hôte ne contactent
pas HA et n'ouvrent aucun port série.

Pour un essai matériel, conserver la version, l'empreinte de l'image, le protocole
d'essai et le résultat. Publier un résumé anonymisé ; garder les captures,
identifiants USB, adresses du réseau, PIN, données NVS et fabric dans `private/`.
Un résultat inconnu après timeout doit rester inconnu jusqu'à vérification.

## Revue avant publication

```sh
python3 .github/scripts/check_public_tree.py
git diff --cached --check
git diff --cached --stat
```

Relire les fichiers staged. `.gitignore` n'enlève pas un secret déjà ajouté à
l'index et ne protège pas une pièce jointe externe. Ne pas forcer l'ajout de
`private/`, `.local/`, `build/`, sauvegardes flash ou identités d'accès. Les
empreintes de firmware et les révisions publiques de SDK sont des métadonnées
d'intégrité ; elles peuvent être publiées.

Les seules images autorisées par le contrôle sont les copies revues sous
`docs/images/`, identifiées par leur SHA-256 dans `check_public_tree.py`.
Avant tout ajout ou remplacement de photo : vérifier son contenu, retirer les
métadonnées EXIF/GPS/commentaires et masquer les identifiants propres à un appareil.
Réviser ensuite explicitement la liste d'empreintes. L'exception ne désactive
ni la limite de taille ni les motifs de secrets ; les autres binaires restent
refusés. Ne pas présenter une photo de prototype comme un montage final.

Toute redistribution doit traiter les points de [LICENSES.txt](LICENSES.txt)
et garder les notices propres aux sources tierces. Ne pas attribuer la licence
du dépôt à des dépendances externes. Pour un rapport de sécurité, décrire le
risque sans publier de clés ni de PIN ; utiliser un canal privé du mainteneur
lorsque le dépôt sera publié.
