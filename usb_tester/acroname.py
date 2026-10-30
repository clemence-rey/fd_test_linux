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

    def read_vbus(self):
        """Return port voltage in volts and current in milliamperes."""

        if not self._connected:
            raise RuntimeError("Acroname is not connected")

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