"""Evren, parametreler ve ayarlar.

Aday listesi geniş tutulur; motor her gün piyasa değerine göre sıralayıp
ilk TOP_N şirketi ve her sektörün liderlerini seçer. Listeye hisse eklemek
için CANDIDATES'a sembol eklemen yeterli.
"""

# Yahoo Finance sembolleri (BRK.B -> BRK-B)
CANDIDATES = [
    # Teknoloji / yarı iletken / yazılım
    "NVDA", "AAPL", "MSFT", "AVGO", "MU", "AMD", "ORCL", "CSCO", "INTC", "PLTR",
    "LRCX", "AMAT", "KLAC", "DELL", "PANW", "CRWD", "TXN", "ANET", "SNDK", "MRVL",
    "APH", "IBM", "ADI", "QCOM", "CRM", "NOW", "ADBE", "INTU", "STX", "WDC", "APP",
    # İletişim
    "GOOGL", "META", "NFLX", "TMUS", "DIS", "VZ", "T",
    # Tüketici (döngüsel)
    "AMZN", "TSLA", "HD", "MCD", "BKNG", "LOW", "TJX", "NKE",
    # Tüketici (temel)
    "WMT", "COST", "PG", "KO", "PEP", "PM",
    # Sağlık
    "LLY", "JNJ", "ABBV", "UNH", "MRK", "TMO", "ABT", "ISRG", "AMGN",
    # Finans
    "BRK-B", "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "AXP", "C", "BX", "SCHW",
    # Sanayi
    "GE", "CAT", "RTX", "GEV", "HON", "UBER", "UNP", "ETN", "BA", "DE", "LMT",
    # Enerji
    "XOM", "CVX", "COP",
    # Hammadde
    "LIN", "SHW", "FCX", "NEM", "ECL",
    # Kamu hizmetleri
    "NEE", "SO", "DUK", "CEG", "VST",
    # Gayrimenkul
    "PLD", "AMT", "WELL", "EQIX",
    # Yeni halka arzlar / büyük şirketler (Yahoo'da yoksa sessizce atlanır)
    "SPCX",
    # İslami filtreden sonra ilk 50'yi doldurmak için eklenen büyük şirketler
    "ACN", "SNPS", "CDNS", "ADSK", "FTNT", "WDAY", "SNOW", "DDOG", "NXPI", "MCHP", "ABNB", "DASH",
    "VRTX", "REGN", "GILD", "BSX", "SYK", "MDT", "DHR", "ZTS", "IDXX", "EW", "GEHC", "RMD", "MCK", "COR",
    "CMG", "ORLY", "AZO", "ROST", "LULU", "TGT", "CL", "KMB", "MDLZ", "HSY", "MNST",
    "TT", "PH", "EMR", "ITW", "WM", "CTAS", "MMM", "FDX", "UPS", "CSX", "NSC", "CPRT", "ODFL", "PCAR", "FAST", "MSI",
    "EOG", "SLB", "PSX", "MPC", "OXY", "NUE", "APD",
]

# ABD dışı ama ABD'de işlem gören dev şirketler (ADR). İstersen True yap.
INCLUDE_ADRS = False
ADRS = ["TSM", "ASML", "ARM", "SHOP", "NVS", "AZN", "SAP", "BABA", "HSBC"]

BENCHMARK = "SPY"
TOP_N = 50                   # İslami filtreden GEÇEN şirketler arasından piyasa değerine göre ilk 50

# ---------- İslami (şeriat) uygunluk filtresi (engine/sharia.py) ----------
SHARIA_FILTER = True         # True: evren, adaylar ve bot yalnız uygun hisselerden seçilir
SHARIA_DEBT_MAX = 0.30       # toplam borç / 36 aylık ort. piyasa değeri < %30 (S&P Shariah 2026 / AAOIFI)
SHARIA_CASH_MAX = 0.30       # (nakit + faizli menkul kıymet) / 36 aylık ort. piyasa değeri < %30
SHARIA_SELL_IF_FAILS = True  # elde tutulan hisse uygunluğunu kaybederse kapanışta sat (veri eksikliğinde satmaz)
# Yahoo'nun endüstri sınıfından yakalanamayan faaliyetler (sembol: neden)
SHARIA_EXCLUDE = {
    "MCD": "domuz ürünleri satışı", "YUM": "domuz ürünleri satışı", "SBUX": "domuz ürünleri satışı",
    "CMCSA": "film/TV (NBCUniversal)", "CHTR": "TV yayıncılığı", "SPOT": "müzik", "WBD": "film/TV",
    "PARA": "film/TV", "FOXA": "TV yayıncılığı", "LYV": "müzik/eğlence", "BRK-B": "sigorta/finans",
}
SHARIA_ALLOW = set()         # bir tarayıcıyla teyit edip elle onayladığın semboller (oran testini atlar)

