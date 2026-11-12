"""
USB flash drive qualification program configuration.

Centralizes test parameters, PASS/FAIL criteria,
reformatting settings and output paths.
"""

from pathlib import Path

TEMPLATE_PATH = Path(__file__).resolve().parent / "Report_Template.docx"

STATION_NAME = "Laptop Dell \nLinux 5.15.0-139-generic \nUbuntu 20.04.6 LTS"
SOFTWARE_VERSION = "1.0"

# Dedicated USB port used by the qualification station
USB_TEST_PORT = "pci-0000:00:14.0-usb-0:1.1"

# ============================================================
# 1. TEST CONFIGURATION
# ============================================================

# Stop mode: "cycles" or "duration"
TEST_DURATION_MODE = "cycles"

TEST_CYCLES = 2                # Number of write + read cycles
TEST_DURATION_MIN = 0.5        # Test duration in minutes

# Data size
TEST_FILE_SIZE_MB = 512        # Data volume written and read per cycle (MB)
BLOCK_SIZE_MB = 4              # I/O block size (MB)


# ============================================================
# 2. QUALIFICATION CRITERIA (PASS / FAIL)
# ============================================================

MIN_WRITE_MBPS = 80.0          # Minimum write speed (MB/s)
MIN_READ_MBPS = 160.0          # Minimum read speed (MB/s)
MAX_READ_MBPS = 500.0          # Maximum read speed (MB/s)


# ============================================================
# 3. REFORMATTING
# ============================================================

REFORMAT_FS = "fat32"          # "fat32", "exfat" or "ntfs"
REFORMAT_LABEL = "FISCHER"     # Volume label after reformatting


# ============================================================
# 4. FILE PATHS
# ============================================================

RESULTS_DIR = Path(__file__).resolve().parent / "results"


# ============================================================
# 5. DERIVED CONSTANTS
# ============================================================

# Convert sizes to bytes
BLOCK_SIZE = BLOCK_SIZE_MB * 1024 * 1024
FILE_SIZE = TEST_FILE_SIZE_MB * 1024 * 1024
NUMBER_BLOCKS = FILE_SIZE // BLOCK_SIZE

# Convert duration to seconds
TEST_DURATION = TEST_DURATION_MIN * 60


# ============================================================
# 6. TEST LINK CONFIGURATION
# ============================================================

TEST_LINK_REFS = {
    "123456": "TL-001",
    "153456": "TL-002",
    "126456": "TL-003",
    "323456": "TL-004",
    "783456": "TL-001",
    "193456": "TL-004",
}

TEST_LINK_FILE = Path(__file__).resolve().parent / "test_link.json"
MAX_MATING_CYCLES = 1000


# ============================================================
# 7. ACRONAME CONFIGURATION
# ============================================================

ACRONAME_PORT = 1
VBUS_SAMPLE_INTERVAL_S = 0.2
VBUS_IDLE_DURATION_S = 2.0

USB2_ENUM_TIMEOUT_S = 10.0
USB2_DISCONNECT_TIMEOUT_S = 3.0
USB2_RESTORE_TIMEOUT_S = 10.0
USB2_POLL_INTERVAL_S = 0.1