# ABD Hisse Takip

**İslami kurallara uygun** hisseler arasından piyasa değerine göre ilk 50 ABD şirketini ve her sektörün ilk 3 liderini her işlem günü otomatik izler; isteğe bağlı olarak swing (birkaç gün–hafta) ve **günlük (day trading)** otomatik işlem yapar:

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
python tests/test_daytrade.py         # günlük işlem + İslami filtre (sahte Alpaca)
python tests/test_dip.py              # ana strateji: 3 gün düşüş
python -m engine.run --mode post --force --no-telegram   # gerçek veriyle, panel site/ klasörüne
cd site && python -m http.server       # http://localhost:8000
```

## Bilinen sınırlar
- Veriler Yahoo Finance'ten (yfinance) gelir: ücretsizdir, resmi değildir, gecikmeli olabilir ve ara sıra boş dönebilir. Boş dönen hisse o çalışmada atlanır.
- Seans içi sinyaller günün tamamlanmamış mumuna dayanır, kapanışta kaybolabilir. Kesin olanlar kapanış sonrası mesajdakilerdir.
- "Geçmiş başarı" istatistikleri işlem maliyeti ve kayma payı içermez. Geçmiş sonuçlar gelecekteki sonuçları garanti etmez.

## Ana strateji: 3 gün düşüş + %2 aşağıdan limitli alış (`engine/dip.py`, `STRATEGY = "dip"`)
- **Sinyal:** İslami filtreden geçen ilk 50 hissede, fiyat 200 günlük ortalamanın üstündeyken 3 gün üst üste düşük kapanış. Aynı gün birden fazla sinyal varsa 3 günlük düşüşü en büyük olan önce.
- **Alış:** kapanıştan sonra, kapanışın %2 altına limitli alış emri. 2 seans içinde dolmazsa iptal.
- **Satış:** alışla birlikte −%2 stop ve +%2 kâr al emri (dolum fiyatına göre ayarlanır). 10 seans içinde ikisi de olmazsa pozisyon kapatılır.
- **Boyut:** en fazla 3 pozisyon, her biri sermayenin 1/3'ü; yalnız nakit, kaldıraç ve açığa satış yok.
- **Neden:** 2017-2026 testinde (`tools/down3_analiz.py`) kapanışta almak −%2 stopla kârsızdı. Sinyalden sonra fiyat ortalama %2,8 daha düşüyor ve dip çoğunlukla ertesi gün geliyor. %2 aşağıdan alınca −%2/+%2 kuralıyla yıllık +%21,9 (3 pozisyon), en büyük düşüş −%6; üç alt dönemin hepsinde kârlı. Günlük veride aynı gün hem stop hem hedef görülen işlemler stop sayıldı; hisse listesi bugünün büyükleri olduğu için sonuçlar biraz iyimser.
- **Eski strateji:** puan + AL sinyali + iz süren stop kuralı yeni alım yapmaz. Eldeki eski pozisyonlar kendi stoplarıyla kapanana kadar korunur. Geri dönmek için `config.py` → `STRATEGY = "eski"`.

## Orta vade stratejisi: evre 2 + 3 aylık taban kırılımı (`engine/ortavade.py`, kurallar `engine/ortavade_kural.py`)
Bir hissenin dört evresi: **taban → yükseliş (evre 2) → tepe → düşüş**. Alım yalnız yükseliş evresinin başında yapılır; tepe ve düşüş evresinde alım yok.
- **1 yıllık grafik (evre 2 teyidi):** fiyat 150 ve 200 günlük ortalamanın üstünde, 150 g ortalama son 1 ayda yükselmiş, 50 g > 150 g, fiyat 52 hafta dibinin en az %25 üstünde ve zirvesinin en çok %25 altında. Fiyat 150 g ortalamanın %40'tan fazla üstündeyse alınmaz (geç kalınmış, tepeye yakın).
- **3 aylık grafik (giriş):** son 3 ayın tabanı en fazla %25 derinlikte ve bugün kapanış bu tabanın zirvesinin üstünde, hacim 50 günlük ortalamanın ≥ 1,3 katı. Piyasa filtresi: SPY 200 g ortalamanın üstünde.
- **Alış:** kapanıştan sonra, ertesi seans için kapanışın en çok %1 üstüne limitli alış (fiyat boşlukla kaçarsa kovalanmaz, emir iptal). Emre bağlı −%8 stop ve +%25 kâr al. En fazla ~1 yıl (252 seans) tutulur.
- **Boyut:** sermayenin %50'si orta vadeye (`OV_ALLOC`), 4 eşit pozisyon; kısa vade (3 gün düşüş) kalan %50 ile çalışmaya devam eder. Emir kimliği `ov-`; iki strateji birbirinin pozisyonuna dokunmaz, aynı hisseye iki kez girmez.
- **Test (`tools/ortavade_test.py`, Actions → *Orta vade geri testi*, 2017-12 – 2026-11, günlük piyasa değerli portföy):** yıllık +%16,1, en büyük düşüş −%18,6, kazanma %47, ortalama işlem +%6,7, ortalama ~80 gün (SPY al-tut +%13,6 / −%33,7). Üç alt dönemin hepsinde pozitif ama 2017-19 ve 2023-26'da SPY'ın gerisinde; asıl fark düşük düşüş (2022'de +%9, SPY −%18). **Uyarı:** aynı kurallar daha geniş 117 hisselik İslami evrende yıllık +%7,4 verdi; ilk 50 bugünün kazananları olduğu için sonuç iyimserdir.
- Kapatmak için `config.py` → `OV_ENABLED = False`.

## Bildirimler ve işlem günlüğü
- **Telegram (`TELEGRAM_MODE = "rapor"`):** yalnız iki tür mesaj gelir: (1) bot limitli alış emri girerken anlık "📥 Emir giriyorum" mesajı (hisse, adet, limit, stop, kâr al, neden); (2) kapanıştan sonra tek **gün sonu raporu**: bugün alınanlar, kapananlar (neden ve K/Z), iptal edilen emirler, açık pozisyonlar ve her biri için ne beklendiği (kâr al / stop seviyesi ve uzaklığı, süre), bekleyen limitli alışlar, başlangıçtan beri getiri.
- **Günlük (`kayitlar/`, bot her çalışmada depoya yazar; panelde tablo ve CSV indirme):**
  - `sinyaller.csv`: her sinyal bir satır – sinyal anındaki koşullar (3 günlük düşüş, 200 gün ortalamasına uzaklık, RSI, ATR %, hacim oranı, puan, piyasa değeri, borç/nakit oranı, SPY trendi ve günlük değişimi, aynı gün kaç sinyal olduğu ve sırası) ve sonucu (emir verildi/atlandı/iptal, alış tarihi-saati-fiyatı, çıkış tarihi-saati-fiyatı, çıkış nedeni, K/Z $ ve %, tutma süresi, pozisyon süresince en yüksek ve en düşük %).
  - `islemler.csv`: her olay bir satır – EMİR, ALIŞ, SATIŞ, İPTAL, SIFIRLAMA; tarih, saat (New York), adet, fiyat, tutar, neden.
  - `baslangic.json`: başlangıç tarihi ve sermayesi.
- **Sıfırlama:** `komutlar/sifirla.json` içine `[{"tarih": "YYYY-MM-DD"}]` yazılırsa o gün (New York) tüm pozisyonlar kapatılır, emirler iptal edilir, eski günlük `kayitlar/arsiv/` altına taşınır ve günlük sıfırdan başlar. Alpaca sanal hesabının bakiyesini sıfırlamak yalnız Alpaca panelinden yapılabilir.
- Günlük işlem (day trading) iş akışının zamanlaması kapalı; elle çalıştırılabilir.

## Otomatik işlem botu – eski strateji (Alpaca, varsayılan: sanal para)
- **Giriş:** Seans içindeki her taramada stratejik adaylar için alım bölgesinin üst sınırına limitli alış emri konur. Emre stop ve kâr al emirleri bağlıdır. Fiyat bölgeye indiğinde emri Alpaca anında gerçekleştirir.
- **Güncelleme:** Aday listesinden çıkan ya da fiyatı alım bölgesinin altına düşen hissenin emri iptal edilir. Bölgesi %1'den fazla değişen hissenin emri yeni seviyeyle yeniden konur. Gün sonunda gerçekleşmemiş alış emirleri iptal edilir.
- **Çıkış:** Stop ya da kâr al emri tetiklenince pozisyon kapanır. Kapanışta teyit edilen bir SAT sinyali gelirse ya da bilançoya 1 gün kalırsa da satılır. Fiyat ilk hedefe ulaşınca stop giriş fiyatına çekilir.
- **Risk:** İşlem başına sermayenin %1'i riske girer. Aynı anda en fazla 5 pozisyon açılır ve tek pozisyon sermayenin en fazla %20'si olur. Günlük kayıp %2'yi aşarsa o gün yeni alım yapılmaz.
- **Açma/kapama:** Actions Variables altındaki `TRADING_ENABLED` değeri `true` ise bot çalışır, `false` ise durur. Gerçek hesaba geçmek için `ALPACA_PAPER` değişkenini `false` yapmak ve gerçek hesabın API anahtarlarını girmek gerekir.

## İslami uygunluk filtresi (`engine/sharia.py`)
S&P 500 Shariah / SPUS yöntemine yakın iki katmanlı tarama; ilk 50, sektör liderleri, alım adayları ve her iki bot yalnız uygun hisselerden seçer:
- **Faaliyet:** konvansiyonel finans (banka, sigorta, Visa/Mastercard gibi kart ağları, aracı kurum, borsa), alkol, tütün, kumar, otel-kumarhane, domuz ürünleri (ör. MCD), film/TV/müzik (ör. NFLX, DIS), sağlık sigortası (ör. UNH) ve savunma sanayi elenir.
- **Oranlar:** toplam borç / 36 aylık ortalama piyasa değeri < %30 ve (nakit + faizli yatırım) / 36 aylık ortalama piyasa değeri < %30 (`SHARIA_DEBT_MAX`, `SHARIA_CASH_MAX`).
- **İşlem şekli:** kaldıraç (margin) ve açığa satış yok. Alımlar yalnız nakitle yapılır; bot Alpaca hesabını her gün `no_shorting=true`, `max_margin_multiplier=1` olarak ayarlamaya çalışır.
- Elde tutulan hisse uygunluğunu kaybederse kapanışta satılır (veri eksikliğinden kaynaklanan durumlarda satılmaz).
- Elenenler ve nedenleri panelde ve sabah planında görünür. Yahoo verisi resmî değildir ve bu bir fetva değildir; kesin karar için Zoya/Musaffa gibi bir tarayıcıyla teyit et. Elle düzeltme: `config.py` → `SHARIA_EXCLUDE` / `SHARIA_ALLOW`. Temettülerde arındırma (purification) gerekiyorsa bot bunu hesaplamaz.

## Günlük işlem / day trading (`engine/daytrade.py`, iş akışı: **Günlük işlem**)
- **Strateji:** açılış aralığı kırılımı (ORB). İlk 15 dakikanın zirvesi, 09:45–11:30 (New York) arasında 5 dakikalık mum kapanışıyla yukarı kırılırsa alınır. Şartlar: fiyat VWAP üstünde, açılış aralığı hacmi son 10 günün ≥ 1,5 katı, günlük trend yukarı, açılış boşluğu < %4, bugün bilanço yok, SPY de VWAP üstünde. Yalnız alış.
- **Emir:** limitli alış + bağlı stop ve kâr al (bracket, yalnız o gün geçerli). Stop = max(aralık dibi, giriş − 0,35 günlük ATR), kâr al = 2R, +1R'de stop girişe çekilir. 3 dakikada dolmayan emir iptal edilir.
- **Gün sonu:** 15:50'de (erken kapanış günlerinde kapanıştan 10 dk önce) tüm günlük pozisyonlar kapatılır; gece pozisyon taşınmaz. Tarama iş akışı 15:52'den sonra açık kalan günlük pozisyon görürse yedek olarak kapatır.
- **Risk:** sermayenin %30'u günlük işleme ayrılır (`DAYTRADE_ALLOC`; swing bot kalan %70 ile çalışır). İşlem başına bu payın %1'i risk, en fazla 3 açık pozisyon, günde 4 giriş, günlük-işlem kaybı %3'ü aşarsa yeni giriş yok.
- **Ayrım:** günlük emirlerin kimliği `dt-` ile başlar; swing bot bu hisselere dokunmaz, günlük bot da swing botun elindeki hisselere girmez.
- **Çalışma:** iş akışı seans boyunca açık kalır (her gün iki tetikleme; GitHub'da tek iş en fazla 6 saat). Veri: Alpaca piyasa verisi (ücretsiz IEX akışı; ücretli SIP için `ALPACA_DATA_FEED=sip`). Telegram'a her giriş/çıkış ve gün sonu özeti gelir.
- **Açma/kapama:** varsayılan **sinyal modu**: kurallar her gün canlı çalışır, sinyaller Telegram'a gelir ama emir verilmez ve swing botun sermayesi bölünmez. Emirleri açmak için Actions Variables → `DAYTRADE_ENABLED` = `true` (`TRADING_ENABLED=true` da gerekir).
- **İlk geri test sonucu (Kasım 2026, son ~60 işlem günü):** varsayılan kurallarla 44 işlem, %25 kazanma, kâr faktörü 0,42, günlük-işlem sermayesinde ≈ −%15. Denenen hiçbir varyasyon (RVOL 1/2, hedef 1,5R/3R, trend filtresiz) kârlı değildi. Bu yüzden emirler kapalı başlar.
- **Geri test:** Actions → *Günlük işlem geri testi* son ~60 işlem gününün 5 dakikalık verisiyle kuralları ve birkaç ayar varyasyonunu test eder (sonuç iş özetinde).
- Not: ABD'de 4 Haziran 2026'dan itibaren "pattern day trader" kuralı ve 25.000 $ alt sınırı kaldırıldı; yine de aracı kurumun kendi kuralları geçerlidir.

## Kripto bölümü (BTC, ETH)
- Sermayenin %10'u; işlem başına sermayenin %0,5'i risk, coin başına en fazla %5. Hisse bölümü kalan %90 ile hesaplanır.
- Puan: teknik 40 + 90 günlük momentum 20 + hacim teyidi 5 (100'e ölçeklenir). Giriş kuralları hisselerle aynı (65/75).
- Emir: limitli alış (GTC); dolunca stop-limit satış. İz süren stop 3 ATR. Fiyat stopun altına boşlukla inerse piyasa emriyle kapatılır.
- SAT sinyali çıkışı yok (10 yıllık testte kriptoda sonucu kötüleştirdi). 7/24 çalışır; her UTC günü kapanışında Telegram'a kripto özeti.
