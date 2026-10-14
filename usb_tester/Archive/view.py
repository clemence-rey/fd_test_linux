"""Interface graphique du testeur USB : affichage, suivi du test et rapports."""

import numpy as np
from datetime import datetime

from PyQt5.QtCore import Qt, QTimer
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
)

from matplotlib.figure import Figure
from matplotlib.backends.backend_qt5agg import (
    FigureCanvasQTAgg as FigureCanvas,
)

from config import (
    RESULTS_DIR,
    TEST_FILE_SIZE_MB,
    BLOCK_SIZE_MB,
    MIN_WRITE_MBPS,
    MIN_READ_MBPS,
)

from controller import (
    TestWorker,
    UsbWatcher,
)

from model import (
    get_diskseq,
    get_device_info,
    format_device_info,
    assert_same_device,
    assert_test_port,
    write_pdf_report,
)

# ============================================================

# Thread : GUI

# ============================================================

WAITING_INSERT, READY, TESTING, WAITING_REMOVAL, WAITING_RESTART = range(5)

COLORS = {
    "wait": "#1f4e79",
    "test": "#b26a00",
    "pass": "#1e7e34",
    "fail": "#b02a37",
}

TXT_INSERT = "Insérez la clé USB"
TXT_TESTING = "Test en cours..."
TXT_PASS = "PASS"
TXT_FAIL = "FAIL"
TXT_DO_NOT_UNPLUG = "Ne pas débrancher svp"
TXT_REMOVE = "Retirer la clé"
TXT_READY = "Clé détectée"
TXT_START = "START"
TXT_INTERRUPT = "TEST INTERROMPU"

SIZE_TXT_BANNER = 44
SIZE_TXT_STATUS = 11
SIZE_TXT_DEV_STATUS = 10
SIZE_TXT_BUTTON = 16
SIZE_TXT_INFO = 9

# ============================================================
# 2. FENÊTRE PRINCIPALE
# ============================================================

