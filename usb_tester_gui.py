#!/usr/bin/env python3

# ============================================================
# USB FLASH DRIVE TESTER - LINUX / PyQt5
# ============================================================
#
# Test automatique de clés USB destiné à la caractérisation
# et au contrôle PASS / FAIL.
#
# Le test :
#   1. détecte et identifie la clé USB ;
#   2. démonte ses partitions ;
#   3. écrit puis relit des données directement sur le périphérique ;
#   4. vérifie l'intégrité des données par SHA-256 ;
#   5. mesure les débits d'écriture et de lecture ;
#   6. reformate et vérifie la clé ;
#   7. génère un CSV, un graphe PNG et un rapport PDF.
#
# Les E/S utilisent O_DIRECT afin de limiter l'influence du cache
# système. Si O_DIRECT n'est pas disponible, le test est FAIL.
#
# PASS si :
#   - aucune erreur d'intégrité ou d'E/S ;
#   - le test est complet ;
#   - les débits respectent les seuils configurés ;
#   - le reformatage final réussit ;
#   - O_DIRECT a été utilisé pendant le test.
#
# ATTENTION : TEST DESTRUCTIF
# Les données présentes sur la clé sont écrasées.
# Le disque système portant "/" est exclu de la détection.
#
# Exécution en root requise.
#
# Dépendances Python :
#   PyQt5, matplotlib, numpy, pyudev
#
# Dépendances système :
#   util-linux, exfatprogs, dosfstools, ntfs-3g
#
# ============================================================

import sys
import os
import csv
import json
import time
import mmap
import shutil
import hashlib
import subprocess
from pathlib import Path
from datetime import datetime

import numpy as np
import matplotlib
matplotlib.use("Qt5Agg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_pdf import PdfPages

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QLabel, 
    QProgressBar, QPlainTextEdit, QSplitter, QPushButton
)

import pyudev

# ============================================================
# CONFIGURATION
# ============================================================

# Mode d'affichage :
#   True  -> version DÉVELOPPEMENT : toutes les infos (étapes du test,
#            mesures live, zone de texte détaillée, graphe)
#   False -> version PRODUCTION : uniquement la bannière
#            (Insérez la clé USB / Test en cours... / PASS / FAIL)
#            et la ligne de statut juste en dessous
DEV_MODE = True

# Condition d'arrêt du test : "duration" (durée fixe) ou "cycles"
# (nombre de cycles écriture+lecture complets).
TEST_LIMIT_MODE = "duration"

TEST_DURATION_MIN = .5      # utilisé si TEST_LIMIT_MODE == "duration"
TEST_CYCLES = 2            # utilisé si TEST_LIMIT_MODE == "cycles"

TEST_FILE_SIZE_MB = 512   # quantité de données écrite/lue par cycle
BLOCK_SIZE_MB = 4

# Critères PASS / FAIL
MIN_WRITE_MBPS = 100.0
MIN_READ_MBPS = 170.0
MAX_READ_MBPS = 500.0

# Reformatage automatique de la clé à la fin du test.
# Valeurs possibles : "exfat", "fat32", "ntfs"
REFORMAT_FS = "fat32"
REFORMAT_LABEL = "FISCHER"


BLOCK_SIZE = BLOCK_SIZE_MB * 1024 * 1024
FILE_SIZE = int(TEST_FILE_SIZE_MB * 1024 ** 2)
NUMBER_BLOCKS = FILE_SIZE // BLOCK_SIZE
TEST_DURATION = TEST_DURATION_MIN * 60

SCRIPT_DIR = Path(__file__).resolve().parent
RESULTS_DIR = SCRIPT_DIR / "USB_results"

# Textes affichés
TXT_INSERT = "Insérez la clé USB"
TXT_TESTING = "Test en cours..."
TXT_PASS = "PASS"
TXT_FAIL = "FAIL"
TXT_DO_NOT_UNPLUG = "Ne pas débrancher svp"
TXT_REMOVE = "Retirer la clé"
TXT_READY = "Clé détectée"
TXT_TOO_MANY = "Plusieurs clés branchées : ne laisser que la clé à tester"
TXT_START = "START"
TXT_INTERRUPT = "TEST INTERROMPU"


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


def calculate_mbps(byte_count, duration):
    if duration <= 0:
        return 0.0
    return byte_count / duration / 1e6


def speed_stats(values):
    n = len(values)

    if n < 2:
        return {
            "n": n,
            "std": None,
            "cv": None,
        }

    arr = np.asarray(values, dtype=float)
    mean = float(arr.mean())
    std = float(arr.std(ddof=1))

    return {
        "n": n,
        "std": std,
        "cv": std / mean * 100.0 if mean > 0 else None,
    }


def safe_name(text):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(text))


