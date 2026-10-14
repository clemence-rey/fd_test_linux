"""Fonctions techniques du test USB : détection, E/S, erreurs et rapports.
Ce module ne gère ni les threads de test ni l'interface graphique.
"""

import os
import sys
import json
import errno
import mmap
import shutil
import fcntl
import struct
import subprocess
import numpy as np
import pyudev
from datetime import datetime
from matplotlib.figure import Figure
from matplotlib.backends.backend_pdf import PdfPages
from config import (USB_TEST_PORT, RESULTS_DIR, MIN_WRITE_MBPS, MIN_READ_MBPS, MAX_READ_MBPS)


BLKGETDISKSEQ = 0x80081280

# ============================================================
# 1. OUTILS SYSTÈME
# ============================================================

def run_command(cmd, timeout=20, input_text=None):
    """Exécute une commande et retourne (stdout, stderr, returncode)."""
    try:
        result = subprocess.run(
            cmd,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        return result.stdout.strip(), result.stderr.strip(), result.returncode

    except (OSError, subprocess.TimeoutExpired) as e:
        return "", str(e), -1

    
def get_root_disk():
    """Retourne le nom du disque contenant la partition système."""
    source, _, rc = run_command(["findmnt", "-n", "-o", "SOURCE", "/"])

    if rc != 0 or not source:
        return None

    disk, _, rc = run_command(["lsblk", "-no", "PKNAME", source])

    return disk.strip() if rc == 0 and disk else None


ROOT_DISK = get_root_disk()

# ============================================================
# 2. DÉTECTION ET IDENTIFICATION USB
# ============================================================

def scan_usb_candidates():
    """Liste les disques USB connectés au port de test, hors disque système."""
    candidates = []

    for dev in pyudev.Context().list_devices(subsystem="block", DEVTYPE="disk"):
        if dev.get("ID_BUS") != "usb":
            continue

        if not dev.get("ID_PATH", "").startswith(USB_TEST_PORT + ":"):
            continue

        if dev.sys_name == ROOT_DISK:
            continue

        candidates.append(dev.device_node)

    return sorted(candidates)


def get_usb_parent_device(udev_device):
    """Retourne le périphérique USB parent (VID/PID)."""
    return udev_device.find_parent("usb", "usb_device")


def get_diskseq(devnode):
    """Retourne le DISKSEQ du périphérique pour identifier son instance."""
    try:
        device = pyudev.Devices.from_device_file(pyudev.Context(), devnode)
        return int(device.properties["DISKSEQ"])

    except (KeyError, ValueError, OSError, pyudev.DeviceNotFoundError) as exc:
        raise RuntimeError(
            f"Impossible d'identifier le périphérique {devnode}"
        ) from exc


def assert_same_device(devnode, expected_diskseq):
    """Vérifie que le périphérique n'a pas été remplacé."""
    current_diskseq = get_diskseq(devnode)

    if current_diskseq != expected_diskseq:
        raise RuntimeError(
            f"Périphérique remplacé sur {devnode} : "
            f"DISKSEQ attendu={expected_diskseq}, actuel={current_diskseq}"
        )


def assert_test_port(devnode):
    """Vérifie que le périphérique utilise le port USB réservé."""
    dev = pyudev.Devices.from_device_file(pyudev.Context(), devnode)
    id_path = dev.get("ID_PATH", "")

    if not id_path.startswith(USB_TEST_PORT + ":"):
        raise RuntimeError(
            f"Port USB non autorisé : {devnode} (ID_PATH={id_path})"
        )

# ============================================================
# 3. INFORMATIONS DU PÉRIPHÉRIQUE
# ============================================================

def get_device_info(devnode):
    """Récupère les informations du périphérique via lsblk et pyudev."""
    info = {
        "Devnode": devnode,
        "Model": "N/A",
        "Manufacturer": "N/A",
        "Serial": "N/A",
        "Size_GB": "N/A",
        "VID": "N/A",
        "PID": "N/A",
        "USBVersion": "N/A",
        "Date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    # Informations du disque via lsblk
    lb = get_lsblk_info(devnode)

    info["Model"] = lb.get("model") or "N/A"
    info["Manufacturer"] = lb.get("vendor") or "N/A"
    info["Serial"] = lb.get("serial") or "N/A"

    try:
        if lb.get("size"):
            info["Size_GB"] = round(int(lb["size"]) / 1e9, 2)
    except (ValueError, TypeError):
        pass

    # Informations USB via pyudev
    try:
        udev_dev = pyudev.Devices.from_device_file(pyudev.Context(), devnode)
        usb_dev = get_usb_parent_device(udev_dev)

        if usb_dev is None:
            return info

        info["VID"] = (
            (usb_dev.attributes.get("idVendor") or b"")
            .decode(errors="ignore").upper() or "N/A"
        )
        info["PID"] = (
            (usb_dev.attributes.get("idProduct") or b"")
            .decode(errors="ignore").upper() or "N/A"
        )

        if info["Manufacturer"] == "N/A":
            info["Manufacturer"] = (
                (usb_dev.attributes.get("manufacturer") or b"")
                .decode(errors="ignore").strip() or "N/A"
            )

        if info["Model"] == "N/A":
            info["Model"] = (
                (usb_dev.attributes.get("product") or b"")
                .decode(errors="ignore").strip() or "N/A"
            )

        speed = (
            (usb_dev.attributes.get("speed") or b"")
            .decode(errors="ignore").strip()
        )
        info["USBVersion"] = get_usb_speed_label(speed) or "N/A"

    except (OSError, pyudev.DeviceNotFoundError, AttributeError, UnicodeError):
        pass

    return info


def get_lsblk_info(devnode):
    """Retourne les informations du disque fournies par lsblk."""
    out, _, rc = run_command([
        "lsblk", "-b", "-J", "-o", "NAME,MODEL,SERIAL,SIZE,VENDOR", devnode
    ])

    if rc != 0 or not out:
        return {}

    try:
        return json.loads(out)["blockdevices"][0]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return {}


def get_usb_speed_label(speed_mbps):
    """Convertit une vitesse USB en Mb/s en libellé lisible."""
    try:
        speed = float(speed_mbps)
    except (TypeError, ValueError):
        return "N/A"

    labels = {
        1.5: "USB Low-Speed (1.5 Mb/s)",
        12: "USB Full-Speed (12 Mb/s)",
        480: "USB 2.0 High-Speed (480 Mb/s)",
        5000: "USB 3.2 Gen 1 (5 Gb/s)",
        10000: "USB 3.2 Gen 2 (10 Gb/s)",
        20000: "USB 3.2 Gen 2x2 (20 Gb/s)",
    }

    for reference, label in labels.items():
        if abs(speed - reference) < reference * 0.05:
            return label

    return f"{speed:.0f} Mb/s"


def get_device_size_bytes(devnode):
    """Retourne la capacité du périphérique bloc en octets."""
    out, _, rc = run_command(["blockdev", "--getsize64", devnode])

    if rc == 0 and out.isdigit():
        return int(out)

    fd = os.open(devnode, os.O_RDONLY)
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)

# ============================================================
# 4. ACCÈS DIRECT AU PÉRIPHÉRIQUE
# ============================================================

def create_aligned_buffer(size, fill=None):
    """Crée un buffer mmap aligné en mémoire, compatible avec O_DIRECT."""
    buf = mmap.mmap(-1, size)

    if fill is not None:
        buf[:] = fill

    return buf


def open_test_device(devnode, expected_diskseq):
    """Ouvre le disque en E/S directes et vérifie son identité."""
    assert_test_port(devnode)
    assert_same_device(devnode, expected_diskseq)

    fd = os.open(
        devnode,
        os.O_RDWR | os.O_SYNC | os.O_DIRECT | os.O_EXCL
    )

    try:
        diskseq = struct.unpack(
            "Q", fcntl.ioctl(fd, BLKGETDISKSEQ, bytes(8))
        )[0]

        if diskseq != expected_diskseq:
            raise RuntimeError(
                "Le périphérique USB a changé avant son ouverture"
            )

        return fd

    except Exception:
        os.close(fd)
        raise

# ============================================================
# 5. GESTION DES PARTITIONS ET REFORMATAGE
# ============================================================

FILESYSTEM_CONFIG = {
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
            "args": ["-Q"],
            "label_arg": "-n",
            "blkid": "ntfs",
            "package": "ntfs-3g",
        },
    }


