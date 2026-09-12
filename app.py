import csv
import io
import json
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import Flask, Response

app = Flask(__name__)

SYMBOL = "ADAUSDT"
PRODUCT_ID = "ADA-USD"
COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/{product_id}/candles"

def fetch_chunk(start_dt, end_dt):
    params = {
        "granularity": 60,
        "start": start_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "end": end_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    url = COINBASE_CANDLES_URL.format(product_id=PRODUCT_ID) + "?" + urlencode(params)
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 crypto-data-exporter",
            "Accept": "application/json",
        },
    )
    with urlopen(req, timeout=20) as r:
        raw = json.loads(r.read().decode("utf-8"))

    rows = {}
    if isinstance(raw, list):
        for k in raw:
            ts = int(k[0])
            rows[ts] = {
                "timestamp": ts,
                "datetime_utc": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                "open": float(k[3]),
                "high": float(k[2]),
                "low": float(k[1]),
                "close": float(k[4]),
                "volume": float(k[5]),
            }
    return [rows[k] for k in sorted(rows)]

def fetch_60_days():
    end_dt = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start_dt = end_dt - timedelta(days=60)

    all_rows = {}
    cur = start_dt

    # Coinbase allows max ~300 one-minute candles per request.
    while cur < end_dt:
        chunk_end = min(cur + timedelta(minutes=299), end_dt)
        try:
            for row in fetch_chunk(cur, chunk_end):
                ts = row["timestamp"]
                if start_dt.timestamp() <= ts < end_dt.timestamp():
                    all_rows[ts] = row
        except Exception as e:
            print("chunk error:", cur, chunk_end, repr(e), flush=True)
            time.sleep(1.0)
            # one retry
            for row in fetch_chunk(cur, chunk_end):
                ts = row["timestamp"]
                if start_dt.timestamp() <= ts < end_dt.timestamp():
                    all_rows[ts] = row

        cur = chunk_end + timedelta(minutes=1)
        time.sleep(0.16)

    return [all_rows[k] for k in sorted(all_rows)], start_dt, end_dt

@app.get("/")
def home():
    return """
    <h2>ADAUSDT 60-Day 1-Minute CSV Exporter</h2>
    <p>Coinbase public ADA-USD candles are exported as ADAUSDT-style data for strategy testing.</p>
    <p><a href="/export">Download ADAUSDT_60day_1m.csv</a></p>
    """

@app.get("/export")
def export_csv():
    rows, start_dt, end_dt = fetch_60_days()

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["timestamp", "datetime_utc", "open", "high", "low", "close", "volume"])
    for r in rows:
        w.writerow([
            r["timestamp"],
            r["datetime_utc"],
            r["open"],
            r["high"],
            r["low"],
            r["close"],
            r["volume"],
        ])

    filename = "ADAUSDT_60day_1m.csv"
    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Data-Start": start_dt.isoformat(),
            "X-Data-End": end_dt.isoformat(),
            "X-Row-Count": str(len(rows)),
        },
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
