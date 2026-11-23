# Instructions de contribution

Le profil actif est `firmware/WindCalmThreadOTA`. L'application UART/Matter est
partagée via `firmware/WindCalmController/Application.h` et `Application.cpp`.

L'utilisateur demande des tests de non-régression après chaque modification.
Exécuter les suites hôte du README, les tests OTA, les contrôles de release et
le contrôle de publication. Pour une modification du firmware ou de sa chaîne
de build, compiler et vérifier aussi le profil ESP32-C6. Rapporter les suites
ignorées et distinguer compilation, simulation et essai matériel.

Préserver les endpoints 1/2/3, les partitions et les identifiants NVS lors d'une
mise à jour compatible. Le contrôleur est détenu par la tâche Arduino ; les
callbacks CHIP utilisent des mailboxes protégées et la supervision lit des
copies atomiques. Conserver les budgets UART/USB, délais, gardes de versions,
autorisations CASE/Administer et contrôles d'intégrité OTA.

Ne pas reconstruire une version déjà distribuée avec d'autres octets. Garder
les archives gelées sous `build/`, les SDK sous `.local/` et les accès, identités,
sauvegardes et captures sous `private/` ; aucun de ces dossiers ne va dans Git.
Une compilation CI ne constitue pas une autorisation d'installer un firmware.

## Reprise du développement

État au 23 novembre 2026 : la release 40006 / `4.0.6-thread-ota` est installée
par OTA sur l'unité du mainteneur et ses archives sont gelées. Elle corrige la
sélection des vitesses 1 et 4 depuis HA ; le reste du comportement est celui de 40005.

Couplage FanControl du SDK. Le serveur ESP-Matter dérive lui-même
`SpeedSetting = ceil(6 × PercentSetting / 100)` et
`PercentSetting = floor(SpeedSetting × 100 / 6)`, puis rappelle le callback
applicatif pour chaque écriture dérivée. HA envoie 17 et 67 pour les crans 1 et
4. `NativeFan` ignore donc l'écho dérivé d'une écriture de pourcentage, et la
publication utilise `Controller::reportedPercentFor` (0, 16, 33, 50, 66, 83,
100), seules valeurs stables. Ne pas publier `percentFor` ni retirer
`forgetDerivedSpeed`. `tests/test_fan_speed_mapping.py` modélise ce couplage.

Un faux SDK écrit pour un test ne prouve que ce qu'il modélise : avant de
conclure sur un comportement à la frontière Matter, lire les sources du SDK
épinglé (`managed_components/espressif__esp_matter` après un build) et
modéliser les écritures dérivées et leurs callbacks.

Préparer une release, dans cet ordre :

1. Changer ensemble `version.cmake`, `-mi` dans `sdkconfig.defaults`,
   `BASE_VERSION`/`TARGET_VERSION` de `tools/ha_ota.py`, la version simulée
   dans `tests/test_ha_health.py` et les documents qui nomment la version.
2. `python3 tools/thread_ota.py build`, `verify`, puis toutes les suites.
3. Geler l'archive avant toute autre modification : copier les fichiers des
   sections `sources` et `sdk_inputs` du manifeste sous `sources/` et
   `sdk-inputs/` de `build/WindCalmThreadOTA-<version>/`, puis exécuter
   `tools/verify_release_archive.py`. Modifier ensuite un fichier manifesté,
   y compris un README du profil, fait échouer `thread_ota.py verify`.
4. Suivre `home-assistant/MAINTENANCE.txt` : preflight, status, import-image,
   check-update, watch-update, une seule demande install-update, puis
   diagnostics et monitor-health. Consigner l'essai dans `VERIFICATION.txt`.

Si l'hôte HA ne répond plus à l'adresse de `private/access/ha_ssh.json`, son
adresse a pu changer. Ne mettre à jour l'accès qu'après avoir constaté que la
clé d'hôte présentée est identique à celle de `known_hosts` ; ne jamais
accepter une clé différente.

Pistes relevées en revue, non traitées :

- La validation d'une image après OTA n'exige pas le rattachement Thread : un
  firmware qui démarre sans radio fonctionnelle serait confirmé.
- Ni Secure Boot, ni chiffrement flash, ni image signée : l'authenticité d'une
  OTA repose sur les droits de la fabric Matter.
- `docs/HARDWARE.md` ne mentionne ni fusible ni isolation pour l'alimentation secteur.
- `TuyaProtocol.h` est dupliqué entre `WindCalmController` et `WindCalmProtocolTest`.
