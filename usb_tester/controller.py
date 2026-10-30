"""Surveillance USB et exécution des tests dans des threads Qt indépendants."""

import time
from datetime import datetime
from enum import Enum, auto
import pyudev
from PyQt5.QtCore import QThread, pyqtSignal, QObject, QTimer


from config import (
    RESULTS_DIR, 
    TEST_DURATION_MODE,
    TEST_CYCLES, 
    TEST_DURATION, 
    REFORMAT_FS, 
    VBUS_SAMPLE_INTERVAL_S,
    TEMPLATE_PATH,
    TEST_FILE_SIZE_MB,
    BLOCK_SIZE_MB,
    STATION_NAME,
    SOFTWARE_VERSION,
    MAX_MATING_CYCLES
)

from model import (
    assert_same_device, 
    get_diskseq,
    assert_test_port, 
    get_device_info,
    format_device_info, 
    build_report, 
    scan_usb_candidates,
    increment_test_link_counter,
    compute_vbus_phase_stats,
    write_docx_report,
    load_test_link_counters
)

from acroname import USBExt3cAdapter

from model import PhaseEvent, TestPhase, VbusSample

from test_service import UsbTestService


class AppState(Enum):
    WAITING_INSERT = auto()
    READY = auto()
    TESTING = auto()
    WAITING_RESTART = auto()


