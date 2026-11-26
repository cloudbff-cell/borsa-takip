"""Ana strateji (3 gün düşüş + %2 aşağıdan limit) testleri – sahte Alpaca ile, ağsız."""
import json
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import config as C  # noqa: E402
from engine import dip, kayit, run, trader  # noqa: E402

C.STRATEGY = "dip"


class Fake:
    paper = True

    def __init__(self):
        self.calls, self.pos, self.open, self.hist = [], [], [], []

    def account(self):
        return {"equity": "90000", "last_equity": "90000", "cash": "90000", "buying_power": "120000"}

    def positions(self):
        return self.pos

    def open_orders(self):
        return self.open

    def orders(self, status="all", after=None, limit=500):
        return self.hist

    def configure(self, **k):
        return k

    def bracket_buy(self, sym, qty, lim, stop, tp, cid):
        self.calls.append(("buy", sym, qty, lim, stop, tp, cid))
        o = {"id": "B" + sym, "symbol": sym, "side": "buy", "client_order_id": cid, "status": "new",
             "qty": str(qty), "limit_price": str(lim)}
        self.open.append(o)
        self.hist.append(dict(o, filled_qty="0"))

    def cancel(self, oid):
        self.calls.append(("cancel", oid))
        self.open = [o for o in self.open if o["id"] != oid]

    def close_position(self, s):
        self.calls.append(("close", s))

    def close_all(self):
        self.calls.append(("close_all",))
        self.pos, self.open = [], []

    def replace_stop(self, oid, v):
        self.calls.append(("stop", oid, v))

    def replace_limit(self, oid, v):
        self.calls.append(("tp", oid, v))

    def stop_sell(self, s, q, v, cid):
        self.calls.append(("stop_sell", s, q, v))


def stock(t, signal=True, drop=-4.0, close=100.0, top=True, ok=True):
    return {"t": t, "isTop": top, "sharia": {"ok": ok}, "signals": [], "recent": [], "fund": {"daysToEarnings": None},
            "dip": {"signal": signal, "drop3": drop, "close": close, "limit": round(close * 0.98, 2)}}


