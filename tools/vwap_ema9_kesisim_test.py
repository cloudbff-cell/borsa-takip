"""Instagram videosundaki (austin.daytrades / algo.jan) VWAP + EMA9 kesişim stratejisi – 10 yıl test (yalnız analiz).

Videodaki kurallar:
  Grafik  : 3 dakikalık mumlar, VWAP (seans), EMA 9
  Giriş   : EMA9, VWAP'ı yukarı keserse CALL al  (EMA9 aşağı keserse PUT – İslami kural: test edilmiyor)
  Çıkış   : mum EMA9'u ihlal ederse çık; "fakeout" olursa opsiyonda -%25/-30'da çık
Videodaki backtest: tek vadeli endeks kontratı (My Funded Futures), ~1 yıl, 174 işlem, %55.8 kazanma, PF 2.17.

Bizim uyarlama (hisse/ETF, opsiyon yok, yalnız alış):
  Sinyal  : mum kapanışında EMA9 > VWAP ve bir önceki mumda EMA9 <= VWAP (aynı gün), 09:39-15:30 arası
  Giriş   : sonraki mumun açılışı
  Çıkış   : mum EMA9 altında kapanırsa sonraki açılışta çık (ana kural);
            fakeout stopu: girişten %X aşağı (opsiyonda -%25/30 ≈ dayanakta ~%0.3 hareket); 15:55'te kapat
Evrenler: SPY ve QQQ (videodaki endeks vadelisine en yakın), İslami top-50 hisse.
Kayma: SPY/QQQ giriş+çıkış %0.02, hisseler %0.05 (ayrıca kaymasız ham sonuç).
"""
import os
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

NY = "America/New_York"
CACHE = Path("cache3m")
OUT = []


def log(s=""):
    print(s, flush=True)
    OUT.append(s)


def load3m(symbols, years=10):
    from engine import trader
    CACHE.mkdir(exist_ok=True)
    api = trader.Alpaca()
    end = pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=20)
    first = end - pd.DateOffset(years=years)
    out, need = {}, {}
    for s in symbols:
        p = CACHE / f"{s}.pkl"
        df = pickle.loads(p.read_bytes()) if p.exists() else None
        out[s] = df
        st = (df.index[-1].tz_convert("UTC") + pd.Timedelta(minutes=3)) if df is not None and len(df) else first
        if end - st > pd.Timedelta(hours=12):
            need[s] = st
    log(f"3 dk veri: {len(symbols) - len(need)} hisse önbellekte güncel, {len(need)} hisse indirilecek")
    t0 = time.time()
    for n, (s, st) in enumerate(need.items()):
        raw = api.bars([s], start=st.isoformat(), end=end.isoformat(), timeframe="3Min", feed="sip",
                       adjustment="all")
        rows = raw.get(s) or []
        if rows:
            df = pd.DataFrame(rows)
            df.index = pd.to_datetime(df["t"], utc=True).dt.tz_convert(NY)
            m = df.index.hour * 60 + df.index.minute
            df = df[(m >= 570) & (m < 960)][["o", "h", "l", "c", "v"]].astype("float32")
            out[s] = pd.concat([out[s], df]) if out[s] is not None else df
            out[s] = out[s][~out[s].index.duplicated(keep="last")]
            (CACHE / f"{s}.pkl").write_bytes(pickle.dumps(out[s]))
        print(f"  {n+1}/{len(need)} {s}: {len(out[s]) if out[s] is not None else 0} mum ({time.time()-t0:.0f} sn)",
              flush=True)
    return {s: df for s, df in out.items() if df is not None and len(df)}


def arrays(df):
    day = df.index.normalize()
    tp = (df["h"] + df["l"] + df["c"]) / 3
    vwap = (tp * df["v"]).groupby(day).cumsum() / df["v"].groupby(day).cumsum().replace(0, np.nan)
    e9 = df["c"].astype(float).ewm(span=9, adjust=False).mean()
    return {
        "o": df["o"].to_numpy(float), "h": df["h"].to_numpy(float), "l": df["l"].to_numpy(float),
        "c": df["c"].to_numpy(float), "vw": vwap.to_numpy(float), "e9": e9.to_numpy(float),
        "mins": np.asarray((df.index.hour * 60 + df.index.minute) - 570), "day": np.asarray(day),
    }