def get_mounted_devices(devnode, expected_diskseq):
    """Retourne les périphériques montés appartenant au disque USB."""
    assert_same_device(devnode, expected_diskseq)

    out, err, rc = run_command([
        "lsblk", "-J", "-p", "-l",
        "-o", "PATH,TYPE,MOUNTPOINT", devnode
    ])

    if rc != 0:
        raise RuntimeError(f"Impossible de lire les partitions : {err or out}")

    return [
        device["path"]
        for device in json.loads(out)["blockdevices"]
        if device.get("mountpoint")
    ]


def unmount_device(devnode, expected_diskseq):
    """Démonte tous les systèmes de fichiers montés sur le disque USB."""
    for device in reversed(get_mounted_devices(devnode, expected_diskseq)):
        assert_same_device(devnode, expected_diskseq)

        out, err, rc = run_command(["umount", device])
        if rc != 0:
            raise RuntimeError(
                f"Impossible de démonter {device} : {err or out}"
            )

    remaining = get_mounted_devices(devnode, expected_diskseq)
    if remaining:
        raise RuntimeError(
            f"Périphériques encore montés : {', '.join(remaining)}"
        )


def execute_device_command(devnode, expected_diskseq, cmd, timeout=30, input_text=None):
    """Exécute une commande après vérification du disque et du port USB."""
    assert_same_device(devnode, expected_diskseq)
    assert_test_port(devnode)

    out, err, rc = run_command(cmd, timeout=timeout, input_text=input_text)

    if rc != 0:
        raise RuntimeError(
            f"Commande échouée : {' '.join(cmd)}\n{err or out}"
        )

    return out


