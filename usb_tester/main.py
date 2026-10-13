"""
Point d'entrée du programme de qualification des clés USB.

Vérifie les privilèges administrateur, initialise l'application Qt
et lance l'interface graphique.
"""

import sys
from PyQt5.QtWidgets import QApplication

from model import ensure_root
from view import MainWindow


# ============================================================
# 1. LANCEMENT DE L'APPLICATION
# ============================================================

def main():
    """Initialise et exécute l'application de test USB."""
    ensure_root()
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()