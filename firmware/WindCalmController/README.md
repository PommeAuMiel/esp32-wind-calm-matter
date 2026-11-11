# Application UART et Matter partagée

`Application.cpp` et `Application.h` fournissent l'application du profil actif
[WindCalmThreadOTA](../WindCalmThreadOTA/README.md). Le sketch `.ino` est destiné
aux profils de développement ; il ne constitue pas une procédure de flash pour
une unité OTA.

## Fonctionnement

Le XIAO dialogue avec le contrôleur d'origine par UART Tuya à 9600 bauds.
Il ne pilote pas directement le moteur ni la puissance de la lampe.

| Fonction | Datapoint |
| --- | --- |
| Lumière | DP20 bool |
| Ventilateur marche/arrêt | DP60 bool |
| Six vitesses | DP62 value, 1 à 6 |
| Sens | DP63 enum, 0 ou 1 |
| Commandes de teinte | DP23 value, 0 / 500 / 1000 |
| Bips | DP66 bool, 1 audible / 0 muet |

Les modes de teinte ne garantissent pas une couleur physique absolue.
Voir les [limites du panneau LED](../../docs/HARDWARE.md#limite-du-panneau-led).
Le sens exige OFF confirmé pendant dix secondes, sans commande ventilateur
pendante ; ce délai ne mesure pas la rotation mécanique réelle.

Le contrôleur est détenu par la tâche Arduino. Les callbacks CHIP utilisent
une mailbox protégée, sans accès concurrent au parseur. Une commande n'est
acceptée qu'après synchronisation. Les confirmations viennent des rapports
du contrôleur ; un état différent annule la consigne sans la renvoyer.
Un timeout abandonne l'attente et demande l'état, sans rejouer la commande.
Le démarrage ne transmet aucune consigne d'actionneur.

## Diagnostic et maintenance

Les diagnostics USB sont bornés, désactivés par défaut et expirent. Commandes :
`diag on`, `diag mark repere`, `diag off`. Ils ne commandent pas les actionneurs ;
une capture manquante ne prouve pas l'absence d'activité physique. Les journaux
et données d'appairage restent privés.

Voir [le matériel](../../docs/HARDWARE.md),
[le développement et les tests](../../docs/DEVELOPMENT.md),
[la maintenance OTA](../../home-assistant/MAINTENANCE.txt) et
[le secours USB](../WindCalmThreadOTA/USB-RECOVERY.txt).
Les anciens profils Arduino ne doivent pas être téléversés sur un appareil OTA.
