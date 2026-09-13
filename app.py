import json, os
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from flask import Flask, jsonify, request, Response

app = Flask(__name__)

PAIRS = {
    "BTC":  {"coinbase": "BTC-USD",  "kraken": "XBTUSD"},
    "ETH":  {"coinbase": "ETH-USD",  "kraken": "ETHUSD"},
    "SOL":  {"coinbase": "SOL-USD",  "kraken": "SOLUSD"},
    "XRP":  {"coinbase": "XRP-USD",  "kraken": "XRPUSD"},
    "ADA":  {"coinbase": "ADA-USD",  "kraken": "ADAUSD"},
    "DOGE": {"coinbase": "DOGE-USD", "kraken": "DOGEUSD"},
    "LTC":  {"coinbase": "LTC-USD",  "kraken": "LTCUSD"},
}

DEFAULTS = {
    "capital": 100.0,
    "coinbase_fee_pct": 0.60,
    "kraken_fee_pct": 0.40,
    "slippage_pct_each_side": 0.05,
    "min_net_profit_pct": 0.20,
}

def get_json(url, timeout=12):
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 Paper-Arbitrage-Scanner",
        "Accept": "application/json",
        "Cache-Control": "no-cache",
    })
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def coinbase_quote(product):
    d = get_json(f"https://api.exchange.coinbase.com/products/{product}/ticker")
    return {"bid": float(d["bid"]), "ask": float(d["ask"]), "last": float(d["price"])}

def kraken_quote(pair):
    d = get_json("https://api.kraken.com/0/public/Ticker?" + urlencode({"pair": pair}))
    if d.get("error"):
        raise RuntimeError("; ".join(d["error"]))
    result = d["result"]
    if not result:
        raise RuntimeError("No Kraken ticker result")
    x = next(iter(result.values()))
    return {"ask": float(x["a"][0]), "bid": float(x["b"][0]), "last": float(x["c"][0])}

def calc_direction(buy_exchange, buy_ask, sell_exchange, sell_bid, capital, fees, slip):
    buy_fee = fees[buy_exchange] / 100.0
    sell_fee = fees[sell_exchange] / 100.0
    s = slip / 100.0

    buy_exec = buy_ask * (1 + s)
    sell_exec = sell_bid * (1 - s)
    gross_spread_pct = (sell_bid / buy_ask - 1) * 100.0

    qty = capital / (buy_exec * (1 + buy_fee))
    sell_net = qty * sell_exec * (1 - sell_fee)
    profit = sell_net - capital
    net_pct = profit / capital * 100.0 if capital else 0.0

    return {
        "buy_exchange": buy_exchange,
        "sell_exchange": sell_exchange,
        "buy_ask": buy_ask,
        "sell_bid": sell_bid,
        "gross_spread_pct": gross_spread_pct,
        "net_profit_pct": net_pct,
        "profit_usd": profit,
    }

@app.get("/api/scan")
def scan():
    capital = max(1.0, float(request.args.get("capital", DEFAULTS["capital"])))
    cb_fee = max(0.0, float(request.args.get("cb_fee", DEFAULTS["coinbase_fee_pct"])))
    kr_fee = max(0.0, float(request.args.get("kr_fee", DEFAULTS["kraken_fee_pct"])))
    slip = max(0.0, float(request.args.get("slippage", DEFAULTS["slippage_pct_each_side"])))
    min_net = float(request.args.get("min_net", DEFAULTS["min_net_profit_pct"]))

    fees = {"Coinbase": cb_fee, "Kraken": kr_fee}
    rows = []

    for sym, ids in PAIRS.items():
        item = {"symbol": sym, "ok": False}
        try:
            cb = coinbase_quote(ids["coinbase"])
            kr = kraken_quote(ids["kraken"])

            d1 = calc_direction("Coinbase", cb["ask"], "Kraken", kr["bid"], capital, fees, slip)
            d2 = calc_direction("Kraken", kr["ask"], "Coinbase", cb["bid"], capital, fees, slip)
            best = d1 if d1["net_profit_pct"] >= d2["net_profit_pct"] else d2

            item.update({
                "ok": True,
                "best": best,
                "opportunity": best["net_profit_pct"] >= min_net,
            })
        except Exception as e:
            item["error"] = repr(e)
        rows.append(item)

    rows.sort(key=lambda x: x.get("best", {}).get("net_profit_pct", -999), reverse=True)
    return jsonify({
        "paper_only": True,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "capital": capital,
        "rows": rows,
    })

@app.get("/health")
def health():
    return jsonify({"ok": True, "paper_only": True})

