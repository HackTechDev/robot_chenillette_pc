#!/usr/bin/env python3
"""BatMobile — télécommande PC (PyQt6) du robot Zumo, en Bluetooth classique (SPP/RFCOMM).

Même protocole que l'appli Android (../robot_chenillette_telecommande) : un caractère
envoyé à l'appui d'un bouton (ou d'une touche), un espace (stop) au relâchement,
aucune répétition pendant l'appui.
"""

import os
import re
import socket
import subprocess
import sys
import threading

from PyQt6.QtCore import QObject, QProcess, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

# Vitesse : chiffre '0'..'9' envoyé au robot → (niveau + 1) × SPEED_PER_LEVEL (40..400).
# Doit correspondre à SPEED_PER_LEVEL / SPEED_LEVEL_DEFAULT dans le firmware.
SPEED_LEVELS = 10
SPEED_PER_LEVEL = 40
SPEED_LEVEL_DEFAULT = 4

ROBOT_NAME = "BatMobile"
RFCOMM_CHANNEL = 1  # canal SPP du Bluetooth Bee
PAIRING_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bt_pairing.py")
STOP = " "

# Touches clavier → commande maintenue (Qt.Key ne dépend pas de la disposition AZERTY/QWERTY)
HOLD_KEYS = {
    k.value: cmd
    for k, cmd in (
        (Qt.Key.Key_Z, "z"), (Qt.Key.Key_Up, "z"),
        (Qt.Key.Key_S, "s"), (Qt.Key.Key_Down, "s"),
        (Qt.Key.Key_Q, "q"), (Qt.Key.Key_Left, "q"),
        (Qt.Key.Key_D, "d"), (Qt.Key.Key_Right, "d"),
        (Qt.Key.Key_V, "v"),
        (Qt.Key.Key_C, "c"),
        (Qt.Key.Key_W, "w"),
        (Qt.Key.Key_X, "x"),
    )
}

STYLE = """
QWidget { background: #111; color: #fff; }
QLabel#title { color: #FFD700; font-size: 28px; font-weight: bold; letter-spacing: 2px; }
QLabel#hint { color: #888; font-size: 11px; }
QLabel#speedValue { color: #FFD700; font-size: 13px; font-weight: bold; }
QComboBox { background: #2a2a2a; border: 1px solid #555; border-radius: 6px; padding: 6px; }
QPushButton#bt { border: 2px solid #555; border-radius: 18px; padding: 8px 24px; font-size: 15px; }
QPushButton#bt[connected="true"] { border-color: #4caf50; }
QPushButton#small { border: 1px solid #555; border-radius: 6px; padding: 6px 10px; }
QPushButton#quit { border: 1px solid #555; border-radius: 16px; color: #888; font-size: 15px; font-weight: bold; }
QPushButton#quit:hover { border-color: #e53935; color: #e53935; }
QMessageBox QPushButton { border: 1px solid #555; border-radius: 6px; padding: 6px 16px; min-width: 60px; }
QPushButton#dir { background: #FFD700; color: #111; border-radius: 14px; font-size: 30px; font-weight: bold; }
QPushButton#dir:pressed, QPushButton#dir[held="true"] { background: #c9a800; }
QPushButton#dir:disabled { background: #2a2a2a; color: #555; }
QSlider::groove:vertical { background: #2a2a2a; width: 28px; border-radius: 14px; }
QSlider::sub-page:vertical { background: #2a2a2a; border-radius: 14px; }
QSlider::add-page:vertical { background: #FFD700; border-radius: 14px; }
QSlider::handle:vertical { background: #fff; height: 14px; margin: 0 2px; border-radius: 7px; }
"""


def paired_devices():
    """Liste (adresse, nom) des appareils appairés, via bluetoothctl."""
    for args in (["bluetoothctl", "devices", "Paired"], ["bluetoothctl", "paired-devices"]):
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            return []
        devices = re.findall(r"^Device ([0-9A-F:]{17}) (.*)$", out, re.MULTILINE)
        if devices:
            return devices
    return []


