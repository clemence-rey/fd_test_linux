#!/usr/bin/env python3
# ============================================================
# USB CHARACTERIZATION TOOL - LINUX - GUI PyQt5
#
# Outil de caractérisation et de tri de clés USB : mesure des
# débits d'écriture / lecture, vérification d'intégrité des données,
# relevé SMART, puis verdict PASS / FAIL.
#
# ------------------------------------------------------------
# CYCLE D'UTILISATION (boucle)
# ------------------------------------------------------------
#   Insérez la clé -> Clé détectée (device + fabricant affichés)
#   -> appui sur START -> Test en cours -> PASS / FAIL
#   -> Retirer la clé -> Insérez la clé suivante ...
#
#   Le test ne démarre JAMAIS automatiquement : l'opérateur vérifie
#   le device affiché (ex. /dev/sdb - Fabricant Modèle - xx GB) puis
#   appuie sur START. Si plusieurs clés USB sont branchées, START est
#   désactivé jusqu'à ce qu'il n'en reste qu'une.
#
# ------------------------------------------------------------
# DÉROULEMENT D'UN TEST
# ------------------------------------------------------------
#   1. Infos périphérique : modèle, fabricant, série, taille (lsblk),
#      VID/PID et vitesse USB négociée (udev / sysfs).
#   2. SMART "avant" : relevé via smartctl (si la clé le permet).
#   3. Démontage de toutes les partitions de la clé.
#   4. Génération d'un bloc de données aléatoires (os.urandom) et
#      calcul de son empreinte SHA-256 de référence.
#   5. Cycles écriture + lecture, répétés jusqu'à la condition d'arrêt
#      choisie (TEST_LIMIT_MODE) : nombre de cycles ("cycles") ou
#      durée fixe ("duration").
#        - ÉCRITURE : le bloc de référence est écrit en séquence,
#          depuis le début du périphérique, TEST_FILE_SIZE_MB par cycle.
#        - LECTURE  : la même zone est relue bloc par bloc ; chaque bloc
#          lu est haché en SHA-256 et comparé à la référence. Un bloc
#          différent (ou de taille incorrecte) compte comme erreur
#          d'intégrité.
#      Le temps de chaque bloc est mesuré uniquement autour de l'appel
#      système d'E/S (writev / readv) : hachage, CSV et interface
#      graphique sont exclus du chronométrage.
#   6. SMART "après" et comparaison avec le relevé "avant".
#   7. Reformatage : la clé repart directement utilisable.
#   8. Verdict, puis génération des fichiers dans USB_results/ :
#      CSV (une ligne par bloc), graphe PNG des débits, rapport PDF
#      (texte + graphe).
#
# ------------------------------------------------------------
# MÉCANISMES DE MESURE
# ------------------------------------------------------------
#   - Test BRUT sur le périphérique bloc entier (/dev/sdX), sans
#     système de fichiers : on mesure la clé, pas le système de
#     fichiers ni le cache du système.
#   - E/S sans cache : O_DIRECT (contourne le cache de pages) et
#     O_SYNC en écriture, avec fsync en fin de phase. Si O_DIRECT n'est
#     pas supporté par l'adaptateur, le test continue sans lui et le
#     rapport signale que les résultats sont à relativiser.
#   - Débit moyen global : octets totaux / somme des temps d'E/S (MB/s,
#     1 MB = 1e6 octets).
#   - Régularité : écart-type (ddof=1) et coefficient de variation (CV)
#     calculés sur les vitesses moyennes de chaque cycle ; non
#     calculables avec moins de 2 cycles.
#   - Déconnexion : toute erreur d'E/S fatale (écriture ou lecture) est
#     comptée comme une déconnexion et arrête le test.
#
# ------------------------------------------------------------
# CRITÈRES PASS / FAIL
# ------------------------------------------------------------
#   PASS uniquement si TOUTES les conditions sont réunies :
#     - aucune erreur d'intégrité et aucune déconnexion ;
#     - test complet (écriture et lecture effectuées, non interrompu) ;
#     - débit moyen d'écriture > MIN_WRITE_MBPS ;
#     - MIN_READ_MBPS < débit moyen de lecture < MAX_READ_MBPS
#       (une lecture trop rapide est jugée suspecte : cache ou mesure
#       non fiable).
#   Dans les autres cas : FAIL, avec la ou les raisons dans le rapport.
#
# ------------------------------------------------------------
# ATTENTION : DESTRUCTIF
# ------------------------------------------------------------
#   Toute donnée présente sur la clé est perdue dès l'appui sur START
#   (la zone testée est écrasée, puis la clé est reformatée).
#   Ne jamais utiliser sur une clé contenant des données à conserver.
#   Le disque portant la racine "/" est exclu de la détection.
#
# ------------------------------------------------------------
# PRÉREQUIS
# ------------------------------------------------------------
#   Doit tourner en ROOT (relance auto via pkexec/sudo si besoin) :
#     - accès direct aux périphériques bloc (/dev/sdX)
#     - démontage / reformatage
#     - smartctl (ATA/SAT pass-through)
#
#   Dépendances Python : PyQt5, matplotlib, numpy, pyudev
#     pip install PyQt5 matplotlib numpy pyudev
#
#   Dépendances système : smartmontools, util-linux, exfatprogs, dosfstools, ntfs-3g
#     sudo apt install smartmontools util-linux exfatprogs dosfstools ntfs-3g
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
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QProgressBar, QPlainTextEdit, QSplitter, QPushButton
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