class AppController(QObject):
    """Coordinate the USB tester application."""

    def __init__(self, view, test_config, parent=None):
        super().__init__(parent)

        self.view = view
        self.test_config = test_config

        self.state = AppState.WAITING_INSERT

        self.current_devnode = None
        self.current_diskseq = None

        self.connected_devnode = None
        self.connected_diskseq = None

        self.device_removed = False
        self.new_device_ready = False
        self.waiting_for_removal = False

        self.test_worker = None
        self.vbus_worker = None
        self.watcher = None

        self.vbus_samples = []
        self.phase_events = []
        self.vbus_error = ""

        self.pending_result = None
        self.current_phase = TestPhase.OTHER

        self.closing = False
        self.test_worker_started = False

        self._connect_view()

    def _connect_view(self):
        """Connect user intentions to controller actions."""

        self.view.start_requested.connect(self.start_test)
        self.view.cancel_requested.connect(self.cancel_test)
        self.view.restart_requested.connect(self.restart_test)
        self.view.close_requested.connect(self.shutdown)

    def start(self):
        """Initialize application state and start USB monitoring."""

        self.view.show_waiting_insert()
        self._refresh_test_link_display()

        self.watcher = UsbWatcher()
        self.watcher.usb_event.connect(self.on_usb_event)
        self.watcher.finished.connect(self._on_thread_finished)
        self.watcher.start()

        # --- Gestion des états de l'interface ---

    # --- Surveillance des périphériques USB ---

    def on_usb_event(self, devnode, event):
        """Handle insertion and removal of the test device."""

        if event == "inserted":
            try:
                assert_test_port(devnode)
                diskseq = get_diskseq(devnode)

            except (OSError, RuntimeError) as exc:
                self.view.set_step_status(f"Device unavailable: {exc}")
                return

            is_new_device = (
                self.connected_devnode,
                self.connected_diskseq,
            ) != (devnode, diskseq)

            self.connected_devnode = devnode
            self.connected_diskseq = diskseq

            if is_new_device:
                try:
                    increment_test_link_counter(
                        self.test_config["test_link"]
                    )
                    self._refresh_test_link_display()
                except (OSError, ValueError, TypeError) as exc:
                    self.view.set_step_status(f"Counter error: {exc}")

            if self.state in (
                AppState.WAITING_INSERT,
                AppState.READY,
            ):
                self._set_ready(devnode, diskseq)

            elif self.state == AppState.WAITING_RESTART:
                if self.device_removed:
                    self.new_device_ready = True

        elif event == "removed":
            if devnode != self.connected_devnode:
                return

            self.connected_devnode = None
            self.connected_diskseq = None

            if self.state == AppState.TESTING:
                if self.test_worker is not None:
                    self.test_worker.abort("Device disconnected")

            elif self.state == AppState.READY:
                self._set_waiting_insert()

            elif self.state == AppState.WAITING_RESTART:
                self.device_removed = True
                self.new_device_ready = False

    def _get_device_label(self, info):
        """Construit le libellé du périphérique à partir du fabricant et du modèle."""
        manufacturer = str(info.get("Manufacturer") or "N/A").strip()
        model = str(info.get("Model") or "N/A").strip()

        return " - ".join(
            x for x in (manufacturer, model) if x and x != "N/A"
        )

    def _set_ready(self, devnode, diskseq):
        """Validate a detected device and place the application in READY."""

        try:
            assert_test_port(devnode)

            if diskseq is None:
                raise RuntimeError("USB ID not available")

            actual_diskseq = get_diskseq(devnode)

            if actual_diskseq != diskseq:
                raise RuntimeError(
                    "The USB flash drive changed while being detected"
                )

            info = get_device_info(devnode)

            assert_same_device(devnode, actual_diskseq)

        except (OSError, RuntimeError) as exc:
            self._set_waiting_insert()
            self.view.set_step_status(
                f"USB detection failed: {exc}"
            )
            return

        self.state = AppState.READY
        self.current_devnode = devnode
        self.current_diskseq = actual_diskseq

        self.view.show_ready(
            device_label=self._get_device_label(info),
            device_info_text=format_device_info(info),
        )

    def _start_workers(self):
        """Start electrical monitoring, then the USB test."""

        self.vbus_samples = []
        self.phase_events = []
        self.vbus_error = ""
        self.pending_result = None

        self.current_phase = TestPhase.OTHER
        self.test_worker_started = False

        self.test_worker = TestWorker(
            self.current_devnode,
            self.current_diskseq,
        )

        self.test_worker.status.connect(
            self.view.set_step_status
        )

        self.test_worker.info_ready.connect(
            self.view.set_result_text
        )

        self.test_worker.sample.connect(
            self.view.add_speed_sample
        )

        self.test_worker.progress.connect(
            self.view.set_progress
        )

        self.test_worker.phase_changed.connect(
            self._on_phase_changed
        )

        self.test_worker.test_done.connect(
            self._on_test_done
        )

        self.test_worker.finished.connect(
            self._on_test_worker_finished
        )

        self.vbus_worker = VbusWorker()

        self.vbus_worker.ready.connect(
            self._on_vbus_ready
        )

        self.vbus_worker.sample_ready.connect(
            self._on_vbus_sample
        )

        self.vbus_worker.error.connect(
            self._on_vbus_error
        )

        self.vbus_worker.finished.connect(
            self._on_vbus_worker_finished
        )

        self.vbus_worker.start()

    def _on_vbus_ready(self):
        """Start the test when VBUS monitoring is ready."""

        self._start_test_worker_once()

    def _on_vbus_error(self, message):
        """Record an Acroname failure without blocking the USB test."""

        self.vbus_error = message
        self.view.set_step_status(
            f"VBUS monitoring unavailable: {message}"
        )

        self._start_test_worker_once()

    def _start_test_worker_once(self):
        """Start TestWorker exactly once."""

        if self.test_worker_started:
            return

        self.test_worker_started = True
        self.test_worker.start()

    def _on_phase_changed(self, event):
        """Store a timestamped phase transition."""

        self.phase_events.append(event)
        self.current_phase = event.phase


    def _on_vbus_sample(self, sample):
        """Store and display one raw electrical sample."""

        self.vbus_samples.append(sample)

        self.view.show_vbus_sample(
            self.current_phase,
            sample,
        )

    def _on_test_done(self, result):
        """Store the result and stop electrical monitoring."""

        self.pending_result = result

        if self.vbus_worker is not None and self.vbus_worker.isRunning():
            self.vbus_worker.stop()
        else:
            QTimer.singleShot(0, self._finalize_test)

    def _on_vbus_worker_finished(self):
        """Finalize the test after the last VBUS sample."""

        worker = self.sender()

        if worker is not None:
            worker.deleteLater()

        if self.vbus_worker is worker:
            self.vbus_worker = None

        if self.pending_result is not None:
            QTimer.singleShot(0, self._finalize_test)

        self._try_finish_shutdown()
        

    def _finalize_test(self):
        """Compute final statistics, build report and update the View."""

        result = self.pending_result
        self.pending_result = None

        if result is None:
            return

        if result["result"] == "CANCELLED":
            self._set_waiting_insert()
            return

        if result["result"] == "CANCEL_FAILED":
            self.state = AppState.WAITING_RESTART
            self.view.show_cancel_failed(
                result.get("error", "Unknown error")
            )
            return

        result["vbus_stats"] = compute_vbus_phase_stats(
            samples=self.vbus_samples,
            phase_events=self.phase_events,
        )

        result["vbus_error"] = self.vbus_error

        info = result.get("device_info", {})

        result["report"] = build_report(info, result)

        self.view.prepare_final_graph(result)

        report_saved = self._write_report(result)

        self.state = AppState.WAITING_RESTART

        self.view.show_final_result(
            result=result,
            report_saved=report_saved,
        )

    def _write_report(self, result):
        """Save the final report and return whether it succeeded."""

        path = result.get("docx") or str(
            RESULTS_DIR
            / f"USB_report_{datetime.now():%Y%m%d_%H%M%S}.docx"
        )

        try:
            write_docx_report(
                template_path=TEMPLATE_PATH,
                output_path=path,
                report_text=result.get("report", ""),
                figure=self.view.get_report_figure(),
                of_number=self.test_config["of"],
                sap=self.test_config["sap"],
                device=result.get("model", "N/A"),
                serial=result.get("serial", "N/A"),
                file_size=f"{TEST_FILE_SIZE_MB} MB",
                block_size=f"{BLOCK_SIZE_MB} MB",
                test_duration=(
                    f"{TEST_CYCLES} cycles"
                    if TEST_DURATION_MODE == "cycles"
                    else f"{TEST_DURATION:g} s"
                ),
                final_fs=REFORMAT_FS.upper(),
                station=STATION_NAME,
                soft_version=SOFTWARE_VERSION,
            )

        except Exception as exc:
            self.view.set_step_status(
                f"Report failed: {exc}"
            )
            return False

        self.view.set_step_status(
            f"Report saved: {path}"
        )

        return True

    def _refresh_test_link_display(self):
        """Read the selected test-link counter and update the View."""

        test_link = self.test_config["test_link"]
        counters = load_test_link_counters()

        link_data = counters.get(test_link, {})

        self.view.set_test_link_info(
            reference=test_link,
            cycles=link_data.get("cycles", 0),
            max_cycles=link_data.get(
                "max_cycles",
                MAX_MATING_CYCLES,
            ),
        )

    def shutdown(self):
        """Request all active workers to stop safely."""

        if self.closing:
            return

        self.closing = True

        if self.test_worker is not None and self.test_worker.isRunning():
            self.test_worker.abort("Application closing")

        if self.vbus_worker is not None and self.vbus_worker.isRunning():
            self.vbus_worker.stop()

        if self.watcher is not None and self.watcher.isRunning():
            self.watcher.stop()

        self._try_finish_shutdown()

    def _on_thread_finished(self):
        """Recheck whether application shutdown can complete."""

        self._try_finish_shutdown()

    def _try_finish_shutdown(self):
        """Close the View when no worker is still running."""

        if not self.closing:
            return

        active_threads = (
            self.test_worker,
            self.vbus_worker,
            self.watcher,
        )

        if any(
            thread is not None and thread.isRunning()
            for thread in active_threads
        ):
            return

        self.view.allow_close()

    def _set_waiting_insert(self):
        """Return to the initial waiting state."""

        self.state = AppState.WAITING_INSERT
        self.current_devnode = None
        self.current_diskseq = None
        self.view.show_waiting_insert()


    def start_test(self):
        """Validate the selected device and start the test."""

        if self.state != AppState.READY:
            return

        try:
            assert_test_port(self.current_devnode)
            assert_same_device(
                self.current_devnode,
                self.current_diskseq,
            )

        except (OSError, RuntimeError) as exc:
            self.view.set_step_status(f"Cannot start test: {exc}")
            self._set_waiting_insert()
            return

        self.state = AppState.TESTING
        self.device_removed = False
        self.new_device_ready = False

        self.view.show_testing()
        self._start_workers()


    def cancel_test(self):
        """Request cancellation of the running test."""

        if self.state != AppState.TESTING:
            return

        self.view.show_cancelling()

        if self.test_worker is not None:
            self.test_worker.cancel()


    def restart_test(self):
        """Prepare the next test."""

        if self.state != AppState.WAITING_RESTART:
            return

        ready_for_next_test = (
            self.new_device_ready
            and self.connected_devnode is not None
        )

        devnode = self.connected_devnode
        diskseq = self.connected_diskseq

        self.new_device_ready = False
        self.device_removed = False

        self._set_waiting_insert()

        if ready_for_next_test:
            self._set_ready(devnode, diskseq)


    def _on_test_worker_finished(self):
        """Release the finished test worker."""

        worker = self.sender()

        if worker is not None:
            worker.deleteLater()

        if self.test_worker is worker:
            self.test_worker = None

        self._try_finish_shutdown()

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
        """Surveille les événements USB et détecte rapidement les périphériques."""
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

                # Détection immédiate d'un nouveau disque.
                if device.action == "add" and device.device_type == "disk":
                    self._update_device()

                # Prévoir un nouveau scan après stabilisation des événements.
                scan_pending = True
                last_event = time.monotonic()

            now = time.monotonic()

            # Scan après stabilisation ou vérification périodique.
            if (scan_pending and now - last_event >= 0.15) or now - last_scan >= 2.0:
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
                "Multiple disks detected on the reserved USB porté"
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
# 2. VBUS MEASUREMENTS
# ============================================================