def root_mount_device():
    """Retourne le nom de périphérique (ex: 'sda') portant la racine '/'."""
    try:
        out, _, _ = run(["findmnt", "-n", "-o", "SOURCE", "/"])
        src = out.strip()
        if not src:
            return None
        out2, _, _ = run(["lsblk", "-no", "PKNAME", src])
        pk = out2.strip().splitlines()[0] if out2.strip() else ""
        if pk:
            return pk
        return os.path.basename(src).rstrip("0123456789")
    except Exception:
        return None


ROOT_DISK = root_mount_device()


def create_result_paths(devnode, serial):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = (
        f"{stamp}_"
        f"{safe_name(os.path.basename(devnode))}_"
        f"{safe_name(serial)}"
    )

    return {
        "csv": str(RESULTS_DIR / f"USB_test_{tag}.csv"),
        "graph": str(RESULTS_DIR / f"USB_READ_WRITE_{tag}.png"),
        "pdf": str(RESULTS_DIR / f"USB_report_{tag}.pdf"),
    }

# ============================================================
# DÉTECTION USB (pyudev)
# ============================================================

def usb_parent_device(udev_device):
    """Remonte l'arbre pyudev jusqu'au noeud usb_device (VID/PID)."""
    d = udev_device
    while d is not None:
        if d.subsystem == "usb" and d.device_type == "usb_device":
            return d
        d = d.parent
    return None


def scan_usb_candidates():
    ctx = pyudev.Context()
    candidates = []

    for dev in ctx.list_devices(subsystem="block", DEVTYPE="disk"):

        if dev.get("ID_BUS") != "usb":
            continue

        if ROOT_DISK and dev.sys_name == ROOT_DISK:
            continue

        candidates.append(dev.device_node)

    return sorted(set(candidates))

# ============================================================
# INFOS PÉRIPHÉRIQUE
# ============================================================

def lsblk_info(devnode):
    out, _, rc = run([
        "lsblk", "-b", "-J", "-o", "NAME,MODEL,SERIAL,SIZE,VENDOR", devnode
    ])
    info = {}
    if rc == 0 and out:
        try:
            data = json.loads(out)
            info = data["blockdevices"][0]
        except Exception:
            pass
    return info


def get_device_info(devnode):

    info = {
        "Devnode": devnode, "Model": "N/A", "Manufacturer": "N/A", "Serial": "N/A",
        "Size_GB": "N/A", "VID": "N/A", "PID": "N/A", "USBVersion": "N/A",
        "Date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    lb = lsblk_info(devnode)
    if lb:
        info["Model"] = lb.get("model") or "N/A"
        info["Manufacturer"] = lb.get("vendor") or "N/A"
        info["Serial"] = lb.get("serial") or "N/A"
        if lb.get("size"):
            try:
                info["Size_GB"] = round(int(lb["size"]) / 1e9, 2)
            except Exception:
                pass

    try:
        ctx = pyudev.Context()
        udev_dev = pyudev.Devices.from_device_file(ctx, devnode)
        usb_dev = usb_parent_device(udev_dev)
        if usb_dev is not None:
            info["VID"] = (usb_dev.attributes.get("idVendor") or b"").decode(
                errors="ignore").upper() or "N/A"
            info["PID"] = (usb_dev.attributes.get("idProduct") or b"").decode(
                errors="ignore").upper() or "N/A"
            if info["Manufacturer"] in ("N/A", ""):
                m = (usb_dev.attributes.get("manufacturer") or b"").decode(
                    errors="ignore").strip()
                info["Manufacturer"] = m or "N/A"
            if info["Model"] in ("N/A", ""):
                p = (usb_dev.attributes.get("product") or b"").decode(
                    errors="ignore").strip()
                info["Model"] = p or "N/A"
            speed = (usb_dev.attributes.get("speed") or b"").decode(
                errors="ignore").strip()
            info["USBVersion"] = speed_label(speed) or "N/A"
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


def get_device_size(devnode):
    """Taille du périphérique bloc en octets."""
    out, _, rc = run(["blockdev", "--getsize64", devnode])
    if rc == 0 and out.strip().isdigit():
        return int(out.strip())

    fd = os.open(str(devnode), os.O_RDONLY)
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)


def unmount_all_partitions(disk_devnode):
    """Démonte le disque et toutes ses partitions."""
    out, _, _ = run(["lsblk", "-ln", "-o", "NAME", disk_devnode])
    for name in out.split():
        run(["umount", f"/dev/{name}"])


# ============================================================
# TEXTES D'INFO / RAPPORT
# ============================================================

def format_device_info(info):
    fields = (
        ("Date", "Date"),
        ("Devnode", "Devnode"),
        ("Model", "Model"),
        ("Manufacturer", "Manufacturer"),
        ("Serial", "Serial"),
        ("VID", "VID"),
        ("PID", "PID"),
        ("Size_GB", "Size_GB"),
        ("USBVersion", "USB Version"),
    )

    lines = ["=== PÉRIPHÉRIQUE ==="]

    for key, label in fields:
        lines.append(f"{label:<14}: {info.get(key, 'N/A')}")

    return "\n".join(lines)


