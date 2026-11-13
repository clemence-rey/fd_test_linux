"""USB qualification test services."""

# Standard library
import csv
import hashlib
import os
import time
from contextlib import ExitStack
from pathlib import Path

# Application configuration
from config import (
    BLOCK_SIZE,
    FILE_SIZE,
    NUMBER_BLOCKS,
    REFORMAT_FS,
    REFORMAT_LABEL,
    RESULTS_DIR,
    TEST_CYCLES,
    TEST_DURATION,
    TEST_DURATION_MODE,
    USB2_DISCONNECT_TIMEOUT_S,
    USB2_ENUM_TIMEOUT_S,
    USB2_POLL_INTERVAL_S,
    USB2_RESTORE_TIMEOUT_S,
    VBUS_IDLE_DURATION_S,
)

# Application modules
from acroname import USBExt3cAdapter
from model import (
    TestPhase,
    assert_expected_test_device,
    build_device_info_text,
    compute_mbps,
    create_aligned_buffer,
    create_result_paths,
    create_test_result,
    evaluate_performance_test,
    get_device_info,
    open_test_device,
    reformat_device,
    set_result_error,
    format_error,
    unmount_device,
    wait_for_test_device,
    wait_for_test_port_empty,
)


# ============================================================
# 1. USB PERFORMANCE TEST
# ============================================================