class VbusWorker(QThread):
    """Acquire timestamped VBUS samples continuously."""

    ready = pyqtSignal()
    sample_ready = pyqtSignal(object)
    error = pyqtSignal(str)

    def run(self):
        """Connect, sample periodically and disconnect."""

        adapter = USBExt3cAdapter()

        try:
            adapter.connect()
            self.ready.emit()

            while not self.isInterruptionRequested():
                start_ns = time.monotonic_ns()

                voltage_v, current_ma = adapter.read_vbus()

                end_ns = time.monotonic_ns()

                sample = VbusSample(
                    timestamp_ns=(start_ns + end_ns) // 2,
                    voltage_v=voltage_v,
                    current_ma=current_ma,
                )

                self.sample_ready.emit(sample)

                self.msleep(
                    max(1, int(VBUS_SAMPLE_INTERVAL_S * 1000))
                )

        except Exception as exc:
            self.error.emit(str(exc))

        finally:
            adapter.disconnect()

    def stop(self):
        """Request clean monitoring termination."""

        self.requestInterruption()

# ============================================================
# 3. EXÉCUTION DES TESTS USB
# ============================================================

class TestWorker(QThread):
    """Run UsbTestService in a Qt worker thread."""

    status = pyqtSignal(str)
    info_ready = pyqtSignal(str)
    sample = pyqtSignal(str, float, float)
    progress = pyqtSignal(int)
    phase_changed = pyqtSignal(object)
    test_done = pyqtSignal(dict)

    def __init__(self, devnode, expected_diskseq):
        super().__init__()

        self.devnode = devnode
        self.expected_diskseq = expected_diskseq

        self.cancel_requested = False
        self.abort_reason = "Test interrupted"

    def run(self):
        service = UsbTestService(
            devnode=self.devnode,
            expected_diskseq=self.expected_diskseq,
            emit_status=self.status.emit,
            emit_device_info=self.info_ready.emit,
            emit_sample=self.sample.emit,
            emit_progress=self.progress.emit,
            emit_phase=self.phase_changed.emit,
            interruption_requested=self.isInterruptionRequested,
            cancellation_requested=lambda: self.cancel_requested,
            abort_reason=lambda: self.abort_reason,
        )

        result = service.run()
        self.test_done.emit(result)

    def abort(self, reason="Test interrupted"):
        self.abort_reason = reason
        self.requestInterruption()

    def cancel(self):
        self.cancel_requested = True
        self.requestInterruption()

    def _emit_phase(self, phase):
        """Emit an accurately timestamped test-phase transition."""

        self.phase_changed.emit(
            PhaseEvent(
                timestamp_ns=time.monotonic_ns(),
                phase=phase,
            )
        )

 