def get_fail_reasons(result):
    reasons = []

    if result.get("error"):
        reasons.append(result["error"])

    if result["errors"]:
        reasons.append(f"{result['errors']} erreur(s) d'intégrité")

    if result["avg_write"] > 0 and result["avg_write"] <= MIN_WRITE_MBPS:
        reasons.append("Écriture trop lente")

    if result["avg_read"] > 0:
        if result["avg_read"] <= MIN_READ_MBPS:
            reasons.append("Lecture trop lente")
        elif result["avg_read"] >= MAX_READ_MBPS:
            reasons.append("Lecture trop rapide")

    if not reasons:
        reasons.append("Test incomplet ou interrompu")

    return reasons


def build_report(info, result):
    lines = [
        format_device_info(info),
        "",
        "=== RÉSULTATS ===",
        f"{'Erreurs intégrité':<18}: {result['errors']}",
    ]

    if result.get("error"):
        lines.append(f"{'Message':<18}: {result['error']}")

    lines.append("")

    for label, avg, stats in (
        ("Écriture", result["avg_write"], result["write_stats"]),
        ("Lecture", result["avg_read"], result["read_stats"]),
    ):
        lines.append(label)
        lines.append(f"  {'Moyenne globale':<16}: {avg:8.2f} MB/s")

        if stats["n"] >= 2:
            lines.append(
                f"  {'Std (par cycle)':<16}: "
                f"{stats['std']:8.2f} MB/s   "
                f"(n = {stats['n']} cycles)"
            )

            cv = f"{stats['cv']:8.2f} %" if stats["cv"] is not None else "N/A"
            lines.append(f"  {'CV (par cycle)':<16}: {cv}")
        else:
            lines.append(
                f"  {'Std / CV':<16}: "
                "N/A (moins de 2 cycles mesurés)"
            )

    lines.extend([
        "",
        "=== REFORMATAGE ===",
        result["reformat"],
        "",
        "================= RÉSULTAT FINAL =================",
        "",
    ])

    if result["result"] == "PASS":
        lines.append("PASS")
    else:
        reasons = get_fail_reasons(result)
        lines.append("FAIL")
        lines.extend(f"- {reason}" for reason in reasons)

    lines.extend([
        "",
        "==================================================",
    ])

    return "\n".join(lines)


def write_pdf_report(path, text, figure=None, lines_per_page=85):
    """PDF : pages de texte (monospace) puis le graphe."""
    lines = text.splitlines() or [""]
    with PdfPages(str(path)) as pdf:
        for start in range(0, len(lines), lines_per_page):
            chunk = "\n".join(lines[start:start + lines_per_page])
            fig = Figure(figsize=(8.27, 11.69))   # A4 portrait
            fig.text(0.05, 0.97, chunk, va="top", ha="left",
                     family="monospace", fontsize=7)
            pdf.savefig(fig)
        if figure is not None:
            pdf.savefig(figure)

# ============================================================
# REFORMATAGE
# ============================================================

FS_CONFIG = {
        "exfat": {
            "tool": "mkfs.exfat",
            "args": [],
            "label_arg": "-n",
            "blkid": "exfat",
            "package": "exfatprogs",
        },
        "fat32": {
            "tool": "mkfs.vfat",
            "args": ["-F", "32"],
            "label_arg": "-n",
            "blkid": "vfat",
            "package": "dosfstools",
        },
        "ntfs": {
            "tool": "mkfs.ntfs",
            "args": ["-F"],
            "label_arg": "-n",
            "blkid": "ntfs",
            "package": "ntfs-3g",
        },
    }

def format_device(devnode, fs_type, label=""):
    fs_type = fs_type.lower().strip()

    if fs_type not in FS_CONFIG:
        raise ValueError(f"Système de fichiers non supporté : {fs_type}")

    cfg = FS_CONFIG[fs_type]

    if not shutil.which(cfg["tool"]):
        raise RuntimeError(
            f"{cfg['tool']} introuvable "
            f"(installer le paquet {cfg['package']})."
        )

    unmount_all_partitions(devnode)

    cmd = [cfg["tool"], *cfg["args"]]

    if label:
        cmd += [cfg["label_arg"], label]

    cmd.append(devnode)

    out, err, rc = run(cmd, timeout=120)

    if rc != 0:
        raise RuntimeError(err or out or f"Reformatage {fs_type} échoué")

    run(["udevadm", "settle"], timeout=10)

    out, err, rc = run(
        ["blkid", "-o", "value", "-s", "TYPE", devnode],
        timeout=10,
    )

    detected = out.strip().lower()

    if rc != 0 or detected != cfg["blkid"]:
        raise RuntimeError(
            f"{fs_type.upper()} non détecté après reformatage "
            f"(blkid : {detected or 'inconnu'}, attendu : {cfg['blkid']})"
        )

