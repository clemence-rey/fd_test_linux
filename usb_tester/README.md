# USB Tester — Qualification de clés USB

Application Python sous **Ubuntu/Linux** destinée à automatiser un test de fonctionnement, de performances et d'intégrité de clés USB. Elle dispose d'une interface graphique PyQt5 et produit un verdict **PASS / FAIL**, accompagné de mesures et de rapports.

> **ATTENTION — TEST DESTRUCTIF** : le programme démonte les partitions de la clé sélectionnée, écrit directement sur le périphérique bloc (`/dev/sdX`) et recrée une table de partitions lors du reformatage final. **Toutes les données présentes sur la clé testée seront perdues.** N'utiliser qu'une clé dédiée aux essais. Ne jamais lancer ce programme sur un disque contenant des données à conserver.

## 1. Fonctionnement général

1. L'application surveille les périphériques de stockage USB connectés avec `pyudev`.
2. Lorsqu'une seule clé candidate est détectée, ses informations sont affichées et le bouton **START** est activé.
3. Au démarrage du test, le programme vérifie l'identité du disque à l'aide de `DISKSEQ`, puis démonte ses partitions.
4. Il écrit des blocs de données de référence directement sur le disque, sans passer par un système de fichiers, avec `O_DIRECT`.
5. Il relit les blocs et compare leur empreinte **SHA-256** à celle des données écrites.
6. Il mesure les débits d'écriture et de lecture, puis applique les critères de qualification définis dans `config.py`.
7. En l'absence d'interruption ou d'erreur d'E/S, il reformate la clé dans le système de fichiers configuré, même si les débits sont hors seuils.
8. Il affiche le verdict et génère les fichiers de résultats disponibles.

Le test est piloté soit par un **nombre de cycles**, soit par une **durée maximale**. En mode durée, un cycle peut être interrompu entre deux blocs ou après la phase d'écriture : ce mode ne garantit donc pas un nombre entier de cycles complets.

**Périmètre actuel :** le code réalise des mesures de lecture/écriture et une vérification d'intégrité sur le périphérique USB connecté. Il ne constitue pas un test électrique USB, une mesure de BER, ni une validation indépendante des modes USB 2.0 et USB 3.x.

## 2. Structure du projet

```text
usb_tester/
├── main.py          # Point d'entrée de l'application
├── config.py        # Paramètres de test et seuils PASS / FAIL
├── model.py         # Accès système, identification USB, formatage et rapports
├── controller.py    # Threads de surveillance et d'exécution du test
├── view.py          # Interface graphique PyQt5 et graphiques
└── results/         # Créé automatiquement pour les résultats
```

| Fichier | Responsabilité |
| --- | --- |
| `main.py` | Vérification des privilèges, création de `QApplication`, ouverture de la fenêtre. |
| `config.py` | Centralisation des paramètres modifiables et calcul des tailles en octets. |
| `model.py` | Commandes Linux, identification des périphériques, protection par `DISKSEQ`, accès brut, reformatage, préparation des rapports. |
| `controller.py` | `UsbWatcher` surveille les événements USB ; `TestWorker` exécute les E/S et calcule le verdict dans un thread distinct. |
| `view.py` | Gestion des états de la fenêtre, des signaux Qt, de la progression, du graphique et de l'export PDF. |

## 3. Prérequis

- **Système** : Linux, prévu pour Ubuntu, avec `udev` et des périphériques blocs accessibles via `/dev/`.
- **Python** : Python 3 avec environnement virtuel recommandé.
- **Bibliothèques Python** : `PyQt5`, `pyudev`, `numpy`, `matplotlib`.
- **Outils système** : `lsblk`, `findmnt`, `blockdev`, `umount`, `wipefs`, `sfdisk`, `partprobe`, `udevadm`, `blkid`, `sync`, et l'outil `mkfs` correspondant au système de fichiers choisi.
- **Droits** : accès administrateur nécessaire pour les opérations sur le périphérique bloc.

Installation typique sur Ubuntu :

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip \
    util-linux fdisk parted udev dosfstools exfatprogs ntfs-3g \
    policykit-1

