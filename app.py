import csv
import io
import os, json, threading, time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from flask import Flask, jsonify, redirect, render_template_string, url_for, Response, send_file
from sqlalchemy import create_engine, String, Integer, BigInteger, Float, Text, select, func, delete
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

app = Flask(__name__)

SYMBOLS = ["XRPUSDT"]
COINBASE_SYMBOL_MAP={
'ADAUSDT':'ADA-USD','XRPUSDT':'XRP-USD','DOGEUSDT':'DOGE-USD','LINKUSDT':'LINK-USD','AVAXUSDT':'AVAX-USD','DOTUSDT':'DOT-USD','LTCUSDT':'LTC-USD','BCHUSDT':'BCH-USD','ATOMUSDT':'ATOM-USD','NEARUSDT':'NEAR-USD','FILUSDT':'FIL-USD','APTUSDT':'APT-USD','ARBUSDT':'ARB-USD','OPUSDT':'OP-USD','INJUSDT':'INJ-USD','SUIUSDT':'SUI-USD','SEIUSDT':'SEI-USD','TIAUSDT':'TIA-USD','JTOUSDT':'JTO-USD','ETCUSDT':'ETC-USD'}
COINBASE_CANDLES_URL='https://api.exchange.coinbase.com/products/{product_id}/candles'

STARTING_CAPITAL=100.0
POSITION_PCT=0.10
MAX_OPEN_POSITIONS=5
CP1_REVERSE=True
COOLDOWN_BARS=0
COMMISSION_PCT_PER_SIDE=0.0005
SLIPPAGE_PCT_PER_SIDE=0.0001
POLL_SECONDS=20
BACKTEST_CSV_PATH='/tmp/XRPUSDT_15day_1m.csv'

DATABASE_URL=os.getenv('DATABASE_URL','').strip()
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL='postgresql+psycopg://'+DATABASE_URL[len('postgres://'):]
elif DATABASE_URL.startswith('postgresql://'):
    DATABASE_URL='postgresql+psycopg://'+DATABASE_URL[len('postgresql://'):]
USING_PERSISTENT_DB=bool(DATABASE_URL)
if not DATABASE_URL:
    DATABASE_URL='sqlite:////tmp/crypto_paper_bot.db'

engine=create_engine(DATABASE_URL,pool_pre_ping=True,future=True,connect_args={'check_same_thread':False} if DATABASE_URL.startswith('sqlite') else {})
SessionLocal=sessionmaker(bind=engine,expire_on_commit=False,future=True)

class Base(DeclarativeBase): pass
class State(Base):
    __tablename__='state'; key:Mapped[str]=mapped_column(String(100),primary_key=True); value:Mapped[str]=mapped_column(Text,nullable=False)
class ProcessedBar(Base):
    __tablename__='processed_bars'; symbol:Mapped[str]=mapped_column(String(30),primary_key=True); close_time_ms:Mapped[int]=mapped_column(BigInteger,primary_key=True)
class Position(Base):
    __tablename__='positions'; symbol:Mapped[str]=mapped_column(String(30),primary_key=True); side:Mapped[str]=mapped_column(String(10),default='LONG'); entry_time:Mapped[str]=mapped_column(String(80)); entry_close_time_ms:Mapped[int]=mapped_column(BigInteger); raw_entry_price:Mapped[float]=mapped_column(Float); entry_price:Mapped[float]=mapped_column(Float); notional:Mapped[float]=mapped_column(Float); bars_held:Mapped[int]=mapped_column(Integer,default=0)
class Trade(Base):
    __tablename__='trades'; id:Mapped[int]=mapped_column(Integer,primary_key=True,autoincrement=True); symbol:Mapped[str]=mapped_column(String(30)); entry_time:Mapped[str]=mapped_column(String(80)); exit_time:Mapped[str]=mapped_column(String(80)); raw_entry_price:Mapped[float]=mapped_column(Float); entry_price:Mapped[float]=mapped_column(Float); raw_exit_price:Mapped[float]=mapped_column(Float); exit_price:Mapped[float]=mapped_column(Float); notional:Mapped[float]=mapped_column(Float); gross_pl:Mapped[float]=mapped_column(Float); commission:Mapped[float]=mapped_column(Float); net_pl:Mapped[float]=mapped_column(Float); return_pct:Mapped[float]=mapped_column(Float); reason:Mapped[str]=mapped_column(String(20))
