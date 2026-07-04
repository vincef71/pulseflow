"""
Live Report — analisa hasil live trading PulseFlow.

Sumber data:
  1. Binance USDS-M Futures API (SUMBER KEBENARAN hasil):
     - futures_account_trades  → setiap fill: harga, qty, realizedPnl, fee
     - futures_income_history  → rekap REALIZED_PNL / COMMISSION / FUNDING_FEE
  2. live_trades.json (jurnal konteks, ditulis TradeExecutor):
     setup, skor, plan SL/TP, risk, alasan exit (STOP/TP2/FLIP/FADED/MANUAL).

Fill digabungkan ke tiap trade jurnal lewat jendela waktu
[opened_ts − 60 s, closed_ts + 120 s] per symbol (SL/TP exchange bisa
terisi sebelum runner mendeteksi setup berakhir). Fill yang tidak cocok
dengan jurnal mana pun dilaporkan terpisah sebagai "di luar jurnal".

Semua panggilan API read-only — tidak ada order yang ditempatkan.

Pakai:
    python live_report.py                    # 30 hari terakhir
    python live_report.py --days 7
    python live_report.py --symbols BTCUSDT ETHUSDT
    python live_report.py --md reports/live_report.md
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parent
LIVE_LOG_FILE = _ROOT / "live_trades.json"

# Margin pencocokan fill ↔ trade jurnal (ms)
WINDOW_BEFORE_MS = 60_000
WINDOW_AFTER_MS = 120_000


# ── Fetch API ──────────────────────────────────────────────────────────

def make_client():
    import os
    from binance.client import Client
    load_dotenv(_ROOT / ".env")
    key = os.getenv("BINANCE_API_KEY", "")
    sec = os.getenv("BINANCE_API_SECRET", "")
    if not key or not sec:
        sys.exit("BINANCE_API_KEY / BINANCE_API_SECRET belum diisi di .env")
    return Client(key, sec)


def fetch_fills(client, symbol: str, start_ms: int) -> list:
    """Semua fill futures symbol ini sejak start_ms (paginate per 1000)."""
    fills, start = [], start_ms
    while True:
        batch = client.futures_account_trades(
            symbol=symbol, startTime=start, limit=1000)
        fills.extend(batch)
        if len(batch) < 1000:
            return fills
        start = int(batch[-1]["time"]) + 1


def fetch_income_totals(client, start_ms: int) -> dict:
    """Rekap income per tipe (REALIZED_PNL/COMMISSION/FUNDING_FEE) — dipakai
    sebagai cross-check terhadap penjumlahan per-trade."""
    totals, start = {}, start_ms
    while True:
        batch = client.futures_income_history(startTime=start, limit=1000)
        for it in batch:
            t = it.get("incomeType", "?")
            totals[t] = totals.get(t, 0.0) + float(it.get("income", 0.0))
        if len(batch) < 1000:
            return totals
        start = int(batch[-1]["time"]) + 1


# ── Join jurnal ↔ fill ─────────────────────────────────────────────────

def join_trades(journal: list, fills_by_symbol: dict, now_ms: int):
    """Kaitkan fill ke tiap trade jurnal; sisanya = di luar jurnal."""
    used_fill_ids = set()
    enriched = []

    for t in sorted(journal, key=lambda x: x.get("opened_ts", 0)):
        sym = t.get("symbol", "")
        t0 = int(t.get("opened_ts", 0)) - WINDOW_BEFORE_MS
        t1 = (int(t["closed_ts"]) + WINDOW_AFTER_MS
              if t.get("closed_ts") else now_ms)
        window = [f for f in fills_by_symbol.get(sym, [])
                  if t0 <= int(f["time"]) <= t1
                  and (sym, f["id"]) not in used_fill_ids]
        for f in window:
            used_fill_ids.add((sym, f["id"]))

        pnl = sum(float(f["realizedPnl"]) for f in window)
        fee_usdt = sum(float(f["commission"]) for f in window
                       if f.get("commissionAsset") == "USDT")
        fee_other = {f["commissionAsset"] for f in window
                     if f.get("commissionAsset") != "USDT"}
        net = pnl - fee_usdt

        entry_side = "BUY" if t.get("side") == "LONG" else "SELL"
        ent = [f for f in window if f["side"] == entry_side]
        ext = [f for f in window if f["side"] != entry_side]

        def _avg(fs):
            q = sum(float(f["qty"]) for f in fs)
            return (sum(float(f["price"]) * float(f["qty"]) for f in fs) / q
                    if q > 0 else 0.0)

        risk = float(t.get("risk_usdt", 0.0) or 0.0)
        rec = dict(t)
        rec.update({
            "n_fills": len(window),
            "entry_avg": _avg(ent),
            "exit_avg": _avg(ext),
            "pnl_gross": round(pnl, 4),
            "fee_usdt": round(fee_usdt, 4),
            "fee_non_usdt": ", ".join(sorted(fee_other)),
            "pnl_net": round(net, 4),
            "r_multiple": round(net / risk, 2) if risk > 0 else None,
            "result": ("OPEN" if t.get("status") == "LIVE_OPEN" and not ext
                       else "WIN" if net > 0 else "LOSS"),
        })
        enriched.append(rec)

    unmatched = {
        sym: [f for f in fs if (sym, f["id"]) not in used_fill_ids]
        for sym, fs in fills_by_symbol.items()
    }
    unmatched = {s: fs for s, fs in unmatched.items() if fs}
    return enriched, unmatched


# ── Agregasi & render ──────────────────────────────────────────────────

def _stats(trades: list) -> dict:
    closed = [t for t in trades if t["result"] in ("WIN", "LOSS")]
    wins = [t for t in closed if t["result"] == "WIN"]
    losses = [t for t in closed if t["result"] == "LOSS"]
    gross_w = sum(t["pnl_net"] for t in wins)
    gross_l = abs(sum(t["pnl_net"] for t in losses))
    rs = [t["r_multiple"] for t in closed if t["r_multiple"] is not None]
    return {
        "n": len(closed),
        "win_rate": 100.0 * len(wins) / len(closed) if closed else 0.0,
        "net": sum(t["pnl_net"] for t in closed),
        "avg_win": gross_w / len(wins) if wins else 0.0,
        "avg_loss": -gross_l / len(losses) if losses else 0.0,
        "profit_factor": gross_w / gross_l if gross_l > 0 else float("inf"),
        "avg_r": sum(rs) / len(rs) if rs else None,
    }


def _fmt_stats_row(label: str, s: dict) -> str:
    pf = "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
    avg_r = f"{s['avg_r']:+.2f}R" if s["avg_r"] is not None else "-"
    return (f"| {label} | {s['n']} | {s['win_rate']:.0f}% | "
            f"${s['net']:+,.2f} | {pf} | {avg_r} |")


def render_report(trades, unmatched, income, days: int) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    L = [f"# Laporan Live Trading PulseFlow", "",
         f"Periode: {days} hari terakhir · dibuat {now}", ""]

    closed = [t for t in trades if t["result"] in ("WIN", "LOSS")]
    still_open = [t for t in trades if t["result"] == "OPEN"]

    if not trades:
        L += ["_Jurnal live_trades.json kosong untuk periode ini._", ""]
    else:
        L += ["## Ringkasan", "",
              "| Segmen | Trade | Win rate | PnL net | PF | Avg R |",
              "|---|---|---|---|---|---|",
              _fmt_stats_row("**Semua**", _stats(trades))]

        by = {}
        for t in closed:
            by.setdefault(("setup", t.get("setup") or "?"), []).append(t)
            by.setdefault(("exit", t.get("close_reason") or "?"), []).append(t)
            by.setdefault(("symbol", t.get("symbol") or "?"), []).append(t)
        for kind, title in (("setup", "Per setup"), ("exit", "Per alasan exit"),
                            ("symbol", "Per symbol")):
            rows = [(k[1], v) for k, v in by.items() if k[0] == kind]
            if rows:
                L += ["", f"### {title}", "",
                      "| | Trade | Win rate | PnL net | PF | Avg R |",
                      "|---|---|---|---|---|---|"]
                for name, ts in sorted(rows, key=lambda r: -_stats(r[1])["net"]):
                    L.append(_fmt_stats_row(name, _stats(ts)))

        L += ["", "## Detail trade", "",
              "| Waktu | Symbol | Arah | Setup | Skor | Entry | Exit | "
              "Alasan | PnL net | R |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for t in trades:
            r = f"{t['r_multiple']:+.2f}" if t["r_multiple"] is not None else "-"
            pnl = "OPEN" if t["result"] == "OPEN" else f"${t['pnl_net']:+,.2f}"
            L.append(
                f"| {t.get('opened_at', '?')} | {t['symbol']} | {t['side']} | "
                f"{t.get('setup', '?')} | {t.get('score', '?')} | "
                f"{t['entry_avg'] or t.get('entry_fill', 0):,.6g} | "
                f"{t['exit_avg']:,.6g} | {t.get('close_reason', '-')} | "
                f"{pnl} | {r} |")
            if t.get("fee_non_usdt"):
                L.append(f"|  |  |  |  |  |  |  | ⚠ fee {t['fee_non_usdt']} "
                         f"tidak ikut dihitung |  |  |")
        if still_open:
            L += ["", f"⏳ {len(still_open)} posisi masih terbuka "
                  "(belum masuk statistik)."]

    if unmatched:
        L += ["", "## Fill di luar jurnal",
              "", "_Trade manual / dari sesi tanpa jurnal:_", ""]
        for sym, fs in unmatched.items():
            pnl = sum(float(f["realizedPnl"]) for f in fs)
            fee = sum(float(f["commission"]) for f in fs
                      if f.get("commissionAsset") == "USDT")
            L.append(f"- {sym}: {len(fs)} fill · PnL net ${pnl - fee:+,.2f}")

    L += ["", "## Cross-check income Binance (semua symbol, periode sama)", ""]
    for k in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
        if k in income:
            L.append(f"- {k}: ${income[k]:+,.2f}")
    total = sum(income.get(k, 0.0)
                for k in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"))
    L += [f"- **Total net (termasuk funding): ${total:+,.2f}**", ""]
    return "\n".join(L)


# ── Main ───────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Analisa hasil live trading (Binance API + live_trades.json)")
    ap.add_argument("--days", type=int, default=30,
                    help="periode analisa ke belakang (default: 30)")
    ap.add_argument("--symbols", nargs="+", metavar="SYM",
                    help="batasi symbol (default: semua di jurnal)")
    ap.add_argument("--md", metavar="FILE",
                    help="simpan laporan markdown ke file ini")
    args = ap.parse_args()

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - args.days * 86_400_000

    journal = []
    if LIVE_LOG_FILE.exists():
        journal = [t for t in json.loads(LIVE_LOG_FILE.read_text(encoding="utf-8"))
                   if int(t.get("opened_ts", 0)) >= start_ms]

    symbols = ([s.upper() for s in args.symbols] if args.symbols
               else sorted({t["symbol"] for t in journal}))
    if args.symbols:
        journal = [t for t in journal if t["symbol"] in symbols]
    if not symbols:
        sys.exit("Jurnal live_trades.json kosong dan --symbols tidak diberikan "
                 "— tidak ada yang bisa dianalisa.")

    client = make_client()
    print(f"Menarik fill {args.days} hari terakhir untuk: {', '.join(symbols)}…",
          file=sys.stderr)
    fills = {s: fetch_fills(client, s, start_ms) for s in symbols}
    income = fetch_income_totals(client, start_ms)

    trades, unmatched = join_trades(journal, fills, now_ms)
    report = render_report(trades, unmatched, income, args.days)
    print(report)

    if args.md:
        out = Path(args.md)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"\nLaporan disimpan: {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
