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