def trades(s, a, stop_pct, slip, start_min=9, exit_mode="close"):
    o, h, l, c, vw, e9, mins, day = (a[k] for k in ("o", "h", "l", "c", "vw", "e9", "mins", "day"))
    n = len(c)
    sig = np.zeros(n, bool)
    sig[1:] = (e9[1:] > vw[1:]) & (e9[:-1] <= vw[:-1]) & (day[1:] == day[:-1])
    sig &= (mins >= start_min) & (mins <= 360)
    out, busy = [], -1
    for i in np.flatnonzero(sig):
        k = i + 1
        if k >= n or day[k] != day[i] or i <= busy:
            continue
        e = o[k] * (1 + slip)
        stop = o[k] * (1 - stop_pct) if stop_pct else -np.inf
        j, ex, why = k, None, None
        while j < n and day[j] == day[i]:
            if mins[j] >= 385:
                ex, why = o[j], "gün sonu"
                break
            if l[j] <= stop:
                ex, why = min(o[j], stop), "stop"
                break
            if exit_mode == "touch" and j > k and l[j] < e9[j - 1]:
                ex, why = min(o[j], e9[j - 1]), "EMA9 ihlali"
                break
            if exit_mode == "close" and c[j] < e9[j]:
                if j + 1 < n and day[j + 1] == day[i]:
                    j += 1
                    ex, why = o[j], "EMA9 altı kapanış"
                else:
                    ex, why = c[j], "EMA9 altı kapanış"
                break
            j += 1
        if ex is None:
            j -= 1
            ex, why = c[j], "gün sonu"
        ex = ex * (1 - slip)
        out.append({"d": pd.Timestamp(day[i]).date(), "s": s, "t_in": int(mins[k]), "t_out": int(mins[j]),
                    "ret": ex / e - 1, "why": why})
        busy = j
    return out


def equity(tr, all_days, alloc, slots):
    """Her işleme sermayenin `alloc` kadarı; aynı anda en fazla `slots` pozisyon."""
    by = {}
    for t in tr:
        by.setdefault(t["d"], []).append(t)
    E, curve, taken = 1.0, [], []
    for d in all_days:
        xs = sorted(by.get(d, []), key=lambda t: t["t_in"])
        open_, pl = [], 0.0
        for t in xs:
            open_ = [q for q in open_ if q > t["t_in"]]
            if len(open_) >= slots:
                continue
            open_.append(t["t_out"])
            pl += alloc * E * t["ret"]
            taken.append(t)
        E += pl
        curve.append(E)
    return pd.Series(curve, index=pd.to_datetime(list(all_days))), taken


def row(name, eq, tr, base=1.0):
    if len(eq) < 2 or not tr:
        log(f"| {name} | 0 | – | – | – | – | – | – |")
        return
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    tot = eq.iloc[-1] / base
    g = (tot ** (1 / yrs) - 1) * 100 if yrs >= 1 else (tot - 1) * 100
    full = pd.concat([pd.Series([base]), eq.reset_index(drop=True)])
    dd = ((full / full.cummax()) - 1).min() * 100
    r = np.array([t["ret"] for t in tr])
    pf = r[r > 0].sum() / -r[r < 0].sum() if (r < 0).any() else float("nan")
    log(f"| {name} | {len(tr)} | {len(tr)/max(yrs, 1/365):.0f} | {(r > 0).mean()*100:.1f} | {r.mean()*100:+.3f} | "
        f"{pf:.2f} | **{g:+.1f}** | {dd:.1f} |")


HDR = ("| Varyant | İşlem | Yılda | Kazanma % | Ort. işlem % | Kâr faktörü | Getiri % (yıllık; <1 yıl: dönem) "
       "| Maks. düşüş % |\n|---|---|---|---|---|---|---|---|")


