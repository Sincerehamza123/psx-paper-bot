import os,time,json,urllib.request
from flask import Flask,jsonify,request,Response
app=Flask(__name__)

CHAIN="arbitrum"
TOKENS={
"WETH":"0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
"USDC":"0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
"ARB":"0x912CE59144191C1204E64559FE8253a0e49E6548",
"LINK":"0xf97f4df75117a78c1A5a0DBb814Af92458539FB4",
"WBTC":"0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f"
}
# Paper assumptions. Scanner is discovery-grade: final executable quote should be verified before any real trade.
DEX_FEE={"uniswap":0.0030,"sushiswap":0.0030,"camelot":0.0030,"ramses":0.0030,"curve":0.0010}
DEFAULT_FEE=0.0030

def get(url):
 req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"})
 with urllib.request.urlopen(req,timeout=12) as r:return json.loads(r.read().decode())

def pools(sym):
 a=TOKENS[sym]
 return get(f"https://api.dexscreener.com/token-pairs/v1/{CHAIN}/{a}")

def normalize(sym,arr,minliq):
 out=[]
 for x in arr or []:
  try:
   b=x.get("baseToken",{}); q=x.get("quoteToken",{})
   bs=(b.get("symbol") or "").upper(); qs=(q.get("symbol") or "").upper()
   # only USD-like quote pools so prices are comparable
   if qs not in ("USDC","USDC.E","USDT","DAI"): continue
   price=float(x.get("priceUsd") or 0); liq=float((x.get("liquidity") or {}).get("usd") or 0)
   if price<=0 or liq<minliq: continue
   dex=(x.get("dexId") or "unknown").lower()
   out.append({"token":sym,"dex":dex,"pair":x.get("pairAddress",""),"price":price,
               "liq":liq,"fee":DEX_FEE.get(dex,DEFAULT_FEE),"url":x.get("url","")})
  except: pass
 return out

def impact(trade,liq):
 # conservative paper estimate for a swap against displayed pool liquidity
 side=max(liq/2,1)
 return min(0.25, trade/side)

@app.get("/api/scan")
def scan():
 capital=max(10,float(request.args.get("capital",100)))
 minliq=max(10000,float(request.args.get("minliq",250000)))
 gas=max(0,float(request.args.get("gas",0.03)))
 rows=[]; errors=[]
 for sym in ("WETH","ARB","LINK","WBTC"):
  try:
   ps=normalize(sym,pools(sym),minliq)
   # compare distinct pools/DEXs
   for buy in ps:
    for sell in ps:
     if buy["pair"]==sell["pair"] or buy["dex"]==sell["dex"]:continue
     if sell["price"]<=buy["price"]:continue
     gross=(sell["price"]/buy["price"]-1)
     fees=buy["fee"]+sell["fee"]
     imp=impact(capital,buy["liq"])+impact(capital,sell["liq"])
     net=gross-fees-imp-(gas/capital)
     rows.append({"token":sym,"buy":buy["dex"],"sell":sell["dex"],
      "buy_price":buy["price"],"sell_price":sell["price"],
      "buy_liq":buy["liq"],"sell_liq":sell["liq"],
      "gross_pct":gross*100,"fees_pct":fees*100,"impact_pct":imp*100,
      "gas":gas,"net_pct":net*100,"profit":capital*net,
      "buy_url":buy["url"],"sell_url":sell["url"]})
  except Exception as e:errors.append(sym+": "+str(e))
 rows.sort(key=lambda z:z["net_pct"],reverse=True)
 return jsonify(rows=rows[:80],errors=errors,paper_only=True,chain="Arbitrum",
                note="Discovery estimate from public pool prices; not an executable quote.")

HTML=r"""<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1"><title>Arbitrum Arbitrage Paper</title><style>
*{box-sizing:border-box}body{margin:0;background:#081018;color:#edf4fb;font-family:Arial;padding:14px}.w{max-width:1100px;margin:auto}.c{background:#111c27;border:1px solid #2a3b4d;border-radius:15px;padding:15px;margin-bottom:12px}.sub{color:#a9bac9;line-height:1.5;font-size:13px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}label{font-size:12px;color:#a9bac9}input{width:100%;padding:10px;margin-top:5px;background:#07111a;color:white;border:1px solid #38506a;border-radius:8px}button{padding:12px 17px;border:0;border-radius:9px;background:#377df0;color:white;font-weight:bold}.scroll{overflow:auto}table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:9px 7px;border-bottom:1px solid #263746;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}.win{background:#103b25}.pos{color:#67e89a;font-weight:bold}.neg{color:#ff9292}.warn{color:#ffc978;font-size:12px}@media(max-width:650px){.grid{grid-template-columns:1fr 1fr}}</style></head><body><div class=w>
<div class=c><h2>Arbitrum Multi-DEX Arbitrage — PAPER</h2><div class=sub>API key nahi • wallet nahi • real order nahi. WETH, ARB, LINK, WBTC ke liquid Arbitrum pools ko multiple DEXs par compare karta hai. Green sirf tab jab estimated DEX fees + liquidity impact + gas ke baad net positive ho.</div></div>
<div class=c><div class=grid><label>Capital $<input id=cap value=100 type=number></label><label>Min Pool Liquidity $<input id=liq value=250000 type=number></label><label>Gas Estimate $<input id=gas value=.03 type=number step=.01></label></div><p><button onclick=go()>Scan Now</button> <span id=st>Ready</span></p><div class=warn>Note: yeh discovery/paper estimate hai. Real-money trade se pehle executable on-chain quote zaroor verify hota hai.</div><div id=err class=neg></div></div>
<div class="c scroll"><table><thead><tr><th>Token</th><th>Buy</th><th>Sell</th><th>Gross %</th><th>Fees %</th><th>Impact %</th><th>Gas $</th><th>Net %</th><th>Profit $</th><th>Buy Liq</th><th>Sell Liq</th></tr></thead><tbody id=tb></tbody></table></div></div>
<script>let busy=0;const f=(x,n=3)=>Number(x).toFixed(n);async function go(){if(busy)return;busy=1;st.textContent="Scanning Arbitrum pools...";err.textContent="";try{let q=new URLSearchParams({capital:cap.value,minliq:liq.value,gas:gas.value});let j=await(await fetch("/api/scan?"+q,{cache:"no-store"})).json();tb.innerHTML="";j.rows.forEach(x=>{let r=document.createElement("tr");if(x.net_pct>0)r.className="win";r.innerHTML=`<td>${x.token}</td><td>${x.buy}</td><td>${x.sell}</td><td>${f(x.gross_pct)}%</td><td>${f(x.fees_pct)}%</td><td>${f(x.impact_pct)}%</td><td>${f(x.gas)}</td><td class="${x.net_pct>0?'pos':'neg'}">${f(x.net_pct)}%</td><td class="${x.profit>0?'pos':'neg'}">${f(x.profit)}</td><td>$${Math.round(x.buy_liq).toLocaleString()}</td><td>$${Math.round(x.sell_liq).toLocaleString()}</td>`;tb.appendChild(r)});err.textContent=(j.errors||[]).join(" | ");st.textContent="Updated "+new Date().toLocaleTimeString()+" • "+j.rows.length+" routes"}catch(e){err.textContent=e;st.textContent="Error"}busy=0}go();setInterval(go,15000)</script></body></html>"""
@app.get("/")
def home():return Response(HTML,mimetype="text/html")
@app.get("/health")
def health():return jsonify(ok=True,paper_only=True,chain="Arbitrum")
if __name__=="__main__":app.run(host="0.0.0.0",port=int(os.environ.get("PORT","8080")),threaded=True)
