#!/usr/bin/env python3
# ============================================================
# USB CHARACTERIZATION TOOL - LINUX - GUI PyQt5 (boucle)
#
# Cycle :  Insérez la clé  ->  Test en cours  ->  PASS / FAIL
#          ->  Débranchez la clé  ->  Insérez la clé suivante ...
#
# Doit tourner en ROOT (relance auto via pkexec/sudo si besoin) :
#   - accès direct aux périphériques bloc (/dev/sdX)
#   - montage éventuel de la clé
#   - smartctl (ATA/SAT pass-through)
#
# Dépendances Python : PyQt5, matplotlib, numpy, pyudev
#   pip install PyQt5 matplotlib numpy pyudev
#
# Dépendances système : smartmontools, util-linux (mount/umount, lsblk)
#   sudo apt install smartmontools util-linux
# ============================================================

import sys
import os
import re
import csv
import json
import time
import mmap
import ctypes
import shutil
import hashlib
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime

import numpy as np
import matplotlib
matplotlib.use("Qt5Agg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QProgressBar, QPlainTextEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QSplitter, QAbstractItemView
)

import pyudev

# ============================================================
# CONFIGURATION
# ============================================================

TEST_FILE_SIZE_GB = 0.2
BLOCK_SIZE_MB = 4
TEST_DURATION_MIN = 0.2

DELETE_TEST_FILE_AT_END = True

BLOCK_SIZE = BLOCK_SIZE_MB * 1024 * 1024
FILE_SIZE = int(TEST_FILE_SIZE_GB * 1024 ** 3)
NUMBER_BLOCKS = FILE_SIZE // BLOCK_SIZE
TEST_DURATION = TEST_DURATION_MIN * 60

SCRIPT_DIR = Path(__file__).resolve().parent
RESULTS_DIR = SCRIPT_DIR / "USB_results"
MOUNT_ROOT = Path("/run/usb-tester")

# ============================================================
# ROOT
# ============================================================

DISPLAY_VARS = [
    "DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE", "DBUS_SESSION_BUS_ADDRESS"
]


def ensure_root():
    if os.geteuid() == 0:
        return

    script = os.path.abspath(__file__)
    args = [script] + sys.argv[1:]

    # pkexec et sudo repartent avec un environnement vide par défaut :
    # DISPLAY/XAUTHORITY (X11) ou WAYLAND_DISPLAY/XDG_RUNTIME_DIR
    # (Wayland) doivent être transmis explicitement, sinon Qt ne trouve
    # plus l'affichage une fois relancé en root.
    env_assignments = [
        f"{k}={os.environ[k]}" for k in DISPLAY_VARS if os.environ.get(k)
    ]

    if shutil.which("pkexec"):
        os.execvp(
            "pkexec",
            ["pkexec", "env"] + env_assignments
            + [sys.executable] + args
        )

    if shutil.which("sudo"):
        preserve = ",".join(DISPLAY_VARS)
        os.execvp(
            "sudo",
            ["sudo", f"--preserve-env={preserve}", "-E",
             sys.executable] + args
        )

    print("Ce programme doit être lancé en root "
          "(aucun pkexec/sudo disponible).")
    sys.exit(1)

# ============================================================
# HELPERS SYSTÈME
# ============================================================

def run(cmd, timeout=20, input_text=None):
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, input=input_text
        )
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except Exception as e:
        return "", str(e), -1


def calculate_bitrate(byte_count, duration):
    if duration <= 0:
        return 0.0
    return byte_count / duration / 1e6


def safe_name(text):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(text))


def root_mount_device():
    """Retourne le nom de périphérique (ex: 'sda') portant la racine '/'."""
    try:
        out, _, _ = run(["findmnt", "-n", "-o", "SOURCE", "/"])
        src = out.strip()
        if not src:
            return None
        # /dev/sda2 -> sda   /dev/nvme0n1p1 -> nvme0n1
        out2, _, _ = run(["lsblk", "-no", "PKNAME", src])
        pk = out2.strip().splitlines()[0] if out2.strip() else ""
        if pk:
            return pk
        return os.path.basename(src).rstrip("0123456789")
    except Exception:
        return None


ROOT_DISK = root_mount_device()

# ============================================================
# DÉTECTION USB (pyudev + sysfs)
# ============================================================

def usb_parent_device(udev_device):
    """Remonte l'arbre pyudev jusqu'au noeud usb_device (VID/PID/vitesse)."""
    d = udev_device
    while d is not None:
        if d.subsystem == "usb" and d.device_type == "usb_device":
            return d
        d = d.parent
    return None


