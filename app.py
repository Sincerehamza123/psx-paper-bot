
import os, time, json, threading, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
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
    "error": None,
    "backtest": {
        "running": False,
        "progress": "",
        "result": None,
        "error": None,
        "last_run": None
    },
    "optimizer": {
        "running": False,
        "progress": "",
        "result": None,
        "error": None,
        "last_run": None
    }
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


def hist_bars(inst, tf, days):
    target = int((datetime.now(timezone.utc)-timedelta(days=days+3)).timestamp()*1000)
    all_rows={}
    after=None
    for _ in range(120):
        params={"instId":inst,"bar":tf,"limit":"100"}
        if after is not None:
            params["after"]=str(after)
        raw=get("/api/v5/market/history-candles", params)
        if not raw:
            break
        batch=[]
        for r in raw:
            if len(r)<9: continue
            b={"ts":int(r[0]),"o":float(r[1]),"h":float(r[2]),"l":float(r[3]),
               "c":float(r[4]),"v":float(r[5]),"qv":float(r[7]),"ok":str(r[8])}
            batch.append(b)
            if b["ok"]=="1":
                all_rows[b["ts"]]=b
        if not batch:
            break
        oldest=min(x["ts"] for x in batch)
        if oldest<=target:
            break
        after=oldest
        time.sleep(0.03)
    out=sorted(all_rows.values(), key=lambda x:x["ts"])
    return [x for x in out if x["ts"]>=target]

def utc_date(ts_ms):
    return datetime.fromtimestamp(ts_ms/1000,tz=timezone.utc).date().isoformat()

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