def main():
    os.chdir(tempfile.mkdtemp())
    Path("komutlar").mkdir()
    Path("cache").mkdir()
    # Sinyal hesabı
    idx = pd.bdate_range(end="2026-11-13", periods=260)
    c = pd.Series(np.linspace(50, 120, 260), index=idx)
    c.iloc[-4:] = [124, 123, 121, 118]
    v = dip.signal_view(pd.DataFrame({"Close": c}))
    assert v["signal"] and v["limit"] == round(118 * 0.98, 2) and v["drop3"] < 0, v
    c.iloc[-2] = 117
    assert not dip.signal_view(pd.DataFrame({"Close": c}))["signal"]            # düşüş zinciri bozuk
    c2 = pd.Series(np.linspace(200, 100, 260), index=idx)
    assert not dip.signal_view(pd.DataFrame({"Close": c2}))["signal"]           # 200 gün ortalamasının altında

    # Kapanış sonrası: sinyallere limitli alış (en büyük düşüş önce, en fazla 3, İslami/ilk 50 şartı)
    stocks = [stock("AAA", drop=-3), stock("BBB", drop=-7), stock("CCC", drop=-5), stock("DDD", drop=-6),
              stock("EEE", drop=-9, ok=False), stock("FFF", drop=-9, top=False), stock("GGG", signal=False)]
    summary = {"lastBar": "2026-11-13", "stocks": stocks, "picks": []}
    by = {s["t"]: s for s in stocks}
    api, state = Fake(), {"sent": {}}
    msgs, port = trader.run(summary, by, "post", state, api)
    buys = [x for x in api.calls if x[0] == "buy"]
    assert [b[1] for b in buys] == ["BBB", "DDD", "CCC"], buys
    for _, s, q, lim, stop, tp, cid in buys:
        assert cid.startswith("dip-") and abs(lim - 98) < 1e-9 and abs(stop - 96.04) < 1e-9 and abs(tp - 99.96) < 1e-9
        assert q * lim <= 90000 / C.DIP_MAX_POSITIONS + 1
    assert len(msgs) == 2 and msgs[0].startswith("📥 <b>Emir giriyorum") and "BBB" in msgs[0], msgs
    # 3 pozisyon doldu: AAA için "yer olsaydı alırdım" bildirimi (emir yok)
    assert msgs[1].startswith("📦 <b>Pozisyon sınırı dolu") and "<b>AAA</b> 153 adet @ $98.00" in msgs[1], msgs
    assert "BBB, CCC, DDD" in msgs[1], msgs[1]
    sg = {r["sembol"]: r for r in kayit.signals()}
    assert sg["AAA"]["durum"].startswith("atlandı") and sg["BBB"]["durum"] == "emir verildi", sg
    assert [e["islem"] for e in kayit.events()] == ["EMİR"] * 3
    print("Anlık bildirim:\n" + msgs[0])
    # Aynı akşam tekrar çalışırsa yeni emir yok
    trader.run(summary, by, "post", state, api)
    assert len([x for x in api.calls if x[0] == "buy"]) == 3

    # Ertesi akşam: BBB doldu (seviyeler gerçek fiyata göre düzeltilir), diğerleri 1 seans bekledi
    summary2 = {"lastBar": "2026-11-16", "stocks": [stock("AAA", signal=False)], "picks": []}
    api.open = [o for o in api.open if o["symbol"] != "BBB"] + [
        {"id": "S1", "symbol": "BBB", "side": "sell", "type": "stop", "stop_price": "96.04", "qty": "300"},
        {"id": "T1", "symbol": "BBB", "side": "sell", "type": "limit", "limit_price": "99.96", "qty": "300"}]
    api.pos = [{"symbol": "BBB", "qty": "300", "avg_entry_price": "97.00", "current_price": "97.5",
                "unrealized_pl": "150", "unrealized_plpc": "0.005", "market_value": "29250"},
               {"symbol": "OLD", "qty": "5", "avg_entry_price": "50", "current_price": "55", "unrealized_pl": "25",
                "unrealized_plpc": "0.1", "market_value": "275"}]
    for h in api.hist:
        if h["symbol"] == "BBB":
            h["filled_qty"] = "300"
    m2, port2 = trader.run(summary2, {"AAA": summary2["stocks"][0], "BBB": {"high": 99.5, "low": 96.2}},
                           "post", state, api)
    assert ("stop", "S1", 95.06) in api.calls and ("tp", "T1", 98.94) in api.calls, api.calls
    assert not [m for m in m2 if "alış" in m.lower() and "BBB" in m], m2   # dolum anlık bildirilmez, raporda
    assert any(e["islem"] == "ALIŞ" and e["sembol"] == "BBB" for e in kayit.events())
    rep = run.daily_report({"lastBar": kayit.now_ny().strftime("%Y-%m-%d")}, port2, state)
    assert "BBB" in rep and "Bekleniyor" in rep, rep
    print(rep)
    assert {p["sym"] for p in port2["positions"]} == {"BBB", "OLD"}
    assert not [x for x in api.calls if x[0] == "buy" and x[1] == "AAA"]
    # Eski stratejiden kalan pozisyon kapatılır, anlık bildirim gider; günlüğe yazılır
    assert ("close", "OLD") in api.calls and not [x for x in api.calls if x[0] == "stop_sell" and x[1] == "OLD"], api.calls
    assert any("Eski strateji" in m and "OLD" in m for m in m2), m2
    assert any(e["sembol"] == "OLD" and e["strateji"] == "eski" for e in kayit.events())
    api.pos = [x for x in api.pos if x["symbol"] != "OLD"]

    # 3. akşam: dolmayan alışlar 2 seans sonra iptal
    summary3 = {"lastBar": "2026-11-17", "stocks": [], "picks": []}
    trader.run(summary3, {}, "post", state, api)
    assert ("cancel", "BDDD") in api.calls and ("cancel", "BCCC") in api.calls, api.calls

    # 10 seans dolunca pozisyon kapatılır
    for k in range(4, 12):
        trader.run({"lastBar": f"2026-11-{14+k}", "stocks": [], "picks": []}, {}, "post", state, api)
    assert ("close", "BBB") in api.calls, api.calls

    # Stop emri kaybolursa yeniden kurulur (seans içi)
    api.open = []
    m4, _ = trader.run({"lastBar": "2026-12-01", "stocks": [], "picks": []}, {}, "intraday", state, api)
    assert ("stop_sell", "BBB", 300, 95.06) in api.calls, api.calls

    # Pozisyon kâr al ile kapandı: günlüğe neden ve K/Z yazılır
    api.pos = []
    api.hist.append({"id": "X9", "symbol": "BBB", "side": "sell", "type": "limit", "filled_qty": "300",
                     "filled_avg_price": "98.94", "filled_at": "2026-12-02T15:10:00Z"})
    trader.run({"lastBar": "2026-12-02", "stocks": [], "picks": []}, {}, "intraday", state, api)
    r = {x["sembol"]: x for x in kayit.signals()}["BBB"]
    assert r["durum"] == "kapandı" and r["cikis_nedeni"].startswith("kâr al") and float(r["kar_zarar_usd"]) == round(1.94 * 300, 2), r
    assert float(r["en_yuksek_yuzde"]) > 2 and float(r["en_dusuk_yuzde"]) < 0, r
    assert kayit.stats()["n"] == 1

    # Sıfırlama komutu: tüm pozisyonlar kapanır, günlük arşivlenip yeniden başlar, bir kez uygulanır
    api.pos = [{"symbol": "ZZZ", "qty": "2", "avg_entry_price": "10", "current_price": "11", "unrealized_pl": "2",
                "unrealized_plpc": "0.1", "market_value": "22"}]
    today = trader.datetime.now(trader.ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    Path("komutlar/sifirla.json").write_text(json.dumps([{"tarih": today}]))
    m6, _ = trader.run({"lastBar": today, "stocks": [], "picks": []}, {}, "intraday", state, api)
    assert ("close_all",) in api.calls and m6 and "Sıfırlama" in m6[0], m6
    assert [e["islem"] for e in kayit.events()] == ["SIFIRLAMA"] and not kayit.signals()
    assert kayit.start_info()["sermaye"] == 90000 and "dip" not in state
    assert list(Path("kayitlar/arsiv").iterdir())
    n = api.calls.count(("close_all",))
    trader.run({"lastBar": today, "stocks": [], "picks": []}, {}, "intraday", state, api)
    assert api.calls.count(("close_all",)) == n
    print("Tüm 3 gün düşüş stratejisi testleri geçti.")


if __name__ == "__main__":
    main()
