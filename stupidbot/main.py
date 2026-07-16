"""Titik masuk CLI stupidbot.

Contoh:
    python main.py backtest --symbol BTCUSDT --entry-tf 1h \
        --start 2024-01-01 --end 2026-06-30 --balance 10000
    python main.py walkforward --symbol BTCUSDT --folds 4 \
        --start 2024-01-01 --end 2026-06-30
"""
import argparse
import sys
from datetime import datetime, timezone

# terminal Windows sering default cp1252; paksa UTF-8 agar output tidak crash
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backtester.backtest import DAY_MS, Backtester, walkforward_folds
from config.settings import Settings
from data.binance import get_klines, interval_ms
from logger.trade_log import TradeLogger, print_summary

WARMUP_DAILY = 200          # hari warmup untuk struktur & ATR Daily
WARMUP_ENTRY_CANDLES = 300  # candle warmup TF entry


def parse_date(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


def load_data(args):
    start = parse_date(args.start)
    end = parse_date(args.end)
    daily = get_klines(args.symbol, "1d", start - WARMUP_DAILY * DAY_MS, end)
    entry_start = start - WARMUP_ENTRY_CANDLES * interval_ms(args.entry_tf)
    entry = get_klines(args.symbol, args.entry_tf, entry_start, end)
    return start, end, daily, entry


def cmd_backtest(args) -> None:
    cfg = Settings.load(args.config)
    start, end, daily, entry = load_data(args)
    print(f"Data: {len(daily)} candle Daily, {len(entry)} candle {args.entry_tf}")

    bt = Backtester(cfg, args.symbol, args.entry_tf, args.balance)
    result = bt.run(daily, entry, trade_from_ts=start)

    log_path = f"logs/trades_{args.symbol}_{args.entry_tf}.jsonl"
    TradeLogger(log_path).write_all(result["trades"])
    print_summary(result["stats"], f"BACKTEST {args.symbol} {args.entry_tf} "
                                   f"{args.start} → {args.end}")
    print(f"\nLog trade: {log_path}")


def cmd_walkforward(args) -> None:
    cfg = Settings.load(args.config)
    start, end, daily, entry = load_data(args)

    bt = Backtester(cfg, args.symbol, args.entry_tf, args.balance)
    result = bt.run(daily, entry, trade_from_ts=start)

    folds = walkforward_folds(result["trades"], start, end, args.folds, args.balance)
    for f in folds:
        print_summary(f["stats"], f"FOLD {f['fold']} ({f['start']} → {f['end']})")
    print_summary(result["stats"], "TOTAL SEMUA FOLD")


def main() -> None:
    p = argparse.ArgumentParser(description="stupidbot — bot price action murni (ATR-only)")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--symbol", default="BTCUSDT")
    common.add_argument("--entry-tf", default="1h", choices=["1h", "15m"],
                        help="TF entry hanya boleh 1H atau 15M")
    common.add_argument("--start", required=True, help="YYYY-MM-DD (UTC)")
    common.add_argument("--end", required=True, help="YYYY-MM-DD (UTC)")
    common.add_argument("--balance", type=float, default=10_000.0)
    common.add_argument("--config", default="config.json",
                        help="file JSON untuk override Settings")

    sp = sub.add_parser("backtest", parents=[common], help="backtest historis")
    sp.set_defaults(func=cmd_backtest)

    sp = sub.add_parser("walkforward", parents=[common], help="uji stabilitas per fold")
    sp.add_argument("--folds", type=int, default=4)
    sp.set_defaults(func=cmd_walkforward)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
