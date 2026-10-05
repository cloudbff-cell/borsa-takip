# ABD Hisse Takip

Piyasa değerine göre ilk 50 ABD şirketini ve her sektörün ilk 3 liderini her işlem günü otomatik izler:

- **Teknik sinyaller:** trend (EMA 20/50/200), RSI, hacimli kırılım ve destek kırılımı. 8 kuralın tamamı `engine/signals.py` dosyasında ve Pine Script'te aynıdır.
- **Stratejik alım adayları:** sinyal, trend gücü, SPY'a göre göreceli güç, bilanço ve mevsimsellik birleştirilerek 100 üzerinden bir puan çıkarılır. Her aday için alım bölgesi, stop ve iki hedef hesaplanır. Bilançoya 3 gün veya daha az kalan hisseler aday listesine alınmaz.
- **Bilanço ve beklentiler:** sonraki bilanço tarihi, HBK ve gelir beklentisi, son çeyreklerde beklentiyi aşıp aşmadığı, analist hedef fiyatı.
- **Mevsimsellik:** son 10 yılda bu ay ve gelecek ay için ortalama getiri, pozitif yıl oranı ve SPY'a göre fark.
- **Bildirimler (Telegram):** açılıştan önce gün planı, seans içinde saatlik yeni sinyaller, kapanıştan sonra teyitli sinyaller ve gün özeti.
- **Web paneli:** GitHub Pages'te; grafikler, al/sat işaretleri, tablolar.

> Sinyaller kural tabanlıdır ve yatırım tavsiyesi değildir. Emirleri sen verirsin.

## Kurulum (yaklaşık 15 dakika)

### 1. GitHub deposu
1. github.com'da yeni bir **public** depo oluştur, örneğin `borsa-takip`. GitHub Pages ücretsiz planda yalnızca public depolarda çalışır. Depoda sadece kod bulunur; şifreler gizli kalır.
2. Bu klasördeki tüm dosyaları depoya yükle: "Add file > Upload files", ardından `.github` klasörünü de sürükle.
3. **Settings > Pages > Source: GitHub Actions** seç.

### 2. Telegram botu
1. Telegram'da **@BotFather**'a yaz, `/newbot` komutunu gönder, bir isim ver ve sana verilen **token**'ı kopyala.
2. Yeni botuna bir mesaj gönder (örneğin "merhaba").
3. Tarayıcıda `https://api.telegram.org/bot<TOKEN>/getUpdates` adresini aç. `"chat":{"id": ...}` içindeki sayı senin **chat id**'n.
4. Depoda **Settings > Secrets and variables > Actions > New repository secret** bölümüne gir ve iki secret ekle:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`

### 3. İlk çalıştırma
**Actions > Hisse tarama > Run workflow** yolunu izle, mod olarak `post` seç. Çalışma birkaç dakika sürer. Bitince panel `https://<kullanıcı-adın>.github.io/borsa-takip/` adresinde açılır ve Telegram'a özet gelir.

Bundan sonrası otomatik: iş akışı hafta içi her saat çalışır ve New York saatine göre modunu kendisi seçer. Yaz/kış saati farkı da otomatik karşılanır.

| Türkiye saati (yaklaşık) | Mod | Ne gelir |
|---|---|---|
| 15:00–16:30 | Açılış öncesi | Gün planı: bilanço takvimi, stratejik adaylar, mevsimsel hisseler |
| 17:00–23:00 | Seans içi | Yeni sinyaller, saatte bir (kapanışta teyit edilmemiş) |
| 23:00–01:00 | Kapanış sonrası | Teyitli sinyaller ve gün özeti |

GitHub'ın zamanlanmış çalışmaları yoğunlukta 5–20 dakika gecikebilir. Saniyelik takip için TradingView alarmlarını kullan.

## TradingView
- `pine/takip_sinyalleri.pine`: grafikte AL/SAT etiketleri, stop/hedef çizgileri ve alarmlar. Pine Editor'e yapıştır, "Grafiğe ekle"ye bas, ardından "Alarm ekle > Herhangi bir AL" seç.
- `pine/takip_backtest.pine`: aynı kuralların geçmiş veriyle testi. Sonuçlar "Strateji Test Aracı" sekmesinde görünür.

## Ayarlar
`engine/config.py`: aday hisse listesi, ilk kaç şirket (`TOP_N`), sektör başına lider sayısı, RSI/EMA/ATR parametreleri, ADR'ler (TSM, ASML vb.) dahil edilsin mi. Parametreleri değiştirirsen Pine Script'teki girişleri de aynı yap.

## Yerel test
```bash
pip install -r requirements.txt
python tests/test_offline.py          # ağsız, sentetik veriyle
python -m engine.run --mode post --force --no-telegram   # gerçek veriyle, panel site/ klasörüne
cd site && python -m http.server       # http://localhost:8000
```

## Bilinen sınırlar
- Veriler Yahoo Finance'ten (yfinance) gelir: ücretsizdir, resmi değildir, gecikmeli olabilir ve ara sıra boş dönebilir. Boş dönen hisse o çalışmada atlanır.
- Seans içi sinyaller günün tamamlanmamış mumuna dayanır, kapanışta kaybolabilir. Kesin olanlar kapanış sonrası mesajdakilerdir.
- "Geçmiş başarı" istatistikleri işlem maliyeti ve kayma payı içermez. Geçmiş sonuçlar gelecekteki sonuçları garanti etmez.