HTML = """<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paper Crypto Arbitrage Scanner</title>
<style>
:root{font-family:Arial,sans-serif;color-scheme:dark}
body{margin:0;background:#0b0f14;color:#e7edf3;padding:14px}
.wrap{max-width:1000px;margin:auto}
.card{background:#131a22;border:1px solid #263241;border-radius:14px;padding:14px;margin-bottom:12px}
h2{margin:4px 0 8px}
.note{font-size:13px;color:#aebdca;line-height:1.45}
.controls{display:grid;grid-template-columns:repeat(5,minmax(110px,1fr));gap:8px}
label{font-size:12px;color:#9fb0bf}
input{width:100%;box-sizing:border-box;margin-top:4px;padding:9px;border-radius:8px;border:1px solid #344354;background:#0c1219;color:#fff}
button{padding:11px 14px;border:0;border-radius:9px;background:#3b82f6;color:#fff;font-weight:700}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:9px 7px;border-bottom:1px solid #25303c;text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
.good{background:#11351f}
.bad{color:#aeb7c2}
.err{color:#ff9292}
.pill{display:inline-block;padding:3px 7px;border-radius:10px;background:#213044;font-size:11px}
@media(max-width:760px){.controls{grid-template-columns:1fr 1fr}.tablebox{overflow-x:auto}}
</style>
</head>
<body>
<div class="wrap">
<div class="card">
<h2>Crypto Arbitrage — Paper Scanner</h2>
<div class="note">
Coinbase aur Kraken ke live best bid/ask compare karta hai. Koi real order place nahi hota aur API key nahi chahiye.
Fees aur slippage estimate include hain. Real arbitrage mein dono exchanges par pehle se funds rakhna aam tor par zaroori hota hai.
</div>
</div>

<div class="card">
<div class="controls">
<div><label>Capital $<input id="capital" type="number" value="100" min="1" step="1"></label></div>
<div><label>Coinbase fee %<input id="cb" type="number" value="0.60" min="0" step="0.01"></label></div>
<div><label>Kraken fee %<input id="kr" type="number" value="0.40" min="0" step="0.01"></label></div>
<div><label>Slippage each side %<input id="slip" type="number" value="0.05" min="0" step="0.01"></label></div>
<div><label>Alert if net >= %<input id="minnet" type="number" value="0.20" step="0.01"></label></div>
</div>
<div style="margin-top:10px">
<button onclick="scan()">Scan Now</button>
<span id="stamp" class="pill">Ready</span>
</div>
</div>

<div class="card tablebox">
<table>
<thead>
<tr>
<th>Coin</th><th>Buy</th><th>Sell</th><th>Buy Ask</th><th>Sell Bid</th><th>Gross %</th><th>Net %</th><th>Est. $</th>
</tr>
</thead>
<tbody id="body"><tr><td colspan="8">Press Scan Now</td></tr></tbody>
</table>
</div>
</div>

<script>
let busy=false;
function n(v,d=4){ return Number(v).toLocaleString(undefined,{maximumFractionDigits:d}); }

async function scan(){
 if(busy) return;
 busy=true;
 document.getElementById('stamp').textContent='Scanning...';
 const q=new URLSearchParams({
  capital:document.getElementById('capital').value,
  cb_fee:document.getElementById('cb').value,
  kr_fee:document.getElementById('kr').value,
  slippage:document.getElementById('slip').value,
  min_net:document.getElementById('minnet').value
 });
 try{
  const r=await fetch('/api/scan?'+q.toString(),{cache:'no-store'});
  const j=await r.json();
  const tb=document.getElementById('body');
  tb.innerHTML='';
  for(const x of j.rows){
   const tr=document.createElement('tr');
   if(!x.ok){
    tr.innerHTML=`<td>${x.symbol}</td><td colspan="7" class="err">${x.error||'Error'}</td>`;
   } else {
    if(x.opportunity) tr.className='good';
    const b=x.best;
    tr.innerHTML=`
      <td><b>${x.symbol}</b></td>
      <td>${b.buy_exchange}</td>
      <td>${b.sell_exchange}</td>
      <td>${n(b.buy_ask,8)}</td>
      <td>${n(b.sell_bid,8)}</td>
      <td>${n(b.gross_spread_pct,3)}%</td>
      <td class="${b.net_profit_pct>0?'':'bad'}"><b>${n(b.net_profit_pct,3)}%</b></td>
      <td>${n(b.profit_usd,3)}</td>`;
   }
   tb.appendChild(tr);
  }
  document.getElementById('stamp').textContent='Updated '+new Date().toLocaleTimeString();
 } catch(e){
  document.getElementById('stamp').textContent='Error: '+e;
 }
 busy=false;
}
scan();
setInterval(scan,15000);
</script>
</body>
</html>"""

@app.get("/")
def home():
    return Response(HTML, mimetype="text/html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True)
