import os, json, urllib.request
from flask import Flask, jsonify, request, Response

app=Flask(__name__)

RPCS=[
 "https://eth-mainnet.g.alchemy.com/public",
 "https://rpc.nodeflare.app/eth/public",
 "https://cloudflare-eth.com"
]

# Ethereum mainnet
TOKENS={
 "WETH":("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",18),
 "USDC":("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",6),
 "USDT":("0xdAC17F958D2ee523a2206206994597C13D831ec7",6),
 "DAI": ("0x6B175474E89094C44Da98b954EedeAC495271d0F",18),
 "WBTC":("0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",8)
}
# V2-compatible factories
DEXES={
 "Uniswap V2":"0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",
 "SushiSwap V2":"0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac"
}
GETPAIR="e6a43905"
GETRESERVES="0902f1ac"
TOKEN0="0dfe1681"

def padaddr(a): return a.lower().replace("0x","").rjust(64,"0")

def rpc(method,params):
    body=json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
    last=None
    for u in RPCS:
        try:
            req=urllib.request.Request(u,data=body,headers={"Content-Type":"application/json","User-Agent":"Mozilla/5.0"})
            with urllib.request.urlopen(req,timeout=10) as r:
                j=json.loads(r.read().decode())
            if "result" in j:return j["result"]
            last=str(j.get("error"))
        except Exception as e:last=str(e)
    raise RuntimeError("RPC unavailable: "+str(last))

def eth_call(to,data):
    return rpc("eth_call",[{"to":to,"data":"0x"+data},"latest"])

def pair(factory,a,b):
    x=eth_call(factory,GETPAIR+padaddr(a)+padaddr(b))
    if not x or int(x,16)==0:return None
    return "0x"+x[-40:]

def reserves(pairaddr):
    x=eth_call(pairaddr,GETRESERVES)[2:]
    return int(x[0:64],16),int(x[64:128],16)

def token0(pairaddr):
    x=eth_call(pairaddr,TOKEN0)
    return "0x"+x[-40:].lower()

def pool(dex,base,quote):
    ba,bd=TOKENS[base];qa,qd=TOKENS[quote]
    pa=pair(DEXES[dex],ba,qa)
    if not pa:return None
    r0,r1=reserves(pa); t0=token0(pa)
    if t0==ba.lower():
        rb,rq=r0/(10**bd),r1/(10**qd)
    else:
        rb,rq=r1/(10**bd),r0/(10**qd)
    if rb<=0 or rq<=0:return None
    return {"pair":pa,"base_reserve":rb,"quote_reserve":rq,"price":rq/rb}

def swap_out(amount_in,res_in,res_out,fee=.003):
    ai=amount_in*(1-fee)
    return (ai*res_out)/(res_in+ai)

def gas_usd():
    try:
        wei=int(rpc("eth_gasPrice",[]),16)
        # WETH/USDC Uniswap price as ETH USD proxy
        u=pool("Uniswap V2","WETH","USDC")
        ethusd=u["price"] if u else 0
        # conservative two swaps ~300k gas total
        return wei/1e18*300000*ethusd
    except:return 0

@app.get("/api/scan")
def scan():
    capital=max(1,float(request.args.get("capital",100)))
    gas_override=float(request.args.get("gas",0))
    rows=[]; errors=[]
    gas=gas_override if gas_override>0 else gas_usd()
    for mid in ["WETH","WBTC","DAI","USDT"]:
        try:
            pools={}
            for dex in DEXES:
                z=pool(dex,mid,"USDC")
                if z:pools[dex]=z
            if len(pools)<2:continue
            for buy in pools:
                for sell in pools:
                    if buy==sell:continue
                    # Spend USDC on buy DEX -> MID, then MID on sell DEX -> USDC
                    bp=pools[buy]; sp=pools[sell]
                    midout=swap_out(capital,bp["quote_reserve"],bp["base_reserve"])
                    usdback=swap_out(midout,sp["base_reserve"],sp["quote_reserve"])
                    before=usdback-capital
                    net=before-gas
                    rows.append({
                      "route":f"USDC → {mid} → USDC","buy":buy,"sell":sell,
                      "buy_price":bp["price"],"sell_price":sp["price"],
                      "gross_spread_pct":(sp["price"]/bp["price"]-1)*100,
                      "before_gas":before,"gas":gas,"profit":net,
                      "net_pct":net/capital*100
                    })
        except Exception as e:errors.append(mid+": "+str(e))
    rows.sort(key=lambda x:x["profit"],reverse=True)
    return jsonify(rows=rows,errors=errors,gas=gas,paper_only=True)