class Cooldown(Base):
    __tablename__='cooldowns'; symbol:Mapped[str]=mapped_column(String(30),primary_key=True); until_close_time_ms:Mapped[int]=mapped_column(BigInteger)

Base.metadata.create_all(engine)
_db_lock=threading.Lock(); _worker_lock=threading.Lock(); _worker_started=False; _backtest_lock=threading.Lock(); _backtest_running=False

def get_state(key,default=None):
    with SessionLocal() as s:
        r=s.get(State,key); return r.value if r else default

def set_state(key,value):
    with _db_lock, SessionLocal() as s:
        r=s.get(State,key)
        if r: r.value=str(value)
        else: s.add(State(key=key,value=str(value)))
        s.commit()

def init_db():
    version='cp1-v1'
    with _db_lock, SessionLocal() as s:
        if not s.get(State,'cash'):
            s.add(State(key='cash',value=str(STARTING_CAPITAL)))
        sv=s.get(State,'strategy_version')
        if not sv:
            s.add(State(key='strategy_version',value=version))
        elif sv.value!=version:
            # Preserve closed-trade history/cash, but old-strategy open positions
            # cannot safely continue under CP1 fixed stop/target rules.
            s.execute(delete(Position)); s.execute(delete(ProcessedBar)); s.execute(delete(Cooldown))
            sv.value=version
            br=s.get(State,'bt_result')
            if br: br.value=''
            bs=s.get(State,'bt_status')
            if bs: bs.value='idle'
            bm=s.get(State,'bt_message')
            if bm: bm.value=''
        s.commit()

def current_cash(s):
    r=s.get(State,'cash')
    if not r:
        r=State(key='cash',value=str(STARTING_CAPITAL)); s.add(r); s.flush()
    return float(r.value)

def set_cash(s,amount):
    r=s.get(State,'cash')
    if r: r.value=str(amount)
    else: s.add(State(key='cash',value=str(amount)))

def cp1_pattern(cs):
    """Geraked CP1 candlestick setup on the most recent two CLOSED candles.

    Original Pine defaults Reverse Signal=True. Therefore:
      bullish CP1 pattern -> SHORT
      bearish CP1 pattern -> LONG
    The signal candle low/high is the profit target in reverse mode and
    the stop is placed at equal distance on the opposite side (1:1 RR).
    """
    if len(cs)<2:
        return None
    prev=cs[-2]; cur=cs[-1]
    long_signal=(cur['close']>cur['open'] and prev['close']>prev['open'] and
                 prev['open']<cur['open'] and cur['close']>prev['close'] and
                 cur['low']<prev['low'] and cur['low']<prev['open'])
    short_signal=(cur['close']<cur['open'] and prev['close']<prev['open'] and
                  prev['open']>cur['open'] and cur['close']<prev['close'] and
                  cur['high']>prev['high'] and cur['high']>prev['open'])
    if long_signal:
        return {'side':'SHORT' if CP1_REVERSE else 'LONG',
                'signal_kind':'BULL_PATTERN','level':cur['low']}
    if short_signal:
        return {'side':'LONG' if CP1_REVERSE else 'SHORT',
                'signal_kind':'BEAR_PATTERN','level':cur['high']}
    return None

def signal_on_last_bar(cs):
    x=cp1_pattern(cs)
    return x['side'] if x else None

