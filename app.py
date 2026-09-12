import os, json, threading, time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from flask import Flask, jsonify, redirect, render_template_string, url_for
from sqlalchemy import create_engine, String, Integer, BigInteger, Float, Text, select, func, delete
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

app = Flask(__name__)

SYMBOLS=['ADAUSDT','XRPUSDT','DOGEUSDT','LINKUSDT','AVAXUSDT','DOTUSDT','LTCUSDT','BCHUSDT','ATOMUSDT','NEARUSDT','FILUSDT','APTUSDT','ARBUSDT','OPUSDT','INJUSDT','SUIUSDT','SEIUSDT','TIAUSDT','JTOUSDT','ETCUSDT']
COINBASE_SYMBOL_MAP={
'ADAUSDT':'ADA-USD','XRPUSDT':'XRP-USD','DOGEUSDT':'DOGE-USD','LINKUSDT':'LINK-USD','AVAXUSDT':'AVAX-USD','DOTUSDT':'DOT-USD','LTCUSDT':'LTC-USD','BCHUSDT':'BCH-USD','ATOMUSDT':'ATOM-USD','NEARUSDT':'NEAR-USD','FILUSDT':'FIL-USD','APTUSDT':'APT-USD','ARBUSDT':'ARB-USD','OPUSDT':'OP-USD','INJUSDT':'INJ-USD','SUIUSDT':'SUI-USD','SEIUSDT':'SEI-USD','TIAUSDT':'TIA-USD','JTOUSDT':'JTO-USD','ETCUSDT':'ETC-USD'}
COINBASE_CANDLES_URL='https://api.exchange.coinbase.com/products/{product_id}/candles'

STARTING_CAPITAL=100.0
POSITION_PCT=0.10
MAX_OPEN_POSITIONS=5
EMA_FAST=9; EMA_MID=20; EMA_TREND=50
RSI_PERIOD=14; RSI_MIN=52.0; RSI_MAX=68.0
VOLUME_LOOKBACK=20; VOLUME_MULTIPLIER=1.50
MIN_BODY_PCT=0.08
EMA50_MIN_DISTANCE_PCT=0.015  # Close must be at least 1.5% above EMA50
COOLDOWN_BARS=15
TAKE_PROFIT_PCT=0.0040
STOP_LOSS_PCT=0.0020
MAX_HOLD_BARS=15
COMMISSION_PCT_PER_SIDE=0.0005
SLIPPAGE_PCT_PER_SIDE=0.0001
POLL_SECONDS=20

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
    __tablename__='positions'; symbol:Mapped[str]=mapped_column(String(30),primary_key=True); entry_time:Mapped[str]=mapped_column(String(80)); entry_close_time_ms:Mapped[int]=mapped_column(BigInteger); raw_entry_price:Mapped[float]=mapped_column(Float); entry_price:Mapped[float]=mapped_column(Float); notional:Mapped[float]=mapped_column(Float); bars_held:Mapped[int]=mapped_column(Integer,default=0)
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
    with _db_lock, SessionLocal() as s:
        if not s.get(State,'cash'): s.add(State(key='cash',value=str(STARTING_CAPITAL)))
        if not s.get(State,'strategy_version'): s.add(State(key='strategy_version',value='v2-trend-rsi-volume-cooldown'))
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

def ema(vals,p):
    if not vals: return []
    a=2/(p+1); out=[float(vals[0])]
    for v in vals[1:]: out.append(a*float(v)+(1-a)*out[-1])
    return out

def rsi(vals,p=14):
    if len(vals)<p+1: return [50.0]*len(vals)
    out=[50.0]*len(vals); gains=[]; losses=[]
    for i in range(1,p+1):
        d=vals[i]-vals[i-1]; gains.append(max(d,0)); losses.append(max(-d,0))
    ag=sum(gains)/p; al=sum(losses)/p; out[p]=100.0 if al==0 else 100-100/(1+ag/al)
    for i in range(p+1,len(vals)):
        d=vals[i]-vals[i-1]; g=max(d,0); l=max(-d,0); ag=(ag*(p-1)+g)/p; al=(al*(p-1)+l)/p; out[i]=100.0 if al==0 else 100-100/(1+ag/al)
    return out

