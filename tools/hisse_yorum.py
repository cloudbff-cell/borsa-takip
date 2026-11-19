"""Tek hisse için orta vade (dipte yatay) ve kısa vade (3 gün düşüş) kurallarına göre durum. İşlem yapmaz.
Kullanım: python tools/hisse_yorum.py PLTR [AAPL ...]"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import config as C  # noqa: E402
from engine import data as D  # noqa: E402
from engine import dip  # noqa: E402
from engine.ortavade_kural import features_taban, setup_taban, snapshot_taban  # noqa: E402

syms = [s.upper() for s in sys.argv[1:]] or ["PLTR"]
prices = D.download_prices(syms, period="3y")
for s in syms:
    df = prices.get(s)
    if df is None:
        print(s, "veri yok")
        continue
    d = features_taban(df)
    r = d.iloc[-1]
    print(f"\n===== {s} · son bar {d.index[-1]:%Y-%m-%d} · kapanış {r['Close']:.2f}")
    print("orta vade snapshot:", snapshot_taban(df))
    pk = d.index[-1 - int(r['peak_age'])]
    bt = d.index[-1 - int(r['low_age'])]
    print(f"tepe {r['peak']:.2f} ({pk:%Y-%m-%d}) · tepeden önceki dip {r['pre_low']:.2f} · yükseliş %{r['rise']*100:.0f}")
    print(f"son 60 g dip {r['bottom']:.2f} ({bt:%Y-%m-%d}) · tepeden düşüş %{r['drop']*100:.0f} · dibe uzaklık %{r['near']*100:.1f} · {int(r['low_age'])} seanstır yeni dip yok")
    print(f"50g {r['sma50']:.2f} · 150g {r['sma150']:.2f} · 200g {df['Close'].tail(200).mean():.2f} · ATR {r['atr']:.2f}")
    print("son 15 gün (tarih, açılış, yüksek, düşük, kapanış):")
    for i, x in df.tail(15).iterrows():
        print(f"  {i:%m-%d} {x['Open']:.2f} {x['High']:.2f} {x['Low']:.2f} {x['Close']:.2f}")
    sig = setup_taban(d)
    on = d.index[sig.to_numpy()]
    print("son 1 yılda kural sağlanan günler:", ", ".join(f"{x:%Y-%m-%d}" for x in on[on >= d.index[-252]][-20:]) or "yok")
    try:
        print("kısa vade:", dip.signal_view(df))
    except Exception as e:  # noqa: BLE001
        print("kısa vade hesaplanamadı:", e)