TEST_DURATION_MIN = 1      # utilisé si TEST_LIMIT_MODE == "duration"
TEST_CYCLES = 3             # utilisé si TEST_LIMIT_MODE == "cycles"

TEST_FILE_SIZE_MB = 512   # quantité de données écrite/lue par cycle
BLOCK_SIZE_MB = 4

# Critères PASS / FAIL
MIN_WRITE_MBPS = 100.0
MIN_READ_MBPS = 170.0
MAX_READ_MBPS = 500.0

# Reformatage automatique de la clé à la fin du test.
# Valeurs possibles : "exfat", "fat32", "ntfs"
REFORMAT_FS = "fat32"


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
TXT_PRESS_START = "Vérifiez le périphérique puis appuyez sur START"
TXT_TOO_MANY = "Plusieurs clés branchées : ne laisser que la clé à tester"
TXT_START = "START"


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


def speed_stats(values):
    """
    Statistiques sur les vitesses moyennes PAR CYCLE (MB/s).
    Écart-type d'échantillon (ddof=1) et CV = std / moyenne * 100.
    std et cv valent None s'il y a moins de 2 cycles.
    """
    n = len(values)
    stats = {"n": n, "mean": None, "std": None, "cv": None}
    if n == 0:
        return stats
    arr = np.asarray(values, dtype=float)
    stats["mean"] = float(arr.mean())
    if n >= 2:
        stats["std"] = float(arr.std(ddof=1))
        if stats["mean"] > 0:
            stats["cv"] = stats["std"] / stats["mean"] * 100.0
    return stats


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
    """
    Retourne la liste des devnodes de disques entiers (ex: /dev/sdb)
    branchés sur bus USB. On travaille toujours au niveau du disque
    entier : le test est brut (pas de système de fichiers) et la clé
    est reformatée à la fin, donc les partitions existantes n'ont pas
    besoin d'être prises en compte individuellement.
    """
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
# SMART (smartctl)
# ============================================================

NA_TEXT = "non disponible"

