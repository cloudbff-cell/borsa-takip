"""Ana tarama. Kullanım:  python -m engine.run [--mode auto|pre|intraday|post] [--force]

pre      : Açılış öncesi gün planı (bilanço takvimi, stratejik adaylar, mevsimsel hisseler)
intraday : Seans içi saatlik tarama, yeni sinyaller (kapanışta teyit edilmemiş)
post     : Kapanış sonrası teyitli sinyaller + gün özeti
"""
import argparse
import json
import os
from datetime import date, datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import config as C
from . import data as D
from . import telegram
from . import trader
from .analysis import fundamental_view, seasonality
from .indicators import add_indicators
from .signals import SIGNALS, aggregate_stats, compute_signals, signal_events, technical_score, trade_levels

NY = ZoneInfo("America/New_York")
TR = ZoneInfo("Europe/Istanbul")
SITE = Path("site")
PAGES_URL = os.getenv("PAGES_URL", "")


def detect_mode(now_ny: datetime) -> str:
    if now_ny.weekday() >= 5:
        return "off"
    t = now_ny.time()
    if dtime(7, 30) <= t < dtime(9, 30):
        return "pre"
    if dtime(9, 30) <= t < dtime(16, 0):
        return "intraday"
    if dtime(16, 0) <= t < dtime(18, 30):
        return "post"
    return "off"


def session_fraction(now_ny: datetime) -> float:
    start = now_ny.replace(hour=9, minute=30, second=0, microsecond=0)
    return float(np.clip((now_ny - start).total_seconds() / (6.5 * 3600), 0, 1))


def r2(x):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), 2)


def pick_universe(fund: dict, available: set):
    rows = [(t, f.get("marketCap") or 0, f.get("sector") or "Diğer") for t, f in fund.items() if t in available]
    rows.sort(key=lambda r: -r[1])
    top = [r[0] for r in rows[: C.TOP_N]]
    leaders = {}
    for t, mc, sec in rows:
        leaders.setdefault(sec, [])
        if len(leaders[sec]) < C.LEADERS_PER_SECTOR:
            leaders[sec].append(t)
    tracked = list(dict.fromkeys(top + [t for v in leaders.values() for t in v]))
    return top, leaders, tracked


def strategic_plan(s):
    """Alım bölgesi: fiyat 20 EMA'dan 1 ATR'den fazla uzaksa geri çekilme beklenir."""
    p, a, e20 = s["price"], s["atr"], s["ema20"]
    stretched = p - e20 > a
    if stretched:
        lo, hi = e20, min(p, e20 + 0.5 * a)
    else:
        lo, hi = (max(e20, p - 0.5 * a) if e20 < p else p - 0.5 * a), p
    mid = (lo + hi) / 2
    stop = min(lo - 1.5 * a, s["ema50"] - 0.25 * a) if s["ema50"] < lo else lo - 1.5 * a
    t1, t2 = mid + C.T1_ATR * a, mid + C.T2_ATR * a
    return {"zoneLow": lo, "zoneHigh": hi, "stop": stop, "t1": t1, "t2": t2,
            "risk_pct": (mid - stop) / mid * 100, "rr": (t2 - mid) / (mid - stop), "wait": stretched}


def chart_payload(df, sig, events, n=300):
    d = df.tail(n)
    start = d.index[0]
    ts = lambda i: i.strftime("%Y-%m-%d")
    return {
        "ohlc": [[ts(i), r2(r.Open), r2(r.High), r2(r.Low), r2(r.Close), int(r.Volume)] for i, r in d.iterrows()],
        "ema20": [r2(x) for x in d["ema20"]], "ema50": [r2(x) for x in d["ema50"]],
        "ema200": [r2(x) for x in d["ema200"]], "rsi": [r2(x) for x in d["rsi"]],
        "macd_hist": [r2(x) for x in d["macd_hist"]],
        "markers": [{"t": ts(e["date"]), "code": e["code"], "side": SIGNALS[e["code"]][0],
                     "p": r2(e["price"])} for e in events if e["date"] >= start],
    }


