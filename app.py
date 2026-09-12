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

PAIRS = {
    "BTCUSDT": "BTC-USD",
    "ETHUSDT": "ETH-USD",
}
DAYS = 60
GRANULARITY = 60
CHUNK_MINUTES = 299

BASE_DIR = "/tmp/crypto_export"
os.makedirs(BASE_DIR, exist_ok=True)

state_lock = threading.Lock()
worker_thread = None
state = {
    "status": "idle",
    "progress": 0,
    "message": "Ready",
    "rows": {},
    "error": None,
}

def fetch_chunk(product_id, start_dt, end_dt, retries=4):
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
                "User-Agent": "Mozilla/5.0 BTC-ETH-CSV-Exporter",
                "Accept": "application/json",
            })
            with urlopen(req, timeout=25) as r:
                data = json.loads(r.read().decode("utf-8"))

            rows = {}
            for k in data:
                if isinstance(k, list) and len(k) >= 6:
                    ts = int(k[0])
                    rows[ts] = [
                        ts,
                        datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                        float(k[3]), float(k[2]), float(k[1]),
                        float(k[4]), float(k[5])
                    ]
            return rows
        except Exception as e:
            last_error = e
            time.sleep(1.5 * (attempt + 1))

    raise RuntimeError(str(last_error))

def build_one(symbol, product_id, base_start, base_end, pair_index, total_pairs):
    chunks = []
    cur = base_start
    while cur < base_end:
        chunk_end = min(cur + timedelta(minutes=CHUNK_MINUTES), base_end)
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
            state["rows"][symbol] = len(all_rows)
            state["message"] = f"{symbol}: fetching... {i}/{total_chunks}"

        time.sleep(0.12)

    path = os.path.join(BASE_DIR, f"{symbol}_60day_1m.csv")
    with open(path + ".part", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "datetime_utc", "open", "high", "low", "close", "volume"])
        for ts in sorted(all_rows):
            w.writerow(all_rows[ts])

    os.replace(path + ".part", path)
    return path, len(all_rows)

def build_all():
    try:
        with state_lock:
            state.update({
                "status": "running",
                "progress": 0,
                "message": "Starting BTC + ETH export...",
                "rows": {},
                "error": None,
            })

        end_dt = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start_dt = end_dt - timedelta(days=DAYS)

        generated = []
        items = list(PAIRS.items())

        for idx, (symbol, product_id) in enumerate(items, start=1):
            path, nrows = build_one(symbol, product_id, start_dt, end_dt, idx, len(items))
            generated.append(path)
            with state_lock:
                state["rows"][symbol] = nrows

        zip_path = os.path.join(BASE_DIR, "BTC_ETH_60day_1m.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for p in generated:
                z.write(p, arcname=os.path.basename(p))

        with state_lock:
            state.update({
                "status": "ready",
                "progress": 100,
                "message": "BTC + ETH files ready",
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
    return """<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BTC + ETH 60-Day CSV Exporter</title>
<style>
body{font-family:Arial,sans-serif;background:#f5f5f5;padding:20px}
.card{max-width:700px;margin:auto;background:#fff;padding:20px;border-radius:14px}
button,a{display:inline-block;padding:12px 16px;margin:8px 6px 0 0;border-radius:9px;border:0;text-decoration:none;font-size:16px}
button{background:#111;color:white}.download{background:#1976d2;color:#fff}
.bar{height:18px;background:#ddd;border-radius:20px;overflow:hidden;margin-top:16px}
.fill{height:100%;width:0;background:#1976d2}
small{display:block;margin-top:10px;color:#555}
</style>
</head>
<body>
<div class="card">
<h2>BTC + ETH 60-Day 1-Minute CSV Exporter</h2>
<p>Ek hi click mein BTC aur ETH dono 60-day 1-minute CSV prepare hongi.</p>

<button onclick="startJob()">Prepare BTC + ETH</button>

<div id="downloads" style="display:none">
  <a class="download" href="/download/btc">Download BTC CSV</a>
  <a class="download" href="/download/eth">Download ETH CSV</a>
  <a class="download" href="/download/zip">Download Both ZIP</a>
</div>

<div class="bar"><div id="fill" class="fill"></div></div>
<p id="status">Ready</p>
<small id="details"></small>
</div>

<script>
async function startJob(){
  await fetch('/start',{method:'POST'});
  poll();
}
async function poll(){
  try{
    let r=await fetch('/status?_='+Date.now());
    let j=await r.json();
    document.getElementById('fill').style.width=(j.progress||0)+'%';
    document.getElementById('status').textContent=j.message||j.status;
    let rows=j.rows||{};
    document.getElementById('details').textContent=
      'BTC rows: '+(rows.BTCUSDT||0)+' | ETH rows: '+(rows.ETHUSDT||0)+
      (j.error?' | '+j.error:'');
    document.getElementById('downloads').style.display=
      j.status==='ready'?'block':'none';
    if(j.status==='running') setTimeout(poll,1500);
  }catch(e){setTimeout(poll,2500)}
}
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
    return jsonify({"ok": True})

@app.get("/status")
def status():
    return jsonify(state)

@app.get("/download/btc")
def download_btc():
    path = os.path.join(BASE_DIR, "BTCUSDT_60day_1m.csv")
    if not os.path.exists(path):
        return Response("BTC CSV not ready yet.", status=404)
    return send_file(path, as_attachment=True, download_name="BTCUSDT_60day_1m.csv", mimetype="text/csv", max_age=0)

@app.get("/download/eth")
def download_eth():
    path = os.path.join(BASE_DIR, "ETHUSDT_60day_1m.csv")
    if not os.path.exists(path):
        return Response("ETH CSV not ready yet.", status=404)
    return send_file(path, as_attachment=True, download_name="ETHUSDT_60day_1m.csv", mimetype="text/csv", max_age=0)

@app.get("/download/zip")
def download_zip():
    path = os.path.join(BASE_DIR, "BTC_ETH_60day_1m.zip")
    if not os.path.exists(path):
        return Response("ZIP not ready yet.", status=404)
    return send_file(path, as_attachment=True, download_name="BTC_ETH_60day_1m.zip", mimetype="application/zip", max_age=0)

@app.get("/health")
def health():
    return jsonify({"ok": True, "status": state["status"]})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, threaded=True)
