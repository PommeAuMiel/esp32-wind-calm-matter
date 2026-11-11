# Première installation sur un XIAO ESP32-C6

**But :** préparer un XIAO neuf, l'ajouter à Home Assistant, puis le raccorder au
ventilateur. L'USB sert au premier flash ; les mises à jour suivantes utilisent
l'[OTA](../home-assistant/MAINTENANCE.txt).

Ce guide est pour **macOS ou Ubuntu 24.04**, avec un **XIAO ESP32-C6 de 4 Mo dédié
à cette installation**, sans appairage à conserver. Il efface entièrement cette
carte, y compris un éventuel programme d'usine. **Ne pas l'appliquer au XIAO
qui pilote déjà votre ventilateur.** Pour celui-ci : OTA ou
[récupération USB sans effacement complet](../firmware/WindCalmThreadOTA/USB-RECOVERY.txt).

## 1. Préparer l'ordinateur et Home Assistant

Il faut Git, les outils de compilation et Python 3.10 ou supérieur ; Python
3.12 est recommandé. Prévoir plusieurs Go libres, Internet et un câble USB de
**données**, pas seulement de charge. La première compilation peut prendre
une demi-heure ou davantage.

Sur **macOS**, installer les outils Xcode si nécessaire :

```sh
xcode-select --install
```

