# Développement et compilation

Les tests hôte demandent Python 3.10 ou supérieur, Git et un compilateur C++20
avec AddressSanitizer/UndefinedBehaviorSanitizer (`clang++` ou `g++`). Aucun
accès HA, USB, token ou SDK téléchargé n'est nécessaire pour les tests portables.

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 tools/test_ota_migration_plan.py
python3 firmware/WindCalmThreadOTA/tests/test_ota.py
python3 firmware/WindCalmThreadOTA/tests/test_release.py
python3 .github/scripts/test_publication_guard.py
python3 .github/scripts/check_public_tree.py
```

Les tests dépendant du SDK privé ou des archives matérielles peuvent être
signalés comme ignorés dans un clone neuf. Les tests de protocole, de politiques,
de limites SDK simulées et des clients SSH/API restent exécutables hors matériel.
La [CI hôte](../.github/workflows/host-tests.yml) exécute ces contrôles sur Linux ; elle
ne flashe aucun appareil et ne promet pas une validation radio.

## Compiler le profil actif

La chaîne épinglée est ESP-IDF 5.5.5, Arduino-ESP32 3.3.12 et ESP-Matter 1.5.1.
Les composants transitifs sont verrouillés dans `dependencies.lock`.
Le bootstrap installe le SDK isolé sous `.local/dedicated/` sur Linux
(x86_64/aarch64) et macOS (Intel/Apple Silicon), avec Python 3.10 ou supérieur.
Il vérifie les SHA-256 des archives Arduino et du compilateur, le commit IDF
et ses sous-modules. Les outils IDF sont épinglés : CMake 3.30.2, Ninja 1.12.1,
ROM ELF 20241011 ; compilateur Arduino GCC 14.2.0_20260121. Il refuse de
remplacer un SDK de version différente et ne touche pas aux installations globales.

Sur Ubuntu, installer d'abord les prérequis du
[guide Espressif 5.5.5](https://docs.espressif.com/projects/esp-idf/en/v5.5.5/esp32c6/get-started/linux-macos-setup.html).
Sur macOS, installer les outils de développement Xcode, Git et Python 3.10+.
Prévoir plusieurs Go libres et un accès aux dépôts Espressif, GitHub et PyPI.
La [CI de compilation](../.github/workflows/firmware-build.yml) exécute ce parcours
sur un clone Linux neuf, sans cache SDK ni accès au matériel.
Les dépendances Python suivent les contraintes IDF 5.5 ; leurs versions peuvent
évoluer. La procédure assure une chaîne de compilation définie, pas des binaires
identiques octet pour octet entre deux OS ou dates de compilation.

```sh
python3 tools/bootstrap_sdk.py
python3 tools/thread_ota.py build --live
python3 tools/thread_ota.py verify
```

Chaque release est isolée dans `build/WindCalmThreadOTA-<version>/` avec le
manifeste, le `.bin` applicatif et le conteneur Matter `.ota`. Archiver également
les fichiers exacts correspondant aux empreintes `sources` et `sdk_inputs` du
manifeste avant distribution ; ces copies sont présentes pour les releases 40004 et 40005.
La vérification contrôle l'identité, les deux versions, la plage
applicable, la taille du slot, le descripteur ESP et les empreintes. Les archives
des versions précédentes sont préservées. Ne pas relancer un build d'une version
déjà distribuée avec un contenu différent : préparer une nouvelle version.
Un manifeste compilé prouve une
préparation ; seule une lecture effective du XIAO prouve la version installée.

Pour vérifier les copies gelées de 40005 après les mises à jour de documentation,
utiliser `python3 tools/verify_release_archive.py build/WindCalmThreadOTA-40005`.
`thread_ota.py verify` compare les sources/SDK courants au manifeste ; ces deux
contrôles ont des objets distincts. Le dépôt actuel documente les essais réalisés
après compilation et ne doit pas écraser les releases déjà distribuées.


## Organisation du code

| Chemin | Usage |
| --- | --- |
| `firmware/WindCalmThreadOTA` | Profil de livraison ESP32-C6, OTA et maintenance |
| `firmware/WindCalmController` | Application partagée, UART et endpoints Matter |
| `tools/ha_ota.py` | Maintenance HA par SSH/API |
| `tests/` | Régressions hôte et interfaces SDK simulées |
| `home-assistant/` | Configuration, carte et procédures HA |

Les profils SDK, simulateur et protocole servent aux tests ou à l'étude.
Ils ne constituent pas une procédure de flash d'une unité OTA. Les commandes
ci-dessus s'exécutent depuis la racine du dépôt.

Voir [Contribuer](../CONTRIBUTING.md), [Sources](../SOURCES.md),
[Licences tierces](../LICENSES.txt) et [Sécurité](../SECURITY.md).
