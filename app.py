
import json, threading, time, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, Response, request

app = Flask(__name__)
BASE = "https://www.okx.com"

START_CAPITAL = 100.0
LEVERAGE = 5.0
FEE = 0.0005       # 0.05% per side
SLIP = 0.0001      # 0.01% per side
RSI_LEN = 14
RSI_LOW = 50.0
RSI_HIGH = 60.0
MAX_DOLLAR_LOSS = 5.0

def get_all_usdt_spot_pairs():
    """All currently LIVE OKX USDT spot pairs, excluding stablecoin/fiat-like bases."""
    raw = api_get("/api/v5/public/instruments", {"instType":"SPOT"})
    exclude = {
        "USDC","USDT","DAI","FDUSD","TUSD","USDP","EUR","EURT","GBP","AUD",
        "TRY","BRL","AED","SGD","USD","PYUSD","USDE","USD0"
    }
    pairs=[]
    for x in raw:
        inst=x.get("instId","")
        base=x.get("baseCcy","").upper()
        quote=x.get("quoteCcy","").upper()
        state_=x.get("state","")
        if quote=="USDT" and state_=="live" and base not in exclude:
            pairs.append(inst)
    return sorted(set(pairs))

state = {
    "test": {
        "running": False,
        "progress": "",
        "result": None,
        "error": None,
        "last_run": None
    }
}
lock = threading.RLock()

def api_get(path, params=None, timeout=25):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 DailyRSINoLowerWick/1.0",
        "Accept": "application/json"
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        obj = json.loads(r.read().decode("utf-8"))
    if obj.get("code") != "0":
        raise RuntimeError(obj.get("msg") or "OKX error")
    return obj.get("data", [])

def parse_bar(r):
    return {
        "ts": int(r[0]),
        "o": float(r[1]),
        "h": float(r[2]),
        "l": float(r[3]),
        "c": float(r[4]),
        "v": float(r[5]),
        "ok": str(r[8]) if len(r) > 8 else "1"
    }

def fetch_daily(inst, days=105):
    target = int((datetime.now(timezone.utc)-timedelta(days=days+30)).timestamp()*1000)
    rows = {}
    after = None
    for _ in range(10):
        p = {"instId":inst, "bar":"1Dutc", "limit":"100"}
        if after is not None:
            p["after"] = str(after)
        raw = api_get("/api/v5/market/history-candles", p)
        if not raw:
            break
        batch = [parse_bar(x) for x in raw if len(x) >= 8]
        if not batch:
            break
        for b in batch:
            if b["ok"] == "1":
                rows[b["ts"]] = b
        oldest = min(x["ts"] for x in batch)
        if oldest <= target:
            break
        after = oldest
        time.sleep(0.03)
    out = sorted(rows.values(), key=lambda x:x["ts"])
    return [x for x in out if x["ts"] >= target]

def date_str(ts):
    return datetime.fromtimestamp(ts/1000, tz=timezone.utc).date().isoformat()

def rsi_series(closes, n=14):
    out = [None]*len(closes)
    if len(closes) < n+1:
        return out
    gains = []
    losses = []
    for i in range(1, n+1):
        d = closes[i]-closes[i-1]
        gains.append(max(d,0))
        losses.append(max(-d,0))
    ag = sum(gains)/n
    al = sum(losses)/n
    rs = float("inf") if al == 0 else ag/al
    out[n] = 100.0 if al == 0 else 100 - 100/(1+rs)
    for i in range(n+1, len(closes)):
        d = closes[i]-closes[i-1]
        ag = (ag*(n-1)+max(d,0))/n
        al = (al*(n-1)+max(-d,0))/n
        rs = float("inf") if al == 0 else ag/al
        out[i] = 100.0 if al == 0 else 100 - 100/(1+rs)
    return out



