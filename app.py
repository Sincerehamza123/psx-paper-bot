import csv
import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import Flask, Response, jsonify, send_file

app = Flask(__name__)

SYMBOL = "XRPUSDT"
PRODUCT_ID = "XRP-USD"
DAYS = 183
GRANULARITY = 60
CHUNK_MINUTES = 299

BASE_DIR = "/tmp/xrp_6month_export"
os.makedirs(BASE_DIR, exist_ok=True)
CSV_PATH = os.path.join(BASE_DIR, "XRPUSDT_6month_1m.csv")

state_lock = threading.Lock()
worker_thread = None
state = {
    "status": "idle",
    "progress": 0,
    "message": "Ready",
    "rows": 0,
    "error": None,
}

def fetch_chunk(start_dt, end_dt, retries=5):
    params = {
        "granularity": GRANULARITY,
        "start": start_dt.isoformat().replace("+00:00", "Z"),
        "end": end_dt.isoformat().replace("+00:00", "Z"),
    }
    url = f"https://api.exchange.coinbase.com/products/{PRODUCT_ID}/candles?" + urlencode(params)
    last_error = None

    for attempt in range(retries):
        try:
            req = Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 XRP-6M-CSV-Exporter",
                    "Accept": "application/json",
                    "Cache-Control": "no-cache",
                },
            )
            with urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))

            if not isinstance(data, list):
                raise RuntimeError(f"Unexpected Coinbase response: {data}")

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

    raise RuntimeError(f"Chunk failed after retries: {last_error}")

def build_csv():
    try:
        with state_lock:
            state.update({
                "status": "running",
                "progress": 0,
                "message": "Starting XRP 6-month export...",
                "rows": 0,
                "error": None,
            })

        end_dt = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start_dt = end_dt - timedelta(days=DAYS)

        chunks = []
        cur = start_dt
        while cur < end_dt:
            chunk_end = min(cur + timedelta(minutes=CHUNK_MINUTES), end_dt)
            chunks.append((cur, chunk_end))
            cur = chunk_end + timedelta(minutes=1)

        all_rows = {}
        total = len(chunks)

        for i, (a, b) in enumerate(chunks, start=1):
            part = fetch_chunk(a, b)
            all_rows.update(part)

            pct = int(i * 100 / total)
            with state_lock:
                state["progress"] = pct
                state["rows"] = len(all_rows)
                state["message"] = f"Fetching XRP data... {pct}%"

            # Slightly slower pacing for a long export to reduce throttling risk.
            time.sleep(0.18)

        tmp = CSV_PATH + ".part"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "datetime_utc", "open", "high", "low", "close", "volume"])
            for ts in sorted(all_rows):
                w.writerow(all_rows[ts])

        os.replace(tmp, CSV_PATH)

        with state_lock:
            state.update({
                "status": "ready",
                "progress": 100,
                "message": "XRP 6-month CSV ready — tap Download CSV",
                "rows": len(all_rows),
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
<title>XRP 6-Month CSV Exporter</title>
<style>
body{font-family:Arial,sans-serif;background:#f5f5f5;padding:20px}
.card{max-width:700px;margin:auto;background:#fff;padding:20px;border-radius:14px}
button,a{display:inline-block;padding:12px 16px;margin:8px 6px 0 0;border-radius:9px;border:0;text-decoration:none;font-size:16px}
button{background:#111;color:#fff}.download{background:#1976d2;color:#fff}
.bar{height:18px;background:#ddd;border-radius:20px;overflow:hidden;margin-top:16px}
.fill{height:100%;width:0;background:#1976d2}
small{display:block;margin-top:10px;color:#555;word-break:break-word}
</style>
</head>
<body>
<div class="card">
<h2>XRPUSDT 6-Month 1-Minute CSV Exporter</h2>
<p>Coinbase XRP-USD public candles ko XRPUSDT-style CSV mein export karta hai.</p>

<button onclick="startJob()">Prepare 6-Month CSV</button>
<a id="download" class="download" href="/download" style="display:none">Download CSV</a>

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
    document.getElementById('details').textContent=
      'Rows: '+(j.rows||0)+(j.error?' | Error: '+j.error:'');
    document.getElementById('download').style.display=
      j.status==='ready'?'inline-block':'none';

    if(j.status==='running') setTimeout(poll,1500);
  }catch(e){
    setTimeout(poll,2500);
  }
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

    worker_thread = threading.Thread(target=build_csv, daemon=True)
    worker_thread.start()
    return jsonify({"ok": True, "message": "XRP 6-month export started."})

@app.get("/status")
def status():
    return jsonify(state)

@app.get("/download")
def download():
    if not os.path.exists(CSV_PATH):
        return Response("CSV not ready yet.", status=404)

    return send_file(
        CSV_PATH,
        as_attachment=True,
        download_name="XRPUSDT_6month_1m.csv",
        mimetype="text/csv",
        max_age=0,
    )

@app.get("/health")
def health():
    return jsonify({"ok": True, "status": state["status"]})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, threaded=True)
