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
# SMC ORDER BLOCK (Bu-OB / Be-OB) - port dari Pine Script yang sudah kita
# validasi & perkuat di awal project ini (body ratio, rejection wick, volume,
# displacement ATR, konfluensi pivot zigzag). Dievaluasi HANYA pada candle
# yang sudah pasti closed (bukan candle yang masih berjalan), persis prinsip
# barstate.isconfirmed di Pine - supaya tidak repaint.
# ---------------------------------------------------------------------------

OB_LOOKBACK = 10
OB_ATR_PERIOD = 14
OB_VOL_PERIOD = 20
OB_ZIGZAG_LEN = 9
OB_MIN_BODY_RATIO = 0.35
OB_MIN_WICK_RATIO = 0.20
OB_VOL_MULT = 1.2
OB_MIN_DISP_ATR = 0.4
OB_REQUIRE_PIVOT = True

# Toleransi jarak candle-OB ke level Pivot/Haoshoku terdekat, supaya dianggap
# "sinyal gabungan" (dua sumber independen saling menguatkan).
OB_LEVEL_TOLERANCE_PCT = 0.5


def true_range(df):
    prev_close = df["close"].shift(1)
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)


def compute_ob(hist):
    """
    hist = seluruh candle yang SUDAH CLOSED (candle yang masih berjalan tidak
    diikutkan), urut dari lama ke baru. Baris terakhir hist = candle yang
    dievaluasi untuk OB.
    """
    min_len = max(OB_LOOKBACK, OB_ATR_PERIOD, OB_VOL_PERIOD, OB_ZIGZAG_LEN) + 2
    if len(hist) < min_len:
        return None  # data historis belum cukup untuk hitung indikator rolling

    tr = true_range(hist)
    atr = tr.rolling(OB_ATR_PERIOD).mean()
    vol_ma = hist["volume"].rolling(OB_VOL_PERIOD).mean()
    swing_low = hist["low"].rolling(OB_LOOKBACK).min()
    swing_high = hist["high"].rolling(OB_LOOKBACK).max()
    to_down = hist["low"] <= hist["low"].rolling(OB_ZIGZAG_LEN).min()
    to_up = hist["high"] >= hist["high"].rolling(OB_ZIGZAG_LEN).max()

    row = hist.iloc[-1]
    body_size = abs(row["close"] - row["open"])
    candle_range = row["high"] - row["low"]
    body_ratio = body_size / candle_range if candle_range > 0 else 0.0
    lower_wick = min(row["open"], row["close"]) - row["low"]
    upper_wick = row["high"] - max(row["open"], row["close"])
    lower_wick_ratio = lower_wick / candle_range if candle_range > 0 else 0.0
    upper_wick_ratio = upper_wick / candle_range if candle_range > 0 else 0.0

    is_bull = row["close"] > row["open"]
    is_bear = row["close"] < row["open"]
    strong_body = body_ratio >= OB_MIN_BODY_RATIO
    disp_ok = body_size >= atr.iloc[-1] * OB_MIN_DISP_ATR
    vol_ok = row["volume"] >= vol_ma.iloc[-1] * OB_VOL_MULT
    bull_rej = lower_wick_ratio >= OB_MIN_WICK_RATIO
    bear_rej = upper_wick_ratio >= OB_MIN_WICK_RATIO
    pivot_low_ok = (not OB_REQUIRE_PIVOT) or bool(to_down.iloc[-1])
    pivot_high_ok = (not OB_REQUIRE_PIVOT) or bool(to_up.iloc[-1])

    is_bu_ob = bool(is_bull and row["low"] <= swing_low.iloc[-1] and strong_body
                    and disp_ok and vol_ok and bull_rej and pivot_low_ok)
    is_be_ob = bool(is_bear and row["high"] >= swing_high.iloc[-1] and strong_body
                    and disp_ok and vol_ok and bear_rej and pivot_high_ok)

    ob_type = "Bu-OB" if is_bu_ob else ("Be-OB" if is_be_ob else None)
    if ob_type is None:
        return {"ob_type": None, "candle_time": int(row["ts"])}

    return {
        "ob_type": ob_type,
        "candle_time": int(row["ts"]),
        "close": row["close"],
        "body_ratio": round(body_ratio, 3),
        "vol_ratio": round(row["volume"] / vol_ma.iloc[-1], 3) if vol_ma.iloc[-1] else None,
    }