def build(mode: str, now_ny: datetime, prices=None, fund=None, monthly=None):
    cands = C.CANDIDATES + (C.ADRS if C.INCLUDE_ADRS else [])
    if prices is None:
        prices = D.download_prices(cands + [C.BENCHMARK], period="3y")
    if fund is None:
        fund = D.fundamentals([t for t in cands if t in prices], force=(mode == "pre"))
    bench = prices.get(C.BENCHMARK)
    top, leaders, tracked = pick_universe(fund, set(prices))

    # Mevsimsellik: aylık 11 yıllık veri, günde bir kez
    if monthly is None:
        mpath = D.CACHE / f"monthly_{date.today().isoformat()}.pkl"
        if mpath.exists():
            monthly = pd.read_pickle(mpath)
        else:
            for old in D.CACHE.glob("monthly_*.pkl"):
                old.unlink()
            monthly = D.download_prices(tracked + [C.BENCHMARK], period="11y", interval="1mo")
            pd.to_pickle(monthly, mpath)

    market_open_today = bench is not None and bench.index[-1].date() == now_ny.date()
    vscale = session_fraction(now_ny) if (mode == "intraday" and market_open_today) else 1.0
    bench_ret = bench["Close"].iloc[-1] / bench["Close"].iloc[-64] - 1 if bench is not None and len(bench) > 64 else np.nan

    stocks, all_events = [], {}
    (SITE / "data" / "c").mkdir(parents=True, exist_ok=True)
    for t in tracked:
        df = add_indicators(prices[t])
        if len(df) < C.EMA_SLOW + 5:
            continue
        sig = compute_signals(df, vscale)
        evs = signal_events(df, sig)
        all_events[t] = evs
        row, last = df.iloc[-1], sig.iloc[-1]
        f = fund.get(t, {})
        fv = fundamental_view(f, float(row["Close"]))
        ret63 = row["Close"] / df["Close"].iloc[-64] - 1 if len(df) > 64 else np.nan
        ts = technical_score(row, ret63 - bench_ret)
        mdf = monthly.get(t)
        sea = seasonality(mdf, monthly.get(C.BENCHMARK), now_ny.date()) if mdf is not None and len(mdf) > 24 else None
        total = ts["tech"] + ts["rs"] + fv["score"] + (sea["score"] if sea else 0)

        today_sigs = [code for code in SIGNALS if bool(last[code])]
        recent = [e for e in evs if e["date"] >= df.index[-3]]
        active = []
        for code in today_sigs:
            side = SIGNALS[code][0]
            lv = trade_levels(row, side)
            risky = side == "AL" and fv["daysToEarnings"] is not None and 0 <= fv["daysToEarnings"] <= C.EARNINGS_GUARD_DAYS
            active.append({"code": code, "side": side, "name": SIGNALS[code][1], "why": SIGNALS[code][2],
                           "levels": {k: r2(v) for k, v in lv.items()}, "earningsRisk": risky})
        prev_close = df["Close"].iloc[-2]
        stocks.append({
            "t": t, "name": f.get("shortName") or t, "sector": C.SECTOR_TR.get(f.get("sector"), f.get("sector") or "Diğer"),
            "mcap": f.get("marketCap"), "isTop": t in top,
            "rank": top.index(t) + 1 if t in top else None,
            "leader": any(t in v for v in leaders.values()),
            "price": r2(row["Close"]), "chg": r2((row["Close"] / prev_close - 1) * 100),
            "trend": last["trend"], "rsi": r2(row["rsi"]), "atr": r2(row["atr"]),
            "ema20": r2(row["ema20"]), "ema50": r2(row["ema50"]), "ema200": r2(row["ema200"]),
            "hh20": r2(row["hh"]), "ll20": r2(row["ll"]), "res60": r2(row["res60"]), "sup60": r2(row["sup60"]),
            "volr": r2(last["volr"]), "rs3m": r2((ret63 - bench_ret) * 100),
            "score": round(float(total), 1), "parts": {"teknik": ts["tech"], "goreceli": ts["rs"],
                                                        "temel": fv["score"], "mevsim": sea["score"] if sea else 0},
            "signals": active,
            "recent": [{"d": e["date"].strftime("%Y-%m-%d"), "code": e["code"], "side": SIGNALS[e["code"]][0],
                        "name": SIGNALS[e["code"]][1]} for e in recent],
            "fund": fv, "season": sea,
        })
        (SITE / "data" / "c" / f"{t}.json").write_text(json.dumps(chart_payload(df, sig, evs), separators=(",", ":")))

    stats = aggregate_stats(all_events)
    by = {s["t"]: s for s in stocks}

    # Stratejik adaylar: son 3 günde AL sinyali ya da güçlü yükseliş trendi + yüksek puan; bilanço riski hariç
    def strategic(s):
        recent_buy = any(r["side"] == "AL" for r in s["recent"])
        recent_sell = any(r["side"] == "SAT" for r in s["recent"])
        strong = s["trend"] == "Yükseliş" and s["score"] >= 65
        er = s["fund"]["daysToEarnings"]
        return ((recent_buy or strong) and not recent_sell and (s["rsi"] or 0) < 75
                and not (er is not None and 0 <= er <= C.EARNINGS_GUARD_DAYS))

    picks = []
    for s in sorted([s for s in stocks if strategic(s)], key=lambda s: -s["score"]):
        plan = strategic_plan(s)
        if plan["rr"] >= 1.5 and len(picks) < 10:   # risk/ödül zayıfsa aday sayma
            s["plan"] = {k: r2(v) if not isinstance(v, bool) else v for k, v in plan.items()}
            picks.append(s)
    warnings = [s for s in stocks if any(r["side"] == "SAT" for r in s["recent"])]
    seasonal = sorted([s for s in stocks if s["season"] and s["season"]["cur"]], key=lambda s: -s["season"]["score"])[:10]
    seasonal_next = sorted([s for s in stocks if s["season"] and s["season"]["next"]], key=lambda s: -s["season"]["nextScore"])[:10]
    earn = sorted([s for s in stocks if s["fund"]["daysToEarnings"] is not None and 0 <= s["fund"]["daysToEarnings"] <= 14],
                  key=lambda s: s["fund"]["daysToEarnings"])

    summary = {
        "generated": datetime.now(TR).strftime("%Y-%m-%d %H:%M"), "mode": mode,
        "marketOpenToday": bool(market_open_today), "lastBar": bench.index[-1].strftime("%Y-%m-%d") if bench is not None else None,
        "benchmark": {"t": C.BENCHMARK, "price": r2(bench["Close"].iloc[-1]) if bench is not None else None,
                      "chg": r2((bench["Close"].iloc[-1] / bench["Close"].iloc[-2] - 1) * 100) if bench is not None else None,
                      "trend": compute_signals(add_indicators(bench)).iloc[-1]["trend"] if bench is not None else None},
        "top": top, "leaders": {C.SECTOR_TR.get(k, k): v for k, v in leaders.items()},
        "picks": [s["t"] for s in picks], "warnings": [s["t"] for s in warnings],
        "seasonal": [s["t"] for s in seasonal], "seasonalNext": [s["t"] for s in seasonal_next],
        "earnings": [s["t"] for s in earn], "stats": stats, "signalDefs": SIGNALS,
        "stocks": stocks,
    }
    (SITE / "data" / "summary.json").write_text(json.dumps(summary, default=str, separators=(",", ":")))
    return summary, by


