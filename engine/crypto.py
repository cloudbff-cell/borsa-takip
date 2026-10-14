"""Kripto bölümü: BTC ve ETH için trend kuralları (hisse orta vade kurallarının uyarlaması).

- Puan (0-100): teknik 40 + 90 günlük momentum 20 + hacim teyidi 5, 65 üzerinden 100'e ölçeklenir
  (bilanço / analist verisi yok).
- Giriş: yükseliş trendi + [son 3 günde AL sinyali ve puan >= 65] ya da [puan >= 75 ve 20 EMA'ya geri çekilme];
  RSI < 75, son 3 günde SAT sinyali yok. Sinyaller yalnız KAPANMIŞ günlük barlarla (UTC) hesaplanır.
- Emir: alım bölgesine limitli alış (GTC); dolunca stop-limit satış kurulur (kriptoda düz stop / bracket yok).
- Çıkış: ilk stop giriş - 2 ATR, iz süren stop en yüksek - 3 ATR (yalnız yukarı). Fiyat stopun altına
  boşlukla inip stop-limit dolmazsa pozisyon piyasa emriyle kapatılır.
- Boyut: işlem başına sermayenin %0.5'i risk; coin başına en fazla bölümün yarısı (sermayenin %5'i).
"""
import math
from datetime import datetime, timezone

import numpy as np

from . import config as C
from .analysis import volume_view
from .indicators import add_indicators
from .signals import SIGNALS, compute_signals, technical_score

TAG = "₿"


def _r2(x):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), 2)


def analyze(prices: dict) -> list:
    out = []
    for ysym, asym in C.CRYPTO.items():
        df_all = prices.get(ysym)
        if df_all is None or len(df_all) < 260:
            continue
        df_all = add_indicators(df_all)
        df = df_all.iloc[:-1]                    # kapanmış barlar (bugünkü UTC günü henüz bitmedi)
        sig = compute_signals(df)
        row, last = df.iloc[-1], df_all.iloc[-1]
        ret90 = row["Close"] / df["Close"].iloc[-91] - 1
        ts = technical_score(row, np.nan)        # göreceli kısmı ayrıca hesaplıyoruz
        mom = float(np.clip((ret90 + 0.30) / 0.90, 0, 1) * 20)
        vol = volume_view(df)["score"]
        score = (ts["tech"] + mom + vol) / 65 * 100
        recent = [code for code in SIGNALS if bool(sig[code].iloc[-3:].any())]
        trend = sig["trend"].iloc[-1]
        p, a, e20, e50 = float(last["Close"]), float(row["atr"]), float(row["ema20"]), float(row["ema50"])
        buy_sig = any(SIGNALS[k][0] == "AL" for k in recent)
        sell_sig = any(SIGNALS[k][0] == "SAT" for k in recent)
        kind = None
        if trend == "Yükseliş" and not sell_sig and row["rsi"] < 75:
            if buy_sig and score >= C.ENTRY_SIGNAL_MIN_SCORE:
                kind = "sinyal"
            elif score >= C.ENTRY_TREND_MIN_SCORE:
                kind = "trend"
        stretched = p > e20 + (a if kind == "sinyal" else 0.5 * a)
        if stretched:
            lo, hi = e20, min(p, e20 + 0.5 * a)
        elif p < e20:
            lo, hi = p - 0.5 * a, p
        else:
            lo, hi = (max(e20, p - 0.5 * a) if kind == "sinyal" else e20), p
        stop = min(lo - 1.5 * a, e50 - 0.25 * a) if e50 < lo else lo - 1.5 * a
        out.append({
            "t": ysym, "asym": asym, "price": _r2(p), "high": _r2(last["High"]),
            "chg": _r2((p / df["Close"].iloc[-1] - 1) * 100), "atr": _r2(a), "ema20": _r2(e20), "ema50": _r2(e50),
            "ema200": _r2(row["ema200"]), "rsi": _r2(row["rsi"]), "trend": trend, "score": round(score, 1),
            "parts": {"teknik": ts["tech"], "momentum": round(mom, 1), "hacim": vol},
            "ret90": _r2(ret90 * 100), "recent": [SIGNALS[k][1] for k in recent],
            "exit": [k for k in recent if k in C.CRYPTO_EXIT_SIGNALS and bool(sig[k].iloc[-1])],
            "barDate": df.index[-1].strftime("%Y-%m-%d"),
            "entry": kind, "plan": {"zoneLow": _r2(lo), "zoneHigh": _r2(hi), "stop": _r2(stop), "wait": stretched},
        })
    return out


