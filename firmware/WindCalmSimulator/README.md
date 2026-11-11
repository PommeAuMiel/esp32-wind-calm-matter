# Simulateur de ventilateur et lumière

Ce profil de test ne commande pas un ventilateur réel. Pour un appareil,
utiliser [WindCalmThreadOTA](../WindCalmThreadOTA/README.md) ; ne pas remplacer
le firmware d'une unité OTA par ce simulateur.

Deux endpoints Matter sur Thread, BLE pour appairage : lumière marche/arrêt
(endpoint 1, LED intégrée), ventilateur (endpoint 2, backend uniquement en RAM).
Aucun UART applicatif, GPIO moteur, PWM moteur ni protocole Tuya n'est activé.
La LED reste le seul actionneur physique. La carte reste uniquement sur USB.

## Compiler et tester

```sh
python3 tools/project.py build --sketch WindCalmSimulator
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Ces commandes ne téléversent rien. Un éventuel essai sur une carte dédiée doit
rester isolé du contrôleur du ventilateur.

## Modèle d'état et profil

`Simulation.h` contient un modèle indépendant de Matter et le backend simulé.
Le sketch adapte les callbacks Matter et les rapports vers Home Assistant.
Le modèle comporte six niveaux ; les pourcentages sont des représentations
de consignes, pas des mesures du régime physique.

| Consigne Matter | Niveau simulé | Pourcentage confirmé simulé |
| --- | --- | --- |
| 0 | 0, arrêté | 0 |
| 1–17 | 1 | 17 |
| 18–33 | 2 | 33 |
| 34–50 | 3 | 50 |
| 51–67 | 4 | 67 |
| 68–83 | 5 | 83 |
| 84–100 | 6 | 100 |

Changer `SpeedProfile.percent` pour un autre profil strictement croissant
terminant à 100. Ces valeurs sont des représentations de consignes, pas des
mesures de régime moteur. La dernière consigne non nulle sert à rallumer le
ventilateur simulé ; le démarrage initialise cependant moteur et lumière à OFF.
Il n'y a pas de sauvegarde d'une ancienne consigne moteur ni de réémission au boot.

Le simulateur confirme la dernière consigne après 150 ms. Ventilateur et lumière
ont leurs attentes indépendantes. Un changement externe remplace l'état de son
endpoint et annule une commande encore en attente pour celui-ci.

`SIM_STATE` distingue `requested_pct`, `confirmed_level`, `confirmed_pct`,
`requested_light`, `confirmed_light` et les drapeaux `*_pending`. Les compteurs
`commands`, `confirmations`, `external` permettent de vérifier l'absence de boucle.
La dernière consigne peut donc être 51 alors que le niveau confirmé est 4/67 %.
Home Assistant affiche le pourcentage représentatif du niveau confirmé.

Limite du SDK : `MatterFan` répercute déjà PercentSetting dans PercentCurrent
lors du callback. Durant les 150 ms d'attente, cet attribut peut refléter la
consigne optimiste ; après confirmation, l'adaptation publie le niveau quantifié.
L'état confirmé propre au projet est uniquement celui du backend simulé, jamais
une mesure ou un acquittement de moteur réel. Une adaptation future devra revoir
cette limite avant de présenter un état moteur physique confirmé.

Seule la séquence Off/High est annoncée : High signifie ici en marche, avec
pourcentage réglable. Auto, Smart, inversion et minuterie ne sont pas ajoutés.
La lumière expose uniquement marche/arrêt.

## Changements externes simulés

Dans le moniteur USB 115200, envoyer une ligne terminée par Entrée :

```text
ext fan 0
ext fan 3
ext fan 6
ext light 1
ext light 0
```

Cela simule une télécommande externe ; ce ne sont pas des trames capturées.
Les lignes invalides ou trop longues sont refusées. Aucun trafic série n'est
envoyé à une carte de ventilateur. Fermer le moniteur pour libérer le port avant
un téléversement ou un autre lecteur USB.

BOOT : clic court = niveau suivant (0..6), double clic = lumière, appui cinq
secondes = reset Matter volontaire. Aucun changement externe n'est programmé
automatiquement. Une remontée de confirmation ne crée pas une nouvelle commande.

## Concurrence et tests

Les callbacks courts modifient le modèle sous le verrou CHIP déjà détenu par
la tâche Matter. La boucle utilise `PlatformMgr().TryLockChipStack()` puis
`UnlockChipStack()` pour le modèle et les accès aux caches/attributs SDK ; elle
ne bloque pas en attente du verrou. Journaux USB et GPIO sont traités hors verrou.
Les publications utilisent un garde `publishing` car les setters du SDK peuvent
réinvoquer les callbacks. Les rapports identiques sont ignorés par les setters.

Les tests hôte compilent le vrai modèle C++ avec AddressSanitizer et
UndefinedBehaviorSanitizer : mapping de tous les pourcentages, valeurs invalides,
commandes rapides, confirmations différées, changements externes, indépendance
des endpoints, absence de nouvelles commandes par confirmation, rollover millis.
Ils ne simulent pas les API Matter ; la compilation ESP32 est une vérification
distincte. La reconnexion du réseau réel et le rendu HA restent des essais matériels.