def reformat_device(devnode, fs_type, label="", expected_diskseq=None):
    """Recrée une table MBR, une partition unique et son système de fichiers."""

    if expected_diskseq is None:
        raise RuntimeError("Reformatage interdit : DISKSEQ non fourni")

    fs_type = fs_type.lower().strip()

    if fs_type not in FILESYSTEM_CONFIG:
        raise ValueError(f"Système de fichiers non supporté : {fs_type}")

    cfg = FILESYSTEM_CONFIG[fs_type]

    # Vérifier les dépendances
    for tool in (cfg["tool"], "sfdisk", "partprobe", "udevadm", "lsblk", "blkid", "sync"):
        if not shutil.which(tool):
            raise RuntimeError(f"Outil introuvable : {tool}")

    # Créer une table MBR avec une partition unique
    partition_type = {
        "fat32": "c",
        "exfat": "7",
        "ntfs": "7",
    }[fs_type]

    script = f"label: dos\nstart=2048, type={partition_type}\n"

    execute_device_command(
        devnode, expected_diskseq,
        ["sfdisk", "--wipe", "always", devnode],
        input_text=script,
    )

    # Actualiser la table de partitions
    execute_device_command(
        devnode, expected_diskseq,
        ["partprobe", devnode]
    )

    execute_device_command(
        devnode, expected_diskseq,
        ["udevadm", "settle", "--timeout=15"],
        timeout=20
    )

    # Identifier la partition créée
    out = execute_device_command(
        devnode, expected_diskseq,
        ["lsblk", "-ln", "-o", "PATH,TYPE", devnode]
    )

    partitions = [
        parts[0]
        for line in out.splitlines()
        if len(parts := line.split()) == 2 and parts[1] == "part"
    ]

    if len(partitions) != 1:
        raise RuntimeError(
            f"Partition unique non détectée sur {devnode}"
        )

    partition = partitions[0]

    # Formater la partition
    cmd = [cfg["tool"], *cfg["args"]]

    if label:
        cmd.extend([cfg["label_arg"], label])

    cmd.append(partition)

    execute_device_command(
        devnode, expected_diskseq,
        cmd,
        timeout=120
    )

    # Synchroniser les écritures
    execute_device_command(
        devnode, expected_diskseq,
        ["sync"],
        timeout=60
    )

    # Vérifier le système de fichiers
    detected = execute_device_command(
        devnode, expected_diskseq,
        ["blkid", "-p", "-o", "value", "-s", "TYPE", partition]
    ).strip().lower()

    if detected != cfg["blkid"]:
        raise RuntimeError(
            f"{fs_type.upper()} non détecté après reformatage "
            f"(blkid : {detected or 'inconnu'}, attendu : {cfg['blkid']})"
        )

