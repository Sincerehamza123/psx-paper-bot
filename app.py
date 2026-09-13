import csv
import json
import os
import threading
import time
import zipfile
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import Flask, jsonify, send_file, Response

app = Flask(__name__)

# 3 relatively high-volatility Coinbase pairs (excluding BTC, ETH, XRP)
PAIRS = {
    "DOGEUSDT": "DOGE-USD",
    "SUIUSDT": "SUI-USD",
    "INJUSDT": "INJ-USD",
}

DAYS = 183
GRANULARITY = 1800  # 30 minutes
CHUNK_CANDLES = 299
CHUNK_MINUTES = CHUNK_CANDLES * 30

BASE_DIR = "/tmp/ha_3pair_30m"
os.makedirs(BASE_DIR, exist_ok=True)
ZIP_PATH = os.path.join(BASE_DIR, "HA_3PAIR_6MONTH_30M.zip")

state = {
    "status": "idle",
    "progress": 0,
    "message": "Ready",
    "current_pair": "",
    "completed_pairs": 0,
    "total_pairs": len(PAIRS),
    "rows": {},
    "error": None,
}
lock = threading.Lock()
worker = None

def fetch_chunk(product_id, start_dt, end_dt, retries=5):
    params = {
        "granularity": GRANULARITY,
        "start": start_dt.isoformat().replace("+00:00", "Z"),
        "end": end_dt.isoformat().replace("+00:00", "Z"),
    }
    url = f"https://api.exchange.coinbase.com/products/{product_id}/candles?" + urlencode(params)
    last_err = None

    for attempt in range(retries):
        try:
            req = Request(url, headers={
                "User-Agent": "Mozilla/5.0 HA-3Pair-Exporter",
                "Accept": "application/json",
                "Cache-Control": "no-cache",
            })
            with urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))

            if not isinstance(data, list):
                raise RuntimeError(f"Unexpected response: {data}")

            out = {}
            for k in data:
                if isinstance(k, list) and len(k) >= 6:
                    ts = int(k[0])
                    out[ts] = [
                        ts,
                        datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                        float(k[3]),
                        float(k[2]),
                        float(k[1]),
                        float(k[4]),
                        float(k[5]),
                    ]
            return out
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))

    raise RuntimeError(f"{product_id} failed: {last_err}")

def build_pair(symbol, product_id, start_dt, end_dt, pair_index):
    chunks = []
    cur = start_dt
    while cur < end_dt:
        chunk_end = min(cur + timedelta(minutes=CHUNK_MINUTES), end_dt)
        chunks.append((cur, chunk_end))
        cur = chunk_end

    all_rows = {}
    total_chunks = len(chunks)

    for i, (a, b) in enumerate(chunks, start=1):
        rows = fetch_chunk(product_id, a, b)
        all_rows.update(rows)
        overall = int((((pair_index - 1) + i / total_chunks) / len(PAIRS)) * 100)
        with lock:
            state["progress"] = overall
            state["current_pair"] = symbol
            state["rows"][symbol] = len(all_rows)
            state["message"] = f"{symbol}: {i}/{total_chunks} chunks"
        time.sleep(0.12)

    path = os.path.join(BASE_DIR, f"{symbol}_6month_30m.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "datetime_utc", "open", "high", "low", "close", "volume"])
        for ts in sorted(all_rows):
            w.writerow(all_rows[ts])
    return path, len(all_rows)

def build_all():
    try:
        with lock:
            state.update({
                "status": "running", "progress": 0, "message": "Starting...",
                "current_pair": "", "completed_pairs": 0, "rows": {}, "error": None,
            })

        end_dt = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start_dt = end_dt - timedelta(days=DAYS)
        generated = []

        for idx, (symbol, product_id) in enumerate(PAIRS.items(), start=1):
            try:
                path, n = build_pair(symbol, product_id, start_dt, end_dt, idx)
                generated.append(path)
                with lock:
                    state["rows"][symbol] = n
                    state["completed_pairs"] = idx
            except Exception as e:
                err_path = os.path.join(BASE_DIR, f"{symbol}_ERROR.txt")
                with open(err_path, "w", encoding="utf-8") as f:
                    f.write(str(e))
                generated.append(err_path)
                with lock:
                    state["completed_pairs"] = idx
                    state["message"] = f"{symbol} failed; continuing..."

        with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as z:
            for p in generated:
                z.write(p, arcname=os.path.basename(p))

        with lock:
            state.update({
                "status": "ready", "progress": 100, "message": "Done — ZIP ready",
                "current_pair": "", "completed_pairs": len(PAIRS),
            })
    except Exception as e:
        with lock:
            state.update({"status": "error", "message": "Exporter failed", "error": repr(e)})

@app.get("/")
def home():
    return """<!doctype html>
<html><head><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>HA 3-Pair 30m Exporter</title>
<style>body{font-family:Arial;background:#f4f4f4;padding:18px}.card{max-width:720px;margin:auto;background:white;padding:20px;border-radius:14px}button,a{padding:12px 16px;border:0;border-radius:9px;font-size:16px;text-decoration:none;display:inline-block;margin-top:8px}button{background:#111;color:white}.download{background:#1976d2;color:white}.bar{height:18px;background:#ddd;border-radius:20px;overflow:hidden;margin-top:15px}.fill{height:100%;width:0;background:#1976d2}small{display:block;margin-top:10px;color:#555}</style></head>
<body><div class=\"card\"><h2>Heikin Ashi Test — 3 High-Volatility Pairs</h2><p>DOGE + SUI + INJ | 6 months | 30-minute candles</p>
<button onclick=\"startJob()\">Prepare Data</button><a id=\"download\" class=\"download\" href=\"/download\" style=\"display:none\">Download ZIP</a>
<div class=\"bar\"><div id=\"fill\" class=\"fill\"></div></div><p id=\"status\">Ready</p><small id=\"details\"></small></div>
<script>
async function startJob(){await fetch('/start',{method:'POST'});poll();}
async function poll(){try{let r=await fetch('/status?_='+Date.now());let j=await r.json();fill.style.width=(j.progress||0)+'%';status.textContent=j.message||j.status;details.textContent='Progress: '+(j.progress||0)+'% | Completed: '+(j.completed_pairs||0)+'/'+j.total_pairs+(j.current_pair?' | Current: '+j.current_pair:'')+(j.error?' | Error: '+j.error:'');download.style.display=j.status==='ready'?'inline-block':'none';if(j.status==='running')setTimeout(poll,1200);}catch(e){setTimeout(poll,2000);}}poll();
</script></body></html>"""

@app.post("/start")
def start():
    global worker
    with lock:
        if state["status"] == "running":
            return jsonify(state)
    worker = threading.Thread(target=build_all, daemon=True)
    worker.start()
    return jsonify({"ok": True})

@app.get("/status")
def status():
    return jsonify(state)

@app.get("/download")
def download():
    if not os.path.exists(ZIP_PATH):
        return Response("ZIP not ready yet", status=404)
    return send_file(ZIP_PATH, as_attachment=True, download_name="HA_3PAIR_6MONTH_30M.zip")

@app.get("/health")
def health():
    return jsonify({"ok": True, **state})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True)
