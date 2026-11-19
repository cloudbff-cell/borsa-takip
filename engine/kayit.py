"""İşlem günlüğü: tarih bazlı, nedenleriyle (repoda kayitlar/ klasöründe CSV, panelde tablo).

kayitlar/sinyaller.csv : her sinyal bir satır – sinyal anındaki koşullar + sonucu (doldu mu, ne zaman,
                         hangi fiyattan, nasıl kapandı, K/Z, tutma süresi, en yüksek/en düşük görülen %).
                         Botu analiz etmek için ana tablo.
kayitlar/islemler.csv  : her olay bir satır – EMİR (limitli alış girildi), ALIŞ (doldu), SATIŞ, İPTAL.
kayitlar/baslangic.json: sıfırlama tarihi ve başlangıç sermayesi.
"""
import csv
import json
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DIR = Path("kayitlar")
SIG = DIR / "sinyaller.csv"
EVT = DIR / "islemler.csv"
START = DIR / "baslangic.json"
NY = ZoneInfo("America/New_York")

SIG_COLS = ["anahtar", "sinyal_tarihi", "sembol", "sirket", "sektor", "strateji", "durum",
            "kapanis", "limit", "dusus_3g_yuzde", "sma200_uzaklik_yuzde", "rsi14", "atr_yuzde", "hacim_orani",
            "puan", "piyasa_degeri_mr", "borc_orani", "nakit_orani", "spy_trend", "spy_gun_yuzde", "sira",
            "ayni_gun_sinyal", "adet", "alis_tarihi", "alis_saati", "alis_fiyati", "stop", "kar_al",
            "cikis_tarihi", "cikis_saati", "cikis_fiyati", "cikis_nedeni", "kar_zarar_usd", "kar_zarar_yuzde",
            "tutma_seans", "en_yuksek_yuzde", "en_dusuk_yuzde",
            "giris_turu", "evre", "taban_derinlik_yuzde", "sma150_uzaklik_yuzde", "getiri_3a_yuzde"]
EVT_COLS = ["tarih", "saat", "sembol", "islem", "adet", "fiyat", "tutar", "neden", "strateji", "emir_id", "anahtar"]


def _read(p):
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write(p, rows, cols):
    DIR.mkdir(exist_ok=True)
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def now_ny():
    return datetime.now(NY)


def add_event(tarih=None, saat=None, **kw):
    """Olay ekle; aynı 'anahtar' varsa tekrar yazma. Eklendiyse True."""
    rows = _read(EVT)
    key = kw.get("anahtar")
    if key and any(r.get("anahtar") == key for r in rows):
        return False
    n = now_ny()
    kw["tarih"] = tarih or n.strftime("%Y-%m-%d")
    kw["saat"] = saat or n.strftime("%H:%M")
    if kw.get("adet") and kw.get("fiyat"):
        kw["tutar"] = round(float(kw["adet"]) * float(kw["fiyat"]), 2)
    rows.append(kw)
    _write(EVT, rows, EVT_COLS)
    return True


def upsert_signal(key, **kw):
    rows = _read(SIG)
    for r in rows:
        if r.get("anahtar") == key:
            r.update({k: v for k, v in kw.items() if v is not None})
            break
    else:
        rows.append(dict(kw, anahtar=key))
    _write(SIG, rows, SIG_COLS)


def signals():
    return _read(SIG)


def events():
    return _read(EVT)


def start_info():
    return json.loads(START.read_text()) if START.exists() else {}


def reset(equity):
    """Eski kayıtları arsiv/ altına taşı, boş günlük ve başlangıç bilgisi oluştur."""
    n = now_ny()
    if SIG.exists() or EVT.exists():
        arc = DIR / "arsiv" / n.strftime("%Y-%m-%d_%H%M")
        arc.mkdir(parents=True, exist_ok=True)
        for p in (SIG, EVT, START):
            if p.exists():
                shutil.move(str(p), arc / p.name)
    _write(SIG, [], SIG_COLS)
    _write(EVT, [], EVT_COLS)
    START.write_text(json.dumps({"tarih": n.strftime("%Y-%m-%d %H:%M"), "sermaye": round(equity, 2)}, ensure_ascii=False))


def publish(site):
    """Panel için kopya (CSV indirme + JSON tablo)."""
    d = Path(site) / "data"
    d.mkdir(parents=True, exist_ok=True)
    for p in (SIG, EVT):
        if p.exists():
            shutil.copy(p, d / p.name)
    (d / "kayit.json").write_text(json.dumps({"baslangic": start_info(), "sinyaller": signals()[-300:],
                                              "islemler": events()[-500:]}, ensure_ascii=False))


def stats():
    """Kapanmış işlemlerin özeti."""
    closed = [r for r in signals() if r.get("durum") == "kapandı" and r.get("kar_zarar_usd")]
    pl = [float(r["kar_zarar_usd"]) for r in closed]
    pc = [float(r["kar_zarar_yuzde"]) for r in closed]
    if not closed:
        return {"n": 0}
    return {"n": len(closed), "win": sum(1 for x in pl if x > 0) / len(pl) * 100, "pl": sum(pl),
            "avg": sum(pc) / len(pc)}
