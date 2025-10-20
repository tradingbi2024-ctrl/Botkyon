# -*- coding: utf-8 -*-
# KYON v2.4 Lite - Optimizado para Render Free
# - Prioridad TwelveData → fallback Yahoo
# - Reintento automático si falla conexión
# - Timeout extendido 25s
# - Interfaz profesional + PDFs + CSV historial
# - Compatible con Render y Pydroid3

import os, csv, time, json
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Tuple
import requests
from flask import Flask, render_template_string, request, redirect, url_for, jsonify
from PyPDF2 import PdfReader

# ============================
# CONFIGURACIÓN GENERAL
# ============================
APP_NAME = "KYON v2.4 Lite"
DATA_DIR = "data"
CSV_PATH = os.path.join(DATA_DIR, "kyon_signals.csv")
MAX_KEEP_DAYS = 30
RESULT_WINDOW_HOURS = 48

# API TwelveData (incrustada)
TD_API_KEY = "1d9ec4d09cbc4085b55188ddd5d7cbbb"
TD_INTERVAL_MAP = {"5m": "5min", "15m": "15min", "1h": "1h", "4h": "4h"}

# Pares soportados
PAIRS = [
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD",
    "USDCAD", "USDCHF", "XAUUSD", "XAGUSD", "BTCUSD"
]

# Yahoo Finance: mapeo
YH_TICKER = {
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "JPY=X",
    "AUDUSD": "AUDUSD=X", "NZDUSD": "NZDUSD=X", "USDCAD": "USDCAD=X",
    "USDCHF": "USDCHF=X", "XAUUSD": "XAUUSD=X", "XAGUSD": "XAGUSD=X",
    "BTCUSD": "BTC-USD"
}
INVERT_TICKER = {"USDJPY": "JPY=X"}

TF_ALLOWED = ["5m", "15m", "1h", "4h"]
YH_INTERVAL = {"5m": "5m", "15m": "15m", "1h": "60m", "4h": "240m"}
YH_RANGE = {"5m": "7d", "15m": "30d", "1h": "60d", "4h": "730d"}

TZ_CHOICES = [
    ("UTC-5", "-05:00"), ("UTC-4", "-04:00"), ("UTC-3", "-03:00"),
    ("UTC-2", "-02:00"), ("UTC-1", "-01:00"), ("UTC", "00:00"),
    ("UTC+1", "+01:00"), ("UTC+2", "+02:00"), ("UTC+3", "+03:00"),
    ("UTC+4", "+04:00"), ("UTC+5", "+05:00"), ("UTC+6", "+06:00"),
    ("UTC+7", "+07:00"), ("UTC+8", "+08:00"), ("UTC+9", "+09:00"),
    ("UTC+10", "+10:00")
]

def apply_tz(dt_utc: datetime, offset_str: str) -> datetime:
    sign = 1 if offset_str.startswith("+") or offset_str.startswith("0") else -1
    hh, mm = offset_str.replace("+", "").replace("-", "").split(":")
    delta = timedelta(hours=int(hh), minutes=int(mm))
    if sign < 0:
        delta = -delta
    return (dt_utc + delta).replace(tzinfo=None)

# ============================
# DESCARGA DE DATOS (TwelveData primero)
# ============================
def to_td_symbol(symbol: str) -> str:
    if "/" in symbol: return symbol
    if len(symbol) >= 6: return symbol[:3] + "/" + symbol[3:]
    return symbol

def fetch_twelvedata(symbol: str, timeframe: str, limit: int = 600) -> List[Dict[str, Any]]:
    sym = to_td_symbol(symbol)
    interval = TD_INTERVAL_MAP.get(timeframe, "15min")
    url = f"https://api.twelvedata.com/time_series?symbol={sym}&interval={interval}&outputsize={limit}&apikey={TD_API_KEY}"
    try:
        r = requests.get(url, timeout=25)
        if r.status_code != 200 or "values" not in r.text:
            time.sleep(2)
            r = requests.get(url, timeout=25)
        j = r.json()
        vals = j.get("values")
        if not vals:
            return []
        vals = vals[::-1]
        candles = []
        for v in vals:
            try:
                candles.append({
                    "time": datetime.fromisoformat(v["datetime"].replace("Z", "+00:00")).astimezone(timezone.utc),
                    "o": float(v["open"]), "h": float(v["high"]),
                    "l": float(v["low"]), "c": float(v["close"])
                })
            except Exception:
                continue
        return candles[-600:]
    except Exception as e:
        print(f"TwelveData fallo: {e}")
        return []

