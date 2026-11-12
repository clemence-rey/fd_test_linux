"""USB tester GUI: display, test monitoring and reporting."""

import numpy as np

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont

from PyQt5.QtWidgets import (
    QMainWindow,
    QWidget,
    QLabel,
    QPushButton,
    QProgressBar,
    QPlainTextEdit,
    QVBoxLayout,
    QSplitter,
    QStackedWidget,
    QDialog,
    QFormLayout,
    QLineEdit,
    QComboBox,
    QDialogButtonBox,
)


from matplotlib.figure import Figure
from matplotlib.backends.backend_qt5agg import (
    FigureCanvasQTAgg as FigureCanvas,
)

from config import (
    TEST_FILE_SIZE_MB,
    BLOCK_SIZE_MB,
    MIN_WRITE_MBPS,
    MIN_READ_MBPS,
    TEST_LINK_REFS,
)


# ============================================================
# 1. INTERFACE CONSTANTS
# ============================================================

COLORS = {
    "wait": "#1f4e79",
    "test": "#b26a00",
    "pass": "#1e7e34",
    "fail": "#b02a37",
}

# -- Test window title
TXT_WINDOW_TITLE = "EOL test flash drive"

# -- Banner text
TXT_INSERT = "Insert flash drive"
TXT_READY = "Flash drive detected"
TXT_TESTING = "In progress..."
TXT_PASS = "PASS"
TXT_FAIL = "FAIL"

# -- Status text
TXT_DO_NOT_UNPLUG = "Do not unplug"

# -- Button text
TXT_BUTTON_START = "START"
TXT_BUTTON_NEW_TEST = "NEW TEST"
TXT_BUTTON_CANCEL_TEST = "CANCEL TEST"

# -- Text sizes
SIZE_TXT_BANNER = 44
SIZE_TXT_STATUS = 11
SIZE_TXT_DEV_STATUS = 10
SIZE_TXT_BUTTON = 16
SIZE_TXT_INFO = 9


# ============================================================
# 2. TEST CONFIGURATION
# ============================================================

class TestSetupDialog(QDialog):
    """Configure OF, SAP and test-link selection at startup."""

    def __init__(self):
        super().__init__()

        self.setWindowTitle(TXT_WINDOW_TITLE)
        self.setFixedWidth(400)

        # --- Widgets ---

        self.of_input = QLineEdit()
        self.of_input.setPlaceholderText("Enter OF number")

        self.sap_combo = QComboBox()
        self.sap_combo.addItems(TEST_LINK_REFS.keys())

        self.test_link_label = QLabel()

        # --- Layout ---

        layout = QVBoxLayout(self)

        title = QLabel("TEST CONFIGURATION")
        title.setFont(QFont("Sans", 16, QFont.Bold))
        title.setAlignment(Qt.AlignCenter)

        layout.addWidget(title)

        form = QFormLayout()

        form.addRow("OF Number:", self.of_input)
        form.addRow("SAP Number:", self.sap_combo)
        form.addRow("Test Link:", self.test_link_label)

        layout.addLayout(form)

        # --- Buttons ---

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )

        self.buttons.button(QDialogButtonBox.Ok).setText("START SESSION")

        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout.addWidget(self.buttons)

        # --- Signals ---

        self.sap_combo.currentTextChanged.connect(
            self._update_test_link_label
        )

        self.of_input.textChanged.connect(
            self._update_start_button_state
        )

        # --- Initialisation ---

        self._update_test_link_label()
        self._update_start_button_state()

    def _update_test_link_label(self):
        """Display the test link associated with the selected SAP."""

        sap = self.sap_combo.currentText()

        self.test_link_label.setText(
            TEST_LINK_REFS.get(sap, "N/A")
        )

    def _update_start_button_state(self):
        """Enable session start only when an OF number is provided."""

        valid = bool(self.of_input.text().strip())

        self.buttons.button(
            QDialogButtonBox.Ok
        ).setEnabled(valid)

    def get_configuration(self):
        """Return the configuration selected by the operator."""

        sap = self.sap_combo.currentText()

        return {
            "of": self.of_input.text().strip(),
            "sap": sap,
            "test_link": TEST_LINK_REFS[sap],
        }