# Variables de synthèse comparées avant / après
SMART_VARIABLES = [
    "Health", 
    "Temperature", 
    "PowerOnHours", 
    "PowerCycles",
    "Wear", 
    "BadBlocks", 
    "Uncorrectable",
]


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def smart_variables(data, attrs):
    """
    Construit le dict {variable: (valeur | None, unité)} à partir de la
    sortie JSON de smartctl (ATA ou NVMe). None = non disponible.
    """
    nvme = data.get("nvme_smart_health_information_log") or {}

    def attr_field(field, *ids):
        for i in ids:
            for a in attrs:
                if a["id"] == i and a.get(field) is not None:
                    return a[field]
        return None

    v = {}

    passed = (data.get("smart_status") or {}).get("passed")
    v["Health"] = (None if passed is None
                   else ("OK" if passed else "FAILED"), "")

    temp = (data.get("temperature") or {}).get("current")
    if temp is None:
        temp = nvme.get("temperature")
    v["Temperature"] = (temp, " °C")

    poh = (data.get("power_on_time") or {}).get("hours")
    if poh is None:
        poh = nvme.get("power_on_hours")
    if poh is None:
        poh = attr_field("raw", 9)
    v["PowerOnHours"] = (poh, " h")

    pc = data.get("power_cycle_count")
    if pc is None:
        pc = nvme.get("power_cycles")
    if pc is None:
        pc = attr_field("raw", 12)
    v["PowerCycles"] = (pc, "")

    # Usure : valeur normalisée des attributs d'usure usuels
    # (177, 231, 233, 202, 173), sinon "percentage_used" en NVMe.
    wear = attr_field("value", 177, 231, 233, 202, 173)
    if wear is not None:
        v["Wear"] = (wear, " (norm.)")
    elif nvme.get("percentage_used") is not None:
        v["Wear"] = (nvme["percentage_used"], " % utilisé")
    else:
        v["Wear"] = (None, "")

    # Blocs défectueux : Reallocated_Sector_Ct (5) / Runtime_Bad_Block (183)
    v["BadBlocks"] = (attr_field("raw", 5, 183), "")

    # Erreurs non corrigibles : Reported_Uncorrect (187) /
    # Offline_Uncorrectable (198), sinon media_errors en NVMe.
    unc = attr_field("raw", 187, 198)
    if unc is None:
        unc = nvme.get("media_errors")
    v["Uncorrectable"] = (unc, "")

    return v


def read_smart(devnode):
    """
    Retourne (variables, attrs).
    variables : dict {nom: (valeur | None, unité)}, cf. smart_variables
    attrs     : liste de dicts {id, name, value, worst, raw}
    Si smartctl est absent ou ne répond pas : ({}, []).
    """

    if not shutil.which("smartctl"):
        return {}, []

    out, _, _ = run(
        ["smartctl", "-a", "-j", "-d", "sat", devnode], timeout=25
    )
    if not out:
        out2, _, _ = run(["smartctl", "-a", "-j", devnode], timeout=25)
        if out2:
            out = out2

    if not out:
        return {}, []

    try:
        data = json.loads(out)
    except Exception:
        return {}, []

    table = (data.get("ata_smart_attributes") or {}).get("table", [])
    attrs = []
    for a in table:
        attrs.append({
            "id": a.get("id"),
            "name": a.get("name", ""),
            "value": a.get("value"),
            "worst": a.get("worst"),
            "raw": (a.get("raw") or {}).get("value"),
        })

    return smart_variables(data, attrs), attrs


def smart_available(variables, attrs):
    return bool(attrs) or any(
        val is not None for val, _ in variables.values())


def smart_value_text(var):
    if not var or var[0] is None:
        return NA_TEXT
    return f"{var[0]}{var[1]}"


def smart_delta(before, after):
    if before is None or after is None:
        return "-"
    if _is_num(before) and _is_num(after):
        d = after - before
        if d == 0:
            return "="
        if isinstance(d, int):
            return f"{d:+d}"
        return f"{d:+.1f}"
    return "=" if before == after else "changé"


def build_smart_diff(vars_before, attrs_before, vars_after, attrs_after):
    """Compare deux relevés SMART : variables de synthèse + attributs ATA."""

    if not (smart_available(vars_before, attrs_before)
            or smart_available(vars_after, attrs_after)):
        return f"SMART : {NA_TEXT}"

    lines = [f"{'Variable':<16}{'Avant':<18}{'Après':<18}{'Delta':<8}"]
    for name in SMART_VARIABLES:
        b = vars_before.get(name, (None, ""))
        a = vars_after.get(name, (None, ""))
        lines.append(
            f"{name:<16}{smart_value_text(b):<18}{smart_value_text(a):<18}"
            f"{smart_delta(b[0], a[0]):<8}"
        )
    lines.append("")

    if not attrs_before and not attrs_after:
        lines.append(f"Attributs SMART ATA : {NA_TEXT}")
        return "\n".join(lines)

    lines.append("Attributs SMART ATA (valeur brute)")
    before = {a["id"]: a for a in attrs_before}
    after = {a["id"]: a for a in attrs_after}
    ids = sorted(
        set(before) | set(after),
        key=lambda x: (x is None, x if x is not None else 0)
    )

    lines.append(f"{'ID':<5}{'Name':<28}{'Avant':<14}{'Après':<14}{'Delta':<8}")
    for i in ids:
        ab = before.get(i)
        aa = after.get(i)
        name = (aa or ab).get("name", "")
        raw_b = ab["raw"] if ab else "-"
        raw_a = aa["raw"] if aa else "-"
        delta = smart_delta(ab["raw"] if ab else None,
                            aa["raw"] if aa else None)
        lines.append(
            f"{str(i):<5}{str(name):<28}{str(raw_b):<14}{str(raw_a):<14}"
            f"{delta:<8}"
        )

    return "\n".join(lines)

