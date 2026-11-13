"""BrainStem adapter for the Acroname USBExt3c."""

# Third-party libraries
import brainstem

# Application configuration
from config import ACRONAME_PORT


class USBExt3cAdapter:
    """Provide synchronous control and VBUS measurement for one USBExt3c."""

    # --------------------------------------------------------
    # INITIALIZATION
    # --------------------------------------------------------

    def __init__(self):
        """Initialize the USBExt3c adapter."""

        self._stem = brainstem.stem.USBExt3c()
        self._connected = False


    # --------------------------------------------------------
    # CONNECTION MANAGEMENT
    # --------------------------------------------------------

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


    def disconnect(self):
        """Disconnect from the USBExt3c."""

        if self._connected:
            self._stem.disconnect()
            self._connected = False


    # --------------------------------------------------------
    # DUT PORT CONTROL
    # --------------------------------------------------------

    def restore_normal_port(self):
        """Restore normal USB operation on the DUT port."""

        self._require_connection()

        error = self._stem.usb.setPortEnable(
            ACRONAME_PORT
        )
        self._check_error(error, "Acroname port enable")

        error = self._stem.usb.setHiSpeedDataEnable(
            ACRONAME_PORT
        )
        self._check_error(error, "USB High-Speed enable")

        error = self._stem.usb.setSuperSpeedDataEnable(
            ACRONAME_PORT
        )
        self._check_error(error, "USB SuperSpeed enable")

        error = self._stem.usb.setPowerEnable(
            ACRONAME_PORT
        )
        self._check_error(error, "VBUS enable")


    def power_off_dut(self):
        """Disable DUT power while preserving the port configuration."""

        self._require_connection()

        error = self._stem.usb.setPowerDisable(
            ACRONAME_PORT
        )

        self._check_error(error, "DUT power disable")


    def enable_usb2_only(self):
        """Enable USB 2.0 High-Speed operation and disable SuperSpeed."""

        self._require_connection()

        # Configure data paths while the DUT is powered off.
        error = self._stem.usb.setHiSpeedDataEnable(
            ACRONAME_PORT
        )
        self._check_error(error, "USB High-Speed enable")

        error = self._stem.usb.setSuperSpeedDataDisable(
            ACRONAME_PORT
        )
        self._check_error(error, "USB SuperSpeed disable")

        # Power the DUT after configuring the data paths.
        error = self._stem.usb.setPowerEnable(
            ACRONAME_PORT
        )
        self._check_error(error, "DUT power enable")


    # --------------------------------------------------------
    # VBUS MEASUREMENT
    # --------------------------------------------------------

    def read_vbus(self):
        """Return DUT voltage in volts and current in milliamperes."""

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


    # --------------------------------------------------------
    # INTERNAL HELPERS
    # --------------------------------------------------------

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