# ---------- Telegram metinleri ----------

def _fmt_sig(s, g, confirmed):
    lv = g["levels"]
    icon = "🟢" if g["side"] == "AL" else "🔴"
    head = f"{icon} <b>{s['t']}</b> {g['side']} – {g['name']}" + ("" if confirmed else " <i>(seans içi, kapanışta teyit gerekir)</i>")
    lines = [head, f"Fiyat ${s['price']} ({s['chg']:+.2f}%) · RSI {s['rsi']} · Trend {s['trend']} · Puan {s['score']:.0f}/100",
             f"Neden: {g['why']}"]
    if g["side"] == "AL":
        lines.append(f"Giriş ~${lv['entry']} · Stop ${lv['stop']} (-{lv['risk_pct']:.1f}%) · H1 ${lv['t1']} · H2 ${lv['t2']}")
        if lv.get("resistance"):
            lines.append(f"Yakın direnç (60g zirve): ${lv['resistance']}")
    else:
        lines.append(f"Elde varsa çıkış/kâr al bölgesi · Geçersizlik ${lv['invalidation']} · Destek ${lv['reentry_support']}")
    if g.get("earningsRisk"):
        lines.append(f"⚠️ Bilanço {s['fund']['nextEarnings']} – {s['fund']['daysToEarnings']} gün kaldı, risk yüksek")
    return "\n".join(lines)


