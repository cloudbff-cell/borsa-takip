"""Sahte Alpaca istemcisiyle otomatik işlem testi."""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tests.test_offline as T  # noqa: E402
from engine import run, trader  # noqa: E402


class FakeAlpaca:
    paper = True

    def __init__(self, day_pl=0.0, positions=None, entries=None):
        self.calls, self.day_pl, self._pos, self._entries = [], day_pl, positions or [], entries or []

    def account(self):
        return {"equity": str(100000 * (1 + self.day_pl)), "last_equity": "100000", "buying_power": "200000"}

    def positions(self):
        return self._pos

    def open_orders(self):
        return [{"id": "o1", "symbol": p["symbol"], "side": "sell", "type": "limit", "legs": [
            {"id": "o2", "symbol": p["symbol"], "side": "sell", "type": "stop", "stop_price": "1"}]} for p in self._pos] \
            + self._entries

    def bracket_buy(self, *a):
        self.calls.append(("buy",) + a)

    def replace_stop(self, *a):
        self.calls.append(("stop",) + a)

    def cancel(self, oid):
        self.calls.append(("cancel", oid))

    def close_position(self, sym):
        self.calls.append(("close", sym))

    def stop_sell(self, sym, qty, stop, cid):
        self.calls.append(("stop_sell", sym, qty, stop))


