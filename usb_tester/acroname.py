"""BrainStem adapter for the Acroname USBExt3c."""

import brainstem

from config import ACRONAME_PORT


class USBExt3cAdapter:
    """Provide synchronous access to one USBExt3c."""

    def __init__(self):
        self._stem = brainstem.stem.USBExt3c()
        self._connected = False


    def connect(self):
        """Connect to the first USBExt3c found over USB."""

        error = self._stem.discoverAndConnect(
            brainstem.link.Spec.USB
        )

        if error != brainstem.result.Result.NO_ERROR:
            raise RuntimeError(
                f"Acroname connection failed: {error}"
            )

        self._connected = True

    def restore_normal_port(self):
        """Force the DUT port back to normal USB operation."""

        self._require_connection()

        error = self._stem.usb.setPortEnable(
            ACRONAME_PORT
        )
        self._check_error(
            error,
            "Acroname port enable",
        )

        error = self._stem.usb.setHiSpeedDataEnable(
            ACRONAME_PORT
        )
        self._check_error(
            error,
            "USB High-Speed enable",
        )

        error = self._stem.usb.setSuperSpeedDataEnable(
            ACRONAME_PORT
        )
        self._check_error(
            error,
            "USB SuperSpeed enable",
        )

        error = self._stem.usb.setPowerEnable(
            ACRONAME_PORT
        )
        self._check_error(
            error,
            "VBUS enable",
        )


    def read_vbus(self):
        """Return port voltage in volts and current in milliamperes."""

        self._require_connection()

        voltage = self._stem.usb.getPortVoltage(ACRONAME_PORT)
        current = self._stem.usb.getPortCurrent(ACRONAME_PORT)

        if voltage.error != brainstem.result.Result.NO_ERROR:
            raise RuntimeError(
                f"Voltage measurement failed: {voltage.error}"
            )

        if current.error != brainstem.result.Result.NO_ERROR:
            raise RuntimeError(
                f"Current measurement failed: {current.error}"
            )

        return (
            voltage.value / 1_000_000,
            current.value / 1_000,
        )


    def disconnect(self):
        """Disconnect safely."""

        if self._connected:
            self._stem.disconnect()
            self._connected = False


    def _require_connection(self):
        """Ensure that the USBExt3c is connected."""

        if not self._connected:
            raise RuntimeError("Acroname is not connected")


    def _check_error(self, error, operation):
        """Raise an exception when a BrainStem command fails."""

        if error != brainstem.result.Result.NO_ERROR:
            raise RuntimeError(
                f"{operation} failed: {error}"
            )


    def power_off_dut(self):
        """Power off the DUT while keeping the USB data/CC configuration."""

        self._require_connection()

        error = self._stem.usb.setPowerDisable(
            ACRONAME_PORT
        )

        self._check_error(
            error,
            "DUT power disable",
        )


    def enable_usb2_only(self):
        """Power the DUT with USB 2 HS enabled and USB 3 SS disabled."""

        self._require_connection()

        # Configure data lines while DUT is powered off.
        error = self._stem.usb.setHiSpeedDataEnable(
            ACRONAME_PORT
        )
        self._check_error(
            error,
            "USB 2 High-Speed enable",
        )

        error = self._stem.usb.setSuperSpeedDataDisable(
            ACRONAME_PORT
        )
        self._check_error(
            error,
            "USB SuperSpeed disable",
        )

        # Power DUT only after the data path is configured.
        error = self._stem.usb.setPowerEnable(
            ACRONAME_PORT
        )
        self._check_error(
            error,
            "DUT power enable",
        )


    def enable_normal_usb(self):
        """Restore normal USB 2 + USB 3 operation."""

        self.restore_normal_port()