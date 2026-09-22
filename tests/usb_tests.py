# region Description
#=================================================================
#
# USB Flash Drive Test Suite
#
# Description :
# Bibliotheque regroupant l'ensemble des tests realises sur une
# cle USB.
#
# Les essais implementes sont :
#
#     ...
#
# Auteure :
#     Clemence Rey
#
#=================================================================
# endregion

from tests.udev import reenumerate_usb, wait_for_usb_disk, get_negotiated_speed, USBReenumerationError
import time
import hashlib
import os
import shutil
import subprocess
from pathlib import Path
import json


def get_usb_info(device, usb):

    info = {
        "VID": usb.get("ID_VENDOR_ID"),
        "PID": usb.get("ID_MODEL_ID"),
        "Manufacturer": usb.get("ID_VENDOR"),
        "Product": usb.get("ID_MODEL"),
        "Serial": usb.get("ID_SERIAL_SHORT"),
        "USBVersion": None,
        "NegotiatedSpeed": None,
        "CapacityGiB": None,
        "Filesystem": device.get("ID_FS_TYPE"),
    }

    try:
        info["USBVersion"] = usb.attributes.asstring("version")
    except Exception:
        pass

    try:
        info["NegotiatedSpeed"] = usb.attributes.asstring("speed")
    except Exception:
        pass

    try:
        sectors = int(device.attributes.asstring("size"))
        size_bytes = sectors * 512
        info["CapacityGiB"] = round(size_bytes / (1024**3), 2)
    except Exception:
        pass

    return info


def run_enumeration_test(
    monitor,
    usb_devpath,
    expected_serial,
    cycles=5,
):
    test_start = time.time()

    results = {}
    enumeration_times = []

    for i in range(cycles):

        print(f"\n=== ENUMERATION TEST : Cycle {i + 1}/{cycles} ===")

        # (3) Un cycle en echec (unbind/bind impossible) ne doit pas
        # interrompre les cycles suivants.
        try:
            start = reenumerate_usb(usb_devpath)
        except USBReenumerationError as exc:
            print(f"Erreur de reenumeration : {exc}")
            results["Reenumeration Error"] = results.get("Reenumeration Error", 0) + 1
            continue

        device, usb = wait_for_usb_disk(monitor)

        enum_time_ms = (time.perf_counter() - start) * 1000
        enumeration_times.append(enum_time_ms)

        if usb is None:
            speed = "Enumeration Failed"
        elif usb.get("ID_SERIAL_SHORT") != expected_serial:
            speed = "Wrong Device"
        else:
            speed = get_negotiated_speed(usb)

        results[speed] = results.get(speed, 0) + 1

    time_elapsed_ms = (time.time() - test_start) * 1000

    return {
        "speeds": results,
        "times_ms": enumeration_times,
        "time_elapsed_ms": time_elapsed_ms
    }


def compute_sha256(filepath):
    sha256 = hashlib.sha256()

    with open(filepath, "rb") as f:
        while chunk := f.read(1024 * 1024):
            sha256.update(chunk)

    return sha256.hexdigest()


def create_random_file(filepath, size_mb):
    size_bytes = size_mb * 1024 * 1024

    with open(filepath, "wb") as f:
        remaining = size_bytes

        while remaining > 0:
            chunk_size = min(1024 * 1024, remaining)
            f.write(os.urandom(chunk_size))
            remaining -= chunk_size


def drop_cache():
    try:
        subprocess.run(["sync"], check=True, timeout=10)

        with open("/proc/sys/vm/drop_caches", "w") as f:
            f.write("3\n")

    except (PermissionError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"Attention : impossible de vider le cache ({exc}). "
              "Le test de lecture peut être fausse par le cache page.")


