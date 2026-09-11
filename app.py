
from flask import Flask, request, jsonify, render_template_string
import sqlite3, os, math, json
from datetime import datetime

APP_TITLE = "PSX All-Symbols 1m Paper Bot V1"
DB_PATH = os.environ.get("PSX_BOT_DB", os.environ.get("DATABASE_PATH", "psx_paper_bot.db"))
WEBHOOK_SECRET = os.environ.get("PSX_WEBHOOK_SECRET", "change-me")

CFG = {
    "starting_capital": 100000.0,
    "position_size_pct": 0.10,
    "ema_fast": 9,
    "ema_slow": 20,
    "volume_lookback": 20,
    "volume_multiplier": 1.20,
    "take_profit_pct": 0.0030,
    "stop_loss_pct": 0.0020,
    "max_hold_bars": 10,
    "min_price": 5.0,
    "min_bar_volume": 1000.0,
    "commission_pct_per_side": 0.0015,
    "slippage_pct_per_side": 0.0005,
    "max_open_positions": 50
}

app = Flask(__name__)

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    cur = con.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS candles(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      symbol TEXT NOT NULL,
      ts TEXT NOT NULL,
      open REAL NOT NULL,
      high REAL NOT NULL,
      low REAL NOT NULL,
      close REAL NOT NULL,
      volume REAL NOT NULL,
      UNIQUE(symbol, ts)
    );
    CREATE INDEX IF NOT EXISTS idx_candles_symbol_ts ON candles(symbol, ts);

    CREATE TABLE IF NOT EXISTS positions(
      symbol TEXT PRIMARY KEY,
      entry_time TEXT NOT NULL,
      entry_price REAL NOT NULL,
      qty INTEGER NOT NULL,
      allocated REAL NOT NULL,
      entry_fee REAL NOT NULL,
      tp REAL NOT NULL,
      sl REAL NOT NULL,
      bars_held INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS trades(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      symbol TEXT NOT NULL,
      entry_time TEXT NOT NULL,
      exit_time TEXT NOT NULL,
      entry_price REAL NOT NULL,
      exit_price REAL NOT NULL,
      qty INTEGER NOT NULL,
      exit_reason TEXT NOT NULL,
      gross_pl REAL NOT NULL,
      total_costs REAL NOT NULL,
      net_pl REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS state(
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    );
    """)
    cur.execute("INSERT OR IGNORE INTO state(key,value) VALUES('cash',?)", (str(CFG["starting_capital"]),))
    con.commit()
    con.close()

def get_cash(con):
    row = con.execute("SELECT value FROM state WHERE key='cash'").fetchone()
    return float(row["value"]) if row else CFG["starting_capital"]

def set_cash(con, value):
    con.execute("INSERT OR REPLACE INTO state(key,value) VALUES('cash',?)", (str(float(value)),))

def ema(values, length):
    if not values:
        return None
    alpha = 2.0 / (length + 1.0)
    e = values[0]
    for v in values[1:]:
        e = alpha * v + (1-alpha) * e
    return e

def current_signal(con, symbol):
    rows = con.execute(
        "SELECT ts,close,volume FROM candles WHERE symbol=? ORDER BY ts DESC LIMIT 60",
        (symbol,)
    ).fetchall()
    rows = list(reversed(rows))
    if len(rows) < max(CFG["ema_slow"], CFG["volume_lookback"]) + 2:
        return False, {}

    closes = [float(r["close"]) for r in rows]
    vols = [float(r["volume"]) for r in rows]
    last = rows[-1]
    close = closes[-1]
    vol = vols[-1]
    ef = ema(closes[-60:], CFG["ema_fast"])
    es = ema(closes[-60:], CFG["ema_slow"])
    prior_vols = vols[-CFG["volume_lookback"]-1:-1]
    vol_avg = sum(prior_vols) / len(prior_vols)

    # Session VWAP reconstructed from today's candles for this symbol
    day = str(last["ts"])[:10]
    day_rows = con.execute(
        "SELECT close,volume FROM candles WHERE symbol=? AND substr(ts,1,10)=? ORDER BY ts",
        (symbol, day)
    ).fetchall()
    pv = sum(float(r["close"]) * float(r["volume"]) for r in day_rows)
    vv = sum(float(r["volume"]) for r in day_rows)
    vwap = pv / vv if vv else close

    signal = (
        ef > es and
        close > vwap and
        close > ef and
        vol >= vol_avg * CFG["volume_multiplier"] and
        close >= CFG["min_price"] and
        vol >= CFG["min_bar_volume"]
    )
    return signal, {
        "ema_fast": round(ef,4), "ema_slow": round(es,4),
        "vwap": round(vwap,4), "vol_avg": round(vol_avg,2)
    }

def process_candle(con, c):
    symbol = c["symbol"].upper().strip()
    ts = c["datetime"]
    o,h,l,cl,v = map(float, [c["open"],c["high"],c["low"],c["close"],c["volume"]])

    con.execute("""
      INSERT OR IGNORE INTO candles(symbol,ts,open,high,low,close,volume)
      VALUES(?,?,?,?,?,?,?)
    """, (symbol,ts,o,h,l,cl,v))

    action = None
    pos = con.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()

    # Manage exit on every new candle
    if pos:
        bars = int(pos["bars_held"]) + 1
        exit_reason, raw_exit = None, None
        if l <= float(pos["sl"]):
            exit_reason, raw_exit = "SL", float(pos["sl"])
        elif h >= float(pos["tp"]):
            exit_reason, raw_exit = "TP", float(pos["tp"])
        elif bars >= CFG["max_hold_bars"]:
            exit_reason, raw_exit = "TIME", cl

        if exit_reason:
            exit_price = raw_exit * (1 - CFG["slippage_pct_per_side"])
            qty = int(pos["qty"])
            gross = (exit_price - float(pos["entry_price"])) * qty
            exit_fee = exit_price * qty * CFG["commission_pct_per_side"]
            entry_fee = float(pos["entry_fee"])
            net = gross - entry_fee - exit_fee
            cash = get_cash(con)
            cash += float(pos["allocated"]) + gross - exit_fee
            set_cash(con, cash)
            con.execute("""
              INSERT INTO trades(symbol,entry_time,exit_time,entry_price,exit_price,qty,exit_reason,gross_pl,total_costs,net_pl)
              VALUES(?,?,?,?,?,?,?,?,?,?)
            """, (symbol,pos["entry_time"],ts,float(pos["entry_price"]),exit_price,qty,exit_reason,gross,entry_fee+exit_fee,net))
            con.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
            action = {"type":"EXIT","symbol":symbol,"reason":exit_reason,"net_pl":round(net,2)}
            pos = None
        else:
            con.execute("UPDATE positions SET bars_held=? WHERE symbol=?", (bars,symbol))

    # Entry after exit management
    if not pos:
        open_count = con.execute("SELECT COUNT(*) AS n FROM positions").fetchone()["n"]
        if open_count < CFG["max_open_positions"]:
            signal, ind = current_signal(con, symbol)
            if signal:
                cash = get_cash(con)
                budget = cash * CFG["position_size_pct"]
                entry_price = cl * (1 + CFG["slippage_pct_per_side"])
                qty = int(budget // entry_price)
                if qty > 0:
                    allocated = qty * entry_price
                    entry_fee = allocated * CFG["commission_pct_per_side"]
                    debit = allocated + entry_fee
                    if debit <= cash:
                        set_cash(con, cash - debit)
                        tp = entry_price * (1 + CFG["take_profit_pct"])
                        sl = entry_price * (1 - CFG["stop_loss_pct"])
                        con.execute("""
                          INSERT OR REPLACE INTO positions(symbol,entry_time,entry_price,qty,allocated,entry_fee,tp,sl,bars_held)
                          VALUES(?,?,?,?,?,?,?,?,0)
                        """, (symbol,ts,entry_price,qty,allocated,entry_fee,tp,sl))
                        action = {"type":"ENTRY","symbol":symbol,"entry":round(entry_price,4),"qty":qty,"tp":round(tp,4),"sl":round(sl,4), **ind}
    return action

def summary(con, day=None):
    if day is None:
        day = datetime.now().strftime("%Y-%m-%d")
    tr = con.execute("SELECT * FROM trades WHERE substr(exit_time,1,10)=? ORDER BY exit_time DESC", (day,)).fetchall()
    total = len(tr)
    wins = sum(1 for r in tr if float(r["net_pl"]) > 0)
    losses = total - wins
    net = sum(float(r["net_pl"]) for r in tr)
    gross = sum(float(r["gross_pl"]) for r in tr)
    costs = sum(float(r["total_costs"]) for r in tr)
    open_positions = con.execute("SELECT COUNT(*) AS n FROM positions").fetchone()["n"]
    cash = get_cash(con)
    return {
        "date": day, "total_trades": total, "wins": wins, "losses": losses,
        "win_rate": round((wins/total*100) if total else 0,2),
        "gross_pl": round(gross,2), "costs": round(costs,2), "net_pl": round(net,2),
        "cash": round(cash,2), "open_positions": open_positions
    }

init_db()

@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_json(force=True, silent=False)
    if WEBHOOK_SECRET != "change-me":
        supplied = request.headers.get("X-Webhook-Secret") or payload.get("secret")
        if supplied != WEBHOOK_SECRET:
            return jsonify({"ok":False,"error":"bad secret"}), 403

    required = ["symbol","datetime","open","high","low","close","volume"]
    missing = [x for x in required if x not in payload]
    if missing:
        return jsonify({"ok":False,"error":"missing fields","missing":missing}), 400

    con = db()
    try:
        action = process_candle(con, payload)
        con.commit()
        s = summary(con, str(payload["datetime"])[:10])
        return jsonify({"ok":True,"action":action,"summary":s})
    finally:
        con.close()

@app.route("/summary")
def summary_api():
    day = request.args.get("date")
    con = db()
    try:
        return jsonify(summary(con, day))
    finally:
        con.close()

@app.route("/trades")
def trades_api():
    day = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
    con = db()
    try:
        rows = con.execute("SELECT * FROM trades WHERE substr(exit_time,1,10)=? ORDER BY exit_time DESC", (day,)).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        con.close()

@app.route("/reset_demo", methods=["POST"])
def reset_demo():
    con = db()
    try:
        con.execute("DELETE FROM candles")
        con.execute("DELETE FROM positions")
        con.execute("DELETE FROM trades")
        set_cash(con, CFG["starting_capital"])
        con.commit()
        return jsonify({"ok":True})
    finally:
        con.close()

DASH = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="20">
<title>{{title}}</title>
<style>
body{font-family:Arial;background:#101317;color:#eee;margin:24px}
h1{font-size:24px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{background:#1a2027;padding:16px;border-radius:12px}.big{font-size:26px;font-weight:700;margin-top:8px}
table{width:100%;border-collapse:collapse;margin-top:20px;background:#1a2027}
th,td{padding:10px;border-bottom:1px solid #2c333b;text-align:right}th:first-child,td:first-child{text-align:left}
.pos{color:#76e39b}.neg{color:#ff8585}.muted{color:#9da7b1}
</style>
</head>
<body>
<h1>PSX All-Symbols 1m Paper Bot V1</h1>
<div class="muted">Auto refresh: 20 sec | Demo only | Date: {{s.date}}</div>
<div class="grid">
 <div class="card">Trades<div class="big">{{s.total_trades}}</div></div>
 <div class="card">Wins<div class="big">{{s.wins}}</div></div>
 <div class="card">Losses<div class="big">{{s.losses}}</div></div>
 <div class="card">Win Rate<div class="big">{{s.win_rate}}%</div></div>
 <div class="card">Net P/L<div class="big {{'pos' if s.net_pl>=0 else 'neg'}}">Rs {{s.net_pl}}</div></div>
 <div class="card">Costs<div class="big">Rs {{s.costs}}</div></div>
 <div class="card">Cash<div class="big">Rs {{s.cash}}</div></div>
 <div class="card">Open Positions<div class="big">{{s.open_positions}}</div></div>
</div>

<h2>Latest Trades</h2>
<table>
<tr><th>Symbol</th><th>Entry</th><th>Exit</th><th>Qty</th><th>Reason</th><th>Net P/L</th></tr>
{% for r in trades %}
<tr>
<td>{{r.symbol}}</td><td>{{"%.2f"|format(r.entry_price)}}</td><td>{{"%.2f"|format(r.exit_price)}}</td>
<td>{{r.qty}}</td><td>{{r.exit_reason}}</td>
<td class="{{'pos' if r.net_pl>=0 else 'neg'}}">{{"%.2f"|format(r.net_pl)}}</td>
</tr>
{% endfor %}
</table>
</body>
</html>
"""

@app.route("/")
def dashboard():
    day = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
    con = db()
    try:
        s = summary(con, day)
        trades = con.execute("SELECT * FROM trades WHERE substr(exit_time,1,10)=? ORDER BY exit_time DESC LIMIT 100", (day,)).fetchall()
        return render_template_string(DASH, title=APP_TITLE, s=s, trades=trades)
    finally:
        con.close()

@app.route("/health")
def health():
    return jsonify({"ok": True, "service": APP_TITLE})

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "5000"))
    print(f"Open dashboard on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
