import os, time, json, threading, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, Response, request

app = Flask(__name__)
BASE = "https://www.okx.com"

START_BALANCE = 100.0
MARGIN_PER_TRADE = 100.0
LEVERAGE = 5.0
NOTIONAL = MARGIN_PER_TRADE * LEVERAGE
TP_PCT = 20.0
MAX_RISK_USD = 10.0
TOP_N = 25
MIN_DAILY_QUOTE_VOL = 1_000_000.0
DEFAULT_BACKTEST_DAYS = 180
MAX_BACKTEST_DAYS = 190

EXCLUDE = {
    "USDC-USDT","USDT-USDC","DAI-USDT","FDUSD-USDT","TUSD-USDT",
    "USDP-USDT","PYUSD-USDT","EURT-USDT"
}

state = {
    "position": None,
    "trades": [],
    "top25": [],
    "candidates": [],
    "last_scan": None,
    "last_error": None,
    "backtest": {"running": False, "progress": "", "result": None, "error": None, "last_run": None}
}
lock = threading.RLock()
_daily_cache = {}
_instruments_cache = {"ts": 0, "ids": []}


def jget(path, params=None, timeout=20):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 Top25VolumeBreakoutPaperBot/2.0",
        "Accept": "application/json"
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        obj = json.loads(r.read().decode("utf-8"))
    if obj.get("code") != "0":
        raise RuntimeError(obj.get("msg") or str(obj))
    return obj.get("data", [])


