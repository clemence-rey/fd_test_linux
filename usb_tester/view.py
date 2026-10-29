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
    RESULTS_DIR,
    TEST_FILE_SIZE_MB,
    BLOCK_SIZE_MB,
    MIN_WRITE_MBPS,
    MIN_READ_MBPS,
    TEMPLATE_PATH,
    STATION_NAME,
    SOFTWARE_VERSION,
    TEST_DURATION_MODE,
    TEST_DURATION,
    TEST_CYCLES,
    REFORMAT_FS,
    TEST_LINK_REFS,
    MAX_MATING_CYCLES,
)

from controller import (
    TestWorker,
    UsbWatcher,
    VbusWorker
)

from model import (
    get_diskseq,
    get_device_info,
    format_device_info,
    assert_same_device,
    assert_test_port,
    write_docx_report,
    increment_test_link_counter,
    load_test_link_counters,
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
# -- Titre de la fenètre de test
TXT_WINDOW_TITLE = "EOL test flash drive"

# -- Texte de la banière
TXT_INSERT = "Insert flash drive"
TXT_READY = "Flash drive detected"
TXT_TESTING = "In progress..."
TXT_PASS = "PASS"
TXT_FAIL = "FAIL"
TXT_INTERRUPT = "TEST INTERRUPTED"

# -- texte de statut
TXT_DO_NOT_UNPLUG = "Do not unplug"
TXT_REMOVE = "Remove flash drive"

# -- texte boutons
TXT_BUTTON_START = "START"
TXT_BUTTON_NEW_TEST = "NEW TEST"
TXT_BUTTON_CANCEL_TEST = "CANCEL TEST"

# -- taille des textes
SIZE_TXT_BANNER = 44
SIZE_TXT_STATUS = 11
SIZE_TXT_DEV_STATUS = 10
SIZE_TXT_BUTTON = 16
SIZE_TXT_INFO = 9


# ============================================================
# 2. TEST CONFIGURATION
# ============================================================

class TestSetupDialog(QDialog):
    """Configure l'OF, le SAP et le test link au lancement."""

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
            self.update_test_link
        )

        self.of_input.textChanged.connect(
            self.update_validation
        )

        # --- Initialisation ---

        self.update_test_link()
        self.update_validation()

    def update_test_link(self):
        """Affiche le test link associé au SAP sélectionné."""

        sap = self.sap_combo.currentText()

        self.test_link_label.setText(
            TEST_LINK_REFS.get(sap, "N/A")
        )

    def update_validation(self):
        """Autorise le démarrage uniquement si l'OF est renseigné."""

        valid = bool(self.of_input.text().strip())

        self.buttons.button(
            QDialogButtonBox.Ok
        ).setEnabled(valid)

    def get_configuration(self):
        """Retourne les paramètres sélectionnés par l'opérateur."""

        sap = self.sap_combo.currentText()

        return {
            "of": self.of_input.text().strip(),
            "sap": sap,
            "test_link": TEST_LINK_REFS[sap],
        }


# ============================================================
# 2. FENÊTRE PRINCIPALE
# ============================================================