Si Python 3.12 manque et que [Homebrew](https://brew.sh/) est installé :

```sh
brew install python@3.12
```

Puis sélectionner cet interpréteur pour les commandes du guide :

```sh
XIAO_PYTHON=python3.12
"$XIAO_PYTHON" --version
```

Sur **Ubuntu 24.04**, utiliser à la place :

```sh
sudo apt-get update
sudo apt-get install -y git python3 python3-venv python3-dev build-essential flex bison gperf libffi-dev libssl-dev libusb-1.0-0
XIAO_PYTHON=python3
"$XIAO_PYTHON" --version
```

Garder **le même terminal** pour toute la procédure. Si les outils Xcode sont
installés, ne pas relancer leur installation. Aucun compte Tuya n'est nécessaire.

Dans HA, préparer les intégrations **Matter** et **Thread**, un routeur de
bordure Thread et l'application mobile **Home Assistant Companion**. Le téléphone
servant à l'appairage doit avoir Bluetooth actif et les identifiants du réseau
Thread souhaité. Voir les prérequis officiels
[Matter](https://www.home-assistant.io/integrations/matter/) et
[Thread](https://www.home-assistant.io/integrations/thread/).

## 2. Télécharger et compiler

Créer un clone neuf, distinct d'un dossier contenant des releases gelées :

```sh
git clone https://github.com/PommeAuMiel/esp32-wind-calm-matter.git esp32-wind-calm-matter-first-install
cd esp32-wind-calm-matter-first-install
"$XIAO_PYTHON" tools/bootstrap_sdk.py
"$XIAO_PYTHON" tools/thread_ota.py build --live
"$XIAO_PYTHON" tools/thread_ota.py verify
```

Le bootstrap télécharge un SDK isolé sous `.local/`. Les commandes ci-dessus
**ne flashent aucune carte**. Si une commande échoue, arrêter et corriger avant
la suite. Ne pas reconstruire ni écraser une archive d'une version distribuée.
Une compilation personnelle ne doit pas être redistribuée sous une version
existante avec d'autres octets.

Sélectionner automatiquement le dossier de la version compilée :

```sh
XIAO_BUILD="$("$XIAO_PYTHON" -c 'import sys; sys.path.insert(0, "tools"); import thread_ota; print(thread_ota.BUILD)')"
"$XIAO_PYTHON" -c 'from pathlib import Path; import sys; assert Path(sys.argv[1]).read_bytes() == b"\xff" * 8192, "otadata initial invalide"; print("otadata initial : OK")' "$XIAO_BUILD/ota_data_initial.bin"
```

Les fichiers nécessaires sont `bootloader/bootloader.bin`,
`partition_table/partition-table.bin`, `ota_data_initial.bin` et
`windcalm-thread-ota.bin`. **Le fichier `.ota` n'est pas une image à écrire
comme application USB.**

## 3. Préparer les outils USB

Le SDK utilise sa propre version d'esptool. Pour employer exactement les
commandes ci-dessous, créer un environnement USB séparé :

```sh
"$XIAO_PYTHON" -m venv .local/first-install-tools
.local/first-install-tools/bin/python -m pip install 'esptool==5.3.1' 'pyserial==3.5'
XIAO_USB_PY="$PWD/.local/first-install-tools/bin/python"
"$XIAO_USB_PY" -m esptool version
```

La version affichée doit être **5.3.1**. Cette installation ne modifie pas le SDK.

## 4. Brancher uniquement la carte et identifier le port

Le XIAO doit être **seul sur USB**, sans liaison au contrôleur du ventilateur,
à l'alimentation externe ni au secteur. Débrancher les autres cartes ESP pour
éviter une confusion et fermer tout moniteur série.

Pour entrer en mode de téléchargement, maintenir **BOOT**, brancher le câble
USB au PC, puis relâcher BOOT. Cette séquence est décrite par
[Seeed](https://wiki.seeedstudio.com/xiao_esp32c6_getting_started/).

Lister les ports :

```sh
"$XIAO_USB_PY" -m serial.tools.list_ports -v
```

Recopier **le port de votre XIAO** dans la variable suivante. Les valeurs sont
des exemples, pas un port à reprendre aveuglément :

```sh
# Exemple macOS : remplacer par le port réellement affiché.
XIAO_PORT=/dev/cu.usbmodem101
# Sous Linux, utiliser à la place le port réellement affiché, par exemple :
# XIAO_PORT=/dev/ttyACM0
```

Contrôler la puce, la flash et son état de sécurité :

```sh
"$XIAO_USB_PY" -m esptool --chip esp32c6 --port "$XIAO_PORT" --after no-reset flash-id
"$XIAO_USB_PY" -m esptool --chip esp32c6 --port "$XIAO_PORT" --after no-reset get-security-info
```

Continuer uniquement pour **ESP32-C6, flash 4 Mo**, Secure Boot et chiffrement
flash **désactivés**. En cas d'écart, arrêter : ne pas utiliser `--force` ni
modifier des eFuses. Si la connexion échoue, refaire la séquence BOOT et relister
les ports avant de réessayer ces lectures.

## 5. Faire le premier flash complet

**Cette étape efface toute la carte sélectionnée.** Vérifier encore le port
et qu'il s'agit du XIAO dédié, sans données Matter à conserver.

La chaîne `&&` arrête les étapes suivantes si l'une échoue. Copier le bloc
complet ; les quatre adresses correspondent au profil OTA du projet :

```sh
"$XIAO_USB_PY" -m esptool --chip esp32c6 --port "$XIAO_PORT" --after no-reset erase-flash &&
"$XIAO_USB_PY" -m esptool --chip esp32c6 --port "$XIAO_PORT" --after no-reset write-flash \
  --flash-mode dio --flash-freq 80m --flash-size 4MB \
  0x0 "$XIAO_BUILD/bootloader/bootloader.bin" \
  0x8000 "$XIAO_BUILD/partition_table/partition-table.bin" \
  0xe000 "$XIAO_BUILD/ota_data_initial.bin" \
  0x10000 "$XIAO_BUILD/windcalm-thread-ota.bin" &&
"$XIAO_USB_PY" -m esptool --chip esp32c6 --port "$XIAO_PORT" --after no-reset verify-flash \
  --flash-mode dio --flash-freq 80m --flash-size 4MB \
  0x0 "$XIAO_BUILD/bootloader/bootloader.bin" \
  0x8000 "$XIAO_BUILD/partition_table/partition-table.bin" \
  0xe000 "$XIAO_BUILD/ota_data_initial.bin" \
  0x10000 "$XIAO_BUILD/windcalm-thread-ota.bin"
```

Attendre la réussite de la vérification de **tous les fichiers**. Si une étape
échoue, ne pas poursuivre vers l'appairage ; conserver le message d'erreur.
Les options de flash sont celles de `flasher_args.json` généré par la compilation.
Les commandes sont celles d'[esptool](https://docs.espressif.com/projects/esptool/en/latest/esp32c6/esptool/basic-commands.html).

Après succès, appuyer **une fois sur RESET, sans maintenir BOOT**, puis laisser
le XIAO démarrer au moins une minute. Le contrôleur peut rester débranché.

## 6. Récupérer le code Matter et ajouter le XIAO à HA

Relister les ports si nécessaire et corriger `XIAO_PORT` s'il a changé. Ouvrir
la console USB à 115200 bauds :

```sh
"$XIAO_USB_PY" -m serial.tools.miniterm --dtr 0 --rts 0 --eol LF --echo "$XIAO_PORT" 115200
```

Dans **la console série**, et non dans le terminal shell, écrire :

```text
pairing
```

Valider par Entrée. Le firmware doit répondre `MANUAL_CODE: ...`. Le code est
propre à la carte : le conserver en privé, sans le publier dans une issue,
une photo ou un journal Git. Un message `already commissioned` ne donne pas
le code : ne pas effacer une carte déjà appairée pour contourner ce message.

Dans l'application mobile Home Assistant Companion, ajouter un **nouvel appareil
Matter**, choisir la saisie manuelle du code, puis entrer ce code. Le firmware
utilise des certificats de développement : un avertissement d'appareil non
certifié peut demander votre accord. Les écrans peuvent varier selon le téléphone.

Sans contrôleur raccordé, `tuya_ready=0` est **normal** et les commandes de
ventilateur/lumière ne sont pas encore utilisables. Le premier contrôle porte
sur l'ajout dans HA et la connexion Thread, pas sur les actionneurs.
Quitter miniterm avec **Ctrl+]** avant toute autre utilisation du port.

## 7. Raccorder le ventilateur et valider

Débrancher l'USB. Réaliser ensuite le [montage matériel](HARDWARE.md), hors tension,
avec l'alimentation régulée indépendante et le GND commun décrits dans ce guide.
Ne pas combiner le 5 V externe et l'USB direct du PC sans dispositif adapté.

Remettre le montage dans son alimentation normale, attendre sa disponibilité
Matter, puis vérifier : télécommande, lumière ON/OFF avec suivi HA, ventilateur
et six vitesses, bips et sens après arrêt confirmé pendant dix secondes. Pour la teinte, garder en
mémoire la [limite du panneau LED](HARDWARE.md#limite-du-panneau-led).
Adapter ensuite [la carte HA](../home-assistant/INSTALLATION.txt).

La première installation est terminée lorsque le montage répond depuis HA et
la télécommande, sans USB, avec des états concordants. Pour la prochaine version,
utiliser l'[OTA](../home-assistant/MAINTENANCE.txt) ; ne pas rejouer ce premier flash.

## Portée de vérification de ce guide

Les commandes et options sont vérifiées avec esptool 5.3.1 ; les adresses,
fichiers et paramètres sont comparés aux artefacts compilés du profil ESP32-C6.
Les tests du projet distinguent compilation, simulation et essai matériel.
**L'enchaînement complet de ce guide n'a pas encore été rejoué sur un XIAO
vierge dédié.** L'installation matérielle qualifiée du projet avait utilisé
une migration suivie d'OTA ; elle ne constitue pas une preuve de ce nouveau
parcours de premier flash. Aucun appareil de production n'est effacé pour
valider la documentation.