def fnum(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


def utc_day_ms(dt):
    d = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
    return int(d.timestamp() * 1000)


def get_instruments(force=False):
    now = time.time()
    if not force and _instruments_cache["ids"] and now - _instruments_cache["ts"] < 3600:
        return list(_instruments_cache["ids"])
    data = jget("/api/v5/public/instruments", {"instType": "SPOT"})
    ids = []
    for x in data:
        iid = x.get("instId", "")
        if not iid.endswith("-USDT"):
            continue
        if iid in EXCLUDE:
            continue
        if x.get("state") not in (None, "live"):
            continue
        ids.append(iid)
    _instruments_cache["ids"] = ids
    _instruments_cache["ts"] = now
    return list(ids)


def parse_bar(r):
    return {
        "ts": int(r[0]), "o": float(r[1]), "h": float(r[2]),
        "l": float(r[3]), "c": float(r[4]), "vol": float(r[5]),
        "vol_ccy": float(r[6]), "quote_vol": float(r[7]), "confirm": str(r[8])
    }


def fetch_daily_recent(inst_id, limit=10):
    data = jget("/api/v5/market/candles", {"instId": inst_id, "bar": "1Dutc", "limit": str(limit)})
    bars = [parse_bar(r) for r in data if len(r) >= 9]
    bars.sort(key=lambda x: x["ts"])
    return bars


def fetch_daily_history(inst_id, days=180):
    key = (inst_id, days)
    if key in _daily_cache:
        return _daily_cache[key]
    target_start = utc_day_ms(datetime.now(timezone.utc) - timedelta(days=days + 8))
    all_rows = {}
    after = None
    for _ in range(5):
        params = {"instId": inst_id, "bar": "1Dutc", "limit": "100"}
        if after is not None:
            params["after"] = str(after)
        data = jget("/api/v5/market/history-candles", params)
        if not data:
            break
        parsed = [parse_bar(r) for r in data if len(r) >= 9]
        for b in parsed:
            if b["confirm"] == "1":
                all_rows[b["ts"]] = b
        oldest = min(b["ts"] for b in parsed)
        if oldest <= target_start:
            break
        after = oldest
        time.sleep(0.05)
    bars = sorted(all_rows.values(), key=lambda x: x["ts"])
    bars = [b for b in bars if b["ts"] >= target_start]
    _daily_cache[key] = bars
    return bars


def ticker_map():
    data = jget("/api/v5/market/tickers", {"instType": "SPOT"})
    return {x.get("instId"): x for x in data}


def build_live_top25():
    recs = []
    ids = get_instruments()
    for i, iid in enumerate(ids):
        try:
            comp = [b for b in fetch_daily_recent(iid, 6) if b["confirm"] == "1"]
            if len(comp) < 3:
                continue
            prior, prev = comp[-2], comp[-1]
            if prior["quote_vol"] <= 0 or prev["quote_vol"] < MIN_DAILY_QUOTE_VOL:
                continue
            upside = (prev["quote_vol"] / prior["quote_vol"] - 1.0) * 100.0
            recs.append({
                "instId": iid,
                "volume_upside_pct": upside,
                "prev_quote_volume": prev["quote_vol"],
                "prior_quote_volume": prior["quote_vol"]
            })
        except Exception:
            continue
        if i % 15 == 14:
            time.sleep(0.12)
    recs.sort(key=lambda x: x["volume_upside_pct"], reverse=True)
    top = recs[:TOP_N]
    for i, x in enumerate(top, 1):
        x["rank"] = i
    return top


def build_live_candidates(top25):
    tmap = ticker_map()
    out = []
    for x in top25:
        iid = x["instId"]
        try:
            comp = [b for b in fetch_daily_recent(iid, 6) if b["confirm"] == "1"]
            if len(comp) < 3:
                continue
            d0, d1, d2 = comp[-3], comp[-2], comp[-1]
            # Start-of-run rule: RED -> Green #1 -> Green #2 only.
            # Later G-G pairs inside a longer green streak are invalid.
            if not (d0["c"] < d0["o"] and d1["c"] > d1["o"] and d2["c"] > d2["o"]):
                continue
            tk = tmap.get(iid, {})
            last = fnum(tk.get("last"))
            ask = fnum(tk.get("askPx"), last)
            out.append({
                **x,
                "d1_high": d1["h"], "d2_high": d2["h"], "d2_low": d2["l"],
                "d2_close": d2["c"], "last": last, "ask": ask,
                "breakout": last > d2["h"]
            })
        except Exception:
            continue
    out.sort(key=lambda x: x["rank"])
    return out


def enter_live_if_needed(cands):
    if state["position"] is not None:
        return
    eligible = [x for x in cands if x["breakout"]]
    if not eligible:
        return
    pick = eligible[0]
    entry = pick["ask"] or pick["last"]
    if entry <= 0:
        return
    sl = pick["d2_low"]
    risk_per_coin = entry - sl
    if risk_per_coin <= 0:
        return
    # Size by max $10 structural risk, but never exceed $500 notional (100 x 5x).
    risk_qty = MAX_RISK_USD / risk_per_coin
    max_qty = NOTIONAL / entry
    qty = min(risk_qty, max_qty)
    actual_notional = qty * entry
    planned_risk = qty * risk_per_coin
    state["position"] = {
        "instId": pick["instId"], "rank": pick["rank"],
        "volume_upside_pct": pick["volume_upside_pct"],
        "entry": entry, "qty": qty, "notional": actual_notional,
        "margin": MARGIN_PER_TRADE, "leverage": LEVERAGE,
        "sl": sl, "planned_risk_usd": planned_risk,
        "tp": entry * (1 + TP_PCT / 100.0),
        "entry_utc": datetime.now(timezone.utc).isoformat(),
        "entry_ts": int(time.time() * 1000)
    }


def update_live_position():
    p = state["position"]
    if not p:
        return
    tmap = ticker_map()
    tk = tmap.get(p["instId"], {})
    last = fnum(tk.get("last"))
    bid = fnum(tk.get("bidPx"), last)
    if last <= 0:
        return
    now = datetime.now(timezone.utc)
    entry_day = datetime.fromisoformat(p["entry_utc"]).date()
    exit_reason = None
    exit_px = None
    if last <= p["sl"]:
        exit_reason = "SL_D2_LOW"
        exit_px = bid if bid > 0 else last
    elif last >= p["tp"]:
        exit_reason = "TP20"
        exit_px = bid if bid > 0 else last
    elif now.date() > entry_day:
        exit_reason = "DAY_END"
        exit_px = bid if bid > 0 else last
    if exit_reason:
        pnl = p["qty"] * (exit_px - p["entry"])
        trade = dict(p)
        trade.update({
            "exit": exit_px, "exit_reason": exit_reason,
            "exit_utc": now.isoformat(), "pnl_usd_gross": pnl,
            "return_on_margin_pct_gross": pnl / MARGIN_PER_TRADE * 100.0
        })
        state["trades"].append(trade)
        state["trades"] = state["trades"][-200:]
        state["position"] = None


def live_scan_once():
    try:
        with lock:
            update_live_position()
            if state["position"] is None:
                top = build_live_top25()
                cands = build_live_candidates(top)
                state["top25"] = top
                state["candidates"] = cands
                enter_live_if_needed(cands)
            state["last_scan"] = datetime.now(timezone.utc).isoformat()
            state["last_error"] = None
    except Exception as e:
        state["last_error"] = repr(e)
        state["last_scan"] = datetime.now(timezone.utc).isoformat()


def live_loop():
    while True:
        live_scan_once()
        time.sleep(60)


def do_backtest(days):
    with lock:
        state["backtest"].update({"running": True, "progress": "Starting...", "error": None, "result": None})
    try:
        ids = get_instruments()
        hist = {}
        total = len(ids)
        for idx, iid in enumerate(ids, 1):
            with lock:
                state["backtest"]["progress"] = f"Downloading {idx}/{total}: {iid}"
            try:
                bars = fetch_daily_history(iid, days)
                if len(bars) >= 5:
                    hist[iid] = {b["ts"]: b for b in bars}
            except Exception:
                pass
            if idx % 10 == 0:
                time.sleep(0.15)

        all_dates = sorted({ts for m in hist.values() for ts in m.keys()})
        cutoff = utc_day_ms(datetime.now(timezone.utc) - timedelta(days=days))
        all_dates = [ts for ts in all_dates if ts >= cutoff]

        trades = []
        balance = START_BALANCE

        for di in range(3, len(all_dates)):
            d3_ts = all_dates[di]
            d2_ts = all_dates[di - 1]
            d1_ts = all_dates[di - 2]
            d0_ts = all_dates[di - 3]

            ranking = []
            for iid, mp in hist.items():
                if not all(x in mp for x in (d0_ts, d1_ts, d2_ts, d3_ts)):
                    continue
                d0, d1, d2, d3 = mp[d0_ts], mp[d1_ts], mp[d2_ts], mp[d3_ts]
                if d1["quote_vol"] <= 0 or d2["quote_vol"] < MIN_DAILY_QUOTE_VOL:
                    continue
                upside = (d2["quote_vol"] / d1["quote_vol"] - 1.0) * 100.0
                ranking.append((upside, iid, d0, d1, d2, d3))

            ranking.sort(key=lambda z: z[0], reverse=True)
            top25 = ranking[:TOP_N]
            candidates = []

            for rank, item in enumerate(top25, 1):
                upside, iid, d0, d1, d2, d3 = item
                # Only the first two greens of a new green run are valid:
                # D0 red -> D1 green #1 -> D2 green #2.
                setup = d0["c"] < d0["o"] and d1["c"] > d1["o"] and d2["c"] > d2["o"]
                if not setup or d3["h"] <= d2["h"]:
                    continue
                entry = max(d2["h"], d3["o"])
                sl = d2["l"]
                risk_per_coin = entry - sl
                if risk_per_coin <= 0:
                    continue
                risk_qty = MAX_RISK_USD / risk_per_coin
                max_qty = NOTIONAL / entry
                qty = min(risk_qty, max_qty)
                actual_notional = qty * entry
                planned_risk = qty * risk_per_coin

                tp = entry * (1 + TP_PCT / 100.0)
                # Daily OHLC cannot prove whether SL or TP happened first after breakout.
                # Use conservative SL-first assumption when both levels are inside D3.
                sl_hit = d3["l"] <= sl
                tp_hit = d3["h"] >= tp
                if sl_hit:
                    exit_px = sl
                    exit_reason = "SL_D2_LOW"
                elif tp_hit:
                    exit_px = tp
                    exit_reason = "TP20"
                else:
                    exit_px = d3["c"]
                    exit_reason = "DAY_END"
                pnl = qty * (exit_px - entry)
                candidates.append({
                    "date": datetime.fromtimestamp(d3_ts / 1000, tz=timezone.utc).date().isoformat(),
                    "coin": iid, "rank": rank, "volume_upside_pct": upside,
                    "entry": entry, "sl": sl, "qty": qty,
                    "notional": actual_notional, "planned_risk_usd": planned_risk,
                    "exit": exit_px, "exit_reason": exit_reason,
                    "pnl_usd": pnl,
                    "return_on_margin_pct": pnl / MARGIN_PER_TRADE * 100.0
                })

            if not candidates:
                continue
            pick = sorted(candidates, key=lambda x: x["rank"])[0]
            balance += pick["pnl_usd"]
            pick["balance_after"] = balance
            trades.append(pick)

        months = {}
        for t in trades:
            m = t["date"][:7]
            rec = months.setdefault(m, {"month": m, "trades": 0, "wins": 0, "losses": 0, "pnl_usd": 0.0})
            rec["trades"] += 1
            rec["pnl_usd"] += t["pnl_usd"]
            if t["pnl_usd"] > 0:
                rec["wins"] += 1
            elif t["pnl_usd"] < 0:
                rec["losses"] += 1

        month_rows = []
        running = START_BALANCE
        for m in sorted(months):
            rec = months[m]
            running += rec["pnl_usd"]
            rec["ending_balance"] = running
            month_rows.append(rec)

        wins = sum(1 for t in trades if t["pnl_usd"] > 0)
        losses = sum(1 for t in trades if t["pnl_usd"] < 0)
        tp_hits = sum(1 for t in trades if t["exit_reason"] == "TP20")
        pnl_total = sum(t["pnl_usd"] for t in trades)

        result = {
            "days_requested": days,
            "starting_balance": START_BALANCE,
            "fixed_margin_per_trade": MARGIN_PER_TRADE,
            "leverage": LEVERAGE,
            "max_notional_per_trade": NOTIONAL,
            "max_risk_usd": MAX_RISK_USD,
            "tp_pct": TP_PCT,
            "total_trades": len(trades),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": (wins / len(trades) * 100.0) if trades else 0.0,
            "tp20_hits": tp_hits,
            "net_pnl_usd_gross": pnl_total,
            "ending_balance_gross": START_BALANCE + pnl_total,
            "months": month_rows,
            "trades": trades,
            "note": "Historical backtest uses completed OKX 1D UTC candles. Fixed $500 notional/trade. Fees/slippage not deducted yet."
        }
        with lock:
            state["backtest"]["result"] = result
            state["backtest"]["progress"] = "Complete"
            state["backtest"]["last_run"] = datetime.now(timezone.utc).isoformat()
    except Exception as e:
        with lock:
            state["backtest"]["error"] = repr(e)
            state["backtest"]["progress"] = "Failed"
    finally:
        with lock:
            state["backtest"]["running"] = False


@app.get("/api/status")
def api_status():
    with lock:
        return jsonify({
            "paper_only": True,
            "rules": {
                "starting_balance": START_BALANCE,
                "margin_per_trade": MARGIN_PER_TRADE,
                "leverage": LEVERAGE,
                "max_notional_per_trade": NOTIONAL,
                "max_risk_usd": MAX_RISK_USD,
                "tp_pct": TP_PCT,
                "top_n": TOP_N,
                "max_open_positions": 1
            },
            **state
        })


@app.get("/api/scan-now")
def api_scan():
    live_scan_once()
    return api_status()


@app.post("/api/backtest")
def api_backtest():
    try:
        days = int((request.get_json(silent=True) or {}).get("days", DEFAULT_BACKTEST_DAYS))
    except Exception:
        days = DEFAULT_BACKTEST_DAYS
    days = max(30, min(days, MAX_BACKTEST_DAYS))
    with lock:
        if state["backtest"]["running"]:
            return jsonify({"ok": False, "message": "Backtest already running"}), 409
        threading.Thread(target=do_backtest, args=(days,), daemon=True).start()
    return jsonify({"ok": True, "message": "Backtest started", "days": days})


HTML = '''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Top-25 Volume Breakout Bot</title>
<style>
body{margin:0;background:#071019;color:#eef6ff;font-family:Arial;padding:14px}.w{max-width:1200px;margin:auto}.c{background:#111d29;border:1px solid #27394b;border-radius:14px;padding:14px;margin-bottom:12px}h2,h3{margin-top:0}.sub{color:#a8bacb;font-size:13px;line-height:1.55}.grid{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}.k{background:#09141e;padding:10px;border-radius:9px}.v{font-size:20px;font-weight:bold}.g{color:#6ff0a0}.r{color:#ff9999}.y{color:#ffd479}.tabs{display:flex;gap:8px;margin-bottom:12px}.tab{padding:10px 14px;border-radius:9px;background:#142536;cursor:pointer}.tab.on{background:#387df3}.pane{display:none}.pane.on{display:block}table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:8px;border-bottom:1px solid #253645;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}.scroll{overflow:auto}button{padding:10px 14px;border:0;border-radius:8px;background:#387df3;color:#fff;font-weight:bold;cursor:pointer}input{background:#09141e;color:#fff;border:1px solid #33485a;border-radius:8px;padding:9px;width:90px}@media(max-width:700px){.grid{grid-template-columns:1fr 1fr}}
</style></head><body><div class="w">
<div class="c"><h2>Top-25 Volume Upside + 3-Candle Breakout</h2><div class="sub">PAPER ONLY — max $100 margin × 5x = $500 notional. Qty D3 entry se D2 Low tak distance par size hoti hai taa-ke planned SL max $10 ho. Previous completed day ke Volume Upside ranking se Top-25 coins. D0 Red ke baad Green #1 + Green #2. D3 par Green #2 ka High break ho to signal. Long green streak ke beech ka G-G pair valid nahi. Multiple signals mein highest-ranked coin trade hota hai. TP +20%, warna day-end exit.</div></div>
<div class="c grid"><div class="k"><div class="sub">Start Balance</div><div class="v">$100</div></div><div class="k"><div class="sub">Margin</div><div class="v">$100</div></div><div class="k"><div class="sub">Leverage</div><div class="v">5x</div></div><div class="k"><div class="sub">Max Notional</div><div class="v">$500</div></div><div class="k"><div class="sub">Max SL Risk</div><div class="v">$10</div></div><div class="k"><div class="sub">TP</div><div class="v">20%</div></div></div>
<div class="tabs"><div id="tLive" class="tab on" onclick="showTab('live')">LIVE PAPER</div><div id="tBt" class="tab" onclick="showTab('bt')">BACKTEST</div></div>
<div id="live" class="pane on"><div class="c"><button onclick="scanNow()">Scan Now</button> <span id="liveMsg" class="sub"></span><div id="pos" style="margin-top:12px"></div></div>
<div class="c scroll"><h3>Valid Setup Candidates</h3><table><thead><tr><th>Coin</th><th>Vol Rank</th><th>Vol Upside</th><th>D2 High</th><th>Last</th><th>Breakout</th></tr></thead><tbody id="cb"></tbody></table></div>
<div class="c scroll"><h3>Previous-Day Top 25</h3><table><thead><tr><th>Coin</th><th>Rank</th><th>Vol Upside</th><th>Quote Volume</th></tr></thead><tbody id="tb"></tbody></table></div>
<div class="c scroll"><h3>Live Paper Trades</h3><table><thead><tr><th>Coin</th><th>Rank</th><th>Entry</th><th>SL</th><th>Qty</th><th>Risk $</th><th>Exit</th><th>Reason</th><th>P/L $</th></tr></thead><tbody id="trb"></tbody></table></div></div>
<div id="bt" class="pane"><div class="c"><h3>Historical Backtest</h3><div class="sub">Recommended 180 days. First run thora time le sakta hai.</div><br>Days: <input id="days" type="number" value="180" min="30" max="190"> <button onclick="startBacktest()">Run Backtest</button><div id="btMsg" class="sub" style="margin-top:10px"></div></div>
<div id="btSummary" class="c grid"></div><div class="c scroll"><h3>Month-wise Result</h3><table><thead><tr><th>Month</th><th>Trades</th><th>Wins</th><th>Losses</th><th>P/L $</th><th>End Balance</th></tr></thead><tbody id="mb"></tbody></table></div>
<div class="c scroll"><h3>Backtest Trades</h3><table><thead><tr><th>Date</th><th>Coin</th><th>Rank</th><th>Vol Upside</th><th>Entry</th><th>SL</th><th>Qty</th><th>Risk $</th><th>Exit</th><th>Reason</th><th>P/L $</th><th>Balance</th></tr></thead><tbody id="btb"></tbody></table></div></div>
</div><script>
const f=(x,n=4)=>Number(x||0).toFixed(n);function showTab(x){live.className='pane'+(x==='live'?' on':'');bt.className='pane'+(x==='bt'?' on':'');tLive.className='tab'+(x==='live'?' on':'');tBt.className='tab'+(x==='bt'?' on':'');}
async function load(){try{let j=await(await fetch('/api/status',{cache:'no-store'})).json();liveMsg.textContent='Last scan: '+(j.last_scan||'—')+(j.last_error?' | '+j.last_error:'');if(j.position){let p=j.position;pos.innerHTML=`<b class=g>OPEN:</b> ${p.instId} | Rank #${p.rank} | Entry ${f(p.entry,6)} | SL ${f(p.sl,6)} | Qty ${f(p.qty,4)} | Risk $${f(p.planned_risk_usd,2)} | Notional $${f(p.notional,2)} | TP ${f(p.tp,6)}`}else pos.innerHTML='<span class=y>No open trade</span>';cb.innerHTML='';(j.candidates||[]).forEach(x=>cb.innerHTML+=`<tr><td>${x.instId}</td><td>#${x.rank}</td><td>${f(x.volume_upside_pct,2)}%</td><td>${f(x.d2_high,6)}</td><td>${f(x.last,6)}</td><td class="${x.breakout?'g':'r'}">${x.breakout?'YES':'NO'}</td></tr>`);tb.innerHTML='';(j.top25||[]).forEach(x=>tb.innerHTML+=`<tr><td>${x.instId}</td><td>#${x.rank}</td><td>${f(x.volume_upside_pct,2)}%</td><td>${f(x.prev_quote_volume,0)}</td></tr>`);trb.innerHTML='';[...(j.trades||[])].reverse().forEach(x=>trb.innerHTML+=`<tr><td>${x.instId}</td><td>#${x.rank}</td><td>${f(x.entry,6)}</td><td>${f(x.exit,6)}</td><td>${x.exit_reason}</td><td class="${x.pnl_usd_gross>=0?'g':'r'}">${f(x.pnl_usd_gross,2)}</td></tr>`);let b=j.backtest||{};btMsg.textContent=(b.running?'Running: ':'')+(b.progress||'')+(b.error?' | '+b.error:'');if(b.result)renderBacktest(b.result);}catch(e){}}
async function scanNow(){liveMsg.textContent='Scanning...';await fetch('/api/scan-now');await load();}async function startBacktest(){let d=parseInt(days.value||180);btMsg.textContent='Starting...';await fetch('/api/backtest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({days:d})});showTab('bt');setTimeout(load,1000);}
function renderBacktest(r){btSummary.innerHTML=`<div class=k><div class=sub>Total Trades</div><div class=v>${r.total_trades}</div></div><div class=k><div class=sub>Win Rate</div><div class=v>${f(r.win_rate_pct,1)}%</div></div><div class=k><div class=sub>TP20 Hits</div><div class=v>${r.tp20_hits}</div></div><div class=k><div class=sub>Net P/L</div><div class="v ${r.net_pnl_usd_gross>=0?'g':'r'}">$${f(r.net_pnl_usd_gross,2)}</div></div><div class=k><div class=sub>End Balance</div><div class=v>$${f(r.ending_balance_gross,2)}</div></div>`;mb.innerHTML='';(r.months||[]).forEach(x=>mb.innerHTML+=`<tr><td>${x.month}</td><td>${x.trades}</td><td>${x.wins}</td><td>${x.losses}</td><td class="${x.pnl_usd>=0?'g':'r'}">${f(x.pnl_usd,2)}</td><td>${f(x.ending_balance,2)}</td></tr>`);btb.innerHTML='';[...(r.trades||[])].reverse().forEach(x=>btb.innerHTML+=`<tr><td>${x.date}</td><td>${x.coin}</td><td>#${x.rank}</td><td>${f(x.volume_upside_pct,2)}%</td><td>${f(x.entry,6)}</td><td>${f(x.sl,6)}</td><td>${f(x.qty,4)}</td><td>${f(x.planned_risk_usd,2)}</td><td>${f(x.exit,6)}</td><td>${x.exit_reason}</td><td class="${x.pnl_usd>=0?'g':'r'}">${f(x.pnl_usd,2)}</td><td>${f(x.balance_after,2)}</td></tr>`);}
load();setInterval(load,15000);
</script></body></html>'''

@app.get("/")
def home():
    return Response(HTML, mimetype="text/html")

if __name__ == "__main__":
    threading.Thread(target=live_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True)
