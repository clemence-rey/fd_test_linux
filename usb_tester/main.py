"""
Entry point for the USB flash drive qualification program.

Ensures administrator privileges, initializes the Qt application
and starts the graphical interface.
"""

# Standard library
import os
import sys
from pathlib import Path

# Third-party libraries
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QApplication, QDialog

# Application modules
from controller import AppController
from view import MainWindow, TestSetupDialog
from test_profiles import TEST_PROFILES

# ============================================================
# 1. PRIVILEGE MANAGEMENT
# ============================================================

def ensure_root():
    """Restart the program as root using graphical authentication."""
    if os.geteuid() == 0:
        return

    script = os.path.join(os.path.dirname(__file__), "main.py")

    env_vars = [
        f"{key}={os.environ[key]}"
        for key in ("DISPLAY", "XAUTHORITY")
        if os.environ.get(key)
    ]

    env_vars.append("XDG_RUNTIME_DIR=/tmp/runtime-root")

    os.execvp("pkexec", [
        "pkexec", "env",
        *env_vars,
        sys.executable, script, *sys.argv[1:]
    ])


# ============================================================
# 2. APPLICATION INITIALIZATION
# ============================================================

def main():
    """Initialize and run the USB test application."""

    # Ensure administrator privileges.
    ensure_root()

    # Initialize the Qt application.
    app = QApplication(sys.argv)
    app.setApplicationName("USB Tester")

    icon_path = Path(__file__).resolve().parent.parent / "usb_tester.png"
    app.setWindowIcon(QIcon(str(icon_path)))

    # Configure the test session.
    setup = TestSetupDialog()
    
    if setup.exec_() != QDialog.Accepted:
        sys.exit(0)

    test_config = setup.get_configuration()
    test_profile = TEST_PROFILES[test_config["sap"]].copy()

    # Create and display the main window.
    view = MainWindow(test_profile)

    controller = AppController(
        view=view,
        test_config=test_config,
        parent=app,
    )

    view.show()
    controller.initialize()

    sys.exit(app.exec_())


# Application entry point.
if __name__ == "__main__":
    main()