def scan_usb_candidates():
    """
    Retourne la liste des devnodes (ex: /dev/sdb1) de partitions ou
    disques USB portant un système de fichiers utilisable.
    """
    ctx = pyudev.Context()
    candidates = []

    for dev in ctx.list_devices(subsystem="block"):

        devtype = dev.get("DEVTYPE")
        if devtype not in ("partition", "disk"):
            continue

        if dev.get("ID_BUS") != "usb":
            continue

        name = dev.sys_name
        base = re.sub(r"\d+$", "", name) if devtype == "partition" else name
        if ROOT_DISK and base == ROOT_DISK:
            continue

        # On ne garde le disque entier que s'il n'a pas de table de
        # partitions (clé "superfloppy", formatée sans partition).
        if devtype == "disk":
            has_children = any(
                c.get("DEVTYPE") == "partition"
                for c in ctx.list_devices(subsystem="block", parent=dev)
            )
            if has_children:
                continue

        if not dev.get("ID_FS_TYPE") and not dev.get("ID_FS_USAGE"):
            # Pas de système de fichiers reconnu -> on ignore
            continue

        candidates.append(dev.device_node)

    return sorted(set(candidates))

# ============================================================
# INFOS PÉRIPHÉRIQUE
# ============================================================

def lsblk_info(devnode):
    out, _, rc = run([
        "lsblk", "-b", "-J", "-o",
        "NAME,PKNAME,FSTYPE,LABEL,MOUNTPOINT,MODEL,SERIAL,TRAN,SIZE,VENDOR",
        devnode
    ])
    info = {}
    if rc == 0 and out:
        try:
            data = json.loads(out)
            info = data["blockdevices"][0]
        except Exception:
            pass
    return info