# ============================================================
# TEXTES D'INFO / RAPPORT
# ============================================================

def format_device_block(info):
    L = ["=== PÉRIPHÉRIQUE ==="]
    for k, label in (
        ("Date", "Date"),
        ("Devnode", "Devnode"), 
        ("Model", "Model"),
        ("Manufacturer", "Manufacturer"), 
        ("Serial", "Serial"),
        ("VID", "VID"), 
        ("PID", "PID"), 
        ("Size_GB", "Size_GB"),
        ("USBVersion", "USB Version"),
    ):
        L.append(f"{label:<14}: {info.get(k, 'N/A')}")
    L.append("")
    return "\n".join(L)

def format_legend():
    mb = BLOCK_SIZE / 1024 ** 2
    L = ["=== LÉGENDE : VARIABLES ET CALCULS ==="]
    L += [
        "",
        "-- Périphérique --",
        "Devnode      : disque entier testé (pas de partition), ex. /dev/sdb.",
        "Model/Manuf. : lsblk (MODEL/VENDOR) ; à défaut, descripteurs USB (udev).",
        "Serial       : numéro de série vu par lsblk.",
        "VID / PID    : idVendor / idProduct du noeud USB parent (sysfs).",
        "Size_GB      : taille en octets / 1e9 (GB décimaux, pas GiB).",
        "USB Version  : vitesse NÉGOCIÉE du lien (sysfs 'speed', tolérance 5 %),",
        "               pas la version annoncée de la clé : un port ou câble",
        "               USB 2.0 affichera 480 Mb/s même avec une clé USB 3.",
        "",
        "-- SMART (smartctl -a -j, pass-through SAT) --",
        "Relevé fait avant puis après le test. Delta = Après - Avant :",
        "  '=' identique, '+N'/'-N' variation, '-' valeur manquante d'un côté,",
        "  'changé' pour une valeur non numérique.",
        "'non disponible' : la clé / son pont USB ne transmet pas le SMART",
        "(cas très courant sur les clés USB).",
        "Health        : verdict global smart_status.passed (OK / FAILED).",
        "Temperature   : température courante en °C.",
        "PowerOnHours  : heures sous tension ; à défaut attribut ATA 9 (brut).",
        "PowerCycles   : nombre de cycles d'alimentation ; à défaut attribut 12.",
        "Wear          : usure. Valeur NORMALISÉE (non brute) du 1er attribut",
        "                trouvé parmi 177, 231, 233, 202, 173 (100 = neuf, baisse",
        "                avec l'usure) ; sinon NVMe percentage_used (% utilisé,",
        "                monte avec l'usure). Les deux sens sont donc opposés.",
        "BadBlocks     : valeur brute de l'attribut 5 (Reallocated_Sector_Ct),",
        "                sinon 183 (Runtime_Bad_Block).",
        "Uncorrectable : valeur brute de l'attribut 187, sinon 198 ; à défaut",
        "                media_errors (NVMe).",
        "Tableau ATA   : valeur BRUTE de chaque attribut, avant / après.",
        "",
        "-- Résultats du test --",
        f"Bloc de test  : {mb:g} MiB de données aléatoires (os.urandom), écrit",
        f"                {NUMBER_BLOCKS} fois par cycle (= {TEST_FILE_SIZE_MB} MB",
        "                par phase), accès brut O_DIRECT / O_SYNC.",
        "Cycle         : une phase d'écriture complète puis une phase de lecture.",
        "Erreurs int.  : nombre de blocs relus dont le SHA-256 diffère de celui",
        "                du bloc de référence (ou de taille incorrecte).",
        "Déconnexions  : nombre d'erreurs d'E/S fatales (exception à l'écriture",
        "                ou à la lecture, périphérique inaccessible). Le test",
        "                s'arrête à la première.",
        "Moyenne glob. : total des octets / SOMME des temps d'E/S, en MB/s",
        "                (1 MB = 1e6 octets). Seuls les appels writev/readv sont",
        "                chronométrés : SHA-256, CSV et GUI sont exclus.",
        "Vitesse cycle : octets du cycle / temps d'E/S du cycle (par phase).",
        "Std (cycle)   : écart-type d'échantillon (ddof=1) des vitesses par cycle.",
        "CV (cycle)    : Std / moyenne des vitesses par cycle x 100 (en %).",
        "                Plus il est bas, plus la clé est régulière.",
        "                N/A si moins de 2 cycles. La moyenne des cycles peut",
        "                différer légèrement de la moyenne globale ci-dessus.",
        "",
        "-- Critères PASS / FAIL --",
        "PASS si : 0 erreur d'intégrité, 0 déconnexion, test complet",
        f"  (écriture ET lecture effectuées, non interrompu),",
        f"  écriture > {MIN_WRITE_MBPS:g} MB/s,",
        f"  {MIN_READ_MBPS:g} MB/s < lecture < {MAX_READ_MBPS:g} MB/s.",
        "  Une lecture trop rapide est jugée suspecte (cache, mesure non fiable).",
        "Sinon FAIL, avec la ou les raisons indiquées dans RÉSULTAT FINAL.",
        "",
    ]
    return "\n".join(L)