def signal_on_last_bar(cs):
    # EMA50 Mean Reversion V3.2
    # SHORT: price >= 1.50% above EMA50 + TWO consecutive red candles.
    # LONG: price <= 1.50% below EMA50 + TWO consecutive green candles.
    if len(cs) < 55:
        return None
    closes = [x['close'] for x in cs]
    e50 = ema(closes, 50)
    last, prev = cs[-1], cs[-2]
    upper = e50[-1] * 1.015
    lower = e50[-1] * 0.985
    two_red = prev['close'] < prev['open'] and last['close'] < last['open']
    two_green = prev['close'] > prev['open'] and last['close'] > last['open']
    if last['close'] >= upper and two_red:
        return 'SHORT'
    if last['close'] <= lower and two_green:
        return 'LONG'
    return None

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

def open_position(s,symbol,c,side):
    if s.get(Position,symbol): return
    if (s.scalar(select(func.count()).select_from(Position)) or 0)>=MAX_OPEN_POSITIONS: return
    cd=s.get(Cooldown,symbol)
    if cd and c['close_time_ms']<=cd.until_close_time_ms: return
    n=current_cash(s)*POSITION_PCT; raw=c['close']
    entry=raw*(1+SLIPPAGE_PCT_PER_SIDE) if side=='LONG' else raw*(1-SLIPPAGE_PCT_PER_SIDE)
    s.add(Position(symbol=symbol,side=side,entry_time=datetime.fromtimestamp(c['close_time_ms']/1000,tz=timezone.utc).isoformat(),entry_close_time_ms=c['close_time_ms'],raw_entry_price=raw,entry_price=entry,notional=n,bars_held=0))

def close_position(s,pos,c,raw_exit,reason):
    side=getattr(pos,'side','LONG') or 'LONG'; entry=pos.entry_price; n=pos.notional
    exitp=float(raw_exit)*(1-SLIPPAGE_PCT_PER_SIDE) if side=='LONG' else float(raw_exit)*(1+SLIPPAGE_PCT_PER_SIDE)
    gr=(exitp-entry)/entry if side=='LONG' else (entry-exitp)/entry
    gross=n*gr; fee=n*COMMISSION_PCT_PER_SIDE+max(0,n*(1+gr))*COMMISSION_PCT_PER_SIDE; net=gross-fee
    s.add(Trade(symbol=pos.symbol,entry_time=pos.entry_time,exit_time=datetime.fromtimestamp(c['close_time_ms']/1000,tz=timezone.utc).isoformat(),raw_entry_price=pos.raw_entry_price,entry_price=entry,raw_exit_price=float(raw_exit),exit_price=exitp,notional=n,gross_pl=gross,commission=fee,net_pl=net,return_pct=(net/n)*100 if n else 0,reason=side+' '+reason)); set_cash(s,current_cash(s)+net); set_cooldown(s,pos.symbol,c['close_time_ms']); s.delete(pos)

def process_symbol(symbol):
    cs=fetch_klines(symbol); last=cs[-1] if cs else None
    if not last: return
    with _db_lock, SessionLocal() as s:
        if s.get(ProcessedBar,{'symbol':symbol,'close_time_ms':last['close_time_ms']}): return
        pos=s.get(Position,symbol)
        if pos and last['close_time_ms']>pos.entry_close_time_ms:
            pos.bars_held+=1; side=getattr(pos,'side','LONG') or 'LONG'
            if side=='LONG':
                tp=pos.entry_price*(1+TAKE_PROFIT_PCT); sl=pos.entry_price*(1-STOP_LOSS_PCT)
                if last['low']<=sl: close_position(s,pos,last,sl,'SL')
                elif last['high']>=tp: close_position(s,pos,last,tp,'TP')
                elif pos.bars_held>=MAX_HOLD_BARS: close_position(s,pos,last,last['close'],'TIME')
            else:
                tp=pos.entry_price*(1-TAKE_PROFIT_PCT); sl=pos.entry_price*(1+STOP_LOSS_PCT)
                if last['high']>=sl: close_position(s,pos,last,sl,'SL')
                elif last['low']<=tp: close_position(s,pos,last,tp,'TP')
                elif pos.bars_held>=MAX_HOLD_BARS: close_position(s,pos,last,last['close'],'TIME')
        if not s.get(Position,symbol):
            side=signal_on_last_bar(cs)
            if side: open_position(s,symbol,last,side)
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

