"""Günlük işlem (ORB) ve İslami filtre testleri – ağsız, sahte Alpaca ile."""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tests.test_offline as TO  # noqa: E402
from engine import config as C  # noqa: E402
from engine import daytrade as DT  # noqa: E402
from engine import sharia, trader  # noqa: E402

NY = DT.NY
DAY = pd.Timestamp("2026-11-16", tz=NY)


def bars(path, vols=None, day=DAY):
    """path: [(o,h,l,c), ...] 09:30'dan itibaren 5 dakikalık mumlar."""
    idx = [day + pd.Timedelta(hours=9, minutes=30) + i * DT.BAR for i in range(len(path))]
    v = vols or [100000] * len(path)
    return pd.DataFrame([{"o": o, "h": h, "l": l, "c": c, "v": vv} for (o, h, l, c), vv in zip(path, v)], index=idx)


CTX = {"prev_close": 100.0, "atr": 3.0, "ema20": 98, "ema50": 95, "trend_ok": True, "earnings_today": False,
       "or_vol_avg": 150000.0}
# Açılış aralığı 100-101, 4. mum 101.4'te yeşil kapanış (kırılım)
BASE = [(100, 101, 100, 100.5), (100.5, 100.9, 100.2, 100.6), (100.6, 100.8, 100.3, 100.7), (100.7, 101.5, 100.7, 101.4)]


def test_rules():
    p = DT.evaluate(bars(BASE), CTX)
    assert p and abs(p["orh"] - 101) < 1e-9 and abs(p["orl"] - 100) < 1e-9, p
    assert abs(p["stop"] - 100.35) < 1e-6, p           # max(100, 101.4 - 0.35*3=100.35)
    assert abs(p["tp"] - (101.4 + 2 * (101.4 - 100.35))) < 1e-6
    assert DT.evaluate(bars(BASE), dict(CTX, earnings_today=True)) is None
    assert DT.evaluate(bars(BASE), dict(CTX, trend_ok=False)) is None
    assert DT.evaluate(bars(BASE), dict(CTX, prev_close=95)) is None            # boşluk > %4
    assert DT.evaluate(bars(BASE), dict(CTX, or_vol_avg=400000)) is None        # düşük göreceli hacim
    assert DT.evaluate(bars(BASE), CTX, spy_ok=False) is None                   # piyasa filtresi
    far = BASE[:3] + [(100.7, 102.2, 100.7, 102.0)]                              # 1.0 > 0.25*3 ATR: kovalama
    assert DT.evaluate(bars(far), CTX) is None
    red = BASE[:3] + [(101.45, 101.5, 101.0, 101.4)]                             # kırmızı mum
    assert DT.evaluate(bars(red), CTX) is None
    assert DT.evaluate(bars(BASE[:3]), CTX) is None                              # aralık tamamlanmadan
    late = bars(BASE[:3] + [(100.7, 100.9, 100.6, 100.8)] * 22 + [(100.7, 101.5, 100.7, 101.4)])
    assert late.index[-1] >= DAY + pd.Timedelta(hours=11, minutes=30) and DT.evaluate(late, CTX) is None
    print("Kurallar: OK")


def test_simulate():
    up = BASE + [(101.4, 101.6, 101.3, 101.5), (101.5, 102.5, 101.5, 102.4), (102.4, 104.0, 102.3, 103.8)]
    r = DT.simulate_day(bars(up), CTX)
    assert r and r["why"] == "kâr al" and r["r"] > 1.5, r
    dn = BASE + [(101.4, 101.45, 100.9, 101.0), (101.0, 101.0, 100.0, 100.1)]
    r = DT.simulate_day(bars(dn), CTX)
    assert r and r["why"] == "stop" and r["r"] < -0.9, r
    # +1R sonrası geri dönüş: stop girişe çekildiği için ~0R
    be = BASE + [(101.4, 101.45, 101.3, 101.4), (101.4, 102.6, 101.4, 102.5), (102.5, 102.5, 100.0, 100.2)]
    r = DT.simulate_day(bars(be), CTX)
    assert r and r["why"] == "stop" and abs(r["r"]) < 0.1, r
    flat = BASE + [(101.4, 101.6, 101.3, 101.5)] * 80
    r = DT.simulate_day(bars(flat), CTX)
    assert r and r["why"] == "gün sonu", r
    print("Simülasyon: OK")