def format_info_final(info, result, smart_text, reformat_msg):
    L = [format_device_block(info)]
    L.append("=== SMART (avant / après test) ===")
    L.append(smart_text)
    L.append("")
    L.append(format_speed_results(result))
    L.append("=== REFORMATAGE ===")
    L.append(reformat_msg)
    L.append("")
#    L.append(format_legend())
    L.append("")
    L.append(format_final_results(result))
    return "\n".join(L)


def format_speed_results(result):
    L = ["=== RÉSULTATS ==="]
    L.append(f"{'Erreurs intégrité':<18}: {result['errors']}")
    L.append(f"{'Déconnexions':<18}: {result['disconnects']}")
    if result.get("error"):
        L.append(f"{'Message':<18}: {result['error']}")
    L.append("")

    for label, avg, st in (
        ("Écriture", result["avg_write"], result["write_stats"]),
        ("Lecture", result["avg_read"], result["read_stats"]),
    ):
        L.append(label)
        L.append(f"  {'Moyenne globale':<16}: {avg:8.2f} MB/s")
        if st["n"] >= 2:
            L.append(f"  {'Std (par cycle)':<16}: {st['std']:8.2f} MB/s"
                     f"   (n = {st['n']} cycles)")
            cv = (f"{st['cv']:8.2f} %" if st["cv"] is not None else "N/A")
            L.append(f"  {'CV (par cycle)':<16}: {cv}")
        else:
            L.append(f"  {'Std / CV':<16}: N/A (moins de 2 cycles mesurés)")
    L.append("")
    return "\n".join(L)

def format_final_results(result):
    L = ["================= RÉSULTAT FINAL ================="]
    L.append("")
    L.append(f"{result['result']} {get_fail_reason(result)}")
    L.append("")
    L.append("==================================================")
    L.append("")
    return "\n".join(L)


def format_info_initial(info):
    return format_device_block(info)


def get_fail_reason(result):
    if result.get("result") == "PASS":
        return ""

    reasons = []
    if result.get("error"):
        reasons.append(result["error"])
    if result.get("errors", 0):
        reasons.append(f"{result['errors']} erreur(s) d'intégrité")
    if result.get("disconnects", 0):
        reasons.append(f"{result['disconnects']} déconnexion(s)")
    if result["avg_write"] <= MIN_WRITE_MBPS:
        reasons.append("Écriture trop lente")
    if result["avg_read"] <= MIN_READ_MBPS:
        reasons.append("Lecture trop lente")
    elif result["avg_read"] >= MAX_READ_MBPS:
        reasons.append("Lecture trop rapide")

    return " ; ".join(reasons) or "Test incomplet ou interrompu"


