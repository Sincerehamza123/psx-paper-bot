import os, json, time, urllib.request, urllib.parse
from flask import Flask, jsonify, request, Response

app = Flask(__name__)
BASE = "https://www.okx.com"

# Liquid USDT spot + perpetual names to scan.
COINS = [
    "BTC","ETH","SOL","XRP","DOGE","ADA","LINK","AVAX","LTC","BCH",
    "SUI","DOT","TRX","NEAR","APT","ETC","ARB","OP","FIL","INJ"
]

def jget(path, params=None, timeout=12):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent":"Mozilla/5.0",
        "Accept":"application/json"
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.loads(r.read().decode())
    if j.get("code") not in (None, "0"):
        raise RuntimeError(j.get("msg") or ("OKX code "+str(j.get("code"))))
    return j.get("data", [])

def all_tickers(inst_type):
    rows = jget("/api/v5/market/tickers", {"instType":inst_type})
    return {x.get("instId"): x for x in rows}

def funding(inst_id):
    d = jget("/api/v5/public/funding-rate", {"instId":inst_id})
    if not d: return None
    x = d[0]
    def fv(k):
        try:return float(x.get(k) or 0)
        except:return 0.0
    return {
        "rate": fv("fundingRate"),
        "next_rate": fv("nextFundingRate"),
        "funding_time": int(x.get("fundingTime") or 0),
        "next_funding_time": int(x.get("nextFundingTime") or x.get("fundingTime") or 0),
        "method": x.get("method",""),
        "formula": x.get("formulaType","")
    }

def px(t, side):
    # executable top-of-book proxy
    try:
        v = float(t.get(side) or 0)
        if v > 0: return v
    except: pass
    try:return float(t.get("last") or 0)
    except:return 0

@app.get("/api/scan")
def scan():
    capital = max(10.0, float(request.args.get("capital",100)))
    spot_fee = max(0.0, float(request.args.get("spotfee",0.10))) / 100.0
    perp_fee = max(0.0, float(request.args.get("perpfee",0.05))) / 100.0
    slip = max(0.0, float(request.args.get("slippage",0.02))) / 100.0
    hold_settles = max(1, min(20, int(request.args.get("settles",3))))
    min_funding = float(request.args.get("minfunding",0.05)) / 100.0
    max_basis = max(0.0, float(request.args.get("maxbasis",1.0))) / 100.0

    spot = all_tickers("SPOT")
    swap = all_tickers("SWAP")
    rows, errors = [], []

    # Capital model: half cash buys spot. Matching perp notional = same half-capital.
    # Remaining half is reserved as derivatives collateral. No leverage assumption required.
    leg_notional = capital / 2.0

    # Round-trip costs if both spot and perpetual are eventually closed.
    roundtrip_cost_usd = leg_notional * (2*spot_fee + 2*perp_fee + 4*slip)
    roundtrip_cost_pct_capital = roundtrip_cost_usd / capital * 100.0

    for c in COINS:
        sid=f"{c}-USDT"
        pid=f"{c}-USDT-SWAP"
        st=spot.get(sid); pt=swap.get(pid)
        if not st or not pt: continue
        try:
            # Enter: buy spot at ask, short perp at bid.
            sask=px(st,"askPx"); pbid=px(pt,"bidPx")
            if sask<=0 or pbid<=0: continue
            basis=(pbid/sask)-1.0
            fr=funding(pid)
            if not fr: continue

            rate=fr["rate"]
            # Positive funding: long pays short => spot long + perp short receives funding.
            projected_funding_usd = leg_notional * max(rate,0) * hold_settles
            projected_net = projected_funding_usd - roundtrip_cost_usd
            projected_net_pct = projected_net/capital*100.0

            # Number of settlements needed for current rate to repay estimated round-trip friction.
            if rate>0:
                breakeven = roundtrip_cost_usd / (leg_notional*rate)
            else:
                breakeven = 9999

            status="NO"
            reason=[]
            if rate <= 0:
                reason.append("funding <= 0")
            if rate < min_funding:
                reason.append("funding below filter")
            if abs(basis) > max_basis:
                reason.append("basis too wide")
            if projected_net <= 0:
                reason.append("fees/slippage exceed projected funding")
            if not reason:
                status="PAPER ENTRY"

            rows.append({
                "coin":c,
                "spot_ask":sask,
                "perp_bid":pbid,
                "basis_pct":basis*100,
                "funding_pct":rate*100,
                "funding_time":fr["funding_time"],
                "leg_notional":leg_notional,
                "projected_funding_usd":projected_funding_usd,
                "roundtrip_cost_usd":roundtrip_cost_usd,
                "cost_pct_capital":roundtrip_cost_pct_capital,
                "projected_net_usd":projected_net,
                "projected_net_pct":projected_net_pct,
                "breakeven_settlements":breakeven,
                "status":status,
                "reason":", ".join(reason) if reason else "positive after filters"
            })
        except Exception as e:
            errors.append(c+": "+str(e))

    rows.sort(key=lambda x:(x["status"]=="PAPER ENTRY", x["projected_net_pct"], x["funding_pct"]), reverse=True)
    return jsonify(
        paper_only=True,
        exchange="OKX",
        strategy="Positive funding: BUY spot + SHORT same-coin USDT perpetual",
        capital=capital,
        hold_settlements=hold_settles,
        rows=rows,
        errors=errors
    )