class FakeApi:
    paper = True

    def __init__(self, cash=100000.0):
        self.calls, self.pos, self.open, self.hist, self.cash = [], [], [], [], cash

    def account(self):
        return {"equity": "100000", "last_equity": "100000", "cash": str(self.cash), "buying_power": "200000"}

    def positions(self):
        return self.pos

    def open_orders(self):
        return self.open

    def orders(self, status="all", after=None, limit=500):
        return self.hist

    def configure(self, **k):
        self.calls.append(("config", k))
        return k

    def day_buy(self, sym, qty, limit, stop, tp, cid):
        self.calls.append(("buy", sym, qty, limit, stop, tp, cid))
        self.hist.append({"symbol": sym, "client_order_id": cid, "side": "buy", "filled_qty": "0"})
        self.open.append({"id": "P" + sym, "symbol": sym, "client_order_id": cid, "side": "buy", "qty": str(qty),
                          "limit_price": str(limit), "status": "new", "submitted_at": "2026-11-16T14:50:21Z"})

    def cancel(self, oid):
        self.calls.append(("cancel", oid))

    def replace_stop(self, oid, stop):
        self.calls.append(("stop", oid, stop))

    def close_position(self, sym):
        self.calls.append(("close", sym))
        self.pos = [p for p in self.pos if p["symbol"] != sym]

    def stop_sell(self, *a):
        self.calls.append(("stop_sell",) + a)

    def bracket_buy(self, *a):
        self.calls.append(("swingbuy",) + a)


def make_trader(api, now):
    rng = np.random.default_rng(3)
    prices, fund = {}, {}
    for i, t in enumerate(C.CANDIDATES):
        df = TO.fake_prices(i, 400)
        df = df[df.index < pd.Timestamp(DAY.date())]
        prices[t] = df
        mc = float(rng.lognormal(26, 1))
        fund[t] = {"sector": "Technology", "industry": "Software", "marketCap": mc, "totalDebt": 0.0, "totalCash": 0.0}
    dt = DT.DayTrader(api, DAY + pd.Timedelta(hours=9, minutes=30), DAY + pd.Timedelta(hours=16), now_fn=lambda: now[0])
    dt.state = DT.load_state("test-" + str(id(dt)))
    dt.wait = 0
    dt.setup(prices=prices, fund=fund, hist={})
    for t in dt.ctx:            # testte tüm evren uygun bağlamda olsun
        dt.ctx[t] = dict(CTX)
    return dt