from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure
import textwrap


def write_pdf_report(path, text, figure=None, lines_per_page=85):
    """PDF : pages de texte (monospace) puisle graphe."""
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

def reformat_device(devnode, fs_type):
    """
    Reformate le périphérique avec le système de fichiers choisi.

    fs_type :
        "exfat"
        "fat32"
        "ntfs"
    """

    fs_type = fs_type.lower().strip()

    unmount_all_partitions(devnode)

    if fs_type == "exfat":
        tool = "mkfs.exfat"

        if not shutil.which(tool):
            raise RuntimeError(
                "mkfs.exfat introuvable "
                "(installer le paquet exfatprogs)."
            )

        cmd = [tool]

        cmd += [devnode]

    elif fs_type == "fat32":
        tool = "mkfs.vfat"

        if not shutil.which(tool):
            raise RuntimeError(
                "mkfs.vfat introuvable "
                "(installer le paquet dosfstools)."
            )

        cmd = [tool, "-F", "32"]

        cmd += [devnode]

    elif fs_type == "ntfs":
        tool = "mkfs.ntfs"

        if not shutil.which(tool):
            raise RuntimeError(
                "mkfs.ntfs introuvable "
                "(installer le paquet ntfs-3g)."
            )

        cmd = [tool, "-F"]

        cmd += [devnode]

    else:
        raise ValueError(
            f"Système de fichiers non supporté : {fs_type}. "
            "Choisir 'exfat', 'fat32' ou 'ntfs'."
        )

    out, err, rc = run(cmd, timeout=120)

    if rc != 0:
        raise RuntimeError(
            err or out or
            f"Reformatage {fs_type} échoué"
        )

    run(["udevadm", "settle"], timeout=10)

