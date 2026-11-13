"""Technical USB test functions: detection, I/O, errors and reporting.

This module handles neither test threads nor the graphical interface.
"""

# Standard library
import errno
import fcntl
import json
import mmap
import os
import shutil
import struct
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from io import BytesIO
from math import sqrt
from pathlib import Path

# Third-party libraries
import pyudev
from docx import Document
from docx.shared import Inches

# Application configuration
from config import (
    MAX_MATING_CYCLES,
    MAX_READ_MBPS,
    MIN_READ_MBPS,
    MIN_WRITE_MBPS,
    RESULTS_DIR,
    TEST_LINK_FILE,
    USB_TEST_PORT,
)


# ============================================================
# MODULE CONSTANTS
# ============================================================

BLKGETDISKSEQ = 0x80081280


# ============================================================
# 1. SYSTEM UTILITIES
# ============================================================

def run_command(cmd, timeout=20, input_text=None):
    """Execute a command and return stdout, stderr and return code."""

    try:
        result = subprocess.run(
            cmd,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        return result.stdout.strip(), result.stderr.strip(), result.returncode

    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", str(exc), -1


def get_root_disk():
    """Return the disk containing the system root partition."""

    # Retrieve the device hosting the root filesystem.
    source, _, rc = run_command(["findmnt", "-n", "-o", "SOURCE", "/"])

    if rc != 0 or not source:
        return None

    # Retrieve the parent disk of the root partition.
    disk, _, rc = run_command(["lsblk", "-no", "PKNAME", source])

    return disk.strip() if rc == 0 and disk else None


# Cache the system disk once at module import.
ROOT_DISK = get_root_disk()


# ============================================================
# 2. USB DETECTION AND IDENTIFICATION
# ============================================================

def scan_usb_candidates():
    """Return USB disks connected to the dedicated test port."""

    candidates = []

    for device in pyudev.Context().list_devices(subsystem="block", DEVTYPE="disk"):

        # Keep only USB disks connected to the dedicated test port.
        if device.get("ID_BUS") != "usb":
            continue

        if not device.get("ID_PATH", "").startswith(USB_TEST_PORT + ":"):
            continue

        # Never allow the system disk to become a test candidate.
        if device.sys_name == ROOT_DISK:
            continue

        candidates.append(device.device_node)

    return sorted(candidates)


def get_usb_parent_device(udev_device):
    """Return the parent USB device."""

    return udev_device.find_parent("usb", "usb_device")


def get_usb_speed_mbps(devnode):
    """Return the negotiated USB link speed in Mb/s."""

    udev_device = pyudev.Devices.from_device_file(pyudev.Context(), devnode)
    usb_device = get_usb_parent_device(udev_device)

    if usb_device is None:
        raise RuntimeError("USB parent device not found")

    speed = (
        (usb_device.attributes.get("speed") or b"")
        .decode(errors="ignore")
        .strip()
    )

    if not speed:
        raise RuntimeError("USB link speed not available")

    try:
        return float(speed)

    except ValueError as exc:
        raise RuntimeError(f"Invalid USB link speed: {speed}") from exc


def wait_for_test_port_empty(timeout_s, poll_interval_s=0.1):
    """Wait until no USB disk is detected on the test port."""

    deadline = time.monotonic() + timeout_s

    while time.monotonic() < deadline:
        if not scan_usb_candidates():
            return

        time.sleep(poll_interval_s)

    raise RuntimeError("USB device did not disconnect from the test port")


def wait_for_test_device(
    timeout_s,
    poll_interval_s=0.1,
    expected_speed_mbps=None,
    interruption_requested=None,
):
    """Wait for one USB disk to enumerate on the test port."""

    deadline = time.monotonic() + timeout_s
    last_speed = None

    while time.monotonic() < deadline:

        # Abort enumeration if the qualification is interrupted.
        if interruption_requested is not None and interruption_requested():
            raise RuntimeError("USB enumeration interrupted")

        candidates = scan_usb_candidates()

        if len(candidates) > 1:
            raise RuntimeError("Multiple USB disks detected on the test port")

        if len(candidates) == 1:
            devnode = candidates[0]

            if expected_speed_mbps is None:
                return devnode, get_usb_speed_mbps(devnode)

            try:
                last_speed = get_usb_speed_mbps(devnode)

            except (OSError, RuntimeError):
                time.sleep(poll_interval_s)
                continue

            if abs(last_speed - expected_speed_mbps) < 1.0:
                return devnode, last_speed

        time.sleep(poll_interval_s)

    if last_speed is not None:
        raise RuntimeError(
            f"USB device enumerated at {last_speed:g} Mb/s "
            f"instead of {expected_speed_mbps:g} Mb/s"
        )

    raise RuntimeError("USB device enumeration timeout")


def get_diskseq(devnode):
    """Return the DISKSEQ identifying the current device instance."""

    try:
        device = pyudev.Devices.from_device_file(pyudev.Context(), devnode)
        return int(device.properties["DISKSEQ"])

    except (KeyError, ValueError, OSError, pyudev.DeviceNotFoundError) as exc:
        raise RuntimeError(f"Unable to identify the device {devnode}") from exc


def assert_same_device(devnode, expected_diskseq):
    """Verify that the device has not been replaced."""

    current_diskseq = get_diskseq(devnode)

    if current_diskseq != expected_diskseq:
        raise RuntimeError(
            f"Device replaced on {devnode}: "
            f"DISKSEQ expected={expected_diskseq}, actual={current_diskseq}"
        )


def assert_test_port(devnode):
    """Verify that the device is connected to the dedicated USB test port."""

    device = pyudev.Devices.from_device_file(pyudev.Context(), devnode)
    id_path = device.get("ID_PATH", "")

    if not id_path.startswith(USB_TEST_PORT + ":"):
        raise RuntimeError(f"Unauthorized port: {devnode} (ID_PATH={id_path})")


def assert_expected_test_device(devnode, expected_diskseq):
    """Verify that the expected DUT is still on the dedicated test port."""

    assert_test_port(devnode)
    assert_same_device(devnode, expected_diskseq)


# ============================================================
# 3. DEVICE INFORMATION
# ============================================================

def get_device_info(devnode):
    """Return all available information about the DUT."""

    info = {
        "Devnode": devnode,
        "DeviceLabel": "Unknown USB device",
        "Model": "N/A",
        "Manufacturer": "N/A",
        "Serial": "N/A",
        "Size_Bytes": None,
        "Size_GB": "N/A",
        "VID": "N/A",
        "PID": "N/A",
        "USBSpeed_Mbps": None,
        "USBVersion": "N/A",
        "Date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    # Retrieve storage information from lsblk.
    out, _, rc = run_command([
        "lsblk",
        "-b",
        "-J",
        "-o",
        "NAME,MODEL,SERIAL,SIZE,VENDOR",
        devnode,
    ])

    if rc == 0 and out:
        try:
            disk_info = json.loads(out)["blockdevices"][0]

            info["Model"] = disk_info.get("model") or "N/A"
            info["Manufacturer"] = disk_info.get("vendor") or "N/A"
            info["Serial"] = disk_info.get("serial") or "N/A"

            if disk_info.get("size"):
                info["Size_Bytes"] = int(disk_info["size"])
                info["Size_GB"] = round(info["Size_Bytes"] / 1e9, 2)

        except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
            pass

    # Retrieve USB identification information.
    try:
        udev_device = pyudev.Devices.from_device_file(pyudev.Context(), devnode)
        usb_device = get_usb_parent_device(udev_device)

        if usb_device is not None:
            info["VID"] = (
                (usb_device.attributes.get("idVendor") or b"")
                .decode(errors="ignore")
                .upper()
                or "N/A"
            )

            info["PID"] = (
                (usb_device.attributes.get("idProduct") or b"")
                .decode(errors="ignore")
                .upper()
                or "N/A"
            )

            if info["Manufacturer"] == "N/A":
                info["Manufacturer"] = (
                    (usb_device.attributes.get("manufacturer") or b"")
                    .decode(errors="ignore")
                    .strip()
                    or "N/A"
                )

            if info["Model"] == "N/A":
                info["Model"] = (
                    (usb_device.attributes.get("product") or b"")
                    .decode(errors="ignore")
                    .strip()
                    or "N/A"
                )

    except (OSError, pyudev.DeviceNotFoundError, AttributeError, UnicodeError):
        pass

    # Retrieve the negotiated USB speed.
    try:
        speed = get_usb_speed_mbps(devnode)
        info["USBSpeed_Mbps"] = speed

        speed_labels = {
            1.5: "USB Low-Speed (1.5 Mb/s)",
            12: "USB Full-Speed (12 Mb/s)",
            480: "USB 2.0 High-Speed (480 Mb/s)",
            5000: "USB 3.2 Gen 1 (5 Gb/s)",
            10000: "USB 3.2 Gen 2 (10 Gb/s)",
            20000: "USB 3.2 Gen 2x2 (20 Gb/s)",
        }

        info["USBVersion"] = next(
            (
                label
                for reference, label in speed_labels.items()
                if abs(speed - reference) < reference * 0.05
            ),
            f"{speed:.0f} Mb/s",
        )

    except (OSError, RuntimeError):
        pass

    # Build the user-facing device label.
    info["DeviceLabel"] = " - ".join(
        value
        for value in (info["Manufacturer"], info["Model"])
        if value != "N/A"
    ) or "Unknown USB device"

    return info


# ============================================================
# 4. DIRECT DEVICE ACCESS
# ============================================================

def create_aligned_buffer(size, fill=None):
    """Create a memory-aligned buffer compatible with O_DIRECT."""

    buffer = mmap.mmap(-1, size)

    if fill is not None:
        buffer[:] = fill

    return buffer


def open_test_device(devnode, expected_diskseq):
    """Open the DUT for exclusive direct access and verify its identity."""

    # Verify that the DUT is connected to the dedicated test port.
    assert_test_port(devnode)

    # Open the raw disk exclusively with direct synchronous I/O.
    fd = os.open(
        devnode,
        os.O_RDWR | os.O_SYNC | os.O_DIRECT | os.O_EXCL,
    )

    try:
        # Verify the identity of the device that was actually opened.
        diskseq = struct.unpack(
            "Q",
            fcntl.ioctl(fd, BLKGETDISKSEQ, bytes(8)),
        )[0]

        if diskseq != expected_diskseq:
            raise RuntimeError("The USB device was changed before raw access")

        return fd

    except Exception:
        os.close(fd)
        raise


# ============================================================
# 5. PARTITIONING AND REFORMATTING
# ============================================================

FILESYSTEM_CONFIG = {
    "exfat": {
        "tool": "mkfs.exfat",
        "args": [],
        "blkid": "exfat",
        "partition_type": "7",
    },
    "fat32": {
        "tool": "mkfs.vfat",
        "args": ["-F", "32"],
        "blkid": "vfat",
        "partition_type": "c",
    },
    "ntfs": {
        "tool": "mkfs.ntfs",
        "args": ["-Q"],
        "blkid": "ntfs",
        "partition_type": "7",
    },
}


def run_checked_command(cmd, timeout=30, input_text=None):
    """Run a command and raise an exception if it fails."""

    out, err, rc = run_command(
        cmd,
        timeout=timeout,
        input_text=input_text,
    )

    if rc != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\n{err or out}"
        )

    return out


def get_mounted_partitions(devnode):
    """Return mounted filesystems belonging to the DUT."""

    out = run_checked_command([
        "lsblk",
        "-J",
        "-p",
        "-l",
        "-o",
        "PATH,TYPE,MOUNTPOINT",
        devnode,
    ])

    try:
        devices = json.loads(out)["blockdevices"]

    except (json.JSONDecodeError, KeyError, TypeError):
        raise RuntimeError("Unable to read mounted partitions")

    return [
        device["path"]
        for device in devices
        if device.get("mountpoint")
    ]


def unmount_device(devnode):
    """Unmount all mounted filesystems of the DUT."""

    assert_test_port(devnode)

    for device in reversed(get_mounted_partitions(devnode)):
        run_checked_command(["umount", device])

    remaining = get_mounted_partitions(devnode)

    if remaining:
        raise RuntimeError(f"Devices still mounted: {', '.join(remaining)}")


def reformat_device(devnode, fs_type, label, expected_diskseq):
    """Create one MBR partition and format the DUT."""

    fs_type = fs_type.lower().strip()

    if fs_type not in FILESYSTEM_CONFIG:
        raise ValueError(f"Unsupported file system: {fs_type}")

    config = FILESYSTEM_CONFIG[fs_type]

    # Check required system tools.
    for tool in (
        config["tool"],
        "sfdisk",
        "partprobe",
        "udevadm",
        "lsblk",
        "blkid",
        "sync",
    ):
        if not shutil.which(tool):
            raise RuntimeError(f"Tool not found: {tool}")

    partition_script = (
        "label: dos\n"
        f"start=2048, type={config['partition_type']}\n"
    )

    # Verify the DUT before modifying its partition table.
    assert_expected_test_device(devnode, expected_diskseq)

    run_checked_command(
        ["sfdisk", "--wipe", "always", devnode],
        input_text=partition_script,
    )

    # Refresh and wait for the new partition table.
    run_checked_command(["partprobe", devnode])
    run_checked_command(
        ["udevadm", "settle", "--timeout=15"],
        timeout=20,
    )

    # Detect the newly created partition.
    out = run_checked_command(["lsblk", "-ln", "-o", "PATH,TYPE", devnode])

    partitions = [
        parts[0]
        for line in out.splitlines()
        if len(parts := line.split()) == 2 and parts[1] == "part"
    ]

    if len(partitions) != 1:
        raise RuntimeError(f"Single partition not detected on {devnode}")

    partition = partitions[0]

    # Verify the DUT again before formatting.
    assert_expected_test_device(devnode, expected_diskseq)

    format_command = [config["tool"], *config["args"]]

    if label:
        format_command.extend(["-n", label])

    format_command.append(partition)

    run_checked_command(format_command, timeout=120)
    run_checked_command(["sync"], timeout=60)

    # Verify the resulting filesystem.
    detected_fs = run_checked_command([
        "blkid",
        "-p",
        "-o", "value",
        "-s", "TYPE",
        partition,
    ]).strip().lower()

    if detected_fs != config["blkid"]:
        raise RuntimeError(
            f"{fs_type.upper()} not detected after reformatting "
            f"(blkid: {detected_fs or 'unknown'}, expected: {config['blkid']})"
        )

# ============================================================
# DOMAIN VALUES
# ============================================================

class TestPhase(str, Enum):
    """Represent VBUS measurement phases."""

    IDLE = "IDLE"
    WRITE = "WRITE"
    READ = "READ"
    OTHER = "OTHER"


@dataclass
class VbusSample:
    """Store one VBUS measurement."""

    timestamp_ns: int
    voltage_v: float
    current_ma: float
    phase: TestPhase = TestPhase.OTHER


# ============================================================
# 6. CALCULATIONS AND STATISTICS
# ============================================================

def compute_mbps(byte_count, duration):
    """Compute throughput in MB/s."""

    if duration <= 0:
        return 0.0

    return byte_count / duration / 1e6


def compute_inter_cycle_stats(cycle_measurements):
    """Compute speed dispersion between test cycles."""

    cycles = []

    # Compute the average speed of each cycle.
    for measurements in cycle_measurements:
        total_bytes = sum(size for size, _ in measurements)
        total_time = sum(duration for _, duration in measurements)

        if total_time > 0:
            cycles.append((
                compute_mbps(total_bytes, total_time),
                total_time,
            ))

    n = len(cycles)

    if n < 2:
        return {"n": n, "std": None, "cv": None}

    # Compute time-weighted dispersion between cycle averages.
    total_time = sum(duration for _, duration in cycles)

    global_mean = sum(
        speed * duration
        for speed, duration in cycles
    ) / total_time

    variance = sum(
        duration * (speed - global_mean) ** 2
        for speed, duration in cycles
    ) / total_time

    std = sqrt(variance)

    return {
        "n": n,
        "std": std,
        "cv": std / global_mean * 100 if global_mean > 0 else None,
    }


def compute_intra_cycle_stats(cycle_measurements):
    """Compute block-speed dispersion within test cycles."""

    total_bytes = 0
    total_time = 0.0
    weighted_variance = 0.0
    n = 0

    # Compute dispersion of block speeds around each cycle average.
    for measurements in cycle_measurements:
        cycle_bytes = sum(size for size, _ in measurements)
        cycle_time = sum(duration for _, duration in measurements)

        if cycle_time <= 0:
            continue

        cycle_mean = compute_mbps(cycle_bytes, cycle_time)

        for size, duration in measurements:
            if duration <= 0:
                continue

            speed = compute_mbps(size, duration)
            weighted_variance += duration * (speed - cycle_mean) ** 2
            n += 1

        total_bytes += cycle_bytes
        total_time += cycle_time

    if n < 2 or total_time <= 0:
        return {"n": n, "std": None, "cv": None}

    global_mean = compute_mbps(total_bytes, total_time)
    std = sqrt(weighted_variance / total_time)

    return {
        "n": n,
        "std": std,
        "cv": std / global_mean * 100 if global_mean > 0 else None,
    }


def _empty_vbus_stats():
    """Return an empty VBUS statistics structure."""

    return {
        "n": 0,
        "voltage_min": None,
        "voltage_avg": None,
        "voltage_max": None,
        "current_min": None,
        "current_avg": None,
        "current_max": None,
    }


def compute_vbus_phase_stats(samples):
    """Compute VBUS statistics for each measured test phase."""

    phases = (
        TestPhase.IDLE,
        TestPhase.WRITE,
        TestPhase.READ,
    )

    grouped = {phase: [] for phase in phases}

    # Group measurements by test phase.
    for sample in samples:
        if sample.phase in grouped:
            grouped[sample.phase].append(sample)

    # Compute voltage and current statistics for each phase.
    statistics = {}

    for phase, phase_samples in grouped.items():
        if not phase_samples:
            statistics[phase.value] = _empty_vbus_stats()
            continue

        voltages = [sample.voltage_v for sample in phase_samples]
        currents = [sample.current_ma for sample in phase_samples]

        statistics[phase.value] = {
            "n": len(phase_samples),
            "voltage_min": min(voltages),
            "voltage_avg": sum(voltages) / len(voltages),
            "voltage_max": max(voltages),
            "current_min": min(currents),
            "current_avg": sum(currents) / len(currents),
            "current_max": max(currents),
        }

    return statistics


# ============================================================
# 7. RESULT AND ERROR HANDLING
# ============================================================

def create_test_result(devnode):
    """Create an empty USB qualification result."""

    empty_stats = {
        "n": 0,
        "std": None,
        "cv": None,
    }

    return {
        "device_info": {
            "Devnode": devnode,
        },

        # Output files.
        "csv": "",
        "docx": "",
        "report": "",

        # Performance and integrity.
        "avg_write": 0.0,
        "avg_read": 0.0,
        "write_inter_stats": empty_stats.copy(),
        "read_inter_stats": empty_stats.copy(),
        "write_intra_stats": empty_stats.copy(),
        "read_intra_stats": empty_stats.copy(),
        "integrity_errors": 0,

        # VBUS.
        "vbus_stats": {},
        "vbus_error": "",

        # USB2 fallback.
        "usb2_test": {
            "result": "NOT PERFORMED",
            "speed_mbps": None,
            "devnode": "",
            "error": "",
        },

        # Test link.
        "test_link": {},

        # Qualification status.
        "reformat": "NOT PERFORMED",
        "error": "",
        "result": "FAIL",
        "completed": False,
    }


def format_error(exc, operation=""):
    """Return a readable error message."""

    if isinstance(exc, OSError):
        error_map = {
            errno.ENOENT: "File or device not found",
            errno.EIO: "Input/output error",
            errno.ENODEV: "Device unavailable",
            errno.ENXIO: "Device or address unavailable",
            errno.ETIMEDOUT: "Input/output timeout",
            errno.EACCES: "Permission denied",
            errno.EBUSY: "Device busy",
            errno.ENOSPC: "Insufficient disk space",
            errno.EINVAL: "Invalid argument",
        }

        message = error_map.get(exc.errno, str(exc) or "System error")

    elif isinstance(exc, subprocess.TimeoutExpired):
        message = "Command execution timeout"

    elif isinstance(exc, (RuntimeError, ValueError)):
        message = str(exc)

    else:
        message = str(exc) or "Unexpected error"

    return f"{operation}: {message}" if operation else message


def set_result_error(result, exc, operation=""):
    """Store an error and mark the qualification as failed."""

    result["error"] = format_error(exc, operation)
    result["result"] = "FAIL"
    result["completed"] = False


# ============================================================
# 8. PASS/FAIL EVALUATION
# ============================================================

def evaluate_performance_test(
    total_written,
    total_read,
    write_io_time,
    read_io_time,
    cycle_write_measurements,
    cycle_read_measurements,
    integrity_errors,
    interrupted,
    io_failed,
):
    """Compute performance statistics and evaluate the test result."""

    avg_write = compute_mbps(total_written, write_io_time)
    avg_read = compute_mbps(total_read, read_io_time)

    completed = (
        not interrupted
        and not io_failed
        and total_written > 0
        and total_read > 0
    )

    passed = (
        completed
        and integrity_errors == 0
        and avg_write > MIN_WRITE_MBPS
        and MIN_READ_MBPS < avg_read < MAX_READ_MBPS
    )

    return {
        "avg_write": avg_write,
        "avg_read": avg_read,
        "write_intra_stats": compute_intra_cycle_stats(cycle_write_measurements),
        "read_intra_stats": compute_intra_cycle_stats(cycle_read_measurements),
        "write_inter_stats": compute_inter_cycle_stats(cycle_write_measurements),
        "read_inter_stats": compute_inter_cycle_stats(cycle_read_measurements),
        "integrity_errors": integrity_errors,
        "completed": completed,
        "result": "PASS" if passed else "FAIL",
    }


# ============================================================
# 8. REPORT GENERATION
# ============================================================

def sanitize_filename(text):
    """Return an ASCII-safe filename component."""

    return "".join(
        char if char.isascii() and (char.isalnum() or char in "-_") else "_"
        for char in str(text)
    )


def create_result_paths(serial):
    """Create output paths for the qualification result."""

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = f"{stamp}_{sanitize_filename(serial)}"

    return {
        "csv": str(RESULTS_DIR / f"USB_test_{tag}.csv"),
        "docx": str(RESULTS_DIR / f"USB_report_{tag}.docx"),
    }


def build_device_info_text(info):
    """Format DUT information for display and reporting."""

    fields = (
        ("Date", "Date"),
        ("Model", "Model"),
        ("Manufacturer", "Manufacturer"),
        ("Serial", "Serial"),
        ("VID", "VID"),
        ("PID", "PID"),
        ("Size_GB", "Size"),
        ("USBVersion", "USB Version"),
    )

    lines = ["=== DEVICE INFORMATION ==="]

    for key, label in fields:
        lines.append(f"{label:<14}: {info.get(key, 'N/A')}")

    return "\n".join(lines)


def get_failure_reasons(result):
    """Return the qualification failure reasons."""

    reasons = []

    if result.get("error"):
        reasons.extend(result["error"].splitlines())

    if result["integrity_errors"]:
        reasons.append(
            f"{result['integrity_errors']} integrity error(s)"
        )

    if 0 < result["avg_write"] <= MIN_WRITE_MBPS:
        reasons.append("Write speed below minimum threshold")

    if 0 < result["avg_read"] <= MIN_READ_MBPS:
        reasons.append("Read speed below minimum threshold")

    elif result["avg_read"] >= MAX_READ_MBPS:
        reasons.append("Read speed above maximum threshold")

    usb2 = result.get("usb2_test", {})

    if usb2.get("result") == "FAIL":
        error = usb2.get("error")

        reasons.append(
            f"USB 2.0 fallback failed: {error}"
            if error
            else "USB 2.0 High-Speed enumeration failed"
        )

    if not reasons:
        reasons.append(
            "Reformatting failed"
            if result.get("reformat", "").startswith("FAILURE")
            else "Test incomplete or interrupted"
        )

    return reasons


def build_report(result):
    """Build the complete USB qualification report."""

    info = result.get("device_info", {})
    test_link = result.get("test_link", {})
    vbus_stats = result.get("vbus_stats", {})
    usb2 = result.get("usb2_test", {})

    lines = [
        "=== TEST-LINK ===",
        f"{'Reference':<20}: {test_link.get('reference', 'N/A')}",
        f"{'Mating cycle':<20}: "
        f"{test_link.get('cycles', 'N/A')} / "
        f"{test_link.get('max_cycles', 'N/A')}",
        "",
        build_device_info_text(info),
        "",
        "=== PERFORMANCE ===",
        "",
    ]

    # Performance statistics.
    for label, avg, intra, inter in (
        (
            "Write",
            result["avg_write"],
            result["write_intra_stats"],
            result["write_inter_stats"],
        ),
        (
            "Read",
            result["avg_read"],
            result["read_intra_stats"],
            result["read_inter_stats"],
        ),
    ):
        lines.append(label)

        if intra["n"] > 0:
            lines.append(f"  {'Global average':<22}: {avg:8.2f} MB/s")
        else:
            lines.append(f"  {'Global average':<22}: N/A")

        if intra["std"] is not None:
            lines.extend([
                f"  {'Intra-cycle STD':<22}: {intra['std']:8.2f} MB/s",
                f"  {'Intra-cycle CV':<22}: {intra['cv']:8.2f} %",
            ])
        else:
            lines.append(f"  {'Intra-cycle STD / CV':<22}: N/A")

        if inter["std"] is not None:
            lines.extend([
                f"  {'Inter-cycle STD':<22}: {inter['std']:8.2f} MB/s",
                f"  {'Inter-cycle CV':<22}: {inter['cv']:8.2f} %",
            ])
        else:
            lines.append(f"  {'Inter-cycle STD / CV':<22}: N/A")

        lines.append("")

    # Integrity.
    integrity = (
        result["integrity_errors"]
        if result["read_intra_stats"]["n"] > 0
        else "N/A"
    )

    lines.extend([
        "=== INTEGRITY ===",
        f"{'Integrity errors':<20}: {integrity}",
        "",
    ])

    # VBUS measurements.
    lines.extend([
        "=== VBUS MEASUREMENTS ===",
        "",
    ])

    if result.get("vbus_error"):
        lines.extend([
            f"Monitoring warning: {result['vbus_error']}",
            "",
        ])

    for phase_name in ("IDLE", "WRITE", "READ"):
        stats = vbus_stats.get(phase_name)

        lines.append(phase_name)

        if not stats or stats["n"] == 0:
            lines.extend([
                "  No measurements available",
                "",
            ])
            continue

        lines.extend([
            f"  {'Samples':<18}: {stats['n']}",
            f"  {'Voltage min':<18}: {stats['voltage_min']:.3f} V",
            f"  {'Voltage average':<18}: {stats['voltage_avg']:.3f} V",
            f"  {'Voltage max':<18}: {stats['voltage_max']:.3f} V",
            f"  {'Current min':<18}: {stats['current_min']:.2f} mA",
            f"  {'Current average':<18}: {stats['current_avg']:.2f} mA",
            f"  {'Current max':<18}: {stats['current_max']:.2f} mA",
            "",
        ])

    # Reformatting.
    lines.extend([
        "=== REFORMATTING ===",
        result.get("reformat", "N/A"),
        "",
    ])

    # USB2 fallback.
    lines.extend([
        "=== USB 2.0 FALLBACK ===",
        "",
        f"{'Result':<22}: {usb2.get('result', 'NOT PERFORMED')}",
    ])

    if usb2.get("speed_mbps") is not None:
        lines.append(
            f"{'Negotiated speed':<22}: {usb2['speed_mbps']:g} Mb/s"
        )

    if usb2.get("error"):
        lines.append(f"{'Error':<22}: {usb2['error']}")

    lines.append("")

    # Final verdict.
    lines.extend([
        "================= FINAL RESULT =================",
        "",
        result["result"],
    ])

    if result["result"] != "PASS":
        lines.extend(
            f"- {reason}"
            for reason in get_failure_reasons(result)
        )

    lines.extend([
        "",
        "================================================",
    ])

    return "\n".join(lines)


def replace_docx_placeholder(paragraph, key, value):
    """Replace one placeholder in a DOCX paragraph."""

    for run in paragraph.runs:
        if key in run.text:
            run.text = run.text.replace(key, str(value))
            return

    if key in paragraph.text:
        paragraph.text = paragraph.text.replace(key, str(value))


def iter_docx_table_paragraphs(table):
    """Yield all paragraphs from a DOCX table."""

    for row in table.rows:
        for cell in row.cells:
            yield from cell.paragraphs

            for nested_table in cell.tables:
                yield from iter_docx_table_paragraphs(nested_table)


def iter_docx_paragraphs(doc):
    """Yield all paragraphs from the document."""

    yield from doc.paragraphs

    for table in doc.tables:
        yield from iter_docx_table_paragraphs(table)


def write_docx_report(
    template_path,
    output_path,
    report_text,
    figure,
    *,
    of_number=None,
    sap=None,
    device=None,
    serial=None,
    file_size=None,
    block_size=None,
    test_duration=None,
    final_fs=None,
    station="USB Tester",
    soft_version="TBD",
):
    """Generate the DOCX report and convert it to PDF."""

    template_path = Path(template_path)
    output_path = Path(output_path)

    if not template_path.is_file():
        raise FileNotFoundError(f"DOCX template not found: {template_path}")

    doc = Document(str(template_path))

    values = {
        "{{OF}}": of_number or "Not assigned",
        "{{SAP}}": sap or "Not assigned",
        "{{DEV_NAME}}": device or "N/A",
        "{{SERIAL}}": serial or "N/A",
        "{{FILE_SIZE}}": file_size or "N/A",
        "{{BLOCK_SIZE}}": block_size or "N/A",
        "{{TEST_DURATION}}": test_duration or "N/A",
        "{{FINAL_FS}}": final_fs or "N/A",
        "{{STATION}}": station,
        "{{SOFT_VERSION}}": soft_version,
    }

    found_text = False
    found_graph = False

    # Fill all template placeholders.
    for paragraph in iter_docx_paragraphs(doc):

        if "{{RESULT_TEXT}}" in paragraph.text:
            found_text = True
            paragraph.clear()

            for index, line in enumerate(report_text.splitlines()):
                if index:
                    paragraph.add_run().add_break()

                paragraph.add_run(line)

            continue

        if "{{RESULT_GRAPH}}" in paragraph.text:
            found_graph = True
            paragraph.clear()

            image = BytesIO()
            figure.savefig(
                image,
                format="png",
                dpi=180,
                bbox_inches="tight",
            )
            image.seek(0)

            paragraph.add_run().add_picture(
                image,
                width=Inches(5.8),
            )

            continue

        for key, value in values.items():
            replace_docx_placeholder(paragraph, key, value)

    if not found_text:
        raise RuntimeError("Placeholder {{RESULT_TEXT}} not found in the template")

    if not found_graph:
        raise RuntimeError("Placeholder {{RESULT_GRAPH}} not found in the template")

    # Save atomically through a temporary DOCX file.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(output_path.stem + ".tmp.docx")

    try:
        doc.save(str(temp_path))
        temp_path.replace(output_path)

    finally:
        temp_path.unlink(missing_ok=True)

    # Convert the DOCX report to PDF.
    subprocess.run(
        [
            "libreoffice",
            "--headless",
            "--convert-to", "pdf",
            "--outdir", str(output_path.parent),
            str(output_path),
        ],
        check=True,
        timeout=60,
        capture_output=True,
        text=True,
    )

    return output_path


# ============================================================
# 9. TEST-LINK WEAR COUNTERS
# ============================================================

def load_test_link_counters():
    """Load persistent test-link mating cycle counters."""

    if not TEST_LINK_FILE.exists():
        return {}

    with open(TEST_LINK_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


def save_test_link_counters(counters):
    """Save test-link counters atomically."""

    TEST_LINK_FILE.parent.mkdir(parents=True, exist_ok=True)

    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=TEST_LINK_FILE.parent,
            delete=False,
        ) as file:
            temp_path = file.name

            json.dump(counters, file, indent=4)
            file.flush()
            os.fsync(file.fileno())

        os.chmod(temp_path, 0o644)
        os.replace(temp_path, TEST_LINK_FILE)

    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def increment_test_link_counter(test_link):
    """Increment and save the mating cycle counter of one test link."""

    counters = load_test_link_counters()

    if test_link not in counters:
        counters[test_link] = {
            "cycles": 0,
            "max_cycles": MAX_MATING_CYCLES,
        }

    counters[test_link]["cycles"] += 1
    save_test_link_counters(counters)

    return counters[test_link]["cycles"]