def fetch_klines(symbol,limit=180):
    pid=COINBASE_SYMBOL_MAP.get(symbol)
    url=COINBASE_CANDLES_URL.format(product_id=pid)+'?'+urlencode({'granularity':60})
    req=Request(url,headers={'User-Agent':'Mozilla/5.0 crypto-paper-bot','Accept':'application/json'})
    with urlopen(req,timeout=10) as r: raw=json.loads(r.read().decode('utf-8'))
    if not isinstance(raw,list) or not raw: raise RuntimeError('Coinbase returned no candles for '+str(pid))
    now_s=int(time.time()); cur=(now_s//60)*60; out=[]
    for k in sorted(raw,key=lambda x:int(x[0])):
        ot=int(k[0])
        if ot>=cur: continue
        out.append({'open_time_ms':ot*1000,'close_time_ms':ot*1000+59999,'open':float(k[3]),'high':float(k[2]),'low':float(k[1]),'close':float(k[4]),'volume':float(k[5])})
    return out[-limit:]

def set_cooldown(s,symbol,t):
    until=t+COOLDOWN_BARS*60000; r=s.get(Cooldown,symbol)
    if r: r.until_close_time_ms=until
    else: s.add(Cooldown(symbol=symbol,until_close_time_ms=until))

def open_cp1_position(s,symbol,c,setup):
    if s.get(Position,symbol): return None
    if (s.scalar(select(func.count()).select_from(Position)) or 0)>=MAX_OPEN_POSITIONS: return None
    cd=s.get(Cooldown,symbol)
    if cd and c['close_time_ms']<=cd.until_close_time_ms: return None
    side=setup['side']; raw_open=float(c['open'])
    entry=raw_open*(1+SLIPPAGE_PCT_PER_SIDE) if side=='LONG' else raw_open*(1-SLIPPAGE_PCT_PER_SIDE)
    level=float(setup['level'])
    if CP1_REVERSE:
        target=level
        stop=2*entry-target
    else:
        stop=level
        target=2*entry-stop
    if side=='LONG' and not (stop<entry<target): return None
    if side=='SHORT' and not (target<entry<stop): return None
    n=current_cash(s)*POSITION_PCT
    # Position.raw_entry_price stores CP1's fixed stop to avoid a schema change.
    p=Position(symbol=symbol,side=side,
               entry_time=datetime.fromtimestamp(c['open_time_ms']/1000,tz=timezone.utc).isoformat(),
               entry_close_time_ms=c['close_time_ms'],raw_entry_price=stop,
               entry_price=entry,notional=n,bars_held=0)
    s.add(p); s.flush(); return p

def cp1_levels(pos):
    stop=float(pos.raw_entry_price); entry=float(pos.entry_price); target=2*entry-stop
    return stop,target

def close_position(s,pos,c,raw_exit,reason):
    side=getattr(pos,'side','LONG') or 'LONG'; entry=pos.entry_price; n=pos.notional
    exitp=float(raw_exit)*(1-SLIPPAGE_PCT_PER_SIDE) if side=='LONG' else float(raw_exit)*(1+SLIPPAGE_PCT_PER_SIDE)
    gr=(exitp-entry)/entry if side=='LONG' else (entry-exitp)/entry
    gross=n*gr; fee=n*COMMISSION_PCT_PER_SIDE+max(0,n*(1+gr))*COMMISSION_PCT_PER_SIDE; net=gross-fee
    s.add(Trade(symbol=pos.symbol,entry_time=pos.entry_time,
                exit_time=datetime.fromtimestamp(c['close_time_ms']/1000,tz=timezone.utc).isoformat(),
                raw_entry_price=entry,entry_price=entry,raw_exit_price=float(raw_exit),exit_price=exitp,
                notional=n,gross_pl=gross,commission=fee,net_pl=net,
                return_pct=(net/n)*100 if n else 0,reason=side+' '+reason))
    set_cash(s,current_cash(s)+net); set_cooldown(s,pos.symbol,c['close_time_ms']); s.delete(pos)

def maybe_exit_cp1(s,pos,c):
    stop,target=cp1_levels(pos); side=getattr(pos,'side','LONG') or 'LONG'
    # Conservative same-candle assumption: if both levels trade, count stop first.
    if side=='LONG':
        if c['low']<=stop: close_position(s,pos,c,stop,'SL'); return True
        if c['high']>=target: close_position(s,pos,c,target,'TP'); return True
    else:
        if c['high']>=stop: close_position(s,pos,c,stop,'SL'); return True
        if c['low']<=target: close_position(s,pos,c,target,'TP'); return True
    return False

def process_symbol(symbol):
    cs=fetch_klines(symbol,limit=180); last=cs[-1] if cs else None
    if not last: return
    with _db_lock, SessionLocal() as s:
        if s.get(ProcessedBar,{'symbol':symbol,'close_time_ms':last['close_time_ms']}): return
        pos=s.get(Position,symbol)
        if pos:
            pos.bars_held+=1
            maybe_exit_cp1(s,pos,last)
        # TradingView strategy orders normally fill on the NEXT bar.
        # So detect CP1 on the previous closed candle pair and enter at this bar's open.
        if not s.get(Position,symbol) and len(cs)>=3:
            setup=cp1_pattern(cs[:-1])
            if setup:
                pos=open_cp1_position(s,symbol,last,setup)
                if pos: maybe_exit_cp1(s,pos,last)
        s.add(ProcessedBar(symbol=symbol,close_time_ms=last['close_time_ms'])); s.commit()

def worker_loop():
    time.sleep(3)
    while True:
        for x in SYMBOLS:
            try: process_symbol(x)
            except Exception as e: print('[worker]',x,type(e).__name__,e,flush=True)
            time.sleep(.2)
        time.sleep(POLL_SECONDS)

def ensure_worker():
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=worker_loop,daemon=True).start(); _worker_started=True

