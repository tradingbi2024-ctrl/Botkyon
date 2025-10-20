# -*- coding: utf-8 -*-
# KYON v2.2 Lite — Visual Edition
# Integración doble: Yahoo Finance + TwelveData
# Compatible con Pydroid3 y Render
# Autor: Ener & ChatGPT (2025)

import os, csv, time, json, requests
from flask import Flask, render_template_string, request, jsonify
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any
from PyPDF2 import PdfReader

# =============================
# CONFIGURACIÓN BASE
# =============================
APP_NAME = "KYON v2.2 Lite"
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
API_KEY = "1d9ec4d09cbc4085b55188ddd5d7cbbb"

PAIRS = ["EURUSD","GBPUSD","USDJPY","AUDUSD","NZDUSD",
         "USDCAD","USDCHF","XAUUSD","XAGUSD","BTCUSD"]

YH_TICKER = {
    "EURUSD":"EURUSD=X","GBPUSD":"GBPUSD=X","USDJPY":"JPY=X",
    "AUDUSD":"AUDUSD=X","NZDUSD":"NZDUSD=X","USDCAD":"USDCAD=X",
    "USDCHF":"USDCHF=X","XAUUSD":"XAUUSD=X","XAGUSD":"XAGUSD=X","BTCUSD":"BTC-USD"
}
INVERT_TICKER={"USDJPY":"JPY=X"}
TF_ALLOWED=["5m","15m","1h","4h"]
YH_INTERVAL={"5m":"5m","15m":"15m","1h":"60m","4h":"240m"}
YH_RANGE={"5m":"7d","15m":"30d","1h":"60d","4h":"730d"}

def apply_tz(dt_utc, offset="+00:00"):
    sign = 1 if offset.startswith("+") or offset.startswith("0") else -1
    hh,mm = offset.replace("+","").replace("-","").split(":")
    delta = timedelta(hours=int(hh),minutes=int(mm))
    if sign<0: delta=-delta
    return (dt_utc+delta).replace(tzinfo=None)

# =============================
# DESCARGA DE DATOS
# =============================
def fetch_yahoo(symbol:str,tf:str)->List[Dict[str,Any]]:
    tkr=YH_TICKER.get(symbol,symbol)
    inv=INVERT_TICKER.get(symbol)
    url=f"https://query1.finance.yahoo.com/v8/finance/chart/{tkr}?interval={YH_INTERVAL[tf]}&range={YH_RANGE[tf]}"
    try:
        r=requests.get(url,timeout=10); j=r.json()
        res=j["chart"]["result"][0]; ts=res["timestamp"]; q=res["indicators"]["quote"][0]
        candles=[]
        for i in range(len(ts)):
            o,h,l,c=[q[k][i] for k in("open","high","low","close")]
            if None in (o,h,l,c): continue
            o,h,l,c=map(float,(o,h,l,c))
            if inv and symbol=="USDJPY": o,h,l,c=1/o,1/l,1/h,1/c
            candles.append({"time":datetime.fromtimestamp(ts[i],tz=timezone.utc),"o":o,"h":h,"l":l,"c":c})
        return candles[-600:]
    except Exception as e:
        print("⚠️ Yahoo:",e); return []

def fetch_twelve(symbol:str,interval="15min",limit=80):
    if "/" not in symbol and len(symbol)>=6: symbol=symbol[:3]+"/"+symbol[3:]
    url=f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={interval}&outputsize={limit}&apikey={API_KEY}"
    try:
        r=requests.get(url,timeout=10); j=r.json()
        if "values" not in j: return []
        vals=j["values"][::-1]
        return [{"time":datetime.fromisoformat(v["datetime"]),
                 "o":float(v["open"]),"h":float(v["high"]),
                 "l":float(v["low"]),"c":float(v["close"])} for v in vals]
    except Exception as e:
        print("⚠️ TwelveData:",e); return []

def get_data(symbol:str,tf:str)->List[Dict[str,Any]]:
    data=fetch_yahoo(symbol,tf)
    if not data:
        print(f"🔄 Yahoo falló, usando TwelveData para {symbol}")
        data=fetch_twelve(symbol.replace('USD','/USD'),"15min" if tf=="15m" else tf)
    return data

# =============================
# INDICADORES
# =============================
def ema(vals,n):
    k=2/(n+1);out=[];e=None
    for v in vals:
        e=v if e is None else v*k+e*(1-k)
        out.append(e)
    return out
def macd_line(close):
    e12,e26=ema(close,12),ema(close,26)
    L=min(len(e12),len(e26))
    macd=[e12[-L+i]-e26[i] for i in range(L)]
    signal=ema(macd,9)
    hist=[m-s for m,s in zip(macd,signal)]
    return macd,signal,hist