def run_set(title, data, variants, alloc, slots, show_years=False, spy=None):
    all_days = sorted({d for a in data.values() for d in pd.to_datetime(a["day"]).date})
    log("")
    log(f"### {title} ({all_days[0]} – {all_days[-1]})")
    log(HDR)
    base = None
    for name, kw in variants:
        tr = []
        for s, a in data.items():
            tr += trades(s, a, **kw)
        eq, taken = equity(tr, all_days, alloc, slots)
        row(name, eq, taken)
        if base is None:
            base = (eq, taken)
    eq, tr = base
    for lab, months in (("İlk varyant – son 1 yıl", 12), ("İlk varyant – son 6 ay", 6)):
        cut = eq.index[-1] - pd.DateOffset(months=months)
        b = eq[eq.index <= cut]
        row(lab, eq[eq.index > cut], [t for t in tr if pd.Timestamp(t["d"]) > cut], base=b.iloc[-1] if len(b) else 1.0)
    if show_years:
        log("")
        log("| Yıl | İşlem | Kazanma % | Yıl getirisi % | SPY % |")
        log("|---|---|---|---|---|")
        for y in sorted({t["d"].year for t in tr}):
            t_y = [t for t in tr if t["d"].year == y]
            e_y, prev = eq[eq.index.year == y], eq[eq.index.year < y]
            yr = (e_y.iloc[-1] / (prev.iloc[-1] if len(prev) else 1.0) - 1) * 100
            sr = ""
            if spy is not None:
                s_y, s_p = spy[spy.index.year == y], spy[spy.index.year < y]
                sr = f"{(s_y.iloc[-1] / (s_p.iloc[-1] if len(s_p) else s_y.iloc[0]) - 1) * 100:+.1f}"
            r = np.array([t["ret"] for t in t_y])
            log(f"| {y} | {len(t_y)} | {(r > 0).mean()*100:.0f} | {yr:+.1f} | {sr} |")
    w = pd.Series([t["why"] for t in tr]).value_counts()
    log("")
    log("Çıkışlar (ilk varyant): " + ", ".join(f"{k} {v}" for k, v in w.items()))


def main():
    from engine import config as C
    from engine import data as D
    from engine import sharia
    from engine.run import pick_universe

    cands = C.CANDIDATES + (C.ADRS if C.INCLUDE_ADRS else [])
    prices = D.download_prices(cands + [C.BENCHMARK], period="10y")
    fund = D.fundamentals([t for t in cands if t in prices])
    sh = sharia.screen(fund, {t: df.tail(756) for t, df in prices.items()})
    top, _, _ = pick_universe(fund, set(prices), sh)
    raw = load3m(sorted(set(top) | {"SPY", "QQQ"}))
    A = {s: arrays(df) for s, df in raw.items()}
    spy_d = prices[C.BENCHMARK]["Close"]

    log("## VWAP + EMA9 kesişim (Instagram videosu) – 3 dk mum, yalnız alış")
    log("Videodaki iddia: ~1 yıl, 174 işlem, %55.8 kazanma, kâr faktörü 2.17 (endeks vadelisi, call+put).")

    etf_var = [
        ("Video kuralı: EMA9 altı kapanışta çık, stop %0.3, kayma %0.02", dict(stop_pct=0.003, slip=0.0002)),
        ("Aynısı, kaymasız (ham sinyal)", dict(stop_pct=0.003, slip=0.0)),
        ("Mum EMA9'a değince çık (sıkı)", dict(stop_pct=0.003, slip=0.0002, exit_mode="touch")),
        ("Stop yok", dict(stop_pct=0.0, slip=0.0002)),
        ("Sinyal 10:00'dan sonra", dict(stop_pct=0.003, slip=0.0002, start_min=30)),
    ]
    for s in ("SPY", "QQQ"):
        if s in A:
            run_set(f"{s} (tek enstrüman, sermayenin %100'ü)", {s: A[s]}, etf_var, alloc=1.0, slots=1,
                    show_years=True, spy=spy_d)

    stk = {s: A[s] for s in top if s in A}
    stk_var = [
        ("Video kuralı, stop %0.5, kayma %0.05", dict(stop_pct=0.005, slip=0.0005)),
        ("Aynısı, kaymasız (ham sinyal)", dict(stop_pct=0.005, slip=0.0)),
        ("Mum EMA9'a değince çık (sıkı)", dict(stop_pct=0.005, slip=0.0005, exit_mode="touch")),
        ("Sinyal 10:00'dan sonra", dict(stop_pct=0.005, slip=0.0005, start_min=30)),
    ]
    run_set(f"İslami top-50 hisse ({len(stk)} hisse; işlem başı sermayenin %20'si, en fazla 5 pozisyon)", stk,
            stk_var, alloc=0.2, slots=5, show_years=True, spy=spy_d)

    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("\n".join(OUT) + "\n")


if __name__ == "__main__":
    main()
