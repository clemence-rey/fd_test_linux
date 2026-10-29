"""Acroname USBExt3c interface."""

import brainstem

from config import ACRONAME_PORT


class Acroname:
    """Manage USBExt3c connection and VBUS measurements."""

    def __init__(self):
        self.stem = brainstem.stem.USBExt3c()

    def connect(self):
        """Connect to the USBExt3c."""
        err = self.stem.discoverAndConnect(brainstem.link.Spec.USB)

        if err != 0:
            raise RuntimeError(f"Acroname connection failed: {err}")

    def measure_vbus(self):
        """Return VBUS voltage (V) and current (mA)."""
        voltage = self.stem.usb.getPortVoltage(ACRONAME_PORT)
        current = self.stem.usb.getPortCurrent(ACRONAME_PORT)

        if voltage.error != 0:
            raise RuntimeError(f"Voltage measurement failed: {voltage.error}")

        if current.error != 0:
            raise RuntimeError(f"Current measurement failed: {current.error}")

        return voltage.value / 1_000_000, current.value / 1_000

    def disconnect(self):
        """Disconnect from the USBExt3c."""
        self.stem.disconnect()