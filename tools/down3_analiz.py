"""'3 gün üst üste düşüş' stratejisi – alım noktası analizi (işlem yapmaz).

1) Teşhis: kapanışta alınca fiyat ne kadar ve kaç gün daha düşüyor (MAE), -%2 stop yiyenlerin kaçı sonra
   kâra dönüyor.
2) Alım noktası varyantları -%2 stop / +%2 hedef (kullanıcı çerçevesi) ve doğal çıkışla test edilir.
Günlük veride aynı gün hem stop hem hedef görülürse sıra bilinmez: temkinli (stop önce) sonuç verilir,
belirsiz işlem oranı ayrıca yazılır. Portföy 5 x %20, kayma %0.05 x 2, en fazla 10 gün tutma.
"""
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import config as C  # noqa: E402
from engine import data as D  # noqa: E402
from engine import sharia  # noqa: E402
from engine.run import pick_universe  # noqa: E402
from tools.giris_arastirma import PERIODS, T_CLOSE, cagr, portfolio, prep  # noqa: E402

SLIP = 0.0005
MAXD = 10
OUT = []


def log(s=""):
    print(s)
    OUT.append(s)


def signals(d, kind):
    c, s200, atr = d["Close"].to_numpy(), d["sma200"].to_numpy(), d["atr"].to_numpy()
    dn3 = d["down3"].to_numpy()
    dn4 = dn3 & (np.r_[False, dn3[:-1]])
    drop3 = c / np.r_[np.full(3, np.nan), c[:-3]] - 1
    up = c > s200
    if kind == "dn4":
        return up & dn4
    if kind == "deep":
        return up & dn3 & (c - np.r_[np.full(3, np.nan), c[:-3]] <= -1.5 * atr)
    return up & dn3


def find_entry(d, i, how):
    """Sinyal günü i'den sonra giriş: (giriş günü, fiyat, aynı gün sonrası görülebilir mi) ya da None."""
    o, h, l, c, atr = (d[k].to_numpy() for k in ("Open", "High", "Low", "Close", "atr"))
    n = len(c)
    if how == "close":
        return i, c[i], False
    if how == "confirm_high":                     # ertesi 2 gün içinde dünkü zirve aşılırsa
        for j in (i + 1, i + 2):
            if j < n and h[j] >= h[j - 1]:
                return j, max(o[j], h[j - 1]), "confirm"
        return None
    if how == "green":                            # ilk yükselen kapanış (en fazla 3 gün bekle)
        for j in range(i + 1, min(i + 4, n)):
            if c[j] > o[j] and c[j] > c[j - 1]:
                return j, c[j], False
        return None
    if how.startswith("limit"):                   # ertesi 2 gün limitli emir
        lvl = c[i] * (1 - float(how[5:]) / 100) if not how.endswith("atr") else c[i] - 0.5 * atr[i]
        for j in (i + 1, i + 2):
            if j < n and l[j] <= lvl:
                return j, min(o[j], lvl), "limit"
        return None
    raise ValueError(how)