def verify_filesystem(devnode, fs_type):
    expected_blkid = {
        "exfat": "exfat",
        "fat32": "vfat",
        "ntfs": "ntfs",
    }

    fs_type = fs_type.lower().strip()

    if fs_type not in expected_blkid:
        raise ValueError(
            f"Système de fichiers non supporté : {fs_type}"
        )

    out, err, rc = run(
        ["blkid", "-o", "value", "-s", "TYPE", devnode],
        timeout=10
    )

    detected = out.strip().lower()

    expected = expected_blkid[fs_type]

    if rc != 0 or detected != expected:
        raise RuntimeError(
            f"{fs_type.upper()} non détecté après reformatage "
            f"(blkid : {detected or 'inconnu'}, attendu : {expected})"
        )

    return detected

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
    """
    mode: 'w' (écriture, write-through) ou 'r' (lecture).
    Le devnode existe déjà : pas de O_CREAT/O_TRUNC.
    Retombe sans O_DIRECT si non supporté (rare sur un périphérique
    bloc, mais certains adaptateurs/USB-SCSI l'exigent).
    """
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
        "devnode": devnode, "model": "N/A", "serial": "N/A",
        "csv": "", "graph": "", "pdf": "", "report": "",
        "avg_write": 0.0, "avg_read": 0.0,
        "write_stats": speed_stats([]), "read_stats": speed_stats([]),
        "errors": 0, "disconnects": 0, "error": "",
        "result": "FAIL", "reformat": ""
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
            result["disconnects"] = 1

        # Sorties anticipées / exception : on complète le rapport
        if not result.get("report"):
            result["report"] = (
                self._last_info + "\n" + format_speed_results(result)).strip()

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

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        tag = f"{stamp}_{safe_name(os.path.basename(devnode))}_{safe_name(info['Serial'])}"
        csv_file = RESULTS_DIR / f"USB_test_{tag}.csv"
        graph_file = RESULTS_DIR / f"USB_READ_WRITE_{tag}.png"
        pdf_file = RESULTS_DIR / f"USB_report_{tag}.pdf"
        result["csv"] = str(csv_file)
        result["graph"] = str(graph_file)
        result["pdf"] = str(pdf_file)

        # ---------------- SMART avant test ----------------
        self.status.emit("Lecture SMART (avant test)...")
        smart_vars_before, smart_attrs_before = read_smart(devnode)
        self._emit_info(format_info_initial(info))

        # ---------------- Accès brut ----------------
        self.status.emit("Démontage / accès brut au périphérique...")
        unmount_all_partitions(devnode)

        try:
            device_size = get_device_size(devnode)
        except Exception as e:
            result["error"] = f"Périphérique inaccessible : {e}"
            result["disconnects"] = 1
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
        disconnects = 0
        total_written = total_read = 0
        write_io_time = read_io_time = 0.0
        cycle_write_speeds = []     # MB/s moyen de chaque phase écriture
        cycle_read_speeds = []      # MB/s moyen de chaque phase lecture
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

        def reached_limit():
            if TEST_LIMIT_MODE == "cycles":
                return cycle >= TEST_CYCLES
            return elapsed() >= TEST_DURATION

        def emit_progress(phase=None, block_number=0):
            nonlocal last_pct
            if TEST_LIMIT_MODE == "cycles":
                frac = (block_number + 1) / NUMBER_BLOCKS
                half = 0.5 * frac
                cyc_frac = half if phase == "write" else 0.5 + half
                pct = min(100, int(
                    ((cycle - 1) + cyc_frac) / TEST_CYCLES * 100))
            else:
                pct = min(100, int(elapsed() / TEST_DURATION * 100))
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
                cw_bytes = 0
                cw_time = 0.0

                try:
                    fd, direct_ok = open_direct_device(devnode, "w")
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
                            cw_bytes += written
                            cw_time += duration

                            writer.writerow([cycle, "WRITE", block_number,
                                             rel, bitrate, 1, errors])
                            self.sample.emit("WRITE", rel, bitrate)
                            emit_progress("write", block_number)
                            if TEST_LIMIT_MODE == "duration" and elapsed() >= TEST_DURATION:
                                break

                        os.fsync(fd)
                    finally:
                        os.close(fd)

                except Exception as e:
                    disconnects += 1
                    io_failed = True
                    result["error"] = f"Erreur écriture : {e}"

                if cw_time > 0:
                    cycle_write_speeds.append(cw_bytes / cw_time / 1e6)

                if io_failed or self._abort:
                    break
                if TEST_LIMIT_MODE == "duration" and elapsed() >= TEST_DURATION:
                    break

                # ================= READ =================
                self.status.emit(f"Cycle {label_cycle} - LECTURE")
                cr_bytes = 0
                cr_time = 0.0

                try:
                    fd, direct_ok = open_direct_device(devnode, "r")
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
                            cr_bytes += n
                            cr_time += duration

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
                            emit_progress("read", block_number)
                            if TEST_LIMIT_MODE == "duration" and elapsed() >= TEST_DURATION:
                                break

                    finally:
                        os.close(fd)

                except Exception as e:
                    disconnects += 1
                    io_failed = True
                    result["error"] = f"Erreur lecture : {e}"

                if cr_time > 0:
                    cycle_read_speeds.append(cr_bytes / cr_time / 1e6)

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
            "avg_write": avg_write, 
            "avg_read": avg_read,
            "write_stats": speed_stats(cycle_write_speeds),
            "read_stats": speed_stats(cycle_read_speeds),
            "errors": errors, 
            "disconnects": disconnects,
            "result": "PASS" if (errors == 0 and disconnects == 0 and completed and 
                                 avg_write > MIN_WRITE_MBPS and avg_read > MIN_READ_MBPS and avg_read < MAX_READ_MBPS) 
                                 else "FAIL"
        })
        if self._abort and not result["error"]:
            result["error"] = "Test interrompu"
        if not direct_ok and not result["error"]:
            result["error"] = ("O_DIRECT non supporté "
                               "(résultats via cache, à relativiser)")

        # ---------------- SMART après test ----------------
        self.status.emit("Lecture SMART (après test)...")
        smart_vars_after, smart_attrs_after = read_smart(devnode)
        smart_text = build_smart_diff(
            smart_vars_before, smart_attrs_before,
            smart_vars_after, smart_attrs_after
        )

        # ---------------- Reformatage ----------------
        self.status.emit(f"Reformatage en {REFORMAT_FS.upper()}...")
        reformat_ok = False
        try:
            reformat_device(devnode, REFORMAT_FS)
            self.status.emit("Vérification du système de fichiers...")
            verify_filesystem(devnode, REFORMAT_FS)
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

        report = format_info_final(info, result, smart_text, reformat_msg)
        result["report"] = report
        self._emit_info(report)

        return result