class MainWindow(QMainWindow):
    """Gère la GUI, les événements USB et l’affichage des résultats."""

    def __init__(self):
        """Initialise la fenêtre, ses états et les threads de surveillance."""
        super().__init__()
        self.setWindowTitle("EOL test flash drive")
        self.setWindowState(self.windowState() | Qt.WindowMaximized)
        self.current_devnode = None
        self.current_diskseq = None
        self.state = WAITING_INSERT
        self.worker = None
        self._closing = False
        self.graph_dirty = False
        self._reset_series()
        self._build_ui()
        self._set_banner(TXT_INSERT, COLORS["wait"])
        self._set_user_status("")
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh_graph)
        self.timer.start(300)
        self.watcher = UsbWatcher()
        self.watcher.finished.connect(self._on_thread_finished)
        self.watcher.usb_event.connect(self.on_usb_event)
        self.watcher.start()

    # --- Construction et mise à jour de l’interface ---

    def _build_ui(self):
        """Construit les widgets et organise les modes production et développement."""
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # ----- Éléments statut et progression -----
        self.banner = QLabel()
        self.banner.setAlignment(Qt.AlignCenter)
        self.banner.setMinimumHeight(SIZE_TXT_BANNER + 20)
        root.addWidget(self.banner)
        self.status_label = QLabel("")
        self.status_label.setFont(QFont("Sans", SIZE_TXT_STATUS))
        self.status_label.setAlignment(Qt.AlignCenter)
        root.addWidget(self.status_label)
        self.status_label.setWordWrap(True)
        self.start_button = QPushButton(TXT_START)
        self.start_button.setFont(
            QFont("Sans", SIZE_TXT_BUTTON, QFont.Bold))
        self.start_button.setMinimumHeight(SIZE_TXT_BUTTON + 20)
        self.start_button.setStyleSheet(
            "QPushButton{background-color:#1e7e34;color:white;"
            "border-radius:10px;}"
            "QPushButton:disabled{background-color:#999;color:#ddd;}")
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self.on_start_clicked)
        root.addWidget(self.start_button)
        self.restart_button = QPushButton("NOUVEAU TEST")
        self.restart_button.setFont(QFont("Sans", SIZE_TXT_BUTTON, QFont.Bold))
        self.restart_button.setMinimumHeight(SIZE_TXT_BUTTON + 20)
        self.restart_button.clicked.connect(self.on_restart_clicked)
        self.restart_button.hide()
        root.addWidget(self.restart_button)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)

        # ----- Éléments données -----
        self.dev_status_label = QLabel("")
        self.dev_status_label.setFont(QFont("Sans", SIZE_TXT_DEV_STATUS))
        self.dev_status_label.setAlignment(Qt.AlignCenter)
        self.info_text = QPlainTextEdit()
        self.info_text.setReadOnly(True)
        self.info_text.setFont(QFont("Monospace", SIZE_TXT_INFO))
        self.info_text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.figure = Figure(figsize=(8, 5))
        self.canvas = FigureCanvas(self.figure)
        self.ax = self.figure.add_subplot(111)
        self._init_axes()
        root.addWidget(self.progress)
        root.addWidget(self.dev_status_label)
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.info_text)
        splitter.addWidget(self.canvas)
        splitter.setSizes([300, 750])
        root.addWidget(splitter, 1)


    # --- Zone banière ---

    def _set_banner(self, title, color):
        """Actualise le texte et la couleur de la bannière principale."""
        size = SIZE_TXT_BANNER
        self.banner.setText(f"<div style='font-size:{size}px;font-weight:bold'>{title}</div>")
        self.banner.setStyleSheet(f"background-color:{color};color:white;border-radius:10px;")

    def _set_user_status(self, text):
        """Ligne juste sous la bannière."""
        self.status_label.setText(text)

    def _set_steps_status(self, text):
        """Étapes détaillées du test."""
        self.dev_status_label.setText(f"Étape : {text}")

    # --- Zone de texte ---

    def _render_info(self, text):
        """Affiche les informations du test courant."""
        self.current_info = text
        self.info_text.setPlainText(text)

    # --- Graphique des performances ---

    def _reset_series(self):
        """Réinitialise les séries de mesures affichées sur le graphique."""
        self.wt, self.ws, self.rt, self.rs = [], [], [], []

    def _init_axes(self):
        """Configure les axes, courbes et seuils du graphique."""
        self.ax.clear()
        self.ax.set_xlabel("Test Time [min]")
        self.ax.set_ylabel("Speed [MB/s]")
        self.ax.set_title(
            f"USB Read / Write Speed "
            f"({TEST_FILE_SIZE_MB} MB test, {BLOCK_SIZE_MB} MB blocks)"
        )
        self.ax.grid(True, alpha=0.3)
        # Mesures
        (self.line_w,) = self.ax.plot([], [],linewidth=1,label="Write Speed")
        (self.line_r,) = self.ax.plot([], [],linewidth=1,label="Read Speed")
        # Seuils PASS / FAIL
        self.ax.axhline(
            MIN_WRITE_MBPS,
            linestyle="--",
            color="darkblue",
            label=f"Min Write: {MIN_WRITE_MBPS:g} MB/s")
        self.ax.axhline(
            MIN_READ_MBPS,
            linestyle="--",
            color="darkorange",
            label=f"Min Read: {MIN_READ_MBPS:g} MB/s")
        self.ax.legend(loc="lower left")
        self.figure.tight_layout()
        self.canvas.draw_idle()

    def _refresh_graph(self):
        """Actualise le graphique lorsque de nouvelles mesures sont disponibles."""
        if not self.graph_dirty:
            return
        self.graph_dirty = False
        self.line_w.set_data(np.asarray(self.wt) / 60, self.ws)
        self.line_r.set_data(np.asarray(self.rt) / 60, self.rs)
        self.ax.relim()
        self.ax.autoscale_view()
        self.canvas.draw_idle()

    # --- Détection USB et états de l’interface ---

    def on_usb_event(self, devnode, event):
        """
        Gère les événements du port USB réservé.

        event :
            "inserted" : périphérique détecté
            "removed"  : périphérique déconnecté
        """

        if event == "inserted":

            if self.state == WAITING_INSERT:
                self._go_ready(devnode)

            elif self.state == READY:
                if devnode != self.current_devnode:
                    self._go_ready(devnode)

        elif event == "removed":

            if devnode != self.current_devnode:
                return

            if self.state == TESTING:
                if self.worker is not None:
                    self.worker.abort("Déconnexion USB détectée")

            elif self.state in (READY, WAITING_REMOVAL):
                self._go_waiting_insert()

    def on_restart_clicked(self):
        """Réinitialise l'interface après un test interrompu."""

        if self.state != WAITING_RESTART:
            return

        self.restart_button.hide()
        self._go_waiting_insert()

        if self.connected_devnode is not None:
            self._go_ready(self.connected_devnode)

    def _device_label(self, info):
        """Retourne le fabricant et le modèle de la clé USB."""
        manufacturer = str(info.get("Manufacturer") or "N/A").strip()
        model = str(info.get("Model") or "N/A").strip()

        return " - ".join(x for x in (manufacturer, model) if x != "N/A")

    def _go_ready(self, devnode):
        """Prépare le test de la clé détectée sur le port réservé."""

        self.state = READY
        self.current_devnode = devnode
        self.current_diskseq = get_diskseq(devnode)

        info = get_device_info(devnode)

        self.progress.setValue(0)
        self._set_banner(TXT_READY, COLORS["wait"])
        self._set_user_status(self._device_label(info))
        self._dev_step_status("clé détectée, en attente de START")
        self._render_info(format_device_info(info))

        self.start_button.setEnabled(True)

    def _go_waiting_insert(self):
        """Réinitialise l'interface en attente d'une nouvelle clé."""

        self.state = WAITING_INSERT
        self.current_devnode = None
        self.current_diskseq = None

        self.start_button.setEnabled(False)
        self.restart_button.hide()
        self.progress.setValue(0)

        self._set_banner(TXT_INSERT, COLORS["wait"])
        self._set_user_status("")
        self._dev_step_status("en attente d'une clé")

    def on_start_clicked(self):
        """Démarre le test uniquement si le périphérique est toujours valide."""

        if self.state != READY or self.current_devnode is None:
            return

        try:
            assert_test_port(self.current_devnode)

            assert_same_device(
                self.current_devnode,
                self.current_diskseq
            )

        except (OSError, RuntimeError) as e:
            self._dev_step_status(f"Périphérique invalide : {e}")
            self._go_waiting_insert()
            return

        self.start_button.setEnabled(False)
        self.start_test(self.current_devnode)

    # --- Lancement et suivi du test ---

    def start_test(self, devnode):
        """Prépare l’interface et démarre le thread de test USB."""
        self.state = TESTING
        self.current_devnode = devnode
        self.restart_button.hide()
        self._reset_series()
        self._init_axes()
        self._render_info("")
        self.progress.setValue(0)
        self._set_banner(TXT_TESTING, COLORS["test"])
        self._set_user_status(TXT_DO_NOT_UNPLUG)
        if self.current_diskseq is None:
            raise RuntimeError("Identité USB non initialisée")
        assert_same_device(self.current_devnode, self.current_diskseq)
        self.worker = TestWorker(self.current_devnode, self.current_diskseq)
        self.worker.status.connect(self._set_steps_status)
        self.worker.info_ready.connect(self._render_info)
        self.worker.sample.connect(self.on_sample)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.test_done.connect(self.on_test_done)
        # Important:
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.finished.connect(self._on_thread_finished)
        self.worker.start()

    def on_worker_finished(self):
        """Libère la référence au thread de test terminé."""
        self.worker = None

    def on_sample(self, op, t, mbps):
        """Ajoute une mesure aux séries affichées sur le graphique."""
        if op == "SEP":
            nan = float("nan")
            self.wt.append(nan); self.ws.append(nan)
            self.rt.append(nan); self.rs.append(nan)
        elif op == "WRITE":
            self.wt.append(t); self.ws.append(mbps)
        else:
            self.rt.append(t); self.rs.append(mbps)
        self.graph_dirty = True

    def on_test_done(self, r):
        # Graphe final (+ moyennes) -> PNG
        """Prépare le graphique et le rapport après la fin du test."""
        self._refresh_graph()
        if r["avg_write"]:
            self.ax.axhline(r["avg_write"], linestyle="--", alpha=0.5,
                            color="C0",
                            label=f"Average Write {r['avg_write']:.1f} MB/s")
        if r["avg_read"]:
            self.ax.axhline(r["avg_read"], linestyle="--", alpha=0.5,
                            color="C1",
                            label=f"Average Read {r['avg_read']:.1f} MB/s")
        self.ax.legend(loc="lower left")
        self.canvas.draw()
        if r.get("graph"):
            try:
                self.figure.savefig(r["graph"], dpi=200)
            except Exception:
                pass
        passed = r["result"] == "PASS"
        # Zone de texte : rapport complet
        self._render_info(r.get("report") or self.current_info)
        # Le PDF est écrit AVANT la bannière : on laisse d'abord la boucle
        # d'événements repeindre le statut, puis _finalize fait le reste.
        self._set_steps_status("Génération du rapport...")
        QTimer.singleShot(0, lambda: self._finalize(r, passed))


    def _on_thread_finished(self):
        """Réessaie de fermer la fenêtre après l'arrêt d'un thread."""
        if self._closing:
            QTimer.singleShot(0, self.close)


    def _finalize(self, r, passed):
        """Affiche le verdict final après la génération du rapport PDF."""
        self._write_pdf(r)
        self.progress.setValue(100)
        if not r.get("completed", False):
            self.state = WAITING_RESTART
            self._set_banner(TXT_INTERRUPT, COLORS["fail"])
            self._set_user_status("")
            self.restart_button.show()
            return
        self.state = WAITING_REMOVAL
        self.restart_button.hide()
        self._set_banner(TXT_PASS if passed else TXT_FAIL, COLORS["pass"] if passed else COLORS["fail"])
        self._set_user_status(TXT_REMOVE)

    def _write_pdf(self, r):
        """Enregistre le rapport PDF et signale les erreurs éventuelles."""
        path = r.get("pdf") or str(
            RESULTS_DIR / f"USB_report_{datetime.now():%Y%m%d_%H%M%S}.pdf")
        try:
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            write_pdf_report(path, r.get("report", ""), self.figure)
            self._set_steps_status(f"PDF enregistré : {path}")
        except Exception as e:
            self._set_steps_status(f"PDF échoué : {e}")

    # --- Fermeture et arrêt des threads ---

    def closeEvent(self, event):
        """Ferme l'application sans bloquer le thread graphique."""

        # Arrêter les timers de la GUI
        self.timer.stop()

        # Demander l'arrêt du test en cours
        if self.worker is not None and self.worker.isRunning():
            self._closing = True
            self.worker.abort()
            event.ignore()
            return

        # Arrêter la surveillance USB
        if self.watcher is not None and self.watcher.isRunning():
            self._closing = True
            self.watcher.stop()
            event.ignore()
            return

        # Tous les threads sont arrêtés
        event.accept()