def dollar_stop_raw(entry_raw, entry_exec, qty):
    """
    Stop price sized so estimated total loss including fees/slippage
    is approximately MAX_DOLLAR_LOSS.
    """
    if qty <= 0:
        return 0.0
    per_unit = MAX_DOLLAR_LOSS / qty
    num = entry_exec*(1+FEE) - per_unit
    den = (1-SLIP)*(1-FEE)
    if den <= 0:
        return 0.0
    x = num/den
    return max(0.0, min(x, entry_raw))

def build_coin_rows(inst, bars):
    closes = [x["c"] for x in bars]
    rsis = rsi_series(closes, RSI_LEN)
    rows = []
    for i,b in enumerate(bars):
        rows.append({
            **b,
            "coin":inst,
            "day":date_str(b["ts"]),
            "rsi":rsis[i],
            "i":i
        })
    return rows

def is_signal(row):
    # User rule:
    # 1) Daily candle must be green.
    # 2) Low must not go below open (no lower wick).
    #    Small floating tolerance is used for exchange precision.
    # 3) RSI(14) must be >50 and <60 at day close.
    tol = max(abs(row["o"])*1e-8, 1e-12)
    no_lower_wick = row["l"] >= row["o"] - tol
    return (
        row["c"] > row["o"] and
        no_lower_wick and
        row["rsi"] is not None and
        RSI_LOW < row["rsi"] < RSI_HIGH
    )