# ============================================================
# E/S SANS CACHE (O_DIRECT)
# ============================================================

def make_aligned_buffer(size, fill=None):
    """mmap anonyme : toujours aligné sur la taille de page (>= 4096)."""
    buf = mmap.mmap(-1, size)
    if fill is not None:
        buf[:] = fill
    return buf


def open_direct_device(devnode, mode):
    if mode == "w":
        base_flags = os.O_WRONLY | os.O_SYNC
    else:
        base_flags = os.O_RDONLY

    try:
        fd = os.open(str(devnode), base_flags | os.O_DIRECT)
        return fd, True
    except OSError as e:
        if e.errno != 22:  # EINVAL : O_DIRECT non supporté ici
            raise
        fd = os.open(str(devnode), base_flags)
        return fd, False

# ============================================================
# THREAD : SURVEILLANCE USB
# ============================================================

class UsbWatcher(QThread):

    drives_changed = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self._stop_requested = False

    def stop(self):
        self._stop_requested = True

    def run(self):
        context = pyudev.Context()
        monitor = pyudev.Monitor.from_netlink(context)
        monitor.filter_by(subsystem="block")
        monitor.start()

        self.drives_changed.emit(scan_usb_candidates())

        while not self._stop_requested:
            device = monitor.poll(timeout=0.5)

            if device is None:
                continue

            if device.action in ("add", "remove", "change"):
                self.msleep(800)

                if self._stop_requested:
                    break

                self.drives_changed.emit(scan_usb_candidates())

# ============================================================
# THREAD : TEST
# ============================================================

