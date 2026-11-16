"""'3 gün düşüş' sonrası: satışlar durup yataya girince al – 5 dk veriyle kesin test (işlem yapmaz).

Sinyal: İslami filtreden geçen ilk 50, 200 gün ort. üstü, 3 gün üst üste düşük kapanış. Sonraki 3 seans
içinde alış şekilleri:
  Kapanışta      : sinyal günü kapanışında (karşılaştırma)
  Yeni dip yok   : önceki günün dibinin altına inmeyen ilk günün kapanışında
  + hacim kurudu : aynı, ayrıca o günün hacmi 20 gün ort. altında ve gün aralığı < 1 ATR
  Gün içi yatay N: seansın en az N dk'sı geçmiş, son N dk'da yeni seans dibi yok ve son N dk'nın
                   aralığı < 0.4 günlük ATR -> sonraki 5 dk mumun açılışında al
Çıkışlar: -%2/+%2 · yatay dibin %0.2 altında stop / +%2 · stopsuz 10 seans · -%8 stop + 10 seans.
5 dk mumlarla mum mum; aynı mumda stop+hedef = stop; giriş mumunda hedef sayılmaz. Kayma %0.05 x 2.
Portföy 3 pozisyon. Veri: Alpaca SIP 5 dk son 3 yıl.
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
from tools import down3_analiz as A  # noqa: E402
from tools.getiri_arttirma import simulate  # noqa: E402
from tools.giris_arastirma import prep  # noqa: E402
from tools.kademeli_cikis_test import load_intraday  # noqa: E402

SLIP = 0.0005
OUT = []


def log(s=""):
    print(s)
    OUT.append(s)


def find_entry(how, d, i, b, bday, o5, h5, l5, c5):
    """-> (giriş mumu k, fiyat, 'close'|'open', yatay dip) ya da None. Fiyatlar 5 dk ölçeğinde."""
    dates = d.index
    dl, dh, dv = d["Low"].to_numpy(), d["High"].to_numpy(), d["Volume"].to_numpy()
    atr = d["atr"].to_numpy()
    vavg = pd.Series(dv).rolling(20).mean().shift(1).to_numpy()

    def day_slice(day):
        a = np.searchsorted(bday, day)
        z = np.searchsorted(bday, day + pd.Timedelta(days=1))
        return (a, z) if z > a and bday[a] == day else None

    sl = day_slice(dates[i])
    if not sl:
        return None
    scale = c5[sl[1] - 1] / d["Close"].iloc[i]
    if how == "close":
        return sl[1] - 1, c5[sl[1] - 1], "close", l5[sl[0]:sl[1]].min()
    for n in range(1, 4):
        if i + n >= len(d):
            return None
        sl = day_slice(dates[i + n])
        if not sl:
            return None
        a, z = sl
        if how in ("nolow", "nolow_vol"):
            if dl[i + n] >= dl[i + n - 1]:
                if how == "nolow_vol" and not (dv[i + n] < vavg[i + n] and dh[i + n] - dl[i + n] < atr[i + n - 1]):
                    continue
                base = min(dl[i + n - 1], dl[i + n]) * scale
                return z - 1, c5[z - 1], "close", base
        elif how.startswith("intra"):
            m = int(how[5:]) // 5
            run_low = np.minimum.accumulate(l5[a:z])
            for j in range(a + m, z - 1):
                win_lo, win_hi = l5[j - m + 1:j + 1].min(), h5[j - m + 1:j + 1].max()
                no_new_low = win_lo > run_low[j - a - m]                   # son m mumda seans dibi yenilenmedi
                if no_new_low and (win_hi - win_lo) < 0.4 * atr[i + n - 1] * scale:
                    return j + 1, o5[j + 1], "open", win_lo
    return None


def run(data, intra, how, exit_kind):
    trades = []
    for t, d in data.items():
        b = intra.get(t)
        if b is None:
            continue
        bday = b.index.normalize().tz_localize(None)
        o5, h5, l5, c5 = (b[k].to_numpy() for k in "ohlc")
        dates = d.index
        sig = A.signals(d, "dn3")
        busy = None
        for i in np.flatnonzero(sig):
            day = dates[i]
            if i < 210 or day < bday[0] or (busy is not None and day <= busy):
                continue
            en = find_entry(how, d, i, b, bday, o5, h5, l5, c5)
            if not en:
                continue
            k, e, kind, base = en
            e *= 1 + SLIP
            if exit_kind == "2/2":
                stop, tp = e * 0.98, e * 1.02
            elif exit_kind == "base":
                stop, tp = min(base * 0.998, e * 0.999), e * 1.02
            elif exit_kind == "none":
                stop, tp = 0.0, np.inf
            else:   # "-8"
                stop, tp = e * 0.92, np.inf
            entry_day = bday[k]
            di = dates.searchsorted(entry_day)
            exit_day = dates[min(di + 10, len(dates) - 1)]
            end = min(np.searchsorted(bday, exit_day + pd.Timedelta(days=1)), len(b))
            start = k + 1 if kind == "close" else k
            ex, why, j = None, "süre", start
            for j in range(start, end):
                if j > k and o5[j] <= stop:
                    ex, why = o5[j], "stop"
                    break
                if l5[j] <= stop:
                    ex, why = stop, "stop"
                    break
                if j > k and h5[j] >= tp:
                    ex, why = max(o5[j], tp), "hedef"
                    break
            if ex is None:
                j = max(end - 1, k)
                ex = c5[j]
            ex *= 1 - SLIP
            busy = bday[j]
            trades.append({"in": entry_day, "out": bday[j], "ret": ex / e - 1, "why": why,
                           "score": -(d["Close"].iloc[i] / d["Close"].iloc[i - 3] - 1), "s": t,
                           "risk": (e - stop) / e if stop else None})
    return trades


def main():
    cands = C.CANDIDATES + (C.ADRS if C.INCLUDE_ADRS else [])
    prices = D.download_prices(cands + [C.BENCHMARK, "SPUS"], period="10y")
    fund = D.fundamentals([t for t in cands if t in prices])
    sh = sharia.screen(fund, {t: df.tail(756) for t, df in prices.items()})
    top, _, _ = pick_universe(fund, set(prices), sh)
    end = pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=20)
    start = end - pd.DateOffset(years=3)
    intra = load_intraday(top, start, end)
    data = {t: prep(prices[t]) for t in top if t in prices and len(prices[t]) > 400}
    s0 = start.tz_convert(None).normalize()
    etf = prices["SPUS"]["Close"].pct_change().loc[s0:].fillna(0)
    spy_r = prices[C.BENCHMARK]["Close"].pct_change().loc[s0:].fillna(0)
    yrs = (etf.index[-1] - etf.index[0]).days / 365.25
    cg = lambda eq: (eq.iloc[-1] ** (1 / yrs) - 1) * 100
    dd = lambda eq: ((eq / eq.cummax()) - 1).min() * 100
    log(f"## Satış durup yataya girince alış – 5 dk kesin test ({etf.index[0]:%Y-%m} – {etf.index[-1]:%Y-%m})")
    log(f"SPY al-tut yıllık {cg((1 + spy_r).cumprod()):+.1f}% (düşüş {dd((1 + spy_r).cumprod()):.1f}%)")
    log("")
    log("| Alış | Çıkış | İşlem | Kazanma % | Ort. % | Stop % | Ort. stop mesafesi % | Yıllık % | Düşüş % | Yıllık, boşta nakit SPUS'ta % |")
    log("|---|---|---|---|---|---|---|---|---|---|")
    entries = [("close", "Kapanışta (karşılaştırma)"), ("nolow", "Yeni dip yok (günlük)"),
               ("nolow_vol", "Yeni dip yok + hacim kurudu"), ("intra60", "Gün içi 60 dk yatay"),
               ("intra90", "Gün içi 90 dk yatay")]
    exits = [("2/2", "−%2 / +%2"), ("base", "yatay dip altı stop / +%2"), ("-8", "−%8 stop, 10 seans"),
             ("none", "stopsuz, 10 seans")]
    for how, hn in entries:
        for ek, en in exits:
            if how == "close" and ek == "base":
                continue
            tr = run(data, intra, how, ek)
            if not tr:
                log(f"| {hn} | {en} | 0 |")
                continue
            r = np.array([x["ret"] for x in tr]) * 100
            risk = [x["risk"] for x in tr if x["risk"]]
            e1, _ = simulate(tr, etf, 3, False)
            e2, _ = simulate(tr, etf, 3, True)
            log(f"| {hn} | {en} | {len(tr)} | {(r > 0).mean()*100:.0f} | {r.mean():+.2f} | "
                f"{np.mean([x['why'] == 'stop' for x in tr])*100:.0f} | "
                f"{(np.mean(risk)*100 if risk else 0):.1f} | **{cg(e1):+.1f}** | {dd(e1):.1f} | {cg(e2):+.1f} |")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("\n".join(OUT) + "\n")


if __name__ == "__main__":
    main()
