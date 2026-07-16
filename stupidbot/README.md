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
   target struktural Daily. **NO MARKET ORDER**: entry adalah LIMIT di bekas
   level SL (di bawah/atas wick rejection — area stop-hunt); SL baru = harga
   limit ∓ `limit_sl_atr_mult` × ATR. Order hidup sampai terisi atau
   zona/struktur rusak (bias Daily flip, trend TF entry berubah, atau harga
   breakout tanpa mengisi order). Order yang batal mengembalikan kuota bulanan.
3. **Risk** (`risk_manager/`) — sizing dari balance × risiko% ÷ jarak stop ATR.
4. **Manajemen** (`position_manager/`) — partial TP di +1.5R, SL ke breakeven,
   ATR trailing setelah +2R.

## Lapisan proteksi akun (`risk_manager/`)

- **Adaptive risk** — tier risiko 0.5% → 1% → 1.5%; naik satu tingkat hanya
  saat equity mencetak high baru, turun satu tingkat saat drawdown dari peak
  ≥ `risk_step_down_dd_pct` (default 3%).
- **Equity protection** — entry baru dihentikan sementara bila drawdown harian
  ≥ 2% (sampai hari UTC berikutnya) atau drawdown total ≥ 8% (cooldown 14
  hari). Posisi terbuka tetap dikelola sampai selesai.
- **Quality over quantity** — maksimal `max_trades_per_month` (default 10)
  trade per bulan kalender + jeda minimal 12 jam antar entry.
- **Portfolio mode** — banyak simbol, satu balance; kandidat sinyal diranking
  dengan `structure_score()` (kerapian label swing Daily + bonus BOS searah)
  dan hanya struktur terbaik yang mengisi slot (`max_open_positions`).

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

python main.py portfolio --symbols BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT \
    --entry-tf 1h --start 2024-01-01 --end 2026-06-30

# runner paper (default, aman): simulasi penuh dari candle closed
python main.py live --symbols BTCUSDT,ETHUSDT --entry-tf 1h

# runner LIVE (uang nyata) — double opt-in:
#   1. set PAPER_MODE=false di ../.env
#   2. tambahkan flag --live
python main.py live --symbols BTCUSDT --entry-tf 1h --live
```

## Live / paper runner

`live/runner.py` memakai alur keputusan yang SAMA dengan backtester,
ditambah eksekusi:

- **Paper** — simulasi penuh; jurnal `logs/paper_live_trades_{tf}.jsonl`.
- **Live** — via `trading/executor.py` (subclass `TradeExecutor` PulseFlow,
  kredensial dari `../.env`): LIMIT entry + STOP_MARKET protektif dipasang
  SERENTAK, jadi posisi terlindungi sejak detik pertama terisi (SL yang
  trigger tanpa posisi hangus tanpa efek). Setelah terisi, TP dipasang
  sebagai LIMIT reduce-only (maker) dua leg: partial di +1.5R dan sisa di
  TP. BE/trailing hanya menggeser STOP_MARKET. Bila SL gagal terpasang,
  posisi ditutup paksa (fail-safe). Satu-satunya order market yang mungkin
  terjadi: eksekusi SL (stop-market) dan fail-safe.
- State (posisi, tier risiko, guard, kuota bulanan) dipersist ke
  `state/live_state.json` — restart aman.
- `--once` menjalankan satu siklus lalu keluar (untuk uji / scheduler).

Catatan balance kecil: risiko tier terendah bisa menghasilkan notional di
bawah minimum Binance (~$100) — order tersebut ditolak exchange dan hanya
tercatat di log. Sesuaikan `risk_tiers_pct` via `config.json` bila perlu.

Override parameter lewat `config.json` (key = field `Settings`), contoh:

```json
{"risk_per_trade_pct": 0.5, "trail_atr_mult": 2.5}
```

## Kejujuran backtest

- Candle Daily baru dipakai setelah harinya close penuh.
- Swing terkonfirmasi dengan lag k candle (tanpa lookahead).
- Limit terisi bila harga menyentuh level; fill + SL di candle yang sama →
  SL dianggap kena (pesimis).
- SL + TP tersentuh di candle yang sama → SL dianggap kena dulu (pesimis).
- Fee dibedakan: maker (`maker_fee_pct`) untuk entry limit & TP limit,
  taker (`fee_pct`) untuk SL/trailing/exit paksa.