def run_backtest(days=90):
    with lock:
        state["backtest"]["running"]=True
        state["backtest"]["progress"]="Starting..."
        state["backtest"]["result"]=None
        state["backtest"]["error"]=None

    try:
        candidate_trades=[]

        for idx,inst in enumerate(COINS,1):
            with lock:
                state["backtest"]["progress"]=f"Downloading {idx}/{len(COINS)}: {inst}"

            try:
                h1=hist_bars(inst,"1H",days)
                m15=hist_bars(inst,"15m",days)
            except Exception:
                continue

            if len(h1)<60 or len(m15)<60:
                continue

            # Walk completed 15m bars. b1 = breakout, b2 = immediate green confirmation.
            for i in range(22,len(m15)-1):
                b1=m15[i-1]
                b2=m15[i]
                prior=m15[i-21:i-1]
                if len(prior)<20:
                    continue

                swing=max(x["h"] for x in prior)
                avgv=sum(x["v"] for x in prior)/20.0

                if not (b1["c"]>swing and b1["c"]>b1["o"] and b1["v"]>=VOL_MULT*avgv):
                    continue
                if not (b2["c"]>b2["o"]):
                    continue

                # Use only completed 1H candles before the confirmation candle.
                hrs=[x for x in h1 if x["ts"]<b2["ts"]]
                if len(hrs)<55:
                    continue

                cls=[x["c"] for x in hrs[-60:]]
                e20=ema(cls,20)
                e50=ema(cls,50)
                rr=rsi(cls[-30:],14)
                if e20 is None or e50 is None or rr is None:
                    continue
                if not (e20>e50 and hrs[-1]["c"]>e20 and RSI_MIN<=rr<=RSI_MAX):
                    continue

                entry=b2["c"]
                tp=entry*(1+TP_PCT/100)
                sl=entry*(1-SL_PCT/100)
                qty=NOTIONAL/entry
                day=utc_date(b2["ts"])

                exit_px=None
                reason=None

                # Search later bars on the same UTC day.
                for j in range(i+1,len(m15)):
                    x=m15[j]
                    xd=utc_date(x["ts"])
                    if xd!=day:
                        prev=m15[j-1]
                        exit_px=prev["c"]
                        reason="DAY_END"
                        break

                    # Conservative if both touched in same candle: SL first.
                    if x["l"]<=sl:
                        exit_px=sl
                        reason="SL"
                        break
                    if x["h"]>=tp:
                        exit_px=tp
                        reason="TP"
                        break

                if exit_px is None:
                    exit_px=m15[-1]["c"]
                    reason="END"

                gross=qty*(exit_px-entry)
                exit_notional=qty*exit_px
                costs=(NOTIONAL+exit_notional)*(FEE+SLIP)
                net=gross-costs

                candidate_trades.append({
                    "day":day,
                    "coin":inst,
                    "entry_ts":b2["ts"],
                    "entry":entry,
                    "exit":exit_px,
                    "reason":reason,
                    "net":net,
                    "rsi":rr,
                    "volx":b1["v"]/avgv if avgv else 0
                })

        # Max one trade/day. Choose strongest breakout-volume multiple on each day.
        by_day={}
        for t in candidate_trades:
            by_day.setdefault(t["day"],[]).append(t)

        chosen=[]
        for d in sorted(by_day):
            arr=sorted(by_day[d], key=lambda x:(x["volx"],x["rsi"]), reverse=True)
            chosen.append(arr[0])

        bal=CAPITAL
        peak=CAPITAL
        max_dd=0.0
        wins=losses=0
        tp_hits=sl_hits=eod_hits=0
        months={}

        for t in chosen:
            bal += t["net"]
            peak=max(peak,bal)
            if peak>0:
                max_dd=max(max_dd,(peak-bal)/peak*100)

            if t["net"]>0: wins+=1
            elif t["net"]<0: losses+=1
            if t["reason"]=="TP": tp_hits+=1
            elif t["reason"]=="SL": sl_hits+=1
            elif t["reason"]=="DAY_END": eod_hits+=1

            t["balance_after"]=bal
            m=t["day"][:7]
            r=months.setdefault(m,{"month":m,"trades":0,"wins":0,"losses":0,"pnl":0.0})
            r["trades"]+=1
            r["pnl"]+=t["net"]
            if t["net"]>0:r["wins"]+=1
            elif t["net"]<0:r["losses"]+=1

        running=CAPITAL
        month_rows=[]
        for m in sorted(months):
            running += months[m]["pnl"]
            months[m]["ending_balance"]=running
            month_rows.append(months[m])

        result={
            "days":days,
            "total_trades":len(chosen),
            "wins":wins,
            "losses":losses,
            "win_rate":(wins/len(chosen)*100) if chosen else 0,
            "tp_hits":tp_hits,
            "sl_hits":sl_hits,
            "day_end_exits":eod_hits,
            "net_pnl":sum(x["net"] for x in chosen),
            "ending_balance":CAPITAL+sum(x["net"] for x in chosen),
            "max_drawdown_pct":max_dd,
            "months":month_rows,
            "trades":chosen[-200:],
            "note":"Paper backtest. Max 1 trade/day, highest breakout-volume multiple selected. Fees/slippage included."
        }

        with lock:
            state["backtest"]["result"]=result
            state["backtest"]["progress"]="Complete"
            state["backtest"]["last_run"]=datetime.now(timezone.utc).isoformat()

    except Exception as e:
        with lock:
            state["backtest"]["error"]=repr(e)
            state["backtest"]["progress"]="Failed"
    finally:
        with lock:
            state["backtest"]["running"]=False


