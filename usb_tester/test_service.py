"""USB qualification test services."""

from contextlib import ExitStack
import hashlib
import os
import csv
import time
from pathlib import Path


from model import (
    assert_expected_test_device,
    get_device_info,
    create_test_result,
    create_result_paths,
    build_device_info_text,
    unmount_device,
    get_device_size_bytes,
    create_aligned_buffer,
    open_test_device,
    set_result_error,
    evaluate_performance_test,
    compute_mbps,
    reformat_device,
    wait_for_test_port_empty,
    wait_for_test_device,
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
    REFORMAT_LABEL,
    USB2_ENUM_TIMEOUT_S,
    USB2_DISCONNECT_TIMEOUT_S,
    USB2_RESTORE_TIMEOUT_S,
    USB2_POLL_INTERVAL_S,
)

from acroname import USBExt3cAdapter

from model import TestPhase


# ============================================================
# 1. USB PERFORMANCE TEST
# ============================================================

class UsbPerformanceTestService:
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
        """Execute the USB performance, integrity, and reformatting sequence."""

        self.current_result = create_test_result(self.devnode)
        self.device_info = {}

        try:
            return self._execute_test_sequence()

        except Exception as exc:
            result = self.current_result

            set_result_error(result, exc, "USB test")
            result["reformat"] = "NOT PERFORMED (test error)"
            result["device_info"] = self.device_info.copy()

            return result

        finally:
            self.emit_phase(TestPhase.OTHER)


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
        self.emit_phase(TestPhase.OTHER)

        # DEVICE IDENTIFICATION AND PREPARATION

        # Verify that the selected DUT has not been replaced.
        assert_expected_test_device(devnode, self.expected_diskseq)

        RESULTS_DIR.mkdir(parents=True, exist_ok=True)

        # Retrieve device information.
        self.emit_status("Reading device information...")
        info = get_device_info(devnode)
        self.device_info = info.copy()

        # Initialize the result structure and output paths.
        result = create_test_result(devnode)
        self.current_result = result
        result["model"] = info["Model"]
        result["serial"] = info["Serial"]
        result.update(create_result_paths(info["Serial"]))

        self.emit_device_info(build_device_info_text(info))

        # Unmount partitions before direct raw access.
        self.emit_status("Unmounting / raw access to the device...")
        unmount_device(devnode)

        # Verify that the device capacity is sufficient for the test.
        try:
            device_size = get_device_size_bytes(devnode)
        except Exception as e:
            raise RuntimeError(
                f"Device unavailable : {e}"
            ) from e

        if device_size < FILE_SIZE * 1.1:
            raise RuntimeError("The device's storage is too small for this test")

        # TEST DATA AND MEASUREMENT PREPARATION

        # Generate one reference block used for all write operations.
        # Its SHA-256 hash is used to verify the read-back data.
        reference_bytes = os.urandom(BLOCK_SIZE)
        reference_hash = hashlib.sha256(reference_bytes).digest()

        # Test counters and statistics.
        errors = 0
        total_written = total_read = 0
        write_io_time = read_io_time = 0.0
        cycle_write_measurements = []
        cycle_read_measurements = []
        
        cycle = 0

        self._cycle = 0
        self._last_pct = -1
        io_failed = False

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
                "Cycle", "Operation", "Block number", "Block size",
                "Duration_s", "MB/s", "Integrity"
            ])

            # Open the disk exclusively using O_DIRECT.
            # The opened device identity is also verified.
            fd = open_test_device(devnode, self.expected_diskseq)
            stack.callback(os.close, fd)

            self.emit_status("Measuring idle VBUS...")
            self.emit_phase(TestPhase.IDLE)

            idle_step_duration = VBUS_IDLE_DURATION_S / 5

            for progress in range(1, 6):
                if self.interruption_requested():
                    break

                time.sleep(idle_step_duration)
                self.emit_progress(progress)

            self.emit_phase(TestPhase.OTHER)

            # Start performance-test timing after the idle phase.
            self._global_start = time.perf_counter()

            # WRITE AND READ CYCLE EXECUTION

            while (
                not self._is_limit_reached()
                and not self.interruption_requested()
                and not io_failed
            ):
                cycle += 1
                self._cycle = cycle

                # Visually separate cycles on the graph.
                if cycle > 1:
                    self.emit_sample(
                        "SEP", float("nan"), float("nan")
                    )

                if TEST_DURATION_MODE == "cycles":
                    label_cycle = f"{cycle}/{TEST_CYCLES}"
                else:
                    label_cycle = str(cycle)

                # 3.1 WRITE

                self.emit_status(f"Cycle {label_cycle} - WRITE")
                self.emit_phase(TestPhase.WRITE)

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
                    self.emit_phase(TestPhase.OTHER)

                # Do not continue after an error or interruption.
                if io_failed or self.interruption_requested():
                    break

                # In duration mode, do not start a read operation
                # if the test duration was already reached after writing.
                if TEST_DURATION_MODE == "duration" and self._is_limit_reached():
                    break

                # 3.2 READ AND INTEGRITY VERIFICATION

                self.emit_status(f"Cycle {label_cycle} - READ")
                self.emit_phase(TestPhase.READ)

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
                    self.emit_phase(TestPhase.OTHER)


        # USER CANCELLATION
        # The CSV file and raw device are now closed.
        if self.cancellation_requested():
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
                integrity_errors=errors,
                interrupted=self.interruption_requested(),
                io_failed=io_failed,
            )
        )

        # INTERRUPTION AND I/O ERROR HANDLING

        if self.interruption_requested() or io_failed:
            result["result"] = "FAIL"
            result["completed"] = False

            if self.interruption_requested():
                result["error"] = self.abort_reason()

            # Never reformat after an interruption or I/O error.
            result["reformat"] = "NOT PERFORMED (test interrupted)"
            result["device_info"] = info.copy()
            return result

        # DEVICE REFORMATTING

        # Reformat even when performance thresholds were not met.
        # A reformatting failure causes the qualification to fail.
        self._run_reformat_step(result)

        # FINALIZE TEST RESULT

        result["device_info"] = info.copy()
        return result

   
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
        Sequentially write blocks to the device using O_DIRECT.

        For each block:
            - Measure write duration.
            - Compute instantaneous throughput.
            - Store the measurement in the CSV file.
            - Send the measurement and progress to the GUI.

        Return the number of bytes written and cumulative I/O time.
        """

       # MEASUREMENT INITIALIZATION
        bytes_done = 0
        io_time = 0.0

        # SEQUENTIAL BLOCK WRITES
        for block_number in range(NUMBER_BLOCKS):

            # Stop the cycle if an interruption was requested.
            if self.interruption_requested():
                break

            # WRITE AND TIMING
            t0 = time.perf_counter_ns()
            written = os.writev(fd, [buffer])
            duration = (time.perf_counter_ns() - t0) / 1e9

            # A partial write is considered an error.
            if written != BLOCK_SIZE:
                raise OSError(
                    f"Partial writing at block {block_number} "
                    f"({written}/{BLOCK_SIZE} bytes)"
                )

            # PERFORMANCE CALCULATION
            bytes_done += written
            io_time += duration

            mbps = compute_mbps(written, duration)
            block_measurements.append((written, duration))

            rel = elapsed()

            # STORE MEASUREMENT
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

            # DURATION CHECK
            if (
                TEST_DURATION_MODE == "duration"
                and elapsed() >= TEST_DURATION
            ):
                break

        # FLUSH WRITES
        # Outside performance timing.
        # Do not unnecessarily extend an interrupted test.
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
        Sequentially read blocks and verify data integrity.

        For each block:
            - Measure read duration using O_DIRECT.
            - Compute instantaneous throughput.
            - Verify block size and SHA-256 hash.
            - Store the measurement in the CSV file.
            - Send the measurement and progress to the GUI.

        Return the number of bytes read, cumulative I/O time and integrity errors.
        """

        # MEASUREMENT INITIALIZATION
        bytes_done = 0
        io_time = 0.0
        errors_found = 0

        # SEQUENTIAL BLOCK READS
        for block_number in range(NUMBER_BLOCKS):

            # Stop the cycle if an interruption was requested.
            if self.interruption_requested():
                break

            # READ AND TIMING
            t0 = time.perf_counter_ns()
            n = os.readv(fd, [buffer])
            duration = (time.perf_counter_ns() - t0) / 1e9

            # A zero-length read is considered an error.
            if n == 0:
                raise OSError(
                    f"Read operation interrupted at the data block {block_number}"
                )

            # PERFORMANCE CALCULATION
            bytes_done += n
            io_time += duration

            mbps = compute_mbps(n, duration)
            block_measurements.append((n, duration))

            # INTEGRITY VERIFICATION
            # SHA-256 computation remains outside I/O timing.
            received_hash = hashlib.sha256(buffer[:n]).digest()

            integrity = int(
                n == BLOCK_SIZE
                and received_hash == reference_hash
            )

            if not integrity:
                errors_found += 1

            # STORE MEASUREMENT
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

            # DURATION CHECK
            if (
                TEST_DURATION_MODE == "duration"
                and elapsed() >= TEST_DURATION
            ):
                break

        return bytes_done, io_time, errors_found


    def _get_elapsed(self):
        """Return elapsed time since the start of measurements, in seconds."""
        return time.perf_counter() - self._global_start


    def _is_limit_reached(self):
        """Return whether the configured cycle or duration limit was reached."""
        if TEST_DURATION_MODE == "cycles":
            return self._cycle >= TEST_CYCLES
        return self._get_elapsed() >= TEST_DURATION


    def _emit_progress(self, phase, block_number):
        """Send GUI progress limited to 95% during I/O operations."""

        if TEST_DURATION_MODE == "cycles":
            fraction = (block_number + 1) / NUMBER_BLOCKS

            if phase == "write":
                cycle_progress = 0.5 * fraction
            else:
                cycle_progress = 0.5 + 0.5 * fraction

            progress = ((self._cycle - 1) + cycle_progress) / TEST_CYCLES

        else:
            progress = self._get_elapsed() / TEST_DURATION

        pct = min(95, 5 + int(progress * 90))

        if pct != self._last_pct:
            self._last_pct = pct
            self.emit_progress(pct)


    def _run_reformat_step(self, result):
        """Reformat the DUT and update the test result."""

        if self.interruption_requested():
            result["result"] = "FAIL"
            result["completed"] = False
            result["error"] = self.abort_reason()
            result["reformat"] = "NOT PERFORMED (test interrupted)"
            return


        self.emit_progress(96)
        self.emit_status(
            f"Reformatting to {REFORMAT_FS.upper()}..."
        )

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
            set_result_error(
                result,
                exc,
                "Reformatting",
            )
            result["reformat"] = (
                f"FAILURE : {result['error']}"
            )


    def _cancel_test(self, result):
        """Clean current results and attempt to restore the DUT after cancellation."""

        self.emit_status("Cancellation: Cleaning up files...")

        # Delete only files associated with the current test.
        for key in ("docx", "csv"):
            path = result.get(key)

            if path:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError as exc:
                    self.emit_status(f"Cannot delete : {exc}")

        try:

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



