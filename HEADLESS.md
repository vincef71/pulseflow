# PulseFlow Headless — Deploy di VPS

Runner `run_headless.py` menjalankan seluruh pipeline PulseFlow (feed live,
analitik, entry engine, auto-trade) **tanpa GUI** — tidak butuh PyQt6,
pyqtgraph, atau display server. Cocok untuk VPS Linux kecil (1 vCPU / 1 GB
RAM cukup untuk 1–2 symbol).

## 1. Persiapan VPS

```bash
# 1. Python + git
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git

# 2. Clone project
git clone https://github.com/vincef71/pulseflow.git ~/pulseflow
cd ~/pulseflow

# 3. Buat virtual environment (sekali saja) lalu aktifkan
python3 -m venv .venv
source .venv/bin/activate      # prompt berubah jadi (.venv)

# 4. Install dependensi headless (tanpa PyQt6)
pip install --upgrade pip
pip install -r requirements.txt
```

> PyQt6 **tidak** perlu diinstall di VPS — semua modul UI hanya diimport
> oleh `run.py` (GUI). Untuk GUI di desktop:
> `pip install -r requirements-gui.txt`.

Catatan venv:
- Aktifkan lagi tiap login baru: `source ~/pulseflow/.venv/bin/activate`
  (`deactivate` untuk keluar).
- Tanpa mengaktifkan pun bisa langsung:
  `~/pulseflow/.venv/bin/python run_headless.py …` — cara ini yang
  dipakai di unit systemd di bawah.
- Di Windows aktivasinya: `.venv\Scripts\activate`.

Terakhir, buat `.env` dari template lalu isi API key:

```bash
cp .env.example .env
nano .env                      # isi BINANCE_API_KEY / SECRET
chmod 600 .env                 # hanya bisa dibaca user ini
```

## 2. Menjalankan

```bash
# SELALU mulai dengan paper untuk verifikasi sinyal & koneksi:
python run_headless.py --paper --symbols BTCUSDT ETHUSDT --heartbeat 30

# LIVE (uang nyata) — butuh PAPER_MODE=false di .env DAN flag --live:
python run_headless.py --live --symbols BTCUSDT
```

Opsi:

| Flag | Default | Fungsi |
|---|---|---|
| `--symbols` | `BTCUSDT ETHUSDT XAUUSDT HYPEUSDT` | symbol yang dilacak & ditradingkan (semua symbol, bukan hanya fokus seperti di GUI) |
| `--mode` | `binance` | sumber feed data (`binance`/`hyperliquid`); eksekusi selalu Binance Futures |
| `--paper` | — | paksa paper mode, abaikan `.env` |
| `--live` | — | konfirmasi eksplisit mode LIVE (pengganti dialog GUI) |
| `--risk` | `RISK_PCT` .env | override risk % per trade |
| `--warmup` | `90` | detik awal tanpa eksekusi (konteks klines seeding) |
| `--heartbeat` | `60` | interval log status berkala |

## 3. Pengaman bawaan

- **Gerbang LIVE ganda**: `PAPER_MODE=false` di `.env` saja tidak cukup —
  tanpa `--live` runner menolak start.
- **Satu posisi per symbol** — fire saat posisi masih terbuka dilewati.
- **Manajemen posisi**: profit ≥ 0.5R → tutup 50% + SL exchange pindah ke
  breakeven; sisa posisi di-trail engine (best ± 2×ATR-1m, exit `TRAIL`).
  Setelah breakeven, FADED tidak lagi menutup posisi (runner dibiarkan).
- **Warm-up** — fire di detik-detik awal dilewati.
- **Circuit breaker** — 3 error eksekusi beruntun → entry baru mati
  (DISARMED); exit posisi terbuka tetap dikelola. Restart untuk re-arm.
- **SL fail-safe** (warisan `TradeExecutor`): order tanpa SL tidak pernah
  dibiarkan hidup; SL gagal terpasang → posisi langsung ditutup paksa.
- **Shutdown**: posisi TIDAK ditutup saat runner berhenti — SL/TP tetap
  terpasang di exchange (algo order), jadi posisi tetap terlindungi
  meskipun VPS mati.

## 4. Service permanen (systemd)

`/etc/systemd/system/pulseflow.service`:

```ini
[Unit]
Description=PulseFlow headless auto-trader
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=trader
WorkingDirectory=/home/trader/pulseflow
ExecStart=/home/trader/pulseflow/.venv/bin/python run_headless.py --paper --symbols BTCUSDT
Restart=on-failure
RestartSec=15
# Matikan buffer supaya log realtime di journalctl
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pulseflow
journalctl -u pulseflow -f          # pantau log realtime
```

> **Catatan `Restart=on-failure`**: restart otomatis me-reset circuit
> breaker (DISARMED) dan tracking posisi live sesi (`_live_tracked`) —
> posisi yang dibuka sesi sebelumnya tidak akan ditutup otomatis oleh
> sesi baru, tapi tetap terlindungi SL/TP di exchange.

## 5. Log & data

- `pulseflow_headless.log` — log runner (rotating, 10 MB × 5).
- `paper_trades.json` — jurnal paper trade + PnL net fee.
- `~/.pulseflow/data/` — parquet metrics/trades (retensi 7 hari).

Heartbeat tiap interval menampilkan: status runner (WARM-UP/ARMED/DISARMED),
harga + fase entry per symbol, jumlah trade dibuka/ditutup, dan peringatan
bila feed macet > 120 s.
