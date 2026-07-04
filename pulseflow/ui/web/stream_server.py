"""
Battle Stream Server
====================

Server WebSocket ringan berbasis QtWebSockets (bawaan PyQt6 — tanpa dependensi
tambahan) yang menyiarkan Battlefield Object Model ke klien browser yang
menjalankan game Phaser 3.

Berjalan di thread GUI Qt. Diberi makan dari `PulseDashboard._on_metric_update`
yang sudah berjalan di thread Qt, jadi tidak ada masalah threading.
"""

import logging
from typing import List, Optional

from PyQt6.QtCore import QObject
from PyQt6.QtNetwork import QHostAddress
from PyQt6.QtWebSockets import QWebSocketServer, QWebSocket

logger = logging.getLogger("PulseFlow.BattleStream")


class BattleStreamServer(QObject):
    """Siarkan string JSON ke semua klien WebSocket yang terhubung."""

    def __init__(self, port: int = 8765, parent=None):
        super().__init__(parent)
        self.base_port = port
        self.port: Optional[int] = None
        self._clients: List[QWebSocket] = []
        self._last_message: Optional[str] = None

        self._server = QWebSocketServer(
            "PulseFlow Battlefield",
            QWebSocketServer.SslMode.NonSecureMode,
            self,
        )
        self._server.newConnection.connect(self._on_new_connection)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> Optional[int]:
        """Mulai listen (idempotent). Coba beberapa port bila bentrok."""
        if self._server.isListening():
            return self.port
        for port in range(self.base_port, self.base_port + 12):
            if self._server.listen(QHostAddress.SpecialAddress.LocalHost, port):
                self.port = port
                logger.info(f"Battle stream server listening on ws://localhost:{port}")
                return port
        logger.error("Battle stream server gagal listen pada semua port yang dicoba.")
        return None

    @property
    def is_listening(self) -> bool:
        return self._server.isListening()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def stop(self):
        for c in list(self._clients):
            c.close()
        self._clients.clear()
        if self._server.isListening():
            self._server.close()

    # ── Connections ───────────────────────────────────────────────────────────

    def _on_new_connection(self):
        sock = self._server.nextPendingConnection()
        if sock is None:
            return
        self._clients.append(sock)
        sock.disconnected.connect(lambda s=sock: self._on_disconnected(s))
        logger.info(f"Battlefield client connected ({len(self._clients)} total)")
        # Kirim snapshot terakhir agar game langsung terisi
        if self._last_message is not None:
            sock.sendTextMessage(self._last_message)

    def _on_disconnected(self, sock: QWebSocket):
        if sock in self._clients:
            self._clients.remove(sock)
        sock.deleteLater()
        logger.info(f"Battlefield client disconnected ({len(self._clients)} left)")

    # ── Broadcast ─────────────────────────────────────────────────────────────

    def broadcast(self, message: str):
        """Kirim string (JSON) ke semua klien. No-op bila tidak ada klien."""
        self._last_message = message
        if not self._clients:
            return
        for c in self._clients:
            if c.isValid():
                c.sendTextMessage(message)
