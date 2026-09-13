
import os, time, json, threading, urllib.parse, urllib.request
from datetime import datetime, timezone
from flask import Flask, jsonify, Response

app = Flask(__name__)
BASE = "https://www.okx.com"

CAPITAL = 100.0
LEVERAGE = 5.0
NOTIONAL = 500.0
TP_PCT = 0.8
SL_PCT = 0.4
DAILY_TARGET = 3.0
DAILY_STOP = -2.0
VOL_MULT = 1.5
RSI_MIN, RSI_MAX = 50.0, 68.0
FEE = 0.0005
SLIP = 0.0001

COINS = [
    "BTC-USDT","ETH-USDT","SOL-USDT","XRP-USDT","DOGE-USDT",
    "ADA-USDT","LINK-USDT","AVAX-USDT","SUI-USDT","LTC-USDT",
    "BCH-USDT","DOT-USDT","NEAR-USDT","APT-USDT","INJ-USDT"
]

state = {
    "position": None,
    "trades": [],
    "candidates": [],
    "daily_pnl": 0.0,
    "day": None,
    "last_scan": None,
    "error": None
}
lock = threading.RLock()

def get(path, params=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        obj = json.loads(r.read().decode())
    if obj.get("code") != "0":
        raise RuntimeError(obj.get("msg","OKX error"))
    return obj["data"]

def bars(inst, tf, limit=100):
    raw = get("/api/v5/market/candles", {"instId":inst,"bar":tf,"limit":str(limit)})
    out=[]
    for r in raw:
        if len(r) < 9: continue
        out.append({"ts":int(r[0]),"o":float(r[1]),"h":float(r[2]),"l":float(r[3]),
                    "c":float(r[4]),"v":float(r[5]),"qv":float(r[7]),"ok":str(r[8])})
    out.sort(key=lambda x:x["ts"])
    return [x for x in out if x["ok"]=="1"]

def ema(vals, n):
    if len(vals)<n: return None
    e=sum(vals[:n])/n
    k=2/(n+1)
    for v in vals[n:]: e=v*k+e*(1-k)
    return e

def rsi(vals, n=14):
    if len(vals)<n+1: return None
    gains=[]; losses=[]
    for i in range(1,n+1):
        d=vals[i]-vals[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    ag=sum(gains)/n; al=sum(losses)/n
    for i in range(n+1,len(vals)):
        d=vals[i]-vals[i-1]
        ag=(ag*(n-1)+max(d,0))/n
        al=(al*(n-1)+max(-d,0))/n
    if al==0: return 100.0
    rs=ag/al
    return 100-100/(1+rs)

def reset_day():
    d=datetime.now(timezone.utc).date().isoformat()
    if state["day"] != d:
        state["day"]=d
        state["daily_pnl"]=0.0

def setup(inst):
    h1=bars(inst,"1H",80)
    m15=bars(inst,"15m",100)
    if len(h1)<55 or len(m15)<25: return None

    c1=[x["c"] for x in h1[-60:]]
    e20=ema(c1,20); e50=ema(c1,50); rr=rsi(c1[-30:],14)
    if e20 is None or e50 is None or rr is None: return None
    if not (e20>e50 and h1[-1]["c"]>e20 and RSI_MIN<=rr<=RSI_MAX): return None

    b1,b2=m15[-2],m15[-1]
    prior=m15[-22:-2]
    swing=max(x["h"] for x in prior)
    avgv=sum(x["v"] for x in prior)/len(prior)

    breakout = b1["c"]>swing and b1["c"]>b1["o"] and b1["v"]>=VOL_MULT*avgv
    confirm = b2["c"]>b2["o"]
    if not (breakout and confirm): return None

    retest = b2["l"]<=swing and b2["c"]>swing
    return {
        "coin":inst,"entry":b2["c"],"rsi":rr,
        "ema20":e20,"ema50":e50,
        "volx":b1["v"]/avgv if avgv else 0,
        "swing":swing,"mode":"RETEST" if retest else "2GREEN"
    }

def rank_candidates():
    rows=[]
    for inst in COINS:
        try:
            s=setup(inst)
            if s: rows.append(s)
        except Exception:
            pass
    rows.sort(key=lambda x:(x["volx"], x["rsi"]), reverse=True)
    for i,x in enumerate(rows,1): x["rank"]=i
    return rows

def enter():
    if state["position"] or not state["candidates"]: return
    if state["daily_pnl"]>=DAILY_TARGET or state["daily_pnl"]<=DAILY_STOP: return
    if any(t.get("day")==state["day"] for t in state["trades"]): return

    p=state["candidates"][0]
    entry=p["entry"]
    qty=NOTIONAL/entry
    state["position"]={
        "coin":p["coin"],"entry":entry,"qty":qty,
        "tp":entry*(1+TP_PCT/100),"sl":entry*(1-SL_PCT/100),
        "day":state["day"],"opened":datetime.now(timezone.utc).isoformat(),
        "rank":p["rank"],"mode":p["mode"],"volx":p["volx"],"rsi":p["rsi"]
    }

def update_position():
    p=state["position"]
    if not p: return
    t=get("/api/v5/market/ticker",{"instId":p["coin"]})[0]
    last=float(t["last"])
    reason=None; px=None
    if last>=p["tp"]: reason="TP"; px=p["tp"]
    elif last<=p["sl"]: reason="SL"; px=p["sl"]
    elif datetime.now(timezone.utc).date().isoformat()!=p["day"]:
        reason="DAY_END"; px=last
    if not reason: return

    gross=p["qty"]*(px-p["entry"])
    exit_notional=p["qty"]*px
    costs=(NOTIONAL+exit_notional)*(FEE+SLIP)
    net=gross-costs
    tr=dict(p)
    tr.update({"exit":px,"reason":reason,"net":net,"closed":datetime.now(timezone.utc).isoformat()})
    state["trades"].append(tr)
    state["trades"]=state["trades"][-100:]
    state["daily_pnl"]+=net
    state["position"]=None

def scan():
    try:
        with lock:
            reset_day()
            update_position()
            if state["position"] is None:
                state["candidates"]=rank_candidates()
                enter()
            state["last_scan"]=datetime.now(timezone.utc).isoformat()
            state["error"]=None
    except Exception as e:
        state["error"]=repr(e)

def loop():
    while True:
        scan()
        time.sleep(60)

@app.get("/api/status")
def status():
    with lock: return jsonify(state)

@app.get("/api/scan")
def scan_now():
    scan()
    return status()

HTML = r"""<!doctype html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Daily $3 Target Paper Bot</title>
<style>
body{margin:0;background:#071019;color:#eef6ff;font-family:Arial;padding:14px}.w{max-width:1000px;margin:auto}
.c{background:#111d29;border:1px solid #27394b;border-radius:14px;padding:14px;margin-bottom:12px}
.g{color:#6ff0a0}.r{color:#ff9999}.y{color:#ffd479}.sub{color:#a8bacb;font-size:13px;line-height:1.5}
.grid{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}.k{background:#09141e;padding:10px;border-radius:9px}.v{font-size:20px;font-weight:bold}
table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:8px;border-bottom:1px solid #253645;text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}.scroll{overflow:auto}
button{padding:10px 14px;border:0;border-radius:8px;background:#387df3;color:white;font-weight:bold}
@media(max-width:700px){.grid{grid-template-columns:1fr 1fr}}
</style></head><body><div class=w>
<div class=c><h2>Daily $3 Target — PAPER BOT</h2>
<div class=sub">Profit guarantee nahi. Goal: 1 trade/day, 1H bullish trend, RSI 50–68, 15m breakout + 1.5x volume confirmation.</div></div>
<div class="c grid">
<div class=k><div class=sub>Capital</div><div class=v>$100</div></div>
<div class=k><div class=sub>Leverage</div><div class=v>5x</div></div>
<div class=k><div class=sub>Notional</div><div class=v>$500</div></div>
<div class=k><div class=sub>TP</div><div class=v>0.8%</div></div>
<div class=k><div class=sub>SL</div><div class=v>0.4%</div></div>
</div>
<div class=c><button onclick=go()>Scan Now</button> <span id=m class=sub></span><div id=d></div><div id=p></div></div>
<div class="c scroll"><h3>Candidates</h3>
<table><thead><tr><th>Coin</th><th>Rank</th><th>RSI</th><th>Vol x</th><th>Mode</th><th>Entry</th></tr></thead><tbody id=tb></tbody></table></div>
<div class="c scroll"><h3>Paper Trades</h3>
<table><thead><tr><th>Coin</th><th>Entry</th><th>Exit</th><th>Reason</th><th>Net P/L</th></tr></thead><tbody id=tr></tbody></table></div>
</div><script>
const f=(x,n=4)=>Number(x||0).toFixed(n);
async function load(){
 let j=await(await fetch('/api/status',{cache:'no-store'})).json();
 m.textContent='Last scan: '+(j.last_scan||'—')+(j.error?' | '+j.error:'');
 d.innerHTML=`<p>Daily P/L: <b class="${j.daily_pnl>=0?'g':'r'}">$${f(j.daily_pnl,2)}</b> | stop after +$3 or -$2</p>`;
 p.innerHTML=j.position?`<p><b class=g>OPEN:</b> ${j.position.coin} | Entry ${f(j.position.entry,6)} | TP ${f(j.position.tp,6)} | SL ${f(j.position.sl,6)}</p>`:'<p class=y>No open trade</p>';
 tb.innerHTML='';(j.candidates||[]).forEach(x=>tb.innerHTML+=`<tr><td>${x.coin}</td><td>#${x.rank}</td><td>${f(x.rsi,1)}</td><td>${f(x.volx,2)}x</td><td>${x.mode}</td><td>${f(x.entry,6)}</td></tr>`);
 tr.innerHTML='';[...(j.trades||[])].reverse().forEach(x=>tr.innerHTML+=`<tr><td>${x.coin}</td><td>${f(x.entry,6)}</td><td>${f(x.exit,6)}</td><td>${x.reason}</td><td class="${x.net>=0?'g':'r'}">${f(x.net,2)}</td></tr>`);
}
async function go(){await fetch('/api/scan');await load()}
load();setInterval(load,15000);
</script></body></html>"""

@app.get("/")
def home():
    return Response(HTML,mimetype="text/html")

if __name__=="__main__":
    threading.Thread(target=loop,daemon=True).start()
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT","8080")),threaded=True)