def fetch_historical_chunk(symbol,start_dt,end_dt):
    pid=COINBASE_SYMBOL_MAP.get(symbol); params={'granularity':60,'start':start_dt.astimezone(timezone.utc).isoformat().replace('+00:00','Z'),'end':end_dt.astimezone(timezone.utc).isoformat().replace('+00:00','Z')}; url=COINBASE_CANDLES_URL.format(product_id=pid)+'?'+urlencode(params); req=Request(url,headers={'User-Agent':'Mozilla/5.0 crypto-paper-bot','Accept':'application/json'})
    with urlopen(req,timeout=15) as r: raw=json.loads(r.read().decode('utf-8'))
    out={}
    for k in raw if isinstance(raw,list) else []:
        ot=int(k[0]); out[ot]={'open_time_ms':ot*1000,'close_time_ms':ot*1000+59999,'open':float(k[3]),'high':float(k[2]),'low':float(k[1]),'close':float(k[4]),'volume':float(k[5])}
    return [out[k] for k in sorted(out)]

def fetch_historical_day(symbol,day_start,range_start,range_end):
    end=min(day_start+timedelta(days=1),range_end); cur=max(day_start,range_start); rows={}
    while cur<end:
        ce=min(cur+timedelta(minutes=299),end)
        for x in fetch_historical_chunk(symbol,cur,ce):
            ts=x['open_time_ms']/1000
            if range_start.timestamp()<=ts<range_end.timestamp(): rows[x['open_time_ms']]=x
        cur=ce+timedelta(minutes=1); time.sleep(.16)
    return [rows[k] for k in sorted(rows)]