# Kaldıraçsız ve açığa satışsız işlem (faiz/margin yok): alımlar yalnız nakitle yapılır,
# Alpaca hesabı açılışta "no_shorting=true, max_margin_multiplier=1" olarak ayarlanmaya çalışılır.
NO_MARGIN = True
LEADERS_PER_SECTOR = 3

# Teknik parametreler (Pine Script ile aynı tutuldu)
EMA_FAST, EMA_MID, EMA_SLOW = 20, 50, 200
RSI_LEN = 14
RSI_OVERSOLD, RSI_PULLBACK, RSI_OVERBOUGHT = 30, 40, 70
BREAKOUT_LEN = 20
VOLUME_MULT = 1.5
ATR_LEN = 14
STOP_ATR, T1_ATR, T2_ATR = 2.0, 2.0, 4.0
# Giriş kuralları (puan 100 üzerinden)
ENTRY_SIGNAL_MIN_SCORE = 65    # son 3 günde AL sinyali varsa gereken en düşük puan
ENTRY_TREND_MIN_SCORE = 75     # sinyal yoksa: yükseliş trendi + bu puan + 20 EMA'ya geri çekilmede alım
# (65/75, 10 yıllık testte 60/70, 70/80 ve 75/85'e göre en iyi işlem başı sonucu ve en az kriz kaybını verdi)
EARNINGS_GUARD_DAYS = 3        # bilançoya bu kadar gün kala yeni alım sinyali "riskli" işaretlenir
FORWARD_DAYS = 20              # geçmiş sinyal başarısı ölçülürken bakılan gün sayısı

# Mevsimsellik
SEASONAL_YEARS = 10

# Önbellek süresi (saat): temel veriler günde bir yenilenir
FUNDAMENTALS_TTL_HOURS = 20

SECTOR_TR = {
    "Technology": "Teknoloji",
    "Communication Services": "İletişim",
    "Consumer Cyclical": "Tüketici (Döngüsel)",
    "Consumer Defensive": "Tüketici (Temel)",
    "Healthcare": "Sağlık",
    "Financial Services": "Finans",
    "Industrials": "Sanayi",
    "Energy": "Enerji",
    "Basic Materials": "Hammadde",
    "Utilities": "Kamu Hizmetleri",
    "Real Estate": "Gayrimenkul",
}

# ---------- Ana strateji ----------
# "dip": 3 gün üst üste düşen (200 gün ort. üstündeki) hisseye kapanışın %2 altından limitli alış,
#        -%2 stop / +%2 kâr al, en fazla 10 seans (engine/dip.py; 2017-2026 testi tools/down3_analiz.py).
# "eski": puan + AL sinyali + iz süren stop (aşağıdaki RISK_PER_TRADE ... ayarları).
STRATEGY = "dip"
# Telegram: "rapor" = yalnız emir girerken anlık mesaj + gün sonunda tek rapor; "detay" = eski davranış
TELEGRAM_MODE = "rapor"
DIP_MAX_POSITIONS = 3        # her pozisyon sermayenin 1/3'ü (testte 3: yıllık +%21.9, 5: +%15.7, 8: +%9.9)
DIP_LIMIT_BELOW = 0.02       # alış limiti: sinyal günü kapanışının %2 altı
DIP_STOP = 0.02              # stop: alış fiyatının %2 altı
DIP_TP = 0.02                # kâr al: alış fiyatının %2 üstü
DIP_ENTRY_SESSIONS = 2       # limitli alış bu kadar seans dolmazsa iptal
DIP_MAX_DAYS = 10            # pozisyon en fazla bu kadar seans tutulur

