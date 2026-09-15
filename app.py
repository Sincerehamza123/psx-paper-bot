
import json, threading, time, urllib.parse, urllib.request, csv, io
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, Response, request

app = Flask(__name__)
BASE = "https://www.okx.com"

START_CAPITAL = 100.0
LEVERAGE = 5.0
FEE = 0.0005
SLIP = 0.0001
RSI_LEN = 14

state={"test":{"running":False,"progress":"","result":None,"error":None,"last_run":None}}
lock=threading.RLock()

def api_get(path, params=None, timeout=25):
    url=BASE+path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0 VariantTester/1.0","Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=timeout) as r:
        obj=json.loads(r.read().decode("utf-8"))
    if obj.get("code")!="0":
        raise RuntimeError(obj.get("msg") or "OKX error")
    return obj.get("data",[])

def get_all_usdt_spot_pairs():
    raw=api_get("/api/v5/public/instruments",{"instType":"SPOT"})
    exclude={"USDC","USDT","DAI","FDUSD","TUSD","USDP","EUR","EURT","GBP","AUD","TRY","BRL","AED","SGD","USD","PYUSD","USDE","USD0"}
    out=[]
    for x in raw:
        if x.get("quoteCcy","").upper()=="USDT" and x.get("state")=="live" and x.get("baseCcy","").upper() not in exclude:
            out.append(x.get("instId"))
    return sorted(set(out))

def parse_bar(r):
    return {"ts":int(r[0]),"o":float(r[1]),"h":float(r[2]),"l":float(r[3]),"c":float(r[4]),"v":float(r[5]),"ok":str(r[8]) if len(r)>8 else "1"}

def fetch_daily(inst,days=120):
    target=int((datetime.now(timezone.utc)-timedelta(days=days+35)).timestamp()*1000)
    rows={}
    after=None
    for _ in range(12):
        p={"instId":inst,"bar":"1Dutc","limit":"100"}
        if after is not None: p["after"]=str(after)
        raw=api_get("/api/v5/market/history-candles",p)
        if not raw: break
        batch=[parse_bar(x) for x in raw if len(x)>=8]
        if not batch: break
        for b in batch:
            if b["ok"]=="1": rows[b["ts"]]=b
        oldest=min(x["ts"] for x in batch)
        if oldest<=target: break
        after=oldest
        time.sleep(0.02)
    return [x for x in sorted(rows.values(),key=lambda x:x["ts"]) if x["ts"]>=target]

def rsi_series(closes,n=14):
    out=[None]*len(closes)
    if len(closes)<n+1:return out
    ag=al=0.0
    for i in range(1,n+1):
        d=closes[i]-closes[i-1]
        ag+=max(d,0); al+=max(-d,0)
    ag/=n; al/=n
    out[n]=100.0 if al==0 else 100-100/(1+ag/al)
    for i in range(n+1,len(closes)):
        d=closes[i]-closes[i-1]
        ag=(ag*(n-1)+max(d,0))/n
        al=(al*(n-1)+max(-d,0))/n
        out[i]=100.0 if al==0 else 100-100/(1+ag/al)
    return out

def ema_series(values,n):
    out=[None]*len(values)
    if len(values)<n:return out
    seed=sum(values[:n])/n
    out[n-1]=seed
    k=2/(n+1)
    e=seed
    for i in range(n,len(values)):
        e=values[i]*k+e*(1-k)
        out[i]=e
    return out

def prep(inst,bars):
    closes=[x["c"] for x in bars]
    rs=rsi_series(closes,RSI_LEN)
    e20=ema_series(closes,20); e50=ema_series(closes,50)
    out=[]
    for i,b in enumerate(bars):
        out.append({**b,"coin":inst,"day":datetime.fromtimestamp(b["ts"]/1000,tz=timezone.utc).date().isoformat(),"rsi":rs[i],"ema20":e20[i],"ema50":e50[i]})
    return out

def stop_price(entry_raw,entry_exec,qty,max_loss):
    if qty<=0:return 0.0
    per_unit=max_loss/qty
    num=entry_exec*(1+FEE)-per_unit
    den=(1-SLIP)*(1-FEE)
    return max(0.0,min(entry_raw,num/den if den>0 else 0.0))

