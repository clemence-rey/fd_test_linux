"""
Point d'entrée du programme de qualification des clés USB.

Vérifie les privilèges administrateur, initialise l'application Qt
et lance l'interface graphique.
"""

import sys
import os
from PyQt5.QtWidgets import QApplication, QDialog

from view import MainWindow, TestSetupDialog

# ============================================================
# 1. GESTION DES PRIVILÈGES
# ============================================================

def ensure_root():
    """Relance le programme en root avec authentification graphique."""
    if os.geteuid() == 0:
        return

    script = os.path.join(os.path.dirname(__file__), "main.py")

    env_vars = [
        f"{key}={os.environ[key]}"
        for key in ("DISPLAY", "XAUTHORITY")
        if os.environ.get(key)
    ]

    os.execvp("pkexec", [
        "pkexec", "env",
        *env_vars,
        sys.executable, script, *sys.argv[1:]
    ])


# ============================================================
# 2. INITIALISATION DE L'APPLICATION
# ============================================================

def main():
    """Initialise et exécute l'application de test USB."""

    # Vérification des privilèges administrateur.
    ensure_root()

    # Initialisation de l'application Qt.
    app = QApplication(sys.argv)

    # Configuration de la session.
    setup = TestSetupDialog()

    if setup.exec_() != QDialog.Accepted:
        sys.exit(0)

    test_config = setup.get_configuration()

    # Création et affichage de la fenêtre principale.
    win = MainWindow(test_config)
    win.show()

    # Exécution de la boucle d'événements Qt.
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()