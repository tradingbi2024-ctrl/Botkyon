# -*- coding: utf-8 -*-
# KYON v2 Lite - Web (Flask)
# Versión estable con conexión a TwelveData, lectura de PDFs, memoria y análisis estadístico

import os, csv, math, time, json, threading
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Tuple
import requests
from flask import Flask, render_template_string, request, redirect, url_for, jsonify
from PyPDF2 import PdfReader

# ---------------------------------------------
# Conexión a API real de mercado (TwelveData)
# ---------------------------------------------
def obtener_datos_reales(simbolo="EUR/USD", intervalo="1min", limite=50):
    try:
        api_key = os.getenv("TWELVEDATA_API_KEY")
        if not api_key:
            print("⚠️ No hay clave TWELVEDATA_API_KEY configurada.")
            else:
    print("✅ Clave API detectada: " + api_key[:5] + "********")
            return []
        url = f"https://api.twelvedata.com/time_series?symbol={simbolo}&interval={intervalo}&outputsize={limite}&apikey={api_key}"
        r = requests.get(url, timeout=10)
        datos = r.json()
        if "values" in datos:
            velas = [{
                "datetime": v["datetime"],
                "open": float(v["open"]),
                "high": float(v["high"]),
                "low": float(v["low"]),
                "close": float(v["close"]),
                "volume": float(v["volume"])
            } for v in datos["values"]]
            return velas[::-1]
        else:
            print("Error API:", datos.get("message", "Error desconocido"))
            return []
    except Exception as e:
        print("Error al conectar con Twelve Data:", e)
        return []

# ---------------------------------------------
# Configuración general
# ---------------------------------------------
APP_NAME = "KYON v2 Lite"
DATA_DIR = os.path.abspath(os.path.dirname(__file__))
CSV_PATH = os.path.join(DATA_DIR, "kyon_signals.csv")
MAX_KEEP_DAYS = 30
RESULT_WINDOW_HOURS = 48

# ---------------------------------------------
# Pares y configuración Yahoo Finance
# ---------------------------------------------
PAIRS = [
    "EURUSD","GBPUSD","USDJPY","AUDUSD","NZDUSD","USDCAD",
    "USDCHF","XAUUSD","XAGUSD","BTCUSD"
]
YH_TICKER = {
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "JPY=X",
    "AUDUSD": "AUDUSD=X", "NZDUSD": "NZDUSD=X", "USDCAD": "USDCAD=X",
    "USDCHF": "USDCHF=X", "XAUUSD": "XAUUSD=X", "XAGUSD": "XAGUSD=X",
    "BTCUSD": "BTC-USD"
}
INVERT_TICKER = {"USDJPY": "JPY=X"}
TF_ALLOWED = ["5m","15m","1h","4h"]
YH_INTERVAL = {"5m":"5m","15m":"15m","1h":"60m","4h":"240m"}
YH_RANGE = {"5m":"7d","15m":"30d","1h":"60d","4h":"730d"}

# ---------------------------------------------
# Zonas horarias
# ---------------------------------------------
TZ_CHOICES = [
    ("UTC-5","-05:00"), ("UTC-4","-04:00"), ("UTC-3","-03:00"),
    ("UTC-2","-02:00"), ("UTC-1","-01:00"), ("UTC","00:00"),
    ("UTC+1","+01:00"), ("UTC+2","+02:00"), ("UTC+3","+03:00"),
    ("UTC+4","+04:00"), ("UTC+5","+05:00"), ("UTC+6","+06:00"),
    ("UTC+7","+07:00"), ("UTC+8","+08:00"), ("UTC+9","+09:00"),
    ("UTC+10","+10:00")
]

def apply_tz(dt_utc: datetime, offset_str: str) -> datetime:
    sign = 1 if offset_str.startswith("+") or offset_str.startswith("0") else -1
    hh, mm = offset_str.replace("+","").replace("-","").split(":")
    delta = timedelta(hours=int(hh), minutes=int(mm))
    if sign < 0:
        delta = -delta
    return (dt_utc + delta).replace(tzinfo=None)

