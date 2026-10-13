"""Surveillance USB et exécution des tests dans des threads Qt indépendants."""

import csv
import hashlib
import os
import time

import pyudev
from PyQt5.QtCore import QThread, pyqtSignal

from config import (
    RESULTS_DIR, FILE_SIZE, BLOCK_SIZE, NUMBER_BLOCKS, TEST_LIMIT_MODE,
    TEST_CYCLES, TEST_DURATION, MIN_WRITE_MBPS, MIN_READ_MBPS, MAX_READ_MBPS,
    REFORMAT_FS, REFORMAT_LABEL,
)
from model import (
    speed_stats, set_result_error, assert_same_device, get_device_info,
    create_result_paths, format_device_info, unmount_all_partitions,
    get_device_size, make_aligned_buffer, open_test_device, calculate_mbps,
    do_format_device, build_report, scan_usb_candidates,
)

# ============================================================
# 1. SURVEILLANCE DES PÉRIPHÉRIQUES USB
# ============================================================


class UsbWatcher(QThread):
    """Surveille les connexions et déconnexions des périphériques USB."""
    drives_changed = pyqtSignal(list)
    usb_removed = pyqtSignal(str)

    def __init__(self):
        """Initialise les paramètres et l’état du thread."""
        super().__init__()
        self._stop_requested = False

    def stop(self):
        """Demande l’arrêt du thread de surveillance USB."""
        self._stop_requested = True

    def run(self):
        """Surveille les événements USB et signale les changements à la GUI."""
        context = pyudev.Context()
        monitor = pyudev.Monitor.from_netlink(context)
        monitor.filter_by(subsystem="block")
        monitor.start()
        self.drives_changed.emit(scan_usb_candidates())
        last_scan = time.monotonic()
        scan_pending = False
        while not self._stop_requested:
            device = monitor.poll(timeout=0.1)
            if device is not None:
                if device.action == "remove" and device.device_type == "disk":
                    self.usb_removed.emit(device.device_node or "")
                if device.action in ("add", "remove", "change"):
                    scan_pending = True
            if scan_pending and time.monotonic() - last_scan >= 0.8:
                self.drives_changed.emit(scan_usb_candidates())
                last_scan = time.monotonic()
                scan_pending = False


# ============================================================
# 2. EXÉCUTION DU TEST USB
# ============================================================


def new_result(devnode):
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
        "write_stats": speed_stats([]),
        "read_stats": speed_stats([]),
        "errors": 0,
        "error": "",
        "error_code": "",
        "error_detail": "",
        "result": "FAIL",
        "reformat": "",
        "completed": False,
    }