def main():
    import numpy as np
    from engine import config as C
    tick = C.CANDIDATES + [C.BENCHMARK]
    full = {t: T.fake_prices(i) for i, t in enumerate(tick)}
    prices = {t: df.tail(756) for t, df in full.items()}
    monthly = {t: df.resample("ME").last() for t, df in full.items()}
    import tests.test_offline as TO
    # temel veriyi test_offline ile aynı şekilde üret
    rng = np.random.default_rng(1)
    fund = {t: {"shortName": t, "sector": TO.SECTORS[i % len(TO.SECTORS)], "marketCap": float(rng.lognormal(26, 1)),
                "earningsHistory": []} for i, t in enumerate(C.CANDIDATES)}
    C.ENTRY_SIGNAL_MIN_SCORE, C.ENTRY_TREND_MIN_SCORE = 30, 50   # sentetik veride yeterli aday olsun
    now = datetime.now(run.NY).replace(hour=11)
    summary, by = run.build("intraday", now, prices=prices, fund=fund, monthly=monthly)
    # İlk adayı alım bölgesine sok
    t0 = summary["picks"][0]
    by[t0]["price"] = by[t0]["plan"]["zoneLow"]
    state = {"sent": {}}

    f = FakeAlpaca()
    msgs, port = trader.run(summary, by, "intraday", state, f)
    buys = [c for c in f.calls if c[0] == "buy"]
    assert buys and buys[0][1] == t0, f.calls
    sym, qty, limit, stop, tp = buys[0][1:6]
    assert limit <= by[t0]["plan"]["zoneHigh"] + 1e-9
    risk = qty * (limit - stop)
    assert risk <= 100000 * C.RISK_PER_TRADE + 1, risk
    assert qty * by[t0]["price"] <= 100000 * C.MAX_POSITION_PCT + 1
    print("ALIŞ:", msgs[0].replace("\n", " | "))

    # Aynı gün ikinci çalıştırmada tekrar alım olmamalı
    f2 = FakeAlpaca()
    trader.run(summary, by, "intraday", state, f2)
    assert not [c for c in f2.calls if c[0] == "buy" and c[1] == t0]

    # Günlük kayıp limiti
    f3 = FakeAlpaca(day_pl=-0.025)
    m3, _ = trader.run(summary, by, "intraday", {"sent": {}}, f3)
    assert not [c for c in f3.calls if c[0] == "buy"] and "limit" in m3[0]
    print("KAYIP LİMİTİ:", m3[0])

    # Maksimum pozisyon: 5 pozisyon varken alım yok
    pos = [{"symbol": f"X{i}", "qty": "1", "avg_entry_price": "10", "current_price": "10", "unrealized_pl": "0",
            "unrealized_plpc": "0"} for i in range(C.MAX_POSITIONS)]
    f4 = FakeAlpaca(positions=pos)
    trader.run(summary, by, "intraday", {"sent": {}}, f4)
    assert not [c for c in f4.calls if c[0] == "buy"]

    # Kapanışta SAT sinyali olan pozisyon kapatılmalı
    s_sell = next((s for s in summary["stocks"] if s["signals"]), None)
    if s_sell is None:
        s_sell = summary["stocks"][0]
    s_sell["signals"] = [{"code": "S_TREND", "name": "Trend bozulması", "side": "SAT"}]
    pos = [{"symbol": s_sell["t"], "qty": "3", "avg_entry_price": "10", "current_price": "9", "unrealized_pl": "-3",
            "unrealized_plpc": "-0.1"}]
    f5 = FakeAlpaca(positions=pos)
    m5, _ = trader.run(summary, by, "post", {"sent": {}}, f5)
    assert ("close", s_sell["t"]) in f5.calls and ("cancel", "o2") in f5.calls, f5.calls
    assert not [c for c in f5.calls if c[0] == "stop_sell"], f5.calls
    print("ÇIKIŞ:", m5[0])

    # Hedef 1 görülünce stop başa baş
    s = summary["stocks"][1]
    entry = s["price"] - 3 * s["atr"]
    pos = [{"symbol": s["t"], "qty": "3", "avg_entry_price": str(entry), "current_price": str(s["price"]),
            "unrealized_pl": "1", "unrealized_plpc": "0.1"}]
    f6 = FakeAlpaca(positions=pos)
    trader.run(summary, by, "intraday", {"sent": {}}, f6)
    st6 = [c for c in f6.calls if c[0] == "stop" and c[1] == "o2"]
    exp = round(max(s["price"], s["high"]) - C.TRAIL_ATR * s["atr"], 2)
    assert st6 and abs(st6[0][2] - exp) < 0.02, (f6.calls, exp)
    print("İZ SÜREN STOP:", st6[0][2])

    # Stop yalnız yukarı: mevcut stop zaten daha yüksekse dokunma
    class HighStop(FakeAlpaca):
        def open_orders(self):
            return [{"id": "o9", "symbol": s["t"], "side": "sell", "type": "stop", "stop_price": str(s["price"] * 0.99)}]
    f11 = HighStop(positions=pos)
    trader.run(summary, by, "intraday", {"sent": {}}, f11)
    assert not [c for c in f11.calls if c[0] == "stop"], f11.calls

    # Stop emri yoksa: koruyucu stop yeniden kurulur (fiyat seviyenin üstünde)
    class NoStop(FakeAlpaca):
        def open_orders(self):
            return []
    f12 = NoStop(positions=pos)
    m12, _ = trader.run(summary, by, "intraday", {"sent": {}}, f12)
    ss = [c for c in f12.calls if c[0] == "stop_sell"]
    lvl = round(max(entry - 2 * s["atr"], max(s["price"], s["high"]) - C.TRAIL_ATR * s["atr"]), 2)
    assert ss and ss[0][1] == s["t"] and ss[0][2] == 3 and abs(ss[0][3] - lvl) < 0.02, (f12.calls, lvl)
    assert any("koruyucu stop yeniden kuruldu" in m for m in m12)
    print("KORUMA:", [m for m in m12 if "koruyucu" in m][0])

    # Stop yok ve fiyat seviyenin altında: pozisyon kapatılır
    low = [dict(pos[0], current_price=str(entry - 3 * s["atr"]))]
    by_low = dict(by); by_low[s["t"]] = dict(s, price=entry - 3 * s["atr"], high=entry - 2.9 * s["atr"])
    f13 = NoStop(positions=low)
    m13, _ = trader.run(summary, by_low, "intraday", {"sent": {}}, f13)
    assert ("close", s["t"]) in f13.calls and not [c for c in f13.calls if c[0] == "stop_sell"], f13.calls

    # Stop adedi pozisyonla uyuşmuyor: eski emir iptal, tam adetle yeniden
    class WrongQty(FakeAlpaca):
        def open_orders(self):
            return [{"id": "w1", "symbol": s["t"], "side": "sell", "type": "stop", "qty": "1", "stop_price": str(entry)}]
    f14 = WrongQty(positions=pos)
    trader.run(summary, by, "intraday", {"sent": {}}, f14)
    ss = [c for c in f14.calls if c[0] == "stop_sell"]
    assert ("cancel", "w1") in f14.calls and ss and ss[0][2] == 3 and ss[0][3] >= entry, f14.calls

    # Kapanış (market) emri yoldaysa dokunma
    class Closing(FakeAlpaca):
        def open_orders(self):
            return [{"id": "m1", "symbol": s["t"], "side": "sell", "type": "market", "qty": "3"}]
    f15 = Closing(positions=pos)
    trader.run(summary, by, "intraday", {"sent": {}}, f15)
    assert not [c for c in f15.calls if c[0] in ("stop_sell", "cancel", "close")], f15.calls

    # Kâr al yoksa 'oto' emri, varsa 'bracket'
    a = trader.Alpaca.__new__(trader.Alpaca)
    bodies = []
    a._r = lambda m, path, **kw: bodies.append(kw["json"])
    a.bracket_buy("X", 1, 10.0, 9.0, None, "c1")
    a.bracket_buy("X", 1, 10.0, 9.0, 12.0, "c2")
    assert bodies[0]["order_class"] == "oto" and "take_profit" not in bodies[0]
    assert bodies[1]["order_class"] == "bracket" and bodies[1]["take_profit"]["limit_price"] == "12.00"
    # Fiyat bölgenin üstündeyken de limitli emir bölge üst sınırına konmalı
    t1 = summary["picks"][1]
    by[t1]["price"] = by[t1]["plan"]["zoneHigh"] * 1.03
    f7 = FakeAlpaca()
    m7, _ = trader.run(summary, by, "intraday", {"sent": {}}, f7)
    b7 = [c for c in f7.calls if c[0] == "buy" and c[1] == t1]
    assert b7 and abs(b7[0][3] - by[t1]["plan"]["zoneHigh"]) < 1e-6, f7.calls
    print("LİMİT:", [m for m in m7 if t1 in m][0].split("\n")[0])

    # Aday listesinden çıkan hissenin bekleyen emri iptal edilmeli; kapanışta kalanlar iptal
    ent = [{"id": "e1", "symbol": "ZZZ", "side": "buy", "client_order_id": "bt-x", "status": "new", "qty": "5", "limit_price": "10"}]
    f8 = FakeAlpaca(entries=ent)
    m8, _ = trader.run(summary, by, "intraday", {"sent": {}}, f8)
    assert ("cancel", "e1") in f8.calls and "artık aday değil" in " ".join(m8)
    ent2 = [{"id": "e2", "symbol": t1, "side": "buy", "client_order_id": "bt-y", "status": "new", "qty": "5",
             "limit_price": str(by[t1]["plan"]["zoneHigh"])}]
    f9 = FakeAlpaca(entries=ent2)
    trader.run(summary, by, "post", {"sent": {}}, f9)
    assert ("cancel", "e2") in f9.calls
    # Seviye %1'den fazla değiştiyse iptal + yeniden koyma
    ent3 = [{"id": "e3", "symbol": t1, "side": "buy", "client_order_id": "bt-z", "status": "new", "qty": "5",
             "limit_price": str(by[t1]["plan"]["zoneHigh"] * 0.95)}]
    f10 = FakeAlpaca(entries=ent3)
    trader.run(summary, by, "intraday", {"sent": {f"entry|{summary['lastBar']}|{t1}": "x"}}, f10)
    assert ("cancel", "e3") in f10.calls and [c for c in f10.calls if c[0] == "buy" and c[1] == t1]
    print("Tüm işlem testleri geçti.")


if __name__ == "__main__":
    main()
