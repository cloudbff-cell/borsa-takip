"""'3 gün düşüş' stratejisinin 5 dakikalık veriyle KESİN testi (ileriye bakma yok, işlem yapmaz).

Sinyal günlük veriden; alış ve çıkış 5 dk mumlarla mum mum izlenir: limit emir fiyat seviyeye indiği mumda
dolar, sonrasında stop / hedef hangisi önce gelirse. Aynı 5 dk mumda hem stop hem hedef varsa stop sayılır.
Dolum mumunda hedef sayılmaz (mum içinde sıra bilinmez). 10 seans sonunda son kapanıştan çıkılır.
Veri: Alpaca SIP 5 dk, son 3 yıl. Evren: İslami filtreden geçen ilk 50. Kayma %0.05 x 2.
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


def run_variant(data, intra, entry, sp, tp, maxd=10):
    """entry: ('close',) kapanışta al | ('limit', yüzde) kapanışın altından limit (2 seans)."""
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
            if i < 210 or i + 2 >= len(d) or day < bday[0] or (busy is not None and day <= busy):
                continue
            s_end = np.searchsorted(bday, day + pd.Timedelta(days=1))
            if s_end == 0 or bday[s_end - 1] != day:
                continue
            scale = c5[s_end - 1] / d["Close"].iloc[i]           # günlük düzeltilmiş -> 5 dk ölçeği
            sclose = c5[s_end - 1]
            k = None
            if entry[0] == "close":
                e, k = sclose, s_end - 1
                start = s_end
            else:
                lvl = sclose * (1 - entry[1])
                last_day = dates[min(i + 2, len(d) - 1)]
                stop_idx = np.searchsorted(bday, last_day + pd.Timedelta(days=1))
                for j in range(s_end, stop_idx):
                    if l5[j] <= lvl:
                        k, e = j, min(o5[j], lvl)
                        break
                if k is None:
                    continue
                start = k
            e *= 1 + SLIP
            stop, target = e * (1 - sp), e * (1 + tp)
            entry_day = bday[k]
            di = dates.get_loc(entry_day) if entry_day in dates else i + 1
            exit_day = dates[min(di + maxd, len(dates) - 1)]
            end = np.searchsorted(bday, exit_day + pd.Timedelta(days=1))
            ex, why, j = None, "süre", start
            for j in range(start, min(end, len(b))):
                if j > k and o5[j] <= stop:
                    ex, why = o5[j], "stop"
                    break
                if l5[j] <= stop and (j > k or entry[0] != "close"):
                    ex, why = stop, "stop"
                    break
                if j > k and h5[j] >= target:
                    ex, why = max(o5[j], target), "hedef"
                    break
            if ex is None:
                j = min(end, len(b)) - 1
                ex = c5[j]
            ex *= 1 - SLIP
            busy = bday[j]
            trades.append({"in": entry_day, "out": bday[j], "ret": ex / e - 1, "why": why,
                           "score": -(d["Close"].iloc[i] / d["Close"].iloc[i - 3] - 1), "s": t})
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
    spy = prices[C.BENCHMARK]["Close"]
    etf = prices["SPUS"]["Close"].pct_change().loc[s0:].fillna(0)
    spy_r = spy.pct_change().loc[s0:].fillna(0)
    yrs = (etf.index[-1] - etf.index[0]).days / 365.25
    cg = lambda eq: (eq.iloc[-1] ** (1 / yrs) - 1) * 100
    dd = lambda eq: ((eq / eq.cummax()) - 1).min() * 100
    log(f"## 3 gün düşüş – 5 dk veriyle kesin test ({etf.index[0]:%Y-%m} – {etf.index[-1]:%Y-%m}, {yrs:.1f} yıl)")
    log(f"SPY al-tut yıllık {cg((1 + spy_r).cumprod()):+.1f}% (düşüş {dd((1 + spy_r).cumprod()):.1f}%) · "
        f"SPUS al-tut {cg((1 + etf).cumprod()):+.1f}% (düşüş {dd((1 + etf).cumprod()):.1f}%)")
    log("")
    log("| Alış / stop / hedef | İşlem | Kazanma % | Ort. % | Stop % | Yıllık (nakit boşta) | Düşüş | "
        "Yıllık (boşta nakit SPUS'ta) | Düşüş |")
    log("|---|---|---|---|---|---|---|---|---|")
    variants = [(("close",), 0.02, 0.02), (("close",), None, None), (("limit", 0.02), 0.02, 0.02),
                (("limit", 0.02), 0.03, 0.03), (("limit", 0.02), 0.03, 0.02), (("limit", 0.03), 0.02, 0.02),
                (("limit", 0.02), None, None)]
    for ent, sp, tp in variants:
        tr = run_variant(data, intra, ent, sp if sp else 0.99, tp if tp else 9.0)
        name = ("Kapanışta" if ent[0] == "close" else f"%{ent[1]*100:g} altından limit") + \
               (f" / −%{sp*100:g} / +%{tp*100:g}" if sp else " / stopsuz, 10 seans")
        if not tr:
            log(f"| {name} | 0 |")
            continue
        r = np.array([x["ret"] for x in tr]) * 100
        e1, _ = simulate(tr, etf, 3, False)
        e2, _ = simulate(tr, etf, 3, True)
        log(f"| {name} | {len(tr)} | {(r > 0).mean()*100:.0f} | {r.mean():+.2f} | "
            f"{np.mean([x['why'] == 'stop' for x in tr])*100:.0f} | **{cg(e1):+.1f}** | {dd(e1):.1f} | "
            f"**{cg(e2):+.1f}** | {dd(e2):.1f} |")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("\n".join(OUT) + "\n")


if __name__ == "__main__":
    main()
