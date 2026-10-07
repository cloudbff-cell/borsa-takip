"""Puan oynaklığı ölçümü (tek seferlik analiz; işlem yapmaz, secret kullanmaz).

Teknik (40) + göreceli güç (20) puanını son ~3 yıl için her gün hesaplar ve:
  1) Günlük / 2 günlük puan değişimlerinin dağılımı
  2) Giriş benzeri günlerden (trend yukarı, puan yüksek) sonraki 10 gün içindeki en büyük puan düşüşü
  3) Puan X kadar düştükten SONRA 20 günlük getiri: düşüş gerçekten kötü haber mi?
Sonuç GitHub Actions özetine (annotation) yazılır.
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


def score_series(df, bench_close):
    c = df["Close"]
    up = (c > df["ema50"]) & (df["ema50"] > df["ema200"])
    part = ((c > df["ema50"]) | (c > df["ema200"])) & ~up
    t = np.where(up, 15, np.where(part, 7, 0)).astype(float)
    r = df["rsi"]
    t += np.where((r >= 45) & (r <= 65), 8, np.where(((r >= 35) & (r < 45)) | ((r > 65) & (r <= 72)), 4, 0))
    t += np.where(df["macd_hist"] > 0, 7, 0)
    t += np.where(c > df["ema20"], 5, 0)
    t += np.where(c >= df["res60"] * 0.95, 5, 0)
    b = bench_close.reindex(df.index).ffill()
    ex = (c / c.shift(63) - 1) - (b / b.shift(63) - 1)
    rs = np.clip((ex + 0.15) / 0.30, 0, 1) * 20
    s = pd.Series(t, index=df.index) + rs
    return s.where(df.index >= df.index[C.EMA_SLOW]), up


def pct(a, qs=(50, 75, 90, 95, 99)):
    a = np.asarray(a)
    return {f"p{q}": round(float(np.percentile(a, q)), 1) for q in qs} if len(a) else {}


def main():
    tick = C.CANDIDATES
    prices = D.download_prices(tick + [C.BENCHMARK], period="3y")
    bench = prices[C.BENCHMARK]["Close"]
    d1, d2, maxdrop10 = [], [], []
    fwd_after = {k: [] for k in (0, 10, 15, 20, 25)}   # düşüş >= k olduktan sonra 20g getiri
    fwd_base = []
    for t in tick:
        if t not in prices or len(prices[t]) < 300:
            continue
        df = add_indicators(prices[t])
        s, up = score_series(df, bench)
        s = s.dropna()
        if len(s) < 60:
            continue
        drops1 = -(s.diff().dropna())
        d1 += list(drops1[drops1 > 0])
        drops2 = -(s.diff(2).dropna())
        d2 += list(drops2[drops2 > 0])
        close = df["Close"].reindex(s.index)
        fwd = close.shift(-20) / close - 1
        # Giriş benzeri günler: yükseliş trendi ve puan >= 45/60 (≈ toplam 75+ seviyesine denk)
        entries = s.index[(s >= 45) & up.reindex(s.index).fillna(False)]
        for i in range(0, len(entries), 5):   # örtüşmeyi azaltmak için 5 günde bir örnek
            e = entries[i]
            pos = s.index.get_loc(e)
            win = s.iloc[pos:pos + 11]
            if len(win) < 11:
                continue
            dd = float(s.iloc[pos] - win.min())
            maxdrop10.append(dd)
            if not np.isnan(fwd.iloc[pos]):
                fwd_base.append(float(fwd.iloc[pos]))
            # Düşüşün ilk gerçekleştiği günden itibaren 20g getiri
            for k in fwd_after:
                hit = np.where((s.iloc[pos] - win.values) >= k)[0] if k > 0 else [0]
                if len(hit):
                    j = pos + int(hit[0])
                    if j < len(fwd) and not np.isnan(fwd.iloc[j]):
                        fwd_after[k].append(float(fwd.iloc[j]))
    res = {
        "hisse": len(tick),
        "gunluk_dusus": pct(d1), "iki_gunluk_dusus": pct(d2),
        "giris_sonrasi_10g_en_buyuk_dusus": pct(maxdrop10),
        "giris_sayisi": len(maxdrop10),
        "dusus_sonrasi_20g_getiri": {
            str(k): {"n": len(v), "ort%": round(float(np.mean(v)) * 100, 2) if v else None,
                     "pozitif%": round(float(np.mean(np.array(v) > 0)) * 100, 1) if v else None}
            for k, v in fwd_after.items()},
    }
    print("::notice title=Olcum::" + json.dumps(res, ensure_ascii=False))
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
