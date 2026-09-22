# region Description
#=================================================================
#
# USB Device Management
#
# Description :
# Regroupe toutes les fonctions liees a la gestion des
# peripheriques USB sous Linux.
#
# Ce module s'appuie sur pyudev et les interfaces sysfs du noyau
# Linux pour :
#
#     - Detecter l'insertion d'une cle USB
#     - Surveiller les evenements udev
#     - Identifier le peripherique USB associe
#     - Monter et demonter une partition
#     - Lire la vitesse USB negociee
#     - Forcer une reenumeration USB via unbind/bind
#
# Fonctionnement :
#
#     pyudev
#         
#     Detection partition USB
#         
#     Recherche du peripherique parent USB
#         
#     Exploitation des attributs sysfs
#
# Ce module constitue la couche d'abstraction entre les tests et
# le systeme Linux.
#
# Auteure :
#     Clemence Rey
#
#=================================================================
#endregion

import pyudev
import subprocess
import time
from pathlib import Path


class USBReenumerationError(Exception):
    pass


def create_monitor():
    context = pyudev.Context()

    monitor = pyudev.Monitor.from_netlink(context)
    monitor.filter_by(subsystem="block")
    monitor.start()

    return monitor


def _poll_for_usb_partition(monitor, timeout=None):
    start = time.time()

    while timeout is None or (time.time() - start) < timeout:

        device = monitor.poll(timeout=1)

        if device is None:
            continue

        if device.action != "add":
            continue

        if device.get("DEVTYPE") != "partition":
            continue

        if device.get("ID_BUS") != "usb":
            continue

        usb = device.find_parent("usb", "usb_device")

        if usb is None:
            continue

        return device, usb

    return None, None


def wait_for_initial_usb(monitor):
    print("Branchez la cle USB svp")
    return _poll_for_usb_partition(monitor, timeout=None)


def wait_for_usb_disk(monitor, timeout=10):
    return _poll_for_usb_partition(monitor, timeout=timeout)


def reenumerate_usb(usb_devpath):
    unbind_path = Path("/sys/bus/usb/drivers/usb/unbind")
    bind_path = Path("/sys/bus/usb/drivers/usb/bind")

    if not unbind_path.exists() or not bind_path.exists():
        raise USBReenumerationError(
            "Interface sysfs usb unbind/bind introuvable "
            f"(unbind={unbind_path}, bind={bind_path})"
        )

    try:
        unbind_path.write_text(usb_devpath)
    except OSError as exc:
        raise USBReenumerationError(
            f"Echec unbind de {usb_devpath} : {exc}"
        ) from exc

    time.sleep(1)

    start = time.perf_counter()

    try:
        bind_path.write_text(usb_devpath)
    except OSError as exc:
        raise USBReenumerationError(
            f"Echec bind de {usb_devpath} : {exc}"
        ) from exc

    return start


def get_negotiated_speed(usb):
    try:
        speed = usb.attributes.asstring("speed")

        mapping = {
            "1.5": "Low-Speed",
            "12": "Full-Speed",
            "480": "High-Speed",
            "5000": "SuperSpeed",
            "10000": "SuperSpeedPlus",
            "20000": "USB20G"
        }

        return mapping.get(speed, f"{speed} Mb/s")

    except Exception:
        return None


def get_mount_point(device):
    devnode = device.device_node

    with open("/proc/mounts") as f:
        for line in f:
            fields = line.split()
            if fields[0] == devnode:
                return fields[1].replace("\\040", " ")

    return None


def mount_partition(devnode, mountpoint):
    mountpoint = Path(mountpoint)
    mountpoint.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        ["mount", devnode, str(mountpoint)],
        check=True,
        timeout=15,
    )

    return mountpoint


def unmount_partition(mountpoint):
    subprocess.run(
        ["umount", str(mountpoint)],
        check=False,
        timeout=15,
    )