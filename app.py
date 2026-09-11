import os, json, sqlite3, threading, time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from flask import Flask, jsonify, redirect, render_template_string, request, url_for

app = Flask(__name__)
DB_PATH = os.getenv('PSX_BOT_DB', os.getenv('DATABASE_PATH', '/tmp/crypto_paper_bot.db'))
SYMBOLS = ['ADAUSDT','XRPUSDT','DOGEUSDT','LINKUSDT','AVAXUSDT','DOTUSDT','LTCUSDT','BCHUSDT','ATOMUSDT','NEARUSDT','FILUSDT','APTUSDT','ARBUSDT','OPUSDT','INJUSDT','SUIUSDT','SEIUSDT','TIAUSDT','JTOUSDT','ETCUSDT']
COINBASE_SYMBOL_MAP = {
    "ADAUSDT":"ADA-USD","XRPUSDT":"XRP-USD","DOGEUSDT":"DOGE-USD",
    "LINKUSDT":"LINK-USD","AVAXUSDT":"AVAX-USD","DOTUSDT":"DOT-USD",
    "LTCUSDT":"LTC-USD","BCHUSDT":"BCH-USD","ATOMUSDT":"ATOM-USD",
    "NEARUSDT":"NEAR-USD","FILUSDT":"FIL-USD","APTUSDT":"APT-USD",
    "ARBUSDT":"ARB-USD","OPUSDT":"OP-USD","INJUSDT":"INJ-USD",
    "SUIUSDT":"SUI-USD","SEIUSDT":"SEI-USD","TIAUSDT":"TIA-USD",
    "JTOUSDT":"JTO-USD","ETCUSDT":"ETC-USD"
}
COINBASE_CANDLES_URL="https://api.exchange.coinbase.com/products/{product_id}/candles"
STARTING_CAPITAL_RS=100.0
STATE_VERSION="coinbase-usd-paper-backtest-v7"
POSITION_PCT=0.10
MAX_OPEN_POSITIONS=20
EMA_FAST=9; EMA_SLOW=20; VOLUME_LOOKBACK=20; VOLUME_MULTIPLIER=1.20
TAKE_PROFIT_PCT=0.0030; STOP_LOSS_PCT=0.0020; MAX_HOLD_BARS=10
COMMISSION_PCT_PER_SIDE=0.0015; SLIPPAGE_PCT_PER_SIDE=0.0005
POLL_SECONDS=20
_db_lock=threading.Lock(); _worker_started=False; _worker_lock=threading.Lock(); _backtest_lock=threading.Lock(); _backtest_running=False

def db():
    c=sqlite3.connect(DB_PATH,timeout=30); c.row_factory=sqlite3.Row; return c

def get_state(key, default=None):
    c=db(); r=c.execute("SELECT value FROM state WHERE key=?",(key,)).fetchone(); c.close()
    return r['value'] if r else default

