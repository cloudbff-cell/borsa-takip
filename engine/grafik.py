"""İşlem grafikleri: her yeni EMİR / ALIŞ / SATIŞ olayı için Telegram'a grafik gönderir.

Grafik: son ~3 ayın günlük mumları, 20 ve 50 günlük ortalama, alış/satış noktaları (▲/▼) ve
limit, stop, kâr al, alış fiyatı çizgileri. Seviyeler kayitlar/sinyaller.csv'den okunur.
Olaylar kayit.add_event() içinde kayit.NEW listesine düşer; run.py çalışma sonunda send_pending() çağırır.
Grafik hatası hiçbir zaman taramayı ya da emirleri durdurmaz.
"""
import io

import pandas as pd

from . import kayit
from . import telegram

KINDS = ("EMİR", "ALIŞ", "SATIŞ")
COLORS = {"up": "#1a9e5c", "down": "#d64545", "stop": "#d64545", "tp": "#1a9e5c", "entry": "#2f6fdf",
          "limit": "#8a63d2", "ma20": "#e0a030", "ma50": "#7f8c8d"}


def _f(x):
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _signal_for(ev):
    """Olayın ait olduğu sinyal satırı: aynı sembolün en son sinyali (emir/alış tarihine göre)."""
    day = ev.get("tarih") or "9999"
    rows = [r for r in kayit.signals() if r.get("sembol") == ev.get("sembol") and (r.get("sinyal_tarihi") or "") <= day]
    if ev.get("islem") in ("ALIŞ", "SATIŞ"):      # alışı gerçekleşmiş sinyal
        held = [r for r in rows if r.get("alis_fiyati") and (r.get("alis_tarihi") or "") <= day]
        rows = held or rows
    else:                                          # emri verilmiş sinyal
        rows = [r for r in rows if not str(r.get("durum", "")).startswith("atlandı")] or rows
    return rows[-1] if rows else {}


def render(df, ev, sig, sessions=70):
    """PNG (bytes). df: günlük OHLC (Open/High/Low/Close)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    d = df.copy()
    d["ma20"] = d["Close"].rolling(20).mean()
    d["ma50"] = d["Close"].rolling(50).mean()
    d = d.tail(sessions)
    x = range(len(d))
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=110)
    for i, (_, r) in zip(x, d.iterrows()):
        up = r["Close"] >= r["Open"]
        col = COLORS["up"] if up else COLORS["down"]
        ax.vlines(i, r["Low"], r["High"], color=col, linewidth=0.8)
        lo, hi = sorted((r["Open"], r["Close"]))
        ax.bar(i, max(hi - lo, (r["High"] - r["Low"]) * 0.02), bottom=lo, width=0.7, color=col)
    ax.plot(list(x), d["ma20"], color=COLORS["ma20"], linewidth=1, label="20 gün ort.")
    ax.plot(list(x), d["ma50"], color=COLORS["ma50"], linewidth=1, label="50 gün ort.")

    dates = [t.strftime("%Y-%m-%d") for t in d.index]

    def idx(day):
        if not day:
            return None
        if day in dates:
            return dates.index(day)
        later = [i for i, s in enumerate(dates) if s >= day]
        return later[0] if later else len(dates) - 1

    lines = []
    entry = _f(sig.get("alis_fiyati")) if ev["islem"] != "EMİR" else None
    if ev["islem"] == "EMİR":
        lines.append(("limit", _f(ev.get("fiyat")), "Limit"))
    if entry:
        lines.append(("entry", entry, "Alış"))
    lines += [("stop", _f(sig.get("stop")), "Stop"), ("tp", _f(sig.get("kar_al")), "Kâr al")]
    for key, y, name in lines:
        if y:
            ax.axhline(y, color=COLORS[key], linestyle="--", linewidth=1)
            ax.annotate(f"{name} ${y:,.2f}", (len(d) - 0.5, y), xytext=(4, 0), textcoords="offset points",
                        va="center", fontsize=8, color=COLORS[key], annotation_clip=False)

    sig_i = idx(sig.get("sinyal_tarihi"))
    if sig_i is not None and ev["islem"] == "EMİR":
        ax.annotate("sinyal", (sig_i, d["High"].iloc[sig_i]), xytext=(0, 10), textcoords="offset points",
                    ha="center", fontsize=8, color="#555")
    buy_i = idx(sig.get("alis_tarihi"))
    if buy_i is not None and entry:
        ax.scatter([buy_i], [entry], marker="^", s=110, color=COLORS["up"], zorder=5, edgecolors="white")
    if ev["islem"] == "SATIŞ":
        px = _f(ev.get("fiyat")) or _f(sig.get("cikis_fiyati"))
        si = idx(ev.get("tarih"))
        if px and si is not None:
            ax.scatter([si], [px], marker="v", s=110, color=COLORS["down"], zorder=5, edgecolors="white")

    step = max(1, len(d) // 7)
    ax.set_xticks(list(x)[::step])
    ax.set_xticklabels([t.strftime("%d.%m") for t in d.index][::step], fontsize=8)
    ax.tick_params(axis="y", labelsize=8)
    ax.set_xlim(-1, len(d) + 13)
    ax.grid(alpha=0.2)
    ax.legend(loc="upper left", fontsize=7, frameon=False)
    ax.set_title(f"{ev['sembol']} · {ev['islem']} · {ev.get('strateji') or ''} · {ev.get('tarih')}", fontsize=10)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def caption(ev, sig):
    s, k = ev["sembol"], ev["islem"]
    em = {"EMİR": "📥", "ALIŞ": "🟢", "SATIŞ": "⚪"}[k]
    out = [f"{em} <b>{s}</b> {k} · {ev.get('adet') or ''} adet @ ${_f(ev.get('fiyat')) or 0:,.2f}"]
    st, tp = _f(sig.get("stop")), _f(sig.get("kar_al"))
    if k != "SATIŞ" and (st or tp):
        out.append(f"Stop ${st:,.2f} · Kâr al ${tp:,.2f}" if st and tp else "")
    if k == "SATIŞ":
        e = _f(sig.get("alis_fiyati"))
        px = _f(ev.get("fiyat"))
        if e and px:
            out.append(f"Alış ${e:,.2f} → Satış ${px:,.2f} ({(px / e - 1) * 100:+.2f}%)")
    if ev.get("neden"):
        out.append(f"Neden: {ev['neden']}")
    return "\n".join(x for x in out if x)[:1000]


def send_pending(prices=None):
    """Bu çalışmada eklenen olayların grafiklerini gönder. prices: {sembol: günlük df} (yoksa indirilir)."""
    evs = [e for e in kayit.NEW if e.get("islem") in KINDS]
    kayit.NEW.clear()
    if not evs:
        return 0
    prices = dict(prices or {})
    need = sorted({e["sembol"] for e in evs if e["sembol"] not in prices})
    if need:
        try:
            from . import data as D
            prices.update(D.download_prices(need, period="1y"))
        except Exception as ex:  # noqa: BLE001
            print("grafik verisi indirilemedi:", ex)
    n = 0
    for ev in evs:
        try:
            df = prices.get(ev["sembol"])
            if df is None or len(df) < 30:
                continue
            sig = _signal_for(ev)
            if telegram.send_photo(render(df, ev, sig), caption(ev, sig)):
                n += 1
        except Exception as ex:  # noqa: BLE001
            print(f"grafik hatası ({ev.get('sembol')}):", ex)
    return n