# ============================================================
# 2. MAIN WINDOW
# ============================================================

class MainWindow(QMainWindow):
    """Manage the GUI and display test state and results."""

    start_requested = pyqtSignal()
    cancel_requested = pyqtSignal()
    new_test_requested = pyqtSignal()
    close_requested = pyqtSignal()


    def __init__(self):
        """Initialize the main test window."""
        super().__init__()

        self.setWindowTitle(TXT_WINDOW_TITLE)
        self.setWindowState(self.windowState() | Qt.WindowMaximized)

        # Current displayed information.
        self.current_info = ""

        self.show_test_link_header = True
        self.test_link_header = ""
        self._close_allowed = False

        # Initialize the interface.
        self.graph_dirty = False
        self._reset_graph_series()
        self._build_ui()
        self._set_banner(TXT_INSERT, COLORS["wait"])
        self._set_user_status("")
        self.set_result_text("")

        # Periodic graph refresh.
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh_graph)
        self.timer.start(300)


    def _build_ui(self):
        """Build and arrange the interface widgets."""

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # --- Main banner ---
        self.banner = QLabel()
        self.banner.setAlignment(Qt.AlignCenter)
        self.banner.setMinimumHeight(SIZE_TXT_BANNER + 20)
        root.addWidget(self.banner)

        # --- User status ---
        self.status_label = QLabel("")
        self.status_label.setFont(QFont("Sans", SIZE_TXT_STATUS))
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        # --- START button ---
        self.start_button = QPushButton(TXT_BUTTON_START)
        self.start_button.setFont(QFont("Sans", SIZE_TXT_BUTTON, QFont.Bold))
        self.start_button.setMinimumHeight(SIZE_TXT_BUTTON + 20)
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self.on_start_clicked)

        # --- NEW TEST button ---
        self.restart_button = QPushButton(TXT_BUTTON_NEW_TEST)
        self.restart_button.setFont(QFont("Sans", SIZE_TXT_BUTTON, QFont.Bold))
        self.restart_button.setMinimumHeight(SIZE_TXT_BUTTON + 20)
        self.restart_button.clicked.connect(self.on_restart_clicked)

        # --- CANCEL TEST button ---
        self.cancel_button = QPushButton(TXT_BUTTON_CANCEL_TEST)
        self.cancel_button.setFont(QFont("Sans", SIZE_TXT_BUTTON, QFont.Bold))
        self.cancel_button.setMinimumHeight(SIZE_TXT_BUTTON + 20)
        self.cancel_button.clicked.connect(self.on_cancel_clicked)

        # Only one action button is visible at a time.
        self.button_stack = QStackedWidget()
        self.button_stack.addWidget(self.start_button)
        self.button_stack.addWidget(self.restart_button)
        self.button_stack.addWidget(self.cancel_button)

        # START is displayed by default.
        self.button_stack.setCurrentWidget(self.start_button)

        root.addWidget(self.button_stack)

        # --- Progress bar ---
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        root.addWidget(self.progress)

        # --- Detailed test status ---
        self.dev_status_label = QLabel("")
        self.dev_status_label.setFont(QFont("Sans", SIZE_TXT_DEV_STATUS))
        self.dev_status_label.setAlignment(Qt.AlignCenter)
        root.addWidget(self.dev_status_label)

        # Live VBUS measurements
        self.vbus_label = QLabel("VBUS: N/A")
        self.vbus_label.setAlignment(Qt.AlignCenter)
        root.addWidget(self.vbus_label)

        # --- Device information ---
        self.info_text = QPlainTextEdit()
        self.info_text.setReadOnly(True)
        self.info_text.setFont(QFont("Monospace", SIZE_TXT_INFO))
        self.info_text.setLineWrapMode(QPlainTextEdit.NoWrap)

        # --- Performance graph ---
        self.figure = Figure(figsize=(8, 5))
        self.canvas = FigureCanvas(self.figure)
        self.ax = self.figure.add_subplot(111)
        self._init_graph_axes()

        # --- Information and graph layout ---
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.info_text)
        splitter.addWidget(self.canvas)
        splitter.setSizes([300, 750])
        root.addWidget(splitter, 1)


    def _set_banner(self, title, color):
        """Update the main banner text and color."""
        self.banner.setText(
            f"<div style='font-size:{SIZE_TXT_BANNER}px;"
            f"font-weight:bold'>{title}</div>"
        )
        self.banner.setStyleSheet(
            f"background-color:{color};color:white;border-radius:10px;"
        )


    def _set_user_status(self, text):
        """Update the message displayed below the banner."""
        self.status_label.setText(text)


    def set_step_status(self, text):
        """Display the current test step."""

        self.dev_status_label.setText(
            f"Step : {text}"
        )


    def _reset_view(self):
        """Clear previous information and reset graph and progress."""

       # Previous test information.
        self.set_result_text("")
        self.set_step_status("")

        # Performance graph.
        self._reset_graph_series()
        self.graph_dirty = False
        self._init_graph_axes()

        # Progress bar.
        self.progress.setValue(0)


    def _reset_graph_series(self):
        """Reset graph measurement series."""
        self.wt, self.ws, self.rt, self.rs = [], [], [], []


    def _init_graph_axes(self):
        """Configure graph axes, curves and qualification thresholds."""
        self.ax.clear()
        self.ax.set_xlabel("Test Time [min]")
        self.ax.set_ylabel("Speed [MB/s]")
        self.ax.set_title(
            f"USB Read / Write Speed ({TEST_FILE_SIZE_MB} MB test, "
            f"{BLOCK_SIZE_MB} MB blocks)"
        )
        self.ax.grid(True, alpha=0.3)
        # Measurements
        (self.line_w,) = self.ax.plot(
            [], [],linewidth=1,label="Write Speed"
            )
        (self.line_r,) = self.ax.plot(
            [], [],linewidth=1,label="Read Speed"
            )
        # PASS / FAIL thresholds
        self.ax.axhline(
            MIN_WRITE_MBPS,
            linestyle="-.",
            linewidth=1.5,
            color="gray",
            label=f"Min Write: {MIN_WRITE_MBPS:g} MB/s",
        )
        self.ax.axhline(
            MIN_READ_MBPS,
            linestyle=":",
            linewidth=1.5,
            color="gray",
            label=f"Min Read: {MIN_READ_MBPS:g} MB/s",
        )
        self.ax.legend(loc="lower left")
        self.figure.tight_layout()
        self.canvas.draw_idle()


    def _refresh_graph(self):
        """Refresh the graph when new measurements are available."""
        if not self.graph_dirty:
            return
        self.graph_dirty = False
        self.line_w.set_data(np.asarray(self.wt) / 60, self.ws)
        self.line_r.set_data(np.asarray(self.rt) / 60, self.rs)
        self.ax.relim()
        self.ax.autoscale_view()
        self.canvas.draw_idle()


    def on_start_clicked(self):
        """Notify the Controller that START was requested."""
        self.start_requested.emit()


    def on_cancel_clicked(self):
        """Notify the Controller that cancellation was requested."""
        self.cancel_requested.emit()

    def set_cancel_enabled(self, enabled):
        """Enable or disable test cancellation."""
        self.cancel_button.setEnabled(enabled)

    def on_restart_clicked(self):
        """Notify the Controller that a new test was requested."""
        self.new_test_requested.emit()

    def get_report_figure(self):
        """Return the graph used in the final report."""

        return self.figure

    def allow_close(self):
        """Permit the final window close."""

        self._close_allowed = True
        self.close()

    def closeEvent(self, event):
        """Delegate shutdown to the controller."""

        if self._close_allowed:
            self.timer.stop()
            event.accept()
            return

        event.ignore()
        self.close_requested.emit()

    def show_waiting_insert(self):
        """Display the waiting-for-device state."""

        self._reset_view()
        self.start_button.setEnabled(False)
        self.button_stack.setCurrentWidget(self.start_button)

        self._set_banner(TXT_INSERT, COLORS["wait"])
        self._set_user_status("")


    def show_ready(self, device_label, device_info_text):
        """Display a detected and validated device."""

        self.progress.setValue(0)

        self._set_banner(TXT_READY, COLORS["wait"])
        self._set_user_status(device_label)
        self.set_step_status(
            "Flash drive detected, waiting for START"
        )

        self.set_result_text(device_info_text)

        self.start_button.setEnabled(True)
        self.button_stack.setCurrentWidget(self.start_button)


    def show_testing(self):
        """Display the active-test state."""

        self._reset_view()

        self._set_banner(TXT_TESTING, COLORS["test"])
        self._set_user_status(TXT_DO_NOT_UNPLUG)

        self.cancel_button.setEnabled(True)
        self.button_stack.setCurrentWidget(self.cancel_button)


    def show_cancelling(self):
        """Display cancellation progress."""

        self.cancel_button.setEnabled(False)

        self._set_banner(
            "Cancellation in progress...",
            COLORS["test"],
        )

        self._set_user_status(
            "Do not unplug the flash drive."
        )

        self.set_step_status(
            "Stopping test operations..."
        )


    def set_progress(self, value):
        """Update the progress bar."""

        self.progress.setValue(value)


    def add_speed_sample(self, operation, elapsed_s, speed_mbps):
        """Add one performance sample to the graph."""

        if operation == "SEP":
            nan = float("nan")
            self.wt.append(nan)
            self.ws.append(nan)
            self.rt.append(nan)
            self.rs.append(nan)

        elif operation == "WRITE":
            self.wt.append(elapsed_s)
            self.ws.append(speed_mbps)

        elif operation == "READ":
            self.rt.append(elapsed_s)
            self.rs.append(speed_mbps)

        else:
            return

        self.graph_dirty = True


    def show_vbus_sample(self, phase, sample):
        """Display one live electrical measurement."""

        self.vbus_label.setText(
            f"VBUS [{phase.value}]: "
            f"{sample.voltage_v:.3f} V | "
            f"{sample.current_ma:.2f} mA"
        )

    def set_test_link_info(self, reference, cycles, max_cycles):
        """Update the test-link presentation header."""

        self.test_link_header = (
            "=== TEST-LINK ===\n"
            f"Reference : {reference}\n"
            f"Mating cycle : {cycles} / {max_cycles}"
        )

        self._refresh_information_panel()

    def set_result_text(
        self,
        text,
        show_test_link_header=True,
    ):
        """Set the currently displayed information text."""

        self.current_info = text
        self.show_test_link_header = show_test_link_header
        self._refresh_information_panel()

    def _refresh_information_panel(self):
        """Render the current information panel."""

        if self.show_test_link_header:
            text = (
                f"{self.test_link_header}\n\n"
                f"{self.current_info}"
            )
        else:
            text = self.current_info

        self.info_text.setPlainText(text)

    def prepare_final_graph(self, result):
        """Finalize the performance graph."""

        self._refresh_graph()

        for key, color, label in (
            ("avg_write", "darkblue", "Average Write"),
            ("avg_read", "darkorange", "Average Read"),
        ):
            value = result.get(key)

            if value is not None and value > 0:
                self.ax.axhline(
                    value,
                    linestyle="--",
                    alpha=0.5,
                    color=color,
                    label=f"{label}: {value:.1f} MB/s",
                )

        self.ax.legend(loc="lower left")
        self.canvas.draw()


    def show_final_result(self, result, report_saved):
        """Display the final test verdict and report."""

        passed = result["result"] == "PASS"

        self.progress.setValue(100)
        self.set_result_text(result.get("report", ""), show_test_link_header=False)

        self._set_banner(
            TXT_PASS if passed else TXT_FAIL,
            COLORS["pass"] if passed else COLORS["fail"],
        )

        self._set_user_status(
            "" if report_saved
            else "WARNING: The report has not been saved."
        )

        self.start_button.setEnabled(False)
        self.button_stack.setCurrentWidget(self.restart_button)


    def show_cancel_failed(self, message):
        """Display an unsuccessful cancellation."""

        self._set_banner("Cancellation failed", COLORS["fail"])
        self._set_user_status(
            "The flash drive could not be restored."
        )
        self.set_step_status(message)
        self.button_stack.setCurrentWidget(self.restart_button)