# ============================================================
# 2. USB 2.0 FALLBACK TEST
# ============================================================

class Usb2FallbackTestService:
    """Verify USB 2.0 High-Speed fallback enumeration."""

    def __init__(
        self,
        emit_status,
        interruption_requested,
    ):
        self.emit_status = emit_status
        self.interruption_requested = interruption_requested


    def run(self):
        """Execute the USB 2.0 fallback test and restore USB 3."""

        result = {
            "result": "FAIL",
            "speed_mbps": None,
            "devnode": "",
            "error": "",
        }

        adapter = USBExt3cAdapter()
        connected = False

        try:
            # ------------------------------------------------
            # Connect to Acroname
            # ------------------------------------------------

            self.emit_status(
                "USB 2.0 fallback - connecting to Acroname..."
            )

            adapter.connect()
            connected = True

            # ------------------------------------------------
            # Power-cycle DUT
            # ------------------------------------------------

            self.emit_status(
                "USB 2.0 fallback - disconnecting DUT..."
            )

            adapter.power_off_dut()

            wait_for_test_port_empty(
                timeout_s=USB2_DISCONNECT_TIMEOUT_S,
                poll_interval_s=USB2_POLL_INTERVAL_S,
            )

            time.sleep(0.3)

            # ------------------------------------------------
            # Reconnect with USB2 only
            # ------------------------------------------------

            self.emit_status(
                "USB 2.0 fallback - reconnecting at High-Speed..."
            )

            adapter.enable_usb2_only()

            devnode, speed = wait_for_test_device(
                timeout_s=USB2_ENUM_TIMEOUT_S,
                poll_interval_s=USB2_POLL_INTERVAL_S,
                expected_speed_mbps=480.0,
                interruption_requested=self.interruption_requested,
            )

            result["result"] = "PASS"
            result["speed_mbps"] = speed
            result["devnode"] = devnode

        except Exception as exc:

            result["result"] = "FAIL"
            result["error"] = str(exc)

        finally:

            # ------------------------------------------------
            # Restore normal USB3 operation
            # ------------------------------------------------

            if connected:
                try:
                    self.emit_status(
                        "Restoring normal USB operation..."
                    )

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

                    restore_error = (
                        f"USB restore failed: {exc}"
                    )

                    if result["error"]:
                        result["error"] += (
                            f" | {restore_error}"
                        )
                    else:
                        result["error"] = restore_error

                    result["result"] = "FAIL"

                finally:
                    adapter.disconnect()

        return result