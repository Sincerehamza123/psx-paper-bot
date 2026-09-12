import csv
import json
import os
import threading
import time
import zipfile
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import Flask, Response, jsonify, send_file

app = Flask(__name__)

# 20 widely available Coinbase USD pairs.
PAIRS = {
    "XRPUSDT": "XRP-USD",
    "BTCUSDT": "BTC-USD",
    "ETHUSDT": "ETH-USD",
    "ADAUSDT": "ADA-USD",
    "DOGEUSDT": "DOGE-USD",
    "LINKUSDT": "LINK-USD",
    "AVAXUSDT": "AVAX-USD",
    "DOTUSDT": "DOT-USD",
    "LTCUSDT": "LTC-USD",
    "BCHUSDT": "BCH-USD",
    "ATOMUSDT": "ATOM-USD",
    "NEARUSDT": "NEAR-USD",
    "FILUSDT": "FIL-USD",
    "APTUSDT": "APT-USD",
    "ARBUSDT": "ARB-USD",
    "OPUSDT": "OP-USD",
    "INJUSDT": "INJ-USD",
    "SUIUSDT": "SUI-USD",
    "SEIUSDT": "SEI-USD",
    "ETCUSDT": "ETC-USD",
}

DAYS = 183
GRANULARITY = 60
CHUNK_MINUTES = 299

BASE_DIR = "/tmp/crypto20_6month"
os.makedirs(BASE_DIR, exist_ok=True)
ZIP_PATH = os.path.join(BASE_DIR, "CRYPTO20_6month_1m.zip")

state_lock = threading.Lock()
worker_thread = None
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

def fetch_chunk(product_id, start_dt, end_dt, retries=5):
    params = {
        "granularity": GRANULARITY,
        "start": start_dt.isoformat().replace("+00:00", "Z"),
        "end": end_dt.isoformat().replace("+00:00", "Z"),
    }
    url = f"https://api.exchange.coinbase.com/products/{product_id}/candles?" + urlencode(params)
    last_error = None

    for attempt in range(retries):
        try:
            req = Request(url, headers={
                "User-Agent": "Mozilla/5.0 Crypto20-6M-Exporter",
                "Accept": "application/json",
                "Cache-Control": "no-cache",
            })
            with urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))

            if not isinstance(data, list):
                raise RuntimeError(f"Unexpected response: {data}")

            rows = {}
            for k in data:
                if isinstance(k, list) and len(k) >= 6:
                    ts = int(k[0])
                    rows[ts] = [
                        ts,
                        datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                        float(k[3]),  # open
                        float(k[2]),  # high
                        float(k[1]),  # low
                        float(k[4]),  # close
                        float(k[5]),  # volume
                    ]
            return rows

        except Exception as e:
            last_error = e
            time.sleep(2.0 * (attempt + 1))

    raise RuntimeError(f"{product_id} chunk failed: {last_error}")

def build_pair(symbol, product_id, start_dt, end_dt, pair_index, total_pairs):
    chunks = []
    cur = start_dt
    while cur < end_dt:
        chunk_end = min(cur + timedelta(minutes=CHUNK_MINUTES), end_dt)
        chunks.append((cur, chunk_end))
        cur = chunk_end + timedelta(minutes=1)

    all_rows = {}
    total_chunks = len(chunks)

    for i, (a, b) in enumerate(chunks, start=1):
        part = fetch_chunk(product_id, a, b)
        all_rows.update(part)

        overall = int((((pair_index - 1) + (i / total_chunks)) / total_pairs) * 100)
        with state_lock:
            state["progress"] = overall
            state["current_pair"] = symbol
            state["rows"][symbol] = len(all_rows)
            state["message"] = f"{symbol}: {i}/{total_chunks} chunks"

        time.sleep(0.18)

    path = os.path.join(BASE_DIR, f"{symbol}_6month_1m.csv")
    tmp = path + ".part"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "datetime_utc", "open", "high", "low", "close", "volume"])
        for ts in sorted(all_rows):
            w.writerow(all_rows[ts])

    os.replace(tmp, path)
    return path, len(all_rows)

