"""Kısa vade (2-5 gün) bölümü için 10 yıllık geriye dönük test – RSI(2) geri çekilme stratejileri.
İşlem yapmaz. Giriş/çıkış: koşul kapanışta oluşur, işlem ERTESİ GÜN AÇILIŞTA (botun gerçekçi zamanlaması).
Stop gün içinde: stop seviyesine değerse o seviyeden (açılış altındaysa açılıştan) çıkılır.
Maliyet: her işlem için %0.10 (alış-satış farkı + kayma) düşülür.
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

COST = 0.0010
VARIANTS = {
    "A RSI2<10, çıkış SMA5 üstü, stop yok, en çok 5g": dict(rsi=10, exit="sma5", stop=None, maxd=5),
    "B RSI2<10, çıkış SMA5, stop 2ATR, 5g": dict(rsi=10, exit="sma5", stop=2.0, maxd=5),
    "C RSI2<5, çıkış SMA5, stop 2ATR, 5g": dict(rsi=5, exit="sma5", stop=2.0, maxd=5),
    "D RSI2<10, çıkış RSI2>70, stop 2ATR, 5g": dict(rsi=10, exit="rsi70", stop=2.0, maxd=5),
    "E 3 gün üst üste düşüş, çıkış SMA5, stop 2ATR, 5g": dict(down3=True, exit="sma5", stop=2.0, maxd=5),
    "F RSI2<10 + güçlü profil, çıkış SMA5, stop 2ATR, 5g": dict(rsi=10, exit="sma5", stop=2.0, maxd=5, score=40),
    "G RSI2<10, çıkış SMA5, stop 3ATR, 10g": dict(rsi=10, exit="sma5", stop=3.0, maxd=10),
    "H RSI2<10, trend filtresi YOK, çıkış SMA5, stop 2ATR": dict(rsi=10, exit="sma5", stop=2.0, maxd=5, notrend=True),
}


def simulate(df, score, v):
    o, h, l, c, a = (df[k].values for k in ("Open", "High", "Low", "Close", "atr"))
    sma5 = df["Close"].rolling(5).mean().values
    sma200 = df["Close"].rolling(200).mean().values
    r2 = rsi(df["Close"], 2).values
    sc = score.values
    n, idx, out, i = len(df), df.index, [], 205
    while i < n - 2:
        p = i - 1                                   # sinyal günü (kapanış)
        trend_ok = v.get("notrend") or c[p] > sma200[p]
        if v.get("down3"):
            sig = c[p] < c[p - 1] < c[p - 2] < c[p - 3]
        else:
            sig = r2[p] < v["rsi"]
        if v.get("score") and sc[p] < v["score"]:
            sig = False
        if not (trend_ok and sig) or np.isnan(a[p]):
            i += 1
            continue
        entry = o[i]
        stop = entry - v["stop"] * a[p] if v["stop"] else None
        px, j = None, i
        for j in range(i, min(i + v["maxd"], n - 1)):
            if stop is not None and l[j] <= stop:
                px = min(o[j], stop) if j > i else stop
                break
            done = c[j] > sma5[j] if v["exit"] == "sma5" else r2[j] > 70
            if done:
                px = o[j + 1]                       # ertesi açılışta çık
                j += 1
                break
        if px is None:                              # süre doldu: ertesi açılış
            j = min(j + 1, n - 1)
            px = o[j]
        out.append({"ret": px / entry - 1 - COST, "in": idx[i], "out": idx[j], "days": j - i + 1})
        i = j + 1
    return out


def stats(tr, years):
    if not tr:
        return {"n": 0}
    r = np.array([t["ret"] for t in tr]) * 100
    s = pd.Series(r, index=[t["out"] for t in tr]).sort_index()
    yearly = s.groupby(s.index.year).sum()
    cum = s.cumsum()
    per = lambda a, b: round(float(s[(s.index >= a) & (s.index <= b)].sum()), 1)
    gains, losses = r[r > 0].sum(), -r[r <= 0].sum()
    return {"n": len(tr), "yilda": round(len(tr) / years), "win": round(float((r > 0).mean() * 100), 1),
            "ort%": round(float(r.mean()), 3), "medyan%": round(float(np.median(r)), 2),
            "kar_faktoru": round(float(gains / losses), 2) if losses else None,
            "en_kotu%": round(float(r.min()), 1), "p5%": round(float(np.percentile(r, 5)), 1),
            "ort_gun": round(float(np.mean([t["days"] for t in tr])), 1),
            "pozitif_yil": f"{int((yearly > 0).sum())}/{len(yearly)}",
            "maxDD%topl": round(float((cum.cummax() - cum).max()), 1),
            "2020": per("2020-02-15", "2020-04-15"), "2022": per("2022-01-01", "2022-12-31"),
            "2025nis": per("2025-02-15", "2025-05-15")}


def main():
    prices = D.download_prices(C.CANDIDATES + [C.BENCHMARK], period="10y")
    spy = prices[C.BENCHMARK]["Close"]
    data = []
    for t in C.CANDIDATES:
        if t in prices and len(prices[t]) > 400:
            df = add_indicators(prices[t])
            s, _ = score_series(df, spy)
            data.append((df, s.fillna(0)))
    years = (spy.index[-1] - spy.index[205]).days / 365.25
    for name, v in VARIANTS.items():
        tr = [t for d in data for t in simulate(*d, v)]
        print(f"::notice title=KV {name}::" + json.dumps(stats(tr, years), ensure_ascii=False))


if __name__ == "__main__":
    main()