class UsbPerformanceTestService:
    """Execute USB performance, integrity and reformatting tests."""

    # --------------------------------------------------------
    # INITIALIZATION
    # --------------------------------------------------------

    def __init__(
        self,
        devnode,
        expected_diskseq,
        status_callback,
        device_info_callback,
        sample_callback,
        progress_callback,
        phase_callback,
        is_interrupted,
        is_cancelled,
        get_abort_reason,
    ):
        """Initialize the test service and its callbacks."""

        self.devnode = devnode
        self.expected_diskseq = expected_diskseq

        self.status_callback = status_callback
        self.device_info_callback = device_info_callback
        self.sample_callback = sample_callback
        self.progress_callback = progress_callback
        self.phase_callback = phase_callback

        self.is_interrupted = is_interrupted
        self.is_cancelled = is_cancelled
        self.get_abort_reason = get_abort_reason


    # --------------------------------------------------------
    # TEST SEQUENCE
    # --------------------------------------------------------

    def run(self):
        """Execute the USB performance, integrity and reformatting sequence."""

        self.current_result = create_test_result(self.devnode)

        try:
            return self._execute_test_sequence()

        except Exception as exc:
            set_result_error(self.current_result, exc, "USB test")
            self.current_result["reformat"] = "NOT PERFORMED (test error)"

            return self.current_result

        finally:
            self.phase_callback(TestPhase.OTHER)


    def _execute_test_sequence(self):
        """
        Execute the USB performance test workflow.

        Sequence:
            1. Identify and prepare the device.
            2. Prepare test data and measurement files.
            3. Execute write and read cycles.
            4. Compute performance statistics and integrity verdict.
            5. Reformat the device if the test was not interrupted.

        I/O operations are performed directly on the block device using O_DIRECT.
        """

        devnode = self.devnode
        result = self.current_result

        self.phase_callback(TestPhase.OTHER)

        # DEVICE IDENTIFICATION AND PREPARATION

        # Verify that the selected DUT has not been replaced.
        assert_expected_test_device(devnode, self.expected_diskseq)

        RESULTS_DIR.mkdir(parents=True, exist_ok=True)

        # Retrieve and store device information.
        self.status_callback("Reading device information...")

        info = get_device_info(devnode)
        result["device_info"] = info.copy()
        result.update(create_result_paths(info["Serial"]))

        self.device_info_callback(build_device_info_text(info))

        # Unmount partitions before direct raw access.
        self.status_callback("Unmounting / raw access to the device...")
        unmount_device(devnode)

        # Verify that the device capacity is sufficient for the test.
        device_size = info["Size_Bytes"]

        if device_size is None:
            raise RuntimeError("Device capacity unavailable")

        if device_size < FILE_SIZE * 1.1:
            raise RuntimeError("The device's storage is too small for this test")

        # TEST DATA AND MEASUREMENT PREPARATION

        # Generate one reference block used for all write operations.
        # Its SHA-256 hash is used to verify the read-back data.
        reference_bytes = os.urandom(BLOCK_SIZE)
        reference_hash = hashlib.sha256(reference_bytes).digest()

        # Initialize test measurements.
        integrity_errors = 0
        total_written = total_read = 0
        write_io_time = read_io_time = 0.0
        cycle_write_measurements = []
        cycle_read_measurements = []

        cycle = 0
        self._cycle = 0
        self._last_pct = -1
        io_failed = False

        # TEST RESOURCE INITIALIZATION

        # Resources are automatically released when leaving this block.
        with ExitStack() as stack:

            # Memory-aligned buffers required by O_DIRECT.
            write_buffer = stack.enter_context(
                create_aligned_buffer(BLOCK_SIZE, fill=reference_bytes)
            )

            read_buffer = stack.enter_context(
                create_aligned_buffer(BLOCK_SIZE)
            )

            # CSV file containing measurements for each block.
            csv_handle = stack.enter_context(
                open(result["csv"], "w", newline="", encoding="utf-8")
            )

            writer = csv.writer(csv_handle)
            writer.writerow([
                "Cycle",
                "Operation",
                "Block number",
                "Block size",
                "Duration_s",
                "MB/s",
                "Integrity",
            ])

            # Open the disk exclusively using O_DIRECT.
            fd = open_test_device(devnode, self.expected_diskseq)
            stack.callback(os.close, fd)

            # IDLE VBUS MEASUREMENT

            self.status_callback("Measuring idle VBUS...")
            self.phase_callback(TestPhase.IDLE)

            idle_step_duration = VBUS_IDLE_DURATION_S / 5

            for progress in range(1, 6):
                if self.is_interrupted():
                    break

                time.sleep(idle_step_duration)
                self.progress_callback(progress)

            self.phase_callback(TestPhase.OTHER)

            # Start performance timing after the idle phase.
            self._global_start = time.perf_counter()

            # WRITE AND READ CYCLES

            while (
                not self._is_limit_reached()
                and not self.is_interrupted()
                and not io_failed
            ):
                cycle += 1
                self._cycle = cycle

                # Visually separate cycles on the graph.
                if cycle > 1:
                    self.sample_callback("SEP", float("nan"), float("nan"))

                label_cycle = (
                    f"{cycle}/{TEST_CYCLES}"
                    if TEST_DURATION_MODE == "cycles"
                    else str(cycle)
                )

                # WRITE

                self.status_callback(f"Cycle {label_cycle} - WRITE")
                self.phase_callback(TestPhase.WRITE)

                try:
                    os.lseek(fd, 0, os.SEEK_SET)

                    block_measurements = []
                    cycle_write_measurements.append(block_measurements)

                    written, write_time = self._write_cycle(
                        fd,
                        write_buffer,
                        cycle,
                        writer,
                        block_measurements,
                    )

                    total_written += written
                    write_io_time += write_time

                except Exception as exc:
                    io_failed = True
                    set_result_error(result, exc, "Write")

                finally:
                    self.phase_callback(TestPhase.OTHER)

                # Do not continue after an error or interruption.
                if io_failed or self.is_interrupted():
                    break

                # Do not start reading if duration mode already reached its limit.
                if TEST_DURATION_MODE == "duration" and self._is_limit_reached():
                    break

                # READ AND INTEGRITY VERIFICATION

                self.status_callback(f"Cycle {label_cycle} - READ")
                self.phase_callback(TestPhase.READ)

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
                        block_measurements,
                    )

                    total_read += read_bytes
                    read_io_time += read_time
                    integrity_errors += read_errors

                except Exception as exc:
                    io_failed = True
                    set_result_error(result, exc, "Read")

                finally:
                    self.phase_callback(TestPhase.OTHER)

        # USER CANCELLATION

        # The CSV file and raw device are closed at this point.
        if self.is_cancelled():
            return self._cancel_test(result)

        # STATISTICS AND VERDICT

        result.update(
            evaluate_performance_test(
                total_written=total_written,
                total_read=total_read,
                write_io_time=write_io_time,
                read_io_time=read_io_time,
                cycle_write_measurements=cycle_write_measurements,
                cycle_read_measurements=cycle_read_measurements,
                integrity_errors=integrity_errors,
                interrupted=self.is_interrupted(),
                io_failed=io_failed,
            )
        )

        # INTERRUPTION AND I/O ERROR HANDLING

        if self.is_interrupted() or io_failed:
            result["result"] = "FAIL"
            result["completed"] = False

            if self.is_interrupted() and not result["error"]:
                result["error"] = self.get_abort_reason()

            result["reformat"] = "NOT PERFORMED (test interrupted or failed)"
            return result

        # DEVICE REFORMATTING

        # Reformat even if performance thresholds were not met.
        self._run_reformat_step(result)

        return result


    # --------------------------------------------------------
    # WRITE AND READ OPERATIONS
    # --------------------------------------------------------

    def _write_cycle(
        self,
        fd,
        buffer,
        cycle,
        writer,
        block_measurements,
    ):
        """
        Sequentially write blocks to the device using O_DIRECT.

        Return the number of bytes written and cumulative I/O time.
        """

        bytes_done = 0
        io_time = 0.0

        # Sequential block writes.
        for block_number in range(NUMBER_BLOCKS):

            if self.is_interrupted():
                break

            # Measure raw write duration.
            start_ns = time.perf_counter_ns()
            written = os.writev(fd, [buffer])
            duration = (time.perf_counter_ns() - start_ns) / 1e9

            if written != BLOCK_SIZE:
                raise OSError(
                    f"Partial write at block {block_number} "
                    f"({written}/{BLOCK_SIZE} bytes)"
                )

            # Store performance measurement.
            bytes_done += written
            io_time += duration

            mbps = compute_mbps(written, duration)
            block_measurements.append((written, duration))

            elapsed = self._get_elapsed()

            writer.writerow([
                cycle,
                "WRITE",
                block_number,
                BLOCK_SIZE,
                duration,
                mbps,
                1,
            ])

            self.sample_callback("WRITE", elapsed, mbps)
            self._update_progress("write", block_number)

            # Stop when the configured duration has been reached.
            if TEST_DURATION_MODE == "duration" and elapsed >= TEST_DURATION:
                break

        # Flush writes outside performance timing.
        if not self.is_interrupted():
            os.fsync(fd)

        return bytes_done, io_time


    def _read_cycle(
        self,
        fd,
        buffer,
        reference_hash,
        cycle,
        writer,
        block_measurements,
    ):
        """
        Sequentially read blocks and verify data integrity.

        Return the number of bytes read, cumulative I/O time and integrity errors.
        """

        bytes_done = 0
        io_time = 0.0
        integrity_errors = 0

        # Sequential block reads.
        for block_number in range(NUMBER_BLOCKS):

            if self.is_interrupted():
                break

            # Measure raw read duration.
            start_ns = time.perf_counter_ns()
            read_bytes = os.readv(fd, [buffer])
            duration = (time.perf_counter_ns() - start_ns) / 1e9

            if read_bytes == 0:
                raise OSError(f"Empty read at block {block_number}")

            # Store performance measurement.
            bytes_done += read_bytes
            io_time += duration

            mbps = compute_mbps(read_bytes, duration)
            block_measurements.append((read_bytes, duration))

            # Verify block integrity outside I/O timing.
            received_hash = hashlib.sha256(buffer[:read_bytes]).digest()

            integrity = int(
                read_bytes == BLOCK_SIZE
                and received_hash == reference_hash
            )

            if not integrity:
                integrity_errors += 1

            elapsed = self._get_elapsed()

            writer.writerow([
                cycle,
                "READ",
                block_number,
                BLOCK_SIZE,
                duration,
                mbps,
                integrity,
            ])

            self.sample_callback("READ", elapsed, mbps)
            self._update_progress("read", block_number)

            # Stop when the configured duration has been reached.
            if TEST_DURATION_MODE == "duration" and elapsed >= TEST_DURATION:
                break

        return bytes_done, io_time, integrity_errors


    # --------------------------------------------------------
    # TEST FINALIZATION
    # --------------------------------------------------------

    def _run_reformat_step(self, result):
        """Reformat the DUT and update the test result."""

        # Prevent reformatting if an interruption occurs at the last moment.
        if self.is_interrupted():
            result["result"] = "FAIL"
            result["completed"] = False
            result["error"] = self.get_abort_reason()
            result["reformat"] = "NOT PERFORMED (test interrupted)"
            return

        self.progress_callback(96)
        self.status_callback(f"Reformatting to {REFORMAT_FS.upper()}...")

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

        except Exception as exc:
            set_result_error(result, exc, "Reformatting")
            result["reformat"] = f"FAILURE: {result['error']}"


    def _cancel_test(self, result):
        """Clean test files and restore the DUT after cancellation."""

        self.status_callback("Cancellation: Cleaning up files...")

        # Delete files created by the cancelled test.
        for key in ("docx", "csv"):
            path = result.get(key)

            if not path:
                continue

            try:
                Path(path).unlink(missing_ok=True)

            except OSError as exc:
                self.status_callback(f"Cannot delete {path}: {exc}")

        # Restore the DUT to its final formatted state.
        self.status_callback(
            f"Cancellation: Reformatting to {REFORMAT_FS.upper()}..."
        )

        try:
            reformat_device(
                self.devnode,
                REFORMAT_FS,
                REFORMAT_LABEL,
                self.expected_diskseq,
            )

        except Exception as exc:
            result["result"] = "CANCEL_FAILED"
            result["completed"] = False
            result["error"] = format_error(exc, "Cancellation reformatting")
            result["reformat"] = f"FAILURE: {result['error']}"
            return result

        result["result"] = "CANCELLED"
        result["completed"] = False
        result["reformat"] = "OK"
        result["report"] = ""

        return result


    # --------------------------------------------------------
    # INTERNAL HELPERS
    # --------------------------------------------------------

    def _get_elapsed(self):
        """Return elapsed test time in seconds."""

        return time.perf_counter() - self._global_start


    def _is_limit_reached(self):
        """Return whether the configured test limit has been reached."""

        if TEST_DURATION_MODE == "cycles":
            return self._cycle >= TEST_CYCLES

        return self._get_elapsed() >= TEST_DURATION


    def _update_progress(self, phase, block_number):
        """Update GUI progress during performance testing."""

        if TEST_DURATION_MODE == "cycles":
            block_progress = (block_number + 1) / NUMBER_BLOCKS

            if phase == "write":
                cycle_progress = 0.5 * block_progress
            else:
                cycle_progress = 0.5 + 0.5 * block_progress

            progress = (
                (self._cycle - 1) + cycle_progress
            ) / TEST_CYCLES

        else:
            progress = self._get_elapsed() / TEST_DURATION

        progress_percent = min(95, 5 + int(progress * 90))

        if progress_percent != self._last_pct:
            self._last_pct = progress_percent
            self.progress_callback(progress_percent)