class TestWorker(QThread):
    """Exécute les tests de lecture, écriture, intégrité et reformatage."""
    status = pyqtSignal(str)
    info_ready = pyqtSignal(str)
    sample = pyqtSignal(str, float, float)   # op, temps (s), MB/s
    progress = pyqtSignal(int)
    test_done = pyqtSignal(dict)

    def __init__(self, devnode, expected_diskseq):
        """Initialise les paramètres et l’état du thread."""
        super().__init__()
        self.devnode = devnode
        self.expected_diskseq = expected_diskseq
        self._abort = False
        self._last_info = ""
        self._abort_reason = "Test interrompu"
        self.device_info = None
        self.current_result = None

    def abort(self, reason="Test interrompu"):
        """Demande l’interruption du test avec une raison explicite."""
        self._abort_reason = reason
        self._abort = True

    def _emit_info(self, text):
        """Transmet le texte du rapport à la GUI."""
        self._last_info = text
        self.info_ready.emit(text)

    def run(self):
        """
        Point d'entrée du thread de test.
        Exécute le test USB, intercepte les erreurs non gérées et
        transmet systématiquement un résultat final à la GUI.
        """

        # EXÉCUTION DU TEST
        try:
            result = self._run_test()
        except Exception as e:

        # GESTION DES ERREURS NON INTERCEPTÉES
            # Récupérer les résultats déjà enregistrés, si disponibles.
            result = self.current_result
            if result is None:
                result = new_result(self.devnode)
            # Enregistrer l'erreur et forcer le verdict FAIL.
            set_result_error(result, e, "Test")
            # Ne pas reformater si le test a échoué de manière inattendue.
            result["reformat"] = "NON EFFECTUÉ (test interrompu)"
            # En cas d'interruption, privilégier la raison enregistrée.
            if self._abort:
                result["error"] = self._abort_reason

            # 2.1 RÉCUPÉRATION DES INFORMATIONS DU PÉRIPHÉRIQUE
            # Utiliser les informations déjà mémorisées pendant le test.
            info = self.device_info
            if info is None:
                info = get_device_info(self.devnode)

            # 2.2 GÉNÉRATION DU RAPPORT D'ERREUR
            result["report"] = build_report(info, result)
            self._emit_info(result["report"])

        # TRANSMISSION DU RÉSULTAT À LA GUI
        # Émettre le résultat dans tous les cas : PASS, FAIL ou erreur.
        self.test_done.emit(result)

    def _run_test(self):
        """
        Exécute le test USB complet.
        Déroulement :
            1. Identifier et préparer le périphérique.
            2. Préparer les données et les fichiers de mesure.
            3. Exécuter les cycles d'écriture et de lecture.
            4. Calculer les performances et le verdict.
            5. Reformater la clé si le test n'a pas été interrompu.
            6. Générer le rapport final.
        Les E/S sont réalisées directement sur le périphérique avec O_DIRECT.
        """
        devnode = self.devnode

        # IDENTIFICATION ET PRÉPARATION DU PÉRIPHÉRIQUE
        # Vérifier que la clé sélectionnée n'a pas été remplacée.
        assert_same_device(devnode, self.expected_diskseq)
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        # Récupérer les informations du périphérique.
        self.status.emit("Lecture des informations du périphérique...")
        info = get_device_info(devnode)
        self.device_info = info.copy()
        # Initialiser les résultats et les chemins des rapports.
        result = new_result(devnode)
        self.current_result = result
        result["model"] = info["Model"]
        result["serial"] = info["Serial"]
        result.update(create_result_paths(devnode, info["Serial"]))
        self._emit_info(format_device_info(info))
        # Démonter les partitions pour accéder directement au disque.
        self.status.emit("Démontage / accès brut au périphérique...")
        unmount_all_partitions(devnode, self.expected_diskseq)
        # Vérifier que la capacité permet d'exécuter le test.
        try:
            device_size = get_device_size(devnode)
        except Exception as e:
            raise RuntimeError(
                f"Périphérique inaccessible : {e}"
            ) from e
        if device_size < FILE_SIZE * 1.1:
            raise RuntimeError("Périphérique trop petit pour ce test")

        # PRÉPARATION DES DONNÉES ET DES MESURES
        # Générer un bloc de référence utilisé pour toutes les écritures.
        # Son empreinte SHA-256 servira à vérifier les données relues.
        reference_bytes = os.urandom(BLOCK_SIZE)
        reference_hash = hashlib.sha256(reference_bytes).digest()
        # Buffers alignés en mémoire, nécessaires aux E/S avec O_DIRECT.
        write_buffer = make_aligned_buffer(BLOCK_SIZE, fill=reference_bytes)
        read_buffer = make_aligned_buffer(BLOCK_SIZE)
        # Compteurs et statistiques du test.
        errors = 0
        total_written = total_read = 0
        write_io_time = read_io_time = 0.0
        cycle_write_speeds = []
        cycle_read_speeds = []
        cycle = 0
        self._cycle = 0
        self._last_pct = -1
        io_failed = False
        # Fichier CSV contenant les mesures de chaque bloc.
        csv_handle = open(
            result["csv"], "w", newline="", encoding="utf-8"
        )
        writer = csv.writer(csv_handle)
        writer.writerow([
            "Cycle", "Operation", "Block",
            "Relative_Time_s", "MB/s", "Integrity"
        ])
        # Point de départ du chronométrage global.
        self._global_start = time.perf_counter()

        # EXÉCUTION DES CYCLES D'ÉCRITURE ET DE LECTURE
        fd = None
        try:
            # Ouverture exclusive du disque avec O_DIRECT.
            # L'identité du périphérique est également vérifiée.
            fd = open_test_device(devnode, self.expected_diskseq)
            while not self._reached_limit() and not self._abort and not io_failed:
                cycle += 1
                self._cycle = cycle
                # Séparer visuellement les cycles sur le graphique.
                if cycle > 1:
                    self.sample.emit(
                        "SEP", float("nan"), float("nan")
                    )
                if TEST_LIMIT_MODE == "cycles":
                    label_cycle = f"{cycle}/{TEST_CYCLES}"
                else:
                    label_cycle = str(cycle)

                # 3.1 ÉCRITURE
                self.status.emit(f"Cycle {label_cycle} - ÉCRITURE")
                # Recommencer l'écriture au début du disque.
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    written, write_time = self._write_cycle(
                        fd,
                        write_buffer,
                        cycle,
                        writer,
                        self._elapsed,
                        self._emit_progress,
                    )
                    # Cumuler les octets et le temps d'E/S.
                    total_written += written
                    write_io_time += write_time
                    # Enregistrer le débit moyen de cette phase.
                    if write_time > 0:
                        cycle_write_speeds.append(
                            calculate_mbps(written, write_time)
                        )
                except Exception as e:
                    io_failed = True
                    set_result_error(result, e, "Écriture")
                # Ne pas poursuivre après une erreur ou une interruption.
                if io_failed or self._abort:
                    break
                # En mode durée, ne pas commencer la lecture si
                # la durée est déjà atteinte après l'écriture.
                if TEST_LIMIT_MODE == "duration" and self._reached_limit():
                    break

                # 3.2 LECTURE ET VÉRIFICATION D'INTÉGRITÉ
                self.status.emit(f"Cycle {label_cycle} - LECTURE")
                # Relire les données depuis le début du disque.
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    read_bytes, read_time, read_errors = self._read_cycle(
                        fd,
                        read_buffer,
                        reference_hash,
                        cycle,
                        writer,
                        self._elapsed,
                        self._emit_progress,
                        errors,
                    )
                    # Cumuler les octets, le temps et les erreurs.
                    total_read += read_bytes
                    read_io_time += read_time
                    errors += read_errors
                    # Enregistrer le débit moyen de cette phase.
                    if read_time > 0:
                        cycle_read_speeds.append(
                            calculate_mbps(read_bytes, read_time)
                        )
                except Exception as e:
                    io_failed = True
                    set_result_error(result, e, "Lecture")
        finally:

            # 3.3 LIBÉRATION DES RESSOURCES
            # Fermer le périphérique avant de libérer les buffers.
            if fd is not None:
                os.close(fd)
            csv_handle.close()
            write_buffer.close()
            read_buffer.close()

        # CALCUL DES STATISTIQUES ET DU VERDICT
        # Calcul des débits moyens, statistiques et critères PASS/FAIL.
        self._evaluate_result(
            result,
            total_written,
            total_read,
            write_io_time,
            read_io_time,
            cycle_write_speeds,
            cycle_read_speeds,
            errors,
            io_failed,
        )

        # GESTION DES INTERRUPTIONS ET DES ERREURS D'E/S
        if self._abort or io_failed:
            result["result"] = "FAIL"
            result["completed"] = False
            if self._abort:
                result["error"] = self._abort_reason
            # Ne jamais reformater après une interruption ou erreur d'E/S.
            result["reformat"] = "NON EFFECTUÉ (test interrompu)"
            return self._finalize_result(info, result)

        # REFORMATAGE DU PÉRIPHÉRIQUE
        # Le reformatage est exécuté même si les débits sont insuffisants.
        # Un échec du reformatage entraîne un verdict FAIL.
        self._reformat_device(result)

        # GÉNÉRATION DU RAPPORT FINAL
        return self._finalize_result(info, result)

    def _elapsed(self):
        """Retourne le temps écoulé depuis le début des mesures (secondes)."""
        return time.perf_counter() - self._global_start


    def _reached_limit(self):
        """Indique si la limite de cycles ou de durée du test est atteinte."""
        if TEST_LIMIT_MODE == "cycles":
            return self._cycle >= TEST_CYCLES
        return self._elapsed() >= TEST_DURATION


    def _emit_progress(self, phase=None, block_number=0):
        """Transmet à la GUI une progression limitée à 95 % pendant les E/S."""
        if TEST_LIMIT_MODE == "cycles":
            fraction = (block_number + 1) / NUMBER_BLOCKS
            cycle_progress = 0.5 * fraction if phase == "write" else 0.5 + 0.5 * fraction
            progress = ((self._cycle - 1) + cycle_progress) / TEST_CYCLES
        else:
            progress = self._elapsed() / TEST_DURATION
        pct = min(95, int(progress * 95))
        if pct != self._last_pct:
            self._last_pct = pct
            self.progress.emit(pct)


    def _write_cycle(
        self,
        fd,
        buffer,
        cycle,
        writer,
        elapsed,
        emit_progress,
    ):
        """
        Écrit séquentiellement les blocs de données sur le périphérique.
        Pour chaque bloc :
            - Écrit les données avec O_DIRECT.
            - Mesure la durée de l'opération.
            - Calcule le débit instantané.
            - Enregistre la mesure dans le CSV.
            - Transmet la mesure à la GUI.
        Retourne :
            bytes_done : nombre total d'octets écrits.
            io_time    : temps cumulé des appels d'écriture (secondes).
        """

        # INITIALISATION DES MESURES
        bytes_done = 0
        io_time = 0.0

        # ÉCRITURE SÉQUENTIELLE DES BLOCS
        for block_number in range(NUMBER_BLOCKS):
            # Arrêter le cycle si une interruption a été demandée.
            if self._abort:
                break

            # 2.1 ÉCRITURE ET CHRONOMÉTRAGE
            # Mesurer uniquement la durée de l'appel système.
            t0 = time.perf_counter_ns()
            written = os.writev(fd, [buffer])
            t1 = time.perf_counter_ns()
            # Une écriture incomplète est considérée comme une erreur.
            if written != BLOCK_SIZE:
                raise OSError(
                    f"Écriture partielle ({written} octets)"
                )

            # 2.2 CALCUL DES PERFORMANCES
            duration = (t1 - t0) / 1e9
            mbps = calculate_mbps(written, duration)
            # Temps relatif au début du test.
            rel = elapsed()
            # Cumuler les octets écrits et le temps d'E/S.
            bytes_done += written
            io_time += duration

            # 2.3 ENREGISTREMENT DES MESURES
            # Enregistrer la mesure du bloc dans le CSV.
            writer.writerow([
                cycle,
                "WRITE",
                block_number,
                rel,
                mbps,
                1,
            ])
            # Transmettre le débit à la GUI et actualiser la progression.
            self.sample.emit("WRITE", rel, mbps)
            emit_progress("write", block_number)

            # 2.4 VÉRIFICATION DE LA DURÉE DU TEST
            # En mode duration, arrêter après le bloc courant
            # dès que la durée configurée est atteinte.
            if (
                TEST_LIMIT_MODE == "duration"
                and elapsed() >= TEST_DURATION
            ):
                break

        # SYNCHRONISATION DES ÉCRITURES
        # Demander au système de synchroniser les écritures.
        # Cette opération n'est pas incluse dans les mesures de débit.
        os.fsync(fd)

        # RETOUR DES MESURES
        return bytes_done, io_time

    def _read_cycle(
        self,
        fd,
        buffer,
        reference_hash,
        cycle,
        writer,
        elapsed,
        emit_progress,
        initial_errors=0,
    ):
        """
        Lit séquentiellement les blocs et vérifie leur intégrité.
        Pour chaque bloc :
            - Lit les données avec O_DIRECT.
            - Mesure la durée de lecture.
            - Calcule le débit instantané.
            - Compare le SHA-256 au bloc de référence.
            - Enregistre les mesures dans le CSV.
            - Transmet le débit à la GUI.
        Retourne :
            bytes_done   : nombre total d'octets lus.
            io_time      : temps cumulé des appels de lecture (secondes).
            errors_found : nombre de blocs présentant une erreur d'intégrité.
        """

        # INITIALISATION DES MESURES
        bytes_done = 0
        io_time = 0.0
        errors_found = 0

        # LECTURE SÉQUENTIELLE DES BLOCS
        for block_number in range(NUMBER_BLOCKS):
            # Arrêter le cycle si une interruption a été demandée.
            if self._abort:
                break

            # 2.1 LECTURE ET CHRONOMÉTRAGE
            # Mesurer uniquement la durée de l'appel système.
            t0 = time.perf_counter_ns()
            n = os.readv(fd, [buffer])
            t1 = time.perf_counter_ns()
            # Une lecture de zéro octet est considérée comme une erreur.
            if n == 0:
                raise OSError(
                    f"Lecture interrompue au bloc {block_number}"
                )

            # 2.2 CALCUL DES PERFORMANCES
            duration = (t1 - t0) / 1e9
            mbps = calculate_mbps(n, duration)
            # Cumuler les octets lus et le temps d'E/S.
            bytes_done += n
            io_time += duration

            # 2.3 VÉRIFICATION DE L'INTÉGRITÉ
            # Le SHA-256 est calculé hors du chronométrage des E/S.
            received = bytes(buffer[:n])
            received_hash = hashlib.sha256(received).digest()
            # Vérifier la taille du bloc et son empreinte.
            if n != BLOCK_SIZE or received_hash != reference_hash:
                errors_found += 1
                integrity = 0
            else:
                integrity = 1

            # 2.4 ENREGISTREMENT DES MESURES
            rel = elapsed()
            writer.writerow([
                cycle,
                "READ",
                block_number,
                rel,
                mbps,
                integrity,
            ])
            # Transmettre le débit à la GUI et actualiser la progression.
            self.sample.emit("READ", rel, mbps)
            emit_progress("read", block_number)

            # 2.5 VÉRIFICATION DE LA DURÉE DU TEST
            # En mode duration, arrêter après le bloc courant
            # dès que la durée configurée est atteinte.
            if (
                TEST_LIMIT_MODE == "duration"
                and elapsed() >= TEST_DURATION
            ):
                break

        # RETOUR DES MESURES
        return bytes_done, io_time, errors_found

    def _evaluate_result(
        self, result, total_written, total_read,
        write_io_time, read_io_time,
        cycle_write_speeds, cycle_read_speeds,
        errors, io_failed
    ):
        """Calcule les statistiques et le verdict PASS/FAIL."""
        avg_write = calculate_mbps(total_written, write_io_time)
        avg_read = calculate_mbps(total_read, read_io_time)
        completed = (
            not self._abort
            and not io_failed
            and total_written > 0
            and total_read > 0
        )
        passed = (
            completed
            and errors == 0
            and avg_write > MIN_WRITE_MBPS
            and MIN_READ_MBPS < avg_read < MAX_READ_MBPS
        )
        result.update({
            "avg_write": avg_write,
            "avg_read": avg_read,
            "write_stats": speed_stats(cycle_write_speeds),
            "read_stats": speed_stats(cycle_read_speeds),
            "errors": errors,
            "completed": completed,
            "result": "PASS" if passed else "FAIL",
        })

    def _reformat_device(self, result):
        """Reformate la clé et actualise le résultat."""
        self.status.emit(f"Reformatage en {REFORMAT_FS.upper()}...")
        try:
            do_format_device(
                self.devnode,
                REFORMAT_FS,
                REFORMAT_LABEL,
                self.expected_diskseq,
            )
            result["reformat"] = (
                f"OK - {self.devnode} reformaté en "
                f"{REFORMAT_FS.upper()} et vérifié."
            )
        except Exception as e:
            set_result_error(result, e, "Reformatage")
            result["reformat"] = f"ÉCHEC : {result['error']}"

    def _finalize_result(self, info, result):
        """Construit le rapport et transmet les informations à la GUI."""
        result["report"] = build_report(info, result)
        self._emit_info(result["report"])
        return result