import os,json,subprocess,threading,csv
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.request import urlopen,Request
from pathlib import Path
from datetime import datetime,timedelta,timezone

BASE=Path("/app"); UD=BASE/"user_data"; STRAT=UD/"strategies"/"GeneticEngineV1.py"; CONFIG=UD/"config.json"; RESULT=UD/"backtest_results"
STATE={"running":False,"status":"Ready","progress":0,"error":"","summary":None,"report":None}; LOCK=threading.Lock()

HTML="""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>GeneticEngineV1 365D</title>
<style>body{font-family:Arial;background:#0b1220;color:#e8eef9;padding:18px}.card{max-width:800px;margin:auto;background:#121c2e;padding:20px;border-radius:16px}button{background:#2878ed;color:white;border:0;padding:14px 18px;border-radius:10px;font-weight:bold}.bar{height:12px;background:#26344b;border-radius:8px;overflow:hidden}.fill{height:100%;background:#2878ed}.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.box{background:#0c1525;padding:12px;border-radius:10px}.muted{color:#9db0cb}a{color:#7fb1ff}pre{white-space:pre-wrap}</style></head>
<body><div class="card"><h1>GeneticEngineV1 - 365D Backtest</h1><p class="muted">Original GitHub strategy | 5m | OKX Spot | $100 start | Max 5 open trades | Top 20 USDT pairs</p>
<button id="run" onclick="go()">Run 365 Days</button><p id="st">Ready</p><div class="bar"><div id="fill" class="fill" style="width:0%"></div></div><div id="sum"></div>
<p><a id="dl" href="/download" style="display:none">Download Trade-wise CSV</a></p><pre id="err"></pre></div>
<script>async function go(){run.disabled=true;await fetch('/run',{method:'POST'});poll()}async function poll(){let x=await(await fetch('/status')).json();st.textContent=x.status;fill.style.width=x.progress+'%';if(x.summary){let s=x.summary;sum.innerHTML='<div class="grid"><div class="box"><b>Trades</b><br>'+s.trades+'</div><div class="box"><b>Net P/L</b><br>$'+s.profit_abs+'</div><div class="box"><b>End Balance</b><br>$'+s.end_balance+'</div><div class="box"><b>Win Rate</b><br>'+s.win_rate+'%</div><div class="box"><b>Max DD</b><br>'+s.max_dd+'%</div><div class="box"><b>Pairs</b><br>'+s.pairs+'</div></div>';dl.style.display='inline'}if(x.error)err.textContent=x.error;if(x.running)setTimeout(poll,2500);else run.disabled=false}poll()</script></body></html>"""

def cmd(a,timeout=7200):
    p=subprocess.run(a,cwd=BASE,text=True,capture_output=True,timeout=timeout)
    if p.returncode: raise RuntimeError((p.stdout+"\n"+p.stderr)[-6000:])
    return p.stdout+p.stderr

def pairs():
    q=Request("https://www.okx.com/api/v5/market/tickers?instType=SPOT",headers={"User-Agent":"Mozilla/5.0"})
    d=json.loads(urlopen(q,timeout=30).read())["data"]; a=[]
    for x in d:
        s=x.get("instId","")
        if not s.endswith("-USDT"): continue
        b=s[:-5]
        if b in {"USDT","USDC","DAI","FDUSD","TUSD","USD","EUR"}: continue
        try:v=float(x.get("volCcy24h") or 0)
        except:v=0
        a.append((v,b+"/USDT"))
    return [p for _,p in sorted(a,reverse=True)[:20]]

def config(ps):
    c={"max_open_trades":5,"stake_currency":"USDT","stake_amount":"unlimited","tradable_balance_ratio":0.99,"dry_run_wallet":100,
       "fiat_display_currency":"USD","dry_run":True,"trading_mode":"spot","exchange":{"name":"okx","key":"","secret":"","password":"",
       "ccxt_config":{"enableRateLimit":True},"ccxt_async_config":{"enableRateLimit":True},"pair_whitelist":ps,"pair_blacklist":[]},
       "pairlists":[{"method":"StaticPairList"}],"entry_pricing":{"price_side":"same","use_order_book":False,"order_book_top":1,"price_last_balance":0.0,
       "check_depth_of_market":{"enabled":False,"bids_to_ask_delta":1}},"exit_pricing":{"price_side":"same","use_order_book":False,"order_book_top":1},
       "database_url":"sqlite:///tradesv3.sqlite","initial_state":"running"}
    CONFIG.write_text(json.dumps(c,indent=2))

