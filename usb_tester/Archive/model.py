"""Fonctions techniques du test USB : détection, E/S, erreurs et rapports.
Ce module ne gère ni les threads de test ni l'interface graphique.
"""

import os
import sys
import json
import errno
import mmap
import shutil
import subprocess
import numpy as np
import pyudev
from datetime import datetime
from matplotlib.figure import Figure
from matplotlib.backends.backend_pdf import PdfPages
from config import (USB_TEST_PORT, RESULTS_DIR, MIN_WRITE_MBPS, MIN_READ_MBPS, MAX_READ_MBPS)


# ============================================================
# 1. PRIVILÈGES ADMINISTRATEUR
# ============================================================


DISPLAY_VARS = [
    "DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE", "DBUS_SESSION_BUS_ADDRESS",
]


def ensure_root():
    """Relance le programme principal avec les privilèges administrateur."""
    if os.geteuid() == 0:
        return
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
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
# 2. OUTILS SYSTÈME ET CALCULS
# ============================================================


def run(cmd, timeout=20, input_text=None):
    """Exécute une commande système et retourne stdout, stderr et le code de sortie."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, input=input_text
        )
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except Exception as e:
        return "", str(e), -1


def compute_mbps(byte_count, duration):
    """Calcule le débit en MB/s à partir des octets transférés et de la durée."""
    if duration <= 0:
        return 0.0
    return byte_count / duration / 1e6


def speed_stats(values):
    """Calcule le nombre de mesures, l’écart-type et le coefficient de variation."""
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
    """Remplace les caractères incompatibles avec un nom de fichier."""
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
    """Construit les chemins des fichiers CSV, graphique et PDF du test."""
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
# 3. GESTION CENTRALISÉE DES ERREURS
# ============================================================


def classify_error(exc, operation=""):
    """
    Convertit une exception Python ou système en erreur structurée.
    Retourne :
        code    : identifiant stable pour le programme
        message : description destinée au rapport
        detail  : exception originale pour le diagnostic
    """
    code = "UNEXPECTED_ERROR"
    message = "Erreur inattendue"
    if isinstance(exc, OSError):
        error_map = {
            errno.ENOENT: (
                "FILE_NOT_FOUND",
                "Fichier ou périphérique introuvable"
            ),
            errno.EIO: (
                "IO_ERROR",
                "Erreur d'entrée/sortie"
            ),
            errno.ENODEV: (
                "DEVICE_UNAVAILABLE",
                "Périphérique indisponible"
            ),
            errno.ENXIO: (
                "DEVICE_UNAVAILABLE",
                "Périphérique ou adresse indisponible"
            ),
            errno.ETIMEDOUT: (
                "IO_TIMEOUT",
                "Délai d'entrée/sortie dépassé"
            ),
            errno.EACCES: (
                "PERMISSION_DENIED",
                "Accès refusé"
            ),
            errno.EBUSY: (
                "DEVICE_BUSY",
                "Périphérique occupé"
            ),
            errno.ENOSPC: (
                "NO_SPACE",
                "Espace disponible insuffisant"
            ),
            errno.EINVAL: (
                "INVALID_ARGUMENT",
                "Argument invalide"
            ),
        }
        code, message = error_map.get(
            exc.errno,
            ("OS_ERROR", "Erreur système")
        )
    elif isinstance(exc, ValueError):
        code = "INVALID_VALUE"
        message = "Valeur invalide"
    elif isinstance(exc, subprocess.TimeoutExpired):
        code = "COMMAND_TIMEOUT"
        message = "Délai d'exécution de la commande dépassé"
    elif isinstance(exc, RuntimeError):
        code = "RUNTIME_ERROR"
        message = str(exc)
    if operation:
        message = f"{operation} : {message}"
    return {
        "code": code,
        "message": message,
        "detail": f"{type(exc).__name__}: {exc}",
    }


def set_result_error(result, exc, operation=""):
    """
    Enregistre une exception dans le dictionnaire de résultat.
    """
    error = classify_error(exc, operation)
    result["error"] = error["message"]
    result["error_code"] = error["code"]
    result["error_detail"] = error["detail"]
    result["result"] = "FAIL"
    result["completed"] = False


# ============================================================
# 4. DÉTECTION ET IDENTIFICATION USB
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
    """Liste les périphériques de stockage USB en excluant le disque système."""
    ctx = pyudev.Context()
    candidates = []
    for dev in ctx.list_devices(subsystem="block", DEVTYPE="disk"):
        if dev.get("ID_BUS") != "usb":
            continue
        # Ignorer les périphériques branchés sur d'autres ports USB
        id_path = dev.get("ID_PATH", "")

        if not id_path.startswith(USB_TEST_PORT + ":"):
            continue
        if ROOT_DISK and dev.sys_name == ROOT_DISK:
            continue
        candidates.append(dev.device_node)
    return sorted(set(candidates))


def get_diskseq(devnode):
    """Identifie une instance de disque, indépendamment de /dev/sdX."""
    try:
        device = pyudev.Devices.from_device_file(
            pyudev.Context(), devnode
        )
        return int(device.properties["DISKSEQ"])
    except (KeyError, ValueError, OSError, pyudev.DeviceNotFoundError) as exc:
        raise RuntimeError(
            f"Impossible d'identifier le périphérique {devnode}"
        ) from exc


def assert_same_device(devnode, expected_diskseq):
    """Refuse de poursuivre si le disque a été remplacé."""
    current_diskseq = get_diskseq(devnode)
    if current_diskseq != expected_diskseq:
        raise RuntimeError(
            f"Périphérique remplacé sur {devnode} : "
            f"DISKSEQ attendu={expected_diskseq}, "
            f"actuel={current_diskseq}"
        )

def assert_test_port(devnode):
    """Vérifie que le disque est connecté au port USB réservé."""
    ctx = pyudev.Context()
    dev = pyudev.Devices.from_device_file(ctx, devnode)
    id_path = dev.get("ID_PATH", "")
    if not id_path.startswith(USB_TEST_PORT + ":"):
        raise RuntimeError(
            f"Port USB non autorisé : {devnode} "
            f"(ID_PATH={id_path})"
        )


# ============================================================
# 5. INFORMATIONS DU PÉRIPHÉRIQUE
# ============================================================


def lsblk_info(devnode):
    """Récupère les informations du disque fournies par lsblk."""
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
    """Collecte les informations du périphérique depuis lsblk et pyudev."""
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

def collect_mounts(device, mounts):
    """Ajoute récursivement les périphériques possédant un point de montage."""
    if device.get("mountpoint"):
        mounts.append(device["path"])
    for child in device.get("children", []):
        collect_mounts(child, mounts)


def get_mounts(devnode, expected_diskseq):
    """Retourne les partitions montées après vérification de l'identité USB."""
    assert_same_device(devnode, expected_diskseq)

    out, err, rc = run([
        "lsblk", "-J", "-p", "-o", "PATH,TYPE,MOUNTPOINT", devnode
    ])
    if rc != 0:
        raise RuntimeError(f"Impossible de lire les partitions : {err or out}")

    devices = json.loads(out)["blockdevices"]
    mounts = []
    for device in devices:
        collect_mounts(device, mounts)
    return mounts