def build_all():
    try:
        with state_lock:
            state.update({
                "status": "running",
                "progress": 0,
                "message": "Starting 20-pair 6-month export...",
                "current_pair": "",
                "completed_pairs": 0,
                "rows": {},
                "error": None,
            })

        end_dt = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start_dt = end_dt - timedelta(days=DAYS)

        generated = []
        items = list(PAIRS.items())

        for idx, (symbol, product_id) in enumerate(items, start=1):
            try:
                path, nrows = build_pair(symbol, product_id, start_dt, end_dt, idx, len(items))
                generated.append(path)
                with state_lock:
                    state["rows"][symbol] = nrows
                    state["completed_pairs"] = idx
            except Exception as pair_error:
                # Keep going so one unsupported/problem pair doesn't kill all 20.
                err_path = os.path.join(BASE_DIR, f"{symbol}_ERROR.txt")
                with open(err_path, "w", encoding="utf-8") as f:
                    f.write(str(pair_error))
                generated.append(err_path)
                with state_lock:
                    state["rows"][symbol] = 0
                    state["completed_pairs"] = idx
                    state["message"] = f"{symbol} failed; continuing..."

        with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as z:
            for p in generated:
                z.write(p, arcname=os.path.basename(p))

        with state_lock:
            state.update({
                "status": "ready",
                "progress": 100,
                "message": "All done — download the ZIP",
                "current_pair": "",
                "completed_pairs": len(PAIRS),
                "error": None,
            })

    except Exception as e:
        with state_lock:
            state.update({
                "status": "error",
                "message": "Export failed",
                "error": repr(e),
            })

@app.get("/")
def home():
    return f"""<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>20 Crypto Pairs 6-Month Exporter</title>
<style>
body{{font-family:Arial,sans-serif;background:#f5f5f5;padding:20px}}
.card{{max-width:760px;margin:auto;background:white;padding:20px;border-radius:14px}}
button,a{{display:inline-block;padding:12px 16px;margin:8px 6px 0 0;border-radius:9px;border:0;text-decoration:none;font-size:16px}}
button{{background:#111;color:#fff}} .download{{background:#1976d2;color:#fff}}
.bar{{height:18px;background:#ddd;border-radius:20px;overflow:hidden;margin-top:16px}}
.fill{{height:100%;width:0;background:#1976d2}}
small{{display:block;margin-top:10px;color:#555;word-break:break-word}}
</style>
</head>
<body>
<div class="card">
<h2>20 Crypto Pairs — 6-Month 1-Minute CSV Exporter</h2>
<p>Ek hi run mein 20 pairs ka 6-month 1-minute data banega aur akhir mein ek ZIP milegi.</p>

<button onclick="startJob()">Prepare 20 Pairs</button>
<a id="download" class="download" href="/download" style="display:none">Download ZIP</a>

<div class="bar"><div id="fill" class="fill"></div></div>
<p id="status">Ready</p>
<small id="details"></small>
</div>

<script>
async function startJob(){{
  await fetch('/start',{{method:'POST'}});
  poll();
}}
async function poll(){{
  try{{
    let r=await fetch('/status?_='+Date.now());
    let j=await r.json();
    document.getElementById('fill').style.width=(j.progress||0)+'%';
    document.getElementById('status').textContent=j.message||j.status;
    document.getElementById('details').textContent=
      'Progress: '+(j.progress||0)+'% | Completed: '+(j.completed_pairs||0)+'/{len(PAIRS)}'+
      (j.current_pair?' | Current: '+j.current_pair:'')+
      (j.error?' | Error: '+j.error:'');
    document.getElementById('download').style.display=
      j.status==='ready'?'inline-block':'none';
    if(j.status==='running') setTimeout(poll,1800);
  }}catch(e){{setTimeout(poll,2500)}}
}}
poll();
</script>
</body>
</html>"""

@app.post("/start")
def start():
    global worker_thread

    with state_lock:
        if state["status"] == "running":
            return jsonify(state)

    worker_thread = threading.Thread(target=build_all, daemon=True)
    worker_thread.start()
    return jsonify({"ok": True, "message": "20-pair export started."})

@app.get("/status")
def status():
    return jsonify(state)

@app.get("/download")
def download():
    if not os.path.exists(ZIP_PATH):
        return Response("ZIP not ready yet.", status=404)
    return send_file(
        ZIP_PATH,
        as_attachment=True,
        download_name="CRYPTO20_6month_1m.zip",
        mimetype="application/zip",
        max_age=0,
    )

@app.get("/health")
def health():
    return jsonify({"ok": True, "status": state["status"]})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, threaded=True)
