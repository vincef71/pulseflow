# stupidbot

Bot trading price action murni. Tanpa indikator — pasar (candle + struktur)
adalah indikatornya. Satu-satunya pengecualian: **ATR**, dipakai hanya untuk
stop loss, position sizing, filter volatilitas, dan trailing stop.

## Filosofi

1. Preservasi modal
2. Struktur pasar Daily
3. Konfirmasi price action
4. Manajemen risiko
5. Kualitas trade
6. Profit

Ragu = tidak trading. Melewatkan trade lebih baik daripada mengambil trade jelek.

## Alur keputusan

1. **Daily bias** (`daily_bias/`) — HH/HL/LH/LL, BOS, CHoCH di TF Daily
   menentukan boleh long/short. Daily netral → tidak ada trade.
2. **Entry 1H/15M** (`entry_engine/`) — pullback 38.2–78.6% ke impulse leg
   searah bias + candle rejection (pin bar / engulfing) + RR ≥ 1:2 terhadap
   target struktural Daily.
3. **Risk** (`risk_manager/`) — sizing dari balance × risiko% ÷ jarak stop ATR.
4. **Manajemen** (`position_manager/`) — partial TP di +1.5R, SL ke breakeven,
   ATR trailing setelah +2R.

## Struktur modul

```
config/            parameter strategi & risiko (Settings)
core/              model data + ATR
data/              fetch OHLCV Binance Futures + cache
price_action/      pola candle (pin bar, engulfing, inside/outside bar)
market_structure/  swing, HH/HL/LH/LL, BOS, CHoCH
daily_bias/        bias arah dari Daily
entry_engine/      aturan entry 1H/15M
risk_manager/      position sizing
position_manager/  partial TP, BE, trailing
backtester/        backtest event-driven + walk-forward
logger/            log trade JSONL + ringkasan
main.py            CLI
```

## Pemakaian

```bash
pip install -r requirements.txt

python main.py backtest --symbol BTCUSDT --entry-tf 1h \
    --start 2024-01-01 --end 2026-06-30 --balance 10000

python main.py walkforward --symbol BTCUSDT --folds 4 \
    --start 2024-01-01 --end 2026-06-30
```

Override parameter lewat `config.json` (key = field `Settings`), contoh:

```json
{"risk_per_trade_pct": 0.5, "trail_atr_mult": 2.5}
```

## Kejujuran backtest

- Candle Daily baru dipakai setelah harinya close penuh.
- Swing terkonfirmasi dengan lag k candle (tanpa lookahead).
- SL + TP tersentuh di candle yang sama → SL dianggap kena dulu (pesimis).
- Fee taker dihitung dua sisi.