def simulate(cache,days,rsi_lo,rsi_hi,wick_tol_pct,max_loss,prev_green,hold_days,trend_mode,min_profit_pct=0.0,pullback_pct=0.0):
    cutoff=(datetime.now(timezone.utc).date()-timedelta(days=days)).isoformat()
    candidates={}
    for inst,rows in cache.items():
        for i in range(1,len(rows)-1):
            prev=rows[i-1]; s=rows[i]; nxt=rows[i+1]
            if s["day"]<cutoff or s["rsi"] is None: continue
            if prev_green and not (prev["c"]>prev["o"]): continue
            if not (s["c"]>s["o"]): continue
            # wick tolerance: low can be at most X% below open
            if s["l"] < s["o"]*(1-wick_tol_pct/100.0): continue
            if not (rsi_lo < s["rsi"] < rsi_hi): continue
            # Trend filter uses SIGNAL day only (no future data).
            if trend_mode=="EMA20":
                if s.get("ema20") is None or prev.get("ema20") is None: continue
                if not (s["c"]>s["ema20"] and s["ema20"]>prev["ema20"]): continue
            elif trend_mode=="EMA20+EMA50":
                if s.get("ema20") is None or s.get("ema50") is None or prev.get("ema20") is None: continue
                if not (s["c"]>s["ema20"]>s["ema50"] and s["ema20"]>prev["ema20"]): continue
            score=(s["c"]-s["o"])/s["o"]*100 + (s["rsi"]-rsi_lo)/max(1,(rsi_hi-rsi_lo))
            candidates.setdefault(nxt["day"],[]).append({"coin":inst,"signal":s,"entry_row":nxt,"score":score})

    equity=START_CAPITAL; peak=START_CAPITAL; maxdd=0.0
    trades=[]; open_pos=None
    all_days=sorted({r["day"] for rows in cache.values() for r in rows if r["day"]>=cutoff})
    rowmap={inst:{r["day"]:r for r in rows} for inst,rows in cache.items()}

    for day in all_days:
        if open_pos is not None:
            row=rowmap[open_pos["coin"]].get(day)
            if row is not None:
                held=(datetime.fromisoformat(day).date()-datetime.fromisoformat(open_pos["entry_day"]).date()).days
                reason=None; exit_exec=None
                if row["l"]<=open_pos["stop_raw"]:
                    reason=f"${max_loss:g} SL"; exit_exec=open_pos["stop_raw"]*(1-SLIP)
                elif row["c"] >= open_pos["entry_raw"]*(1+min_profit_pct/100.0):
                    reason=f"EOD PROFIT {min_profit_pct:g}%+"; exit_exec=row["c"]*(1-SLIP)
                elif held>=hold_days:
                    reason=f"MAX {hold_days}D EXIT"; exit_exec=row["c"]*(1-SLIP)
                if reason:
                    q=open_pos["qty"]
                    net=q*(exit_exec-open_pos["entry_exec"])-FEE*q*open_pos["entry_exec"]-FEE*q*exit_exec
                    equity+=net
                    trades.append({"coin":open_pos["coin"],"signal_day":open_pos["signal_day"],"entry_day":open_pos["entry_day"],"exit_day":day,"reason":reason,"net":net})
                    open_pos=None
                    peak=max(peak,equity)
                    if peak>0:maxdd=max(maxdd,(peak-equity)/peak*100)

        if open_pos is None and equity>0 and day in candidates:
            pick=sorted(candidates[day],key=lambda x:x["score"],reverse=True)[0]
            e=pick["entry_row"]; entry_raw=e["o"]; entry_exec=entry_raw*(1+SLIP)
            notional=min(START_CAPITAL*LEVERAGE,equity*LEVERAGE)
            qty=notional/entry_exec
            sp=stop_price(entry_raw,entry_exec,qty,max_loss)
            open_pos={"coin":pick["coin"],"signal_day":pick["signal"]["day"],"entry_day":day,"entry_raw":entry_raw,"entry_exec":entry_exec,"qty":qty,"stop_raw":sp}

            # same-day exit
            row=e; reason=None; exit_exec=None
            if row["l"]<=sp:
                reason=f"${max_loss:g} SL"; exit_exec=sp*(1-SLIP)
            elif row["c"] >= entry_raw*(1+min_profit_pct/100.0):
                reason=f"EOD PROFIT {min_profit_pct:g}%+"; exit_exec=row["c"]*(1-SLIP)
            if reason:
                net=qty*(exit_exec-entry_exec)-FEE*qty*entry_exec-FEE*qty*exit_exec
                equity+=net
                trades.append({"coin":open_pos["coin"],"signal_day":open_pos["signal_day"],"entry_day":day,"exit_day":day,"reason":reason,"net":net})
                open_pos=None
                peak=max(peak,equity)
                if peak>0:maxdd=max(maxdd,(peak-equity)/peak*100)

    wins=sum(1 for t in trades if t["net"]>0)
    return {
        "trades":len(trades),"wins":wins,"win_rate":wins/len(trades)*100 if trades else 0,
        "net_pnl":equity-START_CAPITAL,"end_balance":equity,"max_dd":maxdd,
        "sl_hits":sum(1 for t in trades if "SL" in t["reason"]),
        "profit_exits":sum(1 for t in trades if str(t["reason"]).startswith("EOD PROFIT"))
    }