class Link(QObject):
    """Liaison RFCOMM avec le robot. Les signaux sont émis depuis des threads de fond."""

    connected = pyqtSignal(str)
    failed = pyqtSignal(str)
    lost = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.sock = None
        self.lock = threading.Lock()

    def connect_to(self, address, first_cmd):
        threading.Thread(target=self._connect, args=(address, first_cmd), daemon=True).start()

    def _connect(self, address, first_cmd):
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        try:
            sock.connect((address, RFCOMM_CHANNEL))
            # Synchronise la vitesse du robot (réinitialisée à chaque démarrage)
            sock.sendall(first_cmd.encode())
        except OSError as e:
            sock.close()
            self.failed.emit(str(e))
            return
        with self.lock:
            self.sock = sock
        threading.Thread(target=self._watch, args=(sock,), daemon=True).start()
        self.connected.emit(address)

    def _watch(self, sock):
        # Le robot n'envoie rien : recv() ne revient qu'en cas de fermeture ou d'erreur de liaison
        try:
            while sock.recv(64):
                pass
        except OSError:
            pass
        with self.lock:
            if self.sock is not sock:
                return  # déconnexion volontaire
            self.sock = None
        sock.close()
        self.lost.emit()

    def send(self, cmd):
        with self.lock:
            sock = self.sock
        if sock is None:
            return
        try:
            sock.sendall(cmd.encode())
        except OSError:
            with self.lock:
                if self.sock is not sock:
                    return
                self.sock = None
            sock.close()
            self.lost.emit()

    def disconnect(self):
        with self.lock:
            sock, self.sock = self.sock, None
        if sock is None:
            return
        try:
            sock.sendall(STOP.encode())  # ne pas laisser le robot rouler
        except OSError:
            pass
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()