def simulate(cache, period_days):
    cutoff = (datetime.now(timezone.utc).date()-timedelta(days=period_days)).isoformat()

    # Create signal candidates. Since "low never below open" and RSI are only
    # fully known at day close, entry is NEXT day's open (avoids look-ahead).
    candidates = {}
    for inst, rows in cache.items():
        for i in range(1, len(rows)-1):
            prev = rows[i-1]
            s = rows[i]
            if s["day"] < cutoff:
                continue

            # New rule: the day BEFORE the signal candle must also be green.
            if not (prev["c"] > prev["o"]):
                continue

            # Signal candle keeps the original rules:
            # green, no lower wick, RSI(14) >50 and <60.
            if not is_signal(s):
                continue

            nxt = rows[i+1]
            body_pct = (s["c"]-s["o"])/s["o"]*100 if s["o"] else 0
            candidates.setdefault(nxt["day"], []).append({
                "coin":inst,
                "prev":prev,
                "signal":s,
                "entry_row":nxt,
                "score":body_pct + (s["rsi"]-50.0)/10.0
            })

    equity = START_CAPITAL
    peak = START_CAPITAL
    max_dd = 0.0
    trades = []
    open_pos = None

    # unified chronological dates
    all_days = sorted({r["day"] for rows in cache.values() for r in rows if r["day"] >= cutoff})

    for day in all_days:
        # First manage open position using this day's close/RSI
        if open_pos is not None:
            rows = cache[open_pos["coin"]]
            row = next((x for x in rows if x["day"] == day), None)
            if row is not None and row["day"] >= open_pos["entry_day"]:
                exit_reason = None
                if row["l"] <= open_pos["stop_raw"]:
                    exit_reason = "$5 MAX LOSS SL"
                    exit_exec = open_pos["stop_raw"] * (1-SLIP)
                elif row["c"] > open_pos["entry_raw"]:
                    exit_reason = "PROFIT EOD"
                    exit_exec = row["c"] * (1-SLIP)

                if exit_reason:
                    qty = open_pos["qty"]
                    gross = qty*(exit_exec-open_pos["entry_exec"])
                    fees = FEE*qty*open_pos["entry_exec"] + FEE*qty*exit_exec
                    net = gross-fees
                    equity += net
                    trades.append({
                        "coin":open_pos["coin"],
                        "signal_day":open_pos["signal_day"],
                        "entry_day":open_pos["entry_day"],
                        "exit_day":day,
                        "signal_rsi":open_pos["signal_rsi"],
                        "exit_rsi":row["rsi"],
                        "entry":open_pos["entry_exec"],
                        "exit":exit_exec,
                        "notional":open_pos["notional"],
                        "net":net,
                        "reason":exit_reason
                    })
                    open_pos = None

                    peak = max(peak, equity)
                    if peak > 0:
                        max_dd = max(max_dd, (peak-equity)/peak*100)

        # If flat, allow a new entry at today's open.
        if open_pos is None and equity > 0 and day in candidates:
            picks = candidates[day]
            pick = sorted(picks, key=lambda x:x["score"], reverse=True)[0]
            e = pick["entry_row"]
            entry_raw = e["o"]
            entry_exec = entry_raw*(1+SLIP)

            # Max 5x current equity; this prevents impossible negative leverage.
            notional = min(START_CAPITAL*LEVERAGE, equity*LEVERAGE)
            if notional > 0:
                qty = notional/entry_exec
                stop_raw = dollar_stop_raw(entry_raw, entry_exec, qty)
                open_pos = {
                    "coin":pick["coin"],
                    "signal_day":pick["signal"]["day"],
                    "signal_rsi":pick["signal"]["rsi"],
                    "entry_day":day,
                    "entry_raw":entry_raw,
                    "entry_exec":entry_exec,
                    "qty":qty,
                    "notional":notional,
                    "stop_raw":stop_raw
                }

                # Entry day itself may qualify for TP or day-end exit.
                row = e
                exit_reason = None
                if row["l"] <= open_pos["stop_raw"]:
                    exit_reason = "$5 MAX LOSS SL"
                    exit_exec = open_pos["stop_raw"]*(1-SLIP)
                elif row["c"] > entry_raw:
                    exit_reason = "PROFIT EOD"
                    exit_exec = row["c"]*(1-SLIP)

                if exit_reason:
                    gross = qty*(exit_exec-entry_exec)
                    fees = FEE*qty*entry_exec + FEE*qty*exit_exec
                    net = gross-fees
                    equity += net
                    trades.append({
                        "coin":open_pos["coin"],
                        "signal_day":open_pos["signal_day"],
                        "entry_day":day,
                        "exit_day":day,
                        "signal_rsi":open_pos["signal_rsi"],
                        "exit_rsi":row["rsi"],
                        "entry":entry_exec,
                        "exit":exit_exec,
                        "notional":notional,
                        "net":net,
                        "reason":exit_reason
                    })
                    open_pos = None
                    peak = max(peak, equity)
                    if peak > 0:
                        max_dd = max(max_dd, (peak-equity)/peak*100)

    # Mark-to-market final open position at last available close so result is honest.
    unrealized = 0.0
    if open_pos is not None:
        rows = cache[open_pos["coin"]]
        last = next((x for x in reversed(rows) if x["day"] >= open_pos["entry_day"]), None)
        if last:
            exit_exec = last["c"]*(1-SLIP)
            qty = open_pos["qty"]
            gross = qty*(exit_exec-open_pos["entry_exec"])
            fees = FEE*qty*open_pos["entry_exec"] + FEE*qty*exit_exec
            unrealized = gross-fees

    wins = sum(1 for t in trades if t["net"] > 0)
    losses = sum(1 for t in trades if t["net"] <= 0)
    profit_exits = sum(1 for t in trades if t["reason"] == "PROFIT EOD")
    dollar_sl_exits = sum(1 for t in trades if t["reason"] == "$5 MAX LOSS SL")
    net = equity-START_CAPITAL

    return {
        "days":period_days,
        "trades":len(trades),
        "wins":wins,
        "losses":losses,
        "win_rate":wins/len(trades)*100 if trades else 0,
        "profit_exits":profit_exits,
        "dollar_sl_exits":dollar_sl_exits,
        "net_pnl":net,
        "end_balance":equity,
        "max_dd":max_dd,
        "open_position": open_pos["coin"] if open_pos else None,
        "unrealized_pnl": unrealized,
        "trades_detail":trades
    }