# ============================
# Yahoo (fallback)
# ============================
def fetch_yahoo(symbol: str, timeframe: str) -> List[Dict[str, Any]]:
    tkr = YH_TICKER.get(symbol, symbol)
    inv = INVERT_TICKER.get(symbol)
    interval = YH_INTERVAL[timeframe]
    rng = YH_RANGE[timeframe]
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{tkr}?interval={interval}&range={rng}"
    try:
        r = requests.get(url, timeout=25)
        j = r.json()
        res = j["chart"]["result"][0]
        ts = res["timestamp"]
        q = res["indicators"]["quote"][0]
        O, H, L, C = q["open"], q["high"], q["low"], q["close"]
        candles = []
        for i in range(len(ts)):
            if any(v is None for v in (O[i], H[i], L[i], C[i])): continue
            close = float(C[i]); open_ = float(O[i]); high = float(H[i]); low = float(L[i])
            if inv and symbol == "USDJPY":
                close = 1.0 / close
                open_ = 1.0 / open_
                high, low = 1.0 / low, 1.0 / high
            candles.append({
                "time": datetime.fromtimestamp(ts[i], tz=timezone.utc),
                "o": open_, "h": high, "l": low, "c": close
            })
        return candles[-600:]
    except Exception as e:
        print(f"Yahoo fallo para {symbol}: {e}")
        return []

# ============================
# HÍBRIDO: PRIORIDAD TWELVEDATA
# ============================
def fetch_candles_hybrid(symbol: str, timeframe: str) -> List[Dict[str, Any]]:
    c2 = fetch_twelvedata(symbol, timeframe)
    if len(c2) >= 60:
        return c2
    c = fetch_yahoo(symbol, timeframe)
    if len(c) >= 60:
        return c
    return c2 if len(c2) >= len(c) else c

# ============================
# INDICADORES
# ============================
def ema(values: List[float], period: int) -> List[float]:
    k = 2 / (period + 1)
    out = []
    ema_val = None
    for v in values:
        ema_val = v if ema_val is None else (v * k + ema_val * (1 - k))
        out.append(ema_val)
    return out

def macd_line(close: List[float]) -> Tuple[List[float], List[float], List[float]]:
    ema12 = ema(close, 12)
    ema26 = ema(close, 26)
    L = min(len(ema12), len(ema26))
    macd = [ema12[-L + i] - ema26[i] for i in range(L)]
    signal = ema(macd, 9)
    hist = [m - s for m, s in zip(macd, signal)]
    return macd, signal, hist

def true_range(h, l, prev_close): return max(h - l, abs(h - prev_close), abs(l - prev_close))

def atr(candles, period=14):
    out = []; prev = None; q = []
    for c in candles:
        tr = (c["h"] - c["l"]) if prev is None else true_range(c["h"], c["l"], prev)
        q.append(tr)
        if len(q) > period: q.pop(0)
        out.append(sum(q) / len(q))
        prev = c["c"]
    return out

def supertrend(candles, period=10, mult=3.0):
    n = len(candles)
    if n == 0: return []
    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    closes = [c["c"] for c in candles]
    atr_vals = atr(candles, period)
    final_upper, final_lower, trend = [0]*n, [0]*n, [0]*n
    dir_up = True
    for i in range(1, n):
        mid = (highs[i] + lows[i]) / 2
        fu = mid + mult * atr_vals[i]
        fl = mid - mult * atr_vals[i]
        if closes[i] > fu: dir_up = True
        elif closes[i] < fl: dir_up = False
        trend[i] = fl if dir_up else fu
    return trend

