"""Seans dışı (kapanış sonrası + piyasa öncesi) fiyatı '3 gün düşüş' stratejisine yardım ediyor mu? (işlem yapmaz)

Mevcut kural: sinyal günü kapanışın %2 altına limitli alış (2 seans), −%2 stop / +%2 kâr al, en çok 10 seans.
Seans dışı fiyat (pm): sinyal kapanışından sonraki seans dışı işlemlerin 09:25'e kadarki son fiyatı
(16:00-20:00 kapanış sonrası + 04:00-09:25 piyasa öncesi; Alpaca SIP 5 dk, 20:00-04:00 gece seansı bu veride yok).
Varyantlar: her seans açılmadan önce pm fiyatı sinyal kapanışının X% altındaysa (haberle sert düşüş) bekleyen
alış emri iptal edilir. Ayrıca pm boşluğuna göre işlem sonuçları ve 'stop seviyesinin altında açılacak
pozisyonu piyasa öncesinde satmak' açılışa göre ne fark eder, ölçülür.
Veri: son 5 yıl, İslami ilk 50, kayma %0.05 x 2. Portföy 3 pozisyon (getiri_arttirma.simulate).
"""
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import config as C  # noqa: E402
from engine import data as D  # noqa: E402
from engine import sharia, trader  # noqa: E402
from engine.run import pick_universe  # noqa: E402
from tools import down3_analiz as A  # noqa: E402
from tools.getiri_arttirma import simulate  # noqa: E402
from tools.giris_arastirma import prep  # noqa: E402

SLIP = 0.0005
LIMIT, SP, TP, MAXD = C.DIP_LIMIT_BELOW, C.DIP_STOP, C.DIP_TP, C.DIP_MAX_DAYS
OUT = []


def log(s=""):
    print(s, flush=True)
    OUT.append(s)


def load(symbols, start, end):
    """{sembol: (normal seans df, seans dışı df)} – Alpaca SIP 5 dk, 'all' düzeltmeli."""
    api = trader.Alpaca()
    reg, ext = {}, {}
    for i in range(0, len(symbols), 4):
        raw = api.bars(symbols[i:i + 4], start=start.isoformat(), end=end.isoformat(), feed="sip", adjustment="all")
        for s, rows in raw.items():
            df = pd.DataFrame(rows)
            df.index = pd.to_datetime(df["t"], utc=True).dt.tz_convert("America/New_York")
            df = df[["o", "h", "l", "c", "v"]].astype(float)
            m = df.index.hour * 60 + df.index.minute
            reg[s] = df[(m >= 570) & (m < 960)]
            ext[s] = df[(m < 565) | (m >= 960)]          # 09:25'ten önce ve 16:00 sonrası
        print(f"  {min(i + 4, len(symbols))}/{len(symbols)}", flush=True)
        time.sleep(0.3)
    return reg, ext


def pm_price(ext_s, after_ts, before_ts):
    """after_ts (kapanış) ile before_ts (09:25) arasındaki son seans dışı fiyat; işlem yoksa None."""
    seg = ext_s[(ext_s.index > after_ts) & (ext_s.index < before_ts)]
    return float(seg["c"].iloc[-1]) if len(seg) else None


def run(data, reg, ext, cancel_gap=None, collect=None):
    trades = []
    for t, d in data.items():
        b, x = reg.get(t), ext.get(t)
        if b is None or x is None or not len(b):
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
            sclose = c5[s_end - 1]
            close_ts = b.index[s_end - 1]
            lvl = sclose * (1 - LIMIT)
            k, e, gap1, cancelled = None, None, None, False
            for n in (1, 2):                              # emir 2 seans geçerli
                sday = dates[i + n]
                a = np.searchsorted(bday, sday)
                z = np.searchsorted(bday, sday + pd.Timedelta(days=1))
                if a >= z:
                    break
                open_ts = b.index[a]
                pm = pm_price(x, close_ts, open_ts - pd.Timedelta(minutes=5))
                gap = (pm / sclose - 1) if pm else None
                if n == 1:
                    gap1 = gap
                if cancel_gap is not None and gap is not None and gap <= -cancel_gap:
                    cancelled = True
                    break
                for j in range(a, z):
                    if l5[j] <= lvl:
                        k, e = j, min(o5[j], lvl)
                        break
                if k is not None:
                    break
            if k is None:
                if collect is not None and cancelled:
                    collect["iptal"] = collect.get("iptal", 0) + 1
                continue
            e *= 1 + SLIP
            stop, target = e * (1 - SP), e * (1 + TP)
            di = dates.get_loc(bday[k])
            end = np.searchsorted(bday, dates[min(di + MAXD, len(dates) - 1)] + pd.Timedelta(days=1))
            ex, why, j = None, "süre", k
            for j in range(k, min(end, len(b))):
                if j > k and o5[j] <= stop:               # stopun altında açılış (boşluk)
                    ex, why = o5[j], "stop (boşluk)"
                    if collect is not None and bday[j] != bday[j - 1]:
                        pm = pm_price(x, b.index[j - 1], b.index[j] - pd.Timedelta(minutes=5))
                        if pm:
                            collect.setdefault("gapstop", []).append((pm / o5[j] - 1) * 100)
                    break
                if l5[j] <= stop:
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
            trades.append({"in": bday[k], "out": bday[j], "ret": ex / e - 1, "why": why, "gap": gap1,
                           "score": -(d["Close"].iloc[i] / d["Close"].iloc[i - 3] - 1), "s": t})
    return trades