def test_live():
    now = [DAY + pd.Timedelta(hours=9, minutes=50, seconds=20)]
    api = FakeApi()
    dt = make_trader(api, now)
    assert ("config", {"no_shorting": True, "max_margin_multiplier": "1"}) in api.calls
    syms = dt.universe[:5]
    today = {s: bars(BASE, [200000] * 4) for s in syms}
    today[C.BENCHMARK] = bars(BASE)
    # İlk sembol swing botta: dokunulmamalı
    api.pos = [{"symbol": syms[0], "qty": "3", "avg_entry_price": "90", "current_price": "100", "unrealized_pl": "30",
                "market_value": "300"}]
    dt._account()
    dt.scan(today)
    buys = [c for c in api.calls if c[0] == "buy"]
    assert syms[0] not in [b[1] for b in buys], buys
    assert len(buys) == C.DT_MAX_POSITIONS, buys
    cap = 100000 * C.DAYTRADE_ALLOC
    for _, sym, qty, limit, stop, tp, cid in buys:
        assert cid.startswith("dt-") and stop < limit < tp
        assert qty * (limit - stop) <= cap * C.DT_RISK + 1, (qty, limit, stop)
        assert qty * limit <= cap / C.DT_MAX_POSITIONS + 1
    assert any("GÜNLÜK ALIŞ" in m for m in dt.msgs)
    # Aynı mum tekrar taranırsa yeni alım yok
    n = len(buys)
    dt._account()
    dt.scan(today)
    assert len([c for c in api.calls if c[0] == "buy"]) == n
    print("Canlı giriş:", dt.msgs[0].split("\n")[0])
    # 3 dakikada dolmayan emir iptal edilir
    now[0] = DAY + pd.Timedelta(hours=9, minutes=54)
    dt._account()
    dt.manage()
    assert ("cancel", "P" + buys[0][1]) in api.calls and any("dolmadı" in m for m in dt.msgs)

    # Nakit yoksa (kaldıraç yok) alım yok
    api2 = FakeApi(cash=0)
    dt2 = make_trader(api2, now)
    dt2._account()
    dt2.scan(today)
    assert not [c for c in api2.calls if c[0] == "buy"]

    # +1R: stop girişe çekilir
    s = buys[0][1]
    api.pos = [{"symbol": s, "qty": "10", "avg_entry_price": "101.4", "current_price": "102.6", "unrealized_pl": "12",
                "market_value": "1026"}]
    api.open = [{"id": "L1", "symbol": s, "side": "sell", "type": "stop", "stop_price": "100.35", "qty": "10"}]
    api.hist.append({"symbol": s, "client_order_id": "dt-x", "side": "buy", "filled_qty": "10", "filled_avg_price": "101.4"})
    dt._account()
    dt.manage()
    assert ("stop", "L1", 101.4) in api.calls, api.calls
    dt.state["open"] = [s]

    # Swing bot dt pozisyonuna karışmamalı (iz süren stop / koruma yok)
    summary = {"lastBar": "2026-11-16", "picks": [s]}
    by = {s: {"t": s, "atr": 2.0, "high": 103, "price": 102.6, "signals": [], "fund": {"daysToEarnings": None},
              "plan": {"zoneLow": 100, "zoneHigh": 103, "stop": 97, "t1": 105, "t2": 108}, "recent": [], "score": 90}}
    m, port = trader.run(summary, by, "intraday", {"sent": {}}, api)
    assert not [c for c in api.calls if c[0] in ("stop_sell", "swingbuy") or (c[0] == "stop" and c[1] != "L1")], api.calls
    assert not port["positions"]

    # Pozisyon kâr al ile kapandı -> bildirim ve K/Z
    api.pos, api.open = [], []
    api.hist.append({"symbol": s, "client_order_id": "leg", "side": "sell", "filled_qty": "10", "filled_avg_price": "103.5"})
    dt._account()
    dt.manage()
    assert s in dt.state["closed"] and abs(dt.state["closed"][s] - 21.0) < 1e-6, dt.state["closed"]

    # Gün sonu: kalan günlük pozisyonlar kapatılır, özet gönderilir
    other = buys[1][1]
    api.pos = [{"symbol": other, "qty": "5", "avg_entry_price": "101.4", "current_price": "101", "unrealized_pl": "-2",
                "market_value": "505"}]
    api.open = [{"id": "L2", "symbol": other, "side": "sell", "type": "stop", "stop_price": "100.35"}]
    api.hist.append({"symbol": other, "client_order_id": "dt-y", "side": "buy", "filled_qty": "5", "filled_avg_price": "101.4"})
    now[0] = DAY + pd.Timedelta(hours=15, minutes=50, seconds=5)
    assert dt.step() is True
    assert ("close", other) in api.calls and ("cancel", "L2") in api.calls
    rep = [m for m in dt.msgs if "Günlük işlem özeti" in m]
    assert rep and "Toplam" in rep[0], dt.msgs
    print(rep[0])

    # Kayıp limiti
    dt3 = make_trader(FakeApi(), [DAY + pd.Timedelta(hours=10)])
    dt3.state["closed"] = {"X": -cap * C.DT_DAILY_LOSS - 1}
    dt3._account()
    dt3.scan(today)
    assert not [c for c in dt3.api.calls if c[0] == "buy"] and any("limit" in m for m in dt3.msgs)

    # Yedek kapatma (tarama iş akışı)
    api4 = FakeApi()
    api4.hist = [{"symbol": "AAA", "client_order_id": "dt-1", "side": "buy"}]
    api4.pos = [{"symbol": "AAA", "qty": "1"}, {"symbol": "BBB", "qty": "1"}]
    api4.open = [{"id": "z", "symbol": "AAA", "side": "sell"}]
    m4 = DT.safety_flatten(datetime(2026, 11, 16, 15, 55, tzinfo=NY), api4)
    assert ("close", "AAA") in api4.calls and ("close", "BBB") not in api4.calls and m4
    assert DT.safety_flatten(datetime(2026, 11, 16, 12, 0, tzinfo=NY), FakeApi()) == []
    print("Canlı işlem: OK")


