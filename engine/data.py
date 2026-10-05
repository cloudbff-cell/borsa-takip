"""Yahoo Finance veri katmanı (yfinance). Tüm ağ çağrıları bu dosyada."""
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import config as C

CACHE = Path("cache")
CACHE.mkdir(exist_ok=True)


def _yf():
    import yfinance as yf  # GitHub Actions'ta kurulur
    return yf


def _clean(v):
    if v is None:
        return None
    try:
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
    except Exception:
        pass
    return v


def download_prices(tickers, period="3y", interval="1d") -> dict:
    yf = _yf()
    out = {}
    for i in range(0, len(tickers), 40):
        chunk = tickers[i:i + 40]
        raw = yf.download(chunk, period=period, interval=interval, group_by="ticker",
                          auto_adjust=True, threads=True, progress=False)
        for t in chunk:
            try:
                df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = df.dropna(subset=["Close"])
                if len(df) > 30:
                    df.index = pd.to_datetime(df.index).tz_localize(None)
                    out[t] = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
            except Exception:
                pass
    return out


def _fetch_fundamentals(t: str) -> dict:
    yf = _yf()
    tk = yf.Ticker(t)
    d = {"ticker": t}
    try:
        info = tk.info or {}
    except Exception:
        info = {}
    for k in ["shortName", "sector", "industry", "marketCap", "trailingPE", "forwardPE", "pegRatio",
              "revenueGrowth", "earningsGrowth", "profitMargins", "returnOnEquity", "debtToEquity",
              "freeCashflow", "targetMeanPrice", "targetHighPrice", "targetLowPrice",
              "recommendationMean", "recommendationKey", "numberOfAnalystOpinions", "currentPrice"]:
        d[k] = _clean(info.get(k))
    if not d.get("marketCap"):
        try:
            d["marketCap"] = _clean(float(tk.fast_info["marketCap"]))
        except Exception:
            pass
    # Bir sonraki bilanço tarihi ve beklentiler
    try:
        cal = tk.calendar or {}
        ed = cal.get("Earnings Date")
        if ed:
            d["nextEarnings"] = str(ed[0] if isinstance(ed, (list, tuple)) else ed)[:10]
        for src, dst in [("Earnings Average", "epsEstNext"), ("Revenue Average", "revEstNext"),
                         ("Earnings High", "epsEstHigh"), ("Earnings Low", "epsEstLow")]:
            if cal.get(src) is not None:
                d[dst] = _clean(float(cal[src]))
    except Exception:
        pass
    # Son çeyrekler: beklenti vs gerçekleşen
    try:
        ed = tk.get_earnings_dates(limit=12)
        hist = []
        if ed is not None and len(ed):
            ed = ed.sort_index(ascending=False)
            for ts, r in ed.iterrows():
                rep = _clean(r.get("Reported EPS"))
                est = _clean(r.get("EPS Estimate"))
                day = str(ts)[:10]
                if rep is None:
                    if est is not None and day >= datetime.now().strftime("%Y-%m-%d"):
                        d.setdefault("nextEarnings", day)
                        d.setdefault("epsEstNext", float(est))
                    continue
                hist.append({"date": day, "est": est, "actual": float(rep),
                             "surprise": _clean(r.get("Surprise(%)"))})
        d["earningsHistory"] = hist[:6]
    except Exception:
        d["earningsHistory"] = []
    return d


def fundamentals(tickers, force=False) -> dict:
    path = CACHE / "fundamentals.json"
    cache = json.loads(path.read_text()) if path.exists() else {}
    now = time.time()
    for t in tickers:
        c = cache.get(t)
        if not force and c and now - c.get("_ts", 0) < C.FUNDAMENTALS_TTL_HOURS * 3600:
            continue
        try:
            f = _fetch_fundamentals(t)
            f["_ts"] = now
            cache[t] = f
        except Exception as e:
            print(f"[temel veri] {t}: {e}")
        time.sleep(0.3)
    path.write_text(json.dumps(cache, default=str))
    return {t: cache[t] for t in tickers if t in cache}


def load_state() -> dict:
    p = CACHE / "state.json"
    return json.loads(p.read_text()) if p.exists() else {"sent": {}}


def save_state(s: dict):
    # 10 günden eski kayıtları temizle
    cutoff = (datetime.now(timezone.utc) - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    s["sent"] = {k: v for k, v in s.get("sent", {}).items() if v >= cutoff}
    (CACHE / "state.json").write_text(json.dumps(s))
