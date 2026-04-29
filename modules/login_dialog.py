"""
Login / connect dialog.
"""

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QPixmap
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QFormLayout, QCheckBox, QFrame,
)

from modules.jellyfin_api import get_api
from modules.settings import get_settings
from modules.ui_helpers import (
    GLOBAL_STYLE, ACCENT, ACCENT_DEEP, TEXT, TEXT_DIM, TEXT_FAINT, BG_PANEL,
    make_app_icon,
)


class _LoginWorker(QThread):
    finished_ok = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, server: str, user: str, pw: str):
        super().__init__()
        self.server = server
        self.user = user
        self.pw = pw

    def run(self):
        try:
            api = get_api()
            api.authenticate(self.server, self.user, self.pw)
            self.finished_ok.emit()
        except Exception as e:
            self.failed.emit(str(e))


class LoginDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect to Jellyfin")
        self.setFixedSize(440, 460)
        self.setStyleSheet(GLOBAL_STYLE + f"""
            QDialog {{ background: {BG_PANEL}; }}
        """)
        self._worker: _LoginWorker = None
        self._build()

        # Pre-fill from settings
        s = get_settings()
        if s.server_url:
            self.server_edit.setText(s.server_url)
        if s.username:
            self.user_edit.setText(s.username)

    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(36, 32, 36, 28)
        v.setSpacing(18)

        # Logo
        logo = QLabel()
        logo.setPixmap(make_app_icon(64))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(logo)

        # Title
        title = QLabel("JellyPlayer")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(f"color: {TEXT}; font-size: 22px; font-weight: 800;")
        v.addWidget(title)

        sub = QLabel("Connect to your Jellyfin server")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setStyleSheet(f"color: {TEXT_DIM}; font-size: 12px;")
        v.addWidget(sub)

        v.addSpacing(8)

        # Form
        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        self.server_edit = QLineEdit()
        self.server_edit.setPlaceholderText("http://192.168.1.100:8096")

        self.user_edit = QLineEdit()
        self.user_edit.setPlaceholderText("username")

        self.pass_edit = QLineEdit()
        self.pass_edit.setPlaceholderText("password")
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pass_edit.returnPressed.connect(self._connect)

        form.addRow("Server URL", self.server_edit)
        form.addRow("Username", self.user_edit)
        form.addRow("Password", self.pass_edit)
        v.addLayout(form)

        # Status
        self.status = QLabel("")
        self.status.setStyleSheet(f"color: #f87171; font-size: 11px;")
        self.status.setWordWrap(True)
        v.addWidget(self.status)

        v.addStretch()

        # Connect button
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("accent")
        self.connect_btn.setMinimumHeight(40)
        self.connect_btn.clicked.connect(self._connect)
        v.addWidget(self.connect_btn)

    def _connect(self):
        server = self.server_edit.text().strip()
        user = self.user_edit.text().strip()
        pw = self.pass_edit.text()

        if not server or not user:
            self.status.setText("Please fill in server and username.")
            return
        if not (server.startswith("http://") or server.startswith("https://")):
            server = "http://" + server
            self.server_edit.setText(server)

        self.status.setText("")
        self.connect_btn.setText("Connecting…")
        self.connect_btn.setEnabled(False)

        self._worker = _LoginWorker(server, user, pw)
        self._worker.finished_ok.connect(self._on_ok)
        self._worker.failed.connect(self._on_fail)
        self._worker.start()

    def _on_ok(self):
        self.accept()

    def _on_fail(self, msg: str):
        self.connect_btn.setText("Connect")
        self.connect_btn.setEnabled(True)
        self.status.setText(f"⚠ {msg}")