def run_backtest_15d():
    global _backtest_running
    try:
        set_state('bt_status','running'); set_state('bt_progress','0'); set_state('bt_message','Starting CP1 15-day backtest...'); set_state('bt_result','')
        end_dt=datetime.now(timezone.utc).replace(second=0,microsecond=0); start_dt=end_dt-timedelta(days=15)
        capital=STARTING_CAPITAL; hist={s:[] for s in SYMBOLS}; pos={}; lastc={}; unavailable=set()
        csv_seen=set()
        with open(BACKTEST_CSV_PATH,'w',newline='',encoding='utf-8') as _f:
            _w=csv.writer(_f)
            _w.writerow(['timestamp','datetime_utc','open','high','low','close','volume'])
        st={'trades':0,'wins':0,'losses':0,'gross':0.0,'costs':0.0,'net':0.0,
            'tp_count':0,'sl_count':0,'end_count':0,'tp_net':0.0,'sl_net':0.0,'end_net':0.0,
            'long_count':0,'short_count':0,'long_net':0.0,'short_net':0.0}
        def close_bt(sym,c,raw,reason):
            nonlocal capital
            p=pos.pop(sym); entry=p['entry']; n=p['notional']; side=p['side']
            exitp=float(raw)*(1-SLIPPAGE_PCT_PER_SIDE) if side=='LONG' else float(raw)*(1+SLIPPAGE_PCT_PER_SIDE)
            gr=(exitp-entry)/entry if side=='LONG' else (entry-exitp)/entry
            gross=n*gr; fee=n*COMMISSION_PCT_PER_SIDE+max(0,n*(1+gr))*COMMISSION_PCT_PER_SIDE; net=gross-fee
            capital+=net; st['trades']+=1; st['gross']+=gross; st['costs']+=fee; st['net']+=net
            if reason=='TP': st['tp_count']+=1; st['tp_net']+=net
            elif reason=='SL': st['sl_count']+=1; st['sl_net']+=net
            else: st['end_count']+=1; st['end_net']+=net
            if side=='LONG': st['long_count']+=1; st['long_net']+=net
            else: st['short_count']+=1; st['short_net']+=net
            if net>0: st['wins']+=1
            else: st['losses']+=1
        def check_exit(sym,c):
            p=pos.get(sym)
            if not p: return False
            side=p['side']; stop=p['stop']; target=p['target']
            if side=='LONG':
                if c['low']<=stop: close_bt(sym,c,stop,'SL'); return True
                if c['high']>=target: close_bt(sym,c,target,'TP'); return True
            else:
                if c['high']>=stop: close_bt(sym,c,stop,'SL'); return True
                if c['low']<=target: close_bt(sym,c,target,'TP'); return True
            return False
        fd=start_dt.replace(hour=0,minute=0,second=0,microsecond=0)
        for d in range(16):
            ds=fd+timedelta(days=d)
            if ds>=end_dt: break
            ev=[]
            for sym in SYMBOLS:
                if sym in unavailable: continue
                try:
                    rows=fetch_historical_day(sym,ds,start_dt,end_dt)
                    # Cache the exact same candles used by the backtest.
                    if sym=='XRPUSDT' and rows:
                        with open(BACKTEST_CSV_PATH,'a',newline='',encoding='utf-8') as _f:
                            _w=csv.writer(_f)
                            for c in rows:
                                ts=int(c['open_time_ms']//1000)
                                if ts not in csv_seen:
                                    csv_seen.add(ts)
                                    dt=datetime.fromtimestamp(ts,tz=timezone.utc).isoformat()
                                    _w.writerow([ts,dt,c['open'],c['high'],c['low'],c['close'],c['volume']])
                    for c in rows: ev.append((c['open_time_ms'],sym,c))
                except Exception as e: print('[backtest]',sym,type(e).__name__,e,flush=True)
            ev.sort(key=lambda z:(z[0],z[1]))
            for _,sym,c in ev:
                lastc[sym]=c; h=hist[sym]; h.append(c)
                if len(h)>180: del h[:-180]
                # Existing position first: it was entered on an earlier bar.
                if sym in pos: check_exit(sym,c)
                # CP1 signal is confirmed on previous bar, entry at CURRENT bar open.
                if sym not in pos and len(pos)<MAX_OPEN_POSITIONS and len(h)>=3:
                    setup=cp1_pattern(h[:-1])
                    if setup:
                        side=setup['side']; raw_open=float(c['open'])
                        entry=raw_open*(1+SLIPPAGE_PCT_PER_SIDE) if side=='LONG' else raw_open*(1-SLIPPAGE_PCT_PER_SIDE)
                        level=float(setup['level'])
                        if CP1_REVERSE: target=level; stop=2*entry-target
                        else: stop=level; target=2*entry-stop
                        valid=(stop<entry<target) if side=='LONG' else (target<entry<stop)
                        if valid:
                            pos[sym]={'t':c['close_time_ms'],'entry':entry,'notional':capital*POSITION_PCT,
                                      'side':side,'stop':stop,'target':target}
                            check_exit(sym,c)
            pct=min(99,int(((ds-start_dt).total_seconds()/(end_dt-start_dt).total_seconds())*100)+3)
            set_state('bt_progress',pct); set_state('bt_message',f"CP1 through {ds.date()} • trades {st['trades']} • capital ${capital:.2f}")
        for sym in list(pos):
            if sym in lastc: close_bt(sym,lastc[sym],lastc[sym]['close'],'END')
        tr=st['trades']; w=st['wins']
        result={'strategy':'CP1 — Candlestick Pattern Strategy 1 (Reverse ON)','days':15,
                'pairs_requested':len(SYMBOLS),'pairs_used':len(SYMBOLS)-len(unavailable),
                'trades':tr,'wins':w,'losses':st['losses'],'win_rate':round((w/tr*100) if tr else 0,2),
                'gross_pl':round(st['gross'],4),'costs':round(st['costs'],4),'net_pl':round(st['net'],4),
                'starting_capital':STARTING_CAPITAL,'final_capital':round(capital,4),
                'return_pct':round((capital/STARTING_CAPITAL-1)*100,3),
                'tp_count':st['tp_count'],'sl_count':st['sl_count'],'end_count':st['end_count'],
                'tp_net':round(st['tp_net'],4),'sl_net':round(st['sl_net'],4),'end_net':round(st['end_net'],4),
                'tp_pct':round((st['tp_count']/tr*100) if tr else 0,2),
                'sl_pct':round((st['sl_count']/tr*100) if tr else 0,2),
                'end_pct':round((st['end_count']/tr*100) if tr else 0,2),
                'long_count':st['long_count'],'short_count':st['short_count'],
                'long_net':round(st['long_net'],4),'short_net':round(st['short_net'],4)}
        set_state('bt_result',json.dumps(result)); set_state('bt_progress','100'); set_state('bt_message','CP1 15-day backtest completed.'); set_state('bt_status','completed')
    except Exception as e:
        set_state('bt_status','error'); set_state('bt_message',type(e).__name__+': '+str(e)); print('[backtest] fatal',e,flush=True)
    finally:
        with _backtest_lock: _backtest_running=False

def summary_data():
    with SessionLocal() as s:
        cash=current_cash(s); open_n=s.scalar(select(func.count()).select_from(Position)) or 0; trades=s.scalars(select(Trade)).all(); t=len(trades); wins=sum(1 for x in trades if x.net_pl>0); costs=sum(x.commission for x in trades); net=sum(x.net_pl for x in trades)
        return {'symbols':len(SYMBOLS),'trades':t,'wins':wins,'losses':t-wins,'win_rate':round((wins/t*100) if t else 0,2),'costs':round(costs,4),'net':round(net,4),'capital':round(cash,4),'open':open_n}

@app.route('/health')
def health(): return jsonify(ok=True,persistent_db=USING_PERSISTENT_DB,worker_started=_worker_started,utc=datetime.now(timezone.utc).isoformat())
@app.route('/summary')
def summary(): return jsonify(summary_data())
@app.route('/trades')
def trades_api():
    with SessionLocal() as s:
        xs=s.scalars(select(Trade).order_by(Trade.id.desc()).limit(300)).all(); return jsonify([{'id':x.id,'symbol':x.symbol,'entry_time':x.entry_time,'exit_time':x.exit_time,'entry_price':x.entry_price,'exit_price':x.exit_price,'notional':x.notional,'net_pl':x.net_pl,'return_pct':x.return_pct,'reason':x.reason} for x in xs])
@app.route('/reset_demo',methods=['POST'])
def reset_demo():
    with _db_lock, SessionLocal() as s:
        s.execute(delete(Trade)); s.execute(delete(Position)); s.execute(delete(ProcessedBar)); s.execute(delete(Cooldown)); set_cash(s,STARTING_CAPITAL); s.commit()
    return redirect(url_for('dashboard'))
@app.route('/backtest/start',methods=['POST'])
def start_backtest():
    global _backtest_running
    with _backtest_lock:
        if _backtest_running: return redirect(url_for('dashboard'))
        _backtest_running=True; threading.Thread(target=run_backtest_15d,daemon=True).start()
    return redirect(url_for('dashboard'))

HTML='''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="20"><style>body{font-family:Arial;background:#10131a;color:#eee;padding:14px;max-width:1100px;margin:auto}.g{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}.c,.n,.bt{background:#1b202b;padding:13px;border-radius:12px}.l{font-size:12px;color:#aaa}.v{font-size:22px;font-weight:700}table{width:100%;border-collapse:collapse;background:#1b202b;margin-top:12px}td,th{padding:8px;border-bottom:1px solid #333;font-size:12px;text-align:left}.n,.bt{margin:14px 0}.ok{color:#78d98b}.bad{color:#ff9c9c}button{background:#fff;color:#111;border:0;border-radius:8px;padding:10px 14px;font-weight:700}.p{height:10px;background:#303746;border-radius:8px;overflow:hidden;margin:8px 0}.pb{height:100%;background:#ddd}.small{color:#aaa;font-size:12px}</style><h2>Crypto 1m Automatic Paper Bot — CP1</h2><p>Coinbase public 1m data • No API key • No real orders • XRPUSDT only</p><div class=n><b>Storage:</b> {% if persistent %}<span class=ok>Persistent database connected ✅</span>{% else %}<span class=bad>Temporary SQLite ⚠️ — restart/redeploy can erase history</span>{% endif %}</div><div class=g>{% for k,v in cards %}<div class=c><div class=l>{{k}}</div><div class=v>{{v}}</div></div>{% endfor %}</div><div class=n><b>Strategy:</b> CP1 — Candlestick Pattern Strategy 1.<br><b>Original setting:</b> Reverse Signal = ON.<br><b>Bullish CP1 pattern:</b> enters SHORT on next 1m candle open.<br><b>Bearish CP1 pattern:</b> enters LONG on next 1m candle open.<br><b>Exit:</b> signal-candle high/low target with equal-distance stop (1:1 gross R:R).<br><b>Execution:</b> next-bar open; if TP and SL both touch in one candle, SL is counted first (conservative).<br><b>Risk:</b> max 5 open positions, 10% capital/trade.<br><b>Costs:</b> fee 0.05%/side + slippage 0.01%/side.</div><div class=bt><h3>15-Day Backtest — CP1</h3>{% if bt_status=='running' %}<b>Running: {{bt_progress}}%</b><div class=p><div class=pb style="width:{{bt_progress}}%"></div></div><div class=small>{{bt_message}}</div>{% else %}<form method=post action=/backtest/start><button>Run 15-Day Backtest</button></form>
<a href="/download-backtest-data" style="display:inline-block;margin-top:12px;padding:14px 20px;background:#fff;color:#111;border-radius:12px;text-decoration:none;font-weight:700;">Download Backtest CSV</a>{% if bt_message %}<p class=small>{{bt_message}}</p>{% endif %}{% endif %}{% if bt_result %}<div class=g><div class=c><div class=l>BT Trades</div><div class=v>{{bt_result.trades}}</div></div><div class=c><div class=l>BT Win Rate</div><div class=v>{{bt_result.win_rate}}%</div></div><div class=c><div class=l>BT Net P/L</div><div class=v>$ {{bt_result.net_pl}}</div></div><div class=c><div class=l>BT Final Capital</div><div class=v>$ {{bt_result.final_capital}}</div></div><div class=c><div class=l>BT Return</div><div class=v>{{bt_result.return_pct}}%</div></div></div><h4>Exit Diagnostics</h4><table><tr><th>Exit</th><th>Count</th><th>% Trades</th><th>Net P/L</th></tr><tr><td>TP</td><td>{{bt_result.tp_count}}</td><td>{{bt_result.tp_pct}}%</td><td>$ {{bt_result.tp_net}}</td></tr><tr><td>SL</td><td>{{bt_result.sl_count}}</td><td>{{bt_result.sl_pct}}%</td><td>$ {{bt_result.sl_net}}</td></tr><tr><td>END</td><td>{{bt_result.end_count}}</td><td>{{bt_result.end_pct}}%</td><td>$ {{bt_result.end_net}}</td></tr></table><h4>Side Diagnostics</h4><table><tr><th>Side</th><th>Trades</th><th>Net P/L</th></tr><tr><td>LONG</td><td>{{bt_result.long_count}}</td><td>$ {{bt_result.long_net}}</td></tr><tr><td>SHORT</td><td>{{bt_result.short_count}}</td><td>$ {{bt_result.short_net}}</td></tr></table>{% endif %}</div><h3>Open Positions</h3><table><tr><th>Pair</th><th>Side</th><th>Entry</th><th>$ Notional</th><th>Bars</th></tr>{% for p in positions %}<tr><td>{{p.symbol}}</td><td>{{p.side}}</td><td>{{'%.8f'|format(p.entry_price)}}</td><td>{{'%.2f'|format(p.notional)}}</td><td>{{p.bars_held}}</td></tr>{% else %}<tr><td colspan=5>None</td></tr>{% endfor %}</table><h3>Latest Trades</h3><table><tr><th>Pair</th><th>Exit</th><th>P/L</th><th>Return</th></tr>{% for t in trades %}<tr><td>{{t.symbol}}</td><td>{{t.reason}}</td><td>$ {{'%.4f'|format(t.net_pl)}}</td><td>{{'%.3f'|format(t.return_pct)}}%</td></tr>{% else %}<tr><td colspan=4>No trades yet</td></tr>{% endfor %}</table><form method=post action=/reset_demo><p><button>Reset Live Demo</button></p></form>'''

@app.route('/')
def dashboard():
    ensure_worker(); d=summary_data(); cards=[('Pairs',d['symbols']),('Trades',d['trades']),('Wins',d['wins']),('Losses',d['losses']),('Win Rate',str(d['win_rate'])+'%'),('Net P/L','$ '+str(d['net'])),('Costs','$ '+str(d['costs'])),('Capital','$ '+str(d['capital'])),('Open',d['open'])]
    with SessionLocal() as s: positions=s.scalars(select(Position).order_by(Position.symbol)).all(); trades=s.scalars(select(Trade).order_by(Trade.id.desc()).limit(20)).all()
    raw=get_state('bt_result','')
    try: bt_result=json.loads(raw) if raw else None
    except Exception: bt_result=None
    return render_template_string(HTML,cards=cards,positions=positions,trades=trades,persistent=USING_PERSISTENT_DB,bt_status=get_state('bt_status','idle'),bt_progress=int(get_state('bt_progress','0') or 0),bt_message=get_state('bt_message',''),bt_result=bt_result)



@app.route("/download-backtest-data")
def download_backtest_data():
    """Download the exact XRP candles already fetched during the latest backtest."""
    try:
        if not os.path.exists(BACKTEST_CSV_PATH):
            return Response(
                "Pehle Run 15-Day Backtest chalayein. Phir CSV download karein.",
                status=409,
                mimetype="text/plain"
            )
        # Header-only/empty file means the backtest has not fetched useful data yet.
        if os.path.getsize(BACKTEST_CSV_PATH) < 150:
            return Response(
                "CSV abhi ready nahi hai. Backtest ko thora chalne dein.",
                status=409,
                mimetype="text/plain"
            )
        return send_file(
            BACKTEST_CSV_PATH,
            mimetype="text/csv",
            as_attachment=True,
            download_name="XRPUSDT_15day_1m.csv"
        )
    except Exception as e:
        return Response("CSV download error: " + str(e), status=500, mimetype="text/plain")


init_db(); ensure_worker()
if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.getenv('PORT','8080')))
