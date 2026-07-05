import queue
import threading
import asyncio
import json
import webbrowser
from pathlib import Path
from urllib.parse import urlencode
from PyQt6.QtWidgets import QMainWindow, QWidget, QGridLayout, QHBoxLayout, QVBoxLayout, QLabel, QFrame, QPushButton, QDialog, QComboBox, QLineEdit, QFormLayout, QSplitter, QMenuBar, QMenu, QTabWidget, QMessageBox
from PyQt6.QtGui import QAction
from PyQt6.QtCore import QTimer, pyqtSignal, QObject, Qt
from pulseflow.ui.styles import QSS_STYLE, COLORS
from pulseflow.ui.components.chart import MarketChart
from pulseflow.ui.components.footprint import FootprintWindow
from pulseflow.ui.components.velocity_meter import VelocityMeter
from pulseflow.ui.components.event_feed import EventFeed
from pulseflow.ui.components.scanner import SymbolScanner
from pulseflow.ui.components.alerts import AlertPanel
from pulseflow.ui.components.connection_log import ConnectionLogPanel
from pulseflow.ui.components.flow_panel import FlowPanel
from pulseflow.ui.components.battlefield import BattlefieldPanel
from pulseflow.ui.components.state_card import MarketStateCard
from pulseflow.ui.components.entry_card import EntrySignalCard
from pulseflow.ui.components.alert_log import AlertLogWindow
from pulseflow.ui.components.heatmap import OrderBookHeatmap
from pulseflow.ui.components.liquidity_ladder import LiquidityLadder
from pulseflow.ui.web.stream_server import BattleStreamServer
from pulseflow.core.engine import PulseEngine
from pulseflow.trading.executor import TradeExecutor

# Append custom QSplitter style to QSS
COMPLETED_QSS = QSS_STYLE + """
QSplitter::handle {
    background-color: #1a1a24;
}
QSplitter::handle:horizontal {
    width: 6px;
}
QSplitter::handle:vertical {
    height: 6px;
}
QMenuBar {
    background-color: #0b0b0d;
    color: #e3e3e7;
    border-bottom: 1px solid #1a1a24;
    padding: 2px 0;
    font-size: 12px;
}
QMenuBar::item {
    padding: 4px 12px;
    border-radius: 3px;
}
QMenuBar::item:selected {
    background-color: #1c1c22;
    color: #00ffd2;
}
QMenu {
    background-color: #121216;
    color: #e3e3e7;
    border: 1px solid #2d2d38;
    padding: 4px 0;
}
QMenu::item {
    padding: 6px 24px 6px 16px;
    font-size: 12px;
}
QMenu::item:selected {
    background-color: #1c1c22;
    color: #00ffd2;
}
QMenu::separator {
    height: 1px;
    background-color: #23232a;
    margin: 4px 0;
}
"""

class EngineSignals(QObject):
    """Qt Signals to transfer data from the background Asyncio thread to the Main GUI thread."""
    metric_update      = pyqtSignal(str, dict, list)   # (symbol, metrics, signals)
    liquidation_update = pyqtSignal(str, float, str)   # (symbol, usd_val, side)
    feed_status_update = pyqtSignal(str, str, str, str) # (symbol, feed_name, status, message)
    depth_update       = pyqtSignal(str, object, object, float)  # (symbol, bids, asks, ts)
    trade_print        = pyqtSignal(str, float, float, bool)     # (symbol, price, size, buyer_maker)


