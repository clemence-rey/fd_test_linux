"""
Configuration du programme de qualification des clés USB.

Centralise les paramètres du test, les critères PASS/FAIL,
le reformatage et les chemins de sauvegarde.
"""

from pathlib import Path

TEMPLATE_PATH = Path(__file__).resolve().parent / "Report_Template.docx"

STATION_NAME = "Laptop Dell \nLinux 5.15.0-139-generic \nUbuntu 20.04.6 LTS"
SOFTWARE_VERSION = "1.0"


# Port USB réservé au banc de qualification
USB_TEST_PORT = "pci-0000:00:14.0-usb-0:1"

# ============================================================
# 1. CONFIGURATION DU TEST
# ============================================================

# Mode d'arrêt : "cycles" ou "duration"
TEST_LIMIT_MODE = "cycles"
TEST_CYCLES = 2                 # Nombre de cycles écriture + lecture
TEST_DURATION_MIN = 0.5        # Durée du test en minutes

# Taille des données
TEST_FILE_SIZE_MB = 512        # Volume écrit et relu par cycle (MB)
BLOCK_SIZE_MB = 4              # Taille d'un bloc d'E/S (MB)


# ============================================================
# 2. CRITÈRES DE QUALIFICATION (PASS / FAIL)
# ============================================================

MIN_WRITE_MBPS = 80.0         # Débit minimal en écriture (MB/s)
MIN_READ_MBPS = 160.0          # Débit minimal en lecture (MB/s)
MAX_READ_MBPS = 500.0          # Débit maximal en lecture (MB/s)


# ============================================================
# 3. REFORMATAGE
# ============================================================

REFORMAT_FS = "fat32"          # "fat32", "exfat" ou "ntfs"
REFORMAT_LABEL = "FISCHER"     # Nom du volume après reformatage


# ============================================================
# 4. CHEMINS DES FICHIERS
# ============================================================

RESULTS_DIR = Path(__file__).resolve().parent / "results"


# ============================================================
# 5. CONSTANTES CALCULÉES
# ============================================================

# Conversion des tailles en octets
BLOCK_SIZE = BLOCK_SIZE_MB * 1024 * 1024
FILE_SIZE = TEST_FILE_SIZE_MB * 1024 * 1024
NUMBER_BLOCKS = FILE_SIZE // BLOCK_SIZE

# Conversion de la durée en secondes
TEST_DURATION = TEST_DURATION_MIN * 60