def get_mount_options(mountpoint):
    """Options de montage effectives, lues dans /proc/mounts."""
    try:
        target = os.path.realpath(mountpoint)
        with open("/proc/mounts", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 4:
                    continue
                if os.path.realpath(parts[1]) == target:
                    return set(parts[3].split(","))
    except Exception:
        pass
    return set()


def base_disk_devnode(devnode):
    out, _, rc = run(["lsblk", "-no", "PKNAME", devnode])
    pk = out.strip().splitlines()[0] if out.strip() else ""
    if pk:
        return f"/dev/{pk}"
    return devnode


def get_device_info(devnode):

    info = {
        "Devnode": devnode, "Model": "N/A", "Manufacturer": "N/A",
        "Serial": "N/A", "BusType": "USB", "Size_GB": "N/A",
        "Health": "Not exposed", "FileSystem": "N/A", "Label": "N/A",
        "VID": "N/A", "PID": "N/A", "USBVersion": "N/A",
        "SpeedMbps": "N/A", "Mountpoint": "", "DiskDevnode": "",
        "MountOptions": ""
    }

    disk_devnode = base_disk_devnode(devnode)
    info["DiskDevnode"] = disk_devnode

    lb = lsblk_info(devnode)
    if lb:
        info["FileSystem"] = lb.get("fstype") or "N/A"
        info["Label"] = lb.get("label") or "(sans label)"
        info["Mountpoint"] = lb.get("mountpoint") or ""
        if info["Mountpoint"]:
            info["MountOptions"] = ",".join(
                sorted(get_mount_options(info["Mountpoint"])))
        if lb.get("size"):
            try:
                info["Size_GB"] = round(int(lb["size"]) / 1e9, 2)
            except Exception:
                pass

    lb_disk = lsblk_info(disk_devnode)
    if lb_disk:
        info["Model"] = lb_disk.get("model") or "N/A"
        info["Manufacturer"] = lb_disk.get("vendor") or "N/A"
        info["Serial"] = lb_disk.get("serial") or "N/A"
        info["BusType"] = lb_disk.get("tran") or "usb"

    try:
        ctx = pyudev.Context()
        udev_dev = pyudev.Devices.from_device_file(ctx, devnode)
        usb_dev = usb_parent_device(udev_dev)
        if usb_dev is not None:
            info["VID"] = (usb_dev.attributes.get("idVendor") or b"").decode(
                errors="ignore").upper() or "N/A"
            info["PID"] = (usb_dev.attributes.get("idProduct") or b"").decode(
                errors="ignore").upper() or "N/A"
            info["USBVersion"] = (
                usb_dev.attributes.get("version") or b""
            ).decode(errors="ignore").strip() or "N/A"
            speed = (usb_dev.attributes.get("speed") or b"").decode(
                errors="ignore").strip()
            info["SpeedMbps"] = speed or "N/A"
            if info["Manufacturer"] in ("N/A", ""):
                m = (usb_dev.attributes.get("manufacturer") or b"").decode(
                    errors="ignore").strip()
                info["Manufacturer"] = m or "N/A"
            if info["Model"] in ("N/A", ""):
                p = (usb_dev.attributes.get("product") or b"").decode(
                    errors="ignore").strip()
                info["Model"] = p or "N/A"
    except Exception:
        pass

    return info


def speed_label(speed_mbps):
    """Traduit la valeur sysfs 'speed' (Mb/s) en nom usuel USB."""
    try:
        v = float(speed_mbps)
    except (TypeError, ValueError):
        return "N/A"
    table = [
        (1.5, "USB 1.0 Low-Speed (1.5 Mb/s)"),
        (12, "USB 1.1 Full-Speed (12 Mb/s)"),
        (480, "USB 2.0 High-Speed (480 Mb/s)"),
        (5000, "USB 3.0/3.1 Gen1 SuperSpeed (5 Gb/s)"),
        (10000, "USB 3.1/3.2 Gen2 SuperSpeed+ (10 Gb/s)"),
        (20000, "USB 3.2 Gen2x2 (20 Gb/s)"),
    ]
    for ref, label in table:
        if abs(v - ref) < ref * 0.05:
            return label
    return f"{v:.0f} Mb/s"

# ============================================================
# MONTAGE
# ============================================================

SLOW_MOUNT_OPTIONS = {"flush", "sync", "dirsync"}


def ensure_mounted(devnode, info):
    """
    Retourne (mountpoint, cree_par_nous:bool).

    Si la clé est déjà montée (typiquement par udisks2) avec une option
    qui force une synchronisation à chaque écriture ("flush" en FAT/
    exFAT, "sync"), on la démonte et on la remonte nous-mêmes sans cette
    option : le contrôle du cache est déjà géré par l'application
    (O_DIRECT / fsync), donc l'empiler avec le "flush" du système de
    fichiers ne fait que doubler la synchronisation et casser le débit
    d'écriture mesuré.
    """
    fstype = info["FileSystem"]

    if info["Mountpoint"]:
        current_opts = get_mount_options(info["Mountpoint"])
        if not (current_opts & SLOW_MOUNT_OPTIONS):
            return info["Mountpoint"], False

        _, err, rc = run(["umount", devnode])
        if rc != 0:
            # Impossible de démonter (déjà utilisée ?) : on garde le
            # montage existant tel quel plutôt que d'échouer le test.
            return info["Mountpoint"], False

    MOUNT_ROOT.mkdir(parents=True, exist_ok=True)
    target = MOUNT_ROOT / safe_name(os.path.basename(devnode))
    target.mkdir(parents=True, exist_ok=True)

    cmd = ["mount", "-o", "rw,noatime"]
    if fstype and fstype != "N/A":
        cmd += ["-t", fstype]
    cmd += [devnode, str(target)]

    _, err, rc = run(cmd)
    if rc != 0:
        raise RuntimeError(f"Montage impossible ({devnode}) : {err}")

    return str(target), True


def cleanup_mount(mountpoint, we_mounted):
    if not we_mounted:
        return
    run(["umount", "-l", mountpoint])
    try:
        Path(mountpoint).rmdir()
    except Exception:
        pass

# ============================================================
# SMART (smartctl)
# ============================================================

def read_smart(disk_devnode):
    """Retourne (dict_interpretation, texte_brut, message_erreur)."""

    if not shutil.which("smartctl"):
        return {}, "", ("smartctl introuvable "
                        "(installer le paquet smartmontools).")

    out, err, rc = run(
        ["smartctl", "-a", "-j", "-d", "sat", disk_devnode], timeout=25
    )

    if not out:
        out2, err2, rc2 = run(
            ["smartctl", "-a", "-j", disk_devnode], timeout=25
        )
        if out2:
            out, err, rc = out2, err2, rc2

    if not out:
        return {}, "", (err or "smartctl n'a rien retourné "
                        "(le boîtier USB ne supporte peut-être pas SAT).")

    try:
        data = json.loads(out)
    except Exception:
        return {}, out, "Réponse smartctl non-JSON (version trop ancienne ?)"

    interp = {
        "Health": "Not exposed", "Temperature": "Not identified",
        "PowerOnHours": "Not identified", "PowerCycles": "Not identified",
        "Wear": "Not identified", "BadBlocks": "Not identified",
        "Uncorrectable": "Not identified",
    }

    health = data.get("smart_status", {}).get("passed")
    if health is not None:
        interp["Health"] = "OK" if health else "FAILED"

    temp = data.get("temperature", {}).get("current")
    if temp is not None:
        interp["Temperature"] = f"{temp} °C"

    poh = data.get("power_on_time", {}).get("hours")
    if poh is not None:
        interp["PowerOnHours"] = f"{poh} h"

    pcc = data.get("power_cycle_count")
    if pcc is not None:
        interp["PowerCycles"] = str(pcc)

    table = data.get("ata_smart_attributes", {}).get("table", [])
    lookup = {a["id"]: a for a in table}

    for aid in (177, 173, 233, 202):  # wear leveling / media wearout usuels
        if aid in lookup:
            raw = lookup[aid].get("raw", {}).get("value")
            interp["Wear"] = (f"{raw} (attribut {aid}, "
                              f"interprétation constructeur requise)")
            break

    if 5 in lookup:
        interp["BadBlocks"] = str(lookup[5].get("raw", {}).get("value"))

    if 198 in lookup:
        interp["Uncorrectable"] = str(
            lookup[198].get("raw", {}).get("value"))

    raw_lines = [f"{'ID':<5}{'Name':<28}{'Value':<7}{'Worst':<7}"
                f"{'Raw':<12}"]
    for a in table:
        raw_lines.append(
            f"{a.get('id', ''):<5}{a.get('name', ''):<28}"
            f"{a.get('value', ''):<7}{a.get('worst', ''):<7}"
            f"{a.get('raw', {}).get('value', ''):<12}"
        )
    raw_text = "\n".join(raw_lines) if table else \
        "Aucun attribut SMART ATA retourné (disque probablement SCSI/UAS)."

    return interp, raw_text, ""


def save_smart_json(disk_devnode, path):
    out, _, rc = run(
        ["smartctl", "-a", "-j", "-d", "sat", disk_devnode], timeout=25
    )
    if out:
        try:
            path.write_text(out, encoding="utf-8")
        except Exception:
            pass


def format_info(info, smart_interp, smart_raw, smart_err):
    L = []
    L.append("=== PÉRIPHÉRIQUE ===")
    for k, label in (
        ("Devnode", "Devnode"), ("DiskDevnode", "Disque"),
        ("Model", "Model"), ("Manufacturer", "Manufacturer"),
        ("Serial", "Serial"), ("BusType", "BusType"),
        ("VID", "VID"), ("PID", "PID"), ("USBVersion", "USB Version"),
        ("SpeedMbps", "Speed (sysfs)"),
        ("FileSystem", "FileSystem"), ("Label", "Label"),
        ("Size_GB", "Size_GB"), ("Mountpoint", "Mountpoint"),
        ("MountOptions", "Mount Options"),
    ):
        L.append(f"{label:<16}: {info[k]}")
    L.append(f"{'Speed':<16}: {speed_label(info['SpeedMbps'])}")

    L.append("")
    L.append("=== SMART (interprétation prudente) ===")
    if smart_interp:
        for k in ("Health", "Temperature", "PowerOnHours", "PowerCycles",
                  "Wear", "BadBlocks", "Uncorrectable"):
            L.append(f"{k:<16}: {smart_interp.get(k, 'Not identified')}")
    else:
        L.append(smart_err or "Non disponible.")

    L.append("")
    L.append("=== SMART RAW (attributs ATA) ===")
    L.append(smart_raw or smart_err or "Non disponible.")

    return "\n".join(L)

# ============================================================
# E/S SANS CACHE (O_DIRECT)
# ============================================================

def make_aligned_buffer(size, fill=None):
    """mmap anonyme : toujours aligné sur la taille de page (>= 4096)."""
    buf = mmap.mmap(-1, size)
    if fill is not None:
        buf[:] = fill
    return buf


def open_direct(path, mode):
    """
    mode: 'w' (création, écriture, write-through) ou 'r' (lecture).
    Retombe sans O_DIRECT si le système de fichiers ne le supporte pas
    (certains montages FAT/exFAT via FUSE, par exemple).
    """
    if mode == "w":
        base_flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_SYNC
    else:
        base_flags = os.O_RDONLY

    try:
        fd = os.open(str(path), base_flags | os.O_DIRECT, 0o644)
        return fd, True
    except OSError as e:
        if e.errno != 22:  # EINVAL : O_DIRECT non supporté ici
            raise
        fd = os.open(str(path), base_flags, 0o644)
        return fd, False

# ============================================================
# THREAD : SURVEILLANCE USB
# ============================================================

class UsbWatcher(QThread):

    drives_changed = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        context = pyudev.Context()
        monitor = pyudev.Monitor.from_netlink(context)
        monitor.filter_by(subsystem="block")
        monitor.start()

        # État initial
        self.drives_changed.emit(scan_usb_candidates())

        while not self._stop:
            device = monitor.poll(timeout=0.5)
            if device is None:
                continue
            if device.action in ("add", "remove", "change"):
                # Laisser le temps au noyau de peupler ID_FS_TYPE etc.
                self.msleep(800)
                self.drives_changed.emit(scan_usb_candidates())

# ============================================================
# THREAD : TEST
# ============================================================

class TestWorker(QThread):

    status = pyqtSignal(str)
    info_ready = pyqtSignal(str)
    sample = pyqtSignal(str, float, float)   # op, temps (s), MB/s
    progress = pyqtSignal(int)
    test_done = pyqtSignal(dict)

    def __init__(self, devnode):
        super().__init__()
        self.devnode = devnode
        self._abort = False

    def abort(self):
        self._abort = True

    def run(self):
        try:
            result = self._run_test()
        except Exception as e:
            result = {
                "result": "FAIL", "error": str(e), "devnode": self.devnode,
                "avg_write": 0.0, "avg_read": 0.0, "errors": 0,
                "disconnects": 1, "model": "N/A", "serial": "N/A",
                "csv": "", "graph": "", "mountpoint": "",
                "we_mounted": False
            }
        self.test_done.emit(result)

    def _run_test(self):

        devnode = self.devnode
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)

        # ---------------- Infos périphérique ----------------
        self.status.emit("Lecture des informations du périphérique...")
        info = get_device_info(devnode)

        result = {
            "devnode": devnode, "model": info["Model"],
            "serial": info["Serial"], "csv": "",
            "graph": "", "avg_write": 0.0, "avg_read": 0.0,
            "errors": 0, "disconnects": 0, "error": "", "result": "FAIL",
            "mountpoint": "", "we_mounted": False
        }

        # ---------------- Montage ----------------
        self.status.emit("Montage de la clé...")
        try:
            mountpoint, we_mounted = ensure_mounted(devnode, info)
        except Exception as e:
            result["error"] = str(e)
            result["disconnects"] = 1
            return result

        # Rafraîchir les infos affichées si on a remonté la clé
        # (ex : suppression de l'option "flush" ajoutée par udisks2)
        info["Mountpoint"] = mountpoint
        info["MountOptions"] = ",".join(
            sorted(get_mount_options(mountpoint)))

        result["mountpoint"] = mountpoint
        result["we_mounted"] = we_mounted
        test_file = Path(mountpoint) / "USB_STRESS_TEST.bin"

        self.status.emit("Lecture SMART...")
        smart_interp, smart_raw, smart_err = read_smart(info["DiskDevnode"])

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        tag = f"{stamp}_{safe_name(os.path.basename(devnode))}_{safe_name(info['Serial'])}"
        csv_file = RESULTS_DIR / f"USB_test_{tag}.csv"
        smart_file = RESULTS_DIR / f"USB_SMART_RAW_{tag}.json"
        graph_file = RESULTS_DIR / f"USB_READ_WRITE_{tag}.png"
        save_smart_json(info["DiskDevnode"], smart_file)

        result["csv"] = str(csv_file)
        result["graph"] = str(graph_file)

        self.info_ready.emit(
            format_info(info, smart_interp, smart_raw, smart_err))

        # ---------------- Espace libre ----------------
        try:
            free = shutil.disk_usage(mountpoint).free
        except Exception as e:
            result["error"] = f"Point de montage inaccessible : {e}"
            result["disconnects"] = 1
            return result

        if free < FILE_SIZE * 1.1:
            result["error"] = "Espace libre insuffisant"
            return result

        # ---------------- Données de référence ----------------
        reference_bytes = os.urandom(BLOCK_SIZE)
        reference_hash = hashlib.sha256(reference_bytes).digest()

        write_buffer = make_aligned_buffer(BLOCK_SIZE, fill=reference_bytes)
        read_buffer = make_aligned_buffer(BLOCK_SIZE)

        errors = 0
        disconnects = 0
        total_written = total_read = 0
        write_io_time = read_io_time = 0.0
        cycle = 0
        last_pct = -1
        io_failed = False
        direct_ok = True

        csv_handle = open(csv_file, "w", newline="", encoding="utf-8")
        writer = csv.writer(csv_handle)
        writer.writerow(["Cycle", "Operation", "Block", "Relative_Time_s",
                         "Bitrate_MB_s", "Integrity", "Errors"])

        global_start = time.perf_counter()

        def elapsed():
            return time.perf_counter() - global_start

        def emit_progress():
            nonlocal last_pct
            pct = min(100, int(elapsed() / TEST_DURATION * 100))
            if pct != last_pct:
                last_pct = pct
                self.progress.emit(pct)

        try:
            while (elapsed() < TEST_DURATION and not self._abort
                   and not io_failed):

                cycle += 1

                if cycle > 1:
                    self.sample.emit("SEP", float("nan"), float("nan"))

                # ================= WRITE =================
                self.status.emit(f"Cycle {cycle} - ÉCRITURE")

                try:
                    fd, direct_ok = open_direct(test_file, "w")
                    try:
                        for block_number in range(NUMBER_BLOCKS):
                            if self._abort:
                                break
                            t0 = time.perf_counter_ns()
                            written = os.writev(fd, [write_buffer])
                            t1 = time.perf_counter_ns()

                            if written != BLOCK_SIZE:
                                raise OSError(
                                    f"Écriture partielle ({written} octets)")

                            duration = (t1 - t0) / 1e9
                            bitrate = calculate_bitrate(written, duration)
                            rel = elapsed()

                            total_written += written
                            write_io_time += duration

                            writer.writerow([cycle, "WRITE", block_number,
                                             rel, bitrate, 1, errors])
                            self.sample.emit("WRITE", rel, bitrate)
                            emit_progress()

                        os.fsync(fd)
                    finally:
                        os.close(fd)

                except Exception as e:
                    disconnects += 1
                    io_failed = True
                    result["error"] = f"Erreur écriture : {e}"

                if io_failed or self._abort or elapsed() >= TEST_DURATION:
                    break

                # ================= READ =================
                self.status.emit(f"Cycle {cycle} - LECTURE")

                try:
                    fd, direct_ok = open_direct(test_file, "r")
                    try:
                        for block_number in range(NUMBER_BLOCKS):
                            if self._abort:
                                break
                            t0 = time.perf_counter_ns()
                            n = os.readv(fd, [read_buffer])
                            t1 = time.perf_counter_ns()

                            if n == 0:
                                break

                            duration = (t1 - t0) / 1e9
                            bitrate = calculate_bitrate(n, duration)

                            total_read += n
                            read_io_time += duration

                            received = bytes(read_buffer[:n])
                            h = hashlib.sha256(received).digest()

                            if n != BLOCK_SIZE or h != reference_hash:
                                errors += 1
                                integrity = 0
                            else:
                                integrity = 1

                            rel = elapsed()
                            writer.writerow([cycle, "READ", block_number,
                                             rel, bitrate, integrity, errors])
                            self.sample.emit("READ", rel, bitrate)
                            emit_progress()

                    finally:
                        os.close(fd)

                except Exception as e:
                    disconnects += 1
                    io_failed = True
                    result["error"] = f"Erreur lecture : {e}"

        finally:
            csv_handle.close()
            write_buffer.close()
            read_buffer.close()

        # ---------------- Statistiques ----------------
        avg_write = (total_written / write_io_time / 1e6
                     if write_io_time else 0.0)
        avg_read = (total_read / read_io_time / 1e6
                    if read_io_time else 0.0)

        completed = (not self._abort and total_written > 0
                     and total_read > 0)

        result.update({
            "avg_write": avg_write, "avg_read": avg_read,
            "errors": errors, "disconnects": disconnects,
            "result": "PASS" if (errors == 0 and disconnects == 0 and completed and avg_read > 180 and avg_write > 100) else "FAIL"
        })
        if self._abort and not result["error"]:
            result["error"] = "Test interrompu"
        if not direct_ok and not result["error"]:
            result["error"] = ("O_DIRECT non supporté par ce montage "
                               "(résultats via cache, à relativiser)")

        # ---------------- Nettoyage ----------------
        if DELETE_TEST_FILE_AT_END:
            try:
                test_file.unlink(missing_ok=True)
            except Exception:
                pass

        return result