def run_trade(d, j0, e, same_day, stop_pct, tp_pct, natural):
    o, h, l, c = (d[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
    e *= 1 + SLIP
    stop = e * (1 - stop_pct) if stop_pct else -1
    tp = e * (1 + tp_pct) if tp_pct else np.inf
    amb = False
    n = len(c)
    if same_day == "confirm":
        # zirve aşıldıktan sonra: günün dibi büyük olasılıkla girişten önceydi; yalnız hedef ve kapanış kontrolü
        if h[j0] >= tp:
            return j0, tp, "hedef", amb
        if c[j0] <= stop:
            return j0, c[j0], "stop", amb
    start = j0 if same_day == "limit" else j0 + 1
    for j in range(start, min(j0 + 1 + MAXD, n)):
        if j > j0 and o[j] <= stop:
            return j, o[j], "stop", amb
        if j > j0 and o[j] >= tp:
            return j, o[j], "hedef", amb
        hs, ht = l[j] <= stop, h[j] >= tp
        if hs and ht:
            amb = True
            return j, stop, "stop", amb
        if hs:
            return j, stop, "stop", amb
        if ht:
            return j, tp, "hedef", amb
        if natural and j > j0 and c[j] > h[j - 1]:
            return j, c[j], "kural", amb
    j = min(j0 + MAXD, n - 1)
    return j, c[j], "süre", amb


def mae_report(data):
    """Kapanışta alım sonrası en derin düşüş ve dibin günü (doğal çıkışa kadar)."""
    rows = []
    for t, d in data.items():
        sig = signals(d, "dn3")
        o, h, l, c = (d[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
        for i in np.flatnonzero(sig):
            if i < 210 or i + 1 >= len(c):
                continue
            e = c[i]
            lows, ex = [], None
            for j in range(i + 1, min(i + 1 + MAXD, len(c))):
                lows.append(l[j] / e - 1)
                if c[j] > h[j - 1]:
                    ex = c[j] / e - 1
                    break
            if ex is None:
                ex = c[min(i + MAXD, len(c) - 1)] / e - 1
            k = int(np.argmin(lows))
            rows.append({"mae": min(lows) * 100, "low_day": k + 1, "ret": ex * 100,
                         "d1": lows[0] * 100})
    df = pd.DataFrame(rows)
    log("## 1) Teşhis – '3 gün düşüş' sinyalinde kapanışta alınca ne oluyor?")
    log(f"Sinyal sayısı: {len(df)} (tüm hisseler, örtüşmeler dahil)")
    log(f"- Alımdan sonra fiyat ortalama **%{-df.mae.mean():.1f}** daha düşüyor (medyan %{-df.mae.median():.1f})")
    log(f"- Sinyallerin **%{(df.mae <= -2).mean()*100:.0f}**'inde fiyat çıkıştan önce −%2'nin altına iniyor; "
        f"bunların **%{(df[df.mae <= -2].ret > 0).mean()*100:.0f}**'i yine de kârla kapanıyor (stop koyunca bu kâr kaybolur)")
    log(f"- Dibin görüldüğü gün: " + ", ".join(f"{k}. gün %{v*100:.0f}" for k, v in
                                              df.low_day.value_counts(normalize=True).sort_index().head(5).items()))
    log(f"- Ertesi gün fiyat alış fiyatının ortalama %{-df.d1.mean():.1f} altına iniyor; "
        f"%{(df.d1 <= -1).mean()*100:.0f}'inde en az %1 altına iniyor")
    log("")


def main():
    cands = C.CANDIDATES + (C.ADRS if C.INCLUDE_ADRS else [])
    prices = D.download_prices(cands + [C.BENCHMARK], period="10y")
    fund = D.fundamentals([t for t in cands if t in prices])
    sh = sharia.screen(fund, {t: df.tail(756) for t, df in prices.items()})
    top, _, _ = pick_universe(fund, set(prices), sh)
    spy = prices[C.BENCHMARK]["Close"]
    data = {t: prep(prices[t]) for t in top if t in prices and len(prices[t]) > 400}
    log(f"# '3 gün düşüş' alım noktası analizi ({spy.index[210]:%Y-%m} – {spy.index[-1]:%Y-%m}) · {len(data)} hisse")
    log("")
    mae_report(data)

    entries = [("Kapanışta al (mevcut)", "dn3", "close"),
               ("Teyit: ertesi gün dünkü zirve aşılınca al", "dn3", "confirm_high"),
               ("Teyit: ilk yeşil kapanışta al", "dn3", "green"),
               ("Limit: kapanışın %1 altından", "dn3", "limit1"),
               ("Limit: kapanışın %2 altından", "dn3", "limit2"),
               ("Limit: kapanış − 0.5 ATR", "dn3", "limitatr"),
               ("4 gün düşüş, kapanışta", "dn4", "close"),
               ("4 gün düşüş + ilk yeşil kapanış", "dn4", "green"),
               ("Derin düşüş (3 günde ≥1.5 ATR) + ilk yeşil kapanış", "deep", "green"),
               ("Derin düşüş + dünkü zirve aşılınca", "deep", "confirm_high")]
    exits = [("-%2 stop / +%2 hedef", 0.02, 0.02, False),
             ("-%2 stop / +%1 hedef", 0.02, 0.01, False),
             ("-%2 stop + doğal çıkış", 0.02, None, True),
             ("Stopsuz doğal çıkış", None, None, True)]
    for ename, sp, tp, nat in exits:
        log(f"## Çıkış: {ename}")
        log("| Alım noktası | İşlem | Kazanma % | Ort. % | Stop oranı % | Belirsiz % | Yıllık getiri % | Maks. düşüş % | "
            + " | ".join(p[0] for p in PERIODS) + " |")
        log("|" + "---|" * (8 + len(PERIODS)))
        for name, sk, how in entries:
            trades, amb = [], 0
            for t, d in data.items():
                sig = signals(d, sk)
                idx = d.index
                busy = -1
                for i in np.flatnonzero(sig):
                    if i < 210 or i <= busy or i + 1 >= len(d):
                        continue
                    en = find_entry(d, i, how)
                    if not en:
                        continue
                    j0, e, same = en
                    j, px, why, a = run_trade(d, j0, e, same, sp, tp, nat)
                    amb += a
                    e2 = e * (1 + SLIP)
                    trades.append({"in": idx[j0] + T_CLOSE, "out": idx[j] + T_CLOSE + pd.Timedelta(minutes=1),
                                   "ret": px * (1 - SLIP) / e2 - 1, "score": -(d["Close"].iloc[i] / d["Close"].iloc[i - 3] - 1),
                                   "why": why, "s": t})
                    busy = j
            taken, eq = portfolio(trades)
            if not taken:
                log(f"| {name} | 0 |")
                continue
            r = np.array([x["ret"] for x in taken]) * 100
            yrs = (eq.index[-1] - eq.index[0]).days / 365.25
            tot = (eq.iloc[-1] ** (1 / yrs) - 1) * 100 if eq.iloc[-1] > 0 else -100
            dd = ((eq / eq.cummax()) - 1).min() * 100
            st = np.mean([x["why"] == "stop" for x in taken]) * 100
            sub = [cagr(eq, a_, b_) for _, a_, b_ in PERIODS]
            log(f"| {name} | {len(taken)} | {(r > 0).mean()*100:.0f} | {r.mean():+.2f} | {st:.0f} | "
                f"{amb/len(trades)*100:.0f} | **{tot:+.1f}** | {dd:.1f} | "
                + " | ".join("–" if s is None else f"{s:+.1f}" for s in sub) + " |")
        log("")
    sp_ = spy.iloc[210:]
    yrs = (sp_.index[-1] - sp_.index[0]).days / 365.25
    log(f"SPY al-tut: yıllık {((sp_.iloc[-1]/sp_.iloc[0])**(1/yrs)-1)*100:+.1f}%")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("\n".join(OUT) + "\n")


if __name__ == "__main__":
    main()
