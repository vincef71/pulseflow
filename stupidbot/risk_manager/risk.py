"""Manajemen risiko akun.

- position_size : sizing dari balance × risiko% ÷ jarak stop (tanpa lot tetap)
- AdaptiveRisk  : risiko naik bertahap hanya saat equity high baru, turun saat drawdown
- EquityGuard   : stop trading sementara saat dd harian / dd total tersentuh
- TradeThrottle : quality over quantity — batasi frekuensi trade
"""
from datetime import datetime, timezone

from config.settings import Settings

DAY_MS = 86_400_000


def position_size(balance: float, risk_pct: float, entry: float, sl: float) -> tuple[float, float]:
    """Kembalikan (qty, risk_amount). qty = 0 bila input tidak valid."""
    stop = abs(entry - sl)
    if stop <= 0 or balance <= 0:
        return 0.0, 0.0
    risk_amount = balance * risk_pct / 100.0
    return risk_amount / stop, risk_amount


class AdaptiveRisk:
    """Tier risiko (mis. 0.5% → 1% → 1.5%).

    Naik satu tier HANYA saat equity mencetak high baru. Turun satu tier saat
    drawdown dari peak melewati ambang; referensi peak lalu di-reset ke balance
    saat ini sehingga kenaikan tier berikutnya harus dibuktikan dengan recovery.
    """

    def __init__(self, cfg: Settings, start_balance: float):
        self.tiers = list(cfg.risk_tiers_pct)
        self.step_down_dd = cfg.risk_step_down_dd_pct
        self.idx = 0
        self.peak = start_balance

    @property
    def current_pct(self) -> float:
        return self.tiers[self.idx]

    def on_trade_close(self, balance: float) -> None:
        if balance > self.peak:
            self.peak = balance
            self.idx = min(self.idx + 1, len(self.tiers) - 1)
        elif self.peak > 0 and (self.peak - balance) / self.peak * 100.0 >= self.step_down_dd:
            self.idx = max(self.idx - 1, 0)
            self.peak = balance


class EquityGuard:
    """Proteksi equity: berhenti entry sementara bila
    - drawdown harian >= daily_dd_stop_pct  → stop sampai hari UTC berikutnya
    - drawdown total  >= total_dd_stop_pct  → cooldown N hari, peak di-reset

    Posisi yang sudah terbuka tetap dikelola; hanya entry baru yang diblokir.
    """

    def __init__(self, cfg: Settings, start_balance: float):
        self.cfg = cfg
        self.peak = start_balance
        self.day: int | None = None
        self.day_start = start_balance
        self.block_until_ts = 0
        self.halts: list[tuple[int, str]] = []

    def on_candle(self, ts: int, balance: float) -> None:
        """Panggil tiap candle agar saldo awal hari tercatat."""
        day = ts // DAY_MS
        if day != self.day:
            self.day = day
            self.day_start = balance

    def on_trade_close(self, ts: int, balance: float) -> None:
        if self.day_start > 0:
            dd_day = (self.day_start - balance) / self.day_start * 100.0
            if dd_day >= self.cfg.daily_dd_stop_pct:
                next_day = (ts // DAY_MS + 1) * DAY_MS
                if next_day > self.block_until_ts:
                    self.block_until_ts = next_day
                    self.halts.append((ts, f"dd harian {dd_day:.2f}% >= {self.cfg.daily_dd_stop_pct}%"))

        self.peak = max(self.peak, balance)
        if self.peak > 0:
            dd_total = (self.peak - balance) / self.peak * 100.0
            if dd_total >= self.cfg.total_dd_stop_pct:
                until = ts + self.cfg.total_dd_cooldown_days * DAY_MS
                if until > self.block_until_ts:
                    self.block_until_ts = until
                    self.halts.append((ts, f"dd total {dd_total:.2f}% >= {self.cfg.total_dd_stop_pct}%"
                                           f" (cooldown {self.cfg.total_dd_cooldown_days} hari)"))
                # mulai segar setelah cooldown; adaptive risk juga sudah turun tier
                self.peak = balance

    def allowed(self, ts: int) -> bool:
        return ts >= self.block_until_ts


class TradeThrottle:
    """Quality over quantity: maksimal N trade per bulan kalender (UTC) dan
    jeda minimal antar entry. Lebih baik melewatkan trade daripada overtrade."""

    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.month: tuple[int, int] | None = None
        self.count = 0
        self.last_entry_ts: int | None = None

    def allowed(self, ts: int) -> bool:
        if (self.last_entry_ts is not None
                and ts - self.last_entry_ts < self.cfg.entry_cooldown_hours * 3_600_000):
            return False
        if self._month(ts) == self.month and self.count >= self.cfg.max_trades_per_month:
            return False
        return True

    def on_entry(self, ts: int) -> None:
        m = self._month(ts)
        if m != self.month:
            self.month = m
            self.count = 0
        self.count += 1
        self.last_entry_ts = ts

    def on_cancel(self) -> None:
        """Order limit dibatalkan tanpa terisi → kembalikan kuota bulanan
        (jeda antar entry tetap berlaku)."""
        if self.count > 0:
            self.count -= 1

    @staticmethod
    def _month(ts: int) -> tuple[int, int]:
        d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        return (d.year, d.month)