def run_test(days):
    with lock:
        state["test"]={"running":True,"progress":"Starting...","result":None,"error":None,"last_run":None}
    try:
        days = max(1, min(int(days), 180))
        cache={}
        fetch_days=max(days+30, 60)
        with lock:
            state["test"]["progress"]="Loading all OKX USDT spot pairs..."
        coins=get_all_usdt_spot_pairs()
        total_pairs=len(coins)
        for idx,inst in enumerate(coins,1):
            with lock:
                state["test"]["progress"]=f"Scanning all pairs {idx}/{total_pairs} — {inst}"
            try:
                bars=fetch_daily(inst, fetch_days)
                if len(bars) >= 20:
                    cache[inst]=build_coin_rows(inst,bars)
            except Exception:
                pass

        with lock:
            state["test"]["progress"]=f"Testing {days} days..."
        result = simulate(cache,days)

        with lock:
            state["test"].update({
                "running":False,
                "progress":"Complete",
                "error":None,
                "last_run":datetime.now(timezone.utc).isoformat(),
                "result":{
                    "rules":{
                        "signal":"Previous daily candle must be green; signal candle green with Low >= Open (no lower wick) and RSI(14) >50 and <60",
                        "entry":"Next day OPEN after valid 2-green setup",
                        "max_trades":"Maximum 1 new trade per day; only 1 global open position at a time",
                        "exit_profit":"At day-end, close only if trade is in profit",
                        "exit_sl":"Intraday max planned loss about $5 including fees/slippage",
                        "hold":"If $5 stop is not hit and day-end is not profitable, keep holding",
                        "capital":"$100 starting equity; max 5x notional",
                        "costs":"0.05% fee/side + 0.01% slippage/side"
                    },
                    "days":days,
                    "pairs_found":total_pairs,
                    "coins_loaded":len(cache),
                    "results":[result]
                }
            })
    except Exception as e:
        with lock:
            state["test"].update({"running":False,"progress":"Failed","error":repr(e)})

@app.post("/api/run")
def api_run():
    data = request.get_json(silent=True) or {}
    try:
        days = int(data.get("days",30))
    except Exception:
        days = 30
    with lock:
        if state["test"]["running"]:
            return jsonify({"ok":False,"message":"Test already running"}),409
        threading.Thread(target=run_test,args=(days,),daemon=True).start()
    return jsonify({"ok":True,"days":days})

@app.get("/api/status")
def api_status():
    with lock:
        return jsonify(state["test"])

