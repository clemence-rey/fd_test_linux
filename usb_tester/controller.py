"""Surveillance USB et exécution des tests dans des threads Qt indépendants."""

import hashlib
import os
import time

import pyudev
from pathlib import Path
from contextlib import ExitStack
from PyQt5.QtCore import QThread, pyqtSignal

from config import (
    RESULTS_DIR, FILE_SIZE, BLOCK_SIZE, NUMBER_BLOCKS, TEST_LIMIT_MODE,
    TEST_CYCLES, TEST_DURATION, MIN_WRITE_MBPS, MIN_READ_MBPS, MAX_READ_MBPS,
    REFORMAT_FS, REFORMAT_LABEL,
)
from model import (
    compute_speed_stats, 
    set_result_error, 
    assert_same_device, 
    get_diskseq,
    assert_test_port, 
    get_device_info,
    create_result_paths, 
    format_device_info, 
    unmount_device,
    get_device_size_bytes, 
    create_aligned_buffer, 
    open_test_device, 
    compute_mbps,
    reformat_device, 
    build_report, 
    scan_usb_candidates,
    create_test_result,
)

# ============================================================
# 1. SURVEILLANCE DES PÉRIPHÉRIQUES USB
# ============================================================

class UsbWatcher(QThread):
    """Surveille les changements du périphérique sur le port USB réservé."""

    usb_event = pyqtSignal(str, str)  # devnode, "inserted" / "removed"

    def __init__(self):
        super().__init__()
        self._current_devnode = None
        self._current_diskseq = None
  
  
    def run(self):
        """Surveille les événements udev et actualise l'état du port USB."""
        context = pyudev.Context()
        monitor = pyudev.Monitor.from_netlink(context)
        monitor.filter_by(subsystem="block")
        monitor.start()

        # Détecter une clé déjà branchée au démarrage.
        self._update_device()

        scan_pending = False
        last_event = 0.0
        last_scan = time.monotonic()

        while not self.isInterruptionRequested():
            device = monitor.poll(timeout=0.1)

            if device is not None and device.action in ("add", "remove", "change"):

                # Retrait immédiat du disque surveillé.
                if (
                    device.action == "remove"
                    and device.device_type == "disk"
                    and device.device_node == self._current_devnode
                ):
                    self.usb_event.emit(self._current_devnode, "removed")
                    self._current_devnode = None
                    self._current_diskseq = None

                # Attendre la stabilisation des autres événements.
                scan_pending = True
                last_event = time.monotonic()

            now = time.monotonic()

            if (scan_pending and now - last_event >= 0.8) or now - last_scan >= 2.0:
                self._update_device()
                scan_pending = False
                last_scan = now


    def stop(self):
        """Demande l'arrêt du thread de surveillance."""
        self.requestInterruption()


    def _scan_device(self):
        """Retourne le périphérique autorisé et son identité."""
        candidates = scan_usb_candidates()

        if len(candidates) > 1:
            raise RuntimeError(
                "Plusieurs disques détectés sur le port USB réservé"
            )

        if not candidates:
            return None, None

        devnode = candidates[0]

        try:
            diskseq = get_diskseq(devnode)
        except (OSError, RuntimeError):
            return None, None

        return devnode, diskseq


    def _update_device(self):
        """Actualise le périphérique surveillé et signale les changements."""
        try:
            devnode, diskseq = self._scan_device()
        except RuntimeError:
            # Situation ambiguë : ne sélectionner aucun disque.
            devnode, diskseq = None, None

        if (devnode, diskseq) == (self._current_devnode, self._current_diskseq):
            return

        if self._current_devnode is not None:
            self.usb_event.emit(self._current_devnode, "removed")

        self._current_devnode = devnode
        self._current_diskseq = diskseq

        if self._current_devnode is not None:
            self.usb_event.emit(self._current_devnode, "inserted")

# ============================================================
# 2. EXÉCUTION DES TESTS USB
# ============================================================

