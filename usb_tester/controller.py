"""USB monitoring and test execution using independent Qt threads."""

# Standard library
import time
from datetime import datetime
from enum import Enum, auto
from threading import Event

# Third-party libraries
import pyudev
from PyQt5.QtCore import QObject, QThread, QTimer, pyqtSignal

# Application configuration
from config import (
    BLOCK_SIZE_MB,
    MAX_MATING_CYCLES,
    REFORMAT_FS,
    RESULTS_DIR,
    SOFTWARE_VERSION,
    STATION_NAME,
    TEMPLATE_PATH,
    TEST_CYCLES,
    TEST_DURATION,
    TEST_DURATION_MODE,
    TEST_FILE_SIZE_MB,
    VBUS_SAMPLE_INTERVAL_S,
)

# Application modules
from acroname import USBExt3cAdapter
from model import (
    TestPhase,
    VbusSample,
    assert_test_port,
    build_device_info_text,
    build_report,
    compute_vbus_phase_stats,
    get_device_info,
    get_diskseq,
    increment_test_link_counter,
    load_test_link_counters,
    scan_usb_candidates,
    write_docx_report,
)
from test_service import Usb2FallbackTestService, UsbPerformanceTestService


# ============================================================
# 0. APPLICATION STATE
# ============================================================

class AppState(Enum):
    """Represent the main states of the application."""
    WAITING_INSERT = auto()
    READY = auto()
    TESTING = auto()
    USB2_TESTING = auto()
    WAITING_RESTART = auto()


# ============================================================
# 1. APPLICATION CONTROLLER
# ============================================================