def unmount_all_partitions(devnode, expected_diskseq):
    """Démonte toutes les partitions du périphérique et vérifie le résultat."""
    for partition in reversed(get_mounts(devnode, expected_diskseq)):
        assert_same_device(devnode, expected_diskseq)
        out, err, rc = run(["umount", partition])
        if rc != 0:
            raise RuntimeError(f"Impossible de démonter {partition} : {err or out}")

    remaining = get_mounts(devnode, expected_diskseq)
    if remaining:
        raise RuntimeError("Partitions encore montées : " + ", ".join(remaining))


# ============================================================
# 6. GÉNÉRATION DES RAPPORTS
# ============================================================


def format_device_info(info):
    """Met en forme les informations du périphérique pour le rapport."""
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
    """Construit les motifs de non-conformité à partir du résultat."""
    reasons = []
    if result.get("error"):
        reasons.extend(result["error"].splitlines())
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
        if result.get("reformat", "").startswith("ÉCHEC"):
            reasons.append("Échec du reformatage (voir section REFORMATAGE)")
        else:
            reasons.append("Test incomplet ou interrompu")
    return reasons


def build_report(info, result):
    """Assemble le rapport textuel des performances et du verdict."""
    lines = [
        format_device_info(info),
        "",
        "=== PERFORMANCES ===",
        "",
    ]
    # ---------------- Performances ----------------
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
    # ---------------- Intégrité ----------------
    lines.extend([
        "=== INTÉGRITÉ ===",
        f"{'Erreurs intégrité':<20}: "
        f"{result['errors'] if result['read_stats']['n'] > 0 else 'N/A'}",
        "",
    ])
    # ---------------- Reformatage ----------------
    lines.extend([
        "=== REFORMATAGE ===",
        result.get("reformat", "N/A"),
        "",
    ])
    # ---------------- Résultat final ----------------
    lines.extend([
        "================= RÉSULTAT FINAL =================",
        "",
    ])
    if result["result"] == "PASS":
        lines.append("PASS")
    else:
        lines.append("FAIL")
        lines.extend(f"- {reason}" for reason in get_fail_reasons(result))
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
# 7. REFORMATAGE DU PÉRIPHÉRIQUE
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