# ============================================================
# 2. USB 2.0 FALLBACK TEST
# ============================================================

class Usb2FallbackTestService:
    """Verify USB 2.0 High-Speed fallback enumeration."""

    # --------------------------------------------------------
    # INITIALIZATION
    # --------------------------------------------------------

    def __init__(self, status_callback, is_interrupted):
        self.status_callback = status_callback
        self.is_interrupted = is_interrupted


    # --------------------------------------------------------
    # TEST EXECUTION
    # --------------------------------------------------------

    def run(self):
        """Test USB2 fallback and restore normal USB operation."""

        result = {
            "result": "FAIL",
            "speed_mbps": None,
            "error": "",
        }

        adapter = USBExt3cAdapter()
        connected = False

        try:
            # Connect to the Acroname module.
            self.status_callback(
                "USB 2.0 fallback - connecting to Acroname..."
            )

            adapter.connect()
            connected = True

            # Disconnect the DUT.
            self.status_callback(
                "USB 2.0 fallback - disconnecting DUT..."
            )

            adapter.power_off_dut()

            wait_for_test_port_empty(
                timeout_s=USB2_DISCONNECT_TIMEOUT_S,
                poll_interval_s=USB2_POLL_INTERVAL_S,
            )

            time.sleep(0.3)

            # Reconnect the DUT with USB2 only.
            self.status_callback(
                "USB 2.0 fallback - reconnecting at High-Speed..."
            )

            adapter.enable_usb2_only()

            _, speed_mbps = wait_for_test_device(
                timeout_s=USB2_ENUM_TIMEOUT_S,
                poll_interval_s=USB2_POLL_INTERVAL_S,
                expected_speed_mbps=480.0,
                is_interrupted=self.is_interrupted,
            )

            result["result"] = "PASS"
            result["speed_mbps"] = speed_mbps

        except Exception as exc:
            result["error"] = format_error(exc, "USB 2.0 fallback")

        finally:
            # Restore normal USB operation before releasing the Acroname.
            if connected:
                try:
                    self.status_callback("Restoring normal USB operation...")

                    adapter.power_off_dut()

                    wait_for_test_port_empty(
                        timeout_s=USB2_DISCONNECT_TIMEOUT_S,
                        poll_interval_s=USB2_POLL_INTERVAL_S,
                    )

                    time.sleep(0.3)

                    adapter.restore_normal_port()

                    wait_for_test_device(
                        timeout_s=USB2_RESTORE_TIMEOUT_S,
                        poll_interval_s=USB2_POLL_INTERVAL_S,
                    )

                except Exception as exc:
                    restore_error = format_error(exc, "USB restore")

                    if result["error"]:
                        result["error"] += f" | {restore_error}"
                    else:
                        result["error"] = restore_error

                    result["result"] = "FAIL"

                finally:
                    adapter.disconnect()

        return result   