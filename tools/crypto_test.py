"""BTC/ETH strateji testi (işlem yapmaz). Günlük bar, 7/24 piyasa (yılda 365 gün).
Kural seti (hisse orta vade kurallarının kripto uyarlaması):
  Giriş: yükseliş trendi (kapanış > EMA50 > EMA200) ve [son 3 günde AL sinyali ya da 20 EMA'ya geri çekilme]
  Çıkış: ilk stop 2 ATR, iz süren stop K ATR, kapanışta teyitli SAT sinyalleri
Bölüm: 10.000 $; BTC ve ETH'ye en fazla yarı yarıya; maliyet işlem başına %0.25 (Alpaca kripto ücreti + kayma, temkinli).
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import data as D  # noqa: E402
from engine.indicators import add_indicators  # noqa: E402
from engine.signals import compute_signals  # noqa: E402

COST = 0.0025
EXIT = ["S_BREAKDOWN", "S_TREND", "S_DEATH"]
VARIANTS = {
    "T3": dict(trail=3.0, sat=True),        # trend kuralları, iz süren 3 ATR, SAT çıkışları (hisselerle aynı)
    "T4": dict(trail=4.0, sat=True),        # iz süren 4 ATR
    "T3-SATyok": dict(trail=3.0, sat=False),
    "SMA200": dict(simple=True),            # basit: kapanış 200 günlük ortalamanın üstündeyse tut
}


def sim_one(df, v):
    sig = compute_signals(df)
    o, h, l, c, a = (df[k].values for k in ("Open", "High", "Low", "Close", "atr"))
    e20, e50, e200 = (df[k].values for k in ("ema20", "ema50", "ema200"))
    buy = sig[[k for k in sig.columns if k.startswith("B_")]].any(axis=1).rolling(3, min_periods=1).max().astype(bool).values
    sell = sig[EXIT].any(axis=1).values
    n = len(df)
    ret = np.zeros(n)          # günlük getiri (pozisyondayken), maliyetler dahil
    inpos, trades, stop, hi, entry, exit_next = False, [], 0, 0, 0, False
    for i in range(201, n):
        p = i - 1
        if inpos and exit_next:                       # önceki kapanışta SAT: bugün açılışta çık
            ret[i] = o[i] / c[p] - 1 - COST
            trades.append(o[i] / entry - 1 - 2 * COST)
            inpos, exit_next = False, False
            continue
        if not inpos:
            if v.get("simple"):
                if c[p] > df["Close"].rolling(200).mean().values[p]:
                    inpos, entry = True, o[i]
                    ret[i] = c[i] / o[i] - 1 - COST
                continue
            up = c[p] > e50[p] > e200[p]
            zone = e20[p] + 0.5 * a[p]
            px = None
            if up and buy[p]:
                px = o[i]
            elif up and l[i] <= zone:
                px = min(o[i], zone)
            if px is None:
                continue
            inpos, entry, stop, hi = True, px, px - 2 * a[p], px
            if l[i] <= stop:
                ret[i] = stop / px - 1 - 2 * COST; trades.append(stop / px - 1 - 2 * COST); inpos = False
                continue
            ret[i] = c[i] / px - 1 - COST
            continue
        # pozisyondayken
        if v.get("simple"):
            if c[p] < df["Close"].rolling(200).mean().values[p]:
                ret[i] = o[i] / c[p] - 1 - COST; trades.append(o[i] / entry - 1 - 2 * COST); inpos = False
            else:
                ret[i] = c[i] / c[p] - 1
            continue
        if l[i] <= stop:
            x = min(o[i], stop)
            ret[i] = x / c[p] - 1 - COST; trades.append(x / entry - 1 - 2 * COST); inpos = False
            continue
        ret[i] = c[i] / c[p] - 1
        hi = max(hi, h[i])
        stop = max(stop, hi - v["trail"] * a[i])
        if v["sat"] and sell[i]:
            exit_next = True
    return pd.Series(ret, index=df.index), trades


def stats(r, trades):
    eq = (1 + r).cumprod() * 10_000
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    yr = eq.resample("YE").last().pct_change().dropna()
    return {"son": round(float(eq.iloc[-1])), "yillik%": round(((eq.iloc[-1] / 10_000) ** (1 / yrs) - 1) * 100, 1),
            "maxDD%": round(float((eq / eq.cummax() - 1).min()) * 100, 1),
            "sharpe": round(float(r.mean() / r.std() * np.sqrt(365)), 2) if r.std() else None,
            "piyasada%": round(float((r != 0).mean() * 100)), "islem": len(trades),
            "win%": round(float(np.mean(np.array(trades) > 0) * 100), 1) if trades else None,
            "en_kotu_yil%": round(float(yr.min()) * 100, 1) if len(yr) else None,
            "2018%": round(float(eq["2018"].iloc[-1] / eq[:"2017"].iloc[-1] - 1) * 100, 1) if len(eq[:"2017"]) else None,
            "2022%": round(float(eq["2022"].iloc[-1] / eq[:"2021"].iloc[-1] - 1) * 100, 1)}


def main():
    prices = D.download_prices(["BTC-USD", "ETH-USD"], period="10y")
    dfs = {t: add_indicators(prices[t]) for t in ("BTC-USD", "ETH-USD") if t in prices}
    idx = dfs["BTC-USD"].index
    for name, v in VARIANTS.items():
        rs, tr = [], []
        for t, df in dfs.items():
            r, trades = sim_one(df, v)
            rs.append(r.reindex(idx).fillna(0))
            tr += trades
            if name == "T3":
                print(f"::notice title=KR {t} ana kural::" + json.dumps(stats(r, trades), ensure_ascii=False))
        port = sum(rs) / len(rs)       # yarı yarıya
        print(f"::notice title=KR 50/50 {name}::" + json.dumps(stats(port, tr), ensure_ascii=False))
    for t, df in dfs.items():
        r = df["Close"].pct_change().fillna(0)
        print(f"::notice title=KR Al-tut {t}::" + json.dumps(stats(r, []), ensure_ascii=False))


if __name__ == "__main__":
    main()