class TestWorker(QThread):
    """Exécute les tests de lecture, écriture, intégrité et reformatage."""

    status = pyqtSignal(str)
    info_ready = pyqtSignal(str)
    sample = pyqtSignal(str, float, float)  # op, temps (s), MB/s
    progress = pyqtSignal(int)
    test_done = pyqtSignal(dict)

    # --- Initialisation et contrôle du thread ---

    def __init__(self, devnode, expected_diskseq):
        """Initialise les paramètres et l'état du thread."""
        super().__init__()

        self.devnode = devnode
        self.expected_diskseq = expected_diskseq

        self._abort_reason = "Test interrompu"

        self.device_info = None
        self.current_result = None
        self.cancel_requested = False
 
 
    def run(self):
        """
        Point d'entrée du thread de test.

        Exécute le test USB, intercepte les erreurs non gérées
        et transmet le résultat final à la GUI, même si le
        rapport d'erreur ne peut pas être généré.
        """

        # --- INITIALISATION ET EXÉCUTION DU TEST ---

        result = create_test_result(self.devnode)

        try:
            result = self._run_test()

        except Exception as e:

            if self.current_result is not None:
                result = self.current_result

            # Si une annulation a été demandée, tenter le nettoyage.
            if self.cancel_requested:
                result = self._cancel_test(result)

            else:
                # Traitement habituel des erreurs.
                set_result_error(result, e, "Test")
                result["result"] = "FAIL"
                result["completed"] = False
                result["reformat"] = "NON EFFECTUÉ (test interrompu)"

                if self.isInterruptionRequested():
                    result["error"] = self._abort_reason

            info = self.device_info

            if info is None:
                try:
                    info = get_device_info(self.devnode)
                except Exception:
                    # La clé peut avoir été déconnectée.
                    # Ne pas masquer l'erreur initiale.
                    info = {
                        "Model": result.get("model", "N/A"),
                        "Serial": result.get("serial", "N/A"),
                    }

            # --- GÉNÉRATION DU RAPPORT D'ERREUR ---

            try:
                result["report"] = build_report(info, result)

            except Exception as report_error:
                # Le rapport ne doit pas empêcher le verdict final.
                result["report"] = (
                    f"FAIL - {result.get('error', 'Erreur inconnue')}\n"
                    f"Rapport indisponible : {report_error}"
                )

            # Transmettre le rapport ou son texte de secours.
            self.info_ready.emit(result["report"])

        # --- TRANSMISSION DU RÉSULTAT À LA GUI ---

        finally:
            self.test_done.emit(result)


    def abort(self, reason="Test interrompu"):
        """Demande l'interruption du test avec une raison explicite."""
        self._abort_reason = reason
        self.requestInterruption()


    def cancel(self):
        """Demande une annulation volontaire du test."""
        self.cancel_requested = True
        self.requestInterruption()

    # --- Orchestration du test ---

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
        assert_test_port(devnode)

        RESULTS_DIR.mkdir(parents=True, exist_ok=True)

        # Récupérer les informations du périphérique.
        self.status.emit("Lecture des informations du périphérique...")
        info = get_device_info(devnode)
        self.device_info = info.copy()

        # Initialiser les résultats et les chemins des rapports.
        result = create_test_result(devnode)
        self.current_result = result
        result["model"] = info["Model"]
        result["serial"] = info["Serial"]
        result.update(create_result_paths(info["Serial"]))

        self.info_ready.emit(format_device_info(info))

        # Démonter les partitions pour accéder directement au disque.
        self.status.emit("Démontage / accès brut au périphérique...")
        unmount_device(devnode, self.expected_diskseq)

        # Vérifier que la capacité permet d'exécuter le test.
        try:
            device_size = get_device_size_bytes(devnode)
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

        # Les ressources sont libérées automatiquement en sortie du bloc.
        with ExitStack() as stack:

            # Buffers alignés en mémoire pour les E/S avec O_DIRECT.
            write_buffer = stack.enter_context(
                create_aligned_buffer(BLOCK_SIZE, fill=reference_bytes)
            )
            read_buffer = stack.enter_context(
                create_aligned_buffer(BLOCK_SIZE)
            )

            # Point de départ du chronométrage global.
            self._global_start = time.perf_counter()

            # EXÉCUTION DES CYCLES D'ÉCRITURE ET DE LECTURE

            # Ouverture exclusive du disque avec O_DIRECT.
            # L'identité du périphérique est également vérifiée.
            fd = open_test_device(devnode, self.expected_diskseq)
            stack.callback(os.close, fd)

            while (
                not self._is_limit_reached()
                and not self.isInterruptionRequested()
                and not io_failed
            ):
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

                try:
                    # Recommencer l'écriture au début du disque.
                    os.lseek(fd, 0, os.SEEK_SET)

                    written, write_time = self._write_cycle(
                        fd,
                        write_buffer,
                        cycle,
                        self._get_elapsed,
                        self._emit_progress,
                    )

                    # Cumuler les octets et le temps d'E/S.
                    total_written += written
                    write_io_time += write_time

                    # Enregistrer le débit moyen de cette phase.
                    if write_time > 0:
                        cycle_write_speeds.append(
                            compute_mbps(written, write_time)
                        )

                except Exception as e:
                    io_failed = True
                    set_result_error(result, e, "Écriture")

                # Ne pas poursuivre après une erreur ou une interruption.
                if io_failed or self.isInterruptionRequested():
                    break

                # En mode durée, ne pas commencer la lecture si
                # la durée est déjà atteinte après l'écriture.
                if TEST_LIMIT_MODE == "duration" and self._is_limit_reached():
                    break

                # 3.2 LECTURE ET VÉRIFICATION D'INTÉGRITÉ

                self.status.emit(f"Cycle {label_cycle} - LECTURE")

                try:
                    # Relire les données depuis le début du disque.
                    os.lseek(fd, 0, os.SEEK_SET)

                    read_bytes, read_time, read_errors = self._read_cycle(
                        fd,
                        read_buffer,
                        reference_hash,
                        cycle,
                        self._get_elapsed,
                        self._emit_progress,
                    )

                    # Cumuler les octets, le temps et les erreurs.
                    total_read += read_bytes
                    read_io_time += read_time
                    errors += read_errors

                    # Enregistrer le débit moyen de cette phase.
                    if read_time > 0:
                        cycle_read_speeds.append(
                            compute_mbps(read_bytes, read_time)
                        )

                except Exception as e:
                    io_failed = True
                    set_result_error(result, e, "Lecture")

        # ANNULATION VOLONTAIRE
        # Les fichiers et le périphérique brut sont maintenant fermés.
        if self.cancel_requested:
            return self._cancel_test(result)

        # CALCUL DES STATISTIQUES ET DU VERDICT

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

        if self.isInterruptionRequested() or io_failed:
            result["result"] = "FAIL"
            result["completed"] = False

            if self.isInterruptionRequested():
                result["error"] = self._abort_reason

            # Ne jamais reformater après une interruption ou erreur d'E/S.
            result["reformat"] = "NON EFFECTUÉ (test interrompu)"
            return self._build_and_emit_report(info, result)

        # REFORMATAGE DU PÉRIPHÉRIQUE

        # Le reformatage est exécuté même si les débits sont insuffisants.
        # Un échec du reformatage entraîne un verdict FAIL.
        self._reformat_device(result)

        # GÉNÉRATION DU RAPPORT FINAL

        return self._build_and_emit_report(info, result)

    # --- Exécution des cycles de mesure ---

    def _write_cycle(
        self,
        fd,
        buffer,
        cycle,
        elapsed,
        emit_progress,
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
            if self.isInterruptionRequested():
                break

            # ÉCRITURE ET CHRONOMÉTRAGE
            t0 = time.perf_counter_ns()
            written = os.writev(fd, [buffer])
            duration = (time.perf_counter_ns() - t0) / 1e9

            # Une écriture partielle est considérée comme une erreur.
            if written != BLOCK_SIZE:
                raise OSError(
                    f"Écriture partielle au bloc {block_number} "
                    f"({written}/{BLOCK_SIZE} octets)"
                )

            # CALCUL DES PERFORMANCES
            bytes_done += written
            io_time += duration

            mbps = compute_mbps(written, duration)
            rel = elapsed()

            self.sample.emit("WRITE", rel, mbps)
            emit_progress("write", block_number)

            # VÉRIFICATION DE LA DURÉE
            if (
                TEST_LIMIT_MODE == "duration"
                and elapsed() >= TEST_DURATION
            ):
                break

        # SYNCHRONISATION DES ÉCRITURES
        # Hors chronométrage des performances.
        # Ne pas prolonger inutilement un test interrompu.
        if not self.isInterruptionRequested():
            os.fsync(fd)

        return bytes_done, io_time


    def _read_cycle(
        self,
        fd,
        buffer,
        reference_hash,
        cycle,
        elapsed,
        emit_progress,
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
            if self.isInterruptionRequested():
                break

            # LECTURE ET CHRONOMÉTRAGE
            t0 = time.perf_counter_ns()
            n = os.readv(fd, [buffer])
            duration = (time.perf_counter_ns() - t0) / 1e9

            # Une lecture nulle indique une erreur.
            if n == 0:
                raise OSError(
                    f"Lecture interrompue au bloc {block_number}"
                )

            # CALCUL DES PERFORMANCES
            bytes_done += n
            io_time += duration

            mbps = compute_mbps(n, duration)

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

            self.sample.emit("READ", rel, mbps)
            emit_progress("read", block_number)

            # VÉRIFICATION DE LA DURÉE
            if (
                TEST_LIMIT_MODE == "duration"
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
        if TEST_LIMIT_MODE == "cycles":
            return self._cycle >= TEST_CYCLES
        return self._get_elapsed() >= TEST_DURATION


    def _emit_progress(self, phase, block_number):
        """Transmet à la GUI une progression limitée à 95 % pendant les E/S."""

        if TEST_LIMIT_MODE == "cycles":
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
            self.progress.emit(pct)

    # --- Évaluation des résultats ---

    def _evaluate_result(
        self, result, total_written, total_read,
        write_io_time, read_io_time,
        cycle_write_speeds, cycle_read_speeds,
        errors, io_failed
    ):
        """Calcule les statistiques et le verdict PASS/FAIL."""

        avg_write = compute_mbps(total_written, write_io_time)
        avg_read = compute_mbps(total_read, read_io_time)

        completed = (
            not self.isInterruptionRequested()
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
            "write_stats": compute_speed_stats(cycle_write_speeds),
            "read_stats": compute_speed_stats(cycle_read_speeds),
            "errors": errors,
            "completed": completed,
            "result": "PASS" if passed else "FAIL",
        })

    # --- Reformatage et annulation ---

    def _reformat_device(self, result):
        """Reformate la clé et actualise le résultat."""

        # Ne pas reformater si une interruption a été demandée.
        if self.isInterruptionRequested():
            result["result"] = "FAIL"
            result["completed"] = False
            result["error"] = self._abort_reason
            result["reformat"] = "NON EFFECTUÉ (test interrompu)"
            return

        self.status.emit(f"Reformatage en {REFORMAT_FS.upper()}...")

        try:
            reformat_device(
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


    def _cancel_test(self, result):
        """Nettoie les résultats et tente de restaurer la clé après annulation."""

        self.status.emit("Annulation : nettoyage des fichiers...")

        # Supprimer uniquement les fichiers associés au test courant.
        for key in ("docx"):
            path = result.get(key)

            if path:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError as exc:
                    self.status.emit(f"Suppression impossible : {exc}")

        # Vérifier l'identité de la clé avant toute opération destructive.
        try:
            assert_test_port(self.devnode)
            assert_same_device(self.devnode, self.expected_diskseq)

            self.status.emit(
                f"Annulation : reformatage en {REFORMAT_FS.upper()}..."
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
            result["reformat"] = f"ÉCHEC : {exc}"
            return result

        result["result"] = "CANCELLED"
        result["completed"] = False
        result["reformat"] = "OK"
        result["report"] = ""

        return result
    
    # --- Finalisation du test ---

    def _build_and_emit_report(self, info, result):
        """Construit le rapport et transmet les informations à la GUI."""
        result["report"] = build_report(info, result)
        self.info_ready.emit(result["report"])
        return result