def run_optimizer(days=90):
    with lock:
        state["optimizer"].update({"running":True,"progress":"Starting...","result":None,"error":None})
    try:
        rsi_ranges=[(45,60),(48,62),(50,65),(50,68),(52,68),(55,70)]
        vol_mults=[1.0,1.25,1.5,1.75,2.0]
        tps=[0.4,0.6,0.8,1.0,1.2,1.5]
        sls=[0.2,0.3,0.4,0.5,0.6,0.8]
        cache={}
        for idx,inst in enumerate(COINS,1):
            with lock: state["optimizer"]["progress"]=f"Downloading {idx}/{len(COINS)} {inst}"
            try:
                h1=hist_bars(inst,"1H",days); m15=hist_bars(inst,"15m",days)
                if len(h1)>=60 and len(m15)>=60: cache[inst]=(h1,m15)
            except Exception: pass

        combos=[]; total=len(rsi_ranges)*len(vol_mults)*len(tps)*len(sls); done=0
        for rlo,rhi in rsi_ranges:
            for vm in vol_mults:
                signals=[]
                for inst,(h1,m15) in cache.items():
                    for i in range(22,len(m15)-1):
                        b1,b2=m15[i-1],m15[i]; prior=m15[i-21:i-1]
                        if len(prior)<20: continue
                        swing=max(x["h"] for x in prior); avgv=sum(x["v"] for x in prior)/20
                        if not (b1["c"]>swing and b1["c"]>b1["o"] and b1["v"]>=vm*avgv and b2["c"]>b2["o"]): continue
                        hrs=[x for x in h1 if x["ts"]<b2["ts"]]
                        if len(hrs)<55: continue
                        cls=[x["c"] for x in hrs[-60:]]
                        e20,e50,rr=ema(cls,20),ema(cls,50),rsi(cls[-30:],14)
                        if e20 is None or e50 is None or rr is None: continue
                        if not (e20>e50 and hrs[-1]["c"]>e20 and rlo<=rr<=rhi): continue
                        signals.append({"coin":inst,"day":utc_date(b2["ts"]),"entry":b2["c"],"idx":i,"m15":m15,"volx":b1["v"]/avgv if avgv else 0,"rsi":rr})

                for tp_pct in tps:
                    for sl_pct in sls:
                        done+=1
                        with lock: state["optimizer"]["progress"]=f"Testing {done}/{total}"
                        evals=[]
                        for s in signals:
                            entry=s["entry"]; tp=entry*(1+tp_pct/100); sl=entry*(1-sl_pct/100); qty=NOTIONAL/entry
                            exit_px=None; reason=None; m15=s["m15"]; day=s["day"]
                            for j in range(s["idx"]+1,len(m15)):
                                x=m15[j]
                                if utc_date(x["ts"])!=day:
                                    exit_px=m15[j-1]["c"]; reason="DAY_END"; break
                                if x["l"]<=sl: exit_px=sl; reason="SL"; break
                                if x["h"]>=tp: exit_px=tp; reason="TP"; break
                            if exit_px is None: exit_px=m15[-1]["c"]; reason="END"
                            gross=qty*(exit_px-entry)
                            costs=(NOTIONAL+qty*exit_px)*(FEE+SLIP)
                            evals.append({"day":day,"net":gross-costs,"reason":reason,"volx":s["volx"],"rsi":s["rsi"]})

                        byday={}
                        for t in evals: byday.setdefault(t["day"],[]).append(t)
                        chosen=[sorted(v,key=lambda x:(x["volx"],x["rsi"]),reverse=True)[0] for d,v in sorted(byday.items())]

                        bal=CAPITAL; peak=CAPITAL; maxdd=0; wins=0
                        for t in chosen:
                            bal+=t["net"]; peak=max(peak,bal)
                            if peak>0: maxdd=max(maxdd,(peak-bal)/peak*100)
                            if t["net"]>0: wins+=1
                        n=len(chosen); netp=bal-CAPITAL; wr=(wins/n*100) if n else 0
                        sample=min(1,n/20) if n else 0
                        score=(netp-0.35*maxdd)*sample
                        combos.append({"rsi":f"{rlo}-{rhi}","vol_mult":vm,"tp_pct":tp_pct,"sl_pct":sl_pct,
                                       "trades":n,"win_rate":wr,"net_pnl":netp,"ending_balance":bal,
                                       "max_drawdown_pct":maxdd,"score":score})
        combos.sort(key=lambda x:(x["score"],x["net_pnl"]),reverse=True)
        profitable=[x for x in combos if x["net_pnl"]>0 and x["trades"]>=10]
        result={"days":days,"variants_tested":len(combos),"best":combos[0] if combos else None,
                "best_profitable":profitable[0] if profitable else None,"top10":combos[:10],
                "note":"Same-sample optimization can overfit; prefer positive P/L with reasonable trades and lower drawdown."}
        with lock:
            state["optimizer"].update({"result":result,"progress":"Complete","last_run":datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        with lock: state["optimizer"].update({"error":repr(e),"progress":"Failed"})
    finally:
        with lock: state["optimizer"]["running"]=False

@app.post("/api/optimize")
def api_optimize():
    try:
        payload=__import__("flask").request.get_json(silent=True) or {}
        days=max(30,min(int(payload.get("days",90)),180))
    except: days=90
    with lock:
        if state["optimizer"]["running"]:
            return jsonify({"ok":False,"message":"Optimizer already running"}),409
        threading.Thread(target=run_optimizer,args=(days,),daemon=True).start()
    return jsonify({"ok":True,"days":days})

@app.get("/api/status")
def status():
    with lock: return jsonify(state)

@app.get("/api/scan")
def scan_now():
    scan()
    return status()


@app.post("/api/backtest")
def api_backtest():
    try:
        payload = __import__("flask").request.get_json(silent=True) or {}
        days=int(payload.get("days",90))
    except:
        days=90
    days=max(30,min(days,180))
    with lock:
        if state["backtest"]["running"]:
            return jsonify({"ok":False,"message":"Backtest already running"}),409
        threading.Thread(target=run_backtest,args=(days,),daemon=True).start()
    return jsonify({"ok":True,"days":days})

HTML = r"""<!doctype html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Daily $3 Target Paper Bot</title>
<style>
body{margin:0;background:#071019;color:#eef6ff;font-family:Arial;padding:14px}.w{max-width:1100px;margin:auto}
.c{background:#111d29;border:1px solid #27394b;border-radius:14px;padding:14px;margin-bottom:12px}
.g{color:#6ff0a0}.r{color:#ff9999}.y{color:#ffd479}.sub{color:#a8bacb;font-size:13px;line-height:1.5}
.grid{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}.k{background:#09141e;padding:10px;border-radius:9px}.v{font-size:20px;font-weight:bold}
table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:8px;border-bottom:1px solid #253645;text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}.scroll{overflow:auto}
button{padding:10px 14px;border:0;border-radius:8px;background:#387df3;color:white;font-weight:bold}
input{background:#09141e;color:white;border:1px solid #33485a;border-radius:8px;padding:9px;width:90px}
@media(max-width:700px){.grid{grid-template-columns:1fr 1fr}}
</style></head><body><div class=w>
<div class=c><h2>Daily $3 Target — PAPER BOT</h2>
<div class=sub">Profit guarantee nahi. Goal: max 1 trade/day, 1H bullish trend, RSI 50–68, 15m breakout + 1.5x volume confirmation.</div></div>

<div class="c grid">
<div class=k><div class=sub>Capital</div><div class=v>$100</div></div>
<div class=k><div class=sub>Leverage</div><div class=v>5x</div></div>
<div class=k><div class=sub>Notional</div><div class=v>$500</div></div>
<div class=k><div class=sub>TP</div><div class=v>0.8%</div></div>
<div class=k><div class=sub>SL</div><div class=v>0.4%</div></div>
</div>

<div class=c><button onclick=go()>Scan Now</button> <button onclick=quickbt(90)>Backtest 90 Days</button> <button onclick=quickbt(180)>Backtest 180 Days</button> <span id=m class=sub></span><div id=d></div><div id=p></div><div id=topbt class=sub style="margin-top:10px">Backtest buttons are here at the top.</div></div>

<div class="c scroll"><h3>Live Candidates</h3>
<table><thead><tr><th>Coin</th><th>Rank</th><th>RSI</th><th>Vol x</th><th>Mode</th><th>Entry</th></tr></thead><tbody id=tb></tbody></table></div>

<div class="c scroll"><h3>Paper Trades</h3>
<table><thead><tr><th>Coin</th><th>Entry</th><th>Exit</th><th>Reason</th><th>Net P/L</th></tr></thead><tbody id=tr></tbody></table></div>

<div class=c><h3>Historical Backtest</h3>
<div class=sub">Same TP/SL, EMA/RSI, 15m breakout and volume rules. Fees + slippage included. Max 1 trade per day.</div><br>
Days <input id=days type=number value=90 min=30 max=180>
<button onclick=runbt()>Run Backtest</button>
<div id=bmsg class=sub style="margin-top:10px"></div>
</div>

<div class="c grid" id=bsum></div>


<div class=c><h3>Auto Optimizer</h3>
<div class=sub">RSI, volume, TP aur SL ke 1,080 combinations automatically test karega.</div><br>
Days <input id=odays type=number value=90 min=30 max=180>
<button onclick=runopt()>Auto Test</button>
<div id=omsg class=sub style="margin-top:10px"></div></div>

<div class="c grid" id=osum></div>

<div class="c scroll"><h3>Top 10 Optimized Setups</h3>
<table><thead><tr><th>#</th><th>RSI</th><th>Vol x</th><th>TP</th><th>SL</th><th>Trades</th><th>Win Rate</th><th>Net P/L</th><th>End</th><th>Max DD</th></tr></thead><tbody id=otb></tbody></table></div>
<div class="c scroll"><h3>Month-wise Backtest</h3>
<table><thead><tr><th>Month</th><th>Trades</th><th>Wins</th><th>Losses</th><th>Net P/L</th><th>End Balance</th></tr></thead><tbody id=mb></tbody></table></div>

<div class="c scroll"><h3>Backtest Trades</h3>
<table><thead><tr><th>Date</th><th>Coin</th><th>Entry</th><th>Exit</th><th>Reason</th><th>Vol x</th><th>RSI</th><th>Net P/L</th><th>Balance</th></tr></thead><tbody id=btb></tbody></table></div>

</div><script>
const f=(x,n=4)=>Number(x||0).toFixed(n);

async function load(){
 let j=await(await fetch('/api/status',{cache:'no-store'})).json();

 m.textContent='Last scan: '+(j.last_scan||'—')+(j.error?' | '+j.error:'');
 d.innerHTML=`<p>Daily P/L: <b class="${j.daily_pnl>=0?'g':'r'}">$${f(j.daily_pnl,2)}</b> | stop after +$3 or -$2</p>`;
 p.innerHTML=j.position?`<p><b class=g>OPEN:</b> ${j.position.coin} | Entry ${f(j.position.entry,6)} | TP ${f(j.position.tp,6)} | SL ${f(j.position.sl,6)}</p>`:'<p class=y>No open trade</p>';

 tb.innerHTML='';
 (j.candidates||[]).forEach(x=>tb.innerHTML+=`<tr><td>${x.coin}</td><td>#${x.rank}</td><td>${f(x.rsi,1)}</td><td>${f(x.volx,2)}x</td><td>${x.mode}</td><td>${f(x.entry,6)}</td></tr>`);

 tr.innerHTML='';
 [...(j.trades||[])].reverse().forEach(x=>tr.innerHTML+=`<tr><td>${x.coin}</td><td>${f(x.entry,6)}</td><td>${f(x.exit,6)}</td><td>${x.reason}</td><td class="${x.net>=0?'g':'r'}">${f(x.net,2)}</td></tr>`);

 let b=j.backtest||{};
 bmsg.textContent=(b.running?'Running: ':'')+(b.progress||'')+(b.error?' | '+b.error:'');
 let o=j.optimizer||{};
 omsg.textContent=(o.running?'Running: ':'')+(o.progress||'')+(o.error?' | '+o.error:'');
 if(o.result){
   let r=o.result, best=r.best_profitable||r.best;
   osum.innerHTML=best?`<div class=k><div class=sub>Best RSI</div><div class=v>${best.rsi}</div></div><div class=k><div class=sub>Volume</div><div class=v>${best.vol_mult}x</div></div><div class=k><div class=sub>TP / SL</div><div class=v>${best.tp_pct}% / ${best.sl_pct}%</div></div><div class=k><div class=sub>Net P/L</div><div class="v ${best.net_pnl>=0?'g':'r'}">$${f(best.net_pnl,2)}</div></div><div class=k><div class=sub>End</div><div class=v>$${f(best.ending_balance,2)}</div></div>`:'';
   otb.innerHTML=''; (r.top10||[]).forEach((x,i)=>otb.innerHTML+=`<tr><td>#${i+1}</td><td>${x.rsi}</td><td>${x.vol_mult}x</td><td>${x.tp_pct}%</td><td>${x.sl_pct}%</td><td>${x.trades}</td><td>${f(x.win_rate,1)}%</td><td class="${x.net_pnl>=0?'g':'r'}">${f(x.net_pnl,2)}</td><td>${f(x.ending_balance,2)}</td><td>${f(x.max_drawdown_pct,1)}%</td></tr>`);
 }
 if(b.result){
   let r=b.result;
   bsum.innerHTML=
    `<div class=k><div class=sub>Trades</div><div class=v>${r.total_trades}</div></div>`+
    `<div class=k><div class=sub>Win Rate</div><div class=v>${f(r.win_rate,1)}%</div></div>`+
    `<div class=k><div class=sub>TP / SL</div><div class=v>${r.tp_hits} / ${r.sl_hits}</div></div>`+
    `<div class=k><div class=sub>Net P/L</div><div class="v ${r.net_pnl>=0?'g':'r'}">$${f(r.net_pnl,2)}</div></div>`+
    `<div class=k><div class=sub>End Balance</div><div class=v>$${f(r.ending_balance,2)}</div></div>`+
    `<div class=k><div class=sub>Max DD</div><div class=v>${f(r.max_drawdown_pct,1)}%</div></div>`;

   mb.innerHTML='';
   (r.months||[]).forEach(x=>mb.innerHTML+=`<tr><td>${x.month}</td><td>${x.trades}</td><td>${x.wins}</td><td>${x.losses}</td><td class="${x.pnl>=0?'g':'r'}">${f(x.pnl,2)}</td><td>${f(x.ending_balance,2)}</td></tr>`);

   btb.innerHTML='';
   [...(r.trades||[])].reverse().forEach(x=>btb.innerHTML+=`<tr><td>${x.day}</td><td>${x.coin}</td><td>${f(x.entry,6)}</td><td>${f(x.exit,6)}</td><td>${x.reason}</td><td>${f(x.volx,2)}x</td><td>${f(x.rsi,1)}</td><td class="${x.net>=0?'g':'r'}">${f(x.net,2)}</td><td>${f(x.balance_after,2)}</td></tr>`);
 }
}

async function go(){await fetch('/api/scan');await load()}
async function quickbt(n){days.value=n; await runbt();}
async function runopt(){
 omsg.textContent='Optimizer starting...';
 await fetch('/api/optimize',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({days:parseInt(odays.value||90)})});
 setTimeout(load,1000);
}
async function runbt(){
 bmsg.textContent='Backtest starting...';
 if(typeof topbt!=='undefined') topbt.textContent='Backtest '+days.value+' days starting...';
 await fetch('/api/backtest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({days:parseInt(days.value||90)})});
 setTimeout(load,1000);
}
load();setInterval(load,15000);
</script></body></html>"""

@app.get("/")
def home():
    return Response(HTML,mimetype="text/html")

if __name__=="__main__":
    threading.Thread(target=loop,daemon=True).start()
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT","8080")),threaded=True)
