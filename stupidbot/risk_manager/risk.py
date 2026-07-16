"""Position sizing dari balance, risiko %, dan jarak stop ATR.

Tidak pernah pakai lot tetap.
"""


def position_size(balance: float, risk_pct: float, entry: float, sl: float) -> tuple[float, float]:
    """Kembalikan (qty, risk_amount). qty = 0 bila input tidak valid."""
    stop = abs(entry - sl)
    if stop <= 0 or balance <= 0:
        return 0.0, 0.0
    risk_amount = balance * risk_pct / 100.0
    return risk_amount / stop, risk_amount
