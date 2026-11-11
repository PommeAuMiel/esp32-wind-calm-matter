# Sources et provenance

Références revues au cours du développement, du 3 au 5 octobre 2026.
Les jeux de test du protocole sont synthétiques. Les observations matérielles
propres au projet sont résumées dans [VERIFICATION.txt](VERIFICATION.txt) ; elles
ne sont pas attribuées à un autre ventilateur ou à sa documentation.

## Protocole et matériel

- [CREATE Wind Calm ESPHome, révision consultée](https://github.com/christiaanderidder/esphome-create-wind-calm/tree/2cd3b198c45db24f19675e13d5897b4309fb8ed3) :
  référence utilisateur pour UART 9600, fonctions et datapoints. Ce dépôt décrit
  aussi d'autres modules/contrôleurs ; ses résultats ne valident pas chaque
  variante matérielle. Aucun fichier YAML ou code n'est copié dans le noyau.
- [ESPHome, traitement Tuya à la fusion de PR 6980](https://github.com/esphome/esphome/blob/10504c4d68b9808b86d51763f3b92ee0a41b96db/esphome/components/tuya/tuya.cpp) :
  lecture du comportement des rapports 07/22 et réponse 23 ; implémentation du
  protocole de ce projet originale, sans copie de la source ESPHome.
- [Protocole MCU Wi-Fi Tuya](https://developer.tuya.com/en/docs/mcu-standard-protocol/MCUSDK-wifi-base?id=Kd2bxu84567gk) :
  trames 55AA, longueur big endian, checksum et rapports synchrones. La réponse
  du XIAO représente l'acceptation locale, pas une connexion au cloud Tuya.
- [Seeed XIAO ESP32-C6](https://wiki.seeedstudio.com/xiao_esp32c6_getting_started/) :
  variante, GPIO et bootloader ROM. TX D4/GPIO22, RX D5/GPIO23 dans ce projet.

Les captures du projet identifient vitesse DP62, sens DP63, bips DP66 et teinte
DP23. Les teintes restent des modes neutres : aucune mesure Kelvin ne permet
d'attribuer une température absolue à leurs valeurs.

## SDK épinglés

- [Arduino-ESP32 3.3.12](https://github.com/espressif/arduino-esp32/tree/3.3.12),
  core principalement LGPL-2.1 ; [exemple Matter Thread](https://github.com/espressif/arduino-esp32/blob/3.3.12/libraries/Matter/examples/Commissioning/MatterCHIPoBLEThread/MatterCHIPoBLEThread.ino)
  sous Apache-2.0 pour l'ordre d'initialisation. Les endpoints et helpers Matter
  ont été lus dans cette version, sans supposer les API d'un autre SDK.
- [ESP-IDF 5.5.5](https://github.com/espressif/esp-idf/tree/b774170ff46c393eeb5e495ea37936038d3f4f4f)
  et [ESP-Matter 1.5.1](https://components.espressif.com/components/espressif/esp_matter/versions/1.5.1/readme).
  Les dépendances exactes sont dans `dependencies.lock` du profil.
- [Réduction de taille ESP-IDF](https://docs.espressif.com/projects/esp-idf/en/v5.5/esp32c6/api-guides/performance/size.html)
  et [entropie matérielle](https://docs.espressif.com/projects/esp-idf/en/v5.5/esp32c6/api-reference/system/random.html).
- [HAL modem-clock C6](https://github.com/espressif/esp-idf/blob/v5.5.5/components/hal/esp32c6/modem_clock_hal.c) :
  adaptation locale de la transition du compteur vers zéro, sans retrait de la
  garde de sous-débordement. Tests hôte sur la fonction réelle avec sanitizers.
- [Lib-builder Espressif, référence historique ClosureControl](https://github.com/espressif/esp32-arduino-lib-builder/tree/6671d0bd65cdb9d4cc1001b759e8610de945a8d5).
  La copie initiale a été remplacée par une adaptation originale MIT qui délègue
  à l'opérateur public du SDK. Elle est testée avec `std::optional` en C++20.
  Voir [LICENSES.txt](LICENSES.txt) ; l'ancien contenu n'est pas distribué ici.

Les ajustements de Kconfig/CMake/HAL s'appliquent aux copies dédiées sous
`.local/`. Wi-Fi est retiré ; protections de pile, assertions, buffers radio,
crypto et BLE de commissioning restent présents. Les wrappers Arduino de
libération BLE et les hooks IPv6/DNS-SRP sont conservés.

## Matter, OTA et Home Assistant

- [Home Assistant Matter et OTA](https://www.home-assistant.io/integrations/matter/#matter-ota-device-updates).
- [API WebSocket Matter Server 1.4.0](https://github.com/matter-js/matterjs-server/blob/v1.4.0/docs/websockets_api.md).
- [Import HTTP OTA](https://github.com/matter-js/matterjs-server/blob/v1.4.0/packages/matter-server/src/server/OtaUploadHandler.ts)
  et [registre d'import](https://github.com/matter-js/matterjs-server/blob/v1.4.0/packages/ws-controller/src/controller/OtaUploadRegistry.ts).
- [Commandes du contrôleur](https://github.com/matter-js/matterjs-server/blob/v1.4.0/packages/ws-controller/src/controller/ControllerCommandHandler.ts) :
  champ minimum applicable du candidat exposé comme zéro par cette version.
  Le client exige toujours le minimum réel de l'image et son reçu d'import.
- [Sécurité ESP-Matter](https://docs.espressif.com/projects/esp-matter/en/latest/esp32c6/security.html) :
  attestation, Secure Boot et protection du stockage sont des mécanismes distincts.

Les interfaces réellement compilées de l'OTA, Actions, accès et diagnostics
sont celles du SDK verrouillé. Le code de téléchargement contrôle longueur,
SHA et contenu flash ; la confirmation locale ne dépend pas de la présence HA.

Pour les cartes HA : [Tile](https://www.home-assistant.io/dashboards/tile/),
[Features](https://www.home-assistant.io/dashboards/features/) et
[Conditional](https://www.home-assistant.io/dashboards/conditional/).
Les exemples publics contiennent des entités à remplacer, sans identifiants
d'une installation particulière.