class AppController(QObject):
    """Coordinate the USB tester application."""

    # --------------------------------------------------------
    # INITIALIZATION
    # --------------------------------------------------------

    def __init__(self, view, test_config, parent=None):
        """Initialize the application controller."""

        # Initialize the Qt base class.
        super().__init__(parent)

        # Store the View and the current test configuration.
        self.view = view
        self.test_config = test_config

        # Initialize the application state.
        self.state = AppState.WAITING_INSERT
        self.closing = False

        # Track the DUT selected for the current qualification.
        self.current_devnode = None
        self.current_diskseq = None

        # Track the device currently detected on the test port.
        self.connected_devnode = None
        self.connected_diskseq = None

        # Track DUT removal and reinsertion between tests.
        self.device_removed = False
        self.new_device_ready = False

        # Initialize worker references.
        self.qualification_worker = None
        self.vbus_worker = None
        self.watcher = None

        # Store VBUS measurements and current test phase.
        self.vbus_samples = []
        self.current_phase = TestPhase.OTHER
        self.vbus_error = ""

        # Connect View signals to Controller actions.
        self.view.start_requested.connect(self.start_test)
        self.view.cancel_requested.connect(self.cancel_test)
        self.view.new_test_requested.connect(self.prepare_next_test)
        self.view.close_requested.connect(self.shutdown)


    def initialize(self):
        """Initialize the application and start USB monitoring."""

        # Initialize the View.
        self.view.show_waiting_insert()
        self._refresh_test_link_display()

        # Restore the Acroname DUT port to normal USB operation.
        self._initialize_acroname()

        # Initialize USB device monitoring.
        self.watcher = UsbWatcher()

        # Connect USB watcher events to Controller actions.
        self.watcher.usb_event.connect(self.on_usb_event)
        self.watcher.finished.connect(self._try_finish_shutdown)

        # Start USB monitoring.
        self.watcher.start()


    def _initialize_acroname(self):
        """Restore the Acroname DUT port to normal USB operation."""

        adapter = USBExt3cAdapter()

        try:
            adapter.connect()
            adapter.restore_normal_port()

        except Exception as exc:
            self.view.set_step_status(f"Acroname initialization failed: {exc}")

        finally:
            adapter.disconnect()


    def _get_test_link_info(self):
        """Return the selected test-link information."""

        reference = self.test_config["test_link"]
        link_data = load_test_link_counters().get(reference, {})

        return {
            "reference": reference,
            "cycles": link_data.get("cycles", 0),
            "max_cycles": link_data.get("max_cycles", MAX_MATING_CYCLES),
        }


    def _refresh_test_link_display(self):
        """Update the View with the selected test-link information."""

        test_link = self._get_test_link_info()

        self.view.set_test_link_info(
            reference=test_link["reference"],
            cycles=test_link["cycles"],
            max_cycles=test_link["max_cycles"],
        )


    # --------------------------------------------------------
    # USB DEVICE MANAGEMENT
    # --------------------------------------------------------

    def on_usb_event(self, devnode, event):
        """Handle insertion and removal events on the dedicated USB test port."""

        # --------------------------------------------------------
        # DEVICE INSERTION
        # --------------------------------------------------------

        if event == "inserted":

            # Validate the detected device and retrieve its identity.
            try:
                assert_test_port(devnode)
                diskseq = get_diskseq(devnode)

            except (OSError, RuntimeError) as exc:
                self.view.set_step_status(f"Device unavailable: {exc}")
                return

            # Determine whether this is a newly detected device instance.
            is_new_device = (
                self.connected_devnode,
                self.connected_diskseq,
            ) != (
                devnode,
                diskseq,
            )

            # Store the device currently detected on the test port.
            self.connected_devnode = devnode
            self.connected_diskseq = diskseq

            # Count every new physical mating event.
            # Re-enumerations caused by the USB2 fallback are excluded.
            if (is_new_device and self.state != AppState.USB2_TESTING):
                try:
                    increment_test_link_counter(self.test_config["test_link"])
                    self._refresh_test_link_display()

                except (OSError, ValueError, TypeError) as exc:
                    self.view.set_step_status(f"Counter error: {exc}")

            # Make a newly inserted DUT ready for testing.
            if self.state == AppState.WAITING_INSERT:
                self._set_ready(devnode, diskseq)

            # After a completed test, remember that a new DUT
            # is available for the next qualification.
            elif (
                self.state == AppState.WAITING_RESTART
                and self.device_removed
            ):
                self.new_device_ready = True

        # --------------------------------------------------------
        # DEVICE REMOVAL
        # --------------------------------------------------------

        elif event == "removed":

            # Ignore removal events that do not concern the
            # device currently tracked by the Controller.
            if devnode != self.connected_devnode:
                return

            # Clear the currently connected device.
            self.connected_devnode = None
            self.connected_diskseq = None

            # Abort the performance test if the DUT is unplugged.
            if self.state == AppState.TESTING:
                self.view.set_step_status("Device disconnected")

                if self.qualification_worker is not None:
                    self.qualification_worker.abort("Device disconnected")

            # During USB2 fallback, disconnection is expected because
            # the Acroname module intentionally power-cycles the DUT.
            elif self.state == AppState.USB2_TESTING:
                return

            # If the DUT is removed before the test starts,
            # return to the waiting state.
            elif self.state == AppState.READY:
                self._set_waiting_insert()

            # After a completed test, record the physical removal.
            elif self.state == AppState.WAITING_RESTART:
                self.device_removed = True
                self.new_device_ready = False


    def _set_waiting_insert(self):
        """Return to the initial waiting state."""

        self.state = AppState.WAITING_INSERT
        self.current_devnode = None
        self.current_diskseq = None
        self.view.show_waiting_insert()


    def _set_ready(self, devnode, diskseq):
        """Validate a detected DUT and place the application in READY state."""

        # Validate the detected DUT and retrieve its information.
        try:
            assert_test_port(devnode)
            info = get_device_info(devnode)

        except (OSError, RuntimeError) as exc:
            self._set_waiting_insert()
            self.view.set_step_status(f"USB detection failed: {exc}")
            return

        # Store the DUT selected for the next qualification.
        self.current_devnode = devnode
        self.current_diskseq = diskseq
        self.state = AppState.READY

        # Update the View with the validated DUT information.
        self.view.show_ready(
            device_label=info["DeviceLabel"],
            device_info_text=build_device_info_text(info),
        )


    # --------------------------------------------------------
    # TEST CONTROL
    # --------------------------------------------------------

    def start_test(self):
        """Start the qualification sequence."""

        if self.state != AppState.READY:
            return

        # Initialize the new qualification state.
        self.state = AppState.TESTING
        self.device_removed = False
        self.new_device_ready = False

        self.view.show_testing()
        self._start_workers()


    def cancel_test(self):
        """Request cancellation of the running qualification."""

        if self.state != AppState.TESTING:
            return

        self.view.show_cancelling()

        if self.qualification_worker is not None:
            self.qualification_worker.cancel()


    def prepare_next_test(self):
        """Prepare the application for the next qualification."""

        if self.state != AppState.WAITING_RESTART:
            return

        ready_for_next_test = (
            self.new_device_ready
            and self.connected_devnode is not None
        )

        devnode = self.connected_devnode
        diskseq = self.connected_diskseq

        # Reset the previous qualification state.
        self.new_device_ready = False
        self.device_removed = False
        self._set_waiting_insert()

        # Immediately prepare a DUT that was already reinserted.
        if ready_for_next_test:
            self._set_ready(devnode, diskseq)


    # --------------------------------------------------------
    # QUALIFICATION WORKERS
    # --------------------------------------------------------

    def _start_workers(self):
        """Initialize qualification and VBUS workers."""

        # Reset data and state for the new qualification.
        self.vbus_samples = []
        self.vbus_error = ""
        self.current_phase = TestPhase.OTHER

        # Initialize the USB qualification worker.
        self.qualification_worker = UsbQualificationWorker(
            self.current_devnode,
            self.current_diskseq,
        )

        self.qualification_worker.status.connect(self.view.set_step_status)
        self.qualification_worker.info_ready.connect(self.view.set_result_text)
        self.qualification_worker.sample.connect(self.view.add_speed_sample)
        self.qualification_worker.progress.connect(self.view.set_progress)
        self.qualification_worker.phase_changed.connect(self._on_phase_changed)
        self.qualification_worker.performance_done.connect(self._on_performance_done)
        self.qualification_worker.qualification_done.connect(self._finalize_test)
        self.qualification_worker.finished.connect(self._on_qualification_worker_finished)

        # Initialize the VBUS monitoring worker.
        self.vbus_worker = VbusWorker()

        self.vbus_worker.initialized.connect(self._on_vbus_initialized)
        self.vbus_worker.sample_ready.connect(self._on_vbus_sample)
        self.vbus_worker.finished.connect(self._on_vbus_worker_finished)
        self.vbus_worker.error.connect(self._on_vbus_error)

        # Start VBUS monitoring first.
        self.vbus_worker.start()


    def _on_vbus_initialized(self, success, message):
        """Handle VBUS initialization and start the qualification."""

        if not success:
            self.vbus_error = message
            self.view.show_vbus_error(message)

        self.qualification_worker.start()


    def _on_phase_changed(self, phase):
        """Update the current test phase."""

        self.current_phase = phase


    def _on_vbus_sample(self, sample):
        """Store and display one VBUS measurement."""

        # Associate the sample with the current test phase.
        sample.phase = self.current_phase

        # Store and display the measurement.
        self.vbus_samples.append(sample)
        self.view.show_vbus_sample(sample.phase, sample)


    def _on_vbus_error(self, message):
        """Record a VBUS monitoring failure."""

        self.vbus_error = message
        self.view.show_vbus_error(message)


    def _on_performance_done(self, run_usb2):
        """Stop VBUS monitoring after the performance stage."""

        if run_usb2:
            self.state = AppState.USB2_TESTING
            self.view.set_progress(97)
            self.view.set_cancel_enabled(False)

        if self.vbus_worker is not None and self.vbus_worker.isRunning():
            self.vbus_worker.stop()
        elif self.qualification_worker is not None:
            self.qualification_worker.notify_vbus_stopped()


    def _on_vbus_worker_finished(self):
        """Release VBUS monitoring and continue the qualification."""

        worker = self.sender()

        if worker is not None:
            worker.deleteLater()

        if self.vbus_worker is worker:
            self.vbus_worker = None

        # Allow the qualification worker to continue with USB2.
        if self.qualification_worker is not None:
            self.qualification_worker.notify_vbus_stopped()

        self._try_finish_shutdown()
        

    def _on_qualification_worker_finished(self):
        """Release the finished qualification worker."""

        worker = self.sender()

        if worker is not None:
            worker.deleteLater()

        if self.qualification_worker is worker:
            self.qualification_worker = None

        self._try_finish_shutdown()


    # --------------------------------------------------------
    # TEST FINALIZATION
    # --------------------------------------------------------

    def _finalize_test(self, result):
        """Finalize the qualification and prepare the final report."""

        # Handle cancellation results separately.
        if result["result"] == "CANCELLED":
            self._set_waiting_insert()
            return

        if result["result"] == "CANCEL_FAILED":
            self.state = AppState.WAITING_RESTART
            self.view.show_cancel_failed(result.get("error", "Unknown error"))
            return

        # Add final qualification data.
        result["vbus_stats"] = compute_vbus_phase_stats(self.vbus_samples)
        result["vbus_error"] = self.vbus_error
        result["test_link"] = self._get_test_link_info()

        # Build the final report content and graph.
        result["report"] = build_report(result)

        self.view.prepare_final_graph(result)

        # Defer report generation so the progress update is displayed first.
        self.view.set_progress(99)
        self.view.set_step_status("Generating report...")
        QTimer.singleShot(100, lambda: self._save_report(result))


    def _save_report(self, result):
        """Save the final report and update the View."""

        # Use the prepared report path or create a fallback path.
        path = result.get("docx") or str(
            RESULTS_DIR / f"USB_report_{datetime.now():%Y%m%d_%H%M%S}.docx"
        )

        # Generate the DOCX report.
        try:
            write_docx_report(
                template_path=TEMPLATE_PATH,
                output_path=path,
                report_text=result.get("report", ""),
                figure=self.view.get_report_figure(),
                of_number=self.test_config["of"],
                sap=self.test_config["sap"],
                device=result.get("device_info", {}).get("Model", "N/A"),
                serial=result.get("device_info", {}).get("Serial", "N/A"),
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
            report_saved = False
            self.view.set_step_status(f"Report failed: {exc}")

        else:
            report_saved = True
            self.view.set_step_status(f"Report saved: {path}")

        # Complete the qualification workflow.
        self.state = AppState.WAITING_RESTART
        self.view.show_final_result(
            result=result,
            report_saved=report_saved,
        )


    # --------------------------------------------------------
    # APPLICATION SHUTDOWN
    # --------------------------------------------------------

    def shutdown(self):
        """Request all active workers to stop safely."""

        if self.closing:
            return

        self.closing = True

        # Stop all active background workers.
        if self.qualification_worker is not None and self.qualification_worker.isRunning():
            self.qualification_worker.abort("Application closing")

        if self.vbus_worker is not None and self.vbus_worker.isRunning():
            self.vbus_worker.stop()

        if self.watcher is not None and self.watcher.isRunning():
            self.watcher.stop()

        # Close the application once all workers have stopped.
        self._try_finish_shutdown()


    def _try_finish_shutdown(self):
        """Close the View once all workers have stopped."""

        if not self.closing:
            return

        active_threads = (
            self.qualification_worker,
            self.vbus_worker,
            self.watcher,
        )

        if any(thread is not None and thread.isRunning() for thread in active_threads):
            return

        self.view.allow_close()


# ============================================================
# 2. USB DEVICE MONITORING
# ============================================================

class UsbWatcher(QThread):
    """Monitor device changes on the dedicated USB test port."""

    usb_event = pyqtSignal(str, str)  # devnode, event

    # --------------------------------------------------------
    # INITIALIZATION
    # --------------------------------------------------------

    def __init__(self):
        super().__init__()

        self._current_devnode = None
        self._current_diskseq = None


    # --------------------------------------------------------
    # USB MONITORING
    # --------------------------------------------------------

    def run(self):
        """Monitor USB events and detect devices on the test port."""

        # Start block-device monitoring.
        context = pyudev.Context()
        monitor = pyudev.Monitor.from_netlink(context)
        monitor.filter_by(subsystem="block")
        monitor.start()

        # Detect a device already connected at startup.
        self._update_device()

        scan_pending = False
        last_event = 0.0
        last_scan = time.monotonic()

        while not self.isInterruptionRequested():
            device = monitor.poll(timeout=0.1)

            if device is not None and device.action in ("add", "remove", "change"):

                # Immediately report removal of the monitored device.
                if (
                    device.action == "remove"
                    and device.device_type == "disk"
                    and device.device_node == self._current_devnode
                ):
                    self.usb_event.emit(self._current_devnode, "removed")
                    self._current_devnode = None
                    self._current_diskseq = None

                # Immediately check newly added disks.
                if device.action == "add" and device.device_type == "disk":
                    self._update_device()

                # Recheck after udev events have stabilized.
                scan_pending = True
                last_event = time.monotonic()

            now = time.monotonic()

            # Recheck after an event or periodically as a fallback.
            if (scan_pending and now - last_event >= 0.15) or now - last_scan >= 2.0:
                self._update_device()
                scan_pending = False
                last_scan = now


    def stop(self):
        """Request the monitoring thread to stop."""

        self.requestInterruption()


    # --------------------------------------------------------
    # DEVICE DETECTION
    # --------------------------------------------------------

    def _scan_device(self):
        """Return the authorized device and its identity."""

        candidates = scan_usb_candidates()

        if len(candidates) > 1:
            raise RuntimeError("Multiple disks detected on the reserved USB port")

        if not candidates:
            return None, None

        devnode = candidates[0]

        try:
            diskseq = get_diskseq(devnode)
        except (OSError, RuntimeError):
            return None, None

        return devnode, diskseq


    def _update_device(self):
        """Detect device changes and emit insertion or removal events."""

        try:
            devnode, diskseq = self._scan_device()
        except RuntimeError:
            devnode, diskseq = None, None

        if (devnode, diskseq) == (self._current_devnode, self._current_diskseq):
            return

        # Report disappearance of the previously monitored device.
        if self._current_devnode is not None:
            self.usb_event.emit(self._current_devnode, "removed")

        self._current_devnode = devnode
        self._current_diskseq = diskseq

        # Report detection of the new device.
        if devnode is not None:
            self.usb_event.emit(devnode, "inserted")


# ============================================================
# 3. VBUS MONITORING
# ============================================================

class VbusWorker(QThread):
    """Acquire VBUS samples continuously."""


    initialized = pyqtSignal(bool, str)
    sample_ready = pyqtSignal(object)
    error = pyqtSignal(str)


    # --------------------------------------------------------
    # VBUS MONITORING
    # --------------------------------------------------------

    def run(self):
        """Connect to the Acroname module and acquire VBUS samples."""

        adapter = USBExt3cAdapter()

        # Connect to the Acroname module.
        try:
            adapter.connect()

        except Exception as exc:
            self.initialized.emit(False, str(exc))
            return

        self.initialized.emit(True, "")

        # Acquire VBUS measurements until stopped.
        try:
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
                self.msleep(max(1, int(VBUS_SAMPLE_INTERVAL_S * 1000)))

        except Exception as exc:
            self.error.emit(str(exc))

        finally:
            adapter.disconnect()


    def stop(self):
        """Request the monitoring thread to stop."""

        self.requestInterruption()


# ============================================================
# 4. USB QUALIFICATION
# ============================================================

class UsbQualificationWorker(QThread):
    """Run the complete USB qualification sequence."""

    status = pyqtSignal(str)
    info_ready = pyqtSignal(str)
    sample = pyqtSignal(str, float, float)
    progress = pyqtSignal(int)
    phase_changed = pyqtSignal(object)
    performance_done = pyqtSignal(bool)
    qualification_done = pyqtSignal(dict)


    # --------------------------------------------------------
    # INITIALIZATION
    # --------------------------------------------------------

    def __init__(self, devnode, expected_diskseq):
        super().__init__()

        self.devnode = devnode
        self.expected_diskseq = expected_diskseq

        self.cancel_requested = False
        self.abort_reason = "Test interrupted"
        self.vbus_stopped = Event()


    # --------------------------------------------------------
    # QUALIFICATION SEQUENCE
    # --------------------------------------------------------

    def run(self):
        """Execute the complete USB qualification sequence."""

        # Run performance, integrity and reformatting.
        performance_service = UsbPerformanceTestService(
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

        result = performance_service.run()

        # Continue with USB2 if the first stage completed normally.
        run_usb2 = (
            result["result"] not in ("CANCELLED", "CANCEL_FAILED")
            and result.get("completed", False)
            and not self.isInterruptionRequested()
        )

        # Stop VBUS monitoring before accessing the Acroname for USB2.
        self.performance_done.emit(run_usb2)
        self.vbus_stopped.wait()

        # Run USB2 fallback.
        if run_usb2 and not self.isInterruptionRequested():
            usb2_service = Usb2FallbackTestService(
                emit_status=self.status.emit,
                interruption_requested=self.isInterruptionRequested,
            )

            usb2_result = usb2_service.run()
            result["usb2_test"] = usb2_result

            if usb2_result["result"] != "PASS":
                result["result"] = "FAIL"

            self.progress.emit(98)

        self.qualification_done.emit(result)


    # --------------------------------------------------------
    # VBUS SYNCHRONIZATION
    # --------------------------------------------------------

    def notify_vbus_stopped(self):
        """Allow qualification to continue after VBUS monitoring stops."""

        self.vbus_stopped.set()


    # --------------------------------------------------------
    # QUALIFICATION CONTROL
    # --------------------------------------------------------

    def abort(self, reason="Test interrupted"):
        """Abort the running qualification."""

        self.abort_reason = reason
        self.requestInterruption()


    def cancel(self):
        """Request operator cancellation."""

        self.cancel_requested = True
        self.requestInterruption()