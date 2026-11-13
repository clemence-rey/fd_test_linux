"""
USB flash drive qualification program configuration.

Centralizes station settings, test parameters, qualification criteria,
hardware configuration and output paths.
"""

from pathlib import Path


# ============================================================
# BASE PATHS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path("/home/fischer/Desktop/USB_Tester_release")


# ============================================================
# 1. STATION CONFIGURATION
# ============================================================

STATION_NAME = "Laptop Dell \nLinux 5.15.0-139-generic \nUbuntu 20.04.6 LTS"
SOFTWARE_VERSION = "2.0"

# Dedicated physical USB port used for DUT qualification.
USB_TEST_PORT = "pci-0000:00:14.0-usb-0:1.1"


# ============================================================
# 2. TEST CONFIGURATION
# ============================================================

# Test limit mode: "cycles" or "duration".
TEST_LIMIT_MODE = "cycles"

# Test duration.
TEST_CYCLES = 2
TEST_DURATION_MINUTES = 0.5

# Test data size.
TEST_FILE_SIZE_MB = 512
BLOCK_SIZE_MB = 4


# ============================================================
# 3. QUALIFICATION CRITERIA
# ============================================================

MIN_WRITE_MBPS = 80.0
MIN_READ_MBPS = 160.0
MAX_READ_MBPS = 500.0


# ============================================================
# 4. REFORMATTING
# ============================================================

REFORMAT_FS = "fat32"          # "fat32", "exfat" or "ntfs"
REFORMAT_LABEL = "FISCHER"


# ============================================================
# 5. TEST-LINK CONFIGURATION
# ============================================================

TEST_LINK_REFS = {
    "123456": "TL-001",
    "153456": "TL-002",
    "126456": "TL-003",
    "323456": "TL-004",
    "783456": "TL-001",
    "193456": "TL-004",
}

MAX_MATING_CYCLES = 1000


# ============================================================
# 6. ACRONAME CONFIGURATION
# ============================================================

ACRONAME_PORT = 1

# VBUS monitoring.
VBUS_SAMPLE_INTERVAL_S = 0.2
VBUS_IDLE_DURATION_S = 2.0

# USB2 fallback.
USB2_ENUM_TIMEOUT_S = 10.0
USB2_DISCONNECT_TIMEOUT_S = 3.0
USB2_RESTORE_TIMEOUT_S = 10.0
USB2_POLL_INTERVAL_S = 0.1


# ============================================================
# 7. FILE PATHS
# ============================================================

TEMPLATE_PATH = BASE_DIR / "Report_Template.docx"

RESULTS_DIR = DATA_DIR / "results"
TEST_LINK_FILE = DATA_DIR / "test_link.json"


# ============================================================
# 8. DERIVED CONSTANTS
# ============================================================

BLOCK_SIZE = BLOCK_SIZE_MB * 1024 * 1024
FILE_SIZE = TEST_FILE_SIZE_MB * 1024 * 1024
NUMBER_BLOCKS = FILE_SIZE // BLOCK_SIZE

TEST_DURATION = TEST_DURATION_MINUTES * 60