@app.get("/health")
def health():
    try:return jsonify(ok=True,block=int(rpc("eth_blockNumber",[]),16),paper_only=True)
    except Exception as e:return jsonify(ok=False,error=str(e),paper_only=True),503

HTML=r"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>DEX Arbitrage Paper</title>
<style>*{box-sizing:border-box}body{margin:0;padding:14px;background:#090e14;color:#edf3f8;font-family:Arial}.w{max-width:1000px;margin:auto}.c{background:#121a23;border:1px solid #293746;border-radius:15px;padding:15px;margin-bottom:12px}.sub{color:#aab8c5;line-height:1.5;font-size:13px}.g{display:grid;grid-template-columns:1fr 1fr;gap:10px}label{font-size:12px;color:#aab8c5}input{width:100%;padding:10px;margin-top:5px;background:#091018;color:white;border:1px solid #34475a;border-radius:8px}button{padding:12px 17px;border:0;border-radius:9px;background:#3b82f6;color:white;font-weight:bold}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:9px 7px;border-bottom:1px solid #263442;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}.yes{background:#123d24}.pos{color:#6ee7a0;font-weight:bold}.neg{color:#ff9696}.err{color:#ffaaaa;font-size:12px}.scroll{overflow-x:auto}</style></head><body><div class=w>
<div class=c><h2>Ethereum DEX Arbitrage — Paper</h2><div class=sub>NO API KEY • Ethereum public RPC • Uniswap V2 ↔ SushiSwap V2. USDC se token buy karke doosre DEX par USDC mein paper-sell simulate karta hai. Pool liquidity, 0.30% swap fee each leg aur estimated Ethereum gas included. Koi wallet ya real order nahi.</div></div>
<div class=c><div class=g><label>Paper Capital USDC<input id=capital value=100 type=number></label><label>Gas override $ (0 = live estimate)<input id=gas value=0 type=number step=.1></label></div><p><button onclick=go()>Scan DEX Now</button> <span id=st>Ready</span></p><div id=er class=err></div></div>
<div class="c scroll"><table><thead><tr><th>Route</th><th>Buy DEX</th><th>Sell DEX</th><th>Gross %</th><th>Before Gas $</th><th>Gas $</th><th>Net %</th><th>Profit $</th></tr></thead><tbody id=tb></tbody></table></div></div>
<script>let busy=0;function f(x,d=3){return Number(x).toFixed(d)}async function go(){if(busy)return;busy=1;st.textContent="Scanning live pools...";er.textContent="";try{let q=new URLSearchParams({capital:capital.value,gas:gas.value});let j=await(await fetch("/api/scan?"+q,{cache:"no-store"})).json();tb.innerHTML="";j.rows.forEach(x=>{let r=document.createElement("tr");if(x.profit>0)r.className="yes";r.innerHTML=`<td>${x.route}</td><td>${x.buy}</td><td>${x.sell}</td><td>${f(x.gross_spread_pct)}%</td><td>${f(x.before_gas)}</td><td>${f(x.gas)}</td><td class="${x.net_pct>0?'pos':'neg'}">${f(x.net_pct)}%</td><td class="${x.profit>0?'pos':'neg'}">${f(x.profit)}</td>`;tb.appendChild(r)});er.textContent=(j.errors||[]).join(" | ");st.textContent="Updated "+new Date().toLocaleTimeString()}catch(e){er.textContent=e;st.textContent="Error"}busy=0}go();setInterval(go,15000)</script></body></html>"""

@app.get("/")
def home():return Response(HTML,mimetype="text/html")
if __name__=="__main__":app.run(host="0.0.0.0",port=int(os.environ.get("PORT","8080")),threaded=True)