def new_result(devnode):
    return {
        "devnode": devnode, 
        "model": "N/A", 
        "serial": "N/A",
        "csv": "", 
        "graph": "", 
        "pdf": "", 
        "report": "",
        "avg_write": 0.0, 
        "avg_read": 0.0,
        "write_stats": speed_stats([]), 
        "read_stats": speed_stats([]),
        "errors": 0, 
        "error": "",
        "result": "FAIL", 
        "reformat": "",
        "completed": False,
    }


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
        self._last_info = ""

    def abort(self):
        self._abort = True

    def _emit_info(self, text):
        self._last_info = text
        self.info_ready.emit(text)

    def run(self):
        try:
            result = self._run_test()
        except Exception as e:
            result = new_result(self.devnode)
            result["error"] = str(e)

        self.test_done.emit(result)

    def _run_test(self):

        devnode = self.devnode
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)

        # ---------------- Infos périphérique ----------------
        self.status.emit("Lecture des informations du périphérique...")
        info = get_device_info(devnode)

        result = new_result(devnode)
        result["model"] = info["Model"]
        result["serial"] = info["Serial"]

        result.update(create_result_paths(devnode, info["Serial"]))

        self._emit_info(format_device_info(info))

        # ---------------- Accès brut ----------------
        self.status.emit("Démontage / accès brut au périphérique...")
        unmount_all_partitions(devnode)

        try:
            device_size = get_device_size(devnode)
        except Exception as e:
            result["error"] = f"Périphérique inaccessible : {e}"
            return result

        if device_size < FILE_SIZE * 1.1:
            result["error"] = "Périphérique trop petit pour ce test"
            return result

        # ---------------- Données de référence ----------------
        reference_bytes = os.urandom(BLOCK_SIZE)
        reference_hash = hashlib.sha256(reference_bytes).digest()

        write_buffer = make_aligned_buffer(BLOCK_SIZE, fill=reference_bytes)
        read_buffer = make_aligned_buffer(BLOCK_SIZE)

        errors = 0
        total_written = total_read = 0
        write_io_time = read_io_time = 0.0
        cycle_write_speeds = []     # MB/s moyen de chaque phase écriture
        cycle_read_speeds = []      # MB/s moyen de chaque phase lecture
        cycle = 0
        last_pct = -1
        io_failed = False
        direct_ok = True

        csv_handle = open(result["csv"], "w", newline="", encoding="utf-8")
        writer = csv.writer(csv_handle)
        writer.writerow(["Cycle", "Operation", "Block", "Relative_Time_s",
                         "MB/s", "Integrity"])

        global_start = time.perf_counter()

        def elapsed():
            return time.perf_counter() - global_start

        def reached_limit():
            if TEST_LIMIT_MODE == "cycles":
                return cycle >= TEST_CYCLES
            return elapsed() >= TEST_DURATION

        def emit_progress(phase=None, block_number=0):
            nonlocal last_pct

            if TEST_LIMIT_MODE == "cycles":
                frac = (block_number + 1) / NUMBER_BLOCKS
                half = 0.5 * frac
                cyc_frac = (half if phase == "write" else 0.5 + half)
                pct = min(95, int(((cycle - 1) + cyc_frac)/ TEST_CYCLES* 95))

            else:
                pct = min(95, int(elapsed() / TEST_DURATION * 95))

            if pct != last_pct:
                last_pct = pct
                self.progress.emit(pct)

        try:
            while (not reached_limit() and not self._abort
                   and not io_failed):

                cycle += 1

                if cycle > 1:
                    self.sample.emit("SEP", float("nan"), float("nan"))

                label_cycle = (f"{cycle}/{TEST_CYCLES}"
                              if TEST_LIMIT_MODE == "cycles" else str(cycle))

                # ================= WRITE =================
                self.status.emit(f"Cycle {label_cycle} - ÉCRITURE")

                try:
                    written, write_time, phase_direct = self._write_cycle(
                        devnode,
                        write_buffer,
                        cycle,
                        writer,
                        elapsed,
                        emit_progress,
                    )

                    total_written += written
                    write_io_time += write_time
                    direct_ok &= phase_direct

                    if write_time > 0:
                        cycle_write_speeds.append(
                            calculate_mbps(written, write_time)
                        )

                except Exception as e:
                    io_failed = True
                    result["error"] = f"Erreur écriture : {e}"

                if io_failed or self._abort:
                    break
                if TEST_LIMIT_MODE == "duration" and elapsed() >= TEST_DURATION:
                    break

                # ================= READ =================
                self.status.emit(f"Cycle {label_cycle} - LECTURE")

                try:
                    read_bytes, read_time, read_errors, phase_direct = self._read_cycle(
                        devnode,
                        read_buffer,
                        reference_hash,
                        cycle,
                        writer,
                        elapsed,
                        emit_progress,
                        errors,
                    )

                    total_read += read_bytes
                    read_io_time += read_time
                    errors += read_errors
                    direct_ok &= phase_direct

                    if read_time > 0:
                        cycle_read_speeds.append(
                            calculate_mbps(read_bytes, read_time)
                        )

                except Exception as e:
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

        completed = (not self._abort 
                     and not io_failed
                     and total_written > 0
                     and total_read > 0)

        result.update({
            "avg_write": avg_write,
            "avg_read": avg_read,
            "write_stats": speed_stats(cycle_write_speeds),
            "read_stats": speed_stats(cycle_read_speeds),
            "errors": errors,
            "completed": completed,
            "result": "PASS" if (
                errors == 0
                and completed
                and avg_write > MIN_WRITE_MBPS
                and MIN_READ_MBPS < avg_read < MAX_READ_MBPS
            ) else "FAIL",
        })
        if self._abort and not result["error"]:
            result["error"] = "Test interrompu"
        if not direct_ok and not result["error"]:
            result["error"] = ("O_DIRECT non supporté "
                               "(résultats via cache, à relativiser)")


        # ---------------- Reformatage ----------------
        self.status.emit(f"Reformatage en {REFORMAT_FS.upper()}...")
        reformat_ok = False
        try:
            format_device(devnode, REFORMAT_FS, REFORMAT_LABEL)
            reformat_msg = (f"OK - {devnode} reformaté en {REFORMAT_FS.upper()} "
                            "et vérifié.")
            reformat_ok = True
        except Exception as e:
            reformat_msg = f"ÉCHEC : {e}"
            msg = f"Reformatage échoué : {e}"
            result["error"] = (f"{result['error']} ; {msg}"
                               if result["error"] else msg)

        # PASS seulement si la clé est reformatée, vérifiée et utilisable
        if not reformat_ok or not direct_ok:
            result["result"] = "FAIL"

        result["reformat"] = reformat_msg

        report = build_report(info, result)
        result["report"] = report
        self._emit_info(report)

        return result


    def _write_cycle(
    self,
    devnode,
    buffer,
    cycle,
    writer,
    elapsed,
    emit_progress,
):
        bytes_done = 0
        io_time = 0.0
        direct_ok = True

        fd, direct_ok = open_direct_device(devnode, "w")

        try:
            for block_number in range(NUMBER_BLOCKS):
                if self._abort:
                    break

                t0 = time.perf_counter_ns()
                written = os.writev(fd, [buffer])
                t1 = time.perf_counter_ns()

                if written != BLOCK_SIZE:
                    raise OSError(
                        f"Écriture partielle ({written} octets)"
                    )

                duration = (t1 - t0) / 1e9
                mbps = calculate_mbps(written, duration)
                rel = elapsed()

                bytes_done += written
                io_time += duration

                writer.writerow([
                    cycle,
                    "WRITE",
                    block_number,
                    rel,
                    mbps,
                    1,
                ])

                self.sample.emit("WRITE", rel, mbps)
                emit_progress("write", block_number)

                if (
                    TEST_LIMIT_MODE == "duration"
                    and elapsed() >= TEST_DURATION
                ):
                    break

            os.fsync(fd)

        finally:
            os.close(fd)

        return bytes_done, io_time, direct_ok
    

    def _read_cycle(
        self,
        devnode,
        buffer,
        reference_hash,
        cycle,
        writer,
        elapsed,
        emit_progress,
        initial_errors=0,
    ):
        bytes_done = 0
        io_time = 0.0
        errors_found = 0

        fd, direct_ok = open_direct_device(devnode, "r")

        try:
            for block_number in range(NUMBER_BLOCKS):
                if self._abort:
                    break

                # Mesure uniquement l'E/S.
                t0 = time.perf_counter_ns()
                n = os.readv(fd, [buffer])
                t1 = time.perf_counter_ns()

                if n == 0:
                        raise OSError(f"Lecture interrompue au bloc {block_number}")

                duration = (t1 - t0) / 1e9
                mbps = calculate_mbps(n, duration)

                bytes_done += n
                io_time += duration

                # Contrôle d'intégrité hors chronométrage.
                received = bytes(buffer[:n])
                received_hash = hashlib.sha256(received).digest()

                if n != BLOCK_SIZE or received_hash != reference_hash:
                    errors_found += 1
                    integrity = 0
                else:
                    integrity = 1

                rel = elapsed()
                total_errors = initial_errors + errors_found

                writer.writerow([
                    cycle,
                    "READ",
                    block_number,
                    rel,
                    mbps,
                    integrity,
                ])

                self.sample.emit("READ", rel, mbps)
                emit_progress("read", block_number)

                if (
                    TEST_LIMIT_MODE == "duration"
                    and elapsed() >= TEST_DURATION
                ):
                    break

        finally:
            os.close(fd)

        return bytes_done, io_time, errors_found, direct_ok

