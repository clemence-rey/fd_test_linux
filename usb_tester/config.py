"""
Configuration du programme de qualification des clés USB.

Centralise les paramètres du test, les critères PASS/FAIL,
le reformatage et les chemins de sauvegarde.
"""

from pathlib import Path


# ============================================================
# 1. INTERFACE GRAPHIQUE
# ============================================================

DEV_MODE = True                 # True : développement / False : production


# ============================================================
# 2. CONFIGURATION DU TEST
# ============================================================

# Mode d'arrêt : "cycles" ou "duration"
TEST_LIMIT_MODE = "cycles"
TEST_CYCLES = 2                 # Nombre de cycles écriture + lecture
TEST_DURATION_MIN = 0.5        # Durée du test en minutes

# Taille des données
TEST_FILE_SIZE_MB = 512        # Volume écrit et relu par cycle (MB)
BLOCK_SIZE_MB = 4              # Taille d'un bloc d'E/S (MB)


# ============================================================
# 3. CRITÈRES DE QUALIFICATION (PASS / FAIL)
# ============================================================

MIN_WRITE_MBPS = 100.0         # Débit minimal en écriture (MB/s)
MIN_READ_MBPS = 170.0          # Débit minimal en lecture (MB/s)
MAX_READ_MBPS = 500.0          # Débit maximal en lecture (MB/s)


# ============================================================
# 4. REFORMATAGE
# ============================================================

REFORMAT_FS = "fat32"          # "fat32", "exfat" ou "ntfs"
REFORMAT_LABEL = "FISCHER"     # Nom du volume après reformatage


# ============================================================
# 5. CHEMINS DES FICHIERS
# ============================================================

RESULTS_DIR = Path(__file__).resolve().parent / "results"


# ============================================================
# 6. CONSTANTES CALCULÉES
# ============================================================

# Conversion des tailles en octets
BLOCK_SIZE = BLOCK_SIZE_MB * 1024 * 1024
FILE_SIZE = TEST_FILE_SIZE_MB * 1024 * 1024
NUMBER_BLOCKS = FILE_SIZE // BLOCK_SIZE

# Conversion de la durée en secondes
TEST_DURATION = TEST_DURATION_MIN * 60