cd usb_tester
python3 -m venv .venv
source .venv/bin/activate
pip install PyQt5 pyudev numpy matplotlib
```

> Les noms de paquets et la disponibilité de `pkexec` varient selon la version d'Ubuntu. `dosfstools` fournit `mkfs.vfat`, `exfatprogs` fournit `mkfs.exfat` et `ntfs-3g` fournit généralement `mkfs.ntfs`. Les dépendances Python ne sont pas figées dans ce dépôt tant qu'aucun fichier `requirements.txt` n'a été créé.

## 4. Configuration

Les paramètres sont regroupés dans **`config.py`**. Les valeurs ci-dessous correspondent à la configuration fournie lors de la rédaction de ce README.

| Paramètre | Valeur | Description |
| --- | --- | --- |
| `DEV_MODE` | `True` | Interface détaillée avec informations et graphique ; `False` pour l'affichage simplifié. |
| `TEST_LIMIT_MODE` | `"cycles"` | `"cycles"` ou `"duration"`. |
| `TEST_CYCLES` | `2` | Nombre de cycles demandés en mode `cycles`. |
| `TEST_DURATION_MIN` | `0.5` | Durée limite en minutes en mode `duration`. |
| `TEST_FILE_SIZE_MB` | `512` | Volume nominal écrit, puis relu, par cycle. |
| `BLOCK_SIZE_MB` | `4` | Taille d'un bloc d'E/S. |
| `MIN_WRITE_MBPS` | `100.0` | Seuil inférieur de débit d'écriture. |
| `MIN_READ_MBPS` | `170.0` | Seuil inférieur de débit de lecture. |
| `MAX_READ_MBPS` | `500.0` | Seuil supérieur de débit de lecture. |
| `REFORMAT_FS` | `"fat32"` | Format final : `fat32`, `exfat` ou `ntfs`. |
| `REFORMAT_LABEL` | `"FISCHER"` | Nom du volume après reformatage. |
| `RESULTS_DIR` | `results/` | Dossier de sortie, relatif au répertoire du programme. |

Les constantes `BLOCK_SIZE`, `FILE_SIZE`, `NUMBER_BLOCKS` et `TEST_DURATION` sont calculées automatiquement à partir de ces paramètres.

**Important :** les seuils sont **stricts** : le débit d'écriture doit être **supérieur** à `MIN_WRITE_MBPS` et le débit de lecture doit être **strictement compris entre** `MIN_READ_MBPS` et `MAX_READ_MBPS`. Les unités affichées en MB/s sont décimales (1 MB = 1 000 000 octets).

## 5. Lancement et utilisation

Depuis le dossier contenant les cinq fichiers Python :

```bash
source .venv/bin/activate
python3 main.py
```

`main.py` appelle `ensure_root()`, qui tente une relance avec `pkexec`, puis avec `sudo` si nécessaire. Selon la configuration de la session graphique, une demande d'authentification peut apparaître. **La relance avec élévation de privilèges dépend de la configuration locale de Linux/Qt et doit être validée sur la station cible.**

Procédure opérateur :

1. Vérifier qu'aucun périphérique de stockage USB non destiné au test n'est branché.
2. Lancer le programme et attendre **« Insérez la clé USB »**.
3. Insérer **une seule clé USB de test** et vérifier son identification à l'écran.
4. Cliquer sur **START**. Ne pas retirer la clé pendant les E/S ou le reformatage.
5. Attendre le verdict **PASS**, **FAIL** ou l'indication de test interrompu.
6. Consulter les rapports dans `results/`, puis retirer la clé lorsque l'interface le demande.

En mode développement, la fenêtre affiche également les informations techniques, les étapes du test et le graphique des débits.

## 6. Mesures et critères de qualification

### Écriture et lecture

Les données sont écrites directement sur le périphérique bloc à l'aide de `os.writev()`, puis relues avec `os.readv()`. Les buffers sont alignés pour `O_DIRECT`. Le chronométrage de débit porte sur les appels d'E/S ; le calcul SHA-256 et la mise à jour de l'interface sont réalisés hors de ce chronométrage.

Pour chaque bloc, le CSV conserve le cycle, l'opération, l'indice du bloc, le temps relatif, le débit mesuré et un indicateur d'intégrité. Les débits moyens globaux sont calculés à partir des octets transférés et du temps cumulé des appels d'E/S. Des statistiques entre cycles (écart-type et coefficient de variation) sont également calculées lorsqu'au moins deux mesures sont disponibles.

### Intégrité

Un bloc de référence aléatoire est généré au début du test et réutilisé pour les écritures. Chaque bloc relu est comparé à l'empreinte SHA-256 attendue. Une différence de taille ou d'empreinte est comptabilisée comme une erreur d'intégrité.

### Verdict PASS / FAIL

Un verdict **PASS** nécessite simultanément :

- un test considéré comme terminé, sans interruption ni erreur d'E/S ;
- au moins des données écrites et lues ;
- **zéro erreur d'intégrité** ;
- un débit moyen d'écriture **> 100 MB/s** ;
- un débit moyen de lecture **> 170 MB/s et < 500 MB/s** ;
- un reformatage final réussi.

Ces valeurs sont les paramètres actuels et peuvent être modifiées dans `config.py`.

Un verdict **FAIL** peut notamment provenir d'un débit hors seuil, d'une erreur d'intégrité, d'une erreur système, d'une interruption ou d'un échec de reformatage. Une erreur d'E/S ou une interruption empêche le reformatage final ; en revanche, un débit insuffisant n'empêche pas, à lui seul, le reformatage.

## 7. Rapports et fichiers générés

Le dossier `results/` contient, selon l'avancement du test :

```text
results/
├── USB_test_YYYYMMDD_HHMMSS_sda_SERIAL.csv
├── USB_READ_WRITE_YYYYMMDD_HHMMSS_sda_SERIAL.png
└── USB_report_YYYYMMDD_HHMMSS_sda_SERIAL.pdf
```

- **CSV** : mesures détaillées de chaque bloc écrit ou lu.
- **PNG** : courbes des débits de lecture et d'écriture, avec seuils et moyennes lorsque disponibles.
- **PDF** : identification du périphérique, performances, intégrité, résultat du reformatage, verdict et graphique.

Les noms de fichiers incluent un horodatage, le nom du périphérique et le numéro de série disponible. **Le numéro de série transmis par certaines clés peut être absent, générique ou non unique** : il ne doit pas être considéré comme une identification matérielle universellement fiable.

La génération du PDF est déclenchée depuis l'interface graphique après réception du résultat. Si le test échoue très tôt, certains fichiers ou champs peuvent être absents.

## 8. Architecture des threads et sécurité

- **Thread principal Qt** : affiche l'interface et traite les événements utilisateurs.
- **`UsbWatcher(QThread)`** : écoute les événements `udev` et transmet les changements de périphériques à l'interface.
- **`TestWorker(QThread)`** : effectue les opérations de test, transmet les mesures, les messages d'état, la progression et le résultat final au moyen de signaux Qt.

L'identité du périphérique est vérifiée avec `DISKSEQ` avant les opérations sensibles. L'ouverture du disque utilise notamment `O_EXCL` et `O_DIRECT`. Ces précautions réduisent le risque de travailler sur un périphérique remplacé, mais **ne remplacent pas la vérification visuelle de la clé sélectionnée**.

En cas de déconnexion signalée pendant le test, l'interface demande l'interruption du worker. L'arrêt est coopératif : une opération d'E/S bloquée dans le noyau peut retarder l'arrêt. La barre de progression atteint au maximum **95 %** pendant les E/S et passe à **100 %** lors de la finalisation de l'interface.

## 9. Vérification et diagnostic

Contrôle syntaxique et chargement des modules :

```bash
python3 -m py_compile *.py
python3 -c "import config, model, controller, view, main"
```

Ces commandes ne remplacent **pas** un essai sur une clé dédiée. Pour une validation avant mise en production, vérifier au minimum : lancement et élévation des privilèges, détection d'une seule clé, test PASS, débits hors seuil, erreur d'intégrité, retrait pendant le test, échec de reformatage, génération des fichiers, et fermeture pendant un test.

Commandes Linux utiles pour identifier le périphérique et sa vitesse négociée :

```bash
lsblk -o NAME,PATH,SIZE,MODEL,SERIAL,TRAN,MOUNTPOINTS
lsusb -t
```

**Ne jamais exécuter manuellement `wipefs`, `sfdisk` ou `mkfs` sur un disque dont l'identité n'a pas été confirmée.**

## 10. Limites et points d'attention

- Le programme teste actuellement **une clé à la fois** ; plusieurs clés candidates désactivent le démarrage du test.
- Il n'impose pas automatiquement un mode de connexion USB 2.0 ou USB 3.x.
- Les performances dépendent aussi du contrôleur USB, du port, du lien, de la station et de la clé testée.
- Les appels d'E/S peuvent rester bloquants malgré la logique d'interruption du thread.
- Le test écrit au début du disque et **n'effectue pas une vérification exhaustive de toute la capacité** de la clé.
- Le reformatage recrée une table **MBR** et une partition principale ; le résultat doit être validé sur les systèmes d'exploitation cibles.
- Les fichiers Python ne sont pas accompagnés ici d'une suite de tests automatisés ni de versions de dépendances figées.

## 11. Maintenance

Pour conserver une architecture lisible :

- modifier les seuils et paramètres dans `config.py`, plutôt que de disperser des valeurs dans les autres fichiers ;
- garder les opérations Linux et les protections de périphérique dans `model.py` ;
- conserver la logique de test et les threads dans `controller.py` ;
- limiter `view.py` à l'affichage et à la coordination de l'interface ;
- documenter chaque nouvelle fonction avec une docstring `"""..."""`.

**Avant toute modification de la logique d'E/S, de détection du disque ou de reformatage, effectuer une revue de code et des essais sur une clé sacrifiable.**