# ============================================================
# GUI
# ============================================================

WAITING_INSERT, READY, TESTING, WAITING_REMOVAL = range(4)

COLORS = {
    "wait": "#1f4e79",
    "test": "#b26a00",
    "pass": "#1e7e34",
    "fail": "#b02a37",
}


class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Test flash drive")
        if DEV_MODE:
            self.resize(1300, 850)
        else:
            self.resize(900, 500)

        self.state = WAITING_INSERT
        self.usb_devnodes = []
        self.current_devnode = None
        self.worker = None
        self.graph_dirty = False
        self.n_pass = 0
        self.n_fail = 0
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
            self.banner.setMinimumHeight(130)
            root.addWidget(self.banner)
        else:
            self.banner.setMinimumHeight(200)
            root.addWidget(self.banner, 1)      # la bannière remplit la fenêtre

        self.status_label = QLabel("")
        self.status_label.setFont(QFont("Sans", 11 if DEV_MODE else 20))
        self.status_label.setAlignment(Qt.AlignCenter)
        root.addWidget(self.status_label)

        self.status_label.setWordWrap(True)

        self.start_button = QPushButton(TXT_START)
        self.start_button.setFont(
            QFont("Sans", 16 if DEV_MODE else 28, QFont.Bold))
        self.start_button.setMinimumHeight(60 if DEV_MODE else 110)
        self.start_button.setStyleSheet(
            "QPushButton{background-color:#1e7e34;color:white;"
            "border-radius:10px;}"
            "QPushButton:disabled{background-color:#999;color:#ddd;}")
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self.on_start_clicked)
        root.addWidget(self.start_button)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)

        # ----- Éléments mode développement -----
        # Toujours créés (les slots y écrivent), ajoutés à la fenêtre
        # seulement en mode développement.
        self.dev_status_label = QLabel("")
        self.dev_status_label.setFont(QFont("Sans", 10))
        self.dev_status_label.setAlignment(Qt.AlignCenter)

        self.info_text = QPlainTextEdit()
        self.info_text.setReadOnly(True)
        self.info_text.setFont(QFont("Monospace", 9))
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
        size = 44 if DEV_MODE else 96
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
        self.ax.set_ylabel("Bitrate [MB/s]")
        self.ax.set_title(f"USB Read / Write Bitrate ({TEST_FILE_SIZE_MB} MB file, {BLOCK_SIZE_MB} MB blocks)")
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
            self._set_user_status(f"{label}\n{TXT_PRESS_START}")
        else:
            others = ", ".join(self.usb_devnodes)
            self._set_user_status(f"{TXT_TOO_MANY}\n({others})")
        self._dev_status("clé détectée, en attente de START")
        self._render_info(format_device_block(info))
        self.start_button.setEnabled(single)

    def _go_waiting_insert(self):
        self.state = WAITING_INSERT
        self.current_devnode = None
        self._ready_snapshot = None
        self.start_button.setEnabled(False)
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

    # ---------------- Test ----------------

    def start_test(self, devnode):
        self.state = TESTING
        self.current_devnode = devnode
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
        if passed:
            self.n_pass += 1
        else:
            self.n_fail += 1
        self.progress.setValue(100)

        # Zone de texte : rapport complet + historique
        self._add_history(r)
        self._render_info(r.get("report") or self.current_info)

        # Le PDF est écrit AVANT la bannière : on laisse d'abord la boucle
        # d'événements repeindre le statut, puis _finalize fait le reste.
        self._set_user_status("Génération du rapport...")
        QTimer.singleShot(0, lambda: self._finalize(r, passed))

    def _finalize(self, r, passed):
        """Dernière étape du cycle : PDF, puis seulement PASS / FAIL."""
        self._write_pdf(r)      # ne lève jamais (try/except interne)

        self.state = WAITING_REMOVAL
        self._set_banner(TXT_PASS if passed else TXT_FAIL,
                         COLORS["pass"] if passed else COLORS["fail"])
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
