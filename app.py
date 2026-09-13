import csv, json, os, zipfile, threading
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from flask import Flask, jsonify, send_file, Response

app = Flask(__name__)

PAIRS = {
    "XRPUSDT": "XRP-USD", "BTCUSDT": "BTC-USD", "ETHUSDT": "ETH-USD",
    "ADAUSDT": "ADA-USD", "DOGEUSDT": "DOGE-USD", "LINKUSDT": "LINK-USD",
    "AVAXUSDT": "AVAX-USD", "DOTUSDT": "DOT-USD", "LTCUSDT": "LTC-USD",
    "BCHUSDT": "BCH-USD", "ATOMUSDT": "ATOM-USD", "NEARUSDT": "NEAR-USD",
    "FILUSDT": "FIL-USD", "APTUSDT": "APT-USD", "ARBUSDT": "ARB-USD",
    "OPUSDT": "OP-USD", "INJUSDT": "INJ-USD", "SUIUSDT": "SUI-USD",
    "SEIUSDT": "SEI-USD", "ETCUSDT": "ETC-USD",
}

DAYS = 183
GRANULARITY = 86400
OUTDIR = "/tmp/crypto20_daily"
ZIP_PATH = os.path.join(OUTDIR, "CRYPTO20_6month_DAILY.zip")
os.makedirs(OUTDIR, exist_ok=True)

state = {"status":"idle","progress":0,"message":"Ready","completed":0,"total":len(PAIRS),"failed":[]}
lock = threading.Lock()

def fetch_daily(product_id, start_dt, end_dt):
    params = {
        "granularity": GRANULARITY,
        "start": start_dt.isoformat().replace("+00:00", "Z"),
        "end": end_dt.isoformat().replace("+00:00", "Z"),
    }
    url = f"https://api.exchange.coinbase.com/products/{product_id}/candles?" + urlencode(params)
    req = Request(url, headers={"User-Agent":"Mozilla/5.0 Crypto20-Daily-Exporter","Accept":"application/json"})
    with urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not isinstance(data, list):
        raise RuntimeError(str(data))
    rows = []
    for k in data:
        if isinstance(k, list) and len(k) >= 6:
            ts = int(k[0])
            rows.append([ts, datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(), float(k[3]), float(k[2]), float(k[1]), float(k[4]), float(k[5])])
    rows.sort(key=lambda x: x[0])
    return rows

def build():
    with lock:
        state.update({"status":"running","progress":0,"message":"Starting...","completed":0,"failed":[]})
    end_dt = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start_dt = end_dt - timedelta(days=DAYS)
    files = []

    for i, (symbol, product_id) in enumerate(PAIRS.items(), start=1):
        try:
            rows = fetch_daily(product_id, start_dt, end_dt)
            path = os.path.join(OUTDIR, f"{symbol}_6month_daily.csv")
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["timestamp","datetime_utc","open","high","low","close","volume"])
                w.writerows(rows)
            files.append(path)
            msg = f"{symbol} done ({len(rows)} daily candles)"
        except Exception as e:
            with lock:
                state["failed"].append(f"{symbol}: {e}")
            msg = f"{symbol} failed, continuing"
        with lock:
            state["completed"] = i
            state["progress"] = int(i / len(PAIRS) * 100)
            state["message"] = msg

    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, arcname=os.path.basename(p))

    with lock:
        state["status"] = "ready"
        state["progress"] = 100
        state["message"] = f"Done. {len(files)}/{len(PAIRS)} pairs ready."

@app.get("/")
def home():
    return f'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Fast 20 Pair Daily Exporter</title><style>body{{font-family:Arial;background:#f4f4f4;padding:18px}}.card{{max-width:700px;margin:auto;background:white;padding:20px;border-radius:14px}}button,a{{padding:12px 16px;border:0;border-radius:8px;font-size:16px;text-decoration:none;display:inline-block;margin-top:8px}}button{{background:#111;color:#fff}}a{{background:#1976d2;color:#fff}}.bar{{height:18px;background:#ddd;border-radius:20px;overflow:hidden;margin-top:15px}}.fill{{height:100%;background:#1976d2;width:0}}</style></head><body><div class="card"><h2>Fast 20-Pair 6-Month DAILY Exporter</h2><p>Signal-count test ke liye 1-minute data ki zaroorat nahi. Yeh 20 pairs ka 6-month daily data bohat jaldi banayega.</p><button onclick="go()">Prepare Data</button><a id="d" href="/download" style="display:none">Download ZIP</a><div class="bar"><div id="f" class="fill"></div></div><p id="s">Ready</p><script>async function go(){{await fetch('/start',{{method:'POST'}});poll();}}async function poll(){{let r=await fetch('/status?_='+Date.now());let j=await r.json();f.style.width=(j.progress||0)+'%';s.textContent=(j.message||j.status)+' | '+(j.completed||0)+'/'+j.total;d.style.display=j.status==='ready'?'inline-block':'none';if(j.status==='running')setTimeout(poll,1000);}}poll();</script></div></body></html>'''

@app.post("/start")
def start():
    with lock:
        if state["status"] == "running":
            return jsonify(state)
    threading.Thread(target=build, daemon=True).start()
    return jsonify({"ok":True})

@app.get("/status")
def status():
    return jsonify(state)

@app.get("/download")
def download():
    if not os.path.exists(ZIP_PATH):
        return Response("ZIP not ready", status=404)
    return send_file(ZIP_PATH, as_attachment=True, download_name="CRYPTO20_6month_DAILY.zip")

@app.get("/health")
def health():
    return jsonify({"ok":True, **state})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True)