# ---------------------------------------------
# Descarga de velas Yahoo Finance
# ---------------------------------------------
def fetch_yahoo(symbol: str, timeframe: str) -> List[Dict[str,Any]]:
    tkr = YH_TICKER.get(symbol, symbol)
    inv = INVERT_TICKER.get(symbol)
    interval = YH_INTERVAL[timeframe]
    rng = YH_RANGE[timeframe]
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{tkr}?interval={interval}&range={rng}"
    try:
        r = requests.get(url, timeout=12)
        j = r.json()
        res = j["chart"]["result"][0]
        ts = res["timestamp"]
        q = res["indicators"]["quote"][0]
        O, H, L, C = q["open"], q["high"], q["low"], q["close"]
        candles = []
        for i in range(len(ts)):
            if any(v is None for v in (O[i],H[i],L[i],C[i])):
                continue
            close = float(C[i])
            open_ = float(O[i])
            high = float(H[i])
            low = float(L[i])
            if inv:
                close = 1.0/close
                open_ = 1.0/open_
                high = 1.0/low
                low = 1.0/high
            candles.append({
                "time": datetime.fromtimestamp(ts[i], tz=timezone.utc),
                "o": open_, "h": high, "l": low, "c": close
            })
        return candles[-600:]
    except Exception:
        return []

# ---------------------------------------------
# Indicadores técnicos
# ---------------------------------------------
def ema(values: List[float], period: int) -> List[float]:
    k = 2/(period+1)
    out = []
    ema_val = None
    for v in values:
        ema_val = v if ema_val is None else (v*k + ema_val*(1-k))
        out.append(ema_val)
    return out

def macd_line(close: List[float]) -> Tuple[List[float], List[float], List[float]]:
    ema12 = ema(close,12)
    ema26 = ema(close,26)
    macd = [a-b for a,b in zip(ema12, ema26)]
    signal = ema(macd,9)
    hist = [m-s for m,s in zip(macd, signal)]
    return macd, signal, hist

def true_range(h:float,l:float,prev_close:float)->float:
    return max(h-l, abs(h-prev_close), abs(l-prev_close))

def atr(candles: List[Dict[str,Any]], period:int=14) -> List[float]:
    out=[]
    prev_close=None
    q=[]
    for c in candles:
        if prev_close is None:
            tr=c["h"]-c["l"]
        else:
            tr=true_range(c["h"],c["l"],prev_close)
        q.append(tr)
        if len(q)>period:
            q.pop(0)
        out.append(sum(q)/len(q))
        prev_close=c["c"]
    return out

def supertrend(candles: List[Dict[str,Any]], period:int=10, mult:float=3.0)->List[float]:
    n=len(candles)
    if n==0:
        return []
    highs=[c["h"] for c in candles]
    lows=[c["l"] for c in candles]
    closes=[c["c"] for c in candles]
    atr_vals=atr(candles,period)
    basic_upper=[]
    basic_lower=[]
    for i in range(n):
        mid=(highs[i]+lows[i])/2
        r=atr_vals[i]*mult
        basic_upper.append(mid+r)
        basic_lower.append(mid-r)
    final_upper=[basic_upper[0]]
    final_lower=[basic_lower[0]]
    trend=[0.0]*n
    dir_up=True
    for i in range(1,n):
        fu = basic_upper[i] if (basic_upper[i]<final_upper[i-1] or closes[i-1]>final_upper[i-1]) else final_upper[i-1]
        fl = basic_lower[i] if (basic_lower[i]>final_lower[i-1] or closes[i-1]<final_lower[i-1]) else final_lower[i-1]
        final_upper.append(fu)
        final_lower.append(fl)
        if closes[i]>final_upper[i-1]:
            dir_up=True
        elif closes[i]<final_lower[i-1]:
            dir_up=False
        trend[i]=final_lower[i] if dir_up else final_upper[i]
    return trend

def liquidity_sweep(candles: List[Dict[str,Any]], lookback:int=10)->str:
    if len(candles)<lookback+2:
        return "none"
    last=candles[-1]
    prevs=candles[-(lookback+2):-1]
    prev_high=max(p["h"] for p in prevs)
    prev_low=min(p["l"] for p in prevs)
    if last["h"]>prev_high and last["c"]<prev_high:
        return "sell"
    if last["l"]<prev_low and last["c"]>prev_low:
        return "buy"
    return "none"
    # ---------------------------------------------