def execute_device_command(devnode, expected_diskseq, cmd, timeout=30, input_text=None):
    """Exécute une commande système après vérification de l'identité USB."""
    assert_same_device(devnode, expected_diskseq)
    out, err, rc = run(cmd, timeout=timeout, input_text=input_text)
    if rc != 0:
        raise RuntimeError(f"Commande échouée : {' '.join(cmd)}\n{err or out}")
    return out


def do_format_device(devnode, fs_type, label="", expected_diskseq=None):
    """Recrée une partition, la formate et vérifie le système de fichiers."""
    if expected_diskseq is None:
        raise RuntimeError("Reformatage interdit : DISKSEQ non fourni")
    fs_type = fs_type.lower().strip()
    if fs_type not in FS_CONFIG:
        raise ValueError(f"Système de fichiers non supporté : {fs_type}")
    cfg = FS_CONFIG[fs_type]
    # Vérification des dépendances
    for tool in (cfg["tool"], "wipefs", "sfdisk", "partprobe", "blkid"):
        if not shutil.which(tool):
            raise RuntimeError(f"Outil introuvable : {tool}")
    # 1. Supprimer les anciennes signatures
    execute_device_command(
        devnode, expected_diskseq,
        ["wipefs", "-a", devnode]
    )
    # 2. Créer une table MBR et une partition primaire
    partition_type = {
        "fat32": "c",
        "exfat": "7",
        "ntfs": "7",
    }[fs_type]
    script = (
        "label: dos\n"
        "start=2048, type=" + partition_type + "\n"
    )
    execute_device_command(
        devnode, expected_diskseq,
        ["sfdisk", "--wipe", "always", devnode],
        input_text=script,
    )
    # 3. Actualiser la table de partitions
    execute_device_command(devnode, expected_diskseq, ["partprobe", devnode])
    execute_device_command(devnode, expected_diskseq, ["udevadm", "settle"], timeout=15)
    # 4. Identifier la première partition
    out = execute_device_command(devnode, expected_diskseq, [
        "lsblk", "-ln", "-o", "PATH,TYPE", devnode
    ])
    partitions = [
        line.split()[0]
        for line in out.splitlines()
        if len(line.split()) == 2 and line.split()[1] == "part"
    ]
    if len(partitions) != 1:
        raise RuntimeError(
            f"Partition unique non détectée sur {devnode}"
        )
    partition = partitions[0]
    # 5. Formater la partition
    cmd = [cfg["tool"], *cfg["args"]]
    if label:
        cmd += [cfg["label_arg"], label]
    cmd.append(partition)
    execute_device_command(devnode, expected_diskseq, cmd, timeout=120)
    # 6. Synchroniser les écritures
    execute_device_command(devnode, expected_diskseq, ["sync"], timeout=60)
    # 7. Vérifier le système de fichiers
    detected = execute_device_command(devnode, expected_diskseq, [
        "blkid", "-p", "-o", "value", "-s", "TYPE", partition
    ]).strip().lower()
    if detected != cfg["blkid"]:
        raise RuntimeError(
            f"{fs_type.upper()} non détecté après reformatage "
            f"(blkid : {detected or 'inconnu'}, "
            f"attendu : {cfg['blkid']})"
        )


# ============================================================
# 8. ACCÈS DIRECT AU PÉRIPHÉRIQUE
# ============================================================


def make_aligned_buffer(size, fill=None):
    """mmap anonyme : toujours aligné sur la taille de page (>= 4096)."""
    buf = mmap.mmap(-1, size)
    if fill is not None:
        buf[:] = fill
    return buf


def open_test_device(devnode, expected_diskseq):
    """Ouvre le disque pour les E/S et vérifie son identité."""
    import fcntl
    import struct
    BLKGETDISKSEQ = 0x80081280
    fd = os.open(
        devnode,
        os.O_RDWR | os.O_SYNC | os.O_DIRECT | os.O_EXCL
    )
    try:
        diskseq = struct.unpack(
            "Q",
            fcntl.ioctl(fd, BLKGETDISKSEQ, bytes(8))
        )[0]
        if diskseq != expected_diskseq:
            raise RuntimeError(
                "Le périphérique USB a changé avant son ouverture"
            )
        return fd
    except Exception:
        os.close(fd)
        raise