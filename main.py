# region Description
#=================================================================
#
# USB Flash Drive Test Runner
#
# Description :
# Script principal du projet FD Test Linux.
#
# Il orchestre l'ensemble de la campagne de test d'une cle USB :
#
#     - Detection de la cle via udev
#     - Montage automatique si necessaire
#     - Lecture des informations USB
#     - Test de vitesse personnalise
#     - Test de performance sequentielle via fio
#     - Test de performance aleatoire via fio
#     - Test de reenumeration USB
#     - Generation d'un rapport synthetique
#
# Fonctionnement :
#
#     1. Attente de l'insertion d'une cle USB.
#     2. Identification du peripherique detecte.
#     3. Montage de la partition si besoin.
#     4. Execution successive des differents tests.
#     5. Affichage des resultats.
#     6. Demontage propre de la cle.
#
# Ce fichier contient uniquement la logique d'orchestration.
# Les operations USB sont deleguees a udev.py et les tests a
# usb_tests.py.
#
# Auteure :
#     Clemence Rey
#
#=================================================================
#endregion

# region Imports
from tests.usb_tests import (
    run_enumeration_test, 
    print_results, 
    get_usb_info, 
    run_speed_test, 
    run_fio_seq_test,
    run_fio_rand_test,
)
from tests.udev import (
    create_monitor, 
    wait_for_initial_usb, 
    get_mount_point, 
    mount_partition, 
    unmount_partition,
)
from pathlib import Path
#endregion

def main():

    ########## Configuration des tests ##########

    # Custom and FIO sequential speed test params
    speed_cycles = 9
    seq_test_file_size_mb = 16

    # FIO random test params
    random_test_file_size_mb = 100
    random_test_block_size_kb = 4
    random_test_iodepth = 32

    # Enumeration params
    enumeration_cycles = 3

    ENABLED_TESTS = {
        "speed":        True,
        "fio_seq":      True,
        "fio_rand":     False,
        "enumeration":  False,
    }

    ########### Detection de la cle USB et montage de la partition ##########


    monitor = create_monitor()

    device, usb = wait_for_initial_usb(monitor)

    if usb is None:
        print("No USB device detected")
        return

    print("Partition :", device.device_node)


    usb_mount = get_mount_point(device)
    mounted_by_us = False

    if usb_mount is None:
        try:
            usb_mount = mount_partition(
                device.device_node,
                f"/mnt/usb_test_{device.sys_name}",
            )
            mounted_by_us = True
        except Exception as exc:
            print(f"Impossible de monter la partition : {exc}")
            return

    print("Mount point :", usb_mount)

    ############ Execution des tests ##########

    try:
        info = get_usb_info(device, usb)

        results = {}

        if ENABLED_TESTS["speed"]:
            results["speed"] = run_speed_test(
                usb_path=Path(usb_mount),
                file_size_mb=seq_test_file_size_mb,
                cycles=speed_cycles,
            )

        if ENABLED_TESTS["fio_seq"]:
            results["fio_seq"] = run_fio_seq_test(
                usb_path=Path(usb_mount),
                file_size_mb=seq_test_file_size_mb,
                cycles=speed_cycles,
            )

        if ENABLED_TESTS["fio_rand"]:
            results["fio_rand"] = run_fio_rand_test(
                usb_path=Path(usb_mount),
                file_size_mb=random_test_file_size_mb,
                cycles=speed_cycles,
                block_size_kb=random_test_block_size_kb,
                iodepth=random_test_iodepth,
            )

        if ENABLED_TESTS["enumeration"]:
            results["enumeration"] = run_enumeration_test(
                monitor,
                usb.sys_name,
                usb.get("ID_SERIAL_SHORT"),
                enumeration_cycles,
            )
        

        print_results(
            info,
            results,
            enumeration_cycles,
            seq_test_file_size_mb
        )
        
    finally:
        if mounted_by_us:
            unmount_partition(usb_mount)




if __name__ == "__main__":
    main()