# ============================================================
# 6. CALCULS ET STATISTIQUES
# ============================================================

def compute_mbps(byte_count, duration):
    """Calcule le débit en MB/s à partir des octets transférés et de la durée."""
    if duration <= 0:
        return 0.0
    return byte_count / duration / 1e6


def compute_speed_stats(values):
    """Calcule le nombre de mesures, l'écart-type et le coefficient de variation."""
    n = len(values)

    if n < 2:
        return {"n": n, "std": None, "cv": None}

    arr = np.asarray(values, dtype=float)
    mean = float(arr.mean())
    std = float(arr.std(ddof=1))

    return {
        "n": n,
        "std": std,
        "cv": std / mean * 100 if mean > 0 else None,
    }

# ============================================================
# 7. GESTION DES RÉSULTATS ET DES ERREURS
# ============================================================

def create_test_result(devnode):
    """Initialise le dictionnaire des mesures et du verdict du test."""
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
        "write_stats": compute_speed_stats([]),
        "read_stats": compute_speed_stats([]),
        "errors": 0,
        "error": "",
        "error_code": "",
        "error_detail": "",
        "result": "FAIL",
        "reformat": "",
        "completed": False,
    }


def classify_error(exc, operation=""):
    """Convertit une exception en erreur structurée pour le rapport."""

    if isinstance(exc, OSError):
        error_map = {
            errno.ENOENT: ("FILE_NOT_FOUND", "Fichier ou périphérique introuvable"),
            errno.EIO: ("IO_ERROR", "Erreur d'entrée/sortie"),
            errno.ENODEV: ("DEVICE_UNAVAILABLE", "Périphérique indisponible"),
            errno.ENXIO: ("DEVICE_UNAVAILABLE", "Périphérique ou adresse indisponible"),
            errno.ETIMEDOUT: ("IO_TIMEOUT", "Délai d'entrée/sortie dépassé"),
            errno.EACCES: ("PERMISSION_DENIED", "Accès refusé"),
            errno.EBUSY: ("DEVICE_BUSY", "Périphérique occupé"),
            errno.ENOSPC: ("NO_SPACE", "Espace disponible insuffisant"),
            errno.EINVAL: ("INVALID_ARGUMENT", "Argument invalide"),
        }
        code, message = error_map.get(exc.errno, ("OS_ERROR", "Erreur système"))

    elif isinstance(exc, ValueError):
        code, message = "INVALID_VALUE", "Valeur invalide"

    elif isinstance(exc, subprocess.TimeoutExpired):
        code, message = "COMMAND_TIMEOUT", "Délai d'exécution de la commande dépassé"

    elif isinstance(exc, RuntimeError):
        code, message = "RUNTIME_ERROR", str(exc)

    else:
        code, message = "UNEXPECTED_ERROR", "Erreur inattendue"

    if operation:
        message = f"{operation} : {message}"

    return {
        "code": code,
        "message": message,
        "detail": f"{type(exc).__name__}: {exc}",
    }


def set_result_error(result, exc, operation=""):
    """Enregistre une exception et marque le test comme échoué."""
    error = classify_error(exc, operation)

    result["error"] = error["message"]
    result["error_code"] = error["code"]
    result["error_detail"] = error["detail"]
    result["result"] = "FAIL"
    result["completed"] = False

# ============================================================
# 8. GÉNÉRATION DES RAPPORTS
# ============================================================

def sanitize_filename(text):
    """Génère un nom de fichier avec des caractères ASCII."""
    return "".join(c if c.isascii() and (c.isalnum() or c in "-_") else "_"
                   for c in str(text))