def _stat_line(stats, code):
    st = stats.get(code)
    return f"   Geçmiş: {st['n']} sinyal, %{st['hit']} isabet, {C.FORWARD_DAYS}g ort. {st['avg']:+.2f}%" if st else ""


def alerts(summary, by, mode, state):
    today = summary["lastBar"]
    phase = "close" if mode == "post" else "intra"
    msgs = []
    for s in summary["stocks"]:
        for g in s["signals"]:
            key = f"{today}|{s['t']}|{g['code']}|{phase}"
            if key in state["sent"]:
                continue
            state["sent"][key] = today
            msgs.append(_fmt_sig(s, g, confirmed=(mode == "post")) + "\n" + _stat_line(summary["stats"], g["code"]))
    return msgs


def plan_message(summary, by):
    b = summary["benchmark"]
    L = [f"☀️ <b>Gün Planı</b> – {summary['generated']} (TR)",
         f"S&P 500 (SPY) ${b['price']} · Trend: {b['trend']}", ""]
    if summary["earnings"]:
        L.append("📅 <b>Yaklaşan bilançolar (14 gün)</b>")
        for t in summary["earnings"][:12]:
            s = by[t]
            fv = s["fund"]
            L.append(f"• {t} – {fv['nextEarnings']} ({fv['daysToEarnings']} gün)" + (f" · {fv['expectation']}" if fv["expectation"] else ""))
        L.append("")
    L.append("🎯 <b>Stratejik alım adayları</b> (kural tabanlı, tavsiye değildir)")
    for t in summary["picks"]:
        s = by[t]
        p = s["plan"]
        L.append(f"• <b>{t}</b> ${s['price']} · Puan {s['score']:.0f} · {s['trend']} · RSI {s['rsi']}\n"
                 f"   Alım bölgesi ${p['zoneLow']}–${p['zoneHigh']}" + (" (geri çekilme bekle)" if p["wait"] else "")
                 + f" · Stop ${p['stop']} (-{p['risk_pct']:.1f}%) · H1 ${p['t1']} · H2 ${p['t2']} · R/Ö {p['rr']:.1f}")
    if summary["warnings"]:
        L += ["", "⚠️ <b>Satış/çıkış uyarısı olanlar (son 3 gün)</b>", ", ".join(summary["warnings"])]
    L += ["", f"🍂 <b>Mevsimsel güçlüler – {by[summary['seasonal'][0]]['season']['curName'] if summary['seasonal'] else ''}</b>"]
    for t in summary["seasonal"][:6]:
        c = by[t]["season"]["cur"]
        L.append(f"• {t}: son {c['n']} yılda %{c['win']:.0f} pozitif, ort. {c['avg']:+.1f}%" + (f" (SPY'a göre {c['excess']:+.1f}%)" if c["excess"] is not None else ""))
    if PAGES_URL:
        L += ["", f"📊 Panel: {PAGES_URL}"]
    return "\n".join(L)