def test_sharia():
    f = {"sector": "Technology", "industry": "Software", "marketCap": 1e12, "totalDebt": 1e11, "totalCash": 5e10}
    assert sharia.check("X", f)["ok"]
    assert not sharia.check("X", dict(f, totalDebt=4e11))["ok"]
    assert not sharia.check("X", dict(f, totalCash=3.5e11))["ok"]
    assert sharia.check("V", dict(f, sector="Financial Services"))["reason"] == "konvansiyonel finans"
    assert sharia.check("LMT", dict(f, industry="Aerospace & Defense"))["reason"] == "savunma sanayi"
    assert sharia.check("PM", dict(f, industry="Tobacco"))["reason"] == "tütün"
    assert not sharia.check("MCD", f)["ok"]
    r = sharia.check("X", {"sector": "Technology", "marketCap": 1e12})
    assert not r["ok"] and not r["hard"]
    # 36 aylık ortalama: fiyat 3 yılda ikiye katlandıysa ortalama piyasa değeri daha düşük -> oran daha yüksek
    closes = pd.Series(np.linspace(50, 100, 756))
    r = sharia.check("X", dict(f, totalDebt=2.5e11), closes)
    assert not r["ok"] and r["debt"] > 0.3, r
    # Swing bot: elde tutulan hisse filtreden kesin olarak çıktıysa kapanışta satılır; veri eksikse satılmaz
    api = FakeApi()
    api.pos = [{"symbol": "AAA", "qty": "2", "avg_entry_price": "10", "current_price": "11", "unrealized_pl": "2",
                "unrealized_plpc": "0.1"}, {"symbol": "BBB", "qty": "2", "avg_entry_price": "10", "current_price": "11",
                "unrealized_pl": "2", "unrealized_plpc": "0.1"}]
    api.open = [{"id": "s1", "symbol": "AAA", "side": "sell", "type": "stop", "stop_price": "9", "qty": "2"},
                {"id": "s2", "symbol": "BBB", "side": "sell", "type": "stop", "stop_price": "9", "qty": "2"}]
    mk = lambda t, sh: {"t": t, "atr": 0.5, "high": 11, "price": 11, "signals": [], "recent": [],
                        "fund": {"daysToEarnings": None}, "sharia": sh}
    by = {"AAA": mk("AAA", {"ok": False, "hard": True, "reason": "borç/piyasa değeri %45"}),
          "BBB": mk("BBB", {"ok": False, "hard": False, "reason": "veri yok"})}
    m, _ = trader.run({"lastBar": "2026-11-16", "picks": []}, by, "post", {"sent": {}}, api)
    assert ("close", "AAA") in api.calls and ("close", "BBB") not in api.calls, api.calls
    assert any("İslami filtreden çıktı" in x for x in m), m
    print("İslami filtre: OK")


if __name__ == "__main__":
    test_sharia()
    test_rules()
    test_simulate()
    test_live()
    print("Tüm günlük işlem testleri geçti.")
