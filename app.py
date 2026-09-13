import json,os
from datetime import datetime,timezone
from urllib.parse import urlencode
from urllib.request import Request,urlopen
from flask import Flask,jsonify,request,Response
app=Flask(__name__)
COINS="BTC ETH SOL XRP ADA DOGE LTC AVAX LINK SUI DOT TRX BCH NEAR APT".split()
def get(u):
 r=Request(u,headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"})
 with urlopen(r,timeout=10) as x:return json.loads(x.read().decode())
def OKX():
 d=get("https://openapi.okx.com/api/v5/market/tickers?instType=SPOT");o={}
 for x in d.get("data",[]):
  s=x.get("instId","")
  if s.endswith("-USDT") and s[:-5] in COINS and x.get("bidPx") and x.get("askPx"):o[s[:-5]]=(float(x["bidPx"]),float(x["askPx"]))
 return o
def Bybit():
 d=get("https://api.bybit.com/v5/market/tickers?category=spot");o={};w={c+"USDT":c for c in COINS}
 for x in d.get("result",{}).get("list",[]):
  s=x.get("symbol","")
  if s in w and x.get("bid1Price") and x.get("ask1Price"):o[w[s]]=(float(x["bid1Price"]),float(x["ask1Price"]))
 return o
def Kraken():
 o={}
 for c in COINS:
  try:
   pair=("XBT" if c=="BTC" else c)+"USDT";d=get("https://api.kraken.com/0/public/Ticker?"+urlencode({"pair":pair}))
   if not d.get("error"):
    x=next(iter(d["result"].values()));o[c]=(float(x["b"][0]),float(x["a"][0]))
  except:pass
 return o
@app.get("/api/scan")
def scan():
 cap=max(1,float(request.args.get("capital",100)));sl=max(0,float(request.args.get("slip",.02)))/100;thr=float(request.args.get("threshold",.05))
 fees={"OKX":float(request.args.get("okx",.10))/100,"Bybit":float(request.args.get("bybit",.10))/100,"Kraken":float(request.args.get("kraken",.40))/100}
 mk={};err={}
 for n,f in [("OKX",OKX),("Bybit",Bybit),("Kraken",Kraken)]:
  try:
   z=f()
   if z:mk[n]=z
   else:err[n]="No data"
  except Exception as e:err[n]=str(e)
 rows=[]
 for c in COINS:
  best=None
  for be,bm in mk.items():
   if c not in bm:continue
   for se,sm in mk.items():
    if be==se or c not in sm:continue
    ask=bm[c][1];bid=sm[c][0];qty=cap/(ask*(1+sl)*(1+fees[be]));out=qty*bid*(1-sl)*(1-fees[se]);pr=out-cap
    z={"coin":c,"buy":be,"sell":se,"ask":ask,"bid":bid,"gross":(bid/ask-1)*100,"net":pr/cap*100,"profit":pr}
    if best is None or z["net"]>best["net"]:best=z
  if best:best["positive"]=best["net"]>=thr;rows.append(best)
 rows.sort(key=lambda x:x["net"],reverse=True)
 return jsonify(rows=rows,errors=err,exchanges=list(mk),paper_only=True,time=datetime.now(timezone.utc).isoformat())
@app.get("/health")
def health():return jsonify(ok=True,paper_only=True)
HTML='''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Arbitrage Scanner</title><style>*{box-sizing:border-box}body{margin:0;padding:14px;background:#090e14;color:#edf3f8;font-family:Arial}.w{max-width:1050px;margin:auto}.c{background:#121a23;border:1px solid #283646;border-radius:15px;padding:15px;margin-bottom:12px}.g{display:grid;grid-template-columns:repeat(6,1fr);gap:8px}label{font-size:11px;color:#9fb0bf}input{width:100%;margin-top:4px;padding:9px;background:#091018;color:white;border:1px solid #34475a;border-radius:8px}button{padding:11px 16px;border:0;border-radius:9px;background:#3b82f6;color:white;font-weight:bold}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:9px 7px;border-bottom:1px solid #263442;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}.yes{background:#123d24}.pos{color:#6ee7a0}.neg{color:#ff9696}.e{color:#ffb4b4;font-size:12px}.sub{color:#aab8c5;font-size:13px}@media(max-width:760px){.g{grid-template-columns:1fr 1fr}.scroll{overflow-x:auto}}</style></head><body><div class=w><div class=c><h2>Multi-Exchange Arbitrage — Paper</h2><div class=sub>OKX + Bybit + Kraken • USDT Spot • auto scan 10 sec • fees/slippage included • no real orders</div></div><div class=c><div class=g><label>Capital $<input id=capital value=100></label><label>OKX fee %<input id=okx value=.10></label><label>Bybit fee %<input id=bybit value=.10></label><label>Kraken fee %<input id=kraken value=.40></label><label>Slippage/side %<input id=slip value=.02></label><label>Green net >= %<input id=threshold value=.05></label></div><p><button onclick=go()>Scan Now</button> <span id=st>Ready</span></p><div id=er class=e></div></div><div class="c scroll"><table><thead><tr><th>Coin</th><th>Buy</th><th>Sell</th><th>Ask</th><th>Bid</th><th>Gross %</th><th>Net %</th><th>Est $</th></tr></thead><tbody id=tb></tbody></table></div></div><script>let b=0;function f(x,d=5){return Number(x).toLocaleString(undefined,{maximumFractionDigits:d})}async function go(){if(b)return;b=1;st.textContent="Scanning...";let q=new URLSearchParams();["capital","okx","bybit","kraken","slip","threshold"].forEach(i=>q.set(i,document.getElementById(i).value));try{let j=await(await fetch("/api/scan?"+q,{cache:"no-store"})).json();tb.innerHTML="";j.rows.forEach(x=>{let r=document.createElement("tr");if(x.positive)r.className="yes";r.innerHTML=`<td><b>${x.coin}</b></td><td>${x.buy}</td><td>${x.sell}</td><td>${f(x.ask,8)}</td><td>${f(x.bid,8)}</td><td>${f(x.gross,3)}%</td><td class=${x.net>=0?"pos":"neg"}>${f(x.net,3)}%</td><td>${f(x.profit,3)}</td>`;tb.appendChild(r)});er.textContent=Object.entries(j.errors).map(x=>x[0]+": "+x[1]).join(" | ");st.textContent="Updated "+new Date().toLocaleTimeString()+" • "+j.exchanges.join(", ")}catch(e){er.textContent=e;st.textContent="Error"}b=0}go();setInterval(go,10000)</script></body></html>'''
@app.get("/")
def home():return Response(HTML,mimetype="text/html")
if __name__=="__main__":app.run(host="0.0.0.0",port=int(os.environ.get("PORT","8080")),threaded=True)
