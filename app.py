import csv, json, os, threading, time, zipfile
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from flask import Flask, jsonify, send_file, Response

app = Flask(__name__)

# 3 higher-volatility pairs, excluding BTC/ETH/XRP
PAIRS = {
    "DOGEUSDT": "DOGE-USD",
    "SUIUSDT": "SUI-USD",
    "INJUSDT": "INJ-USD",
}

DAYS = 183
SRC_GRANULARITY = 900   # 15m supported by Coinbase
SRC_CHUNK_CANDLES = 250
SRC_CHUNK_SECONDS = SRC_GRANULARITY * SRC_CHUNK_CANDLES

BASE = "/tmp/ha3_30m_from_15m"
os.makedirs(BASE, exist_ok=True)
ZIP_PATH = os.path.join(BASE, "HA_3PAIR_6MONTH_30M_FROM_15M.zip")

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

def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def fetch_15m(product, start_dt, end_dt, retries=6):
    params = {
        "granularity": SRC_GRANULARITY,
        "start": iso(start_dt),
        "end": iso(end_dt),
    }
    url = f"https://api.exchange.coinbase.com/products/{product}/candles?" + urlencode(params)
    last_err = None

    for attempt in range(retries):
        try:
            req = Request(url, headers={
                "User-Agent": "Mozilla/5.0 HA-3Pair-30m-Exporter",
                "Accept": "application/json",
                "Cache-Control": "no-cache",
            })
            with urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8")

            data = json.loads(body)
            if not isinstance(data, list):
                raise RuntimeError(body[:500])

            out = {}
            for x in data:
                if isinstance(x, list) and len(x) >= 6:
                    ts = int(x[0])
                    out[ts] = {
                        "timestamp": ts,
                        "open": float(x[3]),
                        "high": float(x[2]),
                        "low": float(x[1]),
                        "close": float(x[4]),
                        "volume": float(x[5]),
                    }
            return out

        except Exception as e:
            last_err = e
            time.sleep(min(8, 1.5 * (attempt + 1)))

    raise RuntimeError(f"{product}: {last_err}")

def combine_to_30m(rows15):
    # Coinbase candle timestamps are bucket starts.
    # Pair :00 + :15 -> :00 30m candle, :30 + :45 -> :30 30m candle.
    buckets = {}
    for ts, r in rows15.items():
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        minute30 = 0 if dt.minute < 30 else 30
        bucket_dt = dt.replace(minute=minute30, second=0, microsecond=0)
        bts = int(bucket_dt.timestamp())
        buckets.setdefault(bts, []).append(r)

    rows30 = {}
    for bts, arr in buckets.items():
        arr.sort(key=lambda x: x["timestamp"])
        # Require both 15m candles for a complete 30m candle.
        if len(arr) != 2:
            continue
        rows30[bts] = [
            bts,
            datetime.fromtimestamp(bts, timezone.utc).isoformat(),
            arr[0]["open"],
            max(x["high"] for x in arr),
            min(x["low"] for x in arr),
            arr[-1]["close"],
            sum(x["volume"] for x in arr),
        ]
    return rows30

