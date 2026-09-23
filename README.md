# FD Test

Suite de tests Python destinée à la caractérisation fonctionnelle de clés USB.

## Fonctionnalités

Le programme permet de réaliser différents essais sur une clé USB :

- Collecte des informations du périphérique USB
  - VID / PID
  - Fabricant
  - Produit
  - Numéro de série
  - Version USB
  - Vitesse négociée
  - Capacité
  - Système de fichiers

- Test de vitesse personnalisé
  - Génération d'un fichier de test
  - Écriture sur la clé
  - Lecture du fichier
  - Vérification de l'intégrité par SHA256

- Benchmarks FIO
  - Lecture séquentielle
  - Écriture séquentielle
  - Lecture aléatoire
  - Écriture aléatoire
  - Mesure des IOPS et de la bande passante

- Test de réénumération USB
  - Déconnexion logique du périphérique
  - Reconnexion logique
  - Validation de la détection du périphérique
  - Mesure du temps de réénumération

- Génération d'un rapport de test

## Structure du projet

```text
fd_test/
├── main.py
├── tests/
│   ├── usb_tests.py
│   └── udev.py
├── results/
├── tools/
└── archive/
```


## Utilisation

Créer et activer un environnement virtuel :

```bash
python3 -m venv venv
source venv/bin/activate
```

Installer les dépendances nécessaires puis lancer :

```bash
python main.py
```

Selon les tests exécutés, des privilèges administrateur peuvent être nécessaires :

```bash
sudo venv/bin/python main.py
```

## Auteur

Clémence Rey