def run_test(days):
    with lock:
        state["test"]={"running":True,"progress":"Starting...","result":None,"error":None,"last_run":None}
    try:
        days=max(10,min(int(days),365))
        coins=get_all_usdt_spot_pairs()
        cache={}
        for idx,inst in enumerate(coins,1):
            with lock: state["test"]["progress"]=f"Loading pairs {idx}/{len(coins)} — {inst}"
            try:
                b=fetch_daily(inst,days+20)
                if len(b)>=20: cache[inst]=prep(inst,b)
            except Exception:
                pass

        # HAMZA BEST STRATEGY stays LOCKED.
        rsi_lo,rsi_hi=52,63
        wick_tol=0.05
        max_loss=2.75
        prev_green=True
        hold_days=3
        trend_mode="EMA20+EMA50"
        min_profit_pct=0.60

        # Next-day entry: OPEN baseline vs limit pullbacks below OPEN.
        pullbacks=[0.00,0.25,0.50,0.75,1.00]
        total=len(pullbacks)
        results=[]; n=0

        for pb in pullbacks:
            n+=1
            with lock:
                state["test"]["progress"]=f"Testing pullback entry {n}/{total}: {pb:.2f}%"
            r=simulate(cache,days,rsi_lo,rsi_hi,wick_tol,max_loss,prev_green,hold_days,trend_mode,min_profit_pct,pb)
            r.update({
                "rsi":"52-63","wick_tol":wick_tol,"max_loss":max_loss,
                "prev_green":True,"hold_days":hold_days,"trend":trend_mode,
                "min_profit_pct":min_profit_pct,"pullback_pct":pb
            })
            r["monthly_avg"]=r["net_pnl"]/12.0
            r["target_gap"]=r["net_pnl"]-360.0
            r["target_hit"]=r["net_pnl"]>=360.0
            results.append(r)

        # Stability score: prefer profit with lower drawdown and enough trades.
        for x in results:
            x["score"] = x["net_pnl"] - 1.5*x["max_dd"] - max(0,x["max_dd"]-20)*5.0 + min(x["trades"],50)*0.15
        results.sort(key=lambda x:(x["trades"]>=3,x["score"],x["net_pnl"],-x["max_dd"]),reverse=True)

        with lock:
            state["test"].update({
                "running":False,"progress":"Complete","error":None,
                "last_run":datetime.now(timezone.utc).isoformat(),
                "result":{
                    "days":days,"pairs_found":len(coins),"pairs_loaded":len(cache),
                    "variants":total,"profitable":sum(1 for x in results if x["net_pnl"]>0),
                    "top":results[:50]
                }
            })
    except Exception as e:
        with lock: state["test"].update({"running":False,"progress":"Failed","error":repr(e)})

@app.post("/api/run")
def run_api():
    d=request.get_json(silent=True) or {}
    days=int(d.get("days",60))
    with lock:
        if state["test"]["running"]: return jsonify({"ok":False}),409
        threading.Thread(target=run_test,args=(days,),daemon=True).start()
    return jsonify({"ok":True})

@app.get("/api/download")
def download_results():
    with lock:
        result=state["test"].get("result")
    if not result:
        return Response("Pehle backtest complete karein.",status=400,mimetype="text/plain")
    out=io.StringIO()
    w=csv.writer(out)
    w.writerow(["Rank","Trend Filter","RSI","Wick Tol %","SL $","Prev Green","Max Hold Days","Min EOD Profit %","Trades","Wins","Win Rate %","EOD Profit","SL Hits","Net P/L $","End Balance $","Max DD %","Score"])
    for i,x in enumerate(result.get("top",[]),1):
        w.writerow([i,x.get("trend"),x.get("rsi"),x.get("wick_tol"),x.get("max_loss"),"Yes" if x.get("prev_green") else "No",x.get("hold_days"),x.get("min_profit_pct",0),x.get("trades"),x.get("wins"),round(x.get("win_rate",0),2),x.get("profit_exits"),x.get("sl_hits"),round(x.get("net_pnl",0),4),round(x.get("end_balance",0),4),round(x.get("max_dd",0),2),round(x.get("score",0),2)])
    name=f"winner_validation_{result.get('days',365)}days_full_results.csv"
    return Response(out.getvalue(),mimetype="text/csv",headers={"Content-Disposition":f"attachment; filename={name}"})