class StartupConfigDialog(QDialog):
    """Sleek dark themed dialog allowing user selection of Exchange Connection and Symbols."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("PulseFlow | Ingestion Config")
        self.resize(480, 260)
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {COLORS["bg_dark"]};
                border: 1px solid #23232a;
            }}
            QLabel {{
                color: {COLORS["text_main"]};
                font-family: 'Inter', sans-serif;
                font-weight: bold;
                font-size: 12px;
            }}
            QLineEdit, QComboBox {{
                background-color: #14141a;
                color: {COLORS["text_main"]};
                border: 1px solid #2d2d38;
                padding: 8px;
                border-radius: 4px;
                font-family: monospace;
                font-size: 13px;
            }}
            QLineEdit:focus, QComboBox:focus {{
                border: 1px solid {COLORS["accent"]};
            }}
        """)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(25, 25, 25, 25)
        layout.setSpacing(18)

        title = QLabel("PULSEFLOW micro-structure CONFIG", self)
        title.setStyleSheet(f"font-size: 15px; color: {COLORS['accent']}; border-bottom: 1px solid #23232a; padding-bottom: 8px; font-weight: bold; letter-spacing: 1px;")
        layout.addWidget(title)

        form = QFormLayout()
        form.setSpacing(12)

        self.conn_box = QComboBox(self)
        self.conn_box.addItem("Live Hyperliquid (Official SDK)", "hyperliquid")
        self.conn_box.addItem("Live Binance Futures (Websockets)", "binance")
        self.conn_box.addItem("High-Fidelity Market Simulator", "simulated")
        form.addRow("EXCHANGE SOURCE:", self.conn_box)

        self.symbols_input = QLineEdit(self)
        self.symbols_input.setText("BTC, ETH, SOL, HYPE")
        form.addRow("TRACK SYMBOLS:", self.symbols_input)

        layout.addLayout(form)
        layout.addSpacing(5)

        self.btn_boot = QPushButton("BOOT micro-structure TERMINAL", self)
        self.btn_boot.setStyleSheet(f"""
            QPushButton {{
                background-color: #171722;
                border: 1px solid #333344;
                color: {COLORS["text_main"]};
                padding: 10px;
                border-radius: 4px;
                font-weight: bold;
                font-size: 12px;
            }}
            QPushButton:hover {{
                background-color: {COLORS["bg_hover"]};
                border: 1px solid {COLORS["accent"]};
                color: {COLORS["accent"]};
            }}
        """)
        self.btn_boot.clicked.connect(self.accept)
        layout.addWidget(self.btn_boot)

    def get_config(self):
        mode = self.conn_box.currentData()
        syms_text = self.symbols_input.text()
        # Preserve case for HIP-3 / DEX coins (contain ':'), uppercase standard perps
        symbols = [s.strip() if ':' in s else s.strip().upper() for s in syms_text.split(",") if s.strip()]
        if not symbols:
            symbols = ["BTC", "ETH", "SOL", "HYPE"]
        return mode, symbols


