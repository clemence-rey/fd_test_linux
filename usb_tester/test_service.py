"""USB test execution service."""

from contextlib import ExitStack
import hashlib
import os
import csv
import time
from pathlib import Path


from model import (
    assert_same_device,
    assert_test_port,
    get_device_info,
    create_test_result,
    create_result_paths,
    format_device_info,
    unmount_device,
    get_device_size_bytes,
    create_aligned_buffer,
    open_test_device,
    set_result_error,
    evaluate_usb_test,
    compute_mbps,
    reformat_device,
)

from config import (
    RESULTS_DIR,
    FILE_SIZE,
    BLOCK_SIZE,
    VBUS_IDLE_DURATION_S,
    TEST_DURATION_MODE,
    NUMBER_BLOCKS,
    TEST_DURATION,
    TEST_CYCLES,
    REFORMAT_FS,
    REFORMAT_LABEL
)

from model import TestPhase, PhaseEvent

class UsbTestService:
    """Execute USB performance, integrity and reformatting tests."""

    def __init__(
        self,
        devnode,
        expected_diskseq,
        emit_status,
        emit_device_info,
        emit_sample,
        emit_progress,
        emit_phase,
        interruption_requested,
        cancellation_requested,
        abort_reason,
    ):
        """Initialize the test service and its callbacks."""

        self.devnode = devnode
        self.expected_diskseq = expected_diskseq

        self.emit_status = emit_status
        self.emit_device_info = emit_device_info
        self.emit_sample = emit_sample
        self.emit_progress = emit_progress
        self.emit_phase = emit_phase

        self.interruption_requested = interruption_requested
        self.cancellation_requested = cancellation_requested
        self.abort_reason = abort_reason


    def run(self):
        """Execute the complete USB test sequence."""

        self.current_result = create_test_result(self.devnode)
        self.device_info = {}

        try:
            return self._run_test()

        except Exception as exc:
            result = self.current_result

            set_result_error(result, exc, "USB test")
            result["reformat"] = "NOT PERFORMED (test error)"
            result["device_info"] = self.device_info.copy()

            return result

        finally:
            self._emit_phase(TestPhase.OTHER)

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
        self._emit_phase(TestPhase.OTHER)

        # IDENTIFICATION ET PRÉPARATION DU PÉRIPHÉRIQUE

        # Vérifier que la clé sélectionnée n'a pas été remplacée.
        assert_same_device(devnode, self.expected_diskseq)
        assert_test_port(devnode)

        RESULTS_DIR.mkdir(parents=True, exist_ok=True)

        # Récupérer les informations du périphérique.
        self.emit_status("Reading device information...")
        info = get_device_info(devnode)
        self.device_info = info.copy()

        # Initialiser les résultats et les chemins des rapports.
        result = create_test_result(devnode)
        self.current_result = result
        result["model"] = info["Model"]
        result["serial"] = info["Serial"]
        result.update(create_result_paths(info["Serial"]))

        self.emit_device_info(format_device_info(info))

        # Démonter les partitions pour accéder directement au disque.
        self.emit_status("Unmounting / raw access to the device...")
        unmount_device(devnode, self.expected_diskseq)

        # Vérifier que la capacité permet d'exécuter le test.
        try:
            device_size = get_device_size_bytes(devnode)
        except Exception as e:
            raise RuntimeError(
                f"Device unavailable : {e}"
            ) from e

        if device_size < FILE_SIZE * 1.1:
            raise RuntimeError("The device's storage is too small for this test")

        # PRÉPARATION DES DONNÉES ET DES MESURES

        # Générer un bloc de référence utilisé pour toutes les écritures.
        # Son empreinte SHA-256 servira à vérifier les données relues.
        reference_bytes = os.urandom(BLOCK_SIZE)
        reference_hash = hashlib.sha256(reference_bytes).digest()

        # Compteurs et statistiques du test.
        errors = 0
        total_written = total_read = 0
        write_io_time = read_io_time = 0.0
        cycle_write_measurements = []
        cycle_read_measurements = []
        
        cycle = 0

        self._cycle = 0
        self._last_pct = -1
        io_failed = False

        # Les ressources sont libérées automatiquement en sortie du bloc.
        with ExitStack() as stack:

            # Buffers alignés en mémoire pour les E/S avec O_DIRECT.
            write_buffer = stack.enter_context(
                create_aligned_buffer(BLOCK_SIZE, fill=reference_bytes)
            )
            read_buffer = stack.enter_context(
                create_aligned_buffer(BLOCK_SIZE)
            )

            # Fichier CSV contenant les mesures de chaque bloc.
            csv_handle = stack.enter_context(
                open(result["csv"], "w", newline="", encoding="utf-8")
            )

            writer = csv.writer(csv_handle)
            writer.writerow([
                "Cycle", "Operation", "Block number", "Block size",
                "Duration_s", "MB/s", "Integrity"
            ])

            # Point de départ du chronométrage global.
            self._global_start = time.perf_counter()

            # EXÉCUTION DES CYCLES D'ÉCRITURE ET DE LECTURE

            # Ouverture exclusive du disque avec O_DIRECT.
            # L'identité du périphérique est également vérifiée.
            fd = open_test_device(devnode, self.expected_diskseq)
            stack.callback(os.close, fd)

            self.emit_status("Measuring idle VBUS...")
            self._emit_phase(TestPhase.IDLE)

            deadline = time.monotonic() + VBUS_IDLE_DURATION_S

            while (time.monotonic() < deadline and not self.interruption_requested()):
                time.sleep(0.05)

            self._emit_phase(TestPhase.OTHER)

            while (
                not self._is_limit_reached()
                and not self.interruption_requested()
                and not io_failed
            ):
                cycle += 1
                self._cycle = cycle

                # Séparer visuellement les cycles sur le graphique.
                if cycle > 1:
                    self.emit_sample(
                        "SEP", float("nan"), float("nan")
                    )

                if TEST_DURATION_MODE == "cycles":
                    label_cycle = f"{cycle}/{TEST_CYCLES}"
                else:
                    label_cycle = str(cycle)

                # 3.1 ÉCRITURE

                self.emit_status(f"Cycle {label_cycle} - WRITE")
                self._emit_phase(TestPhase.WRITE)

                try:
                    os.lseek(fd, 0, os.SEEK_SET)

                    block_measurements = []
                    cycle_write_measurements.append(block_measurements)

                    written, write_time = self._write_cycle(
                        fd,
                        write_buffer,
                        cycle,
                        writer,
                        self._get_elapsed,
                        self._emit_progress,
                        block_measurements,
                    )

                    total_written += written
                    write_io_time += write_time

                except Exception as exc:
                    io_failed = True
                    set_result_error(result, exc, "Write")

                finally:
                    self._emit_phase(TestPhase.OTHER)

                # Ne pas poursuivre après une erreur ou une interruption.
                if io_failed or self.interruption_requested():
                    break

                # En mode durée, ne pas commencer la lecture si
                # la durée est déjà atteinte après l'écriture.
                if TEST_DURATION_MODE == "duration" and self._is_limit_reached():
                    break

                # 3.2 LECTURE ET VÉRIFICATION D'INTÉGRITÉ

                self.emit_status(f"Cycle {label_cycle} - READ")
                self._emit_phase(TestPhase.READ)

                try:
                    os.lseek(fd, 0, os.SEEK_SET)

                    block_measurements = []
                    cycle_read_measurements.append(block_measurements)

                    read_bytes, read_time, read_errors = self._read_cycle(
                        fd,
                        read_buffer,
                        reference_hash,
                        cycle,
                        writer,
                        self._get_elapsed,
                        self._emit_progress,
                        block_measurements,
                    )

                    total_read += read_bytes
                    read_io_time += read_time
                    errors += read_errors

                except Exception as exc:
                    io_failed = True
                    set_result_error(result, exc, "Read")

                finally:
                    self._emit_phase(TestPhase.OTHER)


        # ANNULATION VOLONTAIRE
        # Les fichiers et le périphérique brut sont maintenant fermés.
        if self.cancellation_requested():
            return self._cancel_test(result)

        # CALCUL DES STATISTIQUES ET DU VERDICT

        result.update(
            evaluate_usb_test(
                total_written=total_written,
                total_read=total_read,
                write_io_time=write_io_time,
                read_io_time=read_io_time,
                cycle_write_measurements=cycle_write_measurements,
                cycle_read_measurements=cycle_read_measurements,
                integrity_errors=errors,
                interrupted=self.interruption_requested(),
                io_failed=io_failed,
            )
        )

        # GESTION DES INTERRUPTIONS ET DES ERREURS D'E/S

        if self.interruption_requested() or io_failed:
            result["result"] = "FAIL"
            result["completed"] = False

            if self.interruption_requested():
                result["error"] = self.abort_reason()

            # Ne jamais reformater après une interruption ou erreur d'E/S.
            result["reformat"] = "NOT PERFORMED (test interrupted)"
            result["device_info"] = info.copy()
            return result

        # REFORMATAGE DU PÉRIPHÉRIQUE

        # Le reformatage est exécuté même si les débits sont insuffisants.
        # Un échec du reformatage entraîne un verdict FAIL.
        self._reformat_device(result)

        # GÉNÉRATION DU RAPPORT FINAL

        result["device_info"] = info.copy()
        return result

    # --- Exécution des cycles de mesure ---

    def _write_cycle(
        self,
        fd,
        buffer,
        cycle,
        writer,
        elapsed,
        emit_progress,
        block_measurements,
    ):
        """
        Écrit séquentiellement les blocs sur le périphérique avec O_DIRECT.

        Pour chaque bloc :
            - Mesure la durée de l'écriture.
            - Calcule le débit instantané.
            - Enregistre la mesure dans le CSV.
            - Transmet la mesure et la progression à la GUI.

        Retourne le nombre d'octets écrits et le temps cumulé des E/S.
        """

        # INITIALISATION DES MESURES
        bytes_done = 0
        io_time = 0.0

        # ÉCRITURE SÉQUENTIELLE DES BLOCS
        for block_number in range(NUMBER_BLOCKS):

            # Arrêter le cycle si une interruption est demandée.
            if self.interruption_requested():
                break

            # ÉCRITURE ET CHRONOMÉTRAGE
            t0 = time.perf_counter_ns()
            written = os.writev(fd, [buffer])
            duration = (time.perf_counter_ns() - t0) / 1e9

            # Une écriture partielle est considérée comme une erreur.
            if written != BLOCK_SIZE:
                raise OSError(
                    f"Partial writing at block {block_number} "
                    f"({written}/{BLOCK_SIZE} bytes)"
                )

            # CALCUL DES PERFORMANCES
            bytes_done += written
            io_time += duration

            mbps = compute_mbps(written, duration)
            block_measurements.append((written, duration))

            rel = elapsed()

            # ENREGISTREMENT DES MESURES
            writer.writerow([
                cycle,
                "WRITE",
                block_number,
                BLOCK_SIZE,
                duration,
                mbps,
                1,
            ])

            self.emit_sample("WRITE", rel, mbps)
            emit_progress("write", block_number)

            # VÉRIFICATION DE LA DURÉE
            if (
                TEST_DURATION_MODE == "duration"
                and elapsed() >= TEST_DURATION
            ):
                break

        # SYNCHRONISATION DES ÉCRITURES
        # Hors chronométrage des performances.
        # Ne pas prolonger inutilement un test interrompu.
        if not self.interruption_requested():
            os.fsync(fd)

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
        block_measurements,
    ):
        """
        Lit séquentiellement les blocs et vérifie leur intégrité.

        Pour chaque bloc :
            - Mesure la durée de lecture avec O_DIRECT.
            - Calcule le débit instantané.
            - Vérifie la taille et l'empreinte SHA-256.
            - Enregistre la mesure dans le CSV.
            - Transmet la mesure et la progression à la GUI.

        Retourne les octets lus, le temps d'E/S et les erreurs d'intégrité.
        """

        # INITIALISATION DES MESURES
        bytes_done = 0
        io_time = 0.0
        errors_found = 0

        # LECTURE SÉQUENTIELLE DES BLOCS
        for block_number in range(NUMBER_BLOCKS):

            # Arrêter le cycle si une interruption est demandée.
            if self.interruption_requested():
                break

            # LECTURE ET CHRONOMÉTRAGE
            t0 = time.perf_counter_ns()
            n = os.readv(fd, [buffer])
            duration = (time.perf_counter_ns() - t0) / 1e9

            # Une lecture nulle indique une erreur.
            if n == 0:
                raise OSError(
                    f"Read operation interrupted at the data block {block_number}"
                )

            # CALCUL DES PERFORMANCES
            bytes_done += n
            io_time += duration

            mbps = compute_mbps(n, duration)
            block_measurements.append((n, duration))

            # VÉRIFICATION DE L'INTÉGRITÉ
            # Le calcul SHA-256 reste hors du chronométrage des E/S.
            received_hash = hashlib.sha256(buffer[:n]).digest()

            integrity = int(
                n == BLOCK_SIZE
                and received_hash == reference_hash
            )

            if not integrity:
                errors_found += 1

            # ENREGISTREMENT DES MESURES
            rel = elapsed()

            writer.writerow([
                cycle,
                "READ",
                block_number,
                BLOCK_SIZE,
                duration,
                mbps,
                integrity,
            ])

            self.emit_sample("READ", rel, mbps)
            emit_progress("read", block_number)

            # VÉRIFICATION DE LA DURÉE
            if (
                TEST_DURATION_MODE == "duration"
                and elapsed() >= TEST_DURATION
            ):
                break

        return bytes_done, io_time, errors_found

    # --- Gestion du temps et de la progression ---

    def _get_elapsed(self):
        """Retourne le temps écoulé depuis le début des mesures (secondes)."""
        return time.perf_counter() - self._global_start


    def _is_limit_reached(self):
        """Indique si la limite de cycles ou de durée du test est atteinte."""
        if TEST_DURATION_MODE == "cycles":
            return self._cycle >= TEST_CYCLES
        return self._get_elapsed() >= TEST_DURATION


    def _emit_progress(self, phase, block_number):
        """Transmet à la GUI une progression limitée à 95 % pendant les E/S."""

        if TEST_DURATION_MODE == "cycles":
            fraction = (block_number + 1) / NUMBER_BLOCKS

            if phase == "write":
                cycle_progress = 0.5 * fraction
            else:
                cycle_progress = 0.5 + 0.5 * fraction

            progress = ((self._cycle - 1) + cycle_progress) / TEST_CYCLES

        else:
            progress = self._get_elapsed() / TEST_DURATION

        pct = min(95, int(progress * 95))

        if pct != self._last_pct:
            self._last_pct = pct
            self.emit_progress(pct)


    # --- Reformatage et annulation ---

    def _reformat_device(self, result):
        """Reformate la clé et actualise le résultat."""

        # Ne pas reformater si une interruption a été demandée.
        if self.interruption_requested():
            result["result"] = "FAIL"
            result["completed"] = False
            result["error"] = self.abort_reason()
            result["reformat"] = "NOT PERFORMED (test interrupted)"
            return

        self.emit_status(f"Reformatting to {REFORMAT_FS.upper()}...")

        try:
            reformat_device(
                self.devnode,
                REFORMAT_FS,
                REFORMAT_LABEL,
                self.expected_diskseq,
            )

            result["reformat"] = (
                f"OK - {self.devnode} reformatted to "
                f"{REFORMAT_FS.upper()} and verified."
            )

        except Exception as e:
            set_result_error(result, e, "Reformatting")
            result["reformat"] = f"FAILURE : {result['error']}"


    def _cancel_test(self, result):
        """Nettoie les résultats et tente de restaurer la clé après annulation."""

        self.emit_status("Cancellation: Cleaning up files...")

        # Supprimer uniquement les fichiers associés au test courant.
        for key in ("docx", "csv"):
            path = result.get(key)

            if path:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError as exc:
                    self.emit_status(f"Cannot delete : {exc}")

        # Vérifier l'identité de la clé avant toute opération destructive.
        try:
            assert_test_port(self.devnode)
            assert_same_device(self.devnode, self.expected_diskseq)

            self.emit_status(
                f"Cancellation: Reformat to {REFORMAT_FS.upper()}..."
            )

            reformat_device(
                self.devnode,
                REFORMAT_FS,
                REFORMAT_LABEL,
                self.expected_diskseq,
            )

        except Exception as exc:
            result["result"] = "CANCEL_FAILED"
            result["error"] = str(exc)
            result["reformat"] = f"FAILURE : {exc}"
            return result

        result["result"] = "CANCELLED"
        result["completed"] = False
        result["reformat"] = "OK"
        result["report"] = ""

        return result


    def _emit_phase(self, phase):
        """Publish a timestamped test-phase transition."""

        self.emit_phase(
            PhaseEvent(
                timestamp_ns=time.monotonic_ns(),
                phase=phase,
            )
        )