# ============================================================
# GUI
# ============================================================

WAITING_INSERT, READY, TESTING, WAITING_REMOVAL, WAITING_RESTART = range(5)

COLORS = {
    "wait": "#1f4e79",
    "test": "#b26a00",
    "pass": "#1e7e34",
    "fail": "#b02a37",
}

SIZE_TXT_BANNER = 44
SIZE_TXT_STATUS = 11
SIZE_TXT_DEV_STATUS = 10
SIZE_TXT_BUTTON = 16
SIZE_TXT_INFO = 9

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Test flash drive")
        if DEV_MODE:
            self.setWindowState(self.windowState() | Qt.WindowMaximized)
        else:
            self.resize(550, 300)

        self.state = WAITING_INSERT
        self.usb_devnodes = []
        self.current_devnode = None
        self.worker = None
        self.graph_dirty = False
        self.history = []          # lignes d'historique (plus récent d'abord)
        self.current_info = ""     # texte du test courant (zone de gauche)
        self._ready_snapshot = None

        self._reset_series()
        self._build_ui()
        self._set_banner(TXT_INSERT, COLORS["wait"])
        self._set_user_status("")

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

        # ----- Éléments communs (prod + dev) -----
        self.banner = QLabel()
        self.banner.setAlignment(Qt.AlignCenter)
        if DEV_MODE:
            self.banner.setMinimumHeight(SIZE_TXT_BANNER + 20)
            root.addWidget(self.banner)
        else:
            self.banner.setMinimumHeight(SIZE_TXT_BANNER + 20)
            root.addWidget(self.banner, 1)      # la bannière remplit la fenêtre

        self.status_label = QLabel("")
        self.status_label.setFont(QFont("Sans", SIZE_TXT_STATUS))
        self.status_label.setAlignment(Qt.AlignCenter)
        root.addWidget(self.status_label)

        self.status_label.setWordWrap(True)

        self.start_button = QPushButton(TXT_START)
        self.start_button.setFont(
            QFont("Sans", SIZE_TXT_BUTTON, QFont.Bold))
        self.start_button.setMinimumHeight(SIZE_TXT_BUTTON + 20)
        self.start_button.setStyleSheet(
            "QPushButton{background-color:#1e7e34;color:white;"
            "border-radius:10px;}"
            "QPushButton:disabled{background-color:#999;color:#ddd;}")
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self.on_start_clicked)
        root.addWidget(self.start_button)

        self.restart_button = QPushButton("RESTART")
        self.restart_button.setFont(QFont("Sans", SIZE_TXT_BUTTON, QFont.Bold))
        self.restart_button.setMinimumHeight(SIZE_TXT_BUTTON + 20)
        self.restart_button.clicked.connect(self.on_restart_clicked)
        self.restart_button.hide()
        root.addWidget(self.restart_button)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)

        # ----- Éléments mode développement -----
        # Toujours créés (les slots y écrivent), ajoutés à la fenêtre
        # seulement en mode développement.
        self.dev_status_label = QLabel("")
        self.dev_status_label.setFont(QFont("Sans", SIZE_TXT_DEV_STATUS))
        self.dev_status_label.setAlignment(Qt.AlignCenter)

        self.info_text = QPlainTextEdit()
        self.info_text.setReadOnly(True)
        self.info_text.setFont(QFont("Monospace", SIZE_TXT_INFO))
        self.info_text.setLineWrapMode(QPlainTextEdit.NoWrap)

        self.figure = Figure(figsize=(8, 5))
        self.canvas = FigureCanvas(self.figure)
        self.ax = self.figure.add_subplot(111)
        self._init_axes()

        root.addWidget(self.progress)

        if DEV_MODE:
            root.addWidget(self.dev_status_label)

            splitter = QSplitter(Qt.Horizontal)
            splitter.addWidget(self.info_text)
            splitter.addWidget(self.canvas)
            splitter.setSizes([550, 750])
            root.addWidget(splitter, 1)

    def _set_banner(self, title, color):
        size = SIZE_TXT_BANNER
        self.banner.setText(
            f"<div style='font-size:{size}px;font-weight:bold'>{title}</div>"
        )
        self.banner.setStyleSheet(
            f"background-color:{color};color:white;border-radius:10px;")

    def _set_user_status(self, text):
        """Ligne juste sous la bannière (prod + dev)."""
        self.status_label.setText(text)

    def _dev_status(self, text):
        """Étapes détaillées du test (visibles uniquement en dev)."""
        if DEV_MODE:
            self.dev_status_label.setText(f"Étape : {text}")

    # ---------------- Zone de texte (gauche) ----------------

    def _render_info(self, text):
        """Texte du test courant + historique de la session."""
        self.current_info = text
        parts = [text.rstrip()] if text else []
        if self.history:
            if parts:
                parts.append("")
            parts.append("=== HISTORIQUE (session) ===")
            parts.append(
                f"{'Heure':<10}{'Device':<11}{'Modèle':<20}{'Serial':<20}"
                f"{'Écr MB/s':>10} {'Lect MB/s':>10} {'Err':>5}  Résultat")
            parts.extend(self.history)
        self.info_text.setPlainText("\n".join(parts))

    def _add_history(self, r):
        line = (
            f"{datetime.now():%H:%M:%S}  {r['devnode']:<11}"
            f"{str(r['model'])[:19]:<20}{str(r['serial']):<20}"
            f"{r['avg_write']:>10.2f} {r['avg_read']:>10.2f} "
            f"{r['errors']:>5}  {r['result']}"
        )
        self.history.insert(0, line)

    # ---------------- Graphe ----------------

    def _reset_series(self):
        self.wt, self.ws, self.rt, self.rs = [], [], [], []

    def _init_axes(self):
        self.ax.clear()

        self.ax.set_xlabel("Test Time [min]")
        self.ax.set_ylabel("Speed [MB/s]")
        self.ax.set_title(
            f"USB Read / Write Speed "
            f"({TEST_FILE_SIZE_MB} MB test, {BLOCK_SIZE_MB} MB blocks)"
        )

        self.ax.grid(True, alpha=0.3)

        # Mesures
        (self.line_w,) = self.ax.plot([], [],linewidth=1,label="Write Speed")
        (self.line_r,) = self.ax.plot([], [],linewidth=1,label="Read Speed")

        # Seuils PASS / FAIL
        self.ax.axhline(
            MIN_WRITE_MBPS,
            linestyle="--",
            color="darkblue",
            label=f"Min Write: {MIN_WRITE_MBPS:g} MB/s")

        self.ax.axhline(
            MIN_READ_MBPS,
            linestyle="--",
            color="darkorange",
            label=f"Min Read: {MIN_READ_MBPS:g} MB/s")

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
            if devnodes:
                self._go_ready(devnodes[0])

        elif self.state == READY:
            if not devnodes:
                self._go_waiting_insert()
            elif (self.current_devnode, tuple(devnodes)) != self._ready_snapshot:
                cur = (self.current_devnode
                       if self.current_devnode in devnodes else devnodes[0])
                self._go_ready(cur)

        elif self.state == WAITING_REMOVAL:
            if self.current_devnode not in self.usb_devnodes:
                self._go_waiting_insert()

    def _device_label(self, info):
        """Ex: '/dev/sdb - SanDisk Ultra Fit - 31.95 GB'."""
        parts = [info["Devnode"]]
        name = " ".join(
            str(x).strip() for x in (info["Manufacturer"], info["Model"])
            if x and str(x).strip() != "N/A")
        if name:
            parts.append(name)
        if info["Size_GB"] != "N/A":
            parts.append(f"{info['Size_GB']} GB")
        return " - ".join(parts)

    def _go_ready(self, devnode):
        """Clé détectée : on affiche le device et on attend START."""
        self.state = READY
        self.current_devnode = devnode
        self._ready_snapshot = (devnode, tuple(self.usb_devnodes))

        info = get_device_info(devnode)
        label = self._device_label(info)
        single = len(self.usb_devnodes) == 1

        self.progress.setValue(0)
        self._set_banner(TXT_READY, COLORS["wait"])
        if single:
            self._set_user_status(label)
        else:
            others = ", ".join(self.usb_devnodes)
            self._set_user_status(f"{TXT_TOO_MANY}\n({others})")
        self._dev_status("clé détectée, en attente de START")
        self._render_info(format_device_info(info))
        self.start_button.setEnabled(single)

    def _go_waiting_insert(self):
        self.state = WAITING_INSERT
        self.current_devnode = None
        self._ready_snapshot = None
        self.start_button.setEnabled(False)
        self.restart_button.hide()
        self.progress.setValue(0)
        self._set_banner(TXT_INSERT, COLORS["wait"])
        self._set_user_status("")
        self._dev_status("en attente d'une clé")

        if self.usb_devnodes:
            self._go_ready(self.usb_devnodes[0])

    def on_start_clicked(self):
        if (self.state != READY
                or len(self.usb_devnodes) != 1
                or self.current_devnode not in self.usb_devnodes):
            return
        self.start_button.setEnabled(False)
        self.start_test(self.current_devnode)

    def on_restart_clicked(self):
        if self.state != WAITING_RESTART:
            return

        self.restart_button.hide()
        self._go_waiting_insert()

    # ---------------- Test ----------------

    def start_test(self, devnode):
        self.state = TESTING
        self.current_devnode = devnode
        self.restart_button.hide()
        self._reset_series()
        self._init_axes()
        self._render_info("")
        self.progress.setValue(0)
        self._set_banner(TXT_TESTING, COLORS["test"])
        self._set_user_status(TXT_DO_NOT_UNPLUG)

        self.worker = TestWorker(devnode)

        self.worker.status.connect(self._dev_status)
        self.worker.info_ready.connect(self._render_info)
        self.worker.sample.connect(self.on_sample)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.test_done.connect(self.on_test_done)

        # Important:
        self.worker.finished.connect(self.on_worker_finished)

        self.worker.start()

    def on_worker_finished(self):
        self.worker = None

    def on_sample(self, op, t, mbps):
        if op == "SEP":
            nan = float("nan")
            self.wt.append(nan); self.ws.append(nan)
            self.rt.append(nan); self.rs.append(nan)
        elif op == "WRITE":
            self.wt.append(t); self.ws.append(mbps)
        else:
            self.rt.append(t); self.rs.append(mbps)
        self.graph_dirty = True

    def on_test_done(self, r):

        # Graphe final (+ moyennes) -> PNG
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

        # Zone de texte : rapport complet + historique
        self._add_history(r)
        self._render_info(r.get("report") or self.current_info)

        # Le PDF est écrit AVANT la bannière : on laisse d'abord la boucle
        # d'événements repeindre le statut, puis _finalize fait le reste.
        self._dev_status("Génération du rapport...")
        QTimer.singleShot(0, lambda: self._finalize(r, passed))

    def _finalize(self, r, passed):
        self._write_pdf(r)
        self.progress.setValue(100)

        if not r.get("completed", False):
            self.state = WAITING_RESTART
            self._set_banner(TXT_INTERRUPT, COLORS["fail"])
            self._set_user_status("")
            self.restart_button.show()
            return

        self.state = WAITING_REMOVAL
        self.restart_button.hide()
        self._set_banner(TXT_PASS if passed else TXT_FAIL, COLORS["pass"] if passed else COLORS["fail"])
        self._set_user_status(TXT_REMOVE)

        if self.current_devnode not in self.usb_devnodes:
            QTimer.singleShot(1500, self._check_removed)

    def _write_pdf(self, r):
        path = r.get("pdf") or str(
            RESULTS_DIR / f"USB_report_{datetime.now():%Y%m%d_%H%M%S}.pdf")
        try:
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            write_pdf_report(path, r.get("report", ""), self.figure)
            self._dev_status(f"PDF enregistré : {path}")
        except Exception as e:
            self._dev_status(f"PDF échoué : {e}")

    def _check_removed(self):
        if (self.state == WAITING_REMOVAL
                and self.current_devnode not in self.usb_devnodes):
            self._go_waiting_insert()

    # ---------------- Fermeture ----------------

    def closeEvent(self, event):
        self.timer.stop()

        if self.watcher is not None:
            self.watcher.stop()

        if self.worker is not None and self.worker.isRunning():
            self.worker.abort()
            self.worker.wait()

        if self.watcher is not None and self.watcher.isRunning():
            self.watcher.wait()

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
