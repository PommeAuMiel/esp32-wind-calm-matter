# Tests du protocole Tuya

`TuyaProtocol.h` contient le noyau C++ indépendant de Matter, Arduino, Home
Assistant et du cloud. L'application partagée l'utilise pour dialoguer avec
le contrôleur. Le sketch de ce dossier vérifie sa compilation ESP32-C6 ;
il ne configure ni UART, ni broches, ni Matter. Son téléversement est bloqué
par `tools/project.py`.

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 tools/project.py build --sketch WindCalmProtocolTest
```

## Contrat de protocole

Format : `55 AA version commande longueur[2] données checksum`. La longueur
est big endian et le checksum est la somme des octets précédents modulo 256.
Le parseur accepte au maximum 256 octets de données, soit 263 octets de trame.
Il n'alloue pas dynamiquement. `tick` doit être appelé même sans réception.
Les limites sont 250 ms entre octets et 1 000 ms depuis le début d'une trame.

Les datapoints sont entièrement validés avant application : un champ mal formé
rejette le rapport entier. Les rapports 07 n'ont pas de réponse ; les rapports
22 reçoivent un ACK23 indiquant l'acceptation locale. Cet acquittement ne prouve
pas une livraison à HA ni un effet physique sur un actionneur.

La session négocie heartbeat, produit, configuration et état initial avant les
commandes. Elle utilise une file RAM bornée. Redémarrage du contrôleur, perte
de heartbeat ou saturation invalident la session sans rejouer d'anciennes
consignes. Voir [l'application partagée](../WindCalmController/README.md) pour
les datapoints et les gardes de commandes, et [SOURCES.md](../../SOURCES.md)
pour les références Tuya/ESPHome.

## Portée des tests

Les vecteurs sont synthétiques. Les tests compilent le vrai noyau avec
AddressSanitizer/UndefinedBehaviorSanitizer : fragmentation, bruit, concaténation,
troncature, checksum, tailles, délais et rollover, données pseudo-aléatoires,
atomicité des DP, six vitesses, ACK, initialisation, état partiel, commandes
refusées, absence d'écho, redémarrage, perte de heartbeat et saturation.
Une compilation ou une simulation ne remplace pas la qualification matérielle
du [profil actif](../WindCalmThreadOTA/README.md).
