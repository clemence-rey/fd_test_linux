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
    DEV_MODE,
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
TXT_TOO_MANY = "Plusieurs clés branchées : ne laisser que la clé à tester"
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
        self.setWindowTitle("Test flash drive")
        if DEV_MODE:
            self.setWindowState(self.windowState() | Qt.WindowMaximized)
        else:
            self.resize(550, 300)
        self.state = WAITING_INSERT
        self.usb_devnodes = []
        self.current_devnode = None
        self.current_diskseq = None
        self.worker = None
        self.graph_dirty = False
        self._ready_snapshot = None
        self._reset_series()
        self._build_ui()
        self._set_banner(TXT_INSERT, COLORS["wait"])
        self._set_user_status("")
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh_graph)
        self.timer.start(300)
        self.watcher = UsbWatcher()
        self.watcher.drives_changed.connect(self.on_drives_changed)
        self.watcher.usb_removed.connect(self.on_usb_removed)
        self.watcher.start()

    # --- Construction et mise à jour de l’interface ---

    def _build_ui(self):
        """Construit les widgets et organise les modes production et développement."""
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # ----- Éléments communs (prod + dev) -----
        self.banner = QLabel()
        self.banner.setAlignment(Qt.AlignCenter)
        if DEV_MODE:
            self.banner.setMinimumHeight(SIZE_TXT_BANNER + 20)
            root.addWidget(self.banner)
        else:
            self.banner.setMinimumHeight(SIZE_TXT_BANNER + 20)
            root.addWidget(self.banner, 1)      # la bannière remplit la fenêtre
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

        # ----- Éléments mode développement -----
        # Toujours créés (les slots y écrivent), ajoutés à la fenêtre
        # seulement en mode développement.
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
        if DEV_MODE:
            root.addWidget(self.dev_status_label)
            splitter = QSplitter(Qt.Horizontal)
            splitter.addWidget(self.info_text)
            splitter.addWidget(self.canvas)
            splitter.setSizes([550, 750])
            root.addWidget(splitter, 1)

    def _set_banner(self, title, color):
        """Actualise le texte et la couleur de la bannière principale."""
        size = SIZE_TXT_BANNER
        self.banner.setText(
            f"<div style='font-size:{size}px;font-weight:bold'>{title}</div>"
        )
        self.banner.setStyleSheet(
            f"background-color:{color};color:white;border-radius:10px;")

    def _set_user_status(self, text):
        """Ligne juste sous la bannière (prod + dev)."""
        self.status_label.setText(text)

    def _dev_status(self, text):
        """Étapes détaillées du test (visibles uniquement en dev)."""
        if DEV_MODE:
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

    def on_drives_changed(self, devnodes):
        """Réagit à la liste des périphériques USB détectés."""
        self.usb_devnodes = devnodes
        if self.state == WAITING_INSERT:
            if devnodes:
                self._go_ready(devnodes[0])
        elif self.state == READY:
            if not devnodes:
                self._go_waiting_insert()
            elif (self.current_devnode, tuple(devnodes)) != self._ready_snapshot:
                cur = (self.current_devnode
                       if self.current_devnode in devnodes else devnodes[0])
                self._go_ready(cur)
        elif self.state == WAITING_REMOVAL:
            if self.current_devnode not in self.usb_devnodes:
                self._go_waiting_insert()

    def on_usb_removed(self, devnode):
        """Interrompt le test si la clé en cours de test est déconnectée."""
        if self.state != TESTING:
            return
        if devnode != self.current_devnode:
            return
        if self.worker is not None:
            self.worker.abort("Déconnexion USB détectée")

    def _device_label(self, info):
        """Ex: '/dev/sdb - SanDisk Ultra Fit - 31.95 GB'."""
        parts = [info["Devnode"]]
        name = " ".join(
            str(x).strip() for x in (info["Manufacturer"], info["Model"])
            if x and str(x).strip() != "N/A")
        if name:
            parts.append(name)
        if info["Size_GB"] != "N/A":
            parts.append(f"{info['Size_GB']} GB")
        return " - ".join(parts)

    def _go_ready(self, devnode):
        """Clé détectée : on affiche le device et on attend START."""
        self.state = READY
        self.current_devnode = devnode
        self.current_diskseq = get_diskseq(self.current_devnode)
        self._ready_snapshot = (devnode, tuple(self.usb_devnodes))
        info = get_device_info(devnode)
        label = self._device_label(info)
        single = len(self.usb_devnodes) == 1
        self.progress.setValue(0)
        self._set_banner(TXT_READY, COLORS["wait"])
        if single:
            self._set_user_status(label)
        else:
            others = ", ".join(self.usb_devnodes)
            self._set_user_status(f"{TXT_TOO_MANY}\n({others})")
        self._dev_status("clé détectée, en attente de START")
        self._render_info(format_device_info(info))
        self.start_button.setEnabled(single)

    def _go_waiting_insert(self):
        """Réinitialise la fenêtre en attente de l’insertion d’une clé."""
        self.state = WAITING_INSERT
        self.current_devnode = None
        self._ready_snapshot = None
        self.start_button.setEnabled(False)
        self.restart_button.hide()
        self.progress.setValue(0)
        self._set_banner(TXT_INSERT, COLORS["wait"])
        self._set_user_status("")
        self._dev_status("en attente d'une clé")
        if self.usb_devnodes:
            self._go_ready(self.usb_devnodes[0])

    def on_start_clicked(self):
        """Démarre le test si une seule clé valide est prête."""
        if (self.state != READY
                or len(self.usb_devnodes) != 1
                or self.current_devnode not in self.usb_devnodes):
            return
        self.start_button.setEnabled(False)
        self.start_test(self.current_devnode)

    def on_restart_clicked(self):
        """Réinitialise l’interface après un test interrompu."""
        if self.state != WAITING_RESTART:
            return
        self.restart_button.hide()
        self._go_waiting_insert()

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
        self.worker = TestWorker(self.current_devnode, get_diskseq(self.current_devnode))
        self.worker.status.connect(self._dev_status)
        self.worker.info_ready.connect(self._render_info)
        self.worker.sample.connect(self.on_sample)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.test_done.connect(self.on_test_done)
        # Important:
        self.worker.finished.connect(self.on_worker_finished)
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
        self._dev_status("Génération du rapport...")
        QTimer.singleShot(0, lambda: self._finalize(r, passed))

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
        if self.current_devnode not in self.usb_devnodes:
            QTimer.singleShot(1500, self._check_removed)

    def _write_pdf(self, r):
        """Enregistre le rapport PDF et signale les erreurs éventuelles."""
        path = r.get("pdf") or str(
            RESULTS_DIR / f"USB_report_{datetime.now():%Y%m%d_%H%M%S}.pdf")
        try:
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            write_pdf_report(path, r.get("report", ""), self.figure)
            self._dev_status(f"PDF enregistré : {path}")
        except Exception as e:
            self._dev_status(f"PDF échoué : {e}")

    def _check_removed(self):
        """Revient à l’attente si la clé testée a été retirée."""
        if (self.state == WAITING_REMOVAL
                and self.current_devnode not in self.usb_devnodes):
            self._go_waiting_insert()

    # --- Fermeture et arrêt des threads ---

    def closeEvent(self, event):
        """Arrête le minuteur et attend la fin des threads avant fermeture."""
        self.timer.stop()
        if self.watcher is not None:
            self.watcher.stop()
        if self.worker is not None and self.worker.isRunning():
            self.worker.abort()
            self.worker.wait()
        if self.watcher is not None and self.watcher.isRunning():
            self.watcher.wait()
        event.accept()