def parse(path,ps):
    j=json.loads(path.read_text()); s=(j.get("strategy") or {}).get("GeneticEngineV1") or {}; ts=s.get("trades",[])
    profit=float(s.get("profit_total_abs",0) or 0); wins=sum(float(t.get("profit_abs",0) or 0)>0 for t in ts)
    dd=float(s.get("max_drawdown_account",0) or 0)*100
    rp=UD/"genetic_engine_v1_365d_trades.csv"
    with rp.open("w",newline="") as f:
        w=csv.writer(f); w.writerow(["#","Pair","Open Date","Close Date","Open Rate","Close Rate","Profit $","Profit %","Exit Reason"])
        for i,t in enumerate(ts,1): w.writerow([i,t.get("pair"),t.get("open_date"),t.get("close_date"),t.get("open_rate"),t.get("close_rate"),round(float(t.get("profit_abs",0) or 0),6),round(float(t.get("profit_ratio",0) or 0)*100,4),t.get("exit_reason")])
    return {"trades":len(ts),"profit_abs":round(profit,2),"end_balance":round(100+profit,2),"win_rate":round(100*wins/len(ts),2) if ts else 0,"max_dd":round(dd,2),"pairs":len(ps)},str(rp)

def work():
    try:
        with LOCK: STATE.update(running=True,status="Finding top 20 OKX pairs...",progress=5,error="",summary=None,report=None)
        ps=pairs(); config(ps); end=datetime.now(timezone.utc).date(); start=end-timedelta(days=365); tr=f"{start:%Y%m%d}-{end:%Y%m%d}"
        with LOCK: STATE.update(status="Downloading 5m candles...",progress=15)
        cmd(["freqtrade","download-data","--config",str(CONFIG),"--timeframes","5m","--timerange",tr])
        with LOCK: STATE.update(status="Running GeneticEngineV1...",progress=55)
        RESULT.mkdir(parents=True,exist_ok=True); out=RESULT/"genetic.json"
        cmd(["freqtrade","backtesting","--config",str(CONFIG),"--strategy-path",str(STRAT.parent),"--strategy","GeneticEngineV1","--timeframe","5m","--timerange",tr,"--export","trades","--export-filename",str(out)])
        candidates=sorted(RESULT.glob("*.json"),key=lambda p:p.stat().st_mtime,reverse=True)
        if not candidates: raise RuntimeError("Backtest result JSON not found.")
        sm,rp=parse(candidates[0],ps)
        with LOCK: STATE.update(running=False,status="Completed",progress=100,summary=sm,report=rp)
    except Exception as e:
        with LOCK: STATE.update(running=False,status="Failed",progress=100,error=str(e))

class H(BaseHTTPRequestHandler):
    def sendx(self,b,typ="text/html",code=200,headers=None):
        self.send_response(code); self.send_header("Content-Type",typ); self.send_header("Content-Length",str(len(b)))
        for k,v in (headers or {}).items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path=="/": return self.sendx(HTML.encode())
        if self.path=="/status":
            with LOCK:x={k:v for k,v in STATE.items() if k!="report"}
            return self.sendx(json.dumps(x).encode(),"application/json")
        if self.path=="/download":
            with LOCK:r=STATE.get("report")
            if r and Path(r).exists(): return self.sendx(Path(r).read_bytes(),"text/csv",200,{"Content-Disposition":'attachment; filename="genetic_engine_v1_365d_trades.csv"'})
            return self.sendx(b"No report yet","text/plain",404)
        self.sendx(b"Not found","text/plain",404)
    def do_POST(self):
        if self.path=="/run":
            with LOCK:
                if not STATE["running"]: STATE["running"]=True; threading.Thread(target=work,daemon=True).start()
            return self.sendx(b'{"ok":true}',"application/json")
        self.sendx(b"Not found","text/plain",404)
    def log_message(self,*a): pass

if __name__=="__main__": ThreadingHTTPServer(("0.0.0.0",int(os.environ.get("PORT","8080"))),H).serve_forever()