HTML=r"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>2-Green No-Lower-Wick RSI Strategy</title>
<style>
body{margin:0;background:#071019;color:#eef6ff;font-family:Arial;padding:14px}
.w{max-width:1000px;margin:auto}.c{background:#111d29;border:1px solid #27394b;border-radius:14px;padding:16px;margin-bottom:12px}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}.k{background:#09141e;padding:12px;border-radius:9px}
.sub{color:#a8bacb;line-height:1.5}.v{font-size:18px;font-weight:bold}
button{padding:13px 18px;border:0;border-radius:9px;background:#387df3;color:white;font-size:16px;font-weight:bold}
input[type=number]{width:110px;padding:11px;border-radius:8px;border:1px solid #3b4d60;background:#09141e;color:white;font-size:17px}
table{width:100%;border-collapse:collapse}th,td{padding:9px;border-bottom:1px solid #253645;text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}.scroll{overflow:auto}.g{color:#6ff0a0}.r{color:#ff9999}
@media(max-width:650px){.grid{grid-template-columns:1fr}}
</style></head><body><div class=w>

<div class=c><h2>2-Green No-Lower-Wick + RSI Strategy</h2>
<div class=sub>All OKX LIVE USDT pairs scan honge. Ek din mein maximum 1 new trade.</div></div>

<div class="c grid">
<div class=k><div class=sub>Setup</div><div class=v>Previous Green + Signal Green</div></div>
<div class=k><div class=sub>Signal Candle</div><div class=v>Low ≥ Open + RSI 50–60</div></div>
<div class=k><div class=sub>Entry</div><div class=v>Next Day Open</div></div>
<div class=k><div class=sub>Stop Loss</div><div class=v>Max ~$5 loss</div></div>
<div class=k><div class=sub>Profit Exit</div><div class=v>Day-end only if profitable</div></div>
<div class=k><div class=sub>Capital</div><div class=v>$100 / max 5x</div></div>
</div>

<div class=c>
<h3>Backtest Settings</h3>
<div class=sub>Kitne din ka backtest:</div>
<input id=days type=number value=30 min=1 max=180 step=1>
<br><button style="margin-top:16px" onclick=run()>Run Backtest</button>
<div id=msg class=sub style="margin-top:12px"></div>
<div id=pairinfo class=sub style="margin-top:8px"></div>
</div>

<div class="c scroll">
<h3>Results</h3>
<table><thead><tr>
<th>Days</th><th>Trades</th><th>Win Rate</th><th>EOD Profit</th><th>$5 SL</th><th>Net P/L</th><th>End</th><th>Max DD</th><th>Open</th>
</tr></thead><tbody id=tb></tbody></table>
</div>

<div class="c scroll">
<h3>Trade Details</h3>
<div class=sub>Har trade ka pair, signal date, entry date, exit date, entry/exit price, RSI aur P/L.</div>
<table><thead><tr>
<th>Pair</th><th>Signal Date</th><th>Entry Date</th><th>Exit Date</th>
<th>Entry</th><th>Exit</th><th>Signal RSI</th><th>Exit RSI</th><th>Reason</th><th>P/L</th>
</tr></thead><tbody id=trades></tbody></table>
</div>

</div>
<script>
const f=(x,n=2)=>Number(x||0).toFixed(n);
async function load(){
 const j=await(await fetch('/api/status',{cache:'no-store'})).json();
 msg.textContent=(j.running?'Running: ':'')+(j.progress||'')+(j.error?' | '+j.error:'');
 if(j.result&&j.result.results){
   pairinfo.textContent=`Pairs found: ${j.result.pairs_found||0} | Pairs with usable history: ${j.result.coins_loaded||0}`;
   tb.innerHTML=''; trades.innerHTML='';
   j.result.results.forEach(x=>{
    tb.innerHTML+=`<tr>
    <td>${x.days}</td><td>${x.trades}</td><td>${f(x.win_rate,1)}%</td>
    <td>${x.profit_exits}</td><td>${x.dollar_sl_exits||0}</td>
    <td class="${x.net_pnl>=0?'g':'r'}">$${f(x.net_pnl)}</td>
    <td>$${f(x.end_balance)}</td><td>${f(x.max_dd,1)}%</td>
    <td>${x.open_position||'-'}${x.open_position?' ('+(x.unrealized_pnl>=0?'+':'')+f(x.unrealized_pnl)+')':''}</td>
    </tr>`;
    (x.trades_detail||[]).forEach(t=>{
      trades.innerHTML+=`<tr>
      <td>${t.coin}</td><td>${t.signal_day}</td><td>${t.entry_day}</td><td>${t.exit_day}</td>
      <td>${f(t.entry,6)}</td><td>${f(t.exit,6)}</td><td>${f(t.signal_rsi,2)}</td><td>${f(t.exit_rsi,2)}</td>
      <td>${t.reason}</td><td class="${t.net>=0?'g':'r'}">$${f(t.net)}</td>
      </tr>`;
    });
   });
 }
}
async function run(){
 const d=Math.max(1,Math.min(180,parseInt(days.value||30)));
 msg.textContent='Starting...';
 await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({days:d})});
 setTimeout(load,1000);
}
load();setInterval(load,8000);
</script></body></html>"""

@app.get("/")
def home():
    return Response(HTML,mimetype="text/html")

if __name__=="__main__":
    app.run(host="0.0.0.0",port=8080)