# ---------- Otomatik işlem (Alpaca) ----------
# Bot yalnızca TRADING_ENABLED=true ortam değişkeni varsa emir verir.
# ALPACA_PAPER=false yapılmadıkça her zaman SANAL PARA (paper) hesabı kullanılır.
RISK_PER_TRADE = 0.01        # işlem başına sermayenin %1'i riske girer (giriş - stop)
MAX_POSITIONS = 5            # aynı anda en fazla açık pozisyon (bekleyen alım emirleri dahil)
MAX_POSITION_PCT = 0.20      # tek pozisyon sermayenin en fazla %20'si
DAILY_LOSS_LIMIT = 0.02      # gün içi kayıp %2'yi aşarsa yeni alım yapılmaz
TAKE_PROFIT = None           # sabit kâr al yok ("t1"/"t2" verilirse bracket kâr al emri de konur)
TRAIL_ATR = 3.0              # iz süren stop: girişten beri en yüksek fiyat - 3 x ATR (yalnız yukarı taşınır)
BREAKEVEN_AT_T1 = False      # iz süren stop bunu kapsıyor
EXIT_SIGNALS = ["S_BREAKDOWN", "S_TREND", "S_DEATH"]   # elde varsa kapanışta bu sinyallerde çık
EXIT_BEFORE_EARNINGS = True  # bilançodan 1 gün önce pozisyonu kapat

# ---------- Kripto bölümü (BTC, ETH – Alpaca, 7/24) ----------
CRYPTO_ENABLED = False      # kapalı (kullanıcı isteği): kripto alımı ve analizi yapılmaz; True yaparak geri açılır
CRYPTO = {"BTC-USD": "BTC/USD", "ETH-USD": "ETH/USD"}   # Yahoo sembolü -> Alpaca sembolü
CRYPTO_ALLOC = 0.10          # sermayenin %10'u kripto bölümü; hisse bölümü kalan %90 ile hesaplanır
CRYPTO_RISK = 0.005          # kripto işlem başına sermayenin %0.5'i riske girer (giriş - stop)
CRYPTO_TRAIL_ATR = 3.0       # iz süren stop (hisselerle aynı)
CRYPTO_EXIT_SIGNALS = []     # 10 yıllık testte SAT sinyali çıkışları kriptoda sonucu kötüleştirdi; yalnız stoplar
CRYPTO_STOP_LIMIT_GAP = 0.02 # stop-limit emrinde limit fiyatı stopun %2 altı (kriptoda düz stop emri yok)

# ---------- Günlük işlem (day trading) – engine/daytrade.py, .github/workflows/gunluk.yml ----------
# Strateji: açılış aralığı kırılımı (ORB), yalnız ALIŞ (açığa satış yok), kaldıraç yok, her gün kapanıştan
# önce tüm pozisyonlar kapatılır (gece pozisyon taşınmaz). Evren: İslami filtreden geçen ilk 50.
# Açmak için: Actions Variables -> DAYTRADE_ENABLED=true (ve TRADING_ENABLED=true).
DAYTRADE_ALLOC = 0.30        # sermayenin %30'u günlük işleme ayrılır; swing bot kalan %70 ile hesaplar
DT_RISK = 0.01               # işlem başına günlük-işlem sermayesinin %1'i riske girer (giriş - stop)
DT_MAX_POSITIONS = 3         # aynı anda en fazla açık günlük pozisyon
DT_MAX_TRADES = 4            # günde en fazla giriş
DT_DAILY_LOSS = 0.03         # günlük-işlem sermayesinin %3'ü kaybedilirse o gün yeni giriş yok
DT_OR_MIN = 15               # açılış aralığı: ilk 15 dakika (09:30-09:45 New York)
DT_BAR_MIN = 5               # 5 dakikalık mumlar
DT_ENTRY_END = "11:30"       # bu saatten sonra yeni giriş yok (New York)
DT_FLATTEN = "15:50"         # tüm günlük pozisyonlar bu saatte kapatılır (erken kapanış günlerinde kapanış -10 dk)
DT_RVOL_MIN = 1.5            # açılış aralığı hacmi, son 10 günün ortalamasının en az 1.5 katı
DT_MAX_GAP = 0.04            # açılış boşluğu %4'ten büyükse (haber riski) işlem yok
DT_STOP_ATR = 0.35           # stop: max(açılış aralığı dibi, giriş - 0.35 x günlük ATR)
DT_MAX_CHASE_ATR = 0.25      # fiyat kırılım seviyesinden 0.25 günlük ATR'den fazla uzaklaştıysa kovalamaz
DT_TARGET_R = 2.0            # kâr al: giriş + 2R
DT_BREAKEVEN_R = 1.0         # fiyat +1R'ye ulaşınca stop girişe çekilir
DT_MIN_PRICE = 10.0
DT_FILL_TIMEOUT_MIN = 3      # limitli giriş bu kadar dakikada dolmazsa iptal
DT_HISTORY_DAYS = 10         # göreceli hacim için geçmiş gün sayısı