# Señales
# ---------------------------------------------
def make_signal(symbol: str, tf: str, tz_disp: str) -> Dict[str, Any]:
    candles = fetch_yahoo(symbol, tf)
    if len(candles) < 60:
        return base_card(symbol, tf, "SIN DATOS", tz_disp)

    closes = [c["c"] for c in candles]
    macd, sig, hist = macd_line(closes)
    st = supertrend(candles, period=10, mult=3.0)
    sweep = liquidity_sweep(candles, lookback=12)

    last = candles[-1]
    atr14 = atr(candles, 14)[-1]
    direction = "SIN SEÑAL"
    entry = last["c"]

    # Confluencia:
    # - MACD>signal y close>SuperTrend y sweep=buy -> COMPRA
    # - MACD<signal y close<SuperTrend y sweep=sell -> VENTA
    if macd[-1] > sig[-1] and last["c"] > st[-1] and (sweep in ["buy", "none"]):
        direction = "COMPRA"
    if macd[-1] < sig[-1] and last["c"] < st[-1] and (sweep in ["sell", "none"]):
        direction = "VENTA"

    if direction == "SIN SEÑAL":
        return base_card(symbol, tf, "SIN SEÑAL", tz_disp)

    # TP/SL por ATR, RR1=1:2, RR2=1:3
    if direction == "COMPRA":
        sl = round_price(symbol, entry - 1.0 * atr14)
        tp1 = round_price(symbol, entry + 2.0 * atr14)
        tp2 = round_price(symbol, entry + 3.0 * atr14)
        action = "ENTRAR AHORA"
    else:
        sl = round_price(symbol, entry + 1.0 * atr14)
        tp1 = round_price(symbol, entry - 2.0 * atr14)
        tp2 = round_price(symbol, entry - 3.0 * atr14)
        action = "ENTRAR AHORA"

    now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)
    show_time = apply_tz(now_utc, tz_disp)

    card = {
        "id": f"{symbol}-{int(now_utc.timestamp())}",
        "symbol": symbol,
        "timeframe": tf,
        "direction": direction,
        "entry": round_price(symbol, entry),
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "rr1": "1:2",
        "rr2": "1:3",
        "action": action,
        "signal_time_utc": now_utc.isoformat(),
        "signal_time_show": show_time.strftime("%Y-%m-%d %H:%M"),
        "tz": tz_disp,
        "explain": (
            f"Confluencia: MACD {'>' if macd[-1] > sig[-1] else '<'} señal, "
            f"precio {'sobre' if last['c'] > st[-1] else 'bajo'} SuperTrend, "
            f"barrido de liquidez: {sweep}."
        ),
    }
    return card


def base_card(symbol: str, tf: str, msg: str, tz_disp: str) -> Dict[str, Any]:
    now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)
    show_time = apply_tz(now_utc, tz_disp)
    return {
        "id": f"{symbol}-{int(now_utc.timestamp())}",
        "symbol": symbol,
        "timeframe": tf,
        "direction": msg,
        "entry": "-",
        "sl": "-",
        "tp1": "-",
        "tp2": "-",
        "rr1": "-",
        "rr2": "-",
        "action": "-",
        "signal_time_utc": now_utc.isoformat(),
        "signal_time_show": show_time.strftime("%Y-%m-%d %H:%M"),
        "tz": tz_disp,
        "explain": "Sin suficientes datos.",
    }


def round_price(symbol: str, p: float) -> float:
    # redondeo por tipo (JPY 3 decimales, BTC 2, oro/plata 2, demás 5)
    if symbol in ("USDJPY",):
        return round(p, 3)
    if symbol in ("BTCUSD",):
        return round(p, 2)
    if symbol in ("XAUUSD", "XAGUSD"):
        return round(p, 2)
    return round(p, 5)

# ---------------------------------------------
# CSV (señales tomadas)
# ---------------------------------------------
CSV_HEADERS = [
    "id","symbol","timeframe","direction","entry","sl","tp1","tp2","rr1","rr2",
    "signal_time_utc","tz","taken","taken_time_utc","result","closed_time_utc","pips"
]