def liquidity_sweep(candles, lookback=12):
    if len(candles) < lookback + 2: return "none"
    last = candles[-1]; prevs = candles[-(lookback+2):-1]
    prev_high = max(p["h"] for p in prevs)
    prev_low = min(p["l"] for p in prevs)
    if last["h"] > prev_high and last["c"] < prev_high: return "sell"
    if last["l"] < prev_low and last["c"] > prev_low: return "buy"
    return "none"

def round_price(symbol, p):
    if symbol in ("USDJPY",): return round(p, 3)
    if symbol in ("BTCUSD", "XAUUSD", "XAGUSD"): return round(p, 2)
    return round(p, 5)

# ============================
# SEÑALES
# ============================
def base_card(symbol, tf, msg, tz_disp):
    now_utc = datetime.now(timezone.utc)
    show_time = apply_tz(now_utc, tz_disp)
    return {
        "symbol": symbol, "timeframe": tf, "direction": msg,
        "entry": "-", "sl": "-", "tp1": "-", "tp2": "-", "rr1": "-", "rr2": "-",
        "action": "-", "signal_time_utc": now_utc.isoformat(),
        "signal_time_show": show_time.strftime("%Y-%m-%d %H:%M"), "tz": tz_disp
    }

def make_signal(symbol, tf, tz_disp):
    candles = fetch_candles_hybrid(symbol, tf)
    if len(candles) < 60:
        print(f"⚠️ {symbol}: pocas velas ({len(candles)})")
        return base_card(symbol, tf, "SIN DATOS", tz_disp)
    closes = [c["c"] for c in candles]
    macd, sig, _ = macd_line(closes)
    st = supertrend(candles)
    sweep = liquidity_sweep(candles)
    last = candles[-1]; atr14 = atr(candles)[-1]
    direction = "SIN SEÑAL"
    if macd[-1] > sig[-1] and last["c"] > st[-1] and sweep in ["buy","none"]: direction = "COMPRA"
    elif macd[-1] < sig[-1] and last["c"] < st[-1] and sweep in ["sell","none"]: direction = "VENTA"
    if direction == "SIN SEÑAL": return base_card(symbol, tf, direction, tz_disp)
    entry = last["c"]
    if direction == "COMPRA":
        sl, tp1, tp2 = entry - atr14, entry + 2*atr14, entry + 3*atr14
    else:
        sl, tp1, tp2 = entry + atr14, entry - 2*atr14, entry - 3*atr14
    sl, tp1, tp2 = map(lambda x: round_price(symbol, x), (sl,tp1,tp2))
    print(f"✅ {symbol} {direction} | Entrada {entry}")
    return {
        "symbol": symbol, "timeframe": tf, "direction": direction,
        "entry": round_price(symbol, entry), "sl": sl, "tp1": tp1, "tp2": tp2,
        "rr1": "1:2", "rr2": "1:3", "action": "ENTRAR AHORA",
        "signal_time_utc": datetime.now(timezone.utc).isoformat(),
        "signal_time_show": apply_tz(datetime.now(timezone.utc), tz_disp).strftime("%Y-%m-%d %H:%M"), "tz": tz_disp,
        "explain": f"MACD y SuperTrend indican {direction}. Barrido: {sweep}"
    }

# ============================
# UI (Flask)
# ============================
app = Flask(__name__)
@app.route("/")
def home():
    tf = request.args.get("tf", "15m"); pair = request.args.get("pair", "all")
    tz = request.args.get("tz", "00:00")
    targets = PAIRS if pair in ["all",""] else [pair]
    cards = [make_signal(p, tf, tz) for p in targets]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"<h2>KYON v2.4 Lite</h2><p>Última actualización: {now}</p>" + "<br>".join([f"{c['symbol']} {c['timeframe']} → {c['direction']}" for c in cards])

if __name__ == "__main__":
    print(f"✅ Ejecutando {APP_NAME}")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