def set_state(key, value):
    with _db_lock:
        c=db(); c.execute("INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,str(value))); c.commit(); c.close()


def init_db():
    with _db_lock:
        c=db(); c.executescript('''
        CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS processed_bars(symbol TEXT NOT NULL, close_time_ms INTEGER NOT NULL, PRIMARY KEY(symbol,close_time_ms));
        CREATE TABLE IF NOT EXISTS positions(symbol TEXT PRIMARY KEY, entry_time TEXT NOT NULL, entry_close_time_ms INTEGER NOT NULL, raw_entry_price REAL NOT NULL, entry_price REAL NOT NULL, notional_rs REAL NOT NULL, bars_held INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, entry_time TEXT NOT NULL, exit_time TEXT NOT NULL, raw_entry_price REAL NOT NULL, entry_price REAL NOT NULL, raw_exit_price REAL NOT NULL, exit_price REAL NOT NULL, notional_rs REAL NOT NULL, gross_pl_rs REAL NOT NULL, commission_rs REAL NOT NULL, net_pl_rs REAL NOT NULL, return_pct REAL NOT NULL, reason TEXT NOT NULL);
        ''')
        ver=c.execute("SELECT value FROM state WHERE key='state_version'").fetchone()
        if ver is None or ver['value'] != STATE_VERSION:
            c.execute('DELETE FROM trades'); c.execute('DELETE FROM positions'); c.execute('DELETE FROM processed_bars')
            c.execute("INSERT INTO state(key,value) VALUES('cash_rs',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(STARTING_CAPITAL_RS),))
            c.execute("INSERT INTO state(key,value) VALUES('state_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(STATE_VERSION,))
        c.commit(); c.close()

def ema(vals,p):
    a=2/(p+1); out=[float(vals[0])]
    for v in vals[1:]: out.append(a*float(v)+(1-a)*out[-1])
    return out

def fetch_klines(symbol,limit=120):
    product_id=COINBASE_SYMBOL_MAP.get(symbol)
    if not product_id:
        raise RuntimeError("No Coinbase mapping for "+symbol)
    url=COINBASE_CANDLES_URL.format(product_id=product_id)+"?"+urlencode({"granularity":60})
    req=Request(url,headers={
        "User-Agent":"Mozilla/5.0 crypto-paper-bot",
        "Accept":"application/json"
    })
    with urlopen(req,timeout=10) as r:
        raw=json.loads(r.read().decode("utf-8"))
    if not isinstance(raw,list) or not raw:
        raise RuntimeError("Coinbase returned no candles for "+product_id)
    now_s=int(time.time())
    current_minute_start=(now_s//60)*60
    candles=[]
    # Coinbase schema: [time, low, high, open, close, volume], newest first.
    for k in sorted(raw,key=lambda x:int(x[0])):
        ot=int(k[0])
        if ot>=current_minute_start:
            continue
        candles.append({
            "open_time_ms":ot*1000,
            "close_time_ms":ot*1000+59999,
            "open":float(k[3]),
            "high":float(k[2]),
            "low":float(k[1]),
            "close":float(k[4]),
            "volume":float(k[5])
        })
    return candles[-limit:]
def signal_on_last_bar(cs):
    if len(cs)<25: return False
    closes=[x['close'] for x in cs]; vols=[x['volume'] for x in cs]; ef=ema(closes,9); es=ema(closes,20); last=cs[-1]
    prev20=vols[-21:-1]; avg=sum(prev20)/len(prev20)
    dt=datetime.fromtimestamp(last['close_time_ms']/1000,tz=timezone.utc); day0=datetime(dt.year,dt.month,dt.day,tzinfo=timezone.utc).timestamp()*1000
    day=[x for x in cs if x['open_time_ms']>=day0]; vv=sum(x['volume'] for x in day); pv=sum(((x['high']+x['low']+x['close'])/3)*x['volume'] for x in day)
    vwap=pv/vv if vv else last['close']
    return ef[-1]>es[-1] and last['close']>vwap and last['close']>ef[-1] and last['volume']>=1.2*avg

def current_cash(c): return float(c.execute("SELECT value FROM state WHERE key='cash_rs'").fetchone()['value'])

def open_position(symbol,candle):
    with _db_lock:
        c=db()
        if c.execute('SELECT 1 FROM positions WHERE symbol=?',(symbol,)).fetchone() or c.execute('SELECT COUNT(*) n FROM positions').fetchone()['n']>=MAX_OPEN_POSITIONS: c.close(); return
        notional=current_cash(c)*POSITION_PCT; raw=candle['close']; entry=raw*(1+SLIPPAGE_PCT_PER_SIDE)
        c.execute('INSERT INTO positions VALUES(?,?,?,?,?,?,0)',(symbol,datetime.fromtimestamp(candle['close_time_ms']/1000,tz=timezone.utc).isoformat(),candle['close_time_ms'],raw,entry,notional)); c.commit(); c.close()

def close_position(c,pos,candle,raw_exit,reason):
    entry=float(pos['entry_price']); notional=float(pos['notional_rs']); exitp=float(raw_exit)*(1-SLIPPAGE_PCT_PER_SIDE); gross_ret=(exitp-entry)/entry; gross=notional*gross_ret
    commission=notional*COMMISSION_PCT_PER_SIDE + max(0,notional*(1+gross_ret))*COMMISSION_PCT_PER_SIDE
    net=gross-commission; cash=current_cash(c)+net
    c.execute('INSERT INTO trades(symbol,entry_time,exit_time,raw_entry_price,entry_price,raw_exit_price,exit_price,notional_rs,gross_pl_rs,commission_rs,net_pl_rs,return_pct,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(pos['symbol'],pos['entry_time'],datetime.fromtimestamp(candle['close_time_ms']/1000,tz=timezone.utc).isoformat(),pos['raw_entry_price'],entry,raw_exit,exitp,notional,gross,commission,net,(net/notional)*100 if notional else 0,reason))
    c.execute('DELETE FROM positions WHERE symbol=?',(pos['symbol'],)); c.execute("UPDATE state SET value=? WHERE key='cash_rs'",(str(cash),))

def process_position(symbol,candle):
    with _db_lock:
        c=db(); pos=c.execute('SELECT * FROM positions WHERE symbol=?',(symbol,)).fetchone()
        if not pos or candle['close_time_ms']<=pos['entry_close_time_ms']: c.close(); return
        bars=pos['bars_held']+1; entry=pos['entry_price']; tp=entry*(1+TAKE_PROFIT_PCT); sl=entry*(1-STOP_LOSS_PCT)
        if candle['low']<=sl: close_position(c,pos,candle,sl,'SL')
        elif candle['high']>=tp: close_position(c,pos,candle,tp,'TP')
        elif bars>=MAX_HOLD_BARS: close_position(c,pos,candle,candle['close'],'TIME')
        else: c.execute('UPDATE positions SET bars_held=? WHERE symbol=?',(bars,symbol))
        c.commit(); c.close()

def processed(symbol,t):
    c=db(); r=c.execute('SELECT 1 FROM processed_bars WHERE symbol=? AND close_time_ms=?',(symbol,t)).fetchone(); c.close(); return bool(r)

def mark(symbol,t):
    c=db(); c.execute('INSERT OR IGNORE INTO processed_bars VALUES(?,?)',(symbol,t)); c.commit(); c.close()

def process_symbol(symbol):
    cs=fetch_klines(symbol); last=cs[-1] if cs else None
    if not last or processed(symbol,last['close_time_ms']): return
    process_position(symbol,last)
    if signal_on_last_bar(cs): open_position(symbol,last)
    mark(symbol,last['close_time_ms'])


def fetch_historical_chunk(symbol,start_dt,end_dt):
    product_id=COINBASE_SYMBOL_MAP.get(symbol)
    if not product_id: return []
    params={"granularity":60,"start":start_dt.astimezone(timezone.utc).isoformat().replace("+00:00","Z"),"end":end_dt.astimezone(timezone.utc).isoformat().replace("+00:00","Z")}
    url=COINBASE_CANDLES_URL.format(product_id=product_id)+"?"+urlencode(params)
    req=Request(url,headers={"User-Agent":"Mozilla/5.0 crypto-paper-bot","Accept":"application/json"})
    with urlopen(req,timeout=15) as r: raw=json.loads(r.read().decode('utf-8'))
    if not isinstance(raw,list): raise RuntimeError('Coinbase bad response for '+product_id)
    out={}
    for k in raw:
        if isinstance(k,list) and len(k)>=6:
            ot=int(k[0]); out[ot]={"open_time_ms":ot*1000,"close_time_ms":ot*1000+59999,"open":float(k[3]),"high":float(k[2]),"low":float(k[1]),"close":float(k[4]),"volume":float(k[5])}
    return [out[k] for k in sorted(out)]

def fetch_historical_day(symbol,day_start,range_start,range_end):
    day_end=min(day_start+timedelta(days=1),range_end); cur=max(day_start,range_start); rows={}
    while cur<day_end:
        chunk_end=min(cur+timedelta(minutes=299),day_end)
        for x in fetch_historical_chunk(symbol,cur,chunk_end):
            ts=x['open_time_ms']/1000
            if range_start.timestamp()<=ts<range_end.timestamp(): rows[x['open_time_ms']]=x
        cur=chunk_end+timedelta(minutes=1); time.sleep(.16)
    return [rows[k] for k in sorted(rows)]

def run_backtest_30d():
    global _backtest_running
    try:
        set_state('bt_status','running'); set_state('bt_progress','0'); set_state('bt_message','Starting 30-day backtest...'); set_state('bt_result','')
        end_dt=datetime.now(timezone.utc).replace(second=0,microsecond=0); start_dt=end_dt-timedelta(days=30)
        capital=STARTING_CAPITAL_RS; histories={s:[] for s in SYMBOLS}; positions={}; last_candle={}; unavailable=set()
        stats={'trades':0,'wins':0,'losses':0,'gross':0.0,'costs':0.0,'net':0.0}
        pair_stats={s:{'trades':0,'wins':0,'net':0.0} for s in SYMBOLS}
        def close_bt(symbol,candle,raw_exit,reason):
            nonlocal capital
            pos=positions.pop(symbol); entry=pos['entry_price']; notional=pos['notional']; exitp=float(raw_exit)*(1-SLIPPAGE_PCT_PER_SIDE)
            gross_ret=(exitp-entry)/entry; gross=notional*gross_ret; commission=notional*COMMISSION_PCT_PER_SIDE+max(0,notional*(1+gross_ret))*COMMISSION_PCT_PER_SIDE; net=gross-commission
            capital+=net; stats['trades']+=1; stats['gross']+=gross; stats['costs']+=commission; stats['net']+=net; pair_stats[symbol]['trades']+=1; pair_stats[symbol]['net']+=net
            if net>0: stats['wins']+=1; pair_stats[symbol]['wins']+=1
            else: stats['losses']+=1
        first_day=start_dt.replace(hour=0,minute=0,second=0,microsecond=0)
        for d in range(31):
            day_start=first_day+timedelta(days=d)
            if day_start>=end_dt: break
            events=[]
            for s in SYMBOLS:
                if s in unavailable: continue
                try:
                    rows=fetch_historical_day(s,day_start,start_dt,end_dt)
                    for candle in rows: events.append((candle['open_time_ms'],s,candle))
                except Exception as e:
                    if '404' in str(e) or 'Not Found' in str(e): unavailable.add(s)
                    print('[backtest]',s,type(e).__name__,e,flush=True); time.sleep(.5)
            events.sort(key=lambda z:(z[0],z[1]))
            for _,s,candle in events:
                last_candle[s]=candle; h=histories[s]; h.append(candle)
                if len(h)>120: del h[:-120]
                pos=positions.get(s)
                if pos and candle['close_time_ms']>pos['entry_close_time_ms']:
                    pos['bars_held']+=1; entry=pos['entry_price']; tp=entry*(1+TAKE_PROFIT_PCT); sl=entry*(1-STOP_LOSS_PCT)
                    if candle['low']<=sl: close_bt(s,candle,sl,'SL')
                    elif candle['high']>=tp: close_bt(s,candle,tp,'TP')
                    elif pos['bars_held']>=MAX_HOLD_BARS: close_bt(s,candle,candle['close'],'TIME')
                if s not in positions and len(positions)<MAX_OPEN_POSITIONS and len(h)>=25 and signal_on_last_bar(h):
                    raw=candle['close']; positions[s]={'entry_close_time_ms':candle['close_time_ms'],'entry_price':raw*(1+SLIPPAGE_PCT_PER_SIDE),'notional':capital*POSITION_PCT,'bars_held':0}
            pct=min(99,int(((day_start-start_dt).total_seconds()/(end_dt-start_dt).total_seconds())*100)+3)
            set_state('bt_progress',pct); set_state('bt_message',f"Processed through {day_start.date()} • trades {stats['trades']} • capital ${capital:.2f}")
        for s in list(positions):
            if s in last_candle: close_bt(s,last_candle[s],last_candle[s]['close'],'END')
        t=stats['trades']; w=stats['wins']
        result={'days':30,'pairs_requested':len(SYMBOLS),'pairs_used':len(SYMBOLS)-len(unavailable),'unavailable':sorted(unavailable),'trades':t,'wins':w,'losses':stats['losses'],'win_rate':round((w/t*100) if t else 0,2),'gross_pl':round(stats['gross'],4),'costs':round(stats['costs'],4),'net_pl':round(stats['net'],4),'starting_capital':round(STARTING_CAPITAL_RS,2),'final_capital':round(capital,4),'return_pct':round(((capital/STARTING_CAPITAL_RS)-1)*100,3) if STARTING_CAPITAL_RS else 0,'completed_utc':datetime.now(timezone.utc).isoformat(),'pair_stats':sorted([{'symbol':s,**v,'win_rate':round((v['wins']/v['trades']*100) if v['trades'] else 0,2),'net':round(v['net'],4)} for s,v in pair_stats.items() if v['trades']>0],key=lambda x:x['net'],reverse=True)}
        set_state('bt_result',json.dumps(result)); set_state('bt_progress','100'); set_state('bt_message','30-day backtest completed.'); set_state('bt_status','completed')
    except Exception as e:
        print('[backtest] fatal',type(e).__name__,e,flush=True); set_state('bt_status','error'); set_state('bt_message',type(e).__name__+': '+str(e))
    finally:
        with _backtest_lock: _backtest_running=False

def worker_loop():
    time.sleep(3)
    while True:
        for s in SYMBOLS:
            try: process_symbol(s)
            except Exception as e: print('[worker]',s,type(e).__name__,e,flush=True)
            time.sleep(.2)
        time.sleep(POLL_SECONDS)

def ensure_worker():
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=worker_loop,daemon=True).start(); _worker_started=True

def summary_data():
    c=db(); cash=current_cash(c); open_n=c.execute('SELECT COUNT(*) n FROM positions').fetchone()['n']; a=c.execute("SELECT COUNT(*) trades,SUM(CASE WHEN net_pl_rs>0 THEN 1 ELSE 0 END) wins,SUM(CASE WHEN net_pl_rs<=0 THEN 1 ELSE 0 END) losses,COALESCE(SUM(commission_rs),0) costs,COALESCE(SUM(net_pl_rs),0) net FROM trades").fetchone(); c.close()
    t=a['trades'] or 0; w=a['wins'] or 0
    return {'symbols':len(SYMBOLS),'trades':t,'wins':w,'losses':a['losses'] or 0,'win_rate':round((w/t*100) if t else 0,2),'costs':round(a['costs'],2),'net':round(a['net'],2),'capital':round(cash,2),'open':open_n}

@app.route('/health')
def health(): return jsonify(ok=True,mode='coinbase-data-paper',symbols=len(SYMBOLS),worker_started=_worker_started,utc=datetime.now(timezone.utc).isoformat())
@app.route('/summary')
def summary(): return jsonify(summary_data())
@app.route('/trades')
def trades():
    c=db(); rows=[dict(x) for x in c.execute('SELECT * FROM trades ORDER BY id DESC LIMIT 200').fetchall()]; c.close(); return jsonify(rows)
@app.route('/reset_demo',methods=['POST'])
def reset_demo():
    with _db_lock:
        c=db(); c.execute('DELETE FROM trades'); c.execute('DELETE FROM positions'); c.execute('DELETE FROM processed_bars'); c.execute("INSERT INTO state(key,value) VALUES('cash_rs',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(STARTING_CAPITAL_RS),)); c.execute("INSERT INTO state(key,value) VALUES('state_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(STATE_VERSION,)); c.commit(); c.close()
    return redirect(url_for('dashboard'))


@app.route('/backtest/start',methods=['POST'])
def start_backtest():
    global _backtest_running
    with _backtest_lock:
        if _backtest_running or get_state('bt_status')=='running': return redirect(url_for('dashboard'))
        _backtest_running=True; threading.Thread(target=run_backtest_30d,daemon=True).start()
    return redirect(url_for('dashboard'))

@app.route('/backtest/status')
def backtest_status():
    raw=get_state('bt_result','')
    try: result=json.loads(raw) if raw else None
    except Exception: result=None
    return jsonify(status=get_state('bt_status','idle'),progress=int(get_state('bt_progress','0') or 0),message=get_state('bt_message','Not started'),result=result)

HTML='''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="20"><style>body{font-family:Arial;background:#10131a;color:#eee;padding:14px;max-width:1100px;margin:auto}.g{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:9px}.c,.n,.bt{background:#1b202b;padding:12px;border-radius:10px}.l{font-size:12px;color:#aaa}.v{font-size:21px;font-weight:700}table{width:100%;border-collapse:collapse;background:#1b202b;margin-top:12px}td,th{padding:8px;border-bottom:1px solid #333;font-size:12px;text-align:left}.n,.bt{margin:14px 0}button{background:#fff;color:#111;border:0;border-radius:8px;padding:10px 14px;font-weight:700}.p{height:10px;background:#303746;border-radius:8px;overflow:hidden;margin:8px 0}.pb{height:100%;background:#ddd}.small{color:#aaa;font-size:12px}</style><h2>Crypto Futures 1m Automatic Paper Bot</h2><p>Coinbase public 1m data • No API key • No real orders • 20 pairs</p><div class=g>{% for k,v in cards %}<div class=c><div class=l>{{k}}</div><div class=v>{{v}}</div></div>{% endfor %}</div><div class=n><b>Strategy:</b> EMA9 &gt; EMA20, close above VWAP & EMA9, volume ≥ 1.2× 20-bar average. TP +0.30%, SL -0.20%, max hold 10 bars, 10% virtual capital/trade. Fee 0.15%/side + slippage 0.05%/side.<br><br><b>Demo only.</b> Render Free can sleep, and SQLite data may disappear after restart/redeploy.</div><div class=bt><h3>30-Day Backtest</h3><p class=small>Backtest is separate from live paper trading. Starting capital $100. Same strategy, fees and slippage.</p>{% if bt_status == 'running' %}<b>Running: {{bt_progress}}%</b><div class=p><div class=pb style="width:{{bt_progress}}%"></div></div><div class=small>{{bt_message}}</div>{% else %}<form method=post action=/backtest/start><button>Run 30-Day Backtest</button></form>{% if bt_message %}<p class=small>{{bt_message}}</p>{% endif %}{% endif %}{% if bt_result %}<div class=g><div class=c><div class=l>BT Trades</div><div class=v>{{bt_result.trades}}</div></div><div class=c><div class=l>BT Win Rate</div><div class=v>{{bt_result.win_rate}}%</div></div><div class=c><div class=l>BT Net P/L</div><div class=v>$ {{bt_result.net_pl}}</div></div><div class=c><div class=l>BT Final Capital</div><div class=v>$ {{bt_result.final_capital}}</div></div><div class=c><div class=l>BT Return</div><div class=v>{{bt_result.return_pct}}%</div></div><div class=c><div class=l>Pairs Used</div><div class=v>{{bt_result.pairs_used}}/{{bt_result.pairs_requested}}</div></div></div>{% if bt_result.unavailable %}<p class=small>Unavailable on Coinbase: {{bt_result.unavailable|join(', ')}}</p>{% endif %}{% endif %}</div><h3>Open Positions</h3><table><tr><th>Pair</th><th>Entry</th><th>$ Notional</th><th>Bars</th></tr>{% for p in positions %}<tr><td>{{p.symbol}}</td><td>{{p.entry_price}}</td><td>{{'%.2f'|format(p.notional_rs)}}</td><td>{{p.bars_held}}</td></tr>{% else %}<tr><td colspan=4>None</td></tr>{% endfor %}</table><h3>Latest Trades</h3><table><tr><th>Pair</th><th>Exit</th><th>P/L</th><th>Return</th></tr>{% for t in trades %}<tr><td>{{t.symbol}}</td><td>{{t.reason}}</td><td>$ {{'%.2f'|format(t.net_pl_rs)}}</td><td>{{'%.3f'|format(t.return_pct)}}%</td></tr>{% else %}<tr><td colspan=4>No trades yet</td></tr>{% endfor %}</table><form method=post action=/reset_demo><p><button>Reset Live Demo</button></p></form>'''

@app.route('/')
def dashboard():
    ensure_worker(); s=summary_data(); c=db(); pos=[dict(x) for x in c.execute('SELECT * FROM positions ORDER BY entry_time DESC')]; tr=[dict(x) for x in c.execute('SELECT * FROM trades ORDER BY id DESC LIMIT 50')]; c.close()
    cards=[('Pairs',s['symbols']),('Trades',s['trades']),('Wins',s['wins']),('Losses',s['losses']),('Win Rate',str(s['win_rate'])+'%'),('Net P/L','$ '+str(s['net'])),('Costs','$ '+str(s['costs'])),('Capital','$ '+str(s['capital'])),('Open',s['open'])]
    bt_status=get_state('bt_status','idle'); bt_progress=int(get_state('bt_progress','0') or 0); bt_message=get_state('bt_message','')
    raw=get_state('bt_result','')
    try: bt_result=json.loads(raw) if raw else None
    except Exception: bt_result=None
    return render_template_string(HTML,cards=cards,positions=pos,trades=tr,bt_status=bt_status,bt_progress=bt_progress,bt_message=bt_message,bt_result=bt_result)

init_db(); ensure_worker()
if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.getenv('PORT','8080')))