class MainWindow(QMainWindow):
    """Gère la GUI, les événements USB et l'affichage des résultats."""

    # --- Initialisation de l'interface ---

    def __init__(self, test_config):
        """Initialise la fenêtre, ses états et les threads de surveillance."""
        super().__init__()

        self.test_config = test_config
        self.setWindowTitle(TXT_WINDOW_TITLE)
        self.setWindowState(self.windowState() | Qt.WindowMaximized)

        # Périphérique USB et état de l'application.
        self.current_devnode = None
        self.current_diskseq = None
        self.connected_devnode = None
        self.connected_diskseq = None
        self._device_removed = False
        self._new_device_ready = False
        self.state = WAITING_INSERT
        self._waiting_for_removal = False
        self.current_info = ""

        # Gestion des threads.
        self.worker = None
        self._closing = False
        self.vbus_worker = None
        self.vbus_measurements = []

        # Initialisation de l'interface.
        self.graph_dirty = False
        self._reset_series()
        self._build_ui()
        self._set_banner(TXT_INSERT, COLORS["wait"])
        self._set_user_status("")
        self._render_info("")

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
        self.start_button = QPushButton(TXT_BUTTON_START)
        self.start_button.setFont(QFont("Sans", SIZE_TXT_BUTTON, QFont.Bold))
        self.start_button.setMinimumHeight(SIZE_TXT_BUTTON + 20)
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self.on_start_clicked)

        # --- Bouton NOUVEAU TEST ---
        self.restart_button = QPushButton(TXT_BUTTON_NEW_TEST)
        self.restart_button.setFont(QFont("Sans", SIZE_TXT_BUTTON, QFont.Bold))
        self.restart_button.setMinimumHeight(SIZE_TXT_BUTTON + 20)
        self.restart_button.clicked.connect(self.on_restart_clicked)

        # --- Bouton ANNULER TEST ---
        self.cancel_button = QPushButton(TXT_BUTTON_CANCEL_TEST)
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

        # Live VBUS measurements
        self.vbus_label = QLabel("VBUS: N/A")
        self.vbus_label.setAlignment(Qt.AlignCenter)
        root.addWidget(self.vbus_label)

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
                raise RuntimeError("USB ID not available")

            actual_diskseq = get_diskseq(devnode)

            if actual_diskseq is None or actual_diskseq != diskseq:
                raise RuntimeError("The USB flash drive changed while it was being detected")

            info = get_device_info(devnode)

            # Vérifie que la clé n'a pas changé pendant la lecture des informations.
            assert_same_device(devnode, diskseq)

        except (OSError, RuntimeError) as exc:
            self._go_waiting_insert()
            self._set_steps_status(f"USB detection failed : {exc}")
            return

        self.state = READY
        self.current_devnode = devnode
        self.current_diskseq = actual_diskseq

        self.progress.setValue(0)
        self._set_banner(TXT_READY, COLORS["wait"])
        self._set_user_status(self._get_device_label(info))
        self._set_steps_status("Flash drive detected, waiting for START")
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
            self._set_steps_status(f"Invalid device: {exc}")
            return

        self.start_button.setEnabled(False)

        try:
            self.start_test(self.current_devnode)

        except (OSError, RuntimeError) as exc:
            self._go_waiting_insert()
            self._set_steps_status(f"Unable to start: {exc}")


    def on_cancel_clicked(self):
        """Demande l'annulation volontaire du test USB."""

        if self.state != TESTING:
            return

        if self.worker is None or not self.worker.isRunning():
            return

        # Empêcher les demandes d'annulation répétées.
        self.cancel_button.setEnabled(False)

        # Informer l'opérateur.
        self._set_banner("Cancellation in progress...", COLORS["test"])
        self._set_user_status("Do not unplug the flash drive.")
        self._set_steps_status("Stopping test operations...")

        # Demander l'arrêt du worker.
        self.worker.cancel()


    def on_restart_clicked(self):
        """Réinitialise l'interface et attend une nouvelle insertion USB."""

        if self.state != WAITING_RESTART:
            return

        if self.worker is not None and self.worker.isRunning():
            return

        # Check whether a remove/add sequence occurred.
        device_ready = (
            self._new_device_ready
            and self.connected_devnode is not None
            and self.connected_diskseq is not None
        )

        devnode = self.connected_devnode
        diskseq = self.connected_diskseq

        # Reset the GUI and detection flags.
        self._go_waiting_insert()

        self._device_removed = False
        self._new_device_ready = False

        if device_ready:
            self._go_ready(devnode, diskseq)
        elif devnode is not None:
            self._waiting_for_removal = True

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
                    raise RuntimeError("Unable to recover the DiskSeq")

            except (OSError, RuntimeError) as exc:
                self._set_steps_status(f"Device unavailable : {exc}")
                return

            # Identifier une nouvelle détection USB.
            is_new_device = (
                self.connected_devnode,
                self.connected_diskseq,
            ) != (devnode, diskseq)

            self.connected_devnode = devnode
            self.connected_diskseq = diskseq

            if self.state == WAITING_RESTART and self._device_removed:
                self._new_device_ready = True

            # Compter les insertions indépendamment de l'état de la GUI.
            if is_new_device:
                try:
                    test_link = self.test_config["test_link"]
                    increment_test_link_counter(test_link)
                    self._render_info(self.current_info)

                except (OSError, ValueError, TypeError) as exc:
                    self._set_steps_status(f"Counter error : {exc}")

            # Préparer un test uniquement lorsque la GUI le permet.
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

            if self.state == WAITING_RESTART:
                self._device_removed = True
                self._new_device_ready = False

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

        self._device_removed = False
        self._new_device_ready = False

        # Vérification du périphérique avant le test destructif.
        if self.current_diskseq is None:
            raise RuntimeError("Flash drive identity not initialized")

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

        # Start VBUS monitoring
        self.vbus_measurements = []

        self.vbus_worker = VbusWorker()
        self.vbus_worker.measurement.connect(self.on_vbus_measurement)
        self.vbus_worker.error.connect(self.on_vbus_error)
        self.vbus_worker.start()

        # Start USB performance test
        self.worker.start()


    def on_test_done(self, r):
        """Traite la fin du test ou son annulation."""

        if self.vbus_worker is not None:
            self.vbus_worker.stop()
            self.vbus_worker.wait()

        stats = self.compute_vbus_stats()

        if stats:
            print("VBUS statistics:", stats)

        if r["result"] == "CANCELLED":
            self._go_waiting_insert()
            return

        if r["result"] == "CANCEL_FAILED":
            self.state = WAITING_RESTART

            self._set_banner(
                "Cancellation failed",
                COLORS["fail"]
            )

            self._set_user_status(
                "The flash drive could not be restored."
            )

            self._set_steps_status(r.get("error", "Unknown error"))

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

        # Affichage du rapport complet.
        passed = r["result"] == "PASS"
        self._render_info(r.get("report") or self.current_info)

        # Génération du docx à la prochaine itération de la boucle Qt.
        self._set_steps_status("Generating report...")
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
        self.dev_status_label.setText(f"Step : {text}")


    def _render_info(self, text):
        """Affiche les informations du test link et du test courant."""

        test_link = self.test_config["test_link"]
        counters = load_test_link_counters()

        link_data = counters.get(test_link, {})
        cycles = link_data.get("cycles", 0)
        max_cycles = link_data.get("max_cycles", MAX_MATING_CYCLES)

        header = (
            "=== TEST-LINK ===\n"
            f"Reference : {test_link}\n"
            f"Mating cycle : {cycles} / {max_cycles}"
        )

        self.current_info = text
        self.info_text.setPlainText(f"{header}\n\n{text}")


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

    def on_vbus_measurement(self, voltage, current):
        """Receive and store VBUS measurements."""
        self.vbus_measurements.append((voltage, current))

        self.vbus_label.setText(
            f"VBUS: {voltage:.3f} V | {current:.2f} mA"
        )


    def on_vbus_error(self, message):
        """Display Acroname monitoring errors."""
        self.vbus_label.setText(f"VBUS ERROR: {message}")

    # --- Finalisation et génération des rapports ---

    def _display_final_result(self, r, passed):
        """Enregistre le rapport et affiche le verdict final."""

        report_saved = self._write_docx(r)

        self.progress.setValue(100)
        self.state = WAITING_RESTART

        self._set_banner(
            TXT_PASS if passed else TXT_FAIL,
            COLORS["pass"] if passed else COLORS["fail"],
        )

        if report_saved:
            self._set_user_status("")
        else:
            self._set_user_status(
                "WARNING: The report has not been saved."
            )

        self.start_button.setEnabled(False)
        self.button_stack.setCurrentWidget(self.restart_button)


    def _write_docx(self, r):
        """Enregistre le rapport DOCX contenant les résultats et le graphique."""

        path = r.get("docx") or str(
            RESULTS_DIR / f"USB_report_{datetime.now():%Y%m%d_%H%M%S}.docx"
        )

        try:
            write_docx_report(
                template_path=TEMPLATE_PATH,
                output_path=path,
                report_text=r.get("report", ""),
                figure=self.figure,
                of_number=self.test_config["of"],
                sap=self.test_config["sap"],
                device=r.get("model", "N/A"),
                serial=r.get("serial", "N/A"),
                file_size=f"{TEST_FILE_SIZE_MB} MB",
                block_size=f"{BLOCK_SIZE_MB} MB",
                test_duration=(
                    f"{TEST_CYCLES} cycles"
                    if TEST_DURATION_MODE == "cycles"
                    else f"{TEST_DURATION:g} s"
                ),
                final_fs=REFORMAT_FS.upper(),
                station=STATION_NAME,
                soft_version=SOFTWARE_VERSION,
            )

        except Exception as exc:
            self._set_steps_status(f"Report failed: {exc}")
            return False

        self._set_steps_status(f"Report saved: {path}")
        return True


    def closeEvent(self, event):
        """Arrête les threads avant de fermer l'application."""

        self._closing = True

        if self.vbus_worker is not None and self.vbus_worker.isRunning():
            self.vbus_worker.stop()
            self.vbus_worker.wait()

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


    def compute_vbus_stats(self):
        """Compute minimum, maximum and average VBUS measurements."""

        if not self.vbus_measurements:
            return None

        voltages = [v for v, i in self.vbus_measurements]
        currents = [i for v, i in self.vbus_measurements]

        return {
            "voltage_min": min(voltages),
            "voltage_max": max(voltages),
            "voltage_avg": sum(voltages) / len(voltages),
            "current_min": min(currents),
            "current_max": max(currents),
            "current_avg": sum(currents) / len(currents),
        }