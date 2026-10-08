"""Kısa vade bölümü – gerçek bütçeyle portföy simülasyonu (10 yıl, işlem yapmaz).
Bölüm bütçesi 25.000 $ (100.000 $'ın %25'i), en fazla 3 pozisyon, her pozisyon bölüm değerinin 1/3'ü.
Sinyal kapanışta, alım ertesi açılışta; aynı anda birden fazla aday varsa en düşük RSI(2) önce.
Çıkış: gün içinde stop (2 ATR) | kapanış 5 günlük ortalamanın üstünde -> ertesi açılış | en çok 5 gün.
Maliyet: alışta ve satışta %0.05 (toplam %0.10).
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import config as C  # noqa: E402
from engine import data as D  # noqa: E402
from engine.indicators import add_indicators, rsi  # noqa: E402
from tools.olcum import score_series  # noqa: E402

START_CASH, SLOTS, SIDE_COST = 25_000.0, 3, 0.0005
VARIANTS = {
    "1 RSI2<5 + profil": dict(rsi=5, score=40, guard=None),
    "2 RSI2<5 + profil + SPY>200g": dict(rsi=5, score=40, guard="sma200"),
    "3 RSI2<5 + profil + SPY tepeden <%10 düşük": dict(rsi=5, score=40, guard="dd10"),
    "4 RSI2<10 + profil + SPY>200g": dict(rsi=10, score=40, guard="sma200"),
    "5 RSI2<5 profilsiz + SPY>200g": dict(rsi=5, score=None, guard="sma200"),
    "6 RSI2<5 + profil + SPY>200g + 2 pozisyon": dict(rsi=5, score=40, guard="sma200", slots=2),
}


def prep():
    prices = D.download_prices(C.CANDIDATES + [C.BENCHMARK], period="10y")
    spy = prices[C.BENCHMARK]
    dates = spy.index
    F = {k: {} for k in ("o", "h", "l", "c", "atr", "sma5", "sma200", "r2", "sc")}
    for t in C.CANDIDATES:
        if t not in prices or len(prices[t]) < 400:
            continue
        df = add_indicators(prices[t]).reindex(dates)
        s, _ = score_series(df.dropna(subset=["Close"]), spy["Close"])
        F["o"][t], F["h"][t], F["l"][t], F["c"][t] = df["Open"], df["High"], df["Low"], df["Close"]
        F["atr"][t] = df["atr"]
        F["sma5"][t] = df["Close"].rolling(5).mean()
        F["sma200"][t] = df["Close"].rolling(200).mean()
        F["r2"][t] = rsi(df["Close"].ffill(), 2)
        F["sc"][t] = s.reindex(dates)
    F = {k: pd.DataFrame(v) for k, v in F.items()}
    sc = spy["Close"]
    guards = {"sma200": sc > sc.rolling(200).mean(), "dd10": sc >= sc.cummax() * 0.90}
    return dates, F, guards, sc


def run(dates, F, guards, v):
    slots = v.get("slots", SLOTS)
    cash, pos, eq, trades = START_CASH, {}, [], []
    o, h, l, c = F["o"], F["h"], F["l"], F["c"]
    pending_exit = set()
    for k in range(210, len(dates)):
        d, prev = dates[k], dates[k - 1]
        # 1) Açılışta: önceki kapanışta çıkış kararı verilenleri sat
        for t in list(pending_exit):
            p = pos.pop(t)
            px = o.at[d, t] if not np.isnan(o.at[d, t]) else c.at[prev, t]
            cash += p["n"] * px * (1 - SIDE_COST)
            trades.append({"ret": px / p["px"] - 1 - 2 * SIDE_COST, "out": d})
        pending_exit.clear()
        # 2) Açılışta: yeni alımlar (önceki kapanış sinyalleri)
        free = slots - len(pos)
        if free > 0 and (v["guard"] is None or bool(guards[v["guard"]].iat[k - 1])):
            r2, cl = F["r2"].loc[prev], c.loc[prev]
            ok = (r2 < v["rsi"]) & (cl > F["sma200"].loc[prev]) & F["atr"].loc[prev].notna()
            if v.get("score"):
                ok &= F["sc"].loc[prev] >= v["score"]
            cands = r2[ok].sort_values().index
            equity = cash + sum(p["n"] * c.at[prev, t] for t, p in pos.items())
            for t in cands:
                if free <= 0:
                    break
                if t in pos or np.isnan(o.at[d, t]):
                    continue
                px = o.at[d, t]
                alloc = min(equity / slots, cash)
                n = alloc / (px * (1 + SIDE_COST))
                if n <= 0:
                    continue
                cash -= n * px * (1 + SIDE_COST)
                pos[t] = {"n": n, "px": px, "stop": px - 2 * F["atr"].at[prev, t], "day": k}
                free -= 1
        # 3) Gün içi stop, kapanışta çıkış kararı
        for t in list(pos):
            p = pos[t]
            if not np.isnan(l.at[d, t]) and l.at[d, t] <= p["stop"]:
                px = min(o.at[d, t], p["stop"]) if k > p["day"] else p["stop"]
                cash += p["n"] * px * (1 - SIDE_COST)
                trades.append({"ret": px / p["px"] - 1 - 2 * SIDE_COST, "out": d})
                del pos[t]
                continue
            if c.at[d, t] > F["sma5"].at[d, t] or k - p["day"] >= 4:
                pending_exit.add(t)
        eq.append(cash + sum(p["n"] * (c.at[d, t] if not np.isnan(c.at[d, t]) else p["px"]) for t, p in pos.items()))
    return pd.Series(eq, index=dates[210:]), trades


def report(eq, trades, spy):
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1
    dd = (eq / eq.cummax() - 1).min()
    yr = eq.resample("YE").last().pct_change()
    yr.iloc[0] = eq.resample("YE").last().iloc[0] / eq.iloc[0] - 1
    r = np.array([t["ret"] for t in trades]) * 100
    seg = lambda a, b: round(float(eq[a:b].iloc[-1] / eq[:a].iloc[-1] - 1) * 100, 1) if len(eq[a:b]) else None
    worst = lambda a, b: round(float((eq[a:b] / eq[:b].cummax()[a:b] - 1).min()) * 100, 1)
    dret = eq.pct_change().dropna()
    return {"son_deger": round(float(eq.iloc[-1])), "yillik%": round(cagr * 100, 1), "maxDD%": round(dd * 100, 1),
            "sharpe": round(float(dret.mean() / dret.std() * np.sqrt(252)), 2), "islem": len(trades),
            "yilda": round(len(trades) / yrs), "win%": round(float((r > 0).mean() * 100), 1),
            "en_kotu_yil%": round(float(yr.min()) * 100, 1), "en_iyi_yil%": round(float(yr.max()) * 100, 1),
            "pozitif_yil": f"{int((yr > 0).sum())}/{len(yr)}",
            "2020_cokus%": seg("2020-02-19", "2020-04-15"), "2022%": seg("2022-01-01", "2022-12-31"),
            "2025nis%": seg("2025-02-15", "2025-05-15")}


def main():
    dates, F, guards, spy = prep()
    for name, v in VARIANTS.items():
        eq, tr = run(dates, F, guards, v)
        print(f"::notice title=KP {name}::" + json.dumps(report(eq, tr, spy), ensure_ascii=False))
    s = spy[dates[210]:]
    b = (s / s.iloc[0]) * START_CASH
    print("::notice title=KP Karşılaştırma: SPY al-tut::" + json.dumps(report(b, [{"ret": 0}], spy), ensure_ascii=False))


if __name__ == "__main__":
    main()
