"""Pencatatan trade ke JSONL + ringkasan performa ke terminal."""
import json
from pathlib import Path

from core.models import Trade


class TradeLogger:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write_all(self, trades: list[Trade]) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            for t in trades:
                f.write(json.dumps(t.to_dict()) + "\n")


def print_summary(stats: dict, title: str = "RINGKASAN BACKTEST") -> None:
    print(f"\n=== {title} ===")
    if stats["trades"] == 0:
        print("Tidak ada trade — bot memilih untuk tidak trading di periode ini.")
        return
    print(f"Jumlah trade      : {stats['trades']}")
    print(f"Win rate          : {stats['win_rate']:.1f}%")
    print(f"Expectancy        : {stats['expectancy_r']:+.2f}R per trade")
    print(f"Profit factor     : {stats['profit_factor']:.2f}")
    print(f"Avg win / loss    : {stats['avg_win_r']:+.2f}R / {stats['avg_loss_r']:+.2f}R")
    print(f"Total PnL         : {stats['total_pnl']:+.2f} ({stats['return_pct']:+.2f}%)")
    print(f"Max drawdown      : {stats['max_dd_pct']:.2f}%")
    print(f"Balance akhir     : {stats['final_balance']:.2f}")
