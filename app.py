import os,json,subprocess,threading,csv,zipfile
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.request import urlopen,Request
from pathlib import Path
from datetime import datetime,timedelta,timezone

BASE=Path("/app"); UD=BASE/"user_data"; STRAT=UD/"strategies"/"GeneticEngineV1.py"; CONFIG=UD/"config.json"; RESULT=UD/"backtest_results"
STATE={"running":False,"status":"Ready","progress":0,"error":"","summary":None,"report":None,"data_zip":None}; LOCK=threading.Lock()

HTML="""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>OKX 5m Data Export</title>
<style>body{font-family:Arial;background:#0b1220;color:#e8eef9;padding:18px}.card{max-width:800px;margin:auto;background:#121c2e;padding:20px;border-radius:16px}button{background:#2878ed;color:white;border:0;padding:14px 18px;border-radius:10px;font-weight:bold;font-size:16px}input{box-sizing:border-box;width:100%;padding:12px;font-size:16px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.muted{color:#9db0cb}a{color:#7fb1ff;font-size:18px}.bar{height:12px;background:#26344b;border-radius:8px;overflow:hidden}.fill{height:100%;background:#2878ed}</style></head>
<body><div class="card"><h1>OKX 5m CSV Data Export</h1>
<p class="muted">Top 20 USDT pairs | Select From/To dates | Downloads raw 5-minute candles directly as CSV. No backtest.</p>
<div class="grid"><label>From Date<br><input id="fd" type="date"></label><label>To Date<br><input id="td" type="date"></label></div><br>
<button id="run" onclick="go()">Download Market Data</button>
<p id="st">Ready</p><div class="bar"><div id="fill" class="fill" style="width:0%"></div></div>
<p><a id="dl" href="/download-data" style="display:none">Download Candle Data ZIP</a></p><pre id="err"></pre></div>
<script>
async function go(){if(!fd.value||!td.value){alert('From aur To date select karein');return}if(fd.value>td.value){alert('Date range check karein');return}run.disabled=true;dl.style.display='none';err.textContent='';await fetch('/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({from:fd.value,to:td.value})});poll()}
async function poll(){let x=await(await fetch('/status')).json();st.textContent=x.status;fill.style.width=x.progress+'%';if(x.ready)dl.style.display='inline';if(x.error)err.textContent=x.error;if(x.running)setTimeout(poll,2000);else run.disabled=false}poll()
</script></body></html>"""

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

def work(from_date,to_date):
    try:
        with LOCK: STATE.update(running=True,status="Finding top 20 OKX pairs...",progress=5,error="",data_zip=None)
        ps=pairs(); config(ps)
        start=datetime.strptime(from_date,"%Y-%m-%d").date()
        end=datetime.strptime(to_date,"%Y-%m-%d").date()
        if start>end: raise RuntimeError("From Date must be before To Date.")
        if end>datetime.now(timezone.utc).date(): raise RuntimeError("To Date future mein nahi ho sakti.")
        tr=f"{start:%Y%m%d}-{end:%Y%m%d}"
        with LOCK: STATE.update(status=f"Downloading 5m candles for {len(ps)} pairs...",progress=20)
        cmd(["freqtrade","download-data","--config",str(CONFIG),"--timeframes","5m","--timerange",tr,"--data-format-ohlcv","csv"])
        with LOCK: STATE.update(status="Packing candle files into ZIP...",progress=85)

        data_root=UD/"data"/"okx"
        if not data_root.exists(): raise RuntimeError("OKX data folder not found after download.")
        zp=UD/f"OKX_5m_CSV_{start:%Y%m%d}_{end:%Y%m%d}_Top20.zip"
        with zipfile.ZipFile(zp,"w",zipfile.ZIP_DEFLATED) as z:
            manifest=["OKX 5m CSV candle export",f"From: {from_date}",f"To: {to_date}","Pairs:"]+ps
            z.writestr("MANIFEST.txt","\\n".join(manifest))
            count=0
            for f in data_root.rglob("*"):
                if f.is_file():
                    z.write(f,arcname=str(Path("data")/f.relative_to(data_root)))
                    count+=1
        if count==0: raise RuntimeError("No candle files were downloaded.")
        with LOCK: STATE.update(running=False,status=f"Completed - {count} data files ready",progress=100,data_zip=str(zp))
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
            with LOCK:
                x={k:v for k,v in STATE.items() if k not in ("report","data_zip")}
                x["ready"]=bool(STATE.get("data_zip"))
            return self.sendx(json.dumps(x).encode(),"application/json")
        if self.path=="/download-data":
            with LOCK: z=STATE.get("data_zip")
            if z and Path(z).exists():
                return self.sendx(Path(z).read_bytes(),"application/zip",200,{"Content-Disposition":f'attachment; filename="{Path(z).name}"'})
            return self.sendx(b"No candle ZIP ready","text/plain",404)
        if self.path=="/download":
            with LOCK:r=STATE.get("report")
            if r and Path(r).exists(): return self.sendx(Path(r).read_bytes(),"text/csv",200,{"Content-Disposition":'attachment; filename="genetic_engine_v1_365d_trades.csv"'})
            return self.sendx(b"No report yet","text/plain",404)
        self.sendx(b"Not found","text/plain",404)
    def do_POST(self):
        if self.path=="/run":
            try:
                n=int(self.headers.get("Content-Length","0") or 0)
                data=json.loads(self.rfile.read(n) or b"{}")
                fd=data.get("from"); td=data.get("to")
                if not fd or not td: return self.sendx(b'{"ok":false,"error":"Dates required"}',"application/json",400)
                with LOCK:
                    if not STATE["running"]:
                        STATE["running"]=True
                        threading.Thread(target=work,args=(fd,td),daemon=True).start()
                return self.sendx(b'{"ok":true}',"application/json")
            except Exception as e:
                return self.sendx(json.dumps({"ok":False,"error":str(e)}).encode(),"application/json",400)
        self.sendx(b"Not found","text/plain",404)
    def log_message(self,*a): pass

if __name__=="__main__": ThreadingHTTPServer(("0.0.0.0",int(os.environ.get("PORT","8080"))),H).serve_forever()