def run_speed_test(
    usb_path,
    file_size_mb=400,
    cycles=3
):
    test_start = time.time()

    write_speeds = []
    read_speeds = []
    integrity_ok = 0

    for cycle in range(1, cycles + 1):

        print(f"\n=== CUSTOM SPEED TEST ({file_size_mb} MB): Cycle {cycle}/{cycles} ===")

        usb_file = usb_path / f"test_{cycle}.bin"
        local_file = Path(f"/tmp/test_{cycle}.bin")

        print("Generating random file...")
        create_random_file(local_file, file_size_mb)

        original_sha256 = compute_sha256(local_file)

        # WRITE TEST
        print("Writing to USB...")
        t0 = time.perf_counter()

        with open(local_file, "rb") as src, open(usb_file, "wb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())

        write_time = time.perf_counter() - t0

        drop_cache()

        # READ TEST
        print("Reading from USB...")
        sha256_read = hashlib.sha256()
        t0 = time.perf_counter()

        with open(usb_file, "rb") as f:
            while chunk := f.read(1024 * 1024):
                sha256_read.update(chunk)

        read_time = time.perf_counter() - t0
        read_sha256 = sha256_read.hexdigest()

        sha256_match = (original_sha256 == read_sha256)

        actual_size_mb = local_file.stat().st_size / (1024 * 1024)

        write_speed = actual_size_mb / write_time
        read_speed = actual_size_mb / read_time

        print(f"Write time  : {write_time:.3f}s")
        print(f"Read time   : {read_time:.3f}s")
        print(f"File size   : {actual_size_mb:.3f}MB")
        print(f"Write speed : {write_speed:.3f}MB/s")
        print(f"Read speed  : {read_speed:.3f}MB/s")

        write_speeds.append(write_speed)
        read_speeds.append(read_speed)

        if sha256_match:
            integrity_ok += 1

        local_file.unlink(missing_ok=True)
        usb_file.unlink(missing_ok=True)

    time_elapsed_ms = (time.time() - test_start) * 1000

    return {
        "cycles": cycles,
        "file_size_mb": file_size_mb,
        "write_speeds": write_speeds,
        "read_speeds": read_speeds,
        "integrity_ok": integrity_ok,
        "time_elapsed_ms": time_elapsed_ms
    }


def run_fio_seq_test(
    usb_path,
    file_size_mb=400,
    cycles=3,
    fio_timeout_s=120,
):
    test_start = time.time()

    write_speeds = []
    read_speeds = []

    for cycle in range(cycles):

        print(f"\n=== FIO SEQUENTIAL TEST ({file_size_mb} MB): Cycle {cycle + 1}/{cycles} ===")

        # (7) Timeout ajoute : une cle qui se deconnecte pendant le
        # test ne fige plus fio (et donc tout le banc) indefiniment.
        try:
            result = subprocess.run(
                [
                    "fio",
                    "--name=usbtest",
                    f"--directory={usb_path}",
                    f"--size={file_size_mb}M",
                    "--rw=write",
                    "--bs=1M",
                    "--iodepth=1",
                    "--direct=1",
                    "--output-format=json",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=fio_timeout_s,
            )
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            print(f"Echec fio (write) : {exc}")
            write_speeds.append(0.0)
            read_speeds.append(0.0)
            continue

        data = json.loads(result.stdout)
        write_bw = data["jobs"][0]["write"]["bw"] / 1024

        try:
            result = subprocess.run(
                [
                    "fio",
                    "--name=usbtest",
                    f"--directory={str(usb_path)}",
                    f"--size={file_size_mb}M",
                    "--rw=read",
                    "--bs=1M",
                    "--iodepth=1",
                    "--direct=1",
                    "--output-format=json",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=fio_timeout_s,
            )
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            print(f"Echec fio (read) : {exc}")
            write_speeds.append(write_bw)
            read_speeds.append(0.0)
            continue

        data = json.loads(result.stdout)
        read_bw = data["jobs"][0]["read"]["bw"] / 1024

        write_speeds.append(write_bw)
        read_speeds.append(read_bw)

        print(f"Write : {write_bw:.1f} MB/s")
        print(f"Read  : {read_bw:.1f} MB/s")

    time_elapsed_ms = (time.time() - test_start) * 1000

    return {
        "cycles": cycles,
        "file_size_mb": file_size_mb,
        "write_avg": sum(write_speeds) / len(write_speeds),
        "read_avg": sum(read_speeds) / len(read_speeds),
        "write_min": min(write_speeds),
        "write_max": max(write_speeds),
        "read_min": min(read_speeds),
        "read_max": max(read_speeds),
        "write_speeds": write_speeds,
        "read_speeds": read_speeds,
        "time_elapsed_ms": time_elapsed_ms
    }

def run_fio_rand_test(
    usb_path,
    file_size_mb=400,
    cycles=3,
    block_size_kb=4,
    iodepth=32,
    fio_timeout_s=120,
):
    test_start = time.time()

    write_iops = []
    read_iops = []
    write_bw = []
    read_bw = []

    for cycle in range(cycles):

        print(f"\n=== FIO RANDOM {block_size_kb}K TEST: Cycle {cycle + 1}/{cycles} ===")

        # RANDOM WRITE
        try:
            result = subprocess.run(
                [
                    "fio",
                    "--name=usbtest_randwrite",
                    f"--directory={usb_path}",
                    f"--size={file_size_mb}M",
                    "--rw=randwrite",
                    f"--bs={block_size_kb}K",
                    f"--iodepth={iodepth}",
                    "--direct=1",
                    "--output-format=json",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=fio_timeout_s,
            )
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            print(f"Echec fio (randwrite) : {exc}")
            write_iops.append(0.0)
            read_iops.append(0.0)
            write_bw.append(0.0)
            read_bw.append(0.0)
            continue

        data = json.loads(result.stdout)
        job_write = data["jobs"][0]["write"]
        write_iops.append(job_write["iops"])
        write_bw.append(job_write["bw"] / 1024) # KB/s -> MB/s

        # RANDOM READ
        try:
            result = subprocess.run(
                [
                    "fio",
                    "--name=usbtest_randread",
                    f"--directory={usb_path}",
                    f"--size={file_size_mb}M",
                    "--rw=randread",
                    f"--bs={block_size_kb}K",
                    f"--iodepth={iodepth}",
                    "--direct=1",
                    "--output-format=json",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=fio_timeout_s,
            )
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            print(f"Echec fio (randread) : {exc}")
            read_iops.append(0.0)
            read_bw.append(0.0)
            continue

        data = json.loads(result.stdout)
        job_read = data["jobs"][0]["read"]
        read_iops.append(job_read["iops"])
        read_bw.append(job_read["bw"] / 1024) # KB/s -> MB/s

        print(f"Write : {job_write['iops']:.0f} IOPS ({write_bw[-1]:.1f} MB/s)")
        print(f"Read : {job_read['iops']:.0f} IOPS ({read_bw[-1]:.1f} MB/s)")

    time_elapsed_ms = (time.time() - test_start) * 1000

    return {
        "cycles": cycles,
        "file_size_mb": file_size_mb,
        "block_size_kb": block_size_kb,
        "iodepth": iodepth,
        "write_iops": write_iops,
        "read_iops": read_iops,
        "write_bw": write_bw,
        "read_bw": read_bw,
        "write_iops_avg": sum(write_iops) / len(write_iops),
        "read_iops_avg": sum(read_iops) / len(read_iops),
        "write_iops_min": min(write_iops),
        "write_iops_max": max(write_iops),
        "read_iops_min": min(read_iops),
        "read_iops_max": max(read_iops),
        "time_elapsed_ms": time_elapsed_ms
    }


def print_results(
    info,
    file_size_mb,
    speed_results,
    fio_seq_results,
    fio_rand_results,
    enumeration_results,
    enumeration_cycles,
):
    print("\n" + "=" * 60)
    print("USB FLASH DRIVE TEST REPORT")
    print("=" * 60)

    print("\nUSB INFORMATION")
    print("-" * 60)

    for key, value in info.items():
        print(f"{key:<18}: {value}")

    print(f"\nSPEED TEST (CUSTOM) ({file_size_mb} MB)")
    print("-" * 60)

    write_speeds = speed_results["write_speeds"]
    read_speeds = speed_results["read_speeds"]

    print(
        f"Write MB/s      : "
        f"avg={sum(write_speeds)/len(write_speeds):.1f}  "
        f"min={min(write_speeds):.1f}  "
        f"max={max(write_speeds):.1f}"
    )

    print(
        f"Read MB/s       : "
        f"avg={sum(read_speeds)/len(read_speeds):.1f}  "
        f"min={min(read_speeds):.1f}  "
        f"max={max(read_speeds):.1f}"
    )

    print(
        f"Integrity       : "
        f"{speed_results['integrity_ok']}/"
        f"{speed_results['cycles']}"
    )

    print(f"Time elapsed    : {speed_results['time_elapsed_ms']:.0f}")

    print(f"\nFIO SEQUENTIAL TEST ({file_size_mb} MB)")
    print("-" * 60)

    print(
        f"Write MB/s      : "
        f"avg={fio_seq_results['write_avg']:.1f}  "
        f"min={fio_seq_results['write_min']:.1f}  "
        f"max={fio_seq_results['write_max']:.1f}"
    )

    print(
        f"Read MB/s       : "
        f"avg={fio_seq_results['read_avg']:.1f}  "
        f"min={fio_seq_results['read_min']:.1f}  "
        f"max={fio_seq_results['read_max']:.1f}"
    )

    print(f"Time elapsed    : {fio_seq_results['time_elapsed_ms']:.0f}")

    print(f"\nFIO RANDOM {fio_rand_results['block_size_kb']}K TEST "
          f"(iodepth = {fio_rand_results['iodepth']}, "
          f"{fio_rand_results['file_size_mb']} MB")
    print("-" * 60)

    print(
        f"Write IOPS      : "
        f"avg={fio_rand_results['write_iops_avg']:.0f}  "
        f"min={fio_rand_results['write_iops_min']:.0f}  "
        f"max={fio_rand_results['write_iops_max']:.0f}"
    )

    print(
        f"Read IOPS       : "
        f"avg={fio_rand_results['read_iops_avg']:.0f}  "
        f"min={fio_rand_results['read_iops_min']:.0f}  "
        f"max={fio_rand_results['read_iops_max']:.0f}"
    )

    print(f"Time elapsed    : {fio_rand_results['time_elapsed_ms']:.0f}")


    print("\nENUMERATION TEST")
    print("-" * 60)

    speed_counts = enumeration_results["speeds"]
    enum_times = enumeration_results["times_ms"]

    for speed, count in sorted(speed_counts.items()):
        print(f"{speed:<18}: {count}")

    if enum_times:
        avg_time = sum(enum_times) / len(enum_times)
        min_time = min(enum_times)
        max_time = max(enum_times)
        print(
            f"Enumeration Time : "
            f"avg={avg_time:.0f} ms  "
            f"min={min_time:.0f} ms  "
            f"max={max_time:.0f} ms"
        )

    success = sum(
        count
        for speed, count in speed_counts.items()
        if speed not in ("Enumeration Failed", "Wrong Device", "Reenumeration Error")
    )
    success_rate = 100 * success / enumeration_cycles

    print(f"\nSuccess Rate     : {success_rate:.1f}%")
    print(f"Time elapsed    : {enumeration_results['time_elapsed_ms']:.0f}")

    print("\n" + "=" * 60)