def build_pair(symbol, product, start_dt, end_dt, pidx):
    chunks = []
    cur = start_dt
    while cur < end_dt:
        nxt = min(cur + timedelta(seconds=SRC_CHUNK_SECONDS), end_dt)
        chunks.append((cur, nxt))
        cur = nxt

    all15 = {}
    for i, (a, b) in enumerate(chunks, 1):
        part = fetch_15m(product, a, b)
        all15.update(part)

        overall = int(((pidx - 1 + i / len(chunks)) / len(PAIRS)) * 100)
        with lock:
            state["progress"] = overall
            state["current_pair"] = symbol
            state["rows"][symbol] = f"{len(all15)} x 15m"
            state["message"] = f"{symbol}: {i}/{len(chunks)} chunks"

        time.sleep(0.18)

    rows30 = combine_to_30m(all15)

    path = os.path.join(BASE, f"{symbol}_6month_30m.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "datetime_utc", "open", "high", "low", "close", "volume"])
        for ts in sorted(rows30):
            w.writerow(rows30[ts])

    with lock:
        state["rows"][symbol] = len(rows30)

    return path, len(rows30)

def worker():
    try:
        with lock:
            state.update({
                "status": "running",
                "progress": 0,
                "message": "Starting...",
                "current_pair": "",
                "completed_pairs": 0,
                "rows": {},
                "error": None,
            })

        # Align end to the last completed 15-minute boundary.
        now = datetime.now(timezone.utc)
        aligned_minute = (now.minute // 15) * 15
        end_dt = now.replace(minute=aligned_minute, second=0, microsecond=0)
        start_dt = end_dt - timedelta(days=DAYS)

        generated = []
        failures = []

        for idx, (symbol, product) in enumerate(PAIRS.items(), 1):
            try:
                p, n = build_pair(symbol, product, start_dt, end_dt, idx)
                generated.append(p)
            except Exception as e:
                failures.append(f"{symbol}: {e}")

            with lock:
                state["completed_pairs"] = idx

        if not generated:
            raise RuntimeError("All pairs failed: " + " | ".join(failures))

        if failures:
            err_path = os.path.join(BASE, "ERRORS.txt")
            Path(err_path).write_text("\n".join(failures), encoding="utf-8")
            generated.append(err_path)

        with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as z:
            for p in generated:
                z.write(p, arcname=os.path.basename(p))

        good = len([p for p in generated if p.endswith(".csv")])
        with lock:
            state.update({
                "status": "ready",
                "progress": 100,
                "message": f"Done — {good}/3 pairs ready",
                "current_pair": "",
            })

    except Exception as e:
        with lock:
            state.update({
                "status": "error",
                "message": "Failed",
                "error": repr(e),
            })

@app.get("/")
def home():
    return """<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HA 3-Pair 30m Exporter</title>
<style>
body{font-family:Arial;background:#f4f4f4;padding:18px}
.card{max-width:720px;margin:auto;background:#fff;padding:20px;border-radius:14px}
button,a{padding:12px 16px;border:0;border-radius:9px;font-size:16px;margin-top:8px;display:inline-block;text-decoration:none}
button{background:#111;color:#fff}.download{background:#1976d2;color:#fff}
.bar{height:18px;background:#ddd;border-radius:20px;overflow:hidden;margin-top:15px}
.fill{height:100%;width:0;background:#1976d2}
small{display:block;margin-top:10px;color:#555;word-break:break-word}
</style>
</head>
<body>
<div class="card">
<h2>HA — DOGE + SUI + INJ</h2>
<p>6 months • 30-minute candles built from Coinbase 15-minute data</p>
<button onclick="go()">Prepare Data</button>
<a id="d" class="download" href="/download" style="display:none">Download ZIP</a>

<div class="bar"><div id="f" class="fill"></div></div>
<p id="s">Ready</p>
<small id="x"></small>
</div>

<script>
async function go(){
  await fetch('/start',{method:'POST'});
  poll();
}
async function poll(){
  try{
    let r = await fetch('/status?_='+Date.now());
    let j = await r.json();

    document.getElementById('f').style.width=(j.progress||0)+'%';
    document.getElementById('s').textContent=j.message||j.status;
    document.getElementById('x').textContent=
      'Progress '+(j.progress||0)+'% | '+(j.completed_pairs||0)+'/3'+
      (j.current_pair?' | '+j.current_pair:'')+
      (j.error?' | '+j.error:'');

    document.getElementById('d').style.display=
      j.status==='ready'?'inline-block':'none';

    if(j.status==='running') setTimeout(poll,1200);
  }catch(e){
    setTimeout(poll,2000);
  }
}
poll();
</script>
</body>
</html>"""

@app.post("/start")
def start_job():
    with lock:
        if state["status"] == "running":
            return jsonify(state)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True})

@app.get("/status")
def status():
    return jsonify(state)

@app.get("/download")
def download():
    if not os.path.exists(ZIP_PATH):
        return Response("ZIP not ready", status=404)

    return send_file(
        ZIP_PATH,
        as_attachment=True,
        download_name="HA_3PAIR_6MONTH_30M_FROM_15M.zip",
    )

@app.get("/health")
def health():
    return jsonify({"ok": True, **state})

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        threaded=True
    )
