
import os, time, json, threading, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, Response, request

app = Flask(__name__)
BASE = "https://www.okx.com"

CAPITAL = 100.0
LEVERAGE = 5.0
NOTIONAL = 500.0
FEE = 0.0005
SLIP = 0.0001

COINS = [
    "BTC-USDT","ETH-USDT","SOL-USDT","XRP-USDT","DOGE-USDT",
    "ADA-USDT","LINK-USDT","AVAX-USDT","SUI-USDT","LTC-USDT",
    "BCH-USDT","DOT-USDT","NEAR-USDT","APT-USDT","INJ-USDT"
]

state = {
    "optimizer": {
        "running": False,
        "progress": "",
        "result": None,
        "error": None,
        "last_run": None
    }
}
lock = threading.RLock()

def get(path, params=None, timeout=20):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent":"Mozilla/5.0 PullbackRecoveryBot/1.0",
        "Accept":"application/json"
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        obj = json.loads(r.read().decode("utf-8"))
    if obj.get("code") != "0":
        raise RuntimeError(obj.get("msg") or "OKX error")
    return obj.get("data", [])

def parse_bar(r):
    return {
        "ts":int(r[0]), "o":float(r[1]), "h":float(r[2]), "l":float(r[3]),
        "c":float(r[4]), "v":float(r[5]), "qv":float(r[7]), "ok":str(r[8])
    }

def hist_bars(inst, tf, days):
    target = int((datetime.now(timezone.utc)-timedelta(days=days+5)).timestamp()*1000)
    rows = {}
    after = None
    for _ in range(160):
        p={"instId":inst,"bar":tf,"limit":"100"}
        if after is not None:
            p["after"]=str(after)
        raw=get("/api/v5/market/history-candles",p)
        if not raw:
            break
        batch=[parse_bar(x) for x in raw if len(x)>=9]
        if not batch:
            break
        for b in batch:
            if b["ok"]=="1":
                rows[b["ts"]]=b
        oldest=min(x["ts"] for x in batch)
        if oldest<=target:
            break
        after=oldest
        time.sleep(0.025)
    out=sorted(rows.values(),key=lambda x:x["ts"])
    return [x for x in out if x["ts"]>=target]

def ema_series(vals, n):
    if len(vals)<n:
        return []
    out=[None]*(n-1)
    e=sum(vals[:n])/n
    out.append(e)
    k=2/(n+1)
    for v in vals[n:]:
        e=v*k+e*(1-k)
        out.append(e)
    return out

def ema(vals,n):
    s=ema_series(vals,n)
    return s[-1] if s else None