@app.get("/health")
def health():
    try:
        d=jget("/api/v5/market/tickers",{"instType":"SPOT"})
        return jsonify(ok=True, exchange="OKX", tickers=len(d), paper_only=True)
    except Exception as e:
        return jsonify(ok=False,error=str(e),paper_only=True),503

HTML=r"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Funding Arbitrage Paper Bot</title>
<style>
*{box-sizing:border-box}body{margin:0;padding:14px;background:#081018;color:#eef5fb;font-family:Arial}.w{max-width:1150px;margin:auto}
.c{background:#111c27;border:1px solid #2a3b4d;border-radius:15px;padding:15px;margin-bottom:12px}.sub{color:#a9bac9;line-height:1.5;font-size:13px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}label{font-size:12px;color:#a9bac9}input{width:100%;padding:10px;margin-top:5px;background:#07111a;color:#fff;border:1px solid #38506a;border-radius:8px}
button{padding:12px 17px;border:0;border-radius:9px;background:#377df0;color:#fff;font-weight:bold}.scroll{overflow:auto}
table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:9px 7px;border-bottom:1px solid #263746;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}
.entry{background:#103b25}.pos{color:#6ce99b;font-weight:bold}.neg{color:#ff9292}.warn{color:#ffc978;font-size:12px}.muted{color:#9eb0c0}
@media(max-width:650px){.grid{grid-template-columns:1fr 1fr}}
</style></head><body><div class=w>
<div class=c><h2>OKX Funding Arbitrage — PAPER BOT</h2>
<div class=sub><b>Direction-neutral concept:</b> same coin ka Spot BUY + USDT Perpetual SHORT. Positive funding mein short side funding receive karti hai. Bot current funding ko fees/slippage ke against test karta hai. Wallet/API key/real order nahi.</div></div>
<div class=c><div class=grid>
<label>Total Paper Capital $<input id=capital value=100 type=number></label>
<label>Spot Fee / side %<input id=spotfee value=.10 type=number step=.01></label>
<label>Perp Fee / side %<input id=perpfee value=.05 type=number step=.01></label>
<label>Slippage / trade %<input id=slippage value=.02 type=number step=.01></label>
<label>Projected Funding Settlements<input id=settles value=3 type=number min=1 max=20></label>
<label>Min Funding / settlement %<input id=minfunding value=.05 type=number step=.01></label>
<label>Max Spot-Perp Basis %<input id=maxbasis value=1.0 type=number step=.1></label>
</div>
<p><button onclick=go()>Scan Funding Now</button> <span id=st>Ready</span></p>
<div class=warn>Green “PAPER ENTRY” guarantee nahi. Current funding next settlement tak change ho sakti hai. Real version se pehle long paper log/backtest zaroori hai.</div><div id=err class=neg></div></div>
<div class="c scroll"><table><thead><tr>
<th>Coin</th><th>Status</th><th>Funding %</th><th>Basis %</th><th>Spot Ask</th><th>Perp Bid</th><th>Projected Funding $</th><th>Round-trip Cost $</th><th>Projected Net $</th><th>Net % Capital</th><th>Break-even Settlements</th>
</tr></thead><tbody id=tb></tbody></table></div></div>
<script>
let busy=0;const f=(x,n=3)=>Number(x).toFixed(n);
async function go(){if(busy)return;busy=1;st.textContent="Scanning OKX spot + perpetual funding...";err.textContent="";
try{
 let q=new URLSearchParams({capital:capital.value,spotfee:spotfee.value,perpfee:perpfee.value,slippage:slippage.value,settles:settles.value,minfunding:minfunding.value,maxbasis:maxbasis.value});
 let j=await(await fetch("/api/scan?"+q,{cache:"no-store"})).json();tb.innerHTML="";
 j.rows.forEach(x=>{let r=document.createElement("tr");if(x.status==="PAPER ENTRY")r.className="entry";
 r.innerHTML=`<td>${x.coin}</td><td class="${x.status==="PAPER ENTRY"?'pos':'muted'}">${x.status}</td><td class="${x.funding_pct>0?'pos':'neg'}">${f(x.funding_pct,4)}%</td><td>${f(x.basis_pct,3)}%</td><td>${f(x.spot_ask,6)}</td><td>${f(x.perp_bid,6)}</td><td>${f(x.projected_funding_usd,3)}</td><td>${f(x.roundtrip_cost_usd,3)}</td><td class="${x.projected_net_usd>0?'pos':'neg'}">${f(x.projected_net_usd,3)}</td><td class="${x.projected_net_pct>0?'pos':'neg'}">${f(x.projected_net_pct,3)}%</td><td>${x.breakeven_settlements>1000?'—':f(x.breakeven_settlements,1)}</td>`;
 r.title=x.reason;tb.appendChild(r)});
 err.textContent=(j.errors||[]).join(" | ");st.textContent="Updated "+new Date().toLocaleTimeString()+" • "+j.rows.length+" coins";
}catch(e){err.textContent=e;st.textContent="Error"}busy=0}
go();setInterval(go,30000);
</script></body></html>"""

@app.get("/")
def home(): return Response(HTML,mimetype="text/html")

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT","8080")),threaded=True)
