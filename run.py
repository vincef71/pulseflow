import sys
import argparse
import logging
import threading
from pathlib import Path

# Console Windows default cp1252 tidak bisa meng-encode emoji di log/traceback
# ("Error in sys.excepthook" tanpa isi) — paksa UTF-8 supaya exception asli
# terlihat utuh.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from PyQt6.QtWidgets import QApplication, QDialog
from pulseflow.ui.dashboard import PulseDashboard, StartupConfigDialog

# Setup logging: console + file UTF-8 (pulseflow.log) supaya traceback
# tertangkap utuh sekalipun console gagal meng-encode.
_LOG_FILE = Path(__file__).resolve().parent / "pulseflow.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_LOG_FILE, encoding="utf-8"),
    ]
)
logger = logging.getLogger("PulseFlow.Launcher")

# ── Penangkap exception yang tidak bisa gagal ─────────────────────────
# Excepthook default menulis langsung ke stderr dan diam-diam gagal
# ("Error in sys.excepthook:" kosong). Ganti dengan hook yang menulis
# lewat logging (console + file) dan menelan error internalnya sendiri.

def _log_uncaught(exc_type, exc_value, exc_tb):
    try:
        logger.critical("UNCAUGHT EXCEPTION",
                        exc_info=(exc_type, exc_value, exc_tb))
    except Exception:
        pass

def _log_thread_uncaught(args):
    try:
        logger.critical("UNCAUGHT EXCEPTION di thread %r",
                        getattr(args.thread, "name", "?"),
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    except Exception:
        pass

def _log_unraisable(unraisable):
    try:
        logger.error("UNRAISABLE EXCEPTION di %r: %s: %s",
                     unraisable.object,
                     getattr(unraisable.exc_type, "__name__", "?"),
                     unraisable.exc_value)
    except Exception:
        pass

sys.excepthook = _log_uncaught
threading.excepthook = _log_thread_uncaught
sys.unraisablehook = _log_unraisable


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion') # Professional look

    # Prompt configuration popup
    dialog = StartupConfigDialog()
    if dialog.exec() == QDialog.DialogCode.Accepted:
        mode, symbols = dialog.get_config()
        logger.info(f"Launching PulseFlow. Connection: {mode.upper()}, Tracking: {symbols}")

        dashboard = PulseDashboard(engine_mode=mode, symbols=symbols)
        dashboard.show()

        logger.info("PulseFlow GUI loaded. Starting event loops.")
        sys.exit(app.exec())
    else:
        logger.info("Configuration cancelled. Exiting.")
        sys.exit(0)

if __name__ == "__main__":
    main()