class Window(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(ROBOT_NAME)
        self.setStyleSheet(STYLE)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.link = Link()
        self.link.connected.connect(self.on_connected)
        self.link.failed.connect(self.on_failed)
        self.link.lost.connect(self.on_lost)
        self.is_connected = False
        self.connected_name = ""
        self.held_key = None
        self.pairing = None  # QProcess de bt_pairing.py en cours
        self.pairing_out = ""

        title = QLabel(ROBOT_NAME, objectName="title", alignment=Qt.AlignmentFlag.AlignCenter)

        # Choix de l'appareil
        self.device_box = QComboBox(editable=True)
        # Une adresse saisie reste du texte libre : sinon Entrée l'ajouterait comme élément
        # sans adresse (None), pris ensuite pour « appairage automatique »
        self.device_box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.device_box.setMinimumWidth(260)
        self.device_box.lineEdit().setPlaceholderText("Adresse (ex. 00:18:A1:12:16:C5)")
        refresh = QPushButton("⟳", objectName="small", toolTip="Rafraîchir les appareils appairés")
        refresh.clicked.connect(self.refresh_devices)
        self.bt_btn = QPushButton("Connecter", objectName="bt")
        self.bt_btn.setMinimumWidth(240)
        self.bt_btn.clicked.connect(self.toggle_connection)
        device_row = QHBoxLayout()
        device_row.addStretch()
        device_row.addWidget(self.device_box)
        device_row.addWidget(refresh)
        device_row.addStretch()
        bt_row = QHBoxLayout()
        bt_row.addStretch()
        bt_row.addWidget(self.bt_btn)
        bt_row.addStretch()
        self.status = QLabel(objectName="hint", alignment=Qt.AlignmentFlag.AlignCenter)

        # Vitesse (slider vertical, à gauche des déplacements) : haut = rapide
        self.speed = QSlider(Qt.Orientation.Vertical)
        self.speed.setRange(0, SPEED_LEVELS - 1)
        self.speed.setValue(SPEED_LEVEL_DEFAULT)
        self.speed.setPageStep(1)
        self.speed.valueChanged.connect(self.on_speed)
        self.speed_value = QLabel(objectName="speedValue", alignment=Qt.AlignmentFlag.AlignCenter)
        speed_col = QVBoxLayout()
        speed_col.addWidget(self.speed_value)
        speed_col.addWidget(self.speed, 1, Qt.AlignmentFlag.AlignHCenter)
        speed_col.addWidget(QLabel("Vit.", objectName="hint", alignment=Qt.AlignmentFlag.AlignCenter))
        self.update_speed_label()

        self.buttons = []
        dpad = QGridLayout()
        dpad.setSpacing(10)
        dpad.addWidget(self.dir_button("▲", "z"), 0, 1)
        dpad.addWidget(self.dir_button("↺", "q"), 1, 0)
        dpad.addWidget(self.dir_button("■", None), 1, 1)
        dpad.addWidget(self.dir_button("↻", "d"), 1, 2)
        dpad.addWidget(self.dir_button("▼", "s"), 2, 1)
        drive = QHBoxLayout()
        drive.addStretch()
        drive.addLayout(speed_col)
        drive.addSpacing(12)
        drive.addLayout(dpad)
        drive.addSpacing(52)  # contrepoids du slider pour garder le pad centré
        drive.addStretch()

        claw = QHBoxLayout()
        claw.addStretch()
        claw.addWidget(self.dir_button("🤏", "v"))
        claw.addWidget(self.dir_button("✋", "c"))
        claw.addStretch()

        head = QHBoxLayout()
        head.addStretch()
        head.addWidget(self.dir_button("Tête ▲", "w", font=18))
        head.addWidget(self.dir_button("Tête ▼", "x", font=18))
        head.addStretch()

        hint = QLabel(
            "Clavier : Z/S/Q/D ou flèches = déplacement · V/C = pince · W/X = tête · "
            "Espace = stop · 0–9 = vitesse",
            objectName="hint", alignment=Qt.AlignmentFlag.AlignCenter, wordWrap=True,
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 20, 16, 16)
        root.setSpacing(10)
        root.addWidget(title)
        root.addLayout(device_row)
        root.addLayout(bt_row)
        root.addWidget(self.status)
        root.addSpacing(20)
        root.addLayout(drive)
        root.addLayout(claw)
        root.addLayout(head)
        root.addStretch()

        # Bas de fenêtre : aide clavier + bouton « X » (quitter) en bas à droite
        quit_btn = QPushButton("✕", objectName="quit", toolTip="Quitter")
        quit_btn.setFixedSize(32, 32)
        quit_btn.clicked.connect(self.close)  # confirmation dans closeEvent
        bottom = QHBoxLayout()
        bottom.addSpacing(32)  # contrepoids du bouton pour garder l'aide centrée
        bottom.addWidget(hint, 1)
        bottom.addWidget(quit_btn, 0, Qt.AlignmentFlag.AlignBottom)
        root.addLayout(bottom)

        # Le clavier pilote le robot : aucun widget ne doit garder le focus (Espace activerait un bouton)
        for w in self.findChildren(QPushButton) + [self.speed]:
            w.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.device_box.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.device_box.lineEdit().returnPressed.connect(self.setFocus)

        self.refresh_devices()
        self.set_enabled(False)

    def dir_button(self, label, cmd, font=None):
        """Bouton maintenu : cmd à l'appui, stop au relâchement (cmd None = stop seul)."""
        b = QPushButton(label, objectName="dir")
        b.setFixedSize(90, 90)
        if font:
            b.setStyleSheet(f"font-size: {font}px;")
        if cmd is None:
            b.pressed.connect(lambda: self.link.send(STOP))
        else:
            b.pressed.connect(lambda: self.link.send(cmd))
            b.released.connect(lambda: self.link.send(STOP))
        b.cmd = cmd
        self.buttons.append(b)
        return b

    # --- Connexion ---

    def refresh_devices(self):
        current = self.device_box.currentText()
        self.device_box.clear()
        devices = paired_devices()
        for address, name in devices:
            self.device_box.addItem(f"{name} — {address}", address)
        robot = next((i for i, (_, n) in enumerate(devices) if n == ROBOT_NAME), None)
        if robot is not None:
            self.device_box.setCurrentIndex(robot)
        else:
            # Robot pas encore appairé : « Connecter » lancera l'appairage automatique (bt_pairing.py)
            self.device_box.insertItem(0, f"{ROBOT_NAME} — appairage automatique", None)
            self.device_box.setCurrentIndex(0)
            if current and current != self.device_box.itemText(0):
                self.device_box.setEditText(current)

    def selected_address(self):
        text = self.device_box.currentText()
        if self.device_box.currentIndex() >= 0 and text == self.device_box.itemText(self.device_box.currentIndex()):
            return self.device_box.currentData()
        m = re.search(r"([0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5})", text)
        return m.group(1).upper() if m else None

    def toggle_connection(self):
        if self.is_connected:
            self.link.disconnect()
            self.set_connected(False)
            return
        if self.device_box.currentIndex() >= 0 and self.device_box.currentData() is None \
                and self.device_box.currentText() == self.device_box.itemText(self.device_box.currentIndex()):
            self.start_pairing()
            return
        address = self.selected_address()
        if not address:
            QMessageBox.warning(self, "Appareil", "Choisir un appareil appairé ou saisir une adresse Bluetooth.")
            return
        self.connect_to(address)

    def connect_to(self, address):
        self.bt_btn.setText("Connexion…")
        self.bt_btn.setEnabled(False)
        self.device_box.setEnabled(False)
        self.status.setText("")
        self.link.connect_to(address, str(self.speed.value()))

    def start_pairing(self):
        """Lance bt_pairing.py (recherche, PIN, confiance) puis se connecte au robot."""
        self.bt_btn.setText("Appairage…")
        self.bt_btn.setEnabled(False)
        self.device_box.setEnabled(False)
        self.status.setText(f"Recherche de {ROBOT_NAME}…")
        self.pairing_out = ""
        self.pairing = QProcess(self)
        self.pairing.readyReadStandardOutput.connect(self.on_pairing_output)
        self.pairing.finished.connect(self.on_pairing_finished)
        self.pairing.start(sys.executable, [PAIRING_SCRIPT])

    def on_pairing_output(self):
        lines = bytes(self.pairing.readAllStandardOutput()).decode(errors="replace").split("\n")
        self.pairing_out += "\n".join(lines)
        last = [l for l in lines if l.strip() and not l.startswith("ADDRESS=")]
        if last:
            self.status.setText(last[-1])

    def on_pairing_finished(self, code, _status):
        self.on_pairing_output()  # reste éventuel de la sortie
        out = self.pairing_out
        err = bytes(self.pairing.readAllStandardError()).decode(errors="replace").strip()
        self.pairing = None
        m = re.search(r"^ADDRESS=(\S+)", out, re.MULTILINE)
        if code == 0 and m:
            self.refresh_devices()
            self.connect_to(m.group(1))
            return
        self.set_connected(False)
        self.status.setText("")
        QMessageBox.critical(self, "Appairage échoué", err or f"bt_pairing.py a échoué (code {code})")

    def on_connected(self, address):
        self.connected_name = self.device_box.currentText().split(" — ")[0] or address
        self.set_connected(True)

    def on_failed(self, error):
        self.set_connected(False)
        self.status.setText("")
        QMessageBox.critical(self, "Connexion échouée", error)

    def on_lost(self):
        self.set_connected(False)
        QMessageBox.warning(self, "Liaison perdue", "La connexion Bluetooth avec le robot a été perdue.")

    def set_connected(self, on):
        self.is_connected = on
        self.held_key = None
        self.bt_btn.setEnabled(True)
        self.bt_btn.setText(f"Connecté — {self.connected_name}" if on else "Connecter")
        self.bt_btn.setProperty("connected", on)
        self.bt_btn.style().polish(self.bt_btn)
        self.device_box.setEnabled(not on)
        self.set_enabled(on)
        self.setFocus()

    def set_enabled(self, on):
        for b in self.buttons:
            b.setEnabled(on)

    # --- Vitesse ---

    def update_speed_label(self):
        self.speed_value.setText(str((self.speed.value() + 1) * SPEED_PER_LEVEL))

    def on_speed(self, level):
        self.update_speed_label()
        self.link.send(str(level))

    # --- Clavier ---

    def set_held(self, cmd, held):
        for b in self.buttons:
            if b.cmd == cmd:
                b.setProperty("held", held)
                b.style().polish(b)

    def keyPressEvent(self, e):
        if e.isAutoRepeat() or not self.is_connected:
            return super().keyPressEvent(e)
        key = e.key()
        if key in HOLD_KEYS:
            self.held_key = key
            self.link.send(HOLD_KEYS[key])
            self.set_held(HOLD_KEYS[key], True)
        elif key == Qt.Key.Key_Space.value:
            self.link.send(STOP)
        elif Qt.Key.Key_0.value <= key <= Qt.Key.Key_9.value:
            self.speed.setValue(key - Qt.Key.Key_0.value)  # envoie le chiffre via on_speed
        else:
            super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        key = e.key()
        if e.isAutoRepeat() or key not in HOLD_KEYS:
            return super().keyReleaseEvent(e)
        self.set_held(HOLD_KEYS[key], False)
        # Stop uniquement si c'est la dernière touche pressée (relâcher une ancienne touche ne coupe pas)
        if key == self.held_key:
            self.held_key = None
            self.link.send(STOP)

    def focusOutEvent(self, e):
        # Fenêtre quittée touche enfoncée : on ne recevra pas le relâchement
        if self.held_key is not None:
            self.set_held(HOLD_KEYS[self.held_key], False)
            self.held_key = None
            self.link.send(STOP)
        super().focusOutEvent(e)

    def confirm_quit(self):
        box = QMessageBox(QMessageBox.Icon.Question, "Quitter", f"Quitter la télécommande {ROBOT_NAME} ?", parent=self)
        yes = box.addButton("Quitter", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(box.addButton("Annuler", QMessageBox.ButtonRole.RejectRole))
        box.exec()
        return box.clickedButton() is yes

    def closeEvent(self, e):
        # Bouton ✕ comme croix de la barre de titre : confirmation avant de quitter
        if not self.confirm_quit():
            e.ignore()
            return
        if self.pairing is not None:
            self.pairing.kill()
            self.pairing.waitForFinished(1000)
        self.link.disconnect()  # envoie un stop avant de fermer
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    w = Window()
    w.resize(480, 760)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