class PulseDashboard(QMainWindow):
    """
    Main dark institutional trading terminal window for PulseFlow.
    """
    # Hasil worker eksekusi order (network thread → GUI thread)
    exec_summary_ready = pyqtSignal(dict)
    exec_done = pyqtSignal(dict)
    exec_failed = pyqtSignal(str)
    exec_skipped = pyqtSignal(str)   # auto-skip: melepas flag busy pipeline eksekusi
    exec_note = pyqtSignal(str)      # info lepas (paper close dll.) — status bar saja

    def __init__(self, engine_mode: str = "simulated", symbols: list = None):
        super().__init__()
        self.setWindowTitle("PulseFlow | Market Microstructure Analytics Engine")
        self.resize(1400, 850)
        self.setStyleSheet(COMPLETED_QSS)
        
        self.engine_mode = engine_mode
        self.symbols = symbols or ["BTC", "ETH", "SOL", "HYPE"]
        self.current_focus_symbol = self.symbols[0]
        self.signals = EngineSignals()
        self.footprint_windows: list[FootprintWindow] = []

        # Thread-safe queue for liquidations
        self.liq_queue = queue.Queue()

        # WebSocket server untuk game Phaser (browser) — start saat dibuka
        self.battle_stream = BattleStreamServer(port=8765)

        # Trade executor (PAPER default via .env; LIVE = PAPER_MODE=false)
        self.executor = TradeExecutor()
        self._exec_busy = False
        # Auto-trade: sekali konfirmasi → semua fire dieksekusi otomatis
        # sampai dimatikan user / error / ganti symbol fokus
        self.auto_trade = False
        self._exec_was_auto = False

        self._init_ui()
        self._start_backend()

    def _create_menu_bar(self):
        mb = self.menuBar()
        view_menu = mb.addMenu("View")

        # Default = mode SIMPLE (chart + entry signal saja). Mode Pro
        # menampilkan kembali semua panel analitik untuk debugging/kalibrasi.
        self.act_pro = QAction("Mode Pro — semua panel analitik", self)
        self.act_pro.setCheckable(True)
        self.act_pro.setChecked(False)
        self.act_pro.setShortcut("Ctrl+P")
        self.act_pro.setStatusTip("Tampilkan panel analitik lengkap (battlefield, heatmap, flow, dll.)")
        self.act_pro.toggled.connect(self._set_pro_mode)
        view_menu.addAction(self.act_pro)
        view_menu.addSeparator()

        act_footprint = QAction("Open Footprint Chart Window", self)
        act_footprint.setShortcut("Ctrl+F")
        act_footprint.setStatusTip("Buka window chart footprint terpisah untuk symbol aktif")
        act_footprint.triggered.connect(self._open_footprint_window)
        view_menu.addAction(act_footprint)

        act_battle_game = QAction("Open Battlefield Game (Phaser) ⚔", self)
        act_battle_game.setShortcut("Ctrl+B")
        act_battle_game.setStatusTip("Buka visualisasi battlefield game-like Phaser 3 di browser")
        act_battle_game.triggered.connect(self._open_battlefield_game)
        view_menu.addAction(act_battle_game)

        act_alert_log = QAction("Open Alert Log 🔔", self)
        act_alert_log.setShortcut("Ctrl+L")
        act_alert_log.setStatusTip("Buka jendela log price alert")
        act_alert_log.triggered.connect(lambda: self.alert_log.show_raise())
        view_menu.addAction(act_alert_log)

    def _open_battlefield_game(self):
        """Start WS server (bila perlu) lalu buka game Phaser di browser default."""
        port = self.battle_stream.start()
        if port is None:
            return
        html = Path(__file__).resolve().parent / "web" / "battlefield_game.html"
        query = urlencode({"port": port, "symbol": self.current_focus_symbol})
        webbrowser.open(html.as_uri() + "?" + query)

    def _open_footprint_window(self):
        win = FootprintWindow(self.current_focus_symbol, self.engine, parent=None)
        win.destroyed.connect(lambda: self._cleanup_footprint_window(win))
        self.footprint_windows.append(win)
        win.show()

    def _cleanup_footprint_window(self, win: "FootprintWindow"):
        if win in self.footprint_windows:
            self.footprint_windows.remove(win)

    def _init_ui(self):
        self._create_menu_bar()
        # Central widget containing the main horizontal splitter
        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)
        
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(0)
        
        # Main dynamic splitter dividing scanner from center graphics
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        
        # Left Panel (Scanner)
        self.scanner = SymbolScanner(self)
        self.scanner.symbol_selected.connect(self._change_focus_symbol)
        self.main_splitter.addWidget(self.scanner)
        
        # Right container consisting of vertical sections (Top and Bottom)
        self.right_vertical_splitter = QSplitter(Qt.Orientation.Vertical, self)
        
        # Top Splitter (main view tabs | right panel)
        self.top_horizontal_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.chart = MarketChart(self)
        self.chart.set_symbol(self.current_focus_symbol)
        self.chart.sigAlertTriggered.connect(self._on_alert_triggered)
        self.battlefield = BattlefieldPanel(self)
        self.battlefield.set_symbol(self.current_focus_symbol)
        self.heatmap = OrderBookHeatmap(self)
        self.heatmap.set_symbol(self.current_focus_symbol)
        self.liquidity_ladder = LiquidityLadder(self)
        self.liquidity_ladder.set_symbol(self.current_focus_symbol)

        # Price-alert log window (non-modal, opened from View menu / on trigger)
        self.alert_log = AlertLogWindow(self)

        # Tab widget: Battlefield (default) + classic OrderFlow chart
        self.main_view_tabs = QTabWidget(self)
        self.main_view_tabs.setStyleSheet("""
            QTabWidget::pane { border: 1px solid #23232a; border-radius: 8px; top: -1px; }
            QTabBar::tab {
                background: #14141a; color: #7d7d8e;
                padding: 6px 18px; margin-right: 2px;
                border: 1px solid #23232a; border-bottom: none;
                border-top-left-radius: 6px; border-top-right-radius: 6px;
                font-weight: bold; font-size: 12px;
            }
            QTabBar::tab:selected { background: #1c1c22; color: #00ffd2; }
            QTabBar::tab:hover { color: #e3e3e7; }
        """)
        self.main_view_tabs.addTab(self.battlefield, "⚔ BATTLEFIELD")
        self.main_view_tabs.addTab(self.chart, "OrderFlow Chart")
        self.main_view_tabs.addTab(self.heatmap, "🌊 HEATMAP")
        self.main_view_tabs.addTab(self.liquidity_ladder, "🎯 LIQUIDITY MAP")

        # Right side of top: Entry Signal (hero) → state card → meter → flow
        self.right_top_splitter = QSplitter(Qt.Orientation.Vertical, self)
        self.entry_card = EntrySignalCard(self)
        self.state_card = MarketStateCard(self)
        self.meter      = VelocityMeter(self)
        self.flow_panel = FlowPanel(self)
        self.right_top_splitter.addWidget(self.entry_card)
        self.right_top_splitter.addWidget(self.state_card)
        self.right_top_splitter.addWidget(self.meter)
        self.right_top_splitter.addWidget(self.flow_panel)
        self.right_top_splitter.setSizes([300, 180, 180, 150])

        self.top_horizontal_splitter.addWidget(self.main_view_tabs)
        self.top_horizontal_splitter.addWidget(self.right_top_splitter)

        # Set chart vs right panel initial ratio
        self.top_horizontal_splitter.setSizes([920, 320])
        
        # Bottom Splitter (Event Feed + Alerts)
        self.bottom_horizontal_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.event_feed = EventFeed(self)
        self.alerts = AlertPanel(self)

        self.bottom_horizontal_splitter.addWidget(self.event_feed)
        self.bottom_horizontal_splitter.addWidget(self.alerts)

        # Set event feed vs alerts initial ratio
        self.bottom_horizontal_splitter.setSizes([520, 750])

        # Connection Log Panel (full-width, below tape+alerts)
        self.connection_log = ConnectionLogPanel(self)

        # Add to the right vertical splitter
        self.right_vertical_splitter.addWidget(self.top_horizontal_splitter)
        self.right_vertical_splitter.addWidget(self.bottom_horizontal_splitter)
        self.right_vertical_splitter.addWidget(self.connection_log)
        self.right_vertical_splitter.setSizes([460, 280, 150])
        
        # Add to the main splitter
        self.main_splitter.addWidget(self.right_vertical_splitter)
        self.main_splitter.setSizes([380, 1020])
        
        main_layout.addWidget(self.main_splitter)
        
        # Connect Qt signals to slots
        self.signals.metric_update.connect(self._on_metric_update)
        self.signals.liquidation_update.connect(self._on_liquidation_update)
        self.signals.feed_status_update.connect(self._on_feed_status_update)
        self.signals.depth_update.connect(self._on_depth_update)
        self.signals.trade_print.connect(self._on_trade_print)

        # Alur eksekusi order: tombol card → siapkan → konfirmasi → eksekusi
        self.entry_card.set_exec_mode("PAPER" if self.executor.paper_mode else "LIVE")
        self.entry_card.execute_requested.connect(self._on_execute_requested)
        self.entry_card.auto_toggle_requested.connect(self._on_auto_toggle)
        self.exec_summary_ready.connect(self._on_exec_summary)
        self.exec_done.connect(self._on_exec_done)
        self.exec_failed.connect(self._on_exec_failed)
        self.exec_skipped.connect(self._on_exec_skipped)
        self.exec_note.connect(
            lambda msg: self.statusBar().showMessage(msg, 10000))

        # Boot dalam mode SIMPLE: chart + entry signal saja
        self.pro_mode = False
        self._apply_view_mode()

    # ── Mode tampilan (SIMPLE default / Pro via View menu, Ctrl+P) ─────

    def _set_pro_mode(self, on: bool):
        self.pro_mode = bool(on)
        self._apply_view_mode()

    def _apply_view_mode(self):
        """SIMPLE: hanya chart (dengan overlay plan) + kartu 🎯 ENTRY SIGNAL +
        scanner ramping — semua panel analitik lain disembunyikan supaya
        keputusan entry tidak tenggelam. Pro: tampilkan semuanya."""
        pro = self.pro_mode

        # Kolom kanan: entry card selalu tampil, sisanya hanya di Pro
        self.state_card.setVisible(pro)
        self.meter.setVisible(pro)
        self.flow_panel.setVisible(pro)

        # Baris bawah + log koneksi hanya di Pro
        self.event_feed.setVisible(pro)
        self.alerts.setVisible(pro)
        self.connection_log.setVisible(pro)

        # Scanner: SIMPLE = kolom SYMBOL · PRICE · SIGNAL saja
        self.scanner.set_simple_mode(not pro)

        # Tab utama: SIMPLE = chart saja, tanpa tab bar
        tabs = self.main_view_tabs
        for i in range(tabs.count()):
            tabs.setTabVisible(i, pro or tabs.widget(i) is self.chart)
        tabs.tabBar().setVisible(pro)

        if pro:
            self.right_vertical_splitter.setSizes([460, 280, 150])
            self.main_splitter.setSizes([380, 1020])
            self.right_top_splitter.setSizes([300, 180, 180, 150])
        else:
            tabs.setCurrentWidget(self.chart)
            self.right_vertical_splitter.setSizes([900, 0, 0])
            self.main_splitter.setSizes([230, 1170])
            self.right_top_splitter.setSizes([900, 0, 0, 0])

    def _start_backend(self):
        # Create and start core engine in a dedicated background asyncio thread
        self.engine = PulseEngine(mode=self.engine_mode, symbols=self.symbols)
        
        # Register engine callbacks
        self.engine.register_ui_callback(self._engine_ui_callback)
        self.engine.register_signal_callback(self._engine_signal_callback)
        self.engine.register_feed_status_callback(self._engine_feed_status_callback)

        # Order book depth + raw trades → Qt thread for the liquidity heatmap
        self.engine.register_depth_callback(
            lambda sym, b, a, t: self.signals.depth_update.emit(sym, b, a, t)
        )
        self.engine.register_raw_trade_callback(
            lambda sym, p, s, bm: self.signals.trade_print.emit(sym, p, s, bm)
        )

        # Connect the Binance liquidation callback to the tape directly via thread-safe queue/signals
        # Since liquidation callbacks occur inside feed handlers, we push to Qt thread
        for symbol in self.engine.symbols:
            # We wrap the on_liquidation callbacks to emit a Qt signal
            orig_liq_cb = self.engine.tickers[symbol].add_liquidation
            
            def make_liq_hook(sym=symbol):
                return lambda val, side, ts: self._on_raw_liquidation_hook(sym, val, side)
            
            # Re-bind ticker callback
            self.engine.tickers[symbol].add_liquidation = lambda usd_val, side, sym=symbol: self._on_raw_liquidation_hook(sym, usd_val, side)

        # Thread runner
        self.backend_thread = threading.Thread(target=self._run_async_loop, daemon=True)
        self.backend_thread.start()

    def _run_async_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        
        # Schedule engine startup on the loop once run_forever starts running
        self.loop.call_soon(self.engine.start)
        
        # Run loop forever
        self.loop.run_forever()

    def _engine_ui_callback(self, symbol: str, metrics: dict, signals: list):
        # Transfer data to GUI thread using safe Qt signal invocation
        self.signals.metric_update.emit(symbol, metrics, signals)

    def _engine_signal_callback(self, symbol: str, signal: dict):
        pass  # Evaluated inside metric_update for sync reasons

    def _engine_feed_status_callback(self, symbol: str, feed_name: str, status, message: str):
        # Emit to Qt thread — status.value converts enum to string
        status_str = status.value if hasattr(status, "value") else str(status)
        self.signals.feed_status_update.emit(symbol, feed_name, status_str, message)

    def _on_raw_liquidation_hook(self, symbol: str, usd_value: float, side: str):
        # Put in safe queue and emit Qt signal
        self.signals.liquidation_update.emit(symbol, usd_value, side)

    def _on_metric_update(self, symbol: str, metrics: dict, signals: list):
        # Real traded price — 0.0 until the first trade arrives for this symbol
        real_price = self.engine.tickers[symbol].last_price
        # Display fallback (for the scanner only) so it doesn't show 0 pre-trade
        display_price = real_price if real_price > 0 else (95000.0 if symbol == "BTC" else 100.0)

        self.scanner.update_symbol_metrics(symbol, display_price, metrics)

        # If this is the active focused symbol, update main dashboard charts/meters
        if symbol == self.current_focus_symbol:
            # Pass the real price — the chart waits for the first valid tick
            self.chart.update_data(real_price, metrics)
            entry = metrics.get("entry")
            self.entry_card.update_entry(entry)
            # Setup entry baru menyala → ping supaya tidak terlewat
            if entry and entry.get("new_fire"):
                self._play_ping()
                # Auto-trade: eksekusi langsung tanpa konfirmasi per-order
                if self.auto_trade and not self._exec_busy:
                    self._auto_execute(symbol, entry)
            self.state_card.update_state(metrics.get("battle"))
            self.event_feed.consume(symbol, metrics.get("battle"))
            self.meter.update_metrics(metrics)
            self.flow_panel.update_metrics(metrics)

            # Panel berat hanya dihitung di mode Pro (di SIMPLE tersembunyi)
            if self.pro_mode:
                self.battlefield.update_battle(metrics.get("battle"))

                # Liquidity probability: ladder tab + heatmap overlay
                liq = metrics.get("liquidity")
                self.liquidity_ladder.update_levels(liq)
                self.liquidity_ladder.update_macro(metrics.get("macro_liquidity"))
                self.heatmap.update_liquidity(liq)

            # Siarkan ke game Phaser (browser) bila ada klien terhubung
            if self.battle_stream.is_listening and self.battle_stream.client_count > 0:
                battle = metrics.get("battle")
                if battle:
                    battle["symbol"] = symbol
                    self.battle_stream.broadcast(json.dumps(battle))

        # Setup berakhir → sinkronkan posisi (semua symbol):
        # paper = tutup + catat PnL net fee; live = tutup posisi milik sesi
        # ini + batalkan order sisa (strategi teruji = exit saat setup mati)
        ent = metrics.get("entry")
        if ent and ent.get("status") in ("STOP", "TP2", "FLIP", "FADED", "TRAIL"):
            self._on_setup_end(symbol, ent)
        elif ent and ent.get("status") == "PARTIAL":
            self._on_partial(symbol, ent)

        # Update connection log trade stats
        count, last_time = self.engine.get_feed_stats(symbol)
        self.connection_log.update_trade_stats(symbol, count, last_time)

        # Append signals to alert log
        for sig in signals:
            self.alerts.add_signal(symbol, sig)

    def _on_depth_update(self, symbol: str, bids: list, asks: list, ts: float):
        if self.pro_mode and symbol == self.current_focus_symbol:
            self.heatmap.update_depth(bids, asks, ts)

    def _on_trade_print(self, symbol: str, price: float, size: float, is_buyer_maker: bool):
        if self.pro_mode and symbol == self.current_focus_symbol:
            self.heatmap.add_trade(price, size, is_buyer_maker)

    def _on_feed_status_update(self, symbol: str, feed_name: str, status_str: str, message: str):
        self.connection_log.on_feed_status(symbol, feed_name, status_str, message)
        # Mode SIMPLE menyembunyikan connection log — status feed tetap
        # terlihat ringkas di status bar bawah
        self.statusBar().showMessage(
            f"{symbol} · {feed_name}: {status_str} — {message}", 15000)

    def _on_liquidation_update(self, symbol: str, usd_value: float, side: str):
        # Curated liquidation surfacing is handled by the Event Feed via the
        # Battle State Engine (cascade-gated). Here we only record the forced
        # order into the ticker system.
        self.engine.tickers[symbol].add_liquidation(usd_value, side)

    def _on_alert_triggered(self, symbol: str, level: float, direction: str, price: float):
        """A price-alert line was crossed: ping + log + surface in the feed."""
        self._play_ping()
        self.alert_log.log(symbol, level, direction, price)
        if not self.alert_log.isVisible():
            self.alert_log.show()          # auto-open once, without stealing focus
        try:
            self.event_feed.push_alert(symbol, level, direction)
        except Exception:
            pass

    # ── Eksekusi order (tombol 🚀 di entry card) ───────────────────────

    def _on_auto_toggle(self, on: bool):
        """Toggle auto-trade: konfirmasi SEKALI saat mengaktifkan; setelah itu
        setiap fire dieksekusi tanpa dialog sampai dimatikan."""
        if not on:
            self.auto_trade = False
            self.entry_card.set_auto_state(False)
            self.statusBar().showMessage("🤖 AUTO TRADE dimatikan", 8000)
            return

        live = not self.executor.paper_mode
        mode = "🔴 LIVE — UANG NYATA!" if live else "📄 PAPER (simulasi)"
        text = (
            f"Mode        : {mode}\n"
            f"Symbol      : {self.current_focus_symbol} (symbol fokus saat ini)\n"
            f"Risk/trade  : {self.executor.risk_pct:.1f}% balance · "
            f"leverage {self.executor.leverage}x\n\n"
            "Setelah dikonfirmasi, SETIAP sinyal entry ACTIVE baru akan\n"
            "dieksekusi OTOMATIS (entry + SL + TP) tanpa konfirmasi per order,\n"
            "sampai kamu mematikan AUTO TRADE.\n\n"
            "Pengaman:\n"
            "• Satu posisi per symbol — sinyal saat posisi terbuka dilewati\n"
            "• Eksekusi error → AUTO TRADE mati otomatis\n"
            "• Ganti symbol fokus → AUTO TRADE mati otomatis\n\n"
            "Aktifkan AUTO TRADE?")
        title = "KONFIRMASI AUTO TRADE — LIVE" if live else "Konfirmasi Auto Trade (Paper)"
        ans = QMessageBox.question(
            self, title, text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans == QMessageBox.StandardButton.Yes:
            self.auto_trade = True
            self.entry_card.set_auto_state(True)
            self.statusBar().showMessage(
                f"🤖 AUTO TRADE AKTIF ({'LIVE' if live else 'PAPER'}) — "
                f"{self.current_focus_symbol}", 15000)
        else:
            self.entry_card.set_auto_state(False)

    def _auto_execute(self, symbol: str, entry: dict):
        """Eksekusi fire otomatis: tanpa dialog. Guard: satu posisi/symbol."""
        plan = entry.get("plan")
        if not plan:
            return
        self._exec_busy = True
        self._exec_was_auto = True

        def work():
            try:
                if self.executor.has_open_position(symbol):
                    self.exec_skipped.emit(
                        f"🤖 AUTO: posisi {symbol} masih terbuka — sinyal dilewati")
                    return
                prepared = self.executor.prepare_order(symbol, plan)
                res = self.executor.execute(
                    prepared,
                    {k: entry.get(k) for k in ("setup", "score", "grade")})
                res["auto"] = True
                res["summary"] = (f"{prepared['side']} {prepared['symbol']} "
                                  f"qty {prepared['quantity']} @ ~{prepared['entry']:,.6g} "
                                  f"(SL {prepared['stop']:,.6g} · TP1 {prepared['tp1']:,.6g})")
                self.exec_done.emit(res)
            except Exception as e:
                self.exec_failed.emit(str(e))
        threading.Thread(target=work, daemon=True, name="exec-auto").start()

    def _on_partial(self, symbol: str, entry: dict):
        """Profit ≥ 0.5R → tutup 50% posisi + SL ke breakeven (paper & live;
        live hanya posisi milik sesi ini). Sisa posisi di-trail engine."""
        plan = entry.get("plan") or {}
        price = float(entry.get("price", 0.0))
        be_stop = float(plan.get("stop", 0.0))   # engine sudah set = entry
        if price <= 0 or be_stop <= 0:
            return
        if not self.executor.paper_mode and not self.executor.is_tracked_live(symbol):
            return

        def work():
            try:
                res = self.executor.partial_close(symbol, price, be_stop)
                if res.get("ok"):
                    self.exec_note.emit(
                        f"🎯 PARTIAL {symbol}: 50% ditutup @ ~{price:,.6g} — "
                        f"SL → BE, sisa trailing")
                else:
                    self.exec_note.emit(
                        f"⚠ PARTIAL {symbol} GAGAL: {res.get('error', '?')} — "
                        f"{res.get('failsafe', '')}")
            except Exception as e:
                self.exec_note.emit(f"⚠ PARTIAL {symbol} error: {e}")
        threading.Thread(target=work, daemon=True, name="partial").start()

    def _on_setup_end(self, symbol: str, entry: dict):
        """Setup berakhir (STOP/TP2/FLIP/FADED) → tutup posisi symbol ini.
        Paper: tutup + PnL net fee. Live: hanya posisi yang dibuka sesi ini
        (tracked) — tutup market + batalkan SL/TP sisa."""
        reason = entry.get("status", "")
        price = float(entry.get("price", 0.0))

        if self.executor.paper_mode:
            def work():
                try:
                    n = self.executor.close_paper_trades(symbol, price, reason)
                    if n:
                        self.exec_note.emit(
                            f"📄 Paper close {symbol}: {n} posisi ({reason})")
                except Exception as e:
                    self.exec_note.emit(f"Paper close {symbol} gagal: {e}")
        else:
            if not self.executor.is_tracked_live(symbol):
                return
            def work():
                try:
                    res = self.executor.close_live_trade(symbol, reason)
                    self.exec_note.emit(
                        f"🔴 LIVE exit {symbol} ({reason}): {res['note']}")
                except Exception as e:
                    self.exec_note.emit(
                        f"⚠ LIVE exit {symbol} GAGAL: {e} — CEK POSISI MANUAL!")
        threading.Thread(target=work, daemon=True, name="setup-end").start()

    def _on_exec_skipped(self, msg: str):
        self._exec_busy = False
        self._exec_was_auto = False
        self.statusBar().showMessage(msg, 10000)

    def _disarm_auto(self, why: str):
        if self.auto_trade:
            self.auto_trade = False
            self.entry_card.set_auto_state(False)
            self.statusBar().showMessage(f"🤖 AUTO TRADE dimatikan — {why}", 15000)

    def _on_execute_requested(self, entry: dict):
        """Siapkan order di worker thread (hitung size dari balance & plan)."""
        plan = entry.get("plan")
        if self._exec_busy or not plan:
            return
        self._exec_busy = True
        self._exec_was_auto = False
        # Konteks setup untuk jurnal (dipakai saat eksekusi setelah konfirmasi)
        self._exec_context = {k: entry.get(k) for k in ("setup", "score", "grade")}
        self.entry_card.exec_btn.setEnabled(False)
        symbol = self.current_focus_symbol
        self.statusBar().showMessage(f"Menyiapkan order {symbol}…")

        def work():
            try:
                prepared = self.executor.prepare_order(symbol, plan)
                self.exec_summary_ready.emit(prepared)
            except Exception as e:
                self.exec_failed.emit(str(e))
        threading.Thread(target=work, daemon=True, name="exec-prepare").start()

    def _on_exec_summary(self, p: dict):
        """Dialog konfirmasi WAJIB — terutama untuk LIVE (uang nyata)."""
        live = p["mode"] == "LIVE"
        lines = [
            f"Mode       : {'🔴 LIVE — UANG NYATA!' if live else '📄 PAPER (simulasi)'}",
            f"Symbol     : {p['symbol']}",
            f"Arah       : {p['side']}",
            "—" * 28,
            f"Entry      : {p['entry']:,.6g}  (market)",
            f"Stop Loss  : {p['stop']:,.6g}",
            f"TP1        : {p['tp1']:,.6g}   (R {p['rr1']:.2f})",
            "—" * 28,
            f"Quantity   : {p['quantity']}",
            f"Notional   : ${p['notional_usdt']:,.2f}",
            f"Margin     : ${p['margin_usdt']:,.2f}  ({p['leverage']}x)",
            f"Risk/Trade : ${p['risk_usdt']:,.2f}  "
            f"({p['risk_pct']:.1f}% dari ${p['balance_usdt']:,.2f})",
            f"Fee est.   : ${p.get('fee_est_usdt', 0.0):,.2f}  (taker 2 sisi)",
        ]
        if p.get("margin_capped"):
            lines.append("")
            lines.append("⚠ Qty dikecilkan agar margin muat (cap leverage×balance)")
        if p["rr1"] < 1.5:
            lines.append("")
            lines.append(f"⚠ R:R {p['rr1']:.2f} di bawah 1.5 — pertimbangkan skip")
        title = "KONFIRMASI ORDER LIVE" if live else "Konfirmasi Order (Paper)"
        ans = QMessageBox.question(
            self, title, "\n".join(lines) + "\n\nEksekusi order ini?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            self._exec_busy = False
            self.statusBar().showMessage("Order dibatalkan", 5000)
            return

        self.statusBar().showMessage(f"Mengeksekusi order {p['symbol']}…")

        def work():
            try:
                self.exec_done.emit(
                    self.executor.execute(p, getattr(self, "_exec_context", None)))
            except Exception as e:
                self.exec_failed.emit(str(e))
        threading.Thread(target=work, daemon=True, name="exec-order").start()

    def _on_exec_done(self, res: dict):
        self._exec_busy = False
        was_auto = bool(res.get("auto")) or self._exec_was_auto
        self._exec_was_auto = False
        self._play_ping()
        if res.get("ok"):
            if was_auto:
                # Sukses auto: cukup status bar — jangan spam dialog modal
                self.statusBar().showMessage(
                    f"🤖 AUTO {res['mode']} OK: {res.get('summary', '')}", 20000)
                if res.get("tp_error"):
                    self.statusBar().showMessage(
                        f"🤖 AUTO {res['mode']}: TP gagal terpasang (posisi ber-SL) — "
                        f"{res['tp_error']}", 20000)
            else:
                note = res.get("note", "Order terkirim — SL & TP terpasang")
                extra = f"\n⚠ TP gagal: {res['tp_error']}" if res.get("tp_error") else ""
                QMessageBox.information(self, f"Order {res['mode']} OK", note + extra)
                self.statusBar().showMessage(f"Order {res['mode']} OK", 10000)
        else:
            msg = res.get("error", "?")
            if res.get("failsafe"):
                msg += f"\n{res['failsafe']}"
            if was_auto:
                self._disarm_auto("order gagal")
                msg += "\n\n🤖 AUTO TRADE dimatikan untuk keamanan."
            QMessageBox.critical(self, "Order GAGAL", msg)
            self.statusBar().showMessage("Order gagal — lihat dialog", 10000)

    def _on_exec_failed(self, msg: str):
        self._exec_busy = False
        was_auto = self._exec_was_auto
        self._exec_was_auto = False
        if was_auto:
            self._disarm_auto("eksekusi gagal")
            QMessageBox.warning(
                self, "AUTO TRADE dimatikan",
                f"Eksekusi otomatis gagal:\n{msg}\n\n"
                "🤖 AUTO TRADE dimatikan untuk keamanan — periksa lalu aktifkan lagi.")
        else:
            QMessageBox.warning(self, "Eksekusi dibatalkan", msg)
        self.statusBar().showMessage(f"Eksekusi gagal: {msg}", 10000)

    def _play_ping(self):
        """Short, non-blocking notification ping (Windows ding, else Qt beep)."""
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
            return
        except Exception:
            pass
        try:
            from PyQt6.QtWidgets import QApplication
            QApplication.beep()
        except Exception:
            pass

    def _change_focus_symbol(self, symbol: str):
        # Auto-trade mengikuti symbol fokus — matikan saat pindah supaya
        # tidak diam-diam trading symbol yang berbeda
        self._disarm_auto("ganti symbol fokus")
        self.current_focus_symbol = symbol
        self.battlefield.set_symbol(symbol)
        self.entry_card.reset()
        self.chart.reset(symbol)
        self.heatmap.reset(symbol)
        self.liquidity_ladder.reset(symbol)
        self.event_feed.reset()
        
    def closeEvent(self, event):
        # Shutdown cleanly
        self.battle_stream.stop()
        if hasattr(self, 'loop') and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.engine.stop(), self.loop)
            self.loop.call_soon_threadsafe(self.loop.stop())
        event.accept()