# ============================================================
# GUI
# ============================================================

WAITING_INSERT, TESTING, WAITING_REMOVAL = range(3)

COLORS = {
    "wait": "#1f4e79",
    "test": "#b26a00",
    "pass": "#1e7e34",
    "fail": "#b02a37",
}


class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("USB Characterization Tool (Linux)")
        self.resize(1300, 850)

        self.state = WAITING_INSERT
        self.usb_devnodes = []
        self.current_devnode = None
        self.worker = None
        self.pending_cleanup = None
        self.graph_dirty = False
        self.n_pass = 0
        self.n_fail = 0

        self._reset_series()
        self._build_ui()
        self._set_banner("Insérez la clé USB", "En attente d'une clé...",
                         COLORS["wait"])

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh_graph)
        self.timer.start(300)

        self.watcher = UsbWatcher()
        self.watcher.drives_changed.connect(self.on_drives_changed)
        self.watcher.start()

    # ---------------- UI ----------------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        self.banner = QLabel()
        self.banner.setAlignment(Qt.AlignCenter)
        self.banner.setMinimumHeight(130)
        root.addWidget(self.banner)

        self.status_label = QLabel("")
        self.status_label.setFont(QFont("Sans", 11))
        self.status_label.setAlignment(Qt.AlignCenter)
        root.addWidget(self.status_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        root.addWidget(self.progress)

        live = QHBoxLayout()
        self.lbl_write = QLabel("Écriture : -")
        self.lbl_read = QLabel("Lecture : -")
        self.lbl_errors = QLabel("Erreurs : 0")
        self.lbl_counts = QLabel("PASS : 0   FAIL : 0")
        for w in (self.lbl_write, self.lbl_read, self.lbl_errors,
                  self.lbl_counts):
            w.setFont(QFont("Sans", 12, QFont.Bold))
            live.addWidget(w)
        root.addLayout(live)

        splitter = QSplitter(Qt.Horizontal)

        self.info_text = QPlainTextEdit()
        self.info_text.setReadOnly(True)
        self.info_text.setFont(QFont("Monospace", 9))
        self.info_text.setLineWrapMode(QPlainTextEdit.NoWrap)
        splitter.addWidget(self.info_text)

        self.figure = Figure(figsize=(8, 5))
        self.canvas = FigureCanvas(self.figure)
        self.ax = self.figure.add_subplot(111)
        self._init_axes()
        splitter.addWidget(self.canvas)
        splitter.setSizes([450, 850])

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["Heure", "Périphérique", "Modèle", "Serial",
             "Écriture MB/s", "Lecture MB/s", "Erreurs", "Résultat"])
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setMaximumHeight(220)

        vsplit = QSplitter(Qt.Vertical)
        vsplit.addWidget(splitter)
        vsplit.addWidget(self.table)
        vsplit.setSizes([550, 200])
        root.addWidget(vsplit, 1)

    def _set_banner(self, title, sub, color):
        self.banner.setText(
            f"<div style='font-size:44px;font-weight:bold'>{title}</div>"
            f"<div style='font-size:18px'>{sub}</div>"
        )
        self.banner.setStyleSheet(
            f"background-color:{color};color:white;border-radius:10px;")

    # ---------------- Graphe ----------------

    def _reset_series(self):
        self.wt, self.ws, self.rt, self.rs = [], [], [], []

    def _init_axes(self):
        self.ax.clear()
        self.ax.set_xlabel("Test Time [min]")
        self.ax.set_ylabel("Bitrate [MB/s]")
        self.ax.set_title("USB Read / Write Bitrate")
        self.ax.grid(True, alpha=0.3)
        (self.line_w,) = self.ax.plot([], [], linewidth=1,
                                      label="Write Bitrate")
        (self.line_r,) = self.ax.plot([], [], linewidth=1,
                                      label="Read Bitrate")
        self.ax.legend(loc="lower left")
        self.figure.tight_layout()
        self.canvas.draw_idle()

    def _refresh_graph(self):
        if not self.graph_dirty:
            return
        self.graph_dirty = False
        self.line_w.set_data(np.asarray(self.wt) / 60, self.ws)
        self.line_r.set_data(np.asarray(self.rt) / 60, self.rs)
        self.ax.relim()
        self.ax.autoscale_view()
        self.canvas.draw_idle()

    # ---------------- Détection USB ----------------

    def on_drives_changed(self, devnodes):
        self.usb_devnodes = devnodes

        if self.state == WAITING_INSERT:
            if self.usb_devnodes:
                self.start_test(self.usb_devnodes[0])

        elif self.state == WAITING_REMOVAL:
            if self.current_devnode not in self.usb_devnodes:
                self._finish_removal()

    def _go_waiting_insert(self):
        self.state = WAITING_INSERT
        self.current_devnode = None
        self.progress.setValue(0)
        self._set_banner("Insérez la clé USB", "En attente d'une clé...",
                         COLORS["wait"])
        self.status_label.setText("")
        if self.usb_devnodes:
            self.start_test(self.usb_devnodes[0])

    def _finish_removal(self):
        if self.pending_cleanup:
            cleanup_mount(*self.pending_cleanup)
            self.pending_cleanup = None
        self._go_waiting_insert()

    # ---------------- Test ----------------

    def start_test(self, devnode):
        self.state = TESTING
        self.current_devnode = devnode
        self._reset_series()
        self._init_axes()
        self.info_text.clear()
        self.progress.setValue(0)
        self.lbl_write.setText("Écriture : -")
        self.lbl_read.setText("Lecture : -")
        self.lbl_errors.setText("Erreurs : 0")
        self._set_banner("Test en cours...",
                         f"Clé détectée sur {devnode}  -  ne pas débrancher",
                         COLORS["test"])

        self.worker = TestWorker(devnode)
        self.worker.status.connect(self.status_label.setText)
        self.worker.info_ready.connect(self.info_text.setPlainText)
        self.worker.sample.connect(self.on_sample)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.test_done.connect(self.on_test_done)
        self.worker.start()

    def on_sample(self, op, t, mbps):
        if op == "SEP":
            nan = float("nan")
            self.wt.append(nan); self.ws.append(nan)
            self.rt.append(nan); self.rs.append(nan)
        elif op == "WRITE":
            self.wt.append(t); self.ws.append(mbps)
            self.lbl_write.setText(f"Écriture : {mbps:8.2f} MB/s")
        else:
            self.rt.append(t); self.rs.append(mbps)
            self.lbl_read.setText(f"Lecture : {mbps:8.2f} MB/s")
        self.graph_dirty = True

    def on_test_done(self, r):
        self.worker = None

        if r.get("mountpoint"):
            self.pending_cleanup = (r["mountpoint"], r.get("we_mounted", False))

        self._refresh_graph()
        if r["avg_write"]:
            self.ax.axhline(r["avg_write"], linestyle="--", alpha=0.5,
                            color="C0",
                            label=f"Average Write {r['avg_write']:.1f} MB/s")
        if r["avg_read"]:
            self.ax.axhline(r["avg_read"], linestyle="--", alpha=0.5,
                            color="C1",
                            label=f"Average Read {r['avg_read']:.1f} MB/s")
        self.ax.legend(loc="lower left")
        self.canvas.draw()
        if r.get("graph"):
            try:
                self.figure.savefig(r["graph"], dpi=200)
            except Exception:
                pass

        passed = r["result"] == "PASS"
        if passed:
            self.n_pass += 1
        else:
            self.n_fail += 1
        self.lbl_counts.setText(
            f"PASS : {self.n_pass}   FAIL : {self.n_fail}")
        self.lbl_errors.setText(
            f"Erreurs : {r['errors']}   Déconnexions : {r['disconnects']}")
        self.progress.setValue(100)

        detail = (f"Écriture {r['avg_write']:.1f} MB/s  |  "
                  f"Lecture {r['avg_read']:.1f} MB/s")
        if r.get("error"):
            detail += f"  |  {r['error']}"

        self._add_history(r)

        self.state = WAITING_REMOVAL
        self._set_banner(
            f"{'PASS' if passed else 'FAIL'}  -  Débranchez la clé USB",
            detail, COLORS["pass"] if passed else COLORS["fail"])
        self.status_label.setText("Retirez la clé pour passer à la suivante")

        if self.current_devnode not in self.usb_devnodes:
            QTimer.singleShot(1500, self._check_removed)

    def _check_removed(self):
        if (self.state == WAITING_REMOVAL
                and self.current_devnode not in self.usb_devnodes):
            self._finish_removal()

    def _add_history(self, r):
        row = 0
        self.table.insertRow(row)
        vals = [
            datetime.now().strftime("%H:%M:%S"), r["devnode"],
            r["model"], r["serial"], f"{r['avg_write']:.2f}",
            f"{r['avg_read']:.2f}", str(r["errors"]), r["result"]
        ]
        for c, v in enumerate(vals):
            item = QTableWidgetItem(v)
            if c == 7:
                item.setForeground(
                    Qt.darkGreen if r["result"] == "PASS" else Qt.red)
                f = item.font(); f.setBold(True); item.setFont(f)
            self.table.setItem(row, c, item)

    # ---------------- Fermeture ----------------

    def closeEvent(self, event):
        self.watcher.stop()
        if self.worker is not None:
            self.worker.abort()
            self.worker.wait(15000)
        self.watcher.wait(5000)
        if self.pending_cleanup:
            cleanup_mount(*self.pending_cleanup)
        event.accept()


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    ensure_root()
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())
