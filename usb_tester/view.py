"""Interface graphique du testeur USB : affichage, suivi du test et rapports."""

import numpy as np
import logging
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
    QStackedWidget,
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
# 1. CONSTANTES DE L'INTERFACE
# ============================================================

WAITING_INSERT, READY, TESTING, WAITING_RESTART = range(4)

COLORS = {
    "wait": "#1f4e79",
    "test": "#b26a00",
    "pass": "#1e7e34",
    "fail": "#b02a37",
}

TXT_INSERT = "Insérez une clé USB"
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
    """Gère la GUI, les événements USB et l'affichage des résultats."""

    # --- Initialisation de l'interface ---

    def __init__(self):
        """Initialise la fenêtre, ses états et les threads de surveillance."""
        super().__init__()

        self.setWindowTitle("EOL test flash drive")
        self.setWindowState(self.windowState() | Qt.WindowMaximized)

        # Périphérique USB et état de l'application.
        self.current_devnode = None
        self.current_diskseq = None
        self.connected_devnode = None
        self.connected_diskseq = None
        self.state = WAITING_INSERT
        self._waiting_for_removal = False

        # Gestion des threads.
        self.worker = None
        self._closing = False

        # Initialisation de l'interface.
        self.graph_dirty = False
        self._reset_series()
        self._build_ui()
        self._set_banner(TXT_INSERT, COLORS["wait"])
        self._set_user_status("")

        # Rafraîchissement périodique du graphique.
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh_graph)
        self.timer.start(300)

        # Surveillance des périphériques USB.
        self.watcher = UsbWatcher()
        self.watcher.finished.connect(self._on_thread_finished)
        self.watcher.usb_event.connect(self.on_usb_event)
        self.watcher.start()


    def _build_ui(self):
        """Construit et organise les widgets de l'interface graphique."""

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # --- Bannière principale ---
        self.banner = QLabel()
        self.banner.setAlignment(Qt.AlignCenter)
        self.banner.setMinimumHeight(SIZE_TXT_BANNER + 20)
        root.addWidget(self.banner)

        # --- Statut utilisateur ---
        self.status_label = QLabel("")
        self.status_label.setFont(QFont("Sans", SIZE_TXT_STATUS))
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        # --- Bouton START ---
        self.start_button = QPushButton(TXT_START)
        self.start_button.setFont(QFont("Sans", SIZE_TXT_BUTTON, QFont.Bold))
        self.start_button.setMinimumHeight(SIZE_TXT_BUTTON + 20)
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self.on_start_clicked)

        # --- Bouton NOUVEAU TEST ---
        self.restart_button = QPushButton("NOUVEAU TEST")
        self.restart_button.setFont(QFont("Sans", SIZE_TXT_BUTTON, QFont.Bold))
        self.restart_button.setMinimumHeight(SIZE_TXT_BUTTON + 20)
        self.restart_button.clicked.connect(self.on_restart_clicked)

        # --- Bouton ANNULER TEST ---
        self.cancel_button = QPushButton("ANNULER LE TEST")
        self.cancel_button.setFont(QFont("Sans", SIZE_TXT_BUTTON, QFont.Bold))
        self.cancel_button.setMinimumHeight(SIZE_TXT_BUTTON + 20)
        self.cancel_button.clicked.connect(self.on_cancel_clicked)

        # Un seul bouton visible à la fois.
        self.button_stack = QStackedWidget()
        self.button_stack.addWidget(self.start_button)
        self.button_stack.addWidget(self.restart_button)
        self.button_stack.addWidget(self.cancel_button)

        # START est affiché par défaut.
        self.button_stack.setCurrentWidget(self.start_button)

        root.addWidget(self.button_stack)

        # --- Barre de progression ---
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        root.addWidget(self.progress)

        # --- Statut détaillé du test ---
        self.dev_status_label = QLabel("")
        self.dev_status_label.setFont(QFont("Sans", SIZE_TXT_DEV_STATUS))
        self.dev_status_label.setAlignment(Qt.AlignCenter)
        root.addWidget(self.dev_status_label)

        # --- Informations du périphérique ---
        self.info_text = QPlainTextEdit()
        self.info_text.setReadOnly(True)
        self.info_text.setFont(QFont("Monospace", SIZE_TXT_INFO))
        self.info_text.setLineWrapMode(QPlainTextEdit.NoWrap)

        # --- Graphique des performances ---
        self.figure = Figure(figsize=(8, 5))
        self.canvas = FigureCanvas(self.figure)
        self.ax = self.figure.add_subplot(111)
        self._init_axes()

        # --- Organisation des informations et du graphique ---
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.info_text)
        splitter.addWidget(self.canvas)
        splitter.setSizes([300, 750])
        root.addWidget(splitter, 1)

    # --- Gestion des états de l'interface ---

    def _go_ready(self, devnode, diskseq):
        """Vérifie la clé détectée et prépare l'interface pour le test."""

        try:
            assert_test_port(devnode)

            if diskseq is None:
                raise RuntimeError("Identité USB non disponible")

            actual_diskseq = get_diskseq(devnode)

            if actual_diskseq is None or actual_diskseq != diskseq:
                raise RuntimeError("La clé a changé pendant sa détection")

            info = get_device_info(devnode)

            # Vérifie que la clé n'a pas changé pendant la lecture des informations.
            assert_same_device(devnode, diskseq)

        except (OSError, RuntimeError) as exc:
            self._go_waiting_insert()
            self._set_steps_status(f"Détection USB échouée : {exc}")
            return

        self.state = READY
        self.current_devnode = devnode
        self.current_diskseq = actual_diskseq

        self.progress.setValue(0)
        self._set_banner(TXT_READY, COLORS["wait"])
        self._set_user_status(self._get_device_label(info))
        self._set_steps_status("clé détectée, en attente de START")
        self._render_info(format_device_info(info))

        self.start_button.setEnabled(True)


    def _go_waiting_insert(self):
        """Réinitialise l'interface et attend une nouvelle insertion USB."""

        self.state = WAITING_INSERT

        # Réinitialisation du périphérique associé au test.
        self.current_devnode = None
        self.current_diskseq = None

        # Réinitialisation de l'affichage.
        self._reset_view()

        # Configuration de l'interface en attente d'insertion.
        self.start_button.setEnabled(False)
        self.button_stack.setCurrentWidget(self.start_button)

        self._set_banner(TXT_INSERT, COLORS["wait"])
        self._set_user_status("")

    # --- Gestion des événements utilisateur ---

    def on_start_clicked(self):
        """Démarre le test uniquement si le périphérique est toujours valide."""

        if self.state != READY or self.current_devnode is None:
            return

        try:
            assert_test_port(self.current_devnode)
            assert_same_device(self.current_devnode, self.current_diskseq)

        except (OSError, RuntimeError) as exc:
            self._go_waiting_insert()
            self._set_steps_status(f"Périphérique invalide : {exc}")
            return

        self.start_button.setEnabled(False)

        try:
            self.start_test(self.current_devnode)

        except (OSError, RuntimeError) as exc:
            self._go_waiting_insert()
            self._set_steps_status(f"Démarrage impossible : {exc}")


    def on_cancel_clicked(self):
        """Demande l'annulation volontaire du test USB."""

        if self.state != TESTING:
            return

        if self.worker is None or not self.worker.isRunning():
            return

        # Empêcher les demandes d'annulation répétées.
        self.cancel_button.setEnabled(False)

        # Informer l'opérateur.
        self._set_banner("Annulation en cours...", COLORS["test"])
        self._set_user_status("Ne pas retirer la clé USB.")
        self._set_steps_status("Arrêt des opérations USB...")

        # Demander l'arrêt du worker.
        self.worker.cancel()


    def on_restart_clicked(self):
        """Réinitialise l'interface et attend une nouvelle insertion USB."""

        if self.state != WAITING_RESTART:
            return

        if self.worker is not None and self.worker.isRunning():
            return

        # Si une clé est encore connectée, attendre son retrait.
        self._waiting_for_removal = self.connected_devnode is not None

        self._go_waiting_insert()

        if self._waiting_for_removal:
            self._set_user_status("")

    # --- Surveillance des périphériques USB ---

    def on_usb_event(self, devnode, event):
        """Traite les connexions et déconnexions du port réservé."""
        

        if event == "inserted":  
            try:
                assert_test_port(devnode)
                diskseq = get_diskseq(devnode)

                if self._waiting_for_removal:
                    return

                if diskseq is None:
                    raise RuntimeError("Impossible de récupérer le diskseq USB")

            except (OSError, RuntimeError) as exc:
                self._set_steps_status(f"USB non disponible : {exc}")
                return

            self.connected_devnode = devnode
            self.connected_diskseq = diskseq

            if self.state in (WAITING_INSERT, READY):
                if (self.current_devnode, self.current_diskseq) != (devnode, diskseq):
                    self._go_ready(devnode, diskseq)

        elif event == "removed":
            if devnode != self.connected_devnode:
                return

            self.connected_devnode = None
            self.connected_diskseq = None
            self._waiting_for_removal = False

            if self.state == TESTING:
                if self.worker is not None and self.worker.isRunning():
                    self.worker.abort()

            elif self.state == READY:
                self._go_waiting_insert()

            # En WAITING_RESTART, conserver le rapport affiché.


    def _get_device_label(self, info):
        """Construit le libellé du périphérique à partir du fabricant et du modèle."""
        manufacturer = str(info.get("Manufacturer") or "N/A").strip()
        model = str(info.get("Model") or "N/A").strip()

        return " - ".join(
            x for x in (manufacturer, model) if x and x != "N/A"
        )

    # --- Gestion du test et des threads ---

    def start_test(self, devnode):
        """Prépare l'interface et démarre le thread de test USB."""

        # Vérification du périphérique avant le test destructif.
        if self.current_diskseq is None:
            raise RuntimeError("Identité USB non initialisée")

        assert_test_port(devnode)
        assert_same_device(devnode, self.current_diskseq)

        # Passage à l'état TESTING.
        self.state = TESTING
        self.current_devnode = devnode

        # Réinitialisation de l'interface.
        self._reset_view()

        self._set_banner(TXT_TESTING, COLORS["test"])
        self._set_user_status(TXT_DO_NOT_UNPLUG)
        self.button_stack.setCurrentWidget(self.cancel_button)
        self.cancel_button.setEnabled(True)

        # Création du thread de test.
        self.worker = TestWorker(self.current_devnode, self.current_diskseq)

        # Communication entre le worker et l'interface.
        self.worker.status.connect(self._set_steps_status)
        self.worker.info_ready.connect(self._render_info)
        self.worker.sample.connect(self.on_sample)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.test_done.connect(self.on_test_done)

        # Gestion de la terminaison du thread.
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.finished.connect(self._on_thread_finished)

        # Démarrage du test.
        self.worker.start()


    def on_test_done(self, r):
        """Traite la fin du test ou son annulation."""

        if r["result"] == "CANCELLED":
            self._go_waiting_insert()
            return

        if r["result"] == "CANCEL_FAILED":
            self.state = WAITING_RESTART

            self._set_banner(
                "ANNULATION ÉCHOUÉE",
                COLORS["fail"]
            )

            self._set_user_status(
                "La clé n'a pas pu être restaurée."
            )

            self._set_steps_status(r.get("error", "Erreur inconnue"))

            self.button_stack.setCurrentWidget(self.restart_button)
            return
    
        # Actualisation du graphique avec les dernières mesures.
        self._refresh_graph()

        # Ajout des vitesses moyennes.
        if r.get("avg_write") is not None:
            self.ax.axhline(
                r["avg_write"],
                linestyle="--",
                alpha=0.5,
                color="C0",
                label=f"Average Write {r['avg_write']:.1f} MB/s"
            )

        if r.get("avg_read") is not None:
            self.ax.axhline(
                r["avg_read"],
                linestyle="--",
                alpha=0.5,
                color="C1",
                label=f"Average Read {r['avg_read']:.1f} MB/s"
            )

        self.ax.legend(loc="lower left")
        self.canvas.draw()

        # Enregistrement du graphique PNG.
        if r.get("graph"):
            try:
                self.figure.savefig(r["graph"], dpi=200)
            except Exception:
                logging.exception("Échec de l'enregistrement du graphique PNG")

        # Affichage du rapport complet.
        passed = r["result"] == "PASS"
        self._render_info(r.get("report") or self.current_info)

        # Génération du PDF à la prochaine itération de la boucle Qt.
        self._set_steps_status("Génération du rapport...")
        QTimer.singleShot(0, lambda: self._display_final_result(r, passed))


    def on_worker_finished(self):
        """Planifie la destruction du worker après la fin du thread."""
        worker = self.sender()

        if worker is not None:
            worker.deleteLater()

        if self.worker is worker:
            self.worker = None


    def _on_thread_finished(self):
        """Réessaie de fermer la fenêtre après l'arrêt d'un thread."""
        if self._closing:
            QTimer.singleShot(0, self.close)

    # --- Affichage des statuts et informations ---

    def _set_banner(self, title, color):
        """Actualise le texte et la couleur de la bannière principale."""
        self.banner.setText(
            f"<div style='font-size:{SIZE_TXT_BANNER}px;"
            f"font-weight:bold'>{title}</div>"
        )
        self.banner.setStyleSheet(
            f"background-color:{color};color:white;border-radius:10px;"
        )


    def _set_user_status(self, text):
        """Actualise le message affiché sous la bannière."""
        self.status_label.setText(text)


    def _set_steps_status(self, text):
        """Affiche l'étape en cours du test."""
        self.dev_status_label.setText(f"Étape : {text}")


    def _render_info(self, text):
        """Affiche les informations du test courant."""
        self.current_info = text
        self.info_text.setPlainText(text)


    def _reset_view(self):
        """Efface les informations et réinitialise le graphique et la progression."""

        # Informations du test précédent.
        self._render_info("")
        self._set_steps_status("")

        # Graphique des performances.
        self._reset_series()
        self.graph_dirty = False
        self._init_axes()

        # Barre de progression.
        self.progress.setValue(0)

    # --- Gestion du graphique ---

    def _reset_series(self):
        """Réinitialise les séries de mesures affichées sur le graphique."""
        self.wt, self.ws, self.rt, self.rs = [], [], [], []


    def _init_axes(self):
        """Configure les axes, courbes et seuils du graphique."""
        self.ax.clear()
        self.ax.set_xlabel("Test Time [min]")
        self.ax.set_ylabel("Speed [MB/s]")
        self.ax.set_title(
            f"USB Read / Write Speed ({TEST_FILE_SIZE_MB} MB test, "
            f"{BLOCK_SIZE_MB} MB blocks)"
        )
        self.ax.grid(True, alpha=0.3)
        # Mesures
        (self.line_w,) = self.ax.plot(
            [], [],linewidth=1,label="Write Speed"
            )
        (self.line_r,) = self.ax.plot(
            [], [],linewidth=1,label="Read Speed"
            )
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


    def on_sample(self, op, t, mbps):
        """Ajoute les mesures d'écriture et de lecture aux séries du graphique."""

        if op == "SEP":
            nan = float("nan")
            self.wt.append(nan)
            self.ws.append(nan)
            self.rt.append(nan)
            self.rs.append(nan)

        elif op == "WRITE":
            self.wt.append(t)
            self.ws.append(mbps)

        elif op == "READ":
            self.rt.append(t)
            self.rs.append(mbps)

        else:
            return

        self.graph_dirty = True

    # --- Finalisation et génération des rapports ---

    def _display_final_result(self, r, passed):
        """Génère le rapport et affiche le verdict final."""

        self._write_pdf(r)
        self.progress.setValue(100)

        # Tous les tests terminent dans le même état.
        self.state = WAITING_RESTART

        # Affichage du verdict.
        self._set_banner(
            TXT_PASS if passed else TXT_FAIL,
            COLORS["pass"] if passed else COLORS["fail"]
        )

        # Instructions à l'opérateur.
        self._set_user_status("")

        # Désactivation du démarrage et affichage du bouton Nouveau test.
        self.start_button.setEnabled(False)
        self.button_stack.setCurrentWidget(self.restart_button)


    def _write_pdf(self, r):
        """Enregistre le rapport PDF avec un graphique de dimensions fixes."""

        path = r.get("pdf") or str(
            RESULTS_DIR / f"USB_report_{datetime.now():%Y%m%d_%H%M%S}.pdf"
        )

        original_size = self.figure.get_size_inches().copy()

        try:
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)

            # Dimensions fixes du graphique pour le rapport PDF.
            self.figure.set_size_inches(8, 5, forward=False)
            self.figure.tight_layout()

            write_pdf_report(path, r.get("report", ""), self.figure)

            self._set_steps_status(f"PDF enregistré : {path}")

        except Exception as exc:
            self._set_steps_status(f"PDF échoué : {exc}")

        finally:
            # Restauration des dimensions du graphique dans la GUI.
            self.figure.set_size_inches(original_size, forward=False)
            self.figure.tight_layout()
            self.canvas.draw_idle()


    def closeEvent(self, event):
        """Arrête les threads avant de fermer l'application."""

        self._closing = True

        # Arrêt du rafraîchissement graphique.
        self.timer.stop()

        # Interruption du test USB en cours.
        if self.worker is not None and self.worker.isRunning():
            self.worker.abort()
            event.ignore()
            return

        # Arrêt de la surveillance USB.
        if self.watcher is not None and self.watcher.isRunning():
            self.watcher.stop()
            event.ignore()
            return

        # Fermeture lorsque tous les threads sont arrêtés.
        event.accept()