def load_taken() -> List[Dict[str, str]]:
    if not os.path.exists(CSV_PATH):
        return []
    rows = []
    with open(CSV_PATH, "r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            rows.append(row)
    # limpiar >30 días
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    keep = []
    for row in rows:
        t = row.get("taken_time_utc") or row["signal_time_utc"]
        try:
            dt_ = datetime.fromisoformat(t.replace("Z", "+00:00"))
        except:
            dt_ = now
        if (now - dt_).days <= MAX_KEEP_DAYS:
            keep.append(row)
    if len(keep) != len(rows):
        save_taken(keep)
    return keep

def save_taken(rows: List[Dict[str, str]]):
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        w.writeheader()
        for row in rows:
            w.writerow(row)

def add_taken(card: Dict[str, Any]):
    rows = load_taken()
    # si ya existe, no duplicar
    if any(r["id"] == card["id"] for r in rows):
        return
    rows.append({
        "id": card["id"], "symbol": card["symbol"], "timeframe": card["timeframe"],
        "direction": card["direction"], "entry": str(card["entry"]), "sl": str(card["sl"]),
        "tp1": str(card["tp1"]), "tp2": str(card["tp2"]), "rr1": card["rr1"], "rr2": card["rr2"],
        "signal_time_utc": card["signal_time_utc"], "tz": card["tz"],
        "taken": "1", "taken_time_utc": datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(),
        "result": "ABIERTA", "closed_time_utc": "", "pips": ""
    })
    save_taken(rows)

def pip_value(symbol: str) -> float:
    # tamaño del pip (no monetario), solo para conteo de pips
    if symbol in ("USDJPY",):
        return 0.01
    if symbol in ("XAUUSD","XAGUSD","BTCUSD"):
        return 0.1
    return 0.0001

def evaluate_open_positions():
    rows = load_taken()
    changed = False
    for r in rows:
        if r.get("result", "ABIERTA") != "ABIERTA":
            continue
        symbol = r["symbol"]
        price_now = current_price(symbol)
        if price_now is None:
            continue
        entry = float(r["entry"]); sl = float(r["sl"]); tp1 = float(r["tp1"])
        # evaluar cruce hacia TP1 o SL
        res = None
        if r["direction"] == "COMPRA":
            if price_now >= tp1: res = "GANADORA"
            elif price_now <= sl: res = "PERDEDORA"
        elif r["direction"] == "VENTA":
            if price_now <= tp1: res = "GANADORA"
            elif price_now >= sl: res = "PERDEDORA"

        if res:
            r["result"] = res
            r["closed_time_utc"] = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
            # Guardar memoria práctica
            guardar_memoria_mercado(symbol, r["timeframe"], r["direction"], res, r["result"])
            # pips:
            pipsize = pip_value(symbol)
            if res == "GANADORA":
                pips = abs(tp1 - entry) / pipsize
            else:
                pips = abs(sl - entry) / pipsize
            r["pips"] = str(round(pips, 1))
            changed = True

    if changed:
        save_taken(rows)

def current_price(symbol: str) -> float | None:
    # último cierre de Yahoo (intervalo 5m) para evaluar rápidamente
    candles = fetch_yahoo(symbol, "5m")
    if not candles:
        return None
    return candles[-1]["c"]

def recent_stats_48h() -> Dict[str, int]:
    rows = load_taken()
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    wins = 0; loss = 0; open_ = 0
    for r in rows:
        try:
            t = datetime.fromisoformat((r.get("taken_time_utc") or r["signal_time_utc"]).replace("Z","+00:00"))
        except:
            t = now
        if (now - t).total_seconds() > RESULT_WINDOW_HOURS * 3600:
            continue
        st = r.get("result", "ABIERTA")
        if st == "GANADORA": wins += 1
        elif st == "PERDEDORA": loss += 1
        else: open_ += 1
    return {"wins": wins, "loss": loss, "open": open_}

# ---------------------------------------------
# KYON: respuesta explicativa (con conocimiento PDF)
# ---------------------------------------------
def kyon_explain(card: Dict[str, Any]) -> str:
    # Usa el conocimiento cargado desde los PDFs
    respuesta = ""
    for nombre, contenido in memoria_conocimiento.items():
        if any(palabra in contenido.lower() for palabra in ["estructura", "liquidez", "confirmación", "entrada", "stop", "tp"]):
            respuesta += f"\n📘 Según {nombre}: el documento trata conceptos clave aplicables a esta señal."
            break

    return (
        f"Hola, soy KYON. Para {card['symbol']} en {card['timeframe']} detecté: "
        f"{card['explain']} Usé MACD(12,26,9), SuperTrend(10,3) y un chequeo de liquidez "
        f"simple (barrido de altos/bajos recientes). SL y TPs se basan en ATR(14). "
        f"RR mínimos: {card['rr1']} y {card['rr2']}. Recuerda validar con tu plan de trading."
        + respuesta
    )

# ---------------------------------------------
# Lectura de PDFs de conocimiento
# ---------------------------------------------
def cargar_pdf_conocimiento(ruta_pdf: str) -> str:
    try:
        reader = PdfReader(ruta_pdf)
        texto = ""
        for page in reader.pages:
            contenido = page.extract_text()
            if contenido:
                texto += contenido + "\n"
        return texto
    except Exception as e:
        print(f"Error al leer {ruta_pdf}: {e}")
        return ""

def inicializar_conocimiento() -> Dict[str, str]:
    carpeta = "pdf_knowledge"
    base_conocimiento = {}
    if not os.path.exists(carpeta):
        os.makedirs(carpeta, exist_ok=True)
        print("📂 Carpeta creada: pdf_knowledge (sube tus PDFs allí)")
    for archivo in os.listdir(carpeta):
        if archivo.lower().endswith(".pdf"):
            nombre_clave = os.path.splitext(archivo)[0]
            ruta = os.path.join(carpeta, archivo)
            print(f"Cargando conocimiento de: {archivo}")
            base_conocimiento[nombre_clave] = cargar_pdf_conocimiento(ruta)
    return base_conocimiento

memoria_conocimiento = inicializar_conocimiento()

# ---------------------------------------------
# MEMORIA DE MERCADO (aprendizaje adaptativo)
# ---------------------------------------------
def guardar_memoria_mercado(simbolo: str, timeframe: str, direction: str, resultado: str, explicacion: str):
    """
    Guarda cada análisis y resultado en memoria_mercado.csv para aprendizaje futuro.
    """
    fecha = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    ruta = os.path.join(DATA_DIR, "memoria_mercado.csv")
    existe = os.path.exists(ruta)
    with open(ruta, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not existe:
            writer.writerow(["fecha", "simbolo", "timeframe", "direction", "resultado", "explicacion"])
        writer.writerow([fecha, simbolo, timeframe, direction, resultado, explicacion])

def cargar_memoria_mercado() -> List[Dict[str, str]]:
    """
    Carga la memoria previa para consultar estadísticas o patrones pasados.
    """
    ruta = os.path.join(DATA_DIR, "memoria_mercado.csv")
    historial = []
    if os.path.exists(ruta):
        with open(ruta, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for fila in reader:
                historial.append(fila)
    return historial

def resumen_memoria() -> str:
    """
    Devuelve un resumen de aciertos y errores históricos.
    """
    memoria = cargar_memoria_mercado()
    if not memoria:
        return "Aún no hay historial de operaciones registradas."
    wins = sum(1 for m in memoria if m["resultado"] == "GANADORA")
    losses = sum(1 for m in memoria if m["resultado"] == "PERDEDORA")
    return f"📈 Historial: {wins} ganadoras · {losses} perdedoras · Total: {len(memoria)} análisis"

# ---------------------------------------------
# ANÁLISIS ESTADÍSTICO DE LA MEMORIA
# ---------------------------------------------
def analizar_memoria_detallada() -> str:
    """
    Analiza la memoria de mercado y genera un resumen con estadísticas útiles.
    """
    registros = cargar_memoria_mercado()
    if not registros:
        return "No hay datos suficientes aún."

    total = len(registros)
    ganadoras = [r for r in registros if r["resultado"] == "GANADORA"]
    perdedoras = [r for r in registros if r["resultado"] == "PERDEDORA"]

    # Ratio de acierto
    winrate = (len(ganadoras) / total * 100) if total else 0.0

    # PARES más rentables
    pares = {}
    for r in registros:
        par = r["simbolo"]
        pares.setdefault(par, {"ganadas": 0, "perdidas": 0})
        if r["resultado"] == "GANADORA":
            pares[par]["ganadas"] += 1
        elif r["resultado"] == "PERDEDORA":
            pares[par]["perdidas"] += 1

    ranking_pares = sorted(
        [(p, v["ganadas"], v["perdidas"]) for p, v in pares.items()],
        key=lambda x: x[1],
        reverse=True
    )

    # TEMPORALIDADES
    tfs = {}
    for r in registros:
        tf = r["timeframe"]
        tfs.setdefault(tf, {"ganadas": 0, "perdidas": 0})
        if r["resultado"] == "GANADORA":
            tfs[tf]["ganadas"] += 1
        elif r["resultado"] == "PERDEDORA":
            tfs[tf]["perdidas"] += 1

    ranking_tf = sorted(
        [(t, v["ganadas"], v["perdidas"]) for t, v in tfs.items()],
        key=lambda x: x[1],
        reverse=True
    )

    resumen = (
        f"📊 ANÁLISIS DE MEMORIA DE KYON\n"
        f"───────────────────────────────\n"
        f"Total operaciones: {total}\n"
        f"Ganadoras: {len(ganadoras)}\n"
        f"Perdedoras: {len(perdedoras)}\n"
        f"Winrate: {winrate:.1f}%\n\n"
        f"💱 Mejores pares:\n"
    )

    for p, g, l in ranking_pares[:5]:
        resumen += f"• {p}: {g} ganadas / {l} perdidas\n"

    resumen += "\n⏱️ Temporalidades más efectivas:\n"
    for t, g, l in ranking_tf:
        resumen += f"• {t}: {g} ganadas / {l} perdidas\n"

    return resumen

# ---------------------------------------------
# Flask (UI)
# ---------------------------------------------
app = Flask(__name__)

HTML = """
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Bot Forex Analyzer ({{app_name}})</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#0f1419;--panel:#151a21;--card:#1b2230;--text:#e6edf3;--muted:#9aa7b0;--accent:#ffd166;--danger:#ff6b6b;--ok:#3bd671;}
*{box-sizing:border-box;}
body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,"Helvetica Neue",sans-serif;}
.header{display:flex;gap:12px;align-items:center;justify-content:space-between;padding:14px 18px;background:#0d1117;position:sticky;top:0;z-index:5;border-bottom:1px solid #202938;}
.title{font-size:20px;font-weight:700;}
.main{display:grid;grid-template-columns:260px 1fr 320px;gap:16px;padding:16px;}
.panel{background:var(--panel);border:1px solid #222a3a;border-radius:12px;padding:14px;}
.btn{background:#263244;color:var(--text);padding:8px 12px;border-radius:8px;border:1px solid #32425c;cursor:pointer}
.btn:hover{filter:brightness(1.05)}
.btn-yellow{background:#3a2a00;border-color:#614a00;color:#ffd166;}
.btn-red{background:#3a0000;border-color:#5c0000;color:#ff9d9d;}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.select,.input{background:#0f1622;border:1px solid #2a3953;color:var(--text);padding:7px 10px;border-radius:8px}
.badge{font-size:11px;color:#c7d1db;background:#1e2634;border:1px solid #2a3142;padding:2px 6px;border-radius:6px}
.card{background:var(--card);border:1px solid #243149;border-radius:12px;padding:12px;margin-bottom:12px}
.card h3{margin:0 0 6px 0;font-size:16px}
.kpi{display:flex;gap:10px;align-items:center;font-size:13px;color:var(--muted)}
.kv{display:grid;grid-template-columns:120px 1fr;gap:4px 10px;font-size:14px;margin-top:8px}
.sep{height:1px;background:#233049;margin:10px 0}
.pair-pill{display:inline-block;padding:6px 10px;border:1px solid #2c3b55;border-radius:30px;background:#131a24;color:#d9e1ea;cursor:pointer}
.pair-pill.active{border-color:#3a62ff;background:#0e1840}
.right small{color:var(--muted)}
.fs-btn{background:#0c1220;border:1px solid #2a3a5e;color:#a8c1ff;border-radius:8px;padding:6px 10px}
</style>
</head>
<body>
<div class="header">
  <div class="title">Bot Forex Analyzer ({{app_name}})</div>
  <div class="row">
    <button class="fs-btn" onclick="toggleFS()">Pantalla completa</button>
    <div class="row">
      <label class="badge">Temporalidad</label>
      <select id="tf" class="select" onchange="refresh()">
        {% for t in tf_allowed %}<option value="{{t}}" {% if t==tf %}selected{% endif %}>{{t}}</option>{% endfor %}
      </select>
      <label class="badge">Zona</label>
      <select id="tz" class="select" onchange="refresh()">
        {% for name,off in tz_choices %}<option value="{{off}}" {% if off==tz %}selected{% endif %}>{{name}}</option>{% endfor %}
      </select>
      <label class="badge">Auto</label>
      <select id="auto" class="select">
        <option value="off" {% if auto=='off' %}selected{% endif %}>OFF</option>
        <option value="5" {% if auto=='5' %}selected{% endif %}>cada 5 min</option>
        <option value="15" {% if auto=='15' %}selected{% endif %}>cada 15 min</option>
      </select>
      <button class="btn" onclick="applyAuto()">Aplicar</button>
      <button class="btn-yellow" onclick="refresh()">Actualizar análisis</button>
    </div>
  </div>
</div>

<div class="main">
  <!-- Pares -->
  <div class="panel">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <div style="font-weight:700">Pares</div>
      <span class="badge">Última: {{last_update}}</span>
    </div>
    <div class="row" style="gap:8px;flex-wrap:wrap">
      <span class="pair-pill {% if active=='all' %}active{% endif %}" onclick="setPair('all')">Todos</span>
      {% for p in pairs %}
      <span class="pair-pill {% if active==p %}active{% endif %}" onclick="setPair('{{p}}')">{{p}}</span>
      {% endfor %}
    </div>
  </div>

  <!-- Señales -->
  <div class="panel">
    {% for c in cards %}
    <div class="card">
      <h3>{{c.symbol}} · {{c.timeframe}} 
        <span class="badge">{% if c.direction in ['COMPRA','VENTA'] %}SEÑAL{% else %}{{c.direction}}{% endif %}</span>
      </h3>
      <div class="kpi">
        <span>Hora señal: {{c.signal_time_show}} (UTC{{c.tz}})</span>
      </div>
      <div class="kv">
        <div>Dirección:</div><div><b>{{c.direction}}</b></div>
        <div>Entrada:</div><div>{{c.entry}}</div>
        <div>StopLoss:</div><div>{{c.sl}}</div>
        <div>TakeProfit 1 (1:2):</div><div>{{c.tp1}}</div>
        <div>TakeProfit 2 (1:3):</div><div>{{c.tp2}}</div>
        <div>Acción:</div><div>{{c.action}}</div>
      </div>
      <div class="sep"></div>
      <div class="row">
        <button class="btn" onclick="ask('{{c.id}}')">Preguntar a KYON</button>
        {% if c.direction in ['COMPRA','VENTA'] %}
        <form method="post" action="{{url_for('take_signal')}}">
          <input type="hidden" name="payload" value='{{c|tojson}}'>
          <button class="btn-yellow" type="submit">Tomada</button>
        </form>
        {% endif %}
      </div>
      <div id="ans-{{c.id}}" style="margin-top:8px;color:#cfe2ff;font-size:13px"></div>
    </div>
    {% endfor %}
  </div>

  <!-- Resultados -->
  <div class="panel right">
    <div style="font-weight:700;margin-bottom:8px">Resultados (48 h)</div>
    <div class="kv">
      <div>Ganadoras:</div><div style="color:var(--ok)">{{stats.wins}}</div>
      <div>Perdedoras:</div><div style="color:var(--danger)">{{stats.loss}}</div>
      <div>Abiertas:</div><div>{{stats.open}}</div>
    </div>
    <div class="sep"></div>
    <small>Solo se guardan señales <b>Tomadas</b> por el usuario (máx 30 días, CSV).</small>
    <div class="sep"></div>
    <form method="post" action="{{url_for('eval_close')}}">
      <button class="btn" type="submit">Evaluar cierres</button>
    </form>
    <div class="sep"></div>
    <div style="font-weight:700;margin-bottom:8px">Historial tomado (últimos 30 días)</div>
    {% if taken %}
      {% for r in taken %}
        <div class="card" style="padding:10px">
          <div style="display:flex;justify-content:space-between">
            <div><b>{{r.symbol}}</b> · {{r.timeframe}} · {{r.direction}}</div>
            <div class="badge">{{r.result}}</div>
          </div>
          <div class="kv" style="font-size:13px;margin-top:6px">
            <div>Entrada:</div><div>{{r.entry}}</div>
            <div>SL:</div><div>{{r.sl}}</div>
            <div>TP1:</div><div>{{r.tp1}}</div>
            <div>Tomada:</div><div>{{r.taken_time_utc.replace('T',' ')[:16]}} UTC</div>
            <div>Cierre:</div><div>{{ (r.closed_time_utc or '-').replace('T',' ')[:16] }}</div>
            <div>Pips:</div><div>{{r.pips}}</div>
          </div>
        </div>
      {% endfor %}
    {% else %}
      <small>No hay operaciones tomadas aún.</small>
    {% endif %}
  </div>
</div>

<script>
function setPair(p){ 
  const tf=document.getElementById('tf').value;
  const tz=document.getElementById('tz').value;
  const auto=document.getElementById('auto').value;
  window.location='/?pair='+p+'&tf='+tf+'&tz='+encodeURIComponent(tz)+'&auto='+auto;
}
function refresh(){
  const params=new URLSearchParams(window.location.search);
  const pair=params.get('pair')||'all';
  const tf=document.getElementById('tf').value;
  const tz=document.getElementById('tz').value;
  const auto=document.getElementById('auto').value;
  window.location='/?pair='+pair+'&tf='+tf+'&tz='+encodeURIComponent(tz)+'&auto='+auto+'&_='+(+new Date());
}
function applyAuto(){ refresh(); }
let timer=null;
(function auto(){
  const auto="{{auto}}";
  if(timer) clearInterval(timer);
  if(auto==='5'){ timer=setInterval(()=>refresh(), 5*60*1000); }
  if(auto==='15'){ timer=setInterval(()=>refresh(), 15*60*1000); }
})();
function toggleFS(){
  const docEl=document.documentElement;
  if(!document.fullscreenElement){ docEl.requestFullscreen?.(); }
  else { document.exitFullscreen?.(); }
}
async function ask(id){
  const res=await fetch('/kyon_explain?id='+encodeURIComponent(id));
  const data=await res.json();
  document.getElementById('ans-'+id).textContent=data.answer;
}
</script>
</body>
</html>
"""

# cache simple en memoria (para la vista actual)
CACHE = {"cards": [], "args": {}}

def build_cards(pair: str, tf: str, tz: str) -> List[Dict[str, Any]]:
    cards = []
    targets = PAIRS if pair in ("", "all", None) else [pair]
    for p in targets:
        cards.append(make_signal(p, tf, tz))
        time.sleep(0.15)  # amable con Yahoo
    return cards

@app.route("/")
def home():
    pair = request.args.get("pair", "all")
    tf = request.args.get("tf", "15m")
    if tf not in TF_ALLOWED:
        tf = "15m"
    tz = request.args.get("tz", "00:00")
    auto = request.args.get("auto", "off")

    # cards (recalcula si cambian args)
    args = {"pair": pair, "tf": tf, "tz": tz}
    if CACHE["args"] == args and CACHE["cards"]:
        cards = CACHE["cards"]
    else:
        cards = build_cards(pair, tf, tz)
        CACHE["cards"] = cards
        CACHE["args"] = args

    stats = recent_stats_48h()
    taken = load_taken()
    last_update = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    return render_template_string(
        HTML,
        app_name=APP_NAME, pairs=PAIRS, tf_allowed=TF_ALLOWED,
        tf=tf, tz=tz, tz_choices=TZ_CHOICES, cards=cards, stats=stats,
        taken=taken, last_update=last_update, active=pair, auto=auto
    )

@app.post("/take")
def take_signal():
    payload = request.form.get("payload")
    card = json.loads(payload)
    add_taken(card)
    return redirect(url_for("home", pair=request.args.get("pair","all"),
                            tf=request.args.get("tf","15m"),
                            tz=request.args.get("tz","00:00"),
                            auto=request.args.get("auto","off")))

@app.post("/eval")
def eval_close():
    # evalúa cierres y regresa a home
    evaluate_open_positions()
    return redirect(url_for("home", pair=request.args.get("pair","all"),
                            tf=request.args.get("tf","15m"),
                            tz=request.args.get("tz","00:00"),
                            auto=request.args.get("auto","off")))

@app.get("/kyon_explain")
def kyon_explain_api():
    id_ = request.args.get("id")
    # busca la card en cache
    for c in CACHE.get("cards", []):
        if c["id"] == id_:
            return jsonify({"answer": kyon_explain(c)})
    return jsonify({"answer": "No encontré la señal en pantalla. Actualiza y vuelve a intentar."})

# alias rutas
app.add_url_rule("/refresh", "refresh", home)
app.add_url_rule("/take", "take_signal", take_signal, methods=["POST"])
app.add_url_rule("/eval", "eval_close", eval_close, methods=["POST"])

if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(CSV_PATH):
        save_taken([])
    print(f"✅ Ejecutando {APP_NAME}")

    # (Opcional) Mostrar autoanálisis al arrancar
    try:
        print(analizar_memoria_detallada())
    except Exception as e:
        print("Nota análisis memoria:", e)

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