def main():
    cands = C.CANDIDATES + (C.ADRS if C.INCLUDE_ADRS else [])
    prices = D.download_prices(cands + [C.BENCHMARK, "SPUS"], period="10y")
    fund = D.fundamentals([t for t in cands if t in prices])
    sh = sharia.screen(fund, {t: df.tail(756) for t, df in prices.items()})
    top, _, _ = pick_universe(fund, set(prices), sh)
    end = pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=20)
    start = end - pd.DateOffset(years=5)
    reg, ext = load(top, start, end)
    data = {t: prep(prices[t]) for t in top if t in prices and len(prices[t]) > 400}
    s0 = start.tz_convert(None).normalize()
    etf = prices["SPUS"]["Close"].pct_change().loc[s0:].fillna(0)
    yrs = (etf.index[-1] - etf.index[0]).days / 365.25
    cg = lambda eq: (eq.iloc[-1] ** (1 / yrs) - 1) * 100
    dd = lambda eq: ((eq / eq.cummax()) - 1).min() * 100
    log(f"## Seans dışı fiyat testi – 3 gün düşüş ({etf.index[0]:%Y-%m} – {etf.index[-1]:%Y-%m}, {yrs:.1f} yıl, "
        f"{len(data)} hisse)")
    log("Seans dışı = kapanış sonrası (16:00-20:00) + piyasa öncesi (04:00-09:25). Gece seansı (20:00-04:00) veride yok.")
    log("")
    log("| Kural | İşlem | İptal | Kazanma % | Ort. % | Yıllık (nakit boşta) | Düşüş | Yıllık (boşta SPUS) | Düşüş |")
    log("|---|---|---|---|---|---|---|---|---|")
    base = None
    for cg_ in (None, 0.06, 0.05, 0.04, 0.03):
        col = {}
        tr = run(data, reg, ext, cg_, col)
        if cg_ is None:
            base, base_col = tr, col
        r = np.array([z["ret"] for z in tr]) * 100
        e1, _ = simulate(tr, etf, 3, False)
        e2, _ = simulate(tr, etf, 3, True)
        name = "Mevcut (seans dışına bakmadan)" if cg_ is None else f"Seans dışında −%{cg_*100:g} ve altıysa emri iptal"
        log(f"| {name} | {len(tr)} | {col.get('iptal', 0)} | {(r > 0).mean()*100:.0f} | {r.mean():+.2f} | "
            f"**{cg(e1):+.1f}** | {dd(e1):.1f} | **{cg(e2):+.1f}** | {dd(e2):.1f} |")

    log("")
    log("### Mevcut kuralda, ilk seans öncesi seans dışı fiyata göre işlem sonuçları (sinyal kapanışına göre)")
    log("| Seans dışı fiyat | İşlem | Kazanma % | Ort. % | Stop % |")
    log("|---|---|---|---|---|")
    df = pd.DataFrame(base)
    df["g"] = df["gap"] * 100
    bins = [-100, -5, -3, -2, -1, 0, 100]
    labels = ["−%5 altı", "−%5 … −%3", "−%3 … −%2", "−%2 … −%1", "−%1 … 0", "0 üstü"]
    df["b"] = pd.cut(df["g"], bins, labels=labels)
    for lab, g in df.groupby("b", observed=True):
        log(f"| {lab} | {len(g)} | {(g.ret > 0).mean()*100:.0f} | {g.ret.mean()*100:+.2f} | "
            f"{g.why.str.startswith('stop').mean()*100:.0f} |")
    log(f"| veri yok | {df['g'].isna().sum()} | | | |")
    gs = base_col.get("gapstop", [])
    if gs:
        log("")
        log(f"### Stopun altında açılan pozisyonlar ({len(gs)} işlem)")
        log(f"Piyasa öncesi son fiyattan satmak, açılış fiyatına göre ortalama {np.mean(gs):+.2f}% "
            f"(medyan {np.median(gs):+.2f}%) fark ederdi; %{np.mean(np.array(gs) > 0)*100:.0f}'inde daha iyi.")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("\n".join(OUT) + "\n")


if __name__ == "__main__":
    main()