def close_message(summary, by):
    st = sorted(summary["stocks"], key=lambda s: -(s["chg"] or 0))
    L = [f"🌙 <b>Kapanış Özeti</b> – {summary['lastBar']}",
         f"SPY {summary['benchmark']['chg']:+.2f}% · Trend {summary['benchmark']['trend']}",
         "En çok yükselen: " + ", ".join(f"{s['t']} {s['chg']:+.1f}%" for s in st[:5]),
         "En çok düşen: " + ", ".join(f"{s['t']} {s['chg']:+.1f}%" for s in st[-5:][::-1]),
         "", "Bugün kapanışta sinyal verenler: " + (", ".join(f"{s['t']}({'/'.join(g['side'] for g in s['signals'])})" for s in summary["stocks"] if s["signals"]) or "yok")]
    if PAGES_URL:
        L.append(f"📊 Panel: {PAGES_URL}")
    return "\n".join(L)


def write_status(extra=None):
    st = {"time": datetime.now(TR).strftime("%Y-%m-%d %H:%M"), "telegram": telegram.STATUS}
    st.update(extra or {})
    (SITE / "data").mkdir(parents=True, exist_ok=True)
    (SITE / "data" / "status.json").write_text(json.dumps(st, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="auto")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--ping", action="store_true", help="Kod güncellemesinden sonra kısa bir test mesajı gönder")
    a = ap.parse_args()
    now_ny = datetime.now(NY)
    mode = detect_mode(now_ny) if a.mode == "auto" else a.mode
    print("Mod:", mode, now_ny)
    if mode == "off" and not a.force:
        print("Piyasa saatleri dışında, çıkılıyor.")
        return
    if mode == "off":
        mode = "post"
    summary, by = build(mode, now_ny)
    if a.ping:
        telegram.send(f"✅ <b>Borsa takip sistemi güncellendi</b>\n{len(summary['stocks'])} hisse izleniyor · "
                      f"{len(summary['picks'])} stratejik aday: {', '.join(summary['picks'][:10])}"
                      + (f"\n📊 Panel: {PAGES_URL}" if PAGES_URL else ""))
        trade_step(summary, by, "check", D.load_state())   # yalnız bağlantı kontrolü, emir yok
        write_status({"mode": "push", "alpaca": trader.STATUS})
    if a.no_telegram:
        return
    state = D.load_state()
    if mode in ("intraday", "post") and not summary["marketOpenToday"]:
        print("Bugün piyasa kapalı (tatil); bildirim yok.")
        return
    if mode == "pre":
        key = f"plan|{now_ny.date()}"
        if key not in state["sent"]:
            telegram.send(plan_message(summary, by))
            state["sent"][key] = str(now_ny.date())
    else:
        msgs = alerts(summary, by, mode, state)
        if msgs:
            telegram.send(("✅ <b>Kapanışta teyitli sinyaller</b>\n\n" if mode == "post" else "⏱ <b>Seans içi yeni sinyaller</b>\n\n")
                          + "\n\n".join(msgs))
        if mode == "post":
            key = f"close|{summary['lastBar']}"
            if key not in state["sent"]:
                telegram.send(close_message(summary, by))
                state["sent"][key] = summary["lastBar"]
        trade_step(summary, by, mode, state)
    D.save_state(state)
    write_status({"mode": mode, "alpaca": trader.STATUS})


def trade_step(summary, by, mode, state, client=None):
    """Otomatik işlem: emirler + portföyün panele yazılması. Hata taramayı durdurmaz."""
    try:
        msgs, portfolio = trader.run(summary, by, mode, state, client)
    except Exception as e:  # noqa: BLE001
        print("İşlem modülü hatası:", e)
        trader.STATUS["result"] = f"hata: {str(e)[:200]}"
        telegram.send(f"⚠️ Otomatik işlem modülü çalışamadı: {e}")
        return
    if msgs:
        telegram.send("🤖 <b>Bot işlemleri</b>\n\n" + "\n\n".join(msgs))
    if portfolio:
        p = D.CACHE / "trades.json"
        portfolio["log"] = json.loads(p.read_text())[-30:] if p.exists() else []
        summary["portfolio"] = portfolio
        (SITE / "data" / "summary.json").write_text(json.dumps(summary, default=str, separators=(",", ":")))


if __name__ == "__main__":
    main()