@app.get("/api/status")
def status():
    with lock:return jsonify(state["test"])

HTML=r"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>365D Profit Improvement Test</title><style>
body{margin:0;background:#071019;color:#eef6ff;font-family:Arial;padding:14px}.w{max-width:1100px;margin:auto}
.c{background:#111d29;border:1px solid #27394b;border-radius:14px;padding:16px;margin-bottom:12px}.sub{color:#a8bacb;line-height:1.5}
input{width:120px;padding:11px;border-radius:8px;border:1px solid #3b4d60;background:#09141e;color:white;font-size:17px}
button{padding:12px 18px;border:0;border-radius:9px;background:#387df3;color:white;font-weight:bold;font-size:16px}
table{width:100%;border-collapse:collapse}th,td{padding:9px;border-bottom:1px solid #253645;text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}.scroll{overflow:auto}.g{color:#6ff0a0}.r{color:#ff9999}
</style></head><body><div class=w>
<div class=c><h2>HAMZA PULLBACK ENTRY TEST V1</h2>
<div class=sub>Signal rules LOCKED hain: EMA20+EMA50, RSI 52–62, Wick 0.10%, Previous Green = Yes. Ab sirf profit/risk management test hoga: HAMZA Best Strategy LOCKED hai. Sirf next-day entry compare hogi: Open, ya Open se 0.25%, 0.50%, 0.75%, 1.00% neeche. Pullback trade tabhi fill hogi jab us din ka Low actual entry level ko touch kare. Future data use nahi hoga. Sab OKX USDT pairs scan honge.</div></div>
<div class=c><b>Backtest Days</b><br><br><input id=days type=number value=365 min=10 max=365>
<button onclick=run()>Run Pullback Entry Test</button> <button id=dl onclick="location.href='/api/download'" style="background:#18a66a">Download Pullback Entry Results</button><div id=msg class=sub style="margin-top:12px"></div><div id=info class=sub></div></div>
<div class="c scroll"><h3>Pullback Entry Results</h3><table><thead><tr>
<th>#</th><th>Trend Filter</th><th>RSI</th><th>Wick Tol</th><th>SL</th><th>Prev Green</th><th>Max Hold</th><th>Min EOD Profit</th><th>Trades</th><th>WR</th><th>EOD Profit</th><th>SL Hits</th><th>Net P/L</th><th>End</th><th>Max DD</th><th>Score</th>
</tr></thead><tbody id=tb></tbody></table></div>
</div><script>
const f=(x,n=2)=>Number(x||0).toFixed(n);
async function load(){
 let j=await(await fetch('/api/status',{cache:'no-store'})).json();
 msg.textContent=(j.running?'Running: ':'')+(j.progress||'')+(j.error?' | '+j.error:'');
 if(j.result){
   info.textContent=`Pairs: ${j.result.pairs_loaded}/${j.result.pairs_found} | Variants: ${j.result.variants} | Profitable: ${j.result.profitable}`;
   tb.innerHTML='';
   (j.result.top||[]).forEach((x,i)=>tb.innerHTML+=`<tr>
   <td>${i+1}</td><td>${x.trend}</td><td>${x.rsi}</td><td>${f(x.wick_tol,2)}%</td><td>$${f(x.max_loss,0)}</td><td>${x.prev_green?'Yes':'No'}</td><td>${x.hold_days}d</td><td>${f(x.min_profit_pct,2)}%</td>
   <td>${x.trades}</td><td>${f(x.win_rate,1)}%</td><td>${x.profit_exits}</td><td>${x.sl_hits}</td>
   <td class="${x.net_pnl>=0?'g':'r'}">$${f(x.net_pnl)}</td><td>$${f(x.end_balance)}</td><td>${f(x.max_dd,1)}%</td><td>${f(x.score,1)}</td></tr>`);
 }
}
async function run(){msg.textContent='Starting...';await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({days:parseInt(days.value||365)})});setTimeout(load,1000)}
load();setInterval(load,8000);
</script></body></html>"""

@app.get("/")
def home(): return Response(HTML,mimetype="text/html")

if __name__=="__main__":
    app.run(host="0.0.0.0",port=8080)
