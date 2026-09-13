import csv, json, os, threading, time, zipfile
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from flask import Flask, jsonify, send_file, Response

app = Flask(__name__)

PAIRS = {
    "DOGEUSDT": "DOGE-USD",
    "SUIUSDT": "SUI-USD",
    "INJUSDT": "INJ-USD",
}
DAYS = 183
GRANULARITY = 1800          # 30 minutes
CANDLES_PER_CHUNK = 250     # safely below Coinbase 300 max
CHUNK_SECONDS = CANDLES_PER_CHUNK * GRANULARITY

BASE = "/tmp/ha3_fixed"
os.makedirs(BASE, exist_ok=True)
ZIP_PATH = os.path.join(BASE, "HA_3PAIR_6MONTH_30M_FIXED.zip")

state = {"status":"idle","progress":0,"message":"Ready","current_pair":"",
         "completed_pairs":0,"total_pairs":3,"rows":{},"error":None}
lock = threading.Lock()

def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def fetch(product, start, end, retries=6):
    url = "https://api.exchange.coinbase.com/products/{}/candles?{}".format(
        product,
        urlencode({"granularity":GRANULARITY, "start":iso(start), "end":iso(end)})
    )
    last = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={
                "User-Agent":"Mozilla/5.0",
                "Accept":"application/json"
            })
            with urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8")
            data = json.loads(body)
            if not isinstance(data, list):
                raise RuntimeError(body[:500])
            ans = {}
            for x in data:
                if isinstance(x, list) and len(x) >= 6:
                    ts = int(x[0])
                    ans[ts] = [
                        ts,
                        datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                        float(x[3]), float(x[2]), float(x[1]),
                        float(x[4]), float(x[5])
                    ]
            return ans
        except Exception as e:
            last = e
            time.sleep(min(8, 1.5*(attempt+1)))
    raise RuntimeError(str(last))

def build_pair(symbol, product, start, end, pidx):
    # Coinbase requires start/end aligned sensibly; each request <= 250 x 30m.
    rows = {}
    chunks = []
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(seconds=CHUNK_SECONDS), end)
        chunks.append((cur, nxt))
        cur = nxt

    for i, (a,b) in enumerate(chunks, 1):
        rows.update(fetch(product, a, b))
        with lock:
            state["current_pair"] = symbol
            state["rows"][symbol] = len(rows)
            state["progress"] = int(((pidx-1 + i/len(chunks))/len(PAIRS))*100)
            state["message"] = f"{symbol}: {i}/{len(chunks)}"
        time.sleep(.20)

    path = os.path.join(BASE, f"{symbol}_6month_30m.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp","datetime_utc","open","high","low","close","volume"])
        for ts in sorted(rows):
            w.writerow(rows[ts])
    return path

def worker():
    try:
        with lock:
            state.update({"status":"running","progress":0,"message":"Starting...",
                          "current_pair":"","completed_pairs":0,"rows":{},"error":None})

        # Align to the last completed 30-minute boundary.
        now = datetime.now(timezone.utc)
        minute = 30 if now.minute >= 30 else 0
        end = now.replace(minute=minute, second=0, microsecond=0)
        start = end - timedelta(days=DAYS)

        made = []
        failures = []
        for idx,(sym,prod) in enumerate(PAIRS.items(),1):
            try:
                made.append(build_pair(sym,prod,start,end,idx))
            except Exception as e:
                failures.append(f"{sym}: {e}")
            with lock:
                state["completed_pairs"] = idx

        if not made:
            raise RuntimeError("All pairs failed: " + " | ".join(failures))

        if failures:
            err = os.path.join(BASE,"ERRORS.txt")
            Path(err).write_text("\n".join(failures), encoding="utf-8")
            made.append(err)

        with zipfile.ZipFile(ZIP_PATH,"w",zipfile.ZIP_DEFLATED) as z:
            for p in made:
                z.write(p,arcname=os.path.basename(p))

        with lock:
            state.update({"status":"ready","progress":100,
                          "message":f"Done — {len(made)-(1 if failures else 0)}/3 pairs ready",
                          "current_pair":""})
    except Exception as e:
        with lock:
            state.update({"status":"error","message":"Failed","error":repr(e)})

@app.route("/")
def home():
    return """<!doctype html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HA 3 Pair Fixed</title>
<style>
body{font-family:Arial;background:#f4f4f4;padding:18px}.c{max-width:700px;margin:auto;background:#fff;padding:20px;border-radius:14px}
button,a{padding:12px 16px;border:0;border-radius:9px;font-size:16px;margin-top:8px;display:inline-block;text-decoration:none}
button{background:#111;color:#fff}a{background:#1976d2;color:#fff}.bar{height:18px;background:#ddd;border-radius:20px;overflow:hidden;margin-top:15px}
#f{height:100%;width:0;background:#1976d2}</style></head><body><div class=c>
<h2>HA — DOGE + SUI + INJ</h2><p>6 months • 30-minute candles • Fixed Coinbase requests</p>
<button onclick="go()">Prepare Data</button> <a id=d href=/download style="display:none">Download ZIP</a>
<div class=bar><div id=f></div></div><p id=s>Ready</p><small id=x></small>
<script>
async function go(){await fetch('/start',{method:'POST'});poll()}
async function poll(){try{let j=await (await fetch('/status?x='+Date.now())).json();
f.style.width=(j.progress||0)+'%';s.textContent=j.message||j.status;
x.textContent='Progress '+(j.progress||0)+'% | '+(j.completed_pairs||0)+'/3'+(j.current_pair?' | '+j.current_pair:'')+(j.error?' | '+j.error:'');
d.style.display=j.status==='ready'?'inline-block':'none';if(j.status==='running')setTimeout(poll,1200)}catch(e){setTimeout(poll,2000)}}poll()
</script></div></body></html>"""

@app.post("/start")
def start_job():
    with lock:
        if state["status"]=="running": return jsonify(state)
    threading.Thread(target=worker,daemon=True).start()
    return jsonify({"ok":True})

@app.get("/status")
def status(): return jsonify(state)

@app.get("/download")
def download():
    if not os.path.exists(ZIP_PATH): return Response("Not ready",404)
    return send_file(ZIP_PATH,as_attachment=True,download_name="HA_3PAIR_6MONTH_30M_FIXED.zip")

@app.get("/health")
def health(): return jsonify({"ok":True,**state})

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT","8080")),threaded=True)