def atr(candles,n=14):
    tr=[];out=[];prev=None
    for c in candles:
        t=c["h"]-c["l"] if prev is None else max(c["h"]-c["l"],abs(c["h"]-prev),abs(c["l"]-prev))
        tr.append(t)
        if len(tr)>n: tr.pop(0)
        out.append(sum(tr)/len(tr))
        prev=c["c"]
    return out

# =============================
# ANÁLISIS
# =============================
def analyze(symbol,tf,tz):
    candles=get_data(symbol,tf)
    if len(candles)<30:
        return card(symbol,tf,"🔄 Analizando…","-",tz)
    closes=[c["c"] for c in candles];highs=[c["h"] for c in candles];lows=[c["l"] for c in candles]
    macd,sig,_=macd_line(closes);diff=macd[-1]-sig[-1]
    atr14=atr(candles,14)[-1];rango=highs[-1]-lows[-1]
    close=closes[-1]
    if rango<atr14*0.5:
        d,a="BAJO VOLUMEN","Esperar movimiento"
    elif abs(diff)<0.3:
        d,a="LATERAL","Esperar cruce MACD"
    elif diff>0 and close>sum(highs[-5:])/5:
        d,a="COMPRA","ENTRAR AHORA"
    elif diff<0 and close<sum(lows[-5:])/5:
        d,a="VENTA","ENTRAR AHORA"
    else:
        d,a="ANALIZANDO","Observando mercado"
    entry=close
    sl=entry-atr14 if d=="COMPRA" else entry+atr14
    tp1=entry+2*atr14 if d=="COMPRA" else entry-2*atr14
    tp2=entry+3*atr14 if d=="COMPRA" else entry-3*atr14
    now=datetime.now(timezone.utc);show=apply_tz(now,tz)
    return {
        "symbol":symbol,"tf":tf,"direction":d,"entry":round(entry,5),
        "sl":round(sl,5),"tp1":round(tp1,5),"tp2":round(tp2,5),
        "action":a,"time":show.strftime("%Y-%m-%d %H:%M"),"tz":tz
    }

def card(symbol,tf,msg,price,tz):
    now=datetime.now(timezone.utc);show=apply_tz(now,tz)
    return {"symbol":symbol,"tf":tf,"direction":msg,"entry":price,"sl":"-","tp1":"-","tp2":"-",
            "action":"-","time":show.strftime("%Y-%m-%d %H:%M"),"tz":tz}

# =============================
# PDF CONOCIMIENTO
# =============================
def load_pdfs():
    base={};folder="pdf_knowledge"
    if not os.path.exists(folder):
        os.makedirs(folder,exist_ok=True)
        print("📂 Carpeta creada :",folder)
    for f in os.listdir(folder):
        if f.lower().endswith(".pdf"):
            try:
                txt="".join(PdfReader(os.path.join(folder,f)).pages[i].extract_text() or "" for i in range(len(PdfReader(os.path.join(folder,f)).pages)))
                base[f]=txt
            except: pass
    return base
pdfs=load_pdfs()

# =============================
# FLASK UI
# =============================
app=Flask(__name__)

HTML="""
<!doctype html><html lang='es'><head>
<meta charset='utf-8'><title>KYON v2.2 Lite</title>
<style>
body{background:#0f1419;color:#e6edf3;font-family:system-ui;margin:0;padding:20px;}
.card{padding:12px;margin:8px 0;border-radius:8px}
.buy{background:#002a00;border:1px solid #00ff84;}
.sell{background:#2a0000;border:1px solid #ff6060;}
.wait{background:#1b2230;border:1px solid #344;}
h2{margin-bottom:12px;}
</style></head><body>
<h2>⚙️ KYON v2.2 Lite — Señales ({{tf}})</h2>
{% for c in cards %}
<div class="card {% if 'COMPRA' in c.direction %}buy{% elif 'VENTA' in c.direction %}sell{% else %}wait{% endif %}">
<b>{{c.symbol}} {{c.tf}}</b> → {{c.direction}}<br>
Entrada: {{c.entry}} | SL: {{c.sl}} | TP1: {{c.tp1}} | TP2: {{c.tp2}}<br>
{{c.action}} ({{c.time}} UTC{{c.tz}})
</div>
{% endfor %}
</body></html>
"""

@app.route("/")
def home():
    pair=request.args.get("pair","all");tf=request.args.get("tf","15m");tz=request.args.get("tz","+00:00")
    if tf not in TF_ALLOWED: tf="15m"
    pairs=PAIRS if pair in ("","all") else [pair]
    cards=[analyze(p,tf,tz) for p in pairs]
    return render_template_string(HTML,cards=cards,tf=tf)

@app.route("/kyon_explain")
def explain():
    return jsonify({"answer":"KYON v2.2 análisis activo — MACD + ATR + confluencias en tiempo real."})

if __name__=="__main__":
    print(f"✅ Ejecutando {APP_NAME}")
    port=int(os.environ.get("PORT",5000))
    app.run(host="0.0.0.0",port=port)