def create_result_paths(devnode, serial):
    """Construit les chemins des rapports du test USB."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = f"{stamp}_{sanitize_filename(os.path.basename(devnode))}_{sanitize_filename(serial)}"

    return {
        "csv": str(RESULTS_DIR / f"USB_test_{tag}.csv"),
        "graph": str(RESULTS_DIR / f"USB_READ_WRITE_{tag}.png"),
        "pdf": str(RESULTS_DIR / f"USB_report_{tag}.pdf"),
    }


def format_device_info(info):
    """Met en forme les informations du périphérique pour le rapport."""
    labels = (
        ("Date", "Date"),
        ("Model", "Model"),
        ("Manufacturer", "Manufacturer"),
        ("Serial", "Serial"),
        ("VID", "VID"),
        ("PID", "PID"),
        ("Size_GB", "Size_GB"),
        ("USBVersion", "USB Version"),
    )

    lines = ["=== PÉRIPHÉRIQUE ==="]

    for key, label in labels:
        lines.append(f"{label:<14}: {info.get(key, 'N/A')}")

    return "\n".join(lines)


def get_failure_reasons(result):
    """Retourne les motifs de non-conformité du test."""
    reasons = []

    if result.get("error"):
        reasons.extend(result["error"].splitlines())

    if result["errors"]:
        reasons.append(f"{result['errors']} erreur(s) d'intégrité")

    if 0 < result["avg_write"] <= MIN_WRITE_MBPS:
        reasons.append("Écriture trop lente")

    if 0 < result["avg_read"] <= MIN_READ_MBPS:
        reasons.append("Lecture trop lente")
    elif result["avg_read"] >= MAX_READ_MBPS:
        reasons.append("Lecture trop rapide")

    if not reasons:
        if result.get("reformat", "").startswith("ÉCHEC"):
            reasons.append("Échec du reformatage (voir section REFORMATAGE)")
        else:
            reasons.append("Test incomplet ou interrompu")

    return reasons


def build_report(info, result):
    """Construit le rapport textuel du test USB."""
    lines = [
        format_device_info(info),
        "",
        "=== PERFORMANCES ===",
        "",
    ]

    # Performances
    for label, avg, stats in (
        ("Écriture", result["avg_write"], result["write_stats"]),
        ("Lecture", result["avg_read"], result["read_stats"]),
    ):
        lines.append(label)

        if stats["n"] > 0:
            lines.append(f"  {'Moyenne globale':<18}: {avg:8.2f} MB/s")
        else:
            lines.append(f"  {'Moyenne globale':<18}: N/A")

        if stats["n"] >= 2:
            lines.append(
                f"  {'Std (par cycle)':<18}: "
                f"{stats['std']:8.2f} MB/s "
                f"(n = {stats['n']} cycles)"
            )

            cv = f"{stats['cv']:8.2f} %" if stats["cv"] is not None else "N/A"
            lines.append(f"  {'CV (par cycle)':<18}: {cv}")
        else:
            lines.append(
                f"  {'Std / CV':<18}: N/A (moins de 2 cycles mesurés)"
            )

        lines.append("")

    # Intégrité
    lines.extend([
        "=== INTÉGRITÉ ===",
        f"{'Erreurs intégrité':<20}: "
        f"{result['errors'] if result['read_stats']['n'] > 0 else 'N/A'}",
        "",
    ])

    # Reformatage
    lines.extend([
        "=== REFORMATAGE ===",
        result.get("reformat", "N/A"),
        "",
    ])

    # Verdict final
    lines.extend([
        "================= RÉSULTAT FINAL =================",
        "",
    ])

    if result["result"] == "PASS":
        lines.append("PASS")
    else:
        lines.append("FAIL")
        lines.extend(f"- {reason}" for reason in get_failure_reasons(result))

    lines.extend([
        "",
        "==================================================",
    ])

    return "\n".join(lines)


def write_pdf_report(path, text, figure=None, lines_per_page=85):
    """Exporte le rapport textuel et son graphique au format PDF."""
    lines = text.splitlines() or [""]

    with PdfPages(str(path)) as pdf:
        for start in range(0, len(lines), lines_per_page):
            chunk = "\n".join(lines[start:start + lines_per_page])

            fig = Figure(figsize=(8.27, 11.69))

            fig.text(
                0.05, 0.97, chunk,
                va="top",
                ha="left",
                family="monospace",
                fontsize=7,
            )

            pdf.savefig(fig)

        if figure is not None:
            pdf.savefig(figure)

