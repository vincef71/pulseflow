"""Blok status market per simbol untuk console — supaya analisa bot bisa
diverifikasi manual terhadap chart: bias Daily, rantai swing, event struktur
terakhir, zona pullback TF entry, dan alasan spesifik kenapa (belum) entry.
"""
from datetime import datetime, timezone

from config.settings import Settings
from core.models import Candle, Direction, SwingType


def _f(x: float | None) -> str:
    return f"{x:,.6g}" if x is not None else "-"


def _ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def format_market_status(symbol: str, entry_tf: str, cfg: Settings,
                         candle: Candle, bias_engine, entry_engine,
                         position=None, pending=None,
                         status_note: str = "") -> list[str]:
    lines: list[str] = []
    bias, bias_reason = bias_engine.bias()
    datr = bias_engine.atr.value
    datr_pct = 100.0 * datr / candle.close if datr else None
    score = bias_engine.structure_score()

    lines.append(f"{symbol}  candle {entry_tf} {_ts(candle.ts)} UTC  "
                 f"close {_f(candle.close)}")
    lines.append(f"  Daily : bias {bias.value} — {bias_reason} | "
                 f"ATR {_f(datr)} ({datr_pct:.2f}%)" if datr_pct is not None
                 else f"  Daily : bias {bias.value} — {bias_reason}")
    if score:
        lines[-1] += f" | skor struktur {score:.0f}"

    swings = bias_engine.tracker.swings[-4:]
    if swings:
        chain = " → ".join(f"{s.label or s.type.value[:1]} {_f(s.price)}"
                           for s in swings)
        ev = bias_engine.tracker.events[-1] if bias_engine.tracker.events else None
        ev_s = f" | event: {ev.type} @ {_f(ev.level)}" if ev else ""
        lines.append(f"  Swing Daily : {chain}{ev_s}")

    # ── TF entry: trend, leg, zona pullback ──────────────────────────
    etrend = entry_engine.tracker.trend.value
    eatr = entry_engine.atr.value
    eatr_pct = 100.0 * eatr / candle.close if eatr else None
    entry_line = f"  Entry {entry_tf}: trend {etrend}"
    if eatr:
        entry_line += f" | ATR {_f(eatr)} ({eatr_pct:.2f}%)"

    high = entry_engine.tracker.last_swing(SwingType.HIGH)
    low = entry_engine.tracker.last_swing(SwingType.LOW)
    zone = None
    if bias == Direction.LONG and high and low and low.index < high.index:
        leg = high.price - low.price
        zone = (high.price - cfg.pullback_max * leg,
                high.price - cfg.pullback_min * leg)
        entry_line += (f" | leg {_f(low.price)} → {_f(high.price)}"
                       f" | zona pullback {_f(zone[0])} – {_f(zone[1])}")
    elif bias == Direction.SHORT and high and low and high.index < low.index:
        leg = high.price - low.price
        zone = (low.price + cfg.pullback_min * leg,
                low.price + cfg.pullback_max * leg)
        entry_line += (f" | leg {_f(high.price)} → {_f(low.price)}"
                       f" | zona pullback {_f(zone[0])} – {_f(zone[1])}")
    lines.append(entry_line)
    if zone:
        if zone[0] <= candle.close <= zone[1]:
            posisi_harga = "DI DALAM zona"
        elif ((bias == Direction.LONG and candle.close > zone[1])
              or (bias == Direction.SHORT and candle.close < zone[0])):
            posisi_harga = "belum sampai zona"
        else:
            posisi_harga = "melewati zona"
        lines.append(f"  Harga vs zona: {posisi_harga}")

    # ── Status keputusan ──────────────────────────────────────────────
    if position is not None:
        sig = position.signal
        lines.append(
            f"  Status: POSISI {sig.direction.value} qty {_f(position.qty)} | "
            f"entry {_f(sig.entry)} SL {_f(position.sl)} TP {_f(sig.tp)} | "
            f"partial {'✔' if position.partial_done else '—'} | "
            f"MFE {position.mfe_r:+.2f}R MAE {position.mae_r:.2f}R")
    elif pending is not None:
        sig = pending.signal
        lines.append(
            f"  Status: MENUNGGU FILL limit {sig.direction.value} @ {_f(sig.entry)} "
            f"(SL {_f(sig.sl)}, TP {_f(sig.tp)}, RR {sig.rr:.1f}) "
            f"sejak {_ts(pending.placed_ts)} — batal bila zona/struktur rusak")
    else:
        lines.append(f"  Status: {status_note or 'TIDAK ENTRY'}")
    return lines
