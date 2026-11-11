# Wind Calm · profil Matter OTA sur Thread

Ce profil ESP-IDF compile l'application Arduino partagée avec deux slots,
requestor OTA Matter et vérification des images. Endpoints lumière 1,
ventilateur 2 et bips 3 stables ; clusters requestor/provider client au root 0.
Wi-Fi retiré, Thread IPv6/SRP/DNS et BLE de commissioning conservés.

Version de référence : **40005 / `4.0.5-thread-ota`**. L'application est partagée
via `Application.h` et `Application.cpp`. Le contrat de diagnostic append-only
est décrit dans [HEALTH.txt](HEALTH.txt). La portée des validations figure dans
[VERIFICATION.txt](../../VERIFICATION.txt), et les limites de qualification dans
[PRODUCTION.txt](../../PRODUCTION.txt).

## Compiler et vérifier

Prérequis : environnement dédié `.local/dedicated/`, ESP-IDF 5.5.5 au commit
`b774170ff46c393eeb5e495ea37936038d3f4f4f`, Arduino-ESP32 3.3.12 et composants
ESP-Matter 1.5.1 verrouillés par `dependencies.lock`. Le helper contrôle le
commit IDF et la configuration avant compilation.

Depuis un clone neuf sur Linux/macOS, installer Python 3.10+ et les prérequis
indiqués dans le [guide de développement](../../docs/DEVELOPMENT.md#compiler-le-profil-actif), puis
exécuter le bootstrap ci-dessous. Il utilise CMake 3.30.2, Ninja 1.12.1, ROM ELF
20241011 et GCC Arduino 14.2.0_20260121. Les copies SDK restent hors Git.
Les correctifs locaux sont idempotents et refusent une source inattendue.

```sh
python3 tools/bootstrap_sdk.py
python3 tools/thread_ota.py build
python3 tools/thread_ota.py verify
python3 firmware/WindCalmThreadOTA/tests/test_ota.py
python3 firmware/WindCalmThreadOTA/tests/test_release.py
```

Sortie `build/WindCalmThreadOTA-<version>/` : configuration, manifeste,
application `.bin` et conteneur Matter `.ota`. Archiver séparément les fichiers
exacts des sections sources et sdk_inputs du manifeste avant distribution.
Les copies gelées se vérifient indépendamment du dépôt courant :

```sh
python3 tools/verify_release_archive.py build/WindCalmThreadOTA-40005
```

`thread_ota.py verify` exige que les entrées du dépôt/SDK courant correspondent
au manifeste, y compris la documentation de ce profil. Pour contrôler une release
après une modification documentaire, utiliser `verify_release_archive.py` sur
les copies gelées. Ce contrôle ne prouve ni signature de l'éditeur ni essai
matériel. Préparer une nouvelle version pour le prochain build destiné à
la distribution ; ne pas reconstruire une version déjà distribuée.

`--build-dir` reste
dans la sortie de release ; archives `base-*`, sources archivées, profils
étrangers, anciennes versions et chemins extérieurs sont refusés avant mutation.
Le helper ne propose aucune action de flash, USB ou réseau.

## Contrat de release

`version.cmake` définit numéro/texte. Pour 40005 : numéro 40005, texte
`4.0.5-thread-ota`, minimum applicable 40004 dans
`CONFIG_CHIP_OTA_IMAGE_EXTRA_ARGS`. Les gardes base/cible du client HA suivent
la même paire. Ne pas renommer un fichier pour en changer la version, modifier
seulement l'en-tête ou redistribuer d'autres octets sous le même numéro.

Identité : VID `0xFFF1`, PID `0x8000`, projet `windcalm-thread-ota`. C'est une
identité Matter de développement. La réception contrôle le provider natif et
la cible négociée ; elle ne se contente pas du cache HA.

Le `.ota` contient application et en-tête Matter, sans bootloader, table ni NVS.
Le SHA du client porte sur le conteneur complet ; le manifeste garde séparément
celui du payload `.bin`. Les archives des releases restent hors Git et distinctes
de toute reconstruction suivante.

## Vérification de réception

`VerifiedOta` enveloppe le processeur ESP32 natif sans remplacer CASE/BDX :

- identité native, version supérieure/égale à la cible négociée, plage applicable ;
- longueur exacte et capacité du slot inactif ;
- SHA du payload reçu, erreurs persistantes jusqu'à Abort/nouveau Prepare ;
- annulation différée bornée après rejet et progression propre à chaque tentative ;
- descripteur ESP32-C6, version et nom de projet en flash ;
- SHA relu du slot sur exactement les octets reçus, avec tampon fixe, avant Apply.

Les tests injectent corruption, réception partielle et travaux SDK refusés.
L'intégrité n'est pas une signature indépendante de l'éditeur ; voir
[SECURITY.txt](../../SECURITY.txt).

## Santé et récupération

Rollback requis dans le bootloader effectif et l'application. Le hook Arduino
`verifyRollbackLater()` reporte la confirmation après setup. Le contrôle local
exige endpoints/clusters attendus, progrès continu et intégrité du heap avant
VALID, sans dépendre de HA, Thread ou d'un état mécanique supposé.
La confirmation exige dix secondes de progression
loop et au moins deux probes exécutées par la tâche Matter, sans connexion HA.
NotifyUpdateApplied n'est envoyé qu'après santé locale et confirmation native
de version, avec remise en file bornée si nécessaire.

Un superviseur permanent agit après confirmation : progrès loop,
travail Matter et garde contre resets répétés. L'interface de maintenance est
authentifiée/autorisée et expose seulement une santé bornée. Contrat et commandes :
[MAINTENANCE.txt](../../home-assistant/MAINTENANCE.txt). Ces mécanismes doivent
encore être testés avec blocage contrôlé et pertes HA/Thread sur le matériel.

Politique locale : grâce startup 60 s ; stagnation loop 15 s ou Matter
45 s ; pendant OTA, limites étendues à 60/180 s. Après trois récupérations
rapprochées, holdoff 10 min ; budget remis à zéro après 10 min de progrès sain.
Le watchdog de la tâche superviseur est configuré à 15 s avec panic ; les
seuils de politique ne sont donc pas un délai total de reboot garanti.
Le budget est gardé en RTC, sans écriture NVS répétée. Un probe Matter est
proposé toutes les cinq secondes, avec au plus un travail en attente.
Connectivité HA/Thread et état des actionneurs ne sont pas les entrées de cette
politique : c'est le progrès local qui est surveillé. Le watchdog et le budget
ne remplacent pas un test matériel du blocage, de la reprise et des resets.

Avant confirmation, crash/reset peut revenir à l'image précédente. Après VALID,
une indisponibilité tardive ne fait pas automatiquement rollback. Les changements
NVS ne sont pas restaurés par un retour de slot. Une panne matérielle/flash
peut encore exiger une intervention physique.

## Partitions et USB

| Partition | Offset | Taille |
| --- | ---: | ---: |
| NVS | `0x9000` | `0x5000` |
| otadata | `0xe000` | `0x2000` |
| app0 | `0x10000` | `0x1f0000` |
| app1 | `0x200000` | `0x1f0000` |
| coredump | `0x3f0000` | `0x10000` |

Une première migration depuis un profil à un slot nécessite une opération USB
avec sauvegarde et vérification des plages protégées. Elle ne se rejoue pas
sur une unité déjà migrée. Pour une intervention USB, lire les sélecteurs et
choisir le slot inactif réel, en préservant l'image VALID, la table, le bootloader
et NVS. Voir [USB-RECOVERY.txt](USB-RECOVERY.txt) ; ne jamais reprendre un offset
ou un sélecteur sans lecture actuelle du périphérique.

## Home Assistant

Le flux validé passe par SSH à clé puis API internes, sans navigateur, paquet
distant ou port LAN ajouté. Runtime vérifié : `matter-server/1.4.0
(matter.js/0.17.9)`, schéma 13/minimum 11. Vérifier le runtime de l'installation
avant d'utiliser ce contrat API. Nœud/entités et clés sont configurés localement.

Validation locale, import à chaud, contrôle du candidat, installation et suivi
sont des étapes séparées. Le ticket d'import est à usage unique, lié à la même
IP et borné dans le temps. Le reçu lie image contrôlée et import réussi, mais
le provider ne retourne pas une relecture du digest de son stockage. Une
mutation expirée reste incertaine ; pas de retry automatique.

Commandes et vérifications après boot dans
[MAINTENANCE.txt](../../home-assistant/MAINTENANCE.txt). `status` relit version,
chaîne et VID/PID ; `get_node` seul est un cache. Le suivi OTA se termine après
confirmation de cible ; ce n'est pas un essai d'endurance.

Sources : [SOURCES.md](../../SOURCES.md),
[OTA HA](https://www.home-assistant.io/integrations/matter/#matter-ota-device-updates),
[API Matter Server 1.4.0](https://github.com/matter-js/matterjs-server/blob/v1.4.0/docs/websockets_api.md).
