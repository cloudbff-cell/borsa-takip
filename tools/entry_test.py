"""Giriş kurallarının 10 yıllık geriye dönük testi (işlem yapmaz).

Çıkış her varyantta aynı: iz süren stop 3.5 ATR + tüm SAT çıkışları, ilk stop giriş - 2 ATR.
Puan geçmişe dönük yaklaşık hesaplanır:
  teknik (40) + göreceli güç (20) + hacim teyidi (5) + mevsimsellik bonusu (15, sadece o tarihten ÖNCEKİ yıllarla)
  + temel ve analist revizyonu için sabit varsayım K (geçmiş değerleri elimizde yok): K = 15 + 5 = 20
Sınır: temel/revizyon farklarını yakalamaz; hisse listesi bugünün büyükleri (sonuçları iyimser gösterir).
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import config as C  # noqa: E402
from engine import data as D  # noqa: E402
from engine.indicators import add_indicators  # noqa: E402
from engine.signals import compute_signals  # noqa: E402
from tools.olcum import score_series  # noqa: E402

K = 20.0
TRAIL = 3.5
EXIT = ["S_BREAKDOWN", "S_TREND", "S_DEATH"]
VARIANTS = {
    "ESKİ kural (her sinyal + güçlü trend ≥65)": dict(old=True),
    "YENİ 70/80": dict(sig=70, trend=80),
    "75/85": dict(sig=75, trend=85),
    "65/75": dict(sig=65, trend=75),
    "60/70": dict(sig=60, trend=70),
    "Sadece sinyal ≥70 (trend girişi yok)": dict(sig=70, trend=None),
    "Sadece trend ≥80 (sinyal girişi yok)": dict(sig=None, trend=80),
    "Sinyal (puan şartsız) + trend ≥80": dict(sig=0, trend=80),
}


def volume_score(df):
    ch = df["Close"].diff()
    v = df["Volume"]
    up = v.where(ch > 0, 0).rolling(50).sum()
    dn = v.where(ch < 0, 0).rolling(50).sum()
    ratio = up / dn.replace(0, np.nan)
    return (np.clip((ratio - 0.8) / 0.7, 0, 1) * 5).fillna(2.5)


def season_bonus(df, spy):
    m = df["Close"].resample("ME").last().pct_change()
    ms = spy.resample("ME").last().pct_change().reindex(m.index)
    out = pd.Series(0.0, index=m.index)
    for d in m.index:
        prev = m[(m.index.month == d.month) & (m.index.year < d.year)].dropna()
        if len(prev) < 4:
            continue
        prev = prev.tail(10)
        ex = float((prev - ms.reindex(prev.index)).mean())
        win = float((prev > 0).mean() * 100)
        out[d] = np.clip((win - 50) / 30, 0, 1) * 10 + np.clip(ex * 100 / 2, 0, 1) * 5
    # günlük seriye: o ayın bonusu ay boyunca geçerli
    key = df.index.to_period("M")
    mp = pd.Series(out.values, index=out.index.to_period("M"))
    return pd.Series(mp.reindex(key).values, index=df.index).fillna(0)


def simulate(df, sig, total, old_total, up, v):
    o, h, l, c, a, e20 = (df[k].values for k in ("Open", "High", "Low", "Close", "atr", "ema20"))
    idx = df.index
    buy = sig[[k for k in sig.columns if k.startswith("B_")]].any(axis=1).values
    rec_buy = pd.Series(buy).rolling(3, min_periods=1).max().astype(bool).values   # son 3 günde AL
    sellsig = sig[EXIT].any(axis=1).values
    rec_sell = pd.Series(sellsig).rolling(3, min_periods=1).max().astype(bool).values
    rsi = df["rsi"].values
    T, OT, upv = total.values, old_total.values, up.values
    n, trades, i = len(df), [], C.EMA_SLOW + 60
    while i < n - 1:
        p = i - 1
        entry, kind = None, None
        ok = not rec_sell[p] and rsi[p] < 75
        zone = e20[p] + 0.5 * a[p]
        if v.get("old"):
            if buy[p]:
                entry, kind = o[i], "sinyal"
            elif upv[p] and OT[p] >= 65 and l[i] <= zone:
                entry, kind = min(o[i], zone), "trend"
        elif ok:
            if v["sig"] is not None and rec_buy[p] and T[p] >= v["sig"]:
                entry, kind = o[i], "sinyal"
            elif v["trend"] is not None and upv[p] and T[p] >= v["trend"] and l[i] <= zone:
                entry, kind = min(o[i], zone), "trend"
        if entry is None or np.isnan(a[p]):
            i += 1
            continue
        stop0 = entry - 2 * a[p]
        stop, hi, exit_px, j = stop0, entry, None, i
        for j in range(i, min(i + 250, n)):
            if l[j] <= stop:
                exit_px = min(o[j], stop) if j > i else stop
                break
            hi = max(hi, h[j])
            stop = max(stop, hi - TRAIL * a[j])
            if sellsig[j]:
                exit_px = c[j]
                break
        if exit_px is None:
            exit_px = c[j]
        trades.append({"R": (exit_px - entry) / (entry - stop0), "ret": exit_px / entry - 1,
                       "in": idx[i], "out": idx[j], "kind": kind})
        i = j + 1
    return trades


def summary(tr, years):
    if not tr:
        return {"n": 0}
    R = np.array([t["R"] for t in tr]); r = np.array([t["ret"] for t in tr]) * 100
    s = pd.Series(R, index=[t["out"] for t in tr]).sort_index().cumsum()
    per = lambda a, b: round(float(sum(t["R"] for t in tr if pd.Timestamp(a) <= t["out"] <= pd.Timestamp(b))), 1)
    return {"n": len(tr), "yilda": round(len(tr) / years), "sinyal%": round(100 * np.mean([t["kind"] == "sinyal" for t in tr])),
            "win": round(float((r > 0).mean() * 100), 1), "avg%": round(float(r.mean()), 2), "R": round(float(R.mean()), 2),
            "topR": round(float(R.sum())), "DD": round(float((s.cummax() - s).max()), 1),
            "2020": per("2020-02-15", "2020-04-15"), "2022": per("2022-01-01", "2022-12-31")}


def main():
    prices = D.download_prices(C.CANDIDATES + [C.BENCHMARK], period="10y")
    spy = prices[C.BENCHMARK]["Close"]
    data = []
    for t in C.CANDIDATES:
        if t not in prices or len(prices[t]) < 400:
            continue
        df = add_indicators(prices[t])
        tr_score, up = score_series(df, spy)
        bonus = season_bonus(df, spy)
        total = tr_score.fillna(0) + volume_score(df) + K + bonus
        old_total = tr_score.fillna(0) + 15 + bonus              # eski ölçek: teknik+göreceli+temel(varsayım 15)+mevsim
        data.append((df, compute_signals(df), total, old_total, up.fillna(False)))
    years = (spy.index[-1] - spy.index[260]).days / 365.25
    for name, v in VARIANTS.items():
        tr = [t for d in data for t in simulate(*d, v)]
        print(f"::notice title=GT {name}::" + json.dumps(summary(tr, years), ensure_ascii=False))


if __name__ == "__main__":
    main()
