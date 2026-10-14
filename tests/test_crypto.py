"""Kripto bölümü testleri (sahte Alpaca)."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tests.test_offline as T  # noqa: E402
from engine import config as C, crypto  # noqa: E402


class FakeC:
    paper = True

    def __init__(self, positions=None, orders=None, equity=100000):
        self.calls, self._p, self._o, self.eq = [], positions or [], orders or [], equity

    def account(self):
        return {"equity": str(self.eq), "last_equity": str(self.eq), "crypto_status": "ACTIVE"}

    def positions(self):
        return self._p

    def open_orders(self):
        return self._o

    def asset(self, sym):
        return {"price_increment": "0.01", "min_trade_increment": "0.000001", "min_order_size": "0.0001"}

    def crypto_buy(self, sym, qty, limit, cid):
        self.calls.append(("buy", sym, qty, limit))

    def crypto_stop(self, sym, qty, stop, limit, cid):
        self.calls.append(("stop", sym, qty, stop, limit))

    def replace_stop_limit(self, oid, stop, limit):
        self.calls.append(("replace", oid, stop, limit))

    def cancel(self, oid):
        self.calls.append(("cancel", oid))

    def close_position(self, sym):
        self.calls.append(("close", sym))


def coin(**kw):
    c = {"t": "BTC-USD", "asym": "BTC/USD", "price": 100000.0, "high": 101000.0, "chg": 1.0, "atr": 3000.0,
         "ema20": 98000.0, "ema50": 95000.0, "ema200": 80000.0, "rsi": 58.0, "trend": "Yükseliş", "score": 82.0,
         "parts": {"teknik": 35, "momentum": 15.0, "hacim": 3.0}, "ret90": 20.0, "recent": [], "exit": [],
         "barDate": "2026-10-13", "entry": "trend",
         "plan": {"zoneLow": 98000.0, "zoneHigh": 99500.0, "stop": 93500.0, "wait": True}}
    c.update(kw)
    return c


def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # analyze sentetik veriyle çalışmalı
    prices = {"BTC-USD": T.fake_prices(3, 800), "ETH-USD": T.fake_prices(4, 800)}
    res = crypto.analyze(prices)
    assert len(res) == 2 and all(0 <= r["score"] <= 100 for r in res), res
    print("ANALİZ:", [(r["t"], r["score"], r["trend"], r["entry"]) for r in res])

    # 1) Yeni alış: limit bölge üstünde, risk ≤ %0.5, değer ≤ %5
    f = FakeC(); st = {"sent": {}}
    m = crypto.run(f, [coin()], st, "T")
    b = [x for x in f.calls if x[0] == "buy"][0]
    _, sym, qty, limit = b
    assert limit <= 99500 and qty * limit <= 100000 * C.CRYPTO_ALLOC / 2 + 1, b
    assert qty * (limit - 93500) <= 100000 * C.CRYPTO_RISK + 1, b
    print("ALIŞ:", m[0].split("\n")[0])

    # 2) Pozisyon var, stop yok: stop-limit kurulur (adet ücret sonrası pozisyon adedi)
    pos = [{"symbol": "BTCUSD", "asset_class": "crypto", "qty": "0.0499", "avg_entry_price": "99000",
            "current_price": "100000", "unrealized_pl": "50", "unrealized_plpc": "0.01"}]
    f = FakeC(positions=pos); st = {"sent": {}}
    crypto.run(f, [coin()], st, "T")
    s = [x for x in f.calls if x[0] == "stop"][0]
    assert s[2] == 0.0499 and s[4] < s[3], s
    print("STOP:", s)

    # 3) Stop var ve iz süren seviye daha yüksek: yukarı taşınır
    od = [{"id": "s1", "symbol": "BTC/USD", "asset_class": "crypto", "side": "sell", "type": "stop_limit",
           "qty": "0.0499", "stop_price": "92000", "limit_price": "90160"}]
    f = FakeC(positions=pos, orders=od); st = {"sent": {}, "crypto": {"trail": {"BTCUSD": {"hi": 110000, "stop0": 93000, "msg": 0}}}}
    pos3 = [dict(pos[0], current_price="105000")]
    f = FakeC(positions=pos3, orders=od)
    m3 = crypto.run(f, [coin(price=105000.0, high=106000.0)], st, "T")
    r = [x for x in f.calls if x[0] == "replace"]
    assert r and abs(r[0][2] - (110000 - 3 * 3000)) < 0.02, f.calls
    print("İZ SÜREN:", r[0], m3)
    # 3b) Fiyat iz süren seviyenin altına düşmüş (taramalar arası sert düşüş): kapat
    f = FakeC(positions=pos, orders=od)
    st = {"sent": {}, "crypto": {"trail": {"BTCUSD": {"hi": 110000, "stop0": 93000, "msg": 0}}}}
    crypto.run(f, [coin()], st, "T")
    assert ("close", "BTCUSD") in f.calls, f.calls

    # 4) Fiyat stopun altına boşlukla indi, stop-limit dolmadı: kapat
    low = [dict(pos[0], current_price="90000")]
    f = FakeC(positions=low, orders=od); st = {"sent": {}}
    m4 = crypto.run(f, [coin(price=90000.0, high=91000.0)], st, "T")
    assert ("close", "BTCUSD") in f.calls and ("cancel", "s1") in f.calls, f.calls
    print("BOŞLUK:", m4[0])

    # 5) Dünden kalan alış emri iptal edilir
    old = [{"id": "b1", "symbol": "BTC/USD", "asset_class": "crypto", "side": "buy", "type": "limit",
            "limit_price": "99500", "client_order_id": "bt-c-2020-01-01-BTCUSD-1200"}]
    f = FakeC(orders=old); st = {"sent": {}}
    crypto.run(f, [coin()], st, "T")
    assert ("cancel", "b1") in f.calls and [x for x in f.calls if x[0] == "buy"], f.calls

    # 6) Bugünkü bekleyen emir yerinde: dokunma, yeni emir yok
    cur = [dict(old[0], client_order_id=f"bt-c-{today}-BTCUSD-1200")]
    f = FakeC(orders=cur); st = {"sent": {}}
    crypto.run(f, [coin()], st, "T")
    assert not f.calls, f.calls

    # 7) Aday değilse emir yok; günlük kayıp limitinde alış yok
    f = FakeC(); crypto.run(f, [coin(entry=None)], {"sent": {}}, "T"); assert not f.calls
    class Loss(FakeC):
        def account(self):
            return {"equity": "97000", "last_equity": "100000", "crypto_status": "ACTIVE"}
    f = Loss(); crypto.run(f, [coin()], {"sent": {}}, "T"); assert not [x for x in f.calls if x[0] == "buy"]

    # 8) check modunda hiçbir emir yok
    f = FakeC(positions=pos); crypto.run(f, [coin()], {"sent": {}}, "T", mode="check"); assert not f.calls
    print("Tüm kripto testleri geçti.")


if __name__ == "__main__":
    main()
