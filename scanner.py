"""
Pivot + Haoshoku Scanner - versi GitHub Actions
=================================================
Dijalankan otomatis via GitHub Actions terjadwal (lihat .github/workflows/scan.yml).
Setiap kali jalan: fetch OHLCV terbaru (4H & 1D, sesuai keputusan project),
hitung Daily Pivot (Classic) + Haoshoku Pivot (Fibonacci), lalu simpan hasilnya
ke data/latest.json - supaya bisa dibaca dari luar (termasuk oleh Claude lewat
raw.githubusercontent.com, yang tidak bisa akses exchange API secara langsung).
"""

import json
import os
from datetime import datetime, timezone

import ccxt
import pandas as pd


# ---------------------------------------------------------------------------
# RUMUS PIVOT (sudah divalidasi terhadap data screenshot ETH nyata sebelumnya)
# ---------------------------------------------------------------------------

def classic_pivot(prev_high, prev_low, prev_close):
    pp = (prev_high + prev_low + prev_close) / 3.0
    r1 = 2 * pp - prev_low
    r2 = pp + (prev_high - prev_low)
    r3 = prev_high + 2 * (pp - prev_low)
    s1 = 2 * pp - prev_high
    s2 = pp - (prev_high - prev_low)
    s3 = prev_low - 2 * (prev_high - pp)
    return {"PP": pp, "R1": r1, "R2": r2, "R3": r3, "S1": s1, "S2": s2, "S3": s3}


def fib_pivot(prev_high, prev_low, prev_close):
    pp = (prev_high + prev_low + prev_close) / 3.0
    rng = prev_high - prev_low
    r1 = pp + 0.382 * rng
    r2 = pp + 0.618 * rng
    r3 = pp + 1.000 * rng
    s1 = pp - 0.382 * rng
    s2 = pp - 0.618 * rng
    s3 = pp - 1.000 * rng
    return {"PP": pp, "R1": r1, "R2": r2, "R3": r3, "S1": s1, "S2": s2, "S3": s3}


def classify_touch(high, low, close, level_price, is_resistance):
    """BREAKOUT = close menembus, REJECTION = wick tembus tapi close balik, None = tidak tersentuh."""
    if not (low <= level_price <= high):
        return None
    if is_resistance:
        return "BREAKOUT" if close > level_price else "REJECTION"
    return "BREAKOUT" if close < level_price else "REJECTION"


# ---------------------------------------------------------------------------
# DAFTAR PAIR - tambah/kurangi sesuai kebutuhan (format ccxt utk Binance USDM)
# ---------------------------------------------------------------------------

SYMBOLS = [
    # Large-cap (pembanding, sesuai keputusan sebelumnya - large-cap vs altcoin)
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "BNB/USDT:USDT",
    "XRP/USDT:USDT",
    # Altcoin established
    "DOGE/USDT:USDT",
    "ADA/USDT:USDT",
    # Altcoin yang sudah kita analisis manual sebelumnya - biar bisa dibandingkan
    "NEAR/USDT:USDT",
    "ENA/USDT:USDT",
    "WLD/USDT:USDT",
    "JTO/USDT:USDT",
    "HYPE/USDT:USDT",
    # Kalau ada pair yang tidak listing di exchange manapun (Binance/OKX/Bybit),
    # otomatis tercatat sebagai error di output - tidak bikin script berhenti.
]

LEVELS = ["R3", "R2", "R1", "S1", "S2", "S3"]


# Dicoba berurutan - Binance Futures kadang diblokir dari lokasi server tertentu
# (termasuk server GitHub Actions gratis), jadi kita fallback ke exchange lain.
EXCHANGE_IDS = ["binanceusdm", "okx", "bybit"]


def fetch_ohlcv_with_fallback(symbol, timeframe, limit=3):
    """Coba tiap exchange di EXCHANGE_IDS berurutan, pakai yang pertama berhasil."""
    last_err = None
    for ex_id in EXCHANGE_IDS:
        try:
            exchange = getattr(ccxt, ex_id)({"enableRateLimit": True})
            raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
            return ex_id, df
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"Semua exchange gagal untuk {symbol} {timeframe}: {last_err}")


def scan_symbol(symbol):
    # Ambil beberapa candle terakhir supaya "candle sebelumnya" pasti sudah closed
    ex_used_4h, df_4h = fetch_ohlcv_with_fallback(symbol, "4h", limit=3)
    ex_used_1d, df_1d = fetch_ohlcv_with_fallback(symbol, "1d", limit=3)

    last_4h = df_4h.iloc[-1]      # candle 4H yang sedang berjalan/baru closed
    prev_4h = df_4h.iloc[-2]      # candle 4H sebelumnya -> sumber Haoshoku (auto-TF)
    prev_1d = df_1d.iloc[-2]      # candle Daily sebelumnya -> sumber Daily Pivot

    daily = classic_pivot(prev_1d["high"], prev_1d["low"], prev_1d["close"])
    haos = fib_pivot(prev_4h["high"], prev_4h["low"], prev_4h["close"])

    events = []
    for src, levels in [("Daily", daily), ("Haos", haos)]:
        for lvl in LEVELS:
            price = levels[lvl]
            result = classify_touch(last_4h["high"], last_4h["low"], last_4h["close"], price, lvl.startswith("R"))
            if result:
                events.append({"source": src, "level": lvl, "price_level": round(price, 8), "result": result})

    pp_gap_pct = abs(daily["PP"] - haos["PP"]) / last_4h["close"] * 100

    return {
        "symbol": symbol,
        "exchange_daily": ex_used_1d,
        "exchange_haoshoku": ex_used_4h,
        "last_candle": {
            "time": int(last_4h["ts"]),
            "open": last_4h["open"], "high": last_4h["high"],
            "low": last_4h["low"], "close": last_4h["close"],
        },
        "daily_pivot": {k: round(v, 8) for k, v in daily.items()},
        "haoshoku_pivot": {k: round(v, 8) for k, v in haos.items()},
        "pp_confluence_pct": round(pp_gap_pct, 4),
        "events": events,
    }


def main():
    results = {"generated_at": datetime.now(timezone.utc).isoformat(), "symbols": []}

    for sym in SYMBOLS:
        try:
            results["symbols"].append(scan_symbol(sym))
        except Exception as e:
            results["symbols"].append({"symbol": sym, "error": str(e)})

    os.makedirs("data", exist_ok=True)

    # Snapshot terkini (ditimpa tiap run - buat cek cepat)
    with open("data/latest.json", "w") as f:
        json.dump(results, f, indent=2, default=str)

    # Log historis (DITAMBAH tiap run, tidak pernah ditimpa - ini yang dipakai untuk statistik)
    with open("data/events_log.jsonl", "a") as f:
        for s in results["symbols"]:
            if "error" in s:
                continue
            for ev in s["events"]:
                row = {
                    "generated_at": results["generated_at"],
                    "symbol": s["symbol"],
                    "exchange_daily": s["exchange_daily"],
                    "exchange_haoshoku": s["exchange_haoshoku"],
                    "close": s["last_candle"]["close"],
                    "pp_confluence_pct": s["pp_confluence_pct"],
                    "source": ev["source"],
                    "level": ev["level"],
                    "price_level": ev["price_level"],
                    "result": ev["result"],
                }
                f.write(json.dumps(row, default=str) + "\n")

    print(f"Scan selesai: {len(SYMBOLS)} pair, disimpan ke data/latest.json + data/events_log.jsonl")


if __name__ == "__main__":
    main()