def rsi(vals,n=14):
    if len(vals)<n+1:
        return None
    gains=[]; losses=[]
    for i in range(1,n+1):
        d=vals[i]-vals[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    ag=sum(gains)/n
    al=sum(losses)/n
    for i in range(n+1,len(vals)):
        d=vals[i]-vals[i-1]
        ag=(ag*(n-1)+max(d,0))/n
        al=(al*(n-1)+max(-d,0))/n
    if al==0:
        return 100.0
    rs=ag/al
    return 100-100/(1+rs)

def utc_date(ts):
    return datetime.fromtimestamp(ts/1000,tz=timezone.utc).date().isoformat()

def max_drawdown(trades):
    bal=CAPITAL
    peak=CAPITAL
    dd=0
    for t in trades:
        bal += t["net"]
        peak=max(peak,bal)
        if peak>0:
            dd=max(dd,(peak-bal)/peak*100)
    return dd, bal

def build_signals(h1, m15, rlo, rhi, pullback_depth, vol_mult, recovery_mode):
    # Strategy:
    # 1H: EMA20 > EMA50, close > EMA50, RSI in range.
    # 15m: pullback candle touches EMA20 zone without losing EMA50 too deeply.
    # Immediate next 15m candle must recover green.
    # Recovery modes:
    #   CLOSE_PREV_HIGH = green closes above pullback candle high
    #   CLOSE_EMA20 = green closes back above 15m EMA20
    signals=[]
    if len(h1)<60 or len(m15)<80:
        return signals

    closes15=[x["c"] for x in m15]
    e20s=ema_series(closes15,20)
    e50s=ema_series(closes15,50)

    for i in range(51,len(m15)-1):
        pb=m15[i-1]
        rec=m15[i]
        e20=e20s[i-1]
        e50=e50s[i-1]
        e20r=e20s[i]
        if e20 is None or e50 is None or e20r is None:
            continue

        # only historical 1H candles known before recovery close
        hrs=[x for x in h1 if x["ts"]<rec["ts"]]
        if len(hrs)<55:
            continue
        hc=[x["c"] for x in hrs[-60:]]
        he20=ema(hc,20)
        he50=ema(hc,50)
        rr=rsi(hc[-30:],14)
        if he20 is None or he50 is None or rr is None:
            continue
        if not (he20>he50 and hrs[-1]["c"]>he50 and rlo<=rr<=rhi):
            continue

        # pullback must be red or weak, touch EMA20 zone, not crash far below EMA50
        if not (pb["c"]<=pb["o"]):
            continue
        touch_limit=e20*(1+pullback_depth/100)
        if pb["l"]>touch_limit:
            continue
        if pb["c"] < e50*0.995:
            continue

        # recovery candle green + volume
        if not (rec["c"]>rec["o"]):
            continue
        prior=m15[max(0,i-21):i-1]
        if len(prior)<20:
            continue
        avgv=sum(x["v"] for x in prior)/len(prior)
        if rec["v"] < vol_mult*avgv:
            continue

        if recovery_mode=="CLOSE_PREV_HIGH":
            if rec["c"]<=pb["h"]:
                continue
        else:
            if rec["c"]<=e20r:
                continue

        score=(rec["v"]/avgv if avgv else 0) + max(0,(rr-rlo)/10)
        signals.append({
            "day":utc_date(rec["ts"]),
            "entry_ts":rec["ts"],
            "entry":rec["c"],
            "idx":i,
            "pb_low":pb["l"],
            "score":score,
            "rsi":rr,
            "volx":rec["v"]/avgv if avgv else 0
        })
    return signals

def evaluate_signal(sig, m15, tp_pct, sl_pct):
    entry=sig["entry"]
    tp=entry*(1+tp_pct/100)
    pct_sl=entry*(1-sl_pct/100)
    # Use tighter of fixed SL or pullback low only if pullback low is below entry.
    structural=sig["pb_low"]
    sl=max(structural,pct_sl) if structural<entry else pct_sl

    qty=NOTIONAL/entry
    day=sig["day"]
    exit_px=None
    reason=None

    for j in range(sig["idx"]+1,len(m15)):
        x=m15[j]
        if utc_date(x["ts"])!=day:
            exit_px=m15[j-1]["c"]
            reason="DAY_END"
            break
        # conservative ordering when both touched in same bar
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
    costs=(NOTIONAL+qty*exit_px)*(FEE+SLIP)
    return gross-costs, reason

def run_optimizer(days=90):
    with lock:
        state["optimizer"]={"running":True,"progress":"Starting...","result":None,"error":None,"last_run":None}
    try:
        cache={}
        for idx,inst in enumerate(COINS,1):
            with lock:
                state["optimizer"]["progress"]=f"Downloading {idx}/{len(COINS)} {inst}"
            try:
                h1=hist_bars(inst,"1H",days)
                m15=hist_bars(inst,"15m",days)
                if len(h1)>=60 and len(m15)>=80:
                    cache[inst]=(h1,m15)
            except Exception:
                pass

        rsi_ranges=[(42,58),(45,60),(48,62),(50,65)]
        pullbacks=[0.0,0.15,0.30]
        vols=[0.8,1.0,1.2]
        modes=["CLOSE_PREV_HIGH","CLOSE_EMA20"]
        tps=[0.6,0.8,1.0,1.2,1.5]
        sls=[0.3,0.4,0.5,0.6]

        signal_cache={}
        base_total=len(rsi_ranges)*len(pullbacks)*len(vols)*len(modes)
        bdone=0
        for rr in rsi_ranges:
            for pd in pullbacks:
                for vm in vols:
                    for mode in modes:
                        bdone+=1
                        with lock:
                            state["optimizer"]["progress"]=f"Building signals {bdone}/{base_total}"
                        all_s=[]
                        for inst,(h1,m15) in cache.items():
                            sigs=build_signals(h1,m15,rr[0],rr[1],pd,vm,mode)
                            for s in sigs:
                                s["coin"]=inst
                            all_s.extend(sigs)
                        signal_cache[(rr,pd,vm,mode)]=all_s

        combos=[]
        total=base_total*len(tps)*len(sls)
        done=0

        for (rr,pd,vm,mode),signals in signal_cache.items():
            for tp in tps:
                for sl in sls:
                    done+=1
                    with lock:
                        state["optimizer"]["progress"]=f"Testing {done}/{total}"

                    evaluated=[]
                    for s in signals:
                        m15=cache[s["coin"]][1]
                        net,reason=evaluate_signal(s,m15,tp,sl)
                        evaluated.append({
                            "day":s["day"],"coin":s["coin"],"net":net,"reason":reason,
                            "score":s["score"],"rsi":s["rsi"],"volx":s["volx"]
                        })

                    # Max 1 trade/day, choose highest-quality candidate known at entry.
                    byday={}
                    for t in evaluated:
                        byday.setdefault(t["day"],[]).append(t)
                    chosen=[]
                    for d in sorted(byday):
                        chosen.append(sorted(byday[d],key=lambda x:x["score"],reverse=True)[0])

                    wins=sum(1 for x in chosen if x["net"]>0)
                    tph=sum(1 for x in chosen if x["reason"]=="TP")
                    slh=sum(1 for x in chosen if x["reason"]=="SL")
                    dd,endbal=max_drawdown(chosen)
                    n=len(chosen)
                    netp=endbal-CAPITAL
                    wr=(wins/n*100) if n else 0

                    # Penalize tiny samples and drawdown.
                    sample=min(1.0,n/18.0) if n else 0
                    score=(netp-0.30*dd)*sample

                    combos.append({
                        "rsi":f"{rr[0]}-{rr[1]}",
                        "pullback_pct":pd,
                        "vol_mult":vm,
                        "recovery":mode,
                        "tp_pct":tp,
                        "sl_pct":sl,
                        "trades":n,
                        "wins":wins,
                        "win_rate":wr,
                        "tp_hits":tph,
                        "sl_hits":slh,
                        "net_pnl":netp,
                        "ending_balance":endbal,
                        "max_drawdown_pct":dd,
                        "score":score
                    })

        combos.sort(key=lambda x:(x["score"],x["net_pnl"]),reverse=True)
        profitable=[x for x in combos if x["net_pnl"]>0 and x["trades"]>=12]
        result={
            "days":days,
            "variants_tested":len(combos),
            "best":combos[0] if combos else None,
            "best_profitable":profitable[0] if profitable else None,
            "top10":combos[:10],
            "profitable_count":len(profitable),
            "note":"Same-sample optimization can overfit. A positive result should later be checked on a different date range before live use."
        }
        with lock:
            state["optimizer"].update({
                "running":False,
                "progress":"Complete",
                "result":result,
                "error":None,
                "last_run":datetime.now(timezone.utc).isoformat()
            })
    except Exception as e:
        with lock:
            state["optimizer"].update({"running":False,"progress":"Failed","error":repr(e)})

@app.post("/api/optimize")
def api_optimize():
    try:
        days=int((request.get_json(silent=True) or {}).get("days",90))
    except:
        days=90
    days=max(1,min(days,90))
    with lock:
        if state["optimizer"]["running"]:
            return jsonify({"ok":False,"message":"Optimizer already running"}),409
        threading.Thread(target=run_optimizer,args=(days,),daemon=True).start()
    return jsonify({"ok":True,"days":days})

@app.get("/api/status")
def api_status():
    with lock:
        return jsonify(state)

HTML=r"""<!doctype html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trend Pullback Recovery Backtest</title>
<style>
body{margin:0;background:#071019;color:#eef6ff;font-family:Arial;padding:14px}.w{max-width:1100px;margin:auto}
.c{background:#111d29;border:1px solid #27394b;border-radius:14px;padding:14px;margin-bottom:12px}
.grid{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}.k{background:#09141e;padding:10px;border-radius:9px}
.v{font-size:20px;font-weight:bold}.sub{color:#a8bacb;font-size:13px;line-height:1.5}.g{color:#6ff0a0}.r{color:#ff9999}.y{color:#ffd479}
button{padding:11px 16px;border:0;border-radius:8px;background:#387df3;color:white;font-weight:bold}
input{background:#09141e;color:white;border:1px solid #33485a;border-radius:8px;padding:10px;width:90px}
table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:8px;border-bottom:1px solid #253645;text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}.scroll{overflow:auto}
@media(max-width:700px){.grid{grid-template-columns:1fr 1fr}}
</style></head><body><div class=w>
<div class=c>
<h2>Trend Pullback Recovery — Auto Backtest</h2>
<div class=sub">Breakout chase nahi. 1H bullish trend ke andar 15m pullback ka wait, phir immediate green recovery par entry. $100 capital, 5x max notional $500, fees + slippage included, max 1 trade/day.</div>
</div>

<div class=c>
<h3>Auto Test</h3>
<div class=sub">1 se 90 days tak jitne din chahein select karein. RSI, pullback depth, recovery type, volume, TP aur SL ke multiple variants automatically test honge.</div><br>
Days <input id=days type=number value=15 min=1 max=90 step=1>
<button onclick=runopt()>Run Auto Backtest</button>
<div id=msg class=sub style="margin-top:12px"></div>
</div>

<div class="c grid" id=sum></div>

<div class="c scroll">
<h3>Top 10 Setups</h3>
<table><thead><tr>
<th>#</th><th>RSI</th><th>Pullback</th><th>Vol</th><th>Recovery</th><th>TP</th><th>SL</th><th>Trades</th><th>Win Rate</th><th>Net P/L</th><th>End</th><th>Max DD</th>
</tr></thead><tbody id=tb></tbody></table>
</div>
</div>

<script>
const f=(x,n=2)=>Number(x||0).toFixed(n);

async function load(){
 let j=await(await fetch('/api/status',{cache:'no-store'})).json();
 let o=j.optimizer||{};
 msg.textContent=(o.running?'Running: ':'')+(o.progress||'')+(o.error?' | '+o.error:'');
 if(o.result){
   let r=o.result, b=r.best_profitable||r.best;
   sum.innerHTML=b?
    `<div class=k><div class=sub>Variants</div><div class=v>${r.variants_tested}</div></div>`+
    `<div class=k><div class=sub>Profitable</div><div class=v>${r.profitable_count}</div></div>`+
    `<div class=k><div class=sub>Best Net P/L</div><div class="v ${b.net_pnl>=0?'g':'r'}">$${f(b.net_pnl)}</div></div>`+
    `<div class=k><div class=sub>End Balance</div><div class=v>$${f(b.ending_balance)}</div></div>`+
    `<div class=k><div class=sub>Max DD</div><div class=v>${f(b.max_drawdown_pct,1)}%</div></div>`:'';
   tb.innerHTML='';
   (r.top10||[]).forEach((x,i)=>tb.innerHTML+=
    `<tr><td>#${i+1}</td><td>${x.rsi}</td><td>${x.pullback_pct}%</td><td>${x.vol_mult}x</td><td>${x.recovery}</td><td>${x.tp_pct}%</td><td>${x.sl_pct}%</td><td>${x.trades}</td><td>${f(x.win_rate,1)}%</td><td class="${x.net_pnl>=0?'g':'r'}">${f(x.net_pnl)}</td><td>${f(x.ending_balance)}</td><td>${f(x.max_drawdown_pct,1)}%</td></tr>`);
 }
}
async function runopt(){
 msg.textContent='Starting...';
 await fetch('/api/optimize',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({days:parseInt(days.value||90)})});
 setTimeout(load,1000);
}
load();setInterval(load,15000);
</script></body></html>"""

@app.get("/")
def home():
    return Response(HTML,mimetype="text/html")

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT","8080")),threaded=True)