def run_backtest_30d():
    global _backtest_running
    try:
        set_state('bt_status','running'); set_state('bt_progress','0'); set_state('bt_message','Starting EMA50 Mean-Reversion V3.2 backtest...'); set_state('bt_result','')
        end_dt=datetime.now(timezone.utc).replace(second=0,microsecond=0); start_dt=end_dt-timedelta(days=15); capital=STARTING_CAPITAL; hist={s:[] for s in SYMBOLS}; pos={}; cooldown={}; lastc={}; unavailable=set(); st={'trades':0,'wins':0,'losses':0,'gross':0.0,'costs':0.0,'net':0.0,'tp_count':0,'sl_count':0,'time_count':0,'end_count':0,'tp_net':0.0,'sl_net':0.0,'time_net':0.0,'end_net':0.0}
        def close_bt(sym,c,raw,reason):
            nonlocal capital
            p=pos.pop(sym); entry=p['entry']; n=p['notional']; side=p.get('side','LONG'); exitp=float(raw)*(1-SLIPPAGE_PCT_PER_SIDE) if side=='LONG' else float(raw)*(1+SLIPPAGE_PCT_PER_SIDE); gr=(exitp-entry)/entry if side=='LONG' else (entry-exitp)/entry; gross=n*gr; fee=n*COMMISSION_PCT_PER_SIDE+max(0,n*(1+gr))*COMMISSION_PCT_PER_SIDE; net=gross-fee; capital+=net; cooldown[sym]=c['close_time_ms']+COOLDOWN_BARS*60000; st['trades']+=1; st['gross']+=gross; st['costs']+=fee; st['net']+=net
            if reason=='TP': st['tp_count']+=1; st['tp_net']+=net
            elif reason=='SL': st['sl_count']+=1; st['sl_net']+=net
            elif reason=='TIME': st['time_count']+=1; st['time_net']+=net
            else: st['end_count']+=1; st['end_net']+=net
            if net>0: st['wins']+=1
            else: st['losses']+=1
        fd=start_dt.replace(hour=0,minute=0,second=0,microsecond=0)
        for d in range(31):
            ds=fd+timedelta(days=d)
            if ds>=end_dt: break
            ev=[]
            for sym in SYMBOLS:
                if sym in unavailable: continue
                try:
                    for c in fetch_historical_day(sym,ds,start_dt,end_dt): ev.append((c['open_time_ms'],sym,c))
                except Exception as e: print('[backtest]',sym,type(e).__name__,e,flush=True)
            ev.sort(key=lambda z:(z[0],z[1]))
            for _,sym,c in ev:
                lastc[sym]=c; h=hist[sym]; h.append(c)
                if len(h)>180: del h[:-180]
                p=pos.get(sym)
                if p and c['close_time_ms']>p['t']:
                    p['bars']+=1; side=p.get('side','LONG')
                    if side=='LONG':
                        tp=p['entry']*(1+TAKE_PROFIT_PCT); sl=p['entry']*(1-STOP_LOSS_PCT)
                        if c['low']<=sl: close_bt(sym,c,sl,'SL')
                        elif c['high']>=tp: close_bt(sym,c,tp,'TP')
                        elif p['bars']>=MAX_HOLD_BARS: close_bt(sym,c,c['close'],'TIME')
                    else:
                        tp=p['entry']*(1-TAKE_PROFIT_PCT); sl=p['entry']*(1+STOP_LOSS_PCT)
                        if c['high']>=sl: close_bt(sym,c,sl,'SL')
                        elif c['low']<=tp: close_bt(sym,c,tp,'TP')
                        elif p['bars']>=MAX_HOLD_BARS: close_bt(sym,c,c['close'],'TIME')
                if sym not in pos and len(pos)<MAX_OPEN_POSITIONS and len(h)>=55 and c['close_time_ms']>cooldown.get(sym,0):
                    side=signal_on_last_bar(h)
                    if side:
                        entry=c['close']*(1+SLIPPAGE_PCT_PER_SIDE) if side=='LONG' else c['close']*(1-SLIPPAGE_PCT_PER_SIDE)
                        pos[sym]={'t':c['close_time_ms'],'entry':entry,'notional':capital*POSITION_PCT,'bars':0,'side':side}
            pct=min(99,int(((ds-start_dt).total_seconds()/(end_dt-start_dt).total_seconds())*100)+3); set_state('bt_progress',pct); set_state('bt_message',f"EMA50 V3 through {ds.date()} • trades {st['trades']} • capital ${capital:.2f}")
        for sym in list(pos):
            if sym in lastc: close_bt(sym,lastc[sym],lastc[sym]['close'],'END')
        t=st['trades']; w=st['wins']; result={'strategy':'EMA50 Mean Reversion ±1.50% + 2 reversal candles','days':30,'pairs_requested':len(SYMBOLS),'pairs_used':len(SYMBOLS)-len(unavailable),'trades':t,'wins':w,'losses':st['losses'],'win_rate':round((w/t*100) if t else 0,2),'gross_pl':round(st['gross'],4),'costs':round(st['costs'],4),'net_pl':round(st['net'],4),'starting_capital':STARTING_CAPITAL,'final_capital':round(capital,4),'return_pct':round((capital/STARTING_CAPITAL-1)*100,3),'tp_count':st['tp_count'],'sl_count':st['sl_count'],'time_count':st['time_count'],'tp_net':round(st['tp_net'],4),'sl_net':round(st['sl_net'],4),'time_net':round(st['time_net'],4),'tp_pct':round((st['tp_count']/t*100) if t else 0,2),'sl_pct':round((st['sl_count']/t*100) if t else 0,2),'time_pct':round((st['time_count']/t*100) if t else 0,2)}
        set_state('bt_result',json.dumps(result)); set_state('bt_progress','100'); set_state('bt_message','EMA50 Mean-Reversion V3.2 15-day backtest completed.'); set_state('bt_status','completed')
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
        _backtest_running=True; threading.Thread(target=run_backtest_30d,daemon=True).start()
    return redirect(url_for('dashboard'))