def _norm(sym):
    return str(sym).replace("/", "").upper()


def run(api, coins: list, state: dict, tag: str, mode: str = "trade") -> list:
    """Kripto emir yönetimi. mode='check' ise emir verilmez."""
    from .trader import _log   # döngüsel içe aktarmayı önle
    msgs = []
    acct = api.account()
    equity = float(acct["equity"])
    day_pl = (equity - float(acct["last_equity"])) / float(acct["last_equity"]) if float(acct["last_equity"]) else 0
    pos = {_norm(p["symbol"]): p for p in api.positions() if p.get("asset_class") == "crypto"}
    orders = [o for o in api.open_orders() if o.get("asset_class") == "crypto"]
    cs = state.setdefault("crypto", {})
    trail = cs.setdefault("trail", {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if mode == "check":
        return msgs
    by = {_norm(c["asym"]): c for c in coins}
    inc = {}
    for c in coins:
        try:
            a = api.asset(c["asym"])
            inc[_norm(c["asym"])] = (float(a.get("price_increment") or 0.01), float(a.get("min_trade_increment") or 1e-6),
                                     float(a.get("min_order_size") or 0))
        except Exception:   # noqa: BLE001
            inc[_norm(c["asym"])] = (0.01, 1e-6, 0)

    def px(sym, v, down=False):
        step = inc[sym][0]
        n = math.floor(v / step) if down else round(v / step)
        return round(n * step, 10)

    def qt(sym, v):
        step = inc[sym][1]
        return round(math.floor(v / step) * step, 10)
    for sym, c in by.items():
        p = pos.get(sym)
        my = [o for o in orders if _norm(o["symbol"]) == sym]
        sells = [o for o in my if o["side"] == "sell"]
        buys = [o for o in my if o["side"] == "buy"]
        price, atr = c["price"], c["atr"]
        if p:
            entry, qty = float(p["avg_entry_price"]), abs(float(p["qty"]))
            t = trail.setdefault(sym, {"hi": max(entry, price), "stop0": entry - 2 * atr, "msg": 0.0})
            t["hi"] = max(t["hi"], price, c.get("high") or price)
            level = round(max(t["stop0"], t["hi"] - C.CRYPTO_TRAIL_ATR * atr), 2)
            stop_o = next((o for o in sells if o.get("type") in ("stop_limit", "stop")), None)
            cur = float(stop_o["stop_price"]) if stop_o and stop_o.get("stop_price") else None
            # a) Fiyat stopun altında (boşluk) ya da teyitli çıkış sinyali: piyasa emriyle kapat
            exit_reason = None
            if cur and price < cur * 0.995:
                exit_reason = f"fiyat stopun (${cur:,.2f}) altına indi, stop-limit dolmadı"
            elif price <= level and not stop_o:
                exit_reason = f"stop seviyesinin (${level:,.2f}) altında, stop emri yoktu"
            elif price < level:
                exit_reason = f"fiyat iz süren stop seviyesinin (${level:,.2f}) altında"
            elif c["exit"] and cs.get(f"exitday|{sym}") != c["barDate"]:
                exit_reason = "kapanışta SAT sinyali: " + ", ".join(SIGNALS[k][1] for k in c["exit"])
            if exit_reason:
                for o in sells:
                    _safe(api.cancel, o["id"])
                try:
                    api.close_position(sym)
                    cs[f"exitday|{sym}"] = c["barDate"]
                    pl = float(p.get("unrealized_pl", 0))
                    msgs.append(f"{tag} {TAG} 🔴 <b>{sym}</b> SATIŞ ({exit_reason}) · {qty:g} · K/Z ${pl:,.2f}")
                    _log({"sym": sym, "side": "sell", "qty": qty, "reason": exit_reason, "pl": pl, "asset": "crypto"})
                    trail.pop(sym, None)
                except RuntimeError as e:
                    msgs.append(f"⚠️ {sym} kapatılamadı: {e}")
                continue
            # b) Stop yoksa / adet yanlışsa kur; varsa yalnız yukarı taşı
            sq = qt(sym, qty)          # kripto ücreti coin olarak kesildiği için pozisyon adedi emirden biraz az olabilir
            if not stop_o or abs(float(stop_o.get("qty") or sq) - sq) > inc[sym][1] / 2:
                for o in sells:
                    _safe(api.cancel, o["id"])
                try:
                    api.crypto_stop(c["asym"], sq, px(sym, level), px(sym, level * (1 - C.CRYPTO_STOP_LIMIT_GAP), True),
                                    f"bt-cs-{sym}-{datetime.now().strftime('%m%d%H%M')}")
                    _log({"sym": sym, "side": "stop_restore", "qty": qty, "stop": level, "asset": "crypto"})
                    msgs.append(f"{tag} {TAG} 🛡 <b>{sym}</b> stop kuruldu: {sq:g} @ ${level:,.2f} (stop-limit)")
                except RuntimeError as e:
                    msgs.append(f"⚠️ {sym} stop kurulamadı: {e}")
            elif level > cur * 1.002 and level < price:
                try:
                    api.replace_stop_limit(stop_o["id"], px(sym, level), px(sym, level * (1 - C.CRYPTO_STOP_LIMIT_GAP), True))
                    _log({"sym": sym, "side": "trail", "stop": level, "old": cur, "asset": "crypto"})
                    if (cur < entry <= level) or level - t.get("msg", 0) >= 0.5 * atr:
                        t["msg"] = level
                        msgs.append(f"{tag} {TAG} 🔒 <b>{sym}</b> iz süren stop: ${cur:,.2f} → ${level:,.2f} "
                                    f"(girişe göre {(level / entry - 1) * 100:+.1f}%)")
                except RuntimeError as e:
                    print(e)
            continue
        trail.pop(sym, None)
        # c) Pozisyon yok: bekleyen alışı yönet / yeni alış
        for o in buys:
            stale = not str(o.get("client_order_id", "")).startswith(f"bt-c-{today}")
            old = float(o.get("limit_price") or 0)
            if stale or not c["entry"] or price < c["plan"]["zoneLow"] or (old and abs(c["plan"]["zoneHigh"] - old) / old > 0.01):
                _safe(api.cancel, o["id"])
                buys = []
        if buys or not c["entry"] or day_pl <= -C.DAILY_LOSS_LIMIT:
            continue
        if cs.get(f"exitday|{sym}") == c["barDate"]:
            continue   # aynı gün çıkılan coine yeniden girme
        pl_ = c["plan"]
        limit = px(sym, min(price, pl_["zoneHigh"]), True)
        if price < pl_["zoneLow"] or limit <= pl_["stop"]:
            continue
        risk_ps = limit - pl_["stop"]
        value = min(equity * C.CRYPTO_RISK / risk_ps * limit, equity * C.CRYPTO_ALLOC / 2)
        qty = qt(sym, value / limit)
        if qty * limit < 10 or qty < inc[sym][2]:
            continue
        try:
            api.crypto_buy(c["asym"], qty, limit, f"bt-c-{today}-{sym}-{datetime.now().strftime('%H%M')}")
            _log({"sym": sym, "side": "buy_limit", "qty": qty, "limit": limit, "stop": pl_["stop"], "asset": "crypto",
                  "decision": {"score": c["score"], "parts": c["parts"], "entryType": c["entry"], "recent": c["recent"]}})
            msgs.append(f"{tag} {TAG} 🟢 <b>{sym}</b> LİMİTLİ ALIŞ · {qty:g} @ ${limit:,.2f} (≈${qty * limit:,.0f})\n"
                        f"   Stop ${pl_['stop']:,.2f} · risk ≈${qty * risk_ps:,.0f} · iz süren stop {C.CRYPTO_TRAIL_ATR} ATR\n"
                        f"   Puan {c['score']:.0f}/100 (teknik {c['parts']['teknik']}/40 · momentum {c['parts']['momentum']:.0f}/20 · "
                        f"hacim {c['parts']['hacim']:.0f}/5) · giriş: {'AL sinyali' if c['entry'] == 'sinyal' else 'yüksek puan + geri çekilme'}")
        except RuntimeError as e:
            msgs.append(f"⚠️ {sym} alış emri verilemedi: {e}")
    return msgs


def _safe(fn, *a):
    try:
        fn(*a)
    except RuntimeError as e:
        print(e)


def summary_lines(coins, positions):
    L = [f"\n{TAG} <b>Kripto</b>"]
    for c in coins:
        L.append(f"• {c['t'].split('-')[0]} ${c['price']:,.2f} · {c['trend']} · RSI {c['rsi']:.0f} · Puan {c['score']:.0f}"
                 + (f" · <b>aday ({c['entry']})</b>" if c["entry"] else ""))
    for p in positions:
        L.append(f"   Pozisyon {p['symbol']}: {float(p['qty']):g} · K/Z ${float(p['unrealized_pl']):,.2f} "
                 f"({float(p['unrealized_plpc']) * 100:+.1f}%)")
    return L