def check_ob_pivot_alignment(ob_close, ob_type, daily, haos):
    """
    Cek apakah candle OB ini juga dekat level Pivot/Haoshoku yang SEARAH
    (Bu-OB dekat S-level = bullish reversal di support -> masuk akal;
    Be-OB dekat R-level = bearish reversal di resistance -> masuk akal).
    Kombinasi yang tidak searah (misal Bu-OB dekat R-level) tidak dihitung,
    karena secara logika dua sinyal itu tidak saling mendukung.
    """
    relevant_levels = ["S1", "S2", "S3"] if ob_type == "Bu-OB" else ["R1", "R2", "R3"]
    aligned = []
    for src, levels in [("Daily", daily), ("Haos", haos)]:
        for lvl in relevant_levels:
            price = levels[lvl]
            gap_pct = abs(ob_close - price) / ob_close * 100
            if gap_pct <= OB_LEVEL_TOLERANCE_PCT:
                aligned.append({"source": src, "level": lvl, "gap_pct": round(gap_pct, 4)})
    return aligned


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
    # 4H & 1H butuh histori lebih panjang untuk hitung ATR/volume MA/swing OB.
    # 1D cukup 3 candle (cuma butuh candle sebelumnya buat Daily Pivot).
    ob_hist_limit = OB_ATR_PERIOD + OB_VOL_PERIOD + 10
    ex_used_4h, df_4h = fetch_ohlcv_with_fallback(symbol, "4h", limit=ob_hist_limit)
    ex_used_1h, df_1h = fetch_ohlcv_with_fallback(symbol, "1h", limit=ob_hist_limit)
    ex_used_1d, df_1d = fetch_ohlcv_with_fallback(symbol, "1d", limit=3)

    last_4h = df_4h.iloc[-1]      # candle 4H yang sedang berjalan/baru closed (dipakai cek sentuhan level real-time)
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

    # OB dievaluasi di candle yang SUDAH CLOSED (bukan candle yang mungkin masih jalan),
    # dicek di 4H dan 1H sekaligus - keduanya dibandingkan ke level pivot yang SAMA
    # (levelnya tidak perlu ganti TF, cuma frekuensi deteksi event-nya yang beda).
    def ob_with_alignment(df):
        hist_closed = df.iloc[:-1].reset_index(drop=True)
        ob = compute_ob(hist_closed)
        alignment = []
        if ob and ob.get("ob_type"):
            alignment = check_ob_pivot_alignment(ob["close"], ob["ob_type"], daily, haos)
        return ob, alignment

    ob_4h, ob_4h_alignment = ob_with_alignment(df_4h)
    ob_1h, ob_1h_alignment = ob_with_alignment(df_1h)

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
        "ob_4h": ob_4h, "ob_4h_alignment": ob_4h_alignment,
        "ob_1h": ob_1h, "ob_1h_alignment": ob_1h_alignment,
    }


STATE_FILE = "data/state.json"


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def main():
    results = {"generated_at": datetime.now(timezone.utc).isoformat(), "symbols": []}

    for sym in SYMBOLS:
        try:
            results["symbols"].append(scan_symbol(sym))
        except Exception as e:
            results["symbols"].append({"symbol": sym, "error": str(e)})

    os.makedirs("data", exist_ok=True)

    # Snapshot terkini (ditimpa tiap run - buat cek cepat, boleh sering di-update)
    with open("data/latest.json", "w") as f:
        json.dump(results, f, indent=2, default=str)

    # Log historis - HANYA ditambah kalau candle-nya benar-benar baru (sudah closed).
    # state per-symbol: "pivot" (candle_time sentuhan level, dari last_4h),
    # "ob_4h" dan "ob_1h" (candle_time konfirmasi OB masing-masing TF) - tiga-tiganya
    # bisa beda candle, jadi di-dedup terpisah.
    state = load_state()
    new_rows = 0
    with open("data/events_log.jsonl", "a") as f:
        for s in results["symbols"]:
            if "error" in s:
                continue

            sym = s["symbol"]
            prev = state.get(sym, {})
            if isinstance(prev, int):  # migrasi dari format state.json paling lama (flat int)
                prev = {"pivot": prev, "ob_4h": None, "ob_1h": None}
            if "ob" in prev and "ob_4h" not in prev:  # migrasi dari format sebelum ada 1H
                prev["ob_4h"] = prev.pop("ob")
                prev.setdefault("ob_1h", None)

            pivot_time = s["last_candle"]["time"]
            if prev.get("pivot") != pivot_time:
                for ev in s["events"]:
                    row = {
                        "generated_at": results["generated_at"], "symbol": sym,
                        "exchange_daily": s["exchange_daily"], "exchange_haoshoku": s["exchange_haoshoku"],
                        "candle_time": pivot_time, "close": s["last_candle"]["close"],
                        "pp_confluence_pct": s["pp_confluence_pct"],
                        "source": ev["source"], "level": ev["level"],
                        "price_level": ev["price_level"], "result": ev["result"],
                    }
                    f.write(json.dumps(row, default=str) + "\n")
                    new_rows += 1

            for tf_key, tf_label in [("ob_4h", "4h"), ("ob_1h", "1h")]:
                ob = s.get(tf_key)
                if ob and ob.get("ob_type") and prev.get(tf_key) != ob["candle_time"]:
                    row = {
                        "generated_at": results["generated_at"], "symbol": sym,
                        "exchange_daily": s["exchange_daily"], "exchange_haoshoku": s["exchange_haoshoku"],
                        "candle_time": ob["candle_time"], "close": ob["close"], "timeframe": tf_label,
                        "source": "OB", "level": ob["ob_type"], "result": ob["ob_type"],
                        "body_ratio": ob.get("body_ratio"), "vol_ratio": ob.get("vol_ratio"),
                        "pivot_alignment": s.get(f"{tf_key}_alignment", []),
                        "combined_signal": bool(s.get(f"{tf_key}_alignment")),
                    }
                    f.write(json.dumps(row, default=str) + "\n")
                    new_rows += 1

            state[sym] = {
                "pivot": pivot_time,
                "ob_4h": s["ob_4h"]["candle_time"] if s.get("ob_4h") else prev.get("ob_4h"),
                "ob_1h": s["ob_1h"]["candle_time"] if s.get("ob_1h") else prev.get("ob_1h"),
            }

    save_state(state)
    print(f"Scan selesai: {len(SYMBOLS)} pair, {new_rows} event baru dicatat (sisanya candle lama/duplikat di-skip)")


if __name__ == "__main__":
    main()