HTML='''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="20"><style>body{font-family:Arial;background:#10131a;color:#eee;padding:14px;max-width:1100px;margin:auto}.g{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}.c,.n,.bt{background:#1b202b;padding:13px;border-radius:12px}.l{font-size:12px;color:#aaa}.v{font-size:22px;font-weight:700}table{width:100%;border-collapse:collapse;background:#1b202b;margin-top:12px}td,th{padding:8px;border-bottom:1px solid #333;font-size:12px;text-align:left}.n,.bt{margin:14px 0}.ok{color:#78d98b}.bad{color:#ff9c9c}button{background:#fff;color:#111;border:0;border-radius:8px;padding:10px 14px;font-weight:700}.p{height:10px;background:#303746;border-radius:8px;overflow:hidden;margin:8px 0}.pb{height:100%;background:#ddd}.small{color:#aaa;font-size:12px}</style><h2>Crypto 1m Automatic Paper Bot — EMA50 Mean Reversion V3.2</h2><p>Coinbase public 1m data • No API key • No real orders • 20 pairs</p><div class=n><b>Storage:</b> {% if persistent %}<span class=ok>Persistent database connected ✅</span>{% else %}<span class=bad>Temporary SQLite ⚠️ — restart/redeploy can erase history</span>{% endif %}</div><div class=g>{% for k,v in cards %}<div class=c><div class=l>{{k}}</div><div class=v>{{v}}</div></div>{% endfor %}</div><div class=n><b>Strategy:</b> EMA50 Mean Reversion V3.2.<br><b>SHORT:</b> price ≥1.50% above EMA50 + TWO consecutive RED 1m candles.<br><b>LONG:</b> price ≥1.50% below EMA50 + TWO consecutive GREEN 1m candles.<br><b>No entry indicators:</b> EMA9, VWAP, RSI and volume removed.<br><b>Exit:</b> TP +0.40%, SL -0.20%, max hold 15 bars.<br><b>Risk:</b> max 5 open positions, 10% capital/trade.<br><b>Costs:</b> fee 0.05%/side + slippage 0.01%/side.</div><div class=bt><h3>15-Day Backtest — EMA50 V3.2</h3>{% if bt_status=='running' %}<b>Running: {{bt_progress}}%</b><div class=p><div class=pb style="width:{{bt_progress}}%"></div></div><div class=small>{{bt_message}}</div>{% else %}<form method=post action=/backtest/start><button>Run 15-Day Backtest</button></form>{% if bt_message %}<p class=small>{{bt_message}}</p>{% endif %}{% endif %}{% if bt_result %}<div class=g><div class=c><div class=l>BT Trades</div><div class=v>{{bt_result.trades}}</div></div><div class=c><div class=l>BT Win Rate</div><div class=v>{{bt_result.win_rate}}%</div></div><div class=c><div class=l>BT Net P/L</div><div class=v>$ {{bt_result.net_pl}}</div></div><div class=c><div class=l>BT Final Capital</div><div class=v>$ {{bt_result.final_capital}}</div></div><div class=c><div class=l>BT Return</div><div class=v>{{bt_result.return_pct}}%</div></div></div><h4>Exit Diagnostics</h4><table><tr><th>Exit</th><th>Count</th><th>% Trades</th><th>Net P/L</th></tr><tr><td>TP</td><td>{{bt_result.tp_count}}</td><td>{{bt_result.tp_pct}}%</td><td>$ {{bt_result.tp_net}}</td></tr><tr><td>SL</td><td>{{bt_result.sl_count}}</td><td>{{bt_result.sl_pct}}%</td><td>$ {{bt_result.sl_net}}</td></tr><tr><td>TIME</td><td>{{bt_result.time_count}}</td><td>{{bt_result.time_pct}}%</td><td>$ {{bt_result.time_net}}</td></tr></table>{% endif %}</div><h3>Open Positions</h3><table><tr><th>Pair</th><th>Entry</th><th>$ Notional</th><th>Bars</th></tr>{% for p in positions %}<tr><td>{{p.symbol}}</td><td>{{'%.8f'|format(p.entry_price)}}</td><td>{{'%.2f'|format(p.notional)}}</td><td>{{p.bars_held}}</td></tr>{% else %}<tr><td colspan=4>None</td></tr>{% endfor %}</table><h3>Latest Trades</h3><table><tr><th>Pair</th><th>Exit</th><th>P/L</th><th>Return</th></tr>{% for t in trades %}<tr><td>{{t.symbol}}</td><td>{{t.reason}}</td><td>$ {{'%.4f'|format(t.net_pl)}}</td><td>{{'%.3f'|format(t.return_pct)}}%</td></tr>{% else %}<tr><td colspan=4>No trades yet</td></tr>{% endfor %}</table><form method=post action=/reset_demo><p><button>Reset Live Demo</button></p></form>'''

@app.route('/')
def dashboard():
    ensure_worker(); d=summary_data(); cards=[('Pairs',d['symbols']),('Trades',d['trades']),('Wins',d['wins']),('Losses',d['losses']),('Win Rate',str(d['win_rate'])+'%'),('Net P/L','$ '+str(d['net'])),('Costs','$ '+str(d['costs'])),('Capital','$ '+str(d['capital'])),('Open',d['open'])]
    with SessionLocal() as s: positions=s.scalars(select(Position).order_by(Position.symbol)).all(); trades=s.scalars(select(Trade).order_by(Trade.id.desc()).limit(20)).all()
    raw=get_state('bt_result','')
    try: bt_result=json.loads(raw) if raw else None
    except Exception: bt_result=None
    return render_template_string(HTML,cards=cards,positions=positions,trades=trades,persistent=USING_PERSISTENT_DB,bt_status=get_state('bt_status','idle'),bt_progress=int(get_state('bt_progress','0') or 0),bt_message=get_state('bt_message',''),bt_result=bt_result)

init_db(); ensure_worker()
if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.getenv('PORT','8080')))
