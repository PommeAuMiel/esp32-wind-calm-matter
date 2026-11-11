# Profil SDK de développement, sans OTA

Ce profil ESP-IDF/Arduino compile l'application partagée `WindCalmController`
avec ESP-IDF 5.5.5, Arduino-ESP32 3.3.12 et ESP-Matter 1.5.1. Il utilise une
partition applicative à un slot et **ne fournit pas d'OTA**.

Pour installer ou maintenir un ventilateur, utiliser le profil actif
[WindCalmThreadOTA](../WindCalmThreadOTA/README.md). Ne pas téléverser ce profil
sur un appareil déjà migré à deux slots.

Le script de développement compile uniquement et ne sélectionne aucun appareil :

```sh
python3 tools/thread_sdk.py reconfigure
python3 tools/thread_sdk.py build
python3 tools/thread_sdk.py size-components
```

Les SDK isolés sont sous `.local/dedicated/` et les sorties sous
`build/WindCalmThreadSDK/`, hors Git. Les tests et le bootstrap courant sont
présentés dans [le guide de développement](../../docs/DEVELOPMENT.md).
Voir [CONFIGURATION.txt](CONFIGURATION.txt) pour les choix de configuration et
[les sources](../../SOURCES.md) pour les adaptations locales et leurs licences.
